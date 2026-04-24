-- =====================================================================
-- A/B test report: hi_no_tied_to_fair_no_v1
-- =====================================================================
-- Arms:
--   control   — hi_no_price from static config (legacy behavior)
--   treatment — hi_no = min(hi_no_config, 100·P(no) − safety_margin)
--
-- Metrics: P&L per ordered contract, fill rate, per-(P(yes) bucket)
-- breakdown, per-city breakdown. Run after the test has accumulated
-- enough settled markets (typically 1-2 weeks of 50/50 split).
-- =====================================================================

-- 1) Headline per-arm summary (settled markets only)
WITH arm_orders AS (
  SELECT
    o.ab_arm,
    o.market_ticker,
    o.kalshi_order_id,
    o.contracts            AS ordered_contracts,
    o.no_price             AS ordered_no_price,
    o.fair_no_cents,
    o.yes_prob,
    o.hi_no_config,
    o.effective_hi_no,
    o.ab_safety_margin_cents,
    o.created_at
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders` o
  WHERE o.ab_test_name = 'hi_no_tied_to_fair_no_v1'
    AND o.ab_test_enabled = TRUE
),
filled AS (
  -- Sum actual fills per order (orders -> fills is 1->many via kalshi_order_id)
  SELECT
    f.order_id AS kalshi_order_id,
    SUM(SAFE_CAST(f.count_fp AS FLOAT64))                                     AS filled_contracts,
    SAFE_DIVIDE(
      SUM(SAFE_CAST(f.count_fp AS FLOAT64) * SAFE_CAST(f.no_price_dollars AS FLOAT64) * 100),
      SUM(SAFE_CAST(f.count_fp AS FLOAT64))
    )                                                                         AS avg_fill_no_price_cents,
    SUM(SAFE_CAST(f.fee_cost AS FLOAT64))                                     AS total_fees
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills` f
  GROUP BY f.order_id
),
settled AS (
  SELECT
    s.market_ticker,
    s.result,                                         -- 'yes' | 'no' | 'cancel'
    s.temp_value                                      -- actual temperature bucket that hit
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements` s
),
joined AS (
  SELECT
    ao.*,
    f.filled_contracts,
    f.avg_fill_no_price_cents,
    f.total_fees,
    s.result,
    CASE WHEN s.result = 'no'  THEN (100 - COALESCE(f.avg_fill_no_price_cents, 0))
         WHEN s.result = 'yes' THEN -COALESCE(f.avg_fill_no_price_cents, 0)
         ELSE 0  -- cancel / unresolved
    END * COALESCE(f.filled_contracts, 0) / 100.0     AS pnl_dollars_gross,
    COALESCE(f.total_fees, 0)                         AS fees
  FROM arm_orders ao
  LEFT JOIN filled f USING (kalshi_order_id)
  LEFT JOIN settled s USING (market_ticker)
)
SELECT
  ab_arm,
  COUNT(*)                                                             AS n_orders,
  COUNTIF(result IS NOT NULL)                                          AS n_settled_orders,
  COUNT(DISTINCT market_ticker)                                        AS unique_markets,
  SUM(ordered_contracts)                                               AS total_ordered,
  SUM(COALESCE(filled_contracts, 0))                                   AS total_filled,
  ROUND(SAFE_DIVIDE(SUM(COALESCE(filled_contracts, 0)), SUM(ordered_contracts)), 3) AS fill_rate,
  ROUND(AVG(avg_fill_no_price_cents), 2)                               AS avg_fill_price_c,
  ROUND(SUM(pnl_dollars_gross), 2)                                     AS gross_pnl,
  ROUND(SUM(fees), 2)                                                  AS fees,
  ROUND(SUM(pnl_dollars_gross) - SUM(fees), 2)                         AS net_pnl,
  ROUND(SAFE_DIVIDE(SUM(pnl_dollars_gross) - SUM(fees),
                    SUM(COALESCE(filled_contracts, 0))), 4)            AS net_pnl_per_filled_contract,
  -- NO win rate: fraction of settled fills where result='no'
  ROUND(AVG(CASE WHEN result = 'no' THEN 1.0 WHEN result = 'yes' THEN 0.0 ELSE NULL END), 3) AS no_win_rate
FROM joined
GROUP BY ab_arm
ORDER BY ab_arm;

-- =====================================================================
-- 2) Per-(P(yes) bucket × arm) breakdown
--    The fix should bite hardest in 0.4+ P(yes) bins. Use this to see
--    whether treatment helps uniformly or only for high-P(yes) bets.
-- =====================================================================
-- Paste joined CTE from above, then:
-- SELECT
--   CASE
--     WHEN yes_prob < 0.3 THEN '1: [0.2, 0.3)'
--     WHEN yes_prob < 0.4 THEN '2: [0.3, 0.4)'
--     WHEN yes_prob < 0.5 THEN '3: [0.4, 0.5)'
--     WHEN yes_prob < 0.6 THEN '4: [0.5, 0.6)'
--     ELSE                       '5: [0.6, 1.0]'
--   END AS pyes_bucket,
--   ab_arm,
--   COUNT(*) AS n_orders,
--   SUM(COALESCE(filled_contracts, 0)) AS filled,
--   ROUND(SUM(pnl_dollars_gross) - SUM(fees), 2) AS net_pnl,
--   ROUND(SAFE_DIVIDE(SUM(pnl_dollars_gross) - SUM(fees),
--                     SUM(COALESCE(filled_contracts, 0))), 4) AS pnl_per_contract
-- FROM joined
-- WHERE result IS NOT NULL
-- GROUP BY pyes_bucket, ab_arm
-- ORDER BY pyes_bucket, ab_arm;

-- =====================================================================
-- 3) Ladder-skip rate by arm
--    Treatment may skip whole markets when effective_hi_no < 2c.
--    This shows how often that happens — a high rate means safety_margin
--    is too aggressive for the current hi_no config.
-- =====================================================================
-- SELECT
--   ab_arm,
--   COUNT(DISTINCT CONCAT(market_ticker, '|', CAST(created_at AS STRING))) AS market_runs_with_orders
-- FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
-- WHERE ab_test_name = 'hi_no_tied_to_fair_no_v1' AND ab_test_enabled
-- GROUP BY ab_arm;
-- Compare to expected (50/50 if treatment never skipped).

-- =====================================================================
-- 4) Per-city breakdown (did the fix help everywhere or only some cities?)
-- =====================================================================
-- SELECT city, ab_arm, COUNT(*) AS n,
--        SUM(COALESCE(filled_contracts, 0)) AS filled,
--        ROUND(SUM(pnl_dollars_gross) - SUM(fees), 2) AS net_pnl
-- FROM joined
-- LEFT JOIN `elite-contact-446323-q7.Kalshi.KXHIGH_orders` USING (kalshi_order_id)
-- WHERE result IS NOT NULL
-- GROUP BY city, ab_arm
-- ORDER BY city, ab_arm;
