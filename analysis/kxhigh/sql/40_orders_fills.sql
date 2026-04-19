-- VIEW: kxhigh_orders_fills
-- Fill-rate and slippage: each order joined to its fills (if any).

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_orders_fills` AS
WITH
order_fills AS (
  SELECT
    o.client_order_id,
    o.kalshi_order_id,
    o.city, o.city_abv, o.forecast_date, o.run_date,
    o.market_ticker,
    o.contracts AS ordered_contracts,
    o.no_price  AS ordered_no_price_cents,
    o.expiration_ts,
    o.created_at,
    COALESCE(SUM(f.filled_count), 0) AS filled_contracts,
    COUNT(f.fill_id) AS n_fills,
    AVG(f.no_price) AS avg_fill_no_price_cents,
    MIN(f.fill_ts)   AS first_fill_ts,
    MAX(f.fill_ts)   AS last_fill_ts
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders` o
  LEFT JOIN (
    SELECT *, TIMESTAMP_SECONDS(SAFE_CAST(ts AS INT64)) AS fill_ts
    FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
  ) f
    ON (f.client_order_id = o.client_order_id OR f.order_id = o.kalshi_order_id)
  GROUP BY
    o.client_order_id, o.kalshi_order_id, o.city, o.city_abv, o.forecast_date,
    o.run_date, o.market_ticker, o.contracts, o.no_price, o.expiration_ts,
    o.created_at
)
SELECT
  *,
  filled_contracts / NULLIF(ordered_contracts, 0) AS fill_rate,
  CASE WHEN filled_contracts > 0 THEN TRUE ELSE FALSE END AS any_fill,
  -- Slippage: avg fill price vs limit price (positive = better than limit)
  CASE
    WHEN filled_contracts > 0 THEN ordered_no_price_cents - avg_fill_no_price_cents
    ELSE NULL
  END AS slippage_cents
FROM order_fills;
