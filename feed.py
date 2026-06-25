"""feed.py — pure RSS fetch/parse for RBI press releases (no Streamlit).

Shared by the Streamlit app and the poller (poll.py) so both parse the feed the
same way. Returns (items, error); never raises.
"""

import calendar
import html
import re

import requests
import feedparser

RBI_FEED = "https://rbi.org.in/pressreleases_rss.xml"
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s):
    """Turn an RSS HTML summary into plain, single-spaced text."""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


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
        items.append({
            "title": (e.get("title") or "(untitled)").strip(),
            "link": e.get("link") or "",
            "summary": strip_html(e.get("summary") or e.get("description") or ""),
            "published": e.get("published") or e.get("updated") or "",
            "ts": calendar.timegm(st_time) if st_time else None,
        })
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items, None
