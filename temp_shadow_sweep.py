#!/usr/bin/env python3
"""Sweep-aware shadow for the hourly-temp market maker (KXTEMPMIAH).

Jack 2026-10-16: "run the sweep-aware shadow, without pausing". In the live
executor's first hour (variant G on subaccount 2, event KXTEMPMIAH-26OCT1610)
sweepers -- takers buying a near-certain favorite at 99c, i.e. hitting 1c
longshot bids -- took our 1c "pad" / "qual" fillers within seconds: ~6,900
contracts at 1c on 6 strikes in ~25 minutes, several strikes capped at
pos_cap 1,500 and then no longer qualifying. The shadow's opt / cons fill
models only fill us from RECORDED trades, and in the recording sweepers only
took the 1c liquidity that existed -- on the strongly decided strikes the
longshot 1c level was usually EMPTY (other makers do not rest there), so the
shadow saw our pads rest there alone, collect the whole side's reward share
and never fill. This module measures the sweep hazard and replays the policy
with a third fill model, "swp". It never places an order.

Fill model swp = cons, plus three sweep channels on our 1c orders:
  (a) recorded sweeps: a taker cluster of 1c prints on a side (taker buys
      YES at 99 -> NO bids at 1c; taker buys NO at 99 -> YES bids at 1c;
      prints within CLUSTER_S = one taker order) fills our resting 1c on that
      side: rec_mode "level" (default) takes ALL of ours when the taker
      cleared the recorded 1c level, else min(ours, the taker's size) -- we
      sit at the front of the queue because the other 1c makers re-place
      constantly (the live hour: a 9-lot taker filled 9 of our ~1,000, an
      850-lot one 850 with 522 others resting behind us); "size" always
      min(ours, size); "all" always all of ours (the task's first rule, which
      over-fills on small takers). In a live hour the cluster sizes include
      the prints that filled our live orders (removed from the replay tape by
      the subtraction); a cluster made only of our prints is kept when others
      rested at 1c and dropped when we rested alone (then it is (b)'s to
      predict).
  (b) hidden sweeps: while we rest >= 1 contract at 1c on a side whose
      EXTERNAL 1c level is empty (nobody else there, so the tape cannot show a
      sweep), an IOC sweeper arrives at the calibrated hazard
      lambda = a[favorite fair bucket][age of our 1c episode] x
      mult[minutes since open] -- fresh 1c liquidity on a > 99.5% favorite is
      swept at ~0.13/s (mean wait ~8 s), decaying to ~0.006/s after 2 min;
      the last 20 minutes run 1.2-2.2x. The taker's size is drawn from the
      recorded IOC sweeps of the same age bucket (cleared -> all of ours).
      Monte Carlo with fixed seeds, exact thinning of a Poisson(lam_max)
      stream; the candidate arrivals depend only on (event, draw), so every
      variant sees the same arrivals (common random numbers).
  (c) posting sweepers: a bid at 99 appearing on the opposite side (which
      would cross our 1c) is a taker that would have hit us first: it fills
      min(ours, the new 99 size). The hazard in (b) counts only sweeps that
      did NOT leave a 99 bid behind, so (b) and (c) do not double count.
Everything else (fills from other trades, queue rules, the policy, re-pads
after a fill, pos_cap / event WCL trimming, a capped strike no longer
qualifying) is temp_shadow_sim's cons model and temp_mm_policy, unchanged:
VPass subclasses temp_shadow_sim._Pass (its run loop is mirrored to add the
arrival / hazard events and the crossing check; with no options it reproduces
_Pass exactly -- test_temp_shadow_sweep checks it). Variants are explicit
overrides of temp_mm_policy.DEFAULT_POLICY (SWEEP_VARIANTS); G_hold180 drops
pad/qual-tagged orders from the policy output for the first 180 s.

Validation (validate --event KXTEMPMIAH-26OCT1610, hazard fit on the 35
pre-live hours only, 30 draws): filler contracts swept 10,317 predicted vs
10,006 actual (opt 3,905 / cons 3,264), reward $14.70 vs $14.11 (opt/cons
$45), trading P&L -$103 vs -$96, net -$88 vs -$82 (opt/cons +$7 / +$13).

Books: temp_shadow_scoring's live-order subtraction (default on) takes our
own live orders out of the recorded books and the trades that filled them out
of the tape, so a replay of a live hour sees the market without us.

CLI (outputs go to --out, default env TEMP_SHADOW_SWEEP_OUT or
<TEMP_SHADOW_DATA>/sweep):
  python temp_shadow_sweep.py calibrate [--events ...] [--days ...]
  python temp_shadow_sweep.py run --day YYYY-MM-DD [...] [--draws N] [--variants ...]
                                  [--rec-mode level|size|all] [--workers N]
  python temp_shadow_sweep.py validate --event EVT [--draws N]
  python temp_shadow_sweep.py table [--days ...]       (aggregate run outputs)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import sys
import time
import traceback
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

import temp_mm_policy as pol
import temp_shadow_common as C
import temp_shadow_model as tmodel
import temp_shadow_scoring as scoring
import temp_shadow_sim as sim

SWEEP_VERSION = "1"
FILL_MODELS = ("opt", "cons", "swp")
CLUSTER_S = 1.0          # 1c prints on one (market, side) within 1 s of the
                         # first = one taker order (Kalshi prints one trade per
                         # maker filled, all at the taker's timestamp)
POST_WINDOW_S = 3        # a 99 bid appearing within 3 s after a sweep = the
                         # taker posted its unfilled rest (a "posting" sweeper)
PRIOR_S = 60.0           # shrinkage weight (exposure seconds) of a hazard cell
                         # toward its favorite-bucket pool (the fresh-liquidity
                         # cells are short in seconds but rich in sweeps)
DEFAULT_DRAWS = 30
REC_MODES = ("level", "size", "all")
SEED_BASE = "tmm-sweep-1"
FAV_EDGES = (0.90, 0.97, 0.99, 0.995)
FAV_LABELS = ("<0.90", "0.90-0.97", "0.97-0.99", "0.99-0.995", ">0.995")
MIN_EDGES = (2, 3, 5, 10, 20, 40, 50, 55, 58)
MIN_LABELS = ("0-2", "2-3", "3-5", "5-10", "10-20", "20-40", "40-50", "50-55",
              "55-58", "58-60")
AGE_EDGES = (5, 30, 120, 600)
AGE_LABELS = ("0-5s", "5-30s", "30-120s", "120-600s", "600s+")
LVL_EDGES = (100, 500, 1500)
LVL_LABELS = ("<100", "100-500", "500-1500", "1500+")
REACT_S = float(pol.DEFAULT_POLICY["react_s"])
LIVE_START_TS = C.parse_iso("2026-10-16T13:00:00Z")   # first live order hour
MAX_WORKERS = 4


def out_dir_default() -> str:
    return os.environ.get("TEMP_SHADOW_SWEEP_OUT") or os.path.join(C.DATA_ROOT, "sweep")


def _bucket(x: float, edges: Sequence[float], labels: Sequence[str]) -> str:
    return labels[bisect.bisect_right(list(edges), x)]


def fav_bucket(fav_p: float) -> str:
    return _bucket(fav_p, FAV_EDGES, FAV_LABELS)


def min_bucket(mins: float) -> str:
    return _bucket(mins, MIN_EDGES, MIN_LABELS)


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------
# Variants (explicit overrides of temp_mm_policy.DEFAULT_POLICY; the VARIANTS
# names are being changed by another workflow, so nothing here relies on them)
# --------------------------------------------------------------------------

G_POLICY = dict(live=False, decided=True, q_pad=1000, q_fav=300, fav_min_p=0.97,
                near_decided_half=True, qual_pads=True, pad_extra=50, skew=0.0,
                pos_cap=1500, event_wcl_cap=1500.0, program_only=True,
                window_mode="pull", fair_guard=True, wcl_resting=False)


def _g(**kw) -> dict:
    d = dict(G_POLICY)
    d.update(kw)
    return d


# name -> {"policy": overrides of DEFAULT_POLICY, "hold_pads_s": seconds after
# open during which pad/qual-tagged orders are dropped from the policy output}
SWEEP_VARIANTS: Dict[str, dict] = {
    "G": {"policy": _g()},
    "G_hold180": {"policy": _g(), "hold_pads_s": 180.0},
    "pads_only": {"policy": _g(q_fav=0)},
    "J_fav100": {"policy": _g(q_fav=100)},
    "G_pad200": {"policy": _g(q_pad=200)},
    "G_pad500": {"policy": _g(q_pad=500)},
    "no_fillers": {"policy": _g(q_pad=0, qual_pads=False)},
    "G_cap500": {"policy": _g(pos_cap=500)},
    "G_cap3000": {"policy": _g(pos_cap=3000)},
    "F": {"policy": _g(live=True, q_live=600, skew=0.3)},
    # extras: F's live-strike quoting without the decided-strike fillers
    "F_nofill": {"policy": _g(live=True, q_live=600, skew=0.3, q_pad=0, qual_pads=False)},
    "F_live_only": {"policy": _g(live=True, q_live=600, skew=0.3, decided=False)},
}


def resolve_variant(spec: dict) -> Tuple[dict, Optional[float]]:
    """-> (full policy dict, hold_pads_s)."""
    ov = dict(spec.get("policy") or {})
    bad = set(ov) - set(pol.DEFAULT_POLICY)
    if bad:
        raise ValueError(f"unknown policy keys {sorted(bad)}")
    P = dict(pol.DEFAULT_POLICY)
    P.update(ov)
    if P.get("wcl_resting"):
        raise ValueError("wcl_resting is not simulated")
    hold = spec.get("hold_pads_s")
    return P, (float(hold) if hold else None)


def pick_variants(names) -> Dict[str, dict]:
    if not names:
        return dict(SWEEP_VARIANTS)
    if isinstance(names, str):
        names = [n for n in names.split(",") if n]
    out = {}
    for n in names:
        if n not in SWEEP_VARIANTS:
            raise ValueError(f"unknown sweep variant {n!r}; known {sorted(SWEEP_VARIANTS)}")
        out[n] = SWEEP_VARIANTS[n]
    return out


# --------------------------------------------------------------------------
# Hazard table
# --------------------------------------------------------------------------

# Built-in table = the 2026-10-16 calibration (python temp_shadow_sweep.py
# calibrate over every recorded hour); a calibrate run's hazard.json replaces
# it via --hazard. IOC sweeps per second while >= 1 contract rests at 1c on the
# longshot side: a[favorite fair bucket][age of the 1c episode] x
# mult[minutes since open]. sizes[age]: the IOC sweeps' (taker size,
# cleared-the-level) pairs, drawn for a hidden sweep's size.
DEFAULT_HAZARD: dict = json.loads(
    '{"built": "2026-10-16T14:13:51Z", "events": 36, "hazard": {"kind": "ioc", "model": '
    '"a[fav][age] x mult[min]", "a": {"<0.90": {"0-5s": 0.000499, "5-30s": 0.00041, "30-120s": '
    '0.000193, "120-600s": 7.8e-05, "600s+": 3.4e-05}, "0.90-0.97": {"0-5s": 0.009117, "5-30s": '
    '0.011175, "30-120s": 0.003012, "120-600s": 0.001848, "600s+": 0.001514}, "0.97-0.99": '
    '{"0-5s": 0.020948, "5-30s": 0.015545, "30-120s": 0.006181, "120-600s": 0.003423, "600s+": '
    '0.002928}, "0.99-0.995": {"0-5s": 0.029471, "5-30s": 0.015825, "30-120s": 0.005243, '
    '"120-600s": 0.003555, "600s+": 0.00479}, ">0.995": {"0-5s": 0.127939, "5-30s": 0.023143, '
    '"30-120s": 0.0119, "120-600s": 0.007007, "600s+": 0.006365}}, "mult": {"0-2": 1.1045, "2-3": '
    '1.0412, "3-5": 1.118, "5-10": 0.8014, "10-20": 0.8219, "20-40": 0.8067, "40-50": 1.6207, '
    '"50-55": 1.1801, "55-58": 1.4765, "58-60": 2.1848}, "lam_pool": {"<0.90": 5.1e-05, '
    '"0.90-0.97": 0.002135, "0.97-0.99": 0.004244, "0.99-0.995": 0.005372, ">0.995": 0.009585}}, '
    '"sizes": {"0-5s": [[0.01, 0], [2.0, 0], [20.93, 0], [70.0, 0], [70.0, 0], [70.0, 0], [100.0, '
    '0], [5.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [9.0, 1], [10.0, 1], [10.0, '
    '1], [13.0, 1], [13.0, 1], [15.0, 1], [21.0, 1], [41.0, 1], [50.0, 1], [58.0, 1], [70.0, 1], '
    '[94.0, 1], [100.0, 1], [100.0, 1], [100.0, 1], [100.0, 1], [100.0, 1], [100.0, 1], [100.0, '
    '1], [100.0, 1], [150.0, 1], [193.0, 1], [200.0, 1], [200.0, 1], [200.0, 1], [228.99, 1], '
    '[600.0, 1]], "5-30s": [[0.01, 0], [0.01, 0], [2.0, 0], [4.0, 0], [4.0, 0], [5.0, 0], [10.0, '
    '0], [10.0, 0], [14.0, 0], [20.92, 0], [20.93, 0], [20.94, 0], [22.3, 0], [70.0, 0], [310.0, '
    '0], [1.0, 1], [2.0, 1], [3.02, 1], [5.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [13.0, 1], [16.0, '
    '1], [20.0, 1], [26.0, 1], [45.0, 1], [52.0, 1], [62.0, 1], [100.0, 1], [106.0, 1], [126.0, '
    '1], [138.0, 1], [155.0, 1], [200.0, 1], [200.0, 1], [201.0, 1], [293.0, 1], [578.0, 1], '
    '[1293.0, 1]], "30-120s": [[0.01, 0], [0.09, 0], [1.0, 0], [2.0, 0], [4.0, 0], [4.61, 0], '
    '[5.04, 0], [5.55, 0], [9.0, 0], [10.0, 0], [10.0, 0], [15.94, 0], [17.0, 0], [20.0, 0], '
    '[20.93, 0], [20.94, 0], [21.0, 0], [31.21, 0], [38.0, 0], [50.0, 0], [100.0, 0], [150.0, 0], '
    '[395.0, 0], [2.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [5.0, 1], [10.0, 1], [15.0, 1], [26.0, '
    '1], [100.0, 1], [119.0, 1], [144.0, 1], [185.05, 1], [200.0, 1], [210.0, 1], [228.0, 1], '
    '[250.0, 1], [1228.0, 1]], "120-600s": [[0.01, 0], [1.0, 0], [1.0, 0], [1.0, 0], [2.0, 0], '
    '[2.52, 0], [4.0, 0], [5.04, 0], [5.04, 0], [5.68, 0], [9.0, 0], [10.0, 0], [10.0, 0], '
    '[10.35, 0], [15.0, 0], [15.14, 0], [17.0, 0], [20.91, 0], [20.93, 0], [21.0, 0], [29.0, 0], '
    '[32.0, 0], [32.0, 0], [38.0, 0], [47.0, 0], [70.0, 0], [81.0, 0], [150.0, 0], [177.1, 0], '
    '[300.0, 0], [965.0, 0], [5000.0, 0], [13.0, 1], [89.0, 1], [133.83, 1], [181.69, 1], [228.0, '
    '1], [968.0, 1], [1678.0, 1], [2156.0, 1]], "600s+": [[0.01, 0], [0.01, 0], [1.0, 0], [2.0, '
    '0], [2.0, 0], [2.84, 0], [4.0, 0], [4.0, 0], [5.0, 0], [5.04, 0], [5.83, 0], [8.0, 0], '
    '[10.0, 0], [10.0, 0], [10.0, 0], [10.0, 0], [12.78, 0], [15.0, 0], [15.0, 0], [15.0, 0], '
    '[16.41, 0], [21.0, 0], [31.21, 0], [50.0, 0], [56.0, 0], [78.14, 0], [95.0, 0], [108.0, 0], '
    '[150.0, 0], [162.28, 0], [207.0, 0], [250.0, 0], [250.0, 0], [290.0, 0], [1000.0, 0], [11.0, '
    '1], [89.0, 1], [168.0, 1], [229.0, 1], [428.46, 1]]}, "sizes_all": [[0.01, 0], [0.09, 0], '
    '[1.0, 0], [2.0, 0], [3.0, 0], [4.0, 0], [5.0, 0], [5.04, 0], [6.05, 0], [10.0, 0], [10.0, '
    '0], [10.0, 0], [15.0, 0], [15.0, 0], [16.41, 0], [20.92, 0], [21.0, 0], [32.0, 0], [41.0, '
    '0], [63.0, 0], [78.0, 0], [100.0, 0], [150.0, 0], [200.0, 0], [250.0, 0], [500.0, 0], [2.0, '
    '1], [5.0, 1], [5.0, 1], [12.0, 1], [18.0, 1], [48.0, 1], [85.0, 1], [100.0, 1], [112.39, 1], '
    '[155.0, 1], [200.0, 1], [214.0, 1], [313.0, 1], [1163.0, 1]], "source": "calibrate '
    '2026-10-16 over 36 recorded hours (26OCT1421-26OCT1610)"}')


class Hazard:
    """lambda(fav_p, minutes since open, age of our 1c episode) for hidden
    (IOC) sweeps of our 1c orders, and the taker-size draw."""

    def __init__(self, doc: Optional[dict] = None, scale: float = 1.0):
        doc = doc or DEFAULT_HAZARD
        hz = doc.get("hazard", doc)
        self.doc = doc
        self.scale = float(scale)
        self.a = {fb: {ab: float(r) for ab, r in (d or {}).items()}
                  for fb, d in (hz.get("a") or {}).items()}
        self.mult = {mb: float(x) for mb, x in (hz.get("mult") or {}).items()}
        self.pool = {k: float(v) for k, v in (hz.get("lam_pool") or {}).items()}
        sz = doc.get("sizes") or {}
        self.sizes = {ab: [(float(q), bool(c)) for q, c in lst] for ab, lst in sz.items() if lst}
        allz = doc.get("sizes_all") or [[1000.0, 1]]
        self.sizes_all = [(float(q), bool(c)) for q, c in allz] or [(1000.0, True)]
        amax = max([r for d in self.a.values() for r in d.values()] + list(self.pool.values())
                   + [0.0])
        self.lam_max = amax * max(list(self.mult.values()) + [1.0]) * self.scale

    def rate(self, fav_p: Optional[float], mins: float, age_s: float) -> float:
        if fav_p is None:
            return 0.0
        fb = fav_bucket(fav_p)
        r = self.a.get(fb, {}).get(_bucket(age_s, AGE_EDGES, AGE_LABELS))
        if r is None:
            r = self.pool.get(fb, 0.0)
        return r * self.mult.get(min_bucket(mins), 1.0) * self.scale

    def take(self, u: float, ours: float, age_s: float) -> float:
        lst = self.sizes.get(_bucket(age_s, AGE_EDGES, AGE_LABELS)) or self.sizes_all
        q, cleared = lst[min(len(lst) - 1, int(u * len(lst)))]
        return ours if cleared else min(ours, q)


def compact_hazard(doc: dict, per_bucket: int = 40) -> dict:
    """A calibrate hazard.json cut to what Hazard reads (the fit, and at
    most `per_bucket` size pairs per age bucket, a quantile-even sample of
    the (cleared, size)-sorted list) -- the shape of DEFAULT_HAZARD."""
    def thin(lst):
        lst = sorted((float(q), int(c)) for q, c in lst)
        lst.sort(key=lambda x: (x[1], x[0]))
        if len(lst) <= per_bucket:
            return [[q, c] for q, c in lst]
        step = len(lst) / float(per_bucket)
        return [[lst[int(i * step)][0], lst[int(i * step)][1]] for i in range(per_bucket)]
    hz = doc.get("hazard", {})
    return {"built": doc.get("built"), "events": len(doc.get("events") or []),
            "hazard": {k: hz[k] for k in ("kind", "model", "a", "mult", "lam_pool") if k in hz},
            "sizes": {ab: thin(lst) for ab, lst in (doc.get("sizes") or {}).items()},
            "sizes_all": thin(doc.get("sizes_all") or [])}


def load_hazard(path: Optional[str] = None, scale: float = 1.0) -> Hazard:
    if path:
        doc = C.read_json(path)
        if not isinstance(doc, dict):
            raise ValueError(f"cannot read hazard table {path}")
        return Hazard(doc, scale)
    p = os.path.join(out_dir_default(), "hazard.json")
    doc = C.read_json(p) if os.path.exists(p) else None
    if not isinstance(doc, dict):
        if DEFAULT_HAZARD.get("placeholder"):
            raise ValueError("no hazard table: run `temp_shadow_sweep.py calibrate` or pass --hazard")
        doc = DEFAULT_HAZARD
    return Hazard(doc, scale)


# --------------------------------------------------------------------------
# Shared per-event state (temp_shadow_sim._Shared + the stream + sweep prep)
# --------------------------------------------------------------------------

class SweepShared(sim._Shared):
    """temp_shadow_sim._Shared that keeps the EventStream, chooses live-order
    subtraction, and indexes the tape's 1c prints into taker clusters."""

    def __init__(self, inp, model, until_ts=None, subtract: bool = True):
        self._subtract = bool(subtract)
        self.stream = None
        super().__init__(inp, model, until_ts)
        self._prep_sweeps()

    def _load_books(self):
        orig = scoring.load_event_stream
        holder = {}
        sub = self._subtract

        def wrapped(*a, **k):
            k.setdefault("subtract_live", sub)
            s = orig(*a, **k)
            holder["s"] = s
            return s
        scoring.load_event_stream = wrapped
        try:
            super()._load_books()
        finally:
            scoring.load_event_stream = orig
        self.stream = holder.get("s")

    def _prep_sweeps(self):
        nt = len(self.trades)
        self.sw_side: List[Optional[str]] = [None] * nt
        self.sw_cid: List[int] = [-1] * nt
        self.clusters: List[dict] = []
        open_c: Dict[tuple, int] = {}
        for j, (ts, m, taker, yc, cnt) in enumerate(self.trades):
            side = "yes" if taker == "no" else "no"       # the book side hit
            px = yc if side == "yes" else 100 - yc
            if px != 1:
                continue
            key = (m, side)
            cid = open_c.get(key)
            if cid is None or ts - self.clusters[cid]["t0"] > CLUSTER_S:
                cid = len(self.clusters)
                self.clusters.append(dict(m=m, side=side, t0=ts, t1=ts, q=0.0, n=0, j0=j))
                open_c[key] = cid
            c = self.clusters[cid]
            c["t1"] = ts
            c["q"] += cnt
            c["n"] += 1
            self.sw_side[j] = side
            self.sw_cid[j] = cid
        self._prep_arrivals()

    def _own_prints(self) -> List[tuple]:
        """The tape rows that filled our LIVE orders (removed from
        self.trades by the subtraction), as (ts, m, taker, yes_cents, count)."""
        out = []
        for tr in (getattr(self.stream, "own_trades", None) or []):
            m = self.mindex.get(tr.ticker)
            ts = sim._trade_time(tr)
            if m is None or ts is None or ts < self.sec0 or ts > self.close:
                continue
            out.append((ts, m, str(tr.taker_side).lower(), int(round(float(tr.yes_cents))),
                        float(tr.count)))
        return out

    def _prep_arrivals(self):
        """Recorded sweep ARRIVALS for channel (a): taker clusters of 1c prints
        over the FULL tape (the replay tape plus the prints that filled our
        live orders, which the subtraction removed). q = the taker's whole
        executed size; cleared = it took the whole recorded 1c level (ours
        included). A cluster made only of our prints is kept when someone
        else rested at 1c (the taker would have hit them without us) and
        dropped when we rested alone (a market without us shows no such
        print: the hidden-sweep hazard (b) is what predicts it). Without live
        orders the arrivals are exactly the replay tape's 1c clusters."""
        rows = [(ts, m, tk, yc, cnt, False) for ts, m, tk, yc, cnt in self.trades]
        rows += [(ts, m, tk, yc, cnt, True) for ts, m, tk, yc, cnt in self._own_prints()]
        rows.sort(key=lambda r: r[0])
        lo = getattr(self.stream, "live_own", None)
        own_tl = lo.timeline(self.sec0, self.sec1) if lo else None
        cl: List[dict] = []
        open_c: Dict[tuple, dict] = {}
        for ts, m, taker, yc, cnt, own in rows:
            side = "yes" if taker == "no" else "no"
            if (yc if side == "yes" else 100 - yc) != 1:
                continue
            c = open_c.get((m, side))
            if c is None or ts - c["t0"] > CLUSTER_S:
                c = dict(t0=ts, m=m, side=side, q=0.0, q_own=0.0, n_other=0)
                cl.append(c)
                open_c[(m, side)] = c
            c["q"] += cnt
            if own:
                c["q_own"] += cnt
            else:
                c["n_other"] += 1
        arr = []
        for c in cl:
            m, side, t0 = c["m"], c["side"], c["t0"]
            i0 = self.idx_at(t0)
            lb = [x for x in (self.level1(m, side, i0 - 1), self.level1(m, side, i0))
                  if x is not None]
            l_ext = max(lb) if lb else None
            l_own = 0.0
            if own_tl is not None:
                for ii in (i0 - 1, i0):
                    if 0 <= ii < len(own_tl):
                        o = own_tl[ii].get(self.tickers[m])
                        if o:
                            l_own = max(l_own, (o[0] if side == "yes" else o[1]).get(1, 0.0))
            if c["n_other"] == 0 and not (l_ext is not None and l_ext >= 1.0):
                continue                     # we rested alone: (b)'s job
            l_full = (l_ext or 0.0) + l_own
            arr.append((t0, m, side, c["q"], bool(c["q"] >= l_full - 0.5), c["q_own"]))
        self.arrivals = arr

    # -- book probes (external book; None = untrusted) -------------------
    def info_at_idx(self, m: int, i: int):
        if not (0 <= i < self.n):
            return None
        v = self.ver[m][i]
        return self.vinfo[m][v] if v >= 0 else None

    def level1(self, m: int, side: str, i: int) -> Optional[float]:
        info = self.info_at_idx(m, i)
        if info is None:
            return None
        return float((info.by if side == "yes" else info.bn).get(1, 0.0))

    def opp99(self, m: int, side: str, i: int) -> Optional[float]:
        """Size bid at 99 (the top price) on the side OPPOSITE `side`: it
        crosses a 1c bid on `side`."""
        info = self.info_at_idx(m, i)
        if info is None:
            return None
        return float((info.bn if side == "yes" else info.by).get(99, 0.0))

    def fair_or_mid(self, m: int, t: float) -> Optional[float]:
        f = self.fair_at(m, t, REACT_S)
        if f is not None:
            return f
        info = self.info_at_idx(m, self.idx_at(t))
        if info is not None and info.mid is not None:
            return info.mid / 100.0
        return None


