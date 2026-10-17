#!/usr/bin/env python3
"""Order-level audit of the hourly-temp shadow's fill models against a live
hour, and the 2026-10-17 stress analyses (KXTEMPMIAH).

Jack 2026-10-17: "Make sure you don't make same mistake where the shadow
trading results actually doesn't hold when it is tested live." The shadow
said variant G made ~+$1.8k/day; its one live hour (KXTEMPMIAH-26OCT1610,
subaccount 2) lost $96 on ~10k 1c filler contracts. The replay had assumed
the market would not react to our orders: fills came only from recorded
trades, made in a market that did not contain our orders.

audit (task 1): the live executor's ACTUAL orders replayed as fixed
trajectories -- every place / amend / renew / decrease / cancel / gone /
expiry of its orders log at its effective time (the logged decision time
plus the request latencies of the rows sent before it in the same
decision, OrderPlan), each order's total as logged -- so the policy is out
of the comparison and only the fill model decides. Each fill model fills
those orders from the recorded tape (live-order subtraction on: our own
orders out of the books, our own prints out of the tape), exactly as the
shadow would: opt, cons, swp (recorded 1c clusters + hidden sweeps +
99c crossers; queue rule 'level' and 'front'; channel (a) alone; strict =
without the recorded takers that hit our live orders) and stress (swp +
level-clear at every price + hidden favorite takers; strict; gap rule).
In each model's world an order keeps the actual lifetime and the logged
total, and fills from its own remaining (total - the model's fills); an
order the live market filled out ends at that fill in every world (what
the bot would have done otherwise is unknown), so the comparison is over
the actual exposure. Compared with the ACTUAL fills (exchange times from
the tape) per order and per class (1c fillers / near-decided / favorite
bids 90-99c / favorite bids under 90c), in contracts, in $ at settlement
and in first-fill latency; plus the hidden-sweep hazard's compensator over
the actual exposure (the expected number of sweeps, sum of lambda dt over
the seconds we rested alone at 1c, against the sweeps that came -- the
unbiased test of the hazard rate).

clears (task 2): how often favorite-bid levels (bid side's fair >= 0.90,
prints at 90-99c) are cleared by a taker cluster, on the recorded hours
with books (cleared = its prints at a level >= the level, or it walked
through to a worse price) and on every settled hour's REST tape (530+
hours: walked = it printed at two or more prices = it cleared at least one
level), and the favorite's realized upset rate after a clear vs after a
non-clearing hit vs the model fair: are level-clearing sellers informed?

stress-table (task 3): temp_shadow_sweep run_events with opt / cons / swp /
stress over every settled recorded hour (the FAV variants, G and G0 by
default), per ET day and total, hour-bootstrap 90% CI, P>0, the total
without the best hour and without the best and worst.

reaction (task 4): did other makers react to our live orders (joined our
price within seconds, stepped ahead of our favorite bids by 1c, cancelled
when we joined, posted a 99c locker opposite our 1c pads)? Each reaction is
measured after our order events and against the same measure at random
instants of the hours before the live one (the baseline).

CLI (outputs to --out, default <TEMP_SHADOW_DATA>/audit; read-only on the
recording and the executor's logs):
  python temp_shadow_audit.py audit --event KXTEMPMIAH-26OCT1610 [--draws 30]
  python temp_shadow_audit.py clears [--days ...] [--prints PATH]
  python temp_shadow_audit.py reaction --event KXTEMPMIAH-26OCT1610
  python temp_shadow_audit.py stress-table [--day ...] [--variants ...] [--draws 6]
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
from collections import defaultdict, deque
from typing import Dict, List, Optional, Sequence, Tuple

import temp_mm_policy as pol
import temp_shadow_common as C
import temp_shadow_scoring as scoring
import temp_shadow_sim as sim
import temp_shadow_sweep as SW

AUDIT_VERSION = "1"
DEFAULT_LAT_S = 0.04      # a logged op without latency_ms (decrease, a cancel batch)
FILL_MATCH_S = 2.0        # a fill row (bot clock) <-> its tape print (exchange clock)
DEFAULT_DRAWS = 30
CLASSES = ("c1", "near", "fav90", "fav_lo", "other")
CLASS_LABELS = {"c1": "1c fillers (pad / qual)", "near": "near-decided quotes",
                "fav90": "favorite bids 90-99c", "fav_lo": "favorite bids < 90c",
                "other": "other"}
# (key, label, fill model, swp options)
AUDIT_CONFIGS: Tuple[Tuple[str, str, str, dict], ...] = (
    ("opt", "opt", "opt", {}),
    ("cons", "cons", "cons", {}),
    ("swp_a", "swp recorded 1c clusters only (a)", "swp", {"channels": "a"}),
    ("swp", "swp (a+b+c, queue rule level)", "swp", {}),
    ("swp_front", "swp, queue rule front", "swp", {"rec_mode": "front"}),
    ("swp_strict", "swp without the takers that hit our live orders", "swp",
     {"own_arrivals": False}),
    ("stress", "stress = swp + level-clear (d) + hidden favorite takers (e)", SW.STRESS, {}),
    ("stress_strict", "stress without the takers that hit our live orders", SW.STRESS,
     {"own_arrivals": False}),
    ("stress_hole", "stress + gap rule", SW.STRESS, {"stress_hole": True}),
    ("swp_nojoin", "swp joiner-free: the sides our pads opened replayed with nobody else at 1c "
                   "and none of their recorded 1c takers (the regime of every pre-live replay)",
     "swp", {"nojoin": True}),
    ("stress_nojoin", "stress joiner-free", SW.STRESS, {"nojoin": True}),
)
# the per-order table's columns (the full set is in the JSON)
ORDER_TABLE_KEYS = ("opt", "cons", "swp", "swp_front", "swp_nojoin", "stress")


def out_dir_default() -> str:
    return os.environ.get("TEMP_SHADOW_AUDIT_OUT") or os.path.join(C.DATA_ROOT, "audit")


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def order_class(tag, price: Optional[int]) -> str:
    """The audit's class of one of our orders at a price: 1c pad / qual
    fillers ('c1'), favorite joins at 90-99c ('fav90') or lower ('fav_lo'),
    near-decided quotes ('near', both sides, 1-4c and 73-95c)."""
    tags = set(str(tag or "").split("+"))
    p = int(price or 0)
    if p == 1 and tags & {"pad", "qual"}:
        return "c1"
    if "fav" in tags:
        return "fav90" if p >= 90 else "fav_lo"
    if "near" in tags:
        return "near"
    return "other"


# --------------------------------------------------------------------------
# The live executor's actual orders as fixed trajectories
# --------------------------------------------------------------------------

_KINDS = ("place", "adopt", "amend", "renew", "decrease", "cancel", "gone", "expired")


class OrderPlan:
    """One event's live orders, rebuilt from the executor's orders log in its
    FILE order (the order the requests went out):

    * effective time: rows of one decision share its logged `ts` (the
      decision time) and were sent one after another, so each op takes
      effect at ts + the latencies of the rows before it in that decision +
      half its own (latency_ms; DEFAULT_LAT_S when not logged; a run of
      cancels is one batch request). The first decision of 26OCT1610 took
      1.13 s for its first create, so the next order rested ~1.2 s after
      its logged time.
    * expiry: an order leaves at its expiration_ts unless a renew moved it.
    * an order the live market filled out ends at that fill (exchange time).
    * queue ahead at a place / price amend: the recorded level at that price
      at the last sample before the logged time, minus our own resting
      there (the recording held none of this order yet).
    orders: oid -> {ticker, m, side, tag, placed (effective), placed_log,
    end, end_kind, size, prices [(t, price)], fills [(exchange ts, q,
    price)], filled}; items: activation items for AuditPass, time-sorted."""

    def __init__(self, event: str, sh: SW.SweepShared, sh_full: SW.SweepShared,
                 lo: scoring.LiveOrders, latency: bool = True):
        self.event = event
        self.sh = sh
        self.latency = bool(latency)
        prefix = event + "-"
        pe = C.parse_event(event)
        t0, t1 = (pe[1] - 3600.0, pe[1]) if pe else (sh.open, sh.close)
        rows = [r for r in scoring.live_log_rows("orders", t0, t1)
                if str(r.get("ticker") or "").startswith(prefix) and r.get("kind") in _KINDS
                and r.get("oid")]
        # effective times (file order within one decision's ts)
        eff: List[float] = []
        cum, last_ts, last_kind = 0.0, None, None
        for r in rows:
            ts = float(r["ts"])
            if ts != last_ts:
                cum, last_ts, last_kind = 0.0, ts, None
            lat = r.get("latency_ms")
            lat = float(lat) / 1000.0 if lat not in (None, "") else DEFAULT_LAT_S
            if r["kind"] in ("cancel", "gone") and last_kind in ("cancel", "gone"):
                lat = 0.0                      # one batch request
            eff.append(ts + ((cum + lat / 2.0) if self.latency else 0.0))
            cum += lat
            last_kind = r["kind"]
        # actual fills, at the exchange time of their tape print
        own = list(getattr(sh.stream, "own_trades", None) or [])
        used = [False] * len(own)
        afills: Dict[str, List[tuple]] = defaultdict(list)
        for f in sorted(lo.fills, key=lambda f: float(f["ts"])):
            tk, q = f.get("ticker"), float(f.get("count") or 0.0)
            ts = float(f["ts"])
            best, bd = None, FILL_MATCH_S
            for j, tr in enumerate(own):
                if used[j] or tr.ticker != tk or abs(float(tr.count) - q) > 1e-6:
                    continue
                d = abs(sim._trade_time(tr) - ts)
                if d <= bd:
                    best, bd = j, d
            if best is not None:
                used[best] = True
                ts = sim._trade_time(own[best])
            afills[f.get("order_id")].append((ts, q, int(f.get("price_ours") or 0)))
        self.unmatched_fills = sum(1 for u in used if not u)
        own_tl = lo.timeline(sh.sec0, sh.sec1)
        # per-order op streams
        by_oid: Dict[str, List[tuple]] = defaultdict(list)
        for r, te in zip(rows, eff):
            by_oid[r["oid"]].append((te, float(r["ts"]), r))
        self.orders: Dict[str, dict] = {}
        items: List[tuple] = []
        for oid, lst in by_oid.items():
            lst.sort(key=lambda x: (x[1], x[0]))
            first = lst[0][2]
            if first["kind"] not in ("place", "adopt"):
                continue                         # placed before the window
            tk = first["ticker"]
            m = sh.mindex.get(tk)
            if m is None:
                continue
            side = str(first.get("side")).lower()
            fl = sorted(afills.get(oid, []))
            filled = sum(q for _, q, _ in fl)
            o = dict(oid=oid, ticker=tk, m=m, side=side, tag=first.get("tag"),
                     placed=lst[0][0], placed_log=lst[0][1], end=None, end_kind=None,
                     size=float(first.get("size") or 0.0), prices=[], fills=fl,
                     filled=filled)
            exp = first.get("expiration_ts")
            cur_exp = float(exp) if exp not in (None, "") else math.inf
            total = o["size"]
            ended = False
            ops: List[tuple] = []
            for te, tl, r in lst:
                k = r["kind"]
                if k not in ("place", "adopt") and tl >= cur_exp:
                    ops.append((cur_exp, ("end", oid, "expired")))
                    ended = True
                    break
                if k in ("place", "adopt"):
                    price = int(r["price"])
                    size = float(r.get("size") or 0.0)
                    total = size if k == "place" else size + filled
                    ops.append((te, ("place", oid, side, price, size,
                                     self._ahead(sh_full, own_tl, m, side, price, tl))))
                    o["prices"].append((te, price))
                elif k in ("amend", "renew"):
                    price = int(r["price"]) if r.get("price") is not None else o["prices"][-1][1]
                    if r.get("total") not in (None, ""):
                        total = float(r["total"])
                    if k == "amend":
                        ops.append((te, ("amend", oid, price, total,
                                         self._ahead(sh_full, own_tl, m, side, price, tl))))
                        if price != o["prices"][-1][1]:
                            o["prices"].append((te, price))
                elif k == "decrease":
                    rb = r.get("reduce_by")
                    rb = float(rb) if rb not in (None, "") else max(
                        0.0, float(r.get("prev_size") or 0.0) - float(r.get("size") or 0.0))
                    total -= rb
                    ops.append((te, ("dec", oid, rb)))
                else:                            # cancel / gone / expired
                    ops.append((te, ("end", oid, k)))
                    ended = True
                    break
                if r.get("expiration_ts") not in (None, ""):
                    cur_exp = float(r["expiration_ts"])
            if fl and filled >= total - 1e-6:
                t_out = fl[-1][0]
                ops = [x for x in ops if x[0] <= t_out]
                ops.append((t_out + 1e-6, ("end", oid, "filled")))
                ended = True
            if not ended and cur_exp < sh.close:
                ops.append((cur_exp, ("end", oid, "expired")))
            ends = [x for x in ops if x[1][0] == "end"]
            if ends:
                o["end"], o["end_kind"] = ends[0][0], ends[0][1][2]
            else:
                o["end"], o["end_kind"] = sh.close, "close"
            self.orders[oid] = o
            items.extend((te, m, op) for te, op in ops)
        items.sort(key=lambda x: x[0])
        # one activation item per op (time, market, [op], rule None)
        self.items = [(te, m, [op], None) for te, m, op in items]

    @staticmethod
    def _ahead(sh_full, own_tl, m: int, side: str, price: int, t_log: float) -> float:
        i = sh_full.idx_at(t_log)
        lv = sh_full.side_levels(m, side, i)
        if lv is None:
            lv = sh_full.side_levels(m, side, i - 1) or {}
        rec = float(lv.get(price, 0.0))
        mine = 0.0
        if 0 <= i < len(own_tl):
            o = own_tl[i].get(sh_full.tickers[m])
            if o:
                mine = float((o[0] if side == "yes" else o[1]).get(price, 0.0))
        return max(0.0, rec - mine)

    def class_at(self, oid: str, price: int) -> str:
        return order_class(self.orders[oid]["tag"], price)

    def exposure(self) -> Dict[str, float]:
        """Contract-seconds each class actually rested (the logged timeline:
        totals minus actual fills), and the actual resting seconds."""
        lo = self.sh.stream.live_own if self.sh.stream is not None else None
        out: Dict[str, float] = defaultdict(float)
        if not lo:
            return out
        tl = lo.timeline(self.sh.sec0, self.sh.sec1)
        tag_at: Dict[tuple, str] = {}
        for o in self.orders.values():
            for _, p in o["prices"]:
                tag_at.setdefault((o["ticker"], o["side"], p), o["tag"])
        for d in tl:
            for tk, (oy, on) in d.items():
                for side, dd in (("yes", oy), ("no", on)):
                    for p, q in dd.items():
                        out[order_class(tag_at.get((tk, side, p), ""), p)] += q
        return out


# --------------------------------------------------------------------------
# The audit pass: VPass on the actual orders (no policy)
# --------------------------------------------------------------------------

class AuditPass(SW.VPass):
    """temp_shadow_sweep.VPass replaying an OrderPlan instead of the policy:
    every fill rule (opt / cons / swp channels / stress channels) acts on our
    actual orders; per-order fills are attributed by order id. In this
    model's world an order keeps its actual life and logged total and fills
    from total - this model's fills on it; a price amend (or a size raise)
    loses the queue place, a cut keeps it."""

    def __init__(self, sh: SW.SweepShared, plan: OrderPlan, fill_model: str, **kw):
        P = pol.make_policy("G_decided_only")      # timing constants only: no decisions
        super().__init__(sh, P, fill_model, **kw)
        self.plan = plan
        self.pending = deque(plan.items)
        self.side_of = {oid: o["side"] for oid, o in plan.orders.items()}
        self.lot: Dict[str, list] = {}
        self.lot_px: Dict[str, int] = {}
        self.prev: Dict[str, float] = {}
        self.total: Dict[str, float] = {}
        self.M: Dict[str, float] = defaultdict(float)
        self.at: Dict[tuple, set] = defaultdict(set)
        self.ofills: Dict[str, List[tuple]] = defaultdict(list)
        self.attr_err = 0.0

    # no policy: the plan is the decisions
    def _decide_all(self, t, idx, fs, t_next):
        return

    def _projected(self, m):
        return {"yes": {}, "no": {}}

    def _put(self, m, side, oid, price, rem, ahead):
        lot = [rem, float(ahead), oid]
        self.lot[oid], self.lot_px[oid], self.prev[oid] = lot, price, rem
        if rem > 1e-9:
            self.orders[m][side].setdefault(price, []).append(lot)
            self.at[(m, side, price)].add(oid)

    def _drop(self, m, side, oid):
        lot = self.lot.pop(oid, None)
        p = self.lot_px.get(oid)
        if lot is None or p is None:
            return
        book = self.orders[m][side]
        lots = book.get(p)
        if lots:
            lots = [l for l in lots if l is not lot]
            if lots:
                book[p] = lots
            else:
                del book[p]
        self.at[(m, side, p)].discard(oid)

    def _activate(self, item):
        t, m, ops, _ = item
        for op in ops:
            kind, oid = op[0], op[1]
            side = self.side_of[oid]
            if kind == "place":
                price, size, ahead = op[3], op[4], op[5]
                self._drop(m, side, oid)
                self.total[oid] = size
                self._put(m, side, oid, price, max(0.0, size - self.M[oid]), ahead)
            elif kind == "amend":
                price, total, ahead = op[2], op[3], op[4]
                rem = max(0.0, total - self.M[oid])
                lot = self.lot.get(oid)
                if (lot is not None and lot[0] > 1e-9 and price == self.lot_px.get(oid)
                        and total <= self.total.get(oid, total) + 1e-9):
                    lot[0] = rem                 # a cut keeps the queue place
                    self.prev[oid] = rem
                    if rem <= 1e-9:
                        self._drop(m, side, oid)
                else:
                    self._drop(m, side, oid)
                    self._put(m, side, oid, price, rem, ahead)
                self.total[oid] = total
            elif kind == "dec":
                rb = op[2]
                self.total[oid] = self.total.get(oid, 0.0) - rb
                lot = self.lot.get(oid)
                if lot is not None:
                    lot[0] = max(0.0, lot[0] - rb)
                    self.prev[oid] = lot[0]
                    if lot[0] <= 1e-9:
                        self._drop(m, side, oid)
            else:                                 # end
                self._drop(m, side, oid)
        self.own_ver[m] += 1
        if self.swp:
            self._ep_update(m, t)
            if self.expo_log is not None:
                self._log_expo(t, m)

    def _record_fill(self, ts, m, side, p, q):
        super()._record_fill(ts, m, side, p, q)
        got = 0.0
        for oid in list(self.at.get((m, side, p), ())):
            lot = self.lot.get(oid)
            if lot is None:
                continue
            d = self.prev[oid] - lot[0]
            if d > 1e-9:
                self.M[oid] += d
                self.prev[oid] = lot[0]
                got += d
                self.ofills[oid].append((ts, d, self.src, p))
            if lot[0] <= 1e-9:
                self.lot.pop(oid, None)
                self.at[(m, side, p)].discard(oid)
        self.attr_err += abs(got - q)

    def order_fills(self) -> Dict[str, List[tuple]]:
        return {oid: list(v) for oid, v in self.ofills.items()}


# --------------------------------------------------------------------------
# Audit of one live hour
# --------------------------------------------------------------------------

def _payoff(sh, m: int, side: str) -> float:
    py = sh.payoff_yes(m)
    return py if side == "yes" else 1.0 - py


def _class_rows(plan: OrderPlan, sh, fills_by_oid: Dict[str, List[tuple]]
                ) -> Dict[str, dict]:
    """per class: contracts, $ at settlement, fills, orders filled."""
    out = {c: {"contracts": 0.0, "pnl": 0.0, "fills": 0, "orders": set()} for c in CLASSES}
    for oid, fl in fills_by_oid.items():
        o = plan.orders.get(oid)
        if o is None:
            continue
        pay = _payoff(sh, o["m"], o["side"])
        for f in fl:
            ts, q, p = f[0], f[1], f[-1]
            c = plan.class_at(oid, p)
            out[c]["contracts"] += q
            out[c]["pnl"] += q * (pay - p / 100.0)
            out[c]["fills"] += 1
            out[c]["orders"].add(oid)
    for c in out:
        out[c]["orders"] = len(out[c]["orders"])
    return out


def _pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def run_config(sh, plan, fm: str, opts: dict, draws: int, hazard, fav, sh_nj=None) -> dict:
    """One fill rule over the plan: per order (mean filled, P(filled), first
    fill latency per draw), per class (mean / p10 / p90 contracts, mean $),
    reward, attribution check. opts 'nojoin' runs on sh_nj (the joiner-free
    shared state)."""
    opts = dict(opts)
    if opts.pop("nojoin", False):
        sh = sh_nj if sh_nj is not None else sh
    mc = fm in ("swp", SW.STRESS) and ("b" in opts.get("channels", "abc")
                                       or (fm == SW.STRESS and fav is not None))
    n = int(draws) if mc else 1
    per_draw = []
    for k in range(n):
        kw = dict(opts)
        if fm in ("swp", SW.STRESS):
            ch = kw.get("channels", "abc")
            kw["hazard"] = hazard
            kw["cands"] = SW.hazard_candidates(sh, k, hazard.lam_max) if "b" in ch else None
        if fm == SW.STRESS and fav is not None:
            kw["fav_hazard"] = fav
            kw["fav_cands"] = SW.fav_candidates(sh, k, fav.lam_max)
        ap = AuditPass(sh, plan, fm, **kw)
        summ = ap.run()
        per_draw.append((ap.order_fills(), summ, ap.attr_err))
    orders = {}
    for oid, o in plan.orders.items():
        tot = [sum(f[1] for f in d[0].get(oid, [])) for d in per_draw]
        first = [min(f[0] for f in d[0][oid]) - o["placed"] for d in per_draw if d[0].get(oid)]
        srcs = defaultdict(float)
        for d in per_draw:
            for f in d[0].get(oid, []):
                srcs[f[2]] += f[1] / n
        orders[oid] = {"filled": _r(sum(tot) / n, 2), "p_fill": _r(len(first) / n, 3),
                       "first_s_med": _r(_pct(first, 0.5), 2), "src": dict(srcs)}
    cls_draws = [_class_rows(plan, sh, d[0]) for d in per_draw]
    classes = {}
    for c in CLASSES:
        ctr = [x[c]["contracts"] for x in cls_draws]
        pnl = [x[c]["pnl"] for x in cls_draws]
        classes[c] = {"contracts": _r(sum(ctr) / n, 2), "pnl": _r(sum(pnl) / n, 4),
                      "contracts_p10": _r(_pct(ctr, 0.1), 1), "contracts_p90": _r(_pct(ctr, 0.9), 1),
                      "pnl_p10": _r(_pct(pnl, 0.1), 2), "pnl_p90": _r(_pct(pnl, 0.9), 2),
                      "fills": _r(sum(x[c]["fills"] for x in cls_draws) / n, 2)}
    rew = [d[1]["reward"] for d in per_draw]
    src = defaultdict(float)
    for d in per_draw:
        for kk, v in (d[1].get("sweep_src") or {}).items():
            src[kk] += v / n
    return {"fill_model": fm, "opts": {k: v for k, v in opts.items()}, "draws": n,
            "orders": orders, "classes": classes, "reward": _r(sum(rew) / n, 4),
            "sweep_src": dict(src), "attr_err": _r(max(d[2] for d in per_draw), 6),
            "contracts": _r(sum(classes[c]["contracts"] for c in CLASSES), 2),
            "pnl": _r(sum(classes[c]["pnl"] for c in CLASSES), 4)}


def hazard_compensator(sh, plan: OrderPlan, hazard, fav) -> dict:
    """The hidden-sweep hazard over the ACTUAL exposure. For every second our
    live 1c orders rested (>= 1 contract, the logged timeline) with nobody
    else at 1c on that side ('alone'), lambda(favorite fair, minutes since
    open, age of our 1c episode) is summed: the expected number of hidden
    sweeps given that exposure (sum of lambda dt), against the taker
    clusters that actually hit our 1c orders while we were alone -- an
    unbiased test of the rate (the at-risk time stops at a fill in both).
    The same for our favorite lots alone at the top of their side with the
    favorite-hazard table (stress channel (e))."""
    lo = sh.stream.live_own
    tl = lo.timeline(sh.sec0, sh.sec1)
    ep: Dict[tuple, Optional[float]] = {}
    alone = defaultdict(lambda: {"secs": 0.0, "expected": 0.0, "hits": 0, "contracts": 0.0})
    cov = defaultdict(lambda: {"secs": 0.0, "hits": 0, "contracts": 0.0})
    favx = defaultdict(lambda: {"secs": 0.0, "expected": 0.0, "hits": 0, "contracts": 0.0})
    favep: Dict[tuple, Optional[tuple]] = {}
    state_at: Dict[tuple, List[Optional[str]]] = defaultdict(lambda: [None] * sh.n)
    fstate_at: Dict[tuple, List[Optional[str]]] = defaultdict(lambda: [None] * sh.n)
    for i in range(min(sh.n, len(tl))):
        T = sh.sec0 + i + 0.5
        if T >= sh.end_t:
            break
        for m, tk in enumerate(sh.tickers):
            o = tl[i].get(tk)
            for side in ("yes", "no"):
                key = (m, side)
                d = (o[0] if side == "yes" else o[1]) if o else {}
                q1 = d.get(1, 0.0)
                f = sh.fair_or_mid(m, T)
                if q1 >= 1.0:
                    if ep.get(key) is None:
                        ep[key] = T
                    ext = sh.level1(m, side, i)
                    fp = SW.fav_p_of(side, f)
                    if ext is not None and fp is not None:
                        fb = SW.fav_bucket(fp)
                        if ext < 1.0:
                            lam = hazard.rate(fp, (T - sh.open) / 60.0, T - ep[key])
                            alone[fb]["secs"] += 1.0
                            alone[fb]["expected"] += lam
                            state_at[key][i] = "alone:" + fb
                        else:
                            cov[fb]["secs"] += 1.0
                            state_at[key][i] = "covered:" + fb
                else:
                    ep[key] = None
                # favorite lots (>= 2c) alone at the top of the side
                ps = [p for p, q in d.items() if p >= 2 and q >= 1.0]
                lv = sh.side_levels(m, side, i)
                if not ps or lv is None or f is None or fav is None:
                    favep[key] = None
                    continue
                sp = f if side == "yes" else 1.0 - f
                p = max(ps)
                top = lv.get(p, 0.0) < 1.0 and not any(qq >= 1.0 and pp > p
                                                        for pp, qq in lv.items())
                if sp < SW.FAVHAZ_MIN_P or not top:
                    favep[key] = None
                    continue
                if favep.get(key) is None or favep[key][0] != p:
                    favep[key] = (p, T)
                fb = SW.fav_bucket(sp)
                favx[fb]["secs"] += 1.0
                favx[fb]["expected"] += fav.rate(sp, (T - sh.open) / 60.0, T - favep[key][1])
                fstate_at[key][i] = fb
    # the hits: our actual fills -> taker clusters (CLUSTER_S per market/side)
    fl = []
    for o in plan.orders.values():
        for ts, q, p in o["fills"]:
            fl.append((ts, o["m"], o["side"], p, q))
    fl.sort()
    last: Dict[tuple, float] = {}
    def _state(arr, i0):
        for i in (i0, i0 - 1):
            if 0 <= i < sh.n and arr[i] is not None:
                return arr[i]
        return None
    for ts, m, side, p, q in fl:
        key = (m, side, p == 1)
        i0 = sh.idx_at(ts)
        if key in last and ts - last[key] <= SW.CLUSTER_S:
            new = False
        else:
            new = True
            last[key] = ts
        if p == 1:
            st = _state(state_at[(m, side)], i0)
            if st is None:
                continue
            kind, fb = st.split(":")
            dst = alone[fb] if kind == "alone" else cov[fb]
            dst["hits"] += int(new)
            dst["contracts"] += q
        else:
            fb = _state(fstate_at[(m, side)], i0)
            if fb is not None:
                favx[fb]["hits"] += int(new)
                favx[fb]["contracts"] += q

    def pois(n, mu):
        if mu <= 0:
            return None, None
        cdf, term = 0.0, math.exp(-mu)
        for k in range(n + 1):
            cdf += term
            term *= mu / (k + 1)
        p_le = cdf
        p_ge = 1.0 - (cdf - math.exp(-mu) * mu ** n / math.factorial(n))
        return _r(p_le, 4), _r(p_ge, 4)
    out = {}
    for name, dd in (("c1_alone", alone), ("c1_covered", cov), ("fav_alone_top", favx)):
        rows = {fb: {k: _r(v, 3) for k, v in d.items()} for fb, d in sorted(dd.items())}
        tot = {"secs": sum(d["secs"] for d in dd.values()),
               "hits": sum(d["hits"] for d in dd.values()),
               "contracts": _r(sum(d["contracts"] for d in dd.values()), 2)}
        if name != "c1_covered":
            tot["expected"] = _r(sum(d["expected"] for d in dd.values()), 3)
            tot["p_le"], tot["p_ge"] = pois(tot["hits"], tot["expected"])
        out[name] = {"by_fav": rows, "total": tot}
    return out


def audit_event(event: str, draws: int = DEFAULT_DRAWS, hazard=None, fav=None,
                configs: Sequence[tuple] = AUDIT_CONFIGS, model=None,
                latency: bool = True) -> dict:
    """Task 1 for one live hour (see the module doc)."""
    t_start = time.time()
    lo = scoring.load_live_orders(event)
    if not lo:
        raise ValueError(f"{event}: the live executor logged no orders")
    hazard = hazard or SW.load_hazard(sim.SWP_HAZARD_REPO)
    if fav is None:
        fav = SW.load_fav_hazard()
    sh = SW.shared_for(event, model, subtract=True)
    sh_full = SW.shared_for(event, model, subtract=False)
    plan = OrderPlan(event, sh, sh_full, lo, latency=latency)
    jsides = SW.joiner_sides(sh_full, lo)
    sh_nj = SW.shared_for(event, model, subtract=True, strip_1c=jsides) if jsides else sh
    actual = {oid: o["fills"] for oid, o in plan.orders.items() if o["fills"]}
    act_cls = _class_rows(plan, sh, actual)
    res = {"event": event, "settled": bool(sh.inp.settled), "results": dict(sh.inp.results),
           "open": sh.open, "close": sh.close, "draws": int(draws),
           "hazard_id": hazard.ident(), "hazard_built": hazard.doc.get("built"),
           "fav_hazard_id": fav.ident() if fav else None,
           "orders_n": len(plan.orders), "unmatched_fills": plan.unmatched_fills,
           "latency_corrected": bool(latency), "exposure": dict(plan.exposure()),
           "actual": {"classes": {c: {k: (_r(v, 4) if isinstance(v, float) else v)
                                      for k, v in d.items()} for c, d in act_cls.items()},
                      "contracts": _r(sum(d["contracts"] for d in act_cls.values()), 2),
                      "pnl": _r(sum(d["pnl"] for d in act_cls.values()), 4)},
           "orders": {}, "configs": {}}
    for oid, o in plan.orders.items():
        res["orders"][oid] = {
            "ticker": o["ticker"], "side": o["side"], "tag": o["tag"],
            "placed_s": _r(o["placed"] - sh.open, 2), "placed_log_s": _r(o["placed_log"] - sh.open, 2),
            "end_s": _r(o["end"] - sh.open, 2), "end_kind": o["end_kind"], "size": o["size"],
            "prices": sorted({p for _, p in o["prices"]}),
            "class0": order_class(o["tag"], o["prices"][0][1] if o["prices"] else None),
            "filled": _r(o["filled"], 2),
            "first_s": _r(o["fills"][0][0] - o["placed"], 2) if o["fills"] else None,
            "fills": [(_r(ts - sh.open, 3), q, p) for ts, q, p in o["fills"]]}
    res["joiner_sides"] = {f"{sh.tickers[m]}|{sd}": round(i - 0.0, 1)
                           for (m, sd), i in sorted(jsides.items())}
    res["joiner_arrivals_dropped"] = getattr(sh_nj, "stripped_arrivals", 0) if jsides else 0
    for key, label, fm, opts in configs:
        r = run_config(sh, plan, fm, opts, draws, hazard, fav, sh_nj=sh_nj)
        r["label"] = label
        res["configs"][key] = r
        print(f"  {key}: {r['contracts']:.0f} contracts, ${r['pnl']:.2f} ({r['draws']} draws)",
              flush=True)
    res["compensator"] = hazard_compensator(sh, plan, hazard, fav)
    res["compensator_nojoin"] = hazard_compensator(sh_nj, plan, hazard, fav) if jsides else None
    res["runtime_s"] = round(time.time() - t_start, 1)
    return res


def format_audit(a: dict) -> str:
    keys = [k for k, *_ in AUDIT_CONFIGS if k in a["configs"]]
    L = [f"order-level fill audit {a['event']} ({a['orders_n']} live orders, {a['draws']} Monte "
         f"Carlo draws for the hazard channels; 1c hazard {a['hazard_id']}, favorite hazard "
         f"{a['fav_hazard_id']}; effective times {'latency-corrected' if a['latency_corrected'] else 'as logged'})",
         "the bot's actual orders replayed as fixed trajectories; each model fills them from the "
         "recorded tape (our own orders out of the books, our own prints out of the tape)", ""]
    w = 10
    hdr = f"{'class':24s} {'actual':>{w}s} " + " ".join(f"{k[:w]:>{w}s}" for k in keys)
    for what, fmt in (("contracts", "{:10.0f}"), ("pnl", "{:10.2f}")):
        L.append(("CONTRACTS filled" if what == "contracts" else "$ AT SETTLEMENT") + ":")
        L.append(hdr)
        for c in CLASSES:
            av = a["actual"]["classes"][c][what]
            cells = [a["configs"][k]["classes"][c][what] for k in keys]
            if not av and not any(cells):
                continue
            L.append(f"{CLASS_LABELS[c]:24s} {fmt.format(av)} " + " ".join(fmt.format(x) for x in cells))
        av = a["actual"][what]
        L.append(f"{'TOTAL':24s} {fmt.format(av)} " + " ".join(
            fmt.format(a["configs"][k][what]) for k in keys))
        L.append("")
    L.append("model / actual, 1c fillers (contracts): " + ", ".join(
        f"{k} {a['configs'][k]['classes']['c1']['contracts'] / max(1e-9, a['actual']['classes']['c1']['contracts']):.2f}"
        for k in keys))
    L.append("p10-p90 over draws, 1c contracts: " + ", ".join(
        f"{k} [{a['configs'][k]['classes']['c1']['contracts_p10']:.0f}, "
        f"{a['configs'][k]['classes']['c1']['contracts_p90']:.0f}]" for k in keys
        if a["configs"][k]["draws"] > 1))
    L.append("favorite bids >= 2c (contracts / $): " + ", ".join(
        f"{k} {sum(a['configs'][k]['classes'][c]['contracts'] for c in ('fav90', 'fav_lo', 'near')):.0f} / "
        f"{sum(a['configs'][k]['classes'][c]['pnl'] for c in ('fav90', 'fav_lo', 'near')):+.2f}"
        for k in keys))
    L.append("reward in each model's world ($; the actual estimate is the shadow's scoring of the "
             "live orders): " + ", ".join(f"{k} {a['configs'][k]['reward']:.2f}" for k in keys))
    L.append("")
    # per order: the filled ones and the ones any model fills
    L.append("per order (actual filled / first fill s after it rested; model mean filled, "
             "P(filled), median first-fill s):")
    okeys = [k for k in ORDER_TABLE_KEYS if k in a["configs"]]
    L.append(f"{'order':14s} {'strike':7s} {'side':4s} {'tag':9s} {'px':>9s} {'rest s':>7s} {'life s':>7s} "
             f"{'actual':>12s} " + " ".join(f"{k[:12]:>18s}" for k in okeys))
    rows = []
    for oid, o in a["orders"].items():
        mx = max(a["configs"][k]["orders"][oid]["filled"] or 0 for k in okeys)
        if (o["filled"] or 0) > 0 or mx >= 1.0:
            rows.append((o["placed_s"], oid, o))
    for _, oid, o in sorted(rows):
        px = o["prices"]
        pxs = (f"{px[0]}" if len(px) == 1 else f"{min(px)}-{max(px)}")
        act = f"{o['filled']:.0f}/{o['first_s']:.1f}" if o["filled"] else "0"
        cells = []
        for k in okeys:
            m = a["configs"][k]["orders"][oid]
            fs = f"{m['first_s_med']:.0f}" if m["first_s_med"] is not None else "-"
            cells.append(f"{m['filled']:7.0f} {m['p_fill']:4.2f} {fs:>5s}")
        L.append(f"{oid[:14]:14s} {o['ticker'].rsplit('-', 1)[-1]:7s} {o['side']:4s} {str(o['tag'])[:9]:9s} "
                 f"{pxs:>9s} {o['placed_s']:7.1f} {o['end_s'] - o['placed_s']:7.1f} {act:>12s} "
                 + " ".join(f"{c:>18s}" for c in cells))
    L.append("")
    # timing of the 1c fillers
    act_first = sorted(o["first_s"] for o in a["orders"].values()
                       if o["first_s"] is not None and o["class0"] == "c1")
    L.append(f"first-fill latency of the filled 1c fillers, actual (s after the order rested): "
             f"{[round(x, 1) for x in act_first]}")
    for k in keys:
        fs = [a["configs"][k]["orders"][oid]["first_s_med"] for oid, o in a["orders"].items()
              if o["first_s"] is not None and o["class0"] == "c1"]
        pf = [a["configs"][k]["orders"][oid]["p_fill"] for oid, o in a["orders"].items()
              if o["first_s"] is not None and o["class0"] == "c1"]
        L.append(f"  {k:13s} on those orders: P(filled) mean {sum(pf) / max(1, len(pf)):.2f}, median "
                 f"first fill {sorted(x for x in fs if x is not None)}")
    L.append("")
    L.append(f"joiner-free sides (our first 1c order opened an empty 1c level): "
             f"{sorted(a.get('joiner_sides') or {})}; their recorded 1c taker clusters dropped: "
             f"{a.get('joiner_arrivals_dropped')}")
    for ck, title in (("compensator", "the live book (others who joined our 1c count as cover)"),
                      ("compensator_nojoin", "the joiner-free book (the pre-live regime)")):
        cp = a.get(ck) or {}
        if not cp:
            continue
        L.append(f"hazard over the ACTUAL exposure, {title} (sum of lambda dt = expected takers; "
                 f"hits = taker clusters that filled us):")
        for name, label in (("c1_alone", "1c orders alone at 1c (hidden sweeps, swp channel b)"),
                            ("c1_covered", "1c orders with others at 1c (recorded takers, channel a)"),
                            ("fav_alone_top", "favorite lots alone at the top (stress channel e)")):
            d = cp.get(name) or {}
            t = d.get("total") or {}
            L.append(f"  {label}: {t.get('secs', 0):.0f} s, hits {t.get('hits', 0)} "
                     f"({t.get('contracts', 0):.0f} contracts)"
                     + (f", expected {t.get('expected', 0):.2f} (P(X<=hits) {t.get('p_le')}, "
                        f"P(X>=hits) {t.get('p_ge')})" if "expected" in t else ""))
            for fb, r in (d.get("by_fav") or {}).items():
                L.append(f"      {fb:>10s}: {r.get('secs', 0):6.0f} s, hits {int(r.get('hits', 0))}"
                         + (f", expected {r.get('expected', 0):.2f}" if "expected" in r else "")
                         + f", contracts {r.get('contracts', 0):.0f}")
    ex = a.get("exposure") or {}
    if ex:
        L.append("actual exposure (contract-seconds resting): " + ", ".join(
            f"{CLASS_LABELS[c]} {ex.get(c, 0):,.0f}" for c in CLASSES if ex.get(c)))
    return "\n".join(L)


# --------------------------------------------------------------------------
# Level clears at favorite bids, and whether the clearing sellers are informed
# --------------------------------------------------------------------------

CLEAR_FAV_MIN = 0.90        # the hit bid side's fair (the favorite side)
CLEAR_PX_MIN = 90           # a favorite-bid print at 90-99c
CLEAR_FB = ((0.90, 0.97), (0.97, 0.99), (0.99, 0.995), (0.995, 1.0001))


def _clear_bucket(sp: float) -> Optional[str]:
    for lo, hi in CLEAR_FB:
        if lo <= sp < hi:
            return f"{lo:.3f}-{min(hi, 1.0):.3f}"
    return None


def fav_clusters_event(event: str, model=None) -> dict:
    """Every taker cluster (prints on one market / side within CLUSTER_S;
    the full tape, nothing subtracted) that hit a favorite side's bids at
    >= CLEAR_PX_MIN, with the book before it: per cluster its prices, the
    levels, cleared (its prints at its worst price >= that level, or it
    printed at two or more prices), walked, whether a next level existed
    within 2c below its top level, the side's fair, minutes left and the
    strike's outcome; plus the favorite-level exposure (strike-seconds with
    a best bid >= CLEAR_PX_MIN on a side whose fair >= CLEAR_FAV_MIN)."""
    sh = SW.shared_for(event, model, subtract=False)
    res = sh.inp.results
    expo = defaultdict(float)
    for i in range(sh.n):
        T = sh.sec0 + i + 0.5
        if T >= sh.end_t:
            break
        for m in range(sh.nm):
            info = sh.info_at_idx(m, i)
            if info is None:
                continue
            f = sh.fair_or_mid(m, T)
            if f is None:
                continue
            for side in ("yes", "no"):
                sp = f if side == "yes" else 1.0 - f
                lv = info.by if side == "yes" else info.bn
                b = max([p for p, q in lv.items() if q >= 1.0], default=0)
                fb = _clear_bucket(sp) if sp >= CLEAR_FAV_MIN else None
                if fb and b >= CLEAR_PX_MIN:
                    expo[fb] += 1.0
    rows = []
    open_c: Dict[tuple, dict] = {}
    cl: List[dict] = []
    for ts, m, taker, yc, cnt in sh.trades:
        side = "yes" if taker == "no" else "no"
        px = yc if side == "yes" else 100 - yc
        c = open_c.get((m, side))
        if c is None or ts - c["t0"] > SW.CLUSTER_S:
            c = dict(t0=ts, m=m, side=side, px=defaultdict(float), nts=set())
            cl.append(c)
            open_c[(m, side)] = c
        c["px"][px] += cnt
        c["nts"].add(round(ts, 3))
    for c in cl:
        m, side, t0 = c["m"], c["side"], c["t0"]
        if t0 >= sh.end_t or max(c["px"]) < CLEAR_PX_MIN:
            continue
        f = sh.fair_or_mid(m, t0)
        if f is None:
            continue
        sp = f if side == "yes" else 1.0 - f
        if sp < CLEAR_FAV_MIN:
            continue
        i0 = sh.idx_at(t0)
        lv = [x for x in (sh.side_levels(m, side, i0 - 1), sh.side_levels(m, side, i0))
              if x is not None]
        if not lv:
            continue
        prices = sorted(c["px"], reverse=True)
        L = {p: max(d.get(p, 0.0) for d in lv) for p in prices}
        w = prices[-1]
        clr_w = c["px"][w] >= L[w] - 0.5
        top = prices[0]
        nxt = [p for p, q in lv[0].items() if q >= 1.0 and top - 2 <= p < top]
        r = res.get(sh.tickers[m])
        rows.append(dict(event=event, ticker=sh.tickers[m], side=side, t=_r(t0 - sh.open, 2),
                         mins_left=_r((sh.close - t0) / 60.0, 2), sp=_r(sp, 5),
                         q=_r(sum(c["px"].values()), 2), prices=prices,
                         qp=[_r(c["px"][p], 2) for p in prices], L=[_r(L[p], 2) for p in prices],
                         walked=len(prices) > 1, cleared=bool(clr_w or len(prices) > 1),
                         top_cleared=bool(c["px"][top] >= L[top] - 0.5),
                         next_within2=bool(nxt), takers=len(c["nts"]),
                         won=(None if r not in ("yes", "no") else r == side)))
    return {"event": event, "expo": dict(expo), "clusters": rows,
            "hour_et": C.et_dt(float(sh.inp.settle_ts)).hour, "et_day": C.et_day(sh.inp.settle_ts)}


def _boot_rate(lst: List[dict], n: int = 2000, seed: int = 17) -> Tuple[Optional[float], Optional[float]]:
    """Strike-clustered bootstrap 90% CI of the cluster-weighted upset rate."""
    by = defaultdict(list)
    for r in lst:
        by[(r["event"], r["ticker"])].append(r)
    keys = list(by)
    if not keys:
        return None, None
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        s = [by[keys[rng.randrange(len(keys))]] for _ in keys]
        nn = sum(len(x) for x in s)
        uu = sum(1 for x in s for r in x if not r["won"])
        out.append(uu / nn if nn else 0.0)
    out.sort()
    return out[int(0.05 * n)], out[min(n - 1, int(0.95 * n))]


def _rate_rows(groups: Dict[tuple, List[dict]]) -> List[dict]:
    rows = []
    for (b, kind), lst in sorted(groups.items()):
        lst = [r for r in lst if r.get("won") is not None]
        if not lst:
            continue
        n = len(lst)
        qq = sum(r["q"] for r in lst)
        strikes = {(r["event"], r["ticker"]) for r in lst}
        ups = {(r["event"], r["ticker"]) for r in lst if not r["won"]}
        lo, hi = _boot_rate(lst)
        rows.append(dict(bucket=b, kind=kind, n=n, strikes=len(strikes), upset_strikes=len(ups),
                         contracts=_r(qq, 1),
                         model_longshot=_r(sum(1.0 - r["sp"] for r in lst) / n, 5),
                         upset_rate=_r(sum(1 for r in lst if not r["won"]) / n, 5),
                         upset_ci=[_r(lo, 5), _r(hi, 5)],
                         upset_rate_ctr=_r(sum(r["q"] for r in lst if not r["won"]) / qq, 5)
                         if qq else None))
    return rows


def tape_clusters(prints: Sequence[Sequence]) -> List[dict]:
    """REST-tape favorite-bid clusters (one taker = one (event, ticker, taker
    side, timestamp)) from SW.prints_backfill / prints_recorded rows (event,
    ticker, ts, P(YES), yes_cents, taker_side, count, y, src): walked = it
    printed at two or more prices, i.e. it cleared at least one level."""
    cl: Dict[tuple, dict] = {}
    for ev, tk, ts, p, yc, side, cnt, y, src in prints:
        k = (ev, tk, side, round(float(ts), 3))
        c = cl.get(k)
        if c is None:
            c = cl[k] = dict(event=ev, ticker=tk, side=side, p=float(p), y=int(y), src=src,
                             px=defaultdict(float))
        c["px"][int(yc)] += float(cnt)
    out = []
    for c in cl.values():
        bid = "yes" if c["side"] == "no" else "no"
        sp = c["p"] if bid == "yes" else 1.0 - c["p"]
        prices = sorted(((yc if bid == "yes" else 100 - yc), q) for yc, q in c["px"].items())
        if sp < CLEAR_FAV_MIN or prices[-1][0] < CLEAR_PX_MIN:
            continue
        out.append(dict(event=c["event"], ticker=c["ticker"], sp=sp, src=c["src"],
                        q=sum(q for _, q in prices), walked=len(prices) > 1,
                        won=(c["y"] == 1) if bid == "yes" else (c["y"] == 0)))
    return out


def clears_report(events: Sequence[str], prints: Optional[dict] = None, workers: int = 2,
                  model=None) -> dict:
    """Task 2: favorite-level clears on the recorded hours (books) and on the
    REST tape of every settled hour (prints: {"backfill": rows, "recorded":
    rows}; computed when None), and the upset rates after them. model (in
    process only: workers 1) replaces the fair model."""
    if model is not None:
        evrows = [fav_clusters_event(e, model) for e in events]
    elif workers > 1 and len(events) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(SW.MAX_WORKERS, workers),
                                 initializer=SW._worker_init) as ex:
            evrows = list(ex.map(_fav_clusters_job, list(events)))
    else:
        evrows = [_fav_clusters_job(e) for e in events]
    evrows = [r for r in evrows if r and not r.get("error")]
    cl = [c for r in evrows for c in r["clusters"]]
    expo = defaultdict(float)
    for r in evrows:
        for fb, v in r["expo"].items():
            expo[fb] += v
    g = defaultdict(list)
    for c in cl:
        b = _clear_bucket(c["sp"])
        g[(b, "all hits")].append(c)
        g[(b, "cleared" if c["cleared"] else "not cleared")].append(c)
    freq = {}
    for (lo, hi) in CLEAR_FB:
        b = _clear_bucket(lo)
        n_all = len(g[(b, "all hits")])
        n_clr = len(g[(b, "cleared")])
        e_h = expo.get(b, 0.0) / 3600.0
        freq[b] = dict(expo_strike_h=_r(e_h, 2), hits=n_all, clears=n_clr,
                       hits_per_h=_r(n_all / e_h, 3) if e_h else None,
                       clears_per_h=_r(n_clr / e_h, 3) if e_h else None,
                       clear_frac=_r(n_clr / n_all, 3) if n_all else None)
    # appetite: a top-level clear with a next level within 2c -> did it go on?
    app = [c for c in cl if c["top_cleared"] and c["next_within2"]]
    appetite = dict(n=len(app), walked=sum(1 for c in app if c["walked"]),
                    frac=_r(sum(1 for c in app if c["walked"]) / len(app), 3) if app else None)
    multi = sum(1 for c in cl if c["takers"] > 1)
    out = {"events": len(evrows), "clusters": len(cl), "frequency": freq, "appetite": appetite,
           "merged_takers": multi, "books": _rate_rows(g)}
    if prints is None:
        pb = SW.prints_backfill()
        seen = {r[0] for r in pb}
        pr = SW.prints_recorded([e for e in events if e not in seen], workers=workers)
        prints = {"backfill": pb, "recorded": pr}
    tc = tape_clusters(list(prints.get("backfill") or []) + list(prints.get("recorded") or []))
    tg = defaultdict(list)
    for c in tc:
        b = _clear_bucket(c["sp"])
        for per in ("all", c["src"]):
            tg[(f"{per} {b}", "all hits")].append(c)
            tg[(f"{per} {b}", "walked" if c["walked"] else "single price")].append(c)
    out["tape"] = _rate_rows(tg)
    out["tape_hours"] = {k: len({c["event"] for c in tc if c["src"] == k})
                         for k in ("backfill", "recorded")}
    out["by_hour_upsets"] = sorted({(c["event"], c["ticker"]) for c in cl if c["won"] is False})
    return out


def _fav_clusters_job(ev):
    try:
        return fav_clusters_event(ev)
    except Exception as e:                      # noqa: BLE001
        return {"event": ev, "error": f"{type(e).__name__}: {e}"}


def format_clears(d: dict) -> str:
    L = [f"favorite-bid level clears (bid side fair >= {CLEAR_FAV_MIN}, prints at >= {CLEAR_PX_MIN}c), "
         f"{d['events']} recorded hours with books, {d['clusters']} taker clusters "
         f"({d['merged_takers']} clusters merge two or more timestamps within {SW.CLUSTER_S} s):",
         f"  {'fav bucket':12s} {'strike-h':>8s} {'hits':>5s} {'clears':>6s} {'hits/h':>7s} "
         f"{'clears/h':>8s} {'clear%':>6s}"]
    for b, r in d["frequency"].items():
        L.append(f"  {b:12s} {r['expo_strike_h'] or 0:8.1f} {r['hits']:5d} {r['clears']:6d} "
                 f"{r['hits_per_h'] or 0:7.2f} {r['clears_per_h'] or 0:8.2f} "
                 f"{100 * (r['clear_frac'] or 0):6.1f}")
    ap = d["appetite"]
    L.append(f"  appetite: of {ap['n']} clusters that cleared the top level with another level within "
             f"2c below, {ap['walked']} went on into it ({100 * (ap['frac'] or 0):.0f}%)")
    for name, rows, note in (("recorded hours (books)", d["books"], "cleared = took its whole worst "
                              "level or walked through a level"),
                             (f"REST tape ({d['tape_hours'].get('backfill', 0)} backfill + "
                              f"{d['tape_hours'].get('recorded', 0)} recorded hours)", d["tape"],
                              "walked = printed at 2+ prices (cleared at least one level); one "
                              "taker = one timestamp")):
        L.append(f"upset rate after a favorite-bid hit, {name} ({note}); CI = strike-clustered "
                 f"bootstrap 90%:")
        L.append(f"  {'bucket':22s} {'kind':12s} {'n':>6s} {'strikes':>7s} {'model%':>7s} "
                 f"{'upset%':>7s} {'90% CI':>16s} {'ctr-wtd%':>8s} {'upset strikes':>13s}")
        for r in rows:
            ci = r["upset_ci"]
            L.append(f"  {r['bucket']:22s} {r['kind']:12s} {r['n']:6d} {r['strikes']:7d} "
                     f"{100 * r['model_longshot']:7.3f} {100 * r['upset_rate']:7.2f} "
                     f"[{100 * (ci[0] or 0):5.2f},{100 * (ci[1] or 0):6.2f}] "
                     f"{100 * (r['upset_rate_ctr'] or 0):8.2f} {r['upset_strikes']:6d}")
    L.append(f"upset strikes in the recorded hours: {d['by_hour_upsets']}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Market reaction to our live orders (task 4)
# --------------------------------------------------------------------------

REACT_H = (2, 5, 12, 30)       # seconds after an order took effect
PAD_QUIET_S = 30               # a 1c level counts as empty when < 1 contract for this long
FAV_JOIN_MIN = 100.0           # a baseline favorite join: the reference level grows by >= this


def _probe(sh, m: int, side: str, p: int, i_before: int, i_from: int, H: int,
           added: float = 0.0) -> dict:
    """What others did at and around price p of one side over the samples
    (i_from, i_from + H] against the sample i_before (external book):
    joined = size added at p beyond the event's own (>= 10 counts), ahead =
    a new external bid above p, cancel = the size at p before the event fell
    by >= 50%, locker = the opposite side's 99c bid grew by >= 1 contract.
    added = the event's own size inside the external book (a baseline
    maker's join; 0 for ours, which the subtraction takes out)."""
    b = sh.side_levels(m, side, i_before) or {}
    q0 = float(b.get(p, 0.0))
    above0 = {pp for pp, q in b.items() if q >= 1.0 and pp > p}
    o0 = sh.opp99(m, side, i_before) or 0.0
    joined = ahead = locker = False
    qmin, qmax = q0, q0
    for i in range(i_from + 1, i_from + H + 1):
        lv = sh.side_levels(m, side, i)
        if lv is None:
            continue
        q = float(lv.get(p, 0.0))
        qmin, qmax = min(qmin, q), max(qmax, q)
        if any(qq >= 1.0 and pp > p and pp not in above0 for pp, qq in lv.items()):
            ahead = True
        o = sh.opp99(m, side, i)
        if o is not None and o >= o0 + 1.0:
            locker = True
    joined = qmax - q0 - added >= 10.0
    return {"joined": joined, "join_q": max(0.0, qmax - q0 - added), "ahead": ahead,
            "cancel": bool(q0 >= 10.0 and qmin - added <= 0.5 * q0), "locker": locker, "q0": q0}


