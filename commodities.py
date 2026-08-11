#!/usr/bin/env python3
"""commodities.py — free commodity-price snapshot for the dashboard (load + best-effort scrape).

The Streamlit app shows a Commodities strip (Brent, Gold, Silver, Copper, Aluminium,
Zinc, Steel, Iron Ore, Coffee, Containerized Freight) with each commodity's **% change
vs the previous close** and a **direct chart link**, all read from a single committed JSON file —
`data/commodities.json` — in the same in-repo, no-database spirit as rates.py / history.

Where the data comes from (all FREE, no paid key) — TRADING ECONOMICS primary, YAHOO fallback,
INVESTING.COM for Coffee:
  • PRIMARY — **Trading Economics' server-rendered commodities table**
    (`tradingeconomics.com/commodities`). For a logged-out visitor the price, net change and
    **% change vs previous close** are rendered straight into the row markup
    (`tr[data-symbol]` → `td#p` price, `td#nch` net change, `td#pch` percent, `td#date` quote
    date) — no key, no JavaScript, no WebSocket (the live socket is login-gated and irrelevant
    to a 30-min poll). It covers every TE-sourced tile INCLUDING Zinc and the Containerized
    Freight Index, and gives a broker-grade % change so we don't have to compute one. Verified
    to serve current (last-settled) values, not stale data.
  • FALLBACK — **Yahoo Finance's keyless chart endpoint**
    (`query1.finance.yahoo.com/v8/finance/chart/<sym>`). If TE is blocked / rate-limited / drops
    a symbol, we fall back to Yahoo for the 7 it covers, reading daily closes and reporting
    `(last − prev)/prev`. **Steel** is pinned to Yahoo on purpose: TE's steel (`JBP:COM`) is
    Chinese rebar in CNY/T, whereas Yahoo `HRC=F` is a USD HR-coil benchmark consistent with the
    rest of the strip. Zinc has no free Yahoo future and the Containerized Freight Index (the
    weekly Shanghai SCFI composite) has no Yahoo series at all, so both are TE-only (preserved
    if TE misses).
  • COFFEE — **investing.com's London (Robusta) coffee futures page**
    (`investing.com/commodities/london-coffee`, quoted in USD/tonne), replacing TE's US
    Arabica (`KC1:COM`, USc/lb). investing.com blocks bots and renders client-side, so the
    quote is fetched with the shared stealth-browser render (`common.stealth_render`, same as
    bonds.py) — history.yml installs that browser on every run, so Coffee rides the same
    30-min cadence as the rest; any failed render (Cloudflare block, or a host without
    scrapling, like this sandbox) keeps its last committed price (like Zinc/Freight on a TE
    miss). No TE/Yahoo fallback: their coffee is a different series AND unit.
  • CHART LINK — Trading Economics' public per-commodity page (one clean URL each); Coffee
    links to its investing.com page instead (the same page scraped).

Refresh model (mirrors rates.py): the committed JSON is the source of truth; `poll_commodities()`
(run by poll.py on the 30-min cron) rewrites it ONLY on a complete + in-bounds parse of the liquid
core, and any symbol both sources miss keeps its last committed price — so a blocked scrape can
never clobber good data. Written WITHOUT live market access here (datacenter IPs may be 403'd /
Cloudflare-challenged), so validate the parser from a host that can reach TE / Yahoo.
Never raises on load; returns None when there's no readable snapshot.
"""

import json
import os
from datetime import datetime

import requests

import common

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMODITIES_PATH = os.environ.get(
    "MARKETWIRE_COMMODITIES_FILE", os.path.join(_DATA_DIR, "commodities.json"))
# Trading Economics' server-rendered commodities table (primary source).
TE_URL = os.environ.get("MARKETWIRE_TE_COMMODITIES", "https://tradingeconomics.com/commodities")
# Public per-commodity chart pages (the "view chart" links).
TE_BASE = "https://tradingeconomics.com/commodity/"
# investing.com's London (Robusta) coffee futures page — Coffee's price source AND chart link.
INVESTING_COFFEE_URL = os.environ.get(
    "MARKETWIRE_INVESTING_COFFEE_URL", "https://www.investing.com/commodities/london-coffee")
