-- VIEW: KXHIGH_fills_enriched (uses _clean views)
-- Every fill annotated with market outcome and correctly-oriented per-fill P&L.
--
-- For a NO-side buy at price_paid_dollars P:
--   outcome=NO  (bot wins):  P&L = filled_count * (1 - P) - fee
--   outcome=YES (bot loses): P&L = filled_count * (-P)    - fee

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_fills_enriched` AS
WITH
outcomes AS (
  SELECT market_ticker, result, outcome_yes
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements_clean`
),
fills_with_snap AS (
  SELECT
    f.*,
    ARRAY_AGG(
      STRUCT(s.yes_probability, s.fair_no_price, s.forecast_avg,
             s.forecast_std, s.forecast_range, s.no_highest_bid,
             s.no_lowest_offer, s.run_date)
      ORDER BY s.run_date DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS snap
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills_clean` f
  LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot` s
    ON f.market_ticker = s.market_ticker
    AND s.run_date <= f.fill_ts
  GROUP BY
    f.fill_id, f.trade_id, f.order_id, f.client_order_id, f.market_ticker,
    f.event_ticker, f.city_abv, f.event_date, f.market_kind, f.market_strike,
    f.side, f.action, f.is_taker, f.filled_count,
    f.price_paid_dollars, f.complement_price_dollars,
    f.raw_no_price_dollars, f.raw_yes_price_dollars, f.raw_fill_price_dollars,
    f.order_limit_no_price_dollars, f.order_contracts, f.fee_cost_dollars,
    f.fill_cost_dollars, f.fill_ts, f.created_ts, f.pulled_ts
)
SELECT
  f.fill_id,
  f.trade_id,
  f.order_id,
  f.client_order_id,
  f.market_ticker,
  f.event_ticker,
  f.city_abv,
  f.event_date,
  f.market_kind,
  f.market_strike,
  f.side,
  f.is_taker,
  f.filled_count,
  f.price_paid_dollars,
  f.fill_cost_dollars,
  f.fee_cost_dollars,
  f.fill_ts,
  -- Correctly-oriented per-fill P&L (NULL if unsettled)
  CASE
    WHEN o.outcome_yes IS NULL THEN NULL
    WHEN f.side = "no"  THEN f.filled_count * ((1 - o.outcome_yes) - f.price_paid_dollars) - f.fee_cost_dollars
    WHEN f.side = "yes" THEN f.filled_count * (o.outcome_yes       - f.price_paid_dollars) - f.fee_cost_dollars
    ELSE NULL
  END AS realized_pnl_per_fill,
  -- Model context at fill time
  f.snap.yes_probability AS model_yes_prob_at_fill,
  f.snap.forecast_avg AS forecast_avg_at_fill,
  f.snap.forecast_std AS forecast_std_at_fill,
  f.snap.run_date AS snap_run_date,
  -- Market-implied YES prob from the actual trade price
  CASE
    WHEN f.side = "no"  THEN 1 - f.price_paid_dollars
    WHEN f.side = "yes" THEN     f.price_paid_dollars
  END AS trade_implied_yes_prob,
  -- Model edge: (model's prob of winning side) - (cost per contract)
  CASE
    WHEN f.side = "no"  THEN (1 - f.snap.yes_probability) - f.price_paid_dollars
    WHEN f.side = "yes" THEN     f.snap.yes_probability   - f.price_paid_dollars
  END AS model_edge_at_fill,
  o.result AS settled_result,
  o.outcome_yes AS settled_outcome_yes
FROM fills_with_snap f
LEFT JOIN outcomes o USING (market_ticker);
