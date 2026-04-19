-- KXHIGH sanity checks. Run each block to validate data conventions
-- before building downstream views. Values we need to confirm:
--   1. Price units (cents vs dollars) in each table
--   2. Outcome logic (winning_temp ∈ [low_range, high_range] == YES)
--   3. settlements.pnl = revenue - total_cost - fee_cost
--   4. Per-source NULL coverage (AccuWeather 401'd since Mar 2026)
--   5. Position sign conventions
--   6. city_abv → expiration-hour mapping coverage
--   7. Duplicate snapshot rows per (market_ticker, run_date)

-- =======================================================================
-- 1. Price units
-- =======================================================================
-- 1a. fills: yes_price + no_price should sum to 100 (cents)
SELECT
  'fills' AS tbl,
  COUNT(*) AS n,
  COUNTIF(yes_price + no_price = 100) AS sum_100,
  COUNTIF(yes_price + no_price BETWEEN 99 AND 101) AS sum_approx_100,
  MIN(yes_price) AS min_yes, MAX(yes_price) AS max_yes,
  MIN(no_price) AS min_no,  MAX(no_price) AS max_no,
  MIN(fill_price) AS min_fill, MAX(fill_price) AS max_fill
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`;

-- 1b. finalized_markets: are avg_trade_price_* in cents or dollars?
SELECT
  'finalized_markets' AS tbl,
  COUNT(*) AS n,
  MIN(avg_trade_price_yes) AS min_yes, MAX(avg_trade_price_yes) AS max_yes,
  MIN(avg_trade_price_no)  AS min_no,  MAX(avg_trade_price_no)  AS max_no,
  AVG(avg_trade_price_yes + avg_trade_price_no) AS avg_sum
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_finalized_markets`;

-- 1c. market_snapshot: fair_no_price, no_highest_bid, no_lowest_offer
SELECT
  'market_snapshot' AS tbl,
  COUNT(*) AS n,
  MIN(fair_no_price)   AS min_fair,    MAX(fair_no_price)   AS max_fair,
  MIN(no_highest_bid)  AS min_bid,     MAX(no_highest_bid)  AS max_bid,
  MIN(no_lowest_offer) AS min_offer,   MAX(no_lowest_offer) AS max_offer,
  MIN(hi_no_price)     AS min_hi_no,   MAX(hi_no_price)     AS max_hi_no,
  MIN(yes_probability) AS min_prob,    MAX(yes_probability) AS max_prob
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`;

-- =======================================================================
-- 2. Outcome logic & winning_temp parsability
-- =======================================================================
-- 2a. winning_temp is STRING — check parse cleanliness
SELECT
  COUNT(*) AS n,
  COUNTIF(SAFE_CAST(winning_temp AS INT64) IS NOT NULL) AS parseable_int,
  COUNTIF(winning_temp IS NULL) AS null_rows,
  ARRAY_AGG(DISTINCT winning_temp IGNORE NULLS ORDER BY winning_temp LIMIT 20) AS sample_values
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_finalized_markets`;

-- 2b. Outcome check: join snapshot strike + finalized winning_temp + settlement result
--     YES if winning_temp ∈ [low_range, high_range]
SELECT
  COUNT(*) AS n_matched,
  COUNTIF(
    SAFE_CAST(fm.winning_temp AS FLOAT64) BETWEEN ms.low_range AND ms.high_range
    AND LOWER(s.result) = 'yes'
  ) AS yes_agree,
  COUNTIF(
    NOT (SAFE_CAST(fm.winning_temp AS FLOAT64) BETWEEN ms.low_range AND ms.high_range)
    AND LOWER(s.result) = 'no'
  ) AS no_agree,
  COUNTIF(
    SAFE_CAST(fm.winning_temp AS FLOAT64) BETWEEN ms.low_range AND ms.high_range
    AND LOWER(s.result) != 'yes'
  ) AS yes_disagree,
  COUNTIF(
    NOT (SAFE_CAST(fm.winning_temp AS FLOAT64) BETWEEN ms.low_range AND ms.high_range)
    AND LOWER(s.result) != 'no'
  ) AS no_disagree
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s
JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_finalized_markets` fm USING (market_ticker)
JOIN (
  SELECT market_ticker,
         ANY_VALUE(low_range)  AS low_range,
         ANY_VALUE(high_range) AS high_range
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`
  GROUP BY market_ticker
) ms USING (market_ticker);

-- =======================================================================
-- 3. PnL formula validation
-- =======================================================================
-- Does pnl ≈ revenue - total_cost - fee_cost?   (units: cents? dollars?)
SELECT
  COUNT(*) AS n,
  AVG(pnl) AS avg_pnl,
  AVG(revenue - total_cost - fee_cost) AS avg_formula,
  AVG(pnl - (revenue - total_cost - fee_cost)) AS avg_resid,
  COUNTIF(ABS(pnl - (revenue - total_cost - fee_cost)) < 0.01) AS close_formula,
  COUNTIF(ABS(pnl - (revenue/100 - total_cost - fee_cost)) < 0.01) AS close_revenue_cents,
  COUNTIF(ABS(pnl - ((revenue - total_cost - fee_cost)/100)) < 0.01) AS close_all_cents
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
WHERE pnl IS NOT NULL;

-- =======================================================================
-- 4. Forecast source NULL coverage
-- =======================================================================
SELECT
  DATE(run_date) AS run_day,
  COUNT(*) AS rows_,
  COUNTIF(nws IS NULL) AS null_nws,
  COUNTIF(accuweather IS NULL) AS null_acc,
  COUNTIF(weather_underground IS NULL) AS null_wu,
  COUNTIF(nws IS NULL OR accuweather IS NULL OR weather_underground IS NULL) AS any_null
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`
GROUP BY run_day
ORDER BY run_day DESC
LIMIT 30;

-- =======================================================================
-- 5. Position sign convention
-- =======================================================================
SELECT
  'settlements' AS tbl,
  MIN(net_position) AS min_net, MAX(net_position) AS max_net,
  AVG(net_position) AS avg_net,
  AVG(position_yes) AS avg_pos_yes, AVG(position_no) AS avg_pos_no,
  COUNTIF(net_position = position_yes - position_no) AS net_equals_yes_minus_no,
  COUNTIF(net_position = position_no  - position_yes) AS net_equals_no_minus_yes,
  COUNT(*) AS n
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`;

-- =======================================================================
-- 6. city_abv coverage (catch any city missing from our expiration map)
-- =======================================================================
SELECT
  city_abv,
  COUNT(DISTINCT market_ticker) AS markets,
  COUNT(*) AS rows_
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`
GROUP BY city_abv
ORDER BY markets DESC;

-- =======================================================================
-- 7. Duplicate snapshots per (market_ticker, run_date)
-- =======================================================================
SELECT
  AVG(n) AS avg_rows_per_snapshot,
  MAX(n) AS max_rows_per_snapshot,
  COUNTIF(n > 1) AS duplicates
FROM (
  SELECT market_ticker, run_date, COUNT(*) AS n
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`
  GROUP BY 1, 2
);

-- =======================================================================
-- 8. Low/high range distribution — catch "below X" and "above X" sentinels
-- =======================================================================
SELECT
  MIN(low_range)  AS min_low,  APPROX_QUANTILES(low_range, 10)  AS low_deciles,
  MIN(high_range) AS min_high, APPROX_QUANTILES(high_range, 10) AS high_deciles,
  MAX(high_range) AS max_high,
  COUNTIF(low_range < -100) AS low_sentinels,
  COUNTIF(high_range > 200) AS high_sentinels
FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot`;
