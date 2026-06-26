"""MarketWire — an RBI press-release & notifications reader.

A Streamlit app that fetches the RBI Press Releases + Notifications RSS feeds
server-side (browsers can't read most RSS directly because of CORS), remembers
items in a small SQLite store so the wire accumulates over time, and presents them
as a news website: a newspaper masthead over a uniform grid of story cards, with
subtle fade-in/hover motion and a summary preview that expands to the full text.

Look & feel: pick a flagship theme in the sidebar (Bloomberg, Reuters, Paper,
High Contrast). Every palette is tuned so all text stays legible.

Run locally:   streamlit run streamlit_app.py
Deploy:        Streamlit Community Cloud, main file = streamlit_app.py
"""

import html
import os
from datetime import datetime, timezone, timedelta

import streamlit as st

# Mirror Streamlit Cloud Secrets into the environment so the same config keys work
# whether set as env vars (VM / Actions) or via the Cloud Secrets UI — needed for
# MARKETWIRE_DB / MARKETWIRE_DB_AUTH_TOKEN (durable history on Cloud).
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass  # no secrets configured — fine

import feed         # pure RSS fetch/parse (shared with the poller)
import history      # durable history kept as JSONL in the repo (maintained by the Action)
import store        # durable history backend (sqlite / Postgres / Turso)

# The two RBI feeds the app reads — both are shown together in one wire, each item
# tagged with its source `label`. Each carries its own live RSS URL, durable store
# category, and committed history file. Override any URL via env (mirror/cache or
# local testing) without code changes.
FEEDS = {
    "press": dict(
        name="Press Releases",            # friendly name shown in the Sources filter
        url=os.environ.get("MARKETWIRE_FEED", feed.RBI_FEED),
        category="press",
        history_url_env="MARKETWIRE_HISTORY_URL",
        history_path=history.HISTORY_PATH,
        label="RBI - Press Release",       # per-item source tag
    ),
    "notifications": dict(
        name="Notifications",
        url=os.environ.get("MARKETWIRE_NOTIFICATIONS_FEED", feed.RBI_NOTIFICATIONS_FEED),
        category="notifications",
        history_url_env="MARKETWIRE_NOTIFICATIONS_URL",
        history_path=history.NOTIFICATIONS_PATH,
        label="RBI - Notifications",
    ),
}
FEED_NAMES = [cfg["name"] for cfg in FEEDS.values()]
IST = timezone(timedelta(hours=5, minutes=30))
# The wire re-runs itself on this interval (seconds) with no clicks; override via env.
REFRESH_SECONDS = int(os.environ.get("MARKETWIRE_REFRESH", "300"))
CACHE_TTL = max(REFRESH_SECONDS - 30, 15)  # just under the interval so each tick re-fetches


def _is_archived(it):
    """Archive-sourced (date-only, no precise time) iff its ts is midnight IST."""
    ts = it.get("ts")
    if not ts:
        return False
    dt = datetime.fromtimestamp(ts, IST)
    return not (dt.hour or dt.minute or dt.second)


def _masthead_html():
    """The newspaper nameplate: kicker rule, serif wordmark, section line, rules."""
    today = datetime.now(IST).strftime("%A, %d %B %Y")
    return (
        "<div class='mw-masthead'>"
        f"<div class='mw-kicker'><span>Reserve Bank of India · Wire</span><span>{today} · IST</span></div>"
        "<div class='mw-wordmark'>MarketWire</div>"
        "<div class='mw-sub'>Press Releases &amp; Notifications</div>"
        "<div class='mw-rule'></div><div class='mw-rule-thin'></div>"
        "</div>"
    )


