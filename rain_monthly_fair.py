#!/usr/bin/env python3
"""Monthly rain fair values for incentive_mm's monthly rain gate.

KXRAIN<CITY>M-<YYMON>-<K> ("If the total precipitation at CLIORD in Chicago
in Oct 2026 is strictly greater than 3 inches, then the market resolves to
Yes") settles on the station's NWS climate report (CLI) month total. Jack
2026-10-01: "quote these monthly rain markets, with an algorithm like how you
quote the dailies KXRAINCHIM-26OCT, KXRAINAUSM-26OCT". The dailies' algorithm:
a fair value from the NWS forecast decides WHETHER the bot joins the touch,
never WHERE (it stands aside when the touch fights the fair by more than the
tolerance), and the bot never quotes the day the rain is measured. The
monthly version, on rain_monthly.py's calibrated model:
  - fair = P(month-to-date + rest of month > K): the month-to-date from the
    CLI, IEM daily obs and today's running total
    (rain_monthly.effective_mtd); the rest by Monte Carlo over 28-46 years
    of the station's history with the NWS gridpoint forecast injected over
    its horizon (rain_monthly.simulate_remaining) -- Brier skill 62% over
    43,080 walk-forward predictions on the climatology backbone;
  - the station is read from each event's own rules ("at CLIORD"), never a
    fixed map: October's KXRAINCHIM settles at O'Hare (CLIORD), not the
    Midway rain_monthly.py was verified on in July;
  - a monthly is measured on every day of its month, so "never the rain
    day" becomes: the event stands aside while it rains at the station (the
    latest observation, younger than OBS_MAX_AGE_MIN, shows precipitation
    in the last hour or a precipitation weather code) and for DRY_MIN after
    the last wet observation; and it stops for good at 22:00 ET the day
    before the month's last day (the cutoff, set in incentive_mm);
  - rungs within BOUNDARY_IN of the month-to-date stand aside (obs vs CLI
    rounding), and every rung of an event whose last CLI is older than
    rain_monthly.STALE_CLI_HOURS.
Writes RAIN_MONTHLY_FILE: per market p and its event; per event the
month-to-date, CLI date, station, wet state, last wet observation and
staleness. The gate in incentive_mm fails CLOSED on all of it.

PERIOD TOTALS (Jack 2026-10-04: "quote KXRAINNAPAM and similar families
based on rain feed"). KXRAINNAPAM-01NOV26-31MAR27-T20 ("If the total
precipitation at KAPC in Napa in ... is strictly greater than 20 inches")
sums The Weather Company's daily values at the station over the period in
the ticker (DDMONYY-DDMONYY, both days inclusive -- the rules' own "from
November 1, 2026 through March 31, 2027" must agree, else the event has no
station and stands aside). Napa lists months and the November-March season;
KXRAINNYCW the week. Same fair, P(to date + rest > K), with three changes:
  - no CLI at these stations: the to-date is IEM's daily summaries for the
    period's past days (ACIS fills a day IEM lacks) plus today's running
    total; more than PERIOD_MAX_MISSING_DAYS unread days, or an unread day
    the station was last seen wet on, is stale (fail closed);
  - the rest is drawn from WHOLE calendar-aligned windows of the station's
    history (a wet November says something about the season), the NWS
    forecast injected over its horizon as above, and the historical part
    smoothed by a log-normal kernel (PERIOD_KERNEL_SIGMA): 28 Napa seasons
    never saw 30 inches, which is not a 0% chance. Leave-one-out over those
    seasons, strikes 10-35: Brier 0.1123 raw, 0.1115 at sigma 0.15;
  - rain matters only inside the period: before it starts the event reads
    dry (no observation needed), so the season quotes through October's
    storms, and stands aside in them from November 1.
Station codes come from the rules: "at CLIORD", "at KAPC", "at NYC in",
"(KBOS; ...", or a named place (STATION_ALIASES).

Standalone:  python rain_monthly_fair.py [--out path] [--period-series S,..]
"""

import argparse
import calendar
import json
import math
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pytz
import requests

import rain_monthly as rm


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_RAIN_MONTHLY_MARKETS_URL",
    "https://api.elections.kalshi.com/trade-api/v2/markets")
