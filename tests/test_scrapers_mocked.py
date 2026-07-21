"""Characterization tests for the TE-table and Yahoo-chart scrapers with requests
mocked at the `requests` module level — the same patch point works before and
after the common.py consolidation (both resolve `requests.get` at call time)."""

from datetime import datetime, timedelta, timezone

import requests

import commodities
import rates

IST = timezone(timedelta(hours=5, minutes=30))


class _Resp:
    def __init__(self, html=None, payload=None):
        self.content = (html or "").encode("utf-8")
        self.text = html or ""
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _te_row(symbol, p, nch, pch, date_cell):
    return (f'<tr data-symbol="{symbol}"><td>x</td><td id="p">{p}</td>'
            f'<td id="nch">{nch}</td><td id="pch">{pch}</td>'
            f'<td id="date">{date_cell}</td></tr>')


def test_fetch_te_commodities(monkeypatch):
    today = datetime.now(IST).date()
    html = ("<html><table>"
            + _te_row("CO1:COM", "78.50", "-0.55", "-0.70%", f"{today:%b}/{today.day}")
            + _te_row("XAUUSD:CUR", "4,087.01", "12.01", "0.29%", "12:09")
            + _te_row("UNRELATED:COM", "1.00", "0", "0%", "Jan/01")
            + "</table></html>")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(html=html))
    quotes, err = commodities.fetch_te()
    assert err is None
    assert set(quotes) == {"brent", "gold"}                  # unrelated symbol ignored
    brent = quotes["brent"]
    assert brent["price"] == 78.5
    assert brent["prev_close"] == 79.05                       # price - net change
    assert brent["change_pct"] == -0.7                        # TE's own signed %
    assert brent["currency"] == "USD"
    assert brent["as_of"] == today.isoformat()
    gold = quotes["gold"]
    assert gold["price"] == 4087.01
    assert gold["prev_close"] == 4075.0
    assert gold["as_of"] is None                              # time cell -> None (commodities)


def test_fetch_te_fx(monkeypatch):
    today = datetime.now(IST).date()
    past = today - timedelta(days=2)
    html = ("<html><table>"
            + _te_row("USDINR:CUR", "86.9950", "0.1750", "0.20%", "12:09")
            + _te_row("EURINR:CUR", "101.2300", "-0.5100", "-0.50%", f"{past:%b}/{past.day}")
            + "</table></html>")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(html=html))
    quotes, err = rates.fetch_te_fx()
    assert err is None
    assert set(quotes) == {"inr_per_usd", "inr_per_eur"}
    usd = quotes["inr_per_usd"]
    assert usd["price"] == 86.995
    assert usd["prev_close"] == 86.82
    assert usd["change_pct"] == 0.2
    assert usd["as_of"] == today.isoformat()                  # time cell -> today (FX)
    assert quotes["inr_per_eur"]["as_of"] == past.isoformat()


def test_fetch_te_error_when_no_rows(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(html="<html></html>"))
    quotes, err = commodities.fetch_te()
    assert quotes == {} and err
    quotes, err = rates.fetch_te_fx()
    assert quotes == {} and err


def _yahoo_payload(ts_closes, currency="USD"):
    ts = [t for t, _ in ts_closes]
    closes = [c for _, c in ts_closes]
    return {"chart": {"result": [{
        "meta": {"currency": currency},
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def test_yahoo_chart_quote(monkeypatch):
    t2 = int(datetime.now(IST).timestamp())
    t1 = t2 - 86400
    payload = _yahoo_payload([(t1, 100.0), (t2, 103.0)])
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(payload=payload))
    for fetch in (commodities.fetch_one, rates._fetch_yahoo_one):
        q, err = fetch("BZ=F", session=None)
        assert err is None, fetch.__module__
        assert q["price"] == 103.0
        assert q["prev_close"] == 100.0
        assert q["change_pct"] == 3.0
        assert q["as_of"] == datetime.fromtimestamp(t2, IST).strftime("%Y-%m-%d")


def test_yahoo_chart_quote_needs_two_closes(monkeypatch):
    payload = _yahoo_payload([(1752000000, 100.0)])
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(payload=payload))
    q, err = commodities.fetch_one("BZ=F", session=None)
    assert q is None and "closes" in err
