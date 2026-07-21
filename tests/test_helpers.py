"""Characterization tests for the small shared helpers.

Written against the module-level names (rates._num, history._key, …) that survive
the common.py consolidation as aliases — so the SAME suite proves behavior is
unchanged before and after the refactor.
"""

from datetime import datetime, timedelta, timezone

import bonds
import commodities
import history
import rates
import rbi_archive
import store

IST = timezone(timedelta(hours=5, minutes=30))

# (input, expected) — the three _num copies must agree on all of these.
NUM_CASES = [
    (None, None),
    ("", None),
    ("abc", None),
    ("6.68", 6.68),
    ("5.25%", 5.25),
    ("−0.34%", -0.34),          # unicode minus (TE renders negatives with it)
    ("-4.34", -4.34),
    ("+1.2%", 1.2),
    ("4,087.01", 4087.01),
    ("₹ 13,600", 13600.0),
    ("12,34,567.89", 1234567.89),   # Indian digit grouping
    ("77,708.52 as on", 77708.52),
    (42, 42.0),
]


def test_num_copies_agree():
    for fn in (rates._num, commodities._num, bonds._num):
        for s, want in NUM_CASES:
            assert fn(s) == want, f"{fn.__module__}._num({s!r})"


def test_item_key_prid_id_and_link_fallback():
    for fn in (history._key, store._key):
        assert fn({"link": "https://rbi.org.in/x.aspx?prid=61234"}) == "61234"
        assert fn({"link": "https://rbi.org.in/x.aspx?PRID=61234"}) == "61234"
        assert fn({"link": "https://rbi.org.in/N.aspx?Id=12995&Mode=0"}) == "12995"
        # SEBI-style link with no prid/Id keys on the link itself
        sebi_link = "https://www.sebi.gov.in/filings/public-issues/jul-2026/x_85001.html"
        assert fn({"link": sebi_link}) == sebi_link
        assert fn({"link": ""}) == ""
        assert fn({}) == ""


def test_rbi_archive_link_key_returns_none_without_match():
    assert rbi_archive._key("https://rbi.org.in/x.aspx?prid=777") == "777"
    assert rbi_archive._key("https://rbi.org.in/x.aspx?Id=888") == "888"
    assert rbi_archive._key("https://rbi.org.in/plain.aspx") is None


def test_te_date_cells():
    today = datetime.now(IST).date()
    # A past-or-near month/day stays in the current year.
    past = today - timedelta(days=3)
    cell = f"{past:%b}/{past.day}"
    assert commodities._te_date(cell) == past.isoformat()
    assert rates._fx_asof(cell) == past.isoformat()
    # A month/day >7 days in the future rolls back a year.
    fut = today + timedelta(days=30)
    got = commodities._te_date(f"{fut:%b}/{fut.day}")
    assert got is not None and got.startswith(str(fut.year - 1))
    # Time-of-day cells: today for FX, None for commodities (documented difference).
    assert rates._fx_asof("12:09") == today.isoformat()
    assert commodities._te_date("12:09") is None
    for fn in (commodities._te_date, rates._fx_asof):
        assert fn(None) is None
        assert fn("garbage") is None


def test_bond_benchmark_picks_closest_to_10y():
    curve = [
        {"tenor": "1Y", "years": 1.0, "yield": 5.6},
        {"tenor": "9Y", "years": 9.0, "yield": 6.6},
        {"tenor": "15Y", "years": 15.0, "yield": 7.0},
        {"tenor": "bad", "years": None, "yield": 6.0},
    ]
    assert bonds.benchmark(curve)["tenor"] == "9Y"
    assert bonds.benchmark([]) is None
    assert bonds.benchmark(None) is None


def test_rates_range_and_merge():
    assert rates._range("6.00% - 6.60%") == [6.0, 6.6]
    assert rates._range("2.50%") == [2.5, 2.5]
    assert rates._range(None) is None
    merged = rates._merge(
        {"a": 1, "mpc": {"next_meeting_start": "2026-08-03"}, "nested": {"keep": 1}},
        {"a": 2, "b": None, "nested": {"keep": None, "new": 3}, "empty": []},
    )
    assert merged["a"] == 2
    assert "b" not in merged or merged.get("b") is None  # None never overwrites
    assert merged["mpc"] == {"next_meeting_start": "2026-08-03"}  # untouched block kept
    assert merged["nested"] == {"keep": 1, "new": 3}
    assert "empty" not in merged  # empty list doesn't overwrite/create
