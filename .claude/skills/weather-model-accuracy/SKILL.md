---
name: weather-model-accuracy
description: Build and open the interactive HTML dashboard that validates the KXHIGH weather-market trading bot's forecast accuracy, probability calibration, P&L attribution, and execution quality. Use this when the user asks to regenerate/refresh/rebuild the weather dashboard, run forecast validation, analyze KXHIGH bot performance, check forecast-vs-actual accuracy, review Brier/calibration scores, analyze P&L by city or weekday, or see how the weather trading bot is doing. Also use when the user mentions the "weather dashboard", "KXHIGH dashboard", "model accuracy", "forecast validation", or "trading bot performance review".
---

# Weather Model Accuracy Dashboard

This skill rebuilds the interactive HTML dashboard at `analysis/kxhigh/output/kxhigh_validation.html` from the latest data in BigQuery.

## What the dashboard shows

Seven sections, each led by a data-driven "Takeaways" block:

1. **Overview & Sanity** — P&L, win rate, ROI, coverage caveats
2. **Forecast accuracy** — MAE / bias per city, GFS vs ECMWF comparison (369 settled markets, real NWS CLI temps)
3. **Prediction calibration** — Brier score, log loss, reliability diagram, model-vs-market comparison
4. **Forecast spread ↔ P&L** — does source disagreement help or hurt
5. **Edge capture** — per-fill scatter of model edge vs realized P&L
6. **Execution quality** — fill rate, fee drag, maker/taker split
7. **P&L attribution** — cumulative curve, drawdown, per-city + weekday breakdowns

## Default flow (rebuild from current BQ state)

This is what to do when the user just says "rebuild the dashboard", "rerun it", "refresh the analysis":

```bash
python analysis/kxhigh/python/dashboard.py
```

Takes 10-20 seconds (reads 5 BQ views). Writes `analysis/kxhigh/output/kxhigh_validation.html`.

Then open it:
```bash
start "" "analysis/kxhigh/output/kxhigh_validation.html"
```

Report the row counts and total P&L back to the user (printed in dashboard.py output).

## Full refresh (when user asks to "pull fresh data first" or "update everything")

Before rebuilding, refresh the two external data sources. MERGE upserts — safe to re-run any time:

```bash
# Latest NWS Daily Climate Reports (actual high temps per settled market)
python analysis/kxhigh/python/fetch_cli.py backfill --year $(date +%Y)

# Historical forecasts (GFS + ECMWF ensemble via Open-Meteo)
python analysis/kxhigh/python/fetch_historical_forecasts.py --start $(date -d '90 days ago' +%Y-%m-%d) --end $(date +%Y-%m-%d)

# Then rebuild the dashboard
python analysis/kxhigh/python/dashboard.py
```

## Verify station coordinates (rare — user explicitly asks)

If the user suspects station/coord drift or asks to verify stations:
```bash
python analysis/kxhigh/python/verify_stations.py
```
Should show 0.00 km distance for all 19 cities if coords are correct.

## Prerequisites

- `CLOUDSDK_PYTHON` env var (already set in `~/.claude/CLAUDE.md` user preferences)
- `gcloud auth application-default login` has been done
- Python deps: `pandas`, `plotly`, `numpy`, `scipy`, `google-cloud-bigquery`, `db-dtypes`

## What not to do

- Don't invoke the `anthropic-skills:weather-analysis` skill — that one operates on raw CSV exports and is different from this dashboard
- Don't rebuild the BQ views unless the user explicitly asks for a schema change — the dashboard reads from `KXHIGH_resolved_markets`, `_fills_enriched`, `_orders_fills`, `_model_call_snapshots`, `_settlements_clean`

## After running

1. Confirm the HTML file was written (size should be 200 KB+)
2. Open it with `start ""` on Windows
3. Summarize key numbers from dashboard.py's stdout: row counts and total P&L

## Source files

- `analysis/kxhigh/python/dashboard.py` — entry point
- `analysis/kxhigh/python/load.py` — BQ loaders
- `analysis/kxhigh/python/metrics.py` — Brier, calibration, etc.
- `analysis/kxhigh/python/fetch_cli.py` — NWS CLI refresh
- `analysis/kxhigh/python/fetch_historical_forecasts.py` — Open-Meteo refresh
- `analysis/kxhigh/sql/` — underlying views
