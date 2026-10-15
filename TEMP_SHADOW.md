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
| `temp_shadow_sim.py` | Replay engine: policies, variants, fill models, reward accrual, settlement P&L. |
| `temp_shadow_report.py` | Daily report (HTML email) plus the week summary and rule-based tweak suggestions. |
| `run_temp_shadow_collector.ps1` / `run_temp_shadow_collector_hidden.vbs`, `run_temp_shadow_report_hidden.vbs`, `register_temp_shadow_collector.ps1`, `register_temp_shadow_report.ps1` | Windows tasks. |
| `test_temp_shadow.py` | Tests. They always sandbox `TEMP_SHADOW_DATA` to a temp dir, never the live data root. |

**Data:** `C:\Users\jackd\Documents\KL-data\temp-shadow\` (env `TEMP_SHADOW_DATA`)

```
raw/<UTC day>/{ws,wx,meta}_<HH>.jsonl[.gz]   recording (shapes in temp_shadow_common.py)
settled/<EVENT>.json                         results, expiration_value, REST trades, official index
backfill/                                    history pulls (wx/, settled/, iem/, study/)
model/model_params.json                      fitted model
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

**Fill models:** `opt` and `cons` run as separate passes, because fills change inventory.

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

**Variant grid:** the starting grid is in `temp_shadow_sim.VARIANTS`.

## Daily report (08:30 ET, previous ET day, plus cumulative)

**Variant table:** reward, fills, contracts, trading P&L and net per variant, for both `opt` and `cons`. The headline is the best `cons` variant.

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

**Suggested tweaks:** rule-based, from the variant deltas and diagnostics.

**Week summary:** on and after day 7, with a go/no-go and a recommended starting configuration.

**Delivery:** emailed via `send_html_email.py` (ALERT_EMAIL_* env). The HTML is kept in reports/.

## Tasks

| task | trigger | does |
|---|---|---|
| `KL temp-shadow collector` | at logon (+1 min), and every 10 min with IgnoreNew, no time limit | `run_temp_shadow_collector.ps1` restart loop |
| `KL temp-shadow report` | daily 08:30 ET, StartWhenAvailable, 2 h limit | sim the previous ET day, build the report, email it |

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
FILL_MODELS = ("opt", "cons")
def run_event(event_ticker, variants=None, fill_models=FILL_MODELS, model=None) -> dict
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
