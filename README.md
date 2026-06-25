# ◢ MarketWire

A minimal Streamlit reader for **RBI Press Releases**. It fetches the feed
server-side (browsers can't read most RSS directly — CORS), strips the HTML, and
shows it newest-first with a keyword filter. No database, no scheduler — just the
wire. More feeds can be added later.

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
