"""Build the live KXHIGH dashboard from BigQuery state.

Refresh model: this is a build script. Re-run it (cron, GH Actions, or manual)
and the HTML page is regenerated. The page also has a meta-refresh so an open
browser tab pulls in regenerations automatically.

Data sources (read-only):
  KXHIGH_market_snapshot  — per-market forecast + model yes_prob (every trading run)
  KXHIGH_orderbook_log    — per-market bid/offer (every 15 min cron)
  KXHIGH_runs             — script run health
  KXHIGH_orders           — today's orders (for A/B variant tally)
"""
from __future__ import annotations

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "CLOUDSDK_PYTHON",
    r"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe",
)

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cities import CITIES  # noqa: E402

PROJECT = "elite-contact-446323-q7"
DATASET = "Kalshi"
LOCATION = "northamerica-northeast1"

OUTPUT = Path(__file__).resolve().parents[1] / "output" / "live_dashboard.html"


def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _q(sql: str) -> pd.DataFrame:
    return _client().query(sql, location=LOCATION).to_dataframe()


# ─────────────────────────── queries ────────────────────────────────
#
# Trading-bot timestamp bug: high_temp_trading.py stamps run_date and
# orders.created_at with `datetime.now(US/Central)` *without* a timezone,
# which BigQuery then labels as UTC. The stored value is therefore CT
# wall-clock-time tagged as UTC — five hours earlier than reality during
# CDT, six during CST. We undo the mislabeling at read time with
#     TIMESTAMP(DATETIME(col), "America/Chicago")
# which interprets the naive datetime in CT and returns a real UTC
# timestamp. Applied below wherever a bot-written timestamp surfaces.
SQL_LATEST_SNAPSHOT = f"""
WITH ranked AS (
  SELECT * REPLACE(TIMESTAMP(DATETIME(run_date), "America/Chicago") AS run_date)
  FROM `{PROJECT}.{DATASET}.KXHIGH_market_snapshot`
  WHERE forecast_date BETWEEN DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
                          AND DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
)
SELECT * FROM ranked
QUALIFY ROW_NUMBER() OVER (PARTITION BY market_ticker ORDER BY run_date DESC) = 1
"""

SQL_FORECAST_HISTORY = f"""
SELECT city, forecast_date,
       TIMESTAMP(DATETIME(run_date), "America/Chicago") AS run_date,
       forecast_avg, forecast_std,
       nws, accuweather, weather_underground,
       observed_temp_f, peak_temp_f
FROM `{PROJECT}.{DATASET}.KXHIGH_market_snapshot`
WHERE forecast_date BETWEEN DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
                        AND DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
  AND TIMESTAMP(DATETIME(run_date), "America/Chicago")
        >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
QUALIFY ROW_NUMBER() OVER (PARTITION BY city, forecast_date, run_date) = 1
ORDER BY city, forecast_date, run_date
"""

SQL_NWS_OBS_24H = f"""
-- NWS observations from the last 48h. Pairs with the forecast SQL below
-- to give a 24h-centered sparkline per (city, local-date). 48h covers:
--   - Yesterday's full local day (for archive page rendering of 04-29's
--     card on the 04-30 build that still overwrites yesterday's archive)
--   - Today's full local day so far (for live cards)
-- per_city_view groups by (city, local_date_in_city_tz) so each card
-- shows its own day's data — yesterday's archive doesn't bleed today's
-- obs into yesterday's sparkline once the rolling window slides.
SELECT city_abv, station, polled_at, observation_time, temperature_f
FROM `{PROJECT}.{DATASET}.KXHIGH_nws_obs_log`
WHERE observation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
  AND temperature_f IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY city_abv, observation_time ORDER BY polled_at DESC) = 1
ORDER BY city_abv, observation_time
"""

SQL_FORECAST_PEAK = f"""
-- Predicted peak (max temperature) hour today per city, computed live from
-- the latest NWS hourly forecast we've polled. Refreshes every 30 min as
-- new forecasts come in. Independent of the trading bot's snapshot
-- (which only updates 4×/day).
WITH latest AS (
  SELECT city_abv, period_start, temperature_f
  FROM `{PROJECT}.{DATASET}.KXHIGH_nws_hourly_forecast`
  WHERE DATE(period_start, "America/New_York") = CURRENT_DATE("America/New_York")
    AND temperature_f IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY city_abv, period_start ORDER BY polled_at DESC) = 1
)
SELECT city_abv,
       period_start AS peak_hour,
       temperature_f AS peak_temp
FROM latest
QUALIFY ROW_NUMBER() OVER (PARTITION BY city_abv ORDER BY temperature_f DESC, period_start ASC) = 1
"""

SQL_NWS_HOURLY_FCST = f"""
-- Forecast for the 24h sparkline window centered on now:
--   Past hours [now-12h, now]: the latest forecast made BEFORE the hour
--     started — i.e., "what NWS predicted just before it happened".
--   Future hours [now, now+12h]: the latest forecast we have for that
--     hour, regardless of when it was polled.
-- This lets the user compare actuals-vs-historical-forecast on the past
-- side and see the upcoming forecast curve on the future side.
SELECT city_abv, period_start, period_end, temperature_f, short_forecast,
       polled_at, forecast_update_ts
FROM `{PROJECT}.{DATASET}.KXHIGH_nws_hourly_forecast`
WHERE period_start >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 12 HOUR)
  AND period_start <= TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 12 HOUR)
  AND (
    -- Past hours: only "made before the hour" forecasts.
    (period_start <= CURRENT_TIMESTAMP() AND polled_at < period_start)
    -- Future hours: any forecast (QUALIFY below picks the freshest).
    OR period_start > CURRENT_TIMESTAMP()
  )
QUALIFY ROW_NUMBER() OVER (PARTITION BY city_abv, period_start ORDER BY polled_at DESC) = 1
ORDER BY city_abv, period_start
"""

SQL_MARKET_ARM = f"""
-- A/B arm per market_ticker. The bot's sticky assignment means a given
-- market_ticker belongs to one arm across runs, but in case of edge cases
-- (e.g., assignment changed mid-flight) we take the most-recent arm and
-- keep that as the canonical attribution.
SELECT market_ticker, ab_arm
FROM `{PROJECT}.{DATASET}.KXHIGH_orders`
QUALIFY ROW_NUMBER() OVER (PARTITION BY market_ticker ORDER BY created_at DESC) = 1
"""

SQL_TODAY_PEAK_OBS = f"""
-- Raw obs from the last 48h, used by per_city_view to compute peak per
-- (city, local_date). We fetch the raw rows (not a SQL aggregate) because
-- Kalshi settles each market on the local 24h window of that station's
-- timezone — Pacific cities' local-day spans roughly the back half of one
-- ET day plus the front quarter of the next, so any single ET-aligned
-- aggregate misattributes obs across forecast_dates. Python groups by
-- city's tz (from cities.py) into a (city_abv, local_date) → peak map.
SELECT city_abv, observation_time, temperature_f
FROM `{PROJECT}.{DATASET}.KXHIGH_nws_obs_log`
WHERE observation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
  AND temperature_f IS NOT NULL
"""

SQL_CLI_RECENT = f"""
-- Latest CLI reading per (city, event_date) for today + yesterday (ET).
-- The CLI ("CLImate") is the WFO's official daily summary that Kalshi
-- settles on. Once published (typically late afternoon / early evening)
-- it's the authoritative daily-high source — beats the 5-min METAR max
-- by ~1°F because CLI uses 1-min sensor data + SPECIs that don't appear
-- in /observations. We override the dashboard's "high so far" with this
-- value once the CLI looks final (see _cli_is_preliminary).
SELECT city_abv, event_date, high_temp_f, low_temp_f, high_time, fetched_at
FROM `{PROJECT}.{DATASET}.KXHIGH_cli_readings`
WHERE event_date >= DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
  AND event_date <= CURRENT_DATE("America/New_York")
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY city_abv, event_date ORDER BY fetched_at DESC
) = 1
"""

SQL_LATEST_POSITIONS = f"""
-- Take ONLY the most recent poll batch — Kalshi's /portfolio/positions
-- returns the current open inventory in one call, so all rows from a
-- single poll share the same polled_at and represent a complete
-- snapshot. A row-number-per-market approach with a 6h fall-back was
-- pulling in rows from earlier polls for markets that have since
-- settled and dropped out of the portfolio, inflating cost.
WITH latest_poll AS (
  SELECT MAX(polled_at) AS ts
  FROM `{PROJECT}.{DATASET}.KXHIGH_positions_log`
  WHERE polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
)
SELECT p.market_ticker, p.polled_at, p.position,
       p.market_exposure_dollars, p.realized_pnl_dollars,
       p.total_traded_dollars, p.fees_paid_dollars,
       p.resting_orders_count
FROM `{PROJECT}.{DATASET}.KXHIGH_positions_log` p, latest_poll lp
WHERE p.polled_at = lp.ts
"""

SQL_TODAY_ORDERS_DETAIL = f"""
-- Per-order detail for markets that are currently open (event_date is
-- today or tomorrow ET). Scoping by event_date — instead of order
-- created_at — picks up the 19:02 ET pre-market run that places orders
-- for tomorrow's market the night before; those are the orders that
-- typically built the cost basis the user sees in the morning.
--
-- created_at is CT-clock-stored-as-UTC (see top-of-file note); the
-- SELECTed value is timestamp-corrected so the modal's "created (ET)"
-- column shows actual wall-clock ET.
WITH open_markets AS (
  SELECT DISTINCT market_ticker
  FROM `{PROJECT}.{DATASET}.KXHIGH_orderbook_log`
  WHERE event_date BETWEEN CURRENT_DATE("America/New_York")
                       AND DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
    AND polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
),
relevant_orders AS (
  SELECT o.city, o.city_abv, o.market_ticker, o.contracts, o.no_price,
         o.fair_no_cents, o.effective_hi_no, o.ab_arm,
         o.client_order_id, o.kalshi_order_id, o.expiration_ts,
         TIMESTAMP(DATETIME(o.created_at), "America/Chicago") AS created_at
  FROM `{PROJECT}.{DATASET}.KXHIGH_orders` o
  JOIN open_markets m ON m.market_ticker = o.market_ticker
),
-- KXHIGH_fills_live is APPEND-only, so the same fill_id reappears every
-- cron tick. UNION ALL + QUALIFY ROW_NUMBER OVER fill_id dedupes
-- correctly (UNION DISTINCT would only collapse exact-row duplicates,
-- which still over-counts when count_fp shifts type between the two
-- tables or order_id is null in one and not the other).
all_fills AS (
  SELECT order_id, fill_id, count_fp FROM (
    SELECT order_id, fill_id, CAST(count_fp AS FLOAT64) AS count_fp
    FROM `{PROJECT}.{DATASET}.KXHIGH_fills`
    UNION ALL
    SELECT order_id, fill_id, CAST(count_fp AS FLOAT64) AS count_fp
    FROM `{PROJECT}.{DATASET}.KXHIGH_fills_live`
  )
  QUALIFY ROW_NUMBER() OVER (PARTITION BY fill_id ORDER BY order_id) = 1
),
fill_qty AS (
  SELECT order_id, SUM(ABS(count_fp)) AS filled_count
  FROM all_fills
  GROUP BY order_id
)
SELECT
  o.*,
  COALESCE(fq.filled_count, 0) AS filled_count
FROM relevant_orders o
LEFT JOIN fill_qty fq ON fq.order_id = o.kalshi_order_id
ORDER BY o.created_at DESC
"""

SQL_LATEST_ORDERBOOK = f"""
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY market_ticker ORDER BY polled_at DESC) AS rn
  FROM `{PROJECT}.{DATASET}.KXHIGH_orderbook_log`
  WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
                       AND DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
    AND polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
)
SELECT * FROM ranked WHERE rn = 1
"""

SQL_RUN_HEALTH = f"""
WITH ends AS (
  SELECT script_name, run_id, event_at, exit_status, duration_seconds, n_orders_placed
  FROM `{PROJECT}.{DATASET}.KXHIGH_runs`
  WHERE event = 'end'
    AND event_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
)
SELECT script_name, run_id, event_at, exit_status, duration_seconds, n_orders_placed
FROM ends
QUALIFY ROW_NUMBER() OVER (PARTITION BY script_name ORDER BY event_at DESC) <= 5
ORDER BY script_name, event_at DESC
"""

SQL_RUNS_RAW_28H = f"""
-- Unfiltered run-end events for the last 28h, used by the missed-run
-- detector to compare scheduled cron slots vs actual completions. The
-- main SQL_RUN_HEALTH query caps at 5 rows per script which isn't
-- enough to evaluate four expected daily trading-bot slots.
SELECT script_name, event_at, exit_status
FROM `{PROJECT}.{DATASET}.KXHIGH_runs`
WHERE event = 'end'
  AND event_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 28 HOUR)
ORDER BY event_at DESC
"""

SQL_HOURLY_FCST_DRIFT = f"""
-- Predicted-peak-of-today per (city, polled_at) for the last 12h. Each
-- cron tick stores the full hourly NWS forecast; taking MAX temp over
-- today's-ET hours gives one daily-high-prediction point per poll.
-- Two such points (oldest within 12h vs latest) drive a "drift since
-- last cron sweep" alert that refreshes every 30 min — in contrast to
-- the snapshot-based alerts that only update when the trading bot runs.
WITH peak_per_poll AS (
  SELECT city_abv, polled_at,
         MAX(temperature_f) AS predicted_peak
  FROM `{PROJECT}.{DATASET}.KXHIGH_nws_hourly_forecast`
  WHERE DATE(period_start, "America/New_York") = CURRENT_DATE("America/New_York")
    AND temperature_f IS NOT NULL
    AND polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 12 HOUR)
  GROUP BY city_abv, polled_at
)
SELECT city_abv, polled_at, predicted_peak
FROM peak_per_poll
ORDER BY city_abv, polled_at
"""

SQL_NWS_HOURLY_FCST_FULL = f"""
-- Full hourly-forecast trajectory for the per-city drilldown chart.
--
-- One line per *distinct NWS forecast issue*, NOT per poll. We poll
-- every ~20 min but NWS itself only updates each grid's forecast every
-- ~6h (some WFOs are faster). Without dedupe, the chart draws 15+
-- identical lines stacked on top of each other and looks like 3-4
-- lines anyway — confusing because the legend implies more diversity
-- than exists. Grouping by forecast_update_ts collapses the redundant
-- polls into one line per actual forecast issue.
WITH per_issue AS (
  SELECT city_abv, forecast_update_ts, period_start, temperature_f,
         MIN(polled_at) OVER (PARTITION BY city_abv, forecast_update_ts) AS first_seen,
         ROW_NUMBER() OVER (
           PARTITION BY city_abv, forecast_update_ts, period_start
           ORDER BY polled_at DESC
         ) AS rn
  FROM `{PROJECT}.{DATASET}.KXHIGH_nws_hourly_forecast`
  WHERE DATE(period_start, "America/New_York") IN (
          CURRENT_DATE("America/New_York"),
          DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
        )
    AND polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 HOUR)
    AND temperature_f IS NOT NULL
    AND forecast_update_ts IS NOT NULL
)
SELECT city_abv, forecast_update_ts, first_seen AS polled_at,
       period_start, temperature_f
FROM per_issue
WHERE rn = 1
ORDER BY city_abv, forecast_update_ts, period_start
"""

SQL_RECENT_ACTIVITY = f"""
-- Most recent fills across both KXHIGH_fills (nightly) and
-- KXHIGH_fills_live (every cron tick), deduped by fill_id and capped
-- at 15. Drives the right-rail "Recent activity" panel so the user can
-- see what's been crossing without opening a per-city modal.
--
-- KXHIGH_fills stores numeric/timestamp columns as STRING (Kalshi's
-- precision-preserving wire format) — explicit CASTs unify both tables
-- before UNION.
WITH all_fills AS (
  SELECT fill_id, order_id, market_ticker, side, action,
         CAST(count_fp AS FLOAT64) AS count_fp,
         CAST(no_price_dollars AS FLOAT64) AS no_price_dollars,
         CAST(yes_price_dollars AS FLOAT64) AS yes_price_dollars,
         CAST(created_time AS TIMESTAMP) AS created_time
  FROM `{PROJECT}.{DATASET}.KXHIGH_fills`
  UNION ALL
  SELECT fill_id, order_id, market_ticker, side, action,
         count_fp, no_price_dollars, yes_price_dollars, created_time
  FROM `{PROJECT}.{DATASET}.KXHIGH_fills_live`
)
SELECT fill_id, order_id, market_ticker, side, action,
       count_fp, no_price_dollars, yes_price_dollars, created_time
FROM all_fills
WHERE market_ticker LIKE 'KXHIGH%'
QUALIFY ROW_NUMBER() OVER (PARTITION BY fill_id ORDER BY created_time DESC) = 1
ORDER BY created_time DESC
LIMIT 15
"""

SQL_DAILY_PNL = f"""
-- Settled P&L per event_date over the last 30 days. Drives the History
-- modal so the user can see whether the bot's been making or losing
-- money over time without leaving the dashboard.
SELECT
  event_date,
  COUNT(*)                                                      AS n_markets,
  COUNTIF(pnl_dollars > 0)                                       AS n_wins,
  ROUND(SUM(total_cost_dollars), 2)                              AS total_cost,
  ROUND(SUM(pnl_dollars), 2)                                     AS pnl,
  ROUND(SAFE_DIVIDE(SUM(pnl_dollars), SUM(total_cost_dollars)) * 100, 1) AS roi_pct
FROM `{PROJECT}.{DATASET}.KXHIGH_settlements_clean`
WHERE event_date >= DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 30 DAY)
  AND event_date <  CURRENT_DATE("America/New_York")
GROUP BY event_date
ORDER BY event_date DESC
"""

SQL_SETTLED_PNL_PER_MARKET = f"""
-- Per-market realized P&L for markets that settled in the last 2 ET
-- days. Drives the per_city_view fallback for closed markets: once
-- Kalshi processes settlement, the position drops out of
-- KXHIGH_positions_log, so the dashboard's `realized_pnl_dollars` for
-- those buckets goes null. settlements_clean.pnl_dollars is the
-- canonical realized value; we merge it in as a fallback so yesterday's
-- archive shows accurate P&L instead of $0.
SELECT
  market_ticker,
  pnl_dollars                  AS settled_pnl_dollars,
  total_cost_dollars           AS settled_cost_dollars,
  fee_cost_dollars             AS settled_fees_dollars,
  total_payout_dollars         AS settled_payout_dollars,
  result                       AS settled_result,
  net_position                 AS settled_net_position
FROM `{PROJECT}.{DATASET}.KXHIGH_settlements_clean`
WHERE event_date >= DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 2 DAY)
"""

