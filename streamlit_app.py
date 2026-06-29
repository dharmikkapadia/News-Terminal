"""MarketWire — an RBI press-release & notifications reader.

A Streamlit app that fetches the RBI Press Releases + Notifications RSS feeds
server-side (browsers can't read most RSS directly because of CORS), remembers
items in a small SQLite store so the wire accumulates over time, and presents them
as a news website: a newspaper masthead over a uniform grid of story cards, with
subtle fade-in/hover motion and a summary preview that expands to the full text.

Look & feel: pick a flagship theme in the sidebar (Bloomberg, Reuters, Paper,
High Contrast). Every palette is tuned so all text stays legible.

Run locally:   streamlit run streamlit_app.py
Deploy:        Streamlit Community Cloud, main file = streamlit_app.py
"""

import html
import os
import re
from datetime import datetime, timezone, timedelta

import streamlit as st

# Mirror Streamlit Cloud Secrets into the environment so the same config keys work
# whether set as env vars (VM / Actions) or via the Cloud Secrets UI — needed for
# MARKETWIRE_DB / MARKETWIRE_DB_AUTH_TOKEN (durable history on Cloud).
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass  # no secrets configured — fine

import commodities  # free commodity-price snapshot (data/commodities.json) for the dashboard
import feed         # pure RSS fetch/parse (shared with the poller)
import history      # durable history kept as JSONL in the repo (maintained by the Action)
import rates        # RBI Current Rates snapshot (data/rates.json) for the dashboard
import store        # durable history backend (sqlite / Postgres / Turso)

# The two RBI feeds the app reads — both are shown together in one wire, each item
# tagged with its source `label`. Each carries its own live RSS URL, durable store
# category, and committed history file. Override any URL via env (mirror/cache or
# local testing) without code changes.
FEEDS = {
    "press": dict(
        name="Press Releases",            # friendly name shown in the Sources filter
        url=os.environ.get("MARKETWIRE_FEED", feed.RBI_FEED),
        category="press",
        history_url_env="MARKETWIRE_HISTORY_URL",
        history_path=history.HISTORY_PATH,
        label="RBI - Press Release",       # per-item source tag
    ),
    "notifications": dict(
        name="Notifications",
        url=os.environ.get("MARKETWIRE_NOTIFICATIONS_FEED", feed.RBI_NOTIFICATIONS_FEED),
        category="notifications",
        history_url_env="MARKETWIRE_NOTIFICATIONS_URL",
        history_path=history.NOTIFICATIONS_PATH,
        label="RBI - Notifications",
    ),
}
FEED_NAMES = [cfg["name"] for cfg in FEEDS.values()]
IST = timezone(timedelta(hours=5, minutes=30))
# The wire re-runs itself on this interval (seconds) with no clicks; override via env.
REFRESH_SECONDS = int(os.environ.get("MARKETWIRE_REFRESH", "300"))
CACHE_TTL = max(REFRESH_SECONDS - 30, 15)  # just under the interval so each tick re-fetches


def _is_archived(it):
    """Archive-sourced (date-only, no precise time) iff its ts is midnight IST."""
    ts = it.get("ts")
    if not ts:
        return False
    dt = datetime.fromtimestamp(ts, IST)
    return not (dt.hour or dt.minute or dt.second)


def _masthead_html():
    """The newspaper nameplate: kicker rule, serif wordmark, section line, rules."""
    today = datetime.now(IST).strftime("%A, %d %B %Y")
    return (
        "<div class='mw-masthead'>"
        f"<div class='mw-kicker'><span>Reserve Bank of India · Wire</span><span>{today} · IST</span></div>"
        "<div class='mw-wordmark'>MarketWire</div>"
        "<div class='mw-sub'>Press Releases &amp; Notifications</div>"
        "<div class='mw-rule'></div><div class='mw-rule-thin'></div>"
        "</div>"
    )


def _story_card_html(it):
    """Inner HTML for one story card: source tag, time, serif headline, and a
    CSS-clamped summary preview. The full body lives in the expander beside it."""
    ts = it.get("ts")
    dt = datetime.fromtimestamp(ts, IST) if ts else None
    archived = dt is not None and not (dt.hour or dt.minute or dt.second)
    if dt and not archived:
        when = dt.strftime("%d %b %Y · %H:%M IST")      # real time (live RSS)
    elif dt:
        when = dt.strftime("%d %b %Y")                  # date only (archive)
    else:
        when = html.escape(it.get("published") or "—")
    src = it.get("source") or ""
    notif = "notif" if "Notification" in src else "press"
    arch = " <span class='mw-tag'>ARCHIVE</span>" if archived else ""
    summary = (it.get("summary") or "").strip()
    preview = html.escape(summary[:340]) if summary else "<span class='mw-nosum'>Open for the full text →</span>"
    title = html.escape(it.get("title") or "(untitled)")
    link = it.get("link") or ""
    # Linkify the headline + show an explicit 'open' link, so the RBI page is one
    # click away without expanding. Only for real http links (archive stubs may lack one).
    if link.startswith("http"):
        href = html.escape(link, quote=True)
        head = f"<a href='{href}' target='_blank' rel='noopener'>{title}</a>"
        open_link = f"<a class='mw-open' href='{href}' target='_blank' rel='noopener'>Open on rbi.org.in ↗</a>"
    else:
        head, open_link = title, ""
    return (
        "<div class='mw-card'>"
        f"<div class='mw-meta'><span class='mw-src {notif}'>{html.escape(src)}</span>"
        f"<span class='mw-time'>{when}{arch}</span></div>"
        f"<div class='mw-head'>{head}</div>"
        f"<div class='mw-sum'>{preview}</div>"
        f"{open_link}"
        "</div>"
    )


