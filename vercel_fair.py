#!/usr/bin/env python3
"""Vercel AI Gateway pre-D fair values for incentive_mm's Vercel gate.

Jack 2026-09-27: "build the pre-D Vercel gate". The Kalshi Vercel families
settle on one UTC day D of Vercel's official AI Gateway leaderboard data
(vercel.com/docs/ai-gateway/leaderboards; export /api/ai/leaderboard-export,
CC BY 4.0 (c) 2026 Vercel):
  KXOPENVSPEND / KXMOONVSPEND / KXANTHVSPEND   a lab's share of SPEND on D
  KXGOOGVREQ / KXOPENVREQ / KXDEEPVREQ / KXANTHVREQ   ... of REQUESTS on D
      (1 dp, strictly above the strike; ticker DDMMMYY = D, close 03:59Z D+1;
      all 19 numeric settlements reproduce exactly from dataset=labs,
      modality=all)
  KXOPENSOURCESHARE   the open-weights share of TOKENS on D (ticker YYMMMDD =
      D+1, close 14:00Z D+1; reconstructed as the summed token share of the
      open-weight-only labs, within 0.6 pp of the settled values)
The export shows the RUNNING day D live, and the informed flow trades D off
it (makers lost ~8c/contract during D), so the bot quotes these ONLY BEFORE
D: incentive_mm cuts every market at D 00:00Z - IMM_VERCEL_CUTOFF_BEFORE_D_MIN.

Fair value, with T = today (UTC) and k = D - T >= 1 days ahead. Two anchors:

RUNNING (Jack 2026-10-01: "yes anchor the fair on D-1's running share"). The
export carries T's running share all day, and the complete-day anchor
cannot see it (on the D = 9/30 pre-D tape makers lost $392 on trades the
complete-anchor gate left open, $156 under this one; e.g. 9/28 06:18Z, an
80c bid on KXOPENSOURCESHARE-26OCT01-T69 the stale Sunday anchor called
85c, the Monday running share 3c). From RUN_START_MIN after 00:00Z:
    X_D = R_T(tau) + delta + e_k
R_T(tau) the running share now (tau = minutes since 00:00Z); delta the
running-to-final error of the same series at the same tau (final - running)
on each logged complete day (the KL-data vercel-logger, every 2 min), which
keeps the strong hour-of-day biases (DeepSeek requests read ~9 pp high at
01Z and ~2 pp high at 12Z, Anthropic spend ~5 pp low early) and is widened
x RUN_WIDEN about its mean; e_k the empirical k-day changes below. No usable
running block, or fewer than RUN_MIN_DAYS calibration days for a series ->
no entry for it (the gate fails closed): once R_T is public the D-2 anchor
is never a fallback.

COMPLETE (the 9/27 model; before RUN_START_MIN, while T's block is still
degenerate, or with IMM_VERCEL_RUN_ANCHOR=0):
    X_D = X_L + e_h,  L = T - 1 the latest complete day, h = D - L.

e_h ~ the empirical h-day changes x_d - x_{d-h} over the last HISTORY_DAYS
complete days, widened about their median by LAB_WIDEN for the lab series
(the lab backtest was overconfident: fairs of 80-100% settled YES 67% of
the time) and OPEN_WIDEN for KXOPENSOURCESHARE (well calibrated).
P(YES) = P(round1(X_D) > K) = P(X_D >= g - 0.05), g the first 0.1 step
above K.

Reads: the 70-day history (1.4 MB) once per HISTORY_REFRESH_SECS and at
each new UTC day, plus a fresh yesterday + today read (~40 KB) every refresh.

Writes VERCEL_FAIR_FILE: per EVENT ticker (D = today+1 .. today+3) the
series, D, the anchor ("run" / "complete"), its tag (`last`: "<T>@run" or
L), h, the anchor value (`x_l`) and the widened error sample.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

EXPORT = "https://vercel.com/api/ai/leaderboard-export"
UA = {"User-Agent": "KL-IMM-vercel-gate/1.0"}
LAB_SERIES = {
    "KXOPENVSPEND": ("openai", "spend"), "KXMOONVSPEND": ("moonshotai", "spend"),
    "KXANTHVSPEND": ("anthropic", "spend"), "KXGOOGVREQ": ("google", "requests"),
    "KXOPENVREQ": ("openai", "requests"), "KXDEEPVREQ": ("deepseek", "requests"),
    "KXANTHVREQ": ("anthropic", "requests"),
}
OPEN_SERIES = "KXOPENSOURCESHARE"
# labs that ship only open-weight models (the deep-dive's reconstruction)
OPEN_WEIGHT_LABS = ("deepseek", "zai", "moonshotai", "stepfun", "xiaomi", "minimax",
                    "alibaba", "inclusionai", "nvidia", "tencent", "meituan", "arcee-ai")
ALL_SERIES = tuple(LAB_SERIES) + (OPEN_SERIES,)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


HISTORY_DAYS = int(_env_float("IMM_VERCEL_HISTORY_DAYS", 60))
LAB_WIDEN = _env_float("IMM_VERCEL_LAB_WIDEN", 1.5)
OPEN_WIDEN = _env_float("IMM_VERCEL_OPEN_WIDEN", 1.0)
HORIZON_DAYS = int(_env_float("IMM_VERCEL_HORIZON_DAYS", 3))   # D up to today+3
MIN_SAMPLE = 20
HISTORY_REFRESH_SECS = _env_float("IMM_VERCEL_HISTORY_REFRESH_SECS", 6 * 3600)
# D-1 running anchor (2026-10-01); IMM_VERCEL_RUN_ANCHOR=0 restores the
# complete-day anchor all day
RUN_ANCHOR = os.environ.get("IMM_VERCEL_RUN_ANCHOR", "1") == "1"
RUN_START_MIN = _env_float("IMM_VERCEL_RUN_START_MIN", 60)     # after 00:00Z
RUN_MIN_DAYS = int(_env_float("IMM_VERCEL_RUN_MIN_DAYS", 3))
RUN_CALIB_DAYS = int(_env_float("IMM_VERCEL_RUN_CALIB_DAYS", 14))      # newest used
RUN_CALIB_MAX_AGE = int(_env_float("IMM_VERCEL_RUN_CALIB_MAX_AGE", 30))  # days back
RUN_WIDEN = _env_float("IMM_VERCEL_RUN_WIDEN", 1.5)
RUN_TAU_TOL_MIN = _env_float("IMM_VERCEL_RUN_TAU_TOL_MIN", 10)
# a running block is usable once it carries >= RUN_MIN_ROWS rows and >=
# RUN_MIN_ROWS_FRAC of yesterday's (the first 10-50 min of a day carry <= 20)
RUN_MIN_ROWS = 60
RUN_MIN_ROWS_FRAC = 0.8
# a logged day's settled value: its block in the last pass 02:00-14:00Z of
# the next day (frozen by ~01:10Z; Kalshi reads ~14:00Z; requests / tokens
# are revised again from D+2, which settlement never sees)
FINAL_FROM_MIN, FINAL_TO_MIN = 120, 840
INTRADAY_DIR = os.environ.get(
    "IMM_VERCEL_INTRADAY_DIR", r"C:\Users\jackd\Documents\KL-data\vercel-logger\data")
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_MON_NAMES = {v: k for k, v in _MON.items()}
# lab tickers write 5.5 as T5P5, KXOPENSOURCESHARE as T67.5
_STRIKE_RE = re.compile(r"^T(\d+)(?:[P.](\d+))?$")


def measured_day(event_ticker: str) -> Optional[date]:
    """UTC day D an event measures: lab series DDMMMYY = D,
    KXOPENSOURCESHARE YYMMMDD = D + 1. None when it does not parse."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) != 7:
        return None
    seg, series = parts[1], parts[0]
    try:
        if series == OPEN_SERIES:
            d = date(2000 + int(seg[:2]), _MON[seg[2:5]], int(seg[5:7]))
            return d - timedelta(days=1)
        if series in LAB_SERIES:
            return date(2000 + int(seg[5:7]), _MON[seg[2:5]], int(seg[:2]))
    except (KeyError, ValueError):
        return None
    return None


