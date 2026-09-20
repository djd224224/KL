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
| `cycle_log_*.csv` | per full cycle | book panel + quote shape (35 cols) |
| `fastlane_*.csv` | per fast-lane cycle | same schema, 5s resolution |
| `scan_perf.json` | daily 07:55 ET, overwritten | the MEASURED historical ROI of every event root / series / family the open-scan tier traded (45 d) with its block / allow verdict; the table the bot hot-reloads |
| `scan_perf_history.jsonl` | one row per scorer run | did the tier's historical ROI actually move? the only place that survives a sink rotation |
| `scan_perf_cache.json` | per scorer run, overwritten | private per-file-signature cache; makes the 45-day re-read a 20s job instead of a 155s one |

### fills

One row per fill, carrying three joins that cannot be reconstructed later:

- **ledger** — the order this fill hit is still in `state.ledger` at that
  moment: `our_price_cents`, `our_remaining_before`, `is_pad`,
  `order_age_secs`, `client_order_id`. One dict lookup away, previously
  dropped. Past 7 days `our_order_ids` prunes and even *ownership* of a
  historical fill becomes unprovable on a shared account.
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
`scan_top_n`, `budget`, `gone`, `perf_roi`, `perf_barred`, plus any `_screen()`
reason.

`perf_roi` and `perf_barred` are the open-scan performance loop (2026-09-18/20,
`INCENTIVE_MM_HANDOFF.md`). `perf_barred` = an admission reject: the unit that
judges the candidate (its event ROOT, else its series, else its grouped family)
is BLOCKED — MEASURED historical ROI under 0.00/day on >= 100 $-days of
history. It never applies to a member (members do not reach
`_scan_admission`). `perf_roi` = a scan candidate whose MODEL ROI cleared
`SCAN_MIN_ROI` but whose BLENDED ROI (the judging unit's history blended into
the model at m = 200 $-days) did not, so the walk cut left the seat EMPTY; it
has its own `scan_skips` counter rather than hiding inside `scan_top_n`. It is
an **UPPER BOUND on the loop's cost, not a measurement**: `_group_walk_cut`
breaks out on the group cap and continues on the per-event cap BEFORE it
reaches its `min_roi` test, so at a full tier a candidate the cap cut carries
this label although the loop never touched it (REPRODUCED against the real
walk: 30 labelled, 0 caused). Both are ARMED by default
(`IMM_SCAN_PERF_BLOCK=1`, `IMM_SCAN_PERF_BLEND=1`).

### scan performance (2026-09-18/20)

`imm_scan_perf.py` runs daily at 07:55 ET, reads this directory and writes
exactly three files into it. It is the only writer of those three, it touches
no other sink, and it never calls `imm_reward_recon.rebuild_estimates()` (that
rewrites `reward_est_cache.json` unconditionally and would fight the 07:50
`KL imm program-history` job).

- **`scan_perf.json`** (`version: 2`) — *what has each event root / series /
  family the open-scan tier traded actually earned per $-day at risk, and
  should a new listing of it be admitted?* One table per run: `params` (the
  floor, threshold, TTL, clips, rent factor and `family_groups`), `window`,
  `coverage`, a `tier` block (`roi_hist` = (rent_used + realized + mtm) /
  risk_days with `roi_hist_trading` and `roi_hist_measured` beside it;
  MODELLED floored rent and MEASURED credits kept separate; `mark_source_mix`
  AND `mark_source_detail`; the DIAGNOSTIC markout at 1h/24h/72h, each with
  its own maturity cutoff; `recon_seeded_tickers` / `recon_seeded_frac`;
  reconciliation delta), then the three unit blocks the bot reads — `events`
  keyed by EVENT ROOT, `series`, `families` (with `members`, `match` and
  `note`) — each record carrying `n_markets`, `n_events`, `risk_days`,
  `rent_used` + `rent_basis` + `rent_measured_dollars`, `credited_measured`
  (its own field, never an addend), `realized_dollars`, `mtm_dollars`,
  `roi_hist` (+ trading / measured), `verdict` (`insufficient` / `block` /
  `allow`), `reason` and `until` on a block — plus `markets` (never read by
  the bot), `limits` (`blocked_events` / `blocked_series` / `blocked_families`
  / `n_units` / `max_records`), `unmatched_block_keys` (blocks whose unit
  produced no scan candidate in 24 h), `audit` (the IN-SAMPLE counterfactual,
  labelled as such), `warnings` and `notes`. Written atomically (tmp +
  `os.replace`). The bot reads `version`, `events`, `series`, `families` and
  `limits`; everything else exists so drift is visible in one file, not
  because anything acts on it.
