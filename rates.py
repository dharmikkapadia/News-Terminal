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
from urllib.parse import quote

import requests

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RATES_PATH = os.environ.get("MARKETWIRE_RATES_FILE", os.path.join(_DATA_DIR, "rates.json"))
# The scrape TARGET (RBI home page). Distinct from MARKETWIRE_RATES_URL, which is where the
# *app* may read the committed rates.json from a raw URL (parallel to MARKETWIRE_HISTORY_URL).
HOME_URL = os.environ.get("MARKETWIRE_RATES_HOME", "https://www.rbi.org.in/")
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
IST = timezone(timedelta(hours=5, minutes=30))

# --- Trading Economics FX overlay (USD/EUR/GBP-INR) ----------------------------
# USD/INR, EUR/INR and GBP/INR are sourced from Trading Economics' server-rendered
# currencies table (quote=inr) instead of RBI/FBIL — it gives an intraday price, a
# signed % change vs the previous close, and a per-pair chart page, mirroring the
# commodities strip. Yahoo's keyless chart endpoint is the fallback if TE is blocked.
# JPY/AED/IDR stay on RBI/FBIL (JPY isn't even quoted on the TE INR page).
TE_CURRENCIES_URL = os.environ.get(
    "MARKETWIRE_TE_CURRENCIES", "https://tradingeconomics.com/currencies?quote=inr")
# Yahoo's keyless chart endpoint (fallback). `<symbol>` is URL-encoded ('=' in FX tickers).
YF_CHART = os.environ.get(
    "MARKETWIRE_YF_CHART", "https://query1.finance.yahoo.com/v8/finance/chart/")
# A browser-ish header set — TE sits behind Cloudflare and Yahoo 403s a bare python UA.
_TE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HTML_HEADERS = {"User-Agent": _TE_UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9",
                 "Referer": "https://tradingeconomics.com/"}
# Each TE-sourced pair: the rates.json scalar it fills, the TE row `data-symbol` (note the
# :CUR suffix), the Yahoo fallback symbol, and the TE chart page. USD/INR's chart link is
# TE's India-currency country page, not a usdinr:cur slug (verified from the live page).
FX_SPECS = [
    dict(key="inr_per_usd", label="USD/INR", te="USDINR:CUR", yf="USDINR=X",
         chart_url="https://tradingeconomics.com/india/currency"),
    dict(key="inr_per_eur", label="EUR/INR", te="EURINR:CUR", yf="EURINR=X",
         chart_url="https://tradingeconomics.com/eurinr:cur"),
    dict(key="inr_per_gbp", label="GBP/INR", te="GBPINR:CUR", yf="GBPINR=X",
         chart_url="https://tradingeconomics.com/gbpinr:cur"),
]
# Sanity bounds (broad) — an INR-per-unit rate outside its range means a mis-parse.
_FX_BOUNDS = {"inr_per_usd": (40, 200), "inr_per_eur": (40, 250), "inr_per_gbp": (50, 300)}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


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
    """First number in `s` as float, else None ('₹ 13,600' -> 13600.0, '5.25%' -> 5.25,
    '−0.12%' -> -0.12 — TE renders negatives with the unicode minus)."""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace("−", "-").replace(",", ""))
    return float(m.group(0)) if m else None


def _range(s):
    """A '6.00% - 6.60%' band -> [6.0, 6.6]; a single '2.50%' -> [2.5, 2.5]; else None."""
    if s is None:
        return None
    nums = re.findall(r"-?\d[\d,]*\.?\d*", str(s).replace(",", ""))
    if not nums:
        return None
    return [float(nums[0]), float(nums[-1])]


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _rows(content):
    """[(label, value)] for every <tr> with a <th>+<td> inside an accordion panel; the
    value has RBI's leading ': ' stripped."""
    out = []
    if content is None:
        return out
    for tr in content.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            label = re.sub(r"\s+", " ", th.get_text(" ", strip=True))
            value = td.get_text(" ", strip=True).lstrip(": ").strip()
            out.append((label, value))
    return out


def _pick(rows, *needles):
    """Value of the first row whose lower-cased label contains all `needles`."""
    for label, value in rows:
        low = label.lower()
        if all(n in low for n in needles):
            return value
    return None


# The five Current Rates panels, by their accordion header text (the page has other
# accordions sharing the class, so we match on these names).
_PANEL_NAMES = {"policy rates", "reserve ratios", "exchange rates",
                "lending / deposit rates", "market trends"}


