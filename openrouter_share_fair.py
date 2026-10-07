#!/usr/bin/env python3
"""OpenRouter market-share fair values for incentive_mm's share gate.

KX<AUTHOR>SHARE ("If Anthropic scores above 3.1% on OpenRouter text market
share by model author week of Sep 28, 2026") settles on the Market Share
chart of openrouter.ai/rankings: each model author's share of TEXT REQUESTS
in a Monday-Sunday UTC week, read 10:00 AM ET the Monday after, rounded to
one decimal. An author the chart does not name -- it names nine and folds
the rest into "Others" -- settles every strike No. Jack has OpenRouter's
permission to read the site's data (2026-09-30; its Terms otherwise forbid
automated access).

The chart's own data: /api/frontend/v1/rankings/modality-chart?routeSegment=
text, field `marketShareData` -- per week, text requests by the nine named
authors plus "Others", 52 weeks, the current week to date. It reproduces
every settled value checked (weeks of Sep 7 / 14 / 21, every named author,
to the rounding). The per-model leaderboard (/api/frontend/v1/rankings/
models?view=day|week, field `count`; text-output models only, from the
catalog) gives the run rate for the days still to come and the share of the
authors the chart folds into Others.

Model, per event (week W = Monday..Sunday UTC; the event is dated the Monday
after):
  K_a, K_T  week-to-date requests from the chart, through its own timestamp
  r         days from the chart's timestamp (cachedAt) to the end of W
  s_a, R    run-rate share and requests/day: the plain average of the
            leaderboard's last complete day and trailing 7 days, the chart's
            week-to-date (once it holds a day's worth) and the chart's last
            ~24 hours (from the snapshots this writer keeps). Since
            2026-10-05 s_a = A_a + SHARE_RUN_PULL (L_a - A_a) whenever the
            snapshots cover the last SHARE_RUN_HOURS (6): L_a the chart's
            share over those hours, A_a the anchor -- the week-to-date, or
            the flow since a break in the mix (see detect_break; the family
            stands aside for SHARE_BREAK_HOLD_HOURS after one). The blend
            is only the fallback; R keeps the blend.
  mu_a    = 100 (K_a + r R s_a) / (K_T + r R)
  sigma_a = SHARE_SIGMA_MULT sqrt((vol_a (r + gap) / 7)^2
                                  + (spread_a r / 7)^2 + SHARE_SIGMA_FLOOR^2)
vol_a = the author's week-over-week change of chart share over its last 20
changes, winsorized RMS (9/30: deepseek 1.77pp, google 1.43, openai 1.04,
qwen 0.59, z-ai 0.54, anthropic 0.43, mistralai 0.36); fewer than 4 changes
-> SHARE_DEFAULT_VOL. Linear in r/7: a whole week ahead is one
week-over-week change, the last day moves the week by a seventh of that
day's surprise. spread_a = half the range of the blend's estimators (they
part while an author is on the move); on the chart's own run rate it is 0
-- the 3/6/24h range it used to take mostly measured the time of day, and
vol alone scores RMS z 0.8-1.06 on the week of 9/28 -- except after a
break: half the gap between the anchor and the week-to-date (the doubt
that the new level holds). gap = days between the data and
the start of a week not yet begun. p_ident = P(the author finishes among the
SHARE_NAMED the chart names), normal on the margin to the boundary
competitor. `complete` once W has ended (00:00Z Monday): from then on anyone
can read the answer.

The chart is LIVE: polled every 10 minutes through 10/01, its week-to-date
included the day in progress and moved 16 times between 02:25Z and 12:21Z
(cachedAt steps of 7-60 minutes). The leaderboard's day view is whole days,
rolled once a day (9/30 was in by 03:00Z). So the known part runs to the
chart's own timestamp, r is clock time from there to the week's end (K_T / R,
the week-to-date in run-rate days, rides along as `ratio_days`), and an
entry is `lag` -- the gate stands aside -- only while the chart is older than
SHARE_CHART_MAX_AGE_MIN or the leaderboard has missed a whole day.

Writes SHARE_FAIR_FILE for incentive_mm: per EVENT ticker mu / sigma in
percentage points, p_ident, known (days of W in the data) / complete / lag,
data_version (moves once a day, with the leaderboard's day), plus the inputs.
Every complete week's chart shares go to
SHARE_FINALS_FILE when first seen and on any revision; the per-event
forecasts go to SHARE_PRED_FILE hourly, for calibrating sigma.

Standalone:  python openrouter_share_fair.py [--out path]
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
OR_SITE = os.environ.get("IMM_SHARE_SITE", "https://openrouter.ai")
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_SHARE_KALSHI_MARKETS_URL",
    "https://api.elections.kalshi.com/trade-api/v2/markets")
SHARE_FINALS_FILE = os.environ.get(
    "IMM_SHARE_FINALS_FILE", os.path.join(STATUS_DIR, "openrouter_share_finals.jsonl"))
SHARE_PRED_FILE = os.environ.get(
    "IMM_SHARE_PRED_FILE", os.path.join(STATUS_DIR, "openrouter_share_preds.jsonl"))
SHARE_SIGMA_MULT = _env_float("IMM_SHARE_SIGMA_MULT", 1.0)
SHARE_SIGMA_FLOOR = _env_float("IMM_SHARE_SIGMA_FLOOR", 0.1)     # pp
SHARE_DEFAULT_VOL = _env_float("IMM_SHARE_DEFAULT_VOL", 2.0)     # pp per week
SHARE_VOL_WEEKS = int(_env_float("IMM_SHARE_VOL_WEEKS", 20))
SHARE_NAMED = int(_env_float("IMM_SHARE_NAMED", 9))              # authors the chart names
SHARE_CHART_MAX_AGE_MIN = _env_float("IMM_SHARE_CHART_MAX_AGE_MIN", 180)
SHARE_RECENT_HOURS = _env_float("IMM_SHARE_RECENT_HOURS", 24)       # the blend's chart window
# The run rate's share comes from the chart's own data once the stored
# snapshots cover the last SHARE_RUN_HOURS (Jack 2026-10-03, after the book
# ran over the blend: "make the share market change"); 0 = the four-way
# blend alone. Since 2026-10-05 (Jack: "yes" to the fair rework) it is an
# ANCHOR -- the week-to-date share, or the flow since a break in the chart's
# mix -- pulled SHARE_RUN_PULL of the way toward the last SHARE_RUN_HOURS.
# Backtest on the week of 9/28 (4 authors, hourly, scored on the settled
# week): RMS error of mu 0.35pp against 0.47 for the plain last 3h (0.57 on
# the Saturday, 0.25 now). The last hours mostly measure the time of day
# (deepseek ~5pp higher at 18-21Z than 00-05Z, google's 26% Saturday spike
# settled 21.5%), and those swings average out over the week.
SHARE_RUN_HOURS = _env_float("IMM_SHARE_RUN_HOURS", 6)
SHARE_RUN_PULL = _env_float("IMM_SHARE_RUN_PULL", 0.3)
# A BREAK in the mix -- a model launched or withdrawn (10/05: z-ai 6.6% ->
# 21% at the week's turn, the stealth model's ~10% to nothing at 16Z) --
# makes the week-to-date the wrong anchor for every author. A named bucket
# whose last SHARE_BREAK_WINDOW_HOURS differ from its share since the anchor
# by >= SHARE_BREAK_ABS_PP and >= SHARE_BREAK_REL of the larger of the two
# is a break (google's 26% Saturday against 21.7% was not: 17%), once the
# anchor holds SHARE_BREAK_MIN_BASE_HOURS before the window. The anchor
# then moves to the change point; until SHARE_BREAK_MIN_HOURS of flow sit
# behind it the run rate is the last hours alone.
# 2026-10-06 (Jack: "implement all 3", after z-ai's 21% -> 8% step at 10:15
# ET): the window is 1h, not 3h. On every stored snapshot 10/04-10/07 (790)
# it finds the same two breaks and nothing else, 56 min (stealth) and 1h38m
# (z-ai, 11:06 ET instead of 12:44) sooner; 21 of the 23 share fills that
# day's step cost (-$95 of -$107) came after 11:06. And the family STANDS
# ASIDE (entries lag) until SHARE_BREAK_HOLD_HOURS of chart time have passed
# since the change point: before that the run rate is mostly pre-break
# flow (at 11:06 the last 6h held z-ai at ~18% against a live 8%), the
# exact mispricing the book was buying. 0 = quote through on the last hours.
SHARE_BREAK_WINDOW_HOURS = _env_float("IMM_SHARE_BREAK_WINDOW_HOURS", 1)
SHARE_BREAK_HOLD_HOURS = _env_float("IMM_SHARE_BREAK_HOLD_HOURS", 3)
SHARE_BREAK_ABS_PP = _env_float("IMM_SHARE_BREAK_ABS_PP", 3.0)
SHARE_BREAK_REL = _env_float("IMM_SHARE_BREAK_REL", 0.5)
SHARE_BREAK_MIN_BASE_HOURS = _env_float("IMM_SHARE_BREAK_MIN_BASE_HOURS", 6)
SHARE_BREAK_MIN_HOURS = _env_float("IMM_SHARE_BREAK_MIN_HOURS", 3)
# A REVISION of the week's counts -- a named author's count falls, one
# with >= 1% of the week vanishes without Others taking it, or one
# interval's increase goes >= SHARE_JUMP_FRAC to one author (requests
# re-attributed, e.g. the stealth model's to its maker) -- marks the
# entries lag for SHARE_REVISION_HOLD_HOURS and anchors the run rate after it.
SHARE_REVISION_HOLD_HOURS = _env_float("IMM_SHARE_REVISION_HOLD_HOURS", 6)
SHARE_JUMP_FRAC = _env_float("IMM_SHARE_JUMP_FRAC", 0.6)
SHARE_HIST_HOURS = 48                                               # snapshots kept
# every chart snapshot, one file per UTC day ("" = off), and the Kalshi
# ladder (every strike's touch) on each hourly event read: the 48h of
# snapshots above and the cycle log's three quoted strikes are too little
# to fit the run rate or to score the fair against the market
SHARE_ARCHIVE_DIR = os.environ.get("IMM_SHARE_ARCHIVE_DIR", STATUS_DIR)
SHARE_LADDER_FILE = os.environ.get(
    "IMM_SHARE_LADDER_FILE", os.path.join(STATUS_DIR, "openrouter_share_ladder.jsonl"))
SHARE_LEADERBOARD_EVERY_SECS = _env_float("IMM_SHARE_LEADERBOARD_EVERY_SECS", 1800)
SHARE_CATALOG_EVERY_SECS = _env_float("IMM_SHARE_CATALOG_EVERY_SECS", 6 * 3600)
SHARE_PRED_EVERY_SECS = 3600
EVENTS_TTL_SECS = 3600
HTTP_TIMEOUT = 30
UA = {"User-Agent": "KL-imm-share/1.0 (OpenRouter rankings read with permission)"}

# Kalshi series -> OpenRouter author slug (the chart's own keys)
SERIES_AUTHOR = {
    "KXANTHSHARE": "anthropic", "KXOPENSHARE": "openai", "KXGOOGSHARE": "google",
    "KXDEEPSHARE": "deepseek", "KXBABASHARE": "qwen", "KXXIAOMISHARE": "xiaomi",
    "KXZAISHARE": "z-ai", "KXMISTRALSHARE": "mistralai", "KXTENCENTSHARE": "tencent",
    "KXSTEALTHSHARE": "stealth",
}
SHARE_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_SHARE_FAIR_SERIES", ",".join(SERIES_AUTHOR)).split(",") if s.strip())
OTHERS = "Others"
LAST: Dict[str, object] = {"data_current": None}    # the last write's state

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[share-fair] {msg}", flush=True)


def _site_json(path: str, params: Optional[dict] = None, retries: int = 2) -> dict:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(OR_SITE + path, params=params or {}, headers=UA,
                             timeout=HTTP_TIMEOUT)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                time.sleep(3 * (attempt + 1) ** 2)
                continue
            r.raise_for_status()
            js = r.json()
            return js if isinstance(js, dict) else {"data": js}
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1) ** 2)
    raise last if last else RuntimeError(f"GET {path} failed")


# ----------------------------------------------------------------------------
# OpenRouter reads

def fetch_market_share() -> Tuple[List[dict], Optional[float]]:
    """The chart's weeks [{"x": "YYYY-MM-DD", "ys": {author: requests}}],
    oldest first, and its cachedAt (epoch seconds)."""
    js = _site_json("/api/frontend/v1/rankings/modality-chart", {"routeSegment": "text"})
    d = js.get("data") if isinstance(js.get("data"), dict) else js
    weeks = [w for w in (d.get("marketShareData") or [])
             if isinstance(w, dict) and isinstance(w.get("ys"), dict) and w.get("x")]
    ca = d.get("cachedAt")
    try:
        ca = float(ca) / 1000.0 if ca is not None else None
    except (TypeError, ValueError):
        ca = None
    if not weeks:
        raise ValueError("market-share chart: no marketShareData")
    return sorted(weeks, key=lambda w: str(w["x"])), ca


def fetch_catalog() -> Dict[str, bool]:
    """permaslug / slug -> produces text."""
    js = _site_json("/api/frontend/v1/catalog/models")
    out: Dict[str, bool] = {}
    for m in js.get("data") or []:
        if not isinstance(m, dict):
            continue
        text = bool("text" in (m.get("output_modalities") or []) or m.get("has_text_output"))
        for k in (m.get("permaslug"), m.get("slug")):
            if k:
                out[str(k)] = text
    return out


def fetch_rankings(view: str) -> List[dict]:
    return list(_site_json("/api/frontend/v1/rankings/models", {"view": view}).get("data") or [])


def author_counts(rows: List[dict], catalog: Dict[str, bool]) -> Tuple[Dict[str, float], float]:
    """Text requests by author (the permaslug's first segment), and the text
    total. A model the catalog does not know counts (9/30: 9 such, 0.0001%
    of requests)."""
    by: Dict[str, float] = {}
    tot = 0.0
    for r in rows or []:
        slug = str(r.get("model_permaslug") or "")
        try:
            c = float(r.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if not slug or c <= 0:
            continue
        text = catalog.get(slug, catalog.get(slug.split(":")[0], True))
        if not text:
            continue
        a = slug.split("/")[0]
        by[a] = by.get(a, 0.0) + c
        tot += c
    return by, tot


def rows_date(rows: List[dict]) -> Optional[date]:
    """The leaderboard's last day (the day view's rows all carry it)."""
    ds = sorted({str(r.get("date") or "")[:10] for r in rows or [] if r.get("date")})
    try:
        return date.fromisoformat(ds[-1]) if ds else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# the model

def chart_shares(weeks: List[dict]) -> List[Tuple[str, Dict[str, float]]]:
    out = []
    for w in weeks:
        ys = {k: float(v) for k, v in w["ys"].items() if isinstance(v, (int, float))}
        tot = sum(ys.values())
        if tot > 0:
            out.append((str(w["x"])[:10], {k: 100.0 * v / tot for k, v in ys.items()}))
    return out


def author_vol(weeks: List[dict], current: date, n: int = SHARE_VOL_WEEKS) -> Dict[str, float]:
    """Winsorized RMS week-over-week change (pp) of each named author's
    chart share over its last n changes between complete weeks (a week
    still running on `current` is left out). Each change is capped at 3x the
    median absolute change (at least 0.3pp): one jump -- openai's -6.57pp in
    the week of Sep 14 -- otherwise sets the band for twenty weeks (raw RMS
    1.94 against 1.04)."""
    cut = (current - timedelta(days=6)).isoformat()
    sh = [s for s in chart_shares(weeks) if s[0] < cut]
    out: Dict[str, float] = {}
    keys = {k for _, s in sh for k in s if k != OTHERS}
    for k in keys:
        ch = [sh[i + 1][1][k] - sh[i][1][k] for i in range(len(sh) - 1)
              if k in sh[i][1] and k in sh[i + 1][1]][-n:]
        if len(ch) >= 4:
            cap = max(0.3, 3.0 * sorted(abs(x) for x in ch)[len(ch) // 2])
            out[k] = math.sqrt(sum(min(cap, abs(x)) ** 2 for x in ch) / len(ch))
    return out


def week_of(event_ticker: str, rules: str = "") -> Optional[date]:
    """The Monday a share event measures: the ticker date (the settling
    Monday) minus 7 days, else 'week of Sep 28, 2026' in the rules. The
    ticker wins: on the August events the rules' "week of" label was a week
    off, while ticker - 7 matched every settlement (checked 2026-09-27)."""
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if m and _MONTHS.get(m.group(2).lower()):
        try:
            return date(2000 + int(m.group(1)), _MONTHS[m.group(2).lower()],
                        int(m.group(3))) - timedelta(days=7)
        except ValueError:
            pass
    m = re.search(r"week of ([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})", rules or "")
    if m and _MONTHS.get(m.group(1).lower()[:3]):
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        except ValueError:
            return None
    return None


def _phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _week_start(week: date) -> datetime:
    return datetime(week.year, week.month, week.day, tzinfo=timezone.utc)


def known_days(week: date, chart_time: Optional[datetime], wtd_total: float,
               rate_total: float) -> Tuple[float, str, Optional[float]]:
    """Days of `week` behind the chart's week-to-date, how that was set, and
    K_T / R -- the week-to-date in run-rate days -- as a cross-check. The
    chart is live, so the known part runs to its own timestamp: cachedAt - W
    in days (0..7). Without a timestamp the ratio stands in."""
    ratio = (min(7.0, max(0.0, wtd_total / rate_total))
             if rate_total > 0 and wtd_total > 0 else None)
    if chart_time is not None:
        e = (chart_time - _week_start(week)).total_seconds() / 86400.0
        return min(7.0, max(0.0, e)), "chart time", ratio
    if ratio is not None:
        return ratio, "ratio", ratio
    return 0.0, "none", None


def data_current(now: datetime, last_day: Optional[date],
                 chart_time: Optional[datetime]) -> Tuple[bool, str]:
    """The feeds are fresh enough to quote against: the chart (live, moving
    every 7-60 minutes on 10/01) is younger than SHARE_CHART_MAX_AGE_MIN, and
    the leaderboard has not missed a whole day (its day view rolls once a
    day, within ~3h of 00:00Z). Otherwise the gate stands aside: a feed
    stalled for us alone is the book's edge over us."""
    if chart_time is None:
        return False, "the chart has no timestamp"
    age = (now - chart_time).total_seconds() / 60.0
    if age > SHARE_CHART_MAX_AGE_MIN:
        return False, f"the chart last moved {chart_time:%m-%d %H:%MZ}, {age:.0f}m ago"
    today = now.astimezone(timezone.utc).date()
    if last_day is None or last_day < today - timedelta(days=2):
        return False, (f"the leaderboard is stuck on {last_day}" if last_day
                       else "no leaderboard day")
    return True, ""


