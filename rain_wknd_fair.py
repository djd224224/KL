#!/usr/bin/env python3
"""Weekend-rain fair values for incentive_mm's KXRAINWKND gate.

Jack 2026-10-11: "build the KXRT, CPI, KXRAINWKND models". KXRAINWKND-
<YYMONDD>-<CITY> ("If the total precipitation at CLIATL in Atlanta on any day
within August 22, 2026 through August 23, 2026 is strictly greater than 0
inches, then the market resolves to Yes") names The Weather Company's
weather.com/kalshi page as the source, which republishes the NWS climate
report (CLI) unchanged -- and whose terms forbid automated use. The NWS data
itself reproduces every settlement (research 2026-10-11: 156 of 156 settled
markets, Aug 22 - Oct 3): YES iff the CLI station shows >= 0.01 in (a trace
is 0) on the Saturday or the Sunday, each a climate day in LOCAL STANDARD
time (01:00-01:00 local during daylight time).

The bot quotes the family only before the weekend (out at 00:00 ET Saturday),
so this is a forecast model, no observation layer: the National Blend of
Models' station guidance (NBE, via the Iowa Environmental Mesonet's public
api/1/mos.json -- the IEM the rain monthlies already read), its 12-hour
probabilities of >= 0.01 in (p12, valid for the 12 hours ending at ftime) --
since 2026-10-30 a composite with the Blend's short-range NBS, hourly from
NOAA and 6-hourly from the IEM, wherever it reaches (THE COMPOSITE below).
Over the 12-hour periods overlapping the window [Sat 00:00, Mon 00:00) local
standard time, with overlap fractions f_i:
    L     = sum f_i * ln(1 - p_i)
    P_raw = max(max p_i over periods >= half inside, 1 - exp(W * L))
    P     = logistic(a + b * logit(P_raw)), (a, b) by lead time
W = 0.85 and the (a, b) table are the walk-free fit on 1,296 station-weekends
(54 weekends, 9/2025-10/2026, ACIS outcomes, which equal settlement here):
Brier 0.100 inside 24 hours to 0.143 at 4-5.5 days, against 0.21 for each
city's base rate. On the 7 settled Kalshi weekends at the same hours: model
0.089, the market mid 0.113 (the mid overprices YES in the 10-50c range).
A period set covering less than 3.9 of the window's 4 half-days, a run older
than NBE_MAX_AGE_HOURS, a lead beyond MAX_LEAD_HOURS (the fit stops at 5.5
days; Kalshi lists each weekend Monday 09:10Z, ~122-137 hours out), a
station neither in STATIONS nor auto-mapped (AUTO-MAPPED STATIONS), rules
naming any window but the ticker's Saturday-Sunday, or a weekend already
started gets no fair (the gate stands aside). An
older run is not a worse input than its lead says: the lead is measured
from the run, so a run the IEM is late with is read as the longer forecast
it is.

ONE-DAY MARKETS (Jack 2026-10-31: "KXRAINELECTION should be quoted", "in
same way as weekend rain family"): KXRAINELECTION-03NOV26-<CODE> ("If the
total precipitation at San Diego (KSAN) in San Diego in November 3, 2026 is
strictly greater than 0 inches", the same Weather Company source; the
ticker writes its day FIRST) is priced the same way over its one climate day,
[00:00, 24:00) local standard time: its two half-days, the same W and lead
table (fit on weekends, untested on single days), 1.9 of the 2 half-days
covered, out once the day starts. DAY_SERIES names them (the bot's
IMM_RAIN_DAY_SERIES).

AUTO DAY SERIES (Jack 2026-10-31: "make sure new markets with 'RAIN', 'TEMP',
etc are always considered for auto-quoting as part of these families"). Every
DAY_DISCOVER_SECS Kalshi's weather category is listed, and a series named for
rain that no family claims (not SERIES, KXRAIN, a DAY_SERIES, or what the
bot's `skip` owns) is read on its open markets. It joins the family when they
are this contract (classify_day_markets): one market per city (a ticker
ending in a city code), "total precipitation at <station> (Kxxx)" or "at
CLIxxx", "strictly greater than 0 inches", over a named day or a span of up to
MAX_SPAN_DAYS. Its window comes from the rules, not the ticker (whose date
Kalshi writes either way round). The file's "day_series" names it for the
bot; a series that is not the contract waits DAY_RETRY_SECS before it is read
again ("day_rejected", with why). A city whose station the writer can't read
stands aside alone, named in the row's "skipped" (2026-11-22: Honolulu's
"(PHNL)" had rejected all 51 Thanksgiving cities); the series is rejected only
when no city reads.

SNOW DAYS (Jack 2026-11-20: "KXSNOWTHANKSGIVING should quote"; 11/14: "rain/
snow markets generally should have fair value calculations and auto-quoted
when they come online"). KXSNOWTHANKSGIVING-26NOV26-<CODE> ("If the total new
snowfall in Detroit (KDTW) on November 26, 2026 is strictly greater than 0
inches, including a reported trace, then the market resolves to Yes"; the
same Weather Company source, the station's local reporting day) is this
family's contract on snow: a series named for snow that no bot family claims
joins the AUTO DAY SERIES when its markets are one per city, "total new
snowfall", "strictly greater than 0 inches" and a trace counting YES, over a
named day or a span of up to MAX_SPAN_DAYS. Its "day_series" row and its
event carry kind "snow", so the bot quotes it with the rain-day kit (out at
00:00 ET of the window's first day, the 10c fair band, fail closed). Its
fair is SNOW MODEL below, not the rain one: the NBE's 12-hour snow
probabilities (psn) and PoPs (p12), the coldest NBE temperature inside the
window and the station's trace-or-more base rate, fit on three winters of
ACIS outcomes.

Writes RAIN_WKND_FILE for incentive_mm: markets {ticker: {p, event,
fetched_at, ...}} (the loader's shape) plus per-event info.

Standalone:  python rain_wknd_fair.py [--out path]
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

import pytz
import requests

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
SERIES = "KXRAINWKND"
DAY_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_RAIN_DAY_SERIES", "KXRAINELECTION").split(",") if s.strip())
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_RAIN_WKND_MARKETS_URL", "https://api.elections.kalshi.com/trade-api/v2/markets")
NBE_URL = os.environ.get("IMM_RAIN_WKND_NBE_URL",
                         "https://mesonet.agron.iastate.edu/api/1/mos.json")
# the rules' CLI code -> (NBM station, local standard time offset from UTC,
# hours). Palm Beach's CLI is CLIDJT (the renamed airport); the NBM station
# list still calls it KPBI. College Station's rules say "at CLL". The second
# row of each offset is the 11 cities Kalshi had added by 26OCT24, which sat
# without a fair until 2026-10-29 (Jack: "Yes, map them"; each checked
# against its rules' CLI code, the NWS station's time zone and a current
# NBE run). The fit carries to them: on the 26 weekends of May-Oct 2026
# (00Z/12Z runs, ACIS outcomes) the 11 score Brier skill 0.41 inside 24h
# to 0.14 at 4-6 days over each city's base rate, the 24 above 0.42 to
# 0.18; mean p 0.55 vs 0.56 observed; per city they sit inside the 24's
# spread (ABQ and LEX near zero skill, as LAS, DJT, SFO and DEN are).
STATIONS: Dict[str, Tuple[str, int]] = {
    "ATL": ("KATL", 5), "BOS": ("KBOS", 5), "DCA": ("KDCA", 5), "EWR": ("KEWR", 5),
    "MIA": ("KMIA", 5), "NYC": ("KNYC", 5), "PHL": ("KPHL", 5), "TTN": ("KTTN", 5),
    "DJT": ("KPBI", 5),
    "CMH": ("KCMH", 5), "DTW": ("KDTW", 5), "IND": ("KIND", 5), "LEX": ("KLEX", 5),
    "PIT": ("KPIT", 5), "PVD": ("KPVD", 5), "TPA": ("KTPA", 5),
    "AUS": ("KAUS", 6), "ORD": ("KORD", 6), "CLL": ("KCLL", 6), "DFW": ("KDFW", 6),
    "HOU": ("KHOU", 6), "MSP": ("KMSP", 6), "MSY": ("KMSY", 6), "OKC": ("KOKC", 6),
    "SAT": ("KSAT", 6),
    "MKE": ("KMKE", 6), "SGF": ("KSGF", 6), "STL": ("KSTL", 6),
    "DEN": ("KDEN", 7), "PHX": ("KPHX", 7),
    "ABQ": ("KABQ", 7),
    "LAX": ("KLAX", 8), "LAS": ("KLAS", 8), "SEA": ("KSEA", 8), "SFO": ("KSFO", 8),
}
# AUTO-MAPPED STATIONS (Jack 2026-10-29: "also automatically map any future
# cities"). A code an open market's rules name that no STATIONS row maps is
# resolved and checked as the 11 above were: the NWS station record for K +
# code (its time zone gives the local standard offset), IEM's CLI for it (a
# report within AUTO_CLI_MAX_AGE_DAYS: the settlement source exists) and a
# current NBE run of MIN_PERIODS rows. A pass is kept in the file's
# "auto_stations"; a failure goes to "unmapped" with what failed and is tried
# again after AUTO_RETRY_SECS -- its markets get no fair meanwhile (the gate
# stands them aside), never a guessed station. The hand rows always win.
NWS_STATION_URL = "https://api.weather.gov/stations/{sid}"
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
AUTO_CLI_MAX_AGE_DAYS = 10
AUTO_RETRY_SECS = 6 * 3600
W = 0.85
# (lead upper bound, hours) -> logistic recalibration (a, b)
RECAL = ((24, -0.06, 1.12), (48, 0.01, 1.11), (72, 0.17, 1.12),
         (96, 0.24, 1.08), (1e9, 0.20, 1.10))
MIN_COVERAGE = 3.9           # of the window's 4 half-days (a one-day window: 1.9 of 2)
MIN_PERIODS = 10             # a run read back with fewer rows is a failed read
NBE_LAG_HOURS = 3.0          # a run is usable ~3h after its nominal time
NBE_MAX_AGE_HOURS = float(os.environ.get("IMM_RAIN_WKND_NBE_MAX_AGE_HOURS", "30"))
MAX_LEAD_HOURS = float(os.environ.get("IMM_RAIN_WKND_MAX_LEAD_HOURS", "144"))
# LONG LEADS (Jack 2026-11-14: "KXSNOWDAYS-01DEC26-01APR27,
# KXRAINTHANKSGIVING-26NOV26 (and rain/snow markets generally) should have
# fair value calculations and auto-quoted when they come online"). A day
# series can list weeks ahead -- Thanksgiving (11/26) listed 11/13, its
# program from 11/15 01:25Z, ~268 hours out -- while the fit stops at
# MAX_LEAD_HOURS, so it would have had no fair until 11/20, a day before its
# program ends. Past the fit the fair is the station's CLIMATOLOGY: the share
# of wet windows (any day >= 0.01 in) among the same window shifted up to
# CLIM_HALF_DAYS either way in each of the CLIM_YEARS years before it (ACIS
# daily pcpn at the NBM station; a window with a missing day is dropped,
# never read as dry). Over (MAX_LEAD_HOURS, CLIM_LEAD_HOURS] -- the NBE
# reaches 264 hours -- the NBM fair (the last recalibration row) blends in
# linearly: its weight (CLIM_LEAD_HOURS - lead) / (CLIM_LEAD_HOURS -
# MAX_LEAD_HOURS), 1 at the fit's edge. That blend is a hedge, NOT a fit:
# the NBE was never scored past 144 hours. A station without CLIM_MIN_WINDOWS
# windows gets no fair (the gate stands it aside). Each (station, window) is
# read once and kept in the file's "clim" for CLIM_TTL_DAYS, at most
# CLIM_MAX_READS new reads a pass. IMM_RAIN_WKND_CLIM=0: no fair past the fit
# (as before).
CLIM_ENABLE = os.environ.get("IMM_RAIN_WKND_CLIM", "1") == "1"
CLIM_LEAD_HOURS = float(os.environ.get("IMM_RAIN_WKND_CLIM_LEAD_HOURS", "264"))
CLIM_YEARS = 30
CLIM_HALF_DAYS = 7
CLIM_MIN_WINDOWS = 150
CLIM_TTL_DAYS = 7.0
CLIM_MAX_READS = 60
ACIS_URL = "https://data.rcc-acis.org/StnData"
# THE COMPOSITE (Jack 2026-10-30: "incorporate both hourly and 6 hours for a
# composite"). The NBE above is issued twice a day (00Z/12Z, at the IEM ~1-2h
# later), so a fair could lag the forecast by 14 hours. The Blend's
# short-range text guidance (NBS: 12-hour PoPs, ending 00Z/12Z, to ~72 hours
# out) is issued EVERY HOUR -- NOAA's blend_nbstx bulletins, on AWS
# (noaa-nbm-grib2-pds) ~57 min after the cycle -- and the IEM carries its
# 00/06/12/18Z cycles. Every 12-hour period the NBS covers takes
# NBS_W_HOURLY x the newest hourly cycle + (1 - NBS_W_HOURLY) x the newest
# 6-hourly one (either alone where only it reaches), the NBE the rest of the
# window, and the lead is measured from the freshest run used. A source that
# fails or ages out (NBS_HOURLY_MAX_AGE_HOURS, NBS_MAX_AGE_HOURS) drops out
# of the composite and the others carry on; nothing short-range at all is
# the NBE fair as before. The IEM's runs are read only once a newer one is
# due (NBE every 12h, NBS every 6h), so the 15-minute refresh costs the IEM
# fewer reads than the 30-minute one did.
# THE WEIGHT, backtested 10/30 on 2,730 station-times (the 35 stations x 26
# weekends of May-Oct 2026 x Thu 12Z / Fri 00Z / Fri 12Z; the hourly cycle an
# hour before each time, from AWS's archive; ACIS outcomes), Brier:
#   NBE alone (the fair to now) 0.1248, 6-hourly + NBE 0.1225, hourly + NBE
#   0.1212; composite at w 0.25 / 0.5 / 0.75 / 1: 0.1216 / 0.1212 / 0.1211 /
#   0.1212.
# w 0.75 beats the NBE by 0.0037 (95% weekend-bootstrap CI 0.0018-0.0061,
# 19 of 26 weekends) and the 6-hourly hybrid by 0.0015 (0.0001-0.0029); it
# ties the hourly alone (+0.0001, CI -0.0003..+0.0005) and keeps the
# 6-hourly run in the blend for the hours NOAA's feed is late or down.
NBS_ENABLE = os.environ.get("IMM_RAIN_WKND_NBS", "1") == "1"
NBS_HOURLY_ENABLE = os.environ.get("IMM_RAIN_WKND_NBS_HOURLY", "1") == "1"
NBS_HOURLY_URL = os.environ.get("IMM_RAIN_WKND_NBS_HOURLY_URL",
                                "https://noaa-nbm-grib2-pds.s3.amazonaws.com")
NBS_W_HOURLY = float(os.environ.get("IMM_RAIN_WKND_NBS_W_HOURLY", "0.75"))
NBS_LAG_HOURS = 2.0          # the IEM posts a 6-hourly cycle ~1.5-2h after it
NBS_HOURLY_LAG_HOURS = 1.0   # NOAA posts an hourly cycle ~57 min after it
NBS_MAX_AGE_HOURS = 12.0     # an older 6-hourly run leaves the composite
NBS_HOURLY_MAX_AGE_HOURS = 6.0
NBS_MIN_PERIODS = 4          # NBS's ~72 hours hold 5-6 twelve-hour PoPs
NBS_HOURLY_LOOKBACK = 4      # cycles back searched for the newest posted one
NBS_HOURLY_SIZES = 6         # file layouts (by size) the byte-range index keeps
NBS_HOURLY_MAX_BPS = float(os.environ.get("IMM_RAIN_WKND_NBS_MAX_BPS", "2000000"))
NBE_EVERY_HOURS = 12.0       # how often a new run is due (from its last one)
NBS_EVERY_HOURS = 6.0
EVENTS_TTL_SECS = 3600
SERIES_CATEGORY = "Climate and Weather"
AUTO_DAY_ENABLE = os.environ.get("IMM_RAIN_DAY_AUTO", "1") == "1"
DAY_DISCOVER_SECS = EVENTS_TTL_SECS
DAY_RETRY_SECS = 6 * 3600
# bump when classify_day_markets / rules_span learn new wording: every
# rejection an older one wrote is re-read at the next pass (2 = 2026-11-14,
# "on <day> is"; 3 = 2026-11-20, the snow contract: SNOW DAYS; 4 = 2026-11-22,
# a city with no readable station stands aside alone)
DAY_CLASSIFY_VERSION = 4
MAX_SPAN_DAYS = 3
HTTP_TIMEOUT = 30
USER_AGENT = "KL-IMM-rain-wknd-fair/1.0"
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_CODE_RE = re.compile(r"\bat (?:CLI)?([A-Z]{3})\b")
_SPAN_RE = re.compile(r"within ([A-Z][a-z]+ \d{1,2}, \d{4}) through ([A-Z][a-z]+ \d{1,2}, \d{4})")
_ICAO_RE = re.compile(r"\(K([A-Z]{3})\)")
# "in November 3, 2026 is" (Election Day) or "on November 26, 2026 is"
# (Thanksgiving, 2026-11-14: the "in"-only form rejected all 48 cities as
# "its rules name no station or no day")
_DAY_RE = re.compile(r"\b(?:in|on) ([A-Z][a-z]+ \d{1,2}, \d{4}) is\b")
_CITY_SEG_RE = re.compile(r"[A-Z]{2,5}")
_ZERO_RE = re.compile(r"total precipitation\b.*\bstrictly greater than 0 inches", re.I)
# SNOW DAYS (see the docstring): the snow contract, where a trace counts YES
SNOW_DAY_ENABLE = os.environ.get("IMM_SNOW_DAY_AUTO", "1") == "1"
_SNOW_ZERO_RE = re.compile(r"total new snowfall\b.*\bstrictly greater than 0 inches", re.I)
_SNOW_TRACE_RE = re.compile(r"including a (?:reported )?trace|\btrace\)? counts as "
                            r"(?:above|greater than|more than) 0", re.I)


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z [rain-wknd] {msg}",
          flush=True)


def station_code(rules: str) -> Optional[str]:
    """'... at CLIDJT in West Palm Beach ...' -> 'DJT'; 'at CLL' -> 'CLL';
    a one-day market's 'at West Palm Beach (KDJT) ...' -> 'DJT'."""
    m = _CODE_RE.search(rules or "") or _ICAO_RE.search(rules or "")
    return m.group(1) if m else None


