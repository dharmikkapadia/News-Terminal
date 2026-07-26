#!/usr/bin/env python3
"""te_stream.py — Trading Economics' news stream (tradingeconomics.com/stream).

Feeds TWO wire sources off the one stream:

    TE - India News   data/te_india_news.jsonl   fetch_india()
    TE - World News   data/te_world_news.jsonl   fetch_world()

Both return the shared (title, link, summary, published, ts) item shape used
across the app, plus an explicit `key` (see "Identity" below), so poll.py /
history.py / store.py handle them exactly like the RBI and SEBI feeds.

Sources, tried in order
-----------------------
TE publishes no documented feed for the stream, and neither this sandbox nor a
GitHub runner can see the page (Cloudflare + datacenter-IP blocks, same as RBI
and investing.com), so this module is written the way bonds.py/econ_calendar.py
were: defensively, against every shape TE's site is known to serve, with the
first candidate that yields items winning.

Each feed has an ORDERED LIST of candidate URLs (comma-separated, overridable
via MARKETWIRE_TE_INDIA_STREAM_URL / MARKETWIRE_TE_WORLD_STREAM_URL). Each is
fetched once and the response is SNIFFED — JSON (the stream's own XHR
endpoint), RSS/Atom, or server-rendered HTML — then handed to the matching
parser. A candidate that errors or parses to nothing just moves to the next, so
a wrong guess costs a request, not the feed. India's LAST candidate is the plain
global stream filtered to India-tagged items, which works whatever TE does with
its country URLs.

Nothing here ever raises: every entry point returns (items, error), and an
empty result simply leaves the committed history untouched (poll.py merges).

Identity
--------
TE items carry no stable public id across those three shapes, so each item gets
an explicit `key` of `te:<sha1(link|title)>` — identical whichever candidate
served it, and stable as an item's rendered timestamp ages from "2 minutes ago"
to a date. The trade-off: an identical headline pointing at the identical page
on a later day merges into the first one (rare — TE headlines carry the numbers).

Timestamps
----------
TE renders recent items relatively ("14 minutes ago"), which is timezone-free
and exact, and older ones as dates, which are assumed IST like the rest of the
wire. history.dedupe() keeps the FIRST-seen ts, so an item polled within 30
minutes of publication keeps that accurate stamp forever. `published` is
rewritten to an absolute IST string once a ts resolves — never a stale
"2 hours ago" frozen into the history file.

Validate from a host that can reach tradingeconomics.com (this sandbox and CI
are blocked): set MARKETWIRE_TE_STREAM_DUMP to capture each candidate's raw
body for parser tuning (history.yml uploads them as the `te-stream-dump`
artifact on schedule runs), then:

    python te_stream.py
"""

import calendar
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

import common

IST = common.IST

# Candidate sources per feed, most-specific first — the first one that yields
# items wins. Comma-separated so a deploy can re-point either feed (or collapse
# it to a single known-good URL) via env, once the live markup is confirmed.
STREAM_URL = "https://tradingeconomics.com/stream"
_WORLD_DEFAULTS = (
    # The stream page's own XHR endpoint (JSON) — cheapest and most stable when served.
    "https://tradingeconomics.com/ws/stream.ashx?start=0&size=50",
    # The server-rendered stream page itself.
    STREAM_URL,
    # An RSS mirror, if TE serves one for the stream.
    "https://tradingeconomics.com/rss/news.aspx",
)
_INDIA_DEFAULTS = (
    "https://tradingeconomics.com/ws/stream.ashx?start=0&size=50&c=india",
    "https://tradingeconomics.com/stream?c=india",
    "https://tradingeconomics.com/india/news",
    # Last resort: the plain global stream, filtered to India-tagged items here.
    STREAM_URL,
)
WORLD_URLS = os.environ.get("MARKETWIRE_TE_WORLD_STREAM_URL", "").strip() or ",".join(_WORLD_DEFAULTS)
INDIA_URLS = os.environ.get("MARKETWIRE_TE_INDIA_STREAM_URL", "").strip() or ",".join(_INDIA_DEFAULTS)

_HTML_HEADERS = {**common.HTML_HEADERS, "Referer": "https://tradingeconomics.com/"}
# Prefix for the per-candidate body dumps (CI uploads them as an artifact).
_DUMP_PATH = os.environ.get("MARKETWIRE_TE_STREAM_DUMP", "").strip()