def _agg_probes(rows: List[dict]) -> dict:
    out = {}
    for H in REACT_H:
        rr = [r[H] for r in rows if H in r]
        n = len(rr)
        if not n:
            continue
        cq = [r for r in rr if r["q0"] >= 10.0]
        out[H] = {"n": n, "joined": _r(sum(r["joined"] for r in rr) / n, 3),
                  "join_q_mean": _r(sum(r["join_q"] for r in rr) / n, 1),
                  "ahead": _r(sum(r["ahead"] for r in rr) / n, 3),
                  "cancel": _r(sum(r["cancel"] for r in cq) / len(cq), 3) if cq else None,
                  "locker": _r(sum(r["locker"] for r in rr) / n, 3)}
    return out


def our_reactions(event: str, model=None) -> dict:
    """Others' reactions after our live orders took effect: 1c pads / quals
    that opened an empty 1c level, and favorite joins (>= 50c) at a new
    price (placements and price amends)."""
    lo = scoring.load_live_orders(event)
    if not lo:
        raise ValueError(f"{event}: no live orders")
    sh = SW.shared_for(event, model, subtract=True)
    sh_full = SW.shared_for(event, model, subtract=False)
    plan = OrderPlan(event, sh, sh_full, lo)
    pads, favs = [], []
    for o in plan.orders.values():
        m, side = o["m"], o["side"]
        for k, (te, p) in enumerate(o["prices"]):
            tlog = o["placed_log"] if k == 0 else te
            i_b = sh.idx_at(tlog) - 1
            i_f = sh.idx_at(te + 1.0)
            if i_b < 0 or i_f + max(REACT_H) >= sh.n:
                continue
            cls = order_class(o["tag"], p)
            if p == 1 and cls == "c1":
                quiet = [sh.level1(m, side, ii) for ii in range(max(0, i_b - PAD_QUIET_S), i_b + 1)]
                quiet = [x for x in quiet if x is not None]
                if quiet and max(quiet) < 1.0:
                    pads.append({H: _probe(sh, m, side, 1, i_b, i_f, H) for H in REACT_H})
            elif cls in ("fav90", "fav_lo") and p >= 50:
                favs.append({H: _probe(sh, m, side, p, i_b, i_f, H) for H in REACT_H})
    return {"event": event, "pads": _agg_probes(pads), "favs": _agg_probes(favs),
            "n_pads": len(pads), "n_favs": len(favs)}


