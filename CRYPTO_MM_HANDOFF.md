# Kalshi Crypto One-Touch Market-Maker Fleet — Handoff

*Current as of 2026-08-05. Self-contained context for a new session. Owner: Jack (jackdu224@gmail.com).*
*Repo: `C:\Users\jackd\Documents\KL` (github.com/djd224224/KL). Bot version v2.5.*

## What this is

**16 live market-making bots** on Kalshi's crypto monthly one-touch events — "How high/low will
{ASSET} get in {month}?" — for **SOL, BTC, ETH, XRP, ZEC, HYPE, DOGE, BNB × MAX and MIN**.
Live with real money since **2026-07-03**. Each bot is one Windows Scheduled Task on this
laptop running one Python process; no window is visible anywhere on screen.

Each strike is a one-touch binary (`strike_type=greater` on MAX / `less` on MIN, settled on the
CF Benchmarks trimmed-mean minute price; touched strikes settle early). Event tickers follow
`KX{ASSET}{MAX|MIN}MON-{ASSET}-{YY}{MON}{lastday}` (e.g. `KXSOLMAXMON-SOL-26AUG31`). The bots
**roll to the next month automatically** — verified live on the 8/1 rollover: all 16 moved to
the August events overnight with zero manual action.

## Current state (2026-08-05)

- **Fleet P&L +$176.42** (realized +$22.83, unrealized +$153.60). Balance **$9,519** (from
  ~$8,535 on 7/6). Net +214 contracts, $1,429 exposure, ~870 orders resting.
- Best: BTC-MAX +$97, BTC-MIN +$21, ETH-MAX +$16. Worst: HYPE-MIN −$10, ZEC-MIN −$8.
- 16/16 bots alive, **0 errors** in the last day's summaries.

## Files (all committed)

| File | Role |
|---|---|
| `crypto_touch_mm.py` | The bot (v2.5). One file: pricing, quoting, risk, alerts, status. |
| `test_crypto_touch_mm.py` | 102 unit tests (`python -m unittest test_crypto_touch_mm`). |
| `run_crypto_touch_mm.ps1` | Launcher: `-Market X -PollSecs N`; 0–45s jitter; restarts the bot forever; logs to `run-logs\crypto-touch\{market}-{date}.log`. |
| `run_crypto_touch_mm_hidden.vbs` | Windowless wrapper the tasks actually invoke (see Fleet ops). |
| `run_digest_hidden.vbs`, `run_watchdog_hidden.vbs` | Same for the digest + watchdog tasks. |
| `send_daily_digest.py` | 7:00 AM ET combined P&L email. |
| `KalshiClientsBaseV2ApiKey_FIXED.py` | Shared Kalshi client (all repo bots). This project added HTTP timeouts, `get_orders(status=)`, `self`-param fix. |
| `run-logs\crypto-touch\` | Logs, `status_{MARKET}.json` heartbeats, `cache_*.json` shared data, digest markers. Git-ignored. |

## Pricing model

Fair value = P(touch before month-end) under **driftless GBM** (reflection principle, both
directions, Monte-Carlo validated). Vol = daily log-returns from Kraken daily candles,
**0.6·EWMA(λ=0.94) + 0.4·stdev(90d)**, in-progress candle excluded. Time = now → last day
11:59 PM ET (per-market `expected_expiration_time` when present). Fairs track live books
within a few cents on majors; meme coins diverge (see guards).

## Quoting rules (accumulated user spec — all are hard requirements)

- **5 levels × 12 contracts** per side, first level **5c off fair**, levels **exactly 2c apart**
  (invariant: clamps move the ladder's *anchor*, never squeeze gaps). So bids sit at fair−5,
  −7, −9, −11, −13. Post-only, GTC + TTL.
- **Join, don't lead**: a quote may at most *match* the best **external** level (book net of our
  own orders — `external_best()`); a side with no external quotes gets nothing. Never alone at
  the top of the book.
- **Caps** (all enforced): ≤12 contracts per price level per market (hard invariant, `level_cap`
  alert if hit); ≤60 contracts (5×12) resting per (market, side) — unconditional backstop in
  `place_with_side_cap()`; **net ±320 per market per direction** (`CMM_MAX_POSITION`, position +
  full ladder ≤ cap); **±2000 net per event** (`CMM_MAX_EVENT`, split across strikes
  near-money-first). Caps scale with ladder size; history: 3×8 → 128/800; 5×10 → 267/1667
  (8/1); **5×12 → 320/2000** (8/5).
- **Stand-down guards** (skip market, log, digest-note): month-to-date extreme crossed the
  strike; fair ≥97c (near touch); book bid ≥85c while fair ≤ bid−20c (suspected unseen touch —
  the one urgent alert); |fair − book mid| >30c on a two-sided book (model/market disagreement).

## Order management & safety

- **TTL 600s stamped per-send / refreshed at 420s age** — quotes die ≤10 min if anything dies.
  Refresh MUST exceed worst-case cycle time or the fleet full-churns (learned the hard way).
- **Local order ledger merged with exchange reads** (`_merge_ledger`): Kalshi portfolio reads
  are eventually consistent; without the ledger a lagging read double-places ladders.
- **Fail-safe**: 4 *consecutive* errored cycles → cancel every resting order, exponential
  backoff, keep retrying. After suspend/resume (loop gap >10 min) errors are fail-safe-exempt
  for 120s (`wake grace`).
- Cancel-confirm before place; blind-orderbook markets keep quotes ≤3 cycles then pull;
  SIGINT/SIGTERM/atexit/finally all cancel; startup sweeps orphans by `client_order_id` prefix
  `cmm-` (current + previous month events).
- **One bot per market, enforced** (`acquire_singleton`): a live bot writes `lock_{MARKET}.pid`
  in STATUS_DIR; a second instance exits rather than double-quoting (duplicates keep separate
  ledgers, so per-side/level caps would silently double). A lock is honored only when the PID is
  alive **and** the market's heartbeat is fresh — a stale lock after PID reuse would otherwise
  park that market forever. `--once` and `--cancel-all` are exempt.
- `python crypto_touch_mm.py --market SOL-MAX --cancel-all` = emergency flatten (always real).
- Dry-run is the default everywhere; `--live` is explicit.

## Fleet operations

- **18 tasks**: `KL crypto_touch_mm {MARKET}` ×16 (at-logon) + `DIGEST` (daily 7:00 AM ET) +
  `WATCHDOG` (every 15 min: starts any bot task sitting in `Ready` = died/was killed).
  SOL-MAX polls 15s, others 30s.
- **Everything runs windowless.** Tasks invoke `wscript.exe //B <shim>.vbs`, which launches the
  PowerShell launcher with `windowstyle 0`. The shim **waits** (`sh.Run cmd, 0, True`) so the
  task stays `Running` — a fire-and-forget shim would make the watchdog treat it as dead and
  spawn duplicate fleets. Process chain: `wscript ← powershell ← cmd ← python`.
