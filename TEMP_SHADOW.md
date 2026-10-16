# Hourly-temp shadow test (KXTEMPMIAH)

Jack asked on 2026-10-14: "build it and shadow test for a week, reporting results and suggestion tweaks daily". This follows the 10/14 study of how to make Kalshi's hourly Miami temperature markets profitable (memory: hourly-temp-mm-design).

**What it does:** it records the live market around the clock, then replays a set of candidate quoting policies against that recording. For each policy, every day, it reports:
- what the policy would have earned in liquidity rewards,
- what it would have won or lost on fills,
- the net of the two, plus suggested tweaks.

**What it never does:** it never places, amends or cancels an order. It needs no subaccount and no trading permission. The IMM's `KXTEMP[A-Z]+H` block stays in place.

## Market facts the code relies on

These were verified on 10/14.

**Events and strikes**
- Each event is one hour, e.g. `KXTEMPMIAH-26OCT1409` = the 9:00 AM ET reading on Oct 14.
- Each event has 10 strikes `-T<k>.99`, read as "above k.99 °F". YES wins iff the settlement index > k.99.
- The event is created about 10 minutes before it opens. It opens at S−60 min and closes at S. Expected expiration is S+5 min.
- The market settles on the index value at minute S. This was confirmed on 162 of 162 events.

**Settlement index**
- The index is Kalshi's Weather Index for Miami, built from the 1-minute readings of KMIA1M, KOPF1M, KFLL1M, KFXE1M and KPMP1M.
- Each station reads in whole °C. With all five present, index = 32 + 0.36·S °F, where S is the sum of the five readings.
- With one station missing, the offset rule applies:
  - Offsets: KOPF −0.5 °C, everyone else 0.
  - City reference: B_c = −0.10 °C.
  - The index is the mean of (x_i − b_i + B_c) over the stations present.
  - A minute needs at least 4 stations.
  - Live offsets: `GET /live_data/weather/miami/calibrations`.
- `/live_data/weather/miami/calibrations` and the other live-data paths need no auth.

**Live station feed:** `GET /trade-api/v2/live_data/weather/miami?detailed=true&last_sec=420`
- Returns per-minute rows `{t, status, v, contributors, stations:[{station_id, temp_f, code, source, received_at_ms}]}`.
- Pending minutes appear with status `incomplete`, station `code: "pending"` and no `v`.
- Kalshi receives a minute's readings about 145 s after the minute; KFLL lags, at about 158–205 s.
- The official `v` lands about 305–317 s after the minute.
- History is available with `&from=<ms>&to=<ms>`, going back about 6 weeks.
- The official per-event series: `GET /live_data/events/<EVENT>`.

**Rewards**
- `GET /incentive_programs` returns one program per strike: `period_reward` 1000000 (centi-cents, so $100), `target_size_fp` 1000, `discount_factor_bps` 5000.
- A program starts about 1.5–2.5 minutes after open and ends at close.

