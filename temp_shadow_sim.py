#!/usr/bin/env python3
"""Replay engine for the hourly-temp shadow test (TEMP_SHADOW.md "Simulator").

Jack 2026-10-14: "build it and shadow test for a week, reporting results and
suggestion tweaks daily". This module replays candidate quoting policies for
KXTEMPMIAH against the recording (ws books/trades, station feed, programs) and
scores, per policy variant and fill model, what the policy would have earned in
liquidity rewards and won or lost on fills. It never places an order.

Public API (the Interfaces contract):
    DEFAULT_POLICY, VARIANTS, FILL_MODELS ("opt", "cons", "swp": every variant),
    RUN_FILL_MODELS (+ "stress" for the STRESS_SCOPE variants)
    run_event(event_ticker, variants=None, fill_models=RUN_FILL_MODELS, model=None,
              until_ts=None, draws=None) -> dict          (sim output schema)
    run_day(et_day, variants=None, force=False, workers=None, deadline=None)
        -> List[dict]
CLI:
    python temp_shadow_sim.py day --day YYYY-MM-DD [--force] [--workers N]
    python temp_shadow_sim.py event --event EVT [--variants A,B] [--write]

Fill models (Jack 2026-10-16, "1": "ship the fixes + the sweep-aware report
column"): opt and cons below, and swp = cons plus sweepers
(temp_shadow_sweep, validated on the live hour 26OCT1610): recorded taker
clusters at a side's 1c level fill our 1c orders (all of them when the taker
cleared the level, else price-time priority behind the external 1c size);
hidden IOC sweeps arrive at the calibrated hazard while we rest alone at 1c
(Monte Carlo, SWP_DRAWS draws with fixed seeds, the same arrivals for every
variant); a new 99c bid opposite crosses our 1c. "<variant>|swp" has the opt /
cons schema (means over the draws) plus draws, net_sd, net_se (the Monte
Carlo standard error of net), draw_net, filler_contracts, sweep_src,
haz_events, swp_exact (a variant that never rests a 1c order is exact in one
draw) and net_cal / net_cal_tlo / net_cal_thi (an ESTIMATE: every fill marked
at the realized longshot rate of its model bucket given a taker hit a bid at
that price, temp_shadow_sweep.TailCal; a 10-90% fair keeps the model's own
value). Hazard: <TEMP_SHADOW_DATA>/model/sweep_hazard.json if present, else
the repo's temp_shadow_sweep_hazard.json (the 10/16 pre-live fit); tails the
same with sweep_tails.json / temp_shadow_sweep_tails.json. Recalibrate:
temp_shadow_sweep.py calibrate, then export --hazard <out>/hazard.json --to
<TEMP_SHADOW_DATA>/model/sweep_hazard.json (TEMP_SHADOW.md); the cache key
holds the hazard / tails identities and the draw count, so a new table
re-sims the window. The output's top-level "swp" block names them, with
hazard_kind / tails_kind ("repo" | "override") and hazard_rejected /
tails_rejected. An override that is not a usable table (no non-empty
'hazard' fit, lam_max <= 0, no size draws; tails without bucket rows) is
REJECTED: the repo default is used and the rejection is written there (the
report flags it). swp is contained (fix pass 10/16): if the sweep path
raises, the event keeps its opt / cons results, has no "|swp" keys and a
top-level swp_error, and run_day re-sims it next time (_cached_ok wants
every fill model).

Fill model stress (SIM_VERSION 7; Jack 2026-10-17, "1": the adopted go-live
rules decide on it, TEMP_SHADOW.md "Pre-registered tests") = swp plus
temp_shadow_sweep's level-clear channel (d) on our lots at >= 2c and the
hidden favorite takers (e): "<variant>|stress" in the swp schema (means over
the same SWP_DRAWS draws, the 1c hazard candidates on SWP_SEED as in swp,
the favorite-taker candidates on temp_shadow_sweep.FAVHAZ_SEED -- the audit's
stress-table draws exactly), plus stress_exact, clear_events, favhaz_events,
above1c_contracts and fav_fills (favorite-side fills by fair bucket:
contracts, cost, table-marked P&L). The favorite-taker table is
<TEMP_SHADOW_DATA>/model/sweep_favhazard.json if usable, else the repo's
temp_shadow_sweep_favhazard.json (calibrate-fav, 10/17); the top-level
"stress" block names it (fav_hazard_id ...). Which variants: STRESS_SCOPE
(env TEMP_SHADOW_STRESS_SCOPE) "all", or "nolive" = the variants that quote
no live strikes (live=False: G, G0, I, J, L and the FAV family), the only
ones the decision rules use; the block's "variants" lists them. Contained
like swp: a failure costs stress only (stress_error, no "|stress" keys; the
next run_day re-sims). Two exact shortcuts as in swp (checked against
running every draw): a variant whose first draw rests no 1c order and no
favorite lot alone at the top is exact in one draw (stress_exact); one whose
first draw accepted no hidden sweep and no hidden favorite taker re-runs a
later draw only if one of its candidates would be accepted along the
logged trajectory (temp_shadow_sweep.hazard_hits / fav_hazard_hits).
Every event also gets "fav_hits" (temp_shadow_sweep.fav_hit_summary: the
taker clusters that hit a favorite side's bids at >= 90c, with the
favorite's outcome) -- the report's recent-regime tail rates.

Mechanics (all times are exchange/epoch seconds; our rx ~ exchange time):
  * Ticks: one per second from open to close. Tick i sits at T_i = sec0+i+0.5,
    the instant of book entry i (EventStream.books_per_second: book as of
    rx <= sec0+i+0.5). At each tick we first SCORE the orders active at T_i
    (reward accrues pool_per_sec * score() inside the program window), then
    DECIDE new quotes from book i; the orders change at T_i + order_latency_s.
  * Weather: a wx row received at rx is acted on at rx + react_s (an extra,
    sub-second decision round). The fair is FairModel.fair on
    WxTape.snapshot(t - react_s) over rows from open - WX_LOOKBACK_S (75 min:
    the model's features use 60 min of history), recomputed on every new
    snapshot (any applied row) and kept when fair or g changed;
    fairs/books/stats are computed once per event and shared by all variants.
    g (the guard's price slope, cents per 0.36 F) = FairModel.slopes(): the
    fat-tail mixture's density at the same lattice cut(s) fair() uses; a
    model without slopes() falls back to the spec's plain normal on dist().
    "The index changed" (fills_after_wx) = the snapshot's five-station
    estimate est_latest moved (index_latest when a model has none): the
    4-station pending replica vs the 5-station value is composition, not
    weather.
  * Orders are per (market, side, price) lists of lots [remaining, queue_ahead].
    The bot's decisions are diffs against what it BELIEVES rests = actual
    resting size with its in-flight ops applied (fills are known instantly,
    so a fill re-projects the belief): same price keeps queue; a size cut is
    relative (reduce_by: trims that many, or what is left, from the back); a
    size increase appends a lot at the back (queue_ahead = book size at that
    price at activation, the book excluding us); a new price is a new order.
  * Fills: taker_side 'no' (taker bought NO = sold YES) hits YES bids: our bid
    b fills when b > yes_cents (fill min(remaining, count)) or b == yes_cents
    (opt: same; cons: the count first eats queue_ahead). taker_side 'yes' hits
    NO bids: our NO bid q (= YES ask 100-q) fills when 100-q < yes_cents or ==
    (queue rule). Fill price = our price. In 'cons' a lot's queue_ahead also
    shrinks to the book size at its price (cancels ahead of us).
  * Trade time = Trade.ts; when ts is a whole second (the WS trade 'ts') and
    rx lands within 3 s of it, the sub-second time is taken as rx - 0.05.
  * Settlement: YES pays $1 if result yes, NO pays $1 if result no; trading
    P&L = payoff - cost. Unsettled markets (run_event only) are marked at the
    last book mid and the output says settled=false.
  * Mark-outs at +60/+300 s use the book mid (clamped to the last book before
    close), in cents per contract, signed in our favour.
  * Policy: every quoting decision below (classes, prices, guard, window,
    pads, skew, risk sizing, the order diff) is temp_mm_policy's, the
    same code the live executor runs; the sim feeds it the replayed book,
    fair, guard slope and filled positions.
  * Risk (pre-trade, on filled positions): per-market |net YES position| <=
    pos_cap (the side that adds is trimmed to the room left); event worst-case
    loss over the nested outcome scenarios: each side's resting orders are
    trimmed so that, filled in full, they keep the loss in that side's losing
    scenarios <= event_wcl_cap (a bid at p risks p cents a contract), and a
    side whose losing scenarios are already at the cap stops. Orders on
    different strikes are sized independently, so a simultaneous burst across
    strikes can still overshoot (by at most the other strikes' headroom).
  * Classes: live = fair 10-90c (P.live); near = 3-10% / 90-97%, quoted as
    live at half size only when P.decided and P.near_decided_half (spec
    "Decided strikes"), labelled "decided" in by_class; the rest decided.
  * Quote hours (P.quote_hours_et, FAV_night): an event whose settlement's
    ET hour (temp_mm_policy.quote_hour_ok on the ticker's hour, DST-safe) is
    not listed rests nothing all hour -- every market fails the program test,
    the live executor's rule.
  * Pad hold (P.pad_hold_open_s, G since 10/16): a decision whose orders
    would start resting (t + order_latency_s) before the event's open +
    the hold rests no 1c longshot / qualification pads (pol.pad_hold_flag,
    the live executor's test); the flag is part of the per-market decision
    key, so the pads go up at the first decision (a tick or a station row)
    whose orders start resting at or after open + hold: with a 180 s hold
    they are active by open + 180.65 s (the hh:03:00.5 tick + 0.15 s).

Definitions used in the output beyond the schema text:
  quoted_secs  program-window strike-seconds (trusted book) with >= 1 order.
  avg_share    mean score() fraction over those quoted strike-seconds.
  max_abs_pos  peak |net YES contracts| on any one market.
  worst_case_loss  peak event worst-case loss (dollars) during the hour.
  program_secs longest program window (seconds) among the event's strikes.
  wx_rows      wx rows received in [open, close); wx_rows_lookback = rows
               loaded from the 75 min before open (flag wx_history_short if
               none reach 55 min before open).
  depth3c_median   median over strike-seconds of the mean per-side depth
                   within 3c of that side's best bid (book without us).
Extra keys (top level): policies, flags, runtime_s, sim_version, fill_models,
swp (draws, rec_mode, channels, seed, hazard_id / hazard_src / hazard_built /
lam_max, tails_id / tails_src), stress (draws, seed, fav_seed, fav_hazard_id
/ _src / _kind / _rejected / _built, fav_lam_max, stress_version, take, hole,
scope, variants), fav_hits; coverage adds fair_missing_frac, programs_src
and wx_rows_lookback. run_day reuses a sim file only when it holds every
requested variant x fill model with the same overrides (stress: the
variants in STRESS_SCOPE), this SIM_VERSION and the same swp / stress
configuration (draws, hazard, tails, favorite-taker table, scope).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
import traceback
from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import temp_mm_policy as pol
import temp_shadow_common as C
import temp_shadow_model as tmodel
import temp_shadow_scoring as scoring

SIM_VERSION = "7"   # 2: 75-min wx lookback, fair per wx update, relative size
                    # cuts + projected belief, near strikes only with decided,
                    # pre-trade worst-case-loss sizing (10/14 review fixes)
                    # 3: guard buffer from FairModel.slopes (mixture slope);
                    # fills_after_wx keyed on the five-station estimate
                    # 4: scoring reads 3 h back for each market's
                    # snapshot (early-listed events, 10/15)
                    # 5: pad_hold_open_s (G holds its 1c pads for the
                    # first 180 s; G0_nohold = the old G), 10/16; also
                    # covers scoring's live-order subtraction (temp-sweep,
                    # merged with it: our live orders out of the books and
                    # tape from 10/16 13:00Z), so the 10/16 files simmed
                    # at 4 without it re-sim
                    # 6: the swp fill model for every variant (Monte Carlo,
                    # temp_shadow_sweep), FAV_only / FAV_night and
                    # quote_hours_et (Jack 2026-10-16, "1"); opt / cons
                    # numbers of the earlier variants unchanged
                    # 7: the stress fill model (STRESS_SCOPE variants),
                    # fav_hits, FAV_night_agree and the policy key
                    # fav_market_min_p (Jack 2026-10-17, "1"); opt / cons /
                    # swp numbers of the earlier variants unchanged

# The policy lives in temp_mm_policy (the live executor runs the same code);
# re-exported here for the report and older callers.
DEFAULT_POLICY = pol.DEFAULT_POLICY
VARIANTS = pol.VARIANTS
STRESS = "stress"
FILL_MODELS = ("opt", "cons", "swp")      # every variant
RUN_FILL_MODELS = FILL_MODELS + (STRESS,)   # what run_event / run_day write: stress for
                                            # the STRESS_SCOPE variants
# stress for "all" variants or the "nolive" ones (live=False: what the
# decision rules use); TEMP_SHADOW.md "Daily pipeline stress" has the runtime
STRESS_SCOPES = ("all", "nolive")
STRESS_SCOPE = (os.environ.get("TEMP_SHADOW_STRESS_SCOPE") or "nolive").strip().lower()
STRESS_TAKE, STRESS_HOLE = "full", False     # the audit's base stress model
STRESS_FAVHAZ_REPO = os.path.join(C.REPO, "temp_shadow_sweep_favhazard.json")
# swp (temp_shadow_sweep): Monte Carlo draws per event. Measured 10/16 on the
# report task's 2 workers (a 22-event day: 398 s at 6 draws, 485 s at 8): a
# full re-sim of the report's longest window (10/15-10/23, 200 events, after
# a SIM_VERSION / grid / swp-table change) takes ~60 min at 6 against the
# task's 2 h limit (~73 min at 8); TEMP_SHADOW.md "Fill models". The Monte
# Carlo standard error is in every swp result (net_se).
SWP_DRAWS = int(os.environ.get("TEMP_SHADOW_SWP_DRAWS", "6") or 6)
SWP_SEED = "tmm-sweep-1"            # = temp_shadow_sweep.SEED_BASE
SWP_REC_MODE = "level"              # recorded takers: price-time priority
SWP_CHANNELS = "abc"                # recorded clusters, hidden sweeps, 99c crossers
SWP_HAZARD_REPO = os.path.join(C.REPO, "temp_shadow_sweep_hazard.json")
SWP_TAILS_REPO = os.path.join(C.REPO, "temp_shadow_sweep_tails.json")

TAU_BUCKETS = ("0-5", "5-10", "10-20", "20-40", "40-60")
# wx rows for [open - 75 min, close]: the model's features use the last 60
# minutes of history (rv over 5-min diffs, the >= 45-min anomaly term, the
# 15-min trend), the latest minute lands ~2.5-5 min late, and the five-station
# fill estimate looks back 5 more minutes. WxTape is causal (rx <= at_rx).
WX_LOOKBACK_S = 4500.0
WX_PREOPEN_MARGIN_S = 60.0     # fair states before open - this are not kept
FILL_WX_WINDOW_S = 3.0         # fills_after_wx window after an index change
LOGLOSS_EPS = 0.005
DEFAULT_PROGRAM = dict(start_after_open=120.0, reward=100.0, target=1000.0, df=0.5)
MAX_WORKERS = 4
DEFAULT_WORKERS = int(os.environ.get("TEMP_SHADOW_SIM_WORKERS", "2") or 2)

_TICK, _TRADE, _WX = 1, 0, 2          # event kinds (sort order at equal time)


def tau_bucket(tau_min: float) -> str:
    if tau_min < 5:
        return "0-5"
    if tau_min < 10:
        return "5-10"
    if tau_min < 20:
        return "10-20"
    if tau_min < 40:
        return "20-40"
    return "40-60"


_phi = pol._phi          # kept for older callers
_floor = pol._floor


def _r(x, nd=6):
    return round(float(x), nd) if x is not None else None


def _by_strike(d: dict, k: float):
    """d[k] for a float strike key, tolerating float-key drift."""
    v = d.get(k)
    if v is None:
        for kk, vv in d.items():
            try:
                if abs(float(kk) - k) < 1e-6:
                    return vv
            except (TypeError, ValueError):
                continue
    return v


# --------------------------------------------------------------------------
# Event inputs (market meta, programs, results)
# --------------------------------------------------------------------------

class EventInputs:
    def __init__(self, event: str):
        self.event = event
        self.series = event.split("-")[0]
        self.settle_ts: Optional[float] = None
        self.open_ts: Optional[float] = None
        self.close_ts: Optional[float] = None
        self.tickers: List[str] = []
        self.strikes: List[float] = []
        self.results: Dict[str, str] = {}
        self.settlement_value: Optional[float] = None
        self.settled = False
        self.programs: Dict[str, dict] = {}
        self.programs_src = "none"
        self.flags: List[str] = []
        self.settled_doc: Optional[dict] = None


def _latest_meta_markets(event: str, t0: float, t1: float) -> Optional[List[dict]]:
    best, best_rx = None, -1.0
    for rec in C.iter_raw("meta", t0, t1):
        if rec.get("kind") != "markets":
            continue
        d = rec.get("data") or {}
        if d.get("event_ticker") != event:
            continue
        rx = float(rec.get("rx") or 0)
        if rx >= best_rx and d.get("markets"):
            best, best_rx = d.get("markets"), rx
    return best


def _program_params(p: dict) -> Optional[dict]:
    st = C.parse_iso(p.get("start_date") or "")
    en = C.parse_iso(p.get("end_date") or "")
    if st is None or en is None or en <= st:
        return None
    pr = float(p.get("period_reward") or 0.0)
    tgt = C.fp(p.get("target_size_fp")) or DEFAULT_PROGRAM["target"]
    dfb = p.get("discount_factor_bps")
    df = float(dfb) / 10000.0 if dfb is not None else DEFAULT_PROGRAM["df"]
    return dict(start=st, end=en, pool=pr / 10000.0 / (en - st), target=tgt,
                df=df, reward=pr / 10000.0, src="program", id=p.get("id"))


def load_event_inputs(event: str) -> EventInputs:
    inp = EventInputs(event)
    pe = C.parse_event(event)
    S = pe[1] if pe else None
    sdoc = None
    for sub in (("settled",), ("backfill", "settled")):   # same order as scoring
        sdoc = C.read_json(os.path.join(C.DATA_ROOT, *sub, f"{event}.json"))
        if isinstance(sdoc, dict):
            break
    inp.settled_doc = sdoc if isinstance(sdoc, dict) else None
    meta_mk = None
    if S is not None:
        meta_mk = _latest_meta_markets(event, S - 4 * 3600, S + 3 * 3600)
    s_mk = (inp.settled_doc or {}).get("markets") or []
    markets = {}
    for src in (meta_mk or [], s_mk):        # settled (later) overrides meta
        for m in src:
            t = m.get("ticker")
            if t and C.event_of(t) == event:
                markets.setdefault(t, {}).update({k: v for k, v in m.items()
                                                  if v not in (None, "")})
    if not markets:
        raise ValueError(f"{event}: no market meta (meta stream or settled file)")
    rows = []
    for t, m in markets.items():
        k = m.get("floor_strike")
        k = float(k) if k not in (None, "") else C.strike_of(t)
        if k is None:
            continue
        rows.append((k, t, m))
    rows.sort()
    inp.strikes = [k for k, _, _ in rows]
    inp.tickers = [t for _, t, _ in rows]
    opens = [C.parse_iso(m.get("open_time") or "") for _, _, m in rows]
    closes = [C.parse_iso(m.get("close_time") or "") for _, _, m in rows]
    opens = [x for x in opens if x]
    closes = [x for x in closes if x]
    inp.close_ts = max(closes) if closes else S
    inp.open_ts = min(opens) if opens else (inp.close_ts - 3600 if inp.close_ts else None)
    if inp.close_ts is None or inp.open_ts is None:
        raise ValueError(f"{event}: no open/close time")
    inp.settle_ts = S if S is not None else inp.close_ts
    if S is not None and abs(S - inp.close_ts) > 1:
        inp.flags.append(f"close_time_vs_ticker_{int(inp.close_ts - S)}s")
    # results
    ev_vals = []
    for _, t, m in rows:
        r = str(m.get("result") or "").lower()
        if r in ("yes", "no"):
            inp.results[t] = r
        ev = m.get("expiration_value")
        if ev not in (None, ""):
            try:
                ev_vals.append(float(ev))
            except (TypeError, ValueError):
                pass
    inp.settled = len(inp.results) == len(inp.tickers) and bool(inp.tickers)
    inp.settlement_value = ev_vals[0] if ev_vals else None
    if inp.settled and not s_mk:
        inp.flags.append("results_from_meta")
    # programs: meta rows (each holds the full active set), settled fallback
    found: Dict[str, dict] = {}
    tickset = set(inp.tickers)

    def _take(plist, src):
        for p in plist or []:
            t = p.get("market_ticker")
            if t not in tickset:
                continue
            pp = _program_params(p)
            if pp is None:
                continue
            pp["src"] = src
            cur = found.get(t)
            ov = min(pp["end"], inp.close_ts) - max(pp["start"], inp.open_ts)
            if cur is None or ov > cur["_ov"] + 1e-6 or (
                    abs(ov - cur["_ov"]) <= 1e-6 and pp.get("id") == cur.get("id")):
                pp["_ov"] = ov
                found[t] = pp
    for rec in C.iter_raw("meta", inp.open_ts - 2 * 3600, inp.close_ts + 3600):
        if rec.get("kind") == "programs":
            _take((rec.get("data") or {}).get("programs"), "meta")
    if len(found) < len(inp.tickers) and inp.settled_doc:
        _take(inp.settled_doc.get("programs"), "settled")
    srcs = set()
    for t in inp.tickers:
        p = found.get(t)
        if p is None:
            st = inp.open_ts + DEFAULT_PROGRAM["start_after_open"]
            en = inp.close_ts
            p = dict(start=st, end=en, pool=DEFAULT_PROGRAM["reward"] / max(1.0, en - st),
                     target=DEFAULT_PROGRAM["target"], df=DEFAULT_PROGRAM["df"],
                     reward=DEFAULT_PROGRAM["reward"], src="default")
            inp.flags.append(f"program_default:{t}")
        p.pop("_ov", None)
        srcs.add(p["src"])
        inp.programs[t] = p
    inp.programs_src = "|".join(sorted(srcs)) if srcs else "none"
    return inp


# --------------------------------------------------------------------------
# Shared per-event state (books, fairs, trades) -- computed once
# --------------------------------------------------------------------------

def _trade_time(tr) -> Optional[float]:
    ts = tr.ts
    if ts is None:
        return float(tr.rx) if tr.rx is not None else None
    ts = float(ts)
    if ts > 1e11:                       # ms by mistake
        ts /= 1000.0
    rx = tr.rx
    if rx is not None and abs(ts - round(ts)) < 1e-9 and 0.0 <= float(rx) - ts < 3.0:
        return min(max(float(rx) - 0.05, ts), ts + 0.999)
    return ts


class _BookInfo(pol.BookView):
    """The policy's BookView (what the quoting rules read) plus the sim's
    market stats."""
    __slots__ = ("paying", "locked99", "depth3")

    def __init__(self, book, target, df):
        by, bn = book
        pol.BookView.__init__(self, by, bn, target)
        self.paying = bool(scoring.score(by, bn, {}, {}, target, df)[1])
        self.locked99 = (self.best_y is not None and self.best_y >= 99) or (
            self.best_n is not None and self.best_n >= 99)
        dy = sum(s for p, s in by.items() if p >= self.best_y - 3) if by else 0.0
        dn = sum(s for p, s in bn.items() if p >= self.best_n - 3) if bn else 0.0
        self.depth3 = (dy + dn) / 2.0


class _Shared:
    """Everything variant-independent for one event."""

    def __init__(self, inp: EventInputs, model, until_ts: Optional[float]):
        self.inp = inp
        self.tickers = inp.tickers
        self.strikes = inp.strikes
        self.nm = len(inp.tickers)
        self.mindex = {t: i for i, t in enumerate(inp.tickers)}
        self.open = float(inp.open_ts)
        self.close = float(inp.close_ts)
        self.sec0 = int(math.floor(self.open))
        self.sec1 = int(math.ceil(self.close))
        self.n = max(0, self.sec1 - self.sec0)
        self.end_t = self.close if until_ts is None else min(self.close, float(until_ts))
        self.until_ts = until_ts
        self.flags: List[str] = []
        self.progs = [inp.programs[t] for t in inp.tickers]
        self._load_books()
        self._load_wx(model)
        self._react_cache: Dict[float, tuple] = {}

    # -- books / trades -------------------------------------------------
    def _load_books(self):
        nm, n = self.nm, self.n
        self.stream_cov: dict = {}
        trades = []
        bps = {}
        try:
            # load_event_stream reads its own snapshot lookback before t0 and
            # counts coverage / keeps trades over [t0, t1)
            stream = scoring.load_event_stream(self.inp.event, self.open, self.close)
            bps = stream.books_per_second(self.sec0, self.sec1) or {}
            trades = list(getattr(stream, "trades", None) or [])
            self.stream_cov = dict(getattr(stream, "coverage", None) or {})
        except Exception as e:                       # no recording -> dark event
            self.flags.append(f"stream_error:{type(e).__name__}:{str(e)[:120]}")
        self.ver: List[List[int]] = []
        self.vinfo: List[List[_BookInfo]] = []
        trusted = 0
        for m, t in enumerate(self.tickers):
            lst = list(bps.get(t) or [])
            if len(lst) < n:
                lst.extend([None] * (n - len(lst)))
            elif len(lst) > n:
                lst = lst[:n]
            pr = self.progs[m]
            vers, infos = [], []
            prev, pv = None, -1
            for b in lst:
                if b is None:
                    vers.append(-1)
                    prev, pv = None, -1
                    continue
                trusted += 1
                if prev is not None and (b is prev or (b[0] == prev[0] and b[1] == prev[1])):
                    vers.append(pv)
                    continue
                infos.append(_BookInfo(b, pr["target"], pr["df"]))
                pv = len(infos) - 1
                vers.append(pv)
                prev = b
            self.ver.append(vers)
            self.vinfo.append(infos)
        self.book_trusted_frac = trusted / float(nm * n) if nm and n else 0.0
        # trades inside the market's life, in time order
        tt = []
        for tr in trades:
            m = self.mindex.get(getattr(tr, "ticker", None))
            if m is None:
                continue
            ts = _trade_time(tr)
            if ts is None or ts < self.sec0 or ts > self.close:
                continue
            side = str(tr.taker_side or "").lower()
            if side not in ("yes", "no"):
                continue
            try:
                yc = int(round(float(tr.yes_cents)))
                cnt = float(tr.count)
            except (TypeError, ValueError):
                continue
            if cnt <= 0 or not (0 < yc < 100):
                continue
            tt.append((ts, m, side, yc, cnt))
        tt.sort(key=lambda x: x[0])
        self.trades = tt
        self.n_trades_all = len(tt)
        # last-known mid per tick (for mark-outs / unsettled marks)
        self.lastmid: List[List[Optional[float]]] = []
        for m in range(nm):
            out, last = [], None
            vers, infos = self.ver[m], self.vinfo[m]
            for i in range(n):
                v = vers[i]
                if v >= 0 and infos[v].mid is not None:
                    last = infos[v].mid
                out.append(last)
            self.lastmid.append(out)

    def idx_at(self, t: float) -> int:
        """Latest book entry known at time t (entry i is as of sec0+i+0.5)."""
        return int(math.floor(t - self.sec0 - 0.5))

    def mid_at(self, m: int, t: float) -> Optional[float]:
        if not self.n:
            return None
        i = self.idx_at(t)
        if i < 0:
            return None
        i = min(i, self.n - 1)
        return self.lastmid[m][i]

    # -- weather / fairs --------------------------------------------------
    def _load_wx(self, model):
        city = C.CITY_OF_SERIES.get(self.inp.series, "miami")
        rows = []
        for rec in C.iter_raw("wx", self.open - WX_LOOKBACK_S, self.close):
            if rec.get("city", city) == city and rec.get("rx") is not None:
                rows.append(rec)
        # coverage: rows received during the market's life (the lookback rows
        # are counted separately)
        self.wx_rows = sum(1 for r in rows if float(r["rx"]) >= self.open)
        self.wx_rows_lookback = len(rows) - self.wx_rows
        self.st_rx: List[float] = []
        self.st_f: List[List[Optional[float]]] = []
        self.st_g: List[List[Optional[float]]] = []
        self.idx_change_rx: List[float] = []
        if not rows:
            self.flags.append("no_wx_rows")
            return
        if not any(float(r["rx"]) < self.open - 3300.0 for r in rows):
            # the model's 60-min features start short of history
            self.flags.append("wx_history_short")
        try:
            tape = tmodel.WxTape(rows)
        except Exception as e:
            self.flags.append(f"wxtape_error:{type(e).__name__}:{str(e)[:120]}")
            return
        settle = float(self.inp.settle_ts)
        thr = []
        for k in self.strikes:
            try:
                thr.append(32.0 + C.STEP_F * (C.sum_threshold(k) - 0.5))
            except Exception:
                thr.append(k + 0.005)
        slopes_fn = getattr(model, "slopes", None)
        if not callable(slopes_fn):
            slopes_fn = None
        prev_snap, prev_state, prev_idx = None, None, None
        errs = 0
        rxs = sorted({float(r["rx"]) for r in rows})
        # states before open - margin are never read except the last one (the
        # state at open): apply those rows in one snapshot call, then step
        first = max(0, bisect.bisect_left(rxs, self.open - WX_PREOPEN_MARGIN_S) - 1)
        for rx in rxs[first:]:
            if rx >= self.end_t:
                break
            try:
                snap = tape.snapshot(rx)
            except Exception:
                errs += 1
                continue
            # "the replica index changed" = the model's five-station estimate
            # (est_latest: a lagging station, usually KFLL, filled with its
            # own newest reading). index_latest flips between the 4-station
            # pending replica and the 5-station value every minute with no
            # temperature change (74 flips vs 0 estimate moves in the
            # 26OCT1422 hour), which would tag ~6% of the hour as after-wx.
            idx = getattr(snap, "est_latest", None)
            if idx is None:
                idx = getattr(snap, "index_latest", None)
            if prev_idx is not None and idx is not None and abs(idx - prev_idx) > 1e-6:
                self.idx_change_rx.append(rx)
            if idx is not None:
                prev_idx = idx
            # WxTape hands back the same object while nothing changed; any new
            # row (a pending station, a finalization, a revision anywhere in
            # the 60-min history) gets a fresh fair. Dedupe on the result.
            if snap is prev_snap:
                continue
            prev_snap = snap
            fr, dist, sl = {}, None, None
            try:
                fr = model.fair(snap, settle, list(self.strikes)) or {}
                if slopes_fn is not None:
                    # the fair's own price slope (cents per 0.36 F): the fat-
                    # tail mixture on the same lattice cut(s) fair() prices.
                    # dist()'s sigma is the mixture's CORE sd, so a plain
                    # normal on it is 0.85x the slope at the centre and up
                    # to 2.8x too small at 3-7% / 93-97% (26OCT1421/22)
                    sl = slopes_fn(snap, settle, list(self.strikes)) or {}
                else:
                    dist = model.dist(snap, settle)
            except Exception:
                errs += 1
            fl, gl = [], []
            for j, k in enumerate(self.strikes):
                f = pol.clean_prob(_by_strike(fr, k))
                fl.append(f)
                g = None
                if f is not None and sl is not None:
                    g = pol.clean_slope(_by_strike(sl, k))
                elif dist and dist[1] and dist[1] > 0 and f is not None:
                    # models without slopes(): the spec's plain-normal buffer
                    g = pol.normal_slope_c(float(dist[0]), float(dist[1]), thr[j], C.STEP_F)
                gl.append(g)
            state = (tuple(fl), tuple(gl))
            if state == prev_state:
                continue
            prev_state = state
            self.st_rx.append(rx)
            self.st_f.append(fl)
            self.st_g.append(gl)
        if errs:
            self.flags.append(f"model_errors:{errs}")

    def react_view(self, react: float):
        """(tick_fs, events) for a reaction latency: the fair-state index at
        every tick, and the merged (time, kind, payload) decision/trade list."""
        key = round(float(react), 6)
        hit = self._react_cache.get(key)
        if hit is not None:
            return hit
        n, sec0 = self.n, self.sec0
        st = self.st_rx
        tick_fs = []
        for i in range(n):
            T = sec0 + i + 0.5
            tick_fs.append(bisect.bisect_right(st, T - react) - 1)
        ev = []
        for i in range(n):
            T = sec0 + i + 0.5
            if T >= self.end_t:
                break
            ev.append((T, _TICK, i))
        first_t = sec0 + 0.5
        for k, rx in enumerate(st):
            t = rx + react
            if first_t < t < self.end_t:
                ev.append((t, _WX, k))
        for j, tr in enumerate(self.trades):
            if tr[0] < self.end_t:
                ev.append((tr[0], _TRADE, j))
        ev.sort(key=lambda e: (e[0], e[1]))
        out = (tick_fs, ev)
        self._react_cache[key] = out
        return out

    def fair_at(self, m: int, t: float, react: float) -> Optional[float]:
        k = bisect.bisect_right(self.st_rx, t - react) - 1
        return self.st_f[k][m] if k >= 0 else None

    def after_wx(self, t: float) -> bool:
        k = bisect.bisect_right(self.idx_change_rx, t) - 1
        return k >= 0 and t - self.idx_change_rx[k] <= FILL_WX_WINDOW_S

    # -- shared evaluations ------------------------------------------------
    def payoff_yes(self, m: int) -> float:
        r = self.inp.results.get(self.tickers[m])
        if r == "yes":
            return 1.0
        if r == "no":
            return 0.0
        last = self.lastmid[m][-1] if self.n else None
        return (last / 100.0) if last is not None else 0.5

    def market_stats(self) -> dict:
        n_ss = paying = locked = live = dec = 0
        depth = []
        for m in range(self.nm):
            pr = self.progs[m]
            vers, infos = self.ver[m], self.vinfo[m]
            for i in range(self.n):
                T = self.sec0 + i + 0.5
                if T >= self.end_t:
                    break
                if not (pr["start"] <= T < pr["end"]):
                    continue
                v = vers[i]
                if v < 0:
                    continue
                b = infos[v]
                n_ss += 1
                paying += b.paying
                locked += b.locked99
                if b.mid is not None and 10 <= b.mid <= 90:
                    live += 1
                else:
                    dec += 1
                depth.append(b.depth3)
        depth.sort()
        med = 0.0
        if depth:
            k = len(depth)
            med = depth[k // 2] if k % 2 else 0.5 * (depth[k // 2 - 1] + depth[k // 2])
        d = float(n_ss) or 1.0
        return {"strike_secs": n_ss, "paying_frac_without_us": _r(paying / d),
                "locked99_frac": _r(locked / d), "live_frac": _r(live / d),
                "decided_frac": _r(dec / d), "depth3c_median": _r(med, 2)}

    def model_eval(self, react: float) -> dict:
        tick_fs, _ = self.react_view(react)
        by = {b: [0, 0.0, 0.0, 0.0, 0.0] for b in TAU_BUCKETS}
        n = 0
        bm = bk = lm = lk = 0.0
        eps = LOGLOSS_EPS
        nofair = 0
        total_ticks = 0
        for i in range(self.n):
            T = self.sec0 + i + 0.5
            if T >= self.end_t:
                break
            fs = tick_fs[i]
            for m in range(self.nm):
                total_ticks += 1
                if fs < 0 or self.st_f[fs][m] is None:
                    nofair += 1
            if i % 60:
                continue
            tb = tau_bucket((self.close - T) / 60.0)
            for m in range(self.nm):
                r = self.inp.results.get(self.tickers[m])
                if r not in ("yes", "no") or fs < 0:
                    continue
                f = self.st_f[fs][m]
                v = self.ver[m][i]
                if f is None or v < 0:
                    continue
                b = self.vinfo[m][v]
                if not b.two_sided:
                    continue
                o = 1.0 if r == "yes" else 0.0
                q = b.mid / 100.0
                e1, e2 = (f - o) ** 2, (q - o) ** 2
                pf = min(1 - eps, max(eps, f))
                pq = min(1 - eps, max(eps, q))
                n += 1
                bm += e1
                bk += e2
                l1 = -math.log(pf if o else 1 - pf)
                l2 = -math.log(pq if o else 1 - pq)
                lm += l1
                lk += l2
                a = by[tb]
                a[0] += 1
                a[1] += e1
                a[2] += e2
                a[3] += l1
                a[4] += l2
        self.fair_missing_frac = nofair / float(total_ticks) if total_ticks else 1.0
        d = float(n) or 1.0
        return {"n": n, "brier_model": _r(bm / d), "brier_mid": _r(bk / d),
                "logloss_model": _r(lm / d), "logloss_mid": _r(lk / d),
                "by_tau": {b: {"n": a[0], "brier_model": _r(a[1] / (a[0] or 1)),
                               "brier_mid": _r(a[2] / (a[0] or 1)),
                               "logloss_model": _r(a[3] / (a[0] or 1)),
                               "logloss_mid": _r(a[4] / (a[0] or 1))}
                           for b, a in by.items()}}


# --------------------------------------------------------------------------
# One policy x fill-model pass
# --------------------------------------------------------------------------

# the policy (temp_mm_policy); aliases kept for older callers
_g_tau = pol.g_tau
_overlap_window = pol.overlap_window


class _Pass:
    def __init__(self, sh: _Shared, P: dict, fill_model: str):
        self.sh, self.P, self.fm = sh, P, fill_model
        nm = sh.nm
        self.orders = [{"yes": {}, "no": {}} for _ in range(nm)]
        self.belief = [{"yes": {}, "no": {}} for _ in range(nm)]
        self.own_ver = [0] * nm
        self.mcls = ["live"] * nm
        self.last_key = [None] * nm
        self.yes_q = [0.0] * nm
        self.no_q = [0.0] * nm
        self.cost = [0.0] * nm
        self.risk_ver = 0
        self._risk_cache = (-1, None)
        self.pending: deque = deque()
        self.fills: List[tuple] = []
        self.score_cache = [None] * nm
        self.reward = 0.0
        self.share_sum = 0.0
        self.quoted_secs = 0
        self.r_cls = {"live": 0.0, "decided": 0.0}
        self.r_tau = {b: 0.0 for b in TAU_BUCKETS}
        self.max_abs_pos = 0.0
        self.peak_wcl = 0.0
        # quote_hours_et: the event's settle hour (ET) decides the whole hour
        self.hours_ok = pol.quote_hour_ok(P, sh.inp.settle_ts)

    # -- risk ----------------------------------------------------------
    def _wcl(self) -> Tuple[float, List[int]]:
        return pol.worst_case_loss(self.yes_q, self.no_q, self.cost)

    def _risk(self):
        if self._risk_cache[0] == self.risk_ver:
            return self._risk_cache[1]
        out = pol.risk_headroom(self.P, self.yes_q, self.no_q, self.cost)
        self._risk_cache = (self.risk_ver, out)
        return out

    # -- policy (temp_mm_policy) -------------------------------------------
    def _price(self, side, info, ref, f_side, buf, back, guard):
        return pol.quote_price(self.P, side, info, ref, f_side, buf, back, guard)

    def _desired(self, m, t, v, f, gf, wflag, pflag, size_full, risk, hold=False):
        """-> (dy, dn, rule). dy/dn: {price: size}."""
        info = self.sh.vinfo[m][v] if v >= 0 else None
        return pol.quote_market(self.P, info, f, gf, wflag, pflag, size_full,
                                risk[m], self.sh.progs[m]["target"], hold_pads=hold)

    def _decide_all(self, t, idx, fs, t_next):
        sh, P = self.sh, self.P
        lat = float(P["order_latency_s"])
        a, b = t + lat, t_next + lat
        wflag = pol.window_flag(P, a, b)
        # the pad hold counts from the event's real open (the markets'
        # open_time; close - 3600 when the meta has none)
        hold = pol.pad_hold_flag(P, a, sh.open)
        tau = (sh.close - t) / 60.0
        size_full = pol.live_size(P, tau)
        risk = self._risk()
        n = sh.n
        for m in range(sh.nm):
            v = sh.ver[m][idx] if 0 <= idx < n else -1
            pr = sh.progs[m]
            pflag = pol.program_flag(P, a, b, pr["start"], pr["end"], sh.close)
            if not self.hours_ok:
                pflag = False           # outside quote_hours_et: nothing rests
            key = (v, fs, wflag, pflag, size_full, self.risk_ver, hold)
            if key == self.last_key[m]:
                continue
            self.last_key[m] = key
            f = sh.st_f[fs][m] if fs >= 0 else None
            gf = sh.st_g[fs][m] if fs >= 0 else None
            dy, dn, rule = self._desired(m, t, v, f, gf, wflag, pflag, size_full, risk, hold)
            # size cuts are relative (Kalshi reduce_by): a fill that lands
            # before activation leaves actual == projected belief
            ops = pol.diff_orders(self.belief[m], dy, dn)
            if ops or (rule is not None and rule != self.mcls[m]):
                self.belief[m] = {"yes": dict(dy), "no": dict(dn)}
                self.pending.append((a, m, ops, rule))

    def _activate(self, item):
        t_act, m, ops, rule = item
        sh = self.sh
        idx = sh.idx_at(t_act)
        v = sh.ver[m][idx] if 0 <= idx < sh.n else -1
        info = sh.vinfo[m][v] if v >= 0 else None
        o = self.orders[m]
        changed = False
        for side, op, p, s in ops:
            cur = o[side]
            if op == "cancel":
                if cur.pop(p, None) is not None:
                    changed = True
            elif op in ("place", "inc"):
                lv = (info.by if side == "yes" else info.bn) if info else {}
                cur.setdefault(p, []).append([s, float(lv.get(p, 0.0))])
                changed = True
            else:                       # dec: trim s (or what is left) from the back
                lots = cur.get(p)
                if not lots:
                    continue
                excess = s
                while excess > 1e-9 and lots:
                    cut = min(lots[-1][0], excess)
                    lots[-1][0] -= cut
                    excess -= cut
                    if lots[-1][0] <= 1e-9:
                        lots.pop()
                    changed = True
                if not lots:
                    cur.pop(p, None)
        if rule is not None:
            self.mcls[m] = rule
        if changed:
            self.own_ver[m] += 1

    # -- reward ------------------------------------------------------------
    def _score(self, i, T):
        sh = self.sh
        tb = tau_bucket((sh.close - T) / 60.0)
        for m in range(sh.nm):
            pr = sh.progs[m]
            if not (pr["start"] <= T < pr["end"]):
                continue
            v = sh.ver[m][i]
            if v < 0:
                continue
            o = self.orders[m]
            if not o["yes"] and not o["no"]:
                continue
            sc = self.score_cache[m]
            if sc is not None and sc[0] == v and sc[1] == self.own_ver[m]:
                frac = sc[2]
            else:
                info = sh.vinfo[m][v]
                oy = {p: sum(l[0] for l in lots) for p, lots in o["yes"].items()}
                on = {p: sum(l[0] for l in lots) for p, lots in o["no"].items()}
                frac = float(scoring.score(info.by, info.bn, oy, on,
                                           pr["target"], pr["df"])[0])
                self.score_cache[m] = (v, self.own_ver[m], frac)
            self.quoted_secs += 1
            self.share_sum += frac
            if frac > 0:
                r = pr["pool"] * frac
                self.reward += r
                self.r_cls[self.mcls[m] if self.mcls[m] in self.r_cls else "live"] += r
                self.r_tau[tb] += r

    # -- fills -------------------------------------------------------------
    def _on_trade(self, tr):
        ts, m, taker, yc, cnt = tr
        o = self.orders[m]
        if taker == "no":
            side, eq = "yes", yc        # taker sold YES -> hits YES bids
        else:
            side, eq = "no", 100 - yc   # taker bought YES -> hits NO bids
        book = o[side]
        if not book:
            return
        prices = sorted((p for p in book if p >= eq), reverse=True)
        if not prices:
            return
        c_left = cnt
        info = None
        for p in prices:
            if c_left <= 1e-9:
                break
            lots = book[p]
            filled = 0.0
            if p > eq or self.fm == "opt":
                for lot in lots:
                    if c_left <= 1e-9:
                        break
                    q = min(lot[0], c_left)
                    lot[0] -= q
                    c_left -= q
                    filled += q
            else:                        # cons at an equal price: queue first
                if info is None:
                    idx = self.sh.idx_at(ts)
                    v = self.sh.ver[m][idx] if 0 <= idx < self.sh.n else -1
                    info = self.sh.vinfo[m][v] if v >= 0 else False
                if info:
                    lvl = float((info.by if side == "yes" else info.bn).get(p, 0.0))
                    for lot in lots:
                        if lot[1] > lvl:
                            lot[1] = lvl
                ours_before = 0.0
                fl = []
                for lot in lots:
                    start = lot[1] + ours_before
                    q = min(lot[0], max(0.0, c_left - start))
                    ours_before += lot[0]
                    fl.append(q)
                tot = sum(fl)
                others = c_left - tot
                for lot, q in zip(lots, fl):
                    lot[0] -= q
                    lot[1] = max(0.0, lot[1] - others)
                filled = tot
                c_left -= tot
            if filled <= 1e-9:
                continue
            book[p] = [l for l in lots if l[0] > 1e-9]
            if not book[p]:
                del book[p]
            self._record_fill(ts, m, side, p, filled)

    def _projected(self, m) -> Dict[str, Dict[int, float]]:
        """What will rest on market m once its in-flight ops land: actual
        resting size with the pending ops applied in order (the bot's belief;
        fills are known instantly)."""
        o = self.orders[m]
        proj = {side: {p: sum(l[0] for l in lots) for p, lots in o[side].items()}
                for side in ("yes", "no")}
        for item in self.pending:
            if item[1] != m:
                continue
            for side, op, p, s in item[2]:
                d = proj[side]
                if op == "cancel":
                    d.pop(p, None)
                elif op in ("place", "inc"):
                    d[p] = d.get(p, 0.0) + s
                else:
                    left = d.get(p, 0.0) - s
                    if left > 1e-9:
                        d[p] = left
                    else:
                        d.pop(p, None)
        return {side: {p: s for p, s in d.items() if s > 1e-9} for side, d in proj.items()}

    def _record_fill(self, ts, m, side, p, q):
        sh = self.sh
        self.own_ver[m] += 1
        self.risk_ver += 1
        self.belief[m] = self._projected(m)
        if side == "yes":
            self.yes_q[m] += q
        else:
            self.no_q[m] += q
        self.cost[m] += q * p / 100.0
        self.max_abs_pos = max(self.max_abs_pos, abs(self.yes_q[m] - self.no_q[m]))
        wcl, _ = self._wcl()
        self.peak_wcl = max(self.peak_wcl, wcl)
        f = sh.fair_at(m, ts, float(self.P["react_s"]))
        self.fills.append((ts, m, side, p, q, self.mcls[m], f, sh.after_wx(ts)))

    # -- run -----------------------------------------------------------------
    def run(self) -> dict:
        sh, P = self.sh, self.P
        tick_fs, events = sh.react_view(float(P["react_s"]))
        pending = self.pending
        sec0 = sh.sec0
        for t, kind, a in events:
            while pending and pending[0][0] <= t:
                self._activate(pending.popleft())
            if kind == _TRADE:
                self._on_trade(sh.trades[a])
            elif kind == _TICK:
                self._score(a, t)
                self._decide_all(t, a, tick_fs[a], t + 1.0)
            else:
                idx = sh.idx_at(t)
                self._decide_all(t, idx, a, sec0 + idx + 1 + 0.5)
        return self._summary()

    def _summary(self) -> dict:
        sh, P = self.sh, self.P
        fee_c = float(P["maker_fee_per_contract"] or 0.0)
        by_class = {c: {"reward": self.r_cls[c], "contracts": 0.0, "pnl": 0.0}
                    for c in ("live", "decided")}
        by_tau = {b: {"reward": self.r_tau[b], "contracts": 0.0, "pnl": 0.0}
                  for b in TAU_BUCKETS}
        by_side = {"yes_bid": {"contracts": 0.0, "pnl": 0.0},
                   "no_bid": {"contracts": 0.0, "pnl": 0.0}}
        aw = {"contracts": 0.0, "pnl": 0.0}
        contracts = pnl = m60 = m300 = 0.0
        for ts, m, side, p, q, cls, f, after in self.fills:
            py = sh.payoff_yes(m)
            if side == "yes":
                val = py
                k = "yes_bid"
            else:
                val = 1.0 - py
                k = "no_bid"
            fp_ = q * (val - p / 100.0)
            contracts += q
            pnl += fp_
            c = by_class[cls if cls in by_class else "live"]
            c["contracts"] += q
            c["pnl"] += fp_
            tb = by_tau[tau_bucket((sh.close - ts) / 60.0)]
            tb["contracts"] += q
            tb["pnl"] += fp_
            by_side[k]["contracts"] += q
            by_side[k]["pnl"] += fp_
            if after:
                aw["contracts"] += q
                aw["pnl"] += fp_
            for horizon in (60.0, 300.0):
                tm = min(ts + horizon, sh.close - 0.5)
                mid = sh.mid_at(m, tm)
                if mid is None:
                    mid = sh.mid_at(m, ts) or float(p if side == "yes" else 100 - p)
                mo = (mid - p) if side == "yes" else ((100.0 - mid) - p)
                if horizon == 60.0:
                    m60 += mo * q
                else:
                    m300 += mo * q
        fees = fee_c * contracts
        rnd = lambda d: {k: _r(v) for k, v in d.items()}
        return {
            "reward": _r(self.reward), "fills_n": len(self.fills),
            "contracts": _r(contracts, 2), "trading_pnl": _r(pnl), "fees": _r(fees),
            "net": _r(self.reward + pnl - fees),
            "avg_share": _r(self.share_sum / self.quoted_secs if self.quoted_secs else 0.0),
            "quoted_secs": self.quoted_secs, "max_abs_pos": _r(self.max_abs_pos, 2),
            "worst_case_loss": _r(self.peak_wcl),
            "by_class": {c: rnd(v) for c, v in by_class.items()},
            "by_tau": {b: rnd(v) for b, v in by_tau.items()},
            "by_side": {k: rnd(v) for k, v in by_side.items()},
            "fills_after_wx": rnd(aw),
            "markouts": {"m60": _r(m60 / contracts if contracts else 0.0, 3),
                         "m300": _r(m300 / contracts if contracts else 0.0, 3)},
        }


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def _resolve_variants(variants) -> Dict[str, dict]:
    if variants is None:
        src = VARIANTS
    elif isinstance(variants, dict):
        src = variants
    else:
        if isinstance(variants, str):
            variants = [v for v in variants.split(",") if v]
        src = {}
        for v in variants:
            if v not in VARIANTS:
                raise ValueError(f"unknown variant {v!r}; known: {sorted(VARIANTS)}")
            src[v] = VARIANTS[v]
    out = {}
    for name, ov in src.items():
        bad = set(ov) - set(DEFAULT_POLICY)
        if bad:
            raise ValueError(f"variant {name}: unknown policy keys {sorted(bad)}")
        P = dict(DEFAULT_POLICY)
        P.update(ov)
        pol.check_values(P)
        if P.get("wcl_resting"):
            # the event-level resting budget needs every market at once; the
            # replay decides per market (cached), so it is not simulated
            raise ValueError(f"variant {name}: wcl_resting is not simulated (live-only switch)")
        out[name] = P
    return out


# -- swp (temp_shadow_sweep) ---------------------------------------------------

_SWP_CACHE: Dict[str, tuple] = {}


def check_hazard_doc(doc) -> Optional[str]:
    """Why a sweep_hazard table cannot be used (None = usable): it must hold a
    non-empty 'hazard' fit (temp_shadow_sweep.Hazard reads a doc without one
    as an all-zero table, which silently switches the hidden-sweep channel
    off), a positive lam_max and the size draws ('sizes_all')."""
    import temp_shadow_sweep as SW
    if not isinstance(doc, dict):
        return "not a JSON object"
    hz = doc.get("hazard")
    if not isinstance(hz, dict) or not hz.get("a"):
        return "no 'hazard' fit (hazard.a missing or empty)"
    try:
        h = SW.Hazard(doc)
    except Exception as e:              # noqa: BLE001
        return f"Hazard() raised {type(e).__name__}: {e}"
    if not h.lam_max > 0.0:
        return f"lam_max {h.lam_max:g} <= 0 (every hidden-sweep rate is zero)"
    if not doc.get("sizes_all"):
        return "no 'sizes_all' size draws"
    return None


def check_tails_doc(doc) -> Optional[str]:
    """Why a sweep_tails table cannot be used (None = usable): bucket rows
    [lo, hi, model_mean, realized, ci_lo, ci_hi] with 0 <= lo < hi <= 0.5
    and rates in [0, 1]."""
    if not isinstance(doc, dict):
        return "not a JSON object"
    rows = doc.get("buckets")
    if not isinstance(rows, list) or not rows:
        return "no 'buckets'"
    for r in rows:
        try:
            lo, hi, _m, mid, clo, chi = (float(x) for x in r[:6])
        except (TypeError, ValueError):
            return f"bucket row {r!r} is not 6 numbers"
        if not (0.0 <= lo < hi <= 0.5) or not all(0.0 <= x <= 1.0 for x in (mid, clo, chi)):
            return f"bucket row {r!r} is out of range"
    return None


def _swp_table(name: str, repo_path: str, check) -> Tuple[str, dict, str, Optional[str]]:
    """(path, doc, kind 'override' | 'repo', why the override was rejected or
    None). An override in <TEMP_SHADOW_DATA>/model/ that fails `check` falls
    back to the repo table; an unusable repo table raises."""
    over = os.path.join(C.DATA_ROOT, "model", name)
    rejected = None
    if os.path.exists(over):
        try:
            doc = _swp_doc(over)
            why = check(doc)
        except Exception as e:          # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
        if why is None:
            return over, doc, "override", None
        rejected = f"{over}: {why}"
    doc = _swp_doc(repo_path)
    why = check(doc)
    if why is not None:
        raise ValueError(f"swp table {repo_path} is not usable: {why}")
    return repo_path, doc, "repo", rejected


def _swp_doc(path: str) -> dict:
    try:
        mt = os.path.getmtime(path)
    except OSError as e:
        raise ValueError(f"swp table missing: {path} ({e})")
    hit = _SWP_CACHE.get(path)
    if hit is not None and hit[0] == mt:
        return hit[1]
    doc = C.read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"cannot read swp table {path}")
    _SWP_CACHE[path] = (mt, doc)
    return doc


def swp_config(draws: Optional[int] = None) -> dict:
    """The swp fill model's inputs: hazard (temp_shadow_sweep.Hazard), tails
    (TailCal) and the identity block written into every sim file / checked by
    run_day's cache (draws, rec_mode, channels, seed, the two tables' ids)."""
    import temp_shadow_sweep as SW      # lazy: temp_shadow_sweep imports this module
    hp, hdoc, hkind, hrej = _swp_table("sweep_hazard.json", SWP_HAZARD_REPO, check_hazard_doc)
    tp, tdoc, tkind, trej = _swp_table("sweep_tails.json", SWP_TAILS_REPO, check_tails_doc)
    hz = SW.Hazard(hdoc)
    tc = SW.TailCal(tdoc)
    n = int(SWP_DRAWS if draws is None else draws)
    if n < 1:
        raise ValueError("swp draws must be >= 1")
    ident = {"draws": n, "rec_mode": SWP_REC_MODE, "channels": SWP_CHANNELS, "seed": SWP_SEED,
             "hazard_id": hz.ident(), "hazard_src": hp, "hazard_kind": hkind,
             "hazard_rejected": hrej, "hazard_built": hz.doc.get("built"),
             "lam_max": _r(hz.lam_max), "tails_id": tc.ident(), "tails_src": tp,
             "tails_kind": tkind, "tails_rejected": trej,
             "tails_built": tc.doc.get("built"), "sweep_version": SW.SWEEP_VERSION}
    return {"hazard": hz, "tails": tc, "ident": ident}


_SWP_CACHE_KEYS = ("draws", "rec_mode", "channels", "seed", "hazard_id", "tails_id",
                   "sweep_version")
_STRESS_CACHE_KEYS = ("draws", "seed", "fav_seed", "fav_hazard_id", "stress_version", "take",
                      "hole", "scope")


def stress_scope() -> str:
    """STRESS_SCOPE, validated ('all' | 'nolive')."""
    s = str(STRESS_SCOPE).strip().lower()
    if s not in STRESS_SCOPES:
        raise ValueError(f"TEMP_SHADOW_STRESS_SCOPE must be one of {STRESS_SCOPES}, not {s!r}")
    return s


def in_stress_scope(P: dict, scope: Optional[str] = None) -> bool:
    """Does a variant (full policy, or overrides of DEFAULT_POLICY) get the
    stress fill model? 'all': every variant; 'nolive': live=False only."""
    scope = stress_scope() if scope is None else scope
    return scope == "all" or not dict(DEFAULT_POLICY, **(P or {})).get("live")


def stress_config(swp_cfg: Optional[dict] = None, draws: Optional[int] = None) -> dict:
    """The stress fill model's inputs on top of swp_config(): the hidden
    favorite-taker table (temp_shadow_sweep.Hazard; <TEMP_SHADOW_DATA>/model/
    sweep_favhazard.json when usable, else the repo's
    temp_shadow_sweep_favhazard.json) and the identity block written into
    every sim file / checked by run_day's cache."""
    import temp_shadow_sweep as SW
    sw = swp_cfg if swp_cfg is not None else swp_config(draws)
    fp, fdoc, fkind, frej = _swp_table("sweep_favhazard.json", STRESS_FAVHAZ_REPO,
                                       check_hazard_doc)
    fhz = SW.Hazard(fdoc)
    ident = {"draws": int(sw["ident"]["draws"]), "seed": SWP_SEED, "fav_seed": SW.FAVHAZ_SEED,
             "fav_hazard_id": fhz.ident(), "fav_hazard_src": fp, "fav_hazard_kind": fkind,
             "fav_hazard_rejected": frej, "fav_hazard_built": fhz.doc.get("built"),
             "fav_lam_max": _r(fhz.lam_max), "stress_version": SW.STRESS_VERSION,
             "take": STRESS_TAKE, "hole": STRESS_HOLE, "scope": stress_scope()}
    return {"fav_hazard": fhz, "ident": ident}


