#!/usr/bin/env python3
"""commodities.py — free commodity-price snapshot for the dashboard (load + best-effort scrape).

The Streamlit app shows a Commodities strip (Brent, Gold, Silver, Copper, Aluminium,
Zinc, Steel, Iron Ore, Coffee) with each commodity's **% change vs the previous close**
and a **direct chart link**, all read from a single committed JSON file —
`data/commodities.json` — in the same in-repo, no-database spirit as rates.py / history.

Where the data comes from (all FREE, no paid key):
  • PRICE + % CHANGE — Yahoo Finance's keyless chart endpoint
    (`query1.finance.yahoo.com/v8/finance/chart/<symbol>`). It returns daily closes, so we
    take the last two and report `(last − prev) / prev` = the move vs the previous close.
    Yahoo covers 8 of the 9 as liquid-enough futures (see SPECS); the LME base metal
    **Zinc** has no free daily future, so it is sourced MONTHLY from the World Bank
    "Pink Sheet" (a manual/preserved value, like the rates manual snapshot) and tagged
    `cadence: "monthly"`.
  • CHART LINK — Trading Economics' public per-commodity page (one clean URL each, all 9),
    which also matches the app's Trading-Economics-inspired look.

Refresh model (mirrors rates.py exactly):
  1. MANUAL — the committed `data/commodities.json` is the source of truth; seed/edit it by
     hand (e.g. Zinc's monthly value from the World Bank Pink Sheet).
  2. AUTOMATED (best-effort) — `.github/workflows/commodities.yml` runs `python commodities.py`
     daily; `poll_commodities()` scrapes Yahoo and rewrites the file, but ONLY if the scrape
     is complete and sane (see `_is_complete`). A blocked/partial scrape, or a single symbol
     that fails, leaves the committed value untouched — automation can never clobber good data.

Like rates.py / rbi_archive.py this was written WITHOUT live market access (datacenter IPs may
be 403'd), so validate the parser from a host that can reach Yahoo Finance.
Never raises on load; returns None when there's no readable snapshot.
"""

import json
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMODITIES_PATH = os.environ.get(
    "MARKETWIRE_COMMODITIES_FILE", os.path.join(_DATA_DIR, "commodities.json"))
# Yahoo's keyless chart endpoint. `<symbol>` is URL-encoded (futures tickers contain '=').
YF_CHART = os.environ.get(
    "MARKETWIRE_YF_CHART", "https://query1.finance.yahoo.com/v8/finance/chart/")
# Public per-commodity chart pages (the "view chart" links).
TE_BASE = "https://tradingeconomics.com/commodity/"
# A browser-ish UA — Yahoo 403s the default python-requests agent.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IST = timezone(timedelta(hours=5, minutes=30))

# The commodity universe. Each: stable key, display name, unit, category, the Yahoo
# futures symbol (None ⇒ no free daily future — sourced monthly/manually), the Trading
# Economics chart slug, and the refresh cadence.
#   Yahoo symbols: BZ=F Brent · GC=F Gold · SI=F Silver · HG=F Copper (COMEX) ·
#   ALI=F Aluminium (COMEX) · HRC=F US-Midwest HR-Coil Steel · TIO=F 62% Fe Iron Ore ·
#   KC=F Arabica Coffee. Aluminium/Steel/Iron are thinner contracts and may lag some days
#   (the guarded write + last-known preserve handles that). Zinc → World Bank monthly.
SPECS = [
    dict(key="brent",    name="Brent Crude", unit="USD/bbl", category="Energy",
         symbol="BZ=F",  slug="brent-crude-oil", cadence="daily"),
    dict(key="gold",     name="Gold",        unit="USD/oz",  category="Precious",
         symbol="GC=F",  slug="gold",            cadence="daily"),
    dict(key="silver",   name="Silver",      unit="USD/oz",  category="Precious",
         symbol="SI=F",  slug="silver",          cadence="daily"),
    dict(key="copper",   name="Copper",      unit="USD/lb",  category="Base Metals",
         symbol="HG=F",  slug="copper",          cadence="daily"),
    dict(key="aluminium", name="Aluminium",  unit="USD/t",   category="Base Metals",
         symbol="ALI=F", slug="aluminum",        cadence="daily"),
    dict(key="zinc",     name="Zinc",        unit="USD/t",   category="Base Metals",
         symbol=None,    slug="zinc",            cadence="monthly"),
    dict(key="steel",    name="Steel (HRC)", unit="USD/t",   category="Base Metals",
         symbol="HRC=F", slug="steel",           cadence="daily"),
    dict(key="iron",     name="Iron Ore",    unit="USD/t",   category="Base Metals",
         symbol="TIO=F", slug="iron-ore",        cadence="daily"),
    dict(key="coffee",   name="Coffee",      unit="USc/lb",  category="Softs",
         symbol="KC=F",  slug="coffee",          cadence="daily"),
]
_SPECS_BY_KEY = {s["key"]: s for s in SPECS}

