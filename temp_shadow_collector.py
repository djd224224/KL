#!/usr/bin/env python3
"""Always-on recorder for the hourly-temp shadow test (TEMP_SHADOW.md, "Collector").

Records Kalshi's hourly Miami temperature markets (KXTEMPMIAH) around the
clock so temp_shadow_sim can replay quoting policies against them. READ-ONLY:
it only GETs REST endpoints and subscribes to WebSocket market-data channels;
it never places, amends or cancels an order. Never imports incentive_mm.py.

Threads (each catches everything and keeps going; log -> collector.log):
  ws    ONE WebSocket connection (kalshi_ws.WSConnection, stdlib RFC 6455),
        channels `orderbook_delta` + `trade` (one subscription per channel)
        for every open/upcoming KXTEMPMIAH market; markets are added as events
        appear and deleted ~10 min after close. Every message is written
        verbatim as {"rx", "m"} to raw/<UTC day>/ws_<HH>.jsonl, plus
        connect / disconnect / gap markers. Per-sid seq tracking (an `ok`
        answering update_subscription takes a seq too); a gap writes a `gap`
        marker and re-snapshots every book (update_subscription get_snapshot).
        Ping every 10 s; capped exponential reconnect backoff.
  wx    station feed GET /live_data/weather/miami?detailed=true&last_sec=420,
        every 1.0 s while second-of-minute in [13, 50] (spec: [18, 50]; see
        WX_LO for why 13), else every 5 s (hard cap 1.25 req/s). Unsigned; on 429 back off 2,4..60 s and after 3
        consecutive 429s sign requests for 30 min. A wx row is written only
        when a minute's row changes; a `poll` meta row per minute.
  rest  discovery every 30 s (/markets status=open + /markets
        min_close_ts=now, which also returns the upcoming event's
        "initialized" markets), incentive programs (every 60 s in hh:00-hh:06,
        else 10 min), settlement pulls (close + 7 min, retry every 2 min for
        up to 2 h until every market has a result) -> settled/<EVENT>.json.
  main  heartbeat (collector_status.json every 10 s), flush (<= 1 s), gzip
        of raw files whose UTC hour is >= 2 h old, HALT / SHADOW_UNTIL exit.

Exit codes: 0 --duration elapsed, 1 crash, 3 past SHADOW_UNTIL_UTC or HALT
(the launcher stops looping), 4 another live collector holds collector.lock.

CLI:  python temp_shadow_collector.py [--duration S] [--settle EVENT ...]
                                      [--settle-last] [--echo]

----------------------------------------------------------------------------
WebSocket message formats -- VERIFIED LIVE (read-only) 2026-10-15 00:20Z on
wss://api.elections.kalshi.com/trade-api/ws/v2, KXTEMPMIAH-26OCT1421, one
subscribe {"channels": ["orderbook_delta", "trade"]} -> TWO sids (one per
channel), each with its own seq starting at 1. Every message carries a
top-level `sending_ts_ms` (int ms).

subscribed (no seq):
  {"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1},
   "sending_ts_ms":1792023602825}

orderbook_snapshot (no ts field; levels are [price_dollars, size_fp] strings,
ascending price; YES bids and NO bids):
  {"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{
     "market_ticker":"KXTEMPMIAH-26OCT1421-T76.99",
     "market_id":"01a13cd2-d9d2-787b-a9f3-bc3594172397",
     "yes_dollars_fp":[["0.0100","810.00"],["0.0200","6.00"],...],
     "no_dollars_fp":[["0.0100","...."],...]},
   "sending_ts_ms":1792023602825}
  (a side with no orders is omitted from msg)

orderbook_delta (delta_fp is a signed size change at price_dollars on side
"yes"|"no"; ts is an ISO string, ts_ms int ms):
  {"type":"orderbook_delta","sid":1,"seq":11,"msg":{
     "market_ticker":"KXTEMPMIAH-26OCT1421-T84.99",
     "market_id":"01a13cd2-d9d3-7034-ba56-d2815ffca486",
     "price_dollars":"0.9800","delta_fp":"-20.00","side":"no",
     "ts":"2026-10-15T00:20:03.029Z","ts_ms":1792023603029},
   "sending_ts_ms":1792023603034}

trade (its own sid/seq; prices are DOLLAR STRINGS -- there is no integer
`yes_price`/`count` -- count_fp is a fixed-point string and can be
fractional; `ts` is epoch SECONDS (int), `ts_ms` epoch ms):
  {"type":"trade","sid":2,"seq":1,"msg":{
     "trade_id":"0725cf55-6f41-98ca-4c46-19a36af7d6b9",
     "market_ticker":"KXTEMPMIAH-26OCT1421-T81.99",
     "yes_price_dollars":"0.8700","no_price_dollars":"0.1300",
     "count_fp":"2.27","taker_side":"yes","taker_outcome_side":"yes",
     "taker_book_side":"bid","is_block_trade":false,
     "ts":1792023615,"ts_ms":1792023615799},
   "sending_ts_ms":1792023615805}
  Semantics checked against the book: taker_side "yes" consumed NO-side
  levels (NO bid 0.13 -> trade yes_price 0.87); taker_side "no" consumed
  YES-side levels at yes_price. ONE trade message per maker fill, at that
  maker's price: a sweep through two levels arrived as 3 trades @0.87 then
  5 @0.88 with identical ts_ms. The book deltas for the fills arrive BEFORE
  the trade messages (same ts_ms). taker_outcome_side == taker_side in every
  message seen; taker_book_side was "bid" for yes takers, "ask" for no.

REST trades (/markets/trades) carry the same fields but `created_time` (ISO)
instead of ts/ts_ms and no sid/seq.
----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import base64
import collections
import gzip
import http.client
import json
import math
import os
import random
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import traceback
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlencode

import temp_shadow_common as C
import kalshi_ws

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

EXIT_OK, EXIT_ERROR, EXIT_DONE, EXIT_LOCKED = 0, 1, 3, 4

API_HOST = "api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
WS_PATH = "/trade-api/ws/v2"
USER_AGENT = "kl-temp-shadow-collector/1.0"
FLEET_KEY_ID_DEFAULT = "c3204983-77fc-491b-99f7-136600698178"

WX_LAST_SEC = 420
# Fast window, second-of-minute (inclusive). TEMP_SHADOW.md says [18, 50];
# the low edge is 13 because station readings often land BEFORE :18: over
# 10,094 historical minutes (Oct 7-14 received_at_ms) the 4th station
# arrived at :13-:17 in 13% of minutes (p05 136 s, p50 146 s after the
# minute), and on 2026-10-15 00:40Z they landed at :14.6-:17.0 -- with an 18 s
# edge those readings were seen 1-3.5 s late (5 s cadence until :18). Costs
# ~5 extra polls a minute (~0.7 req/s total). TEMP_SHADOW_WX_LO / _HI override.
WX_LO = float(os.environ.get("TEMP_SHADOW_WX_LO", "13"))
WX_HI = float(os.environ.get("TEMP_SHADOW_WX_HI", "50"))
WX_FAST, WX_SLOW = 1.0, 5.0
WX_MIN_GAP = 0.8                    # hard cap 1.25 req/s
WX_TIMEOUT = 5.0

BACKOFF_429_BASE, BACKOFF_429_CAP = 2.0, 60.0
SIGN_AFTER_429 = 3
SIGN_SECS = 1800.0
# The fleet key the spec's sign switch uses is ALSO the IMM's key (imm_account:
# no IMM override set), so signed polling draws on the IMM's own budget.
# OFF by default since 2026-10-15 (coordinator): the shadow can live with a
# throttled poll; the IMM must not pay for it. TEMP_SHADOW_SIGN_ON_429=1 turns
# the switch back on.
SIGN_ON_429 = os.environ.get("TEMP_SHADOW_SIGN_ON_429", "0") != "0"
# Yield mode: after ANY wx 429 the fast cadence drops to 2 s for 2 min. The
# unsigned budget is shared with everything else on this IP (smoke
# 2026-10-15: zero 429s in steady state, clusters of 1-4/min while the IMM
# restarted), so a 429 means someone else needs it.
WX_YIELD_FAST = 2.0
WX_YIELD_SECS = 120.0

DISCOVERY_SECS = 30.0
PROGRAMS_FAST_SECS, PROGRAMS_SLOW_SECS = 60.0, 600.0
PROGRAMS_FAST_MINUTES = 6           # hh:00 .. hh:06 inclusive
PROGRAMS_FULL_SWEEP_SECS = 1800.0
# REST thread pacing: >= 2 s between requests, >= 4 s while the station
# feed's landing window ([WX_LO, WX_HI] s of the minute) is open, so the unsigned
# per-IP budget stays with the 1 req/s wx poller when it matters. (Smoke
# 2026-10-15: unsigned requests share one per-IP bucket of roughly 2 req/s
# sustained -- /markets/trades at 2 req/s beside the wx poller drew 429s, a
# wx poll among them, and even 1 req/s drew one; steady-state REST load is
# ~0.07 req/s, so only settlement / recovery pulls feel this pacing.)
REST_MIN_GAP = 2.0
REST_WINDOW_GAP = 4.0
REST_TIMEOUT = 20.0

SETTLE_DELAY = 420.0                # close + 7 min
SETTLE_RETRY = 120.0
SETTLE_WINDOW = 7200.0              # keep retrying up to 2 h
RECOVER_HOURS = 26                  # startup: events closed this recently lacking settled files
WS_KEEP_AFTER_CLOSE = 600.0         # delete a market from the ws subs ~10 min after close
EVENT_FORGET_SECS = 3 * 3600.0

HEARTBEAT_SECS = 10.0
STATUS_META_SECS = 60.0
FLUSH_SECS = 0.5
GZIP_AGE_HOURS = 2
LOG_MAX_BYTES = 20 * 1024 * 1024

LOG_FILE = os.path.join(C.DATA_ROOT, "collector.log")
_log_lock = threading.Lock()
_ECHO = False


def log(msg: str) -> None:
    line = (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            + f"Z [{threading.current_thread().name}] {msg}")
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            try:
                if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if _ECHO:
            try:
                print(line, flush=True)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------

def sec_of_minute(ts: float) -> float:
    return ts % 60.0


def next_poll_time(last_start: float, lo: float = WX_LO, hi: float = WX_HI,
                   fast: float = WX_FAST, slow: float = WX_SLOW,
                   min_gap: float = WX_MIN_GAP) -> float:
    """When the next station-feed poll starts, given the last one's start.
    1 s cadence while the next poll would land inside [lo, hi] s of the
    minute; otherwise 5 s, but never later than the next window start (so the
    landing window's first second is not skipped). Never closer than
    min_gap to the last start (the 1.25 req/s hard cap)."""
    c = last_start + fast
    if lo <= sec_of_minute(c) <= hi:
        t = c
    else:
        s = last_start + slow
        w = math.floor(last_start / 60.0) * 60.0 + lo
        if w <= last_start:
            w += 60.0
        t = min(s, w)
    return max(t, last_start + min_gap)


class RateState:
    """429 handling for one request stream: back off 2, 4, 8 .. 60 s on
    consecutive 429s; after `sign_after` consecutive 429s, sign requests for
    `sign_secs` (extended while 429s continue). Any success resets the
    consecutive count and the backoff; other errors leave both alone."""

    def __init__(self, base: float = BACKOFF_429_BASE, cap: float = BACKOFF_429_CAP,
                 sign_after: int = SIGN_AFTER_429, sign_secs: float = SIGN_SECS):
        self.base, self.cap = base, cap
        self.sign_after, self.sign_secs = sign_after, sign_secs
        self.consec = 0
        self.n429 = 0
        self.backoff_until = 0.0
        self.signed_until = 0.0
        self.sign_switches = 0

    def on_429(self, now: float) -> float:
        self.consec += 1
        self.n429 += 1
        wait = min(self.base * (2 ** (self.consec - 1)), self.cap)
        self.backoff_until = now + wait
        if self.consec >= self.sign_after:
            if self.signed_until <= now:
                self.sign_switches += 1
            self.signed_until = max(self.signed_until, now + self.sign_secs)
        return wait

    def on_ok(self, now: float) -> None:
        self.consec = 0
        self.backoff_until = 0.0

    def signed(self, now: float) -> bool:
        return now < self.signed_until


class WxDiffer:
    """Turns station-feed responses into wx rows, one per minute whose
    record changed versus the last one seen (station order ignored)."""

    def __init__(self, city: str, keep_ms: int = 3600 * 1000):
        self.city = city
        self.keep_ms = keep_ms
        self.last: Dict[int, str] = {}

    @staticmethod
    def canon(row: dict) -> str:
        r = dict(row)
        st = r.get("stations") or []
        r["stations"] = sorted(st, key=lambda s: str((s or {}).get("station_id")))
        return json.dumps(r, sort_keys=True, separators=(",", ":"), default=str)

    def changed(self, resp: dict, rx: float) -> List[dict]:
        rows = (resp or {}).get("timeseries") or []
        cv = (resp or {}).get("config_version")
        out: List[dict] = []
        newest = None
        for row in sorted((r for r in rows if isinstance(r, dict) and r.get("t") is not None),
                          key=lambda r: r["t"]):
            t = int(row["t"])
            newest = t if newest is None else max(newest, t)
            key = self.canon(row)
            if self.last.get(t) == key:
                continue
            self.last[t] = key
            rec = {"rx": rx, "city": self.city, "t": t, "status": row.get("status"),
                   "v": row.get("v"), "contributors": row.get("contributors"),
                   "stations": row.get("stations") or []}
            for k, v in row.items():
                if k not in rec:
                    rec[k] = v
            if cv is not None:
                rec["config_version"] = cv
            out.append(rec)
        if newest is not None:
            for t in [t for t in self.last if t < newest - self.keep_ms]:
                del self.last[t]
        return out


class SeqTracker:
    """Per-sid seq tracking (any message carrying both sid and seq, `ok`s
    included). check() -> None when in order, else (sid, expected, got)."""

    def __init__(self):
        self.last: Dict[int, int] = {}

    def reset(self) -> None:
        self.last.clear()

    def drop(self, sid) -> None:
        try:
            self.last.pop(int(sid), None)
        except (TypeError, ValueError):
            pass

    def check(self, msg: dict) -> Optional[Tuple[int, int, int]]:
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return None
        try:
            sid, seq = int(sid), int(seq)
        except (TypeError, ValueError):
            return None
        last = self.last.get(sid)
        self.last[sid] = seq
        if last is None:
            return None if seq <= 1 else (sid, 1, seq)
        if seq == last + 1:
            return None
        return (sid, last + 1, seq)


class PollStats:
    """Per-minute station-poll stats; add() returns the finished minute's
    `poll` meta data when a new minute starts."""

    def __init__(self):
        self.minute: Optional[int] = None
        self._reset()
        self.total = collections.Counter()

    def _reset(self) -> None:
        self.n = self.n429 = self.nerr = self.n_signed = 0
        self.lats: List[float] = []

    def add(self, start_ts: float, status: int, lat_ms: float, signed: bool) -> Optional[dict]:
        m = int(start_ts // 60)
        out = None
        if self.minute is not None and m != self.minute:
            out = self.flush()
        self.minute = m
        self.n += 1
        self.total["n"] += 1
        if status == 429:
            self.n429 += 1
            self.total["n429"] += 1
        elif status != 200:
            self.nerr += 1
            self.total["nerr"] += 1
        if signed:
            self.n_signed += 1
            self.total["n_signed"] += 1
        self.lats.append(float(lat_ms))
        return out

    def flush(self) -> Optional[dict]:
        if self.minute is None or self.n == 0:
            return None
        lats = sorted(self.lats)
        d = {"t": self.minute * 60, "minute": C.iso(self.minute * 60), "n": self.n,
             "n429": self.n429, "nerr": self.nerr,
             "lat_ms_p50": round(lats[len(lats) // 2], 1) if lats else None,
             "lat_ms_max": round(lats[-1], 1) if lats else None,
             "signed": self.n_signed > 0, "n_signed": self.n_signed}
        self._reset()
        return d


def rest_gap(now: float, base: float = REST_MIN_GAP, window: float = REST_WINDOW_GAP) -> float:
    """Minimum spacing for the REST thread's next request at `now`."""
    return max(base, window) if WX_LO <= sec_of_minute(now) <= WX_HI else base


def programs_interval(now: float) -> float:
    minute = int((now % 3600) // 60)
    return PROGRAMS_FAST_SECS if minute <= PROGRAMS_FAST_MINUTES else PROGRAMS_SLOW_SECS


def programs_due(now: float, last: Optional[float]) -> bool:
    return last is None or now - last >= programs_interval(now)


def all_results(markets: List[dict]) -> bool:
    return bool(markets) and all(str((m or {}).get("result") or "").strip() for m in markets)


def settle_action(now: float, close_ts: Optional[float], last_attempt: Optional[float],
                  done: bool) -> str:
    """'done' | 'wait' | 'attempt' for one event's settlement pull."""
    if done:
        return "done"
    if close_ts is None or now < close_ts + SETTLE_DELAY:
        return "wait"
    if last_attempt is not None and now - last_attempt < SETTLE_RETRY:
        return "wait"
    return "attempt"


def settle_deadline_passed(now: float, close_ts: float) -> bool:
    return now > close_ts + SETTLE_DELAY + SETTLE_WINDOW


def markets_signature(markets: List[dict]) -> str:
    rows = sorted((str(m.get("ticker")), str(m.get("status")), str(m.get("open_time")),
                   str(m.get("close_time")), str(m.get("result") or ""))
                  for m in markets if isinstance(m, dict))
    return json.dumps(rows, separators=(",", ":"))


_PROG_KEYS = ("id", "market_ticker", "start_date", "end_date", "period_reward",
              "target_size_fp", "discount_factor_bps", "paid_out", "incentive_type")


def programs_signature(progs: List[dict]) -> str:
    rows = sorted(tuple(str(p.get(k)) for k in _PROG_KEYS) for p in progs)
    return json.dumps(rows, separators=(",", ":"))


def event_ticker_for_hour(series: str, settle_ts: float) -> str:
    """Settlement epoch (on an ET hour) -> KXTEMPMIAH-26OCT1409."""
    d = C.et_dt(settle_ts)
    return f"{series}-{d.strftime('%y')}{d.strftime('%b').upper()}{d.strftime('%d%H')}"


def recent_event_tickers(series: str, now: float, hours: int) -> List[Tuple[str, float]]:
    """(event, settle_ts) for the ET hours that settled in the last `hours`
    hours, newest first (DST-safe: steps in absolute hours)."""
    top = math.floor(now / 3600.0) * 3600.0
    out = []
    for k in range(hours + 1):
        s = top - 3600.0 * k
        if s > now:
            continue
        out.append((event_ticker_for_hour(series, s), s))
    return out


# --------------------------------------------------------------------------
# Single-instance lock (PID inside)
# --------------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _PQLI = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
    _STILL_ACTIVE = 259

    def _open(pid: int):
        return _k32.OpenProcess(_PQLI, False, int(pid))

    def pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _open(pid)
        if not h:
            return ctypes.get_last_error() == 5      # access denied -> it exists
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)

    def pid_exe(pid: int) -> str:
        h = _open(pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            _k32.CloseHandle(h)

    def pid_create_time(pid: int) -> Optional[float]:
        h = _open(pid)
        if not h:
            return None
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                return None
            v = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
            return (v - 116444736000000000) / 1e7
        finally:
            _k32.CloseHandle(h)
else:                                           # pragma: no cover (dev boxes)
    def pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def pid_exe(pid: int) -> str:
        try:
            return os.path.basename(os.readlink(f"/proc/{pid}/exe")).lower()
        except OSError:
            return ""

    def pid_create_time(pid: int) -> Optional[float]:
        return None


def read_lock(path: str) -> Tuple[Optional[int], Optional[float]]:
    try:
        with open(path, encoding="utf-8") as f:
            parts = f.read().split()
    except OSError:
        return None, None
    pid = ts = None
    try:
        pid = int(parts[0])
    except (IndexError, ValueError):
        pass
    try:
        ts = float(parts[1])
    except (IndexError, ValueError):
        try:
            ts = os.path.getmtime(path)
        except OSError:
            ts = None
    return pid, ts


def lock_holder_live(pid: Optional[int], lock_ts: Optional[float],
                     alive: Callable[[int], bool] = None,
                     exe: Callable[[int], str] = None,
                     ctime: Callable[[int], Optional[float]] = None) -> bool:
    """True when `pid` (from the lock) is a live python process that started
    before the lock was written (a newer process = a reused PID)."""
    alive = alive or pid_alive
    exe = exe or pid_exe
    ctime = ctime or pid_create_time
    if not pid or not alive(pid):
        return False
    name = exe(pid)
    if name and "python" not in name:
        return False                 # alive but not python: stale lock
    if lock_ts is not None:
        ct = ctime(pid)
        if ct is not None and ct > lock_ts + 2.0:
            return False             # started after the lock was written: PID reuse
    return True                      # python (or unknown name: be conservative)


def acquire_lock(path: str, pid: Optional[int] = None, now: Optional[float] = None,
                 **probe) -> Tuple[bool, str]:
    pid = os.getpid() if pid is None else pid
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other, ts = read_lock(path)
            if other == pid:
                return True, "already held by this process"
            if lock_holder_live(other, ts, **probe):
                return False, f"collector.lock held by live python pid {other}"
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, f"cannot clear stale lock ({e})"
            continue
        try:
            os.write(fd, f"{pid}\n{time.time() if now is None else now:.3f}\n".encode("ascii"))
        finally:
            os.close(fd)
        return True, "acquired"
    return False, "lost the lock race"


def release_lock(path: str, pid: Optional[int] = None) -> bool:
    pid = os.getpid() if pid is None else pid
    other, _ = read_lock(path)
    if other != pid:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Raw files: writer + gzip rotation
# --------------------------------------------------------------------------

class RawWriter:
    """Appends JSON lines to raw/<UTC day>/<stream>_<HH>.jsonl, keeping one
    open handle per stream-hour; flush() at least once a second."""

    def __init__(self):
        self._lock = threading.Lock()
        self._files: Dict[str, list] = {}           # path -> [fh, last_write_mono]
        self._cur: Dict[str, Tuple[int, str]] = {}  # stream -> (hour, path)
        self.lines = collections.Counter()
        self.bytes = collections.Counter()

    def _path(self, stream: str, rx: float) -> str:
        h = int(rx // 3600)
        cur = self._cur.get(stream)
        if cur is not None and cur[0] == h:
            return cur[1]
        p = C.raw_file(stream, rx)
        if cur is None or h >= cur[0]:
            self._cur[stream] = (h, p)
        return p

    def write_line(self, stream: str, rx: float, line: str) -> None:
        with self._lock:
            p = self._path(stream, rx)
            ent = self._files.get(p)
            if ent is None:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                ent = self._files[p] = [open(p, "a", encoding="utf-8", newline="\n"), 0.0]
            ent[0].write(line + "\n")
            ent[1] = time.monotonic()
            self.lines[stream] += 1
            self.bytes[stream] += len(line) + 1

    def write(self, stream: str, rec: dict) -> None:
        self.write_line(stream, float(rec["rx"]),
                        json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str))

    def flush(self) -> None:
        with self._lock:
            for ent in self._files.values():
                try:
                    ent[0].flush()
                except Exception:
                    pass

    def close_path(self, path: str) -> None:
        with self._lock:
            ent = self._files.pop(path, None)
            for s, (h, p) in list(self._cur.items()):
                if p == path:
                    del self._cur[s]
        if ent is not None:
            try:
                ent[0].close()
            except Exception:
                pass

    def close_idle(self, idle_secs: float = 120.0) -> None:
        now = time.monotonic()
        with self._lock:
            current = {p for _h, p in self._cur.values()}
            stale = [p for p, ent in self._files.items()
                     if p not in current and now - ent[1] > idle_secs]
        for p in stale:
            self.close_path(p)

    def close_all(self) -> None:
        with self._lock:
            ents = list(self._files.values())
            self._files.clear()
            self._cur.clear()
        for ent in ents:
            try:
                ent[0].close()
            except Exception:
                pass


_RAW_NAME = re.compile(r"^(ws|wx|meta)_(\d\d)\.jsonl$")
_DAY_NAME = re.compile(r"^\d{4}-\d\d-\d\d$")


def raw_hour_of(path: str) -> Optional[float]:
    """raw/<YYYY-MM-DD>/<stream>_<HH>.jsonl -> epoch of that UTC hour."""
    m = _RAW_NAME.match(os.path.basename(path))
    day = os.path.basename(os.path.dirname(path))
    if not m or not _DAY_NAME.match(day):
        return None
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
        return d.timestamp() + 3600 * int(m.group(2))
    except ValueError:
        return None


def gzip_due_files(root: str, now: float, age_hours: int = GZIP_AGE_HOURS) -> List[str]:
    """Plain raw files whose UTC hour started >= age_hours whole hours
    before the current hour."""
    cutoff = math.floor(now / 3600.0) * 3600.0 - age_hours * 3600.0
    out = []
    raw = os.path.join(root, "raw")
    try:
        days = sorted(os.listdir(raw))
    except OSError:
        return out
    for day in days:
        d = os.path.join(raw, day)
        if not _DAY_NAME.match(day) or not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            h = raw_hour_of(p)
            if h is not None and h <= cutoff:
                out.append(p)
    return out


def gzip_file(path: str) -> str:
    """path -> path.gz (a late plain file next to an existing .gz becomes an
    extra gzip member: readers handle multi-member gzip). Removes path."""
    gz = path + ".gz"
    tmp = gz + ".tmp"
    with open(tmp, "wb") as out:
        if os.path.exists(gz):
            with open(gz, "rb") as old:
                shutil.copyfileobj(old, out)
        with open(path, "rb") as src, gzip.GzipFile(fileobj=out, mode="wb",
                                                    compresslevel=6, mtime=0) as z:
            shutil.copyfileobj(src, z, 1 << 20)
    os.replace(tmp, gz)
    os.remove(path)
    return gz


# --------------------------------------------------------------------------
# Auth (FLEET key; same signing as incentive_mm.py's WS feed and REST client)
# --------------------------------------------------------------------------

def fleet_key_id() -> str:
    try:
        import imm_account
        return imm_account.fleet_key_id()
    except Exception:
        return os.environ.get("KALSHI_API_KEY_ID", FLEET_KEY_ID_DEFAULT)


class Signer:
    """Loads the fleet private key lazily (KALSHI_PRIVATE_KEY b64 env, else
    KALSHI_PRIVATE_KEY_PATH, else Lisa_Kalshi.txt, else
    C:/Users/jackd/Downloads/Lisa_Kalshi.txt). Never logs key material."""

    def __init__(self):
        self.key_id = fleet_key_id()
        self._key = None
        self._lock = threading.Lock()
        self._last_try = 0.0
        self.error = ""

    def _load(self):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
        if pem_b64:
            try:
                pem = base64.b64decode(pem_b64)
            except Exception:
                pem = pem_b64.encode()
            return serialization.load_pem_private_key(pem, password=None, backend=default_backend())
        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
        if not os.path.exists(pem_path):
            local_default = r"C:/Users/jackd/Downloads/Lisa_Kalshi.txt"
            if os.path.exists(local_default):
                pem_path = local_default
            else:
                raise FileNotFoundError("no Kalshi private key (KALSHI_PRIVATE_KEY / "
                                        "KALSHI_PRIVATE_KEY_PATH / Lisa_Kalshi.txt)")
        with open(pem_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None,
                                                      backend=default_backend())

    def available(self) -> bool:
        with self._lock:
            if self._key is not None:
                return True
            if time.time() - self._last_try < 300 and self._last_try:
                return False
            self._last_try = time.time()
            try:
                self._key = self._load()
                self.error = ""
                return True
            except Exception as e:
                self.error = f"{type(e).__name__}: {str(e)[:120]}"
                log(f"! signing key unavailable: {self.error}")
                return False

    def sign(self, text: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = self._key.sign(text.encode("utf-8"),
                             padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                         salt_length=padding.PSS.DIGEST_LENGTH),
                             hashes.SHA256())
        return base64.b64encode(sig).decode("utf-8")

    def headers(self, method: str, full_path: str) -> Dict[str, str]:
        """full_path = the URL path without query, e.g. /trade-api/ws/v2."""
        if not self.available():
            raise RuntimeError(f"no signing key ({self.error})")
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method + full_path),
                "KALSHI-ACCESS-TIMESTAMP": ts}


