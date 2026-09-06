# Kalshi Incentive-Rewards Market Maker — Handoff

*Written 2026-07-10. Self-contained context for a new session. Owner: Jack (jackdu224@gmail.com).*
*STATUS: built, tested, dry-run verified against the live API — **NOT turned on** (no scheduled task registered, never run with --live).*

## What this is

**One bot, one process** (`incentive_mm.py`) that discovers every active **liquidity
incentive program** on Kalshi (`GET /trade-api/v2/incentive_programs`, ~3,900 markets,
~$388k of open pools as of 2026-07-10), ranks the markets by reward-$/day, and rests
conservative two-sided post-only ladders on the top ~30 within a **$1,000 collateral
budget**. Goal: incentive rewards + round-trip spread capture with ~zero directional
intent. Sister design to `crypto_touch_mm.py` (same order plumbing, ledger, fail-safe,
alerting patterns) — but **no pricing model**: quotes anchor to the *external* best
bid/ask (join, never lead), so the bot is a pure liquidity provider, not a view-taker.

## How the rewards actually work (CFTC filing, Aug 2025 program)

- `period_reward` is in **centi-cents** (1,000,000 = $100). Programs are per-market,
  mostly $25–$200 over ~2–7 day periods; `status=active` filter works on the endpoint.
- Once per second (random moment), Kalshi snapshots the book. Per side (YES bids and
  NO bids — a YES ask *is* a NO bid), levels are walked best-first until cumulative
  size ≥ **target size** (usually 1000). **If a side's total depth < target, NOBODY
  earns on that side that snapshot.**
- Each qualifying order scores `DiscountFactor^(ticks behind best) × size`
  (factor **0.5**: at-best = 1.0×, 1c behind = 0.5×, 2c = 0.25×). Reward = your share
  of everyone's summed scores × pool (min payout $1/period).
- Optimal conservative shape: **small lot AT the best, larger lots 1–2c behind** —
  the ladder Jack asked for, and it doubles as the anti-pickoff posture.
- Eligibility: normal members only (no MM-agreement firms). Kalshi may claw back
  abusive participation. Volume-incentive programs (taker-side) are ignored — none
  active today, and this bot is post-only maker.

## Files

