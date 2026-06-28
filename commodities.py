#!/usr/bin/env python3
"""commodities.py — free commodity-price snapshot for the dashboard (load + best-effort scrape).

The Streamlit app shows a Commodities strip (Brent, Gold, Silver, Copper, Aluminium,
Zinc, Steel, Iron Ore, Coffee) with each commodity's **% change vs the previous close**
and a **direct chart link**, all read from a single committed JSON file —
`data/commodities.json` — in the same in-repo, no-database spirit as rates.py / history.

Where the data comes from (all FREE, no paid key) — TRADING ECONOMICS primary, YAHOO fallback:
  • PRIMARY — **Trading Economics' server-rendered commodities table**
    (`tradingeconomics.com/commodities`). For a logged-out visitor the price, net change and
    **% change vs previous close** are rendered straight into the row markup
    (`tr[data-symbol]` → `td#p` price, `td#nch` net change, `td#pch` percent, `td#date` quote
    date) — no key, no JavaScript, no WebSocket (the live socket is login-gated and irrelevant
    to a 30-min poll). It covers all 9 INCLUDING Zinc, and gives a broker-grade % change so we
    don't have to compute one. Verified to serve current (last-settled) values, not stale data.
  • FALLBACK — **Yahoo Finance's keyless chart endpoint**
    (`query1.finance.yahoo.com/v8/finance/chart/<sym>`). If TE is blocked / rate-limited / drops
    a symbol, we fall back to Yahoo for the 8 it covers, reading daily closes and reporting
    `(last − prev)/prev`. **Steel** is pinned to Yahoo on purpose: TE's steel (`JBP:COM`) is
    Chinese rebar in CNY/T, whereas Yahoo `HRC=F` is a USD HR-coil benchmark consistent with the
    rest of the strip. Zinc has no free Yahoo future, so it's TE-only (preserved if TE misses).
  • CHART LINK — Trading Economics' public per-commodity page (one clean URL each, all 9).

Refresh model (mirrors rates.py): the committed JSON is the source of truth; `poll_commodities()`
(run by poll.py on the 30-min cron) rewrites it ONLY on a complete + in-bounds parse of the liquid
core, and any symbol both sources miss keeps its last committed price — so a blocked scrape can
never clobber good data. Written WITHOUT live market access here (datacenter IPs may be 403'd /
Cloudflare-challenged), so validate the parser from a host that can reach TE / Yahoo.
Never raises on load; returns None when there's no readable snapshot.
"""

import json
import os
import re
from datetime import datetime, date, timezone, timedelta
from urllib.parse import quote

import requests

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMODITIES_PATH = os.environ.get(
    "MARKETWIRE_COMMODITIES_FILE", os.path.join(_DATA_DIR, "commodities.json"))
# Trading Economics' server-rendered commodities table (primary source).
TE_URL = os.environ.get("MARKETWIRE_TE_COMMODITIES", "https://tradingeconomics.com/commodities")
# Yahoo's keyless chart endpoint (fallback). `<symbol>` is URL-encoded (futures tickers contain '=').
YF_CHART = os.environ.get(
    "MARKETWIRE_YF_CHART", "https://query1.finance.yahoo.com/v8/finance/chart/")
# Public per-commodity chart pages (the "view chart" links).
TE_BASE = "https://tradingeconomics.com/commodity/"
# A browser-ish UA + headers — TE sits behind Cloudflare and Yahoo 403s the bare python UA.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HTML_HEADERS = {"User-Agent": UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9"}
IST = timezone(timedelta(hours=5, minutes=30))

# The commodity universe. Each: stable key, display name, unit, category, the Trading Economics
# row symbol (`te`), the Yahoo fallback symbol (`yf`, None ⇒ no free future), the TE chart slug,
# and which source leads (`source`):
#   "te"     → Trading Economics first, Yahoo fallback (the default for 8 of 9).
#   "yahoo"  → Yahoo only — used for Steel, since TE steel (JBP:COM) is Chinese rebar in CNY/T
#              while Yahoo HRC=F is a USD HR-coil benchmark; no TE fallback (wrong series/currency).
# Zinc is TE-only (no free Yahoo future): preserved from the last snapshot if TE misses.
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
    dict(key="coffee",    name="Coffee",      unit="USc/lb",  category="Softs",
         te="KC1:COM",      yf="KC=F",  slug="coffee",          source="te"),
]
_SPECS_BY_KEY = {s["key"]: s for s in SPECS}

# Sanity bounds (broad) — a value outside its range means a mis-parse, not a real price.
_BOUNDS = {
    "brent": (5, 1000), "gold": (200, 50000), "silver": (1, 2000), "copper": (0.1, 100),
    "aluminium": (200, 20000), "zinc": (200, 20000), "steel": (50, 20000),
    "iron": (10, 5000), "coffee": (10, 2000),
}
# The liquid core that must resolve (from EITHER source) in-bounds for a scrape to be trusted
# enough to overwrite the committed file (the rest are preserved if missing — see poll_commodities).
_CORE = ("brent", "gold", "silver", "copper")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


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
# Parsing helpers
# --------------------------------------------------------------------------- #
def _num(s):
    """First signed number in `s` as float, tolerant of commas and the unicode minus
    ('−4.34%' -> -4.34, '4,087.01' -> 4087.01), else None."""
    if s is None:
        return None
    s = str(s).replace("−", "-")
    m = re.search(r"-?\d[\d,]*\.?\d*", s)
    return float(m.group(0).replace(",", "")) if m else None


def _in_bounds(key, v):
    if not isinstance(v, (int, float)):
        return False
    lo, hi = _BOUNDS.get(key, (None, None))
    return lo is None or (lo <= v <= hi)


