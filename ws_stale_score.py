"""ws_stale_score.py -- would a fast WebSocket stale-quote cancel have paid?

Jack 2026-10-04 ("yes do both"): before IMM_WS_FAST=1, score every
would-cancel. The bot's dry stale-quote check (IMM_WS=shadow) writes each
episode to ws_stale_<UTC date>.jsonl: the rung it found strictly ahead of the
external touch (`flag`, with the book, the touch and our orders) and how that
ended (`clear` / `amend` / `cancel` / `gone`). A fast path would have
cancelled the rung at the flag. Against that, this scores:

  AVOIDED   our fills on that rung after the flag (+1s for the cancel to
            land), until the cycle would have re-placed it (fills_*.jsonl by
            order_id and price), marked out against the external mid 5 and
            30 min later (cycle_log_*.csv). Avoiding a losing fill is a gain.
  GIVEN UP  the reward the rung would have earned meanwhile: the bot's own
            model (estimate_reward_share on the book at the flag, with and
            without the rung -- the two-sided target rule included) x the
            market's pool (cycle_log pool_per_day).

Re-place time (the counterfactual): an episode the cycle fixed itself
(amend / cancel) -> that moment, since the fast path's early cycle re-places
about when the slow one acted; one the market fixed (clear) or still open ->
D, the median time the cycle took to act; one that left the book (gone:
filled out or expired) -> the lesser of the two. Every fill counts once, in
the first episode whose window holds it; a re-flag of the same order inside
an earlier episode's window is folded into that episode (the fast path had
already pulled the order).

The bot writes a cycle's amend / cancel end line with the CYCLE's start time,
so the moment the cycle really acted comes from orders_*.jsonl (each of our
amends / cancels, timestamped when sent).

The dry check runs only in the bot's ~10s idle between cycles, so a rung that
went stale mid-cycle is flagged at the next idle -- exactly what the fast
path as built would see. Fills before the flag are reported, never counted.

It also scores the EVENT SWEEP BREAKER's dry trips (ws_sweep_*.jsonl): each
trip would have pulled every quote we had in its event for its hold, so
our fills there in (trip + 2s, trip + hold] are what it would have avoided
(marked out as above), against the event's modelled reward for the hold
(pool_per_day x est_frac over the event's quoted markets at the trip).

    python ws_stale_score.py                        # every ws_stale file
    python ws_stale_score.py --since 2026-10-04T19:00 --json out.json
"""

from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import incentive_mm as imm   # noqa: E402  (the bot's own book + reward model)

try:
    from imm_dashboard import family_of as _family_of   # noqa: E402
except Exception:            # pragma: no cover - the dashboard is optional
    _family_of = None

DEFAULT_DIR = imm.STATUS_DIR
HORIZONS = (300, 1800)       # mark-out horizons, seconds
CANCEL_LAND_SECS = 1.0       # a fill this soon after the flag beat the cancel
PRICE_TOL = 0.01             # cents: a fill on the flagged rung's price


def _parse_when(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"bad time {s!r} (want YYYY-MM-DD[THH:MM[:SS]], UTC)")


def _day_files(d: str, prefix: str, ext: str) -> List[str]:
    return sorted(glob.glob(os.path.join(d, f"{prefix}_????-??-??{ext}")))


def _family(ticker: str) -> str:
    series = ticker.split("-")[0]
    if _family_of is not None:
        try:
            return str(_family_of(series)[0])
        except Exception:
            pass
    return series


# ---- loading -----------------------------------------------------------------

def load_episodes(d: str, since: Optional[float], until: Optional[float]
                  ) -> List[dict]:
    """The flag lines in [since, until), each with its end line (matched by
    run and order) or end=None while still open."""
    rows: List[dict] = []
    for p in _day_files(d, "ws_stale", ".jsonl"):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    open_: Dict[Tuple[str, str], dict] = {}
    eps: List[dict] = []
    for r in rows:
        key = (str(r.get("run_id") or ""), str(r.get("order_id") or ""))
        if r.get("ev") == "flag":
            prev = open_.pop(key, None)
            if prev is not None:            # never closed: the flag replaced it
                prev["end"] = None
            ep = {"flag": r, "end": None}
            open_[key] = ep
            ts = float(r["ts"])
            if (since is None or ts >= since) and (until is None or ts < until):
                eps.append(ep)
        else:
            ep = open_.pop(key, None)
            if ep is not None:
                ep["end"] = r
    return eps