**Scoring rules** (the IMM's `_side_share`, incentive_mm.py:15057)
- One snapshot is taken at a random time each second.
- Each side (YES bids, NO bids) is walked from its best price down.
- The reference price is the level where cumulative size first reaches target/5.
- The walk continues until cumulative size reaches the target. The level that crosses the target counts in full.
- Weight is 0.5^(ticks below the reference); everything at or above the reference gets full weight.
- A side that never reaches the target does not qualify.
- A snapshot counts only if BOTH sides qualify. Our share is (YES share + NO share)/2.
- The period payout is scaled by the fraction of snapshots that counted.

**Book locking:** a YES bid at 99 and a NO bid at 1 cross. On a decided strike where someone bids the favorite at 99, the longshot side cannot rest at all, so the strike pays nobody.

**Informed flow:** the burst comes in the 0–2 s after station readings land, in each minute's :20–:48 s window.

## Files

**Code** (repo root, flat like the rest of KL)

| file | role |
|---|---|
| `temp_shadow_common.py` | THE CONTRACT: paths, record shapes, time/price/ticker helpers. Every module imports it. |
| `temp_shadow_collector.py` | Always-on recorder: WS books and trades, the station feed, markets, programs, post-close settlement pulls. |
| `temp_shadow_backfill.py` | One-shot history pull: 6 weeks of station readings plus settled markets and trades for model fitting and calibration. |
| `temp_shadow_model.py` | Index replica plus the fair-value model (`fit` CLI writes model_params.json). |
| `temp_shadow_scoring.py` | Reward scoring (an exact port of the IMM rules) plus book reconstruction from the ws stream. |
| `temp_mm_policy.py` | THE QUOTING POLICY as pure logic (DEFAULT_POLICY, VARIANTS, `decide()` / `quote_market()`, risk sizing, order diff). The sim replays it and the live executor runs it, so shadow numbers describe the code that trades. |
| `temp_shadow_sim.py` | Replay engine: feeds temp_mm_policy the replayed books, fairs and fills; fill models opt / cons / swp, reward accrual, settlement P&L. |
| `temp_shadow_sweep.py` | The sweep-aware fill model (swp, its `VPass`) that the sim runs for every variant, plus its own tools: hazard `calibrate`, `tails`, `validate` on live hours, `run` / `sens` / `table` studies, `export` of a fit. |
| `temp_shadow_sweep_hazard.json` / `temp_shadow_sweep_tails.json` | The swp defaults the sim loads: the 10/16 pre-live hazard fit (37 hours, validated on the live hour 26OCT1610) and the realized-tails table behind `net_cal`. `<TEMP_SHADOW_DATA>\model\sweep_hazard.json` / `sweep_tails.json` override them (Recalibrating swp, below). |
| `temp_mm_live.py` / `run_temp_mm.ps1` | The LIVE executor of the same policy (DRY by default; `--live --subaccount N` is Jack's). It tails this collector's wx stream read-only. Handoff: TEMP_MM_LIVE.md. |
| `temp_shadow_report.py` | Daily report (HTML email) plus the week summary and rule-based tweak suggestions. |
| `run_temp_shadow_collector.ps1` / `run_temp_shadow_collector_hidden.vbs`, `run_temp_shadow_report_hidden.vbs`, `register_temp_shadow_collector.ps1`, `register_temp_shadow_report.ps1` | Windows tasks. |
| `test_temp_shadow_*.py`, `test_temp_mm_policy.py`, `test_temp_mm_live.py` | Tests. They always sandbox `TEMP_SHADOW_DATA` (and `TEMP_MM_DATA`) to a temp dir, never the live data roots. |

**Data:** `C:\Users\jackd\Documents\KL-data\temp-shadow\` (env `TEMP_SHADOW_DATA`)

```
raw/<UTC day>/{ws,wx,meta}_<HH>.jsonl[.gz]   recording (shapes in temp_shadow_common.py)
settled/<EVENT>.json                         results, expiration_value, REST trades, official index
backfill/                                    history pulls (wx/, settled/, iem/, study/)
model/model_params.json                      fitted model
model/sweep_hazard.json, sweep_tails.json    optional swp recalibration (else the repo defaults)
sim/<ET day>/<EVENT>.json                    per-event per-variant results
reports/<ET day>.html                        daily reports
collector_status.json, collector.log, HALT   ops
```

## Collector (always on)

**Discovery**
- Every 30 s: `GET /markets?series_ticker=KXTEMPMIAH&min_close_ts=<now>` (paginate). Upcoming markets sit in status `initialized`, which neither `status=open` nor `/events?status=open` returns.
- Log a `markets` meta record per event when it is first seen and whenever its markets' status/open/close change.

**WebSocket**
- One connection to `WS_URL`, using `kalshi_ws.WSConnection`: stdlib, no websocket package on this box.
- Auth headers are signed exactly like incentive_mm.py:18380, with the FLEET key: `KALSHI_API_KEY_ID` plus `KALSHI_PRIVATE_KEY` / `KALSHI_PRIVATE_KEY_PATH` / Lisa_Kalshi.txt.
- Subscribe channels `orderbook_delta` and `trade` for every open or upcoming KXTEMPMIAH market. Add markets as events appear; delete them about 10 minutes after close.
- Write every message verbatim, `{"rx", "m"}`, plus markers.
- Track the per-sid `seq`, counting `ok` replies too, the same as `kalshi_ws`. On a gap, write a `gap` marker and resnapshot (`get_snapshot` or resubscribe).
- Ping every 10 s. On reconnect, use capped exponential backoff and write a `disconnect`/`connect` marker.

**Station feed**
- `last_sec=420`. Poll every 1.0 s while second-of-minute ∈ [13, 50], otherwise every 5 s (`TEMP_SHADOW_WX_LO`/`WX_HI`). The 4th station lands at :13–:17 in 13% of minutes. Hard cap: 1.25 requests/s.
- Unsigned. Unsigned requests appear to share one per-IP budget of about 2 requests/s (inferred from the 429 pattern). On HTTP 429, back off (2 s, 4 s … 60 s); after any station 429, the fast cadence drops to 2 s for 2 minutes. The REST thread waits 2 s between requests, 4 s while the station window is open.
- **No signed fallback by default** (`TEMP_SHADOW_SIGN_ON_429=1` re-enables it). The fleet key is ALSO the IMM's key, so signed polling would draw on the IMM's own rate budget.
- Write a `wx` row whenever a minute's row differs from the last one seen.
- Write a `poll` meta row each minute with: n, n429, nerr, latency p50/max, signed.

**Programs**
- Every 60 s from hh:00 to hh:06, every 10 min otherwise.
- `GET /incentive_programs?status=active&limit=1000` (paginate), keeping KXTEMPMIAH tickers.
- Write a `programs` meta row when the set changes.

**Settlement pull**
- Each event, once its close is ≥ 7 minutes past, retried every 2 min for up to 2 h until every market has `result`.
- Fetch markets (`/markets?event_ticker=`), trades per market (`/markets/trades?ticker=&limit=1000`, paginate), and `/live_data/events/<EVENT>`.
- Write `settled/<EVENT>.json` atomically.

**Ops**
- Heartbeat to `collector_status.json` every 10 s.
- At each UTC-hour roll, gzip raw files older than 2 h.
- The single instance is enforced through `collector.lock`, a PID check, and a refusal to start if another live PID holds it (exit 4; the launcher then quits).
- A restart costs about 20–30 s of recording. Raw data is about 15 MB/hour uncompressed, about 0.6 MB/hour gzipped.
- `HALT` exits cleanly.
- Raw-file lookups create no folders (since SIM_VERSION 6, 10/16). `temp_shadow_common.raw_file` / `raw_files` / `iter_raw` only compute paths. Every writer creates its folder first: the collector's `RawWriter`, `append_jsonl` / `write_json_atomic`, or `raw_file(..., create=True)`. Before this, every probe created `raw/<UTC day>/`: the sim's `day_events` reads 3 h past its day, and the sweep's `recorded_events` probed too. That left empty future-day folders in the recording (10/16: `raw/2026-10-06`..`13` and `2026-10-17`..`25`, all empty and safe to delete). The running collector keeps the old module until its next restart. Its writes are unaffected, because `RawWriter` makes the folder itself.
- Exits at `SHADOW_UNTIL_UTC`.
- Total REST load is about 0.6 requests/s.
- Never imports incentive_mm.py.

## Model

**Replica:** compute the index per minute from station rows, using the plain mean when all five are present and the offset rule otherwise. Check it against the official `v`. The report tracks the match rate.

**State at time t:** only data with `rx ≤ t`, which is look-ahead safe.
- The latest minute with ≥4 stations received (pending allowed) gives `I_last` at minute `m_last`.
- Plus per-station recent history.

**Fair at t** for settlement minute S, with τ = S − m_last in minutes:
- μ = I_last + a·drift(hour_ET_standard, month, τ) + b·trend15·τ.
- σ = σ(τ, hour bucket, season), with a fat-tail mixture.
- P(YES_k) = P(index_S > k.99), mapped onto the 0.36 °F lattice with a continuity correction (`sum_threshold`).

**Fit:** `python temp_shadow_model.py fit` uses the backfilled Kalshi station history (the actual whole-°C feed) for σ(τ) and trend. The IEM long-run 1-minute climatology (backfill/iem/) gives drift by hour and month. Output: `model/model_params.json`.

The model is a guard and a classifier, not the price. Quotes sit at the book's reward reference price; the fair only caps sides and labels strikes live or decided. Its Brier score against the market mid is reported daily.

## Simulator

The simulator replays one event at a time.

**Shared per-second book states:** rebuilt from the ws stream (snapshot plus deltas; untrusted after a gap until the next snapshot). These are computed once and shared by every variant.

**Timeline**
- 1-second ticks run from open to close.
- Weather observations are applied at `rx + REACT` (default 0.5 s).
- Trades come from the ws `trade` channel. REST trades in settled/ are the fallback when ws coverage has a hole.

**Quotes**
- The policy recomputes quotes on every tick and immediately after a weather update, after the reaction latency.
- A replaced order loses its queue place. Its new queue-ahead = the book size at that price at placement time (the book excludes us).

**Reward:** on each tick inside the market's program window, overlay our orders on the book and score them (`scoring.share`). Accrue `pool_per_sec × share`.

**Fills**
- A trade with `taker_side=yes` lifts NO bids, i.e. YES asks. Our NO bid at q (YES ask 100−q) fills when the trade's YES price is above 100−q.
- At an equal price:
  - `opt`: we fill up to count.
  - `cons`: count first consumes queue-ahead.
- `taker_side=no` hits YES bids, symmetrically.
- Trade prices are the YES price (`yes_price_dollars`, or `yes_price` cents on ws).

**Settlement and P&L**
- Fills settle at the result or expiration_value, with maker fee 0 (configurable).
- Mark-outs are taken at +60 s and +300 s against the book mid.

**Risk limits:** per-market position cap, plus a per-event worst-case-loss cap across the nested strikes.

**Fill models:** `opt`, `cons` and `swp` run as separate passes, because fills change inventory. Jack, 2026-10-16 ("1"): "ship the fixes + the sweep-aware report column".
- `swp` is cons plus the sweepers who take our 1c orders. The model is `temp_shadow_sweep`'s, described under "Fill model swp" below: recorded 1c taker clusters with price-time priority (rec_mode `level`), hidden IOC sweeps at the calibrated hazard while we rest alone at 1c, and new 99c bids that cross us. It is the only model that reproduced live G's first hour (26OCT1610).
- `run_event` / `run_day` write `"<variant>|swp"` for every variant, with the opt/cons schema holding the means over the draws. They add `draws`, `net_sd`, `net_se` (the Monte Carlo standard error of `net`), `draw_net`, `filler_contracts`, `sweep_src`, `haz_events` and `swp_exact`. They also add `net_cal` / `net_cal_tlo` / `net_cal_thi`: reward plus every fill marked at the realized longshot rate of its model bucket given a taker hit a bid at that price (an ESTIMATE; "Realized tails" below). The top-level `"swp"` block names the draws, seed, rec_mode, channels and the hazard / tails ids and files.
- **Monte Carlo:** `SWP_DRAWS` = 6 draws per event (env `TEMP_SHADOW_SWP_DRAWS`), with fixed seeds (`tmm-sweep-1`). Draw k's hidden-sweep candidates depend only on (event, k), so every variant meets the same arrivals (common random numbers) and a re-run gives the same numbers. Draw k is also the standalone module's draw k.
- **Exact shortcuts**, which `test_temp_shadow_sim` checks against running every draw in full:
  - A variant whose first draw never rests a 1c order is the same in every draw (`swp_exact`: FAV_only, FAV_night).
  - A variant whose first draw accepts no hidden sweep logs its 1c trajectory, and a later draw is run only if one of its candidates would be accepted along it (`temp_shadow_sweep.hazard_hits`). This covers the live-only variants, whose qualification pads rarely meet the hazard.
- **Runtime** (measured 10/16 on a copy of the recording, `run_day` at the report task's 2 workers, nothing else running): a 22-event day (10/15) took 398 s at 6 draws and 485 s at 8, against about 70 s for opt / cons alone. 10/16 (18 settled events) took 331 s at 6 draws. That is about 18 s of wall time per event at 6 draws: a 24-event day in about 7.5 minutes, and the longest window (10/15-10/23 = 200 events, re-simmed after a SIM_VERSION, grid or swp-table change) in about 61 minutes, against the task's 2 h limit (8 draws: about 73 minutes). Hence 6 draws. The Monte Carlo se of a 24-event day of a 1c-filler variant is then about $25 (G0_nohold: $33 over the 43 recorded hours); FAV_* have none.
- **Cache:** `run_day` reuses a sim file only when its swp block matches the current draws, hazard id, tails id, rec_mode, channels, seed and sweep version. A recalibration or a new draw count re-sims the report's whole window, so the cumulative figures never mix tables.

**Recalibrating swp.** The sim loads `<TEMP_SHADOW_DATA>\model\sweep_hazard.json` when it exists, else the repo's `temp_shadow_sweep_hazard.json`. Tails work the same way, with `sweep_tails.json` / `temp_shadow_sweep_tails.json`. To refit on newer hours:
```
python temp_shadow_sweep.py --out <dir> calibrate                  # <dir>\hazard.json (+ hazard_prelive.json while live hours exist)
python temp_shadow_sweep.py --out <dir> validate --live --hazard <dir>\hazard_prelive.json   # check it on the live hours first
python temp_shadow_sweep.py export --hazard <dir>\hazard.json --to <TEMP_SHADOW_DATA>\model\sweep_hazard.json
python temp_shadow_sweep.py --out <dir> tails                      # optional
python temp_shadow_sweep.py export --tails <dir>\tails.json --to <TEMP_SHADOW_DATA>\model\sweep_tails.json
```
`export` keeps exactly what `Hazard` / `TailCal` read, in order (the size draws are indexed, so their order is part of the fit). It prints the table's id, which every sim file then records. The next report re-sims its window under the new table, up to about an hour for 9 days at 2 workers. Deleting the file goes back to the repo default.

### Policy and variants

These are all parameters of one policy, so a tweak is a config change.

**Live strikes** (fair 10–90¢)
- Rest per side `Q_live·g(τ)` at the side's reward reference price, never crossing.
- Cap the price at fair ± buffer, where buffer = max(1¢, k·φ(z)/σ·0.36·100).
- Stop at τ < `live_stop_min`.

**Landing window**
- Second-of-minute ∈ [`win_lo`, `win_hi`], default 20–45.
- Mode `pull` (no live quotes) or `back` (2 ticks behind).

**Decided strikes** (if enabled)
- On the longshot side, rest `Q_pad` at 1¢ when it can rest.
- On the favorite side, join the reference with `Q_fav` when the favorite's fair ≥ `fav_min_p`, capped by fair − buffer.
- Near-decided strikes (3–10% / 90–97%) are handled as live at half size.

**Qualification pads:** a 1¢ order that lifts a thin side to 1,000 contracts when the side can rest it.

**Skew** (if enabled): in the 5–30¢ and 70–95¢ zones, scale the NO-bid side ×(1+s) and the YES-bid side ×(1−s), because YES is overpriced there.

**Quoting window:** quote only inside the program window.

**Quote hours** (`quote_hours_et`, added 10/16 for FAV_night). This is a list of ET clock hours, `None` = every hour (the default). An event is quoted only when the ET hour of its SETTLEMENT (the ticker's hour: `KXTEMPMIAH-26OCT1607` settles at 07:00 ET) is listed. Outside them nothing rests on any of its markets: `temp_mm_policy.decide` fails every market's program test (`reason` `off_hours`), and the sim applies the same rule per event. `temp_mm_policy.et_hour` is DST-safe without tz files and matches `zoneinfo` hour by hour over 2024-2030. On the 2026-11-01 fall-back, the 07:00 event (12:00Z, EST) is quoted and 08:00 (13:00Z) is not, and the repeated 01:00 is the ticker's first (05:00Z). The live executor passes the same settle time (TEMP_MM_LIVE.md, Quote hours).

**Pad hold** (`pad_hold_open_s`, added 10/16). Jack, 2026-10-16: "hold the pads for the first 3 minutes of each hour going forward". In live G's first hour (26OCT1610, 13:02Z) sweepers took 4,659 of its 1c fillers within about 45 s of the program opening.
- While the orders would start resting before the event's open + `pad_hold_open_s`, no 1c longshot pad and no 1c qualification pad rests. Favorite joins, live and near-decided quotes are unaffected (a near quote that sits at 1c stays).
- The open is the markets' `open_time` (close − 3600 when unknown). The test is `temp_mm_policy.pad_hold_flag` on the orders' resting start (decision time + 0.15 s), the same rule the live executor applies. In the sim the pads are active by open + 180.65 s (the hh:03:00.5 tick, or an earlier station-row decision).
- Default 0 (off). Only G holds (180 s); `G0_nohold` is the G before the hold, kept for comparison.

**Variant grid:** the grid is in `temp_mm_policy.VARIANTS` (re-exported as `temp_shadow_sim.VARIANTS`). Two variants were added on 10/16 (Jack: "1", "add favorite-only and overnight-favorite-only to the shadow"). `FAV_only` = live off, decided on, `q_pad` 0, `qual_pads` off, `near_decided_half` off: G's favorite joins and nothing at 1c (the sweep study's "favorite joins only"). `FAV_night` = FAV_only with `quote_hours_et` [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7], i.e. the events settling 20:00-07:00 ET, where the sweep study found almost all of its expectation. Every earlier variant is unchanged: on the recording their opt / cons results are bit-identical to SIM_VERSION 5 (Daily pipeline swp, below). SIM_VERSION 6 (swp for every variant, the two FAV variants) re-sims every cached day the report asks for. Since 10/16 `G_decided_only` (what runs live) holds its pads for 180 s and `G0_nohold` is the old G. SIM_VERSION 5 makes `run_day` re-sim the older cached files of any day it is asked for; the daily report asks for every day of its cumulative window (day 1 .. the report day), so the first report after a SIM_VERSION or `VARIANTS` change re-sims the whole window (review fix 10/16: before it only the report day re-simmed, so 10/15 stayed at SIM_VERSION 4, where `G_decided_only` had no hold and `G0_nohold` did not exist; the first report run with the fix re-sims it to 5). SIM_VERSION 5 also covers the live-order subtraction from the sweep-aware shadow (merged with it, see below): the 10/16 files simmed at 4 without it re-sim too. The sweep-aware shadow runs the same pair as `G_hold180` (= `G_decided_only`) and `G` (= `G0_nohold`).

## Daily report (08:30 ET, previous ET day, plus cumulative)

**Variant table:** reward, fills, contracts, trading P&L and net per variant, for `opt`, `cons` and `swp` side by side. The swp group adds the Monte Carlo standard error, the 1c filler contracts swept, and "expected (calibrated tails)" = `net_cal`, with its caveat that it is an estimate and circular for 10-90% fills. A one-line note explains swp. **The headline is the best `swp` variant** ("Chosen on swp net"), with the best cons / opt variants beside it. The email subject says so: `best FAV_night net +$X/day on swp (reward ..., fills ...)`. Sim output from before SIM_VERSION 6 has no swp, and the report then falls back to cons and says so (`headline_fill`).

**Headline-variant breakdowns:** by hour of day, strike class, τ bucket and side, plus worst events.

**Market structure**
- % of strike-seconds paying without us.
- % locked at 99.
- Our average share.
- Competitor depth.

**Model:** Brier and log loss against the market mid by τ bucket, replica match rate, and calibration.

**Data health**
- Collector uptime.
- WS reconnects and gaps.
- Poll latency and 429s.
- Our observation lag versus Kalshi's `received_at`.
- Missing events or hours.

**Suggested tweaks:** rule-based, from the variant deltas and diagnostics, compared on **swp**. The noise gates are unchanged; the per-event spread they use now includes the Monte Carlo noise. The fill-model rule is now the sweep gap (cons - swp on the best variant: what sweepers take). The losing-hours rule on a variant without live quotes (G, FAV) proposes dropping those settle hours through `quote_hours_et` instead of halving a live size. The decided-strikes rule measures decided strikes alone on `G0_nohold` (G without the pad hold) when the window has it, else on G with its hold named. The pad-hold rule pairs `G_decided_only` with `G0_nohold` (G-G0) on the events both ran on.

**Sim freshness:** the report re-runs `run_day` for every window day (cached files from the current SIM_VERSION with the current overrides are only loaded). A window file still from an older SIM_VERSION (a `--no-sim` report, a failed re-sim) is flagged at the top of the report and in the subject.

**Cumulative table:** per variant, the swp sum, per day, days positive, Monte Carlo se and expected-at-calibrated-tails sum, then cons and opt sums. The day columns are swp net.

**Week summary:** on and after day 7, with a go/no-go and a recommended starting configuration. The GO / NO-GO is judged on the best **swp** variant, and its risks line gives swp (+/- Monte Carlo se), cons, opt and the expected-at-calibrated-tails figure.

**Delivery:** emailed via `send_html_email.py` (ALERT_EMAIL_* env). The HTML is kept in reports/.

## Tasks

| task | trigger | does |
|---|---|---|
| `KL temp-shadow collector` | at logon (+1 min), and every 10 min with IgnoreNew, no time limit | `run_temp_shadow_collector.ps1` restart loop |
| `KL temp-shadow report` | daily 08:30 ET, StartWhenAvailable, 2 h limit | sim the previous ET day and re-sim any stale window day, build the report, email it. With swp, a full window re-sim after a SIM_VERSION, grid or swp-table change takes about 7.5 min a day at 6 swp draws and 2 workers, about 61 min for the longest window (10/15-10/23, 200 events; measured 10/16) |

To stop the test:
- Create `KL-data\temp-shadow\HALT`.
- Run `Unregister-ScheduledTask -TaskName 'KL temp-shadow collector','KL temp-shadow report' -Confirm:$false`.

## Interfaces (the build contract between modules)

Prices are integer cents in their own book's space. Book levels are `Dict[int, float]`, mapping cents to size. Times are epoch seconds.

```python
# temp_shadow_scoring.py
def reference_price(levels: Dict[int, float], target: float) -> Optional[int]
    # best-first walk; the level where cumulative size >= target/5; None if never
def side_share(levels: Dict[int, float], own: Dict[int, float],
               target: float, df: float) -> Tuple[float, bool]
    # levels INCLUDE own. Exact port of incentive_mm._side_share (15057)
def score(book_yes, book_no, own_yes, own_no, target=1000.0, df=0.5
          ) -> Tuple[float, bool, bool, bool]
    # book_* EXCLUDE ours (we overlay). (frac, counted, yes_qual, no_qual);
    # frac = (s_yes + s_no)/2 if counted else 0.0
def would_cross(book_yes, book_no, side: str, price: int) -> bool
    # side 'yes' (a YES bid at price) crosses iff price + best_no >= 100
Trade = namedtuple("Trade", "ts rx ticker taker_side yes_cents count")
    # taker_side 'yes' = the taker BOUGHT YES (lifted YES asks = NO bids)
class EventStream:            # load_event_stream(event, t0, t1)
    tickers: List[str]
    def books_per_second(self, sec0: int, sec1: int
        ) -> Dict[str, List[Optional[Tuple[Dict[int,float], Dict[int,float]]]]]
        # entry i = book as of rx <= sec0+i+0.5; None = untrusted/unknown
    trades: List[Trade]       # ws trade channel (REST settled trades fill ws holes)
    coverage: dict            # trusted_frac per ticker, gaps, reconnects
def load_event_stream(event_ticker: str, t0: float, t1: float) -> EventStream

# temp_shadow_model.py
def replica_index(stations_f: Dict[str, float], offsets_c: Dict[str, float],
                  b_city_c: float) -> Optional[float]   # None if < 4 stations
class WxTape:                 # WxTape(rows) - wx stream rows, any order (sorted by rx inside)
    def snapshot(self, at_rx: float) -> "WxSnapshot"    # monotone calls are O(new rows)
class WxSnapshot:
    latest_minute_ms: Optional[int]  # latest minute with >= 4 stations received by at_rx
    index_latest: Optional[float]    # replica index at that minute
    history: List[Tuple[int, float]] # (minute_ms, index) for the last <= 60 min with >= 4 stations
    stations_latest: Dict[str, Tuple[int, float]]  # station -> (minute_ms, temp_f)
class FairModel:              # FairModel(params=None) loads MODEL_PARAMS_FILE (defaults if absent)
    def dist(self, snap: WxSnapshot, settle_ts: float) -> Optional[Tuple[float, float]]  # (mu_F, sigma_F)
    def fair(self, snap: WxSnapshot, settle_ts: float, strikes: List[float]
             ) -> Dict[float, float]      # P(YES) per strike; {} if no data
# CLI: python temp_shadow_model.py fit   |   python temp_shadow_model.py replica-check --day D

# temp_shadow_sim.py
DEFAULT_POLICY: dict; VARIANTS: Dict[str, dict]   # each variant = overrides of DEFAULT_POLICY
FILL_MODELS = ("opt", "cons", "swp")
def run_event(event_ticker, variants=None, fill_models=FILL_MODELS, model=None,
              until_ts=None, draws=None) -> dict          # draws: swp Monte Carlo draws (SWP_DRAWS)
def run_day(et_day: str, variants=None, force=False) -> List[dict]  # writes sim/<day>/<EVENT>.json
# CLI: python temp_shadow_sim.py day --day YYYY-MM-DD [--force] | event --event EVT
```

### Sim output: `sim/<ET day>/<EVENT>.json`

```json
{"event": "...", "open_ts": 0, "close_ts": 0, "settle_ts": 0,
 "settled": true, "settlement_value": 81.68, "results": {"<ticker>": "yes"},
 "coverage": {"book_trusted_frac": 0.0, "wx_rows": 0, "trades": 0, "trades_src": "ws|rest|mixed",
              "reconnects": 0, "gaps": 0, "program_secs": 0},
 "market_stats": {"strike_secs": 0, "paying_frac_without_us": 0.0, "locked99_frac": 0.0,
                  "live_frac": 0.0, "decided_frac": 0.0, "depth3c_median": 0.0},
 "model_eval": {"n": 0, "brier_model": 0.0, "brier_mid": 0.0, "logloss_model": 0.0, "logloss_mid": 0.0,
                "by_tau": {"0-5": {"n": 0, "brier_model": 0.0, "brier_mid": 0.0}}},
 "variants": {"<variant>|<fill>": {
     "reward": 0.0, "fills_n": 0, "contracts": 0.0, "trading_pnl": 0.0, "fees": 0.0, "net": 0.0,
     "avg_share": 0.0, "quoted_secs": 0, "max_abs_pos": 0.0, "worst_case_loss": 0.0,
     "by_class": {"live": {"reward": 0.0, "contracts": 0.0, "pnl": 0.0}, "decided": {}},
     "by_tau": {"0-5": {"reward": 0.0, "contracts": 0.0, "pnl": 0.0}, "5-10": {}, "10-20": {}, "20-40": {}, "40-60": {}},
     "by_side": {"yes_bid": {"contracts": 0.0, "pnl": 0.0}, "no_bid": {}},
     "fills_after_wx": {"contracts": 0.0, "pnl": 0.0},
     "markouts": {"m60": 0.0, "m300": 0.0}}}}
```

- `by_tau` buckets are minutes to close at the time of the fill or reward. Model eval samples once a minute per strike.
- `fills_after_wx` counts fills within 3 s after a weather update changed the replica index.
- `trading_pnl` is settled P&L of the fills in dollars. `net = reward + trading_pnl - fees`.
- `"<variant>|swp"` (since SIM_VERSION 6) holds the same keys as means over its Monte Carlo draws, plus `draws`, `net_sd` / `reward_sd` / `trading_pnl_sd` / `contracts_sd` / `filler_contracts_sd`, `net_se` (= net_sd / sqrt(draws)), `draw_net` (per draw), `swp_exact`, `filler_contracts` (1c), `haz_events`, `sweep_src` (`rec` / `haz` / `cross` contracts), `pnl_cal`, and `net_cal` with `net_cal_tlo` / `net_cal_thi`. The file adds `"fill_models"` and `"swp": {"draws", "rec_mode", "channels", "seed", "hazard_id", "hazard_src", "hazard_built", "lam_max", "tails_id", "tails_src", "tails_built", "sweep_version"}`.

### Verified live formats (collector, 2026-10-15)

```json
{"type":"trade","sid":2,"seq":1,"msg":{"trade_id":"...","market_ticker":"KXTEMPMIAH-26OCT1421-T81.99","yes_price_dollars":"0.8700","no_price_dollars":"0.1300","count_fp":"2.27","taker_side":"yes","taker_outcome_side":"yes","taker_book_side":"bid","is_block_trade":false,"ts":1792023615,"ts_ms":1792023615799},"sending_ts_ms":1792023615805}
{"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{"market_ticker":"...T76.99","market_id":"...","yes_dollars_fp":[["0.0100","810.00"]]}}
{"type":"orderbook_delta","sid":1,"seq":11,"msg":{"market_ticker":"...T84.99","market_id":"...","price_dollars":"0.9800","delta_fp":"-20.00","side":"no","ts":"2026-10-15T00:20:03.029Z","ts_ms":1792023603029}}
```

**Message fields**
- The trade channel has no integer `yes_price` or `count`.
- `ts` is epoch seconds on trades but an ISO string on deltas.
- A snapshot omits an empty side.

**Trades**
- Each maker fill arrives as its own trade message, at that maker's price.
- The book deltas for a fill arrive BEFORE its trade messages.
- `taker_side:"yes"` consumes NO bids.

**Subscription sequencing**
- One subscribe returns two sids (one per channel), each with its own seq starting at 1.
- The `ok` for add/delete takes a seq on both channels.

**Record-shape additions** beyond temp_shadow_common
- wx rows carry `config_version`.
- Settled files add `complete`, `index_details` and sometimes `index_error`; backfill files add `source`.
- Gap markers add `sid`, `channel`, `expected` and `got`.
- A book-channel resubscribe is written as a `gap` marker with `resubscribe: true`, so books are treated as untrusted. A trade-channel resubscribe uses the `resubscribe` marker.
- Poll rows add `t`, `minute`, `n_signed` and `city`.

### Backfill on disk (2026-10-15)

- `backfill/wx`: 177 chunks, 2026-09-01 to 2026-10-15T01:14Z, 63,306 minutes (129 missing, longest hole 9 minutes).
- `backfill/settled`: 492 events from 26SEP2322 to 26OCT1421, with 273,849 trades. Kalshi listed no event for 12 of those hours.
- `backfill/calibrations.json`.
- `backfill/study`: the 10/14 study data.

## Sweep-aware shadow (2026-10-16)

Jack 2026-10-16: "run the sweep-aware shadow, without pausing". In the live executor's first hour (variant G, subaccount 2, KXTEMPMIAH-26OCT1610) sweepers took our 1c pad / qual fillers within seconds. Sweepers are takers buying a near-certain favorite at 99c, which hits 1c longshot bids. Over the hour 10,006 contracts filled at 1c and 6 strikes capped at pos_cap 1,500, after which those strikes no longer qualified. The hour earned an estimated $14 of reward and lost $96 on fills, a net of -$82. The shadow had said +$7 (opt) / +$13 (cons) for that hour. The executor has been on its HALT file since 14:00Z, so 26OCT1610 is the only live hour.

**Why the shadow missed it.** opt / cons fill us only from recorded trades, and sweepers in the recording only took the 1c liquidity that existed. On strongly decided strikes the longshot 1c level was usually empty (the other makers do not rest there), so the shadow saw our pads rest alone, take the whole side's reward share and never fill.

### Live-order subtraction (temp_shadow_scoring)

From 2026-10-16 13:00Z the recorded books contain our own live orders. `load_event_stream(..., subtract_live=None)` (default on; `TEMP_SHADOW_SUBTRACT_LIVE=0` turns it off) rebuilds our resting orders per second from the executor's `orders_` / `fills_<UTC day>.jsonl` and subtracts them from every recorded level (clipped at 0). It reads `mode == "live"` rows only: place / adopt / amend / renew (total) / decrease (reduce_by) / cancel / gone / expired. Fills reduce what is left, and an order leaves at its `expiration_ts` unless renewed. The trades that filled our orders are dropped from the replay tape (kept in `EventStream.own_trades`). `live_own_orders(event, sec0, sec1)` exposes the reconstruction.

The executor's logs are found at `TEMP_MM_LIVE_DIR`, else `TEMP_MM_DATA` (where temp_mm_live writes when relocated), else `KL-data\temp-mm`. That last default applies only while `TEMP_SHADOW_DATA` is the real recording, so a sandboxed test replay never reads the real executor's logs. Before this, the e2e tests on future-dated events would have started failing the day the live executor traded those hours.

With no live rows for an event it is a no-op. On all 35 recorded settled hours before live (26OCT1421 through 26OCT1609) the books, trades, coverage and full sim are identical to origin/main. On 26OCT1610 it removes 186 orders and 21 fills (10,108 contracts).

**What it does not remove.** The subtracted book is the recorded market minus our orders. It is not a market that never saw us. On 26OCT1610 other makers joined five 1c levels that our pads had opened, adding +200 to +800 within 2-12 s, and they stay in the subtracted book. A plain replay of the live hour therefore has company at 1c on those sides, where every pre-live replay has none. `validate` also runs the joiner-free replay (swp_nojoin below).

### Sweep hazard (temp_shadow_sweep.py calibrate)

Measured over the 38 recorded settled hours (26OCT1421 to 26OCT1612), with books and tape NOT subtracted, so anyone's 1c liquidity counts. A sweep is a taker cluster of 1c prints on one side (prints within 1 s). The hazard is sweeps per second while at least 1 contract rests at 1c on that side.

| favorite fair | exposure h | sweeps | lambda /s | mean wait | cleared level | median size |
|---|---|---|---|---|---|---|
| 0.90-0.97 | 25.0 | 213 | 0.0024 | 423 s | 41% | 15 |
| 0.97-0.99 | 14.7 | 236 | 0.0045 | 224 s | 39% | 21 |
| 0.99-0.995 | 6.0 | 119 | 0.0055 | 181 s | 33% | 33 |
| > 0.995 | 17.2 | 618 | 0.0100 | 100 s | 37% | 25 |

The age of the 1c liquidity drives the hazard. On a > 99.5% favorite, liquidity 0-5 s old is swept at 0.138/s, 5-30 s at 0.026/s, 30-120 s at 0.013/s and after 2 minutes at about 0.0065/s. The fitted model is `a[fav][age] x mult[minute]`, IOC sweeps only (the ~7% that left a 99c bid behind are replayed from the tape instead). The minute multiplier is 0.8 between minutes 5 and 40 and 1.2-2.3x over the last 20 minutes.

**Flash sweeps.** 74 of the 107 fresh-liquidity sweeps on > 99.5% favorites are "flash": the 1c liquidity appeared and was swept between two 1 s samples, so the samples saw none of its exposure. Each flash episode now counts 0.5 s of exposure, and its cleared flag reads only the level after it (empty = cleared) instead of being automatic. That gives 0.138/s. Without the flash sweeps the rate is 0.045/s. The first pass reported 0.157/s and an 84% clear rate; the corrected clear rates are 68% (flash) and 58% (non-flash). The flash-free fit (`a[>0.995][0-5s]` 0.039 against 0.119) is carried in hazard.json and run as the "noflash" sensitivity.

**Live evidence (26OCT1610).** 96 of our orders rested at 1c and 14 were hit. Time from placement to first fill was 2.8, 3.2, 6.4, 13, 14, 15, 17.5, 31, 85, 101, 111, 119, 183 and 314 s. Resting alone at 1c on a > 99.5% favorite we were hit 2 times in 13 s (0.15/s). With others also at 1c we were hit at 0.0008 / 0.0033 / 0.0092 / 0.0115 per s by favorite bucket, against a recorded 0.0024 / 0.0045 / 0.0055 / 0.0100. Both agree within the noise of 1-7 hits. The fresh pads that were swept went whole (1,000 / 500 / 1,800 / 1,200 lots).

### Fill model swp

swp is cons plus three channels on our 1c orders (module docstring):
- **(a) recorded taker clusters.** A recorded taker cluster at 1c that cleared the recorded level takes all of ours. One that did not clear it now meets price-time priority: the external size that rested at 1c when our lot was placed, clamped to the level at every sample since, is ahead of us. The first pass put us at the front of the queue, and that rule is kept as rec_mode "front".
- **(b) hidden IOC sweeps.** While we rest alone at 1c, hidden IOC sweeps arrive at the calibrated hazard. The draws are Monte Carlo with fixed seeds, 30 per variant, with common random numbers across variants.
- **(c) new 99c bids.** A new 99c bid opposite our 1c would have crossed us, so it fills us.

Re-pads after a sweep follow the policy, so pos_cap limits them and a capped strike stops qualifying. In a live hour, the cluster sizes in (a) include the prints that hit our live orders. A cluster made only of our prints is kept if others rested at 1c, and dropped if we were alone (then (b) has to predict it).

The live hour supports each queue rule on some clusters. A 1,075-lot taker filled 736 of ours behind 339 that were there first, which is exactly the queue rule. But 842 of a 1,000-lot taker went to us with 1,758 or more resting on the level, so we were nearly first; makers there re-place constantly. On another cluster others were filled ahead of an order of ours that had opened the level. The queue rule fills fewer 1c contracts, so it is the default that is kinder to the fillers.

### Validation on the live hour (26OCT1610)

`validate --live` runs every settled hour the executor traded; 26OCT1610 is the only one. The hazard here is fit on the 37 pre-live hours only (`hazard_prelive.json`), 30 draws, variant G:

| | actual | opt | cons | swp (queue, default) | swp front | swp joiner-free | joiner-free, hazard x1.5 | swp strict |
|---|---|---|---|---|---|---|---|---|
| filler contracts | 10,006 | 3,905 | 3,264 | 9,271 | 10,296 | 7,354 | 8,177 | 7,322 |
| reward $ | 14.11 | 45.19 | 45.23 | 29.90 | 15.21 | 36.16 | 30.60 | 45.38 |
| trading P&L $ | -95.99 | -37.93 | -32.64 | -92.71 | -102.96 | -76.54 | -84.77 | -73.22 |
| net $ | -81.88 | +7.26 | +12.58 | -62.80 | -87.75 | -40.38 | -54.17 | -27.84 |

- "front" (we first in the queue) fits the live hour best. The queue rule misses T86.99: it fills 17 of the 857 contracts that filled live, so that pad keeps earning reward in the replay ($30 against $14).
- "joiner-free" replays the five sides our pads opened (T79.99 / T80.99 / T81.99 NO, T87.99 / T88.99 YES) with nobody else at 1c and drops their 11 recorded 1c takers. That is the regime of every pre-live replay, so it is the out-of-sample test of the hidden-sweep hazard. It under-predicts: 3,337 contracts at minute 5 against 4,659 live, and 7,354 for the hour. At hazard x1.5 it reaches 4,795 at minute 5 and 8,177 for the hour. Our fresh pads went whole, while the corrected size draw clears only 58-68% of the time.
- On this one hour, then, the default swp is if anything kind to the fillers.
- opt / cons see none of it: +$7 / +$13 against -$82.

### Realized tails, and why the sweeps matter (temp_shadow_sweep.py tails)

The swp totals are dominated by a few settlements. A 1,500-lot longshot pays about $1,485 once in dozens of hours, so the Monte Carlo error next to a total says nothing about the outcome risk. `tails` measures the outcome side on every settled hour with station data: 492 backfill hours (26SEP to 26OCT1421, the model's tape) plus 38 recorded hours, 530 hours and 318,000 strike-minutes in all.

**Unconditional.** The model's longshot probabilities are fat by design. Realized longshot rates by model bucket, with event-clustered 90% CIs:

| model longshot P | model | realized | 90% CI |
|---|---|---|---|
| < 0.1% | 0.009% | 0.047% | 0.001-0.111% |
| 0.1-0.3% | 0.19% | 0.18% | 0.02-0.37% |
| 0.3-0.5% | 0.39% | 0.20% | 0.05-0.40% |
| 0.5-1% | 0.73% | 0.44% | 0.24-0.67% |
| 1-2% | 1.45% | 0.76% | 0.46-1.13% |
| 2-3% | 2.46% | 1.57% | 1.05-2.19% |
| 3-6% | 4.30% | 2.72% | 2.08-3.39% |

Per distinct strike at its first crossing of a favorite level, the model is about right: 23.1 expected upsets against 23 realized at 0.99, and 11.7 against 12 at 0.995. The exception is the far tail: at 0.999 it expects 2.4 and 8 happened (P = 0.004). The review's point stands. The 37 recorded hours alone cannot pin the tail; their realized rate rests on one or two upset strikes.

**Conditional on the fill.** This is what decides the fillers. The question is how often the longshot won when a taker actually hit a bid at that price. The data are every REST trade in those 530 hours, contract-weighted with event-clustered CIs:

| model longshot P | a 1c bid hit (a sweeper buys the favorite at 99c) | unconditional | a 97-99c favorite bid hit (a taker buys the longshot) |
|---|---|---|---|
| < 0.1% | 0.026% (480k contracts) | 0.047% | 0.032% |
| 0.1-0.3% | 0.18% | 0.18% | 0.008% |
| 0.3-0.5% | 0.08% | 0.20% | 0.005% |
| 0.5-1% | 0.77% [0-2.2] | 0.44% | 2.4% [0.06-6.7] |
| 1-2% | 0.15% [0.04-0.32] (307k) | 0.76% | 0.25% |
| 2-3% | 0.19% [0-0.48] | 1.57% | 0.79% |
| 3-6% | 0.72% [0.10-1.49] | 2.72% | 0.51% |

Sweepers buy the favorite when it is safe. Given a sweep, the longshot wins at a fraction of the unconditional rate, far below the 1% that a 1c bid needs. Every 1c fill therefore loses about 0.8-1c in expectation. Takers who buy longshots at 1-3c are the opposite case, lottery buyers who hit favorite bids when the favorite is safe. `net_cal` in the variant table marks every fill at the realized rate of its model bucket given a taker hit a bid at its price: the 1c, 2-3c and 97-99c classes, unconditional otherwise. The CI ends give the bracket. Fills at a 10-90% fair keep the model's own fair, so for live-strike quoting (the F variants) `net_cal` is circular and is not evidence.

### Variants under opt / cons / swp (all 39 settled recorded hours, 26OCT1421-26OCT1613)

Run with subtraction on, 30 swp draws, the 38-hour hazard and the conditional tails. Each variant is an explicit set of overrides of DEFAULT_POLICY. G = live off, decided, q_pad 1000, q_fav 300, pos_cap 1500: the live G before the 10/16 pad hold, `G0_nohold` in the grid. G_hold180 = G with the 180 s pad hold, `G_decided_only` in the grid (what runs live). This table was run on the temp-sweep branch, where G_hold180 was the sweep module's own decision-time hold (`hold_pads_s`); since the merge into temp-integrate it is the policy's `pad_hold_open_s` 180, and the two give identical results on all 39 hours (opt, cons and every swp draw). The hours are 10/14: 3, 10/15: 22, 10/16: 14. Column notes:
- "90% CI" and "P>0" are a bootstrap of the swp total over hours.
- "ex-best" is the swp total without its best hour.
- "net_cal" is the swp total with fills marked at the fill-conditional realized tails, with its CI ends in brackets.

| variant | opt net | cons net | swp reward | swp P&L | swp net | 90% CI | P>0 | ex-best | net_cal [CI ends] |
|---|---|---|---|---|---|---|---|---|---|
| G | +3,074 | +3,315 | 1,421 | -2,184 | -763 | -2,874..+2,084 | 0.25 | -2,166 | **-978** [-1,624, +63] |
| G_hold180 | +2,853 | +3,319 | 1,541 | -1,998 | -457 | -2,556..+2,365 | 0.33 | -1,861 | -743 [-1,325, +202] |
| pads_only (q_fav 0) | +2,132 | +2,366 | 973 | -1,885 | -912 | -2,502..+1,969 | 0.26 | -2,300 | -1,717 [-2,371, -663] |
| J (q_fav 100) | +2,717 | +2,813 | 1,171 | -2,011 | -840 | -2,616..+2,031 | 0.25 | -2,234 | -1,385 [-2,036, -336] |
| G q_pad 200 | +3,410 | +3,688 | 1,308 | -2,111 | -803 | -2,925..+2,055 | 0.25 | -2,211 | -1,031 [-1,664, -16] |
| G q_pad 500 | +3,236 | +3,509 | 1,352 | -2,145 | -792 | -2,906..+2,060 | 0.25 | -2,197 | -1,013 [-1,653, +17] |
| G pos_cap 500 | +286 | -95 | 315 | -914 | -599 | -1,669..+501 | 0.17 | -1,070 | -354 [-591, +26] |
| G pos_cap 3000 | +3,146 | +3,648 | 2,206 | -3,478 | -1,271 | -4,687..+4,097 | 0.26 | -3,898 | -2,371 [-3,440, -632] |
| no fillers (q_pad 0, no qual) | -98 | -37 | 199 | -300 | -101 | -1,121..+605 | 0.46 | -282 | +512 [+496, +529] |
| no fillers, q_fav 600 | -195 | -179 | 310 | -554 | -244 | -1,832..+770 | 0.41 | -442 | +662 [+653, +665] |
| **favorite joins only** (no fillers, no near-decided quotes) | -118 | -87 | 172 | -259 | **-87** | -1,100..+609 | 0.46 | -265 | **+499** [+479, +511] |
| F (live 600, skew 0.3) | +2,010 | +3,276 | 1,780 | -2,652 | -872 | -3,252..+2,072 | 0.27 | -2,278 | (circular) |
| F without fillers | -1,426 | -198 | 474 | -839 | -365 | -1,700..+870 | 0.33 | -725 | (circular) |
| F live strikes only | -1,448 | -91 | 343 | -523 | -180 | -1,027..+685 | 0.35 | -537 | (circular) |

Per day, swp net / net_cal:
- G: -118 / -100 (10/14), -986 / -604 (10/15), +340 / -274 (10/16).
- Favorite joins only: +26 / +26, +93 / +92, -205 / +381.

`variant_table.txt` has every day and fill model.

**Sensitivities** (`sens`, 10 draws, swp). Swp net and net_cal, with the hazard x0.5 / x1.5, the flash-free hazard, rec_mode size / front, and 800 joiners diluting our 1c reward:

| config | G net | G net_cal | G_hold180 net | G_hold180 net_cal | pads_only net_cal | no fillers net | no fillers net_cal |
|---|---|---|---|---|---|---|---|
| base | -763 | -978 | -457 | -743 | -1,717 | -101 | +512 |
| hazard x0.5 | -311 | -588 | -148 | -479 | -1,399 | -85 | +521 |
| hazard x1.5 | -1,022 | -1,182 | -630 | -884 | -1,885 | -116 | +506 |
| noflash | -562 | -786 | -287 | -583 | -1,549 | -95 | +516 |
| rec size | -666 | -383 | -194 | +2 | -1,242 | -106 | +508 |
| rec front | -933 | -1,115 | -638 | -891 | -1,842 | -116 | +503 |
| joiners 800 | -737 | -953 | -456 | -748 | -1,837 | -102 | +512 |

**Reading it.**
- **Realized settlements cannot decide this alone.** Every swp total is negative, but each is driven by two settlements going opposite ways. 26OCT1606 (+$1,403 for G) is a longshot that won: the T81.99 YES we held 1,500 of at 1c. 26OCT1612 (-$592 for G, -$497 for favorite joins) is a favorite that lost: T86.99 YES at 97%+ settled NO at 86.36. P(total > 0) is 0.25 for G and 0.46 for favorite joins.
- **The fill-conditional tails do decide it.**
  - Every 1c filler variant is negative in expectation, by $354 to $2,371 over the 39 hours (about -$25/h for G). That holds in every sensitivity except G_hold180 under rec_mode size, which comes out at +2.
  - Only the CI ends of G, G_hold180 and q_pad 500 reach positive, at +17 to +202, and they assume the high end of every 1c bucket at once.
  - Smaller pads, a 180 s hold, joiners and a lower hazard shrink the loss without removing it.
- **Favorite joins are positive in expectation and robust.** Without fillers, net_cal is +$499 to +$662 over 39 hours (+$13-17/h). No fillers stays at +503..+521 under every sensitivity. The realized total is -$87 because of one upset (26OCT1612). Without that hour it is +$410.
  - Near-decided half-size quoting adds about $13 and 7,000 swept 1c contracts.
  - q_fav 600 adds $150 of expectation and doubles the upset loss.
- **By ET settle hour, favorite joins only:** +$6 to +$36 per hour from 20:00 to 07:00 ET, +$1-5 from 08:00 to 11:00, about $0-7 from 14:00 to 19:00 (reward about $1/h there). 12:00 and 13:00 hold the upset (-$496) and a +$179 hour.
- **F's live-strike quoting loses** under opt and cons as well, and its net_cal is circular, so it is not a candidate.

### Daily pipeline swp (SIM_VERSION 6, 2026-10-16)

Jack 2026-10-16 ("1"): "ship the fixes + the sweep-aware report column, and add favorite-only and overnight-favorite-only to the shadow". Since SIM_VERSION 6, `temp_shadow_sim` runs swp for every variant beside opt / cons (Simulator, Fill models). These checks were run on a copy of the recording (raw/, settled/, model/, backfill/calibrations.json and the executor's 10/16 logs, taken 10/16 17:18 ET; KL-data only read). The settled recorded hours are the 43 from 26OCT1421 to 26OCT1617.

**opt / cons unchanged.** Every pre-existing variant's opt and cons result on all 43 hours is identical to the merged tree before swp (SIM_VERSION 5, temp-integrate 88d5f5d): 1,118 variant x fill results, 43,602 values, 0 differences. The shared blocks (coverage, market stats, model eval, results) are identical too.

**swp = the standalone sweep module.** Here the pipeline ran with the sweep study's 38-hour hazard (id 1e6dfe6a65c52a4d, dropped in as `model\sweep_hazard.json` on the copy) at 6 draws, on the study's 39 hours (26OCT1421-1613). For the six variants both grids have, every event's per-draw nets equal the study's first 6 draws exactly: 234 of 234 variant x event pairs. The pairs are G0_nohold = G, G_decided_only = G_hold180, FAV_only = "favorite joins only", I_pads_only = pads_only, J_decided_fav100 = J_fav100 and F_full = F. Against the study's 30-draw means, the 6-draw totals are within Monte Carlo noise:

| variant | pipeline swp, 6 draws | study, first 6 draws | study, 30 draws | difference (Monte Carlo se) | net_cal 6 / 30 draws |
|---|---|---|---|---|---|
| G0_nohold (study G) | -805.13 | -805.13 | -763.29 | -41.84 (34.70) | -1,022 / -978 |
| G_decided_only (G_hold180) | -462.14 | -462.14 | -456.78 | -5.36 (19.72) | -758 / -743 |
| FAV_only (favorite joins only) | -86.61 | -86.61 | -86.61 | 0 (exact) | +499 / +499 |
| I_pads_only (pads_only) | -941.94 | -941.94 | -911.87 | -30.06 (31.00) | -1,749 / -1,717 |
| J_decided_fav100 (J_fav100) | -875.82 | -875.82 | -839.63 | -36.20 (32.67) | -1,423 / -1,385 |
| F_full (F) | -904.42 | -904.42 | -872.45 | -31.98 (28.74) | +1,442 / +1,491 (circular) |

The se is that of a 6-draw mean against the 30-draw mean of the same draws. The differences share their draws (common random numbers), so they move together.

**The variant table under the shipped defaults** (the 37-hour pre-live hazard, 6 draws, all 43 settled recorded hours, totals in $):

| variant | opt net | cons net | swp reward | swp P&L | swp net | mc se | swp 1c contracts | net_cal (estimate) |
|---|---|---|---|---|---|---|---|---|
| H_noguard_pull | +2,020 | +669 | 731 | -179 | **+551** | 11 | 11,253 | (circular) |
| FAV_night | +682 | +198 | 119 | +80 | **+198** | 0 | 0 | +196 |
| C_live300_back | -962 | +119 | 396 | -412 | -15 | 51 | 20,236 | (circular) |
| B_live300_pull | -1,033 | +32 | 274 | -353 | -80 | 11 | 11,108 | (circular) |
| FAV_only | -192 | -82 | 177 | -259 | -82 | 0 | 0 | **+503** |
| D_live600_pull | -1,495 | -53 | 395 | -575 | -179 | 2 | 11,485 | (circular) |
| A_live300_nowin | -1,154 | -231 | 510 | -875 | -365 | 51 | 20,180 | (circular) |
| G_decided_only | +2,843 | +3,473 | 1,618 | -2,205 | -588 | 22 | 347,156 | -793 |
| K_full_wcl500 | +2,757 | +3,496 | 1,856 | -2,760 | -904 | 33 | 372,972 | (circular) |
| G0_nohold | +3,075 | +3,480 | 1,493 | -2,452 | -959 | 33 | 371,806 | -1,090 |
| E_live300_pull_decided | +2,231 | +3,512 | 1,742 | -2,737 | -995 | 35 | 376,128 | (circular) |
| F_full | +2,039 | +3,506 | 1,916 | -2,942 | -1,026 | 34 | 377,179 | (circular) |
| J_decided_fav100 | +2,693 | +2,956 | 1,235 | -2,278 | -1,043 | 30 | 369,609 | -1,504 |
| L_decided_pad3000 | +3,065 | +3,471 | 1,542 | -2,616 | -1,074 | 35 | 388,223 | -1,168 |
| I_pads_only | +2,180 | +2,491 | 1,031 | -2,152 | -1,121 | 28 | 367,715 | -1,842 |

net_cal for the variants that quote live strikes keeps the model's own fair on their 10-90% fills: +1,384 to +5,137, which is circular and not evidence (Realized tails, above).

Per ET day, opt / cons / swp net, and swp net_cal:
- FAV_only: 10/14 (3 hours) +114 / +26 / +26, +26; 10/15 (22) +372 / +93 / +93, +92; 10/16 (18) -678 / -200 / -200, +386.
- FAV_night: 10/14 +114 / +26 / +26, +26; 10/15 +316 / +77 / +77, +76; 10/16 +251 / +96 / +96, +95.
- G_decided_only: 10/14 +262 / +176 / -88, -76; 10/15 +1,811 / +1,688 / -852, -495; 10/16 +770 / +1,609 / +353, -221.

**Reading it.**
- FAV_* never rest a 1c order, so swp = cons for them, exactly and with no Monte Carlo noise. FAV_night is positive on every day: +$9.4 per event-hour realized and +$9.4 at calibrated tails over its 21 hours. FAV_only's daytime hours hold the 26OCT1612 upset (-$496), so it is -$82 realized and +$503 at calibrated tails. The 13:00 hours alone are +$175 of that +$503, and the other daytime hours add about +$2 an hour.
- Every variant with 1c fillers keeps losing under swp, as in the study. The live-quoting variants without decided strikes (A-D, H) rest qualification pads only, and sweepers take 11,000-20,000 contracts of them. H_noguard_pull is the best realized swp net (+$551), and the report's headline would show it. Its expectation (net_cal) is circular, and the guard rule weighs it against B.

### Live-order subtraction and the 10/16 shadow numbers

- 26OCT1600 to 1609 (before live) and 26OCT1611 to 1613 (after the HALT) have no live rows. The subtraction is a no-op there. On 1611 all 24 VARIANT x fill entries are identical.
- On 26OCT1610 the plain shadow sim (temp_shadow_sim.run_event, as the KL checkout would run it in the 08:30 report) without the subtraction sees our own pads and the sweeps of them:

  | variant | unsubtracted opt / cons | subtracted opt / cons |
  |---|---|---|
  | G_decided_only (SIM_VERSION 4, before the pad hold: today's G0_nohold) | -$71.71 / -$35.24 | +$7.26 / +$12.58 |
  | F_full | -$21.38 / -$23.32 | +$59.37 / +$23.05 |
  | I_pads_only | -$84.67 / -$42.75 | -$4.21 / +$2.22 |

  Variants without decided-strike quoting move by about $0.02.
- Neither opt/cons figure is right. The unsubtracted one is "right" only because our live orders were in the book. swp says -$63 (queue) / -$88 (front) against -$82 actual.

### Commands

Outputs go to `--out`, which defaults to `TEMP_SHADOW_SWEEP_OUT`, else `<TEMP_SHADOW_DATA>\sweep`. The 2026-10-16 runs wrote to the session scratchpad and left KL-data read-only.

```
python temp_shadow_sweep.py calibrate                        # hazard.json (+ hazard_prelive.json), calibration.txt
python temp_shadow_sweep.py tails                            # tails.json / tails.txt: unconditional + fill-conditional rates
python temp_shadow_sweep.py validate --live --hazard <out>\hazard_prelive.json
python temp_shadow_sweep.py run --day 2026-10-14 2026-10-15 2026-10-16 [--draws 30] [--variants G,no_fillers]
                                [--rec-mode level|front|size|all] [--hazard-scale X] [--hazard-kind noflash] [--tag T]
python temp_shadow_sweep.py sens --day ... [--configs h0.5,h1.5,noflash,size,front,join800] [--draws 10]
python temp_shadow_sweep.py table                            # variant_table.txt/.json incl. sensitivities
python temp_shadow_sweep.py export --hazard <out>\hazard.json --to <TEMP_SHADOW_DATA>\model\sweep_hazard.json
```

`run` and `sens` read `<out>\hazard.json` and `<out>\tails.json` when present, else the built-in `DEFAULT_HAZARD` / `DEFAULT_TAILS` (the 2026-10-16 calibrations). A cached run file is reused only when the draws, rec_mode, channels, policies, fill models, hazard (table, scale and kind), joiners and tails all match. The first pass had two bugs here. `--hazard-scale` never reached the worker, and a recalibrated hazard silently reused old run files.