| File | Role |
|---|---|
| `incentive_mm.py` | The bot (v1.0). One file: discovery, selection, quoting, risk, alerts, status. |
| `test_incentive_mm.py` | 110 unit tests incl. fake-exchange cycle tests (`python -m unittest test_incentive_mm`). Tests sandbox all file I/O away from the live status dir. |
| `run_incentive_mm.ps1` | Launcher (restart-forever, logs to `run-logs\incentive-mm\`). **No task registered yet.** |
| `rain_fair.py` | NWS hourly-PoP fair values for the 20 KXRAIN daily stations; a daemon thread in the bot refreshes `rain_fair_values.json` every 30 min (also runnable standalone). |
| `run-logs\incentive-mm\` | Logs, `status_incentive_mm.json` heartbeat, `imm_state.json` (persisted known-tickers), `HALT` kill file. |

Shared: `KalshiClientsBaseV2ApiKey_FIXED.py` (same client as every repo bot),
PEM at `C:\Users\jackd\Downloads\Lisa_Kalshi.txt`, alert creds in HKCU env.

## v1.1 (2026-07-11): MENTION + CRYPTO universe, per-contract-minute objective

User decision: restrict to the two lowest-adverse-selection families with defined
information windows. Changes on top of v1.0:

- **Exact-series allowlist** (`IMM_ALLOWLIST_ONLY=1`): series ending `MENTION` +
  named crypto structural series (`IMM_ALLOW_SERIES`, default includes
  KXCHINAUNBANBTC, KXBTCMAXY/MINY, KXETHMINY/MAXY, KXCRYPTORETURNY, KXBTCVSGOLD,
  KXINXVSBTC, KXBTC50VS100, KXCRYPTOSTRUCTURE...). Exact matching — substring
  matching once caught KXHEGSETHOUT via "ETH". Blocklist still wins: programs now
  exist on crypto-fleet events (KXXRPMAXMON, KXDOGEMINMON) — the fleet's own resting
  ladders already earn those; this bot must never quote the same books.
- **Real event-start cutoffs** (`EventStartResolver`): MLB mention games via
  statsapi.mlb.com, World Cup via ESPN's public scoreboard (ticker teams+date →
  kickoff), fixed broadcast hours for schedule-less series
  (`IMM_SERIES_START_ET`, default LOVEISL 21:00 / BIGBROTHER 20:00 / FIGHT 17:00 ET),
  cutoff = start − 30 min (`IMM_EVENT_START_BUFFER_MIN`). Fallback stays midnight-ET.
  **Mention markets with NO derivable window are excluded entirely**
  (`no_event_window`) — e.g. KXWCMENTION-MENWORLDCUP, whose broadcasts already run
  daily. Same-day games are quotable until kickoff−30min (game-day morning is the
  richest safe rent window; programs run ~1–2 days including game day).
- **Objective = incentive per contract-minute quoted**: selection fetches every
  candidate's live book (~180 books/10min) and ranks by estimated $/day *per resting
  contract* (share estimator with our ladder overlaid), with a ~$0.75/day min-payout
  floor and 1.15× stickiness for incumbents. A $25 pool with an empty near-touch now
  outranks a $145 pool where farmers stack the walk. Fresh listings (<24h) are exempt
  from the volume screen (mention markets list the day before with zero volume).
  Digest/status report contract-minutes and cents per 1k contract-minutes.

Live snapshot 2026-07-11: 29 markets selected (~$974 collateral), est **$147/day**
share at rest — today's WC games until kickoff−30m, tomorrow's MLB until game time,
tonight's fights until 4:30 ET, slow crypto structurals. Estimate remains an upper
bound pending the paid-period micro-probe (strategy doc §6).

## v1.2 (2026-07-11): the bot yields to the human

The user trades some mention markets manually on this same account. v1.2 makes the
bot get out of his way, everywhere:

- **Fills are matched by ORDER OWNERSHIP**, not ticker: every imm- order id is
  persisted (`our_order_ids`, 7-day retention) and only fills of those orders enter
  the P&L tracker / loss halt. Manual and fleet fills are invisible to it. The fill
  cursor still advances on all account fills so the scan window stays bounded.
- **The bot's own book** (`pnl.pos`, persisted as `own_pos`) is tracked separately
  from account positions. Fill-burst breaker runs on own-book deltas; inventory
  reserve and orphan-restore use the own book.
- **Manual standoff**: on any candidate/managed market, if |account position − own
  book| ≥ 5 contracts (`IMM_MANUAL_STANDOFF`) or a non-imm resting order exists
  there (live), the bot cancels its quotes, deselects the market, and won't
  reselect until the manual activity is gone (auto-released when the divergence
  clears). Digest alert `manual_standoff`; current list in status JSON.
  Verified live 2026-07-11: 29 candidates skipped `manual` on the user's real
  mention positions.
- The ±500 event cap and ±100 market cap track the BOT'S exposure: event netting is
  computed from the bot's own book, so the user's manual positions (on quoted or
  sibling markets of an event) never consume the bot's capacity (user decision
  2026-07-11).
## v1.2 fix (2026-07-12): don't wipe the user's manual orders

Symptom: during the first probe, some of the user's manual limit orders were wiped.
Root cause: the bot's orders are post-only (always resting MAKERS), so the only
self-cross is the USER aggressing into a bot quote. The client default STP
`taker_at_cross` only cancels OUR order when OUR order is the taker — which
post-only orders never are — so it gave the user's crossing order no protection
(self-match / cancel). Two-layer fix:

1. **STP = `maker`** for imm orders (`IMM_STP_TYPE`, set per-order; the shared
   client default and the crypto fleet are untouched). On any self-cross the
   BOT's resting order is the one cancelled, so the user's incoming manual order
   survives. (Per Kalshi: `maker` = "your resting maker order is cancelled if a
   taker side of yours crosses it".)
2. **Event-level standoff** (`IMM_EVENT_STANDOFF=1`): a manual footprint —
   position ≥5 vs the bot's own book, or ANY non-imm resting order — on ANY market
   of an event makes the bot avoid EVERY market of that event, not just that
   strike. The user trades whole games/episodes, so this removes the collision
   surface at its source. Verified live 2026-07-12: MILPIT (his MLB positions)
   and MENWORLDCUP (his big manual book) fully excluded; `manual` skips 32→54.

Residual: a ~1-cycle (90s) race if the user opens a brand-new order on an event the
bot is already quoting — but STP=`maker` protects his order even in that window,
and the next cycle yields the whole event. The bulletproof elimination is a
**separate Kalshi subaccount** for the bot (no self-trade between subaccounts at
all) — recommended if manual + bot activity stays heavy on the same series; needs
confirming reward eligibility is per-subaccount first.

## Per-series overrides (v1.2, 2026-07-12) — Love Island

`SERIES_OVERRIDES` lets a series depart from the global spec. Currently
**KXLOVEISLMENTION** (user decision — high incentive/minute, one-day pools):

- **Ladder 5/5/5** (`IMM_LOVEISL_LEVELS`, flat 5 at 0/1/2 ticks = 15/side) instead
  of the global 1/2/4 probe ladder.
- **Max net 50/market** (`IMM_LOVEISL_MAX_POSITION`) — tighter than the global 100.
- **quote_all**: EVERY market of the event is force-selected, exempt from the yield
  ranking, MAX_MARKETS, the collateral budget, and the payout-floor/zero-yield
  filters (still subject to safety screens: one-sided, wide, cutoff, breakers, and
  the foreign-order yield). Live 2026-07-12: 17 markets, ~$247 collateral.
- **Hard expiry 9:00pm ET** (`hard_expiry_et=(21,0)`) + **start_buffer_min=0** —
  the cutoff is exactly the 9pm episode start with NO pre-broadcast buffer (user
  2026-07-12: "quote until 9p not 8:30"). Both the hard-expiry floor and the
  resolver path (fixed 9pm start − 0 buffer) yield 9:00pm ET; `place_order` caps
  each order's exchange-side expiration at it, so nothing rests past 9pm.
  (Other mention series keep the global 30-min `EVENT_START_BUFFER_MIN`.)
- **Depth padding** (`pad_to_target`): a reward side pays no one unless its total
  resting depth reaches the target size (usually 1000). When a side the bot is
  quoting falls short, it adds throwaway contracts at the **1c mark** (bid) /
  **99c mark** (ask = NO bid at 1c), rounded up to the nearest 100
  (`IMM_PAD_ROUND`), to reach target — so the near-touch ladder qualifies. The
  pad earns ~0 itself (weight 0.5^~47 ≈ 0), costs ~1c collateral + ~1c max loss
  per contract, is exempt from the ladder side/level caps, and is netted out of
  the depth calc so it doesn't churn against itself. Only pads a side it already
  has near-touch quotes on (join-don't-lead preserved). `IMM_PAD_TO_TARGET=1`
  enables it for all series; `IMM_PAD_MAX` caps per-side pad (default 5000).
  Fill note: at 1c/99c the pad almost never fills — near-touch fills first on any
  move and trips the fill-burst breaker (cancel + stand down) long before price
  reaches the pad. A pad fill would count toward P&L/breakers normally.
- **Live-event depth gate (2026-08-31, Jack)**: `KXTRUMPMENTION*` and
  `KXMAMDANIMENTION*` (`IMM_EVENT_DEPTH_SERIES`, prefix match) never pad, and any
  in-band market of theirs with < 1000 external contracts (`IMM_EVENT_DEPTH_MIN`,
  book minus our own orders) on either side — or that loses a touch mid-band —
  stands down its WHOLE event: these events' start times aren't reliably known,
  and a thin book is the tell that the event has gone live (adverse selection).
  A strike that JUMPS from in-band to out-of-band by `IMM_EVENT_DEPTH_JUMP`
  (8c)+ in one cycle (49c→99c: settled in practice) confirms the event live and
  kills it **permanently** — no resume, no re-selection ("settled strike SHOULD
  hold an event down forever"). Thin-only halts resume only once EVERY managed
  market reads healthy in-band at target for 15 min
  (`IMM_EVENT_DEPTH_RESUME_SECS`); an out-of-band pin or one-sided book blocks
  resume for as long as it sits there. Halts, live-confirms, and the gated
  series' mid history all persist across restarts. **Fill tripwire
  (2026-09-01 postmortem)**: an own-book move of `IMM_EVENT_FILL_HALT` (15)+
  contracts in one cycle on a gated market stands the whole event down; a
  second burst (`IMM_EVENT_FILL_STRIKES`) confirms it live permanently —
  active regardless of `IMM_BREAKERS`, because on MAMDANI-shaped books
  (~99% of side depth is 1c/99c junk that never flees a live event) the
  depth check is structurally blind and fills are the only unmaskable
  live signal.

Reconciliation with the yield-to-human rule: for quote_all series the bot ignores
the user's POSITIONS (he wants full coverage) and its caps/skew track the bot's OWN
book, but STILL yields any single market where the user has a live resting ORDER
(direct collision) — matching his stated model ("yield while I have live orders,
resume when they're gone"). STP=maker protects the race. Non-quote_all series keep
the full event-level position standoff.

NOTE: quote_all bypasses the probe's $200 budget, so the live footprint during the
Love Island window is ~$247 collateral + inventory reserve (~$320 total at rest),
not $200. Deliberate.

## Market selection (every 10 min)

1. Pull all `active` liquidity programs; aggregate per market → $/day; drop paid-out,
   not-yet-started, blocklisted series (**crypto fleet's KX*MAXMON/MINMON + KXHIGH***
   — never trade against our own bots), and markets whose ticker-embedded event date
   has arrived (cheap pre-filter).
2. Bulk `get_markets` the top ~135 by $/day → hard screens: active, >1h to close,
   two-sided book, spread ≤ 25c, mid in 5–95c, lifetime volume ≥ 25, target size known,
   not benched/breakered, event-start cutoff not imminent.
3. Select best-$/day-first until **MAX_MARKETS (35)** or the **$1,000 budget**
   (full-ladder collateral per market + 50¢/contract reserve for *this bot's* open
   inventory) is exhausted.

## Quoting (every 90s)

- Per side per market: **5 @ external best (join, never improve, never alone),
  10 @ 1c behind, 2c gaps** → `IMM_LEVELS=0:5,1:10,2:20`. Post-only, GTC,
  **TTL 600s / refresh 420s** (same anti-churn math as the crypto fleet).
- Exact-price diffing: a resting order is kept only at exactly the desired price/size
  (reward credit halves per tick, and a stale at-best order whose anchor faded would
  *lead* the book).
- **Caps** (all enforced, crypto-bot style): ≤ level size per price level; ≤35/side
  resting per market; net **±100/market** (position + full ladder, user spec);
  net **±500/event** (user spec, budget split across the event's markets,
  best-paying first); ≤450 resting orders account-wide for this bot; ≤120
  placements/cycle (deferred, not dropped).

## Guards / when it stands down

| Guard | Trigger | Action |
|---|---|---|
| **Event-start cutoff** | ticker date (e.g. `-26JUL11ARGSUI`) → **00:00 ET day-of**; or `occurrence_datetime` when it's ≥60min before expiration | reduce-only in the last 60 min, cancel + abandon at cutoff, **and every order's exchange-side expiration is capped at the cutoff** — nothing can fill past event start even if the process dies. Mention/broadcast markets are therefore pre-event only (user decision 2026-07-10). |
| Mid-move breaker | external mid moved ≥15c between cycles | cancel market, 30 min cooldown |
| One-sided breaker | a previously two-sided book lost a side (everyone pulled quotes = news) | cancel market, 30 min cooldown |
| **Fill-burst breaker** | our position moved ≥15 contracts in one cycle | cancel market both sides, 60 min cooldown, **urgent email** (insider sweep signature) |
| Inventory skew | \|pos\| ≥30 → halve accumulating side; ≥60 → pull it | passive unwind via the other side |
| Reduce-only tail | market deselected but \|pos\| ≥5 | keep quoting *only* the reducing side, ≤\|pos\| |
| Crossed/locked or >25c external book | — | cancel market this cycle |
| Zero-reward bench | est. reward share 0 for 30 cycles (book below target size) | bench 4h |
| **Daily loss halt** | realized P&L **today** ≤ −$50 (this bot's fills only; baseline rolls at the 6 AM ET summary, so banked profit can't mask a bad day and yesterday's breach can't re-halt today) | cancel everything, idle until next ET day, urgent email |
| `HALT` file | `run-logs\incentive-mm\HALT` exists | cancel everything, idle until removed |
| **Rain-fair gate** (2026-07-28) | KXRAIN daily whose touch fights the NWS fair: bid touch > fair+10c or ask touch < fair−10c | cancel market both sides, sticky-selected, auto-resumes when book and forecast re-agree; quotes are never re-priced (at-touch or nothing) |
| Fail-safe | 4 consecutive cycle errors | cancel all resting, exponential backoff; wake-grace 120s after suspend/resume |
| Shutdown | SIGINT/SIGTERM/SIGBREAK/atexit/finally | cancel all imm- orders; startup sweeps orphans by prefix |

Dry-run is the default; `--live` is explicit. `--cancel-all` always operates on the
real book. `--status` prints the live selection table without trading (verified
2026-07-10: 29 markets, ~$975 ladder collateral, sensible universe of political/
entertainment/long-dated markets, all pre-event).

## Rain daily fair-value gate (2026-07-28, "strategy 5")

KXRAIN dailies resolve on something PUBLICLY FORECAST at the exact settlement
station (NWS hourly PoP ≈ P(measurable rain on the local calendar day) — the
contract definition), so joining a touch that fights the forecast is
voluntarily adverse. `rain_fair.py` computes per-station day probabilities
(exponent-haircut complement product, `RAIN_FAIR_HOURLY_EXP=0.5`).

Jack's constraint (same day, replacing the first-cut ±8c quote clamp):
**rewards need the top of book** — credit halves per tick behind the touch,
so fair must never re-price a quote. The fair is therefore a **gate**, not
an anchor:

- When quoting, quotes join the touch UNCHANGED (full reward credit).
- **Stand aside entirely** when the touch fights fair on the adverse side:
  bid touch > fair + 10c (paying over fair) or ask touch < fair − 10c
  (selling under fair). `IMM_RAIN_FAIR_TOL_CENTS`, strict inequality.
  One-side breach parks both sides; logged once per transition
  (`rain-fair stand-aside` / `rain-fair resume`); sticky selection keeps the
  market so quoting auto-resumes when book and forecast re-agree.
- missing/stale fair (per-entry TTL `IMM_RAIN_FAIR_TTL_MIN=240`) → gate open
  → **plain band behavior**; every failure mode degrades to the pre-feature bot.
- TOMORROW-ONLY (Jack 2026-07-28): dailies quote only the day BEFORE the
  measurement day — already enforced by the midnight-ET ticker-date rule
  (verified live: all resting rain orders on the next-day event; PT cities
  stop at 9pm local). The fair used is therefore the full-day probability;
  the today/remaining-hours entries in the JSON are monitoring-only.
- Kill switch: `IMM_RAIN_FAIR_ENABLE=0` (launcher env) restores 7/26 behavior.
- Data path: daemon thread `rain-fair` (started in `run()`, never on the
  trading thread) → `run-logs\incentive-mm\rain_fair_values.json` →
  mtime hot-reload at universe refresh, like every other override file.
- Behavior at ship time (JUL29 books vs fair): quotes at touch on
  ATL/AUS/DC/NYC/SEA/PHX/... (book within tol of fair); stands aside on
  BOS(85 vs 98)/DEN(71 vs 93)/MIA(20 vs 33)/MIN(29 vs 67)/PHIL(51 vs 92) —
  exactly the books where the forecast says the ask is donating YES.

## P&L & attribution

- Fills are filtered to markets **this bot has ever quoted** (persisted in
  `imm_state.json` across restarts) — the crypto fleet / weather bot fills on the
  same account must not pollute the loss halt or the budget reserve. Fill reads
  are **deduped by `fill_id`** (the min_ts cursor is inclusive; without dedupe,
  boundary fills would re-book into P&L every cycle — review-confirmed bug, fixed).
- After a restart, positions on markets no longer selected are **restored as
  reduce-only** (metas rebuilt from a market read), so no inventory is ever
  orphaned or invisible to the event cap.
- Realized P&L = avg-cost round trips (PnlTracker). Estimated reward accrual =
  live implementation of the snapshot-scoring formula against the fetched book
  (`estimate_reward_share`), integrated over time — an *estimate*; actual payouts
  land as Kalshi account credits (not modeled, check the app).
  **2026-08-01: estimator rewritten to the AMENDED program rules (effective
  7/30, CFTC filing 7/15):** reference price = level where cumulative depth
  reaches target/5 (full weight for everything at/above it, pro-rata by size
  — tiny at-touch lots lost their multiplier edge); snapshots excluded unless
  BOTH sides reach target; est $/day scaled by a per-market counted-snapshot
  EMA (`IMM_COVERAGE_EMA_ALPHA`, proxy for the filing's non-excluded ratio).
  Old-math estimates from 7/30-8/01 were systematically inflated — that was
  the rain "underearning" mystery. Reconcile credits vs estimator over the
  next paid period; ladder SHAPE re-derivation lives in imm_shape_sim_v2.py.
  **Kalshi support confirmed (via Jack, 8/1): the engine runs the amended
  REFERENCE scoring; the site's per-order efficiency tooltip is WRONG (still
  touch-based) — ignore it for at-ref orders.** Same evening: deep-reference
  size multiplier (`ref_depth_mult`, IMM_REF_DEPTH_SLOPE=0.1/tick capped at
  IMM_REF_DEPTH_MAX_MULT=2.0) scales at-ref rungs and their side_max room —
  deeper reference = safer rung = more size (30 -> up to 60/side).
  **Same day: ladder switched to IMM_LADDER_MODE=atref at IMM_LEVELS=0:30**
  (Jack sign-off): each side rests as ONE rung at the book's reference level
  (deepest full-weight price; falls back to touch when the book is too thin
  for a reference). Sim v2 on 90 live books: same est reward as all-at-touch
  within ~1%, ~half the fill exposure, ~2/3 the collateral. 30/side (was 10)
  rebuilds per-market share against band dilution (~size/200 within the
  qualifying band). Mention x1.5 and quiet-hours x2 multiply the 30. Rain
  keeps 3/side via IMM_RAIN_LEVELS, also at-ref. Collateral estimator still
  prices rungs at the anchor (conservative: at-ref rests deeper = cheaper).
  Applied via full task restart 2026-08-01 (the $ProbeEnv gotcha — a
  bot-process restart alone would keep the stale env; one orphaned python
  from Stop-ScheduledTask was killed by hand, check for doubles after any
  task-level restart).
- Daily summary email 7:00 AM ET-ish (counters roll 6 AM ET): markets quoted,
  est. reward/day captured, realized P&L, fills, top inventory, alert counts.

## Restarting the live bot

- **Use `restart_imm.ps1`** (Jack 2026-08-24: "always restart bot in the
  :45 - 1:05 timeframe, if there is an hourly temp market. since hourly temp
  isnt quoted"; window shifted to **:50-:05** later the same day). Hourly
  temp (KXTEMP*H) quotes ~hh:11 (program activation) to ~hh:50 (close-10
  cutoff) — the richest pools in the feed live mid-hour, and :50-:05 IS the
  dead zone where a restart forfeits nothing (resting orders die server-side
  at TTL/cutoff regardless; a python kill does not cancel them). The script
  reads `selected_tickers` from `imm_state.json`: hourly temp in play →
  waits for the window; temp dark or no bot process running → restarts
  immediately.
- Default mode kills the `incentive_mm.py` python; the launcher relaunches
  it ~30s later with freshly imported code — the right tool after a
  sync-kl-main code pull. `-Task` does the full scheduled-task bounce (stop
  task, sweep orphaned pythons — the 2026-08-01 Stop-ScheduledTask double —
  start task): **required after a `$ProbeEnv`/launcher change**, which a
  python kill would keep stale. `-Now` skips the window wait (emergencies).
- **Remote restart from a Claude session**: bump `$RestartRequest` in
  `sync_kl_main.ps1` and push to main — the sync task dispatches
  `restart_imm.ps1` (windowed, detached) exactly once per request id on the
  next sync run after the pull, ~30-60 min post-push. Done-stamps live in
  `run-logs\incentive-mm\restart_request_<id>.done` (local, untracked).
- **The bot also restarts itself on code changes** (2026-08-24, after the
  KXTRUEV enrollment sat inert on a running process): `incentive_mm`
  watches its own source mtime and cleanly exits at the next safe moment —
  the :50-:05 window when hourly temp is selected, immediately otherwise,
  never on an mtime younger than 60s — and the launcher relaunches it on
  the new code (`code_change_exit_due`; `IMM_EXIT_ON_CODE_CHANGE=0`
  disables). Code deploys therefore self-apply once this version is
  running; the dispatch/ps1 remain for launcher-env changes and manual
  bounces.

## Go-live checklist (when Jack says go)

1. `python -m unittest test_incentive_mm` → all green.
2. `python incentive_mm.py --status` → eyeball the selection (no junk, no own-bot series).
3. `python incentive_mm.py --once` (dry) → check ladders look sane in the log.
4. **Single-order smoke test**: place one real 1-lot via a tiny script or briefly run
   `--live` with `IMM_MAX_MARKETS=1 IMM_LEVELS=0:1` — verifies Kalshi accepts the
   `imm-<run>-<hex>` client_order_id format (the orphan sweep keys on it), then
   `--cancel-all`.
5. Register the task (at-logon, non-elevated, same pattern as the crypto fleet):
   ```powershell
   $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\Users\jackd\Documents\KL\run_incentive_mm.ps1"
   $trg = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
   Register-ScheduledTask -TaskName "KL incentive_mm" -Action $act -Trigger $trg
   Start-ScheduledTask -TaskName "KL incentive_mm"
   ```
6. Watch `run-logs\incentive-mm\incentive-mm-*.log` for 2–3 cycles; confirm the 7 AM
   digest next morning; check reward credits in the Kalshi app after the first period.

Scale-up levers (env vars, set in launcher or HKCU): `IMM_COLLATERAL_BUDGET`,
`IMM_MAX_MARKETS`, `IMM_LEVELS`, `IMM_POLL_SECS`. Emergency: create the `HALT` file,
or `python incentive_mm.py --cancel-all`.

## Daily email digest (2026-07-13)

`send_imm_digest.py` — one HTML morning email, structured like the crypto fleet's
`send_daily_digest.py`: headline **estimated reward** (contract-minutes + c/1k-
contract-min efficiency), P&L breakdown, per-EVENT table sorted best→worst
(P&L$/REAL$/UNREAL$/NET/EXPO$/Q-markets-quoted), TOTAL row, balance, capital-at-work,
one-line health check. **All figures are the bot's OWN book** (own_pos/own_avg from
`imm_state.json` + realized replayed from fills matched by order id) — the user's
manual trades and the other cloud bots are excluded. Reward figures parsed from the
bot's last stored daily summary (`status_incentive_mm.json` `summary_body`).

- Task **`KL incentive_mm DIGEST`**, daily **7:10 AM ET** (staggered after the crypto
  DIGEST's 7:00), cmd.exe wrapper → `run-logs\incentive-mm\digest-task.log`. Idempotent
  marker (`imm_digest_sent_<date>.marker`), Modern-Standby retries (8×5min), registry
  cred fallback. `python send_imm_digest.py --test` sends immediately.
- The bot's own one-liner email is suppressed (`IMM_SUMMARY_EMAIL=0` in the launcher);
  it still STORES the daily summary in status for the digest to read.
- Verified sending unattended under Task Scheduler 2026-07-13.

## Daily quote-gaps email (2026-08-02)

`imm_quote_gaps.py` — second morning email (Jack 2026-08-02: "every AM send
email on which markets should be quoted that aren't, based on expected
earnings per minute"): every live-incentive market the bot is NOT quoting,
rolled up by event, ranked by est $/day (+ c/min), each with Kalshi's event
title as a plain-English "what is this" snippet and a WHY-NOT-QUOTED reason
(blocklisted/frozen, not in allowlist, no-new gate, yielded to manual,
screened:<reason>, under payout floor (global $1; TEMP $0.70 since
2026-08-02), zero yield, capacity, candidate cap). Second table: deliberately-off blocklist/freeze families with their
pools, so a deliberate block that starts leaving real money shows up.

- Numbers come from the bot's OWN machinery imported from `incentive_mm.py`
  (`fetch_programs`/`_allowed`/`_screen`/`_estimate_candidate_yield` — the
  amended-rules estimator with the standard at-ref ladder overlaid on each
  live book). **Config parity**: the launcher's `$ProbeEnv` block is parsed
  from `run_incentive_mm.ps1` and applied BEFORE import (logs "mirrored N
  launcher env vars" — if that line says 0, the report ran on defaults and
  is wrong). "Currently quoted" = `selected_tickers` from `imm_state.json`.
  Strictly read-only: never cycles, never orders, never `_save_persist`.
- Estimation is bounded: `IMM_GAPS_MAX_BOOKS` (250) book reads, biggest
  pools first, `IMM_GAPS_MAX_PER_EV` (12) per event (partial events shown
  as ">="), ~40 reads reserved for the deliberately-off table; unreached
  events are listed with pool only — never silently dropped. Headline is a
  sum of independent per-market estimates, not a feasible portfolio.
- Task **`KL imm quote-gaps`**, daily **7:20 AM ET** (after the 6:45
  overrides/auto-enroll + 7:10 digest), cmd.exe wrapper →
  `run-logs\incentive-mm\quote-gaps-task.log`. Idempotent marker
  (`imm_quote_gaps_sent_<date>.marker`), Modern-Standby retries (8×5min),
  registry cred fallback. `--test` sends now (no marker); `--dry` prints
  only. Mirrored blocks in `incentive_mm.py` (`ticker_cutoff_passed`, meta
  construction) carry keep-in-sync comments.
- First real send + unattended TS launch verified 2026-08-02. Same-day
  finding: KXDDR5* programs GONE from `/incentive_programs` (0 markets,
  $0 pool) — the DDR5 open item resolved itself.

## Strategy layer

`INCENTIVE_MM_STRATEGY.md` (v1.1, red-teamed 2026-07-11) is the quant strategy on top
of this chassis: two-gate placement rule, regime classes, WCB/CVaR risk budgets, and a
measurement-first go-live sequence (dry-run sensor → $200 micro-probe → scale).

**P0 items are BUILT (v1.2, 2026-07-11):**
- **Loss halt sees total P&L**: positions are marked to external mid every cycle
  (books for managed markets, bulk reads for the rest); settlements are detected
  when an own-book position vanishes from the unsettled read and booked through the
  P&L tracker at 0/100 (a manual offset of our lot is dropped without P&L instead);
  the −$50 halt runs on realized + unrealized vs a daily baseline; digest shows
  unrealized MTM + carried-contract count. Entry costs (`own_avg`) persist across
  restarts so restored inventory marks correctly.
- **Cycle logger** (`IMM_CYCLE_LOG=1`, `run-logs\incentive-mm\cycle_log_YYYY-MM-DD.csv`):
  per cycle per managed market — book best/depths, target, est share, qualifying
  sides, account vs own position, pool rate, quoted size. This is the η/jump panel
  and qualification-flap sensor the strategy's calibration reads.
- **Micro-probe profile**: `run_incentive_mm.ps1 -Probe` → 1/5-size ladders
  (`0:1,1:2,2:4`), 10 markets, $200 budget.

## Known gaps / deliberate choices

1. **Reward estimate ≠ payout truth** — verify against actual Kalshi credits after the
   first paid period and recalibrate expectations.
2. Realized P&L / breakers / benches reset on restart (known-tickers, fill-cursor
   and seen-fill-ids persist; the loss-halt window restarts with the process).
   Accepted for v1.
3. Kickoff *times* aren't in the API — date-only cutoffs stop at ET midnight day-of
   (forfeits same-day-listed daily markets entirely; deliberate, user choice).
   Deadline-style tickers (date = expiry, not event) also stop a day early — safe
   direction, some reward forfeited.
4. Orders are placed one-by-one (~2/s); first live cycle takes a few minutes to build
   ~350 orders. `batch_create_orders` exists in the client if this ever matters.
5. Maker fees are not modeled (post-only flow is free on today's incentive series —
   all sampled series are `fee_type: quadratic`, taker-charged). If Kalshi ever runs
   programs on maker-fee series (S&P/Nasdaq ranges), add them to `IMM_BLOCKLIST`.
6. Volume-incentive programs ignored (taker flow; none active anyway).
7. Kalshi eligibility fine print: liquidity rewards require normal member status —
   already true for this account. Kalshi can revoke "abusive" participation; this bot
   provides genuine two-sided liquidity, which is the program's stated purpose.
8. Same Modern-Standby caveat as the crypto fleet: on battery the laptop freezes and
   quotes TTL-expire (safe); a VPS remains the fix for true 24/7.

## Quick commands

```powershell
# preview what it would trade right now (read-only)
python incentive_mm.py --status
# one dry cycle / continuous dry run
python incentive_mm.py --once
python incentive_mm.py
# emergency: flatten all quotes (real, works without --live)
python incentive_mm.py --cancel-all
# instant stand-down while live
New-Item C:\Users\jackd\Documents\KL\run-logs\incentive-mm\HALT -ItemType File
# tests
python -m unittest test_incentive_mm
```

## 2026-08-31 — KXAAAGASW paused (Jack: "pause KXAAAGASW")

(Restored 9/1 — the original section was written by the pause session but
sat uncommitted and was lost when main synced over the working tree.)
AAA gas WEEKLY added to the launcher IMM_BLOCKLIST (run_incentive_mm.ps1)
and the bot bounced via `restart_imm.ps1 -Task` at 19:0x ET. Evidence from
the 8/31 rewards report: lifetime net −$298 (cred $181 / P&L −$479),
post-8/12 −$180, last week −$115 — negative in every window. KXAAAGASD
(daily, net +$77 post-8/12) and KXDIESELW/D stay live. Open weekly
positions ride to the 9/7 settlement. Blocklist is PREFIX-matched —
KXAAAGASW collides with nothing (checked) and catches state weeklies too.

## 2026-08-31 — APP + foot-traffic + state-gas families allowlisted (Jack)

Jack: "allowlist the app markets e.g. KXCLAUDEAPP... foottraffic markets
e.g. KXBKFT... state gas markets e.g. KXAAAGASDIL" — the "e.g." was swept
to the full families live in the programs feed that evening:
**APP x10** (KXCARTAPP KXCLAUDEAPP KXDASHAPP KXDISNEYAPP KXDKNGAPP
KXESPNAPP KXFACEBOOKAPP KXFANDUELAPP KXGEMINIAPP KXGPTAPP) and **FT x8
new** (KXBROSFT KXCAVAFT KXCMGFT KXCOSTFT KXMCDFT KXSGFT KXSHAKFT KXTGTFT;
BKFT/YUMTBFT already in since 8/3) into `_DEFAULT_COMPANY_SERIES`; **state
gas dailies** (six states that day). All day-dated tickers listed weeks
ahead (no KXTRUEV listing trap — checked); APP/FT are dated observations
with no release moment, so they joined the overrides script's
consumer-observation disclosure-sweep exclusion. First live cycle after
enrollment: state gas + CMG/SG selected and quoting; est share $171 ->
$209/day.

## 2026-09-01 — family growth coverage (Jack: "fix this going forward")

Kalshi expanded the families overnight and the 8/31 exact lists missed
every new member for a day: five NEW state gas dailies (GA/NC/OH/PA/WA)
and 12 NEW `*APP` series (GROK/GRUBHUB/HULU/INSTAGRAM/LYFT/MAX/NFLX/
PARAMOUNT/PEACOCK/TWITTER/UBER/UBERE). Fix, superseding the 8/31 exact
lists:

- **State gas = prefix family**: `KXAAAGASD` in `ALLOW_SERIES_PREFIXES`
  (per-state exact entries retired). `KXAAAGASW*` weeklies untouched.
- **Family guard inheritance**: `FAMILY_OVERRIDE_PARENTS` +
  `ensure_family_override()` — a prefix/suffix-admitted series clones its
  archetype's `SERIES_OVERRIDES` entry (safe-join, rate-floor setting, AAA
  blackout) at first sight in the candidates loop, logged "family override
  inherited". Parents: KXAAAGASD / KXBKFT (`*FT`) / KXCLAUDEAPP (`*APP`).
  Suffix rules also require exact/extra-allow membership (KXNFLDRAFT can
  never clone).
- **New FT/APP auto-enroll**: `classify_series` enrolls the
  dated-observation shape (FT/APP suffix + day-dated event + T-strike) —
  the no-new company rule is earnings-release companies, not these.
  Live-feed sweep: exactly the 12 new APPs, zero false positives.
- **Rate bar OFF for FT/APP** (Jack same evening: "dont hold off. start
  quoting things as if normal"): `IMM_CONSUMER_OBS_MIN_RATE=0` loop, the
  KXDIESELW shape — safe-join + $1 payout floor + caps + midnight cutoff
  stay. Reason: the share-based $2/day bar excluded exactly the LIQUID
  books (KXHULUAPP/KXINSTAGRAMAPP: real ladders, $21.30/day pools — richer
  than quoted CMGFT's $18.76 — yet est pennies/day against 1-2k-deep
  1c-wide touches), while the first-wave APPs had passed only by enrolling
  when books were thin. The 12 new APPs were also hand-added to
  extra_allow_series.json (~20:33 ET, hot-reloaded; the merge-writing
  6:45am task keeps them).

DEPLOYMENT NOTE (the 9/1 wipe): all of the above was first applied locally
uncommitted, and at 20:41 ET a main fast-forward (PRs #14/#15, after a
stuck MERGE_HEAD cleared) replaced the working tree — the bot self-
restarted onto code with none of it, evicting the enrolled families
(52 -> 39 events), and the uncommitted KXAAAGASW launcher pause vanished
with it. Everything was reapplied on branch `claude/imm-family-allowlist`
and landed via PR. Standing lesson: THIS REPO'S WORKING TREE IS DISPOSABLE
— main syncs every 30 min and other sessions land PRs concurrently, so any
change that must survive goes through a branch + PR, same day.

## 2026-09-02 — gas events capped to top-3 by ROI (Jack)

Jack: "for GAS markets, quote only the 3 highest ROI markets in each
event. because they are all correlated so i dont want to quote them all."
`IMM_EVENT_TOP_N` (default `KXAAAGAS:3`, prefix:N, longest wins) caps each
gas event to its N highest-ROI markets — ROI = est $/day per $ at risk
(the quote-gaps metric: fill-weighted exposure, else collateral), with the
yield rank's 1.15x incumbent factor against churn. Applied to `ranked`
before sticky seeding (skip bucket `event_top_n`); overrides the 7/13
"no per-event market cap" rule for these prefixes only.

Deploy verification (8:41pm ET restart): first universe cut 115 gas
markets (`event_top_n: 115`); kept-3 are adjacent near-money strikes
(e.g. KXAAAGASD 4.1400/4.1450/4.1500). Settled server-side state: NO gas
event carries more than 3 two-sided ladders (tonight actually zero —
evening books polarize and the per-side band rule one-sides the kept
markets too). The 4-7 one-sided markets per event beyond the kept-3 are
NOT fresh quoting: two uncapped days left own-book inventory on 132 gas
strikes, and those ride as reduce-only orphan-managed exits OUTSIDE
`ranked` — the cut deliberately never touches them (evicting an orphan
strands inventory unmanaged). They drain at settlement; fresh events
start clean at <=3. NOTE the selected_tickers count in imm_state.json
conflates laddering members with reduce-only orphan management — judge
the cap by two-sided ladders per event, not by selected count.

Diesel joined the cap the same evening (Jack "do the same with diesel"):
default now `KXAAAGAS:3,KXDIESEL:3` — KXDIESELD/KXDIESELW have the same
one-print-per-event correlation; identical semantics (reduce-only
inventory rides outside the cap).

## 2026-09-02 — Finance/Economics quiet-print sweep, group top-10 (Jack)

Jack: "in Finance/Economics sections that are unquoted in normal IMM bot,
quote the top 10 markets based on ROI (with no more than 3 on a single
event). dont include any events with major adverse selection risk, or have
major realtime data risk. bias towards quieter markets."

Every live-program market in Kalshi category Economics/Financials was
scanned (1,687 paying, 453 already quoted) and each unquoted family
risk-reviewed for (a) settlement on a continuously-observable live feed
and (b) scheduled releases the bot would quote THROUGH (release timing
verified against the bot's actual cutoff per event). Survivors —
11 series, all near-zero 24h volume, enrolled in `_DEFAULT_FINECON_SERIES`
(env `IMM_ALLOW_FINECON_SERIES`) with safe-join + no rate bar (the
KXDIESELW/KXTRUEV pattern; $1 payout floor still gates):
KXSPRLVL (weekly EIA SPR), KXCBDECISIONNZ (RBNZ, Oct decision verified
Oct 28 2pm NZT vs bot exit Oct 27 00:00 ET), KXCBDISRAEL, KXVENEZCRUDE
(OPEC MOMR), KXAAAGASMINM/MAXM (AAA touch-extreme monthlies — join the
03:05-04:00 ET AAA blackout and the KXAAAGAS:3 event cap), KXBRAZILGDP,
KXJOLTSOPEN, KXDATACENTCON, KXWENBACONATOR/KXTBCRUNCHWRAP (Spice
fast-food monthlies). The rejected list and reasons are inline above
`_DEFAULT_FINECON_SERIES` — notable traps found: KXUE-RUS26SEP and
KXISMPMI have NO day in the ticker so the midnight-ET rule never fires
and the bot would quote through Rosstat/into ISM morning;
KXSNOWCRABCATCH's TAC announcement (~Oct 6) and KXSOCKEYERUN's ADF&G
forecast (~Nov 13, verified 2025 precedent) both land BEFORE their
cutoffs — single-report pickoff traps wearing quiet books.

Mechanism: `finecon_group_cut` — greedy walk of the group by the shared
`_market_roi` (the event_top_n metric, factored out), keeping the best
`IMM_FINECON_TOP_N` (10) with at most `IMM_FINECON_EVENT_TOP_N` (3) per
event, everything else cut before sticky seeding (skip bucket
`finecon_top_n`, same deselect path as `event_top_n`). Dry-run against
the scan snapshot kept exactly: NZ HOLD/H25, SPR T286/T284/T281,
VENEZ 1.2M/1.3M, MINM 3.70/3.75/3.95 — ~$8.3/day est. Point-in-time est
at enrollment, not a promise; the walk re-ranks every refresh.

## 2026-09-03 — finecon members quote to completion (Jack)

Jack: "once start quoting, should quote to completion. dont unquote them."
The day-1 group walk re-ranked members every refresh and could evict one
when a sibling's ROI rose (it did, within hours: morning books reshuffled
the kept set vs the enrollment snapshot). Now `finecon_group_cut` is
ADMISSION-ONLY: members (group markets in the previous selection) are
never cut, consume their global-10 and per-event-3 slots, and newcomers
compete only for the remainder; if members ever exceed a lowered cap they
all stay and nothing new enters until attrition frees slots. The same
immunity is threaded into `event_top_n_cut` (new `immune` arg — the
KXAAAGAS:3 prefix catches KXAAAGASMINM/MAXM; gas/diesel proper keep their
evictable semantics) and into the hopeless exit (finecon members exempt —
cents-a-day rates on quiet long windows are where the absolute-$1
projection is noisiest). Members leave ONLY by natural completion
(cutoff/close/program end) or a safety screen stand-down — screens,
bands, blackouts and budget were deliberately NOT loosened.

## 2026-09-04 — finecon widened: KPI set + state stats, top-15, digest tracker (Jack)

Jack, correcting the day-1 risk frame: what matters is whether the
settling release lands INSIDE a paying program window — "as long as its
not live during the incentive period duration it should be safe" — not
the market's close date. Under that test the company-KPI set is clean
(weekly periods end months before the Q3 reports) and is ENROLLED into
the finecon group: KXDKS, KXZM, KXURBN, KXLOW, KXDG, KXAFRM, KXBBY,
KXWSM, KXOKTA (`_FINECON_KPI_SERIES`), plus KXTXOIL + KXVAPORTTEU (lagged
state statistics he asked after by name; day-1 exclusion was ROI-only).
Report-week periods get the release-time guard the other company series
already have: imm_earnings_overrides.py now includes the KPI set in
COMPANY_DISCLOSURE_SERIES + COMPANY_TICKERS (their tickers carry no day,
so the midnight-ET rule can never protect them — the override IS the
guard). Group cap 10 -> 15 (IMM_FINECON_TOP_N; "increase from 10 to 15
markets"), per-event 3 unchanged.

Payout-floor basis: confirmed ALREADY per Jack's spec — `_quotable_days`
projects est over the REMAINING program window (to program end, capped
by cutoff/close), so verdicts renew at each period rollover; no change.

Tracking ("make sure im able to track performance of these"): the daily
digest (send_imm_digest.py) grew a FINECON SWEEP section — members with
period-to-date accrual est + net inventory, group past-day/week trading
P&L from the same per-event windows as the events table, and
Kalshi-CREDITED rewards on group events from the recon ledger (the only
actual-money number; credits land 1-2d after period end).

Also in this commit: TestLiveEventDepthGate repaired — 37f16a7 (the
date-arm) landed with all 11 gate tests red because the class's 99DEC31
fixtures are exactly the known-future dates the arm suppresses. setUp now
arms unconditionally (inf pre-arm) so gate logic stays tested, and the
arm rule itself got the test it shipped without.

## 2026-09-05 — Carbon Arc family joins finecon (Jack)

Jack asked after KXFOOTWEARADS-26OCT06, KXELECTRONICSADS-26OCT06,
KXDRPEPPERPOS-26OCT03, KXAMZNCC-26OCT07. The ADS pair was in the Sep-2
scan and rejected on ROI alone ("never contends") — under the 15-slot
walk that judgment belongs to the walk, so the WHOLE 11-series ad-spend
family is enrolled (state-gas lesson: no half-covered families).
KXDRPEPPERPOS + KXAMZNCC are post-scan Carbon Arc listings, same
dated-observation shape (monthly index print, ticker date = print day,
midnight-ET exit; subscriber panel-drip residual = the accepted FT/APP
one). AMZNCC's Oct strikes est 5.9-11.1%/day at enrollment — immediate
slot contenders. Standard finecon guards (safe-join, no rate bar, $1
floor, group walk 15/3).

Known gap, deliberate: new Carbon Arc series keep appearing (2 in 3
days) and do NOT auto-enroll — the daily classifier files unknown shapes
as review. New siblings need a hand add to _DEFAULT_FINECON_SERIES (or a
future classifier rule if Jack wants the family to self-extend).

## 2026-09-05 pm — Carbon Arc self-extension + daily openings (Jack)

Jack: "yes self-extend carbon arc" + "add 5 openings each day to the 15
quoted. they dont all need to be used, but its so that new events have a
chance to be quoted if high ROI even if the main 15 slots are full."

SELF-EXTENSION: the daily overrides task now source-checks every REVIEW
series (one /series read per novel series; steady state zero) and files
Carbon Arc-sourced ones into `finecon_extra_series.json` instead of the
review email. The bot hot-reloads that file each refresh
(load_finecon_extra_series, mtime-gated): merges into FINECON_SERIES
(now a mutable set over the code-owned _FINECON_BASE), applies the
standard finecon guard on first sight, and _allowed() checks
FINECON_SERIES live so a task-appended series is quotable the same
refresh. Blocklist wins as everywhere; removing a line from the file
drops the extra member through the normal deselect path.

DAILY OPENINGS: IMM_FINECON_DAILY_OPENINGS (5) over-cap admissions per
ET day. In-cap slot fills are free; only admissions THROUGH a full cap
burn one (finecon_openings_used: kept beyond max(members, cap)).
Members admitted via openings are ordinary sticky members, so
membership can sit above 15 and drains only by completion — while
above, even settlement-freed capacity re-fills via openings only. The
burn counter persists (finecon_admit_day/finecon_admits_today in
imm_state.json — ~20 restarts/day must not refill the day) and resets
at ET midnight. Digest shows "+used/5 daily openings" in the FINECON
SWEEP header.

## 2026-09-05 — "Opportunistic IMM" daily email (Jack)

Jack: "add daily email called 'opportunistic IMM' showing a table of the
events quoted, earning est, P&L, and net." The opportunistic book = the
finecon sweep (incentive_mm.FINECON_SERIES). New standalone
`send_opportunistic_imm.py`: one row per currently-quoted finecon EVENT
with EARN EST$ (bot accrued-reward estimate, period-to-date), P&L$
(trading: open-book MTM on held inventory + past-day realized/settlement),
NET$ (P&L + EARN EST), plus MKTS and a plain-English label; TOTAL row;
footer = actual Kalshi-credited on opportunistic events to date (recon
ledger). Numbers reuse send_imm_digest's validated helpers
(pnl_windows/own_book/current_mids/credit ledger) — imported, not
reimplemented, so a row can't disagree with the digest. Picks up Carbon
Arc self-extensions via imm.load_finecon_extra_series(). Flags:
--test (send now, no marker) / --dry / --print (build + print only);
daily sent-marker idempotency; 8x retry loop; Alerter tag IMM-OPP.

First dry run (2026-09-05): 9 events / 20 markets (openings expanded past
15), est $32.36/period, trading -$2.10, net +$30.26.

Scheduled "KL imm opportunistic" DAILY 7:25 AM ET (after 7:10 digest,
7:20 quote-gaps), same principal/settings as quote-gaps (Interactive/
Limited/jackd, PT2H limit, battery-allowed, StartWhenAvailable). Recreate:
```powershell
$arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" "C:\Users\jackd\Documents\KL\send_opportunistic_imm.py" >> "C:\Users\jackd\Documents\KL\run-logs\incentive-mm\opportunistic-task.log" 2>&1"'
Register-ScheduledTask -TaskName 'KL imm opportunistic' -Force `
  -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At '7:25AM') `
  -Principal (New-ScheduledTaskPrincipal -UserId 'jackd' -LogonType Interactive -RunLevel Limited) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2))