def recent_delta(hist: List[dict], week: date, chart_time: Optional[datetime],
                 ys_now: Dict[str, float], hours: Optional[float] = None
                 ) -> Optional[Tuple[Dict[str, float], float, float]]:
    """The chart's last ~`hours` (default SHARE_RECENT_HOURS) of week W:
    requests by named author and in total since the stored snapshot nearest
    that far back (same week, within 0.75-1.25x the window), and the days
    between. None without one -- the start of each week and of a fresh fair
    file. An author named at only one end, or whose count fell (a revision),
    has no entry."""
    if chart_time is None or not ys_now:
        return None
    now_t = chart_time.timestamp()
    span = (hours if hours else SHARE_RECENT_HOURS) * 3600.0
    cands = [h for h in hist or [] if str(h.get("x"))[:10] == week.isoformat()
             and now_t - 1.25 * span <= float(h.get("t") or 0) <= now_t - 0.75 * span]
    if not cands:
        return None
    then = min(cands, key=lambda h: abs(float(h["t"]) - (now_t - span)))
    ys_then = {k: float(v) for k, v in (then.get("ys") or {}).items()
               if isinstance(v, (int, float))}
    tot = sum(ys_now.values()) - sum(ys_then.values())
    days = (now_t - float(then["t"])) / 86400.0
    if tot <= 0 or days <= 0:
        return None
    by = {k: ys_now[k] - ys_then[k] for k in ys_now
          if k != OTHERS and k in ys_then and ys_now[k] >= ys_then[k]}
    return by, tot, days


