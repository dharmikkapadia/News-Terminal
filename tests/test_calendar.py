"""Tests for econ_calendar.py — parser on fixture markup modelled on TE's calendar
table, the accumulate/merge model, and the guarded poll write."""

import json
from datetime import datetime, timedelta, timezone

import requests

import econ_calendar

IST = timezone(timedelta(hours=5, minutes=30))


CAL_HTML = """
<html><body><table id="calendar">
<thead class="table-header"><tr><th>Thursday July 23 2026</th></tr></thead>
<tr data-id="in-cpi-jun" data-url="/india/inflation-cpi" data-importance="3">
  <td>12:00 PM</td>
  <td><a href="/india/inflation-cpi">Inflation Rate YoY JUN</a></td>
  <td><span id="actual"></span></td>
  <td><span id="previous">5.30%</span></td>
  <td><span id="consensus">5.40%</span></td>
  <td><span id="forecast">5.2%</span></td>
</tr>
<tr data-id="in-wpi-jun" data-url="/india/producer-prices-change">
  <td>2:30 PM</td>
  <td><a href="/india/producer-prices-change">WPI Inflation YoY JUN</a></td>
  <td><span id="actual">2.61%</span></td>
  <td><span id="previous">2.74%</span></td>
  <td><span id="consensus">2.50%</span></td>
  <td><span id="forecast"></span></td>
</tr>
<thead class="table-header"><tr><th>Friday July 24 2026</th></tr></thead>
<tr data-url="/india/foreign-exchange-reserves" data-importance="1">
  <td>5:00 PM</td>
  <td><a href="/india/foreign-exchange-reserves">Foreign Exchange Reserves</a></td>
  <td><span id="actual"></span></td>
  <td><span id="previous">$698.9B</span></td>
  <td><span id="consensus"></span></td>
  <td><span id="forecast"></span></td>
</tr>
<tr><td>not an event row (no data-url)</td></tr>
</table></body></html>
"""


def test_parse_calendar_fixture():
    events, err = econ_calendar.parse_calendar(CAL_HTML, "https://tradingeconomics.com/india/calendar")
    assert err is None
    assert len(events) == 3
    cpi = events[0]
    assert cpi["id"] == "in-cpi-jun"
    assert cpi["date"] == "2026-07-23"
    assert cpi["time"] == "12:00 PM"
    assert cpi["event"] == "Inflation Rate YoY JUN"
    assert cpi["url"] == "https://tradingeconomics.com/india/inflation-cpi"
    assert cpi["importance"] == 3
    assert cpi["actual"] == "" and cpi["consensus"] == "5.40%" and cpi["previous"] == "5.30%"
    wpi = events[1]
    assert wpi["actual"] == "2.61%" and wpi["importance"] is None
    fx = events[2]
    assert fx["date"] == "2026-07-24"
    assert fx["id"] == "2026-07-24|Foreign Exchange Reserves"   # no data-id -> composite key
    assert fx["previous"] == "$698.9B"


def test_parse_calendar_empty_on_block_page():
    events, err = econ_calendar.parse_calendar("<html><body>Attention Required</body></html>")
    assert events == [] and err


def test_time_minutes():
    tm = econ_calendar._time_minutes
    assert tm("12:00 PM") == 720
    assert tm("12:30 AM") == 30
    assert tm("2:30 PM") == 870
    assert tm("09:15") == 555
    assert tm("") is None and tm(None) is None and tm("noon") is None


def test_merge_fills_actuals_and_keeps_rolled_out_events():
    today = datetime.now(IST).date()
    d_recent = (today - timedelta(days=2)).isoformat()
    d_old = (today - timedelta(days=30)).isoformat()
    prior = [
        {"id": "a", "date": d_recent, "event": "CPI", "actual": "", "consensus": "5.4%",
         "first_seen": "x"},
        {"id": "gone-but-recent", "date": d_recent, "event": "WPI", "actual": "2.6%"},
        {"id": "ancient", "date": d_old, "event": "Old", "actual": "1%"},
    ]
    fresh = [
        {"id": "a", "date": d_recent, "event": "CPI", "actual": "5.08%", "consensus": "5.4%"},
        {"id": "new", "date": (today + timedelta(days=5)).isoformat(), "event": "GDP", "actual": ""},
    ]
    merged = econ_calendar.merge_events(prior, fresh, today=today)
    by_id = {e["id"]: e for e in merged}
    assert by_id["a"]["actual"] == "5.08%"              # released actual filled in
    assert by_id["a"]["first_seen"] == "x"              # identity preserved
    assert "updated_at" in by_id["a"]
    assert "gone-but-recent" in by_id                   # rolled out of TE's window, kept
    assert "ancient" not in by_id                       # pruned past KEEP_PAST_DAYS
    assert "new" in by_id and "first_seen" in by_id["new"]


def test_merge_empty_fresh_field_does_not_wipe():
    today = datetime.now(IST).date()
    d = today.isoformat()
    prior = [{"id": "a", "date": d, "event": "CPI", "consensus": "5.4%"}]
    fresh = [{"id": "a", "date": d, "event": "CPI", "consensus": ""}]
    merged = econ_calendar.merge_events(prior, fresh, today=today)
    assert merged[0]["consensus"] == "5.4%"


class _Resp:
    def __init__(self, html):
        self.text = html
        self.content = html.encode("utf-8")
        self.status_code = 200

    def raise_for_status(self):
        pass


def _dated_fixture(days_from_today=1):
    """The fixture HTML re-dated near today so merge pruning keeps its events."""
    d = datetime.now(IST).date() + timedelta(days=days_from_today)
    return CAL_HTML.replace("Thursday July 23 2026", f"Thursday {d:%B} {d.day} {d.year}") \
                   .replace("Friday July 24 2026", f"Friday {d:%B} {d.day} {d.year}")


def test_poll_calendar_writes_and_guards(monkeypatch, tmp_path):
    path = str(tmp_path / "calendar.json")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(_dated_fixture()))
    status = econ_calendar.poll_calendar(path=path, url="https://x/calendar")
    assert "calendar updated" in status
    snap = json.load(open(path))
    assert snap["source"] == "Trading Economics"
    assert len(snap["events"]) == 3
    # Second poll on a blocked page must keep the committed snapshot untouched.
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp("<html>blocked</html>"))
    status = econ_calendar.poll_calendar(path=path, url="https://x/calendar")
    assert "keeping committed snapshot" in status
    assert len(json.load(open(path))["events"]) == 3
