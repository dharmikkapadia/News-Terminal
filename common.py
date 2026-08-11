"""common.py — shared constants + helpers for MarketWire's modules.

One home for the plumbing that used to be copy-pasted per module (and had already
started to drift — three slightly different `_num`s): the IST timezone, the two
User-Agent families, number/date/identity parsing, the GitHub-Actions annotation
printer, the URL-or-path JSON snapshot loader, the Trading-Economics
`tr[data-symbol]` table scraper (shared by commodities + FX), the Yahoo Finance
chart-endpoint fetcher, the Scrapling stealth-browser render for bot-blocked
investing.com pages (shared by the bond curve + the London coffee quote), and the
~10Y bond-benchmark picker.

Deliberately imports NO project modules (so it can never create an import cycle);
bs4 and scrapling are imported lazily inside their users, matching the callers'
old pattern.
Each consuming module keeps its public names as thin aliases (e.g.
`rates._num = common.num`), so call sites and the test suite are unchanged.
"""

import html as _html
import json
import os
import re
from datetime import datetime, date, timezone, timedelta
from urllib.parse import quote

import requests

IST = timezone(timedelta(hours=5, minutes=30))

# The polite feed-reader UA (RBI/SEBI/history fetches).
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
# A browser-ish UA + headers — TE sits behind Cloudflare and Yahoo 403s a bare
# python UA. rates.py adds its own Referer on top of HTML_HEADERS.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTML_HEADERS = {"User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}

# Yahoo's keyless chart endpoint. `<symbol>` is URL-encoded (futures/FX tickers
# contain '='). Shared by the commodities fallback and the FX fallback.
YF_CHART = os.environ.get(
    "MARKETWIRE_YF_CHART", "https://query1.finance.yahoo.com/v8/finance/chart/")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_ID_RE_PRID = re.compile(r"\bprid=(\d+)", re.I)