def _counts(h: dict) -> Dict[str, float]:
    return {k: float(v) for k, v in (h.get("ys") or {}).items() if isinstance(v, (int, float))}


def week_snaps(hist: List[dict], week: date) -> List[Tuple[float, Dict[str, float]]]:
    """The stored snapshots of `week`, oldest first."""
    wk = week.isoformat()
    return sorted(((float(h["t"]), _counts(h)) for h in hist or []
                   if str(h.get("x"))[:10] == wk and h.get("t")), key=lambda s: s[0])


def detect_break(hist: List[dict], week: date, chart_time: Optional[datetime],
                 ys_now: Dict[str, float], base: Optional[dict] = None) -> Optional[dict]:
    """A break in the chart's mix since `base` (the last break, else the
    week's start): the named bucket whose last SHARE_BREAK_WINDOW_HOURS moved
    furthest from its share between the base and that window, when the move
    is >= SHARE_BREAK_ABS_PP and >= SHARE_BREAK_REL of the larger share.
    Placed at the single change point of that bucket's share over the stored
    snapshots (least squares, volume-weighted), the detection window's start
    when the snapshots do not show one. {"t", "x", "ys" (counts at t),
    "bucket", "old", "new" (its share either side, %)} or None."""
    if chart_time is None or not ys_now:
        return None
    now_t = chart_time.timestamp()
    b_t = float(base["t"]) if base else _week_start(week).timestamp()
    b_ys = {k: float(v) for k, v in (base or {}).get("ys", {}).items()}
    if now_t - SHARE_BREAK_WINDOW_HOURS * 3600.0 - b_t < SHARE_BREAK_MIN_BASE_HOURS * 3600.0:
        return None
    rec = recent_delta(hist, week, chart_time, ys_now, SHARE_BREAK_WINDOW_HOURS)
    if rec is None:
        return None
    by, tot, days = rec
    then_t = now_t - days * 86400.0
    old_tot = sum(ys_now.values()) - tot - sum(b_ys.values())
    if old_tot <= 0:
        return None
    best = None
    for k, n in by.items():
        if base and k not in b_ys:
            continue
        new = 100.0 * n / tot
        old = 100.0 * (ys_now[k] - n - b_ys.get(k, 0.0)) / old_tot
        d = abs(new - old)
        if d >= SHARE_BREAK_ABS_PP and d >= SHARE_BREAK_REL * max(old, new) \
                and (best is None or d > best[0]):
            best = (d, k, old, new)
    if best is None:
        return None
    _, k, old, new = best
    pts = [(b_t, b_ys)] + [s for s in week_snaps(hist, week) if b_t < s[0] <= now_t]
    segs = []                                  # (start, volume, k's share)
    for (t0, y0), (t1, y1) in zip(pts, pts[1:]):
        v = sum(y1.values()) - sum(y0.values())
        if v > 0:
            segs.append((t0, v, (y1.get(k, 0.0) - y0.get(k, 0.0)) / v, y0))

    def cost(xs):
        w = sum(v for _, v, _, _ in xs)
        m = sum(v * s for _, v, s, _ in xs) / w
        return sum(v * (s - m) ** 2 for _, v, s, _ in xs), m
    # the window's own start snapshot (recent_delta measured from it) unless
    # the best split shows the step
    at, at_ys = min(pts[1:], key=lambda s: abs(s[0] - then_t))
    fit = None
    for i in range(1, len(segs)):
        (cb, mb), (ca, ma) = cost(segs[:i]), cost(segs[i:])
        if fit is None or cb + ca < fit:
            fit = cb + ca
            split = ((segs[i][0], segs[i][3], 100.0 * mb, 100.0 * ma)
                     if abs(ma - mb) * 100.0 >= SHARE_BREAK_ABS_PP else None)
    if fit is not None and split is not None:
        at, at_ys, old, new = split            # the step's own levels
    return {"t": at, "x": week.isoformat(), "ys": dict(at_ys), "bucket": k,
            "old": round(old, 3), "new": round(new, 3)}


