#!/usr/bin/env python3
r"""imm_allocation.py -- where the next dollar of size should go: evidence
section 14 of the daily review (Jack 2026-11-26: "Yes, build it", to "Build
the capital-allocation proposal next?").

Size is set by hand, one family at a time (x2, x3, x5 ...), on a family's
AVERAGE return. What moves money is the MARGINAL one -- what the next (or the
last) $1k of resting size earns -- and an average overstates it. This
section measures both and proposes only what clears the bar:

  1. FAMILY ECONOMICS  moat/family_daily.csv, the last WINDOW_DAYS ET days:
     $ resting, rewards (MODELLED: the dashboard's estimate), trading
     (MEASURED at the last trade), net per $1k resting a day, our pool share
     (the bot's est_frac, pool-weighted, from the cycle log).
  2. THE MARGIN, MEASURED  the change registry's readouts (changes.jsonl):
     every change after which a scope's resting $ moved MOVE_MIN x or more,
     with the programs' supply inside SUPPLY_BAND. Adds and cuts apart: the
     proportional prediction from the 7 days before against the 7 days
     after, for rewards and for trading, BY FAMILY (each readout's series).
     The 11/26 back-fill (30 days):
       - 8 ELECTION adds earned -$612/day of rewards against +$1,586
         predicted. Those programs were ending (R-261124-02's re-check), and
         the supply check missed it.
       - The 12 adds in every other family earned x0.47 of the predicted
         rewards and x0.37 of the trading: marginal net per $1k +2.03,
         against their +3.58 average.
       - Cuts moved about as predicted.
  3. THE MODEL  a size step k on a family: rewards by the bot's share model,
     rew x k / (k s + 1 - s) with s our share, and trading in proportion.
     Each is scaled by the family's own calibration:
       - each readout's measured / predicted ratio, kept in CAL_CLIP (more
         size cannot cost rewards or trading volume);
       - the family's own mean, shrunk toward the mean of every OTHER
         family's readouts by SHRINK_N readouts, so a family with none takes
         the book's;
       - every readout weighs the same. The first build (186f8da) pooled by
         dollars, and 8 election readouts zeroed every family's adds.
     Days and readouts are resampled together: p10 / p50 / p90 of the change
     in net $ a day.

PROPOSALS (at most MAX_PROPOSALS): a CUT or an ADD whose p10 gain is above
zero and whose p50 is MIN_GAIN $/day or more, on a family that no
registered change touched in the last RECENT_DAYS (such a family waits for
that change's readout). Where no add qualifies, the best-measured family
(net per $1k p10 above zero, t >= EXP_MIN_T) is offered as an add
EXPERIMENT: an EXP_STEP step, pre-registered, read out at 7 days.

Every run appends what it proposed to moat/allocation.jsonl (kept forever,
once a day; a "none" row when nothing clears the bar). The review files the
proposals it takes as "allocation" findings, and Jack approves by text. The
registry reads each shipped one out at 7 days, and that readout joins the
track record above, so the model calibrates itself.

    python imm_allocation.py [--day YYYY-MM-DD]    # print the section (read-only: no log row)

IMM_REVIEW_ALLOCATION=0 turns the section off. Read-only on the bot.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import imm_review as rv  # noqa: E402

WINDOW_DAYS = 30
MIN_DAYS = 10                 # a family on fewer days of the window is not proposed
STEP_UP, STEP_DOWN = 1.5, 1 / 1.5
EXP_STEP = 1.25               # an add experiment's step
EXP_MIN_T = 2.0
MOVE_MIN = 1.3                # a readout counts when the scope's resting $ moved this much
SUPPLY_BAND = (0.8, 1.25)     # ... with the programs' supply inside this
MIN_REST = 1000.0             # ... from at least this much resting $ a day
DUP_TOL = 0.01                # two readouts within 1% before and after are the same money
TRACK_DAYS = 120              # readouts whose split is this recent
CAL_MIN_N = 5                 # fewer readouts: that direction is uncalibrated (ratio 1)
CAL_CLIP = (0.0, 2.0)
SHRINK_N = 5.0                # a family's own readouts weigh against this many of the book's
RATIO_MIN_PRED = 1.0          # $/day: a readout predicting less has no ratio of its own
BOOT = 1000
MIN_GAIN = 5.0                # $/day
MAX_PROPOSALS = 3
RECENT_DAYS = 7
ALLOC_LOG = "allocation.jsonl"


# ---------------------------------------------------------------------------
# 1. family economics
# ---------------------------------------------------------------------------

def window_days(day: date) -> Tuple[List[str], Dict[str, Dict[str, List[float]]]]:
    """(the window's ET days, {family: {"rew", "pnl", "rest", "share", "cts"}:
    one value per window day, 0 where the family was absent, share None}).
    The days are the book's: a family present on 10 of 30 days is averaged
    over 30, so its $/day is per calendar day."""
    lo = (day - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    hi = day.isoformat()
    rows = [r for r in rv._read_rows(os.path.join(rv.MOAT_DIR, "family_daily.csv"))
            if lo <= str(r.get("day") or "") <= hi]
    days = sorted({r["day"] for r in rows})
    pos = {d: i for i, d in enumerate(days)}
    fams: Dict[str, Dict[str, list]] = {}
    for r in rows:
        f = fams.setdefault(r["family"], {"rew": [0.0] * len(days), "pnl": [0.0] * len(days),
                                          "rest": [0.0] * len(days), "cts": [0.0] * len(days),
                                          "share": [None] * len(days)})
        i = pos[r["day"]]
        for k in ("rew", "pnl", "rest", "cts"):
            f[k][i] = rv._f(r.get(k))
        if r.get("share") not in (None, ""):
            f["share"][i] = rv._f(r.get("share"))
    return days, fams


def _per1k(rew: float, pnl: float, rest: float) -> Optional[float]:
    return 1000.0 * (rew + pnl) / rest if rest > 0 else None


def family_stats(days: List[str], fams: Dict[str, dict]) -> Dict[str, dict]:
    n = len(days) or 1
    tot_rest = sum(sum(v["rest"]) for v in fams.values())
    out = {}
    for f, v in fams.items():
        present = sum(1 for x in v["rest"] if x > 0)
        nets = [a + b for a, b in zip(v["rew"], v["pnl"])]
        mean = sum(nets) / n
        sd = (sum((x - mean) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else 0.0
        sh = [(s, r) for s, r in zip(v["share"], v["rest"]) if s is not None and r > 0]
        w = sum(r for _, r in sh)
        rest = sum(v["rest"]) / n
        out[f] = {"days": present, "rest": rest, "rew": sum(v["rew"]) / n, "pnl": sum(v["pnl"]) / n,
                  "net": mean, "cts": sum(v["cts"]) / n,
                  "rest_share": sum(v["rest"]) / tot_rest if tot_rest > 0 else None,
                  "net_per_1k": _per1k(sum(v["rew"]), sum(v["pnl"]), sum(v["rest"])),
                  "trade_c": rv.cpc(sum(v["pnl"]), sum(v["cts"])),
                  "t": round(mean / (sd / n ** 0.5), 2) if sd > 0 else None,
                  "share": round(sum(s * r for s, r in sh) / w, 5) if w > 0 else None,
                  "share_days": len(sh)}
    return out


# ---------------------------------------------------------------------------
# 2. the margin, measured: the registry's readouts
# ---------------------------------------------------------------------------

def _same(a: dict, b: dict) -> bool:
    return (abs(a["rest_b"] / b["rest_b"] - 1) <= DUP_TOL and abs(a["rest_a"] / b["rest_a"] - 1) <= DUP_TOL)


def _ratio(measured: float, predicted: float) -> Optional[float]:
    if abs(predicted) < 1e-9:
        return None
    return min(max(measured / predicted, CAL_CLIP[0]), CAL_CLIP[1])


def capital_moves(day: date) -> List[dict]:
    """The registry's complete readouts in which the scope's resting $ moved
    MOVE_MIN x or more with the programs' supply steady, one per money (a
    readout read again replaces the earlier; near-identical scopes count
    once)."""
    subj: Dict[str, str] = {}
    latest: Dict[tuple, dict] = {}
    for r in rv._iter_jsonl(os.path.join(rv.REVIEW_DIR, "changes.jsonl")):
        op = r.get("op")
        if op == "register" and r.get("key"):
            subj[r["key"]] = str(r.get("subject") or "")
        elif op == "readout" and isinstance(r.get("result"), dict):
            res = r["result"]
            latest[(json.dumps(res.get("scope_key"), sort_keys=True, default=str), res.get("split"),
                    res.get("horizon"))] = r
    lo = (day - timedelta(days=TRACK_DAYS)).isoformat()
    out: List[dict] = []
    for r in sorted(latest.values(), key=lambda x: str((x.get("result") or {}).get("split") or "")):
        res = r["result"]
        b, a = res.get("before") or {}, res.get("after") or {}
        if res.get("book") or not res.get("complete") or str(res.get("split") or "") < lo \
                or str(res.get("split") or "") > day.isoformat():
            continue
        rb, ra, sup = rv._f(b.get("rest")), rv._f(a.get("rest")), res.get("supply_ratio")
        if rb < MIN_REST or ra <= 0 or sup is None or not SUPPLY_BAND[0] <= rv._f(sup) <= SUPPLY_BAND[1]:
            continue
        k = ra / rb
        if abs(math.log(k)) < math.log(MOVE_MIN):
            continue
        series = list(res.get("series_sample") or [])[:6]
        fams = defaultdict(int)
        for s in series:
            fams[rv.family_of(s)[0]] += 1
        it = {"split": res.get("split"), "scope": str(res.get("scope") or "")[:80], "k": round(k, 3),
              "supply": sup, "rest_b": rb, "rest_a": ra,
              "rew_b": rv._f(b.get("rew")), "rew_a": rv._f(a.get("rew")),
              "pnl_b": rv._f(b.get("pnl")), "pnl_a": rv._f(a.get("pnl")),
              "series": series, "family": max(sorted(fams), key=lambda f: fams[f]) if fams else None,
              "subjects": [subj.get(x, "")[:110] for x in (r.get("keys") or res.get("keys") or [])][:2]}
        it["r_rew"], it["r_pnl"] = readout_ratios(it)
        if not any(_same(it, o) for o in out):
            out.append(it)
    return out


def readout_ratios(m: dict) -> Tuple[Optional[float], Optional[float]]:
    """One readout's measured / predicted change, for rewards and for trading,
    kept in CAL_CLIP; None where it predicted under RATIO_MIN_PRED $/day."""
    pr, pp = m["rew_b"] * (m["k"] - 1), m["pnl_b"] * (m["k"] - 1)
    return (_ratio(m["rew_a"] - m["rew_b"], pr) if abs(pr) >= RATIO_MIN_PRED else None,
            _ratio(m["pnl_a"] - m["pnl_b"], pp) if abs(pp) >= RATIO_MIN_PRED else None)


def family_calibration(moves: List[dict], families) -> Dict[str, Tuple[float, float]]:
    """{family: (c_rew, c_pnl)} for one direction.
    - Each readout is one vote: its ratio, clipped.
    - A family's own mean is shrunk toward the mean of every OTHER family's
      readouts by SHRINK_N readouts; a family with none takes the book's mean.
    - Equal votes, so one big wave cannot set every family's ratio: in the
      first build's dollar-weighted pool, 8 election readouts zeroed all the
      adds.
    - (1.0, 1.0), the proportional model, while the direction has fewer than
      CAL_MIN_N readouts."""
    want = set(families) | {m.get("family") or "?" for m in moves}
    if len(moves) < CAL_MIN_N:
        return {f: (1.0, 1.0) for f in want}
    S = ({}, {})
    N = ({}, {})
    for m in moves:
        f = m.get("family") or "?"
        for j, r in enumerate((m["r_rew"], m["r_pnl"])):
            if r is not None:
                S[j][f] = S[j].get(f, 0.0) + r
                N[j][f] = N[j].get(f, 0) + 1
    tot = [(sum(S[j].values()), sum(N[j].values())) for j in (0, 1)]
    out = {}
    for f in want:
        cs = []
        for j in (0, 1):
            s_f, n_f = S[j].get(f, 0.0), N[j].get(f, 0)
            s_o, n_o = tot[j][0] - s_f, tot[j][1] - n_f
            pool = s_o / n_o if n_o else (s_f / n_f if n_f else 1.0)
            cs.append(round((s_f + SHRINK_N * pool) / (n_f + SHRINK_N), 4))
        out[f] = (cs[0], cs[1])
    return out


def direction(moves: List[dict], cal: Dict[str, Tuple[float, float]]) -> dict:
    """Predicted (proportional, from the before-window) against measured, for
    one direction's readouts: the dollar totals for the record, and by family
    the readouts' own ratios beside the calibration the model uses."""
    n = len(moves)
    S = defaultdict(float)
    by: Dict[str, dict] = {}
    for i in moves:
        S["pred_rew"] += i["rew_b"] * (i["k"] - 1)
        S["meas_rew"] += i["rew_a"] - i["rew_b"]
        S["pred_pnl"] += i["pnl_b"] * (i["k"] - 1)
        S["meas_pnl"] += i["pnl_a"] - i["pnl_b"]
        S["d_rest"] += i["rest_a"] - i["rest_b"]
        S["rest_b"] += i["rest_b"]
        S["net_b"] += i["rew_b"] + i["pnl_b"]
        f = i.get("family") or "?"
        b = by.setdefault(f, {"family": f, "n": 0, "d_rest": 0.0, "pred_rew": 0.0, "meas_rew": 0.0,
                              "r_rew": [], "r_pnl": []})
        b["n"] += 1
        b["d_rest"] += i["rest_a"] - i["rest_b"]
        b["pred_rew"] += i["rew_b"] * (i["k"] - 1)
        b["meas_rew"] += i["rew_a"] - i["rew_b"]
        for key in ("r_rew", "r_pnl"):
            if i[key] is not None:
                b[key].append(i[key])
    fams = []
    for b in sorted(by.values(), key=lambda b: -abs(b["d_rest"])):
        c = cal.get(b["family"]) or (1.0, 1.0)
        fams.append({"family": b["family"], "n": b["n"], "d_rest": round(b["d_rest"], 0),
                     "pred_rew": round(b["pred_rew"], 2), "meas_rew": round(b["meas_rew"], 2),
                     "own_rew": round(sum(b["r_rew"]) / len(b["r_rew"]), 3) if b["r_rew"] else None,
                     "own_pnl": round(sum(b["r_pnl"]) / len(b["r_pnl"]), 3) if b["r_pnl"] else None,
                     "c_rew": c[0], "c_pnl": c[1]})
    pool = {j: [m[j] for m in moves if m[j] is not None] for j in ("r_rew", "r_pnl")}
    return {"n": n, "calibrated": n >= CAL_MIN_N,
            "pred_rew": round(S["pred_rew"], 2), "meas_rew": round(S["meas_rew"], 2),
            "pred_pnl": round(S["pred_pnl"], 2), "meas_pnl": round(S["meas_pnl"], 2),
            "d_rest": round(S["d_rest"], 2),
            "pool_rew": round(sum(pool["r_rew"]) / len(pool["r_rew"]), 3) if pool["r_rew"] else None,
            "pool_pnl": round(sum(pool["r_pnl"]) / len(pool["r_pnl"]), 3) if pool["r_pnl"] else None,
            "marginal_per_1k": (round(1000 * (S["meas_rew"] + S["meas_pnl"]) / S["d_rest"], 2)
                                if S["d_rest"] else None),
            "average_per_1k": round(1000 * S["net_b"] / S["rest_b"], 2) if S["rest_b"] > 0 else None,
            "by_family": fams,
            "moves": [{k: v for k, v in m.items() if k not in ("r_rew", "r_pnl")}
                      for m in sorted(moves, key=lambda i: -abs(i["rest_a"] - i["rest_b"]))[:6]]}


