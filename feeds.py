# feeds.py — your sources live here. Add/remove freely; the app reloads on restart.
#
# Each feed: id (unique slug), name (shown in UI), url (the RSS/Atom XML),
# weight (how much this source nudges an item's priority score), enabled.
#
# CONFIRMED WORKING: RBI press releases.
# The rest are standard public endpoints for Indian markets. Government/exchange
# sites occasionally change paths or block datacenter IPs — if one shows an error
# in the "Sources" tab, that's expected; fix or disable it there. Per-feed failures
# never crash the app.
#
# To check which URLs are live from THIS machine, run:  python verify_feeds.py
# (Run it from your desk/VM — these sites 403 datacenter IPs, so a cloud sandbox
# will report everything down, including RBI.)

FEEDS = [
    # --- Regulators / policy (highest signal for a markets desk) ---
    {"id": "rbi_pr",   "name": "RBI · Press Releases",  "url": "https://rbi.org.in/pressreleases_rss.xml",        "weight": 4, "enabled": True},
    {"id": "sebi",     "name": "SEBI · All",            "url": "https://www.sebi.gov.in/sebirss.xml",             "weight": 4, "enabled": True},   # sebirss.xml is the right file (RSS index: sebi.gov.in/rss.html); confirm reachability with verify_feeds.py
    # MCA publishes an RSS *landing page*, not a direct .xml — the feed links sit inside it and need a browser to read.
    # Landing pages seen: /content/mca/global/en/notifications-tender/rss-feeds.html  and  /mca/html/mcav2_en/home/home/more/newsandupdates/rssfeeds/rss.html
    # Open one in a browser, copy the actual .xml link into url, then flip enabled=True.
    {"id": "mca",      "name": "MCA · Updates",          "url": "https://www.mca.gov.in/content/mca/global/en/notifications-tender/rss-feeds.html", "weight": 2, "enabled": False},  # landing page, not a feed (see note above)

    # --- Government / sector (PIB carries defence, finance ministry, etc.) ---
    # PIB feeds are region/unit-scoped. Current scheme: ViewRss.aspx?reg=<unit>&lang=<1=English,2=Hindi>.
    # reg=1 = National / All-India English stream. Browse https://www.pib.gov.in/ViewRss.aspx to pick another unit.
    # (The old RssMain.aspx?ModId=..&Regid=.. form is dead — PIB migrated to www.pib.gov.in/ViewRss.aspx.)
    {"id": "pib",      "name": "PIB · Releases",         "url": "https://www.pib.gov.in/ViewRss.aspx?reg=1&lang=1", "weight": 3, "enabled": True},  # verify with verify_feeds.py

    # --- Exchanges (often need browser-like requests; may 403 from cloud IPs) ---
    # NSE blocks datacenter/non-browser clients hard (wants browser cookies). Official RSS index: https://www.nseindia.com/static/rss-feed
    {"id": "nse",      "name": "NSE · Corporate",        "url": "https://www.nseindia.com/api/rss/corporate",      "weight": 3, "enabled": False},  # unverified endpoint — grab a working URL from the RSS index above

    # --- Market news (context + flow; great for an equity analyst) ---
    {"id": "et_mkts",  "name": "ET · Markets",           "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "weight": 2, "enabled": True},   # verify
    {"id": "bs_mkts",  "name": "Business Standard · Markets", "url": "https://www.business-standard.com/rss/markets-106.rss", "weight": 2, "enabled": True},  # verify
    {"id": "mint_mkt", "name": "Mint · Markets",         "url": "https://www.livemint.com/rss/markets",            "weight": 2, "enabled": False},  # verify

    # --- More candidates to enable once verified (uncomment & confirm in Sources tab) ---
    # {"id": "rbi_notif", "name": "RBI · Notifications", "url": "https://rbi.org.in/notifications_rss.xml", "weight": 4, "enabled": False},
    # {"id": "rbi_speech","name": "RBI · Speeches",      "url": "https://rbi.org.in/speeches_rss.xml",      "weight": 2, "enabled": False},
    # {"id": "mc_news",   "name": "Moneycontrol · News",  "url": "https://www.moneycontrol.com/rss/latestnews.xml", "weight": 2, "enabled": False},
]

# How much each category contributes to an item's priority score.
# Tune to your desk: a policy decision matters more than a routine auction notice.
CATEGORY_WEIGHTS = {
    "Monetary Policy":   5,
    "Rates":             3,
    "Bank Supervision":  3,
    "Auctions / G-Sec":  1,
    "Data / Statistics": 1,
    "Press Release":     1,
}

# Seed watchlist. Edit live in the sidebar — this is just the starting set.
# term -> weight (how strongly a match boosts priority & triggers alerts).
DEFAULT_WATCHLIST = {
    "repo": 4,
    "CRR": 4,
    "monetary policy": 5,
    "policy rate": 5,
    "OMO": 3,
    "liquidity": 2,
    "QRSAM": 6,
    "BEL": 6,
    "defence": 4,
    "order book": 3,
    "REIT": 4,
    "InvIT": 4,
    "IPO": 3,
    "penalty": 4,
}
