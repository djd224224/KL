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
- **Summary for the morning email:** each build also writes
  `imm_dashboard_summary.json` beside the page (`summary_of`): every window's
  card totals (modeled rewards, trading P&L, realized, change in marks), each
  event's share of Yesterday and 7 days, and each ET day as its day card shows
  it. The IMM section of the 7:00 portfolio email reads its yesterday / 7-day /
  30-day figures from this file rather than re-deriving them (Jack 2026-10-02:
  "yesterday RAW will equal the dashboard's total trading P&L"), and says so
  when the file is missing or was not rebuilt since midnight ET.

## What the headline numbers are

| Number | Definition | Source |
|---|---|---|
| Modeled rewards | the bot's reward estimate (`est_frac x pool_per_day`) integrated over each gap between full cycles, gaps capped at 900 s; exactly `imm_reward_recon._scan_cycle_log`, bucketed by UTC hour | `cycle_log_*.csv` |
| Projected full day (Today only; replaced the "now $X/day run-rate" line 2026-10-02) | modeled rewards so far + the bot's last full cycle carried to 00:00 ET: each market keeps its share of its pool (`est_frac x pool_per_day`), rescaled every ET hour to the size the bot quotes then (`share_at`: k f / (1 + (k-1) f), k = `incentive_mm.hour_size_mult` then / now; it put 10/2's 10:00 ET drop from x2 to x1 at $751/day against $788 logged), until the market's cutoff or program end. n/a when no cycle in 15 min. A model: before the $1 floor; new markets and competition changes are not in it | `cycle_log_*.csv`, the decision snapshot, the bot's size schedule |
| Trading P&L | mark-to-market on the bot's own book: realized in the window + change in unrealized between the 5-minute position snapshots at the window's edges + exits the bot does not book (below) | `realized_*`, `marks_*`, `settlements_*`, Kalshi market results |
| Net | modeled rewards + trading P&L | |
| Credited | Kalshi credits by credit date (IMM-attributable when the calibration has it) | `reward_credits.csv` |

**Exits the bot does not book** are valued at Kalshi's actual settlement:
scalar settlements (NFL ladders and escalators settle at a fractional value;
until 2026-09-29 the bot booked only yes/no and logged the rest as
`manual_offset` -- all 34 such rows on 9/6-9/29 were scalar settlements, +$299
measured from the dashboard's last mark; the bot's own ledger, which dropped
them at cost, was short +$29.95) and positions that leave its book with no
record at all (4 on 9/6-9/29: markets that settled while the bot restarted,
zeroed by its startup reconcile; ledger short +$62.38). Since 2026-09-29 the
bot books both itself (scalar at `settlement_value`, void at cost; the
reconcile settles instead of zeroing), so new exits of either kind should not
appear. Only an exit Kalshi has not settled counts as a transfer at the last
mark. Results are cached in `cache/exit_results.json`. Days are **ET calendar
days**; the bot's own halt counter rolls at 5am CT and is shown under Risk
next to the daily-loss meter.

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
drops P&L across restarts, and before 2026-09-29 it never booked scalar
settlements.

## Sections

- **Summary:** net (hero), rewards and trading tiles with 14-day sparklines,
  pace against the same time yesterday, the cumulative intraday chart, fills
  with per-contract edge / 30-minute / to-date mark-outs, quoting now, open
  inventory with the book's worst case, halts.
- **Needs attention:** stale heartbeat, daily-loss / manual halts, toxic event
  and side halts, risk guards, a morning email that did not send, a stale credit
  ledger, restarts, promising new launches, open pick-off windows.
- **Drivers** (below), **Movers**, **Quoting now** (coverage, share of pool,
  yield in cents per $1 resting per day, why candidates are not quoted),
  **Opportunities**, **Risk & halts** (inventory by event, sorted by worst
  case), **Fills** (edge and every mark-out horizon per family and per fill),
  **History** (daily net, modeled vs credited), **Health**.
- **Market drawer:** position and worst case, live quote, reward share,
  per-window P&L as new fills vs carry, 7-day hourly P&L / mark / position,
  fills with edge, 30-minute and to-date mark-outs.

## Drivers: what drove the window, and what to do about it

A family -> event -> market tree. Families promote the rewards report's
"Quiet-print & scan" subjects to top level (`family_of`), because that bucket
is most of the book. Every header sorts (default: worst net first); the
horizon buttons pick the mark-out column; "At paid rate" swaps modeled
rewards for what Kalshi has paid per modeled dollar.

