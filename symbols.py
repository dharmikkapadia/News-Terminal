"""symbols.py — NSE symbol ⇄ company-name map (from NSE's EQUITY_L.csv).

Powers two things:
  1. the watchlist PICKER — show "Reliance Industries Limited (RELIANCE)", store RELIANCE;
  2. the announcements feed's SYMBOL RESOLUTION — nse.resolve_symbol() matches a
     normalised announcement <title> against the name→symbol map built here.

The committed data/nse_symbols.json ({SYMBOL: "Name Of Company"}) is generated from
EQUITY_L.csv. refresh_symbols() re-fetches it (cookie-primed, like nse.py) on a slow
cadence and rewrites the file ONLY on a sane parse — a blocked/partial fetch keeps the
committed snapshot (NSE 403s datacenter IPs, so validate the fetch in CI / on a desk).
"""

import csv
import io
import json
import os
import time

import requests

import nse  # normalize_name() + the browser headers / cookie-priming host

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOLS_PATH = os.environ.get("MARKETWIRE_NSE_SYMBOLS_FILE",
                              os.path.join(_DATA_DIR, "nse_symbols.json"))
EQUITY_L_URL = os.environ.get(
    "MARKETWIRE_NSE_EQUITY_L",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")
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
    """The committed {SYMBOL: name} map, or {} if it isn't there yet. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def name_to_symbol(symbols=None):
    """{normalized company name -> SYMBOL} for nse.resolve_symbol(). Built from `symbols`
    (or the committed map). Later entries win on the rare normalized-name collision."""
    symbols = symbols if symbols is not None else load_symbols()
    return {nse.normalize_name(name): sym for sym, name in symbols.items()}


def save_symbols(symbols, path=SYMBOLS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(symbols, f, ensure_ascii=False, indent=0, sort_keys=True)
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


def refresh_symbols(path=SYMBOLS_PATH, url=EQUITY_L_URL, max_age_days=7):
    """Refresh data/nse_symbols.json from EQUITY_L.csv — but only every `max_age_days`
    (the list barely changes), and only overwrite on a sane parse (>= _MIN_ROWS). Preserves
    the committed map on any failure. Never raises. Returns a status string."""
    try:
        if max_age_days and os.path.exists(path):
            age_days = (time.time() - os.path.getmtime(path)) / 86400.0
            if age_days < max_age_days and load_symbols(path):
                return f"symbols fresh ({age_days:.1f}d old) — skipping fetch"
        symbols, err = fetch_equity_l(url)
        if err:
            return f"symbols fetch failed ({err}) — keeping committed map"
        if len(symbols) < _MIN_ROWS:
            return f"symbols parse too small ({len(symbols)} rows) — keeping committed map"
        save_symbols(symbols, path)
        return f"symbols updated ({len(symbols)} companies)"
    except Exception as ex:                         # defensive — must never raise in the cron
        return f"symbols refresh errored ({type(ex).__name__}: {ex}) — keeping committed map"


if __name__ == "__main__":
    print(refresh_symbols())
