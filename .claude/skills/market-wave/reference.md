# Market wave reference

Lookup tables for the market-wave runbook (`SKILL.md` beside this file). Read the section you need.

## Tools (all in KL; committed at 09225c9 on 11/15 -- later edits are committed only on Jack's "commit")

| Tool | Role |
|---|---|
| `wave_detect.py` | Detector + size-up (read-only). `--since --min-markets --min-pool --min-days-out --min-soon-days --min-events --json --update-state`. Exit 3 = wave, 4 = needs Jack, 0 = nothing. FEW = under 5 events; `INFO_RISK` title screen -> CHECK; `KNOWN_SHAPES` (11/14: Elections, Politics) -> any other FIT / NEAR is NEW (Jack's call, texted); LISTED = the list or an enabled, uncapped registry wave. `--upcoming [--horizon-hours 72] [--preflight]` (pass 11, 11/14): the upcoming programs judged per 30-min start window as the check will at the start; exit 3 = a wave would be greenlit, 4 = your call, 5 = a registry relight only; state `run-logs/pvd-scan/wave_upcoming_state.json`. Tests: `test_wave_detect_livefeed.py`, `test_wave_detect_upcoming.py`, `test_wave_detect_shapes.py` |
| `pvd_waves.py` | Wave registry and gates: `add [--auto --detect] / run [--only --wait-lock] / dash / report [--email] / setarg [--unset VALUE] / set / list / stop / remove / preflight [--budget --no-capacity] / pickoffs / darkcheck / datefix / pace / resize / whydark`. `set NAME KEY VALUE` keys: `scan_size port budget enabled floor_auto series near stop_cap window floor_max_mult floor_trim floor_tier scan_max_mult` (`scan_max_mult` 1-5, needs a `--size-mult-file`; 4c69bb8. `floor_trim` on|off needs `floor_auto`; ae46e39. `floor_tier C|off` needs `floor_auto` and a `--cheap-tiers`, C past its last tier up to 25; it adds / drops `--tier-file`; 5e2f061). `setarg` replaces a non-repeatable flag (e.g. `cheap-tiers 2:10,5:5,15:5`) and waits for a running pass. Presets `default`, `near`, `2028` (Jack only). Env: `PVD_WAVES_REGISTRY`, `PVD_PICKOFF_STATE`, `PVD_DARK_STATE`, `PVD_MIN_CASH0` (5000), `PVD_MIN_CASH_OTHER` (1000). Tests: `test_pvd_waves.py`, `test_pvd_waves_fair.py`, `test_pvd_wave_pace.py` |
| `pvd_wave_pace.py` | `pace` / `resize` / `whydark` (2026-11-14): the projection on the average share since a size landed, the $1-floor sizing with the two-read off rule, per-side "why not resting" (the bot log's crypto- and fx-event stand-aside lines included). Statuses that are NOT judged (no size-up, no off): `landing` (resting under 90% of the bot's want), `held` (the bot WANTS under 90% of the list's size on a side it quotes: its first hour or its off-usual cut; c30a81f), `restoring` (a lifted 0 the tune hasn't applied; 3317216), `no rows` (none since the entry's last `scaled_at` / `unpinned`); `off` (multiple 0). `whydark --json` rows are per SIDE (`ticker`, `side`, `kind`, `reason`). `floor_auto` (pass_resize) needs a live fair: the wave's fresh `--fair-file`, or with none the bot's own (`BOT_FAIRS`: `crypto_event_fair.json`, `fx_event_fair.json` written within 5 min and listing the wave's series, 11/15); the registry's `floor.max_mult` (`set NAME floor_max_mult X`) caps its size-up. TRIM (ae46e39, the registry's `floor.trim`; OFF everywhere: Jack 11/19 "Leave it off" after a 4x cut lost ~10x the share on FX): a judged market over its target comes down to the smallest `TRIM_GRID` (0.05) multiple that keeps `TRIM_TO` (1.25) x the aim and `TRIM_MIN_SHARE` (2.25%), the whole share loss counted (`share_at`); a cut of 1 - `TRIM_TRIGGER` (20%)+, on two reads at the same multiple (`trim_candidates` in the state file), at most to `TRIM_MAX_CUT` (a quarter) a step, never under `TRIM_MIN_MULT` (0.2); `pace` rows carry `x_trim`. Files: `run-logs/pvd-scan/size_mult_NAME.json` (the multiples `resize --apply` writes; the tune's `--size-mult-file`), `resize_state_NAME.json` (the last read, for the two-read rule), `trim_NAME.json` (each trimmed market's earlier multiple, given back at a relight, off the list, or with the trim off). TIER REACH (5e2f061, the registry's `floor.tier_to` with `--tier-file` in the tune; `TIER_REACH_MAX` 25): `plan_resize` gives `reach` {t: why} to a judged market at the cap short of the aim or under $1 at the cap (instead of `off_next`), and `reach_restore` {t: 1.0} to an off market with `pre_off` whose side (`costs`) sits past the last tier within the reach; never twice (`reach_granted` = the file's markets); `apply_reach` writes `tier_NAME.json` (`{"to", "markets": {t: {at, why, sides, stop, period_start[, restored]}}}`), drops a record at a relight / off the list, and all of them when the reach is off |
| `pvd_scan.py` | Scorer and lister. Any-wave mode `--series --no-vote-check --new-since --stop-cap --report`. Env `PVD_SCAN_LIVE/DEAD/DECIDED_SIZE`, `_BAR`, `_BUDGET` (every wave's live scan adds), `_OWN_BUDGET` + `_OWN_SERIES` (the wave's own budget against its own listings, 2026-11-14), `_CHEAP_TIERS`, `_TIER_SKIP`, `_PRICE_MIN/_MAX`, `_CUSHION`, `_FOLLOW_BOOK`, `_SIM_HOURS`, and the SIZE-UP TO THE BAR (4c69bb8, 11/18): `_MAX_MULT` (the wave's `scan_max_mult`), `_MULT_BASE` (its `--bank-max` x `--size-mult`), `_MULT_FILE` (its `--size-mult-file`) -- an under-bar market judged at the bank max then quarter steps up to the cap, listed with the multiple that clears (written to the file; never lowers, never lifts a 0); its log line ends "N only at the bank max B x up to M (x1 n, x1.25 n, ...)". Tests: `test_pvd_scan_tiers.py`, `test_pvd_scan_budget.py` |
| `pvd_wave_tune.py` | Idempotent tuner: every lever in SKILL.md section 10, `--size-mult-file`, `--tier-file` (`{"to": C, "markets": {...}}`: a listed market in it rests the last `--cheap-tiers` multiple on a side past every tier up to C c; the cheap note says "(x5, tier to 25c)"; missing = none; 5e2f061), `--fair-file` (the file's `floor` replaces `--fair-floor`, 0 = nothing rests without a fair; `margin_extra` per ticker), `--halts-on`, `--wave-added`. Tests: `test_pvd_wave_tune_mult.py` |
| `wave_fair.py OUT [--live]` | The FX + metals fair (w1113): lognormal on Friday's close moved by 24/7 venues. The weekend rolls by itself (11/14): `fx_shut_window` (DST-aware), closes from Kalshi's weekly settlements / Yahoo kept in `run-logs/pvd-scan/wave_fair_closes.json` (with each venue's own close), each within 0.5% of its 24/7 source or refused. Weekday mode while FX trades (11/14, Jack: "can quote anytime ... but need a fair value"): priced from now, `margin_extra` (staleness), nothing within 12 h of a measurement, `floor` 0, valid 20 min. Tests: `test_wave_fair.py` |
| `crypto_event_fair.py` | The bot's crypto *EVENT fair thread (fleet GBM, Kraken spot, every 15 s); status `run-logs/incentive-mm/crypto_event_fair.json`; stop file `crypto_event_stop.json` (by hand; covers strikes the wave never listed) |
| `fx_event_fair.py` | The bot's FX + metals *EVENT fair thread (70d05b7 + d736cc4, 11/14): wave_fair.py's model on the kept close (`run-logs/pvd-scan/wave_fair_closes.json`) and Hyperliquid / Kraken / Bitso mids every 15 s; status `run-logs/incentive-mm/fx_event_fair.json` (each market's `p`, `margin`, `bid_max`, `ask_min`; `model.edge_c`); dry read `python fx_event_fair.py`. EDGE (13ffefe, 11/19): margin = `EDGE_C` cents (`IMM_FX_EVENT_EDGE_C`, default 0: bid <= floor(fair), ask >= ceil(fair)); `=off` = the 11/14 margin. Its limits replace the list's fair-file limits every cycle (`pvd_limits`), so the tune's `--fair-file` margin doesn't bind an FX strike while the read is fresh. Tests: `test_fx_event_fair.py`, `test_incentive_mm.TestFxEventFair`, `TestFarFromFair1118` |
| `nfl_prop_fair.py`, `league_news.py`, `nfl_snipe_bot.py` | Sports (type E): the NFL player-prop fair (`python nfl_prop_fair.py --event E`; the bot's thread, status `run-logs/incentive-mm/nfl_prop_fair.json`), the NBA injury-report reader, and the NFL prop taker (OFF since 11/07). Families, knobs, logs: `sports.md` section 8 |
| `pvd_marginal.py` | What-if: marginal $/day per extra $1k by wave x band (read-only, ~12 min) |
| `imm_pvd_watch.py` | Wave dashboard. Env `PVD_WATCH_GO_LIVE / _DIR / _SERIES / _EXCLUDE / _PACE_BAR / _PACE_SPARE_H / _LATEST_TARGET` (1.20), `PVD_WATCH_HOST / _TOKEN`. `/data` adds `latest` (the tiles over the program periods running now, `periods` = [[start, markets]]; 4d59e4a, every running period since d6b19ea) and `sides` (the One side or both table, 06421a0); a market's share is the 15-min mean of the bot's cycle rows (5265a34; the tail read is 30 MB). Trading P&L (d6b19ea): `settle_cents` (a decided market realized at what it paid), `mark_fn` (the last trade; none -> `imm.halt_mark_cents` on the touch); rows carry `settle`. Restart to load a change: stop the process on its port, then `pvd_waves.py dash NAME`, or let pass 0 restart it within 15 min (its `history.json` persists; a fixed page such as :8774 / :8783 comes back only through pass 0). `imm_crypto_event_watch.py` wraps it for the crypto strikes (:8783) |
| `pvd_watch_keepalive.ps1` | Pass 0: restarts down or hung dashboards: every enabled registry wave, plus the fixed :8774 (11/04 books + other listings) and :8783 (crypto page). :8773 / :8771 retired 11/14 |
| `relight_watch.py` | What the relight tasks listed and the live program count (exit 3 = something); `new_waves` names a wave the check added; each pass's `bar` and size-up count (`sized`, `bank_max`, `sized_cap`) from the scan's line (beb9966) |
| `wave_alert.py` | Every wave / relight / pick-off / dark / bait / upcoming alert in one layout (`--send`); pick-off lines carry the fill time; a NEW shape held for Jack texts (wave and upcoming), an upcoming relight emails (texts on a preflight FAIL). Tests: `test_wave_alert.py`, `test_wave_alert_upcoming.py` |
| `bait_watch.py` | Pass 10: the bot's bait defenses on wave markets. Tests: `test_bait_watch.py` |
| `imm_heightened_report.py` | Scores heightened bait mode (dry) from its narrowed go-live: `--since 2026-11-14`, from a worktree |
| `sms_alert.py` / `send_alert_email.py` | Text Jack (T-Mobile gateway, rate-capped; `--long` <= 900 chars) / email |
| `wave_pocket.py` | Wave Pocket builder (skill `wave-pocket`): "Today #N" tabs, trims older tabs to the 256 KB cap |
| `run_pvd_scan_waves.cmd` / task **KL pvd-scan waves** | The relight task (passes 0, 6-11; 1-5 retired) |
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

| Knob | Live (11/19) |
|---|---|
| `IMM_JOIN_WAIT`, `_SUBPENNY`, `_REST_MEMORY_H` | on, on, 24 |
| `IMM_PVD_OFF_USUAL`, `_C`, `_HOURS`, `_COLD` | on, 3, 6, base (`IMM_CRYPTO_WAVE_COLD_SIZE` 400 for crypto strikes) |
| `IMM_FAR_FROM_FAIR_C` | 10 (8281473 built it at 20; 10 since 32bd17c, Jack 11/18: "Yes, 10c"). A wave side this far on the safe side of its live FX / crypto event fair skips the join wait and the off-usual cut; 20 = the old threshold, 0 = off |
| `IMM_FX_EVENT_EDGE_C` (read by `fx_event_fair.py`) | 0 (13ffefe: bid <= floor(fair), ask >= ceil(fair); `off` = the 11/14 2c + vol-error + requote margin) |
| `IMM_PVD_CLOSE_CLAMP`, `_C` | on, 5 (breakeven or better also rests) |
| `IMM_UNWIND_WAVES_HELD`, `IMM_UNWIND_WAVES_EXEMPT_NEW` | on, on |
| `IMM_UNWIND_WAVES_AT_COST` | 0 (85a5d8e: the outside price, at most 5c past usual or at cost where further, never leading; 1 = the old breakeven floor) |
| `IMM_RESTART_KEEP_WAVE_UNWIND` | 1 (95eef3b: held wave closing orders ride restarts) |
| `IMM_RESTART_HANDOFF_MAX_AGE_SECS` | 1200 (38f07e9; was 300) |
| `IMM_LONE_REF_PVD` / `IMM_LONE_REF_GUARD` | on (wave list) / dry (rest of the bot) |
| `IMM_TWIN_CHECK`, `_TOL_C` | on, 2 (6fbad53) |
| `IMM_SWEEP_GROUP`, `_MIN_EVENTS`, `_MIN_CT`, `_HOLD_MIN` | on, 2, 200, 20 (76d9201, 25cbd54) |
| `IMM_WAVE_GROUPS` | on (2063782: a wave series with no risk group is its own group for the group breaker) |
| `IMM_CAND_BREAKER`, `IMM_PULL_BATCH`, `IMM_CYCLE_CANCEL_BATCH` | on, 20, 1 (736cc94: the candidate breaker; breaker pulls and the cycle's cancels 20 a batch) |
| `IMM_FAIR_HOLD_PULL` | on (2063782: a held or stale coin / FX series, every order pulled at once) |
| `IMM_EARN_ONLY` | on (1fd880b: no rung rests that the walk would not pay, but the unwind) |
| `IMM_SWEEP_BREAKER`, `IMM_SWEEP_HOLD_SECS` | on, 600 |
| `IMM_TAPE_TRIGGER`, `IMM_TAPE_WAVE_MIN_CT`, `IMM_WAVE_BREAKER` | ab 100%, 200, on |
| `IMM_HEIGHTENED` | dry (b41c0d4) |
| `IMM_MARK_EXTERNAL`, `IMM_PVD_FULL_EARN`, `IMM_PVD_ZERO_OFF` | on, on, 1 |
| `IMM_CRYPTO_EVENT_FAIR`, `_LEAD`, `_TOL_CENTS`, `_SPOT_BAND`, `_JUMP_PCT`, `_JUMP_HOLD_SECS` | on, on (listed strikes take no lead), 2, 0.005, 0.01, 300 (fa0c63c; was 600) |
| `IMM_FX_EVENT_FAIR`, `_TTL_SECS`, `_REFRESH_SECS`, `_SERIES`, `IMM_FX_EVENT_CLOSES_FILE` | on, 180, 15, the nine FX + metals *EVENT series, `run-logs/pvd-scan/wave_fair_closes.json` (70d05b7 + d736cc4) |
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
7. **The bot restarts itself only when `incentive_mm.py` changes** (`code_change_exit_due`: >= 60 s after, at ET minute >= 50 or <= 5, or any time with no hourly-temp market selected). A change to a helper module alone (`fx_event_fair.py`, `crypto_event_fair.py`) waits for some other restart: touch `incentive_mm.py` in the same commit (ff05c8b did) or restart on purpose.
8. **The sync is `git merge --ff-only`:** a commit touching a file modified in KL's working copy refuses the WHOLE deploy (11/14: `wave_fair.py`; the uncommitted wave tools are the same trap until committed). After `schtasks /run`, check KL's `git rev-parse HEAD`: on 11/15 the task's fetch took ~4 min; `sync_kl_main.ps1` run directly does the same thing.
9. **Fetch before you push:** another session may push to main meanwhile (11/15: 1fd880b landed between the worktree and the push). Rebase, re-run the suite, then push. Tests: `python -m unittest` (no pytest here).
10. The wave tools run live from KL with no restart (each pass starts a new process). Test them in a scratch copy or worktree, check that KL's copy hasn't changed since you read it (other sessions edit them), then copy. Run the wave test modules one at a time: `test_pvd_waves` / `test_pvd_wave_pace` stub `incentive_mm` / `kalshi_reads` in sys.modules, which breaks tests that import the real ones after them.

## Gotchas

- Public list reads 429. Use signed reads for lists; public only for single markets and books.
- The scan's projection is optimistic: 11/04 ~2x, 11/06 ~2.7x; the first-hour rate of every 11/11-11/14 wave fell 30-80% within a day.
- The scanner rebuilds a relit entry from scratch: keep settings in the registry, which every relight inherits.
- Settings a pass reads mid-run: since 2026-11-14 the pass lock serializes setarg with a running pass; values that must never revert (a fair margin) go in the file the tune reads at run time.
- Bash heredocs halve backslashes: write patch scripts with the Write tool. Windows Python can't open a Git Bash `/c/...` path: pass `C:\...` paths to `python -c`.
- Dashboard rate vs listed size: after a size change the rate reflects the old size for ~25 min; its per-market share is the 15-min mean of the bot's rows since 5265a34 (one cycle's before: 40 of 112 "under $1" on 11/18 were dips). `pace` for sizing.
- **The pass order is scan -> fair -> resize -> tune:** a tier or size change set before a pass lands at that pass's tune, AFTER its floor check judged the old size (11/18: 20 strikes off at 21:01Z just before the 10c tier applied). Lift such offs after the pass (SKILL.md section 11).
- **Kalshi shows a YES ask as a NO bid at 100 minus its price.** Jack 11/19 on USD/JPY 11/25 T16150: "why is this unwinding at 74c". That was our 26c YES ask closing 825 YES bought at 17c. Read our orders from the bot's side (`own_ask_top` in the cycle log) before answering.
- **An unwind never sells under the live fair.** On a wave with a live FX / crypto event fair (0c edge), the unwind-only switch's closing order stops at ceil(fair) for a long (floor(fair) for a short), even after its join wait clears for the outside price. T16150 sat at 26c against a 2x12 book.
- **On wall books, share moves faster than our size** (the 11/19 FX trim): cutting a side to a quarter took its share from 7.7% to 0.7%, not the ~2% the "others standing still" model gives. Model a size-down from a measured response, never from the size-up model, and cut in measured steps.
- **Saving `incentive_mm.py` restarts the live bot** at its next safe moment (preflight import, order handoff): make sure the module imports BEFORE the last save (a test module that imports it, e.g. `test_pvd_scan_tiers`). And the KL sync (:15 / :45 local) reverts uncommitted files: a reverted `incentive_mm.py` restarts the bot on the OLD code. Commit bot edits before the next :15 / :45.
- **The bot plans a quote from the book it reads:** after a wall steps up, the join wait keeps the old price for 100-270 s, and earn-only then pulls a side with 1,000+ contracts in front. A one-sided count right after a step-up is mostly that loop; read it again a few cycles later, or use the far-from-fair exemption's coverage.
- **A ladder strike's other makers peg to the best price in DOLLARS** (11/17 deep dive: 244,897 @ 4.9c = $12k): stepping inside them draws the walls after us within the hour (11/18: four step-ups in four hours). A share bought by price is rented, not owned.
- Cycle-log rows land in batches and lag a cycle: a just-turned-off market can still show resting in the latest row. The bot log's order lines are faster.
- Sub-penny books: the guard judges `price_exact`; whole-cent limits only on unwinds.
- After 00:00Z, `orders_ / fills_ / cycle_log_ / selection_events_` files roll to the new UTC date; the day log keeps its start-date name.
- Two sessions editing the list within minutes collide: take `scan.lock`, re-read, write atomically.
- National / cross-region markets move with every region.
- **Listed is not selected:** the list gets a market past the allowlist; the bot's screens still apply. Count selected vs listed (`selection_events_<date>.jsonl`, or `whydark`).
- **Live-feed series have no fair in the bot** (SCAN_EXCLUDE_PREFIXES: FX, indexes, metals, crypto prices) except the *EVENT ladders: crypto (`crypto_event_fair`) and the nine FX + metals series (`fx_event_fair`, since 70d05b7). Anything else on a live feed: quote it only with a fair.
- **The cycle log's `quoted` column reads 0 on every row:** count resting with `own_bid_ct + own_ask_ct`.
- **Jump-hold time:** count it from the bot's `crypto-event fair: ... JUMP HOLD` lines (printed on every change of the hold set and at each start); stand-aside -> resume pairs overstate it (some strikes never log a resume).
- **A wave's stop cap ends its relights:** pass 7 lists nothing past it, and until 11/14 the detector still called its series LISTED, so a later batch went unseen. The after-economic-events waves have no cap since 11/14; `set NAME stop_cap off` clears one (`window fx-shut` would hold a wave to the FX close).
- **The resting-order read must stay far above the resting cap** (40 x 200 = 8,000 under a 12,000 cap doubled 3,462 sides on 11/14).
- **Placement cap vs renewals:** every order renews every ~25 min, so ~9,000 resting orders need ~1,000 placements per ~3.6-min cycle just to stand; a big listing waits behind them (dark sides go first).
- **The collateral budget charges selected markets, not list sizes:** turning list markets off frees nothing while they stay selected.
- **The pick-off watch's 6 h lookback** alerts a fill the first time it crosses the bar, possibly hours after the fill; three such fills in a series pin the whole series at base.
- **Wave Pocket's page holds 256 KB:** its builder trims the older tabs first; the :8774 "Since 11/06" tab overlaps the newer registry waves (:8773 / :8771 retired 11/14).
