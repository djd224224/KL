#!/usr/bin/env python3
"""VERCEL FAIR BLEND weights for incentive_mm (Jack 2026-11-25: "Yes, build
the blend", on "have the gate and the leads read a blended price per series
... refitting them on new settled events").

incentive_mm's Vercel gate, fair's edge, FAIR LEAD and FAIR CLOSE read
    blend = mid + w (fair - mid)
the market's model fair (vercel_fair) blended with its external two-sided
mid. w is how far, on settled history, the outcome travelled from the book's
mid toward our fair. For each Vercel LAB series (one lab's share; the
open-weights shares read Vercel's weight-class chart, which the logger has
kept only since 2026-11-24, so they keep w = 1):

  1. every settled event whose measured day D falls in the WINDOW_DAYS before
     today (Kalshi's results, signed reads through kalshi_reads);
  2. today's vercel_fair replayed every hour from D - 4 days to the IMM's
     cutoff (D 00:00Z - IMM_VERCEL_CUTOFF_BEFORE_D_MIN), exactly as the bot
     builds it: the running anchor on the KL-data vercel-logger's running
     share from RUN_START_MIN after 00:00Z (no running value: no row, as the
     gate fails closed), the complete-day anchor before it;
  3. the logger's Kalshi quotes at that hour (within 5 min): the strikes the
     blend is used on -- a two-sided book, mid 5-95c, model fair 5-95c (a
     decided strike reads the model itself);
  4. w = the slope of (100 y - mid) on (fair - mid) through the origin, with
     an event-bootstrap 90% band, and the event-weighted Brier of the mid,
     the fair and the blend.

Writes VERCEL_BLEND_FILE (incentive_mm hot-reloads it by mtime) for each
series with at least MIN_EVENTS events, w clipped to [0, 1]. A series whose
Kalshi read fails keeps its previous entry ("carried"); one without the
events is left out (the bot then uses its VERCEL_BLEND_DEFAULT_W, else 1).
A failed export read writes nothing. The 11/25 study this productionizes:
KL-data/vercel-logger/analysis-2026-11-25-deepvreq.

Usage: python vercel_blend_fit.py [--dry] [--out PATH] [--window DAYS]
Daily: Windows task "KL vercel-blend-fit" at 10:40 local (Kalshi settles D's
markets ~14:00Z D+1), logging to run-logs/incentive-mm/vercel-blend-fit-task.log.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

KL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KL)
import vercel_fair as vf  # noqa: E402

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
OUT_FILE = os.environ.get("IMM_VERCEL_BLEND_FILE",
                          os.path.join(STATUS_DIR, "vercel_blend.json"))
SERIES_FILE = os.environ.get("IMM_VERCEL_SERIES_FILE",
                             os.path.join(STATUS_DIR, "vercel_series.json"))
WINDOW_DAYS = int(os.environ.get("IMM_VERCEL_BLEND_WINDOW_DAYS", "60"))
MIN_EVENTS = int(os.environ.get("IMM_VERCEL_BLEND_MIN_EVENTS", "12"))
CUTOFF_MIN = int(os.environ.get("IMM_VERCEL_CUTOFF_BEFORE_D_MIN", "60"))
FAIR_MIN_C = 100.0 * float(os.environ.get("IMM_VERCEL_FAIR_MIN_P", "0.05"))
LEAD_DAYS = 4                 # programs list ~3.3 days before D
QUOTE_TOL_S = 300             # a logger quote at most 5 min old
RUN_TOL_S = 600               # a running value at most 10 min old
BOOT = 300                    # event-bootstrap draws (seeded)
MID_LO, MID_HI = 5.0, 95.0


# ---------------------------------------------------------------------------
# the fit

def slope(rows: List[dict]) -> Optional[float]:
    """Least-squares slope of (100 y - mid) on (fair - mid) through the
    origin; None when every row has fair == mid."""
    sx = sum((r["fair"] - r["mid"]) ** 2 for r in rows)
    if sx <= 0:
        return None
    return sum((r["fair"] - r["mid"]) * (100.0 * r["y"] - r["mid"])
               for r in rows) / sx


def ev_mean(rows: List[dict], f: Callable[[dict], float]) -> float:
    """Mean over events of each event's own mean (hourly rows of one event
    are not independent)."""
    g: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        g[r["ev"]].append(f(r))
    return statistics.fmean(statistics.fmean(v) for v in g.values())


def fit_rows(rows: List[dict], boot: int = BOOT, seed: int = 1) -> Optional[dict]:
    """The blend weight of one series' replay rows: {"w" (clipped to [0, 1]),
    "w_raw", "lo", "hi" (event-bootstrap 5th / 95th percentiles), "events",
    "rows", "brier_mid", "brier_fair", "brier_blend"}; None without rows or
    spread."""
    w = slope(rows) if rows else None
    if w is None:
        return None
    by: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by[r["ev"]].append(r)
    evs = sorted(by)
    rng = random.Random(seed)
    draws = []
    for _ in range(boot):
        smp = [r for e in rng.choices(evs, k=len(evs)) for r in by[e]]
        s = slope(smp)
        if s is not None:
            draws.append(s)
    draws.sort()
    lo = draws[int(0.05 * (len(draws) - 1))] if draws else w
    hi = draws[int(0.95 * (len(draws) - 1))] if draws else w
    wc = min(max(w, 0.0), 1.0)

    def blend(r: dict) -> float:
        return min(max(r["mid"] + wc * (r["fair"] - r["mid"]), 1.0), 99.0)

    return {"w": round(wc, 4), "w_raw": round(w, 4), "lo": round(lo, 4),
            "hi": round(hi, 4), "events": len(evs), "rows": len(rows),
            "brier_mid": round(ev_mean(rows, lambda r: (r["mid"] / 100 - r["y"]) ** 2), 4),
            "brier_fair": round(ev_mean(rows, lambda r: (r["fair"] / 100 - r["y"]) ** 2), 4),
            "brier_blend": round(ev_mean(rows, lambda r: (blend(r) / 100 - r["y"]) ** 2), 4)}


# ---------------------------------------------------------------------------
# the replay

def load_quotes(data_dir: str, first: date, last: date, prefixes: Tuple[str, ...]
                ) -> Dict[str, List[Tuple[float, float, float]]]:
    """ticker -> sorted [(ts, yes_bid_c, yes_ask_c)] from the logger's
    kalshi_quotes files, first .. last, for tickers starting with a prefix."""
    out: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    d = first
    while d <= last:
        for ext in (".jsonl", ".jsonl.gz"):
            p = os.path.join(data_dir, f"kalshi_quotes_{d.isoformat()}{ext}")
            if not os.path.exists(p):
                continue
            opener = gzip.open if p.endswith(".gz") else open
            try:
                with opener(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            ts = datetime.fromisoformat(rec["t"]).timestamp()
                        except (ValueError, KeyError, TypeError):
                            continue
                        for q in rec.get("q") or []:
                            if not q or not str(q[0]).startswith(prefixes):
                                continue
                            try:
                                out[q[0]].append((ts, float(q[1]) * 100.0,
                                                  float(q[2]) * 100.0))
                            except (TypeError, ValueError, IndexError):
                                continue
            except (OSError, EOFError):
                pass
            break
        d += timedelta(days=1)
    for v in out.values():
        v.sort()
    return out


def at_or_before(xs: List[tuple], ts: float, tol: float) -> Optional[tuple]:
    """The last entry of a ts-sorted list at or before `ts`, within `tol` s."""
    i = bisect.bisect_right([x[0] for x in xs], ts) - 1
    if i < 0 or ts - xs[i][0] > tol:
        return None
    return xs[i]


class RunningShare:
    """The logger's running share by (series, ts), parsed once per day."""

    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.days: Dict[date, Dict[str, List[Tuple[float, float]]]] = {}

    def at(self, series: str, ts: float) -> Optional[float]:
        day = datetime.fromtimestamp(ts, timezone.utc).date()
        if day not in self.days:
            by: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
            p = vf._day_file(self.dir, day)
            if p:
                base = datetime(day.year, day.month, day.day,
                                tzinfo=timezone.utc).timestamp()
                try:
                    for mnt, vals in vf.parse_logger_day(p, day, True)["run"]:
                        for s, v in vals.items():
                            by[s].append((base + mnt * 60.0, v))
                except (OSError, EOFError, ValueError):
                    pass
            self.days[day] = by
        hit = at_or_before(self.days[day].get(series) or [], ts, RUN_TOL_S)
        return None if hit is None else hit[1]


