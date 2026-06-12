-- =====================================================================
-- A/B test report: hi_no_tied_to_fair_no_v2  (sticky per market_ticker)
-- =====================================================================
-- Arms:
--   control   — hi_no_price from static config (legacy behavior)
--   treatment — hi_no = min(hi_no_config, 100·(1−P(yes)) − safety_margin_cents)
--
-- GROUND TRUTH = cash-flow realized P&L, NOT KXHIGH_settlements.pnl.
--
--   realized_pnl(market) = fills_cash + settlement_payout
--     fills_cash         = Σ over fills of  (+price if sell, −price if buy) × count / 100
--     settlement_payout  = net_held_position × $1 on the winning side
--
-- WHY NOT settlements.pnl:  that column = revenue − total_cost. It counts
-- the BUY cost of every contract but only credits the settlement payout of
-- contracts STILL HELD at expiry. Any contract the bot sold back before
-- settlement (a closeout) has its buy cost counted with NO offsetting sale
-- proceeds — so each closed-out contract looks like a total loss. For this
-- actively-closing strategy that overstated the v2 loss by ~$5.3k
-- (−$8.51k settlements.pnl  vs  −$3.26k true realized) and FLIPPED the
-- per-arm sign (settlements.pnl said treatment worse; realized says better).
--
-- Reconciliation: fills-derived net NO position matches settlements.position_no
-- within ≤1 contract on all 529 settled v2 markets, so the fills cash-flow is
-- trustworthy as the unit of analysis.
--
-- Contamination note still applies: 16 markets were partially hit by 258
-- orders fired 2026-04-30 07:51–07:56 UTC with ab_test_enabled=false (commit
-- 675da2048). Those fills are in the cash-flow too; the per-market realized
-- P&L still corresponds to the arm the market was assigned to. Treat as noise.
-- =====================================================================

-- Reusable realized-P&L building blocks
-- (BigQuery has no shared CTEs across statements; each query below repeats
--  the v2 / fills_cf / settle CTEs. Keep them in sync if you edit one.)

-- 1) Headline per-arm summary (cash-flow realized P&L)
WITH v2 AS (
  SELECT DISTINCT market_ticker, ab_arm
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
  WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2'
    AND ab_test_enabled = TRUE
),
fills_cf AS (
  SELECT market_ticker,
    SUM( (CASE WHEN action = 'sell' THEN 1 ELSE -1 END) * filled_count
         * (CASE WHEN side = 'no' THEN no_price ELSE yes_price END) ) / 100.0 AS fills_cash,
    SUM( filled_count * (CASE WHEN side = 'no' THEN 1 ELSE -1 END)
         * (CASE WHEN action = 'buy' THEN 1 ELSE -1 END) )                    AS fills_net_no,
    SUM( filled_count )                                                       AS total_filled
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
  GROUP BY market_ticker
),
settle AS (
  SELECT market_ticker, UPPER(result) AS result, position_no, position_yes,
         pnl AS settlements_pnl_legacy,
         (CASE WHEN UPPER(result) = 'NO'  THEN position_no  ELSE 0 END)
       + (CASE WHEN UPPER(result) = 'YES' THEN position_yes ELSE 0 END) AS settle_payout
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
),
joined AS (
  SELECT v.ab_arm, v.market_ticker, s.result, s.position_no,
         s.settlements_pnl_legacy,
         COALESCE(f.fills_cash, 0) + s.settle_payout AS realized_pnl
  FROM v2 v
  JOIN settle s USING (market_ticker)
  LEFT JOIN fills_cf f USING (market_ticker)
)
SELECT
  ab_arm,
  COUNT(*)                                                  AS n_settled_markets,
  ROUND(SUM(realized_pnl), 2)                               AS net_realized_pnl,
  ROUND(AVG(realized_pnl), 4)                               AS mean_realized_per_market,
  ROUND(STDDEV(realized_pnl), 2)                            AS sd_realized_per_market,
  ROUND(STDDEV(realized_pnl) / SQRT(COUNT(*)), 4)           AS se_realized_per_market,
  ROUND(SUM(settlements_pnl_legacy), 2)                     AS net_settlements_pnl_legacy,
  ROUND(SUM(realized_pnl) - SUM(settlements_pnl_legacy), 2) AS closeout_credit_vs_legacy,
  ROUND(SUM(position_no), 0)                                AS total_no_contracts_held,
  ROUND(SAFE_DIVIDE(COUNTIF(result = 'NO'  AND position_no > 0),
                    COUNTIF(result IN ('NO','YES') AND position_no > 0)), 3) AS no_win_rate,
  COUNTIF(result = 'NO')                                    AS markets_resolved_no,
  COUNTIF(result = 'YES')                                   AS markets_resolved_yes
