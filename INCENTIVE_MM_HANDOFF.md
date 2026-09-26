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
screened:<reason>, under payout floor (global $1.50 since 2026-09-12, 1.5x the
exchange's $1.00 minimum; TEMP inherits it), zero yield, capacity, candidate cap). Second table: deliberately-off blocklist/freeze families with their
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
`IMM_EVENT_TOP_N` (default `KXAAAGAS:3`, prefix:N or `*suffix:N` since 2026-09-10, longest wins) caps each
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

## 2026-09-07 — profitability pass: 30 slots, undated opened, hopeless exit armed (Jack)

Measured first, on the live tier: 25 members earning an est **$6.58/day**
in total, while ONE fully admissible blocked event (KXTRUMPENDORSEMENTS,
target 300) was worth **$3.68/day** on its top 3 strikes and the three
weakest members were worth **$0.18/day** combined. The tier was slot-bound,
not screen-bound, and nowhere near its risk limit — $36 of collateral
deployed, -$1.90 MTM, against a $75/day loss budget.

**`SCAN_TOP_N` 15 -> 30.** The cheapest of the three: the constraint was
slot count, not exposure.

**Undated opened (`cutoff_known`).** `undated` was never really about the
ticker string — it is about whether ANY stand-down guard exists. A market
with no day in the ticker is now admissible when a cutoff RESOLVED
(Kalshi publishes an occurrence meaningfully before expiration, which
`trade_cutoff_utc` already turns into a real cutoff) or when it is a
Fiscal.ai month event. Everything else still rejects as `undated`, which is
most of the bucket and deliberately so: of the top 120 undated by pool,
only ~24% carry a derivable cutoff; the rest are YouTube view counts,
headlines, playoff and primary OUTCOMES, where quoting would mean quoting
straight through whatever resolves them. The string-level pre-drop is gone
(an occurrence lives on the market object, so these must hydrate to be
judged) and `_scan_admission` makes the call.

Measured: scan candidates 926 -> 1699 after the string screens, of which
71 undated markets (~$1,320/day pool) now clear the structure screen —
KXBAA/KXF/KXFA/KXKR/KXYUM/KXTTAN (Fiscal.ai KPI ladders) and
KXPRIMARYTURNOUT. **COST TO WATCH:** the candidate list now exceeds
`SCAN_MAX_BULK` 1000 by ~700, so the bulk cap drops far more than before.
It is pool-ranked and existing members are always kept, so nothing quoting
is lost, but low-pool candidates now go unread. Raising `IMM_SCAN_MAX_BULK`
is the lever if that starts hiding good markets.

**SAFETY FIX SHIPPED WITH IT:** opening undated made KXYTVIEWSW (327
markets) and KXYTVIEWSHIGH (139) reachable for the first time, and both
settle on `charts.youtube.com` — a public view counter that ticks
continuously, i.e. exactly the "everyone can price this but us" class. They
PASSED the live-source screen because no youtube keyword existed.
`youtube.com` added to `SCAN_LIVE_SOURCE_KEYWORDS`.

**Hopeless exit now applies to scan members (`SCAN_HOPELESS_EXIT`, default
on).** Jack: "refuse candidates that cannot reach a dollar before their
program ends." Entry ALREADY required a projected $1 — `reaches_min` is
`accrued + est x _quotable_days`, bounded by the program end — but a MEMBER
bypassed it forever: sticky members skip the floor and the hopeless exit
explicitly exempted `meta.scan`. So a slot could be held by a market that
had become mathematically unable to earn. Measured the same day: the
weakest member projected ~$0.59 against Kalshi's hard $1.00 per-market
floor, with 11 days still to run. Now a scan member sustained under the bar
for `HOPELESS_SUSTAIN_SECS` is evicted and its slot freed; the dip guard
still means one low reading cannot evict, and since this morning eviction
cancels every order rather than leaving a wind-down leg. FINECON stays
exempt — that group's absolute $1 projections are noisiest on deliberately
quiet long windows, which is why Jack made it quote-to-completion on 9/3.

**Band screen: a book entirely outside the quoting band never takes a slot**
(Jack 2026-09-07: "add a screen for books entirely outside the quotable
band, dont allow them as one of the 30"). The quote loop's per-side
top-in-band rule already stands a side down when its own touch is out of
band, and stands the WHOLE market down when both are — so such a market
could be selected, hold a slot, and rest nothing. `_scan_admission` now
rejects it as `band` before any read. Measured at ship time: 272 scan
candidates ($9,439/day of nominal pool) were in exactly that state,
led by KXMLBPLAYOFFS, KXHEADLINE, the KXYT* video families and
KXPRIMARYTURNOUT — all of which the undated opening had just made
reachable. ONE side in band is enough to admit: the healthy side still
earns, which is the same asymmetry the quote loop uses.
A MEMBER whose book widens into this state is not caught here (members skip
admission) but the hopeless exit gets it inside `HOPELESS_SUSTAIN_SECS`:
with no placeable quote the estimator returns 0/day, which cannot reach the
$1 floor. NOTE the touch can move back INTO band — including because our
own maker order improved it — so this is a snapshot test at admission, not
a permanent verdict on the series.

**Loss budget 75 -> 200**, raised with the slot count: it was never close
to binding (the 25-member tier ran $36 of collateral and -$1.90 MTM), and
doubling the slots doubles the book it covers.

**Scope note.** `SCAN_DAILY_LOSS_LIMIT` is the TIER's budget — realized
plus MTM across every market the scan ever admitted (`scan_book`), for the
ET day. Not per event, and separate from the whole-bot `DAILY_LOSS_LIMIT`
($1,200).

## 2026-09-07 — history screen rebalanced: two checks off, two loosened (Jack)

Jack, after an ELI5 walk through the four history checks: "turn off these
requirements: two-sided hourly bars >= 12, mid range <= 10c" and "adjust
this requirement: traded volume <= 1000, largest bar-to-bar move < 10c".

| check | was | now | why |
|---|---|---|---|
| `MIN_HISTORY_BARS` | 12 | **0 = OFF** | it demanded half a day of two-sided quotes before a market could be judged, which rejected exactly the quiet tail the tier hunts. A market nobody quotes is the archetype here, not a warning sign. |
| `MAX_RANGE` | 10c | **0 = OFF** | high-minus-low over the whole window punished slow drift as hard as news. A book that walked 12c in 1c steps is being repriced by everyone at once, and the bot re-quotes into it every cycle. |
| `MAX_JUMP` | 6c | **10c** | the check that matters. One hour-to-hour STEP is the moment stale quotes get run over, so it stays; the bar is raised to catch genuine gaps rather than ordinary moves. |
| `MAX_HISTORY_VOLUME` | 250 | **1000** | still the "someone is trading this" signal, tolerant of real but modest flow. |

EVERY cap is now independently disabled at `<= 0`, and all four stats are
still MEASURED and persisted whether or not their cap is armed — so the
caches, the `--status` view and the quote-gaps labels keep reporting what a
book actually did. `scan_history_verdict` gained guards for the empty and
single-bar cases that turning the bar-count check off makes reachable
(`max()` over no mids used to be unreachable, and would raise).

MEASURED by replaying all 337 cached verdicts through both rule sets:

| verdict | old | new |
|---|---|---|
| PASS | 245 | **270** |
| history_range | 28 | 0 |
| history_volume | 53 | 44 |
| history_jump | 11 | **23** |

Net +25 markets. Note history_jump goes UP: the old order checked range
BEFORE jump, so 12 markets with a big range from a single step were
labelled `history_range` and never reached the jump check. With range off
they fall through and are still rejected — correctly, and now under the
name that describes them. The 16 whose range came from drift now pass, as
do 9 that were only over the old 250 volume cap. Newly admissible families
include KXPADATACENTERS, KXILNUCLEAR, KXNECORNYIELD, KXKYBOURBONBARRELS,
KXNHMAPLE and two KXFA-28JANUSSALES strikes.

CACHED VERDICTS ARE RE-SCORED, not waited out (Jack, same afternoon:
"rescore cached verdicts against current caps"). History verdicts cache 6h,
so the loosening went live and the very next refresh still reported 27
`history_range` rejects written under the old caps. `score_history()` is
now the ONE place the four thresholds are compared, used by both a fresh
read and `scan_history_rescore()`, which re-applies today's caps to the
stats already persisted on a cache entry — no re-read, no API budget. A
knob change therefore lands on the next refresh. `imm_quote_gaps` re-scores
the same way, so a label can never report a stand-down that no longer
exists. FAIL CLOSED: an entry whose `why` is `history_thin` was written by
the old short-circuit that returned BEFORE measuring range/jump, so its
zeros are absence of data rather than evidence of calm — those, and any
entry missing the stats, return None and are re-read. Same instinct as
`scan_cached_verdict` for the category ban.

CONSEQUENCE TO WATCH. With the bar-count check off, a market that never
showed a two-sided quote in 72h now reaches the later screens. It is not
unguarded: `_screen` still requires a real two-sided book RIGHT NOW
(`one_sided`) and a mid inside the 5-90c band, and the 24h age screen still
applies. But "no quote history at all" is no longer a reason on its own.

## 2026-09-07 — a DROPPED market carries no orders (Jack)

Jack, after finding `KXSBUXCC-26OCT07-T98` not earning: "fine for market to
get dropped, but quotes on the dropped market should all be canceled."

WHAT HE SAW. T98 held a -50 short, had fallen out of the finecon selection
(the group was 20 members against a cap of 15 with all 5 daily openings
spent, so it could not re-enter), and was therefore a reduce-only
`managed_extra` leg. That leg rested exactly ONE order: a 82c bid against
an 84/85 book — one-sided and two ticks behind the touch, so it qualified
for nothing while the market's live $100/day program ran, and it still
carried fill risk. T99 and T100 were in the same state.

THE RULE NOW (`WINDDOWN_DROPPED`, `IMM_WINDDOWN_DROPPED`, default OFF).
Deselection cancels everything on the market. There is no reduce-only
wind-down leg: `restore_orphan_metas` is a no-op, the "remember metas"
loop does not run, any surviving `managed_extra` entries are released with
a log line, and the stray-order sweep cancels what is resting — exactly
the path blocklisted / no-rent / call-window freezes already take.
Positions ride to settlement; flatten by hand. Same conclusion Jack
reached for blocklisted series on 2026-07-25, when the gas retirement's
wind-down fire-sold longs into pinned books. `=1` restores the old
behaviour, and the five tests that exercise the wind-down machinery arm
the knob explicitly.

THE TRAP THIS CHANGE HAD TO AVOID. `event_net` (the per-event net cap) was
computed by iterating the MANAGED set, so dropping held markets from it
would have made their inventory invisible to the cap — the bot could then
have rebuilt the same exposure on a sibling strike of the same event
(SBUXCC would have looked flat while carrying -152). It now counts every
OWN-BOOK position whose event is managed, quoted or not. Strictly more
conservative than before, and pinned by
`test_dropped_inventory_still_counts_against_its_event_cap`.

WHAT THIS DOES NOT CHANGE. The pre-cutoff reduce-only window
(`IMM_PRE_CUTOFF_REDUCE_ONLY`, default 0) still applies to markets that
are STILL selected. Manual-standoff, cutoff, closing and blackout paths
already cancelled and are untouched.

### Opportunistic email: P&L covers the tier's BOOK, not its selection

Same morning, same root cause. The email summed MTM only over currently
SELECTED markets, so a market the bot had stopped quoting dropped out of
both the P&L and the EARN EST columns while its inventory and accrual were
still real. KXSBUXCC-26OCT07 reported `P&L $0.00` on 9/6 while its three
unselected strikes marked to **-$32.72** — which is what Kalshi's -$37
unrealized (liquidation marks, always below mids) was showing.

Rows are now built from the tier's BOOK: every market it is quoting, plus
every market of the tier it holds inventory in or has accrued reward on.
The tables gained a HELD column next to QUOTED (held = in the book, no
longer quoted), the headline reports both counts, and the footnote says so.
`mkts` still counts what is QUOTED, so the slot lines above each table
still line up with the tier caps.

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
| ACTIVITY | market `volume_24h` <= 80, EVENT MEAN `volume_24h` per market <= 100 (averaged over every bulk-read sibling, pinned strikes included; a SUM against 250, then a mean against 60, both on 2026-09-06), listed >= 6h (24h until 2026-09-10) | finecon members read ~0 volume at enrollment; informed flow on one strike shows up on its siblings; the history read needs data |
| HISTORY | 72h of hourly candlesticks: no bar-to-bar move >= 10c, traded volume <= 1000 (cached 6h). Bar-count and mid-range caps OFF since 2026-09-07 — every cap is independently disabled at <= 0, and all four stats stay MEASURED either way | a single hour-to-hour STEP is the moment stale quotes get run over; slow drift and a thin quote history are not that |

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
  Measured on the live feed the same afternoon: the rule itself stops
  cutting off KXCCL-26SEPALBD (reports 9/30, program ends 9/11),
  KXDOL-26SEPCOMP (9/12 vs 9/11) and KXF-26OCTUSSALES (10/3 vs 9/11) —
  $356/day of pool, 26 markets.
  **It changed NOTHING observable, and it is important to know why.**
  Verified after the deploy: the refresh reject counts did not move
  (`cutoff_passed` 79 before and after). Those 26 markets never reach the
  admission screens at all — they are cut one layer earlier by
  `SCAN_MAX_BULK`, which ranks scan candidates by PER-MARKET
  `dollars_per_day` and keeps 600. The KPI strikes sit at ranks 644-708
  against a rank-600 cutoff of $14.29/day; each strike is $13.66-13.74.
  That is the same breadth penalty as the event-volume cap: a 14-strike
  event worth $192/day ranks BELOW a 2-strike event worth $30/day, because
  the ranking never sees the event total. The Fiscal.ai change itself made
  this worse — waving month-named tickers through grew the candidate list
  and `bulk_cap` went 144 -> 311 over the day. So the KPI class needs the
  bulk cap raised (`IMM_SCAN_MAX_BULK`, ~12 bulk reads per 600 tickers, so
  1000 is cheap) or the ranking changed to consider the event pool, AND
  the event-volume cap below, before any of it quotes.
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

### 2026-09-06 — the activity caps were never measured (finding + the fix Jack chose)

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

**SHIPPED** (Jack, same afternoon: "keep market cap at 60, change event cap
to be avg_per_market_on_event, and set that to 60"). The market cap is
unchanged at 60. The event screen is now the MEAN 24h volume per market
over the event's bulk-read strikes, capped at 60:
`SCAN_MAX_EVENT_AVG_VOLUME_24H` / `IMM_SCAN_MAX_EVENT_AVG_VOLUME_24H`
(a NEW env name on purpose — an old `IMM_SCAN_MAX_EVENT_VOLUME_24H` value
was a sum against 250 and would be nonsense against a mean). The reject
reason in the refresh log is now `event_avg_volume`, not `event_volume`,
so old and new refreshes can be told apart.

Jack chose the mean over the `max()` suggested above. **Known loosening,
pinned by a test so nobody "fixes" it silently:** the mean lets ONE hot
strike hide behind quiet siblings (20 strikes, one at 1000, rest 0 -> mean
50, passes) where both the old sum and a max() reject the event. The hot
strike still fails the per-market cap; what changes is that its quiet
neighbours are admitted, relaxing the original "informed flow on one
strike shows up on its siblings" intent. Watch the tier's $75/day loss
budget for whether that holds.

Measured effect on the live feed, 9/6, over the post-bulk-cap universe
(594 markets): 203 markets / $11.9k per day admitted under the old sum,
209 / $10.8k under the mean. Net +10 markets, +$143/day — KXKSWHEAT-26SEP30
(7 strikes, sum 330, mean 47) and KXORBOFHARVEST-27NOV30 (5 strikes, sum
270, mean 54), exactly the wide-quiet-ladder class. Four KXTTELITEMATCH
events show as "lost" in a volume-only diff but were already past their
midnight-ET day cutoff, so nothing real was given up.

**The mean at 60 was about as strict in aggregate as sum<=250.** Worth
understanding, because it is the whole reason the thresholds moved again
an hour later. `mean <= C` is exactly `sum <= C x n_strikes`, so the mean
turns a FIXED sum allowance into one that scales with the ladder length.
At C=60 the crossover is n≈4.2: events with 4 strikes or fewer got
STRICTER, 5 or more got LOOSER. The 9/6 universe held 85 events at n<=4
and 65 at n>=5, so the two effects cancelled — 84 events passed the old
rule, 78 passed the new one. What it did was REALLOCATE, correctly on both
ends: quiet strikes in a long quiet ladder (KXKSWHEAT, n=7, mean 47) came
in, quiet strikes sitting beside a busy sibling in a 2-strike event
(KXTTELITEMATCH, mean 81-125) went out. It could never recover the $5.8k,
because event means are bimodal too — 57 events under 1, 59 over 120, only
5 in the 30-60 band — so the cap sits in a near-empty gap, and the big
withheld pool is BUSY PER STRIKE, not merely wide (KXBAA n=8 mean 7,288;
KXBWAYATTENDANCE n=8 mean 3,080). The statistic decides fairness across
ladder lengths; the THRESHOLD decides how much opens up.

**Thresholds and the bulk cap raised, same afternoon** (Jack: "raise to 80
for the market, and avg 100 for the event" + "raise IMM_SCAN_MAX_BULK to
1000"). Market 60 -> 80, event mean 60 -> 100, bulk 600 -> 1000. Measured
on the live feed, activity screens only (the series/history/age/shape
screens still cut further, so real admissions are a subset):

| config | markets | events | pool/day |
|---|---|---|---|
| bulk 600, mkt 60, mean 60 | 211 | 78 | $10,809 |
| bulk 600, mkt 80, mean 100 | 256 | 87 | $12,192 |
| bulk 1000, mkt 80, mean 100 | 341 | 98 | $12,951 |

+130 markets / +$2.1k per day, roughly half from the thresholds and half
from retiring the bulk cap (980 candidates that afternoon, so 1000 stops
binding). Biggest additions: KXTRUMPSAYCOMPANY-26OCT01 (44 strikes),
KXWALLSTBONUS-27MAR31, KXAGTWINNER-26SEP24 (the wide-quiet-ladder case),
KXTRUMPACT-26SEP06, KXCTMFPERMITS, KXWYCOAL.

NOTE the tier still admits at most `SCAN_TOP_N` 15 + `SCAN_DAILY_OPENINGS`
5 per ET day, so a wider candidate pool changes WHICH markets compete on
ROI far more than how many get quoted. And with the bulk cap retired, more
unjudged candidates reach the series/candle budgets (30/40 per refresh),
so the verdict caches take longer to fill and `series_meta_pending` /
`history_pending` are more common for a while — expected, not a fault.

Of the Fiscal.ai KPI events only KXFA-28JANUSSALES (8 of 11 strikes) now
clears the activity screens. KXDOL-26SEPCOMP sits at event mean 112 versus
the 100 cap, just outside; KXCCL 251, KXF 248, KXBAA 7,288.

**`SCAN_MAX_BULK` has the same breadth flaw and bites FIRST.** It ranks
candidates by per-market `dollars_per_day` and keeps 600, so a wide ladder
is punished for being wide however good its event pool is — the 9/6 KPI
events (CCL/DOL/F, $13.66-13.74 per strike) all land at ranks 644-708
against a $14.29 cutoff and never reach a single admission screen. Any
work on the activity caps should raise `IMM_SCAN_MAX_BULK` (or rank on the
event pool) first, or it will measure nothing.

### Knobs (env, prefix IMM_SCAN_)

`TOP_N` 30 (0 = tier OFF, nothing else in the bot reads these) ·
`EVENT_TOP_N` 3 · `DAILY_OPENINGS` 5 · `LEVELS` unset = global ladder ·
`MAX_POSITION` unset = global cap · `REF_MULT_CAP` 0 = uncapped ·
`HOUR_MULT` 1 · `REQUIRE_DATED` 1 · `REQUIRE_NUMERIC` 1 ·
`MIN_AGE_H` 6 (24 until 2026-09-10) · `MAX_VOLUME_24H` 80 · `MAX_EVENT_AVG_VOLUME_24H` 100
(MEAN per market on the event since 2026-09-06; the old
`MAX_EVENT_VOLUME_24H` sum-against-250 knob is GONE, not renamed) ·
`HISTORY_H` 72 · `MIN_HISTORY_BARS` 0 = off · `MAX_RANGE` 0 = off ·
`MAX_JUMP` 10 ·
`MAX_HISTORY_VOLUME` 1000 · `HISTORY_TTL_H` 6 · `SERIES_META_TTL_D` 7 ·
`MAX_BULK` 2000 (1000 until 2026-09-10) · `MAX_BOOKS` 120 · `MAX_SERIES_FETCHES` 30 ·
`MAX_HISTORY_FETCHES` 40 · `FILL_HALT` 0 = off · `MID_JUMP` 0 = off ·
`DRIFT` 0 = off ·
`DAILY_LOSS_LIMIT` 200 (TIER-wide, per ET day) · `SERIES_STRIKES` 2 · `SERIES_STRIKE_DAYS` 7 ·
`EXCLUDE_CATEGORIES` (EMPTY since 2026-09-06 — a name here is a
deliberate re-ban) · `EXCLUDE_PREFIXES` · `LIVE_SOURCE_KEYWORDS` ·
`LIVE_OVERLAP_MIN` 0.8 · `LIVE_TAIL_H` 24 (the live-feed rule's reward-window
prong, 2026-09-24; OVERLAP_MIN <= 0 = the pure live-source reject).
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
  then one table per tier in the same format — FINECON, OPEN SCAN and
  (since 2026-09-11) CARBON ARC *CC — each with its own slot/openings line
  (HALTED / evicted flags on the scan one) and TOTAL row (Jack 2026-09-06:
  "a similarly formatted table for non-finecon opportunistic bot"), then
  the CUMULATIVE per-tier table; Kalshi-credited footer covers all three.
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

## 2026-09-09 — the finecon group had no ceiling (Jack)

Jack, after I explained the admission semantics: "yes add it."

WHAT WAS WRONG. Both opportunistic tiers share one admission walk,
`_group_walk_cut`, whose size line is

```python
admit_cap = max(len(keep), top_n, max(0, refill_to)) + max(0, extra_openings)
if hard_cap > 0:
    admit_cap = min(admit_cap, hard_cap)
```

The open scan passes `refill_to` and `hard_cap=scan_ceiling()`. Finecon
passed neither, so its cap reduced to `max(members, FINECON_TOP_N) +
openings`. That `max` is a ratchet: once membership passes the cap, the
CURRENT SIZE becomes the new floor, and each ET day's openings stack on the
previous high-water mark. Members quote to completion and are never
out-ranked, so only attrition — a program ending, a safety screen, a
hopeless eviction — ever pulls the group down. On any day attrition is
under `FINECON_DAILY_OPENINGS`, the tier is permanently larger.

Measured live 2026-09-09 15:28Z: **30 members against a nominal cap of 25**,
with all 10 of the day's openings already spent. Next morning's cap would
have been 40, then 50, then 80 inside a week at zero attrition. Raising
openings 5 -> 10 earlier the same day had doubled the climb rate.

THE RULE NOW. `finecon_ceiling()` = `FINECON_TOP_N +
max(0, FINECON_DAILY_OPENINGS)` = 35, passed as `hard_cap`. Steady state is
that number, not `FINECON_TOP_N`. `IMM_FINECON_DAILY_OPENINGS=0` gives a
hard `FINECON_TOP_N`. This is additive-only: incumbents are seeded into
`keep` BEFORE `admit_cap` is consulted, so a group already above the
ceiling (as it is today at 30, and would be at 36+) evicts nobody — it
simply admits no one until attrition brings it under.

REFILL CAME WITH IT, and had to. `members` is built from the post-screen
`ranked`, so a market that departed this refresh is already subtracted;
once the group sits at the ceiling with the day's openings spent,
`extra_openings` is 0 and `admit_cap` collapses to the survivor count. The
freed seat then stays empty until the next ET midnight — the exact dead-seat
bug the open scan had (measured 9/8: 15h07m with zero admissions while it
bled 35 -> 30). Finecon has carried that bug since 9/3; the ratchet just
papered over it every morning. `FINECON_REFILL_ON_DEPARTURE`
(`IMM_FINECON_REFILL_ON_DEPARTURE`, default ON) closes the seat in the same
pass.

The refill target is `fin_prev_count` = the pre-refresh finecon selection,
counted off `prev_selected` rather than a persisted member list — finecon
has no `scan_members` equivalent. That is exact, not a proxy:
`prev_selected = state.selected | state.sticky_prev`, and `sticky_prev` is
populated only from the persisted selection at load and cleared at the end
of the first refresh, so the union is the pre-refresh selection in both the
steady-state and post-restart cases.

A REFILL IS NOT AN EXPANSION. The openings counter's baseline became
`max(len(fin_sticky), fin_prev_count)`, so a seat a departure freed no
longer bills an opening it never used. Same correction the scan tier got.

Pinned by `test_finecon_group_is_bounded_and_refills`: the ceiling binds at
the live 30-member shape, incumbents above the ceiling are never cut, the
freed seat refills and charges nothing, the knob restores the bleed-down,
and `FINECON_DAILY_OPENINGS=0` collapses the ceiling to the cap.

## 2026-09-09 — a cut market was burning a lifetime event slot (Jack)

Jack, after the adversarial review of the finecon ceiling surfaced it:
"fix the lifetime slot ordering too."

WHAT WAS WRONG. The lifetime slot ledger claimed an invariant it did not
hold. Its own comment says the ledger is "written after it with whatever
the cut kept — so a market only ever burns a slot by actually surviving
into the selection." It was written immediately after `event_top_n_cut`,
and THREE more filters run after that point:

| Filter | Line |
|---|---|
| `finecon_group_cut` | ~6799 |
| `scan_group_cut` | ~6822 |
| MAX_MARKETS / collateral budget loop | ~6900 |

A market taken by any of them had already appended itself to
`state.event_slots[ev]["markets"]`. Because those slots are PERMANENT, the
event lost one forever to a market that was never quoted and never earned a
cent. Measured 2026-09-09 17:17Z, restricted to events with live liquidity
programs: 3 such slots — `KXDIESELYE-26DEC31` holding 2 of its 3, and
`KXAAAGASDVA-26SEP10` holding 1 of 3. (The 87-of-162 figure across the whole
ledger is mostly expired Sept 8 gas dailies; that is stale ledger, not a
leak.)

THE FIX. The append moved to just after `self.state.selected = selected`,
where the selection is final. `ranked` is still what gets iterated, filtered
by `_m.ticker in selected`, so the ledger keeps its ROI ordering and the two
agree on membership (`selected` is built only from `ranked`).

THE TRAP THIS CHANGE HAD TO AVOID. The `ts` touch could NOT move with the
append. `ts` is the persist filter (`EVENT_SLOTS_TTL_SECS`, 14 days), and it
was being refreshed for every event with a surviving candidate. Had the whole
block moved, an event whose candidates were all cut would stop being touched,
age out of the state file, and come back with a FRESH lifetime budget — the
exact "kept admitting replacements" the ledger exists to stop. The touch
therefore stays where it was, now doing nothing but `setdefault` + `ts`, and
only the slot spend moved.

THE PER-EVENT CAP IS UNAFFECTED. `event_top_n_cut` reads `slots_used` from
the ledger as of PREVIOUS refreshes, above the append either way. Appending
later cannot let an event exceed N, because only markets that cut already
allowed can reach the selection.

Pinned by `test_a_cut_market_never_burns_a_lifetime_event_slot`, driven
through the MAX_MARKETS filter (the last of the three, furthest from the old
write point). It asserts the cut event spent nothing AND still holds a live
ledger entry so the TTL cannot age it out. Verified to FAIL on 54ef318:
`Lists differ: ['KXGOOD-99DEC30-A'] != []`.

WHAT THIS DOES NOT FIX. The 3 slots already burned stay burned. State alone
cannot tell a slot mis-recorded by this bug from one legitimately spent by a
market that was quoted and later dropped without fills — both read as "not
selected, no fills" — so an automatic repair would over-release and weaken
the lifetime semantics. They release through the normal rule
(`_live_event_slots`) once those markets go out of band.

## 2026-09-10 — KXRAINWKND weekend rain allowlisted, out at the ticker date (Jack)

Jack: "allowlist KXRAINWKND in IMM bot, but only quote until the cutoff e.g.
KXRAINWKND-26SEP12 stops quoting on 9/12".

WHAT THE SERIES IS. `KXRAINWKND-26SEP12-<CITY>`, "Where will it rain this
weekend (Sep 12 - Sep 13)?": YES if total precipitation at the city's CLI
station is > 0 on either day. 23 cities. Listed Thursday ~21:00Z, closes
Monday 05:00Z, settles on The Weather Company (weather.com/kalshi), same
source as the KXRAIN dailies. One `series_lip` program per market at $100
(period 9/10 21:01Z -> 9/14), $2,300 on the event — about $29/day/market.
The overrides task had been filing it as `review: KXRAINWKND (unclassified)`
three times a day since 9/5; an allowed series is skipped by the classifier,
so that line stops.

THE CUTOFF. The ticker date is the weekend's FIRST day, so the bot's plain
midnight-ET ticker rule (`trade_cutoff_utc`) already reads "out at 00:00 ET
Saturday" — exactly the ask. The new `SERIES_OVERRIDES["KXRAINWKND"]` pins
it: `cutoff_before_event_min=0` (env `IMM_RAINWKND_CUTOFF_BEFORE_MIN`; the
dailies run 120 = 10pm the night before, deliberately NOT copied — Jack named
the date), rain band 5-90c, global ladder. Both producers (selection and
orphan-restore) go through `apply_series_cutoff_adjustments`, so a restored
position gets the same instant. Kalshi's `occurrence_datetime` on these
(9/15 05:00Z) sits AFTER `expected_expiration_time` (9/15 03:59Z), so it is
never a candidate and cannot move the cutoff later; the `_screen` cutoff
rule + exchange-side order expiration at the cutoff are the hard stop.
Fresh entry stops `IMM_CUTOFF_SCREEN_BUFFER` (5 min) earlier, members quote
to the instant. For 26SEP12 that is Fri 9/11 23:55 ET (fresh) / Sat 9/12
00:00 ET (members) = 04:00Z; the pre-filter drops the event outright from
Sun 04:00Z. ~26.5h of a ~82h program are quotable per weekend; the rest is
forfeited by design.

ALLOWANCE. In CODE: `_DEFAULT_WEATHER_SERIES = "KXRAINWKND"` (env
`IMM_ALLOW_WEATHER_SERIES`), merged into `ALLOW_SERIES`. Exact series, no
prefix — the daily KXRAIN stays where it was (the extra-allow file) and the
KXRAIN<CITY>M monthlies stay behind the launcher blocklist. The open-scan
ownership exclusion (`KXRAIN` prefix) still covers it, which is correct: it
is enrolled, not scanned.

WHAT READS ACROSS FROM THE DAILIES, AND WHAT DOES NOT.
| Rule | Match | Weekend? |
|---|---|---|
| 7pm-01:59 ET size halving (`IMM_SERIES_HOUR_MULT` `KXRAIN:19-1:0.5`) | startswith | yes — Thu/Fri evenings run half size, same as a daily two days out |
| quiet-hours 3-7am x2 (launcher `IMM_HOUR_SIZE_MULT`) | global | yes |
| 5-90c band + top-in-band stand-aside | override | yes |
| NWS fair gate / directional take / curated next-day tier | `== RAIN_FAIR_SERIES` (exact `KXRAIN`) | no — the weekend contract is P(rain on either day); the daily station fair does not price it |
| 10pm-night-before early stop | override, per series | no (0 here) |

Pinned by `test_rain_weekend_series_allowed` and
`test_rain_weekend_quotes_to_ticker_date_midnight` (cutoff through both
producers, screen at the instant, hour-mult inheritance, fair-gate
non-inheritance). Deployed through the source-mtime self-restart.

## 2026-09-10 pm — *CC Carbon Arc family into the NORMAL book; scan age 24h -> 6h (Jack)

Jack, on KX30YMORTW-26SEP17 / KX10YRDIRHM-26SEP30H / KXURBNCC-26OCT07
not being quoted: "drop the open scan 24h requirement to 6h. allowlist the
CC Carbon Arc family -- KXURBNCC, KXDGCC, KXCOSTCC, etc. and they should be
picked up by the normal IMM right not the opportunistic".

WHY THE THREE WERE OUT (measured 2026-09-11 ~01:45Z):
- KX30YMORTW-26SEP17: not allowlisted, so open-scan only; its markets
  opened 22:40Z 9/10 and the scan wanted 24h of listing age. The tier was
  also at its 35/35 ceiling. Last week's SEP10 event had no program at all.
- KX10YRDIRHM-26SEP30H ("10Y how high monthly"): the treasury enrollment
  is ten exact KXUST{2,5,7,10,30}A{D,M} names, no prefix; the scan rejects
  it on activity every cycle (event mean 1,604 contracts/market/24h vs the
  100 cap; 10 of 21 strikes pinned 96-99c because the yield already
  touched them). A one-touch max on a live yield -- left out by design.
- KXURBNCC-26OCT07: KXURBN (Fiscal.ai KPI) is in _FINECON_KPI_SERIES;
  KXURBNCC is a different Carbon Arc series and allowlisting is exact.
  Its first program ran 8/28-8/30 (filed "unclassified" before the 9/5
  self-extend rule existed); the second lit 9/10 22:02Z, after the 4:45pm
  overrides run. The finecon tier was also at its 35/35 ceiling.

CHANGE 1 — ALLOW_FAMILY_SUFFIXES ("CC"; env IMM_ALLOW_FAMILY_SUFFIXES):
a name-pattern allowlist for the NORMAL book, deliberately separate from
ALLOW_SERIES_SUFFIXES (that tuple is the MENTION family and three call
sites read it as mention semantics: mention_cutoff_is_clear, the
no_event_window stand-down, the never-pre-drop-on-ticker-date rule).
Live feed 2026-09-10 pm: all 30 paying *CC series are "<Company> Credit
Card Spend", Financials, Carbon Arc, 9 strikes each on 26OCT07 -- the
suffix IS the family, no false positive in the feed. Guards: a new
"family_suffix" kind in FAMILY_OVERRIDE_PARENTS clones the KXAMZNCC
archetype (safe-join, no fresh-candidate rate bar -- the FT/APP consumer-
observation set) WITHOUT the exact/extra-allow membership the *FT/*APP
suffix entries require, because here the suffix is the membership.
KXAMZNCC left _DEFAULT_FINECON_SERIES and got its own SERIES_OVERRIDES
entry; KXSBUXCC was removed from finecon_extra_series.json (hot reload)
after the new code was live, so both keep quoting as sticky normal-book
selections. Being `_allowed`, every *CC series is now "allowed" to
scan_universe_reason and never scanned; the overrides task's Carbon Arc
self-extend skips them before classification (`_allowed` first), so the
finecon file cannot re-absorb them. Blocklist still wins in `_allowed`.

Effect on the normal book: up to 30 new events x 9 strikes (~$7/market/
day, ~$1.9k/day of pool) compete under the usual screens (5-90c band,
extreme_mid, $1/market payout floor) and the IMM_MAX_MARKETS=100 event
cap, which was at 73 events -- expect it to bind.

CHANGE 2 — SCAN_MIN_AGE_HOURS default 24 -> 6 (IMM_SCAN_MIN_AGE_H).
6h still keeps same-day price structures out and leaves the 72h history
read something to see. Comments updated where they said 24h.

Tests: test_cc_family_suffix_allows_into_normal_book; the finecon
membership test drops KXAMZNCC; test_finecon_daily_openings' synthetic
group members were KXAMZNCC tickers and are KXVENEZCRUDE now (same
shape, still a base member); the scan-admission test pins the 6h default
and admits a 7h-old listing.

## 2026-09-10 pm (later) — *CC events capped at 3 by ROI; scan bulk read 1000 -> 2000 (Jack)

Jack, minutes after the *CC family went into the normal book (27 events,
234 selected strikes, 100/100 events): "just quote max 3 markets per
event, based on ROI, for CC family" and "bulk read the top 2k markets by
pool by day".

CAP: `IMM_EVENT_TOP_N` now accepts a `*suffix:N` pattern next to the
prefix ones; default is `KXAAAGAS:3,KXDIESEL:3,KXTRUEV:3,*CC:3`. Same
machinery as gas: ROI rank (two-sided first), 1.15x incumbency, sticky
members, LIFETIME slot ledger. On the first refresh the 9-strike CC
selections are trimmed to 3 by the concurrent path (no ledger entries
yet), the 3 survivors are recorded, and the 6 cut wind down reduce-only.
A bare `*` or empty pattern is a config error.

BULK: `SCAN_MAX_BULK` 1000 -> 2000. Measured 2026-09-11 01:45Z: the scan
pre-read list was 2,718 markets and rank 1000 sat at ~$24/day/market, so
KX30YMORTW-26SEP17 ($14.91, rank ~1588) never reached an admission screen
and the 6h age change could not help it (the `age` reject key vanished
from the scan line for exactly that reason). 2000 covers it with headroom;
cost 20 -> 40 bulk reads per 600s refresh.

## 2026-09-10 pm (later still) — event cap 100 -> 150 (Jack)

Jack: "increase 100 event cap to 150". Launcher `$ProbeEnv`
`IMM_MAX_MARKETS=150` (run_incentive_mm.ps1); applied with
`restart_imm.ps1 -Task` (env lives in the running launcher, so the bot's
own code-change restart cannot pick it up). Before: 100/100 events with the
three CC events KXCFILCC/KXWENCC/KXWMTCC out as `not_ranked`.

## 2026-09-11 — KXTRUEV sunset: 26SEP11 quotes to completion, series blocked going forward (Jack)

Jack: "quote KXTRUEV-26SEP11 until completion, but block KXTRUEV going
forward".

STATE AT THE TIME (08:13 ET). KXTRUEV-26SEP11 was the only open KXTRUEV
event: 15 strikes, $600 of programs to 9/12 03:59Z, 3 markets selected under
the top-3 ROI cap (T1213.73 / T1223.73 / T1233.73), cutoff = close - 60 min =
10:59pm ET tonight. Positions on the settled 26SEP10 strikes (55/60/60) ride
to settlement as they would under any blocklist.

MECHANISM. Two pieces, both in code (the launcher `IMM_BLOCKLIST` is not
touched):

1. `"KXTRUEV"` joins `SERIES_BLOCKLIST_PREFIXES`. Blocklist semantics as
   always: no orders at all, not even reduce-only; positions ride; the
   daily classifier files the series as `skip: blocklisted` rather than
   review; the extra-allow loader refuses it.
2. `BLOCKLIST_WIND_DOWN_EVENTS = {"KXTRUEV-26SEP11"}` (env
   `IMM_BLOCKLIST_WIND_DOWN_EVENTS`): `_blocked()` — the single choke point
   every freeze path already goes through (candidates, orphan-restore,
   managed flush, open-scan verdicts) — lets a market of a named event
   through a prefix block. The event then runs under every rule it had
   before: top-3 cap, close-anchored cutoff, 5pm halving, safe-join. When
   its cutoff passes it leaves through the ordinary cutoff/closing path;
   the entry is then inert and needs no cleanup.

A plain blocklist entry would have cancelled the three resting ladders on
the next cycle (the 8/25 shape); `NO_NEW_SERIES` would have grandfathered
the three members but refused a replacement strike if one dropped out of
band. The event-scoped exemption is the reading that matches the ask.

KEPT ON PURPOSE for a one-line un-block later: the `_DEFAULT_ECON_SERIES`
entry, the `KXTRUEV:3` top-N cap, the `KXTRUEV:17-1:0.5` halving and the
close-60 override. The blocklist entry wins over all of them.

Pinned by `test_truev_sunset_winds_down_one_event`; the two 9/7 un-block
pins were flipped to the new state. Deployed through the source-mtime
self-restart; verify with `imm_feed_audit.py` (26SEP11 must NOT appear as an
excluded-with-programs event; later KXTRUEV dailies appear in the
not-allowed leaderboard as deliberate exclusions).

## 2026-09-11 — *CC back in the opportunistic email; CUMULATIVE table (Jack)

Jack: "AmazonCC and StarbucksCC arent in the opportunistic daily email
anymore. make sure it shows not just active, but also cumulative".

WHY THEY VANISHED. The 9/10 family rule (previous section) moved the whole
*CC set into the NORMAL book by name pattern, which meant taking KXAMZNCC
out of `_DEFAULT_FINECON_SERIES` and KXSBUXCC out of
`finecon_extra_series.json`. The opportunistic email's tier test was a
closed two-way enumeration of the two QUOTING groups — finecon by
`FINECON_SERIES`, else scan by `scan_members` — and every row-set
comprehension filters on its truthiness, so a market matching neither is
dropped from the email entirely rather than rendered untiered. A *CC ticker
can never be scan either (it is `_allowed`, so `scan_universe_reason`
returns "allowed" and it is never a scan candidate). All 30 series / 30
events / 90 selected markets left the report in silence, and with them the
$9.00 of real Kalshi credit already booked on KXSBUXCC-26OCT07 ($5.21) and
KXAMZNCC-26OCT07 ($3.79) — `_is_opp_event` gates the footer off the same
predicate.

The root cause is that QUOTING tier and REPORTING tier are different
questions. Jack's 9/10 "picked up by the normal IMM right not the
opportunistic" was about slots and caps (no finecon walk, no scan screen);
his 9/11 ask is about this scorecard. Nothing in the bot changed here.

CHANGE 1 — THIRD REPORTING TIER. `tier_of(ticker_or_event, fin, scan_set)`
is now MODULE LEVEL in `send_opportunistic_imm.py` (it was a closure inside
`build_report`, which is exactly how 538 green tests missed 30 vanished
series — the tests could only reach the pure formatters). Precedence is
finecon, then scan, then `is_family()` — the *CC arm reads
`imm.ALLOW_FAMILY_SUFFIXES`, the bot's OWN membership constant, so a future
family suffix lands in the email for free. finecon first because a series
in both sets is walked and capped as finecon; scan before family because
scan membership is an explicit per-ticker fact the bot persisted. The tier
line reports no slot budget, because the family has no tier walk — it
states the caps that actually bind, read from the bot
(`imm.event_top_n_for` → `*CC:3`, `imm.MAX_MARKETS` → 150).

CHANGE 2 — CUMULATIVE TABLE + per-row CREDITED$. The per-tier tables stay
the ACTIVE book; the new CUMULATIVE table is the same three tiers over
every event each has ever touched, settled-and-gone included. Columns and
their bases:
- CREDITED  actual Kalshi money, per event, all-time, from the recon
            ledger (back to 2026-03-21, so complete). Also a per-row
            CRED$ column in the active tables, BLANK not 0.00 when nothing
            has landed. Footer carries an as-of stamp ("ledger through
            <date>, Nd behind") and a LEDGER STALE banner past
            `sd.LEDGER_STALE_DAYS` — `reward_credits.csv` is a hand-run
            statement paste, so a $0.00 that really means "not pasted yet"
            must not read as "earned nothing".
- EST       the bot's accrual estimator, over the markets it is tracking
            RIGHT NOW. EST and CREDITED measure the same earnings at
            different scopes and **NEITHER CONTAINS THE OTHER** — no
            renderer sums them. NET stays EST + REALIZED + MTM, the same
            basis as the active tables' NET. The old footnote called EST
            "period-to-date", which was wrong; a first pass then called
            CREDITED "a SUBSET of EST", which was also wrong — see the
            2026-09-11 pm correction below.
- REALIZED  per-market realized trading P&L summed from the `realized`
            sink's DELTAS. That sink starts 2026-09-06 (commit ffe4b48),
            and the footer states that floor rather than implying history
            it does not have.
- MTM       the open book right now, same `own_book()`/mids path.

CHANGE 3 — DURABLE TIER ATTRIBUTION (`opportunistic_roster.json`). finecon
and *CC are series-name rules, durable by construction. The scan tier is
per-TICKER and the bot PRUNES it — `scan_book` sheds flat non-members at
every daily roll — so a settled scan event silently stops being ours.
`durable_history()` folds the `selection_events` sink's `is_scan` flag into
a per-event set and caches it. Measured 2026-09-11: 7 credited scan events
worth $118.40 had already left `scan_book`, against $21.83 the two-tier
footer was reporting as the book's whole lifetime. Footer now reads
$149.23 (finecon 21.83 + scan 118.40 + *CC 9.00).

The cache folds once per UTC day so the email stays O(one day) as the sinks
grow (27MB on 9/11, ~4MB/day) and keeps its history if those files are ever
archived off. The scan roster is a SET and folds idempotently; the realized
map is DELTAS and does NOT, so only COMPLETE UTC days are ever cached and
today's partial file is summed live on top every run. That asymmetry is the
one real hazard here and is pinned by
`test_durable_history_folds_sinks_without_double_counting`. The call is
wrapped: `build_report` is retried 8x/5min and a sink read must never cost
the whole day's email — on failure the cumulative table degrades to the
live book.

ALSO FIXED, same bug class: the ticker-level tier test used `scan_members`
where it should have used `scan_book`, so a market the scan tier had
stopped quoting but still held inventory in was dropped from the tier's
book — contradicting the 2026-09-07 "scope the email's P&L to every market
in the tier's book" rule. Slot counts stay on true membership so the tier
line cannot inflate. Worth +2 events / +4 held markets on the day.

NOT CHANGED: quoting, selection, caps, the launcher, the schedule. The
"KL imm opportunistic" task runs the .py directly with no flags, so the
7:25 AM ET run picks this up with no restart. Verify with
`python send_opportunistic_imm.py --dry` (a plain re-run is a no-op once
the day's marker exists; `--test` resends for real).
Pinned by six new cases in `TestOpportunisticEmail`.

### Correction (2026-09-11 pm): EST does NOT contain CREDITED

Jack, reading the test send: "how can credited$ be more than earn est$ for
certain markets?" He is right and the first footnote was wrong. Two live
counterexamples:

- **KXCBDECISIONNZ-26OCT27** — credited twice ($4.61 + $6.31 = $10.92) but
  EST $6.33. Credits are per EVENT and forever; a row's EST sums only the
  tickers still in its book, and only `-HOLD` survives in state (the one
  market still carrying inventory, -40). The other market's $4.61 has no EST
  counterpart at all.
- **KXMONSTERPOS-26OCT03** — credited $3.41 across three markets, EST $1.63
  across those same three. `accrued_est` is pruned with `known_tickers`, and
  `known_tickers &= (managed | positions)` (incentive_mm.py ~8096) drops a
  market the cycle it stops being quoted AND is flat. So accrual is DELETED
  and restarts from zero across program periods. `selection_events` shows
  T105 going `-> gone` on 9/07 01:02Z and 9/08 01:21Z and not quoting again
  until 9/11 02:23Z: its $0.44 is a fresh accrual, its $1.05 credit was
  earned in the earlier stretch.

So the accrual CODE never resets at a period boundary (that part was right),
but the known_tickers PRUNE deletes the counter outright, which in practice
resets it whenever a market goes unquoted-and-flat between periods. The
estimator itself is fine: on the one market that was never pruned,
est $6.33 vs credit $6.31.

The rule is therefore: EST and CREDITED are two scopes on one stream,
neither contains the other, and they must never be summed. EST > CREDITED is
the normal case (the current period has not paid). EST < CREDITED means
accrual was deleted, or the credited market has left the book. NET stays on
the EST basis, so it UNDERSTATES on those rows, and the footer says so.
Pinned by `test_est_and_credited_are_not_nested`.

## 2026-09-11 pm — per-program-period accrual (Jack)

Jack, on the AAA gas monthlies: "AAAGASMINM-26SEP30 earn est of 23.70 seems
wrong ... I see earnings of ~$4". The estimate was not wrong; it was a
different quantity. The live `/incentive_programs` feed shows these markets
re-list WEEKLY — KXAAAGASMINM-26SEP30 ran 2026-09-02 -> 09-09 and again
09-09 -> 09-16 — and `accrued_est` is not reset at a boundary (and these
strikes were quoted continuously, so the known_tickers prune never cleared
it either). EARN EST was therefore the sum across BOTH periods while the
exchange's rewards view shows only the current one, ~2.5 days in.

BOT: `BotState.period_start` (ticker -> current program start iso) and
`period_base` (accrued_est when that period opened), rolled by
`_roll_reward_periods(by_market)` off the LIVE programs feed only — same
guard as `finecon_expand_family`, so an empty/failed read cannot look like
every market's period ending. `fetch_programs` now carries `start` in
`by_market` (min across overlapping programs, mirroring the max on `end`:
overlapping programs on one market are ONE paying stretch and must not
read as a new period). Both dicts persist and prune on the accrued_est
rule — a ticker that drops out of one drops out of both, or a re-listed
market would measure this period against a baseline from its last life.

`period_base` is written ONLY on an OBSERVED roll. On first sight of a
market the running period may already be half over, so no honest baseline
exists; the entry stays absent and the email prints "-". This means the
column is blank on every row until each market's next roll — by design,
and the alternative (baselining at first sight) would silently report a
partial period as a whole one.

EMAIL: columns are now PERIOD$ (this program period, the number that
reconciles against the app) and ALL-TIME$ (the old EARN EST, kept because
it is what NET is built on). "-" never means zero: a tier whose rows are
all unmeasurable totals "-" rather than 0.00.

Not asserted in the email, but worth knowing: reward_calibration.json
measures KXAAAGASM at credited $15.94 vs floored estimate $30.10 — factor
0.53 on n=2 settled events, against 1.01 for the GAS/DIESEL family and 1.22
account-wide. The estimator has run hot on the gas monthlies specifically;
n=2 is too thin to rescale anything on.

Pinned by `test_reward_period_roll_rebaselines_accrual` (first sight, the
roll, a start-less program, persist/prune) and
`test_period_column_unmeasurable_is_not_zero`. 548 tests.

## 2026-09-12 — Saturday x1.5 on the long-dated families + weekly tracker (Jack)

Jack asked whether weekends are quieter and whether the bot should quote
bigger on them. Measured on the post-temp window 2026-08-08..09-11 (cycle
logs; the account fills API back to 7/8, IMM fills = ticker in the cycle log
within +-2h, maker only; scored to settlement), by ET day type:

| | Weekday (25) | Saturday (4) | Sunday (4) |
|---|---|---|---|
| fills per resting contract-day | 0.64 | 0.20 | 0.59 |
| rent per resting contract-day | 2.08c | 1.66c | 2.10c |
| loss per filled contract | 2.58c | 2.40c | 4.62c |
| net per resting contract-day | +0.44c | +1.17c | -0.61c |
| net per filled contract | +0.68c | +5.80c | -1.04c |

Rent per resting contract is flat across day types (pools and competition
do not change by day — competitor depth 1.05x, est_frac 0.98x on weekends);
the whole difference is the cost side. The Saturday effect lives in the
long-dated families (mention / econ+company / earnings / Carbon Arc:
turnover 0.51 -> 0.08, 13.5c rent per fill) while the dailies fill at the
same rate every day (gas/diesel 1.3 vs 1.0, rain 0.3 vs 0.4). Sunday is the
worst day of the week (Sunday rain -11.8c/fill on 8/16 and 8/23, the 8/30
21h ET 4,600-contract KXTRUMPMENTION burst). Weekday-only pools explain the
smaller weekend book: KXWORLDNEWSMENTION (~$500/day est), KXTRUMPMENTIONB,
earnings mention, econ prints. Budget/event caps bind on neither day.

Jack: "Saturday multiplier of 1.5x, only on long-dated families. and lets
remeasure each weekend to understand performance."

- `saturday_size_mult()` (IMM_SAT_SIZE_MULT, code default 1.0 = off;
  launcher sets 1.5) applies on the ET Saturday calendar day to every series
  except the prefixes in IMM_SAT_MULT_EXCLUDE (default
  KXAAAGAS,KXDIESEL,KXRAIN,KXTEMP). It sits INSIDE `hour_size_mult()`
  (= `_hour_window_mult() x saturday_size_mult()`), so the ladder, placement
  caps, collateral estimate, TOTAL_SIZE_MULT_CAP via capped_ref_mult and the
  cycle log's `hour_mult` column all see one number. Composes with the
  3-7am x2 (Sat 3-7am = x3 on the global 20 -> 60/side; the x5 total cap
  still trims the at-ref depth mult). Env knob => task-level restart
  (`restart_imm.ps1 -Task`).
- `imm_saturday_tracker.py`: every ET day since 9/12, long-dated vs
  excluded: resting, modelled rent, own fills (fills_*.jsonl), turnover,
  rent per filled contract, 24h mark-out, settled share, settlement loss per
  fill, net per fill, net per resting contract-day, and the mean cycle-log
  hour_mult as proof the multiplier was live; pooled by day type against the
  8/8-9/11 baseline table embedded in the script. Cycle-log parsing cached
  under run-logs/incentive-mm/sat_tracker_cache/. Scheduled "KL imm
  saturday-tracker" WEEKLY Monday 07:40 ET (same principal/settings as the
  opportunistic task), `--print` for a local look.
- Pinned by `TestSaturdaySizeMult` (ET calendar-day edges at 04:00Z, prefix
  exclusion incl. the env override, half-up rounding, composition with the
  global and per-series hour windows, the total cap).

Four Saturdays of evidence at deploy (Saturday day-to-day sd 3.3c per
contract-day), so this is a hypothesis under test: the tracker's long-dated
Saturday `rent/fill` and `net/ct-day` against the baseline row (13.5c /
0.62c) are the read, once `settled%` is high; `mo24` is the early read.

## 2026-09-12 pm — Quiet hours 0-9 ET, long-dated only; daily families become a structural class (Jack)

Jack asked whether the 3-7am ET x2 still makes sense. Re-measured on the
post-temp window (8/8-9/11 weekdays, fills scored to settlement, per ET
hour block; the doubled 3-7 book is IN the data):

| long-dated, weekday | fills / resting ct-hour | loss per fill | net per fill | net per ct-hour | net $/day, block |
|---|---|---|---|---|---|
| 0-2 | 0.010 | -1.4c | +7.1c | +0.074c | +23 |
| 3-7 (x2 live) | 0.005 | +0.8c | +11.1c | +0.054c | +47 |
| 8-9 | 0.009 | -0.8c | +5.8c | +0.054c | +13 |
| 10-13 | 0.036 | +3.0c | -1.0c | -0.036c | -16 |
| 14-16 | 0.047 | +0.1c | +1.6c | +0.073c | +28 |
| 17-19 | 0.032 | +2.8c | -0.1c | -0.003c | -1 |
| 20-23 | 0.020 | +4.8c | -1.6c | -0.032c | -15 |

The doubling did not dilute rent per resting contract (0.057c/ct-hour in
3-7 vs 0.060 in the undoubled 0-2), and the turnover cliff is the US open,
not 8am. The 8am jump in the overall numbers was the gas dailies (0.093
fills per contract-hour, 3.2c lost per fill at the AAA print) and rain
(11.8c). Other pockets, for later: KXTRUMPMENTION 10-13 / 17-23 (-$56/day
modelled, ~-$24 after its 2x credit realization), gas/diesel dailies 14-19
(7-10c lost per fill, -$24/day, the 4pm halving is not enough), KXUST*
17-23 (8-9c per fill, Asian session), Saturday 20-23 long-dated (-$8/
Saturday), Sunday dailies 8-9 and 17-19 (16-17c per fill on 4 Sundays).

Jack: "extend to midnight to 10am ET for longdated. ensure future daily
families are excluded (in addition to current daily families)."

- Launcher `IMM_HOUR_SIZE_MULT=0-9:2.0` (was 3-7). Expected +$30/weekday
  modelled from doubling 0-2 and 8-9 on the long-dated book.
- Daily families take NO global window and no Saturday multiplier; their
  own per-series windows (16-1 / 19-1 halvings) still apply.
  `is_daily_series()` = prefix floor (`IMM_DAILY_PREFIXES`, default
  KXAAAGASD,KXDIESELD,KXRAIN,KXTEMP — the TRUE dailies; the gas/diesel
  weeklies/monthlies/annuals are long-dated by structure and benign
  overnight: 0.024 fills per ct-hour, 0.6c per fill) + a STRUCTURAL class
  refreshed from the live program feed every universe refresh
  (`classify_daily_series` / `refresh_daily_series`): dated tickers with
  >= 2 event dates live at once, p75 program window <= 48h, and p75 EVENT
  HORIZON (program start -> end of the ticker's event day) <= 72h. The
  horizon is what keeps weekly markets funded in 24h program chunks
  (KXSUEZWEEKLY, KXBABELMANDEBWEEKLY: horizon ~228h) long-dated; the p75
  keeps KXTRUMPMENTION (same-day speech events + 22-day programs) long-
  dated; the dates test keeps earnings mention (one event per company)
  long-dated. Hysteresis on the program window only: stays daily until
  p75 program > 72h; the horizon is re-checked every refresh AND on load
  (a launcher relaunch at 17:06Z ran the half-patched, horizon-less rule
  for six minutes and seeded the five shipping weeklies as daily; the
  purge-on-load dropped them). Forgotten after 14 days out of the feed.
  Persisted to `daily_series.json`, loaded at startup (main) and by the
  tracker. Validated on the full feed history:
  daily = temp hourlies, gas state dailies, KXDIESELD, KXRAIN, KXTRUEV,
  KXUST*AD, KXSOFRD, KXEURUSD/KXUSDJPY, KXTXERCOTPEAKD, 15-minute metals,
  KXWORLDNEWSMENTION, KXTRUMPMENTIONB, KXMAMDANIMENTION; long-dated =
  KXTRUMPMENTION, earnings, *CC, KPI months, shipping weeklies, gas/diesel
  W/M/Y. `IMM_DAILY_EXEMPT` prefixes are never classified daily.
- Saturday x1.5 keeps skipping the whole gas/diesel family through
  `IMM_SAT_MULT_EXCLUDE` (default KXAAAGAS,KXDIESEL) as decided that
  morning; rain/temp via the daily floor.
- `imm_saturday_tracker.py` groups by `imm.is_daily_series` (same
  classification the bot sizes with) and reads the mult check outside 0-9.
- Pinned by `TestDailySeries` (floor/exempt, feed classification incl.
  the shipping-weekly and undated-KPI shapes, hysteresis + persistence,
  no global window for dailies but own windows kept, Saturday skip) and the
  reworked `test_global_window_survives_outside_the_per_series_hours`
  (KXTRUEV keeps the window outside its 5pm rule; dailies do not).

## 2026-09-13 — "New incentive programs" daily email (Jack)

Jack: "new daily morning email, table of all the new incentive rewards events
that started in the past day." New standalone `send_imm_new_programs.py` —
the SUPPLY-side email, the one thing the morning lineup had no report for.
The 7:10 digest is the bot's own book, the 7:20 quote-gaps email ranks
markets that already pay and are not quoted, and the 6:45 sweeper only pings
a new family once it clears `IMM_AUDIT_NEW_FAMILY_MIN_DPD` ($300/day). Every
expensive surprise here has been a supply event seen late: 11 KXAVGT
stations lit at ~$2,600/day on 2026-08-20 with nothing surfacing them, and
KXTEMPMIAH's 2026-08-15 relaunch that left the bot dark on its top reward
family for 16 days.

One table, one row per EVENT whose liquidity programs started in the window:
`EVENT | WHAT IT IS | MKTS | $/DAY | POOL$ | PROGRAM WINDOW (ET) | TGT | BOT`,
sorted by $/day, TOTAL row across EVERY new event (row list is capped at
`IMM_NEWPROG_MAX_ROWS`, the total is not). Headline leads with the pool
actually on the table and the rate it pays at — those diverge hard on short
programs (four $20 hourly markets = $80 of pool at a $1,920/day rate) — then
bot-eligible / already-quoting / unearnable splits, then NEW SERIES and
RELIT SERIES call-outs (a series back after >= `IMM_NEWPROG_RELIT_DAYS`
dark: the KXTEMPMIAH class). Footer carries the whole feed's program count,
event count and $/day with the delta vs the last email — the supply time
series, in every email, for free.

**How "new" is decided** — two conditions, both required: the event is not
in the script's own seen-file (`run-logs\incentive-mm\imm_new_programs_seen.json`)
AND the earliest program start across its markets is at or after the window
start. Condition 1 alone floods the first run with the live feed; condition 2
alone re-reports a long-running event the morning a fresh program period
opens on it (the ROLL case). The window start is the last SUCCESSFUL send
(the seen-file watermark), capped at `IMM_NEWPROG_MAX_LOOKBACK_HOURS` (168h),
so a failed send or a skipped day is picked up by the next email instead of
falling in a hole; with no watermark it is `IMM_NEWPROG_LOOKBACK_HOURS` (24h).
An unknown event whose programs started BEFORE the window is a LATE ARRIVAL
(second table, listed once the seen-file is seeded), not a new event.

- **BOT column** from the bot's own machinery: `_allowed`/`_blocked` with
  the extra-allow + finecon extension files hot-loaded, `selected_tickers`
  from `imm_state.json` for "quoting k/n", and a red cutoff flag
  (`UNEARNABLE` / `cutoff passed`) computed with the same guards as
  `imm_feed_audit.run_audit` (close-anchored overrides and the mention
  family are carved out — keep in sync). Config parity comes from importing
  `imm_quote_gaps` FIRST, which mirrors the launcher's `$ProbeEnv` before
  `incentive_mm`'s config is read (it logs "[GAPS] mirrored N launcher env
  vars"; a 0 there means the allowlist columns ran on defaults).
- **Coverage boundary**: the feed read is `status=active`, so a program that
  both starts AND ends between two runs is invisible. Hourly families are not
  in that hole (a current-hour program is always active). `--include-ended`
  additionally pages `IMM_NEWPROG_ENDED_STATUSES` (settled, closed) for the
  same window — off by default, because the size and ordering of the
  historical feed (~76k rows) is not something a 7:30 AM email should
  discover for the first time.
- **Feed-health guard**: a feed under `IMM_NEWPROG_FEED_SHRINK` (25%) of the
  last run's program count is treated as an API problem, not a supply event —
  red banner, and the watermark is NOT advanced, so anything hidden today is
  reported tomorrow. The seen-file is written only after a successful send
  (a `--test` send counts; `--dry` never writes), for the same reason.
- STRICTLY READ-ONLY: GETs only (feed pages + up to `IMM_NEWPROG_TITLES`=60
  event-title reads, biggest pools first), no cycle, no orders, and the only
  file written is its own seen-file.
- Task **`KL imm new-programs`**, daily **5 minutes after the opportunistic
  email** (whatever clock that task is registered in — see the registration
  note below; 7:30 AM ET when no sibling task exists to copy), cmd.exe
  wrapper →
  `run-logs\incentive-mm\new-programs-task.log`. Idempotent marker
  (`imm_new_programs_sent_<date>.marker`), Modern-Standby retries (8×5min),
  registry cred fallback, Alerter tag `IMM-NEWPROG`.
- Flags: `--test` (send now, no marker) / `--dry` / `--print` (build + print,
  write nothing) / `--since HOURS` (widen the window by hand) /
  `--include-ended` / `--html-out FILE` (with `--dry`).
- Tests: `python -m unittest test_send_imm_new_programs` (46) — centi-cent
  and $/day math incl. the hourly floor, the ROLL case, late arrivals, the
  window/watermark rules, the feed-shrink hold, the BOT column and cutoff
  flags, row-cap totals, ASCII body, and main()'s marker/state writes.

**Registration is automatic once this lands on main.** `sync_kl_main.ps1`
(the every-30-minutes "KL sync-kl-main" task) fast-forwards the repo and then
bootstraps the task if it is missing, exactly as it does for
"KL dashboards-daily": it runs `register_imm_new_programs.ps1` and logs
`imm new-programs bootstrap: registered` (or FAILED, with the output) to
`run-logs\sync-kl-main.log`. No-op once the task exists. The bootstrap
deliberately does NOT `Start-ScheduledTask` — the first run is a seeding run
and kicking it would fire an email at an arbitrary minute. To do it by hand:
`powershell -ExecutionPolicy Bypass -File register_imm_new_programs.ps1`.

`register_imm_new_programs.ps1` derives both the schedule and the command
line from the box instead of hardcoding them, because the repo is ambiguous
about which clock the task triggers are in: `register_portfolio_digest.ps1`
documents 6:00 AM **local (Central)** = 7:00 AM ET "matching the crypto
DIGEST / imm quote-gaps conventions", while the quote-gaps and opportunistic
notes above describe 7:20/7:25 as ET. So the script reads the
"KL imm opportunistic" task's own trigger and adds 5 minutes (quote-gaps
+10 as second choice), which puts this email right after the lineup under
either convention, and prints the resolved time in BOTH local and ET so the
answer is visible the first time it runs. With no sibling task registered it
falls back to 7:30 AM ET converted into the machine's local time. The command
line is the sibling's own, with the script and log names swapped, so a Python
upgrade cannot leave this task pointing at a `Python312` path that no longer
exists; the documented literal paths are the fallback, used only when both
swaps cannot be verified.

**First run is a seeding run**: with no seen-file, every event already in the
feed is recorded as known and only events that started inside the default 24h
window can be reported — so the first email is small by construction and the
footer says so. Run `python send_imm_new_programs.py --dry` once before
registering the task to see what it would say (the box's Kalshi key and
`ALERT_EMAIL_*` are required — this runs nowhere but the trading box).

## 2026-09-22 — Carbon Arc *ADS / *POS families into the NORMAL book at 3/event; family membership source-verified (Jack)

Jack: "ad spend markets by Carbon Arc e.g. KXAMUSEMENTADS, KXCASINOADS,
KXELECTRONICSADS should be auto-quoted as part of IMM right? why arent these
picked up? set limit of max 3 per event" -- and the same for the POS family
(KXC4POS, KXBUDLIGHTPOS, KXCOORSLIGHTPOS).

WHY THEY WERE NOT PICKED UP. Both families WERE allowed -- as FINECON
members: the 11 *ADS series sat in _DEFAULT_FINECON_SERIES since 9/05 and 17
*POS series had been self-extended into finecon_extra_series.json by the
overrides task (KXDRPEPPERPOS in the base list). The finecon tier is a
25-slot group walk (+10 openings/day, ceiling 35) shared by ~60 series, so
they mostly lost the ROI walk. Measured 13:05Z, one refresh: 189
`finecon_top_n` rejections; *ADS 143 markets / 11 events with 16 selected in
6 events; *POS 162 markets / 18 events with 22 selected in 12 events; 11 of
the 29 events had NO market quoted at all; $4.4k/day of pool.

CHANGE 1 -- ALLOW_FAMILY_SUFFIXES "CC" -> "CC,ADS,POS" (env
IMM_ALLOW_FAMILY_SUFFIXES). FAMILY_OVERRIDE_PARENTS gains ("family_suffix",
"ADS", "KXAMUSEMENTADS") and ("family_suffix", "POS", "KXDRPEPPERPOS"), both
archetypes given an explicit SERIES_OVERRIDES entry (safe-join, no rate bar
-- the identical guard set they carried as finecon members). The 11 *ADS +
KXDRPEPPERPOS leave _DEFAULT_FINECON_SERIES; load_finecon_extra_series now
SKIPS any family-suffix name in the extra file (logged once), so the 17 *POS
entries there are inert and the task's self-extend cannot re-absorb a
sibling. The overrides task also skips a suffix match the bot has not
judged yet (its refresh does that within the hour).

CHANGE 2 -- EVENT_TOP_N default gains `*ADS:3,*POS:3` (the *CC:3 machinery:
ROI rank, two-sided first, sticky + lifetime slots, IMM_EVENT_TOP_N).

CHANGE 3 -- MEMBERSHIP IS SOURCE-VERIFIED. The 9/10 note said "the suffix IS
the family, no false positive in the feed" -- true of the feed that night,
not of the catalog. Sweeping all 14,248 series found 35 non-Carbon-Arc
series ending in CC/ADS/POS: KXAMAZONADS (an FTC-lawsuit binary,
PACER-settled, which carried a paying program 9/07-9/13), KXSBADS / KXWCADS
/ KXNFLREDZONEADS / KXDRUGADS / KXKHCGRADS, the live-index "positive" family
KXINXPOS / KXNASDAQ100POS / KXDJIAPOS / KXNIKKEIPOS ... (11 series, Trading
View-settled, DAY-DATED NUMERIC tickers such as KXINXPOS-26DEC31H1900-
T6845.5 -- no ticker-shape rule separates them from a Carbon Arc print),
five KXNFL*POS draft-position series, and for the *CC rule already live:
FCC / KXFCC, KXGRAMBCC / KXGRAMBCCC, KXAUWPCC, KXANIMEMPAACC, six KXNCAA*CC.
So a suffix match is now only the CANDIDATE test. `_resolve_family_verdicts`
reads GET /series/<s> once per novel suffix-matching series in the live
feed (IMM_FAMILY_MAX_SERIES_FETCHES=80 per refresh, never-read first,
re-read after IMM_FAMILY_VERDICT_TTL_D=7) and the verdict is "a settlement
source names Carbon Arc" (series_is_carbon_arc -- now also what
imm_earnings_overrides.carbon_arc_series calls, one test for both).
Verdicts persist in run-logs/incentive-mm/family_series_verdicts.json,
loaded at import (so imm_quote_gaps / imm_feed_audit / send_imm_new_programs
/ the overrides task classify the feed with the bot's own membership) and by
mtime each refresh. No verdict = NOT allowed (fail closed), and
scan_universe_reason reports `family_pending` rather than letting the scan
screen it; a failed or empty read keeps the previous verdict (a 429 must
never un-admit a quoting family). Kill switch: IMM_FAMILY_SOURCE_CHECK=0 =
the bare suffix rule. Deleting the file = every family series re-reads on
the next refresh (and is out until it does).

Probe under the launcher env against the live feed (13:51Z): 59 suffix
matches in the feed, all 59 verified Carbon Arc (CC 30 series / 270 mkts /
$2,058/day; ADS 11 / 143 / $2,043; POS 18 / 162 / $2,315), none unresolved,
every member inherits its archetype guard. The verdict file that probe wrote
was copied into run-logs before the restart, so the first refresh needed no
reads.

Tests: test_cc_family_suffix_allows_into_normal_book rewritten for three
suffixes + the verdict gate + the kill switch; new TestFamilySourceVerdicts
(detector shared with the task, one read per series + persist + TTL
re-read, failed read keeps a verdict / unread stays out, budget order,
kill switch, judged non-member); finecon loader skip; the finecon
membership test no longer names ADS/POS. setUpModule redirects
FAMILY_VERDICT_FILE and clears the import-time load.

NOT changed: IMM_MAX_MARKETS=150 (launcher env). It was already BINDING
before this change (150/150 events; KXAAAGASD-26SEP23 and KXDIESELD-26SEP23
were `not_ranked` at 13:05Z), so the family events with no quoting market
compete for seats with everything else by yield rank as seats free up.

POST-RESTART MEASUREMENT (first refresh of the new process, 13:58:34Z):
1733 candidates -> 846 selected across 150/150 events (808 before); skips
{'event_top_n': 251, 'not_ranked': 42, ...} and `finecon_top_n` is GONE from
the skip list (189 the refresh before). Per family, max 3 per event
everywhere: *ADS 18 selected in 6 events (16 in 6 before), *POS 26 in 9
events (22 in 12 before -- KXBUDLIGHTPOS / KXCELSIUSPOS / KXVELOPOS each
lost their single sticky finecon member to the hopeless screen on the
same refresh), *CC 89 in all 30 events. The 42 `not_ranked` are ALL family
markets: 15 *ADS (5 events: KXSPORTGOODSADS, KXSPORTSBOOKADS,
KXSTREAMINGADS, KXTEENCLOTHADS, KXVIDEOGAMESADS) and 27 *POS (9 events) --
the IMM_MAX_MARKETS=150 event cap, full with sticky events, is now the ONLY
thing between those 14 events and the book. They enter as seats free
(events settling), by yield rank against every other newcomer, or when the
launcher cap is raised (the 9/10 precedent: 100 -> 150 for the *CC wave via
$ProbeEnv + `restart_imm.ps1 -Task`). The verdict file was not rewritten
(every verdict fresh), and no family read was needed.

EVENT CAP 150 -> 200 (Jack 2026-09-22 pm: "increase to IMM_MAX_MARKETS to
200"; launcher $ProbeEnv, commit 8707d5e, applied 00:08Z 9/23 with
`restart_imm.ps1 -Task` -- hourly temp was dark, so no window wait). First
refresh under the new cap (00:10Z): 833 selected across 164/200 events (795
across 150/150 the refresh before), `not_ranked` 0 (was 51). Every family
event now holds a seat at most 3 strikes deep: *ADS 31 markets in all 11
events, *POS 50 in all 18, *CC 88 in all 30. No family read was needed and
no family/finecon error line since the restart.

## 2026-09-22 pm — The $1 floor credit is THIS period's accrual, not lifetime (Jack: KXRT-STRA-50 / -45 "should be hopeless")

Jack, 8pm ET: "why is this quoted? it should be hopeless KXRT-STRA-50,
KXRT-STRA-45" (Street Fighter Rotten Tomatoes score, resolves Oct 19).

WHAT HAPPENED. Kalshi re-listed the KXRT programs as a fresh ONE-DAY period
(start 2026-09-22 16:49:35Z, end 2026-09-23 16:49:35Z, $100/day) right after
the paid 9/10-9/22 period ended at 16:46Z. In the new period the two markets
had accrued $0.09 / $0.06 with 0.68 days left and $0.15-0.22/day of
estimated share (20 lots at the touch of a 29k/21k-deep book, est_frac
0.0015) -- projection about $0.25, hopeless by the 7/25 rule, and the bot was
short 65 / 40 contracts there. But the shared floor projection
(refresh_universe, "reaches_min") credited `accrued_est`, the LIFETIME
counter ($5.01 / $6.83, nearly all of it banked and PAID in the period that
had just ended), so reaches_min was trivially true, hopeless_since never
started, and the bot kept re-placing the same quotes every refresh. The
per-period baseline (`period_base`, 2026-09-11) existed for the digest but
the floor logic never subtracted it. The exchange pays credits per market
per PROGRAM PERIOD behind a hard $1 floor, so the old reading was wrong for
every re-listed market.

CHANGE (FLOOR_ACCRUAL_PER_PERIOD, env IMM_FLOOR_ACCRUAL_PER_PERIOD, default
on): `IncentiveMarketMaker.period_accrued(t)` = accrued_est - period_base
(no baseline = first period seen = lifetime, unchanged) is now the credit in
the shared projection (entry floor AND hopeless exit). The digest's
paid-basis crossing had the same defect (a market that crossed $1 in a paid
period counted every cent of a fresh sub-floor period as paid): it is now
`_paid_basis_delta`, measured from the period baseline; a roll discards the
market's `paid_crossed` flag, and each refresh heals a flag left from an
earlier period on a market whose current period is under the floor (the
state this change inherited). =0 restores the lifetime credit everywhere.

CLASS SWEEP at 00:10Z 9/23 (833 quoting members): 89 cleared the bar on
lifetime accrual only -- 74 KXRT, 11 KXFSLR, 1 each KXMLBSEASONGAMES /
KXHOODA / KXBA / KXAAAGASMINM -- every one under $0.30 for its period. After
the restart they start the hopeless clock and exit after the 1h sustain
(reduce-only wind-down for any inventory, the normal hopeless path); on the
NEXT one-day re-listing they never enter (entry floor on this period's
numbers -> payout_floor). Tests: test_floor_credit_is_this_periods_accrual,
test_floor_credit_keeps_a_member_that_banked_this_period,
test_paid_basis_crosses_the_floor_per_period.

MEASURED after the restart (new process 00:36:16Z, first refresh 00:37:34Z):
"paid-basis: 236 market(s) uncrossed" on the first pass (734 -> 498 flags);
119 members started the hopeless clock on the first refresh -- 79 KXRT
(KXRT-STRA-50 and -45 among them), 10 KXVENUEPERFORM, 9 KXFSLR, 3
KXMLBSEASONGAMES, 3 KXAAL, the rest singletons -- against the probe's 107;
fresh-candidate `payout_floor` rejections 303 -> 313. Evictions follow at
the first refresh after 01:37Z (HOPELESS_SUSTAIN_SECS = 3600).

## 2026-09-22 pm — Scan bulk read 2000 -> 5000: the whole universe every refresh (Jack)

Jack, after the KXCPICORE-26DEC-T0.2 vs KXCPI-26DEC-T0.3 question ("why
not read all the markets? what's the loss?"): "set default to 5000".
Measured 00:37Z 9/23: pre-read list 3,018 markets; 1,159 tied at $14.29/day
across ranks 1332-2490, so which ties fell outside the 2,000 cap was feed
order (KXCPI-26DEC-T0.3 rank 2454 quoting as a sticky member, KXCPICORE-
26DEC-T0.2 rank 2176 judged hourly by the tail sweep and losing every
borderline call); 630 `bulk_cap` rejects per refresh, 127 tail-swept. Cost
of reading everything: 13 more chunked market reads (~3s of a 2-3 minute
refresh); the series/candle/book budgets (60/80/120 per refresh) are the
real throttles and are unchanged. SCAN_MAX_BULK default 2000 -> 5000
(IMM_SCAN_MAX_BULK); the hourly tail sweep is a no-op until the feed
outgrows 5,000. Expect `bulk_cap` and `tail_swept` to vanish from the
open-scan funnel line and `book_cap` / history_pending to absorb the
newly visible candidates through their rotations.

EVICTION MEASURED (first refresh past the 1h sustain, 01:43:16Z 9/23): 93
members left as `hopeless` in one refresh -- 77 KXRT (KXRT-STRA-50 and
-45 among them: selection_events `selected -> hopeless`, est $0.14 /
$0.16 per day), 10 KXVENUEPERFORM, 3 KXMLBSEASONGAMES, KXHOODA, KXBA,
KXCART (the last also permanently barred by the scan tier). Selected 839
-> 746 across 156/200 events; their quotes were cancelled and the
positions ride to settlement (managed_extra 0 -- the 9/07 "dropped
markets carry no orders" rule), e.g. KXRT-STRA-50 short 65, -45 short 40.
Of the 119 that started the clock, the rest either recovered above the
bar on a later reading (the clock resets) or are exempt by design
(KXFSLR-26OCTMWSOLD is an IMM_FORCE_EVENTS event, KXAAAGASMINM is finecon).

## 2026-09-23 — Opportunistic email: the CARBON ARC group is by settlement source, not suffix (Jack)

Jack: "in daily Opportunistic IMM email, why doesnt carbon arc group include
all carbon arc events including foottraffic markets like KXBROSFT-26OCT08 and
KXCAVAFT-26OCT08. and app download markets like KXDKNGAPP-26OCT08 and
KXNFLXAPP-26OCT08?"

WHY. The email's third tier was the NAME-PATTERN family (is_family: a suffix
in imm.ALLOW_FAMILY_SUFFIXES = CC/ADS/POS) -- a "how it was admitted"
bucket. The foot-traffic (*FT) and app-download (*APP) series are Carbon
Arc-settled too (GET /series: settlement source "Carbon Arc" for KXBROSFT,
KXCAVAFT, KXDKNGAPP, KXNFLXAPP, KXBKFT, KXCLAUDEAPP ...) but they enter the
normal book through the exact company allowlist (_DEFAULT_COMPANY_SERIES +
the daily *FT/*APP auto-enroll into extra_allow_series.json), so tier_of
returned None (normal book, not opportunistic) and the email never listed
them. Quoting is unchanged by this note.

CHANGE (send_opportunistic_imm.py, reporting only): tier "family" =
is_carbon_arc(series) OR the old suffix rule. is_carbon_arc reads
CARBON_ARC_SERIES, a module dict seeded from the bot's own verdicts
(imm.FAMILY_VERDICTS, the suffix families) and filled by
resolve_carbon_arc(client, series, now): one GET /series per novel series in
the book through imm.series_is_carbon_arc (the bot's detector), persisted in
run-logs/incentive-mm/carbon_arc_series.json with a 30-day TTL, at most 250
reads per run, a failed read = unknown this run (normal book) and retried
next run. build_report resolves the active book's series before tier
assignment and the cumulative universe's series before the cumulative
table. Headers: "CARBON ARC" (was "CARBON ARC *CC/ADS/POS"); the slot line
says CC/ADS/POS are capped at 3/event by ROI and FT/APP are uncapped.
family_label gains "Foot traffic (Carbon Arc)" / "App downloads (Carbon
Arc)", source-checked so KXNFLDRAFT (a *FT) never gets the label.

DRY RENDER 12:55Z 9/23 (patched script, live state): CARBON ARC 77 events /
416 markets (was the 59 suffix-family events); 161 series read once and
cached. Every FT/APP row shows as NEW in the first email after this change
(they were never in a previous email's record); the cumulative table now
also carries their ledger credits and realized P&L. Test:
test_carbon_arc_tier_is_by_settlement_source (tier by source, event ticker
resolves the same, precedence finecon > scan > carbon arc, TTL, cache
round-trip, failed read tolerated, labels).

## 2026-09-24 — Carbon Arc late-month rule: no bids, half-size asks inside 14 days of month-end (Jack)

Jack, after the adverse-selection assessment (markouts -4.1c/ct at 1h, the
worst family in the book, widening to -6.75c at 72h; buying YES the toxic
side at -7.5c/24h vs -3.1c selling; the August cycle's bracketing strikes
moving 20 -> 99 inside the last day before close): "when Carbon Arc events
are 2 weeks from month-end, halve size and skew away from the toxic side",
then minutes later "completely block the toxic side when 2 weeks from
month-end".

RULE (ca_late_month_mults): for a market whose series is Carbon Arc-settled
(carbon_arc_settled = the bot's own source verdict) and day-dated, from
CA_LATE_DAYS (14) before 00:00 ET on the 1st of the ticker-date month (= the
end of the measurement month; the print day is early the NEXT month) until
the market closes: bid rungs x CA_LATE_TOXIC_MULT (0 = none), ask rungs x
CA_LATE_SIZE_MULT (0.5). Applied per side in the quote loop (lv_bid /
lv_ask), in the estimator's hypothetical ladder (so est reward and the
$1.50 floor projection see the real size) and in the collateral
reservation. Position / event caps and the inventory-skew knees are
untouched. The 1c depth pad on the bid side stays (it qualifies the reward
snapshot at ~1c/ct of worst case; it is not liquidity at the touch). Logged
once per event: "Carbon Arc late-month: <event> bid side BLOCKED / asks
x0.5". Knobs: IMM_CA_LATE_DAYS, IMM_CA_LATE_SIZE_MULT, IMM_CA_LATE_TOXIC_SIDE
(bid|ask), IMM_CA_LATE_TOXIC_MULT; SIZE_MULT=1 with TOXIC_MULT=1 = off.

VERDICT COVERAGE: the bot now source-verifies the exact-list Carbon Arc
families too (FAMILY_VERDICT_EXTRA_SUFFIXES = FT,APP), so carbon_arc_settled
answers for foot traffic and app downloads; admission is unchanged
(family_series_allowed still requires a suffix in ALLOW_FAMILY_SUFFIXES).
The opportunistic email's own lookup seeds from these verdicts first.

CONSEQUENCES TO EXPECT: est reward on every Carbon Arc market drops to
roughly a quarter to a third inside the window, so some markets (the
shortest remaining periods -- POS closes Oct 3) may fall under the $1.50
projection and exit `hopeless` after the 1h sustain, or never enter; every
Carbon Arc market reads quotable_sides = 1 in the window (one-sided
candidates rank below two-sided ones in the per-event cut, lifetime slots
already held are unaffected). Window for the current cycle: Sep 17 00:00 ET
onward, i.e. the rule engaged on deploy for all 77 events.

Tests: test_late_month_rule_halves_and_skews_carbon_arc_markets (window
boundary at Sep 17 00:00 ET, holds past month-end, exact-list families,
unknown/undated/non-Carbon-Arc untouched, ask-side variant, kill switch,
scale_levels incl. the empty blocked ladder),
test_late_month_rule_shapes_the_resting_ladder (cycle-log want columns:
no bids, asks = half the plain ladder),
test_verdict_reads_cover_the_exact_list_carbon_arc_families.

MEASURED after the restart (new process 01:18:45Z 9/25, first refresh
01:20:12Z): 86 events logged "bid side BLOCKED / asks x0.5"; on the next
cycle all 394 managed Carbon Arc markets show want_bid_ct 0 and want_ask_ct
10-30 (the halved 10-lot base times the deep-reference multiplier), 81
already resting ask-only, zero bids, zero bid pads. Members all retained
(CC 80/30 events, ADS 31/11, POS 53/18, FT 104/10, APP 153/18), estimated
reward on them 222 -> 70 $/day (CC 29.6 -> 11.6, ADS 15.2 -> 5.4, POS 41.4
-> 13.3, FT 58.9 -> 13.3, APP 77.3 -> 26.4); fresh-candidate payout_floor
rejections in the families rose (CC 1 -> 82, ADS 29 -> 72, POS 13 -> 44);
5 members (3 POS, 2 CC) started the hopeless clock. Universe 728 selected
across 171/200 events, ladder collateral ~$12.5k -> ~$7.1k, no errors.

## 2026-09-24 pm — Carbon Arc: stop quoting entirely in the last 5 days of the month (Jack)

Jack, after the cost/benefit of the half-size rule ("so you're saying it's
a negative trade overall?" -- reward accrues linearly, the informed flow
and the price moves are back-loaded; the families' P&L to date was -$1,180
MTM on 6,190 long-YES / 4,890 short-YES contracts against ~$1,110 of
estimated reward): "2. stop quoting entirely in the last 5 days OF THE
MONTH".

RULE (CA_LATE_STOP_DAYS = 5, env IMM_CA_LATE_STOP_DAYS, 0 = off): for a
Carbon Arc-settled, day-dated market, apply_series_cutoff_adjustments caps
the cutoff at ca_stop_utc = 00:00 ET on the 26th of the measurement month
(month_end - 5d; Sep 26 04:00Z for every 26OCT03/06/07/08 print). It runs
in the shared tightener, so BOTH cutoff producers (refresh_universe and the
orphan restore) agree; members leave through the ordinary cutoff death
(quotes cancelled, positions ride to settlement) and nothing enters. A
cutoff is terminal, so the bot also stays out of Oct 1-8 -- the post-month-
end days before the print, when the panel is complete. The 14-day rule
(bids blocked, asks x0.5 from the 17th) covers the 17th-25th. Keyed on the
source verdict, not the name: the two Taylor Swift *FT chart series keep
their ordinary cutoff. The per-event late-month log line now ends
"quoting stops Sep 26 00:00 ET (last 5 days of the month)".

SIDE EFFECT ON DEPLOY (9/25 01:xxZ, ~26h before the stop): _quotable_days
on every Carbon Arc member drops to ~1.1 days, so the $1.50 projection
(this period's accrual + est x 1.1d) puts thinly-accrued members on the
hopeless clock an hour before the stop would have taken them anyway, and
fresh candidates reject as payout_floor. Both are the intended direction.

Tests: test_late_month_stop_is_the_last_five_days_of_the_month (26th
00:00 ET, no-prior-cutoff, never loosens, non-Carbon-Arc / unknown /
undated untouched, kill switch); 605 green.

## 2026-09-24 pm — KXRT: stand down every Rotten Tomatoes event 7 days before close (Jack)

Jack: "seems im getting picked off on Rotten Tomatoes markets in the past
few days, why?" -> "is the release day trackable for each movie?" ->
"stand down KXRT events 7 days before close".

WHY (measured from fills_*.jsonl vs the 5-min marks, 9/16-9/24): 1,137
KXRT fills / 26k contracts / ~$12.4k at risk, all our own resting orders;
mark-out -$589 at 1h / -$769 at 6h (-2.9c per contract, the worst family
in the book by dollars); realized -$346 since 9/11 plus -$483 open MTM
against ~$1,105 of estimated reward (credits landed ~$697 for 9/17-9/22).
By days-to-close at fill time: est reward >14d $737 / 7-14d $192 / 4-7d
$152 / 2-4d $71 / 0-2d $23 versus 6h mark-out -$260 / -$151 / -$244 /
-$48 / -$63 -- the last week earns ~21% of the reward and takes ~46% of
the loss. Every KXRT market closes 10:00 ET on the Monday after a Friday
release (close - 3d = release day, verified on all 42 titles; Kalshi
exposes no release field, occurrence_datetime = close_time). Review
embargoes lift Mon-Thu of release week (Heart of the Beast Tue 9/22 09:03
ET: 74 contracts sold at 51-55c across four strikes, 96c by the weekend;
Forgotten Island Thu 9/24 16:39 ET: a 100-lot bid at 37c, resting 6-9
ticks under the touch at the top-200-contract reference, swept as the
touch went 44 -> 29) and the score keeps drifting as reviews land (HEA
96%/25 reviews on 9/22 -> 89%/94 reviews on 9/24): >=10c five-minute
jumps explain -$145 of the marked loss, small moves -$412. Public trade
history of the 23 settled titles: the event only settles within 10c the
weekend AFTER release, so the stand-down runs through the close. KXRT had
NO SeriesOverride at all (not in IMM_UNDATED_GUARD_SERIES: no safe-join,
no cutoff, no hour rule) and the 0-9 ET x2 doubled rungs in the hours
reviews drop (29% of contracts, 49% of the 6h loss).

RULE: KXRT_CUTOFF_BEFORE_CLOSE_DAYS = 7 (env IMM_KXRT_CUTOFF_BEFORE_CLOSE_DAYS,
0 = off; series list IMM_KXRT_CUTOFF_SERIES, default KXRT).
register_close_cutoff_days() sets SERIES_OVERRIDES["KXRT"] =
SeriesOverride(cutoff_from_close_min=10080), keeping any other override
field (the kill switch clears just this one). It rides the existing
close-anchored machinery: both cutoff producers (refresh_universe and the
orphan restore) and the imm_quote_gaps mirror compute cutoff = close_time
- 7d, _screen returns "cutoff" from then on (members and candidates
alike), quotes are cancelled, positions ride to settlement (the 9/07
dropped-markets-carry-no-orders rule), nothing re-enters. _quotable_days
ends at the cutoff, so the $1.50 floor is judged on the shorter window.
Nothing else about KXRT changes (ladder, 150 cap, band, no safe-join).

DEPLOY (9/25 01:44Z, in-place edit -> source-mtime clean exit 01:44:30Z ->
launcher relaunch): Heart of the Beast, Forgotten Island, Primetime (close
Mon 9/28) are past the stop and cut off on the first refresh; their
inventory (HEA-75/-70 short at 44-51c, FOR-97/-98 long at 25-41c, ...)
rides to Monday's settlement. Next stops: Digger + Verity Mon 9/28 10:00
ET; Social Reckoning + Other Mommy 10/5; You Can See Everything /
Whalefall / Sense and Sensibility / Street Fighter 10/12; Wildwood +
Clayface 10/19; Godzilla Minus Zero + Wild Horse Nine 11/2; I Play Rocky
+ Hunger Games 11/16; Avengers + Dune 12/14.

NOT COVERED: festival reveals (Digger revealed 9/22 for a 10/2 release;
Sense and Sensibility drifted 65c -> 7c from 9/19 for 10/16) land weeks
before release. A direct reveal tracker is feasible -- the
rottentomatoes.com/m/<slug> page renders "Tomatometer NN% based on N
Reviews" plus the release date in plain text (slug ambiguity for generic
titles, scraping fragility) -- or the in-market IMM_EVENT_DEPTH_SERIES
gate (jump confirm / fill tripwire; KXRT tickers are undated so the
date-arm never suppresses it), untested on KXRT.

Tests: test_kxrt_release_week_stand_down (override registered, nothing
else changed, tightener anchors on close and never loosens, screen inside
vs outside the window, quotable-days horizon, kill switch); 606 green.

## 2026-09-24 pm — Sports ladders and escalators allowlisted, cutoff = kickoff (Jack)

Jack: "allowlist sports ladders and escalators, up until the game starts.
e.g. NFLLADDERREC-26SEP24ATLGB, NFLLADDERRECYDS-26SEP24ATLGB. though these
shouldnt be quoted since the game started".

WHAT THEY ARE. Kalshi's per-game player-prop SCALARS (strike_type
"custom"): a Receptions Ladder YES pays $0.05 per catch capped at $1, a
Fantasy Ladder $0.01 per PPR point, an Escalator a convex per-stat
schedule; one market per player, event = one game (26SEP24ATLGB = ET game
date + team codes). Live 2026-09-24: seven NFL series on the Thursday game
-- ladders REC / RECYDS / RSHYDS / FFPTS ($478-956/day per series, programs
from ~2 days out) and escalators REC / RECYDS / RSHYDS (programs 21:55Z-
03:59Z only, game-time pools of ~$790/market/day).

ALLOWLIST BY REGEX (ALLOW_SERIES_PATTERNS, env IMM_ALLOW_SERIES_PATTERNS):
a league prefix (NFL NBA WNBA NHL MLB NCAAF NCAAB CFB CBB MLS) with LADDER
or ESCALATOR anywhere after it, checked in _allowed after the suffix
families -- every stat and every future league's ladders are covered
without a hand add. Guards clone the KXNFLLADDERREC archetype (safe-join,
no rate bar) through a new "pattern" kind in FAMILY_OVERRIDE_PARENTS.

THE CUTOFF IS THE KICKOFF. Kalshi's occurrence_datetime on these markets is
~3h AFTER kickoff (ATL@GB: kickoff 00:15Z, occurrence 03:15Z, expiration
06:15Z, close two days later), so trade_cutoff_utc's occurrence branch
would have quoted three hours into the game. EventStartResolver now
resolves sports_ladder_league(series) -> ESPN_LEAGUE_PATHS -> the league's
ESPN scoreboard (_espn_game_start: the WNBA blob-split team matcher,
generalised; one single-date call per ET day, ticker date + 2 days for
ladders, 14 for WNBA mentions) and refresh_universe cuts off
EVENT_START_BUFFER_MIN (30) before kickoff. schedule_resolved_series()
(the old exact set + every ladder league with an ESPN path) replaces the
SCHEDULE_RESOLVED_SERIES membership test in ticker_cutoff_passed so game-
day markets are never pre-dropped on the string. FAILURE MODE: no ESPN
path / API down / unknown team code -> resolver None -> trade_cutoff_utc's
min() = the ticker-date midnight-ET rule = out the night before the game,
never the occurrence. Tonight's game resolved 00:15Z -> cutoff 23:45Z,
already past at deploy, so the ATL@GB ladders show as `cutoff`, unquoted.

ESPN HOST: site.api.espn.com answers 403 "Access Denied" to the browser UA
since ~2026-09 (measured for nfl / wnba / cfb alike), so ALL ESPN lookups
(WNBA, World Cup, the ladders) now use site.web.api.espn.com, which serves
the same API; the WNBA/World Cup mention resolvers had been silently
falling back to midnight since the block began (no live games in the
window, so no log line).

NOT DONE / TO WATCH: inactives are announced ~90 min before kickoff and are
THE information event for player props (a scratched player's ladder goes
to ~0); the 30-min buffer leaves an hour of that. A larger buffer is one
knob: SERIES_OVERRIDES["KXNFLLADDERREC"].start_buffer_min. Escalator mids
are often under the 5c band (Bijan REC 1.0/9.4c) and will reject as
extreme_mid / wide by themselves. First real quoting window: the Sunday
Sep 27 slate once Kalshi lists it (~2 days out).

Tests: test_sports_ladder_resolves_kickoff_from_espn (kickoff either team
order, no match -> None, no league path -> None, the fallback is the night
before not the occurrence), test_sports_ladders_and_escalators_are_
allowlisted_by_pattern (7 live shapes + an NBA one, non-ladders refused,
archetype clone, schedule predicate, blocklist wins). 608 green.

## 2026-09-24 pm — CPI + company headcount blocklisted; live-feed rule gains the reward-window prong (Jack)

Jack: "blocklist CPI markets and company headcount markets. also dont quote
markets that are easily adversely selected against because there is live
data flowing visibly directly impacting the market that the bot is quoting,
and the market resolution overlaps entirely or almost entirely with the
incentive reward period e.g. KXTOKENUSE-26SEP28, KXXIAOMISHARE-26SEP28".

WHAT THE BOOK LOOKED LIKE (imm_state.json 01:44Z 9/25): 11 CPI strikes
quoting as OPEN-SCAN members (KXCPI-26NOV/26DEC, KXCPIYOY-26NOV/26DEC,
KXCPICOREYOY-26DEC; 15 CPI positions, 490 contracts gross); one headcount
market selected in the NORMAL book (KXAMZN-26OCTEMP-1600000.0, -49) with
31 headcount markets held from earlier programs (599 gross: KXGOOG-26NOVHEAD
15 strikes, KXINTC-26OCTHEAD 10, KXSBUXA/KXAXPA/KXCMGA-27FEBHEAD,
KXAMZNA-28JANHEAD); and EIGHT live-feed scan members -- KXTOKENUSE-26SEP28
x3, KXXIAOMISHARE-26SEP28 x1 (both settle on openrouter.ai/rankings, a
key-less feed that "updates live as traffic flows") plus KXANTHVREQ /
KXANTHVSPEND / KXMOONVSPEND-26SEP26 x5 (Vercel AI Gateway share, the same
shape). All eight had passed the scan's live-source screen: neither
`openrouter` nor `vercel` was a keyword, and their cached ok-verdicts were
up to a week old. Both OpenRouter events run their reward window over the
market's entire life (open 9/21 16:00Z or 9/22 00:00Z -> close 9/28
03:59Z = the program window exactly).

### 1. CPI: SERIES_BLOCK_PATTERNS `.*CPI.*` (+ KXCOREUND, KXUSEDCAR, USEDCAR)

The family is not one prefix (KXECONSTATCPI/CPIYOY/CPICORE/CORECPIYOY carry
60 live program markets and do not start with KXCPI; KXUSGASCPI,
KXSHELTERCPI, KXCHINACPI, KXJPCPIYOY ... likewise). The substring pattern
takes every series whose NAME carries CPI, today's and future. Audited
against the whole 14,379-series catalog before shipping: all 90 tickers
containing "CPI" are CPI/inflation markets -- no KXTEMPHELP-style near
miss. Two CPI prints whose ticker lacks the letters are named explicitly
(KXCOREUND "Will Core CPI fall below x.x%", KXUSEDCAR/USEDCAR "US CPI print
on used cars"). Deliberately NOT taken: KXUSGBEEF (BLS average ground-beef
price, a price tracker by shape), KXTRUFEGGS (Truflation index with "CPI"
only in its title), PCE/PPI. Blocked rather than de-allowlisted because
scan_universe_reason() reads "not blocked and not allowed" as a scan
candidate -- these were scan members.

### 2. Company headcount: EVENT_BLOCK_PATTERNS (new) + `KX[A-Z0-9]+HEADCOUNT`

The Fiscal.ai company-KPI class names the metric in the EVENT segment, one
series per company for all its KPIs: KXAMZN-26OCTEMP ("Amazon headcount in
Q3"), KXAMZNA-28JANHEAD, KXGOOGA-28JANHEAD, KXINTC-26OCTHEAD ... (21 HEAD
events + 1 EMP event in the catalog) beside the same companies' DAP /
CLICKS / IMPR / PROD / DEL / CARDS / MAU / RESTS / STORES / COMPTXN events
that stay quotable. No series prefix or pattern can say "this KPI of every
company", so `_blocked()` gained a third test: `event_pattern_blocked`,
a FULL-match of the event ticker against EVENT_BLOCK_PATTERNS (env
IMM_BLOCK_EVENT_PATTERNS), default
`KX[A-Z0-9]+-\d\d(?:[A-Z]{3}|Q[1-4])(?:HEAD|HEADCOUNT|EMP|EMPL|EMPLOYEES)`.
The "<series>-X" family probe has no date and never matches, so KXAMZN
stays in the company set with its other KPIs. Meta's headcount is its own
series (KXMETAHEADCOUNT-26Q4, no suffix) -> SERIES_BLOCK_PATTERNS entry.
Wind-down exemption (BLOCKLIST_WIND_DOWN_EVENTS) works on event blocks too.

### 3. Live-feed rule: keywords swept, prong B added, cache re-scored, members re-tested

- `SCAN_LIVE_SOURCE_KEYWORDS` += openrouter, vercel.com, arena.ai,
  artificialanalysis, realclearpolling, steampowered, usgs.gov,
  synopticdata, tt-series, ornnai.com, data.ornn.com, rottentomatoes,
  metacritic. Chosen by sweeping every settlement-source host in the
  catalog against the live program feed for the OpenRouter shape (public,
  continuously updated, the market is a function of the running value).
  KXRT / KXMC are NORMAL-book allow entries (Jack's) and untouched -- the
  keywords only keep a look-alike (KXRTTV) out of the scan. Judgment calls
  NOT added: portwatch.imf.org (KXSUEZWEEKLY / KXBABELMANDEBWEEKLY --
  daily transit counts published with a multi-day lag; credited scan
  admits so far, 0 unpaid events today), luminatedata (weekly album
  consumption, a weekly report), gasprices.aaa (a daily print; the gas
  families are hand-managed in the normal book).
- Prong B (`live_reward_overlaps`): a live source rejects a market only
  when the reward window covers >= SCAN_LIVE_OVERLAP_MIN (0.8) of the
  market's open->close life OR ends within SCAN_LIVE_TAIL_HOURS (24) of
  the close (a late boost on a live-feed market IS the reveal:
  KXOPENSHARE-26SEP21's program was the last 3.3 days of its 6.5-day
  week). Unknown timestamps fail closed. `IMM_SCAN_LIVE_OVERLAP_MIN=0`
  restores the pure live-source reject the tier ran with 9/5-9/24.
  MEASURED 9/24 over 516 program markets on live-feed hosts: OpenRouter
  0.84-1.00 covered / tail 0-24h, Vercel 0.85 / 0h, YouTube 1.00 / 0h ->
  every one rejected; the only A-and-not-B shapes were categorical
  (KXLLM1, KXTOPMODEL, KXSTEAMTOPSELLER -> 'shape') or prefix-excluded
  (KXBTCPRICE), so the literal two-prong rule admits nothing the old rule
  rejected in practice.
- The series cache (`scan_series_meta`) now persists the source blobs
  (`sources`), and `scan_cached_verdict` re-scores them against the
  current keywords in place -- the scan_history_rescore pattern. A
  pre-9/24 ok-entry has no sources and is STALE (re-read, fail closed);
  a pre-9/24 live_source reject is trusted to its TTL. MarketMeta gained
  `program_start` (the reward window's start; imm_quote_gaps.build_meta
  mirrors it).
- MEMBER POLICY RE-TEST: members skip _scan_admission (quote-to-
  completion), which is how the eight sat on week-old ok-verdicts. The
  refresh now runs `_scan_member_policy` on every member FIRST (the
  series-read budget goes to the quoted book before any candidate): a
  live-source (with prong B) or category-ban verdict evicts the member --
  left out of the candidate list, so it leaves `selected` and the stray-
  order sweep cancels its quotes; positions ride. A pending/unreadable
  read keeps the member. Log tell: `open-scan member <t> evicted: <why>
  (policy re-test)`; refresh reject counts gain `member_<why>`.

Positions ride to settlement under every mechanism above (standard
blocklist semantics; flatten by hand if wanted): CPI 15 markets / 490
gross, headcount 31 / 599, OpenRouter+Vercel 6 / 307 (KXTOKENUSE T150 +62,
T156 +55, T158 +50; KXXIAOMISHARE 1.8 -35, 3.7 +60; KXANTHVREQ T5P5 -45).

Knobs: IMM_BLOCK_SERIES_PATTERNS (now 7 entries), IMM_BLOCK_EVENT_PATTERNS,
IMM_SCAN_LIVE_SOURCE_KEYWORDS, IMM_SCAN_LIVE_OVERLAP_MIN 0.8,
IMM_SCAN_LIVE_TAIL_H 24 -- all in the config hash. Deploy = the in-bot
self-restart on the source mtime (no env change).

Tests: test_cpi_family_is_pattern_blocked,
test_company_headcount_events_are_blocked,
test_live_source_keywords_cover_the_ai_gateway_feeds,
test_cached_series_verdicts_are_rescored_against_the_keywords,
test_live_source_rejects_when_the_reward_window_covers_the_market,
test_a_member_whose_series_turns_live_is_evicted_on_refresh; two older
tests modernised to the sourced cache shape.

## 2026-09-24 pm — Rainstorm spans (KXRAINS<CITY>-<start>-<end>) allowed as a family, quoted until the START date (Jack)

Jack: "allowlist these types of rain markets, up until the start date.
KXRAINSBOS-26SEP26-27SEP26, KXRAINSNYC-26SEP26-27SEP26".

WHAT THEY ARE: "How much will it rain in <city> in September 26-27, 2026?"
-- total precipitation at the city's airport station over a two-day
window, a 10-rung inch ladder (T0P5 .. T5), listed Thu 18:00Z, close Mon
04:59Z. Catalog 9/24: KXRAINSBOS "Rainstorm in Boston", KXRAINSNYC
"Rainstorm in NYC" (custom-frequency series Kalshi lists as storms come).
Live programs (fetch_programs under the launcher env): 10 markets each,
$16.57/market/day, target 1000, df 0.5, 9/24 18:04Z -> Sat 9/26 23:59 ET.
Neither series was allowed before this change (the KXRAIN prefix is only
an open-scan ownership exclusion, never an allow).

RULE: a FAMILY allow keyed on TICKER SHAPE, not name (the 9/1 gas-states
lesson: wire the family, member lists go stale in a day).
rainstorm_span_allowed(ticker) = series fullmatches
RAINSTORM_SERIES_RE (env IMM_RAINSTORM_SERIES_RE, default KXRAINS[A-Z]{3})
AND the next two segments are both day-dates (start, end). Added as one
more clause in _allowed (the blocklist still wins upstream). The name
prefix alone is NOT enough: KXRAINS also names the Seattle / San
Francisco / St Petersburg MONTHLIES (KXRAINSEAM-26SEP-7, KXRAINSFOM --
which is not even in the launcher blocklist -- KXRAINSTPM) and the Seattle
daily KXRAINSEA-26SEP26; all fail the two-date shape, as does the
"<series>-X" family probe. Kill switch IMM_RAINSTORM_ALLOW=0.
CUTOFF: "up until the start date" is the plain midnight-ET ticker rule --
parse_event_date reads the second segment, the window's START day -- so
the bot is out at 00:00 ET on that day (Sat 9/26 04:00Z for the first
event). The archetype override SERIES_OVERRIDES["KXRAINSBOS"]
(cutoff_before_event_min=0 via IMM_RAINSTORM_CUTOFF_BEFORE_MIN, rain band
5-90) PINS that reading, the KXRAINWKND pattern; every other city clones
it through FAMILY_OVERRIDE_PARENTS ("pattern", RAINSTORM_SERIES_RE,
KXRAINSBOS) at first sight in the candidates loop, and a city the
quote-gaps mirror sees first still gets the same cutoff from the midnight
rule alone. Kalshi's occurrence_datetime equals expiration (Mon 05:00Z),
never a cutoff candidate. Inherited by PREFIX like the weekend family: the
KXRAIN 7pm-01:59 ET size halving (measured at deploy: hour_mult 0.5,
ladder [(0, 10)]) and the open-scan exclusion. NOT inherited: the NWS fair
gate and the directional take (exact RAIN_FAIR_SERIES).

DEPLOY (9/25 ~02:07Z, in-place edits -> source-mtime clean exits ->
launcher relaunch): both 9/26-27 events enter on the first refresh,
~26h of quoting to the Saturday 00:00 ET stop; positions ride to Monday's
settlement.

Tests: test_rainstorm_span_family_allowed_by_shape (BOS/NYC/event form/a
future city allowed; monthlies, Seattle daily, one-date sibling, probe
form and KXRAINWKND fail the shape; no KXRAIN prefix in
ALLOW_SERIES_PREFIXES; blocklist wins; kill switch) and
test_rainstorm_span_quotes_until_the_start_date (parse -> raw -> both
producers land Sat 00:00 ET; archetype fields; NYC inherits; member quotes
to the instant, fresh entry stops at the buffer); 616 green.

MEASURED AT DEPLOY (relaunch 02:09:23Z, first refresh 02:10:58Z 9/25):
candidates 1792 -> 1841, "KXRAINSNYC: family override inherited from
KXRAINSBOS" logged, all 20 rainstorm markets evaluated -- and all 20
rejected as payout_floor: est $0.05-0.10/market/day against the $1.50
projected floor with ~1.08 quotable days left. The books are the reason:
KXRAINSBOS-...-T5 rests 5 @ 32c YES / 5 @ 23c NO at the touch (a 45c
spread) over 4,400 @ 2c + 1,047 @ 1c YES and 5,300 @ 2c + 1,162 @ 1c NO;
T1 is 9 @ 59c over 3,917 @ 2c + 1,801 @ 1c YES and 8,714 @ 1c NO. The
estimator's reference walk (cumulative depth >= target/5 = 200) lands on
the 2c junk level, so a 10-20 lot rung is ~0.2% of the counted depth and
the pool projects to pennies. Same shape on the weekend family: the
KXRAINWKND-26SEP26 members sit at est_frac 0.00000 as sticky members
(ask-only 30 lots on HOU, 3/22 book). The bot re-evaluates every refresh
until the Sat 00:00 ET stop, so it enters on its own if the junk clears
or a pool grows. Levers if Jack wants them quoted regardless:
IMM_FORCE_EVENTS (bypasses the floor; precedent KXFSLR) or a min_est_total
on the KXRAINSBOS archetype -- both are paying collateral for an
estimated ~$0.10 of reward per market unless the estimator is wrong about
1c/2c junk counting as qualifying depth (unmeasured for this family).

## 2026-09-24 pm — KXRTTV quoted under the KXRT rules (Jack)

Jack, after "what is KXRTTV": "yes quote KXRTTV under the KXRT rules".

KXRTTV is Kalshi's Rotten Tomatoes series for TELEVISION shows, the twin of
KXRT (movies): one event per show (KXRTTV-VIS VisionQuest, scores Oct 17;
-BLA Blade Runner 2099, Nov 28; -HAR Harry Potter and the Philosopher's
Stone, Dec 28), ~10 Tomatometer-score strikes each, "score above N on
<date> at 10:00 AM ET", same rottentomatoes.com source. Never in the book:
3 program events all-time, all paid out, zero positions; KXRT is an exact
allow entry so KXRTTV was reachable only as an open-scan candidate, and
the 9/24 pm live-feed sweep had just given the scan a `rottentomatoes`
keyword to keep it out.

CHANGE: `KXRTTV` added to `_DEFAULT_ENTERTAINMENT_SERIES` (exact series) and
to the `IMM_KXRT_CUTOFF_SERIES` default ("KXRT,KXRTTV"), so
`register_close_cutoff_days` gives it the same 7-day release-week
stand-down and nothing else -- no guard set, no cap, no hour rule, exactly
KXRT's SeriesOverride. `rottentomatoes` dropped from
`SCAN_LIVE_SOURCE_KEYWORDS` (inert once KXRTTV is allowed, and misleading:
RT series are the normal book's call; a future one belongs in the allow
list + cutoff list, not the scan). imm_quote_gaps gains the series label.
Tests: test_kxrt_release_week_stand_down covers KXRTTV; the keyword test
pins RT as NOT a keyword.

WATCH: no live program on any KXRTTV event today, so nothing changes in the
book until Kalshi funds one; the first sign will be KXRTTV in the
"selected" log line, quoting until close - 7d.

## 2026-09-25 — KXART (Sotheby's lot prices) allowlisted, 3 per event, out at midnight before the sale (Jack)

Jack: "allowlist KXART, max 3 markets per event".

WHAT IT IS: Kalshi's Sotheby's live-auction series -- one event per lot,
KXART-SOT10107OCT26 = lot 101 of the sale beginning Oct 7 2026 at 11:00 AM
ET ("If the lot sold price of Opus III (lot 101) by Alma Thomas on Sotheby's
is above $30K during the live auction beginning October 7, 2026 at 11:00
AM"), 9 "greater" strikes per lot ($30K ... $500K), close Oct 8 14:00Z,
occurrence_datetime = close. On 9/25: 11 lots (Alma Thomas, Alexander
Calder), ~$45/market on a 2-day listing program (9/25 00:02Z -> 9/27
03:59Z, ~$21/market/day), never quoted, zero positions, no scan verdict.

THREE MECHANISMS:
1. `KXART` in `_DEFAULT_ENTERTAINMENT_SERIES` (exact series). KXART is a
   PREFIX of twenty unrelated series (KXARTISTSTREAMS*, KXARTISTCOLLAB*,
   KXARTEMISII, KXARTICICE); the allowlist is exact so none ride in.
2. Per-event cap: EVENT_TOP_N gained an EXACT-match form, `=KXART:3`
   (`_parse_event_top_n` / `event_top_n_for`), because the prefix form
   would have capped KXARTISTSTREAMS etc. the day the scan admitted one.
   ROI-ranked, sticky slots, lifetime ledger -- the CC/gas semantics.
3. AUCTION-DAY CUTOFF (`AUCTION_DATE_SERIES`, env IMM_AUCTION_DATE_SERIES,
   default KXART): the hammer IS the reveal, a day before the close, and
   the auction date sits at the END of the event segment as DDMMMYY glued
   to the lot number (SOT101|07OCT26), which parse_event_date's
   leading-YYMMMDD rule cannot read -- trade_cutoff_utc returned None, so
   without this the bot would have quoted through the sale and the day
   after. `auction_event_date` reads the trailing date; the cutoff is 00:00
   ET on sale day (the midnight-before rule every dated ticker gets),
   applied in apply_series_cutoff_adjustments for BOTH producers and the
   quote-gaps mirror, never loosening. Unreadable segment ->
   RELEASE_GUARD_UNKNOWN (stood down, fail closed), logged once. Today's
   program ends 9/27, ten days before the sale, so the cutoff is inert
   until Kalshi renews the pools into October.

No safe-join, no cap, no hour rule -- KXRT's shape. Both knobs are in the
config hash. imm_quote_gaps labels the series.

Tests: test_kxart_auction_day_cutoff_and_three_per_event (date parse incl.
4-digit lot / other house / EST, tightener never loosens, fail-closed,
kill switch, screen, exact allow vs the KXART* near misses, exact cap
form, 9-lot ROI cut keeps 3), parser test for '=' form; suite green.

## 2026-09-25 — Award shows allowlisted: KXGGNOM, KXNATBOOKAWARDS, KXGRAMMY, KXVMA + the KXOSCAR family; 3 per event; out one month before the event (Jack)

Jack: "also allowlist KXGGNOM, KXNATBOOKAWARDS, KXGRAMMY, KXOSCAR, KXVMA.
max 3 markets per event, and do not quote within 1 month of when the event
starts".

WHAT THEY ARE (API 9/25): one series per show, one event per category,
~10 nominee binaries each, all undated tickers --
- KXGGNOM-ANI26 ... (22 events): 84th Golden Globe NOMINATIONS, announced
  Dec 8 2026 (expected_expiration Dec 9 15:00Z; occurrence = the 2027
  placeholder close). $100/market, 5-day program 9/22 -> 9/27.
- KXNATBOOKAWARDS-FIC26 ... (5): 77th National Book Awards, ceremony Nov 18
  2026 (expiration Nov 19 04:59Z). $80/market.
- KXGRAMMY-BAMP69 ... (29): 69th Grammys, Feb 7 2027 (expiration Feb 8
  04:59Z; close is a 2027-12-31 placeholder). $60/market.
- KXVMA-ALB26 ... (23): 2026 MTV VMAs, Sep 27 2026 (expiration Sep 28
  03:59Z) -- two days away, i.e. already inside its month.
- KXOSCAR: no such series. The Oscars are a FAMILY of per-category series
  (KXOSCARPIC-27, KXOSCARINTLFILM-27 ... 66 in the catalog, every one an
  Oscars market); only KXOSCARINTLFILM-27 carries a program today
  ($40/market). Their expected_expiration is the 2027-12-31 placeholder
  and there is no occurrence: NO machine-readable date at all.
None had ever been quoted; zero positions.

RULE SET (registered beside `awards_event_start`, AWARDS_SERIES =
KXGGNOM,KXNATBOOKAWARDS,KXGRAMMY,KXVMA,KXOSCAR):
1. Allow: the four exact series in _DEFAULT_ENTERTAINMENT_SERIES (KXGRAMMY
   / KXVMA are prefixes of dozens of per-category / per-artist strangers --
   KXGRAMMYNOMSOTY, KXGRAMMYCOUNTSZA, KXVMAPOP -- which stay out); KXOSCAR
   as a PREFIX in ALLOW_SERIES_PREFIXES (the whole family), with a
   FAMILY_OVERRIDE_PARENTS pattern `(?!.*MENTION)KXOSCAR[A-Z0-9]*` so every
   category series inherits KXOSCAR's guard set and KXOSCARMENTION stays
   the mention family's.
2. 3 per event by ROI: EVENT_TOP_N `=KXGGNOM:3,=KXNATBOOKAWARDS:3,
   =KXGRAMMY:3,=KXVMA:3,KXOSCAR:3`. event_top_n_for gained one rule with
   it: a MENTION-suffix series is capped only by an EXACT entry, so the
   KXOSCAR prefix never trims KXOSCARMENTION's word legs.
3. Safe-join (the KXCMA precedent for nominee binaries; free on stacked
   touches per the 9/11 measurement).
4. PRE-EVENT STAND-DOWN, new SeriesOverride fields `pre_event_days` (31)
   and `pre_event_dates_only`: cutoff = event start - 31 days, applied in
   apply_series_cutoff_adjustments (both producers + quote-gaps mirror),
   never loosening. The start comes from (a) AWARDS_EVENT_DATES, a hand
   table of event-ticker globs (default `KXOSCARNOM*-27=2027-01-21,
   KXOSCAR*-27=2027-03-14`, the Academy's published 99th-Oscars schedule --
   ceremony Mar 14 2027, nominations Jan 21 2027 -- per Deadline / Screen
   Daily / The Gold Knight, Apr 2026; UPDATE YEARLY, a family year without
   a row stands down), else (b) Kalshi's occurrence when it precedes the
   expiration by >= 60 min, else the expiration itself; a Dec 31 expiration
   is the placeholder shape and reads as unknown; unknown ->
   RELEASE_GUARD_UNKNOWN (stood down, fail closed, logged once). KXOSCAR is
   table-only (IMM_AWARDS_TABLE_ONLY_SERIES). 31 not 30: measured from an
   end-of-day expiration, 30 days would start the stand-down inside the
   month before the ceremony evening.

RESULTING CUTOFFS: GGNOM Nov 8 15:00Z, National Book Awards Oct 19 04:59Z,
Grammys Jan 8 2027 04:59Z, Oscars (winners) Feb 11 2027 05:00Z / (nominations
series) Dec 21 2026, VMAs Aug 28 -- already past, so KXVMA is stood down
from the first refresh (its live program IS the ceremony week: exactly the
window the rule excludes).

WHAT THIS DOES NOT COVER: nomination / finalist announcements that land more
than a month before the ceremony -- Oscar nominations Jan 21 (ceremony Mar
14), Grammy nominations ~Nov 7 (Feb 7), National Book Awards finalists Oct 6
(Nov 18). The rule as given is the event start, so the bot quotes through
those reveals unless IMM_AWARDS_PRE_EVENT_DAYS is raised or a nominations
row is added to the table. Today's programs all end 9/27, before any of
them.

Knobs: IMM_AWARDS_SERIES, IMM_AWARDS_PRE_EVENT_DAYS, IMM_AWARDS_EVENT_DATES,
IMM_AWARDS_TABLE_ONLY_SERIES (all in the config hash).
Tests: test_award_shows_three_per_event_and_one_month_stand_down (starts from
table / occurrence / expiration / placeholder / missing, cutoffs for all
five incl. the Oscar family inheritance and the mention carve-out, exact
allow vs the strangers, caps, screen); allowlist test extended; suite green.

## 2026-09-25 pm — Sub-$1 credit was being stranded: the exit bar is now the $1.00 cliff, the floor projection is judged at day size, counters survive an eviction (Jack)

Jack: "why are there a bunch of markets on KXVENUEPERFORM-REDROCKS28JAN01
that stopped quoting but are close to the $1 cutoff? that is lost money"
-> "build it".

WHAT WAS HAPPENING (measured 9/25, live feed + state + selection_events +
cycle logs): Red Rocks = 35 markets, one $200 program each, period 9/17
21:02Z -> 10/01 21:02Z ($14.29/market/day), est share $0.04-0.15/market/day.
Eleven members had banked $0.50-$0.99 this period (REB .99, HOZ .93, DEA
.92, RUF .92, JOH .91, GRE .91, NAT .86, TUR .79, LUM .77, ODE .74, BIL
.50) and were being evicted as "hopeless"; twelve more (JOE .99, VAM .96,
TAM .82, PRE .79, FRE .77, DOM .72, KAC .72, CHR .70, NOA .66, WID .65,
LOR .62, BLU .55 -- reconstructed from the cycle logs, the same integral
matches the 23 surviving counters to the cent) had no counter at all and sat
in payout_floor. ~$18 of period accrual on the event, ~$44 book-wide (25
kept-counter + 35 pruned markets in the $0.50-$1.00 band), all paying $0
unless each market crosses $1.00 by its period end. 1,510 unquoted
market-hours on the event over the period.

THREE DEFECTS, THREE MECHANISMS (each with a kill switch):

1. THE EXIT READ THE ENTRY BAR. The 9/12 move of MIN_EST_TOTAL_DOLLARS to
   $1.50 fed the same `series_min_est_total()` to the hopeless exit, so a
   member with $0.93 banked and a $1.25 projection (HOZ, 9/24 14:00Z) was
   above the exchange's real cliff, under the entry bar, and evicted -- the
   $0.93 then pays nothing. The $0.50 margin was ENTRY risk cover; on an
   exit it guarantees the sub-$1 outcome it was meant to avoid.
   -> `floor_bar_dollars(series, banked)`: a MEMBER, or a re-entrant with
   ANY banked accrual this period, is tested against PAYOUT_FLOOR_DOLLARS
   ($1.00); a fresh candidate still needs $1.50. `EXIT_FLOOR_IS_PAYOUT`
   (env IMM_EXIT_FLOOR_IS_PAYOUT=0 restores the single $1.50 bar).
2. THE CHURN ENGINE. `_estimate_candidate_yield` sizes its ladder with
   `hour_size_mult` (launcher IMM_HOUR_SIZE_MULT=0-9:2.0, Saturday x1.5),
   so est/day doubled 04:00-13:59Z: the evicted markets cleared $1.50 and
   re-entered at the 04:00Z refresh, halved at 14:00Z (cycle log: HOZ
   est_frac .00613 -> .00308 at 14:00:32Z), and HOPELESS_SUSTAIN_SECS later
   were evicted again -- ~9h quoted / ~14h dark, every day (HOZ 9/23, 9/24,
   9/25 identical). A market must not be admitted on doubled size and
   evicted on normal size.
   -> `MarketMeta.floor_dollars_per_day`: the share the DAY ladder
   (`base_scaled_levels`: hour/Saturday multiplier off, family multiplier
   and the Carbon Arc late-month rule kept) would earn on the EXTERNAL book
   (an incumbent's own hour-scaled orders stripped first by
   `external_levels`, touches and reference prices re-read on that book).
   Equal to est_dollars_per_day whenever no multiplier is active, so nothing
   changes outside the windows. Read by the entry floor, the hopeless exit,
   the 1h peak and the rate-floor escape; ranking/yield keep the live
   estimate. `FLOOR_PROJECTION_BASE_SIZE` (env
   IMM_FLOOR_PROJECTION_BASE_SIZE=0 restores the live-size projection).
   Logged per market in selection_events as `floor_dollars_per_day`.
3. THE CREDIT WAS FORGOTTEN. `known_tickers &= managed | positions` and
   `_save_persist` pruned accrued_est / period_base / period_start /
   hopeless_since / paid_crossed with it, so a FLAT evicted market lost its
   counter at the next restart (~20 restarts/day), re-entered with $0 and
   could never clear $1.50 on rate alone. The 11 with positions kept
   theirs; the 12 flat ones did not.
   -> `_keeps_accrual(t)`: persist while quoted/held (known_tickers) OR
   while the market's LIVE program period is running (`state.programmed`);
   before the first successful feed of a run (programmed empty) the
   counters the file already held are kept (`_loaded_credit`), a counter
   born in memory this run still prunes as before. Once the program ends
   and the market is flat, it prunes as it always did, so a re-listing
   starts clean. `KEEP_ACCRUAL_WHILE_PROGRAMMED` (env
   IMM_KEEP_ACCRUAL_WHILE_PROGRAMMED=0 restores the known_tickers-only
   prune).

The quote-gaps email mirror (`imm_quote_gaps.py`) reads the same three
things (`floor_dollars_per_day`, `period_accrued`, `floor_bar_dollars`) so
"under payout floor" means what the bot means. All three knobs are in the
config hash. Startup logs one ASCII line: `[IMM] floor credit (2026-09-25):
exit / banked re-entry bar = $1.00 payout cliff; fresh entry bar = $1.50;
floor projection at day size; accrual counters kept while programmed`.

ONE-OFF STATE RESTORE: the 12 forgotten Red Rocks counters were written
back into imm_state.json during the deploy's restart window (accrued_est =
the cycle-log reconstruction, period_base 0, period_start = the live
period key; backup `imm_state_backup_<ts>_pre_redrocks_restore.json`).
Existing counters were not touched.

WHAT THIS DOES NOT FIX: the estimate itself (accrual model ~1.07x on
covered periods, see imm_reward_recon) -- the cliff test is only as good
as the counter; markets whose program ended with the credit under $1
before 9/25 are gone; deliberate stand-downs (KXCPI blocklist, KXRT 7-day
cutoff) still park credit under the cliff by design; the day-size
projection is conservative during the multiplier windows (a doubled ladder
really does accrue faster), so a market genuinely borderline at day size
is judged as if it never got the quiet-hours boost.

Tests: `TestExitBarIsThePayoutCliff`, `TestFloorProjectionAtDaySize`,
`TestAccrualCountersSurviveEviction` (+ the existing "nothing can reach the
bar" tests now raise BOTH bars). Suite 626 green at deploy.

## 2026-09-26 — Award shows: the stand-down month runs to the NOMINATIONS, not the ceremony (Jack)

Jack, on the 9/25 caveat: "stand down at nominations instead of the
ceremony".

CHANGE: the awards event start is now the first reveal that narrows the
field. Kalshi's API knows nothing about nominations (occurrence = close or
expiration; expiration = the ceremony), so every WINNER family is now
TABLE-ONLY (`AWARDS_TABLE_ONLY_SERIES` = KXGRAMMY,KXNATBOOKAWARDS,KXVMA,
KXOSCAR): the hand table `AWARDS_EVENT_DATES` carries the announcement
dates and a family year without a row stands down until someone adds one.
KXGGNOM stays on the API fallback because its markets ARE the nominations
(expiration = the day after the Dec 8 announcement).

TABLE (default, verified 9/26; env IMM_AWARDS_EVENT_DATES replaces the
whole thing):
- `KXGRAMMY-*69=2026-11-16` -- 69th Grammy nominations Nov 16 2026
  (Recording Academy / Rolling Stone; show Feb 7 2027) -> out Oct 16
  05:00Z (was Jan 8).
- `KXNATBOOKAWARDS-*26=2026-10-06` -- finalists Oct 6 2026
  (nationalbook.org; ceremony Nov 18) -> out Sep 5, i.e. ALREADY inside
  the month: the family stands down from this deploy (it had 0 selected
  anyway: payout floor / extreme mid).
- 24 rows `KXOSCAR<shortlist category>-27=2026-12-15` -- the 99th Oscars
  shortlists (international / documentary feature + short / animated +
  live-action short / score / song / makeup & hair / sound / VFX / casting,
  winner AND nomination series; _OSCAR_SHORTLIST_SERIES_27) -> out Nov 14;
  KXOSCARINTLFILM, the one Oscar series with a live program, is one of
  them.
- `KXOSCAR*-27=2027-01-21` -- nominations Jan 21 2027 for every other
  Oscar series -> out Dec 21 (was Feb 11). The Mar 14 ceremony date is
  gone from the table.
- KXVMA: no row on purpose (the 2026 nominations landed in early
  September, exact day unverified; the family is inside its month either
  way) -> RELEASE_GUARD_UNKNOWN, stood down, one warning line per event.

ALSO: the 66-name audit of the KXOSCAR prefix found KXOSCARAWARDACTR, a SAG
Award series filed under the Oscar prefix -> SERIES_BLOCK_PATTERNS
`KXOSCARAWARD[A-Z]*` so the prefix allow can never quote it on the Oscars'
dates. KXOSCARNOMBSOUND is Best Song and KXOSCARVIS is Makeup & Hairstyling
despite their names (both in the shortlist list).

Tests: test_award_shows_three_per_event_and_one_month_stand_down updated
(table beats API for winners, shortlist vs nominations rows, VMA fail
closed, table-only flags, the SAG block); suite green.

## 2026-09-26 — Sports ladders / escalators: 1-99c band and x2 size (Jack)

Jack, after "i see a lot of events like KXNFLLADDERRECYDS-26SEP27NEJAC, but
none are quoting. why?": the Sunday slate (466 markets / 98 events, programs
since 9/25 22:03Z) was allowed and kickoffs resolved, but at the 14:08Z
refresh 221 ladders failed the $1.50 projection -- a designated maker rests
20k-60k contracts at the touch, Kalshi scores the first 1,000, so our 30-lot
was ~0.3% of the scored depth (~$0.35/day on a $113/day pool, ~$0.40 to
kickoff) -- 117 escalators sat under the 5c mid band, 69 read zero yield
(sub-penny escalator prices round to a 0c spread), 59 were selected and 46
resting. Jack: "for ESCALATOR/LADDER only, allow quoting range 1-99c and
double contract size (use that for calculating if hits payout floor)".

CHANGE (on the KXNFLLADDERREC archetype every pattern sibling clones):
price_min_cents 1 / price_max_cents 99 (env IMM_SPORTS_LADDER_PRICE_MIN/MAX)
and a new SeriesOverride.size_mult = 2.0 (env IMM_SPORTS_LADDER_SIZE_MULT).
size_mult rides applied_mention_mult -- the single family multiplier the
ladder (hour_scaled_levels), the per-market cap (series_max_position), the
per-event cap (event_cap_contracts) and the skew knees already read -- so
the estimator's hypothetical ladder, and with it the payout-floor
projection, is the doubled one. The extreme_mid screen now widens to the
series' own band (min/max with the global 5-90; a narrower series band
never tightens it), so 2-4c escalator mids are candidates. quotable_sides,
the quote loop's band and the sticky widening all read the series band.
Expect: ladder rungs 40 (x deep-ref up to 60), est share ~x2 on the same
books -- most 20k-deep ladders still project under $1.50 to kickoff, the
thinner books and the escalators clear more often; escalators whose bid and
ask round to the same cent stay zero_yield (sub-penny pricing, not fixed).

Tests: the ladder allowlist test now checks band (1,99) for fresh and
members, x2 rungs / caps, the screen passing a 3c ladder mid and still
rejecting a 3c ordinary mid, ordinary series unchanged. 626 green.

## 2026-09-26 pm — Sub-penny join: ladder / escalator rungs rest AT the exact touch, not a fraction of a cent behind it (Jack)

Jack: "allow for quoting within a cent if it means staying in the earnings
range. e.g. KXNFLESCALATORREC-26SEP27NYJDET-DETASTBROWN14".

WHY. The sports ladders / escalators are priced in 0.0001 steps
(price_level_structure center_centi_edge_centi_cent, price_ranges step
0.0001); the whole bot reasons in integer cents: orderbook_levels rounds
the book to the penny, the ladder rests on the penny grid, the client
writes body['price'] from integer cents. On DETASTBROWN14 that afternoon
the true book was YES bid 0.1915 x 13,192 (the designated maker) and NO
0.8030 x 10,025 (= YES ask 0.1970); the bot read 19/20 and rested its
30-lots at 0.1900 and 0.2000 -- 0.15c and 0.30c BEHIND the two touches.
Kalshi scores the first 1,000 contracts walking from the best price, all of
them at the maker's level, so a rung one sub-tick behind is not in the
scored range at all: zero share on every sub-penny market, whatever the
band or size (the 9/26 am change doubled a zero).

CHANGE (SUBPENNY_JOIN, env IMM_SUBPENNY_JOIN=0 = off). Every integer-cent
decision is untouched. For a market whose MarketMeta.price_step is finer
than a cent (market_price_step(m): finest price_ranges step, else 0.001 if
the level structure says "centi", else 0.01; set on the selection meta and
the orphan-restore meta), the quote loop runs subpenny_snap(mq, exact book,
own exact) after the ladder and pads are built: each non-pad rung snaps to
the most aggressive EXTERNAL level inside its own cent bucket -- bid 19 ->
19.15, ask 20 -> 19.70. Rules: never outside the rung's bucket ("within a
cent"), never crossing the exact opposite touch, never a price that does
not already exist on the grid (we only join a level), our own resting size
netted out first (no self-chase), no external level in the bucket -> the
integer price stands (it is already the best in that cent). Quote gained
price_exact (YES cents, 2 dp); place_order sends it as
create_order(price_dollars="0.1915") -- the client's _build_v2_order_body
takes price_dollars and still derives the book side from yes_price /
no_price -- and records yes_price_exact in the ledger / sim order and the
order log (place rows). diff_orders: an exact rung matches only a resting
order at that exact price (order_yes_exact_cents: our ledger's
yes_price_exact, else the exchange's yes_price_dollars / price_dollars, 4
dp); a moved touch is a cancel + fresh place, never an amend (amend takes
integer cents) and never an aggressive-keep; an INTEGER rung still keeps an
exact resting order sitting in its bucket. Integer-priced markets never
reach any of it (price_step 0.01).

Expect on the Sunday escalators: resting orders at the maker's exact levels
(0.1915 / 0.1970 on DETASTBROWN14 while the maker sits there), share per the
scored walk instead of zero; more cancel+place churn on those markets as the
maker moves by sub-ticks (each move re-places the rung). Not changed:
escalators whose bid and ask round to the same cent still read zero_yield in
the estimator (integer spread 0), so they are not selected in the first
place; the "within a cent" rule cannot help a market the integer logic will
not quote.

Verify: run-logs/incentive-mm order log place rows carry yes_price_exact;
the bot log prints "placed ... @ 19.15c"; the exchange's resting orders for
KXNFLESCALATOR*/KXNFLLADDER* show 4-decimal yes_price_dollars.

Tests: test_subpenny_join_rests_at_the_exact_touch -- the DETASTBROWN14 book
snaps 19/20 -> 19.15/19.70 with the buckets kept and the pad untouched;
own-size netting leaves a lone rung on the cent; a locked synthetic book
refuses to cross; price-step detection; exact-price parsing; the diff in
both ladder modes (cancel+place on a moved touch, no-op at the exact price,
integer rung keeps an exact order); the wire body ("0.1915" / "0.1970" vs
"0.20" on the integer path); a dry placement records the exact price so the
next diff keeps it. 627 green.

VERIFIED 15:21Z (deploy c76a0e3 15:15Z, clean exit 15:16:14Z, relaunch
15:17:19Z, first cycle 15:21:15Z): the DETASTBROWN14 rungs went 0.1900 /
0.2000 -> 0.1915 / 0.1965 (the maker's ask had moved 0.1970 -> 0.1966 ->
0.1965); exchange probe once the book had rebuilt (15:29Z; a probe 40s after
the first cycle had caught the rebuild mid-way at 82 orders on 41
markets -- a restart cancels everything and re-places over ~3 cycles):
98 resting orders on the same 49 ladder / escalator markets as before
the restart, 96 exactly at the external touch, 2 one sub-tick behind
(the maker moved after placement; such rungs are re-placed by the next
cycle, cancel + place, the unchanged exact rungs left alone); the whole-cent
ladders (LADDERREC / FFPTS / RECYDS makers quote on the penny grid) keep
their integer prices, which ARE the touch there. Order log run 84064ca2:
621 places, 0 rejects, 19 exact-price place rows on 8 escalator markets,
no amend rows on the sub-penny markets. Before-picture (15:14Z, scratchpad
subpenny_before.json): 98 orders on 49 markets, none at a sub-penny price.

## 2026-09-26 pm — The payout-floor projection follows the size schedule: quiet hours and Saturday weighted over the accrual window, every series (Jack)

Jack, after seeing the ladder sizing table (weekday 40 / Saturday 60 /
quiet hours 80 / Saturday quiet hours 120 for a ladder series; 20 / 30 /
40 / 60 for an ordinary one) and that the floor projection used the day
size: "the payout-floor should factor in Saturday and quiet-hour size
proportionally to the calculation. this should be true generally, not just
on ESCALATOR/LADDER".

WHY. 9/25 moved the floors (entry bar, hopeless exit, 1h peak, rate-floor
escape) from the live-size estimate to a DAY-size projection because the
live estimate doubled at 04:00Z and halved at 14:00Z and markets were
admitted on the doubled number and evicted an hour after the halving,
every day. Day size stopped the churn but under-projects every market
whose remaining window contains quiet hours or a Saturday: the bot WILL
rest the bigger ladder then and take the bigger share, and that credit is
real. It also over-projects the evening-halved families (gas / diesel /
rain dailies, KXTRUEV at x0.5 from 16:00-19:00 ET to 01:59) on the same
logic in reverse.

CHANGE (FLOOR_PROJECTION_SCHEDULE, default on; IMM_FLOOR_PROJECTION_
SCHEDULE=0 restores the flat day size; IMM_FLOOR_PROJECTION_BASE_SIZE=0
still means the live-size projection). MarketMeta.floor_dollars_per_day
is now the window-weighted mean of what the ladder earns at EACH size
multiplier the schedule will apply over the remaining accrual window:
  * size_mult_profile(series, now, _quotable_days(meta)) samples
    hour_size_mult once per ET hour from now to the end of the window
    (program end capped by cutoff / close, the same horizon the $ total
    uses) and returns [(multiplier, share of the window)] -- quiet hours
    x2, Saturday x1.5 (composed x3), the per-series evening halvings, the
    daily-family / open-scan / prefix exclusions, all of it, because it IS
    hour_size_mult. The walk is capped at FLOOR_PROFILE_MAX_DAYS (14, two
    weekly cycles, env IMM_FLOOR_PROFILE_MAX_DAYS); that mix stands for a
    longer window. An empty window reports the live multiplier alone.
  * for each multiplier m in the profile the estimator builds the ladder
    at m (scaled_levels_at: series levels x m x family multiplier, the
    same shape hour_scaled_levels produces when the clock reads m; the
    reference multiplier goes through capped_ref_mult(hour_mult=m), so
    TOTAL_SIZE_MULT_CAP applies the way it will at that hour) on the
    EXTERNAL book -- an incumbent's own hour-scaled orders stripped first
    (external_levels), touches and reference prices re-read from that
    book -- scores it with estimate_reward_share, and the floor $/day is
    sum(share_m x pool x weight_m). A window carrying the live multiplier
    alone is the live estimate itself (for an incumbent: the score of what
    actually rests), exactly as before.
  * est_total = floor_dollars_per_day x quotable_days is unchanged
    downstream (entry bar, hopeless exit, 1h peak, rate-floor escape).
  * MarketMeta.floor_mult_profile ("1:0.583,2:0.417") is written to the
    selection_snapshot sink next to floor_dollars_per_day, and the two
    knobs join the config hash.

Properties. Time-consistent by construction: the mix a window carries does
not depend on which part of it is happening now, so the 04:00Z and 14:00Z
projections agree up to the window shrinking -- no re-admit / evict cycle
(the 9/25 property is kept, the under-projection is not). Saturday
afternoon: every long-dated candidate's window holds the rest of Saturday
at x1.5 and tonight's quiet hours at x3, so projections rise at once;
weekdays they rise by the quiet-hours share (10/24 of the window at the
x2 ladder's share). Evening-halved families project lower for the hours
they are halved. The share is concave in size, so a x2 window is worth
less than 2x -- the estimator scores each multiplier's ladder against the
book rather than scaling a number.

Cost: up to len(profile) extra estimate_reward_share calls per candidate
per refresh (2-4, pure arithmetic on the book already read; the
orderbook GET dominates) plus <=336 hour_size_mult samples per candidate.

Tests: TestFloorProjectionFollowsSchedule -- the profile splits a Saturday
evening window 4h x1.5 / 6h x2, a full day 10/24 x2, a full week
10/168 x3 + 14/168 x1.5 + 60/168 x2 + 84/168 x1, caps at 14 days, reads
1.0 for an excluded family, reports the live multiplier for an empty
window; the floor equals the live estimate with no multiplier in the
window, equals the doubled estimate under an all-day x2 window (the 9/25
rule read the day number there; it still does behind the kill switch),
equals (14 x day + 10 x doubled) / 24 under a 0-9 ET window over a
one-day accrual window, and an incumbent's day term is the plain external
read. 628 green.
