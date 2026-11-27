#!/usr/bin/env python3
"""The nights pilot's morning email: every series in the bot's night family.

The family is the AS-test family "MLB" in incentive_mm.py -- the series in
IMM_ASTEST_MLB_SERIES, quoted 00:00-07:00 ET only, at x3, under one $100/day
loss halt and one stop file, to ASTEST_MLB_UNTIL (2026-12-03 05:00Z).
  - Jack 2026-11-26, "give email daily report": KXMLBNEXTTEAM alone (d032033).
  - Jack 2026-11-27, "Yes, extend it": every series in the family, the day 17
    more joined it (f236dbf).

Task Scheduler job "KL imm mlb-nights" runs it at 07:15 ET, after each night's
window closes (run_mlb_nights_report.ps1, hidden). Every number comes from the
bot's own sinks in STATUS_DIR, read-only, plus public reads of the current
prices and the active reward programs; nothing here imports incentive_mm.
  - MEMBERS: the bot's run config (config_history_<utc>.jsonl: each run's
    code:ASTEST_MLB_SERIES / _ENABLE / _UNTIL / _WINDOW_ET). A series is a
    member on night D when a bot run live in D's window had it, so a series
    added to the launcher shows up on its first night with no change here,
    and its "since start" runs from that night.
  - REWARDS, MODELLED: the cycle log's est_frac x pool_per_day over the gap
    since the previous cycle, capped at 900 s -- the reconciler's own rule
    (imm_reward_recon._scan_cycle_log). A market writes a cycle row only while
    it is quoted. One pass over a day's cycle log serves every series.
  - PAYOUT FLOOR, MODELLED: Kalshi pays a market's period only once it
    accrues $1. Each night counts in the reward period live in its window (the
    active programs, read each run and kept in OUT_DIR/programs_seen.jsonl,
    unioned per market as the bot's fetch_programs does), and each
    market-period's accrual is held against $1.
  - TRADING, MEASURED: every family fill (fills_*.jsonl) marked to Kalshi's
    last trade now (the dashboard's valuation since 2026-11-10), or to 100/0
    once the market settles -- mark-to-market including unsettled positions,
    before fees.
  - WHY A MARKET SAT OUT: the bot's selection-snapshot decisions in the
    window (under the $1 floor, an extreme or one-sided book, ...).
  - STOPS: the guard sink's entries (fill pauses, halts), sweep-breaker trips
    (ws_sweep_*.jsonl), the family's loss-halt day (imm_state.json) and the
    stop file.
The "night" of day D is D 00:00-07:00 ET; "since start" runs from a series'
first night to the run. A sent-marker per day keeps it to one email; --force
resends, --dry builds and prints without sending, --test prefixes the
subject, --series limits a check to named series (no marker). Exit 0 sent /
dry / already sent, 1 send failed.

  python send_mlb_nights_report.py --dry
  python send_mlb_nights_report.py --day 2026-11-28 --test
"""
from __future__ import annotations

import argparse
import csv
import html as _html
import json
import os
import re
import smtplib
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_logfiles  # noqa: E402

ET = ZoneInfo("America/New_York")
STATUS_DIR = os.environ.get("IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
# IMM_NIGHTS_OUT_DIR: write the report, caches and program reads elsewhere
# (a dry run from a worktree reads the live sinks without touching mlb_nights)
OUT_DIR = os.environ.get("IMM_NIGHTS_OUT_DIR") or os.path.join(STATUS_DIR, "mlb_nights")
SERIES = "KXMLBNEXTTEAM"           # the family's first member; the fallback
LABEL = "Nights pilot"
FIRST_NIGHT = date(2026, 11, 27)
# fallbacks for a bot run whose config lacks them (the run's own values win)
PILOT_END = datetime(2026, 12, 3, 5, 0, tzinfo=timezone.utc)
WINDOW_ET = ("00:00", "07:00")
CFG = "code:ASTEST_MLB_"
CONFIG_LOOKBACK_DAYS = 7
STOP_FILE = "astest_mlb_stop.txt"
HALT_KEY = "ASTEST:MLB"
FLOOR_USD = 1.0
MAX_DT = 900.0
MOVE_CENTS = 15.0
PROGRAM_PAGES = 30
MAX_ROWS = 25
SEND_TRIES, SEND_WAIT = 3, 20

# What each series was expected to make a night when it went live, written
# before its first night (MODELLED). KXMLBNEXTTEAM: the bot's floor projection
# at the go-live (47 markets, 14:58Z 11/26), rewards only. The other 17: the
# night tape test of 2026-11-27 19:49Z (run-logs/incentive-mm/report_tools/
# night_tape_2026-11-27/night_report.txt): rewards = the dashboard's x3
# estimate x 7/24, net = less the trading at the bot's measured 0.3x fill
# share. That test priced every market's night accrual as paid: it did not
# hold the $1-a-period floor against the nights left in each period, so it
# overstates the series whose period ends after one night.
# {series: (rewards a night, net a night or None, tape events)}
EXPECTED = {
    "KXMLBNEXTTEAM": (20.77, None, None),
    "KXUSACOMPANYSTAKE": (11.58, 11.02, 1),
    "KXAIMILLENNIUM": (9.37, 9.64, 2),
    "KXHORMUZMAX": (9.67, 7.80, 7),
    "KXVOICERANK": (6.89, 5.75, 3),
    "KXGTA6UNITS": (10.92, 5.68, 2),
    "KXUSHOME": (4.98, 4.96, 3),
    "KXNFCI": (5.12, 4.93, 2),
    "KXPCEHEAD": (3.61, 3.60, 3),
    "KXNVDABOARDTESTIFY": (3.40, 3.34, 1),
    "KXMULLINANNOUNCEOUT": (3.18, 3.13, 1),
    "KXAGICO": (3.63, 3.13, 1),
    "KXHOUSECONTEMPTANY": (3.24, 3.09, 1),
    "KXCBDECISIONKOREA": (3.16, 2.90, 3),
    "KXDUFFYANNOUNCEOUT": (2.95, 2.83, 1),
    "KXNVDACHARGE": (2.09, 2.09, 1),
    "KXFAOFPI": (2.64, 2.05, 2),
    "KXALBUMSTREAMS": (5.36, 1.92, 25),
}

INK, MUTED, LINE, GOOD, BAD = "#1f2328", "#59636e", "#d1d9e0", "#1a7f37", "#cf222e"
S_TD = f"padding:5px 8px;border-top:1px solid {LINE};font-size:13px;vertical-align:top"
S_TH = f"padding:5px 8px;border-bottom:2px solid {LINE};font-size:12px;color:{MUTED};font-weight:600;text-align:left"
S_SMALL = f"color:{MUTED};font-size:11px"


def setting(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0] or "")
    except Exception:
        return ""


def et_instant(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET).astimezone(timezone.utc)


