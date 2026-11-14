# Market wave reference

Lookup tables for the market-wave runbook (`SKILL.md` beside this file). Read the section you need.

## Tools (all in KL; the wave tools are untracked or uncommitted until Jack says "commit")

| Tool | Role |
|---|---|
| `wave_detect.py` | Detector + size-up (read-only). `--since --min-markets --min-pool --min-days-out --min-soon-days --min-events --json --update-state`. Exit 3 = wave, 4 = needs Jack, 0 = nothing. FEW = under 5 events; `INFO_RISK` title screen -> CHECK |
| `pvd_waves.py` | Wave registry and gates: `add [--auto --detect] / run [--only --wait-lock] / dash / report [--email] / setarg [--unset VALUE] / set / list / stop / remove / preflight [--budget --no-capacity] / pickoffs / darkcheck / datefix / pace / resize / whydark`. Presets `default`, `near`, `2028` (Jack only). Env: `PVD_WAVES_REGISTRY`, `PVD_PICKOFF_STATE`, `PVD_DARK_STATE`, `PVD_MIN_CASH0` (5000), `PVD_MIN_CASH_OTHER` (1000). Tests: `test_pvd_waves.py`, `test_pvd_waves_fair.py`, `test_pvd_wave_pace.py` |
| `pvd_wave_pace.py` | `pace` / `resize` / `whydark` (2026-11-14): the projection on the average share since a size landed, the $1-floor sizing with the two-read off rule, per-side "why not resting". Files: `run-logs/pvd-scan/size_mult_NAME.json` (the multiples `resize --apply` writes; the tune's `--size-mult-file`), `resize_state_NAME.json` (the last read, for the two-read rule) |
| `pvd_scan.py` | Scorer and lister. Any-wave mode `--series --no-vote-check --new-since --stop-cap --report`. Env `PVD_SCAN_LIVE/DEAD/DECIDED_SIZE`, `_BAR`, `_BUDGET` (every wave's live scan adds), `_OWN_BUDGET` + `_OWN_SERIES` (the wave's own budget against its own listings, 2026-11-14), `_CHEAP_TIERS`, `_TIER_SKIP`, `_PRICE_MIN/_MAX`, `_CUSHION`, `_FOLLOW_BOOK`, `_SIM_HOURS`. Tests: `test_pvd_scan_tiers.py`, `test_pvd_scan_budget.py` |
| `pvd_wave_tune.py` | Idempotent tuner: every lever in SKILL.md section 10, `--size-mult-file`, `--fair-file`, `--halts-on`, `--wave-added`. Tests: `test_pvd_wave_tune_mult.py` |
| `wave_fair.py OUT [--live]` | The FX + metals weekend fair (w1113): lognormal on Friday's close moved by 24/7 venues; CLOSE / T0 hard-coded to 11/13 -- update for a new weekend. Tests: `test_wave_fair.py` |
| `crypto_event_fair.py` | The bot's crypto *EVENT fair thread (fleet GBM, Kraken spot, every 15 s); status `run-logs/incentive-mm/crypto_event_fair.json`; stop file `crypto_event_stop.json` |
| `pvd_marginal.py` | What-if: marginal $/day per extra $1k by wave x band (read-only, ~12 min) |
| `imm_pvd_watch.py` | Wave dashboard. Env `PVD_WATCH_GO_LIVE / _DIR / _SERIES / _EXCLUDE / _PACE_BAR / _PACE_SPARE_H`, `PVD_WATCH_HOST / _TOKEN`. `imm_crypto_event_watch.py` wraps it for the crypto strikes (:8783) |
| `pvd_watch_keepalive.ps1` | Pass 0: restarts down or hung dashboards: every enabled registry wave, plus the fixed :8774 (11/04 books + other listings) and :8783 (crypto page). :8773 / :8771 retired 11/14 |
| `relight_watch.py` | What the relight tasks listed and the live program count (exit 3 = something); `new_waves` names a wave the check added |
| `wave_alert.py` | Every wave / relight / pick-off / dark / bait alert in one layout (`--send`); pick-off lines carry the fill time. Tests: `test_wave_alert.py` |
| `bait_watch.py` | Pass 10: the bot's bait defenses on wave markets. Tests: `test_bait_watch.py` |
| `imm_heightened_report.py` | Scores heightened bait mode (dry) from its narrowed go-live: `--since 2026-11-14`, from a worktree |
| `sms_alert.py` / `send_alert_email.py` | Text Jack (T-Mobile gateway, rate-capped; `--long` <= 900 chars) / email |
| `wave_pocket.py` | Wave Pocket builder (skill `wave-pocket`): "Today #N" tabs, trims older tabs to the 256 KB cap |
| `run_pvd_scan_waves.cmd` / task **KL pvd-scan waves** | The relight task (passes 0, 6-10; 1-5 retired) |
| `run-logs/incentive-mm/post_vote_dead_books.json` | THE list. Hot-loaded. Write atomically under `run-logs/pvd-scan/scan.lock` |
| `run-logs/pvd-scan/wave_pass_NAME.lock` | A wave's pass lock (held by `run --write`; setarg / set / stop wait for it; stale after 25 min) |
| `kalshi_reads.kalshi_get(_all) / read` | Signed list reads (public list reads 429 from this box); one market / order book public first |
| `kalshi_subaccount.py shard <$> --from 0 --to N` | Shard transfer (Jack runs it; it asks for `MOVE`). `shards` prints the balances |

## List entry fields

**Read by the bot:**

| Field | Meaning |
|---|---|
| `bid_max`, `ask_min` | Ints. With `follow_book` they always stand; else last trade +- cushion once traded |
| `stop` | ISO UTC, required. Replaces an election cutoff; elsewhere only shortens it (`override_cutoff` to replace) |
| `size`, `bid_size`, `ask_size`, `max_position` | Rung sizes. The side's position limit = its size. A side OFF = `bid_size` / `ask_size` 0; `"size": 0` alone = the bot's default (PVD_SIZE, 40) |
| `kind`, `cushion`, `price_min`, `price_max` | dead/live; cushion; band (default 3-97; a listed market's band also governs the bot's entry screen and quote loop since 38f07e9 / 0f12693) |
| `flip_through`, `flip_to_flat` | Reducing side = size + position, or max(size, position) |
| `follow_book` | Listed limits stand even with a last trade |
| `no_halts` | Its series joins PVD_NO_HALT_SERIES (even after the entry expires) |
| `wave_added` | The registry wave's creation time, stamped by the tune: UNWIND_ONLY_WAVES lets a flat stamped market quote |
| `no_losing_side`, `event` | Decided markets' losing side rests only a closing rung; FORCE_EVENTS |

**Read by the tools only:** `period_start`, `added`, `coll_usd`, `size_mult`, `scaled_at`, `pre_bank`, `pre_off` (sizes before a 0 multiple), `cheap_sides`, `cheap`, `bank`, `widened`, `anchored`, `anchor_touch`, `anchor_capped`, `fair_clamp`, `pinned`, `unpinned`, `tpause`, `paused`, `prev_period`. The top-level `stopped` = {ticker: {at, reason, entry}}: the scanner skips them until a LATER period. `tune_last` = the last tune summary.

**Decided markets** (last TRADE >= 90 / <= 10, `IMM_PVD_DECIDED_HI/LO`): the decided side bypasses limits AND band. Cap it with `--side-cap`.

## Bot guard knobs (launcher `run_incentive_mm.ps1` $ProbeEnv or code defaults; a launcher change needs `restart_imm.ps1 -Task`)

| Knob | Live (11/14) |
|---|---|
| `IMM_JOIN_WAIT`, `_SUBPENNY`, `_REST_MEMORY_H` | on, on, 24 |
| `IMM_PVD_OFF_USUAL`, `_C`, `_HOURS`, `_COLD` | on, 3, 6, base (`IMM_CRYPTO_WAVE_COLD_SIZE` 400 for crypto strikes) |
| `IMM_PVD_CLOSE_CLAMP`, `_C` | on, 5 (breakeven or better also rests) |
| `IMM_UNWIND_WAVES_HELD`, `IMM_UNWIND_WAVES_EXEMPT_NEW` | on, on |
| `IMM_UNWIND_WAVES_AT_COST` | 0 (85a5d8e: the outside price, at most 5c past usual or at cost where further, never leading; 1 = the old breakeven floor) |
| `IMM_RESTART_KEEP_WAVE_UNWIND` | 1 (95eef3b: held wave closing orders ride restarts) |
| `IMM_RESTART_HANDOFF_MAX_AGE_SECS` | 1200 (38f07e9; was 300) |
| `IMM_LONE_REF_PVD` / `IMM_LONE_REF_GUARD` | on (wave list) / dry (rest of the bot) |
| `IMM_TWIN_CHECK`, `_TOL_C` | on, 2 (6fbad53) |
| `IMM_SWEEP_GROUP`, `_MIN_EVENTS`, `_MIN_CT`, `_HOLD_MIN` | on, 2, 200, 20 (76d9201, 25cbd54) |
| `IMM_SWEEP_BREAKER`, `IMM_SWEEP_HOLD_SECS` | on, 600 |
| `IMM_TAPE_TRIGGER`, `IMM_TAPE_WAVE_MIN_CT`, `IMM_WAVE_BREAKER` | ab 100%, 200, on |
| `IMM_HEIGHTENED` | dry (b41c0d4) |
| `IMM_MARK_EXTERNAL`, `IMM_PVD_FULL_EARN`, `IMM_PVD_ZERO_OFF` | on, on, 1 |
| `IMM_CRYPTO_EVENT_FAIR`, `_LEAD`, `_TOL_CENTS`, `_SPOT_BAND`, `_JUMP_PCT`, `_JUMP_HOLD_SECS` | on, on (listed strikes take no lead), 2, 0.005, 0.01, 600 |
| `IMM_MAX_TOTAL_RESTING` | 12000 (the own-orders read covers 40,000: 757614b) |
| `IMM_MAX_PLACEMENTS_PER_CYCLE` | 1500 (e403df0); `IMM_PLACE_PRIORITY` 1 (60c237d: dark sides first) |
| `IMM_ORDER_TTL_SECS` / `IMM_ORDER_REFRESH_SECS` | 1800 / 1500 (renewals use most of the placement cap) |
| `IMM_ATREF_COUNT_TOL` | 0.2 (a resting order within 20% of target is not amended) |
| `IMM_COLLATERAL_BUDGET` | 3000000 (ea5e796; was $1M) |
| `IMM_MAX_MARKETS` | 1000 events |
| `IMM_DAILY_LOSS_LIMIT` / `IMM_NET_LOSS_LIMIT` | 8000 / 0 |
| `IMM_COMPANY_KPI_BLOCK` | on |
| `IMM_PVD_EVENT_CAP` | 10M (no cap) |

## Deploy safety (bot code)

1. Worktree off origin/main (not under `%TEMP%` for long work: cleanup deletes tracked files).
2. Tests with `IMM_STATUS_DIR` set to a NON-EMPTY scratch dir; never hand-run test fixtures.
3. Full suite: compare failure SETS with a clean main run (the known failure: a CPI test).
4. Push; `schtasks /run /tn "KL sync-kl-main"`; `restart_imm.ps1 -Task -Now` for a launcher change (`-NoHandoff` when halted). Orders rest UNMANAGED from the exit until the relaunch adopts them: ~1-2 min on a good restart; at 13:05Z 11/13 a low-RAM start took 426 s and, past the old 300 s limit, cancelled the whole book (the limit is 1,200 s since 38f07e9).
5. Other sessions restart the bot too: check the log before and after. During a short-window wave, batch deploys (8 restarts the night of 11/14; each re-queues the book and reads ~15 min low).
6. Before a deploy that changes how a live position unwinds, set the list so the OLD code already rests the safe size.
7. The wave tools run live from KL with no restart (each pass starts a new process). Test them in a scratch copy or worktree, check that KL's copy hasn't changed since you read it (other sessions edit them), then copy. Run the wave test modules one at a time: `test_pvd_waves` / `test_pvd_wave_pace` stub `incentive_mm` / `kalshi_reads` in sys.modules, which breaks tests that import the real ones after them.

## Gotchas

- Public list reads 429. Use signed reads for lists; public only for single markets and books.
- The scan's projection is optimistic: 11/04 ~2x, 11/06 ~2.7x; the first-hour rate of every 11/11-11/14 wave fell 30-80% within a day.
- The scanner rebuilds a relit entry from scratch: keep settings in the registry, which every relight inherits.
- Settings a pass reads mid-run: since 2026-11-14 the pass lock serializes setarg with a running pass; values that must never revert (a fair margin) go in the file the tune reads at run time.
- Bash heredocs halve backslashes: write patch scripts with the Write tool. Windows Python can't open a Git Bash `/c/...` path: pass `C:\...` paths to `python -c`.
- Dashboard rate vs listed size: after a size change the rate reflects the old size for ~25 min; its per-market share is ONE cycle's (use `pace`).
- Cycle-log rows land in batches and lag a cycle: a just-turned-off market can still show resting in the latest row. The bot log's order lines are faster.
- Sub-penny books: the guard judges `price_exact`; whole-cent limits only on unwinds.
- After 00:00Z, `orders_ / fills_ / cycle_log_ / selection_events_` files roll to the new UTC date; the day log keeps its start-date name.
- Two sessions editing the list within minutes collide: take `scan.lock`, re-read, write atomically.
- National / cross-region markets move with every region.
- **Listed is not selected:** the list gets a market past the allowlist; the bot's screens still apply. Count selected vs listed (`selection_events_<date>.jsonl`, or `whydark`).
- **Live-feed series have no fair in the bot** (SCAN_EXCLUDE_PREFIXES: FX, indexes, metals, crypto prices) except the crypto *EVENT gate: quote them only with a fair.
- **The resting-order read must stay far above the resting cap** (40 x 200 = 8,000 under a 12,000 cap doubled 3,462 sides on 11/14).
- **Placement cap vs renewals:** every order renews every ~25 min, so ~9,000 resting orders need ~1,000 placements per ~3.6-min cycle just to stand; a big listing waits behind them (dark sides go first).
- **The collateral budget charges selected markets, not list sizes:** turning list markets off frees nothing while they stay selected.
- **The pick-off watch's 6 h lookback** alerts a fill the first time it crosses the bar, possibly hours after the fill; three such fills in a series pin the whole series at base.
- **Wave Pocket's page holds 256 KB:** its builder trims the older tabs first; the :8774 "Since 11/06" tab overlaps the newer registry waves (:8773 / :8771 retired 11/14).