_ID_RE_ID = re.compile(r"\bid=(\d+)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def num(s):
    """First signed number in `s` as float, tolerant of commas and the unicode minus
    ('−4.34%' -> -4.34, '4,087.01' -> 4087.01, '₹ 13,600' -> 13600.0), else None."""
    if s is None:
        return None
    s = str(s).replace("−", "-")
    m = re.search(r"-?\d[\d,]*\.?\d*", s)
    return float(m.group(0).replace(",", "")) if m else None


def strip_html(s):
    """Turn an HTML summary (RSS description, a scraped blurb) into plain,
    single-spaced text."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", s))).strip()


def link_id(link):
    """RBI press-release `prid` or notification `Id` from a detail link, else None."""
    m = _ID_RE_PRID.search(link or "") or _ID_RE_ID.search(link or "")
    return m.group(1) if m else None


def item_key(item):
    """Stable identity for a wire item: an explicit `key` the source set (Trading
    Economics stream items — their links repeat across headlines, so te_stream.py
    derives its own), else the RBI `prid`/`Id` from the link, else the link itself
    (SEBI links carry no id param but are unique + permanent). The feeds are stored
    separately, so a prid and an Id sharing a number never clash."""
    explicit = item.get("key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    link = item.get("link", "") or ""
    return link_id(link) or link


def annotate(level, title, msg):
    """Emit a GitHub Actions annotation (read from stdout) so problems show on the
    run summary, not just buried in the log. Harmless when run locally (just prints)."""
    msg = str(msg).replace("\n", " ").replace("\r", " ")
    print(f"::{level} title={title}::{msg}")


def load_json_snapshot(source, url_env, default_path, timeout=15, headers=None):
    """Read a committed JSON snapshot (rates/commodities). `source` (or the `url_env`
    env var) may be a raw http(s) URL or a file path; defaults to the local committed
    file. Never raises — returns the parsed dict, or None if there's nothing readable."""
    source = source or os.environ.get(url_env, "").strip() or default_path
    try:
        if source.startswith(("http://", "https://")):
            resp = requests.get(source, headers=headers or {"User-Agent": UA}, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return json.loads(resp.text)
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def te_asof(s, time_means_today=False):
    """A Trading-Economics date cell -> ISO date. 'Jun/26' maps to the current IST
    year, rolling back a year if that lands more than a week in the future (a Dec
    date read in early Jan). TE's most-liquid rows show a TIME ('12:09') instead:
    with time_means_today that maps to today (the FX behavior), else None (the
    commodities behavior — both preserved exactly from the pre-common copies)."""
    if not s:
        return None
    s = str(s).strip()
    today = datetime.now(IST).date()
    if time_means_today and re.match(r"^\d{1,2}:\d{2}\b", s):
        return today.isoformat()
    m = re.match(r"\s*([A-Za-z]{3})\s*/\s*(\d{1,2})", s)
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        d = date(today.year, mon, int(m.group(2)))
    except ValueError:
        return None
    if (d - today).days > 7:
        d = date(today.year - 1, mon, int(m.group(2)))
    return d.isoformat()


def _te_celltext(tr, cid):
    """Text of the <td id="cid"> inside a TE row (ids repeat per row; scoping to
    `tr` is fine)."""
    td = tr.find("td", id=cid)
    return td.get_text(" ", strip=True) if td else None


def fetch_te_table(url, want, timeout=25, headers=None, currency=None,
                   time_means_today=False, what="target", want_slug=None):
    """Scrape a Trading-Economics server-rendered quotes table (commodities or the
    INR currencies board) into {our_key: {price, prev_close, change_pct[, currency],
    as_of}}. `want` maps TE row `data-symbol` -> our key. Returns (quotes, error).

    Each `tr[data-symbol]` row carries `td#p` price, `td#nch` net change, `td#pch`
    TE's own signed % vs previous close, and `td#date`. Best effort: a Cloudflare
    block / markup change yields an error (or an empty parse) and the caller falls
    back to Yahoo / preserves prior values.

    `want_slug` (optional) maps a TE page slug (the last path segment of the row's
    name link, e.g. 'containerized-freight-index') -> our key, tried only when the
    row's `data-symbol` isn't in `want`. Row links are stable public URLs (they're
    our chart links), so this rescues symbols whose `data-symbol` guess is wrong —
    some were written without live TE access."""
    try:
        resp = requests.get(url, headers=headers or HTML_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return {}, f"{type(ex).__name__}: {ex}"
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return {}, f"BeautifulSoup unavailable: {ex}"

    soup = BeautifulSoup(resp.content, "html.parser")
    out = {}
    for tr in soup.select("tr[data-symbol]"):
        key = want.get(tr.get("data-symbol"))
        if key is None and want_slug:
            a = tr.find("a", href=True)
            if a:
                slug = a["href"].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                key = want_slug.get(slug)
        if not key or key in out:
            continue
        price = num(_te_celltext(tr, "p"))
        if price is None:
            continue
        nch = num(_te_celltext(tr, "nch"))
        pch = num(_te_celltext(tr, "pch"))            # signed % vs prev close, straight from TE
        if nch is not None:
            prev = price - nch
        elif pch not in (None, -100):
            prev = price / (1 + pch / 100.0)
        else:
            prev = None
        q = {
            "price": round(price, 4),
            "prev_close": round(prev, 4) if prev is not None else None,
            "change_pct": round(pch, 2) if pch is not None else None,
            "as_of": te_asof(_te_celltext(tr, "date"), time_means_today=time_means_today),
        }
        if currency is not None:
            q["currency"] = currency
        out[key] = q
    if not out:
        return {}, f"no {what} rows parsed (markup changed or blocked)"
    return out, None


def yahoo_chart_quote(symbol, timeout=20, session=None):
    """One Yahoo symbol's last close, previous close, % change, currency and as-of
    date from the keyless chart endpoint. Returns ({price, prev_close, change_pct,
    currency, as_of}, error)."""
    url = YF_CHART + quote(symbol, safe="") + "?range=7d&interval=1d"
    try:
        get = (session or requests).get
        resp = get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
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
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
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


def env_flag(name, default=False):
    """Read a boolean-ish env var; empty/unset → `default`."""
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def stealth_render(url, timeout=None, dump_path=None, wait=4000):
    """Fetch a bot-blocked investing.com page in Scrapling's STEALTH browser and
    return (html, error). Shared by bonds.py (the yield-curve board) and
    commodities.py (the London coffee quote); scrapling is imported lazily, so a
    host without it just gets an error back (and the caller keeps its snapshot).

    investing.com hard-blocks bots from datacenter IPs (Cloudflare 403), so this is
    tuned to look like a real user and NOT hang on its never-idle ad/tracker traffic:
      • StealthyFetcher only — plain Chromium (DynamicFetcher) is pointless against
        Cloudflare;
      • solve_cloudflare + arrive via a Google click + block ads so the DOM settles;
      • network_idle=False — waiting for 'load'/network-idle just times out on
        investing.com (the original failure: `Page.goto ... waiting until "load"` →
        60s timeout);
      • non-headless by default (set MARKETWIRE_BONDS_HEADLESS=true to override) —
        run under a virtual display (xvfb) in CI; headless stealth Chrome is easier
        for Cloudflare to flag;
      • MARKETWIRE_SCRAPE_PROXY routes through a (residential) proxy — the RELIABLE
        fix from CI, since a GitHub-runner IP is usually 403'd however good the
        browser fingerprint is.
    `timeout` (ms) defaults from MARKETWIRE_RENDER_TIMEOUT_MS; `dump_path` (if set)
    receives whatever HTML came back — even a block page — so a CI artifact can carry
    it for markup diagnosis. A 403/429 (or empty) response comes back as an error so
    callers preserve their committed snapshots."""
    if timeout is None:
        timeout = int(os.environ.get("MARKETWIRE_RENDER_TIMEOUT_MS", "90000"))
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception as ex:                       # scrapling/browser not installed
        return None, f"scrapling unavailable: {type(ex).__name__}: {ex}"
    kw = dict(
        headless=env_flag("MARKETWIRE_BONDS_HEADLESS", default=False),
        solve_cloudflare=True,       # attempt the Cloudflare/Turnstile challenge
        google_search=True,          # arrive via a Google results click (organic-looking)
        network_idle=False,          # investing.com's trackers never idle → don't wait on it
        load_dom=True,
        block_ads=True,              # fewer ad/tracker requests → the page settles + parses
        wait=wait,                   # let the client-rendered content paint post-challenge
        timeout=timeout,
    )
    proxy = os.environ.get("MARKETWIRE_SCRAPE_PROXY", "").strip()
    if proxy:
        kw["proxy"] = proxy
    try:
        page = StealthyFetcher.fetch(url, **kw)
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    status = getattr(page, "status", None)
    body = getattr(page, "html_content", None) or ""
    if dump_path and body:                        # dump even a block page for diagnosis
        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception:
            pass
    if body.strip() and status not in (403, 429):
        return body, None
    return None, f"blocked/empty (status={status}, html={len(body)}B)"


def bond_benchmark(curve, target_years=10.0):
    """The bond in `curve` whose maturity (`years`) is closest to `target_years`
    (with a numeric yield), or None — the ~10Y benchmark for the G-Sec tile."""
    best = None
    for b in curve or []:
        yrs, y = b.get("years"), b.get("yield")
        if not isinstance(yrs, (int, float)) or not isinstance(y, (int, float)):
            continue
        d = abs(yrs - target_years)
        if best is None or d < best[0]:
            best = (d, b)
    return best[1] if best else None
