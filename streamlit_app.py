"""MarketWire — a minimal RBI press-release reader.

A single-file Streamlit app that fetches the RBI Press Releases RSS feed
server-side (browsers can't read most RSS directly because of CORS) and shows
it newest-first. No database, no scheduler — just the wire.

Look & feel: pick a data-terminal theme in the sidebar (Bloomberg, Reuters,
green/amber phosphor, etc.). Every palette is tuned so all text stays legible.

Run locally:   streamlit run streamlit_app.py
Deploy:        Streamlit Community Cloud, main file = streamlit_app.py
"""

import calendar
import html
import os
import re
from datetime import datetime, timezone, timedelta

import feedparser
import requests
import streamlit as st

import rbi_archive  # best-effort scraper for older releases (beyond the RSS ~10)

RBI_FEED = "https://rbi.org.in/pressreleases_rss.xml"
# Override to point at a mirror/cache (or for local testing) without code changes.
FEED_URL = os.environ.get("MARKETWIRE_FEED", RBI_FEED)
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))
# The wire re-runs itself on this interval (seconds) with no clicks; override via env.
REFRESH_SECONDS = int(os.environ.get("MARKETWIRE_REFRESH", "300"))
CACHE_TTL = max(REFRESH_SECONDS - 30, 15)  # just under the interval so each tick re-fetches
# Archive listing URL(s) to backfill history (comma-separated). Add month/year
# archive URLs here once you've confirmed RBI's pattern from a reachable machine.
ARCHIVE_URLS = tuple(
    u.strip() for u in os.environ.get("MARKETWIRE_ARCHIVE_URLS", rbi_archive.LISTING_URL).split(",") if u.strip()
)

