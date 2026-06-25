"""store.py — a tiny SQLite store so MarketWire remembers releases over time.

Without it the app only shows whatever is in RBI's RSS window right now (~10) and
forgets items as they roll off. With it, every release the app has ever fetched
is kept and accumulated, deduped by RBI prid (or link). It also makes the app
resilient: if a live fetch fails, the stored history is still shown.

DB path is the MARKETWIRE_DB env var (default: marketwire.db next to this file).

CAVEAT: on Streamlit Community Cloud the filesystem is EPHEMERAL — the DB
accumulates while the app is awake but resets when it sleeps / redeploys. For
durable history use an always-on VM, or point MARKETWIRE_DB at a hosted DB and
swap this sqlite3 layer for Postgres/Turso (the SQL is standard).
"""

import os
import re
import sqlite3
import time

DB_PATH = os.environ.get(
    "MARKETWIRE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "marketwire.db"),
)


def _key(item):
    """Stable identity: RBI prid if present in the link, else the link itself."""
    m = re.search(r"prid=(\d+)", item.get("link", ""), re.I)
    return m.group(1) if m else item.get("link", "")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # better read/write concurrency
        conn.execute(
            """CREATE TABLE IF NOT EXISTS articles (
                   key        TEXT PRIMARY KEY,
                   link       TEXT,
                   title      TEXT,
                   summary    TEXT,
                   published  TEXT,
                   ts         INTEGER,
                   fetched_ts INTEGER
               )"""
        )
        conn.commit()
    finally:
        conn.close()


def upsert(items):
    """Insert items we haven't stored; backfill a summary if we now have one.
    Returns the number of genuinely new rows (so the UI can show "N new")."""
    if not items:
        return 0
    now = int(time.time())
    new = 0
    conn = _conn()
    try:
        for it in items:
            k = _key(it)
            if not k:
                continue
            row = conn.execute("SELECT summary FROM articles WHERE key=?", (k,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO articles(key, link, title, summary, published, ts, fetched_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (k, it.get("link", ""), it.get("title", ""), it.get("summary", "") or "",
                     it.get("published", "") or "", it.get("ts"), now),
                )
                new += 1
            elif not row["summary"] and it.get("summary"):
                conn.execute("UPDATE articles SET summary=? WHERE key=?", (it["summary"], k))
        conn.commit()
    finally:
        conn.close()
    return new


def load(limit=1000):
    """All stored releases, newest first (by published date, then fetch time)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT link, title, summary, published, ts FROM articles "
            "ORDER BY COALESCE(ts, 0) DESC, fetched_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count():
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()