| Column | What it is | Source |
|---|---|---|
| New fills | the window's own fills, marked to its end (or to the settlement) | `pnl - cr - cs` |
| Carry: marks (`cr`) | the position held at the window's start x (end mark - start mark); for a position that left the book, its last mark before leaving | 5-minute snapshots, `gone` |
| Carry: settled (`cs`) | that start position in a market Kalshi settled in the window, start mark -> settlement | settlement rows, resolved exits |
| Mark-out | maker fills (no pads, no taker fills), $ and cents per contract, at the chosen horizon: *at fill* = edge vs the mid of the external book the bot read the cycle before the fill (none on a 50c+ book); *5m / 30m / 4h* = the first 5-minute snapshot that long after (within 5 / 15 / 15 min); *to date* = Kalshi's settlement, else the bot's mark while held, else the external mid it quotes against, else Kalshi's touch under the bot's mark rule | `fills_*` ext_bid/ext_ask, `marks_*`, market records |
| Coverage | rewards / what the new fills lost (under 1x: the pool does not pay for the flow) | |
| Typical day | mean net per complete ET day over the 7 (and 30) days before the window; the net cell carries a sigma mark at 1.5+ sd, pro-rated to the window | `days` |
| Shape | cumulative net, hourly; "jump" when one hour holds half the trading move; ticks = code / config changes whose IMM commit subject or IMM_* knob names the family | `fcurves`, `config_history_*`, `git log` |
| Quoting / Est $/day | resting now / the estimator's rewards on what rests now | last cycle |
| Exp. net/day | a model: est $/day now + the last 7 days' average trading P&L per day | |
| Worst case | loss from today's marks to settlement if everything goes against the book, netted per event: across its strikes when every market prices one number, across names when Kalshi marks it mutually exclusive; mention words and unread structure count in full. On a past day: the book at that day's close | Kalshi market / event records |

The three trading parts add up to trading P&L exactly; the tie-out to Kalshi
(`imm_dashboard_verify.py`) is unchanged. Above the table: the window's P&L
bridge (rewards -> new fills -> carry -> net) and the mark-out heatmap
(family x horizon, blue made money, red lost, capped at +/-5c). Row notes say
what settled, the biggest market, a jump, halts and pick-offs (yesterday and
today), guard holds, accrual projected under the $1 floor this period, and the
changes that name the family.

**Paid rate** = credited / modeled per family from `imm_reward_recon`'s
calibration (settled events only). The calibration's post-amendment
`series` (written by `write_calibration` since 2026-10-01) carries the RAW
estimate, so the rate covers the $1 floor and the model error together;
until the next recon run the page falls back to the lifetime series on the
FLOORED estimate (model error only) and says so. Account credits include other
bots quoting the same markets.

**Kalshi reads.** Market records (strike type / floor / cap, value now) are
cached in `cache/markets_meta.json` forever for the structure and for 30 min
(a market traded in the last day) or 3 h (older) for the value, at most
`MARKET_READ_BUDGET` (1,500) tickers per build; event exclusivity in
`cache/events_meta.json` (`EVENT_READ_BUDGET` 60 per build). A cold cache
fills in over a few builds; until then the worst case counts unread markets in
full. Multi-horizon mark-outs are cached per fill in `cache/markouts_h.json`
(seeded from the old 30-minute `markouts.json`); a completed marks file is
re-parsed only while it still owes a mark-out, so the first build after the
upgrade re-reads the history once (~30 s).

## Any past day

Every ET day the cycle history covers (30 days) can be opened as its own
window, computed exactly as "Yesterday" is: rewards, trading P&L, net, the
intraday curve (15-minute steps), drivers, movers (with start and end-of-day
positions) and fills. Open a day by clicking it in the Daily history table or
either history chart, from the day picker beside the window buttons, with the
◀ ▶ buttons or the left/right arrow keys, or with a link such as
`imm_dashboard.html#2026-09-28`. Quoting, inventory, halts, opportunities and
health always show the book now; a banner says so. Days before the position
log (2026-09-06) show rewards only. The ET-midnight position snapshots are
kept for the whole history (`edges` in the marks cache), so a month-old day
measures P&L between the same snapshots its history row uses.

A build fixes "now" when it starts and ignores log rows written after it, so
a slow build cannot pull a window's end edge back to an older snapshot.

**Window edges line up with the snapshots.** A 5-minute position snapshot is
stamped at its cycle's start but written after that cycle booked its fills
and settlements, while the bot writes the matching realized row only at its
next state save (37s later on median, 2+ minutes at the 90th percentile). So
each realized row is dated by the cycle that booked it (the fills log's
`cycle_ts`; a settlement's or offset's own cycle), and a vanished position by
the first snapshot without it. Without this, a window ending on that snapshot
held the position change but not the realized P&L (9/30: -$5.80 shown for a
true -$12.80 on one gas market).

`imm_dashboard_verify.py` lists apart any market whose end position Kalshi's
fills on the bot's orders cannot reproduce: there the bot's own book was
edited without a fill (a state restore, a reconcile adopting someone else's
fill), and the dashboard follows the bot's book. First seen 9/30 on
KXMLBSEASONGAMES-27-2425 (book -47.36 vs fills -25.00 until the 0077bd5
restore), worth about $1-2 a day.

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
`imm_dashboard_summary.json`, `cache/`, `programs_seen.json`). Completed UTC-day sink files are parsed once
and cached on size:mtime; the live day's files are re-parsed each run.
Output lines are ASCII-safe (stdout is reconfigured with `errors="replace"`).
