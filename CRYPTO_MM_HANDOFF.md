# Kalshi Crypto One-Touch Market-Maker Fleet — Handoff

*Current as of 2026-08-05. Self-contained context for a new session. Owner: Jack (jackdu224@gmail.com).*
*Repo: `C:\Users\jackd\Documents\KL` (github.com/djd224224/KL). Bot version v2.1.*

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
| `crypto_touch_mm.py` | The bot (v2.1). One file: pricing, quoting, risk, alerts, status. |
| `test_crypto_touch_mm.py` | 96 unit tests (`python -m unittest test_crypto_touch_mm`). |
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

- **5 levels × 10 contracts** per side, first level **5c off fair**, levels **exactly 2c apart**
  (invariant: clamps move the ladder's *anchor*, never squeeze gaps). So bids sit at fair−5,
  −7, −9, −11, −13. Post-only, GTC + TTL.
- **Join, don't lead**: a quote may at most *match* the best **external** level (book net of our
  own orders — `external_best()`); a side with no external quotes gets nothing. Never alone at
  the top of the book.
- **Caps** (all enforced): ≤10 contracts per price level per market (hard invariant, `level_cap`
  alert if hit); ≤50 contracts (5×10) resting per (market, side) — unconditional backstop in
  `place_with_side_cap()`; **net ±267 per market per direction** (`CMM_MAX_POSITION`, position +
  full ladder ≤ cap); **±1667 net per event** (`CMM_MAX_EVENT`, split across strikes
  near-money-first). Sizing history: 3×10 → 3×5 → 3×8 (caps 128/800) → 3×10 → **5×10 with caps
  scaled 50/24 → 267/1667** (2026-08-01).
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
- **Restart procedure** (do it exactly this way): stop tasks → kill bots **by the PIDs in
  `status_*.json`** plus a structural sweep (python whose parent is cmd) → verify the python
  count actually dropped → delete `__pycache__` → start tasks → **verify 16 Running AND banners
  show the new version**. Command-line matching does NOT work from the agent shell
  (`Win32_Process.CommandLine` returns null) — a cmdline-based kill silently matches nothing and
  leaves the old fleet trading. This has caused two stale-code incidents.
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

## Alerts & digest

- **Email only, to jackdu224@gmail.com** (Gmail push on phone). T-Mobile's email-to-SMS gateway
  is dead since 2026-06-29 — all repo bots were rerouted (`bcc4bee`). App passwords expire
  periodically; symptom is SMTP 535 in `digest-task.log`.
- **Only `divergence` emails immediately.** Everything else is digest-only. Removed by request
  over time: fill, breach, near-touch (7/5), then failsafe + shutdown (7/13 — fleet restarts and
  standby wakes were firing 16 emails at once).
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

## Quick commands

```powershell
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