FROM joined
GROUP BY ab_arm
ORDER BY ab_arm;

-- =====================================================================
-- 2) Per-(P(yes) bucket × arm) breakdown, on realized P&L
--    Hypothesis: the cap bites hardest when fair NO is in the danger
--    zone (P(yes) ≥ 0.4). Bucket each market by the median yes_prob of
--    its v2-enabled orders so we attribute one bucket per market.
-- =====================================================================
WITH v2 AS (
  SELECT market_ticker,
         ANY_VALUE(ab_arm)                        AS ab_arm,
         APPROX_QUANTILES(yes_prob, 2)[OFFSET(1)] AS median_yes_prob
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
  WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2' AND ab_test_enabled = TRUE
  GROUP BY market_ticker
),
fills_cf AS (
  SELECT market_ticker,
    SUM( (CASE WHEN action = 'sell' THEN 1 ELSE -1 END) * filled_count
         * (CASE WHEN side = 'no' THEN no_price ELSE yes_price END) ) / 100.0 AS fills_cash
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
  GROUP BY market_ticker
),
settle AS (
  SELECT market_ticker,
         (CASE WHEN UPPER(result) = 'NO'  THEN position_no  ELSE 0 END)
       + (CASE WHEN UPPER(result) = 'YES' THEN position_yes ELSE 0 END) AS settle_payout
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
),
joined AS (
  SELECT v.ab_arm, v.median_yes_prob,
         COALESCE(f.fills_cash, 0) + s.settle_payout AS realized_pnl
  FROM v2 v
  JOIN settle s USING (market_ticker)
  LEFT JOIN fills_cf f USING (market_ticker)
)
SELECT
  CASE
    WHEN median_yes_prob < 0.20 THEN '1: [0.0, 0.2)'
    WHEN median_yes_prob < 0.30 THEN '2: [0.2, 0.3)'
    WHEN median_yes_prob < 0.40 THEN '3: [0.3, 0.4)'
    WHEN median_yes_prob < 0.50 THEN '4: [0.4, 0.5)'
    WHEN median_yes_prob < 0.60 THEN '5: [0.5, 0.6)'
    ELSE                              '6: [0.6, 1.0]'
  END AS pyes_bucket,
  ab_arm,
  COUNT(*)                            AS n_markets,
  ROUND(SUM(realized_pnl), 2)         AS net_realized_pnl,
  ROUND(AVG(realized_pnl), 4)         AS mean_realized_per_market
FROM joined
GROUP BY pyes_bucket, ab_arm
ORDER BY pyes_bucket, ab_arm;

-- =====================================================================
-- 3) Per-city breakdown, on realized P&L
-- =====================================================================
WITH v2 AS (
  SELECT market_ticker, ANY_VALUE(ab_arm) AS ab_arm, ANY_VALUE(city) AS city
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
  WHERE ab_test_name = 'hi_no_tied_to_fair_no_v2' AND ab_test_enabled = TRUE
  GROUP BY market_ticker
),
fills_cf AS (
  SELECT market_ticker,
    SUM( (CASE WHEN action = 'sell' THEN 1 ELSE -1 END) * filled_count
         * (CASE WHEN side = 'no' THEN no_price ELSE yes_price END) ) / 100.0 AS fills_cash
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`
  GROUP BY market_ticker
),
settle AS (
  SELECT market_ticker,
         (CASE WHEN UPPER(result) = 'NO'  THEN position_no  ELSE 0 END)
       + (CASE WHEN UPPER(result) = 'YES' THEN position_yes ELSE 0 END) AS settle_payout
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
)
SELECT v.city, v.ab_arm, COUNT(*) AS n_markets,
       ROUND(SUM(COALESCE(f.fills_cash, 0) + s.settle_payout), 2) AS net_realized_pnl
FROM v2 v
JOIN settle s USING (market_ticker)
LEFT JOIN fills_cf f USING (market_ticker)
GROUP BY city, ab_arm
ORDER BY city, ab_arm;
