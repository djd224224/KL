-- VIEW: kxhigh_fills_enriched
-- Every fill annotated with market outcome, per-fill P&L, per-fill edge vs
-- market-implied (from snapshot) and model.
--
-- Bot trades NO exclusively (side = 'no'). Per-fill P&L for a NO fill:
--   If market settled NO (outcome_yes=0): P&L = filled_count * (100 - no_fill_price)/100 - fee
--   If market settled YES (outcome_yes=1): P&L = filled_count * (-no_fill_price)/100 - fee
-- i.e., NO pays $1 if result=NO, $0 if YES.

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_fills_enriched` AS
WITH
outcomes AS (
  SELECT
    market_ticker,
    UPPER(result) AS result,
    CASE WHEN UPPER(result) = "YES" THEN 1 ELSE 0 END AS outcome_yes
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
),
-- Snapshot context for each fill: pick latest snapshot before the fill timestamp
fills_ts AS (
  SELECT
    *,
    TIMESTAMP_SECONDS(SAFE_CAST(ts AS INT64)) AS fill_ts
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
),
fills_with_snap AS (
  SELECT
    f.*,
    ARRAY_AGG(STRUCT(s.yes_probability, s.fair_no_price, s.forecast_avg,
                     s.forecast_std, s.forecast_range, s.no_highest_bid,
                     s.no_lowest_offer, s.run_date)
              ORDER BY s.run_date DESC LIMIT 1)[SAFE_OFFSET(0)] AS snap
  FROM fills_ts f
  LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot` s
    ON f.market_ticker = s.market_ticker
    AND s.run_date <= f.fill_ts
  GROUP BY
    f.action, f.count_fp, f.created_time, f.fee_cost, f.fill_id, f.is_taker,
    f.market_ticker, f.no_price_dollars, f.order_id, f.side,
    f.subaccount_number, f.trade_id, f.ts, f.yes_price_dollars, f.pulled_at,
    f.filled_count, f.yes_price, f.no_price, f.fill_price, f.client_order_id,
    f.city, f.city_abv, f.order_no_price, f.order_contracts, f.forecast_date,
    f.series_city, f.city_code, f.city_name, f.date_code, f.event_date,
    f.event_ticker, f.market_code, f.market_type, f.temp_value, f.fill_ts
)
SELECT
  f.fill_id,
  f.trade_id,
  f.order_id,
  f.client_order_id,
  f.market_ticker,
  f.event_ticker,
  f.city,
  f.city_abv,
  f.forecast_date,
  f.fill_ts,
  f.side,
  f.action,
  f.is_taker,
  f.filled_count,
  f.fill_price,   -- cents on the side filled (bot only trades NO)
  f.no_price,
  f.yes_price,
  f.order_no_price AS order_limit_no_price,
  SAFE_CAST(f.fee_cost AS FLOAT64) AS fee_cost_dollars,
  -- Realized per-fill economics (NULL if market unsettled)
  CASE
    WHEN o.outcome_yes IS NULL THEN NULL
    WHEN LOWER(f.side) = "no" THEN
      f.filled_count * ((1 - o.outcome_yes) - f.no_price/100.0)
      - SAFE_CAST(f.fee_cost AS FLOAT64)
    WHEN LOWER(f.side) = "yes" THEN
      f.filled_count * (o.outcome_yes - f.yes_price/100.0)
      - SAFE_CAST(f.fee_cost AS FLOAT64)
    ELSE NULL
  END AS realized_pnl_per_fill,
  -- Edge at fill time: model probability vs market-implied
  f.snap.yes_probability AS model_yes_prob_at_fill,
  f.snap.forecast_avg AS forecast_avg_at_fill,
  f.snap.forecast_std AS forecast_std_at_fill,
  f.snap.run_date AS snap_run_date,
  -- Market-implied YES probability from the trade price
  CASE LOWER(f.side)
    WHEN "no" THEN 1 - f.no_price/100.0
    WHEN "yes" THEN f.yes_price/100.0
  END AS trade_implied_yes_prob,
  -- Edge: how much more likely does the model think the winning side is vs the trade price
  CASE LOWER(f.side)
    -- Bot bought NO at price p → model sees (1-yes_prob) win chance; paying no_price
    WHEN "no" THEN (1 - f.snap.yes_probability) - (f.no_price/100.0)
    WHEN "yes" THEN f.snap.yes_probability - (f.yes_price/100.0)
  END AS model_edge_at_fill,
  -- Outcome
  o.result AS settled_result,
  o.outcome_yes AS settled_outcome_yes
FROM fills_with_snap f
LEFT JOIN outcomes o USING (market_ticker);