def _swp_summary(runs: List[dict], exact: bool, stress: bool = False) -> dict:
    """Mean over the swp (or stress) draws in the opt / cons schema, plus the
    Monte Carlo spread (net_sd, net_se = net_sd / sqrt(draws)) and the sweep
    extras; stress adds its own (clear_events, favhaz_events,
    above1c_contracts, fav_fills) and names the shortcut stress_exact."""
    import temp_shadow_sweep as SW
    n = len(runs)
    ms = SW.mean_summary(runs)
    out = {k: ms[k] for k in ("reward", "fills_n", "contracts", "trading_pnl", "fees", "net",
                              "avg_share", "quoted_secs", "max_abs_pos", "worst_case_loss")}
    for k in ("by_class", "by_tau", "by_side") + (("fav_fills",) if stress else ()):
        # means at the opt / cons precision
        acc: Dict[str, Dict[str, float]] = {}
        for r in runs:
            for b, d in (r.get(k) or {}).items():
                a = acc.setdefault(b, {})
                for kk, vv in d.items():
                    a[kk] = a.get(kk, 0.0) + float(vv or 0.0)
        out[k] = {b: {kk: _r(vv / n) for kk, vv in d.items()} for b, d in acc.items()}
    faw = {"contracts": 0.0, "pnl": 0.0}
    mo = {"m60": 0.0, "m300": 0.0}
    ct = 0.0
    for r in runs:
        for k in faw:
            faw[k] += float((r.get("fills_after_wx") or {}).get(k) or 0.0)
        c = float(r.get("contracts") or 0.0)
        ct += c
        for k in mo:
            mo[k] += float((r.get("markouts") or {}).get(k) or 0.0) * c
    out["fills_after_wx"] = {k: _r(v / n) for k, v in faw.items()}
    out["markouts"] = {k: _r(v / ct if ct else 0.0, 3) for k, v in mo.items()}
    out["draws"] = n
    for k in ("net", "reward", "trading_pnl", "contracts", "filler_contracts"):
        out[f"{k}_sd"] = ms.get(f"{k}_sd")
    out["net_se"] = _r((ms.get("net_sd") or 0.0) / math.sqrt(n)) if n else None
    out["draw_net"] = ms.get("draw_net")
    out["stress_exact" if stress else "swp_exact"] = bool(exact)
    for k in ("filler_contracts", "haz_events", "pnl_cal", "net_cal", "net_cal_tlo",
              "net_cal_thi") + (("clear_events", "favhaz_events", "above1c_contracts")
                                if stress else ()):
        if k in ms:
            out[k] = ms[k]
    out["sweep_src"] = ms.get("sweep_src") or {}
    return out


