#!/usr/bin/env python3
"""bonds.py — India Government Bond yield curve from investing.com (browser-rendered).

This REPLACES RBI as the source of the government-bond / bill yields shown in the Current
Rates dashboard — both the "10Y G-Sec" signal tile and the yield rows of the Market Trends
panel. investing.com blocks bots and renders the table client-side, so the page is fetched
in a REAL browser via Scrapling (reusing rates_scrapling._render — Chromium/DynamicFetcher
first, stealth Firefox/StealthyFetcher with Cloudflare solving as the anti-bot fallback) and
parsed with BeautifulSoup.

It overlays data/rates.json's `market_trends.bonds` block and rides the SAME 30-min
poll.py / history.yml cron as the RSS feeds, the commodities strip and the Trading-Economics
FX overlay — bond yields move intraday, so (like FX/commodities) they refresh every 30 min,
NOT on the once-a-day rates.yml.

Guarded exactly like commodities.poll_commodities / rates.poll_fx: it WRITES ONLY on a sane
parse (a ~10Y benchmark present AND every yield in-bounds), preserves the committed block on
any failure, and NEVER raises — a blocked/partial render just keeps the last good snapshot.

WHERE THIS RUNS: a host with open egress to investing.com — a GitHub Action runner
(history.yml) or a real desk. The project's allowlisted sandbox can't reach investing.com
(the egress proxy denies the CONNECT, same as rbi.org.in), so this can only be validated in
CI or on a real desk — the first run is its real test.

Requires: pip install "scrapling[fetchers]" beautifulsoup4  +  `scrapling install`.
"""

import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import rates              # reuse RATES_PATH / IST / save_rates (single source of truth)

# The investing.com India Government Bonds board. Default = the FULL curve (all maturities:
# short bills through 40Y), so ALL the Market-Trends yields come from one source (Call Money
# Rate is dropped in the app). The maturity-filtered board
# (…india-government-bonds?maturity_from=10&maturity_to=300 — long bonds only) can be set via
# MARKETWIRE_INVESTING_BONDS_URL if you'd rather show just the 10Y+ end.
BONDS_BOARD_URL = "https://www.investing.com/rates-bonds/india-government-bonds"
BONDS_URL = os.environ.get("MARKETWIRE_INVESTING_BONDS_URL", BONDS_BOARD_URL)
RENDER_TIMEOUT_MS = int(os.environ.get("MARKETWIRE_RENDER_TIMEOUT_MS", "90000"))
# investing.com hard-blocks datacenter IPs (Cloudflare 403). A (residential) proxy is the
# reliable way through from CI; set MARKETWIRE_SCRAPE_PROXY to a proxy URL to use it.
SCRAPE_PROXY = os.environ.get("MARKETWIRE_SCRAPE_PROXY", "").strip() or None
# Where to dump the rendered HTML for markup diagnosis (a CI run uploads it as an artifact).
_DUMP_PATH = os.environ.get("MARKETWIRE_BONDS_DUMP", "").strip()

RATES_PATH = rates.RATES_PATH
IST = rates.IST

# Sane bounds for an Indian sovereign yield (percent). Anything outside ⇒ a mis-parse; the
# whole write is then skipped so a garbage render can't clobber the committed curve.
_YIELD_BOUNDS = (2.0, 15.0)
_BENCH_YEARS = 10.0                      # the tenor the "10Y G-Sec" tile tracks


def _num(s):
    """First signed number in `s` as float, tolerant of commas and the unicode minus
    ('−0.34%' -> -0.34, '6,68' stays as digits), else None."""
    if s is None:
        return None
    s = str(s).replace("−", "-")
    m = re.search(r"-?\d[\d,]*\.?\d*", s)
    return float(m.group(0).replace(",", "")) if m else None


def _signed_num(cell):
    """Number from a table cell, applying a sign from red/green colour hints when the text
    itself is unsigned (investing.com sometimes conveys direction only by cell colour)."""
    if cell is None:
        return None
    txt = cell.get_text(" ", strip=True)
    v = _num(txt)
    if v is None:
        return None
    if not re.search(r"[+\-−]", txt):                       # unsigned text → infer from colour
        blob = (" ".join(cell.get("class") or []) + " " + (cell.get("style") or "")).lower()
        if any(k in blob for k in ("red", "minus", "down", "neg")):
            v = -abs(v)
    return v


def _tenor_years(label):
    """Parse an investing.com bond name to a maturity in years: 'India 10Y'→10, 'India 6M'→0.5,
    'India 3M'→0.25, '10-Year'→10, '52 Week'→1. None if no tenor is found."""
    if not label:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(Y(?:ear|r)?|M(?:onth|o)?|W(?:eek|k)?|D(?:ay)?)s?\b",
                  label, re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)[0].upper()
    return {"Y": n, "M": n / 12.0, "W": n / 52.0, "D": n / 365.0}.get(unit)