def load_fills(d: str, order_ids) -> Dict[str, List[dict]]:
    """Our maker fills on the given orders, once per fill_id (the sink can
    write a fill twice)."""
    want = set(order_ids)
    out: Dict[str, List[dict]] = defaultdict(list)
    seen = set()
    for p in _day_files(d, "fills", ".jsonl"):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("order_id") not in want or r.get("is_taker"):
                    continue
                fid = r.get("fill_id")
                if fid:
                    if fid in seen:
                        continue
                    seen.add(fid)
                out[r["order_id"]].append(r)
    for v in out.values():
        v.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def load_order_actions(d: str, order_ids, t_lo: float, t_hi: float
                       ) -> Dict[str, List[float]]:
    """order id -> the times (ascending) of our own amends / cancels of it,
    from orders_*.jsonl. The file is large (~180 MB/day): lines are matched on
    the order id before they are parsed."""
    want = set(order_ids)
    out: Dict[str, List[float]] = defaultdict(list)
    lo_day = datetime.fromtimestamp(t_lo, timezone.utc).strftime("%Y-%m-%d")
    hi_day = datetime.fromtimestamp(t_hi, timezone.utc).strftime("%Y-%m-%d")
    key = '"order_id": "'
    for p in _day_files(d, "orders", ".jsonl"):
        day = os.path.basename(p)[len("orders_"):-len(".jsonl")]
        if day < lo_day or day > hi_day:
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                i = line.find(key)
                if i < 0:
                    continue
                j = line.find('"', i + len(key))
                if line[i + len(key):j] not in want:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("kind") not in ("amend", "cancel"):
                        continue
                    ts = datetime.fromisoformat(str(r["ts"])).timestamp()
                except (ValueError, KeyError, TypeError):
                    continue
                out[r["order_id"]].append(ts)
    for v in out.values():
        v.sort()
    return out


_COLS = ("ts", "ext_bid", "ext_ask", "target", "discount", "pool_per_day",
         "est_frac")


