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
market trends). It reads `data/rates.json` via `rates.py`. Durable history accumulates per feed
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
  separate **daily** GitHub Action (`.github/workflows/rates.yml`, midnight IST = `30 18 * * *`
  UTC) runs `rates.poll_rates()` via `python rates.py`; it scrapes the home page but writes
  **only on a complete + in-bounds parse** (`rates._is_complete`), so a blocked/partial
  scrape can never clobber the manual snapshot, and the MPC block (not on the home page)
  is preserved. (Kept out of `poll.py`/the 30-min history cron on purpose — rates refresh
  once a day.) The scraper was written WITHOUT live RBI access — validate it from a host
  that can reach the site (like `rbi_archive.py`).
