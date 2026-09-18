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
| `scan_perf.json` | daily 07:55 ET, overwritten | what the open-scan tier actually traded, per structural cohort; the table the bot hot-reloads |
| `scan_perf_history.jsonl` | one row per scorer run | did the tier's loss rate actually move? the only place that survives a sink rotation |
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

`perf_roi` and `perf_barred` are the open-scan performance loop (2026-09-18,
`INCENTIVE_MM_HANDOFF.md`). `perf_roi` = a scan market whose GROSS `_raw_roi`
cleared `SCAN_MIN_ROI` but whose value net of the table's `req` did not, so the
walk cut left the seat EMPTY; it has its own `scan_skips` counter rather than
hiding inside `scan_top_n`. It is an **UPPER BOUND on the loop's cost, not a
measurement**: `_group_walk_cut` breaks out on the group cap and continues on
the per-event cap BEFORE it reaches its `min_roi` test, so at a full tier a
candidate the cap cut carries this label although the loop never touched it
(REPRODUCED against the real walk: 30 labelled, 0 caused).

`perf_barred` = an admission reject from a live series/event-root bar, and it never applies to a member (members do not reach
`_scan_admission`). Both are zero today: the loop ships with
`IMM_SCAN_PERF_WEIGHT=0.0` and `IMM_SCAN_PERF_BAR=0`.

### scan performance (2026-09-18)

`imm_scan_perf.py` runs daily at 07:55 ET, reads this directory and writes
exactly three files into it. It is the only writer of those three, it touches
no other sink, and it never calls `imm_reward_recon.rebuild_estimates()` (that
rewrites `reward_est_cache.json` unconditionally and would fight the 07:50
`KL imm program-history` job).

- **`scan_perf.json`** — *what has the open-scan tier actually traded, and what
  extra ROI should a newcomer of this shape have to earn?* One table per run:
  `coverage`, a `tier` block (markout c/ct MEASURED, MODELLED floored rent,
  MEASURED credits kept separate, `mu_trading_only` and `mu_rent_blended`,
  mark-source mix AND `mark_source_detail`, which splits tier 2 into the live
  cycle book and the `marks`-sink fallback because `mark_source_mix` folds both
  into `two_sided` per SPEC 2.4 and cannot see a drift toward the staler one;
  `rent_measured_dollars` beside `rent_measured_frac`; `markout_horizons`, the
  same statistic at 1h/24h/72h each with its OWN maturity cutoff, episode
  collapse, winsorisation and sample size; `recon_seeded_tickers` /
  `recon_seeded_frac`, the tickers whose cost basis the replay inherited from
  the bot; reconciliation delta), the four frozen cohort dimensions
  with both bases per bucket, per-series and per-event-ROOT records with their
  verdict, and `limits` / `audit` / `warnings` / `notes`. Written atomically
  (tmp + `os.replace`). The bot reads `params`, `cohorts[*].dev_trading`,
  `series`, `events` and `limits`; `markets`, `raw_*`, `audit` and `warnings`
  exist so drift is visible in one file, not because anything acts on them.
- **`scan_perf_history.jsonl`** — *did the loss rate actually move?* One
  tier-summary row per run (`generated_at`, window, basis, `risk_days` and
  `risk_days_at50c`, `mo_dollars`, `realized_dollars`, `mtm_dollars`,
  `rent_modelled`, `credited_measured`, both mus, `markout_c_per_ct_24h`,
  fills, `episodes` (every fill collapsed to (ticker, UTC hour)) and
  `episodes_matured` (the marked-and-matured subset), `unmarked_frac`,
  `mark_source_mix` and `mark_source_detail`, `n_down_rank`, `n_bar`,
  `reconciliation_delta`, `exit_code`). This is the durable series the
  2026-10-03 re-measurement is read off, and the only place that answer
  survives a sink rotation. It is also the previous-run reference for the
  exposure-drop guard and the ratchet.
