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
`scan_top_n`, `budget`, `gone`, plus any `_screen()` reason.

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
