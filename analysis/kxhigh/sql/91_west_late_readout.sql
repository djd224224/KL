-- =====================================================================
-- West late-morning test readout                    (started 2026-09-29)
-- =====================================================================
-- What the test adds: the scheduled 14:07 UTC run (run_slot='west_late')
-- re-quotes LAX/SFO/SEA/LV/SD/PHX/DEN and keeps quoting until 10:05 LOCAL.
-- Before the test those cities stopped at 10:05 CT (08:05 PT / 09:05 MT).
--
-- Unit: contracts filled on orders placed by the west_late run, split by
-- fill time:
--   'new window'  = after the old 10:05 CT cutoff  -> what the test adds
--   're-quote'    = before it -> the window the 07:02 run already covered,
--                   now quoted with fresher obs/peak filters
-- P&L = hold to settlement: filled x 1[NO won] - cost. SETTLED MARKETS ONLY
-- (unsettled fills show in filled_ct but not in pnl).
--
-- Evidence it was tested against (Jul 7 - Sep 27, morning-run fills):
-- East/Central earned +8 to +16c/ct in their 8-10 AM local hour buckets.
-- Decision rule: at 30 trading days, keep if the new window is > +3c/ct
-- with a positive total, kill if it is negative, else run to 60 days.
-- (~50-100 filled ct/day expected: a ~5c standard error at 30 days, so
-- 30 days separates +8c from zero, not +8c from +4c.)
--
-- SELECT-only: build_views.py skips files without CREATE OR REPLACE.
-- =====================================================================
WITH o AS (
  SELECT kalshi_order_id, market_ticker, city, DATE(forecast_date) AS fd
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_orders`
  WHERE run_slot = 'west_late'
),
f AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT order_id, TIMESTAMP(created_time) AS fill_ts,
           CAST(count_fp AS FLOAT64) AS ct,
           CAST(no_price_dollars AS FLOAT64) AS px,
           ROW_NUMBER() OVER (PARTITION BY fill_id ORDER BY pulled_at DESC) AS rn
    FROM `elite-contact-446323-q7.Kalshi.KXHIGH_fills`)
  WHERE rn = 1
),
s AS (
  SELECT market_ticker, UPPER(result) AS result
  FROM `elite-contact-446323-q7.Kalshi.KXHIGH_settlements`
),
x AS (
  SELECT
    o.city, o.fd, o.market_ticker, f.ct, s.result,
    IF(f.fill_ts >= TIMESTAMP(DATETIME(o.fd, TIME(10, 5, 0)), 'America/Chicago'),
       'new window', 're-quote') AS fill_window,
    CASE s.result WHEN 'NO'  THEN f.ct * (1 - f.px)
                  WHEN 'YES' THEN -f.ct * f.px END AS pnl
  FROM o
  JOIN f ON f.order_id = o.kalshi_order_id
  LEFT JOIN s USING (market_ticker)
)
SELECT
  IFNULL(fill_window, 'ALL') AS fill_window,
  IFNULL(city, 'ALL') AS city,
  COUNT(DISTINCT fd) AS days,
  COUNT(DISTINCT market_ticker) AS mkts,
  ROUND(SUM(ct), 1) AS filled_ct,
  ROUND(SUM(IF(result IN ('NO', 'YES'), ct, 0)), 1) AS settled_ct,
  ROUND(SUM(pnl), 2) AS settled_pnl,
  ROUND(100 * SUM(pnl) / NULLIF(SUM(IF(result IN ('NO', 'YES'), ct, 0)), 0), 2) AS c_per_ct
FROM x
GROUP BY ROLLUP (fill_window, city)
ORDER BY fill_window, city;
