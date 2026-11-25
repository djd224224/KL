# IMM daily review: reference

The facts the runbook (`SKILL.md`) leans on. Sourced from the 11/25 survey of the code, the run-logs and the memory notes. Line numbers drift: grep the symbol, don't trust the number.

## 1. Objective and definitions

- **Rewards (MODELLED).** The bot's estimator integrated over the window: est_frac x pool_per_day over each gap between full cycles (gaps capped at 900 s). It is before Kalshi's $1-per-market-per-period floor. It equals `imm_reward_recon._scan_cycle_log` and is the dashboard's number.
- **Credits (MEASURED).**
  - Kalshi's actual payouts, per market per program period, with a hard $1.00 floor. They land 1-2 days after the period ends.
  - There is no API: Jack pastes the rewards page and `imm_reward_recon.py --statement` writes `reward_credits.csv`.
  - Paid/est was 1.118 overall at the 11/18 statement (earnings mentions 1.11, gas 1.02, other 1.07, other mentions 1.37, rain 1.02, hourly temp 0.99).
- **Trading (MEASURED).**
  - Mark-to-market on the bot's own book: realized P&L plus the change in unrealized, with exits valued at Kalshi's settlement. It ties to Kalshi's own records to the cent (`imm_dashboard_verify.py`).
  - Marks are the LAST TRADE since 11/09.
  - Split per market into new fills, carry re-marked (`cr`) and carry settled (`cs`).
- **Net:** rewards + trading.
- **Tax:** -trading / rewards over a window. It is the share of modelled rewards that fills gave back. Lower is better. It ran 0.52 over the 30 days to 11/24.
- **Sharpe / Sortino:** daily net mean / sd (or downside sd) x sqrt(365). Treat them as relative indicators only, because daily net is autocorrelated through carry.
- **Max drawdown:** the worst peak-to-trough of cumulative daily net in the window.
- **$ resting ("rest"):** the average dollars resting on the book over the day, summed over markets. **Net per $1k resting** is net / (rest / 1000), a return on deployed collateral.
- **Mark-outs** (dashboard per market per day, the `fx` array): fill edge vs the mid at fill, then 5 min / 30 min (`mk`) / 4 h / to date, in $ with contracts. c/ct = $ / contracts x 100. Negative means we were picked off.
- **Realisation factor:** how much of a one-reading estimate a market actually earns once quoted.
  - The code found admission estimates realised 0.71-0.74x (the incentive_mm.py comment at the admission clock).
  - The review measures its own: `counterfactual_calibration.json`, used once n >= 15.

## 2. The evidence pack, section by section

| Section | Source | Notes |
|---|---|---|
| Scorecard | `dashboard/cache/day_archive/<ET day>.json` (from 07-12; days before 09-07 are `ok: false`), `dashboard/imm_dashboard_summary.json`, `status_incentive_mm.json`, `reward_calibration.json`, `reward_credits.csv` | Tie-out: archive sums = summary day row within $1 |
| Attribution | day archive x `imm_dashboard.family_of` (falls back to the series), events from `dashboard/cache/markets_meta.json` | |
| Adverse | archive `fx`, `toxic_halts_<utc>.jsonl`, `ws_sweep_<utc>.jsonl` | |
| Monitors | `toxic_halts_profit.json`, `lone_ref_monitor.json`, `join_wait_report.json`, `bol_report.json`, `cheap_report.json`, `rt_settled_report.json`, `risk_controls_history.json` | They run 06:45-08:30, so at 04:30 they show D-1 |
| Counterfactual | `dashboard/cache/api_gaps.pkl` (hourly `imm_quote_gaps.classify_and_estimate`: every unquoted book, 100 per call, since R-261124-22), `api_unquoted.pkl` (not-enrolled series with an ADD/PASS call, 300 books, 3-hour cache), `config_gaps.json`, `selection_snapshot_<utc>.jsonl` | Each day's top 40 predictions go to `counterfactual.jsonl` |
| Coverage | `cycle_log_<utc>.csv` timestamps, `guard_skips_<utc>.jsonl` enters | Dark minutes = cycle gaps beyond 10 min |
| System | `incentive-mm-<local launch date>.log`, `config_history_<utc>.jsonl`, Task Scheduler (`KL *`), task logs, `health-alert.log`, `run-logs/sync-kl-main.log`, git, disk, token mtime | Signatures in `error_signatures.json` |
| Data | sizes of each sink's UTC-day file vs its 14-day median | LOW < 0.3x, HIGH > 3x, MISSING when present 10+ of 14 days |
| Changes | `git log` (local HEAD + origin/main), `config_manifest.jsonl` + `config_store/` (the reloadable files, versioned by the review since 11/25) | Commit subjects naming `R-YYMMDD-NN` mark that item shipped |
| Moat (section 10) | `daily_review/moat/`:<br>- `family_daily.csv`: per day x family, from the day archive. `share` = sum(est_frac x pool_per_day) / sum(pool_per_day) over the cycle log's market-cycles, and `pool_cov` = the average pool $/day managed.<br>- `priors.json`: 30 days, shrunk with m = 5 days toward the book.<br>- `playbook.md`.<br>Plus the edge watch and the review's calibration from the ledger. | Share is our cut of the pool where we quote, the cleanest competition signal. The first run back-fills share for 7 days; older days carry none. |

