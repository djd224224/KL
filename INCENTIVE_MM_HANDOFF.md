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
Since 2026-10-02 `imm_account.py` can point the IMM (and only the IMM) at its
own Kalshi account -- see that day's "own Kalshi account" section.

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
| **Daily loss halt** | realized + MTM P&L **today** ≤ −`IMM_DAILY_LOSS_LIMIT` ($1,200; this bot's book only, rewards excluded; baseline rolls at the 5 AM CT / 6 AM ET summary, so banked profit can't mask a bad day and yesterday's breach can't re-halt today) | cancel everything, idle until that 5 AM CT roll (`_next_roll_utc`; until 2026-10-01 it lifted at ET midnight, re-tripped on the same day's counter and idled through the next day), urgent email |
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
5. Register the task (at-logon, non-elevated, same pattern as the crypto fleet).
   Since 2026-09-28 the action is the hidden wrapper `run_incentive_mm_hidden.vbs`
   (it runs `run_incentive_mm.ps1 -Probe` with no console window and waits on it):
   the old visible console WAS the bot, and closing it by accident killed it with
   no order cancel. Ending the task can orphan the launcher under the wrapper, so
   bounce with `restart_imm.ps1 -Task` (it sweeps launcher, cmd, wrapper and
   python), never a bare `Stop-ScheduledTask` / `Start-ScheduledTask`.
   ```powershell
   $act = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B ""C:\Users\jackd\Documents\KL\run_incentive_mm_hidden.vbs"""
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

**Since 2026-10-02 it is not an email of its own** (Jack: "cut it as a standalone
email and add it into the Kalshi portfolio ... email"). The 7:00 portfolio email
(`send_portfolio_digest.py`) runs `send_imm_digest.py --section-out <json>` in a
child process and appends the section after its movers table; the PICK-OFF
WINDOW flag rides on the portfolio subject. A failed section is one line in that
email, never a held email; child output goes to
`run-logs\portfolio-digest\imm-section.log`; kill `PF_IMM_SECTION=0`. The
section's yesterday / 7-day rows and its 30-day daily table are the IMM
dashboard's own figures (`imm_dashboard_summary.json`, written beside the page
each build): trading P&L marked to market and modeled rewards per ET calendar
day, so the email equals the dashboard to the cent. The "Events traded" table is
gone. The `KL incentive_mm DIGEST` 7:10 task now lands on a logged no-op
(delete it at leisure); `python send_imm_digest.py --test` still sends the
section as its own email, `--print` prints it. The history below is how it got
here.

**The digest's own P&L path is retired (2026-10-02).** Once the windows came off
the dashboard, `daily_series`, `pnl_windows`, `raw_pnl_for_fills`,
`et_day_rewards`, `credited_windows`, `fetch_own_fills`, `replay_realized` and
`_yes_delta_and_price` had no callers left; they are gone, with their tests.
The opportunistic email's `pnl_windows` call (its result unread since
2026-09-11) and its 168h fill fetch went with them. Nothing writes or reads
`run-logs\incentive-mm\daily_pnl.json` any more: it stays on disk as a FROZEN
record of per-day RAW, 2026-07-12 to 2026-10-01 (82 days, the only per-day RAW
before the dashboard's position log starts 2026-09-06). `imm_backfill_daily_pnl.py`
rebuilt that file wholesale from the live fills API, whose ~67-day reach means
a re-run would now drop the oldest weeks; it is deleted (Jack), recoverable
from git history.

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
(own_book/current_mids/credit ledger; pnl_windows until it was retired
2026-10-02) — imported, not
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
member deselected, tier closed until the 5am-CT roll (`scan_halt_day`, a
`_halt_day_key` roll day since 2026-10-01, the ET date before),
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

VERIFIED 16:13Z (deploy 7c06a01 16:07Z, clean exit 16:09:54Z, relaunch
16:10:55Z with the new banner, first refresh 16:13:23Z), a Saturday
afternoon: selected 394 -> 413 across 129 -> 134 events, payout_floor
845 -> 820 (selection_snapshot decisions), 19 payout_floor -> selected
flips, all Sunday NFL ladders (window = rest of Saturday at x1.5, the
overnight quiet hours at x2, Sunday morning at x1: profile
1:0.10,1.5:0.49,2:0.41; e.g. KXNFLLADDERREC-26SEP27NYJDET-DETASTBROWN14
floor $0.49 -> $0.81/day, over the cliff with its banked accrual), zero
selected -> other flips (no evictions). Over the 1,227 markets with a
projection in both refreshes the new/old ratio is p10 1.31 / p50 1.50 /
p90 1.65 -- the Saturday x1.5 plus tonight's x2 -- and the evening-halved
families read UNDER 1 as intended: KXRAIN dailies mean 0.88 (profile
0.5:0.31,1:0.69 to the 10pm cutoff), KXDIESELD 0.76 (0.5:0.68,1:0.32).
The extremes (KXRT-HUN-85 18x, the DETASTBROWN14 escalator 0.15x,
KXSUEZWEEKLY 0.25x) are book moves -- their LIVE estimates moved the same
way between the two reads. Profiles are in the sink rows
(floor_mult_profile); 1,697 rows carry none because they never reached
the estimator (cutoff / extreme_mid / one_sided / manual).

## 2026-09-26 pm — Near-cliff rule: a market that has banked most of the $1 and projects to just under it quotes to completion (Jack)

Jack, after the schedule-weighted projection flipped only Sunday NFL
ladders ("im just surprised that ONLY nfl ladders flip and nothing else
flipped at all"): the measured picture at the 16:10Z refresh was 525 of
820 floored markets in daily program periods with ~12h of window left
(4-90x short of the bar, nothing a size multiplier can fix), the NFL
ladders the only large near-bar group, and the Red Rocks strikes the one
family sitting AT the cliff: KXVENUEPERFORM-REDROCKS28JAN01 CHR $0.70
banked + $0.30 projected = $1.00 vs $1.00 (floored by rounding), LOR $0.62
+ $0.36 = $0.98, NOA $0.66 + $0.30 = $0.96, BLU $0.55 + $0.38 = $0.93, BIL
$0.50 + $0.35 = $0.85. While they sit out the banked part is frozen and
the window keeps shrinking, so they can only drift further under. Jack:
"yes, build the knob for banked markets near the cliff".

CHANGE (near_cliff_ok; knobs NEAR_CLIFF_DOLLARS = 0.15, env
IMM_NEAR_CLIFF_DOLLARS, 0 = off; NEAR_CLIFF_MIN_BANKED_FRAC = 0.5, env
IMM_NEAR_CLIFF_MIN_BANKED_FRAC). In the shared floor verdict (entry bar
AND hopeless exit), after `reaches_min = banked + max(projection, 1h peak)
>= floor_bar`: a market that has banked at least half the PAYOUT_FLOOR
cliff THIS period and whose projected total lands within
NEAR_CLIFF_DOLLARS under the cliff is treated as reaching the bar. It
re-enters (or stays -- the hopeless exit reads the same verdict, so no
flapping around the cliff) and quotes to completion. Fresh candidates
(nothing banked) are untouched: the $1.50 entry bar stays. Only in the
cliff regime (EXIT_FLOOR_IS_PAYOUT); off outside it. Bounded downside: at
most NEAR_CLIFF_DOLLARS of projected shortfall on a market that is at
least half paid for, against the whole banked credit as upside. One log
line per market ("near-cliff: <ticker> banked $x + projected $y = $z,
within $0.15 of the $1.00 cliff; quoting to completion"),
MarketMeta.near_cliff in the selection_snapshot rows, both knobs in the
config hash, the banner names the rule.

Not changed: the rate floor (re-entry of non-members below the series
min rate still needs the horizon escape), zero_yield, event caps and
ranking -- a near-cliff market still competes for its seat on
yield_per_contract like everyone else; Red Rocks held seats before, so it
should again.

Expect at the first refresh after deploy: the four Red Rocks strikes
within $0.15 (CHR, LOR, NOA, BLU) back in with near_cliff=true; BIL
($0.85 exactly at the edge, inclusive) in if its projection has not
slipped; KXBAA-28JANDELIV-640 ($1.49 vs the $1.50 FRESH bar) stays out --
nothing banked, the rule does not apply.

Tests: TestNearCliffQuoteToCompletion -- the rule's edges (0.70/0.93 in,
0.50/0.85 in, 0.70/0.84 out, 0.40/0.93 out, fresh never, kill switch,
non-cliff regime); on the exit-bar tests' 1e9 scale a $0.90-banked market
re-enters with near_cliff set, stays as a member with the hopeless clock
expired, $0.80 stays floored, banked-fraction knob at 0.95 keeps it out, a
fresh candidate keeps the $1.50 bar, NEAR_CLIFF_DOLLARS=0 keeps it out.
630 green.

VERIFIED 16:39-16:42Z (deploy 99cbf6f 16:34Z, relaunch 16:37:03Z, first
refresh 16:39:29Z): 10 near-cliff verdicts. Red Rocks LOR ($0.62 + $0.31
= $0.93), NOA ($0.66 + $0.27 = $0.92), CHR ($0.70 + $0.23 = $0.93) and BLU
($0.55 + $0.33 = $0.88) re-entered and are resting two-sided 29/30-lots
since 16:41:39Z (placed in the third cycle: the post-restart placement
cap of 250/cycle deferred them twice); BIL slipped to ~$0.80 and stays
floored, as the rule says. Also admitted on the same verdict:
KXTXOIL-27MAR31-T5.9 ($0.60 + $0.35), KXCBDTHAILAND-26OCT28-C25 ($0.83 +
$0.11), KXFSLR-26OCTMWSOLD-4100/-4200 ($0.71 + $0.23 / $0.16), all
placed. Selected 413 -> 418, payout_floor 820 -> 810, no evictions.
Two notes for the next reader: (1) KXNFLESCALATORREC-26SEP27NYJDET-
DETJGIBBS0 was KEPT as a member on $0.95 banked + $0.00 projected -- the
zero is the estimator's integer-spread blind spot on a sub-penny escalator
(its bid and ask round to one cent), the market earned that $0.95 today
and now rests at the exact touch, so keeping it is right in substance
even though the estimator's own number says nothing more can be earned;
if you want the rule to demand a positive forward projection, that is a
one-line change in near_cliff_ok. (2) The near-cliff log line fires
before the entry chain, so a FRESH zero-yield candidate can log "quoting
to completion" and then be stopped by the zero_yield gate (the RECYDS
DETASTBROWN14 escalator did exactly that; the sink row shows near_cliff
true with decision zero_yield) -- cosmetic, fix with the next functional
change rather than a restart of its own.

## 2026-09-26 pm — Near-cliff SIZE mode: x1.5 ladder until the banked accrual crosses the cliff (Jack)

Jack: "if Banked + projected is near cliff, do a 1.5x size multiplier to
ensure it gets across the cliff".

CHANGE (NEAR_CLIFF_SIZE_MULT = 1.5, env IMM_NEAR_CLIFF_SIZE_MULT, 1.0 =
off). When the floor verdict admits or keeps a market on the near-cliff
rule it also ARMS size mode for that ticker (IncentiveMarketMaker.
_near_cliff_boost, ticker -> armed ts). While armed, _near_cliff_size_mult
(ticker) scales the ladder everywhere the shape is read -- the quote
loop's rungs (so side_max and the deep-reference multiplier scale with
them), the refresh's collateral reservation, the placement guard's
side/level bracket in place_with_caps, and the estimator's hypothetical
ladder (live estimate and every multiplier of the schedule profile) -- the
2026-07-14 rule that every consumer sees one shape. The mode is STICKY
for the period once armed: _prune_near_cliff_boost, run at the top of the
refresh's yield pass, ends it when the banked accrual crosses the cliff
(logs "near-cliff: <ticker> crossed the cliff (banked $x); ladder back to
normal size" -- the $1 is secured and accrual above it pays linearly, so
there is no reason to carry the extra size) or when the market has left
the selection; a period roll resets the banked amount and the market is
judged fresh anyway. Stickiness matters: the boosted projection often
clears the bar outright at the next refresh, which would otherwise switch
the boost off, drop the projection back under, and switch it on again.
Arming happens in the floor verdict, AFTER that refresh's estimate, so the
estimator sees the boosted ladder from the following refresh; the quote
loop sizes it up in the same cycle. The near-cliff log line now names the
size ("quoting to completion at x1.5 size until it crosses"), and the
selection_snapshot rows carry near_cliff_boost next to near_cliff. Also
fixed here: a FRESH zero-yield candidate no longer logs or arms (the
zero_yield gate stops it anyway); members are unaffected.

Why 1.5x and not more: the share model is roughly linear in our size at
these depths (the Red Rocks strikes are ~0.4% of a 12k-50k book), so 1.5x
size is ~1.5x accrual rate -- CHR at $0.045/day x 5.2d = $0.23 becomes
~$0.35 on top of $0.70 banked, over the cliff instead of $0.07 under it.
The cost is 1.5x the fill exposure on markets that are, by construction,
already mostly paid for; caps are untouched (the 29/30-lot Red Rocks
ladders go to 44/45 against a 150 position cap).

Tests: the near-cliff test now runs a plain control first, then checks
the armed market rests exactly x1.5 the control's rung sizes, is flagged
near_cliff_boost in the selection, that the estimator's number rises from
the refresh after arming (bounded by 2x -- the share model steps at the
reference depth, so it is not a linear bound), that crossing the cliff
(banked 1.05 on the 1e9 scale) ends the mode and the next cycle re-places
the plain sizes, and that NEAR_CLIFF_SIZE_MULT=1.0 arms but does not
resize. 630 green.

## 2026-09-26 pm — Near-cliff size mode only past $0.50 banked (Jack)

Jack, on the size mode: "only do this if banked is > $0.50" (strict).

CHANGE (NEAR_CLIFF_BOOST_MIN_BANKED = 0.50, env IMM_NEAR_CLIFF_BOOST_MIN_
BANKED). The quote-to-completion verdict is unchanged (banked >= half the
cliff and projected within $0.15 under it). The x1.5 SIZE mode now arms
only when, in addition, the banked accrual is strictly above
NEAR_CLIFF_BOOST_MIN_BANKED and the size knob is on; a near-cliff market
with $0.50 or less banked keeps quoting at plain size (log: "quoting to
completion at plain size (banked <= $0.50)"), and a market at exactly
$0.50 is admitted but not resized. Because banked accrual only grows
while a market quotes, the gate is checked at arming time; a market that
crosses $0.50 later arms at the next refresh's verdict (the log fires
again when it arms). With the knob off nothing arms (the earlier build
armed and sized 1.0). Banner names the gate; knob in the config hash.

Tests: at the gate exactly (banked = gate, moved to $0.90e9 on the test
scale so the verdict still fires) the market is admitted with near_cliff
set, no size mode, plain rung sizes; a cent over the gate arms and rests
x1.5; knob-off admits without arming. 630 green.

VERIFIED (near-cliff size mode + banked gate, bacc225): relaunch banner
17:14:38Z, first refresh 17:16:58Z, 9 near-cliff verdicts all "at x1.5
size until it crosses" (every one had > $0.50 banked: DETJGIBBS0 $0.95,
CBDTHAILAND C25 $0.83, TXOIL T5.9 $0.60, FSLR 4200/4100 $0.71, Red Rocks
LOR $0.62 / BLU $0.55 / NOA $0.66 / CHR $0.70). Red Rocks placed 17:19:10Z
(third cycle, placement cap): ASK 45 (= 30 x 1.5) on all four; BID 28-29,
unchanged, because the bid side was already room-limited at 29 before the
boost (cycle_log room_buy 29 / room_sell 30 -- inventory skew and the
event cap bind the buy side; caps are deliberately untouched). Selected
416, no evictions.

## 2026-09-26 pm — Sub-penny books: floor/ceil cent buckets so a sub-cent spread survives as a quotable 1c spread (Jack)

Jack, after "why are sub-penny books a blindspot?" -- the whole bot
reasons in whole cents and orderbook_levels ROUNDED every price to the
penny, so a 0.1940 bid / 0.1960 ask escalator read 19 / 19: no integer bid
can rest below the ask, no ladder, est $0.00, zero_yield (157-160 skips
per refresh, nearly all NFL escalators), and the sub-penny join can only
move a rung that was already built. "yes" to fixing it.

CHANGE (no knob: it is a representation fix; whole-cent books are
bit-for-bit unchanged). The whole-cent view of a book is now FLOOR for
bids and CEIL for asks: orderbook_levels floors each book's prices in its
own terms (a YES bid down, a NO bid down = the YES ask up), and MERGES
levels that share a cent (sizes summed -- the share model's integer keys
used to keep only the last of them); order_yes_book_cents buckets a
resting bid's YES price down and an ask's up (or its NO price down);
market_cents floors a *_bid and ceils a *_ask (mid / spread for the pass-1
screen); subpenny_snap uses the same buckets (bid level -> floor, ask level
-> ceil). Consequences: the integer touch is never better than the true
touch, so an integer rung never crosses the true book; any sub-cent spread
becomes a >= 1c integer spread, so the ordinary pipeline (sides_can_
qualify, reference prices, build_side_ladder, the estimator's probe
ladder, the quote loop) treats 0.1940 / 0.1960 as 19 / 20, builds 19 / 20,
and subpenny_snap rests them at 19.40 / 19.60 exactly; a truly LOCKED
exact book (bid == ask) still reads floor / ceil around it and the snap
refuses both sides (crossing guard), so the integer rungs stay one cent
outside the lock and never cross. Helpers _floor_cents / _ceil_cents
(round to 4 dp first: 0.29 * 100 is 28.999...). The reward share is scored
on the merged cent buckets: our exact-touch order shares the maker's
cent, which is what Kalshi's exact walk pays when we sit on the maker's
level; the decay term still counts whole cents, so a level a fraction of
a cent behind the reference is scored as at it -- an over-estimate of at
most one decay step, acceptable until Kalshi's tick definition for
0.0001-step markets is confirmed.

What this opens: the escalators that read zero_yield only because their
spread was sub-cent (the estimator now sees a 1c spread; the payout floor,
the band and the event caps still apply). What it does not change: the
integer rungs' size, caps, bands, pads (a 1c pad is a valid price on the
0.0001 grid), fills accounting (a 0.1915 fill still books as 19c in the
P&L tracker -- pre-existing 0.15c imprecision, separate fix).

Tests: TestSubPennyCentBuckets -- floor/ceil helpers incl. the 0.29 float
guard; a sub-cent book floors, ceils and merges (DETASTBROWN14 shape ->
[[19, 13332.43]] / [[77, 1359], [80, 10030]] -> external best 19 / 20);
whole-cent books unchanged; resting orders and market objects use the
same buckets; end to end on a 0.1940 / 0.1960 escalator: external best
19 / 20, build_side_ladder builds 19 / 20, subpenny_snap rests 19.40 /
19.60, the estimator in the live (atref) ladder mode reads two quotable
sides with a positive estimate, and a locked 0.1950 book snaps neither
side. 632 green.

VERIFIED 17:30Z (deploy f55414a 17:24Z, clean exit 17:26:30Z, relaunch
17:27:37Z, first refresh 17:30:06Z): zero_yield 161 -> 1 (the one left is
KXAAAGASMAXM, an 11c spread, a different reason). Of the 160 former
zero-yield escalators, 38 selected (floor $/day p50 $0.70, max $1.63, all
now read a 1c integer spread) and 122 payout_floor (p50 $0.57/day on a
one-day window against the $1.50 fresh bar -- the floor, not the book,
keeps them out now). Selected 416 -> 455 across 132 -> 145 events,
collateral ~$11.7k -> ~$13.2k. Exchange probe ~17:45Z: 250 resting orders
on ladder / escalator markets (98 this morning), 250 of 250 AT the exact
external touch, 0 behind; escalators rest at both touches of one-sub-tick
markets (LARDADAMS17 0.1280 / 0.1281, SFCMCCAFFREY23 0.0799 / 0.0800,
DETASTBROWN14 0.1915 / 0.1926), 136 whole-cent rungs on the penny-grid
ladders. Watch: these one-sub-tick books put our 60-lots at the maker's
own touch on both sides, so fill risk on the escalators is now the
maker's, not a penny behind it; the first Sunday of settled escalator
fills (9/27) is the measurement.

## 2026-09-26 pm — Ladders / escalators: x3 size (90 on a Saturday) and a $1.20 fresh-entry bar (Jack)

Jack, once the sub-penny buckets had the escalators resting at the exact
touch: "3x ESCALATOR/LADDER instead of 2x'ing. so should be 90. and lower
entry floor to 1.2 for ESCALATOR/LADDER".

CHANGE (archetype KXNFLLADDERREC, cloned onto every pattern sibling):
size_mult 2.0 -> 3.0 (env IMM_SPORTS_LADDER_SIZE_MULT): 20 x 3 = 60 on a
weekday, x1.5 Saturday = 90 (the number Jack named), quiet hours x2 on top
(120 / 180), near-cliff size mode x1.5 on top of that; per-market cap 450
and per-event cap 3,000 follow through applied_mention_mult; the
estimator's hypothetical ladder and so the payout-floor projection see x3.
min_est_total = 1.20 (env IMM_SPORTS_LADDER_MIN_EST_TOTAL): the family's
FRESH-entry bar (series_min_est_total, read by floor_bar_dollars for a
candidate with nothing banked); members and banked re-entrants keep the
$1.00 cliff, the near-cliff rule keeps its $0.15 margin. Every other
series stays at $1.50 -- the 9/12 "same bar everywhere" test now carves
out exactly the SPORTS_LADDER_LEAGUE_RE family.

Expect: at the 17:30Z refresh 122 escalators were payout_floor at a p50
projection of $0.57/day on a one-day window against $1.50; x1.5 more size
(3 vs 2) and a $1.20 bar should admit a good part of the top half. Collateral
rises with it (~$13.2k ladder collateral before this; the $1,000-per-market
COLLATERAL_BUDGET check and the inventory reserve are unchanged).

Tests: the allowlist test reads x3 rungs / caps and the $1.20 bar for a
fresh candidate, the cliff for banked, $1.50 for an ordinary series; the
same-bar invariant test admits only this family under $1.50. 632 green.

VERIFIED 17:59Z (deploy e46ed31 17:54Z, relaunch 17:56:28Z, first refresh
17:58:58Z): ladder / escalator rungs place at exactly 90 (274 of 274 in
the first wave: 20 x 3 x 1.5 Saturday); selected 455 -> 510 across 145 ->
153 events, 42 ladder / escalator markets flipped payout_floor ->
selected on the $1.20 bar and the x3 projection (family: 125 -> 167
selected, 341 -> 299 floored), payout_floor 930 -> 875 overall, zero_yield
still 1; ladder collateral ~$13.2k -> ~$18.3k against the $15.2k inventory
reserve. The two near-cliff escalators re-armed at x1.5 on top (135).

## 2026-09-26 pm — Saturday x2, gated: the Monday tracker decides once from the boosted Saturdays (Jack)

Jack, after the 1.5 -> 2.0 read (9/19 plus the morning of 9/26; long-dated
net per resting contract-hour by block against weekdays, ex-KXRT: Saturday
10-23 ET 30.6 vs 11.4, Saturday 0-9 ET 33.0 vs 13.8 cents per 1k ct-h;
modelled share ~10%, no saturation): "yes, 2x next saturday if today +
prior saturdays show no sign of edge degradation".

MECHANISM -- one decision, made by a script, read by the bot, armed:
- `imm_saturday_tracker.py` scores every boosted Saturday -- 9/12 from
  10:00 ET (the x1.5 went live ~09:10 ET, so its 0-9 block is not an
  observation), 9/19, 9/26 -- per ET block (0-9 / 10-23) against the same
  block on the weekdays of its own Mon-Fri (all weekdays in the window
  when its week has fewer than two). Net = modelled rent minus mark-out
  cost, cents per 1,000 resting contract-hours. The mark is the 24h
  cycle-log mid, else the settlement value, else the last logged mid inside
  24h: the strict 24h mid (the report's mo24 column, unchanged) drops
  12-62% of long-dated fills a day, mostly the ones nearest resolution
  (KXTRUMPMENTION 59% missing, 99% of those settled) -- exactly the lumpy
  losses a gate has to see. Degradation = any of:
    G1 a boosted block nets less than the same block on its weekdays;
    G2 the Saturday's boosted hours net <= 0;
    G3 its fills mark out more than 2c/contract worse than its weekdays';
    G4 pooled over the boosted Saturdays, rent per fill minus settlement
       loss per settled fill < 0 (judged from 500 settled contracts).
  Every boosted Saturday must be a full day + 24h old, else PENDING and
  nothing is written. The SCHEDULED run ("KL imm saturday-tracker", Monday
  07:40 ET, no flags) writes run-logs/incentive-mm/sat_mult_gate.json ONCE
  -- {"verdict": "PASS"|"FAIL", "mult": 2.0, "effective_from": <the first
  Saturday after the run>, "checks": [...]} -- and never overwrites it;
  `--gate-rewrite` re-evaluates and rewrites by hand. The email carries a
  "2x gate" section (status, per-block rates, every check, a who-moved-it
  family table for the newest Saturday); the subject names the verdict on
  the run that writes it. A gate exception never costs the weekly email
  (status ERROR, nothing written).
- `incentive_mm.py`: `saturday_size_mult` returns `gated_sat_mult` --
  min(IMM_SAT_SIZE_MULT_GATED, the file's mult), code default 2.0 -- on
  Saturdays on/after effective_from when the verdict is PASS; anything
  else (FAIL, no file, unreadable, a mult not above SAT_SIZE_MULT) keeps
  x1.5, fail closed. Same exclusions; composes with the quiet hours
  (Saturday 0-9 ET = x4 on the global ladder, where TOTAL_SIZE_MULT_CAP
  leaves the at-ref depth mult x1.25); the payout-floor projection sees
  the step because size_mult_profile samples hour_size_mult. The file is
  read at import and by mtime every refresh (`load_sat_gate`, next to
  load_family_verdicts), logged `[IMM] Saturday gate: ...` on change and
  in the banner. SAT_SIZE_MULT_GATED joins the config hash; the verdict is
  runtime state, so a PASS shows in the cycle log's hour_mult (2.0 on
  Saturday long-dated rows outside 0-9 ET) and the tracker's mult column.

KILL SWITCHES: hand-edit the file's verdict to anything but PASS (instant,
no restart; the tracker never overwrites an existing file);
IMM_SAT_SIZE_MULT_GATED=0 in the launcher (task-level restart);
IMM_SAT_SIZE_MULT=1.0 turns the whole Saturday multiplier off. Deleting
the file RE-ARMS the gate: the next Monday run evaluates again.

WHAT THIS DOES NOT FIX / WATCH:
- 9/26 afternoon changed the Saturday book under the gate. Ladders x3
  (13:53 ET) put the sports ladders/escalators at 90/side: 49% of
  long-dated resting contracts 10-20 ET, earning 16.5c per 1k ct-h (our
  share ~0.5% of ~$113/day pools -- crowded, not saturated) against ~47c
  for the rest of the book. On partial marks at 20:40 ET the 9/26 10-23
  block was tracking just under its weekdays (21.0 vs 23.4). A G1 FAIL
  there is the rule working: 2x would mostly double down on the ladder
  book. A PASS takes ladders to 120/side on Saturday daytime (240 in 0-9
  ET, x1.5 more in near-cliff mode; per-market cap 450), above the 90 Jack
  named for the x3.
- One decision. Afterwards the tracker keeps re-checking every Monday
  (labelled "re-check only") but nothing reverts a PASS automatically.
- Modelled rent (pre-realization), not credits; mark-outs, not P&L.

Tests: TestSaturdayGatedStepUp (file-armed; ET day edges on 10/3;
exclusions and the x4 composition; fail closed on FAIL / garbage / a mult
not above base; the step is the smaller of file and env; both kill
switches; deleting clears; the mtime cache; the floor profile sees x2;
ASCII summary). GateEvaluateTests / GateVerdictFileTests / GateInputsTests
/ GateRenderTests in test_imm_saturday_tracker (G1-G4, pending until
Saturday + 24h, only boosted hours judged, every Saturday must pass,
one-shot write, logged-hours-only fills, the mark fallback chain, ASCII
text part). Both test modules point the bot at a never-existing verdict
name so no test reads the box's live file. 673 green (+87 in the other
IMM suites).

VERIFIED 2026-09-27 01:16Z (3b2657a synced 00:55:37Z, riding the same
relaunch as 7576bf9): banner `[IMM] Saturday gate: step-up x2 awaiting the
tracker's verdict (no sat_mult_gate.json); Saturday stays x1.5`. The
00:59:01Z relaunch's first cycle ran 605s -- ESPN scoreboard SSL retries
(~75s each) resolving NFL escalator kickoffs on a cold start -- and the
600s hang watchdog hard-exited it at 01:09:12Z (code 86); the 01:10:03Z
relaunch refreshed in 4 min (ESPN back: 200 in 0.3s), 454 selected across
144 events. Cycle log: config_hash 246f70c9 -> b4486d0b; long-dated quoted
rows at hour_mult 1.5 (399 of 404), dailies 0.5 -- unchanged, as it must
be before a verdict. The step-up costs ~1s a refresh (3,130 fourteen-day
size_mult_profile walks: 22.8s vs 21.0s). Tracker preview (print mode):
9/12 and 9/19 pass G1-G3 (9/19 0-9 ET 51.1 vs 26.1, 10-23 ET 25.7 vs 11.6
c per 1k ct-h; mark-outs -1.61c vs -3.81c), G4 +7.9c on 2,319 settled
contracts; 9/26 pending until Monday.

## 2026-09-26 pm — KXTRUMPAPPROVE allowlisted (out 07:00 ET on settlement day); hopeless clock 30 min on the live projection (Jack)

KXTRUMPAPPROVE. Jack: "yes allowlist it, and stop it at 7:00 ET on
settlement day". RCP "RCP Average" Approve value at exactly 1:00 PM ET,
nine 0.1-point strikes, listed 10:01 ET the day before, $600/market per
24h program (10:02 -> 10:02 ET). New politics group
_DEFAULT_POLITICS_SERIES (env IMM_ALLOW_POLITICS_SERIES) plus
SERIES_OVERRIDES["KXTRUMPAPPROVE"] event_day_cutoff_et=(7, 0) (env
IMM_TRUMPAPPROVE_CUTOFF_HOUR_ET / _MIN_ET). Occurrence 16:59Z sits one
minute before the 17:00Z expected expiration, so the extender's 07:00 ET
is the cutoff: 11:00Z in EDT, 12:00Z in EST. Basis, the public tape of 14
daily events 9/13-9/26 (54k trades): 21 re-pricings between 07:00 and
12:59 ET cost resting orders ~$10.5k on 51k contracts, takers leading the
visible move by 3-6 minutes; the 7 overnight/evening re-pricings netted
makers +$40.

LIVE (config 19367f4e, first refresh 00:59Z 9/27): all nine 26SEP27
strikes in the universe at $600/day and screened `manual`. The account
holds a hand book in that event: 11 resting orders with no
client_order_id (none in imm_order_journal / our_order_ids; every bot on
the account tags its orders), positions E38.6 -200, E38.7 +95, E38.8
+137, $643 cost. Yield-to-human is event-level, so the bot quotes only
KXTRUMPAPPROVE events with no manual footprint.

HOPELESS CLOCK. Jack, on KXNFLFFPTSLADDER-26SEP27MINTB-MINKMURRAY1:
"make the hopeless clock more consistent / faster. what about 30min
checks instead of hourly?". HOPELESS_SUSTAIN_SECS 3600 -> 1800, and
MEMBER_PEAK_GUARD (env IMM_MEMBER_PEAK_GUARD, default 0): a member is
judged on banked + CURRENT remaining estimate, the test fresh candidates
have had since 9/13, not max(current, 1h est peak). The peak re-seeded
from the single reading on the first refresh after it expired, so one
thin-book moment bought another hour. MURRAY1: admitted 21:32Z on a
10-minute thin-book reading, peak to 22:42Z, re-seeded inside a 20-minute
blip, clock started 23:43Z, evicted 00:51Z under the old rule; -135
filled, $0.11 banked. 9/6-9/26: 1,826 admit -> hopeless rides, median
2.8h, p25 2.07h (the structural 2h floor), 13% filled, 12,206 contracts.
The exit now lands 30-40 minutes after the last above-bar live reading.
_est_peak is still tracked and persisted; near-cliff is unchanged.

LIVE: the new process's first refresh (started 00:59:08Z, finished
01:09Z, slow on two transient ESPN SSL failures that fell back to
midnight-ET) evicted 10 hopeless members (NFL ladders / escalators and
one KXRT strike) already 30+ minutes under the bar. The members left on
long clocks are exempt by design (KXRAIN curated tier, finecon
KXCBD* / KXSPRLVL, forced KXFSLR-26OCTMWSOLD).

KILL SWITCHES (launcher env, task-level restart): IMM_ALLOW_POLITICS_SERIES=""
drops the allow; IMM_TRUMPAPPROVE_CUTOFF_HOUR_ET / _MIN_ET move the
cutoff; IMM_HOPELESS_SUSTAIN_SECS=3600 restores the hour;
IMM_MEMBER_PEAK_GUARD=1 restores the member peak carry.

WHAT THIS DOES NOT FIX / WATCH:
- Admission on a one-refresh blip still happens: MURRAY1 was admitted on
  a 10-minute thin-book reading and filled 90 lots 11 minutes later. A
  two-consecutive-refresh entry test would stop that; suggested, not built.
- KXTRUMPAPPROVE inventory taken overnight still rides through the
  morning update to the 1:00 PM snapshot; quiet-hour size applies up to
  07:00 (the overnight flow measured benign).
- A hand book on an approval event keeps the bot out of that whole event.

Tests: TestTrumpApproveAllowlist (allowed, exact series; cutoff 11:00Z
EDT / 12:00Z EST; the series tighteners leave it);
test_sustain_window_is_30_minutes_by_default;
test_member_exit_runs_on_the_live_projection_not_the_peak; the three
member-peak tests now pin the default AND the knob. 636 green at 7576bf9.

## 2026-09-26 pm — Carbon Arc fair-value gate: quoting reads Carbon Arc's month-to-date index (Jack, with Carbon Arc's consent)

Jack, after the data-subscription review and with Carbon Arc's written
consent: "use Carbon Arc's data when quoting".

WHY. Every Carbon Arc contract settles on the FIRST monthly value Carbon Arc
reports. Carbon Arc's Prisms feed publishes the month-to-date (MTD) value of
the same index daily, 2-6 days lagged (POS ~2, card ~3, app ~5, foot
traffic / ads ~6), and that read is what the book trades on: on 9/26 the
market-implied median sat within ~0.5 index points of the MTD value on card
spend, POS and foot traffic. Our fills 9/06-9/26 marked out -7.8c/contract
(bids -14.6c, asks 0.0) -- we were the stale quote when the read moved.

MODEL (carbon_arc_fair.py, new): per series and measurement month,
first print ~ N(mu, sigma) with mu = latest MTD (+ IMM_CA_DRIFT_PTS, 0),
sigma = 1.7 * sqrt(((1-w) * sigma_m)^2 + floor^2), w = observed days / days
in month, sigma_m = stdev of the entity's month-over-month changes
(hist_yoy, complete months only), floor = max(1.0, 1% of mu). The 1.7 is the
median market-implied / model sigma ratio on the 9/26 books (69 series:
card 1.59-1.71, POS 1.51, FT 1.77, ads 1.76, apps 1.56). Series -> (prism,
entity) comes from each series' Kalshi settlement-source URL, read from the
PUBLIC GET /series catalog (no auth) once a day. No entry before 10% of the
month is observed or when the read is 10+ days old. Every new read is
appended to run-logs/incentive-mm/carbon_arc_vintages.jsonl for calibration
against the prints.

GATE (incentive_mm.py, the rain gate's shape): quotes still join the touch
unchanged (safe-join for these families); a Carbon Arc-settled market
(carbon_arc_settled verdict) stands aside on BOTH sides while
- its read MOVED within IMM_CA_FAIR_REFRESH_HOLD_MIN (10) minutes (the book
  is repricing; not on the first load after a restart), or
- its external touch fights the fair BAND on the adverse side: bid touch >
  hi + tol or ask touch < lo - tol, tol IMM_CA_FAIR_TOL_CENTS (15), band =
  P(first print > K) at sigma and at sigma x IMM_CA_FAIR_SIGMA_LO_FRAC (0.5).
No gate when the read is too noisy to overrule the book (sigma > 20% of mu,
IMM_CA_FAIR_MAX_REL_SIGMA: Eli Lilly's pharmacy index, most app and ad
series), when the entry is stale (IMM_CA_FAIR_TTL_MIN 60), or for another
month's read -- plain quoting, exactly as before. Strikes read on the index;
a negative strike reads as growth percent (the August POS ladders, T-3 =
index 97). Sticky selection keeps a stood-aside market; logs
"ca-fair stand-aside <t>: <why>" / "ca-fair resume <t>" once per episode and
"ca-fair reloaded: N series, M with a new read" when reads move. The
late-month rule (CA_LATE_*: no bids from the 17th, out from the 26th) is
untouched and applies on top.

DRY RUN on the 9/26 books (909 September strikes, today's reads): 16 stand
asides (1.8%), every one a 1-3 index-point book-vs-read disagreement near
the money (9 POS asks where the book prices the print under the MTD read,
ANF / COST / SBUX / URBN card strikes). Without the band the same run
fired 75 times, mostly on wing strikes the book prices tighter than the
model (Amazon's history swings with Prime Day timing); a nearer-reading
growth-percent rule flipped KXGROKAPP / KXAMUSEMENTADS strikes and was
dropped.

FEED -- NOT CONFIGURED AT DEPLOY. The refresher thread (ca-fair, every
IMM_CA_FAIR_REFRESH_SECS = 120s, all I/O off the trading thread) reads the
feed URL from IMM_CA_FEED_URL (+ IMM_CA_FEED_TOKEN as a Bearer token) or from
~/.carbonarc_feed.json {"url": ..., "token": ...} -- OUTSIDE the repo,
because the repo is public. The settings are re-read every 10 minutes, so
adding the file needs no restart. The URL must return the Prisms JSON shape
({"prisms": [{"prism_id", "category", "data_through", "last_refreshed_at",
"entities": [{"entity_name", "mtd_yoy": [...], "hist_yoy": [...]}]}]}).
Until it is set the bot logs "ca-fair: no feed configured -- gate open"
once and quotes exactly as before. IMM_CA_FEED_URL is redacted in the
config snapshot (FEED_URL joined the secret-name list).

FEED UPDATE (same evening): Jack opened a Carbon Arc Professional account
($20/mo). A TOKEN alone now works -- {"token": "..."} in
~/.carbonarc_feed.json (or IMM_CA_FEED_TOKEN), from app.carbonarc.ai ->
Developers. It reads the official Prisms API that the carbonarc SDK's
client.prisms wraps (Carbon-Arc/carbonarc src/carbonarc/prisms.py): GET
https://api.carbonarc.co/v2/prisms?insight_id=<id> per insight, falling
back to /v2/prisms/<prism_id>, header "Authorization: Bearer <token>";
per the SDK, any valid token may read prisms with no entitlement and no
cost per call. The prism -> insight map is learned on the first sweep and
kept in the fair file (prism_insights), so a steady sweep is ~6 calls,
not ~40. Same payload shape as the Prisms page. The config file may carry
a Notepad BOM (read as utf-8-sig).

KILL SWITCH: IMM_CA_FAIR_ENABLE=0 (launcher env, task restart), or delete
the feed config (the gate opens when entries pass their 60-minute TTL).

WHAT THIS DOES NOT FIX / WATCH:
- The informed flow reacts within seconds of a Carbon Arc refresh; we see
  it after up to 120s + one cycle. The hold covers the repricing minutes,
  not the first fill.
- The MTD read vs FIRST print gap is unmeasured (drift = 0). August's card
  values were later revised up a median 3.4 points over the first print, so
  never use revised hist_yoy levels; calibrate drift / sigma per category
  from carbon_arc_vintages.jsonl against the Oct 3-8 prints.
- The late-month stand-down is unchanged: lifting it on the strength of the
  data is Jack's call after the October prints.
- A stood-aside market keeps its seat and its lifetime event slot.

Tests: TestCarbonArcFairGate (measurement month, strike scale, lookup / TTL
/ wrong month, reload counting, strict per-side breach, band + noise cap,
refresh hold incl. first load and unchanged rewrites, stand-aside -> hold
-> resume end to end, ask-side stand-aside, stale / disabled / non-Carbon
Arc quote plainly) and test_carbon_arc_fair.py (catalog map, month spread,
entry math, thin / stale refusals, case-insensitive entity match, file +
vintage writes, series-map reuse and failure, feed settings env vs home
file, bearer header + payload shape). Guard-skip sink: the stand-aside is
guard "ca_fair" in guard_skips_*.jsonl (inputs: hold_min, or fair / lo / hi /
tol / bid_bad / ask_bad), so the sweep test counts 24 continues. 820 green
after the rebase onto the data-capture merge.

## 2026-09-26 late — KXTRUMPAPPROVE x3 family size (Jack)

Jack: "give KXTRUMPAPPROVE markets a 3x multiplier, like
LADDER/ESCALATOR". size_mult=3.0 on SERIES_OVERRIDES["KXTRUMPAPPROVE"]
(env IMM_TRUMPAPPROVE_SIZE_MULT; 1.0 reverts) -- the ladders' wire, so
applied_mention_mult scales the rungs, the per-market / per-event caps,
the skew knees and the estimator's ladder (the payout-floor projection)
together. Size only: not the ladders' 1-99c band or $1.20 entry bar.
Checked against the committed code with the launcher's $ProbeEnv applied:
60 on a weekday, 90 Saturday, 120 in quiet hours up to the 07:00 ET
cutoff, 180 on a Saturday settlement morning (Saturday x quiet hours);
caps 450 per market / 3,000 net per event (were 150 / 1,000).

Deploy 8b2bc35, relaunch 03:47:51Z (run c0b1c656). Not yet seen in live
orders: 26SEP27 is still yielded to the hand book (manual); the first x3
quotes come on 26SEP28 (lists 10:01 ET 9/27) if it has no hand orders.

WATCH: x3 triples what can ride through the settlement-morning RCP
update -- up to 450 contracts per strike, 3,000 net per event.

Tests: test_x3_family_size_like_the_ladders (mult 3.0; caps x3; day
ladder and weekday ladder exactly x3; band / entry bar not copied;
cutoff kept). 694 green.

## 2026-09-27 — OpenRouter token-usage gate: KXTOKENUSE / KXTOKENUSEM allowlisted, quoted only against OpenRouter's own daily totals (Jack)

Jack: "yes build the OpenRouter token usage gate".

WHY THIS FAMILY. KXTOKENUSE ("OpenRouter total token usage for Sep 21-27 above
164T") and KXTOKENUSEM (the same over a 4-week "month": "September 2026
(measured August 31 - September 27)") settle on OpenRouter's rankings summed
over UTC days, read 10:00 AM ET the Monday after. That is a live public feed,
so the open-scan tier rejects them on the `openrouter` keyword. But OpenRouter
publishes the settling data itself: the official Data API
(openrouter.ai/docs/cookbook/administration/data-api, CC BY 4.0, commercial
use with attribution, any OpenRouter key, 30 req/min + 500 req/day), endpoint
/api/v1/datasets/rankings-daily = 51 rows per COMPLETED UTC day (top 50 +
"other"). The Monday-Sunday sums reproduce all five settled weeks:
93.38 -> 93.4T, 113.00 -> 113, 115.46 -> 115T, 126.76 -> 127T, 128.90 -> 129T.
Pool ~$15k/30d at $200/strike/week; the books are thin and wide (T144 38x92).

MODEL (openrouter_fair.py, new). Per event window: mu = known days + each
remaining day at base x weekday factor x half the recent weekly growth, plus
a measured bias; sigma = 1.2 x measured RMSE. base = mean of the last 7
completed days; weekday factor = trailing 4 weeks; growth = last 7 / prior 7
(clipped -10%..+20%) at weight 0.5. RMSE / BIAS in units of one day's volume,
MEASURED by a backtest over 2026-02..09 (63 week + 30 four-week windows): one
day left 0.09 / +0.05, seven left 0.82 / +0.31, twenty-eight left 7.07 / +3.52
(usage has been growing, so the plain forecast runs low; the full-trend
variant overshot). Event windows are parsed from the open markets' own rules
text on Kalshi's public API (the "month" is NOT a calendar month); a weekly
event whose text does not parse falls back to the 7 days before its ticker
date, a monthly one gets no entry. Writes run-logs/incentive-mm/
openrouter_fair.json per EVENT (mu, sigma in T, known / days, complete) and
appends each newly published day to openrouter_vintages.jsonl. Dry run 9/27
05Z: week of Sep 21 at 6/7 days -> mu 144.5T sigma 2.3 (book T144 38x92,
T146 14x45); the Aug 31-Sep 27 month -> 515.7T (book T500 84 bid, T525 no bid).

ALLOWLIST + CAP. _DEFAULT_AI_USAGE_SERIES = "KXTOKENUSE,KXTOKENUSEM" joins
ALLOW_SERIES (IMM_ALLOW_AI_USAGE_SERIES="" removes it); EVENT_TOP_N gains
=KXTOKENUSE:3,=KXTOKENUSEM:3 (exact names: KXTOKENUSE prefixes KXTOKENUSEM
and KXTOKENUSED) -- every strike of an event settles on one total, the same
3-highest-ROI rule as the other one-number families.

GATE (the Carbon Arc gate's shape, but fail CLOSED -- these series are
allowed only because the feed exists). A KXTOKENUSE/KXTOKENUSEM market stands
aside on BOTH sides while: its event has no fresh read (no file, no key,
older than IMM_OR_FAIR_TTL_MIN 60, unparseable window); the window is
complete (the last day is published just after 00:00Z Monday, ~4h before the
03:59Z close -- everyone with the API knows the answer); its read moved
within IMM_OR_FAIR_REFRESH_HOLD_MIN (10) minutes (a new day published; not
on the first load); or its external touch fights the fair band (P at sigma
and at sigma x 0.5) on the adverse side by more than IMM_OR_FAIR_TOL_CENTS
(15). Quotes still join the touch unchanged. Logs "or-fair stand-aside <t>:
<why>" / "or-fair resume <t>" once per episode, "or-fair refresh: N events
with a read" when that changes, guard "or_fair" in guard_skips_*.jsonl
(the sweep test now counts 25 continues). The refresher thread polls every
IMM_OR_FAIR_REFRESH_SECS (600) and every IMM_OR_FAIR_FAST_SECS (120) for
IMM_OR_FAIR_FAST_WINDOW_MIN (45) after 00:00Z, ~170 of the 500 daily
requests.

KEY. ~/.openrouter_key.json {"key": "sk-or-..."} (or IMM_OR_API_KEY), outside
the public repo, read at call time (no restart to add it); never logged; the
config snapshot redacts env names containing KEY. No key = the family stands
aside.

KILL SWITCHES: IMM_ALLOW_AI_USAGE_SERIES="" (take the series out);
IMM_OR_FAIR_ENABLE=0 turns the gate OFF (plain quoting -- only for testing).

WHAT THIS DOES NOT FIX / WATCH:
- The current UTC day is invisible to the API until it completes; anyone with
  intraday numbers (the site's live chart) still sees the last day forming.
- The bias table is a growth era's; if usage growth stalls, mu runs high --
  recheck against openrouter_vintages.jsonl and the settlements.
- Rounding: Kalshi compares against the displayed total (e.g. "129T"); a
  total within 0.5T of a strike can round across it. sigma covers it except
  on the final day, when the gate is already out (complete).

Tests: TestOpenRouterFairGate (allowlist + exact caps, fail-closed / stale /
complete / hold / band reasons, quote-then-stand-aside end to end, complete
and stale stand aside, gate-off plain quoting) and test_openrouter_fair.py
(window parsing incl. en dash, cross-month and year boundary, ticker-date
fallback, flat / complete / thin-history / growth model cases, file +
vintage writes, no-key, key sources incl. a Notepad BOM, Bearer fetch).
840 green.

## 2026-09-27 — GasBuddy state-gas gate: the AAA state dailies are back, quoted only against GasBuddy's live state averages (Jack)

Jack: "i received permission to use fuelinsights.gasbuddy.com. use that".

WHY THIS FAMILY. The AAA state dailies (KXAAAGASD<ST>, 26 states, 17 strikes
$0.005 apart, ~$100 per strike-period, ~$44k/day posted) were pattern-blocked
2026-09-14: Sep 6-11 they ran -$39.64/day net, fills -10c/ct, because
anything that trades against our rung inside a 45c spread is informed -- the
informed flow reads intraday station prices (OPIS is AAA's supplier;
GasBuddy the crowd feed). The 9/27 replay showed a fair from the morning AAA
print + momentum could not separate those fills. GasBuddy's Fuel Insights
site (Jack has GasBuddy's permission) publishes exactly the missing input:
  - Live Ticking Average per state = average of the last price received at
    each station over the past 24h, refreshed every 5 minutes (observed
    10:45 -> 10:50 ET);
  - Full Day Average = average of each station's LAST price on the day,
    published 03:00 ET the next morning -- the same hour AAA posts. The
    "1 Day Ago" figure and the charts are these.

TIMING. Event D trades 08:00-23:59 ET on D-1 (open 12:00Z, close 03:59Z),
AAA posts D ~03:20 ET, Kalshi settles the state events ~07:06 ET (the
national ~09:10). So the previous event's settled value is the anchor, known
before the next event opens -- no AAA scraping.

MEASURED (Kalshi expiration_value, 26 states, Aug 24 - Sep 26, against
GasBuddy's daily chart): AAA(D) tracks GasBuddy's day D-1 -- correlation of
daily changes 0.94 (day D 0.68, D-2 0.42); AAA(D) - AAA(D-1) = 0.30c + 0.81 x
(GB(D-1) - GB(D-2)), residual 0.95c vs 3.34c for AAA carried forward (333
state-days). WEEKDAY structure (print weekday, residual; leave-one-date-out):
Mon b1 0.09 on Sunday's move + b2 0.47 on SATURDAY's, 0.41c (the Monday
print is Saturday's prices; the Sunday dip in GasBuddy never reaches AAA);
Thu 0.88 / 1.05c; Fri 0.80 / 0.83c; Sat 0.99 / 0.77c; Sun 0.63 / 0.30c;
Tue and Wed UNMEASURED (GasBuddy's chart has no Monday values). National
check (Mar-Sep): Mon beta 0.15, Thu-Sat ~0.8 -- same shape. Per-state bias
and residual scale shrunk to the pool (K=8): FL 1.70x, WA 1.40x, CA 1.26x,
NY 0.57x, TX 0.61x. Tails are fat: |z|>3 in 2.4% of state-days (normal 0.3%).

REPLAY (the 397 September state-daily fills, 7,832 ct, as-filled prices vs
the fair band; this measures separation, not the intraday feed): with
GasBuddy's END-OF-DAY value known at fill time, TOL 0 lets 2,607 ct through
at +0.3c/ct and blocks 3,182 at -15.2 (sigma x0.75: +4.9c/ct on 2,009 ct);
with the day's move assumed to show up LINEARLY through the day, what gets
through still loses -3 to -5c/ct. A wider tolerance only let losing fills
back in at every setting -> TOL 0. Unit economics at the Sep 6-11 measure
(credit ~$2.44 per market-period, 42 ct/market-day): positive in both bounds
IF rewards hold (MODELLED ~+$30/day linear to ~+$150/day end-of-day at 26
states x 3 strikes) -- the intraday curve decides which.

MODEL (gasbuddy_fair.py, new). While event D trades (ET day T = D-1):
  mu    = AAA(T) + alpha_w + alpha_s + b1_w x (live_T - GB(T-1))
                                     + b2_w x (GB(T-1) - GB(T-2))
  sigma = sqrt((e_w x scale_s)^2 + (b1_w x remain(t))^2)
remain(t) = sd of (Full Day Average - live at t): NOT MEASURED YET, default
linear from the trading weekday's sd of GasBuddy's daily move at midnight to
0 at 24:00 (Wednesday 5.3c: the Midwest price-cycle day). Reads per refresh
(IMM_GB_FAIR_REFRESH_SECS 300): GasBuddy HeatMap/GetMapData (every state's
live average, one call) + the national LiveAvg (update time + dates); once a
day per state: Kalshi's settled anchor (public markets endpoint, from 07:00
ET, 0.3s apart) and the state's 1 Day Ago average; Sundays also GB(Fri) from
the chart. States without a Kalshi event are re-asked every 6h (24 today).
No entry (-> the gate stands aside) without: today's anchor, a live update
younger than 30 min dated today, yesterday's average (GasBuddy's, else our
own last live read of yesterday, kept as "closes"), or on a Monday (the
Tuesday print is skipped: IMM_GB_SKIP_PRINT_WEEKDAYS=1). Writes
run-logs/incentive-mm/gasbuddy_fair.json per EVENT and, every ~15 minutes,
every state's live average to gasbuddy_live.jsonl -- the data remain(t) gets
calibrated from.

UNBLOCK. SERIES_BLOCK_PATTERNS' first entry is now
_series_block_default(): "KXAAAGASD[A-Z][A-Z][A-Z]+" while the gate is on
(the two-letter STATES come back; any longer suffix stays blocked) and the
9/14 "KXAAAGASD[A-Z]+" when IMM_GB_FAIR_ENABLE=0. The states are normal-book
members through the KXAAAGASD allow prefix and keep the national's guards
(safe-join, the $2/day rate floor, KXAAAGAS:3 per event, the 4pm-1am ET
halving, the day-dated midnight cutoff). The national KXAAAGASD, W and M
are untouched.

GATE (the OpenRouter gate's shape, fail CLOSED). A state market stands aside
on BOTH sides while its event has no fresh read (older than
IMM_GB_FAIR_TTL_MIN 20), its sigma exceeds IMM_GB_FAIR_MAX_SIGMA_CENTS
(2.0 -- too early in the day to call; Wednesdays until mid-afternoon on the
linear default), or its external touch fights the fair band (P at sigma and
at sigma x 0.5) on the adverse side by more than IMM_GB_FAIR_TOL_CENTS (0).
SELECTION: _screen returns "gb_no_read" / "gb_fair" (both sticky deaths) for
a state market with no read or a listed touch the gate rejects, so the 3
event slots go to strikes that can quote and a Monday or an outage frees
them (IMM_MAX_MARKETS 200). Logs "gb-fair stand-aside <t>: <why>" /
"gb-fair resume <t>", "gb-fair refresh: N events with a read", guard
"gb_fair" in guard_skips_*.jsonl (the sweep test now counts 26 continues),
startup "gb-fair gate: ...".

DRY RUN 9/27 ~15:25Z (Monday events): 26 of 26 states with a read, sigma
0.24-0.70c (a Sunday: only Saturday's known move matters); against the live
books 110 of 442 strikes quotable, 332 stood aside -- mostly sure strikes
whose touch sits on the wrong side (OR 5.07 at 2x87 with the fair at 100).

KILL SWITCH: IMM_GB_FAIR_ENABLE=0 -> gate off AND the state dailies
pattern-blocked again (the 9/14 state). Positions ride either way.

WHAT THIS DOES NOT FIX / WATCH:
- remain(t) is a guess until measured. The collector (session scratchpad
  gb_live.jsonl, 5-min, all states, to 10/4) and gasbuddy_live.jsonl give
  Full Day Average minus live by ET hour; recalibrate DAY_MOVE_SD / the
  curve and re-run the September replay with it before judging the gate.
- GasBuddy is not OPIS: the 0.95c residual is the informed flow's remaining
  edge on near-money strikes (end-of-day replay: fills within 5c of fair
  -7.2c/ct). TOL 0 + the 0.5-sigma band is the only defence there.
- Fat tails: a "sure" strike is ~98%, not 99.9%. sigma is plain normal.
- The Tuesday print (Monday trading) is skipped and the Wednesday print uses
  the pooled fit with a 1.2c residual -- both unmeasured.
- Settled state P&L: check fills_*.jsonl for KXAAAGASD<ST> daily (markout
  to the settled value) for the first week; the 9/14 block reason was fill
  cost, so that is the number that decides.

Tests: TestGasBuddyFairGate (family shape + both kill-switch pattern sets,
fail-closed / stale / vague / band reasons, strict tolerance, loader drops
bad rows, no read = no slot, a disagreeing member freed at the refresh, a
rejected fresh strike takes no slot, stale read, gate-off plain quoting),
test_gasbuddy_fair.py (19: ticker dates, ET clock, remain curve, weekday
math incl. the Monday b2 path, once-a-day anchors / absents / 6h re-ask,
before-07:00, stale / wrong-day live, close fallback, Monday skip, Sunday
history, pending / failing anchors, 15-min live log, fetch parsing); the
four 9/14 pattern tests now assert the kill-switch form. 798 green
(test_incentive_mm, test_gasbuddy_fair, test_carbon_arc_fair,
test_openrouter_fair, test_send_imm_new_programs).

### 2026-09-27 pm — live check + the state dailies' $2/day rate floor dropped (Jack)

Deployed 15:46Z (Jack's 15:39Z restart ran before the 11:45 ET sync, so it
reloaded the old code; restart_imm.ps1 re-run after the sync). First refresh
15:48Z: 26/26 events with a read. First selection with reads (15:58Z): of
442 state strikes, 335 rejected by the gate at selection (gb_fair), 42
one-sided, 10 extreme-mid, 61 rate_floor, 14 payout_floor -- NONE selected.
The accepted strikes' estimated share is ~0.4% of books holding 10-30k
contracts at the 20-lot size: est median $0.58/day per strike (p90 $1.68,
max $4.02) against the national KXAAAGASD $2/day rate floor they inherited.

Jack: "drop the $2/day floor". GB_STATE_MIN_RATE (IMM_GB_STATE_MIN_RATE,
default 0) replaces min_est_per_day when ensure_family_override clones the
national guard set onto a two-letter state (only while the gate is on);
every other guard is unchanged and the national keeps its $2/day bar.
IMM_GB_STATE_MIN_RATE=2 restores it. The $1-per-period payout floor still
decides entry, and over a 16h period with the 4pm-1am halving it needs about
$2/day of estimate as well: 4 of the 68 gate-passing two-sided strikes
cleared it on 9/27's books (17 at x2 size, 28 at x3, 43 at x4). Test:
test_state_clone_drops_only_the_rate_floor. 799 green.

## 2026-09-27 pm — PICK-OFF WINDOWS: the emails say when Kalshi's event start is LATER than the real event (Jack)

Jack: "when kalshi's datetimes are off, sometimes i can manually pick off
other traders who have their orders expire 'at event start'. be clear in the
emails when this is the case, with what time the event is and what time
kalshi thinks the event is."

Kalshi's "At event start" time-in-force pulls a resting order when the
scheduled event its market is tied to starts: the milestone feed (GET
/milestones start_date, e.g. "Nike Earnings Call"). New module imm_pickoff.py
sweeps every milestone starting after now (~6,000 rows, 13 pages, ~2s; ALL
types, because DELL's call was a one_off_milestone) and compares each start
with our own time: the event_start_overrides, else, for an earnings-mention
event with no override (no program, which is most of them: JPM, GS, BAC,
PEP...), the Nasdaq calendar.

A window = Kalshi's start later than the real event by more than a normal
release->call gap. After-close release or no hour: Kalshi after NOON the next
day (TOL's next-morning call stays quiet). Anything else: 6h+. Measured over
the 135 overrides with a milestone: LLY (+49.5h) and DELL (+48.5h) clear it,
nothing else does (-47h..+4.8h, TOL +16.5h). Replays flag LLY on Aug 5, BULL
on Aug 19 (Nasdaq-only; markets closed Aug 20 09:08 while Kalshi said Aug 27)
and DELL on Sep 1. CCL/NKE (9/27) are Kalshi EARLY: orders pulled before the
real call, nothing to pick off. Also required: a market still trading (Kalshi
closes them after the real call), real event in [now-3d, now+14d], gap <= 30d
(else it is next quarter's event).

Where it shows:
- 7:10 digest: ">> PICK-OFF WINDOW" block at the top (blue box in HTML),
  subject tagged. The red banner and the cutoff-audit rows now give Kalshi's
  event start next to ours, and each row says which way the gap cuts.
- 7:20 quotes-and-overrides: the same block atop OVERRIDES, subject tagged.
- imm_earnings_overrides.py (6:45/12:45/4:45): a NEW window emails at once
  ("IMM PICK-OFF WINDOW: <event>"), once per event + both times
  (run-logs/incentive-mm/pickoff_seen.json, marked only after the send
  succeeds). Kept out of overrides_last_runs.json action_lines so the 7:20
  email does not print it twice.

Read-only, never raises (a failure prints "PICK-OFF CHECK could not run").
Text is ASCII (cp1252 task console). Kill switch IMM_PICKOFF_ENABLE=0;
lookahead IMM_PICKOFF_LOOKAHEAD_DAYS (14). Blind spot: a same-day hour error
smaller than the bar, which the data cannot tell from a normal release->call
gap. Tests: test_imm_pickoff.

## 2026-09-27 — USGS earthquake gate: KXBIGGESTQUAKE allowlisted, YES bids only, priced off USGS + GFZ (Jack)

Jack: "yes build the USGS bid-only gate", then "also use GFZ data".

WHY THIS FAMILY. KXBIGGESTQUAKE-<DDMMMYY>-<K> ("If the highest USGS-reported
earthquake magnitude worldwide during Sep 27, 2026 from 12:00:00 AM through
11:59:59 PM UTC is 5.2 or higher") -- ten strikes 5.2..7.0 in 0.2 steps, one
event per UTC day, open 20:00Z the day before, close 23:59:59Z; program $50
per market per period, daily since 9/13 (~$429/day over the ten). Measured
9/27 (read-only study, session scratchpad vau/usgs/): all 204 settled
markets reproduce on the USGS-DISPLAYED daily maximum at Kalshi's
quarter-hour check (a crossed strike closes at the next :14/:29/:44/:59 and
settles YES at once; NO strikes wait days-weeks). USGS first publishes a
median 17.4 min after origin (M5.5+), GFZ GEOFON a median 5.3 min. On the 62
crossed strikes the first informed YES buy came before GFZ 11 times, between
GFZ and USGS 11, within 60 s of USGS 27 (24 within ~12 s): a resting 100-lot
YES ask ladder lost ~$87/day even USGS-gated (~$41 GFZ+USGS-gated), one
M6.4-6.6 day costing $400-600. A quake can only make YES worth more, so a
resting YES BID is never on the wrong side of the news -- hence bid-only.

MODEL (usgs_quake_fair.py, new). For a strike not crossed yet:
P = 1 - exp(-lam(K) * W / 24), lam(K) = -ln(1 - P_DAY(K)) with P_DAY the
empirical frequency of UTC days whose maximum reached K over 2016-2026
(3,650 days, aftershock clustering included; 0.890 at 5.2 ... 0.035 at 7.0,
log-linear between strikes, the end slope for up to 0.4 beyond, no model
past that). W = the rest of the day plus the unpublished tail (0.15 h with a
fresh GFZ feed, 0.3 h on USGS alone), clipped to the day; before 00:00Z the
whole day. The live ladder sat within 1c of this model on 5.6-6.4 on 9/27;
5.2 trades 5-8c rich (so the bot mostly rests behind it there).

FEEDS. USGS past-day M4.5+ summary GeoJSON (public domain) every
IMM_QUAKE_USGS_POLL_SECS (15), GFZ GEOFON FDSN event service text (earthquake
products CC BY 4.0 -- attribution "GEOFON data centre, GFZ Helmholtz Centre
for Geosciences") every IMM_QUAKE_GFZ_POLL_SECS (20), both from a refresher
thread ("quake") into an in-memory QuakeWatch (_quake_state) -- the gate
works in seconds, not through a file. A status snapshot (today's max, open
detections, feed ages, the module's knobs) goes to
run-logs/incentive-mm/usgs_quake_state.json once a minute.

GATE (quake_gate, per strike, in the quote loop after the GasBuddy gate):
  - STAND ASIDE (cancel): USGS read older than 180 s or the feed's own
    generation stamp older than 300 s (fail CLOSED); no watch yet; an
    unparseable ticker or a close not on the ticker's day; the strike already
    crossed on USGS (any network's displayed magnitude, so a tsunami centre's
    early high number also stands it down); no rate model; fair - margin < 1c.
  - HOLD (resting bids left exactly as they are, never raised on the news;
    the requote diff preserves them like a blind market's): a new M4.8+
    detection on either feed not yet confirmed -- confirmed = USGS shows the
    quake (origins within 90 s) with NEIC's own solution for 120 s, or, for a
    regional network that stays authoritative, a stable magnitude for 120 s
    once the quake is 25 min old; also a stale GFZ feed (> 120 s). Each hold
    is bounded by FREEZE_MAX_MIN (45 min per detection; a stale GFZ then
    falls back to USGS-only pricing). Only quakes under 105 min old are news,
    so a restart does not freeze on the past day's feed.
  - QUOTE: the bid ladder, every rung capped at floor(fair - 1c). No ask
    rungs and no ask pad ever: series_bid_only zeroes the ask side in the
    per-side ladder multipliers (side_size_mults: quote loop, reward probe,
    collateral estimate), skips the ask pad at both pad sites, and stops the
    two-sided depth test counting an ask pad (a thin ask side = no reward =
    no quote). The reward probe prices the same capped, bid-only ladder.
Guard-skip rows "quake" (stand) and "quake_hold"; log lines "quake
stand-aside / hold / resume <t>: why" once per transition and "quake
detection: <src> M<x> <id>" once per new detection.

SIZE + CUTOFF. Jack: "3x size KXTRUMPAPPROVE already uses (60)" -- the
family size_mult wire, SeriesOverride size_mult IMM_QUAKE_SIZE_MULT=3
(rungs, per-market and per-event caps and the skew knees together, and the
estimator's ladder). With the launcher geometry (20-lot rung, 150 / 1,000
caps): 60 on a weekday, 90 on a Saturday, 120 in the quiet 0-9 ET hours;
caps 450 per strike / 3,000 net per event -- the worst quiet day is up to
3,000 YES contracts bought and no quake at the strikes. (A first cut used a
hand-tuned 100-lot rung with the 150 / 1,000 caps; at the plain 20-lot size
the dry check estimated ~$5.70/day with only 4 of 8 quotable strikes over
the $1 payout floor.) cutoff_from_close_min 10 -- the close-anchored rule,
because parse_event_date would read 27SEP26 as 2027-09-26 and 05OCT26 as
2005.

KILL SWITCHES. IMM_QUAKE_ENABLE=0 takes KXBIGGESTQUAKE out of the
allowlist entirely (it is never quoted without the gate);
IMM_ALLOW_QUAKE_SERIES="" does the same. Knobs: IMM_QUAKE_MARGIN_CENTS (1),
IMM_QUAKE_CUTOFF_FROM_CLOSE_MIN (10), IMM_QUAKE_SIZE_MULT (3; 1.0 = the
plain book size), the poll intervals, and the module's IMM_QUAKE_* (lags,
freeze magnitude, confirm seconds, NEIC wait, freeze cap, staleness, news
age).

DRY CHECK 9/27 16:44Z (production env, live books, the bot's estimator,
~7.25 h left in the day), at the x3 size: 5.2 book 56x58 fair 49.5 -> bid
48 x165 est $4.71/day; 5.4 32x40 -> 32 x60 $2.31; 5.6 20x26 -> 20 $2.35;
5.8 14x16 -> 14 $3.02; 6.0 9x11 -> 8 $1.15; 6.2 6x7 -> 5 $1.09 (both just
over the $1 per-period floor); 6.4 and 6.6 bid behind the reference ($0);
6.8 / 7.0 stood aside (fair under 2c). ~$14.60/day on that book. Same books
minutes earlier: ~$21/day at a 100-lot rung, ~$5.70/day at the plain
20-lot size. The study's $40-48/day was at 100 lots on different books.

WHAT THIS DOES NOT FIX. The reward share is modelled (two snapshots in the
study, one dry check here); 25 days of study data with no M6.8+ quake; the
low strikes trade rich, so the bid often sits behind the touch there; a GFZ
detection USGS rates under 4.5 (not on the M4.5+ feed) holds the family for
the full 45 min; the fills are one-directional (long YES across correlated
strikes -- a quiet day loses every filled bid); KXEARTHQUAKEM (monthly,
settles on the REVISED magnitude) and KXBIGGESTQUAKEH (never listed) are
not enrolled.

WATCH AFTER DEPLOY: startup "quake gate: KXBIGGESTQUAKE BID-ONLY
fail-closed ..."; usgs_quake_state.json refreshing each minute with small
usgs_ok_age_s / gfz_ok_age_s; KXBIGGESTQUAKE rungs are bids only, at or
under fair - 1c; "quake detection" lines followed by holds and resumes; no
ask orders on the family, ever.

## 2026-09-27 pm — Undated earnings calls no longer quote to Dec 31; the resolver reads the company's own announcement (Jack)

Found while answering "it is not able to date Aritzia or Dominos?". Both had
sat UNRESOLVED on all 41 runs since 9/14, and the ACTION email's "(unresolved
events ... stop quoting the night before)" was false: the 8/14 listing-date
rule fires for any "MENTION" series whose market expires >5d past the ticker
date, and every earnings-mention market is a "next earnings call" contract
expiring Dec 31. So an undated earnings event's cutoff was Dec 31 -- it would
have quoted Aritzia's 13 markets (150 contracts on the book) straight through
its Oct 8 4:30pm ET call (Kalshi's ticker and milestone both said Oct 14).
Hand-set the same afternoon from the companies' releases:
ARITZIA-26OCT14 = Oct 8 16:00 (after close), DPZ-26OCT15 = Oct 13 06:05
(results 6:05am, webcast 8:30am; the Nasdaq pre-market proxy would have said
07:00).

Bot (incentive_mm.py): ticker_date_is_listing_date() never fires for
KXEARNINGSMENTION; trade_cutoff_utc() gives an earnings event its override
when one exists (the orphan-restore path calls it without the resolver) and
otherwise ticker date minus EARNINGS_UNDATED_LEAD_DAYS (14; Kalshi's ticker has
run up to 6 days late: ARITZIA +6, LLY +2, DELL +2). IMM_EARNINGS_UNDATED_LEAD_DAYS
=0 restores the midnight-of-ticker-date rule the resolver always assumed.
imm_feed_audit mirrors it and no longer calls an undated earnings event
"UNEARNABLE / close-anchored series missing its override". Checked against the
336 live selected markets at deploy: 0 cutoffs changed (all dated).

Resolver (Jack: "you were able to figure out what the earnings call times
were, so make sure the fallback can do that"): new earnings_announcements.py,
keyless, read FIRST for every undated event (and to upgrade a 7am guess):
- the IR site's event feed: Q4-hosted sites serve /feed/Event.svc/GetEventList
  as public JSON (Aritzia's JavaScript-only events page loads from it);
- the company's press releases: Nasdaq's per-symbol feed
  (api.nasdaq.com/api/news/topic/press_release) + nasdaq.com's copy of each
  wire release (Domino's own IR site 403s scripts; Nasdaq's copy does not).
The parser reads only the body after the wire dateline -- the page stamp
"Published Sep 10, 2026 4:05pm EDT" is what fooled parse_call_time() into a
4:05pm Domino's call -- classifies each time by the nearest keyword before it
(call/webcast vs results released/distributed), skips archive/replay times,
converts PT/CT/MT. Override = the RELEASE: stated time, else 16:00 after the
close, else min(07:00, call - 2h). Merged with the Nasdaq calendar: the EARLIER
wins, and sources more than a day apart go to the ACTION email as SOURCES
DISAGREE. An announced date >45d past the ticker date is not written
(IMM_ANNOUNCED_MAX_LATE_DAYS). Backtest on the 17 earnings events Kalshi has in
the next 30 days: 7 found (CCL, NKE, STZ split-report, DPZ 06:05, JNJ, JPM,
ARITZIA via IR feed), all matching the notices; the rest (big banks, PEP, UAL,
ACI, INTC) are US-listed and still fall to the Nasdaq calendar.

Not changed, noted: mention_cutoff_is_clear() only covers series ENDING in
MENTION, so the no-cutoff depth gate never applied to KXEARNINGSMENTION<SYM>
despite the 9/8 comment saying earnings were "deliberately IN scope". With the
14-day lead an undated earnings event no longer quotes near its call anyway.
Tests: test_earnings_announcements (26), TestListingDateCutoff (3 new).

## 2026-09-27 pm — ROI scan: admission clock, hourly-window auto-arm, restart book handoff, realized floor anchor + near-cliff room priority (Jack)

Jack asked for a scan of the bot for ROI optimizations "e.g. previously wasn't
incorporating saturday multiplier or overnight multiplier to calculate $1 /
$1.50 floor min / entry min. hopeless clock could malfunction / be slow to
eject MURRAY, if banked > $0.50 it wouldnt keep quoting to hit the $1 min",
then "build 1,2,3,4". Measurement window 9/13-9/27, from selection_events /
selection_snapshot / floor_state / cycle_log / fills / realized / orders.
Context: 1,401 of 2,873 quoted markets (49%) never reached $1 in the window
($324 of accrual stranded).

1. ADMISSION CLOCK (IMM_ADMIT_SUSTAIN_SECS, default 600; run-gap limit
   IMM_ADMIT_RUN_MAX_GAP_SECS 1500). Entry was one reading and the estimate
   swings ~2x between refreshes, so admission bought spikes: at admission the
   estimate was a median 1.69x the market's last rejected reading (p75 2.64x,
   n=2,217); once resting the market earned a median 0.74x of it in its first
   3 cycles and 0.71x over the first hour (n=1,276). Members scored at the
   same instant match the hypothetical ladder (median 1.00), so it is the spike
   reverting, not the model. 1,370 of 1,987 fresh admissions (69%) ended
   hopeless; fills on those rides lost $495 held to settlement (-5.6c/ct).
   Now a fresh candidate is admitted only once its projection has HELD at/above
   its bar for the sustain window (an unbroken run; a sub-bar reading or a gap
   > 1500s between readings starts it over) -- decision `admit_pending` until
   then. The same held run is now what resets a member's hopeless clock: one
   above-bar spike no longer buys a fresh 30 minutes (9/27: 42 single-reading
   resets on 18 members). Near-cliff re-entrants skip the wait; members,
   quote_all, FORCE_EVENTS and curated events never wait; an admission still
   starts the dip guard clean. State: `admit_run` (ticker -> [start, last]) in
   imm_state.json, pruned like hopeless_since; `above_secs` on selection rows.
   imm_quote_gaps reports such markets as "admission clock (x of 10 min held)".
   Kill: IMM_ADMIT_SUSTAIN_SECS=0 (single-reading entry and reset).

2. HOURLY ACTIVATION WINDOW AUTO-ARM (IMM_HOURLY_ACTIVATION_AUTO, default 1;
   IMM_HOURLY_PROGRAM_MAX_HOURS 2). The hh:00-hh:11 per-cycle refresh window
   exists for hourly program families; none (no program of <= 3h at all) has
   been in the candidate set since at least 9/25, and each per-cycle refresh
   reads ~2,700 books: on 9/26 quote cycles in the window took a median 126s
   vs 42s outside it, and the window held 46% of all refreshes. It now arms
   only when the last live feed carried a candidate on a program of <= 2h
   (logged "hourly activation window ARMED / disarmed"). Kill:
   IMM_HOURLY_ACTIVATION_AUTO=0 (always on, the old behaviour).

3. RESTART BOOK HANDOFF (IMM_RESTART_KEEP_ORDERS, default 1). A code-change
   exit cancelled the whole book and the relaunch cancelled again at startup:
   9/26 17:26:30Z exit -> 656 orders pulled 17:27:03 -> relaunch 17:27:36 ->
   first refresh done 17:30:06 -> first placements 17:30:32 -> rebuilt ~17:33;
   17 such restarts that day. Now the code-change exit (only that path; SIGINT,
   crashes, halts and manual stops are unchanged):
   a. imports the NEW source in a child python (preflight); a failure cancels
      the book exactly as before -- a broken deploy never leaves orders behind;
   b. cancels orders on live-event depth-gated series, events under a depth
      halt, fast-lane and bid-only (quake) series, unselected markets, and any
      market whose cutoff is within IMM_RESTART_KEEP_MIN_CUTOFF_SECS (1800);
   c. leaves the rest resting and writes run-logs/incentive-mm/
      restart_handoff.json;
   d. the relaunch adopts the book if that file is <= IMM_RESTART_HANDOFF_
      MAX_AGE_SECS (300) old -- ledger + TTL clock seeded from each order's
      exchange created_time -- else cancels as before. The file is consumed
      either way. The first cycle's refresh + diff re-prices, keeps or strays
      every adopted order.
   Log lines: "restart handoff: N order(s) left resting", "shutdown: left N
   resting bot order(s)", "startup: adopted N resting imm- order(s)"; the
   shutdown alert says "handed over to the relaunch (N kept)".
   Kill: IMM_RESTART_KEEP_ORDERS=0.

4. REALIZED FLOOR ANCHOR + NEAR-CLIFF ROOM PRIORITY
   (IMM_FLOOR_PROJECTION_REALIZED, IMM_NEAR_CLIFF_ROOM_PRIORITY, both default 1).
   The schedule-weighted projection scores HYPOTHETICAL ladders, but the loop's
   real orders are also cut by the per-event room share (net cap / strikes),
   inventory skew, the position cap and the per-side band: same-instant member
   reads put the resting score at p25 0.76 of the hypothetical on Red Rocks,
   0.54 on KXTRUMPMENTION, 0.69 on KXRBLX; and the near-cliff x1.5 boost was
   mostly undeliverable -- boost-armed Red Rocks / KXRT members had room below
   the boosted ladder in 88% / 91% of cycle rows (below the plain ladder 78% /
   82%) while the projection counted the boost.
   a. For an incumbent the schedule is scaled by (resting score / hypothetical
      at the live multiplier), capped at 1 (`floor_realized_ratio` on
      selection rows). Fresh candidates are untouched.
   b. In the quote loop a near-cliff market (verdict or sticky size mode) leads
      its event and may take the event's whole remaining room; siblings split
      the rest. Event cap, position caps and skew unchanged.
   Kill: IMM_FLOOR_PROJECTION_REALIZED=0 / IMM_NEAR_CLIFF_ROOM_PRIORITY=0.

What this does not fix:
- Handoff: nothing re-prices between the old process's last quote pass and the
  new one's first (~4-5 min including the first full refresh); the kept book
  is stale there instead of empty. Exchange TTL / cutoff expirations bind.
- The admission clock delays every genuine admission by one refresh (~10 min)
  and cannot help a market whose estimate is wrong on BOTH readings.
- The realized anchor reads the refresh's resting orders from the local
  ledger, whose remaining_count is not reduced by partial fills (the 20%
  amend tolerance bounds the drift); a transient shortfall (placement
  deferral, an hour-boundary resize) scales one refresh's projection down,
  which the 30-minute clock absorbs.
- Challengers still ignore the event-room share and the per-side band (the
  estimator builds a side the top-in-band gate won't place; measured impact
  small: one-sided admissions realize 0.68x vs 0.70x). Not in this change.
Tests: TestAdmissionClock (11), TestHourlyWindowAutoArm (3), TestRestartHandoff
(8), TestFloorRealizedAnchor (3), TestNearCliffRoomPriority (1); the suite runs
with ADMIT_SUSTAIN_SECS=0 at import, as it runs HOURLY_ACTIVATION_WINDOW_SECS=0.

## 2026-09-27 pm — Requote gaps interleaved; IMM_FORCE_EVENTS emptied (Jack)

Jack, on two more scan items: "fix these".

REQUOTE INTERLEAVE (IMM_REQUOTE_INTERLEAVE, default 1). The end of run_cycle
ran every diff cancel, then every placement, so an order being REPLACED (TTL
renewal, or a reprice the amend path can't take) was off the book for the
rest of the cancel loop plus every placement queued ahead of its successor:
23,013 cancel->replace gaps on 9/27 to 17Z (orders sink, same ticker + side),
median 9.7s, p90 20.3s, mean 12.4s -- ~0.7% of all order-time. pair_requotes()
now pairs each cancel with the placement replacing it (same ticker, side and
pad-ness, in placement order); place_with_caps cancels the old order one call
before its successor is placed, leaving it out of the cap totals as a
one-for-one swap (counted back in, successor skipped, if the cancel fails --
the cycle then raises at its end, the old failed-cancel semantics). Cancels
with no successor still run first. When the per-cycle placement cap defers a
successor, an old order IDENTICAL to it (pure TTL renewal: the post-restart
synchronized waves -- "placement cap 250/cycle reached; 439 deferred") keeps
resting until the next cycle; any other deferred swap cancels as before.
Kill: IMM_REQUOTE_INTERLEAVE=0.

IMM_FORCE_EVENTS EMPTIED (launcher). The 8/14 entries: KXEARNINGSMENTIONDKNG-
26AUG07 (settled 8/7; the note said "prune it on the next touch"),
KXNCLH-26OCTPAX (no live program since at least 9/20), KXFSLR-26OCTMWSOLD
(forced 8/7 so the $2/day re-entry rate bar would not shut out sibling strikes
of an event held only as orphaned inventory). The force also bypassed the
hopeless exit, so at 17:37Z 9/27 KXFSLR had 11 selected strikes projecting
under the $1.00 cliff for the period ending 9/28 03:02Z -- 3900/4000/4300/
4400/4700 at $0.64-0.76, 4800/5000/5100/5200 at $0.02-0.06 -- quoting for no
payout. Unforced: near-cliff holds 4100/4200 ($0.92 projected, banked $0.88-
0.89) to completion, 4500/4600 are over $1, the rest leave after the
30-minute clock with their positions riding. DEPLOY: the launcher builds
$ProbeEnv once, so this needs restart_imm.ps1 -Task; the code sync alone does
not pick it up.
Tests: TestRequoteInterleave (7), TestForceEventsEmptied (2).

## 2026-09-27 pm — Estimator fidelity: Carbon Arc cut scheduled, loop's side band in the estimate, schedule-weighted rate bar (Jack)

Jack, on the three scan items explained but not built: "fix all 3". One
block of knobs after KEEP_ACCRUAL_WHILE_PROGRAMMED; all three default ON.

(1) FLOOR_PROJECTION_SIDES. The floor projection read side_size_mults (the
Carbon Arc late-month cut: no bids, asks at half size from 00:00 ET fourteen
days before the measurement month ends) once, at `now`, for the whole window.
floor_size_profile() now splits the window at every side-rule change point
(side_mult_change_points: ca_late_window_start) and walks each piece with
size_mult_profile, so keys are (hour, bid, ask) multipliers and a step past
the 14-day walk cap still lands. Real estimator on a 45x48 KXAMZNCC book at
frozen October dates: the old projection for the Oct 12 -> Oct 19 15:02Z
period was 16% phantom at the period start, 35% on Oct 16, 55% on Oct 17;
identical once inside the window. First bites the October cycle (window
start Oct 18 04:00Z); September-cycle CA markets already stopped 9/26.
Profiles of every non-CA series are unchanged (462 random windows, max
weight diff 1e-16). Logging: floor_by_mult rows with non-unit side mults
carry [.., bid_mult, ask_mult]; floor_mult_profile tokens are tagged
"1/b0a0.5:0.210". Kill: IMM_FLOOR_PROJECTION_SIDES=0.

(2) ESTIMATE_SIDE_BAND. The estimator's probe ladder now builds a side only
when the loop would place it (that side's EXTERNAL touch inside
member_price_band(series, True) -- the band a selected market quotes in)
and passes the loop's rung band (RUNG_DEEP_FLOOR on a healthy book, else
the band floor, .. band top). quotable_sides reads the same test on the
external touches (an incumbent's own orders stripped), so a challenger's
91-93c ask now counts as a side. Replay 9/20-27 (1,251 fresh admissions):
94 logged quotable_sides=0, 90 had no side the loop could place (52 on 1c x
99c opens; KXYUMTBFT, KXMCDFT, KXDIESELD, KXBA ...); 89 logged one-sided were
two-sided in the member band. Caveat, measured: those 90 books mostly filled
in -- first-hour realized est_frac a median 2.4x the admission estimate
(n=64) -- so for fresh markets the fix mostly moves admission to when the
book is quotable (+ the admission clock). Members: the 38 that sat wholly
out of band 30+ min on 9/27 were all curated (KXRAIN), finecon (KXCBD,
KXSPRLVL), forced (KXFSLR) or banked >= $0.85, so none would have exited.
Kill: IMM_ESTIMATE_SIDE_BAND=0.

(3) RATE_FLOOR_SCHEDULE. The $2/day rate bar (68 series) compared the
live-size est_dollars_per_day; it now compares floor_dollars_per_day
(rate_floor_rate), the rate the payout floor and horizon escape already use.
Consequence, measured on 9/26-27 snapshots: the schedule-weighted rate runs
a median 1.39x normal size for long-dated series and 0.68x for the gas /
diesel dailies, so the bar is now ~$1.44/day of normal-size earnings for
the former (was $2 by day, $1 overnight, $1.33 Saturday) and ~$2.94 for the
latter (was $2 by day, $4 in the evening) -- at every hour. No new-event
candidate in the 9/26-27 snapshots changes verdict. selection rows add
rate_est. Kill: IMM_RATE_FLOOR_SCHEDULE=0.
Tests: TestFloorSideSchedule (10), TestEstimatorSideBand (8, incl. a
dry-run mirror: the loop places exactly the sides the estimator counts on
49x51 / 3x50 / 80x92 / 1x99 books), TestRateFloorSchedule (3). Every one of
8 targeted mutations fails them. TestFloorRealizedAnchor.test_never_scales_up
compared two reads ms apart on a sliding window at 9 places (flaky); now 6.

### 2026-09-27 pm — KXDIESELD joins the GasBuddy gate (Jack: "ok do that for diesel daily")

KXDIESELD settles on AAA's NATIONAL diesel average (posted ~03:20 ET, settled
~09:00-09:40 ET); each event trades 08:00 ET the day before to 01:59 ET on the
print date. The bot has quoted it ungated since 2026-08-02: September's fills
lost -$178 on 5,942 ct (-3.0c/ct; sell-YES -8.0c/ct in a rising market;
08:00-15:59 ET -3.8/-7.0c/ct, after 16:00 +0.4/+1.7c/ct), credits ~$63 over
9/6-9/22 in reward_credits.csv.

GasBuddy diesel (fuel type 1): live national average from the map endpoint
at country level (subRegionType 6; LiveAvg ignores the fuel type), Full Day
Averages from the chart. Measured on 39 print days (Aug 3 - Sep 27): AAA_d(D)
- AAA_d(D-1) = 0.24c + 0.76 x (GB_d(D-1) - GB_d(D-2)), residual 1.69c vs
2.9c carried forward. AAA's diesel matches GasBuddy's SAME-date diesel more
closely (corr 0.98, 0.66c) -- GasBuddy's diesel runs about a day behind --
but that figure is published after the close. Replay of the September fills
at tol 0: GasBuddy's end-of-day value known -> allowed +7.5c/ct (2,185 ct),
blocked -32c/ct (1,204 ct); day's move revealed linearly -> no separation.

gasbuddy_fair.py writes a KXDIESELD-<print date> entry beside the states
(DIESEL_MODEL alpha 0.24c / b1 0.76 / e 1.69c, DIESEL_DAY_MOVE_SD 3.57c for
the linear remain default; diesel_finals / diesel_closes in the file; the
live log rows carry diesel_us). The anchor reads "pending" until Kalshi
settles the day's print (~09:00-09:40 ET), so the first hour or two after the
08:00 open stands aside; the 2c sigma cap then holds until mid-afternoon on
the linear default -- the hours where September's fills lost. Mondays are
skipped as for gas. incentive_mm: gb_fair_series() includes KXDIESELD while
GB_DIESEL_ENABLE (IMM_GB_DIESEL_ENABLE, default 1; 0 = plain quoting as
before); the strike regex takes the "-T6.470" form; the national gas daily,
KXDIESELW and KXDIESELMONAK are untouched. Dry run 9/27 17:11 ET: Monday print
AAA ~6.4566 +-1.86c (Sunday 6.4709, GasBuddy -2.2c today); against the live
books 18 of 21 strikes quotable, 3 stood aside (a 99c bid on T6.435 vs fair
88c, a 59c ask on T6.450 vs 64c, a 5c ask on T6.470 vs 23c). Tests:
TestDieselDaily (4), the diesel fetch parser, gate strike format + series
membership; 950 green across the IMM suites.

## 2026-09-27 — Vercel pre-D gate: the eight Vercel AI Gateway series quoted only BEFORE their measured day (Jack)

Jack: "build the pre-D Vercel gate".

WHY THIS FAMILY, AND WHY ONLY PRE-D. KXOPENVSPEND / KXMOONVSPEND /
KXANTHVSPEND (a lab's share of SPEND), KXGOOGVREQ / KXOPENVREQ / KXDEEPVREQ /
KXANTHVREQ (share of REQUESTS) and KXOPENSOURCESHARE (the open-weights share
of TOKENS) settle on one UTC day D of Vercel's official leaderboard export
(vercel.com/docs/ai-gateway/leaderboards, CC BY 4.0 (c) 2026 Vercel). Read-only
study 9/27 (session scratchpad vau/vercel/): all 19 numeric lab settlements
reproduce exactly (dataset=labs, modality=all); the open-weights share is
reconstructed within 0.6 pp as the summed token share of the open-weight-only
labs. The export shows D LIVE (50/50 fresh reads 30 s apart changed), and the
informed flow trades D off it: makers lost -$4,846 (-7.9c/ct) during D and
made +$679 before it. Kalshi posted the 28SEP26 programs ($50/market,
16:00Z 9/27 -> 03:59Z 9/29) 8 hours before D -- at that lead a pre-D-only
book clears the $1 floor on the better strikes (~$1.26/market at a 13%
share); at the 1-2 h leads of 9/22 and 9/24 it never does.

CUTOFF. Out at D 00:00Z - IMM_VERCEL_CUTOFF_BEFORE_D_MIN (60) = 23:00Z the day
before, in apply_series_cutoff_adjustments (both producers). D from the
ticker by series: lab DDMMMYY = D, KXOPENSOURCESHARE YYMMMDD = D+1.
parse_event_date reads both wrong (05OCT26 would be 2005), so the series get
cutoff_from_close_min=0 (the ticker-date rule out) and this tightener sets the
real cutoff; an unparseable event stands down (fail closed, logged once).

FAIR (vercel_fair.py, new; refresher thread "vercel-fair" every
IMM_VERCEL_FAIR_REFRESH_SECS=900; one fresh export read per refresh -- the
plain URL is cached 24 h, so each read uses a distinct `to` date, clamped by
the API). The deep-dive's backtested pre-D model: X_D = X_L + e with L the
latest COMPLETE day (D-2 while quoting on D-1) and e the empirical h-day
changes of the last 60 days, widened about the median x1.5 for the lab
series (their backtest was overconfident: 80-100% fairs settled YES 67%)
and x1.0 for the open-weights share (well calibrated). P(YES) =
P(round1(X_D) > K) = P(X_D >= g - 0.05). VERCEL_FAIR_FILE per EVENT ticker
for D = today+1 .. today+3.

GATE (vercel_gate_reason, quote loop after the quake gate, guard
"vercel_fair"): stand aside (cancel) on an unparseable ticker, a close not
on D+1, less than 60 min to D, no read (fail CLOSED), a stale read (90 min),
a new complete day within 10 min (the book reprices), a decided strike
(fair < 5c or > 95c) or a touch fighting the fair by > 15c on the adverse
side. Otherwise the ordinary two-sided ladder (safe-join placement, regular
size, no per-event cap). Strikes parse as T5P5 (labs) or T67.5
(open-weights). Whether a program's pre-D window clears the $1 floor is left
to the existing floor projection (the quotable window ends at the cutoff).

DRY CHECK 9/27 22:20Z (live books from the Vercel logger, fair from the
export): of the 81 28SEP26 markets, 75 would quote, 4 stood aside on the
band (e.g. KXOPENSOURCESHARE T82.5: ask 9c vs fair 25c), 2 decided.

KILL SWITCHES. IMM_VERCEL_ENABLE=0 takes the family out of the allowlist
(never quoted without the gate); IMM_ALLOW_VERCEL_SERIES="" does the same.

WHAT THIS DOES NOT FIX. The fair ignores the RUNNING D-1 value (only complete
days), so an informed trader reading today's running share knows more than
the gate before D; the measured pre-D maker P&L was positive anyway (95
trades -- thin). Programs are bursty (none for days at a time). The
open-weights classification is undocumented. The intraday history needed
for any DURING-D quoting is being logged separately (Windows task "KL
vercel-logger", Documents/KL-data/vercel-logger).

WATCH AFTER DEPLOY: startup "vercel gate: ... PRE-D only, fail-closed ...",
"vercel-fair refresh: 24 events with a read", run-logs/incentive-mm/
vercel_fair.json refreshing every 15 min, "vercel stand-aside / resume"
lines, and no Vercel orders at or after 23:00Z the day before D.

### 2026-10-01 addendum — the fair anchors on TODAY's running share (Jack)

Jack: "yes anchor the fair on D-1's running share" (after the "what does
this not fix" line above: the export shows today's running share live, and
the 9/27 fair, anchored on the latest COMPLETE day, could not see it).

MODEL (vercel_fair.py). From IMM_VERCEL_RUN_START_MIN (60) after 00:00Z,
with T = today and k = D - T days ahead:
    X_D = R_T(tau) + delta + e_k
R_T(tau) = today's running share now; delta = final - running for the same
series at the same minute of day (+-10) on each logged complete day (the KL
vercel-logger's data/export_*.jsonl[.gz], a pass every 2 min; the final =
the next day's 02:00-14:00Z read, before the D+2 revisions settlement never
sees), widened x1.5 about its mean; e_k = the empirical k-day changes as
before (x1.5 labs / x1.0 open-weights). delta keeps the strong hour-of-day
biases (DeepSeek requests read ~9 pp high at 01Z and ~2 pp high at 12Z,
Anthropic spend ~5 pp low early, open-weights ~4 pp high mid-day). It
applies to every horizon (k = 1..3), not only D-1: the running day is the
freshest public number for all of them. Before 01:00Z (today's block is
degenerate for the first 10-50 min) the 9/27 complete anchor stands.

FAIL CLOSED. Past 01:00Z a missing / degenerate running block (mode
fail:block) or fewer than IMM_VERCEL_RUN_MIN_DAYS (3) calibration days for
a series (fail:calib) writes no entry for it -- the D-2 anchor is never a
fallback once the running share is public. So the KL vercel-logger task is
now a LIVE dependency of the gate: if it stops, the calibration days age
out after IMM_VERCEL_RUN_CALIB_MAX_AGE (30) days and the family stands
aside. IMM_VERCEL_INTRADAY_DIR points elsewhere.

IMM SIDE. Entries carry anchor "run" / "complete"; running entries go stale
after IMM_VERCEL_RUN_TTL_MIN (20) (complete ones keep 90); the refresher
runs every IMM_VERCEL_FAIR_REFRESH_SECS = 300 (was 900) and logs the mode;
a switch of anchor (01:00Z, 00:00Z) starts the usual 10-min hold. Reads:
the 70-day history (1.4 MB) once a day / every 6 h, plus a fresh
yesterday + today read (~40 KB) per refresh.

EVIDENCE (read-only, Documents/KL-data/vercel-logger/
analysis-2026-10-01-anchor/). (1) Leave-one-out backtest on the three logged
D-1 days (9/27 partial, 9/28, 9/29; 776 series x half-hour cases): CRPS
3.23 -> 2.13, median |center - final| 2.58 -> 1.63 pp, the new fair better
in 569/776 and in every hour block; better for 6 of 8 series, a tie on
KXGOOGVREQ, WORSE on KXMOONVSPEND (its 9/28 dip to 10.2 reverted to 18.7
on 9/29; Moonshot's daily changes mean-revert, corr -0.31). (2) Over 68 days
of finals, X_{D-1} beats X_{D-2} as a predictor of X_D for all 8 series
(MAE, e.g. open-weights 3.41 vs 5.45, Moonshot 2.54 vs 3.06). (3) Pre-D
tape of the D = 9/30 markets (19.8k ct, makers -$1,425 at settlement):
makers lost -$392 on trades where the OLD gate would have been open, -$156
where the NEW one would. The 9/29 losses (-$880) were stood aside by BOTH
gates (Sep 28's complete day already showed the weekday drop); the new
anchor's gain came on 9/28, when the running share showed the Monday drop
and the stale Sunday anchor did not (KXOPENSOURCESHARE-26OCT01-T69: an 80c
bid makers lost $240 on; old fair 85c, new 3c = decided).

WHAT THIS DOES NOT FIX. Three calibration days (it grows by one a day; the
early-hour deltas are the noisiest). No mean reversion (Moonshot) and no
weekday term (open-weights ~76% on weekends vs ~57-62% on weekdays) in the
change sample. The gate still only joins the touch: the fair decides
stand-asides, it does not price.

KILL SWITCHES. IMM_VERCEL_RUN_ANCHOR=0 restores the 9/27 complete anchor all
day (no logger dependency); IMM_VERCEL_ENABLE=0 takes the family out.

WATCH AFTER DEPLOY: "vercel gate: ... anchor today's running share (ttl
20m; X_L before 01:00Z)", "vercel-fair refresh: N events with a read,
anchor run" (complete before 01:00Z), vercel_fair.json's "anchor" block
(mode, tau_min, calib_days, delta_n), and "decided: fair Xc (running ...)"
stand-asides.

## 2026-09-27 pm — Fair refreshers read Kalshi SIGNED: OpenRouter windows, GasBuddy anchors, Carbon Arc catalog (Jack)

Jack, after the public-API 429 investigation: "yes build change A on a
branch".

WHY. Kalshi throttles UNSIGNED /markets (and /events) LIST reads from any
IP; it is not this box's volume. 9/27: the OpenRouter window read (2 list
calls per 10-minute refresh) failed about half its attempts 06-14Z while the
box sent under one unsigned list call a minute and no other session was
calling Kalshi; a lone list call at 00:41Z got a 429 before any collector
ran; from a second IP (Anthropic's fetchers) 6/6 unique list URLs got 429
while /exchange/status, a single market and an order book passed. Unsigned
order-book reads are fine (yt_kalshi_books.py ran ~9k at ~1/s with few
retries). Paired probe 20:44-20:47Z, the gate's exact call: unsigned 18/20
(both 429s on CDN cache misses, 2 of 7), signed 20/20 on the first try,
25-55 ms against 0.7-1.1 s. Damage so far: the 05:49-06:28Z OpenRouter
stand-aside on 3 markets after the 05:47 restart (the cached-window fallback
covers restarts now; a NEW event still needs one good read). The GasBuddy
gate's exposure is the 07:00 ET anchor pass, 26 list reads re-asked every 5
minutes until they land, plus the KXDIESELD anchor, re-asked every 5 minutes
until Kalshi settles it (~09:00-09:40 ET).

WHAT. FAIR_SIGNED_READS (IMM_FAIR_SIGNED_READS, default 1). The ca-fair,
or-fair and gb-fair refresher threads each build a SignedKalshiGet through
fair_reader(): its own ExchangeClient with the account key and its own
keep-alive session (not the trading loop's), built on first use. Each
thread passes it to its module's write_fair_file(get_json=...), and
openrouter_fair.fetch_windows, gasbuddy_fair.fetch_anchor and
carbon_arc_fair.fetch_series_map read through it first. A failed signed
read (the client already retried a 429/5xx at 1s and 3s) falls back to the
old public read with its old retries, so the worst case is today's
behaviour. Each module logs a distinct signed failure once ("[or-fair] !
signed Kalshi read failed (<error>); reading the public endpoint") and the
recovery once ("signed Kalshi reads back"). Startup line: "fair refreshers:
Kalshi reads SIGNED, public endpoint as the fallback". Hashed in the config.
Cost: ~500 signed reads a day on the account's bucket (Advanced: 300 read
tokens/s, 10 per read), about 0.02% of it.

NOT CHANGED: the rain and quake refreshers (NWS / USGS; no Kalshi reads);
the three modules' standalone CLIs (no reader, so public reads as before);
the other session's collectors on this IP (yt_kalshi_books.py, the vercel
logger), which do not cause these 429s.
Kill: IMM_FAIR_SIGNED_READS=0 (public reads only). DEPLOY: code only, no
launcher change; the sync and the incentive_mm.py mtime restart pick it up.
Verified live on the branch: the real SignedKalshiGet fed all three module
reads (both token windows, the KXAAAGASDCA-26SEP27 anchor 6.3528, the
KXDIESELD-26SEP27 anchor 6.4709, 93 Carbon Arc series) with the public
endpoint patched to raise: 0 public calls.
Tests: TestSignedFairReads (5, including a source guard that all three
refreshers pass their reader) and the modules' signed-read tests (4 / 3 / 3).
Each of 6 targeted mutations fails them. Rebased onto the KXDIESELD entry
above: its anchor goes through the same anchor_fn, so it is read signed too
(the GasBuddy write test counts 51 state anchors + KXDIESELD through the
reader). The Vercel pre-D gate above reads no Kalshi (vercel_fair.py reads
only Vercel's export), so it takes no reader. Full suite 1424 green.

## 2026-09-27 pm — KXAAAGASD (the national gas daily) joins the GasBuddy gate (Jack: "yes gate KXAAAGASD national on gasbuddy")

WHY. The national AAA gas daily was the one gas daily still quoted without a
fair after the state dailies and KXDIESELD went behind GasBuddy. On the 9/27
rewards statement (mark-to-market) it lost $161 over 9/23-9/27 on $27 of
credits: the 9/24, 9/25 and 9/26 events -$60, -$57 and -$53.

MODEL (gasbuddy_fair.py). Same clock as the states: event D trades 08:00-23:59
ET on D-1 (open 12:00Z, close 03:59Z), AAA posts ~03:20 ET, Kalshi settles
~07:06 ET (Sundays ~09:10). Fitted on 129 print days (Kalshi's KXAAAGASD
expiration values, 211 events back to 2023 via /historical/markets, against
GasBuddy's national Full Day Averages Mar 27 - Sep 26; leave-one-out
residual, carried-forward sd in brackets):
    Mon print  alpha +1.07c  b1 0.11  b2 0.48  e 0.41c  (0.67c)  n 26
    Tue print  unmeasured (3 days) -- skipped, like the states
    Wed print  unmeasured -- b1 0.87, e 1.0c assumed
    Thu print  alpha -0.03c  b1 0.91  e 0.63c  (2.88c)  n 23
    Fri print  alpha -0.35c  b1 0.87  e 0.46c  (2.68c)  n 25
    Sat print  alpha +0.46c  b1 0.85  e 0.58c  (1.77c)  n 25
    Sun print  alpha +0.66c  b1 0.65  e 0.41c  (0.82c)  n 27
remain(t) scales with GasBuddy's national daily move by trading weekday (Wed
3.1c, Thu 3.0c, Fri 2.0c, Sat 1.1c, Sun 0.7c; Mon/Tue the states' figures),
linear in the ET hour like the states. The national live average (LiveTicking
Avg) and 1 Day Ago come from the country LiveAvg read every refresh already
makes -- no extra GasBuddy call (4.425 matched the map endpoint's USA row);
the chart is read only for the Monday print's day-before-yesterday. Entry
KXAAAGASD-<print date>, fuel "gas"; natgas_finals / natgas_closes persist in
the fair file; gasbuddy_live.jsonl rows gain "gas_us".

GATE (incentive_mm.py). gb_fair_series() includes KXAAAGASD while
GB_NATGAS_ENABLE: fail CLOSED, tol 0, the 0.5-sigma band, the 2c sigma cap,
no slot without a read, Monday trading (the Tuesday print) skipped. Its own
guards are untouched (ensure_family_override never re-clones the parent, so
no state-style rate-floor swap). Kill switch IMM_GB_NATGAS_ENABLE=0 = plain
quoting again. Startup line: "gb-fair gate: AAA state dailies + KXDIESELD +
KXAAAGASD ...".

DRY RUN 9/27 19:35 ET (Sunday -> Monday print): 28 entries (26 states,
KXDIESELD, KXAAAGASD), 0 missing. National fair 4.4800 +- 0.41c (anchor
4.4798, GasBuddy live 4.425 vs Saturday's 4.468 -- a large Sunday drop the
Monday fit passes through at only 0.11). The book disagreed near the money
(4.480 strike 17x18 against a 50c fair, 4.475 61x62 against 89c): those 4
strikes stand aside, the 13 far strikes where book and fair agree quote.

ALSO FIXED (all three chart reads). GasBuddy's chart carries TODAY's figure
so far. The diesel path filed it as a Full Day Average, so the next day saw
"yesterday present", skipped its re-read and priced KXDIESELD off a partial
(every other day: 9/27's 6.445 was sitting in diesel_finals as a "final").
Chart points for today are no longer filed (states, diesel, national).

WHAT THIS DOES NOT FIX / WATCH:
- The Monday print's b1 0.11 was fitted on ordinary Sundays (sd 0.7c); a
  large Sunday drop like 9/27's (-4.3c live) is outside the sample. The gate
  stands aside where the book disagrees, so an under-reaction costs quotes,
  not fills -- check 9/28's print (AAA vs 4.4800) against it.
- remain(t) is the same linear guess as the states until gb_live.jsonl is
  calibrated; the national now logs "gas_us" for that.
- Tuesday prints stay unquoted (no Monday Full Day Averages in the chart).
- Measure: the national's fills markout vs settlement over the first week.

Tests: TestNatGasFair, TestNationalGasDaily (entry math, pending anchor, no
live read, close fallback, Monday skip, Sunday chart read once, live log),
TestDieselDaily.test_todays_partial_chart_point_is_not_a_final,
test_fetch_live_avg (live_price), the national in the gate's family-shape and
kill-switch test, test_national_gas_strike_format_and_guards; anchor/absent
counts +1. 977 green (test_incentive_mm, test_gasbuddy_fair,
test_carbon_arc_fair, test_openrouter_fair, test_send_imm_new_programs,
test_usgs_quake_fair, test_imm_pickoff, test_earnings_announcements).

## 2026-09-27 late — KXUST (all ten Treasury tenors) and daily KXRAIN x1.5 family size (Jack)

Jack: "1.5x multiplier on KXUST and daily RAIN since it's consistently
performed well". size_mult=1.5 on the ten rates overrides
(RATES_SIZE_MULT, env IMM_RATES_SIZE_MULT) and on
SERIES_OVERRIDES["KXRAIN"] only (RAIN_DAILY_SIZE_MULT, env
IMM_RAIN_DAILY_SIZE_MULT) -- the KXTRUMPAPPROVE / ladders wire:
applied_mention_mult scales the rungs, the per-market / per-event caps
(225 / 1,500, were 150 / 1,000), the skew knees and the estimator's
ladder (the payout-floor projection) together. 1.0 reverts either (env
=> task-level restart). Not scaled: the rain monthlies (blocklisted in
the launcher anyway), KXRAINWKND, the KXRAINS<CITY> spans (their own
archetype) and the NWS directional take (RAIN_DIR_SIZE).

Checked against the committed code with the launcher's $ProbeEnv
applied: KXRAIN 30 by day, 15 in the 19-01 ET halving (a daily: no quiet
hours, no Saturday). KXUST*AD / *AM 30 on a weekday, 60 in quiet hours
(0-9 ET, up to the 07:30 cutoff), 45 Saturday, 90 Saturday quiet hours:
the Treasuries are not in the structural daily class, so they already
took the quiet-hours and Saturday multipliers and x1.5 rides on top.

Evidence (rewards statement 9/27, mark-to-market, report_tools): KXUST
+$1,229 lifetime on $1,291 of credits (trading -$61, give-back 5%),
positive 8 of 8 weeks since Aug 3. Daily rain +$538 lifetime ($1,601
credits, -$1,063 trading) but positive every week since Aug 24, +$927
over the last five (give-back 11%). Modelled share of the scored book
(cycle log est_frac, 9/14-9/27, reward-weighted): rain ~6%, Treasury
dailies ~17%, so x1.5 size is about x1.45 / x1.39 reward before anyone
else adds size; fills scale about x1.5.

WATCH: the Treasury programs have thinned -- the dailies paid on only a
few days in the last two weeks (modelled $144 over 14 days, pool median
$15/day per market against ~$102 at enrollment) and the monthlies not at
all, so the dollar effect is small until the pools come back. Rain's
lifetime give-back is 66% (the late-July weeks); if its trading line
turns back, 1.0 via env.

Tests: TestDailyRainSizeMult (x1.5, both caps, the day ladder; the 19-01
halving composes to x0.75; monthlies / weekend / rainstorm archetype stay
1.0), TestTreasuryYieldSeriesEnrolled.test_x1_5_family_size (ten tenors,
guards kept, no prefix bleed); TestRainFairAnchor pins the family size
at 1.0 (its assertions are on literal rung sizes). 1,453 green.

## 2026-09-28 late — Elections allowlisted: county judges, mayors, House, state AGs and the general elections, quoted 1-99c until 00:00 ET on election day (Jack)

Jack: "allowlist county judge markets e.g. KXBEXARCOUNTYJUDGE,
KXCOLLINCOUNTYJUDGE / house election markets e.g. KXHOUSEWINSTATE,
KXHOUSEWINSTATE-NJD / mayor elections e.g. KXHENDERSONMAYOR, KXLEXMAYOR /
quote until election day. expand range to quote between 1 and 99", then
"also allowlist general election markets e.g. KXSERBIAPRES, KXBC3RD,
KXQUEBEC4TH, KXSAARLAND, KXNORDRHEINWESTFALEN". Commit fc3f84e.

THE FEED. Kalshi lit an elections batch 20:02Z-23:02Z 9/28, programs to
10/04 03:59Z: 60 Elections-category series. One market per candidate /
party / count, $285-$500 per market per ~5-day period ($20-55/market/day),
and most books empty -- a 1-3c bid under a 97-99c ask -- which the global
5-90c band stood aside from entirely.

MEMBERSHIP (election_series; allowed in _allowed). The "e.g." is the
family, so the three US families are NAME PATTERNS (ELECTION_SERIES_
PATTERNS, full-match): KX[A-Z]+CO(UNTY)?JUDGE (incl. KXWILCOJUDGE),
<city>MAYOR less KXISTANBULMAYOR / KXACKMANMAYOR / KXAPCALLLAMAYOR /
KXBBGMAYOR (a court case, "will he run", AP-call timing, unattributed),
KXHOUSEWINSTATE, KXHOUSE<ST><N> district winners, KX<ST>HOUSE1R
(Louisiana's open primaries) and KXATTYGEN<ST> (29 states). The general
elections share no naming shape, so they are an exact list of the 9/28
feed's election-OUTCOME series (ELECTION_SERIES): SERBIAPRES, BC2ND /
BC3RD, QUEBEC4TH / QUEBEC5TH, SAARLAND, NORDRHEINWESTFALEN,
SCHLESWIGHOLSTEIN, PUNJABASSEMBLY, BRAZILTURNOUT, DEMTRIFECTA,
UNDERHARRIS, VOTEGENERAL, CAATTORNEYGENERAL, FULTONCHAIR. Filed under
Elections but left OUT: KXSENMIN (a leadership vote by senators after the
election), KXSERBIAELECTIONCALL (when the vote is called: news timing),
KXGENERICBALLOTVOTEHUB (a daily polling average, the KXTRUMPAPPROVE
pick-off shape), KXVPRESPERSON (2028: the running-mate picks, not the
vote, are the first reveal). Checked against the live feed: 56 of the 60
programmed Elections series allowed, exactly those four not, and no
election_series() hit outside the Elections category.

THE CUTOFF (election_cutoff_utc, applied in apply_series_cutoff_
adjustments for both producers and the quote-gaps mirror, keyed on the
family so orphan restore gets it too): 00:00 ET on the voting day, the
EARLIEST of the hand table ELECTION_DATES, the ticker date, and the ET day
of Kalshi's occurrence less 6h. Kalshi's occurrence is the poll close on
the county judges / mayors / CA AG / Fulton (01:00Z Nov 4 Texas, 23:00Z
Nov 3 Kentucky, 03:00Z Nov 4 Nevada) and the ticker date is the vote
abroad (26OCT24 BC, 26OCT05 Quebec, 27APR18 Saarland) -- but on the House
seat counts and district winners the occurrence is Jan 3 2027 (Congress
convenes; KXHOUSEWINSTATE-SCD May 2027, -ALD Nov 2027), on the attorneys
general the swearing-in (Dec 15 2026 - Jan 18 2027) and KXUNDERHARRIS's ticker is
certification (27JAN04). Every one of those would have quoted THROUGH the
election; the table pulls them to Nov 3: `*-26` (every year-only 2026
event -- the US races), KXHOUSEWINSTATE-*, KXVOTEGENERAL-*-26*,
KXUNDERHARRIS-*. A row can only move the day earlier. The 6h read-back
keeps an Alaska / Hawaii close after midnight ET on Nov 3 and moves an
Asian close (Taipei 08:00Z Nov 28) to the day before -- Taipei's vote opens
19:00 ET Nov 27. A Dec 31 date is Kalshi's placeholder (KXJOHANNESBURG
MAYOR-26DEC31); placeholder or no date at all -> RELEASE_GUARD_UNKNOWN
(stood down, logged once) unless the table has a row.

Resulting cutoffs on the programmed events: every US race 2026-11-03
05:00Z (00:00 EST), Brazil turnout 10-04 04:00Z, Quebec 10-05, BC 10-24,
Serbia 12-27 05:00Z, Punjab 2027-02-20, Saarland / Schleswig-Holstein
2027-04-18, North Rhine-Westphalia 2027-04-25.

GUARDS. ELECTION_ARCHETYPE (KXBEXARCOUNTYJUDGE) override: price band
1-99c (price_min/max: member_price_band, the quote loop, the estimator's
quotable sides and the extreme_mid screen) and safe-join (the KXCMA /
awards / KXVENUEPERFORM nominee-binary guard; free on the empty 2/98
books and on a stacked touch). Every member clones it on first sight via
a new "predicate" kind in FAMILY_OVERRIDE_PARENTS. No size multiplier, no
per-event cap, the ordinary $1.50 entry bar. imm_quote_gaps now runs
ensure_family_override before build_meta, as refresh_universe does --
without it every pattern family (ladders / escalators too) read the
global 5-90c band in the email.

LIVE (run 7be453a4, config 793ed6b8): the source-mtime exit at 00:22:20Z
handed 426 resting orders to the relaunch. First refresh 00:24:08Z:
candidates 1,996 -> 2,216 (231 election markets, every cutoff as listed
above), 131 of them admit_pending on the 10-minute admission clock.
Second refresh 00:34:29Z admitted 130: 491 selected across 101/200
events (345 / 73 before the deploy). Election family: 153 selected (72
general-election, 50 KXVOTEGENERAL, 16 House, 13 county judge, 1 mayor,
1 AG), 78 payout_floor (59 AGs -- deep books already trading -- plus 3 of
the 4 mayor markets and 3 county judges). First cycle: 250 placements
(the per-cycle cap; 10 deferred) on 125 election markets, 0 rejects; 52
bids at 1-4c and 30 asks at 96-99c, the orders the 5-90c band stood aside
from (KXCOLLINCOUNTYJUDGE-26-JBRO 3c / 97c, KXFULTONCHAIR-26-ETAT 2c / 98c,
KXBC2ND-26OCT24-2-CEN 2c / 3c). KXVOTEGENERAL had been an open-scan series
(23 members): it left the scan tier ("guard set released") and inherited
the election archetype. imm_quote_gaps --dry builds clean on the new code.

KILL SWITCHES (launcher env, task-level restart): IMM_ELECTION_ALLOW=0
drops the family; IMM_ELECTION_SERIES / IMM_ELECTION_SERIES_PATTERNS
replace the lists (comma-separated, so no regex may contain a comma);
IMM_ELECTION_DATES replaces the WHOLE table (the IMM_AWARDS_EVENT_DATES
format); IMM_ELECTION_PRICE_MIN / _MAX; IMM_ELECTION_SAFE_JOIN=0.

WHAT THIS DOES NOT FIX / WATCH:
- Unscheduled votes: KXSERBIAPRES-26DEC27 is "the next Serbian
  presidential election" (whether it is called by Oct 15 / Nov 1 is its
  own market) and KXPUNJABASSEMBLY-27FEB20 carries 2022's date. If a vote
  is set EARLIER than Kalshi's date, add a table row that day.
- The `*-26` row covers the 2026 US races only; a 2027 / 2028 listing
  (KXCHICAGOMAYOR-27 reads its own Feb 23 occurrence) needs its own row
  where Kalshi's dates are post-election.
- ET midnight of the local election date is inside the voting day east of
  Europe when no poll-close occurrence exists (India: 10:30 IST); Punjab
  has one at 14:00Z, which lands on the same day.
- Inventory taken before the cutoff rides through the vote to settlement
  (standard cutoff semantics).
- 1-99c means the bot will sell a 98c ask into a near-certain race; the
  per-market cap bounds it (~150 x 2c).

Tests: TestScreen.test_election_family_quoted_1_to_99_until_election_day
(membership and exclusions, kill switch, blocklist wins; 17 events'
cutoffs as Kalshi served them incl. the Jan-2027 House / AG dates and the
Taipei day-before; placeholder / no date fail closed unless tabled;
guards cloned; the empty 2/98 book quoted at its touch where the global
band places nothing; the screen). 1,476 green (unittest discover) at
fc3f84e.

### 2026-09-29 early — Verified election days replace Kalshi's; safe-join off (Jack)

Jack, on the first cut's summary: "Abroad the bot uses Kalshi's date --
dont trust that, verify yourself" and "Safe-join ... make sure you're
optimizing for rewards like the IMM bot and resting within the 200
contracts". Commit 10d8491.

DATES. ELECTION_DATES is now the only source of an election's day. 54
rows, each checked against the electoral authority, with the source
beside the row in incentive_mm.py: Canada (Elections BC, Elections Quebec,
ontario.ca, gov.bc.ca, gov.mb.ca), Germany (the NRW / Saarland /
Schleswig-Holstein cabinet decisions), Serbia (RIK + the 9/27
resignation), Brazil (Senate voter guide), South Africa (SAnews
proclamation), Taiwan (CEC via the Taipei / Tainan city notices),
Louisiana (Secretary of State, Act 7 of 2026), 2 U.S.C. s.7 and the state
and county election offices for the rest. Four research agents gathered
the sources; I re-read the deciding page myself for every programmed
election. Each row carries the vote's IANA zone and the cutoff is the
earlier of 00:00 ET and local midnight (Serbia 22:00Z Oct 24, Germany
22:00Z the day before, Taipei 16:00Z Nov 27, Brazil 03:00Z Oct 4; US and
Canadian votes 00:00 ET). The verified day REPLACES the Kalshi-derived
cutoff (apply_series_cutoff_adjustments), a hand event_start_overrides
entry may still pull it earlier, and the ticker-date pre-filter skips the
family (refresh_universe + imm_quote_gaps). Kalshi's ticker / occurrence
are only compared; an EARLIER Kalshi date is logged once ("Kalshi dates
the vote ... re-check the row"). No row -> stood down.

WHAT VERIFICATION CHANGED. Kalshi's dates matched for Canada, Germany,
Brazil, Taiwan and the US races. They were wrong on:
- KXSERBIAPRES-26DEC27: Vucic resigned 2026-09-27 to lead SNS in a snap
  PARLIAMENTARY election on Sun 2026-10-25; the presidential vote is not
  called, must follow its call by >= 30 days and be held by Dec 26/27.
  26DEC27 is that deadline. The row stands down at 00:00 Belgrade Oct 25:
  the parliamentary result is the first thing the presidential market
  reprices on, and no presidential round can come before Sat Oct 31. Move
  the row once the presidential date is called.
- KXPUNJABASSEMBLY-27FEB20: unscheduled (term ends 2027-03-16; 27FEB20
  is 2022's poll date). No row: dark until the ECI announces.
- KXJOHANNESBURGMAYOR-26DEC31: proclaimed for Wed 2026-11-04 (a stale
  Dec 31 placeholder on Kalshi).
- KXPRAGUEMAYOR-26OCT10: the public vote is Fri 9 / Sat 10 Oct; the row
  uses Oct 9 (the assembly picks the mayor later).
- House seat / district and AG markets: Jan 2027 on Kalshi (the first
  cut's `*-26` row already fixed these; now each family has its own row).
Louisiana's Nov 3 OPEN House primary is real (Act 7 of 2026 after
Louisiana v. Callais; open general Dec 12). Henderson is live Nov 3
(Romero 49.75% in June, short of a majority).

SAFE-JOIN OFF on the election archetype (IMM_ELECTION_SAFE_JOIN=1
restores). Measured first: at 00:53Z all 306 resting election orders sat
at or above their side's reward reference (the price where cumulative
depth reaches target/5 -- 200 on a 1,000 target) with fewer than target/5
contracts ahead at better prices; 270 at the touch. At-ref placement caps
safe-join at the reference, so it was not costing weight; off, the
family places exactly like the default book, and a thin tight side with
no reference joins the touch instead of resting two ticks back.

LIVE (run cf0ac55e, config 984f1238, 01:22:39Z 9/29; 718 orders handed
over): the snapshot's election cutoffs are exactly the table's -- US
2026-11-03 05:00Z, BC 10-24 04:00Z, Quebec 10-05 04:00Z, Brazil 10-04
03:00Z, Serbia 10-24 22:00Z, Saarland / Schleswig-Holstein 2027-04-17
22:00Z, NRW 2027-04-24 22:00Z; Punjab stood down (4 markets `cutoff`,
orders pulled, no position). 153 selected, 72 payout_floor. No "Kalshi
dates the vote earlier" line. Window audits: 01:24Z 301/302 and 01:28Z
304/306 at full weight; each straggler was a book that had just moved and
re-pinned within a cycle (NRW SPD 4 -> 5c at 01:26:09; Alaska DBRO-12 /
-16 45 -> 50c / 49 -> 54c at 01:28:25). The global at-ref tolerance is
already 0 (IMM_ATREF_PRICE_TOL), so any reference move re-pins.

WATCH / CHORES:
- Serbia: add the presidential date the day it is called.
- Punjab: add a row when the ECI announces (expect early January).
- A new county / city / country Kalshi lists is dark until a checked row
  is added; the fail-closed line names it in the log.
- KXVOTEGENERAL-CAWEALTHTAX26YES (California wealth-tax measure vote
  share) has no row -- unverified, unprogrammed.

## 2026-09-29 early — Toxic-flow side halt: two confirmed pick-offs on one side halt that side for 30 min (Jack)

Jack: "i need a mechanism to halt a side/market when there is toxic flow /
adverse selection in the form of repeatedly getting picked off going in
one direction (maybe the thing i already have on MENTION markets does
this?)". Commit c4d618d.

THE MENTION THING DOES NOT. EVENT_DEPTH_GATE (KXTRUMPMENTION + undated
mention series via MENTION_NO_CUTOFF_GATE) detects a LIVE BROADCAST --
thin side, one-sided book, a settle-grade jump, or our book moving 15+
contracts in ONE cycle -- date-armed, and stands the whole event down.
The old per-market fill-burst / mid-move breakers are off since 7/21
(IMM_BREAKERS). The only general directional brake was the inventory
skew (halve the accumulating side at 30 net, pull it at 60): keyed on
position, not flow.

THE RULE (TOXIC_*, _toxic_note_fill / _toxic_confirm / toxic_side_halted):
every maker fill of ours that is not a 1c/99c pad and not a taker fill is
queued; TOXIC_CONFIRM_SECS (300) later, on a full cycle, it is judged
against last_mark -- only a mark refreshed THIS cycle (the market was
quoted, or we hold it and _refresh_marks re-read it); no fresh mark within
an hour of due -> dropped unjudged. A PICK-OFF = the mark >=
TOXIC_PICKOFF_CENTS (5) against the fill. TOXIC_PICKOFFS (2) on the same
side of a market inside TOXIC_WINDOW_SECS (24h) halt that side for
TOXIC_HALT_SECS (30 min): its quotes (rungs AND pads) leave `desired`
right before diff_orders, which cancels what rests; the other side keeps
quoting; both halted = the market is out. Pending checks, pick-off
counts and halts persist. Each halt logs "TOXIC <ticker> BID|ASK picked
off Nx ..." and raises a non-urgent alert (daily summary). The one-sided
coverage page skips a market with a halted side.

MEASURED BEFORE BUILDING (scratchpad toxic_load/toxic_sim*.py): all 8,078
non-pad maker fills 9/6-9/29, markouts off the marks log, 30m total
-$2,657 (-1.6c/ct). One-direction runs: singles carry 47% of it; runs of
3+ ~25% (-$20..-$50 per run, bounded by the skew caps). Net of the reward
a halted side forgoes (half the market's est reward for the halt):
- a raw "N same-side fills" rule: ~$390 saved at 30m for 914 halts on 650
  markets, with +$1,155 of profitable fills blocked -- a loser;
- the markout-confirmed rule shipped here: best at 5c / 2 / 24h / 30 min,
  ~12 halts/day, $53 saved at 2h ($125 at 30m) vs ~$49 of reward --
  break-even; 4h halts cost ~$455 of reward for ~$87 saved. Same on
  tight books only (spread <= 6c).
Where halting clearly paid -- state gas dailies (now GasBuddy-gated),
hourly temp (blocked), KXRT release week (cutoff), live mentions (gate) --
a family rule already exists. So this ships as a circuit breaker for the
NEXT episode (new families like the 9/28 elections have no fill history),
tuned not to cost rent.

KNOBS (launcher env, task-level restart): IMM_TOXIC_HALT=0 kills;
IMM_TOXIC_PICKOFF_CENTS, IMM_TOXIC_CONFIRM_SECS, IMM_TOXIC_PICKOFFS,
IMM_TOXIC_WINDOW_H, IMM_TOXIC_HALT_MIN.

WHAT THIS DOES NOT DO: event-wide propagation (a ladder picked off on
several strikes halts each strike on its own evidence); a halt shorter
than the move (30 min, then the side re-joins; a third pick-off re-halts);
anything about the single isolated pick-off, which is most of the cost.

Tests: TestToxicSideHalt (side mapping, pads / takers never count, the
second pick-off halts that side only, asks mirror, sub-5c / favourable
moves never count, due time + fresh mark + too-late drop, window expiry,
kill switch, persistence) and TestDryRunCycle.test_toxic_side_halt_
cancels_only_the_picked_off_side (end to end: bids cancelled, asks
re-quote, bids back after the halt). 1,485 green (unittest discover).

### 2026-09-29 early — Event-wide toxic halt + the 7:15 ET "what was halted" email (Jack)

Jack: "yes add event-wide halting too", "and give me a daily morning email
on what was halted in the prior day". Commits 9d6f23d, 8faba89 (email
formatting); live run 590456a8 (config ce77f450) from 03:02:56Z.

EVENT RULE (TOXIC_EVENT_*): pick-offs on TOXIC_EVENT_MARKETS (2) DIFFERENT
markets of one event inside TOXIC_EVENT_WINDOW_SECS (60 min) take the whole
event -- every market, both sides -- out for TOXIC_EVENT_HALT_SECS (30
min). Same pick-off definition as the side rule; both rules judge every
pick-off. Unlike the side rule this one PAYS on the backtest
(toxic_event_sim.py: on top of the side rule, net of the bot's own est/day
over the event's selected markets from the selection snapshots): ~9.5
event halts/day, $304 saved at 2h markouts vs $163 of reward, net +$141
over 22.5 days; every 2-market setting from 15-180 min windows / 15-60 min
halts nets positive. By family: state gas +$114, mentions +$80, diesel
+$18, hourly temp -$49 (blocked since 9/17). IMM_TOXIC_EVENT_MARKETS=0
turns it off alone. A halted market (side or event) is exempt from the
one-sided coverage page AND the zero-share bench (which would otherwise
stretch a 30-min halt into an hour's deselection).

RECORDS: every pick-off / side_halt / event_halt goes to the toxic_halts
sink (STATUS_DIR/toxic_halts_<UTC date>.jsonl) with the est reward the
halt forgoes. The 02:06Z side halts below happened under c4d618d, before
the sink existed; they were BACKFILLED from the log + fills/marks sinks
(rows carry "backfilled": true).

EMAIL: send_imm_toxic_halts.py, task "KL imm toxic-halts" daily 7:15 AM ET
(register_imm_toxic_halts.ps1: the quote-gaps task's own command line, 5
min earlier). Prior ET day: event halts (markets picked + side, markets
taken down, forgone reward), side halts (pick-offs fill->mark, forgone
reward, what the price did DURING the halt -- still moving against =
helped, moved back = cost rent), most picked-off markets incl. ones that
never halted. Sends every morning, "none" included; --dry / --test /
--day YYYY-MM-DD. Output ASCII (cp1252 console).

FIRST LIVE HALTS (9/28 22:06 ET, side rule): KXLAHOUSE1R-26NOV03 LA05 and
LA06 BIDS -- filled 20 @34c and 20 @64c at 01:57:52Z, re-joined lower and
filled again @31c / @61c at 02:00:19Z; marks 20c / 51c. During the halt
LA05 held ~19-20c and LA06 drifted to 48c; LA06 bought 10 more @46c at
02:52Z after the halt ended. Two strikes of one event inside 3 minutes =
exactly what the event rule (not yet live then) now halts whole.

WOULD IT HAVE STOPPED THE FAMILIES THAT GOT THEIR OWN FIXES? (Jack asked;
toxic_family_check.py, both live rules replayed over each family's fills,
2h markouts):
- KXRT (9/10-9/28): loss -$651; halts prevent $13 (2%). NO -- the loss is
  slow drift as reviews land over days; the release-week cutoff is the
  right tool.
- state gas dailies (9/6-9/14): loss -$152 at 2h (the settled loss was
  ~-$612 over 9/6-9/11); halts prevent nothing. NO -- the flow traded
  against the intraday station data and the loss lands at the 3:20am
  print, not in a 5-minute move on a 30-64c-wide book.
- hourly temp (9/16-9/17): 54% of the 30-min markout loss prevented, but
  the halts forgo more reward than they save (net -$87). PARTLY, at a loss
  -- the block was right.
- mentions (9/6-9/29): loss -$449; halts prevent $137 (31%) for $66 of
  reward (net +$71), almost all KXTRUMPMENTION (-$312, $130 prevented);
  MAMDANI (gate off by Jack's choice) $4 of $113. PARTLY.
So the halts are a complement for sharp, cross-strike episodes, not a
substitute for the family rules; slow or settlement-time adverse
selection is invisible to a 5-minute pick-off detector.

Tests: TestToxicSideHalt event cases (two markets -> whole event incl. an
unpicked sibling, other events untouched, sink rows; same market twice =
side halt only; window / rule-off; persistence), TestDryRunCycle.test_
toxic_event_halt_cancels_every_market_of_the_event, test_send_imm_toxic_
halts (ET-day window across UTC files, pick-offs per halt, price during
halt from marks, forgone reward, quiet day, --dry sends nothing). 1,492
green (unittest discover).

## 2026-09-28 late — Mortgage rates: weekly KX30YMORTW blocked; year-end / how-high-in-a-year quoted against a PMMS fair (Jack)

Jack, on KX30YMORTW-26OCT01 / KXFM30YMTG-26EOY / KXFM30YMTG-27EOY /
KXMORTGAGERATE-27DEC30: "should i quote mortgage markets in realtime", then
"yes" to "block KX30YMORTW and build a fair-value gate for KXFM30YMTG and
KXMORTGAGERATE? The gate would stop quoting if its data went stale. It would
start from Freddie's latest weekly number and use the weekly market's own
implied rate as a free live reading".

THE WEEKLY IS BLOCKED (SERIES_BLOCK_PATTERNS += KX30YMORTW). Freddie Mac's
Thursday-noon PMMS averages Thursday-Wednesday applications (Kalshi's rules)
and the market opens Thursday 17:00Z, 13 hours INTO that window, so the
answer is being written the whole time it trades -- no pre-window exists.
Strikes are 1bp apart against a median 6bp weekly move (p90 16bp, since the
Nov-2022 method change). Optimal Blue's daily lock index (FRED OBMMIC30YF,
posted about a business day late) predicts the print change to 9.3bp RMSE
with nothing, 4.1 by Friday, 2.7 by Monday, 1.7 by Wednesday (194 weeks);
the informed side reads MBS live. Measured on our book (open-scan tier, the
only way in -- Kalshi's series page names the NY Fed SOFR page as the
source, so the live-source keyword screen never saw Freddie Mac): SEP24
bought YES 7.05/7.07 at 17c/8c, printed 7.03 (-$8.40); OCT01 sold YES
7.13-7.15 at avg 70.5c, ~96.6c on 9/28 (-$47.50 MTM, settles 10/1) =
-$55.90 on 242 contracts vs ~$15.70 estimated rent. The OCT01 position rides
to settlement (block semantics).

THE LONG-DATED FAMILIES (KXFM30YMTG, KXMORTGAGERATE; listed 9/28 ~18Z, $100
per strike for the first period to 10/04 03:59Z, target 1000, df 0.5) are
allowlisted only with the gate (_MORT_LIVE; IMM_MORT_ENABLE=0 or
IMM_ALLOW_MORT_SERIES="" takes them out). Two shapes are modelled, read from
the rules text and cross-checked against the ticker:
- FINAL KXFM30YMTG-<YY>EOY: the year's last PMMS release above K.
- MAX KXMORTGAGERATE-<YYMMMDD>: any release published in the year above K.
Everything else in those series is an in-year touch market (KXFM30YMTG-
26DEC31 "below 5.75% ... between Issuance and Dec 31", KXMORTGAGERATE-26DEC
"above 6.6% in 2026") whose weekly prints are live settlement events -- the
weekly's problem -- and is stood down (fail closed).

FAIR (mortgage_fair.py, new; refresher thread "mort-fair", in-memory
snapshot + run-logs/incentive-mm/mortgage_fair.json). Kalshi-only data,
signed through fair_reader() (public fallback): the open KX30YMORTW ladder
every IMM_MORT_FAIR_REFRESH_SECS (120), the last print (a settled weekly
market's expiration_value, e.g. '7.03') and the family's open markets every
15 min.
- X0 = the median print implied by the open weekly ladder (mids of strikes
  two-sided within 20c, made non-increasing by isotonic fit, the 50c
  crossing interpolated between strikes <= 6bp apart), dated at that
  event's Thursday. Refused if more than 30bp from the last print or older
  than 20 min. Fallback within 24h of a release: the print itself. A print
  older than 8 days, or neither source: no X0 -> every market stands aside.
- The weekly PMMS as a driftless Gaussian walk with the n-week sd fitted to
  2000-2026 (10.5bp x n^0.536: 22/41/58/85/98bp at 4/13/26/52/65 weeks,
  ~1.2x the sqrt(n) scaling -- weekly changes are autocorrelated) plus 3bp
  for the anchor. FINAL: 1 - Phi((K + 0.005 - X0)/sd). MAX: the first
  print's law plus the reflection principle for the rest of the year with
  the barrier raised 15bp for weekly monitoring -- calibrated: the textbook
  0.58-step shift read up to 7c high against a sign-symmetrized 13-week
  block bootstrap of 2000-2026 changes; 15bp is within 2.5c on strikes
  7.00-9.00 from three starting points. FINAL is within ~3c of the same
  bootstrap. (The raw bootstrap carries the sample's -7bp/13 weeks drift;
  the model takes no view.)

GATE (mort_gate, quote loop after the Vercel gate, guard "mort_fair"; guard
sweep 29 -> 30). Stand aside (cancel) on no read, a read older than
IMM_MORT_FAIR_TTL_MIN (20), no X0, a market the model does not cover, a
ticker kind/year that disagrees with its rules, or a touch fighting the fair
by more than IMM_MORT_FAIR_TOL_CENTS (15) on the adverse side. Otherwise
quote with EVERY bid <= fair - 15c and EVERY ask >= fair + 15c
(mort_cap_quotes, applied last -- after pads and the sub-penny snap; a rung
past its bound moves to it, a pad past it is dropped). A side with no room
(fair above 84c / under 16c) is not quoted. The reward estimate runs the
same gate (probe ladder), so a stood-aside market estimates at zero and is
not admitted. Family size x3 (IMM_MORT_SIZE_MULT), band 1-99c,
cutoff_from_close_min=0.

CUTOFF (mort_cutoff_utc in apply_series_cutoff_adjustments, from the
ticker): 00:00 ET IMM_MORT_CUTOFF_BUFFER_D (7) days before the measurement
week of the first print that can settle -- FINAL 2026: 12/17/2026 05:00Z,
FINAL 2027: 12/16/2027, MAX 2027: 12/24/2026 (inside the year every weekly
print can settle a near strike; quoting there needs its own rule).

DRY CHECK 9/29 01:40Z (production env: one 20-lot rung x3 = 60 a side, cap
450/market): X0 7.218 (anchor OCT01), print 7.03. 26EOY T7.00 fair 69.6 on a
66x89 book -> bid capped at 54, ask joins 89; 27EOY / MAX-27 bids join the
placeholder touches (23-56c) 10-60c under fair, asks join 97-99; deep strikes
one-sided (e.g. MAX-27 T7.00 fair 88.6: bid 40, no ask). All 11 in-year
touch markets stand aside. Modelled rent at 9/28 books (the bot's own share
formula, others static): ~$12/day at x3 across 23 strikes; x10 (200 lots)
~$34/day.

KILL SWITCHES: IMM_MORT_ENABLE=0 (family out); IMM_BLOCK_SERIES_PATTERNS
(the weekly block lives in its default).

WATCH AFTER DEPLOY: startup "mortgage gate: KXFM30YMTG,KXMORTGAGERATE
fail-closed ...", "mort-fair refresh: X0 7.2xx (anchor KX30YMORTW-...), N/51
markets priced", run-logs/incentive-mm/mortgage_fair.json refreshing,
"mortgage stand-aside / resume" lines, no KX30YMORTW orders, no mortgage
order within 15c of the status file's fair. Thursday 12:00-13:00 ET the
weekly ladder rolls: X0 falls back to the fresh print, then the next ladder.

Tests: TestMortgageFairGate (enrollment, the weekly block, cutoffs by shape,
every fail-closed reason, the caps incl. pads and sub-penny, the loop
quoting 50/90 at fair 70, capping bids at 45 at fair 60, lifting asks to 95
at fair 80, standing aside at fair 30); test_mortgage_fair.py (20: rules,
calendar, the sd fit, FINAL/MAX against the bootstrap, the anchor incl. the
9/28 ladder -> 7.218, X0 fallbacks, entries, the watch end to end without
the network). 1,500 green (unittest discover).

## 2026-09-28 late — Saturday x2 is armed (gate PASS); the Monday email now watches for the next step (Jack)

Jack: "based on your email earlier, sounds like i should move the saturday
multiplier to 2x? if so, do it", then "and keep an eye on if i should
increase it even further, after a few saturdays at 2x".

NOTHING TO MOVE FOR 2x. The 9/28 07:40 ET tracker run wrote
sat_mult_gate.json = PASS, mult 2.0, effective 2026-10-03 (every check green
on 9/12, 9/19, 9/26: G1 net above same-week weekdays in every block, G2
positive, G3 mark-out within 2c, G4 pooled settled +9.26c/fill on 3,981
settled contracts). The live bot logs "Saturday gate: verdict PASS: x2 on
Saturdays from 2026-10-03" on every refresh; no launcher change (the gated
level is the code default IMM_SAT_SIZE_MULT_GATED=2.0).

STEP-UP WATCH (imm_saturday_tracker.evaluate_stepup; report only -- nothing
moves the bot's knob). While a step-up is in force (the verdict the bot will
run next Saturday), each Monday scores the Saturdays that ran AT it (day-block
cycle-log hour_mult to the nearest 0.25: x1.5 logs ~1.49, x2 ~1.98; a
Saturday split between levels matches neither) with the gate's G1-G4 against
their own weeks' weekdays, plus DILUTION: rent per resting contract-hour,
Saturday / its weekdays, pooled over the step-up Saturdays, divided by the
same ratio at the level below (x1.5: 0.91 over 9/12-9/26). 1.0 = the extra
size earned pro rata, 0.75 = it earned nothing; the bar is 0.85 (rent
elasticity ~0.44 vs the ~0.2 break-even of the 9/26 analysis). Verdicts, in
the email body and the subject:
- WATCHING "n of 3 Saturdays at x2 judged, none degrading" (subject "x2 watch n/3");
- DEGRADED any x2 check failed -- consider x1.5 back ("verdict": "FAIL" in
  the gate file; the bot re-reads it on its next refresh);
- HOLD three pass but the rent kept < 0.85 (the size is crowding its own share);
- RAISE three pass and the rent held: x2.5 worth trying (IMM_SAT_SIZE_MULT_GATED=2.5
  in the launcher + "mult": 2.5 in the gate file + restart_imm.ps1 -Task). At
  x2.5 the Saturday quiet hours (0-9 ET x2) run x5, where TOTAL_SIZE_MULT_CAP
  leaves no deep-reference boost.
The first x2 Saturday is 10/3; the earliest RAISE is the 10/19 email.

Tests: StepUpWatchTests (OFF, WATCHING 1-2/3, RAISE pro rata, HOLD at 0.75,
DEGRADED on one failed check, levels incl. a split Saturday and an x2.5
comparison against x2, text/HTML/subject).

## 2026-09-29 — OpenRouter token usage uncapped: every strike the OR fair gate clears quotes (Jack)

Jack, on "why is KXTOKENUSE-26OCT05 only quoting 3 markets?": "remove the
3-strike cap on OpenRouter".

WHY IT WAS 3. EVENT_TOP_N =KXTOKENUSE:3,=KXTOKENUSEM:3 came with the gate on
9/27 by analogy with the one-number families (gas, diesel, *CC), not on an
instruction; sticky slots then kept the first three admitted (T146, T158,
T170) while T166 ($3.56/day) and T156 ($2.73/day) ranked above them. The
13:57Z 9/29 snapshot: 15 strikes, 3 selected at ~$6.95/day modelled, 10 cut
by event_top_n at ~$22.93/day modelled, 2 extreme_mid (T142/T144 at 97-98c).
The monthly KXTOKENUSEM-26OCT26 was not cap-bound (8 payout_floor, 1 zero
yield, 1 selected at $0.06/day).

CHANGE. =KXTOKENUSE:0,=KXTOKENUSEM:0 (0 = no cap, exact names so
KXTOKENUSED stays untouched). The OR fair gate is unchanged: fail closed
without a fresh read, out once the window is complete, the 10-minute hold
after each new day, stand aside when a touch fights the fair band by >15c.

RISK NOTED TO JACK BEFORE THE CHANGE. The market priced the Oct 5 week's
total ~4T under the model (median ~160T vs mu 164.4T, sigma 16.9T, 1 of 7
days known); the week's one fill was a YES buy on T170 40 @ 37c that marked
23c (-$5.60). Pre-gate open-scan fills lost $37.93 on 26SEP28 T150/156/158
(YES buys as the total came in low); the gate's one settled trade made
+$7.20 (T146 NO). The bot's per-market reward estimate has matched Kalshi's
credits on the one paid week (KXTOKENUSE-26SEP21: est $9.35 + $4.01,
credited $9.35 + $4.02); the 9/27 note's "~$9-10/day vs ~$80-100/day" was
the pre-build projection against the live estimate, not against payouts.

Watch after deploy: selection rows for KXTOKENUSE-26OCT05 move from
event_top_n to selected over the next refresh or two (the admission clock
applies); or-fair stand-asides by strike; fills by strike against the
fair file's mu.

## 2026-09-29 — Sports ladders / escalators x3 -> x4 (Jack)

Jack: "increase LADDER and ESCALATOR families to 4x multiplier, from the 3x".

CHANGE. The archetype KXNFLLADDERREC's size_mult default 3.0 -> 4.0 (env
IMM_SPORTS_LADDER_SIZE_MULT; the launcher does not set it, so the code sync
and the bot's own restart carry it; every pattern sibling clones the
archetype). Through applied_mention_mult, at the launcher's IMM_LEVELS=0:20 /
MAX_POSITION 150 / MAX_EVENT 1,000 (checked with $ProbeEnv and the live
Saturday verdict):
- weekday 80 a side, 160 in the 0-9 ET quiet hours (x2);
- Saturday from 10/3 (the gated x2 step-up) 160 daytime, 320 in 0-9 ET;
- per-market cap 600 (was 450), per-event cap 4,000 (was 3,000);
- the estimator's hypothetical ladder and the floor projection scale with it.
Unchanged: KXTRUMPAPPROVE's own x3 (IMM_TRUMPAPPROVE_SIZE_MULT), the family's
1-99c band, safe-join and $1.20 fresh-entry bar, TOTAL_SIZE_MULT_CAP (x5 on
hour x deep-reference only; hour x Saturday x family is not capped).

Collateral at the 14:18Z 9/29 refresh: ~$8.2k ladder + $16.9k inventory
reserve of the $50k budget (all families).

Tests: the ladder-family enrollment test reads x4 rungs and caps and pins
KXTRUMPAPPROVE at x3. 1,550 green (unittest discover).

## 2026-09-30 — Mortgage gate reads the rate past the end of the weekly ladder (Jack)

Jack, after the live check showed the mortgage gate standing the whole
family aside: "yes want that".

WHAT HAPPENED. From the 9/29 14:17Z deploy the gate quoted nothing: rates
rose past the OCT01 weekly ladder's top strike (T7.23 bid 85 / ask 88, T7.22
87 / 90 -- Kalshi listed nothing higher), so no mid crossed 50c, the anchor
failed ("ladder 7.04-7.22 does not bracket 50c"), and the print fallback
only covers 24h after a release (9/24's was 5 days old). Fail closed, as
designed, but dark until Thursday's print.

CHANGE (mortgage_fair.ladder_anchor; anchor_from_ladder keeps its (x, why)
shape). No crossing -> read the edge strike: median = K + s *
Phi^-1(P(print > K)), s = IMM_MORT_ANCHOR_EXTRAP_SD_BP (3bp: the spread of
the week's print as a mid-week market sees it -- a daily lock index pins it
to 2-4bp RMSE by Monday), only while the edge strike's fitted mid is within
[5c, 95c] (IMM_MORT_ANCHOR_EXTRAP_EDGE_C; at 95c the read is at most 1.6 sd
out, beyond it the ladder only says "higher") -- else fail closed as before.
The 30bp max-deviation-from-the-print check still applies. The X0 source
says so: "anchor KX30YMORTW-26OCT01 (past the top strike 7.23 at 86.5c)";
the anchor dict and the status file carry `extrap` {k, p, side}.

LIVE READ 9/30 ~03:10Z: X0 7.263 (was none), 39 of 50 markets priced (the
11 in-year touch markets stay unmodelled); 26EOY T7.00 fair 73.2c, T7.25
50.8c; 27EOY T7.00 60.3c; MAX-27 T7.25 79.6c. An X0 error of a few bp moves
these long-dated fairs ~1c/bp at most, inside the 15c margin.

Tests: test_mortgage_fair (22): the 9/29 ladder -> 7.263, the bottom end,
the inclusive 95c/5c edge, a crossing still wins, the X0 source note, the
last-good-anchor test on a ladder past the edge. 1,568 green (unittest
discover).

### 2026-09-30 — KXTOKENUSEM: the October month's window parses (Jack: "allowlist KXTOKENUSEM and use the openrouter algorithm to quote it")

KXTOKENUSEM was already allowlisted (_DEFAULT_AI_USAGE_SERIES), in
OR_FAIR_SERIES and uncapped (4136824) -- but it had not quoted since the
October event listed. KXTOKENUSEM-26OCT26 words its window "October 2026
(Sep 28–Oct 25)": no "measured", no year inside the range. parse_window()
knew only "measured August 31 - September 27" and "Sep 21–27, 2026", so the
event got NO window, openrouter_fair.json carried only the weekly
(KXTOKENUSE-26OCT05), and the six selected October strikes (T625-T800) stood
aside "no OpenRouter read" -- silently: an unparsed event was not even
listed as missing.

Fix (openrouter_fair.py): a third pattern for a bracketed range without a
year, "(Sep 28–Oct 25)", the year taken from the close (a Dec–Jan wrap
works); windows_from_markets/fetch_windows take an optional `unparsed` list
and write_fair_file puts those events in "missing", so the refresher's
"or-fair refresh: N events with a read, M without one" shows any future
wording change. Live dry run 10/01 02Z: KXTOKENUSEM-26OCT26 mu 807.3T sigma
151.6T (3 of 28 days known, 73.2T; base 22.6T/day) -> fair 99% at T450 ...
52% at T800, 39% at T850; the October books are 70-88c wide, so the 15c
band gate passes the selected strikes. No incentive_mm change. Tests: the
October wording (rules_primary and rules_secondary), a Dec–Jan wrap, a
bracketed weekly, a single-date bracket rejected, the unparsed list, and an
end-to-end write with one parsed and one unparsed monthly; 915 green.

## 2026-10-01 — Gas trial: national + diesel dailies plain, 1pm-midnight ET only; state dailies blocked; reports at 1 and 2 weeks (Jack)

Jack: "adjust all three to trade 1pm-midnight. and report on results at the
1 week and 2 week marks", then (asked plain vs gated, and whether to keep
the states, given they did WORST in the afternoon) "National + diesel only".

WHY. The 10/01 review (scratch scripts saved to Documents/KL-data/
gas-analysis-2026-10-01/): the GasBuddy gate's fair forecast the AAA print
worse than the market's own price (states Brier 0.084 vs 0.011, national
0.095 vs 0.044, diesel 0.36 vs 0.044); state errors ran 1.3-1.9x the gate's
sigma; the gated bot since 9/27 had 0 state fills, 12 national fills
(-$6.62 at mid) and 0 diesel fills, with every market's estimated reward
under the $1 floor. September's PLAIN quoting by ET band (settled fills):
national 08-13 -$197 (-2.5c/ct) vs 13-24 +$126 (+2.0c/ct, +$112 of it
21-24 ET; +$150 Sep 1-15, -$24 Sep 16-27); diesel 08-13 -$215 (-7.8c/ct)
vs 13-24 +$13; states 08-13 -$296 vs 13-24 -$467 (13-17 ET -14.2c/ct, the
worst band). In-sample: the window was picked on the same data.

CHANGE (all defaults; no launcher edit, the code sync + restart carry it):
- GB_STATE_QUOTE (IMM_GB_STATE_QUOTE, default 0): the state dailies are
  pattern-blocked again (the 9/14 pattern) while IMM_GB_FAIR_ENABLE stays 1,
  so the GasBuddy refresher keeps writing gasbuddy_live.jsonl and the fair
  file. IMM_GB_STATE_QUOTE=1 re-gates them.
- GB_NATGAS_ENABLE / GB_DIESEL_ENABLE default 1 -> 0: KXAAAGASD and KXDIESELD
  quote PLAIN (no GasBuddy gate). =1 puts each gate back.
- GAS_TRIAL_SERIES (KXAAAGASD,KXDIESELD) get blackout_et 00:00-13:00 ET
  (IMM_GAS_TRIAL_BLACKOUT_ET; it contains the 03:05-04:00 print blackout,
  and the blackout path cancels resting orders): they quote 13:00 ET to the
  close. The weeklies, monthlies and KXDIESELMONAK keep 03:05-04:00 only.
  IMM_GAS_TRIAL_BLACKOUT_ET="" = full-day plain quoting again.
- Startup line "gas trial: KXAAAGASD,KXDIESELD quoted plain outside the
  no-quote window 00:00-13:00 ET; state dailies blocked" -- the report dates
  the trial from its first appearance. The gb-fair gate line now names what
  is gated ("NOTHING gated (state dailies blocked)").

REPORTS. imm_gas_trial_report.py (read-only; tests test_imm_gas_trial_report):
per family -- ET hours and markets quoted, placements before 13:00 ET and on
state dailies (both must be 0), fills by ET band and side, trading P&L
(settled at Kalshi's result, open at mid, signed reads), the bot's reward
estimate rebuilt from the cycle logs with the $1/market floor, Kalshi's
credits where a statement has been pasted, net, September's same-hours
baseline, and an advisory KEEP / STOP (STOP = trading loss larger than the
reward). Task "KL imm gas-trial" (register_imm_gas_trial.ps1): daily 07:45 ET;
emails only on the first run at/after day 7 and day 14 (markers
gas_trial_sent_1w / _2w; a missed 1-week folds into the 2-week). --dry
prints; --test sends now. Unregister after the 2-week report.

Tests: 9 existing gas tests moved to the new defaults (state blocking reads
GB_STATE_QUOTE; TestGasBuddyFairGate runs under IMM_GB_STATE_QUOTE=1; the
print-blackout test pins 00:00-13:00 for the two trial dailies);
test_imm_gas_trial_report (7).

## 2026-10-01 — Data center count family allowlisted, 3 strikes per event (Jack)

Jack: "allowlist the datacenter family, use the live feed to quote realtime.
and start the logger so we can hone the fair value. max 3 markets per event."

WHAT THEY ARE. KX<state>DATACENTERS: "will <state> have at least N data
centers this year?", settled on the whole-number count Data Center Map's
directory page shows for the state at 11:59:59 PM ET 2026-12-31. Nine states
listed 9/30 (AZ CA FL GA NY OH PA TX VA), six strikes each just above the
10/1 counts (TX 537 vs 540-650, VA 674 vs 680-800, PA 174 vs 180-205, ...);
$100 per strike per 4-day period, Pennsylvania $500 per 2 weeks.

WHY. Pennsylvania has quoted in the open-scan tier since 9/9 under the scan
guard set (safe-join, no rate bar, global ladder): 11 fills, -$14.45 marked to
mid (Kalshi replay of the bot's order ids == dashboard, to the cent) against
$11.02 credited for 9/9-9/20 (model $10.83) + ~$18.51 modeled since: ~+$15 net,
the rewards about twice the pick-off losses.

CHANGE (defaults; no launcher edit):
- datacenter_series(): KX + a real US state code + DATACENTERS, admitted by
  _allowed. IMM_DATACENTER_ALLOW=0 removes the family.
- DATACENTER_ARCHETYPE KXTXDATACENTERS = the scan guard set PA ran under
  (safe_join, min_est_per_day 0, global ladder and caps); every member clones
  it through FAMILY_OVERRIDE_PARENTS ("predicate").
- EVENT_TOP_N gains "*DATACENTERS:3".
Dry check 10/1 02:50Z (imm_quote_gaps.classify_and_estimate on this code with
the launcher env): all nine events allowed; eight enter once the 10-minute
admission clock holds (est ~$54/day for the eight at 3 strikes, a MODEL);
deep strikes screened extreme_mid; NY zero est (empty books).

LIVE COUNT GATE + LOGGER (Data Center Map gave permission 10/1 -- their
terms.html otherwise forbids programmatic reads: "only direct, human access
via standard web browsers is permitted unless otherwise authorized by a
licensing agreement"; keep the pace polite):
- datacenter_fair.py reads each state page (one pass ~1 s apart, browser UA
  + "KL-datacenter-fair/1.0"). The count is the page's "We currently have N
  data centers listed" line (the page title carries the same N; the
  per-market breakdown does NOT always sum to it -- Texas 533 vs 537 on 10/1).
  Writes run-logs/incentive-mm/datacenter_fair.json (count, read_at,
  changed_at, prev_count per state) and datacenter_counts_YYYY-MM-DD.jsonl:
  a "read" row per state per pass, plus a "detail" row (per-market counts +
  the page's MW stats: live / planned / pipeline / built-out) on the first
  read of a UTC day and whenever the count or breakdown changes. That log is
  the growth history the year-end fair value will be fitted from.
- Refresher thread "dc-count" every IMM_DC_REFRESH_SECS (180), states
  IMM_DC_STATES (the nine) + any state with a live program.
- Gate (dc_gate_reason, guard "dc_count"): stand aside with no read or one
  older than IMM_DC_TTL_SECS (900, fail closed); for IMM_DC_HOLD_SECS (900)
  after the state's count changes; and once count >= strike ("dc_decided",
  also a _screen reason so it never takes one of the event's 3 slots, as are
  dc_no_read / dc_stale). NO fair-value band yet: with no growth history any
  fair would be a guess, and the 10/1 gas review found a guessed fair worse
  than the market's own price. Kill switch IMM_DC_GATE_ENABLE=0 (the family
  then quotes blind, as Pennsylvania did in the open-scan tier).
- WATCH: startup line "dc-count gate: ...", "dc-count refresh: 9 states
  read", "dc-count change: TX 537 -> 538 ..." lines, stand-aside/resume lines.

## 2026-09-30 — OpenRouter market-share gate: KX<AUTHOR>SHARE allowlisted, quoted only against the Market Share chart's own data (Jack)

Jack: "scrape OpenRouter for market share markets", then (after being told
OpenRouter's Terms forbid automated access without permission) "i got
permission".

WHY THIS FAMILY. KX<AUTHOR>SHARE ("If Anthropic scores above 3.1% on
OpenRouter text market share by model author week of Sep 28, 2026") settles
on the Market Share chart of openrouter.ai/rankings at 10:00 AM ET the Monday
after: the author's share of the week's TEXT REQUESTS (Monday-Sunday UTC),
rounded to one decimal; an author the chart folds into "Others" (it names
nine) settles every strike No. A live public feed, so the open-scan tier
rejects the family on `openrouter`. Ten series (Anthropic, OpenAI, Google,
DeepSeek, Qwen/KXBABASHARE, Xiaomi, Z.ai, Mistral, Tencent, Stealth); on
9/30 four had events (26OCT05) with 34 strikes at $200-250 per strike for
the week (~$1,000/day posted), 1000-contract targets.

DATA (openrouter_share_fair.py, new). The chart's own data:
/api/frontend/v1/rankings/modality-chart?routeSegment=text, field
marketShareData -- per week, text requests by the nine named authors plus
"Others", 52 weeks, the current week to date. It reproduces every settled
value checked to the rounding (weeks of Sep 7 / 14 / 21: e.g. Sep 21
deepseek 24.39 -> 24.4, google 20.51 -> 20.5, openai 17.87 -> 17.9, stealth
3.13 -> 3.1, anthropic 2.74 -> 2.7, mistralai 2.45 -> 2.4). The Data API
(tokens only) is NOT this quantity: Google was 4.0% of tokens but 20.5% of
requests that week. The per-model leaderboard (/api/frontend/v1/rankings/
models?view=day|week, `count`; text-output models via the catalog) gives the
run rate and the share of authors folded into Others. The chart is LIVE:
polled every 10 minutes 10/01 03-13Z, its week-to-date took in the day in
progress at 16 cachedAt steps 7-60 minutes apart (2.871B requests at 02:25Z,
3.226B at 12:21Z). The leaderboard is whole days (views day / week / month /
trending, no intraday one); its day view had rolled to 9/30 by 03:00Z. The measured
week is the ticker date (the settling Monday) minus 7: the rules' "week of"
label was a week off on the August events, the ticker matched settlement.

MODEL. Per event, week W: mu = 100 (K_a + r R s_a) / (K_T + r R), K = the
chart's week-to-date through its own timestamp (cachedAt), r = clock days
from that timestamp to the week's end, s_a / R = the plain
average of up to four run-rate estimators (the leaderboard's last day and
trailing 7 days, the chart's week-to-date, and the chart's last ~24 hours
from the snapshots the writer keeps -- 48h of them in the fair file, so the
24h estimator joins a day after a fresh file). sigma = sqrt((vol_a
(r + gap)/7)^2 + (spread_a r/7)^2 + 0.1^2): vol_a = the author's winsorized
RMS week-over-week change of chart share, last 20 changes (deepseek 1.77pp,
google 1.43, openai 1.04, qwen 0.59, z-ai 0.54, anthropic 0.43, mistralai
0.36; raw openai RMS 1.94 is one -6.57 week); spread_a = half the range of
the three estimators; gap = days before a week not yet begun. p_ident =
P(named) from the margin to the boundary author (the 9th-best of the
others). P(YES at K) = p_ident x P(share >= K + 0.05). Linear-in-r sigma and
the three-way rate are design choices, not measured: no intraday history
exists to backtest them. The writer logs each event's forecast hourly to
openrouter_share_preds.jsonl and every complete week's chart shares (first
sight and any revision) to openrouter_share_finals.jsonl -- the calibration
record.

GATE (the OR token gate's shape, fail CLOSED). A share market stands aside
on BOTH sides while: no fresh read (no file, older than
IMM_SHARE_FAIR_TTL_MIN 30); the week is complete (00:00Z Monday); a feed has
stalled -- the chart older than IMM_SHARE_CHART_MAX_AGE_MIN (180) or the
leaderboard a whole day behind (its day view is a day old for a few hours
after 00:00Z every day: that is normal and quotes); its read moved within
IMM_SHARE_FAIR_REFRESH_HOLD_MIN (10) -- keyed to the leaderboard's day, so
once a day, never on the live chart's moves; not on first load; or its
touch fights the band (P
at sigma and at sigma x 0.5) on the adverse side by more than
IMM_SHARE_FAIR_TOL_CENTS (15). Joins the touch unchanged otherwise (no
safe-join, no size multiplier). 3 STRIKES PER EVENT (Jack 2026-10-01: "set
max 3 markets per event"): EVENT_TOP_N gains =KX<AUTHOR>SHARE:3 for the ten
names (exact -- KXOPENSHARE must not cap Vercel's KXOPENSOURCESHARE;
IMM_SHARE_EVENT_TOP_N=0 lifts it), picked by ROI with sticky members; and,
as for the GasBuddy states, two selection screens keep a strike the gate
would stand aside every cycle out of the three -- "share_no_read" (no fresh
read for its event) and "share_fair" (its listed touch fights the band),
both sticky deaths that free a member's slot at the next refresh. A stalled
feed or the daily-roll hold stays the quote loop's (transient). Cutoff = close -
IMM_SHARE_CUTOFF_FROM_CLOSE_MIN (840) = 00:00Z Monday while the read is 14:00Z
(EDT). Logs "share-fair stand-aside <t>: <why>" / "share-fair resume <t>",
"share-fair refresh: N events with a read [-- a feed has stalled, family
stands aside]", guard "share_fair" (the sweep test counts 32 continues with
the data center gate). Refresher thread "share-fair": every
IMM_SHARE_FAIR_REFRESH_SECS (300), every IMM_SHARE_FAIR_FAST_SECS (120) while
a feed has stalled; the chart every call,
the leaderboard every 30 min (every call while behind), the catalog every 6h,
Kalshi's open events hourly through the signed reader. No key.

DRY RUNS. 10/01 03Z (books ~02:40Z, first cut): 32 of the 34 rewarded
strikes inside the band; out GOOG 19.9 (27x31 vs fair 49c) and OPEN 17.6
(49x52 vs 71c). By 13Z the books had moved to the model (GOOG 19.9 60x63 vs
58c, OPEN 17.6 78x81 vs 86c) while the live chart showed OpenAI and Google
at ~22% of the day so far. Final model, 13Z: 33 of 34 inside with no
snapshots yet (out: OPEN 19.3, 31x35 vs 7c; openai mu 18.37 sigma 0.67) and
33 of 34 with the overnight snapshots standing in for the 24h window over
the 10h that existed (out: GOOG 20.5, 15x16 vs 41c [33-41]; openai mu 18.82
sigma 1.24, google 20.35 / 0.92, deepseek 21.10 / 1.04, anthropic 2.67 /
0.24, p_ident 0.91 against tencent at 1.6%).

KILL SWITCHES: IMM_SHARE_FAIR_ENABLE=0 takes the ten series out of the
allowlist (never quoted without the gate); IMM_ALLOW_OR_SHARE_SERIES=""
does the same while leaving the gate code armed.

WHAT THIS DOES NOT FIX / WATCH:
- Model launches. A new model (Stealth 3.1% -> 12% in one week) moves shares
  faster than any weekly vol; the gate sees it only as each day lands.
- The run rate. No single window is right (10/01: the last 10 hours ran
  OpenAI at 22.0% against 17.5-19.2 on the slower windows, and the book sat
  between); the plain four-way average is a choice, not a fit. Compare
  openrouter_share_preds.jsonl against the 10/5 settlement before trusting
  the band's width.
- p_ident uses the rival's vol; tencent's (1.48, launch-driven) makes
  Anthropic's 0.89 look low next to the market's ~0.97.
- Grouping: the leaderboard groups by the permaslug's author; one DeepSeek
  model (deepseek-chat-v3, 0.13% of requests) carries catalog author
  "deepseek-ai". The chart's own week-to-date is unaffected; only the run
  rate is.
- The chart's own lag behind real traffic is unmeasured: r runs from its
  cachedAt, so a chart trailing by an hour makes r an hour short (mu leans
  a little on the known part, sigma a touch small).
- The first cut (pushed 10/01, rejected because main had moved -- never
  deployed) took the chart for a daily feed: it would have held the family
  10 minutes after every chart move (15-30% of the day) and stood it aside
  ~2.5h after each midnight.

Tests: TestOpenRouterShareFairGate (allowlist + cutoff + no cap, kill switch
in a subprocess, fail-closed / stale / lag / hold / band / p_ident /
complete reasons, quote-then-lag end to end, gate-off plain quoting), the
refresher wiring guard (4 signed readers) and test_openrouter_share_fair.py
(15: settled weeks to the rounding, rounding edge, rules/ticker week, text
filter, winsorized vol, known days from the chart time, stalled-feed lag,
mu/sigma arithmetic incl. a half day, the 24h snapshot window, complete/future
weeks, identification,
writer finals/preds/lag/leaderboard caching, signed event reads, fail-closed
chart read). TestNoLivePathUnderTest fails the suite if any incentive_mm
path still points at the live status dir under test (the first cut missed
SHARE_FAIR_FILE and a full-suite run left the share fixture there).

## 2026-09-30 — Every Treasury yield family on the Treasury allowlist (Jack)

Jack: "KXUST10YRRATE27 / KXUST30YRRATE27 and all treasury families should be
on the treasury allowlist."

WHY. The allowlist was exactly ten names (KXUST{2,5,7,10,30}A{D,M}). The 2027
year-end pair (KXUST10YRRATE27 / KXUST30YRRATE27, Dec 31 2027, $1,500 per
period over 25 strikes each = $18/day per strike, 3.3-day periods) reached
the bot only as OPEN-SCAN candidates: ~28-33k contracts already rest on each
book and most strikes are one-sided for us, so the estimate was $0.03-0.32
per strike per day -> $0.17-0.98 per period, under the scan's $1.50 bar
(two 4.24% strikes at $1.71 sat in admit_pending).

WHAT. _DEFAULT_RATES_EXTRA_SERIES (enumerated from the full 14,518-series
catalog: every series whose contract is a US Treasury yield, yield move or
curve spread): the KXUST weeklies (A W) and older KXUST names (A, 2/5/10/30,
*M, M), the 2027 year-end pair and KXUST10Y27, KXNOTE10/10M/10W/10Y/30/30W,
KXTNOTE/D/W and TNOTE/D/W, the how-high/how-low tenors (KX{2,5,7,10,30}YRDIR*),
KXTREASURYMAX/5, KX30YUSTW, KXUSTYLD, KX3MTBILL, TBILL, KX2YFOMC, and the
10Y-2Y / 10Y-3M spreads and inversion (KX10Y2Y, KX10Y2YDATE, KX10Y3M, 10Y2Y,
10Y3M, KXYINVERT, YINVERT). Joined into IMM_ALLOW_RATES_SERIES's default, and
SERIES_OVERRIDES gives them the ten's guards -- safe-join, the 07:30 ET
event-day cutoff, the x1.5 family size -- with the $2/day re-entry rate bar
OFF (IMM_RATES_EXTRA_MIN_RATE, default 0): a per-day bar is horizon-blind and
on an $18/day pool it is exactly what kept them dark; the $1-per-period
payout floor still decides entry. The ten keep their $2/day bar.

LEFT OUT: the 15-minute tenors (KX{2,5,10,30}YRRATE15M -- settle every
quarter hour on a live yield, the hourly-temp shape blocked 9/17), the
KX2YTEST test series, and the non-yield "Treasury" series (Secretary
nominations, sanctions, the coin, debt, TIC holdings, corn "yield").

LIVE PROGRAMS 9/30: only KXUST10AD (today's daily) and the 2027 year-end
pair among all of these; the rest are enrolled for when programs list. At
the x1.5 size the year-end estimates scale ~x1.5, so roughly the two-sided
strikes near the money (e.g. 10Y T3.24/T4.24, 30Y T4.24/T6.74-T7.24) clear
$1 per period; the one-sided ones ($0.17 -> ~$0.25) still do not.

Tests: test_every_treasury_family_is_allowlisted,
test_year_end_cutoff_is_the_print_day_morning; the two prefix-bleed tests now
use the 15-minute / test / hypothetical shapes (the weeklies are enrolled by
name). 1,160 green across the IMM suites.

## 2026-10-01 — Carbon Arc and Ramp AI Index: the 3-per-event cap removed (Jack)

Jack: "remove the 3 max cap on carbon arc, ramp AI index".

CHANGE (code defaults; the code sync + self-restart carry it, no launcher
edit):
- Carbon Arc families (*CC since 9/10, *ADS / *POS since 9/22): the
  "*CC:3,*ADS:3,*POS:3" entries leave the EVENT_TOP_N default.
  CA_FAMILY_EVENT_TOP_N (IMM_CA_FAMILY_EVENT_TOP_N, default 0) rebuilds them
  at any N > 0. No explicitly allowlisted series ends in CC/ADS/POS, so the
  suffix rules only ever capped Carbon Arc family members (suffix + source
  verdict).
- Ramp AI Index (13 series, 3/event since 9/12): RAMP_EVENT_TOP_N default
  3 -> 0 (IMM_RAMP_EVENT_TOP_N=3 restores).
- send_opportunistic_imm: the Carbon Arc line says "no per-event cap" when
  the cap is 0 instead of "capped at 0".
Every other cap is unchanged (gas, diesel, TrueV, Oscars, award shows,
KXART, OpenRouter share, data center counts, finecon, the scan tier).

SIZE OF IT. The 10/01 selection log had 180 Carbon Arc strikes selected and
258 more cut by event_top_n across ~60 events (Ramp: 3 selected, 3 cut);
those 261 become eligible, still subject to the payout floor, the admission
clock and the $100k budget (~$26k of ladder collateral in use).

WATCH: the Carbon Arc adverse-selection read of 9/24 (fills mark out -4 to
-7c/ct, buying YES the toxic side) now applies to roughly twice as many
strikes; the late-month rules and the CA fair gate (dormant until the
October cycle reads) are the protections, as before.

Tests: the Ramp class (no cap by default, knob in the config hash), the
event-cap test (Carbon Arc suffixes uncapped by default; the suffix rule,
the not-a-substring rule and the 9 -> 3 ROI cut pinned under the restored
spec), the CC-family membership test (no cap); 1699 green.

## 2026-10-01 — KXAPRPOTUS allowlisted on KXTRUMPAPPROVE's rules (Jack)

Jack, asked why KXAPRPOTUS-26OCT02 was not quoting, then: "yes, same rules
as KXTRUMPAPPROVE".

WHY IT WAS DARK. KXAPRPOTUS ("President RCP approval rating this week") is
the SAME RealClearPolitics approval average as KXTRUMPAPPROVE, read at
11:00 AM ET on its ticker date (a Friday; weekly, listed the Friday before,
close 15:00Z) in 0.2-0.3 point range buckets. It was on no allowlist, so
only the open scan could reach it, and the scan rejects realclearpolling.com
as a live feed (the 9/24 rule) with the reward window running to the close
-- silently, before any selection decision is logged. Rewards on 26OCT02:
$100 per strike x 8, from 10/01 17:46Z to the 11:00 ET read.

CHANGE. "KXAPRPOTUS" joins _DEFAULT_POLITICS_SERIES (exact name) and
SERIES_OVERRIDES["KXAPRPOTUS"] IS KXTRUMPAPPROVE's override object: the
07:00 ET settlement-day cutoff and x3 size, on the same
IMM_TRUMPAPPROVE_CUTOFF_* / IMM_TRUMPAPPROVE_SIZE_MULT knobs, so the twins
move together. Checked with the bot's own cutoff functions on the live
shapes: occurrence at the 15:00Z read, an irregular occurrence (26SEP25),
one after the close (26JUL31) and standard time all cut at 07:00 ET on the
ticker date. No per-event cap. IMM_ALLOW_POLITICS_SERIES="KXTRUMPAPPROVE"
takes it back out.

WATCH: the 9/26 tape study that set the 07:00 cutoff was KXTRUMPAPPROVE's
(1:00 PM read); KXAPRPOTUS reads at 11:00, inside the same 07:00-12:59 ET
update window, so the same cutoff covers it, but its own book has not been
studied. Inventory taken overnight rides to the 11:00 read.

Tests: test_aprpotus_twin_rides_the_same_rules (allowed exactly, same
override object, x3, uncapped, the four cutoff shapes); 1710 green.

## 2026-10-01 — Restarts keep the book; placements paced at 12/s, cap 1000; renewal jitter (Jack: "both")

WHY. Two things made the book go thin, both measured on 10/1:
- The 14:59Z `restart_imm.ps1 -Task` bounce (to apply IMM_MAX_MARKETS 1000
  and the $100k budget) hard-killed the bot, so the relaunch cancelled 1,391
  leftover orders at 15:00:50 and rebuilt at 250 placements a cycle: first
  placements 15:05, book back ~15:19Z. Code-change restarts already handed the
  book over (13:20Z and 13:47Z adopted 1,115 / 1,265 orders, ~40s gaps); task
  restarts never did.
- The 250/cycle cap was the only thing spreading writes out. A capped cycle
  fired its 250 in ~10s (~25/s), and the write budget is the ACCOUNT's: both
  429 storms that day hit in exactly those seconds and rejected the crypto
  fleets too -- 13:25-26Z, a renewal wave (IMM 14, crypto-touch 23,
  crypto-annual 7) and 15:14Z, the rebuild (IMM 9, crypto-touch 6,
  crypto-annual 9). With the bigger book (~1,700 orders) the renewals also
  came back in waves that bound the cap on their own: 15:45 (329 deferred),
  16:09-16:21 (300-440 deferred for ~12 min), 16:47.

WHAT.
1. Operator restarts hand the book over. `restart_imm.ps1` (both modes)
   writes `run-logs/incentive-mm/restart_handoff_request.json` (atomic) and
   waits up to -HandoffWaitSecs (600) for the bot to exit; -Task first kills
   the launcher, cmd shim and wrapper (python keeps trading) so nothing can
   relaunch python on the old env, then starts the task once python is gone.
   The bot checks for the request at the top of every loop and after every
   cycle (`_restart_requested`), runs the same `_prepare_restart_handoff` as
   the code-change exit and leaves; the relaunch adopts the book
   (RESTART_HANDOFF_MAX_AGE_SECS 300). A request counts only for a process
   that was already running when it was written and for
   IMM_RESTART_REQUEST_MAX_AGE_SECS (900); a stale or unreadable one is
   deleted unread. A handoff that cannot arm (halted, failed import
   preflight) still exits, cancelling as before. A bot still running at the
   deadline is killed exactly as before. `-NoHandoff` = the old hard kill.
2. Paced writes. `WritePacer` spaces place_with_caps' writes at
   IMM_PLACE_RATE_PER_SEC (12; 0 = unpaced): a fresh placement books one
   slot, a swap (REQUOTE_INTERLEAVE cancel + replacement) books two so the
   pair still goes out back to back. The amend loop is paced the same way
   (it shares the cap). Live only; stray / halt / fail-safe / shutdown
   cancels are never paced. Launcher IMM_MAX_PLACEMENTS_PER_CYCLE 250 ->
   1000: a full cycle now spreads over ~85s at half the old peak.
   "placement pacing: N placed at <= 12 writes/s (Ns paced)" logs any cycle
   that waited >= 1s.
3. Renewal jitter. Each order renews up to IMM_ORDER_REFRESH_JITTER_SECS
   (300) EARLY, by a fixed amount from crc32(order id) (`order_refresh_secs`):
   never later than ORDER_REFRESH_SECS, stable while the order rests,
   redrawn per new id, so a cohort placed together spreads out over
   successive renewals. ~11% more renewals at 1500s refresh. 0 = one clock.
4. imm_health_alert: while a fresh request (<= 660s) or handoff (<= 300s)
   file exists, "task not Running" / "process gone" read as "restarting"
   (OK) instead of paging DOWN then UP. A stale heartbeat on a live process
   is never excused.

DEPLOY. The code reaches the bot through the normal code-change exit (which
already hands over). The launcher's 1000 cap needs one task restart, which is
the first live run of the new -Task handoff:
`restart_imm.ps1 -Task`. Watch restart-imm.log for "handoff: bot exited and
handed its book over", then the bot log for "startup: adopted N resting imm-
order(s)" instead of "startup: cancelled N leftover imm- orders".

Kill switches: IMM_PLACE_RATE_PER_SEC=0, IMM_ORDER_REFRESH_JITTER_SECS=0,
`restart_imm.ps1 -NoHandoff`. Tests: TestWritePacer, TestPacedPlacements,
TestRefreshJitter, TestRestartRequest, ProbeRestartWindow (the suite header
turns pacing/jitter off for the older tests; the full suite also passes with
both on).

## 2026-10-01 — Monthly rain (KXRAINCHIM, KXRAINAUSM) back, quoted only through a monthly fair gate (Jack)

Jack: "quote these monthly rain markets, with an algorithm like how you
quote the dailies KXRAINCHIM-26OCT, KXRAINAUSM-26OCT".

WHY THEY WERE DARK. Every KXRAIN<CITY>M has sat in the launcher's
IMM_BLOCKLIST since the 7/26 rain removal (frozen: no orders, positions
ride); the daily KXRAIN came back behind the NWS fair gate, the monthlies
never did. October programs: 14 monthly cities x 7 strikes at $55 per
strike for 10/01 19:05Z -> 10/04 03:59Z (~$23/day/strike); this entry takes
the two Jack named.

THE DAILIES' ALGORITHM, AND THE MONTHLY VERSION. The daily gate: a fair
from the NWS forecast decides WHETHER the bot joins the touch, never WHERE
(stand aside when the touch fights it by more than 10c on the adverse
side), and the bot never quotes the day the rain is measured (the
midnight / 10 pm rule). A monthly is measured on every day of its month,
so (rain_monthly_fair.py, new, on rain_monthly.py's model):
- FAIR = P(month-to-date + rest of month > K): MTD from the CLI + IEM daily
  obs + today's running total (rain_monthly.effective_mtd); the rest by
  Monte Carlo over 28-46 years of the station's ACIS history with the NWS
  gridpoint QPF/PoP injected over its horizon (simulate_remaining; Brier
  skill 62% on 43,080 walk-forward predictions, climatology backbone; the
  forecast layer is not backtestable).
- STATION from each event's own rules ("at CLIORD"): October's KXRAINCHIM
  settles at O'HARE (CLIORD), not the Midway rain_monthly.py was verified
  on in July. CLI_STATIONS = rain_monthly's stations + ORD; an unknown code
  = no fair (stood aside).
- RAIN-DAY RULE: the event stands aside while the station's latest
  observation (IEM currents, younger than 90 min) shows precipitation in
  the last hour or a precipitation weather code (RA/DZ/SN/TS/SH..., VC
  included), and for IMM_RAIN_MONTHLY_DRY_MIN (60) after the last wet one;
  and it stops for good at 22:00 ET the day before the month's last day
  (cutoff_from_close_min 1560 -- the dailies' "10 pm the day before").
- Also out: strikes within 0.05" of the MTD (obs-vs-CLI rounding), every
  strike when the last CLI is older than 40h, and any market without a
  fair younger than IMM_RAIN_MONTHLY_TTL_MIN (30). FAIL CLOSED (the daily
  gate fails open -- but the dailies are never quoted while rain falls).
- Joins the touch unchanged otherwise: the rain family's 5-90c band and
  global ladder; as KXRAIN-prefix "daily" series they skip the quiet-hours
  x2 and the Saturday step-up and take the 7 pm-01:59 ET halving. No
  per-event cap. No directional takes (rain_monthly.py's own taker stays
  dry).
Refresher thread "rain-monthly" every IMM_RAIN_MONTHLY_REFRESH_SECS (600),
signed Kalshi reads for the events (hourly); ACIS history cached 7 days and
the NWS grid hourly inside rain_monthly (first run ~17s, cached after).
Logs "rain-monthly stand-aside <t>: <why>" / "rain-monthly resume <t>",
guard "rain_monthly" (the sweep test counts 33 continues).

LAUNCHER. KXRAINAUSM and KXRAINCHIM leave IMM_BLOCKLIST (the other seven
monthly cities stay frozen); env => `restart_imm.ps1 -Task`, not just the
code sync. test_launcher_unfreezes_exactly_the_two pins code + launcher
together.

DRY RUN 10/01 22:00Z: both stations RAINING (O'Hare 0.53" today, 0.13" in
the last hour; Austin 1.10", 0.20", a thunderstorm) -> both events would
stand aside. Fair vs book, every strike inside the 10c tolerance: CHI MTD
0.53" -> 3" 65c vs 56x62, 4" 40c vs 32x33, 5" 25c vs 10x17; AUS MTD 1.10"
-> 4" 79c vs 72x80, 5" 65c vs 50x70, 7" 43c vs 31x46.

KILL SWITCHES: IMM_RAIN_MONTHLY_ENABLE=0 or IMM_ALLOW_RAIN_MONTHLY_SERIES=""
(the series leave the allowlist; positions ride).

WATCH: the forecast layer is uncalibrated (no archived forecasts); the
station's hourly METAR is the rain detector, so a storm that starts between
observations is seen up to an hour late -- the dry window and the touch
tolerance are the cover. Adding a city = its CLI code in CLI_STATIONS + the
series in IMM_RAIN_MONTHLY_SERIES + out of the launcher blocklist.

Tests: TestRainMonthlyGate (allowlist / band / cutoff, the launcher pin,
every reason incl. wet / drying / no_obs / boundary / stale CLI / band,
quote-then-rain end to end, kill switch in a subprocess) and
test_rain_monthly_fair.py (9: station from rules, table, signed events,
wet/dry/stale/down observations, rung pricing + boundary, unknown station,
writer memory of the last wet observation, failing fair, event cache).

ALL CITIES (same day, Jack: "yes add all the cities"). All sixteen
KXRAIN<CITY>M series in the catalog are now in the gate:
AUS CHI CLL CMH DAL DEN HOU LAX LEX MIA MKE NYC PVD SEA SFO STP (14 had
October programs; STP had no open event). The October rules name, as
CLI codes: AUS, ORD, CLL (College Station), CMH, DFW, DEN, HOU (Hobby),
LAX, LEX, MIA, MKE, PVD, SEA, SFO, SPG -- and KXRAINNYCM names the place
("at Central Park, New York City"), so STATION_ALIASES maps it to NYC. The
seven stations new to the model (CLL, CMH, LAX, LEX, MKE, PVD, SFO) were
checked live on IEM currents ({state}_ASOS), IEM's CLI (K + code, a full
September) and ACIS (K + code). The launcher blocklist keeps only
KXCRYPTOSTRUCTURE and KXAAAGASW; IMM_RAIN_SERIES gains the seven new
series (the rain band). Dry run 10/01 ~00Z: 15 events, 108 strikes, 106
inside the 10c tolerance (out: CHI 4" 25x30 vs fair 44 while it rained,
DAL 7" 47x49 vs 60); five stations wet (AUS, CHI, CLL, MIA, MKE); MKE 1"
on the boundary (MTD 0.96"); LAX/SEA/SFO had no October CLI yet
(obs-only MTD, as rain_monthly does on a month's first day). First write
111s (ACIS history downloads, cached 7 days), later ones cached.

## 2026-10-01 — CPI pilot takes the whole YoY core family, KXCPICOREYOY (Jack)

Jack: "yes add this family" -- after asking why KXCPICOREYOY-26DEC was not
quoting ("i thought core CPI markets were quoted"). The 9/28 CPI pilot named
two EVENTS, KXCPICORE-26NOV / -26DEC (core month-over-month); the YoY core
series stayed under the 9/24 ".*CPI.*" block and never reached selection.
Its 26DEC program opened 16:03Z 10/01: 15 strikes x $100 over 7 days
(~$14.29/day each), to 10/08.

MECHANISM. CPI_PILOT_FAMILIES (env IMM_CPI_PILOT_FAMILIES, default
"KXCPICOREYOY", in the config hash): every EVENT of a listed series is
cpi_pilot_active, so _blocked exempts it and _allowed admits it -- the
normal book at normal size, exactly like the pilot events. The family's
series joins CPI_PILOT_SERIES, so it gets the same guards: the weekday
08:25-11:05 ET blackout ("CPI pilot release blackout", guard cpi_blackout)
and out 7 days before close (26DEC closes 2027-01-13 13:25Z -> out
2027-01-06 13:25Z). The "<series>-X" probe and a bare series never match,
so no auto-enroll / finecon / scan path opens the series; the scan screen
reads the family "allowed" (the book owns it). Every other CPI series,
KXCPIYOY included, stays blocked. IMM_CPI_PILOT_UNTIL closes the family too.

PARITY (launcher $ProbeEnv, live books 17:30Z 10/01): all 15 26DEC strikes
allowed; T2.3-T3.3 have mids in band, T2.1/T2.2 (97x99) and T3.4/T3.5 (2x5)
are extreme.

REPORT. imm_cpi_pilot_report.py gains --families (default KXCPICOREYOY): the
family's open events plus any event with an IMM fill join the review
(10/01: 26SEP, 26NOV, 26DEC). accrued_est is a lifetime counter, so the four
KXCPICOREYOY tickers' $21.31 from the 9/14-9/24 open-scan stint
(PRE_PILOT_ACCRUED, snapshotted before the deploy while the family was
blocked) comes off their estimate. The Oct 13 review covers the family.

KILL SWITCHES. IMM_CPI_PILOT_FAMILIES="" (task restart) drops the family and
leaves the two named events; IMM_CPI_PILOT_EVENTS="" drops those.

WATCH: KXCPICOREYOY-26DEC strikes in selection_snapshot ("selected" /
"extreme_mid"), "CPI pilot release blackout" lines on weekday mornings, no
orders in 08:25-11:05 ET.

## 2026-10-01 late — Treasury how-high / how-low ladders quote only against CNBC's live yield (Jack)

Jack: "is treasury feed not realtime updating my quoting? i got sniped on
KX10YRDIRLM-26OCT30L, and KX30YRDIRLM-26OCT30L" ... "i got permission on cnbc"
(CNBC's permission for this automated read is Jack's, told in chat 10/02
~01:00Z; its terms otherwise forbid automated collection -- NBCUniversal
prohibited actions K/F).

WHAT HAPPENED. The 9/30 Treasury allowlist enrolled the touch ladders plain:
no yield feed anywhere in the bot. They resolve YES when Treasury's Daily Par
Yield (NY Fed quotes at/near 3:30pm ET, first published value, 2 dp) is below
(L) / above (H) the strike on ANY business day of the window, so the live
Treasury market decides strikes hours before the fix. 10/1 par: 2Y 4.88 ->
4.78, 5Y 5.09 -> 5.01, 7Y 5.19 -> 5.12, 10Y 5.29 -> 5.24, 30Y 5.64 -> 5.61. A
taker swept our 10Y-low asks one strike every ~6 s at 11:38:40-11:39:08 ET
(T5.28 82c, T5.29 88, T5.27 79, T5.26 76, T5.24 66, T5.20 53), the same flow
hit the 2Y/5Y/30Y lows 11:29-11:47 and 12:25 ET: -$76 marked on 23 fills
(several settled YES) against ~$26 modelled reward. Programs: ~$5,563/day
across the ten OCT30 ladders, 10/01 04:02Z -> 10/04 03:59Z.

FEED (treasury_fair.py, new). CNBC's quote service: real-time Tradeweb
on-the-run yields, one request for US2Y/5Y/7Y/10Y/30Y, every
IMM_TREASURY_POLL_SECS=10 (60 while the market is shut), refresher thread
"treasury-yield" into an in-memory YieldWatch (the quake gate's contract).
CNBC's 10/1 closes sat within ~1bp of the published par.

FAIR. Driftless Gaussian random walk from the live yield; daily sd per tenor
= the last 60 par-yield changes (treasury.gov, the settlement source, read
once a day) x1.25, floor 4bp (10/2: 2Y 6.8, 5Y 6.4, 7Y 6.2, 10Y 5.8, 30Y
5.2 bp/day); a fix at 15:30 ET each business day through the close (SIFMA
full closes skipped), each with 1bp of CNBC-vs-par noise; 4,000-path Monte
Carlo, fixed seed. From 15:30 ET to midnight the day's fix is PENDING at the
read nearest 15:30. Past fixes are not modelled: a strike they crossed has
resolved (99c) and the band filters already skip it.

GATE (treasury_gate_reason -> YieldWatch.verdict, quote loop after the
Vercel gate, guard "treasury_yield"): stand aside (cancel) with no feed, a
read older than 60 s, a tenor quote older than 10 min in the NY session (90
min other weekday hours; no limit while the market is shut), in the weekday
release windows 08:25-08:45 / 09:55-10:10 ET, while the tenor is MOVING (a
>= 3bp range in 10 min freezes it 10 min), within 2bp of the live yield,
DECIDED (fair < 3c or > 97c), or when the touch fights the fair by > 10c on
the adverse side. Otherwise the ordinary safe-join ladder at the x1.5
Treasury size, the 07:30 ET event-day cutoff unchanged. Knobs: IMM_TREASURY_*
(treasury_fair.py); status run-logs/incentive-mm/treasury_yield_state.json
every minute.

DRY RUN 10/02 01:13Z (launcher env, live books): 268 of 278 ladder strikes
quote, 9 near the money, 1 decided. On 10/1's sweep the gate would have
stood T5.24-T5.29 aside (near / decided) and failed T5.20's 53c ask on the
band (fair ~75c).

KILL SWITCH. IMM_TREASURY_GATE_ENABLE=0 BLOCKS the ladders (series pattern;
the open scan would otherwise quote them plain), never quoted without the
gate.

WHAT THIS DOES NOT FIX. A 10 s poll is slower than takers on direct feeds:
the release windows and the move freeze carry the scheduled prints; an
unscheduled jump inside the poll gap can still fill a stale strike. The
model has no drift and no event-day vol; it decides stand-asides, it does
not price. The point-in-time Treasury markets (KXUST*AD/AM/...) keep their
07:30 ET event-day cutoff and no feed. Positions from 10/1 ride.

WATCH: startup "treasury gate: ...", "treasury: sigma {...}", "treasury
stand-aside / resume" lines, "treasury: US10Y moved 4.1bp in 10m --
freezing" around 08:30, no ladder orders 08:25-08:45 / 09:55-10:10 ET.

## 2026-10-02 — The live-yield gate covers EVERY Treasury market (Jack)

Jack, after the touch-ladder gate (9aab273) went to main: "should use this for
all treasury markets".

SCOPE. treasury_gated_series: the whole Treasury allowlist
(TREASURY_GATED_SERIES = IMM_RATES_SERIES + IMM_RATES_EXTRA_SERIES, 69 series)
quotes only through treasury_gate_reason -> YieldWatch.verdict. CNBC now also
reads US3M (one request, six symbols).

MODELS (treasury_fair.classify):
- POINT ("par yield ABOVE K on date D", strike_type greater, T strikes):
  KXUST{2,5,7,10,30}A{D,W,M}, KXUST{2,5,10,30}A, KXUST{2,5,10,30},
  KXUST{5,10,30}M, KXUST{10,30}YRRATE<yy>, KXUST10Y<yy>. Fair = the normal
  tail at D's 15:30 fix: live yield, sigma^2 x the business-day variance to
  D, +1bp basis; D's own fix pending after 15:30 at the 15:30 read.
- TOUCH: the DIR ladders as before, plus KX10Y2Y / 10Y2Y (10Y-2Y) and
  KX10Y3M / 10Y3M (10Y-3M) as touch-ABOVE on the live spread (FRED's T10Y2Y /
  T10Y3M are the par curve's differences); sigma from the spread's own par
  changes (10/2: 4.1 / 5.6 bp/day).
- UNMODELLED -> stand aside ("unmodelled"): KX2YFOMC (FOMC-day move), the
  bills (KX3MTBILL, TBILL), KXTREASURYMAX/5 (year max), KXUSTYLD (tenor only
  in the rules), KXUSTM, KX30YUSTW, the KXNOTE*/KXTNOTE*/TNOTE* names and
  KXYINVERT/YINVERT/KX10Y2YDATE. None has an open program on 10/2; each needs
  its rules read before it can be modelled.

HORIZON RULES. Feed (no feed / stale read / stale quote), release windows
(08:25-08:45, 09:55-10:10 ET weekdays) and the move freeze (>= 3bp in 10 min)
apply to everything. A touch strike the live value has already CROSSED
stands aside at any horizon. NEAR (within 2bp of the live value): touch
always, point only within IMM_TREASURY_NEAR_MAX_DAYS_POINT (7) days -- a
month-end strike 1bp from the yield moves ~1.5c per bp, a daily's ~7c. The
fair checks (decided < 3c / > 97c, the 10c band) apply only within
IMM_TREASURY_FAIR_MAX_DAYS (45) of the market date: past it (the 2027
year-end pair) the live yield barely moves the fair, and a driftless random
walk over 15 months would only create false stand-asides.

DRY RUN 10/02 ~01:27Z (launcher env, live books, every Treasury market with
a program): 393 of 403 strikes quote -- all 75 month-end (KXUST*AM-26OCT30)
and 50 year-end strikes, the ladders less 6 near and 4 crossed (10Y-low
T5.27-T5.29, already resolved on 10/1's 5.24 fix).

KILL SWITCHES. IMM_TREASURY_GATE_ALL=0 restores the 2026-10-01 scope (only
the ladders gated; the rest quoted plain, as they were for months);
IMM_TREASURY_GATE_ENABLE=0 additionally BLOCKS the ladders.

WATCH: startup "treasury gate: ALL 69 Treasury series quote only against
CNBC's live yield ...", "treasury stand-aside KXUST..." lines in the 08:25 /
09:55 windows, and the daily KXUST*AD book on the day before each print.

## 2026-10-02 — The IMM can sign as its own Kalshi account (Jack)

Jack: "if i want to switch my IMM bot to a different kalshi account, what
would need to do" -> "set up 2" (the IMM-only key switch). Nothing changes
until the two variables below are set.

WHY A SEPARATE SWITCH. Every fleet bot signs with one key:
KALSHI_API_KEY_ID is set nowhere (all fall back to the c3204983... default)
and KALSHI_PRIVATE_KEY_PATH (HKCU) points at Downloads\Lisa_Kalshi.txt.
Changing either moves the crypto / weather / gas / rain bots too.

THE SWITCH (imm_account.py). Set BOTH user variables:
    IMM_KALSHI_API_KEY_ID        the new account's API key id
    IMM_KALSHI_PRIVATE_KEY_PATH  its private key (PEM) file
incentive_mm.py and kalshi_reads.py resolve them at import -- process env
first, then HKCU\Environment directly (a Task Scheduler session or an old
shell may not carry a new user variable) -- so the bot and every IMM report
follow: the "KL imm *" and DIGEST tasks import one of the two, and
imm_cpi_pilot_report.py moved off crypto_touch_mm's fleet client onto
kalshi_reads. Neither set = the fleet key, unchanged. ONE set =
misconfigured: load_private_key raises and nothing signs -- never a
half-and-half pair, never a silent fall back to the fleet key. Startup
logs `kalshi account: key xxxxxxxx... (<source>)` from build_client (no
balance there: GitHub Actions call it and their logs are public) and
`[IMM] account balance $... (key xxxxxxxx...)` from main (local only).

STATE OWNER. imm_state.json now records `account_key_id`; a file written
before 10/02 is the fleet account's. A live start or --cancel-all signing as
a different key is REFUSED (exit 2; the launcher retries every ~30s and the
health alert pages PROCESS GONE / HEARTBEAT STALE): positions, order ids,
banked accrual, the P&L carry and the balance-floor anchor all describe one
account. The owner is stamped when the file is created and never rewritten,
so a dry run signed as the other account cannot launder it. Dry runs and
--status never check.

CUTOVER, in this order. Do NOT set the variables while the old-account bot
runs: its next restart comes up refused, with any handed-off book resting
until TTL.
1. Disable-ScheduledTask 'KL incentive_mm' (the watchdog starts a Ready
   task within 15 min; Disabled is the pause switch).
2. Stop the chain: Stop-ScheduledTask, then make sure no incentive_mm.py
   python, run_incentive_mm.ps1 or run_incentive_mm_hidden.vbs process
   survives (restart_imm.ps1's sweep shows how).
3. `python incentive_mm.py --cancel-all`, still signing as the OLD account.
   Killing python never cancels resting orders.
4. Archive run-logs\incentive-mm\imm_state.json, imm_order_journal.jsonl and
   restart_handoff.json (if present). Everything else there is market data.
5. Set the two user variables.
6. From a NEW shell: `python incentive_mm.py --status` -- the startup lines
   must name the new key and its balance.
7. Enable-ScheduledTask, Start-ScheduledTask. Consider -Probe sizing for the
   first paid period.
The old account's IMM inventory (1,027 markets, ~38k contracts on 10/01)
cannot move: it settles there unmanaged (no hopeless exits, toxic halts or
floor logic) unless closed by hand.

RE-CHECK BEFORE GO-LIVE. Settings sized to the old account
(IMM_ACCOUNT_DROP_HALT 2000 -- account value since 10/3 --, IMM_DAILY_LOSS_LIMIT 2000 -- since 10/3 --,
IMM_COLLATERAL_BUDGET 300000 -- since 10/3 --) and to its Advanced API tier
(KALSHI_RATE_LIMIT_MS 25, 1000 placements/cycle at 12/s).

WHAT SPLITS. Fleet readers stay on the fleet account: fetch_settlements_csv /
fetch_trades_csv (the 5:45 exports behind the Kalshi dashboards),
send_portfolio_digest, send_daily_digest, lowtemp_status. IMM history splits
at the cutover: the dashboard, digest, reward recon and the Oct 13 CPI pilot
report read the new account only, and lifetime counters restart with the
fresh state file. manual_events and STP=maker only see the IMM's own
account, so manual trading on the old account is invisible to it.

ROLLBACK. Remove the two user variables and the fleet key comes back; the
new account's state file then refuses it -- archive that file (or restore
the archived fleet one) first.

Tests: test_imm_account.py (14), TestImmOwnAccount in test_incentive_mm (9),
two key tests in test_kalshi_reads.

## 2026-10-02 — Sports ladders / escalators x4 -> x5 now; from Monday 10/5 the quiet hours are 0-8 ET x3 (hour 9 back to x1) and earnings x2, scheduled (Jack)

Jack, after the multiplier review: "yes, make both changes for monday. also
increase LADDER/ESCALATOR to 5x immediately". The two Monday changes were the
review's recommendations; for the ladders the review said hold x4 until a
rewards statement shows ladder credits (see PAID RATE below).

LADDERS x5, NOW. The archetype KXNFLLADDERREC's size_mult default 4.0 -> 5.0
(env IMM_SPORTS_LADDER_SIZE_MULT; the launcher does not set it). Checked with
$ProbeEnv applied and the live Saturday verdict (x2 from 10/3): 100 a side on
a weekday, 200 in 0-9 ET; Saturday 10/3 200 by day, 400 in 0-9 ET; from
10/5 300 on weekday nights (0-8 ET x3) and 600 on Saturday nights; per-market
cap 750 / per-event 5,000 at the launcher's 150 / 1,000, and the full unwind
rests up to the 750. Reward is still linear in size: the median est_frac on
ladder rows 9/28-10/2 was 0.1-0.3% of the scored book (designated makers
rest 55k-330k contracts per side). Since 9/25 (modelled): $434 rewards,
+$12 trading MTM on 9,746 contracts, 24h mark-out -0.6c per contract,
positive 7 of 7 days. Rent per resting contract-hour at x4 (one midweek game,
PIT@CLE, $429/day pool, ~300k depth) was about half of x2/x3's on the 9/27-28
slate (escalators 55 -> 30c, ladders 39 -> 19c per 1k), a pool/depth
confound until the 10/4 slate is measured at x4/x5.

PAID RATE NOT YET CONFIRMED. reward_credits.csv ends at the 9/27 statement,
before any ladder program paid out (credits land 0-1 days after a period
ends). The 9/29 calibration's "credited 0.0 on 14 events" for the seven NFL
series is the recon judging "settled" against now rather than against the
last pasted statement (fix in flight, "Stop recon counting not-yet-credited
events as unpaid"); it is also why the dashboard's Sports & awards paid rate
reads 0.28. Paste a statement after the 10/4 games and compare.

SCHEDULED KNOBS (new). IMM_HOUR_SIZE_MULT_NEXT / IMM_HOUR_SIZE_MULT_FROM and
IMM_EARNINGS_SIZE_MULT_NEXT / IMM_EARNINGS_SIZE_MULT_FROM: the NEXT value
takes over from 00:00 ET on the FROM day (YYYY-MM-DD, an ET day), read off
the clock, so Monday needs no restart. The hour window goes through
global_hour_mults(et_day) at the instant being sized, so the floor
projection's hour walk (size_mult_profile) crosses the switch hour by hour;
earnings goes through earnings_size_mult() (an epoch compare per call), which
the floor projection prices at the current value -- earnings accrual is
under-projected for the last day before the switch, nothing else reads
ahead. Both halves of a pair are required: half a pair is ignored with a
startup warning rather than turning the quiet hours off on a typo (to end a
window from a date, NEXT=0-23:1.0). Startup logs each scheduled value and
what is in force now. The launcher sets both pairs FROM 2026-10-05;
IMM_HOUR_SIZE_MULT=0-9:2.0 stays in force until then.

QUIET HOURS 0-9 ET x2 -> 0-8 ET x3 FROM 10/5. Weekdays 9/15-10/1, long-dated
families ex-ladders, cycle-log rent minus 24h mark-out (the Saturday
tracker's measure, sat_tracker_cache rebuilt without its partial days), per
1k resting contract-hours: 0-8 ET at x2 +23.4c ($95/day, positive 11 of 13
weekdays; broad -- earnings, Carbon Arc, KXRT, elections, econ all net
positive), 10-23 ET at x1 +16.8c ($68/day), hour 9 at x2 -25.1c (-$11/day:
8.3 fills per 1k at a -9.0c mark-out, mostly KXRT -11.5c per fill at 9am and
econ / rates). The 9/12 extension read as a natural experiment: hours 0-2
and 8-9 went x1 -> x2 and kept their rent per contract (50.5 -> 50.1c per 1k)
while the unchanged hours fell 11-19% over the same weeks, and fills per
contract and mark-out did not worsen. Reward share is small (rent-weighted
est_frac 12% or less, Treasury nights ~19%), so +50% size buys ~+45% rent.
Expected ~+$40-45 per weekday, modelled. It composes with every family and
with Saturday: from 10/10 Saturday 0-8 ET is x6 on the global ladder,
ladders 600, KXTRUMPAPPROVE 360 and KXUST*AD 180 up to their cutoffs;
TOTAL_SIZE_MULT_CAP (x5 on hour x deep-reference) leaves the deep-reference
boost at x1.67 on weekday nights and none on Saturday nights.

EARNINGS x1.5 -> x2 FROM 10/5. +$81/day net 9/18-10/1 (rewards at the
family's paid rate + trading MTM), positive 13 of 14 days, the best family on
the book. Since the x1.5 (9/17) net per resting contract-hour rose 25 -> 33
-> 66c per 1k and settled P&L per filled contract went -9.2c -> +8.6c.
Through applied_mention_mult: 40 a side by day, 120 on weekday nights (x2 x
x3), caps 300 per market / 2,000 per event.

WATCH. Saturday 10/3 is the first x2 Saturday (0-9 ET x4 on the global
ladder) on a book ~4x the size of the x1.5 Saturdays' (10/2 09:36 ET: $60.5k
resting on 1,509 markets at x2). The selection budget (IMM_COLLATERAL_BUDGET
100,000 on the x0.65 estimate plus the inventory reserve) will start skipping
NEW markets overnight; sticky ones keep their size.

Saturday tracker: QUIET_HOURS / BLOCK_LABEL are still the static 0-9 window;
they must read global_hour_mults(et_date) before the 10/12 run (the first
with 0-8 weekdays). Follow-up after the cache fix ("Fix Saturday tracker
caching partial cycle-log days") lands.

Revert: delete a NEXT/FROM pair from $ProbeEnv + restart_imm.ps1 -Task;
ladders back to x4 with IMM_SPORTS_LADDER_SIZE_MULT=4 in the launcher.

Tests: TestScheduledKnobs (FROM-day parsing, the window and earnings switch
at ET midnight, hour 9 back to x1, exclusions and Saturday composition, half
a pair ignored, the floor projection across the switch, caps); the ladder
enrollment test reads x5; the suite neutralises the scheduled knobs at
import (a wall-clock hazard in a process that mirrored the launcher env).
1,890 green (unittest discover).

### 2026-10-02 — The Saturday tracker reads the dated quiet window (follow-up to the 10/5 step)

imm_saturday_tracker's quiet / day blocks now follow each ET day's own
global window, imm.global_hour_mults(et_date) (quiet_hours / block_of /
block_label): 0-9 ET through 10/4, 0-8 ET from 10/5, so from the 10/12 run
hour 9 (back to x1) sits in the day block for both the x2 Saturdays and
their anchor weekdays. gate_family_table classifies each day by its own
window (an "all weekdays" anchor can straddle the switch). Labels follow
the day ("0-8 ET" / "9-23 ET" from 10/5), and the email's "Quiet hours"
line states the switch ("0-9 ET x2 ...; from 2026-10-05: 0-8 ET x3"). The
cycle-log parser is untouched -- its hour_mult column still excludes 0-9
and only feeds the day block's hm (10-23 either way) -- so the per-day
parse cache (4421783) needs no CACHE_VERSION bump.

Tests: DatedQuietWindowTests (hours and labels by day, hour 9 by its own
day in gate_blocks, the family table, the knob line). 1,902 green.

## 2026-10-02 — From Monday 10/5: an evening window (ET 18-21 x1.5) and a yield size mode (x1.5 on markets yielding >= $0.011/contract/day) (Jack)

Jack, after the "where else can I add a multiplier" review: "1 and 2 for
monday". Both are dated CODE DEFAULTS (no launcher change, no -Task restart:
the sync and the bot's code-change restart deploy them, and they switch on
off the clock at 00:00 ET 2026-10-05).

THE REVIEW (9/15-10/1, long-dated families ex-ladders; modelled rent at each
family's paid rate from the 13:51Z calibration -- 1.08x overall -- minus the
24h mark-out of the fills, and separately minus the dashboard's actual
trading P&L; per 1k resting contract-hours; gains assume fills scale 1:1
with size, as the 9/12 overnight doubling showed, and reward dilutes by our
share of the scored book):
- hour blocks, weekdays: 0-8 ET (x2) +23c, hour 9 (x2) -25c, 10-12 -10c,
  13-17 +22c, 18-21 +36c, 22-23 +2c. Evening minus the other day hours
  bootstrap 90% CI [+7, +51]c; 18-21 is the best or near-best block every
  week (18 -> 49 -> 61c), positive 12 of 13 weekdays, and broad: TRUMP
  mentions, econ, AI, Carbon Arc, earnings, KXRT all net positive there.
- yield quintiles (market-day level, paid rent per 1k contract-hours):
  Q1 <19c nets ~0 (-1.5 / -0.3c) on 19% of the long-dated book; Q4 44-69c
  +35 / +20c; Q5 >69c +49 / +82c; positive every week for Q4-Q5. KXRT is a
  third of Q5 and nets -8c on mark-outs; gas / diesel, econ & rates and
  elections lose money even in their high-yield markets.
- other levers measured: Carbon Arc *CC x1.5 +$9-10/day; commodities, AI,
  politics, Carbon Arc *FT / *ADS, weather x1.5 ~+$15/day together (largely
  the same markets the yield mode reaches); TRUMPMENTION as a family ~$0 on
  trading P&L; floor rescue (x2 on payout_floor skips) 174 of 615 clear,
  ~$37/day net at best, needs its own design; cuts: 10-12 ET x0.5 +$2.5/day,
  the lowest-yield fifth x0.5 +$1-3/day (and ~80k contract-hours of collateral).

EVENING WINDOW. EVENING_SIZE_MULTS = IMM_EVENING_SIZE_MULT ("18-21:1.5", the
IMM_HOUR_SIZE_MULT syntax, "" = off) from IMM_EVENING_SIZE_MULT_FROM
("2026-10-05", "" = at once); evening_hour_mults(et_day). Applied in
_hour_window_mult after the global window: never on a quiet hour, never on
sports ladders / escalators (their pre-kickoff window -- inactives post
~90 min before kickoff, the cutoff is kickoff - 30 min -- and the study left
them out), never on daily families, open-scan members or
IMM_HOUR_MULT_EXCLUDE prefixes; a per-series window wins for its hours
(KXRAIN 19-1 x0.5, gas 16-1 x0.5); Saturday multiplies on top (x3); the
floor projection's hour walk sees it. Modelled +$20/day at x1.5.

YIELD SIZE MODE. YIELD_SIZE_MULT 1.5 (IMM_YIELD_SIZE_MULT, 1 = off) from
YIELD_SIZE_MULT_FROM 2026-10-05, on markets whose meta.yield_per_contract
(the estimator's modelled $/contract/day, pads excluded) is >= YIELD_SIZE_MIN
$0.011 (~44c per 1k contract-hours paid, the Q4 floor), held down to
YIELD_SIZE_EXIT_FRAC 0.85 of it (own-size dilution of the per-contract yield
is up to 1 / (1 + 0.5 x share)). yield_size_eligible: long-dated only; no
family multiplier and no hand-tuned ladder / cap / quote_all; not open-scan,
finecon, elections, sports ladders; not the excluded families by prefix
(IMM_YIELD_SIZE_EXCLUDE: KXRT, gas & diesel, Treasuries and econ by prefix,
sports & venues, awards) or word (IMM_YIELD_SIZE_EXCLUDE_WORDS: econ words,
NOBEL / ALBUM); a *MENTION series skips the prefix / word lists (KXWCMENTION).
Per ticker, sticky, decided on each refresh's estimate (_yield_size_verdict
right after yield_per_contract), dropped when the market leaves the
selection (_prune_yield_boost at the top of the yield pass). Composed with
the near-cliff size mode as _market_size_mult at every reader of the shape:
the estimator's probe ladder and floor projection, the collateral
reservation, the quote loop, the placement guard's bracket. Caps and skew
knees unscaled (like near-cliff). Logged: the startup line, "yield size: N of
M selected markets" when N changes, ys_size_mult in the selection snapshot.
For the rule as built: +$17-22/day modelled, net +47c (mark-out) / +39c
(trading P&L) per 1k contract-hours in the tier, positive every week.

SATURDAY TRACKER. Evening hours sit out of the hour_mult it reads the
Saturday level from (drop_evening_hm, in build_rows and gate_blocks): with
the evening x1.5 a x2 Saturday's day block would read ~x2.25 and the
step-up watch's _level would never match x2. Quiet / day blocks unchanged
(the evening hours are day-block hours; Saturday and its weekdays both carry
them). The email's knob lines name the evening window.

SIZES (launcher geometry, 20-lot rung): a generic long-dated market 30 by day
in 18-21 ET from Monday, 60 Saturday evenings; earnings (x2) 60 Monday
evenings; a yield-mode market x1.5 on top of whatever its hour says (e.g. 90 a
side in Monday's 0-8 ET quiet hours, 180 Saturday nights). Collateral rises
on the boosted markets; the $100k selection budget (x0.65 estimate + the
inventory reserve) will skip more NEW markets on Saturday nights.

Tests: TestEveningWindow, TestYieldSizeMode (eligibility, activation,
hysteresis, near-cliff composition, the end-to-end cycle resting x1.5 and
back), the tracker's evening hm test; the suite neutralises both dated
defaults at import. 1,913 green; with both forced on (FROM "" and the
evening over all 24 hours) only the two code-default assertions differ.

## 2026-10-03 — The account floor measures ACCOUNT VALUE and halts on a $2,000 daily drop, with its own email (Jack)

Jack: "make the balance guard use account value. drop of $2k in a day
should halt." and "i also need a clear email alert if halts due to balance
guard".

WHAT HAPPENED. 01:40 ET 10/3 the cash guard (IMM_BALANCE_DROP_HALT=5000 in
the launcher, on balance_dollars) halted the bot until the 06:00 ET roll:
"ACCOUNT balance dropped $5097 since the daily anchor ($8978 -> $3882)";
3,590 orders cancelled. It was inventory, not loss: 502 fills since the
Friday 06:00 ET anchor spent $9,565 of cash (the Saturday-stacked 400-lot
ladder fills just after midnight among them) while the bot's marked-to-
market trading P&L over the span was about -$700; the daily-loss halt never
came close. The halt idled 4.3 hours of the week's most boosted window
(flat modelled rewards 02:00-06:00 on the dashboard) and its dip clocks
pushed members such as KXNFLESCALATORREC-26OCT05ATLNO-ATLDLONDON5 into the
hopeless exit at 06:08, after which the over-budget selection kept them out.

CHANGE.
- account_value_dollars(balance) = balance_dollars + portfolio_value / 100
  (Kalshi's own valuation of every position; send_portfolio_digest's
  equity_kalshi). A response without a positions value skips the check
  rather than falling back to cash.
- ACCOUNT_DROP_HALT = IMM_ACCOUNT_DROP_HALT, $2,000 in code (a new name
  because the measure changed; the launcher's IMM_BALANCE_DROP_HALT=5000 is
  removed -- the running launcher's copy is ignored). 0 = off.
- state.account_value_day_start replaces balance_day_start (persisted the
  same way, zeroed at the 5am-CT roll); an old cash anchor in the state file
  is ignored, so the first check on the new code anchors on value.
- --clear-halt also zeroes the anchor: before, a deliberate resume re-read
  the same drop against the old anchor and halted again at once.
- THE EMAIL. balance_halt_email: subject "IMM HALTED: account value down $X
  today (limit $2,000) - orders cancelled until <roll, ET>"; body: a first
  line that stands alone on an SMS gateway, anchor / now / cash + positions,
  the IMM's own P&L today against the part of the drop it does not explain
  (another bot, manual trading, a withdrawal, Kalshi re-pricing), what the
  bot did (orders cancelled, positions ride, resumes at the roll) and how to
  resume sooner. Sent directly; the generic alert is kept for the log and
  the daily summary only (urgent=False), so one email per halt.
- Startup logs cash and account value with the floor.

Tests: TestBalanceFloor (the knob and the value read; a real $2,000 drop
halts; the 10/3 inventory case does not, while a $2,000 cash withdrawal
does; small drops, partial and failed reads; an old cash anchor is not
carried; the roll re-arms; one clear email with the numbers; attribution;
--clear-halt re-anchors) and TestHaltUntilRoll on value.

## 2026-10-03 — Pokemon (KXPOKEMON) allowlisted, quoted only against a TCGplayer fair (Jack)

Jack: "are you able to quote KXPOKEMON-26OCTCHA based on Collectr realtime
data", then "yes build the gate to quote all pokemon events".

THE FAMILY. KXPOKEMON lists one "Up or Down" market per item per month around
the 2nd, 22:00 ET: "If the Ungraded Price of the <item> on Collectr is above
$<K> on Oct 31, 2026" -- K is Collectr's price at listing, strike_type
greater, close 23:59 ET on the date; Kalshi settles on the Collectr price it
reads just after (September: 04:31Z Oct 1). October: Charizard, Mew ex,
Pikachu ex - 109 (30th Celebration), Mew - R/G/B RGB, the 30th Celebration
Pokemon Center ETB -- $55/day each from 10/3 (the feed audit's "KXPOKEMON
$370/day pool"). Before this the open scan treated KXPOKEMON as a candidate
with no fair at all.

THE FEED -- TCGplayer, never Collectr. Collectr has no public API: its
backend (api-v2.getcollectr.com) 403s curl/python, and after ~30 calls from
a real browser it served an AWS WAF "Human Verification" CAPTCHA. But
Collectr's product ids ARE TCGplayer's, and its ungraded price is TCGplayer's
Market Price for the printing -- or, with no Market Price, TCGplayer's listed
median (26OCTMEWBRG's strike 5010.87 is exactly 717609's listed median). On
10/3 Collectr matched TCGplayer within 1% on 11 of 13 cards; the two outliers
were October underlyings that had just dropped on TCGplayer (Charizard 194.14
on Collectr vs 171.77, Mew ex 84.40 vs 70.65). TCGplayer leads the settlement
source by Collectr's refresh lag. Endpoints (undocumented, plain requests;
flagged as a terms-of-use question before Jack's "yes build the gate" --
no permission from TCGplayer itself): mpapi.tcgplayer.com/v2/product/<id>/pricepoints (market
+ listed median per printing), mp-search-api.tcgplayer.com/v2/product/<id>/
details (sellers), and the site search (/v1/search/request, --suggest only).
TCGplayer's latest-sales and price-history endpoints answer 403: not used.

PRODUCT MAP (pokemon_products.json, tracked). Event ticker -> {item,
tcgplayer_id, printing, kind}, keyed by EVENT because Kalshi reuses codes
(26SEPCHA was Charmander, 26OCTCHA is Charizard), and the map's item must
equal the rules' item. The refresher re-reads the file when its mtime
changes, so mapping a new month is a data push: the KL sync lands it within
30 minutes, with no restart. An unmapped event is stood aside and named in
the "poke-fair refresh: ... unmapped" log line. `python pokemon_fair.py
--suggest` lists TCGplayer candidates per unmapped event (exact names first,
" - 158/128" stripped as Collectr strips it) with their price against the
strike; the strike equals the right product's price at listing, often to the
cent. EVERY MONTH (~the 2nd, 22:00 ET) the new events need entries.

FAIR (pokemon_fair.py; refresher thread "poke-fair", in-memory snapshot +
run-logs/incentive-mm/pokemon_fair.json). Kalshi's open KXPOKEMON markets
(signed via fair_reader()) and TCGplayer seller counts every 15 min, each
mapped item's pricepoints every IMM_POKE_FAIR_REFRESH_SECS (180).
- V0 = TCGplayer's market price for the mapped printing, else the listed
  median (Collectr's rule). |ln(V0/K)| > 0.7 reads as a wrong product.
- ln(V/V0) ~ N(mu tau, sigma^2 tau), tau = months to the close;
  P = Phi((ln(V0/(K + 0.005)) + mu tau) / (sigma sqrt(tau))). Calibrated on
  the 53 settled Jul-Sep KXPOKEMON markets (strike -> settlement; the
  26SEPCHA mis-settlement dropped): cards mean -9.0% sd 15.0% a month (n 25),
  sealed -4.4% sd 7.1% (n 28), 11/53 at-the-money strikes YES. Defaults
  shrink the drift a third toward zero and widen sigma: cards mu -6% sigma
  18%, sealed -3% / 8% (IMM_POKE_MU_* / IMM_POKE_SIGMA_*).
- THIN items (under 15 sellers, or no market price) move only on a sale:
  q = exp(-1 x tau) the chance nothing sells, P = q [V0 > K] + (1 - q)
  P(sigma 25%). The October RGB Mews and the 30th Pikachu sit AT their
  strikes (one seller for the Pikachu), so an unmoved price is a NO.
- A read that moves V0 5%+ stamps moved_at.

GATE (poke_gate, quote loop after the mortgage gate, guard "poke_fair";
guard sweep 34 -> 35; the probe ladder runs it too, so a stood-aside market
estimates at zero). Stand aside (cancel) on no read, a snapshot or an item
price older than IMM_POKE_FAIR_TTL_MIN (20), an err entry (unmapped, rules
naming another item, strike/date mismatch, no TCGplayer price or seller
count, a price ~2x from K), for IMM_POKE_MOVE_HOLD_MIN (15) after a 5% jump,
or a touch fighting the fair by more than IMM_POKE_BAND_TOL_CENTS (30) on
the adverse side. Otherwise every bid <= fair - IMM_POKE_FAIR_TOL_CENTS
(15) and every ask >= fair + 15 (mort_cap_quotes, applied last). The band
is wider than the caps on purpose (the mortgage gate uses 15 for both):
these books anchor on Collectr's stale print while the fair reads TCGplayer
ahead of it, so a 15-30c gap is the expected disagreement, and the side the
caps leave is the side it favours (10/3: ETB bid 32 vs fair 13, Mew ex 24 vs
7 -- both stood aside under a 15c band). Size x1 (IMM_POKE_SIZE_MULT), band
1-99c. CUTOFF close - IMM_POKE_CUTOFF_FROM_CLOSE_MIN (4320 = 72h),
close-anchored, which also keeps the ticker-date rule from reading
26OCT30TCELPO as Oct 30 or 26AUG151ULTCO as Aug 15.

DRY CHECK 10/3 12:53Z (live books, the bot's own gate): ETB fair 13.3 on
32x70 -> asks >= 29 only; Charizard 15.3 on 22x82 -> asks >= 31 only; Mew ex
7.4 (TCGplayer 69.40) on 24x78 -> asks >= 23 only; the three RGB Mews and
the Pikachu 24.9 (thin) on ~20x80 -> bids <= 9, asks >= 40. All seven quote,
mostly the NO side.

LIVE 10/3 13:19Z (sync 09:15 ET, safe-window restart): "poke-fair refresh:
7/7 markets priced", pokemon_fair.json every 3 min. SELECTION at x1 (one
20-lot a side, the gate leaving mostly the NO side): six markets
payout_floor -- $0.52-1.13 projected over the period's last 0.61 days vs
the $1.50 floor (Kalshi pays nothing under $1 a period; ~3% of books
9,000 contracts deep) -- and the ETB ($2.22) budget (members keep their
slots). Jack 10/3, offered x3 (the mortgage gate's) or x5: "Keep x1". So
a market quotes only when its own estimate clears the floor -- a full-day
period (04:00Z renewals) roughly x1.6 the numbers above -- and the ETB
when budget frees. IMM_POKE_SIZE_MULT is the lever if that changes.

X2 (10/3 ~14Z, Jack: "Raise to 2x multiplier"): POKE_SIZE_MULT 1.0 -> 2.0 in
code -- 40 a side, net cap 300 per market. Scaling the x1 shares (~3% of
the book) by ~1.9: a full-day period projects about $1.6 (Charizard, still
marginal) to $3.5 (Mew R/RGB) for the six cards and ~$6.6 for the ETB, so
most should clear the floor from the 04:00Z renewal; the rest of 10/3's
period is too short for most of them.

BUDGET (10/3 14:29Z): at x2 Mew R/RGB ($2.00), Mew ex ($1.62) and the ETB
($1.55) cleared the floor and the admission clock, then hit `budget`: the
bot is over IMM_COLLATERAL_BUDGET ($100k) on sticky members alone (~$106k
ladders + ~$24k inventory reserve), so NO newcomer from any family is
admitted until members finish. Newcomers then enter by yield_per_contract,
where the Pokemon markets (0.02-0.05) rank above the median member
(0.007). No budget-only exemption exists (quote_all also skips the floors
and manual-yield; FORCE_EVENTS keeps the budget). Jack 10/3, offered a
KXPOKEMON budget exemption: "Wait for room".

BUDGET RAISED (later 10/3, 1f4b334, another session): IMM_COLLATERAL_BUDGET
$100k -> $300k, live from the ~17:50Z restart, ~$137k used -- the budget no
longer binds Pokemon. Pikachu ex 109 was selected 20:33Z and quoted to
22:35Z (YES bid 200 @ 9c, ask 200 @ 74-77c vs fair ~25c, no fills) until
`hopeless` near the 04:00Z period end. The payout floor is the only gate
left: per-day estimates at 22:19Z (x2) Mew G/RGB $3.9, Pikachu $2.0, Mew
R/RGB $1.8, Mew ex $1.6, Mew B/RGB $1.54, Charizard $1.2, ETB $1.1.
SIZE NOTE: the x2 family size is the BASE (20 x 2 = 40 a side); the
Saturday x2 / quiet-hour multipliers and the deep-reference multiplier
(the gate parks rungs far behind the touch; TOTAL_SIZE_MULT_CAP 5) stack
on it -- Pikachu rested 20 x 2 x 2 x 2.5 = 200 a side. The net cap stays
300 per market (150 x 2); none of those multipliers raise it.

RISKS. Settlement: 26SEPCHA (Charmander, K 39.87) settled YES at 190.24
while Collectr's Charmander promo 038 -- the product its July/August markets
tracked to the cent -- was ~$26.7; the October Charizard reuses the code.
Manipulation: on the thin items one TCGplayer sale sets the price, so the
last days (the 72h cutoff) and the x1 size are deliberate. The endpoints are
undocumented and can change; every failure fails closed.

KILL SWITCHES: IMM_POKE_ENABLE=0 (family out); IMM_ALLOW_POKE_SERIES="".

WATCH AFTER DEPLOY: startup "pokemon gate: KXPOKEMON fail-closed on
pokemon_fair's TCGplayer read ...", "poke-fair refresh: 7/7 markets priced",
pokemon_fair.json refreshing every 3 min, "pokemon stand-aside / resume"
lines, no KXPOKEMON order within 15c of the status file's fair. Around Nov 2
22:00 ET: the November events list unmapped and stand aside until mapped.

Tests: TestPokemonFairGate (enrollment, the close-anchored cutoff beating a
ticker date, every fail-closed reason incl. stale prices and the jump hold,
the band vs the caps, the 10/3 Charizard and ETB books, the loop capping and
lifting rungs, holding after a jump and standing aside past the band);
test_pokemon_fair.py (20: the October rules as listed, Collectr's
market-else-median read, the model and the thin mass, every entry doubt
incl. the CHA code reuse, the watch end to end without the network --
cadence, a live map reload, jump stamps, failed reads aging out, --suggest,
and the shipped map against the listed rules). 1,952 green (unittest
discover); the baked-path sweep caught POKE_STATUS_FILE before the fix.

## 2026-10-03 — Caps doubled so they stop binding: budget $300k, resting 8,000, candidates 10,000, slot caps x2 (Jack)

Jack: "Double collateral budget, slot caps, resting order cap, and candidate
book cap. I don't want the caps to trip." The risk-controls table built the
same day had them binding or close.

Launcher changes (see its comment block for the measurements):
- IMM_COLLATERAL_BUDGET 100000 -> 300000. Jack picked $300k over a literal
  $200k, because sticky members alone reserved ~$224k at the Saturday-night
  x4, so $200k would still have refused every newcomer.
- IMM_MAX_TOTAL_RESTING 4000 -> 8000.
- IMM_MAX_CANDIDATE_BOOKS=10000 (was the 5000 code default).
- IMM_SCAN_TOP_N=120, IMM_SCAN_EVENT_TOP_N=6.
- IMM_FINECON_TOP_N=50, IMM_FINECON_EVENT_TOP_N=6.
- IMM_EVENT_TOP_N_MULT=2.

IMM_MAX_PLACEMENTS_PER_CYCLE stays 1000 (Jack: leave it).

CODE: EVENT_TOP_N_MULT scales every per-event strike cap at lookup
(event_top_n_for / _scaled_top_n): 3 -> 6 for gas, diesel, TRUEV, data
centers, awards and OpenRouter share. Uncapped entries stay 0 and a capped
family never rounds to 0. It is a multiplier rather than a launcher copy of
the spec, so a cap added to the defaults later is scaled rather than masked.
Default 1.0, so tests and other importers see the spec as written. Tests:
test_imm_event_top_n_mult.py (4).

WATCH: budget skips should go to 0 outside the multiplier peaks. From 10/10
the Saturday 0-8 ET x6 may push the reservation past $300k on those nights.
Also watch resting orders above 4,000 (untested on Kalshi) and placement-cap
deferrals rising with the book. The gas / diesel x6 event caps double the
correlated strikes per print.

## 2026-10-03 — OpenRouter share: the fair runs on the chart's last 3 hours; the family quotes x1 and nets at most 100 per event (Jack)

Jack: "am i getting sniped on the AI market share markets?", then "Can market
share not quote in more real time?", then "Yes make the share market change
and check it later".

WHY. 10/01-10/03 (first 2.5 days live): ~$17.7 modeled rewards against
-$79 of trading at Kalshi marks on the four 26OCT05 events (OpenAI -$59).
Not latency sniping -- fills were +3.3c/ct at the fill and still +0.8c five
minutes later -- but slow adverse selection: mark-outs went -1.2c at 30m and
-2.4c at 2h. The run rate (plain average of the leaderboard's last day and
trailing 7 days, the chart's week-to-date and its last 24h) lagged the live
chart by hours. On 10/02 13-15Z OpenAI's share of the chart's last 3h fell
from ~19% to ~16% while the model held mu 18.9. The bot bought 100 YES @17
on 19.3 and 62 @81-82 on 18.1 (-$44 of the -$79), all at the quiet-hours
x2 size. The 00:16Z daily leaderboard roll pushed the model UP (OpenAI mu
18.53 -> 18.82) and the bot then bought ANTH 2.5 @59 (marked 17).

EVIDENCE (the 48h of chart_hist snapshots, scratch backtest):
- Forecast: the chart's last 3h / 6h predicted each author's share of the
  next 12h to 0.85 / 0.86pp mean error, against 1.32 for the live blend
  (OpenAI and Google about 1.2 vs 1.8-2.7).
- The OpenAI drop was a regime change, not time of day: the same 3h UTC
  block was 19.5% on Thursday and 16.5% on Friday.
- The live gate rule replayed on every logged cycle (touch vs band +-
  tol, both sides out):

  | fair from | tol | fills stopped | their P&L | cycles out |
  |---|---|---|---|---|
  | live blend | 15c | 0 | $0 | 1% |
  | chart 6h | 15c | 10 | -$43 | 26% |
  | chart 3h | 15c | 12 | -$78 | 26% |
  | chart 3h | 6c | 18 | -$72 | 51% |

  The base is 31 testable fills, -$101 at the 17:28Z marks. Tightening the
  LIVE blend only blocked winners. A tighter band on the 3h fair stopped no
  more losers and doubled the time out, so the tolerance stays 15c.

WHAT:
- openrouter_share_fair: a named author's run-rate share is the chart's own
  last SHARE_RUN_HOURS (IMM_SHARE_RUN_HOURS, 3) whenever the stored
  snapshots cover the window (recent_delta now takes `hours`). The blend is
  the fallback: the first hours of a week or of a fresh file, a week not yet
  begun, and authors the chart folds into Others. Its spread is half the
  range of the chart's shares over SHARE_SPREAD_HOURS
  (IMM_SHARE_SPREAD_HOURS, 3,6,24). The total request rate keeps the blend,
  because volume swings by hour of day.
- New entry and pred fields: run_mode ("chart 3h" / "blend"), fast_share,
  fast_hours and blend_share. The preds log carries run_mode, fast_share and
  blend_share, for scoring the old blend against the new rate at
  settlement.
- incentive_mm `_share_x1`: the ten KX<AUTHOR>SHARE series take no
  quiet-hours, Saturday or evening size (hour_size_mult 1.0) and no yield
  mode (yield_size_eligible False).
- `event_cap_contracts` is SHARE_EVENT_CAP (IMM_SHARE_EVENT_CAP, 100) for
  them instead of the launcher's IMM_MAX_EVENT=1000. The quote loop's event
  room is net YES, so a +127 OpenAI event bids no strike and still offers
  every one.
- SHARE_EVENT_CAP is in the config hash.
- The same day IMM_EVENT_TOP_N_MULT=2 (the caps-doubled change above)
  lets an event quote 6 strikes instead of 3. The 100 net cap bounds
  the six together.

DRY RUN on the 17:33Z inputs (old -> new mu, pp):
- DeepSeek 21.58 -> 22.08. The 22.0 strike goes from ~10c to ~53c fair,
  with the book at 26 and the bot short 75 YES. The gate now stands aside
  instead of selling more.
- OpenAI 18.18 -> 17.84. 18.1 goes to ~9c fair, book 26: no more YES bids.
- Google 21.84 -> 22.50.
- Anthropic 2.49 -> 2.38.

KILL SWITCHES (env in run_incentive_mm.ps1, then restart_imm.ps1 -Task --
the writer runs in the bot's own refresher thread): IMM_SHARE_RUN_HOURS=0
restores the blend; IMM_SHARE_EVENT_CAP raises the cap;
IMM_SHARE_FAIR_ENABLE=0 takes the family out.

WATCH:
- The fair file's run_mode "chart 3h" on all four events.
- "share-fair stand-aside" lines on DEEP 22.0 / OPEN 18.1.
- hour_mult 1.00 on every *SHARE row of the cycle log.
- No new YES bids on KXOPENSHARE while its net is over 100.
- A 3h window is noisy, so expect more stand-asides (about a quarter of
  cycles on the replay). Rewards here were about $7/day.
- Score the preds against the week of Sep 28 final in
  openrouter_share_finals.jsonl, written once the week completes at 00:00Z
  10/5. A check is scheduled for Monday 10/5 after the 10:00 ET settlement.

Tests: test_openrouter_share_fair.py has 2 new tests (20 green). One covers
the 3h rate, its spread from 3/6/24h, and the blend's volume and sigma. The
other covers the fallbacks: no 3h snapshot, the knob at 0, an author folded
into Others. TestOpenRouterShareFairGate.test_x1_at_every_hour_and_a_net_cap_per_event
covers no hour, Saturday or evening mult; no yield mode; the cap 100 and
Vercel's series untouched; and the quote loop bidding at cap 100 but not
at cap 20 with +25 held.

## 2026-10-03 — The 7:00 email's Capacity table becomes a risk-controls table: what fired, what binds, what has slack (Jack)

Jack: "in 'Capacity — how close to each ceiling' in my daily email, show all
risk controls and if any have been breached e.g. the balance guard, or other.
or are close to breaching. make it clear which risk controls are actually
constraining /have real impact vs not".

THE OLD TABLE MISLED IN TWO PLACES on the morning of 10/3. It never showed
the balance guard, which had halted the bot from 01:40 to 06:00 ET. It also
flagged the per-market cap "400 vs 150, 267% AT CAP" on an NFL ladder whose
family cap is 750: a fresh import sees the global 150 because the bot clones
family overrides onto members at refresh. The "collateral deployed (resting)
$290k vs $100k budget" row compared resting-order notional with the modelled
budget, two different quantities.

NOW (imm_risk_controls.py; send_imm_digest builds the context and renders):
every halt, capacity cap, position cap and per-market guard gets one verdict
over the last 24h.
- TRIPPED: a halt fired.
- BINDING: it refused markets, deferred placements or held positions at a cap.
- CLOSE: it reached 80% of its limit at the worst point.
- SLACK: not constraining.
- Per-market guards read FIRED, QUIET or OFF.
Rows sort worst first, under a one-line headline. Evidence is the bot's own:
ALERT lines, cycle summaries, universe lines, placement-cap and idle lines
from the logs (picked by mtime, filtered to the window), plus the toxic_halts
and guard_skips sinks. Caps resolve through ensure_family_override.
capacity_rows / last_universe_line are gone; _UNIVERSE_RE aliases the
module's regex.

BOT CHANGE (log only, no trading effect): one `risk:` line per full cycle
(incentive_mm.risk_line): account value vs anchor, P&L today and open-scan
P&L against their limits. At 7:00 every daily halt has been re-anchored an
hour earlier, so the email needs the day's WORST reading, and only a
per-cycle record gives it. Until this deploys, the account-value and
open-scan "worst 24h" read "not logged yet". See IMM_LOGGING.md.

FIRST READING (live, 10/3 09:30 ET):
- TRIPPED: the account-value guard (the cash rule at 01:40: 3,590 orders
  cancelled, 4.3h idle).
- BINDING:
  - the collateral budget refused 87 markets at the last refresh (28 of 101
    refreshes);
  - the slot caps refused in 99 of 101;
  - the 1,000-placement cap was hit in 134 of 187 cycles, ~600 placements
    waiting a cycle each time.
- CLOSE: candidate books 4,647 of 5,000; resting orders 3,743 of 4,000;
  worst position 125 of 150 (KXANFCC).
- SLACK: daily loss (worst -$594 of -$1,200, at 01:35), events (417 of
  1,000), per-event net (62%), fail-safe.

Tests: test_imm_risk_controls.py (15, including the 10/3 cash trip end to
end, the family-cap false alarm, and a contract test that the bot's risk line
parses). test_send_imm_digest stubs risk_section. TestDigestCapacity lost its
three capacity_rows cases, now ported.

## 2026-10-03 pm — Slot caps reverted; the budget, resting and candidate caps stay doubled (Jack)

Jack: "revert Open-scan slots, finecon slots, per-event strike caps". The
launcher drops IMM_SCAN_TOP_N=120, IMM_SCAN_EVENT_TOP_N=6,
IMM_FINECON_TOP_N=50, IMM_FINECON_EVENT_TOP_N=6 and IMM_EVENT_TOP_N_MULT=2.
The code defaults are back: scan 60 / 3 per event, finecon 25 / 3, and the
per-event spec as written (gas, diesel, TRUEV, data centers, awards,
OpenRouter share at 3). The EVENT_TOP_N_MULT knob stays in code at 1.0.

These stay as set earlier the same day: IMM_COLLATERAL_BUDGET 300000,
IMM_MAX_TOTAL_RESTING 8000, IMM_MAX_CANDIDATE_BOOKS 10000.

What happens to markets admitted during the ~2h at the doubled caps:
- Per-event strikes past 3 are trimmed by the lifetime slot ledger: the
  earliest 3 keep their slots, the rest are deselected and their positions
  ride.
- Scan and finecon members above the cap are quote-to-completion and stay.
  Nothing new enters until attrition brings each tier under its cap.

## 2026-10-03 evening — Daily loss halt $1,200 -> $2,000 (Jack)

Jack: "Increase daily halt to $2000". `set IMM_DAILY_LOSS_LIMIT=2000` in
the launcher; the code default stays 1200. imm_dashboard takes the limit
from the launcher env with a 1200 fallback, so the env is the one place
that keeps the bot, the dashboard and the 7:00 email in agreement.

The halt is unchanged in kind: IMM P&L today (realized + marked, since the
5am-CT roll, carried across restarts) at or below -$2,000 cancels every
order and idles the bot until the roll. At the change P&L today was -$826,
69% of the old limit and 41% of the new.

## 2026-10-03 night — Open scan off; its 83 earning series graduate to the normal book; company KPIs, food trackers and AAA MAXM/MINM cut (Jack)

Jack, after the rewards-statement review (10.3.26 paste): "yes do 1 - 4 but
also keep Jobs (+$16), travel (+$10) and energy (+$5) and any other markets
that are near-breakeven in addition to the 3 you mentioned (endorsements,
strait transit, agriculture)". The four: (1) keep the scan's earners and turn
the scan off, (2) block the food-price trackers, (3) take the Fiscal.ai
company KPIs out of the normal book, (4) drop the AAA MAXM/MINM pair.

EVIDENCE (dashboard days 9/06-10/03; rewards at each family's paid rate from
the 10/3 calibration, trading marked to market; scan events from
opportunistic_roster.json, classified with scan_universe_reason under the
launcher's env so events already allowed or blocked are not counted):
- Scan-only series: +$234. The 83 graduates +$501:
  - endorsements, the weekly strait transits (Suez, Bab-el-Mandeb, Hormuz,
    Panama; the port TEU counts are ~$0) and 17 agriculture series: +$344
    (rewards $343, trading +$1, 46 fills), positive every week;
  - jobs / state economies, travel, energy & mining: +$32;
  - 26 more with a 4-week net of -$2 or better: +$124 (home prices, Netflix
    top 10s, OER, KXGENERICBALLOTVOTEHUB +$45 mostly trading, gas EOY /
    monthly ...). The next one down lost $8.
- The 46 left behind: -$267 (KXBILLSSIGNED -$103 on 9 fills, monthly diesel
  -$45 on 45 fills, KXTOP10BBSPOTS -$20, KXCASESSION, KXEOWEEK, KXTRUMPVH,
  monthly diesel election, KXUSHOMEINVENT, and the scan's own Fiscal.ai KPI
  and food series -$41). 22 of them earned under $0.50 with no fill (~$4
  together, mostly vote counts): left out, one line to add back.
- Fiscal.ai company KPIs: $367 of rewards against -$678 of trading. The 12
  normal-book series that traded lost $166, 8 of 12 negative; KXBA -$99 on
  60 fills.
- Food-price trackers: $25 of rewards against -$151. They settle on Spice
  Data (KXBKNUGGETS / KXAMSAVO list BLS), not Carbon Arc, so no fair gate
  covers them.
- AAA MAXM/MINM: lost on both of its events (the 10/3 tier review), -$42 over
  four weeks.
- NOT cut: Carbon Arc *POS. Its -$221 was the September-data book (138
  fills) before the fair gate had a read it could use; the October book is
  +$17 on 21 fills. The gate needs 10% of the month observed and the POS feed
  lags ~2 days, so the November-settling POS events quote ungated until ~10/6
  (carbon_arc_fair.json lists all 18 POS series as missing on 10/3). Re-judge
  around 10/20 on gated fills.

WHAT:
- Launcher: `set IMM_SCAN_TOP_N=0` (scan_universe_reason -> "off"). Members
  leave the universe at the next refresh: quotes cancelled, positions ride.
- incentive_mm `SCAN_GRADUATE_SERIES` (IMM_ALLOW_SCAN_GRADUATE_SERIES, ""
  removes them), joined into ALLOW_SERIES. They keep the treatment they
  earned the +$501 under, as the data centers did on 10/1:
  - SCAN_GRADUATE_ARCHETYPE (KXSUEZWEEKLY) via FAMILY_OVERRIDE_PARENTS: the
    scan guard set (safe-join, no fresh-candidate rate bar, global ladder and
    caps, or IMM_SCAN_LEVELS / IMM_SCAN_MAX_POSITION if ever set). Listed
    after the KXAAAGASD prefix, which no graduate matches, so the gas
    graduates take the scan guard, not the gas-daily one.
  - At most SCAN_GRADUATE_EVENT_TOP_N (3, the scan's) strikes per event:
    "=SERIES:3" entries appended to the EVENT_TOP_N spec.
  - No yield size mode (yield_size_eligible False), as in the scan.
  - Hour windows and the Saturday multiplier unchanged. Scan members took
    both (IMM_SCAN_HOUR_MULT defaults to 1): the 9/30-10/02 cycle logs show
    hour_mult 2.00 on them in 0-9 ET (Suez weekly ~78 contracts a side
    overnight vs ~41 by day). The review had proposed "x1"; that would have
    halved the size the +$501 was earned at, so they keep the scan's size.
    From 10/5 that means the global 0-8 ET x3 and the evening window, which
    the scan would have applied too.
- SERIES_BLOCKLIST_PREFIXES: `<series>-` for 36 Fiscal.ai KPI series (the 27
  in _DEFAULT_COMPANY_SERIES plus the 9-series finecon KPI set), 10 food
  trackers (incl. KXBKDWHOPPER from the scan and KXAMSAVO) and
  KXAAAGASMAXM / KXAAAGASMINM. The trailing dash makes each entry exactly one
  series: "KXBA-" leaves KXBABELMANDEBWEEKLY quotable, "KXCMG-" leaves the
  Carbon Arc KXCMGFT / KXCMGCC, "KXDG-" leaves KXDGCC. Markets, events and
  the "<series>-X" probe match. Standard semantics: no new orders, resting
  quotes cancelled next cycle, positions ride. The allowlist entries stay.
- Foot traffic KXBKFT / KXYUMTBFT stay: they are Carbon Arc FT series (in
  carbon_arc_fair's series map), not company KPIs.

WATCH:
- Startup / first refresh: no "open-scan admit" lines; graduates select as
  normal-book members ("family override inherited from KXSUEZWEEKLY").
- Graduates under the $300k budget: the scan's 60 slots no longer bound
  them, the budget and the payout floor do. Strikes stay at 3 per event.
- Blocked KPI / food positions ride to settlement unmanaged by quotes.

REVERT: `IMM_SCAN_TOP_N` out of the launcher + restart_imm.ps1 -Task (the
scan returns at 60 slots); IMM_ALLOW_SCAN_GRADUATE_SERIES="" in the launcher
removes the graduates; delete the `_CUT_*` line in SERIES_BLOCKLIST_PREFIXES
to un-block.

Tests: test_imm_scan_graduates.py (7): graduates allowed / not blocked, scan
losers stay out, 3 strikes per event, no yield mode, the guard-set clone
(incl. a gas graduate), the cut series frozen, and the one-series-per-entry
neighbours (KXBABELMANDEBWEEKLY, KXCMGFT, KXAMZNCC, KXDGCC, KXLOWCC, the
Carbon Arc FT series, KXAAAGASM / KXAAAGASMTX) not.

### 2026-10-03 night — Four company-KPI series un-blocked: Robinhood x2, First Solar, Coinbase (Jack)

Jack asked why the rewards report showed Company KPIs positive in the weeks of
Sep 21 and Sep 28. The report books rewards on the day Kalshi pays them, and
KPI programs pay when each period ends: the $108 earned in the Sep 14 week
(dashboard, at the paid rate) was paid over the next two weeks ($111, then
$148), while trading lost money in every one of those weeks (report: -$10,
-$295, -$36, -$86). On the report's own weeks Sep 7 - Oct 3 the class is
-$164 credited / -$115 earned; since July -$429 (credits $405, trading
-$790), negative in 7 of 11 weeks. The review's "-$312 over 4 weeks" was
overstated: its window opened on Sat 9/6, a single -$194 trading day.

Per series the losses are concentrated (lifetime: KXBA -$106, KXGOOG -$87,
KXCMG -$84, KXRBLX -$47, KXINTC -$45, KXSBUX -$40, KXTLN -$38, KXDPZ -$31),
and four series were positive throughout. Jack: "unblock those four":
- KXHOOD + KXHOODA (Robinhood): +$92 lifetime, rewards $77 and trading +$15;
  about -$6 on the dashboard's last 4 weeks.
- KXFSLR (First Solar): +$29 lifetime, +$24 on the dashboard's last 4 weeks.
- KXCOINBASE: +$16 lifetime (no market in the last 4 weeks).
They leave _CUT_KPI_SERIES; the annual KXFSLRA (no history) stays cut.
Code-only change: the bot's code-change exit applies it (handoff, no -Task).

Tests: test_imm_scan_graduates.py gains test_the_four_unblocked_kpi_series_
quote_again; 1,957 green.

## 2026-10-03 night — NFL player props quoted only inside a player-history fair band, injured players out (Jack)

Jack: "how to avoid getting blown up on the NFL markets like
KXNFLESCALATORREC-26OCT05ATLNO? can you model what it should be, based on
past data of the player to protect from the market going super out of wack
like the 400-lot overnight fill: the ATL@NO Robinson escalator."

WHAT HAPPENED (orders / fills logs). The Bijan Robinson receptions
escalator (pays floor(1e4 x (min(rec,14)/14)^3)/1e4) listed 00:24Z 10/3 and
traded 3-6c until 05:02Z, when the touch jumped 6 / 97 -> 45 / 60. The quote
loop follows the touch: bid 6 -> 8 -> 12, amended to the 40c touch at
05:15Z with 400 lots (Saturday quiet hours), filled 400 @ 40 at 05:36Z; book
6 / 7 again by 10:14Z (6.8c at 23:52Z), -$134. Same evening his receptions
ladder ran 17 -> 44 -> 24 and filled 200 @ 42 (fair ~20), Diggs' @ 51 (fair
~21), Johnston's yards ladder @ 48 (fair ~9). The open NFL prop book marked
-$544 at 23:52Z. Two more patterns: McLaurin went Doubtful on ESPN at 14:42Z
and the bot bought his yards ladder 250 @ 12 at 16:43Z (3 / 4 by evening);
Keenan Allen went Out at 14:37Z and Downs' ladder repriced 24 -> 34 against
the bot's short 400. And the RB receiving-yards escalators trade ~2c against
a 0.1-0.5c fair; the bot kept buying 200-lots of them at 2c.

THE MODEL -- nfl_prop_fair.py (docstring has the detail). Calibrated
2026-10-03 out of sample on nflverse weekly stats (every 2025-26 game of a
QB/WR/TE/RB predicted from that player's earlier games only, ~2,200 starter
games per stat):
- mean = EWMA of the player's games, half-life 8 games, earlier seasons
  x0.5 (best of 21 combinations on all four stats), shrunk mu = a x ewma + b
  (receptions 0.902 / 0.066, rec yds 0.867 / 2.94, rush yds 0.833 / 6.59,
  PPR 0.894 / 0.63). Targets / carries add nothing.
- shape: var / mean is flat across levels -- receptions NB1 var 1.3 mu,
  yards / PPR gamma var 23 / 21 / 5 x mu. Escalator payouts predicted vs
  paid: rec 4.71 vs 4.63c, rec yds 3.23 vs 3.14c, rush yds 4.02 vs 4.10c, by
  quintile within ~0.5c, and down to 0.1c fairs (RB rec yds 8-13: 0.20 vs
  0.11c -- the 2c market is the mispricing, not the tail).
- payouts read from custom_strike; reproduce all 596 settlements to 10/1
  exactly (60-69 yards pays 0.0270: a float trap in floor()).
- vs the market: on 368 settled props with a pre-game book, model MSE 59.5
  vs the pre-game mid's 59.1 (correlation 0.89-0.95).
- band [fair_lo, fair_hi] = the payout at mu x 0.7 / x 1.5 (x 0.5 / x 2
  under 8 games; under 3 games no fair).

THE GATE -- nfl_gate in incentive_mm.py, every NFL ladder / escalator
(sports_ladder_league == NFL, so a new stat stands aside until modelled),
the Pokemon gate's shape: refresher thread "nfl-fair" (Kalshi family 15m,
nflverse current season 6h / older weekly, cached run-logs/incentive-mm/
nfl_stats; ESPN rosters 10m, ~28 KB gzipped per team), status file
nfl_prop_fair.json, quote loop + estimator probe. Stands aside (cancel) on:
no read / stale (30m), market not priced, roster read missing / stale (45m),
ANY ESPN designation (Questionable, Doubtful, Out, IR, not on roster), a
touch over NFL_BAND_TOL_CENTS (3) outside the band on the side that would
fill us. Otherwise bids <= floor(fair_hi + 1c), asks >= ceil(fair_lo - 1c)
(mort_cap_quotes, last). Positions ride while stood aside (as every gate).
Kill switch IMM_NFL_FAIR_ENABLE=0. CLI: `python nfl_prop_fair.py
[--event E | --ticker T]` prints every open prop's mu / fair / band / book.

MEASURED IMPACT. 517k logged tight-book cycles 9/24-10/3: the touch sat
above fair_hi + 1c 1.3% of the time, never under fair_lo. Fills 9/30-10/3
replayed: the 32 outside the band lost $384 at settlement / model fair
(-$280 at marks), the 241 inside made $97. Live dry run 00:25Z 10/4 (511
open props): 485 quote untouched, 12 bid-capped below the touch (the 2c RB
escalators), 10 injury stand-asides (McLaurin, Zay Flowers), 4 band
stand-asides (Downs x2, Nacua's 60c bid, Robinson's yards ladder 18 / 20 vs
9.3).

NOT DONE: other leagues' ladders (NBA from late October) are still
unguarded; teammate news (Allen out -> Downs up) is only caught once the
book has moved past the band; the inactive list (~90 min before kickoff) vs
the kickoff - 30 min cutoff is unchanged; per-market caps are in contracts,
so a 400-lot at 40c risks $160 where one at 6c risks $24.

Tests: test_nfl_prop_fair.py (14: payout tables, distributions, the EWMA,
tickers / names / rosters, the snapshot, the watch end to end with caching),
TestNflPropGate (fail-closed reasons, caps, the Robinson night end to end,
kill switch); the suite neutralises the gate at import. 1,968 green.

## 2026-10-04 — NFL fantasy ladders stop quoting 90 minutes before kickoff, when the inactive list posts (Jack)

Jack: "So block fantasy ladders 90min before kickoff to align to when
inactive reports come out", after the question whether a market cancels
when the player is inactive.

THE CONTRACT TERMS (assets.kalshi.com/contract_terms, read 2026-10-04):
- FFPTSSCALAR.pdf (KXNFLFFPTSLADDER): a player Sleeper gives no figure
  (bye, did not play, inactive, removed) is deemed to have scored 0.0
  points -- YES pays $0, NO $1. A surprise inactive is a total loss for YES.
- FOOTBALLENTITYSCALARSTAT.pdf (the receptions / yards ladders and
  escalators): a player who takes no snap resolves at the Exchange's last
  fair price, which may be the last trade before the non-participation
  became known or reasonably anticipated. One snap and it settles on the
  real stats.
- No precedent yet: every one of the 602 props settled through 10/1 had a
  stat line. McLaurin (Doubtful 10/3 14:42Z, reported out) would be first.

CHANGE. NFL_FFPTS_START_BUFFER_MIN (IMM_NFL_FFPTS_START_BUFFER_MIN, 90):
ensure_family_override sets start_buffer_min on the KXNFLFFPTSLADDER clone
of the KXNFLLADDERREC archetype (nfl_ffpts_series: NFL + FFPTS), so the
selection cutoff is kickoff - 90 where the rest of the family keeps kickoff
- EVENT_START_BUFFER_MIN (30); the estimator window, the screen and
imm_quote_gaps read the same override. Orphan-restored positions already
stop at the ticker-date fallback. Positions ride past the cutoff, as with
every cutoff.

Tests: TestNflFantasyLadderCutoff (only the NFL fantasy ladder takes the 90,
every other guard still the family's; the knob; a cycle's selection cutoff
kickoff - 90 vs - 30 for a receptions ladder). 1,982 green.

## 2026-10-04 — Opportunistic email: finecon and open scan only, Carbon Arc out (Jack)

Jack, on the 10/04 email ("est $2,351 accrued, net $-29 ($2,975 paid to
date)"): "email should only have finecon and open scan, remove carbon arc".

CHANGE (send_opportunistic_imm.py, reporting only -- quoting is unchanged):
tier_of returns None for every Carbon Arc-settled market, ahead of the
finecon and scan tests. excluded_carbon_arc decides: the settlement-source
verdict where one exists (the script's carbon_arc_series.json cache, else
imm.FAMILY_VERDICTS), the CC/ADS/POS suffix rule only while none does, so a
CC-named series with a not-Carbon-Arc verdict stays reportable. The CARBON
ARC table and cumulative row are gone; the headline, the subject, the "N
tiers" count and the Kalshi-credited footer cover finecon + scan only. The
settlement-source lookups stay -- they now decide what is left out.
_family_suffix / _family_event_top_n (the old tier line) are deleted.

The 12 September point-of-sale events the open scan admitted before point
of sale became a normal-book family (9/22) leave the scan's cumulative line
with the rest of Carbon Arc: credited $79.82, realized -$217.53.

DRY RENDER 13:04Z 10/4 (live state): 75 events / 35 markets across 2 tiers;
subject "est $430 accrued, net $-221 ($947 paid to date)". Cumulative OPEN
SCAN 155 -> 143 events, credited $900.07 -> $820.25, realized -$673.21 ->
-$455.68. No Carbon Arc event in the text, the HTML or the shown-events
record.

Tests (TestOpportunisticEmail): test_carbon_arc_is_left_out_of_the_email,
test_tier_precedence_matches_the_bot (Carbon Arc first; a not-Carbon-Arc
verdict keeps a CC-named series), test_no_family_series_the_bot_allows_
reaches_the_email (replaces "every family series has a tier"),
test_carbon_arc_exclusion_is_by_settlement_source. 1,984 green.

## 2026-10-04 — NFL props: teammate absences move the fair, and a team news hold (Jack)

Jack: "build the teammate adjustment and news hold, dont merge it yet
though" -- after the read that Downs' ladder went 24 -> 34 when Keenan Allen
was ruled out and the gate could only stand aside -- then, with the hold cut
to 30 minutes and the earning check made exact: "ship both earnings and
teammate rule" (deployed 10/4).

THE FIT (nflverse 2024-26, the definitions the code uses). A teammate's
share = his targets (carries) / the team's over its last 4 games, games he
missed counted as zero (one out for weeks has little left: the EWMAs already
carry his absence); absent = played in one of the team's last 2 games, no
line in this one. With V the absent teammates' summed share, a remaining
player's stat ran x (1 + alpha x V / (1 - V)) of his history's prediction
(WLS, weights mu; bootstrap 90%): receptions 0.18 [0.13, 0.24], receiving
yards 0.14 [0.08, 0.21], PPR WR/TE 0.08 [0.01, 0.17], RB rushing (carries)
0.33 [0.23, 0.48], RB PPR (carries) 0.23 [0.14, 0.35]. That is ~30% of a
proportional hand-out of the vacated targets. By size: a vacated target
share of 0.2-0.3 -> +5% catches, 0.3+ -> +20%; a lead back out -> +37% to
the backup's rushing. A starting QB out (>= 70% of the window's attempts):
receptions -5%, yards -2.5%, RB rushing -9%.

MODEL (nfl_prop_fair.py). PlayerIndex.team_shares (targets / carries /
attempts per team-game from nflverse, now parsed); roster_status keeps each
player's position and designation time; team_context per team: designated
contributors who played in one of the last 2 games, weighted by P_MISS (Out /
IR / suspended 1, Doubtful 0.8, Questionable 0.25, day-to-day 0.1),
teammate_mult per stat and position (QBs untouched, V clamped at 0.6). The
entry's mu is the adjusted one; mu_base, team_mult and teammates show the
working. Knobs IMM_NFL_TEAMMATE_ENABLE (1), IMM_NFL_TEAM_WINDOW_GAMES (4).

NEWS HOLD (incentive_mm.nfl_gate, NFL_NEWS_HOLD_MIN 30 -- Jack: "stand aside
for 30min, not an hour"; 0 = off): every prop of a team stands aside for
30 minutes after a CONTRIBUTOR (>= 8% of targets,
15% of carries or 50% of attempts over the window) is designated, changes
designation or is cleared -- ESPN's designation time, or the refresher
seeing the status change between reads (first sighting stamps nothing). A
fringe player's news holds nothing. Game-day inactives land ~90 minutes
before kickoff, so a team with inactive news is out from the news to its
cutoff.

LIVE DRY RUN 12:53Z 10/4 (511 props, before deploying): 260 adjusted, mostly
small (Questionable teammates x0.25); Downs x1.066 (Allen out + Pierce IR),
Diggs x1.082 (McLaurin + White out), Wicks x1.15 (Smith + Goedert out), the
Tampa receivers x0.95 (Mayfield out); news holds on TB (Mayfield, 0.2h) and
WSH (White's entry, 0.8h -- the London inactive list).

Tests: TestTeammates (P_MISS, window shares with misses as zero, the context
and multipliers, lead back + QB out with the clamp, news needs a
contributor and tracks changes, the snapshot carries it), TestNflPropGate
.test_news_hold.

### 2026-10-04 — NFL props: only the sides that earn rest (Jack)

Jack, on the branch's Downs case (escalator book 15 / 17, which the teammate
adjustment moved from stand-aside to quoted with the bid capped at 14):
"only quote a market if you're earning". A fair-gate cap moves a rung off
the touch; when the program's scoring walk (the first target_size contracts
from the best price, the reference where target/5 accumulate -- _side_share)
never reaches it, it earns nothing and fills only when the book moves
through it.

drop_unearning_sides (after the NFL caps, in the quote loop and the
estimator probe): on the EXTERNAL book (our resting orders netted out, the
side's rungs and pads overlaid), a rung earns iff the side qualifies (depth
>= target) and the depth strictly in front of it is under the target -- the
walk reaches its level; a side with no earning rung is dropped; with neither
side earning nothing rests, pads included. TO THE CENT AND BELOW (Jack: "the
bid/quotes would be to the cent right?"): the quote loop judges sub-penny
books on their EXACT levels (orderbook_exact_levels, our orders at their
exact prices) -- a rung capped at 14.00 behind a maker at 14.60 shares the
14c bucket but not the walk; whole-cent books and the estimator's probe use
whole cents. Uncapped rungs rest at the exact external touch (the sub-penny
join, e.g. 16.99); a capped rung rests at the whole-cent cap. Logged once per change ("nfl not earning <t>: bid side(s) out of
the scored walk ..."), state _nfl_unearning.

Live dry run 13:09Z 10/4 (511 props, target 500): 484 both sides at the
touch, 22 stood aside (12 news hold, 10 injury), 3 capped but still inside
the walk on both sides (Downs' escalator among them -- the 15 level had
thinned since 03:23Z, so a 14 bid scores again), 2 with the bid dropped
(Tuten's receptions ladder 9 / 11 capped at 8; Dobbins' receiving-yards
escalator 2 / 3, its 1c bid behind the maker's 2c). The verdict is per cycle
on the live book.

Tests: TestNflPropGate.test_drop_unearning_sides (deep touch drops the
capped side, a thin touch keeps it, nothing earning rests nothing, our own
orders netted out, a rung at the touch earns) and
.test_downs_book_rests_only_the_earning_side (atref, the live ladder mode).
1,993 green.

## 2026-10-04 — Carbon Arc fair reads a month from its first day; early reads widened where they are noisy (Jack)

Jack asked to check that the Carbon Arc markets quote off realtime data,
then "yes ship it" on the fix.

FINDING. The fair gate (2026-09-26) had never stood a market aside: 0
"ca_fair" guard skips and 0 stand-aside log lines 9/27-10/04, and none of
the 567 Carbon Arc fills since 9/13 had a read of their measurement month.
- The September book stopped quoting 9/26 04:00Z (CA_LATE_STOP_DAYS), a
  day before the feed came online (9/27 04:06Z).
- The October book has quoted since 10/01, but carbon_arc_fair held a month
  back until 10% of it was observed. With the feed's 2-6 day lag that is
  ~6-10 days of no read. Point of sale rolled to October on 10/02 23:00Z
  with 1 day of data, so all 18 POS series read "missing"; card / app /
  foot traffic / ads were still on September.
- October fills 10/01-10/04 were the worst-marked Carbon Arc fills yet:
  -6.9c/contract at 1h, -9.4c at 24h, -$462 on 5,426 contracts, POS -$231.
  At 10/03 20:14Z every C4POS ask (T112-116, 12-44c) was lifted while the
  Oct-1 read on disk said 131; the mid rose 28-48c within the hour. The
  family still netted positive (dashboard 10/03: $600 rewards, -$400
  trading), so standing the book down was not the fix.

REPLAY of the 147 October fills and every October cycle (stand-aside cost =
each ticker-hour's estimated reward x the share of its cycles gated):
- POS read from day 1 at the formula's sigma: 19-20 fills prevented, ~$142
  of the loss, for $34-46 of reward. Flat to the POS sigma: x1.3 +$99 net,
  x1.6 +$82, x2.0 +$75 (x1.0 +$96).
- September's read carried into October: $31 avoided for $181 of reward.
- Not quoting until a read exists: all $462 for ~$1.4k est of reward.

CALIBRATION (September's daily paths, 75 entities: each day-k read against
the month's last read, over the sigma the formula gives that day): card
spend missed by 7.7x sigma on day 1 and 1.4-2.4x on days 2-5; foot traffic
1.2-1.9x on days 1-2 (plus a Labor Day shift on days 4-5); apps and ads
stayed inside sigma (0.6-1.35x) -- the 20%-of-mu noise cap already turns
most of them off early.

CHANGE (carbon_arc_fair.py; incentive_mm.py untouched):
- CA_MIN_OBS_FRAC 0.10 -> 0 (IMM_CA_MIN_OBS_FRAC): a read is an entry from
  its first day.
- sigma x early_sigma_mult = max(1, A / sqrt(days observed)), A per prism
  category (CA_EARLY_SIGMA_A): card 3.4, foot traffic 2.5, apps / ads 1.8,
  point of sale 3.4 (shipped at 1.0 in 0c38351, raised the same hour --
  see POST-DEPLOY); unknown category 3.4. Card days 2-5 come to
  0.95-1.0x, foot traffic days 1-3 to 0.7x.
- CA_MIN_OBS_DAYS {"credit card": 2}: a one-day card read is past repair.
- Entries carry "early_mult"; the fair file's model block carries
  min_obs_days and early_sigma_a.

AT DEPLOY (dry run on the 13:00Z feed and today's book): 93 entries, 0
missing (18 POS series on October). 44 of 136 October POS markets stand
aside, every one on the ASK side: the book prices every POS series well
under its Oct-1 read (C4 131, ON! 124, Coors 98 ...). That is the
behaviour the replay scored; if the day-1 reads carry a weekday bias (Oct
1 was a Thursday, 2025's a Wednesday) it fades as the month fills in.

POST-DEPLOY (live 13:39Z via the code-change restart the NFL commits
triggered; 93 series with a read, 0 missing). The POS prism had refreshed
at 12:58Z with Oct 1-3, and the reads fell 5-10% from the Oct-1 values:
Miller Lite's MTD went 97.7 (Oct 1) -> 108.7 (Oct 2) -> 95.0 (Oct 3),
every brand in step (C4 131 -> 121, Rogue 74 -> 66, Mountain Dew 94 ->
87). That is the weekday mix (or a thin latest day -- Oct 1's own value
was revised only +0.2 a day later), a ~1.2-sigma RMS move in two days, so
the A=1.0 gate flipped sides: 63 of 136 POS markets stood aside, 51 on
the BID side (book ~September levels, read 5-10% under). POS takes card
spend's A=3.4 (days 1-11 only): 47 stand aside at today's day-3 read (38
bid, 9 ask), 22 more drop under the noise cap. The day-1 replay still nets
+$70 at x3.4 ($86 avoided, $16 forgone) vs +$96 at x1.0.

KILL / REVERT: IMM_CA_MIN_OBS_FRAC=0.10 and IMM_CA_EARLY_SIGMA=0 in the
launcher env (restart_imm.ps1 -Task) restore the old model; the gate
itself is still IMM_CA_FAIR_ENABLE=0. carbon_arc_fair is imported by the
refresher thread, so any change to it loads only on a bot restart.

WATCH: POS stand-asides vs its rewards through ~10/10 -- ~1/3 of the POS
book is parked on the day-3 read; whether the read or the book was right
shows in the POS marks over the week. Watch whether the latest POS day is
revised up the next morning (a thin last day would bias every early read
low). Card spend gates from its day-2 read (~10/06), foot traffic / apps
/ ads as their prisms roll. Calibrate POS's A from October's own path once
it is ~10 days long.

Tests (test_carbon_arc_fair): test_a_read_is_an_entry_from_its_first_day,
test_early_reads_are_widened_by_category,
test_early_sigma_knob_off_and_the_old_model; the thin-read refusal now
runs under the 0.10 knob. 1,996 green.

## 2026-10-04 — Sports ladders / escalators OFF for now (Jack)

Jack: "turn off LADDER/ESCALATOR events for now" -- after the weekend's
fills (the Robinson 40c escalator; -$544 open at the 10/3 23:52Z marks) and
the read that ~40k of the day's 52k placement rejects were NFL prop orders
(the account had $1.5-3.8k of free cash against ~$83k of planned ladder
collateral; the $300k budget never binds).

CHANGE. SPORTS_LADDER_BLOCK (IMM_SPORTS_LADDER_BLOCK, default 1) appends the
ladder allowlist's own pattern (_SPORTS_LADDER_PATTERN: every league's
LADDER / ESCALATOR series) to SERIES_BLOCK_PATTERNS. The blocklist wins in
_allowed and restore_orphan_metas skips blocked tickers, so the standard
block semantics apply: no new orders, resting quotes cancelled the next
cycle, NOT reduce-only, positions ride to settlement. Startup logs "sports
ladders / escalators: OFF".

KEPT IN CODE, unchanged: the NFL fair gate (nfl_prop_fair, nfl_gate), the
90-minute fantasy cutoff, the teammate adjustment, the 30-minute news hold,
only-what-earns, the x5 family size. The nfl-fair refresher keeps running
(nfl_prop_fair.json stays fresh for the CLI / nfl_prop_snipe.py). Turning
the family back on: IMM_SPORTS_LADDER_BLOCK=0 in the launcher +
restart_imm.ps1 -Task, or flip the default.

Tests: TestSportsLadderBlock (the default blocks every ladder / escalator
series, NFL and NBA, and nothing else; with the pattern out they are allowed
again); the suite takes the pattern out at import so the family's fixtures
still run. 1,997 green.

### 2026-10-04 — Revised: ladders / escalators quote YES BIDS ONLY instead (Jack)

Jack, before the full block had loaded (the bot was waiting for its :50
safe window): "actually turn off the ASK sides only, bid sides can stay on".

CHANGE. SPORTS_LADDER_BLOCK's default is now 0 (the switch stays).
SPORTS_LADDER_ASKS_OFF (IMM_SPORTS_LADDER_ASKS_OFF, default 1) puts every
ladder / escalator series into series_bid_only -- the quake family's
bid-only machinery: lv_ask empty in the quote loop (so no full-unwind ask
either: a long position rides, a short is still reduced by bids), no ask
pad, the two-sided depth test never counts an ask pad, side_size_mults
zeroes asks so the estimator projects the bid side alone. The quake gate's
own branches (the quote-loop gate, the probe's fair cap, the restart-order
exclusion) now key on quake_gated(), so the ladders run none of the quake
logic. Startup logs "sports ladders / escalators: YES BIDS ONLY".

WHY ASKS: on a cheap contract the ask is the expensive side -- selling YES
at 5c locks 95c of cash a contract against 5c for a 5c bid -- and 42k of
the day's 52k cash-bound rejects were asks.

Tests: TestSportsLadderAsksOff (default and scope, bids only end to end with
no ask pad, a long gets no unwind ask), TestSportsLadderBlock.
test_block_switch; the suite turns the ask-off default off at import for
the two-sided fixtures. 2,000 green.

## 2026-10-04 — Carbon Arc gate rests the sides that earn instead of parking the market (Jack)

Jack, after 45 of 136 October point-of-sale markets sat parked on the
day-3 read: "the bot should sit on the bid if it's earning money on it, but
not if it wouldnt earn" -- the NFL props' rule from the same morning ("only
quote a market if you're earning", drop_unearning_sides).

CHANGE (incentive_mm.py, CA_FAIR_CAP / IMM_CA_FAIR_CAP, default on): a touch
that fights the Carbon Arc read no longer parks BOTH sides. The side that
fights it is capped at the limit the breach is judged by -- bids at most
the band top + CA_FAIR_TOL_CENTS (15), asks at least the band bottom - 15
(ca_fair_caps; a floor at or under zero is no floor) -- and the caps and
drop_unearning_sides run last in the quote build, exactly as the NFL caps
do (exact levels on sub-penny books): a side whose capped rungs the
program's scored walk no longer reaches does not rest; the side the read
favours quotes at the (safe-)join as usual. The refresh hold (a new read
parks both sides for 10 minutes) is unchanged. Logs: "ca-fair capped <t>:
<book vs fair>; bids capped at Xc ..." once per episode, "ca-fair not
earning <t>: bid side(s) out of the scored walk ..." when a side drops,
"ca-fair uncapped <t>" when the book is back inside the band (or the read
goes away). A capped market writes its normal cycle_log row (no guard skip).

DRY RUN on the live books (14:28Z, the 46 markets parked under the old
rule): 22 bid breaches rest the ask only (the bid capped behind a deep
touch earns nothing), 14 rest the ask plus a bid at the cap (thin front:
the walk still reaches it), 8 ask breaches rest the bid plus an ask at the
floor, 2 no longer breach. None stays parked.

KILL: IMM_CA_FAIR_CAP=0 (launcher env, restart_imm.ps1 -Task) restores the
both-sides stand-aside.

Tests (TestCarbonArcFairGate): test_caps_are_the_breach_limits,
test_bid_touch_over_fair_rests_only_the_earning_side_then_resumes,
test_a_capped_bid_that_still_earns_rests_at_its_cap,
test_ask_touch_under_fair_rests_only_the_bid,
test_cap_off_parks_both_sides_as_before. 2,003 green.

### 2026-10-04 — Ladder / escalator asks come back on at $4,000 free cash (Jack)

Jack: "once cash is over $4k, turn on the full ladder/escalator again".

CHANGE. SPORTS_LADDER_ASKS_ON_CASH (IMM_SPORTS_LADDER_ASKS_ON_CASH, 4000;
0 = never). _check_balance_floor already reads /portfolio/balance every
full live cycle; right after a good read, _ladder_asks_latch fires the
first time free cash (balance_dollars, the figure the startup line logs as
"cash") is at or over the threshold: it sets state.ladder_asks_on_at
(persisted, NOT tied to the roll day) and the module mirror
_LADDER_ASKS_STATE, logs and alerts ("ladder_asks_on", not urgent) once,
and series_bid_only stops counting the family -- both sides quote from that
cycle. ONE-WAY, as asked: a later dip does not turn the asks off again;
IMM_SPORTS_LADDER_ASKS_ON_CASH=0 ignores the latch (bids only again), or
clear ladder_asks_on_at from imm_state.json with the bot stopped. Startup
logs which state it is in ("YES BIDS ONLY ... until free cash reaches
$4,000" / "asks back ON since ...").

At deploy: free cash $1,914 (14:30Z); bids only verified on the live book
(14:27Z cycle: 446 NFL prop markets, 50,245 bid contracts resting, 0 asks).

Tests: TestLadderAsksCashLatch (fires at $4,000 not $3,999, one alert, a dip
keeps it on, quakes stay bid-only, persists across a restart, the 0 knob
ignores an old latch). 2,006 green.

## 2026-10-04 — NFL props: injury tags stand aside only the fantasy ladder; exact caps; the family quotes 0.05-99.5c (Jack)

Jack, on why KXNFLESCALATORRECYDS-26OCT04TENBAL was not all quoting (Flowers
stood aside as Questionable; Pollard's capped 1.00c bid sat behind 1,050
contracts at 1.02-1.03c; Henry's book is 0.03c / 0.99c, under the 1c band):
"for questionable, etc its ok to quote non-fantasy ladders because injury
risk being picked off is less risk", "caps sohld keep exact values",
"Ladders and escalators only quote from 1c to 99c -- change this to .05 to
.995".

1. INJURY. nfl_gate stands a designated player aside only on the FANTASY
   ladder (nfl_ffpts_series): a player who never takes a snap pays a
   fantasy YES $0 (FFPTSSCALAR), while the receptions / yards contracts
   resolve at the Exchange's last fair price (FOOTBALLENTITYSCALARSTAT).
   The designation still rides in the gate inputs; the team news hold and
   the stale-roster check are unchanged.
2. EXACT CAPS. The gate's caps are on the 0.01c grid (band top + 1c =
   1.47c, not 1c; floor band bottom - 1c, 0 = none) and nfl_cap_quotes
   moves a rung past its bound TO it -- exactly on a sub-penny market
   (price_cents its bucket), to the whole cent inside it otherwise.
   Pollard's 1.03c touch bid now stays (under 1.47c) and earns.
3. 0.05-99.5c. SPORTS_LADDER_PRICE_MIN_EXACT / _MAX_EXACT (env
   IMM_SPORTS_LADDER_PRICE_MIN_EXACT / _MAX_EXACT). The whole-cent band (1 /
   99) still drives the integer ladder; subcent_edge_quotes adds ONE rung
   AT the exact external touch on a sub-penny ladder / escalator side the
   integer ladder left empty when that touch is a bid in [0.05, 1c) or an
   ask in (99, 99.5c] (price_cents 0 / 100 is its bucket; the wire price
   is exact -- the client sends price_dollars and only reads yes/no_price
   for the side). Sized like the side (min of its ladder total and room),
   never crossing, our own orders netted out. Wired into the quote loop
   (after the sub-penny snap, before the caps), the estimator probe,
   quotable_sides, the meta's mid / spread (subcent_bid_cents: market_cents
   reads a bid under 1c as "no bid") and the extreme_mid screen (the
   family's band 0.05-99.5c). Henry's 0.03c bid is still under the floor.

Tests: TestNflPropGate -- reasons (a non-fantasy designation quotes, the
fantasy one stands aside), test_caps (13.78 / 1.07), test_news_hold, the
Robinson e2e (Questionable keeps quoting), test_nfl_cap_quotes_keeps_exact
_prices, test_subcent_edge_quotes, test_a_book_under_one_cent_quotes_at_
its_exact_touch (selected, bids AT 0.30c). 2,009 green.

## 2026-10-04 — the NFL prop SNIPER, a separate taker bot (nfl_snipe_bot.py); the IMM nets its book out (Jack)

Jack: "Build the sniper taker bot separately." nfl_snipe_bot.py automates
the nfl_prop_snipe.py scanner as its own process: it TAKES a book outside
the player-history band (sells YES into a bid past the band top, buys YES
from an ask under the band bottom) with immediate-or-cancel orders, so
nothing it sends rests. Its module docstring holds the rules; in short:

- The band it must clear is the WIDER of nfl_prop_fair's and the same band
  on the player's last 4 games (recent form -- the EWMA is slow to see a
  role that grew: Golden's yards book at 96 vs model 51, last four 84 / 95
  / 58 / 100), edge vs the less favourable fair. The limit is past the
  band top + 1c, strictly over the IMM's own bid cap (and a tick over any
  resting bid of ours), worth 2c and 3% of the money at risk per contract
  after the taker fee. Self-trade prevention taker_at_cross.
- Skips: any designation on the player, team news in 12h (its own watch,
  persisted across restarts, and the IMM's nfl_prop_fair.json -- a
  designation CLEARED is only seen by a watch that saw it set), a sibling
  contract that agrees with the book, a book implying >2.5x / <0.4x the
  model's mean, the IMM's run of the model >15% apart, a stale model /
  roster, inside 15 min of kickoff (ESPN scoreboard), a 15-min cooldown.
- Risk: $40 a market / $80 a player / $250 total at risk (100 - P a
  contract sold), free cash on the shard over $500, 3 orders a scan, 40 a
  day. DRY RUN by default (paper fills in snipe_book_paper.json); --live
  trades; --subaccount N runs in a numbered Kalshi subaccount (own cash and
  positions, invisible to the IMM's reads). Halt: run-logs/nfl-snipe/HALT.

IMM side (only matters on subaccount 0): fetch_positions nets the
sniper's live book (run-logs/nfl-snipe/snipe_book.json, IMM_SNIPE_BOOK_FILE)
out of the account's positions -- read as the account's, a snipe would trip
the manual standoff on the whole EVENT. A market the account shows flat
(the sniper's short against our long) comes back as our long; one neither
holds is left alone; no file nets nothing.

Found on the way (10/4 16:00Z dry run, 506 props): the ladder books' BIDS
sit at ~1.6-2.4x the players' output across the board (Malik Washington's
receptions ladder 34c = 6.8 catches after 3/3/4/5; Pickens' yards 34c =
136 yds after 9/28/40/82) -- the ask side is nearly empty since the makers
stopped selling YES (collateral), and the bidders compete for the reward
queue. 42 books outside the band; recent form, news, siblings and the
ratio guard leave ~2-16 to take.

Tests: test_nfl_snipe_bot (28), TestSnipeBookNetOut (2).

## 2026-10-04 — ladder / escalator asks: ON over $4,000, OFF under $2,000 (Jack)

Jack, the same evening: "above $4k open up all ESCALATOR/LADDERS, below $2k
turn off the ask side just like you did today". The one-way $4k latch
(afd288b) is now a two-way switch with a band:

- Free cash at or over SPORTS_LADDER_ASKS_ON_CASH ($4,000) turns the asks
  on; under SPORTS_LADDER_ASKS_OFF_CASH ($2,000, env IMM_SPORTS_LADDER_ASKS_
  OFF_CASH) turns them off (series_bid_only again: no ask rungs, no ask
  pad, no full-unwind ask -- a long rides; resting asks go at the next
  requote); between the two the last state holds. 0 = the old one-way latch.
- THE CASH IS THE IMM'S SHARD'S: ladder_cash() reads exchange_index 0 of
  the balance breakdown (10/4 16:05Z: $1,292 of the $1,862 total; the
  crypto bots' shard-2 cash cannot back a ladder order), the cross-shard
  balance_dollars only when the read has no breakdown. The balance floor
  still uses the total.
- Each switch: state.ladder_asks_on_at (0 = off, persisted), a log line,
  a digest alert (ladder_asks_on / ladder_asks_off, not urgent).

Also in this deploy: b122e65 (cherry-picked) -- the IMM nets the NFL
sniper's subaccount-0 live book out of the account's positions, and a
refused placement logs Kalshi's reason (e.body / error_body). A numbered
subaccount's sniper book (snipe_book_sub<N>.json, "subaccount": N) is never
netted: its positions are not in the primary's read.

Tests: TestLadderAsksCashLatch (band both ways, the shard's cash, the 0
knob), TestSnipeBookNetOut (a subaccount's book), test_nfl_snipe_bot
TestBookFile. 2,046 green.

REVISED minutes later (Jack: "adjust to 1.5 and 3.5k instead of 2k and
4k"): ON at $3,500, OFF under $1,500 (the env knobs unchanged).

## 2026-10-04 — NFL gate: the band is the wider of the model's and the recent-form one (Jack)

Jack asked why some KXNFLFFPTSLADDER-26OCT04DETCAR markets quoted one side:
Hubbard's and McMillan's books were bid over the model's band, the bid cap
(band top + 1c, 19c / 17c) sat behind 3,510 / 8,572 contracts at the touch,
and only-what-earns dropped it. McMillan's model (10.75 PPR) matches his
last four (10.6); Hubbard's (12.6) did not -- 2026 games 23.7 / 14.4 / 15.0,
the EWMA weighed down by late-2025 games of 3-5. Jack: "yes add it and
ship".

- nfl_prop_fair: each entry also carries recent_mean (the stat's mean over
  the player's last RECENT_GAMES = 4 games, IMM_NFL_RECENT_GAMES; >= 3
  needed) and fair_recent / _lo / _hi -- the same payout and band on that
  mean, the teammate multiplier applied (the fair cache shares it).
- nfl_gate: lo / hi = the wider of the two bands (NFL_RECENT_BAND,
  IMM_NFL_RECENT_BAND=0 = the model's alone), for the caps AND the band
  breach check. Inputs carry model_band and recent_mean. The fair itself is
  the model's (the sniper's model_mismatch check compares it).
- Hubbard: cap 19.94 -> ~28.3c, the bid joins the 20c touch. A declining
  player's recent band lowers the ask floor the same way.

Tests: TestNflPropGate.test_recent_form_widens_the_band, test_nfl_prop_fair
TestSnapshot.test_recent_form_band.

## 2026-10-03 — WebSocket books in SHADOW (Jack: "i only want to turn on the websocket shadow")

WHAT. kalshi_ws.py (new, stdlib only -- the box's Python has no websocket
library) runs one daemon thread with one Kalshi WebSocket connection
(wss://api.elections.kalshi.com/trade-api/ws/v2, signed with the bot's own
client: timestamp + GET + /trade-api/ws/v2). It keeps every managed book
(orderbook_delta: a snapshot per market, then deltas; every message's
per-subscription seq checked -- the `ok` answering update_subscription takes
a seq in the same sequence -- and a gap re-snapshots the subscription) plus
our fills (`fill`, filtered to imm- client ids). A book is trusted only with
a snapshot on the live connection, no gap since, and a frame inside
IMM_WS_STALE_SECS (20; the feed pings every 10s). Reconnects with backoff;
the thread never raises.

SHADOW = NO TRADING EFFECT. The quote loop reads REST exactly as before
(`_read_book`); each read is then compared with the WS book. Between
cycles the bot waits on the feed instead of time.sleep (`_idle`, same
deadline, never early in shadow) and runs the stale-quote check DRY: a
resting rung the external touch has left strictly ahead of the book, on
two looks >= 1s apart, >= 2s old, pads never -- counted once in
`would_cancel`, a sample logged ("WS fast (dry): would cancel ..."),
never cancelled. The idle loop looks at the feed at most
IMM_WS_IDLE_BATCHES_PER_SEC (4) times a second.

DEFAULT AND SWITCHES. IMM_WS=shadow is the CODE default (so a plain code
deploy -- the bot's own code-change restart, book handed over -- turns it
on) and is also set in the launcher. IMM_WS=off: no feed at all (+
restart_imm.ps1 -Task). IMM_WS=on (trusted WS books feed the quote loop,
REST otherwise) and IMM_WS_FAST=1 (live stale-quote cancels, next cycle
early) are in the code but NOT enabled.

VERIFIED LIVE, READ-ONLY (10/2-10/3, nothing placed or cancelled): 1,512
managed markets, every book in 1.0s, ~154 msgs/s, 0 gaps, 2.5% of one core;
102/102 REST spot checks EXACT (whole depth); with the production client
(build_client) through the bot's own _ws_start + _read_book: 40/40 books in
0.3s, 15/15 equal to REST. The stale-quote check run on the live bot's real
orders for 4 min flagged ~15-20 distinct orders ahead of the touch; the two
checked by hand were real (a 7Y-high bid left at 46c for minutes after the
bids fell to 29-30c; a 5Y ask placed 1c ahead off a minutes-old read).

WATCH. Startup: "WebSocket books (2026-10-03): shadow -- ...", "[IMM]
WebSocket feed started (shadow)", "[IMM] [WS] connected ...". Status
status_incentive_mm.json `latency.ws`: feed {connected, healthy, books_ok,
want, gaps, resnapshots, errors, msgs}, shadow {compared, exact, top,
ws_missing}, fast_stats {would_cancel, fills, events}. Healthy shadow =
exact/compared ~1.0, gaps ~0, books_ok ~ want.

NOT IN THIS DEPLOY: the cycle governor, TTL margin, order groups, by-date
decay and the 10/1 breadth rollback (branch claude/imm-latency-risk,
unmerged).

## 2026-10-04 — WS REST audit + stale-quote episode log (Jack: "yes do both")

WHY. The two safeguards before IMM_WS=on / IMM_WS_FAST=1. (1) Once the WS
book drives the quote loop, nothing re-checked it against REST: a stream
that is complete but wrong (an exchange glitch, or a message-format change
that parses into empty books) would hit every market at once. (2) Whether a
fast stale-quote cancel pays needs every would-cancel scored: was the rung
hit while stale, at what mark-out, against the reward the cancel gives up.
The counter alone can't answer that, and the log sample is capped at 20 an
hour.

AUDIT (`_read_book`, `_ws_compare`, `_ws_audit_note`). IMM_WS=on: 1 read in
IMM_WS_AUDIT_EVERY (20, a random draw) ALSO reads REST, trades on the REST
book and compares the two; a failed audit read trades on the WS book
(`rest_errors`). Only these draws feed the trip window, in every mode. So
shadow (which compares every read anyway) rehearses exactly the audit `on`
would run, and a data release's burst of books racing their REST reads
can't crowd the window. TRIP at IMM_WS_AUDIT_TRIP_TOP (5) top-of-book or
IMM_WS_AUDIT_TRIP_EXACT (15) whole-book mismatches among the last
IMM_WS_AUDIT_WINDOW (100) audited compares. The live base rate on 10/4 was
0.06% top and 0.18% whole over 5,054 compares, so 5 in 100 means broken.
A trip:
- sends every read to REST (each compared);
- pauses the stale-quote check (dry and live);
- calls `KalshiFeed.resync()`: every book is untrusted at once, and the
  feed reconnects without the failure backoff, taking fresh snapshots;
- raises an ALERT `ws_audit`, which emails only with IMM_WS=on.

RE-ARM after IMM_WS_AUDIT_COOLDOWN_SECS (900) once a full window of audited
compares since the trip is under both limits. A feed that stays wrong stays
tripped and does not alert again.

EPISODE LOG (`_ws_episode_flag` / `_ws_episode_end`). Each episode the
stale-quote check flags writes `ws_stale_<UTC date>.jsonl` lines: `flag`,
then `clear` / `amend` / `cancel` / `gone` (schema in IMM_LOGGING.md).
`would_cancel` now counts EPISODES: a rung that clears and goes stale again
counts twice, as each would be a fast-path cancel. Also fixed: an order the
cycle amends drops its first-seen-ahead time, so the two looks start over at
its new price (before, it carried the old price's clock).

PRESENCE CHECK (same evening, after the first live hour). A rung is only
flagged while the WS book still shows size at its price. The view is the
cycle's read, so an order filled out or pulled mid-cycle used to be flagged
with nothing left to cancel: KXHORMUZWEEKLY-26OCT11-T30 ask 25 @16c, lifted
at 18:51:01Z, was flagged at 18:51:53Z. A flagged rung that leaves the book
now ends `gone` at once. A partly filled remainder still shows, so it still
flags. ws_stale_score.py drops such flags logged before the fix ("phantom").

SCORING. ws_stale_score.py: the episodes plus fills_*.jsonl (by order_id),
cycle_log_*.csv (mid mark-outs) and the reward estimate (pool_per_day x
est_frac). The one-time scheduled task imm-ws-stale-score runs it on
2026-10-05 at 16:00 ET, over the first full day of episodes.

WATCH. Startup: "WS REST audit (2026-10-04): 1 shadow compare in 20 is
audited; trips at 5 top / 15 whole-book ...". Status `latency.ws.audit`:
{audits, rest_errors, trips, rearms, tripped, tripped_at, last_trip, every,
window, window_bad_top, window_bad_book}; `latency.ws.stale_open` (open
episodes). Feed status gains `resyncs`. A trip in the log reads "ALERT
[ws_audit] WebSocket books disagree with REST: ...".

UNCHANGED. Trading still reads REST (IMM_WS=shadow). IMM_WS=on and
IMM_WS_FAST=1 are still not enabled.

## 2026-10-04 (evening) — in-cycle stale check, event sweep breaker (dry), cycle timing (Jack: "Build these 3")

CONTEXT. First live hour of the episode log: 99 stale rungs flagged, and 1 hit
before the cycle fixed it itself (median 73s). That hit was a winner.

CORRECTED the same evening: 29-47% of fills_*.jsonl rows a day carry no ledger
join. Their order had left the ledger, filled out, before the fill was logged
(spun off as its own fix). The first pass read those rows as asks.
ws_stale_score.our_side / is_full now take Kalshi's side/action (exact on
every joined row) and count an unjoined row as full-rung. With that:
- Over the 4 days to 10/4, fills lost $246-830/day at 5 min (not $141-436).
- Full-rung fills on quotes LEVEL with the touch at the bot's last read lost
  -$266/day at 5m. These are sweeps, which no cancel speed fixes.
- Quotes already AHEAD of the touch lost -$121/day at 5m and -$200 at 30m
  (not -$19 / -$80). That is the part a fast cancel can reach. Those fills
  came a median 64s after the read.

The check only ran in the ~10s idle of each 2-4 min cycle. Three builds
followed. Nothing here changes trading.

1. WS BOOKS ON: READY, NOT FLIPPED. book_depth timing (17:52-18:45Z): a plain
   cycle spends ~60s of its ~137s median period reading ~1,025 books over
   REST. The universe-refresh cycles also read ~1,390 candidates over REST,
   for ~142s, and the feed doesn't cover those. IMM_WS=on serves the managed
   reads from the feed (1 in 20 audited), so a plain cycle should drop to
   ~80s, and every stale quote gets fixed about a minute sooner at no reward
   cost.
   - Status `latency.cycle`: {n, reads_s (cycle start -> last managed book
     read, median of the last 30 full cycles), run_s, period_s, last_reads_s,
     last_run_s}. Before/after for the flip.
   - The flip: IMM_WS=on in run_incentive_mm.ps1 + restart_imm.ps1 -Task,
     after the 10/5 health check (task imm-ws-stale-score). Jack's call.

2. EVENT SWEEP BREAKER (SWEEP_BREAKER, default dry). A maker WS fill that
   takes ALL that was left of our order trips its event: every quote we have
   there, all markets, both sides, for SWEEP_HOLD_SECS (300).
   - Sources: the remaining size comes from the resting view, booked down by
     earlier WS fills, or from the ledger for an order placed this cycle.
   - dry: ws_sweep_<date>.jsonl `trip` lines (the fill, the event's resting
     orders it would pull) plus a log line, and no cancels.
   - on: those orders are cancelled at once on the fast-cancel budget
     (WS_FAST_MAX_CANCELS_PER_MIN; the cycle's diff pulls any it skips), and
     `desired` drops the event's quotes until the hold ends (sweep_held(),
     next to the toxic-halt filter).
   - Backtest, corrected sides (fills 9/6-10/4, 10,761 maker fills marking out
     -$116/day at 5m and -$171 at 30m; 2s reaction; net of the event's
     modelled reward for the hold; per day, 5m / 30m mark-outs):
     30s +$12/+$16, 2 min +$21/+$31, 5 min +$25/+$41, 10 min +$26/+$42.
     At 5 min: ~206 trips a day, avoiding ~29% of the book's adverse
     selection for ~$9 of reward. On 10/3-10/4 alone (-$635/day at 5m):
     +$42 / +$85.
   - Variants: one side only nets ~80% of the event. An order-group-style
     trigger (>= 20 contracts in 15s) nets about the same. A 0s
     exchange-side reaction adds nothing over 2s, so Kalshi order groups
     aren't needed for this.
   - The reward cost uses the event's day-average rate, which runs low while
     the event is active; double it and 30m is still +$17/day.
   - Script: imm_sweep_backtest.py.
   - VIEW SIZES (fixed the same evening, after the first live fill missed its
     trip: KXRAIN-26OCT05-NOLA ask read at 38, amended to 30 this cycle,
     filled out for 30 at 20:42:40Z):
     - The rebuild takes an amended order's new size (_cycle_amended_ct).
     - It then takes off our WS fills (_ws_fill_log, exchange time) that
       landed more than 1s after the cycle's resting read
       (_resting_read_at, stamped when the paged read ends). So a fill at
       the boundary can only cause a missed trip, never a false one.
     - An order placed since the last rebuild nets its earlier WS fills
       off the ledger size.
     - Every maker fill is logged and booked into the view whatever the
       breaker's mode.

3. IN-CYCLE STALE CHECK (WS_CHECK_IN_CYCLE, default on; WS_TICK_SECS 1).
   _ws_cycle_tick runs the stale check and the sweep breaker inside the
   cycle, at most once a second. It is called from _read_book and the
   amend / cancel / place loops, and guards against re-entering itself.
   - In-cycle, an order this cycle already amended or cancelled is skipped.
     Its view entry is stale until _build_resting_view at the cycle's end.
   - Dry as before. Live only with IMM_WS=on + IMM_WS_FAST=1.
   - An in-cycle cancel is safe for the rest of the cycle: the cycle's own
     later cancel of it reads 404/409 as done (no cycle error), and an amend
     of it fails soft, so the next cycle re-places.
   - Episodes carry phase "cycle" / "idle". ws_stale_score.py splits them,
     and also scores the sweep trips.
   - RECHECK (same evening): each view rebuild sets _ws_recheck_all, so the
     next check covers every market, not only the books that changed. Without
     it, a quote ahead from its placement on a book that then went quiet was
     never examined.
   - REFRESH TICK (same evening): _estimate_candidate_yield ticks before each
     REST candidate read. The universe refresh (~1,400 reads, 40-140s every
     10 min) used to hold our WS fills: two sweep trips at 21:01Z were logged
     37s after their fills.

WATCH.
- Startup lines: "WS stale-quote check (2026-10-04): between cycles AND
  inside them ..." and "event sweep breaker (2026-10-04): dry -- ...".
- Log: "sweep breaker (dry): <ticker> BID @ <px>c filled out (<n>) -> would
  pull <k> order(s) in <event> for 300s".
- Status latency.sweep: {trips, cancels, skipped_budget, fills_seen,
  unparsed, unknown_order, mode, hold_s, held_now}. `unparsed` should stay 0;
  it would mean the WS fill format changed.

## 2026-10-04 ~21:35Z -- WebSocket books ON (Jack: "turn on the websocket books now")

run_incentive_mm.ps1 now sets IMM_WS=on, applied with restart_imm.ps1 -Task
(handoff, so the book is kept). The quote loop reads each managed book from
the feed when it is trusted, REST otherwise, with 1 read in 20 audited
against REST. A trip sends every read back to REST and emails ALERT
ws_audit.

Before: shadow from 17:51Z, 99.7-100% of books equal to REST, 0 gaps; a
plain cycle spent ~60s of its ~137s on REST book reads (latency.cycle
reads_s). Expect reads_s to fall to a few seconds, books_used to climb
and rest_fallback to stay small.

Unchanged: IMM_WS_FAST=1 stays OFF (the stale-quote check runs dry), and
the sweep breaker stays dry. Jack chose to flip it before the 10/5 scoring
task (imm-ws-stale-score), which still scores the dry experiments.

BACK OUT: IMM_WS=shadow in run_incentive_mm.ps1, then restart_imm.ps1 -Task.

## 2026-10-04 evening — Period rain totals (KXRAINNAPAM and the family) quoted through the rain fair (Jack)

Jack: "quote KXRAINNAPAM and similar families based on rain feed".

WHAT IT IS. KXRAINNAPAM "Napa rainfall totals" (contract RAINGLOBALPERIOD,
new 10/2): KXRAINNAPAM-<DDMONYY>-<DDMONYY>-T<inches>, "total precipitation
at KAPC in Napa ... strictly greater than K", the sum of The Weather
Company's daily values at Napa County Airport over the period, both days
inclusive. Events: Oct, Nov and Dec 2026, and the Nov 1-Mar 31 season.
Programs: only the season's six strikes (T10-T35), $500 each, 10/4 20:46Z ->
10/6 03:59Z, target 1000 (~$578/day the new-programs email showed). It was
never quoted: not allowlisted ("review: KXRAINNAPAM (unclassified)" in the
earnings-overrides log since 10/2). The same shape is KXRAINNYCW (the NYC
week, no programs now) and the rainstorm spans KXRAINS<CITY>.

THE FAMILY RULE. rain_period_gated(ticker): a KXRAIN series, then two
DDMONYY day segments (day FIRST: KXRAINNYCW-28SEP26-04OCT26 can only read
that way), minus the rainstorm spans. _allowed admits it, so a new city
needs no list edit -- but it quotes ONLY through the rain fair, failing
closed, and a station the writer cannot map stands aside. Guard set on the
archetype KXRAINNAPAM (KXRAINNYCW shares it; a new series clones it via
FAMILY_OVERRIDE_PARENTS): rain band 5-90c, cutoff close - 1560 min = 22:00
local the day before the period's last day (the monthlies' rule; the
season stops 21:59 PDT Mar 30). As KXRAIN-prefix series they are "daily"
(no quiet-hours or Saturday multipliers) and take the 7pm-01:59 ET halving.

THE FAIR (rain_monthly_fair.py; the same file and refresher thread as the
monthlies). P(to date + rest > K):
- station from the rules: "at CLIxxx", "(Kxxx;", "at Kxxx", "at XXX in", or
  an alias. CLI_STATIONS gains APC (Napa County Airport, CA_ASOS, US/Pacific;
  ACIS record from 1998-05-22, 28 full seasons). The rules' "from <date>
  through <date>" must equal the ticker's period, else no station.
- to date: IEM daily summaries for the period's past days (the ACIS record
  fills a day IEM lacks) + today's running total. Stale (fail closed) with
  more than IMM_RAIN_PERIOD_MAX_MISSING_DAYS (1) unread days, or when the day
  the station was last seen wet is unread, or wet today with no running
  total. No CLI at these stations.
- the rest: whole calendar-aligned historical windows (keeps the in-season
  correlation), the NWS forecast injected over its horizon as for the
  monthlies, and the historical part smoothed by a mean-one log-normal
  kernel (IMM_RAIN_PERIOD_KERNEL_SIGMA 0.15): 28 Napa seasons never saw 30",
  which is not 0%. Leave-one-out over those seasons, strikes 10-35: Brier
  0.1123 raw, 0.1115 at sigma 0.15 (0.27 was worse).
- rain counts only inside the period: before its first day an event reads
  dry with no observation, so the season quotes through October's storms
  and stands aside in Napa rain from Nov 1. (The monthlies still stand
  aside on rain at the station before their month starts, e.g. a November
  event in an October storm -- unchanged.)

THE GATE (rain_period_gate). The monthly checks at IMM_RAIN_PERIOD_TOL_CENTS
(10): no fresh fair / no observation / raining / drying 60 min / stale to
date / within 0.05" of the to-date -> stand aside both sides. A touch that
fights the fair CAPS that side at fair +- 10c instead (IMM_RAIN_PERIOD_CAP,
the Carbon Arc rule: "the bot should sit on the bid if it's earning money
on it, but not if it wouldnt earn"); the capped side rests only while the
scored walk reaches it (drop_unearning_sides), the other side joins the
touch. Logs "rain-period stand-aside / resume / capped / uncapped / not
earning <t>", guard "rain_period" (risk table: gate guards).

10/4 22:10Z, season fair vs book (YES bid x ask, depth at the touch):
T10 78c vs 28x73 (1,739 / 45), T15 51 vs 28x74, T20 21 vs 26x74, T25 9 vs
26x74 -> bid capped at 19 behind 1,099 (does not earn, ask only), T30 3 vs
12x60 (inside the tolerance), T35 1 vs 12x59 -> bid capped at 10 behind 2
(earns). October (in period, 0.00" through 10/3, 7 forecast days): T1 30c,
T2 15, T3 10, T6 3. The touches are other makers' 1,000-3,000 lots; the
bot's own sizing decides its share.

ALSO FIXED: parse_event_date reads a KXRAIN period ticker's FIRST day. The
YYMONDD reading made KXRAINSNYC-03OCT26-04OCT26 start 2003-10-26 (past), so
every rainstorm span not starting on the 26th stood down unquoted; spans
now stop at 00:00 ET on their real start day, as Jack asked on 9/24. The
spans themselves stay blind (no fair): a 2-day storm window is a forecast
bet the model's 30% climatology mix would price dry.

KILL SWITCHES: IMM_RAIN_PERIOD_ENABLE=0 (the family leaves the allowlist;
positions ride), IMM_RAIN_PERIOD_CAP=0 (a breach parks both sides);
IMM_RAIN_PERIOD_SERIES (writer's default list, KXRAINNAPAM,KXRAINNYCW).
Env => restart_imm.ps1 -Task.

WATCH: the first refresh after the restart re-reads Kalshi's events at once
(the old file has no events_series) and logs "rain-monthly refresh: N
events"; the season's six markets then read "rain-period capped" (T25,
T35) or quote. A month's TWC total vs IEM: unverified until the October
event settles (the boundary rule covers rounding).

Tests: TestRainPeriodGate (guards, cutoff, span start day, every gate
reason and the caps, quote / cap-not-earning / rain end to end, a capped
bid that earns), TestAllowlist.test_rain_period_family_allowed_by_shape,
test_rain_monthly_fair.py (+11: period rules and station codes, events,
segments across the new year and Feb 29, to-date incl. ACIS fill and
the stale rules, the kernel, forecast injection, the writer's period
weather rule and event re-read).

## 2026-10-04 (late) — fills rows keep their ledger join (spun off from "Build these 3")

WHY. Every day since 9/6, 29-48% of maker rows in fills_*.jsonl had no
ledger join (our_book_side, our_price_cents, our_remaining_before,
client_order_id, order_age_secs, is_pad all null), and they were mostly the
full-rung fills. _log_fill read state.ledger when the fills poll booked the
fill, and the ledger had already let the order go. A replay of 10/4's sinks
through ~22:00Z (231 unjoined of 482 maker rows) splits them by path:
- 87: _merge_ledger. The order filled out after a cycle's fills poll, the
  same cycle's resting read (after the universe refresh, often a minute or
  more later) no longer held it, and the entry was dropped. The next poll
  booked the fill.
- 26: a cancel of the order got 404/409 (it had filled out). An amend
  answered 404/409 drops it the same way and leaves no orders row.
- 23: a partial fill, then our own cancel (requote, renewal, stray) pulled
  the rest before the poll.
- 93: orders an earlier run placed (22 runs that day). 51 were adopted and
  then dropped in the ways above. 42 filled in the restart gap: the old
  run's last poll missed them and the new run's ledger never held them.
An amend also overwrote what was left: 37 of 489 joined rows on 10/3-10/4
carried a price and size set AFTER the fill (KXTRUMPAPPROVE-26OCT04-E38.1:
79.11 filled at 35, then amended to 40 x 120, then logged at 40 / 120).

WHAT (analytics only; the ledger and every trading path are unchanged).
- _fill_join keeps each ledger write (place_order, amend_order_inplace,
  _adopt_restart_handoff) as a version of the order:
  [set at, YES cents, exact cents, remaining as set, source]. An order is
  forgotten FILL_JOIN_KEEP_SECS (IMM_FILL_JOIN_KEEP_SECS, 1800) after the
  first prune that finds it out of the ledger. Pruning runs once a cycle,
  after the fills poll is booked; FILL_JOIN_MAX_ORDERS (60,000) caps it.
  10/4 placed ~5,200 orders an hour, so expect ~10-15k orders held.
- _log_fill takes the version in force at Kalshi's fill stamp. Kalshi
  stamps whole seconds, so the fill's price (a maker fill trades at our
  resting price) settles an amend inside the stamp's second (+2s).
- A poll's batch is registered on its orders before any row is written
  (Kalshi returns it newest first: 455 of 455 multi-stamp batches 10/1-10/4),
  so each row knows the order's earlier fills.
- The restart handoff carries the join (restart_handoff.json "fill_join").
  The relaunch loads it whatever becomes of the book, so a gap fill joins
  the old run's version.
- The row keeps every column and gains two: `join_src` (ledger / handoff /
  derived) and `our_left_before` (what was left of the order just before
  this fill; count >= it = the fill took the rung). With no record of the
  order (a crash restart, a taker order), our_book_side comes from Kalshi's
  side/action and the row is marked derived. IMM_LOGGING.md "### fills" has
  the semantics and the caveats for rows written before this.

Replayed through the bot on the base commit vs this branch: a merge drop,
a cancel 404 and a partial-then-cancel logged (None, None, None) before and
(side, price, size) now. The amend case logged 40 / 120 before and 35 / 90
now. On production data (versions rebuilt from 10/2-10/4 orders rows, the
branch's own _join_pick / _join_left): all 888 maker fills of 10/3-10/4
pick a version resting at the fill's price, every old join reproduces
exactly except the 37 amend cases, and our_left_before equals the WS sweep
breaker's rem_before on all 25 trips.

NOT CHANGED, for Jack's call:
- _toxic_note_fill still reads state.ledger to skip 1c/99c pads. A pad fill
  whose entry was already dropped counts toward the toxic side halt. That
  happened 5 times 9/6-10/4 (24 pad fills were joined). Pointing it at
  _fill_join would change trading.
- A restart that hands nothing over (crash, hard kill, halt exit) still
  starts empty. Its gap fills log derived.
- ws_stale_score.is_full reads our_remaining_before <= count. It can switch
  to our_left_before when the row has it. Rows before this carry no such
  key, so the 10/5 scoring of 10/4 is unaffected either way.

DEPLOY. A plain code deploy (the code-change exit hands the book over). The
first restart onto this code reads a handoff file without "fill_join": that
one gap logs as before for orders that filled out, and adopted orders' gap
fills keep side / price / client id with a null size. Every later restart
hands the join over. Startup line: "startup: fill join for N order(s)
handed over by run <id>".

WATCH. In fills_*.jsonl after the deploy, maker rows with join_src derived
should be ~0 outside the first restart gap. our_book_side null should be 0.

Kill: IMM_ANALYTICS=0 (it already turns off the fills sink). Tests:
TestFillLedgerJoin (12).

## 2026-10-04 (late) -- event sweep breaker ON: whole event, both sides, 2 min, 20% control (Jack: "do Whole event, both sides, 2 min. but monitor to make sure it's effective and net positive")

WHY THIS SETTING. Jack found the 5-min whole-event pull punitive. Scope
backtest 9/6-10/4 (corrected sides; per day at 5m / 30m mark-outs):
- whole event, both sides: 2 min +$21/+$30 (reward ~$4); 5 min +$25/+$40 (~$9)
- hit side only, 2 min: +$17/+$26 (~$2)
- linked group (same player/word/city, or the whole ladder), 5 min: +$18/+$22
- swept market alone: +$9/+$8
About 60% of the whole-event rule's avoided loss is in the event's OTHER
players / words / cities: one game, one speech, one weather system.

LIVE PATH. A trip:
- cancels the event's resting orders (view + any placed since the last
  rebuild, from the ledger), on the fast-cancel budget;
- sets _sweep_hold[event];
- skips every placement / amend into the event for the rest of that
  cycle (a swap's old order is still cancelled);
- leaves `desired` without the event's quotes until the hold ends.
_sweep_until is only the trip window (no second trip inside it, any
mode).

MONITORING. IMM_SWEEP_HOLDOUT=0.2: a random 20% of trips are logged
mode "control" and pull nothing. ws_stale_score.py then nets the live
breaker (net_live):
  avoided per control trip x live trips
  - fills that still landed inside live holds
  - reward the live holds gave up
It flags the estimate as noisy until ~30 control trips have fills. A daily
scheduled task (imm-sweep-breaker-check) reports it for a week. Status
latency.sweep: trips, trips_live, trips_control, cancels, skipped_budget,
skipped_writes, held_now, holdout.

BACK OUT: IMM_SWEEP_BREAKER=dry in run_incentive_mm.ps1, then
restart_imm.ps1 -Task.

## 2026-10-04 night — Monthly snow (KX<CITY>SNOWM) quoted only through a snow fair (Jack)

Jack: "build the snow feed" (after the period-rain entry above named the
snow monthlies as uncovered: KXCHISNOWM $2,003/day, KXDETSNOWM $148/day in
the 10/2 new-programs email).

THE MARKETS. KX<CITY>SNOWM-<YYMON>-<K>, "total snowfall in Chicago (O'Hare)
in December 2026 strictly greater than 8 inches", contract SNOWOVERTIME,
TWC first in the source hierarchy. TWC's Kalshi dashboard, Snowfall tab:
"Official daily snowfall from the NWS climate report, for the city's own
local calendar day" -- the CLI's daily snowfall, summed. The rules name the
city only, so the station is the dashboard's: BOS, ORD (the rules say
O'Hare), DCA, DEN, DTW, MKE, MSP, NYC (Central Park), PHL, PIT. The 10/1-10/3
programs feed carried these ten, all on 26DEC (strikes 2-12, MKE/MSP 4-24),
programs ended 10/3 23:59 ET; none active on 10/4. Not covered: Big Sky
(KXTSNOWFALLBIGSKYM, OnTheSnow's resort report), KXDENSNOWMB (NWS NOWData
line), the old KXSNOW* lines.

FAMILY. The ten in ALLOW_SERIES and any other KX<CITY>SNOWM by name
(snow_monthly_series, IMM_SNOW_MONTHLY_SERIES_RE), ONLY while the gate is
on; a city the writer cannot map stands aside. Archetype KXCHISNOWM: band
5-90c, cutoff close - 1560 min (22:00 local the day before the month's last
day). is_daily_series counts them with the rain monthlies: x1 at every hour,
no Saturday or yield size.

THE FAIR (snow_monthly_fair.py -> snow_monthly_fair.json, refresher thread
"snow-monthly" every IMM_SNOW_MONTHLY_REFRESH_SECS 600; the rain gate's
loader and checks, weather_fair_gate_reason):
- to date: the latest CLI's snow_month (IEM json/cli.py; T counts 0, as
  the dashboard's totals do) and the report's issue time from its product id.
- ASOS measures no snowfall, so the hours after a report are unknown: snow
  seen at the station after it was issued makes the event STALE ("snow
  since the last climate report") until a later report counts it; a CLI
  over 40h old is stale too.
- the rest: rain_monthly_fair.simulate_period on ACIS daily SNOWFALL since
  1980 (Denver from the threaded DENthr record -- DIA measures snow only
  since 2006), NWS grid snowfallAmount injected over the horizon (new
  `element` parameter in rain_monthly.fetch_forecast_days), log-normal kernel
  sigma 0.25. Leave-one-out over 1980-2025 Decembers at the ten stations,
  2,688 predictions: Brier 0.1827 raw, 0.1820 at 0.25 (coin: 0.25).
- snowing = SN/SG/PL/GS/UP in the present weather, or precipitation at 35F
  or colder; stand aside while it snows and SNOW_MONTHLY_DRY_MIN (60) after.
  A month that has not started reads dry (no observation needed).
- strikes within 0.1" of the to-date stand aside.
- a touch that fights the fair by more than SNOW_MONTHLY_TOL_CENTS (10)
  caps that side, resting only while it earns (SNOW_MONTHLY_CAP, the
  period-rain rule); the other side joins the touch.

10/4 23:00Z, December fair vs book (no touch breaches): CHI 2" 71 vs 60x99,
6" 41 vs 31x99, 12" 20 vs 12x86; MSP 8" 59 vs 48x99, 24" 8 vs 4x5; NYC 2"
50 vs 40x67; DC 2" 22 vs 14x85; DEN 12" 26 vs 1x21.

KILL SWITCHES: IMM_SNOW_MONTHLY_ENABLE=0; IMM_SNOW_MONTHLY_CAP=0 (a breach
parks both sides). Env => restart_imm.ps1 -Task.

WATCH: first refresh logs "snow-monthly refresh: 10 events with a fair"
(first write ~50s: ten ACIS downloads, cached a day). When programs return,
"snow-monthly stand-aside / capped / resume <t>". In December, how often the
"snow since the last climate report" rule holds a market out.

Tests: TestSnowMonthlyGate (allowlist + pattern + other snow contracts out,
guards, cutoff, every reason incl. snowing / drying / the stale reason / caps,
end to end), test_snow_monthly_fair.py (13), dashboard family "Snow
monthlies".

### 2026-10-04 night — Snow monthlies and the period-rain family at x2 size (Jack)

Jack: "2x snow markets, and rain ones like napa". After the 23:09Z restart
the Napa season's books were 5-13k contracts deep at the touch and only T15
($2.02) and T20 ($1.81) projected over the $1.50 payout floor; T10/T25/T30/
T35 read $0.94-1.26 and sat out. RAIN_PERIOD_SIZE_MULT (KXRAINNAPAM,
KXRAINNYCW and every cloned period series) and SNOW_MONTHLY_SIZE_MULT (the
ten KX<CITY>SNOWM and clones), both 2.0, on the size_mult wire: rungs x2,
per-market cap 150 -> 300 net, per-event cap 1,000 -> 2,000, and the floor
projection sees the doubled ladder. Still "daily" (no quiet-hours, Saturday
or yield size); the KXRAIN 7pm-01:59 ET halving still applies to the rain
family (evenings x1). The KXRAIN<CITY>M monthlies are unchanged (x1).
Revert: IMM_RAIN_PERIOD_SIZE_MULT=1.0 / IMM_SNOW_MONTHLY_SIZE_MULT=1.0
(env => restart_imm.ps1 -Task).

### 2026-10-04 20:35 ET (00:35Z 10/5) — the period-rain family keeps x2 in the evenings (Jack)

Jack: "KXRAINNAPAM-01NOV26-31MAR27 isnt at 2x". The x2 was on (T15 10/30 ->
20/60 at 23:37Z), but IMM_SERIES_HOUR_MULT's KXRAIN:19-1:0.5 matched
KXRAINNAPAM by prefix, so from 19:00 to 01:59 ET the season rested at x1
(20 a side). That halving is the rain DAILIES' (the evening before the rain
day is informed by it); a November-March total has no such evening.
_hour_window_mult now skips the per-series windows for rain_period_member
series (KXRAINNAPAM's override object: KXRAINNYCW and every clone): x2 at
every hour. KXRAIN dailies, KXRAINWKND and the KXRAIN<CITY>M monthlies keep
the halving; the snow monthlies never matched an hour window.

## 2026-10-05 — Vercel fair: a weekday term for open-weights, mean reversion for Moonshot, D up to today+4 (Jack)

Jack: "yes IMM_VERCEL_HORIZON_DAYS=4. fix open-weight shares and Moonshot."
(after the 10/05 Vercel check-in, KL-data/vercel-logger/analysis-2026-10-05/
verdict.md: the gate holds only ~4% of pre-D program hours, the payout-floor
projection 48% and 1c x 99c placeholder books 35%. The running fair ran ~9 pp
LOW on KXOPENSOURCESHARE for a weekend D and ~6 pp low on KXMOONVSPEND after
its 10/01 drop, Brier vs the book +0.066 and +0.170.)

WHAT CHANGED (vercel_fair.py only; incentive_mm.py and the launcher untouched):
- HORIZON_DAYS default 3 -> 4. Programs post ~3.3 days before D; at 3 the
  D = 10/8 set (posted 10/04 16:02Z) sat on "no Vercel read" until 00:00Z
  (424 of the 608 stand-aside lines that day).
- DOW_SERIES (default KXOPENSOURCESHARE): the change sample is taken from
  the series less its weekday effect (each day's deviation from its centred
  7-day mean over SEASON_DAYS 90, averaged by weekday; open-weights runs
  ~+4 pp Sat/Sun, -1 to -2.6 Mon-Thu) and re-centred by effect(D) -
  effect(anchor day). A Friday running share now prices a Sunday D ~+6 pp.
- MR_SERIES (default KXMOONVSPEND): the k-day change is regressed on the
  start day's deviation from its trailing MR_TRAIL_DAYS (7) mean, slope
  clipped to [-1, 0] (Moonshot ~-0.3 at k=1, ~-0.6 at k=3); the residuals
  are the sample, re-centred by slope x the anchor's deviation. After a dip
  the fair now prices the rebound.
- SQRT_SERIES (default KXMOONVSPEND): changes in sqrt(share), so a share
  near zero moves in proportion to its level. It ran 0.6-20.7% in 90 days;
  the additive model put its 5th percentile at -10 to -13% at a 5% level.
- OPEN_WIDEN 1.0 -> 1.5: at 1.0 the open-weights 5-95% band missed 16-17% of
  May-Oct outcomes (8% at 1.5).
- The history read covers max(HISTORY_DAYS, SEASON_DAYS) + 10 = 100 days
  (~2 MB, once per HISTORY_REFRESH_SECS / UTC day).
- Entries of a changed series carry "adj": {space, dow, mr_slope}; the
  file's "model" block lists the new knobs. The IMM reads x_l + errs as
  before (errs are X_D - x_l), so the gate code is unchanged.

EVIDENCE (walk-forward on Vercel's export 2025-10-01 -> 2026-10-04, every
model built only from the 60 days of finals before its anchor; scripts and
outputs in Documents/KL-data/vercel-logger/fairfix-2026-10-05/). Brier of
P(X_D >= K) on a strike grid around the anchor, anchor = the day's final,
k = 1..3, Jan-Apr / May-Jul / Aug-Oct:
  open-weights  0.0797 / 0.1015 / 0.1272 -> 0.0816 / 0.0851 / 0.0982
                wrong "decided" calls 1.5 / 2.3 / 4.0% -> 0.8 / 0.8 / 1.6%
  Moonshot      0.1622 / 0.1028 / 0.1558 -> 0.1610 / 0.0986 / 0.1471
                wrong "decided" calls 11.7 / 6.4 / 5.0% -> 6.3 / 5.6 / 4.5%
CRPS (k = 1..4, Jun-Oct): open-weights 3.74 -> 3.16 (the weekday term),
Moonshot 1.71 -> 1.58 (log-space MR; the shipped sqrt-space MR was chosen on
the strike Brier: a log space put Moonshot's 95th percentile at 50-88%).
crosscheck.py: the shipped code reproduces the backtest's percentiles to
0.0001 pp. Not adopted: a weekday term on Moonshot (worse in every period),
mean reversion on open-weights (no better than the weekday term alone).
The other six series are unchanged; the same backtest suggests the weekday
term would also help KXGOOGVREQ / KXDEEPVREQ / KXANTHVREQ (CRPS -9 to -28%
Jun-Oct) -- not shipped, Jack's call.

DRY RUN 10/05 15:00Z (run anchor, 7 calibration days): 32 entries (8 series
x 4). KXMOONVSPEND (running 5.4%): old p5/50/95 -10.1 / 4.5 / 14.4 -> new
0.1 / 5.9 / 18.6 for D = 10/6, and median 7.5 for D = 10/8 (the old fair
called KXMOONVSPEND-08OCT26 T5P3 45c against a 73x85 book; the new one
~60c). KXOPENSOURCESHARE (Monday, weekday D's): medians within ~2 pp of the
old, bands ~1.5x wider.

DEPLOY: vercel_fair.py is imported by the IMM's "vercel-fair" refresher
thread, and the code-change exit watches only incentive_mm.py, so the sync
alone does not reload it: restart_imm.ps1 (plain mode; no env change).

KILL SWITCHES (env => restart_imm.ps1 -Task): IMM_VERCEL_DOW_SERIES="",
IMM_VERCEL_MR_SERIES="", IMM_VERCEL_SQRT_SERIES="" restore the old model per
term; IMM_VERCEL_OPEN_WIDEN=1.0; IMM_VERCEL_HORIZON_DAYS=3.

WATCH: "vercel-fair refresh: 32 events with a read, anchor run" (was 24);
vercel_fair.json entries for KXMOONVSPEND / KXOPENSOURCESHARE carry "adj";
the next weekend D for open-weights (D = Sat 10/10 / Sun 10/11 events, if
listed) should no longer stand aside as "decided" off a weekday running
share. Re-score fair vs book on the settled D = 10/6 and 10/8 events.

Tests: test_vercel_fair.py 17 -> 24 (defaults, the weekday profile, a
Friday anchor pricing a Sunday D on both anchors, the Moonshot rebound in
sqrt space on both anchors with no mass below zero, plain series
unchanged, the model block + "adj", the 100-day history read);
test_incentive_mm.TestVercelPreDGate unchanged and green.

## 2026-10-05 — YouTube #2 top-video PILOT: KXYTTOPVIDEOG2D / KXYTTOPVIDEO2D quoted plain at 20 lots to the end of the chart day, 100/event, $40/day family halt (Jack)

Jack, on the 10/05 YouTube check-in: "why not softlaunch the viable youtube
markets yet?", then (after the proposed spec) "go ship the youtube #2 pilot".

WHY THESE TWO, AND WITHOUT A FAIR. Check-in: KL-data/youtube-analysis-
2026-10-05/verdict.md (markouts_out.txt, fill_cost_out.txt, reward_out.txt).
Each event asks which video ranks #2 on the YouTube Charts daily top music
videos chart (global / US) dated the ticker's UTC day D: 15 videos, listed
~18:00Z on D-1, closing 03:59Z D+1, $84.85 per market-day. MEASURED on the
public tape (complete events, in band): makers +1.3 c/ct global (20 events,
69.5k ct, CI -8.9/+12.6) and +13.4 c/ct US (21 events, 73k ct, CI -4.3/
+31.5); +31 / +28 c/ct on the 3 events since 10/01. #2 stays a toss-up for
the API watchers (a leading contender's chart/API ratio swings 0.96-1.35,
the model called #2 on 2 of 5 days), unlike #1 (called 5/5; makers -27 c/ct
after the chart day). MODELLED at 20 lots (the bot's own scorer x0.7 + fills
x measured markouts, two fill models): +$6-15/day global, +$5-20/day US.
Not piloted: KXYTVIEWSW (makers -5.1 c/ct, every complete week negative),
KXYTVIEWSHIGH, #1 global (one market in band), KXYTVIEWSD.

THE RULES (code: the YT2_* block after the Vercel gate):
- Allowlist: IMM_ALLOW_YT2_SERIES (default the two) while IMM_YT2_ENABLE=1.
  Normal book otherwise: selection, payout floor, pads, the toxic-flow side
  halt and the sweep breaker apply as for any series. Joins the touch (no
  safe_join).
- Cutoff: 00:00Z on D+1 (the end of the UTC chart day) less
  IMM_YT2_CUTOFF_BEFORE_DAY_END_MIN (0). cutoff_from_close_min=0 takes out
  the ticker-date midnight-ET rule (it would have stopped at 04:00Z on D);
  the tightener in apply_series_cutoff_adjustments sets the real cutoff;
  an unparseable ticker is stood down (fail closed). After the chart day
  the API views are complete; the final hour before the close cost makers
  30 c/ct on US #2.
- Size: x1 at every hour -- the global ladder (IMM_LEVELS 0:20 = one 20-lot
  rung at the touch), is_daily_series (no quiet-hours / evening / Saturday
  size), KXYT already out of the yield mode, and a hand-set per-market cap
  (YT2_MAX_POSITION 100) that also opts out of every family multiplier.
- YT2_EVENT_CAP 100 net per event (event_cap_contracts, the share family's
  wire).
- FAMILY DAILY LOSS HALT: the pilot's realized + marked P&L today (5am-CT
  roll day; baseline / carry across restarts exactly like the scan tier's
  loss budget) at or below -IMM_YT2_DAILY_LOSS_LIMIT ($40) cancels the
  family's orders, emails "yt2_halt", and the quote loop stands both series
  aside (guard "yt2_halt") until the roll. Positions ride to settlement.
  State: yt2_halt_day + yt2_pnl_carry persisted.

STARTUP LINE: "yt2 pilot: KXYTTOPVIDEO2D,KXYTTOPVIDEOG2D quoted plain to
00:00Z after the chart day, x1 at every hour, 100/market and 100/event net,
family halt at -$40 P&L today".

KILL SWITCH: IMM_YT2_ENABLE=0 (or IMM_ALLOW_YT2_SERIES="") takes both out of
the allowlist; positions ride. Env => restart_imm.ps1 -Task.

REVIEW: the 10/15 YouTube re-score task (scheduled-tasks youtube-markets-
rescore-oct15) checks whether this shipped and scores it from fills_*.jsonl
(our markouts at settlement, rewards, net/day vs the modelled +$11-34) ->
KEEP / CHANGE / STOP.

WATCH: the 26OCT06 events list ~18:00Z 10/05 and quote to 00:00Z 10/07.
Contended strikes (two-sided, mid 5-95) should rest 20 a side at the touch;
decided ones sit out on extreme_mid. guard_skips "yt2_halt" only after a
trip. Unlisted winners (all listed markets NO) hit 3 of 25 #2 events since
9/9 -- the 100/event cap is the bound.

Tests: TestYouTube2Pilot (enrolment, x1 at quiet / evening / Saturday
hours, 100/100 caps, the rest of the YouTube catalog stays out under
ALLOWLIST_ONLY, the end-of-chart-day cutoff incl. the knob and fail-closed,
quotes end to end, the loss halt: inside / past the limit, alert, stays
aside, persists across a restart, resumes at the roll).

### 2026-10-05 16:10Z — the YouTube #2 pilot rests exactly 20 lots: no deep-reference size (YT2_REF_MULT_CAP 1.0)

First live cycle (16:01Z, KXYTTOPVIDEOG2D-26OCT05): bids 20 at the touch,
but asks 50 on XAM (55c, 6 ticks behind a 49 touch) and 30 on CAS (41c, 2
behind 39) -- the atref deep-reference multiplier (+0.25 per tick behind,
to 3x), not the 20 lots the pilot was approved at. capped_ref_mult now caps
the pilot's multiplier at IMM_YT2_REF_MULT_CAP (1.0), the same lever the open
scan used (SCAN_REF_MULT_CAP): the rung still rests at the reference level
(full reward weight, fills only after the band above is eaten), 20 lots
however deep. Every sizing site (ladder total, estimator meta, live side
rooms) reads capped_ref_mult with the series. Test: TestYouTube2Pilot
(capped_ref_mult 1.0 for both pilot series, > 1 for a plain series).

### 2026-10-06 00:45Z — the YouTube #2 pilot quotes to one hour before the close (Jack: "move the cutoff to 1 hour before close")

Jack was watching KXYTTOPVIDEOG2D-26OCT05 stop at 00:00Z (the end of its
UTC chart day; the market trades to 03:59Z). The cutoff is now the close
anchor: cutoff_from_close_min = IMM_YT2_CUTOFF_FROM_CLOSE_MIN (60) -> 02:59Z
on D+1 (22:59 ET on D). The end-of-chart-day rule stays behind
IMM_YT2_CHART_DAY_CUTOFF=1 (with IMM_YT2_CUTOFF_BEFORE_DAY_END_MIN); an
unparseable ticker is still stood down. Tape for the added window (D+1
00-03Z, complete events, in band, makers): global +1.1 c/ct (16 events,
5.7k ct, CI -7.7/+14.0), US +9.5 (11 events, 6.6k ct, CI -10/+64) -- the
final hour before the close (-5.7 global, -30.4 US) stays out. Side effect at
deploy: the 26OCT05 events (close 03:59Z 10/06) are quotable again to 02:59Z
if they re-clear the payout floor on what is left of the window.
Startup line: "yt2 pilot: ... quoted plain to 60m before the close, ...".
Test: TestYouTube2Pilot.test_cutoff_is_one_hour_before_the_close.

## 2026-10-05 (late) — OpenRouter share fair: week-to-date anchor pulled toward the last 6h, breaks in the mix, revisions stand aside, snapshot + ladder archive (Jack: "yes")

WHY. The week of 9/28 settled: the share family lost -$155 trading against
$19 of rewards. The loss sat on strikes priced 25-75c (-$172 vs $11 of
rewards across the family since 10/01). The worst stretch was Saturday:
-$112, mostly OpenAI 18.1 shorts that settled YES at 18.20, 0.05pp past
the rounding edge. The fair agreed with those fills, so the fair was wrong.

EVIDENCE (scratch backtest; week of 9/28, 4 authors, hourly):
- Data: exact `chart_hist` from Sun 01Z. Earlier hours rebuilt from the
  preds' wtd_share on a Mon/Sun hourly volume profile, within 0.1-0.7pp of
  the exact 3h shares logged on Saturday.
- RMS error of mu against the settled week, by run rate:

  | run rate | Fri (2-3d left) | Sat (1-2d) | Sun (<1d) | all |
  |---|---|---|---|---|
  | last 3h (live since 10/03) | 0.53 | 0.57 | 0.23 | 0.47 |
  | week-to-date | 0.58 | 0.27 | 0.18 | 0.39 |
  | WTD + 0.3 x (last 6h - WTD) | 0.51 | 0.25 | 0.19 | 0.35 |

- Pulls scheduled by days left (r/3, r/4, r/5) scored 0.33-0.35: noise.
- The last hours chase the time of day. DeepSeek runs ~5pp higher at
  18-21Z than at 00-05Z on both days seen. Google's 3h share went 26% ->
  17% in 18 hours over the weekend; its week settled at 21.5%.
- With that run rate, sigma = vol x r/7 (no 3/6/24h spread term) scores
  RMS z 0.77-1.06.
- The model's remaining-volume weight runs 6-19% high on weekends (Sun
  ~34.5M req/h vs Mon ~43.5M). Its effect on mu is about 0.01pp; not
  changed here.
- Breaks: z-ai went 6.6% -> 21% at the 10/05 week turn, and the stealth
  model's ~10% stopped at 16:04Z. A plain WTD anchor then runs low on every
  named author (OpenAI 16.7 vs ~18.4 flow).

WHAT (openrouter_share_fair):
- Run rate. A named author's share is A + SHARE_RUN_PULL (0.3) x (L - A):
  - L is the chart's last SHARE_RUN_HOURS, now 6 (was 3).
  - A is the week-to-date, or the flow since the week's last break.
  - Until SHARE_BREAK_MIN_HOURS (3) of flow sit behind a break, the last
    hours alone.
  - The blend stays the fallback, and R keeps the blend.
- spread is 0 on the chart's rate. After a break it is half the gap
  between the anchor and the week-to-date.
- detect_break. Fires when a named bucket's last SHARE_BREAK_WINDOW_HOURS
  (3) differ from its share since the anchor by >= SHARE_BREAK_ABS_PP (3)
  and >= SHARE_BREAK_REL (0.5) of the larger share. The anchor must hold
  >= SHARE_BREAK_MIN_BASE_HOURS (6) before the window.
  - Google's Saturday 21.7% -> 26% is 17%: not a break.
  - It is placed at the least-squares change point over the snapshots.
  - Persisted in the fair file as `breaks` {week: {t, ys, bucket, old,
    new}}, so it outlives the 48h of snapshots. Dropped at the week's turn.
- chart_revision. Within SHARE_REVISION_HOLD_HOURS (6), any of these:
  - a named count falls;
  - an author with >= 1% of the week vanishes and Others does not take it
    in (named-set churn is fine);
  - one interval's increase goes >= SHARE_JUMP_FRAC (0.6) to a single
    author: the stealth model's requests re-attributed to its maker.

  Effect: data_current False and entries lag, so the gate stands aside. A
  break is anchored at the revision.
- Archive (no trading effect):
  - every new chart snapshot -> openrouter_share_chart_YYYY-MM-DD.jsonl
    (IMM_SHARE_ARCHIVE_DIR, "" = off);
  - every strike's touch on each hourly Kalshi event read (same call, no
    extra requests) -> openrouter_share_ladder.jsonl (IMM_SHARE_LADDER_FILE).
- New entry fields: anchor_mode ("week to date" / "since break" / "break,
  last hours"), anchor_share, break_at. run_mode reads "wtd+0.3x6h" /
  "break+0.3x6h" / "chart 6h" / "blend". The preds log carries
  anchor_share and break_at.

REPLAY on the stored 48h (516 snapshots, both weeks):
- No break and no revision across Sunday or the week's turn.
- Stealth flagged at 17:26Z, placed at 16:04Z (10.67% -> 0.02%).
- mu at 00:58Z 10/06, live -> new: Anthropic 2.50 -> 2.57, DeepSeek 22.64
  -> 23.14, Google 15.99 -> 16.35, OpenAI 17.88 -> 18.07, Z-AI 21.18 ->
  20.42.

KILL SWITCHES (env in run_incentive_mm.ps1, then restart_imm.ps1 -Task):
- IMM_SHARE_RUN_PULL=1 with IMM_SHARE_RUN_HOURS=3: the 10/03 rate, minus its spread.
- IMM_SHARE_BREAK_ABS_PP=99: no breaks.
- IMM_SHARE_REVISION_HOLD_HOURS=0: no revision hold.
- IMM_SHARE_RUN_HOURS=0: the blend.

WATCH:
- run_mode "break+0.3x6h" on the 26OCT12 events: this week's stealth
  break is re-found from the stored snapshots on the first write.
- "[share-fair] break in the mix" / "chart revision" lines.
- The openrouter_share_chart_*.jsonl files growing by ~150 rows a day.
- Score at the 10/12 settlement: preds mu vs openrouter_share_finals.jsonl.

Tests: test_openrouter_share_fair.py, 27 green. One reworked: the run
rate and vol-only sigma, and pull 1 = the plain last hours. New:
- TestBreaks: a withdrawn model at its change point and the since-break
  anchor; a fresh break on the last hours alone; no break on a 17% swing,
  at a week-turn launch, without a base, or again from the break itself;
  revisions vs named-set churn and the hold.
- TestWriterBreaks: a break kept past the 48h of snapshots and dropped
  next week; the archive once per cachedAt; a revision standing the
  family aside; the ladder on each event read only.

### 2026-10-06 ~02:25Z — share fair: the nine-name boundary swapping is not a revision

The first deploy (dc14a7c, live 02:17Z) flagged a "revision" at 01:43:47Z
and stood the family aside. What happened was the chart's ninth name
swapping: mistralai (18.48M) went out to Others and meta-llama (18.55M)
came in, so Others moved +0.38M. The vanish rule ("Others did not take it
in") misread that. The kept revision also replaced the stealth break as
the anchor.

Fix:
- An author leaving the named set is a revision only with >= 1% of the
  week AND more than 1.5x the smallest name still listed.
- The same test catches a newcomer arriving with such a count (the
  stealth model's 74.7M re-attributed to an unnamed maker).
- A kept revision the rules no longer find, while still inside the hold,
  is dropped, and the break before it is re-found from the snapshots.

On a copy of the live file, one write drops the 01:43Z revision, re-finds
stealth at 16:04Z, and data_current is True again.

Tests: the swap, the newcomer, and the self-heal (28 green; suite 2,237 OK).

## 2026-10-06 — YouTube weekly artist-views PILOT: KXYTVIEWSW for KATSEYE / Drake / The Weeknd, quoted against a realtime API fair (Jack: "live now")

Jack: "consider quoting KATSEYE, Drake and The Weeknd with realtime data",
then (shadow-first or live) "live now".

WHY THESE THREE. KXYTVIEWSW-<CODE><YYMMMDD>-<K>M = the max of the artist's
daily Global views on charts.youtube.com over the 7 UTC days ending on the
ticker date ($100 per strike per week, ~15 strikes per artist). Public tape,
3 complete weeks, in band (MEASURED): makers +4.0 c/ct on these three (9
artist-weeks, CI -2.8/+8.9) -- -20.4 c/ct in the final 2h before the close
(13k ct, takers buying YES 99.8%: the last print sniped) and +9.4 c/ct on the
59k ct before it. Every other artist but Fuerza Regida lost; Ariana -26.7
c/ct (-$5.2k). KL-data/youtube-analysis-2026-10-05/ (markouts), the
2026-10-06 check-in.

THE FAIR (yt_weekly_fair.py, new; refresher thread "ytw-fair" every
IMM_YTW_FAIR_REFRESH_SECS 600 -> run-logs/incentive-mm/yt_weekly_fair.json):
- API views: the KL-data youtube-collect artist collector's hourly snapshots
  of every tracked video (yt_artist_snapshots.jsonl; 386 / 649 / 450 videos),
  read incrementally (first pass ~6 s). Chart day D <-> API window
  [D 15:00Z, D+1 15:00Z) (the 10/05 study's most stable alignment).
- ratio = median chart/API over the last 3 days with a print (the validated
  NOWCAST3 form); prints from run-logs/incentive-mm/yt_weekly_chart.json
  (seeded 10/06 from the 10/05 study: 8/25 -> 10/02-10/03) plus every print
  Kalshi reveals (a YES-early close's expiration_value) that matches a day's
  nowcast within 12% and 18-72h after the day -- written back to the file.
  charts.youtube.com still 429s every request (10/05-10/06), so Kalshi's
  early closes are the only print feed. Ratio sd = recent CV + 1%/day of age
  (Drake / The Weeknd ratios fell ~1%/day 9/28-10/02).
- each day: print / complete (r x API, sd 2.5-4.5% by artist) / partial
  (r x API so far / the pooled intraday profile, sd 16% early -> 3.3% after
  12h) / future (a weekday-factor random walk on bootstrapped de-seasonalized
  daily log changes of the last 35 days, x1.25). Complete days > 50h old with
  no revealed print are capped at the lowest open strike (they printed below
  it). P(YES) = P(max(revealed max, every day) > K), 4,000 joint draws.
- HOLD: a strike within 1.5 ladder spacings of a running or complete-but-
  unprinted day (<= 50h after its UTC end) is flagged -- the gate stands it
  aside until the print is known (the print-snipe guard; the API knows a
  finished day ~8-23h before the chart prints it).
- fails closed: snapshots > 2.5h old -> status stale_api, no entries; no
  ratio inside 14 days, no open strike or a failed read -> no entry.

THE GATE (incentive_mm.py, ytw_gate_reason in the quote loop, guard
"ytw_fair"): stand aside (cancel) with no / a stale (YTW_FAIR_TTL_MIN 30)
entry, a missing strike, a hold, a decided strike (fair < 5c / > 95c) or a
touch fighting the fair by > YTW_FAIR_TOL_CENTS 15 on the adverse side.
Logs "ytw stand-aside <t>: <why>" / "ytw resume <t>".

ADMISSION: per TICKER (ytw_pilot_ticker in _allowed): KXYTVIEWSW events whose
code is in IMM_YTW_PILOT_ARTISTS (KAT,DRA,WEE). The series is never in
ALLOW_SERIES, so every other artist stays out.

SIZING / RISK (the #2 pilot's): 20 lots x1 at every hour (is_daily_series,
a hand-set 100 per-market cap, YTW_REF_MULT_CAP 1.0, KXYT out of the yield
mode), YTW_EVENT_CAP 100 net per event, cutoff YTW_CUTOFF_FROM_CLOSE_MIN 720
(12h before the 14:00Z close: out at 02:00Z, before the final print's snipe
window), and its own family daily loss halt YTW_DAILY_LOSS_LIMIT $40 (state
ytw_halt_day / ytw_pnl_carry, email "ytw_halt", guard "ytw_halt").

DRY RUN 10/06 12:30Z (week Oct 5-11; 10/05 partial at 20.8h): Drake's fair
sits inside the book on most strikes (13.5M 75c vs 48x87, 14M 63c vs 42x65);
The Weeknd's 17-18M run 30c+ under the book (the market prices a higher
level: band stand-asides); KATSEYE's API views fell ~25% week over week from
Sunday 10/04 (broad, every top video) while the book still prices a 22M+
weekend -- its 17M+ strikes stand aside on the band. Holds on the strikes
around 10/05's estimate (DRA 12.5-13M, KAT 12-14M, WEE 15-17M) until its
print (due ~10/06 23:30Z - 10/07).

DATA DEPENDENCY: the collectors are bare python processes (no Windows task)
running to 2026-10-18 12:00Z. After that the snapshots go stale and the gate
stands the family aside (fail closed) until a feed replaces them -- the
26OCT18 week's events trade to ~10/20.
-> SUPERSEDED the same day: the durable "KL ytw-feed" task feeds the fair
past 10/18 (see "YouTube weekly pilot: a durable API feed" below).

KILL SWITCH: IMM_YTW_ENABLE=0 (env => restart_imm.ps1 -Task); positions ride.

WATCH: "ytw pilot: KXYTVIEWSW for DRA,KAT,WEE against the realtime fair ..."
at startup; "ytw-fair refresh: N events with a fair, status ok"; the 10/05
print revealed by Kalshi early closes ~10/06 23:30Z+ -> yt_weekly_chart.json
gains 2026-10-05 and the ratio recalibrates; fills on KXYTVIEWSW only for
KAT/DRA/WEE. The 10/15 re-score task should score this too.

Tests: test_yt_weekly_fair.py (8: tickers, incremental snapshots + windows,
day kinds / monotone fair / holds, revealed print floor + day match, the
published-day cap, fail closed, the file writer); TestYouTubeWeeklyPilot (3:
admission per artist, x1 / caps / cutoff, every gate reason, quotes end to
end with a hold and the loss halt); guard-sweep count 37 -> 39. 1117 green
(test_incentive_mm, test_yt_weekly_fair, test_vercel_fair).

### 2026-10-06 12:57Z — Tate McRae (TAT) joins the weekly pilot (Jack: "yes add her")

The per-artist replay of the gate's windows (KL-data/youtube-analysis-2026-
10-06-artists/gated_proxy.py: trades > 12h before the close and away from a
pending print; 3 complete weeks, makers, MEASURED): Tate McRae +22.6 c/ct
(1.1k ct, +21 / +23 by week), KATSEYE +17.0, Drake +6.1, Fuerza Regida +1.4
(+25 / -11 / -11), YoungBoy +0.7, The Weeknd -2.9 (2.1k ct), Bad Bunny -4.9,
Morgan Wallen -6.1, Taylor Swift -11.1, Justin Bieber -12.6, Future -21.9,
Ariana -26.7, Post Malone -42.4. Across all artists the final 12h ran -12.8
c/ct and the held trades +1.7, while the gated remainder ran -6.7: for most
artists the losses are through the week, not the print snipes the gate
removes. Tate: nowcast MAE ~1.1%, ratio CV ~1%; her ladder is 0.1M-spaced
(31 strikes for 26OCT11, 2.6-2.9M contested).
Defaults: yt_weekly_fair ARTISTS += TAT:Tate McRae (SIGMA_NOW 0.025),
IMM_YTW_PILOT_ARTISTS KAT,DRA,WEE,TAT. Her chart history (8/25 -> 10/01) was
added to run-logs/incentive-mm/yt_weekly_chart.json.
Dry run 12:55Z: fair 2.6M 36c / 2.7M 19c / 2.8M 9c / 2.9M 4c vs the book
65x88 / 37x56 / 25x26 / 15x20 -- the book prices last week's level; this
Monday's API views run ~5% under last Monday for most artists (the intraday
profile checks out: 0.885 measured vs 0.885 pooled at 20.8h), so 2.6-2.8M
stand aside on the band and 2.9M quotes.

## 2026-10-06 — YouTube weekly pilot: a durable API feed ("KL ytw-feed") so the KXYTVIEWSW fair outlives the 10/18 collectors

The pilot's fair read only yt_artists.py's snapshots: a bare research process
(no task, gone on a reboot) whose --until is 2026-10-18 12:00Z, while the
26OCT18 week trades to ~10/20. Task: a durable feed before 10/17, total quota
under 10k units/day while the old collector runs, collectors untouched.

CHOICE: (a) a Windows scheduled task, not (b) a fetcher inside
yt_weekly_fair. The bot restarts several times a day and imports the fair
once per start, so (b) would need its own persisted cadence and quota ledger
anyway, and would put API stalls and quota blocks inside the trading process.
A one-pass task is a single writer, separate from the bot, and back after a
reboot by its logon trigger.

THE FEED (yt_pilot_feed.py, new; one pass, then exit):
- snapshot: videos.list part=statistics, 50 ids a call, for every tracked
  video of yt_weekly_fair.ARTISTS (1,909 videos with Tate McRae, 39 calls,
  ~10 s; a new pilot artist is picked up from ARTISTS by itself); each batch
  stamped at its request's midpoint; a hidden count writes no line (the
  collector writes 0).
- scan, every 3 h: the newest 50 uploads per channel (playlistItems); a new
  video joins its artist at once. 9 of the 22 channels (Topic channels) have
  no uploads playlist (404 playlistNotFound): marked in yt_pilot_ids.json and
  rechecked weekly, so a scan is 13 units. (The collector retries each of
  those 4x per scan.)
- video lists: KL-data/youtube-collect/yt_pilot_ids.json, seeded from the
  collector's yt_artist_ids.json, which every pass re-reads (read only) to
  merge in what the collector found.
- output: KL-data/youtube-collect/yt_pilot_snapshots.jsonl, the collector's
  line format in its own file (two processes appending to one file can tear
  lines on Windows). State yt_pilot_feed_state.json, log yt_pilot_feed.log,
  tracebacks yt_pilot_feed.stdout.log, lock yt_pilot_feed.lock, all beside it.

QUOTA (MEASURED from the logs, PT day 10/05): yt_artists 22 x 158 + 7 x 84
= 4,064; yt_collect 72 x 9 + 8 x 30 = 888; total 4,952. The feed adds about
24 x 39 + 8 x 13 = ~1,040/day, for ~6,000/day while the collectors run and
~1,040 after. It keeps its own Pacific-day ledger and hard cap,
IMM_YTW_FEED_DAILY_UNITS 2,000:
- every attempt is charged, failed ones too;
- the snapshot is budgeted before the scan;
- a pass the remaining budget can't snapshot is skipped whole.
Safeguards:
- a quotaExceeded 403 keeps what was read and blocks the feed to the next
  Pacific midnight;
- a 40-minute gap guard (IMM_YTW_FEED_MIN_GAP_MIN) stops logon + :00 or a
  manual run from snapshotting twice;
- a pass makes no request after 10 minutes.

KEY: read from ~/Downloads/youtube api key.txt and sent in the X-Goog-Api-Key
header (verified live 10/06: 1,906 of 1,909 videos), so it never sits in a
URL or in an exception's text. Every log line is scrubbed of it as well.

THE FAIR (yt_weekly_fair.py): it now reads SNAP_FILES = [the collector's
file, FEED_SNAP_FILE]; override with env IMM_YTW_SNAP_FILES (;-separated) or
IMM_YTW_FEED_SNAP_FILE.
- Each file is read incrementally and merged per video in time order; a time
  already held is skipped.
- The staleness clock is the newest snapshot across both files.
- A file that isn't there yet is skipped until it appears.
- Regression check on the real 141 MB file alone: the series is identical to
  the old loader's (3.8 s warm vs 3.2 s).
- FAIR_FILE gains "api_last_by_file" ({file: newest snapshot}), which shows
  which feed is carrying the fair.
Live check, 10/06 12:57Z: the feed (first three artists) vs the 12:49 pass, 1,482
videos, 13 tiny negative deltas (API jitter). Views over those 7 min were
+17k / +26k / +28k (Drake / The Weeknd / KATSEYE), against +194k / +330k /
+336k the hour before.

DEPLOY NOTE: the bot imports yt_weekly_fair once per start, and its
self-restart watches incentive_mm.py only. The merge therefore goes live at
the bot's next restart (several a day lately); the collector carries the fair
until then. Check: "api_last_by_file" shows up in
run-logs/incentive-mm/yt_weekly_fair.json.

THE TASK: register_ytw_feed.ps1 registers "KL ytw-feed", which runs
wscript //B run_ytw_feed_hidden.vbs -> python yt_pilot_feed.py.
- Triggers: hourly at :00 (the fair's windows turn at 15:00Z) and 2 min after
  logon.
- Interactive, like every KL task; StartWhenAvailable, IgnoreNew, 15-min
  limit.
- Exit codes (the task's Last Result): 0 ok or skipped, 1 partial or failed,
  2 no key or no video list, 3 lock held.

COLLECTORS: untouched. yt_artists.py, yt_collect.py and yt_kalshi_books.py
run to 10/18 12:00Z and exit by themselves. Ask Jack before stopping any of
them. After that the feed alone carries the fair.

WATCH:
- yt_pilot_feed.log, one line an hour: "snapshot 1906 videos (KATSEYE
  386/386, Drake 647/649, The Weeknd 449/450, Tate McRae 424/424) | ...
  units PT <day>".
- `python yt_pilot_feed.py --status` (and --dry-run, --force, --scan).
- After 10/18 12:00Z: yt_weekly_fair.json shows api_last_by_file with
  yt_pilot_snapshots.jsonl advancing and status ok, and the IMM log shows
  "ytw-fair refresh: N events with a fair, status ok".
- The file grows ~4 MB/day. That's fine for months, but the bot's startup
  read grows with it: rotate past ~300 MB.
New artist: it needs a row in yt_artist_ids.json (yt_artists.py setup;
all 16 chart artists have one) and its code in IMM_YTW_ARTISTS; the feed then
tracks it from its next pass. Each ~400-video artist adds ~9 calls (~220
units/day).

KILL: Disable-ScheduledTask -TaskName 'KL ytw-feed'. Once the collector has
stopped too, the fair goes stale_api after 2.5 h and the gate stands the
family aside.

Tests:
- test_yt_pilot_feed.py (14): batching, the fair reading the format, the key
  only in a header and never in a file, seeding and merging, a torn source
  file, the 3 h scan, no-uploads channels, the gap guard, the Pacific day,
  the cap, quotaExceeded blocking, retries, partial passes, the deadline,
  lock / dry-run / status.
- test_yt_weekly_fair.py TestTwoFeeds (3): default files, the time-ordered
  merge and a window from the feed alone, a late-appearing file.

### 2026-10-07 00:42Z — Seattle joins the monthly snow fair (Jack: "i thought snow markets would automatially quote. why isnt KXSEASNOWM-26DEC?")

KXSEASNOWM ("total snowfall in Seattle in <month>") was admitted by the
KX<CITY>SNOWM pattern and SELECTED (19:42Z 10/06), but snow_monthly_fair had
no station for "Seattle", so its event sat in the fair file's "missing" list
with code null and every market stood aside ("no monthly snow fair for this
market") -- the designed fail-closed path for a new city. Added SEA to
SNOW_STATIONS (Seattle-Tacoma Intl, WA_ASOS, US/Pacific; checked live: IEM
currents, the KSEA CLI, ACIS snowfall back to 1980) and "Seattle" to
CITY_CODES. Dry run: P(December total > 2/4/6/8/10/12 in) = 20 / 11 / 7 / 5 /
3 / 2% from 38 Decembers. snow_monthly_fair is imported by the IMM's refresher
thread: deploy = restart_imm.ps1. Test lists gain Seattle.
## 2026-10-07 ~00:45Z — KXCRITICSCOMEDY (Critics Choice, Best Comedy Series) quoted under the awards rules (Jack: "KXCRITICSCOMEDY series should be quoting")

WHY. 25 KXCRITICS* series (the 32nd Critics Choice Awards, one per
category, all -27 events) got programs between 10/06 19:06Z and 10/07
00:02Z. None was on the allowlist, and the open scan is off, so the bot
never saw them: no cycle-log, guard-skip or selection rows.

WHAT. KXCRITICSCOMEDY joins the award-show set (the 9/25-9/26 rules):
- exact allow in _DEFAULT_ENTERTAINMENT_SERIES (KXCRITICSCOMEDYACTO,
  Best Actor in a Comedy Series, is a different series and stays out);
- `=KXCRITICSCOMEDY:3` in EVENT_TOP_N;
- AWARDS_SERIES + AWARDS_TABLE_ONLY_SERIES (safe-join, out 31 days before
  the nominations, table date only);
- table row KXCRITICSCOMEDY-27=2026-12-04: nominations Fri Dec 4 2026,
  film and TV together, per the Critics Choice Association's Apr 20
  announcement (Awards Radar; @CriticsChoice). Ceremony Jan 3 2027.

So it quotes from the deploy to Nov 3 2026 00:00 ET. A -28 event has no row
and stands down (fail closed).

NOT DONE: the other 24 KXCRITICS* series (Picture, Director, the acting
categories, Drama Series ...) are still unlisted -- Jack's call. The film
craft categories (Cinematography, Editing, Production Design, Costume, Hair
& Makeup, VFX, Score) would need November's below-the-line shortlist date
as their row, the Oscars' shortlist pattern.

Test: TestScreen.test_critics_choice_comedy_series_quotes_under_the_awards_rules
(suite 2,266 OK).

### 2026-10-07 ~01:10Z — every Critics Choice series, as a prefix family (Jack: "yes add all critics choice series")

Replaces the exact KXCRITICSCOMEDY entry with the Oscar shape:
- KXCRITICS in ALLOW_SERIES_PREFIXES.
- CRITICS_FAMILY_RE ((?!.*MENTION)KXCRITICS...) in FAMILY_OVERRIDE_PARENTS,
  parent "KXCRITICS" in AWARDS_SERIES and AWARDS_TABLE_ONLY_SERIES.
- `KXCRITICS:3` prefix cap. A mention book under the prefix keeps the
  mention rules: no cap, no awards override.

All 25 KXCRITICS*-27 series (18 film, 7 TV) quote at 3 per event with
safe-join, out 31 days before their first narrowing. The dates are exact
per-series rows; a category Kalshi adds later stands down until it gets one:
- CRAFTS (CINE, COST, EDIT, HAIR, PROD, SCORE, VIS) = 2026-11-16, out Oct 16.
  The 31st's below-the-line shortlists came Mon Nov 24 2025, 11 days before
  its Dec 5 nominations, and were not announced ahead (Awards Radar). The
  32nd's is unannounced, so the row is a week earlier than that spacing.
  MOVE IT when the CCA posts the date.
- Everything else (picture, director, acting, screenplays, animated, comedy
  film, foreign language, and the 7 TV series) = 2026-12-04, the
  nominations, out Nov 3.

imm_dashboard `_AWARDS_PREFIX` gains KXCRITICS (family "Awards, charts &
media").

Test: TestScreen.test_critics_choice_family_quotes_under_the_awards_rules
(replaces the comedy-only test; suite 2,266 OK).

### 2026-10-07 ~01:00Z — sports ladders / escalators at x3 every hour (Jack: "do the 3x overnight multiplier, at all times for ESCALATOR/LADDER events")

WHY. On 10/06 TB@DAL (26OCT08TBDAL), all 37 ladder/escalator markets were
admitted at listing (18:42Z). Between ~20:30Z and 21:40Z another maker
rested 160k-1.1M contracts at the touch (1,106,733 YES @ 4.16c on
ESCALATORREC-DALJFERGUSON87; 163,173 NO @ 81c on LADDERRECYDS-DALGPICKENS3,
both checked against the REST book). Our 100-lot fell to 0.04-0.1% of the
scored book, and 18 markets projected $0.37-0.95 through kickoff -- under the
$1 cliff (the family bar is $1.20 fresh / $1.00 once banked, not the global
$1.50). The hopeless exit pulled them at 21:30-22:10Z.

WHAT. SPORTS_LADDER_HOUR_MULT (env IMM_SPORTS_LADDER_HOUR_MULT, default
3.0): the hour part of hour_size_mult for every series the ladder pattern
matches is that flat value at every ET hour. Before this it was the global
window: 0-8 ET x3, otherwise x1, and never the evening x1.5. So it's 20 x 5
(family) x 3 = 300 a side all day. Saturday still multiplies on top: x2
while the gate is PASS = 600. Unchanged: the per-market / per-event caps
(750 / 5,000) and the skew knees, which ride the family multiplier only;
the near-cliff x1.5 still composes. The floor projection (size_mult_profile)
sees x3 at every hour, so the projections of the TBDAL cuts rise ~1.7x
(their window had been ~62% x1 / 38% x3). About 10 of the 18 clear $1 on
that and re-enter after the 10-min admission clock. The deepest-diluted ~6
stay out. It is applied before the daily-family and scan exclusions, so the
whole family takes it. 0 = the old behaviour.

Cash: TBDAL alone at 300 a side is ~$45 of bid and ~$255 of ask collateral
a market. Shard-0 cash was $21.5k at 00:46Z. The ladder asks' cash switch
(off under $1,500) still governs.

imm_saturday_tracker: ladder rows now sit out of the Saturday-level
hour_mult read (drop_ladder_hm, next to drop_evening_hm). Otherwise their x3 /
x6 would read a x2 Saturday as ~x4. Rent, fills and contract-hours are kept.

Tests: TestSportsLadderHourMult (7) +
test_ladder_rows_sit_out_of_the_saturday_level_read. The suite pins the knob
to 0 like the other dated knobs, so the ladder fixtures keep sizing off the
global window (repo suite 2,274 OK).

## 2026-10-07 ~01:20Z — OpenRouter: breaks on a 1h window, the family stands aside 3h after one, token run rate caps launch-day spikes (Jack: "implement all 3")

WHY (10/06, first day of the reworked share fair). OpenRouter was -$142 at
marks against ~$29 of raw rewards.

- At 10:15 ET z-ai/glm-5.3-flash's requests stepped from ~148k to ~52k/min
  in one 5-minute snapshot (everyone else flat ~620k/min). Z-AI's share went
  21% -> 8%, so every other author's rose.
- The 3h break window fired at 12:44 ET. Meanwhile the run rate (6h flow)
  was mostly pre-break traffic, and the bot sold YES on the risers: 23 share
  fills, -$107 (-$95 after 11:06).
- The Kalshi books themselves only repriced from ~11:05-11:20 ET.
- KXTOKENUSE lost -$45 on 4 overnight buys: the fair read 43-67c on
  T174-T186 where the book read 15-51c, and Tuesday printed 22.1T after
  Monday's 25.9T.

WHAT:
1. SHARE_BREAK_WINDOW_HOURS 3 -> 1. Replay on all 790 stored snapshots
   (10/04 01Z - 10/07 01Z): the same two breaks (stealth Mon 12:04 ET,
   z-ai Tue 10:15 ET) and nothing else, found 56 min / 1h38m sooner
   (z-ai at 11:06 ET).
2. SHARE_BREAK_HOLD_HOURS (3). While the chart time is within 3h of the
   week's last break, data_current is False and the entries lag, so the gate
   stands the family aside ("break in the mix at HH:MMZ (bucket old -> new);
   standing aside to HH:MMZ"). After that the since-break anchor has 3h
   behind it. On 10/06 this would have held 22 share fills (375 ct) worth
   -$95.6 at marks; the other 20 fills netted -$0.75. 0 = quote through on
   the last hours.
3. openrouter_fair OR_SPIKE_CAP (0.15). cap_spikes holds each day at most
   1.15x the median of the 7 days before it, in base, trend and weekday
   factors only. The known days of a window still count at their actual
   totals. Entries carry spike_capped.
   - Backtest 8/03-10/04, every day of every window: weekly RMSE 5.34 ->
     4.61T, 4-week 36.2 -> 29.2T. The mean error goes -1.43 -> -2.04T
     (the BIAS table was fit uncapped).
   - It would NOT have changed 10/06: Monday was +8.1% on its median, and
     a 10% cap moves that forecast only 182.6 -> 180.6. That miss was
     intraday information -- the book saw Tuesday running low -- which
     completed days cannot hold.

FOLLOW-UP for Jack (not built): an intraday nowcast of the current day's
tokens from the share chart's live request totals (the 10/06 token miss).

KILL SWITCHES:
- IMM_SHARE_BREAK_WINDOW_HOURS=3 / IMM_SHARE_BREAK_HOLD_HOURS=0 (env in
  run_incentive_mm.ps1 + restart_imm.ps1 -Task);
- IMM_OR_SPIKE_CAP=0.

Tests:
- test_openrouter_share_fair: the hold (lag from detection to 3h after the
  change point; hold 0 quotes on); the early-week no-base case resized for
  the 1h window.
- test_openrouter_fair: the spike capped in the run rate, not the known
  days; off = the old model.
- 49 green across both files.

## 2026-10-07 — Quake freeze clock anchored on the quake's origin; KXBIGGESTQUAKE x4; every rain family x2 (Jack)

QUAKE FREEZE (Jack: "why is KXBIGGESTQUAKE-07OCT26 not quoting", then "yes"
to the fix). A GFZ M5.08 (gfz2026tpui, origin 00:30:02Z 10/07) that USGS
still had not listed an hour later froze the 07OCT26 book -- the design --
but FREEZE_MAX_MIN (45) ran from when the watch FIRST SAW the detection, in
memory, and the 00:46Z and 00:56Z IMM restarts each re-saw it as new: the
freeze stretched from ~01:15Z to ~01:42Z. usgs_quake_fair: the clock now
starts at min(first seen, origin + FIRST_SEEN_MAX_LAG_MIN 20) for both USGS
and GFZ detections, so a live process is unchanged and a restart cannot hold
a freeze past origin + 65 min (NEWS_MAX_AGE_MIN 105 still bounds it too).
Test: test_a_restart_does_not_extend_the_freeze.

SIZES (Jack: "add 2x multiplier on KXBIGGESTQUAKE and rain events"; asked
which, he chose "Keep x3" for the quake and "Bring all rain to x2", then
"increase KXBIGGESTQUAKE to 4x"):
- KXBIGGESTQUAKE: QUAKE_SIZE_MULT 3.0 -> 4.0. Not a daily series, so the hour
  windows compose on top: x4 by day, x6 in the 18-21 ET evening window, x12
  in the 0-8 ET quiet hours (80 / 120 / 240 per rung at the 20-lot base),
  per-market cap 400, per-event 4,000. Bid-only YES at fair - 1c as before.
- Rain: daily KXRAIN 1.5 -> 2.0 (RAIN_DAILY_SIZE_MULT); the KXRAIN<CITY>M
  monthlies 1 -> 2 (RAIN_MONTHLY_SIZE_MULT, in the monthly gate's override);
  KXRAINWKND 1 -> 2 (RAINWKND_SIZE_MULT); the KXRAINS<CITY> rainstorm spans
  1 -> 2 (RAINSTORM_SIZE_MULT); the period family (KXRAINNAPAM) stays 2. The
  19-01 ET halving still applies to the dailies, weekend and monthlies (x1 in
  those evenings). Each knob reverts with its env var (task-level restart).
Tests: TestDailyRainSizeMult rewritten (x2 everywhere, the halving to x1,
quake x4). 1141 green (test_incentive_mm, test_usgs_quake_fair,
test_rain_monthly_fair, test_snow_monthly_fair).
