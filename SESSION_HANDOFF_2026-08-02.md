# Session Handoff — 2026-07-28 → 2026-08-02 (gas bot build + IMM post-amendment overhaul)

*Written 2026-08-02 ~07:30 ET for a fresh context window. Everything below is
committed as of this file's commit. Memory files under
`~/.claude/projects/C--Users-jackd-Documents-KL/memory/` carry the same facts
in recall-sized pieces (project_gas_bot.md, project_incentive_mm.md).*

## TL;DR state of the world

- **Gas sniper: LIVE**, daily one-shot TS task "KL gas snipe" 2:40am ET.
  1-contract units, 5/morning envelope. First real fire 8/1 (+0.8c print):
  caught at 3:20:27, all 3 IOCs missed by ~2s → speed v1.1 + day-of-week
  drift shipped same day, first armed run = next print.
- **Gas carry: DRY-ONLY, unscheduled** (deliberate — model lags market at
  turning points without RBOB input).
- **IMM: LIVE** under Kalshi's AMENDED liquidity scoring (effective 7/30,
  support-confirmed): reference-based estimator + coverage EMA, atref
  ladders (band-exempt, deep-ref 2x sizing), slack pads, hysteresis
  requoting. ~330 selected / ~360 managed markets, est ~$100/day (new
  honest math; old estimator inflated ~30x).
- **Credits reconciliation still PENDING** — the ground-truth check of the
  new estimator vs Kalshi's actual paid credits for any post-7/30 period.
  Do this before scaling anything.

## Current IMM launcher env (run_incentive_mm.ps1, verbatim)

```
IMM_BLOCKLIST=KXAAAGASW,KXAAAGASM,KXAAAGASD,KXUSGASCPI,KXRAIN<9 monthlies>
IMM_LEVELS=0:20            IMM_TEMP_LEVELS=0:20   (temp MUST stay explicit:
                            its override default is 5/2/2, NOT the global)
IMM_MAX_POSITION=150       IMM_MAX_EVENT=1000     (all series, pinned)
IMM_MAX_TOTAL_RESTING=4000 (raised from 2000, 8/2)
IMM_LADDER_MODE=atref      IMM_MAX_MARKETS=50     (this is the EVENT cap)
IMM_COLLATERAL_BUDGET=13000  IMM_ORDER_TTL_SECS=1800  IMM_ORDER_REFRESH_SECS=1500
KALSHI_RATE_LIMIT_MS=25    IMM_MAX_PLACEMENTS_PER_CYCLE=250
IMM_HOUR_SIZE_MULT=3-7:2.0 (ET quiet hours 2x; KXTEMP excluded in code)
IMM_BALANCE_DROP_HALT=5000
```

Sizes stack: 20 base × quiet-hours 2x (3-7am ET) × deep-ref mult (up to 2x,
+0.1/tick behind touch) → 20-80/side; caps/skew/budget bind above.

## The amendment (the week's central fact)

