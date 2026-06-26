# ◢ MarketWire

[![MarketWire history](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml/badge.svg)](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml)

A minimal Streamlit reader for **RBI Press Releases** and **RBI Notifications**.
It fetches each feed server-side (browsers can't read most RSS directly — CORS),
strips the HTML, **remembers items in a small SQLite store so the wire accumulates
over time**, and shows **both feeds together** with a keyword filter, a **sort order**
toggle (newest-first / oldest-first), and an opt-in **date-range** filter.
Every item is tagged with its source — **RBI - Press Release** or
**RBI - Notifications** — and each feed keeps its own durable history. A sidebar
**Sources** filter lets you keep all feeds or pick one/some individually (the choice
is remembered in the URL via `?sources=…`, so it's shareable).

### History store

RBI's RSS only carries the latest ~10, and replaces them as new ones publish.
`store.py` keeps every item the app has fetched (press releases deduped by `prid`,
notifications by `Id`) in a SQLite file — one table per feed, so the ids never
collide — so the list **grows over time** and the app **still shows stored history
even if a live fetch fails**. The DB path is the `MARKETWIRE_DB` env var (default
`marketwire.db` beside the app).

⚠️ **Streamlit Cloud caveat:** Community Cloud storage is **ephemeral** — a sqlite
file accumulates while the app is awake but **resets when it sleeps / redeploys**.
On an always-on **VM**, a plain sqlite path is already durable. For durable history
**on Cloud with no external database**, use the in-repo history below; or point
`MARKETWIRE_DB` at a hosted Postgres / Turso DB (further below).

### Durable history in the repo (GitHub Action, no external DB)

History can live **in this repo** instead of an external database. A scheduled
GitHub Action (`.github/workflows/history.yml`) runs the poller, which fetches both
RBI feeds and writes **`data/history.jsonl`** (press releases) and
**`data/notifications.jsonl`** (notifications) — JSON-lines so each update is a
small git diff — then commits them. The app reads those committed files and merges
each with its live feed — so history survives Streamlit Cloud restarts with no DB.

- The workflow runs **independently of the app** — it keeps building history even
  while the Streamlit app is asleep, and only commits when something changed. Each
  run bills as ~1 Actions minute on a private repo (free tier: 2,000 min/month).

#### Reliable 30-min cadence (external cron)

GitHub's own scheduler is **best-effort and unreliable** — a `*/30` cron here actually
fired only ~every 90 min (GitHub drops most frequent scheduled runs). So the workflow's
built-in `schedule` is just a sparse **6-hour fallback**; the primary cadence comes from
an **external cron** that calls the `workflow_dispatch` API every 30 min:

1. Create a **fine-grained PAT** (GitHub → Settings → Developer settings → Fine-grained
   tokens): scope it to **only this repo**, permission **Actions: Read and write**
   (Metadata: read is automatic). Set an expiry and save the token.
2. In a free cron service (e.g. [cron-job.org](https://cron-job.org)), add a job every
   30 min:
   - **URL:** `https://api.github.com/repos/dharmikkapadia/News-Terminal/actions/workflows/history.yml/dispatches`
   - **Method:** `POST`
   - **Headers:** `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`,
     `Authorization: Bearer <YOUR_PAT>`
   - **Body:** `{"ref":"main"}`
   - Expect HTTP **204** on success.

This gives a dependable 30-min cadence and bypasses GitHub's flaky scheduler. ~48
runs/day ≈ 1,460 min/month — within the free tier. (Security: the token lives in the
cron service, so keep it minimally scoped and rotate it on expiry.)
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

RBI's RSS feeds only carry the ~10 most recent items each. For **both** feeds the
poller (`poll.py`, run by the Action) also scrapes RBI's **listing** page —
press releases from `BS_PressReleaseDisplay.aspx` (keyed by `?prid=`), notifications
from `NotificationUser.aspx` (keyed by `?Id=`) — and, for each item it doesn't
already have, fetches the **detail page** to recover the **full body + date**
(`rbi_archive.fetch_detail`), capped at `MARKETWIRE_ENRICH_LIMIT` (default 120) per
feed per run. Detail-link matching is done on each link's **resolved absolute URL**,
so RBI's relative listing hrefs (e.g. `?Id=123&Mode=0`) resolve correctly; body
extraction tries `<div class="text1">` (press releases) and the notification body
cell, falling back to the largest text block. For **notifications** the scraper also
**follows the listing's per-year navigation links** (an `<a>` whose text is a 4-digit
year), one level deep, so history is walked back **year by year** rather than just
the latest page — all pages deduped by `Id`. So backfilled items end up with a date
and a full summary — they just lack a precise **time** (RBI's HTML doesn't expose
one), so they carry a date-only stamp and the app shows them without a misleading
`00:00`. Older, date-only items show an **ARCHIVE** tag and can be hidden with the
sidebar **“Show archive”** toggle.

Everything is deduped by `prid` / `Id` and **isolated/non-fatal** — if scraping is
blocked or the markup changes, you still get the RSS view. Run the scraper yourself
with `python rbi_archive.py` (or `python rbi_archive.py https://www.rbi.org.in/Scripts/NotificationUser.aspx`
for notifications — it auto-selects the right matcher). For deeper history, add RBI
month/year listing URLs (comma-separated) via the `MARKETWIRE_ARCHIVE_URLS` (press) /
`MARKETWIRE_NOTIFICATIONS_ARCHIVE_URLS` (notifications) env var / repo variable.

**Look & feel:** the app is laid out like a news website — a newspaper **masthead**
over your choice of two layouts (sidebar **Layout** toggle, remembered via `?layout=`):
- **Stream** (default) — a single-column feed (Trading Economics style): underlined
  headline (the link itself), right-aligned colour-coded source tag(s), a clamped
  body preview with an inline **Show more / Show less** toggle, and a relative
  timestamp ("16 minutes ago"), with hairline dividers.
- **Grid** — a uniform grid of story cards, each with a source tag, headline, a
  clamped summary preview, and a **Full text** expander.

Both tag every item with its colour-coded source, link the headline straight to RBI,
and use subtle fade-in/hover motion.

**Themes:** five flagship palettes in the sidebar — **Bloomberg** (dark amber),
**Reuters** (light orange), **Paper** (warm light), **Trading Economics** (light,
navy/blue data-site look with a sans headline font), and **High Contrast**
(black/yellow). Each theme carries its own headline font stack (`headfont` — serif
for the newspaper looks, sans for Trading Economics). Every palette is tuned so all
text stays legible (including portaled overlays like dropdowns and the date-picker),
and your choice is remembered in the URL (`?theme=…`), so it sticks and is shareable.

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

Feeds:
- Press Releases: `https://rbi.org.in/pressreleases_rss.xml` — override with `MARKETWIRE_FEED`.
- Notifications: `https://rbi.org.in/notifications_rss.xml` — override with `MARKETWIRE_NOTIFICATIONS_FEED`.

Point either env var at a mirror/cache (or a local file for testing) without code changes.
