#!/usr/bin/env python3
"""verify_feeds.py — probe every configured feed and print a status table.

Run this from a machine that can actually reach the feed hosts (your desk or the
deployment VM). Government / exchange / news sites routinely block datacenter IPs,
so results from a cloud sandbox are NOT representative — RBI itself will 403 there.

Unlike the poller it does NOT write to the database or send alerts; it only fetches
and reports. That makes it safe to run anytime, including against feeds that are
disabled in feeds.py — use it to confirm a URL before you flip enabled=True.

  python verify_feeds.py            # probe every feed (enabled and disabled)
  python verify_feeds.py --enabled  # only the feeds that are enabled

Exit code is non-zero if any *enabled* feed fails, so a scheduler/CI can flag it.
"""

import argparse
import sys
import time

from feeds import FEEDS
from core import fetch_source


def probe(feed):
    """Return (ok, item_count, seconds, detail) for one feed."""
    t0 = time.time()
    try:
        items = fetch_source(feed)
        dt = time.time() - t0
        sample = items[0]["title"][:60] if items else "(0 entries — reachable but empty)"
        return True, len(items), dt, sample
    except Exception as ex:
        dt = time.time() - t0
        return False, 0, dt, f"{type(ex).__name__}: {ex}"[:90]


def main():
    ap = argparse.ArgumentParser(description="Probe MarketWire feeds and report status (no DB writes).")
    ap.add_argument("--enabled", action="store_true", help="only probe feeds with enabled=True")
    args = ap.parse_args()

    feeds = [f for f in FEEDS if f.get("enabled", True)] if args.enabled else FEEDS
    scope = " (enabled only)" if args.enabled else " (all, incl. disabled)"
    print(f"Probing {len(feeds)} feed(s){scope}…\n")
    print(f"{'STATUS':6} {'ENABLED':7} {'ITEMS':5} {'TIME':6} {'ID':10} DETAIL")
    print("-" * 100)

    enabled_failures = 0
    for f in feeds:
        ok, n, dt, detail = probe(f)
        status = "OK" if ok else "FAIL"
        en = "yes" if f.get("enabled", True) else "no"
        print(f"{status:6} {en:7} {n:<5} {dt:>4.1f}s  {f['id']:10} {detail}")
        if not ok and f.get("enabled", True):
            enabled_failures += 1

    print()
    if enabled_failures:
        print(f"{enabled_failures} ENABLED feed(s) failed — fix the URL in feeds.py or set enabled=False.")
    else:
        print("All probed enabled feeds are reachable.")
    return 1 if enabled_failures else 0


if __name__ == "__main__":
    sys.exit(main())
