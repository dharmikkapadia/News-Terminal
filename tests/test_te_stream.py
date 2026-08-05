"""Tests for te_stream.py — the three parsers (HTML / JSON / RSS) on fixture
payloads modelled on what Trading Economics' stream can serve, the timestamp
reader, the India filter, item identity, and the candidate-chain fetch.

TE can't be reached from CI or the dev sandbox (Cloudflare + datacenter-IP
blocks), so these fixtures — not the live site — are what pins the parsers'
behaviour; the first live Action run is the integration test, with the
`te-stream-dump` artifact for tuning.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import te_stream

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://tradingeconomics.com/stream"


# TE's stream page: repeated item blocks, each a headline link into a country
# page, a description, a relative timestamp and a flag image naming the country.
STREAM_HTML = """
<html><body>
<div class="stream-items-wrap">
  <div class="stream-item">
    <img class="flag" src="/flags/in.png" alt="India"/>
    <h3 class="stream-item-title"><a href="/india/inflation-cpi">India Inflation Rate Eases to 2.1% in June</a></h3>
    <div class="stream-item-description">Annual inflation rate in India eased to 2.1% in June
      from 2.8% in May, below market forecasts of 2.4%.</div>
    <span class="stream-item-date">14 minutes ago</span>
  </div>
  <div class="stream-item">
    <img class="flag" src="/flags/us.png" alt="United States"/>
    <h3 class="stream-item-title"><a href="/united-states/non-farm-payrolls">US Payrolls Rise 147K</a></h3>
    <div class="stream-item-description">The US economy added 147,000 jobs in June.</div>
    <span class="stream-item-date">2 hours ago</span>
  </div>
  <div class="stream-item">
    <h3 class="stream-item-title"><a href="/brazil/interest-rate">Brazil Holds Selic at 15%</a></h3>
    <div class="stream-item-description">The Copom kept the Selic rate unchanged.</div>
    <span class="stream-item-date"><time datetime="2026-07-24T18:30:00Z">Jul 24</time></span>
  </div>
</div>
</body></html>
"""

# The stream's XHR endpoint: a bare list of records, TE-ish field names.
STREAM_JSON = json.dumps([
    {"id": 8814, "title": "India Rupee Falls to 86.4 per USD",
     "description": "The Indian rupee weakened past 86 against the dollar.",
     "url": "/india/currency", "date": "2026-07-26T09:15:00", "country": "India",
     "importance": 2},
    {"id": 8815, "Title": "Germany Ifo Business Climate Improves",
     "Description": "The Ifo index rose to 89.0 in July.",
     "URL": "https://tradingeconomics.com/germany/business-confidence",
     "Date": "/Date(1753500600000)/", "Country": "Germany"},
    {"id": 8816, "title": "", "description": "no headline — dropped", "url": "/x", "date": ""},
])

STREAM_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Trading Economics Stream</title>
  <item>
    <title>India Trade Deficit Narrows</title>
    <link>https://tradingeconomics.com/india/balance-of-trade</link>
    <description>&lt;p&gt;The trade gap narrowed to $18.8B.&lt;/p&gt;</description>
    <pubDate>Fri, 25 Jul 2026 06:30:00 GMT</pubDate>
  </item>
  <item>
    <title>Japan Tokyo CPI Slows</title>
    <link>https://tradingeconomics.com/japan/inflation-cpi</link>
    <description>Tokyo core inflation slowed to 2.9%.</description>
    <pubDate>Fri, 25 Jul 2026 00:30:00 GMT</pubDate>
  </item>
</channel></rss>
"""


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def test_parse_stream_html_fixture():
    items, err = te_stream.parse_stream(STREAM_HTML, BASE)
    assert err is None
    assert len(items) == 3
    cpi = items[0]
    assert cpi["title"] == "India Inflation Rate Eases to 2.1% in June"
    assert cpi["link"] == "https://tradingeconomics.com/india/inflation-cpi"
    assert cpi["summary"].startswith("Annual inflation rate in India eased to 2.1%")
    assert cpi["country"] == "India"
    assert cpi["key"].startswith("te:")
    # "14 minutes ago" resolved against now, and `published` rewritten absolute so
    # the history file never freezes a stale relative string.
    assert abs(cpi["ts"] - (datetime.now(IST) - timedelta(minutes=14)).timestamp()) < 90
    assert "ago" not in cpi["published"] and cpi["published"].endswith("IST")
    assert items[1]["country"] == "United States"
    # A <time datetime="…Z"> attribute wins over the printed text, zone honoured.
    assert items[2]["ts"] == int(datetime(2026, 7, 24, 18, 30, tzinfo=timezone.utc).timestamp())


