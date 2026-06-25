"""core.py — the shared engine for MarketWire.

Used by both app.py (the Streamlit viewer) and poller.py (the scheduled fetcher).
Responsibilities: database, fetching/parsing feeds, categorisation, priority
scoring, full-history search, and the poll loop.
"""

import os
import re
import time
import html
import sqlite3
import calendar
from datetime import datetime, timezone, timedelta

import requests
import feedparser

from feeds import FEEDS, CATEGORY_WEIGHTS, DEFAULT_WATCHLIST

DB_PATH = os.environ.get(
    "MARKETWIRE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "marketwire.db"),
)
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
ALERT_SCORE = int(os.environ.get("MARKETWIRE_ALERT_SCORE", "9"))

CATEGORIES = list(CATEGORY_WEIGHTS.keys())

CATEGORY_META = {
    "Auctions / G-Sec":   {"color": "#FFB23E", "short": "AUCTION"},
    "Rates":              {"color": "#F0C674", "short": "RATES"},
    "Bank Supervision":   {"color": "#E8736B", "short": "SUPV"},
    "Monetary Policy":    {"color": "#5BC8E0", "short": "POLICY"},
    "Data / Statistics":  {"color": "#7E9CC4", "short": "DATA"},
    "Press Release":      {"color": "#8A93A6", "short": "PR"},
}