# A browser-ish UA + headers — TE sits behind Cloudflare and Yahoo 403s the bare python UA.
UA = common.BROWSER_UA
_HTML_HEADERS = common.HTML_HEADERS
IST = common.IST

# The commodity universe. Each: stable key, display name, unit, category, the Trading Economics
# row symbol (`te`), the Yahoo fallback symbol (`yf`, None ⇒ no free future), the TE chart slug,
# and which source leads (`source`):
#   "te"        → Trading Economics first, Yahoo fallback (the default).
#   "yahoo"     → Yahoo only — used for Steel, since TE steel (JBP:COM) is Chinese rebar in CNY/T
#              while Yahoo HRC=F is a USD HR-coil benchmark; no TE fallback (wrong series/currency).
#   "investing" → investing.com only — used for Coffee (London Robusta futures, USD/T, from the
#              `investing` page URL, which doubles as the chart link). Browser-rendered via
#              common.stealth_render (history.yml installs the browser every run); preserved
#              from the last snapshot on any failed render; no TE/Yahoo
#              fallback (their coffee is US Arabica in USc/lb — a different series AND unit).
# Zinc and Containerized Freight are TE-only (no free Yahoo series): preserved from the last
# snapshot if TE misses. Freight is the weekly Shanghai (SCFI) composite, quoted in points —
# `cadence` marks it weekly (default daily), and its `te` symbol is a best guess written without
# live TE access (Bloomberg's SHSPSCFI, TE's usual convention): if wrong, the row still resolves
# via `fetch_te`'s slug fallback on the row's /commodity/containerized-freight-index link.
SPECS = [
    dict(key="brent",     name="Brent Crude", unit="USD/bbl", category="Energy",
         te="CO1:COM",      yf="BZ=F",  slug="brent-crude-oil", source="te"),
    dict(key="gold",      name="Gold",        unit="USD/oz",  category="Precious",
         te="XAUUSD:CUR",   yf="GC=F",  slug="gold",            source="te"),
    dict(key="silver",    name="Silver",      unit="USD/oz",  category="Precious",
         te="XAGUSD:CUR",   yf="SI=F",  slug="silver",          source="te"),
    dict(key="copper",    name="Copper",      unit="USD/lb",  category="Base Metals",
         te="HG1:COM",      yf="HG=F",  slug="copper",          source="te"),
    dict(key="aluminium", name="Aluminium",   unit="USD/t",   category="Base Metals",
         te="LMAHDS03:COM", yf="ALI=F", slug="aluminum",        source="te"),
    dict(key="zinc",      name="Zinc",        unit="USD/t",   category="Base Metals",
         te="LMZSDS03:COM", yf=None,    slug="zinc",            source="te"),
    dict(key="steel",     name="Steel (HRC)", unit="USD/t",   category="Base Metals",
         te="JBP:COM",      yf="HRC=F", slug="steel",           source="yahoo"),
    dict(key="iron",      name="Iron Ore",    unit="USD/t",   category="Base Metals",
         te="SCO:COM",      yf="TIO=F", slug="iron-ore",        source="te"),
    dict(key="coffee",    name="Coffee (Robusta)", unit="USD/T", category="Softs",
         investing=INVESTING_COFFEE_URL,            source="investing"),
    dict(key="freight",   name="Containerized Freight", unit="Points", category="Freight",
         te="SHSPSCFI:IND", yf=None,    slug="containerized-freight-index", source="te",
         cadence="weekly"),
]
_SPECS_BY_KEY = {s["key"]: s for s in SPECS}

# Sanity bounds (broad) — a value outside its range means a mis-parse, not a real price.
_BOUNDS = {
    "brent": (5, 1000), "gold": (200, 50000), "silver": (1, 2000), "copper": (0.1, 100),
    "aluminium": (200, 20000), "zinc": (200, 20000), "steel": (50, 20000),
    "iron": (10, 5000), "coffee": (500, 20000), "freight": (100, 50000),
}
# The liquid core that must resolve (from EITHER source) in-bounds for a scrape to be trusted
# enough to overwrite the committed file (the rest are preserved if missing — see poll_commodities).
_CORE = ("brent", "gold", "silver", "copper")


