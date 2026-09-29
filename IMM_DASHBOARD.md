# IMM command center

One self-contained HTML page for the incentive MM bot: how the day went at a
glance, what drove it, where the risk is and what is left on the table, with
every number drillable to the market. Built by `imm_dashboard.py`; rebuilt
every 10 minutes by the `KL imm dashboard` task.

- **Page:** `run-logs\incentive-mm\dashboard\imm_dashboard.html` (open it in a
  browser; a local copy reloads itself every 10 minutes). In the Claude desktop
  app, the `imm-dashboard` preview config in `.claude/launch.json` serves it on
  http://127.0.0.1:8874/imm_dashboard.html.
- **Build by hand:** `python imm_dashboard.py [--open] [--no-api] [--api-refresh]`.
- **Task:** `register_imm_dashboard.ps1` (the 30-minute sync registers it the
  first time the script is on main). Log: `run-logs\incentive-mm\dashboard\dashboard-task.log`.

## What the headline numbers are

| Number | Definition | Source |
|---|---|---|
| Modeled rewards | the bot's reward estimate (`est_frac x pool_per_day`) integrated over each gap between full cycles, gaps capped at 900 s; exactly `imm_reward_recon._scan_cycle_log`, bucketed by UTC hour | `cycle_log_*.csv` |
| Trading P&L | mark-to-market on the bot's own book: realized in the window + change in unrealized between the 5-minute position snapshots at the window's edges | `realized_*`, `marks_*`, `settlements_*` |
| Net | modeled rewards + trading P&L | |
| Credited | Kalshi credits by credit date (IMM-attributable when the calibration has it) | `reward_credits.csv` |

A market that leaves the book by a **manual offset** is treated as transferred
at its last mark, never as a trading loss. Days are **ET calendar days**; the
bot's own halt counter rolls at 5am CT and is shown under Risk next to the
daily-loss meter.

**Checked 2026-09-28** on live data: modeled rewards over the bot's roll day
$483.97 vs the bot's own `reward_est_today` $482.02 (0.4%); trading P&L by
this method vs an independent cash-flow rebuild (fills + settlements + marks)
within ~2% (today -$423 vs -$435, yesterday -$533 vs -$527). The bot's own
`pnl_today_carry` reads less negative (-$310 vs -$370 that day) because it
drops P&L across restarts (9 runs that day).

## Sections

- **Summary:** net (hero), rewards and trading tiles with 14-day sparklines,
  pace against the same time yesterday, the cumulative intraday chart, fills and
  30-minute mark-outs, quoting now, open inventory, halts.
- **Needs attention:** stale heartbeat, daily-loss / manual halts, toxic event
  and side halts, risk guards, a morning email that did not send, a stale credit
  ledger, restarts, promising new launches, open pick-off windows.
- **Drivers:** family -> event -> market tree (rewards, trading, net, fills,
  mark-out, quoting, resting $, est $/day now, open MTM) and a rewards-vs-trading
  chart per family. Families promote the rewards report's "Quiet-print & scan"
  subjects to top level (`family_of`), because that bucket is most of the book.
- **Movers**, **Quoting now** (coverage, share of pool, yield in cents per $1
  resting per day, why candidates are not quoted), **Opportunities**, **Risk &
  halts**, **Fills**, **History** (daily net, modeled vs credited), **Health**.
- **Market drawer:** position, live quote, reward share, per-window P&L split,
  7-day hourly P&L / mark / position, fills with mark-outs.

## New launches and "promising"

Events are grouped by series. A series that already had events before the
window is a **routine re-listing** (daily rain, state gas, weekly YouTube...)
and is hidden unless flagged. `left` = pool $/day x time left in the programs,
so a 15-minute program at $480/day counts as the $5 it holds.

For new series nothing quotes, the dashboard runs the bot's own estimator
(`build_meta` + `_estimate_candidate_yield`, the 7:20 email's path) on the
biggest-pool markets (3 per event, 120 book reads per hourly refresh, biggest
pools first). **Promising** = modeled >= $3/day (or >= $1.50/day at >= 2%/day
ROI) and >= $3 over the time left, not off by a standing decision (blocklist,
freeze, open-scan family exclusion). **Unestimated pool** = no estimate yet but
>= $500 left. These are models: standard ladder, full coverage, no competitor
response.

## Knobs (environment)

`IMM_DASH_DIR`, `IMM_DASH_HISTORY_DAYS` (30), `IMM_DASH_API_TTL_MIN` (60),
`IMM_DASH_PROGRAMS_TTL_MIN` (30), `IMM_DASH_PROMISING_EST` (3.0),
`IMM_DASH_PROMISING_LEFT` (500), `IMM_DASH_EXTRA_PER_EVENT` (3),
`IMM_DASH_EXTRA_MAX_BOOKS` (120), `IMM_DASH_TITLE_BUDGET` (80).

## Guarantees

Read-only: GETs against Kalshi through the same code the morning emails use;
nothing places, amends or cancels. Writes only under `DASH_DIR` (the page,
`cache/`, `programs_seen.json`). Completed UTC-day sink files are parsed once
and cached on size:mtime; the live day's files are re-parsed each run.
Output lines are ASCII-safe (stdout is reconfigured with `errors="replace"`).