- **Deliberate pause** = `Disable-ScheduledTask` (the watchdog skips Disabled). Killing
  processes or plain `Stop-ScheduledTask` gets auto-revived within 15 min.
- **Restart procedure** (do it exactly this way): stop tasks → kill the **whole chain**
  (`wscript` shims, the PowerShell launchers, `cmd`, `python` — skipping anything with a
  `claude.exe` ancestor), looping a few passes → delete stale `lock_*.pid` and `__pycache__` →
  start tasks → **verify ~16 bot pythons, 16 locks, and banners showing the new version**.
  Two traps, both hit for real: (1) command-line matching does NOT work from the agent shell
  (`Win32_Process.CommandLine` returns null), so cmdline-based kills silently match nothing and
  leave the old fleet trading — two stale-code incidents; (2) killing only `python` leaves the
  launcher's `while` loop alive and it **respawns a duplicate** ~30s later — 24 processes for 16
  markets on 8/5, which the singleton lock now prevents. A count of 15 right after start is
  usually a launcher mid-respawn; confirm with heartbeats, not the raw count.
- A task stuck `Ready` with LastResult `0xC000013A` = queued-stop race → Unregister + Register
  + Start.
- **Shared data cache** (`cache_*.json`): price 10s, hourly candles 240s, daily 3600s. Kraken
  rate-limits per IP — 16 independent fetchers got the IP throttled by Kraken *and* Coinbase
  (fallback herd) and triggered a fail-safe storm. MAX/MIN pairs share identical data. Keep it.
- **Machine constraints**: consumer laptop. Sleep/hibernate disabled (AC+DC), lid-close on AC =
  do nothing (battery lid-close still sleeps, deliberate). **Modern Standby still freezes
  everything when idle/on battery** — quotes TTL-expire safely, bots resume on wake; cost ~8h
  uptime on 7/3 and a missed 7 AM digest on 7/12. VPS is the only real fix (~$5/mo, no code
  changes).