def _find_panels(soup):
    """{normalized panel name: accordionContent element} for the Current Rates widget —
    each `h3.accordionButton` whose text is a known panel, paired with its following
    `div.accordionContent` sibling. Scoped to `div#wrapper` when present."""
    scope = soup.find(id="wrapper") or soup
    panels = {}
    for h in scope.find_all("h3", class_="accordionButton"):
        name = _norm(h.get_text(" ", strip=True))
        if name in _PANEL_NAMES:
            content = h.find_next_sibling("div", class_="accordionContent")
            if content is not None:
                panels[name] = content
    return panels


def _parse_market_trends(content):
    """Walk the Market Trends panel in document order, routing each <tr> by its inner <h3>
    sub-section and label into money-market / G-Sec / T-bill / capital-market, and each
    'as on …' subText note to its section."""
    mt = {
        "money_market": {"call_rate": None, "as_on": None},
        "gsec_yields": [],
        "tbill_yields": {"91_day": None, "182_day": None, "364_day": None},
        "gsec_tbill_as_on": None,
        "capital_market": {"sensex": None, "nifty_50": None, "as_on": None},
    }
    if content is None:
        return mt
    section = ""
    for el in content.find_all(["h3", "tr", "span"]):
        if el.name == "h3":
            section = _norm(el.get_text(" ", strip=True))
        elif el.name == "tr":
            th, td = el.find("th"), el.find("td")
            if not (th and td):
                continue
            label = re.sub(r"\s+", " ", th.get_text(" ", strip=True))
            low = label.lower()
            value = td.get_text(" ", strip=True).lstrip(": ").strip()
            if "call" in low:
                mt["money_market"]["call_rate"] = _range(value)
            elif "t-bill" in low or "t bill" in low or "treasury bill" in low:
                m = re.search(r"(91|182|364)", low)
                if m:
                    mt["tbill_yields"][f"{m.group(1)}_day"] = _num(value)
            elif re.search(r"gs\s*20\d\d", low) or "g-sec" in low:
                y = _num(value)
                if y is not None:
                    mt["gsec_yields"].append({"security": label, "yield": y})
            elif "sensex" in low:
                mt["capital_market"]["sensex"] = _num(value)
            elif "nifty" in low:
                mt["capital_market"]["nifty_50"] = _num(value)
        elif el.name == "span" and "subText" in (el.get("class") or []):
            note = el.get_text(" ", strip=True)
            if "as on" in note.lower():
                if "money" in section:
                    mt["money_market"]["as_on"] = note
                elif "government" in section or "securities" in section:
                    mt["gsec_tbill_as_on"] = note
                elif "capital" in section:
                    mt["capital_market"]["as_on"] = note
                else:
                    mt["gsec_tbill_as_on"] = mt["gsec_tbill_as_on"] or note
    return mt


def fetch_rates(url=HOME_URL, timeout=20):
    """Scrape RBI's home-page Current Rates accordion (`div#wrapper`) into the rates schema.
    Returns (rates_dict, error).

    The widget is server-rendered: each `h3.accordionButton` is followed by a
    `div.accordionContent` whose <table> rows are `<th>label</th><td>: value</td>`; Market
    Trends is sub-sectioned by inner <h3> (Money Market / Government Securities / Capital).
    BEST EFFORT: RBI may 403 datacenter IPs and the markup can change — either yields a
    partial result that _is_complete() rejects. Validate from a host that can reach RBI.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return None, f"BeautifulSoup unavailable: {ex}"

    soup = BeautifulSoup(resp.content, "html.parser")
    panels = _find_panels(soup)
    if "policy rates" not in panels:
        return None, "Current Rates widget not found (markup changed or page blocked)"

    rates = {"captured_at": datetime.now(IST).isoformat(timespec="seconds"), "source": url}

    pol = _rows(panels.get("policy rates"))
    rates["policy_rates"] = {
        "repo_rate": _num(_pick(pol, "policy repo")),
        "standing_deposit_facility_rate": _num(_pick(pol, "standing deposit")),
        "marginal_standing_facility_rate": _num(_pick(pol, "marginal standing")),
        "bank_rate": _num(_pick(pol, "bank rate")),
        "fixed_reverse_repo_rate": _num(_pick(pol, "fixed reverse")),
    }

    res = _rows(panels.get("reserve ratios"))
    rates["reserve_ratios"] = {"crr": _num(_pick(res, "crr")), "slr": _num(_pick(res, "slr"))}

    fxp = panels.get("exchange rates")
    fxr = _rows(fxp)
    fx_text = fxp.get_text(" ", strip=True) if fxp else ""
    m_as = re.search(r"As at .*?\d{4}", fx_text)
    m_src = re.search(r"Source\s*:?\s*([A-Za-z]+)", fx_text)
    rates["exchange_rates"] = {
        "as_of": m_as.group(0).strip() if m_as else None,
        "source": m_src.group(1).strip() if m_src else None,
        "inr_per_usd": _num(_pick(fxr, "usd")),
        "inr_per_gbp": _num(_pick(fxr, "gbp")),
        "inr_per_eur": _num(_pick(fxr, "eur")),
        "inr_per_100_jpy": _num(_pick(fxr, "jpy")),
        "inr_per_aed": _num(_pick(fxr, "aed")),
        "inr_per_10000_idr": _num(_pick(fxr, "idr")),
    }

    lend = _rows(panels.get("lending / deposit rates"))
    rates["lending_deposit_rates"] = {
        "base_rate": _range(_pick(lend, "base rate")),
        "mclr_overnight": _range(_pick(lend, "mclr")),
        "savings_deposit_rate": _num(_pick(lend, "savings")),
        "term_deposit_rate_gt_1yr": _range(_pick(lend, "term deposit")),
    }

    rates["market_trends"] = _parse_market_trends(panels.get("market trends"))
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
    merged = _merge(prior, scraped)   # scrape covers all panels but MPC; deep-merge keeps it
    save_rates(merged, path)
    return f"rates updated ({merged['policy_rates']['repo_rate']}% repo)"


def _merge(prior, scraped):
    """Deep-merge a scrape onto the prior snapshot: scraped non-null scalars and non-empty
    lists win; None / missing / empty-list keep the prior value — so a partial parse never
    wipes good data, and fields the scrape doesn't cover (e.g. the MPC block) are preserved."""
    out = dict(prior) if isinstance(prior, dict) else {}
    for k, v in (scraped or {}).items():
        if isinstance(v, dict):
            out[k] = _merge(out.get(k) if isinstance(out.get(k), dict) else {}, v)
        elif isinstance(v, list):
            if v:
                out[k] = v
        elif v is not None:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Trading Economics FX overlay (poller) — USD/EUR/GBP-INR, Yahoo fallback
