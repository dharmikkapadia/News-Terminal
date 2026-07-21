#!/usr/bin/env python3
"""rates_scrapling.py — browser-rendered RBI scrape (via Scrapling) for the bits the static
requests scraper (rates.py) can't read: the JS-rendered home-page Current Rates accordion and
the **MPC meeting dates** (the one genuinely hand-maintained field in data/rates.json).

Why a browser? `rates.py` fetches with `requests` and parses static HTML. RBI's rates box is a
JS accordion and the MPC schedule isn't on the home page at all — so the project has leaned on a
manual Claude-for-Chrome run for those. Scrapling drives a *real* browser (Chromium via
DynamicFetcher; stealth Firefox via StealthyFetcher as an anti-bot fallback) that executes the
page's JS, so we get the fully-rendered DOM and can automate what was manual.

Design (deliberately thin + safe):
  • RENDER with Scrapling, then PARSE with rates.parse_rates() — the exact same proven parser the
    requests path uses. Scrapling only replaces the *fetch*, not the parse.
  • Write data/rates.json through the SAME guards as rates.poll_rates(): only on a complete,
    in-bounds parse (rates._is_complete), deep-merged (rates._merge) so a partial scrape never
    wipes good data, and the MPC block is updated only when a plausible future date parses.
  • PRESERVE the Trading-Economics FX overlay (USD/EUR/GBP-INR + fx_te) — that's owned by
    rates.poll_fx() on the 30-min cron, not RBI/FBIL. JPY/AED/IDR (also FBIL) take fresh values.
  • Never raise. A blocked/partial render keeps the committed snapshot, exactly like rates.py.

WHERE THIS RUNS: on a host with OPEN egress to rbi.org.in — a GitHub Action runner (see
.github/workflows/rates-scrapling.yml) or a real desk. The project's allowlisted sandbox can't
reach RBI (the egress proxy denies the CONNECT), so this can only be validated there/in CI.

Requires: pip install "scrapling[fetchers]" beautifulsoup4 requests  +  `scrapling install`.
"""

import json
import os
import re
import sys
from datetime import datetime, date

import common
import rates  # reuse parse_rates / _is_complete / _merge / save_rates / IST / RATES_PATH / _MONTHS

# Scrape targets (overridable for testing). HOME_URL is RBI's home page (the rates accordion);
# MPC_URL is the meeting-schedule / overview page that carries the upcoming MPC dates.
# NB: an UNSET GitHub repo variable injects an EMPTY env var (not absent), so use `or` to fall
# back — os.environ.get(..., default) would hand back "" and we'd navigate to an invalid URL.
_HOME_DEFAULT = "https://www.rbi.org.in/"
_MPC_DEFAULT = "https://www.rbi.org.in/scripts/FS_Overview.aspx?fn=2752"
HOME_URL = (os.environ.get("MARKETWIRE_RATES_HOME", "").strip()
            or (rates.HOME_URL or "").strip() or _HOME_DEFAULT)
MPC_URL = os.environ.get("MARKETWIRE_MPC_URL", "").strip() or _MPC_DEFAULT
# Browser fetch budget (Playwright/Camoufox timeouts are in milliseconds).
RENDER_TIMEOUT_MS = int(os.environ.get("MARKETWIRE_RENDER_TIMEOUT_MS", "60000"))

_MONTH_RE = (r"(?:January|February|March|April|May|June|July|August|September|"
             r"October|November|December)")
# An MPC date phrase ends in a comma + 4-digit year, e.g. "August 3, 4 and 5, 2026" or
# "September 29 to October 1, 2026" or "August 3-5, 2026". The trailing ",\s*\d{4}" anchors
# the year (so day numbers in the run are never mistaken for it).
_MPC_PHRASE_RE = re.compile(
    _MONTH_RE + r"\s+\d{1,2}"
    r"(?:\s*(?:,|and|&|to|through|[-–—])\s*(?:" + _MONTH_RE + r"\s+)?\d{1,2})*"
    r"\s*,\s*\d{4}",
    re.I,
)


_annotate = common.annotate     # GitHub Actions run-summary annotation


