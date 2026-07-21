#!/usr/bin/env python3
"""econ_calendar.py — India macro-economic calendar from Trading Economics.

Scrapes TE's server-rendered India calendar page
(https://tradingeconomics.com/india/calendar) into a committed snapshot —
`data/calendar.json` — in the same in-repo, no-database spirit as rates /
commodities. The Streamlit app renders it as a signal strip of the next key
releases over an expandable full event table (previous / consensus / actual,
with TE's 1–3 importance rating).

ACCUMULATE + MERGE, not replace: TE's logged-out page serves a rolling window
(roughly the current weeks), so each poll MERGES what it sees onto the committed
events by id — newly listed events are added, released `actual` values fill in,
and events that rolled out of TE's window are kept until they age out (pruned
past ~14 days back / ~180 days ahead). That way the snapshot builds the forward
schedule over time and retains recent actuals for the app's "recent releases"
view, whatever window TE happens to serve.

Guarded exactly like the other scrapers: the file is rewritten ONLY when the
parse looks sane (`_is_sane`), a blocked/Cloudflare page or markup change keeps
the committed snapshot untouched, and poll_calendar() NEVER raises. Rides the
30-min poll.py cron (actuals land promptly after release), like FX/commodities.

Times are stored as TE prints them (no timezone conversion or claim — TE
localizes per-visitor by cookie, so the raw string is the honest value).

Written WITHOUT live TE access (this sandbox is Cloudflare-challenged): the
parser is unit-tested against fixture markup modelled on TE's calendar table
(tests/test_calendar.py), and the first CI poll is its live test — set
MARKETWIRE_CALENDAR_DUMP to write the fetched HTML for markup diagnosis
(history.yml uploads it as the `calendar-fetch-dump` artifact on schedule runs).
"""

import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests

import common

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CALENDAR_PATH = os.environ.get(
    "MARKETWIRE_CALENDAR_FILE", os.path.join(_DATA_DIR, "calendar.json"))
CALENDAR_URL = os.environ.get(
    "MARKETWIRE_TE_CALENDAR", "https://tradingeconomics.com/india/calendar")
_HTML_HEADERS = {**common.HTML_HEADERS, "Referer": "https://tradingeconomics.com/"}
# Where to dump the fetched HTML for markup diagnosis (CI uploads it as an artifact).
_DUMP_PATH = os.environ.get("MARKETWIRE_CALENDAR_DUMP", "").strip()

IST = common.IST
# Snapshot retention around "today": recent actuals stay 2 weeks, schedule 6 months.
KEEP_PAST_DAYS = 14
KEEP_FUTURE_DAYS = 180
# The per-event fields the scrape may refresh (non-empty fresh value wins on merge).
_MERGE_FIELDS = ("date", "time", "event", "url", "importance",
                 "actual", "previous", "consensus", "forecast")

# "Thursday July 23 2026" (TE's per-day header) — month name + day + year.
_DATE_RE = re.compile(r"([A-Z][a-z]{2,8})\s+(\d{1,2})\s+(\d{4})")
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", re.I)


def load_calendar(source=None, url_env="MARKETWIRE_CALENDAR_URL",
                  default_path=CALENDAR_PATH):
    """Read the committed calendar snapshot (URL-or-path, like rates/commodities).
    Never raises — returns the parsed dict, or None if there's nothing readable."""
    return common.load_json_snapshot(source, url_env, default_path,
                                     headers={"User-Agent": common.BROWSER_UA})


def _header_date(text):
    """ISO date from a per-day header ('Thursday July 23 2026'), else None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mon = common.MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(2))).date().isoformat()
    except ValueError:
        return None


def _time_minutes(s):
    """'12:00 PM' / '09:15' -> minutes since midnight for ordering, else None.
    Display always uses the raw string; this is only a sort key."""
    m = _TIME_RE.match(s or "")
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    return h * 60 + mins if h < 24 and mins < 60 else None


def _span_text(tr, span_id):
    """Text of the descendant carrying id=`span_id` (TE repeats ids per row —
    scoping to the row is fine, same as its quotes tables). '' if absent/empty."""
    el = tr.find(id=span_id)
    return el.get_text(" ", strip=True) if el else ""


def _row_event(tr, current_date, base_url):
    """One calendar <tr> -> an event dict, or None for non-event rows."""
    if current_date is None or not tr.get("data-url"):
        return None
    a = tr.find("a", href=True)
    if a is not None and a.get_text(strip=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        url = urljoin(base_url, a["href"])
    else:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        title = max(cells, key=len, default="").strip()
        url = urljoin(base_url, tr["data-url"])
    if not title:
        return None
    time_str = ""
    for td in tr.find_all("td"):
        if _TIME_RE.match(td.get_text(" ", strip=True)):
            time_str = td.get_text(" ", strip=True)
            break
    imp = common.num(tr.get("data-importance"))
    return {
        "id": tr.get("data-id") or f"{current_date}|{title}",
        "date": current_date,
        "time": time_str,
        "event": title,
        "url": url,
        "importance": int(imp) if isinstance(imp, float) and imp in (1.0, 2.0, 3.0) else None,
        "actual": _span_text(tr, "actual"),
        "previous": _span_text(tr, "previous"),
        "consensus": _span_text(tr, "consensus"),
        "forecast": _span_text(tr, "forecast"),
    }


def parse_calendar(html_text, base_url=CALENDAR_URL):
    """Parse TE's calendar table into a list of event dicts. Walks the markup in
    document order, tracking the current per-day header (`<thead>` text carrying
    'Weekday Month DD YYYY') and reading each `tr[data-url]` under it; field spans
    are found by id (actual/previous/consensus/forecast), importance from the
    row's data-importance. Returns (events, error); never raises."""
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return [], f"BeautifulSoup unavailable: {ex}"
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        scope = soup.find("table", id="calendar") or soup
        events, seen, current_date = [], set(), None
        for el in scope.find_all(["thead", "tr"]):
            if el.name == "thead":
                d = _header_date(el.get_text(" ", strip=True))
                if d:
                    current_date = d
                continue
            ev = _row_event(el, current_date, base_url)
            if ev is None or ev["id"] in seen:
                continue
            seen.add(ev["id"])
            events.append(ev)
        if not events:
            return [], "no calendar rows parsed (markup changed or blocked)"
        return events, None
    except Exception as ex:
        return [], f"parse error: {type(ex).__name__}: {ex}"


