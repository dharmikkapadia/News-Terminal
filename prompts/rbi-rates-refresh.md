# Claude‑for‑Chrome prompt — refresh `data/rates.json` from RBI

RBI (`rbi.org.in`) **403s datacenter IPs** and its *Current Rates* box is a
JavaScript accordion, so the automated `rates.yml` scraper can't reach it and the
**Exchange Rates (JPY/AED/IDR) + Market Trends** part of the rate card only refreshes
when someone commits a new `data/rates.json`. This is the prompt that does that
end‑to‑end from a real browser — paste the block below into **Claude for Chrome**
(or save it as a scheduled task) and it will read RBI, merge onto the live file, and
commit, with **no manual editing**.

**When to run:** any time, but ideally **daily ≈ 1:30 pm IST** — just after RBI posts
its "1.00 pm" FBIL exchange-rate reference, so each run captures the same day's rates
(mirrors `.github/workflows/rates.yml`). Schedule it as a recurring Claude‑for‑Chrome
task for true "set and forget".

**What it must NOT touch** (these are maintained automatically by the 30‑min cron and
must be copied through unchanged): `exchange_rates.inr_per_usd`, `exchange_rates.inr_per_eur`,
`exchange_rates.inr_per_gbp`, the whole `exchange_rates.fx_te` block and
`exchange_rates.fx_te_captured_at`. USD/EUR/GBP are sourced from Trading Economics, not
RBI — overwriting them here would break the % change + chart links until the next poll.

> **Note — deploy branch.** The prompt reads and commits `data/rates.json` on the branch
> the live Streamlit app deploys from — **`main`**. Quick sanity check before relying on it:
> the raw URL in Step 1 must return **HTTP 200**, not 404. If it 404s, the app's branch
> changed — update `main` in the three GitHub URLs below to match.

---

## ▼ Paste everything below this line into Claude for Chrome ▼

You are updating the committed `data/rates.json` for the MarketWire app from the
**Reserve Bank of India** website. Work carefully and do not invent any numbers — only
report values you actually read on the page. Produce one final JSON object and commit it.

### Step 1 — Read the current committed file (for the fields you must preserve)

Open this raw file and keep its contents in mind:

    https://raw.githubusercontent.com/dharmikkapadia/news-terminal/main/data/rates.json

You will **copy these fields through unchanged** (they're maintained by automation, not RBI):
`exchange_rates.inr_per_usd`, `exchange_rates.inr_per_eur`, `exchange_rates.inr_per_gbp`,
the entire `exchange_rates.fx_te` object, and `exchange_rates.fx_te_captured_at`.
If you cannot read a required RBI value in Step 2/3 (panel missing, page blocked), keep
the existing value from this file rather than guessing or emitting null.

### Step 2 — Read RBI's "Current Rates" widget

Open **https://www.rbi.org.in/**. On the home page find the **Current Rates** widget — a
set of click‑to‑expand accordion panels. **Click each panel header to expand it** before
reading (the rows are hidden until expanded). Read these five panels:

1. **Policy Rates** — Policy Repo Rate, Standing Deposit Facility (SDF) Rate, Marginal
   Standing Facility (MSF) Rate, Bank Rate, Fixed Reverse Repo Rate.
2. **Reserve Ratios** — CRR, SLR.
3. **Exchange Rates** — INR per 1 USD, 1 GBP, 1 EUR, **100 JPY**, 1 AED, **10000 IDR**,
   plus the "As at 1.00pm of <date>" line and the "Source:" line (usually `FBIL`).
4. **Lending / Deposit Rates** — Base Rate, MCLR (Overnight), Savings Deposit Rate,
   Term Deposit Rate > 1 year.
5. **Market Trends**
   - *Money Market:* Call Rate (a band like "x.xx – y.yy %"; if RBI shows "‑%" / blank,
     use `null`) and its "as on <date>".
   - *Government Securities Market:* every benchmark G‑Sec line (e.g. "6.03% GS 2029")
     with its yield, the 91/182/364‑day T‑Bill yields, and the "as on <date>".
   - *Capital Market:* S&P BSE Sensex, Nifty 50, and the "as on <date>".

### Step 3 — Read the next MPC meeting date (not on the home page)

Open **https://www.rbi.org.in/scripts/FS_Overview.aspx?fn=2752** (Monetary Policy →
MPC meeting schedule). Find the **next upcoming** meeting and note its start date, end
date and the human label (e.g. "August 3, 4 and 5, 2026"). If you can't reach this page,
keep the `mpc` block from the file you read in Step 1 **as long as its
`next_meeting_start` is still today or in the future**; otherwise leave it as‑is and note
that it may be stale.

