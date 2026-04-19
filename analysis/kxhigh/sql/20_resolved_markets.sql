-- VIEW: kxhigh_resolved_markets (uses _clean views)
-- One row per settled market with "model's call" snapshot + actual outcome.
-- All money in dollars; timestamps as TIMESTAMP; winning_high_temp from
-- between-bucket YES midpoint.

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_resolved_markets` AS
SELECT
  sc.market_ticker,
  sc.event_ticker,
  sc.city_abv,
  mc.city,
  sc.event_date AS forecast_date,
  mc.run_date AS model_call_run_date,
  mc.earliest_run_date,
  mc.snapshots_in_window,
  mc.cutoff_ts,
  mc.low_range,
  mc.high_range,
  (mc.low_range + mc.high_range) / 2.0 AS bucket_midpoint,
  sc.market_kind,
  sc.market_strike,
  -- Forecast from actual bot snapshot (only 19 markets due to logging gap)
  mc.nws, mc.accuweather, mc.weather_underground,
  mc.forecast_avg AS snap_forecast_avg,
  mc.forecast_std AS snap_forecast_std,
  mc.forecast_range AS snap_forecast_range,
  mc.midnight_temperature,
  -- Historical forecast backfill from Open-Meteo (GFS + ECMWF ensemble)
  hf.forecast_high_avg   AS backfill_forecast_avg,
  hf.forecast_high_std   AS backfill_forecast_std,
  hf.forecast_high_range AS backfill_forecast_range,
  hf.high_f_gfs_seamless AS backfill_forecast_gfs,
  hf.high_f_ecmwf_ifs025 AS backfill_forecast_ecmwf,
  -- "Best" forecast: real snapshot if present, else Open-Meteo backfill
  COALESCE(mc.forecast_avg, hf.forecast_high_avg) AS forecast_avg,
  COALESCE(mc.forecast_std, hf.forecast_high_std) AS forecast_std,
  COALESCE(mc.forecast_range, hf.forecast_high_range) AS forecast_range,
  CASE
    WHEN mc.forecast_avg IS NOT NULL THEN "snapshot"
    WHEN hf.forecast_high_avg IS NOT NULL THEN "backfill_open_meteo"
    ELSE NULL
  END AS forecast_source,
  -- Model prediction from snapshot (only populated for 19 markets)
  mc.yes_probability,
  mc.fair_no_price,
  mc.earliest_yes_prob,
  mc.no_highest_bid AS snap_no_bid_cents,
  mc.no_lowest_offer AS snap_no_offer_cents,
  mc.hi_no_price AS snap_hi_no_cents,
  mc.position AS snap_position,
  mc.historical_var,
  -- Settlement economics (all dollars, from _clean)
  sc.result,
  sc.outcome_yes,
  sc.revenue_dollars,
  sc.total_cost_dollars,
  sc.fee_cost_dollars,
  sc.pnl_dollars AS pnl,
  sc.net_position AS settled_net_position,
  sc.position_yes,
  sc.position_no,
  sc.num_fills,
  sc.settled_ts,
  -- Realized high temp (per event)
  sc.winning_high_temp AS actual_high_estimate,
  sc.winning_temp_source,
  -- Error metrics (uses best forecast)
  COALESCE(mc.forecast_avg, hf.forecast_high_avg) - sc.winning_high_temp AS forecast_error,
  ABS(COALESCE(mc.forecast_avg, hf.forecast_high_avg) - sc.winning_high_temp) AS forecast_abs_error,
  -- Market-implied YES prob from snapshot orderbook midpoint
  CASE
    WHEN mc.no_highest_bid IS NOT NULL AND mc.no_lowest_offer IS NOT NULL THEN
      1 - ((mc.no_highest_bid + mc.no_lowest_offer) / 2.0) / 100.0
    WHEN mc.no_highest_bid IS NOT NULL THEN
      1 - mc.no_highest_bid / 100.0
    ELSE NULL
  END AS market_implied_yes_prob_snap
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements_clean` sc
LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_model_call_snapshots` mc USING (market_ticker)
LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_historical_forecasts` hf
  ON sc.city_abv = hf.city_abv AND sc.event_date = hf.event_date;