def _short_label(label):
    """'India 10Y' -> '10Y' (strip a leading country word so rows read compactly)."""
    if not label:
        return label
    return re.sub(r"^\s*india\s+", "", label, flags=re.I).strip() or label


def _header_cols(table):
    """Map an investing.com bonds table's header cells to column indices for the fields we
    need. Header-driven (not id/attr-driven) so the parser tolerates markup churn."""
    ths = table.find_all("th")
    if not ths:                                             # some tables header via the first row
        first = table.find("tr")
        ths = first.find_all(["td", "th"]) if first else []
    headers = [th.get_text(" ", strip=True).lower() for th in ths]

    def col(*needles):
        for i, h in enumerate(headers):
            if any(n in h for n in needles):
                return i
        return None

    return {
        "name": col("name"),
        "yield": col("yield"),
        "prev": col("prev"),
        "chg_pct": col("chg. %", "chg %", "change %", "chg.%", "chg%"),
    }


def parse_bonds(html_text, url=BONDS_URL):
    """Parse investing.com's India Government Bonds table into an ascending yield curve
    [{tenor, label, yield, prev_close, change_pct, years, chart_url}]. Returns (curve, error).

    Robust to markup churn: it scans every <table>, keeps the one yielding the most rows whose
    NAME parses to a tenor, reads the yield/prev/chg% columns by HEADER text, and takes the
    bond-name link as the row label + per-bond chart URL. Best-effort — an empty parse yields
    an error and the caller preserves the committed snapshot."""
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:                                 # bs4 not installed
        return [], f"BeautifulSoup unavailable: {ex}"
    soup = BeautifulSoup(html_text, "html.parser")

    best = []
    for table in soup.find_all("table"):
        cols = _header_cols(table)
        yi = cols["yield"]
        if yi is None:                                      # not a yields table
            continue
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) <= yi:
                continue
            a = tr.find("a")                                # bond name links to its yield page
            name = (a.get_text(" ", strip=True) if a and a.get_text(strip=True)
                    else cells[cols["name"]].get_text(" ", strip=True)
                    if cols["name"] is not None and cols["name"] < len(cells)
                    else cells[0].get_text(" ", strip=True))
            years = _tenor_years(name)
            if years is None:                               # header/spacer/non-bond row
                continue
            y = _num(cells[yi].get_text(" ", strip=True))
            if y is None:
                continue
            pi, ci = cols["prev"], cols["chg_pct"]
            prev = _num(cells[pi].get_text(" ", strip=True)) if pi is not None and pi < len(cells) else None
            chg = _signed_num(cells[ci]) if ci is not None and ci < len(cells) else None
            if prev is None and isinstance(chg, (int, float)) and chg not in (0, -100):
                prev = y / (1 + chg / 100.0)                # derive prev close from the % change
            rows.append({
                "tenor": _short_label(name),
                "label": name.strip(),
                "yield": round(y, 4),
                "prev_close": round(prev, 4) if isinstance(prev, (int, float)) else None,
                "change_pct": round(chg, 2) if isinstance(chg, (int, float)) else None,
                "years": round(years, 4),
                "chart_url": urljoin(url, a["href"]) if a and a.get("href") else BONDS_BOARD_URL,
            })
        if len(rows) > len(best):
            best = rows

    if not best:
        return [], "no bond rows parsed (markup changed or blocked)"
    # De-dupe by tenor (keep first), order the curve by ascending maturity.
    seen, curve = set(), []
    for b in sorted(best, key=lambda r: r["years"]):
        if b["tenor"] in seen:
            continue
        seen.add(b["tenor"])
        curve.append(b)
    return curve, None


def benchmark(curve, target_years=_BENCH_YEARS):
    """The ~10Y benchmark bond (maturity closest to `target_years`), or None."""
    best = None
    for b in curve or []:
        yrs, y = b.get("years"), b.get("yield")
        if not isinstance(yrs, (int, float)) or not isinstance(y, (int, float)):
            continue
        d = abs(yrs - target_years)
        if best is None or d < best[0]:
            best = (d, b)
    return best[1] if best else None


def _is_complete(curve):
    """True only if a ~10Y benchmark resolved AND every parsed yield is in-bounds — the gate
    that stops a partial/garbage render from overwriting the committed bond block."""
    if not curve or benchmark(curve) is None:
        return False
    return all(
        isinstance(b.get("yield"), (int, float)) and _YIELD_BOUNDS[0] <= b["yield"] <= _YIELD_BOUNDS[1]
        for b in curve
    )


