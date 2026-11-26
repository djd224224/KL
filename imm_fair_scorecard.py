#!/usr/bin/env python3
r"""imm_fair_scorecard.py -- every fair model scored against the market, daily
(the review's evidence section 13; Jack 2026-11-26: "Yes, build it", to "Build
the fair-model scorecard: log the fair behind each quote, score every model
daily?").

Reads what imm_fair_marks.py logs every 15 minutes (fair_marks_<UTC date>.jsonl,
fair_results_<UTC date>.jsonl) and the bot's fills, and asks of every model
family:

  SETTLEMENT   When a market settled, was the model's fair before the close
               closer to the outcome than the market's mid at the same moment?
               Brier score of each (lower is better); skill = Brier(market) -
               Brier(model): positive means the model knew more than the
               market. Strikes of one event settle together (one rain total
               decides them all), so the 90% interval is a bootstrap over
               EVENTS, not markets. Also on the markets where the model and
               the mid disagreed by DISAGREE_C or more: there, trusting the
               model is a trade.
  CONVERGENCE  Over the day, does the mid move toward the model? The mid's move
               against the model's gap at the start of the day: the slope
               through the origin, and the share of moves toward the model.
  FILLS        Our fills scored by the model at that moment: edge = model minus
               price on a bid fill, price minus model on an ask fill, in cents.
               Contracts at -3c or worse are trades against our own model.

Settlements scored once are kept in daily_review/moat/model_scores.jsonl (one
row per market, forever, backed up with the moat), so the 30-day window costs
one small file, not 30 days of marks. moat/model_daily.csv keeps one row per
day per family. Every number is MEASURED (logged fairs, books, fills,
Kalshi's results); "beats the market" is a statistical call, not a proof.
"""
from __future__ import annotations

import csv
import json
import os
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import imm_logfiles
import imm_review as rv

WINDOW_DAYS = 30
DISAGREE_C = 5.0          # the model and the mid this far apart: following the model is a trade
MIN_EVENTS = 8            # fewer settled events than this: accumulating, no call
BOOT = 1000               # bootstrap resamples over events
CONV_MIN_H = 6.0          # a day's convergence pair spans at least this
CONV_MIN_GAP_C = 3.0      # and starts with the model this far from the mid
FILL_MARK_AGE_S = 5400    # a fill is scored only against a mark from the 90 min before it
AGAINST_C = -3.0          # a contract filled this far against the model
MARK_LOOKBACK_DAYS = 3    # a settled market's last mark is looked for this far before the close
MODEL_DAILY_COLS = ["day", "family", "markets", "settled_30d", "events_30d", "brier_model", "brier_market",
                    "skill", "skill_lo", "skill_hi", "disagree_n", "disagree_skill", "verdict",
                    "conv_n", "conv_slope", "conv_toward", "fills", "fill_cts", "fill_edge_c", "against_share"]


def _utc_days(day: date, before: int = 0, after: int = 1) -> List[str]:
    return [(day + timedelta(days=i)).isoformat() for i in range(-before, after + 1)]