# --------------------------------------------------------------------------- #
def _celltext(tr, cid):
    """Text of the <td id="cid"> inside a TE row (ids repeat per row; scoping to `tr` is fine)."""
    td = tr.find("td", id=cid)
    return td.get_text(" ", strip=True) if td else None


def _fx_asof(s):
    """TE's FX date cell -> ISO date. The most-liquid pair shows a TIME ('12:09', i.e. today);
    others show 'Jun/29'. A time-only cell maps to today (IST); a month/day that lands more than
    a week in the future rolls back a year (a Dec date read in early Jan)."""
    if not s:
        return None
    s = str(s).strip()
    today = datetime.now(IST).date()
    if re.match(r"^\d{1,2}:\d{2}\b", s):
        return today.isoformat()
    m = re.match(r"\s*([A-Za-z]{3})\s*/\s*(\d{1,2})", s)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        d = date(today.year, mon, int(m.group(2)))
    except ValueError:
        return None
    if (d - today).days > 7:
        d = date(today.year - 1, mon, int(m.group(2)))
    return d.isoformat()


def _fx_ok(key, q):
    """True if `q` has an in-bounds price for `key` (a sane INR-per-unit rate)."""
    if not isinstance(q, dict):
        return False
    v = q.get("price")
    lo, hi = _FX_BOUNDS.get(key, (None, None))
    return isinstance(v, (int, float)) and (lo is None or lo <= v <= hi)


