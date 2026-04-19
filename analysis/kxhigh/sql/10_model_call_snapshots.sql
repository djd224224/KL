-- VIEW: kxhigh_model_call_snapshots
-- One row per market_ticker = the snapshot the bot's "final call" was made from.
-- Definition: latest snapshot with run_date BEFORE the market's expiration cutoff
-- (cancel_hour:cancel_minute US/Central on the event_date, per high_temp_trading.py).
-- Evening/overnight runs from the prior day qualify (they're before cutoff).

CREATE OR REPLACE VIEW `elite-contact-446323-q7.Kalshi.KXHIGH_model_call_snapshots` AS
WITH
city_abv_map AS (
  SELECT * FROM UNNEST([
    STRUCT("Chicago" AS city, "CHI" AS city_abv, 10 AS cancel_hour),
    STRUCT("New York City", "NY-", 9),
    STRUCT("Denver", "DEN", 10),
    STRUCT("Philadelphia", "PHI", 9),
    STRUCT("Austin", "AUS", 10),
    STRUCT("Miami", "MIA", 9),
    STRUCT("Houston", "HOU", 10),
    STRUCT("Los Angeles", "LAX", 10),
    STRUCT("Atlanta", "ATL", 9),
    STRUCT("Washington DC", "TDC", 9),
    STRUCT("Phoenix", "PHX", 10),
    STRUCT("Dallas", "DAL", 10),
    STRUCT("Las Vegas", "TLV", 10),
    STRUCT("Oklahoma City", "OKC", 10),
    STRUCT("Seattle", "SEA", 10),
    STRUCT("San Francisco", "SFO", 10),
    STRUCT("San Antonio", "SATX", 10),
    STRUCT("Minneapolis", "TMIN", 10),
    STRUCT("New Orleans", "NOLA", 10)
  ])
),
snapshots AS (
  -- Dedupe to one row per (market_ticker, run_date) (ties: highest yes_probability first)
  SELECT * EXCEPT(rn) FROM (
    SELECT
      ms.*,
      ROW_NUMBER() OVER (PARTITION BY market_ticker, run_date
                         ORDER BY yes_probability DESC NULLS LAST) AS rn
    FROM `elite-contact-446323-q7.Kalshi.KXHIGH_market_snapshot` ms
  )
  WHERE rn = 1
),
with_cutoff AS (
  SELECT
    s.*,
    m.city_abv,
    m.cancel_hour,
    -- Expiration cutoff: (forecast_date at cancel_hour:05 CT)
    TIMESTAMP(
      DATETIME(s.forecast_date, TIME(m.cancel_hour, 5, 0)),
      "America/Chicago"
    ) AS cutoff_ts
  FROM snapshots s
  LEFT JOIN city_abv_map m ON s.city = m.city
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY market_ticker
      ORDER BY run_date DESC
    ) AS call_rank
  FROM with_cutoff
  WHERE run_date < cutoff_ts
),
-- Also compute earliest snapshot per market, for intraday edge-decay analysis
earliest AS (
  SELECT
    market_ticker,
    ARRAY_AGG(yes_probability ORDER BY run_date ASC LIMIT 1)[OFFSET(0)] AS earliest_yes_prob,
    ARRAY_AGG(forecast_avg   ORDER BY run_date ASC LIMIT 1)[OFFSET(0)] AS earliest_forecast_avg,
    MIN(run_date) AS earliest_run_date,
    COUNT(*) AS snapshots_in_window
  FROM with_cutoff
  WHERE run_date < cutoff_ts
  GROUP BY market_ticker
)
SELECT
  r.* EXCEPT(call_rank),
  e.earliest_yes_prob,
  e.earliest_forecast_avg,
  e.earliest_run_date,
  e.snapshots_in_window,
  -- Recomputed forecast stats using only non-NULL sources (AccuWeather died Mar 2026)
  (
    SELECT AVG(v) FROM UNNEST([r.nws, r.accuweather, r.weather_underground]) v WHERE v IS NOT NULL
  ) AS forecast_avg_recomputed,
  (
    SELECT STDDEV_SAMP(v) FROM UNNEST([r.nws, r.accuweather, r.weather_underground]) v WHERE v IS NOT NULL
  ) AS forecast_std_recomputed,
  (
    SELECT COUNT(*) FROM UNNEST([r.nws, r.accuweather, r.weather_underground]) v WHERE v IS NOT NULL
  ) AS forecast_n_sources
FROM ranked r
LEFT JOIN earliest e USING (market_ticker)
WHERE r.call_rank = 1;