# --------------------------------------------------------------------------- #
# Themes — data-terminal palettes. Each key is tuned for contrast so that ALL
# text (titles, body, timestamps, captions, inputs, links) stays readable.
#   bg=app background  panel=cards/inputs/sidebar  text=body  heading=titles
#   muted=timestamps/captions (kept high-contrast)  accent=hover/focus
#   link=hyperlinks  border=hairlines
# --------------------------------------------------------------------------- #
THEMES = {
    "Bloomberg": dict(bg="#000000", panel="#0F0F0F", text="#EDE6DA", heading="#FFA028",
                      muted="#C99A57", accent="#FFA028", link="#4FC3E8", border="#2B2218"),
    "Reuters Carbon": dict(bg="#0B0E13", panel="#151A24", text="#E7EAF1", heading="#FF7A1A",
                           muted="#9AA4B2", accent="#FF7A1A", link="#5BC8E0", border="#242B37"),
    "Amber Phosphor": dict(bg="#0B0E15", panel="#11151F", text="#E7EAF1", heading="#FFB23E",
                           muted="#8A93A6", accent="#FFB23E", link="#5BC8E0", border="#1E2532"),
    "Green Phosphor": dict(bg="#001108", panel="#04210F", text="#5BFF92", heading="#BFFFD6",
                           muted="#34C46E", accent="#00FF66", link="#7FFFD4", border="#0A4A28"),
    "Ice (Cyan)": dict(bg="#06121C", panel="#0C1B29", text="#DCEEF6", heading="#38D6FF",
                       muted="#86AABF", accent="#38D6FF", link="#6CE0C0", border="#173646"),
    "Paper (Light)": dict(bg="#FBFBF8", panel="#FFFFFF", text="#1A1A1A", heading="#7A3E00",
                          muted="#555555", accent="#C2410C", link="#1D4ED8", border="#D9D9D0"),
    "High Contrast": dict(bg="#000000", panel="#0B0B0B", text="#FFFFFF", heading="#FFFF00",
                          muted="#D0D0D0", accent="#FFFF00", link="#5AD1FF", border="#6A6A6A"),
}
DEFAULT_THEME = "Bloomberg"

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """Turn an RSS HTML summary into plain, single-spaced text."""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_feed(url):
    """Fetch + parse the feed. Returns (items, error). Cached per URL (see CACHE_TTL)."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
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


def _key(it):
    """Dedupe key: RBI prid if present in the link, else the link itself."""
    m = re.search(r"prid=(\d+)", it.get("link", ""), re.I)
    return m.group(1) if m else it.get("link", "")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_archive(urls):
    """Scrape RBI's listing page(s) for older releases. Returns (items, error).
    Isolated: any failure yields ([], error) so the RSS view still works."""
    all_items, errors = [], []
    for u in urls:
        got, err = rbi_archive.scrape_listing(u)
        all_items.extend(got)
        if err:
            errors.append(err)
    return all_items, ("; ".join(errors) if errors and not all_items else None)


def theme_css(p):
    """Build a full CSS override for palette `p`, forcing every text surface."""
    return f"""
    <style>
      /* surfaces */
      .stApp {{ background-color: {p['bg']}; color: {p['text']}; }}
      [data-testid="stHeader"] {{ background: {p['bg']}; }}
      section[data-testid="stSidebar"] > div {{ background-color: {p['panel']}; border-right: 1px solid {p['border']}; }}

      /* headings */
      .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {{ color: {p['heading']}; }}

      /* body text: markdown, lists, widget labels, sidebar */
      .stApp p, .stApp li, .stApp strong,
      [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
      [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
      section[data-testid="stSidebar"] label {{ color: {p['text']}; }}

      /* muted: captions + the timestamp line */
      [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {p['muted']} !important; }}
      [data-testid="stMarkdownContainer"] .mw-time {{
        color: {p['muted']}; font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size: 12px; letter-spacing: .02em;
      }}

      /* links */
      .stApp a, [data-testid="stMarkdownContainer"] a {{ color: {p['link']} !important; text-decoration: none; }}
      .stApp a:hover {{ text-decoration: underline; }}

      /* text input — force bg + text so it tracks the theme (esp. light palettes,
         which would otherwise inherit Streamlit's dark base and hide typed text) */
      .stTextInput div[data-baseweb="input"],
      .stTextInput div[data-baseweb="base-input"] {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      .stTextInput input {{ background-color: {p['panel']} !important; color: {p['text']} !important; -webkit-text-fill-color: {p['text']}; }}
      .stTextInput input::placeholder {{ color: {p['muted']} !important; -webkit-text-fill-color: {p['muted']}; opacity: 1; }}
      [data-testid="InputInstructions"] {{ color: {p['muted']} !important; }}

      /* selectbox — closed control */
      [data-baseweb="select"] > div {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      [data-baseweb="select"] div {{ color: {p['text']} !important; }}
      [data-baseweb="select"] svg {{ fill: {p['muted']}; }}
      /* selectbox — open dropdown. It's portaled to <body> (outside .stApp), so
         these selectors are global and forced, else light themes show dark-on-dark. */
      [data-baseweb="popover"] > div,
      [data-baseweb="menu"],
      ul[role="listbox"] {{ background-color: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      li[role="option"], [role="option"] {{ background-color: {p['panel']} !important; color: {p['text']} !important; }}
      li[role="option"]:hover,
      [role="option"][aria-selected="true"] {{ background-color: {p['border']} !important; color: {p['accent']} !important; }}

      /* buttons */
      .stButton > button {{ background-color: {p['panel']}; color: {p['text']}; border: 1px solid {p['border']}; }}
      .stButton > button:hover {{ color: {p['accent']}; border-color: {p['accent']}; }}

      /* expander */
      [data-testid="stExpander"] {{ background-color: {p['panel']}; border: 1px solid {p['border']}; border-radius: 6px; }}
      [data-testid="stExpander"] summary {{ color: {p['text']}; }}
      [data-testid="stExpander"] summary:hover {{ color: {p['accent']}; }}
      [data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {{ color: {p['text']}; }}

      /* dividers */
      .stApp hr {{ border-color: {p['border']}; }}
    </style>
    """


st.set_page_config(page_title="MarketWire · RBI", page_icon="◢", layout="wide")

# --- theme selection ---------------------------------------------------------
# Bind the selectbox to session_state via `key` and seed it once from the URL.
# (Passing index= from the query param fought the widget's own state, so a new
# pick only applied on the *next* rerun — i.e. you had to click twice.) Reading
# st.session_state["theme"] means the choice takes effect on the first click.
names = list(THEMES)
if "theme" not in st.session_state:
    _qp = st.query_params.get("theme")
    st.session_state["theme"] = _qp if _qp in names else DEFAULT_THEME
with st.sidebar:
    st.markdown("### ◢ MarketWire")
    theme = st.selectbox("Theme", names, key="theme", help="Data-terminal palettes — all text stays legible in each.")
    st.caption("Switch the look to suit your screen / lighting.")
    st.divider()
    st.checkbox(
        "Include archive (beyond latest 10)", value=True, key="archive",
        help="Also scrape RBI's press-release listing for older items. Works where "
             "RBI is reachable (your desk / VM); often blocked on Streamlit Cloud.",
    )
st.query_params["theme"] = theme  # keep the URL in sync (shareable / sticky)
st.markdown(theme_css(THEMES[theme]), unsafe_allow_html=True)

# --- header ------------------------------------------------------------------
head, refresh = st.columns([6, 1])
head.markdown("## ◢ MarketWire — RBI Press Releases")
if refresh.button("⟳ Refresh", use_container_width=True):
    fetch_feed.clear()
    st.rerun()

@st.fragment(run_every=REFRESH_SECONDS)
def wire():
    """The live wire. As a fragment with run_every, it re-runs itself every
    REFRESH_SECONDS with no clicks — only this part of the page, so the theme and
    header stay put. fetch_feed is cached just under the interval, so each tick
    pulls fresh data. (st.stop() can't be used in a fragment, so we return.)"""
    items, error = fetch_feed(FEED_URL)
    if error:
        st.error(f"Couldn't fetch the feed: {error}")
        st.caption(
            "Government sites sometimes block datacenter IPs (e.g. Streamlit Cloud). "
            "Try Refresh, or run it locally where the feed is reachable."
        )
        return
    if not items:
        st.info("The feed returned no items.")
        return

    # Optionally merge in older releases scraped from RBI's listing page (deduped
    # by prid). Isolated + non-fatal: if it's blocked/unavailable, show RSS only.
    note = ""
    if st.session_state.get("archive", True):
        arch_items, arch_err = fetch_archive(ARCHIVE_URLS)
        if arch_err and not arch_items:
            note = " · archive unavailable"
        else:
            keys = {_key(it) for it in items}
            added = 0
            for a in arch_items:
                k = _key(a)
                if k not in keys:
                    keys.add(k)
                    items.append(a)
                    added += 1
            items.sort(key=lambda x: x["ts"] or 0, reverse=True)
            note = f" · +{added} older from archive" if added else " · archive: no older items"

    q = st.text_input("Filter", placeholder="filter by keyword…", label_visibility="collapsed")
    shown = [it for it in items if q.lower() in (it["title"] + " " + it["summary"]).lower()] if q.strip() else items
    mins = REFRESH_SECONDS // 60
    every = f"{mins} min" if mins else f"{REFRESH_SECONDS}s"
    checked = datetime.now(IST).strftime("%H:%M:%S")
    st.caption(
        f"{len(shown)} of {len(items)} press releases · newest first{note} · "
        f"auto-refresh every {every} · last checked {checked} IST"
    )

    for it in shown:
        when = (
            datetime.fromtimestamp(it["ts"], IST).strftime("%d %b %Y · %H:%M IST")
            if it["ts"] else (it["published"] or "—")
        )
        st.markdown(
            f"**{it['title']}**  \n<span class='mw-time'>{when}</span>",
            unsafe_allow_html=True,
        )
        with st.expander("details"):
            st.write(it["summary"] or "(no summary in feed)")
            if it["link"].startswith("http"):
                st.markdown(f"[Open original ↗]({it['link']})")
        st.divider()


wire()