def rules_span(rules: str) -> Optional[Tuple[date, date]]:
    """'... on any day within October 10, 2026 through October 11, 2026 ...'
    -> (date(2026, 10, 10), date(2026, 10, 11)); a one-day market's '... in
    November 3, 2026 is ...' (or '... on November 26, 2026 is ...') -> that
    day twice; else None."""
    m = _SPAN_RE.search(rules or "")
    days = m.groups() if m else None
    if days is None:
        m = _DAY_RE.search(rules or "")
        days = (m.group(1), m.group(1)) if m else None
    if days is None:
        return None
    try:
        a, b = (datetime.strptime(x, "%B %d, %Y").date() for x in days)
    except ValueError:
        return None
    return a, b


def event_saturday(event_ticker: str) -> Optional[date]:
    """'KXRAINWKND-26OCT10' -> date(2026, 10, 10), only if a Saturday."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) != 7:
        return None
    seg = parts[1]
    try:
        d = date(2000 + int(seg[:2]), _MON[seg[2:5]], int(seg[5:7]))
    except (KeyError, ValueError):
        return None
    return d if d.weekday() == 5 else None


def event_day(event_ticker: str) -> Optional[date]:
    """A one-day series' ticker day, written first: 'KXRAINELECTION-03NOV26'
    -> date(2026, 11, 3)."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) != 7:
        return None
    seg = parts[1]
    try:
        return date(2000 + int(seg[5:7]), _MON[seg[2:5]], int(seg[:2]))
    except (KeyError, ValueError):
        return None


