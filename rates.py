#!/usr/bin/env python3
"""rates.py — RBI "Current Rates" snapshot for the dashboard (load + best-effort scrape).

The Streamlit app shows a Current Rates panel (policy/reserve/exchange/lending rates,
market trends) and a next-MPC-meeting countdown, all read from a single committed
JSON file — `data/rates.json` — in the same in-repo, no-database spirit as history.

Two ways that file gets refreshed:
  1. MANUAL (source of truth): a Claude-for-Chrome run on rbi.org.in emits the JSON
     (RBI 403s datacenter IPs and the rates box is a JS accordion, so a browser on a
     real desk is the reliable extractor); you commit it.
  2. AUTOMATED (best-effort): the poller (poll.py, on the cron) calls poll_rates(),
     which tries to scrape the RBI home page and rewrites the file — but ONLY if the
     scrape is complete and sane (see _is_complete). A blocked / partial / changed-markup
     scrape leaves the committed manual file untouched, so automation can never clobber
     good data. Like rbi_archive.py this was written WITHOUT live access to RBI, so the
     parser must be validated from a machine that can reach the site.

Shapes (all keys may be null if unknown) — see data/rates.json for a full example.
Never raises on load; returns None when there's no readable snapshot.
"""

import json
import os
import re
from datetime import datetime, date, timezone, timedelta

