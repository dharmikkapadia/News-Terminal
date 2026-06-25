#!/usr/bin/env python3
"""poller.py — fetch all feeds, store new items, send alerts. Run on a schedule.

This is the always-on half of MarketWire. Streamlit can't reliably run jobs when
no one has the page open, so a scheduler runs this every few minutes:

  cron:            */5 * * * *  cd /path/to/app && /path/to/venv/bin/python poller.py
  systemd timer:   see README
  GitHub Actions:  see .github/workflows/poll.yml

It writes to the same SQLite database the Streamlit app reads, and only alerts
once per item (the `alerted` flag prevents repeats).
"""

import sys
import time

from core import init_db, poll_all


def main():
    init_db()
    t0 = time.time()
    summary = poll_all(send_alerts=True)
    dt = time.time() - t0
    print(
        f"[poller] fetched={summary['fetched']} new={summary['new']} "
        f"alerts={summary.get('alerts_sent') or 'none'} "
        f"errors={len(summary['errors'])} in {dt:.1f}s"
    )
    for e in summary["errors"]:
        print(f"[poller]   ! {e['id']}: {e['error']}")
    # Non-zero exit if every feed failed, so schedulers can flag it.
    return 1 if summary["errors"] and summary["fetched"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
