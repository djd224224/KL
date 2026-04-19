-- VIEW: KXHIGH_settlements_clean
-- Canonical settlements:
--   * all money in DOLLARS (was mixed: revenue in cents, pnl/costs in dollars)
--   * settled_ts / pulled_ts as TIMESTAMP (was STRING with microseconds)
--   * event_date as DATE parsed from ticker (was STRING, 274/369 NULL on raw)
--   * winning_high_temp per event from the YES between-bucket midpoint
--     (accurate to ±1°F; NULL for events where a tail market resolved YES)
--   * city_abv + market_kind + market_strike parsed from ticker

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_settlements_clean` AS
WITH
parsed AS (
  SELECT
    s.*,
    -- Ticker: KXHIGH{CITY}-{YYMMMDD}-{B|T}{STRIKE}
    REGEXP_EXTRACT(market_ticker, r"^KXHIGH([A-Z-]+?)-[0-9]{2}[A-Z]{3}[0-9]{2}-") AS parsed_city_abv,
    REGEXP_EXTRACT(market_ticker, r"^KXHIGH[A-Z-]+?-([0-9]{2}[A-Z]{3}[0-9]{2})-") AS parsed_date_code,
    CASE
      WHEN REGEXP_CONTAINS(market_ticker, r"-B[0-9.]+$") THEN "between"
      WHEN REGEXP_CONTAINS(market_ticker, r"-T[0-9.]+$") THEN "tail"
      ELSE "unknown"
    END AS parsed_market_kind,
    SAFE_CAST(REGEXP_EXTRACT(market_ticker, r"-[BT]([0-9]+(?:\.[0-9]+)?)$") AS FLOAT64) AS parsed_strike,
    REGEXP_EXTRACT(market_ticker, r"^(KXHIGH[A-Z-]+?-[0-9]{2}[A-Z]{3}[0-9]{2})-") AS parsed_event_ticker
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s
),
event_winners AS (
  -- For each event, find the YES-resolving between-bucket; use its midpoint as
  -- the realized high temp (accurate to ±1°F).
  SELECT
    parsed_event_ticker,
    AVG(parsed_strike) AS winning_high_temp
  FROM parsed
  WHERE UPPER(result) = "YES" AND parsed_market_kind = "between"
  GROUP BY parsed_event_ticker
)
SELECT
  p.market_ticker,
  p.parsed_event_ticker AS event_ticker,
  p.parsed_city_abv     AS city_abv,
  -- Event date parsed from ticker (e.g. "26APR17" → DATE '2026-04-17')
  SAFE.PARSE_DATE("%y%b%d", p.parsed_date_code) AS event_date,
  p.parsed_market_kind  AS market_kind,
  p.parsed_strike       AS market_strike,
  UPPER(p.result) AS result,
  CASE WHEN UPPER(p.result) = "YES" THEN 1 ELSE 0 END AS outcome_yes,
  w.winning_high_temp,
  -- Money — all in dollars
  p.revenue / 100.0        AS revenue_dollars,
  p.value   / 100.0        AS residual_value_dollars,
  p.total_cost             AS total_cost_dollars,
  p.fee_cost               AS fee_cost_dollars,
  p.pnl                    AS pnl_dollars,
  p.total_payout           AS total_payout_dollars,
  p.yes_total_cost         AS yes_total_cost_dollars,
  p.no_total_cost          AS no_total_cost_dollars,
  -- Positions & fills
  p.net_position,
  p.position_yes,
  p.position_no,
  p.yes_count,
  p.no_count,
  p.num_fills,
  -- Times as TIMESTAMP (settled_time has microseconds + Z; pulled_at uses +00:00)
  SAFE.PARSE_TIMESTAMP("%Y-%m-%dT%H:%M:%E*SZ", p.settled_time) AS settled_ts,
  SAFE.PARSE_TIMESTAMP("%Y-%m-%dT%H:%M:%E*S%Ez", p.pulled_at)  AS pulled_ts
FROM parsed p
LEFT JOIN event_winners w ON p.parsed_event_ticker = w.parsed_event_ticker;