def run_swp(sh, Ps: Dict[str, dict], cfg: dict, screen: bool = True) -> Dict[str, dict]:
    """name -> the swp summary, over cfg's draws; sh = a SweepShared. Draws
    are the outer loop and draw k's hidden-sweep candidates depend only on
    (event, k, seed), so every variant meets the same arrivals (common random
    numbers). Two exact shortcuts (screen=False runs every draw in full;
    test_temp_shadow_sim checks they agree):
      * a variant whose first draw never rested a 1c order: every channel
        acts on our 1c orders only, so all its draws are that one
        (swp_exact);
      * a variant whose first draw accepted no hidden sweep: its 1c
        trajectory is logged, and a later draw whose candidates would all
        be rejected along it (temp_shadow_sweep.hazard_hits) is identical
        to the first; only the draws with a hit are run."""
    import temp_shadow_sweep as SW
    hz, tc = cfg["hazard"], cfg["tails"]
    n = int(cfg["ident"]["draws"])
    runs: Dict[str, List[dict]] = {name: [] for name in Ps}
    exact = set()
    logs: Dict[str, dict] = {}            # name -> draw 0's hazard-free 1c trajectory
    for k in range(n):
        todo = [name for name in Ps if name not in exact]
        cands = SW.hazard_candidates(sh, k, hz.lam_max, SWP_SEED) if todo else []
        for name, P in Ps.items():
            if name in exact:
                runs[name].append(runs[name][0])
                continue
            if k > 0 and name in logs and not SW.hazard_hits(sh, hz, logs[name], cands):
                runs[name].append(runs[name][0])
                continue
            vp = SW.VPass(sh, P, "swp", hazard=hz, cands=cands, rec_mode=SWP_REC_MODE,
                          channels=SWP_CHANNELS, tail_cal=tc if tc else None)
            if k == 0 and screen:
                vp.record_expo()
            runs[name].append(vp.run())
            if k == 0:
                if not vp.had_1c:
                    exact.add(name)
                elif screen and vp.haz_n == 0:
                    logs[name] = vp.expo_log
    return {name: _swp_summary(runs[name], name in exact) for name in Ps}