_CATEGORY_RULES = [
    (re.compile(r"treasury bill|government stock|auction|securities|gs \d|t-bill|borrowing|\bsgs\b|devolvement|cut-?off", re.I), "Auctions / G-Sec"),
    (re.compile(r"section 35a|banking regulation|co-?operative|\bdirections\b|penalty|cancellation of licen|moratorium|imposed", re.I), "Bank Supervision"),
    (re.compile(r"floating rate|rate of interest|\brepo\b|policy rate|coupon|reverse repo", re.I), "Rates"),
    (re.compile(r"monetary policy|\bmpc\b", re.I), "Monetary Policy"),
    (re.compile(r"statistical|money stock|reserves|bulletin|supplement", re.I), "Data / Statistics"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style).*?</\1>")
_PRID_RE = re.compile(r"Press Release:\s*([\d\-/]+)", re.I)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def strip_html(s: str) -> str:
    if not s:
        return ""
    s = _SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def categorize(title: str) -> str:
    for rx, cat in _CATEGORY_RULES:
        if rx.search(title or ""):
            return cat
    return "Press Release"


def compute_score(title, summary, category, published_ts, source_weight, watch_terms):
    """Return (score, watch_hits). Higher score = more worth your attention."""
    text = f"{title} {summary}".lower()
    title_l = (title or "").lower()
    score = source_weight + CATEGORY_WEIGHTS.get(category, 1)
    hits = 0
    for term, weight in watch_terms.items():
        tl = term.lower()
        if tl and tl in text:
            hits += 1
            score += weight
            if tl in title_l:
                score += 1  # match in the headline counts for more
    if published_ts:
        age_h = (time.time() - published_ts) / 3600.0
        if age_h <= 24:
            score += 2
        elif age_h <= 72:
            score += 1
    return score, hits


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            link         TEXT PRIMARY KEY,
            source_id    TEXT,
            source_name  TEXT,
            title        TEXT,
            summary      TEXT,
            category     TEXT,
            prid         TEXT,
            published_raw TEXT,
            published_ts INTEGER,
            fetched_ts   INTEGER,
            score        INTEGER DEFAULT 0,
            watch_hits   INTEGER DEFAULT 0,
            read         INTEGER DEFAULT 0,
            starred      INTEGER DEFAULT 0,
            note         TEXT DEFAULT '',
            alerted      INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_articles_pub  ON articles(published_ts);
        CREATE INDEX IF NOT EXISTS idx_articles_src  ON articles(source_id);
        CREATE TABLE IF NOT EXISTS sources (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            url         TEXT,
            last_status TEXT,
            last_fetch_ts INTEGER,
            item_count  INTEGER DEFAULT 0,
            error       TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS watchlist (
            term   TEXT PRIMARY KEY,
            weight INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Seed the watchlist on first run only.
    if conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist(term, weight) VALUES (?, ?)",
            list(DEFAULT_WATCHLIST.items()),
        )
    conn.commit()
    if own:
        conn.close()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_watchlist(conn):
    return {r["term"]: r["weight"] for r in conn.execute("SELECT term, weight FROM watchlist")}


def add_watch_term(conn, term, weight=4):
    term = term.strip()
    if not term:
        return
    conn.execute(
        "INSERT INTO watchlist(term, weight) VALUES(?, ?) "
        "ON CONFLICT(term) DO UPDATE SET weight=excluded.weight",
        (term, weight),
    )
    conn.commit()


def remove_watch_term(conn, term):
    conn.execute("DELETE FROM watchlist WHERE term=?", (term,))
    conn.commit()


def set_flag(conn, link, field, value):
    if field not in ("read", "starred", "alerted"):
        return
    conn.execute(f"UPDATE articles SET {field}=? WHERE link=?", (int(value), link))
    conn.commit()


def toggle_flag(conn, link, field):
    row = conn.execute(f"SELECT {field} FROM articles WHERE link=?", (link,)).fetchone()
    if row is not None:
        set_flag(conn, link, field, 0 if row[field] else 1)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_source(feed):
    """Fetch + parse one feed. Returns a list of article dicts. Raises on failure."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    resp = requests.get(feed["url"], headers=headers, timeout=20)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a readable feed ({getattr(parsed, 'bozo_exception', 'unknown')})")

    items = []
    for e in parsed.entries:
        link = e.get("link") or e.get("id") or ""
        title = (e.get("title") or "(untitled)").strip()
        raw = e.get("summary") or e.get("description") or ""
        if not raw and e.get("content"):
            raw = e["content"][0].get("value", "")
        summary = strip_html(raw)
        prid = None
        m = _PRID_RE.search(summary)
        if m:
            prid = m.group(1)
        published_raw = e.get("published") or e.get("updated") or ""
        st = e.get("published_parsed") or e.get("updated_parsed")
        published_ts = calendar.timegm(st) if st else None
        items.append(
            {
                "link": link or f"{feed['id']}::{title}",
                "source_id": feed["id"],
                "source_name": feed["name"],
                "source_weight": feed.get("weight", 1),
                "title": title,
                "summary": summary[:800],
                "category": categorize(title),
                "prid": prid,
                "published_raw": published_raw,
                "published_ts": published_ts,
            }
        )
    return items


def upsert_articles(conn, items, watch_terms):
    """Insert items not already stored. Returns the list of newly inserted dicts."""
    new_rows = []
    for it in items:
        exists = conn.execute("SELECT 1 FROM articles WHERE link=?", (it["link"],)).fetchone()
        if exists:
            continue
        score, hits = compute_score(
            it["title"], it["summary"], it["category"],
            it["published_ts"], it["source_weight"], watch_terms,
        )
        conn.execute(
            """INSERT INTO articles
               (link, source_id, source_name, title, summary, category, prid,
                published_raw, published_ts, fetched_ts, score, watch_hits,
                read, starred, note, alerted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,'',0)""",
            (
                it["link"], it["source_id"], it["source_name"], it["title"],
                it["summary"], it["category"], it.get("prid"),
                it.get("published_raw"), it.get("published_ts"),
                int(time.time()), score, hits,
            ),
        )
        it = dict(it, score=score, watch_hits=hits)
        new_rows.append(it)
    conn.commit()
    return new_rows


def record_source_status(conn, feed, status, count, error=""):
    conn.execute(
        """INSERT INTO sources(id, name, url, last_status, last_fetch_ts, item_count, error)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, url=excluded.url, last_status=excluded.last_status,
             last_fetch_ts=excluded.last_fetch_ts, item_count=excluded.item_count,
             error=excluded.error""",
        (feed["id"], feed["name"], feed["url"], status, int(time.time()), count, error),
    )
    conn.commit()


def poll_all(send_alerts=False, only_enabled=True):
    """Fetch every (enabled) feed, store new items, optionally fire alerts.

    Returns a summary dict. This is the single function both the UI's
    'Fetch now' button and the scheduled poller call.
    """
    conn = get_conn()
    init_db(conn)
    watch = get_watchlist(conn)
    summary = {"fetched": 0, "new": 0, "errors": [], "alerts_sent": []}
    all_new = []

    for feed in FEEDS:
        if only_enabled and not feed.get("enabled", True):
            continue
        try:
            items = fetch_source(feed)
            new = upsert_articles(conn, items, watch)
            record_source_status(conn, feed, "ok", len(items), "")
            summary["fetched"] += len(items)
            summary["new"] += len(new)
            all_new.extend(new)
        except Exception as ex:  # isolate per-feed failures
            record_source_status(conn, feed, "error", 0, str(ex)[:300])
            summary["errors"].append({"id": feed["id"], "error": str(ex)[:200]})

    if send_alerts and all_new:
        alert_items = [n for n in all_new if n["watch_hits"] > 0 or n["score"] >= ALERT_SCORE]
        if alert_items:
            try:
                from alerts import dispatch_alerts
                summary["alerts_sent"] = dispatch_alerts(alert_items)
                for n in alert_items:
                    conn.execute("UPDATE articles SET alerted=1 WHERE link=?", (n["link"],))
                conn.commit()
            except Exception as ex:
                summary["errors"].append({"id": "alerts", "error": str(ex)[:200]})

    set_meta(conn, "last_poll_ts", int(time.time()))
    conn.close()
    return summary


# --------------------------------------------------------------------------- #
# Reads / search
# --------------------------------------------------------------------------- #
def _placeholders(values):
    return ",".join("?" for _ in values)


def query_articles(conn, *, text="", sources=None, categories=None,
                   since_ts=None, until_ts=None, watch_only=False,
                   unread_only=False, starred_only=False, priority_only=False,
                   order="published_ts DESC", limit=200):
    where, params = [], []
    if text:
        where.append("(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)")
        like = f"%{text.lower()}%"
        params += [like, like]
    if sources:
        where.append(f"source_id IN ({_placeholders(sources)})")
        params += list(sources)
    if categories:
        where.append(f"category IN ({_placeholders(categories)})")
        params += list(categories)
    if since_ts is not None:
        where.append("(published_ts >= ? OR fetched_ts >= ?)")
        params += [since_ts, since_ts]
    if until_ts is not None:
        where.append("published_ts <= ?")
        params.append(until_ts)
    if watch_only:
        where.append("watch_hits > 0")
    if unread_only:
        where.append("read = 0")
    if starred_only:
        where.append("starred = 1")
    if priority_only:
        where.append(f"(watch_hits > 0 OR score >= {ALERT_SCORE})")

    sql = "SELECT * FROM articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def counts(conn):
    c = {}
    c["total"] = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    c["unread"] = conn.execute("SELECT COUNT(*) FROM articles WHERE read=0").fetchone()[0]
    c["starred"] = conn.execute("SELECT COUNT(*) FROM articles WHERE starred=1").fetchone()[0]
    c["priority"] = conn.execute(
        f"SELECT COUNT(*) FROM articles WHERE watch_hits>0 OR score>={ALERT_SCORE}"
    ).fetchone()[0]
    day_ago = int(time.time()) - 86400
    c["today"] = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE COALESCE(published_ts, fetched_ts) >= ?", (day_ago,)
    ).fetchone()[0]
    return c


def get_sources_status(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY name")]


def fmt_stamp(row):
    """Prefer the feed's own published string; fall back to a tidy IST stamp."""
    if row.get("published_raw"):
        return row["published_raw"]
    ts = row.get("published_ts") or row.get("fetched_ts")
    if ts:
        return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %H:%M IST")
    return ""


def highlight(text, terms):
    """HTML-escape, then wrap any watchlist matches in <mark>."""
    safe = html.escape(text or "")
    terms = [t for t in (terms or []) if t and t.strip()]
    if not terms:
        return safe
    pat = re.compile("(" + "|".join(re.escape(t) for t in terms) + ")", re.I)
    return pat.sub(r"<mark class='mw-mark'>\1</mark>", safe)
