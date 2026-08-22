# KXB200WS — B200 GPU Rental Price Weekly: Market Research & Trading Strategy

*Research date: 2026-08-09 (Sun). All data pulled live from Kalshi + Ornn public APIs.*

## 1. What this market is

**KXB200WS "B200 Weekly"** — weekly ladder of binary strikes on the **NVIDIA B200
GPU rental price** ($/GPU-hour) as published by **Ornn** (Ornn Compute Price
Index, OCPI). Category Financials, tags AI/Compute. Quadratic (standard) fees.

- Each week: 13 strikes "Above $K", $0.50 apart, currently $3.00–$9.00
  (AUG28 is a legacy wide ladder, $3.00–$19.00, 33 strikes).
- Close: **Friday 20:00Z (4 PM ET)**. Strike type `greater` (strictly above),
  index rounded to 2dp.
- Settlement source: `dashboard.ornnai.com`. Rules fallback: *"If no data is
  published… the most recently available published data will be used."*
- Contract template (GPUA.pdf): point-in-time "at" evaluation; source hierarchy
  Ornn → TradingView → Trading Economics → Bloomberg; position accountability
  $25,000/strike (not binding at our size).
- Siblings (same index, cross-market structure): KXB200MS (monthly),
  KXB200MON, KXB200MAX, plus H100/H200/A100/RTX5090 weekly+monthly families.
- Kalshi launched the GPU "compute forward curves" 2026-07-14 aimed at
  **hedgers** (neoclouds/DCs vs AI labs) — i.e. structurally price-insensitive
  flow. Settled history exists since 26JUN19.

**Volume**: settled weeks did 22k–71k contracts/event; AUG14 already 32k with
26k OI on a Sunday. Books are 1–4c wide at the ATM strikes but **thin at touch**
(1–200 contracts), with 5–15k walls off-touch. This is a thin-MM market where a
correct model + patience gets paid.

## 2. The underlying index — the core facts

Ornn publishes **one B200 print per day, timestamped exactly 20:00:00Z**,
via a **free public API** (3-month history, no key):

```
GET https://dashboard.ornnai.com/api/gpu/B200/index-history
```

(Also `H100 SXM`, `H200`, `A100 SXM4`, `RTX 5090`. A live *intraday* price
endpoint exists at data.ornn.com but is paid/401 — optional upgrade later.)

**Settlement is the Friday 20:00Z print — proven**, not assumed: for every
settled week the Friday print falls in the settled bracket, and two weeks
discriminate Thursday vs Friday:
- JUL10: Fri=6.69 ∈ (6.50,7.00] settled bracket; Thu=6.15 would not.
- AUG07: Fri=5.76 settled ">5.75 = YES" **by one cent**; Thu=5.75 would not.

**Statistical character** (92 daily prints, 2026-05-10 → 2026-08-09):

- Level path: 3.86 (May 10) → peak 7.24 (Jul 17) → **5.70 today**. External
  press: $2.31 in early March, +114% in six weeks that spring. This thing
  moves 30–50% in multi-week runs, both directions.
- Daily changes: sd $0.19 (~3%), median |move| 1.5%.
- **Weekly (Fri→Fri) changes: sd $0.67, lag-1 autocorrelation +0.52.**
  Momentum is mechanical — it's a negotiated-transaction average that drifts
  toward recent deal levels. Runs: JUN down (−0.68, −0.76, −0.25), JUL up
  (+0.64, +1.55, +0.55), current down-run (−0.46, −0.37, −0.65).
- Weekday structure of daily moves (sd): Tue/Wed hottest (0.26/0.24),
  Fri 0.16, Sat 0.13, **Sun 0.06**. Weekends are near-frozen.
- Friday-only moves: sd $0.16 → by Thursday 4 PM the settlement value is one
  *small* step away.
- Cross-GPU daily correlation vs B200 is weak (H100 +0.26, H200 +0.20,
  RTX5090 +0.38): mostly idiosyncratic; siblings are separate books, not a
  hedge.

## 3. Why this market is beatable