SQL_TODAY_ORDERS = f"""
SELECT
  COALESCE(ab_arm, 'none') AS ab_arm,
  COUNT(*) AS n_orders,
  SUM(contracts) AS n_contracts,
  COUNT(DISTINCT market_ticker) AS n_markets,
  COUNT(DISTINCT city) AS n_cities
FROM `{PROJECT}.{DATASET}.KXHIGH_orders`
-- created_at is CT-clock-stored-as-UTC (see top-of-file note); convert
-- back to a real UTC timestamp before applying the today-ET filter.
WHERE DATE(TIMESTAMP(DATETIME(created_at), "America/Chicago"),
           "America/New_York") = CURRENT_DATE("America/New_York")
GROUP BY ab_arm
"""

SQL_TODAY_FILL_RATE = f"""
-- Contract-based fill rate, scoped to **currently-open markets** so the
-- numerator/denominator align with the cost-basis number on the top
-- strip.
--
-- Fills come from BOTH tables:
--   KXHIGH_fills       → nightly TRUNCATE/load (history up to last cycle)
--   KXHIGH_fills_live  → APPENDed every cron tick (today's fresh fills)
-- We UNION DISTINCT on fill_id so each fill counts once even if it
-- appears in both tables.
WITH open_markets AS (
  SELECT DISTINCT market_ticker
  FROM `{PROJECT}.{DATASET}.KXHIGH_orderbook_log`
  WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
                       AND DATE_ADD(CURRENT_DATE("America/New_York"), INTERVAL 1 DAY)
    AND polled_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
),
relevant_orders AS (
  SELECT o.kalshi_order_id, o.contracts, o.ab_arm
  FROM `{PROJECT}.{DATASET}.KXHIGH_orders` o
  JOIN open_markets m ON m.market_ticker = o.market_ticker
  WHERE o.kalshi_order_id IS NOT NULL
    -- created_at is CT-stored-as-UTC; restrict to the last ~30 hours of
    -- real wall-clock time (covers yesterday-evening pre-market through
    -- this morning's runs).
    AND TIMESTAMP(DATETIME(o.created_at), "America/Chicago")
          >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 HOUR)
),
all_fills AS (
  -- Cast count_fp to FLOAT64 in both branches (KXHIGH_fills stores it as
  -- STRING, fills_live as FLOAT). Dedupe by fill_id via ROW_NUMBER —
  -- fills_live is APPEND-only and re-inserts the same fill every cron
  -- tick, so a plain UNION DISTINCT still over-counts whenever the row
  -- isn't byte-identical across tables.
  SELECT order_id, fill_id, count_fp FROM (
    SELECT order_id, fill_id, CAST(count_fp AS FLOAT64) AS count_fp
    FROM `{PROJECT}.{DATASET}.KXHIGH_fills`
    UNION ALL
    SELECT order_id, fill_id, CAST(count_fp AS FLOAT64) AS count_fp
    FROM `{PROJECT}.{DATASET}.KXHIGH_fills_live`
  )
  QUALIFY ROW_NUMBER() OVER (PARTITION BY fill_id ORDER BY order_id) = 1
),
fill_contracts AS (
  SELECT order_id, SUM(ABS(count_fp)) AS filled_contracts
  FROM all_fills
  GROUP BY order_id
),
joined AS (
  SELECT
    CAST(o.contracts AS FLOAT64) AS contracts,
    LEAST(CAST(o.contracts AS FLOAT64), COALESCE(fc.filled_contracts, 0.0)) AS filled,
    o.ab_arm
  FROM relevant_orders o
  LEFT JOIN fill_contracts fc ON fc.order_id = o.kalshi_order_id
)
SELECT
  SUM(contracts)                                            AS placed,
  SUM(filled)                                               AS filled,
  SUM(IF(ab_arm = 'control',   contracts, 0))               AS placed_control,
  SUM(IF(ab_arm = 'control',   filled,    0))               AS filled_control,
  SUM(IF(ab_arm = 'treatment', contracts, 0))               AS placed_treatment,
  SUM(IF(ab_arm = 'treatment', filled,    0))               AS filled_treatment
FROM joined
"""


def load() -> dict:
    out = {
        "snapshot": _q(SQL_LATEST_SNAPSHOT),
        "forecast_history": _q(SQL_FORECAST_HISTORY),
        "orderbook": _q(SQL_LATEST_ORDERBOOK),
        "runs": _q(SQL_RUN_HEALTH),
        "orders": _q(SQL_TODAY_ORDERS),
        "orders_detail": _q(SQL_TODAY_ORDERS_DETAIL),
    }
    # The obs and positions tables may not exist yet (created on first
    # orderbook-logger run after this change ships). Tolerate missing tables
    # so the rest of the dashboard still renders.
    try:
        out["nws_obs"] = _q(SQL_NWS_OBS_24H)
    except Exception as e:
        print(f"  KXHIGH_nws_obs_log unavailable yet ({type(e).__name__}); skipping obs panel")
        out["nws_obs"] = pd.DataFrame()
    try:
        out["nws_fcst"] = _q(SQL_NWS_HOURLY_FCST)
    except Exception as e:
        print(f"  KXHIGH_nws_hourly_forecast unavailable yet ({type(e).__name__}); skipping forecast curve")
        out["nws_fcst"] = pd.DataFrame()
    try:
        out["positions"] = _q(SQL_LATEST_POSITIONS)
    except Exception as e:
        print(f"  KXHIGH_positions_log unavailable yet ({type(e).__name__}); using stale snapshot positions")
        out["positions"] = pd.DataFrame()
    try:
        out["peak_obs"] = _q(SQL_TODAY_PEAK_OBS)
    except Exception:
        out["peak_obs"] = pd.DataFrame()
    try:
        out["cli"] = _q(SQL_CLI_RECENT)
    except Exception as e:
        print(f"  KXHIGH_cli_readings unavailable ({type(e).__name__}); skipping CLI override")
        out["cli"] = pd.DataFrame()
    try:
        out["market_arm"] = _q(SQL_MARKET_ARM)
    except Exception:
        out["market_arm"] = pd.DataFrame()
    try:
        out["fill_rate"] = _q(SQL_TODAY_FILL_RATE)
    except Exception:
        out["fill_rate"] = pd.DataFrame()
    try:
        out["daily_pnl"] = _q(SQL_DAILY_PNL)
    except Exception:
        out["daily_pnl"] = pd.DataFrame()
    try:
        out["settled_pnl"] = _q(SQL_SETTLED_PNL_PER_MARKET)
    except Exception as e:
        print(f"  KXHIGH_settlements_clean (per-market) unavailable ({type(e).__name__}); skipping settled-P&L fallback")
        out["settled_pnl"] = pd.DataFrame()
    try:
        out["forecast_peak"] = _q(SQL_FORECAST_PEAK)
    except Exception:
        out["forecast_peak"] = pd.DataFrame()
    try:
        out["recent_fills"] = _q(SQL_RECENT_ACTIVITY)
    except Exception as e:
        print(f"  recent activity load failed: {e}", flush=True)
        out["recent_fills"] = pd.DataFrame()
    try:
        out["hourly_fcst_drift"] = _q(SQL_HOURLY_FCST_DRIFT)
    except Exception:
        out["hourly_fcst_drift"] = pd.DataFrame()
    try:
        out["nws_fcst_full"] = _q(SQL_NWS_HOURLY_FCST_FULL)
    except Exception as e:
        print(f"  KXHIGH_nws_hourly_forecast (trajectory) unavailable ({type(e).__name__}); skipping per-city trajectory chart")
        out["nws_fcst_full"] = pd.DataFrame()
    try:
        out["runs_raw"] = _q(SQL_RUNS_RAW_28H)
    except Exception:
        out["runs_raw"] = pd.DataFrame()
    return out


# ──────────────────────────── shaping ───────────────────────────────
def _city_key_for_ticker(t: str) -> str | None:
    if not isinstance(t, str) or not t.startswith("KXHIGH"):
        return None
    rest = t[len("KXHIGH"):]
    for k in sorted(CITIES.keys(), key=len, reverse=True):
        if rest.startswith(k + "-"):
            return k
    return None


def _cli_is_preliminary(high_time_str: object, high_f: object, low_f: object) -> bool:
    """Detect a stale-partial CLI report.

    Some WFOs issue a CLI overnight before the day's actual high happens.
    Such reports show a "high" time in the early-morning hours and a tiny
    diurnal range (because no daytime data is in yet). Treating them as
    final causes the dashboard to display e.g. "high 49°" when the day's
    real peak will be 70+ in the afternoon. The heuristic combines two
    signals — pre-dawn high_time AND tiny diurnal range — because either
    alone is fine on its own (frontal-passage days legitimately have
    pre-dawn highs with normal diurnal range, and small ranges happen on
    overcast days that don't warm up).
    """
    try:
        h = float(high_f) if high_f is not None and not pd.isna(high_f) else None
        l = float(low_f) if low_f is not None and not pd.isna(low_f) else None
    except (TypeError, ValueError):
        return False
    if h is None or l is None:
        return False  # missing data — defer to upstream defaults, treat as final
    if (h - l) >= 5:
        return False  # normal/large diurnal range — looks like a complete day
    # Range is small (<5°F). Now check the high_time.
    if not isinstance(high_time_str, str):
        return False
    s = high_time_str.strip().upper()
    if not (s.endswith(" AM") or s.endswith(" PM")):
        return False
    is_am = s.endswith(" AM")
    digits = s[:-3].strip()
    if not digits.isdigit() or len(digits) < 3 or len(digits) > 4:
        return False
    try:
        hour = int(digits[:-2])  # strip last 2 chars (minutes)
    except ValueError:
        return False
    # Pre-dawn = AM hours <= 5 (i.e., 12:00 AM through 5:59 AM).
    return is_am and hour <= 5


def _bucket_label(low: float | None, high: float | None) -> str:
    """Mirror the trading-bot bucket convention.

    T-markets (threshold) come through with low=0 and high=N.5 → "≤N".
    B-markets (between) come through with low=N-1.5, high=N+0.5 → "N-N+2".
    """
    try:
        lo = float(low) if low is not None else None
        hi = float(high) if high is not None else None
    except (TypeError, ValueError):
        return "?"
    if lo is None and hi is None:
        return "?"
    if lo is None or lo == 0.0:
        return f"≤{int(hi)}" if hi is not None else "?"
    if hi is None or hi >= 999:
        return f"≥{int(lo)+1}"
    return f"{int(lo)+1}-{int(hi)}"