def event_ticker_for(series: str, d: date) -> str:
    if series == OPEN_SERIES:
        e = d + timedelta(days=1)
        return f"{series}-{e.year % 100:02d}{_MON_NAMES[e.month]}{e.day:02d}"
    return f"{series}-{d.day:02d}{_MON_NAMES[d.month]}{d.year % 100:02d}"


def strike_of(ticker: str) -> Optional[float]:
    """'KXANTHVREQ-28SEP26-T5P5' -> 5.5, '...-T21' -> 21.0,
    'KXOPENSOURCESHARE-26SEP29-T67.5' -> 67.5."""
    m = _STRIKE_RE.match(ticker.rsplit("-", 1)[-1])
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def yes_threshold(k: float) -> float:
    """YES iff round1(X) > K (half up)  <=>  X >= g - 0.05, g = the first
    0.1 step above K."""
    g = math.floor(k * 10 + 1e-9) / 10.0 + 0.1
    return g - 0.05


def fetch_labs(now: Optional[datetime] = None, timeout: float = 30,
               days_back: Optional[int] = None) -> Dict[Tuple[str, str], Dict[str, float]]:
    """(lab, metric) -> {date: share_percent} from `days_back` days ago
    (default HISTORY_DAYS+10) through today, the RUNNING day. The plain
    export URL is cached 24 h, so each read uses a distinct documented `to`
    date (the API clamps it to today) for a fresh copy."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    back = HISTORY_DAYS + 10 if days_back is None else days_back
    slot = (now.hour * 60 + now.minute) // 5
    r = requests.get(EXPORT, params={
        "dataset": "labs", "modality": "all",
        "from": (today - timedelta(days=back)).isoformat(),
        "to": (today + timedelta(days=2000 + slot)).isoformat()},
        headers=UA, timeout=timeout)
    r.raise_for_status()
    tab: Dict[Tuple[str, str], Dict[str, float]] = {}
    for x in (r.json() or {}).get("rows") or []:
        if x.get("metric") in ("spend", "requests", "tokens"):
            try:
                tab.setdefault((x["name"], x["metric"]), {})[x["date"]] = float(x["share_percent"])
            except (KeyError, TypeError, ValueError):
                continue
    if not tab:
        raise RuntimeError("Vercel export returned no lab rows")
    return tab


_hist: dict = {"day": None, "at": 0.0, "tab": None}


def fetch_table(now: Optional[datetime] = None, timeout: float = 30
                ) -> Dict[Tuple[str, str], Dict[str, float]]:
    """The history (re-read at each new UTC day and every
    HISTORY_REFRESH_SECS) with today's block replaced by a fresh
    yesterday + today read."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if _hist["tab"] is None or _hist["day"] != today \
            or time.time() - _hist["at"] > HISTORY_REFRESH_SECS:
        tab = fetch_labs(now, timeout)
        _hist.update(tab=tab, day=today, at=time.time())
        return {k: dict(v) for k, v in tab.items()}
    fresh = fetch_labs(now, timeout, days_back=1)
    t_iso = today.isoformat()
    out = {k: {d: x for d, x in v.items() if d != t_iso} for k, v in _hist["tab"].items()}
    for k, v in fresh.items():
        out.setdefault(k, {}).update(v)
    return out