# --------------------------------------------------------------------------- #
# Browser render (Scrapling)
# --------------------------------------------------------------------------- #
def _render(url, wait_selector=None, timeout=RENDER_TIMEOUT_MS):
    """Render `url` in a real browser and return (html, error). Tries Chromium
    (DynamicFetcher) first, then stealth Firefox with Cloudflare solving (StealthyFetcher)
    as the anti-bot fallback. Each fetcher is retried once without `wait_selector` so a
    markup change to the awaited element can't sink an otherwise-good render."""
    if not (isinstance(url, str) and url.strip()):
        return None, "empty/invalid url"
    attempts = []

    def _one(fetcher_name, **extra):
        try:
            from scrapling import fetchers
        except Exception as ex:                      # scrapling/browser not installed
            attempts.append(f"{fetcher_name}: import failed: {type(ex).__name__}: {ex}")
            return None
        fetcher = getattr(fetchers, fetcher_name)
        for ws in ([wait_selector, None] if wait_selector else [None]):
            kw = dict(headless=True, network_idle=True, timeout=timeout, **extra)
            if ws:
                kw["wait_selector"] = ws
            try:
                page = fetcher.fetch(url, **kw)
                html = getattr(page, "html_content", None) or ""
                if html.strip():
                    return html
                attempts.append(f"{fetcher_name}(wait={ws!r}): empty html")
            except Exception as ex:
                attempts.append(f"{fetcher_name}(wait={ws!r}): {type(ex).__name__}: {ex}")
        return None

    html = _one("DynamicFetcher")
    if html:
        return html, None
    html = _one("StealthyFetcher", solve_cloudflare=True)
    if html:
        return html, None
    return None, "; ".join(attempts) or "no html"


def fetch_rates_browser(url=HOME_URL):
    """Render the RBI home page in a browser and parse the Current Rates accordion with the
    shared rates.parse_rates(). Returns (rates_dict, error)."""
    html, err = _render(url, wait_selector="h3.accordionButton")
    if err:
        return None, f"render failed ({err})"
    return rates.parse_rates(html, url)


# --------------------------------------------------------------------------- #
# MPC meeting dates (the manual field) — parse the rendered schedule page
# --------------------------------------------------------------------------- #
def parse_mpc_phrase(phrase):
    """'August 3, 4 and 5, 2026' -> (date(2026,8,3), date(2026,8,5), 'August 3, 4 and 5, 2026').
    Days inherit the most recently named month (handles cross-month ranges like
    'September 29 to October 1, 2026'); the trailing year applies to both endpoints, rolling the
    end into the next year only if it would otherwise precede the start. Returns None if unparsable."""
    ym = re.search(r"\b(\d{4})\b", phrase)
    if not ym:
        return None
    year = int(ym.group(1))
    body = phrase[:ym.start()] + phrase[ym.end():]   # drop the year so it isn't read as a day
    pairs, cur_month = [], None
    for tok in re.finditer(_MONTH_RE + r"|\d{1,2}", body, re.I):
        t = tok.group(0)
        if t[0].isdigit():
            if cur_month is not None:
                day = int(t)
                if 1 <= day <= 31:
                    pairs.append((cur_month, day))
        else:
            cur_month = rates._MONTHS.get(t[:3].lower())
    if not pairs:
        return None
    (sm, sd), (em, ed) = pairs[0], pairs[-1]
    try:
        start = date(year, sm, sd)
        end = date(year, em, ed)
    except ValueError:
        return None
    if end < start:                                  # range crossed into the next year
        try:
            end = date(year + 1, em, ed)
        except ValueError:
            return None
    return start, end, re.sub(r"\s+", " ", phrase.strip())


def find_next_mpc(text, today=None):
    """Scan free text for MPC date phrases and return the next meeting as
    {next_meeting_start, next_meeting_end, as_text} (ISO dates), or None. 'Next' = the
    earliest meeting whose END is today-or-later (so an in-progress meeting still counts);
    if none are upcoming, the most recent past meeting is returned as a best effort."""
    today = today or datetime.now(rates.IST).date()
    cands = []
    for m in _MPC_PHRASE_RE.finditer(text or ""):
        parsed = parse_mpc_phrase(m.group(0))
        if parsed:
            cands.append(parsed)
    if not cands:
        return None
    upcoming = sorted((c for c in cands if c[1] >= today), key=lambda c: c[0])
    start, end, txt = upcoming[0] if upcoming else max(cands, key=lambda c: c[0])
    return {
        "next_meeting_start": start.isoformat(),
        "next_meeting_end": end.isoformat(),
        "as_text": txt,
    }