def run_stress(sh, Ps: Dict[str, dict], swp_cfg: dict, st_cfg: dict,
               screen: bool = True) -> Dict[str, dict]:
    """name -> the stress summary over swp_cfg's draws (temp_shadow_sweep
    VPass fill model 'stress': swp + level-clear (d) + hidden favorite takers
    (e), the base settings of the 10/17 audit: take 'full', no gap rule).
    Draw k uses the 1c hazard candidates of swp's draw k (SWP_SEED) and the
    favorite-taker candidates fav_candidates(sh, k) (FAVHAZ_SEED): common
    random numbers across variants, and the same draws as
    temp_shadow_sweep.run_event_variants. Shortcuts (screen=False runs every
    draw in full; test_temp_shadow_sim checks they agree):
      * a variant whose first draw rested no 1c order and no favorite lot
        alone at the top is exact in that draw (stress_exact);
      * a variant whose first draw accepted no hidden sweep and no hidden
        favorite taker logs both trajectories; a later draw is run only if
        one of its candidates would be accepted along them
        (temp_shadow_sweep.hazard_hits / fav_hazard_hits)."""
    import temp_shadow_sweep as SW
    hz, tc, fhz = swp_cfg["hazard"], swp_cfg["tails"], st_cfg["fav_hazard"]
    n = int(swp_cfg["ident"]["draws"])
    runs: Dict[str, List[dict]] = {name: [] for name in Ps}
    exact = set()
    logs: Dict[str, tuple] = {}          # name -> draw 0's (1c log, favorite log)
    for k in range(n):
        todo = [name for name in Ps if name not in exact]
        cands = SW.hazard_candidates(sh, k, hz.lam_max, SWP_SEED) if todo else []
        fcands = SW.fav_candidates(sh, k, fhz.lam_max) if todo else []
        for name, P in Ps.items():
            if name in exact:
                runs[name].append(runs[name][0])
                continue
            if k > 0 and name in logs:
                l1, lf = logs[name]
                if (not SW.hazard_hits(sh, hz, l1, cands)
                        and not SW.fav_hazard_hits(sh, fhz, lf, fcands)):
                    runs[name].append(runs[name][0])
                    continue
            vp = SW.VPass(sh, P, SW.STRESS, hazard=hz, cands=cands, rec_mode=SWP_REC_MODE,
                          channels=SWP_CHANNELS, tail_cal=tc if tc else None, fav_hazard=fhz,
                          fav_cands=fcands, stress_hole=STRESS_HOLE, stress_take=STRESS_TAKE)
            if k == 0 and screen:
                vp.record_expo()
                vp.record_fav_expo()
            runs[name].append(vp.run())
            if k == 0:
                if not vp.had_1c and not vp.had_fav:
                    exact.add(name)
                elif screen and vp.haz_n == 0 and vp.favhaz_n == 0:
                    logs[name] = (vp.expo_log, vp.fav_log)
    return {name: _swp_summary(runs[name], name in exact, stress=True) for name in Ps}