# --- markup probes ---------------------------------------------------------- #
# TE's stream markup can't be read from here, so each field is looked up through
# an ordered list of probes (specific -> generic). Anything class-name based uses
# a substring match, so `stream-item`, `te-stream-item` and `stream-item-content`
# all hit without pinning the exact class TE happens to ship.
_ITEM_SELECTORS = (
    "div.stream-item", "li.stream-item", "article.stream-item",
    "[class*='stream-item']", "[data-stream-id]", "[class*='te-stream']",
    "[itemtype*='NewsArticle']", "article",
)
_TITLE_SELECTORS = ("h1", "h2", "h3", "h4", "[class*='title']", "[class*='headline']", "a")
_BODY_SELECTORS = ("[class*='description']", "[class*='summary']", "[class*='snippet']",
                   "[class*='body']", "[class*='text']", "p")
_DATE_SELECTORS = ("time", "[datetime]", "[data-date]", "[class*='date']",
                   "[class*='ago']", "[class*='time']")
_COUNTRY_SELECTORS = ("[data-country]", "[class*='country']", "[class*='flag']", "img[alt]")

# Field names accepted from the JSON endpoint (matched case-insensitively).
_JSON_LIST_KEYS = ("items", "data", "stream", "results", "news", "rows", "articles")
_TITLE_KEYS = ("title", "headline", "header", "name")
_BODY_KEYS = ("description", "body", "text", "content", "summary", "snippet", "desc")
_URL_KEYS = ("url", "link", "href", "permalink", "pageurl", "symbolurl")
_DATE_KEYS = ("date", "datetime", "published", "publisheddate", "pubdate", "time", "timestamp")
_COUNTRY_KEYS = ("country", "countryname", "location")

_REL_RE = re.compile(
    r"\b(?:(\d+)|an?)\s*(sec(?:ond)?|min(?:ute)?|hour|hr|day|week|month|year)s?\s*(?:ago|old)\b", re.I)
_REL_UNITS = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hour": 3600, "hr": 3600,
              "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*(Z|[+-]\d{2}:?\d{2})?")
_DMY_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_MD_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s*/?\s*(\d{1,2})\b")
_HM_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b", re.I)
_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)")
# The India fallback when an item carries no country tag at all: a whole-word
# mention in the HEADLINE only (body text name-drops far too many countries).
_INDIA_RE = re.compile(r"\b(india|indian)\b", re.I)


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #
def _tz_offset(marker):
    """An ISO zone marker ('Z' / '+05:30') -> a fixed-offset tzinfo. No marker (or
    an unreadable one) -> IST, the wire's assumed zone for undated sources."""
    if not marker:
        return IST
    if marker.upper() == "Z":
        return timezone.utc
    try:
        digits = marker[1:].replace(":", "")
        mins = int(digits[:2]) * 60 + int(digits[2:4])
        return timezone(timedelta(minutes=mins if marker[0] == "+" else -mins))
    except (ValueError, IndexError):
        return IST


def _hm(text, base):
    """Apply an 'HH:MM [AM|PM]' found in `text` to the date of `base`, else `base`."""
    m = _HM_RE.search(text or "")
    if not m:
        return base
    h, mins, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    if h > 23 or mins > 59:
        return base
    return base.replace(hour=h, minute=mins, second=0, microsecond=0)