def _env_flag(name, default=False):
    """Read a boolean-ish env var; empty/unset → `default`."""
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _maybe_dump(html):
    """Write the rendered HTML to MARKETWIRE_BONDS_DUMP if set (a CI run can upload it as an
    artifact to diagnose the markup / confirm a block page). Best-effort; never raises."""
    if not (_DUMP_PATH and html):
        return
    try:
        with open(_DUMP_PATH, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass


def _render(url, timeout=RENDER_TIMEOUT_MS):
    """Fetch investing.com in Scrapling's STEALTH browser and return (html, error).

    investing.com hard-blocks bots from datacenter IPs (Cloudflare 403), so this is tuned to
    look like a real user and NOT hang on investing.com's never-idle ad/tracker traffic:
      • StealthyFetcher only — plain Chromium (DynamicFetcher) is pointless against Cloudflare;
      • solve_cloudflare + arrive via a Google click + block ads so the DOM actually settles;
      • network_idle=False — waiting for 'load'/network-idle just times out on investing.com
        (that was the original failure: `Page.goto ... waiting until "load"` → 60s timeout);
      • non-headless by default (set MARKETWIRE_BONDS_HEADLESS=true to override) — run under a
        virtual display (xvfb) in CI; headless stealth Chrome is easier for Cloudflare to flag;
      • MARKETWIRE_SCRAPE_PROXY routes through a (residential) proxy — the RELIABLE fix from CI,
        since a GitHub-runner IP is usually 403'd however good the browser fingerprint is.
    A 403/429 (or empty) response comes back as an error so the caller preserves the snapshot."""
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception as ex:                       # scrapling/browser not installed
        return None, f"scrapling unavailable: {type(ex).__name__}: {ex}"
    kw = dict(
        headless=_env_flag("MARKETWIRE_BONDS_HEADLESS", default=False),
        solve_cloudflare=True,       # attempt the Cloudflare/Turnstile challenge
        google_search=True,          # arrive via a Google results click (organic-looking)
        network_idle=False,          # investing.com's trackers never idle → don't wait on it
        load_dom=True,
        block_ads=True,              # fewer ad/tracker requests → the page settles + parses
        wait=4000,                   # let the client-rendered yields table paint post-challenge
        timeout=timeout,
    )
    if SCRAPE_PROXY:
        kw["proxy"] = SCRAPE_PROXY
    try:
        page = StealthyFetcher.fetch(url, **kw)
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    status = getattr(page, "status", None)
    html = getattr(page, "html_content", None) or ""
    _maybe_dump(html)                             # dump even a block page for diagnosis
    if html.strip() and status not in (403, 429):
        return html, None
    return None, f"blocked/empty (status={status}, html={len(html)}B)"


def fetch_bonds(url=BONDS_URL, timeout=RENDER_TIMEOUT_MS):
    """Render investing.com's India Government Bonds board and parse the yield curve.
    Returns (curve, error). Never raises for the expected failure modes (render blocked,
    scrapling missing, markup change) — those come back as an error string."""
    html_text, err = _render(url, timeout=timeout)
    if err:
        return [], f"render failed ({err})"
    if not (html_text or "").strip():
        return [], "render returned empty html"
    return parse_bonds(html_text, url)


def poll_bonds(path=RATES_PATH, url=BONDS_URL):
    """Overlay the investing.com India bond curve onto data/rates.json's
    `market_trends.bonds` block (source, board_url, as_of, benchmark_tenor, curve[]). Writes
    ONLY on a sane parse (10Y benchmark present + all yields in-bounds); on any failure the
    committed block is preserved untouched. Never raises. Returns a status string."""
    try:
        curve, err = fetch_bonds(url)
    except Exception as ex:                                 # defensive: this must never raise
        return f"bond scrape errored ({type(ex).__name__}: {ex}) — keeping committed snapshot"
    if err:
        return f"bond scrape failed ({err}) — keeping committed snapshot"
    if not _is_complete(curve):
        return "bond parse incomplete/out-of-range — keeping committed snapshot"

    # Read the prior snapshot strictly from the local FILE so we merge onto the committed JSON.
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    mkt = snap.get("market_trends")
    if not isinstance(mkt, dict):
        mkt = {}
        snap["market_trends"] = mkt

    bench = benchmark(curve)
    mkt["bonds"] = {
        "source": "investing.com",
        "board_url": BONDS_BOARD_URL,
        "as_of": datetime.now(IST).isoformat(timespec="seconds"),
        "benchmark_tenor": bench.get("tenor") if bench else None,
        "curve": curve,
    }
    rates.save_rates(snap, path)
    return (f"bonds updated ({len(curve)} tenors from investing.com — "
            f"10Y≈{bench.get('yield') if bench else '?'}%)")


if __name__ == "__main__":
    print(poll_bonds())
