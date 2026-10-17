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
| `temp_shadow_sweep_favhazard.json` | The stress fill model's hidden favorite-taker table (channel (e), `calibrate-fav` over the 62 recorded hours without live orders, 10/17). Not read by the daily pipeline. |
| `temp_shadow_audit.py` | The 10/17 audit: `audit` (the live executor's actual orders under every fill model), `clears` (favorite-level clears and the upset rate after them), `reaction` (did other makers react to our orders), `stress-table` (the variants under opt / cons / swp / stress). Section "Order-level audit and the stress model", below. |
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
  - A variant whose first draw never rests a 1c order is the same in every draw (`swp_exact`). FAV_only and FAV_night are exact on every event by construction since the 10/16 fix pass (`fav_min_price_c` 50, under Policy and variants); before it 5 of the 43 recorded events were not, because their favorite join fell to a 1-3c reference.
  - A variant whose first draw accepts no hidden sweep logs its 1c trajectory, and a later draw is run only if one of its candidates would be accepted along it (`temp_shadow_sweep.hazard_hits`). This covers the live-only variants, whose qualification pads rarely meet the hazard.
- **Runtime** (measured 10/16 on copies of the recording at the report task's 2 workers, 6 draws). With nothing else running, `run_day` took 398 s for a 22-event day (10/15; 485 s at 8 draws; about 70 s for opt / cons alone) and 331 s for 10/16's 18 settled events: about 18 s of wall time per event. Under the machine's normal load (the bots, the collector and other Python jobs, about 80% total CPU) the same work runs about 1.6x slower: the full report took 660 s for 10/15 and 552 s for 10/16 in the review, about 29.5 s per event (other analysis jobs were running too), and the fix pass's end-to-end report for 10/16 (the first report after deploy: 42 events re-simmed over 10/15-10/16, the bots at about 80% CPU) took 681 s, about 16 s per event. At the review's loaded rate a 24-event day takes about 12 minutes, and a full re-sim of the longest window (10/15-10/23 = 200 events, after a SIM_VERSION, grid or swp-table change) about 100 minutes, which does not fit the task's 2 h limit with margin. So the report re-sims earlier window days only within a budget (Daily report, Sim freshness: 80 minutes from the start of its sim), and a window re-sim may take two mornings. The Monte Carlo se of a 24-event day of a 1c-filler variant is about $25 (G0_nohold: $33 over the 43 recorded hours); FAV_* have none.
- **Contained** (fix pass 10/16): if the sweep path raises for an event (setup or the draws), the event keeps its opt / cons results, gets a top-level `swp_error` and no `"|swp"` keys, and the next `run_day` re-sims it (the cache wants every fill model). Before, the whole event failed and its opt / cons results were lost. If `swp_config()` itself raises (an unusable repo table), every event is simmed without swp and cached files without swp are reused, since a re-sim could not add it. The report counts the window's events without swp in a banner and the subject.
- **Cache:** `run_day` reuses a sim file only when its swp block matches the current draws, hazard id, tails id, rec_mode, channels, seed and sweep version. A recalibration or a new draw count re-sims the report's whole window, so the cumulative figures never mix tables.

**Recalibrating swp.** The sim loads `<TEMP_SHADOW_DATA>\model\sweep_hazard.json` when it exists, else the repo's `temp_shadow_sweep_hazard.json`. Tails work the same way, with `sweep_tails.json` / `temp_shadow_sweep_tails.json`. To refit on newer hours:
```
python temp_shadow_sweep.py --out <dir> calibrate                  # <dir>\hazard.json (+ hazard_prelive.json while live hours exist)
python temp_shadow_sweep.py --out <dir> validate --live --hazard <dir>\hazard_prelive.json   # check it on the live hours first
python temp_shadow_sweep.py export --hazard <dir>\hazard.json --to <TEMP_SHADOW_DATA>\model\sweep_hazard.json
python temp_shadow_sweep.py --out <dir> tails                      # optional
python temp_shadow_sweep.py export --tails <dir>\tails.json --to <TEMP_SHADOW_DATA>\model\sweep_tails.json
```
`export` keeps exactly what `Hazard` / `TailCal` read, in order (the size draws are indexed, so their order is part of the fit). It prints the table's id, which every sim file then records. The next report re-sims its window under the new table: the report day first, then the earlier days within the 80-minute budget, and whatever does not fit the next morning (flagged stale meanwhile). Deleting the file goes back to the repo default.

**Table checks** (fix pass 10/16). A `model\` override is used only if it is a usable table: a hazard needs a non-empty `hazard` fit (`hazard.a`), a positive `lam_max` and its `sizes_all` size draws; tails need bucket rows `[lo, hi, model_mean, realized, ci_lo, ci_hi]` with 0 <= lo < hi <= 0.5 and rates in [0, 1]. `Hazard()` reads a doc without a `hazard` fit as an all-zero table, so before this check a wrong or truncated file silently switched the hidden-sweep channel off, re-simmed the window and changed the headline. A rejected override is not used: swp runs on the repo default, the sim file's `swp` block records `hazard_rejected` / `tails_rejected`, and the report shows a "swp table problem" banner and subject flag. An unusable repo table raises, and swp is then left out as above. The report names the tables behind its swp numbers (id, repo default or `model/` override) under Variants and in the email text.

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

**Variant grid:** the grid is in `temp_mm_policy.VARIANTS` (re-exported as `temp_shadow_sim.VARIANTS`). Two variants were added on 10/16 (Jack: "1", "add favorite-only and overnight-favorite-only to the shadow"). `FAV_only` = live off, decided on, `q_pad` 0, `qual_pads` off, `near_decided_half` off, `fav_min_price_c` 50: G's favorite joins and nothing under 50c. The floor came from the 10/16 fix-pass review. G's favorite join is priced at the side's reward reference, the first level where the cumulative size reaches target/5 = 200. When the favorite side's levels above a deep 1-5c pad hold fewer than 200 contracts, that reference IS the pad, so without the floor FAV_only bid the favorite at 1-3c (26OCT1611: YES 99:31, 98:0.01, 94:13 ... over a 1c pad, a 300-lot YES bid at 1c; swp was not exact in 5 of the 43 recorded hours because of such orders), and the DRY executor moved a favorite join 95c -> 79c -> 5c. Over the 43 hours the floor removes 4,905 strike-seconds of favorite joins under 50c in 39 hours, worth $4 of reward (cons / swp) and $42 of opt fills; FAV_night is unchanged but for $1 of opt. With the floor, such a join is dropped (`diag` `fav_floor`); a 79c reference is still joined. `fav_min_price_c` defaults to 0, so no earlier variant changes. The sweep study's "favorite joins only" is FAV_only without the floor. `FAV_night` = FAV_only with `quote_hours_et` [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7], i.e. the events settling 20:00-07:00 ET, where the sweep study found almost all of its expectation. Every earlier variant is unchanged: on the recording their opt / cons results are bit-identical to SIM_VERSION 5 (Daily pipeline swp, below). SIM_VERSION 6 (swp for every variant, the two FAV variants) re-sims every cached day the report asks for. Since 10/16 `G_decided_only` (what runs live) holds its pads for 180 s and `G0_nohold` is the old G. SIM_VERSION 5 makes `run_day` re-sim the older cached files of any day it is asked for; the daily report asks for every day of its cumulative window (day 1 .. the report day), so the first report after a SIM_VERSION or `VARIANTS` change re-sims the whole window (review fix 10/16: before it only the report day re-simmed, so 10/15 stayed at SIM_VERSION 4, where `G_decided_only` had no hold and `G0_nohold` did not exist; the first report run with the fix re-sims it to 5). SIM_VERSION 5 also covers the live-order subtraction from the sweep-aware shadow (merged with it, see below): the 10/16 files simmed at 4 without it re-sim too. The sweep-aware shadow runs the same pair as `G_hold180` (= `G_decided_only`) and `G` (= `G0_nohold`).

## Daily report (08:30 ET, previous ET day, plus cumulative)

**Variant table:** reward, fills, contracts, trading P&L and net per variant, for `opt`, `cons` and `swp` side by side. The swp group adds the Monte Carlo standard error, the 1c filler contracts swept, and "expected (calibrated tails)" = `net_cal`, with its caveat that it is an estimate. `net_cal` is shown only for variants that quote no live strikes (policy `live` False: G, G0, I, J, L, FAV_*); for a live-quoting variant a fill at a 10-90% fair keeps the model's own fair, so the cell reads "(circular)", and the headline, the email text and the week risks leave the figure out when the best variant quotes live strikes (fix pass 10/16: on the recording the swp headline was a live-quoting variant on both days, shown next to a circular "expected" 2-9x its realized net). A one-line note explains swp. **The headline is the best `swp` variant** ("Chosen on swp net"), with the best cons / opt variants beside it. The email subject says so: `best FAV_night net +$X/day on swp (reward ..., fills ...)`. Sim output from before SIM_VERSION 6 has no swp, and the report then falls back to cons and says so (`headline_fill`).

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

**Sim freshness:** the report re-runs `run_day` for every window day (cached files from the current SIM_VERSION with the current overrides are only loaded). A window file still from an older SIM_VERSION (a `--no-sim` report, a failed or deferred re-sim) is flagged at the top of the report and in the subject.
- **Time budget** (fix pass 10/16): the report day is always simmed in full first. The earlier days run with `run_day`'s deadline at the sim's start + `TEMP_SHADOW_RESIM_BUDGET_MIN` (80) minutes: an event not started by then is deferred, keeps its older file (flagged stale), and the next report continues. A banner and the subject (`[N re-sims deferred]`) say so.
- **Events without swp** (the swp path failed and the event kept opt / cons, or an older file) are counted in a banner and the subject (`[N events without swp]`); the swp columns, headline, rules and verdict cover the other events.
- A report-day event that failed in the sim but whose older file is still loaded is named as such in the failed-events banner, instead of "left out of every figure".

**Cumulative table:** per variant, the swp sum, per day, days positive, Monte Carlo se and expected-at-calibrated-tails sum ("(circular)" for live-quoting variants), then cons and opt sums. The day columns are swp net.

**Week summary:** on and after day 7, with a go/no-go and a recommended starting configuration. The GO / NO-GO is judged on the best **swp** variant, and its risks line gives swp (+/- Monte Carlo se), cons, opt and, when the best variant quotes no live strikes, the expected-at-calibrated-tails figure.

**Delivery:** emailed via `send_html_email.py` (ALERT_EMAIL_* env). The HTML is kept in reports/.

## Tasks

| task | trigger | does |
|---|---|---|
| `KL temp-shadow collector` | at logon (+1 min), and every 10 min with IgnoreNew, no time limit | `run_temp_shadow_collector.ps1` restart loop |
| `KL temp-shadow report` | daily 08:30 ET, StartWhenAvailable, 2 h limit | sim the previous ET day and re-sim any stale window day, build the report, email it. With swp, a day takes about 7.5 min idle and about 12 min under the machine's normal load at 6 swp draws and 2 workers; a full re-sim of the longest window (10/15-10/23, 200 events) would take about 100 min loaded, so earlier window days re-sim only within 80 min of the sim's start and the rest continues the next morning (measured 10/16) |

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
- `"<variant>|swp"` (since SIM_VERSION 6) holds the same keys as means over its Monte Carlo draws, plus `draws`, `net_sd` / `reward_sd` / `trading_pnl_sd` / `contracts_sd` / `filler_contracts_sd`, `net_se` (= net_sd / sqrt(draws)), `draw_net` (per draw), `swp_exact`, `filler_contracts` (1c), `haz_events`, `sweep_src` (`rec` / `haz` / `cross` contracts), `pnl_cal`, and `net_cal` with `net_cal_tlo` / `net_cal_thi`. The file adds `"fill_models"` (the fill models actually written) and `"swp": {"draws", "rec_mode", "channels", "seed", "hazard_id", "hazard_src", "hazard_kind" ("repo" | "override"), "hazard_rejected", "hazard_built", "lam_max", "tails_id", "tails_src", "tails_kind", "tails_rejected", "tails_built", "sweep_version"}`. If the sweep path raised, `"swp"` is null, there are no `"|swp"` keys and `"swp_error"` holds the error. `run_day` results can also be `{"event", "deferred": true, "settled": true}` (not started before its deadline).

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

**swp = the standalone sweep module.** Here the pipeline ran with the sweep study's 38-hour hazard (id 1e6dfe6a65c52a4d, dropped in as `model\sweep_hazard.json` on the copy) at 6 draws, on the study's 39 hours (26OCT1421-1613). For the six variants both grids have, every event's per-draw nets equal the study's first 6 draws exactly: 234 of 234 variant x event pairs. The pairs are G0_nohold = G, G_decided_only = G_hold180, FAV_only = "favorite joins only", I_pads_only = pads_only, J_decided_fav100 = J_fav100 and F_full = F. (Since the fix pass FAV_only has `fav_min_price_c` 50 and the study's "favorite joins only" has no floor: the pair is FAV_only with the floor off, re-run in the fix pass: again 234 of 234 identical. The shipped FAV_only is -$90.85 there against -$86.61, the $4.24 of reward its sub-50c joins earned, and exact on 39 of 39 hours.) Against the study's 30-draw means, the 6-draw totals are within Monte Carlo noise:

| variant | pipeline swp, 6 draws | study, first 6 draws | study, 30 draws | difference (Monte Carlo se) | net_cal 6 / 30 draws |
|---|---|---|---|---|---|
| G0_nohold (study G) | -805.13 | -805.13 | -763.29 | -41.84 (34.70) | -1,022 / -978 |
| G_decided_only (G_hold180) | -462.14 | -462.14 | -456.78 | -5.36 (19.72) | -758 / -743 |
| FAV_only (favorite joins only) | -86.61 | -86.61 | -86.61 | 0 (exact) | +499 / +499 |
| I_pads_only (pads_only) | -941.94 | -941.94 | -911.87 | -30.06 (31.00) | -1,749 / -1,717 |
| J_decided_fav100 (J_fav100) | -875.82 | -875.82 | -839.63 | -36.20 (32.67) | -1,423 / -1,385 |
| F_full (F) | -904.42 | -904.42 | -872.45 | -31.98 (28.74) | +1,442 / +1,491 (circular) |

The se is that of a 6-draw mean against the 30-draw mean of the same draws. The differences share their draws (common random numbers), so they move together.

**The variant table under the shipped defaults** (the 37-hour pre-live hazard, 6 draws, all 43 settled recorded hours, totals in $; the FAV rows with the fix pass's `fav_min_price_c` 50 -- before it FAV_only was opt -192, cons / swp -82, net_cal +503 and FAV_night opt +682; every other row is unchanged, 1,677 of 1,677 results identical):

| variant | opt net | cons net | swp reward | swp P&L | swp net | mc se | swp 1c contracts | net_cal (estimate) |
|---|---|---|---|---|---|---|---|---|
| H_noguard_pull | +2,020 | +669 | 731 | -179 | **+551** | 11 | 11,253 | (circular) |
| FAV_night | +681 | +198 | 118 | +80 | **+198** | 0 | 0 | +196 |
| C_live300_back | -962 | +119 | 396 | -412 | -15 | 51 | 20,236 | (circular) |
| B_live300_pull | -1,033 | +32 | 274 | -353 | -80 | 11 | 11,108 | (circular) |
| FAV_only | -234 | -86 | 173 | -259 | -86 | 0 | 0 | **+499** |
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
- FAV_only: 10/14 (3 hours) +114 / +26 / +26, +26; 10/15 (22) +372 / +93 / +93, +92; 10/16 (18) -719 / -204 / -204, +382.
- FAV_night: 10/14 +114 / +26 / +26, +26; 10/15 +316 / +77 / +77, +76; 10/16 +251 / +96 / +96, +95.
- G_decided_only: 10/14 +262 / +176 / -88, -76; 10/15 +1,811 / +1,688 / -852, -495; 10/16 +770 / +1,609 / +353, -221.

**Reading it.**
- FAV_* never rest an order under 50c (`fav_min_price_c`), so swp = cons for them, exactly and with no Monte Carlo noise (`swp_exact` on 43 of 43 hours; 38 and 39 before the floor). FAV_night is positive on every day: +$9.4 per event-hour realized and +$9.3 at calibrated tails over its 21 hours. FAV_only's daytime hours hold the 26OCT1612 upset (-$497), so it is -$86 realized and +$499 at calibrated tails. The 13:00 hours are +$172 of that +$499, the 12:00 hours (the upset) +$95 at calibrated tails, and the other daytime hours add about +$2 an hour.
- Every variant with 1c fillers keeps losing under swp, as in the study. The live-quoting variants without decided strikes (A-D, H) rest qualification pads only, and sweepers take 11,000-20,000 contracts of them. H_noguard_pull is the best realized swp net (+$551), and the report's headline would show it. Its expectation (net_cal) is circular, so since the fix pass the report shows "(circular)" for it and every live-quoting variant and leaves the figure out of the headline; the guard rule weighs it against B.

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

## Order-level audit and the stress model (2026-10-17)

Jack 2026-10-17: "Make sure you don't make same mistake where the shadow trading results actually doesn't hold when it is tested live." The shadow said G made about +$1.8k a day; its one live hour (26OCT1610) lost $96 on about 10,000 1c filler contracts. The replay had assumed the market would not react to our orders: fills came only from recorded trades, made in a market that did not contain our orders. This pass audits the fill models against the live hour with the policy taken out, adds a pessimistic fill model (stress), re-runs the favorite-join variants under it, and checks how other makers reacted to our orders. Code: `temp_shadow_audit.py` (new) and the stress model in `temp_shadow_sweep.py`. The daily pipeline is unchanged: it does not run stress.

### Audit of the live hour on the bot's actual orders (`temp_shadow_audit.py audit`)

The live executor's 186 orders on 26OCT1610 are replayed as fixed trajectories from its orders log: every place / amend / renew / decrease / cancel / gone / expiry at its effective time. The effective time is the logged decision time plus the request latencies of the rows sent before it in the same decision (the hour's first create took 1.13 s, so the first pad rested about 1.2 s after its logged time; the shadow's subtraction uses the logged times). Each fill model then fills those exact orders from the recorded tape, with our orders out of the books and our 21 prints out of the tape, as the shadow does. In each model's world an order keeps its actual lifetime and logged total and fills from its own remaining; an order the market filled out ends at that fill in every world. The queue ahead at a place or price amend is the recorded level at that price just before the logged time (the recording held none of the order yet). Actual fills are timed by their tape prints (exchange time).

Contracts / $ at settlement (30 Monte Carlo draws for the hazard channels):

| class | actual | opt | cons | swp, live tape | swp front | swp joiner-free | stress |
|---|---|---|---|---|---|---|---|
| 1c fillers (pad / qual) | 10,006 / -$100.06 | 3,558 / -$35.58 | 2,012 / -$20.12 | 8,775 / -$87.75 | 10,064 / -$100.64 | 4,952 / -$49.52 | 8,775 / -$87.75 |
| favorite bids 90-99c | 102 / +$4.07 | 28 / +$1.12 | 0 | 0 | 0 | 0 | 0 |
| near-decided quotes | 0 | 26 / -$0.26 | 0 | 0 | 26 / -$0.26 | 0 | 0 |
| total | 10,108 / -$95.99 | 3,613 / -$34.73 | 2,012 / -$20.12 | 8,775 / -$87.75 | 10,091 / -$100.91 | 4,952 / -$49.52 | 8,775 / -$87.75 |

- "swp, live tape" is the shadow's swp on this hour as the daily pipeline replays it: its recorded 1c takers include the ones that hit our live orders while other makers sat at 1c beside us (channel (a) alone gives 8,649). Without those takers (swp strict) it predicts 689. Other makers joined the 1c levels our pads opened within seconds (Market reaction, below), so most of our fills look "covered" in this tape.
- "swp joiner-free" replays the five sides our pads opened with nobody else at 1c and none of their recorded 1c takers: the regime every pre-live hour of the shadow is replayed in. It predicts 4,952 1c contracts (p10-p90 over draws 3,575-6,274), half of what filled. On the orders that actually filled it gives a fill probability of 0.54 (live tape: 0.88).
- The hidden-sweep hazard over the ACTUAL exposure (the sum of lambda dt over the seconds our live 1c orders rested alone, joiner-free book): 5.39 sweeps expected, 10 came (P(X >= 10) = 0.049), and they took 615 contracts each on average, whole pads. The rate is about 1.9x too low, and the size draw (cleared 58-68%) too small.
- Timing: the 14 filled 1c orders were first hit 1.4, 3.1, 6.4, 12, 14, 14.8, 17.5, 31, 85, 101, 109, 119, 183 and 314 s after they rested. opt / cons reproduce the times only where a recorded print existed (P(filled) 0.79 / 0.50 on those orders).
- Favorite bids: one fill, 101.77 contracts on the T86.99 NO bid at 96c (+$4.07). A taker bought YES at 2c, 3c and 4c, clearing the 98 and 97 NO levels and taking 129.77 of the 578 at 96: 28 of others' and then ours. cons / swp / stress put us behind the 278 contracts that rested at 96 when we amended there, so they fill nothing; the live queue had us nearly first (makers re-place constantly, and a steady level size hides it). opt fills 28 (the external print it sees). One fill is not a measurement of the favorite fill model.
- Near-decided quotes: no fills; opt alone gives 26.
- Stress = swp on this hour: no recorded taker cleared or walked through a level our favorite orders rested at, and our favorite orders never rested alone at the top.

### Stress fill model (`temp_shadow_sweep.py`, fill model `stress`)

stress = swp plus two channels on our orders at every price of 2c or more (swp leaves those to cons):
- (d) level-clear: a recorded taker cluster (prints on one market and side within 1 s, the full tape: replay tape plus the prints that filled our live orders) takes ALL of our lots on that side priced better than its worst print (it walked through our price), and all of our lots AT its worst price when it cleared that level (its prints there >= the level before it, ours included). cons fills such lots only after the queue ahead and only up to the prints' counts. A cluster made only of our prints is kept when someone else rested at a price it hit, dropped when we were alone. Sensitivities: `--stress-take size` (a clearing taker takes only up to its own observed size) and `--stress-hole` (a cluster that cleared its worst level also takes our lots in the gap below it, down to the next external level).
- (e) hidden favorite takers: while our lot on a favorite side (fair >= 0.90) rests alone at the top of that side, takers arrive at the rate at which the best favorite bid gets hit in the recording, a[favorite fair][age of the top level] x mult[minutes since open] (`calibrate-fav`, the 62 recorded hours without live orders: 0.0053 / 0.0030 / 0.0020 / 0.00044 hits per second at fair 0.90-0.97 / 0.97-0.99 / 0.99-0.995 / > 0.995, two to three times higher on a top level under 30 s old, and 2-3x in the last 10 minutes), and take a recorded top-hit size. Monte Carlo on its own seed. It barely acts: FAV's joins rest alone at the top about 0.06% of their resting time.

The appetite of level-clearing takers supports taking our lots whole: of 734 clusters that cleared the top favorite level with another level within 2c below it, 528 (72%) went on into it.

### Favorite-level clears and the upset rate after them (`temp_shadow_audit.py clears`)

Over the 63 recorded hours, a taker hits a favorite bid (side fair >= 0.90, prints at 90-99c) 14.6 / 8.4 / 5.7 / 1.5 times per strike-hour at fair 0.90-0.97 / 0.97-0.99 / 0.99-0.995 / > 0.995, and clears a level (takes its whole worst level or walks through one) in 37-48% of those hits.

Upset rate of the favorite after a favorite-bid hit (cluster-weighted, strike-clustered bootstrap 90% CI), against the model's longshot probability at the time:

| fair bucket | model | recorded hours, all hits | after a clear | after a hit that did not clear | 3 weeks before (REST tape), all hits | walked | single price |
|---|---|---|---|---|---|---|---|
| 0.90-0.97 | 5.9% | 5.0% [1.2, 10.0] | 5.0% | 5.0% | 3.2% [2.1, 4.4] | 3.5% | 3.1% |
| 0.97-0.99 | 1.9% | 4.5% [1.3, 8.6] | 6.4% [1.4, 13.3] | 3.3% [0.8, 6.3] | 1.9% [0.9, 3.1] | 1.4% | 2.1% |
| 0.99-0.995 | 0.7% | 6.6% [1.6, 13.2] | 9.1% | 5.2% | 1.0% [0.3, 1.9] | 0.5% | 1.2% |
| > 0.995 | 0.12% | 3.2% [0, 7.2] | 4.3% | 2.1% | 0.25% [0.09, 0.44] | 0.30% | 0.23% |

- Level-clearing sellers are not reliably more informed than other favorite-bid takers: in the recorded hours clears run somewhat higher but every CI overlaps (2-7 upset strikes per bucket); on 492 backfill hours of REST tape, takers that walked through a level were no more informed than single-price takers.
- Favorite-bid takers as a group WERE informed relative to the model in the recorded hours: 2-25x the model's rate, against about the model's rate in the three weeks before (the far tail about 2x). Ten favorite strikes lost in the 63 recorded hours: 26OCT1511, 1606, 1612, 1615, 1620, 1621, 1622 (two strikes) and 1713 (two strikes).
- The `net_cal` estimate marks 97-99c fills at the 530-hour table's conditional rates (about the model's), so it reads favorite joins as positive (FAV_night stress +$1,002) while the recorded hours lose. It is not evidence for favorite joins.

### The variants under opt / cons / swp / stress (`temp_shadow_audit.py stress-table`)

All 63 settled recorded hours, 26OCT1421 through 26OCT1713 (ET days 10/14: 3 hours, 10/15: 22, 10/16: 24, 10/17: 14), the repo's hazard and tails tables, 6 draws; net $ (cons = swp for every FAV variant: they rest nothing at 1c):

| variant | opt | cons | swp | stress | stress 90% CI (hours) | P>0 | ex-best | ex-best & worst | stress per day 10/14 / 15 / 16 / 17 |
|---|---|---|---|---|---|---|---|---|---|
| FAV_night | -985 | -1,471 | -1,471 | -1,635 | [-4,386, +558] | 0.13 | -1,723 | -546 | +32 / +149 / -1,977 / +161 |
| FAV_night_q100 | -439 | -515 | -515 | -523 | [-1,428, +197] | 0.13 | -553 | -168 | +12 / +51 / -642 / +56 |
| FAV_night_p99 | -745 | -672 | -672 | -894 | [-2,520, +341] | 0.15 | -971 | -205 | +24 / +137 / -1,106 / +52 |
| FAV_only | -2,838 | -2,450 | -2,450 | -3,573 | [-7,451, -227] | 0.03 | -3,789 | -2,320 | +32 / +165 / -2,481 / -1,289 |
| FAV_q100 | -1,526 | -1,007 | -1,007 | -1,185 | [-2,493, -61] | 0.04 | -1,259 | -755 | +12 / +57 / -814 / -441 |
| FAV_p99 | -1,595 | -1,097 | -1,097 | -1,634 | [-3,595, +26] | 0.05 | -1,772 | -1,005 | +24 / +143 / -1,224 / -577 |
| G_decided_only (the live G) | +1,197 | +2,887 | -4,388 | -5,527 | [-10,153, -1,041] | 0.02 | -6,933 | -5,490 | -85 / -790 / -2,636 / -2,016 |
| G0_nohold | +1,589 | +3,017 | -3,041 | -4,231 | [-8,124, -376] | 0.04 | -5,636 | -4,193 | -122 / -921 / -1,054 / -2,134 |

Stress sensitivities (totals): FAV_night -1,635 base, -1,886 with the gap rule, -1,640 with the size take, -1,635 without (e) (it never acts there). FAV_night_q100 -523 / -603 / -523; FAV_night_p99 -894 / -1,143 / -890; FAV_only -3,573 / -4,014 / -3,554.

- FAV_night is negative under every fill model and every stress sensitivity, and still negative without its best and worst hour (-$546 stress, -$627 cons). The +$198 of the 10/16 fix pass covered 21 night hours through 26OCT1617; the two night hours after it, 26OCT1621 and 26OCT1622 (21:00 and 22:00 ET on 10/16), lost $918 and $898 (cons; $967 and $1,177 stress). The daily report for 10/16 already showed FAV_night at -$1,710 realized.
- Per night hour: median +$7 (cons) / +$9 (stress), mean -$45 / -$50, sd $220 / $262. Without those two hours +$11 / +$16 an hour. Every FAV variant makes a few dollars in most hours and loses $300-1,500 in the hours when a strike the model held at 98-99.7% fails: 4 of the 63 hours (26OCT1612, 1621, 1622, 1713), 2 of the 33 night hours.
- In both night losses the model and the market disagreed. 26OCT1621 settled 82.40: the model held T82.99 YES at 98.4-99.7% while the book mid was 78.5-89.5c; FAV bought 1,121 (cons) to 1,200 (stress) contracts at 76-89c, the market's reference, and the strike failed. 26OCT1622 settled 78.44: T78.99 YES at 98-99.7% (model) against an 89.5-94.5c mid, 856-1,200 contracts at 88-93c. A favorite join priced at the market's reference buys the market's view; when the model is the overconfident one it keeps buying.
- Smaller size (q_fav 100) scales the loss down about in proportion (FAV_night_q100 -$515 cons / -$523 stress); a stricter threshold (fav_min_p 0.99) does not help (-$672 / -$894): the failing strikes were at 98-99.7%.
- Stress costs FAV_night only $164 more than cons: most stress fills land in normal hours, where the favorite wins and each fill earns 1-3c (stress fills 408 contracts an hour against cons' 258), and the upset hours were already filled heavily under cons.
- G (with and without the pad hold) loses under swp and stress; its cons profit is the old model's blind spot.

### Market reaction to our live orders (`temp_shadow_audit.py reaction`)

What other makers did after our orders took effect on 26OCT1610 (others' size only: our orders are subtracted), against the same measures after other makers' comparable orders in the 35 hours before (the baseline):

| | within 2 s | 5 s | 12 s | 30 s |
|---|---|---|---|---|
| our 1c pads (8 that opened an empty 1c level): others joined (>= 10) | 25% | 50% | 62% | 88% (mean +382) |
| baseline (181 fresh pads by others on empty longshot levels) | 22% | 32% | 40% | 53% (mean +483) |
| a new 99c bid opposite our pad (a locker) | 0% | 0% | 0% | 12% (1 of 8) |
| baseline | 0% | 1% | 2% | 4% |
| our favorite joins (160 placements and price moves >= 50c): a new external bid above our price | 36% | 42% | 48% | 51% |
| baseline (3,944 joins of >= 100 at a favorite side's reference) | 11% | 16% | 22% | 30% |
| our favorite joins: others joined our price (>= 10) | 28% | 32% | 38% | 45% |
| baseline | 11% | 20% | 28% | 36% |
| our favorite joins: the size that was at our price fell by half | 16% | 23% | 30% | 40% |

What the shadow cannot see, and the bias:
- Joiners at our pads: replayed pads rest alone. The shadow therefore credits filler variants with the whole side's 1c reward (overstated) and leaves their sweeps to the hidden-sweep hazard, which is about 1.9x too low (above). Every filler variant is worse than its swp number.
- Others stepping ahead of our favorite joins, three times the baseline rate within 2 s: a better bid moves the reward reference up (our order at half weight or less until we re-price, debounced 2 s, at the back of the queue) and takes the calm flow first. The shadow keeps FAV at the reference with full weight and its queue place, so it overstates FAV's reward and its normal-hour fills; the clearing sellers of an upset take both orders, so the losses stay. FAV live would do worse than its shadow.
- Others leaving our price after we joined: undecided. The size that was at our price halved within 2 s after 16% of our joins (40% within 30 s), but the baseline version of that measure is confounded by the baseline joiners' own re-placing, so there is no clean comparison. If the makers ahead of us leave or re-place, we move up the queue, which the cons queue rule cannot see: the one live favorite fill (nearly first in a 578-contract level that cons had us 278 deep in) points that way. More fills than the shadow books, in both the winning and the losing hours.
- 99c lockers: one in eight pads, rare; swp's crossing channel only sees recorded 99c bids.

### What live evidence would make a shadow result trustworthy

- 1c fillers: none more is needed. The live hour already contradicts opt / cons, and the shadow's swp, in the regime it replays every other hour in, predicts half of the fills that happened. Every filler variant loses under swp and stress.
- Favorite joins, the fill model: FAV_night fills 975 (opt) / 258 (cons) / 408 (stress) contracts a night hour, with a per-hour coefficient of variation of 1.6-1.8. Auditing every live hour (`temp_shadow_audit.py audit`: the bot's actual orders against each model), about 10 live hours separate opt from cons (3.8x apart) and 50-60 hours separate cons from stress (1.6x apart), at two standard errors, at q_fav 300. At q_fav 100 cons and stress are only 1.28x apart (about 200 hours). The one live favorite fill (102 contracts at 96c, where cons predicted 0) says the queue rule may be too pessimistic; one fill does not decide it.
- Favorite joins, the P&L: the hourly sd is $220-260 against a median of +$7-9; the mean is set by how often the model is confidently wrong. At q_fav 300 an upset hour costs $900-1,200 against +$11-16 in a normal night hour, so break-even needs at most about one upset hour in 70-100. Live P&L cannot test that in reasonable time (ruling out the observed -$45 an hour takes about 100 hours; establishing +$15 an hour about 1,000). The shadow books those losses without trading (cons already does), so the frequency can be counted there: no FAV upset hour in the next 200 recorded hours would bound it under 1.5% (95%), about break-even; five or more in the next 100 hours would confirm the loss at 95% (the recorded rate, 4 in 63 hours, predicts about 6).
- So before trusting a favorite-join shadow result: at least about 200 recorded hours with the stress column positive and not flipped by its sensitivities, with the upset-hour frequency counted; then a small audited live run (q_fav 100, its hours only, a loss cap) of at least 50 hours, accepted only if the actual favorite fills fall inside the opt-to-stress range of the audit. A rule that keeps the favorite joins out of hours where the model and the market disagree (both losing night hours) would have to earn that record out of sample: it would be fitted on these four hours.

### Commands

```
python temp_shadow_audit.py --out <dir> audit --event KXTEMPMIAH-26OCT1610 [--draws 30]   # audit_<EVENT>.txt / .json
python temp_shadow_audit.py --out <dir> clears [--prints <prints.json>]                   # clears.txt / .json
python temp_shadow_audit.py --out <dir> reaction --event KXTEMPMIAH-26OCT1610             # reaction_<EVENT>.txt / .json
python temp_shadow_audit.py --out <dir> stress-table [--day ...] [--variants ...] [--sens hole,size,nofav]
python temp_shadow_sweep.py --out <dir> run --day ... --fill-models opt,cons,swp,stress [--stress-take size] [--stress-hole] [--no-favhaz]
python temp_shadow_sweep.py --out <dir> calibrate-fav                                   # favhazard.json (repo copy: temp_shadow_sweep_favhazard.json)
```

`--out` defaults to `TEMP_SHADOW_AUDIT_OUT`, else `<TEMP_SHADOW_DATA>\audit`. The 2026-10-17 runs used a copy of the recording and the executor's 10/16 logs in the session scratchpad (`TEMP_SHADOW_DATA`, `TEMP_MM_LIVE_DIR`); KL-data was only read.

## Pre-registered tests (written 2026-10-17 ~15:00 ET, before any hour of their windows)

Jack 2026-10-17 adopted these go-live rules ("Yes. And Assume rewards are paid"):
1. Rewards are assumed paid to the account that quotes.
2. Decisions use the STRESS fill model (temp_shadow_sweep `stress`: swp plus level-clear at every price).
3. A variant passes only on hours AFTER it was chosen.
4. Any live pilot runs at 1/3 size, night hours only, with a $300 daily halt, an hourly live-vs-shadow reconciliation, and an auto-halt after 2 consecutive hours below the shadow's band.
5. Scale up only if the pilot earns at least 50% of the shadow's prediction.

Jack 2026-10-17 then chose "1": keep live halted, fix the report, and pre-register the agreement rule below, to be tested only on future hours.

### Test A — FAV_night (rule 3)
- **Window:** ET days 2026-10-17 through 2026-10-21 (5 days).
- **PASS iff all of:**
  - the window's total stress net is > 0;
  - at least 4 of the 5 days have stress net > 0;
  - the calibrated-tails expectation is > 0.
- The total excluding the best and worst hour is reported for information.

### Test B — FAV_night_agree (new rule; chosen from the 10/16 21:00 and 22:00 ET losses, so it is judged ONLY on later data)
- **Definition:** FAV_night plus policy key `fav_market_min_p = 0.95`. A favorite join is placed only when the MARKET also prices the strike as a favorite, i.e. market favorite probability ≥ 0.95, computed on the external book at decision time:
  - **Both sides have resting bids:** mid = (best favorite bid + (100 − best longshot bid)) / 200.
  - **Longshot side empty (locked or one-sided):** (best favorite bid + 1) / 100.
  - **Favorite side empty:** no join.
- The model condition is unchanged: favorite fair ≥ `fav_min_p` 0.97.
- **Window:** ET days 2026-10-18 through 2026-10-22 (5 days).
- **PASS iff all of:**
  - the window's total stress net is > 0;
  - at least 4 of the 5 days have stress net > 0;
  - the total excluding the variant's best and worst hour is > 0;
  - the calibrated-tails expectation is > 0 (reported next to a recent-regime version, because the tail table comes from a calmer period).

### Both tests
- Nothing is re-tuned on window data. A changed rule needs a new pre-registered window.
- A PASS only makes the variant eligible for the rule-4 pilot. It does not start trading.
- The verdicts appear in the daily report: "in progress (n/5 days)" until the window completes, then PASS or FAIL.