# ---------------------------------------------------------------------------
# 3. the model, and what changed lately
# ---------------------------------------------------------------------------

def gain(rew: float, pnl: float, share: Optional[float], k: float, c_rew: float, c_pnl: float) -> float:
    """The change in net $/day from a size step k: rewards by the share model,
    trading in proportion, each scaled by its calibration."""
    s = min(max(share or 0.0, 0.0), 0.95)
    return c_rew * rew * (k / (k * s + 1 - s) - 1) + c_pnl * pnl * (k - 1)


def recent_changes(day: date) -> Dict[str, List[dict]]:
    """{family: [{"day", "key", "subject"}]}: registered changes that went live
    in the RECENT_DAYS ET days to `day` and name the family or its series
    (a83dab0, the 11/25 Carbon Arc unwinds, is scoped by family). Book-wide
    changes are not counted: one lands almost every day, and counting them
    would hold every family forever."""
    s, _ = rv.et_bounds(day - timedelta(days=RECENT_DAYS - 1))
    _, e = rv.et_bounds(day)
    reg: Dict[str, dict] = {}
    for r in rv._iter_jsonl(os.path.join(rv.REVIEW_DIR, "changes.jsonl")):
        k = r.get("key")
        if not k:
            continue
        if r.get("op") == "register":
            reg[k] = dict(r)
        elif k in reg and r.get("op") == "live" and r.get("live_at") is not None:
            reg[k]["live_at"] = r["live_at"]
        elif k in reg and r.get("op") == "scope" and isinstance(r.get("scope"), dict):
            reg[k]["scope"] = r["scope"]
    out: Dict[str, List[dict]] = defaultdict(list)
    for k, r in reg.items():
        ts = r.get("live_at")
        if ts is None or not s <= rv._f(ts) < e:
            continue
        sc = r.get("scope") or {}
        fams = {rv.family_of(x)[0] for x in (sc.get("series") or [])} | {str(x) for x in (sc.get("families") or [])}
        for f in sorted(fams):
            out[f].append({"day": datetime.fromtimestamp(rv._f(ts), rv.ET).date().isoformat(), "key": k,
                           "subject": str(r.get("subject") or "")[:110]})
    return dict(out)