ALL_SERIES = ("KXRAINAUSM,KXRAINCHIM,KXRAINCLLM,KXRAINCMHM,KXRAINDALM,KXRAINDENM,"
              "KXRAINHOUM,KXRAINLAXM,KXRAINLEXM,KXRAINMIAM,KXRAINMKEM,KXRAINNYCM,"
              "KXRAINPVDM,KXRAINSEAM,KXRAINSFOM,KXRAINSTPM")
SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_RAIN_MONTHLY_SERIES", ALL_SERIES).split(",") if s.strip())
# period-total series priced on every write; incentive_mm adds any other
# KXRAIN period series its programs feed shows (write_fair_file's
# period_series)
PERIOD_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_RAIN_PERIOD_SERIES", "KXRAINNAPAM,KXRAINNYCW").split(",") if s.strip())
PERIOD_KERNEL_SIGMA = _env_float("IMM_RAIN_PERIOD_KERNEL_SIGMA", 0.15)
PERIOD_MAX_MISSING_DAYS = int(_env_float("IMM_RAIN_PERIOD_MAX_MISSING_DAYS", 1))
PERIOD_MAX_DAYS = 400
N_SAMPLES = int(_env_float("IMM_RAIN_MONTHLY_SAMPLES", rm.N_SAMPLES))
OBS_MAX_AGE_MIN = _env_float("IMM_RAIN_MONTHLY_OBS_MAX_AGE_MIN", 90)
BOUNDARY_IN = _env_float("IMM_RAIN_MONTHLY_BOUNDARY_IN", rm.BOUNDARY_IN)
EVENTS_TTL_SECS = 3600
HTTP_TIMEOUT = 30

# CLI code (the rules' "at CLIxxx") -> station spec in rain_monthly.STATIONS'
# shape. Seeded from rain_monthly's verified stations, plus O'Hare (CLIORD,
# KXRAINCHIM from Oct 2026). A code missing here leaves its event without a
# fair -- stood aside, never guessed.
CLI_STATIONS: Dict[str, dict] = {
    v["iem"]: {k: v[k] for k in ("icao", "iem", "net", "name", "lat", "lon", "tz")}
    for v in rm.STATIONS.values()}
CLI_STATIONS["ORD"] = {"icao": "KORD", "iem": "ORD", "net": "IL_ASOS",
                       "name": "Chicago O'Hare", "lat": 41.995, "lon": -87.934,
                       "tz": "US/Central"}
# the cities added 2026-10-01 (Jack: "yes add all the cities"): every code
# the October rules name, checked live that day on all three feeds -- IEM
# currents ({state}_ASOS), IEM's CLI (K + code, a full September) and ACIS
# (K + code) -- coordinates from ACIS, zones from IEM's local-vs-UTC stamps
for _code, _net, _name, _lat, _lon, _tz in (
        ("CLL", "TX_ASOS", "College Station Easterwood", 30.588, -96.364, "US/Central"),
        ("CMH", "OH_ASOS", "Columbus John Glenn", 39.991, -82.881, "US/Eastern"),
        ("LAX", "CA_ASOS", "Los Angeles Intl", 33.938, -118.387, "US/Pacific"),
        ("LEX", "KY_ASOS", "Lexington Bluegrass", 38.041, -84.606, "US/Eastern"),
        ("MKE", "WI_ASOS", "Milwaukee Mitchell", 42.947, -87.897, "US/Central"),
        ("PVD", "RI_ASOS", "Providence T.F. Green", 41.722, -71.433, "US/Eastern"),
        ("SFO", "CA_ASOS", "San Francisco Intl", 37.619, -122.375, "US/Pacific")):
    CLI_STATIONS[_code] = {"icao": f"K{_code}", "iem": _code, "net": _net,
                           "name": _name, "lat": _lat, "lon": _lon, "tz": _tz}
# the period-total stations (2026-10-04), checked live that day on IEM
# currents + daily ({state}_ASOS) and ACIS (record from 1998-05-22 for Napa,
# 28 full November-March seasons); coordinates from ACIS
CLI_STATIONS["APC"] = {"icao": "KAPC", "iem": "APC", "net": "CA_ASOS",
                       "name": "Napa County Airport", "lat": 38.2075,
                       "lon": -122.2804, "tz": "US/Pacific"}
