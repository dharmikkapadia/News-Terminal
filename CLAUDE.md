# MarketWire — project notes for Claude

A minimal, single-file Streamlit reader for **RBI Press Releases**
(`streamlit_app.py`): fetches one RSS feed server-side, shows it newest-first
with a keyword filter and a sidebar theme picker (data-terminal palettes).
No database, no scheduler.

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
- Themes are injected CSS keyed by palette. The open selectbox menu is portaled
  outside `.stApp`, so style dropdowns with **global** selectors (not
  `.stApp`-scoped) and `!important`, or light themes render dark-on-dark.