def fetch_calendar(url=CALENDAR_URL, timeout=25):
    """GET the calendar page (browser headers — TE sits behind Cloudflare) and
    parse it. Dumps the HTML to MARKETWIRE_CALENDAR_DUMP if set (even a block
    page, for diagnosis). Returns (events, error); never raises."""
    try:
        resp = requests.get(url, headers=_HTML_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return [], f"{type(ex).__name__}: {ex}"
    html = resp.text or ""
    if _DUMP_PATH:
        try:
            with open(_DUMP_PATH, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
    return parse_calendar(html, url)


def _is_sane(events):
    """The gate that keeps a partial/garbage page from touching the snapshot:
    a real TE India week always lists several events (auctions, reserves, …)."""
    return len(events) >= 3 and all(e.get("date") and e.get("event") for e in events)


def _sort_key(e):
    tm = _time_minutes(e.get("time"))
    return (e.get("date") or "", tm if tm is not None else -1, e.get("event") or "")


def merge_events(prior, fresh, today=None):
    """Merge freshly scraped events onto the committed list by id: non-empty fresh
    fields win (so released `actual`s fill in and consensus revisions update),
    events TE no longer lists are kept until pruned (date outside
    today-KEEP_PAST_DAYS .. today+KEEP_FUTURE_DAYS). Returns the merged list,
    sorted by (date, time)."""
    today = today or datetime.now(IST).date()
    now_iso = datetime.now(IST).isoformat(timespec="seconds")
    by_id = {e.get("id"): dict(e) for e in (prior or []) if e.get("id")}
    for ev in fresh:
        cur = by_id.get(ev["id"])
        if cur is None:
            by_id[ev["id"]] = dict(ev, first_seen=now_iso)
            continue
        changed = False
        for k in _MERGE_FIELDS:
            v = ev.get(k)
            if v not in (None, "") and v != cur.get(k):
                cur[k] = v
                changed = True
        if changed:
            cur["updated_at"] = now_iso
    lo = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    hi = (today + timedelta(days=KEEP_FUTURE_DAYS)).isoformat()
    kept = [e for e in by_id.values() if lo <= (e.get("date") or "") <= hi]
    kept.sort(key=_sort_key)
    return kept


def save_calendar(snapshot, path=CALENDAR_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")


def poll_calendar(path=CALENDAR_PATH, url=CALENDAR_URL):
    """Refresh the committed calendar: scrape TE, merge onto the prior snapshot,
    prune the window, rewrite the file — ONLY on a sane parse. A blocked/partial
    fetch keeps the committed snapshot. Never raises. Returns a status string."""
    try:
        fresh, err = fetch_calendar(url)
        if err:
            return f"calendar fetch failed ({err}) — keeping committed snapshot"
        if not _is_sane(fresh):
            return (f"calendar parse too thin ({len(fresh)} rows) — "
                    "keeping committed snapshot")
        # Read the prior snapshot strictly from the local FILE (merge target).
        try:
            with open(path, "r", encoding="utf-8") as f:
                prior = (json.load(f) or {}).get("events") or []
        except Exception:
            prior = []
        events = merge_events(prior, fresh)
        with_actual = sum(1 for e in events if (e.get("actual") or "").strip())
        save_calendar({
            "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
            "source": "Trading Economics",
            "calendar_url": url,
            "events": events,
        }, path)
        return (f"calendar updated ({len(fresh)} scraped, {len(events)} stored, "
                f"{with_actual} with actuals)")
    except Exception as ex:                       # defensive — must never fail the cron
        return f"calendar errored ({type(ex).__name__}: {ex}) — keeping committed snapshot"


if __name__ == "__main__":
    print(poll_calendar())