# rules that name the place rather than a CLI product ("at Central Park, New
# York City" on KXRAINNYCM)
STATION_ALIASES = {"Central Park": "NYC"}

# METAR present-weather tokens that mean precipitation at or near the station
_WET_RE = re.compile(r"(RA|DZ|SN|SG|PL|GR|GS|UP|TS|SH)")
_CLI_RE = re.compile(r"\bat\s+CLI([A-Z]{3})\b")
# the period rules' station: "(KBOS; BOS on the TWC daily dashboard)", "at
# KAPC in Napa", "at NYC in New York City"
_ICAO_PAREN_RE = re.compile(r"\(K([A-Z]{3});")
_ICAO_RE = re.compile(r"\bat\s+K([A-Z]{3})\b")
_CODE_RE = re.compile(r"\bat\s+([A-Z]{3})\s+in\b")
_PERIOD_SEG_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
_RULES_PERIOD_RE = re.compile(
    r"\bfrom\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s+through\s+"
    r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
_MONTH_NAMES = {n: i for i, n in enumerate(calendar.month_name) if n}


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[rain-monthly-fair] {msg}", flush=True)


def station_code(rules: str) -> Optional[str]:
    """The settlement station's code from a market's rules text: "at
    CLIxxx", "(Kxxx;", "at Kxxx", "at XXX in", else a named place in
    STATION_ALIASES."""
    for rx in (_CLI_RE, _ICAO_PAREN_RE, _ICAO_RE, _CODE_RE):
        m = rx.search(rules or "")
        if m:
            return m.group(1)
    for place, code in STATION_ALIASES.items():
        if re.search(rf"\bat\s+{re.escape(place)}\b", rules or ""):
            return code
    return None


def parse_period(event: str) -> Optional[Tuple[date, date]]:
    """KXRAINNAPAM-01NOV26-31MAR27 -> (2026-11-01, 2027-03-31): the event's
    two day segments, DDMONYY, both days inclusive. None for any other
    shape (the monthlies' 26OCT, a one-date ticker, a period that runs
    backwards or past PERIOD_MAX_DAYS)."""
    parts = event.split("-")
    if len(parts) < 3:
        return None
    days = []
    for seg in parts[1:3]:
        m = _PERIOD_SEG_RE.fullmatch(seg)
        month = rm.MONTHS.get(m.group(2)) if m else None
        if month is None:
            return None
        try:
            days.append(date(2000 + int(m.group(3)), month, int(m.group(1))))
        except ValueError:
            return None
    if not days[0] <= days[1] or (days[1] - days[0]).days > PERIOD_MAX_DAYS:
        return None
    return days[0], days[1]


def rules_period(text: str) -> Optional[Tuple[date, date]]:
    """The period a market's rules name ("from November 1, 2026 through
    March 31, 2027"), or None when they name none."""
    m = _RULES_PERIOD_RE.search(text or "")
    if not m:
        return None
    try:
        return (date(int(m.group(3)), _MONTH_NAMES[m.group(1)], int(m.group(2))),
                date(int(m.group(6)), _MONTH_NAMES[m.group(4)], int(m.group(5))))
    except (KeyError, ValueError):
        return None


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


def all_series(period_series: Iterable[str] = ()) -> Tuple[str, ...]:
    """The monthly SERIES, then PERIOD_SERIES and any extra period series,
    each once."""
    out: List[str] = []
    for s in tuple(SERIES) + tuple(PERIOD_SERIES) + tuple(period_series or ()):
        if s and s not in out:
            out.append(s)
    return tuple(out)


def fetch_events(get_json: Optional[Callable] = None,
                 period_series: Iterable[str] = ()) -> Dict[str, dict]:
    """Open rain-total events: event -> {"series", "code", "strikes":
    {ticker: K}}, plus "start"/"end" (ISO days) on a period event. The code
    comes from the rules; an event whose markets disagree on it, or whose
    rules name another period than its ticker, gets no code (stood aside)."""
    out: Dict[str, dict] = {}
    for s in all_series(period_series):
        js = _signed_or_public(get_json, {"series_ticker": s, "status": "open", "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            t = str(m.get("ticker") or "")
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not ev or not t or str(m.get("strike_type") or "greater") != "greater":
                continue
            code = station_code(f"{m.get('rules_primary') or ''}")
            per = parse_period(ev)
            if per is not None:
                named = rules_period(f"{m.get('rules_secondary') or ''}")
                if named is not None and named != per:
                    code = None
            e = out.setdefault(ev, {"series": s, "code": code, "strikes": {}})
            if per is not None:
                e["start"], e["end"] = per[0].isoformat(), per[1].isoformat()
            if e["code"] != code:
                e["code"] = None
            e["strikes"][t] = k
    return out


def obs_state(spec: dict, now: datetime) -> dict:
    """The station's latest observation: {"wet": True/False/None, "obs_time",
    "phour", "wx"}. None = no observation younger than OBS_MAX_AGE_MIN."""
    try:
        data = rm.http_json(f"https://mesonet.agron.iastate.edu/api/1/currents.json"
                            f"?station={spec['iem']}&network={spec['net']}", tries=2)
        row = (data.get("data") or [{}])[0]
    except RuntimeError:
        return {"wet": None, "obs_time": None, "phour": None, "wx": None}
    t_s = str(row.get("utc_valid") or "")
    try:
        t = datetime.fromisoformat(t_s.replace("Z", "+00:00"))
    except ValueError:
        t = None
    phour = rm.parse_precip(row.get("phour"))
    wx = str(row.get("wxcodes") or "")
    if t is None or (now - t).total_seconds() > OBS_MAX_AGE_MIN * 60:
        return {"wet": None, "obs_time": t_s or None, "phour": phour, "wx": wx}
    wet = bool((phour or 0.0) > 0.0 or _WET_RE.search(wx))
    return {"wet": wet, "obs_time": t.isoformat(), "phour": phour, "wx": wx}


def period_history(key: str, now: datetime,
                   elem: str = "pcpn") -> Dict[str, Optional[float]]:
    """ACIS daily precipitation at the station from rm.HIST_YEARS back
    through yesterday: {YYYY-MM-DD: inches, None = missing}. One cached
    read a day per station. `elem` "snow" reads snowfall (the snow gate),
    and a station's "acis" id replaces its ICAO where the airport's own
    record is short (Denver's threaded DENthr)."""
    st = rm.STATIONS[key]
    last = (now - timedelta(days=1)).date()
    data = rm.cached_get_json(
        f"acis_period_{key}" if elem == "pcpn" else f"acis_{elem}_{key}", 86400,
        lambda: rm.http_json("https://data.rcc-acis.org/StnData", post_body={
            "sid": st.get("acis") or st["icao"],
            "sdate": f"{last.year - rm.HIST_YEARS}-01-01",
            "edate": last.isoformat(), "elems": [{"name": elem}]}))
    return {str(d): rm.parse_precip(v) for d, v in data.get("data") or []}


def period_segments(hist: Dict[str, Optional[float]],
                    days: List[date]) -> List[List[float]]:
    """The historical windows calendar-aligned with `days`, one per past
    year: the same month-days `back` years earlier (a target Feb 29 reads
    that year's Feb 28; a past Feb 29 the target lacks is not in it). A
    window reaching outside the record, or with more than max(1, 2%) of its
    days missing, is dropped; a missing day it keeps reads 0."""
    if not days:
        return [[]]
    years = sorted({int(k[:4]) for k in hist})
    if not years:
        return []
    segs = []
    for back in range(1, days[-1].year - years[0] + 1):
        vals: List[float] = []
        missing = 0
        for d in days:
            try:
                hd = date(d.year - back, d.month, d.day)
            except ValueError:
                hd = date(d.year - back, d.month, 28)
            k = hd.isoformat()
            if k not in hist:
                break
            v = hist[k]
            if v is None:
                missing += 1
                v = 0.0
            vals.append(v)
        if len(vals) == len(days) and missing <= max(1, len(days) // 50):
            segs.append(vals)
    return segs


def period_to_date(key: str, start: date, end: date, now: datetime,
                   last_wet_at: Optional[str] = None,
                   hist: Optional[Dict[str, Optional[float]]] = None) -> dict:
    """Precipitation already in the period: IEM's daily summaries for its
    past days (`hist`, the ACIS record, fills a day IEM lacks), plus today's
    running total while today is in it. Stale when more than
    PERIOD_MAX_MISSING_DAYS past days stay unread, or when the day of the
    station's last wet observation is unread (or is today, with no running
    total) -- rain known to have fallen that the total cannot count. An
    unread day after it saw no wet observation."""
    tz = pytz.timezone(rm.STATIONS[key]["tz"])
    today = now.astimezone(tz).date()
    out = {"obs": 0.0, "through": None, "stale": False, "missing": [],
           "pday": None, "notes": [], "in_period": start <= today <= end}
    if today < start:
        out["notes"].append("period not started")
        return out
    wet_day = None
    if last_wet_at:
        try:
            wet_day = datetime.fromisoformat(
                str(last_wet_at).replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            wet_day = today          # unreadable: assume it rained today
    last_past = min(end, today - timedelta(days=1))
    daily: Dict[str, float] = {}
    y, m = start.year, start.month
    while last_past >= start and (y, m) <= (last_past.year, last_past.month):
        daily.update(rm.fetch_iem_daily(key, y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    obs = 0.0
    d = start
    while d <= last_past:
        v = daily.get(d.isoformat())
        if v is None and hist is not None:
            v = hist.get(d.isoformat())
        if v is None:
            out["missing"].append(d.isoformat())
        else:
            obs += v
        d += timedelta(days=1)
    if last_past >= start:
        out["through"] = last_past.isoformat()
    if out["missing"]:
        out["notes"].append(f"{len(out['missing'])} past day(s) unread, counted 0")
        if len(out["missing"]) > PERIOD_MAX_MISSING_DAYS or (
                wet_day is not None and wet_day.isoformat() in out["missing"]):
            out["stale"] = True
    if out["in_period"]:
        pday, _t = rm.fetch_pday(key)
        out["pday"] = pday
        if pday is not None:
            obs += pday
        elif wet_day is not None and wet_day >= today:
            out["stale"] = True
            out["notes"].append("wet today but no running total")
    out["obs"] = round(obs, 3)
    return out


def simulate_period(key: str, start: date, end: date, now: datetime,
                    n_samples: int = N_SAMPLES,
                    rng: Optional[random.Random] = None,
                    hist: Optional[Dict[str, Optional[float]]] = None,
                    element: str = "quantitativePrecipitation",
                    kernel_sigma: Optional[float] = None) -> dict:
    """Monte Carlo of the rest of the period (inches): today's remaining
    hours on the forecast while today is in it; every later day of the
    period from one whole historical window, each forecast-horizon day
    replaced w.p. rm.FORECAST_WEIGHT by a forecast draw (the shared regime
    multiplier correlates them), and the window's historical part scaled by
    a mean-one log-normal kernel (PERIOD_KERNEL_SIGMA). The snow gate
    passes its snowfall history, element "snowfallAmount" and its own
    kernel."""
    rng = rng or random.Random()
    tz = pytz.timezone(rm.STATIONS[key]["tz"])
    today = now.astimezone(tz).date()
    if today > end:
        raise ValueError("period is over")
    first = max(start, today + timedelta(days=1))
    days = [first + timedelta(days=i) for i in range((end - first).days + 1)]
    hist = hist if hist is not None else period_history(key, now)
    segs = period_segments(hist, days)
    if not segs:
        raise ValueError("no history window covers the period")
    fc = rm.fetch_forecast_days(key, now, element=element)

    def fc_for(day_iso: str) -> Optional[dict]:
        f = fc.get(day_iso)
        return f if f and f.get("complete") else None

    fc_today = fc_for(today.isoformat()) if start <= today <= end else None
    day_fc = [fc_for(d.isoformat()) for d in days]
    fc_idx = [i for i, f in enumerate(day_fc) if f is not None]
    in_fc = set(fc_idx)
    base = [sum(v for i, v in enumerate(s) if i not in in_fc) for s in segs]
    sig = PERIOD_KERNEL_SIGMA if kernel_sigma is None else kernel_sigma
    totals = []
    for _ in range(n_samples):
        regime = math.exp(rng.gauss(0.0, rm.REGIME_SIGMA))
        total = rm._draw_day(fc_today, regime, rng) if fc_today is not None else 0.0
        j = rng.randrange(len(segs))
        past = base[j]
        for i in fc_idx:
            if rng.random() < rm.FORECAST_WEIGHT:
                total += rm._draw_day(day_fc[i], regime, rng)
            else:
                past += segs[j][i]
        if sig > 0 and past > 0:
            past *= math.exp(rng.gauss(-0.5 * sig * sig, sig))
        totals.append(total + past)
    totals.sort()
    return {"totals": totals, "n_segments": len(segs) if days else 0,
            "horizon_days": len(fc_idx), "fc_today": fc_today}


def period_fair(event: str, info: dict, now: datetime,
                n_samples: int = N_SAMPLES,
                rng: Optional[random.Random] = None) -> Optional[dict]:
    """event_fair for a period event (info carries "start"/"end"); None
    when the station is unknown or the period is over."""
    spec = CLI_STATIONS.get(info.get("code") or "")
    try:
        start = date.fromisoformat(str(info["start"]))
        end = date.fromisoformat(str(info["end"]))
    except (KeyError, ValueError):
        return None
    if spec is None or not info.get("strikes"):
        return None
    if now.astimezone(pytz.timezone(spec["tz"])).date() > end:
        return None
    key = f"TWC{info['code']}"
    rm.STATIONS[key] = dict(spec, series=info.get("series", ""))
    hist = period_history(key, now)
    td = period_to_date(key, start, end, now, info.get("last_wet_at"), hist=hist)
    sim = simulate_period(key, start, end, now, n_samples=n_samples,
                          rng=rng or random.Random(), hist=hist)
    strikes = info["strikes"]
    p = rm.price_rungs(td["obs"], sim["totals"], sorted(set(strikes.values())))
    return {"p": {t: round(p[k], 4) for t, k in strikes.items()},
            "boundary": sorted(t for t, k in strikes.items()
                               if abs(td["obs"] - k) < BOUNDARY_IN),
            "mtd": td["obs"], "cli_mtd": None, "cli_date": td["through"],
            "stale_cli": td["stale"], "pday": td["pday"], "notes": td["notes"],
            "horizon_days": sim["horizon_days"], "n_segments": sim["n_segments"],
            "in_period": td["in_period"], "missing_days": td["missing"]}


def event_fair(event: str, info: dict, now: datetime,
               n_samples: int = N_SAMPLES,
               rng: Optional[random.Random] = None) -> Optional[dict]:
    """Per-market fair for one event and its month-to-date state; None when
    the station or the month is unknown (the gate then stands aside). A
    period event (info "start"/"end") goes to period_fair."""
    if info.get("start"):
        return period_fair(event, info, now, n_samples=n_samples, rng=rng)
    code = info.get("code")
    spec = CLI_STATIONS.get(code or "")
    ym = rm.parse_event_month(event)
    if spec is None or ym is None or not info.get("strikes"):
        return None
    key = f"CLI{code}"
    rm.STATIONS[key] = dict(spec, series=info.get("series", ""))
    year, month = ym
    mtd = rm.effective_mtd(key, year, month, now)
    if mtd.get("mtd") is None:
        return None
    sim = rm.simulate_remaining(key, year, month, now, n_samples=n_samples,
                                rng=rng or random.Random())
    strikes = info["strikes"]
    p = rm.price_rungs(float(mtd["mtd"]), sim["totals"], sorted(set(strikes.values())))
    return {"p": {t: round(p[k], 4) for t, k in strikes.items()},
            "boundary": sorted(t for t, k in strikes.items()
                               if abs(float(mtd["mtd"]) - k) < BOUNDARY_IN),
            "mtd": mtd["mtd"], "cli_mtd": mtd.get("cli_mtd"),
            "cli_date": mtd.get("cli_date"), "stale_cli": bool(mtd.get("stale")),
            "pday": mtd.get("pday"), "notes": mtd.get("notes") or [],
            "horizon_days": sim.get("horizon_days"),
            "n_segments": sim.get("n_segments")}


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
                    fair_fn: Callable = event_fair,
                    obs_fn: Callable = obs_state,
                    period_series: Iterable[str] = ()) -> Tuple[int, int]:
    """Build and atomically write the fair file. Returns (events with a
    fair, events without one). Kalshi's open events are re-read hourly, and
    at once when `period_series` names a series the cached read lacks; a
    failed read reuses the previous file's. The last wet observation per
    event survives across writes (the DRY_MIN cool-down). A period event
    before its first day reads dry without an observation."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    ev_cache = old.get("events") or {}
    events_at = old.get("events_at")
    want = all_series(period_series)
    have = old.get("events_series")
    if events is None:
        # a file from before the period families (no events_series) is
        # re-read at once
        if ev_cache and ts - float(events_at or 0) < EVENTS_TTL_SECS \
                and have is not None and set(want) <= set(have):
            events = ev_cache
            want = tuple(have)
        else:
            try:
                events = fetch_events(get_json, period_series)
                events_at = ts
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); "
                     f"reusing {len(ev_cache)} cached")
                events = ev_cache
                want = tuple(have) if have is not None else want
    old_ev = old.get("event_state") or {}
    markets: Dict[str, dict] = {}
    state: Dict[str, dict] = {}
    missing: List[str] = []
    for ev, info in sorted(events.items()):
        spec = CLI_STATIONS.get(info.get("code") or "")
        st = {"code": info.get("code"), "fetched_at": now.isoformat(),
              "last_wet_at": (old_ev.get(ev) or {}).get("last_wet_at")}
        before = False
        if info.get("start"):
            st.update(start=info["start"], end=info.get("end"))
            if spec is not None:
                before = (now.astimezone(pytz.timezone(spec["tz"])).date().isoformat()
                          < str(info["start"]))
        if before:
            # rain before the period counts for nothing: no stand-aside
            st.update(wet=False, obs_time=None, phour=None, wx=None)
        elif spec is not None:
            ob = obs_fn(spec, now)
            st.update(wet=ob["wet"], obs_time=ob["obs_time"], phour=ob["phour"],
                      wx=ob["wx"])
            if ob["wet"]:
                st["last_wet_at"] = ob["obs_time"]
        try:
            f = fair_fn(ev, dict(info, last_wet_at=st["last_wet_at"]), now) \
                if spec is not None else None
        except Exception as e:
            _log(f"! {ev}: {type(e).__name__}: {str(e)[:120]}")
            f = None
        if f is None:
            missing.append(ev)
            st["error"] = "no station" if spec is None else "no fair"
            state[ev] = st
            continue
        st.update({k: f[k] for k in ("mtd", "cli_mtd", "cli_date", "stale_cli",
                                     "pday", "notes", "horizon_days", "n_segments")})
        st.update({k: f[k] for k in ("in_period", "missing_days") if k in f})
        state[ev] = st
        for t, p in f["p"].items():
            markets[t] = {"event": ev, "p": p, "boundary": t in f["boundary"],
                          "fetched_at": now.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "markets": markets,
                   "event_state": state, "missing": missing, "events": events,
                   "events_at": events_at, "events_series": list(want),
                   "model": {"samples": N_SAMPLES, "obs_max_age_min": OBS_MAX_AGE_MIN,
                             "boundary_in": BOUNDARY_IN,
                             "forecast_weight": rm.FORECAST_WEIGHT,
                             "stale_cli_hours": rm.STALE_CLI_HOURS,
                             "period_kernel_sigma": PERIOD_KERNEL_SIGMA,
                             "period_max_missing_days": PERIOD_MAX_MISSING_DAYS}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(events) - len(missing), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "rain_monthly_fair.json"))
    ap.add_argument("--period-series", default="",
                    help="extra period-total series, comma separated")
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out, period_series=[
        s.strip() for s in args.period_series.split(",") if s.strip()])
    _log(f"{ok} events with a fair, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