def run_event(event_ticker: str, variants=None, fill_models=RUN_FILL_MODELS, model=None,
              until_ts: Optional[float] = None, draws: Optional[int] = None) -> dict:
    """Simulate one event for every variant x fill model.

    variants: None (all of VARIANTS), a list/comma string of VARIANTS names,
    or a dict name -> overrides of DEFAULT_POLICY. until_ts (testing/what-if)
    stops the timeline there; settlement still uses the final results.
    draws: swp Monte Carlo draws (default SWP_DRAWS)."""
    t_start = time.time()
    Ps = _resolve_variants(variants)
    for fm in fill_models:
        if fm not in RUN_FILL_MODELS:
            raise ValueError(f"unknown fill model {fm!r}")
    inp = load_event_inputs(event_ticker)
    if model is None:
        model = tmodel.FairModel()
    swp_cfg, sw_err, sh = None, None, None
    if "swp" in fill_models or STRESS in fill_models:
        # contained (fix pass 10/16): a failure in the sweep path costs swp
        # (and stress, which builds on it) only -- the event keeps opt /
        # cons, with swp_error / stress_error and no |swp / |stress keys
        try:
            import temp_shadow_sweep as SW
            swp_cfg = swp_config(draws)
            # _Shared plus the tape's 1c taker clusters; the books are
            # _Shared's (live-order subtraction as configured), so opt /
            # cons are unchanged
            sh = SW.SweepShared(inp, model, until_ts, subtract=scoring.SUBTRACT_LIVE)
        except Exception as e:          # noqa: BLE001
            swp_cfg, sh, sw_err = None, None, _swp_error("setup", e)
    swp_err = sw_err if "swp" in fill_models else None
    st_cfg, st_err = None, None
    if STRESS in fill_models:
        if swp_cfg is None:
            st_err = ("stress setup failed: the sweep setup it builds on failed: "
                      + str(sw_err or "").splitlines()[0][:300] + "\n" + str(sw_err or "")[-1200:])
        else:
            try:
                st_cfg = stress_config(swp_cfg)
            except Exception as e:      # noqa: BLE001
                st_err = _swp_error("setup", e, "stress")
    sweep_sh = sh
    if sh is None:
        sh = _Shared(inp, model, until_ts)
    react0 = float(DEFAULT_POLICY["react_s"])
    mstats = sh.market_stats()
    meval = sh.model_eval(react0)
    det = {}
    for name, P in Ps.items():
        for fm in fill_models:
            if fm not in ("swp", STRESS):
                det[f"{name}|{fm}"] = _Pass(sh, P, fm).run()
    swp_out: Dict[str, dict] = {}
    if swp_cfg is not None and "swp" in fill_models:
        try:
            swp_out = run_swp(sh, Ps, swp_cfg)
        except Exception as e:          # noqa: BLE001
            swp_out, swp_err = {}, _swp_error("run", e)
    st_out: Dict[str, dict] = {}
    st_names: List[str] = []
    if st_cfg is not None:
        st_names = [name for name, P in Ps.items() if in_stress_scope(P)]
        if st_names:
            try:
                st_out = run_stress(sh, {n: Ps[n] for n in st_names}, swp_cfg, st_cfg)
            except Exception as e:      # noqa: BLE001
                st_out, st_err = {}, _swp_error("run", e, "stress")
    fav_hits, fh_err = None, None
    if sweep_sh is not None:
        # the favorite-bid hits and the favorite's outcome (the report's
        # recent-regime tail rates); a failure costs this key only
        try:
            import temp_shadow_sweep as SW
            fav_hits = SW.fav_hit_summary(sweep_sh)
        except Exception as e:          # noqa: BLE001
            fh_err = f"fav_hits_error:{type(e).__name__}:{str(e)[:120]}"
    fms = [fm for fm in fill_models
           if (fm != "swp" or swp_out) and (fm != STRESS or st_out)]
    out_vars = {}
    for name in Ps:
        for fm in fms:
            if fm == "swp":
                out_vars[f"{name}|{fm}"] = swp_out[name]
            elif fm == STRESS:
                if name in st_out:
                    out_vars[f"{name}|{fm}"] = st_out[name]
            else:
                out_vars[f"{name}|{fm}"] = det[f"{name}|{fm}"]
    cov = sh.stream_cov or {}
    gaps = cov.get("gaps", 0)
    recon = cov.get("reconnects", 0)
    tsrc = cov.get("trades_src") or ("ws" if sh.trades else "none")
    prog_secs = max((min(p["end"], sh.close) - max(p["start"], sh.open))
                    for p in sh.progs) if sh.progs else 0.0
    flags = list(inp.flags) + list(sh.flags)
    if fh_err:
        flags.append(fh_err)
    if not inp.settled:
        flags.append("unsettled_marked_at_last_mid")
    return {
        "event": event_ticker, "open_ts": sh.open, "close_ts": sh.close,
        "settle_ts": float(inp.settle_ts), "settled": bool(inp.settled),
        "settlement_value": inp.settlement_value, "results": dict(inp.results),
        "coverage": {
            "book_trusted_frac": _r(sh.book_trusted_frac),
            "wx_rows": sh.wx_rows, "wx_rows_lookback": getattr(sh, "wx_rows_lookback", 0),
            "trades": len(sh.trades), "trades_src": tsrc,
            "reconnects": len(recon) if isinstance(recon, (list, tuple)) else int(recon or 0),
            "gaps": len(gaps) if isinstance(gaps, (list, tuple)) else int(gaps or 0),
            "program_secs": int(round(max(0.0, prog_secs))),
            "fair_missing_frac": _r(getattr(sh, "fair_missing_frac", 1.0)),
            "programs_src": inp.programs_src,
        },
        "market_stats": mstats,
        "model_eval": meval,
        "variants": out_vars,
        "policies": {name: (variants[name] if isinstance(variants, dict) else VARIANTS.get(name, {}))
                     for name in Ps},
        "flags": flags,
        "fill_models": fms,
        "swp": dict(swp_cfg["ident"]) if swp_out else None,
        **({"swp_error": swp_err} if swp_err else {}),
        **({"stress": dict(st_cfg["ident"], variants=sorted(st_out))} if st_out else {}),
        **({"stress_error": st_err} if st_err and STRESS in fill_models else {}),
        **({"fav_hits": fav_hits} if fav_hits is not None else {}),
        "runtime_s": round(time.time() - t_start, 2),
        "sim_version": SIM_VERSION,
    }