def replay_series(series: str, x_all: Dict[str, float],
                  markets: Dict[str, dict], quotes, running: RunningShare,
                  calib: Callable[[date], dict]) -> List[dict]:
    """The hourly replay rows of one series' settled events (see the module
    docstring): {"ev", "t", "ts", "k", "K", "mid", "fair", "y"}."""
    events: Dict[str, List[str]] = defaultdict(list)
    for t, m in markets.items():
        events[m["event"]].append(t)
    rows: List[dict] = []
    for ev, tks in sorted(events.items()):
        D = vf.measured_day(ev)
        if D is None:
            continue
        d0 = datetime(D.year, D.month, D.day, tzinfo=timezone.utc)
        T = d0 - timedelta(days=LEAD_DAYS)
        cut = d0 - timedelta(minutes=CUTOFF_MIN)
        while T <= cut:
            ts = T.timestamp()
            today = T.date()
            last = today - timedelta(days=1)
            x = {d: v for d, v in x_all.items() if d <= last.isoformat()}
            tau = T.hour * 60 + T.minute
            e = None
            if tau >= vf.RUN_START_MIN:
                rv = running.at(series, ts)
                if rv is not None:
                    e = vf.run_entry(x, rv, vf.delta_samples(calib(today), series, tau),
                                     D, today, vf.LAB_WIDEN, series)
            else:
                e = vf.fair_entry(x, D, last, vf.LAB_WIDEN, series)
            if e is not None:
                for t in tks:
                    K = vf.strike_of(t)
                    q = at_or_before(quotes.get(t) or [], ts, QUOTE_TOL_S)
                    if K is None or q is None:
                        continue
                    _, b, a = q
                    if not 1.0 <= b < a <= 99.0:
                        continue
                    mid = (a + b) / 2.0
                    fair = 100.0 * vf.p_yes(e, K)
                    if not (MID_LO <= mid <= MID_HI
                            and FAIR_MIN_C <= fair <= 100.0 - FAIR_MIN_C):
                        continue
                    rows.append({"ev": ev, "t": t, "ts": ts, "k": (D - today).days,
                                 "K": K, "mid": mid, "fair": fair,
                                 "y": 1.0 if markets[t]["result"] == "yes" else 0.0})
            T += timedelta(hours=1)
    return rows