def fav_p_of(side: str, f: Optional[float]) -> Optional[float]:
    """P(the side OPPOSITE a `side` bid wins) = the favorite's fair when a
    `side` bid at 1c is the longshot."""
    if f is None:
        return None
    return (1.0 - f) if side == "yes" else f


def shared_for(event: str, model=None, subtract: bool = True, until_ts=None) -> SweepShared:
    inp = sim.load_event_inputs(event)
    if model is None:
        model = tmodel.FairModel()
    return SweepShared(inp, model, until_ts, subtract=subtract)


# --------------------------------------------------------------------------
# Hazard candidates (Monte Carlo draws)
# --------------------------------------------------------------------------

def hazard_candidates(sh: SweepShared, draw: int, lam_max: float,
                      seed: str = SEED_BASE) -> List[tuple]:
    """A Poisson(lam_max) stream per (market, side) over [open, end): sorted
    [(t, m, side, u_accept, u_size)]. Depends only on (event, draw, seed), so
    every variant sees the same arrivals (common random numbers)."""
    if lam_max <= 0:
        return []
    out = []
    for m in range(sh.nm):
        for side in ("yes", "no"):
            rng = random.Random(f"{seed}|{sh.inp.event}|{draw}|{m}|{side}")
            t = sh.open
            while True:
                t += rng.expovariate(lam_max)
                if t >= sh.end_t:
                    break
                out.append((t, m, side, rng.random(), rng.random()))
    out.sort(key=lambda c: c[0])
    return out