# --------------------------------------------------------------------------- #
# Load (app + poller)
# --------------------------------------------------------------------------- #
def load_commodities(source=None, url_env="MARKETWIRE_COMMODITIES_URL",
                     default_path=COMMODITIES_PATH):
    """Read the committed commodities snapshot. `source` (or the env var) may be a raw
    http(s) URL or a file path; defaults to the local committed JSON. Never raises —
    returns the parsed dict, or None if there's nothing readable."""
    return common.load_json_snapshot(source, url_env, default_path,
                                     headers={"User-Agent": UA})


def chart_url(spec):
    """The public 'view chart' link for a commodity — the investing.com page for
    investing-sourced specs (Coffee), else the Trading Economics per-commodity page."""
    return spec.get("investing") or TE_BASE + spec["slug"]


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
_num = common.num           # shared numeric-cell parser
_te_date = common.te_asof   # 'Jun/26' -> ISO date; a time-of-day cell -> None (default)


def _in_bounds(key, v):
    if not isinstance(v, (int, float)):
        return False
    lo, hi = _BOUNDS.get(key, (None, None))
    return lo is None or (lo <= v <= hi)


# --------------------------------------------------------------------------- #
# Primary scrape — Trading Economics server-rendered table
# --------------------------------------------------------------------------- #
def fetch_te(url=TE_URL, timeout=25):
    """Scrape TE's commodities table into {our_key: {price, prev_close, change_pct, currency,
    as_of}}. Returns (quotes, error). Best effort: a Cloudflare block / markup change yields an
    error (or an empty parse) and the caller falls back to Yahoo / preserves prior values.
    Rows are matched by `data-symbol`, falling back to the row's /commodity/<slug> name link
    (the slugs are verified public URLs — our chart links — unlike some symbol guesses)."""
    want = {s["te"]: s["key"] for s in SPECS if s.get("te")}
    slugs = {s["slug"]: s["key"] for s in SPECS if s.get("te")}
    return common.fetch_te_table(url, want, timeout=timeout, headers=_HTML_HEADERS,
                                 currency="USD", want_slug=slugs)


# --------------------------------------------------------------------------- #
# Fallback scrape — Yahoo Finance chart endpoint
# --------------------------------------------------------------------------- #
fetch_one = common.yahoo_chart_quote    # one symbol's last/prev close, % change, as-of


def fetch_yahoo(specs, timeout=20):
    """Fetch the given specs from Yahoo into {key: {...}} (only sane, in-bounds parses). Returns
    (quotes, errors). Used for fallback, so we only hit Yahoo for the symbols that actually need it."""
    quotes, errors = {}, {}
    targets = [s for s in specs if s.get("yf")]
    if not targets:
        return quotes, errors
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA})
        for spec in targets:
            q, err = fetch_one(spec["yf"], timeout=timeout, session=s)
            if err:
                errors[spec["key"]] = err
            elif not _in_bounds(spec["key"], q.get("price")):
                errors[spec["key"]] = f"price {q.get('price')} out of bounds"
            else:
                quotes[spec["key"]] = q
    return quotes, errors