1. **The crowd doesn't watch the print.** Today (Sunday) the index has ticked
   5.76 → 5.73 → 5.70 since Friday's settle, three weeks of −0.4/wk momentum
   are running, and the AUG14 book still prices P(>5.50)=62% and
   P(>5.00)=97.5%. The implied distribution is ~half the width history
   supports and ignores drift.
2. **Structural endgame**: settlement = a *daily* print at a *known hour*.
   After Thursday 20:00Z the final value is `Thu print + one Friday move
   (sd $0.16)`. Tail strikes still quoted 3–8c on Thursday evening are free
   money at that point; near-certain sides quoted 90–96c are cheap carry.
3. **Weekend staleness**: Sat+Sun prints reveal two data points while books
   sleep (Sun sd $0.06 ≈ the weekend prints are almost pure signal about
   where the week starts).
4. **Hedger flow**: Kalshi's own launch post targets capacity hedgers — flow
   that pays for immediacy and doesn't reprice ladders coherently.
5. **Liquidity is subsidized**: every KXB200WS strike carries a live
   **$15/market liquidity pool** (rolling ~2-week periods; target size 1000,
   50%-per-tick score discount). Live GPU-family pools as of today:
   **$16,275 total (~$1,164/day run-rate)**; KXB200WS alone $1,470 across 98
   markets (~$105/day). (`GET /trade-api/v2/incentive_programs`, public,
   paginate via `next_cursor`, reward in centi-cents.) IMM blocklists the GPU
   family for capacity reasons (incentive_mm.py:943) — the pools are sitting
   there.

## 4. Model

`b200_pricer.py` (repo root, stdlib-only, **read-only** — places no orders):

- **E[Friday]** = last print + EW-weighted mean daily drift (halflife 6d),
  with weekend days drift/vol-damped by the empirical weekday weights.
  Sanity: the model's AUG14 mean (5.42) matches the independent estimate
  `last Friday + 0.52 × last week's move` (5.76 − 0.34 ≈ 5.42).
- **Uncertainty**: Student-t(4) on drift residuals, weekday-weighted,
  sd × 1.25 inflation. AUG14 (5 days out): sd $0.55.
- **P(>K)** uses K+0.005 (2dp rounding of the index).

**Walk-forward validation** (13 settled Fridays × 13-strike ladder, Brier
score, lower=better, vs a no-drift random walk with the same vol):

| entry day | model | no-drift baseline |
|---|---|---|
| Mon (4d out) | 0.0572 | 0.0538 |
| Wed (2d out) | **0.0265** | 0.0285 |
| Thu (1d out) | **0.0135** | 0.0146 |

Momentum wins from midweek onward; at Monday leads the drift extrapolation
overshot in the July reversal week (n=13 weeks — treat all of this as
directionally informative, not precise). Practical reading: **full-size the
midweek/Thursday trades, half-size the weekend/Monday trades.**

## 5. The strategy — four stacked plays

### S1. Daily post-print reprice (the workhorse)
Every day at **20:01Z** pull the new print, recompute the fair curve, and
compare to live books. Act maker-first where |model − mid| ≥ 5–8c on strikes
with mid in [3c, 97c]:
- rest inside the spread on the rich side (sell YES rich / sell NO rich),
- never cross except when edge ≥ ~12c after the quadratic taker fee
  (fee ≈ 1.75c at 50c, 0.6c at 10c).
Tue/Wed prints move most — the Wednesday reprice is historically the
highest-information moment of the week (and where the model most clearly
beats no-drift).

### S2. Thursday→Friday endgame (the precision shot)
After Thursday's 20:00Z print, the final-day distribution is
`N(Thu + 0.08, 0.16)` (empirical Friday moves; slight up-bias). Then:
- **Sell tails > 2σ away** still bid 2c+ (either side). At AUG07 settle the
  5.75 strike came down to a 1-cent margin — strikes *near* the Thursday
  print stay genuinely uncertain; everything ≥ $0.40 away is ~decided.
- **Buy the near-certain side** where it's still ≤ 95c (5c on $0.40+ distance
  is ~free carry into next-day settlement).
This is the KXHIGH-endgame playbook transplanted to a cleaner underlying
(one deterministic print instead of noisy CLI reports).