# --------------------------------------------------------------------------
# The pass: temp_shadow_sim._Pass + pad hold + the swp fill model
# --------------------------------------------------------------------------

_PADQUAL = {"pad", "qual"}


class VPass(sim._Pass):
    """One policy x fill-model pass. fill_model 'opt' / 'cons' = the sim's
    models; 'swp' = cons + sweep channels (a)/(b)/(c) (module doc).
    hold_pads_s: pad/qual-tagged orders are dropped from the policy output
    for decisions before open + hold_pads_s."""

    def __init__(self, sh: SweepShared, P: dict, fill_model: str,
                 hold_pads_s: Optional[float] = None, hazard: Optional[Hazard] = None,
                 cands: Optional[List[tuple]] = None, rec_mode: str = "level",
                 channels: str = "abc", detail: bool = False, own_arrivals: bool = True):
        if fill_model not in FILL_MODELS:
            raise ValueError(f"unknown fill model {fill_model!r}")
        super().__init__(sh, P, "cons" if fill_model == "swp" else fill_model)
        self.fm_name = fill_model
        self.swp = fill_model == "swp"
        self.hold_until = (sh.open + float(hold_pads_s)) if hold_pads_s else None
        self.P_hold = dict(P, q_pad=0, qual_pads=False) if hold_pads_s else None
        self.hazard = hazard
        self.cands = cands or []
        if rec_mode not in REC_MODES:
            raise ValueError(f"rec_mode must be one of {REC_MODES}")
        self.rec_mode = rec_mode
        self.ch = set(channels)
        self.own_arrivals = bool(own_arrivals)   # False: drop taker clusters that
        # hit our LIVE orders (a strict out-of-sample test of the hazard)
        self.detail = detail
        self.src = "trade"
        self.sweep_fills: List[tuple] = []     # (ts, m, side, q, channel)
        self.ep1c = [{"yes": None, "no": None} for _ in range(sh.nm)]   # our 1c since
        self.prev99 = [{"yes": 0.0, "no": 0.0} for _ in range(sh.nm)]
        self.prev_v = [-2] * sh.nm
        self.haz_n = 0
        self.haz_checked = 0

    # -- pad hold ----------------------------------------------------------
    def _desired(self, m, t, v, f, gf, wflag, pflag, size_full, risk):
        if self.hold_until is None or t >= self.hold_until:
            return super()._desired(m, t, v, f, gf, wflag, pflag, size_full, risk)
        info = self.sh.vinfo[m][v] if v >= 0 else None
        tgt = self.sh.progs[m]["target"]
        tags: dict = {}
        dy, dn, rule = pol.quote_market(self.P, info, f, gf, wflag, pflag, size_full,
                                        risk[m], tgt, tags)
        hold = None
        for (side, p), tg in tags.items():
            if not (set(tg) & _PADQUAL):
                continue
            d = dy if side == "yes" else dn
            if p not in d:
                continue
            if set(tg) <= _PADQUAL:
                del d[p]
            else:                     # shares its price with a live/near/fav order
                if hold is None:
                    hold = pol.quote_market(self.P_hold, info, f, gf, wflag, pflag,
                                            size_full, risk[m], tgt)
                hd = hold[0] if side == "yes" else hold[1]
                if p in hd:
                    d[p] = hd[p]
                else:
                    del d[p]
        return dy, dn, rule

    # -- 1c episode age (hazard) -------------------------------------------
    def _ep_update(self, m, t):
        o, ep = self.orders[m], self.ep1c[m]
        for side in ("yes", "no"):
            if 1 in o[side]:
                if ep[side] is None:
                    ep[side] = t
            else:
                ep[side] = None

    def _activate(self, item):
        super()._activate(item)
        if self.swp:
            self._ep_update(item[1], item[0])

    # -- fills ---------------------------------------------------------------
    def _record_fill(self, ts, m, side, p, q):
        super()._record_fill(ts, m, side, p, q)
        if self.src != "trade":
            self.sweep_fills.append((ts, m, side, q, self.src))

    def _fill_1c(self, ts, m, side, want: Optional[float], src: str) -> float:
        """Fill up to `want` (None = all) of our resting 1c orders on `side`,
        front lot first."""
        book = self.orders[m][side]
        lots = book.get(1)
        if not lots:
            return 0.0
        left = math.inf if want is None else float(want)
        got = 0.0
        for lot in lots:
            if left <= 1e-9:
                break
            q = min(lot[0], left)
            lot[0] -= q
            left -= q
            got += q
        if got <= 1e-9:
            return 0.0
        book[1] = [l for l in lots if l[0] > 1e-9]
        if not book[1]:
            del book[1]
            self.ep1c[m][side] = None
        prev, self.src = self.src, src
        try:
            self._record_fill(ts, m, side, 1, got)
        finally:
            self.src = prev
        return got

    def _on_trade_j(self, tr, j):
        """A tape row. In swp a 1c print only fills our better-priced lots by
        the cons rules here: the 1c level itself is channel (a), handled per
        taker cluster by _on_arrival."""
        if not self.swp or "a" not in self.ch or self.sh.sw_side[j] is None:
            return self._on_trade(tr)
        book = self.orders[tr[1]][self.sh.sw_side[j]]
        held = book.pop(1, None)
        try:
            if book:
                self._on_trade(tr)
        finally:
            if held:
                book[1] = held

    def _on_arrival(self, a):
        """(a): a recorded taker cluster at a side's 1c level. rec_mode
        'level' (default): all of our 1c when the taker cleared the recorded
        level, else min(ours, its size) (front of the queue: the other 1c
        makers re-place constantly); 'size': always min(ours, its size);
        'all': always all of ours."""
        t, m, side, q, cleared = a[0], a[1], a[2], a[3], a[4]
        if 1 not in self.orders[m][side]:
            return
        if self.rec_mode == "all" or (self.rec_mode == "level" and cleared):
            want = None
        else:
            want = q
        self._fill_1c(t, m, side, want, "rec")

    def _on_hazard(self, c):
        t, m, side, u_acc, u_size = c
        lots = self.orders[m][side].get(1)
        if not lots:
            return
        ours = sum(l[0] for l in lots)
        if ours < 1.0 - 1e-9:
            return
        sh = self.sh
        i = sh.idx_at(t)
        ext = sh.level1(m, side, i)
        if ext is None or ext >= 1.0:
            return                     # untrusted, or covered: the tape decides
        self.haz_checked += 1
        f = sh.fair_or_mid(m, t)
        fp = fav_p_of(side, f)
        ep = self.ep1c[m][side]
        age = (t - ep) if ep is not None else 0.0
        lam = self.hazard.rate(fp, (t - sh.open) / 60.0, age)
        if lam <= 0 or u_acc * self.hazard.lam_max >= lam:
            return
        self.haz_n += 1
        self._fill_1c(t, m, side, self.hazard.take(u_size, ours, age), "haz")

    def _cross_check(self, i, T):
        """(c): a 99 bid appearing opposite our resting 1c fills us first
        (min(ours, the new 99 size)). The last trusted 99 size is kept
        through untrusted spans."""
        sh = self.sh
        for m in range(sh.nm):
            v = sh.ver[m][i] if 0 <= i < sh.n else -1
            if v == self.prev_v[m] or v < 0:
                continue
            self.prev_v[m] = v
            info = sh.vinfo[m][v]
            o = self.orders[m]
            pv = self.prev99[m]
            for side in ("yes", "no"):
                cur = float((info.bn if side == "yes" else info.by).get(99, 0.0))
                new = cur - pv[side]
                pv[side] = cur
                if new >= 1.0 - 1e-9 and 1 in o[side]:
                    self._fill_1c(T, m, side, new, "cross")

    # -- run (mirrors temp_shadow_sim._Pass.run, SIM_VERSION 4) ------------
    def run(self) -> dict:
        sh, P = self.sh, self.P
        tick_fs, events = sh.react_view(float(P["react_s"]))
        pending = self.pending
        sec0 = sh.sec0
        extra = []
        if self.swp and "a" in self.ch:
            extra.extend((a[0], 0, a) for a in sh.arrivals
                         if self.own_arrivals or not a[5])
        if self.swp and "b" in self.ch and self.hazard:
            extra.extend((c[0], 1, c) for c in self.cands)
        extra.sort(key=lambda e: (e[0], e[1]))
        nc, k = len(extra), 0
        cross = self.swp and "c" in self.ch
        hold_t = self.hold_until
        for t, kind, a in events:
            while k < nc and extra[k][0] < t:
                te, ek, c = extra[k]
                k += 1
                while pending and pending[0][0] <= te:
                    self._activate(pending.popleft())
                if ek == 0:
                    self._on_arrival(c)
                else:
                    self._on_hazard(c)
            if hold_t is not None and t >= hold_t:
                self.last_key = [None] * sh.nm      # re-decide without the hold
                hold_t = None
            while pending and pending[0][0] <= t:
                self._activate(pending.popleft())
            if kind == sim._TRADE:
                self._on_trade_j(sh.trades[a], a)
            elif kind == sim._TICK:
                if cross:
                    self._cross_check(a, t)
                self._score(a, t)
                self._decide_all(t, a, tick_fs[a], t + 1.0)
            else:
                idx = sh.idx_at(t)
                self._decide_all(t, idx, a, sec0 + idx + 1 + 0.5)
        out = self._summary()
        out.update(self._extra())
        return out

    def _extra(self) -> dict:
        sh = self.sh
        by_src = defaultdict(float)
        for ts, m, side, q, src in self.sweep_fills:
            by_src[src] += q
        f1 = defaultdict(float)
        pnl_fair = pnl_fair_1c = 0.0
        for ts, m, side, p, q, cls, f, after in self.fills:
            if p == 1:
                f1[sh.tickers[m]] += q
            # each fill marked at the model fair when it filled (book mid when
            # the model has none): the expected value of the position under
            # the model, free of the few-upsets outcome noise
            fy = f if f is not None else sh.fair_or_mid(m, ts)
            if fy is None:
                fy = sh.payoff_yes(m)
            v = q * ((fy if side == "yes" else 1.0 - fy) - p / 100.0)
            pnl_fair += v
            if p == 1:
                pnl_fair_1c += v
        ex = {"filler_contracts": _r(sum(f1.values()), 2),
              "pnl_fair": _r(pnl_fair, 4), "pnl_fair_1c": _r(pnl_fair_1c, 4),
              "net_fair": _r(self.reward + pnl_fair, 4),
              "sweep_src": {k: _r(v, 2) for k, v in by_src.items()},
              "haz_events": self.haz_n,
              "pos": {sh.tickers[m]: _r(self.yes_q[m] - self.no_q[m], 2)
                      for m in range(sh.nm) if self.yes_q[m] or self.no_q[m]},
              "filler_by_strike": {t: _r(v, 2) for t, v in f1.items()}}
        if self.detail:
            ex["fills_detail"] = [(round(ts, 3), sh.tickers[m], side, p, round(q, 2))
                                  for ts, m, side, p, q, cls, f, after in self.fills]
            ex["sweep_detail"] = [(round(ts, 3), sh.tickers[m], side, round(q, 2), src)
                                  for ts, m, side, q, src in self.sweep_fills]
        return ex