def parse_when(text, now=None):
    """A TE stream timestamp -> epoch seconds, or None.

    Handles the four shapes the stream can print: relative ("14 minutes ago",
    "an hour ago"), ISO/`datetime` attributes (honouring an explicit offset),
    absolute dates ("Jul 24, 2026", "24 July 2026", "Jul/24") and a bare clock
    time (today). Dates without a zone are read as IST, like the rest of the wire.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        v = float(text)
        return int(v / 1000.0 if abs(v) > 1e11 else v)         # epoch ms or s
    s = str(text).strip()
    if not s:
        return None
    now = now or datetime.now(IST)

    m = _DOTNET_DATE_RE.search(s)                              # /Date(1753500000000)/
    if m:
        return int(int(m.group(1)) / 1000)
    if re.fullmatch(r"-?\d{9,14}", s):                         # bare epoch
        v = float(s)
        return int(v / 1000.0 if abs(v) > 1e11 else v)

    low = s.lower()
    if "just now" in low or "moments ago" in low or low in ("now", "live"):
        return int(now.timestamp())

    m = _REL_RE.search(s)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        step = _REL_UNITS.get(m.group(2).lower())
        if step:
            return int((now - timedelta(seconds=n * step)).timestamp())

    m = _ISO_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        try:
            return int(datetime(y, mo, d, hh, mm, ss, tzinfo=_tz_offset(m.group(7))).timestamp())
        except ValueError:
            return None

    if "yesterday" in low:
        return int(_hm(s, (now - timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                            microsecond=0)).timestamp())
    if "today" in low:
        return int(_hm(s, now.replace(hour=0, minute=0, second=0, microsecond=0)).timestamp())

    for rx, order in ((_DMY_RE, "dmy"), (_MDY_RE, "mdy")):
        m = rx.search(s)
        if not m:
            continue
        if order == "dmy":
            day, mon_s, year = int(m.group(1)), m.group(2), int(m.group(3))
        else:
            mon_s, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        mon = common.MONTHS.get(mon_s[:3].lower())
        if not mon:
            continue
        try:
            return int(_hm(s, datetime(year, mon, day, tzinfo=IST)).timestamp())
        except ValueError:
            return None

    m = _MD_RE.search(s)                                        # 'Jul 24' / 'Jul/24'
    if m:
        mon = common.MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                dt = datetime(now.year, mon, int(m.group(2)), tzinfo=IST)
                if (dt - now).days > 7:                         # a Dec date read in early Jan
                    dt = dt.replace(year=now.year - 1)
                return int(_hm(s, dt).timestamp())
            except ValueError:
                return None

    if _HM_RE.search(s):                                        # bare clock time = today
        dt = _hm(s, now.replace(second=0, microsecond=0))
        if dt > now + timedelta(minutes=5):     # a late-evening stamp read after midnight
            dt -= timedelta(days=1)
        return int(dt.timestamp())
    return None


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
def _country_from_link(link):
    """TE stream items point at a country/indicator page ('/india/inflation-cpi'),
    so the first path segment is the most reliable country tag on the page."""
    try:
        parts = [p for p in urlparse(link or "").path.split("/") if p]
    except Exception:
        return ""
    if not parts:
        return ""
    head = parts[0].lower()
    if head in ("stream", "articles", "news", "rss", "ws", "calendar", "commodities", "currencies"):
        return ""
    return head.replace("-", " ")


def _mkitem(title, link, summary, when_text, country, base_url, ts=None):
    """Assemble one wire item (shared by all three parsers), or None if it has no
    headline. `key` is set explicitly — see the module docstring's Identity note."""
    title = " ".join((title or "").split())
    if not title:
        return None
    link = urljoin(base_url, (link or "").strip()) if link else ""
    if link.startswith(("javascript:", "mailto:")) or link.endswith("#"):
        link = ""
    summary = " ".join(common.strip_html(summary or "").split())
    if summary.lower().startswith(title.lower()):        # don't repeat the headline as the body
        summary = summary[len(title):].lstrip(" -–—:·|").strip()
    ts = ts if ts is not None else parse_when(when_text)
    published = (datetime.fromtimestamp(ts, IST).strftime("%d %b %Y %H:%M IST")
                 if ts else " ".join(str(when_text or "").split()))
    country = " ".join(str(country or "").split()) or _country_from_link(link)
    digest = hashlib.sha1(f"{link}|{title}".encode("utf-8")).hexdigest()[:16]
    return {
        "key": f"te:{digest}",
        "title": title,
        "link": link,
        "summary": summary,
        "published": published,
        "ts": ts,
        "country": country,
    }


def _is_india(item):
    """India-tagged: TE's own country tag / the item's country page, else a
    whole-word mention in the headline (the only honest fallback when a candidate
    source carries no country field at all)."""
    if "india" in (item.get("country") or "").lower():
        return True
    if "india" in _country_from_link(item.get("link")):
        return True
    return bool(_INDIA_RE.search(item.get("title") or ""))


# --------------------------------------------------------------------------- #
# parsers — HTML / JSON / RSS
# --------------------------------------------------------------------------- #
def _first_text(node, selectors, skip=None):
    """(element, text) for the first probe that finds non-empty text, else (None, '')."""
    for sel in selectors:
        for el in node.select(sel):
            # Never re-read the headline element, its ancestors or its descendants.
            if skip is not None and (el is skip or el in skip.parents or skip in el.parents):
                continue
            text = el.get_text(" ", strip=True)
            if text:
                return el, text
    return None, ""


