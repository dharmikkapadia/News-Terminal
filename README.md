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
  while the Streamlit app is asleep, and only commits when something changed.

#### Actions-minutes budget (private repo)

GitHub bills each job **rounded up to the next whole minute**, so on a private repo
run *count* matters more than run length: at a 30-min dispatch cadence (~48 runs/day,
~2 billable min each once the Scrapling browser install rode every run) the workflow
burned ~100 min/day ≈ 3,000 min/month — **150% of the free tier's 2,000 min** (the
July 2026 cycle exhausted in ~20 days; an earlier note here claiming 48 runs/day fit
the tier was wrong for exactly this rounding reason). The budget now holds because:

- the external cron fires **hourly**, not every 30 min (~24 dispatches/day);
- the heavy Scrapling-browser + xvfb install for the investing.com **bond scrape runs
  only on the 6-hourly `schedule` runs** (bonds no-op safely on dispatch runs and keep
  the committed curve), so dispatch runs stay ~1 billable minute;
- both workflows carry a `timeout-minutes` cap so one hung scrape can't burn hours.

That's roughly `24×1 + 4×3` ≈ **~40 min/day ≈ 1,200 min/month** including the daily
`rates.yml` — comfortable headroom. (Making the repo **public** would remove the
constraint entirely: standard-runner Actions minutes are free for public repos.)

#### Reliable hourly cadence (external cron)

GitHub's own scheduler is **best-effort and unreliable** — a `*/30` cron here actually
fired only ~every 90 min (GitHub drops most frequent scheduled runs). So the workflow's
built-in `schedule` is just a sparse **6-hour fallback**; the primary cadence comes from
an **external cron** that calls the `workflow_dispatch` API every hour (do NOT set it
faster — see the budget section above):

1. Create a **fine-grained PAT** (GitHub → Settings → Developer settings → Fine-grained
   tokens): scope it to **only this repo**, permission **Actions: Read and write**
   (Metadata: read is automatic). Set an expiry and save the token.