def per_city_view(
    snapshot: pd.DataFrame,
    orderbook: pd.DataFrame,
    positions: pd.DataFrame | None = None,
    peak_obs: pd.DataFrame | None = None,
    market_arm: pd.DataFrame | None = None,
    orders_detail: pd.DataFrame | None = None,
    cli: pd.DataFrame | None = None,
    settled_pnl: pd.DataFrame | None = None,
) -> list[dict]:
    """Collapse per-market rows into per-(city, forecast_date) cards.

    `positions` is the latest row per market from `KXHIGH_positions_log`.
    When present it supersedes the trading-bot's snapshot.position field
    (which is unsigned and stale between trading runs); the live positions
    log is signed (negative = NO inventory) and refreshed every cron tick.
    """
    if snapshot.empty:
        return []

    snap = snapshot.copy()
    snap["city_key"] = snap["market_ticker"].map(_city_key_for_ticker)
    snap["bucket_label"] = [
        _bucket_label(lo, hi) for lo, hi in zip(snap["low_range"], snap["high_range"])
    ]

    # Layer live positions onto the snapshot so per-bucket chips and the
    # top-strip exposure read from Kalshi rather than the once-per-trading-run
    # snapshot.position. Rename the snapshot's `position` column out of the
    # way to avoid the same merge-conflict trap that bit us with the
    # bid/offer columns.
    if "position" in snap.columns:
        snap = snap.rename(columns={"position": "snap_position"})
    if positions is not None and not positions.empty:
        pos = positions[[
            "market_ticker", "position",
            "market_exposure_dollars", "realized_pnl_dollars",
            "total_traded_dollars", "fees_paid_dollars",
            "resting_orders_count",
        ]].copy()
        snap = snap.merge(pos, on="market_ticker", how="left")
    else:
        snap["position"] = snap.get("snap_position")  # fallback — unsigned
        for col in ("market_exposure_dollars", "realized_pnl_dollars",
                    "total_traded_dollars", "fees_paid_dollars",
                    "resting_orders_count"):
            snap[col] = None

    # Settlements fallback: once Kalshi processes settlement (~hours after
    # market close), the position drops out of /portfolio and so the
    # `realized_pnl_dollars` field in positions_log goes null. The
    # canonical realized P&L lives in settlements_clean. Merge it in so
    # yesterday's archive (rendered the day after) reflects realized P&L
    # for closed markets instead of $0.
    if settled_pnl is not None and not settled_pnl.empty:
        sp = settled_pnl[[
            "market_ticker", "settled_pnl_dollars", "settled_cost_dollars",
            "settled_fees_dollars", "settled_payout_dollars", "settled_result",
            "settled_net_position",
        ]].copy()
        snap = snap.merge(sp, on="market_ticker", how="left")
    else:
        for col in ("settled_pnl_dollars", "settled_cost_dollars",
                    "settled_fees_dollars", "settled_payout_dollars",
                    "settled_result", "settled_net_position"):
            snap[col] = None

    # Both tables carry no_highest_bid / no_lowest_offer / yes_*; rename the
    # orderbook side to ob_* so the merge produces unambiguous columns and we
    # can prefer the fresher orderbook prices over the snapshot's run-time copy.
    ob = orderbook.copy() if not orderbook.empty else pd.DataFrame()
    if not ob.empty:
        ob = ob[["market_ticker", "no_highest_bid", "no_lowest_offer",
                 "yes_highest_bid", "yes_lowest_offer", "polled_at", "market_status"]].rename(
            columns={
                "no_highest_bid": "ob_no_bid",
                "no_lowest_offer": "ob_no_offer",
                "yes_highest_bid": "ob_yes_bid",
                "yes_lowest_offer": "ob_yes_offer",
            }
        )
        snap = snap.merge(ob, on="market_ticker", how="left")
    else:
        for col in ("ob_no_bid", "ob_no_offer", "ob_yes_bid", "ob_yes_offer",
                    "polled_at", "market_status"):
            snap[col] = None

    def implied_yes(row) -> float | None:
        # Prefer orderbook (fresher, every 15min); fall back to snapshot copy.
        bid = row.get("ob_yes_bid"); offer = row.get("ob_yes_offer")
        if pd.isna(bid) and pd.isna(offer):
            # Fall back: snapshot may have no_highest_bid/no_lowest_offer (NO side);
            # convert to YES via 100 − NO.
            no_bid = row.get("no_highest_bid"); no_offer = row.get("no_lowest_offer")
            if pd.notna(no_bid) and pd.notna(no_offer):
                return 1.0 - (float(no_bid) + float(no_offer)) / 200.0
            if pd.notna(no_offer):
                return 1.0 - float(no_offer) / 100.0
            if pd.notna(no_bid):
                return 1.0 - float(no_bid) / 100.0
            return None
        if pd.notna(bid) and pd.notna(offer):
            return (float(bid) + float(offer)) / 200.0
        if pd.notna(bid):
            return float(bid) / 100.0
        return float(offer) / 100.0

    snap["market_yes_p"] = snap.apply(implied_yes, axis=1)
    snap["model_yes_p"] = snap["yes_probability"].astype(float)
    snap["edge"] = snap["model_yes_p"] - snap["market_yes_p"]

    # Per-market top open NO bid — the bot's most-aggressive resting order
    # not yet filled or expired. Card bars render a tick at the implied
    # YES price (100 − no_price) so the user can see where their bid sits
    # vs the current market mid + model. Picks max no_price among orders
    # with filled=0 and expiration_ts > now.
    open_bid_by_ticker: dict[str, int] = {}
    if orders_detail is not None and not orders_detail.empty:
        now_unix = datetime.now(timezone.utc).timestamp()
        for _, r in orders_detail.iterrows():
            filled = float(r.get("filled_count") or 0)
            if filled > 0:
                continue
            exp_ts = int(r.get("expiration_ts") or 0)
            if exp_ts and exp_ts <= now_unix:
                continue
            try:
                no_price = int(r.get("no_price") or 0)
            except (TypeError, ValueError):
                continue
            ticker = r.get("market_ticker")
            if not ticker:
                continue
            cur = open_bid_by_ticker.get(ticker)
            if cur is None or no_price > cur:
                open_bid_by_ticker[ticker] = no_price

    # A/B arm per market_ticker. The bot's sticky assignment means a market
    # belongs to one arm — we attach it to each bucket so the dashboard can
    # break P&L down by control vs treatment.
    arm_by_ticker: dict[str, str] = {}
    if market_arm is not None and not market_arm.empty:
        for _, r in market_arm.iterrows():
            if pd.notna(r.get("ab_arm")):
                arm_by_ticker[r["market_ticker"]] = r["ab_arm"]

    # Peak per (city, LOCAL date). Kalshi settles each market on the
    # local 24h window of the station — e.g., LAX's "April 30" market
    # covers 00:00-23:59 PT April 30 (which spans 03:00 UTC April 30
    # → 03:00 UTC May 1). Aggregating obs by ET-day misattributes 3-7h
    # of every Pacific/Mountain city's data to the wrong forecast_date.
    # We group raw obs by each city's local tz (from CITIES[k]["tz"])
    # and key the result by (city_abv, local_date_str) so the per-card
    # lookup matches the card's forecast_date directly.
    peak_by_city: dict[tuple[str, str], dict] = {}
    if peak_obs is not None and not peak_obs.empty:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            ZoneInfo = None  # type: ignore
        po = peak_obs.copy()
        po["observation_time"] = pd.to_datetime(po["observation_time"], utc=True)
        for city_abv, grp in po.groupby("city_abv"):
            tz_name = CITIES.get(city_abv, {}).get("tz", "America/New_York")
            try:
                tz = ZoneInfo(tz_name) if ZoneInfo else None
            except Exception:
                tz = None
            if tz is None:
                # Fallback: use ET (legacy behavior). Better than crashing.
                local_dates = grp["observation_time"].dt.tz_convert("America/New_York").dt.date
            else:
                local_dates = grp["observation_time"].dt.tz_convert(tz).dt.date
            for local_date, sub in grp.groupby(local_dates):
                peak_by_city[(city_abv, str(local_date))] = {
                    "peak": float(sub["temperature_f"].max()),
                    "latest_obs_time": str(sub["observation_time"].max()),
                }

    # CLI override: once the WFO publishes the daily climate report, that's
    # the value Kalshi settles on. Beats peak_obs because CLI uses 1-min
    # sensor data + SPECIs (typically ~1°F higher than the 5-min METAR max
    # we get from /observations). Keyed by (city_abv, event_date_str) so
    # both today's and yesterday's cards can pick up their own CLI rows
    # if both are still on the dashboard. Skips preliminary reports per
    # the heuristic in `_cli_is_preliminary`.
    cli_by_city: dict[tuple[str, str], dict] = {}
    if cli is not None and not cli.empty:
        for _, r in cli.iterrows():
            if _cli_is_preliminary(r.get("high_time"), r.get("high_temp_f"), r.get("low_temp_f")):
                continue
            high_f = r.get("high_temp_f")
            if high_f is None or pd.isna(high_f):
                continue
            cli_by_city[(r["city_abv"], str(r["event_date"]))] = {
                "high": float(high_f),
                "high_time": r.get("high_time") if pd.notna(r.get("high_time")) else None,
                "fetched_at": str(r["fetched_at"]) if pd.notna(r.get("fetched_at")) else None,
            }

    cards = []
    for (city_key, forecast_date), grp in snap.groupby(["city_key", "forecast_date"]):
        if city_key is None or city_key not in CITIES:
            continue
        meta = CITIES[city_key]
        grp_sorted = grp.sort_values("low_range", na_position="first")

        buckets = []
        for _, r in grp_sorted.iterrows():
            pos_val = r.get("position")
            # Realized P&L source priority:
            #   1. positions_log.realized_pnl_dollars (live, while still in
            #      portfolio — usually $0 because positions clear at close)
            #   2. settlements_clean.pnl_dollars (canonical post-settlement
            #      value — covers closed markets that have been settled)
            # Same for cost_basis: prefer live exposure for open positions,
            # fall back to settlement's total_cost for closed ones so the
            # archive page shows real cost+P&L on yesterday's settled markets.
            live_realized = r.get("realized_pnl_dollars")
            settled_pnl_val = r.get("settled_pnl_dollars")
            realized_pnl = (
                float(live_realized) if pd.notna(live_realized) and float(live_realized) != 0
                else float(settled_pnl_val) if pd.notna(settled_pnl_val)
                else None
            )
            live_exposure = r.get("market_exposure_dollars")
            settled_cost = r.get("settled_cost_dollars")
            exposure_dollars = (
                float(live_exposure) if pd.notna(live_exposure)
                else float(settled_cost) if pd.notna(settled_cost)
                else None
            )
            live_fees = r.get("fees_paid_dollars")
            settled_fees = r.get("settled_fees_dollars")
            fees_paid_dollars = (
                float(live_fees) if pd.notna(live_fees)
                else float(settled_fees) if pd.notna(settled_fees)
                else None
            )
            settled_result_val = r.get("settled_result")
            buckets.append({
                "label": r["bucket_label"],
                "ticker": r["market_ticker"],
                # Plumb actual numeric range (not label-parsed) so the
                # impossible-bucket alert and any future bucket-geometry
                # logic compares against Kalshi's true settlement
                # boundaries. Stored as `lo=N-1.5, hi=N+0.5` for B-markets
                # and `lo=0, hi=N.5` for T-markets — labels round these
                # to integers for display, which loses 0.5°F of precision.
                "low_range": float(r["low_range"]) if pd.notna(r.get("low_range")) else None,
                "high_range": float(r["high_range"]) if pd.notna(r.get("high_range")) else None,
                "model_p": float(r["model_yes_p"]) if pd.notna(r["model_yes_p"]) else None,
                "market_p": float(r["market_yes_p"]) if pd.notna(r["market_yes_p"]) else None,
                "edge": float(r["edge"]) if pd.notna(r["edge"]) else None,
                "yes_bid": int(r["ob_yes_bid"]) if pd.notna(r.get("ob_yes_bid")) else None,
                "yes_offer": int(r["ob_yes_offer"]) if pd.notna(r.get("ob_yes_offer")) else None,
                "position": int(pos_val) if pd.notna(pos_val) else 0,
                "exposure_dollars": exposure_dollars,
                "realized_pnl": realized_pnl,
                "total_traded_dollars": float(r["total_traded_dollars"]) if pd.notna(r.get("total_traded_dollars")) else None,
                "fees_paid_dollars": fees_paid_dollars,
                "resting_orders": int(r["resting_orders_count"]) if pd.notna(r.get("resting_orders_count")) else 0,
                "arm": arm_by_ticker.get(r["market_ticker"]),
                "open_no_bid": open_bid_by_ticker.get(r["market_ticker"]),
                # `settled_result` ('YES'/'NO') is set once Kalshi settles —
                # downstream renderers can show a "settled" tag in archive views.
                "settled_result": str(settled_result_val) if pd.notna(settled_result_val) else None,
            })

        # best |edge| with two-sided market (otherwise meaningless)
        cardable = [b for b in buckets if b["edge"] is not None and b["market_p"] is not None]
        best_edge = max(cardable, key=lambda b: abs(b["edge"]), default=None)

        head = grp_sorted.iloc[0]
        # `positions` drives cost/P&L/per-arm aggregations. Include both:
        #   - buckets with a LIVE position (open inventory in Kalshi portfolio)
        #   - buckets that have SETTLED (closed positions, no longer in
        #     portfolio but their cost+P&L should still tally toward the
        #     day's totals on yesterday's archive view).
        positions = [b for b in buckets if b["position"] or b.get("settled_result")]

        # Mark-to-market current value per bucket.
        #
        # Kalshi's `market_exposure_dollars` is the cost basis at entry, NOT
        # the live value (it equals total_traded for entry-only positions).
        # So we recompute current value from the orderbook mid:
        #   NO position (pos < 0): current_value = |pos| × (1 − yes_mid)
        #   YES position (pos > 0): current_value =  pos  ×  yes_mid
        #
        # Open P&L = current_value − cost_basis − fees, the dollar amount
        # we'd realize if we closed the remaining contracts at the mid right
        # now. Real-time as the orderbook moves.
        for b in buckets:
            pos = b["position"]
            cost = b.get("exposure_dollars")  # this is Kalshi's cost-basis field
            fees = b.get("fees_paid_dollars") or 0
            yes_mid = b.get("market_p")
            if pos and cost is not None and yes_mid is not None:
                side_mid = (1.0 - yes_mid) if pos < 0 else yes_mid
                current_value = abs(pos) * side_mid
                b["current_value"] = current_value
                b["open_pnl"] = current_value - cost - fees
            else:
                b["current_value"] = None
                b["open_pnl"] = None

        cost_basis = sum(b.get("exposure_dollars") or 0 for b in positions)
        fees_paid = sum(b.get("fees_paid_dollars") or 0 for b in positions)
        realized_pnl_today = sum(b["realized_pnl"] or 0 for b in buckets)
        exposure = sum(b.get("current_value") or 0 for b in positions)  # live now
        open_pnl = sum(b.get("open_pnl") or 0 for b in positions)
        live_pnl = open_pnl + realized_pnl_today

        # Per-arm P&L breakdown (control vs treatment): the trading bot
        # assigns each market to one arm; we aggregate live P&L over the
        # held buckets in each arm. Buckets without an arm tag (legacy
        # orders pre-A/B-test) collapse into "(unset)".
        # Tracks: cost (Kalshi market_exposure_dollars = $ paid), now
        # (current_value mark-to-market), open_pnl, realized, n.
        arm_pnl: dict[str, dict] = {}
        for b in positions:
            arm = b.get("arm") or "(unset)"
            slot = arm_pnl.setdefault(arm, {"open_pnl": 0.0, "realized": 0.0, "cost": 0.0, "now": 0.0, "n": 0})
            slot["open_pnl"] += b.get("open_pnl") or 0
            slot["realized"] += b.get("realized_pnl") or 0
            slot["cost"] += b.get("exposure_dollars") or 0
            slot["now"] += b.get("current_value") or 0
            slot["n"] += 1

        # Resolve "high so far" with priority: CLI (Kalshi's settlement
        # source) → live obs max → snapshot peak_temp_f. CLI beats live obs
        # by ~1°F when a brief intra-METAR-cycle peak is in the daily-high
        # 1-min sensor record but not in the 5-min /observations stream.
        # Both lookups are keyed by (city, forecast_date) so they match
        # the card's local-day exactly — see peak_by_city build above.
        _cli_entry = cli_by_city.get((city_key, str(forecast_date)))
        _live_entry = peak_by_city.get((city_key, str(forecast_date)), {})
        _snap_peak = float(head["peak_temp_f"]) if pd.notna(head.get("peak_temp_f")) else None
        if _cli_entry is not None:
            _peak_resolution = {
                "temp": _cli_entry["high"],
                "source": "cli",
                "label": _cli_entry.get("high_time"),  # e.g. "1052 AM"
                "latest_obs_time": _cli_entry.get("fetched_at"),
            }
        elif _live_entry.get("peak") is not None:
            _peak_resolution = {
                "temp": _live_entry["peak"],
                "source": "live",
                "label": None,
                "latest_obs_time": _live_entry.get("latest_obs_time"),
            }
        elif _snap_peak is not None:
            _peak_resolution = {
                "temp": _snap_peak, "source": "snapshot",
                "label": None, "latest_obs_time": None,
            }
        else:
            _peak_resolution = {
                "temp": None, "source": None,
                "label": None, "latest_obs_time": None,
            }

        cards.append({
            "city_key": city_key,
            "city_name": meta["city"],
            "icao": meta["icao"],
            "forecast_date": str(forecast_date),
            "forecast_avg": float(head["forecast_avg"]) if pd.notna(head["forecast_avg"]) else None,
            "forecast_std": float(head["forecast_std"]) if pd.notna(head["forecast_std"]) else None,
            "nws": float(head["nws"]) if pd.notna(head["nws"]) else None,
            "accu": float(head["accuweather"]) if pd.notna(head["accuweather"]) else None,
            "wu": float(head["weather_underground"]) if pd.notna(head["weather_underground"]) else None,
            "nws_short": head.get("nws_short_conditions") if pd.notna(head.get("nws_short_conditions")) else None,
            "observed_temp": float(head["observed_temp_f"]) if pd.notna(head.get("observed_temp_f")) else None,
            "peak_temp": _peak_resolution["temp"],
            "peak_temp_source": _peak_resolution["source"],
            "peak_temp_label": _peak_resolution["label"],
            "latest_obs_time": _peak_resolution["latest_obs_time"],
            "buckets": buckets,
            "best_edge": best_edge,
            "positions": positions,
            "exposure": exposure,
            "cost_basis": cost_basis,
            "fees_paid": fees_paid,
            "realized_pnl_today": realized_pnl_today,
            "open_pnl": open_pnl,
            "live_pnl": live_pnl,
            "arm_pnl": arm_pnl,
            "polled_at": str(head.get("polled_at")) if pd.notna(head.get("polled_at")) else None,
            "run_date": str(head["run_date"]),
        })

    # Show only today/tomorrow ET cards — drop past dates entirely, even
    # when residual positions remain on yesterday's market_tickers.
    # (Those positions still flow through the BQ data but won't render
    # as their own card; if they materially affect aggregated totals the
    # top strip will still pick them up via the positions_log read.)
    try:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        today_et = datetime.now(timezone.utc).date()
    cards = [
        c for c in cards
        if pd.Timestamp(c["forecast_date"]).date() >= today_et
    ]

    # Sort: positioned cities first by live P&L descending (biggest winners
    # at top, biggest losers at bottom of the positioned block); flat cities
    # follow, ordered by event_date ascending then alphabetically so today's
    # cards lead and tomorrow's trail.
    cards.sort(key=lambda c: (
        0 if c["positions"] else 1,
        -c["live_pnl"] if c["positions"] else 0,
        c["forecast_date"] if not c["positions"] else "",
        -abs(c["best_edge"]["edge"]) if c["best_edge"] else 0,
        c["city_name"],
    ))
    return cards


def forecast_sparklines(history: pd.DataFrame) -> dict[str, list[dict]]:
    """Per-city series of (run_date, forecast_avg). Used as a fallback for
    cards where we don't yet have enough NWS hourly obs to draw an intraday
    temperature curve."""
    out: dict[str, list[dict]] = {}
    if history.empty:
        return out
    h = history.copy()
    h["run_iso"] = h["run_date"].astype(str)
    for city, g in h.groupby("city"):
        g_sorted = g.sort_values("run_date")
        out[city] = [
            {"t": r["run_iso"], "v": float(r["forecast_avg"])}
            for _, r in g_sorted.iterrows()
            if pd.notna(r["forecast_avg"])
        ]
    return out