# --------------------------------------------------------------------------
# HTTP (stdlib keep-alive client, GET only)
# --------------------------------------------------------------------------

class Resp:
    __slots__ = ("status", "body", "lat_ms", "error")

    def __init__(self, status: int, body, lat_ms: float, error: str = ""):
        self.status, self.body, self.lat_ms, self.error = status, body, lat_ms, error

    @property
    def ok(self) -> bool:
        return self.status == 200 and isinstance(self.body, (dict, list))


class Http:
    """One persistent HTTPS connection (keep-alive saves the ~1 s TLS
    handshake per call). GET only. Never raises: errors come back as
    Resp(status=0, error=...)."""

    _RETRYABLE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                  http.client.BadStatusLine, BrokenPipeError, ConnectionResetError,
                  ConnectionAbortedError)

    def __init__(self, signer: Optional[Signer] = None, timeout: float = REST_TIMEOUT,
                 host: str = API_HOST):
        self.host = host
        self.timeout = timeout
        self.signer = signer
        self.conn: Optional[http.client.HTTPSConnection] = None
        self.ctx = ssl.create_default_context()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def get(self, path: str, params: Optional[dict] = None, signed: bool = False) -> Resp:
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = API_PREFIX + path + (("?" + urlencode(q)) if q else "")
        headers = {"Accept": "application/json", "Accept-Encoding": "gzip",
                   "User-Agent": USER_AGENT}
        t0 = time.time()
        if signed:
            try:
                headers.update(self.signer.headers("GET", API_PREFIX + path))
            except Exception as e:
                return Resp(0, None, 0.0, f"sign: {e}")
        for attempt in (0, 1):
            t0 = time.time()
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPSConnection(self.host, timeout=self.timeout,
                                                            context=self.ctx)
                self.conn.request("GET", url, headers=headers)
                r = self.conn.getresponse()
                data = r.read()
                if (r.getheader("Content-Encoding") or "").lower() == "gzip":
                    data = gzip.decompress(data)
                if (r.getheader("Connection") or "").lower() == "close":
                    self.close()
                lat = (time.time() - t0) * 1000.0
                body = None
                if data:
                    try:
                        body = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        body = None
                err = "" if r.status == 200 else f"HTTP {r.status}: {data[:200]!r}"
                return Resp(r.status, body, lat, err)
            except self._RETRYABLE as e:
                self.close()
                if attempt == 0:
                    continue
                return Resp(0, None, (time.time() - t0) * 1000.0, f"{type(e).__name__}: {e}")
            except Exception as e:      # timeouts, DNS, TLS, ...
                self.close()
                return Resp(0, None, (time.time() - t0) * 1000.0,
                            f"{type(e).__name__}: {str(e)[:160]}")
        return Resp(0, None, 0.0, "unreachable")


