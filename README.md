# ◢ MarketWire

[![MarketWire history](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml/badge.svg)](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml)

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
**on Cloud with no external database**, use the in-repo history below; or point
`MARKETWIRE_DB` at a hosted Postgres / Turso DB (further below).

### Durable history in the repo (GitHub Action, no external DB)

History can live **in this repo** instead of an external database. A scheduled
GitHub Action (`.github/workflows/history.yml`) runs the poller, which fetches RBI
and writes **`data/history.jsonl`** (deduped by `prid`, JSON-lines so each update is
a small git diff), then commits it. The app reads that committed file and merges it
with the live feed — so history survives Streamlit Cloud restarts with no DB.

- The Action runs every 30 minutes (and on demand via **Actions → Run workflow**),
  **independently of the app** — it keeps building history even while the Streamlit
  app is asleep. It only commits when something changed. Tune the `cron` in the workflow.
  At 30 min it uses ~1,460 Actions minutes/month, comfortably under the 2,000-min
  private-repo free tier. (Going faster on a private repo can exceed it — each run
  bills as a full minute — so make the repo **public** for free unlimited Actions first.)
- Run it yourself anytime: `python poll.py` (writes `data/history.jsonl`).
- **Trade-offs:** history grows at the Action's cadence (not instant — the *live*
  view is still real-time); and because each commit updates the tracked branch,
  Streamlit Cloud briefly **redeploys** when history changes. (A no-redeploy variant
  — commit to a side branch + read via a raw URL with `MARKETWIRE_HISTORY_URL` — is
  easy to switch to if the restarts bother you.)
- One unknown until the first run: RBI must answer GitHub's runner IPs (gov sites
  sometimes block datacenter IPs). If it 403s there, run the poller from your own
  machine / a self-hosted runner instead.

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

RBI's RSS feed only carries the ~10 most recent releases. The poller (`poll.py`,
run by the Action) also scrapes RBI's press-release **listing**
(`BS_PressReleaseDisplay.aspx`, releases keyed by `?prid=`) and, for each release it
doesn't already have, fetches the **detail page** to recover the **full body + date**
(`rbi_archive.fetch_detail`, from `<div class="text1">`), capped at
`MARKETWIRE_ENRICH_LIMIT` (default 30) per run. So backfilled items end up with a
date and a full summary — they just lack a precise **time** (RBI's HTML doesn't
expose one), so they carry a date-only stamp and the app shows them without a
misleading `00:00`. The app also does a light listing scrape live (the sidebar
**“Include archive”** toggle) for immediate display; un-enriched items show an
**ARCHIVE** tag until the poller fills in their body.

Everything is deduped by `prid` and **isolated/non-fatal** — if scraping is blocked
or the markup changes, you still get the RSS view. Run the scraper yourself with
`python rbi_archive.py`. For deeper history, add RBI month/year listing URLs
(comma-separated) via the `MARKETWIRE_ARCHIVE_URLS` env var / repo variable.

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
