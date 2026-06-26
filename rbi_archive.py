#!/usr/bin/env python3
"""rbi_archive.py — best-effort scraper for RBI's Press Releases *listing* page,
to backfill releases beyond the ~10 the RSS feed exposes.

WHY "best-effort": it parses HTML from rbi.org.in, whose markup can change and
which blocks datacenter IPs. It was written WITHOUT live access to the site, so
VALIDATE IT FROM A MACHINE THAT CAN REACH RBI (your desk / VM):

    python rbi_archive.py                  # scrape the default listing, print rows
    python rbi_archive.py "<listing-url>"  # try an archive / month URL

It returns items in the same shape as the RSS reader (title, link, summary,
published, ts), so the app can merge them with the feed (deduped by prid/link).
On any failure it returns (items=[], error=str) and NEVER raises, so the app
keeps working on the RSS feed alone.

Heuristics (resilient to class/layout changes):
  - a press release is any <a> whose href contains "prid=" (the detail page);
    notifications are the <a>s whose href contains "notificationuser.aspx?id=";
  - its title is the link text; its date is the first date found by walking a
    few ancestors (handles both per-row dates and date-grouped sections).

Both RBI feeds (Press Releases + Notifications) share this scraper — pass the
listing URL and the matching `href_match` for the feed you want.
"""

import calendar
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False

LISTING_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
NOTIFICATIONS_LISTING_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx"
# Substring (lowercased href) that marks a detail link for each feed.
PRESS_HREF_MATCH = "prid="
NOTIFICATIONS_HREF_MATCH = "notificationuser.aspx?id="
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))

# Dates as RBI tends to show them, most specific first.
_DATE_PATTERNS = [
    ("%b %d, %Y", re.compile(r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}")),
    ("%B %d, %Y", re.compile(r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}")),
    ("%d %b %Y",  re.compile(r"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}")),
    ("%d %B %Y",  re.compile(r"\d{1,2}\s+[A-Z][a-z]+\s+\d{4}")),
    ("%d-%m-%Y",  re.compile(r"\d{1,2}-\d{1,2}-\d{4}")),
    ("%d/%m/%Y",  re.compile(r"\d{1,2}/\d{1,2}/\d{4}")),
]


def _parse_date(text):
    """Return (epoch_ts, matched_string) for the first date found, else (None, '')."""
    for fmt, rx in _DATE_PATTERNS:
        m = rx.search(text or "")
        if m:
            try:
                dt = datetime.strptime(m.group(0), fmt).replace(tzinfo=IST)
                return calendar.timegm(dt.utctimetuple()), m.group(0)
            except ValueError:
                continue
    return None, ""


def _key(link):
    """RBI press-release `prid` or notification `Id` from a detail link, else None."""
    m = re.search(r"\bprid=(\d+)", link, re.I) or re.search(r"\bid=(\d+)", link, re.I)
    return m.group(1) if m else None


def scrape_listing(url=LISTING_URL, timeout=20, href_match=PRESS_HREF_MATCH):
    """Scrape an RBI listing page for detail links. `href_match` is the lowercased
    href substring that marks a detail link (press releases vs notifications).
    Return (items, error). Never raises."""
    if not _HAVE_BS4:
        return [], "beautifulsoup4 not installed (pip install beautifulsoup4)"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"

    try:
        soup = BeautifulSoup(resp.content, "html.parser")
        items, seen = [], set()
        for a in soup.find_all("a", href=True):
            if href_match not in a["href"].lower():
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            if not title:
                continue
            link = requests.compat.urljoin(url, a["href"])
            key = _key(link) or link
            if key in seen:
                continue
            seen.add(key)
            ts, raw = None, ""
            node = a
            for _ in range(4):  # climb ancestors looking for a nearby date
                node = node.parent
                if node is None:
                    break
                # Drop the title text first, so a date inside the headline
                # (e.g. "Money Market Operations as on July 02") isn't mistaken
                # for the publication date in the adjacent cell.
                ctx = node.get_text(" ", strip=True).replace(title, " ", 1)
                ts, raw = _parse_date(ctx)
                if ts:
                    break
            items.append({"title": title, "link": link, "summary": "",
                          "published": raw, "ts": ts})
        items.sort(key=lambda x: x["ts"] or 0, reverse=True)
        return items, None
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


_DATE_LABEL_RE = re.compile(r"Date\s*:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", re.I)


def fetch_detail(url, title="", timeout=20):
    """Fetch one press-release DETAIL page and pull the full body + date.

    RBI puts the release in <div class="text1"> as "... Date : <date> <title> <body>"
    and exposes only a DATE (no time), so the returned ts is that date at midnight.
    Returns {"summary","published","ts"} or None. Never raises.
    """
    if not _HAVE_BS4:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return None
    try:
        soup = BeautifulSoup(resp.content, "html.parser")
        node = soup.find("div", class_="text1")
        if node is None:
            return None
        txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        published, ts = "", None
        m = _DATE_LABEL_RE.search(txt)
        if m:
            published = m.group(1)
            ts, _ = _parse_date(published)
            txt = txt[m.end():].strip()
        txt = re.sub(r"^\(\s*\d+\s*kb\s*\)\s*", "", txt)  # drop a leading "( 142 kb )"
        if title and txt.startswith(title):              # drop a duplicated headline
            txt = txt[len(title):].strip()
        if not txt:
            return None
        return {"summary": txt[:1500], "published": published, "ts": ts}
    except Exception:
        return None


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else LISTING_URL
    # Pick the right detail-link matcher from the listing URL (press vs notifications).
    href_match = NOTIFICATIONS_HREF_MATCH if "notification" in url.lower() else PRESS_HREF_MATCH
    kind = "notification" if href_match == NOTIFICATIONS_HREF_MATCH else "press-release"
    print(f"Scraping {url} …\n")
    items, error = scrape_listing(url, href_match=href_match)
    if error:
        print("ERROR:", error)
        sys.exit(1)
    for it in items[:50]:
        print(f"  {(it['published'] or '(no date)'):>18}  {it['title'][:80]}")
        print(f"  {'':18}  {it['link']}")
    dated = sum(1 for i in items if i["ts"])
    print(f"\n{len(items)} {kind} links found; {dated} with a parsed date.")
    if not items:
        print("Nothing parsed — the page structure may differ. Share a snippet "
              "of the listing HTML and the parser can be tuned.")
