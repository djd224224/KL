# Session Handoff — 2026-08-03 → 08-04 (IMM overhaul + credits reconciliation)

*Written 2026-08-04 ~00:50 ET for a fresh context window. Everything below is
committed. 309 tests green (`python -m unittest test_incentive_mm`).*

---

## TL;DR

- **Credits reconciliation is DONE** — the #1 open item since 7/30. Jack's
  statement: lifetime rewards **$11,564.87**, August **$1,368.50**.
- **IMM-attributable realization ≈ 0.83x** credited-to-date, **~1.0x** fully
  settled. My earlier "estimator runs 18% low" was **backwards** — see below.
- **Lifetime: RAW −$9,866 trading, +$11,565 rewards, NET +$1,699.**
  The business is: trading loses, rewards more than cover it.
- ~20 deploys today, all via the restart drill. Bot healthy, ~500 markets /
  40 events, budget $20k.

---

## THE BIG FINDING: reward estimate vs credited

Jack challenged a digest table showing `prior periods 1.18x` alongside
`lifetime 0.95x` — arithmetically impossible as two independent measurements.
A 7-agent workflow audit resolved it. **Two large errors of opposite sign in
different buckets, which nearly cancel in the lifetime row:**

| | $ | Lands in |
|---|---|---|
| Credited reward IMM never estimated | **~$1,500** | 100% "prior periods" |
| IMM estimate Kalshi hasn't paid yet | **~$2,400** | ~98% "this month" |
| Net | −$563 | = 11,564.87 − 12,128.27 |

**Why ~$1,500 is not IMM's:** Kalshi pays on the **account's aggregate book
presence** — one `user_id`, `subaccount_number=0`, no per-strategy
segregation, and **no per-strategy reward endpoint exists** (8 paths tried,
all 404). `reward_est_lifetime` starts **2026-07-11**; the account's first
fill was **2026-02-09**. So **152 of 176 credited days have no estimate term
at all**, and the KXHIGH weather bot rested on programmed markets throughout.
Measured by replaying that bot's reconstructed ladder (BigQuery
`Kalshi.KXHIGH_orders`) against **8,389 in-window order-book snapshots**
scored with IMM's own qualification-walk logic → ~$1,500 (band $700–$2,800),
against a $1,553.86 prior-period excess. Two numbers from disjoint data.

**Consequences:**
- Account-level 0.95x is **meaningless** (contaminated + offset).
- The month/prior split is **removed from the digest** — it was one
  measurement and a subtraction, so every month-boundary error landed on
  "prior" and inverted it. The month row spanned **0.39x–5.9x** across
  plausible statement cut dates (the statement's "as of" instant is recorded
  nowhere).
- `IMM_REWARDS_NON_IMM` (default 1500) configures the offset.

**Watch:** re-check the lifetime ratio weekly. If it stays well below 1.0
after August's in-flight programs pay, the post-8/2 estimator IS overstating.

---

## What changed in the bot (chronological, all committed)

### Temp (hourly weather) tuning arc
| Change | Value |
|---|---|
| Cutoff | close−15min → **close−10min** |
| Payout floor | $1 → **$0.70** (per-series `min_est_total`) |
| At-ref hysteresis | 1 tick → **0** (later made global) |
| Fast lane | **5s mini-cycles**, temp only |
| Reduce-only window | **removed bot-wide** (`PRE_CUTOFF_REDUCE_ONLY=0`) |
| Members vs cutoff | members quote to the **true** cutoff (5-min buffer is fresh-entry only) |

### Repricing
- **Amend-in-place** (`POST /portfolio/events/orders/{id}/amend`) — same
  `order_id`, no cancel→place gap. Was costing **1–9s dark per reprice**.
- **Asymmetric chase** — keep rungs when the reference moves *deeper*
  (identical full weight). Now inert globally since tolerance is 0, which is
  correct: every rung sits at the deepest full-weight price = less fill risk.