def window(sat: date, lst_hours: int, days: int = 2) -> Tuple[datetime, datetime]:
    """[Sat 00:00, Mon 00:00) local standard time, in UTC; `days` from `sat`
    for a one-day market (its [00:00, 24:00))."""
    s = datetime(sat.year, sat.month, sat.day, tzinfo=timezone.utc) + timedelta(hours=lst_hours)
    return s, s + timedelta(hours=24 * days)


def _logit(p: float) -> float:
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def recalibrate(p: float, lead_h: float) -> float:
    for hi, a, b in RECAL:
        if lead_h < hi:
            return 1.0 / (1.0 + math.exp(-(a + b * _logit(p))))
    return p


def weekend_p(periods: List[Tuple[datetime, float]], ws: datetime, we: datetime
              ) -> Optional[Tuple[float, float]]:
    """(P_raw, coverage) from [(ftime, p12 in 0..1)]: the 12-hour periods
    ending at ftime, weighted by their overlap with [ws, we)."""
    pers = []
    for ft, p in periods:
        ov = (min(ft, we) - max(ft - timedelta(hours=12), ws)).total_seconds() / 43200.0
        if ov > 0:
            pers.append((ov, p))
    cov = sum(f for f, _ in pers)
    half_days = round((we - ws).total_seconds() / 43200.0)
    if not pers or cov < MIN_COVERAGE - (4 - half_days):
        return None
    lp = sum(f * math.log(max(1.0 - min(p, 0.995), 1e-6)) for f, p in pers)
    big = [p for f, p in pers if f >= 0.5]
    pmax = max(big) if big else max(p for _, p in pers)
    return max(pmax, 1.0 - math.exp(W * lp)), cov


def market_fair(periods: List[Tuple[datetime, float]], runtime: datetime,
                sat: date, lst_hours: int,
                lag_hours: float = NBE_LAG_HOURS, days: int = 2,
                max_lead: Optional[float] = None) -> Optional[dict]:
    """{'p', 'p_raw', 'lead_h', 'coverage'} for one city's weekend (or its
    one day: `days`), else None (a window that has started, a lead past the
    fit, or periods that do not cover it). The lead runs from `runtime` +
    `lag_hours` (the composite's: its freshest run's)."""
    ws, we = window(sat, lst_hours, days)
    lead = (ws - (runtime + timedelta(hours=lag_hours))).total_seconds() / 3600.0
    if lead < 0 or lead > (MAX_LEAD_HOURS if max_lead is None else max_lead):
        return None
    r = weekend_p(periods, ws, we)
    if r is None:
        return None
    p_raw, cov = r
    p = min(max(recalibrate(p_raw, lead), 0.01), 0.99)
    return {"p": round(p, 4), "p_raw": round(p_raw, 4), "lead_h": round(lead, 1),
            "coverage": round(cov, 2)}


