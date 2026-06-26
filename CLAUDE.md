# MarketWire — project notes for Claude

A Streamlit reader for **RBI Press Releases** and **RBI Notifications**
(`streamlit_app.py`): fetches both RSS feeds server-side and shows them **together**
in one wire with a keyword filter, a **sort order** toggle (newest/oldest first), an
opt-in **date-range** filter, and a sidebar **Sources** multiselect (show all feeds
or pick individually), each item tagged with its source
(`RBI - Press Release` / `RBI - Notifications`), plus a theme picker. The UI is laid
out like a news website: a serif **masthead** over a uniform **card grid** (`st.columns`
+ bordered `st.container`s) rendered by `_story_card_html`, summary preview + a
**Full text** expander per card, subtle fade-in/hover CSS, and four flagship themes
(Bloomberg, Reuters, Paper, High Contrast). Durable history accumulates per feed
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
