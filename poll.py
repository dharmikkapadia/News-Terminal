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


def main():
    existing = history.load_file()

    rss, err = feed.fetch_rss(os.environ.get("MARKETWIRE_FEED", feed.RBI_FEED))
    if err:
        print(f"[poll] RSS error: {err}", file=sys.stderr)

    arch = []
    for url in [u.strip() for u in os.environ.get("MARKETWIRE_ARCHIVE_URLS", "").split(",") if u.strip()]:
        got, aerr = rbi_archive.scrape_listing(url)
        if aerr:
            print(f"[poll] archive {url}: {aerr}", file=sys.stderr)
        arch += got

    before = len(existing)
    merged = history.dedupe(existing + rss + arch)
    history.save_file(merged)
    after = len(merged)
    print(f"[poll] existing={before} rss={len(rss)} archive={len(arch)} "
          f"total={after} new={after - before}")

    # Fail only if we ended up with nothing at all (so the Action can flag it).
    return 0 if after else 1


if __name__ == "__main__":
    sys.exit(main())
