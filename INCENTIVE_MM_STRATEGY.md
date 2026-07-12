# Incentive-MM Bidding Strategy — Return per Unit of Risk

*v1.1, 2026-07-11. Strategy layer for `incentive_mm.py` (mechanics in
INCENTIVE_MM_HANDOFF.md). v1.0 was red-teamed by three independent adversarial
reviews (program-gaming, risk-management, estimation-realism lenses); this version
incorporates every confirmed finding. Confirmed-critical fixes are marked* **[RT]**.

---

## 0. The one-paragraph thesis

Kalshi's liquidity program pays **rent on resting orders** — per contract, per second,
discounted 0.5^ticks from the best price, pro-rata against everyone else's resting
score, *but only on levels inside the qualification walk* (best-first to ~1000
contracts of cumulative depth). Rent accrues without fills; fills are a **pure cost**
here — joining under price-time priority puts us at the back of every queue, so the
flow that reaches us is disproportionately informed **[RT]**. The optimal strategy is
therefore **subsidy harvesting with a minimal fill footprint**: rest the smallest size
that captures near-max score share, as close to the touch as the market's *information
regime* allows, only on rungs that actually score, spread across many independent
catalyst-free markets, and stand down whenever information can arrive. Every sizing
decision reduces to two gates: positive expected value after adverse selection, and
tail-loss payback within days.

---

## 1. P&L decomposition

Per market *m*, side *s* (YES-bid / NO-bid), ladder level at distance *d* ticks behind
the best with size *n_d*:

```
E[P&L/day] = R (reward rent) − A_jump (gap risk) − A_bleed (drift risk) − X (exit cost)
```

**Uninformed capture is budgeted at zero** **[RT]**. Kalshi matches price-time; a
joined order sits queue-last, so small uninformed prints fill the front of the queue
and the fills that reach the back are level-clearing sweeps. Any realized positive
markout is upside, not plan.

**R — reward rent.** Pool rate W ($/day), discount δ=0.5. Our weighted score
O_s = Σ_d δ^d·n_d and external S_s, **summed only over levels inside the
qualification walk** (cumulative depth from the best ≤ target T; if the walk exhausts
the book, the side pays nobody). Payout is pro-rata on share mass *summed over the
period*: a share unit is worth `W / Σ_t Q_t` where Q_t = qualifying sides at snapshot
t — i.e., value share mass at the period-average qualification pattern `W/Q̄`, not at
the instantaneous `W/Q_t` **[RT]** (the v1.0 estimator over-credits one-sided moments
by up to 2×; fix in the accrual).

Structural facts:
- **Walk truncation** **[RT]**: on a book with ≥T contracts at/near the touch, rungs
  at d=1,2 sit *outside* the walk and score exactly **zero** while keeping full sweep
  exposure. The "94% of weight within 3 ticks" claim holds only when the walk reaches
  3 ticks. Ladder construction must compute the walk end from the live book and place
  only inside it (§4.2).
- **Concavity, with one exception**: marginal reward of added size is
  `(W/Q̄)·δ^d·S_s/(O_s+S_s)²` — diminishing — *except* near the walk boundary, where
  our added near-touch size can evict deep external score from qualification
  (locally convex). Treat as a bonus, not a lever.