def chart_revision(hist: List[dict], week: date, now_t: float) -> Optional[Tuple[float, dict]]:
    """The last snapshot of `week` (time, counts) that revised the counts
    before it, within SHARE_REVISION_HOLD_HOURS of now_t: a named author's
    count fell; one interval's increase went >= SHARE_JUMP_FRAC to a single
    author (requests re-attributed, not new flow); or an author holding >= 1%
    of the week left -- or one arrived -- the nine named with more than 1.5x
    the smallest name on the other side. The nine-name boundary swapping
    (10/06 01:43Z: mistralai 18.48M out to Others, meta-llama 18.55M in) is
    not a revision; the stealth model's 74.7M vanishing beside xiaomi's 24M
    would be. None otherwise."""
    last = None
    snaps = week_snaps(hist, week)
    for (t0, y0), (t1, y1) in zip(snaps, snaps[1:]):
        if now_t - t1 > SHARE_REVISION_HOLD_HOURS * 3600.0:
            continue
        tot0 = sum(y0.values())
        d_tot = sum(y1.values()) - tot0
        named0 = [v for k, v in y0.items() if k != OTHERS]
        named1 = [v for k, v in y1.items() if k != OTHERS]
        for k, v0 in y0.items():
            if k == OTHERS:
                continue
            if k in y1:
                if y1[k] < v0 - 0.5 or (d_tot > 0 and y1[k] - v0 >= SHARE_JUMP_FRAC * d_tot
                                        and y1[k] - v0 > 0.01 * tot0):
                    last = (t1, y1)
            elif v0 >= 0.01 * tot0 and (not named1 or v0 > 1.5 * min(named1)):
                last = (t1, y1)
        for k, v1 in y1.items():
            if k != OTHERS and k not in y0 and v1 >= 0.01 * tot0                     and (not named0 or v1 > 1.5 * min(named0)):
                last = (t1, y1)
    return last