## 3. Data map and reading recipes

`STATUS_DIR` = `C:\Users\jackd\Documents\KL\run-logs\incentive-mm`. It is 113+ GB, not in git, has no backup, and grows about 2.2 GB a day; nothing prunes it.

Every sink row from `_sink` carries `run_id` and `config_hash`. Files are `<name>_<UTC date>.jsonl`.

| Sink | What it answers | Size/day | Read with |
|---|---|---|---|
| `cycle_log_*.csv` (34 cols) | per full cycle x managed market: book, est_frac, pool, our quotes, wants, hour_mult, run | ~850 MB | stream the first column; never load whole |
| `orders_*.jsonl` | every place / cancel / amend / gone / reject | ~350-470 MB | grep by ticker first |
| `fills_*.jsonl` | each fill with the book at fill, pos/avg before and after, is_pad, order age, est_frac, pool | ~0.2 MB | json lines |
| `realized_*.jsonl`, `settlements_*.jsonl` | realized P&L changes; settlements with own position | small | |
| `marks_*.jsonl` | 5-minute marks of open positions (mark, halt mark, last) | ~150 MB | filter by ticker |
| `selection_events_*.jsonl` / `selection_snapshot_*.jsonl` | decision changes / hourly snapshot of every allowed candidate, with est $/day, floor inputs, member, quotable_sides | 35 / 120 MB | |
| `floor_state_*.jsonl` | member floor clock (accrued_lifetime, period_base, floor_bar, hopeless) | ~25-33 MB | |
| `guard_skips_*.jsonl` | the in-loop guards: enter / clear / hourly snapshot (a guarded market writes no cycle_log row) | ~2 MB | |
| `config_history_*.jsonl` | one row per process start: git_sha, source mtime, every IMM_* env var, named code settings | small | |
| `book_depth/book_depth_<utc>_<run>.jsonl.gz` | raw L2 for every book read (managed every cycle, candidates every refresh), our own size per level | ~550 MB | `python imm_book_depth_read.py` / `iter_rows()`; dedupe on (ts, ticker, source) |
| `toxic_halts_*`, `ws_sweep_*`, `ws_stale_*`, `ws_tape_*`, `ws_group_*`, `join_wait_*`, `back_of_line_*`, `risk_dry_*`, `heightened_*`, `comp_hedge_*` | guard episodes | small | |

- **Bot log** `incentive-mm-<local launch date>.log`:
  - Lines are `YYYY-MM-DD HH:MM:SSZ [IMM] ...` in true UTC. Launcher lines use local time with a fake Z.
  - A file runs until the next restart, so a UTC day spans two or three files.
  - ` ! ` is a non-fatal problem, ` !! ` a hard exit, `ALERT [category]` goes to email unless log-only.
  - Restarts: `kalshi account: key` (start), `clean exit` (code-change exit), `hard-exiting` (watchdog).