def _to_et_label(ts: object) -> str | None:
    """Convert a UTC timestamp to an `YYYY-MM-DD HH:MM` ET string used as a
    sparkline x-axis label. Sparklines need the same string for an obs and
    a forecast at the same hour so the merge keys line up — that's why
    we render both series through this single function."""
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return None
    if pd.isna(t):
        return None
    if t.tz is None:
        t = t.tz_localize("UTC")
    try:
        from zoneinfo import ZoneInfo
        return t.tz_convert(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        import pytz
        return t.tz_convert(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M")


def _to_et_short(ts: object) -> str | None:
    """ET HH:MM short label — used for the "added at" timestamp shown in
    sparkline tooltips so the user can see when a datapoint landed in BQ
    independent of the obs/forecast hour itself."""
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return None
    if pd.isna(t):
        return None
    if t.tz is None:
        t = t.tz_localize("UTC")
    try:
        from zoneinfo import ZoneInfo
        return t.tz_convert(ZoneInfo("America/New_York")).strftime("%I:%M%p ET").lstrip("0")
    except Exception:
        import pytz
        return t.tz_convert(pytz.timezone("America/New_York")).strftime("%I:%M%p ET").lstrip("0")


def fcst_sparklines(nws_fcst: pd.DataFrame) -> dict[str, list[dict]]:
    """Per-city series of (period_start_ET, temperature_f, polled_at_ET) —
    NWS hourly forecast. polled_at is the time we wrote this forecast row
    to BQ (i.e., when our cron poll captured it); shown in tooltips so the
    user can tell apart "forecast for 3pm today" from "forecast we *got*
    at 11:23am today".
    """
    out: dict[str, list[dict]] = {}
    if nws_fcst is None or nws_fcst.empty:
        return out
    for city_abv, g in nws_fcst.groupby("city_abv"):
        g_sorted = g.sort_values("period_start")
        pts = []
        for _, r in g_sorted.iterrows():
            if pd.isna(r["temperature_f"]):
                continue
            label = _to_et_label(r["period_start"])
            if label is None:
                continue
            pts.append({
                "t": label,
                "v": float(r["temperature_f"]),
                "added": _to_et_short(r.get("polled_at")),
            })
        if pts:
            out[city_abv] = pts
    return out


def obs_sparklines(nws_obs: pd.DataFrame) -> dict[tuple[str, str], list[dict]]:
    """Per-(city_abv, local_date_str) series of (obs_time_ET, temp_f, polled_at_ET).

    Each card on the dashboard renders a sparkline for ITS forecast_date,
    not "the last 12h rolling". Yesterday's archive shows yesterday's
    full local day in green; today's live card shows today-so-far. The
    grouping uses each city's tz (from CITIES[k]["tz"]) so Pacific cards'
    local-day boundaries align with the station's clock — same convention
    Kalshi uses for settlement.

    Obs are downsampled to one point per HOUR (max temp in that hour) so
    the green line cadence matches the blue forecast line's hourly
    cadence. Without this, raw 5-min obs (~96 points/day) outweighed
    forecast hours (~12 points) on the categorical x-axis, eating ~89%
    of the chart width and squeezing the forecast curve into the right
    sliver. Hourly-max preserves the day's peak trajectory, which is
    what matters for tracking "high so far".

    Cards with fewer than 2 obs points fall back to the forecast sparkline.
    """
    out: dict[tuple[str, str], list[dict]] = {}
    if nws_obs is None or nws_obs.empty:
        return out
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None  # type: ignore
    nws_obs = nws_obs.copy()
    nws_obs["observation_time"] = pd.to_datetime(nws_obs["observation_time"], utc=True)
    for city_abv, g in nws_obs.groupby("city_abv"):
        tz_name = CITIES.get(city_abv, {}).get("tz", "America/New_York")
        try:
            tz = ZoneInfo(tz_name) if ZoneInfo else None
        except Exception:
            tz = None
        if tz is None:
            local_times = g["observation_time"].dt.tz_convert("America/New_York")
        else:
            local_times = g["observation_time"].dt.tz_convert(tz)
        # `hour_bucket`: floor each obs to its containing hour in local
        # time. Two obs at 14:05 and 14:55 land in the same 14:00 bucket.
        g = g.assign(
            local_date=local_times.dt.date,
            hour_bucket=local_times.dt.floor("h"),
        ).sort_values("observation_time")
        for local_date, day_obs in g.groupby("local_date"):
            # Pick the hottest reading per hour. For each hour-bucket,
            # also keep the obs_time and polled_at of THAT max reading
            # so the tooltip remains accurate ("75°F at 13:25 ET, added
            # at 13:30 ET").
            day_obs_clean = day_obs[day_obs["temperature_f"].notna()]
            if day_obs_clean.empty:
                continue
            idx = day_obs_clean.groupby("hour_bucket")["temperature_f"].idxmax()
            hourly_peaks = day_obs_clean.loc[idx].sort_values("hour_bucket")
            pts = []
            for _, r in hourly_peaks.iterrows():
                label = _to_et_label(r["observation_time"])
                if label is None:
                    continue
                pts.append({
                    "t": label,
                    "v": float(r["temperature_f"]),
                    "added": _to_et_short(r.get("polled_at")),
                })
            if pts:
                out[(city_abv, str(local_date))] = pts
    return out


# ───────────────────────── alert signals ────────────────────────────
def compute_alerts(cards: list[dict], forecast_history: pd.DataFrame,
                   forecast_peak: pd.DataFrame | None = None) -> list[dict]:
    """Derive bucket-distance + source-agreement alerts from the latest snapshot.

    Three alert flavors:

      1. **High source disagreement** — std([NWS, AccuWeather, WeatherUnderground])
         > 2.0 °F. Indicates the model's bucket distribution is wider than
         our forecast_std assumes, so we may be under-pricing tails.

      2. **Top model bucket near forecast** — for the model's argmax bucket,
         |bucket_center − forecast_avg| / forecast_std < 0.3. Cluster of
         buckets fight for ~equal probability, so a small forecast revision
         can flip the winner — markets here are noisy to handicap.

      3. **Top model bucket shifted vs prior run** — comparing the latest two
         runs for this city/forecast_date, the argmax-bucket label changed.

    Returns a list of dicts {severity, when, city, what} sorted newest-first.
    """
    alerts: list[dict] = []
    # Per-city lookup for the NWS hourly-grid peak temperature today,
    # used by alert (5) to flag NWS daily-vs-hourly disagreement. Source:
    # SQL_FORECAST_PEAK aggregates max temp from KXHIGH_nws_hourly_forecast.
    hourly_peak_by_city: dict[str, float] = {}
    if forecast_peak is not None and not forecast_peak.empty:
        for _, r in forecast_peak.iterrows():
            try:
                if pd.notna(r.get("peak_temp")):
                    hourly_peak_by_city[r["city_abv"]] = float(r["peak_temp"])
            except (TypeError, ValueError):
                pass

    # Build city → (forecast_date → ordered runs) of (run_date, argmax_label).
    # Argmax label requires snapshot rows but we have it via cards' buckets for
    # the *latest* run. For the prior-run comparison we approximate using the
    # forecast_avg history: when forecast_avg moves enough that a different
    # bucket would now be argmax, we count that as a shift. Cheaper than
    # re-querying full snapshot history.
    fh = forecast_history.copy() if forecast_history is not None else pd.DataFrame()
    history_by_city: dict[str, pd.DataFrame] = {}
    if not fh.empty:
        for city_name, g in fh.groupby("city"):
            history_by_city[city_name] = g.sort_values("run_date")

    for c in cards:
        city = c["city_key"]
        # Each alert carries `when` = the ET-formatted time-of-day of the
        # most relevant data source for that signal (orderbook poll for
        # market-price-driven alerts, snapshot run_date for forecast-driven
        # ones). Rendered in parens after the message so the user can
        # gauge staleness at a glance.
        ob_when = _fmt_et(c.get("polled_at"), fmt="%I:%M%p ET").lstrip("0")
        snap_when = _fmt_et(c.get("run_date"), fmt="%I:%M%p ET").lstrip("0")

        # ── (0) impossible buckets still priced ──────────────────────
        # Daily highs only go up. A bucket is mathematically dead (0% YES)
        # only when the current peak EXCEEDS its actual `high_range`
        # (Kalshi's settlement upper bound — typically N.5°F for B-markets
        # like "53-54" → high_range=54.5; T-markets "≤64" → high_range=64.5).
        # Earlier this was parsing the integer from the LABEL (54, 64),
        # which fired false alarms when the peak rounded to that integer
        # but was still below the real .5 cutoff (e.g., AUS ≤64 with peak
        # 64.04 isn't dead — it pays YES if final high ≤ 64.5).
        peak = c.get("peak_temp")
        if peak is not None:
            for b in c["buckets"]:
                hi = b.get("high_range")
                if hi is None or hi >= peak:
                    continue
                m_yes = b.get("market_p")
                if m_yes is None or m_yes < 0.05:
                    continue
                alerts.append({
                    "severity": "red",
                    "city": city,
                    "when": ob_when,
                    "what": (f"<b>{city}</b> {b['label']} priced YES {m_yes*100:.0f}% "
                             f"but high already {peak:.1f}° — bucket is dead "
                             f"(closes ≤{hi:.1f}°), market is stale"),
                    "rank": 100 + m_yes * 100,  # always near top
                })

        # ── (1) source agreement ─────────────────────────────────────
        sources = [v for v in (c.get("nws"), c.get("accu"), c.get("wu")) if v is not None]
        if len(sources) >= 2:
            mean = sum(sources) / len(sources)
            var = sum((x - mean) ** 2 for x in sources) / len(sources)
            std = var ** 0.5
            if std > 2.0:
                spread = max(sources) - min(sources)
                names = []
                if c.get("nws") is not None: names.append(f"NWS {c['nws']:.0f}")
                if c.get("accu") is not None: names.append(f"AW {c['accu']:.0f}")
                if c.get("wu") is not None: names.append(f"WU {c['wu']:.0f}")
                alerts.append({
                    "severity": "amber",
                    "city": city,
                    "when": snap_when,
                    "what": (f"<b>{city}</b> source disagreement σ={std:.1f}° "
                             f"(spread {spread:.0f}°): {' · '.join(names)}"),
                    "rank": std,  # used to keep most-extreme alerts on top
                })

        # ── (2) top model bucket near forecast ───────────────────────
        f_avg = c.get("forecast_avg"); f_std = c.get("forecast_std")
        cardable = [b for b in c["buckets"] if b["model_p"] is not None]
        if f_avg is not None and f_std and cardable:
            top = max(cardable, key=lambda b: b["model_p"])
            # bucket center: parse from low_range/high_range stored elsewhere?
            # Approximate from label "N-M" or "≤N" or "≥N+1".
            center = _bucket_center(top["label"])
            if center is not None:
                norm_dist = abs(center - f_avg) / max(f_std, 0.5)
                if norm_dist < 0.3:
                    alerts.append({
                        "severity": "amber",
                        "city": city,
                        "when": snap_when,
                        "what": (f"<b>{city}</b> top bucket {top['label']} "
                                 f"only {norm_dist:.2f}σ from forecast {f_avg:.1f}° — "
                                 f"argmax is fragile"),
                        "rank": -norm_dist,
                    })

        # ── (3) forecast-avg shift in last 24h ───────────────────────
        # Compare ONLY runs for the same forecast_date as the card. Earlier
        # the alert pulled the last 8 rows of (city, *) sorted by run_date,
        # which mixed yesterday's forecast (e.g., 87° for the 04-27 day-of)
        # with today's (69° for 04-28) and reported an 18° "drop" that was
        # just two different days, not the same forecast moving.
        hist = history_by_city.get(c["city_name"])
        if hist is not None and f_avg is not None:
            same_day = hist[hist["forecast_date"].astype(str) == c["forecast_date"]]
            if len(same_day) >= 2:
                recent = same_day.tail(8)
                prev = recent.iloc[0]["forecast_avg"]
                if pd.notna(prev) and abs(f_avg - float(prev)) >= 1.5:
                    direction = "↑" if f_avg > float(prev) else "↓"
                    alerts.append({
                        "severity": "green" if direction == "↑" else "red",
                        "city": city,
                        "when": snap_when,
                        "what": (f"<b>{city}</b> {c['forecast_date']} forecast "
                                 f"{direction} {float(prev):.0f}° → {f_avg:.0f}° "
                                 f"over last {len(recent)} runs"),
                        "rank": abs(f_avg - float(prev)),
                    })

        # ── (4) model vs market YES disagreement ─────────────────────
        # Card bars are model_p (blue) vs market_p (purple). When they
        # diverge by ≥20pp, that's either a trading opportunity or a
        # signal one of the two is wrong. Skip buckets already flagged
        # as "dead" by alert (0) — those are guaranteed disagreements
        # but the (0) alert already explains them. Cap each city to its
        # single largest disagreement so one outlier city can't crowd
        # out other cities in the top-8 list.
        # Direction interpretation (in Kalshi prediction-market terms,
        # contract price = implied probability × $1):
        #   model > market: market is UNDERPRICING YES → YES is cheap
        #     relative to fair → buy YES.
        #   model < market: market is OVERPRICING YES → YES is expensive,
        #     NO is correspondingly cheap → buy NO.
        DISAGREE_THRESHOLD_PP = 20  # percentage points
        candidates = []
        for b in c["buckets"]:
            mdl = b.get("model_p")
            mkt = b.get("market_p")
            if mdl is None or mkt is None:
                continue
            edge_pp = (mdl - mkt) * 100
            if abs(edge_pp) < DISAGREE_THRESHOLD_PP:
                continue
            # De-dup: the impossible-bucket alert already covers cases
            # where the bucket's high_range is below the day's peak.
            hi = b.get("high_range")
            if peak is not None and hi is not None and hi < peak and mkt >= 0.05:
                continue
            candidates.append((b, edge_pp))
        if candidates:
            # Pick the single biggest disagreement per city.
            b, edge_pp = max(candidates, key=lambda x: abs(x[1]))
            mdl_pct = b["model_p"] * 100
            mkt_pct = b["market_p"] * 100
            if edge_pp > 0:
                # Model thinks YES more likely than market — YES is cheap.
                direction = "buy YES"
                arrow = "↑"
            else:
                # Model thinks YES less likely than market — NO is cheap.
                direction = "buy NO"
                arrow = "↓"
            alerts.append({
                "severity": "amber",
                "city": city,
                "when": snap_when,
                "what": (f"<b>{city}</b> {b['label']} model YES {mdl_pct:.0f}% "
                         f"vs market YES {mkt_pct:.0f}% (Δ {edge_pp:+.0f}pp · {arrow} {direction})"),
                # Rank: bigger gap = higher up. Add small offset so these
                # tend to surface above the noise but below true bucket-
                # dead alerts (which use rank=100+).
                "rank": 50 + abs(edge_pp),
            })

        # ── (5) NWS daily-vs-hourly grid disagreement ────────────────
        # NWS publishes two forecasts: the human-curated daily ("today's
        # high near X°") via /forecast, and the auto-derived hourly grid
        # via /forecast/hourly. They don't auto-reconcile — forecasters
        # often update the daily without redoing the hourly grid. Gaps
        # ≥1.5°F suggest the WFO doesn't have a clean view of the day,
        # or one product is stale.
        nws_daily = c.get("nws")
        nws_hourly_peak = hourly_peak_by_city.get(city)
        if nws_daily is not None and nws_hourly_peak is not None:
            gap = abs(nws_daily - nws_hourly_peak)
            if gap >= 1.5:
                direction = "↓" if nws_hourly_peak < nws_daily else "↑"
                alerts.append({
                    "severity": "amber",
                    "city": city,
                    "when": snap_when,
                    "what": (f"<b>{city}</b> NWS daily forecast {nws_daily:.0f}° "
                             f"vs hourly grid peak {nws_hourly_peak:.0f}° "
                             f"(Δ {direction}{gap:.1f}°) — forecaster intuition "
                             f"and gridded model disagree"),
                    # Rank below disagreement alerts (50+) but above
                    # garden-variety source-disagreement alerts (~2-3).
                    "rank": 30 + gap,
                })

    alerts.sort(key=lambda a: -a["rank"])
    return alerts[:8]


def _label_sort_key(label: str) -> float:
    """Sort key for bucket labels — lower bound (or single value) of the
    bucket's temperature range. Open-ended buckets sort to ends.
    """
    s = label.strip()
    if not s or s == "?":
        return -999
    if s.startswith("≤"):
        try:
            return float(s[1:]) - 0.5  # sort just before the matching value
        except ValueError:
            return -999
    if s.startswith("≥"):
        try:
            return float(s[1:]) + 0.5  # sort just after
        except ValueError:
            return 999
    if "-" in s:
        try:
            lo, _ = s.split("-")
            return float(lo)
        except ValueError:
            return 0
    try:
        return float(s)
    except ValueError:
        return 0


def _bucket_upper_bound(label: str) -> float | None:
    """Highest temperature this bucket can win at. Used by the
    impossible-bucket alert to detect dead-but-still-priced markets."""
    s = label.strip()
    if not s or s == "?":
        return None
    if s.startswith("≤"):
        try:
            return float(s[1:])
        except ValueError:
            return None
    if s.startswith("≥"):
        return None  # open-ended high; never "impossible" by current peak
    if "-" in s:
        try:
            _, hi = s.split("-")
            return float(hi)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _bucket_center(label: str) -> float | None:
    """Best-effort numeric center for an alert calculation. Returns None for
    open-ended buckets (we can't compute distance for those)."""
    s = label.strip()
    if not s or s in ("?",):
        return None
    if s.startswith("≤") or s.startswith("≥"):
        return None  # open-ended; skip distance check
    if "-" in s:
        try:
            lo, hi = s.split("-")
            return (int(lo) + int(hi)) / 2.0
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


# ──────────────────────────── render ────────────────────────────────
TEMPLATE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="refresh" content="180" />
<title>KXHIGH Live</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0b1020; --panel:#131a2e; --panel-2:#182040; --line:#243056;
    --ink:#e6ecff; --ink-dim:#93a2c8; --green:#2ecc71; --red:#ff5b6e;
    --amber:#ffb94a; --blue:#4aa3ff; --violet:#b18cff; --chip:#1f2950;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);
    font:13px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,sans-serif}
  header.top{display:grid;grid-template-columns:1.6fr repeat(6,1fr);gap:1px;
    background:var(--line);border-bottom:1px solid var(--line)}
  header.top .cell{background:var(--panel);padding:10px 14px}
  header.top .label{color:var(--ink-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
  header.top .value{font-size:18px;font-weight:600;margin-top:2px}
  header.top .brand .value{font-size:16px;color:var(--blue)}
  header.top .brand .sub{color:var(--ink-dim);font-size:11px;margin-top:2px}
  .pos{color:var(--green)} .neg{color:var(--red)} .warn{color:var(--amber)} .muted{color:var(--ink-dim)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;vertical-align:1px;margin-right:6px}
  .dot.green{background:var(--green);box-shadow:0 0 8px var(--green)}
  .dot.amber{background:var(--amber);box-shadow:0 0 8px var(--amber)}
  .dot.red{background:var(--red);box-shadow:0 0 8px var(--red)}
  main{display:grid;grid-template-columns:1fr 360px;gap:16px;padding:16px}
  @media (max-width:1200px){main{grid-template-columns:1fr}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;
    position:relative;overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s}
  .card:hover{transform:translateY(-1px);border-color:#2f3d6f}
  .card .open-hint{position:absolute;top:6px;right:8px;font-size:10px;color:var(--ink-dim);
    opacity:0;transition:opacity .12s}
  .card:hover .open-hint{opacity:.7}
  .card .stripe{position:absolute;top:0;left:0;right:0;height:3px;background:var(--ink-dim)}
  .card.edge-pos .stripe{background:var(--green)}
  .card.edge-neg .stripe{background:var(--red)}
  .card.edge-flat .stripe{background:var(--ink-dim)}
  .card-head{display:flex;justify-content:space-between;align-items:baseline}
  .city{font-size:16px;font-weight:700;letter-spacing:.04em}
  .ticker{color:var(--ink-dim);font-size:11px}
  .row{display:flex;justify-content:space-between;gap:12px;margin-top:8px}
  .stat{font-size:11px;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em}
  .stat-val{font-size:14px;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
  .spark{height:72px;margin:8px -4px 4px}
  /* HTML tooltip for sparklines, positioned absolutely on document.body
     so it can extend outside the small canvas + the card's overflow:hidden
     without any clipping. Single shared element — only one tooltip is
     ever visible at a time. */
  #spark-tip{position:absolute;background:#0b1020;color:var(--ink);
    border:1px solid var(--line);border-radius:4px;padding:6px 9px;
    font-size:11px;line-height:1.4;pointer-events:none;
    transform:translate(-50%,-115%);white-space:nowrap;z-index:9999;
    display:none;font-variant-numeric:tabular-nums;box-shadow:0 4px 12px rgba(0,0,0,.4)}
  #spark-tip .tt-title{color:var(--ink-dim);font-size:10px;margin-bottom:3px}
  #spark-tip .tt-obs{color:var(--green)}
  #spark-tip .tt-fcst{color:var(--blue)}
  .buckets{margin-top:8px}
  .bucket-row{display:grid;grid-template-columns:60px 1fr 100px;gap:6px;align-items:center;
    margin-bottom:3px;padding:1px 4px;border-radius:3px;font-variant-numeric:tabular-nums;
    border-left:3px solid transparent}
  .bucket-row.held-no{background:rgba(255,91,110,0.10);border-left-color:var(--red)}
  .bucket-row.held-yes{background:rgba(46,204,113,0.10);border-left-color:var(--green)}
  .bucket-label{font-size:11px;color:var(--ink-dim);text-align:right}
  .bucket-row.held-no .bucket-label,.bucket-row.held-yes .bucket-label{color:var(--ink);font-weight:600}
  .bars{height:14px;background:var(--panel-2);border-radius:3px;position:relative;overflow:visible}
  .bar-mkt,.bar-mdl{position:absolute;left:0;top:0;height:50%}
  .bar-mkt{top:0;background:linear-gradient(90deg,#4aa3ff88,#4aa3ff)}
  .bar-mdl{top:50%;background:linear-gradient(90deg,#b18cff88,#b18cff)}
  /* Open-order tick: vertical line on the bar at the YES probability
     implied by the bot's top open NO bid (100 − no_price). Stretches
     slightly above and below the bar for visibility. */
  .order-tick{position:absolute;top:-3px;bottom:-3px;width:2px;
    background:var(--amber);z-index:2;pointer-events:none}
  .order-tick::after{content:attr(data-px);position:absolute;top:-12px;
    left:50%;transform:translateX(-50%);font-size:9px;color:var(--amber);
    white-space:nowrap;font-variant-numeric:tabular-nums}
  .bucket-edge{font-size:11px;text-align:right;display:flex;justify-content:flex-end;
    align-items:center;gap:6px}
  .bucket-edge .pos-tag{font-size:10px;color:var(--ink);background:var(--chip);
    padding:1px 5px;border-radius:3px;font-weight:600;letter-spacing:.02em}
  .bucket-edge .pos-tag.no{background:rgba(255,91,110,0.25)}
  .bucket-edge .pos-tag.yes{background:rgba(46,204,113,0.25)}
  .pos-chip{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
  .chip{background:var(--chip);color:var(--ink);padding:3px 6px;border-radius:4px;font-size:11px;
    font-variant-numeric:tabular-nums}
  .chip .b{color:var(--ink-dim);margin-right:4px}
  .chip .pnl-pos{color:var(--green)} .chip .pnl-neg{color:var(--red)}
  .legend{display:flex;gap:10px;margin-left:auto;font-size:10px;color:var(--ink-dim)}
  .legend i{display:inline-block;width:8px;height:8px;vertical-align:1px;margin-right:3px;border-radius:1px}
  .legend .mkt i{background:var(--blue)} .legend .mdl i{background:var(--violet)}
  aside{display:flex;flex-direction:column;gap:12px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
  .panel h3{margin:0 0 8px;font-size:12px;letter-spacing:.08em;color:var(--ink-dim);
    text-transform:uppercase;display:flex;justify-content:space-between;align-items:center}
  .panel h3 .count{color:var(--ink);background:var(--chip);padding:1px 6px;border-radius:10px;font-size:11px}
  .heat{display:grid;gap:2px;font-variant-numeric:tabular-nums;font-size:10px}
  .heat .h-h{color:var(--ink-dim);text-align:center;padding:4px 0}
  .heat .h-c{padding:4px 6px;color:var(--ink-dim)}
  .heat .h-cell{text-align:center;padding:4px 0;border-radius:3px;color:#0b1020;font-weight:600}
  .heat .h-empty{padding:4px 0}
  table.runs{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}
  table.runs td{padding:4px 6px;border-bottom:1px solid var(--line)}
  table.runs td:last-child{text-align:right;color:var(--ink-dim)}
  footer.note{color:var(--ink-dim);text-align:center;padding:16px;font-size:11px}
  /* alerts panel */
  .alert{display:flex;gap:8px;align-items:flex-start;padding:8px 0;
    border-bottom:1px solid var(--line);font-size:12px}
  .alert:last-child{border-bottom:none}
  .alert .what{flex:1}
  .alert .what b{color:var(--ink)}
  /* drill-in modal */
  .modal-bg{position:fixed;inset:0;background:rgba(8,12,28,.7);display:none;
    align-items:flex-start;justify-content:center;z-index:50;padding:24px;overflow-y:auto}
  .modal-bg.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    width:min(960px,100%);padding:20px;color:var(--ink)}
  .modal h2{margin:0 0 12px;font-size:18px;display:flex;justify-content:space-between;align-items:center}
  .modal h4{margin:18px 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-dim)}
  .modal .close{cursor:pointer;background:var(--chip);border:none;color:var(--ink);
    border-radius:4px;padding:4px 10px;font-size:12px}
  .modal .kv{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:8px}
  .modal .kv > div{background:var(--panel-2);padding:8px 10px;border-radius:5px}
  .modal .kv .k{font-size:10px;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em}
  .modal .kv .v{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
  .modal .chart-wrap{height:180px;position:relative;background:var(--panel-2);
    border-radius:5px;padding:8px}
  table.detail{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums;
    margin-top:4px}
  table.detail th{background:var(--panel-2);color:var(--ink-dim);text-transform:uppercase;
    font-size:10px;letter-spacing:.05em;padding:6px;text-align:left}
  table.detail td{padding:5px 6px;border-bottom:1px solid var(--line)}
  table.detail tr:last-child td{border-bottom:none}
  .hist-btn{background:var(--chip);border:1px solid var(--line);color:var(--ink);
    padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;margin-left:8px}
  .hist-btn:hover{border-color:var(--blue);color:var(--blue)}
</style>
</head>
<body>
"""


_ET = None


def _fmt_et(ts: object, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a UTC timestamp/string as US/Eastern.

    BigQuery returns timezone-aware UTC timestamps; the modal display has
    been showing the raw UTC clock without saying so, which is misleading
    for a US trading dashboard. We center on ET because it matches Kalshi's
    default UI and the bot's market-cutoff conventions.
    """
    global _ET
    if _ET is None:
        try:
            from zoneinfo import ZoneInfo
            _ET = ZoneInfo("America/New_York")
        except Exception:
            import pytz
            _ET = pytz.timezone("America/New_York")
    if ts is None:
        return "—"
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return str(ts)[:19]
    if pd.isna(t):
        return "—"
    if t.tz is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(_ET).strftime(fmt)


def _fmt_age(ts_str: str | None, now_utc: datetime) -> tuple[str, str]:
    """Return (humanized_age, freshness_class) for a UTC ISO timestamp."""
    if not ts_str:
        return "—", "amber"
    try:
        ts = pd.Timestamp(ts_str)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
    except Exception:
        return "—", "amber"
    age_s = max(0.0, (now_utc - ts.to_pydatetime()).total_seconds())
    if age_s < 60:
        label = f"{int(age_s)}s ago"
    elif age_s < 3600:
        label = f"{int(age_s // 60)}m ago"
    elif age_s < 86400:
        label = f"{int(age_s // 3600)}h ago"
    else:
        label = f"{int(age_s // 86400)}d ago"
    cls = "green" if age_s < 60 * 30 else "amber" if age_s < 3600 * 4 else "red"
    return label, cls


def render(data: dict) -> str:
    now_utc = datetime.now(timezone.utc)
    cards = per_city_view(
        data["snapshot"], data["orderbook"],
        data.get("positions"), data.get("peak_obs"),
        data.get("market_arm"),
        data.get("orders_detail"),
        cli=data.get("cli"),
        settled_pnl=data.get("settled_pnl"),
    )
    forecast_spark = forecast_sparklines(data["forecast_history"])
    obs_spark = obs_sparklines(data.get("nws_obs", pd.DataFrame()))
    fcst_spark = fcst_sparklines(data.get("nws_fcst", pd.DataFrame()))

    # ── top strip ──
    if not data["orderbook"].empty:
        last_poll = data["orderbook"]["polled_at"].max()
    else:
        last_poll = None
    last_poll_label, last_poll_cls = _fmt_age(str(last_poll) if last_poll is not None else None, now_utc)

    if not data["snapshot"].empty:
        last_run = data["snapshot"]["run_date"].max()
    else:
        last_run = None
    last_run_label, last_run_cls = _fmt_age(str(last_run) if last_run is not None else None, now_utc)

    n_active_cities = len({c["city_key"] for c in cards})
    n_active_markets = sum(len(c["buckets"]) for c in cards)
    total_exposure = sum(c["exposure"] for c in cards)
    total_cost = sum(c.get("cost_basis") or 0 for c in cards)
    total_open_pnl = sum(c.get("open_pnl") or 0 for c in cards)
    total_realized_pnl = sum(c.get("realized_pnl_today") or 0 for c in cards)
    total_live_pnl = total_open_pnl + total_realized_pnl
    n_positions = sum(len(c["positions"]) for c in cards)

    # Aggregate per-arm P&L across all cards.
    arm_totals: dict[str, dict] = {}
    for c in cards:
        for arm, slot in (c.get("arm_pnl") or {}).items():
            t = arm_totals.setdefault(arm, {"open_pnl": 0.0, "realized": 0.0, "cost": 0.0, "now": 0.0, "n": 0})
            t["open_pnl"] += slot["open_pnl"]
            t["realized"] += slot["realized"]
            t["cost"] += slot["cost"]
            t["now"] += slot["now"]
            t["n"] += slot["n"]

    def _fmt_arm_summary(totals: dict[str, dict]) -> str:
        if not totals:
            return "no positions"
        # Sort: control first, then treatment, then anything else alphabetically.
        order = sorted(totals.keys(), key=lambda k: (0 if k == "control" else 1 if k == "treatment" else 2, k))
        parts = []
        for arm in order:
            t = totals[arm]
            pnl = t["open_pnl"] + t["realized"]
            cls = "pos" if pnl > 0.5 else "neg" if pnl < -0.5 else "muted"
            parts.append(
                f'<span class="{cls}">{arm}: ${pnl:+,.0f}</span> '
                f'<span class="muted">(cost ${t["cost"]:,.0f})</span>'
            )
        return " · ".join(parts)

    arm_summary_html = _fmt_arm_summary(arm_totals)
    positions_live = "positions" in data and not data["positions"].empty
    if positions_live:
        latest_pos_poll = data["positions"]["polled_at"].max()
        pos_age_label, pos_age_cls = _fmt_age(str(latest_pos_poll), now_utc)
        exposure_subtitle = f"<span class='ticker'>live · {pos_age_label}</span>"
    else:
        exposure_subtitle = "<span class='ticker warn'>fallback (snap.position)</span>"

    def _safe_int(v) -> int:
        # SQL SUM() over no rows returns NaN; pd.NA / NaN both blow up int().
        try:
            if v is None or pd.isna(v):
                return 0
        except (TypeError, ValueError):
            pass
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    fr_df = data.get("fill_rate", pd.DataFrame())
    if not fr_df.empty:
        fr = fr_df.iloc[0]
        placed = _safe_int(fr.get("placed"))
        filled = _safe_int(fr.get("filled"))
        fr_pc = _safe_int(fr.get("placed_control"))
        ff_c = _safe_int(fr.get("filled_control"))
        fr_pt = _safe_int(fr.get("placed_treatment"))
        ff_t = _safe_int(fr.get("filled_treatment"))
    else:
        placed = filled = fr_pc = ff_c = fr_pt = ff_t = 0
    if placed:
        rate = filled / placed
        rate_c = (ff_c / fr_pc) if fr_pc else 0.0
        rate_t = (ff_t / fr_pt) if fr_pt else 0.0
        fill_rate_main = f"{filled:,}/{placed:,} = {rate*100:.1f}%"
        fill_rate_arm = (
            f"control: {ff_c:,}/{fr_pc:,} ({rate_c*100:.0f}%) · "
            f"treatment: {ff_t:,}/{fr_pt:,} ({rate_t*100:.0f}%)"
        )
    else:
        fill_rate_main = "—"
        fill_rate_arm = "no orders today"

    top_html = f"""
<header class="top">
  <div class="cell brand">
    <div class="value">KXHIGH · Live <button class="hist-btn" onclick="openDetail('history')">📊 history</button></div>
    <div class="sub">{now_utc.strftime("%Y-%m-%d %H:%M UTC")} · data {(_fmt_et(data['snapshot']['run_date'].max()) if not data['snapshot'].empty else '—')[11:16]} ET<br>page reloads every 3m · upstream rebuilds every 20m</div>
  </div>
  <div class="cell">
    <div class="label">Last orderbook poll</div>
    <div class="value"><span class="dot {last_poll_cls}"></span>{last_poll_label}</div>
  </div>
  <div class="cell">
    <div class="label">Last trading run</div>
    <div class="value"><span class="dot {last_run_cls}"></span>{last_run_label}</div>
  </div>
  <div class="cell">
    <div class="label">Positions</div>
    <div class="value">{n_positions} <span class="ticker">{n_active_cities} cities</span></div>
  </div>
  <div class="cell">
    <div class="label">Cost · now</div>
    <div class="value">${total_cost:,.0f} · ${total_exposure:,.0f}<br><span class="ticker">{exposure_subtitle}</span></div>
  </div>
  <div class="cell">
    <div class="label">Live P&amp;L</div>
    <div class="value {('pos' if total_live_pnl > 0.5 else 'neg' if total_live_pnl < -0.5 else 'muted')}">
      ${total_live_pnl:+,.0f}<br>
      <span class="ticker">{arm_summary_html}</span>
    </div>
  </div>
  <div class="cell">
    <div class="label">Fill rate (open markets)</div>
    <div class="value">{fill_rate_main}<br>
      <span class="ticker">{fill_rate_arm}</span>
    </div>
  </div>
</header>
"""

    # ── cards ──
    # Count cards per city so we can show a date subtitle on cards from
    # cities with multiple active forecast_dates (e.g., positions still
    # open on yesterday's market alongside today's). Single-card cities
    # don't show the date — keeps the header clean for the common case.
    cards_per_city: dict[str, int] = {}
    for _c in cards:
        cards_per_city[_c["city_key"]] = cards_per_city.get(_c["city_key"], 0) + 1

    cards_html_parts: list[str] = []
    for c in cards:
        edge_val = c["best_edge"]["edge"] if c["best_edge"] else 0.0
        edge_label = f"{edge_val * 100:+.0f}¢"
        edge_bucket = c["best_edge"]["label"] if c["best_edge"] else "—"

        # Stripe color = live mark-to-market P&L sign. Flat cards never get a
        # red/green stripe — the "edge" by itself isn't a portfolio outcome,
        # so green/red there is misleading.
        positioned = bool(c["positions"])
        live_pnl = c["live_pnl"]
        if positioned:
            edge_cls = "edge-pos" if live_pnl > 0.01 else "edge-neg" if live_pnl < -0.01 else "edge-flat"
        else:
            edge_cls = "edge-flat"

        # Top-right: live P&L + cost basis when positioned, nothing on flat
        # cards. Position count and current value still in the modal KV.
        if positioned:
            pnl_cls = "pos" if live_pnl > 0.01 else "neg" if live_pnl < -0.01 else "muted"
            pnl_str = f"${live_pnl:+,.0f}" if abs(live_pnl) >= 0.5 else "$0"
            top_right_html = (
                f'<div class="stat-val muted" style="font-size:12px">cost ${c["cost_basis"]:,.0f}</div>'
                f'<div class="stat-val {pnl_cls}" style="font-size:12px">P&amp;L {pnl_str}</div>'
            )
        else:
            top_right_html = ""

        forecast_avg = c["forecast_avg"]
        forecast_std = c["forecast_std"] or 0.0
        nws = c["nws"]; accu = c["accu"]; wu = c["wu"]

        # bucket bars — buckets we hold get a tinted row + left-edge stripe +
        # an inline position tag (e.g. "364 NO") so the user can see at a
        # glance which buckets they're in without scrolling to the chips.
        bucket_rows = []
        for b in c["buckets"]:
            mkt = b["market_p"]; mdl = b["model_p"]; e = b["edge"]
            mkt_w = (mkt or 0) * 100
            mdl_w = (mdl or 0) * 100
            edge_str = "—" if e is None else f"{e * 100:+.0f}"
            edge_class = "" if e is None else ("pos" if e > 0.02 else "neg" if e < -0.02 else "")
            pos = b.get("position", 0)
            row_cls = ""
            pos_tag = ""
            if pos:
                side = "NO" if pos < 0 else "YES"
                row_cls = "held-no" if pos < 0 else "held-yes"
                pos_tag = f'<span class="pos-tag {side.lower()}">{abs(pos)} {side}</span>'
            # Open-order tick: drawn at the YES position implied by the
            # bot's most-aggressive resting NO bid (100 − no_price). With
            # a NO bid of e.g. 38¢, the tick lands at YES = 62% on the
            # bar. Tag includes (C) or (T) for the A/B arm so the user
            # can see which arm placed the bid without opening the modal.
            order_tick = ""
            ob = b.get("open_no_bid")
            if ob:
                yes_pct = max(0, min(100, 100 - ob))
                arm = b.get("arm") or ""
                arm_tag = " (C)" if arm == "control" else " (T)" if arm == "treatment" else ""
                order_tick = (
                    f'<div class="order-tick" style="left:{yes_pct}%" '
                    f'data-px="NO {ob}c{arm_tag}" '
                    f'title="top open NO bid: {ob}c (implied YES {yes_pct}%, arm={arm or "?"})"></div>'
                )
            bucket_rows.append(
                f'<div class="bucket-row {row_cls}">'
                f'<div class="bucket-label">{b["label"]}</div>'
                f'<div class="bars">'
                f'<div class="bar-mkt" style="width:{mkt_w:.0f}%"></div>'
                f'<div class="bar-mdl" style="width:{mdl_w:.0f}%"></div>'
                f'{order_tick}'
                f'</div>'
                f'<div class="bucket-edge {edge_class}">'
                f'<span>{edge_str}</span>{pos_tag}'
                f'</div>'
                f'</div>'
            )

        # position chips — Kalshi position is signed (− = NO inventory).
        # Display contract count + side rather than a raw signed number to
        # avoid "+50" reading as YES when it's actually NO. Append P&L if
        # there's any realized so far on the bucket.
        if c["positions"]:
            chips = []
            for p in c["positions"]:
                pos = p["position"]
                side = "NO" if pos < 0 else "YES"
                qty = abs(pos)
                pnl = p.get("open_pnl")
                if pnl is not None and abs(pnl) >= 0.01:
                    pnl_cls = "pnl-pos" if pnl > 0 else "pnl-neg"
                    pnl_chip = f' <span class="{pnl_cls}">${pnl:+.0f}</span>'
                else:
                    pnl_chip = ""
                chips.append(
                    f'<span class="chip"><span class="b">{p["label"]}</span>'
                    f'{qty} {side}{pnl_chip}</span>'
                )
            chips_html = f'<div class="pos-chip">{"".join(chips)}</div>'
        else:
            chips_html = '<div class="pos-chip"><span class="chip muted">flat</span></div>'

        sparkline_id = f"spark-{c['city_key']}"
        # (sparkline data now resolved later in the spark_js block)

        forecast_summary = (
            f'{forecast_avg:.1f}° ±{forecast_std:.1f}'
            if forecast_avg is not None else "—"
        )
        sources = " · ".join(filter(None, [
            f"NWS {nws:.0f}" if nws is not None else None,
            f"AW {accu:.0f}" if accu is not None else None,
            f"WU {wu:.0f}" if wu is not None else None,
        ])) or "—"

        cards_html_parts.append(f"""
<div class="card {edge_cls}" id="card-{c['city_key']}" onclick="openDetail('{c['city_key']}')">
  <div class="stripe"></div>
  <div class="open-hint">click for details ▸</div>
  <div class="card-head">
    <div>
      <div class="city">{c['city_key']}{(' <span class="ticker">' + c['forecast_date'][5:] + '</span>') if cards_per_city.get(c['city_key'], 0) > 1 else ''}</div>
      <div class="ticker">{sources}</div>
      {f'<div class="ticker">{c["nws_short"]}</div>' if c.get("nws_short") else ''}
    </div>
    <div style="text-align:right">
      {top_right_html}
    </div>
  </div>

  <div class="row">
    <div><div class="stat">Forecast</div><div class="stat-val">{forecast_summary}</div></div>
    <div><div class="stat">High so far</div><div class="stat-val">{(f"{c['peak_temp']:.0f}°" if c['peak_temp'] is not None else (f"{c['observed_temp']:.0f}°" if c['observed_temp'] is not None else '—'))}</div></div>
  </div>

  <div class="spark"><canvas id="{sparkline_id}"></canvas></div>

  <div class="buckets">
    {''.join(bucket_rows) or '<div class="muted" style="font-size:11px">no buckets</div>'}
  </div>

  {chips_html}
</div>
""")
    cards_html = "<section class='grid'>" + "\n".join(cards_html_parts) + "</section>"

    # ── edge heatmap (top-N buckets per card) ──
    # Collect a uniform set of top buckets across all cards for the column header.
    all_labels: list[str] = []
    seen = set()
    for c in cards:
        for b in c["buckets"]:
            if b["label"] not in seen:
                seen.add(b["label"]); all_labels.append(b["label"])
    # narrow to ~6 most-populated
    label_counts: dict[str, int] = {}
    for c in cards:
        for b in c["buckets"]:
            label_counts[b["label"]] = label_counts.get(b["label"], 0) + 1
    # Pick the 6 most-traded buckets, then re-sort by temperature so the
    # x-axis reads cold→hot left→right (otherwise columns appeared in a
    # frequency-driven jumble like "89-90, 68-69, 66-67").
    top_labels = sorted(label_counts, key=lambda k: -label_counts[k])[:6]
    top_labels = sorted(top_labels, key=lambda k: (_label_sort_key(k), k))
    heat_cells: list[str] = ['<div></div>']
    heat_cells.extend(f'<div class="h-h">{lab}</div>' for lab in top_labels)
    for c in cards:
        heat_cells.append(f'<div class="h-c">{c["city_key"]}</div>')
        bucket_by_label = {b["label"]: b for b in c["buckets"]}
        for lab in top_labels:
            b = bucket_by_label.get(lab)
            if b is None or b["edge"] is None:
                heat_cells.append('<div class="h-empty"></div>')
            else:
                e = b["edge"]
                x = max(-0.15, min(0.15, e)) / 0.15
                if x >= 0:
                    bg = f"rgba(46,204,113,{0.15 + x * 0.7:.2f})"
                else:
                    bg = f"rgba(255,91,110,{0.15 + (-x) * 0.7:.2f})"
                heat_cells.append(
                    f'<div class="h-cell" style="background:{bg}">{e * 100:+.0f}</div>'
                )
    heat_grid_cols = f"64px repeat({len(top_labels)},1fr)"
    heat_html = (
        f'<div class="heat" style="grid-template-columns:{heat_grid_cols}">'
        + "".join(heat_cells)
        + "</div>"
    )

    # ── runs panel ──
    runs_df = data["runs"]
    if runs_df.empty:
        runs_html = '<div class="muted" style="font-size:11px">no runs in last 48h</div>'
    else:
        runs_df = runs_df.copy()
        runs_df = runs_df.sort_values("event_at", ascending=False)
        rows = []
        for _, r in runs_df.head(10).iterrows():
            status = r["exit_status"] or "?"
            cls = "green" if status == "success" else "red"
            ts_label = _fmt_et(r["event_at"], fmt="%m-%d %H:%M")
            dur = f'{r["duration_seconds"]:.0f}s' if pd.notna(r["duration_seconds"]) else "—"
            rows.append(
                f'<tr><td><span class="dot {cls}"></span>{r["script_name"]}</td>'
                f'<td>{status}</td><td>{ts_label} ET · {dur}</td></tr>'
            )
        runs_html = "<table class='runs'>" + "".join(rows) + "</table>"

    # ── missed-run detector ──
    # Trading-bot cron is `2 2,9,11,23 * * *` UTC; each slot should produce
    # one successful end event. GH Actions cron drift can be substantial
    # (up to ~3.5h on the 02:02 slot historically) and runs occasionally
    # get skipped, so we compare each scheduled slot in the last 24h
    # against actual ends and flag the missing ones. A slot at HH:02 UTC
    # is "satisfied" by any successful end between HH:02 and HH:02+4h.
    # For the orderbook-logger we flag when the most recent end is older
    # than 60 min (its cron is */20, two skipped ticks).
    # Next-slot-bounded windows: slot at time t is "covered" by any run
    # in [t, next_scheduled_slot). This avoids the overlap problem where
    # two adjacent slots both claim the same run as coverage.
    EXPECTED_TRADING_SLOTS_UTC = (2, 9, 11, 23)
    missed_runs: list[dict] = []
    runs_raw_df = data.get("runs_raw", pd.DataFrame())
    if not runs_raw_df.empty:
        all_ends = runs_raw_df.copy()
        all_ends["event_at"] = pd.to_datetime(all_ends["event_at"], utc=True)
        trading_ends = all_ends[
            (all_ends["script_name"] == "high_temp_trading.py")
            & (all_ends["exit_status"] == "success")
        ]
        # Build the past 28h of scheduled slot times, sorted chronologically.
        slot_times: list[pd.Timestamp] = []
        for hours_back in range(28, -2, -1):
            slot_dt = (now_utc - pd.Timedelta(hours=hours_back)).replace(minute=2, second=0, microsecond=0)
            if slot_dt.hour in EXPECTED_TRADING_SLOTS_UTC and slot_dt <= now_utc:
                slot_times.append(slot_dt)
        slot_times.sort()
        # For each slot, the window is [slot, next_slot). The most recent
        # slot's window extends to "now" — but we only flag it as missed
        # once the next scheduled slot has been reached (otherwise we'd
        # cry wolf on slots whose drift is still in progress).
        for i, slot in enumerate(slot_times):
            next_slot = slot_times[i + 1] if i + 1 < len(slot_times) else None
            if next_slot is None:
                # The next scheduled slot is in the future — find the next
                # slot AFTER `now`. Window for the current "last" slot
                # closes when that next slot starts.
                future_slot = None
                for h in range(0, 13):
                    candidate = (now_utc + pd.Timedelta(hours=h)).replace(minute=2, second=0, microsecond=0)
                    if candidate.hour in EXPECTED_TRADING_SLOTS_UTC and candidate > now_utc:
                        future_slot = candidate
                        break
                if future_slot is None:
                    continue  # shouldn't happen
                window_end = future_slot
                # Don't flag yet if the window hasn't closed.
                if window_end > now_utc:
                    continue
            else:
                window_end = next_slot
            covered = trading_ends[
                (trading_ends["event_at"] >= slot)
                & (trading_ends["event_at"] < window_end)
            ]
            if len(covered) == 0:
                missed_runs.append({
                    "kind": "trading",
                    "slot_et": _fmt_et(slot, fmt="%m-%d %H:%M"),
                })
        ob_ends = all_ends[
            (all_ends["script_name"] == "kxhigh_orderbook_logger.py")
            & (all_ends["exit_status"] == "success")
        ]
        if not ob_ends.empty:
            last_ob = ob_ends["event_at"].max()
            stale_min = (now_utc - last_ob.to_pydatetime()).total_seconds() / 60.0
            if stale_min > 60:
                missed_runs.append({
                    "kind": "orderbook",
                    "stale_min": stale_min,
                    "last_seen": _fmt_et(last_ob, fmt="%m-%d %H:%M"),
                })

    if missed_runs:
        miss_rows = []
        for m in missed_runs:
            if m["kind"] == "trading":
                miss_rows.append(
                    f'<div style="font-size:11px;color:var(--red);padding:2px 0">'
                    f'<span class="dot red"></span>'
                    f'missed trading slot · scheduled {m["slot_et"]} ET</div>'
                )
            else:
                miss_rows.append(
                    f'<div style="font-size:11px;color:var(--red);padding:2px 0">'
                    f'<span class="dot red"></span>'
                    f'orderbook poll stale · last seen {m["last_seen"]} ET '
                    f'({m["stale_min"]:.0f}m ago)</div>'
                )
        missed_html = (
            f'<div style="border-top:1px solid var(--line);margin-top:6px;padding-top:6px">'
            f'<div style="font-size:10px;color:var(--ink-dim);margin-bottom:2px;'
            f'text-transform:uppercase;letter-spacing:.06em">missed</div>'
            + "".join(miss_rows) + "</div>"
        )
    else:
        missed_html = ""

    # ── recent activity panel ──
    rf_df = data.get("recent_fills", pd.DataFrame())
    if rf_df is None or rf_df.empty:
        recent_html = '<div class="muted" style="font-size:11px">no fills in last cycle</div>'
    else:
        rows = []
        for _, r in rf_df.iterrows():
            t = _fmt_et(r["created_time"], fmt="%I:%M%p").lstrip("0")
            ticker = r["market_ticker"]
            # Strip "KXHIGH" prefix and event date for compact display.
            short_tk = re.sub(r"^KXHIGH", "", ticker) if ticker else ""
            count = float(r.get("count_fp") or 0)
            side = (r.get("side") or "").upper()  # "no" / "yes"
            # The fill price for the side we crossed: no_price for NO,
            # yes_price for YES. Both stored as dollars (e.g., 0.48).
            no_p = r.get("no_price_dollars")
            yes_p = r.get("yes_price_dollars")
            if side == "NO" and pd.notna(no_p):
                px_dollars = float(no_p)
            elif side == "YES" and pd.notna(yes_p):
                px_dollars = float(yes_p)
            else:
                px_dollars = None
            price = f'{px_dollars*100:.0f}c' if px_dollars is not None else "—"
            cost = f'${abs(count) * px_dollars:.0f}' if px_dollars is not None else "—"
            side_cls = "neg" if side == "NO" else "pos" if side == "YES" else ""
            rows.append(
                f'<tr><td style="color:var(--ink-dim);font-size:10px">{t}</td>'
                f'<td style="font-size:10px">{short_tk}</td>'
                f'<td>{count:.0f}</td>'
                f'<td class="{side_cls}">{side}</td>'
                f'<td>{price}</td><td>{cost}</td></tr>'
            )
        recent_html = "<table class='runs'>" + "".join(rows) + "</table>"

    # ── live NWS hourly-drift panel ──
    # For each city: earliest poll vs latest poll within the last 12h. If
    # the predicted daily high has shifted ≥ a threshold, emit a drift row.
    # This refreshes every 30 min (orderbook-logger cron) — independent of
    # the trading-bot's 4×/day snapshot cadence.
    drift_alerts: list[dict] = []
    drift_df = data.get("hourly_fcst_drift", pd.DataFrame())
    if drift_df is not None and not drift_df.empty:
        for city_abv, grp in drift_df.groupby("city_abv"):
            g = grp.sort_values("polled_at")
            if len(g) < 2:
                continue
            first = g.iloc[0]
            last = g.iloc[-1]
            try:
                t0 = pd.Timestamp(first["polled_at"])
                t1 = pd.Timestamp(last["polled_at"])
                p0 = float(first["predicted_peak"])
                p1 = float(last["predicted_peak"])
            except Exception:
                continue
            delta = p1 - p0
            if abs(delta) < 1.0:
                continue
            hours = max(0.5, (t1 - t0).total_seconds() / 3600.0)
            arrow = "↑" if delta > 0 else "↓"
            sev = "green" if delta > 0 else "red"
            drift_alerts.append({
                "severity": sev,
                "city": city_abv,
                "when": _fmt_et(last["polled_at"], fmt="%I:%M%p ET").lstrip("0"),
                "what": (f"<b>{city_abv}</b> NWS hourly peak {arrow} "
                         f"{p0:.0f}° → {p1:.0f}° over {hours:.1f}h"),
                "rank": abs(delta),
            })
    drift_alerts.sort(key=lambda a: -a["rank"])
    drift_alerts = drift_alerts[:8]
    if not drift_alerts:
        drift_html = '<div class="muted" style="font-size:11px">no significant drift in last 12h</div>'
    else:
        drift_html = "".join(
            f'<div class="alert"><span class="dot {a["severity"]}"></span>'
            f'<div class="what">{a["what"]} '
            f'<span class="muted" style="font-size:10px">({a["when"]})</span></div></div>'
            for a in drift_alerts
        )

    # ── alerts panel ──
    alerts = compute_alerts(cards, data.get("forecast_history", pd.DataFrame()),
                            forecast_peak=data.get("forecast_peak", pd.DataFrame()))
    if not alerts:
        alerts_html = '<div class="muted" style="font-size:11px">no alerts</div>'
    else:
        alerts_html = "".join(
            f'<div class="alert"><span class="dot {a["severity"]}"></span>'
            f'<div class="what">{a["what"]}'
            f'{" <span class=\"muted\" style=\"font-size:10px\">(" + a["when"] + ")</span>" if a.get("when") and a["when"] != "—" else ""}'
            f'</div></div>'
            for a in alerts
        )

    # ── per-city detail modals (pre-rendered, hidden until clicked) ──
    modals_html, modal_chart_js = _render_modals(cards, data, forecast_spark)

    # ── history modal: settled P&L over last 30 days ──
    history_modal_html, history_chart_js = _render_history_modal(data.get("daily_pnl", pd.DataFrame()))

    # ── sparkline JS ──
    # The card sparkline shows TWO series sharing one time axis:
    #   green line  = NWS hourly observations (today, intraday)
    #   blue dashed = NWS hourly forecast (next ~30h)
    # Their union of timestamps becomes the chart's labels; each dataset
    # carries `null` for hours where it has no data, with `spanGaps:false`.
    # If neither series has ≥2 points (e.g., obs table just created), fall
    # back to the forecast-avg-over-runs sparkline so the card isn't blank.
    spark_js_lines = []
    for c in cards:
        obs = obs_spark.get((c["city_key"], c["forecast_date"]), [])
        fcst_h = fcst_spark.get(c["city_key"], [])
        fcst_runs = forecast_spark.get(c["city_name"], [])

        if len(obs) >= 2 or len(fcst_h) >= 2:
            # Merge timestamps into a single sorted axis. Each timestamp
            # also carries the most-recent polled_at for that series so
            # the tooltip can show "added at HH:MM ET".
            ts: dict[str, dict[str, object]] = {}
            for p in obs:
                slot = ts.setdefault(p["t"], {"obs": None, "fcst": None,
                                              "obs_added": None, "fcst_added": None})
                slot["obs"] = p["v"]; slot["obs_added"] = p.get("added")
            for p in fcst_h:
                slot = ts.setdefault(p["t"], {"obs": None, "fcst": None,
                                              "obs_added": None, "fcst_added": None})
                slot["fcst"] = p["v"]; slot["fcst_added"] = p.get("added")
            ordered = sorted(ts.keys())
            labels = ordered
            obs_vals = [ts[t]["obs"] for t in ordered]
            fcst_vals = [ts[t]["fcst"] for t in ordered]
            obs_added = [ts[t]["obs_added"] for t in ordered]
            fcst_added = [ts[t]["fcst_added"] for t in ordered]
            # spanGaps:true on obs so the green line bridges null hours we
            # haven't polled yet. polledAt[] is a parallel array used by
            # the external tooltip handler to render "(added HH:MM)".
            data_block = (
                f"datasets:["
                f"{{label:'fcst',data:{json.dumps(fcst_vals)},"
                f"polledAt:{json.dumps(fcst_added)},"
                f"borderColor:'#4aa3ff',"
                f"borderWidth:1.5,borderDash:[3,3],tension:.25,pointRadius:0,"
                f"fill:false,spanGaps:true}},"
                f"{{label:'obs',data:{json.dumps(obs_vals)},"
                f"polledAt:{json.dumps(obs_added)},"
                f"borderColor:'#2ecc71',"
                # pointRadius was 2.5 — looked clean when each cron tick
                # wrote one obs (~12 points/chart over 12h). Now that the
                # poller does a full 8h re-pull at the station's native
                # 5-min cadence, charts have ~96 points and the dots merge
                # into a fat line. Hide them by default; reveal on hover.
                f"borderWidth:2,tension:.25,pointRadius:0,pointHoverRadius:4,"
                f"pointHitRadius:8,fill:false,spanGaps:true}}"
                f"]"
            )
        elif len(fcst_runs) >= 2:
            labels = [s["t"] for s in fcst_runs]
            values = [s["v"] for s in fcst_runs]
            data_block = (
                f"datasets:[{{label:'forecast μ',data:{json.dumps(values)},"
                f"borderColor:'#4aa3ff',borderWidth:2,tension:.35,pointRadius:0,fill:false}}]"
            )
        else:
            continue
        # Tooltip is HTML-rendered (see _SPARK_TOOLTIP_FN below). The
        # native canvas tooltip is disabled so we don't clip against the
        # 72px canvas height with multi-line content.
        spark_js_lines.append(
            f"new Chart(document.getElementById('spark-{c['city_key']}'), {{"
            f"type:'line',data:{{labels:{json.dumps(labels)},{data_block}}},"
            f"options:{{animation:false,responsive:true,maintainAspectRatio:false,"
            f"interaction:{{mode:'index',intersect:false}},"
            f"plugins:{{legend:{{display:false}},"
            f"tooltip:{{enabled:false,external:_sparkTooltip}}}},"
            f"scales:{{x:{{display:false}},y:{{display:false}}}}}}}});"
        )
    # Wrap each init in try/catch (so a single failure doesn't break the
    # rest of the script block) and defer the whole batch until after the
    # browser has computed layout for the cards. Without the deferral,
    # canvases below the initial viewport had 0×0 dimensions at script
    # execution time and Chart.js silently rendered nothing — that was
    # the root cause of "TDC/TSEA/TMIN/TNOLA blank but TOKC/AUS show".
    safe_lines = [
        f"try {{ {line} }} catch(e) {{ console.error('spark init failed:', e); }}"
        for line in spark_js_lines
    ]
    spark_js = (
        "<script>\n"
        + _SPARK_TOOLTIP_FN
        + "\nfunction _initSparklines(){\n"
        + "\n".join(safe_lines)
        + "\n}\n"
        "if (document.readyState === 'complete') {\n"
        "  requestAnimationFrame(_initSparklines);\n"
        "} else {\n"
        "  window.addEventListener('load', () => requestAnimationFrame(_initSparklines));\n"
        "}\n"
        "</script>"
    )

    body = (
        TEMPLATE_HEAD
        + top_html
        + "<main>"
        + cards_html
        + "<aside>"
        + f"<div class='panel'><h3>Alerts <span class='count'>{len(alerts)}</span></h3>"
          f"<div style='font-size:10px;color:var(--ink-dim);margin-bottom:6px'>"
          f"snapshot-based · refreshes 4×/day at trading-bot cron</div>{alerts_html}</div>"
        + f"<div class='panel'><h3>NWS hourly drift <span class='count'>{len(drift_alerts)}</span></h3>"
          f"<div style='font-size:10px;color:var(--ink-dim);margin-bottom:6px'>"
          f"live NWS forecast · refreshes every 20m</div>{drift_html}</div>"
        + f"<div class='panel'><h3>Recent activity <span class='count'>{len(rf_df)}</span></h3>{recent_html}</div>"
        + f"<div class='panel'><h3>Edge heatmap (city × bucket) "
          f"<span class='legend'><span class='mkt'><i></i>mkt</span>"
          f"<span class='mdl'><i></i>model</span></span></h3>"
          f"{heat_html}<div style='font-size:10px;color:var(--ink-dim);margin-top:6px'>"
          f"Cell = model_p − market_mid · green&nbsp;&gt;&nbsp;0 · red&nbsp;&lt;&nbsp;0</div></div>"
        + f"<div class='panel'><h3>Run health <span class='count'>{len(runs_df)}</span>"
          f"{(' <span style=\"color:var(--red);font-size:10px;margin-left:6px\">' + str(len(missed_runs)) + ' missed</span>') if missed_runs else ''}"
          f"</h3>{runs_html}{missed_html}</div>"
        + "</aside>"
        + "</main>"
        + modals_html
        + history_modal_html
        + f"<footer class='note'>{len(cards)} cards · {n_active_markets} markets · "
          f"snapshots through {data['snapshot']['run_date'].max() if not data['snapshot'].empty else 'n/a'} · "
          f"orderbook through {last_poll if last_poll is not None else 'n/a'}</footer>"
        + spark_js
        + modal_chart_js
        + history_chart_js
        + _MODAL_NAV_JS
        + "</body></html>"
    )
    return body


_SPARK_TOOLTIP_FN = """
// HTML tooltip for sparklines. Single shared element appended to body so
// it positions absolutely without being clipped by the card's overflow
// or the small canvas. Receives Chart.js external-tooltip context and
// renders title + obs + fcst on three lines.
const _spark_tip = (() => {
  let el = document.getElementById('spark-tip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'spark-tip';
    document.body.appendChild(el);
  }
  return el;
})();
function _sparkTooltip(ctx) {
  const tt = ctx.tooltip;
  if (!tt || tt.opacity === 0) {
    _spark_tip.style.display = 'none';
    return;
  }
  const rawTitle = (tt.title && tt.title[0]) || '';
  const title = (rawTitle.length >= 16 ? rawTitle.substring(5, 16) : rawTitle) + ' ET';
  const items = (tt.dataPoints || []).filter(d => d.parsed && d.parsed.y != null);
  if (items.length === 0) { _spark_tip.style.display = 'none'; return; }
  // For each present series, render its value. If a series is missing at
  // the hover label (e.g., forecast is hourly but obs lands at 09:35),
  // walk outward up to ±3 indices to find the nearest non-null value
  // and surface it with "(near)" so the user sees both obs and forecast
  // even when their timestamps don't perfectly align.
  const seen = new Set(items.map(d => d.dataset.label));
  const idx = items[0].dataIndex;
  const datasets = ctx.chart.data.datasets || [];
  const labels = ctx.chart.data.labels || [];
  // Label format is "YYYY-MM-DD HH:MM" in ET — pull just the HH:MM portion
  // for the per-line "recorded HH:MM ET" annotation.
  const _hhmm = (s) => (s && s.length >= 16) ? s.substring(11, 16) + ' ET' : s;
  const lines = items.map(d => {
    const lbl = d.dataset.label || '';
    const cls = lbl === 'obs' ? 'tt-obs' : lbl === 'fcst' ? 'tt-fcst' : '';
    const recorded = _hhmm(labels[d.dataIndex]);
    const added = (d.dataset.polledAt && d.dataset.polledAt[d.dataIndex]) || '';
    const meta = [];
    if (recorded) meta.push(`recorded ${recorded}`);
    if (added) meta.push(`added ${added}`);
    const metaTag = meta.length ? ` <span style="opacity:.55">${meta.join(' \\u00b7 ')}</span>` : '';
    return `<div class="${cls}">${lbl}: ${d.parsed.y.toFixed(1)}\\u00b0F${metaTag}</div>`;
  });
  for (const ds of datasets) {
    if (seen.has(ds.label)) continue;
    let val = null, near_idx = null;
    for (let off = 1; off <= 3; off++) {
      if (typeof ds.data[idx - off] === 'number') { val = ds.data[idx - off]; near_idx = idx - off; break; }
      if (typeof ds.data[idx + off] === 'number') { val = ds.data[idx + off]; near_idx = idx + off; break; }
    }
    if (val != null) {
      const cls = ds.label === 'obs' ? 'tt-obs' : 'tt-fcst';
      const recorded = _hhmm(labels[near_idx]);
      const added = (ds.polledAt && ds.polledAt[near_idx]) || '';
      const meta = [];
      if (recorded) meta.push(`recorded ${recorded}`);
      if (added) meta.push(`added ${added}`);
      const metaTag = meta.length ? ` <span style="opacity:.55">${meta.join(' \\u00b7 ')}</span>` : '';
      lines.push(`<div class="${cls}">${ds.label}: ${val.toFixed(1)}\\u00b0F <span style="opacity:.5">(near)</span>${metaTag}</div>`);
    }
  }
  _spark_tip.innerHTML = `<div class="tt-title">${title}</div>${lines.join('')}`;
  const rect = ctx.chart.canvas.getBoundingClientRect();
  _spark_tip.style.left = (rect.left + window.scrollX + tt.caretX) + 'px';
  _spark_tip.style.top = (rect.top + window.scrollY + tt.caretY) + 'px';
  _spark_tip.style.display = 'block';
}
"""


_MODAL_NAV_JS = """
<script>
const _modalCharts = {};  // city → array of Chart instances (lazy-initialized)
function openDetail(city) {
  const m = document.getElementById('modal-' + city);
  if (!m) return;
  m.classList.add('open');
  // lazy-init charts for this city the first time it's opened
  if (!_modalCharts[city] && typeof window['initModalCharts_' + city] === 'function') {
    _modalCharts[city] = window['initModalCharts_' + city]();
  }
}
function closeDetail(city) {
  const m = document.getElementById('modal-' + city);
  if (m) m.classList.remove('open');
}
document.addEventListener('click', (e) => {
  if (e.target && e.target.classList && e.target.classList.contains('modal-bg')) {
    e.target.classList.remove('open');
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
  }
});
</script>
"""


def _render_history_modal(daily_pnl: pd.DataFrame) -> tuple[str, str]:
    """Build the History modal: list of past dashboard snapshots stored at
    `archive/{YYYY-MM-DD}.html` in GCS. Each entry links to the full
    archived dashboard for that day, plus the day's settled P&L if we
    have it from `KXHIGH_settlements_clean`.

    The archive grows by one entry per ET-day going forward (each build
    overwrites today's file — the final write at end-of-day is the
    permanent record).
    """
    archive_dates = _list_archive_dates()

    # Index settled-P&L by event_date so we can annotate each entry.
    pnl_by_date: dict[str, dict] = {}
    if daily_pnl is not None and not daily_pnl.empty:
        for _, r in daily_pnl.iterrows():
            pnl_by_date[str(r["event_date"])] = {
                "pnl": float(r["pnl"]) if pd.notna(r["pnl"]) else 0.0,
                "cost": float(r["total_cost"]) if pd.notna(r["total_cost"]) else 0.0,
                "n": int(r["n_markets"]) if pd.notna(r["n_markets"]) else 0,
                "wins": int(r["n_wins"]) if pd.notna(r["n_wins"]) else 0,
            }

    if not archive_dates:
        body = (
            '<div class="muted" style="font-size:12px;line-height:1.6">'
            'No archives yet — each dashboard build snapshots itself to '
            '<code>archive/{YYYY-MM-DD}.html</code> in GCS. Today\'s archive '
            'will appear here on the next loop tick (within 30 min); past '
            'days fill in as the bot runs over time.'
            '</div>'
        )
    else:
        rows = []
        for d in archive_dates:
            url = f"https://storage.googleapis.com/{PUBLIC_BUCKET}/archive/{d}.html"
            stats = pnl_by_date.get(d)
            if stats:
                pnl = stats["pnl"]; cost = stats["cost"]
                cls = "pos" if pnl > 0.01 else "neg" if pnl < -0.01 else "muted"
                roi = (pnl / cost * 100) if cost else 0.0
                wr = (stats["wins"] / stats["n"] * 100) if stats["n"] else 0.0
                stats_cell = (
                    f'<td>{stats["n"]}</td>'
                    f'<td>${cost:,.0f}</td>'
                    f'<td class="{cls}">${pnl:+,.0f}</td>'
                    f'<td class="{cls}">{roi:+.1f}%</td>'
                    f'<td>{stats["wins"]}/{stats["n"]} · {wr:.0f}%</td>'
                )
            else:
                stats_cell = '<td colspan="5" class="muted">unsettled</td>'
            rows.append(
                f'<tr><td><a href="{url}" target="_blank" '
                f'style="color:var(--blue);text-decoration:none">{d} ▸</a></td>'
                f'{stats_cell}</tr>'
            )
        body = (
            '<div style="font-size:11px;color:var(--ink-dim);margin-bottom:8px">'
            'Each link opens the full dashboard as it was at end of that day '
            '(or latest snapshot, if today). Settled P&amp;L numbers join in '
            'from <code>KXHIGH_settlements_clean</code> the day after.'
            '</div>'
            '<table class="detail">'
            '<tr><th>date</th><th>markets</th><th>cost</th><th>P&amp;L</th>'
            '<th>ROI</th><th>wins</th></tr>'
            + "".join(rows) + "</table>"
        )

    html = (
        f'<div class="modal-bg" id="modal-history">'
        f'<div class="modal" onclick="event.stopPropagation()">'
        f'<h2>Past days · {len(archive_dates)} archives '
        f'<button class="close" onclick="closeDetail(\'history\')">close</button></h2>'
        f'{body}</div></div>'
    )

    return html, ""


def _render_modals(
    cards: list[dict],
    data: dict,
    sparkline_data: dict[str, list[dict]],
) -> tuple[str, str]:
    """Build per-city detail modals plus the JS that lazy-inits their charts.

    Returns (html, js). HTML is appended to <body>; JS goes after the page's
    main sparkline JS so chart objects are available.
    """
    snapshot = data["snapshot"]
    history = data["forecast_history"]
    nws_obs = data.get("nws_obs", pd.DataFrame())
    orders_detail = data.get("orders_detail", pd.DataFrame())
    forecast_peak = data.get("forecast_peak", pd.DataFrame())
    nws_fcst_full = data.get("nws_fcst_full", pd.DataFrame())

    # Bucket trajectory rows by (city, forecast_date) so each city modal can
    # plot the per-NWS-issue spaghetti for its own settlement day. SQL has
    # already deduped to one row per (city, forecast_update_ts, period_start)
    # so the polls→issues collapse is done. We just normalize timestamps
    # and group by city for fast modal access.
    fcst_full_by_city: dict[str, pd.DataFrame] = {}
    if not nws_fcst_full.empty:
        ff = nws_fcst_full.copy()
        ff["polled_at"] = pd.to_datetime(ff["polled_at"], utc=True)
        ff["forecast_update_ts"] = pd.to_datetime(ff["forecast_update_ts"], utc=True)
        ff["period_start"] = pd.to_datetime(ff["period_start"], utc=True)
        ff["period_start_et"] = ff["period_start"].dt.tz_convert("America/New_York")
        ff["period_date_et"] = ff["period_start_et"].dt.date.astype(str)
        for ck, g in ff.groupby("city_abv"):
            fcst_full_by_city[ck] = g

    # Index predicted-peak rows by city_abv (= card city_key) so the modal
    # can show "forecast peak: 87° at 3 PM ET" in the KV strip.
    forecast_peak_by_city: dict[str, dict] = {}
    if not forecast_peak.empty:
        for _, r in forecast_peak.iterrows():
            forecast_peak_by_city[r["city_abv"]] = {
                "hour": r["peak_hour"],
                "temp": float(r["peak_temp"]) if pd.notna(r["peak_temp"]) else None,
            }

    # Index helpers
    snap_by_ticker: dict[str, pd.Series] = {}
    if not snapshot.empty:
        for _, r in snapshot.iterrows():
            snap_by_ticker[r["market_ticker"]] = r

    history_by_city: dict[str, pd.DataFrame] = {}
    if not history.empty:
        for city_name, g in history.groupby("city"):
            history_by_city[city_name] = g.sort_values("run_date")

    obs_by_city: dict[str, pd.DataFrame] = {}
    if not nws_obs.empty:
        for ck, g in nws_obs.groupby("city_abv"):
            obs_by_city[ck] = g.sort_values("observation_time")

    orders_by_city: dict[str, pd.DataFrame] = {}
    if not orders_detail.empty:
        # orders.city_abv may be None; key off market_ticker instead so we get
        # exact matching to the dashboard's city_key.
        od = orders_detail.copy()
        od["city_key"] = od["market_ticker"].map(_city_key_for_ticker)
        for ck, g in od.groupby("city_key"):
            if ck is not None:
                orders_by_city[ck] = g.sort_values("created_at", ascending=False)

    modal_blocks: list[str] = []
    modal_js_parts: list[str] = []

    # Global helpers for the hourly-trajectory chart's legend hover-to-
    # highlight behavior. Stash original color/width once on the dataset
    # so we can restore on leave; on hover, dim every other line to a
    # faint gray so the focused issue pops. Defined once at the top of
    # the modal JS block; per-chart options reference window.kxTrajLegendHover
    # / kxTrajLegendLeave.
    modal_js_parts.append(
        "window.kxTrajLegendHover = function(event, legendItem, legend) {"
        "  var ci = legend.chart;"
        "  var idx = legendItem.datasetIndex;"
        "  ci.data.datasets.forEach(function(ds, i) {"
        "    if (ds._origColor === undefined) {"
        "      ds._origColor = ds.borderColor;"
        "      ds._origWidth = ds.borderWidth;"
        "    }"
        "    if (i === idx) {"
        "      ds.borderColor = ds._origColor;"
        "      ds.borderWidth = (ds._origWidth || 1.5) + 1.5;"
        "    } else {"
        "      ds.borderColor = 'rgba(120,120,120,0.10)';"
        "      ds.borderWidth = 1;"
        "    }"
        "  });"
        "  ci.update('none');"
        "};"
        "window.kxTrajLegendLeave = function(event, legendItem, legend) {"
        "  var ci = legend.chart;"
        "  ci.data.datasets.forEach(function(ds) {"
        "    if (ds._origColor !== undefined) {"
        "      ds.borderColor = ds._origColor;"
        "      ds.borderWidth = ds._origWidth;"
        "    }"
        "  });"
        "  ci.update('none');"
        "};"
    )

    for c in cards:
        city = c["city_key"]
        meta = CITIES.get(city, {})
        modal_id = f"modal-{city}"

        # ── A. KV strip ─────────────────────────────────────────────
        n_pos = len(c["positions"])
        peak = forecast_peak_by_city.get(city)
        if peak and peak.get("temp") is not None:
            # %I gives 12-hour with leading zero; strip it manually since
            # %-I isn't supported on Windows. Result like "3 PM" or "12 PM".
            hour_str = _fmt_et(peak["hour"], fmt="%I %p")
            if hour_str.startswith("0"):
                hour_str = hour_str[1:]
            peak_str = f'{peak["temp"]:.0f}° at {hour_str}'
        else:
            peak_str = "—"
        kv_html = (
            f'<div class="kv">'
            f'<div><div class="k">ICAO</div><div class="v">{c["icao"]}</div></div>'
            f'<div><div class="k">Forecast date</div><div class="v">{c["forecast_date"]}</div></div>'
            f'<div><div class="k">Last snapshot (ET)</div><div class="v" style="font-size:12px">{_fmt_et(c["run_date"])}</div></div>'
            f'<div><div class="k">Last orderbook (ET)</div><div class="v" style="font-size:12px">{_fmt_et(c["polled_at"])}</div></div>'
            f'<div><div class="k">Forecast μ ± σ</div><div class="v">{(f"{c['forecast_avg']:.1f}° ±{c['forecast_std'] or 0:.1f}" if c["forecast_avg"] is not None else "—")}</div></div>'
            # Two distinct NWS products — daily forecast (curated by WFO,
            # shown as "NWS X" in card header + folded into forecast_avg)
            # vs the hourly grid peak (auto-derived from NDFD model). Label
            # explicitly so a 2°F divergence between them doesn't look
            # like a bug. See alert (5) which flags large gaps.
            f'<div><div class="k">NWS hourly grid peak (ET)</div><div class="v" style="font-size:13px">{peak_str}</div></div>'
            f'<div><div class="k">High so far</div><div class="v">{(f"{c['peak_temp']:.0f}°" if c["peak_temp"] is not None else (f"{c['observed_temp']:.0f}°" if c["observed_temp"] is not None else "—"))}</div></div>'
            f'<div><div class="k">Cost · now</div><div class="v">${c["cost_basis"]:,.0f} · ${c["exposure"]:,.0f}</div></div>'
            f'<div><div class="k">Positions</div><div class="v">{n_pos}</div></div>'
            f'</div>'
        )

        # Per-arm P&L block — cost (entry $) vs now (live MTM value) makes
        # cost reconcile with the top-strip total: control.cost +
        # treatment.cost = total cost. live P&L = (now − cost − fees) +
        # realized.
        arm_pnl_block = ""
        if c.get("arm_pnl"):
            order = sorted(c["arm_pnl"].keys(), key=lambda k: (0 if k == "control" else 1 if k == "treatment" else 2, k))
            rows = []
            for arm in order:
                t = c["arm_pnl"][arm]
                pnl = t["open_pnl"] + t["realized"]
                cls = "pos" if pnl > 0.01 else "neg" if pnl < -0.01 else ""
                rows.append(
                    f'<tr><td>{arm}</td><td>{t["n"]}</td>'
                    f'<td>${t["cost"]:,.0f}</td>'
                    f'<td>${t["now"]:,.0f}</td>'
                    f'<td class="{cls}">${pnl:+,.0f}</td>'
                    f'<td>${t["open_pnl"]:+,.0f}</td>'
                    f'<td>${t["realized"]:+,.0f}</td></tr>'
                )
            arm_pnl_block = (
                '<h4>P&amp;L by A/B arm</h4>'
                '<table class="detail">'
                '<tr><th>arm</th><th>positions</th><th>cost</th><th>now</th>'
                '<th>live P&amp;L</th><th>open</th><th>realized</th></tr>'
                + "".join(rows) + "</table>"
            )

        # ── B. Forecast trend chart (avg + 3 sources) ────────────────
        fcst_chart = ""
        hist = history_by_city.get(c["city_name"])
        fcst_series_avg, fcst_series_nws, fcst_series_accu, fcst_series_wu = [], [], [], []
        fcst_labels = []
        if hist is not None and len(hist) >= 2:
            for _, r in hist.iterrows():
                fcst_labels.append(_fmt_et(r["run_date"], fmt="%m-%d %H:%M"))
                fcst_series_avg.append(float(r["forecast_avg"]) if pd.notna(r["forecast_avg"]) else None)
                fcst_series_nws.append(float(r["nws"]) if pd.notna(r["nws"]) else None)
                fcst_series_accu.append(float(r["accuweather"]) if pd.notna(r["accuweather"]) else None)
                fcst_series_wu.append(float(r["weather_underground"]) if pd.notna(r["weather_underground"]) else None)
            fcst_chart = (
                f'<h4>Forecast over last {len(fcst_labels)} runs (ET)</h4>'
                f'<div class="chart-wrap"><canvas id="ch-fcst-{city}"></canvas></div>'
            )

        # ── F. Hourly forecast trajectory (last 24h of NWS issues) ──
        # One line per *distinct NWS forecast issue* (deduped in SQL by
        # forecast_update_ts). NWS only refreshes every ~6h, so polling
        # more often produces identical curves; we collapse those into
        # one line. Newest issue renders bold green; older issues fade
        # with age. Reveals late-breaking forecast revisions for the
        # city's settlement day.
        traj_chart = ""
        traj_issues: list[dict] = []  # serialized below for Chart.js
        x_labels: list[str] = []
        ff_city = fcst_full_by_city.get(city)
        if ff_city is not None and not ff_city.empty and c.get("forecast_date"):
            ff_day = ff_city[ff_city["period_date_et"] == c["forecast_date"]].copy()
            if not ff_day.empty:
                # Build canonical x-axis = union of all hours seen across
                # any issue. NWS forecasts shrink as the day progresses
                # (already-passed hours drop off), so newer issues have
                # fewer hours than older ones — using the union prevents
                # truncation of older curves at their morning end.
                all_hours = (
                    ff_day["period_start_et"]
                    .drop_duplicates()
                    .sort_values()
                    .tolist()
                )
                x_labels = [t.strftime("%H:%M") for t in all_hours]

                issues_sorted = sorted(ff_day["forecast_update_ts"].unique())
                latest_issue = issues_sorted[-1]

                for ut in issues_sorted:
                    sub = ff_day[ff_day["forecast_update_ts"] == ut].sort_values("period_start_et")
                    age_h = (latest_issue - ut).total_seconds() / 3600.0
                    is_latest = (ut == latest_issue)
                    # Align this issue's values to the canonical x-axis
                    # (missing hours render as nulls; spanGaps draws over).
                    v_by_label = {
                        t.strftime("%H:%M"): (float(v) if pd.notna(v) else None)
                        for t, v in zip(sub["period_start_et"], sub["temperature_f"])
                    }
                    aligned = [v_by_label.get(lbl) for lbl in x_labels]
                    first_seen = sub["polled_at"].min()
                    if is_latest:
                        color = "#2ecc71"
                        width = 2.5
                    else:
                        # Linear fade over 24h; older = lighter.
                        alpha = max(0.15, min(0.85, 1.0 - age_h / 24.0))
                        color = f"rgba(74,163,255,{alpha:.2f})"
                        width = 1.5
                    traj_issues.append({
                        # Compact label — NWS's forecast_update_ts in ET
                        # (when NWS refreshed this grid's forecast, NOT our
                        # poll time). Bold green color signals the latest
                        # issue without needing extra text.
                        "label": _fmt_et(ut, fmt="%m-%d %H:%M") + " ET",
                        "values": aligned,
                        "color": color,
                        "width": width,
                    })
                traj_chart = (
                    f'<h4>Hourly forecast trajectory · {c["forecast_date"]} '
                    f'({len(traj_issues)} NWS issues, last 24h)</h4>'
                    f'<div style="font-size:10px;color:var(--ink-dim);margin-bottom:4px">'
                    f'One line per distinct NWS forecast update; '
                    f'<b>legend timestamps are when NWS refreshed the forecast</b> '
                    f'(<code>forecast_update_ts</code>, ET) — not when we polled. '
                    f'We poll every ~20 min but NWS refreshes only ~every 6h, so '
                    f'most polls return identical curves and collapse into one line. '
                    f'Newest issue = bold green; older issues fade. Divergence between '
                    f'the newest line and older ones = a late-breaking forecast revision.</div>'
                    f'<div class="chart-wrap"><canvas id="ch-traj-{city}"></canvas></div>'
                )

        # ── C. NWS hourly obs (last 24h) ────────────────────────────
        obs_chart = ""
        obs = obs_by_city.get(city)
        obs_labels, obs_values = [], []
        if obs is not None and len(obs) >= 2:
            for _, r in obs.iterrows():
                obs_labels.append(_fmt_et(r["observation_time"], fmt="%m-%d %H:%M"))
                obs_values.append(float(r["temperature_f"]))
            obs_chart = (
                f'<h4>NWS hourly observations (last 12h, station {c["icao"]}, ET)</h4>'
                f'<div class="chart-wrap"><canvas id="ch-obs-{city}"></canvas></div>'
            )
        else:
            obs_chart = (
                f'<h4>NWS hourly observations</h4>'
                f'<div class="muted" style="font-size:11px">'
                f'No obs yet — table populates after the next orderbook-logger run.</div>'
            )

        # ── D. Full bucket table ────────────────────────────────────
        bucket_rows = []
        for b in c["buckets"]:
            ticker = b["ticker"]
            snap_row = snap_by_ticker.get(ticker)
            # Best YES bid / best YES offer in cents (live orderbook). bid is
            # what the market will pay you for a YES contract; offer is what
            # you'd pay to buy one. Mid ≈ (bid+offer)/2 → market_p.
            if b["yes_bid"] is not None and b["yes_offer"] is not None:
                yes_mid = f'{int(b["yes_bid"])}¢ / {int(b["yes_offer"])}¢'
            elif b["yes_bid"] is not None:
                yes_mid = f'{int(b["yes_bid"])}¢ / —'
            elif b["yes_offer"] is not None:
                yes_mid = f'— / {int(b["yes_offer"])}¢'
            else:
                yes_mid = "—"
            model_p = "—" if b["model_p"] is None else f'{b["model_p"]*100:.0f}%'
            market_p = "—" if b["market_p"] is None else f'{b["market_p"]*100:.0f}%'
            edge = "—" if b["edge"] is None else f'{b["edge"]*100:+.0f}'
            edge_cls = "" if b["edge"] is None else ("pos" if b["edge"] > 0.02 else "neg" if b["edge"] < -0.02 else "")
            pos = b["position"]
            pos_str = "—" if pos == 0 else f'{pos:+d}'
            fair_no = "—"
            if snap_row is not None and pd.notna(snap_row.get("fair_no_price")):
                fair_no = f'{float(snap_row["fair_no_price"]):.0f}'
            # Live mark-to-market: cost was paid at entry, "now" is what
            # we'd realize closing at the current mid. P&L = now − cost − fees.
            cost_str = f'${b["exposure_dollars"]:.0f}' if b.get("exposure_dollars") else "—"
            now_str = f'${b["current_value"]:.0f}' if b.get("current_value") is not None else "—"
            pnl_val = b.get("open_pnl")
            if pnl_val is None or b["position"] == 0:
                pnl_str = "—"; pnl_cls = ""
            else:
                pnl_str = f'${pnl_val:+,.0f}'
                pnl_cls = "pos" if pnl_val > 0.01 else "neg" if pnl_val < -0.01 else ""

            # Position YES%: the YES probability implied by the price we
            # actually paid at entry. Tells the user "what YES prob did I
            # bet was wrong?". For NO holdings (pos < 0): avg NO entry =
            # cost / |contracts|, so entry YES = 1 − avg_NO. For YES
            # holdings: cost / contracts is directly the YES price paid.
            pos_qty = b.get("position") or 0
            cost_dollars = b.get("exposure_dollars")
            if pos_qty and cost_dollars:
                avg_side_price = cost_dollars / abs(pos_qty)
                pos_yes_p = (1.0 - avg_side_price) if pos_qty < 0 else avg_side_price
                pos_yes_str = f'{pos_yes_p * 100:.0f}%'
            else:
                pos_yes_str = "—"

            bucket_rows.append(
                f'<tr><td>{b["label"]}</td><td style="font-size:10px;color:var(--ink-dim)">{ticker}</td>'
                f'<td>{model_p}</td><td>{market_p}</td>'
                f'<td>{pos_yes_str}</td>'
                f'<td class="{edge_cls}">{edge}</td>'
                f'<td>{yes_mid}</td><td>{fair_no}</td>'
                f'<td>{pos_str}</td>'
                f'<td>{cost_str}</td><td>{now_str}</td>'
                f'<td class="{pnl_cls}">{pnl_str}</td></tr>'
            )
        bucket_table = (
            '<h4>Buckets</h4>'
            '<div style="font-size:10px;color:var(--ink-dim);margin-bottom:4px">'
            'model = our YES probability · market = orderbook-implied YES probability · '
            'position YES% = YES probability implied by the price we paid (1 − avg NO entry for NO holdings) · '
            'edge¢ = (model − market)·100 · YES bid/offer = best buy/sell prices in cents · '
            'fair NO¢ = bot\'s computed fair value for NO · '
            'cost = $ paid at entry · now = live mark-to-market value · '
            'P&amp;L = now − cost − fees'
            '</div>'
            '<table class="detail">'
            '<tr><th>bucket</th><th>ticker</th>'
            '<th>model YES%</th><th>market YES%</th><th>position YES%</th><th>edge¢</th>'
            '<th>YES bid / offer</th><th>fair NO¢</th>'
            '<th>position</th><th>cost</th><th>now</th><th>P&amp;L</th></tr>'
            + "".join(bucket_rows) + "</table>"
        )

        # ── E. Today's orders for this city ─────────────────────────
        orders_html = ""
        od = orders_by_city.get(city)
        if od is not None and len(od):
            order_rows = []
            now_unix = int(datetime.now(timezone.utc).timestamp())
            for _, r in od.head(60).iterrows():
                ts = _fmt_et(r.get("created_at"), fmt="%m-%d %H:%M:%S")
                fair = r.get("fair_no_cents")
                fair_str = f'{float(fair):.0f}' if pd.notna(fair) else "—"
                contracts = int(r.get("contracts") or 0)
                filled = float(r.get("filled_count") or 0)
                exp_ts = int(r.get("expiration_ts") or 0)
                # Status taxonomy:
                #   filled  — fills cover the full order
                #   partial — some fills, but less than ordered size
                #   open    — no fills yet, order still resting (exp in future)
                #   canceled — no fills, expiration_ts has passed (Kalshi
                #              auto-cancels expired resting orders;
                #              equivalent to "didn't get crossed before
                #              the deadline")
                if filled + 0.5 >= contracts and contracts > 0:
                    status = "filled"; status_cls = "pos"
                elif filled > 0:
                    status = f"partial {filled:.0f}/{contracts}"; status_cls = "warn"
                elif exp_ts and exp_ts <= now_unix:
                    status = "canceled"; status_cls = "muted"
                else:
                    status = "open"; status_cls = "warn"
                order_rows.append(
                    f'<tr><td>{ts}</td><td>{r.get("market_ticker")}</td>'
                    f'<td>{contracts}</td>'
                    f'<td>{int(r.get("no_price") or 0)}¢</td>'
                    f'<td>{fair_str}</td><td>{r.get("ab_arm") or ""}</td>'
                    f'<td class="{status_cls}">{status}</td></tr>'
                )
            orders_html = (
                f'<h4>Orders on currently-open markets ({len(od)})</h4>'
                f'<table class="detail">'
                f'<tr><th>created (ET)</th><th>market</th><th>contracts</th>'
                f'<th>no_price</th><th>fair_no¢</th><th>arm</th><th>status</th></tr>'
                + "".join(order_rows) + "</table>"
            )
        else:
            orders_html = '<h4>Orders on currently-open markets</h4><div class="muted" style="font-size:11px">No orders for this city.</div>'

        modal_blocks.append(f"""
<div class="modal-bg" id="{modal_id}">
  <div class="modal" onclick="event.stopPropagation()">
    <h2>{city} · {c['city_name']} <button class="close" onclick="closeDetail('{city}')">close</button></h2>
    {kv_html}
    {arm_pnl_block}
    {fcst_chart}
    {traj_chart}
    {obs_chart}
    {bucket_table}
    {orders_html}
  </div>
</div>
""")

        # ── chart init JS for this city ──
        chart_init = []
        if fcst_labels:
            chart_init.append(
                f"charts.push(new Chart(document.getElementById('ch-fcst-{city}'), {{"
                f"type:'line',data:{{labels:{json.dumps(fcst_labels)},datasets:["
                f"{{label:'avg',data:{json.dumps(fcst_series_avg)},borderColor:'#e6ecff',borderWidth:2,tension:.25,pointRadius:0,fill:false,spanGaps:true}},"
                f"{{label:'NWS',data:{json.dumps(fcst_series_nws)},borderColor:'#4aa3ff',borderWidth:1.5,tension:.25,pointRadius:0,fill:false,spanGaps:true}},"
                f"{{label:'AW',data:{json.dumps(fcst_series_accu)},borderColor:'#b18cff',borderWidth:1.5,tension:.25,pointRadius:0,fill:false,spanGaps:true}},"
                f"{{label:'WU',data:{json.dumps(fcst_series_wu)},borderColor:'#ffb94a',borderWidth:1.5,tension:.25,pointRadius:0,fill:false,spanGaps:true}}"
                f"]}},options:{{animation:false,responsive:true,maintainAspectRatio:false,"
                f"plugins:{{legend:{{display:true,labels:{{color:'#93a2c8',font:{{size:10}},boxWidth:10}}}}}},"
                f"scales:{{x:{{ticks:{{color:'#93a2c8',maxTicksLimit:6,font:{{size:9}}}}}},y:{{ticks:{{color:'#93a2c8',font:{{size:9}}}}}}}}}}}}));"
            )
        if obs_labels:
            chart_init.append(
                f"charts.push(new Chart(document.getElementById('ch-obs-{city}'), {{"
                f"type:'line',data:{{labels:{json.dumps(obs_labels)},datasets:[{{"
                f"label:'temp °F',data:{json.dumps(obs_values)},borderColor:'#2ecc71',borderWidth:2,tension:.25,pointRadius:1,fill:false}}]}},"
                f"options:{{animation:false,responsive:true,maintainAspectRatio:false,"
                f"plugins:{{legend:{{display:false}}}},"
                f"scales:{{x:{{ticks:{{color:'#93a2c8',maxTicksLimit:6,font:{{size:9}}}}}},y:{{ticks:{{color:'#93a2c8',font:{{size:9}}}}}}}}}}}}));"
            )
        # Trajectory chart: x-axis is the union of all period_start hours
        # across issues (built above); each dataset's `data` is already
        # aligned to that axis. Issues that don't extend to a given hour
        # leave gaps via spanGaps.
        if traj_issues and x_labels:
            datasets_js = []
            for ti in traj_issues:
                datasets_js.append(
                    "{"
                    f"label:{json.dumps(ti['label'])},"
                    f"data:{json.dumps(ti['values'])},"
                    f"borderColor:{json.dumps(ti['color'])},"
                    f"borderWidth:{ti['width']},"
                    "tension:.3,pointRadius:0,fill:false,spanGaps:true"
                    "}"
                )
            chart_init.append(
                f"charts.push(new Chart(document.getElementById('ch-traj-{city}'), {{"
                f"type:'line',data:{{labels:{json.dumps(x_labels)},datasets:[{','.join(datasets_js)}]}},"
                f"options:{{animation:false,responsive:true,maintainAspectRatio:false,"
                f"plugins:{{legend:{{display:true,"
                f"labels:{{color:'#93a2c8',font:{{size:10}},boxWidth:10}},"
                # Hover-to-highlight: dim non-hovered datasets, bump the
                # hovered one's width. Helpers are defined globally below.
                f"onHover:window.kxTrajLegendHover,onLeave:window.kxTrajLegendLeave}},"
                f"tooltip:{{mode:'nearest',intersect:false,callbacks:{{title:function(ctx){{return ctx[0].label+' ET';}}}}}}}},"
                f"scales:{{x:{{ticks:{{color:'#93a2c8',maxTicksLimit:8,font:{{size:9}}}}}},"
                f"y:{{ticks:{{color:'#93a2c8',font:{{size:9}}}},title:{{display:true,text:'°F',color:'#93a2c8',font:{{size:9}}}}}}}}}}}}));"
            )
        if chart_init:
            modal_js_parts.append(
                f"window['initModalCharts_{city}'] = function() {{"
                f"const charts = []; "
                + " ".join(chart_init)
                + " return charts; };"
            )

    return ("\n".join(modal_blocks), "<script>\n" + "\n".join(modal_js_parts) + "\n</script>")


PUBLIC_BUCKET = os.environ.get("KXHIGH_DASHBOARD_BUCKET", "kxhigh-live-dashboard-elite446323")
PUBLIC_OBJECT = os.environ.get("KXHIGH_DASHBOARD_OBJECT", "index.html")
PUBLISH_DEFAULT = os.environ.get("KXHIGH_DASHBOARD_PUBLISH", "1") not in ("0", "", "false", "False")


def _publish_to_gcs(html: str) -> str | None:
    """Upload the rendered HTML to the public GCS bucket — as the live
    `index.html` AND as TWO archive copies: today's and yesterday's.

    Why both archive dates: settlements + final CLI for ET-day D land
    several hours AFTER the date rolls to D+1 (Kalshi processes ~7-8 AM
    ET, sometimes later). If we only overwrite archive/{today}.html, the
    archive file for D freezes at midnight ET — *before* the day's
    realized P&L exists. By also overwriting archive/{yesterday}.html on
    every publish, yesterday's archive keeps refreshing for ~24h after
    rollover; settlement lands, the next publish picks it up, the archive
    becomes accurate. Once "yesterday" becomes 2-days-ago it naturally
    stops being touched and freezes for good.

    The dashboard's queries already span [yesterday, today, tomorrow], so
    yesterday's cards still render correctly the day after — paired with
    the per_city_view fallback to settlements_clean for realized P&L on
    settled markets, this delivers an accurate post-settlement archive
    without re-rendering with date-scoped queries.
    """
    if not PUBLISH_DEFAULT:
        return None
    try:
        from google.cloud import storage
    except ImportError:
        print("  google-cloud-storage not installed; skipping publish", flush=True)
        return None
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        et_today = now_et.strftime("%Y-%m-%d")
        et_yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        now_et = datetime.utcnow()
        et_today = now_et.strftime("%Y-%m-%d")
        et_yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        client = storage.Client(project=PROJECT)
        bucket = client.bucket(PUBLIC_BUCKET)
        # Live copy: short cache so changes show up within ~1 min.
        blob_live = bucket.blob(PUBLIC_OBJECT)
        blob_live.cache_control = "public,max-age=60"
        blob_live.upload_from_string(html, content_type="text/html; charset=utf-8")
        # Archive copies: short cache so post-settlement updates land in
        # browsers quickly. Once yesterday becomes 2-days-ago, no more
        # writes happen and the file freezes for good.
        for et_date in (et_today, et_yesterday):
            blob_arch = bucket.blob(f"archive/{et_date}.html")
            blob_arch.cache_control = "public,max-age=300"
            blob_arch.upload_from_string(html, content_type="text/html; charset=utf-8")
        url = f"https://storage.googleapis.com/{PUBLIC_BUCKET}/{PUBLIC_OBJECT}"
        print(f"  published -> {url} (+ archive/{et_today}.html, archive/{et_yesterday}.html)", flush=True)
        return url
    except Exception as e:
        print(f"  publish failed (non-fatal): {e}", flush=True)
        return None


def _list_archive_dates() -> list[str]:
    """Pull the list of existing archive/{YYYY-MM-DD}.html objects from GCS
    so the history modal can render links. Cheap (one ListObjects call,
    typically <50 entries). Returns empty list on any failure.
    """
    try:
        from google.cloud import storage
    except ImportError:
        return []
    try:
        client = storage.Client(project=PROJECT)
        bucket = client.bucket(PUBLIC_BUCKET)
        blobs = list(client.list_blobs(bucket, prefix="archive/"))
        dates: list[str] = []
        for b in blobs:
            # Expect "archive/YYYY-MM-DD.html"
            name = b.name.removeprefix("archive/").removesuffix(".html")
            if len(name) == 10 and name[4] == "-" and name[7] == "-":
                dates.append(name)
        return sorted(dates, reverse=True)
    except Exception as e:
        print(f"  archive list failed: {e}", flush=True)
        return []


def main() -> Path:
    print("loading bigquery state...", flush=True)
    data = load()
    for k, df in data.items():
        print(f"  {k}: {len(df)} rows", flush=True)
    html = render(data)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUTPUT}", flush=True)
    _publish_to_gcs(html)
    return OUTPUT


if __name__ == "__main__":
    main()