def test_parse_stream_html_blocked_page():
    items, err = te_stream.parse_stream("<html><body>Attention Required! | Cloudflare</body></html>", BASE)
    assert items == [] and err


def test_parse_stream_html_wrapper_does_not_swallow_the_page():
    """When only the substring probe matches, it hits the wrapper AND each item
    ('stream-items' contains 'stream-item'). Only the innermost matches may win —
    otherwise the whole page collapses into one item carrying every headline."""
    html = """
    <div class="stream-items">
      <div class="stream-item-content">
        <h3><a href="/india/inflation-cpi">India CPI Eases</a></h3>
        <div class="description">Inflation eased to 2.1%.</div><span class="date">5 minutes ago</span>
      </div>
      <div class="stream-item-content">
        <h3><a href="/china/gdp-growth">China GDP Beats</a></h3>
        <div class="description">GDP rose 5.2%.</div><span class="date">1 hour ago</span>
      </div>
    </div>
    """
    items, err = te_stream.parse_stream(html, BASE)
    assert err is None
    assert [i["title"] for i in items] == ["India CPI Eases", "China GDP Beats"]
    assert items[0]["summary"] == "Inflation eased to 2.1%."


def test_parse_stream_html_generic_article_markup():
    """No stream-* classes at all — the generic <article> probe still reads it."""
    html = ("<html><body><article><h2><a href='/india/gdp-growth'>India GDP Grows 7.4%</a></h2>"
            "<p>GDP expanded 7.4% year on year.</p>"
            "<span class='date'>3 hours ago</span></article></body></html>")
    items, err = te_stream.parse_stream(html, BASE)
    assert err is None and len(items) == 1
    assert items[0]["title"] == "India GDP Grows 7.4%"
    assert items[0]["summary"] == "GDP expanded 7.4% year on year."
    assert items[0]["country"] == "india"          # inferred from the link's country page


# --------------------------------------------------------------------------- #
# JSON / RSS
# --------------------------------------------------------------------------- #
def test_parse_stream_json_fixture():
    items, err = te_stream.parse_stream_json(STREAM_JSON, BASE)
    assert err is None
    assert len(items) == 2                        # the title-less record is dropped
    inr = items[0]
    assert inr["title"] == "India Rupee Falls to 86.4 per USD"
    assert inr["link"] == "https://tradingeconomics.com/india/currency"
    assert inr["country"] == "India"
    # No zone on the record's date -> TE's own zone, not IST.
    assert inr["ts"] == int(datetime(2026, 7, 26, 9, 15, tzinfo=te_stream.STREAM_TZ).timestamp())
    ifo = items[1]                                # capitalised keys + a .NET date
    assert ifo["title"] == "Germany Ifo Business Climate Improves"
    assert ifo["ts"] == 1753500600


def test_parse_stream_json_wrapped_and_bad_payloads():
    wrapped = json.dumps({"status": "ok", "Items": json.loads(STREAM_JSON)})
    items, err = te_stream.parse_stream_json(wrapped, BASE)
    assert err is None and len(items) == 2
    assert te_stream.parse_stream_json("[]", BASE)[0] == []
    assert te_stream.parse_stream_json("not json", BASE)[1]