- **Status** `status_incentive_mm.json` (every cycle): markets_line, reward est and paid, unrealized, inventory, errors/alerts today (these counters reset per process), latency, halted_until.
- **Dashboard caches** `dashboard/cache/`: `markouts_h.json` (fill_id -> m5/mk/m4), `exit_results.json`, `markets_meta.json` (ticker -> ev, strike), `events_meta.json`, `day_archive/`.
- **Pool supply over time:** `program_history.jsonl`, a daily per-series rollup (`total_reward_raw`; the unit is unverified but consistent). `measure` uses it to normalise rewards for supply.
- **Kalshi reads** (avoid; rate limit shared with the bot): `python kalshi_reads.py markets/<T>/orderbook --public`. Fills and positions are signed and paced; never curl the public host.

## 4. Levers (what a fix can change, and how it takes effect)

**Selection** (`refresh_universe` every 600 s):
- **Allowlist:** about 40 membership tests in `_allowed()`, plus files in STATUS_DIR:
  - `extra_allow_series.json`;
  - `finecon_extra_series.json`;
  - `family_series_verdicts.json`;
  - the election / awards / politics / league auto files;
  - the wave list `post_vote_dead_books.json`;
  - `cheap_side_books.json`.
- **Blocklist:** `IMM_BLOCKLIST` in the launcher, plus CPI / headcount patterns, company KPIs, the NQE suffix and frozen series.
- **Pass 1 screens:** cutoff, closing, extreme mid (5-90c new, 2-98c members), one-sided, wide spread, no volume.
- **Pass 2 gates:**
  - payout floor ($1.10 new / $1.00 member);
  - admission clock (600 s, skipped at 3x the bar);
  - hopeless clock (30 min);
  - near-cliff hold;
  - rate floor;
  - per-event top-N (`IMM_EVENT_TOP_N`, e.g. KXAAAGAS:3);
  - 1,000-event cap;
  - budget ($3M, not binding).
- **Open scan is OFF** (`IMM_SCAN_TOP_N=0` since 10/03). The scan-performance loop (admit on measured family ROI) exists only on branch `imm-scan-performance-loop`, unmerged since 9/20, and needs a rebase.

**Cutoffs:**
- `trade_cutoff_utc`: 00:00 ET of the ticker date, occurrence time, or the earnings call minus a buffer.
- `event_start_overrides.json` is written by `imm_earnings_overrides.py` 3x a day.
- `election_dates_extra.json` and `awards_dates_extra.json` are written by `imm_rows.py`.
- `release_calendar.json` (in git): a pause from 480 min before to 1,440 min after.
- A missing date fails closed and goes into `config_gaps.json`.

**Fair gates:** about 20 `*_fair.json` files reloaded each refresh; fair-lead families can lead the book.

**Size:**
- `IMM_LEVELS=0:20`, `IMM_LADDER_MODE=atref`: one rung per side at the reference level (cumulative depth = target / 5).
- About 55 `IMM_*_SIZE_MULT` family knobs. Each scales the rung, the per-market cap, the per-event cap and skew together.
- Text overrides go in `env_overrides.txt`, applied at a `-Task` restart.
- Hour multipliers for long-dated series: 0-8 ET x3, 18-23 x1.5, Saturday x2 (per `sat_mult_gate.json`).
- Per-market boosts: near-cliff x1.5, yield mode x1.5, the RT / Vercel / art floor boosts.
- Caps: 150 per market, 1,000 per event, skew 30/60, 12,000 resting orders.

**Protection:**

| State | Mechanisms |
|---|---|
| On | Toxic halts (side: 2 pick-offs in 24 h -> 30 min; event: 2 markets in 60 min -> 30 min; mentions exempt); sweep breaker (600 s, escalating; holdout 0); tape trigger (100% pause after a 100+ burst; A/B ended 11/06); websocket fast cancel (20% control); back-of-line; join wait; bait stop |
| Dry | lone-ref guard (on for waves), alone-front, heightened bait, dollar worst-case limits |
| Off | mid-move / fill-burst breakers (`IMM_BREAKERS=0`) |
| Halts | daily loss $8,000; account drop $2,000; the HALT file |
| Unwind files | `UNWIND_ONLY`, `UNWIND_ONLY_WAVES`, `UNWIND_ONLY_EXCEPT_WAVES`, `UNWIND_AT_MARKET.json` |

**How a change takes effect:**

