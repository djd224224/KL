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