def series_values(tab, series: str, dates: List[str]) -> Dict[str, float]:
    if series in LAB_SERIES:
        v = tab.get(LAB_SERIES[series], {})
        return {d: v.get(d, 0.0) for d in dates}
    return {d: sum(tab.get((lab, "tokens"), {}).get(d, 0.0) for lab in OPEN_WEIGHT_LABS)
            for d in dates}


def block_usable(rows: int, ref_rows: int) -> bool:
    """A day's running block is usable: >= RUN_MIN_ROWS rows and >=
    RUN_MIN_ROWS_FRAC of the reference (yesterday's) block."""
    return rows >= max(RUN_MIN_ROWS, RUN_MIN_ROWS_FRAC * ref_rows)


def block_series(block: dict) -> Dict[str, float]:
    """A logger block {"lab|metric": share} -> {series: value}."""
    out = {s: float(block.get(f"{lab}|{met}") or 0.0)
           for s, (lab, met) in LAB_SERIES.items()}
    out[OPEN_SERIES] = sum(float(block.get(f"{lab}|tokens") or 0.0)
                           for lab in OPEN_WEIGHT_LABS)
    return out


# ---------------------------------------------------------------------------
# running-to-final calibration from the vercel-logger's export files
# (data/export_YYYY-MM-DD.jsonl[.gz], one per UTC day, a pass every 2 min:
# {"t": iso, "labs": {date: {"lab|metric": share}}} for yesterday + today)