| Change | Takes effect |
|---|---|
| Launcher env (`$ProbeEnv` in `run_incentive_mm.ps1`, or `env_overrides.txt`) | `restart_imm.ps1 -Task`, which hands the book over |
| Code in incentive_mm.py | the bot restarts itself on any mtime change, so every save is a deploy |
| STATUS_DIR files | reload by mtime at the next refresh (stop files about 60 s; HALT / UNWIND every cycle) |
| Dated `_NEXT` / `_FROM` pairs | switch on the clock |

**Recording:** `config_history` holds the env per run. The review's `config_manifest.jsonl` holds the reloadable files' history. Commits carry Jack's quoted words.

## 5. Counterfactual method and its biases

Rows come from the dashboard's hourly `classify_and_estimate` (the bot's own estimator, run under the launcher env), plus `imm_unquoted` for series not in the allowlist.

Reason classes:

| Class | Covers |
|---|---|
| deliberate | blocklisted, frozen, excluded family, yielded to manual |
| not enrolled | not in allowlist |
| capacity | the cap or budget |
| economics | under payout floor, under rate floor, zero est yield, admission clock, no-new |
| market screen | screened: extreme_mid, one_sided, ... |

Known biases (all point the same way, so treat raw estimates as upper bounds):

1. They assume full-day coverage and no competitor response. Short-window churny books top the ROI list.
2. A single reading is often a spike: admission estimates realised 0.71-0.74x of themselves.
3. Books before a program starts are thin, so upcoming estimates lean high.
4. Totals add up independent markets. That is not a feasible portfolio: caps, slots and per-event room are ignored.
5. The average tape maker's P&L understates our pick-offs. On the 10/26 gas test, makers lost 3.2c a contract and we lost 31c.
6. Unmeasured pool (R-261124-21, -22).
   - Until 11/25 the gap scan read about 210 books, so its `unest_pool` ($65k/day on 11/24) mostly counted markets that other tools already estimate. The bot estimates every allowed candidate on each refresh (`selection_snapshot`), and the not-enrolled scan scores every series outside the allowlist.
   - Only `counterfactual.unmeasured.unmeasured_pool` goes unread by all three. On 11/24 that was $24.7k/day, $20.2k of it blocked on purpose. The allowed markets nobody quotes were worth $35/day, all from markets under $1/day each.
   - Since R-261124-22 the gap scan reads every unquoted book, 100 per call (11/25: 4,377 books in 44 calls, 15 s), so `unest_pool` should be near 0. If it is not, `batch_error` or `book_calls` in the universe line says why.
   - Blocked families now carry a modelled price (`by_class.deliberate`: $111/day gross at x1 on 11/25, before realisation and trading tax). Report the price of a block as a decision for Jack, never as money left on the table.

Calibrate: each day's top predictions are logged, and when those events get quoted later the realised modelled $/day over the predicted $/day sets the factor (median, clamped 0.05-2.0).

## 6. Probing incentive_mm without lying to yourself

- Any local import of incentive_mm must mirror the launcher env first:
  - parse `$ProbeEnv = "(set ...)"` out of `run_incentive_mm.ps1` (the second, non-empty assignment);
  - `os.environ.setdefault` each `set K=V&&` pair;
  - then import.
- `import imm_quote_gaps` does this (`_apply_launcher_env`) and logs "mirrored N launcher env vars": N=0 means wrong numbers.
- Prefer the bot's real methods (`IncentiveMarketMaker(client, live=False)._estimate_candidate_yield`) over re-implementations.
- Prefer production sinks over local reconstructions.
- `STATUS_DIR` defaults to the LIVE path even from a worktree, so a probe can write live caches. Set `IMM_STATUS_DIR` to a scratch copy for previews.

## 7. Did a change work?

- `python imm_review.py measure --pattern '<regex on ticker>' --split <YYYY-MM-DD[THH:MM]> --before 7 --after 7` (or `--family "<name>"`).
  - Output: per-day net for each side, the means of rewards / trading / net / fills / resting / mark-out, `delta_net_day`, a Welch t-stat, and rewards per unit of program supply (`program_history.jsonl`).
  - It excludes the split day itself.