def _story_card_html(it):
    """Inner HTML for one story card: source tag, time, serif headline, and a
    CSS-clamped summary preview. The full body lives in the expander beside it."""
    ts = it.get("ts")
    dt = datetime.fromtimestamp(ts, IST) if ts else None
    archived = dt is not None and not (dt.hour or dt.minute or dt.second)
    if dt and not archived:
        when = dt.strftime("%d %b %Y · %H:%M IST")      # real time (live RSS)
    elif dt:
        when = dt.strftime("%d %b %Y")                  # date only (archive)
    else:
        when = html.escape(it.get("published") or "—")
    src = it.get("source") or ""
    notif = "notif" if "Notification" in src else "press"
    arch = " <span class='mw-tag'>ARCHIVE</span>" if archived else ""
    summary = (it.get("summary") or "").strip()
    preview = html.escape(summary[:340]) if summary else "<span class='mw-nosum'>Open for the full text →</span>"
    title = html.escape(it.get("title") or "(untitled)")
    link = it.get("link") or ""
    # Linkify the headline + show an explicit 'open' link, so the RBI page is one
    # click away without expanding. Only for real http links (archive stubs may lack one).
    if link.startswith("http"):
        href = html.escape(link, quote=True)
        head = f"<a href='{href}' target='_blank' rel='noopener'>{title}</a>"
        open_link = f"<a class='mw-open' href='{href}' target='_blank' rel='noopener'>Open on rbi.org.in ↗</a>"
    else:
        head, open_link = title, ""
    return (
        "<div class='mw-card'>"
        f"<div class='mw-meta'><span class='mw-src {notif}'>{html.escape(src)}</span>"
        f"<span class='mw-time'>{when}{arch}</span></div>"
        f"<div class='mw-head'>{head}</div>"
        f"<div class='mw-sum'>{preview}</div>"
        f"{open_link}"
        "</div>"
    )


def _relative_time(ts):
    """Trading-Economics-style relative stamp ('91 seconds ago', '16 minutes ago').
    Archive items (date-only) fall back to the date; very old items show the date."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, IST)
    if not (dt.hour or dt.minute or dt.second):        # archive: no real time
        return dt.strftime("%d %b %Y")
    secs = max(0, int((datetime.now(IST) - dt).total_seconds()))
    if secs < 60:
        v, unit = secs, "second"
    elif secs < 3600:
        v, unit = secs // 60, "minute"
    elif secs < 86400:
        v, unit = secs // 3600, "hour"
    elif secs < 30 * 86400:
        v, unit = secs // 86400, "day"
    else:
        return dt.strftime("%d %b %Y")
    return f"{v} {unit}{'s' if v != 1 else ''} ago"


def _stream_row_html(it):
    """One full-width stream row (Trading Economics style): the headline is the link
    (no separate 'open' link); the body is clamped small with an inline Show more /
    Show less toggle (pure <details>) when long; then a relative timestamp."""
    ts = it.get("ts")
    dt = datetime.fromtimestamp(ts, IST) if ts else None
    archived = dt is not None and not (dt.hour or dt.minute or dt.second)
    src = it.get("source") or ""
    notif = "notif" if "Notification" in src else "press"
    arch = "<span class='mw-tag'>ARCHIVE</span>" if archived else ""
    title = html.escape(it.get("title") or "(untitled)")
    link = it.get("link") or ""
    if link.startswith("http"):
        href = html.escape(link, quote=True)
        head = f"<a class='mw-shead' href='{href}' target='_blank' rel='noopener'>{title}</a>"
    else:
        head = f"<span class='mw-shead'>{title}</span>"
    text = (it.get("summary") or "").strip()
    if not text:
        body = "<div class='mw-sbody mw-sbody-plain'><span class='mw-nosum'>Open the headline for the full text.</span></div>"
    elif len(text) > 240:                       # long: clamp to a preview + expand
        body = ("<details class='mw-det'><summary>"
                f"<span class='mw-sbody'>{html.escape(text)}</span>"
                "<span class='mw-more'></span></summary></details>")
    else:                                       # short: show it all, no toggle
        body = f"<div class='mw-sbody mw-sbody-plain'>{html.escape(text)}</div>"
    return (
        "<div class='mw-stream'>"
        f"<div class='mw-srow'>{head}<span class='mw-stags'><span class='mw-src {notif}'>{html.escape(src)}</span>{arch}</span></div>"
        f"{body}"
        f"<div class='mw-sfoot'><span class='mw-time'>{_relative_time(ts)}</span></div>"
        "</div>"
    )


def _stream_html(items):
    """The whole single-column stream as one HTML blob (fewer Streamlit elements)."""
    return "<div class='mw-streamwrap'>" + "".join(_stream_row_html(it) for it in items) + "</div>"

# --------------------------------------------------------------------------- #
# Themes — flagship palettes, each tuned for contrast so ALL text (headlines, body,
# timestamps, captions, inputs, links) stays legible.
#   bg=page  panel=cards/inputs/sidebar  text=body  heading=headlines
#   muted=timestamps/captions  accent=primary brand (Press tag, hover, focus)
#   accent2=secondary (Notifications tag)  link=hyperlinks  border=hairlines
#   shadow=card hover glow (rgba)  headfont=masthead/headline font stack
# --------------------------------------------------------------------------- #
_SERIF = "'Source Serif 4', Georgia, 'Times New Roman', serif"   # newspaper headlines
_SANS = "'Libre Franklin', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"  # data-site headlines
THEMES = {
    "Bloomberg": dict(bg="#0A0A0A", panel="#161513", text="#E8E2D6", heading="#FBF7EF",
                      muted="#9A9080", accent="#FF9E1B", accent2="#4FC3E8", link="#6FD0EE",
                      border="#2A2622", shadow="rgba(255,158,27,.12)", headfont=_SERIF),
    "Reuters": dict(bg="#FFFFFF", panel="#FFFFFF", text="#15171C", heading="#08090B",
                    muted="#5C616B", accent="#FB6400", accent2="#0B6EFD", link="#0A66C2",
                    border="#E5E7EB", shadow="rgba(15,23,42,.10)", headfont=_SERIF),
    "Paper": dict(bg="#FBFAF4", panel="#FFFFFF", text="#1C1B17", heading="#12110D",
                  muted="#6A6456", accent="#B3471B", accent2="#1D4ED8", link="#1D4ED8",
                  border="#E7E1D2", shadow="rgba(80,60,30,.10)", headfont=_SERIF),
    # Trading Economics-inspired: soft-grey page, white cards, navy headlines,
    # signal blue/green accents, crisp sans (no serif) — a markets-data look.
    "Trading Economics": dict(bg="#EEF2F6", panel="#FFFFFF", text="#1C2A38", heading="#14304F",
                              muted="#6A7889", accent="#0E72BC", accent2="#13A36B", link="#0E72BC",
                              border="#DCE3EB", shadow="rgba(16,48,90,.12)", headfont=_SANS),
    "High Contrast": dict(bg="#000000", panel="#0C0C0C", text="#FFFFFF", heading="#FFFF00",
                          muted="#D8D8D8", accent="#FFE000", accent2="#5AD1FF", link="#6BD8FF",
                          border="#5C5C5C", shadow="rgba(255,224,0,.20)", headfont=_SERIF),
}
DEFAULT_THEME = "Bloomberg"

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_feed(url):
    """Cached wrapper around feed.fetch_rss. Returns (items, error)."""
    return feed.fetch_rss(url)


@st.cache_data(ttl=600, show_spinner=False)
def load_history(url_env, default_path):
    """Durable history from the repo for one feed: the committed JSONL, or a raw URL
    via the feed's env var. Cached (keyed on its args); the Refresh button clears it."""
    return history.load_durable(url_env=url_env, default_path=default_path)


