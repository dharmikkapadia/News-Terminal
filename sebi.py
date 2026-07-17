#!/usr/bin/env python3
"""sebi.py — scraper for a SEBI "Filings" listing page (sebiweb HomeAction.do).

SEBI's site-wide feed (sebirss.xml, linked from /rss.html) only carries
Enforcement (Orders / Recovery Proceedings / Appeals) and Legal (Regulations)
items — nothing from the Filings module (Public Issues, Rights Issues,
Takeovers, …), so there's no RSS to read for those sections. This scrapes the
listing's server-rendered HTML directly instead, the same best-effort pattern
as rbi_archive.py: never raises, returns (items, error) in the shared
(title, link, summary, published, ts) shape used across the app.

The URL's sid/ssid/smid select the section: sid=3 is the fixed "Filings"
module; ssid picks the subsection (15 = Public Issues); smid picks the
sub-tab within it (10 = "Draft Offer Documents filed with SEBI", i.e. DRHPs —
LISTING_URL below). Point this at a different Filings listing by swapping
ssid/smid (e.g. ssid=16/smid=13 = Draft Letters of Offer under Rights Issues);
the row markup is the same across subsections.

Each row's title <td> nests a SECOND <a> inside the first — SEBI's own markup
puts a related-document link (almost always a PDF) inside the detail-page
<a>'s content, unescaped. That inner anchor becomes a "Related document" note
in the item's `summary`; the outer <a> is always present and is the item's
`link` — a stable per-filing permalink SEBI never reuses. SEBI's listing
carries no `prid=`/`Id=` query param like RBI's, so store.py/history.py fall
back to keying on the link itself, which works fine here since it's unique
and permanent.

Only the first page (~25 newest rows) is fetched — plenty for 30-min forward
polling. There's no pagination/backfill support yet.

SEBI blocks datacenter IPs, same as RBI, so this can't be validated from a
cloud sandbox — run it from a host that can reach sebi.gov.in:

    python sebi.py
"""

import calendar
import re
from datetime import datetime, timezone, timedelta

import requests

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False

# sid=3 (Filings) / ssid=15 (Public Issues) / smid=10 (Draft Offer Documents
# filed with SEBI, i.e. DRHPs) — see the module docstring for the sid/ssid/smid scheme.
LISTING_URL = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=10"
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))
_DATE_RE = re.compile(r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}")


def _parse_date(text):
    """Return (epoch_ts, matched_string) for SEBI's 'Jul 16, 2026' date format."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None, ""
    try:
        dt = datetime.strptime(m.group(0), "%b %d, %Y").replace(tzinfo=IST)
        return calendar.timegm(dt.utctimetuple()), m.group(0)
    except ValueError:
        return None, ""


def _row_item(tr, base_url):
    """One <tr> from the listing table -> an item dict, or None if it doesn't
    look like a data row (e.g. the header)."""
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 2:
        return None
    ts, published = _parse_date(tds[0].get_text(" ", strip=True))
    anchors = tds[1].find_all("a", href=True)
    if not anchors:
        return None
    outer = anchors[0]
    link = requests.compat.urljoin(base_url, outer["href"])
    # Direct-child text of the outer <a> only — SEBI nests a second <a> (the
    # related-document link) inside the outer <a>'s own content, so a plain
    # get_text() would pull that second title into the headline too.
    title_bits = []
    for node in outer.contents:
        if getattr(node, "name", None) == "a":
            break
        title_bits.append(node if isinstance(node, str) else node.get_text(" ", strip=True))
    title = " ".join("".join(title_bits).split())
    if not title:
        title = " ".join(outer.get_text(" ", strip=True).split())  # fallback
    summary = ""
    if len(anchors) > 1:
        inner = anchors[1]
        doc_link = requests.compat.urljoin(base_url, inner["href"])
        doc_title = " ".join(inner.get_text(" ", strip=True).split())
        summary = f"Related document: {doc_title or 'PDF'} — {doc_link}"
    return {"title": title, "link": link, "summary": summary, "published": published, "ts": ts}


def fetch_listing(url=LISTING_URL, timeout=20):
    """Scrape one SEBI Filings listing page (page 1 only — the newest ~25
    rows). Returns (items, error); never raises."""
    if not _HAVE_BS4:
        return [], "beautifulsoup4 not installed (pip install beautifulsoup4)"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"
    try:
        soup = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", id=re.compile("sample")) or soup.find("table")
        if table is None:
            return [], "listing table not found — SEBI's markup may have changed"
        items, seen = [], set()
        for tr in table.find_all("tr"):
            it = _row_item(tr, url)
            if it is None:
                continue
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            items.append(it)
        items.sort(key=lambda x: x["ts"] or 0, reverse=True)
        return items, None
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


if __name__ == "__main__":
    print(f"Scraping {LISTING_URL} …\n")
    items, error = fetch_listing()
    if error:
        print("ERROR:", error)
        raise SystemExit(1)
    for it in items[:50]:
        print(f"  {(it['published'] or '(no date)'):>14}  {it['title'][:90]}")
        print(f"  {'':14}  {it['link']}")
        if it["summary"]:
            print(f"  {'':14}  {it['summary']}")
    dated = sum(1 for i in items if i["ts"])
    print(f"\n{len(items)} filings found; {dated} with a parsed date.")
    if not items:
        print("Nothing parsed — the page structure may have changed. Share a "
              "snippet of the listing HTML and the parser can be tuned.")
