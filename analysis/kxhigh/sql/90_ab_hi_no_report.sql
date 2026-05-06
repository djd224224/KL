-- =====================================================================
-- A/B test report: hi_no_tied_to_fair_no_v2  (sticky per market_ticker)
-- =====================================================================
-- Arms:
--   control   — hi_no_price from static config (legacy behavior)
--   treatment — hi_no = min(hi_no_config, 100·(1−P(yes)) − safety_margin_cents)
--
-- Ground truth = `KXHIGH_settlements.pnl` (revenue/100 − total_cost),
-- which already nets fees, both sides, and any closeouts at the
-- account level — i.e. it matches your Kalshi statement 1:1. Each
-- v2-test market gets one settlement row, so the per-market net P&L
-- is the right unit of analysis given sticky assignment.
--
-- Includes ALL 100 settled v2-test markets. 16 of them were partially
-- contaminated by 258 orders fired 2026-04-30 07:51–07:56 UTC during
-- a one-off ad-hoc local run with `ab_test_enabled=false` (commit
-- 675da2048 documents the cause and fix). Those fills land on top of
-- the v2 sticky assignment without re-randomizing it, so we keep them
-- in — the per-market P&L still corresponds to the arm the market was
-- assigned to. Treat the contamination as added noise, not as bias.
-- =====================================================================

-- 1) Headline per-arm summary (settlements.pnl, all 100 settled markets)
WITH v2_markets AS (
  SELECT DISTINCT market_ticker, ab_arm
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
  WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2'
    AND ab_test_enabled = TRUE
),
joined AS (
  SELECT v.ab_arm, v.market_ticker, s.result, s.pnl AS market_pnl,
         s.position_no, s.position_yes, s.total_cost, s.total_payout, s.fee_cost
  FROM v2_markets v
  JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s USING (market_ticker)
)
SELECT
  ab_arm,
  COUNT(*)                                                  AS n_settled_markets,
  COUNTIF(position_no > 0 OR position_yes > 0)              AS markets_with_action,
  ROUND(SUM(market_pnl), 2)                                 AS net_pnl,
  ROUND(AVG(market_pnl), 4)                                 AS mean_pnl_per_market,
  ROUND(STDDEV(market_pnl), 2)                              AS sd_pnl_per_market,
  ROUND(STDDEV(market_pnl) / SQRT(COUNT(*)), 4)             AS se_pnl_per_market,
  ROUND(SUM(position_no), 0)                                AS total_no_contracts,
  ROUND(SUM(total_cost), 2)                                 AS total_cost,
  ROUND(SUM(fee_cost), 4)                                   AS total_fees,
  -- NO win rate over markets where we held a NO position
  ROUND(SAFE_DIVIDE(COUNTIF(UPPER(result) = 'NO'  AND position_no > 0),
                    COUNTIF(UPPER(result) IN ('NO','YES') AND position_no > 0)), 3)  AS no_win_rate,
  COUNTIF(UPPER(result) = 'NO')                             AS markets_resolved_no,
  COUNTIF(UPPER(result) = 'YES')                            AS markets_resolved_yes
FROM joined
GROUP BY ab_arm
ORDER BY ab_arm;

-- =====================================================================
-- 2) Per-(P(yes) bucket × arm) breakdown
--    Hypothesis: the cap bites hardest when fair NO is in the danger
--    zone (P(yes) ≥ 0.4). Bucket each market by the median yes_prob
--    of its v2-enabled orders so we attribute one bucket per market.
-- =====================================================================
-- WITH v2_markets AS (
--   SELECT
--     market_ticker,
--     ANY_VALUE(ab_arm)                       AS ab_arm,
--     APPROX_QUANTILES(yes_prob, 2)[OFFSET(1)] AS median_yes_prob
--   FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
--   WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2' AND ab_test_enabled = TRUE
--   GROUP BY market_ticker
-- ),
-- joined AS (
--   SELECT v.ab_arm, v.median_yes_prob, s.pnl AS market_pnl
--   FROM v2_markets v
--   JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s USING (market_ticker)
-- )
-- SELECT
--   CASE
--     WHEN median_yes_prob < 0.20 THEN '1: [0.0, 0.2)'
--     WHEN median_yes_prob < 0.30 THEN '2: [0.2, 0.3)'
--     WHEN median_yes_prob < 0.40 THEN '3: [0.3, 0.4)'
--     WHEN median_yes_prob < 0.50 THEN '4: [0.4, 0.5)'
--     WHEN median_yes_prob < 0.60 THEN '5: [0.5, 0.6)'
--     ELSE                              '6: [0.6, 1.0]'
--   END AS pyes_bucket,
--   ab_arm,
--   COUNT(*)                       AS n_markets,
--   ROUND(SUM(market_pnl), 2)      AS net_pnl,
--   ROUND(AVG(market_pnl), 4)      AS mean_pnl_per_market
-- FROM joined
-- GROUP BY pyes_bucket, ab_arm
-- ORDER BY pyes_bucket, ab_arm;

-- =====================================================================
-- 3) Fill-rate / cap-binding diagnostic (orders side)
--    The cap clips the bid; expect lower fill rate but better avg fill
--    NO price for treatment. This isn't a P&L cut — it's a sanity check.
-- =====================================================================
-- WITH arm_orders AS (
--   SELECT ab_arm, kalshi_order_id, contracts, hi_no_config, effective_hi_no
--   FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
--   WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2' AND ab_test_enabled = TRUE
-- ),
-- per_order_fill AS (
--   SELECT order_id AS kalshi_order_id,
--          SUM(SAFE_CAST(count_fp AS FLOAT64))                                                   AS filled,
--          SAFE_DIVIDE(SUM(SAFE_CAST(count_fp AS FLOAT64) * SAFE_CAST(no_price_dollars AS FLOAT64) * 100),
--                      SUM(SAFE_CAST(count_fp AS FLOAT64)))                                       AS avg_no_c
--   FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
--   GROUP BY order_id
-- )
-- SELECT ao.ab_arm,
--        COUNT(*)                                                       AS n_orders,
--        ROUND(SUM(COALESCE(f.filled, 0)) / SUM(ao.contracts), 3)       AS fill_rate,
--        ROUND(AVG(f.avg_no_c), 2)                                      AS avg_fill_no_c,
--        COUNTIF(ao.effective_hi_no < ao.hi_no_config)                  AS n_cap_binding,
--        ROUND(SAFE_DIVIDE(COUNTIF(ao.effective_hi_no < ao.hi_no_config), COUNT(*)), 3)
--                                                                       AS cap_binding_rate
-- FROM arm_orders ao
-- LEFT JOIN per_order_fill f USING (kalshi_order_id)
-- GROUP BY ao.ab_arm
-- ORDER BY ao.ab_arm;

-- =====================================================================
-- 4) Per-city breakdown
-- =====================================================================
-- WITH v2_markets AS (
--   SELECT market_ticker, ANY_VALUE(ab_arm) AS ab_arm, ANY_VALUE(city) AS city
--   FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
--   WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2' AND ab_test_enabled = TRUE
--   GROUP BY market_ticker
-- )
-- SELECT v.city, v.ab_arm, COUNT(*) AS n_markets,
--        ROUND(SUM(s.pnl), 2) AS net_pnl
-- FROM v2_markets v
-- JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s USING (market_ticker)
-- GROUP BY city, ab_arm
-- ORDER BY city, ab_arm;
