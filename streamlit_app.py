"""MarketWire — a minimal RBI press-release reader.

A single-file Streamlit app that fetches the RBI Press Releases RSS feed
server-side (browsers can't read most RSS directly because of CORS) and shows
it newest-first. No database, no scheduler — just the wire.

Run locally:   streamlit run streamlit_app.py
Deploy:        Streamlit Community Cloud, main file = streamlit_app.py
"""

import calendar
import html
import re
from datetime import datetime, timezone, timedelta

import feedparser
import requests
import streamlit as st

RBI_FEED = "https://rbi.org.in/pressreleases_rss.xml"
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """Turn an RSS HTML summary into plain, single-spaced text."""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_rbi():
    """Fetch + parse the RBI feed. Returns (items, error). Cached for 5 min."""
    try:
        resp = requests.get(RBI_FEED, headers={"User-Agent": UA}, timeout=20)
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


st.set_page_config(page_title="MarketWire · RBI", page_icon="◢", layout="wide")

head, refresh = st.columns([6, 1])
head.markdown("## ◢ MarketWire — RBI Press Releases")
if refresh.button("⟳ Refresh", use_container_width=True):
    fetch_rbi.clear()
    st.rerun()

items, error = fetch_rbi()

if error:
    st.error(f"Couldn't fetch the RBI feed: {error}")
    st.caption(
        "Government sites sometimes block datacenter IPs (e.g. Streamlit Cloud). "
        "Try Refresh, or run it locally where the feed is reachable."
    )
    st.stop()

if not items:
    st.info("The feed returned no items.")
    st.stop()

q = st.text_input("Filter", placeholder="filter by keyword…", label_visibility="collapsed")
shown = [it for it in items if q.lower() in (it["title"] + " " + it["summary"]).lower()] if q.strip() else items
st.caption(f"{len(shown)} of {len(items)} press releases · newest first")

for it in shown:
    when = (
        datetime.fromtimestamp(it["ts"], IST).strftime("%d %b %Y · %H:%M IST")
        if it["ts"] else (it["published"] or "—")
    )
    st.markdown(
        f"**{it['title']}**  \n"
        f"<span style='color:#8A93A6;font-size:12px'>{when}</span>",
        unsafe_allow_html=True,
    )
    with st.expander("details"):
        st.write(it["summary"] or "(no summary in feed)")
        if it["link"].startswith("http"):
            st.markdown(f"[Open original ↗]({it['link']})")
    st.divider()
