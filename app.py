"""app.py — MarketWire viewer (Streamlit).

Reads from the shared SQLite database and gives an equity analyst a fast way to
stay current: priority triage, "what did I miss", full-history search, a live
watchlist, and one-click export. Run with:  streamlit run app.py
"""

import io
import csv
import os
import time
from datetime import date, datetime, timedelta

import streamlit as st

# On Streamlit Community Cloud, configuration lives in st.secrets, not the process
# environment. Mirror it into os.environ *before* importing core — which reads
# MARKETWIRE_DB / MARKETWIRE_ALERT_SCORE at import time — so the same keys work
# whether they come from env vars (VM / GitHub Actions) or Secrets (Cloud).
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass  # no secrets configured (e.g. a plain local run) — that's fine

import core
from core import (
    get_conn, init_db, get_watchlist, add_watch_term, remove_watch_term,
    toggle_flag, set_flag, query_articles, counts, get_sources_status,
    get_meta, set_meta, poll_all, highlight, fmt_stamp,
    CATEGORIES, CATEGORY_META, IST, ALERT_SCORE,
)
from feeds import FEEDS

st.set_page_config(page_title="MarketWire", page_icon="◢", layout="wide")

# Optional in-session auto-refresh (separate package; degrade gracefully).
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

st.markdown(
    """
    <style>
      .mw-mark { background:#3a2e12; color:#ffcf7a; border-radius:2px; padding:0 2px; }
      .mw-read { color:#6b7280; }
      .mw-time { font-family:ui-monospace,Menlo,Consolas,monospace; color:#8A93A6; font-size:12px; }
      .mw-tag  { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11px; letter-spacing:.08em; }
      .mw-title { font-size:15px; line-height:1.35; }
      .mw-hl { color:#FFB23E; font-weight:600; }
      div[data-testid="stHorizontalBlock"] { align-items:center; }
      .block-container { padding-top:1.6rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

conn = get_conn()
init_db(conn)
terms = list(get_watchlist(conn).keys())

SOURCE_NAMES = {f["id"]: f["name"] for f in FEEDS}
NAME_TO_ID = {v: k for k, v in SOURCE_NAMES.items()}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_csv(rows):
    buf = io.StringIO()
    cols = ["published_raw", "source_name", "category", "title", "score", "watch_hits", "prid", "link"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def render_items(rows, prefix):
    """Render a wire of items. Each row: time | tag | headline, with a detail expander."""
    if not rows:
        st.caption("Nothing here yet. Try **Fetch now** in the sidebar, or widen your filters.")
        return
    for i, r in enumerate(rows):
        meta = CATEGORY_META.get(r["category"], CATEGORY_META["Press Release"])
        c1, c2, c3 = st.columns([1.0, 1.1, 8.0])
        ts = r.get("published_ts") or r.get("fetched_ts")
        clock = datetime.fromtimestamp(ts, IST).strftime("%d %b · %H:%M") if ts else "—"
        c1.markdown(f"<span class='mw-time'>{clock}</span>", unsafe_allow_html=True)
        c2.markdown(
            f"<span class='mw-tag' style='color:{meta['color']}'>● {meta['short']}</span>",
            unsafe_allow_html=True,
        )
        flags = ""
        if r.get("watch_hits"):
            flags += " 🔔"
        if r.get("starred"):
            flags += " ⭐"
        title_html = highlight(r["title"], terms)
        cls = "mw-title mw-read" if r["read"] else "mw-title"
        c3.markdown(f"<span class='{cls}'>{title_html}{flags}</span>", unsafe_allow_html=True)

        with st.expander("details"):
            st.markdown(highlight(r["summary"] or "(no summary in feed)", terms), unsafe_allow_html=True)
            cap = f"{r['source_name']}  ·  {fmt_stamp(r)}  ·  score {r['score']}"
            if r.get("prid"):
                cap += f"  ·  PR {r['prid']}"
            st.caption(cap)
            b1, b2, b3 = st.columns([1.4, 1.4, 6])
            star_label = "★ Unstar" if r["starred"] else "☆ Star"
            read_label = "Mark unread" if r["read"] else "Mark read"
            if b1.button(star_label, key=f"{prefix}_star_{i}"):
                toggle_flag(conn, r["link"], "starred")
                st.rerun()
            if b2.button(read_label, key=f"{prefix}_read_{i}"):
                toggle_flag(conn, r["link"], "read")
                st.rerun()
            if r["link"].startswith("http"):
                b3.markdown(f"[Open original ↗]({r['link']})")
        st.divider()


# --------------------------------------------------------------------------- #
# Sidebar — controls, watchlist, filters
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### ◢ MarketWire")
    st.caption("RSS desk for Indian markets")

    if st.button("⟳ Fetch now", use_container_width=True, type="primary"):
        with st.spinner("Fetching feeds…"):
            summ = poll_all(send_alerts=False)
        msg = f"Fetched {summ['fetched']} items · {summ['new']} new"
        if summ["errors"]:
            msg += f" · {len(summ['errors'])} feed error(s) — see Sources tab"
        st.toast(msg)
        st.rerun()

    last_poll = get_meta(conn, "last_poll_ts")
    if last_poll:
        ago = int(time.time()) - int(last_poll)
        st.caption(f"Last fetch: {ago // 60} min ago" if ago >= 60 else "Last fetch: just now")

    if HAS_AUTOREFRESH:
        if st.toggle("Auto-refresh page (60s)", value=False, help="Re-runs the page; fetch on a schedule via the poller for true freshness"):
            st_autorefresh(interval=60_000, key="mw_autorefresh")
    else:
        st.caption("Install `streamlit-autorefresh` for live page refresh.")

    st.divider()
    st.markdown("**Watchlist**")
    st.caption("Matches are highlighted, scored up, and trigger alerts.")
    wl = get_watchlist(conn)
    chips = st.columns(2)
    for j, (term, weight) in enumerate(sorted(wl.items())):
        if chips[j % 2].button(f"✕ {term}", key=f"wl_del_{j}", help=f"weight {weight} — click to remove"):
            remove_watch_term(conn, term)
            st.rerun()
    nt1, nt2 = st.columns([3, 1])
    new_term = nt1.text_input("Add term", key="wl_new", label_visibility="collapsed", placeholder="Add keyword / ticker")
    new_w = nt2.number_input("w", min_value=1, max_value=9, value=4, key="wl_w", label_visibility="collapsed")
    if st.button("Add to watchlist", use_container_width=True):
        if new_term.strip():
            add_watch_term(conn, new_term, int(new_w))
            st.rerun()

    st.divider()
    st.markdown("**Filters**")
    f_sources = st.multiselect("Sources", options=list(SOURCE_NAMES.values()), default=[])
    f_cats = st.multiselect("Categories", options=CATEGORIES, default=[])
    use_dates = st.checkbox("Filter by date", value=False)
    since_ts = until_ts = None
    if use_dates:
        today = date.today()
        dr = st.date_input("Range", value=(today - timedelta(days=30), today))
        if isinstance(dr, (list, tuple)) and len(dr) == 2:
            since_ts = int(datetime(dr[0].year, dr[0].month, dr[0].day, tzinfo=IST).timestamp())
            until_ts = int(datetime(dr[1].year, dr[1].month, dr[1].day, 23, 59, 59, tzinfo=IST).timestamp())
    f_unread = st.checkbox("Unread only", value=False)
    f_watch = st.checkbox("Watchlist only", value=False)
    limit = st.slider("Max items shown", 20, 300, 80, step=20)

selected_source_ids = [NAME_TO_ID[n] for n in f_sources] or None
selected_cats = f_cats or None

common = dict(
    sources=selected_source_ids, categories=selected_cats,
    since_ts=since_ts, until_ts=until_ts,
    unread_only=f_unread, watch_only=f_watch, limit=limit,
)


# --------------------------------------------------------------------------- #
# Header + metrics
# --------------------------------------------------------------------------- #
c = counts(conn)
st.markdown("## ◢ MarketWire")
m = st.columns(5)
m[0].metric("Total stored", c["total"])
m[1].metric("New (24h)", c["today"])
m[2].metric("Unread", c["unread"])
m[3].metric("Priority", c["priority"])
m[4].metric("Starred", c["starred"])

tab_wire, tab_pri, tab_new, tab_star, tab_search, tab_src = st.tabs(
    ["📰 Wire", "🔴 Priority", "🆕 Since last visit", "⭐ Starred", "🔎 Search", "🛰 Sources"]
)

with tab_wire:
    rows = query_articles(conn, **common)
    st.download_button("⬇ Export view (CSV)", to_csv(rows), "marketwire_wire.csv", "text/csv")
    render_items(rows, "wire")

with tab_pri:
    st.caption(f"Watchlist hits, or anything scoring ≥ {ALERT_SCORE}. Sorted by impact.")
    rows = query_articles(conn, priority_only=True, order="score DESC, published_ts DESC", **common)
    render_items(rows, "pri")

with tab_new:
    last_visit = int(get_meta(conn, "last_visit_ts", 0) or 0)
    cta, _ = st.columns([2, 6])
    if cta.button("✓ Mark all caught up"):
        set_meta(conn, "last_visit_ts", int(time.time()))
        st.rerun()
    if last_visit:
        st.caption(f"Everything added since {datetime.fromtimestamp(last_visit, IST):%d %b %H:%M IST}.")
    else:
        st.caption("First visit — showing the most recent items. Click *Mark all caught up* to set your baseline.")
    rows = query_articles(conn, since_ts=last_visit or None,
                          sources=selected_source_ids, categories=selected_cats, limit=limit)
    render_items(rows, "new")

with tab_star:
    rows = query_articles(conn, starred_only=True, order="published_ts DESC", limit=300)
    render_items(rows, "star")

with tab_search:
    q = st.text_input("Search the full history", placeholder="e.g. QRSAM, repo rate, cut-off, Maharashtra…")
    if q.strip():
        rows = query_articles(conn, text=q.strip(), sources=selected_source_ids,
                              categories=selected_cats, since_ts=since_ts, until_ts=until_ts,
                              limit=300)
        st.caption(f"{len(rows)} match(es).")
        st.download_button("⬇ Export results (CSV)", to_csv(rows), "marketwire_search.csv", "text/csv")
        render_items(rows, "search")
    else:
        st.caption("Type to search across every item ever fetched — titles and summaries.")

with tab_src:
    st.caption("Health of each feed at its last fetch. Errors here are isolated and never break the app.")
    srcs = get_sources_status(conn)
    if not srcs:
        st.info("No fetches yet. Click **Fetch now** in the sidebar.")
    else:
        table = []
        for s in srcs:
            when = datetime.fromtimestamp(s["last_fetch_ts"], IST).strftime("%d %b %H:%M") if s["last_fetch_ts"] else "—"
            table.append({
                "Source": s["name"],
                "Status": "✅ ok" if s["last_status"] == "ok" else "⚠️ error",
                "Items": s["item_count"],
                "Last fetch": when,
                "Note": s["error"][:80],
            })
        st.dataframe(table, use_container_width=True, hide_index=True)
    st.caption("Configured feeds (edit `feeds.py` to add/remove):")
    st.dataframe(
        [{"id": f["id"], "name": f["name"], "enabled": f.get("enabled", True), "url": f["url"]} for f in FEEDS],
        use_container_width=True, hide_index=True,
    )

conn.close()
