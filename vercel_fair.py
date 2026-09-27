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

Fair value (the deep-dive's backtested pre-D model): while quoting on D-1 the
latest COMPLETE day is L = D-2 (D-1 is still running), so
    X_D = X_L + e,  e ~ the empirical h-day changes x_d - x_{d-h} (h = D - L)
over the last HISTORY_DAYS complete days, widened about their median by
LAB_WIDEN for the lab series (the lab backtest was overconfident: fairs of
80-100% settled YES 67% of the time) and OPEN_WIDEN for KXOPENSOURCESHARE
(well calibrated). P(YES) = P(round1(X_D) > K) = P(X_D >= g - 0.05), g the
first 0.1 step above K.

Writes VERCEL_FAIR_FILE: per EVENT ticker (D = today+1 .. today+3) the
series, D, L, h, X_L and the widened error sample.
"""
from __future__ import annotations

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


def fetch_labs(now: Optional[datetime] = None, timeout: float = 30) -> Dict[Tuple[str, str], Dict[str, float]]:
    """(lab, metric) -> {date: share_percent} for the last HISTORY_DAYS+10
    days. The plain export URL is cached 24 h, so each read uses a distinct
    documented `to` date (the API clamps it to today) for a fresh copy."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    slot = (now.hour * 60 + now.minute) // 5
    r = requests.get(EXPORT, params={
        "dataset": "labs", "modality": "all",
        "from": (today - timedelta(days=HISTORY_DAYS + 10)).isoformat(),
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


def series_values(tab, series: str, dates: List[str]) -> Dict[str, float]:
    if series in LAB_SERIES:
        v = tab.get(LAB_SERIES[series], {})
        return {d: v.get(d, 0.0) for d in dates}
    return {d: sum(tab.get((lab, "tokens"), {}).get(d, 0.0) for lab in OPEN_WEIGHT_LABS)
            for d in dates}


def fair_entry(x: Dict[str, float], d: date, last: date, widen: float) -> Optional[dict]:
    """X_L and the widened h-day error sample for measured day d, with L
    the latest complete day (h = d - L >= 1)."""
    h = (d - last).days
    if h < 1 or last.isoformat() not in x:
        return None
    errs = []
    for i in range(HISTORY_DAYS):
        de = last - timedelta(days=i)
        ds = de - timedelta(days=h)
        if de.isoformat() in x and ds.isoformat() in x:
            errs.append(x[de.isoformat()] - x[ds.isoformat()])
    if len(errs) < MIN_SAMPLE:
        return None
    med = statistics.median(errs)
    wide = sorted(round(med + widen * (e - med), 4) for e in errs)
    return {"x_l": round(x[last.isoformat()], 4), "h": h, "n": len(wide), "errs": wide}


def p_yes(entry: dict, k: float) -> float:
    """P(round1(X_D) > K) from an entry's X_L + error sample, in [0.01, 0.99]."""
    thr = yes_threshold(k)
    errs = entry["errs"]
    hit = sum(1 for e in errs if entry["x_l"] + e >= thr - 1e-9)
    return min(max(hit / len(errs), 0.01), 0.99)


def write_fair_file(path: str, now: Optional[datetime] = None,
                    tab=None) -> Tuple[int, int]:
    """Write VERCEL_FAIR_FILE; returns (entries written, series without one)."""
    now = now or datetime.now(timezone.utc)
    if tab is None:
        tab = fetch_labs(now)
    today = now.date()
    last = today - timedelta(days=1)          # latest COMPLETE UTC day
    dates = sorted({d for v in tab.values() for d in v if d <= last.isoformat()})
    entries, missing = {}, 0
    for s in ALL_SERIES:
        x = series_values(tab, s, dates)
        widen = OPEN_WIDEN if s == OPEN_SERIES else LAB_WIDEN
        got = 0
        for k in range(1, HORIZON_DAYS + 1):
            d = today + timedelta(days=k)
            e = fair_entry(x, d, last, widen)
            if e is None:
                continue
            entries[event_ticker_for(s, d)] = dict(
                e, series=s, d=d.isoformat(), last=last.isoformat(),
                fetched_at=now.isoformat(timespec="seconds"))
            got += 1
        missing += int(got == 0)
    data = {"at": now.isoformat(timespec="seconds"),
            "model": {"history_days": HISTORY_DAYS, "lab_widen": LAB_WIDEN,
                      "open_widen": OPEN_WIDEN, "horizon_days": HORIZON_DAYS,
                      "open_weight_labs": list(OPEN_WEIGHT_LABS)},
            "entries": entries}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return len(entries), missing


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "vercel_fair.json"
    print(write_fair_file(out))
