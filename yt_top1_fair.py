#!/usr/bin/env python3
"""YouTube Global daily #1 music video: P(each listed video ranks #1) from
live API view counts, for incentive_mm's #1 leader-bid pilot (YT1_*).

Jack 2026-10-14: "build the #1 leader bid strategy". KXYTDAILYTOPVIDEOG-
<YYMMMDD>-<CODE>: "Will <video> rank #1 on YouTube Charts' Daily Top Music
Videos Global chart dated <day>?" (the chart day is the UTC day; it prints
~1-2 days later; Kalshi closes the event ~03:59Z the next day).

WHY (KL-data/youtube-analysis-2026-10-14-topvid2/verdict.md): over 13 chart
days (9/28-10/12) a view-count nowcast named the eventual #1 by hour 3 on 10
of them, while the market priced the eventual #1 at 56-64c through midday --
the leader is underpriced. The pilot bids the leader, never offers it.

THE MODEL (each piece measured in KL-data/youtube-analysis-2026-10-14-top1):
  views    the 20-min viewCount snapshots of SNAP_FILES -- the research
           collector's yt_snapshots.jsonl (to 2026-10-18 12:00Z) and the
           durable feed's yt_topvid_snapshots.jsonl (yt_topvid_feed.py, task
           "KL yt-topvid-feed") -- merged per video.
  window   chart day D <-> the API window [D 00:00Z + LAG_H, +24 h): the API
           lags the chart's clock ~1-2 h (the 10/05 study, #1 videos).
  nowcast  h hours into the window: views so far / the video's own median
           share of its day by h over its previous 3 days (its audience's
           clock -- Indian videos run ahead of Latin American ones in UTC);
           the median of the top videos' shares for a video without history.
           Error (log sd, top-20 videos, 10/01-10/13): ~0.22 to h4, ~0.3 at
           h5-8, 0.18 at h12, 0.12 at h16, 0.04 at h18, 0.02 by h20 (SIGMA_PROJ).
  ratio    chart / API per video from the 10/05 study's prints to 10/03
           (RATIO_FILE, windows starting 01-02Z), SIGMA_RATIO_KNOWN for its
           staleness and day-to-day noise; the median ratio and
           SIGMA_RATIO_UNKNOWN for a video it never printed.
  field    every listed candidate plus the THREAT_TOP fastest tracked videos
           (an unlisted video can take #1: 4 of the 25 #1 events from 9/9).
  P        N_SIM joint draws of log(nowcast x ratio) + N(0, sd); P(code) =
           the share of draws its video comes first. "leader" = the listed
           code with the highest P.
FAIL CLOSED: no entries when the newest snapshot over every file is older
than API_STALE_SECS, and none for an event before MIN_H hours into its window
or whose candidates the feed has not mapped (yt_topvid_ids.json).

Writes FAIR_FILE: {"at", "api_last", "status", "entries": {event: {"fetched_at",
"h", "leader", "p", "margin", "n_field", "threat", "codes": {code: {"id",
"proj", "p"}}}}}.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import os
import random
import re
import statistics
import time
import zlib
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from yt_weekly_fair import _env_float, _interp, _val

SERIES = "KXYTDAILYTOPVIDEOG"
DATA_DIR = r"C:\Users\jackd\Documents\KL-data\youtube-collect"
SNAP_FILES = [p.strip() for p in os.environ.get("IMM_YT1_SNAP_FILES", "").split(";") if p.strip()] or [
    os.path.join(DATA_DIR, "yt_snapshots.jsonl"), os.path.join(DATA_DIR, "yt_topvid_snapshots.jsonl")]
IDS_FILE = os.environ.get("IMM_YT1_IDS_FILE", os.path.join(DATA_DIR, "yt_topvid_ids.json"))
RATIO_FILE = os.environ.get(
    "IMM_YT1_RATIO_FILE",
    r"C:\Users\jackd\Documents\KL-data\youtube-analysis-2026-10-05\vid_calib.csv")
LAG_H = _env_float("IMM_YT1_LAG_H", 1.5)
MIN_H = _env_float("IMM_YT1_MIN_H", 2.0)
API_STALE_SECS = _env_float("IMM_YT1_API_STALE_SECS", 2700)
THREAT_TOP = int(_env_float("IMM_YT1_THREAT_TOP", 40))
N_SIM = int(_env_float("IMM_YT1_N_SIM", 4000))
SIGMA_RATIO_KNOWN = _env_float("IMM_YT1_SIGMA_RATIO_KNOWN", 0.10)
SIGMA_RATIO_UNKNOWN = _env_float("IMM_YT1_SIGMA_RATIO_UNKNOWN", 0.20)
HISTORY_DAYS = 3
# snapshots older than this behind the newest are dropped after each read:
# HISTORY_DAYS + today is all event_fair reads (and the bot's RAM is finite)
SNAP_KEEP_DAYS = 5
# (hours into the window, log sd of the nowcast) -- calib.py, smoothed
SIGMA_PROJ = [(1, 0.24), (4, 0.25), (6, 0.30), (8, 0.27), (10, 0.20), (12, 0.18),
              (14, 0.155), (16, 0.12), (17, 0.075), (18, 0.04), (20, 0.022),
              (22, 0.012), (24, 0.0)]
SHARE_FLOOR = 0.02             # a share of the day below this projects nothing
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_EVENT_RE = re.compile(r"^KXYTDAILYTOPVIDEOG-(\d{2})([A-Z]{3})(\d{2})$")

Series = Dict[str, Tuple[List[float], List[float]]]


def parse_event(event_ticker: str) -> Optional[date]:
    """'KXYTDAILYTOPVIDEOG-26OCT14' -> 2026-10-14 (the chart day); None."""
    m = _EVENT_RE.match(event_ticker or "")
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3)))
    except (KeyError, ValueError):
        return None


def window_start(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + LAG_H * 3600


def views_between(t: List[float], y: List[float], a: float, b: float) -> Optional[float]:
    va, _ = _val(t, y, a)
    vb, _ = _val(t, y, b)
    return None if va is None or vb is None else vb - va


def own_share(t: List[float], y: List[float], d: date, h: float) -> Optional[float]:
    """The median share of a full window a video had by h hours in, over the
    HISTORY_DAYS windows before d's; None without one."""
    w0 = window_start(d)
    shares = []
    for i in range(1, HISTORY_DAYS + 1):
        s = w0 - i * 86400
        full = views_between(t, y, s, s + 86400)
        part = views_between(t, y, s, s + h * 3600)
        if full and full > 0 and part is not None:
            shares.append(part / full)
    return statistics.median(shares) if shares else None