- Python: `AppData\Local\Programs\Python\Python312\python.exe`. Deps live in the interpreter's
  own `Lib\site-packages` — the Task Scheduler context **cannot see** the Roaming user-site, so
  don't "clean up" that duplication. Kalshi creds: key-id fallback in code, PEM
  `C:\Users\jackd\Downloads\Lisa_Kalshi.txt`. Gmail app password in HKCU env
  `ALERT_EMAIL_FROM`/`ALERT_EMAIL_PASSWORD` (launcher reads the registry directly).

## INCIDENT 2026-08-09: concurrent bot GENERATIONS (all crypto fleets)

Standby wakes had been MINTING fleets: three generations of the monthly bots (spawned by
wake-respawns on 8/5, 8/6, 8/7) and two of the updown fleet ran CONCURRENTLY - stacked ladders
on the same markets for days. Root cause: **the v2.5 singleton stops a newcomer but never
evicts an incumbent.** When standby makes the incumbent's heartbeat look stale, the newcomer
takes over the lock and the incumbent keeps trading forever (it checks the lock only at
startup). TODO: incumbent should re-verify it still holds its lock each cycle and exit if not.

Detection recipe (how it was caught): status_*.json failing json.load with "Extra data"
(two writers interleaving the SAME .tmp file - atomic replace still lands a complete-JSON-
plus-garbage hybrid), status pid ALTERNATING between two live pids across reads, python count
~2x expected, python StartTimes clustering in generations. NB: fleet_health() silently drops
unparseable status files, so the digest UNDER-COUNTS bots during exactly this failure.

Cleanup pattern (cleanup_duplicate_bots.ps1): kill every bot chain INCLUDING task-owned ones
(python + cmd + powershell ancestors; spare IMM + shell ancestry), then let the watchdog
revive one clean chain per market (<=15 min) and TTL flush the stacked book. Killing only
pythons is NOT enough - orphan launchers respawn in 30s. Three orphan chains survived the
first sweep (their pythons were mid-respawn during the survey; pid-reuse also forges PPID
links, e.g. an orphan launcher appeared to be a child of the IMM bot) - verify afterward by
checking lock/status pids against task-owned chains and re-sweep.

Same day, unrelated: a brief AC unplug stopped ONLY the 7 updown tasks - they'd been
registered with Task Scheduler defaults (start-only-on-AC + stop-on-battery) while every
other KL task is battery-safe. Fixed via fix_updown_battery.bat (Set-ScheduledTask, safe on
running tasks); register_updown_tasks.ps1 now sets battery-safe flags AND refuses to run
while updown tasks are Running (re-registering live tasks orphans their chains - that is how
updown gen 2 was minted).

## Alerts & digest

- **Email only, to jackdu224@gmail.com** (Gmail push on phone). T-Mobile's email-to-SMS gateway
  is dead since 2026-06-29 — all repo bots were rerouted (`bcc4bee`). App passwords expire
  periodically; symptom is SMTP 535 in `digest-task.log`.
- **Only `divergence` emails immediately.** Everything else is digest-only. Removed by request
  over time: fill, breach, near-touch (7/5), then failsafe + shutdown (7/13 — fleet restarts and
  standby wakes were firing 16 emails at once).
- **Digest has TWO sections since 8/12** (same columns/format): "Monthly one-touch" and
  "Weekly above/below (KX*D)" — the weekly rows come from the updown bots' heartbeats
  (their events are discovered, not computed), the headline Fleet P&L is the grand total,
  and each fleet gets its own health line.
- **Daily digest, 7:00 AM ET** (bots roll counters 6 AM ET): HTML table sorted best→worst —
  **P&L$ (realized+unrealized), REAL$, UNREAL$, NET contracts, EXPO$** — plus fleet totals,
  balance, and a health line flagging any bot whose heartbeat is >30 min stale. Retries ~40 min
  if the machine is asleep at 7. Idempotent via marker files.
  `python send_daily_digest.py --test` sends immediately.

## Known gaps / candidate next steps

1. **VPS migration** — the only real reliability gap (laptop sleep, accidental closes).
2. **Incentive rewards**: `GET /trade-api/v2/incentive_programs` (public; `market_ticker`,
   `period_reward` in centi-cents, `target_size_fp`, `discount_factor_bps`, status filters).
   Kalshi samples the book at a **random instant every second**; score = size × distance
   multiplier (1.0× at BBO, discounted deeper, capped at target size); payout = your share of
   the pool. The fleet is nearly ideal for this. Currently **no programs on our 16 events**
   (they cluster on MENTION markets) — a digest line flagging new crypto programs would catch
   free yield.
