"""feed.py — pure RSS fetch/parse for RBI press releases (no Streamlit).

Shared by the Streamlit app and the poller (poll.py) so both parse the feed the
same way. Returns (items, error); never raises.
"""

import calendar
import html
import re
from datetime import datetime, timezone, timedelta

import requests
import feedparser

RBI_FEED = "https://rbi.org.in/pressreleases_rss.xml"
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))
_TAG_RE = re.compile(r"<[^>]+>")
# RBI's pubDate omits a timezone (e.g. "Thu, 25 Jun 2026 22:45:00"), which
# feedparser can't parse — so it leaves published_parsed=None. Parse it ourselves
# (assuming IST) so items get a real timestamp instead of falling back to midnight.
_DT_FORMATS = ("%a, %d %b %Y %H:%M:%S", "%d %b %Y %H:%M:%S", "%a, %d %b %Y", "%d %b %Y")


def strip_html(s):
    """Turn an RSS HTML summary into plain, single-spaced text."""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _parse_dt(s):
    """Best-effort epoch (IST-assumed) from a feed date string feedparser couldn't."""
    s = (s or "").strip()
    for fmt in _DT_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=IST).timestamp())
        except ValueError:
            continue
    return None


def fetch_rss(url, timeout=20):
    """Fetch + parse one RSS/Atom feed. Returns (items, error)."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        return [], f"not a readable feed ({getattr(parsed, 'bozo_exception', 'unknown')})"

    items = []
    for e in parsed.entries:
        st_time = e.get("published_parsed") or e.get("updated_parsed")
        published = e.get("published") or e.get("updated") or ""
        ts = calendar.timegm(st_time) if st_time else _parse_dt(published)
        items.append({
            "title": (e.get("title") or "(untitled)").strip(),
            "link": e.get("link") or "",
            "summary": strip_html(e.get("summary") or e.get("description") or ""),
            "published": published,
            "ts": ts,
        })
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items, None
