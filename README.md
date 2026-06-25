# ◢ MarketWire

A minimal Streamlit reader for **RBI Press Releases**. It fetches the feed
server-side (browsers can't read most RSS directly — CORS), strips the HTML,
**remembers releases in a small SQLite store so the wire accumulates over time**,
and shows them newest-first with a keyword filter. More feeds can be added later.

### History store

RBI's RSS only carries the latest ~10, and replaces them as new ones publish.
`store.py` keeps every release the app has fetched (deduped by `prid`) in a SQLite
file, so the list **grows over time** and the app **still shows stored history
even if a live fetch fails**. The DB path is the `MARKETWIRE_DB` env var (default
`marketwire.db` beside the app).

⚠️ **Streamlit Cloud caveat:** Community Cloud storage is **ephemeral** — a sqlite
file accumulates while the app is awake but **resets when it sleeps / redeploys**.
On an always-on **VM**, a plain sqlite path is already durable. For durable history
**on Cloud**, point `MARKETWIRE_DB` at a hosted Postgres or Turso DB (below).

### Durable history: Postgres or Turso

`store.py` picks the backend from the `MARKETWIRE_DB` connection string — sqlite by
default, no config needed. To use a hosted DB, uncomment its driver in
`requirements.txt` and set the connection string (locally as an env var; on
Streamlit Cloud under **Settings → Secrets**, which the app mirrors into the env):

**Postgres** (Neon / Supabase / RDS) — uncomment `psycopg[binary]`:
```toml
# .streamlit/secrets.toml  (or env var)
MARKETWIRE_DB = "postgresql://USER:PASSWORD@HOST:5432/DBNAME"
```

**Turso** (libSQL) — uncomment `libsql-experimental`:
```toml
MARKETWIRE_DB = "libsql://YOUR-DB-ORG.turso.io"
MARKETWIRE_DB_AUTH_TOKEN = "your-turso-token"
```

The schema is created automatically on first run. All three backends were tested
for accumulate + dedupe + ordering; Postgres against a live server, Turso via the
libSQL driver (only the remote hop differs from the local check).

The wire **auto-refreshes every 5 minutes** on its own (no clicking) via a
Streamlit fragment, and there's a **⟳ Refresh** button for an immediate pull.
Override the interval with the `MARKETWIRE_REFRESH` env var (seconds).

### Beyond the latest 10 (archive backfill)

RBI's RSS feed only carries the ~10 most recent releases. To see more, toggle
**“Include archive”** in the sidebar: `rbi_archive.py` scrapes RBI's press-release
listing (`BS_PressReleaseDisplay.aspx`, releases keyed by `?prid=`) and merges the
older items in, deduped by `prid`. It's **isolated and non-fatal** — if scraping is
blocked or the page changed, you still get the RSS view.

⚠️ The scraper was written without live access to RBI (these pages 403 datacenter
IPs), so **validate it from a machine that can reach RBI**:

```bash
python rbi_archive.py            # prints what it parsed from the listing
```

If it finds nothing, RBI's markup likely differs — share a snippet of the listing
HTML and the selectors can be tuned. For deeper history, find RBI's month/year
archive URLs and add them (comma-separated) via the `MARKETWIRE_ARCHIVE_URLS` env var.

**Themes:** pick a data-terminal palette in the sidebar — Bloomberg, Reuters
Carbon, Amber/Green phosphor, Ice (cyan), a high-contrast light **Paper**, or
**High Contrast**. Every palette is tuned so all text stays legible, and your
choice is remembered in the URL (`?theme=…`), so it sticks and is shareable.

## Run locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py    # opens at http://localhost:8501
```

## Deploy on Streamlit Community Cloud

1. Push this repo to GitHub.
2. At [share.streamlit.io](https://share.streamlit.io) → **Create app → Deploy from GitHub**.
3. Set **Main file path** to `streamlit_app.py` and **Deploy**.

> Note: government sites occasionally block datacenter IPs. The RBI feed works
> from a normal desk/VM but may 403 from Streamlit Cloud — if so, run it locally.

Feed: `https://rbi.org.in/pressreleases_rss.xml` — override with the
`MARKETWIRE_FEED` env var to point at a mirror/cache without code changes.