def _relative_time(ts):
    """Trading-Economics-style relative stamp ('91 seconds ago', '16 minutes ago').
    Archive items (date-only) fall back to the date; very old items show the date."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, IST)
    if not (dt.hour or dt.minute or dt.second):        # archive: no real time
        return dt.strftime("%d %b %Y")
    secs = max(0, int((datetime.now(IST) - dt).total_seconds()))
    if secs < 60:
        v, unit = secs, "second"
    elif secs < 3600:
        v, unit = secs // 60, "minute"
    elif secs < 86400:
        v, unit = secs // 3600, "hour"
    elif secs < 30 * 86400:
        v, unit = secs // 86400, "day"
    else:
        return dt.strftime("%d %b %Y")
    return f"{v} {unit}{'s' if v != 1 else ''} ago"


def _stream_row_html(it):
    """One full-width stream row (Trading Economics style): the headline is the link
    (no separate 'open' link); the body is clamped small with an inline Show more /
    Show less toggle (pure <details>) when long; then a relative timestamp."""
    ts = it.get("ts")
    dt = datetime.fromtimestamp(ts, IST) if ts else None
    archived = dt is not None and not (dt.hour or dt.minute or dt.second)
    src = it.get("source") or ""
    notif = "notif" if "Notification" in src else "press"
    arch = "<span class='mw-tag'>ARCHIVE</span>" if archived else ""
    title = html.escape(it.get("title") or "(untitled)")
    link = it.get("link") or ""
    if link.startswith("http"):
        href = html.escape(link, quote=True)
        head = f"<a class='mw-shead' href='{href}' target='_blank' rel='noopener'>{title}</a>"
    else:
        head = f"<span class='mw-shead'>{title}</span>"
    text = (it.get("summary") or "").strip()
    if not text:
        body = "<div class='mw-sbody mw-sbody-plain'><span class='mw-nosum'>Open the headline for the full text.</span></div>"
    elif len(text) > 240:                       # long: clamp to a preview + expand
        body = ("<details class='mw-det'><summary>"
                f"<span class='mw-sbody'>{html.escape(text)}</span>"
                "<span class='mw-more'></span></summary></details>")
    else:                                       # short: show it all, no toggle
        body = f"<div class='mw-sbody mw-sbody-plain'>{html.escape(text)}</div>"
    return (
        "<div class='mw-stream'>"
        f"<div class='mw-srow'>{head}<span class='mw-stags'><span class='mw-src {notif}'>{html.escape(src)}</span>{arch}</span></div>"
        f"{body}"
        f"<div class='mw-sfoot'><span class='mw-time'>{_relative_time(ts)}</span></div>"
        "</div>"
    )


def _stream_html(items):
    """The whole single-column stream as one HTML blob (fewer Streamlit elements)."""
    return "<div class='mw-streamwrap'>" + "".join(_stream_row_html(it) for it in items) + "</div>"


def _pct(v, dec=2):
    return f"{v:.{dec}f}%" if isinstance(v, (int, float)) else "—"


def _rng(pair, dec=2, suffix="%"):
    """A [low, high] band -> 'low–high%' (or a single value if both ends match)."""
    if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(x, (int, float)) for x in pair)):
        return "—"
    lo, hi = pair
    return f"{lo:.{dec}f}{suffix}" if lo == hi else f"{lo:.{dec}f}–{hi:.{dec}f}{suffix}"


def _sig(lab, val, sub="", cls="", href=None, chg_html="", sub_raw=None):
    """A signal-strip tile. `chg_html` is a pre-built (already-safe) coloured % span rendered
    next to the value; `sub_raw` is pre-built (already-safe) HTML for the sub line (overrides
    `sub`, e.g. EUR/GBP with their own coloured % changes); `href` turns the whole tile into a
    chart link (e.g. the TE FX page)."""
    if sub_raw is not None:
        sub_block = f"<div class='sub'>{sub_raw}</div>"
    elif sub:
        sub_block = f"<div class='sub'>{html.escape(sub)}</div>"
    else:
        sub_block = ""
    inner = (f"<div class='lab'>{html.escape(lab)}</div>"
             f"<div class='val'>{html.escape(val)}{chg_html}</div>{sub_block}")
    if href:
        return (f"<a class='mw-sig mw-siglink {cls}' href='{html.escape(href)}' "
                f"target='_blank' rel='noopener'>{inner}</a>")
    return f"<div class='mw-sig {cls}'>{inner}</div>"


def _rrow(k, v):
    return f"<div class='mw-rrow'><span class='mw-rk'>{html.escape(k)}</span><span class='mw-rv'>{html.escape(v)}</span></div>"


def _fx_chg_html(pct):
    """A coloured (up/down/flat) '+0.20%' span for a signed % change, or '' if unknown."""
    if not isinstance(pct, (int, float)):
        return ""
    txt, cls = _cmd_change(pct)
    return f"<span class='chg mw-{cls}'>{html.escape(txt)}</span>"


def _fx_rrow(lab, value_str, te=None):
    """An Exchange Rates row. For a TE-sourced pair (`te` dict carrying a chart_url) the value
    is a chart link followed by a coloured % vs previous close; otherwise a plain _rrow."""
    if te and te.get("chart_url"):
        link = (f"<a class='mw-fxlink' href='{html.escape(te['chart_url'])}' "
                f"target='_blank' rel='noopener'>{html.escape(value_str)}</a>")
        return (f"<div class='mw-rrow'><span class='mw-rk'>{html.escape(lab)}</span>"
                f"<span class='mw-rv'>{link}{_fx_chg_html(te.get('change_pct'))}</span></div>")
    return _rrow(lab, value_str)


def _panel(title, rows, note=""):
    note_html = f"<div class='mw-rnote'>{html.escape(note)}</div>" if note else ""
    return f"<div class='mw-rpanel'><h4>{html.escape(title)}</h4>{''.join(rows)}{note_html}</div>"


def _benchmark_gsec(gsecs):
    """Pick the ~10-year benchmark: the G-Sec whose maturity year is closest to 10y out."""
    target = datetime.now(IST).year + 10
    best = None
    for g in gsecs or []:
        m = re.search(r"(20\d{2})", g.get("security", ""))
        if not m or not isinstance(g.get("yield"), (int, float)):
            continue
        dist = abs(int(m.group(1)) - target)
        if best is None or dist < best[0]:
            best = (dist, g)
    return best[1] if best else None


def _rates_dashboard_html(r):
    """The equity-desk Current Rates dashboard: a signal strip (key rates + a live
    next-MPC countdown) over an expandable full rate card. Built from data/rates.json."""
    pol = r.get("policy_rates") or {}
    res = r.get("reserve_ratios") or {}
    fx = r.get("exchange_rates") or {}
    fx_te = fx.get("fx_te") or {}            # USD/EUR/GBP enrichment (TE: % vs prev close + chart)
    lend = r.get("lending_deposit_rates") or {}
    mkt = r.get("market_trends") or {}
    cap = mkt.get("capital_market") or {}

    # --- signal strip ---
    sigs = [
        _sig("Policy Repo", _pct(pol.get("repo_rate")), "RBI key rate"),
        _sig("SDF · MSF", f"{_pct(pol.get('standing_deposit_facility_rate'))} · {_pct(pol.get('marginal_standing_facility_rate'))}", "LAF corridor"),
        _sig("CRR · SLR", f"{_pct(res.get('crr'))} · {_pct(res.get('slr'))}", "reserve ratios"),
    ]
    usd = fx.get("inr_per_usd")
    usd_val = f"{usd:,.4f}" if isinstance(usd, (int, float)) else "—"
    usd_te = fx_te.get("inr_per_usd") or {}
    eur = fx.get("inr_per_eur"); gbp = fx.get("inr_per_gbp")
    eur_te = fx_te.get("inr_per_eur") or {}; gbp_te = fx_te.get("inr_per_gbp") or {}
    # EUR/GBP sit in the USD tile's sub line — show each with its own coloured % vs prev close.
    fx_sub = " · ".join(s for s in (
        f"EUR {eur:,.2f}{_fx_chg_html(eur_te.get('change_pct'))}" if isinstance(eur, (int, float)) else "",
        f"GBP {gbp:,.2f}{_fx_chg_html(gbp_te.get('change_pct'))}" if isinstance(gbp, (int, float)) else "") if s) or "FBIL ref"
    sigs.append(_sig("USD / INR", usd_val, href=usd_te.get("chart_url"),
                     chg_html=_fx_chg_html(usd_te.get("change_pct")), sub_raw=fx_sub))
    bench = _benchmark_gsec(mkt.get("gsec_yields"))
    if bench:
        sigs.append(_sig("10Y G-Sec", _pct(bench.get("yield"), 4), bench.get("security", "")))
    elif isinstance(cap.get("sensex"), (int, float)):
        sigs.append(_sig("Sensex", f"{cap['sensex']:,.2f}", "S&P BSE"))

    # --- MPC countdown ---
    cd = rates.mpc_countdown(r)
    if cd:
        label, days = cd
        if days > 1:
            sub = f"in {days} days"
        elif days == 1:
            sub = "tomorrow"
        elif days == 0:
            sub = "today"
        elif days >= -3:                       # within the multi-day meeting window
            sub = "in progress"
        else:
            sub = "decision out"
        sigs.append(_sig("Next MPC", label, sub, cls="mpc"))
    else:
        sigs.append(_sig("Next MPC", "TBA", "schedule pending", cls="mpc"))

    # --- full rate card panels ---
    panels = [
        _panel("Policy Rates", [
            _rrow("Policy Repo Rate", _pct(pol.get("repo_rate"))),
            _rrow("Standing Deposit Facility", _pct(pol.get("standing_deposit_facility_rate"))),
            _rrow("Marginal Standing Facility", _pct(pol.get("marginal_standing_facility_rate"))),
            _rrow("Bank Rate", _pct(pol.get("bank_rate"))),
            _rrow("Fixed Reverse Repo", _pct(pol.get("fixed_reverse_repo_rate"))),
        ]),
        _panel("Reserve Ratios", [
            _rrow("CRR", _pct(res.get("crr"))),
            _rrow("SLR", _pct(res.get("slr"))),
        ]),
        _panel("Lending / Deposit", [
            _rrow("Base Rate", _rng(lend.get("base_rate"))),
            _rrow("MCLR (Overnight)", _rng(lend.get("mclr_overnight"))),
            _rrow("Savings Deposit Rate", _pct(lend.get("savings_deposit_rate"))),
            _rrow("Term Deposit > 1 Year", _rng(lend.get("term_deposit_rate_gt_1yr"))),
        ]),
    ]
    fx_rows = []
    for lab, key, dec in [("INR / 1 USD", "inr_per_usd", 4), ("INR / 1 GBP", "inr_per_gbp", 4),
                          ("INR / 1 EUR", "inr_per_eur", 4), ("INR / 100 JPY", "inr_per_100_jpy", 4),
                          ("INR / 1 AED", "inr_per_aed", 4), ("INR / 10000 IDR", "inr_per_10000_idr", 4)]:
        v = fx.get(key)
        vs = f"{v:,.{dec}f}" if isinstance(v, (int, float)) else "—"
        fx_rows.append(_fx_rrow(lab, vs, fx_te.get(key)))     # USD/EUR/GBP get a chart link + % change
    # Mixed-source note: USD/EUR/GBP are Trading Economics now; the rest are RBI's FBIL reference.
    fbil = " · ".join(s for s in (fx.get("as_of") or "",
                                  f"Source: {fx.get('source')}" if fx.get("source") else "") if s)
    fx_note = " · ".join(s for s in (
        "USD/EUR/GBP: Trading Economics (intraday, % vs prev close)" if fx_te else "",
        (f"JPY/AED/IDR: {fbil}" if fx_te else fbil) if fbil else "") if s)
    panels.append(_panel("Exchange Rates", fx_rows, note=fx_note))

    mm = mkt.get("money_market") or {}
    trend_rows = [_rrow("Call Money Rate", _rng(mm.get("call_rate")))]
    for g in (mkt.get("gsec_yields") or []):
        if isinstance(g.get("yield"), (int, float)):
            trend_rows.append(_rrow(g.get("security", "G-Sec"), _pct(g["yield"], 4)))
    tb = mkt.get("tbill_yields") or {}
    for lab, key in [("91-day T-Bill", "91_day"), ("182-day T-Bill", "182_day"), ("364-day T-Bill", "364_day")]:
        if isinstance(tb.get(key), (int, float)):
            trend_rows.append(_rrow(lab, _pct(tb[key], 4)))
    if isinstance(cap.get("sensex"), (int, float)):
        trend_rows.append(_rrow("S&P BSE Sensex", f"{cap['sensex']:,.2f}"))
    if isinstance(cap.get("nifty_50"), (int, float)):
        trend_rows.append(_rrow("Nifty 50", f"{cap['nifty_50']:,.2f}"))
    mkt_note = " · ".join(s for s in (mkt.get("gsec_tbill_as_on") or "", cap.get("as_on") or "") if s)
    panels.append(_panel("Market Trends", trend_rows, note=mkt_note))

    captured = r.get("captured_at") or ""
    try:
        captured = datetime.fromisoformat(captured).strftime("%d %b %Y, %H:%M IST")
    except (ValueError, TypeError):
        pass
    sub = f"snapshot · {captured}" if captured else "snapshot"
    return (
        "<div class='mw-rates'>"
        f"<div class='mw-rates-hd'><span class='t'>Current Rates</span><span class='s'>{html.escape(sub)} · rbi.org.in · FX: tradingeconomics.com</span></div>"
        f"<div class='mw-sigstrip'>{''.join(sigs)}</div>"
        f"<details><summary></summary><div class='mw-ratesgrid'>{''.join(panels)}</div></details>"
        "</div>"
    )


def _cmd_num(v):
    """Format a commodity price by magnitude — big numbers get thousands separators, small
    ones (e.g. copper in $/lb) keep precision. None -> em dash."""
    if not isinstance(v, (int, float)):
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:,.2f}"
    return f"{v:,.3f}"


def _cmd_change(pct):
    """('+1.23%', 'up') / ('-0.84%', 'down') / ('0.00%', 'flat') / ('—', 'flat')."""
    if not isinstance(pct, (int, float)):
        return "—", "flat"
    cls = "up" if pct > 0 else "down" if pct < 0 else "flat"
    return f"{pct:+.2f}%", cls


def _cmd_card(c):
    """One commodity tile: name·unit, price, coloured % vs previous close, and the whole
    card links out to its chart (Trading Economics). 'monthly'/as-of date in the subtext."""
    chg, cls = _cmd_change(c.get("change_pct"))
    tag = "monthly" if c.get("cadence") == "monthly" else (c.get("as_of") or "")
    sub = " · ".join(s for s in (tag, "chart ↗") if s)
    url = c.get("chart_url") or "#"
    return (
        f"<a class='mw-cmd' href='{html.escape(url)}' target='_blank' rel='noopener'>"
        f"<div class='lab'>{html.escape(c.get('name', ''))} "
        f"<span class='u'>{html.escape(c.get('unit', ''))}</span></div>"
        f"<div class='val'>{html.escape(_cmd_num(c.get('price')))}</div>"
        f"<div class='chg mw-{cls}'>{html.escape(chg)}</div>"
        f"<div class='sub'>{html.escape(sub)}</div>"
        "</a>"
    )


def _commodities_dashboard_html(snap):
    """The Commodities strip: a tile per commodity with price, % change vs the previous
    close (green/red), and a direct chart link. Built from data/commodities.json."""
    rows = snap.get("commodities") or []
    if not rows:
        return ""
    captured = snap.get("captured_at") or ""
    try:
        captured = datetime.fromisoformat(captured).strftime("%d %b %Y, %H:%M IST")
    except (ValueError, TypeError):
        pass
    sub = f"snapshot · {captured}" if captured else "awaiting first snapshot"
    return (
        "<div class='mw-cmds-wrap'>"
        f"<div class='mw-rates-hd'><span class='t'>Commodities</span>"
        f"<span class='s'>{html.escape(sub)} · % vs prev close · charts: Trading Economics</span></div>"
        f"<div class='mw-cmds'>{''.join(_cmd_card(c) for c in rows)}</div>"
        "</div>"
    )

# --------------------------------------------------------------------------- #
# Themes — flagship palettes, each tuned for contrast so ALL text (headlines, body,
# timestamps, captions, inputs, links) stays legible.
#   bg=page  panel=cards/inputs/sidebar  text=body  heading=headlines
#   muted=timestamps/captions  accent=primary brand (Press tag, hover, focus)
#   accent2=secondary (Notifications tag)  link=hyperlinks  border=hairlines
#   shadow=card hover glow (rgba)  headfont=masthead/headline font stack
# --------------------------------------------------------------------------- #
_SERIF = "'Source Serif 4', Georgia, 'Times New Roman', serif"   # newspaper headlines
_SANS = "'Libre Franklin', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"  # data-site headlines
_MONO = "'JetBrains Mono','SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace"  # rate/ticker numerics
# `up`/`down` = gain/loss greens & reds for the rates dashboard, tuned per palette.
THEMES = {
    "Bloomberg": dict(bg="#0A0A0A", panel="#161513", text="#E8E2D6", heading="#FBF7EF",
                      muted="#9A9080", accent="#FF9E1B", accent2="#4FC3E8", link="#6FD0EE",
                      border="#2A2622", shadow="rgba(255,158,27,.12)", headfont=_SERIF,
                      up="#2EBD85", down="#E5484D"),
    "Reuters": dict(bg="#FFFFFF", panel="#FFFFFF", text="#15171C", heading="#08090B",
                    muted="#5C616B", accent="#FB6400", accent2="#0B6EFD", link="#0A66C2",
                    border="#E5E7EB", shadow="rgba(15,23,42,.10)", headfont=_SERIF,
                    up="#0F9D58", down="#D93025"),
    "Paper": dict(bg="#FBFAF4", panel="#FFFFFF", text="#1C1B17", heading="#12110D",
                  muted="#6A6456", accent="#B3471B", accent2="#1D4ED8", link="#1D4ED8",
                  border="#E7E1D2", shadow="rgba(80,60,30,.10)", headfont=_SERIF,
                  up="#1E7A46", down="#B3261E"),
    # Trading Economics-inspired: soft-grey page, white cards, navy headlines,
    # signal blue/green accents, crisp sans (no serif) — a markets-data look.
    "Trading Economics": dict(bg="#EEF2F6", panel="#FFFFFF", text="#1C2A38", heading="#14304F",
                              muted="#6A7889", accent="#0E72BC", accent2="#13A36B", link="#0E72BC",
                              border="#DCE3EB", shadow="rgba(16,48,90,.12)", headfont=_SANS,
                              up="#13A36B", down="#D64550"),
    "High Contrast": dict(bg="#000000", panel="#0C0C0C", text="#FFFFFF", heading="#FFFF00",
                          muted="#D8D8D8", accent="#FFE000", accent2="#5AD1FF", link="#6BD8FF",
                          border="#5C5C5C", shadow="rgba(255,224,0,.20)", headfont=_SERIF,
                          up="#00E000", down="#FF5555"),
    # Equity Terminal: dark trading-desk palette — charcoal page, terminal-green press
    # accent, amber notifications, monospace numerics. Pairs with the Current Rates
    # dashboard for an equity-investor look.
    "Equity Terminal": dict(bg="#0A0D12", panel="#11161D", text="#C7D0DB", heading="#EEF3F8",
                            muted="#6B7785", accent="#16C784", accent2="#E8B339", link="#46B3FF",
                            border="#1E2630", shadow="rgba(22,199,132,.14)", headfont=_SANS,
                            up="#16C784", down="#F0616D"),
}
DEFAULT_THEME = "Equity Terminal"

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_feed(url):
    """Cached wrapper around feed.fetch_rss. Returns (items, error)."""
    return feed.fetch_rss(url)


@st.cache_data(ttl=600, show_spinner=False)
def load_history(url_env, default_path):
    """Durable history from the repo for one feed: the committed JSONL, or a raw URL
    via the feed's env var. Cached (keyed on its args); the Refresh button clears it."""
    return history.load_durable(url_env=url_env, default_path=default_path)


