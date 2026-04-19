-- VIEW: KXHIGH_orders_clean
-- Canonical orders:
--   * price in DOLLARS (was cents int)
--   * expiration_ts as TIMESTAMP (was unix int)
--   * city/event parsed from ticker if missing
--   * market_kind added

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_orders_clean` AS
SELECT
  client_order_id,
  kalshi_order_id,
  market_ticker,
  REGEXP_EXTRACT(market_ticker, r"^(KXHIGH[A-Z-]+?-[0-9]{2}[A-Z]{3}[0-9]{2})-") AS event_ticker,
  COALESCE(city_abv, REGEXP_EXTRACT(market_ticker, r"^KXHIGH([A-Z-]+?)-[0-9]{2}[A-Z]{3}[0-9]{2}-")) AS city_abv,
  COALESCE(
    forecast_date,
    SAFE.PARSE_DATE("%y%b%d", REGEXP_EXTRACT(market_ticker, r"-([0-9]{2}[A-Z]{3}[0-9]{2})-"))
  ) AS event_date,
  CASE
    WHEN REGEXP_CONTAINS(market_ticker, r"-B[0-9.]+$") THEN "between"
    WHEN REGEXP_CONTAINS(market_ticker, r"-T[0-9.]+$") THEN "tail"
    ELSE "unknown"
  END AS market_kind,
  SAFE_CAST(REGEXP_EXTRACT(market_ticker, r"-[BT]([0-9]+(?:\.[0-9]+)?)$") AS FLOAT64) AS market_strike,
  contracts AS ordered_contracts,
  no_price / 100.0 AS no_price_dollars,
  TIMESTAMP_SECONDS(expiration_ts) AS expiration_ts,
  run_date AS run_ts,
  created_at AS created_ts
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`;
