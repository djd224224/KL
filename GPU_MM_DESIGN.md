# GPU/Ornn Family Market-Making — Design Proposal

*2026-08-09. Companion to `B200_STRATEGY.md` (single-series deep dive; settlement
proofs and the B200 momentum model live there). This doc: how to market-make
the whole family.*

## 0. Recommendation in one paragraph

Build a **new standalone bot (`gpu_mm.py`)** — do not unblock the GPU family in
IMM (the 7/23 capacity decision stands; IMM also has no fair-value model, and
these markets punish book-anchored quoting). Architecture = crypto-MM pattern
(model-anchored quotes from an external data source) + IMM's amended-LIP
scoring machinery (atref placement, pad-to-target, program-period accounting).
Quote the **five weeklies + front two monthly (MS) months + MON**, skip MAX
(one-touch semantics — wrong model class, junk books, tiny pools). The
rewards and alpha legs are ONE leg: fair-anchored two-sided quotes on LIVE
strikes (fair within ~[2c, 98c]), sized for LIP score where pools justify the
~$1k/market qualification collateral — dead-strike pools are structurally
unclaimable (§3.2, Jack's catch) and get zero orders. Phase in over ~2 weeks
with a dry-run reward estimator before any live order.

## 1. The universe (verified 2026-08-09)

| series (×5 GPUs) | settles on | front-event volume | live pools |
|---|---|---|---|
| `KX*WS` weekly | Ornn daily print, Fri 20:00Z (terminal) | 24k–51k/wk each, ~186k combined | ~$65–110/day/series |
| `KX*MS` monthly | **arithmetic mean of hourly values over the calendar month** | 10k–89k per front month | ~$100–155/day/series |
| `KXB200MON` | terminal print on a fixed date (Aug 31) | 4.6k | ~$21/day |
| `KX*MAX` | **one-touch: "above $X *by* Dec 31"** | thin, med spread 9–80c | ~$3–10/day |

- Total live pools family-wide: **$16,275 (~$1,164/day)**, $15/market/period,
  rolling ~2-week periods, target 1000, discount 0.5/tick
  (`/incentive_programs`, paginate `next_cursor`, centi-cents).
- Strike grids are ~10% relative steps on every GPU (0.05 → 0.50 absolute).
- Books: median spread 1–2c on quoted strikes, but **only 2–7 of each
  ladder's strikes are two-sided** — the rest are one-sided or empty.
- MAX verified one-touch by rules text ("**by** Dec 31") — same trap as
  crypto updown-vs-touch; a terminal model would misprice it ~2x. Out of v1.

## 2. Per-GPU index regimes (fit 92 daily prints; refit weekly)

| GPU | last | daily sd | weekly sd (rel) | weekly AC1 | wsd/step | regime → quoting rule |
|---|---|---|---|---|---|---|
| B200 | 5.70 | 0.19 | 0.67 (11.7%) | **+0.52** | 1.3 | trending: center quotes on drift-skewed fair (halflife 6d) |
| H100 | 2.53 | 0.08 | 0.14 (5.3%) | +0.22 | 0.5 | calm: tight symmetric quotes, rewards workhorse |
| H200 | 4.67 | 0.37 | 0.59 (12.6%) | **−0.37** | 1.2 | mean-reverting chop: NO drift term, widen quotes, fade extremes gently |
| A100 | 1.06 | 0.03 | 0.07 (6.3%) | +0.07 | 0.7 | near-static: safest farm, tightest quotes |
| RTX5090 | 0.49 | 0.04 | 0.16 (**32.9%**) | +0.46 | **3.2** | wild: drift-skew AND wide; 3+ strikes live per week; biggest volume (51k) |

The point of the table: **one quoting policy across GPUs would be wrong four
ways.** B200 rewards momentum-skew; applying that skew to H200 (negative
autocorr) systematically buys tops and sells bottoms. RTX5090's weekly sd
spans 3.2 strike steps — its "tails" are not tails.

MS monthly-average state (free daily prints ≈ hourly mean; error ≪ strike
step except final-days precision): AUG MTD through day 9 — B200 5.95,
H100 2.66, H200 4.75, A100 1.04, RTX5090 0.49. The average "banks" as the
month runs: by day ~20, E[month avg] is mostly locked and late-month books
that still price wide uncertainty are the KXRAIN-monthly-style snipe.

## 3. Why the rewards are actually capturable (the LIP geometry)

Amended LIP (eff. 7/30, from IMM's docstring — mechanics already proven in
production): random 1-second snapshots; per side, walk levels from best until
cumulative size ≥ target (1000); reference = level where cumulative ≥
target/5; orders at/above reference score full weight pro-rata, 0.5^ticks
below it; **a snapshot is EXCLUDED unless BOTH sides reach target**; period
payout scales by non-excluded fraction and splits by score share; $1.00
floor per market-period.

Consequences on these books:

1. **Collateral identity**: two-sided at full target costs ≈ target × $1
   (p + (1−p)) ≈ **$1,000/market** regardless of price level. Blanketing
   ~600 markets is $600k — nobody does it, which is why most pools pay out
   ~nothing today.
2. **The dead-strike trap — most of the pool money is UNCLAIMABLE**
   (Jack, 8/9, killing the original "thin-side completion" tier-1):
   on an effectively-settled strike, fair is <1c away from 0 or 100, but
   the minimum tick is 1c — **any resting order on the empty side is above
   fair by construction**. Concretely (verified KXA100WS-26AUG14-2.000):
   the "wall" of 18,755 NO bids @ 99c IS the YES ask at 1c, so a YES bid
   at 1c can't even rest (it crosses); and where a tick of room exists
   (wall at 2c), a resting 1c bid is a free option for the 98c NO queue —
   any member jumps the queue by bidding 99c NO, matching the 1c bid: the
   completer donates ~$10–12 per sweep against $7.50/period, and must
   refill to stay qualified. A 1c ATM machine. The incumbents' one-sided
   walls are the *correct* play; those pools pay nobody because nobody
   rational can complete them. Corollary: a large share of the $1,164/day
   headline is structurally dead — only pools on LIVE strikes (fair
   roughly within [2c, 98c], both sides restable at rational prices)
   are real revenue.
3. **At-ref share on live strikes**: where organic two-sided depth already
   qualifies snapshots, added size at/above reference takes pro-rata share
   with no exclusion work. Post at the reference level (IMM `atref` mode:
   same reward as at-touch, ~half the fill exposure, ~2/3 collateral) and
   let size, not tick-priority, earn the share.
4. **$1 floor discipline** (IMM lesson): projected share × pool < $1/period
   → that market pays literally nothing; don't token-quote it. Fund fewer
   markets properly.

Allocator: rank LIVE-strike market-periods by `pool_$per_day ÷
collateral_required` greedily under `GPU_MM_BUDGET`. Sweeten with the
hybrid case: near-tail strikes where the model fair (5–15%) sits ABOVE a
1–2c book bid — resting there is +EV per fill on its own and the score is
a bonus (B200 6.50 @ 1c vs 9% model fair is today's example). The
both-sides-1000 rule still prices full qualification at ~$1k/market;
partial size only earns pro-rata where organic depth already qualifies the
snapshot — verify per market with a cumulative book walk, not top-of-book.

## 4. Quoting engine

Per market each cycle (30–60s poll; fast-lane after prints):

1. **Fair** from the per-GPU model (§2; terminal for WS/MON with
   days-to-settle scaling, running-average for MS). Same math as
   `b200_pricer.py` generalized — one `forecast(gpu, target_date)` +
   `p_above(K)`; MS variant averages banked MTD + forecast remainder.
2. **Quote band**: bid ≤ fair − δ, ask ≥ fair + δ, where δ = max(2c,
   z × sd_to_settle × φ'(K)) — i.e. wider where the strike's probability is
   vol-sensitive, near-flat (1–2c) on deep tails. Inventory skew shifts the
   band (IMM-style: |pos| ≥ X halves the accumulating side, ≥ 2X pulls it).
3. **Placement for score**: within the band, rest at the reference level
   (atref), sized by the allocator; pad the thin side to target where tier-1
   applies. Post-only always; TTL 600s; amend-in-place on reprice.
4. **Print embargo**: cancel/widen the front-week ATM strikes from 19:55Z,
   requote at ~20:02Z after reading the new print (all five GPUs, one free
   API call each). The print is the only scheduled information event; being
   the first correct book after it is both the alpha and the safety.
5. **Never lead into emptiness on the dangerous side**: thin-side completion
   only where the model says the completed side is ≥ ~4 daily-sd from ATM
   *in the safe direction* (e.g. don't build 1c YES bids on a strike the
   index is crashing toward — B200 down-run + 5.00 bid side = the exact
   pickoff S1 in B200_STRATEGY.md monetizes from the other side).

## 5. Risk framework

- **Per-GPU kill switch**: print moves ≥ 2.5× recent daily sd → cancel that
  GPU's whole complex, stand down until manual/next-day (regime break:
  B200 had a +$0.79 day in July).
- **Daily realized-loss halt** across the bot (start $100/day), halt file +
  restart-surviving carry (IMM pattern, `--clear-halt` to un-halt).
- **Fill-burst breaker** per market (≥15 contracts in a cycle → pull both
  sides 60 min — insider/news sweep guard, straight from IMM).
- **Tenor cap**: nothing beyond front 2 weekly events + front 2 MS months —
  B200 backtest: entries >14d to settlement were the only losing bucket
  (−2.5c/ct on 393 trades); long tenors also lock collateral against
  slow-period pools.
- **H200 special**: no drift skew, δ × 1.5 (7.8% daily sd + negative AC1 =
  the chop eats trend-followers).
- **RTX5090 special**: treat ±2 strikes around ATM as "live" (wsd = 3.2
  steps); tails start 3+ steps out.
- **Ops**: singleton lock, watchdog task, TTL bounds orphan risk
  (laptop-standby memory applies: long lid-close = books empty via TTL —
  acceptable, snapshots just exclude), resting-order count budget (family
  at target sizes ≈ hundreds of orders — stay under the ~2000 account cap
  minus IMM's usage; coordinate via a shared env knob).
- **Settlement-source discipline**: archive all 5 Ornn histories daily
  (20:05Z task) — free API is a rolling 3-month window; the archive is both
  the model's training data and the settlement audit trail.

## 6. Economics (labelled estimates, not measurements)

- Pools: $1,164/day family-wide headline, but a large share sits on dead
  strikes and is structurally unclaimable (§3.2). The claimable core is
  live strikes: ~2–4 per ladder × fronts ≈ the best **8–12 market-periods
  at ~$1k each → roughly $80–150/day of gross pool share at $8–12k
  collateral, BEFORE adverse-selection costs** — which are real (once-daily
  data vs possibly intraday-informed flow) and only measurable live. Hybrid
  near-tail bids (below-fair pennies) add small +EV with score as bonus.
  All modelled numbers; re-project from Phase-B realized credits before
  committing Phase-C capital. Pools also get repriced by Kalshi — DDR5
  pools vanished 8/2 overnight.
- Alpha overlay (B200/RTX5090 fronts, print-timing + drift): B200 backtest
  says +14–30c/contract inside 14 days at ≥8c edge; family-wide guess
  $30–100/day at current thin-touch capacity. Secondary to rewards here.
- Fees: post-only throughout → maker, $0. Taker only for kill-switch flattening.

## 7. Build plan

- **Phase A (2–3 days)**: `gpu_mm.py --estimate` — dry-run: full universe
  scan, allocator output, projected $/day per market-period with the
  exclusion simulation (IMM's estimator meta pattern). Validate projected
  vs. actual pool payouts on 2–3 markets quoted manually if desired.
- **Phase B (week 1 live)**: 2–3 live strikes on the calmest GPUs
  (A100/H100 ATM ± 1), fair-anchored at-ref quotes at partial size, budget
  $2–3k, plus below-fair penny bids on near-tails (B200 6.50-style).
  Measures BOTH the score-share model and realized adverse selection
  (`imm_reward_recon.py --statement` pattern for credits; fill logs for
  pickoff cost).
- **Phase C (week 2)**: scale to the best 8–12 market-periods family-wide
  with per-GPU fair gates + print embargo; add MS front months; budget
  $8–12k, contingent on Phase-B credits ≈ projection and pickoff cost <
  reward.
- **Phase D**: alpha overlay sizing up (B200/RTX5090 endgame plays);
  H200 last (hardest regime).
- Reuse: `KalshiClientsBaseV2ApiKey_FIXED` client, IMM's watchdog/singleton/
  halt-carry/email-digest scaffolding, crypto-MM's model-quote loop shape.
  New code is mostly the per-GPU fair engine (exists in `b200_pricer.py`,
  needs the MS average variant + per-GPU params) and the LIP allocator.

## 8. Open items before Phase B goes live

1. Confirm reward accounting on one manually-quoted tail (does a completed
   thin side actually pay ~50% share? — validates the snapshot model).
2. MS hourly-mean vs daily-print approximation error (compare a settled MS
   month's implied settle vs mean-of-daily — if >1c, budget the margin).
3. Resting-order account cap headroom vs IMM (count IMM's typical usage).
4. Whether MON strikes (auto-generated 0.1-step, all 20 two-sided at 1c)
   have a resident MM whose behavior to observe first.
