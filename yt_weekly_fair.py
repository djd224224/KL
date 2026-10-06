#!/usr/bin/env python3
"""YouTube weekly artist-views fair values for incentive_mm's KXYTVIEWSW pilot.

Jack 2026-10-06: "consider quoting KATSEYE, Drake and The Weeknd with realtime
data", then "live now". KXYTVIEWSW-<CODE><YYMMMDD>-<K>M asks whether the
artist has "above K daily views at any point during" the seven UTC chart days
ending on the ticker date: the MAX of the artist's daily Global views on
charts.youtube.com that week. Each day prints ~24-46 h after it ends; Kalshi
closes a strike a print crosses YES early (expiration_value = that print) and
closes the event ~14:00Z two days after the week.

WHY THREE ARTISTS (KL-data/youtube-analysis-2026-10-05, the 10/06 check-in):
on the public tape of the 3 complete weeks makers made +4.0 c/ct on KATSEYE /
Drake / The Weeknd (9 artist-weeks, CI -2.8/+8.9) -- -20.4 c/ct in the last 2 h
before the close (takers buying YES 99.8%: the final print sniped) and +9.4
c/ct on everything earlier -- while every other artist but Fuerza Regida lost
(Ariana -26.7). The losses are print snipes, which an API nowcast sees coming.

THE MODEL (each piece validated in that folder; MODELLED):
  API views: the "KL-data youtube-collect" artist collector snapshots every
    tracked video of each artist's official + Topic channels hourly
    (yt_artist_snapshots.jsonl); read incrementally here. An artist's API views
    over [T0, T1) = the sum over videos of the interpolated view-count delta
    (a video first seen after T0 back-extrapolated <= 4 h, the last snapshot
    forward-extrapolated <= 1.6 h).
  Chart day D <-> the API window [D 15:00Z, D+1 15:00Z) (WINDOW_H; the most
    stable chart/API ratio on all three clean day-pairs).
  ratio r = the median of chart(L) / API(L) over the last RATIO_N (3) days L
    with a known print (the chart history in CHART_FILE, extended by every
    print Kalshi reveals and this module matches to its day). Its error: the recent CV plus
    RATIO_DRIFT_PER_DAY x days from L (Drake's and The Weeknd's ratios fell
    ~1%/day 9/28 -> 10/02).
  Each day of the week:
    print     known (the chart file or a matched Kalshi YES-early value)
    complete  window over: r x API(D), sd SIGMA_NOW[artist] (15h-window first
              print errors: The Weeknd ~1.7%, KATSEYE ~3.6%, Drake ~3.4% RMS)
    partial   window running k h: r x API so far / PROFILE(k) (the pooled
              intraday profile), sd PARTIAL_SD(k) (11-14% in the first 3 h,
              ~3% after 10 h)
    future    a random walk from the latest estimate: weekday factors and
              bootstrapped de-seasonalized daily log changes of the artist's
              chart-equivalent series (last HISTORY_DAYS), x FUTURE_WIDEN
  A complete day more than PUBLISHED_AFTER_H after its UTC end with no revealed
  print has printed below every strike still open (a print above one closes it
  YES), so its draws are capped at the lowest open strike.
  P(YES) = P(max(revealed max, every day's draw) > K) over N_SIM joint draws
  (one shared ratio error, independent per-day noise).
  HOLD (the print-snipe guard): a strike within HOLD_WIDTHS local ladder
  spacings of a running or complete-but-unprinted day's estimate (<=
  HOLD_RECENT_H after the day's UTC end) is flagged "print" -- the gate stands
  it aside until that print is known.

FAIL CLOSED: snapshots older than API_STALE_SECS -> no entries at all; an
artist without a ratio inside RATIO_MAX_AGE_DAYS, or whose event read failed,
-> no entries for it. The gate stands aside anything without a fresh entry.

Writes FAIR_FILE: {"at", "api_last", "status", "entries": {event: {"artist",
"week_end", "fetched_at", "ratio", "ratio_day", "m_pub", "k_min_open", "days":
{date: [kind, est, sd]}, "strikes": {"21.0M": {"k", "p", "hold"}}}}}.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
import re
import statistics
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_map(name: str, default: str) -> Dict[str, str]:
    out = {}
    for part in os.environ.get(name, default).split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


SERIES = "KXYTVIEWSW"
# ticker code -> the artist's name on YouTube Charts (and in the collector)
# Tate McRae added 2026-10-06 (Jack: "yes add her")
ARTISTS = _env_map("IMM_YTW_ARTISTS", "KAT:KATSEYE,DRA:Drake,WEE:The Weeknd,TAT:Tate McRae")
SNAP_FILE = os.environ.get(
    "IMM_YTW_SNAP_FILE",
    r"C:\Users\jackd\Documents\KL-data\youtube-collect\yt_artist_snapshots.jsonl")
CHART_FILE = os.environ.get(
    "IMM_YTW_CHART_FILE",
    r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm\yt_weekly_chart.json")
WINDOW_H = int(_env_float("IMM_YTW_WINDOW_H", 15))
API_STALE_SECS = _env_float("IMM_YTW_API_STALE_SECS", 9000)
SIGMA_NOW = {k: float(v) for k, v in _env_map(
    "IMM_YTW_SIGMA_NOW", "KATSEYE:0.045,Drake:0.04,The Weeknd:0.025,Tate McRae:0.025").items()}
SIGMA_NOW_DEFAULT = 0.045
RATIO_DRIFT_PER_DAY = _env_float("IMM_YTW_RATIO_DRIFT_PER_DAY", 0.01)
RATIO_CV_MIN = 0.015
RATIO_N = int(_env_float("IMM_YTW_RATIO_N", 3))
RATIO_MAX_AGE_DAYS = _env_float("IMM_YTW_RATIO_MAX_AGE_DAYS", 14)
FUTURE_WIDEN = _env_float("IMM_YTW_FUTURE_WIDEN", 1.25)
HISTORY_DAYS = 35
N_SIM = int(_env_float("IMM_YTW_N_SIM", 4000))
HOLD_WIDTHS = _env_float("IMM_YTW_HOLD_WIDTHS", 1.5)
HOLD_RECENT_H = _env_float("IMM_YTW_HOLD_RECENT_H", 50)
PUBLISHED_AFTER_H = _env_float("IMM_YTW_PUBLISHED_AFTER_H", 50)
PRINT_MATCH_TOL = 0.12           # |log(print / nowcast)| to accept a day match
PRINT_LAG_H = (18.0, 72.0)       # a revealed print lands this long after its day's UTC end
# pooled intraday API profile (cumulative share of the window's views after k h)
PROFILE = [(0, 0.0), (3, 0.146), (6, 0.31), (9, 0.462), (12, 0.592), (15, 0.696),
           (18, 0.794), (21, 0.892), (24, 1.0)]
# partial-window nowcast sd by hours in (pooled MAE% x 1.25)
PARTIAL_SD = [(1, 0.16), (3, 0.16), (4, 0.072), (5, 0.062), (6, 0.053), (9, 0.042),
              (12, 0.033), (24, 0.033)]
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_EVENT_RE = re.compile(r"^KXYTVIEWSW-([A-Z]+)(\d{2})([A-Z]{3})(\d{2})$")
_STRIKE_RE = re.compile(r"^(\d+(?:\.\d+)?)M$")


# ---------------------------------------------------------------------------
# tickers

def parse_event(event_ticker: str) -> Optional[Tuple[str, date]]:
    """'KXYTVIEWSW-KAT26OCT11' -> ('KAT', 2026-10-11): the code and the week's
    last chart day. None when it does not parse."""
    m = _EVENT_RE.match(event_ticker or "")
    if not m or m.group(3) not in _MON:
        return None
    try:
        return m.group(1), date(2000 + int(m.group(2)), _MON[m.group(3)], int(m.group(4)))
    except ValueError:
        return None


def strike_key(ticker: str) -> Optional[str]:
    """'KXYTVIEWSW-KAT26OCT11-21.0M' -> '21.0M'."""
    k = (ticker or "").rsplit("-", 1)[-1]
    return k if _STRIKE_RE.match(k) else None


def strike_views(key: str) -> Optional[float]:
    m = _STRIKE_RE.match(key or "")
    return float(m.group(1)) * 1e6 if m else None


def day0(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# API snapshots (the collector's jsonl, read incrementally)

_snap: dict = {"path": None, "offset": 0, "partial": b"", "series": {}, "last_ts": 0.0}


def reset_snapshots() -> None:
    _snap.update(path=None, offset=0, partial=b"", series={}, last_ts=0.0)


def load_snapshots(path: Optional[str] = None, names: Optional[set] = None,
                   chunk: int = 16 << 20) -> Tuple[Dict[str, Dict[str, Tuple[List[float], List[float]]]], float]:
    """{artist: {video: ([ts], [views])}} for the pilot artists, and the newest
    snapshot time. Only the bytes appended since the last call are read."""
    path = path or SNAP_FILE
    names = names if names is not None else set(ARTISTS.values())
    if _snap["path"] != path:
        reset_snapshots()
        _snap["path"] = path
    try:
        size = os.path.getsize(path)
    except OSError:
        return _snap["series"], _snap["last_ts"]
    if size < _snap["offset"]:               # truncated / replaced: start over
        reset_snapshots()
        _snap["path"] = path
    keys = [('"artist": %s' % json.dumps(n)).encode("utf-8") for n in names]
    with open(path, "rb") as f:
        f.seek(_snap["offset"])
        while True:
            block = f.read(chunk)
            if not block:
                break
            _snap["offset"] += len(block)
            lines = (_snap["partial"] + block).split(b"\n")
            _snap["partial"] = lines.pop()
            for ln in lines:
                if not any(k in ln for k in keys):
                    continue
                try:
                    x = json.loads(ln)
                    a, vid = x["artist"], x["id"]
                    ts, v = float(x["ts"]), float(x["views"])
                except (ValueError, KeyError, TypeError):
                    continue
                t, y = _snap["series"].setdefault(a, {}).setdefault(vid, ([], []))
                if t and ts <= t[-1]:
                    continue
                t.append(ts)
                y.append(v)
                if ts > _snap["last_ts"]:
                    _snap["last_ts"] = ts
    return _snap["series"], _snap["last_ts"]


def _val(t: List[float], y: List[float], T: float, back_max: float = 4 * 3600,
         fwd_max: float = 1.6 * 3600) -> Tuple[Optional[float], bool]:
    """One video's cumulative views at T (linear interpolation; a short
    extrapolation past either end), and whether T precedes its first snapshot."""
    if not t:
        return None, False
    if T < t[0]:
        if t[0] - T > back_max or len(t) < 2:
            return None, True
        j = min(len(t) - 1, max(1, bisect.bisect_left(t, t[0] + 3 * 3600)))
        rate = (y[j] - y[0]) / (t[j] - t[0]) if t[j] > t[0] else 0.0
        return max(0.0, y[0] - rate * (t[0] - T)), True
    if T > t[-1]:
        if T - t[-1] > fwd_max or len(t) < 2:
            return None, False
        k = max(0, bisect.bisect_left(t, t[-1] - 3 * 3600))
        rate = (y[-1] - y[k]) / (t[-1] - t[k]) if t[-1] > t[k] else 0.0
        return y[-1] + rate * (T - t[-1]), False
    i = bisect.bisect_left(t, T)
    if t[i] == T:
        return y[i], False
    return y[i - 1] + (y[i] - y[i - 1]) * (T - t[i - 1]) / (t[i] - t[i - 1]), False


def api_window(videos: Dict[str, Tuple[List[float], List[float]]], T0: float,
               T1: float, min_cover: float = 0.5) -> Optional[float]:
    """The artist's API views over [T0, T1): the sum of every covered video's
    delta. None when fewer than min_cover of the videos cover the window."""
    tot, n = 0.0, 0
    for t, y in videos.values():
        v0, _late = _val(t, y, T0)
        v1, _ = _val(t, y, T1)
        if v0 is None or v1 is None:
            continue
        tot += v1 - v0
        n += 1
    if not videos or n < min_cover * len(videos):
        return None
    return tot


def _interp(table: List[Tuple[float, float]], x: float) -> float:
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


# ---------------------------------------------------------------------------
# chart history + the ratio

def load_chart(path: Optional[str] = None) -> Dict[str, Dict[str, int]]:
    path = path or CHART_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return {a: {d: int(v) for d, v in s.items()}
                    for a, s in (json.load(f).get("chart") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_chart(chart: Dict[str, Dict[str, int]], path: Optional[str] = None) -> None:
    path = path or CHART_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data["chart"] = {a: dict(sorted(s.items())) for a, s in chart.items()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def window_bounds(d: date) -> Tuple[float, float]:
    t0 = day0(d) + WINDOW_H * 3600
    return t0, t0 + 86400


def ratios(videos, chart_a: Dict[str, int], today: date) -> Dict[date, float]:
    """chart / API for every day of the last RATIO_MAX_AGE_DAYS with a print
    and a covered window."""
    out = {}
    for ds, v in chart_a.items():
        d = date.fromisoformat(ds)
        if (today - d).days > RATIO_MAX_AGE_DAYS or d >= today:
            continue
        A = api_window(videos, *window_bounds(d))
        if A and A > 0:
            out[d] = v / A
    return out


# ---------------------------------------------------------------------------
# the week

def day_estimates(videos, last_ts: float, week: List[date], prints: Dict[date, int],
                  r: float, r_day: date, r_cv: float, sigma_now: float) -> Dict[date, list]:
    """[kind, est, sd_ind, sd_ratio] per day: print / complete / partial / future."""
    out = {}
    for d in week:
        if d in prints:
            out[d] = ["print", float(prints[d]), 0.0, 0.0]
            continue
        T0, T1 = window_bounds(d)
        sd_r = math.hypot(r_cv, RATIO_DRIFT_PER_DAY * max(0, (d - r_day).days))
        if last_ts >= T1 - 600:
            A = api_window(videos, T0, T1)
            out[d] = (["complete", r * A, sigma_now, sd_r] if A and A > 0
                      else ["future", None, None, None])
        elif last_ts >= T0 + 3600:
            k = (last_ts - T0) / 3600.0
            P = api_window(videos, T0, last_ts)
            F = _interp(PROFILE, k)
            out[d] = (["partial", r * P / F, max(sigma_now, _interp(PARTIAL_SD, k)), sd_r]
                      if P and P > 0 and F > 0 else ["future", None, None, None])
        else:
            out[d] = ["future", None, None, None]
    return out


def weekday_model(series: Dict[date, float]) -> Tuple[Dict[int, float], List[float]]:
    """(log weekday factor by weekday, de-seasonalized daily log changes)."""
    logs = {d: math.log(v) for d, v in series.items() if v > 0}
    rel: Dict[int, List[float]] = {}
    for d, lv in logs.items():
        win = [logs.get(d + timedelta(days=j)) for j in range(-3, 4)]
        if any(x is None for x in win):
            continue
        rel.setdefault(d.weekday(), []).append(lv - statistics.fmean(win))
    if len(rel) == 7:
        m = statistics.fmean(statistics.fmean(v) for v in rel.values())
        w = {k: statistics.fmean(v) - m for k, v in rel.items()}
    else:
        w = {k: 0.0 for k in range(7)}
    ch = []
    for d, lv in logs.items():
        p = logs.get(d - timedelta(days=1))
        if p is not None:
            ch.append((lv - w[d.weekday()]) - (p - w[(d - timedelta(days=1)).weekday()]))
    return w, ch


def local_spacing(strikes: List[float], k: float) -> float:
    ks = sorted(set(strikes))
    near = [x for x in ks if 0.8 * k <= x <= 1.2 * k]
    gaps = [b - a for a, b in zip(near, near[1:])]
    if gaps:
        return statistics.median(gaps)
    gaps = [b - a for a, b in zip(ks, ks[1:])]
    return statistics.median(gaps) if gaps else 0.05 * k


def simulate(days: List[date], in_week: set, est: Dict[date, list], m_pub: float,
             k_min_open: Optional[float], now_ts: float, w: Dict[int, float], changes: List[float],
             strikes: Dict[str, float], seed: int) -> Dict[str, float]:
    """P(week max > K) per strike key over N_SIM joint draws. `days` runs in
    order from before the week (anchors for the future-day walk) to its last
    day; only `in_week` days enter the max."""
    rng = random.Random(seed)
    hits = {k: 0 for k in strikes}
    ks = sorted(strikes.items(), key=lambda kv: kv[1])
    for _ in range(N_SIM):
        z_r = rng.gauss(0.0, 1.0)
        mx = m_pub
        prev_d, prev_v = None, None
        for d in days:
            e = est[d]
            if e[0] == "print":
                v = e[1]
            elif e[0] in ("complete", "partial"):
                v = e[1] * math.exp(z_r * e[3] + rng.gauss(0.0, e[2]))
                if e[0] == "complete" and d in in_week and k_min_open is not None                         and now_ts - day0(d + timedelta(days=1)) > PUBLISHED_AFTER_H * 3600:
                    v = min(v, k_min_open)        # printed below every open strike
            else:
                if prev_v is None:
                    continue
                v = prev_v
                for j in range(1, (d - prev_d).days + 1):
                    a = prev_d + timedelta(days=j - 1)
                    b = prev_d + timedelta(days=j)
                    step = rng.choice(changes) if changes else 0.0
                    v *= math.exp(w[b.weekday()] - w[a.weekday()] + FUTURE_WIDEN * step)
            prev_d, prev_v = d, v
            if d in in_week and v > mx:
                mx = v
        for key, k in ks:
            if mx > k:
                hits[key] += 1
            else:
                break
    return {k: hits[k] / N_SIM for k in strikes}


def match_prints(revealed: List[Tuple[float, float]], est: Dict[date, list], week: List[date]) -> Dict[date, int]:
    """Assign each revealed (value, close_ts) to the week day it printed: the
    complete day whose UTC end lies PRINT_LAG_H before the close and whose
    nowcast is nearest (within PRINT_MATCH_TOL in log terms)."""
    out: Dict[date, int] = {}
    for v, t in sorted(revealed, key=lambda x: x[1]):
        best = None
        for d in week:
            e = est.get(d)
            if not e or e[0] not in ("complete", "print") or d in out:
                continue
            lag = (t - day0(d + timedelta(days=1))) / 3600.0
            if not PRINT_LAG_H[0] <= lag <= PRINT_LAG_H[1]:
                continue
            err = abs(math.log(v / e[1])) if e[1] and e[1] > 0 else 9.0
            if err <= PRINT_MATCH_TOL and (best is None or err < best[0]):
                best = (err, d)
        if best is not None:
            out[best[1]] = int(v)
    return out


def _parse_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


EXTEND_DAYS = 14        # days before the week priced too: anchors + history for the walk


def event_fair(event: str, markets: List[dict], videos, last_ts: float, chart_a: Dict[str, int],
               artist: str, now_ts: float) -> Tuple[Optional[dict], Dict[date, int]]:
    """One event's entry (None = fail closed) and newly matched prints."""
    pe = parse_event(event)
    if pe is None:
        return None, {}
    week_end = pe[1]
    week = [week_end - timedelta(days=6 - i) for i in range(7)]
    days = [week[0] - timedelta(days=EXTEND_DAYS - i) for i in range(EXTEND_DAYS)] + week
    today = datetime.fromtimestamp(now_ts, timezone.utc).date()
    rs = ratios(videos, chart_a, today)
    if not rs:
        return None, {}
    # the median of the last RATIO_N ratios (the validated NOWCAST3 form): a
    # single day's ratio is noisy (The Weeknd's 10/02 sat 3% under its
    # 9/27-10/01 run); its day anchors the drift allowance
    last_n = sorted(rs)[-RATIO_N:]
    r = statistics.median(rs[d] for d in last_n)
    r_day = last_n[len(last_n) // 2]
    recent = [math.log(rs[d]) for d in sorted(rs)[-5:]]
    r_cv = max(RATIO_CV_MIN, statistics.pstdev(recent) if len(recent) > 1 else RATIO_CV_MIN)
    sigma_now = SIGMA_NOW.get(artist, SIGMA_NOW_DEFAULT)
    strikes: Dict[str, float] = {}
    revealed: List[Tuple[float, float]] = []
    k_open: List[float] = []
    for m in markets:
        key = strike_key(m.get("ticker", ""))
        if key is None:
            continue
        k = m.get("floor_strike")
        k = float(k) if k not in (None, "") else strike_views(key)
        status = str(m.get("status") or "")
        if m.get("result") == "yes" or status in ("closed", "settled", "finalized", "determined"):
            ev = m.get("expiration_value")
            ct = _parse_ts(m.get("close_time") or "")
            if m.get("result") == "yes" and ev not in (None, "") and ct:
                try:
                    revealed.append((float(ev), ct))
                except ValueError:
                    pass
            continue
        strikes[key] = k
        k_open.append(k)
    if not strikes:
        return None, {}
    known_all = {date.fromisoformat(d): v for d, v in chart_a.items()}
    in_range = set(days)
    known = {d: v for d, v in known_all.items() if d in in_range}
    est = day_estimates(videos, last_ts, days, known, r, r_day, r_cv, sigma_now)
    new_prints = match_prints(revealed, est, week)
    if new_prints:
        known.update(new_prints)
        est = day_estimates(videos, last_ts, days, known, r, r_day, r_cv, sigma_now)
    m_pub = max([v for v, _t in revealed] + [0.0])
    k_min_open = min(k_open) if k_open else None
    # the future-day walk: weekday factors + changes of the chart-equivalent
    # series (prints, then complete-window estimates) before the first future day
    futures = [d for d in days if est[d][0] == "future"]
    ref = futures[0] if futures else week_end + timedelta(days=1)
    series = {d: float(v) for d, v in known_all.items()}
    for d, e in est.items():
        if e[0] == "complete" and d not in series:
            series[d] = e[1]
    hist = {d: v for d, v in series.items() if ref - timedelta(days=HISTORY_DAYS) <= d < ref}
    if any(d in week for d in futures) and not any(
            est[d][0] != "future" for d in days if futures and d < futures[0]):
        return None, new_prints                   # nothing to walk from: fail closed
    w, changes = weekday_model(hist)
    seed = zlib.crc32(f"{event}|{int(now_ts // 3600)}".encode()) & 0x7FFFFFFF
    ps = simulate(days, set(week), est, m_pub, k_min_open, now_ts, w, changes, strikes, seed)
    # the print-snipe hold
    holds = {}
    allk = list(strikes.values())
    for key, k in strikes.items():
        sp = local_spacing(allk, k)
        why = ""
        for d in week:
            e = est[d]
            if e[0] not in ("complete", "partial"):
                continue
            if e[0] == "complete" and now_ts - day0(d + timedelta(days=1)) > HOLD_RECENT_H * 3600:
                continue
            if abs(e[1] - k) <= HOLD_WIDTHS * sp:
                why = f"{d.isoformat()} {e[0]} est {e[1] / 1e6:.2f}M within {HOLD_WIDTHS:g} strikes"
                break
        holds[key] = why
    entry = {"artist": artist, "week_end": week_end.isoformat(),
             "ratio": round(r, 5), "ratio_day": r_day.isoformat(), "ratio_cv": round(r_cv, 4),
             "m_pub": m_pub, "k_min_open": k_min_open,
             "days": {d.isoformat(): [est[d][0], None if est[d][1] is None else round(est[d][1]),
                                      None if est[d][2] is None else round(math.hypot(est[d][2], est[d][3]), 4)]
                      for d in week},
             "strikes": {key: {"k": strikes[key], "p": round(ps[key], 4), "hold": holds[key]}
                         for key in strikes}}
    return entry, new_prints


def default_reads() -> Tuple[Callable, Callable]:
    """(list open pilot events, all markets of one event) on Kalshi, signed."""
    from kalshi_reads import kalshi_get_all

    def open_events() -> List[str]:
        ms = kalshi_get_all("/markets", {"series_ticker": SERIES, "status": "open", "limit": 1000},
                            items_key="markets")
        return sorted({m["event_ticker"] for m in ms})

    def event_markets(ev: str) -> List[dict]:
        return kalshi_get_all("/markets", {"event_ticker": ev, "limit": 200}, items_key="markets")
    return open_events, event_markets


def write_fair_file(path: str, now: Optional[datetime] = None, reads: Optional[Tuple[Callable, Callable]] = None,
                    snap_path: Optional[str] = None, chart_path: Optional[str] = None) -> Tuple[int, int, str]:
    """Write FAIR_FILE; returns (events written, pilot events without an
    entry, status). Status "stale_api" writes no entries (the gate stands
    every market aside once the previous file ages out)."""
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    series, last_ts = load_snapshots(snap_path)
    chart = load_chart(chart_path)
    entries: Dict[str, dict] = {}
    missing = 0
    status = "ok"
    if now_ts - last_ts > API_STALE_SECS:
        status = "stale_api"
    else:
        open_events, event_markets = reads or default_reads()
        evs = []
        for ev in open_events():
            pe = parse_event(ev)
            if pe and pe[0] in ARTISTS:
                evs.append((ev, pe[0]))
        added = False
        for ev, code in evs:
            artist = ARTISTS[code]
            try:
                e, new_prints = event_fair(ev, event_markets(ev), series.get(artist, {}), last_ts,
                                           chart.get(artist, {}), artist, now_ts)
            except Exception:
                e, new_prints = None, {}
            if e is None:
                missing += 1
                continue
            e["fetched_at"] = now.isoformat(timespec="seconds")
            entries[ev] = e
            for d, v in new_prints.items():
                if chart.setdefault(artist, {}).get(d.isoformat()) is None:
                    chart[artist][d.isoformat()] = int(v)
                    added = True
        if added:
            save_chart(chart, chart_path)
    data = {"at": now.isoformat(timespec="seconds"),
            "api_last": datetime.fromtimestamp(last_ts, timezone.utc).isoformat(timespec="seconds") if last_ts else None,
            "status": status,
            "model": {"artists": ARTISTS, "window_h": WINDOW_H, "sigma_now": SIGMA_NOW,
                      "ratio_drift_per_day": RATIO_DRIFT_PER_DAY, "future_widen": FUTURE_WIDEN,
                      "hold_widths": HOLD_WIDTHS, "hold_recent_h": HOLD_RECENT_H,
                      "published_after_h": PUBLISHED_AFTER_H, "n_sim": N_SIM},
            "entries": entries}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return len(entries), missing, status


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "yt_weekly_fair.json"
    t0 = time.time()
    print(write_fair_file(out), f"{time.time() - t0:.1f}s")
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    print("api_last", data["api_last"], "status", data["status"])
    for ev, e in sorted(data["entries"].items()):
        print(ev, "ratio", e["ratio"], "@", e["ratio_day"], "m_pub", e["m_pub"], "k_min_open", e["k_min_open"])
        print("   days:", {d[5:]: (x[0][0], None if x[1] is None else round(x[1] / 1e6, 2), x[2]) for d, x in e["days"].items()})
        for key, s in sorted(e["strikes"].items(), key=lambda kv: kv[1]["k"]):
            print(f"   {key:>7s} p {s['p']:.3f} {s['hold']}")