2. In a free cron service (e.g. [cron-job.org](https://cron-job.org)), add a job every
   hour:
   - **URL:** `https://api.github.com/repos/dharmikkapadia/News-Terminal/actions/workflows/history.yml/dispatches`
   - **Method:** `POST`
   - **Headers:** `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`,
     `Authorization: Bearer <YOUR_PAT>`
   - **Body:** `{"ref":"main"}`
   - Expect HTTP **204** on success.

This gives a dependable hourly cadence and bypasses GitHub's flaky scheduler.
(Security: the token lives in the cron service, so keep it minimally scoped and
rotate it on expiry.)
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

### Current Rates dashboard (equity desk)

Above the wire, an opt-in **Current Rates** panel (sidebar **Show rates dashboard**) gives
an equity-investor snapshot of RBI's home-page rates: a **signal strip** — Policy Repo, the
SDF/MSF LAF corridor, CRR/SLR, USD/INR, the ~10-year benchmark G-Sec, and a **next-MPC-meeting
countdown** ("Next MPC: Aug 3–5, 2026 · in 38 days") — over an expandable **full rate card**
(policy/reserve/exchange/lending-deposit rates and market trends: call money, G-Sec & T-bill
yields, Sensex/Nifty), each with its "as on" stamp.

It reads a committed **`data/rates.json`** (`rates.py`), refreshed two ways:
- **Manual (source of truth):** RBI 403s datacenter IPs and the rates box is a JS accordion,
  so a **Claude-for-Chrome** run on rbi.org.in is the reliable extractor — it emits the JSON
  (including the next MPC date, which isn't on the home page); commit it. A ready-to-paste,
  schedulable prompt that does this end-to-end (read RBI → merge onto the live file → commit,
  preserving the Trading-Economics FX overlay) lives at
  [`prompts/rbi-rates-refresh.md`](prompts/rbi-rates-refresh.md).
- **Automated (best-effort):** GitHub Actions refresh the snapshot on a **daily** cadence, across
  several slots from **early-afternoon to late-evening IST**. The first slot lands just after RBI's
  "1.00pm" FBIL FX update (so each run captures the same day's exchange rates; a midnight run would
  only get the prior day's); the later slots both give RBI time to post its **same-day** EOD
  G-Sec/Capital numbers and back up any run GitHub's best-effort scheduler delays or drops — the
  cause of a stale Market Trends panel when a single daily run went missing. Two guarded workflows
  share the job: `.github/workflows/rates.yml` runs `python rates.py` → `rates.poll_rates()` (a
  plain `requests` scrape), and `.github/workflows/rates-scrapling.yml` renders the page in a real
  browser (Scrapling) — a JS-executing **superset** that also reads the next MPC date. Both rewrite
  the file **only on a complete, in-bounds parse** (a blocked/partial scrape leaves the committed
  snapshot untouched, and the MPC block is preserved), deep-merged and commit-only-on-change, so the
  redundant runs are idempotent — whichever fires first wins, the rest no-op. It's kept off the
  hourly history poller (RBI rates change once a day). GitHub's scheduler is best-effort; for exact
  timing, also point an external cron at either workflow's `workflow_dispatch`. Like `rbi_archive.py`,
  the scrapers need validating from a host that can reach RBI.

This pairs with the new dark **Equity Terminal** theme (now the default) — charcoal page,
terminal-green press tags, amber notifications, monospace numerics.

### Commodities strip (free prices · % vs prev close · chart links)

Below the rates panel, an opt-in **Commodities** strip (sidebar **Show commodities**) shows
**Brent, Gold, Silver, Copper, Aluminium, Zinc, Steel (HRC), Iron Ore and Coffee** — each as a
tile with the price, the **% change vs its previous close** (coloured with the theme's
`up`/`down` gain/loss tones), and a **direct chart link** (the whole tile opens the commodity's
[Trading Economics](https://tradingeconomics.com/commodities) page in a new tab).

It reads a committed **`data/commodities.json`** (`commodities.py`), refreshed the same guarded
way as the rates snapshot:
- **Source — all free, no paid key: Trading Economics primary, Yahoo Finance fallback.** Prices and
  the **% change vs previous close** come from **Trading Economics' server-rendered commodities
  table** (`tradingeconomics.com/commodities`) — for a logged-out visitor the price, net change and
  percent change are baked into each row's markup (`tr[data-symbol]` → `td#p`/`td#nch`/`td#pch`),
  no key or JavaScript needed. TE covers all 9 **including Zinc** (`LMZSDS03:COM`) and gives a
  broker-grade % change, so we don't compute one. If TE is blocked / rate-limited / drops a symbol,
  we fall back to **Yahoo Finance's keyless chart endpoint** (`…/v8/finance/chart/<symbol>`, daily
  closes → `(last − prev)/prev`) for the 8 it covers. **Steel** is pinned to Yahoo (`HRC=F`, a USD
  HR-coil benchmark) on purpose — TE's steel (`JBP:COM`) is Chinese rebar in CNY/T. Yahoo is only
  queried for the symbols that actually need it (Steel + any TE gaps), so a healthy TE run hits Yahoo
  once. Chart links are Trading Economics' public per-commodity pages. (Note: TE's logged-out page
  serves last-settled values, so prices can lag the live intraday tick until TE's next server rebuild
  — fine for an hourly poll; verified to be current, not stale.)
- **Automated (best-effort):** commodities ride the **same hourly poller as history** — `poll.py`
  calls `commodities.poll_commodities()` each run and `.github/workflows/history.yml` commits
  `data/commodities.json` alongside the history files. (Prices move intraday, so they want frequent
  updates — unlike the once-a-day RBI rates, which stay on their own `rates.yml`.) The refresh
  rewrites the file **only when the liquid core (Brent/Gold/Silver/Copper) resolves in-bounds from
  either source** — a blocked/rate-limited scrape leaves the committed snapshot untouched, and any
  symbol both sources miss (e.g. Zinc, which is TE-only) keeps its last committed price. The seed
  file ships with `null` prices; they fill on the next hourly poll (or trigger `history.yml` via
  **workflow_dispatch** to populate now). Like `rates.py`, the scrapers were written without live
  market access — validate them from a machine that can reach TE / Yahoo (TE sits behind Cloudflare;
  Yahoo 403s some datacenter IPs). Note: committing on each price tick means more frequent commits
  during market hours (each a brief Streamlit Cloud redeploy) — the trade-off for fresher prices.

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

**Themes:** six flagship palettes in the sidebar — **Equity Terminal** (dark trading-desk,
the default), **Bloomberg** (dark amber), **Reuters** (light orange), **Paper** (warm light),
**Trading Economics** (light, navy/blue data-site look with a sans headline font), and
**High Contrast** (black/yellow). Each theme carries its own headline font stack (`headfont` —
serif for the newspaper looks, sans for the data-site looks) and `up`/`down` gain/loss colours
for the rates dashboard. Every palette is tuned so all
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
