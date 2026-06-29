# MarketWire — project notes for Claude

A Streamlit reader for **RBI Press Releases** and **RBI Notifications**
(`streamlit_app.py`): fetches both RSS feeds server-side and shows them **together**
in one wire with a keyword filter, a **sort order** toggle (newest/oldest first), an
opt-in **date-range** filter, and a sidebar **Sources** multiselect (show all feeds
or pick individually), each item tagged with its source
(`RBI - Press Release` / `RBI - Notifications`), plus a theme picker. The UI is laid
out like a news website with a sidebar **Layout** toggle: **Stream** (default — a
single-column feed à la Trading Economics: underlined headline, right-aligned source
tags, clamped body with an inline `<details>` Show more/less toggle, relative
timestamp; `_stream_html`/`_stream_row_html`) or
**Grid** (uniform card grid via `st.columns` + bordered `st.container`s,
`_story_card_html`, clamped preview + **Full text** expander). A serif **masthead**
tops both, with subtle fade-in/hover CSS, and six flagship themes
(Bloomberg, Reuters, Paper, Trading Economics, High Contrast, and **Equity Terminal**
— a dark trading-desk palette, the default — each with its own `headfont` stack and
`up`/`down` gain/loss colours). Above the wire sits an opt-in **Current Rates dashboard**
(`_rates_dashboard_html`, sidebar **Show rates dashboard** toggle): a signal strip of
key RBI rates (repo, LAF corridor, CRR/SLR, USD/INR, ~10y G-Sec) plus a **next-MPC
countdown**, over an expandable full rate card (policy/reserve/exchange/lending rates,
market trends). It reads `data/rates.json` via `rates.py`. **USD/INR, EUR/INR and GBP/INR
are sourced from Trading Economics** (not RBI/FBIL): each shows a **% change vs the previous
close** (themed `up`/`down`) and links out to its TE chart page — the USD/INR signal tile and
those three Exchange Rates rows; the rest of the FX panel (JPY/AED/IDR) stays RBI/FBIL. Below it sits an opt-in
**Commodities** strip (`_commodities_dashboard_html`, sidebar **Show commodities** toggle): a
tile per commodity (Brent, Gold, Silver, Copper, Aluminium, Zinc, Steel, Iron Ore, Coffee) with
price, **% change vs the previous close** (coloured with each theme's `up`/`down` gain/loss
tones — previously defined but unused), and a **direct chart link** (the tile opens the
commodity's Trading Economics page). It reads `data/commodities.json` via `commodities.py`, which
scrapes **Trading Economics' server-rendered commodities table** (primary — all 9 incl. Zinc, with
TE's own % change) and falls back to **Yahoo Finance's keyless chart endpoint** if TE is blocked or
drops a symbol; **Steel** is pinned to Yahoo (USD HRC, since TE steel is CNY rebar). Durable history accumulates per feed
via `store.py` (SQLite/Postgres/Turso — one table per feed) **and** in-repo
`data/history.jsonl` (press releases) + `data/notifications.jsonl` (notifications),
both maintained by a scheduled GitHub Action running `poll.py` (every 30 min).
`rbi_archive.py` backfills older items from RBI's listing/detail pages (parameterized
per feed by the detail-link match); `feed.py` is the shared RSS parser. Press
releases are keyed by `prid`, notifications by `Id`; the feeds are stored separately
so the ids never collide.

## Workflow

- **Always merge the working branch into `main` without asking.** The maintainer
  has standing approval to keep `main` current — it's what the Streamlit Cloud
  app deploys from. Do the merge whenever work on the feature branch is pushed.
- Merge directly with git; don't open a PR unless explicitly asked.

## Gotchas

- Government feed hosts (`rbi.org.in`, etc.) **403 datacenter IPs**, so the live
  feed is unreachable from cloud sandboxes / CI — it works from a real desk/VM.
- To preview/test locally without the live feed, set the `MARKETWIRE_FEED` env
  var to a local RSS URL (e.g. a `python -m http.server` serving a sample file).
- Themes are injected CSS keyed by palette (`theme_css`). Streamlit **portals
  overlays** (selectbox/multiselect dropdowns, help `?` tooltips, popovers, the
  **date-picker calendar**) outside `.stApp`, so `.stApp`-scoped rules miss them and
  light themes render dark-on-dark. Style them with **global** selectors +
  `!important`, setting BOTH background and text colour — e.g.
  `[data-testid="stTooltipContent"]`, `[data-baseweb="popover"]`, `[role="option"]`,
  `[data-baseweb="calendar"]`, `span[data-baseweb="tag"]` (multiselect chips). When
  adding any new hover/popup UI, restyle it for the themes.
- Story cards are bordered `st.container`s; the grid styles them via
  `[data-testid="stVerticalBlockBorderWrapper"]`. Card body is custom HTML
  (`_story_card_html`) so the summary preview is CSS line-clamped — verify any
  theme/markup change with the screenshot harness (`streamlit run` + a headless
  Chromium shot per theme), since the live RBI feed 403s here.