def baseline_reactions(event: str, model=None) -> dict:
    """The same probes after OTHER makers' comparable orders in an hour we
    did not trade: a fresh 1c pad (an empty longshot 1c level, side fair <=
    0.10, quiet PAD_QUIET_S, grows to >= 100 between two samples), and a
    favorite join (the reference level of a side with fair >= 0.97, priced
    >= 50c, grows by >= FAV_JOIN_MIN between two samples)."""
    sh = SW.shared_for(event, model, subtract=True)
    pads, favs = [], []
    hmax = max(REACT_H)
    for m in range(sh.nm):
        for side in ("yes", "no"):
            quiet_since = None
            prev = None
            for i in range(1, sh.n - hmax - 1):
                T = sh.sec0 + i + 0.5
                lv = sh.side_levels(m, side, i)
                if lv is None:
                    quiet_since, prev = None, None
                    continue
                f = sh.fair_or_mid(m, T)
                sp = None if f is None else (f if side == "yes" else 1.0 - f)
                q1 = float(lv.get(1, 0.0))
                if prev is not None and sp is not None:
                    pq1 = float(prev.get(1, 0.0))
                    if (sp <= 0.10 and pq1 < 1.0 and q1 >= 100.0 and quiet_since is not None
                            and i - quiet_since >= PAD_QUIET_S):
                        pads.append({H: _probe(sh, m, side, 1, i, i, H) for H in REACT_H})
                    if sp >= 0.97:
                        ref = pol.reference_price(prev)
                        if ref is not None and ref >= 50:
                            grow = float(lv.get(ref, 0.0)) - float(prev.get(ref, 0.0))
                            if grow >= FAV_JOIN_MIN:
                                favs.append({H: _probe(sh, m, side, ref, i - 1, i, H, grow)
                                             for H in REACT_H})
                if q1 < 1.0:
                    if quiet_since is None:
                        quiet_since = i
                else:
                    quiet_since = None
                prev = lv
    return {"event": event, "pads": pads, "favs": favs}


