#!/usr/bin/env python3
"""Shared contract for the hourly-temp shadow test (TEMP_SHADOW.md).

Jack 2026-10-14: "build it and shadow test for a week, reporting results and
suggestion tweaks daily" -- a market-making bot for Kalshi's hourly Miami
temperature markets (KXTEMPMIAH), run in SHADOW: it records the live market
(books, trades, station readings, reward programs), replays candidate quoting
policies against the recording, and reports what each policy would have
earned in rewards and lost or won in fills. It never places an order.

Every temp_shadow_* module imports its paths, record shapes, time handling and
price conventions from here, so the collector that WRITES the recording and the
simulator that READS it cannot drift apart. Stdlib only.

Conventions (all modules):
  * Wall-clock times are epoch SECONDS as floats ("rx" = when OUR process
    received a message / response). Kalshi's millisecond fields keep their
    names (t, received_at_ms) and stay in ms.
  * Prices inside the simulator are integer CENTS in their own book's space:
    a YES bid at 45 is 45; a NO bid at 55 is 55 (= a YES ask at 45). Kalshi's
    dollar strings ("0.4500") convert with dollars_to_cents().
  * A strike ticker KXTEMPMIAH-26OCT1409-T81.99 is "above 81.99": YES iff the
    settlement index value > 81.99 (the index is published to 0.01 F).
  * The index with all five stations present = 32 + 0.36 * S (F), S = sum of
    the five stations' whole-degree-C readings (verified on 9,636 points).
"""
from __future__ import annotations

import datetime as _dt
import gzip
import io
import json
import math
import os
import re
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REPO = os.path.dirname(os.path.abspath(__file__))
# Data lives OUTSIDE the repo (the KL-data convention): tens of MB a day.
DATA_ROOT = os.environ.get(
    "TEMP_SHADOW_DATA", r"C:\Users\jackd\Documents\KL-data\temp-shadow")

SERIES = tuple(s for s in os.environ.get(
    "TEMP_SHADOW_SERIES", "KXTEMPMIAH").split(",") if s)
CITY_OF_SERIES = {"KXTEMPMIAH": "miami"}
STATIONS = {"miami": ("KMIA1M", "KOPF1M", "KFLL1M", "KFXE1M", "KPMP1M")}

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# The test window (the collector stops logging after it; the report task
# writes its week summary on the last day).
SHADOW_START_UTC = os.environ.get("TEMP_SHADOW_START", "2026-10-14T00:00:00Z")
SHADOW_UNTIL_UTC = os.environ.get("TEMP_SHADOW_UNTIL", "2026-10-23T12:00:00Z")