### Step 4 — Emit the complete JSON

Output a single JSON object **exactly** in this shape and order, filling RBI fields from
Steps 2–3 and copying the preserved fields from Step 1. Conventions:

- **All rates are plain numbers** — no `%`, no commas, no quotes. `5.25`, not `"5.25%"`.
- **Sensex / Nifty** are plain numbers too: `77100.47`, `24056.0` (drop the commas).
- **Bands** (`call_rate`, `base_rate`, `mclr_overnight`, `term_deposit_rate_gt_1yr`) are
  two‑element arrays `[low, high]`. A single published value becomes `[x, x]`. A blank
  call rate becomes `null` (not `[]`).
- **`gsec_yields`** is an array of `{"security": "<exact RBI label>", "yield": <number>}`,
  in the order RBI lists them.
- Copy every **"as on …"** / **"As at …"** string **verbatim** (keep RBI's wording/case).
- **`captured_at`** = the current timestamp in IST, e.g. `2026-06-29T13:32:00+05:30`.

```json
{
  "captured_at": "<now, ISO‑8601 with +05:30>",
  "source": "https://www.rbi.org.in/",
  "policy_rates": {
    "repo_rate": <num>,
    "standing_deposit_facility_rate": <num>,
    "marginal_standing_facility_rate": <num>,
    "bank_rate": <num>,
    "fixed_reverse_repo_rate": <num>
  },
  "reserve_ratios": { "crr": <num>, "slr": <num> },
  "exchange_rates": {
    "as_of": "As at 1.00pm of <Month> <day>, <year>",
    "source": "FBIL",
    "inr_per_usd": <PRESERVE from Step 1>,
    "inr_per_gbp": <PRESERVE from Step 1>,
    "inr_per_eur": <PRESERVE from Step 1>,
    "inr_per_100_jpy": <num from RBI>,
    "inr_per_aed": <num from RBI>,
    "inr_per_10000_idr": <num from RBI>,
    "fx_te": { <PRESERVE entire object from Step 1> },
    "fx_te_captured_at": "<PRESERVE from Step 1>"
  },
  "lending_deposit_rates": {
    "base_rate": [<lo>, <hi>],
    "mclr_overnight": [<lo>, <hi>],
    "savings_deposit_rate": <num>,
    "term_deposit_rate_gt_1yr": [<lo>, <hi>]
  },
  "market_trends": {
    "money_market": { "call_rate": [<lo>, <hi>] or null, "as_on": "as on <date>" },
    "gsec_yields": [
      { "security": "<exact RBI label>", "yield": <num> }
    ],
    "tbill_yields": { "91_day": <num>, "182_day": <num>, "364_day": <num> },
    "gsec_tbill_as_on": "as on <date>",
    "capital_market": { "sensex": <num>, "nifty_50": <num>, "as_on": "as on <date>" }
  },
  "mpc": {
    "next_meeting_start": "YYYY-MM-DD",
    "next_meeting_end": "YYYY-MM-DD",
    "as_text": "<human label>",
    "source_url": "https://www.rbi.org.in/scripts/FS_Overview.aspx?fn=2752"
  }
}
```

### Step 5 — Commit it to GitHub (no manual step)

1. Open the web editor: **https://github.com/dharmikkapadia/news-terminal/edit/main/data/rates.json**
2. Select all and replace the file contents with the JSON from Step 4.
3. Before committing, sanity‑check: it's valid JSON; the repo rate is ~4–8; USD/INR is
   40–200; Sensex is in the tens of thousands; and the `inr_per_usd/eur/gbp` + `fx_te`
   fields still match what you read in Step 1.
4. Commit to **`main`** with message: `rates: refresh RBI snapshot (Claude-for-Chrome)`.

Then report a one‑line summary of what changed (repo rate, USD as‑of date, Sensex/Nifty,
next MPC), or say clearly if RBI was unreachable and you committed nothing.

## ▲ End of prompt ▲

---

### Why this is safe

`data/rates.json` has two owners. The **30‑min `history.yml` cron** (`rates.poll_fx`) owns
the TE‑sourced FX — `inr_per_usd/eur/gbp` + `fx_te` — and this prompt copies those through
untouched, so the % change and chart links keep working. Everything else (policy/reserve
rates, JPY/AED/IDR, lending, market trends, MPC) is **RBI‑owned** and is what this prompt
refreshes. Even if a run wrote a slightly stale FX scalar, the next 30‑min poll would
re‑overlay TE — but copying Step 1's values through avoids even a brief mismatch.

If you'd rather not commit from the browser, stop after Step 4 and paste the JSON into
`data/rates.json` yourself; the guarded daily `rates.yml` scraper will never clobber it.
