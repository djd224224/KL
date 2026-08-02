# GAS_BOT_HANDOFF — AAA gas-price bot (KXAAAGASD / KXAAAGASW / KXAAAGASM)

Built 2026-07-28. Status as of 2026-07-28 am:
- **Sniper: LIVE** — Task Scheduler job "KL gas snipe", daily 2:40am local
  (machine TZ = ET). Sizing (Jack 2026-07-28, second revision): **per-morning
  envelope of 5 contracts** (`GAS_SNIPE_MORNING_CAP=5`), spread
  **best-net-edge-first in 1-contract per-strike units**
  (`GAS_SNIPE_UNIT_CONTRACTS=1`) across all strikes/events. The envelope —
  not any per-event split — is the risk unit, because every same-morning
  take is one correlated bet on the same AAA path. Envelope counts
  placements (an unfilled IOC consumes it). Worst case ~$5/morning.
- **Carry: dry-run only, not scheduled.**

Strategies implemented (Jack picked 1 + 2 from the proposal):
1. **Print-time sniper** — the AAA national average (the settlement source,
   gasprices.aaa.com) updates once a day ~3-7am ET. The sniper polls the page
   inside an ET window, and the moment the new print appears it IOC-takes
   quotes that haven't repriced yet, across all three gas series.
2. **Drift + carry model** — settle = last_print + drift·h + N(0, σ_h);
   EWMA drift (walk-forward-selected params) with per-day decay, σ_h power law
   calibrated from history. Carry mode rests post-only maker orders where the
   blended fair differs from the market by ≥ 4c.

## Files

- `gas_data.py` — data layer. AAA live scrape, Wayback backfill (exact
  values), Kalshi settled-market backfill (half-cent brackets from finalized
  daily/weekly/monthly strikes), validation, `gas_data/aaa_history.csv`.