def data_path(*parts: str) -> str:
    p = os.path.join(DATA_ROOT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def raw_dir(utc_day: str) -> str:
    """raw/<YYYY-MM-DD>/ (UTC day of the rx stamp)."""
    d = os.path.join(DATA_ROOT, "raw", utc_day)
    os.makedirs(d, exist_ok=True)
    return d


def raw_file(stream: str, rx: float) -> str:
    """raw/<UTC day>/<stream>_<HH>.jsonl for the UTC hour of rx.

    stream in RAW_STREAMS. The collector appends plain .jsonl and gzips a
    file (-> .jsonl.gz, original removed) once its hour is >= 2h old; readers
    accept both."""
    t = _dt.datetime.fromtimestamp(rx, _dt.timezone.utc)
    return os.path.join(raw_dir(t.strftime("%Y-%m-%d")),
                        f"{stream}_{t.strftime('%H')}.jsonl")


RAW_STREAMS = ("ws", "wx", "meta")


def settled_file(event_ticker: str) -> str:
    return data_path("settled", f"{event_ticker}.json")


def sim_dir(et_day: str) -> str:
    d = os.path.join(DATA_ROOT, "sim", et_day)
    os.makedirs(d, exist_ok=True)
    return d


def report_dir() -> str:
    d = os.path.join(DATA_ROOT, "reports")
    os.makedirs(d, exist_ok=True)
    return d


MODEL_PARAMS_FILE = os.path.join(DATA_ROOT, "model", "model_params.json")
STATUS_FILE = os.path.join(DATA_ROOT, "collector_status.json")
HALT_FILE = os.path.join(DATA_ROOT, "HALT")
LOCK_FILE = os.path.join(DATA_ROOT, "collector.lock")

# --------------------------------------------------------------------------
# Record shapes (one JSON object per line)
# --------------------------------------------------------------------------
#
# ws stream   {"rx": float, "m": <raw Kalshi WS message, verbatim>}
#             {"rx": float, "marker": "connect"|"disconnect"|"gap"|"resubscribe",
#              "detail": str, ...}
#   After a "connect", "disconnect" or "gap" marker every book is UNTRUSTED
#   until that market's next orderbook_snapshot.
#
# wx stream   {"rx": float, "city": "miami", "t": <minute ms>, "status": str,
#              "v": float|null, "contributors": int|null,
#              "stations": [{"station_id","temp_f","code","source",
#                            "received_at_ms"}, ...]}
#   Written whenever a minute's record CHANGES versus the last one this
#   process saw (a new pending station, finalization, a revised value).
#   rx = when our poll response arrived = when the bot could first act.
#
# meta stream {"rx": float, "kind": KIND, "data": {...}}
#   KIND: "markets"   - data = {"event_ticker", "markets": [REST market objs]}
#                       (written when first seen and whenever open/close/
#                       status fields change)
#         "programs"  - data = {"programs": [REST incentive_program objs]}
#                       (KXTEMPMIAH ones only; written when the set changes)
#         "poll"      - data = per-minute weather-poll stats {n, n429, nerr,
#                       lat_ms_p50, lat_ms_max, signed}
#         "error"     - data = {"where", "error"}
#         "status"    - data = collector heartbeat (also STATUS_FILE)
#
# settled/<EVENT>.json
#   {"event_ticker", "fetched_rx", "markets": [REST market objs with result /
#    expiration_value], "trades": {ticker: [REST trade objs]},
#    "index": [live_data/events timeseries rows], "programs": [...]}

# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
UTC = _dt.timezone.utc


def utcnow() -> float:
    return _dt.datetime.now(UTC).timestamp()


def parse_iso(s: str) -> Optional[float]:
    """ISO-8601 (Kalshi 'Z' or offset, any fractional digits) -> epoch s."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?([+-]\d\d:\d\d)?$", s)
    if not m:
        return None
    frac = (m.group(2) or "")[:7]               # '.' + at most 6 digits
    s = m.group(1) + frac + (m.group(3) or "+00:00")
    try:
        return _dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def et_dt(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts, UTC).astimezone(ET)


def et_day(ts: float) -> str:
    return et_dt(ts).strftime("%Y-%m-%d")


def et_day_bounds(day: str) -> Tuple[float, float]:
    """[start, end) epoch seconds of an ET calendar day 'YYYY-MM-DD'."""
    d = _dt.date.fromisoformat(day)
    a = _dt.datetime(d.year, d.month, d.day, tzinfo=ET)
    b = a + _dt.timedelta(days=1)
    b = _dt.datetime(b.year, b.month, b.day, tzinfo=ET)   # DST-safe midnight
    return a.timestamp(), b.timestamp()


_MON = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
     "NOV", "DEC"), start=1)}
_EVENT_RE = re.compile(r"^(KX[A-Z]+)-(\d\d)([A-Z]{3})(\d\d)(\d\d)$")


def parse_event(event_ticker: str) -> Optional[Tuple[str, float]]:
    """KXTEMPMIAH-26OCT1409 -> ("KXTEMPMIAH", settlement epoch s = 09:00 ET
    on 2026-10-14). The market's close_time from the API is authoritative;
    this is the offline fallback and the cross-check. Ambiguous fall-back
    hour: the contract takes the EARLIER occurrence (fold=0)."""
    m = _EVENT_RE.match(event_ticker)
    if not m:
        return None
    series, yy, mon, dd, hh = m.groups()
    if mon not in _MON:
        return None
    local = _dt.datetime(2000 + int(yy), _MON[mon], int(dd), int(hh), tzinfo=ET)
    return series, local.timestamp()


def event_of(market_ticker: str) -> str:
    """KXTEMPMIAH-26OCT1409-T81.99 -> KXTEMPMIAH-26OCT1409"""
    return market_ticker.rsplit("-", 1)[0]


def strike_of(market_ticker: str) -> Optional[float]:
    """KXTEMPMIAH-26OCT1409-T81.99 -> 81.99 ('above' strike)."""
    tail = market_ticker.rsplit("-", 1)[-1]
    if not tail.startswith("T"):
        return None
    try:
        return float(tail[1:])
    except ValueError:
        return None


def yes_wins(strike: float, index_value: float) -> bool:
    """'Above <strike>' at the published 0.01 F precision, no re-rounding."""
    return round(index_value, 2) > round(strike, 2) + 1e-9


# --------------------------------------------------------------------------
# Prices / sizes
# --------------------------------------------------------------------------

def dollars_to_cents(x) -> int:
    """'0.4500' / 0.45 / '45' (already cents if > 1.5) -> 45."""
    v = float(x)
    if v > 1.5:          # already cents
        return int(round(v))
    return int(round(v * 100))


def fp(x) -> float:
    """Kalshi fixed-point size string ('300.00') -> 300.0."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def levels_from_fp(rows) -> Dict[int, float]:
    """[[price_dollars, size_fp], ...] -> {cents: size} (sizes summed)."""
    out: Dict[int, float] = {}
    for row in rows or []:
        try:
            px, q = row[0], row[1]
        except (TypeError, IndexError):
            continue
        c = dollars_to_cents(px)
        s = fp(q)
        if s > 1e-9:
            out[c] = out.get(c, 0.0) + s
    return out


# Index lattice (all five stations present, whole-C readings)
STEP_F = 0.36


def index_from_sum(s_sum: int) -> float:
    return round(32.0 + STEP_F * s_sum, 2)


def sum_threshold(strike: float) -> int:
    """Smallest five-station whole-C sum S whose index is > strike."""
    s = math.floor((strike + 0.01 - 32.0) / STEP_F - 1e-9)
    while index_from_sum(s) <= round(strike, 2):
        s += 1
    while index_from_sum(s - 1) > round(strike, 2):
        s -= 1
    return s


def f_to_c(f: float) -> float:
    return (f - 32.0) / 1.8


# --------------------------------------------------------------------------
# JSON-lines IO
# --------------------------------------------------------------------------

def append_jsonl(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")


def iter_jsonl(path: str) -> Iterator[dict]:
    """Rows of a .jsonl or .jsonl.gz file. A torn last line (crash mid-write)
    or a truncated gzip member ends the file quietly instead of raising."""
    if not os.path.exists(path):
        return
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (EOFError, OSError, gzip.BadGzipFile):
        return


def raw_files(stream: str, t0: float, t1: float) -> List[str]:
    """Existing raw files of `stream` whose UTC hour overlaps [t0, t1), in time
    order (plain or gzipped)."""
    out: List[str] = []
    h = math.floor(t0 / 3600) * 3600
    while h < t1:
        p = raw_file(stream, h)
        if os.path.exists(p + ".gz"):
            out.append(p + ".gz")
        if os.path.exists(p):
            out.append(p)
        h += 3600
    return out


def iter_raw(stream: str, t0: float, t1: float) -> Iterator[dict]:
    """Rows of a raw stream with t0 <= rx < t1, in file (= rx) order."""
    for p in raw_files(stream, t0, t1):
        for rec in iter_jsonl(p):
            rx = rec.get("rx")
            if rx is None or rx < t0 or rx >= t1:
                continue
            yield rec


def write_json_atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
