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
- STP note: with standoff, the user's crossing orders no longer meet bot quotes on
  markets he trades. On first contact (before a divergence exists) a manual taker
  order crossing a bot quote can still trigger a same-member STP cancel once —
  unavoidable without pre-reading his intent; the resting-order detection catches
  passive manual orders before any cross.

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
| Fail-safe | 4 consecutive cycle errors | cancel all resting, exponential backoff; wake-grace 120s after suspend/resume |
| Shutdown | SIGINT/SIGTERM/SIGBREAK/atexit/finally | cancel all imm- orders; startup sweeps orphans by prefix |

Dry-run is the default; `--live` is explicit. `--cancel-all` always operates on the
real book. `--status` prints the live selection table without trading (verified
2026-07-10: 29 markets, ~$975 ladder collateral, sensible universe of political/
entertainment/long-dated markets, all pre-event).

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
- Daily summary email 7:00 AM ET-ish (counters roll 6 AM ET): markets quoted,
  est. reward/day captured, realized P&L, fills, top inventory, alert counts.

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