- **Min-payout cliff**: the filing pays only if a participant's period result
  ≥ $1.00 (*"if the result is greater than or equal to $1.00, the result is paid
  out"* — a forfeiture threshold, not a round-up floor; verify against the first
  paid period's credits anyway). Markets where we can't clear ~$1.50/period
  expected are dead weight.

**A_jump — gap risk.** Information arrives two ways, and they have different sizes
**[RT]**:
- *Mark-noise jumps*: repricing of 10–40c (rate η_mark). Loss per swept contract
  ≈ (J − s/2 − d).
- *Resolution jumps*: the announcement that effectively decides the market. For
  announcement/deadline markets — most of our universe — **the rare jump IS the
  resolution**, and loss-given-sweep is the *distance to the adverse boundary*
  (a 20c market's bid side loses ~19c; its ask side ~79c), not a modeled J95.
  Quiet ≠ small jumps; quiet means the jump hasn't happened yet.

```
A_jump = [ η_mark·E(J−s/2−d)⁺ + η_res·(boundary distance − d) ] · n_d / 100   $/day
```

**A_bleed — slow drift** **[RT]**: informed flow arriving as 5c-per-cycle drift with
steady fills trips no single-cycle breaker while the join logic re-anchors into the
move; 9 minutes of that accumulates 50+ toxic contracts. Guarded by trailing-window
breakers (§5) and charged in expectation as a category-level cost until measured.

**X — exit cost**: unwinding inventory on a thin book costs up to half the spread per
contract (or rides to settlement). Charge `min(half-spread, boundary distance)` per
contract expected to be swept. This automatically penalizes wide-spread, thin books.

**Risk ruler.** Two numbers per market, computed from *actual resting sizes and live
spread*, not static shapes **[RT]**:
- **WCB (worst-case bound)** = full sweep of ONE side to the adverse boundary
  (one print hits one side; do not sum sides): `max_s Σ_d n_d·(boundary_s − entry_d)/100`.
- **mCVaR (modeled daily)** = η-weighted expected shortfall used in *ranking*.
WCB is the hard cap; mCVaR is the selection currency. The user's ±100/market cap
bounds the absolute worst carry at ~$100/market independent of everything above.

---

## 2. The placement rule — two gates, not one ratio **[RT]**

*(v1.0 used a single ratio with κ=0.25, which (a) admitted contracts earning a
quarter of their expected AS cost — negative EV — and (b) mislabeled itself as a
"4-day J95 payback," which is a different denominator by ~16× in class A. Both
red-team reviews flagged it independently. Corrected:)*

Place the marginal contract (m, s, d) iff **both**:

1. **EV gate**: `marginal R ≥ 1.5 × marginal (A_jump + A_bleed + X)` — never
   knowingly negative-EV; the 0.5 margin absorbs estimator optimism and is the
   tunable dial (§6).
2. **Tail-payback gate**: `marginal net E[P&L/day] × 4 ≥ marginal WCB contribution` —
   any contract must earn back its own worst-case single-print loss within 4 days.

Greedy placement by net-EV-per-WCB, stopping at the gates and budgets, solves the
CVaR-constrained maximization at this scale; no heavier optimizer is warranted.

---

## 3. Regime classification (the master input)

η_mark / η_res / boundary exposure are predictable by **category × catalyst
proximity**. Until a category has real measurement (§6), it defaults to **class C —
"class A" status must be earned with data, not assumed** **[RT]**:

| Class | Information regime | Examples | Policy |
|---|---|---|---|
| **A. Measured-quiet, catalyst-free** | η_res ≤ 0.01/day, no schedulable news path | earned via §6 jump panel only | Ladder from the touch, sizes per gates |
| **B. Slow news** | η_res ~ 0.02–0.05 | KPI far from report, layoffs, casting long-shots | Touch size halved |
| **C. Default / episodic** | η_res ~ 0.1–0.3 or **unmeasured** | anything new, political negotiations, trending topics | Start at d=1; tail side halved; rich pools only |
| **D. Scheduled/live catalyst** | event window | mention markets day-of, dailies at cutoff | **No quotes** (cutoff machinery, already enforced) |

Catalyst proximity overrides category (C→D demotion near a known
occurrence_datetime). The worked example that justified touch-quoting in v1.0 assumed
10% share and J95=15c for class A; under resolution-sized jumps the same example must
clear the two gates with boundary-distance losses — which it does only at genuinely
tiny η_res, hence "measured-quiet, earned with data."

---

## 4. Ladder design rules

1. **Join, never lead** (unchanged): leading ≤2×'s the weight but makes us the unique
   counterparty to the next informed trade and starts share-diluting quote wars.
2. **Place only inside the qualification walk** **[RT]**: compute the walk end from
   the fetched book each cycle (cumulative size from the best vs target); skip rungs
   beyond it — they are pure risk with zero rent. On thick books this collapses the
   ladder toward the touch (subject to the gates); on hollow books it extends.
3. **Qualification-feasibility screen** **[RT]** *(replaces v1.0's misaimed
   "50%-of-target" rule)*: quote a side only if `external depth ≥ target − our
   ladder size` — otherwise the side cannot reach target even with us and pays
   nobody. (Our 35 can only tip qualification when external is within 35 of target;
   the v1.0 screen admitted sides in [500, 965) that were guaranteed zero-rent.)
4. **Tail-side asymmetry, boundary-based**: on any market, the side whose adverse
   boundary is far (short-the-longshot side) carries multiples of the cheap side's
   worst case (79c vs 19c on a 20c market). The gates handle this automatically via
   WCB; the practical output is ~half size and +1 tick on the tail side for
   mid ≤ 20c or ≥ 80c.
5. **Gap-filling behind the best is free reward only inside the walk** (see rule 2);
   a gap level is also un-split (no share dilution at that price).
6. *(v1.0's 50% self-dilution cap is retired as dead policy at current caps — our
   max weighted score ~15/side rarely approaches external weighted score on any book
   that passes rule 3. It returns only if ladder sizes are ever raised ~10×.)*

---

## 5. Portfolio construction

**Breadth over depth** (concavity + linear risk + independent pools), bounded by the
min-payout floor (expected ≥ $1.50/period per market) and ops overhead (35 markets /
90s polls is the current sweet spot).

**Risk budgets** — all CVaR/WCB-denominated; v1.0's collateral-denominated category
cap let one correlated theme sweep 2–3× the daily budget **[RT]**:

| Budget | Limit | Notes |
|---|---|---|
| Per-market WCB (one-side boundary sweep) | ≤ $18 | computed live from resting sizes + spread; ladder auto-shaved to fit |
| Per **category/underlying cluster** WCB | ≤ $36 | one theme = one jump: sum member WCBs |
| Daily portfolio CVaR | max(worst cluster WCB, k largest idiosyncratic WCBs) ≤ $36 | k = Poisson 95th pct of Σ η_m over the book — not a hardcoded 3 **[RT]** |
| Per-event net / per-market net | ±500 / ±100 | user caps (correlated by construction) |
| Realized + **mark-to-market** daily loss | −$50 halt | see the blindness fix below |

**The loss-halt blindness (fix required before go-live)** **[RT]**: v1.0's halt sees
only fill-round-trip realized P&L. Settlements are not fills (a settled position
silently vanishes from the unsettled-positions read with its loss never booked), and
there is no mark-to-market — so gapped inventory riding to settlement, the *dominant*
loss channel this strategy worries about, is invisible to the backstop. Required:
(1) MTM all open positions to external mid each cycle; halt on realized + ΔMTM;
(2) book settlements through the P&L tracker (poll settled positions or synthesize a
0/100 fill when a known position disappears); (3) carried-inventory count + MTM in
the digest; (4) an explicit exit policy for stale reduce-only tails (cross up to X
cents after N hours post-catalyst) instead of passive-forever.

**Trailing-window breakers** **[RT]** (the anti-bleed guard, complementing the
single-cycle breakers): cumulative one-direction external-mid move ≥ 20c over 30 min,
OR net position change ≥ 25 contracts / 30 min while mid drifts against the acquired
side → cancel market, 60-min cooldown. Near-touch quoting additionally requires
spread ≤ 10c *or* 7-day volume above a real floor (25 *lifetime* volume is not
evidence of price discovery).

No hedge exists for any of this; sizing and stand-downs are the entire risk stack.

---

## 6. Measurement before belief (the go-live sequence)

**The base case is unknown until one period has actually paid** **[RT]**. The v1.0
"$50–125/day" figure was an unvalidated estimator times a guessed haircut. Restated
honestly: *unknown, bounded above by ~$500/day of estimator share at full size.* The
sequence that converts guesses into numbers:

1. **Phase 0 — dry-run sensor (running now, 2–4 weeks).** The bot already fetches
   ~35 full books every 90s. Persist per cycle: ticker, ts, best bid/ask, depth-to-
   target both sides, our estimated share. This panel *is* the η/J estimator at
   pickoff-relevant resolution — `get_market_history` cannot do this job (last-trade
   artifacts on thin books, survivorship of not-yet-jumped markets, no intra-day
   resolution; all three biases point toward the answer we'd like) **[RT]**.
   Category earns class-A/B status only with ≥300 market-days and jump counts
   consistent with the class rate.
2. **Phase 1 — live micro-probe (2 weeks, ~$200 collateral).** `IMM_LEVELS=0:1,1:2,
   2:4` on ~10 class-B/C markets. Purpose: one full **paid period** of ground truth —
   realized credits ÷ estimated accrual (also validates centi-cent parsing, the
   min-payout cliff, per-market credit itemization, and measured process uptime).
   No scale-up until this ratio exists.
3. **Phase 2 — scale with decomposed feedback.** The realized/estimated ratio is not
   one scalar **[RT]**: decompose into uptime factor (heartbeat-measured), qualification-
   flap factor (fraction of cycles a quoted side qualified, from the Phase-0 logger),
   and residual (competition + rule error). Apply the first two globally, the residual
   per market if credits itemize (check the ledger API in Phase 1; if they don't
   itemize, reconciliation is portfolio-level and the doc says so).

**Markout protocol** **[RT]** (ground truth for adverse selection, made computable):
mark hierarchy = (1) two-sided mid; (2) one-sided: remaining best ± half trailing-24h
median spread; (3) **settlement value whenever the market settles inside the horizon —
the best mark, and on this universe the common case**. Contract-weighted, per
category-side, bootstrap CI. Demotion trigger: upper CI bound of
(markout + accrued rent) < 0 with ≥ 30 fills — never on raw markout (queue-last fills
skew negative even in healthy markets) and never on <30 fills in either direction.

**Dial tuning without the peso problem** **[RT]**: the EV-gate margin may only
*relax* when the Phase-0/live jump panel shows observed jump counts ≤ the current
class's 80th-percentile Poisson band over ≥300 market-days — never because a quiet
30-day P&L window looked good (that ratchet loads maximum size into the first real
jump). Tightening on realized pain (any day worse than −1.5× modeled) stays
immediate and asymmetric.

**Share-decay rule, concrete**: persist per-market estimated share each cycle;
3-day EWMA < 50% of the market's first-72h EWMA *and* estimated $/day < 2× the
min-payout floor → deselect, 7-day re-entry cooldown. Never respond to dilution by
adding size (concavity + arms race).

---

## 7. Failure modes the math doesn't capture

| Failure | Control |
|---|---|
| Settlement/MTM-blind loss halt | **fix before go-live** (§5) — the backstop must see the dominant loss channel |
| Stale quotes during process freeze | 600s exchange TTL; cutoff-capped expirations; VPS is the real fix |
| Estimator ≠ payout (rule nuance) | Phase-1 micro-probe gates all scale-up |
| Correlated theme sweep | cluster WCB cap ≤ $36 |
| Category misclassified quiet | default-to-C until measured; trailing breakers limit the first hit; markout demotion |
| Program repriced/terminated/clawback | rent not positions → orderly wind-down; genuine two-sided liquidity is the program's stated purpose; keep this book a bounded fraction of the account |
| Imitators join our touch | share-decay rule: re-rank, never size up |

---

## 8. Implementation delta from v1.0 (prioritized)

**P0 — before any `--live`:**
1. MTM + settlement booking into PnlTracker; halt on realized + ΔMTM; digest lines. **[RT]**
2. Phase-0 cycle logger (book snapshot panel → CSV/BQ).
3. Micro-probe config profile (`IMM_LEVELS=0:1,1:2,2:4`, MAX_MARKETS=10).

**P1 — with first scale-up:**
4. Walk-aware ladder placement (rungs only inside the qualifying walk). **[RT]**
5. Qualification-feasibility screen (ext depth ≥ target − ladder). **[RT]**
6. Trailing-window breakers (20c/30min drift; 25 contracts/30min accumulation). **[RT]**
7. Runtime WCB computation + auto-shave to the $18/market, $36/cluster caps. **[RT]**
8. Two-gate placement math + accrual fix (share mass ÷ Q̄). **[RT]**

**P2 — steady state:**
9. Category classifier + measured class table; per-class ladder shapes; tail-side
   asymmetry; selection score = net-EV/WCB replacing $/day sort.
10. Markout pipeline + CI-gated demotions; share-decay EWMA rule; decomposed
    reconciliation.

The v1.0 chassis (caps, cutoffs, single-cycle breakers, loss halt, join-don't-lead,
fill dedupe, orphan restore) is the substrate all of this assumes — already built.
