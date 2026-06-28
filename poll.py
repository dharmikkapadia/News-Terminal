#!/usr/bin/env python3
"""poll.py — fetch RBI (RSS + optional archive) and merge into the repo history.

Run by the GitHub Action (.github/workflows/history.yml) on a schedule, or by hand:

    python poll.py

It polls BOTH RBI feeds — Press Releases (data/history.jsonl) and Notifications
(data/notifications.jsonl) — reading the existing history, fetching the RSS feed
(and any archive listing URLs), merging + deduping, and rewriting each file. The
Action then commits the files so durable history lives in the repo — no external
database.
"""

import os
import sys

import commodities
import feed
import history
import rbi_archive

# Each feed: how to fetch it (RSS), where to store it (JSONL), and how to backfill
# older items (listing URL + the href substring that marks a detail link). Env vars
# let a deploy override the feed/listing URLs without code changes.
FEEDS = [
    {
        "label": "press releases",
        "feed_url": os.environ.get("MARKETWIRE_FEED", feed.RBI_FEED),
        "history_path": history.HISTORY_PATH,
        "listing_url": rbi_archive.LISTING_URL,
        "href_match": rbi_archive.PRESS_HREF_MATCH,
        "archive_env": "MARKETWIRE_ARCHIVE_URLS",
        "follow_year_archives": False,
    },
    {
        "label": "notifications",
        "feed_url": os.environ.get("MARKETWIRE_NOTIFICATIONS_FEED", feed.RBI_NOTIFICATIONS_FEED),
        "history_path": history.NOTIFICATIONS_PATH,
        "listing_url": rbi_archive.NOTIFICATIONS_LISTING_URL,
        "href_match": rbi_archive.NOTIFICATIONS_HREF_MATCH,
        "archive_env": "MARKETWIRE_NOTIFICATIONS_ARCHIVE_URLS",
        # Walk notifications back through earlier years via the listing's per-year
        # navigation links, not just the latest page.
        "follow_year_archives": True,
    },
]


def _annotate(level, title, msg):
    """Emit a GitHub Actions annotation (read from stdout) so problems show on the
    run summary, not just buried in the log. Harmless when run locally (just prints)."""
    msg = str(msg).replace("\n", " ").replace("\r", " ")
    print(f"::{level} title={title}::{msg}")


def poll_feed(cfg):
    """Poll one feed (RSS + archive) and rewrite its history file. Returns the
    stored item count (0 means we have nothing at all for this feed)."""
    label = cfg["label"]
    existing = history.load_file(cfg["history_path"])

    rss, err = feed.fetch_rss(cfg["feed_url"])
    if err:
        _annotate("warning", f"RBI {label} fetch failed", err)

    arch = []
    # Note: an unset GitHub repo variable injects an EMPTY env var, so use `or`
    # (not the get() default) to fall back to the listing URL.
    archive_env = os.environ.get(cfg["archive_env"], "").strip() or cfg["listing_url"]
    archive_urls = [u.strip() for u in archive_env.split(",") if u.strip()]
    for url in archive_urls:
        got, aerr = rbi_archive.scrape_listing(
            url, href_match=cfg["href_match"],
            follow_year_archives=cfg.get("follow_year_archives", False))
        if aerr:
            _annotate("warning", f"{label} archive fetch failed", f"{url}: {aerr}")
        arch += got
    raw_archive = len(arch)

    # Enrich archive stubs (the listing has title+date+link only) with the full
    # body from each detail page — only for items we don't already have full text
    # for, capped per run to bound requests. RBI's detail page exposes no time, so
    # enriched items keep a date-only (midnight) timestamp.
    have_full = {history._key(it) for it in existing + rss if (it.get("summary") or "").strip()}
    cap = int(os.environ.get("MARKETWIRE_ENRICH_LIMIT", "120"))
    enriched = 0
    for a in arch:
        if enriched >= cap:
            break
        if (a.get("summary") or "").strip() or history._key(a) in have_full:
            continue
        det = rbi_archive.fetch_detail(a["link"], a.get("title", ""))
        if det and det.get("summary"):
            a["summary"] = det["summary"]
            if det.get("ts") is not None:
                a["ts"] = det["ts"]
            if det.get("published"):
                a["published"] = det["published"]
            enriched += 1

    arch = [a for a in arch if (a.get("summary") or "").strip()]  # store only full (enriched) archive items
    before = len(existing)
    merged = history.dedupe(existing + rss + arch)
    history.save_file(merged, cfg["history_path"])
    after = len(merged)
    print(f"[poll:{label}] existing={before} rss={len(rss)} archive={raw_archive} "
          f"enriched={enriched} stored_archive={len(arch)} total={after} new={after - before}")
    return after


def main():
    counts = [poll_feed(cfg) for cfg in FEEDS]
    # Refresh the commodity-price snapshot (data/commodities.json) on the SAME 30-min cadence
    # as history — commodity prices move intraday, so they want frequent updates (unlike the
    # once-a-day RBI Current Rates snapshot, data/rates.json, which stays on its own daily
    # workflow). Best-effort + self-guarding: commodities.poll_commodities() scrapes Yahoo,
    # writes only on a complete+in-bounds parse, and never raises — a blocked/partial fetch
    # just preserves the committed snapshot, so it can't fail the history run.
    try:
        print(f"[poll:commodities] {commodities.poll_commodities()}")
    except Exception as ex:                       # defensive — poll_commodities swallows errors
        _annotate("warning", "commodities refresh failed", ex)
    # NOTE: the Current Rates snapshot (data/rates.json) is refreshed on its OWN cadence —
    # once a day at 1:30pm IST (after RBI's 1pm FX update) by .github/workflows/rates.yml
    # (python rates.py) — not here.
    # Hard-fail (red run + failure email) only if EVERY feed ended up with nothing —
    # one blocked feed shouldn't fail the run while the other still has history.
    if not any(counts):
        _annotate("error", "No data", "Fetched nothing and no stored history — check feed reachability.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
