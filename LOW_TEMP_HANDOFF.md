# KXLOW Low-Temperature Trading Bot — Handoff

**Script:** `low_temp_trading.py` · **Workflow:** `.github/workflows/run_low_temp_trading.yml`
**Status:** built 2026-07-20, adapted from `high_temp_trading.py` (KXHIGH bot). Not yet scheduled.

## What it trades

Kalshi `KXLOW<CITY>` events — "Lowest temperature in <city> on <date>?" — settled on the
**minimum** temp in the NWS CLI daily report (midnight-to-midnight **local** calendar day,
same CLI product and stations as KXHIGH). Strategy is the same maker NO-ladder as the high
bot: post_only NO bids, 2c ladder descending from `min(hi_no_config, fair_NO − 3c)`, on
bucket markets with P(yes) > 0.2, normal-CDF pricing off a 2-source forecast ensemble.

**Only 13 of the 20 city series actually list events** (as of Jul 2026): the T-prefix
cities (THOU TATL TDC TPHX TDAL TLV TOKC TSEA TSFO TSATX TMIN TNOLA TBOS). The classic 7
(NY CHI MIA LAX DEN PHIL AUS) are empty series shells — the bot keeps them configured and
quietly logs the 404 each run; they start trading automatically if Kalshi activates them.

Each event: 4 `between` bands (2°F wide, `B<center>.5`) + a cold tail (`less`) + a warm
tail (`greater`). Strikes are parsed from `strike_type`/`floor_strike`/`cap_strike`
(NOT the high bot's positional B→T trick — low events have TWO tails).

## Why it is not just the high bot with a sign flip

| | Highs (KXHIGH bot) | Lows (this bot) |
|---|---|---|
| Outcome prints | mid-afternoon (~14–16 local) | near sunrise (~05–07 local) |
| Info starts flowing | slowly through the morning | **immediately at local midnight** (midnight temp is ~3–6°F above the low vs ~15°F below the high) |
| Order expiry | 9–10 AM CT day-of | **local 23:59 the night before** (evening runs) / **02:59 local** (day-of runs) |
| Run boundary (`variable`) | ≥14:00 CT → tomorrow | **≥08:00 CT → tomorrow** (today's low is set by 8 AM) |
| WU field | `temperatureMax` | **`calendarDayTemperatureMin`** — `temperatureMin` is the *overnight-night-period* low, crosses midnight, does NOT match CLI settlement. Index matched via `validTimeLocal` dates. |
| NWS field | daily "day" period temp | **min over the target local day of the HOURLY forecast** (daily "Tonight" period also spans two calendar days) |
| Degenerate-day filter | midnight temp within 4.5°F of high → skip | **late-day cold drop**: 18–23h local forecast min within 4.5°F of 00–12h min → skip (front can reset the low at 23:59). Plus **midnight-print**: temp@00 within 1.5°F of day min → low prints at 00:00 → skip (evening runs). |
| Day-of bucket skip | obs ≥ bucket bottom | **obs ≤ bucket top** (low ≤ obs always) |
| Forecast busted | obs > forecast max + 2 | **obs < forecast min − 2** |
| Tails traded | warm tail, priced from empirical error dist | **none** (`TRADE_TAILS=false`) — no low-temp error distribution yet; the cold tail (radiational-cooling busts) is the fat one |
| σ floors | per-city, high-temp calibrated | **high-bot floors +0.3°F, default 1.5** (Tmin vendor error runs wider) |

Also different: **expiries never roll forward a day** (`compute_expiry_ts` returns None →
city skipped — the high bot's roll-forward would leave orders resting through the entire
outcome window); the cancel sweep covers **all** discovered markets, not just tradable
rows; the fair-NO cap is always on (no A/B machinery); UTF-8 stdout reconfigure for
Windows runs.

## Sizing / limits (probe phase)

`LOW_STARTING_CONTRACTS=8` per rung (×2 on evening runs), `LOW_MAX_CONTRACTS=50` per
market, ladder = 8 rungs × 2c, per-city mults all 1.0. Bump only after real KXLOW P&L.

## Schedule (GH Actions native cron — LIVE since 2026-07-22)

`schedule:` crons in the workflow file (UTC): **00:17** (~19:17 CDT evening, main),
**03:17** (~22:17 CDT refresh), **07:17** (~02:17 CDT day-of, CT/MT/PT), **08:47**
(~03:47 CDT day-of, MT/PT). Native cron is fine here (unlike the high bot, which needs
cron-job.org precision): expiries are fixed local-clock stops computed in-script, so GH
cron drift only shortens the quoting window, and runs firing past a city's stop skip it.
Runs at any other hour are safe — cities past their quote stop are skipped up front (a
late-morning run does nothing but sweep cancels). Note GH pauses scheduled workflows
after 60 days without repo activity (this repo commits near-daily, so moot).

## BigQuery

Prefix `KXLOW_`: `market_snapshot`, `orders`, `runs`, `alerts` in
`elite-contact-446323-q7.Kalshi` (auto-created on first live run — remember the
retention-guard rule if any of these are ever retired). Actual lows come from the shared
`KXHIGH_cli_readings.low_temp_f` (CLI fetcher already captures them). Rolling bias
correction (same shrinkage design as the high bot) reads `KXLOW_market_snapshot` earliest
run per city/day joined to CLI lows — it's a silent no-op until snapshot history
accumulates.

## Env knobs

`LOW_DRY_RUN` (full run, no cancels/orders/BQ writes), `LOW_STARTING_CONTRACTS`,
`LOW_MAX_CONTRACTS`, `LOW_SAFETY_MARGIN_CENTS` (default 3), `TRADE_TAILS`,
`BIAS_CORRECTION_ENABLED` + `BIAS_*` (same as high bot), `KALSHI_PRIVATE_KEY[_PATH]`,
`ALERT_EMAIL_*`.

## Later (calibration-gated)

- Build a low-temp actual-vs-forecast error distribution (template:
  `analysis/kxhigh/python/build_forecast_error_dist.py`) → enable tails.
- Per-city hi_no / size tilts once P&L differentiates cities.
- Tighten σ floors from measured Tmin error.
- Analysis views + dashboard integration (mirror `analysis/kxhigh/`).