def run_swp(sh: SweepShared, P: dict, hazard: Hazard, draws: int,
            hold_pads_s: Optional[float] = None, rec_mode: str = "level",
            channels: str = "abc", detail: bool = False, seed: str = SEED_BASE,
            cand_cache: Optional[dict] = None) -> dict:
    """Mean of `draws` swp passes (draw k uses hazard_candidates(sh, k)):
    the mean summary plus per-draw net / reward / pnl / contracts and their
    standard deviations."""
    runs = []
    for k in range(int(draws)):
        cands = None
        if "b" in channels:
            key = (k, round(hazard.lam_max, 12), seed)
            if cand_cache is not None and key in cand_cache:
                cands = cand_cache[key]
            else:
                cands = hazard_candidates(sh, k, hazard.lam_max, seed)
                if cand_cache is not None:
                    cand_cache[key] = cands
        runs.append(VPass(sh, P, "swp", hold_pads_s=hold_pads_s, hazard=hazard, cands=cands,
                          rec_mode=rec_mode, channels=channels, detail=detail).run())
    return mean_summary(runs, keep_detail=detail)


_NUM_KEYS = ("reward", "fills_n", "contracts", "trading_pnl", "fees", "net", "avg_share",
             "quoted_secs", "max_abs_pos", "worst_case_loss", "filler_contracts", "haz_events",
             "pnl_fair", "pnl_fair_1c", "net_fair")