CFTC filing 7/15, effective 7/30, engine confirmed by Kalshi support (the
site's per-order efficiency tooltip is WRONG — still touch-based; ignore it):
- Reference price = level where cumulative depth reaches target/5 (200 for
  tgt 1000). Everything at/above it: FULL weight pro-rata by size. Below:
  0.5^ticks-below-REFERENCE. Whole levels included atomically (no FIFO).
- Snapshot EXCLUDED unless BOTH sides reach target; payout scaled by
  non-excluded ratio → pads matter more (they un-exclude), one-sided books
  pay nobody.
- Contracted MMs now eligible (more competition), min pool $1/day, program
  umbrella to Jan 2027.
- Strategy consequences: at-touch lost its multiplier edge → atref (deepest
  full-weight price) at 2x size = same reward, ~half fill exposure, ~2/3
  collateral (imm_shape_sim_v2.py, 90 live books).

## What was built/changed (chronological)

**Gas bot (new: gas_bot.py, gas_data.py, run_gas_bot.ps1, GAS_BOT_HANDOFF.md)**
- Data: AAA scrape (live ~3:18-3:36am ET post time) + Wayback + Kalshi
  settlement-bracket backfill; `gas_data.py validate` = 0 violations.
- Model: print + EWMA drift (walk-forward grid) + per-weekday adjustments
  (Sat −0.41/Sun −0.37 vs Wed/Thu +0.65; h1-3 RMS −7-10%) + sigma power law.
  Caveat: demeaned grid re-picked lam_last=0.6 → hot after surprises; the
  sniper's eff_fair = min(model, pre-print mid + model shift) is the guard.
- Sniper: hot-window 5s polling (03:10-03:45 ET), 60s universe/book
  prefetch, two-wave fire (cache wave ~200ms → fresh wave, shared envelope).
- IOC path live-validated 8/1; auto-fallback to 120s GTC if IOC rejected.

**IMM (incentive_mm.py + tests + launcher)**
- Estimator rewritten to amended rules + per-market counted-snapshot EMA
  (IMM_COVERAGE_EMA_ALPHA). 7/30-8/1 est figures were ~30x inflated (that
  was the rain "underearning" mystery, plus pool cuts: rain $1899 promo →
  $98 → $54.5/day; mention words → $5.90; monthlies dead).
- atref ladder mode + ladder_reference_prices/side_reference_level;
  band-EXEMPT placement both sides (safe: bids only at/below touch, asks
  at/above); ref_depth_mult sizing (+0.1/tick, cap 2x) wired into ladder,
  side_max rooms, placement gate (side_cap AND level_cap = the two
  duplicated-threshold bugs), and collateral reservation (ref_mult_bid/ask
  on MarketMeta — was under-reserving up to ~75% on mid-priced deep-ref
  books).
- Pads: +300 slack (IMM_PAD_SLACK) so external withdrawal between requotes
  can't drop a side under target (the AUG0110-T82.99 lesson).
- diff_orders hysteresis for atref rungs (±1 tick behind-only, ±20% count;
  IMM_ATREF_PRICE_TOL / IMM_ATREF_COUNT_TOL) — kills touch-wiggle churn.
- Rain: 3/side override removed (global ladder); daily cutoff 9pm ET
  day-before (IMM_RAIN_CUTOFF_BEFORE_MIN=180); rain-fair gate + directional
  module untouched (other sessions').
- KXTEMP re-allowed (blocklist entry removed) at 20/side.
- KXRT (Rotten Tomatoes) pulled out of _DEFAULT_ECON_SERIES onto its own
  _DEFAULT_ENTERTAINMENT_SERIES allow track — the 7/29 econ no-new run-off
  had swept it by config placement (SPI-89 $7/day was barred). SPI-89
  quoting again 8/2 ~11:02Z.
- Phase 4 in imm_earnings_overrides.py: broadcast-mention sweep — resolver-
  gated detection of same-day mention events dying at the midnight fallback
  (the KXFOXNEWSMENTION miss), TVmaze exact-match auto-resolve (patchy for
  cable news — expect the email --set fallback), 3x/day on the existing task.
- Test suite: 280 green.

## Incidents & lessons (the "classes")

1. **Duplicated thresholds**: any new multiplier/mode must be applied at
   construction AND the placement gate (side_cap 8/1, level_cap 8/2) AND
   budgeting (collateral 8/2). Grep `sum(s for _t, s in` + every cap when
   touching sizing.
2. **Config-family inheritance**: series placed in a family list for
   convenience inherit later family POLICY (KXRT-in-econ → no_new'd).
   Allow-tracks and policy-tracks are now separate for entertainment.
3. **Override defaults ≠ global**: dropping a series env falls back to its
   SeriesOverride default, not the global (temp 5/2/2 incident).
4. **Restart drill (mandatory)**: Stop-ScheduledTask → wait 20s (the dying
   instance DRAINS its placement wave for minutes) → kill any surviving
   `incentive_mm.py` python → Start-ScheduledTask. Six restarts, six
   orphans. Also: TS can show "Ready" while a detached tree quotes on —
   check processes, not task state; and per-date log redirection binds at
   launcher-iteration start (a bot started Aug 1 writes to the Aug 1 file
   forever — no Aug-2 file ≠ bot down).
5. **Unstamped orders = Jack's manual app trading** (no client_order_id, no
   STP in reads). 21-day scan matched his known manual stream; the 7/27
   6:41-6:42am gas ladder (3.90/4.00/4.40 filled + 4.30 resting) was his.

## Open items / watch list (priority order)

1. **Credits reconciliation** (post-7/30 paid period vs new estimator) —
   gates all further scaling.
2. **Gas sniper next fire** — first run with speed v1.1 + DOW; check the
   wave-1/wave-2 split in the morning email and whether IOCs fill now.
3. **Digest sanity** — first full-day honest est numbers; overnight 2x +
   coverage EMA interplay (one-sided overnight books pay nobody now).
4. **DDR5 decision** — KXDDR5EMS/MS: 780 mkts, ~$27k/day pool, OUTSIDE the
   allowlist. Thin-book pool capture vs index-informed flow; if probed,
   micro-probe sizing. (GPU-rental family also parked, same class.)
5. **Weekly gas settle Monday** — model (momentum, ~60c on >4.110) vs
   market (fade, ~5c); sniper positioned either way; also Jack's manual
   Aug-monthly YES ladder (3.90/4.00/4.40) rides on it.
6. Mention-event curation: LVCHI-style quote-all via
   event_start_overrides.json (value = TRUE start; doubles as cutoff−30min);
   consider a rolling same-day-WNBA rule like next-day rain if wanted.

## Ops quick reference

```powershell
# IMM restart (THE drill)
Stop-ScheduledTask "KL incentive_mm"; sleep 20; <kill surviving python>; Start-ScheduledTask "KL incentive_mm"
# gas
python gas_bot.py --status | --calibrate ; python gas_data.py show --days 7
# IMM offline diagnosis
python -c "...bot._screen(meta, now) / bot._estimate_candidate_yield(meta, [])"
# halt: gas_data\HALT (gas) / run-logs\incentive-mm HALT file (imm)
```

Key files: incentive_mm.py (+test), run_incentive_mm.ps1, gas_bot.py,
gas_data.py, run_gas_bot.ps1, imm_earnings_overrides.py (Phase 4),
imm_shape_sim_v2.py, INCENTIVE_MM_HANDOFF.md, GAS_BOT_HANDOFF.md,
run-logs/incentive-mm/event_start_overrides.json (hot-reloaded).
