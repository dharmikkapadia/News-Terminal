"""store.py — durable history for MarketWire, with pluggable backends.

The backend is chosen from the MARKETWIRE_DB connection string:

  - sqlite (default): a file path, e.g. `marketwire.db`  — ephemeral on Cloud
  - Postgres:  `postgres://USER:PASS@HOST:PORT/DBNAME`   — needs `psycopg`
  - Turso:     `libsql://DBNAME-ORG.turso.io`            — needs `libsql-experimental`
               (+ auth token in MARKETWIRE_DB_AUTH_TOKEN / TURSO_AUTH_TOKEN)

Every release the app fetches is kept (deduped by RBI prid, or link), so the wire
accumulates over time and survives a failed live fetch. The schema and SQL are
portable across all three; the Postgres/Turso drivers are optional and imported
lazily — install only the one you use.

For durable history on Streamlit Cloud (whose disk is ephemeral), point
MARKETWIRE_DB at a hosted Postgres (Neon/Supabase) or Turso DB via the app's
Secrets. On a VM, a plain sqlite file path is already durable.
"""

import os
import re
import sqlite3
import time

_CREATE = """CREATE TABLE IF NOT EXISTS articles (
    key        TEXT PRIMARY KEY,
    link       TEXT,
    title      TEXT,
    summary    TEXT,
    published  TEXT,
    ts         BIGINT,
    fetched_ts BIGINT
)"""


def _db_url():
    return os.environ.get(
        "MARKETWIRE_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "marketwire.db"),
    )


def _backend(url):
    u = url.lower()
    if u.startswith(("postgres://", "postgresql://")):
        return "postgres"
    if u.startswith("libsql://") or "turso.io" in u:
        return "turso"
    return "sqlite"


def _connect():
    """Return (conn, paramstyle). paramstyle is 'qmark' (?) or 'pyformat' (%s).

    All three drivers expose a sqlite3-style `conn.execute(sql, params)` that
    returns a cursor, plus `commit()`/`close()`, so the callers stay uniform.
    """
    url = _db_url()
    kind = _backend(url)
    if kind == "postgres":
        try:
            import psycopg  # psycopg 3
        except Exception as ex:
            raise RuntimeError(
                "MARKETWIRE_DB is a Postgres URL but psycopg isn't installed — "
                "pip install 'psycopg[binary]'"
            ) from ex
        return psycopg.connect(url), "pyformat"
    if kind == "turso":
        try:
            import libsql_experimental as libsql
        except Exception as ex:
            raise RuntimeError(
                "MARKETWIRE_DB is a Turso URL but libsql-experimental isn't installed — "
                "pip install libsql-experimental"
            ) from ex
        token = os.environ.get("MARKETWIRE_DB_AUTH_TOKEN") or os.environ.get("TURSO_AUTH_TOKEN", "")
        return libsql.connect(database=url, auth_token=token), "qmark"
    path = re.sub(r"^sqlite://", "", url)  # accept sqlite:///path or a bare path
    return sqlite3.connect(path, timeout=30), "qmark"


def _q(sql, style):
    """Translate ? placeholders to the backend's style."""
    return sql if style == "qmark" else sql.replace("?", "%s")


def _key(item):
    """Stable identity: RBI prid if present in the link, else the link itself."""
    m = re.search(r"prid=(\d+)", item.get("link", ""), re.I)
    return m.group(1) if m else item.get("link", "")


def init_db():
    conn, style = _connect()
    try:
        if _backend(_db_url()) == "sqlite":
            conn.execute("PRAGMA journal_mode=WAL")  # better read/write concurrency
        conn.execute(_q(_CREATE, style))
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
    conn, style = _connect()
    try:
        for it in items:
            k = _key(it)
            if not k:
                continue
            row = conn.execute(_q("SELECT summary FROM articles WHERE key=?", style), (k,)).fetchone()
            if row is None:
                conn.execute(
                    _q("INSERT INTO articles(key, link, title, summary, published, ts, fetched_ts) "
                       "VALUES (?,?,?,?,?,?,?)", style),
                    (k, it.get("link", ""), it.get("title", ""), it.get("summary", "") or "",
                     it.get("published", "") or "", it.get("ts"), now),
                )
                new += 1
            elif not row[0] and it.get("summary"):
                conn.execute(_q("UPDATE articles SET summary=? WHERE key=?", style), (it["summary"], k))
        conn.commit()
    finally:
        conn.close()
    return new


def load(limit=1000):
    """All stored releases, newest first (by published date, then fetch time)."""
    conn, style = _connect()
    try:
        rows = conn.execute(
            _q("SELECT link, title, summary, published, ts FROM articles "
               "ORDER BY COALESCE(ts, 0) DESC, fetched_ts DESC LIMIT ?", style),
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ("link", "title", "summary", "published", "ts")
    return [dict(zip(cols, r)) for r in rows]


def count():
    conn, _ = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()