def _swp_error(stage: str, e: BaseException, kind: str = "swp") -> str:
    """One line plus the traceback's tail: the swp_error / stress_error
    written into a sim file whose sweep path raised (the report shows it)."""
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    return f"{kind} {stage} failed: {type(e).__name__}: {e}"[:400] + "\n" + tb[-1200:]


# -- day runner ---------------------------------------------------------------

def _event_settle_ts(ev: str) -> Optional[float]:
    pe = C.parse_event(ev)
    return pe[1] if pe else None


def day_events(et_day: str) -> List[str]:
    """Events whose settlement time (the ticker's hour, = close) falls in the
    ET day: from settled/ files and meta 'markets' rows."""
    a, b = C.et_day_bounds(et_day)
    evs = set()
    sdir = os.path.join(C.DATA_ROOT, "settled")
    if os.path.isdir(sdir):
        for fn in os.listdir(sdir):
            if fn.endswith(".json"):
                evs.add(fn[:-5])
    for rec in C.iter_raw("meta", a - 3 * 3600, b + 3600):
        if rec.get("kind") == "markets":
            ev = (rec.get("data") or {}).get("event_ticker")
            if ev:
                evs.add(ev)
    out = []
    for ev in evs:
        if not any(ev.startswith(s + "-") for s in C.SERIES):
            continue
        S = _event_settle_ts(ev)
        if S is not None and a <= S < b:
            out.append(ev)
    return sorted(out)


