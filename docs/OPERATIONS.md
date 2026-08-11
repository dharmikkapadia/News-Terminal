# MarketWire — Operations & Developer Guide

[![MarketWire history](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml/badge.svg)](https://github.com/dharmikkapadia/News-Terminal/actions/workflows/history.yml)

The product overview lives in the repo [README](../README.md); this guide covers how
MarketWire works under the hood and how to run, deploy, and operate it.

MarketWire is a Streamlit app (`streamlit_app.py`) that fetches **RBI Press Releases**
and **RBI Notifications** (RSS, server-side — browsers can't read most RSS directly due
to CORS) plus **SEBI's Public Issues listing** and **Trading Economics' news stream**,
split into **TE India News** and **TE World News** (all three scraped; none has an RSS
feed), strips the HTML,
**remembers items in a small SQLite store so the wire accumulates over time**,
and shows the feeds together with a keyword filter, a **sort order** toggle
(newest-first / oldest-first), and an opt-in **date-range** filter. Every item is tagged
with its source, and each feed keeps its own durable history. A sidebar **Sources**
filter lets you keep all feeds or pick one/some individually (the choice is remembered
in the URL via `?sources=…`, so it's shareable).

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
GitHub Action (`.github/workflows/history.yml`) runs the poller, which fetches every
feed and writes one JSON-lines file each — **`data/history.jsonl`** (press releases),
**`data/notifications.jsonl`** (notifications), **`data/sebi_public_issues.jsonl`**,
**`data/te_india_news.jsonl`** and **`data/te_world_news.jsonl`** — JSON-lines so each
update is a small git diff — then commits them. The app reads those committed files and
merges each with its live feed — so history survives Streamlit Cloud restarts with no DB.

- The workflow runs **independently of the app** — it keeps building history even
  while the Streamlit app is asleep, and only commits when something changed.

#### Actions minutes (public repo — free)

This repo is **public**, so GitHub Actions on standard runners is **free and
unmetered** — the 30-min cadence costs nothing. **History lesson, should the repo
ever go private again:** GitHub bills private-repo jobs **rounded up to the next
whole minute**, so run *count* matters more than run length — at a 30-min cadence
(~48 runs/day × ~2 billable min once the Scrapling browser install rode every run)
the workflow burned ~3,000 min/month, 150% of the private free tier's 2,000, and
exhausted the July 2026 cycle in ~20 days. If privating, drop the external cron to
hourly (≈1,200 min/month with the structure below) **and gate the Scrapling-browser
install back onto the 2-hourly `schedule` runs** (an `if: github.event_name ==
'schedule'` on the install step — how it ran until Aug 2026, when the investing.com
scrapes were moved onto every run so bonds + coffee refresh at the full 30-min
cadence; the scrapes no-op safely on runs without the browser and keep their
committed values). One robustness measure from that episode is kept regardless:

- both workflows carry a `timeout-minutes` cap so one hung scrape dies fast.

#### Reliable 30-min cadence (external cron)

GitHub's own scheduler is **best-effort and unreliable** — a `*/30` cron here actually
fired only ~every 90 min (GitHub drops most frequent scheduled runs). So the workflow's
built-in `schedule` is a best-effort **2-hour fallback**; the primary cadence comes
from an **external cron** that calls the
`workflow_dispatch` API every 30 min:

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

This gives a dependable 30-min cadence and bypasses GitHub's flaky scheduler.
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

### Trading Economics news stream (TE India News · TE World News)

Two more wire sources come off **[Trading Economics' stream](https://tradingeconomics.com/stream)**
(`te_stream.py`), selectable individually in the sidebar **Sources** filter and on by
default: **TE India News** (`data/te_india_news.jsonl`, tag `TE - India News`) and
**TE World News** (`data/te_world_news.jsonl`, tag `TE - World News`). They ride the same
30-min `poll.py` cron as history and are committed by `history.yml`, and — like the RBI
and SEBI feeds — accumulate **without a cap**.

- **No documented feed, and TE is unreachable from CI**, so each feed is an ordered
  **candidate chain** rather than one URL: TE's stream XHR endpoint
  (`/ws/stream.ashx?start=0&size=50`), the server-rendered `/stream` page, and an RSS
  mirror; India's chain adds its country-filtered variants and ends with the plain global
  stream **filtered to India-tagged items here**, so it produces something whatever TE
  does with its country URLs. Each candidate is fetched once, the response is **sniffed**
  (JSON / RSS / HTML) and handed to the matching parser, and the **first one that yields
  items wins** — a wrong guess in the chain costs a request, not the feed. Override either
  chain (comma-separated) with `MARKETWIRE_TE_INDIA_STREAM_URL` /
  `MARKETWIRE_TE_WORLD_STREAM_URL`; the file paths with `MARKETWIRE_TE_INDIA_NEWS_FILE` /
  `MARKETWIRE_TE_WORLD_NEWS_FILE`; the app's read-from-URL with
  `MARKETWIRE_TE_INDIA_NEWS_URL` / `MARKETWIRE_TE_WORLD_NEWS_URL`.
- **India matching** (`_is_india`): TE's own country tag, else the item's country page
  (`/india/…` — the most reliable signal, since stream items link to indicator pages),
  else a whole-word "India/Indian" in the **headline** only (body text name-drops far too
  many countries).
- **Identity:** TE items carry no stable public id across those three shapes and TE points
  many headlines at the same indicator page, so each item sets its own
  **`key` = `te:<sha1(link|title)>`** — identical whichever candidate served it, and stable
  as an item's printed time ages from "2 minutes ago" to a date. `common.item_key()` honours
  an explicit `key`; `history.save_file()` writes one only when the source set it (so the
  RBI/SEBI files keep their exact shape) and `store.load()` returns the stored key, so
  identity round-trips through both history backends. Trade-off: an identical headline on
  the identical page on a later day merges into the first — rare, since TE headlines carry
  the numbers.
- **Timestamps:** relative stamps ("14 minutes ago") are timezone-free and exact; absolute
  dates are read as IST like the rest of the wire. `history.dedupe()` keeps the
  **first-seen** ts, so an item polled within 30 minutes of publication keeps that accurate
  stamp forever, and `published` is rewritten to an absolute IST string as soon as a ts
  resolves — a stale "2 hours ago" is never frozen into the history file. TE labels are in
  `_DATE_ONLY_SOURCES`, so a midnight stamp never mislabels one **ARCHIVE** (that badge
  means "backfilled from an RBI listing").
- **Guarded + non-fatal:** every entry point returns `(items, error)` and never raises; an
  empty or blocked fetch simply leaves the committed history untouched. There's no archive
  backfill — TE serves a rolling window only.
- **Written without live TE access** (this sandbox and GitHub runners are Cloudflare- and
  datacenter-IP-blocked): the three parsers are fixture-tested in `tests/test_te_stream.py`
  and the first live Action run is the integration test. Schedule runs upload each
  candidate's raw body as the **`te-stream-dump`** artifact (`MARKETWIRE_TE_STREAM_DUMP`,
  one file per candidate) — use it to see which shape TE actually served, then pin the feed
  to that URL via the env override. Validate by hand from a host that can reach TE:
  `python te_stream.py`.

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
  [`prompts/rbi-rates-refresh.md`](../prompts/rbi-rates-refresh.md).
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
  30-min history poller (RBI rates change once a day). GitHub's scheduler is best-effort; for exact
  timing, also point an external cron at either workflow's `workflow_dispatch`. Like `rbi_archive.py`,
  the scrapers need validating from a host that can reach RBI.

The dashboard renders in the app's single **Trading Economics** palette — monospace
numerics, green/red gain-loss tones.

### Commodities strip (free prices · % vs prev close · chart links)

Below the rates panel, an opt-in **Commodities** strip (sidebar **Show commodities**) shows
**Brent, Gold, Silver, Copper, Aluminium, Zinc, Steel (HRC), Iron Ore, Coffee (London Robusta)
and the
Containerized Freight Index** (the weekly Shanghai SCFI composite, quoted in points) — each as a
tile with the price, the **% change vs its previous close** (coloured with the palette's
`up`/`down` gain/loss tones), and a **direct chart link** (the whole tile opens the commodity's
[Trading Economics](https://tradingeconomics.com/commodities) page in a new tab; the Coffee tile
opens its [investing.com](https://www.investing.com/commodities/london-coffee) page).

It reads a committed **`data/commodities.json`** (`commodities.py`), refreshed the same guarded
way as the rates snapshot:
- **Source — all free, no paid key: Trading Economics primary, Yahoo Finance fallback; Coffee from
  investing.com.** Prices and
  the **% change vs previous close** come from **Trading Economics' server-rendered commodities
  table** (`tradingeconomics.com/commodities`) — for a logged-out visitor the price, net change and
  percent change are baked into each row's markup (`tr[data-symbol]` → `td#p`/`td#nch`/`td#pch`),
  no key or JavaScript needed. TE covers every TE-sourced tile **including Zinc** (`LMZSDS03:COM`)
  and the
  **Containerized Freight Index**, and gives a broker-grade % change, so we don't compute one. If TE
  is blocked / rate-limited / drops a symbol, we fall back to **Yahoo Finance's keyless chart
  endpoint** (`…/v8/finance/chart/<symbol>`, daily closes → `(last − prev)/prev`) for the 7 it
  covers. **Steel** is pinned to Yahoo (`HRC=F`, a USD HR-coil benchmark) on purpose — TE's steel
  (`JBP:COM`) is Chinese rebar in CNY/T. Yahoo is only queried for the symbols that actually need it
  (Steel + any TE gaps), so a healthy TE run hits Yahoo once. Chart links are Trading Economics'
  public per-commodity pages (Coffee: its investing.com page). TE rows are matched by `data-symbol`,
  falling back to the row's
  `/commodity/<slug>` name link (`common.fetch_te_table`'s `want_slug`) — the freight index's
  symbol (`SHSPSCFI:IND`) was written without live TE access and is unverified, but its slug
  (`containerized-freight-index`) is its verified public URL, so the row resolves either way. It's
  marked `cadence: "weekly"` (SCFI prints weekly) and, like Zinc, is TE-only: no free Yahoo series,
  so a TE miss preserves its last committed value. (Note: TE's logged-out page
  serves last-settled values, so prices can lag the live intraday tick until TE's next server rebuild
  — fine for a 30-min poll; verified to be current, not stale.)
- **Coffee — investing.com's London (Robusta) coffee futures page**
  ([investing.com/commodities/london-coffee](https://www.investing.com/commodities/london-coffee),
  quoted in **USD/tonne**), replacing Trading Economics' US Arabica (`KC1:COM`, USc/lb). It has
  **no TE/Yahoo fallback** — their coffee is a different series AND unit. investing.com blocks bots
  and renders client-side, so `commodities.fetch_investing()` fetches the page with the **shared
  stealth-browser render** (`common.stealth_render` — the same investing.com-tuned Scrapling fetch,
  xvfb + `MARKETWIRE_SCRAPE_PROXY` story as the bond scrape), and — like the bond
  curve — the quote refreshes on **every `history.yml` run** (the browser installs on all runs
  since Aug 2026); a blocked/failed render preserves the last committed price (marked `stale`).
  `parse_investing_quote()` reads the instrument page's price header by `data-test` attribute
  (`instrument-price-last` / `instrument-price-change-percent` / `prevClose`, with the legacy
  `#last_last` ids as fallback). Written without live investing.com access — each run uploads
  the rendered HTML as the `coffee-render-dump` artifact (`MARKETWIRE_COFFEE_DUMP`) for parser
  tuning, and `MARKETWIRE_INVESTING_COFFEE_URL` overrides the page URL.
- **Automated (best-effort):** commodities ride the **same 30-min poller as history** — `poll.py`
  calls `commodities.poll_commodities()` each run and `.github/workflows/history.yml` commits
  `data/commodities.json` alongside the history files. (Prices move intraday, so they want frequent
  updates — unlike the once-a-day RBI rates, which stay on their own `rates.yml`.) The refresh
  rewrites the file **only when the liquid core (Brent/Gold/Silver/Copper) resolves in-bounds from
  either source** — a blocked/rate-limited scrape leaves the committed snapshot untouched, and any
  symbol its source(s) miss (e.g. Zinc and Containerized Freight, which are TE-only, or Coffee
  on a blocked render) keeps its last
  committed price. The seed
  file ships with `null` prices; they fill on the next 30-min poll (or trigger `history.yml` via
  **workflow_dispatch** to populate now). Like
  `rates.py`, the scrapers were written without live
  market access — validate them from a machine that can reach TE / Yahoo (TE sits behind Cloudflare;
  Yahoo 403s some datacenter IPs). Note: committing on each price tick means more frequent commits
  during market hours (each a brief Streamlit Cloud redeploy) — the trade-off for fresher prices.

### Economic Calendar (India macro releases)

Below the commodities strip, an opt-in **Economic Calendar** (sidebar **Show economic
calendar**) shows India's macro release schedule: a signal strip of the next key events
(with consensus + importance stars) over an expandable full table — upcoming releases plus
the past week's **actuals coloured vs consensus** (▲ above / ▼ below — direction only, not
a judgement).

It reads a committed **`data/calendar.json`** (`econ_calendar.py` — not `calendar.py`,
which would shadow the stdlib module), refreshed on the same 30-min poller as history so
released actuals land promptly:
- **Source:** Trading Economics' server-rendered India calendar
  (`tradingeconomics.com/india/calendar`) — per-day date headers over `tr[data-url]` event
  rows with `actual`/`previous`/`consensus`/`forecast` spans and a `data-importance`
  rating. Override the URL via `MARKETWIRE_TE_CALENDAR`, the file path via
  `MARKETWIRE_CALENDAR_FILE`, or point the app at a raw URL via `MARKETWIRE_CALENDAR_URL`.
- **Accumulate + merge:** TE serves a rolling window, so each poll merges what it sees
  onto the committed events by id — new events are added, released actuals fill in, and
  events that rolled out of TE's window are kept until pruned (~14 days back / ~180 days
  ahead). The snapshot therefore builds the forward schedule over time.
- **Guarded:** the file is rewritten only on a sane parse (≥3 dated rows); a blocked or
  changed page keeps the committed snapshot. Times are stored exactly as TE prints them
  (TE localizes per-visitor, so no timezone is claimed). The seed ships with an empty
  `events` list — the app hides the section until the first scrape lands (or trigger
  `history.yml` via workflow_dispatch). For markup diagnosis, schedule runs upload the
  fetched HTML as the `calendar-fetch-dump` artifact (`MARKETWIRE_CALENDAR_DUMP`).

### Look & feel

The app is laid out like a news website — a newspaper **masthead**
over your choice of two layouts (sidebar **Layout** toggle, remembered via `?layout=`):
- **Stream** (default) — a single-column feed (Trading Economics style): underlined
  headline (the link itself), right-aligned colour-coded source tag(s), a clamped
  body preview with an inline **Show more / Show less** toggle, and a relative
  timestamp ("16 minutes ago"), with hairline dividers.
- **Grid** — a uniform grid of story cards, each with a source tag, headline, a
  clamped summary preview, and a **Full text** expander.

Both tag every item with its colour-coded source, link the headline straight to RBI,
and use subtle fade-in/hover motion.

**Palette:** a single fixed **Trading Economics**-inspired palette (`THEMES` in
`streamlit_app.py`) — soft-grey page, white cards, navy headlines, signal
blue/green/violet accents, a sans headline font (`headfont`), and `up`/`down`
gain/loss colours for the rates/commodities dashboards. There is no theme picker and
no `?theme=` URL param. The palette is tuned so all text stays legible, including
portaled overlays like dropdowns and the date-picker (see the CLAUDE.md gotcha on
Streamlit portaling).

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