def fetch_te_fx(url=TE_CURRENCIES_URL, timeout=25):
    """Scrape USD/EUR/GBP-INR from TE's server-rendered currencies table into
    {scalar_key: {price, prev_close, change_pct, as_of}}. Returns (quotes, error). Best
    effort: a Cloudflare block / markup change yields an error (or empty parse) and the
    caller falls back to Yahoo / preserves prior values. Mirrors commodities.fetch_te —
    each `tr[data-symbol]` row carries `td#p` price, `td#nch` net change, `td#pch` signed
    % vs previous close and `td#date` (the data-symbol keeps TE's ':CUR' suffix)."""
    try:
        resp = requests.get(url, headers=_HTML_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as ex:
        return {}, f"{type(ex).__name__}: {ex}"
    try:
        from bs4 import BeautifulSoup
    except Exception as ex:
        return {}, f"BeautifulSoup unavailable: {ex}"

    soup = BeautifulSoup(resp.content, "html.parser")
    want = {s["te"]: s["key"] for s in FX_SPECS}
    out = {}
    for tr in soup.select("tr[data-symbol]"):
        key = want.get(tr.get("data-symbol"))
        if not key or key in out:
            continue
        price = _num(_celltext(tr, "p"))
        if price is None:
            continue
        nch = _num(_celltext(tr, "nch"))
        pch = _num(_celltext(tr, "pch"))          # signed % vs previous close, straight from TE
        if nch is not None:
            prev = price - nch
        elif pch not in (None, -100):
            prev = price / (1 + pch / 100.0)
        else:
            prev = None
        out[key] = {
            "price": round(price, 4),
            "prev_close": round(prev, 4) if prev is not None else None,
            "change_pct": round(pch, 2) if pch is not None else None,
            "as_of": _fx_asof(_celltext(tr, "date")),
        }
    if not out:
        return {}, "no FX rows parsed (markup changed or blocked)"
    return out, None


def _fetch_yahoo_one(symbol, timeout=20, session=None):
    """One Yahoo symbol's last/prev daily close, % change and as-of date, or (None, error)."""
    url = YF_CHART + quote(symbol, safe="") + "?range=7d&interval=1d"
    try:
        get = (session or requests).get
        resp = get(url, headers={"User-Agent": _TE_UA}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    try:
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return None, "no result"
        res = res[0]
        ts = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        pairs = [(t, c) for t, c in zip(ts, closes) if isinstance(c, (int, float))]
        if len(pairs) < 2:
            return None, "need ≥2 daily closes for a change"
        (cur_t, cur), (_, prev) = pairs[-1], pairs[-2]
        change_pct = ((cur - prev) / prev * 100.0) if prev else None
        return {
            "price": round(cur, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "as_of": datetime.fromtimestamp(cur_t, IST).strftime("%Y-%m-%d"),
        }, None
    except Exception as ex:
        return None, f"parse error: {type(ex).__name__}: {ex}"


def fetch_yahoo_fx(specs, timeout=20):
    """Fetch the given FX specs from Yahoo into {key: {...}} (in-bounds only). Returns
    (quotes, errors). Only the pairs TE missed are passed in, so a healthy TE run skips it."""
    quotes, errors = {}, {}
    if not specs:
        return quotes, errors
    with requests.Session() as s:
        s.headers.update({"User-Agent": _TE_UA})
        for spec in specs:
            q, err = _fetch_yahoo_one(spec["yf"], timeout=timeout, session=s)
            if err:
                errors[spec["key"]] = err
            elif not _fx_ok(spec["key"], q):
                errors[spec["key"]] = f"price {q.get('price')} out of bounds"
            else:
                quotes[spec["key"]] = q
    return quotes, errors


def poll_fx(path=RATES_PATH):
    """Overlay TE-sourced USD/EUR/GBP-INR onto the committed rates.json: replace the
    exchange_rates scalars with the TE price and attach a per-pair `fx_te` block (signed
    % vs previous close, previous close, chart link, as-of, source), Yahoo as fallback.
    Writes ONLY when the USD/INR headline resolves in-bounds; any pair both sources miss
    keeps its last committed value (marked stale). JPY/AED/IDR (RBI/FBIL) are untouched.
    Returns a status string; never raises."""
    te, te_err = fetch_te_fx()
    need = [s for s in FX_SPECS if not _fx_ok(s["key"], te.get(s["key"]))]
    yf, yf_err = fetch_yahoo_fx(need)

    resolved = {}                                    # key -> (quote, source_label, spec)
    for s in FX_SPECS:
        for label, sym, quotes in (("Trading Economics", s["te"], te),
                                   ("Yahoo Finance", s["yf"], yf)):
            q = quotes.get(s["key"])
            if _fx_ok(s["key"], q):
                resolved[s["key"]] = (q, f"{label} · {sym}", s)
                break

    if "inr_per_usd" not in resolved:                # headline pair must resolve to write
        return (f"FX scrape incomplete (USD/INR missing; te_err={te_err or '-'}; "
                f"yf_err={yf_err or '-'}) — keeping committed snapshot")

    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    fx = snap.get("exchange_rates")
    if not isinstance(fx, dict):
        fx = {}
        snap["exchange_rates"] = fx
    prior_te = fx.get("fx_te") if isinstance(fx.get("fx_te"), dict) else {}

    fx_te, fresh = {}, 0
    for s in FX_SPECS:
        key = s["key"]
        if key in resolved:
            q, src, spec = resolved[key]
            fx[key] = q["price"]
            fx_te[key] = {
                "label": spec["label"],
                "prev_close": q["prev_close"],
                "change_pct": q["change_pct"],
                "chart_url": spec["chart_url"],
                "as_of": q["as_of"],
                "source": src,
            }
            fresh += 1
        elif key in prior_te:
            fx_te[key] = dict(prior_te[key], stale=True)   # both sources missed → keep last good
    fx["fx_te"] = fx_te
    fx["fx_te_captured_at"] = datetime.now(IST).isoformat(timespec="seconds")
    save_rates(snap, path)
    return f"FX updated ({fresh}/{len(FX_SPECS)} fresh from TE/Yahoo — USD/INR {fx.get('inr_per_usd')})"


if __name__ == "__main__":
    print(poll_rates())
    print(poll_fx())