def _lower_priority():
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(5)
    except Exception:
        pass


def _day_worker(ev: str, variants, out_path: str, draws: Optional[int] = None,
                scope: Optional[str] = None) -> dict:
    global STRESS_SCOPE
    if scope:                    # the parent's stress scope (a spawned worker re-imports)
        STRESS_SCOPE = scope
    try:
        res = run_event(ev, variants=variants, draws=draws)
        C.write_json_atomic(out_path, res)
        return res
    except Exception as e:
        return {"event": ev, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:]}


def _policy_overrides(variants) -> Dict[str, dict]:
    """name -> overrides (JSON-normalized, as run_event writes 'policies')."""
    if isinstance(variants, dict):
        src = variants
    else:
        src = {name: VARIANTS.get(name, {}) for name in _resolve_variants(variants)}
    return json.loads(json.dumps(src, default=str))


def _cached_ok(doc, want: Dict[str, dict], swp_want: Optional[dict] = None,
               swp_broken: bool = False, stress_want: Optional[dict] = None,
               stress_broken: bool = False) -> bool:
    """A sim file is reused only if it is settled, from this SIM_VERSION,
    holds every requested variant x fill model with the same overrides and,
    with swp_want (swp_config()["ident"]), the same swp draws / rec_mode /
    channels / seed / hazard and tails tables. A file whose swp failed
    (swp_error, no |swp keys) is re-simmed -- unless swp_broken (run_day's
    swp_config() raised: a re-sim could not add swp either, so opt / cons
    suffice). stress the same way: every requested variant in the stress
    scope needs its |stress result and, with stress_want
    (stress_config()["ident"]), the same favorite-taker table, draws,
    seeds, settings and scope; stress_broken (stress_config() raised, or
    swp is broken: stress builds on it) waives it."""
    if not isinstance(doc, dict) or not doc.get("settled") or doc.get("error"):
        return False
    if str(doc.get("sim_version")) != SIM_VERSION:
        return False
    if swp_want is not None:
        sw = doc.get("swp") or {}
        if any(sw.get(k) != swp_want.get(k) for k in _SWP_CACHE_KEYS):
            return False
    have = doc.get("variants") or {}
    pol = doc.get("policies") or {}
    need = tuple(fm for fm in FILL_MODELS if not (swp_broken and fm == "swp"))
    for name, ov in want.items():
        if any(f"{name}|{fm}" not in have for fm in need):
            return False
        if pol.get(name) != ov:
            return False
    if STRESS in RUN_FILL_MODELS and not (stress_broken or swp_broken):
        scope = (stress_want or {}).get("scope") or stress_scope()
        need = [name for name, ov in want.items() if in_stress_scope(ov, scope)]
        if need:            # no requested variant in the scope: no stress block to match
            st = doc.get("stress") or {}
            if stress_want is not None and any(st.get(k) != stress_want.get(k)
                                               for k in _STRESS_CACHE_KEYS):
                return False
            if any(f"{name}|{STRESS}" not in have for name in need):
                return False
    return True