# Sanity bounds (broad) — a value outside its range means a mis-parse, not a real price.
# Used both to gate a write (_is_complete) and to reject a single absurd symbol.
_BOUNDS = {
    "brent": (5, 1000), "gold": (200, 50000), "silver": (1, 2000), "copper": (0.1, 100),
    "aluminium": (200, 20000), "zinc": (200, 20000), "steel": (50, 20000),
    "iron": (10, 5000), "coffee": (10, 2000),
}
# The liquid core that must be present + in-bounds for a scrape to be trusted enough to
# overwrite the committed file (the rest are preserved if missing — see poll_commodities).
_CORE = ("brent", "gold", "silver", "copper")


# --------------------------------------------------------------------------- #
# Load (app + poller)
# --------------------------------------------------------------------------- #
def load_commodities(source=None, url_env="MARKETWIRE_COMMODITIES_URL",
                     default_path=COMMODITIES_PATH):
    """Read the committed commodities snapshot. `source` (or the env var) may be a raw
    http(s) URL or a file path; defaults to the local committed JSON. Never raises —
    returns the parsed dict, or None if there's nothing readable."""
    source = source or os.environ.get(url_env, "").strip() or default_path
    try:
        if source.startswith(("http://", "https://")):
            resp = requests.get(source, headers={"User-Agent": UA}, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return json.loads(resp.text)
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def chart_url(spec):
    """The public 'view chart' link for a commodity (Trading Economics per-commodity page)."""
    return TE_BASE + spec["slug"]


# --------------------------------------------------------------------------- #
# Best-effort scrape (poller only) — guarded so it can't clobber the manual file
# --------------------------------------------------------------------------- #
def _in_bounds(key, v):
    if not isinstance(v, (int, float)):
        return False
    lo, hi = _BOUNDS.get(key, (None, None))
    return lo is None or (lo <= v <= hi)


def fetch_one(symbol, timeout=20, session=None):
    """Fetch one Yahoo symbol's last close, previous close, and as-of date.
    Returns ({price, prev_close, change_pct, currency, as_of}, error). Best effort: a 403 /
    rate-limit / markup change yields an error and the caller preserves the prior value."""
    url = YF_CHART + quote(symbol, safe="") + "?range=7d&interval=1d"
    try:
        get = (session or requests).get
        resp = get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    try:
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return None, "no result"
        res = res[0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        quote_blk = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote_blk.get("close") or []
        # Pair each daily close with its timestamp, dropping nulls (holidays/half-days).
        pairs = [(t, c) for t, c in zip(ts, closes) if isinstance(c, (int, float))]
        if len(pairs) < 2:
            return None, "need ≥2 daily closes for a change"
        (cur_t, cur), (_, prev) = pairs[-1], pairs[-2]
        change_pct = ((cur - prev) / prev * 100.0) if prev else None
        as_of = datetime.fromtimestamp(cur_t, IST).strftime("%Y-%m-%d")
        return {
            "price": round(cur, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "currency": meta.get("currency"),
            "as_of": as_of,
        }, None
    except Exception as ex:
        return None, f"parse error: {type(ex).__name__}: {ex}"


def fetch_commodities(timeout=20):
    """Scrape every Yahoo-covered commodity into {key: {price, prev_close, change_pct, …}}.
    Returns (quotes, errors): `quotes` maps only the symbols that parsed sanely; `errors`
    maps key→reason for the rest. Zinc (no Yahoo symbol) is never fetched here."""
    quotes, errors = {}, {}
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA})
        for spec in SPECS:
            if not spec.get("symbol"):
                continue
            q, err = fetch_one(spec["symbol"], timeout=timeout, session=s)
            if err:
                errors[spec["key"]] = err
            elif not _in_bounds(spec["key"], q.get("price")):
                errors[spec["key"]] = f"price {q.get('price')} out of bounds"
            else:
                quotes[spec["key"]] = q
    return quotes, errors


def _is_complete(quotes):
    """True only if the liquid core (Brent/Gold/Silver/Copper) all parsed + in-bounds — the
    gate that keeps a partial/garbage scrape from overwriting the committed snapshot."""
    return all(isinstance((quotes.get(k) or {}).get("price"), (int, float)) for k in _CORE)


def _entry(spec, quote=None, prior=None):
    """Build one commodity record: static config from `spec`, price fields from a fresh
    `quote` if present, else preserved from the `prior` committed record, else null."""
    e = {
        "key": spec["key"], "name": spec["name"], "unit": spec["unit"],
        "category": spec["category"], "cadence": spec["cadence"],
        "chart_url": chart_url(spec),
        "source": (f"Yahoo Finance · {spec['symbol']}" if spec.get("symbol")
                   else "World Bank Pink Sheet (monthly)"),
        "price": None, "prev_close": None, "change_pct": None,
        "currency": None, "as_of": None,
    }
    src = quote or prior or {}
    for k in ("price", "prev_close", "change_pct", "currency", "as_of"):
        if src.get(k) is not None:
            e[k] = src[k]
    # A preserved (not freshly-scraped) value is flagged so the UI can mark it stale.
    if quote is None and (prior or {}).get("price") is not None:
        e["stale"] = True
    return e


def save_commodities(snapshot, path=COMMODITIES_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")


def poll_commodities(path=COMMODITIES_PATH):
    """Try to refresh the snapshot from Yahoo. Rebuilds every record from SPECS (so static
    config stays correct) and fills prices from the scrape; any symbol the scrape missed
    keeps its last committed value (so Zinc's monthly figure and a transient single-symbol
    failure are both preserved). Writes ONLY when the liquid core parsed sanely. Returns a
    status string; never raises."""
    quotes, errors = fetch_commodities()
    if not _is_complete(quotes):
        miss = ", ".join(f"{k}: {errors.get(k, 'missing')}" for k in _CORE
                         if k not in quotes) or "core incomplete"
        return f"scrape incomplete ({miss}) — keeping committed snapshot"
    # Read the prior snapshot strictly from the local FILE (not load_commodities, which would
    # honour MARKETWIRE_COMMODITIES_URL) so we always merge onto the committed JSON.
    try:
        with open(path, "r", encoding="utf-8") as f:
            prior = {c.get("key"): c for c in (json.load(f).get("commodities") or [])}
    except Exception:
        prior = {}

    rows = [_entry(spec, quotes.get(spec["key"]), prior.get(spec["key"])) for spec in SPECS]
    snapshot = {
        "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (query1.finance.yahoo.com) + World Bank Pink Sheet (Zinc)",
        "chart_links": "Trading Economics (tradingeconomics.com/commodity/…)",
        "commodities": rows,
    }
    save_commodities(snapshot, path)
    fresh = sum(1 for k in quotes)
    note = f" — preserved: {', '.join(sorted(errors))}" if errors else ""
    return f"commodities updated ({fresh} fresh of {len(SPECS)}){note}"


if __name__ == "__main__":
    print(poll_commodities())