def event_fair(author: str, week: date, now: datetime, weeks: List[dict],
               last_day: Optional[date],
               day_counts: Tuple[Dict[str, float], float],
               week_counts: Tuple[Dict[str, float], float],
               vol: Dict[str, float],
               chart_time: Optional[datetime] = None,
               hist: Optional[List[dict]] = None,
               anchor: Optional[dict] = None) -> Optional[dict]:
    """N(mu, sigma) in pp for the author's chart share of week W, p_ident and
    the bookkeeping; None without a run rate. `anchor` is the week's last
    break in the chart's mix (detect_break / chart_revision), if any.

    The run rate for the days still to come is the plain average of the
    estimators at hand: the leaderboard's last complete day and trailing 7
    days, the chart's week-to-date once it holds a day, and the chart's last
    ~24 hours (from the stored snapshots). The chart is live and the book
    trades it, but no one window is right: on 10/01 13Z OpenAI read 19.18 /
    17.51 / 18.38 on the first three and 22.0 over the last 10 hours, while
    the book centred near 18.5 -- the plain average of all four lands there,
    any one alone misses by a point. The estimators part while an author is
    on the move, so half their range, over the days to come, joins sigma.

    Since 2026-10-03 a named author's share of the run rate is the chart's
    own last SHARE_RUN_HOURS instead, whenever the stored snapshots cover
    that window (the blend stays the fallback: the first hours of a week or
    of a fresh file, a week not yet begun, an author the chart folds into
    Others). The book traded the live chart's last hours while the blend's
    day-old windows lagged: on 10/02 OpenAI's last 3h fell from 19% to 16%
    while the blend held 18.9%, and the bot bought its strikes from the
    sellers. Its spread was then half the range of the chart's shares over
    the last 3/6/24h (until 2026-10-05). The total request rate keeps the
    blend: volume swings by hour of day.

    Since 2026-10-05 the chart's run rate is an anchor pulled SHARE_RUN_PULL
    of the way toward the last SHARE_RUN_HOURS (6). The anchor is the
    week-to-date; after a break in the mix the flow since it (until
    SHARE_BREAK_MIN_HOURS of that sit behind the break, the last hours
    alone). The last 3h alone ran 0.47pp RMS off the settled week of 9/28,
    this 0.35: the last hours chase the time of day (google's 3h share went
    26% -> 17% in 18 hours over the weekend; its week settled 21.5%). Its
    spread is 0 -- vol alone is calibrated -- except after a break, where it
    is half the gap between the anchor and the week-to-date."""
    d_by, d_tot = day_counts
    w_by, w_tot = week_counts
    wk = next((w for w in weeks if str(w["x"])[:10] == week.isoformat()), None)
    ys = {k: float(v) for k, v in (wk or {}).get("ys", {}).items()
          if isinstance(v, (int, float))}
    k_tot = sum(ys.values())
    lb = [(d_by, d_tot, 1.0), (w_by, w_tot, 7.0)]
    lb = [p for p in lb if p[1] > 0]
    if not lb:
        return None
    rate_lb = sum(t / n for _, t, n in lb) / len(lb)
    today = now.astimezone(timezone.utc).date()
    complete = today >= week + timedelta(days=7)
    e, how, ratio = known_days(week, chart_time, k_tot, rate_lb)
    if k_tot <= 0:
        how = "not started" if week > today else "no week-to-date"
    # (requests by author, total, days covered, counts every author)
    parts = [(by, t, n, True) for by, t, n in lb]
    recent = recent_delta(hist or [], week, chart_time, ys) if k_tot > 0 else None
    if recent is not None:
        parts.append((recent[0], recent[1], recent[2], False))
    if k_tot > 0 and e >= 1.0:  # the chart's week-to-date joins the run rate
        parts.append((ys, k_tot, e, False))
    rate_t = sum(t / n for _, t, n, _ in parts) / len(parts)
    authors = set(d_by) | set(w_by) | {k for k in ys if k != OTHERS}

    def ests(a: str) -> List[float]:
        # the chart's parts know only the authors it names
        return [by.get(a, 0.0) / t for by, t, _, every in parts if every or a in by]
    s_rate, spread = {}, {}
    for a in authors:
        xs = ests(a)
        s_rate[a] = sum(xs) / len(xs) if xs else 0.0
        spread[a] = 100.0 * (max(xs) - min(xs)) / 2.0 if xs else 0.0
    # the chart's own last hours, for the authors it names (see docstring)
    fast = None
    if SHARE_RUN_HOURS > 0 and k_tot > 0:
        fast = recent_delta(hist or [], week, chart_time, ys, SHARE_RUN_HOURS)
    fast_set: set = set()
    anchor_mode, anchor_share = None, {}
    brk = anchor if anchor and str(anchor.get("x"))[:10] == week.isoformat() else None
    if fast is not None:
        since = None
        anchor_mode = "week to date"
        if brk is not None and chart_time is not None:
            b_ys = {k: float(v) for k, v in (brk.get("ys") or {}).items()}
            b_tot = sum(b_ys.values())
            anchor_mode = "break, last hours"
            if (chart_time.timestamp() - float(brk["t"])) / 3600.0 >= SHARE_BREAK_MIN_HOURS                     and k_tot > b_tot:
                since = ({k: ys[k] - b_ys[k] for k in ys
                          if k != OTHERS and k in b_ys and ys[k] >= b_ys[k]}, k_tot - b_tot)
                anchor_mode = "since break"
        for a, n in fast[0].items():
            last = n / fast[1]
            if anchor_mode == "week to date":
                anc = ys[a] / k_tot
            elif since is not None and a in since[0]:
                anc = since[0][a] / since[1]
            else:
                anc = last
            s_rate[a] = anc + SHARE_RUN_PULL * (last - anc)
            anchor_share[a] = anc
            spread[a] = (0.0 if brk is None
                         else 100.0 * abs(anc - ys.get(a, 0.0) / k_tot) / 2.0)
            fast_set.add(a)
    r = 7.0 - e
    gap = 0.0
    if chart_time is not None:  # a week not yet begun: the data ends at the chart
        gap = max(0.0, (_week_start(week) - chart_time).total_seconds() / 86400.0)
    elif week > today:
        ref = (last_day + timedelta(days=1)) if last_day else today
        gap = float(max(0, (week - ref).days))
    named = {k for k in ys if k != OTHERS}

    def k_of(a: str) -> float:
        if a in ys:
            return ys[a]
        # folded into Others this week: its run-rate share of the week so far
        return s_rate.get(a, 0.0) * k_tot
    denom = k_tot + r * rate_t
    if denom <= 0:
        return None
    mus = {a: 100.0 * (k_of(a) + r * rate_t * s_rate.get(a, 0.0)) / denom
           for a in (authors | named | {author})}
    mu = mus[author]

    def sig(a: str) -> float:
        v = vol.get(a, SHARE_DEFAULT_VOL) * (r + gap) / 7.0
        s = spread.get(a, 0.0) * r / 7.0
        return SHARE_SIGMA_MULT * math.sqrt(v * v + s * s + SHARE_SIGMA_FLOOR ** 2)
    sigma = sig(author)
    ranked = sorted(((m, a) for a, m in mus.items() if a != author), reverse=True)
    p_ident, boundary = 1.0, None
    if len(ranked) >= SHARE_NAMED:
        # named iff it beats the SHARE_NAMED-th best of the others
        other_mu, other = ranked[SHARE_NAMED - 1]
        sp = math.sqrt(sigma ** 2 + sig(other) ** 2)
        p_ident = _phi((mu - other_mu) / sp)
        boundary = {"author": other, "mu": round(other_mu, 3)}
    return {"mu": round(mu, 4), "sigma": round(sigma, 4), "p_ident": round(p_ident, 4),
            "known": round(e, 3), "coverage": how, "complete": complete,
            "ratio_days": round(ratio, 3) if ratio is not None else None,
            "week": week.isoformat(), "named_wtd": author in named,
            "wtd_share": (round(100.0 * ys[author] / k_tot, 4)
                          if author in ys and k_tot > 0 else None),
            "rate_share": round(100.0 * s_rate.get(author, 0.0), 4),
            "rate_spread": round(spread.get(author, 0.0), 4),
            "recent_share": (round(100.0 * recent[0][author] / recent[1], 4)
                             if recent is not None and author in recent[0] else None),
            "recent_hours": round(recent[2] * 24.0, 2) if recent is not None else None,
            "run_mode": ((f"chart {SHARE_RUN_HOURS:g}h" if anchor_mode == "break, last hours"
                          else f"{'break' if anchor_mode == 'since break' else 'wtd'}"
                               f"+{SHARE_RUN_PULL:g}x{SHARE_RUN_HOURS:g}h")
                         if author in fast_set else "blend"),
            "anchor_mode": anchor_mode if author in fast_set else None,
            "anchor_share": (round(100.0 * anchor_share[author], 4)
                             if author in anchor_share else None),
            "break_at": (datetime.fromtimestamp(float(brk["t"]), timezone.utc).isoformat()
                         if brk is not None else None),
            "fast_share": (round(100.0 * fast[0][author] / fast[1], 4)
                           if fast is not None and author in fast[0] else None),
            "fast_hours": round(fast[2] * 24.0, 2) if fast is not None else None,
            "blend_share": round(100.0 * (sum(ests(author)) / len(ests(author))), 4)
            if ests(author) else None,
            "vol": round(vol.get(author, SHARE_DEFAULT_VOL), 3), "boundary": boundary,
            # moves once a day, when the leaderboard's day rolls (the chart
            # moves every few minutes and would hold the gate all day)
            "data_version": f"{week.isoformat()}:{last_day}"}


