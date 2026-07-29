# Monthly rain ladder model — rain_monthly.py (strategy 4)

Prices every rung of the KXRAIN<CITY>M monthly cumulative-rain ladders as
P(MTD + remaining > strike) and recommends capped taker trades on big
model-vs-market divergence. **DRY-RUN BY DEFAULT** — `--live` is the only
thing that places orders, and Jack arms it deliberately.

## Why this market

Cumulative precip is monotone and the rungs close early on cross — a
one-touch ladder (crypto_touch_mm's payoff shape) on a variable that is
publicly observed (station obs), publicly forecast (NWS QPF), and has 30-50
years of station history for the remaining-days distribution. Real volume
(20-100k contracts/rung on actives).

## Settlement stations (VERIFIED 2026-07-28)

Cross-checked live: IEM parsed-CLI month-to-date vs Kalshi's settled rungs —
9/9 consistent. **Monthlies ≠ dailies for two cities**: CHI settles at
MIDWAY (CLIMDW), HOU at HOBBY (CLIHOU); the dailies use O'Hare/IAH.

| city | series | station | ACIS/ICAO | IEM | history yrs (Jul) |
|---|---|---|---|---|---|
| AUS | KXRAINAUSM | Austin Bergstrom | KAUS | AUS/TX_ASOS | 44 |
| CHI | KXRAINCHIM | Chicago **Midway** | KMDW | MDW/IL_ASOS | 29 |
| DAL | KXRAINDALM | DFW | KDFW | DFW/TX_ASOS | 46 |
| DEN | KXRAINDENM | Denver Intl | KDEN | DEN/CO_ASOS | 32 |
| HOU | KXRAINHOUM | Houston **Hobby** | KHOU | HOU/TX_ASOS | 46 |
| MIA | KXRAINMIAM | Miami Intl | KMIA | MIA/FL_ASOS | 46 |
| NYC | KXRAINNYCM | Central Park | KNYC | NYC/NY_ASOS | 46 |
| SEA | KXRAINSEAM | Sea-Tac | KSEA | SEA/WA_ASOS | 46 |
| STP | KXRAINSTPM | St Pete Albert Whitted | KSPG | SPG/FL_ASOS | 28 |

## Model

- **MTD_effective** = latest CLI `precip_month` (IEM json/cli.py) + IEM daily
  obs for days after the report + today's `pday` (IEM currents), with a
  double-count guard when the evening CLI already covers part of today.
  Live proof at ship time: NYC CLI said 3.97 but 0.05" fell post-report —
  effective 4.02 crossed rung 4 exactly as the market's 99c bid said.
- **Remaining** = Monte Carlo (default 20k): sample a historical year's
  calendar-aligned remaining-days segment (ACIS, autocorrelation for free),
  then with probability `RMM_FORECAST_WEIGHT` (0.7) replace each in-horizon
  day with a forecast draw: wet ~ Bernoulli(PoP), amount ~ Gamma(shape 0.8,
  mean QPF/PoP), a shared lognormal regime multiplier (σ 0.35) correlating
  the horizon days. NWS gridpoint QPF is mm, 6h periods, ~3-7 day horizon;
  today's already-elapsed periods are dropped (that rain is in the obs).
- **Strictly greater**: a final total exactly equal to the strike is NO.

## Calibration (climatology backbone, 2026-07-28)

Walk-forward leave-one-out over all 9 stations x Jul+Aug x 6 check-days x
strikes 1-10: **43,080 predictions, Brier 0.0835 vs 0.2224 constant
baseline (62% skill)**; reliability flat — every decile within ±4pts of
realized, most ±1.5. The FORECAST layer is not backtestable without
archived forecasts — treat the first weeks of the ledger as its calibration
sample before sizing up (same discipline as the rain directional module).

## Guards / caps (env-tunable, all RMM_*)

| Guard | Default | Why |
|---|---|---|
| MIN_EDGE_YES / NO | 6c / 9c net of quadratic fee | NO locks collateral to month-end (frozen-inventory lesson) |
| MAX_ORDER / MAX_POS | 10 / 50 net incl. existing account position | shares the account with IMM's frozen rain inventory + manual |
| MAX_RUN_COLLATERAL / CITY | $250 / $100 per run | blast-radius cap |
| MAX_SPREAD | 15c | no taking into empty AUG books (2x98) |
| BOUNDARY_IN | 0.05" | obs-vs-CLI rounding near a strike is unknowable |
| STALE_CLI_HOURS | 40h | SEA's report lags; a stale MTD is untradeable |
| Order style | taker limit at touch, 90s exchange expiry, `rmm-` ids | no resting book, no MM interaction |

## Usage

```
python rain_monthly.py                    # dry scan, all cities, both months
python rain_monthly.py --city NYC --samples 50000 --seed 7
python rain_monthly.py --live             # ARMED: places capped orders
```
Every scan logs per-rung fair vs market and writes
`run-logs\rain-monthly\scan_*.json`; every recommendation (dry or live)
appends to `run-logs\rain-monthly\rmm_ledger.csv` — the performance loop.
Tests: `python -m unittest test_rain_monthly` (25, all network mocked).

## Ship-time signals (dry, 2026-07-29 00:08Z)

Model vs market agreed tightly where the forecast dominates (NYC rung 5:
fair 60.4 vs 59x62) and diverged where the model is drier than the crowd:
BUY_YES NYC-6 @22c (fair 33) and NYC-7 @11c (fair 19); BUY_NO STP-8 @12c
(fair 56 vs 88x90 market), STP-9, MIA-5 @60c (fair 20 vs 40x43), CHI-AUG-3,
NYC-AUG-2. Divergences are concentrated in Florida convection — if the
first settled week shows the market beating the model there, raise
FORECAST_WEIGHT's climatology share (lower the env) or the edge thresholds.

## Known gaps (deliberate v1)

- No radar/nowcast input: the market sees a storm cell before the QPF grid
  updates. The BOUNDARY + spread guards limit the damage; don't arm --live
  city-wide during an active convective outbreak until the ledger says the
  model holds up.
- No maker mode (taker only). The AUG 2x98 books are a maker opportunity,
  but IMM owns resting-quote logic in this account; keep separation.
- Forecast layer unvalidated by backtest (no archived-forecast source);
  climatology layer is the validated backbone.
- Multi-agent adversarial review pending (subagent session limit at build
  time); self-review + 25 unit tests only. Run a /code-review before
  first --live arming.
