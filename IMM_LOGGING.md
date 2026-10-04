# IMM analytics logging

What `incentive_mm.py` records for analysis, added 2026-09-06. Before this the
bot fetched fills, booked them into memory and dropped the dict; settlement —
where nearly all of its trading P&L lands, since it is a maker that almost
never closes a position — existed only as prose in a 1.1 GB stdout log.

Everything below lives in `STATUS_DIR` (`run-logs/incentive-mm/`, override with
`IMM_STATUS_DIR`). All JSONL files are one append-only file per UTC day, so a
day of any stream is one `bq load` or `pd.read_json(lines=True)` away.

**Kill switch:** `IMM_ANALYTICS=0` disables every JSONL sink at once.
`IMM_CYCLE_LOG=0` disables the two CSVs.

## Guarantees

Three rules hold for every sink, and they are why this is safe on a live book:

1. **No sink changes a trading decision.** Fills are booked into P&L *before*
   they are logged, so a broken analytics row cannot cost a fill.
2. **No sink raises.** Failures are swallowed.
3. **No sink floods the log.** A sink that fails mutes itself for the rest of
   the run after one line — on a live book a per-cycle failure would otherwise
   bury the trading messages that matter.

Every row carries `run_id` and `config_hash`.

## Streams

| File | Cadence | What it answers |
|---|---|---|
| `fills_*.jsonl` | per fill | realized edge, adverse selection, pad economics |
| `orders_*.jsonl` | per order event | fill rate, queue position, ladder shape, churn |
| `settlements_*.jsonl` | per settlement | where the P&L actually lands |
| `realized_*.jsonl` | per P&L change | per-market rent vs risk |
| `marks_*.jsonl` | 5 min, open positions | mark-out, MTM history |
| `selection_events_*.jsonl` | on decision change | why market X was not quoted |
| `selection_snapshot_*.jsonl` | hourly, all candidates | the counterfactual |
| `config_history_*.jsonl` | per run | which config produced which rows |
| `cycle_log_*.csv` | per full cycle | book panel + quote shape (34 cols) |
| `fastlane_*.csv` | per fast-lane cycle | same schema, 5s resolution |
| `book_depth/book_depth_*.jsonl.gz` | every book read | re-scoring ANY alternative ladder (since 2026-09-27) |
| `guard_skips_*.jsonl` | on guard change + hourly | why a managed market went quiet, and on what book |
| `floor_state_*.jsonl` | on member floor-state change | the hopeless clock and near-cliff, visible |
| `ws_stale_*.jsonl` | per stale-quote episode event | would a fast WS cancel have paid (2026-10-04) |
| `ws_sweep_*.jsonl` | per sweep-breaker trip | would pulling an event on a sweep have paid (2026-10-04) |

### fills

One row per fill, carrying three joins that cannot be reconstructed later:

- **ledger** — `our_book_side`, `our_price_cents`, `our_remaining_before`,
  `is_pad`, `order_age_secs`, `client_order_id`: the order the fill hit, **as
  it stood when the fill hit**. Since 2026-10-04 they come from
  `_fill_join`, which keeps every version the ledger gave the order
  (placement, amend, restart adoption) for `FILL_JOIN_KEEP_SECS` (1800)
  after the order leaves the ledger, and is handed over at a restart. The
  row takes the version in force at Kalshi's fill stamp, with the fill's
  price (a maker fill trades at our resting price) settling an amend inside
  the stamp's second. Two columns ride along:
  - `join_src`: `ledger` (this run's record), `handoff` (the run before a
    restart handoff), `derived` (no record of the order -- a restart that
    handed nothing over, a taker order: `our_book_side` from Kalshi's
    side/action, every other join column null).
  - `our_left_before`: what was left of the order just before this fill,
    i.e. `our_remaining_before` (the size as we last set it, not reduced by
    fills) less the order's earlier fills on that version. `count >=
    our_left_before` = the fill took the rung. A poll's batch arrives newest
    first and is registered whole before any row is written, so the order is
    right. Null when the version was set after the fill (an adopted order's
    gap fill with nothing handed over); `our_remaining_before` is then null
    too.

  **Rows before 2026-10-04 (from 9/6):** 29-48% of maker fill rows a day
  carry no join. The ledger let the order go before the next fills poll
  booked its fill: `_merge_ledger` dropped an order the resting read no
  longer held (it filled out), a cancel or amend of it got 404/409, our own
  cancel pulled what a partial fill left, or a restart started the ledger
  empty. On 10/4 to ~22:00Z: 87 / 26 / 23 / 93 of 231 unjoined rows (the 93 = 51
  adopted orders dropped the same ways + 42 restart-gap fills). They are
  mostly the fills that took the whole rung, so use Kalshi's side/action for
  the side (exact on every joined row) and count an unjoined row as
  full-rung. ~8% of the JOINED rows (37 of 489 on 10/3-10/4) carry a price
  and size set by an amend AFTER the fill. Every fill's `order_id` joins to
  a `place` row in `orders_*.jsonl` (100%, using day N and N-1), and
  replaying that order's `place` + `amend` rows up to the fill's `ts`
  recovers its resting price.
- **panel** — the book from the last cycle read (`ext_bid`, `ext_ask`,
  `yes_depth`, `no_depth`, `target`, `est_frac`, `qual_sides`,
  `pool_per_day`). A fill arrives one cycle *after* the read that produced the
  order it hit, so the cached panel is the correct context — and it costs no
  API call.
- **position** — `pos_before`/`after`, `avg_before`/`after`, so a fill is
  classifiable as opening, adding or reducing without replaying the tape.

### orders

`kind` ∈ `place` | `reject` | `uncertain` | `amend` | `cancel` | `gone`.

Two distinctions prose could not express, and both matter for a denominator:

- `uncertain` vs `reject` — an ambiguous timeout may have left a live untracked
  order, so it must be *excludable* rather than silently counted as unfilled.
- `gone` vs `cancel` — an order that vanished before we could pull it (404/409,
  i.e. filled or expired) is not one we chose to cancel.

`ticks_from_touch` is signed on our own side: positive = resting behind the
touch, negative = inside it. Cancels carry a `reason` (`cancel_all` /
`market_cancel` / `stray_unmanaged` / `requote_diff`) and `rested_secs`.

### selection

`selection_events` fires only on a *change*, which is what makes an incident
reconstructable — both times this bot silently benched hundreds of markets, the
state had to be recovered from prose after the fact. `selection_snapshot` dumps
every candidate hourly with its modelled yield, which is the only thing that
makes the counterfactual possible: realized credits on what was kept against
modelled yield on what each cut reason dropped.

Decisions: `selected`, `manual`, `book_unreadable`, `no_new`, `hopeless`,
`rate_floor`, `zero_yield`, `payout_floor`, `event_top_n`, `finecon_top_n`,
`scan_top_n`, `budget`, `gone`, plus any `_screen()` reason.

**Decision inputs (since 2026-09-27).** Selection rows also carry what each
floor rule looked at, so a rule change can be replayed rather than guessed:
`banked` (this program period), `period_base`, `period_start`, `floor_bar`,
`projected_total`, `reaches_min_raw` (before near-cliff can flip it) and
`reaches_min`, `peak` and `peak_age_s`, `qdays`, `est_total`, the hopeless
clock (`hopeless_since`, `sub_bar_secs`), `rate_bar` / `rate_est` (the $/day
the bar compares: the schedule-weighted floor rate since 2026-09-27) /
`rate_proj`, `exempt`
(the tier that bypasses a floor), `near_cliff_armed_ts`, `est_frac`,
`est_hour_mult` / `nc_size_mult` (`null` = the estimate never ran at a
multiplier), `floor_by_mult` (`[[mult, weight, $/day], ...]`; `null` = the floor
is the live estimate; a row whose per-side multipliers are not 1 -- the Carbon
Arc late-month cut, the bid-only quake family -- carries them as two more
entries, `[mult, weight, $/day, bid_mult, ask_mult]`, and the matching
`floor_mult_profile` token is tagged, e.g. `1/b0a0.5:0.210`), `cutoff`,
`program_end`, and `screen_waived` (the sticky waiver that kept a member). Fields only appear on candidates that reached pass
2. Selection rows never lose their existing keys: the email that reads them
substring-matches `"decision": "selected"` and `"is_scan": true`.

### book_depth

One row per book the bot reads -- managed markets every full cycle,
candidates every universe refresh -- with the **raw** Kalshi `orderbook_fp`
arrays (sub-penny exact; `orderbook_levels` floors and merges, so it is
lossy) plus our own resting size per exact level. Kalshi serves no historical
orderbook, so this is the only way to re-score a different ladder shape, size,
depth curve or sub-penny snap later. Read it with `imm_book_depth_read.py`
(`iter_day`, `competitor_levels`); plain `gzip.open` works too but raises on a
truncated tail member. Managed rows join `cycle_log` on `(cycle_ts, ticker)`.

- `unit` is `dollars` for the book arrays; own size is in **cents**
  (`own_yes_cents`, `own_no_cents`). Our ask at YES `p` is a NO bid at `100-p`.
- `own_src`: `resting_read` (managed; this cycle's exchange read, empty for
  an event an earlier sibling's event-wide cancel pulled this cycle) or
  `resting_prev_cycle` (candidate; one cycle old). For a market we quote,
  prefer the managed row.
- Sampled: ~every cycle for managed books, ~every refresh for candidates,
  against Kalshi's per-second reward snapshot. Re-scoring is still an
  estimate, and there is no trade tape or order identity in it.
- Dedup on read by `(ts, ticker, source)`.

### guard_skips

A market that an in-loop guard skips writes **no** `cycle_log` row that
cycle. These rows record which guard, what it judged (`inputs`), and the book
it judged. `kind`: `enter` (a guard starts holding a ticker, or it moves to
another guard; `prev` = the old one), `clear` (evaluated clean again;
`managed=false` means it left the book, and then the book fields are null),
`snapshot` (hourly, every held ticker), `cycle` (hourly counts per guard with
`n_managed` as the denominator). The key is `(ticker, guard)`, so input drift
while a guard holds shows only in the hourly snapshot. After a restart every
held guard re-emits `enter` with `prev=null`, which does not mean a new
episode.

Guards: `manual_grace`, `manual_yield`, `scan_evicted`, `cutoff_passed`,
`aaa_blackout`, `closing`, `breaker_cooldown`, `event_fill_tripwire`,
`scan_fill_tripwire`, `fill_burst`, `blind`, `event_depth_trip`,
`event_depth_hold`, `one_sided_breaker`, `scan_mid_tripwire`, `move_breaker`,
`crossed`, `wide_spread`, `band_both_out`, `cannot_qualify`, `rain_fair`,
`cutoff_extra`. Several are dead under the live env (breakers off, scan
tripwires at 0, `MAX_JOIN_SPREAD_CENTS=99`) and will simply never appear. A test
fails if a new `continue` is added to the quote loop without a row.

### floor_state

Members only. A member under the floor bar keeps decision `selected` while its
hopeless clock runs, so `selection_events` never showed the clock starting,
resetting, being carried by near-cliff, or the size boost arming. A row is
written when `(reaches_min_raw, reaches_min, near_cliff_boost_armed)` changes,
with `prev_state` and the full decision inputs. Its own file, so
`selection_events` still means one row per decision change.

### ws_stale (2026-10-04)

One row per event of a stale-quote episode: a resting rung the WebSocket
stale-quote check found strictly ahead of the external touch, on two looks
at least 1s apart (dry in shadow; the cancel itself with IMM_WS=on +
IMM_WS_FAST=1). `ev` is one of:

- `flag`: the episode opens, once per order and price. It carries `mode`
  (dry/live), `ticker`, `order_id`, `side` (bid/ask), `px` (our YES cents),
  `rem`, `age_s` (since placed), `ahead_s` (since first seen ahead), `phase`
  (cycle = the in-cycle check caught it, idle = between cycles; flags before
  the evening of 10/4 carry none),
  `ext_bid` / `ext_ask` (the external touch in cents, our own size taken
  out), `gap_c` (cents ahead of it), `yes` / `no` (the WS book's best 60
  levels as [cents, qty], deep enough to re-run the reward model) and `ours` (our orders on the market, as [side,
  cents, remaining]).
- `clear`: the touch came back level with or past the rung, so a fast
  cancel would have pulled it for nothing. It carries the new `ext_bid` /
  `ext_ask`.
- `amend` (`new_px`) / `cancel`: the cycle repriced or pulled it. A fast-path
  cancel carries `by: "fast"`.
- `gone`: it left the book with no cancel or amend of ours: filled out, or
  expired. Its fills are in `fills_*.jsonl`, by `order_id`.

Every end row carries `stale_s` (seconds since the flag). A cycle's `amend` /
`cancel` / `gone` row is stamped with that CYCLE's start time, so its
`stale_s` runs short; the exact amend / cancel time is the order's row in
`orders_*.jsonl` (ws_stale_score.py uses it). An episode can stay
open across a restart, because the new process starts with none. The check
runs only between cycles (the ~10s idle), so a rung that went stale
mid-cycle is flagged at the next idle. `ws_stale_score.py` turns a day of
rows plus the fills and cycle logs into the verdict on a fast path.
`IMM_WS_STALE_LOG=0` turns this file off.

### ws_sweep (2026-10-04)

One `trip` row per event the sweep breaker trips (SWEEP_BREAKER dry / on).
The trigger is a maker WS fill that took all that was left of our order. Each
row carries:
- `mode`, `event`, `ticker`, `order_id`, `side`, `px` (YES cents);
- `count` and `rem_before` (the fill and what was left of the order);
- `fill_ts` (Kalshi's), `trade_id` and `hold_s`;
- `pull`: our other resting orders in the event that the trip pulls (on) or
  would pull (dry), as [ticker, order_id, side, cents, remaining].

ws_stale_score.py scores the dry trips: our fills in the event inside
(trip + 2s, trip + hold], marked out, against the event's modelled reward
for the hold. `IMM_SWEEP_BREAKER=off` writes nothing.

### The `risk:` log line (2026-10-03)

Not a sink: one bot-log line per FULL cycle, right after the cycle summary,
written by `incentive_mm.risk_line`:

    risk: account value $27,862 (anchor $24,387, up $3,475 of $2,000 halt) | P&L today $-85.66 of -$1,200 halt | open-scan $+7.03 of -$200 budget

It records where the three daily halts stand, so the 7:00 email can report
each one's worst point in the day rather than its value at 7:00, an hour after
the roll re-anchors them all. `imm_risk_controls.py` parses it with
`RISK_ACCT_RE` / `RISK_PNL_RE` / `RISK_SCAN_RE`. Change the wording in both
places; `test_imm_risk_controls.BotRiskLineContractTests` fails if they drift.
The account part is missing when the floor check did not run that cycle or
read nothing. The open-scan part is missing while the tier holds no book.

### status `latency.ws` block

Not a sink: since 2026-10-03 `status_incentive_mm.json` carries
`latency.ws` -- the WebSocket feed (kalshi_ws.py) in shadow: feed health
(connected, healthy, books_ok, want, gaps, resnapshots, resyncs, errors,
msgs), the shadow compare of every REST book read against the WS book
(compared, exact, top, ws_missing), `check_in_cycle`, the dry stale-quote counts
(would_cancel, in episodes since 2026-10-04) and `stale_open` (episodes
open in `ws_stale_*.jsonl`). Since 2026-10-04 also `audit`, the REST
audit of the WS books: audits, rest_errors, trips, rearms, tripped,
tripped_at, last_trip, every, and the trip window's size and mismatch
counts (window, window_bad_top, window_bad_book). Since the same evening
`latency.sweep` (the sweep breaker's trips and counts) and `latency.cycle`
(the full cycles' REST read phase and length, medians of the last 30:
reads_s, run_s, period_s, last_reads_s, last_run_s). A live fast-path cancel
(IMM_WS=on + IMM_WS_FAST=1, not enabled) would carry the orders-row cancel
reason `ws_stale`.

## Two compatibility traps, both tested

**Cycle-log columns are APPEND-ONLY.** `imm_reward_recon.py` reads the file
positionally (`row[0],[1],[7],[8],[11]`) and gates on `len(row) >= 13`.
Verified against 60k rows of production data: 579 markets, accrual identical to
8 decimal places, narrow vs widened. Never reorder these columns.

**Fast-lane rows are NOT in the cycle log.** The reconciler derives each
market's accrual from the gap between consecutive *distinct timestamps*.
Interleaving 5-second rows would shrink those gaps and silently under-count the
reward estimate the whole realization factor rests on. They go to
`fastlane_*.csv`, whose name also stays outside the `cycle_log_*.csv` glob.

`2026-09-06` is a transition file: it opens with the old 13-column header and
carries a re-emitted 34-column header inline at each restart. The reconciler
already skips rows whose first field is `ts`. Files from 09-07 on are clean.

## Helper scripts

- `imm_reward_recon.py` → `program_history.jsonl`, a daily per-series roll-up of
  reward-program supply (~1,800 rows/day). `reward_programs.json` is overwritten
  each run, so there was no time series — which is why the KXTEMP pool removal
  around 2026-08-07 is prose in a memory file rather than data. Reward fields
  are named `_raw`: they sum the API's `period_reward` as-is, whose unit is not
  dollars and is not verified, so compare them across time for a series rather
  than reading them as currency.
- `imm_health_alert.py` emails when the newest credit in `reward_credits.csv` is
  more than 3 days old. That ledger is the only record of money actually paid
  and has no API behind it — the human pasting the statement *is* the archive
  job, so it needs a monitor.

## Kill switches

`IMM_ANALYTICS=0` (every JSONL sink, and the fills join's memory),
`IMM_CYCLE_LOG=0` (the two CSVs),
`IMM_BOOK_LOG=0`, `IMM_BOOK_LOG_CANDIDATES=0`, `IMM_GUARD_LOG=0`,
`IMM_SELECTION_INPUTS=0`, `IMM_WS_STALE_LOG=0`, `IMM_SWEEP_BREAKER=off`. Setting any of them in the launcher changes
`CONFIG_HASH`, like every `IMM_*` variable. Tests prove order placement is
identical with the logging on or off.

## Still open

- **Storage.** The book log roughly doubles IMM's disk growth (~385 MB/day
  before it; ESTIMATED 100-200 MB/day more for `book_depth`, possibly higher),
  on a directory with no rotation and no off-box backup. Retention should be a
  deliberate, registered decision, not a side effect. Do not gzip
  `cycle_log_*.csv` in place: `imm_reward_recon.py` globs and signature-caches
  them, and compression would break it.
- **Memory.** Queued books are held until the flush at the end of each cycle,
  which makes the bot's existing full-GC pauses roughly twice as frequent.
  Trading was unaffected in replay.
- **BigQuery.** These are still flat files, so every analysis is a pandas scan
  rather than SQL. An `imm_bq_load.py` daily job would fix that — the dataset is
  in `northamerica-northeast1` and needs explicit expiration clearing given the
  two prior incidents.
- **`STATUS_DIR` sits in a disposable worktree** that has been wiped once. It is
  already env-overridable; moving it is one edit to `run_incentive_mm.ps1`.