3. Fees are not modeled (post-only maker flow; spreads dominate).
4. Meme-coin model risk is handled by standing down (>30c divergence) — those books are often
   one-sided or unquoted. Deliberate, not a bug.
5. ZEC-MAX periodically logs "no active markets" when every upside strike has touched. Healthy
   idle, not a failure.
6. Log filenames stamp the python **start** date; a bot running for days keeps appending to its
   start-date log. Check mtimes, not names.

## Weekly variant (`crypto_touch_mm_weekly.py`, v1.0 — built 2026-08-05, NOT deployed)

A weekly sibling of this bot exists. It **subclasses** `TouchMarketMaker` rather than forking
it: pricing, the order ledger, the caps, the fail-safe loop, alerting and the daily summary are
all this file's code. Only the measurement window differs, and that lives behind horizon hooks
added to `TouchMarketMaker` for the purpose (`window_start_utc` / `window_end_utc` /
`window_label` / `min_hours_left` / `skip_reason_for_fair` / `active_markets` /
`maybe_rollover` / `poll_interval` / `startup_event_and_sweep` / `status_dir` / `status_extra`,
plus the per-fleet class attributes `model_version` / `client_order_prefix` / `poll_secs` /
`max_position` / `max_event`). **The monthly fleet's behaviour is unchanged** — all 96 existing
tests pass untouched — but the extraction means a future fix to `run_cycle` benefits both.

- **There is nothing to trade yet.** Exchange-wide, exactly two series match this family —
  `KXBTCMAXW` and `KXDOGEMAXW` — and both have been dormant since **November 2024** (last
  events `KXBTCMAXW-24-NOV22`, `KXDOGEMAXW-24`). No weekly MIN series exists for any asset. The
  bot's normal state today is **idle**: it polls slowly, logs `no open weekly event`, places
  nothing, and starts quoting on its own the day Kalshi relists. `--discover` reports live ones.
- **Event tickers are DISCOVERED, not computed.** The two historical weeklies used two
  different, non-monthly conventions, so a formula would be a guess. Each cycle asks
  `get_markets(series_ticker=…, status=open)`, groups by event, and trades the **front week**
  (soonest window end still in the future). The window comes from those markets' own
  `open_time` / `expected_expiration_time`, so stub weeks and odd close hours just work.
