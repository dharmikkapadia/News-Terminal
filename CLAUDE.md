# MarketWire — project notes for Claude

A Streamlit reader for **RBI Press Releases** (`streamlit_app.py`): fetches the
RSS feed server-side and shows it newest-first with a keyword filter and a sidebar
theme picker (data-terminal palettes). Durable history accumulates via `store.py`
(SQLite/Postgres/Turso) **and** an in-repo `data/history.jsonl` maintained by a
scheduled GitHub Action running `poll.py` (every 30 min). `rbi_archive.py` backfills
older releases from RBI's listing/detail pages; `feed.py` is the shared RSS parser.

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
- Themes are injected CSS keyed by palette. Streamlit **portals overlays**
  (selectbox dropdowns, help `?` tooltips, popovers) outside `.stApp`, so
  `.stApp`-scoped rules miss them and light themes render dark-on-dark. Style them
  with **global** selectors + `!important`, setting BOTH background and text
  colour — e.g. `[data-testid="stTooltipContent"]`, `[data-baseweb="popover"]`,
  `[role="option"]`. When adding any new hover/popup UI, restyle it for the themes.