def test_parse_stream_rss_fixture():
    pytest.importorskip("feedparser")
    items, err = te_stream.parse_stream_rss(STREAM_RSS, BASE)
    assert err is None and len(items) == 2
    assert items[0]["title"] == "India Trade Deficit Narrows"
    assert items[0]["summary"] == "The trade gap narrowed to $18.8B."   # HTML stripped
    assert items[0]["ts"] == int(datetime(2026, 7, 25, 6, 30, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------- #
# timestamps / identity / filtering
# --------------------------------------------------------------------------- #
def test_parse_when_shapes():
    """Absolute times carry TE's zone (STREAM_TZ, observed as UTC); relative ones are
    zone-free; a bare DATE stays IST-midnight so it renders as a plain date."""
    TE_TZ = te_stream.STREAM_TZ
    now = datetime(2026, 7, 26, 15, 0, tzinfo=IST)
    pw = te_stream.parse_when
    assert pw("14 minutes ago", now) == int((now - timedelta(minutes=14)).timestamp())
    assert pw("an hour ago", now) == int((now - timedelta(hours=1)).timestamp())
    assert pw("just now", now) == int(now.timestamp())
    assert pw("2026-07-24T18:30:00Z", now) == int(datetime(2026, 7, 24, 18, 30, tzinfo=timezone.utc).timestamp())
    assert pw("2026-07-24 18:30", now) == int(datetime(2026, 7, 24, 18, 30, tzinfo=TE_TZ).timestamp())
    assert pw("2026-07-24", now) == int(datetime(2026, 7, 24, tzinfo=IST).timestamp())
    assert pw("Jul 24, 2026", now) == int(datetime(2026, 7, 24, tzinfo=IST).timestamp())
    assert pw("24 July 2026 09:15", now) == int(datetime(2026, 7, 24, 9, 15, tzinfo=TE_TZ).timestamp())
    assert pw("Jul/24", now) == int(datetime(2026, 7, 24, tzinfo=IST).timestamp())
    assert pw("09:15", now) == int(datetime(2026, 7, 26, 9, 15, tzinfo=TE_TZ).timestamp())
    assert pw(1753500600) == 1753500600 and pw(1753500600000) == 1753500600
    assert pw("") is None and pw(None) is None and pw("sometime soon") is None


def test_parse_when_absolute_times_are_read_in_tes_zone_not_ist():
    """Regression for the first live poll: TE stamped a 'SENSEX Index Closes' story
    10:30, which is 16:00 IST (just after the 15:30 close) — not 10:30 IST, five
    hours BEFORE it. Absolute TE stamps must not be read as IST."""
    now = datetime(2026, 7, 26, 11, 39, tzinfo=IST)
    ts = te_stream.parse_when("2026-07-24 10:30", now)
    assert datetime.fromtimestamp(ts, IST).strftime("%H:%M") == "16:00"


def test_parse_when_bare_clock_time_rolls_back_over_midnight():
    TE_TZ = te_stream.STREAM_TZ
    # 00:05 in TE's zone — last night's item, read just after TE's midnight.
    now = datetime(2026, 7, 26, 0, 5, tzinfo=TE_TZ)
    assert te_stream.parse_when("23:50", now) == int(datetime(2026, 7, 25, 23, 50, tzinfo=TE_TZ).timestamp())
    assert te_stream.parse_when("00:02", now) == int(datetime(2026, 7, 26, 0, 2, tzinfo=TE_TZ).timestamp())


def test_parse_when_bare_month_day_rolls_back_a_year():
    now = datetime(2026, 1, 3, 10, 0, tzinfo=IST)          # a Dec date read in early Jan
    assert te_stream.parse_when("Dec 28", now) == int(datetime(2025, 12, 28, tzinfo=IST).timestamp())


def test_key_is_stable_across_source_shapes_and_ages():
    """The same story served as HTML and as JSON must key identically, and must not
    re-key as its printed timestamp ages from relative to absolute."""
    a = te_stream._mkitem("India CPI Eases", "/india/inflation-cpi", "body",
                          "2 minutes ago", "India", BASE)
    b = te_stream._mkitem("India CPI Eases", "https://tradingeconomics.com/india/inflation-cpi",
                          "body", "Jul 24, 2026", "", BASE)
    assert a["key"] == b["key"]
    assert a["key"] != te_stream._mkitem("India WPI Eases", "/india/inflation-cpi", "b",
                                         "", "", BASE)["key"]


def test_mkitem_drops_headline_echo_and_untitled_records():
    it = te_stream._mkitem("India CPI Eases", "/india/inflation-cpi",
                           "India CPI Eases — the rate fell to 2.1%.", "", "", BASE)
    assert it["summary"] == "the rate fell to 2.1%."
    assert te_stream._mkitem("   ", "/x", "body", "", "", BASE) is None


def test_india_filter():
    keep = te_stream._is_india
    assert keep({"country": "India", "title": "x", "link": ""})
    assert keep({"country": "", "title": "Anything", "link": "https://tradingeconomics.com/india/gdp"})
    assert keep({"country": "", "title": "Indian Rupee Slips", "link": ""})   # headline fallback
    assert not keep({"country": "United States", "title": "US Payrolls Rise", "link": "/united-states/x"})
    assert not keep({"country": "", "title": "Indiana Farm Prices Rise", "link": ""})  # word-boundary


# --------------------------------------------------------------------------- #
# fetching: the candidate chain
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body, status=200, ctype="text/html"):
        self.text = body
        self.content = body.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def _router(routes):
    """requests.get stub: map url -> _Resp (or an exception to raise)."""
    def get(url, **kw):
        resp = routes.get(url)
        if resp is None:
            raise requests.ConnectionError(f"no route for {url}")
        if isinstance(resp, Exception):
            raise resp
        return resp
    return get


def test_fetch_stream_falls_through_to_the_first_working_candidate(monkeypatch):
    urls = "https://x/json,https://x/html"
    monkeypatch.setattr(requests, "get", _router({
        "https://x/json": _Resp("<html>Attention Required</html>", ctype="text/html"),  # blocked
        "https://x/html": _Resp(STREAM_HTML),
    }))
    items, err = te_stream.fetch_stream(urls)
    assert err is None and len(items) == 3
    assert [i["ts"] for i in items] == sorted((i["ts"] for i in items), reverse=True)


def test_fetch_stream_reports_every_candidate_when_all_fail(monkeypatch):
    monkeypatch.setattr(requests, "get", _router({}))         # every URL raises
    items, err = te_stream.fetch_stream("https://x/a,https://x/b")
    assert items == []
    assert "https://x/a" in err and "https://x/b" in err


def test_fetch_india_filters_the_global_stream(monkeypatch):
    monkeypatch.setattr(requests, "get", _router({BASE: _Resp(STREAM_HTML)}))
    items, err = te_stream.fetch_india(BASE)
    assert err is None
    assert [i["title"] for i in items] == ["India Inflation Rate Eases to 2.1% in June"]
    world, err = te_stream.fetch_world(BASE)
    assert err is None and len(world) == 2
    assert "India Inflation Rate Eases to 2.1% in June" not in [i["title"] for i in world]


def test_fetch_sniffs_json_by_body_when_content_type_lies(monkeypatch):
    monkeypatch.setattr(requests, "get", _router({
        "https://x/j": _Resp(STREAM_JSON, ctype="text/plain"),
    }))
    items, err = te_stream.fetch_stream("https://x/j")
    assert err is None and len(items) == 2


def test_fetch_stream_never_raises_on_a_garbage_response(monkeypatch):
    monkeypatch.setattr(requests, "get", _router({"https://x/a": _Resp("", ctype="application/json")}))
    items, err = te_stream.fetch_stream("https://x/a")
    assert items == [] and err