- **`scan_perf_history.jsonl`** — *did the tier's historical ROI actually
  move?* One tier-summary row per run (`generated_at`, `window_days`,
  `risk_days` and `risk_days_at50c`, `net_dollars`, `realized_dollars`,
  `mtm_dollars`, `rent_used` / `rent_modelled` / `rent_measured`,
  `credited_measured`, `roi_hist` / `roi_hist_trading` / `roi_hist_measured`,
  `markout_c_per_ct_24h`, `fills`, `episodes`, `episodes_matured`,
  `unmarked_frac`, `mark_source_mix` / `mark_source_detail`, `n_block_events`
  / `n_block_series` / `n_block_families`, `n_units`, `reconciliation_delta`,
  `exit_code`). This is the durable series the re-measurement is read off, and
  the only place that answer survives a sink rotation. It is also the
  previous-run reference for the exposure-drop guard.
- **`scan_perf_cache.json`** — *private to the scorer.* Per-file-signature
  (`path`, size, mtime) cache of COMPLETE UTC day-files only; today's partial
  day is recomputed from scratch every run and never cached. MEASURED: a cold
  run over 45 days of `cycle_log_*.csv` is ~130 s, a warm one ~20 s, cache
  11 MB. Deleting it costs one slow run and nothing else.

**What the BOT stamps, and what it costs.** Nothing on a row no unit judges.
On a scan row that a unit DOES judge, five fields: `perf_unit`
(`event:KXCPIYOY` / `series:KXAXP` / `family:FISCAL_KPI` — the most specific
unit with >= 100 $-days of history), `perf_roi_hist` (that unit's MEASURED
historical ROI per $-day), `perf_n` (its $-days), `perf_v` (`block` /
`allow`) and `perf_roi` (the blended ROI the walk actually tested; absent
when the blend is off). No unit -> no fields, which is the
third guarantee above: `selection_events` wrote **56,720 rows / 19,406,254 B**
on 2026-09-15 [MEASURED], and only the scan rows inside a judged unit (48 units
with a verdict today, of 203) carry the ~90 B.

There is deliberately **no `perf_gen`** on a row. The table's `generated_at`
is constant for a whole refresh and is already published in the status file's
`scan_perf` block and in the per-refresh `scan perf table:` log line, so a
per-row copy bought nothing. To attribute a row to a table, join on the
refresh.

`cycle_log_*.csv` gained **one** column, `perf_roi_hist`, appended at the END
of the header and the row builder (34 -> 35 columns, 2026-09-20; blank when no
unit judges the market). Same value, same discipline; see the append-only trap
below.

A run that trips one of the two writer guards — cent-exact reconciliation
against the `realized` sink, tolerance $1.00 (exit 2), or a >30% day-over-day
drop in tier `risk_days` (exit 3) — writes **no file at all**, appends no
history row and prints why. A missing or stale table is the fail-open path
everywhere downstream: the bot treats every unit as unknown, the emails print
"no table".

## Two compatibility traps, both tested

**Cycle-log columns are APPEND-ONLY.** `imm_reward_recon.py` reads the file
positionally (`row[0],[1],[7],[8],[11]`) and gates on `len(row) >= 13`.
Verified against 60k rows of production data: 579 markets, accrual identical to
8 decimal places, narrow vs widened. Never reorder these columns.

`perf_roi_hist` was appended as the 35th column on 2026-09-20 for the same
reason the other 21 were appended in place rather than slotted in: the
reconciler's five positional reads are all under index 12 and do not move.
Pinned by `test_cycle_log_column_is_appended_last`, which asserts
`perf_roi_hist` is the FINAL header column and that the 34 pre-existing
indices are unchanged. Nobody has yet run `imm_reward_recon.py` end to end
against a file the 35-column bot actually wrote.

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
- `imm_scan_perf.py` -> `scan_perf.json` + `scan_perf_history.jsonl` (see
  above), the daily open-scan scorer. Read-only over this directory apart from
  its own three files; `--dry` prints the whole table and writes nothing, not
  even the cache. Registered as `KL imm scan-perf` with its trigger PINNED at
  07:55 ET inside `register_imm_scan_perf.ps1` — never derived from a sibling
  task, which is how `KL imm new-programs` silently slid back to 07:30.
- `imm_health_alert.py` emails when the newest credit in `reward_credits.csv` is
  more than 3 days old. That ledger is the only record of money actually paid
  and has no API behind it — the human pasting the statement *is* the archive
  job, so it needs a monitor.

## Still open

- **Storage.** The widened cycle log roughly doubles to ~160 MB/day, on a
  directory with no rotation and no off-box backup. Do not gzip
  `cycle_log_*.csv` in place: `imm_reward_recon.py` globs and signature-caches
  them, and compression would break it.
- **BigQuery.** These are still flat files, so every analysis is a pandas scan
  rather than SQL. An `imm_bq_load.py` daily job would fix that — the dataset is
  in `northamerica-northeast1` and needs explicit expiration clearing given the
  two prior incidents.
- **`STATUS_DIR` sits in a disposable worktree** that has been wiped once. It is
  already env-overridable; moving it is one edit to `run_incentive_mm.ps1`.