### S3. Weekend-staleness Monday entry
Sat+Sun prints are near-deterministic reads on the week's starting level. If
the weekend confirms the run (like this weekend: 5.76→5.73→5.70), take the
stale side Sunday night/Monday before the crowd's first weekday reprice —
at half size (this is the lead where the model is only ~breakeven vs
no-drift; the entry exists because *books* are stalest here, not because the
model is sharpest).

### S4. Rewards overlay (subsidy, not thesis)
While running S1–S3 you're already resting orders; shape them to score LIP
credit: two-sided where you'd quote anyway, all-at-touch (50%/tick discount
kills deep ladders — same conclusion as IMM's 10/0/0 shape sim), and let the
far-tail strikes (1c/99c) be quoted only where capital-cheap side exists.
$105/day of KXB200WS pools split among few quoters; even 20–30% share ≈
$20–30/day riding on orders the alpha legs want anyway. Scaling to the whole
GPU family (~$1.1k/day of pools) is a separate, later decision — that's the
universe IMM deliberately excluded, and it needs its own bot, not an IMM
unblock.

## 6. Concrete mispricings right now (Sun 2026-08-09, index 5.70)

Model AUG14: E=5.42, sd=0.55. Book vs model:

| strike | book (yes) | model P(>K) | play | EV/contract |
|---|---|---|---|---|
| 5.00 | 97/98c | 70% | **BUY NO @ ~3c** (rest YES ask 98c) | **+26c** (model), +9c even at a conservative 12% |
| 5.50 | 61/64c | 45% | **BUY NO @ ~38c** (rest YES ask 62c) | **+14c** |
| 6.00 | 22/23c | 22.5% | none — priced right | — |
| 6.50–7.00 | 4–9c | 4–9% | none / slight YES lean, skip | — |

The signature: **the ATM strike is priced correctly, the "trend continues"
wing is not.** The market pays you specifically for underwriting
continuation of a 3-week trend that the weekend prints are already
confirming. Conservative sanity check on the 5.00 NO: unconditional
frequency of ≥$0.70 down-weeks is 2/12 ≈ 17% — call it 12% conditionally —
vs. a 2.5–3c price. That trade is +EV under any defensible parameterization;
size it as the anchor and treat 5.50 NO as the scale-out.

AUG21/AUG28 books price further decline (5.50 @ ~71c, 6.00 @ ~43c on AUG21)
— closer to model, smaller edges, and model uncertainty grows with horizon.
Front week is where the money is.

## 7. Execution notes

- **Maker-first always.** Taker fee is quadratic (1.75c/contract at 50c);
  passive fills are free. Touch sizes are 1–200 — size orders 50–200, replace
  patiently, let trend-extrapolating flow lift you.
- Capacity honestly: $50–200/week of premium at risk initially; expected
  $30–120/week from S1–S3 combined at current book depth, plus S4 subsidy.
  This is a **portfolio-of-small-edges** market like the rest of the fleet —
  the point is it recurs every week with a deterministic settlement.
- Position accountability $25k/strike — irrelevant at this size.
- Don't quote through a print: pull/requote around 20:00Z daily (the only
  scheduled information moment). No other intraweek event risk exists unless
  Ornn revises methodology.
- Weekly settlement is T+0 (30-min settlement timer after Friday close).

## 8. Risks

1. **Regime spike**: the +1.55 week (Jul 10→17) shows launches/capacity
   crunches can gap the index up violently. NO-side inventory on
   "trend-continuation" strikes loses when the trend V-bottoms. Mitigants:
   t(4) tails in the model, front-week-only concentration, kill switch:
   **any daily print ≥ 2.5× recent daily sd against your book → flatten
   forward weeks at market** (taker fee is cheap vs being run over).
2. **Small sample**: 92 daily prints, 13 weekly settlements. All parameters
   are provisional; re-fit weekly. The 3-month API window is a rolling
   archive problem — **start archiving prints daily** (gas-tracker pattern)
   so we build history Ornn's free tier won't serve later.