def _rows(prefix: str, days: Iterable[str]) -> Iterable[dict]:
    """Every row of STATUS_DIR/<prefix>_<day>.jsonl[.gz] for those UTC days."""
    days = sorted(set(days))
    for d, path in imm_logfiles.day_files(rv.STATUS_DIR, prefix, "jsonl", days[0], days[-1]):
        if d not in days:
            continue
        try:
            fh = imm_logfiles.open_text(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def _event(ticker: str) -> str:
    parts = (ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else ticker


def _mid(r: dict) -> Optional[float]:
    b, a = r.get("bid"), r.get("ask")
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return (b + a) / 2.0
    return None


def scores_path() -> str:
    return os.path.join(rv.MOAT_DIR, "model_scores.jsonl")


# ---------------------------------------------------------------------------
# settlement scores (incremental, kept forever)
# ---------------------------------------------------------------------------

def score_new_results(day: date) -> int:
    """Score every logged result not yet in model_scores.jsonl: the model's and
    the market's values from the last mark with a book before the close (the
    model alone from the last mark when no mark had a book)."""
    done = {r.get("ticker") for r in rv._iter_jsonl(scores_path())}
    new = [r for r in _rows("fair_results", _utc_days(day, before=7)) if r.get("ticker") not in done]
    new = list({r["ticker"]: r for r in new if r.get("ticker")}.values())
    if not new:
        return 0
    want = {r["ticker"]: (rv._ts_of(r.get("close_ts")) or rv._ts_of(r.get("ts"))) for r in new}
    days = set()
    for close in want.values():
        if close:
            cd = datetime.fromtimestamp(close, timezone.utc).date()
            days.update(_utc_days(cd, before=MARK_LOOKBACK_DAYS, after=0))
    best_book: Dict[str, dict] = {}
    best_any: Dict[str, dict] = {}
    for m in _rows("fair_marks", days) if days else []:
        t = m.get("ticker")
        close = want.get(t)
        ts = rv._ts_of(m.get("ts"))
        if close is None or ts is None or ts > close:
            continue
        if t not in best_any or ts >= best_any[t]["_ts"]:
            best_any[t] = dict(m, _ts=ts)
        if _mid(m) is not None and (t not in best_book or ts >= best_book[t]["_ts"]):
            best_book[t] = dict(m, _ts=ts)
    rows = []
    for r in new:
        t = r["ticker"]
        m = best_book.get(t) or best_any.get(t)
        if not m:
            continue                          # no mark before the close: nothing to score
        close = want[t]
        mid = _mid(m)
        rows.append({"ticker": t, "event": _event(t), "family": r.get("family") or m.get("family"),
                     "y": 1 if r.get("result") == "yes" else 0, "close_ts": r.get("close_ts"),
                     "mark_ts": m.get("ts"), "p": m.get("p"), "mid": None if mid is None else round(mid, 2),
                     "bid": m.get("bid"), "ask": m.get("ask"),
                     "lead_h": round((close - m["_ts"]) / 3600.0, 2) if close else None,
                     "scored": day.isoformat()})
    if rows:
        os.makedirs(rv.MOAT_DIR, exist_ok=True)
        with open(scores_path(), "a", encoding="utf-8") as fh:
            for x in rows:
                fh.write(json.dumps(x, separators=(",", ":")) + "\n")
    return len(rows)


def _event_bootstrap(diffs_by_event: Dict[str, List[float]], seed: int = 0) -> Tuple[Optional[float], Optional[float]]:
    """90% interval of the mean per-market difference, resampling EVENTS."""
    evs = list(diffs_by_event.values())
    if len(evs) < 2:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(BOOT):
        s = n = 0.0
        for _k in range(len(evs)):
            d = evs[rng.randrange(len(evs))]
            s += sum(d)
            n += len(d)
        means.append(s / n)
    means.sort()
    return round(means[int(0.05 * BOOT)], 5), round(means[int(0.95 * BOOT) - 1], 5)


def settle_table(day: date, window: int = WINDOW_DAYS) -> Dict[str, dict]:
    """{family: stats} over the markets that closed in the window's ET days."""
    lo = day - timedelta(days=window - 1)
    by_fam: Dict[str, List[dict]] = defaultdict(list)
    for r in rv._iter_jsonl(scores_path()):
        c = rv._ts_of(r.get("close_ts"))
        if c is None:
            continue
        d = datetime.fromtimestamp(c, rv.ET).date()
        if lo <= d <= day and isinstance(r.get("p"), (int, float)):
            by_fam[r.get("family") or "?"].append(r)
    out: Dict[str, dict] = {}
    for fam, rs in by_fam.items():
        paired = [r for r in rs if isinstance(r.get("mid"), (int, float))]
        diffs: Dict[str, List[float]] = defaultdict(list)
        dis: Dict[str, List[float]] = defaultdict(list)
        bm, bk = [], []
        for r in paired:
            y, p, q = r["y"], float(r["p"]), float(r["mid"]) / 100.0
            b_model, b_mkt = (p - y) ** 2, (q - y) ** 2
            bm.append(b_model)
            bk.append(b_mkt)
            diffs[r.get("event") or r["ticker"]].append(b_mkt - b_model)
            if abs(p * 100.0 - r["mid"]) >= DISAGREE_C:
                dis[r.get("event") or r["ticker"]].append(b_mkt - b_model)
        n_ev = len(diffs)
        skill = round(statistics.mean(x for v in diffs.values() for x in v), 5) if diffs else None
        lo_ci, hi_ci = _event_bootstrap(diffs)
        dskill = (round(statistics.mean(x for v in dis.values() for x in v), 5) if dis else None)
        if n_ev < MIN_EVENTS:
            verdict = "accumulating"
        elif lo_ci is not None and lo_ci > 0:
            verdict = "beats the market"
        elif hi_ci is not None and hi_ci < 0:
            verdict = "loses to the market"
        else:
            verdict = "no measurable edge"
        model_only = [(float(r["p"]) - r["y"]) ** 2 for r in rs]
        out[fam] = {"settled": len(rs), "paired": len(paired), "events": n_ev,
                    "brier_model": round(statistics.mean(bm), 4) if bm else None,
                    "brier_market": round(statistics.mean(bk), 4) if bk else None,
                    "brier_model_all": round(statistics.mean(model_only), 4) if model_only else None,
                    "skill": skill, "skill_lo": lo_ci, "skill_hi": hi_ci,
                    "disagree_n": sum(len(v) for v in dis.values()), "disagree_skill": dskill,
                    "median_lead_h": (round(statistics.median(r["lead_h"] for r in paired if r.get("lead_h") is not None), 1)
                                      if any(r.get("lead_h") is not None for r in paired) else None),
                    "verdict": verdict}
    return out


# ---------------------------------------------------------------------------
# the day: convergence and fills
# ---------------------------------------------------------------------------

def day_marks(day: date) -> Dict[str, List[Tuple[float, float, Optional[float], str]]]:
    """{ticker: [(ts, p, mid, family)]} for the ET day, in time order."""
    s, e = rv.et_bounds(day)
    out: Dict[str, List[Tuple[float, float, Optional[float], str]]] = defaultdict(list)
    for m in _rows("fair_marks", rv.utc_dates(day)):
        ts = rv._ts_of(m.get("ts"))
        p = m.get("p")
        if ts is None or not (s - FILL_MARK_AGE_S <= ts < e) or not isinstance(p, (int, float)):
            continue
        out[m.get("ticker")].append((ts, float(p), _mid(m), m.get("family") or "?"))
    for v in out.values():
        v.sort()
    return out


def convergence(day: date, marks) -> Dict[str, dict]:
    s, _e = rv.et_bounds(day)
    acc: Dict[str, dict] = defaultdict(lambda: {"xy": 0.0, "xx": 0.0, "n": 0, "toward": 0, "moved": 0})
    for t, ms in marks.items():
        booked = [m for m in ms if m[2] is not None and m[0] >= s]
        if len(booked) < 2:
            continue
        a, b = booked[0], booked[-1]
        if (b[0] - a[0]) / 3600.0 < CONV_MIN_H:
            continue
        x = a[1] * 100.0 - a[2]
        y = b[2] - a[2]
        if abs(x) < CONV_MIN_GAP_C:
            continue
        r = acc[a[3]]
        r["xy"] += x * y
        r["xx"] += x * x
        r["n"] += 1
        if y:
            r["moved"] += 1
            r["toward"] += (y > 0) == (x > 0)
    return {f: {"n": r["n"], "slope": round(r["xy"] / r["xx"], 3) if r["xx"] else None,
                "toward": round(r["toward"] / r["moved"], 3) if r["moved"] else None}
            for f, r in acc.items()}


def fills_edge(day: date, marks) -> Dict[str, dict]:
    s, e = rv.et_bounds(day)
    acc: Dict[str, dict] = defaultdict(lambda: {"fills": 0, "cts": 0.0, "edge_cts": 0.0, "against": 0.0,
                                                "by_ticker": defaultdict(float)})
    for f in _rows("fills", rv.utc_dates(day)):
        ts = rv._ts_of(f.get("ts"))
        if ts is None or not (s <= ts < e):
            continue
        ms = marks.get(f.get("ticker"))
        if not ms:
            continue
        prior = [m for m in ms if m[0] <= ts and ts - m[0] <= FILL_MARK_AGE_S]
        if not prior:
            continue
        p = prior[-1][1] * 100.0
        px = f.get("yes_price_cents")
        side = f.get("our_book_side")
        if not isinstance(px, (int, float)) or side not in ("bid", "ask"):
            continue
        edge = (p - px) if side == "bid" else (px - p)
        cts = abs(float(f.get("count") or 0))
        r = acc[prior[-1][3]]
        r["fills"] += 1
        r["cts"] += cts
        r["edge_cts"] += edge * cts
        if edge <= AGAINST_C:
            r["against"] += cts
            r["by_ticker"][f.get("ticker")] += cts * edge
    out = {}
    for fam, r in acc.items():
        worst = sorted(r["by_ticker"].items(), key=lambda kv: kv[1])[:3]
        out[fam] = {"fills": r["fills"], "contracts": round(r["cts"], 1),
                    "edge_c": round(r["edge_cts"] / r["cts"], 2) if r["cts"] else None,
                    "against_share": round(r["against"] / r["cts"], 3) if r["cts"] else None,
                    "worst": [{"ticker": t, "cents_x_contracts": round(v, 1)} for t, v in worst]}
    return out


def snapshot_health(now: Optional[float] = None) -> dict:
    st = rv._load_json(os.path.join(rv.STATUS_DIR, "fair_marks_state.json"), {}) or {}
    run = st.get("run") or {}
    at = rv._ts_of(run.get("at"))
    age_min = round(((now or datetime.now(timezone.utc).timestamp()) - at) / 60.0, 1) if at else None
    return {"last_run": rv.et_str(at, "%Y-%m-%d %H:%M") if at else None, "age_min": age_min,
            "families": run.get("families") or {}, "markets": run.get("markets"), "marks": run.get("marks"),
            "books": run.get("books"), "errors": (run.get("errors") or [])[:5],
            "state": "never run" if not at else ("stale" if age_min is not None and age_min > 60 else "ok")}


def sec_models(day: date) -> dict:
    added = score_new_results(day)
    settle = settle_table(day)
    marks = day_marks(day)
    conv = convergence(day, marks)
    fills = fills_edge(day, marks)
    health = snapshot_health()
    fams = sorted(set(settle) | set(conv) | set(fills) | set(health.get("families") or {}))
    rows = []
    for fam in fams:
        s, c, f = settle.get(fam) or {}, conv.get(fam) or {}, fills.get(fam) or {}
        rows.append({"family": fam, "markets": (health.get("families") or {}).get(fam), **{
            "settled": s.get("settled", 0), "events": s.get("events", 0), "brier_model": s.get("brier_model"),
            "brier_market": s.get("brier_market"), "skill": s.get("skill"), "skill_lo": s.get("skill_lo"),
            "skill_hi": s.get("skill_hi"), "disagree_n": s.get("disagree_n", 0),
            "disagree_skill": s.get("disagree_skill"), "median_lead_h": s.get("median_lead_h"),
            "verdict": s.get("verdict") or "accumulating",
            "conv_n": c.get("n", 0), "conv_slope": c.get("slope"), "conv_toward": c.get("toward"),
            "fills": f.get("fills", 0), "fill_cts": f.get("contracts", 0), "fill_edge_c": f.get("edge_c"),
            "against_share": f.get("against_share"), "worst_fills": f.get("worst") or []}})
    _append_daily(day, rows)
    return {"health": health, "scored_today": added, "window_days": WINDOW_DAYS, "families": rows,
            "counts": {v: sum(1 for r in rows if r["verdict"] == v)
                       for v in ("beats the market", "loses to the market", "no measurable edge", "accumulating")}}


def _append_daily(day: date, rows: List[dict]) -> None:
    path = os.path.join(rv.MOAT_DIR, "model_daily.csv")
    old = [r for r in rv._read_rows(path) if r.get("day") != day.isoformat()] if os.path.exists(path) else []
    new = [{"day": day.isoformat(), "family": r["family"], "markets": r.get("markets"),
            "settled_30d": r["settled"], "events_30d": r["events"], "brier_model": r["brier_model"],
            "brier_market": r["brier_market"], "skill": r["skill"], "skill_lo": r["skill_lo"],
            "skill_hi": r["skill_hi"], "disagree_n": r["disagree_n"], "disagree_skill": r["disagree_skill"],
            "verdict": r["verdict"], "conv_n": r["conv_n"], "conv_slope": r["conv_slope"],
            "conv_toward": r["conv_toward"], "fills": r["fills"], "fill_cts": r["fill_cts"],
            "fill_edge_c": r["fill_edge_c"], "against_share": r["against_share"]} for r in rows]
    os.makedirs(rv.MOAT_DIR, exist_ok=True)
    rv._write_rows(path, old + new, MODEL_DAILY_COLS)
