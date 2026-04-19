-- VIEW: kxhigh_resolved_markets
-- One row per settled market: joins the "model's call" snapshot, settlement
-- economics, and (where derivable) the realized high temp.
--
-- Conventions confirmed in sanity checks:
--   settlements.revenue = CENTS; pnl, total_cost, fee_cost = DOLLARS
--   pnl = revenue/100 - total_cost - fee_cost (validated 364/369)
--   market_snapshot.fair_no_price = DOLLARS (0-1)
--   market_snapshot.no_highest_bid / no_lowest_offer = CENTS (1-99)
--   market_snapshot.yes_probability = PROBABILITY (0-1)
--   Outcome: settlements.result in ("YES","NO"). YES iff actual high in [low_range, high_range].
--
-- For forecast-vs-actual analysis we *infer* actual_high_estimate by finding the
-- single between-bucket (B ticker) per (city, event_date) that resolved YES,
-- and taking the midpoint of its [low_range, high_range].

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_resolved_markets` AS
WITH
-- 1. Derive actual_high estimate per (city, event_date) from the winning B-bucket
winning_buckets AS (
  SELECT
    s.city,
    s.forecast_date AS event_date,
    -- bucket midpoint = strike (e.g., B84.5 -> [83.5,85.5], mid=84.5)
    (s.low_range + s.high_range) / 2.0 AS actual_high_estimate,
    s.low_range AS winning_low,
    s.high_range AS winning_high
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_model_call_snapshots` s
  JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` stl USING (market_ticker)
  WHERE UPPER(stl.result) = "YES"
    AND REGEXP_CONTAINS(s.market_ticker, r"-B[0-9.]+$")
),
settlement_econ AS (
  SELECT
    market_ticker,
    UPPER(result) AS result,
    CASE WHEN UPPER(result) = "YES" THEN 1 ELSE 0 END AS outcome_yes,
    revenue / 100.0 AS revenue_dollars,
    total_cost,
    fee_cost,
    pnl,
    net_position,
    position_yes,
    position_no,
    num_fills,
    SAFE.PARSE_TIMESTAMP("%Y-%m-%dT%H:%M:%SZ", settled_time) AS settled_ts
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
)
SELECT
  mc.market_ticker,
  mc.event_ticker,
  mc.city,
  mc.city_abv,
  mc.forecast_date,
  mc.run_date AS model_call_run_date,
  mc.earliest_run_date,
  mc.snapshots_in_window,
  mc.cutoff_ts,
  -- Market strike / resolution band
  mc.low_range,
  mc.high_range,
  (mc.low_range + mc.high_range) / 2.0 AS bucket_midpoint,
  CASE
    WHEN REGEXP_CONTAINS(mc.market_ticker, r"-B[0-9.]+$") THEN "between"
    WHEN REGEXP_CONTAINS(mc.market_ticker, r"-T[0-9.]+$") THEN "tail"
    ELSE "unknown"
  END AS market_kind,
  -- Forecast inputs
  mc.nws, mc.accuweather, mc.weather_underground,
  mc.forecast_avg, mc.forecast_std, mc.forecast_range,
  mc.forecast_avg_recomputed,
  mc.forecast_std_recomputed,
  mc.forecast_n_sources,
  mc.midnight_temperature,
  -- Model prediction and market context
  mc.yes_probability,
  mc.fair_no_price,  -- dollars 0-1
  mc.earliest_yes_prob,
  mc.no_highest_bid AS snap_no_bid_cents,
  mc.no_lowest_offer AS snap_no_offer_cents,
  mc.hi_no_price AS snap_hi_no_cents,
  mc.position AS snap_position,
  mc.historical_var,
  -- Settlement economics
  se.result,
  se.outcome_yes,
  se.revenue_dollars,
  se.total_cost,
  se.fee_cost,
  se.pnl,
  se.net_position AS settled_net_position,
  se.position_yes,
  se.position_no,
  se.num_fills,
  se.settled_ts,
  -- Realized high estimate (per city, event_date)
  wb.actual_high_estimate,
  wb.winning_low,
  wb.winning_high,
  -- Error metrics (NULL for markets without snapshot)
  mc.forecast_avg - wb.actual_high_estimate AS forecast_error,
  ABS(mc.forecast_avg - wb.actual_high_estimate) AS forecast_abs_error,
  -- Market-implied YES probability from snapshot orderbook midpoint (cents -> prob)
  -- no_price_mid = (no_highest_bid + no_lowest_offer) / 2; implied YES = 1 - no_price/100
  CASE
    WHEN mc.no_highest_bid IS NOT NULL AND mc.no_lowest_offer IS NOT NULL THEN
      1 - ((mc.no_highest_bid + mc.no_lowest_offer) / 2.0) / 100.0
    WHEN mc.no_highest_bid IS NOT NULL THEN
      1 - mc.no_highest_bid / 100.0
    ELSE NULL
  END AS market_implied_yes_prob_snap
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_model_call_snapshots` mc
INNER JOIN settlement_econ se USING (market_ticker)
LEFT JOIN winning_buckets wb
  ON mc.city = wb.city AND mc.forecast_date = wb.event_date;