3. **Index methodology**: Ornn is a young provider; a methodology change,
   outage (fallback = stale print settles — actually favorable if you know
   it), or a Kalshi source-hierarchy fallback to TradingView would change
   the game. Watch the Ornn status page; the settlement-source memory from
   the earnings-override work applies: *the source of truth is what the
   rules say settles, not what looks right.*
4. **Thin books**: exiting mid-week costs the spread; plan to hold entries to
   settlement (premium-at-risk sizing, like rain_monthly ledger discipline).
5. **Competition**: the mispricing pattern (correct ATM, stale wings) says
   at least one competent participant already prices the mode. Edges will
   compress; the durable legs are S2 (structural) and S4 (subsidized).

## 9. Infrastructure plan (fits the fleet)

Phase 0 (now): run `python b200_pricer.py` after any 20:00Z print; manual
orders on flagged edges. Anchor trades: AUG14 5.00 NO / 5.50 NO per §6.

Phase 1: `b200_archiver` scheduled task at 20:05Z daily — append all 5 GPU
prints to `gas_data/`-style CSV (survives the 3-month API window; enables
honest re-fits).

Phase 2 (if Phase 0 pays for 2–3 weeks): `b200_trader.py` — S1/S2 as a
30–60s poll bot, DRY default + `--live` arm, singleton lock, watchdog,
halt-carry, email digest; V2 API client patterns from crypto_touch_mm.
Rewards-aware quoting (S4) folded in here, all-at-touch shape only.

Phase 3 (separate decision): extend to the full GPU family for the
~$1.1k/day pool universe — H200WS books look identical in structure; that's
a fleet-scale build reusing IMM's program-scoring machinery outside IMM's
process (respect the 7/23 capacity decision — new bot, not an unblock).

## 10. Data & reproducibility

- Ornn history: `https://dashboard.ornnai.com/api/gpu/{B200,H200,...}/index-history` (free, 3mo, daily 20:00Z prints; UA header required).
- Kalshi: public `/markets?series_ticker=KXB200WS`, `/incentive_programs`
  (paginate `next_cursor`; **rate-limit ~read-tier — pace ≥1s between calls,
  back off on 429**). Fields are the `_dollars`/`_fp` variants (post-rename).
- Session scratch data: `fetch_kalshi_b200.py`, `b200_model.py`,
  `ornn_*.json`, `live_incentive_programs.json`, `b200ws_candles.json` in
  the session scratchpad (candle backtest results appended below when run).

## 11. Backtest B — model vs actual Kalshi prices (settled weeks)

Setup: for every settled KXB200WS market, daily candle closes (bid/ask);
whenever |model − price| ≥ 8c (model from prints available *that day* only),
enter 1 contract **at the taker price**, pay the quadratic fee, hold to
settlement. Spreads > 15c skipped. This is the crudest execution possible —
maker fills would do better.

Overall: 609 trades, +$26.69, +4.4c/contract, 44% hit. But the split by
days-to-settlement is the real finding:

| days to settlement | n | total | avg/contract | hit |
|---|---|---|---|---|
| 1–2d | 22 | +$6.74 | **+30.6c** | 73% |
| 3–7d | 34 | +$6.47 | +19.0c | 50% |
| 8–14d | 160 | +$23.25 | +14.5c | 48% |
| 15d+ | 393 | −$9.77 | **−2.5c** | 40% |

Every dollar of loss came from far-dated entries (the model chased the July
up-run into back-week events that later reversed — the same weakness
Backtest A's Monday-lead numbers flagged). Inside 14 days the strategy made
+$36.46 on 216 one-lot trades (+17c/contract avg).

**Codified rule: no entries beyond 14 days to settlement; concentrate inside
7; maximum conviction inside 2.** (S1–S3 above already respect this.)

Honesty notes ([[feedback-model-is-not-measurement]] discipline): forecasts
at each entry date use only prints available then (walk-forward), but the
two hyperparameters (halflife 6d, sd×1.25) were chosen on this same 13-week
period — expect some shrinkage live. 1-lot sizing ignores book capacity;
taker pricing understates maker economics. Re-run this backtest weekly as
new weeks settle: `python scratchpad/b200_model.py`.