def _node_when(node):
    """The item's timestamp text: a machine-readable attribute if there is one
    (datetime=/data-date=), else whatever the date-ish element prints."""
    for sel in ("time[datetime]", "[datetime]", "[data-date]"):
        el = node.select_one(sel)
        if el is not None:
            val = el.get("datetime") or el.get("data-date")
            if val:
                return val
    if node.get("data-date"):
        return node["data-date"]
    return _first_text(node, _DATE_SELECTORS)[1]


def _node_country(node):
    for sel in _COUNTRY_SELECTORS:
        el = node.select_one(sel)
        if el is None:
            continue
        val = el.get("data-country") or (el.get("alt") if el.name == "img" else None) \
            or el.get_text(" ", strip=True)
        if val and len(val) < 40:
            return val
    return node.get("data-country") or ""


def _node_item(node, base_url):
    """One stream-item element -> a wire item, or None if it isn't one."""
    title_el, title = _first_text(node, _TITLE_SELECTORS)
    if not title:
        return None
    link = ""
    if title_el is not None:
        a = title_el if title_el.name == "a" else (title_el.select_one("a[href]")
                                                   or title_el.find_parent("a", href=True))
        if a is not None and a.get("href"):
            link = a["href"]
    if not link:
        a = node.select_one("a[href]")
        link = (a["href"] if a is not None else "") or node.get("data-url") or ""
    _, summary = _first_text(node, _BODY_SELECTORS, skip=title_el)
    if not summary:                                   # no body element — take what's left
        whole = node.get_text(" ", strip=True)
        summary = whole[len(title):].strip() if whole.startswith(title) else ""
    return _mkitem(title, link, summary, _node_when(node), _node_country(node), base_url)


def parse_stream(html_text, base_url=STREAM_URL):
    """Parse TE's server-rendered stream page. Walks _ITEM_SELECTORS most-specific
    first and returns the first probe that yields items. Returns (items, error);
    never raises."""
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return [], f"BeautifulSoup unavailable: {ex}"
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for sel in _ITEM_SELECTORS:
            nodes = soup.select(sel)
            if not nodes:
                continue
            # A substring probe can match an item AND its wrapper; keep only the
            # innermost matches so a wrapper doesn't swallow the whole page as one item.
            ids = {id(n) for n in nodes}
            nodes = [n for n in nodes if not any(id(d) in ids for d in n.find_all(True))]
            items, seen = [], set()
            for n in nodes:
                it = _node_item(n, base_url)
                if it is None or it["key"] in seen:
                    continue
                seen.add(it["key"])
                items.append(it)
            if items:
                return items, None
        return [], "no stream items parsed (markup changed or blocked)"
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


