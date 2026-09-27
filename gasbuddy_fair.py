#!/usr/bin/env python3
"""GasBuddy fair values for incentive_mm's AAA state gas gate.

KXAAAGASD<ST> ("Will average regular gas prices for California be strictly
greater than $6.3950 on Sep 28, 2026 according to AAA?") settles on AAA's
state average for the event date. AAA posts it ~03:20 ET that morning and
Kalshi settles ~07:06 ET; the event itself trades the day BEFORE, 08:00-23:59
ET (open 12:00Z, close 03:59Z), strikes $0.005 apart. The state dailies were
blocked 2026-09-14 (-10c/ct fills: the informed flow reads intraday station
prices); Jack has GasBuddy's permission to use fuelinsights.gasbuddy.com
(2026-09-27), whose data is exactly that.

GasBuddy Fuel Insights (methodology page, 2026-09-27):
  Full Day Average  = average of the LAST price received at each station on
                      the day, published 03:00 ET the next morning (the same
                      hour AAA posts). "1 Day Ago" and the charts are these.
  Live Ticking Avg  = average of the last price received at each station over
                      the past 24 hours, refreshed every 5 minutes (observed).
Endpoints used (the site's own JSON):
  GET /api/HeatMap/GetMapData?...&timeType=1   live average, every state
  GET /api/LiveAvg/?id=<region>&countryID=...  update time, today's and
                                               yesterday's dates, and a
                                               state's 1 Day Ago average
  POST /api/HighChart/GetHighChartRecords/     a state's daily history (only
                                               for the Monday print's input)

Measured 2026-09-27: Kalshi's settled state values (26 states, Aug 24 - Sep
26) against GasBuddy's Full Day Averages. AAA's print for day D tracks
GasBuddy's day D-1 (correlation of the daily changes 0.94; D itself 0.68,
D-2 0.42):
    AAA(D) = AAA(D-1) + alpha_w + b1_w * (GB(D-1) - GB(D-2))
                                + b2_w * (GB(D-2) - GB(D-3)) + e
with a WEEKDAY structure (w = the print's weekday; residual sd, leave-one-
date-out where it could be computed):
    Mon print  b1 0.09  b2 0.47  e 0.41c  -- Sunday's move barely shows;
                                             the Monday print carries the
                                             rest of SATURDAY's move
    Tue print  UNMEASURED (GasBuddy's chart has no Monday values)
    Wed print  UNMEASURED (same reason)
    Thu print  b1 0.88           e 1.05c
    Fri print  b1 0.80           e 0.83c
    Sat print  b1 0.99           e 0.77c
    Sun print  b1 0.63           e 0.30c
Pooled: alpha +0.30c, b1 0.81, e 0.95c (AAA carried forward alone: 3.34c).
Per-state bias and residual scale are shrunk toward the pool (K=8 obs):
Florida's residual runs 1.7x the pool, New York's 0.57x.

While event D trades (ET day T = D-1) GB(D-1) is still forming, so the fair
uses the live average in its place and carries the part of the day not yet
seen as extra variance:
    mu    = AAA(T) + alpha_w + alpha_s + b1_w * (live_T - GB(T-1))
                                       + b2_w * (GB(T-1) - GB(T-2))
    sigma = GB_SIGMA_MULT * sqrt((e_w * scale_s)^2 + (b1_w * remain(t))^2)
remain(t) = sd of (Full Day Average - live at time t). NOT YET MEASURED:
the default is linear in the ET hour from the weekday's sd of daily GasBuddy
moves at midnight to 0 at 24:00. Every refresh appends the live averages to
GB_LIVE_LOG so it can be calibrated. When GasBuddy has no 1 Day Ago average
of the right date, yesterday's last live read (kept in the file as
"closes") stands in.

Writes GB_FAIR_FILE for incentive_mm: per EVENT ticker (KXAAAGASDCA-26SEP28)
mu and sigma in dollars plus the inputs. A state gets no entry (the gate
stands it aside) without a settled AAA anchor for today, a prior-day average,
or a live update newer than GB_MAX_AGE_MIN; Monday trading (the Tuesday
print) is skipped outright until it has been measured
(GB_SKIP_PRINT_WEEKDAYS). The anchor comes from Kalshi's markets endpoint,
read SIGNED when incentive_mm passes its reader and publicly otherwise.

DIESEL DAILY (Jack 2026-09-27: "ok do that for diesel daily"). KXDIESELD
settles on AAA's NATIONAL diesel average (posted ~03:20 ET on the print date,
settled by Kalshi ~09:00-09:40 ET); each event trades 08:00 ET the day
before until 01:59 ET on the print date. GasBuddy's diesel is fuel type 1:
the national live average comes from the map endpoint at country level
(subRegionType 6 -- LiveAvg ignores the fuel type), the Full Day Averages
from the chart. Measured on 39 print days (Aug 3 - Sep 27):
    AAA_d(D) - AAA_d(D-1) = 0.24c + 0.76 x (GB_d(D-1) - GB_d(D-2)) + e,
    sd(e) 1.69c  (carried forward: 2.9c)
AAA's diesel tracks GasBuddy's SAME-date diesel even closer (corr 0.98,
0.66c): GasBuddy's diesel runs about a day behind, and that figure is only
published after the close, so it cannot be used. No weekday structure is
fitted (too few days); the Tuesday print is skipped as for gas. One entry
per event, KXDIESELD-<print date>, same fields as a state's.

NATIONAL GAS DAILY (Jack 2026-09-27: "yes gate KXAAAGASD national on
gasbuddy"). KXAAAGASD settles on AAA's NATIONAL regular average -- the same
clock as the states: each event trades 08:00-23:59 ET the day before, AAA posts
~03:20 ET, Kalshi settles ~07:06 ET (Sundays ~09:10). GasBuddy's national live
average and 1 Day Ago Full Day Average ride in the LiveAvg read every refresh
already makes (region 500000, regular), so the entry costs no extra GasBuddy
call; the chart (fuel type 3, national) fills the day before yesterday for the
Monday print. Measured on 129 print days (Kalshi KXAAAGASD expiration values
against GasBuddy's national Full Day Averages, Mar 27 - Sep 26), the states'
weekday shape with tighter residuals (leave-one-out; carried forward in
brackets):
    Mon print  alpha +1.07c  b1 0.11  b2 0.48  e 0.41c  (0.67c)   n 26
    Tue print  UNMEASURED (3 days: GasBuddy's chart has no Mondays) -- skipped
    Wed print  UNMEASURED: b1 0.87, e 1.0c assumed
    Thu print  alpha -0.03c  b1 0.91           e 0.63c  (2.88c)   n 23
    Fri print  alpha -0.35c  b1 0.87           e 0.46c  (2.68c)   n 25
    Sat print  alpha +0.46c  b1 0.85           e 0.58c  (1.77c)   n 25
    Sun print  alpha +0.66c  b1 0.65           e 0.41c  (0.82c)   n 27
The unseen part of the trading day scales with GasBuddy's national daily move
(sd by trading weekday: Wed 3.1c, Thu 3.0c, Fri 2.0c, Sat 1.1c, Sun 0.7c) on
the same linear default as the states. One entry per event,
KXAAAGASD-<print date>, fuel "gas".

Standalone:  python gasbuddy_fair.py [--out path]
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows without tzdata
    import pytz
    ET = pytz.timezone("US/Eastern")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
GB_API = os.environ.get("IMM_GB_API", "https://fuelinsights.gasbuddy.com/api")
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_GB_KALSHI_MARKETS_URL",
    "https://api.elections.kalshi.com/trade-api/v2/markets")
SERIES_PREFIX = "KXAAAGASD"
GB_LIVE_LOG = os.environ.get(
    "IMM_GB_LIVE_LOG", os.path.join(STATUS_DIR, "gasbuddy_live.jsonl"))
LIVE_LOG_EVERY_SECS = 14 * 60        # ~every 15 min: ~200KB/day, all states
GB_MAX_AGE_MIN = _env_float("IMM_GB_MAX_AGE_MIN", 30)
GB_SIGMA_MULT = _env_float("IMM_GB_SIGMA_MULT", 1.0)
# print weekdays (Mon=0) never given an entry: Tuesday's print (traded on
# Monday) is unexplained by anything GasBuddy's chart carries
GB_SKIP_PRINT_WEEKDAYS = frozenset(
    int(x) for x in os.environ.get("IMM_GB_SKIP_PRINT_WEEKDAYS", "1").split(",")
    if x.strip().isdigit())
# Kalshi settles the day's print ~07:06 ET and the next event opens 08:00 ET:
# no anchor reads (and no entries) before this ET hour
GB_ANCHOR_FROM_HOUR = _env_float("IMM_GB_ANCHOR_FROM_HOUR", 7)
ABSENT_RECHECK_SECS = 6 * 3600       # a state with no Kalshi event, re-asked
CALL_PACE_SECS = 0.3                 # between per-state reads
HTTP_TIMEOUT = 30
US = 500000
_REGION_IDS: Dict[str, int] = {}     # state -> GasBuddy RegionID, learned

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY"}

# print weekday (Mon=0) -> (alpha, b1, b2, e) in dollars; see the docstring.
# Tue/Wed are the pooled fit with the residual inflated (unmeasured).
WEEKDAY_MODEL = {
    0: (0.0014, 0.09, 0.47, 0.0041),
    1: (0.0030, 0.81, 0.00, 0.0150),
    2: (0.0030, 0.81, 0.00, 0.0120),
    3: (-0.0004, 0.88, 0.00, 0.0105),
    4: (0.0039, 0.80, 0.00, 0.0083),
    5: (0.0017, 0.99, 0.00, 0.0077),
    6: (0.0013, 0.63, 0.00, 0.0030),
}
# sd of GasBuddy's own daily move, by the TRADING day's weekday (Mon=0), in
# dollars -- the scale of what is still unseen at midnight. Tuesday unmeasured.
DAY_MOVE_SD = {0: 0.0148, 1: 0.0320, 2: 0.0528, 3: 0.0308, 4: 0.0193,
               5: 0.0139, 6: 0.0123}
# state -> (bias, residual scale vs the pool); shrunk, K=8 (n in comments)
STATE_ADJ = {
    "AZ": (-0.0004, 0.79),   # n 12
    "CA": (+0.0049, 1.26),   # n 23
    "CO": (-0.0007, 1.02),   # n 7
    "CT": (-0.0010, 1.35),   # n 2
    "FL": (-0.0040, 1.70),   # n 23
    "GA": (-0.0005, 0.78),   # n 18
    "IL": (+0.0015, 0.95),   # n 23
    "IN": (-0.0014, 0.89),   # n 2
    "MA": (+0.0003, 0.80),   # n 12
    "MD": (+0.0010, 1.04),   # n 7
    "MI": (+0.0014, 1.14),   # n 12
    "MN": (-0.0008, 0.85),   # n 7
    "MO": (+0.0000, 0.90),   # n 2
    "NC": (-0.0008, 0.66),   # n 18
    "NJ": (-0.0001, 0.80),   # n 23
    "NV": (+0.0037, 1.14),   # n 2
    "NY": (-0.0008, 0.57),   # n 23
    "OH": (-0.0013, 0.80),   # n 18
    "OR": (+0.0008, 0.88),   # n 7
    "PA": (-0.0010, 0.64),   # n 18
    "SC": (-0.0021, 0.87),   # n 7
    "TN": (+0.0002, 0.73),   # n 12
    "TX": (+0.0006, 0.61),   # n 23
    "VA": (-0.0002, 0.67),   # n 12
    "WA": (+0.0009, 1.40),   # n 18
    "WI": (-0.0011, 0.90),   # n 2
}
NEW_STATE_SCALE = 1.25               # a state with no history yet

DIESEL_SERIES = "KXDIESELD"
DIESEL_MODEL = (0.0024, 0.76, 0.0169)     # alpha, b1, e in dollars (39 days)
DIESEL_DAY_MOVE_SD = 0.0357               # sd of GasBuddy's daily diesel move

NATGAS_SERIES = SERIES_PREFIX             # KXAAAGASD: AAA's national regular
# print weekday (Mon=0) -> (alpha, b1, b2, e) in dollars, national fit on 129
# print days (e = leave-one-out residual). Tue (skipped) and Wed unmeasured.
NATGAS_WEEKDAY_MODEL = {
    0: (0.0107, 0.11, 0.48, 0.0041),
    1: (0.0055, 0.63, 0.00, 0.0150),
    2: (0.0000, 0.87, 0.00, 0.0100),
    3: (-0.0003, 0.91, 0.00, 0.0063),
    4: (-0.0035, 0.87, 0.00, 0.0046),
    5: (0.0046, 0.85, 0.00, 0.0058),
    6: (0.0066, 0.65, 0.00, 0.0041),
}
# sd of GasBuddy's national daily move by TRADING weekday (Mon=0), dollars.
# Mon (3 obs) and Tue (none) unmeasured: the states' figures stand in.
NATGAS_DAY_MOVE_SD = {0: 0.0148, 1: 0.0320, 2: 0.0311, 3: 0.0303, 4: 0.0198,
                      5: 0.0111, 6: 0.0069}

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
           "OCT", "NOV", "DEC"]


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[gb-fair] {msg}", flush=True)


# the last signed-read failure, so each distinct one is logged once
_signed_err: Optional[str] = None


def _signed_read(get_json: Optional[Callable[[str, dict], dict]], path: str,
                 params: dict) -> Optional[dict]:
    """A SIGNED Kalshi read through `get_json(path, params)`, or None when
    there is no reader or it failed -- the caller then reads the public
    endpoint as before. incentive_mm passes the reader (2026-09-27): Kalshi
    throttles UNSIGNED /markets list reads from any IP, while the same read
    signed passed 20/20 in a paired probe. Each distinct failure is logged
    once (a refresh can read 51 anchors)."""
    global _signed_err
    if get_json is None:
        return None
    try:
        js = get_json(path, dict(params))
        if not isinstance(js, dict):
            raise ValueError(f"signed read returned {type(js).__name__}")
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:120]}"
        if err != _signed_err:
            _log(f"! signed Kalshi read failed ({err}); reading the public endpoint")
        _signed_err = err
        return None
    if _signed_err is not None:
        _log("signed Kalshi reads back")
        _signed_err = None
    return js


def event_ticker(series: str, d: date) -> str:
    """KXAAAGASDCA + 2026-09-28 -> KXAAAGASDCA-26SEP28."""
    return f"{series}-{d.year % 100:02d}{_MONTHS[d.month - 1]}{d.day:02d}"


def _get(url: str, params: dict, timeout: float = HTTP_TIMEOUT,
         retries: int = 3, backoff: float = 3.0):
    """GET JSON; a 429 / 5xx / network error is retried (3s, 12s, 27s)."""
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                time.sleep(backoff * (attempt + 1) ** 2)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1) ** 2)
    raise last if last else RuntimeError(f"GET {url} failed")


def fetch_live() -> Dict[str, float]:
    """State abbreviation -> live regular average (one call, every state).
    Also learns each state's GasBuddy RegionID."""
    d = _get(f"{GB_API}/HeatMap/GetMapData",
             {"regionID": US, "subRegionType": 4, "fuelType": 3, "timeType": 1,
              "masterRegionType": 6, "masterRegionIDForRanking": US,
              "calculationType": 1})
    out: Dict[str, float] = {}
    for rec in (d or {}).get("PriceRecords") or []:
        ab = STATES.get(str(rec.get("RegionName") or "").strip())
        if ab and isinstance(rec.get("RegionID"), int):
            _REGION_IDS[ab] = rec["RegionID"]
        try:
            p = float(rec.get("Price"))
        except (TypeError, ValueError):
            continue
        if ab and math.isfinite(p) and p > 0:
            out[ab] = p
    return out