class RestClient:
    """Http + RateState + a minimum gap between calls, for the REST thread."""

    def __init__(self, signer: Signer, stop_evt: threading.Event, min_gap: float = REST_MIN_GAP,
                 window_gap: float = REST_WINDOW_GAP):
        self.http = Http(signer)
        self.rate = RateState(sign_after=SIGN_AFTER_429 if SIGN_ON_429 else 10 ** 9)
        self.signer = signer
        self.stop_evt = stop_evt
        self.min_gap = min_gap
        self.window_gap = window_gap
        self._last = 0.0
        self.n = self.nerr = 0
        self.last_error = ""

    def get(self, path: str, params: Optional[dict] = None, tries: int = 3) -> Resp:
        resp = Resp(0, None, 0.0, "not attempted")
        for _ in range(max(1, tries)):
            while True:
                now = time.time()
                gap = rest_gap(now, self.min_gap, self.window_gap)
                wait = max(self._last + gap, self.rate.backoff_until) - now
                if wait <= 0:
                    break
                if self.stop_evt.wait(min(wait, 1.0)):
                    return Resp(0, None, 0.0, "stopping")
            self._last = time.time()
            signed = self.rate.signed(self._last) and self.signer.available()
            resp = self.http.get(path, params, signed=signed)
            self.n += 1
            if resp.status == 429:
                w = self.rate.on_429(time.time())
                log(f"rest 429 on {path} (consecutive {self.rate.consec}); backoff {w:.0f}s"
                    + ("; signing" if self.rate.signed(time.time()) else ""))
                continue
            if resp.ok:
                self.rate.on_ok(time.time())
                return resp
            self.nerr += 1
            self.last_error = f"{path}: {resp.error[:160]}"
            if resp.status in (400, 401, 403, 404):
                return resp
            if self.stop_evt.wait(1.0):
                break
        return resp

    def paginate(self, path: str, params: dict, key: str, cursor_key: str = "cursor",
                 max_pages: int = 50) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            r = self.get(path, p)
            if not r.ok:
                raise RuntimeError(f"GET {path} failed: {r.error[:200]}")
            batch = r.body.get(key) or []
            out.extend(batch)
            cursor = r.body.get(cursor_key)
            if not cursor or not batch:
                break
        return out


