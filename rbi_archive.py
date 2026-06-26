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
  - a press release is any <a> whose resolved URL contains "prid=" (the detail
    page); notifications are the <a>s carrying an "Id=" (never a "prid="), either an
    explicit NotificationUser.aspx URL or a bare same-page "?Id=..." query — matched
    on the absolute URL so relative listing hrefs like "?Id=123&Mode=0" still work;
  - notifications also follow the listing's per-year navigation links (text == a
    4-digit year) to walk back through earlier years, not just the latest page;
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
# A detail-link matcher is a predicate (raw_href, resolved_url_lower) -> bool.
# Matching the RESOLVED absolute URL — not the raw href — is what makes RBI's
# relative listing links (e.g. "?Id=123&Mode=0") work.
def _press_match(raw_href, resolved_low):
    """A press-release detail link — 'prid=' is unambiguous on the resolved URL."""
    return "prid=" in resolved_low


def _notif_match(raw_href, resolved_low):
    """A notification detail link: carries an 'Id=' (never a 'prid='), and is either
    an explicit NotificationUser.aspx URL or a bare same-page query ('?Id=...'). The
    bare-query case matters when following per-year archive pages whose own filename
    isn't NotificationUser.aspx, so their relative '?Id=' links wouldn't otherwise
    resolve to a NotificationUser.aspx URL."""
    if "prid=" in resolved_low or "id=" not in resolved_low:
        return False
    return "notificationuser.aspx" in resolved_low or raw_href.strip().startswith("?")


PRESS_HREF_MATCH = _press_match
NOTIFICATIONS_HREF_MATCH = _notif_match
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


_YEAR_TEXT_RE = re.compile(r"^(?:19|20)\d{2}$")  # an <a> whose text is just a year


def _year_archive_links(soup, base_url):
    """Resolved URLs of the listing's year-navigation anchors (text == a 4-digit
    year), skipping javascript:/# postbacks we can't GET. RBI's notification (and
    press-release) listings expose older items behind these per-year links."""
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if not _YEAR_TEXT_RE.match(a.get_text(strip=True)):
            continue
        href = a["href"].strip()
        if href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        url = requests.compat.urljoin(base_url, href)
        if url == base_url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _scrape_page(url, timeout, href_match):
    """Scrape ONE listing page. Returns (items, soup, error); never raises."""
    if not _HAVE_BS4:
        return [], None, "beautifulsoup4 not installed (pip install beautifulsoup4)"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], None, f"{type(ex).__name__}: {ex}"
    try:
        soup = BeautifulSoup(resp.content, "html.parser")
        items, seen = [], set()
        for a in soup.find_all("a", href=True):
            raw = a["href"]
            link = requests.compat.urljoin(url, raw)  # resolve relative hrefs
            if not href_match(raw, link.lower()):
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            if not title:
                continue
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
        return items, soup, None
    except Exception as ex:
        return [], None, f"parse error: {type(ex).__name__}: {ex}"


def scrape_listing(url=LISTING_URL, timeout=20, href_match=PRESS_HREF_MATCH,
                   follow_year_archives=False, max_pages=20):
    """Scrape an RBI listing page for detail links. `href_match` is a predicate
    (raw_href, resolved_url_lower) -> bool selecting detail links for the feed
    (PRESS_HREF_MATCH / NOTIFICATIONS_HREF_MATCH).

    With `follow_year_archives=True`, also follow the listing's per-year navigation
    links (one level deep, up to `max_pages` pages total) and merge their items —
    this is what walks notifications back through earlier years, not just the latest
    page. Items are deduped by key (prid / Id) across all pages. Returns
    (items, error); never raises."""
    items, soup, error = _scrape_page(url, timeout, href_match)
    if error:
        return [], error

    if follow_year_archives and soup is not None:
        visited = {url}
        for yurl in _year_archive_links(soup, url):
            if len(visited) >= max_pages:
                break
            if yurl in visited:
                continue
            visited.add(yurl)
            more, _, yerr = _scrape_page(yurl, timeout, href_match)
            if not yerr:
                items += more

    # Dedupe by key across all pages (prefer an entry that carries a date).
    best = {}
    for it in items:
        k = _key(it["link"]) or it["link"]
        cur = best.get(k)
        if cur is None or (cur.get("ts") is None and it.get("ts") is not None):
            best[k] = it
    items = list(best.values())
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items, None


_DATE_LABEL_RE = re.compile(r"Date\s*:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", re.I)

# Containers RBI uses for the main body, most specific first. Press releases use
# <div class="text1">; notification pages use a different template, so we also try
# their known content cells and fall back to the largest text block on the page.
_BODY_SELECTORS = [
    ("div", {"class": "text1"}),         # press releases (and some notifications)
    ("td", {"class": "tablecontent2"}),  # notification body cell
    ("div", {"id": "divNotification"}),
    ("div", {"class": "notification"}),
]


def _body_node(soup):
    """Pick the element holding the main body. Tries known RBI containers, then
    falls back to the leaf-most block with a substantial amount of text (skips
    short nav/sidebar blocks and outer wrappers that would drag in the header)."""
    for name, attrs in _BODY_SELECTORS:
        node = soup.find(name, attrs=attrs)
        if node and node.get_text(strip=True):
            return node
    blocks = [b for b in soup.find_all(["td", "div"]) if len(b.get_text(" ", strip=True)) > 200]
    if not blocks:
        return None
    # Fewest nested td/div (most leaf-like) first, then most text — so we land on
    # the content block itself, not a layout wrapper enclosing the whole page.
    blocks.sort(key=lambda b: (len(b.find_all(["td", "div"])), -len(b.get_text(" ", strip=True))))
    return blocks[0]


def fetch_detail(url, title="", timeout=20):
    """Fetch one DETAIL page (press release or notification) and pull the full
    body + date.

    RBI puts the body in a content container (e.g. <div class="text1"> for press
    releases) as "... Date : <date> <title> <body>" and exposes only a DATE (no
    time), so the returned ts is that date at midnight. Returns
    {"summary","published","ts"} or None. Never raises.
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
        node = _body_node(soup)
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
    is_notif = href_match == NOTIFICATIONS_HREF_MATCH
    kind = "notification" if is_notif else "press-release"
    print(f"Scraping {url} …\n")
    items, error = scrape_listing(url, href_match=href_match, follow_year_archives=is_notif)
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