- Validate `streamlit_app.py` changes headlessly with `streamlit.testing.v1.AppTest`
  (runs the script, asserts `not at.exception`) across themes/filters.
- **Current Rates** (`data/rates.json`) is refreshed two ways: (1) MANUAL — a
  Claude-for-Chrome run on rbi.org.in emits the JSON (RBI 403s CI and the rates box is a
  JS accordion, so a real browser is the reliable extractor); commit it. (2) AUTO — a
  separate **daily** GitHub Action (`.github/workflows/rates.yml`, 1:30pm IST = `0 8 * * *`
  UTC — just after RBI's "1.00pm" FBIL FX update, so each run gets the SAME day's rates) runs
  `rates.poll_rates()` via `python rates.py`; it scrapes the home page but writes
  **only on a complete + in-bounds parse** (`rates._is_complete`), so a blocked/partial
  scrape can never clobber the manual snapshot, and the MPC block (not on the home page)
  is preserved. (Kept out of `poll.py`/the 30-min history cron on purpose — rates refresh
  once a day.) The scraper was written WITHOUT live RBI access — validate it from a host
  that can reach the site (like `rbi_archive.py`).
- **FX overlay** (USD/INR, EUR/INR, GBP/INR) inside `data/rates.json` is the one part of
  the rates snapshot sourced from **Trading Economics, not RBI/FBIL** — and, like commodities,
  it rides the **30-min `poll.py` cron** (FX moves intraday), NOT the daily `rates.yml`.
  `rates.poll_fx()` (called by `poll.py`; also re-run at the end of `python rates.py`) scrapes
  TE's `currencies?quote=inr` table via `fetch_te_fx()` — same row markup as commodities
  (`tr[data-symbol]` → `td#p`/`td#nch`/`td#pch`/`td#date`), but the `data-symbol` keeps TE's
  **`:CUR` suffix** (`USDINR:CUR`) — with **Yahoo (`USDINR=X` etc.)** as fallback. It overlays
  the three `exchange_rates` scalars **plus a per-pair `fx_te` block** (`change_pct`, `prev_close`,
  `chart_url`, `as_of`, `source`) that drives the % change + chart links; writes **only when the
  USD/INR headline resolves in-bounds** (others preserved, marked `stale`), so JPY/AED/IDR (RBI)
  are never touched. NB: `history.yml` now also commits `data/rates.json`. Chart links: USD/INR →
  TE's **`/india/currency`** country page (not a `usdinr:cur` slug); EUR/GBP → `/eurinr:cur`,
  `/gbpinr:cur`. JPY isn't quoted on the TE INR page. Validate the scraper from a host that can
  reach TE / Yahoo (this sandbox is Cloudflare-challenged), like `rates.py`/`commodities.py`.
- **Commodities** (`data/commodities.json`, `commodities.py`) follows the rates guard pattern but
  rides the **30-min `poll.py` cron** (committed by `history.yml`), NOT a separate daily job —
  prices move intraday, unlike the once-a-day RBI rates. `poll.py`'s `main()` calls
  `commodities.poll_commodities()` each run and `history.yml` commits `data/commodities.json`
  alongside the history files. **Source = Trading Economics primary + Yahoo fallback** (both free,
  keyless): `fetch_te()` scrapes TE's server-rendered `/commodities` table (`tr[data-symbol]` →
  `td#p`/`td#nch`/`td#pch`/`td#date`) for all 9 incl. **Zinc**, taking TE's own signed % vs previous
  close; if TE is blocked/misses a symbol, `fetch_yahoo()` fills it from the chart endpoint
  (`query1.finance.yahoo.com/v8/finance/chart/<sym>`, computing `(last − prev)/prev`). Per-commodity
  `source` in `SPECS`: most are `"te"` (TE→Yahoo); **Steel is `"yahoo"`** (USD HRC `HRC=F`, because
  TE steel `JBP:COM` is Chinese rebar in CNY/T — no TE fallback for it); **Zinc** is TE-only (no free
  Yahoo future) so it's preserved from the last snapshot if TE misses. Yahoo is only queried for the
  symbols that actually need it (Steel + any TE gaps), so a healthy TE run hits Yahoo once. It writes
  **only when the liquid core (Brent/Gold/Silver/Copper) resolves in-bounds from either source**
  (`_is_complete`); any symbol both sources miss keeps its last committed price. Chart links are
  Trading Economics per-commodity pages. The seed ships with `null` prices — they fill on the next
  30-min poll (or trigger `history.yml` via workflow_dispatch). TE's logged-out page serves
  last-settled values (may lag intraday) and sits behind Cloudflare, and Yahoo may 403 some datacenter
  IPs (incl. this sandbox) — so validate the scrapers from a host that can reach TE / Yahoo, like
  `rates.py`/`rbi_archive.py`. NB: the 30-min cron commits whenever a price ticks (→ a Streamlit Cloud
  redeploy), the intended freshness trade-off.