# --------------------------------------------------------------------------
# Event registry
# --------------------------------------------------------------------------

class EventInfo:
    def __init__(self, event_ticker: str):
        self.event_ticker = event_ticker
        self.markets: Dict[str, dict] = {}
        self.sig = ""
        self.open_ts: Optional[float] = None
        self.close_ts: Optional[float] = None
        pe = C.parse_event(event_ticker)
        if pe:
            self.close_ts = pe[1]
            self.open_ts = pe[1] - 3600.0
        self.first_seen = time.time()
        self.settle_done = False
        self.settle_last: Optional[float] = None
        self.settle_attempts = 0
        self.settle_force = False
        self.recovered = False        # from the startup scan, never seen open


class Registry:
    def __init__(self, writer: RawWriter):
        self.writer = writer
        self.lock = threading.RLock()
        self.events: Dict[str, EventInfo] = {}

    def get_or_add(self, evt: str) -> EventInfo:
        with self.lock:
            ev = self.events.get(evt)
            if ev is None:
                ev = self.events[evt] = EventInfo(evt)
            return ev

    def update(self, evt: str, markets: List[dict], rx: float) -> bool:
        """Merge REST market objects; write a `markets` meta row when the event
        is first seen with markets or a status/open/close/result changes."""
        markets = [m for m in markets if isinstance(m, dict) and m.get("ticker")]
        if not markets:
            return False
        with self.lock:
            ev = self.get_or_add(evt)
            for m in markets:
                ev.markets[m["ticker"]] = m
            sig = markets_signature(list(ev.markets.values()))
            changed = sig != ev.sig
            ev.sig = sig
            opens = [C.parse_iso(m.get("open_time") or "") for m in ev.markets.values()]
            closes = [C.parse_iso(m.get("close_time") or "") for m in ev.markets.values()]
            opens = [x for x in opens if x]
            closes = [x for x in closes if x]
            if opens:
                ev.open_ts = min(opens)
            if closes:
                ev.close_ts = max(closes)
            snapshot = sorted(ev.markets.values(), key=lambda m: m.get("ticker", ""))
        if changed:
            self.writer.write("meta", {"rx": rx, "kind": "markets",
                                       "data": {"event_ticker": evt, "markets": snapshot}})
        return changed

    def want_tickers(self, now: float) -> Set[str]:
        with self.lock:
            out: Set[str] = set()
            for ev in self.events.values():
                if ev.recovered and not ev.markets:
                    continue
                if ev.close_ts is not None and now >= ev.close_ts + WS_KEEP_AFTER_CLOSE:
                    continue
                out.update(ev.markets)
            return out

    def current_event(self, now: float) -> Optional[str]:
        with self.lock:
            cur = [ev for ev in self.events.values()
                   if ev.markets and ev.open_ts is not None and ev.close_ts is not None
                   and ev.open_ts <= now < ev.close_ts]
            return min(cur, key=lambda e: e.close_ts).event_ticker if cur else None

    def prune(self, now: float) -> None:
        with self.lock:
            for evt in [e for e, ev in self.events.items()
                        if ev.settle_done and ev.close_ts is not None
                        and now > ev.close_ts + EVENT_FORGET_SECS]:
                del self.events[evt]

    def summary(self, now: float) -> dict:
        with self.lock:
            evs = sorted(self.events.values(), key=lambda e: e.close_ts or 0)
            return {
                "tracked": [e.event_ticker for e in evs if e.markets],
                "pending_settlement": [e.event_ticker for e in evs
                                       if not e.settle_done and e.close_ts is not None
                                       and now >= e.close_ts],
            }