_parsed_files: Dict[tuple, dict] = {}


def _day_file(dir_: str, d: date) -> Optional[str]:
    for ext in (".jsonl", ".jsonl.gz"):    # mid-gzip, the plain file is whole
        p = os.path.join(dir_, f"export_{d.isoformat()}{ext}")
        if os.path.exists(p):
            return p
    return None


def parse_logger_day(path: str, day: date, want_run: bool = True) -> dict:
    """One logger file (the passes during `day`) -> {"run": [(minute of day,
    {series: running value})] over the passes whose `day` block is usable,
    "prev_final": {series: value of day-1} from the last pass between
    FINAL_FROM_MIN and FINAL_TO_MIN, or None}."""
    d_iso, p_iso = day.isoformat(), (day - timedelta(days=1)).isoformat()
    run: List[Tuple[float, Dict[str, float]]] = []
    prev = None
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            # '{"t":"2026-09-29T00:00:23+00:00",...' -> skip unparsed lines
            # outside the final window when the running path is not wanted
            if not want_run and line.startswith('{"t":"'):
                try:
                    m0 = int(line[17:19]) * 60 + int(line[20:22])
                except ValueError:
                    m0 = None
                if m0 is not None and not FINAL_FROM_MIN <= m0 <= FINAL_TO_MIN:
                    continue
            try:
                rec = json.loads(line)
                t = datetime.fromisoformat(str(rec["t"]).replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue
            if t.astimezone(timezone.utc).date() != day:
                continue
            t = t.astimezone(timezone.utc)
            mod = t.hour * 60 + t.minute + t.second / 60.0
            labs = rec.get("labs") or {}
            cur, pb = labs.get(d_iso), labs.get(p_iso)
            if want_run and cur and block_usable(len(cur), len(pb or cur)):
                run.append((mod, block_series(cur)))
            if pb and FINAL_FROM_MIN <= mod <= FINAL_TO_MIN:
                prev = block_series(pb)
    return {"run": run, "prev_final": prev}


def _parsed(path: str, day: date, want_run: bool) -> dict:
    key = (path, want_run)
    mtime = os.path.getmtime(path)
    hit = _parsed_files.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    out = parse_logger_day(path, day, want_run)
    _parsed_files[key] = (mtime, out)
    return out


def load_calibration(today: date, dir_: Optional[str] = None) -> Dict[str, dict]:
    """day -> {"final": {series: v}, "run": [(minute, {series: v})]} for the
    newest RUN_CALIB_DAYS complete logged days within RUN_CALIB_MAX_AGE days
    (a day needs its own file and the next day's settled read)."""
    dir_ = dir_ or INTRADAY_DIR
    out: Dict[str, dict] = {}
    if not os.path.isdir(dir_):
        return out
    for back in range(1, RUN_CALIB_MAX_AGE + 1):
        if len(out) >= RUN_CALIB_DAYS:
            break
        j = today - timedelta(days=back)
        nxt = j + timedelta(days=1)
        run_f, fin_f = _day_file(dir_, j), _day_file(dir_, nxt)
        if not run_f or not fin_f:
            continue
        try:
            run = _parsed(run_f, j, True)["run"]
            fin = _parsed(fin_f, nxt, nxt != today)["prev_final"]
        except (OSError, EOFError, ValueError):
            continue        # a file mid-rotation: the next refresh retries
        if run and fin:
            out[j.isoformat()] = {"final": fin, "run": run}
    return out


def delta_samples(calib: Dict[str, dict], series: str, tau: float) -> List[float]:
    """final - running for `series` at the logged pass nearest `tau`
    (minutes since 00:00Z, within RUN_TAU_TOL_MIN), one per calibration day."""
    out = []
    for _day, c in sorted(calib.items()):
        fin = c["final"].get(series)
        best = None
        for m, vals in c["run"]:
            if abs(m - tau) <= RUN_TAU_TOL_MIN and \
                    (best is None or abs(m - tau) < abs(best[0] - tau)):
                best = (m, vals)
        if fin is None or best is None or series not in best[1]:
            continue
        out.append(fin - best[1][series])
    return out


# ---------------------------------------------------------------------------
# the fair

def change_sample(x: Dict[str, float], last: date, h: int) -> List[float]:
    """The empirical h-day changes x_d - x_{d-h}, d over the HISTORY_DAYS
    days ending at `last`."""
    out = []
    for i in range(HISTORY_DAYS):
        de = last - timedelta(days=i)
        ds = de - timedelta(days=h)
        if de.isoformat() in x and ds.isoformat() in x:
            out.append(x[de.isoformat()] - x[ds.isoformat()])
    return out


def fair_entry(x: Dict[str, float], d: date, last: date, widen: float) -> Optional[dict]:
    """COMPLETE anchor: X_L and the widened h-day error sample for measured
    day d, with L the latest complete day (h = d - L >= 1)."""
    h = (d - last).days
    if h < 1 or last.isoformat() not in x:
        return None
    errs = change_sample(x, last, h)
    if len(errs) < MIN_SAMPLE:
        return None
    med = statistics.median(errs)
    wide = sorted(round(med + widen * (e - med), 4) for e in errs)
    return {"x_l": round(x[last.isoformat()], 4), "h": h, "n": len(wide), "errs": wide}


def run_entry(x: Dict[str, float], run_val: float, deltas: List[float], d: date,
              today: date, widen: float) -> Optional[dict]:
    """RUNNING anchor: R_T(tau) and the sample delta + e_k (k = d - today),
    e_k widened about its median by `widen`, delta about its mean by
    RUN_WIDEN; every pairing kept."""
    k = (d - today).days
    if k < 1 or len(deltas) < RUN_MIN_DAYS:
        return None
    ch = change_sample(x, today - timedelta(days=1), k)
    if len(ch) < MIN_SAMPLE:
        return None
    med, mu = statistics.median(ch), statistics.fmean(deltas)
    cw = [med + widen * (e - med) for e in ch]
    dw = [mu + RUN_WIDEN * (v - mu) for v in deltas]
    errs = sorted(round(a + b, 4) for a in cw for b in dw)
    return {"x_l": round(run_val, 4), "h": k, "n": len(errs), "errs": errs,
            "delta_mean": round(mu, 4), "calib_days": len(deltas)}


def p_yes(entry: dict, k: float) -> float:
    """P(round1(X_D) > K) from an entry's anchor + error sample, in [0.01, 0.99]."""
    thr = yes_threshold(k)
    errs = entry["errs"]
    hit = sum(1 for e in errs if entry["x_l"] + e >= thr - 1e-9)
    return min(max(hit / len(errs), 0.01), 0.99)


def write_fair_file(path: str, now: Optional[datetime] = None, tab=None,
                    calib: Optional[Dict[str, dict]] = None) -> Tuple[int, int, str]:
    """Write VERCEL_FAIR_FILE; returns (entries written, series without one,
    mode): "run", "complete", "fail:block" (today's running block not usable
    past RUN_START_MIN) or "fail:calib" (no series has RUN_MIN_DAYS
    calibration days). A fail mode writes no entries, so the gate stands
    every market aside once the previous file ages out."""
    now = now or datetime.now(timezone.utc)
    if tab is None:
        tab = fetch_table(now)
    today = now.date()
    last = today - timedelta(days=1)          # latest COMPLETE UTC day
    t_iso, l_iso = today.isoformat(), last.isoformat()
    dates = sorted({d for v in tab.values() for d in v if d <= l_iso})
    tau = now.hour * 60 + now.minute + now.second / 60.0
    use_run = RUN_ANCHOR and tau >= RUN_START_MIN
    mode = "run" if use_run else "complete"
    n_today = sum(1 for v in tab.values() if t_iso in v)
    n_last = sum(1 for v in tab.values() if l_iso in v)
    deltas: Dict[str, List[float]] = {}
    if use_run:
        if not block_usable(n_today, n_last):
            mode = "fail:block"
        else:
            if calib is None:
                calib = load_calibration(today)
            deltas = {s: delta_samples(calib, s, tau) for s in ALL_SERIES}
            if not any(len(v) >= RUN_MIN_DAYS for v in deltas.values()):
                mode = "fail:calib"
    entries, missing = {}, 0
    for s in ALL_SERIES:
        x = series_values(tab, s, dates)
        widen = OPEN_WIDEN if s == OPEN_SERIES else LAB_WIDEN
        run_val = series_values(tab, s, [t_iso])[t_iso] if mode == "run" else None
        got = 0
        for k in range(1, HORIZON_DAYS + 1):
            d = today + timedelta(days=k)
            if mode == "run":
                e = run_entry(x, run_val, deltas.get(s, []), d, today, widen)
                tag = {"anchor": "run", "last": f"{t_iso}@run", "tau_min": round(tau, 1)}
            elif mode == "complete":
                e = fair_entry(x, d, last, widen)
                tag = {"anchor": "complete", "last": l_iso}
            else:
                e = None
            if e is None:
                continue
            entries[event_ticker_for(s, d)] = dict(
                e, **tag, series=s, d=d.isoformat(),
                fetched_at=now.isoformat(timespec="seconds"))
            got += 1
        missing += int(got == 0)
    data = {"at": now.isoformat(timespec="seconds"),
            "model": {"history_days": HISTORY_DAYS, "lab_widen": LAB_WIDEN,
                      "open_widen": OPEN_WIDEN, "horizon_days": HORIZON_DAYS,
                      "open_weight_labs": list(OPEN_WEIGHT_LABS),
                      "run_anchor": RUN_ANCHOR, "run_start_min": RUN_START_MIN,
                      "run_min_days": RUN_MIN_DAYS, "run_widen": RUN_WIDEN,
                      "run_calib_days": RUN_CALIB_DAYS,
                      "run_tau_tol_min": RUN_TAU_TOL_MIN},
            "anchor": {"mode": mode, "tau_min": round(tau, 1),
                       "rows_today": n_today, "rows_last": n_last,
                       "calib_days": sorted(calib) if calib else [],
                       "delta_n": {s: len(v) for s, v in deltas.items()}},
            "entries": entries}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return len(entries), missing, mode


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "vercel_fair.json"
    print(write_fair_file(out))
    if "--explain" in sys.argv:
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        print(json.dumps(data["anchor"]))
        for ev, e in sorted(data["entries"].items()):
            mid = e["x_l"] + statistics.median(e["errs"])
            print(f"{ev:28s} {e['anchor']:8s} x {e['x_l']:7.3f} h {e['h']} "
                  f"median X_D {mid:7.3f} n {e['n']}"
                  + (f" delta {e['delta_mean']:+.2f} ({e['calib_days']}d)"
                     if e["anchor"] == "run" else ""))