def reaction_report(event: str, baseline_events: Sequence[str], workers: int = 2) -> dict:
    ours = our_reactions(event)
    if workers > 1 and len(baseline_events) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(SW.MAX_WORKERS, workers),
                                 initializer=SW._worker_init) as ex:
            base = list(ex.map(_baseline_job, list(baseline_events)))
    else:
        base = [_baseline_job(e) for e in baseline_events]
    base = [b for b in base if b and not b.get("error")]
    bp = [r for b in base for r in b["pads"]]
    bf = [r for b in base for r in b["favs"]]
    # the live hour's 1c joiners in the book vs the hours around it
    return {"event": event, "ours": ours,
            "baseline": {"events": [b["event"] for b in base], "pads": _agg_probes(bp),
                         "favs": _agg_probes(bf), "n_pads": len(bp), "n_favs": len(bf)}}


def _baseline_job(ev):
    try:
        return baseline_reactions(ev)
    except Exception as e:                      # noqa: BLE001
        return {"event": ev, "error": f"{type(e).__name__}: {e}"}


def format_reaction(d: dict) -> str:
    o, b = d["ours"], d["baseline"]
    L = [f"market reaction to our live orders, {d['event']}, against other makers' comparable "
         f"orders in {len(b['events'])} hours we did not trade (others' size only: our orders "
         f"are subtracted from the book)"]
    for key, title, cols in (
            ("pads", "1c pads that opened an empty 1c level", ("joined", "join_q_mean", "locker")),
            ("favs", "favorite joins at a new price (>= 50c)", ("ahead", "joined", "cancel"))):
        L.append(f"{title}: ours n={o['n_' + key]}, baseline n={b['n_' + key]}")
        L.append(f"  {'within':>7s} " + " ".join(f"{c + ' ours':>16s} {c + ' base':>16s}" for c in cols))
        for H in REACT_H:
            ro, rb = (o[key] or {}).get(H) or {}, (b[key] or {}).get(H) or {}
            cells = []
            for c in cols:
                for r in (ro, rb):
                    v = r.get(c)
                    cells.append(f"{'-':>16s}" if v is None else
                                 (f"{v:16.1f}" if c == "join_q_mean" else f"{100 * v:15.0f}%"))
            L.append(f"  {str(H) + ' s':>7s} " + " ".join(cells))
    L.append("joined = others added >= 10 contracts at our price; join_q_mean = mean size added; "
             "locker = a new 99c bid opposite (it crosses a 1c bid); ahead = a new external bid "
             "above our price; cancel = the external size at our price before we came fell by "
             ">= 50%")
    return "\n".join(L)


