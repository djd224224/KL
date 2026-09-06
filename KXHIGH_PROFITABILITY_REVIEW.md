# KXHIGH Weather Bot — Profitability Review

*Written 2026-09-06. Scope: `high_temp_trading.py` (KXHIGH daily-high NO ladders).
The KXLOW bot has been paused since 2026-08-31 and is out of scope except where the
same defect is inherited.*

*Environment caveat: this review was done without BigQuery or Kalshi credentials.
Every number below comes from artifacts committed in the repo. Sample sizes are
stated for each one. Nothing here was re-run against the live tables — Section 5
lists the queries that should be.*

---

## 0. Verdict in five sentences

1. The bot is a **passive liquidity provider 4–18c below the best NO bid**, gated by a
   forecast model. The model does **not** set the price paid — a static per-city
   `hi_no` cap does — and the model's fair value is the binding constraint in only
   ~28% of markets (bias_diag, n=100). The A/B test that tied the cap to fair value
   found nothing after 1,290 markets, which is the expected result when the fair
   value is wrong (see 2).
2. The probability model is **overconfident by roughly 15–25 points on the modal
   bucket**: it uses σ floors of 1.0–1.9°F (median 1.2) against a realized forecast
   error SD of ≈2.0–2.4°F in three independent samples (the bot's own hardcoded
   8-city table, n≈300; May history, n=100; April ensemble RMSE 2.27, n=386).
   Its Brier score is worse than the market's (0.244 vs 0.217, n=90, ≈1.7σ).
3. **Fills are adverse-selected by construction** — the bot only fills after the
   market moves toward YES on that bucket — and every sample I have says toxicity
   rises with depth: May fills at ≤40c were 100% YES (n=11, −$909); 41–50c 67% YES
   (n=39); 51–60c 36% YES (n=47). The Aug fill analysis quoted in the code says the
   41–60c band (82% of volume) earns +0.45c/contract, i.e. nothing.
4. **Per-city P&L tilts are fitted to noise**: Spearman ρ = 0.00 (p=0.99) between
   April and May per-city P&L across 20 cities; 11/20 sign agreements. The per-city
   size multipliers and price floors should be removed.
5. At current deployment (~$300–600/day, ~1,000 contracts/day), **even a genuine
   3c/contract edge is ~$10–30/day**, and it would take ~500 trading days to
   distinguish that from zero using P&L. Decisions must be driven by mechanism and
   by calibration metrics (which have 4× more data because unfilled markets count),
   not by P&L slices.

---

## 1. What the bot actually does (mechanics that matter for P&L)

Per market, four times a day (19:00, 23:00 CT evening; 05:02, 07:02 CT day-of):

| Step | Rule | Consequence |
|---|---|---|
| Fair value | `P(yes) = Φ((hi−μ)/σ) − Φ((lo−μ)/σ)`, μ = mean(NWS, WU) + rolling bias, σ = max(inter-source sd, city floor 1.0–1.9) | Two point forecasts, normal shape, σ floor binds whenever NWS≈WU (most days) |
| Gate | trade B-market only if `P(yes) > 0.20`; **tails exempt** | Tails have no model gate at all |
| Ladder top | `min(hi_no_config, best_NO_bid − 4c)` | Static per-city cap (43–63c) — not fair value |
| Ladder | 8 rungs × 2c (B) / 15c (T) below the top, flat size | Deepest rung ≥ 18c below the bid at placement (B), ≥ 60c below (T) |
| Size | 15/rung day, 30/rung night, ×city mult (0.5–1.25), ×1.5 in 61–80c band; caps 100–150 ct and $125 cash per market | ~2,000 orders/day for ~15 fills (3.5% of orders, 1.9% of contracts, April) |
| Expiry | evening orders → 01:59 CT; day-of → 09:05 / 10:05 CT | Orders rest 3–6 h through the 00Z/12Z model cycles and morning obs |
| Hold | to settlement, never sells | P&L = Σ contracts × (1{NO} − price) |

Two structural facts follow directly from the code, no sample needed:

- **A fill requires the market to move ≥4c toward YES on that bucket after
  placement.** Fills therefore carry information against the position. The only
  question is how much, and it depends on depth (how far the market had to move) and
  timing (what information arrived while the order rested).