def parse_pcpn(v) -> Optional[float]:
    """An ACIS daily value -> inches: 'T' (trace) 0.0, 'M' / 'S' / an
    accumulated 'A' total / anything else unreadable None (missing)."""
    s = str(v if v is not None else "").strip()
    if s in ("T", "t"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def climatology(daily: Dict[str, Optional[float]], first: date, days: int,
                years: int = CLIM_YEARS, half: int = CLIM_HALF_DAYS
                ) -> Optional[Tuple[float, int]]:
    """(share of wet windows, windows) over the `days`-long window from
    `first`, shifted -half..+half days in each of the `years` years before
    it (LONG LEADS); a window with a missing day is dropped. None under
    CLIM_MIN_WINDOWS windows."""
    wet = n = 0
    for back in range(1, years + 1):
        try:
            base = first.replace(year=first.year - back)
        except ValueError:                       # Feb 29 in a common year
            base = date(first.year - back, 2, 28)
        for o in range(-half, half + 1):
            d0 = base + timedelta(days=o)
            vals = [daily.get((d0 + timedelta(days=k)).isoformat()) for k in range(days)]
            if any(v is None for v in vals):
                continue
            n += 1
            wet += any(v >= 0.01 for v in vals)
    return (wet / n, n) if n >= CLIM_MIN_WINDOWS else None


def fetch_clim(sid: str, first: date, days: int) -> Tuple[float, int]:
    """(p, windows) from ACIS daily precipitation at `sid` (LONG LEADS)."""
    s = date(first.year - CLIM_YEARS - 1, 1, 1)
    e = min(date(first.year - 1, 12, 31) + timedelta(days=CLIM_HALF_DAYS + days + 1),
            datetime.now(timezone.utc).date() - timedelta(days=1))
    rsp = requests.post(ACIS_URL, json={"sid": sid, "sdate": s.isoformat(),
                                        "edate": e.isoformat(), "elems": [{"name": "pcpn"}]},
                        timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    rsp.raise_for_status()
    js = rsp.json()
    if js.get("error"):
        raise RuntimeError(f"ACIS {sid}: {str(js['error'])[:120]}")
    daily = {str(d): parse_pcpn(v) for d, v in js.get("data") or []}
    got = climatology(daily, first, days)
    if got is None:
        raise RuntimeError(f"ACIS {sid}: under {CLIM_MIN_WINDOWS} complete windows")
    return got


# ----------------------------------------------------------------------------
# SNOW MODEL (SNOW DAYS, 2026-11-20). Over the NBE's 12-hour periods
# overlapping the window, with overlap fractions f_i:
#     Ps   = 1 - exp(sum f_i ln(1 - psn_i))     (the snow probabilities)
#     Pp   = 1 - exp(sum f_i ln(1 - p12_i))     (the PoPs: any precipitation)
#     cold = clip((40 - Tmin) / 10, -2, 3)       (Tmin: the coldest tmp / txn
#                                                 of those periods, F)
#     clim = the station's share of days with new snow, a trace counting, in
#            the CLIM_YEARS years before, +-CLIM_HALF_DAYS days (ACIS)
#     early = 1 when the window starts in October or November, else 0
#     P    = logistic(a + b_ps logit(Ps) + b_clim logit(clim) + b_pp logit(Pp)
#                     + b_cold cold + b_early early), the coefficients by lead
# A trace is the hard part: flurries leave a "T" with no measurable
# precipitation, so psn alone (a >= 0.01 in type probability) runs low on the
# lake-effect stations and the climatology and the cold carry it. THE FIT
# (2026-11-20): the NBE's 01Z/13Z runs (IEM archive) for the 21 Thanksgiving
# stations, October-March of the 2023-24, 2024-25 and 2025-26 winters, every
# station-day 0-144 h ahead of its local-standard day (lead from the run + 3
# h), against ACIS daily snowfall (a "T" is new snow): 126,104 forecasts,
# base rate 20.8%. Brier, leave-one-winter-out (the station's base rate /
# the model):
#     lead  0-24h  0.140 / 0.076     24-48h  0.141 / 0.083
#          48-72h  0.142 / 0.090     72-96h  0.142 / 0.094    96-144h  0.143 / 0.105
# and on the 11/15-12/05 days alone 0.144 / 0.085 inside a day to 0.144 /
# 0.113 at 4-6 days. Without the early term the model ran low every
# November (8.1% against 10.9% seen at 2-3 days; 18% against 21% on the late
# November days); with it 20.1% against 20.9%. Calibrated within ~3 points
# in every bin from 5% to 85%. Fails closed: no climatology, short periods
# or a stale run is no fair.
SNOW_FIT = ("NBE 01Z/13Z x 21 stations x Oct-Mar 2023-26 vs ACIS snow (T = yes), "
            "126,104 forecasts; LOWO Brier clim 0.140-0.143 vs model 0.076 (0-24h) .. "
            "0.105 (96-144h); late Nov 0.144 vs 0.085 .. 0.113")
# (lead upper bound, hours, a, b_ps, b_clim, b_pp, b_cold, b_early)
SNOW_RECAL = ((24, -0.758, 0.4784, 0.218, 0.2958, 1.1323, 0.5675),
              (48, -0.6663, 0.4463, 0.3296, 0.2678, 1.125, 0.6703),
              (72, -0.5347, 0.427, 0.445, 0.2351, 0.9811, 0.7771),
              (96, -0.2951, 0.4891, 0.4115, 0.207, 0.8206, 0.8055),
              (1e9, -0.2042, 0.517, 0.3706, 0.1993, 0.6995, 0.7278))


def snow_day_value(v) -> Optional[int]:
    """An ACIS daily snowfall -> 1 for new snow (a trace counts), 0 for none,
    None when missing ('M', 'S', an accumulated 'A' total, unreadable)."""
    s = str(v if v is not None else "").strip()
    if s in ("T", "t"):
        return 1
    try:
        return 1 if float(s) > 0 else 0
    except ValueError:
        return None


def snow_climatology(daily: Dict[str, Optional[int]], first: date, days: int,
                     years: int = CLIM_YEARS, half: int = CLIM_HALF_DAYS
                     ) -> Optional[Tuple[float, int]]:
    """(share of windows with new snow on any day, windows) -- climatology()
    on snow, a trace counting; None under CLIM_MIN_WINDOWS windows."""
    yes = n = 0
    for back in range(1, years + 1):
        try:
            base = first.replace(year=first.year - back)
        except ValueError:                       # Feb 29 in a common year
            base = date(first.year - back, 2, 28)
        for o in range(-half, half + 1):
            d0 = base + timedelta(days=o)
            vals = [daily.get((d0 + timedelta(days=k)).isoformat()) for k in range(days)]
            if any(v is None for v in vals):
                continue
            n += 1
            yes += any(vals)
    return (yes / n, n) if n >= CLIM_MIN_WINDOWS else None


def fetch_snow_clim(sid: str, first: date, days: int) -> Tuple[float, int]:
    """(p, windows) from ACIS daily snowfall at `sid` (SNOW MODEL)."""
    s = date(first.year - CLIM_YEARS - 1, 1, 1)
    e = min(date(first.year - 1, 12, 31) + timedelta(days=CLIM_HALF_DAYS + days + 1),
            datetime.now(timezone.utc).date() - timedelta(days=1))
    rsp = requests.post(ACIS_URL, json={"sid": sid, "sdate": s.isoformat(),
                                        "edate": e.isoformat(), "elems": [{"name": "snow"}]},
                        timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    rsp.raise_for_status()
    js = rsp.json()
    if js.get("error"):
        raise RuntimeError(f"ACIS {sid}: {str(js['error'])[:120]}")
    daily = {str(d): snow_day_value(v) for d, v in js.get("data") or []}
    got = snow_climatology(daily, first, days)
    if got is None:
        raise RuntimeError(f"ACIS {sid}: under {CLIM_MIN_WINDOWS} complete snow windows")
    return got


SnowPeriods = List[Tuple[datetime, Dict[str, Optional[float]]]]
SNOW_FIELDS = ("p12", "psn", "tmp", "txn")


def snow_day_inputs(periods: SnowPeriods, ws: datetime, we: datetime) -> Optional[dict]:
    """{'ps', 'pp', 'tmin', 'coverage'} over the 12-hour periods (ending at
    00Z / 12Z) overlapping [ws, we), else None (short of the window, or a
    period without its snow probability or PoP)."""
    pers = []
    for ft, v in periods:
        if ft.minute or ft.hour not in (0, 12):
            continue
        ov = (min(ft, we) - max(ft - timedelta(hours=12), ws)).total_seconds() / 43200.0
        if ov > 0:
            if v.get("psn") is None or v.get("p12") is None:
                return None
            pers.append((ov, v))
    cov = sum(f for f, _ in pers)
    half_days = round((we - ws).total_seconds() / 43200.0)
    if not pers or cov < MIN_COVERAGE - (4 - half_days):
        return None
    ls = sum(f * math.log(max(1.0 - min(v["psn"], 0.995), 1e-6)) for f, v in pers)
    lp = sum(f * math.log(max(1.0 - min(v["p12"], 0.995), 1e-6)) for f, v in pers)
    temps = [v[k] for _f, v in pers for k in ("tmp", "txn") if v.get(k) is not None]
    return {"ps": 1.0 - math.exp(ls), "pp": 1.0 - math.exp(lp),
            "tmin": min(temps) if temps else None, "coverage": cov}


def snow_recalibrate(ps: float, pp: float, tmin: Optional[float], clim: float,
                     lead_h: float, early: bool = False) -> float:
    """SNOW MODEL's logistic on its inputs, the row for `lead_h`."""
    cold = 0.0 if tmin is None else min(max((40.0 - tmin) / 10.0, -2.0), 3.0)
    for hi, a, b_ps, b_clim, b_pp, b_cold, b_early in SNOW_RECAL:
        if lead_h < hi:
            z = (a + b_ps * _logit(ps) + b_clim * _logit(clim) + b_pp * _logit(pp)
                 + b_cold * cold + b_early * (1.0 if early else 0.0))
            return 1.0 / (1.0 + math.exp(-z))
    return clim


def snow_market_fair(periods: SnowPeriods, runtime: datetime, first: date, lst_hours: int,
                     clim: float, days: int = 1, lag_hours: float = NBE_LAG_HOURS,
                     max_lead: Optional[float] = None) -> Optional[dict]:
    """market_fair on SNOW MODEL: {'p', 'p_raw' (Ps), 'p_precip', 'tmin',
    'lead_h', 'coverage'} for one city's window, else None."""
    ws, we = window(first, lst_hours, days)
    lead = (ws - (runtime + timedelta(hours=lag_hours))).total_seconds() / 3600.0
    if lead < 0 or lead > (MAX_LEAD_HOURS if max_lead is None else max_lead):
        return None
    x = snow_day_inputs(periods, ws, we)
    if x is None:
        return None
    p = min(max(snow_recalibrate(x["ps"], x["pp"], x["tmin"], clim, lead,
                                 early=first.month in (10, 11)), 0.01), 0.99)
    return {"p": round(p, 4), "p_raw": round(x["ps"], 4), "p_precip": round(x["pp"], 4),
            "tmin": x["tmin"], "lead_h": round(lead, 1), "coverage": round(x["coverage"], 2)}


def _ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_nbe(payload: dict) -> Tuple[Optional[datetime], List[Tuple[datetime, float]]]:
    """(latest runtime, [(ftime, p12)]) from IEM api/1/mos.json (NBE or NBS:
    the same rows)."""
    rows = (payload or {}).get("data") or []
    runs: Dict[datetime, List[Tuple[datetime, float]]] = {}
    for r in rows:
        rt, ft, p = _ts(r.get("runtime") or ""), _ts(r.get("ftime") or ""), r.get("p12")
        if rt is None or ft is None or p is None:
            continue
        try:
            runs.setdefault(rt, []).append((ft, float(p) / 100.0))
        except (TypeError, ValueError):
            continue
    if not runs:
        return None, []
    rt = max(runs)
    return rt, sorted(runs[rt])


IEM_GAP_SECS = 0.5     # between our IEM reads (its limiter refuses a burst)
_iem_last = [0.0]


def _iem_mos(station: str, model: str) -> dict:
    wait = _iem_last[0] + IEM_GAP_SECS - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(NBE_URL, params={"station": station, "model": model},
                         timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    finally:
        _iem_last[0] = time.monotonic()
    r.raise_for_status()
    return r.json()


def fetch_nbe(station: str) -> Tuple[Optional[datetime], List[Tuple[datetime, float]]]:
    return parse_nbe(_iem_mos(station, "NBE"))


def fetch_nbs(station: str) -> Tuple[Optional[datetime], List[Tuple[datetime, float]]]:
    """The IEM's latest 6-hourly NBS run for `station` (THE COMPOSITE)."""
    return parse_nbe(_iem_mos(station, "NBS"))


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_nbe_snow(payload: dict) -> Tuple[Optional[datetime], SnowPeriods]:
    """(latest runtime, [(ftime, {'p12', 'psn' in 0..1, 'tmp', 'txn' in F})])
    from IEM api/1/mos.json (SNOW MODEL's inputs)."""
    rows = (payload or {}).get("data") or []
    runs: Dict[datetime, SnowPeriods] = {}
    for r in rows:
        rt, ft = _ts(r.get("runtime") or ""), _ts(r.get("ftime") or "")
        if rt is None or ft is None:
            continue
        v = {k: _num(r.get(k)) for k in SNOW_FIELDS}
        for k in ("p12", "psn"):
            if v[k] is not None:
                v[k] /= 100.0
        runs.setdefault(rt, []).append((ft, v))
    if not runs:
        return None, []
    rt = max(runs)
    return rt, sorted(runs[rt], key=lambda x: x[0])


def fetch_nbe_snow(station: str) -> Tuple[Optional[datetime], SnowPeriods]:
    return parse_nbe_snow(_iem_mos(station, "NBE"))


def _cached_snow(c: dict) -> Tuple[Optional[datetime], SnowPeriods]:
    """The file's copy of a station's snow run ({'run', 'periods': [[ftime,
    p12, psn, tmp, txn]]})."""
    rt = _ts(str(c.get("run") or ""))
    per: SnowPeriods = []
    for row in c.get("periods") or []:
        try:
            ft = _ts(str(row[0]))
            v = {k: _num(x) for k, x in zip(SNOW_FIELDS, row[1:1 + len(SNOW_FIELDS)])}
        except (IndexError, TypeError):
            continue
        if ft is not None:
            per.append((ft, v))
    return (rt, sorted(per, key=lambda x: x[0])) if rt is not None and per else (None, [])


def snow_due_run(sid: str, cache: dict, fn: Callable, now: datetime
                 ) -> Tuple[Tuple[Optional[datetime], SnowPeriods], Optional[str]]:
    """due_run for the snow runs: the IEM asked only once a newer NBE run is
    due, a failed or short read falling back to the file's copy."""
    crt, cper = _cached_snow(cache.get(sid) or {})
    if crt is not None and len(cper) >= MIN_PERIODS \
            and now < crt + timedelta(hours=NBE_EVERY_HOURS + 1.0):
        return (crt, cper), None
    err = None
    try:
        rt, per = fn(sid)
        if rt is None or len(per) < MIN_PERIODS:
            raise ValueError(f"run {rt} with {len(per)} periods")
    except Exception as ex:
        rt, per = None, []
        err = f"{type(ex).__name__}: {str(ex)[:80]}"
    if crt is not None and (rt is None or crt > rt):
        return (crt, cper), err
    return (rt, per), err


# (the bulletin pads a one-digit month or day with a space: " 9/24/2026")
_NBS_HDR = r"^ (\S+)\s+NBM V[\d.]+ NBS GUIDANCE\s+(\d{1,2})/\s*(\d{1,2})/(\d{4})\s+(\d\d)00 UTC"
_NBS_HDR_RE = re.compile(_NBS_HDR)
_NBS_HDR_BYTES_RE = re.compile(_NBS_HDR.encode("ascii"), re.M)


def parse_nbs_block(block: str) -> Tuple[Optional[datetime], List[Tuple[datetime, float]]]:
    """(runtime, [(ftime, p12)]) from one station's block of NOAA's NBS text
    bulletin: a header naming the station and cycle, then rows of a 5-char
    label (" FHR ") and fixed 3-char fields -- FHR gives each column's hours
    after the run, P12 the 12-hour PoP (percent) ending there."""
    lines = (block or "").splitlines()
    m = _NBS_HDR_RE.match(lines[0]) if lines else None
    if not m:
        return None, []
    rt = datetime(int(m.group(4)), int(m.group(2)), int(m.group(3)), int(m.group(5)),
                  tzinfo=timezone.utc)
    rows = {ln[1:4]: ln for ln in lines[1:] if len(ln) > 4}
    fhr, p12 = rows.get("FHR"), rows.get("P12")
    if fhr is None or p12 is None:
        return rt, []
    out = []
    for i in range((len(fhr) - 5) // 3):
        h = fhr[5 + 3 * i: 8 + 3 * i].strip()
        v = p12[5 + 3 * i: 8 + 3 * i].strip()
        if h.isdigit() and v.isdigit():
            out.append((rt + timedelta(hours=int(h)), float(v) / 100.0))
    return rt, out


def nbs_blocks(data: bytes, sids) -> Dict[str, Tuple[int, int, str]]:
    """{station: (offset, length, text)} for `sids` in a whole NBS bulletin."""
    hdr = list(_NBS_HDR_BYTES_RE.finditer(data))
    want = set(sids)
    out = {}
    for i, m in enumerate(hdr):
        sid = m.group(1).decode("ascii", "replace")
        if sid in want:
            end = hdr[i + 1].start() if i + 1 < len(hdr) else len(data)
            out[sid] = (m.start(), end - m.start(),
                        data[m.start():end].decode("ascii", "replace"))
    return out


def _download(http, url: str, hdrs: dict) -> bytes:
    """A whole file, read at most NBS_HOURLY_MAX_BPS (a ~30 MB bulletin
    must not crowd the bots' own connection)."""
    r = http.get(url, headers=hdrs, timeout=max(HTTP_TIMEOUT, 120), stream=True)
    r.raise_for_status()
    buf = bytearray()
    t0 = time.monotonic()
    for chunk in r.iter_content(65536):
        buf += chunk
        if NBS_HOURLY_MAX_BPS > 0:
            ahead = len(buf) / NBS_HOURLY_MAX_BPS - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
    return bytes(buf)


def fetch_nbs_hourly(sids, now: datetime, index: dict, http=requests
                     ) -> Tuple[Optional[datetime], Dict[str, Tuple[datetime, list]], dict, str]:
    """(cycle, {station: (runtime, periods)}, index, how) from the newest
    hourly NBS bulletin NOAA has posted (this hour's cycle -- posted in its
    last minutes -- back to NBS_HOURLY_LOOKBACK hours). `index` ({'sizes':
    {size: {'offsets': {sid: [off,
    len]}, 'absent': [sid]}}}) is where each station's block sat in the last
    whole file of each size, and which were not in it (the off-hour cycles
    share one layout, the 00/06/12/18Z ones another): a file of a known size
    is read by byte range, every block checked to be its station's for this
    cycle; a new size, a station that layout never looked for, or a block
    that is not its station's is read whole once (_download) and that size's
    layout rebuilt -- the last NBS_HOURLY_SIZES are kept. (None, {}, index,
    why) when none is posted."""
    base = now.replace(minute=0, second=0, microsecond=0)
    hdrs = {"User-Agent": USER_AGENT}
    sizes = dict((index or {}).get("sizes") or {})
    for back in range(0, NBS_HOURLY_LOOKBACK + 1):
        cyc = base - timedelta(hours=back)
        url = (f"{NBS_HOURLY_URL}/blend.{cyc:%Y%m%d}/{cyc:%H}/text/"
               f"blend_nbstx.t{cyc:%H}z")
        h = http.head(url, headers=hdrs, timeout=HTTP_TIMEOUT)
        if h.status_code != 200:
            continue
        size = str(int(h.headers.get("Content-Length") or 0))
        got: Dict[str, Tuple[datetime, list]] = {}
        lay = sizes.get(size) or {}
        offs = lay.get("offsets") or {}
        absent = set(lay.get("absent") or ())
        need = [s for s in sids if s not in absent]
        if offs and need and all(s in offs for s in need):
            for sid in need:
                off, ln = offs[sid]
                r = http.get(url, headers=dict(hdrs, Range=f"bytes={off}-{off + ln - 1}"),
                             timeout=HTTP_TIMEOUT)
                txt = r.content.decode("ascii", "replace") if r.status_code == 206 else ""
                rt, per = parse_nbs_block(txt)
                if not txt.startswith(f" {sid} ") or rt != cyc:
                    got = {}
                    break
                got[sid] = (rt, per)
            if len(got) == len(need):
                return cyc, got, {"sizes": sizes}, "by range"
        blocks = nbs_blocks(_download(http, url, hdrs), sids)
        sizes.pop(size, None)
        sizes[size] = {"offsets": {s: [o, ln] for s, (o, ln, _t) in blocks.items()},
                       "absent": sorted(set(sids) - set(blocks))}
        while len(sizes) > NBS_HOURLY_SIZES:
            sizes.pop(next(iter(sizes)))
        for sid, (_o, _l, txt) in blocks.items():
            rt, per = parse_nbs_block(txt)
            if rt == cyc:
                got[sid] = (rt, per)
        return cyc, got, {"sizes": sizes}, "whole file (its layout indexed)"
    return None, {}, {"sizes": sizes}, ("no cycle posted in the last %d hours"
                                        % NBS_HOURLY_LOOKBACK)


def _cached_nbe(c: dict) -> Tuple[Optional[datetime], List[Tuple[datetime, float]]]:
    """The file's copy of a station's run ({'run', 'periods': [[ftime, p]]})."""
    rt = _ts(str(c.get("run") or ""))
    per = []
    for row in c.get("periods") or []:
        try:
            ft, p = _ts(str(row[0])), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if ft is not None:
            per.append((ft, p))
    return (rt, sorted(per)) if rt is not None and per else (None, [])


def station_run(sid: str, cache: dict, nbe_fn: Callable,
                min_periods: int = MIN_PERIODS
                ) -> Tuple[Tuple[Optional[datetime], list], Optional[str]]:
    """(the newer of the station's fresh read and the file's copy, the read's
    error or None). A run with fewer than `min_periods` rows is a failed read."""
    err = None
    try:
        rt, per = nbe_fn(sid)
        if rt is None or len(per) < min_periods:
            raise ValueError(f"run {rt} with {len(per)} periods")
    except Exception as ex:
        rt, per = None, []
        err = f"{type(ex).__name__}: {str(ex)[:80]}"
    crt, cper = _cached_nbe(cache.get(sid) or {})
    if crt is not None and (rt is None or crt > rt):
        return (crt, cper), err
    return (rt, per), err


def due_run(sid: str, cache: dict, fn: Callable, now: datetime, every_h: float,
            min_periods: int = MIN_PERIODS
            ) -> Tuple[Tuple[Optional[datetime], list], Optional[str]]:
    """station_run, but the IEM is asked only once a newer run is due: an
    hour past the file's run + `every_h`, then each pass until it shows."""
    crt, cper = _cached_nbe(cache.get(sid) or {})
    if crt is not None and len(cper) >= min_periods \
            and now < crt + timedelta(hours=every_h + 1.0):
        return (crt, cper), None
    return station_run(sid, cache, fn, min_periods)


def _get_json(url: str, params: Optional[dict] = None) -> dict:
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json() or {}


def lst_hours(tz: str, now: datetime) -> int:
    """Hours local standard time runs behind UTC in the IANA zone `tz`
    (America/Denver -> 7, daylight time taken out), or RuntimeError for an
    unknown zone or one off the whole hour."""
    try:
        loc = now.astimezone(pytz.timezone(tz))
    except pytz.UnknownTimeZoneError:
        raise RuntimeError(f"unknown time zone {tz!r}")
    hours = -(loc.utcoffset() - loc.dst()).total_seconds() / 3600.0
    if hours != int(hours):
        raise RuntimeError(f"time zone {tz} is off the whole hour ({hours:g}h)")
    return int(hours)


def resolve_station(code: str, now: datetime, get: Callable = _get_json,
                    nbe_fn: Optional[Callable] = None) -> dict:
    """An auto row ({'icao', 'lst', 'tz', 'name', ...}) for the station the
    rules' `code` names, or RuntimeError saying which check failed (see
    AUTO-MAPPED STATIONS)."""
    sid = f"K{code}"
    props = get(NWS_STATION_URL.format(sid=sid)).get("properties") or {}
    tz = str(props.get("timeZone") or "")
    if not tz:
        raise RuntimeError(f"{sid}: NWS gives no time zone")
    lst = lst_hours(tz, now)
    cli = get(IEM_CLI_URL, {"station": sid, "year": now.year}).get("results") or []
    days = sorted(str(x.get("valid") or "") for x in cli if x.get("valid"))
    if not days or days[-1] < (now - timedelta(days=AUTO_CLI_MAX_AGE_DAYS)).date().isoformat():
        raise RuntimeError(f"{sid}: no IEM CLI report within {AUTO_CLI_MAX_AGE_DAYS} days "
                           f"(last {days[-1] if days else None})")
    rt, per = (nbe_fn or fetch_nbe)(sid)
    if rt is None or len(per) < MIN_PERIODS:
        raise RuntimeError(f"{sid}: no NBE run of {MIN_PERIODS}+ periods "
                           f"(run {rt}, {len(per)} periods)")
    if (now - rt).total_seconds() / 3600.0 > NBE_MAX_AGE_HOURS:
        raise RuntimeError(f"{sid}: its latest NBE run ({rt:%Y-%m-%d %H}Z) is over "
                           f"{NBE_MAX_AGE_HOURS:g}h old")
    return {"icao": sid, "lst": lst, "tz": tz, "name": str(props.get("name") or sid),
            "auto": True, "checked_at": now.isoformat()}


def _signed_or_public(get_json: Optional[Callable], params: dict,
                      path: str = "/markets") -> dict:
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    url = KALSHI_MARKETS_URL.rsplit("/markets", 1)[0] + path
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def classify_day_markets(markets: List[dict]) -> Tuple[Optional[dict], str]:
    """({'events': {event: [first ISO day, days]}, 'sample': ticker}, '') when
    a series' open markets are the one-day / span contract (AUTO DAY
    SERIES), plus 'kind': 'snow' on the snow contract (SNOW DAYS) and
    'skipped': {ticker: why} for the cities with no readable station, else
    (None, why)."""
    if not markets:
        return None, "no open market"
    events: Dict[str, list] = {}
    kinds = set()
    skipped: Dict[str, str] = {}
    sample = ""
    for m in markets:
        t, ev = str(m.get("ticker") or ""), str(m.get("event_ticker") or "")
        rules = str(m.get("rules_primary") or "")
        if not ev or not t.startswith(ev + "-") \
                or not _CITY_SEG_RE.fullmatch(t[len(ev) + 1:]):
            return None, f"{t}: not one market per city"
        if SNOW_DAY_ENABLE and _SNOW_ZERO_RE.search(rules):
            if not _SNOW_TRACE_RE.search(rules):
                return None, (f"{t}: a trace does not count YES (the snow model's "
                              "outcome counts it)")
            kinds.add("snow")
        elif not _ZERO_RE.search(rules):
            return None, f"{t}: not 'total precipitation ... strictly greater than 0 inches'"
        else:
            kinds.add("rain")
        if len(kinds) > 1:
            return None, f"{t}: rain and snow markets in one series"
        span = rules_span(rules)
        if span is None:
            return None, f"{t}: its rules name no station or no day"
        days = (span[1] - span[0]).days + 1
        if not 1 <= days <= MAX_SPAN_DAYS:
            return None, f"{t}: a {days}-day window"
        if events.setdefault(ev, [span[0].isoformat(), days]) != [span[0].isoformat(), days]:
            return None, f"{ev}: its markets name different windows"
        # ONE CITY, NOT THE SERIES (2026-11-22): Kalshi added Honolulu and
        # Anchorage to Thanksgiving ("at Honolulu (PHNL)"), P codes that
        # station_code doesn't read, and rejecting the series on them took all
        # 51 cities dark from 01:20Z. A city with no readable station stands
        # aside alone (write_fair_file finds no station to price it on).
        if station_code(rules) is None:
            skipped[t] = "its rules name no station this writer reads"
            continue
        sample = sample or t
    if not sample:
        return None, f"{next(iter(skipped))}: its rules name no station or no day"
    got = {"events": events, "sample": sample}
    if kinds == {"snow"}:
        got["kind"] = "snow"
    if skipped:
        got["skipped"] = skipped
    return got, ""


def discover_day_series(old: dict, now_ts: float, get_json: Optional[Callable] = None,
                        skip: Optional[Callable[[str], bool]] = None
                        ) -> Tuple[Dict[str, dict], Dict[str, dict], float]:
    """(day series {series: {'events', 'sample', 'at'}}, rejected {series:
    {'at', 'why'}}, listed at): the file's until DAY_DISCOVER_SECS has passed,
    then Kalshi's weather category re-listed and each unclaimed rain series
    read again once its last verdict is DAY_RETRY_SECS old (AUTO DAY
    SERIES). A series whose read fails keeps its last verdict."""
    found = dict(old.get("day_series") or {})
    rejected = dict(old.get("day_rejected") or {})
    at = float(old.get("day_discovered_at") or 0.0)
    if not AUTO_DAY_ENABLE:
        return {}, rejected, at
    # a verdict from an older classifier is re-read at once (2026-11-14: the
    # parser learned Thanksgiving's "on <day> is" wording, and the 6-hour
    # retry would have kept the old rejection past the program's start)
    old_verdicts = any(int(v.get("v") or 1) != DAY_CLASSIFY_VERSION
                       for v in rejected.values() if isinstance(v, dict))
    if now_ts - at < DAY_DISCOVER_SECS and not old_verdicts:
        return found, rejected, at
    js = _signed_or_public(get_json, {"category": SERIES_CATEGORY}, path="/series")
    names = sorted({str(s.get("ticker") or "") for s in js.get("series") or []})
    out: Dict[str, dict] = {}
    for s in names:
        # rain series, and since SNOW DAYS (2026-11-20) snow ones
        if not ("RAIN" in s or (SNOW_DAY_ENABLE and "SNOW" in s)) \
                or s in (SERIES, "KXRAIN") + DAY_SERIES or (skip and skip(s)):
            continue
        last = found.get(s) or rejected.get(s) or {}
        if last and now_ts - float(last.get("at") or 0.0) < DAY_RETRY_SECS and (
                s in found or int(last.get("v") or 1) == DAY_CLASSIFY_VERSION):
            if s in found:
                out[s] = found[s]
            continue
        try:
            mj = _signed_or_public(get_json, {"series_ticker": s, "status": "open",
                                              "limit": 200})
        except Exception:
            if s in found:
                out[s] = found[s]
            continue
        got, why = classify_day_markets(mj.get("markets") or [])
        if got is None:
            rejected[s] = {"at": now_ts, "why": why, "v": DAY_CLASSIFY_VERSION}
            continue
        rejected.pop(s, None)
        out[s] = dict(got, at=now_ts)
        if s not in found:
            _log(f"day series {s} joins the family: {len(got['events'])} event(s), "
                 f"e.g. {got['sample']}")
    return out, rejected, now_ts


def fetch_events(get_json: Optional[Callable] = None,
                 day_series: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """Open weekend events, the one-day series' (DAY_SERIES) and the auto
    day series' (`day_series`, AUTO DAY SERIES): event -> {'sat': the
    window's first day (ISO), 'days': its length, 'markets': {ticker:
    {'code': CLI code or None, 'span': [first, last] ISO days from the
    rules, or None}}}. An auto series' window is its rules'; a snow series'
    event (SNOW DAYS) carries 'kind': 'snow'."""
    out: Dict[str, dict] = {}
    for series in (SERIES,) + DAY_SERIES + tuple(sorted(day_series or ())):
        kind = ((day_series or {}).get(series) or {}).get("kind")
        js = _signed_or_public(get_json, {"series_ticker": series, "status": "open",
                                          "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            t = str(m.get("ticker") or "")
            rules = str(m.get("rules_primary") or "")
            span = rules_span(rules)
            if series == SERIES:
                first, days = event_saturday(ev), 2
            elif series in DAY_SERIES:
                first, days = event_day(ev), 1
            else:
                first, days = (span[0], (span[1] - span[0]).days + 1) if span else (None, 0)
            if not ev.startswith(series + "-") or not t or first is None:
                continue
            e = out.setdefault(ev, {"sat": first.isoformat(), "days": days, "markets": {}})
            if kind == "snow":
                e["kind"] = "snow"
            e["markets"][t] = {"code": station_code(rules),
                               "span": [d.isoformat() for d in span] if span else None}
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    nbe_fn: Callable = fetch_nbe,
                    resolve_fn: Optional[Callable] = None,
                    nbs_fn: Optional[Callable] = None,
                    nbs_hourly_fn: Optional[Callable] = None,
                    skip: Optional[Callable[[str], bool]] = None,
                    clim_fn: Optional[Callable] = None,
                    snow_nbe_fn: Optional[Callable] = None,
                    snow_clim_fn: Optional[Callable] = None) -> Tuple[int, int]:
    """Build and atomically write the weekend-rain fair file; returns
    (markets with a fair, open markets without one). Kalshi's open events
    are re-read hourly, and every pass while none is open (the Monday gap
    between a weekend's close and the next listing); a failed read reuses
    the cache. Each station's NBE and 6-hourly NBS runs are read once a newer
    one is due (due_run), the newest hourly NBS cycle once NOAA has posted it
    (nbs_hourly_fn, default fetch_nbs_hourly), and a failed read falls back to
    the file's copy (still subject to each source's age limit): THE
    COMPOSITE. A code no STATIONS row maps is auto-mapped (resolve_fn,
    default resolve_station; see AUTO-MAPPED STATIONS). A rain series no
    family claims joins on its contract (AUTO DAY SERIES; `skip`: the
    bot's own rain families); a pass that lists the category re-reads the
    events. A window past the fit takes the station's climatology (LONG
    LEADS; clim_fn, default fetch_clim). A snow event (SNOW DAYS) is priced
    on SNOW MODEL instead: its stations' NBE snow runs (snow_nbe_fn, default
    fetch_nbe_snow; their own copy in the file) and snow climatology
    (snow_clim_fn, default fetch_snow_clim)."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    nbe_cache = old.get("nbe") or {}
    ev_cache = old.get("open_events") or {}
    events_at = float(old.get("events_at") or 0.0)
    day_series, day_rejected, day_at = dict(old.get("day_series") or {}), \
        dict(old.get("day_rejected") or {}), float(old.get("day_discovered_at") or 0.0)
    listed = False
    if events is None:
        try:
            day_series, day_rejected, day_at = discover_day_series(old, ts, get_json, skip)
            listed = day_at == ts
        except Exception as ex:
            _log(f"weather category read failed ({type(ex).__name__}: {str(ex)[:120]}); "
                 f"the file's day series stand")
        if ev_cache and ts - events_at < EVENTS_TTL_SECS and not listed:
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json, day_series)
                events_at = ts
            except Exception:
                if not ev_cache:
                    raise
                events = ev_cache
    # the auto-mapped stations: the file's, plus any code an open market
    # names that no row maps (a failure is retried after AUTO_RETRY_SECS)
    auto = {c: v for c, v in (old.get("auto_stations") or {}).items()
            if isinstance(v, dict) and v.get("icao") and v.get("lst") is not None}
    unmapped: Dict[str, dict] = dict(old.get("unmapped") or {})
    resolve = resolve_fn or resolve_station
    codes = {str(mk.get("code")) for e in events.values()
             for mk in (e.get("markets") or {}).values() if mk.get("code")}
    for code in sorted(codes):
        if code in STATIONS or code in auto:
            unmapped.pop(code, None)
            continue
        if ts - float((unmapped.get(code) or {}).get("at") or 0) < AUTO_RETRY_SECS:
            continue
        try:
            auto[code] = resolve(code, now)
            unmapped.pop(code, None)
            _log(f"station {code} auto-mapped: {auto[code]['name']} ({auto[code]['icao']}, "
                 f"{auto[code]['tz']}, standard time UTC-{auto[code]['lst']})")
        except Exception as ex:
            unmapped[code] = {"at": ts, "why": f"{type(ex).__name__}: {str(ex)[:160]}"}
            _log(f"! station {code} unmapped, its markets stand aside: "
                 f"{unmapped[code]['why']}")
    stamp = now.isoformat()
    markets: Dict[str, dict] = {}
    missing: Dict[str, str] = {}
    # 1. the markets to price, and the stations they need (a snow event's to
    # snow_todo: SNOW DAYS)
    todo: List[Tuple[str, str, date, str, int, int]] = []
    snow_todo: List[Tuple[str, str, date, str, int, int]] = []
    for ev, e in events.items():
        sat = date.fromisoformat(e["sat"])
        days = int(e.get("days") or 2)
        for t, mk in sorted(e["markets"].items()):
            code, span = mk.get("code"), mk.get("span")
            st = STATIONS.get(code or "")
            if st is None and code in auto:
                st = (auto[code]["icao"], int(auto[code]["lst"]))
            if st is None and not code:
                # a city classify_day_markets skipped (2026-11-22, PHNL / PANC)
                missing[t] = "its rules name no station this writer reads"
                continue
            if st is None:
                why = (unmapped.get(code or "") or {}).get("why")
                missing[t] = f"unmapped station {code}" + (f" ({why})" if why else "")
                continue
            if span and span != [sat.isoformat(), (sat + timedelta(days=days - 1)).isoformat()]:
                missing[t] = (f"rules window {span[0]}..{span[1]} is not the "
                              + ("weekend" if days == 2 else "ticker's day"))
                continue
            sid, lst = st
            if now >= window(sat, lst, days)[0]:
                missing[t] = "weekend started" if days == 2 else "the day started"
                continue
            (snow_todo if e.get("kind") == "snow" else todo).append((t, ev, sat, sid, lst, days))
    sids = sorted({sid for _t, _e, _s, sid, _l, _d in todo})
    # 2. each source once per station: the NBE and the 6-hourly NBS from the
    # IEM when a newer run is due, the newest hourly NBS cycle from NOAA
    nbe: Dict[str, Tuple[Optional[datetime], list]] = {}
    nbs: Dict[str, Tuple[Optional[datetime], list]] = {}
    failed: Dict[str, str] = {}
    nbs_failed: Dict[str, str] = {}
    nbs_cache = old.get("nbs") or {}
    for sid in sids:
        nbe[sid], err = due_run(sid, nbe_cache, nbe_fn, now, NBE_EVERY_HOURS)
        if err:
            failed[sid] = err
        if NBS_ENABLE:
            nbs[sid], err = due_run(sid, nbs_cache, nbs_fn or fetch_nbs, now,
                                    NBS_EVERY_HOURS, NBS_MIN_PERIODS)
            if err:
                nbs_failed[sid] = err
    hourly_old = old.get("nbs_hourly") or {}
    h_cycle = _ts(str(hourly_old.get("cycle") or ""))
    h_index = hourly_old.get("index") or {}
    h_runs: Dict[str, Tuple[Optional[datetime], list]] = {
        sid: _cached_nbe(c) for sid, c in (hourly_old.get("runs") or {}).items()}
    h_searched = set(hourly_old.get("searched") or ())
    if NBS_HOURLY_ENABLE and sids and (
            h_cycle is None or now >= h_cycle + timedelta(hours=1 + NBS_HOURLY_LAG_HOURS)
            or any(sid not in h_runs and sid not in h_searched for sid in sids)):
        try:
            cyc, got, h_index, how = (nbs_hourly_fn or fetch_nbs_hourly)(sids, now, h_index)
            if cyc is not None and (h_cycle is None or cyc > h_cycle
                                    or any(s not in h_runs for s in got)):
                h_cycle, h_runs = cyc, got
                h_searched = set(sids)
                _log(f"NBS hourly cycle {cyc:%Y-%m-%d %H}Z: {len(got)} of {len(sids)} "
                     f"station(s), {how}")
        except Exception as ex:
            _log(f"NBS hourly read failed ({type(ex).__name__}: {str(ex)[:120]}); "
                 f"the file's cycle stands in while young enough")
    # 2b. LONG LEADS: the climatology of each window that may sit past the
    # fit (the lead runs from the run, which can be up to NBE_MAX_AGE_HOURS
    # old), read once and kept CLIM_TTL_DAYS; a failed read is retried after
    # AUTO_RETRY_SECS
    clim: Dict[str, dict] = {k: v for k, v in (old.get("clim") or {}).items()
                             if isinstance(v, dict)
                             and ts - float(v.get("at") or 0) < CLIM_TTL_DAYS * 86400}
    reads = 0
    for t, ev, sat, sid, lst, days in todo if CLIM_ENABLE else ():
        key = f"{sid}|{sat.isoformat()}|{days}"
        lead_now = (window(sat, lst, days)[0] - now).total_seconds() / 3600.0
        c = clim.get(key) or {}
        if lead_now <= MAX_LEAD_HOURS - NBE_MAX_AGE_HOURS or c.get("p") is not None \
                or (c.get("why") and ts - float(c.get("at") or 0) < AUTO_RETRY_SECS) \
                or reads >= CLIM_MAX_READS:
            continue
        reads += 1
        try:
            p, n = (clim_fn or fetch_clim)(sid, sat, days)
            clim[key] = {"p": round(p, 4), "n": n, "at": ts}
            _log(f"climatology {key}: {p:.1%} of {n} windows")
        except Exception as ex:
            clim[key] = {"at": ts, "why": f"{type(ex).__name__}: {str(ex)[:160]}"}
            _log(f"! climatology {key} failed: {clim[key]['why']}")
    # 3. each market's composite: the NBS periods (hourly and 6-hourly blended
    # where both reach), the NBE for the rest, the lead off the freshest run
    for t, ev, sat, sid, lst, days in todo:
        ws = window(sat, lst, days)[0]
        c = clim.get(f"{sid}|{sat.isoformat()}|{days}") or {}
        srcs = []
        e_rt, e_per = nbe.get(sid) or (None, [])
        if e_rt is not None and (ts - e_rt.timestamp()) / 3600.0 <= NBE_MAX_AGE_HOURS:
            srcs.append(("E", e_rt, NBE_LAG_HOURS, dict(e_per)))
        s_rt, s_per = nbs.get(sid) or (None, [])
        if s_rt is not None and len(s_per) >= NBS_MIN_PERIODS \
                and (ts - s_rt.timestamp()) / 3600.0 <= NBS_MAX_AGE_HOURS:
            srcs.append(("S", s_rt, NBS_LAG_HOURS, dict(s_per)))
        h_rt, h_per = h_runs.get(sid) or (None, [])
        if NBS_HOURLY_ENABLE and h_rt is not None and len(h_per) >= NBS_MIN_PERIODS \
                and (ts - h_rt.timestamp()) / 3600.0 <= NBS_HOURLY_MAX_AGE_HOURS:
            srcs.append(("H", h_rt, NBS_HOURLY_LAG_HOURS, dict(h_per)))
        if not srcs:
            lead_now = (ws - now).total_seconds() / 3600.0
            if CLIM_ENABLE and lead_now > MAX_LEAD_HOURS:
                if c.get("p") is None:
                    missing[t] = ("past the fit, no fresh NBE run and no climatology"
                                  + (f" ({c['why']})" if c.get("why") else ""))
                    continue
                markets[t] = dict(p=min(max(c["p"], 0.01), 0.99), p_raw=None,
                                  lead_h=round(lead_now, 1), coverage=None, event=ev,
                                  station=sid, run=None, fetched_at=stamp, src="C",
                                  runs={}, clim=c["p"], clim_n=c.get("n"), w_model=0.0)
                continue
            missing[t] = "no fresh NBE run"
            continue
        by = {k: per for k, _rt, _lag, per in srcs}
        merged = dict(by.get("E") or {})
        hd, sd = by.get("H") or {}, by.get("S") or {}
        for ft in set(hd) | set(sd):
            if ft in hd and ft in sd:
                merged[ft] = NBS_W_HOURLY * hd[ft] + (1.0 - NBS_W_HOURLY) * sd[ft]
            else:
                merged[ft] = hd[ft] if ft in hd else sd[ft]
        # the freshest run: the newest runtime (a tie, the one posted soonest)
        _k, f_rt, f_lag, _p = max(srcs, key=lambda s: (s[1], -s[2]))
        f = market_fair(sorted(merged.items()), f_rt, sat, lst, lag_hours=f_lag, days=days)
        lead = (ws - (f_rt + timedelta(hours=f_lag))).total_seconds() / 3600.0
        if f is None and CLIM_ENABLE and lead > MAX_LEAD_HOURS:
            # LONG LEADS: the climatology, with the NBM blended in toward the
            # fit's edge where it reaches the window
            if c.get("p") is None:
                missing[t] = ("past the fit and no climatology"
                              + (f" ({c['why']})" if c.get("why") else ""))
                continue
            fl = market_fair(sorted(merged.items()), f_rt, sat, lst, lag_hours=f_lag,
                             days=days, max_lead=CLIM_LEAD_HOURS)
            w = (min(max((CLIM_LEAD_HOURS - lead) / (CLIM_LEAD_HOURS - MAX_LEAD_HOURS),
                         0.0), 1.0) if fl is not None and CLIM_LEAD_HOURS > MAX_LEAD_HOURS
                 else 0.0)
            p = w * (fl["p"] if fl else 0.0) + (1.0 - w) * c["p"]
            srcs_used = [k for k in ("H", "S", "E") if k in by] if w > 0 else []
            markets[t] = dict(p=round(min(max(p, 0.01), 0.99), 4),
                              p_raw=fl["p_raw"] if fl else None, lead_h=round(lead, 1),
                              coverage=fl["coverage"] if fl else None, event=ev,
                              station=sid, run=f_rt.isoformat() if w > 0 else None,
                              fetched_at=stamp, src="+".join(srcs_used + ["C"]),
                              runs={k: rt.isoformat() for k, rt, _lag, _p in srcs} if w > 0
                              else {},
                              clim=c["p"], clim_n=c.get("n"), w_model=round(w, 3),
                              p_model=fl["p"] if fl else None)
            continue
        if f is None:
            span = "the weekend" if days == 2 else "the day"
            missing[t] = (f"lead past the fit or periods short of {span}" if "E" in by
                          else f"no fresh NBE run, and the NBS alone does not cover {span}")
            continue
        markets[t] = dict(f, event=ev, station=sid, run=f_rt.isoformat(),
                          fetched_at=stamp, src="+".join(k for k in ("H", "S", "E") if k in by),
                          runs={k: rt.isoformat() for k, rt, _lag, _p in srcs})
    # 4. SNOW DAYS: each snow station's NBE snow run (once a newer one is due)
    # and each window's snow climatology (a model input at every lead, kept
    # CLIM_TTL_DAYS, a failure retried after AUTO_RETRY_SECS), then SNOW MODEL
    snow_cache = old.get("nbe_snow") or {}
    snow_runs: Dict[str, Tuple[Optional[datetime], SnowPeriods]] = {}
    snow_failed: Dict[str, str] = {}
    for sid in sorted({x[3] for x in snow_todo}):
        snow_runs[sid], err = snow_due_run(sid, snow_cache, snow_nbe_fn or fetch_nbe_snow, now)
        if err:
            snow_failed[sid] = err
    snow_clim: Dict[str, dict] = {k: v for k, v in (old.get("snow_clim") or {}).items()
                                  if isinstance(v, dict)
                                  and ts - float(v.get("at") or 0) < CLIM_TTL_DAYS * 86400}
    s_reads = 0
    for t, ev, first, sid, lst, days in snow_todo:
        key = f"{sid}|{first.isoformat()}|{days}"
        c = snow_clim.get(key) or {}
        if c.get("p") is not None or s_reads >= CLIM_MAX_READS \
                or (c.get("why") and ts - float(c.get("at") or 0) < AUTO_RETRY_SECS):
            continue
        s_reads += 1
        try:
            p, n = (snow_clim_fn or fetch_snow_clim)(sid, first, days)
            snow_clim[key] = {"p": round(p, 4), "n": n, "at": ts}
            _log(f"snow climatology {key}: {p:.1%} of {n} windows")
        except Exception as ex:
            snow_clim[key] = {"at": ts, "why": f"{type(ex).__name__}: {str(ex)[:160]}"}
            _log(f"! snow climatology {key} failed: {snow_clim[key]['why']}")
    for t, ev, first, sid, lst, days in snow_todo:
        c = snow_clim.get(f"{sid}|{first.isoformat()}|{days}") or {}
        if c.get("p") is None:
            missing[t] = "no snow climatology" + (f" ({c['why']})" if c.get("why") else "")
            continue
        ws = window(first, lst, days)[0]
        rt, per = snow_runs.get(sid) or (None, [])
        if rt is not None and (ts - rt.timestamp()) / 3600.0 > NBE_MAX_AGE_HOURS:
            rt, per = None, []
        lead = ((ws - (rt + timedelta(hours=NBE_LAG_HOURS))).total_seconds() / 3600.0
                if rt is not None else (ws - now).total_seconds() / 3600.0)
        base = dict(event=ev, station=sid, fetched_at=stamp, kind="snow", clim=c["p"],
                    clim_n=c.get("n"))
        f = snow_market_fair(per, rt, first, lst, c["p"], days=days) if rt is not None else None
        if f is not None:
            markets[t] = dict(f, run=rt.isoformat(), src="E", runs={"E": rt.isoformat()}, **base)
            continue
        if not (CLIM_ENABLE and lead > MAX_LEAD_HOURS):
            missing[t] = ("snow: no fresh NBE run" if rt is None
                          else "snow: periods short of the day or a period without psn")
            continue
        # LONG LEADS on snow: the climatology, the model blended in toward the
        # fit's edge where it reaches the window
        fl = (snow_market_fair(per, rt, first, lst, c["p"], days=days, max_lead=CLIM_LEAD_HOURS)
              if rt is not None else None)
        w = (min(max((CLIM_LEAD_HOURS - lead) / (CLIM_LEAD_HOURS - MAX_LEAD_HOURS), 0.0), 1.0)
             if fl is not None and CLIM_LEAD_HOURS > MAX_LEAD_HOURS else 0.0)
        p = w * (fl["p"] if fl else 0.0) + (1.0 - w) * c["p"]
        markets[t] = dict(p=round(min(max(p, 0.01), 0.99), 4),
                          p_raw=fl["p_raw"] if fl else None, lead_h=round(lead, 1),
                          coverage=fl["coverage"] if fl else None,
                          run=rt.isoformat() if w > 0 else None,
                          src="E+C" if w > 0 else "C",
                          runs={"E": rt.isoformat()} if w > 0 else {},
                          w_model=round(w, 3), p_model=fl["p"] if fl else None, **base)
    if snow_failed:
        sid, err = sorted(snow_failed.items())[0]
        _log(f"NBE snow read failed for {len(snow_failed)} station(s) ({sid}: {err}); "
             "the file's runs stand in while young enough")
    if failed:
        sid, err = sorted(failed.items())[0]
        _log(f"NBE read failed for {len(failed)} station(s) ({sid}: {err}); "
             "the file's runs stand in while young enough")
    if nbs_failed:
        sid, err = sorted(nbs_failed.items())[0]
        _log(f"NBS (6-hourly) read failed for {len(nbs_failed)} station(s) ({sid}: {err}); "
             "the file's runs stand in while young enough")

    def _runs(d):
        return {sid: {"run": rt.isoformat(),
                      "periods": [[ft.isoformat(), p] for ft, p in per]}
                for sid, (rt, per) in sorted(d.items()) if rt is not None}
    payload = {"generated_at": stamp, "events_at": events_at, "open_events": events,
               "markets": markets, "missing": missing, "nbe": _runs(nbe),
               "nbs": _runs(nbs),
               "nbs_hourly": {"cycle": h_cycle.isoformat() if h_cycle else None,
                              "index": h_index, "runs": _runs(h_runs),
                              "searched": sorted(h_searched)},
               "auto_stations": auto, "unmapped": unmapped,
               "day_series": day_series, "day_rejected": day_rejected,
               "day_discovered_at": day_at, "clim": clim,
               "nbe_snow": {sid: {"run": rt.isoformat(),
                                  "periods": [[ft.isoformat()] + [v.get(k) for k in SNOW_FIELDS]
                                              for ft, v in per]}
                            for sid, (rt, per) in sorted(snow_runs.items()) if rt is not None},
               "snow_clim": snow_clim,
               "model": {"w": W, "recal": RECAL, "min_coverage": MIN_COVERAGE,
                         "snow_recal": SNOW_RECAL, "snow_fit": SNOW_FIT,
                         "max_lead_h": MAX_LEAD_HOURS, "nbe_max_age_h": NBE_MAX_AGE_HOURS,
                         "clim": CLIM_ENABLE, "clim_lead_h": CLIM_LEAD_HOURS,
                         "clim_years": CLIM_YEARS, "clim_half_days": CLIM_HALF_DAYS,
                         "nbs_w_hourly": NBS_W_HOURLY,
                         "nbs_max_age_h": NBS_MAX_AGE_HOURS,
                         "nbs_hourly_max_age_h": NBS_HOURLY_MAX_AGE_HOURS}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)
    return len(markets), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "rain_wknd_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    d = _read_json(args.out)
    _log(f"{ok} markets priced, {miss} without a fair")
    for t, m in sorted((d.get("markets") or {}).items()):
        raw = "-" if m.get("p_raw") is None else f"{m['p_raw'] * 100:.0f}"
        _log(f"  {t}: {m['p'] * 100:.0f}c (raw {raw}, lead "
             f"{m['lead_h']:.0f}h, {m['station']}, {m.get('src')}"
             + (f", clim {m['clim'] * 100:.0f}" if m.get("clim") is not None else "") + ")")
    for t, why in sorted((d.get("missing") or {}).items()):
        _log(f"  {t}: no fair -- {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
