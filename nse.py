"""nse.py — NSE corporate-announcements feed, resolved to NSE symbols (no Streamlit).

Parses NSE's Online_announcements.xml. Each <item> carries:
  - <title>       the COMPANY NAME, full legal form ("Zensar Technologies Limited") —
                  the ONE reliable identifier (the <link> filename is often an uploader
                  username like "team_bbodade_…" or a BSE numeric code, not the symbol).
  - <link>        the attachment URL (PDF / XBRL); may be empty (mutual-fund NAV rows).
  - <description> "{Company} has informed the Exchange about … |SUBJECT: {category}".
  - <pubDate>     "08-Jul-2026 16:26:47" (DD-Mon-YYYY HH:MM[:SS], IST; sometimes UPPER
                  month / no seconds) — NOT RFC-822, so we parse it ourselves.

Because the symbol isn't in the item, we resolve it by matching the NORMALISED title
against a symbol→name map built from NSE's EQUITY_L.csv (see symbols.py / data/nse_symbols.json).
The huge block of mutual-fund/ETF "Declaration of NAV" rows is dropped (never an equity
announcement). This module is pure fetch/parse — the watchlist filter lives in the app.

NB: NSE blocks bots (cookie handshake + browser headers required, and datacenter IPs may
be 403'd), so fetch_announcements primes cookies from the site first; validate reachability
from CI / a real desk, like the RBI/investing.com scrapers. Returns (items, error); never raises.
"""

import calendar
import html
import re
from datetime import datetime, timezone, timedelta

import requests

# The announcements RSS. Override via env for a mirror / local test file.
import os
NSE_ANNOUNCEMENTS_FEED = os.environ.get(
    "MARKETWIRE_NSE_FEED",
    "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml")
# NSE gates the archive host behind a cookie set by the main site; prime it first.
NSE_HOME = "https://www.nseindia.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}
IST = timezone(timedelta(hours=5, minutes=30))
_TAG_RE = re.compile(r"<[^>]+>")
# "…details |SUBJECT: Board Meeting Intimation"  ->  the trailing category.
_SUBJECT_RE = re.compile(r"\|\s*SUBJECT\s*:\s*(.+?)\s*$", re.I | re.S)
# A leading "SYMBOL : " / "SYMBOL: " some descriptions carry ("UNICHEMLAB: …", "GOLDBEES : …").
_LEAD_SYM_RE = re.compile(r"^\s*([A-Z0-9][A-Z0-9&\-]{1,19})\s*:\s+")
# NAV rows we drop wholesale (mutual-fund / ETF spam — ~40% of the feed, empty <link/>).
_DROP_SUBJECTS = {"declaration of nav"}


def strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()


def parse_pubdate(s):
    """NSE's 'DD-Mon-YYYY HH:MM[:SS]' (IST) -> epoch seconds, or None. Tolerates an
    UPPERCASE month ('08-JUL-2026') and a missing seconds field ('08-Jul-2026 15:20')."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.title()                                   # 'JUL'->'Jul'; digits unaffected
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=IST).timestamp())
        except ValueError:
            continue
    return None


def split_subject(description):
    """('body without the |SUBJECT tag', 'Subject Category' or ''). The subject is the
    announcement TYPE (Board Meeting Intimation, Press Release, Certificate under SEBI …)."""
    d = strip_html(description)
    m = _SUBJECT_RE.search(d)
    if not m:
        return d, ""
    return d[:m.start()].strip(" |"), m.group(1).strip()


def normalize_name(name):
    """Fold a company name to a comparable key: lowercase, drop a leading 'the', unify
    '&'/'and' and 'ltd'/'limited', strip punctuation, collapse whitespace. So the feed's
    'The Tata Power Company Limited' / 'SHREE CEMENT LIMITED' match EQUITY_L's canonical name."""
    s = (name or "").lower().strip()
    s = re.sub(r"[^a-z0-9&]+", " ", s)              # punctuation -> space
    s = s.replace("&", " and ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\bltd\b", "limited", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def resolve_symbol(title, description, name_to_symbol):
    """Best-effort NSE symbol for an item: primary = normalised-title lookup in the
    symbol→name map; fallback = a leading 'SYMBOL:' in the description if it's a real symbol."""
    sym = (name_to_symbol or {}).get(normalize_name(title))
    if sym:
        return sym
    m = _LEAD_SYM_RE.match(description or "")
    if m and (name_to_symbol is None or m.group(1) in set((name_to_symbol or {}).values())):
        return m.group(1)
    return None


def parse_announcements(xml_bytes, name_to_symbol=None, drop_nav=True):
    """Parse the announcements XML into a list of dicts (newest first):
      {title, company, symbol, subject, summary, link, published, ts}
    `name_to_symbol` (normalized name -> SYMBOL) resolves the symbol; None leaves it None.
    NAV / ETF rows are dropped when drop_nav. Returns (items, error); never raises."""
    try:
        import feedparser
    except Exception as ex:
        return [], f"feedparser unavailable: {ex}"
    try:
        parsed = feedparser.parse(xml_bytes)
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"
    if parsed.bozo and not parsed.entries:
        return [], f"not a readable feed ({getattr(parsed, 'bozo_exception', 'unknown')})"

    items = []
    for e in parsed.entries:
        company = (e.get("title") or "").strip()
        raw_desc = e.get("summary") or e.get("description") or ""
        body, subject = split_subject(raw_desc)
        if drop_nav and subject.lower() in _DROP_SUBJECTS:
            continue
        published = (e.get("published") or e.get("updated") or "").strip()
        symbol = resolve_symbol(company, raw_desc, name_to_symbol)
        items.append({
            "title": company,                       # the company name is the headline
            "company": company,
            "symbol": symbol,
            "subject": subject,
            "summary": body,
            "link": (e.get("link") or "").strip(),
            "published": published,
            "ts": parse_pubdate(published),
        })
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items, None


def fetch_announcements(url=NSE_ANNOUNCEMENTS_FEED, name_to_symbol=None, timeout=20):
    """Fetch + parse the live feed. Primes NSE cookies from the home page first (the archive
    host 403s a cold request), then GETs the RSS with browser headers. Returns (items, error)."""
    try:
        with requests.Session() as s:
            s.headers.update(_HEADERS)
            try:
                s.get(NSE_HOME, timeout=timeout)     # seed cookies (best-effort)
            except Exception:
                pass
            resp = s.get(url, timeout=timeout)
            resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"
    return parse_announcements(resp.content, name_to_symbol=name_to_symbol)