def _pct(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def _cal_draws(moves: List[dict], families, rnd: random.Random) -> List[Dict[str, Tuple[float, float]]]:
    """BOOT per-family calibrations from the direction's readouts resampled
    (family_calibration on each resample); (1.0, 1.0) for every family while
    the direction is uncalibrated."""
    fams = list(families)
    if len(moves) < CAL_MIN_N:
        one = {f: (1.0, 1.0) for f in fams}
        return [one] * BOOT
    return [family_calibration([moves[rnd.randrange(len(moves))] for _ in moves], fams)
            for _ in range(BOOT)]


def _q(xs: List[float]) -> dict:
    return {"p10": round(_pct(xs, 0.10), 2), "p50": round(_pct(xs, 0.50), 2), "p90": round(_pct(xs, 0.90), 2),
            "p_gain": round(sum(1 for x in xs if x > 0) / len(xs), 3)}


def sec_allocation(day: date, log: bool = True) -> dict:
    """Evidence section 14. `log` appends the day's proposals to
    moat/allocation.jsonl (once a day)."""
    days, fams = window_days(day)
    stats = family_stats(days, fams)
    moves = capital_moves(day)
    adds_m = [m for m in moves if m["k"] > 1]
    cuts_m = [m for m in moves if m["k"] < 1]
    names = list(stats)
    cal_pt = {"add": family_calibration(adds_m, names), "cut": family_calibration(cuts_m, names)}
    adds, cuts = direction(adds_m, cal_pt["add"]), direction(cuts_m, cal_pt["cut"])
    rnd = random.Random(day.toordinal())         # the same day gives the same intervals
    n = len(days)
    idx = [[rnd.randrange(n) for _ in range(n)] for _ in range(BOOT)] if n else []
    cal = {"add": _cal_draws(adds_m, names, rnd), "cut": _cal_draws(cuts_m, names, rnd)}
    recent = recent_changes(day)
    rows, cands = [], []
    for f, st in sorted(stats.items(), key=lambda kv: -kv[1]["rest"]):
        v = fams[f]
        boot = {"cut": [], "add": [], "net_per_1k": []}
        for b, ix in enumerate(idx):
            rew = sum(v["rew"][i] for i in ix) / n
            pnl = sum(v["pnl"][i] for i in ix) / n
            rest = sum(v["rest"][i] for i in ix) / n
            if rest > 0:
                boot["net_per_1k"].append(1000.0 * (rew + pnl) / rest)
            for kind, k in (("cut", STEP_DOWN), ("add", STEP_UP)):
                c_rew, c_pnl = cal[kind][b][f]
                boot[kind].append(gain(rew, pnl, st["share"], k, c_rew, c_pnl))
        row = dict(st, family=f, recent=recent.get(f) or [],
                   cal={"add": cal_pt["add"][f], "cut": cal_pt["cut"][f]},
                   cut=_q(boot["cut"]) if boot["cut"] else None,
                   add=_q(boot["add"]) if boot["add"] else None,
                   net_per_1k_q=_q(boot["net_per_1k"]) if boot["net_per_1k"] else None)
        rows.append(row)
        if st["days"] < MIN_DAYS:
            continue
        for kind, k in (("cut", STEP_DOWN), ("add", STEP_UP)):
            q = row[kind]
            if q and q["p10"] > 0 and q["p50"] >= MIN_GAIN:
                cands.append({"family": f, "kind": kind, "k": round(k, 3), **q,
                              "capital": round(st["rest"] * (k - 1), 0), "rest": round(st["rest"], 0),
                              "net_per_1k": st["net_per_1k"], "t": st["t"], "share": st["share"],
                              "days": st["days"], "recent": row["recent"],
                              "cal": {"rew": cal_pt[kind][f][0], "pnl": cal_pt[kind][f][1]}})
    waits = [c for c in cands if c["recent"]]
    props = sorted((c for c in cands if not c["recent"]), key=lambda c: -c["p50"])[:MAX_PROPOSALS]
    experiment = None
    if not any(p["kind"] == "add" for p in props):
        best = [r for r in rows if r["days"] >= MIN_DAYS and not r["recent"] and r.get("net_per_1k_q")
                and r["net_per_1k_q"]["p10"] > 0 and (r.get("t") or 0) >= EXP_MIN_T]
        if best:
            r = max(best, key=lambda r: r["net_per_1k_q"]["p10"])
            ca, cp = cal_pt["add"][r["family"]]
            experiment = {"family": r["family"], "kind": "experiment", "k": EXP_STEP,
                          "cal": {"rew": ca, "pnl": cp},
                          "capital": round(r["rest"] * (EXP_STEP - 1), 0), "rest": round(r["rest"], 0),
                          "net_per_1k": r["net_per_1k"], "net_per_1k_p10": r["net_per_1k_q"]["p10"],
                          "t": r["t"], "share": r["share"],
                          "proportional": round(gain(r["rew"], r["pnl"], r["share"], EXP_STEP, 1.0, 1.0), 2),
                          "calibrated": round(gain(r["rew"], r["pnl"], r["share"], EXP_STEP, ca, cp), 2)}
    tot = {k: sum(r[k] for r in rows) for k in ("rest", "rew", "pnl")}
    out = {"window": {"days": n, "from": days[0] if days else None, "to": days[-1] if days else None},
           "book": {"rest": round(tot["rest"], 0), "net": round(tot["rew"] + tot["pnl"], 2),
                    "net_per_1k": _per1k(tot["rew"], tot["pnl"], tot["rest"]),
                    "rew_per_1k": (1000.0 * tot["rew"] / tot["rest"] if tot["rest"] > 0 else None)},
           "track": {"adds": adds, "cuts": cuts, "moves": len(moves)},
           "steps": {"cut": STEP_DOWN, "add": STEP_UP, "experiment": EXP_STEP},
           "families": rows[:20], "proposals": props, "experiment": experiment, "waits": waits[:6],
           "rules": {"min_gain": MIN_GAIN, "recent_days": RECENT_DAYS, "min_days": MIN_DAYS,
                     "move_min": MOVE_MIN, "supply_band": list(SUPPLY_BAND)}}
    out["logged"] = log_day(day, out) if log else 0
    return out


def log_day(day: date, sec: dict) -> int:
    """Append the day's proposals (or one "none" row) to moat/allocation.jsonl,
    once per day: the track record of what the model said, kept forever."""
    path = os.path.join(rv.MOAT_DIR, ALLOC_LOG)
    iso = day.isoformat()
    if any(r.get("day") == iso for r in rv._iter_jsonl(path)):
        return 0
    book = {k: {x: sec["track"][k].get(x) for x in ("n", "calibrated", "pool_rew", "pool_pnl", "marginal_per_1k")}
            for k in ("adds", "cuts")}
    rows = [dict({k: v for k, v in p.items() if k != "recent"}, day=iso, book_cal=book) for p in sec["proposals"]]
    if sec.get("experiment"):
        rows.append(dict(sec["experiment"], day=iso, book_cal=book))
    if not rows:
        rows = [{"day": iso, "kind": "none", "book_cal": book, "families": len(sec["families"])}]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    return len(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day", help="ET day (default: yesterday)")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.day) if a.day else (datetime.now(rv.ET).date() - timedelta(days=1))
    sec = sec_allocation(day, log=False)
    L: List[str] = []
    rv.render_allocation(sec, L.append)
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