# ---------------------------------------------------------------------------
# Kalshi

def settled_markets(series: str, first_d: date, last_d: date) -> Dict[str, dict]:
    """ticker -> {"event", "result"} of the series' settled markets whose
    event measures a day in first_d .. last_d (signed reads via kalshi_reads)."""
    from kalshi_reads import kalshi_get_all
    min_close = int(datetime(first_d.year, first_d.month, first_d.day,
                             tzinfo=timezone.utc).timestamp())
    params = {"series_ticker": series, "status": "settled", "limit": 1000,
              "min_close_ts": min_close}
    try:
        mk = kalshi_get_all("/markets", params)
    except Exception:
        params.pop("min_close_ts")
        mk = kalshi_get_all("/markets", params)
    out: Dict[str, dict] = {}
    for m in mk:
        D = vf.measured_day(m.get("event_ticker") or "")
        if D is None or not first_d <= D <= last_d or m.get("result") not in ("yes", "no"):
            continue
        out[m["ticker"]] = {"event": m["event_ticker"], "result": m["result"]}
    return out


def write_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true", help="print, write nothing")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--series", default="", help="comma list (default: every lab series)")
    a = ap.parse_args(argv)
    t0 = time.time()
    now = datetime.now(timezone.utc)
    today = now.date()
    last_d = today - timedelta(days=1)
    first_d = today - timedelta(days=a.window)
    vf.register(vf.load_specs(SERIES_FILE))
    series = [s.strip() for s in a.series.split(",") if s.strip()] or sorted(vf.LAB_SERIES)
    try:
        tab = vf.fetch_labs(days_back=a.window + max(vf.HISTORY_DAYS, vf.SEASON_DAYS) + 15)
    except Exception as e:
        print(f"vercel-blend-fit: export read failed, nothing written: {e}")
        return 1
    try:
        with open(a.out, encoding="utf-8") as f:
            prev = (json.load(f) or {}).get("series") or {}
    except (OSError, ValueError):
        prev = {}
    quotes = load_quotes(vf.INTRADAY_DIR, first_d - timedelta(days=LEAD_DAYS), last_d,
                         tuple(s + "-" for s in series))
    running = RunningShare(vf.INTRADAY_DIR)
    calib_cache: Dict[date, dict] = {}

    def calib(d: date) -> dict:
        if d not in calib_cache:
            calib_cache[d] = vf.load_calibration(d)
        return calib_cache[d]

    out_series: Dict[str, dict] = {}
    skipped: Dict[str, str] = {}
    for s in series:
        if s not in vf.LAB_SERIES:
            skipped[s] = "not a lab series"
            continue
        key = vf.lab_key(tab, s)
        if key is None:
            skipped[s] = "no export history"
            continue
        try:
            mk = settled_markets(s, first_d, last_d)
        except Exception as e:
            if s in prev:
                out_series[s] = dict(prev[s], carried=True)
            skipped[s] = f"kalshi read failed ({e.__class__.__name__}); previous entry kept"
            continue
        rows = replay_series(s, tab[key], mk, quotes, running, calib)
        fit = fit_rows(rows)
        n_ev = len({r["ev"] for r in rows})
        if fit is None or n_ev < MIN_EVENTS:
            skipped[s] = f"{n_ev} events with rows (< {MIN_EVENTS})"
            continue
        out_series[s] = fit
    data = {"at": now.isoformat(timespec="seconds"), "window_days": a.window,
            "min_events": MIN_EVENTS, "first_d": first_d.isoformat(),
            "last_d": last_d.isoformat(), "series": out_series, "skipped": skipped,
            "runtime_s": round(time.time() - t0, 1)}
    for s, f in sorted(out_series.items()):
        print(f"  {s:14s} w {f['w']:.2f} (raw {f['w_raw']:+.2f}, 90% {f['lo']:+.2f}.."
              f"{f['hi']:+.2f}) events {f['events']:3d} rows {f['rows']:5d} | Brier "
              f"mid {f['brier_mid']:.3f} fair {f['brier_fair']:.3f} blend "
              f"{f['brier_blend']:.3f}" + (" CARRIED" if f.get("carried") else ""))
    for s, why in sorted(skipped.items()):
        print(f"  {s:14s} skipped: {why}")
    if a.dry:
        print(f"vercel-blend-fit: dry run, {len(out_series)} series, nothing written "
              f"({data['runtime_s']}s)")
        return 0
    write_atomic(a.out, data)
    print(f"vercel-blend-fit: wrote {len(out_series)} series to {a.out} "
          f"({data['runtime_s']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
