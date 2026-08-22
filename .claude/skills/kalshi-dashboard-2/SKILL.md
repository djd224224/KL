---
name: kalshi-dashboard-2
description: Local-execution variant of kalshi-dashboard. Automatically pulls fresh Kalshi settlement and trade CSVs from the Kalshi API (via fetch_settlements_csv.py + fetch_trades_csv.py), then runs the standard Kalshi dashboard analyzer on them. No CSV upload required. Use when the user asks to rebuild/refresh the Kalshi dashboard, analyze trading performance, run the trading report, or mentions "run kalshi-dashboard-2", "fresh dashboard", "pull fresh trades and analyze", or "how am I doing on Kalshi (from live data)". This skill does NOT accept uploaded CSVs — it always fetches fresh data from the Kalshi API. For CSV-upload workflows, use the original kalshi-dashboard skill instead.
---

# Kalshi Dashboard (Live-Fetch) Skill

Same output as the original `kalshi-dashboard` skill, but no file upload required —
pulls the latest settlement + trade data from the Kalshi API, analyzes locally, and
opens the resulting HTML.

## Prerequisites

- Run from the project root (`C:\Users\jackd\Documents\KL`) — must be the dir
  containing `fetch_settlements_csv.py` and `analyze_kalshi_dashboard.py`
- Kalshi API auth set up (`KALSHI_API_KEY_ID` env var + `Lisa_Kalshi.txt` PEM, or
  `KALSHI_PRIVATE_KEY` env var containing the base64-encoded PEM)
- Python 3.x with `cryptography`, `requests` installed

## Steps

Run from the project root in this order:

```bash
# 1. Pull fresh settlement CSV from Kalshi API
python fetch_settlements_csv.py

# 2. Pull fresh trade CSV from Kalshi API
python fetch_trades_csv.py
```

Both scripts print the output filename on completion. They follow the pattern:
- `Kalshi-Settlements-YYYYMMDD-HHMMSS.csv`
- `Kalshi-Trades-YYYYMMDD-HHMMSS.csv`

Capture those filenames (the `-> ...csv` lines from stdout), then:

```bash
# 3. Merge the pulls into the append-only archives. Kalshi's portfolio
#    endpoints only serve ~65 days back, so the archives are the real
#    dataset — analyzing the raw pull would silently drop older history.
python merge_kalshi_archive.py Kalshi-Settlements-archive.csv <settlement.csv> "seed_settlements_*.csv" "KalshiRecentActivitySettlement*.csv"
python merge_kalshi_archive.py Kalshi-Trades-archive.csv <trade.csv>

# 4. Build the dashboard HTML from the ARCHIVES (not the raw pulls)
python analyze_kalshi_dashboard.py Kalshi-Settlements-archive.csv Kalshi-Trades-archive.csv none "Kalshi" kalshi_dashboard_latest.html

# 5. Open it
start "" kalshi_dashboard_latest.html
```

## Argument notes

`analyze_kalshi_dashboard.py` takes:
1. Settlement CSV (or `none`)
2. Trade CSV (or `none`)
3. Order CSV (or `none` — we don't have a fetch script for orders, so always `none`)
4. Label (shown in dashboard header — use `"Kalshi"` or the current date)
5. Output HTML path (optional; defaults to `kalshi_report.html`)

## After running

Report the summary block the script prints to stdout — net P&L, ROI, Sharpe, max
drawdown, and per-family breakdown. Then confirm the HTML opened.

## Differences vs original `kalshi-dashboard`

| | kalshi-dashboard | kalshi-dashboard-2 |
|---|---|---|
| Input | User uploads CSV into chat | Pulled live from Kalshi API |
| Runtime | Claude Desktop sandbox | Local machine |
| Freshness | As-of the uploaded export | Latest available from API |
| Requires Kalshi creds | No | Yes |

## Automated daily rebuild

The Windows scheduled task `KL dashboards-daily` (self-registers via the
`sync_kl_main.ps1` bootstrap, or manually via `register_dashboard_task.ps1`;
runner: `run_dashboards.ps1`) executes these
same steps every day at 7:00 AM and overwrites `kalshi_dashboard_latest.html`
(plus the weather dashboard). The generated HTML carries a 15-minute meta
refresh, so a browser tab left open on the file picks up each rebuild by
itself. Manual runs of this skill still work any time — they write the same
file. Failures email via `send_alert_email.py`; log: `run-logs\dashboards.log`.

Website "Recent Activity" settlement exports (cents prices, fractional
counts, gross-payout profit) can be dropped into the repo root as
`seed_settlements_*.csv` or `KalshiRecentActivitySettlement*.csv` — the
merge normalizes them to the API format and folds them into the archive
(all gitignored: personal data, public repo).

## Troubleshooting

- **"No private key" / auth error**: ensure `Lisa_Kalshi.txt` is in the project root,
  or `KALSHI_PRIVATE_KEY` env var is set.
- **Script missing**: `fetch_settlements_csv.py`, `fetch_trades_csv.py`, and
  `analyze_kalshi_dashboard.py` all must exist in CWD. If any is missing, the
  repo is out of date — `git pull` first.
- **Empty CSV**: if the fetch returns 0 rows, Kalshi's API may be returning a
  rate-limit or empty response. Retry in 30 seconds.