def fetch_mpc(url=MPC_URL):
    """Render the MPC schedule page and extract the next meeting. Returns (mpc_dict, error);
    mpc_dict carries source_url. Best effort — page structure varies and may change."""
    html, err = _render(url)
    if err:
        return None, f"render failed ({err})"
    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)         # crude fallback if bs4 is unavailable
    mpc = find_next_mpc(text)
    if not mpc:
        return None, "no MPC date phrase parsed (page structure changed?)"
    mpc["source_url"] = url
    return mpc, None


def _mpc_in_window(mpc, today=None):
    """Guard: accept a parsed MPC block only if its start is within a sane window
    (today-60d .. today+540d) — keeps a stray/garbage date from overwriting a good block."""
    today = today or datetime.now(rates.IST).date()
    try:
        start = datetime.strptime(mpc["next_meeting_start"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
        return False
    return -60 <= (start - today).days <= 540


# --------------------------------------------------------------------------- #
# Orchestrate: render rates + MPC, guard, merge, write
# --------------------------------------------------------------------------- #
def poll_rates_browser(path=None, home_url=HOME_URL, mpc_url=MPC_URL):
    """Refresh data/rates.json from a browser render. Writes ONLY on a complete+in-bounds rates
    parse, deep-merged onto the committed snapshot; the MPC block updates only when a plausible
    future date parses, and the TE FX overlay (USD/EUR/GBP-INR + fx_te) is always preserved.
    Returns a status string; never raises."""
    path = path or rates.RATES_PATH

    scraped, err = fetch_rates_browser(home_url)
    mpc, mpc_err = fetch_mpc(mpc_url)

    # Read the committed snapshot (the merge target) straight from disk.
    try:
        with open(path, "r", encoding="utf-8") as f:
            prior = json.load(f)
    except Exception:
        prior = {}

    notes = []
    if err:
        notes.append(f"rates: {err}")
    elif not rates._is_complete(scraped):
        notes.append("rates: incomplete/out-of-range")
        scraped = None

    if scraped is None and not (mpc and _mpc_in_window(mpc)):
        # Nothing usable from either render — leave the committed snapshot untouched.
        if mpc_err:
            notes.append(f"mpc: {mpc_err}")
        return "kept committed snapshot — " + ("; ".join(notes) or "no usable data")

    merged = rates._merge(prior, scraped) if scraped else dict(prior)

    # Preserve the Trading-Economics FX overlay — owned by poll_fx (30-min cron), not RBI/FBIL.
    if scraped:
        prior_fx = prior.get("exchange_rates") if isinstance(prior.get("exchange_rates"), dict) else {}
        out_fx = merged.get("exchange_rates")
        if isinstance(out_fx, dict):
            for k in ("inr_per_usd", "inr_per_eur", "inr_per_gbp", "fx_te", "fx_te_captured_at"):
                if k in prior_fx:
                    out_fx[k] = prior_fx[k]

    # Update MPC only when a plausible future date parsed (else keep the existing block).
    mpc_status = "unchanged"
    if mpc and _mpc_in_window(mpc):
        cur = merged.get("mpc") if isinstance(merged.get("mpc"), dict) else {}
        merged["mpc"] = {**cur, **mpc}
        mpc_status = mpc["as_text"]
    elif mpc_err:
        notes.append(f"mpc: {mpc_err}")

    rates.save_rates(merged, path)
    repo = ((merged.get("policy_rates") or {}).get("repo_rate"))
    head = f"rates {'updated' if scraped else 'kept'} (repo {repo}%), MPC: {mpc_status}"
    return head + (" — " + "; ".join(notes) if notes else "")


def main():
    status = poll_rates_browser()
    print(status)
    if status.startswith("kept committed snapshot"):
        _annotate("warning", "rates(browser) refresh", status)
    return 0          # green-safe: a failed render keeps the snapshot, never fails the run


if __name__ == "__main__":
    sys.exit(main())