def run_day(et_day: str, variants=None, force=False, workers: Optional[int] = None,
            deadline: Optional[float] = None) -> List[dict]:
    """Simulate every settled event of an ET day into sim/<day>/<EVENT>.json.

    Existing files are loaded and returned unless force, but only if they hold
    every requested variant (same overrides) from this SIM_VERSION with the
    current swp configuration (draws, hazard, tails); otherwise the event is
    re-run (and the file rewritten with the requested set).
    Unsettled events come back as {"event", "pending": True, "settled": False};
    failures as {"event", "error"}.
    deadline (epoch s; the report's time budget for re-simming earlier
    window days): an event whose sim has not started by then is not run and
    comes back as {"event", "deferred": True, "settled": True}; its existing
    file (if any) is left as it is, and the next run_day picks it up. The
    events already running finish.
    swp_config() raising (an unusable repo table) costs swp only: run_event
    writes opt / cons with swp_error, and cached files without swp are
    reused (a re-sim could not add it). stress_config() raising (an
    unusable favorite-taker table; or swp broken) costs stress the same
    way."""
    evs = day_events(et_day)
    out_dir = C.sim_dir(et_day)
    want = _policy_overrides(variants)
    swp_want, swp_broken, swp_cfg = None, False, None
    if "swp" in FILL_MODELS:
        try:
            swp_cfg = swp_config()
            swp_want = swp_cfg["ident"]
        except Exception:               # noqa: BLE001 -- run_event records it per event
            swp_broken = True
    st_want, st_broken = None, False
    if STRESS in RUN_FILL_MODELS:
        try:
            if swp_cfg is None:
                raise ValueError("stress builds on swp, whose configuration failed")
            st_want = stress_config(swp_cfg)["ident"]
        except Exception:               # noqa: BLE001 -- run_event records it per event
            st_broken = True
    results: List[dict] = []
    todo = []
    for ev in evs:
        path = os.path.join(out_dir, f"{ev}.json")
        if os.path.exists(path) and not force:
            doc = C.read_json(path)
            if _cached_ok(doc, want, swp_want, swp_broken, st_want, st_broken):
                results.append(doc)
                continue
        try:
            inp = load_event_inputs(ev)
            settled = inp.settled
        except Exception:
            settled = False
        if not settled:
            results.append({"event": ev, "pending": True, "settled": False})
            continue
        todo.append((ev, path))
    w = DEFAULT_WORKERS if workers is None else int(workers)
    w = max(1, min(MAX_WORKERS, w, len(todo) or 1))
    draws = swp_want["draws"] if swp_want else None     # the workers' draw count = the cache's
    scope = st_want["scope"] if st_want else None       # ... and stress scope
    def _deferred(ev: str) -> dict:
        return {"event": ev, "deferred": True, "settled": True}

    if w == 1 or len(todo) <= 1:
        for ev, path in todo:
            if deadline is not None and time.time() >= deadline:
                results.append(_deferred(ev))
                continue
            results.append(_day_worker(ev, variants, path, draws, scope))
    else:
        from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
        with ProcessPoolExecutor(max_workers=w, initializer=_lower_priority) as ex:
            futs = {ex.submit(_day_worker, ev, variants, path, draws, scope): ev
                    for ev, path in todo}
            pending = set(futs)
            dl = deadline
            while pending:
                tmo = None if dl is None else max(0.0, dl - time.time())
                done, pending = wait(pending, timeout=tmo, return_when=FIRST_COMPLETED)
                for fu in done:
                    try:
                        results.append(fu.result())
                    except Exception as e:      # a dead worker
                        results.append({"event": futs[fu], "error": f"{type(e).__name__}: {e}"})
                if dl is not None and time.time() >= dl:
                    # past the budget: what has not started is deferred; the
                    # running events finish
                    for fu in list(pending):
                        if fu.cancel():
                            pending.discard(fu)
                            results.append(_deferred(futs[fu]))
                    dl = None
    results.sort(key=lambda d: d.get("event", ""))
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_event(res: dict) -> None:
    if res.get("error") or res.get("pending") or res.get("deferred"):
        print(f"{res.get('event')}: " + ("PENDING" if res.get("pending") else
                                         "DEFERRED" if res.get("deferred") else res.get("error")))
        return
    if res.get("swp_error"):
        print(f"{res['event']}: SWP FAILED (opt / cons kept): "
              + str(res["swp_error"]).splitlines()[0])
    if res.get("stress_error"):
        print(f"{res['event']}: STRESS FAILED (opt / cons / swp kept): "
              + str(res["stress_error"]).splitlines()[0])
    cov = res["coverage"]
    print(f"{res['event']} settled={res['settled']} value={res['settlement_value']} "
          f"book_trusted={cov['book_trusted_frac']} trades={cov['trades']} "
          f"wx_rows={cov['wx_rows']} runtime={res.get('runtime_s')}s flags={res.get('flags')}")
    print(f"  {'variant|fill':32s} {'reward':>8s} {'pnl':>9s} {'net':>9s} {'ctr':>8s} "
          f"{'share':>6s} {'m60':>6s}")
    for k, v in res["variants"].items():
        se = f" +/-{v['net_se']:.2f} (mc se, {v.get('draws')} draws)" if "net_se" in v else ""
        print(f"  {k:32s} {v['reward']:8.2f} {v['trading_pnl']:9.2f} {v['net']:9.2f} "
              f"{v['contracts']:8.0f} {v['avg_share']:6.3f} {v['markouts']['m60']:6.2f}{se}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hourly-temp shadow simulator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("day")
    d.add_argument("--day", required=True)
    d.add_argument("--force", action="store_true")
    d.add_argument("--workers", type=int, default=None)
    d.add_argument("--variants", default=None)
    e = sub.add_parser("event")
    e.add_argument("--event", required=True)
    e.add_argument("--variants", default=None, nargs="*")
    e.add_argument("--write", action="store_true", help="also write sim/<day>/<EVENT>.json")
    a = ap.parse_args(argv)
    if a.cmd == "day":
        vs = a.variants.split(",") if a.variants else None
        res = run_day(a.day, variants=vs, force=a.force, workers=a.workers)
        for r in res:
            _print_event(r)
        n_err = sum(1 for r in res if r.get("error"))
        print(f"{len(res)} events, {n_err} errors, "
              f"{sum(1 for r in res if r.get('pending'))} pending")
        return 1 if n_err else 0
    vs = None
    if a.variants:
        vs = [x for v in a.variants for x in v.split(",") if x]
    res = run_event(a.event, variants=vs)
    if a.write:
        C.write_json_atomic(os.path.join(C.sim_dir(C.et_day(res["settle_ts"])),
                                         f"{a.event}.json"), res)
    _print_event(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
