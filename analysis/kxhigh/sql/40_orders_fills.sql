-- VIEW: KXHIGH_orders_fills (uses _clean views)
-- Fill-rate and slippage per order.

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_orders_fills` AS
WITH
joined AS (
  SELECT
    o.client_order_id,
    o.kalshi_order_id,
    o.city_abv,
    o.event_date,
    o.run_ts,
    o.market_ticker,
    o.market_kind,
    o.market_strike,
    o.ordered_contracts,
    o.no_price_dollars AS ordered_no_price_dollars,
    o.expiration_ts,
    o.created_ts,
    COALESCE(SUM(f.filled_count), 0) AS filled_contracts,
    COUNT(f.fill_id) AS n_fills,
    AVG(f.price_paid_dollars) AS avg_fill_price_dollars,
    MIN(f.fill_ts) AS first_fill_ts,
    MAX(f.fill_ts) AS last_fill_ts
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders_clean` o
  LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_fills_clean` f
    ON (f.client_order_id = o.client_order_id OR f.order_id = o.kalshi_order_id)
  GROUP BY
    o.client_order_id, o.kalshi_order_id, o.city_abv, o.event_date, o.run_ts,
    o.market_ticker, o.market_kind, o.market_strike,
    o.ordered_contracts, o.no_price_dollars, o.expiration_ts, o.created_ts
)
SELECT
  *,
  filled_contracts / NULLIF(ordered_contracts, 0) AS fill_rate,
  filled_contracts > 0 AS any_fill,
  -- Slippage: avg fill price vs ordered limit price (positive = better)
  CASE
    WHEN filled_contracts > 0
      THEN ordered_no_price_dollars - avg_fill_price_dollars
  END AS slippage_dollars
FROM joined;
