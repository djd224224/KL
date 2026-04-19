---
name: weather-analysis-2
description: Local-execution variant of weather-analysis. Automatically pulls fresh Kalshi settlement CSV (KXHIGH weather markets filtered) via fetch_settlements_csv.py, then runs the standard weather dashboard analyzer on it. No CSV upload required. Use when the user asks to rebuild/refresh the weather CSV dashboard, analyze weather trading performance, run the KXHIGH trading report, or mentions "run weather-analysis-2", "fresh weather dashboard", "pull fresh weather trades and analyze", or "weather P&L from live data". This skill does NOT accept uploaded CSVs — it always fetches fresh data from the Kalshi API. Not to be confused with weather-model-accuracy (which uses BigQuery + forecast validation) or the original weather-analysis (which requires CSV upload).
---

# Weather Analysis (Live-Fetch) Skill

Same output as the original `weather-analysis` skill, but no file upload required —
pulls the latest settlement data from the Kalshi API and analyzes the KXHIGH
subset locally.

## Related skills — pick the right one

| Skill | Data source | Focus |
|---|---|---|
| **weather-analysis-2** (this) | Live API pull | Weather P&L, city breakdowns, price-level edge from CSV |
| `weather-analysis` (original) | Uploaded CSV | Same analysis, but CSV-driven |
| `weather-model-accuracy` | BigQuery views | Forecast accuracy, Brier/calibration, actual vs forecast NWS temps |

If the user wants **forecast-vs-actual analysis**, use `weather-model-accuracy`
instead. Use this skill when they want the **P&L / city breakdown** flavor.

## Prerequisites

- Run from the project root (`C:\Users\jackd\Documents\KL`) — must contain
  `fetch_settlements_csv.py` and `analyze_weather_dashboard.py`
- Kalshi API auth (`KALSHI_API_KEY_ID` env var + `Lisa_Kalshi.txt` PEM, or
  `KALSHI_PRIVATE_KEY` env var)
- Python 3.x with `cryptography`, `requests`

## Steps

```bash
# 1. Pull fresh settlement CSV (all markets; script filters KXHIGH internally)
python fetch_settlements_csv.py
```

Grab the output filename from stdout (`Kalshi-Settlements-YYYYMMDD-HHMMSS.csv`), then:

```bash
# 2. Optional but recommended — also pull trades (gives fill-level detail)
python fetch_trades_csv.py

# 3. Build the weather dashboard HTML
python analyze_weather_dashboard.py <settlement.csv> <trade.csv> weather_dashboard_latest.html
# Or without trades:
python analyze_weather_dashboard.py <settlement.csv> none weather_dashboard_latest.html

# 4. Open it
start "" weather_dashboard_latest.html
```

## Argument notes

`analyze_weather_dashboard.py` takes:
1. Settlement CSV (required — the script internally filters to KXHIGH tickers)
2. Trade CSV (optional; use `none` if skipping)
3. Output HTML path (optional; defaults to `weather_report.html`; can also set
   `WEATHER_DASHBOARD_OUT` env var)

## After running

Report the summary block the script prints to stdout — net P&L, ROI, Sharpe,
max drawdown, daily/weekly/monthly win rates, and per-city P&L breakdown.
Confirm the HTML opened.

## Troubleshooting

- **Empty dashboard**: if no KXHIGH markets settled in the pulled window,
  the output will be mostly empty. Check the Kalshi API response size.
- **`Kalshi-Trades-*.csv` not found**: you can skip the trade step and pass
  `none` — the weather analyzer works fine on settlement-only data.
- **Script missing**: `analyze_weather_dashboard.py` is in the project root.
  Run `git pull` if it's not there.