# --------------------------------------------------------------------------
# The variants under opt / cons / swp / stress over every settled recorded hour
# --------------------------------------------------------------------------

STRESS_SENS = {"hole": {"stress_hole": True}, "size": {"stress_take": "size"},
               "nofav": {"fav_off": True}}


def stress_table(events: Sequence[str], out: str, variants: Sequence[str], draws: int = 6,
                 workers: int = 2, sens: Sequence[str] = ("hole", "size"),
                 hazard=None, tail=None, fav=None,
                 sens_variants: Optional[Sequence[str]] = None) -> dict:
    """temp_shadow_sweep.run_events for every fill model (opt, cons, swp,
    stress; <out>/runs) and each stress sensitivity (stress only, for
    sens_variants -- default the FAV variants; <out>/runs_<tag>), then the
    tables."""
    vs = {n: SW.SWEEP_VARIANTS[n] for n in variants}
    svs = {n: SW.SWEEP_VARIANTS[n] for n in (sens_variants if sens_variants is not None
                                             else [v for v in variants if v.startswith("FAV")])}
    hazard = hazard or SW.load_hazard(sim.SWP_HAZARD_REPO)
    tail = tail if tail is not None else SW.TailCal(C.read_json(sim.SWP_TAILS_REPO))
    fav = fav if fav is not None else SW.load_fav_hazard()
    base = SW.run_events(events, out, vs, draws, hazard, workers, tail=tail,
                         fill_models=SW.ALL_FILL_MODELS, fav_hazard=fav)
    tab = SW.variant_table(base)
    sens_tabs = {}
    for tag in sens:
        o = STRESS_SENS[tag]
        rr = SW.run_events(events, out, svs, draws, hazard, workers, tail=tail, tag=tag,
                           fill_models=(SW.STRESS,), fav_hazard=None if o.get("fav_off") else fav,
                           fav_off=bool(o.get("fav_off")), stress_hole=bool(o.get("stress_hole")),
                           stress_take=o.get("stress_take", "full"))
        sens_tabs[tag] = SW.variant_table(rr)
    errs = [r.get("event") for r in base if r.get("error")]
    return {"table": tab, "sens": sens_tabs, "variants": list(variants), "errors": errs,
            "events": len(events), "hazard_id": hazard.ident(), "fav_hazard_id": fav.ident()}