- `gas_bot.py` — model + both modes + risk rails. Dry-run by default.
- `run_gas_bot.ps1` — TS launcher (`-Mode snipe|carry`, `-Live`). Logs to
  `run-logs\gas-bot\`. **Not registered as a task** (deliberate).
- `gas_data/aaa_history.csv` — 92 days (2026-03-03..07-28), 37 exact-value
  (wayback/live), rest Kalshi bracket midpoints (±0.25c).
- `gas_data/status_gas_bot.json` — day-spend ledger. `gas_data/HALT` — touch
  to stop placements + cancel resting (checked every cycle).

## Speed upgrade (2026-08-01, after 3 missed IOCs on the first surprise)

The 8/1 print (+0.8c, first real surprise) was caught at 3:20:27 but all
three IOCs missed — the stale asks vanished in the ~2s between detection
and order arrival. v1.1: hot-window fast polling (GAS_SNIPE_HOT_WINDOW
03:10-03:45 ET @ GAS_SNIPE_HOT_POLL_SECS 5s — every observed print: 3:18,
3:20, 3:24, 3:36), a universe+book cache refreshed every
GAS_SNIPE_PREFETCH_SECS (60s) during the wait, and two-wave firing: wave 1
sends IOCs against the cache instantly on the print flip (limit prices
bound staleness), wave 2 re-fetches fresh and sweeps the remaining
envelope. Detection latency ~2.5s expected (was ~10s), post-detection
~200ms (was ~2s).

Day-of-week drift (built same evening): per-weekday delta adjustments
(shrunk n/(n+GAS_DOW_SHRINK_K), clamped GAS_DOW_CLAMP_CENTS, off via
GAS_DOW_ENABLE=0); EWMA runs on demeaned deltas, projections re-add the
adjustment along the calendar path. Learned 8/1: Sat -0.41 / Sun -0.37 /
Mon -0.13 vs Wed/Thu +0.65; h1-3 RMS -7-10%. Caveat: on demeaned deltas
the walk-forward grid re-picked last-delta weight 0.6, so the model still
runs hot after surprise prints — the sniper's eff_fair = min(model,
pre-print mid + model shift) anchor stays the real guard on live takes.

## Data integrity (verified 2026-07-28)

- `python gas_data.py validate` → 13 overlapping days, **0 violations**
  (every exact value inside its Kalshi settlement bracket). The scrape, the
  as-of-date keying, and Kalshi's settlement source all agree.
- Settlement mechanics: markets close 11:59pm ET the night before; the
  settling print posts next morning (Kalshi reads it ~10am ET). So the final
  print is never tradeable, and h ≥ 1 always. "Strictly greater" strikes.
- Kalshi's `expiration_value` is 2dp-rounded; finalized strike results give
  the true half-cent bracket (that's what backfill-kalshi stores).

## Model state at build time

- 2026-07 regime is HOT: +25c spike Jul 15→25 (+4-5c days), now rolling
  over (-0.25, -0.25, -1.0). Calibration: σ₁ ≈ 1.8c RMS, σ_h ≈ 1.8·h^0.8,
  drift params hl=3d / λ_last=0.3 / ρ=1.0 (walk-forward grid on h=1..3 RMS).
- **Known limitations (v1):** no wholesale (RBOB) input, so the model lags
  the market at trend turning points; params re-fit each start and can hop
  grid points; sigma has mild selection-bias optimism (offset by ×1.15 pad);
  normal tails understate jump days. Mitigations in code: carry quotes a
  50/50 blend of model and market mid (`GAS_MODEL_WEIGHT`), stands aside
  when |model−mid| > 25c on a two-sided book (`GAS_MAX_DISAGREE_CENTS`),
  and the sniper only harvests *print surprise* anchored to the pre-print
  mid — `eff_fair = min(model, mid + Δfair_model)` per side — never the
  model's absolute disagreement.

## Safety rails

- DRY RUN default everywhere; `--live` required for real orders.
- Only orders with client_order_id prefix `gas-` are ever cancelled.
- Budgets: total $500 (`GAS_BUDGET_DOLLARS`), $250/event, 50/strike,
  $300 new-cost/ET-day, balance floor $2500 (shared account), price band
  2..98c, max 40 placements/cycle, order TTL 1h.
- Stale-data refusal: no trading if newest print > 30h old (carry self-heals
  by scraping AAA directly).
- `--cancel-all` (always real) to flatten resting orders.

## Ops quick reference

```powershell
python gas_bot.py --status       # fair vs market table (read-only)
python gas_bot.py --calibrate    # model diagnostics
python gas_bot.py --once         # one dry carry cycle
python gas_bot.py --mode snipe --snipe-now   # snipe plumbing test (inert)
python gas_data.py live          # record today's AAA print
python gas_data.py show --days 14
python gas_data.py validate      # exact vs Kalshi brackets (~3 min)
```

## Scale-up path

1. ~~Dry soak~~ / ~~snipe live~~ — **done 2026-07-28**: snipe live with a
   5-contract morning envelope in 1/strike units (worst case ~$5/morning).
   Watch `run-logs\gas-bot\gas-bot-YYYY-MM-DD.log` + the snipe email after
   each print; reconcile fills against next-morning settlements for a few
   days.
2. **Scale the sniper** by raising `GAS_SNIPE_MORNING_CAP` (envelope, e.g.
   5 -> 15 -> 30) and/or `GAS_SNIPE_UNIT_CONTRACTS` (per-strike unit, e.g.
   1 -> 3 -> 5) via env in run_gas_bot.ps1's cmd line. Keep unit small
   relative to envelope so size stays spread across strikes. Per-strike cap
   50, day-cost cap $300 and total budget $500 still bound everything.
3. **Carry live small** only after snipe P&L confirms the model's h=1..3
   fairs aren't systematically off. Register a "KL gas carry" at-logon task
   with `-Mode carry -Live`; consider `GAS_MODEL_WEIGHT=0.3` (more
   market-anchored) to start.
4. Before scaling further: add RBOB lead-lag input (strategy 3 from the
   proposal), realized-P&L daily halt, GasBuddy intraday check for the
   settlement-eve pin trade.

## Scheduling (registered 2026-07-28)

- **"KL gas snipe"** — daily 2:40am local (= ET on this machine), action:
  `powershell -NoProfile -ExecutionPolicy Bypass -File run_gas_bot.ps1
  -Mode snipe -Live`. StartWhenAvailable + WakeToRun + battery-proof,
  6h execution limit. The bot itself waits for the 02:45-07:30 ET window,
  fires once on the new print, exits.
- Carry task: not registered (step 3 above).
- To pause the sniper: `Disable-ScheduledTask -TaskName "KL gas snipe"`,
  or touch `gas_data\HALT` (bot checks it before placing).

## Gotchas inherited from the fleet

- The IMM blocklist (`IMM_BLOCKLIST=KXAAAGASW,KXAAAGASM,KXAAAGASD,...`)
  keeps the incentive MM off gas — do NOT remove it; this bot is the only
  intended gas trader. Positions are netted per account, not per bot.
- Alert creds come from HKCU env (`ALERT_EMAIL_FROM/PASSWORD`); launcher
  pulls them like run_incentive_mm.ps1. SMS gateway is dead — email only.
- Shared API account: client keep-alive ON, default 100ms throttle (don't
  lower; the crypto fleet + IMM share the budget).
- Wayback top-up (optional, monthly): `python gas_data.py backfill-wayback
  --from <last-month>` — some snapshots are junk captures and skip; fine.