- **Before/after is not causal.** The confounders here:
  - waves starting or ending (the 109-series program end on 11/15 halved modelled rewards overnight);
  - program supply (renewals paused around 10/28);
  - weekends and the hour multipliers;
  - other changes shipped the same week (check `git log` for the family);
  - one big settlement.
  Normalise by supply, compare against a family that didn't change, and say which confounders apply.
- **Verdict rules:** verified needs the effect to meet the item's own success rule, a |t| around 2 or more (or an effect larger than the day-to-day sd), and agreement after supply normalisation. Otherwise inconclusive, and say what would settle it.
- **Holdouts settle what before/after can't.** The repo has the pattern: hash-split series (the 10/26 size test), random control trips (the sweep breaker's 20% `control` mode), a dry-run sink. Propose one when the expected effect is under about 1 sd of daily net.

## 8. Invariants the collector checks (add one for every bug class the review finds)

| Invariant | Signal |
|---|---|
| Dashboard tie-out | summary day row = archive sums within $1 |
| Bot liveness | cycle gaps beyond 10 min (dark minutes); health-alert DOWN outside restarts |
| Persistence | `state save failed` lines; stale `.tmp` files; muted sinks |
| Data sinks | each daily sink present and within 0.3-3x its 14-day median |
| Repo | sync streak; unpushed commits; dirty tracked files in the live tree; prunable worktrees |
| Machine | disk days-to-full; research-token age; memory |
| Ops | failing scheduled tasks (new vs chronic); task-log errors |
| Selection | config gaps (fail-closed); floor misses |

To add one: propose it as a finding of kind `review` with the check spelled out. Once Jack approves, apply mode adds it to `imm_review.py` with a test.

## 9. Known traps

- **Two clocks.** The bot logs true UTC; the launcher and the sync log use local time (the launcher with a fake Z). Sinks are per UTC day; the review day is ET (05:00Z to 05:00Z in winter).
- **Log files are named by launch date,** not rotated at midnight.
- **Status counters reset per process.** A restart zeroes errors_today and alerts_today.
- **`! N position(s) unmarkable (valued at cost)`** fires once per cycle. It is chronic (about 2,400 a day), not new; positions valued at cost hide their P&L.
- **The fills ledger join was about 60% and biased before 10/04.** Use the orders-sink replay for history.
- **Accrued estimate vs credited.** `accrued_est` per ticker is deleted when a market goes unquoted and flat, so never sum it with credits. Stale accruals (more than 14 days) are not pending payouts.
- **Monitor traps:**
  - join wait's stored days are never re-scored, and a cut-short tape read is alphabetical;
  - the toxic-halt and lone-ref nets use the public tape;
  - Carbon Arc's PROBLEM means unwinds.
- **The 7:00 portfolio email's IMM section** times out at 900 s on heavy days, and the section is then missing (11-18, 19, 21, 24, 25).
- **Task Scheduler codes:**
  - 267009 = running;
  - 267011 = not yet run;
  - 0x800710E0 on a Running task = a new instance was refused (benign);
  - 267014 = killed (a time limit or a closed console).
- **The sync is ff-only.** Unpushed commits on the box's main plus the nightly high-temp digest push to GitHub main make them diverge, and the sync then fails every 30 minutes until someone merges (11/20-11/25).
- **The cc-plugin-privacy hook** refuses Bash commands that name files containing Jack's email address (incentive_mm.py, sms_*.py, send_alert_email.py, ...). Use the Grep and Read tools on those files.

## 10. Tools worth knowing

| Tool | Use |
|---|---|
| `python imm_review.py collect / ledger / measure` | this review |
| `python imm_dashboard_verify.py` | P&L method audit (signed reads; exit 1 = a method gap) |
| `python imm_book_depth_read.py` | L2 history |
| `python imm_feed_audit.py` | the excluded-but-paying sweep; for any selection bug, sweep the class |
| `send_portfolio_digest.imm_advice` | the 7:00 email's per-family SHRINK / PAUSE / KEEP calls; compare, don't duplicate |
| `imm_risk_controls.py` | each control TRIPPED / BINDING / CLOSE / SLACK vs its usual level |
| `imm_reward_recon.py` | the reference reward maths; `--statement` ingests Jack's paste |
| Monitor `build_report(day)` functions | toxic halts, lone-ref, join wait, bol, comp lead, rt-settled, cheap. They save their stores, so call them only in apply mode |
