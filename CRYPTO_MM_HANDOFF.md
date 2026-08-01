# Kalshi Crypto One-Touch Market-Maker Fleet — Handoff

*Written 2026-07-08. Self-contained context for a new session. Owner: Jack (jackdu224@gmail.com).*

## What this is

**16 live market-making bots** on Kalshi's crypto monthly one-touch events — "How high/low will
{ASSET} get in {month}?" — for **SOL, BTC, ETH, XRP, ZEC, HYPE, DOGE, BNB × MAX and MIN**.
Live with real money since **2026-07-03** (account balance ~$8.5k, collateral at rest ~$0.5–1k).
Each bot is one Windows Scheduled Task on this machine running one Python process.

Each strike is a one-touch binary (`strike_type=greater` on MAX / `less` on MIN, settled on the
CF Benchmarks trimmed-mean minute price; touched strikes settle early). Event tickers follow
`KX{ASSET}{MAX|MIN}MON-{ASSET}-{YY}{MON}{lastday}` (e.g. `KXSOLMAXMON-SOL-26JUL31`) and the bots
**roll to the next month automatically** (ET calendar; sweeps stale orders across the rollover).

## Files (all in `C:\Users\jackd\Documents\KL`, currently **untracked in git** — worth committing)

| File | Role |
|---|---|
| `crypto_touch_mm.py` | The bot (v1.7). One file: pricing, quoting, risk, alerts, status. |
| `test_crypto_touch_mm.py` | 96 unit tests (`python -m unittest test_crypto_touch_mm`). |
| `run_crypto_touch_mm.ps1` | Task launcher: `-Market X -PollSecs N`; 0–45s startup jitter; restarts bot forever; logs to `run-logs\crypto-touch\{market}-{date}.log`. |
| `send_daily_digest.py` | 7:00 AM ET combined email (task `KL crypto_touch_mm DIGEST`). |
| `KalshiClientsBaseV2ApiKey_FIXED.py` | Shared Kalshi client (all repo bots). Session added: HTTP timeouts, `get_orders(status=)`, `self`-param fix. **Modified, uncommitted.** |
| `run-logs\crypto-touch\` | Logs, `status_{MARKET}.json` heartbeats, `cache_*.json` shared market data, digest markers. Git-ignored. |

## Pricing model

Fair value = P(touch before month-end) under **driftless GBM** (reflection principle, both
directions, Monte-Carlo validated). Vol = daily log-returns from Kraken daily candles,
**0.6·EWMA(λ=0.94) + 0.4·stdev(90d)**, in-progress candle excluded. Time = now → last day
11:59 PM ET (per-market `expected_expiration_time` when present). Fairs tracked live books
within a few cents on majors; meme coins diverge (see guards).

## Quoting rules (accumulated user spec — all are hard requirements)

- **5 levels × 10 contracts**, first level **5c off fair**, levels **exactly 2c apart**
  (invariant: clamps move the ladder's *anchor*, never squeeze gaps). Post-only, GTC + TTL.
- **Join, don't lead**: a quote may at most *match* the best **external** level (book net of our
  own orders — `external_best()`); a side with no external quotes gets nothing. Never alone at
  the top of the book.
- **Caps** (all enforced, 5×10 as of 2026-08-01): ≤10 contracts per price level per market (hard
  invariant, `level_cap` alert if ever hit); ≤50 contracts (5×10) resting
  per (market, side) — unconditional backstop in `place_with_side_cap()`; **net ±267 per market
  per direction** (`CMM_MAX_POSITION`, position + full ladder ≤ cap); **±1667 net per event**
  (`CMM_MAX_EVENT`, budget split across strikes near-money-first). No other directional stop.
- **Stand-down guards** (skip market, log, digest-note): month-to-date extreme crossed the
  strike (Kraken hourly+daily candles + session, accumulated); fair ≥97c (near touch);
  book bid ≥85c while fair ≤ bid−20c (suspected unseen touch — urgent alert);
  |fair − book mid| >30c on a two-sided book (model/market disagreement, don't take the view).

## Order management & safety

- **TTL 600s stamped per-send / refreshed at 420s age** — quotes die ≤10 min if anything dies.
  Refresh MUST exceed worst-case cycle time or the fleet full-churns (learned the hard way).
- **Local order ledger merged with exchange reads** (`_merge_ledger`): Kalshi portfolio reads
  are eventually consistent; without the ledger a lagging read double-places ladders.
  Unconfirmed entries count as resting for 2×poll+15s, then verified via `get_order`.
- **Fail-safe**: 4 *consecutive* errored cycles → cancel every resting order, exponential
  backoff (×2…×8), keep retrying. After a suspend/resume (loop gap >10 min) errors are
  fail-safe-exempt for 120s (`wake grace`) while the network reconnects.
- Cancel-confirm before place; unparseable resting orders cancelled defensively; blind-orderbook
  markets keep quotes ≤3 cycles then pull; SIGINT/SIGTERM/atexit/finally all cancel; startup
  sweeps orphans by `client_order_id` prefix `cmm-` (current + previous month events).
- `python crypto_touch_mm.py --market SOL-MAX --cancel-all` = emergency flatten (always real).
- Dry-run is the default everywhere; `--live` is explicit.

## Fleet operations

- **Tasks**: `KL crypto_touch_mm {MARKET}` ×16 (at-logon trigger, non-elevated) + `DIGEST`
  (daily 7:00 AM local/ET). SOL-MAX polls 15s; all others 30s.
- **Restart procedure**: stop tasks → kill `run_crypto_touch_mm|crypto_touch_mm.py` processes
  (exclude own PID) → delete `__pycache__` → start tasks → **verify 16 Running AND banners show
  the expected version** (stale pycache/orphan launchers have served old code). A task stuck
  `Ready` with LastResult `0xC000013A` = queued-stop race → Unregister + Register + Start.
- **Shared data cache** (`cache_*.json`): price 10s, hourly candles 240s, daily 3600s.
  Kraken rate-limits per IP — 16 independent fetchers got the IP throttled by Kraken *and*
  Coinbase (fallback herd). MAX/MIN pairs share identical data. Don't remove this.
- **Machine constraints**: consumer laptop. Sleep/hibernate disabled (AC+DC), lid-close on AC =
  do nothing (battery lid-close still sleeps, deliberate). **Modern Standby still freezes
  everything when idle/on battery** — quotes TTL-expire safely, bots resume on wake; this cost
  ~8h of uptime on 7/3. For true 24/7 move the fleet to a VPS (~$5/mo, zero code changes).
- Python: `AppData\Local\Programs\Python\Python312\python.exe`. Deps live in the interpreter's
  own `Lib\site-packages` (Task Scheduler context cannot see the Roaming user-site — real bug,
  don't "clean up"). Kalshi creds: key-id fallback in code, PEM `C:\Users\jackd\Downloads\Lisa_Kalshi.txt`.
  Gmail app password in HKCU env `ALERT_EMAIL_FROM/PASSWORD` (launcher pulls from registry).

## Alerts & digest

- **All notifications are email to jackdu224@gmail.com** (Gmail push on phone). T-Mobile's
  email-to-SMS gateway is dead since 2026-06-29 — every repo bot was rerouted (commit `bcc4bee`).
- Urgent (immediate, deduped 6h/market): `divergence` (unseen touch), `failsafe`, `shutdown`.
  Digest-only: `side_cap`, `model_divergence`, `rollover`. Removed by request: fill, breach,
  near-touch alerts.
- **Daily digest, 7:00 AM ET** (bots roll counters 6 AM ET): HTML table per market —
  **P&L$ (realized+unrealized, sorted best→worst), REAL$, UNREAL$, NET contracts, EXPO$** —
  with fleet totals, balance, and a health line that flags any bot whose heartbeat
  (`status_*.json`) is >30 min stale. Realized P&L from fills (avg-cost round trips);
  unrealized marks positions at current book. Idempotent via marker files;
  `python send_daily_digest.py --test` sends immediately.

## State as of 2026-07-08

- 16/16 tasks running; ~350–450 orders resting typical. **ZEC-MAX idle** — every ZEC upside
  strike already touched; "no active markets" is healthy (resumes when Kalshi lists strikes).
- P&L (7/6 digest): fleet ≈ **−$13** (realized +$4). Biggest drag HYPE-MAX (−$35 unrealized,
  +212 net long from day-one fills); biggest winner XRP-MAX (+$20). Balance ≈ $8,535.
- July events end 7/31 11:59 PM ET; bots roll to August automatically (new-month events may
  list a few hours late — bots poll until they appear).

## Known gaps / candidate next steps

1. **Commit the bot suite to git** (it's all untracked; the repo's other bots are committed).
2. **VPS migration** for 24/7 uptime (only real reliability gap left).
3. **Incentive rewards**: Kalshi exposes liquidity/volume reward programs at
   `GET /trade-api/v2/incentive_programs` (status filters; `market_ticker`, `period_reward`,
   `target_size_fp`, `discount_factor_bps`). **Not** on market objects. Currently none on our
   16 events — a digest line flagging new programs on crypto markets would catch free yield.
4. Fees are not modeled (post-only maker flow; spreads dominate). Fine at 5-lots.
5. Meme-coin model risk is handled by standing down (>30c divergence), which also means those
   books are often one-sided or unquoted — deliberate, not a bug.
6. Log filenames stamp the python **start** date; a bot running for days keeps appending to its
   start-date log. Check file mtimes, not names.

## Quick commands

```powershell
# fleet status
Get-ScheduledTask -TaskName "KL crypto_touch_mm *" | Select TaskName, State
# stop / start one market
Stop-ScheduledTask  -TaskName "KL crypto_touch_mm DOGE-MAX"
Start-ScheduledTask -TaskName "KL crypto_touch_mm DOGE-MAX"
# emergency flatten one market's quotes
python crypto_touch_mm.py --market DOGE-MAX --cancel-all
# watch a bot
Get-Content run-logs\crypto-touch\sol-max-*.log -Tail 30 -Wait
# tests / manual digest
python -m unittest test_crypto_touch_mm
python send_daily_digest.py --test
```