- **The model's fair value influences the price paid only when `fair_NO − margin <
  hi_no_config`**, i.e. on high-P(yes) buckets — exactly the buckets where the
  4c maker buffer already prevents placement (market NO bid ≈ 40–45c on the modal
  bucket vs. a ladder that bottoms at `hi_no − 14`). So in practice the model is a
  filter, not a pricer. bias_diag confirms: `hi_no_config` was the binding cap in
  68 of 100 markets; `fair_no − 3` in 28.

---

## 2. Evidence, by sample

| Sample | Period | n | Result | P&L definition | Selection |
|---|---|---|---|---|---|
| Validation dashboard (`analysis/kxhigh/output/kxhigh_validation.html`, built 2026-04-25) | Mar 9 – Apr 25 | 465 settled markets, 36 settlement days, 1,975 fills | +$365 on $13,352 cost (2.7% ROI); 55% market win rate; mean **$+10/day, sd $123/day → t = 0.49** | `settlements.pnl` | full period, not selected |
| May slump file (`bias_sim_input.csv`) | May 17–24 | 100 markets | **−$2,606 on $10,021 (−26%)**; NO win 44%; 212 ct/market avg, max 907 | `settlements.pnl` | **selected as a slump** |
| June (code comment, `MARKET_CASH_CAP_DOLLARS`) | June | — | −$1,732; 28 markets >150 ct = 64% of the loss (cap leak, since fixed) | unknown | full month |
| Aug fill-level analysis (code comment, `BAND_TILT_*`) | ≤ Aug 9 | unstated | 61–80c band +9.86c/ct, win 0.749 vs 0.650 breakeven; 41–60c band +0.45c/ct on 82% of volume | fills, pro-rated | best-of-bands pick |
| A/B `hi_no_tied_to_fair_no_v2` (workflow comment) | Apr 30 – Aug 9 | 1,290 settled markets | no treatment effect (p≈0.88); treatment cut fill participation 30.7%→26.3% (p=0.001) | cash-flow realized | randomized |
| 17 committed digest days (`run-logs/high-temp/`) | Apr–Jul | 17 days | Σ headline = +$1,526; range −$306 to +$377/day | mixed est./settled | **committed days, not random** |
| Aug 14 portfolio snapshot | Aug 12–14 | 37 events | realized +$100 on settled Aug 12–13 events; $297 basis open on Aug 14 | marks | one day |

**P&L-definition warning.** `analysis/kxhigh/sql/90_ab_hi_no_report.sql` documents that
`settlements.pnl` treats any contract sold before settlement as a total loss; on the
529 A/B markets it overstated the loss by 2.6× (−$8.51k vs −$3.26k true realized).
Jack trades manually on the same account (INCENTIVE_MM_HANDOFF: "unstamped orders =
Jack's manual app trading"). Every row above marked `settlements.pnl` — including the
April +$365 and the May −$2,606 — is exposed to this. **The bot's standalone P&L is
not knowable from settlements; it needs the fills cash-flow joined on
`client_order_id`.** Lifetime KXHIGH P&L is not recorded anywhere in the repo; from
what is visible (April +, May −−, June −, Jul/Aug ≈ flat-to-positive) it is more
likely negative than positive. Confidence: low.

### 2.1 σ miscalibration (confidence: high — three independent samples agree)

The bot's own hardcoded actual-minus-forecast table (`high_temp_trading.py:1165–1200`,
NWS+WU, n≈37/city) implies error SDs the bot does not use:

| City | Table-implied error SD (°F) | σ floor used | P(modal bucket YES): table ≈ | P(modal bucket YES): bot |
|---|---|---|---|---|
| Austin | 2.02 | 1.2 | 0.32 | 0.60 |
| Miami | 1.73 | 1.0 | 0.45 | 0.68 |
| Denver | 2.60 | 1.4 | 0.32 | 0.52 |
| Houston | 2.00 | 1.9 | 0.27 | 0.40 |
| Philadelphia | 2.12 | 1.3 | 0.30 | 0.56 |
| New York City | 1.93 | 1.2 | 0.37 | 0.60 |
| Chicago | 2.02 | 1.3 | 0.28 | 0.56 |
| Los Angeles | 2.18 | 1.2 | 0.47 | 0.60 |

(Table P(modal) ≈ P(round(err)=0) + ½·P(|round(err)|=1); crude but the direction is
unambiguous.) The May history (`bias_history.csv`, n=100, 6 cities) gives the same
answer without rounding: P(|err| ≤ 0.5) = **0.38**, P(|err| ≤ 1.0) = 0.48, error SD
2.36, MAE 1.78; per-city SD 1.6 (Chicago, Austin) to 3.0 (Boston). April ensemble
RMSE 2.27 / MAE 1.68 (n=386, part Open-Meteo backfill). A normal fitted to
P(|err|<1) ≈ 0.40 has σ ≈ 1.9–2.2.

Consequences:

- Model fair NO on the modal bucket ≈ 40c; empirical ≈ 60–70c. Every downstream use
  of `fair_no_cents` (A/B cap, printed edge, `model_edge_at_fill`) inherits a
  ~20-point error in the direction that makes the bot's own trades look worse than
  they are pre-fill.
- The A/B treatment `min(hi_no, fair_NO − 3)` therefore cut bids on buckets where NO
  was actually worth *more* than the cap — it reduced fills (measured) without
  touching toxicity. The null result is what a wrong fair value produces; it is not
  evidence that "model-linked pricing doesn't work."
- The "inverted edge" finding (April, n=467 fills: top edge quintile −$2.87/fill,
  41.5% win; bottom quintile −$0.66, 64% win) is the same defect seen from the fill
  side: "high model edge" = market NO price far below a too-low model fair NO =
  the market is far more YES-confident than the bot = informed.
- April's reliability bins looked roughly calibrated (0.465→0.44, 0.56→0.48, n=25,
  23) because two errors cancelled: ~20 points of model overconfidence against
  ~15–20 points of adverse selection conditional on fill. In May the second term
  dominated: predicted 0.47 → realized 0.63 (n=41); 0.33 → 0.47 (n=30).
- The normal shape is also wrong: the 8-city table puts 16–22% of mass at |err| ≥
  2.5°F where a σ=1.2 normal puts 3.7%. LAX has 20% mass at −5 (marine-layer
  busts, GFS table). Use the empirical per-city CDF — already in the codebase for
  tails — for all buckets, or at minimum a Student-t.
- Inherited by `low_temp_trading.py` (floors = high-bot floors + 0.3; still ~1.5 vs
  a Tmin error that the handoff itself says "runs wider").

### 2.2 The model is not the edge (confidence: moderate)

- Brier: model 0.244 vs market-implied 0.217 (n=90; difference ≈1.7σ). Resolution
  0.016 on uncertainty 0.244 — the model barely separates outcomes.
- A/B null at n=1,290 (see above).
- Edge-quintile inversion (n=467 fills).
- Two point forecasts (NWS grid, WU/TWC MOS) are inputs every market participant
  has. The market's better Brier says it is using more (NBM percentiles, HRRR,
  intraday obs). This is a statement about *relative* information, and it is the
  reason fills are adverse-selected.

### 2.3 Depth below the market predicts toxicity (confidence: moderate; direction consistent in 4 sources)

| Source | Finding |
|---|---|
| May slump, by avg NO price paid | ≤40c: 11 mkts, **100% YES**, −$909 · 41–50c: 39 mkts, 67% YES, −$1,475 · 51–60c: 47 mkts, 36% YES, −$179 |
| May slump, by \|strike − μ\| | 0–0.5°F: model 0.51 → realized 0.58 · 0.5–1.0: 0.435 → 0.61 · 1.0–1.5: **0.32 → 0.52** (n=21) |
| Aug fill analysis (code comment) | 41–60c +0.45c/ct; 61–80c +9.86c/ct |
| IMM temp-hourly (same account, different product; SESSION_HANDOFF_2026-08-04) | at touch −4.7c, 1 tick −6.3c, 6–10 ticks −11.2c; "deep fills are sweep fills" |

The single worst market in the May file is the mechanism in one line:
`KXHIGHTBOS-26MAY21-T72` — forecast 64.5, model P(yes) = 0.00, market NO bid ≈ 48c,
bot bought **600 NO at 44.6c** because Boston's static `hi_no` is 50 and tails have
no model gate; actual 73°F (+8.5); **−$268**. The model and the market disagreed by
~55 points and the bot sized as if the model were right. Nothing in the current code
can refuse that trade.

### 2.4 Per-city tilts are noise (confidence: high)

April per-city P&L (37 days, 14–40 markets/city) vs May 17–24 per-city P&L (20
cities): **Spearman ρ = 0.00, p = 0.99; sign agreement 11/20.** Houston −$235 → +$319;
Chicago +$212 → −$639; NYC +$327 → −$418; Atlanta +$155 → +$384. Per-market P&L SD
in May was $132, so a 30-market city total has SE ≈ $720 — larger than any city's
observed total. `CITY_SIZE_MULT` (0.5–1.25), `CITY_MIN_NO_PRICE` (CHI 50, DEN 55),
and the hand-set `hi_no` spread (MIA 43 … AUS 63) were tuned against this noise.

Per-city parameters that *are* defensible because they have a mechanism: error SD
(Boston/Denver/Minneapolis wide, Miami/Vegas tight), tail asymmetry (LAX cold busts),
early-peak propensity (frontal cities; the peak-before-cutoff filter already handles
this on day-of runs).

### 2.5 What P&L can and cannot tell you at this scale (confidence: high)

Per-market ROI SD ≈ 0.97 (May; outcomes are near-binary at ~50c so this is generic).
Two-sided 5%, 80% power:

| True mean ROI | Markets needed | Trading days at 15 filled markets/day |
|---|---|---|
| 2% | 18,300 | ~1,220 |
| 3% | 8,150 | ~540 |
| 5% | 2,900 | ~200 |
| 10% | 730 | ~50 |

Any A/B halves the per-arm n and doubles the requirement. April's +$10/day on sd
$123 would need ~1,190 days. The "61–80c band, 15.2% ROI" pick — if it rests on
~200 markets — is ≈2σ before correcting for choosing the best of ~6 bands.

Implication: **use calibration on all quoted markets (≈60/day, filled or not) and
adverse-selection measurements (filled vs unfilled at matched model P(yes)) as the
decision metrics.** Those accumulate 4× faster than P&L and are not subject to
fill selection. Reserve P&L for detecting large effects (≥10% ROI) and for
kill-switches.

### 2.6 Code-level inconsistencies (confidence: high; low P&L impact individually)

- Bias correction is applied to B-market μ (`Average_corrected`) but the tail table
  lookup at `high_temp_trading.py:1228` uses raw `Average`. In a +1.25°F warm regime
  (May 26 global bias) tail P(yes) is understated → tail `hi_no` too high.
- `round(low_range − Average)` uses banker's rounding; the tail lookup is exact for
  half the parity cases and conservative by 1°F for the other half. Harmless
  direction, sloppy.
- SFO, DC, BOS tails use a static `hi_no` of 50 with no distance logic (they were
  excluded from the dynamic path pending data). This is the Boston T72 hole.
- Tails are exempt from the `P(yes) > 0.20` gate; combined with 15c rung spacing the
  tail ladder bids at fair−15, −30, −45, −60c. Rungs 3–5 are sold disaster
  insurance.
- The night ×2 size multiplier has no cited evidence in either direction.

---

## 3. Recommendations

### Tier 1 — mechanism-backed, do without waiting for more data

1. **Re-anchor the ladder to fair value and the market, not to static cents.**
   Top rung = `min(fair_NO_corrected − margin, best_NO_bid − 4c, global_ceiling≈70c)`.
   Rungs = 3–4 at 2c spacing (deepest ≥ 10c below the bid instead of ≥ 18c).
   Tail rungs never deeper than `fair_NO − 25c`. This removes the fills that every
   sample says lose (deep sweeps) and cuts order volume by >50% (the 416–518/day
   `ORDER_429` storms in June truncate ladders unpredictably today).
   Keep `hi_no` only as a per-city *risk ceiling*, not as the price.
2. **Global NO price floor for B-markets, start at 45c**, then verify by 5c band on
   the full fills table (the analyzer already emits `fill_price` buckets). The 41–50c
   band was the largest loss bucket in May and the 41–60c band is ≈0 in the Aug
   analysis; ≤40c has no positive sample anywhere in the repo.
3. **Disagreement kill switch.** At placement, compute market-implied P(yes) from the
   snapshot's `no_highest_bid/no_lowest_offer` mid. Skip the market if
   `market_P(yes) − model_P(yes) > 0.20` (market far more YES-confident than the
   model) — this is the Boston T72 / edge-quintile-5 pattern. Apply to tails
   especially. Threshold is a judgment call; 0.20 is wide enough to keep the modal
   bucket tradable.
4. **Replace the normal-with-σ-floor by the per-city empirical error CDF** for all
   buckets (or σ≈2.0–2.2 with t-tails as a stopgap). Apply `Average_corrected` to the
   tail lookup too. Refresh the empirical table from
   `KXHIGH_model_call_snapshots` × `KXHIGH_cli_readings` (the rolling-bias query
   already joins these) rather than the frozen 2026-04-24 backfill. This does little
   for P&L on its own under the current architecture; it is the prerequisite for 1
   and 3, and for measuring adverse selection honestly.
5. **Remove P&L-fitted per-city knobs**: `CITY_SIZE_MULT`, `CITY_MIN_NO_PRICE`, and
   the hand-set `hi_no` dispersion. Keep only mechanism-based per-city inputs
   (empirical CDF, station/peak-time logic). Also hold `BAND_TILT_MULT` at 1.5, do
   not scale it until it survives out-of-sample.

### Tier 2 — measure first, then act (all runnable today from BQ)

6. **Adverse-selection cost.** For every snapshotted market (filled or not), bucket
   by corrected model P(yes); compare realized YES rate for markets the bot filled vs
   did not fill. The gap, in points, is the toxicity tax per fill. This is the number
   the strategy lives or dies on and it has never been computed; n is ≈5,000+ since
   Apr 19.
7. **P&L per contract by fill session and rung depth.** Night (19–02 CT) vs day-of
   (05–10 CT); depth at placement (`order_no_price` vs snapshot `no_highest_bid`).
   Mechanism: the 12Z cycle and morning obs land during the day-of window; IMM found
   early fills half as toxic on hourly temp. If day-of fills are net negative, drop
   the 07:02 CT run or move expiry to 08:00 CT. `kxhigh_orderbook_logger.py` was
   built for exactly this cancel-time question.
8. **Signed distance, not absolute.** Actual runs warmer than the vendor mean by
   +0.3 to +1.1°F in every sample (8-city table means; May +0.88; April bias −1.0 in
   forecast−actual terms). If retail anchors on the forecast, NO on the bucket *below*
   μ is systematically better than NO on the bucket *above*. `backtest_filters.py`
   only tests |distance|; re-run it signed. This is the one place a genuine
   informational edge is plausible.
9. **Rebuild the validation dashboard on the full history.** It is frozen at
   2026-04-25 with 19 snapshot-covered markets. Read the Brier model-vs-market on
   all quoted markets — that is the fastest-accumulating verdict on whether the
   forecast layer adds anything.
10. **Re-derive P&L with the cash-flow method** (`90_ab_hi_no_report.sql` CTEs)
    attributed by `client_order_id`, so manual trades and closeouts stop
    contaminating the bot's record. Publish a lifetime KXHIGH number.

### Tier 3 — strategic

11. **Decide what this bot is for.** KXHIGH earned $5.80 of liquidity incentive in
    its life; the account's money comes from IMM rewards. If KXHIGH series carry a
    program pool (IMM blocklists `KXHIGH` only to avoid trading against this bot),
    quantify the $/day with IMM's program fetch. If it is material, IMM-style
    two-sided quoting with a NO skew on KXHIGH almost certainly dominates a
    one-sided directional ladder that pays no rewards. If it is not, run this bot
    small and instrumented as a research line, with the Tier 1 changes as the
    minimum to remove the fat left tail.
12. **Architecture.** A 4×/day cron with orders resting 3–6 hours cannot react to
    information; a persistent process that cancels when obs-vs-forecast drift or a
    fresh NWS/HRRR cycle moves the expected high would attack adverse selection
    directly. Sizeable build; only worth it after 6–7 show the fills are salvageable.

### Do not

- Add more band, city, or weekday tilts from P&L slices under ~500 markets.
- Re-enable the fair−3 cap A/B as designed; it caps the side that never fills.
- Scale size before Tier 1 plus measurements 6–7 are in. The June −$1,732 (64% from
  28 oversized markets) and May 500–900-contract positions are what scaling the
  current design looks like.
- Read the 17 committed digest days (Σ +$1,526) as a track record; they are the days
  someone chose to write up.

---

## 4. Expected effect, honestly stated

| Change | Mechanism | Expected effect | Confidence |
|---|---|---|---|
| Depth cap + NO floor + fewer rungs (1, 2) | removes sweep fills | removes most of the fat left tail; mean effect on the ≈0 41–60c book small | moderate |
| Disagreement kill switch (3) | refuses trades where the market has information the model lacks | eliminates Boston-T72 class losses; costs some winning "cheap NO" fills | moderate on direction, low on threshold |
| Empirical CDF / σ fix (4) | correct fair value | ≈0 alone; enables 1, 3, 6 | high that it is correct; low that it moves P&L by itself |
| Remove city tilts (5) | stop fitting noise | ≈0 expected, lower variance of future surprises | high |
| Session/depth measurement (6–7) | find where fills are uninformed | unknown; the only path to a positive book | — |
| Signed-distance tilt (8) | forecast cold bias | plausible +2–5c/ct on the affected buckets; regime-dependent (MOS bias flips seasonally) | low |

No combination above turns a ≈0 book into a large one at current fill volume. The
realistic best case is a small, positive, low-variance line (+$10–30/day) that is
mostly justified as infrastructure and data for the IMM program on the same markets.

---

## 5. Queries to run (BQ, in order)

1. Toxicity tax: `KXHIGH_model_call_snapshots` ⋈ `KXHIGH_settlements_clean`, LEFT
   JOIN `KXHIGH_fills_clean` → for each corrected-P(yes) decile, realized YES rate
   split by `has_fill`. Report the gap and n per decile.
2. Fill P&L per contract by (`order_no_price` band × session × depth-at-placement),
   cash-flow method, attributed by `client_order_id`.
3. Brier model vs market on all snapshotted markets since 2026-04-19, and per
   month.
4. Signed distance: P&L per contract by sign(strike − μ_corrected) × |distance| bin.
5. Lifetime KXHIGH cash-flow P&L, monthly, with manual (unstamped) orders excluded.

Refresh the empirical error CDF from the same joins (rolling 90 days, per city,
min 60 samples, shrink to the global CDF below that).

---

## Appendix — reproduction

All computed with `python3` against repo files:

- Table-implied SDs: hardcoded `data` matrix at `high_temp_trading.py:1165–1200`,
  mean/SD over rows −5..5 per city; P(modal) ≈ P(row 0) + ½[P(row 1) + P(row −1)].
  Bot P(modal) = 2Φ(1/σ_floor) − 1.
- May history: `bias_history.csv` (n=100, 6 cities, May 8–26): err mean +0.88, SD
  2.36, MAE 1.78, RMSE 2.51; P(|err|≤0.5)=0.38, P(|err|≤1)=0.48, P(|err|≥2)=0.39.
- May markets: `bias_sim_input.csv` ⋈ `bias_sim_orders.csv` (n=100): totals, price
  bands (avg price = `no_total_cost_dollars / |net_position|`), P(yes) bins,
  |strike−μ| bins, largest losses.
- City persistence: April per-city table from the dashboard HTML vs May per-city
  sums; `scipy.stats.spearmanr`.
- Power: n = (2.8 · σ_ROI / edge)², σ_ROI = 0.967 from the May file.
- `bias_sim.py`, `bias_sim_v2.py`, `bias_diag.py` re-run unchanged: rolling bias
  recovers +4.7% of the May loss; `hi_no_config` binding in 68/100 markets.
