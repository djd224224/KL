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
- **Task:** run `register_imm_dashboard.ps1` once, by hand, to create the
  10-minute rebuild (nothing registers it automatically). Until then the page
  refreshes only when `imm_dashboard.py` is run. Log:
  `run-logs\incentive-mm\dashboard\dashboard-task.log`.

## What the headline numbers are

| Number | Definition | Source |
|---|---|---|
| Modeled rewards | the bot's reward estimate (`est_frac x pool_per_day`) integrated over each gap between full cycles, gaps capped at 900 s; exactly `imm_reward_recon._scan_cycle_log`, bucketed by UTC hour | `cycle_log_*.csv` |
| Trading P&L | mark-to-market on the bot's own book: realized in the window + change in unrealized between the 5-minute position snapshots at the window's edges + exits the bot does not book (below) | `realized_*`, `marks_*`, `settlements_*`, Kalshi market results |
| Net | modeled rewards + trading P&L | |
| Credited | Kalshi credits by credit date (IMM-attributable when the calibration has it) | `reward_credits.csv` |

**Exits the bot does not book** are valued at Kalshi's actual settlement:
scalar settlements (NFL ladders and escalators settle at a fractional value;
the bot only books yes/no and logs the rest as `manual_offset` -- all 34 such
rows on 9/6-9/29 were scalar settlements, +$299 the bot never booked) and
positions that leave its book with no record at all (4 on 9/6-9/29). Only an
exit Kalshi has not settled counts as a transfer at the last mark. Results are
cached in `cache/exit_results.json`. Days are **ET calendar days**; the bot's
own halt counter rolls at 5am CT and is shown under Risk next to the
daily-loss meter.

**Audited 2026-09-29** against sources the dashboard does not use:

- Trading P&L equals a replay of Kalshi's own records (the account's fills on
  the bot's order ids, Kalshi settlement results incl. scalar values, start
  positions from the snapshot) to the cent: 9/16 -$637.10, 9/22 -$776.20,
  9/27 -$278.51, 9/28 -$450.48, 9/29 intraday, and the 7-day window. The
  replay reproduces the bot's end-of-day positions on every market, and the
  bot's own book equals the account's positions API on all 926 open markets.
- Marks are Kalshi's top-of-book mid (1-minute candles at the snapshot minute:
  median gap 0.0c), or the last trade when the book is empty.
- Modeled rewards equal `imm_reward_recon`'s hourly cache on every market-hour
  of 80 cycle-log files, and the bot's own `reward_history` within 0.2%.
- Quoting now equals Kalshi's resting orders: 367 two-sided / 55 one-sided on
  all 422 markets, resting $8,923 vs $8,916 (top-rung pricing); run-rate equals
  the bot's status line.
- Fills (after de-duplicating the sink's double-written rows), $ traded,
  30-minute mark-outs (25 of 25 recomputed by hand), toxic-halt counts, guard
  holds, the decision mix, the program feed (680 vs 678 events, minutes
  apart), pick-off windows and the credit history all tie out.

The bot's own `pnl_today_carry` differs from the dashboard for two reasons: it
drops P&L across restarts, and it never books scalar settlements.

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