def _pick(record, keys):
    """First non-empty value in `record` for any of `keys` (case-insensitive)."""
    lower = {str(k).lower(): v for k, v in record.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def parse_stream_json(payload, base_url=STREAM_URL):
    """Parse the stream's XHR payload: a bare list of records, or a dict wrapping
    one under items/data/stream/…. Field names are matched case-insensitively from
    a candidate set, so TE renaming a key doesn't break the feed. Returns
    (items, error); never raises."""
    try:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        records = payload
        if isinstance(payload, dict):
            lower = {str(k).lower(): v for k, v in payload.items()}
            records = next((lower[k] for k in _JSON_LIST_KEYS if isinstance(lower.get(k), list)), None)
            if records is None:                       # a dict of records, or one bare record
                nested = [v for v in payload.values() if isinstance(v, dict)]
                records = nested or [payload]
        if not isinstance(records, list) or not records:
            return [], "no stream records in JSON payload"
        items, seen = [], set()
        for rec in records:
            if not isinstance(rec, dict):
                continue
            it = _mkitem(_pick(rec, _TITLE_KEYS), _pick(rec, _URL_KEYS), _pick(rec, _BODY_KEYS),
                         _pick(rec, _DATE_KEYS), _pick(rec, _COUNTRY_KEYS), base_url)
            if it is None or it["key"] in seen:
                continue
            seen.add(it["key"])
            items.append(it)
        if not items:
            return [], "no usable records in JSON payload"
        return items, None
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


def parse_stream_rss(body, base_url=STREAM_URL):
    """Parse an RSS/Atom mirror of the stream (if TE serves one) into wire items.
    Returns (items, error); never raises."""
    try:
        import feedparser
    except Exception as ex:
        return [], f"feedparser unavailable: {ex}"
    try:
        parsed = feedparser.parse(body)
        if not parsed.entries:
            return [], "no entries in feed"
        items, seen = [], set()
        for e in parsed.entries:
            st_time = e.get("published_parsed") or e.get("updated_parsed")
            when = e.get("published") or e.get("updated") or ""
            tags = e.get("tags") or []
            it = _mkitem(e.get("title"), e.get("link"),
                         e.get("summary") or e.get("description") or "",
                         when, tags[0].get("term", "") if tags else "",
                         base_url, ts=calendar.timegm(st_time) if st_time else None)
            if it is None or it["key"] in seen:
                continue
            seen.add(it["key"])
            items.append(it)
        if not items:
            return [], "no usable entries in feed"
        return items, None
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _dump(body, url, n):
    """Write one candidate's raw body (real page or a Cloudflare block) for markup
    diagnosis. Best effort — a dump failure must never break a poll."""
    if not _DUMP_PATH:
        return
    stem, ext = os.path.splitext(_DUMP_PATH)
    try:
        with open(f"{stem}.{n}{ext or '.txt'}", "w", encoding="utf-8") as f:
            f.write(f"<!-- {url} -->\n{body}")
    except Exception:
        pass


def _parse_body(body, content_type, url):
    """Sniff a candidate's response and hand it to the matching parser, so an
    env-supplied URL works whatever shape TE serves it in."""
    head = (body or "").lstrip()[:400]
    ctype = (content_type or "").lower()
    if "json" in ctype or head.startswith(("[", "{")):
        return parse_stream_json(body, url)
    if "xml" in ctype or head.startswith("<?xml") or "<rss" in head.lower() or "<feed" in head.lower():
        return parse_stream_rss(body, url)
    return parse_stream(body, url)


def _fetch_one(url, timeout=25, n=1):
    """GET one candidate and parse it. Returns (items, error); never raises."""
    try:
        resp = requests.get(url, headers=_HTML_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"
    body = resp.text or ""
    _dump(body, url, n)
    return _parse_body(body, resp.headers.get("Content-Type", ""), url)


def _urls(spec):
    return [u.strip() for u in str(spec or "").split(",") if u.strip()]


def fetch_stream(urls=WORLD_URLS, timeout=25, keep=None):
    """Fetch the first candidate in `urls` (comma-separated or a list) that yields
    items, optionally filtered by `keep(item)`. Returns (items, error) — the error
    summarises every candidate tried when none produced anything. Never raises."""
    errors = []
    for n, url in enumerate(_urls(urls) if isinstance(urls, str) else list(urls), 1):
        items, err = _fetch_one(url, timeout=timeout, n=n)
        if err:
            errors.append(f"{url}: {err}")
            continue
        if keep is not None:
            items = [it for it in items if keep(it)]
        if items:
            items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
            return items, None
        errors.append(f"{url}: parsed 0 usable items")
    return [], "; ".join(errors) or "no stream URLs configured"


def fetch_world(urls=WORLD_URLS, timeout=25):
    """TE's whole news stream — every country, every category."""
    return fetch_stream(urls, timeout=timeout)


def fetch_india(urls=INDIA_URLS, timeout=25):
    """The India slice of TE's stream (see _is_india for how items are matched —
    it also makes the last, unfiltered global candidate usable)."""
    return fetch_stream(urls, timeout=timeout, keep=_is_india)


if __name__ == "__main__":
    for label, fn, spec in (("India", fetch_india, INDIA_URLS), ("World", fetch_world, WORLD_URLS)):
        print(f"\n=== TE {label} stream ===")
        print("candidates:", ", ".join(_urls(spec)))
        items, error = fn()
        if error:
            print("ERROR:", error)
            continue
        for it in items[:15]:
            print(f"  {(it['published'] or '(no date)'):>22}  [{it['country'] or '-'}] {it['title'][:80]}")
            if it["summary"]:
                print(f"  {'':22}  {it['summary'][:110]}")
        dated = sum(1 for i in items if i["ts"])
        print(f"\n{len(items)} items; {dated} with a parsed timestamp.")