import requests

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RATES_PATH = os.environ.get("MARKETWIRE_RATES_FILE", os.path.join(_DATA_DIR, "rates.json"))
# The scrape TARGET (RBI home page). Distinct from MARKETWIRE_RATES_URL, which is where the
# *app* may read the committed rates.json from a raw URL (parallel to MARKETWIRE_HISTORY_URL).
HOME_URL = os.environ.get("MARKETWIRE_RATES_HOME", "https://www.rbi.org.in/")
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Load (app + poller)
# --------------------------------------------------------------------------- #
def load_rates(source=None, url_env="MARKETWIRE_RATES_URL", default_path=RATES_PATH):
    """Read the committed rates snapshot. `source` (or the env var) may be a raw
    http(s) URL or a file path; defaults to the local committed JSON. Never raises —
    returns the parsed dict, or None if there's nothing readable."""
    source = source or os.environ.get(url_env, "").strip() or default_path
    try:
        if source.startswith(("http://", "https://")):
            resp = requests.get(source, headers={"User-Agent": UA}, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return json.loads(resp.text)
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def mpc_countdown(rates, now=None):
    """(label, days) for the next MPC meeting, or None. `days` is calendar days from
    today (IST) to the meeting's start: 0 = today, negative = in progress/just past."""
    mpc = (rates or {}).get("mpc") or {}
    start = mpc.get("next_meeting_start")
    if not start:
        return None
    try:
        d = datetime.strptime(start, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    today = (now or datetime.now(IST)).date()
    days = (d - today).days
    end = mpc.get("next_meeting_end")
    label = mpc.get("as_text") or _fmt_range(start, end)
    return label, days


def _fmt_range(start, end):
    """'2026-08-03'/'2026-08-05' -> 'Aug 3–5, 2026' (best effort)."""
    try:
        a = datetime.strptime(start, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return start or "—"
    try:
        b = datetime.strptime(end, "%Y-%m-%d").date() if end else None
    except (ValueError, TypeError):
        b = None
    if b and b != a:
        if (a.year, a.month) == (b.year, b.month):
            return f"{a:%b} {a.day}–{b.day}, {a.year}"
        return f"{a:%b %d} – {b:%b %d}, {b.year}"
    return f"{a:%b %d, %Y}"


# --------------------------------------------------------------------------- #
# Best-effort scrape (poller only) — guarded so it can't clobber the manual file
# --------------------------------------------------------------------------- #
def _num(s):
    """First number in `s` as float, else None ('₹ 13,600' -> 13600.0, '5.25%' -> 5.25)."""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(",", ""))
    return float(m.group(0)) if m else None


def _after(text, label, window=60):
    """The first number appearing within `window` chars after `label` in `text`."""
    m = re.search(re.escape(label), text, re.I)
    if not m:
        return None
    return _num(text[m.end(): m.end() + window])


def fetch_rates(url=HOME_URL, timeout=20):
    """Scrape RBI's home-page Current Rates box. Returns (rates_dict, error).

    BEST EFFORT: RBI may 403 datacenter IPs, the box may be JS-rendered (values absent
    from the served HTML), and the markup can change — any of which yields a partial or
    empty result that _is_complete() will reject. Validate from a host that can reach RBI.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"

    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(resp.content, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"\s+", " ", text)

    rates = {
        "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": url,
        "policy_rates": {
            "repo_rate": _after(text, "Policy Repo Rate"),
            "standing_deposit_facility_rate": _after(text, "Standing Deposit Facility Rate"),
            "marginal_standing_facility_rate": _after(text, "Marginal Standing Facility Rate"),
            "bank_rate": _after(text, "Bank Rate"),
            "fixed_reverse_repo_rate": _after(text, "Fixed Reverse Repo Rate"),
        },
        "reserve_ratios": {"crr": _after(text, "CRR"), "slr": _after(text, "SLR")},
        "exchange_rates": {
            "as_of": None, "source": "FBIL",
            "inr_per_usd": _after(text, "1 USD"),
            "inr_per_gbp": _after(text, "1 GBP"),
            "inr_per_eur": _after(text, "1 EUR"),
            "inr_per_100_jpy": _after(text, "100 JPY"),
            "inr_per_aed": _after(text, "1 AED"),
            "inr_per_10000_idr": _after(text, "10000 IDR"),
        },
    }
    return rates, None


# Sanity bounds — a value outside its range means a mis-parse, not a real rate.
_BOUNDS = {
    "repo_rate": (0, 25), "standing_deposit_facility_rate": (0, 25),
    "marginal_standing_facility_rate": (0, 25), "crr": (0, 15), "slr": (0, 50),
    "inr_per_usd": (40, 200),
}


def _is_complete(rates):
    """True only if the core fields are present AND within sane bounds — the gate that
    keeps a partial/garbage scrape from overwriting the committed manual snapshot."""
    if not rates:
        return False
    pol = rates.get("policy_rates") or {}
    res = rates.get("reserve_ratios") or {}
    fx = rates.get("exchange_rates") or {}
    vals = {
        "repo_rate": pol.get("repo_rate"),
        "marginal_standing_facility_rate": pol.get("marginal_standing_facility_rate"),
        "crr": res.get("crr"), "slr": res.get("slr"),
        "inr_per_usd": fx.get("inr_per_usd"),
    }
    if any(v is None for v in vals.values()):
        return False
    for k, v in vals.items():
        lo, hi = _BOUNDS.get(k, (None, None))
        if lo is not None and not (lo <= v <= hi):
            return False
    return True


def save_rates(rates, path=RATES_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rates, f, ensure_ascii=False, indent=2)
        f.write("\n")


def poll_rates(path=RATES_PATH, url=HOME_URL):
    """Try to refresh the snapshot from RBI. Writes `path` ONLY on a complete+sane
    scrape, preserving any prior fields (e.g. the MPC block, which isn't on the home
    page) the scrape doesn't supply. Returns a status string; never raises."""
    scraped, err = fetch_rates(url)
    if err:
        return f"scrape failed ({err}) — keeping committed snapshot"
    if not _is_complete(scraped):
        return "scrape incomplete/out-of-range — keeping committed snapshot"
    # Read the prior snapshot strictly from the local FILE (not load_rates, which would honour
    # MARKETWIRE_RATES_URL) so we always merge onto — and preserve — the committed JSON.
    try:
        with open(path, "r", encoding="utf-8") as f:
            prior = json.load(f)
    except Exception:
        prior = {}
    merged = dict(prior)
    merged.update(scraped)
    if prior.get("mpc") and not merged.get("mpc"):   # MPC isn't on the home page
        merged["mpc"] = prior["mpc"]
    save_rates(merged, path)
    return f"rates updated ({merged['policy_rates']['repo_rate']}% repo)"


if __name__ == "__main__":
    print(poll_rates())