# --------------------------------------------------------------------------- #
# Coffee scrape — investing.com's London (Robusta) page, browser-rendered
# --------------------------------------------------------------------------- #
def parse_investing_quote(html_text):
    """Parse an investing.com single-instrument page (the London coffee futures page) into
    {price, prev_close, change_pct, currency, as_of}. Returns (quote, error).

    Reads the price header by `data-test` attribute (`instrument-price-last` /
    `instrument-price-change-percent` / the key-stats `prevClose`), with the legacy
    `#last_last`/`#last_pcp` ids as fallbacks; prev close is derived from the % change when
    the key-stats block is missing. Written WITHOUT live investing.com access (this sandbox
    can't reach it) — MARKETWIRE_COFFEE_DUMP captures the rendered HTML in CI for tuning."""
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:                                 # bs4 not installed
        return None, f"BeautifulSoup unavailable: {ex}"
    soup = BeautifulSoup(html_text, "html.parser")

    def probe(*selectors):
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(" ", strip=True)
        return None

    price = _num(probe('[data-test="instrument-price-last"]', "#last_last"))
    if price is None:
        return None, "no price parsed (markup changed or blocked)"
    pct = _num(probe('[data-test="instrument-price-change-percent"]', "#last_pcp"))
    prev = _num(probe('[data-test="prevClose"]'))
    if prev is None and pct not in (None, -100):
        prev = price / (1 + pct / 100.0)
    return {
        "price": round(price, 4),
        "prev_close": round(prev, 4) if prev is not None else None,
        "change_pct": round(pct, 2) if pct is not None else None,
        "currency": "USD",              # London Robusta futures are quoted in USD/tonne
        "as_of": datetime.now(IST).strftime("%Y-%m-%d"),
    }, None


def fetch_investing(url=None, timeout=None):
    """Render an investing.com commodity page (it blocks bots — the shared stealth browser,
    see common.stealth_render) and parse its price header. Returns (quote, error). Needs the
    Scrapling browser (history.yml installs it on every run); on a host without it the render
    fails fast and the caller preserves the committed price. Never
    raises for the expected failure modes (render blocked, scrapling missing, markup change)."""
    url = url or INVESTING_COFFEE_URL
    dump = os.environ.get("MARKETWIRE_COFFEE_DUMP", "").strip() or None
    html_text, err = common.stealth_render(url, timeout=timeout, dump_path=dump)
    if err:
        return None, f"render failed ({err})"
    return parse_investing_quote(html_text)


# --------------------------------------------------------------------------- #
# Resolve (TE primary + Yahoo fallback) and write — guarded so it can't clobber the file
# --------------------------------------------------------------------------- #
def _resolve(spec, te_quotes, yf_quotes, inv_quotes):
    """Pick a commodity's quote per its source preference. Returns (quote, source_label) or
    (None, None). 'te'-source tries TE then Yahoo; 'yahoo'-source (Steel) uses Yahoo only —
    no TE fallback, since TE steel is a different series/currency; 'investing'-source
    (Coffee) uses investing.com only — TE/Yahoo coffee is US Arabica in USc/lb, a different
    series AND unit."""
    if spec["source"] == "te":
        order = [("Trading Economics", spec.get("te"), te_quotes),
                 ("Yahoo Finance", spec.get("yf"), yf_quotes)]
    elif spec["source"] == "investing":
        slug = (spec.get("investing") or "").rstrip("/").rsplit("/", 1)[-1]
        order = [("investing.com", slug, inv_quotes)]
    else:
        order = [("Yahoo Finance", spec.get("yf"), yf_quotes)]
    for label, sym, quotes in order:
        if not sym:
            continue
        q = quotes.get(spec["key"])
        if q and _in_bounds(spec["key"], q.get("price")):
            return q, f"{label} · {sym}"
    return None, None


def _is_complete(resolved):
    """True only if the liquid core (Brent/Gold/Silver/Copper) all resolved + in-bounds — the
    gate that keeps a partial/garbage scrape from overwriting the committed snapshot."""
    return all(isinstance((resolved.get(k) or {}).get("price"), (int, float)) for k in _CORE)


def _entry(spec, quote=None, source=None, prior=None):
    """Build one commodity record: static config from `spec`, price fields from a fresh `quote`
    (with its `source` label) if present, else preserved from the `prior` committed record."""
    e = {
        "key": spec["key"], "name": spec["name"], "unit": spec["unit"],
        "category": spec["category"], "cadence": spec.get("cadence", "daily"),
        "chart_url": chart_url(spec),
        "source": source or (prior or {}).get("source"),
        "price": None, "prev_close": None, "change_pct": None, "currency": None, "as_of": None,
    }
    src = quote or prior or {}
    for k in ("price", "prev_close", "change_pct", "currency", "as_of"):
        if src.get(k) is not None:
            e[k] = src[k]
    if quote is None and (prior or {}).get("price") is not None:
        e["stale"] = True                            # preserved, not freshly fetched
    return e