def theme_css(p):
    """Build a full CSS override for palette `p` — masthead, card grid, subtle
    animations, and every themed widget (forcing portaled overlays too)."""
    return f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;500;600;700;800&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700;8..60,900&display=swap');

      /* ---- surfaces ---- */
      .stApp {{ background-color: {p['bg']}; color: {p['text']};
        font-family: 'Libre Franklin', system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
      [data-testid="stHeader"] {{ background: transparent; }}
      [data-testid="stToolbar"] {{ right: .5rem; }}
      .block-container {{ padding-top: 1.1rem; max-width: 1280px; }}
      section[data-testid="stSidebar"] > div {{ background-color: {p['panel']}; border-right: 1px solid {p['border']}; }}

      /* ---- newspaper masthead ---- */
      .mw-masthead {{ text-align: center; margin: 0 0 .3rem; animation: mwFade .5s ease both; }}
      .mw-kicker {{ display: flex; justify-content: space-between; align-items: center;
        font-size: 11px; letter-spacing: .16em; text-transform: uppercase; font-weight: 600;
        color: {p['muted']}; border-top: 1px solid {p['border']}; border-bottom: 1px solid {p['border']}; padding: 7px 2px; }}
      .mw-wordmark {{ font-family: {p['headfont']}; font-weight: 900;
        font-size: clamp(38px, 6.5vw, 74px); line-height: 1.02; letter-spacing: -.02em;
        color: {p['heading']}; margin: .16em 0 .05em; }}
      .mw-wordmark::first-letter {{ color: {p['accent']}; }}
      .mw-sub {{ font-size: 12px; letter-spacing: .34em; text-transform: uppercase;
        color: {p['muted']}; font-weight: 600; }}
      .mw-rule {{ height: 3px; background: {p['heading']}; margin: .55rem 0 0; }}
      .mw-rule-thin {{ height: 1px; background: {p['border']}; margin-top: 2px; }}

      /* ---- headings & body ---- */
      .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {{ color: {p['heading']};
        font-family: {p['headfont']}; }}
      .stApp p, .stApp li, .stApp strong,
      [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
      [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
      section[data-testid="stSidebar"] label {{ color: {p['text']}; }}
      [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {p['muted']} !important; }}

      /* ---- story cards: the bordered containers in the grid ---- */
      [data-testid="stVerticalBlockBorderWrapper"] {{
        background: {p['panel']}; border: 1px solid {p['border']} !important; border-radius: 10px;
        height: 100%; overflow: hidden;
        transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        animation: mwRise .45s ease both; }}
      [data-testid="stVerticalBlockBorderWrapper"]:hover {{
        transform: translateY(-3px); border-color: {p['accent']} !important;
        box-shadow: 0 12px 30px {p['shadow']}; }}
      @keyframes mwRise {{ from {{ opacity: 0; transform: translateY(9px); }} to {{ opacity: 1; transform: none; }} }}
      @keyframes mwFade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}

      .mw-card {{ padding: 15px 16px 4px; }}
      .mw-meta {{ display: flex; align-items: center; gap: 10px; margin-bottom: 9px; }}
      .mw-src {{ font-size: 10px; font-weight: 800; letter-spacing: .07em; text-transform: uppercase;
        padding: 3px 8px; border-radius: 3px; color: {p['bg']}; background: {p['accent']}; white-space: nowrap; }}
      .mw-src.notif {{ background: {p['accent2']}; }}
      .mw-time {{ font-size: 11px; color: {p['muted']}; letter-spacing: .02em; margin-left: auto; text-align: right; }}
      .mw-head {{ font-family: {p['headfont']}; font-weight: 700; font-size: 17px;
        line-height: 1.24; color: {p['heading']}; margin: 0 0 8px; }}
      /* clickable headline -> opens the RBI source in a new tab (keep heading colour) */
      [data-testid="stMarkdownContainer"] .mw-head a {{ color: {p['heading']} !important; text-decoration: none; }}
      [data-testid="stMarkdownContainer"] .mw-head a:hover {{ color: {p['accent']} !important; text-decoration: underline; }}
      .mw-sum {{ font-size: 13px; line-height: 1.5; color: {p['muted']};
        display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
      .mw-nosum {{ font-style: italic; opacity: .8; }}
      /* always-visible 'open original' link on the card face (no expand needed) */
      [data-testid="stMarkdownContainer"] .mw-open {{ display: inline-block; margin: 11px 0 2px;
        font-size: 12px; font-weight: 600; color: {p['link']} !important; }}
      [data-testid="stMarkdownContainer"] .mw-open:hover {{ text-decoration: underline; }}

      /* ---- stream layout: single-column feed (Trading Economics style) ---- */
      .mw-stream {{ padding: 16px 4px 15px; border-bottom: 1px solid {p['border']}; animation: mwRise .4s ease both; }}
      .mw-stream:first-child {{ border-top: 1px solid {p['border']}; }}
      .mw-srow {{ display: flex; align-items: flex-start; gap: 14px; }}
      [data-testid="stMarkdownContainer"] .mw-shead {{ flex: 1; font-family: {p['headfont']}; font-weight: 700;
        font-size: 19px; line-height: 1.3; color: {p['heading']} !important;
        text-decoration: underline; text-underline-offset: 3px; text-decoration-thickness: 1px; }}
      [data-testid="stMarkdownContainer"] .mw-shead:hover {{ color: {p['accent']} !important; }}
      .mw-stags {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; flex: none; max-width: 46%; margin-top: 4px; }}
      .mw-stags .mw-tag {{ margin-left: 0; }}
      .mw-sbody {{ font-size: 14.5px; line-height: 1.62; color: {p['text']}; margin: 9px 0 0; }}
      .mw-sbody-plain {{ margin: 9px 0 11px; }}
      .mw-det {{ margin: 0; }}
      .mw-det > summary {{ list-style: none; cursor: pointer; outline: none; }}
      .mw-det > summary::-webkit-details-marker {{ display: none; }}
      .mw-det .mw-sbody {{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }}
      .mw-det[open] .mw-sbody {{ -webkit-line-clamp: unset; display: block; overflow: visible; }}
      .mw-more {{ display: inline-block; margin: 6px 0 11px; font-size: 12px; font-weight: 600; color: {p['accent']}; }}
      .mw-more::after {{ content: "Show more ▾"; }}
      .mw-det[open] .mw-more::after {{ content: "Show less ▴"; }}
      .mw-sfoot {{ display: flex; align-items: center; gap: 16px; }}
      .mw-sfoot .mw-time {{ margin: 0; }}
      .mw-tag {{ font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
        color: {p['muted']}; border: 1px solid {p['border']}; border-radius: 3px; padding: 1px 5px; margin-left: 7px; }}

      /* expander = the in-card 'Full text' toggle (strip its default chrome) */
      [data-testid="stExpander"] {{ border: none !important; background: transparent !important; }}
      [data-testid="stExpander"] details {{ border: none !important; background: transparent !important; }}
      [data-testid="stExpander"] summary {{ color: {p['accent']}; font-size: 12px; font-weight: 600; padding: 4px 16px; }}
      [data-testid="stExpander"] summary:hover {{ color: {p['accent']}; opacity: .78; }}
      [data-testid="stExpander"] summary p {{ color: {p['accent']} !important; font-weight: 600; }}
      [data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {{ color: {p['text']}; font-size: 13.5px; line-height: 1.55; }}

      /* links */
      .stApp a, [data-testid="stMarkdownContainer"] a {{ color: {p['link']} !important; text-decoration: none; font-weight: 600; }}
      .stApp a:hover {{ text-decoration: underline; }}

      /* ---- inputs ---- */
      .stTextInput div[data-baseweb="input"],
      .stTextInput div[data-baseweb="base-input"] {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      .stTextInput input {{ background-color: {p['panel']} !important; color: {p['text']} !important; -webkit-text-fill-color: {p['text']}; }}
      .stTextInput input::placeholder {{ color: {p['muted']} !important; -webkit-text-fill-color: {p['muted']}; opacity: 1; }}
      [data-testid="InputInstructions"] {{ color: {p['muted']} !important; }}
      .stDateInput div[data-baseweb="input"], .stDateInput div[data-baseweb="base-input"] {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      .stDateInput input {{ background-color: {p['panel']} !important; color: {p['text']} !important; -webkit-text-fill-color: {p['text']}; }}

      /* selectbox / multiselect — closed control */
      [data-baseweb="select"] > div {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      [data-baseweb="select"] div {{ color: {p['text']} !important; }}
      [data-baseweb="select"] svg {{ fill: {p['muted']}; }}
      /* multiselect chips */
      span[data-baseweb="tag"] {{ background-color: {p['accent']} !important; color: {p['bg']} !important; border-radius: 4px; }}
      span[data-baseweb="tag"] span {{ color: {p['bg']} !important; }}
      span[data-baseweb="tag"] svg {{ fill: {p['bg']} !important; }}

      /* portaled overlays (dropdowns, calendar) live outside .stApp — force globally */
      [data-baseweb="popover"] > div, [data-baseweb="menu"], ul[role="listbox"] {{
        background-color: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      li[role="option"], [role="option"] {{ background-color: {p['panel']} !important; color: {p['text']} !important; }}
      li[role="option"]:hover, [role="option"][aria-selected="true"] {{ background-color: {p['border']} !important; color: {p['accent']} !important; }}
      /* date-picker calendar */
      [data-baseweb="calendar"], [data-baseweb="calendar"] > div {{ background-color: {p['panel']} !important; }}
      [data-baseweb="calendar"] * {{ color: {p['text']} !important; }}
      [data-baseweb="calendar"] [aria-selected="true"] {{ background-color: {p['accent']} !important; color: {p['bg']} !important; }}
      [data-baseweb="calendar"] [aria-selected="true"] * {{ color: {p['bg']} !important; }}

      /* tooltips */
      [data-testid="stTooltipContent"] {{ background-color: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      [data-testid="stTooltipContent"], [data-testid="stTooltipContent"] * {{ color: {p['text']} !important; }}

      /* radio (sort order) selected dot */
      [role="radiogroup"] [aria-checked="true"] div:first-child {{ background-color: {p['accent']} !important; border-color: {p['accent']} !important; }}

      /* buttons */
      .stButton > button {{ background-color: {p['panel']}; color: {p['text']}; border: 1px solid {p['border']};
        border-radius: 6px; font-weight: 600; transition: all .15s ease; }}
      .stButton > button:hover {{ color: {p['bg']}; background: {p['accent']}; border-color: {p['accent']}; }}

      .stApp hr {{ border-color: {p['border']}; }}
    </style>
    """


st.set_page_config(page_title="MarketWire · RBI", page_icon="◢", layout="wide")
store.init_db()

# --- theme selection ---------------------------------------------------------
# Bind the selectbox to session_state via `key` and seed it once from the URL.
# (Passing index= from the query param fought the widget's own state, so a new
# pick only applied on the *next* rerun — i.e. you had to click twice.) Reading
# st.session_state["theme"] means the choice takes effect on the first click.
names = list(THEMES)
if "theme" not in st.session_state:
    _qp = st.query_params.get("theme")
    st.session_state["theme"] = _qp if _qp in names else DEFAULT_THEME
# Seed the Sources multiselect once from the URL (?sources=Press Releases,Notifications),
# same first-click pattern as the theme. Absent param → all feeds selected.
if "sources" not in st.session_state:
    _qs = st.query_params.get("sources")
    if _qs is None:
        st.session_state["sources"] = FEED_NAMES
    else:
        st.session_state["sources"] = [s for s in _qs.split(",") if s in FEED_NAMES]
LAYOUTS = ["Stream", "Grid"]
if "layout" not in st.session_state:
    _ql = st.query_params.get("layout")
    st.session_state["layout"] = _ql if _ql in LAYOUTS else "Stream"
with st.sidebar:
    st.markdown("### ◢ MarketWire")
    sources = st.multiselect(
        "Sources", FEED_NAMES, key="sources",
        help="Which RBI feeds to show — keep all selected, or pick one/some individually.",
    )
    layout = st.radio(
        "Layout", LAYOUTS, key="layout", horizontal=True,
        help="Stream = single-column feed (Trading Economics style). Grid = card grid.",
    )
    st.divider()
    theme = st.selectbox("Theme", names, key="theme", help="Five flagship palettes — all text stays legible in each.")
    st.caption("Switch the look to suit your screen / lighting.")
    st.divider()
    st.checkbox(
        "Show archive (older, date-only)", value=True, key="archive",
        help="Show older releases backfilled from RBI's listing — full text, but date "
             "only (RBI's pages don't expose a publish time). Off = live RSS items only.",
    )
st.query_params["theme"] = theme  # keep the URL in sync (shareable / sticky)
st.query_params["sources"] = ",".join(sources)
st.query_params["layout"] = layout
st.markdown(theme_css(THEMES[theme]), unsafe_allow_html=True)

# --- masthead ----------------------------------------------------------------
st.markdown(_masthead_html(), unsafe_allow_html=True)
_spacer, refresh = st.columns([6, 1])
if refresh.button("⟳ Refresh", use_container_width=True):
    fetch_feed.clear()
    load_history.clear()
    st.rerun()

@st.fragment(run_every=REFRESH_SECONDS)
def wire():
    """The live wire. As a fragment with run_every, it re-runs itself every
    REFRESH_SECONDS with no clicks — only this part of the page, so the theme and
    header stay put. fetch_feed is cached just under the interval, so each tick
    pulls fresh data. (st.stop() can't be used in a fragment, so we return.)"""
    # Pull each SELECTED feed, tag every item with its source label, then interleave
    # them into one newest-first wire. Each feed is deduped on its OWN ids (prid / Id)
    # BEFORE combining, so a press-release prid never merges with a notification Id
    # that happens to share the number. Archive items (older, date-only, full body)
    # come pre-enriched from the repo history the Action maintains.
    selected = st.session_state.get("sources", FEED_NAMES)
    active = [cfg for cfg in FEEDS.values() if cfg["name"] in selected]
    if not active:
        st.info("No sources selected — pick at least one feed in the sidebar.")
        return
    items, new_count, errors = [], 0, []
    for cfg in active:
        rss_items, error = fetch_feed(cfg["url"])
        if error:
            errors.append(error)
        new_count += store.upsert(rss_items or [], category=cfg["category"])  # accumulate live RSS in the DB
        feed_items = history.dedupe(
            store.load(limit=5000, category=cfg["category"])
            + load_history(cfg["history_url_env"], cfg["history_path"])
            + (rss_items or [])
        )
        for it in feed_items:
            it["source"] = cfg["label"]
        items += feed_items
    if not st.session_state.get("archive", True):
        items = [it for it in items if not _is_archived(it)]

    if not items:
        if errors:
            st.error(f"Couldn't fetch the feeds: {errors[0]}")
            st.caption(
                "Government sites sometimes block datacenter IPs (e.g. Streamlit Cloud). "
                "Try Refresh, or run it locally where the feeds are reachable."
            )
        else:
            st.info("Nothing stored yet — try ⟳ Refresh.")
        return

    bits = []
    if new_count:
        bits.append(f"{new_count} new this fetch")
    if errors:
        bits.append("live feed unreachable — showing stored")
    note = (" · " + " · ".join(bits)) if bits else ""

    # --- controls: keyword · sort order · optional date range ----------------
    c_kw, c_sort, c_date = st.columns([3, 2, 2])
    q = c_kw.text_input("Filter", placeholder="filter by keyword…", label_visibility="collapsed")
    order = c_sort.radio(
        "Sort order", ["Newest first", "Oldest first"], horizontal=True,
        label_visibility="collapsed", key="order",
        help="Sort the wire by date — newest first (descending) or oldest first (ascending).",
    )
    # Date filtering is opt-in (default off) so newly-published items always show
    # without having to widen a range; bounds track the items currently loaded.
    dated = [it["ts"] for it in items if it.get("ts")]
    date_on = c_date.checkbox(
        "Filter by date", key="date_filter", disabled=not dated,
        help="Limit the wire to a date range you pick.",
    )
    lo = hi = None
    if date_on and dated:
        dmin = datetime.fromtimestamp(min(dated), IST).date()
        dmax = datetime.fromtimestamp(max(dated), IST).date()
        picked = st.date_input(
            "Date range", value=(dmin, dmax), format="DD/MM/YYYY",
            key="date_range", label_visibility="collapsed",
        )
        pair = picked if isinstance(picked, (list, tuple)) else (picked,)
        if len(pair) == 2:
            lo, hi = pair
        elif len(pair) == 1:
            lo = hi = pair[0]
        if lo and hi and lo > hi:           # tolerate a reversed pick
            lo, hi = hi, lo

    shown = items
    if q.strip():
        shown = [it for it in shown if q.lower() in (it["title"] + " " + (it["summary"] or "")).lower()]
    if lo and hi:
        shown = [it for it in shown
                 if it.get("ts") and lo <= datetime.fromtimestamp(it["ts"], IST).date() <= hi]
    reverse = order == "Newest first"
    shown = sorted(shown, key=lambda x: x.get("ts") or 0, reverse=reverse)

    order_txt = "newest first" if reverse else "oldest first"
    range_txt = f" · {lo:%d %b %Y}–{hi:%d %b %Y}" if (lo and hi) else ""
    mins = REFRESH_SECONDS // 60
    every = f"{mins} min" if mins else f"{REFRESH_SECONDS}s"
    checked = datetime.now(IST).strftime("%H:%M:%S")
    st.caption(
        f"{len(shown)} of {len(items)} stored items · {order_txt}{range_txt}{note} · "
        f"auto-refresh every {every} · last checked {checked} IST"
    )

    # --- render: Stream (single-column feed) or Grid (3-up card grid) ---------
    RENDER_CAP = 120          # bound the DOM; narrow with filters to reach the rest
    visible = shown[:RENDER_CAP]
    if len(shown) > RENDER_CAP:
        st.caption(f"Showing the first {RENDER_CAP} of {len(shown)} — narrow with the filters above to see the rest.")

    if st.session_state.get("layout", "Stream") == "Grid":
        CARD_COLS = 3
        for row in range(0, len(visible), CARD_COLS):
            cols = st.columns(CARD_COLS, gap="small")
            for col, it in zip(cols, visible[row:row + CARD_COLS]):
                with col:
                    with st.container(border=True):
                        st.markdown(_story_card_html(it), unsafe_allow_html=True)
                        with st.expander("Full text"):
                            body = (it.get("summary") or "").strip()
                            st.write(body or "(full text not stored yet — use the link above to open it on rbi.org.in)")
    else:
        st.markdown(_stream_html(visible), unsafe_allow_html=True)


wire()