def load_ratios(path: Optional[str] = None) -> Dict[str, float]:
    """{video: chart/API ratio}, the median over the 10/05 study's windows
    starting 01-02Z; {} when the file is missing."""
    by: Dict[str, List[float]] = {}
    try:
        with open(path or RATIO_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("h") in ("1", "2") and r.get("ratio"):
                    by.setdefault(r["id"], []).append(float(r["ratio"]))
    except (OSError, ValueError):
        return {}
    return {k: statistics.median(v) for k, v in by.items() if v}


def event_fair(event: str, code_vid: Dict[str, Optional[str]], series: Series,
               now_ts: float, ratios: Dict[str, float]) -> Tuple[Optional[dict], str]:
    """(entry, '') or (None, why). `series` must hold no snapshot after now_ts
    (a backtest truncates it)."""
    d = parse_event(event)
    if d is None:
        return None, "ticker does not parse"
    t0 = window_start(d)
    h = (now_ts - t0) / 3600.0
    if h < MIN_H:
        return None, f"{h:.1f} h into the window (< {MIN_H:g})"
    hh = min(h, 24.0)
    listed = {c: v for c, v in code_vid.items() if v}
    if not listed:
        return None, "no candidate mapped to a video"
    so_far: Dict[str, float] = {}
    shares: Dict[str, Optional[float]] = {}
    for vid, (t, y) in series.items():
        p = views_between(t, y, t0, t0 + hh * 3600)
        if p is None or p <= 0:
            continue
        so_far[vid] = p
        shares[vid] = 1.0 if hh >= 24 else own_share(t, y, d, hh)
    known = [s for s in shares.values() if s]
    pooled = statistics.median(known) if known else None
    proj: Dict[str, float] = {}
    for vid, p in so_far.items():
        s = shares[vid] or pooled
        if s and s >= SHARE_FLOOR:
            proj[vid] = p / s
    in_field = set(sorted(proj, key=proj.get, reverse=True)[:THREAT_TOP])
    in_field |= {v for v in listed.values() if v in proj}
    if not any(v in proj for v in listed.values()):
        return None, "no listed video has views in the window yet"
    sd_p = _interp(SIGMA_PROJ, hh)
    ratio_mid = statistics.median(ratios.values()) if ratios else 1.0
    field = sorted(in_field)
    mu, sd = [], []
    for v in field:
        r = ratios.get(v)
        mu.append(math.log(proj[v] * (r if r else ratio_mid)))
        sd.append(math.hypot(sd_p, SIGMA_RATIO_KNOWN if r else SIGMA_RATIO_UNKNOWN))
    rng = random.Random(zlib.crc32(f"{event}|{int(now_ts // 600)}".encode()))
    wins = [0] * len(field)
    for _ in range(N_SIM):
        best_i, best = 0, -1e300
        for i in range(len(field)):
            x = mu[i] + rng.gauss(0.0, sd[i]) if sd[i] > 0 else mu[i]
            if x > best:
                best_i, best = i, x
        wins[best_i] += 1
    p_vid = {v: wins[i] / N_SIM for i, v in enumerate(field)}
    codes = {c: {"id": v, "proj": round(proj[v]) if v in proj else None,
                 "p": round(p_vid.get(v, 0.0), 4)} for c, v in listed.items()}
    leader = max(codes, key=lambda c: codes[c]["p"])
    by_mu = sorted(range(len(field)), key=lambda i: -mu[i])
    top, second = by_mu[0], by_mu[1] if len(by_mu) > 1 else None
    listed_ids = set(listed.values())
    threat = next((field[i] for i in by_mu if field[i] not in listed_ids), None)
    return {
        "fetched_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(timespec="seconds"),
        "h": round(h, 2), "leader": leader, "p": codes[leader]["p"],
        # how far ahead the model's top video is of the next (log scale)
        "margin": round(mu[top] - mu[second], 4) if second is not None else None,
        "top_listed": field[top] in listed_ids, "n_field": len(field),
        "threat": None if threat is None else {
            "id": threat, "proj": round(proj[threat]), "p": round(p_vid.get(threat, 0.0), 4)},
        "codes": codes}, ""


# ---------------------------------------------------------------------------
# live: the snapshot files, read incrementally, and the fair file

_snap: dict = {"files": {}, "series": {}, "last_ts": 0.0}


def load_snapshots(paths: Optional[List[str]] = None, chunk: int = 16 << 20,
                   keep_days: Optional[float] = SNAP_KEEP_DAYS) -> Tuple[Series, float]:
    """{video: ([ts], [views])} merged over `paths` (SNAP_FILES), reading only
    what each file gained since the last call (a shrunk file: from scratch),
    less what is more than keep_days behind the newest (None keeps all);
    and the newest snapshot time."""
    series = _snap["series"]
    for p in paths or SNAP_FILES:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        st = _snap["files"].setdefault(p, {"offset": 0, "partial": b""})
        if size < st["offset"]:
            _snap.update(files={}, series={}, last_ts=0.0)
            return load_snapshots(paths, chunk, keep_days)
        with open(p, "rb") as f:
            f.seek(st["offset"])
            while True:
                block = f.read(chunk)
                if not block:
                    break
                st["offset"] += len(block)
                lines = (st["partial"] + block).split(b"\n")
                st["partial"] = lines.pop()
                for ln in lines:
                    try:
                        x = json.loads(ln)
                        vid, ts, v = x["id"], float(x["ts"]), float(x["views"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    t, y = series.setdefault(vid, ([], []))
                    if not t or ts > t[-1]:
                        t.append(ts)
                        y.append(v)
                    else:
                        i = bisect.bisect_left(t, ts)
                        if i < len(t) and t[i] == ts:
                            continue
                        t.insert(i, ts)
                        y.insert(i, v)
                    if ts > _snap["last_ts"]:
                        _snap["last_ts"] = ts
    if keep_days:
        floor = _snap["last_ts"] - keep_days * 86400
        for vid in list(series):
            t, y = series[vid]
            i = bisect.bisect_left(t, floor)
            if i:
                del t[:i]
                del y[:i]
            if not t:
                del series[vid]
    return series, _snap["last_ts"]


def open_events(ids: dict, now_ts: float) -> Dict[str, Dict[str, Optional[str]]]:
    """{event: {code: video}} for the #1 events the feed has mapped whose
    chart day's window is open or ended less than 6 h ago."""
    out = {}
    for ev, codes in (ids.get("events") or {}).items():
        d = parse_event(ev)
        if d is None:
            continue
        t0 = window_start(d)
        if t0 - 86400 <= now_ts <= t0 + 30 * 3600:
            out[ev] = {c: (r or {}).get("id") for c, r in codes.items()}
    return out


def write_fair_file(path: str, now_ts: Optional[float] = None) -> dict:
    """Recompute every open #1 event's entry and write `path` (atomically)."""
    now_ts = now_ts if now_ts is not None else time.time()
    series, last = load_snapshots()
    out = {"at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(timespec="seconds"),
           "api_last": datetime.fromtimestamp(last, timezone.utc).isoformat(timespec="seconds") if last else None,
           "status": "ok", "entries": {}, "skipped": {}}
    if not last or now_ts - last > API_STALE_SECS:
        out["status"] = "stale_api"
    else:
        try:
            with open(IDS_FILE, encoding="utf-8") as f:
                ids = json.load(f)
        except (OSError, ValueError):
            ids = None
        if not isinstance(ids, dict):
            out["status"] = "no_ids"
        else:
            ratios = load_ratios()
            for ev, code_vid in sorted(open_events(ids, now_ts).items()):
                e, why = event_fair(ev, code_vid, series, now_ts, ratios)
                if e is None:
                    out["skipped"][ev] = why
                else:
                    out["entries"][ev] = e
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, path)
    return out


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm", "yt_top1_fair.json")
    print(json.dumps(write_fair_file(target), indent=1)[:3000])
