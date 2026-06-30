# MarketWire — Automation status

_Snapshot: 2026-06-30. Based on live GitHub Actions run history + the committed
`data/*` files, not just the code._

MarketWire has **no server and no database** — every piece of live data is refreshed by a
scheduled GitHub Action that scrapes a source and **commits the result back into the repo**.
Streamlit Cloud then redeploys from `main` and reads the committed files. This is a summary
of which of those automated pipelines are healthy and which are degraded, best-effort, or
manual.

## TL;DR

| Pipeline | Cadence | Status | Notes |
|---|---|---|---|
| RBI feeds → `history.jsonl` / `notifications.jsonl` | 30 min | ✅ Working | RSS reachable from CI (`rss=10`/feed); 224 runs, 100% green |
| Commodities → `commodities.json` | 30 min | ✅ Working | 9/9 fresh (TE×8 + Yahoo×1) on latest run |
| FX overlay (USD/EUR/GBP-INR) → `rates.json` | 30 min | ✅ Working | 3/3 fresh from Trading Economics |
| RBI Current Rates → `rates.json` | Daily | ✅ Working | Scraped + committed 06-28 & 06-29 (`5.25% repo`) |
| 30-min cadence itself (external cron) | — | ⚠️ Fragile | Depends on an off-repo cron + PAT; GitHub's own scheduler is unreliable |
| Daily scrape **timing** | — | ⚠️ Imprecise | Scheduler drift fires it hours late (still same-day, so OK) |
| MPC meeting dates in `rates.json` | Daily | ✅ Working | Browser-scraped from RBI's MPC page (`rates_scrapling.py`) — validated in CI (parsed "August 3, 4 and 5, 2026") |
| Silent-staleness alerting | — | ❌ Missing | Blocked/partial scrapes keep old data and still pass green |

## ✅ What's working

### 1. RBI feed history (`history.yml` → `poll.py`)
- **224 runs, all green.** The 30-min poll fetches both RBI RSS feeds, merges/dedupes, and
  commits `data/history.jsonl` (press releases, keyed by `prid`) and
  `data/notifications.jsonl` (keyed by `Id`).
- **RBI is reachable from GitHub's runners.** The latest run logged
  `[poll:press releases] … rss=10` and `[poll:notifications] … rss=10` — the live RSS is
  answering CI IPs, so this is *not* falling back to stored-only. (`new=0` just means those
  10 items were already captured; backfill is in steady state, `stored_archive=0`.)
- Commits only when something changed, so the history is durable and survives Streamlit
  restarts with no external DB.

### 2. Commodities (`poll.py` → `commodities.poll_commodities`)
- Rides the same 30-min cron. Latest run: **`9 of 9 fresh — Trading Economics×8, Yahoo
  Finance×1`** (Steel is pinned to Yahoo `HRC=F`; the other 8 come from TE).
- Self-guarding: writes only on a complete, in-bounds parse; never raises.

### 3. FX overlay — USD/EUR/GBP-INR (`poll.py` → `rates.poll_fx`)
- Also on the 30-min cron. Latest run: **`3/3 fresh from TE/Yahoo — USD/INR 94.846`**.
- Touches only the three TE pairs inside `rates.json`; the FBIL JPY/AED/IDR scalars and the
  rest of the rates snapshot are left untouched.

### 4. RBI Current Rates daily snapshot (`rates.yml` → `rates.py`)
- 4 runs since 2026-06-26, all green. It **did successfully scrape and commit** on 06-28
  (`rates updated (5.25% repo)` → `rates: update`) and 06-29 — so the auto path is live, not
  just the manual Claude-for-Chrome fallback documented in `CLAUDE.md`.
- Self-guarding: writes only on a complete, in-bounds parse, so a blocked/partial scrape
  preserves the last good snapshot and the MPC block.

## ⚠️ Risks & partial gaps

### A. The 30-min cadence depends on an off-repo cron + token
GitHub's native scheduler is best-effort and drops frequent crons, so the workflows' built-in
`schedule` is only a **sparse 6-hour fallback**. The real 30-min cadence comes from an
**external cron service** (e.g. cron-job.org) hitting the `workflow_dispatch` API with a
fine-grained PAT (see README → "Reliable 30-min cadence"). Evidence: nearly every recent
history run is `event: workflow_dispatch`, with only the occasional `schedule` run mixed in.
**Single points of failure:** if that cron service goes down or the **PAT expires**, the
cadence silently collapses to once every 6 hours with no error anywhere.