- TTL rewrites still cancel+place (amend can't extend expiration).

### Bands / pads / placement
- Member band **5–90 → 2–98 → 5–93** (settled at 5–93 after a 96c ask on a
  near-certain temp strike sold 60 into the close).
- **Per-side top-in-band**: only the out-of-band side stands down; both out =
  whole market down.
- **Deep-rung floor 2c** on healthy books (both touches in-band) so rungs can
  follow a sub-5c reference.
- **Pads**: 1c/99c, **≤1000 contracts**, only where the mid is inside 5–90,
  and only **≥2 ticks behind the touch** (`PAD_MIN_TICKS_BEHIND`).

### Universe / caps
| Knob | Value |
|---|---|
| `IMM_COLLATERAL_BUDGET` | 13000 → **20000** |
| `IMM_MAX_CANDIDATE_BOOKS` | 700 → **5000** |
| `IMM_MAX_MARKETS` (events) | 50 → **75** |
| `PAD_MAX_CONTRACTS` | 5000 → 800 → **1000** |
| `REF_DEPTH_SLOPE / MAX_MULT` | 0.1/2.0 → **0.25/3.0** (sim-confirmed) |
| `TOTAL_SIZE_MULT_CAP` | **5.0** (hour × ref product) |

### Re-entry (company / econ / gas / diesel / foot traffic)
- `FREEZE_SERIES` and `NO_NEW_SERIES` defaults **emptied**.
- All re-entry series get **$2/day rate floor** + **safe-join** (rest ≥2 ticks
  off the touch unless spread ≥5).
- **Horizon escape**: admits on `rate ≥ bar` **OR** `projected total ≥ $5`
  (`RATE_FLOOR_TOTAL_ALT`). **Projection is bounded by the PROGRAM end, not
  the market close** — foot traffic closes 9/7 but its programs end 8/9, so
  the window is 5 days and only 1 of 22 markets qualifies.
- **KXDIESELW** overridden to a $0 rate floor (all 21 strikes quote).
- **Diesel (KXDIESELD/W)** and **foot traffic (KXBKFT/KXYUMTBFT)** were never
  allowlisted at all — now enrolled.
- `IMM_FORCE_EVENTS` — per-event bypass for **Kalshi data bugs** (program
  period stamped far past the real event). Currently holds
  `KXTRUMPMENTION-26AUG05`. **Remove after it settles.**

### Cutoffs / safety
- **`apply_series_cutoff_adjustments` now takes `close_time`** and applies the
  close-anchored rule. **This was a live bug Jack caught**: temp markets the
  bot held inventory in (orphan-restored) got cutoff = *close* instead of
  close−10 and quoted into the final 10 minutes — 7 fills observed as late as
  **6.71 min to close**. Same "two producers" class as the 7/29 fix, one layer
  down. *Rule: the shared helper must contain EVERY cutoff rule, and there is
  now a test asserting it.*
- **AAA print blackout 03:05–04:00 ET** on KXAAAGASD/W/M + KXDIESELD/W.
  Observed post times (sniper, 5s resolution): **03:36, 03:18, 03:20, 03:22,
  03:25**. Dailies close 01:59 and were safe; **weeklies/monthlies stay open
  across the print** and measured **−14.4c/contract in hour 3**.

---

## Emails

### `imm_quote_gaps.py` — NEW, daily 7:20 AM ET ("KL imm quote-gaps")
Every live-incentive market the bot is NOT quoting, event-rolled, **ranked by
profit per minute**, each with a plain-English description
(`describe_event()`: "Hourly high temperature in Washington DC, 9am hour").
Why-not reasons: blocklisted/frozen, not in allowlist, no-new gate, yielded to
manual, screened:*, under payout/rate floor, capacity, candidate cap.
Config parity: parses the launcher `$ProbeEnv` **before** importing
incentive_mm (log line must say "mirrored N launcher env vars" — **if N=0 the
numbers are wrong**).

### `send_imm_digest.py` — 3 REAL BUGS FIXED, then restructured
1. **`reward_est_today` was never persisted** → every restart zeroed it.
   Measured 8/3: digest would report **$45.81** against a true **$959.47**.
   Now persisted with a roll-day key, plus `reward_history` (60 days) recording
   each completed roll-day.
2. **`summary_body` was empty** (the roll only fires if the process survives
   past 5am CT) → digest fell through to that same broken counter. Fallbacks
   now **label themselves** ("today so far (partial)").
3. **Settlement P&L was missing entirely** (audit finding from 7/23, never
   fixed) — a position held to settlement leaves the own-book, so it appeared
   in neither realized nor unrealized. First corrected run: **$1,554 across
   722 markets**. The digest had been green-washing losses.

Current layout: RAW/REWARD/NET × day/week/lifetime; **daily table back to
7/12, newest-first, with TOTAL**; past-day event breakdown with TOTAL.
Reconciliation block and detail lines removed at Jack's request.

- Lifetime RAW uses the bot's new **persisted `realized_lifetime`** counter
  (captures settlements of multi-week holds — fills can't, since they carry no
  `client_order_id` and `our_order_ids` prunes at 7 days).
- `imm_backfill_daily_pnl.py` (NEW) recovers **677,700 order ids from logs**,
  attributes 42,932 fills, writes `daily_pnl.json`. Re-run any time.
- Env on the DIGEST task: `IMM_REWARDS_CREDITED`, `IMM_REWARDS_CREDITED_MTD`.
  **Update from the statement.**

### `imm_earnings_overrides.py` — stale-ticker autofix + WH schedule
- **Stale-ticker trap autofix**: Kalshi stamps "next earnings call" events with
  a ticker date that goes stale → cutoff sits in the PAST → never quotes, never
  self-heals. `KXEARNINGSMENTIONPGR-26JUL15` sat dead on **12 markets ×
  $28.67/day**. Detector flags the signature, retries Nasdaq on a **120-day**
  horizon, and emails unresolved ones **with their pool cost**.
- **White House schedule resolver** (Roll Call Factbase JSON) for
  KXTRUMPMENTION events — validated live against a known 1:30pm EO signing.
  Requires a unique ≥2-word match or it stays unresolved (no guessing).

---

## MEASURED ECONOMICS (use these for decisions)

### Temp, by bucket — net $/market-hour
| Bucket | credited-to-date (0.83x) | fully settled (~1.0x) |
|---|---|---|
| Mid 15–35c | +2.57 | +3.41 |
| Mid 65–85c | +1.93 | +2.74 |
| Mid 1–15c | +1.93 | +2.68 |
| Mid 85–99c | +0.35 | +1.11 |
| **Mid 35–65c (ATM)** | **−1.12** | **−0.32** |
| NYC / CHI | +2.8 / +2.5 | +3.6 / +3.3 |
| **DC / LAX** | **−0.46 / −0.69** | +0.24 / +0.13 |

**ATM temp strikes lose money on every basis.** DC and LAX are break-even at
best. Whole temp book since 8/2: **+$549 to +$935** over 483 market-hours.

### Temp fill quality
- **Depth** (13,290 fills matched to the book at fill time): at touch −4.69c,
  1 tick −6.27c, 2 ticks −6.56c, 6–10 ticks −11.16c. **Deep fills are sweep
  fills.** *Caveat: this is cost CONDITIONAL ON FILLING; deep rungs fill less
  often, so it does NOT mean "stop quoting deep."*
- **Time in window**: 50+ min to close **−3.01c** vs 20–50 min **−5.2 to
  −5.9c**. **Early is ~half as toxic.** At-touch in the first 10 min: −2.3c.
- **Actionable**: a time-in-window size taper (bigger early, smaller
  mid-hour). Not built.

### Pads — NOT profitable, but negligibly cheap
9,873 pad orders → **6,103,700 contracts placed, 4,912 filled (0.080%)**,
**−1.00c/contract**, **$49.12 lifetime cost**. They're the precondition for a
thin side to qualify (under-target side = snapshot excluded = market pays
zero). Keep them; "profitable" was an error of mine.

### Family fill P&L (settled)
TEMP −5.0c/ct (65% of all volume), earnings-mention −1.9c, gas −4.7c,
diesel −0.65c, TRUMPMENTION **+2.1c**.

### AAA gas/diesel
Prints **03:18–03:36 ET**. Daily markets close 01:59 (safe). Daily gas/diesel
fills run **−1.81c/ct** overall — the dramatic intraday repricing (books open
at a uniform 50c and walk to extremes) has **not** translated into large
losses; safe-join is doing real work.

---

## OPEN ITEMS

1. **Cut ATM temp strikes (mid 35–65c)** — loses on every reward basis.
2. **Derisk DC / LAX temp** — 5x worse than NYC/CHI.
3. **Time-in-window size taper** for temp — early is half as toxic.
4. **Unquoted financial families** — the gaps email now ranks these top:
   Treasury yields (**$1,534/day pool per event × 5 tenors**), 15-min
   gold/oil/silver ($480/day each), FX. **~$700/day est unquoted total.**
   Real capital decision; not enrolled.
5. **Gas fair-value gate** — reuse `gas_bot.py`'s calibrated model the way the
   rain-fair gate works: stand aside when the touch diverges from fair.
6. **`KXMAMDANIMENTION-26AUG04`** flagged UNRESOLVED — needs a `--set` if you
   know the announcement time.
7. **Remove `KXTRUMPMENTION-26AUG05` from `IMM_FORCE_EVENTS`** after it
   settles (8/5 4:30pm ET).
8. **Weekly realization check** — see the big finding above.
9. Today's P&L carry ran **−$800** intraday against the $1,200 halt. The
   wider/faster regime is the cause; watch the digest's temp line.

---

## OPS

```powershell
# THE restart drill (mandatory)
Stop-ScheduledTask "KL incentive_mm"; sleep 20; <kill surviving python>; Start-ScheduledTask "KL incentive_mm"

python -m unittest test_incentive_mm          # 309 green
python send_imm_digest.py --test              # send digest now
python imm_quote_gaps.py --dry                # gaps email, print only
python imm_backfill_daily_pnl.py              # rebuild daily_pnl.json
python imm_earnings_overrides.py --dry
python imm_earnings_overrides.py --set EVENT "2026-08-05T16:30:00-04:00"
```

**Scheduled:** `KL incentive_mm` (at logon), `KL incentive_mm DIGEST` 7:10,
`KL imm quote-gaps` 7:20, `KL imm earnings-overrides` 6:45/12:45/16:45,
`KL gas snipe` 2:40.

**Deploy rule Jack set:** deploy in the temp-quiet window (**:50–:00**) so no
live hourly-temp quotes are disturbed — he overrode it himself twice with
"Launch now", but default to it.

**Key files:** `incentive_mm.py`, `send_imm_digest.py`, `imm_quote_gaps.py`,
`imm_backfill_daily_pnl.py`, `imm_earnings_overrides.py`,
`run_incentive_mm.ps1`, `run-logs/incentive-mm/{imm_state,daily_pnl,
event_start_overrides}.json`.

---

## LESSONS (classes, not incidents)

- **Shared helpers must contain EVERY rule.** The 7/29 "one tightener both
  producers run" helper was missing the close-anchored rule, so the same bug
  recurred. Add a test that asserts the helper's completeness, not just its
  behavior.
- **Derived rows are not measurements.** `prior = lifetime − month` absorbs
  every error in the month split and can inverted-signal. Don't present a
  subtraction as evidence.
- **Account-level ≠ strategy-level.** Kalshi pays the account. Any comparison
  of a per-bot estimate to an account credit is contaminated by every other
  bot and by all history before that bot existed.
- **Check the program end, not the market close.** They differ (foot traffic:
  close 9/7, program ends 8/9) and the difference was 8x on a projection.
- **Cost-conditional-on-fill ≠ cost of the strategy.** Deep rungs look awful
  per fill precisely because they only fill when swept.