class Programs:
    def __init__(self, writer: RawWriter):
        self.writer = writer
        self.lock = threading.Lock()
        self.sig = None
        self.active: List[dict] = []
        self.seen: Dict[str, dict] = {}
        self.last_poll: Optional[float] = None
        self.last_ok: Optional[float] = None
        self.polls = 0
        self.pages = 0

    def update(self, progs: List[dict], rx: float) -> bool:
        sig = programs_signature(progs)
        with self.lock:
            for p in progs:
                if p.get("id"):
                    self.seen[str(p["id"])] = p
            cut = rx - 6 * 3600
            for pid in [k for k, p in self.seen.items()
                        if (C.parse_iso(p.get("end_date") or "") or rx) < cut]:
                del self.seen[pid]
            changed = sig != self.sig
            self.sig = sig
            self.active = list(progs)
        if changed:
            self.writer.write("meta", {"rx": rx, "kind": "programs",
                                       "data": {"programs": sorted(
                                           progs, key=lambda p: (p.get("market_ticker", ""),
                                                                 p.get("start_date", "")))}})
        return changed

    def for_event(self, evt: str) -> List[dict]:
        with self.lock:
            return sorted((p for p in self.seen.values()
                           if C.event_of(str(p.get("market_ticker", ""))) == evt),
                          key=lambda p: (p.get("market_ticker", ""), p.get("start_date", "")))


# --------------------------------------------------------------------------
# WebSocket recorder
# --------------------------------------------------------------------------

CHANNELS = ("orderbook_delta", "trade")


class _Chan:
    __slots__ = ("name", "sid", "markets", "pending", "pending_at", "pending_kind",
                 "pending_mk", "retry_after")

    def __init__(self, name: str):
        self.name = name
        self.sid: Optional[int] = None
        self.markets: Set[str] = set()
        self.pending: Optional[int] = None
        self.pending_at = 0.0
        self.pending_kind = ""
        self.pending_mk: Set[str] = set()
        self.retry_after = 0.0