def night_bounds(day: date, window: Tuple[str, str] = WINDOW_ET) -> Tuple[datetime, datetime]:
    """[start, end) UTC of day D's quote window ("24:00" = the next midnight)."""
    start = et_instant(day, window[0])
    end = (et_instant(day + timedelta(days=1), "00:00") if window[1] == "24:00"
           else et_instant(day, window[1]))
    return start, end


def utc_dates(start: datetime, end: datetime) -> List[date]:
    out, d = [], start.date()
    while d <= (end - timedelta(microseconds=1)).date():
        out.append(d)
        d += timedelta(days=1)
    return out


def _day_path(prefix: str, d: date, ext: str) -> Optional[str]:
    p = imm_logfiles.resolve(os.path.join(STATUS_DIR, f"{prefix}_{d.isoformat()}.{ext}"))
    return p if p and os.path.exists(p) else None


def _ts(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _as_set(series) -> Set[str]:
    return {series} if isinstance(series, str) else set(series)


def series_of(t: str) -> str:
    return t.split("-")[0]


def event_of(t: str) -> str:
    parts = t.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else t


def short(t: str) -> str:
    """'KXMLBNEXTTEAM-26GSPRINGER4-TOR' -> 'GSPRINGER to TOR'; any other
    ticker without its series ('KXNFCI-26NOV27-T0.5' -> '26NOV27-T0.5')."""
    parts = t.split("-")
    if parts[0] == "KXMLBNEXTTEAM" and len(parts) >= 3:
        who = parts[1][2:].rstrip("0123456789") or parts[1]
        return f"{who} to {parts[2]}"
    return "-".join(parts[1:]) if len(parts) > 1 else t


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(path + ".tmp", path)
    except OSError as e:
        print(f"write failed ({os.path.basename(path)}): {type(e).__name__}")


# the guard sink's names in plain words (the rest show as they are)
GUARD_WORDS = {
    "astest_fill_pause": "fill pause (event aside 15 min)",
    "twk_halt": "loss halt ($100 budget)",
    "sweep_trip": "sweep breaker (event pulled)",
    "move_breaker": "mid-move breaker",
    "fill_burst": "fill burst",
    "manual_yield": "your hand trades (bot yields)",
}
# the selection snapshot's decisions for a market that sat out
DECISION_WORDS = {
    "payout_floor": "under the $1 floor",
    "hopeless": "can't reach $1",
    "extreme_mid": "price extreme",
    "one_sided": "one-sided book",
    "cutoff": "past its cutoff",
    "selected": "selected, not quoted",
}


# ---- the family: who was a member, when --------------------------------------

def _parse_members(v) -> Optional[frozenset]:
    """code:ASTEST_MLB_SERIES as config_history stores it (a frozenset repr),
    or a comma list; None when the run's config has no such key."""
    if v is None:
        return None
    if isinstance(v, (list, tuple, set, frozenset)):
        return frozenset(str(x) for x in v if x)
    s = str(v).strip()
    names = re.findall(r"'([^']+)'", s) if s[:1] in ("{", "f", "s", "(", "[") else s.split(",")
    return frozenset(n.strip() for n in names if n.strip())


def _parse_window(v) -> Optional[Tuple[str, str]]:
    m = re.findall(r"\d{1,2}:\d{2}", str(v or ""))
    return (m[0], m[1]) if len(m) == 2 else None


def family_runs(lo: datetime, hi: datetime) -> List[dict]:
    """The bot's runs from config_history, oldest first: the run live at `lo`
    (the last one started before it, looking back CONFIG_LOOKBACK_DAYS) and
    every run started in [lo, hi). Each {ts, run_id, members, until, window};
    members is None for a run whose config predates the family, empty when
    the family was switched off."""
    rows, d = [], (lo - timedelta(days=CONFIG_LOOKBACK_DAYS)).date()
    while d <= hi.date():
        p = _day_path("config_history", d, "jsonl")
        if p:
            with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ts = _ts(r.get("ts"))
                    c = r.get("config") if isinstance(r.get("config"), dict) else {}
                    if ts is None:
                        continue
                    members = _parse_members(c.get(CFG + "SERIES"))
                    if members is not None and str(c.get(CFG + "ENABLE")) != "True":
                        members = frozenset()
                    until = c.get(CFG + "UNTIL")
                    rows.append({"ts": ts, "run_id": r.get("run_id"), "members": members,
                                 "until": (_ts(str(until).replace(" ", "T"))
                                           if until not in (None, "", "None") else None),
                                 "window": _parse_window(c.get(CFG + "WINDOW_ET"))})
        d += timedelta(days=1)
    rows.sort(key=lambda r: r["ts"])
    before = [r for r in rows if r["ts"] <= lo.timestamp()]
    return before[-1:] + [r for r in rows if lo.timestamp() < r["ts"] < hi.timestamp()]


def night_members(runs: List[dict], day: date, window: Tuple[str, str]) -> Set[str]:
    """The series any bot run live in day D's window had in the family (a run
    is live from its start to the next run's), before its UNTIL."""
    ns, ne = (x.timestamp() for x in night_bounds(day, window))
    out: Set[str] = set()
    for i, r in enumerate(runs):
        nxt = runs[i + 1]["ts"] if i + 1 < len(runs) else float("inf")
        if r["ts"] < ne and nxt > ns and r["members"] and (r["until"] is None or ns < r["until"]):
            out |= r["members"]
    return out


def family(day: date, now: datetime, series: Optional[Iterable[str]] = None,
           window: Optional[Tuple[str, str]] = None) -> dict:
    """{window, until, nights, members: {night: set}, first / last: {series:
    night}, source}. `series` (checks) replaces the members on every night."""
    nights = [FIRST_NIGHT + timedelta(days=i) for i in range(max(0, (day - FIRST_NIGHT).days + 1))]
    lo = night_bounds(FIRST_NIGHT, window or WINDOW_ET)[0]
    runs = family_runs(lo, max(now, night_bounds(day, window or WINDOW_ET)[1]))
    window = window or next((r["window"] for r in reversed(runs) if r["window"]), WINDOW_ET)
    until = next((r["until"] for r in reversed(runs) if r["until"]), PILOT_END.timestamp())
    if series:
        source = "named on the command line"
        members = {d: set(series) for d in nights}
    elif any(r["members"] is not None for r in runs):
        source = "the bot's run config"
        members = {d: night_members(runs, d, window) for d in nights}
    else:
        source = f"no run config found: {SERIES} only"
        members = {d: ({SERIES} if night_bounds(d, window)[0].timestamp() < until else set())
                   for d in nights}
    first: Dict[str, date] = {}
    last: Dict[str, date] = {}
    for d in nights:
        for s in members[d]:
            first.setdefault(s, d)
            last[s] = d
    return {"window": window, "until": until, "nights": nights, "members": members,
            "first": first, "last": last, "source": source}


# ---- the sinks ------------------------------------------------------------

def load_fills(series, start: datetime, end: datetime) -> List[dict]:
    """Our fills on `series` (one or a set) stamped in [start, end), one per
    fill_id."""
    want = _as_set(series)
    seen, out = set(), []
    for d in utc_dates(start, end):
        p = _day_path("fills", d, "jsonl")
        if not p:
            continue
        with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(s in line for s in want):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = _ts(r.get("ts"))
                if r.get("series") not in want or ts is None or not (start.timestamp() <= ts < end.timestamp()):
                    continue
                fid = r.get("fill_id") or f"{r.get('order_id')}:{ts}:{r.get('count')}"
                if fid in seen:
                    continue
                seen.add(fid)
                out.append(r)
    out.sort(key=lambda r: _ts(r.get("ts")) or 0)
    return out


def fill_sign(r: dict) -> float:
    """+1 when the fill made us longer YES, -1 shorter."""
    obs = r.get("our_book_side")
    if obs in ("bid", "ask"):
        return 1.0 if obs == "bid" else -1.0
    return 1.0 if (r.get("side") == "yes") == (r.get("action") == "buy") else -1.0


def _scan_day(path: str, series) -> Dict[str, Dict[str, Dict[str, list]]]:
    """{series: {ticker: {utc_hour_index: [est, cycles, first_mid, last_mid,
    max_bid_ct, max_ask_ct]}}} for one cycle-log day file, every series in
    `series` in ONE pass: the reconciler's accrual (each cycle's est_frac x
    pool_per_day over the gap since the previous DISTINCT timestamp, capped
    at MAX_DT; the day's first cycle gets no gap, its cross-file seam),
    bucketed by the hour of the cycle it lands on. A day file is ~900 MB:
    every line's timestamp is read off its first 20 characters, its series
    off the ticker that follows, and only the wanted series' lines are
    parsed."""
    want = _as_set(series)
    out: Dict[str, Dict[str, Dict[str, list]]] = {s: {} for s in want}
    ts_cache: Dict[str, float] = {}
    prev_ts = cur_ts = None
    batch: List[Tuple[str, list]] = []

    def flush(ts, rows, dt):
        hour = str(int(ts // 3600))
        for s, row in rows:
            try:
                frac, pool = float(row[7]), float(row[11])
                bid, ask = float(row[2] or 0), float(row[3] or 0)
                bct = float(row[13] or 0) if len(row) > 14 else 0.0
                act = float(row[14] or 0) if len(row) > 14 else 0.0
            except (ValueError, IndexError):
                continue
            h = out[s].setdefault(row[1], {}).setdefault(hour, [0.0, 0, None, None, 0.0, 0.0])
            h[0] += frac * pool * dt / 86400.0
            h[1] += 1
            if 0 < bid < ask:
                mid = (bid + ask) / 2.0
                if h[2] is None:
                    h[2] = mid
                h[3] = mid
            h[4] = max(h[4], bct)
            h[5] = max(h[5], act)

    with imm_logfiles.open_text(path, newline="", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s20 = line[:20]
            ts = ts_cache.get(s20)
            if ts is None:
                try:
                    ts = datetime.strptime(s20, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue                       # the header, a torn line
                ts_cache[s20] = ts
            if cur_ts is None:
                cur_ts = ts
            elif ts != cur_ts:
                if prev_ts and batch:
                    dt = min(cur_ts - prev_ts, MAX_DT)
                    if dt > 0:
                        flush(cur_ts, batch, dt)
                prev_ts, cur_ts, batch = cur_ts, ts, []
            j = line.find("-", 21, 100)
            if j < 0 or line[21:j] not in want:
                continue
            row = next(csv.reader([line]))
            s = line[21:j]
            if len(row) >= 13 and row[1].startswith(s + "-"):
                batch.append((s, row))
        if batch and prev_ts:
            dt = min(cur_ts - prev_ts, MAX_DT)
            if dt > 0:
                flush(cur_ts, batch, dt)
    return out


def _day_hours(d: date, want: Set[str], today: date, memo: dict) -> Dict[str, Dict[str, Dict[str, list]]]:
    """The day's hourly buckets for `want`, scanning the day file at most once
    a run (memo) and caching finished days (before `today`; a finished day
    file no longer changes) in OUT_DIR/cycle_cache.json by its signature."""
    p = _day_path("cycle_log", d, "csv")
    if not p:
        return {s: {} for s in want}
    key = d.isoformat()
    got = memo.setdefault(key, {})
    need = set(want) - set(got)
    if need and d < today:
        cpath = os.path.join(OUT_DIR, "cycle_cache.json")
        cache = _load_json(cpath, {})
        sig = imm_logfiles.sig(p)
        hit = cache.get(key) if isinstance(cache.get(key), dict) else {}
        kept = dict(hit.get("series") or {}) if hit.get("sig") == sig else {}
        for s in need & set(kept):
            got[s] = kept[s]
        need -= set(kept)
        if need:
            scanned = _scan_day(p, need)
            got.update(scanned)
            kept.update(scanned)
            cache[key] = {"sig": sig, "series": kept}
            _save_json(cpath, cache)
    elif need:
        got.update(_scan_day(p, need))
    return {s: got.get(s, {}) for s in want}


def hours_for(windows: Dict[str, Tuple[datetime, datetime]],
              today: Optional[date] = None) -> Dict[str, Dict[str, Dict[str, list]]]:
    """{series: {ticker: {utc_hour_index: [...]}}} over every UTC day the
    series' windows touch: one scan per day file for all of them."""
    today = today or datetime.now(timezone.utc).date()
    need: Dict[date, Set[str]] = defaultdict(set)
    for s, (lo, hi) in windows.items():
        for d in utc_dates(lo, hi):
            need[d].add(s)
    out: Dict[str, Dict[str, Dict[str, list]]] = {s: {} for s in windows}
    memo: dict = {}
    for d in sorted(need):
        for s, ticks in _day_hours(d, need[d], today, memo).items():
            for t, hrs in ticks.items():
                out[s].setdefault(t, {}).update(hrs)
    return out


def agg_cycles(hours: Dict[str, Dict[str, list]], start: datetime, end: datetime) -> Dict[str, dict]:
    """{ticker: {est, cycles, first_mid, last_mid, max_bid_ct, max_ask_ct}}
    over the cycles whose hour starts in [start, end) (the windows run on
    whole hours)."""
    lo, hi = start.timestamp(), end.timestamp()
    out: Dict[str, dict] = {}
    for t, hrs in hours.items():
        for hk in sorted(hrs, key=int):
            if not (lo <= int(hk) * 3600 < hi):
                continue
            v = hrs[hk]
            m = out.setdefault(t, {"est": 0.0, "cycles": 0, "first_mid": None,
                                   "last_mid": None, "max_bid_ct": 0.0, "max_ask_ct": 0.0})
            m["est"] += v[0]
            m["cycles"] += v[1]
            if v[2] is not None and m["first_mid"] is None:
                m["first_mid"] = v[2]
            if v[3] is not None:
                m["last_mid"] = v[3]
            m["max_bid_ct"] = max(m["max_bid_ct"], v[4])
            m["max_ask_ct"] = max(m["max_ask_ct"], v[5])
    return out


def scan_cycles(series: str, start: datetime, end: datetime,
                today: Optional[date] = None) -> Dict[str, dict]:
    """One series' agg_cycles over [start, end)."""
    return agg_cycles(hours_for({series: (start, end)}, today)[series], start, end)


def load_guards(series, start: datetime, end: datetime) -> Dict[str, Dict[str, dict]]:
    """{series: {guard: {"enters": n, "markets": set, "events": set}}} from the
    guard sink's `enter` rows, plus "sweep_trip" from ws_sweep."""
    want = _as_set(series)
    out: Dict[str, Dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"enters": 0, "markets": set(), "events": set()}))
    lo, hi = start.timestamp(), end.timestamp()
    for d in utc_dates(start, end):
        for prefix, kind in (("guard_skips", "guard"), ("ws_sweep", "sweep")):
            p = _day_path(prefix, d, "jsonl")
            if not p:
                continue
            with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not any(s in line for s in want):
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ts = _ts(r.get("ts"))
                    if ts is None or not (lo <= ts < hi):
                        continue
                    t = str(r.get("ticker") or r.get("event") or "")
                    s = series_of(t)
                    if s not in want:
                        continue
                    if kind == "guard":
                        if r.get("kind") != "enter":
                            continue
                        g = str(r.get("guard") or "?")
                    else:
                        g = "sweep_trip"
                    o = out[s][g]
                    o["enters"] += 1
                    o["markets"].add(t)
                    o["events"].add(event_of(t) if t.count("-") >= 2 else t)
    return {s: dict(v) for s, v in out.items()}


_SNAP_HEAD = re.compile(r'^\{"ts": "([^"]+)", "ticker": "([^"]+)", "decision": "([^"]*)"')


def load_selection(series, start: datetime, end: datetime) -> Dict[str, Dict[str, Counter]]:
    """{series: {ticker: Counter(decision)}} from the selection snapshot's
    rows stamped in [start, end): every market the bot weighed, and why."""
    want = _as_set(series)
    out: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    lo, hi = start.timestamp(), end.timestamp()
    for d in utc_dates(start, end):
        p = _day_path("selection_snapshot", d, "jsonl")
        if not p:
            continue
        with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _SNAP_HEAD.match(line)
                if m:
                    ts_s, t, dec = m.groups()
                else:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ts_s, t, dec = r.get("ts"), str(r.get("ticker") or ""), str(r.get("decision") or "")
                s = series_of(t)
                if s not in want:
                    continue
                ts = _ts(ts_s)
                if ts is None or not (lo <= ts < hi):
                    continue
                out[s][t][dec] += 1
    return {s: dict(v) for s, v in out.items()}


def halt_day() -> Optional[str]:
    try:
        with open(os.path.join(STATUS_DIR, "imm_state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        return (st.get("twk_halt_day") or {}).get(HALT_KEY)
    except (OSError, ValueError, AttributeError):
        return None


def fetch_marks(tickers: List[str]) -> Dict[str, dict]:
    """{ticker: {bid, ask, last, status, result}} in cents, from Kalshi's
    public market reads (kalshi_reads: signed first, paced); {} on failure."""
    out: Dict[str, dict] = {}
    if not tickers:
        return out
    try:
        from kalshi_reads import kalshi_get
    except Exception as e:  # noqa: BLE001
        print(f"marks: kalshi_reads unavailable ({type(e).__name__})")
        return out
    c = lambda v: float(v) * 100.0 if v not in (None, "") else None
    for i in range(0, len(tickers), 20):
        try:
            js = kalshi_get("/markets", {"tickers": ",".join(tickers[i:i + 20]), "limit": 100})
        except Exception as e:  # noqa: BLE001
            print(f"marks: read failed ({type(e).__name__}: {str(e)[:80]})")
            continue
        for m in js.get("markets") or []:
            out[m["ticker"]] = {"bid": c(m.get("yes_bid_dollars")), "ask": c(m.get("yes_ask_dollars")),
                                "last": c(m.get("last_price_dollars")), "status": m.get("status"),
                                "result": m.get("result") or ""}
    return out


def fetch_programs(tickers: Set[str], now: datetime) -> Optional[Dict[str, Tuple[float, float]]]:
    """{ticker: (start_ts, end_ts)}: each market's active liquidity reward
    window now, overlapping programs unioned (the bot's fetch_programs), from
    GET /incentive_programs?status=active paged by next_cursor (kalshi_reads);
    None when the read fails."""
    if not tickers:
        return {}
    try:
        from kalshi_reads import kalshi_get
    except Exception as e:  # noqa: BLE001
        print(f"programs: kalshi_reads unavailable ({type(e).__name__})")
        return None
    out: Dict[str, Tuple[float, float]] = {}
    cursor, now_ts = None, now.timestamp()
    try:
        for _ in range(PROGRAM_PAGES):
            params = {"limit": 1000, "status": "active"}
            if cursor:
                params["cursor"] = cursor
            js = kalshi_get("/incentive_programs", params) or {}
            batch = js.get("incentive_programs") or []
            for p in batch:
                t = p.get("market_ticker") or ""
                if t not in tickers or p.get("incentive_type") != "liquidity" or p.get("paid_out"):
                    continue
                a, b = _ts(p.get("start_date")), _ts(p.get("end_date"))
                if a is None or b is None or not (a <= now_ts < b):
                    continue
                cur = out.get(t)
                out[t] = (a, b) if cur is None else (min(cur[0], a), max(cur[1], b))
            cursor = js.get("next_cursor")
            if not cursor or not batch:
                break
    except Exception as e:  # noqa: BLE001
        print(f"programs: read failed ({type(e).__name__}: {str(e)[:80]})")
        return None
    return out


def program_reads(now: datetime, current: Optional[Dict[str, Tuple[float, float]]]) -> List[tuple]:
    """[(read_ts, {ticker: (start, end)})], oldest first: every read kept in
    OUT_DIR/programs_seen.jsonl, then this run's."""
    out = []
    try:
        with open(os.path.join(OUT_DIR, "programs_seen.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out.append((float(r["read_at"]), {t: (float(w[0]), float(w[1]))
                                                      for t, w in (r.get("programs") or {}).items()}))
                except (ValueError, KeyError, TypeError, IndexError):
                    continue
    except OSError:
        pass
    if current is not None:
        out.append((now.timestamp(), current))
    out.sort(key=lambda x: x[0])
    return out


def keep_programs(now: datetime, current: Dict[str, Tuple[float, float]]) -> None:
    """Append this run's read, so a later run knows a period after it ends."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "programs_seen.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"read_at": now.timestamp(), "at": now.isoformat(),
                                 "programs": {t: list(w) for t, w in sorted(current.items())}}) + "\n")
    except OSError as e:
        print(f"programs: keep failed ({type(e).__name__})")


def period_of(t: str, at: float, reads: List[tuple]) -> Optional[Tuple[float, float]]:
    """The reward window covering `at` for market t: from the first read made
    at or after `at` that covers it (the next morning's), else the last one
    made before it."""
    hits = [(rt, progs[t]) for rt, progs in reads
            if t in progs and progs[t][0] <= at < progs[t][1]]
    after = [h for h in hits if h[0] >= at]
    if after:
        return min(after)[1]
    return max(hits)[1] if hits else None


def mark_of(t: str, marks: Dict[str, dict], fallback: Optional[float]) -> Tuple[Optional[float], str]:
    """(cents, basis): the settlement once there is a result, else Kalshi's
    last trade, else the mid, else `fallback` (the last cycle mid or the
    fill's own price)."""
    m = marks.get(t) or {}
    if m.get("result") in ("yes", "no"):
        return (100.0 if m["result"] == "yes" else 0.0), "settled"
    if m.get("last") is not None and m["last"] > 0:
        return m["last"], "last"
    if m.get("bid") is not None and m.get("ask") and m["ask"] > (m["bid"] or 0):
        return (m["bid"] + m["ask"]) / 2.0, "mid"
    return fallback, "stale"


# ---- the numbers ------------------------------------------------------------

def summarize(fills: List[dict], cyc: Dict[str, dict], marks: Dict[str, dict]) -> dict:
    """Rewards (modelled), trading P&L at the marks (measured), positions."""
    rew = sum(v["est"] for v in cyc.values())
    quoted = {t for t, v in cyc.items() if v["cycles"] > 0}
    pnl, rows, pos = 0.0, [], defaultdict(lambda: [0.0, 0.0])   # net, cost
    for r in fills:
        t = r.get("ticker") or ""
        n = float(r.get("count") or 0)
        px = float(r.get("yes_price_cents") or 0)
        s = fill_sign(r)
        fb = (cyc.get(t) or {}).get("last_mid")
        mk, basis = mark_of(t, marks, fb if fb is not None else px)
        p = s * ((mk if mk is not None else px) - px) * n / 100.0
        pnl += p
        pos[t][0] += s * n
        pos[t][1] += s * n * px
        rows.append({"ts": _ts(r.get("ts")), "ticker": t, "side": "bought" if s > 0 else "sold",
                     "px": px, "n": n, "mark": mk, "basis": basis, "pnl": p,
                     "taker": bool(r.get("is_taker"))})
    positions = []
    for t, (net, cost) in pos.items():
        if abs(net) < 1e-9:
            continue
        mk, basis = mark_of(t, marks, (cyc.get(t) or {}).get("last_mid"))
        avg = cost / net
        positions.append({"ticker": t, "net": net, "avg": avg, "mark": mk, "basis": basis,
                          "mtm": net * ((mk if mk is not None else avg) - avg) / 100.0})
    positions.sort(key=lambda x: x["mtm"])
    return {"rew": rew, "pnl": pnl, "net": rew + pnl, "fills": rows, "positions": positions,
            "quoted": quoted, "events": {event_of(t) for t in quoted},
            "bought": sum(x["n"] for x in rows if x["side"] == "bought"),
            "sold": sum(x["n"] for x in rows if x["side"] == "sold")}


def merge(parts: List[dict]) -> dict:
    """Family totals of per-series summaries (their tickers never overlap)."""
    out = {"rew": 0.0, "pnl": 0.0, "net": 0.0, "fills": [], "positions": [], "quoted": set(),
           "events": set(), "bought": 0.0, "sold": 0.0}
    for p in parts:
        for k in ("rew", "pnl", "net", "bought", "sold"):
            out[k] += p[k]
        out["fills"] += p["fills"]
        out["positions"] += p["positions"]
        out["quoted"] |= p["quoted"]
        out["events"] |= p["events"]
    out["fills"].sort(key=lambda x: x["ts"] or 0)
    out["positions"].sort(key=lambda x: x["mtm"])
    return out


def moves(cyc: Dict[str, dict], marks: Dict[str, dict]) -> List[dict]:
    """Quoted markets whose price moved MOVE_CENTS or more from their first
    quoted cycle to now, or that settled: where news landed."""
    out = []
    for t, v in cyc.items():
        if v["first_mid"] is None:
            continue
        mk, basis = mark_of(t, marks, v["last_mid"])
        if mk is None:
            continue
        if abs(mk - v["first_mid"]) >= MOVE_CENTS or basis == "settled":
            out.append({"ticker": t, "from": v["first_mid"], "to": mk, "basis": basis})
    out.sort(key=lambda x: -abs(x["to"] - x["from"]))
    return out


def nights_left_in(end_ts: float, until_ts: float, day: date, window: Tuple[str, str]) -> int:
    """The family's windows after day D that open before a period's end."""
    n, d = 0, day + timedelta(days=1)
    while night_bounds(d, window)[0].timestamp() < min(end_ts, until_ts) and n < 60:
        n += 1
        d += timedelta(days=1)
    return n


def floor_view(by_night: Dict[str, Dict[date, float]], reads: List[tuple], day: date,
               window: Tuple[str, str], until_ts: float, now: datetime) -> dict:
    """Each market-period's modelled accrual against FLOOR_USD. A night counts
    in the period covering its window's start. open: periods still running
    (past $1 so far, on pace = what's left of the period's nights accruing
    like tonight gets there); closed: periods over (reached $1, and the
    accrual in the ones that didn't -- Kalshi pays nothing there)."""
    op = {"mkts": 0, "past": 0, "on_pace": 0, "ends": Counter()}
    cl = {"n": 0, "past": 0, "under_usd": 0.0}
    unknown = 0.0
    now_ts = now.timestamp()
    for t, nights in by_night.items():
        per: Dict[Tuple[float, float], float] = defaultdict(float)
        for d, est in nights.items():
            if est <= 0:
                continue
            w = period_of(t, night_bounds(d, window)[0].timestamp(), reads)
            if w is None:
                unknown += est
            else:
                per[w] += est
        for w, acc in per.items():
            if w[1] <= now_ts:
                cl["n"] += 1
                if acc >= FLOOR_USD:
                    cl["past"] += 1
                else:
                    cl["under_usd"] += acc
                continue
            op["mkts"] += 1
            op["ends"][w[1]] += 1
            if acc >= FLOOR_USD:
                op["past"] += 1
            elif nights.get(day, 0.0) > 0 and \
                    acc + nights[day] * nights_left_in(w[1], until_ts, day, window) >= FLOOR_USD:
                op["on_pace"] += 1
    return {"open": op, "closed": cl, "unknown_usd": unknown}


def _decision(c: Counter) -> str:
    return c.most_common(1)[0][0] if c else "?"


# ---- the email --------------------------------------------------------------

def usd(x: float, signed: bool = True) -> str:
    return (f"{'+' if x >= 0 else '-'}${abs(x):,.2f}" if signed else f"${x:,.2f}")


def _tbl(head: List[str], rows: List[List[str]]) -> str:
    th = "".join(f'<th style="{S_TH}">{_html.escape(h)}</th>' for h in head)
    tr = "".join("<tr>" + "".join(f'<td style="{S_TD}">{c}</td>' for c in r) + "</tr>" for r in rows)
    return f'<table style="border-collapse:collapse;margin:6px 0 14px 0">{"<tr>" + th + "</tr>"}{tr}</table>'


def _col(x: float) -> str:
    return f'<span style="color:{GOOD if x >= 0 else BAD}">{usd(x)}</span>'


def _small(s: str) -> str:
    return f'<br><span style="{S_SMALL}">{s}</span>' if s else ""


def _et(ts: float, fmt: str = "%a %b %d %H:%M") -> str:
    return datetime.fromtimestamp(ts, ET).strftime(fmt)


def _expected_cell(s: str) -> str:
    e = EXPECTED.get(s)
    if not e:
        return "-"
    rew, net, ev = e
    if net is None:
        return f"{usd(rew, False)} rewards" + _small("bot projection")
    return f"{usd(net)} net" + _small(f"rewards {usd(rew, False)}; {ev} tape event{'s' if ev != 1 else ''}")


def _floor_cell(fv: dict) -> str:
    op, cl = fv["open"], fv["closed"]
    parts = []
    if op["mkts"]:
        s = f"{op['past']}/{op['mkts']} past $1" + (f", {op['on_pace']} on pace" if op["on_pace"] else "")
        end = max(op["ends"], key=lambda k: op["ends"][k])
        parts.append(s + _small(f"period ends {_et(end)} ET"))
    if cl["n"]:
        parts.append(f"closed: {cl['past']}/{cl['n']} reached $1"
                     + (_small(f"{usd(cl['under_usd'], False)} under $1, unpaid") if cl["under_usd"] > 0.005 else ""))
    if fv["unknown_usd"] > 0.005:
        parts.append(f"period unknown for {usd(fv['unknown_usd'], False)}")
    return "<br>".join(parts) if parts else "-"


def build(day: date, now: Optional[datetime] = None, marks: Optional[Dict[str, dict]] = None,
          series: Optional[Iterable[str]] = None, window: Optional[Tuple[str, str]] = None,
          programs: Optional[Dict[str, Tuple[float, float]]] = None) -> Tuple[str, str, str, dict]:
    """(subject, html, text, ctx) for the night of `day`, over every series
    in the night family (or `series`, for checks). `marks` and `programs`
    skip the Kalshi reads (tests)."""
    now = now or datetime.now(timezone.utc)
    timing, t0 = {}, time.time()

    def lap(name):                         # each phase's seconds, for task.log
        nonlocal t0
        timing[name] = round(time.time() - t0, 1)
        t0 = time.time()

    if isinstance(series, str):
        series = [series]
    fam = family(day, now, series, window)
    window, until_ts = fam["window"], fam["until"]
    until = datetime.fromtimestamp(until_ts, timezone.utc)
    n_start, n_end = night_bounds(day, window)
    tonight = fam["members"].get(day, set())
    everyone = sorted(fam["first"])
    spans: Dict[str, Tuple[datetime, datetime]] = {}
    for s in everyone:
        lo = night_bounds(fam["first"][s], window)[0]
        hi = now if s in tonight else min(now, night_bounds(fam["last"][s], window)[1] + timedelta(hours=1))
        spans[s] = (lo, max(lo, hi))
    lap("config")
    hours = hours_for(spans, now.astimezone(timezone.utc).date()) if spans else {}
    lap("cycle_log")
    all_fills = load_fills(set(everyone), min(v[0] for v in spans.values()), now) if spans else []
    fills_by = defaultdict(list)
    for r in all_fills:
        fills_by[r.get("series")].append(r)
    tickers = sorted({r["ticker"] for r in all_fills if r.get("ticker")}
                     | {t for s in everyone for t in hours.get(s, {})})
    lap("fills")
    marks = fetch_marks(tickers) if marks is None else marks
    lap("marks")
    fetched = programs is None
    if fetched:
        programs = fetch_programs(set(tickers), now)
        if programs:
            keep_programs(now, programs)
    reads = program_reads(now, programs) if fetched else ([(now.timestamp(), programs)] if programs is not None else [])
    lap("programs")
    guards = load_guards(set(tonight), n_start, n_end) if tonight else {}
    sel = load_selection(set(tonight), n_start, n_end) if tonight else {}
    lap("guards_selection")

    rows, n_parts, s_parts, mv = [], [], [], []
    for s in everyone:
        lo, hi = spans[s]
        h = hours.get(s, {})
        nf = [r for r in fills_by.get(s, []) if n_start.timestamp() <= (_ts(r.get("ts")) or 0) < n_end.timestamp()] \
            if s in tonight else []
        sf = [r for r in fills_by.get(s, []) if lo.timestamp() <= (_ts(r.get("ts")) or 0) < hi.timestamp()]
        ncyc = agg_cycles(h, n_start, n_end) if s in tonight else {}
        scyc = agg_cycles(h, lo, hi)
        night, since = summarize(nf, ncyc, marks), summarize(sf, scyc, marks)
        n_parts.append(night)
        s_parts.append(since)
        mv += moves(scyc, marks)
        member_nights = [d for d in fam["nights"] if s in fam["members"][d]]
        by_night: Dict[str, Dict[date, float]] = defaultdict(dict)
        for d in member_nights:
            for t, v in agg_cycles(h, *night_bounds(d, window)).items():
                by_night[t][d] = v["est"]
        fv = floor_view(by_night, reads, day, window, until_ts, now) if reads else None
        cands = sel.get(s, {})
        sat = {t: c for t, c in cands.items() if t not in night["quoted"]}
        reasons = Counter(_decision(c) for c in sat.values())
        rows.append({"series": s, "first": fam["first"][s], "member": s in tonight,
                     "nights": len(member_nights), "night": night, "since": since,
                     "candidates": len(set(cands) | night["quoted"]), "reasons": reasons,
                     "floor": fv, "guards": guards.get(s, {}), "expected": EXPECTED.get(s)})
    rows.sort(key=lambda r: (not r["member"], -r["night"]["net"], -(r["expected"] or (0, 0))[0], r["series"]))
    night, since = merge(n_parts), merge(s_parts)
    mv.sort(key=lambda x: -abs(x["to"] - x["from"]))

    last_night = (until - timedelta(minutes=1)).astimezone(ET).date()
    nights_done = max(0, (min(day, last_night) - FIRST_NIGHT).days + 1)
    nights_left = max(0, (last_night - day).days)
    stopped = os.path.exists(os.path.join(STATUS_DIR, STOP_FILE))
    hd = halt_day()
    status = ("STOPPED (stop file)" if stopped else
              "ENDED" if now >= until else
              f"HALTED {hd} (loss budget)" if hd and hd >= day.isoformat() else "ON")
    quoted_series = sum(1 for r in rows if r["night"]["quoted"])
    fl = [r["floor"] for r in rows if r["floor"]]
    fam_floor = {"open_mkts": sum(f["open"]["mkts"] for f in fl), "open_past": sum(f["open"]["past"] for f in fl),
                 "on_pace": sum(f["open"]["on_pace"] for f in fl), "closed": sum(f["closed"]["n"] for f in fl),
                 "closed_past": sum(f["closed"]["past"] for f in fl),
                 "under_usd": sum(f["closed"]["under_usd"] for f in fl)} if fl else None
    ctx = {"day": day.isoformat(), "night": night, "since": since, "moves": mv, "rows": rows,
           "status": status, "nights_done": nights_done, "nights_left": nights_left,
           "members_tonight": sorted(tonight), "series_quoted": quoted_series, "floor": fam_floor,
           "source": fam["source"], "window": window, "until": until.isoformat(),
           "programs_read": programs is not None, "timing": timing}
    wd = f"{day:%a %b} {day.day}"
    nf_ = len(night["fills"])
    subject = (f"{LABEL}, {wd}: {usd(night['net'])} net (rewards {usd(night['rew'], False)} modelled, "
               f"trading {usd(night['pnl'])}), {nf_} fill{'s' if nf_ != 1 else ''}, "
               f"{quoted_series} of {len(tonight)} series quoted; since start {usd(since['net'])}")

    def block(lbl, s, n_series):
        return [lbl, n_series, f"{len(s['quoted'])} / {len(s['events'])}", f"{len(s['fills'])}",
                f"{s['bought']:,.0f} / {s['sold']:,.0f}", usd(s["rew"], False), _col(s["pnl"]), _col(s["net"])]

    others = len(tonight) - (1 if SERIES in tonight else 0)
    who = (f"{SERIES} and {others} more" if SERIES in tonight and others else
           f"{len(tonight)} series" if tonight else "no series")
    h = [f'<div style="font-family:Segoe UI,Arial,sans-serif;color:{INK};max-width:1000px">',
         f'<h2 style="margin:0 0 4px 0;font-size:18px">{LABEL}: night of {wd} '
         f'({window[0]}-{window[1]} ET)</h2>',
         f'<p style="margin:0 0 10px 0;color:{MUTED};font-size:13px">The bot\'s night family '
         f'tonight: {who} ({fam["source"]}), x3, quoted {window[0]}-{window[1]} ET only, to '
         f'{until:%b %d %H:%M}Z. Status: <b>{status}</b>. Night {nights_done} of '
         f'{nights_done + nights_left}; {nights_left} left.</p>',
         _tbl(["", "series quoted", "markets / events quoted", "fills", "bought / sold ct",
               "rewards (modelled)", "trading (at last trade)", "net"],
              [block("This night", night, f"{quoted_series} of {len(tonight)}"),
               block("Since start", since, f"{sum(1 for r in rows if r['since']['quoted'])} of {len(rows)}")])]
    if fam_floor:
        h.append(f'<p style="margin:0 0 10px 0;font-size:13px">Payout floor (modelled: Kalshi pays a '
                 f'market\'s period only once it accrues $1): in the periods still open, '
                 f'{fam_floor["open_past"]} of {fam_floor["open_mkts"]} quoted markets are past $1 and '
                 f'{fam_floor["on_pace"]} more are on pace. Periods closed since the start: '
                 f'{fam_floor["closed_past"]} of {fam_floor["closed"]} reached $1'
                 + (f'; {usd(fam_floor["under_usd"], False)} of the modelled rewards sat in the rest '
                    f'(unpaid)' if fam_floor["under_usd"] > 0.005 else "")
                 + ('' if programs is not None else
                    ' (from the reward periods kept by earlier runs: this run\'s program read failed)')
                 + '.</p>')
    else:
        h.append(f'<p style="margin:0 0 10px 0;font-size:13px">Payout floor: not checked (the reward '
                 f'program read failed).</p>')

    srows = []
    for r in rows:
        n, s = r["night"], r["since"]
        why = ", ".join(f"{DECISION_WORDS.get(k, k.replace('_', ' '))} {v}" for k, v in r["reasons"].most_common(2))
        tag = "" if r["member"] else "not in the family tonight"
        if r["first"] != FIRST_NIGHT:
            tag = (tag + "; " if tag else "") + f"from {r['first']:%b} {r['first'].day}"
        srows.append([
            _html.escape(r["series"]) + _small(tag),
            (f"{len(n['quoted'])} of {r['candidates']}" + _small(f"sat out: {why}" if why else "")) if r["member"] else "-",
            (f"{len(n['fills'])}" + _small(f"{n['bought']:,.0f} / {n['sold']:,.0f} ct" if n["fills"] else "")) if r["member"] else "-",
            usd(n["rew"], False) if r["member"] else "-",
            _col(n["pnl"]) if r["member"] else "-",
            f'<b>{_col(n["net"])}</b>' if r["member"] else "-",
            _col(s["net"]) + _small(f"{r['nights']} night{'s' if r['nights'] != 1 else ''}"),
            _expected_cell(r["series"]),
            _floor_cell(r["floor"]) if r["floor"] else "-"])
    h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">By series</h3>')
    h.append(_tbl(["series", "markets quoted", "fills", "rewards (modelled)", "trading", "net",
                   "since start", "expected a night (go-live model)", "payout floor"], srows))
    if night["fills"]:
        h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Fills this night</h3>')
        h.append(_tbl(["ET", "series", "market", "", "price", "contracts", "now", "P&L now"],
                      [[datetime.fromtimestamp(f["ts"], ET).strftime("%H:%M"), _html.escape(series_of(f["ticker"])),
                        _html.escape(short(f["ticker"])),
                        f["side"] + (" (taker)" if f["taker"] else ""), f"{f['px']:.0f}c", f"{f['n']:,.0f}",
                        (f"{f['mark']:.0f}c" if f["mark"] is not None else "-") + ("" if f["basis"] == "last" else f" ({f['basis']})"),
                        _col(f["pnl"])] for f in night["fills"][:MAX_ROWS * 2]]))
    else:
        h.append('<p style="font-size:13px">No fills this night.</p>')
    if since["positions"]:
        ps = since["positions"]
        h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Open positions</h3>')
        h.append(_tbl(["series", "market", "net YES", "avg", "now", "MTM"],
                      [[_html.escape(series_of(p["ticker"])), _html.escape(short(p["ticker"])),
                        f"{p['net']:+,.0f}", f"{p['avg']:.1f}c",
                        (f"{p['mark']:.0f}c" if p["mark"] is not None else "-"), _col(p["mtm"])]
                       for p in ps[:MAX_ROWS]]))
        if len(ps) > MAX_ROWS:
            h.append(f'<p style="{S_SMALL}">{len(ps) - MAX_ROWS} more, MTM '
                     f'{usd(sum(p["mtm"] for p in ps[MAX_ROWS:]))}.</p>')
    if mv:
        h.append(f'<h3 style="margin:8px 0 2px 0;font-size:15px">Price moves of {MOVE_CENTS:.0f}c+ since quoting began (news)</h3>')
        h.append(_tbl(["series", "market", "first quoted mid", "now"],
                      [[_html.escape(series_of(m["ticker"])), _html.escape(short(m["ticker"])), f"{m['from']:.1f}c",
                        f"{m['to']:.0f}c" + (" (settled)" if m["basis"] == "settled" else "")] for m in mv[:15]]))
    g_rows = [[_html.escape(GUARD_WORDS.get(g, g)), _html.escape(r["series"]), f"{v['enters']}", f"{len(v['events'])}"]
              for r in rows for g, v in sorted(r["guards"].items()) if g != "astest_window"]
    h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Stops this night</h3>')
    h.append(_tbl(["guard", "series", "entries", "events"], g_rows) if g_rows else
             '<p style="font-size:13px">None fired (no fill pause, halt or sweep trip).</p>')
    h.append(f'<p style="margin:12px 0 0 0;color:{MUTED};font-size:12px">Rewards are the bot\'s '
             f'model (cycle log est_frac x pool, capped 900 s per gap), not Kalshi\'s credit; the payout '
             f'floor holds each market\'s accrual in a reward period against $1, and "on pace" assumes the '
             f'period\'s remaining nights accrue like tonight. Trading is every family fill marked to '
             f'Kalshi\'s last trade (100/0 once settled), before fees. "Expected" is what each series was '
             f'expected to make a night when it went live: for the series added 11/27, the night tape test, '
             f'which counted every market\'s night accrual as paid without holding the $1 floor against the '
             f'nights left in each period, so it overstates the series whose period ends after one night. '
             f'Members come from {fam["source"]}: a series added to IMM_ASTEST_MLB_SERIES shows up here on '
             f'its first night. Stop the whole family: create run-logs\\incentive-mm\\{STOP_FILE} (positions '
             f'ride). Drop one series: take it out of IMM_ASTEST_MLB_SERIES in run_incentive_mm.ps1, then '
             f'restart_imm.ps1 -Task.</p></div>')
    page = "\n".join(h)
    lines = [subject, f"Status {status}; night {nights_done}, {nights_left} left; members: {fam['source']}.",
             f"This night: {quoted_series} of {len(tonight)} series, {len(night['quoted'])} markets, "
             f"{nf_} fills, rewards {usd(night['rew'], False)} (modelled), trading {usd(night['pnl'])}, "
             f"net {usd(night['net'])}.",
             f"Since start: rewards {usd(since['rew'], False)}, trading {usd(since['pnl'])}, net {usd(since['net'])}."]
    for r in rows:
        n = r["night"]
        lines.append(f"  {r['series']}: " + (
            f"{len(n['quoted'])} of {r['candidates']} markets, {len(n['fills'])} fills, rewards "
            f"{usd(n['rew'], False)}, trading {usd(n['pnl'])}, net {usd(n['net'])}" if r["member"] else
            "not in the family tonight") + f"; since start {usd(r['since']['net'])}")
    return subject, page, "\n".join(lines) + "\n", ctx


def send(subject: str, page: str, text: str) -> bool:
    frm, pw = setting("ALERT_EMAIL_FROM"), setting("ALERT_EMAIL_PASSWORD")
    to = setting("IMM_REVIEW_TO") or setting("ALERT_EMAIL_TO") or frm
    if not frm or not pw:
        print("send: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
        return False
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(page, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = subject, frm, to
    rcpt = [r.strip() for r in to.split(",") if r.strip()]
    for i in range(SEND_TRIES):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(frm, pw)
                s.sendmail(frm, rcpt, msg.as_string())
            print(f"send: ok ({len(rcpt)} recipient(s))")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"send: try {i + 1} failed: {type(e).__name__}")
            if i + 1 < SEND_TRIES:
                time.sleep(SEND_WAIT)
    print("send: FAILED")
    return False


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Email the nights pilot's morning report (every series in the night family).")
    ap.add_argument("--day", help="the night's ET date (default: today in ET)")
    ap.add_argument("--dry", action="store_true", help="build and print, no email")
    ap.add_argument("--test", action="store_true", help="send now with [TEST] in the subject")
    ap.add_argument("--force", action="store_true", help="send even if already sent")
    ap.add_argument("--series", default="", help="(checks) these series only, comma-separated; no marker")
    ap.add_argument("--window", default="", help="(checks) HH:MM-HH:MM ET instead of the family's window")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = date.fromisoformat(a.day) if a.day else now.astimezone(ET).date()
    if not a.day and (day < FIRST_NIGHT or now > PILOT_END + timedelta(days=1, hours=12)):
        print(f"outside the pilot ({FIRST_NIGHT} to {PILOT_END:%Y-%m-%d}): nothing to send")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    series = [s.strip() for s in a.series.split(",") if s.strip()] or None
    marker = os.path.join(OUT_DIR, f"sent_{day.isoformat()}.marker")
    if os.path.exists(marker) and not (a.force or a.dry or a.test or series):
        print(f"already sent for {day} ({marker}); --force to resend")
        return 0
    t0 = time.time()
    subject, page, text, ctx = build(day, now, series=series,
                                     window=tuple(a.window.split("-")) if a.window else None)
    if a.test:
        subject = "[TEST] " + subject
    out = os.path.join(OUT_DIR, f"report_{day.isoformat()}{'_' + '_'.join(series) if series else ''}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    n, s = ctx["night"], ctx["since"]
    print(f"subject: {subject}".encode("ascii", "replace").decode("ascii"))
    print(f"night: {ctx['series_quoted']} of {len(ctx['members_tonight'])} series, {len(n['quoted'])} markets / "
          f"{len(n['events'])} events, {len(n['fills'])} fills, rew {n['rew']:.4f} pnl {n['pnl']:.4f}; since: "
          f"{len(s['quoted'])} markets, {len(s['fills'])} fills, rew {s['rew']:.4f} pnl {s['pnl']:.4f}; status "
          f"{ctx['status']}; members from {ctx['source']}; programs read {ctx['programs_read']}; "
          f"{time.time() - t0:.0f}s ({', '.join(f'{k} {v}s' for k, v in ctx['timing'].items())}); html {out}")
    if a.dry:
        return 0
    if not send(subject, page, text):
        return 1
    if not (a.test or series):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(now.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
