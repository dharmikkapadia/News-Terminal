# ◢ MarketWire

A self-hosted RSS news terminal for an Indian-markets desk. Built for one job:
**let an equity analyst stay current without babysitting a dozen websites.**

It polls regulator, government, exchange, and news feeds *server-side* (so the
CORS wall that blocks browser-based readers doesn't apply), stores everything in
SQLite, scores each item by how much it should matter to you, and can push
alerts to Slack / Telegram / email.

---

## How it's put together

```
        feeds.py  ──►  ┌─────────────┐         ┌──────────────┐
   (your sources)      │  poller.py   │  write  │ marketwire.db │  read   ┌──────────┐
                       │ (scheduled)  ├────────►│   (SQLite)    │◄────────┤  app.py  │
   alerts.py  ◄────────┤ fetch+alert  │         └──────────────┘         │(Streamlit│
 (Slack/TG/email)      └─────────────┘                                    │  viewer) │
                                                                          └──────────┘
```

Two processes share one database:

- **`poller.py`** — the always-on half. A scheduler (cron / systemd / GitHub
  Actions) runs it every few minutes to fetch feeds, store new items, and fire
  alerts. It alerts once per item.
- **`app.py`** — the Streamlit viewer your team opens in a browser. It reads the
  same database and also has a **Fetch now** button for on-demand pulls.

Why split them? Streamlit can't reliably run background work when nobody has the
page open, so alerts that must fire at 2 a.m. belong in the poller.

---

## Quick start (local)

```bash
cd marketwire-streamlit
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python poller.py        # first fetch — populates the database
streamlit run app.py    # open the viewer at http://localhost:8501
```

That's enough to use it. Alerts and scheduling are configured below.

---

## The analyst features

- **Wire** — every item, newest first, with category tags and watchlist highlights.
- **Priority** — watchlist hits and high-scoring items only, sorted by impact. This
  is the triage view: open it first each morning.
- **Since last visit** — exactly what's new since you were last caught up. Hit
  *Mark all caught up* to reset your baseline.
- **Search** — full-text across *everything ever fetched* (titles + summaries),
  with source / category / date filters. Export results to CSV for your notes.
- **Watchlist** — edit live in the sidebar. Each term has a weight; matches get
  highlighted, scored up, and trigger alerts. Seeded with repo, CRR, QRSAM, BEL,
  REIT, InvIT, etc. — make it yours.
- **Sources** — per-feed health at last fetch, so you can see what's working.

Priority score = source weight + category weight + watchlist hits (headline
matches count extra) + a recency bonus. Tune the weights in `feeds.py`.

---

## Adding feeds

Open **`feeds.py`** and add a line to `FEEDS`:

```python
{"id": "rbi_pr", "name": "RBI · Press Releases",
 "url": "https://rbi.org.in/pressreleases_rss.xml", "weight": 4, "enabled": True},
```

`weight` nudges priority; `enabled` toggles it. Restart the app to pick up changes.

**About the shipped sources:** RBI Press Releases is confirmed working. SEBI, PIB,
and the news feeds are standard public endpoints, but government and
exchange sites occasionally change paths or block datacenter IPs. If one shows an
error in the **Sources** tab, that's expected — fix the URL or set `enabled: False`.
A failing feed never breaks the rest. (NSE in particular usually rejects
non-browser clients; leave it off unless you have a working endpoint. MCA ships
only an RSS *landing page*, not a direct `.xml`, so it's off by default.)

**Checking URLs without the UI:** run `python verify_feeds.py` to probe every feed
(enabled *and* disabled) and print a status table — no DB writes, no alerts. Run it
from your desk or the VM: these sites 403 datacenter IPs, so a cloud/CI runner will
report everything down, RBI included. `--enabled` limits it to enabled feeds and
exits non-zero if any fail.

---

## Alerts

Set any subset of these as environment variables (VM / GitHub Actions) or in
`.streamlit/secrets.toml` (Streamlit Cloud). Copy `.streamlit/secrets.toml.example`
to start. A channel only fires if its variables are present.

- **Slack:** `SLACK_WEBHOOK_URL`
- **Telegram:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- **Email:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM`

Alerts fire from the **poller** on new items that hit your watchlist (or score
≥ `MARKETWIRE_ALERT_SCORE`, default 9).

---

## Deploying for the team

### Option A — small always-on VM  *(recommended: full history + reliable alerts + one shared URL)*

Run the viewer as a service and the poller on a timer. With systemd:

```ini
# /etc/systemd/system/marketwire-web.service
[Service]
WorkingDirectory=/opt/marketwire
ExecStart=/opt/marketwire/.venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Restart=always
Environment=MARKETWIRE_DB=/var/lib/marketwire/marketwire.db
[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/marketwire-poll.service   (oneshot)
[Service]
Type=oneshot
WorkingDirectory=/opt/marketwire
ExecStart=/opt/marketwire/.venv/bin/python poller.py
Environment=MARKETWIRE_DB=/var/lib/marketwire/marketwire.db
EnvironmentFile=/opt/marketwire/alerts.env

# /etc/systemd/system/marketwire-poll.timer
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now marketwire-web.service marketwire-poll.timer
```

Put a reverse proxy (Caddy/Nginx) in front for HTTPS and, if you want, basic auth.
Or, with plain cron instead of systemd:

```
*/5 * * * *  cd /opt/marketwire && /opt/marketwire/.venv/bin/python poller.py >> /var/log/marketwire.log 2>&1
```

### Option B — Streamlit Community Cloud  *(fastest to share; good for a trial)*

1. Push this repo to GitHub (already done if you're reading this there).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **Create app** → **Deploy
   from GitHub**, sign in with GitHub, and authorize the repo.
3. Set:
   - **Repository:** your fork/repo
   - **Branch:** the branch you want to serve (e.g. `main`)
   - **Main file path:** `app.py`
   - **Python version** (Advanced): 3.11 or 3.12
4. *(Optional)* Open **Advanced settings → Secrets** and paste the keys you want,
   in TOML (the format mirrors [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example)):
   ```toml
   MARKETWIRE_ALERT_SCORE = "9"
   # SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/…"
   ```
   `app.py` mirrors these into the environment at startup, so the same keys work
   here and on a VM. Click **Deploy**. First build installs `requirements.txt`
   (~1–2 min); then hit **⟳ Fetch now** in the sidebar to populate the wire.

**Caveats (by design on the free tier):** Community Cloud storage is **ephemeral**
and the app **sleeps when idle** — so the SQLite history resets on restart and
*alerting does not run* (there's no always-on poller). It's great for a shared
read-only trial. For persistent history + reliable alerts, pair with Option C
(GitHub Actions poller) or a hosted DB (Turso / Postgres), or use Option A.
Also note these gov/exchange feeds often **403 from datacenter IPs** — some feeds
that work from your desk may show errors in the **Sources** tab on Cloud.

### Option C — GitHub Actions poller (no VM)

`.github/workflows/poll.yml` runs the poller every 15 minutes and sends alerts.
Add your alert keys under the repo's **Settings → Secrets → Actions**. It caches
the database between runs; GitHub cron is best-effort and the cache can be
evicted, so it's a convenience option, not a guarantee.

---

## Notes & easy next steps

- **Storage:** SQLite is plenty for a desk. To make a Cloud deployment durable or
  truly multi-user, point `MARKETWIRE_DB` at a hosted DB (swap the `sqlite3`
  connection in `core.py` for Postgres/Turso — the queries are standard SQL).
- **Read/star state** is currently shared across whoever opens the app (single
  desk). Per-user state would mean adding a user column + light auth.
- **Full-text search** uses `LIKE`; for large histories switch to SQLite FTS5.
- **Entity views, per-ticker dashboards, calendar extraction** (auction/settlement
  dates already sit in the text) are natural additions on top of this schema.

Confirmed-working seed feed: RBI Press Releases (`https://rbi.org.in/pressreleases_rss.xml`).