def fetch_live_diesel() -> Optional[float]:
    """GasBuddy's live NATIONAL diesel average (map endpoint, country level)."""
    d = _get(f"{GB_API}/HeatMap/GetMapData",
             {"regionID": US, "subRegionType": 6, "fuelType": 1, "timeType": 1,
              "masterRegionType": 6, "masterRegionIDForRanking": US,
              "calculationType": 1})
    for rec in (d or {}).get("PriceRecords") or []:
        if str(rec.get("RegionName") or "").strip() in ("USA", "United States"):
            try:
                p = float(rec.get("Price"))
            except (TypeError, ValueError):
                return None
            return p if math.isfinite(p) and p > 0 else None
    return None


def fetch_diesel_history() -> Dict[str, float]:
    """National diesel Full Day Averages over the last month: date -> price."""
    r = requests.post(f"{GB_API}/HighChart/GetHighChartRecords/",
                      json={"regionID": [US], "fuelType": 1, "timeWindow": [4],
                            "frequency": 1}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out: Dict[str, float] = {}
    for block in r.json() or []:
        for row in block.get("USList") or []:
            try:
                d = datetime.strptime(str(row["datetime"]), "%m/%d/%Y").date()
                out[d.isoformat()] = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def fetch_natgas_history() -> Dict[str, float]:
    """National regular Full Day Averages over the last month: date -> price
    (only for the Monday print's day-before-yesterday)."""
    r = requests.post(f"{GB_API}/HighChart/GetHighChartRecords/",
                      json={"regionID": [US], "fuelType": 3, "timeWindow": [4],
                            "frequency": 1}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out: Dict[str, float] = {}
    for block in r.json() or []:
        for row in block.get("USList") or []:
            try:
                d = datetime.strptime(str(row["datetime"]), "%m/%d/%Y").date()
                out[d.isoformat()] = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def fetch_live_avg(region_id: int = US) -> dict:
    """A region's LiveAvg: when the live averages were last updated (the
    site's clock is ET), today's date, the live (ticking) regular average,
    and the 1 Day Ago average + date. For the country these are the
    NATIONAL regular figures the KXAAAGASD entry uses."""
    d = _get(f"{GB_API}/LiveAvg/", {"id": region_id, "countryID": US}) or {}
    avg = d.get("AvgPriceDict") or {}
    one = avg.get("OneDayAgo") or {}
    today = avg.get("Today") or {}

    def _px(v) -> Optional[float]:
        try:
            p = float(v)
        except (TypeError, ValueError):
            return None
        return p if math.isfinite(p) and p > 0 else None
    live = _px(d.get("LiveTickingAvg"))
    return {"updated": d.get("LastUpdatedTime"),
            "today": str(today.get("date") or "")[:10],
            "live_price": live if live is not None else _px(today.get("AvgPrice")),
            "prev": str(one.get("date") or "")[:10], "prev_price": _px(one.get("AvgPrice"))}


def fetch_prev(abbr: str) -> Tuple[str, Optional[float]]:
    """(date, price) of a state's 1 Day Ago Full Day Average."""
    rid = _REGION_IDS.get(abbr)
    if rid is None:
        return "", None
    m = fetch_live_avg(rid)
    return m["prev"], m["prev_price"]


def live_updated_utc(meta: dict) -> Optional[datetime]:
    """LastUpdatedTime ('2026-09-27T10:45:00.63', ET wall clock) as UTC."""
    raw = str((meta or {}).get("updated") or "")
    try:
        naive = datetime.fromisoformat(raw[:19])
    except ValueError:
        return None
    if hasattr(ET, "localize"):                  # pytz
        aware = ET.localize(naive)
    else:
        aware = naive.replace(tzinfo=ET)
    return aware.astimezone(timezone.utc)


def fetch_history(abbr: str) -> Dict[str, float]:
    """A state's Full Day Averages over the last month (chart timeWindow 4):
    ISO date -> price."""
    rid = _REGION_IDS.get(abbr)
    if rid is None:
        return {}
    r = requests.post(f"{GB_API}/HighChart/GetHighChartRecords/",
                      json={"regionID": [rid], "fuelType": 3,
                            "timeWindow": [4], "frequency": 1},
                      timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out: Dict[str, float] = {}
    for block in r.json() or []:
        for row in block.get("USList") or []:
            try:
                d = datetime.strptime(str(row["datetime"]), "%m/%d/%Y").date()
                out[d.isoformat()] = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def fetch_anchor(series: str, d: date,
                 get_json: Optional[Callable[[str, dict], dict]] = None
                 ) -> Tuple[str, Optional[float]]:
    """Kalshi's settled AAA value for <series>-<d>: ('ok', value), ('pending',
    None) while the event is not settled yet, ('absent', None) when Kalshi
    has no such event. Signed through `get_json` when given, else -- or when
    that read fails -- the public endpoint."""
    params = {"event_ticker": event_ticker(series, d), "limit": 1}
    js = _signed_read(get_json, "/markets", params)
    if js is None:
        js = _get(KALSHI_MARKETS_URL, params, retries=2, backoff=2.0)
    ms = (js or {}).get("markets") or []
    if not ms:
        return "absent", None
    try:
        v = float(ms[0].get("expiration_value"))
    except (TypeError, ValueError):
        return "pending", None
    return ("ok", v) if math.isfinite(v) and v > 0 else ("pending", None)


def remain_sd(trade_day_wd: int, et_hour: float) -> float:
    """Default (unmeasured) sd of the day's move still unseen at ET hour."""
    frac = min(1.0, max(0.0, (24.0 - et_hour) / 24.0))
    return DAY_MOVE_SD.get(trade_day_wd, 0.032) * frac


def state_fair(abbr: str, print_day: date, anchor: float, live: float,
               prev: float, prev2: Optional[float],
               now_et: datetime) -> Optional[dict]:
    """N(mu, sigma) in dollars for AAA's print on `print_day`, or None when a
    needed input is missing (b2 > 0 needs the day-before-yesterday)."""
    wd = print_day.weekday()
    alpha, b1, b2, e = WEEKDAY_MODEL[wd]
    bias, scale = STATE_ADJ.get(abbr, (0.0, NEW_STATE_SCALE))
    if b2 and prev2 is None:
        return None
    hour = now_et.hour + now_et.minute / 60.0
    rem = remain_sd(now_et.weekday(), hour)
    mu = anchor + alpha + bias + b1 * (live - prev) + (b2 * (prev - prev2) if b2 else 0.0)
    sigma = GB_SIGMA_MULT * math.sqrt((e * scale) ** 2 + (b1 * rem) ** 2)
    return {"mu": round(mu, 5), "sigma": round(sigma, 5), "weekday": wd,
            "alpha": round(alpha + bias, 5), "b1": b1, "b2": b2,
            "e": round(e * scale, 5), "remain": round(rem, 5)}


def diesel_fair(print_day: date, anchor: float, live: float, prev: float,
                now_et: datetime) -> dict:
    """N(mu, sigma) in dollars for AAA's national diesel print on print_day."""
    alpha, b1, e = DIESEL_MODEL
    hour = now_et.hour + now_et.minute / 60.0
    rem = DIESEL_DAY_MOVE_SD * min(1.0, max(0.0, (24.0 - hour) / 24.0))
    mu = anchor + alpha + b1 * (live - prev)
    sigma = GB_SIGMA_MULT * math.sqrt(e ** 2 + (b1 * rem) ** 2)
    return {"mu": round(mu, 5), "sigma": round(sigma, 5),
            "weekday": print_day.weekday(), "alpha": alpha, "b1": b1, "b2": 0.0,
            "e": e, "remain": round(rem, 5)}


def natgas_fair(print_day: date, anchor: float, live: float, prev: float,
                prev2: Optional[float], now_et: datetime) -> Optional[dict]:
    """N(mu, sigma) in dollars for AAA's national regular print on print_day,
    or None when the Monday print's day-before-yesterday is missing."""
    wd = print_day.weekday()
    alpha, b1, b2, e = NATGAS_WEEKDAY_MODEL[wd]
    if b2 and prev2 is None:
        return None
    hour = now_et.hour + now_et.minute / 60.0
    rem = (NATGAS_DAY_MOVE_SD.get(now_et.weekday(), 0.0277)
           * min(1.0, max(0.0, (24.0 - hour) / 24.0)))
    mu = anchor + alpha + b1 * (live - prev) + (b2 * (prev - prev2) if b2 else 0.0)
    sigma = GB_SIGMA_MULT * math.sqrt(e ** 2 + (b1 * rem) ** 2)
    return {"mu": round(mu, 5), "sigma": round(sigma, 5), "weekday": wd,
            "alpha": alpha, "b1": b1, "b2": b2, "e": e, "remain": round(rem, 5)}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_fair_file(path: str, now: Optional[datetime] = None,
                    live: Optional[Dict[str, float]] = None,
                    meta: Optional[dict] = None,
                    anchor_fn: Optional[Callable] = None,
                    prev_fn: Optional[Callable] = None,
                    history_fn: Optional[Callable] = None,
                    live_log: Optional[str] = None,
                    pace: float = CALL_PACE_SECS,
                    diesel_live: Optional[float] = None,
                    diesel_live_fn: Optional[Callable] = None,
                    diesel_hist_fn: Optional[Callable] = None,
                    get_json: Optional[Callable[[str, dict], dict]] = None,
                    natgas_hist_fn: Optional[Callable] = None
                    ) -> Tuple[int, int]:
    """Fetch (unless given), build and atomically write the fair file.
    Returns (events with an entry, series without one). Anchors, absent
    states, Full Day Averages ("finals") and each day's last live read
    ("closes") persist in the file between refreshes, so a steady refresh
    costs two GasBuddy calls; each state's anchor and 1 Day Ago average are
    read once a day. Raises on a GasBuddy live-read error (the caller logs
    and keeps the previous file, whose entries then age out of the bot's
    TTL). `get_json` is incentive_mm's signed Kalshi reader, used by the
    default anchor_fn (fetch_anchor)."""
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    today = now_et.date()
    yday = today - timedelta(days=1)
    d2 = (today - timedelta(days=2)).isoformat()
    print_day = today + timedelta(days=1)
    keep_from = (today - timedelta(days=10)).isoformat()
    anchor_fn = anchor_fn or (lambda s, d: fetch_anchor(s, d, get_json=get_json))
    prev_fn = prev_fn or fetch_prev
    history_fn = history_fn or fetch_history
    diesel_hist_fn = diesel_hist_fn or fetch_diesel_history
    natgas_hist_fn = natgas_hist_fn or fetch_natgas_history
    old = _read_json(path)
    if meta is None:
        meta = fetch_live_avg(US)
    if live is None:
        live = fetch_live()
    if diesel_live is None:
        try:
            diesel_live = (diesel_live_fn or fetch_live_diesel)()
        except Exception:
            diesel_live = None                # the diesel entry just goes missing
    upd = live_updated_utc(meta)
    live_fresh = (upd is not None
                  and (now - upd).total_seconds() <= GB_MAX_AGE_MIN * 60
                  and meta.get("today") == today.isoformat())

    def _days(block: str) -> Dict[str, Dict[str, float]]:
        return {d: dict(v) for d, v in (old.get(block) or {}).items()
                if isinstance(v, dict) and d >= keep_from}
    # Full Day Averages by date (the Monday print needs the day before
    # yesterday) and each day's LAST live read -- the stand-in for a Full
    # Day Average GasBuddy does not publish (its chart has no Mondays)
    finals, closes = _days("finals"), _days("closes")
    if live_fresh and live:
        closes[today.isoformat()] = dict(live)
    dfinals, dcloses = _days("diesel_finals"), _days("diesel_closes")
    if live_fresh and diesel_live:
        dcloses[today.isoformat()] = {"US": diesel_live}
    # the national regular average rides in the country LiveAvg read (meta)
    nfinals, ncloses = _days("natgas_finals"), _days("natgas_closes")
    ng_live = meta.get("live_price")
    if live_fresh and ng_live:
        ncloses[today.isoformat()] = {"US": ng_live}
    if meta.get("prev_price") and str(meta.get("prev") or "") >= keep_from:
        nfinals.setdefault(str(meta["prev"]), {})["US"] = meta["prev_price"]
    anchors: Dict[str, dict] = {s: a for s, a in (old.get("anchors") or {}).items()
                                if isinstance(a, dict)}
    absent: Dict[str, float] = {s: float(t) for s, t in (old.get("absent") or {}).items()
                                if isinstance(t, (int, float))}
    tried_prev: Dict[str, str] = {s: str(t) for s, t in (old.get("tried_prev") or {}).items()}
    entries: Dict[str, dict] = {}
    missing: List[str] = []
    failed = 0
    trading = now_et.hour + now_et.minute / 60.0 >= GB_ANCHOR_FROM_HOUR
    for abbr in sorted(set(STATES.values())) if trading else []:
        series = SERIES_PREFIX + abbr
        if absent.get(series, 0.0) > now.timestamp() - ABSENT_RECHECK_SECS:
            continue
        a = anchors.get(series) or {}
        if a.get("date") != today.isoformat():
            try:
                status, val = anchor_fn(series, today)
            except Exception:
                failed += 1
                missing.append(series)
                continue
            finally:
                time.sleep(pace)
            if status == "absent":
                absent[series] = now.timestamp()
                continue
            absent.pop(series, None)
            if status != "ok":
                missing.append(series)
                continue
            a = anchors[series] = {"date": today.isoformat(), "value": val}
        # yesterday's Full Day Average: once a day per state
        if abbr not in (finals.get(yday.isoformat()) or {}) \
                and tried_prev.get(abbr) != today.isoformat():
            try:
                pd, pp = prev_fn(abbr)
                tried_prev[abbr] = today.isoformat()
                if pd and pp is not None and pd >= keep_from:
                    finals.setdefault(pd, {})[abbr] = pp
            except Exception:
                pass
            finally:
                time.sleep(pace)
        if print_day.weekday() in GB_SKIP_PRINT_WEEKDAYS:
            missing.append(series)
            continue
        pv, src = (finals.get(yday.isoformat()) or {}).get(abbr), "gasbuddy"
        if pv is None:
            pv, src = (closes.get(yday.isoformat()) or {}).get(abbr), "close"
        lv = live.get(abbr)
        if not live_fresh or lv is None or pv is None:
            missing.append(series)
            continue
        pv2 = (finals.get(d2) or {}).get(abbr)
        if pv2 is None:
            pv2 = (closes.get(d2) or {}).get(abbr)
        if pv2 is None and WEEKDAY_MODEL[print_day.weekday()][2]:
            try:
                hist = history_fn(abbr)
            except Exception:
                hist = {}
            for d, p in hist.items():
                if keep_from <= d < today.isoformat():   # today's is still forming
                    finals.setdefault(d, {}).setdefault(abbr, p)
            pv2 = (finals.get(d2) or {}).get(abbr)
        f = state_fair(abbr, print_day, a["value"], lv, pv, pv2, now_et)
        if f is None:
            missing.append(series)
            continue
        entries[event_ticker(series, print_day)] = dict(
            f, series=series, anchor=a["value"], anchor_date=a["date"],
            live=lv, prev=pv, prev_source=src, prev2=pv2,
            print_day=print_day.isoformat(),
            live_updated=upd.isoformat() if upd else None,
            fetched_at=now.isoformat())
    # ---- the national diesel daily (KXDIESELD): the anchor settles ~09:00-
    # 09:40 ET, after the event opens, so it reads "pending" until then
    if trading and absent.get(DIESEL_SERIES, 0.0) <= now.timestamp() - ABSENT_RECHECK_SECS:
        series = DIESEL_SERIES
        a = anchors.get(series) or {}
        ok = a.get("date") == today.isoformat()
        if not ok:
            try:
                status, val = anchor_fn(series, today)
            except Exception:
                status, val = "error", None
                failed += 1
            finally:
                time.sleep(pace)
            if status == "absent":
                absent[series] = now.timestamp()
            elif status == "ok":
                absent.pop(series, None)
                a = anchors[series] = {"date": today.isoformat(), "value": val}
                ok = True
            else:                                 # pending / read error
                missing.append(series)
        if ok:
            if yday.isoformat() not in dfinals and tried_prev.get("diesel") != today.isoformat():
                try:
                    # the chart's point for TODAY is the day so far, not a
                    # Full Day Average: filed as one it made the next day skip
                    # this read and price off a partial (fixed 2026-09-27)
                    for d, p in diesel_hist_fn().items():
                        if keep_from <= d < today.isoformat():
                            dfinals.setdefault(d, {})["US"] = p
                    tried_prev["diesel"] = today.isoformat()
                except Exception:
                    pass
            pv, src = (dfinals.get(yday.isoformat()) or {}).get("US"), "gasbuddy"
            if pv is None:
                pv, src = (dcloses.get(yday.isoformat()) or {}).get("US"), "close"
            if (print_day.weekday() in GB_SKIP_PRINT_WEEKDAYS or not live_fresh
                    or diesel_live is None or pv is None):
                missing.append(series)
            else:
                f = diesel_fair(print_day, a["value"], diesel_live, pv, now_et)
                entries[event_ticker(series, print_day)] = dict(
                    f, series=series, fuel="diesel", anchor=a["value"],
                    anchor_date=a["date"], live=diesel_live, prev=pv,
                    prev_source=src, prev2=None, print_day=print_day.isoformat(),
                    live_updated=upd.isoformat() if upd else None,
                    fetched_at=now.isoformat())
    # ---- the national gas daily (KXAAAGASD): anchored on Kalshi's settled
    # national print (~07:06 ET, Sundays ~09:10), "pending" until then
    if trading and absent.get(NATGAS_SERIES, 0.0) <= now.timestamp() - ABSENT_RECHECK_SECS:
        series = NATGAS_SERIES
        a = anchors.get(series) or {}
        ok = a.get("date") == today.isoformat()
        if not ok:
            try:
                status, val = anchor_fn(series, today)
            except Exception:
                status, val = "error", None
                failed += 1
            finally:
                time.sleep(pace)
            if status == "absent":
                absent[series] = now.timestamp()
            elif status == "ok":
                absent.pop(series, None)
                a = anchors[series] = {"date": today.isoformat(), "value": val}
                ok = True
            else:                                 # pending / read error
                missing.append(series)
        if ok:
            pv, src = (nfinals.get(yday.isoformat()) or {}).get("US"), "gasbuddy"
            if pv is None:
                pv, src = (ncloses.get(yday.isoformat()) or {}).get("US"), "close"
            pv2 = (nfinals.get(d2) or {}).get("US")
            if pv2 is None:
                pv2 = (ncloses.get(d2) or {}).get("US")
            if (pv2 is None and NATGAS_WEEKDAY_MODEL[print_day.weekday()][2]
                    and tried_prev.get("natgas") != today.isoformat()):
                try:
                    for d, p in natgas_hist_fn().items():
                        if keep_from <= d < today.isoformat():
                            nfinals.setdefault(d, {}).setdefault("US", p)
                    tried_prev["natgas"] = today.isoformat()
                except Exception:
                    pass
                pv2 = (nfinals.get(d2) or {}).get("US")
            f = None
            if (print_day.weekday() not in GB_SKIP_PRINT_WEEKDAYS and live_fresh
                    and ng_live and pv is not None):
                f = natgas_fair(print_day, a["value"], ng_live, pv, pv2, now_et)
            if f is None:
                missing.append(series)
            else:
                entries[event_ticker(series, print_day)] = dict(
                    f, series=series, fuel="gas", anchor=a["value"],
                    anchor_date=a["date"], live=ng_live, prev=pv,
                    prev_source=src, prev2=pv2, print_day=print_day.isoformat(),
                    live_updated=upd.isoformat() if upd else None,
                    fetched_at=now.isoformat())
    if failed:
        _log(f"! Kalshi anchor read failed for {failed} series; retrying next refresh")
    # the intraday record the remain() curve gets calibrated from, one row
    # per ~15 minutes of GasBuddy updates
    lp = live_log or GB_LIVE_LOG
    logged = str(old.get("live_logged") or "")
    try:
        since = (upd - datetime.fromisoformat(logged)).total_seconds() if upd else None
    except ValueError:
        since = None
    if live and upd is not None and (since is None or since >= LIVE_LOG_EVERY_SECS):
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now.isoformat(), "updated": upd.isoformat(),
                                 "day": meta.get("today"), "live": live,
                                 "diesel_us": diesel_live, "gas_us": ng_live},
                                sort_keys=True) + "\n")
        logged = upd.isoformat()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "trade_day": today.isoformat(),
                   "print_day": print_day.isoformat(),
                   "live_updated": upd.isoformat() if upd else None,
                   "entries": entries, "missing": sorted(set(missing)),
                   "anchors": anchors, "absent": absent, "finals": finals,
                   "closes": closes, "tried_prev": tried_prev,
                   "diesel_finals": dfinals, "diesel_closes": dcloses,
                   "natgas_finals": nfinals, "natgas_closes": ncloses,
                   "live_logged": logged or None,
                   "model": {"sigma_mult": GB_SIGMA_MULT,
                             "max_age_min": GB_MAX_AGE_MIN,
                             "anchor_from_hour": GB_ANCHOR_FROM_HOUR,
                             "skip_print_weekdays": sorted(GB_SKIP_PRINT_WEEKDAYS)}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(entries), len(set(missing))


def p_above(strike: float, mu: float, sigma: float) -> float:
    """P(AAA > strike), all in dollars (the contract is strictly greater)."""
    if sigma <= 0:
        return 1.0 if mu > strike else 0.0
    return 0.5 * math.erfc((strike - mu) / (sigma * math.sqrt(2.0)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "gasbuddy_fair.json"))
    ap.add_argument("--live-log", default=None)
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out, live_log=args.live_log)
    _log(f"{ok} events with a fair entry, {miss} series without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