@st.cache_data(ttl=600, show_spinner=False)
def load_rates_cached():
    """The committed Current Rates snapshot (data/rates.json). Cached; Refresh clears it."""
    return rates.load_rates()


@st.cache_data(ttl=600, show_spinner=False)
def load_commodities_cached():
    """The committed Commodities snapshot (data/commodities.json). Cached; Refresh clears it."""
    return commodities.load_commodities()


def theme_css(p):
    """Build a full CSS override for palette `p` — masthead, card grid, subtle
    animations, and every themed widget (forcing portaled overlays too)."""
    return f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;500;600;700;800&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700;8..60,900&display=swap');

      /* ---- surfaces ---- */
      .stApp {{ background-color: {p['bg']}; color: {p['text']};
        font-family: 'Libre Franklin', system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
      [data-testid="stHeader"] {{ background: transparent; }}
      [data-testid="stToolbar"] {{ right: .5rem; }}

      /* ---- header / sidebar chrome — Streamlit's toolbar icons, the 3-dot main menu,
         its popover, the Deploy/Share button and the sidebar collapse/expand arrow inherit
         Streamlit's OWN base-theme colours (not this palette), so on some themes they go
         near-invisible (e.g. a light collapse arrow on a light sidebar). Force them all to
         palette-contrasting colours — `heading` for icons, `panel`+`text` for the menu —
         with GLOBAL selectors (the popover/arrow are portaled outside .stApp). ---- */
      [data-testid="stDecoration"] {{ background: {p['accent']}; }}
      [data-testid="stMainMenuButton"], [data-testid="stMainMenuButton"] svg,
      [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapseButton"] button,
      [data-testid="stSidebarCollapseButton"] svg,
      [data-testid="stExpandSidebarButton"], [data-testid="stExpandSidebarButton"] svg {{
        color: {p['heading']} !important; fill: {p['heading']} !important; opacity: 1 !important; }}
      [data-testid="stMainMenuButton"]:hover, [data-testid="stSidebarCollapseButton"] button:hover,
      [data-testid="stExpandSidebarButton"]:hover {{ background: {p['bg']} !important; }}
      [data-testid="stAppDeployButton"] button {{
        color: {p['heading']} !important; background: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      [data-testid="stAppDeployButton"] button:hover {{ border-color: {p['accent']} !important; }}
      /* the main-menu (3-dot) dropdown — portaled, so style it globally for every theme */
      [data-testid="stMainMenuPopover"] {{ background: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      [data-testid="stMainMenuPopover"] > div, [data-testid="stMainMenuList"] {{ background: transparent !important; }}
      [data-testid="stMainMenuPopover"] [role="menuitem"],
      [data-testid="stMainMenuPopover"] [role="menuitemcheckbox"],
      [data-testid="stMainMenuItemLabel"] {{ color: {p['text']} !important; background: transparent !important; }}
      [data-testid="stMainMenuPopover"] [role="menuitem"]:hover,
      [data-testid="stMainMenuPopover"] [role="menuitemcheckbox"]:hover {{ background: {p['bg']} !important; }}
      [data-testid="stMainMenuPopover"] kbd {{ color: {p['muted']} !important; background: {p['bg']} !important; border-color: {p['border']} !important; }}
      [data-testid="stMainMenuDivider"] {{ border-color: {p['border']} !important; background: {p['border']} !important; }}

      .block-container {{ padding-top: 1.1rem; max-width: 1280px; }}
      section[data-testid="stSidebar"] > div {{ background-color: {p['panel']}; border-right: 1px solid {p['border']}; }}

      /* ---- newspaper masthead ---- */
      .mw-masthead {{ text-align: center; margin: 0 0 .3rem; animation: mwFade .5s ease both; }}
      .mw-kicker {{ display: flex; justify-content: space-between; align-items: center;
        font-size: 11px; letter-spacing: .16em; text-transform: uppercase; font-weight: 600;
        color: {p['muted']}; border-top: 1px solid {p['border']}; border-bottom: 1px solid {p['border']}; padding: 7px 2px; }}
      .mw-wordmark {{ font-family: {p['headfont']}; font-weight: 900;
        font-size: clamp(38px, 6.5vw, 74px); line-height: 1.02; letter-spacing: -.02em;
        color: {p['heading']}; margin: .16em 0 .05em; }}
      .mw-wordmark::first-letter {{ color: {p['accent']}; }}
      .mw-sub {{ font-size: 12px; letter-spacing: .34em; text-transform: uppercase;
        color: {p['muted']}; font-weight: 600; }}
      .mw-rule {{ height: 3px; background: {p['heading']}; margin: .55rem 0 0; }}
      .mw-rule-thin {{ height: 1px; background: {p['border']}; margin-top: 2px; }}

      /* ---- headings & body ---- */
      .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {{ color: {p['heading']};
        font-family: {p['headfont']}; }}
      .stApp p, .stApp li, .stApp strong,
      [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
      [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
      section[data-testid="stSidebar"] label {{ color: {p['text']}; }}
      [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {p['muted']} !important; }}

      /* ---- story cards: the bordered containers in the grid ---- */
      [data-testid="stVerticalBlockBorderWrapper"] {{
        background: {p['panel']}; border: 1px solid {p['border']} !important; border-radius: 10px;
        height: 100%; overflow: hidden;
        transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        animation: mwRise .45s ease both; }}
      [data-testid="stVerticalBlockBorderWrapper"]:hover {{
        transform: translateY(-3px); border-color: {p['accent']} !important;
        box-shadow: 0 12px 30px {p['shadow']}; }}
      @keyframes mwRise {{ from {{ opacity: 0; transform: translateY(9px); }} to {{ opacity: 1; transform: none; }} }}
      @keyframes mwFade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}

      .mw-card {{ padding: 15px 16px 4px; }}
      .mw-meta {{ display: flex; align-items: center; gap: 10px; margin-bottom: 9px; }}
      .mw-src {{ font-size: 10px; font-weight: 800; letter-spacing: .07em; text-transform: uppercase;
        padding: 3px 8px; border-radius: 3px; color: {p['bg']}; background: {p['accent']}; white-space: nowrap; }}
      .mw-src.notif {{ background: {p['accent2']}; }}
      .mw-time {{ font-size: 11px; color: {p['muted']}; letter-spacing: .02em; margin-left: auto; text-align: right; }}
      .mw-head {{ font-family: {p['headfont']}; font-weight: 700; font-size: 17px;
        line-height: 1.24; color: {p['heading']}; margin: 0 0 8px; }}
      /* clickable headline -> opens the RBI source in a new tab (keep heading colour) */
      [data-testid="stMarkdownContainer"] .mw-head a {{ color: {p['heading']} !important; text-decoration: none; }}
      [data-testid="stMarkdownContainer"] .mw-head a:hover {{ color: {p['accent']} !important; text-decoration: underline; }}
      .mw-sum {{ font-size: 13px; line-height: 1.5; color: {p['muted']};
        display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
      .mw-nosum {{ font-style: italic; opacity: .8; }}
      /* always-visible 'open original' link on the card face (no expand needed) */
      [data-testid="stMarkdownContainer"] .mw-open {{ display: inline-block; margin: 11px 0 2px;
        font-size: 12px; font-weight: 600; color: {p['link']} !important; }}
      [data-testid="stMarkdownContainer"] .mw-open:hover {{ text-decoration: underline; }}

      /* ---- stream layout: single-column feed (Trading Economics style) ---- */
      .mw-stream {{ padding: 16px 4px 15px; border-bottom: 1px solid {p['border']}; animation: mwRise .4s ease both; }}
      .mw-stream:first-child {{ border-top: 1px solid {p['border']}; }}
      .mw-srow {{ display: flex; align-items: flex-start; gap: 14px; }}
      [data-testid="stMarkdownContainer"] .mw-shead {{ flex: 1; font-family: {p['headfont']}; font-weight: 700;
        font-size: 19px; line-height: 1.3; color: {p['heading']} !important;
        text-decoration: underline; text-underline-offset: 3px; text-decoration-thickness: 1px; }}
      [data-testid="stMarkdownContainer"] .mw-shead:hover {{ color: {p['accent']} !important; }}
      .mw-stags {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; flex: none; max-width: 46%; margin-top: 4px; }}
      .mw-stags .mw-tag {{ margin-left: 0; }}
      .mw-sbody {{ font-size: 14.5px; line-height: 1.62; color: {p['text']}; margin: 9px 0 0; }}
      .mw-sbody-plain {{ margin: 9px 0 11px; }}
      .mw-det {{ margin: 0; }}
      .mw-det > summary {{ list-style: none; cursor: pointer; outline: none; }}
      .mw-det > summary::-webkit-details-marker {{ display: none; }}
      .mw-det .mw-sbody {{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }}
      .mw-det[open] .mw-sbody {{ -webkit-line-clamp: unset; display: block; overflow: visible; }}
      .mw-more {{ display: inline-block; margin: 6px 0 11px; font-size: 12px; font-weight: 600; color: {p['accent']}; }}
      .mw-more::after {{ content: "Show more ▾"; }}
      .mw-det[open] .mw-more::after {{ content: "Show less ▴"; }}
      .mw-sfoot {{ display: flex; align-items: center; gap: 16px; }}
      .mw-sfoot .mw-time {{ margin: 0; }}
      .mw-tag {{ font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
        color: {p['muted']}; border: 1px solid {p['border']}; border-radius: 3px; padding: 1px 5px; margin-left: 7px; }}

      /* expander = the in-card 'Full text' toggle (strip its default chrome) */
      [data-testid="stExpander"] {{ border: none !important; background: transparent !important; }}
      [data-testid="stExpander"] details {{ border: none !important; background: transparent !important; }}
      [data-testid="stExpander"] summary {{ color: {p['accent']}; font-size: 12px; font-weight: 600; padding: 4px 16px; }}
      [data-testid="stExpander"] summary:hover {{ color: {p['accent']}; opacity: .78; }}
      [data-testid="stExpander"] summary p {{ color: {p['accent']} !important; font-weight: 600; }}
      [data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {{ color: {p['text']}; font-size: 13.5px; line-height: 1.55; }}

      /* links */
      .stApp a, [data-testid="stMarkdownContainer"] a {{ color: {p['link']} !important; text-decoration: none; font-weight: 600; }}
      .stApp a:hover {{ text-decoration: underline; }}

      /* ---- inputs ---- */
      .stTextInput div[data-baseweb="input"],
      .stTextInput div[data-baseweb="base-input"] {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      .stTextInput input {{ background-color: {p['panel']} !important; color: {p['text']} !important; -webkit-text-fill-color: {p['text']}; }}
      .stTextInput input::placeholder {{ color: {p['muted']} !important; -webkit-text-fill-color: {p['muted']}; opacity: 1; }}
      [data-testid="InputInstructions"] {{ color: {p['muted']} !important; }}
      .stDateInput div[data-baseweb="input"], .stDateInput div[data-baseweb="base-input"] {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      .stDateInput input {{ background-color: {p['panel']} !important; color: {p['text']} !important; -webkit-text-fill-color: {p['text']}; }}

      /* selectbox / multiselect — closed control */
      [data-baseweb="select"] > div {{ background-color: {p['panel']} !important; border-color: {p['border']} !important; }}
      [data-baseweb="select"] div {{ color: {p['text']} !important; }}
      [data-baseweb="select"] svg {{ fill: {p['muted']}; }}
      /* multiselect chips */
      span[data-baseweb="tag"] {{ background-color: {p['accent']} !important; color: {p['bg']} !important; border-radius: 4px; }}
      span[data-baseweb="tag"] span {{ color: {p['bg']} !important; }}
      span[data-baseweb="tag"] svg {{ fill: {p['bg']} !important; }}

      /* portaled overlays (dropdowns, calendar) live outside .stApp — force globally */
      [data-baseweb="popover"] > div, [data-baseweb="menu"], ul[role="listbox"] {{
        background-color: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      li[role="option"], [role="option"] {{ background-color: {p['panel']} !important; color: {p['text']} !important; }}
      li[role="option"]:hover, [role="option"][aria-selected="true"] {{ background-color: {p['border']} !important; color: {p['accent']} !important; }}
      /* date-picker calendar */
      [data-baseweb="calendar"], [data-baseweb="calendar"] > div {{ background-color: {p['panel']} !important; }}
      [data-baseweb="calendar"] * {{ color: {p['text']} !important; }}
      [data-baseweb="calendar"] [aria-selected="true"] {{ background-color: {p['accent']} !important; color: {p['bg']} !important; }}
      [data-baseweb="calendar"] [aria-selected="true"] * {{ color: {p['bg']} !important; }}

      /* tooltips */
      [data-testid="stTooltipContent"] {{ background-color: {p['panel']} !important; border: 1px solid {p['border']} !important; }}
      [data-testid="stTooltipContent"], [data-testid="stTooltipContent"] * {{ color: {p['text']} !important; }}

      /* radio (sort order) selected dot */
      [role="radiogroup"] [aria-checked="true"] div:first-child {{ background-color: {p['accent']} !important; border-color: {p['accent']} !important; }}

      /* buttons */
      .stButton > button {{ background-color: {p['panel']}; color: {p['text']}; border: 1px solid {p['border']};
        border-radius: 6px; font-weight: 600; transition: all .15s ease; }}
      .stButton > button:hover {{ color: {p['bg']}; background: {p['accent']}; border-color: {p['accent']}; }}

      .stApp hr {{ border-color: {p['border']}; }}

      /* ---- Current Rates dashboard (equity desk) ---- */
      .mw-rates {{ margin: 4px 0 20px; animation: mwFade .5s ease both; }}
      .mw-rates-hd {{ display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; margin: 0 0 11px; }}
      .mw-rates-hd .t {{ font-family: {p['headfont']}; font-weight: 800; font-size: 16px;
        letter-spacing: .02em; color: {p['heading']}; }}
      .mw-rates-hd .s {{ font-family: {_MONO}; font-size: 11px; color: {p['muted']}; }}
      .mw-sigstrip {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }}
      @media (max-width: 1100px) {{ .mw-sigstrip {{ grid-template-columns: repeat(3, 1fr); }} }}
      @media (max-width: 640px) {{ .mw-sigstrip {{ grid-template-columns: repeat(2, 1fr); }} }}
      .mw-sig {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 9px;
        padding: 11px 13px; transition: border-color .18s ease, transform .18s ease; }}
      .mw-sig:hover {{ transform: translateY(-2px); border-color: {p['accent']}; }}
      .mw-sig .lab {{ font-family: {_MONO}; font-size: 10px; letter-spacing: .07em;
        text-transform: uppercase; color: {p['muted']}; }}
      .mw-sig .val {{ font-family: {_MONO}; font-size: 21px; font-weight: 700; line-height: 1.08;
        margin-top: 5px; color: {p['heading']}; }}
      .mw-sig .sub {{ font-family: {_MONO}; font-size: 11px; color: {p['muted']}; margin-top: 4px; }}
      .mw-sig.mpc {{ border-color: {p['accent']}; box-shadow: 0 0 0 1px {p['accent']} inset; }}
      .mw-sig.mpc .val {{ color: {p['accent']}; }}
      /* TE-sourced FX (USD/EUR/GBP): the USD signal tile links out to its TE chart and shows a
         coloured % vs previous close inline next to the rate (reusing up/down/flat gain-loss tones). */
      .mw-siglink {{ text-decoration: none; color: inherit; display: block; }}
      .mw-sig .val .chg {{ font-size: 12px; font-weight: 700; margin-left: 7px; }}
      .mw-sig .sub .chg {{ font-weight: 700; margin-left: 4px; }}   /* EUR/GBP % in the USD tile sub */
      .mw-ratesgrid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 12px; }}
      @media (max-width: 1100px) {{ .mw-ratesgrid {{ grid-template-columns: 1fr; }} }}
      .mw-rpanel {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 13px 15px; }}
      .mw-rpanel h4 {{ font-family: {_MONO}; font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
        color: {p['muted']}; margin: 0 0 9px; padding-bottom: 7px; border-bottom: 1px solid {p['border']}; }}
      .mw-rrow {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline;
        padding: 4px 0; border-bottom: 1px dotted {p['border']}; }}
      .mw-rrow:last-child {{ border-bottom: none; }}
      .mw-rk {{ font-size: 13px; color: {p['text']}; }}
      .mw-rv {{ font-family: {_MONO}; font-size: 13px; font-weight: 700; color: {p['heading']}; white-space: nowrap; }}
      /* Exchange Rates: TE pairs render the rate as a dotted chart link + a coloured % change. */
      .mw-fxlink {{ color: inherit; text-decoration: none; border-bottom: 1px dotted {p['muted']}; }}
      .mw-fxlink:hover {{ color: {p['accent']}; border-bottom-color: {p['accent']}; }}
      .mw-rv .chg {{ font-size: 11.5px; font-weight: 700; margin-left: 8px; }}
      .mw-rnote {{ font-size: 10.5px; color: {p['muted']}; margin-top: 8px; font-style: italic; }}
      .mw-rates > details > summary {{ list-style: none; cursor: pointer; outline: none; width: max-content;
        font-family: {_MONO}; font-size: 11px; font-weight: 700; letter-spacing: .04em; color: {p['accent']}; margin-top: 13px; }}
      .mw-rates > details > summary::-webkit-details-marker {{ display: none; }}
      .mw-rates > details > summary::after {{ content: "▾ FULL RATE CARD"; }}
      .mw-rates > details[open] > summary::after {{ content: "▴ HIDE RATE CARD"; }}
      /* Commodities strip — tiles link out to each commodity's chart; % vs prev close
         is coloured with the palette's gain/loss (up/down) tones. */
      .mw-cmds-wrap {{ margin: 4px 0 20px; animation: mwFade .5s ease both; }}
      .mw-cmds {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(116px, 1fr)); gap: 8px; }}
      .mw-cmd {{ display: block; text-decoration: none; background: {p['panel']}; border: 1px solid {p['border']};
        border-radius: 9px; padding: 10px 11px; transition: border-color .18s ease, transform .18s ease; }}
      .mw-cmd:hover {{ transform: translateY(-2px); border-color: {p['accent']}; }}
      .mw-cmd .lab {{ font-family: {_MONO}; font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
        color: {p['text']}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
      .mw-cmd .lab .u {{ color: {p['muted']}; }}
      .mw-cmd .val {{ font-family: {_MONO}; font-size: 18px; font-weight: 700; line-height: 1.1;
        margin-top: 5px; color: {p['heading']}; }}
      .mw-cmd .chg {{ font-family: {_MONO}; font-size: 12.5px; font-weight: 700; margin-top: 3px; }}
      .mw-cmd .sub {{ font-family: {_MONO}; font-size: 10px; color: {p['muted']}; margin-top: 4px; }}
      .mw-up {{ color: {p['up']}; }}
      .mw-down {{ color: {p['down']}; }}
      .mw-flat {{ color: {p['muted']}; }}
    </style>
    """


st.set_page_config(page_title="MarketWire · RBI", page_icon="◢", layout="wide")
store.init_db()

# --- theme selection ---------------------------------------------------------
# Bind the selectbox to session_state via `key` and seed it once from the URL.
# (Passing index= from the query param fought the widget's own state, so a new
# pick only applied on the *next* rerun — i.e. you had to click twice.) Reading
# st.session_state["theme"] means the choice takes effect on the first click.
names = list(THEMES)
if "theme" not in st.session_state:
    _qp = st.query_params.get("theme")
    st.session_state["theme"] = _qp if _qp in names else DEFAULT_THEME
# Seed the Sources multiselect once from the URL (?sources=Press Releases,Notifications),
# same first-click pattern as the theme. Absent param → all feeds selected.
if "sources" not in st.session_state:
    _qs = st.query_params.get("sources")
    if _qs is None:
        st.session_state["sources"] = FEED_NAMES
    else:
        st.session_state["sources"] = [s for s in _qs.split(",") if s in FEED_NAMES]
LAYOUTS = ["Stream", "Grid"]
if "layout" not in st.session_state:
    _ql = st.query_params.get("layout")
    st.session_state["layout"] = _ql if _ql in LAYOUTS else "Stream"
with st.sidebar:
    st.markdown("### ◢ MarketWire")
    sources = st.multiselect(
        "Sources", FEED_NAMES, key="sources",
        help="Which RBI feeds to show — keep all selected, or pick one/some individually.",
    )
    layout = st.radio(
        "Layout", LAYOUTS, key="layout", horizontal=True,
        help="Stream = single-column feed (Trading Economics style). Grid = card grid.",
    )
    st.divider()
    theme = st.selectbox("Theme", names, key="theme", help="Five flagship palettes — all text stays legible in each.")
    st.caption("Switch the look to suit your screen / lighting.")
    st.divider()
    st.checkbox(
        "Show archive (older, date-only)", value=True, key="archive",
        help="Show older releases backfilled from RBI's listing — full text, but date "
             "only (RBI's pages don't expose a publish time). Off = live RSS items only.",
    )
    st.checkbox(
        "Show rates dashboard", value=True, key="rates_dash",
        help="RBI Current Rates (policy/reserve/exchange/lending rates, market trends) "
             "and the next MPC-meeting countdown, above the wire.",
    )
    st.checkbox(
        "Show commodities", value=True, key="commodities_dash",
        help="Free commodity prices (Brent, gold, silver, copper, aluminium, zinc, steel, "
             "iron ore, coffee) with % change vs the previous close and a direct chart link.",
    )
st.query_params["theme"] = theme  # keep the URL in sync (shareable / sticky)
st.query_params["sources"] = ",".join(sources)
st.query_params["layout"] = layout
st.markdown(theme_css(THEMES[theme]), unsafe_allow_html=True)

# --- masthead ----------------------------------------------------------------
st.markdown(_masthead_html(), unsafe_allow_html=True)
_spacer, refresh = st.columns([6, 1])
if refresh.button("⟳ Refresh", use_container_width=True):
    fetch_feed.clear()
    load_history.clear()
    load_rates_cached.clear()
    load_commodities_cached.clear()
    st.rerun()

# --- Current Rates dashboard (equity desk) -----------------------------------
# Read once from the committed snapshot (rarely changes), above the live wire.
if st.session_state.get("rates_dash", True):
    _rates = load_rates_cached()
    if _rates:
        st.markdown(_rates_dashboard_html(_rates), unsafe_allow_html=True)

# --- Commodities strip (free prices · % vs prev close · chart links) ----------
if st.session_state.get("commodities_dash", True):
    _cmds = load_commodities_cached()
    if _cmds:
        st.markdown(_commodities_dashboard_html(_cmds), unsafe_allow_html=True)

@st.fragment(run_every=REFRESH_SECONDS)
def wire():
    """The live wire. As a fragment with run_every, it re-runs itself every
    REFRESH_SECONDS with no clicks — only this part of the page, so the theme and
    header stay put. fetch_feed is cached just under the interval, so each tick
    pulls fresh data. (st.stop() can't be used in a fragment, so we return.)"""
    # Pull each SELECTED feed, tag every item with its source label, then interleave
    # them into one newest-first wire. Each feed is deduped on its OWN ids (prid / Id)
    # BEFORE combining, so a press-release prid never merges with a notification Id
    # that happens to share the number. Archive items (older, date-only, full body)
    # come pre-enriched from the repo history the Action maintains.
    selected = st.session_state.get("sources", FEED_NAMES)
    active = [cfg for cfg in FEEDS.values() if cfg["name"] in selected]
    if not active:
        st.info("No sources selected — pick at least one feed in the sidebar.")
        return
    items, new_count, errors = [], 0, []
    for cfg in active:
        rss_items, error = fetch_feed(cfg["url"])
        if error:
            errors.append(error)
        new_count += store.upsert(rss_items or [], category=cfg["category"])  # accumulate live RSS in the DB
        feed_items = history.dedupe(
            store.load(limit=5000, category=cfg["category"])
            + load_history(cfg["history_url_env"], cfg["history_path"])
            + (rss_items or [])
        )
        for it in feed_items:
            it["source"] = cfg["label"]
        items += feed_items
    if not st.session_state.get("archive", True):
        items = [it for it in items if not _is_archived(it)]

    if not items:
        if errors:
            st.error(f"Couldn't fetch the feeds: {errors[0]}")
            st.caption(
                "Government sites sometimes block datacenter IPs (e.g. Streamlit Cloud). "
                "Try Refresh, or run it locally where the feeds are reachable."
            )
        else:
            st.info("Nothing stored yet — try ⟳ Refresh.")
        return

    bits = []
    if new_count:
        bits.append(f"{new_count} new this fetch")
    if errors:
        bits.append("live feed unreachable — showing stored")
    note = (" · " + " · ".join(bits)) if bits else ""

    # --- controls: keyword · sort order · optional date range ----------------
    c_kw, c_sort, c_date = st.columns([3, 2, 2])
    q = c_kw.text_input("Filter", placeholder="filter by keyword…", label_visibility="collapsed")
    order = c_sort.radio(
        "Sort order", ["Newest first", "Oldest first"], horizontal=True,
        label_visibility="collapsed", key="order",
        help="Sort the wire by date — newest first (descending) or oldest first (ascending).",
    )
    # Date filtering is opt-in (default off) so newly-published items always show
    # without having to widen a range; bounds track the items currently loaded.
    dated = [it["ts"] for it in items if it.get("ts")]
    date_on = c_date.checkbox(
        "Filter by date", key="date_filter", disabled=not dated,
        help="Limit the wire to a date range you pick.",
    )
    lo = hi = None
    if date_on and dated:
        dmin = datetime.fromtimestamp(min(dated), IST).date()
        dmax = datetime.fromtimestamp(max(dated), IST).date()
        picked = st.date_input(
            "Date range", value=(dmin, dmax), format="DD/MM/YYYY",
            key="date_range", label_visibility="collapsed",
        )
        pair = picked if isinstance(picked, (list, tuple)) else (picked,)
        if len(pair) == 2:
            lo, hi = pair
        elif len(pair) == 1:
            lo = hi = pair[0]
        if lo and hi and lo > hi:           # tolerate a reversed pick
            lo, hi = hi, lo

    shown = items
    if q.strip():
        shown = [it for it in shown if q.lower() in (it["title"] + " " + (it["summary"] or "")).lower()]
    if lo and hi:
        shown = [it for it in shown
                 if it.get("ts") and lo <= datetime.fromtimestamp(it["ts"], IST).date() <= hi]
    reverse = order == "Newest first"
    shown = sorted(shown, key=lambda x: x.get("ts") or 0, reverse=reverse)

    order_txt = "newest first" if reverse else "oldest first"
    range_txt = f" · {lo:%d %b %Y}–{hi:%d %b %Y}" if (lo and hi) else ""
    mins = REFRESH_SECONDS // 60
    every = f"{mins} min" if mins else f"{REFRESH_SECONDS}s"
    checked = datetime.now(IST).strftime("%H:%M:%S")
    st.caption(
        f"{len(shown)} of {len(items)} stored items · {order_txt}{range_txt}{note} · "
        f"auto-refresh every {every} · last checked {checked} IST"
    )

    # --- render: Stream (single-column feed) or Grid (3-up card grid) ---------
    RENDER_CAP = 120          # bound the DOM; narrow with filters to reach the rest
    visible = shown[:RENDER_CAP]
    if len(shown) > RENDER_CAP:
        st.caption(f"Showing the first {RENDER_CAP} of {len(shown)} — narrow with the filters above to see the rest.")

    if st.session_state.get("layout", "Stream") == "Grid":
        CARD_COLS = 3
        for row in range(0, len(visible), CARD_COLS):
            cols = st.columns(CARD_COLS, gap="small")
            for col, it in zip(cols, visible[row:row + CARD_COLS]):
                with col:
                    with st.container(border=True):
                        st.markdown(_story_card_html(it), unsafe_allow_html=True)
                        with st.expander("Full text"):
                            body = (it.get("summary") or "").strip()
                            st.write(body or "(full text not stored yet — use the link above to open it on rbi.org.in)")
    else:
        st.markdown(_stream_html(visible), unsafe_allow_html=True)


wire()