def save_commodities(snapshot, path=COMMODITIES_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")


def poll_commodities(path=COMMODITIES_PATH):
    """Refresh the snapshot: scrape Trading Economics, fall back to Yahoo only for the symbols TE
    didn't supply (plus Steel, which is Yahoo by design), render investing.com for Coffee (only
    possible on runs with the Scrapling browser), resolve each commodity, and rewrite the
    file — but ONLY when the liquid core resolved sanely. Any symbol its source(s) miss keeps its
    last committed price (so Zinc, Coffee between browser runs, and transient failures are
    preserved). Never raises."""
    te_quotes, te_err = fetch_te()
    # Only hit Yahoo where it's actually needed: Steel (yahoo-source) + any te-source symbol TE
    # didn't supply in-bounds. When TE parses fully, that's just the one Steel request.
    need_yf = []
    for spec in SPECS:
        if spec["source"] == "yahoo":
            need_yf.append(spec)
        elif spec["source"] == "te":
            q = te_quotes.get(spec["key"])
            if not (q and _in_bounds(spec["key"], q.get("price"))) and spec.get("yf"):
                need_yf.append(spec)
    yf_quotes, yf_err = fetch_yahoo(need_yf)

    # investing.com-sourced specs (Coffee) need the stealth browser (history.yml installs
    # it on every run); on a blocked render or a host without scrapling the fetch fails
    # fast and the last committed price is preserved (stale-marked), same as a TE miss
    # on Zinc/Freight.
    inv_quotes, inv_err = {}, {}
    for spec in SPECS:
        if spec["source"] != "investing":
            continue
        q, err = fetch_investing(spec["investing"])
        if err:
            inv_err[spec["key"]] = err
        elif _in_bounds(spec["key"], q.get("price")):
            inv_quotes[spec["key"]] = q
        else:
            inv_err[spec["key"]] = f"price {q.get('price')} out of bounds"

    resolved = {}                                    # key -> (quote, source_label)
    for spec in SPECS:
        q, src = _resolve(spec, te_quotes, yf_quotes, inv_quotes)
        if q:
            resolved[spec["key"]] = (q, src)

    if not _is_complete({k: v[0] for k, v in resolved.items()}):
        miss = ", ".join(k for k in _CORE if k not in resolved) or "core incomplete"
        return (f"scrape incomplete (core missing: {miss}; te_err={te_err or '-'}; "
                f"yf_err={yf_err or '-'}) — keeping committed snapshot")

    # Read the prior snapshot strictly from the local FILE so we merge onto the committed JSON.
    try:
        with open(path, "r", encoding="utf-8") as f:
            prior = {c.get("key"): c for c in (json.load(f).get("commodities") or [])}
    except Exception:
        prior = {}

    rows, by_src = [], {}
    for spec in SPECS:
        q, src = resolved.get(spec["key"], (None, None))
        rows.append(_entry(spec, q, src, prior.get(spec["key"])))
        if q:
            tag = (src or "").split(" · ")[0]
            by_src[tag] = by_src.get(tag, 0) + 1
    snapshot = {
        "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "Trading Economics (primary) + Yahoo Finance (fallback) + investing.com (coffee)",
        "chart_links": "Trading Economics (tradingeconomics.com/commodity/…); coffee: investing.com",
        "commodities": rows,
    }
    save_commodities(snapshot, path)
    src_note = ", ".join(f"{n}×{v}" for v, n in sorted(((v, k) for k, v in by_src.items()), reverse=True))
    note = f"commodities updated ({len(resolved)} of {len(SPECS)} fresh — {src_note})"
    if inv_err:
        note += "; investing.com miss: " + "; ".join(f"{k}: {v}" for k, v in inv_err.items())
    return note


if __name__ == "__main__":
    print(poll_commodities())
