# Q15 shadow test -- pre-registered variants and verdict rules

Written 2026-10-22 ~22:00Z, BEFORE the recorder's first hour (Jack 2026-10-22:
"Yes use a live price feed like Miami temp, with recorder and a week of
testing" / "It's paid, do FX and crypto"). Committed to git so the rules
cannot drift with the data. Lessons carried over from the hourly-temp test
(TEMP_SHADOW.md, memory shadow-must-survive-live): judge on the STRESS fill
model, exclude the best and worst hour in a robust total, and never re-tune
on the window being judged.

## Markets

KXEURUSD15M, KXGBPUSD15M, KXUSDJPY15M, KXAUDUSD15M, KXUSDCAD15M ("price up in
next 15 mins?", Pyth value at close >= value at open) and KXCRYPTOLEAD15M
(which of BTC ETH SOL XRP HYPE has the highest 15-min return, CF Benchmarks),
priced off Kraken's FX mid and Hyperliquid's mids. Pool and target from the
recorded programs (FX $50, race $20 per market per 15 min; target 300).

## Fair value (fixed before data)

- FX: P(up) = P(S_T >= S_0) for a driftless random walk on the log mid, with
  sigma = the EWMA (half-life 10 min) of 1-second squared log changes of the
  proxy mid over the last 60 min, floored at 0.2 x the running median sigma
  since the recording began; a half-tick continuity term for "at least"
  (ties resolve Yes).
- Race: P(coin i leads) by 4,000 Monte Carlo draws of the remaining returns
  (multivariate normal, EWMA covariance of 5-second log returns over 60 min)
  added to each coin's return since the open.
- S_0 = the proxy mid at the open second (the fidelity check compares Kalshi's
  results with the proxy's own up/down call at the close).

## Variants (fixed)

| id | quotes | when |
|---|---|---|
| FAIR4 | YES bid at fair - 4c, YES ask at fair + 4c (whole cents, inside 2-98c), 100 per side | tau >= 60 s, the proxy fresh (< 5 s) |
| FAIR4_night | FAIR4 | only 20:00-07:00 ET |
| FAIR8 | as FAIR4 at +-8c | as FAIR4 |
| FAIR8_night | FAIR8 | only 20:00-07:00 ET |
| JOIN | join the best bid / ask when it is >= 6c on the safe side of fair | as FAIR4 |
| FAV_night | bid the favorite at <= 97c when fair >= 0.995 | 20:00-07:00 ET, tau <= 5 min |

No fillers or 1c pads (they lost in every hourly-temp variant). One market
order book is never quoted against itself; quotes do not cross the book.

## Fill models

- stress (DECIDES): any trade at our price or through it fills our whole
  resting size at our price, before anyone else in the queue.
- queue (reported): fills only what reaches our place in the queue.
Fills are marked to the market's settlement (0 / 100c).

## Rewards

Each second, our resting quotes are overlaid on the rebuilt book and scored
with temp_shadow_scoring.score (the IMM's exact scorer) at the program's
target and discount; reward = our share x the program's pool per second.

## Verdict (ET days 2026-10-23 .. 2026-10-29)

A variant is PILOT ELIGIBLE only if, under the stress model, ALL of:
1. total net (reward + fill P&L at settlement) > 0;
2. >= 5 of 7 days positive;
3. the robust total (dropping its best and worst hour) > 0;
4. a 90% bootstrap CI over days whose lower bound > -$50 (so one lucky day
   cannot carry it);
5. the fair model's Brier score beats the market's own mid on the same
   seconds (otherwise the "fair" is not adding information).
Otherwise NO-GO (or EXTEND if 1-4 pass with fewer than 7 recorded days).
A pilot, if any, is Jack's call: 1/3 size, the variant's own hours, a $200
daily halt and a live-vs-shadow divergence breaker.