def format_stress_table(d: dict) -> str:
    vs = d["variants"]
    L = [SW.format_table(d["table"], vs), ""]
    t = d["table"]["table"]
    L.append("stress sensitivities (totals; base = level-clear takes our lots whole + hidden "
             "favorite takers; hole = + the gap rule; size = a clearing taker takes only up to its "
             "own size; nofav = without the hidden favorite takers):")
    L.append(f"  {'variant':16s} {'cons':>8s} {'swp':>8s} {'stress':>8s} "
             + " ".join(f"{k:>8s}" for k in d["sens"]) + f" {'stress ex-b&w':>14s} {'P>0':>5s}")
    for n in vs:
        cells = []
        for fm in ("cons", "swp", SW.STRESS):
            v = t.get(f"{n}|{fm}")
            cells.append(f"{v['total'].get('net', 0):8.0f}" if v else f"{'-':>8s}")
        for k, st in d["sens"].items():
            v = (st.get("table") or {}).get(f"{n}|stress")
            cells.append(f"{v['total'].get('net', 0):8.0f}" if v else f"{'-':>8s}")
        v = t.get(f"{n}|stress") or {}
        L.append(f"  {n:16s} " + " ".join(cells)
                 + f" {(v.get('ex_best_worst') or 0):14.0f} {(v.get('p_pos') or 0):5.2f}")
    for n in vs:
        if not n.startswith("FAV_only"):
            continue
        for fm in ("cons", SW.STRESS):
            hh = d["table"]["by_hour"].get(f"{n}|{fm}") or {}
            if hh:
                L.append("")
                L.append(SW.format_hours(d["table"], f"{n}|{fm}"))
    if d.get("errors"):
        L.append(f"events that failed: {d['errors']}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_stress_table(a) -> int:
    days = a.day
    evs = SW._day_settled_events(days) if days else SW.recorded_events(None)
    vs = [v for v in (a.variants.split(",") if a.variants else SW.STRESS_TABLE_VARIANTS) if v]
    sens = [x for x in (a.sens.split(",") if a.sens is not None else ("hole", "size")) if x]
    out = os.path.join(a.out, "stress")
    print(f"{len(evs)} settled recorded hours, variants {vs}, sensitivities {sens}", flush=True)
    d = stress_table(evs, out, vs, a.draws, a.workers, sens)
    os.makedirs(a.out, exist_ok=True)
    C.write_json_atomic(os.path.join(a.out, "stress_table.json"), d)
    txt = format_stress_table(d)
    with open(os.path.join(a.out, "stress_table.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return 1 if d.get("errors") else 0


def cmd_reaction(a) -> int:
    evs = a.event or SW.live_events()
    base = a.baseline or [e for e in SW.recorded_events(None) if e < min(evs)]
    os.makedirs(a.out, exist_ok=True)
    for ev in evs:
        print(f"reactions on {ev} against {len(base)} baseline hours ...", flush=True)
        d = reaction_report(ev, base, workers=a.workers)
        C.write_json_atomic(os.path.join(a.out, f"reaction_{ev}.json"), d)
        txt = format_reaction(d)
        with open(os.path.join(a.out, f"reaction_{ev}.txt"), "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(txt)
    return 0


def cmd_clears(a) -> int:
    evs = a.events or SW.recorded_events(a.days)
    prints = C.read_json(a.prints) if a.prints else None
    print(f"favorite-level clears over {len(evs)} recorded hours ...", flush=True)
    d = clears_report(evs, prints, workers=a.workers)
    os.makedirs(a.out, exist_ok=True)
    C.write_json_atomic(os.path.join(a.out, "clears.json"), d)
    txt = format_clears(d)
    with open(os.path.join(a.out, "clears.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return 0


def cmd_audit(a) -> int:
    hz = SW.load_hazard(a.hazard or sim.SWP_HAZARD_REPO)
    fav = None if a.no_favhaz else SW.load_fav_hazard(a.favhazard)
    os.makedirs(a.out, exist_ok=True)
    evs = a.event or SW.live_events()
    for ev in evs:
        print(f"auditing {ev} ...", flush=True)
        doc = audit_event(ev, a.draws, hz, fav, latency=not a.no_latency)
        C.write_json_atomic(os.path.join(a.out, f"audit_{ev}.json"), doc)
        txt = format_audit(doc)
        with open(os.path.join(a.out, f"audit_{ev}.txt"), "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(txt)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="order-level fill audit + stress analyses")
    ap.add_argument("--out", default=None, help="output dir (default env TEMP_SHADOW_AUDIT_OUT "
                                                "or <TEMP_SHADOW_DATA>/audit)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("audit", help="the live executor's actual orders under every fill model")
    p.add_argument("--event", nargs="*", default=None, help="default: every settled live hour")
    p.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
    p.add_argument("--hazard", default=None, help="1c hazard table (default: the repo's)")
    p.add_argument("--favhazard", default=None, help="favorite hazard table (default: the repo's)")
    p.add_argument("--no-favhaz", action="store_true")
    p.add_argument("--no-latency", action="store_true", help="effective times = logged times")
    c = sub.add_parser("clears", help="favorite-level clears and the upset rate after them")
    c.add_argument("--events", nargs="*", default=None)
    c.add_argument("--days", nargs="*", default=None)
    c.add_argument("--prints", default=None, help="cached prints JSON {backfill, recorded} "
                                                 "(else recomputed from the tapes)")
    c.add_argument("--workers", type=int, default=2)
    r = sub.add_parser("reaction", help="did other makers react to our live orders?")
    r.add_argument("--event", nargs="*", default=None, help="default: every settled live hour")
    r.add_argument("--baseline", nargs="*", default=None,
                   help="hours we did not trade (default: every recorded hour before the first)")
    r.add_argument("--workers", type=int, default=2)
    t = sub.add_parser("stress-table", help="the variants under opt / cons / swp / stress over "
                                            "every settled recorded hour")
    t.add_argument("--day", nargs="*", default=None, help="ET days (default: every recorded day)")
    t.add_argument("--variants", default=None,
                   help="comma list of temp_shadow_sweep.SWEEP_VARIANTS (default: "
                        + ",".join(SW.STRESS_TABLE_VARIANTS) + ")")
    t.add_argument("--draws", type=int, default=6)
    t.add_argument("--workers", type=int, default=2)
    t.add_argument("--sens", default=None, help="comma list of " + ",".join(STRESS_SENS)
                                                + " (default hole,size; '' = none)")
    a = ap.parse_args(argv)
    a.out = a.out or out_dir_default()
    return {"audit": cmd_audit, "clears": cmd_clears, "reaction": cmd_reaction,
            "stress-table": cmd_stress_table}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