- **Calibration differences** (all `CTW_*`-tunable): `min_hours_left` 2.0 (vs 1.0); a NEW
  deep-OTM stand-down at `fair <= 3c` (`CTW_SKIP_FAIR_BELOW_CENTS`) because driftless GBM
  *understates* fat-tailed touch probability over 7 days and the ask side of a 2c market risks
  ~98c to win 2c; caps 100/500 (vs 267/1667) as an unproven-market starting point — **raise
  only against a ledger**. Ladder spec is unchanged (5×10, 5c off fair, 2c apart, post-only,
  join-don't-lead).
- **Isolation is enforced and tested**: own env namespace (`CTW_*`, never written back onto
  `CMM_*` unless the operator sets one), own `client_order_id` prefix (`cmw-`, disjoint from
  `cmm-`), own heartbeat dir (`run-logs\crypto-touch-weekly\` — keeping weekly bots out of
  `send_daily_digest.py`'s `status_*.json` glob, which would otherwise price them with monthly
  tickers). The Kraken/Coinbase **cache stays shared** with this fleet on purpose (per-IP rate
  limits). `test_crypto_touch_mm_weekly.py` has 71 tests, including a `TestFleetIsolation` class
  that fails if the weekly bot can retune or impersonate the monthly one.
- Files: `crypto_touch_mm_weekly.py`, `test_crypto_touch_mm_weekly.py`,
  `run_crypto_touch_mm_weekly.ps1`, `run_crypto_touch_mm_weekly_hidden.vbs`.
  No scheduled tasks registered — deploy only once a weekly event actually lists.
- Also added: `ExchangeClient.get_events()` (additive; the weekly orphan sweep needs to list a
  series' recent events).

## Above/below variant (`crypto_updown_mm.py`, v1.0 — built 2026-08-05)

**LIVE PILOT (armed 2026-08-05, weekly tenor, all assets).** Settings per Jack: `--cadence
weekly`, ladder **3 x 2** (doubled from 3x1 on 8/10, caps 2x'd 40/200/400 -> **80/400/800**),
**3c off fair**, 2c apart, band **10-90c**, **8 markets/event**.
The offset went 5c -> 3c after the first live cycles showed us resting 5-6c behind a ~2c-wide
book — at 5c we would essentially never have filled.

**INCIDENT 2026-08-05 -> 08-08: the whole updown fleet died and stayed dead 3 days.** The seven
bots were launched fire-and-forget (`Start-Process wscript`), NOT as scheduled tasks, and the
watchdog filter only covered `KL crypto_touch_mm *-M*`. At 12:57:57Z on 8/5 all seven pythons
exited code -1 simultaneously (launchers respawned them); ~2 min later the entire chains died
mid-write — an external kill sweep, consistent with the monthly fleet's restart procedure,
whose structural sweep kills ANY python whose parent is cmd (the updown bots match). Nothing
restarted them, the 8/7 weekly settled unquoted, and the 8/14 weekly listed with nobody on it.
TTL cleaned the book (0 orphans); the settled weekly had **zero fills** in ~14h of quoting, so
nothing was lost — but auto-roll only works if the process is alive. Fixes: the watchdog now
also sweeps `KL crypto_updown_mm *` (run_watchdog_hidden.vbs), and the fleet must be run as
**registered scheduled tasks**, never bare Start-Process. Note for the restart procedure: the
structural python sweep WILL kill updown bots too — with tasks + watchdog they come back
within 15 min, but expect it. Registration is one elevated double-click:
`register_updown_tasks.bat` (idempotent; UAC-elevates itself, registers all 7 at-logon tasks
and starts them).

The 3x1 ladder bounds
resting size at 3 contracts per market per side — 48 contracts per asset if every level on both
sides filled (~$25/side at mid). The 40/200/400 caps are non-binding backstops at this size;
**the ladder is the pilot control — scale it and revisit the caps together.** Kill switch:
`python crypto_updown_mm.py --asset BTC --cancel-all`.
`test_crypto_updown_mm.py::test_ladder_is_the_live_pilot_shape` pins the deployed shape, so
loosening size has to break a test first.

Markets the one-touch fleet does **not** cover: `KX{ASSET}D`, "What will {ASSET}'s price be on
{date} at {hour} ET?" — 21 open events across 7 assets right now, far better quoted than the
monthlies.

**These settle on the TERMINAL price, not on a touch — and that is easy to miss.** They carry
`strike_type="greater"` and a `floor_strike`, *exactly* like the MAX one-touch markets, so
`crypto_touch_mm.py` parses them without complaint. Measured, `touch_prob` is **2.0–2.1×** the
true terminal probability at every tenor here: a market genuinely worth 31c prices at 62c under
the one-touch model. That would systematically overpay for YES and undersell NO across the whole
ladder with no existing guard catching it, because the numbers still look plausible. Hence a
separate module, a separate `terminal_prob()` (Black-Scholes N(d2), r=0, MC-validated to
<0.001), and a regression test pinning the two models apart.

- **Three tenors trade under one series**, classified by the event's own window (exchange
  metadata, not ticker convention): `hourly` ~1h (188 strikes on BTC), `daily` ~25h, and
  **`weekly` ~169h** — so a 7-day crypto market does exist; it is above/below, not one-touch.
  One bot quotes all selected tenors of one asset concurrently: `--cadence hourly|daily|weekly`
  (comma list), `both` (default), or `all`.
- **Live calibration, measured 2026-08-05** against real two-sided books (bid>0, ask<100,
  spread≤10c) across 7 assets — model fair vs book mid: hourly `MAE 2.7c` (n=24), daily
  `MAE 1.6c` (n=41), weekly `MAE 3.1c` (n=46). Mean error is −0.4 to −0.9c on every tenor, i.e.
  a small consistent bias below the market — worth watching, plausibly the driftless assumption.
  Thin books (a BNB hourly, MAE 13c) are caught by the 15c divergence stand-down.
  *Beware when re-measuring:* Kalshi reports `yes_ask_dollars=1.00` when there is **no** offer,
  so a naive "mid" of a one-sided book reads ~51c and fabricates huge errors. The bot is immune
  (it uses `external_best()` on the real orderbook), but a diagnostic script is not.
- **Volatility is horizon-matched**: 5m candles under 6h, 60m under 3d, daily beyond, each
  scaled to a per-day sigma and blended 70/30 with the daily estimate. A 1h market priced off
  90-day candles ignores the regime it expires into.
- **Near-money band only** (`fair` strictly between 10c and 90c, then the 8 nearest to 50c).
  Without it an hourly BTC event would need 188 orderbook calls per cycle.
- Caps 40 / 200 / 400 (market / event / **asset**, the last spanning all tenors). Bug found and
  fixed in dry run: draining the asset budget greedily in expiry order
  let hourly+daily consume all of it and the weekly quoted **neither side**; each event now gets
  an even share of the remaining budget with unused share rolling forward.
- Own env namespace `CUD_*`, own prefix `cud-`, own heartbeat dir `run-logs\crypto-updown\`.
  `test_crypto_updown_mm.py` has 58 tests; `TestFleetIsolation` asserts all three fleets'
  prefixes and status dirs are pairwise disjoint. **223 tests pass across all three suites.**
- **DOGE uses a different market shape.** Every DOGE rung across all three tenors (118 markets)
  is `strike_type="custom"` with `floor_strike=None` — same "$0.18 or above" payoff as the
  others, but the numeric strike exists only in the ticker suffix. Every other asset is
  `greater` with a numeric strike. Unhandled, DOGE priced 0 strikes and quoted nothing while
  reporting a healthy `36 strikes -> 0 quotable`. Now recovered from the ticker, but only when
  `yes_sub_title` independently confirms both the direction ("or above") and the number to
  within 1% — otherwise the market is skipped rather than traded on a guessed strike.
- **BNB and HYPE weekly books are empty** (bid 1c / ask 99c, nothing between). Join-don't-lead
  matches those extremes, so we rest one 1c bid and one 99c ask per market — free options
  (max loss 1c, hugely +EV if ever hit) but not market making. Real two-sided quoting is
  BTC / SOL / ETH / XRP.
- ZEC lists no `KXZECD` events (configured anyway; it idles).
- Files: `crypto_updown_mm.py`, `test_crypto_updown_mm.py`, `run_crypto_updown_mm.ps1`,
  `run_crypto_updown_mm_hidden.vbs`. No scheduled tasks registered yet — the pilot is started
  by hand (see Quick commands); register tasks once it has run clean for a session.

## Quick commands

```powershell
# --- above/below variant ---
python crypto_updown_mm.py --discover                       # all open events, all tenors
python crypto_updown_mm.py --asset BTC --cadence all --once  # dry run, one cycle
python crypto_updown_mm.py --asset BTC --cancel-all
python -m unittest test_crypto_updown_mm

# --- weekly variant ---
# what weekly one-touch events are live right now (nothing, as of 2026-08-05)
python crypto_touch_mm_weekly.py --discover
python crypto_touch_mm_weekly.py --market BTC-MAX --once      # dry run, one cycle
python crypto_touch_mm_weekly.py --market BTC-MAX --cancel-all
python -m unittest test_crypto_touch_mm test_crypto_touch_mm_weekly

# fleet status
Get-ScheduledTask -TaskName "KL crypto_touch_mm *" | Select TaskName, State
# deliberate pause / resume one market (watchdog-proof)
Disable-ScheduledTask -TaskName "KL crypto_touch_mm DOGE-MAX"
Enable-ScheduledTask  -TaskName "KL crypto_touch_mm DOGE-MAX"
# emergency flatten one market's quotes
python crypto_touch_mm.py --market DOGE-MAX --cancel-all
# live health: heartbeats + P&L without emailing
python -c "import json,glob;[print(json.load(open(f))['market'], json.load(open(f))['updated_at']) for f in glob.glob('run-logs/crypto-touch/status_*.json')]"
python send_daily_digest.py --test
# tests
python -m unittest test_crypto_touch_mm
```

## Session history (what changed and why)

Built 2026-07-03 from scratch; went live the same day after a single 1-contract write-path
verification. Expanded to 8 assets, then to MAX+MIN (16 bots). Notable fixes, each driven by a
real incident: dead SMS gateway → email (also fixed 4 other repo bots); Modern Standby freezes →
power settings + wake grace; TTL death-loop after slow wake-up placement waves → per-send
expiration stamps; overlapping ladders → order ledger + hard caps; fleet-scale API throttling →
shared cache + startup jitter; alert storms → digest-only categories; accidental window closes
→ watchdog + windowless shims; two stale-code rollouts → PID-based restart procedure.
