-- VIEW: kxhigh_resolved_markets (uses _clean views)
-- One row per settled market with "model's call" snapshot + actual outcome.
-- All money in dollars; timestamps as TIMESTAMP; winning_high_temp from
-- between-bucket YES midpoint.

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_resolved_markets` AS
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
  sc.market_kind,
  sc.market_strike,
  -- Forecast inputs
  mc.nws, mc.accuweather, mc.weather_underground,
  mc.forecast_avg, mc.forecast_std, mc.forecast_range,
  mc.forecast_avg_recomputed,
  mc.forecast_std_recomputed,
  mc.forecast_n_sources,
  mc.midnight_temperature,
  -- Model prediction and market context (snapshot-time)
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
  -- Error metrics
  mc.forecast_avg - sc.winning_high_temp AS forecast_error,
  ABS(mc.forecast_avg - sc.winning_high_temp) AS forecast_abs_error,
  -- Market-implied YES prob from snapshot orderbook midpoint (cents -> prob)
  CASE
    WHEN mc.no_highest_bid IS NOT NULL AND mc.no_lowest_offer IS NOT NULL THEN
      1 - ((mc.no_highest_bid + mc.no_lowest_offer) / 2.0) / 100.0
    WHEN mc.no_highest_bid IS NOT NULL THEN
      1 - mc.no_highest_bid / 100.0
    ELSE NULL
  END AS market_implied_yes_prob_snap
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_model_call_snapshots` mc
INNER JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_settlements_clean` sc USING (market_ticker);