- **`scan_perf_cache.json`** — *private to the scorer.* Per-file-signature
  (`path`, size, mtime) cache of COMPLETE UTC day-files only; today's partial
  day is recomputed from scratch every run and never cached. MEASURED: a cold
  run over 45 days of `cycle_log_*.csv` is 154.7s, a warm one 18.8-26.2s, cache
  9.1 MB. Deleting it costs one slow run and nothing else.

**What the BOT stamps, and what it costs.** Two fields go on every row that
carries a meta at all: `perf_adj` (the MIN over the interpolated cohort devs
and the series / event-root devs, <= 0.0) and `perf_req` (the extra $/day-per-$
demanded, >= 0.0, 0.0 at the shipped `IMM_SCAN_PERF_WEIGHT=0.0`). Three more go
on **non-neutral rows only**: `perf_v`, `perf_code` (ONE `|`-joined string with
the binding term first, e.g. `sp20|dtc61|p14` — never a list) and `perf_unit`
(the barring unit, when barred).

`perf_v` is `bar`, `down_rank` (`perf_req > 0`, i.e. something was actually
down-ranked) or **`would_down_rank`** (`perf_adj < 0` while `perf_req == 0`).
That third value is the whole observe-only phase: at `WEIGHT=0.0` nothing is
down-ranked, and the INTEGRATION unit MEASURED a negative `adj` on **360 of
384 (93.8%)** live scan candidates — stamping `down_rank` on all of them would
record an intention in the vocabulary of an action while the email is careful
to parenthesise the same quantity as NOT APPLIED.

There is deliberately **no `perf_gen`** on a row. The table's `generated_at` is
constant for a whole refresh and is already published in `imm_status.json`'s
`scan_perf` block and in the per-refresh `scan perf table:` log line, so a
per-row copy bought nothing and cost ~33 B on every non-neutral row. To
attribute a row to a table, join on the refresh.

That split is the third guarantee above, not politeness. **A meta-less row —
`decision: "gone"` — carries nothing at all**, and on the flood day those were
half the file. MEASURED read-only on 2026-09-15: **56,720 rows / 19,406,254 B**,
of which **28,954 carry a meta** and 27,655 of those are `is_scan`. The
always-on floor is +39 B on the meta rows = **+1,129,206 B = +5.8%
[MEASURED]**; the absolute ceiling, every `is_scan` row also non-neutral, is
**+13.1% [MEASURED denominators, MODELLED share]**. The realistic steady state
once the scorer's first table lands is close to the ceiling, **~+12.6%**, not
the floor — 93.8% of scan candidates score a negative `adj`. (2026-09-17 is the
same shape: 51,350 rows / 17,955,740 B, floor +5.9%, ceiling +12.7%.)

`cycle_log_*.csv` gained **one** column, `perf_req`, appended at the END of the
header and the row builder (34 -> 35 columns, 2026-09-18). Same value, same
discipline; see the append-only trap below.

A run that trips one of the three writer guards — cent-exact reconciliation
against the `realized` sink (exit 2), a >30% day-over-day drop in tier
`risk_days` (exit 3), or a clamp storm, >10% of devs sitting exactly at
±`dim_clip` (exit 4) — writes **no file at all** and prints why. A missing or
stale table is the fail-open path everywhere downstream: the bot goes neutral,
the emails print "no table".

## Two compatibility traps, both tested

**Cycle-log columns are APPEND-ONLY.** `imm_reward_recon.py` reads the file
positionally (`row[0],[1],[7],[8],[11]`) and gates on `len(row) >= 13`.
Verified against 60k rows of production data: 579 markets, accrual identical to
8 decimal places, narrow vs widened. Never reorder these columns.

`perf_req` was appended as the 35th column on 2026-09-18 for the same reason
the other 21 were appended in place rather than slotted in: the reconciler's
five positional reads are all under index 12 and do not move. Pinned by
`test_cycle_log_column_is_appended_last`, which asserts `perf_req` is the FINAL
header column and that the 34 pre-existing indices are unchanged. Nobody has
yet run `imm_reward_recon.py` end to end against a file the 35-column bot
actually wrote.

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