def p_yes(strike: float, mu: float, sigma: float, p_ident: float) -> float:
    """P(the published share, rounded to 0.1, is above `strike`) and named."""
    edge = strike + 0.05
    if sigma <= 0:
        return p_ident if mu >= edge else 0.0
    return p_ident * 0.5 * math.erfc((edge - mu) / (sigma * math.sqrt(2.0)))


# ----------------------------------------------------------------------------
# Kalshi events

def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def _cents(m: dict, base: str) -> Optional[float]:
    """A price off a Kalshi market object in cents: '<base>_dollars' (V2),
    else the legacy integer-cent '<base>'."""
    for key, mult in ((base + "_dollars", 100.0), (base, 1.0)):
        v = m.get(key)
        if v is not None:
            try:
                return round(float(v) * mult, 2)
            except (TypeError, ValueError):
                continue
    return None


def fetch_events(get_json: Optional[Callable] = None,
                 ladder: Optional[List[dict]] = None) -> Dict[str, dict]:
    """Open share events: event ticker -> {"series", "author", "week"}. With
    `ladder`, every market's touch is appended to it as well."""
    out: Dict[str, dict] = {}
    for s in SHARE_SERIES:
        author = SERIES_AUTHOR.get(s)
        if not author:
            continue
        js = _signed_or_public(get_json, {"series_ticker": s, "status": "open", "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            if ladder is not None and ev and m.get("ticker"):
                ladder.append({"event": ev, "ticker": str(m["ticker"]),
                               "yes_bid": _cents(m, "yes_bid"), "yes_ask": _cents(m, "yes_ask"),
                               "last": _cents(m, "last_price"), "volume": m.get("volume"),
                               "open_interest": m.get("open_interest")})
            if not ev or ev in out:
                continue
            wk = week_of(ev, f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}")
            if wk:
                out[ev] = {"series": s, "author": author, "week": wk.isoformat()}
    return out


# ----------------------------------------------------------------------------
# the file

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _append(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    chart: Optional[Tuple[List[dict], Optional[float]]] = None,
                    catalog: Optional[Dict[str, bool]] = None,
                    day_rows: Optional[List[dict]] = None,
                    week_rows: Optional[List[dict]] = None,
                    events: Optional[Dict[str, dict]] = None,
                    finals_path: Optional[str] = None,
                    pred_path: Optional[str] = None) -> Tuple[int, int]:
    """Fetch (unless given), build and atomically write the fair file.
    Returns (events with an entry, events without one). Raises when the chart
    read fails (the caller logs and keeps the previous file, whose entries
    then age out of the bot's TTL). The leaderboard and the catalog are
    re-read every SHARE_LEADERBOARD_EVERY_SECS / SHARE_CATALOG_EVERY_SECS,
    and the leaderboard on every call while its last day is older than
    yesterday (its daily roll is due); between reads the file's copy of the
    run rate is used. Sets LAST["data_current"] for the caller's polling."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    weeks, cached_at = chart if chart is not None else fetch_market_share()
    chart_time = datetime.fromtimestamp(cached_at, timezone.utc) if cached_at else None
    # the chart's snapshots, one per cachedAt, for the last-24h run rate
    hist = [h for h in (old.get("chart_hist") or [])
            if isinstance(h, dict) and isinstance(h.get("ys"), dict)]
    if cached_at and weeks and (not hist or float(hist[-1].get("t") or 0) != float(cached_at)):
        hist.append({"t": float(cached_at), "x": str(weeks[-1]["x"])[:10],
                     "ys": {k: float(v) for k, v in weeks[-1]["ys"].items()
                            if isinstance(v, (int, float))}})
        if SHARE_ARCHIVE_DIR:
            _append(os.path.join(SHARE_ARCHIVE_DIR, "openrouter_share_chart_"
                                 f"{datetime.fromtimestamp(cached_at, timezone.utc):%Y-%m-%d}.jsonl"),
                    [hist[-1]])
    hist = [h for h in hist if float(h.get("t") or 0)
            >= float(cached_at or ts) - SHARE_HIST_HOURS * 3600]
    lb = old.get("leaderboard") if isinstance(old.get("leaderboard"), dict) else {}
    cat_store = old.get("catalog") if isinstance(old.get("catalog"), dict) else {}
    cat_at = float(old.get("catalog_at") or 0)
    lb_behind = (not lb.get("last_day")
                 or lb["last_day"] < (now.date() - timedelta(days=1)).isoformat())
    if day_rows is not None or week_rows is not None or catalog is not None \
            or not lb or lb_behind \
            or ts - float(lb.get("at") or 0) >= SHARE_LEADERBOARD_EVERY_SECS:
        try:
            cat = catalog
            if cat is None:
                if cat_store and ts - cat_at < SHARE_CATALOG_EVERY_SECS:
                    cat = {k: bool(v) for k, v in cat_store.items()}
                else:
                    cat = fetch_catalog()
                    cat_store, cat_at = cat, ts
            drows = day_rows if day_rows is not None else fetch_rankings("day")
            wrows = week_rows if week_rows is not None else fetch_rankings("week")
            d_by, d_tot = author_counts(drows, cat)
            w_by, w_tot = author_counts(wrows, cat)
            ld = rows_date(drows)
            lb = {"at": ts, "day": {"by": d_by, "total": d_tot},
                  "week": {"by": w_by, "total": w_tot},
                  "last_day": ld.isoformat() if ld else None}
        except Exception as e:
            if not lb:
                raise
            _log(f"! leaderboard read failed ({type(e).__name__}); keeping "
                 f"the run rate read at {datetime.fromtimestamp(float(lb.get('at') or 0), timezone.utc):%m-%d %H:%MZ}")
    day_counts = ({k: float(v) for k, v in lb["day"]["by"].items()}, float(lb["day"]["total"]))
    week_counts = ({k: float(v) for k, v in lb["week"]["by"].items()}, float(lb["week"]["total"]))
    last_day = date.fromisoformat(lb["last_day"]) if lb.get("last_day") else None
    current, lag_why = data_current(now, last_day, chart_time)
    LAST["data_current"] = current
    vol = author_vol(weeks, now.date())
    ev_cache = old.get("events") or {}
    events_at = old.get("events_at")
    if events is None:
        if ev_cache and ts - float(events_at or 0) < EVENTS_TTL_SECS:
            events = ev_cache
        else:
            try:
                ladder: List[dict] = []
                events = fetch_events(get_json, ladder=ladder)
                events_at = ts
                if SHARE_LADDER_FILE:
                    _append(SHARE_LADDER_FILE, [dict(r, at=now.isoformat()) for r in ladder])
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); "
                     f"reusing {len(ev_cache)} cached")
                events = ev_cache
    # the current week's last break in the mix (kept past the 48h of
    # snapshots: its counts ride along) and a revision of its counts
    cur_x = str(weeks[-1]["x"])[:10] if weeks else ""
    old_brk = (old.get("breaks") or {}).get(cur_x) if isinstance(old.get("breaks"), dict) else None
    brk = old_brk if isinstance(old_brk, dict) and old_brk.get("ys") else None
    revised = None
    try:
        cur_week = date.fromisoformat(cur_x)
    except ValueError:
        cur_week = None
    if cur_week is not None and chart_time is not None:
        revised = chart_revision(hist, cur_week, chart_time.timestamp())
        # a kept revision the rules no longer find while it is still in view
        # is dropped (and the break before it re-found from the snapshots)
        if brk is not None and brk.get("bucket") == "revision"                 and chart_time.timestamp() - float(brk["t"]) <= SHARE_REVISION_HOLD_HOURS * 3600.0                 and (revised is None or revised[0] < float(brk["t"])):
            _log(f"dropping the revision kept at "
                 f"{datetime.fromtimestamp(float(brk['t']), timezone.utc):%m-%d %H:%MZ}: not one now")
            brk = None
        if revised is not None and (brk is None or revised[0] > float(brk["t"])):
            brk = {"t": revised[0], "x": cur_x, "ys": dict(revised[1]), "bucket": "revision",
                   "old": None, "new": None}
            _log(f"chart revision at {datetime.fromtimestamp(revised[0], timezone.utc):%m-%d %H:%MZ}: "
                 f"family lag {SHARE_REVISION_HOLD_HOURS:g}h, run rate anchored after it")
        nb = detect_break(hist, cur_week, chart_time,
                          {k: float(v) for k, v in weeks[-1]["ys"].items()
                           if isinstance(v, (int, float))}, brk)
        if nb is not None:
            brk = nb
            _log(f"break in the mix: {nb['bucket']} {nb['old']:.2f}% -> {nb['new']:.2f}%, "
                 f"anchored at {datetime.fromtimestamp(nb['t'], timezone.utc):%m-%d %H:%MZ}")
    if revised is not None:
        current = False
        lag_why = (f"the chart revised the week's counts at "
                   f"{datetime.fromtimestamp(revised[0], timezone.utc):%m-%d %H:%MZ}")
        LAST["data_current"] = current
    elif brk is not None and chart_time is not None and SHARE_BREAK_HOLD_HOURS > 0             and chart_time.timestamp() - float(brk["t"]) < SHARE_BREAK_HOLD_HOURS * 3600.0:
        # a fresh break: stand aside until enough flow sits behind it
        current = False
        moved = (f" ({brk['bucket']} {brk['old']:.1f}% -> {brk['new']:.1f}%)"
                 if brk.get("old") is not None and brk.get("new") is not None else "")
        lag_why = (f"break in the mix at "
                   f"{datetime.fromtimestamp(float(brk['t']), timezone.utc):%m-%d %H:%MZ}"
                   f"{moved}; standing aside to "
                   f"{datetime.fromtimestamp(float(brk['t']) + SHARE_BREAK_HOLD_HOURS * 3600.0, timezone.utc):%H:%MZ}")
        LAST["data_current"] = current
    entries: Dict[str, dict] = {}
    missing: List[str] = []
    for ev, info in sorted(events.items()):
        try:
            wk = date.fromisoformat(info["week"])
            author = str(info["author"])
        except (KeyError, ValueError, TypeError):
            missing.append(ev)
            continue
        f = event_fair(author, wk, now, weeks, last_day, day_counts, week_counts, vol,
                       chart_time=chart_time, hist=hist, anchor=brk)
        if f is None:
            missing.append(ev)
            continue
        entries[ev] = dict(f, series=info.get("series"), author=author,
                           lag=not current, lag_reason=lag_why,
                           fetched_at=now.isoformat())
    # finals: every complete week's chart shares, on first sight and on revision
    keep_from = (now.date() - timedelta(days=21)).isoformat()
    finals = {x: v for x, v in (old.get("finals") or {}).items() if x >= keep_from}
    new_rows = []
    for x, sh in chart_shares(weeks):
        try:
            done = now.date() >= date.fromisoformat(x) + timedelta(days=7)
        except ValueError:
            continue
        if not done or x < keep_from:
            continue
        rounded = {k: round(v, 2) for k, v in sh.items()}
        if finals.get(x) != rounded:
            new_rows.append({"read_at": now.isoformat(), "week": x, "shares": rounded,
                             "revision": x in finals})
            finals[x] = rounded
    _append(finals_path or SHARE_FINALS_FILE, new_rows)
    pred_at = float(old.get("pred_at") or 0)
    live = [(ev, e) for ev, e in entries.items() if not e["complete"]]
    if live and ts - pred_at >= SHARE_PRED_EVERY_SECS:
        _append(pred_path or SHARE_PRED_FILE,
                [{"at": now.isoformat(), "event": ev, "mu": e["mu"], "sigma": e["sigma"],
                  "p_ident": e["p_ident"], "known": e["known"],
                  "wtd_share": e["wtd_share"], "rate_share": e["rate_share"],
                  "run_mode": e.get("run_mode"), "fast_share": e.get("fast_share"),
                  "blend_share": e.get("blend_share"), "anchor_share": e.get("anchor_share"),
                  "break_at": e.get("break_at")}
                 for ev, e in live])
        pred_at = ts
    cur_sh = chart_shares(weeks[-1:])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "entries": entries,
                   "missing": missing, "events": events, "events_at": events_at,
                   "data_current": current, "lag_reason": lag_why,
                   "chart_cached_at": chart_time.isoformat() if chart_time else None,
                   "chart_hist": hist, "breaks": {cur_x: brk} if brk else {},
                   "chart_week": cur_sh[0][0] if cur_sh else None,
                   "chart_week_shares": ({k: round(v, 3) for k, v in
                                          sorted(cur_sh[0][1].items(), key=lambda kv: -kv[1])}
                                         if cur_sh else {}),
                   "leaderboard": lb, "catalog": cat_store, "catalog_at": cat_at,
                   "vol": {k: round(v, 3) for k, v in sorted(vol.items())},
                   "finals": finals, "pred_at": pred_at,
                   "model": {"sigma_mult": SHARE_SIGMA_MULT, "sigma_floor": SHARE_SIGMA_FLOOR,
                             "default_vol": SHARE_DEFAULT_VOL, "vol_weeks": SHARE_VOL_WEEKS,
                             "named": SHARE_NAMED,
                             "chart_max_age_min": SHARE_CHART_MAX_AGE_MIN,
                             "run_hours": SHARE_RUN_HOURS, "run_pull": SHARE_RUN_PULL,
                             "break_window_hours": SHARE_BREAK_WINDOW_HOURS,
                             "break_abs_pp": SHARE_BREAK_ABS_PP,
                             "break_rel": SHARE_BREAK_REL,
                             "break_min_base_hours": SHARE_BREAK_MIN_BASE_HOURS,
                             "break_min_hours": SHARE_BREAK_MIN_HOURS,
                             "break_hold_hours": SHARE_BREAK_HOLD_HOURS,
                             "revision_hold_hours": SHARE_REVISION_HOLD_HOURS}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(entries), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "openrouter_share_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    _log(f"{ok} events with a fair entry, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
