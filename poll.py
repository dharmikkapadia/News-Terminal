#!/usr/bin/env python3
"""poll.py — fetch RBI (RSS + optional archive) and merge into data/history.jsonl.

Run by the GitHub Action (.github/workflows/history.yml) on a schedule, or by hand:

    python poll.py

It reads the existing history, fetches the feed (and any MARKETWIRE_ARCHIVE_URLS),
merges + dedupes, and rewrites data/history.jsonl. The Action then commits the file
so durable history lives in the repo — no external database.
"""

import os
import sys

import feed
import history
import rbi_archive


def _annotate(level, title, msg):
    """Emit a GitHub Actions annotation (read from stdout) so problems show on the
    run summary, not just buried in the log. Harmless when run locally (just prints)."""
    msg = str(msg).replace("\n", " ").replace("\r", " ")
    print(f"::{level} title={title}::{msg}")


def main():
    existing = history.load_file()

    rss, err = feed.fetch_rss(os.environ.get("MARKETWIRE_FEED", feed.RBI_FEED))
    if err:
        _annotate("warning", "RBI fetch failed", err)

    arch = []
    archive_urls = [u.strip() for u in
                    os.environ.get("MARKETWIRE_ARCHIVE_URLS", rbi_archive.LISTING_URL).split(",") if u.strip()]
    for url in archive_urls:
        got, aerr = rbi_archive.scrape_listing(url)
        if aerr:
            _annotate("warning", "Archive fetch failed", f"{url}: {aerr}")
        arch += got

    # Enrich archive stubs (the listing has title+date+link only) with the full
    # body from each release's detail page — only for items we don't already have
    # full text for, capped per run to bound requests. RBI's detail page exposes no
    # time, so enriched items keep a date-only (midnight) timestamp.
    have_full = {history._key(it) for it in existing + rss if (it.get("summary") or "").strip()}
    cap = int(os.environ.get("MARKETWIRE_ENRICH_LIMIT", "30"))
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

    before = len(existing)
    merged = history.dedupe(existing + rss + arch)
    history.save_file(merged)
    after = len(merged)
    print(f"[poll] existing={before} rss={len(rss)} archive={len(arch)} "
          f"enriched={enriched} total={after} new={after - before}")

    # Hard-fail (red run + failure email) only if we ended up with nothing at all.
    if after == 0:
        _annotate("error", "No data", "Fetched nothing and no stored history — check feed reachability.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
