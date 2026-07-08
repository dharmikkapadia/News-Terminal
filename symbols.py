"""symbols.py — NSE symbol ⇄ company-name map (from NSE's EQUITY_L.csv).

Powers two things:
  1. the watchlist PICKER — show "Reliance Industries Limited (RELIANCE)", store RELIANCE;
  2. the announcements feed's SYMBOL RESOLUTION — nse.resolve_symbol() matches a
     normalised announcement <title> against the name→symbol map built here.

The committed data/nse_symbols.json ({SYMBOL: "Name Of Company"}) is generated from
NSE's main-board EQUITY_L.csv merged with the SME (Emerge) SME_EQUITY_L.csv, so SME
companies in the announcements feed resolve too. refresh_symbols() re-fetches both
(cookie-primed, like nse.py) on a slow cadence and rewrites the file ONLY on a sane parse
— a blocked/partial fetch keeps the committed snapshot (NSE 403s datacenter IPs, so
validate the fetch in CI / on a desk).
"""

import csv
import io
import json
import os
from datetime import datetime, timezone, timedelta

import requests

import nse  # normalize_name() + the browser headers / cookie-priming host

IST = timezone(timedelta(hours=5, minutes=30))

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOLS_PATH = os.environ.get("MARKETWIRE_NSE_SYMBOLS_FILE",
                              os.path.join(_DATA_DIR, "nse_symbols.json"))
EQUITY_L_URL = os.environ.get(
    "MARKETWIRE_NSE_EQUITY_L",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")
# NSE's SME (Emerge) board list — same CSV shape, merged onto the main board so SME
# companies in the announcements feed also resolve. Set to "" to disable SME coverage.
SME_EQUITY_L_URL = os.environ.get(
    "MARKETWIRE_NSE_SME_EQUITY_L",
    "https://nsearchives.nseindia.com/content/equities/SME_EQUITY_L.csv")
# A real EQUITY_L carries ~2000+ rows; a much shorter parse means a blocked/garbage fetch,
# so we refuse to overwrite the committed map with it.
_MIN_ROWS = 1000


def parse_csv(text):
    """EQUITY_L.csv text -> {SYMBOL: 'Name Of Company'}. Uses the header to find the SYMBOL
    and NAME columns (the header's field names carry stray spaces: 'SYMBOL,NAME OF COMPANY, SERIES…')."""
    out = {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return out
    header = [h.strip().upper() for h in rows[0]]
    try:
        i_sym = header.index("SYMBOL")
        i_name = header.index("NAME OF COMPANY")
    except ValueError:
        i_sym, i_name = 0, 1                       # fall back to the known column order
    for r in rows[1:]:
        if len(r) <= max(i_sym, i_name):
            continue
        sym, name = r[i_sym].strip(), r[i_name].strip()
        if sym and name:
            out[sym] = name
    return out


def load_symbols(path=SYMBOLS_PATH):
    """The committed {SYMBOL: name} map, or {} if absent. Handles both the wrapped format
    ({refreshed_at, count, symbols}) written by save_symbols and a legacy flat {SYMBOL: name}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
        return data["symbols"]
    return data if isinstance(data, dict) else {}


def symbols_refreshed_at(path=SYMBOLS_PATH):
    """ISO timestamp of the last successful refresh, stored IN the committed file — or None
    (legacy flat file / never refreshed). Gates the fetch cadence in a CHECKOUT-PROOF way:
    file mtime can't be used because CI resets it to the checkout time on every run."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("refreshed_at") if isinstance(data, dict) else None
    except Exception:
        return None


def name_to_symbol(symbols=None):
    """{normalized company name -> SYMBOL} for nse.resolve_symbol(). Built from `symbols`
    (or the committed map). Later entries win on the rare normalized-name collision."""
    symbols = symbols if symbols is not None else load_symbols()
    return {nse.normalize_name(name): sym for sym, name in symbols.items()}


def save_symbols(symbols, path=SYMBOLS_PATH, refreshed_at=None):
    """Write the map as {refreshed_at, count, symbols} — the refreshed_at stamp lets the
    cadence gate survive a fresh CI checkout (unlike file mtime)."""
    payload = {
        "refreshed_at": refreshed_at or datetime.now(IST).isoformat(timespec="seconds"),
        "count": len(symbols),
        "symbols": dict(sorted(symbols.items())),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0)
        f.write("\n")


def fetch_equity_l(url=EQUITY_L_URL, timeout=30):
    """Fetch EQUITY_L.csv from NSE (priming cookies from the home page first). Returns
    (symbols_dict, error)."""
    try:
        with requests.Session() as s:
            s.headers.update(nse._HEADERS)
            try:
                s.get(nse.NSE_HOME, timeout=timeout)
            except Exception:
                pass
            resp = s.get(url, timeout=timeout)
            resp.raise_for_status()
    except Exception as ex:
        return {}, f"{type(ex).__name__}: {ex}"
    return parse_csv(resp.text), None


def fetch_all(main_url=EQUITY_L_URL, sme_url=SME_EQUITY_L_URL, timeout=30):
    """Fetch the main-board list (required) and merge the SME (Emerge) list onto it
    (additive + best-effort — an SME failure just omits SME rows; the main board wins on
    any symbol overlap). Returns (symbols, error); error is set only if the MAIN fetch fails."""
    main, err = fetch_equity_l(main_url, timeout=timeout)
    if err:
        return {}, err
    merged = dict(main)
    if sme_url:
        sme, _sme_err = fetch_equity_l(sme_url, timeout=timeout)   # best-effort
        for sym, name in sme.items():
            merged.setdefault(sym, name)
    return merged, None


def refresh_symbols(path=SYMBOLS_PATH, max_age_days=7):
    """Refresh data/nse_symbols.json from EQUITY_L.csv (+ SME_EQUITY_L.csv) — but only every
    `max_age_days` (the lists barely change), and only overwrite on a sane parse
    (>= _MIN_ROWS). Preserves the committed map on any failure. Never raises. Status string."""
    try:
        ra = symbols_refreshed_at(path)             # last SUCCESSFUL refresh (committed)
        if max_age_days and ra and load_symbols(path):
            try:
                age_days = (datetime.now(IST) - datetime.fromisoformat(ra)).total_seconds() / 86400.0
            except Exception:
                age_days = None
            if age_days is not None and age_days < max_age_days:
                return f"symbols fresh ({age_days:.1f}d old) — skipping fetch"
        symbols, err = fetch_all()
        if err:
            return f"symbols fetch failed ({err}) — keeping committed map"
        if len(symbols) < _MIN_ROWS:
            return f"symbols parse too small ({len(symbols)} rows) — keeping committed map"
        save_symbols(symbols, path)
        return f"symbols updated ({len(symbols)} companies, main+SME)"
    except Exception as ex:                         # defensive — must never raise in the cron
        return f"symbols refresh errored ({type(ex).__name__}: {ex}) — keeping committed map"


if __name__ == "__main__":
    print(refresh_symbols())