### B. Daily-rates timing drifts (still functional)
`rates.yml` is meant to fire at 13:30 IST (08:00 UTC), just after RBI's 1 pm FBIL FX post.
Actual scheduled fire times were 09:59 / 10:23 / 12:26 UTC on 06-27/28/29 — hours of GitHub
scheduler drift. They still land in the afternoon IST, so each run captures the **same day's**
rates and the snapshot is correct; the precise "just after 1 pm" intent just isn't honored.
For exact timing, point the external cron at `rates.yml`'s `workflow_dispatch` too.

### C. No alerting on silent staleness
All three intraday scrapers (`poll_fx`, `poll_commodities`) and the daily `rates.poll_rates`
**write only on a good parse and never raise**. The only hard failure (red run + email) is
when *every* RBI feed ends up empty. So a partial degradation — TE changes its table markup,
Yahoo starts 403-ing the runner, RBI's home page accordion changes — would **keep serving the
last committed values while every run stays green**. Symptoms to watch for instead of relying
on a red ❌:
- A commodity/FX `as_of` that stops advancing (e.g. Iron Ore is currently `2026-06-29` while
  the rest are `2026-06-30` — TE served a stale iron-ore row, which was preserved as designed).
- `rates.json`'s top-level `captured_at` not advancing day to day.

### D. MPC dates are manual
The MPC block (`next_meeting_start/end`) isn't on RBI's home page, so it's **not scraped** —
it's preserved across runs and hand-maintained (currently `2026-08-03 → 2026-08-05`). After
the August meeting it will go stale until someone updates it (the `prompts/rbi-rates-refresh.md`
browser run is the intended way to refresh it).

### E. Can't validate scrapers from a cloud sandbox / CI dev box
RBI 403s datacenter IPs, TE sits behind Cloudflare, and Yahoo 403s some datacenter IPs — so
scraper changes can't be verified from this sandbox. They must be validated from a real
desk/VM (or by reading the actual Action run logs, as this snapshot does).

## ✋ Manual (by design, not broken)
- **RBI Current Rates source-of-truth refresh** via a Claude-for-Chrome run
  (`prompts/rbi-rates-refresh.md`) — a backstop for when the daily auto-scrape can't parse.
- **Theme/markup screenshot QA** for `streamlit_app.py` — manual headless-Chromium harness.

## Update — Scrapling browser scraper (addresses D & E)

A browser-rendered RBI scraper was added to automate the genuinely-manual gap:
- **`rates_scrapling.py`** — uses [Scrapling](https://github.com/D4Vinci/Scrapling) to render the
  RBI home-page **rates accordion** and the **MPC schedule page** in a real browser (Chromium via
  `DynamicFetcher`, stealth Firefox via `StealthyFetcher` as the anti-bot fallback), then feeds the
  HTML to the *same* `rates.parse_rates()` the static scraper uses. It writes `data/rates.json`
  through the existing guards (`_is_complete` + `_merge`), updates **MPC dates** when a plausible
  future date parses, and **preserves the Trading-Economics FX overlay**. Never raises.
- **`.github/workflows/rates-scrapling.yml`** — runs it daily on GitHub's runners (where RBI egress
  is open), sharing the `marketwire-rates` concurrency group with `rates.yml` so they never race.
- **Validated in CI** (2026-06-30, two `workflow_dispatch` runs): Scrapling reaches live RBI from
  GitHub's runners — `Fetched (200) <GET https://www.rbi.org.in/>` and
  `Fetched (200) <GET .../FS_Overview.aspx?fn=2752>` — parsed `repo 5.25%` + `MPC: August 3, 4 and
  5, 2026`, committed a fresh snapshot (Jun 29 → Jun 30 data), and preserved the TE FX overlay.
- **Sandbox caveat:** the render can't be validated from the *dev sandbox* (the egress proxy is an
  allowlist — `rbi.org.in` *and* a neutral control site both get a gateway `403 connect_rejected`),
  which is why CI is the test bed. Parsing/merge/guard logic is also unit-tested locally (24/24).
- **Now that the browser path is proven**, `rates.yml` (static requests) is redundant and can be
  retired — the two coexist safely (shared concurrency group, guarded merges) until you do.

## Suggested follow-ups (optional)
1. Add a second external `workflow_dispatch` cron for `rates.yml` to remove the timing drift.
2. Emit a GitHub Actions `::warning::` (or fail) when a scraper preserves stale data N runs in
   a row, so silent staleness becomes visible.
3. Track PAT expiry (calendar reminder / dashboard) — it's the cadence's single point of failure.
4. Automate or schedule an MPC-dates refresh after each policy meeting.
