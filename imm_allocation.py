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
     after, for rewards and for trading. The 11/26 back-fill (30 days): 21
     adds (+$927k resting) earned -$511/day of rewards against +$2,686
     predicted (marginal net per $1k -0.66; the average before +2.16), while
     6 cuts lost rewards and saved trading as predicted (-$604 vs -$632 and
     +$727 vs +$739). The proportional model holds for cuts, not for adds.
  3. THE MODEL  a size step k on a family: rewards by the bot's share model,
     rew x k / (k s + 1 - s) with s our share, and trading in proportion,
     each scaled by its direction's measured realized / predicted ratio, kept
     in CAL_CLIP (more size cannot cost rewards or trading volume: a negative
     add ratio is confounding, not mechanism). Days and readouts are
     resampled together: p10 / p50 / p90 of the change in net $ a day.

PROPOSALS (at most MAX_PROPOSALS): a CUT or an ADD whose p10 gain is above
zero and whose p50 is MIN_GAIN $/day or more, on a family that no
registered change touched in the last RECENT_DAYS (such a family waits for
that change's readout). With adds measured near zero at the margin, an add
rarely qualifies, so the best-measured family (net per $1k p10 above zero,
t >= EXP_MIN_T) is offered as an add EXPERIMENT instead: an EXP_STEP step,
pre-registered, read out at 7 days.

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


def _c(x: Optional[float]) -> float:
    """A calibration ratio, 1.0 (the proportional model) where there is none.
    0.0 is a real ratio (the next dollar earns nothing): never `x or 1.0`."""
    return 1.0 if x is None else x


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
        it = {"split": res.get("split"), "scope": str(res.get("scope") or "")[:80], "k": round(k, 3),
              "supply": sup, "rest_b": rb, "rest_a": ra,
              "rew_b": rv._f(b.get("rew")), "rew_a": rv._f(a.get("rew")),
              "pnl_b": rv._f(b.get("pnl")), "pnl_a": rv._f(a.get("pnl")),
              "series": list(res.get("series_sample") or [])[:6],
              "subjects": [subj.get(x, "")[:110] for x in (r.get("keys") or res.get("keys") or [])][:2]}
        if not any(_same(it, o) for o in out):
            out.append(it)
    return out


def direction(moves: List[dict]) -> dict:
    """Predicted (proportional, from the before-window) against measured, for
    one direction's readouts; the calibration ratios once CAL_MIN_N exist."""
    n = len(moves)
    S = defaultdict(float)
    for i in moves:
        S["pred_rew"] += i["rew_b"] * (i["k"] - 1)
        S["meas_rew"] += i["rew_a"] - i["rew_b"]
        S["pred_pnl"] += i["pnl_b"] * (i["k"] - 1)
        S["meas_pnl"] += i["pnl_a"] - i["pnl_b"]
        S["d_rest"] += i["rest_a"] - i["rest_b"]
        S["rest_b"] += i["rest_b"]
        S["net_b"] += i["rew_b"] + i["pnl_b"]
    cal = n >= CAL_MIN_N
    return {"n": n, "calibrated": cal,
            "pred_rew": round(S["pred_rew"], 2), "meas_rew": round(S["meas_rew"], 2),
            "pred_pnl": round(S["pred_pnl"], 2), "meas_pnl": round(S["meas_pnl"], 2),
            "d_rest": round(S["d_rest"], 2),
            "c_rew": (_ratio(S["meas_rew"], S["pred_rew"]) if cal else None),
            "c_pnl": (_ratio(S["meas_pnl"], S["pred_pnl"]) if cal else None),
            "marginal_per_1k": (round(1000 * (S["meas_rew"] + S["meas_pnl"]) / S["d_rest"], 2)
                                if S["d_rest"] else None),
            "average_per_1k": round(1000 * S["net_b"] / S["rest_b"], 2) if S["rest_b"] > 0 else None,
            "moves": sorted(moves, key=lambda i: -abs(i["rest_a"] - i["rest_b"]))[:6]}


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


def _cal_draws(moves: List[dict], d: dict, rnd: random.Random) -> List[Tuple[float, float]]:
    """BOOT (c_rew, c_pnl) pairs: the direction's readouts resampled, or 1.0
    while it is uncalibrated."""
    if not d["calibrated"]:
        return [(1.0, 1.0)] * BOOT
    out = []
    for _ in range(BOOT):
        pick = [moves[rnd.randrange(len(moves))] for _ in moves]
        pr = sum(i["rew_b"] * (i["k"] - 1) for i in pick)
        mr = sum(i["rew_a"] - i["rew_b"] for i in pick)
        pp = sum(i["pnl_b"] * (i["k"] - 1) for i in pick)
        mp = sum(i["pnl_a"] - i["pnl_b"] for i in pick)
        cr, cp = _ratio(mr, pr), _ratio(mp, pp)
        out.append((_c(d["c_rew"]) if cr is None else cr, _c(d["c_pnl"]) if cp is None else cp))
    return out


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
    adds, cuts = direction(adds_m), direction(cuts_m)
    rnd = random.Random(day.toordinal())         # the same day gives the same intervals
    n = len(days)
    idx = [[rnd.randrange(n) for _ in range(n)] for _ in range(BOOT)] if n else []
    cal = {"add": _cal_draws(adds_m, adds, rnd), "cut": _cal_draws(cuts_m, cuts, rnd)}
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
                c_rew, c_pnl = cal[kind][b]
                boot[kind].append(gain(rew, pnl, st["share"], k, c_rew, c_pnl))
        row = dict(st, family=f, recent=recent.get(f) or [],
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
                              "days": st["days"], "recent": row["recent"]})
    waits = [c for c in cands if c["recent"]]
    props = sorted((c for c in cands if not c["recent"]), key=lambda c: -c["p50"])[:MAX_PROPOSALS]
    experiment = None
    if not any(p["kind"] == "add" for p in props):
        best = [r for r in rows if r["days"] >= MIN_DAYS and not r["recent"] and r.get("net_per_1k_q")
                and r["net_per_1k_q"]["p10"] > 0 and (r.get("t") or 0) >= EXP_MIN_T]
        if best:
            r = max(best, key=lambda r: r["net_per_1k_q"]["p10"])
            ca, cp = (_c(adds["c_rew"]), _c(adds["c_pnl"])) if adds["calibrated"] else (1.0, 1.0)
            experiment = {"family": r["family"], "kind": "experiment", "k": EXP_STEP,
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
    cal = {k: {x: sec["track"][k].get(x) for x in ("n", "calibrated", "c_rew", "c_pnl", "marginal_per_1k")}
           for k in ("adds", "cuts")}
    rows = [dict({k: v for k, v in p.items() if k != "recent"}, day=iso, cal=cal) for p in sec["proposals"]]
    if sec.get("experiment"):
        rows.append(dict(sec["experiment"], day=iso, cal=cal))
    if not rows:
        rows = [{"day": iso, "kind": "none", "cal": cal, "families": len(sec["families"])}]
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