def mean_summary(runs: List[dict], keep_detail: bool = False) -> dict:
    n = len(runs)
    if n == 0:
        return {}
    out = {}
    for k in _NUM_KEYS:
        vals = [float(r.get(k) or 0.0) for r in runs]
        out[k] = _r(sum(vals) / n, 6)
    for k in ("net", "reward", "trading_pnl", "contracts", "filler_contracts"):
        vals = [float(r.get(k) or 0.0) for r in runs]
        mu = sum(vals) / n
        out[f"{k}_sd"] = _r(math.sqrt(sum((v - mu) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0, 6)
    out["draws"] = n
    out["draw_net"] = [_r(r.get("net"), 4) for r in runs]

    def avg_nested(key):
        acc = defaultdict(float)
        for r in runs:
            for kk, vv in (r.get(key) or {}).items():
                if isinstance(vv, (int, float)):
                    acc[kk] += float(vv)
        return {kk: _r(vv / n, 4) for kk, vv in acc.items()}
    for key in ("sweep_src", "pos", "filler_by_strike", "markouts"):
        out[key] = avg_nested(key)
    for key in ("by_class", "by_tau", "by_side"):
        acc = {}
        for r in runs:
            for b, d in (r.get(key) or {}).items():
                a = acc.setdefault(b, defaultdict(float))
                for kk, vv in d.items():
                    a[kk] += float(vv or 0.0)
        out[key] = {b: {kk: _r(vv / n, 4) for kk, vv in d.items()} for b, d in acc.items()}
    if keep_detail:
        out["runs"] = runs
    return out


# --------------------------------------------------------------------------
# Calibration (recorded sweeps + the live hours' direct evidence)
# --------------------------------------------------------------------------

def _ddict():
    return defaultdict(float)


def calibrate_event(event: str, model=None) -> dict:
    """One event's sweep exposure and sweep clusters from the FULL recording
    (live orders NOT subtracted: anyone's 1c liquidity counts; the live
    hours' own trades stay in the tape). Exposure = book samples (1/s) with
    >= 1 contract at 1c on a side, keyed by the favorite's fair bucket
    (model fair, book mid when missing), minutes since open, the age of the
    1c episode and the level size. A sweep = a taker cluster of 1c prints
    (CLUSTER_S); L = the 1c level before it, cleared = it took the whole
    level, posting = a 99 bid appeared opposite within POST_WINDOW_S (the
    taker rested its unfilled rest), flash = no 1c liquidity in the sample
    before (it appeared and went inside a second)."""
    sh = shared_for(event, model, subtract=False)
    tick_fs, _ = sh.react_view(REACT_S)
    expo = {"fav_min": defaultdict(_ddict), "fav_age": defaultdict(_ddict),
            "fav_lvl": defaultdict(_ddict), "joint": defaultdict(_ddict)}
    ep_start: Dict[tuple, Optional[float]] = {}
    ep_at: Dict[tuple, List[Optional[float]]] = {}
    for m in range(sh.nm):
        for side in ("yes", "no"):
            ep_at[(m, side)] = [None] * sh.n
    for i in range(sh.n):
        T = sh.sec0 + i + 0.5
        if T >= sh.end_t:
            break
        fs = tick_fs[i]
        for m in range(sh.nm):
            info = sh.info_at_idx(m, i)
            if info is None:
                for side in ("yes", "no"):
                    ep_start[(m, side)] = None
                continue
            f = sh.st_f[fs][m] if fs >= 0 else None
            if f is None and info.mid is not None:
                f = info.mid / 100.0
            for side in ("yes", "no"):
                lvl = (info.by if side == "yes" else info.bn).get(1, 0.0)
                key = (m, side)
                if lvl < 1.0:
                    ep_start[key] = None
                    continue
                if ep_start.get(key) is None:
                    ep_start[key] = T
                ep_at[key][i] = ep_start[key]
                fp = fav_p_of(side, f)
                if fp is None:
                    continue
                fb = fav_bucket(fp)
                mb = min_bucket((T - sh.open) / 60.0)
                ab = _bucket(T - ep_start[key], AGE_EDGES, AGE_LABELS)
                expo["fav_min"][fb][mb] += 1.0
                expo["fav_age"][fb][ab] += 1.0
                expo["fav_lvl"][fb][_bucket(lvl, LVL_EDGES, LVL_LABELS)] += 1.0
                expo["joint"][fb][mb + "|" + ab] += 1.0
    lo = scoring.load_live_orders(event)
    clusters = []
    for c in sh.clusters:
        m, side, t0, t1 = c["m"], c["side"], c["t0"], c["t1"]
        if t0 >= sh.end_t:
            continue
        i0 = sh.idx_at(t0)
        i1 = sh.idx_at(t1)
        lb = [x for x in (sh.level1(m, side, i0 - 1), sh.level1(m, side, i0)) if x is not None]
        if not lb:
            continue                      # untrusted book: not scored
        L = max(lb)
        la = [x for x in (sh.level1(m, side, i1 + 1), sh.level1(m, side, i1 + 2)) if x is not None]
        cleared = c["q"] >= L - 0.5 or (bool(la) and min(la) < 1.0)
        o_before = sh.opp99(m, side, i0 - 1)
        o_after = [x for x in (sh.opp99(m, side, i1 + d) for d in range(1, POST_WINDOW_S + 1))
                   if x is not None]
        posting = (o_before is not None and bool(o_after)
                   and max(o_after) >= o_before + 1.0)
        f = sh.fair_or_mid(m, t0)
        fp = fav_p_of(side, f)
        if fp is None:
            continue
        ep = None
        for ii in (i0 - 1, i0):
            if 0 <= ii < sh.n and ep_at[(m, side)][ii] is not None:
                ep = ep_at[(m, side)][ii]
        age = (t0 - ep) if ep is not None else 0.0
        clusters.append(dict(
            t=round(t0, 3), ticker=sh.tickers[m], side=side, q=round(c["q"], 2), n=c["n"],
            L=round(L, 2), cleared=bool(cleared), posting=bool(posting), flash=L < 1.0,
            fav_p=round(fp, 5), fb=fav_bucket(fp), mb=min_bucket((t0 - sh.open) / 60.0),
            ab=_bucket(age, AGE_EDGES, AGE_LABELS),
            lb=_bucket(max(L, c["q"]), LVL_EDGES, LVL_LABELS), live_hour=bool(lo)))
    out = {"event": event, "open": sh.open, "close": sh.close,
           "live_hour": bool(lo), "book_trusted_frac": _r(sh.book_trusted_frac),
           "expo": {k: {fb: dict(d) for fb, d in v.items()} for k, v in expo.items()},
           "clusters": clusters}
    if lo:
        out["live"] = live_measure(event, sh_full=sh, lo=lo)
    return out


def live_measure(event: str, sh_full: Optional[SweepShared] = None,
                 lo: Optional[scoring.LiveOrders] = None, model=None) -> dict:
    """The live hour's direct evidence: every live order resting at 1c --
    placement to first fill, filled fraction -- and the hazard our own 1c
    orders faced: taker clusters that filled us (our 1c fills within
    CLUSTER_S on one market/side) per second we rested >= 1 contract at 1c,
    split by the favorite bucket and by whether anyone else rested at 1c
    (the recorded level minus ours)."""
    if lo is None:
        lo = scoring.load_live_orders(event)
    if not lo:
        return {}
    sh = sh_full if sh_full is not None else shared_for(event, model, subtract=False)
    own = lo.timeline(sh.sec0, sh.sec1)
    tix = {t: m for m, t in enumerate(sh.tickers)}
    expo = defaultdict(_ddict)          # fb -> {"covered"/"alone": secs}
    for i in range(min(sh.n, len(own))):
        T = sh.sec0 + i + 0.5
        if T >= sh.end_t:
            break
        for tk, (oy, on) in own[i].items():
            m = tix.get(tk)
            if m is None:
                continue
            for side, d in (("yes", oy), ("no", on)):
                q = d.get(1, 0.0)
                if q < 1.0:
                    continue
                rec = sh.level1(m, side, i)
                if rec is None:
                    continue
                cov = "covered" if rec - q >= 1.0 else "alone"
                fp = fav_p_of(side, sh.fair_or_mid(m, T))
                if fp is None:
                    continue
                expo[fav_bucket(fp)][cov] += 1.0
    # fills at 1c -> taker clusters
    f1 = sorted((float(f["ts"]), f["ticker"], str(f.get("side_ours")), float(f["count"]),
                 f.get("order_id")) for f in lo.fills if int(f.get("price_ours") or 0) == 1)
    clusters = []
    last = {}
    for ts, tk, side, q, oid in f1:
        key = (tk, side)
        c = last.get(key)
        if c is None or ts - c["t0"] > CLUSTER_S:
            m = tix.get(tk)
            i = sh.idx_at(ts)
            rec = sh.level1(m, side, i - 1) if m is not None else None
            mine = (own[i - 1].get(tk, ({}, {}))[0 if side == "yes" else 1].get(1, 0.0)
                    if 0 < i <= len(own) else 0.0)
            fp = fav_p_of(side, sh.fair_or_mid(m, ts)) if m is not None else None
            c = dict(t0=ts, ticker=tk, side=side, q=0.0,
                     fb=fav_bucket(fp) if fp is not None else None, fav_p=_r(fp, 5),
                     cov=("covered" if rec is not None and rec - mine >= 1.0 else "alone"),
                     ours_before=_r(mine, 2), mb=min_bucket((ts - sh.open) / 60.0))
            clusters.append(c)
            last[key] = c
        c["q"] += q
    # per-order life
    life = []
    fills_by = defaultdict(list)
    for f in lo.fills:
        fills_by[f.get("order_id")].append((float(f["ts"]), float(f["count"])))
    end_at = {}
    size0 = {}
    for ts, _, oid, k, r in lo.ops:
        if k in ("cancel", "gone", "expired") and oid not in end_at:
            end_at[oid] = ts
        if k in ("place", "adopt") and oid not in size0:
            try:
                size0[oid] = float(r.get("size") or 0.0)
            except (TypeError, ValueError):
                size0[oid] = 0.0
    for oid, meta in lo.orders.items():
        if meta["price"] != 1:
            continue
        fl = sorted(fills_by.get(oid, []))
        filled = sum(q for _, q in fl)
        s0 = size0.get(oid) or 0.0
        end = end_at.get(oid, sh.close)
        if fl and filled >= s0 - 1e-6:
            end = fl[-1][0]
        m = tix.get(meta["ticker"])
        fp = fav_p_of(meta["side"], sh.fair_or_mid(m, meta["placed"])) if m is not None else None
        life.append(dict(oid=oid[:13], ticker=meta["ticker"], side=meta["side"],
                         tag=meta.get("tag"), placed=round(meta["placed"], 3), size=s0,
                         filled=_r(filled, 2),
                         first_fill_s=_r(fl[0][0] - meta["placed"], 2) if fl else None,
                         life_s=_r(end - meta["placed"], 2), fav_p=_r(fp, 5),
                         fb=fav_bucket(fp) if fp is not None else None))
    return {"expo": {fb: dict(d) for fb, d in expo.items()}, "clusters": clusters,
            "orders_1c": sorted(life, key=lambda d: d["placed"])}


def _rate(n, e):
    return (n / e) if e > 0 else None


HAZ_FAVS = ("0.90-0.97", "0.97-0.99", "0.99-0.995", ">0.995")  # buckets the minute
                                     # multiplier is fit on (sweepers buy favorites)
PRIOR_N = 5.0        # pseudo-sweeps shrinking each minute multiplier toward 1


def fit_hazard(rows: List[dict], clusters: List[dict], iters: int = 60) -> dict:
    """Poisson fit of the IOC sweep hazard lambda = a[fav bucket][age of the
    1c episode] x mult[minutes since open] (iterative proportional fitting on
    the joint exposure); a is shrunk toward its favorite bucket's pool with
    PRIOR_S exposure seconds, mult toward 1 with PRIOR_N pseudo-sweeps.
    Also returns observed vs fitted counts by bucket (goodness of fit)."""
    E = defaultdict(float)
    N = defaultdict(float)
    for r in rows:
        for fb, d in (r.get("expo", {}).get("joint") or {}).items():
            for k, v in d.items():
                mb, ab = k.split("|")
                E[(fb, mb, ab)] += v
    for c in clusters:
        if not c["posting"]:
            N[(c["fb"], c["mb"], c["ab"])] += 1.0
    mult = {mb: 1.0 for mb in MIN_LABELS}
    a = {}
    for _ in range(iters):
        for fb in FAV_LABELS:
            for ab in AGE_LABELS:
                n = sum(N[(fb, mb, ab)] for mb in MIN_LABELS)
                e = sum(E[(fb, mb, ab)] * mult[mb] for mb in MIN_LABELS)
                a[(fb, ab)] = n / e if e > 0 else 0.0
        for mb in MIN_LABELS:
            n = sum(N[(fb, mb, ab)] for fb in HAZ_FAVS for ab in AGE_LABELS)
            e = sum(E[(fb, mb, ab)] * a[(fb, ab)] for fb in HAZ_FAVS for ab in AGE_LABELS)
            mult[mb] = (n + PRIOR_N) / (e + PRIOR_N) if e > 0 else 1.0
        # exposure-weighted mean multiplier = 1 (identifiability)
        tot_e = sum(E[(fb, mb, ab)] for fb in HAZ_FAVS for mb in MIN_LABELS for ab in AGE_LABELS)
        if tot_e > 0:
            wm = sum(E[(fb, mb, ab)] * mult[mb] for fb in HAZ_FAVS for mb in MIN_LABELS
                     for ab in AGE_LABELS) / tot_e
            if wm > 0:
                mult = {k: v / wm for k, v in mult.items()}
    pool, a_out, fit_rows = {}, {}, {}
    for fb in FAV_LABELS:
        n = sum(N[(fb, mb, ab)] for mb in MIN_LABELS for ab in AGE_LABELS)
        e = sum(E[(fb, mb, ab)] * mult[mb] for mb in MIN_LABELS for ab in AGE_LABELS)
        pool[fb] = n / e if e > 0 else 0.0
        a_out[fb] = {}
        for ab in AGE_LABELS:
            n_ab = sum(N[(fb, mb, ab)] for mb in MIN_LABELS)
            e_ab = sum(E[(fb, mb, ab)] * mult[mb] for mb in MIN_LABELS)
            a_out[fb][ab] = round((n_ab + pool[fb] * PRIOR_S) / (e_ab + PRIOR_S), 6)
    for mb in MIN_LABELS:
        obs = sum(N[(fb, mb, ab)] for fb in HAZ_FAVS for ab in AGE_LABELS)
        fit = sum(E[(fb, mb, ab)] * a_out[fb][ab] * mult[mb] for fb in HAZ_FAVS for ab in AGE_LABELS)
        fit_rows["min:" + mb] = [obs, round(fit, 1)]
    for ab in AGE_LABELS:
        obs = sum(N[(fb, mb, ab)] for fb in HAZ_FAVS for mb in MIN_LABELS)
        fit = sum(E[(fb, mb, ab)] * a_out[fb][ab] * mult[mb] for fb in HAZ_FAVS for mb in MIN_LABELS)
        fit_rows["age:" + ab] = [obs, round(fit, 1)]
    return {"kind": "ioc", "model": "a[fav][age] x mult[min]", "prior_s": PRIOR_S,
            "prior_n": PRIOR_N, "a": a_out, "mult": {k: round(v, 4) for k, v in mult.items()},
            "lam_pool": {k: round(v, 6) for k, v in pool.items()}, "fit": fit_rows}


def aggregate_calibration(rows: List[dict]) -> dict:
    """Pool calibrate_event rows into the report + the hazard table."""
    rows = [r for r in rows if r and not r.get("error")]

    def agg(sel):
        ex = defaultdict(_ddict)
        exa = defaultdict(_ddict)
        exl = defaultdict(_ddict)
        cl = []
        for r in rows:
            if not sel(r):
                continue
            for fb, d in r["expo"]["fav_min"].items():
                for mb, v in d.items():
                    ex[fb][mb] += v
            for fb, d in r["expo"]["fav_age"].items():
                for k, v in d.items():
                    exa[fb][k] += v
            for fb, d in r["expo"]["fav_lvl"].items():
                for k, v in d.items():
                    exl[fb][k] += v
            cl.extend(r["clusters"])
        by_fav = {}
        for fb in FAV_LABELS:
            e = sum(ex[fb].values())
            cs = [c for c in cl if c["fb"] == fb]
            ioc = [c for c in cs if not c["posting"]]
            qs = sorted(c["q"] for c in cs)
            by_fav[fb] = dict(
                expo_s=e, sweeps=len(cs), ioc=len(ioc), posting=len(cs) - len(ioc),
                flash=sum(1 for c in cs if c["flash"]),
                lam_all=_r(_rate(len(cs), e), 6), lam_ioc=_r(_rate(len(ioc), e), 6),
                mean_wait_s=_r(e / len(cs), 1) if cs else None,
                cleared_frac=_r(sum(1 for c in cs if c["cleared"]) / len(cs), 3) if cs else None,
                q_median=qs[len(qs) // 2] if qs else None,
                q_p90=qs[int(0.9 * (len(qs) - 1))] if qs else None,
                contracts=_r(sum(qs), 1))

        def grid(labels, exd, key):
            return {fb: {lab: dict(expo_s=exd[fb].get(lab, 0.0),
                                   sweeps=sum(1 for c in cl if c["fb"] == fb and c[key] == lab),
                                   ioc=sum(1 for c in cl if c["fb"] == fb and c[key] == lab
                                           and not c["posting"]))
                         for lab in labels} for fb in FAV_LABELS}
        by_fav_min = grid(MIN_LABELS, ex, "mb")
        by_fav_age = grid(AGE_LABELS, exa, "ab")
        by_fav_lvl = grid(LVL_LABELS, exl, "lb")
        for d in (by_fav_min, by_fav_age, by_fav_lvl):
            for fb, dd in d.items():
                for k, x in dd.items():
                    x["lam"] = _r(_rate(x["sweeps"], x["expo_s"]), 6)
                    x["lam_ioc"] = _r(_rate(x["ioc"], x["expo_s"]), 6)
        return dict(by_fav=by_fav, by_fav_min=by_fav_min, by_fav_age=by_fav_age,
                    by_fav_lvl=by_fav_lvl, clusters=cl)

    allr = agg(lambda r: True)
    pre = agg(lambda r: not r["live_hour"])
    liveh = agg(lambda r: r["live_hour"])
    # hazard: IOC sweeps only (posting ones are replayed from the tape's 99
    # bids, channel (c)); lambda = a[fav bucket][age] x mult[minutes]
    hazard = fit_hazard(rows, allr["clusters"])
    sizes = {}
    for ab in AGE_LABELS:
        sizes[ab] = [[c["q"], int(c["cleared"])] for c in allr["clusters"]
                     if c["ab"] == ab and not c["posting"] and c["fb"] in HAZ_FAVS[1:]]
    sizes_all = [[c["q"], int(c["cleared"])] for c in allr["clusters"]
                 if not c["posting"] and c["fb"] in HAZ_FAVS[1:]]
    # live direct evidence
    lv_rows = [r["live"] for r in rows if r.get("live")]
    lexpo = defaultdict(_ddict)
    lcl = []
    lorders = []
    for lv in lv_rows:
        for fb, d in lv["expo"].items():
            for k, v in d.items():
                lexpo[fb][k] += v
        lcl.extend(lv["clusters"])
        lorders.extend(lv["orders_1c"])
    live_by_fav = {}
    for fb in FAV_LABELS:
        e = lexpo[fb]
        n_alone = sum(1 for c in lcl if c["fb"] == fb and c["cov"] == "alone")
        n_cov = sum(1 for c in lcl if c["fb"] == fb and c["cov"] == "covered")
        live_by_fav[fb] = dict(expo_alone_s=e.get("alone", 0.0),
                               expo_covered_s=e.get("covered", 0.0),
                               hits_alone=n_alone, hits_covered=n_cov,
                               lam_alone=_r(_rate(n_alone, e.get("alone", 0.0)), 6),
                               lam_covered=_r(_rate(n_cov, e.get("covered", 0.0)), 6),
                               contracts=_r(sum(c["q"] for c in lcl if c["fb"] == fb), 1))
    ff = sorted(o["first_fill_s"] for o in lorders if o["first_fill_s"] is not None)
    live = dict(events=[r["event"] for r in rows if r.get("live")], by_fav=live_by_fav,
                orders_1c=len(lorders), orders_1c_filled=sum(1 for o in lorders if o["filled"]),
                filled_frac=_r(sum(o["filled"] or 0 for o in lorders) /
                               max(1e-9, sum(o["size"] or 0 for o in lorders)), 3),
                first_fill_s_median=ff[len(ff) // 2] if ff else None,
                first_fill_s=ff, orders=lorders, clusters=lcl)
    return {"version": 1, "sweep_version": SWEEP_VERSION, "built": C.iso(time.time()),
            "events": [r["event"] for r in rows],
            "hazard": hazard, "sizes": sizes, "sizes_all": sizes_all,
            "recorded": {"all": {k: v for k, v in allr.items() if k != "clusters"},
                         "pre_live": {k: v for k, v in pre.items() if k != "clusters"},
                         "live_hours": {k: v for k, v in liveh.items() if k != "clusters"}},
            "clusters": allr["clusters"], "live": live}


def recorded_events(days: Optional[Sequence[str]] = None, settled_only: bool = True) -> List[str]:
    """Settled events whose hour the collector recorded (ws rows exist)."""
    if not days:
        rd = os.path.join(C.DATA_ROOT, "raw")
        ets = set()
        for d in (sorted(os.listdir(rd)) if os.path.isdir(rd) else []):
            t = C.parse_iso(d + "T00:00:00Z")
            if t is None:
                continue
            ets.add(C.et_day(t))
            ets.add(C.et_day(t + 86399))
        days = sorted(ets)
    evs = []
    for d in days:
        for ev in sim.day_events(d):
            S = C.parse_event(ev)[1]
            if not C.raw_files("ws", S - 3600, S):
                continue
            if settled_only:
                try:
                    if not sim.load_event_inputs(ev).settled:
                        continue
                except Exception:
                    continue
            evs.append(ev)
    return sorted(set(evs))


# --------------------------------------------------------------------------
# Live actuals (reward estimator + fills) for validation
# --------------------------------------------------------------------------

def live_actuals(event: str, model=None, sh_full: Optional[SweepShared] = None) -> dict:
    """What the live executor actually did on an event: the reward estimate
    (the shadow's scoring over our reconstructed live orders against the
    UNSUBTRACTED recorded book: ours are taken out and overlaid back, as
    temp_shadow_sim overlays its own), fills, positions and settled trading
    P&L. A cleaned version of scratchpad tmm_reward_est.py (same scoring;
    programs from temp_shadow_sim.load_event_inputs, books sampled like the
    sim's ticks)."""
    lo = scoring.load_live_orders(event)
    if not lo:
        return {}
    sh = sh_full if sh_full is not None else shared_for(event, model, subtract=False)
    own = lo.timeline(sh.sec0, sh.sec1)
    reward = 0.0
    per = defaultdict(float)
    per_min = defaultdict(float)
    shares = []
    for m, tk in enumerate(sh.tickers):
        pr = sh.progs[m]
        cache = (None, None, 0.0)
        for i in range(min(sh.n, len(own))):
            T = sh.sec0 + i + 0.5
            if T >= sh.end_t:
                break
            if not (pr["start"] <= T < pr["end"]):
                continue
            o = own[i].get(tk)
            if not o:
                continue
            info = sh.info_at_idx(m, i)
            if info is None:
                continue
            if cache[0] is info and cache[1] is o:
                frac = cache[2]
            else:
                ey = scoring.subtract_levels(info.by, o[0])
                en = scoring.subtract_levels(info.bn, o[1])
                frac = float(scoring.score(ey, en, o[0], o[1], pr["target"], pr["df"])[0])
                cache = (info, o, frac)
            shares.append(frac)
            r = pr["pool"] * frac
            reward += r
            per[tk] += r
            per_min[int((T - sh.open) // 300) * 5] += r
    yes_q = defaultdict(float)
    no_q = defaultdict(float)
    cost = defaultdict(float)
    f1 = defaultdict(float)
    f1_t = defaultdict(list)
    for f in lo.fills:
        tk = f["ticker"]
        q = float(f["count"])
        p = int(f.get("price_ours") or 0)
        if str(f.get("side_ours")) == "yes":
            yes_q[tk] += q
        else:
            no_q[tk] += q
        cost[tk] += q * p / 100.0
        if p == 1:
            f1[tk] += q
            f1_t[tk].append((round(float(f["ts"]) - sh.open, 1), round(q, 2)))
    pnl = 0.0
    per_pnl = {}
    for tk in set(yes_q) | set(no_q):
        m = sh.mindex.get(tk)
        py = sh.payoff_yes(m) if m is not None else 0.5
        v = yes_q[tk] * py + no_q[tk] * (1.0 - py) - cost[tk]
        per_pnl[tk] = round(v, 4)
        pnl += v
    return {"event": event, "settled": bool(sh.inp.settled), "reward": _r(reward, 4),
            "reward_by_strike": {t: _r(v, 3) for t, v in sorted(per.items())},
            "reward_by_5min": {k: _r(v, 3) for k, v in sorted(per_min.items())},
            "avg_share": _r(sum(shares) / len(shares), 4) if shares else 0.0,
            "quoted_secs": len(shares),
            "contracts": _r(sum(yes_q.values()) + sum(no_q.values()), 2),
            "filler_contracts": _r(sum(f1.values()), 2),
            "filler_by_strike": {t: _r(v, 2) for t, v in sorted(f1.items())},
            "filler_times": {t: v for t, v in sorted(f1_t.items())},
            "pos": {t: _r(yes_q[t] - no_q[t], 2) for t in sorted(set(yes_q) | set(no_q))},
            "trading_pnl": _r(pnl, 4), "pnl_by_strike": per_pnl,
            "net": _r(reward + pnl, 4), "results": dict(sh.inp.results)}


# --------------------------------------------------------------------------
# Variant runs (opt / cons / swp) per event and per day
# --------------------------------------------------------------------------

def _event_meta(sh: SweepShared) -> dict:
    S = float(sh.inp.settle_ts)
    cov = sh.stream_cov or {}
    return {"event": sh.inp.event, "open_ts": sh.open, "close_ts": sh.close, "settle_ts": S,
            "settled": bool(sh.inp.settled), "et_day": C.et_day(S), "hour_et": C.et_dt(S).hour,
            "book_trusted_frac": _r(sh.book_trusted_frac), "trades": len(sh.trades),
            "sweep_clusters": len(sh.clusters), "live_own": cov.get("live_own"),
            "flags": list(sh.inp.flags) + list(sh.flags)}


def run_event_variants(event: str, variants: Optional[Dict[str, dict]] = None,
                       draws: int = DEFAULT_DRAWS, hazard: Optional[Hazard] = None,
                       model=None, fill_models: Sequence[str] = FILL_MODELS,
                       rec_mode: str = "level", channels: str = "abc",
                       seed: str = SEED_BASE) -> dict:
    """Every variant x fill model on one event (live-order subtraction on).
    swp = the mean of `draws` Monte Carlo passes; draws are the outer loop,
    so every variant sees the same hazard arrivals (common random numbers)."""
    t_start = time.time()
    variants = variants or dict(SWEEP_VARIANTS)
    sh = shared_for(event, model, subtract=True)
    out = _event_meta(sh)
    out["variants"] = {}
    out["policies"] = {n: v for n, v in variants.items()}
    specs = {n: resolve_variant(v) for n, v in variants.items()}
    for name, (P, hold) in specs.items():
        for fm in fill_models:
            if fm != "swp":
                out["variants"][f"{name}|{fm}"] = VPass(sh, P, fm, hold_pads_s=hold).run()
    if "swp" in fill_models:
        if hazard is None:
            hazard = load_hazard()
        runs = {n: [] for n in specs}
        for k in range(int(draws)):
            cands = hazard_candidates(sh, k, hazard.lam_max, seed) if "b" in channels else None
            for name, (P, hold) in specs.items():
                runs[name].append(VPass(sh, P, "swp", hold_pads_s=hold, hazard=hazard,
                                        cands=cands, rec_mode=rec_mode,
                                        channels=channels).run())
        for name in specs:
            out["variants"][f"{name}|swp"] = mean_summary(runs[name])
    out["swp"] = {"draws": int(draws), "rec_mode": rec_mode, "channels": channels,
                  "seed": seed, "lam_max": _r(hazard.lam_max, 6) if hazard else None,
                  "hazard_src": (hazard.doc.get("built") if hazard else None)}
    out["sweep_version"] = SWEEP_VERSION
    out["sim_version"] = sim.SIM_VERSION
    out["runtime_s"] = round(time.time() - t_start, 2)
    return out


def _worker_init():
    sim._lower_priority()


def _event_job(args) -> dict:
    ev, variants, draws, hazard_doc, rec_mode, channels, path = args
    try:
        hz = Hazard(hazard_doc)
        res = run_event_variants(ev, variants, draws, hz, rec_mode=rec_mode, channels=channels)
        if path:
            C.write_json_atomic(path, res)
        return res
    except Exception as e:
        return {"event": ev, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:]}


def run_events(events: Sequence[str], out: str, variants=None, draws: int = DEFAULT_DRAWS,
               hazard: Optional[Hazard] = None, workers: int = 2, rec_mode: str = "level",
               channels: str = "abc", force: bool = False) -> List[dict]:
    """run_event_variants over events (settled ones), written to
    <out>/runs/<ET day>/<EVENT>.json; existing files are reused unless force
    (or their draws / variants / rec_mode differ)."""
    variants = variants or dict(SWEEP_VARIANTS)
    hazard = hazard or load_hazard()
    want = json.loads(json.dumps(variants))
    todo, results = [], []
    for ev in events:
        S = C.parse_event(ev)[1]
        path = os.path.join(out, "runs", C.et_day(S), f"{ev}.json")
        if not force and os.path.exists(path):
            doc = C.read_json(path)
            if (isinstance(doc, dict) and not doc.get("error")
                    and (doc.get("swp") or {}).get("draws") == int(draws)
                    and (doc.get("swp") or {}).get("rec_mode") == rec_mode
                    and (doc.get("swp") or {}).get("channels") == channels
                    and doc.get("policies") == want and doc.get("sweep_version") == SWEEP_VERSION):
                results.append(doc)
                continue
        todo.append((ev, variants, int(draws), hazard.doc, rec_mode, channels, path))
    w = max(1, min(MAX_WORKERS, int(workers), len(todo) or 1))
    if w == 1:
        for a in todo:
            results.append(_event_job(a))
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=w, initializer=_worker_init) as ex:
            for res in ex.map(_event_job, todo):
                results.append(res)
                print(f"  {res.get('event')}: {res.get('error') or str(res.get('runtime_s')) + ' s'}",
                      flush=True)
    results.sort(key=lambda d: d.get("event", ""))
    return results


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

_TAB_KEYS = ("reward", "contracts", "trading_pnl", "net", "pnl_fair", "net_fair")


def variant_table(results: List[dict]) -> dict:
    """{variant|fm: {"days": {day: {reward, contracts, trading_pnl, net, events}},
    "total": {...}, "net_se": standard error of the swp total over draws}}
    plus by_hour[variant|fm][hour_et] = {...}."""
    tab: Dict[str, dict] = {}
    by_hour: Dict[str, dict] = {}
    for r in results:
        if r.get("error") or not r.get("settled"):
            continue
        day, hr = r["et_day"], r["hour_et"]
        for key, v in (r.get("variants") or {}).items():
            t = tab.setdefault(key, {"days": {}, "total": defaultdict(float), "var": 0.0})
            d = t["days"].setdefault(day, defaultdict(float))
            h = by_hour.setdefault(key, {}).setdefault(hr, defaultdict(float))
            for k in _TAB_KEYS:
                x = float(v.get(k) or 0.0)
                d[k] += x
                t["total"][k] += x
                h[k] += x
            d["events"] += 1
            t["total"]["events"] += 1
            h["events"] += 1
            if v.get("draws"):
                t["var"] += float(v.get("net_sd") or 0.0) ** 2 / float(v["draws"])
    out = {}
    for key, t in tab.items():
        out[key] = {"days": {d: {k: round(x, 2) for k, x in dd.items()} for d, dd in sorted(t["days"].items())},
                    "total": {k: round(x, 2) for k, x in t["total"].items()},
                    "net_se": round(math.sqrt(t["var"]), 2)}
    hours = {key: {h: {k: round(x, 2) for k, x in dd.items()} for h, dd in sorted(hh.items())}
             for key, hh in by_hour.items()}
    return {"table": out, "by_hour": hours}


def format_table(tab: dict, variants: Sequence[str]) -> str:
    t = tab["table"]
    days = sorted({d for v in t.values() for d in v["days"]})
    lines = []
    hdr = f"{'variant':12s} {'fill':4s} " + " ".join(f"{d[5:]:>16s}" for d in days) + \
        f" {'reward':>9s} {'ctr':>9s} {'pnl':>9s} {'NET':>9s} {'se':>5s} {'ev':>3s}"
    lines.append(hdr)
    lines.append("            (per-day cells: net $; totals over all days)")
    for name in variants:
        for fm in FILL_MODELS:
            v = t.get(f"{name}|{fm}")
            if not v:
                continue
            cells = " ".join(f"{v['days'].get(d, {}).get('net', 0.0):16.2f}" for d in days)
            tt = v["total"]
            se = f"{v['net_se']:5.1f}" if fm == "swp" else "     "
            lines.append(f"{name:12s} {fm:4s} {cells} {tt.get('reward', 0):9.2f} "
                         f"{tt.get('contracts', 0):9.0f} {tt.get('trading_pnl', 0):9.2f} "
                         f"{tt.get('net', 0):9.2f} {se} {int(tt.get('events', 0)):3d}")
    return "\n".join(lines)


def format_day_detail(tab: dict, variants: Sequence[str]) -> str:
    t = tab["table"]
    days = sorted({d for v in t.values() for d in v["days"]})
    lines = [f"{'variant|fill':18s} {'day':10s} {'ev':>3s} {'reward':>9s} {'ctr':>9s} "
             f"{'pnl':>9s} {'net':>9s}"]
    for name in variants:
        for fm in FILL_MODELS:
            v = t.get(f"{name}|{fm}")
            if not v:
                continue
            for d in days:
                dd = v["days"].get(d)
                if dd:
                    lines.append(f"{name + '|' + fm:18s} {d:10s} {int(dd.get('events', 0)):3d} "
                                 f"{dd.get('reward', 0):9.2f} {dd.get('contracts', 0):9.0f} "
                                 f"{dd.get('trading_pnl', 0):9.2f} {dd.get('net', 0):9.2f}")
    return "\n".join(lines)


def format_hours(tab: dict, key: str) -> str:
    hh = tab["by_hour"].get(key) or {}
    lines = [f"by ET settle hour, {key}:",
             f"{'hour':>4s} {'ev':>3s} {'reward':>9s} {'ctr':>9s} {'pnl':>9s} {'net':>9s} {'net/ev':>8s}"]
    for h in sorted(hh, key=int):
        d = hh[h]
        n = d.get("events", 0) or 1
        lines.append(f"{int(h):4d} {int(d.get('events', 0)):3d} {d.get('reward', 0):9.2f} "
                     f"{d.get('contracts', 0):9.0f} {d.get('trading_pnl', 0):9.2f} "
                     f"{d.get('net', 0):9.2f} {d.get('net', 0) / n:8.2f}")
    return "\n".join(lines)


def load_runs(out: str, days: Optional[Sequence[str]] = None) -> List[dict]:
    base = os.path.join(out, "runs")
    res = []
    if not os.path.isdir(base):
        return res
    for d in sorted(os.listdir(base)):
        if days and d not in days:
            continue
        for fn in sorted(os.listdir(os.path.join(base, d))):
            if fn.endswith(".json"):
                doc = C.read_json(os.path.join(base, d, fn))
                if isinstance(doc, dict):
                    res.append(doc)
    return res


# --------------------------------------------------------------------------
# Validation against a live hour
# --------------------------------------------------------------------------

def _pct(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def _first_sweep_times(fills_detail: List[tuple], open_ts: float) -> Dict[str, float]:
    out = {}
    for ts, tk, side, p, q in fills_detail:
        if p == 1 and tk not in out:
            out[tk] = round(ts - open_ts, 1)
    return out


def _cum_by_5min(fills_detail: List[tuple], open_ts: float) -> Dict[int, float]:
    acc = defaultdict(float)
    for ts, tk, side, p, q in fills_detail:
        if p == 1:
            acc[int((ts - open_ts) // 300) * 5] += q
    out, run = {}, 0.0
    for k in range(0, 60, 5):
        run += acc.get(k, 0.0)
        out[k] = round(run, 1)
    return out


def validate_event(event: str, draws: int = DEFAULT_DRAWS, hazard: Optional[Hazard] = None,
                   model=None, variant: Optional[dict] = None) -> dict:
    """Predicted (variant G under opt / cons / swp, subtraction on) vs the live
    executor's actual on one live hour: filler contracts swept per strike and
    when, positions, settled trading P&L and the reward estimate. swp is
    reported for rec_mode size (default) and all, and channel by channel."""
    hazard = hazard or load_hazard()
    if model is None:
        model = tmodel.FairModel()
    spec = variant or SWEEP_VARIANTS["G"]
    P, hold = resolve_variant(spec)
    full = shared_for(event, model, subtract=False)
    actual = live_actuals(event, sh_full=full)
    if not actual:
        raise ValueError(f"{event}: no live orders logged")
    meas = live_measure(event, sh_full=full)
    sh = shared_for(event, model, subtract=True)
    pred = {}
    for fm in ("opt", "cons"):
        r = VPass(sh, P, fm, hold_pads_s=hold, detail=True).run()
        pred[fm] = r
    configs = {"swp": ("level", "abc", True), "swp_size": ("size", "abc", True),
               "swp_all": ("all", "abc", True), "swp_strict": ("level", "abc", False),
               "swp_a": ("level", "a", True), "swp_ac": ("level", "ac", True),
               "swp_ab": ("level", "ab", True)}
    cands_k = [hazard_candidates(sh, k, hazard.lam_max) for k in range(int(draws))]
    for key, (rm, ch, own_arr) in configs.items():
        n = int(draws) if "b" in ch else 1
        runs = [VPass(sh, P, "swp", hold_pads_s=hold, hazard=hazard,
                      cands=cands_k[k] if "b" in ch else None, rec_mode=rm, channels=ch,
                      detail=True, own_arrivals=own_arr).run() for k in range(n)]
        ms = mean_summary(runs)
        ms["first_sweep_s"] = {}
        fst = defaultdict(list)
        cum = defaultdict(list)
        for r in runs:
            for tk, t in _first_sweep_times(r["fills_detail"], sh.open).items():
                fst[tk].append(t)
            for k5, v in _cum_by_5min(r["fills_detail"], sh.open).items():
                cum[k5].append(v)
        ms["first_sweep_s"] = {tk: {"median": _pct(v, 0.5), "p10": _pct(v, 0.1),
                                    "p90": _pct(v, 0.9), "frac_runs": round(len(v) / n, 3)}
                               for tk, v in sorted(fst.items())}
        ms["cum_filler_by_5min"] = {k5: round(sum(v) / n, 1) for k5, v in sorted(cum.items())}
        ms["filler_p10_p90"] = [_pct([float(r.get("filler_contracts") or 0) for r in runs], 0.1),
                                _pct([float(r.get("filler_contracts") or 0) for r in runs], 0.9)]
        ms["net_p10_p90"] = [_pct([float(r.get("net") or 0) for r in runs], 0.1),
                             _pct([float(r.get("net") or 0) for r in runs], 0.9)]
        pred[key] = ms
    for fm in ("opt", "cons"):
        r = pred[fm]
        r["first_sweep_s"] = _first_sweep_times(r.get("fills_detail") or [], sh.open)
        r["cum_filler_by_5min"] = _cum_by_5min(r.get("fills_detail") or [], sh.open)
        r.pop("fills_detail", None)
        r.pop("sweep_detail", None)
    act_cum = defaultdict(float)
    for tk, lst in (actual.get("filler_times") or {}).items():
        for t, q in lst:
            act_cum[int(t // 300) * 5] += q
    run, ac = 0.0, {}
    for k5 in range(0, 60, 5):
        run += act_cum.get(k5, 0.0)
        ac[k5] = round(run, 1)
    actual["cum_filler_by_5min"] = ac
    actual["first_sweep_s"] = {tk: lst[0][0] for tk, lst in (actual.get("filler_times") or {}).items()}
    return {"event": event, "draws": int(draws), "variant": spec, "settled": bool(sh.inp.settled),
            "results": dict(sh.inp.results), "actual": actual, "predicted": pred,
            "live_measure": {k: v for k, v in meas.items() if k != "orders_1c"},
            "live_orders_1c": meas.get("orders_1c"), "coverage_live_own": (sh.stream_cov or {}).get("live_own"),
            "hazard_built": hazard.doc.get("built"), "sweep_version": SWEEP_VERSION}


def format_validation(v: dict) -> str:
    a, p = v["actual"], v["predicted"]
    keys = ["opt", "cons", "swp", "swp_size", "swp_all", "swp_strict", "swp_a", "swp_ac", "swp_ab"]
    lines = [f"validation {v['event']} (variant G, {v['draws']} swp draws, settled={v['settled']})",
             f"{'':18s} {'actual':>10s} " + " ".join(f"{k:>9s}" for k in keys)]

    def row(label, av, getter, fmt="{:9.1f}"):
        cells = []
        for k in keys:
            x = getter(p[k])
            cells.append(fmt.format(x) if x is not None else f"{'-':>9s}")
        avs = fmt.format(av) if av is not None else "-"
        lines.append(f"{label:18s} {avs:>10s} " + " ".join(cells))
    row("filler contracts", a["filler_contracts"], lambda r: r.get("filler_contracts"))
    row("all contracts", a["contracts"], lambda r: r.get("contracts"))
    row("reward $", a["reward"], lambda r: r.get("reward"), "{:9.2f}")
    row("trading P&L $", a["trading_pnl"], lambda r: r.get("trading_pnl"), "{:9.2f}")
    row("net $", a["net"], lambda r: r.get("net"), "{:9.2f}")
    row("avg share", a["avg_share"], lambda r: r.get("avg_share"), "{:9.3f}")
    lines.append("")
    lines.append("cumulative filler contracts by minute (actual | opt cons swp swp_size swp_all "
                 "swp_strict):")
    for k5 in range(0, 60, 5):
        lines.append(f"  {k5:2d}-{k5 + 5:2d}m  {a['cum_filler_by_5min'].get(k5, 0):8.0f} | "
                     + " ".join(f"{(p[k].get('cum_filler_by_5min') or {}).get(k5, 0):8.0f}"
                                for k in ("opt", "cons", "swp", "swp_size", "swp_all", "swp_strict")))
    lines.append("")
    tks = sorted(set(a.get("pos", {})) | set(p["swp"].get("pos", {})) | set(a.get("filler_by_strike", {}))
                 | set(p["swp"].get("filler_by_strike", {})), key=lambda t: C.strike_of(t) or 0)
    lines.append(f"{'strike':8s} {'fill act':>9s} {'fill swp':>9s} {'fill size':>9s} {'fill all':>9s} "
                 f"{'fill cons':>9s} {'pos act':>9s} {'pos swp':>9s} {'1st act s':>9s} "
                 f"{'1st swp s':>9s} {'swp hit%':>8s}")
    for tk in tks:
        fs = (p["swp"].get("first_sweep_s") or {}).get(tk) or {}
        lines.append(f"{tk.rsplit('-', 1)[-1]:8s} {a.get('filler_by_strike', {}).get(tk, 0):9.0f} "
                     f"{p['swp'].get('filler_by_strike', {}).get(tk, 0):9.0f} "
                     f"{p['swp_size'].get('filler_by_strike', {}).get(tk, 0):9.0f} "
                     f"{p['swp_all'].get('filler_by_strike', {}).get(tk, 0):9.0f} "
                     f"{p['cons'].get('filler_by_strike', {}).get(tk, 0):9.0f} "
                     f"{a.get('pos', {}).get(tk, 0):9.0f} {p['swp'].get('pos', {}).get(tk, 0):9.0f} "
                     f"{a.get('first_sweep_s', {}).get(tk, float('nan')):9.1f} "
                     f"{(fs.get('median') if fs.get('median') is not None else float('nan')):9.1f} "
                     f"{100 * (fs.get('frac_runs') or 0):8.0f}")
    lines.append("")
    src = p["swp"].get("sweep_src") or {}
    lines.append("swp filler contracts by channel (mean): " +
                 ", ".join(f"{k} {v:.0f}" for k, v in sorted(src.items())))
    lines.append(f"swp net p10/p90: {p['swp'].get('net_p10_p90')}  filler p10/p90: "
                 f"{p['swp'].get('filler_p10_p90')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Calibration report
# --------------------------------------------------------------------------

def format_calibration(agg: dict) -> str:
    L = []
    rec = agg["recorded"]
    for name in ("all", "pre_live", "live_hours"):
        bf = rec[name]["by_fav"]
        L.append(f"recorded sweeps [{name}] by favorite fair (exposure = seconds with >= 1 "
                 f"contract at 1c on that side):")
        L.append(f"  {'fav':>10s} {'expo_h':>7s} {'sweeps':>6s} {'ioc':>5s} {'post':>4s} "
                 f"{'flash':>5s} {'lam/s':>8s} {'wait_s':>7s} {'clear%':>6s} {'q_med':>6s} {'q_p90':>6s}")
        for fb in FAV_LABELS:
            d = bf[fb]
            L.append(f"  {fb:>10s} {d['expo_s'] / 3600:7.1f} {d['sweeps']:6d} {d['ioc']:5d} "
                     f"{d['posting']:4d} {d['flash']:5d} {(d['lam_all'] or 0):8.5f} "
                     f"{(d['mean_wait_s'] or 0):7.0f} {100 * (d['cleared_frac'] or 0):6.0f} "
                     f"{(d['q_median'] or 0):6.0f} {(d['q_p90'] or 0):6.0f}")
    a = rec["all"]
    for title, key, labels in (("age of the 1c episode", "by_fav_age", AGE_LABELS),
                               ("minutes since open", "by_fav_min", MIN_LABELS),
                               ("1c level size", "by_fav_lvl", LVL_LABELS)):
        L.append(f"hazard lam (sweeps/s) by {title} [all recorded hours]:")
        L.append(f"  {'fav':>10s} " + " ".join(f"{lab:>10s}" for lab in labels))
        for fb in ("0.90-0.97", "0.97-0.99", "0.99-0.995", ">0.995"):
            L.append(f"  {fb:>10s} " + " ".join(
                f"{(a[key][fb][lab]['lam'] or 0):10.4f}" for lab in labels))
            L.append(f"  {'(n/expo s)':>10s} " + " ".join(
                f"{a[key][fb][lab]['sweeps']}/{int(a[key][fb][lab]['expo_s'])}".rjust(10)
                for lab in labels))
    hz = agg["hazard"]
    L.append("fitted IOC hazard a[fav][age] (per s) x mult[minute]:")
    for fb in FAV_LABELS:
        L.append(f"  {fb:>10s} " + " ".join(f"{ab}={hz['a'][fb][ab]:.4f}" for ab in AGE_LABELS))
    L.append("  mult: " + " ".join(f"{mb}={x:.2f}" for mb, x in hz["mult"].items()))
    L.append("  fit (observed, fitted): " + ", ".join(f"{k} {v[0]:.0f}/{v[1]:.0f}"
                                                      for k, v in hz["fit"].items()))
    lv = agg.get("live") or {}
    if lv.get("events"):
        L.append(f"LIVE ({', '.join(lv['events'])}): our 1c orders {lv['orders_1c']}, "
                 f"{lv['orders_1c_filled']} filled, filled fraction {lv['filled_frac']}, "
                 f"placement->first fill median {lv['first_fill_s_median']} s, all: {lv['first_fill_s']}")
        L.append(f"  {'fav':>10s} {'alone_s':>8s} {'hits':>4s} {'lam':>8s} {'cov_s':>7s} {'hits':>4s} "
                 f"{'lam':>8s} {'ctr':>7s}")
        for fb in FAV_LABELS:
            d = lv["by_fav"][fb]
            if not (d["expo_alone_s"] or d["expo_covered_s"] or d["contracts"]):
                continue
            L.append(f"  {fb:>10s} {d['expo_alone_s']:8.0f} {d['hits_alone']:4d} "
                     f"{(d['lam_alone'] or 0):8.5f} {d['expo_covered_s']:7.0f} {d['hits_covered']:4d} "
                     f"{(d['lam_covered'] or 0):8.5f} {d['contracts']:7.0f}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cal_job(ev):
    try:
        return calibrate_event(ev)
    except Exception as e:
        return {"event": ev, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:]}


def cmd_calibrate(a) -> int:
    evs = a.events or recorded_events(a.days)
    print(f"calibrating on {len(evs)} events", flush=True)
    w = max(1, min(MAX_WORKERS, a.workers))
    if w == 1:
        rows = [_cal_job(ev) for ev in evs]
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=w, initializer=_worker_init) as ex:
            rows = list(ex.map(_cal_job, evs))
    for r in rows:
        if r.get("error"):
            print(f"  {r['event']}: ERROR {r['error']}")
    agg = aggregate_calibration(rows)
    os.makedirs(a.out, exist_ok=True)
    C.write_json_atomic(os.path.join(a.out, "hazard.json"), agg)
    txt = format_calibration(agg)
    with open(os.path.join(a.out, "calibration.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return 0


def cmd_run(a) -> int:
    hz = load_hazard(a.hazard, a.hazard_scale)
    variants = pick_variants(a.variants)
    evs = []
    for d in a.day:
        evs.extend(e for e in sim.day_events(d) if C.raw_files("ws", C.parse_event(e)[1] - 3600,
                                                                 C.parse_event(e)[1]))
    evs = [e for e in evs if _settled(e)]
    print(f"{len(evs)} settled recorded events, {len(variants)} variants, {a.draws} swp draws",
          flush=True)
    res = run_events(evs, a.out, variants, a.draws, hz, a.workers, a.rec_mode, a.channels, a.force)
    tab = variant_table(res)
    txt = format_table(tab, list(variants))
    print(txt)
    return 1 if any(r.get("error") for r in res) else 0


def _settled(ev: str) -> bool:
    try:
        return sim.load_event_inputs(ev).settled
    except Exception:
        return False


def cmd_table(a) -> int:
    res = load_runs(a.out, a.day)
    names = [n for n in SWEEP_VARIANTS if any(f"{n}|" in k for r in res for k in r.get("variants", {}))]
    tab = variant_table(res)
    txt = [format_table(tab, names), "", format_day_detail(tab, names)]
    best = None
    for n in names:
        v = tab["table"].get(f"{n}|swp")
        if v and (best is None or v["total"]["net"] > best[1]):
            best = (n, v["total"]["net"])
    if best:
        txt += ["", f"best swp variant: {best[0]} (net {best[1]:+.2f})",
                format_hours(tab, f"{best[0]}|swp")]
    out = "\n".join(txt)
    print(out)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "variant_table.txt"), "w", encoding="utf-8") as f:
        f.write(out + "\n")
    C.write_json_atomic(os.path.join(a.out, "variant_table.json"), tab)
    return 0


def cmd_validate(a) -> int:
    hz = load_hazard(a.hazard, a.hazard_scale)
    v = validate_event(a.event, a.draws, hz)
    os.makedirs(a.out, exist_ok=True)
    C.write_json_atomic(os.path.join(a.out, f"validate_{a.event}.json"), v)
    txt = format_validation(v)
    with open(os.path.join(a.out, f"validate_{a.event}.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="sweep-aware hourly-temp shadow")
    ap.add_argument("--out", default=None, help="output dir (default env TEMP_SHADOW_SWEEP_OUT "
                                                "or <TEMP_SHADOW_DATA>/sweep)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--events", nargs="*", default=None)
    c.add_argument("--days", nargs="*", default=None)
    c.add_argument("--workers", type=int, default=2)
    for name in ("run", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
        p.add_argument("--hazard", default=None, help="hazard.json (default <out>/hazard.json)")
        p.add_argument("--hazard-scale", type=float, default=1.0)
        if name == "run":
            p.add_argument("--day", nargs="+", required=True)
            p.add_argument("--variants", default=None)
            p.add_argument("--workers", type=int, default=2)
            p.add_argument("--rec-mode", default="level", choices=REC_MODES)
            p.add_argument("--channels", default="abc")
            p.add_argument("--force", action="store_true")
        else:
            p.add_argument("--event", required=True)
    t = sub.add_parser("table")
    t.add_argument("--day", nargs="*", default=None)
    a = ap.parse_args(argv)
    a.out = a.out or out_dir_default()
    if a.cmd in ("run", "validate") and a.hazard is None:
        hp = os.path.join(a.out, "hazard.json")
        a.hazard = hp if os.path.exists(hp) else None
    return {"calibrate": cmd_calibrate, "run": cmd_run, "validate": cmd_validate,
            "table": cmd_table}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