def load_cycle_rows(d: str, tickers, t_lo: float, t_hi: float
                    ) -> Dict[str, List[tuple]]:
    """ticker -> [(ts, ext_bid, ext_ask, target, discount, pool_per_day,
    est_frac)] ascending, from the cycle logs (a header re-emitted inline on
    a schema widening resets the column map)."""
    want = set(tickers)
    out: Dict[str, List[tuple]] = defaultdict(list)
    ts_cache: Dict[str, float] = {}
    lo_day = datetime.fromtimestamp(t_lo, timezone.utc).strftime("%Y-%m-%d")
    hi_day = datetime.fromtimestamp(t_hi, timezone.utc).strftime("%Y-%m-%d")

    def num(v: str) -> Optional[float]:
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    for p in _day_files(d, "cycle_log", ".csv"):
        day = os.path.basename(p)[len("cycle_log_"):-len(".csv")]
        if day < lo_day or day > hi_day:
            continue
        with open(p, encoding="utf-8", newline="") as f:
            idx: Dict[str, int] = {}
            for row in csv.reader(f):
                if not row:
                    continue
                if row[0] == "ts":
                    idx = {c: i for i, c in enumerate(row)}
                    continue
                if not idx or len(row) <= idx.get("ticker", 1) \
                        or row[idx["ticker"]] not in want:
                    continue
                tss = row[idx["ts"]]
                ts = ts_cache.get(tss)
                if ts is None:
                    try:
                        ts = datetime.strptime(tss, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        continue
                    ts_cache[tss] = ts
                if ts < t_lo or ts > t_hi:
                    continue
                out[row[idx["ticker"]]].append((ts,) + tuple(
                    num(row[idx[c]]) if c in idx and idx[c] < len(row) else None
                    for c in _COLS[1:]))
    for v in out.values():
        v.sort(key=lambda r: r[0])
    return out


# ---- the model ---------------------------------------------------------------

def _own_cents(side: str, px: float) -> int:
    """Our order's integer YES cents the way the bot floors a book: a bid
    down, an ask (a NO bid) down in NO terms -- i.e. the YES price up."""
    if side == "bid":
        return int(math.floor(px + 1e-9))
    return 100 - int(math.floor(100.0 - px + 1e-9))


def _levels(flag: dict) -> Tuple[list, list]:
    ob = {"orderbook_fp": {
        "yes_dollars": [[f"{c / 100.0:.4f}", f"{q:.2f}"] for c, q in flag.get("yes") or []],
        "no_dollars": [[f"{c / 100.0:.4f}", f"{q:.2f}"] for c, q in flag.get("no") or []]}}
    return imm.orderbook_levels(ob)


def reward_rate(flag: dict, target: Optional[float], df: Optional[float],
                pool_per_day: Optional[float]) -> Optional[float]:
    """$/s the flagged rung adds to our reward on its market: the pool times
    our modelled share with the rung minus without it (book at the flag)."""
    if not target or df is None or pool_per_day is None:
        return None
    yes, no = _levels(flag)
    own = [(s, _own_cents(s, float(px)), float(r)) for s, px, r in flag.get("ours") or []]
    f_with, _ = imm.estimate_reward_share(yes, no, own, target, df, own_in_book=True)
    side, px, rem = flag["side"], float(flag["px"]), float(flag["rem"])
    oc = _own_cents(side, px)

    def drop(levels: list, at: int, q: float) -> list:
        out = []
        for p, s in levels:
            if int(p) == at:
                s = max(0.0, s - q)
            if s > 1e-9:
                out.append([p, s])
        return out

    if side == "bid":
        yes2, no2 = drop(yes, oc, rem), no
    else:
        yes2, no2 = yes, drop(no, 100 - oc, rem)
    own2, gone = [], False
    for s, c, r in own:
        if not gone and s == side and c == oc:
            gone = True
            r = r - rem
        if r > 1e-9:
            own2.append((s, c, r))
    f_without, _ = imm.estimate_reward_share(yes2, no2, own2, target, df,
                                             own_in_book=True)
    return pool_per_day / 86400.0 * max(0.0, f_with - f_without)


def _in_book(flag: dict) -> bool:
    """Was the flagged rung in the book it was flagged on? Before the bot's
    presence check (10/4 evening) it could flag an order already filled out or
    pulled mid-cycle: nothing to cancel, so not an episode."""
    px = float(flag["px"])
    lv, at = ((flag.get("yes") or [], px) if flag["side"] == "bid"
              else (flag.get("no") or [], 100.0 - px))
    return any(abs(float(c) - at) < 0.005 and float(q) > 0.01 for c, q in lv)


def _row_at_or_before(rows: List[tuple], tss: List[float], t: float
                      ) -> Optional[tuple]:
    if not rows:
        return None
    i = bisect.bisect_right(tss, t)
    return rows[i - 1] if i > 0 else rows[0]


def _mid_after(rows: List[tuple], tss: List[float], t: float) -> Optional[float]:
    if not rows:
        return None
    i = bisect.bisect_left(tss, t)
    for r in rows[i:]:
        b, a = r[1], r[2]
        if b is not None and a is not None:
            return (b + a) / 2.0
    return None


_SIDE = {("yes", "buy"): "bid", ("no", "buy"): "ask",
         ("yes", "sell"): "ask", ("no", "sell"): "bid"}


def our_side(f: dict) -> Optional[str]:
    """Our book side for a fill: the row's own ledger join, else Kalshi's
    side / action. 29-47% of fills_*.jsonl rows a day carry no ledger join
    (9/6-10/4) -- the order had left the ledger, filled out, before the
    fill was logged -- and the side / action map agrees with the join on
    every row that has both."""
    s = f.get("our_book_side")
    return s if s else _SIDE.get((f.get("side"), f.get("action")))


def is_full(f: dict) -> bool:
    """Did the fill take all that was left of our order? A row without the
    ledger join counts as full: its order had already left the ledger (on
    10/4, 85% of them show no later amend, cancel or fill)."""
    rb = f.get("our_remaining_before")
    return True if rb is None else float(rb) <= float(f.get("count") or 0) + 1e-9


def _fill_pnl(f: dict, mid: Optional[float]) -> Optional[float]:
    """$ mark-out of one of our fills against a later mid."""
    if mid is None:
        return None
    px, n = float(f.get("yes_price_cents") or 0), float(f.get("count") or 0)
    sign = 1.0 if our_side(f) == "bid" else -1.0
    return sign * (mid - px) * n / 100.0


# ---- scoring -----------------------------------------------------------------

def _acted_stale(e: dict, actions: Dict[str, List[float]]) -> Optional[float]:
    """Seconds from the flag to the cycle's own amend / cancel of the rung:
    the first such action in orders_*.jsonl after the flag, else the end
    line's figure (which counts to the cycle's start, so runs short)."""
    end = e["end"]
    if not end or end["ev"] not in ("amend", "cancel") or end.get("by") == "fast":
        return None
    t0 = float(e["flag"]["ts"])
    later = [ts for ts in actions.get(e["flag"]["order_id"], ()) if ts > t0]
    return (later[0] - t0) if later else float(end["stale_s"])


def score(eps: List[dict], fills: Dict[str, List[dict]],
          cyc: Dict[str, List[tuple]],
          actions: Optional[Dict[str, List[float]]] = None) -> dict:
    actions = actions or {}
    acted = {id(e): _acted_stale(e, actions) for e in eps}
    fixed = [v for v in acted.values() if v is not None]
    D = statistics.median(fixed) if fixed else 120.0
    used_fills = set()
    window_until: Dict[str, float] = {}
    ts_lists: Dict[str, List[float]] = {}
    res = []
    for e in sorted(eps, key=lambda e: float(e["flag"]["ts"])):
        fl, end = e["flag"], e["end"]
        t0 = float(fl["ts"])
        oid, t = fl["order_id"], fl["ticker"]
        ev = end["ev"] if end else "open"
        stale = acted[id(e)] if acted[id(e)] is not None else \
            (float(end["stale_s"]) if end else None)
        if fl.get("mode") == "live" or (end and end.get("by") == "fast"):
            res.append({"ticker": t, "order_id": oid, "end": ev, "live": True})
            continue
        if not _in_book(fl):
            res.append({"ticker": t, "order_id": oid, "end": ev, "phantom": True})
            continue
        if oid in window_until and t0 < window_until[oid]:
            res.append({"ticker": t, "order_id": oid, "end": ev, "folded": True})
            continue
        if ev in ("amend", "cancel"):
            A = stale
        elif ev == "gone":
            A = min(stale, D)
        else:                                # clear, open
            A = D
        window_until[oid] = t0 + A
        px = float(fl["px"])
        mine = [f for f in fills.get(oid, ())
                if abs(float(f.get("yes_price_cents") or -1) - px) <= PRICE_TOL]
        before = [f for f in mine if t0 - 5.0 <= float(f["ts"]) < t0 + CANCEL_LAND_SECS]
        avoid = [f for f in mine
                 if t0 + CANCEL_LAND_SECS <= float(f["ts"]) <= t0 + A
                 and f.get("fill_id") not in used_fills]
        used_fills.update(f.get("fill_id") for f in avoid)
        while_stale = [f for f in avoid
                       if stale is None or float(f["ts"]) <= t0 + stale]
        rows = cyc.get(t, [])
        tss = ts_lists.setdefault(t, [r[0] for r in rows])
        p = _row_at_or_before(rows, tss, t0)
        rate = reward_rate(fl, p[3], p[4], p[5]) if p else None
        mk = {}
        for h in HORIZONS:
            vals = [_fill_pnl(f, _mid_after(rows, tss, float(f["ts"]) + h))
                    for f in avoid]
            mk[h] = (sum(v for v in vals if v is not None),
                     sum(1 for v in vals if v is None))
        res.append({
            "ticker": t, "family": _family(t), "order_id": oid, "side": fl["side"],
            "phase": fl.get("phase") or "idle",
            "px": px, "rem": float(fl["rem"]), "gap_c": fl.get("gap_c"),
            "t0": t0, "end": ev, "stale_s": stale, "replace_s": A,
            "fills_before": sum(float(f["count"]) for f in before),
            "avoid_n": len(avoid),
            "avoid_ct": sum(float(f["count"]) for f in avoid),
            "while_stale_ct": sum(float(f["count"]) for f in while_stale),
            "pnl": {h: mk[h][0] for h in HORIZONS},
            "unmarked": {h: mk[h][1] for h in HORIZONS},
            "rate": rate,
            "reward_cost": None if rate is None else rate * A,
        })
    return {"D": D, "episodes": res}


def summarize(sc: dict, t_lo: float, t_hi: float) -> dict:
    eps = [r for r in sc["episodes"]
           if not (r.get("live") or r.get("folded") or r.get("phantom"))]
    hours = max((t_hi - t_lo) / 3600.0, 1e-9)

    def agg(rs: List[dict]) -> dict:
        cost = sum(r["reward_cost"] or 0.0 for r in rs)
        out = {"episodes": len(rs),
               "hit": sum(1 for r in rs if r["avoid_n"]),
               "avoid_ct": sum(r["avoid_ct"] for r in rs),
               "while_stale_ct": sum(r["while_stale_ct"] for r in rs),
               "reward_cost": cost,
               "reward_unscored": sum(1 for r in rs if r["rate"] is None)}
        for h in HORIZONS:
            pnl = sum(r["pnl"][h] for r in rs)
            out[f"avoided_{h}"] = -pnl                    # gain from skipping the fills
            out[f"net_{h}"] = -pnl - cost
        return out

    by_end = defaultdict(int)
    for r in eps:
        by_end[r["end"]] += 1
    fam = defaultdict(list)
    gap = defaultdict(list)
    phase = defaultdict(list)
    for r in eps:
        fam[r["family"]].append(r)
        phase[r["phase"]].append(r)
        g = r["gap_c"]
        gap["n/a" if g is None else "<=1c" if g <= 1.0 + 1e-9 else "1-3c"
            if g <= 3.0 + 1e-9 else "3-10c" if g <= 10.0 + 1e-9 else ">10c"].append(r)
    return {
        "window": [t_lo, t_hi], "hours": hours, "D": sc["D"],
        "total": agg(eps), "per_day": {k: v * 24.0 / hours for k, v in agg(eps).items()
                                       if isinstance(v, (int, float))},
        "by_end": dict(by_end),
        "live": sum(1 for r in sc["episodes"] if r.get("live")),
        "folded": sum(1 for r in sc["episodes"] if r.get("folded")),
        "phantom": sum(1 for r in sc["episodes"] if r.get("phantom")),
        "fills_before_ct": sum(r["fills_before"] for r in eps),
        "by_family": {k: agg(v) for k, v in fam.items()},
        "by_gap": {k: agg(v) for k, v in gap.items()},
        "by_phase": {k: agg(v) for k, v in phase.items()},
        "worst": sorted((r for r in eps if r["avoid_n"]),
                        key=lambda r: r["pnl"][HORIZONS[0]])[:10],
    }


def render(s: dict) -> str:
    T = s["total"]
    h5, h30 = HORIZONS
    lo, hi = (datetime.fromtimestamp(x, timezone.utc).strftime("%m-%d %H:%MZ")
              for x in s["window"])
    n = max(T["episodes"], 1)
    out = [f"# Fast stale-quote cancel, scored ({lo} - {hi}, {s['hours']:.1f}h)", "",
           f"{T['episodes']} episodes; the cycle took a median {s['D']:.0f}s to act "
           f"on its own (D, the re-place time for clears / open ones)."
           + (f" {s['folded']} re-flags folded into an earlier window." if s["folded"] else "")
           + (f" {s['live']} live fast-path episodes not scored." if s["live"] else "")
           + (f" {s['phantom']} flags on orders already out of the book dropped."
              if s["phantom"] else ""),
           "", "How the episodes ended: " + ", ".join(
               f"{k} {v} ({100.0 * v / n:.0f}%)" for k, v in sorted(
                   s["by_end"].items(), key=lambda kv: -kv[1])), "",
           f"Hit after the flag, before the re-place: {T['hit']} episodes "
           f"({100.0 * T['hit'] / n:.1f}%), {T['avoid_ct']:.0f} contracts "
           f"({T['while_stale_ct']:.0f} while the rung was still ahead). "
           f"Fills just BEFORE a flag (often its cause, never counted): "
           f"{s['fills_before_ct']:.0f} contracts.", "",
           "| | total | per day |", "|---|---|---|",
           f"| avoided (5m mark-out) | ${T[f'avoided_{h5}']:+.2f} | ${s['per_day'][f'avoided_{h5}']:+.2f} |",
           f"| avoided (30m mark-out) | ${T[f'avoided_{h30}']:+.2f} | ${s['per_day'][f'avoided_{h30}']:+.2f} |",
           f"| reward given up (model) | ${-T['reward_cost']:+.2f} | ${-s['per_day']['reward_cost']:+.2f} |",
           f"| **net, 5m basis** | **${T[f'net_{h5}']:+.2f}** | **${s['per_day'][f'net_{h5}']:+.2f}** |",
           f"| **net, 30m basis** | **${T[f'net_{h30}']:+.2f}** | **${s['per_day'][f'net_{h30}']:+.2f}** |",
           ""]
    if T["reward_unscored"]:
        out += [f"{T['reward_unscored']} episodes had no cycle-log row for their "
                f"market's pool (reward cost counted as 0).", ""]
    if s.get("by_phase"):
        out += ["## By when the check caught it", "",
                "cycle = the in-cycle check (WS_CHECK_IN_CYCLE); idle = between "
                "cycles, all the original fast path would see.", "",
                "| phase | episodes | hit | avoided 5m | avoided 30m | reward | net 30m |",
                "|---|---|---|---|---|---|---|"]
        for k in ("cycle", "idle"):
            v = s["by_phase"].get(k)
            if v:
                out.append(f"| {k} | {v['episodes']} | {v['hit']} | "
                           f"${v[f'avoided_{h5}']:+.2f} | ${v[f'avoided_{h30}']:+.2f} | "
                           f"${-v['reward_cost']:+.2f} | ${v[f'net_{h30}']:+.2f} |")
        out.append("")
    out += ["## By family", "", "| family | episodes | hit | ct | avoided 5m | avoided 30m | reward | net 30m |",
            "|---|---|---|---|---|---|---|---|"]
    for k, v in sorted(s["by_family"].items(), key=lambda kv: kv[1][f"net_{h30}"]):
        out.append(f"| {k} | {v['episodes']} | {v['hit']} | {v['avoid_ct']:.0f} | "
                   f"${v[f'avoided_{h5}']:+.2f} | ${v[f'avoided_{h30}']:+.2f} | "
                   f"${-v['reward_cost']:+.2f} | ${v[f'net_{h30}']:+.2f} |")
    out += ["", "## By how far ahead (gap to the external touch)", "",
            "| gap | episodes | hit | avoided 30m | reward | net 30m |", "|---|---|---|---|---|---|"]
    for k in ("<=1c", "1-3c", "3-10c", ">10c", "n/a"):
        v = s["by_gap"].get(k)
        if v:
            out.append(f"| {k} | {v['episodes']} | {v['hit']} | ${v[f'avoided_{h30}']:+.2f} | "
                       f"${-v['reward_cost']:+.2f} | ${v[f'net_{h30}']:+.2f} |")
    if s["worst"]:
        out += ["", "## Worst fills a fast cancel would have skipped", "",
                "| flagged | market | rung | gap | ct | 5m | 30m | ended |",
                "|---|---|---|---|---|---|---|---|"]
        for r in s["worst"]:
            out.append(
                f"| {datetime.fromtimestamp(r['t0'], timezone.utc):%m-%d %H:%MZ} | "
                f"{r['ticker']} | {r['side']} {r['px']:g}c | {r['gap_c']} | "
                f"{r['avoid_ct']:.0f} | ${r['pnl'][h5]:+.2f} | ${r['pnl'][h30]:+.2f} | "
                f"{r['end']} |")
    return "\n".join(out) + "\n"



# ---- event sweep breaker (ws_sweep_*.jsonl) ------------------------------------

SWEEP_LAT = 2.0              # the bot reacts on its WS fill feed within ~1-2s


def load_sweeps(d: str, since: Optional[float], until: Optional[float]
                ) -> List[dict]:
    out = []
    for p in _day_files(d, "ws_sweep", ".jsonl"):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = float(r.get("ts") or 0)
                if r.get("ev") == "trip" and (since is None or ts >= since) \
                        and (until is None or ts < until):
                    out.append(r)
    out.sort(key=lambda r: float(r["ts"]))
    return out


def load_event_fills(d: str, events) -> Dict[str, List[dict]]:
    """event -> our maker fills there (each fill_id once), by time."""
    want = set(events)
    out: Dict[str, List[dict]] = defaultdict(list)
    seen = set()
    for p in _day_files(d, "fills", ".jsonl"):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ev = r.get("event_ticker") or str(r.get("ticker", "")).rsplit("-", 1)[0]
                if ev not in want or r.get("is_taker"):
                    continue
                fid = r.get("fill_id")
                if fid:
                    if fid in seen:
                        continue
                    seen.add(fid)
                out[ev].append(r)
    for v in out.values():
        v.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def score_sweeps(trips: List[dict], efills: Dict[str, List[dict]],
                 cyc: Dict[str, List[tuple]]) -> List[dict]:
    """Each trip as if `on`: the event's fills in (t* + SWEEP_LAT, t* + hold]
    avoided (each fill once), against the event's reward for the hold."""
    used = set()
    tsl: Dict[str, List[float]] = {}
    res = []
    for tr in trips:
        t0, ev, hold = float(tr["ts"]), tr["event"], float(tr.get("hold_s") or 0)
        avoid = [f for f in efills.get(ev, ())
                 if t0 + SWEEP_LAT < float(f["ts"]) <= t0 + hold
                 and f.get("fill_id") not in used]
        used.update(f.get("fill_id") for f in avoid)
        pnl = {h: 0.0 for h in HORIZONS}
        unmarked = {h: 0 for h in HORIZONS}
        for f in avoid:
            rows = cyc.get(f["ticker"], [])
            ts = tsl.setdefault(f["ticker"], [r[0] for r in rows])
            for h in HORIZONS:
                v = _fill_pnl(f, _mid_after(rows, ts, float(f["ts"]) + h))
                if v is None:
                    unmarked[h] += 1
                else:
                    pnl[h] += v
        tickers = {tr["ticker"]} | {x[0] for x in tr.get("pull") or []}
        rate = 0.0
        for tk in tickers:
            rows = cyc.get(tk, [])
            ts = tsl.setdefault(tk, [r[0] for r in rows])
            p = _row_at_or_before(rows, ts, t0)
            if p and p[5] is not None and p[6] is not None:
                rate += p[5] * p[6]                    # pool_per_day x est_frac
        res.append({"ts": t0, "event": ev, "family": _family(tr["ticker"]),
                    "mode": tr.get("mode") or "dry",
                    "ticker": tr["ticker"], "pulled": len(tr.get("pull") or []),
                    "hold": hold, "avoid_n": len(avoid),
                    "avoid_ct": sum(float(f["count"]) for f in avoid),
                    "pnl": pnl, "unmarked": unmarked,
                    "cost": rate / 86400.0 * hold})
    return res


def _render_dry(res: List[dict], hours: float, title: str) -> List[str]:
    h5, h30 = HORIZONS
    k = 24.0 / max(hours, 1e-9)
    cost = sum(r["cost"] for r in res)
    a5 = -sum(r["pnl"][h5] for r in res)
    a30 = -sum(r["pnl"][h30] for r in res)
    hit = sum(1 for r in res if r["avoid_n"])
    out = ["", f"# {title} ({hours:.1f}h)", "",
           f"{len(res)} trips ({len(res) * k:.0f}/day); {hit} saw our fills in the "
           f"event inside its hold, {sum(r['avoid_ct'] for r in res):.0f} contracts. "
           f"Each would have pulled a median "
           f"{statistics.median([r['pulled'] for r in res]):.0f} order(s).", "",
           "| | total | per day |", "|---|---|---|",
           f"| avoided (5m mark-out) | ${a5:+.2f} | ${a5 * k:+.2f} |",
           f"| avoided (30m mark-out) | ${a30:+.2f} | ${a30 * k:+.2f} |",
           f"| reward given up (model) | ${-cost:+.2f} | ${-cost * k:+.2f} |",
           f"| **net, 5m basis** | **${a5 - cost:+.2f}** | **${(a5 - cost) * k:+.2f}** |",
           f"| **net, 30m basis** | **${a30 - cost:+.2f}** | **${(a30 - cost) * k:+.2f}** |",
           "", "| family | trips | hit | ct | avoided 30m | reward | net 30m |",
           "|---|---|---|---|---|---|---|"]
    fam = defaultdict(list)
    for r in res:
        fam[r["family"]].append(r)
    rows = []
    for f, rs in fam.items():
        c = sum(r["cost"] for r in rs)
        a = -sum(r["pnl"][h30] for r in rs)
        rows.append((a - c, f, len(rs), sum(1 for r in rs if r["avoid_n"]),
                     sum(r["avoid_ct"] for r in rs), a, c))
    for net, f, n, hh, ct, a, c in sorted(rows):
        out.append(f"| {f} | {n} | {hh} | {ct:.0f} | ${a:+.2f} | ${-c:+.2f} | ${net:+.2f} |")
    return out


def net_live(res: List[dict], hours: float) -> Optional[dict]:
    """The LIVE breaker's estimated net, per day: the pull's avoided loss per
    trip, measured on the CONTROL trips (SWEEP_HOLDOUT -- nothing pulled, so
    the losses a pull prevents are still visible), times the live trips, less
    what still filled inside the live holds and the reward the live holds
    gave up. None without both kinds of trip."""
    live = [r for r in res if r["mode"] == "on"]
    ctrl = [r for r in res if r["mode"] == "control"]
    if not live or not ctrl:
        return None
    k = 24.0 / max(hours, 1e-9)
    out = {"live": len(live), "control": len(ctrl),
           "control_hit": sum(1 for r in ctrl if r["avoid_n"]),
           "live_per_day": len(live) * k,
           "pulled_median": statistics.median([r["pulled"] for r in live]),
           "leak_ct": sum(r["avoid_ct"] for r in live),
           "cost_day": sum(r["cost"] for r in live) * k}
    for h in HORIZONS:
        per = -sum(r["pnl"][h] for r in ctrl) / len(ctrl)      # avoided per trip
        leak = -sum(r["pnl"][h] for r in live) * k             # loss that still landed
        out[f"avoid_per_trip_{h}"] = per
        out[f"avoided_day_{h}"] = per * len(live) * k
        out[f"leak_day_{h}"] = leak
        out[f"net_day_{h}"] = per * len(live) * k - leak - out["cost_day"]
    return out


def render_sweeps(res: List[dict], hours: float) -> str:
    if not res:
        return ""
    h5, h30 = HORIZONS
    live = [r for r in res if r["mode"] == "on"]
    ctrl = [r for r in res if r["mode"] == "control"]
    dry = [r for r in res if r["mode"] == "dry"]
    out: List[str] = []
    n = net_live(res, hours)
    if n:
        out += ["", f"# Event sweep breaker -- LIVE, netted against its control trips ({hours:.1f}h)", "",
                f"{n['live']} live trips ({n['live_per_day']:.0f}/day), a median "
                f"{n['pulled_median']:.0f} order(s) pulled each; {n['leak_ct']:.0f} contracts "
                f"still filled inside a live hold. {n['control']} control trips "
                f"({n['control_hit']} with fills inside the hold) give the pull's "
                f"avoided loss per trip: ${n[f'avoid_per_trip_{h5}']:+.3f} (5m) / "
                f"${n[f'avoid_per_trip_{h30}']:+.3f} (30m)."
                + (" FEW control trips with fills -- noisy until ~30."
                   if n["control_hit"] < 30 else ""), "",
                "| per day | 5m mark-out | 30m mark-out |", "|---|---|---|",
                f"| avoided (control rate x live trips) | ${n[f'avoided_day_{h5}']:+.2f} | ${n[f'avoided_day_{h30}']:+.2f} |",
                f"| still filled inside live holds | ${-n[f'leak_day_{h5}']:+.2f} | ${-n[f'leak_day_{h30}']:+.2f} |",
                f"| reward given up (live holds) | ${-n['cost_day']:+.2f} | ${-n['cost_day']:+.2f} |",
                f"| **net of the live breaker** | **${n[f'net_day_{h5}']:+.2f}** | **${n[f'net_day_{h30}']:+.2f}** |"]
    elif live:
        out += ["", f"# Event sweep breaker -- LIVE ({hours:.1f}h)", "",
                f"{len(live)} live trips and no control trips (IMM_SWEEP_HOLDOUT=0): "
                f"what a pull prevents is not visible, so no net. Reward given up "
                f"${sum(r['cost'] for r in live):+.2f}; {sum(r['avoid_ct'] for r in live):.0f} "
                f"contracts still filled inside a live hold."]
    if ctrl:
        out += _render_dry(ctrl, hours, "Control trips (nothing pulled) -- what a pull would have done")
    if dry:
        out += _render_dry(dry, hours, "Event sweep breaker (dry), scored")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", help="UTC, e.g. 2026-10-04T19:00")
    ap.add_argument("--until", help="UTC")
    ap.add_argument("--json", help="also write the summary here")
    a = ap.parse_args(argv)
    since, until = _parse_when(a.since), _parse_when(a.until)
    eps = load_episodes(a.dir, since, until)
    trips = load_sweeps(a.dir, since, until)
    if not eps and not trips:
        print("no ws_stale episodes or ws_sweep trips in the window")
        return 1
    stamps = [float(e["flag"]["ts"]) for e in eps] + [float(t["ts"]) for t in trips]
    t_lo, t_hi = min(stamps), max(stamps)
    efills = load_event_fills(a.dir, {t["event"] for t in trips}) if trips else {}
    tickers = {e["flag"]["ticker"] for e in eps} | {t["ticker"] for t in trips} \
        | {x[0] for t in trips for x in t.get("pull") or []} \
        | {f["ticker"] for v in efills.values() for f in v}
    cyc = load_cycle_rows(a.dir, tickers, t_lo - 3600.0, t_hi + 2 * 3600.0)
    out: dict = {}
    if eps:
        fills = load_fills(a.dir, {e["flag"]["order_id"] for e in eps})
        actions = load_order_actions(a.dir, {e["flag"]["order_id"] for e in eps},
                                     t_lo - 60.0, t_hi + 2 * 3600.0)
        s = summarize(score(eps, fills, cyc, actions), since or t_lo, until or t_hi)
        sys.stdout.write(render(s))
        out = s
    if trips:
        sres = score_sweeps(trips, efills, cyc)
        hours = max(((until or t_hi) - (since or t_lo)) / 3600.0, 1e-9)
        sys.stdout.write(render_sweeps(sres, hours))
        out["sweeps"] = sres
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
