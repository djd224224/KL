-- VIEW: KXHIGH_fills_clean
-- Canonical fills with CORRECT price orientation.
--
-- ⚠ Data-orientation bug in raw: for side=no fills, the `yes_price` column
-- stores what the bot actually paid per NO contract; `no_price` stores the
-- complementary YES market price at fill time. (Verified: using yes_price as
-- NO-side cost reconciles EXACTLY with settlements.total_cost; using no_price
-- leaves a $66 gap across the dataset.)
-- This view exposes `price_paid_dollars` as the unambiguous truth.
--
-- Other cleanups:
--   * prices in DOLLARS
--   * ts/created/pulled as TIMESTAMP
--   * redundant STRING dup columns dropped
--   * city/event parsed from ticker if missing
--   * market_kind added

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_fills_clean` AS
SELECT
  fill_id,
  trade_id,
  order_id,
  client_order_id,
  market_ticker,
  COALESCE(event_ticker, REGEXP_EXTRACT(market_ticker, r"^(KXHIGH[A-Z-]+?-[0-9]{2}[A-Z]{3}[0-9]{2})-")) AS event_ticker,
  COALESCE(city_abv, REGEXP_EXTRACT(market_ticker, r"^KXHIGH([A-Z-]+?)-[0-9]{2}[A-Z]{3}[0-9]{2}-")) AS city_abv,
  COALESCE(
    SAFE.PARSE_DATE("%Y-%m-%d", CAST(forecast_date AS STRING)),
    SAFE.PARSE_DATE("%y%b%d", REGEXP_EXTRACT(market_ticker, r"-([0-9]{2}[A-Z]{3}[0-9]{2})-"))
  ) AS event_date,
  CASE
    WHEN REGEXP_CONTAINS(market_ticker, r"-B[0-9.]+$") THEN "between"
    WHEN REGEXP_CONTAINS(market_ticker, r"-T[0-9.]+$") THEN "tail"
    ELSE "unknown"
  END AS market_kind,
  SAFE_CAST(REGEXP_EXTRACT(market_ticker, r"-[BT]([0-9]+(?:\.[0-9]+)?)$") AS FLOAT64) AS market_strike,
  LOWER(side) AS side,
  LOWER(action) AS action,
  is_taker,
  filled_count,
  -- TRUE price paid (per contract, dollars).
  -- For side=no: bot paid yes_price (Kalshi's fill response puts the NO-side
  -- executed price in the `yes_price` field for NO fills). For side=yes: the
  -- analogous swap — bot paid no_price. Belt-and-suspenders CASE below.
  CASE
    WHEN LOWER(side) = "no"  THEN yes_price / 100.0
    WHEN LOWER(side) = "yes" THEN no_price  / 100.0
    ELSE NULL
  END AS price_paid_dollars,
  CASE
    WHEN LOWER(side) = "no"  THEN 1.0 - (yes_price / 100.0)
    WHEN LOWER(side) = "yes" THEN 1.0 - (no_price  / 100.0)
    ELSE NULL
  END AS complement_price_dollars,
  -- Raw columns preserved as *_raw for debugging / validation
  no_price  / 100.0 AS raw_no_price_dollars,
  yes_price / 100.0 AS raw_yes_price_dollars,
  fill_price / 100.0 AS raw_fill_price_dollars,
  order_no_price / 100.0 AS order_limit_no_price_dollars,
  order_contracts,
  SAFE_CAST(fee_cost AS FLOAT64) AS fee_cost_dollars,
  -- Gross cost / payoff per fill (in dollars)
  filled_count * CASE
    WHEN LOWER(side) = "no"  THEN yes_price / 100.0
    WHEN LOWER(side) = "yes" THEN no_price  / 100.0
  END AS fill_cost_dollars,
  -- Times as TIMESTAMP
  TIMESTAMP_SECONDS(SAFE_CAST(ts AS INT64)) AS fill_ts,
  SAFE.PARSE_TIMESTAMP("%Y-%m-%dT%H:%M:%E*SZ", created_time) AS created_ts,
  SAFE.PARSE_TIMESTAMP("%Y-%m-%dT%H:%M:%E*S%Ez", pulled_at)  AS pulled_ts
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`;