class WSRecorder(threading.Thread):
    def __init__(self, writer: RawWriter, auth_headers: Callable[[], Dict[str, str]],
                 stop_evt: threading.Event, on_error: Callable[[str, str], None] = None,
                 url: str = C.WS_URL, ping_secs: float = 10.0, stale_secs: float = 30.0,
                 backoff_max: float = 60.0, timeout: float = 10.0,
                 connect_factory: Optional[Callable[[], kalshi_ws.WSConnection]] = None):
        super().__init__(daemon=True, name="ws")
        self.writer = writer
        self.auth_headers = auth_headers
        self.stop_evt = stop_evt
        self.on_error = on_error or (lambda where, err: None)
        self.url = url
        self.ping_secs = ping_secs
        self.stale_secs = stale_secs
        self.backoff_max = backoff_max
        self.timeout = timeout
        self.connect_factory = connect_factory
        self._lock = threading.Lock()
        self._want: Set[str] = set()
        self.conn: Optional[kalshi_ws.WSConnection] = None
        self.connected = False
        self.seq = SeqTracker()
        self.retired: Set[int] = set()
        self.chans: Dict[str, _Chan] = {ch: _Chan(ch) for ch in CHANNELS}
        self.next_id = 1
        self.last_msg_rx = 0.0
        self._resnap_pending = False
        self._resnap_next = 0.0
        self.by_type = collections.Counter()
        self.stats = {"connects": 0, "disconnects": 0, "gaps": 0, "resnapshots": 0,
                      "errors": 0, "msgs": 0, "last_error": "", "connected_since": None}

    # ---- caller API ----
    def set_markets(self, tickers: Iterable[str]) -> None:
        with self._lock:
            self._want = {t for t in tickers if t}

    def status(self) -> dict:
        now = time.time()
        return {"connected": self.connected,
                "last_msg_age_s": round(now - self.last_msg_rx, 1) if self.last_msg_rx else None,
                "msgs_by_type": dict(self.by_type),
                "subscribed": {ch: {"sid": c.sid, "markets": len(c.markets)}
                               for ch, c in self.chans.items()},
                **self.stats}

    def stop(self) -> None:
        self.stop_evt.set()
        c = self.conn
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    # ---- thread ----
    def run(self) -> None:
        backoff = 1.0
        while not self.stop_evt.is_set():
            started = time.monotonic()
            reason = "shutdown"
            try:
                self._session()
            except Exception as e:
                reason = f"{type(e).__name__}: {str(e)[:200]}"
                self.stats["errors"] += 1
                self.stats["last_error"] = reason
                if not self.stop_evt.is_set():
                    log(f"ws session ended: {reason}; reconnecting in ~{backoff:.0f}s")
                    self.on_error("ws", reason)
            finally:
                try:
                    self._teardown(reason)
                except Exception as e:
                    log(f"ws teardown error: {e}")
            if self.stop_evt.is_set():
                break
            if time.monotonic() - started > 300:
                backoff = 1.0
            if self.stop_evt.wait(backoff * (0.8 + 0.4 * random.random())):
                break
            backoff = min(backoff * 2.0, self.backoff_max)

    def _marker(self, kind: str, detail: str, **extra) -> None:
        rec = {"rx": time.time(), "marker": kind, "detail": detail}
        rec.update(extra)
        self.writer.write("ws", rec)

    def _teardown(self, reason: str) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if self.connected:
            self.connected = False
            self.stats["disconnects"] += 1
            self.stats["connected_since"] = None
            self._marker("disconnect", reason)
        self.chans = {ch: _Chan(ch) for ch in CHANNELS}
        self.seq.reset()
        self.retired = set()
        self._resnap_pending = False

    def _new_conn(self) -> kalshi_ws.WSConnection:
        if self.connect_factory is not None:
            return self.connect_factory()
        c = kalshi_ws.WSConnection(self.url, headers=self.auth_headers(),
                                   timeout=self.timeout)
        c.connect()
        return c

    def _send(self, conn, cmd: str, params: dict) -> int:
        cid = self.next_id
        self.next_id += 1
        conn.send_json({"id": cid, "cmd": cmd, "params": params})
        return cid

    def _session(self) -> None:
        conn = self._new_conn()
        self.conn = conn
        self.connected = True
        self.stats["connects"] += 1
        self.stats["connected_since"] = time.time()
        self.chans = {ch: _Chan(ch) for ch in CHANNELS}
        self.seq.reset()
        self.retired = set()
        self._marker("connect", self.url)
        log(f"ws connected {self.url}")
        conn.settimeout(1.0)
        last_ping = time.monotonic()
        next_apply = 0.0
        while not self.stop_evt.is_set():
            now = time.monotonic()
            if now >= next_apply:
                self._apply(conn)
                next_apply = now + 0.5
            if now - conn.last_rx > self.stale_secs:
                raise kalshi_ws.WSError(f"no frames for {now - conn.last_rx:.0f}s")
            if now - last_ping >= self.ping_secs:
                conn.ping(b"ts")
                last_ping = now
            try:
                op, payload = conn.recv_message()
            except (socket.timeout, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                # SSLWantRead: a TLS record with no app data (e.g. a TLS 1.3
                # session ticket) at the 1 s timeout -- seen live 10/15; the
                # frame buffer is untouched (kalshi_ws consumes only whole
                # frames), so it is a timeout, not a dead connection
                continue
            rx = time.time()
            if op != kalshi_ws.OP_TEXT:
                continue
            self.on_message(rx, payload, conn)

    def on_message(self, rx: float, payload: bytes, conn) -> None:
        text = payload.decode("utf-8", "replace")
        try:
            msg = json.loads(text)
        except ValueError:
            self.stats["errors"] += 1
            self.on_error("ws", f"non-JSON frame: {text[:120]!r}")
            return
        if "\n" in text or "\r" in text:
            line = json.dumps({"rx": rx, "m": msg}, separators=(",", ":"), ensure_ascii=False)
        else:
            line = '{"rx":%.6f,"m":%s}' % (rx, text)
        self.writer.write_line("ws", rx, line)
        self.stats["msgs"] += 1
        self.last_msg_rx = rx
        if isinstance(msg, dict):
            self.handle(msg, conn)

    def _chan_of_sid(self, sid) -> Optional[_Chan]:
        for c in self.chans.values():
            if c.sid is not None and sid is not None and c.sid == int(sid):
                return c
        return None

    def _unsubscribe(self, conn, c: _Chan) -> None:
        """Drop a channel's subscription; its sid is retired at once (late
        messages on it are recorded but not seq-checked) and the next _apply
        subscribes the wanted set afresh."""
        sid = c.sid
        cid = self._send(conn, "unsubscribe", {"sids": [sid]})
        if sid is not None:
            self.retired.add(int(sid))
            self.seq.drop(sid)
        c.sid, c.markets = None, set()
        c.pending, c.pending_at, c.pending_kind, c.pending_mk = \
            cid, time.monotonic(), "unsubscribe", set()

    def handle(self, msg: dict, conn) -> None:
        typ = msg.get("type")
        self.by_type[str(typ)] += 1
        sid = msg.get("sid")
        try:
            retired = sid is not None and int(sid) in self.retired
        except (TypeError, ValueError):
            retired = False
        if not retired:
            gap = self.seq.check(msg)
            if gap is not None:
                self._on_gap(gap, typ, conn)
        mid = msg.get("id")
        body = msg.get("msg") if isinstance(msg.get("msg"), dict) else {}
        if typ == "subscribed":
            c = self.chans.get(str(body.get("channel")))
            if c is not None and body.get("sid") is not None:
                c.sid = int(body["sid"])
                if c.pending == mid:
                    c.pending = None
        elif typ in ("ok", "unsubscribed"):
            for c in self.chans.values():
                if c.pending is not None and c.pending == mid:
                    c.pending = None
        elif typ == "error":
            code = body.get("code")
            text = f"code {code}: {str(body.get('msg'))[:160]}"
            self.stats["errors"] += 1
            self.stats["last_error"] = text
            log(f"ws error reply to cmd {mid}: {text}")
            self.on_error("ws_cmd", text)
            for c in self.chans.values():
                if c.pending is None or c.pending != mid:
                    continue
                kind = c.pending_kind
                c.pending = None
                if kind == "subscribe" or c.sid is None:
                    c.markets = set()
                    c.retry_after = time.monotonic() + 30.0
                elif kind in ("add", "delete"):
                    # resubscribe this channel from scratch. For the book
                    # channel that is a `gap` (books untrusted until the new
                    # subscription's snapshots); for trades a `resubscribe`
                    # marker (trades may be missing around it).
                    mk = "gap" if c.name == "orderbook_delta" else "resubscribe"
                    self._marker(mk, f"resubscribe {c.name} after {kind} error ({text})",
                                 resubscribe=True, channel=c.name, sid=c.sid)
                    self._unsubscribe(conn, c)
                    c.retry_after = time.monotonic() + 2.0

    def _on_gap(self, gap: Tuple[int, int, int], typ, conn) -> None:
        sid, exp, got = gap
        c = self._chan_of_sid(sid)
        ch = c.name if c is not None else str(typ)
        self.stats["gaps"] += 1
        self._marker("gap", f"sid {sid} ({ch}) expected seq {exp} got {got}",
                     sid=sid, channel=ch, expected=exp, got=got)
        log(f"ws gap: sid {sid} ({ch}) expected {exp} got {got}; re-snapshot")
        self._resnap_pending = True
        self._try_resnap(conn)

    def _try_resnap(self, conn) -> None:
        if not self._resnap_pending or time.monotonic() < self._resnap_next:
            return
        ob = self.chans.get("orderbook_delta")
        if ob is None or ob.sid is None or not ob.markets:
            self._resnap_pending = False
            return
        self._send(conn, "update_subscription",
                   {"sids": [ob.sid], "market_tickers": sorted(ob.markets),
                    "action": "get_snapshot"})
        self.stats["resnapshots"] += 1
        self._resnap_pending = False
        self._resnap_next = time.monotonic() + 5.0

    def _apply(self, conn) -> None:
        """Bring each channel's subscription in line with the wanted set."""
        with self._lock:
            want = set(self._want)
        now = time.monotonic()
        for ch in CHANNELS:
            c = self.chans[ch]
            if c.pending is not None:
                if now - c.pending_at <= 30.0:
                    continue
                log(f"ws: no reply to cmd {c.pending} ({c.pending_kind} {ch}) in 30s")
                if c.sid is None:
                    c.markets = set()
                c.pending = None
            if now < c.retry_after:
                continue
            if c.sid is None:
                if want:
                    cid = self._send(conn, "subscribe",
                                     {"channels": [ch], "market_tickers": sorted(want)})
                    c.pending, c.pending_at, c.pending_kind, c.pending_mk = \
                        cid, now, "subscribe", set(want)
                    c.markets = set(want)
                continue
            add, rem = want - c.markets, c.markets - want
            if rem and not add and rem == c.markets:
                self._unsubscribe(conn, c)
                continue
            if rem:
                cid = self._send(conn, "update_subscription",
                                 {"sids": [c.sid], "market_tickers": sorted(rem),
                                  "action": "delete_markets"})
                c.markets -= rem
                c.pending, c.pending_at, c.pending_kind, c.pending_mk = \
                    cid, now, "delete", set(rem)
                continue
            if add:
                cid = self._send(conn, "update_subscription",
                                 {"sids": [c.sid], "market_tickers": sorted(add),
                                  "action": "add_markets"})
                c.markets |= add
                c.pending, c.pending_at, c.pending_kind, c.pending_mk = \
                    cid, now, "add", set(add)
        self._try_resnap(conn)


# --------------------------------------------------------------------------
# Station-feed poller
# --------------------------------------------------------------------------

class WxPoller(threading.Thread):
    def __init__(self, coll: "Collector", city: str):
        super().__init__(daemon=True, name=f"wx-{city}")
        self.coll = coll
        self.city = city
        self.path = f"/live_data/weather/{city}"
        self.params = {"detailed": "true", "last_sec": WX_LAST_SEC}
        self.http = Http(coll.signer, timeout=WX_TIMEOUT)
        self.rate = RateState(sign_after=SIGN_AFTER_429 if SIGN_ON_429 else 10 ** 9)
        self.yield_until = 0.0
        self.differ = WxDiffer(city)
        self.stats = PollStats()
        self.rows = 0
        self.last_poll = 0.0
        self.last_ok = 0.0
        self.last_error = ""
        self._err_logged = 0.0

    def status(self) -> dict:
        now = time.time()
        return {"polls": self.stats.total["n"], "n429": self.stats.total["n429"],
                "nerr": self.stats.total["nerr"], "n_signed": self.stats.total["n_signed"],
                "rows_written": self.rows,
                "last_poll_age_s": round(now - self.last_poll, 1) if self.last_poll else None,
                "last_ok_age_s": round(now - self.last_ok, 1) if self.last_ok else None,
                "signed_now": self.rate.signed(now), "consecutive_429": self.rate.consec,
                "sign_switches": self.rate.sign_switches,
                "yielding": now < self.yield_until, "last_error": self.last_error}

    def run(self) -> None:
        last_start = 0.0
        stop = self.coll.stop_evt
        while not stop.is_set():
            try:
                now = time.time()
                fast = WX_YIELD_FAST if now < self.yield_until else WX_FAST
                due = next_poll_time(last_start, fast=fast) if last_start else now
                due = max(due, self.rate.backoff_until)
                if due > now:
                    stop.wait(min(due - now, 1.0))
                    continue
                last_start = time.time()
                self.poll_once(last_start)
            except Exception as e:
                self.coll.error(f"wx-{self.city}", e)
                stop.wait(1.0)
        d = self.stats.flush()
        if d:
            self.coll.writer.write("meta", {"rx": time.time(), "kind": "poll",
                                            "data": dict(d, city=self.city)})

    def poll_once(self, start: float) -> None:
        signed = self.rate.signed(start) and self.coll.signer.available()
        resp = self.http.get(self.path, self.params, signed=signed)
        rx = time.time()
        self.last_poll = rx
        d = self.stats.add(start, resp.status, resp.lat_ms, signed)
        if d:
            self.coll.writer.write("meta", {"rx": rx, "kind": "poll",
                                            "data": dict(d, city=self.city)})
        if resp.status == 429:
            wait = self.rate.on_429(rx)
            was_yielding = rx < self.yield_until
            self.yield_until = rx + WX_YIELD_SECS
            if not was_yielding or self.rate.consec == SIGN_AFTER_429 \
                    or self.rate.consec % 10 == 0:
                log(f"wx 429 (consecutive {self.rate.consec}); backoff {wait:.0f}s; "
                    f"fast cadence {WX_YIELD_FAST:.0f}s for {WX_YIELD_SECS:.0f}s"
                    + ("; signing for 30 min" if self.rate.signed(rx) else ""))
            return
        if resp.ok and isinstance(resp.body, dict):
            self.rate.on_ok(rx)
            self.last_ok = rx
            for rec in self.differ.changed(resp.body, rx):
                self.coll.writer.write("wx", rec)
                self.rows += 1
            return
        self.last_error = resp.error[:200] or f"HTTP {resp.status}"
        if rx - self._err_logged > 60:
            self._err_logged = rx
            log(f"wx poll error: {self.last_error}")
            self.coll.error(f"wx-{self.city}", self.last_error, log_it=False)


# --------------------------------------------------------------------------
# REST poller: discovery, programs, settlement pulls
# --------------------------------------------------------------------------

class RestPoller(threading.Thread):
    def __init__(self, coll: "Collector"):
        super().__init__(daemon=True, name="rest")
        self.coll = coll
        self.client = RestClient(coll.signer, coll.stop_evt)
        self.last_disc: Optional[float] = None
        self.last_disc_ok: Optional[float] = None
        self.last_prog: Optional[float] = None
        self.last_full: Optional[float] = None
        self.early_stop_ok = True
        self.settled_written = 0
        self.settled_partial = 0

    def status(self) -> dict:
        now = time.time()
        age = (lambda t: round(now - t, 1) if t else None)
        return {"requests": self.client.n, "n429": self.client.rate.n429,
                "nerr": self.client.nerr, "last_error": self.client.last_error,
                "signed_now": self.client.rate.signed(now),
                "last_discovery_age_s": age(self.last_disc_ok),
                "last_programs_age_s": age(self.coll.programs.last_ok),
                "programs_active": len(self.coll.programs.active),
                "programs_early_stop": self.early_stop_ok,
                "settled_written": self.settled_written,
                "settled_partial": self.settled_partial}

    def run(self) -> None:
        stop = self.coll.stop_evt
        while not stop.is_set():
            for name, fn in (("discovery", self.maybe_discover),
                             ("want", self.update_want),
                             ("programs", self.maybe_programs),
                             ("settlement", self.maybe_settle),
                             ("want", self.update_want)):
                if stop.is_set():
                    break
                try:
                    fn(time.time())
                except Exception as e:
                    if not stop.is_set():
                        self.coll.error(f"rest-{name}", e)
            stop.wait(1.0)

    def update_want(self, now: float) -> None:
        self.coll.ws.set_markets(self.coll.registry.want_tickers(now))
        self.coll.registry.prune(now)

    # ---- discovery ----
    def maybe_discover(self, now: float) -> None:
        if self.last_disc is not None and now - self.last_disc < DISCOVERY_SECS:
            return
        self.last_disc = now
        for series in C.SERIES:
            markets = self.client.paginate("/markets", {"series_ticker": series, "status": "open",
                                                        "limit": 1000}, "markets")
            # Upcoming events: their markets sit in status "initialized" for
            # the ~10 min between creation and open, which neither
            # status=open nor status=unopened returns, and /events?status=open
            # misses them too (verified live 10/15 00:53Z on 26OCT1422). An
            # unfiltered /markets with min_close_ts=now returns them.
            upcoming = self.client.paginate("/markets", {"series_ticker": series,
                                                         "min_close_ts": int(time.time()),
                                                         "limit": 1000}, "markets")
            rx = time.time()
            by_event: Dict[str, Dict[str, dict]] = {}
            for m in markets:
                evt = m.get("event_ticker") or C.event_of(m.get("ticker", ""))
                by_event.setdefault(evt, {})[m["ticker"]] = m
            for m in upcoming:
                evt = m.get("event_ticker") or C.event_of(m.get("ticker", ""))
                if m.get("ticker"):
                    by_event.setdefault(evt, {}).setdefault(m["ticker"], m)
            for evt, mk in sorted(by_event.items()):
                if not evt.startswith(series + "-"):
                    continue
                ev = self.coll.registry.get_or_add(evt)
                ev.recovered = False
                if self.coll.registry.update(evt, list(mk.values()), rx):
                    log(f"markets {evt}: {len(mk)} markets, statuses "
                        f"{sorted({str(m.get('status')) for m in mk.values()})}")
        self.last_disc_ok = time.time()

    # ---- programs ----
    def maybe_programs(self, now: float) -> None:
        if not programs_due(now, self.coll.programs.last_poll):
            return
        self.coll.programs.last_poll = now      # attempt time: a failure waits a full interval
        full = (self.last_full is None or now - self.last_full >= PROGRAMS_FULL_SWEEP_SECS
                or not self.early_stop_ok)
        with self.coll.registry.lock:
            opens = [ev.open_ts for ev in self.coll.registry.events.values()
                     if ev.markets and ev.open_ts and ev.close_ts and ev.close_ts > now - 3600]
        oldest_open = min(opens) if opens else None
        if oldest_open is None:
            full = True
        prefixes = tuple(s + "-" for s in C.SERIES)
        progs: List[dict] = []
        cursor = None
        early_page = None        # the page an early stop would have ended on
        last_hit = None
        pages = 0
        for page in range(30):
            params = {"status": "active", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = self.client.get("/incentive_programs", params)
            if not r.ok:
                raise RuntimeError(f"incentive_programs failed: {r.error[:200]}")
            pages += 1
            batch = r.body.get("incentive_programs") or []
            hits = [p for p in batch if str(p.get("market_ticker", "")).startswith(prefixes)]
            if hits:
                last_hit = page
            progs.extend(hits)
            cursor = r.body.get("next_cursor")
            if not cursor or not batch:
                break
            # Pages come newest-first (by start_date at page granularity):
            # once a page reaches back past the oldest tracked market's open
            # (- 1 h margin), later pages cannot hold a program for it. The
            # 30-min full sweep checks that and disables this if it is wrong.
            if oldest_open is not None and early_page is None:
                starts = [C.parse_iso(p.get("start_date") or "") for p in batch]
                starts = [s for s in starts if s]
                if starts and min(starts) < oldest_open - 3600:
                    early_page = page
                    if not full:
                        break
        rx = time.time()
        if full:
            self.last_full = rx
            if early_page is not None and last_hit is not None and last_hit > early_page:
                if self.early_stop_ok:
                    log(f"programs: full sweep found {prefixes} programs on page {last_hit} past "
                        f"the early-stop page {early_page}; early stop disabled")
                self.early_stop_ok = False
        self.coll.programs.polls += 1
        self.coll.programs.pages += pages
        self.coll.programs.last_ok = rx
        if self.coll.programs.update(progs, rx):
            log(f"programs: {len(progs)} active {'/'.join(C.SERIES)} programs "
                f"({pages} page{'s' if pages != 1 else ''}{', full' if full else ''})")

    # ---- settlement ----
    def maybe_settle(self, now: float) -> None:
        """At most ONE settlement attempt per tick, oldest event first, so a
        backlog (the startup recovery) never starves discovery."""
        with self.coll.registry.lock:
            evs = sorted(self.coll.registry.events.values(), key=lambda e: e.close_ts or 0)
        for ev in evs:
            if self.coll.stop_evt.is_set():
                return
            if ev.settle_done:
                continue
            if ev.settle_force:
                act = ("attempt" if ev.settle_last is None or now - ev.settle_last >= SETTLE_RETRY
                       else "wait")
            else:
                act = settle_action(now, ev.close_ts, ev.settle_last, ev.settle_done)
            if act != "attempt":
                continue
            if (ev.close_ts is not None and ev.settle_attempts >= 5
                    and now > ev.close_ts + SETTLE_DELAY + SETTLE_WINDOW + 3600):
                ev.settle_done = True
                log(f"settle {ev.event_ticker}: giving up after {ev.settle_attempts} failed attempts")
                self.coll.error(f"settle {ev.event_ticker}", "gave up (requests kept failing)",
                                log_it=False)
                continue
            ev.settle_last = time.time()
            ev.settle_attempts += 1
            try:
                self.settle_event(ev)
            except Exception as e:
                if not self.coll.stop_evt.is_set():
                    self.coll.error(f"settle {ev.event_ticker}", e)
            return

    def settle_event(self, ev: EventInfo) -> None:
        """One pull. Forcing (--settle) only skips the close + 7 min wait: the
        file is still written only once every market has a result, or
        incomplete (complete=false) once the 2 h retry window has passed."""
        evt = ev.event_ticker
        r = self.client.get("/markets", {"event_ticker": evt, "limit": 1000})
        if not r.ok:
            raise RuntimeError(f"markets for {evt}: {r.error[:200]}")
        markets = r.body.get("markets") or []
        now = time.time()
        expired = ev.close_ts is not None and settle_deadline_passed(now, ev.close_ts)
        if not markets:
            if expired or ev.recovered or ev.settle_force:
                ev.settle_done = True
                ev.settle_force = False
                log(f"settle {evt}: no markets (no such event); giving up")
            return
        self.coll.registry.update(evt, markets, now)
        complete = all_results(markets)
        if not complete and not expired:
            if ev.settle_attempts == 1 or ev.settle_attempts % 10 == 0:
                n = sum(1 for m in markets if str(m.get("result") or "").strip())
                log(f"settle {evt}: {n}/{len(markets)} results so far; retrying in "
                    f"{SETTLE_RETRY:.0f}s")
            return
        trades: Dict[str, List[dict]] = {}
        for m in sorted(markets, key=lambda m: m.get("ticker", "")):
            t = m["ticker"]
            vol = C.fp(m.get("volume_fp") if m.get("volume_fp") is not None else m.get("volume"))
            if vol <= 0:
                trades[t] = []
                continue
            trades[t] = self.client.paginate("/markets/trades", {"ticker": t, "limit": 1000},
                                             "trades", max_pages=200)
        live = self.client.get(f"/live_data/events/{evt}")
        details: dict = {}
        index_error = ""
        if live.ok:
            details = ((live.body.get("live_data") or {}).get("details") or {})
        else:
            index_error = live.error[:200]
            if not expired:
                raise RuntimeError(f"live_data/events/{evt}: {index_error}")
        obj = {"event_ticker": evt, "fetched_rx": time.time(), "complete": complete,
               "markets": sorted(markets, key=lambda m: m.get("ticker", "")),
               "trades": trades,
               "index": details.get("timeseries") or [],
               "index_details": {k: v for k, v in details.items() if k != "timeseries"},
               "programs": self.coll.programs.for_event(evt)}
        if index_error:
            obj["index_error"] = index_error
        C.write_json_atomic(C.settled_file(evt), obj)
        ev.settle_done = True
        ev.settle_force = False
        self.settled_written += 1
        if not complete:
            self.settled_partial += 1
        ntr = sum(len(v) for v in trades.values())
        log(f"settled/{evt}.json written: {len(markets)} markets, {ntr} trades, "
            f"{len(obj['index'])} index rows, {len(obj['programs'])} programs"
            + ("" if complete else " (INCOMPLETE: not every market has a result)"))


# --------------------------------------------------------------------------
# Collector (main thread)
# --------------------------------------------------------------------------

class Collector:
    def __init__(self, duration: Optional[float] = None, settle: Iterable[str] = (),
                 settle_last: bool = False):
        self.started = time.time()
        self.duration = duration
        self.stop_evt = threading.Event()
        self.writer = RawWriter()
        self.signer = Signer()
        self.registry = Registry(self.writer)
        self.programs = Programs(self.writer)
        self.errors = collections.deque(maxlen=20)
        self.n_errors = 0
        self._err_seen: Dict[str, float] = {}
        self._err_lock = threading.Lock()
        self.until = C.parse_iso(C.SHADOW_UNTIL_UTC) or float("inf")
        self.ws = WSRecorder(self.writer, lambda: self.signer.headers("GET", WS_PATH),
                             self.stop_evt, on_error=lambda w, e: self.error(w, e, log_it=False))
        cities = sorted({C.CITY_OF_SERIES.get(s, "") for s in C.SERIES} - {""})
        self.wx = [WxPoller(self, city) for city in cities]
        self.rest = RestPoller(self)
        self.exit_reason = ""
        self._recover(settle, settle_last)

    def _recover(self, settle: Iterable[str], settle_last: bool) -> None:
        now = time.time()
        start = C.parse_iso(C.SHADOW_START_UTC) or 0.0
        n = 0
        for series in C.SERIES:
            for evt, s_ts in recent_event_tickers(series, now, RECOVER_HOURS):
                if s_ts < start or os.path.exists(C.settled_file(evt)):
                    continue
                ev = self.registry.get_or_add(evt)
                ev.recovered = True
                n += 1
            if settle_last:
                evt, _s = recent_event_tickers(series, now, 1)[0]
                ev = self.registry.get_or_add(evt)
                ev.settle_force = True
                log(f"--settle-last: forcing a settlement pull for {evt}")
        for evt in settle:
            ev = self.registry.get_or_add(evt)
            ev.settle_force = True
            log(f"--settle: forcing a settlement pull for {evt}")
        if n:
            log(f"startup: {n} recently settled event(s) lack settled files; will pull them")

    def error(self, where: str, err, log_it: bool = True) -> None:
        if isinstance(err, BaseException):
            text = f"{type(err).__name__}: {str(err)[:300]}"
            tb = traceback.format_exc(limit=4)
        else:
            text, tb = str(err)[:300], ""
        now = time.time()
        with self._err_lock:
            self.n_errors += 1
            self.errors.append(f"{C.iso(now)} {where}: {text}")
            key = f"{where}|{text[:80]}"
            quiet = now - self._err_seen.get(key, 0.0) < 60.0
            self._err_seen[key] = now
            if len(self._err_seen) > 500:
                self._err_seen = {k: v for k, v in self._err_seen.items() if now - v < 60}
        if quiet:
            return
        if log_it:
            log(f"! {where}: {text}" + (f"\n{tb.rstrip()}" if tb and "NoneType: None" not in tb
                                         else ""))
        try:
            self.writer.write("meta", {"rx": now, "kind": "error",
                                       "data": {"where": where, "error": text}})
        except Exception:
            pass

    def status(self) -> dict:
        now = time.time()
        return {"pid": os.getpid(), "state": "running" if not self.exit_reason else "stopped",
                "exit_reason": self.exit_reason, "started": C.iso(self.started),
                "now": C.iso(now), "rx": now, "uptime_s": round(now - self.started, 1),
                "shadow_until": C.SHADOW_UNTIL_UTC,
                "connected": self.ws.connected,
                "ws": self.ws.status(),
                "wx": {w.city: w.status() for w in self.wx},
                "rest": self.rest.status(),
                "current_event": self.registry.current_event(now),
                "events": self.registry.summary(now),
                "raw_lines": dict(self.writer.lines),
                "raw_bytes": dict(self.writer.bytes),
                "errors": {"count": self.n_errors, "recent": list(self.errors)[-10:]},
                "signing_key": "ok" if self.signer._key is not None else
                               (self.signer.error or "not loaded")}

    def write_status(self, meta: bool) -> None:
        st = self.status()
        try:
            C.write_json_atomic(C.STATUS_FILE, st)
        except Exception as e:
            self.error("status", e)
        if meta:
            self.writer.write("meta", {"rx": st["rx"], "kind": "status", "data": st})

    def rotate(self) -> None:
        for p in gzip_due_files(C.DATA_ROOT, time.time()):
            try:
                self.writer.close_path(p)
                gzip_file(p)
                log(f"gzipped {os.path.relpath(p, C.DATA_ROOT)}")
            except Exception as e:
                self.error("gzip", e)

    def run(self) -> int:
        log(f"collector starting pid {os.getpid()}; data {C.DATA_ROOT}; series {C.SERIES}; "
            f"until {C.SHADOW_UNTIL_UTC}")
        self.rotate()
        self.ws.start()
        for w in self.wx:
            w.start()
        self.rest.start()
        code = EXIT_OK
        next_flush = next_hb = next_meta = 0.0
        next_rot = time.time() + 600
        last_hour = int(time.time() // 3600)
        try:
            while True:
                now = time.time()
                if os.path.exists(C.HALT_FILE):
                    self.exit_reason, code = "HALT file present", EXIT_DONE
                    break
                if now >= self.until:
                    self.exit_reason, code = f"past SHADOW_UNTIL_UTC {C.SHADOW_UNTIL_UTC}", EXIT_DONE
                    break
                if self.duration is not None and now - self.started >= self.duration:
                    self.exit_reason, code = f"--duration {self.duration:.0f}s elapsed", EXIT_OK
                    break
                dead = [t.name for t in [self.ws, self.rest] + self.wx if not t.is_alive()]
                if dead:
                    self.exit_reason, code = f"thread(s) died: {dead}", EXIT_ERROR
                    break
                if now >= next_flush:
                    self.writer.flush()
                    next_flush = now + FLUSH_SECS
                if now >= next_hb:
                    self.write_status(meta=now >= next_meta)
                    if now >= next_meta:
                        next_meta = now + STATUS_META_SECS
                    next_hb = now + HEARTBEAT_SECS
                hour = int(now // 3600)
                if hour != last_hour or now >= next_rot:
                    self.writer.close_idle()
                    self.rotate()
                    last_hour = hour
                    next_rot = now + 600
                self.stop_evt.wait(0.25)
        except KeyboardInterrupt:
            self.exit_reason, code = "KeyboardInterrupt", EXIT_OK
        except Exception as e:
            self.exit_reason, code = f"main loop: {type(e).__name__}: {e}", EXIT_ERROR
            log(traceback.format_exc())
        finally:
            self.shutdown()
        log(f"collector exiting ({self.exit_reason}) code {code}")
        return code

    def shutdown(self) -> None:
        self.stop_evt.set()
        self.ws.stop()
        for t in [self.ws, self.rest] + self.wx:
            if t.is_alive():
                t.join(timeout=10)
        try:
            self.write_status(meta=True)
        except Exception:
            pass
        self.writer.flush()
        self.writer.close_all()


def main(argv: Optional[List[str]] = None) -> int:
    global _ECHO
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--duration", type=float, default=None,
                    help="exit (code 0) after this many seconds (smoke tests)")
    ap.add_argument("--settle", action="append", default=[],
                    help="force a settlement pull for EVENT right away (repeatable)")
    ap.add_argument("--settle-last", action="store_true",
                    help="force a settlement pull for the most recently closed event")
    ap.add_argument("--echo", action="store_true", help="echo log lines to stdout")
    args = ap.parse_args(argv)
    _ECHO = args.echo or sys.stdout.isatty()
    if os.path.exists(C.HALT_FILE):
        log("HALT file present; not starting")
        return EXIT_DONE
    if time.time() >= (C.parse_iso(C.SHADOW_UNTIL_UTC) or float("inf")):
        log(f"past SHADOW_UNTIL_UTC {C.SHADOW_UNTIL_UTC}; not starting")
        return EXIT_DONE
    ok, why = acquire_lock(C.LOCK_FILE)
    if not ok:
        log(f"not starting: {why}")
        return EXIT_LOCKED
    try:
        coll = Collector(duration=args.duration, settle=args.settle,
                         settle_last=args.settle_last)
        return coll.run()
    except Exception:
        log("fatal: " + traceback.format_exc())
        return EXIT_ERROR
    finally:
        release_lock(C.LOCK_FILE)


if __name__ == "__main__":
    sys.exit(main())
