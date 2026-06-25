# ◢ MarketWire

A minimal Streamlit reader for **RBI Press Releases**. It fetches the feed
server-side (browsers can't read most RSS directly — CORS), strips the HTML, and
shows it newest-first with a keyword filter. No database, no scheduler — just the
wire. More feeds can be added later.

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

Feed: `https://rbi.org.in/pressreleases_rss.xml`