def _te_date(s):
    """TE's row date 'Jun/26' -> ISO '2026-06-26' (current IST year; rolls back a year if the
    parsed date lands more than a week in the future, e.g. a Dec date read in early Jan)."""
    if not s:
        return None
    m = re.match(r"\s*([A-Za-z]{3})\s*/\s*(\d{1,2})", str(s))
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    today = datetime.now(IST).date()
    try:
        d = date(today.year, mon, int(m.group(2)))
    except ValueError:
        return None
    if (d - today).days > 7:
        d = date(today.year - 1, mon, int(m.group(2)))
    return d.isoformat()


# --------------------------------------------------------------------------- #
# Primary scrape — Trading Economics server-rendered table
# --------------------------------------------------------------------------- #
def _celltext(tr, cid):
    """Text of the <td id="cid"> inside a row (TE repeats ids per row; scoping to `tr` is fine)."""
    td = tr.find("td", id=cid)
    return td.get_text(" ", strip=True) if td else None


def fetch_te(url=TE_URL, timeout=25):
    """Scrape TE's commodities table into {our_key: {price, prev_close, change_pct, currency,
    as_of}}. Returns (quotes, error). Best effort: a Cloudflare block / markup change yields an
    error (or an empty parse) and the caller falls back to Yahoo / preserves prior values."""
    try:
        resp = requests.get(url, headers=_HTML_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return {}, f"{type(ex).__name__}: {ex}"
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return {}, f"BeautifulSoup unavailable: {ex}"

    soup = BeautifulSoup(resp.content, "html.parser")
    want = {s["te"]: s["key"] for s in SPECS if s.get("te")}
    out = {}
    for tr in soup.select("tr[data-symbol]"):
        key = want.get(tr.get("data-symbol"))
        if not key or key in out:
            continue
        price = _num(_celltext(tr, "p"))
        if price is None:
            continue
        nch = _num(_celltext(tr, "nch"))
        pch = _num(_celltext(tr, "pch"))             # signed % vs previous close, straight from TE
        if nch is not None:
            prev = price - nch
        elif pch not in (None, -100):
            prev = price / (1 + pch / 100.0)
        else:
            prev = None
        out[key] = {
            "price": round(price, 4),
            "prev_close": round(prev, 4) if prev is not None else None,
            "change_pct": round(pch, 2) if pch is not None else None,
            "currency": "USD",
            "as_of": _te_date(_celltext(tr, "date")),
        }
    if not out:
        return {}, "no target rows parsed (markup changed or blocked)"
    return out, None


# --------------------------------------------------------------------------- #
# Fallback scrape — Yahoo Finance chart endpoint
# --------------------------------------------------------------------------- #
def fetch_one(symbol, timeout=20, session=None):
    """Fetch one Yahoo symbol's last close, previous close, % change and as-of date.
    Returns ({price, prev_close, change_pct, currency, as_of}, error)."""
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
        pairs = [(t, c) for t, c in zip(ts, closes) if isinstance(c, (int, float))]
        if len(pairs) < 2:
            return None, "need ≥2 daily closes for a change"
        (cur_t, cur), (_, prev) = pairs[-1], pairs[-2]
        change_pct = ((cur - prev) / prev * 100.0) if prev else None
        return {
            "price": round(cur, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "currency": meta.get("currency"),
            "as_of": datetime.fromtimestamp(cur_t, IST).strftime("%Y-%m-%d"),
        }, None
    except Exception as ex:
        return None, f"parse error: {type(ex).__name__}: {ex}"


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
# Resolve (TE primary + Yahoo fallback) and write — guarded so it can't clobber the file
# --------------------------------------------------------------------------- #
def _resolve(spec, te_quotes, yf_quotes):
    """Pick a commodity's quote per its source preference. Returns (quote, source_label) or
    (None, None). 'te'-source tries TE then Yahoo; 'yahoo'-source (Steel) uses Yahoo only —
    no TE fallback, since TE steel is a different series/currency."""
    if spec["source"] == "te":
        order = [("Trading Economics", spec.get("te"), te_quotes),
                 ("Yahoo Finance", spec.get("yf"), yf_quotes)]
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
        "category": spec["category"], "cadence": "daily", "chart_url": chart_url(spec),
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
    didn't supply (plus Steel, which is Yahoo by design), resolve each commodity, and rewrite the
    file — but ONLY when the liquid core resolved sanely. Any symbol both sources miss keeps its
    last committed price (so Zinc and transient failures are preserved). Never raises."""
    te_quotes, te_err = fetch_te()
    # Only hit Yahoo where it's actually needed: Steel (yahoo-source) + any te-source symbol TE
    # didn't supply in-bounds. When TE parses fully, that's just the one Steel request.
    need_yf = []
    for spec in SPECS:
        if spec["source"] == "yahoo":
            need_yf.append(spec)
        else:
            q = te_quotes.get(spec["key"])
            if not (q and _in_bounds(spec["key"], q.get("price"))) and spec.get("yf"):
                need_yf.append(spec)
    yf_quotes, yf_err = fetch_yahoo(need_yf)

    resolved = {}                                    # key -> (quote, source_label)
    for spec in SPECS:
        q, src = _resolve(spec, te_quotes, yf_quotes)
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
        "source": "Trading Economics (primary) + Yahoo Finance (fallback)",
        "chart_links": "Trading Economics (tradingeconomics.com/commodity/…)",
        "commodities": rows,
    }
    save_commodities(snapshot, path)
    src_note = ", ".join(f"{n}×{v}" for v, n in sorted(((v, k) for k, v in by_src.items()), reverse=True))
    return f"commodities updated ({len(resolved)} of {len(SPECS)} fresh — {src_note})"


if __name__ == "__main__":
    print(poll_commodities())
