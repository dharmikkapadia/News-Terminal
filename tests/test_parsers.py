"""Characterization tests for the HTML parsers (SEBI listing, investing.com bonds,
RBI archive dates) on literal fixture markup — no network."""

import calendar
from datetime import datetime, timedelta, timezone

import bonds
import rbi_archive
import sebi

IST = timezone(timedelta(hours=5, minutes=30))


# SEBI's real row markup nests a SECOND <a> (a related PDF) inside the detail <a>'s
# own content, unescaped — the parser must keep it out of the headline.
SEBI_HTML = """
<html><body><table id="sample_1">
<tr><td>Date</td><td>Title</td></tr>
<tr><td>Jul 16, 2026</td><td>
  <a href="/filings/public-issues/jul-2026/foo-industries_85001.html">Foo Industries Limited
    <a href="/web/abridged_foo.pdf">Abridged Prospectus</a></a>
</td></tr>
<tr><td>Jul 15, 2026</td><td>
  <a href="/filings/public-issues/jul-2026/bar-tech_85002.html">Bar Tech Limited - DRHP</a>
</td></tr>
</table></body></html>
"""


def _fake_get(html_text):
    class Resp:
        content = html_text.encode("utf-8")
        text = html_text
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("not json")

    return lambda *a, **kw: Resp()


def test_sebi_listing_parses_nested_anchor_rows(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", _fake_get(SEBI_HTML))
    items, err = sebi.fetch_listing("https://www.sebi.gov.in/sebiweb/home/HomeAction.do?x=1")
    assert err is None
    assert len(items) == 2
    top = items[0]
    assert top["title"] == "Foo Industries Limited"          # inner <a> text kept out
    assert top["link"].endswith("/filings/public-issues/jul-2026/foo-industries_85001.html")
    assert top["link"].startswith("https://www.sebi.gov.in/")  # relative href resolved
    assert "Related document: Abridged Prospectus" in top["summary"]
    assert "abridged_foo.pdf" in top["summary"]
    expected_ts = int(datetime(2026, 7, 16, tzinfo=IST).timestamp())
    assert top["ts"] == expected_ts                           # midnight IST
    assert items[1]["title"] == "Bar Tech Limited - DRHP"
    assert items[1]["summary"] == ""


BONDS_HTML = """
<html><body><table>
<tr><th>Name</th><th>Yield</th><th>Prev.</th><th>Chg. %</th></tr>
<tr><td><a href="/rates-bonds/india-10-year-bond-yield">India 10Y</a></td>
    <td>6.680</td><td>6.700</td><td>-0.30%</td></tr>
<tr><td><a href="/rates-bonds/india-3-month-bond-yield">India 3M</a></td>
    <td>5.450</td><td>5.440</td><td>+0.18%</td></tr>
<tr><td><a href="/rates-bonds/india-40-year-bond-yield">India 40Y</a></td>
    <td>7.100</td><td>7.100</td><td>0.00%</td></tr>
</table></body></html>
"""


def test_parse_bonds_reads_by_header_and_sorts_by_maturity():
    curve, err = bonds.parse_bonds(BONDS_HTML, url="https://www.investing.com/rates-bonds/x")
    assert err is None
    assert [b["tenor"] for b in curve] == ["3M", "10Y", "40Y"]   # ascending maturity
    ten_y = curve[1]
    assert ten_y["yield"] == 6.68
    assert ten_y["prev_close"] == 6.7
    assert ten_y["change_pct"] == -0.3
    assert ten_y["years"] == 10.0
    assert ten_y["chart_url"] == "https://www.investing.com/rates-bonds/india-10-year-bond-yield"
    assert bonds._is_complete(curve)                # 10Y present, all yields in 2–15%
    assert bonds.benchmark(curve)["tenor"] == "10Y"


def test_parse_bonds_empty_on_no_rows():
    curve, err = bonds.parse_bonds("<html><table><tr><th>Nope</th></tr></table></html>")
    assert curve == [] and err


def test_rbi_archive_date_formats():
    for text, ymd in [
        ("Jul 2, 2026", (2026, 7, 2)),
        ("July 2, 2026", (2026, 7, 2)),
        ("2 Jul 2026", (2026, 7, 2)),
        ("02/07/2026", (2026, 7, 2)),
    ]:
        ts, raw = rbi_archive._parse_date(text)
        assert raw and ts == calendar.timegm(
            datetime(*ymd, tzinfo=IST).utctimetuple()), text
    assert rbi_archive._parse_date("no date here") == (None, "")