```

## 2026-09-05 — OPEN SCAN: second opportunistic tier, +15 slots / +5 openings over ALL markets (Jack)

Jack: "extend the opportunistic IMM with 15 slots and 5 to scan all markets.
be very careful for adverse selection."

READING OF THE ASK (state it, because the numbers coincide with finecon's):
the finecon sweep already runs 15 slots + 5 daily openings over a HAND-
CURATED universe. "Extend ... with 15 slots and 5 to scan all markets" is
read as a SECOND tier — its own 15 slots, 3/event, 5 daily openings — whose
candidate universe is every live-program market the bot does not otherwise
quote. Finecon is untouched (15/3/5, curated list, same walk); the normal
book is untouched (`_allowed` unchanged). If Jack meant "widen finecon's
universe to everything", the change is one env var: set
`IMM_SCAN_TOP_N=0` and enroll series into finecon instead — but that would
drop every screen below, which is the opposite of "very careful".

### What the tier is

`incentive_mm.py`, the `SCAN_*` block (right after the finecon block).
Universe (`scan_universe_reason`): NOT blocked/frozen, NOT `_allowed`
(finecon, suffix/prefix families and extra-allow included), NOT on
`SCAN_EXCLUDE_PREFIXES`. The curator is replaced by machine screens, every
one of which FAILS CLOSED (missing data = not admitted), applied cheapest
first (`_scan_admission`):

| Screen | Rule (default) | Why |
|---|---|---|
| STRUCTURE | day-dated event ticker — **or, since 2026-09-06, a month-named one on a Fiscal.ai-settled series** (`KXCCL-26SEPALBD`; see the dated note below: the first of that month becomes the cutoff); numeric-threshold strike (`T286`, `B90`, `4.1400`, or Kalshi `strike_type` greater/less/between) | the midnight-ET rule is the only release guard an unknown series has (KXUE/KXISMPMI had no day and would quote THROUGH their prints); for the KPI class the report MONTH is that guard; a "will X happen" binary's one jump IS the resolution (strategy §1) |
| FAMILY | **no category ban since 2026-09-06** (the first cut banned `Sports, Crypto, Elections, Politics, Climate and Weather, Culture, Entertainment` wholesale — see the dated note below; `IMM_SCAN_EXCLUDE_CATEGORIES` is empty by default and only a deliberate re-ban names a category); no LIVE settlement source (GET /series, cached 7d: pyth/coinbase/**cfbenchmarks**/espn/nba.com/ercot/weather.gov/**weather.com**/... keywords — a live price index, a live scoreboard, a live weather feed); prefix exclusions are OWNERSHIP and feeds, not categories: other repo bots (KXLOWT, KXRAIN, KXHIGH, KXTEMP, KXAVGT, KXAQI), the crypto fleets' families by asset (KX<ASSET>D / MAX-MINW / MAX-MINMON / MAX-MINY / Y), FX/index/commodity/grid feeds, the 9/2 scan's rejects (KXUE, KXISMPMI, KXSNOWCRABCATCH, KXSOCKEYERUN, KXTECHLAYOFF, ...) | realtime risk is a property of the settlement SOURCE, not the category: a market everyone else can price off a live feed is one we are always last to reprice; two of our bots must never anchor to each other |
| ACTIVITY | market `volume_24h` <= 60, EVENT `volume_24h` <= 250 (summed over every bulk-read sibling, pinned strikes included), listed >= 24h | finecon members read ~0 volume at enrollment; informed flow on one strike shows up on its siblings; the history read needs data |
| HISTORY | 72h of hourly candlesticks: >= 12 two-sided bars, mid range <= 10c, no bar-to-bar move >= 6c, traded volume <= 250 (cached 6h) | a book that moved is not a quiet print, whatever its family says |

Read budgets per refresh (the universe is thousands of markets): bulk
market reads capped at 600 tickers (pool-ranked, members always in), 30
`/series` reads, 40 candle reads, 120 estimator book reads for non-
members. Verdicts are PERSISTED (`scan_series_meta`, `scan_history_cache`
in imm_state.json), so after the first hour the steady state is a handful
of reads per refresh. A candidate whose reads didn't fit the budget is
"pending" — not admitted, retried next refresh.

Admitted markets are ordinary sticky members (quote-to-completion, immune
to the hopeless exit and the event top-N, like finecon) SIZED LIKE THE
NORMAL BOOK — Jack, same evening: "it can have the same contracts/max net
position/deep reference/overnight size as the normal book" — so the global
ladder, the global net cap, the full deep-reference multiplier and the
quiet-hours window all apply unchanged. The per-series guard set, applied
on first sight (`ensure_scan_override`) and RE-APPLIED AT LOAD from the
persisted member list, is safe-join placement + no rate bar. The first cut
shipped half-size guards (0:10 / cap 50 / ref 1.5x / no overnight x2); the
knobs survive for a later tightening: `IMM_SCAN_LEVELS`,
`IMM_SCAN_MAX_POSITION`, `IMM_SCAN_REF_MULT_CAP` (0 = uncapped;
`capped_ref_mult` grew a `series` arg for it — all three sizing sites pass
it), `IMM_SCAN_HOUR_MULT=0`.

The backstop that is ON: the **tier loss budget** — the tier's own
realized + MTM today over every market it ever admitted (`scan_book`;
flat, departed markets leave it only at the daily roll, so a settlement
loss booked mid-day stays in that day's figure) <= -$75 -> every scan
member deselected, tier closed until the next ET day (`scan_halt_day`),
urgent alert `scan_halt`. Carried across restarts like pnl_today
(`scan_pnl_carry`, same 5am-CT roll). The whole-book $1,200 halt is
untouched — this bounds the blast radius of an unreviewed universe on its
own. Inventory skew and the per-market/event caps apply as everywhere.

Two per-event eviction tripwires exist in the quote loop but are OFF by
default — the first cut shipped them (fill >= 8 in one cycle; mid jump
>= 8c or drift >= 15c from the admission mid -> whole event evicted
PERMANENTLY, series struck, 2 strikes/7d bar the family) and Jack removed
them the same evening: "dont need these". They arm through
`IMM_SCAN_FILL_HALT` (15 = most of a 20-lot rung is the sane bar on the
normal ladder), `IMM_SCAN_MID_JUMP`, `IMM_SCAN_DRIFT` (>0 = on) and then
run regardless of `IMM_BREAKERS`; `scan_evicted_events` /
`scan_series_strikes` / the `scan_evict` alert only ever populate when
armed.

### 2026-09-06 — the category ban is gone (Jack)

Jack, on the first morning's admissions (20 members, every one a state-
level Economics print: employment, home prices, corn, milk, taconite —
"why is the open scan placing quotes that seem like finance/econ?"), then:
"dont systematically drop Sports, Crypto, Elections, Politics, Climate
and Weather, Culture, Entertainment. they should be scanned, but of
course watch out for adverse selection and realtime risk."

What changed (`incentive_mm.py`, `SCAN_*` block; `imm_quote_gaps.py`):

- `SCAN_EXCLUDE_CATEGORIES` defaults to EMPTY. The knob stays for a
  deliberate re-ban; a category named there rejects the whole family
  again. Cached verdicts follow the knob BOTH ways without waiting out the
  7-day TTL (`scan_cached_verdict`): a persisted `category:<c>` reject
  whose category is no longer on the knob is STALE and triggers a fresh
  `/series` read (the live-source screen needs the settlement sources,
  which are not cached; no budget = `series_meta_pending`, never admitted
  on the strength of a lifted ban alone), and a persisted ok on a newly
  banned category rejects in place with no read. The quote-gaps label
  applies the same rule (a stale ban reads "screens pending").
- The sports-family prefix backup (`KXNFL, KXNBA, ...`) and the generic
  `KXCRYPTO` prefix are retired from `SCAN_EXCLUDE_PREFIXES`. What stays
  is ownership and feeds, not categories: the other repo bots' weather
  families, the crypto fleets' families BY ASSET (`KX<ASSET>D`,
  `MAX/MINW`, `MAX/MINMON`, `MAX/MINY`, `Y` — two of our bots must never
  anchor to each other; the same prefixes cover the remaining live-index
  price structures), the FX/index/commodity/grid feeds, the 9/2 pickoff
  traps. Crypto series that are not price structures (hard forks, ETF
  flows, reserve bills, the KXBITCOIN25 class) carry other prefixes and
  are scanned like anything else.
- `SCAN_LIVE_SOURCE_KEYWORDS` += `cfbenchmarks` (CF Benchmarks — the
  live probe showed every `KX<ASSET>` hourly/daily/monthly price structure
  settles on it and none names Pyth) and `weather.com` (The Weather
  Company feed behind the daily high/low/avg temperature families).

What "watch out for adverse selection and realtime risk" means here — the
screens that judge a series or a market on its own, all unchanged:

| Risk | Screen |
|---|---|
| realtime (a live feed everyone else prices off) | the live-source keyword screen on the SERIES' settlement sources (ESPN/nba.com/nfl.com/pgatour/atptour/fifa/uefa/... for scoreboards, cfbenchmarks/pyth/coinbase/coingecko for price indices, weather.gov/weather.com/wunderground for observations); `SCAN_REQUIRE_DATED` + `trade_cutoff_utc`: a day-dated ticker cuts quoting off at ET midnight BEFORE event day, so a game-day market is never quoted on game day (the live probe: Kalshi's `occurrence_datetime` on game markets equals the expected expiration, i.e. game END — it is no help, the ticker date is the guard); the 24h age screen keeps same-day listings (hourly/daily price structures) out |
| adverse selection (news gaps, progressively-known data) | numeric-threshold strikes only (`REQUIRE_NUMERIC`: a "will X happen" binary's one jump IS the resolution — this alone still rejects most sports winner / election winner / award markets as `shape`); the 72h quiet-history screen (range <= 10c, jump < 6c, volume <= 250); the 24h activity screens (60/250); the tier's $75/day loss budget; the 2-strikes/7d series bar; the optional fill / mid-jump / drift tripwires (`IMM_SCAN_FILL_HALT` / `MID_JUMP` / `DRIFT`, still off — Jack 9/5 "dont need these") |

Expected effect: most Sports series still reject, per series, as
`live_source` (nearly every one cites ESPN or the league site); crypto
price structures reject on `cfbenchmarks`/prefix; what opens up is the
numeric, dated, record- or report-settled tail of those categories
(season stat thresholds settled on a governing body's record, vote-share
and approval thresholds, snowfall/hurricane counts off non-live records,
box-office thresholds) — each still needing 24h of age, a quiet 72h and
a sub-60-contract day. The refresh log's `rejects {...}` shows the new
mix (`live_source` up, `category:*` gone after the caches re-read).

### 2026-09-06 — Fiscal.ai KPI markets join the scan (Jack)

Jack: "include markets settled by Fiscal.ai as part of the opportunistic
scan." Fiscal.ai is the company-KPI aggregator Kalshi settles ~277 series
on (quarterly + annual KPIs: Carnival ALBD, Chewy active customers, Ford
US sales, Kroger identical sales, Taco Bell same-store sales, Boeing
deliveries, ...). Nine carried live programs on 9/6 — 80 markets, ~$1,270/
day of pool, i.e. more than the whole econ-print tail the scan had been
admitting. They already reached the scan universe (not allowed, not
blocked — the main book's company set and the finecon KPI set stay where
they are) and every one died at the STRUCTURE screen as `undated`: the
KPI class names its events by month with no day (`KXCCL-26SEPALBD`,
`KXF-26OCTUSSALES`, `KXBAA-28JANDELIV`).

The live probe settled what that month means: it is the REPORT month.
Ford's Q3 sales ticker says 26OCT and Kalshi's own occurrence is Oct 3;
Dollarama's 26SEPCOMP has occurrence Sep 12; Carnival's 26SEPALBD reports
late September. So the month is a release guard of exactly the kind the
day-dated midnight rule provides, one level coarser:

- `parse_event_month` reads the month-named segment (`26SEPALBD` ->
  2026-09-01 00:00 ET); day-dated, month-less (`DOG`, `RUS26SEP`) and
  bad-month segments stay None.
- `scan_report_month_cutoff`: an open-scan month-named event is OUT at
  00:00 ET on the first of its month, or earlier if the resolver / a
  series tightener / an event_start_overrides release already resolved
  earlier (min). EARLY is the safe direction — a KPI that leaks before its
  report (auto sales, monthly deliveries, airline traffic, a pre-announced
  comp) leaks INSIDE the report month, so the month costs accrual, never a
  fill against a public number. Deliberately NOT the main book's
  override-only behaviour (quote up to the Nasdaq release): that path is
  reviewed by hand per series; the scan is unreviewed. A series Jack wants
  quoted into its report month belongs in the finecon KPI set /
  `COMPANY_TICKERS`, the reviewed path.
  **NARROWED the same afternoon** (Jack, on KXDOL-26SEPCOMP: "narrow the
  month rule"): the month is a stand-in for "the report might land while
  we are quoting", and when Kalshi PUBLISHES a report date that falls
  AFTER the paying program window closes, that stand-in is provably wrong
  — the number cannot print while the bot earns, so the window is
  release-free and the month only forfeits accrual. `scan_report_date`
  reads the published date (occurrence >1h before expiration, the same
  test `trade_cutoff_utc` uses; occurrence == expiration means Kalshi
  publishes none), and the caller passes it with the program end. Report
  after the window -> no month cutoff, the occurrence-derived cutoff plus
  the `program_over` screen stand. Report inside the window, or NO
  published date (CHWY/KR/TTAN) -> the month applies, unchanged: an
  unknown date must fail toward quoting less (the 8/6 CELH asymmetry).
  Measured on the live feed the same afternoon: frees KXCCL-26SEPALBD
  (reports 9/30, program ends 9/11), KXDOL-26SEPCOMP (9/12 vs 9/11) and
  KXF-26OCTUSSALES (10/3 vs 9/11) — $356/day of pool, 26 markets. NOTE
  all three still fail the 250/event activity cap today, so the narrowing
  removes a wrong stand-down without yet changing what is quoted; see the
  activity-cap calibration note below.
  `imm_quote_gaps.scan_gap_label` deliberately reports NO report-month
  verdict: the narrowing depends on the report date and the program end,
  neither of which is in the persisted caches that function reads.
- `scan_series_is_fiscal` flags a series whose settlement source names
  fiscal.ai (`IMM_SCAN_FISCAL_SOURCE_KEYWORDS`); persisted on the series
  verdict as `fiscal`. `scan_shape_reason(..., fiscal=True)` waives
  `undated` only when the month parses; binaries stay `shape`.
- Order of operations: the string pre-screen (`scan_month_prescreen_ok`)
  lets a month-named ticker hydrate when its series is fiscal or NOT YET
  JUDGED (one hydration; a judged non-fiscal month series is then screened
  on the string without a read); `_scan_admission` takes the budgeted
  series read BEFORE the structure verdict for those, re-reads a verdict
  persisted before the flag existed, returns `series_meta_pending` with no
  budget (never admitted on the month alone), `shape` for a month binary
  without spending a read, and `cutoff_passed` once the report month has
  begun. Members (quote-to-completion) complete at the month start.
- `imm_quote_gaps.py` mirrors all of it: `build_meta` applies the month
  cutoff to scan-universe tickers; the label reads `screens pending` for
  an unread month-named series, `undated` for a judged non-fiscal one,
  `cutoff_passed (report month)` inside the month, `shape` for binaries.

What it means today (9/6): the five 26SEP events (CCL, CHWY, DOL, KR,
TTAN) are already inside their report month — out, correctly (Chewy
reports ~Sep 10, Kroger ~Sep 11, Dollarama Sep 12, Carnival late Sep).
Admissible now: KXYUM-26NOVTBSSS (14 mkts, $192/d, to Nov 1), KXF-
26OCTUSSALES (13, $179/d, to Oct 1), KXFA-28JANUSSALES (11, $151/d) and
KXBAA-28JANDELIV (8, $286/d) — the last two are running annual tallies
(Boeing/Ford publish monthly), the public-running-tally class the 9/2
sweep rejected by hand for KXTECHLAYOFF; here the machine screens are the
defence (KXBAA's 560 strike read 16,591 contracts/24h -> `volume`; the
95c annual strikes -> `extreme_mid`; the rest face the 72h history
screen). All of them still need the 24h age, <=60/day, quiet-72h screens
and a free slot: the tier stood at 20/15 with 5/5 openings used, so the
first KPI admissions come at the ET rollover, and by ROI they will
out-rank the $14/day state prints.

### 2026-09-06 — the activity caps were never measured (finding, NOT yet changed)

Jack asked where `SCAN_MAX_VOLUME_24H` (60/market) and
`SCAN_MAX_EVENT_VOLUME_24H` (250/event) came from. Answer: they were
guessed. Both entered in efb9707 (9/5) from the container that could not
reach the Kalshi API — the DEPLOYMENT CAVEAT above — and neither has been
touched since. The source comment's basis is an analogy: "the 9/2 finecon
members all read near-zero 24h volume at enrollment."

Measured against the live feed 9/6 (594-market scan universe, $24.8k/day
of pool). Nothing below is implemented; it is the evidence for whoever
takes the decision.

- **The analogy does not hold.** 3 of the 8 finecon markets selected that
  afternoon exceed the scan's own 60/day cap; two KXAMZNCC strikes read
  ~1,700/day. The curated tier the caps were modelled on would be
  substantially rejected by them.
- **Volume is bimodal, so the MARKET cap is nearly free.** 58% of the
  universe reads exactly zero; p60 = 13.7, p70 = 329. Almost nothing lives
  between 60 and 250, so moving the market cap anywhere in that band
  changes ~15 markets.
- **The EVENT cap is the binding constraint.** Holding the market cap at
  60: event cap 250 -> 205 markets / $11.9k per day; 1000 -> 271 / $13.3k;
  no event cap -> 394 / $17.7k. It withholds 189 individually-quiet
  (<=60/day) markets worth ~$5.8k/day of pool.
- **And it measures the wrong thing.** The event figure is a SUM over
  every strike compared against a fixed 250, so an event fails on BREADTH
  as much as on activity: KXAGTWINNER-26SEP24 has 11 quiet strikes and a
  757 total, no busy strike at all, just a wide ladder. Contrast
  KXYTDAILYTOPVIDEOG-26SEP07 at 7,430, where a genuinely hot strike is
  poisoning its neighbours — the case the code comment says the screen is
  FOR ("informed flow on one strike shows up on its siblings"). A sum
  cannot express that intent; `max()` over the event's strikes can.

Suggested (unimplemented): score the event on its BUSIEST strike rather
than the sum, and leave the market cap at 60 or raise it to 250 — the
distribution gap makes that choice nearly free either way. Both are env
knobs, so an experiment needs no code change: `IMM_SCAN_MAX_EVENT_VOLUME_24H`
and `IMM_SCAN_MAX_VOLUME_24H`. This is also what currently keeps the three
events freed by the report-month narrowing (CCL 1,757 / DOL 675 / F 3,220
event volume) out of the book.

### Knobs (env, prefix IMM_SCAN_)

`TOP_N` 15 (0 = tier OFF, nothing else in the bot reads these) ·
`EVENT_TOP_N` 3 · `DAILY_OPENINGS` 5 · `LEVELS` unset = global ladder ·
`MAX_POSITION` unset = global cap · `REF_MULT_CAP` 0 = uncapped ·
`HOUR_MULT` 1 · `REQUIRE_DATED` 1 · `REQUIRE_NUMERIC` 1 ·
`MIN_AGE_H` 24 · `MAX_VOLUME_24H` 60 · `MAX_EVENT_VOLUME_24H` 250 ·
`HISTORY_H` 72 · `MIN_HISTORY_BARS` 12 · `MAX_RANGE` 10 · `MAX_JUMP` 6 ·
`MAX_HISTORY_VOLUME` 250 · `HISTORY_TTL_H` 6 · `SERIES_META_TTL_D` 7 ·
`MAX_BULK` 600 · `MAX_BOOKS` 120 · `MAX_SERIES_FETCHES` 30 ·
`MAX_HISTORY_FETCHES` 40 · `FILL_HALT` 0 = off · `MID_JUMP` 0 = off ·
`DRIFT` 0 = off ·
`DAILY_LOSS_LIMIT` 75 · `SERIES_STRIKES` 2 · `SERIES_STRIKE_DAYS` 7 ·
`EXCLUDE_CATEGORIES` (EMPTY since 2026-09-06 — a name here is a
deliberate re-ban) · `EXCLUDE_PREFIXES` · `LIVE_SOURCE_KEYWORDS`.
Widening levers, in order of how much risk they add: `MAX_VOLUME_24H`,
`LIVE_SOURCE_KEYWORDS` (dropping a scoreboard/index keyword admits
markets everyone else prices off a live feed), `REQUIRE_NUMERIC=0`
(admits "will X happen" binaries — the resolution-jump class; don't).
Tightening levers: `EXCLUDE_CATEGORIES` (name a category to ban it
again; cached verdicts follow within one refresh), the tripwires.

### Observability

- Refresh log: `open-scan: N string-screened -> M eligible -> k/15 members
  (+u/5 openings used today[, HALTED today]); rejects {...}` with the
  per-screen reject counts; `open-scan admit <ticker>: pool/est/mid/vol24h`
  per admission; `open-scan daily openings: n used`.
- `--status` table grew a TIER column (`scan` / `fin` / blank) and an
  `open-scan k/15` tail.
- `status_incentive_mm.json`: `scan_members`, `scan_slots`,
  `scan_openings_used`, `scan_evicted_events`, `scan_halted_today`,
  `scan_pnl_today`. `imm_state.json`: `scan_members`, `scan_book`,
  `scan_entry_mid`, `scan_evicted_events`, `scan_series_strikes`,
  `scan_history_cache`, `scan_series_meta`, `scan_halt_day`,
  `scan_admit_day/scan_admits_today`, `scan_pnl_carry`.
- Opportunistic email (`send_opportunistic_imm.py`): a combined headline,
  then one table per tier in the same format — FINECON and OPEN SCAN —
  each with its own slot/openings line (HALTED / evicted flags on the scan
  one) and TOTAL row (Jack 2026-09-06: "a similarly formatted table for
  non-finecon opportunistic bot"); Kalshi-credited footer covers both.
- Digest FINECON SWEEP section gained an OPEN SCAN block (members, accrual,
  openings, evictions, halt flag).
- Quote-gaps email: markets in the scan universe are labelled
  `open-scan: <cached verdict>` (undated / shape / category:<c> /
  live_source / history_* / evicted / series_struck / tier halted today /
  screens pending / eligible) from the bot's persisted caches — the script
  never fetches; excluded families read `excluded family (open-scan)`.

### DEPLOYMENT CAVEAT — read before merging

This was built in a container whose egress policy blocks
api.elections.kalshi.com (403 on CONNECT). The exclusion list and the
screens were designed from the repo's code and docs (the 9/2 finecon scan
notes, the other bots' series, the strategy doc), NOT from a live pass over
the programs feed, and the candlestick/series parsers are written tolerant
of both the cents and `_dollars` encodings but were exercised only against
fixtures. Before merging:

1. `python -m unittest test_incentive_mm` — 497 tests, green on Windows
   (the two pre-existing red items were fixed in this change: the 35
   `winreg` ImportErrors on non-Windows and the stale KXMAMDANIMENTION gate
   assertion from fdc3a17).
2. `python incentive_mm.py --status` on the trading box (read-only): read
   the `open-scan:` line — the rejects breakdown is the first live evidence
   of what the universe looks like — and the TIER column. Expect few or no
   admissions on the first pass (`series_meta_pending` / `history_pending`
   until the caches fill, ~an hour of refreshes live) and check that
   nothing admitted is a family Jack recognises as live-feed. If the
   candlestick parser sees a field shape it doesn't know, every candidate
   reads `history_thin` — that is the fail-closed direction and the log
   will say so; fix the parser, don't loosen the screen.
3. Merge to main; the bot self-restarts on the code change. The tier's
   kill switch is `IMM_SCAN_TOP_N=0` in the launcher (`restart_imm.ps1
   -Task` to apply), or its own loss budget.

Portability note: the five satellite scripts' `_env_from_registry` caught
only `OSError`, so `import winreg` raised on Linux and every test importing
them errored; they now also catch `ImportError` (Windows unchanged).
