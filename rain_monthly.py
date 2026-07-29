#!/usr/bin/env python3
"""Monthly cumulative-rain ladder model + bot for KXRAIN<CITY>M (strategy 4).

Each rung of a monthly ladder resolves YES when the settlement station's NWS
CLI monthly precipitation total STRICTLY EXCEEDS the strike (early close on
cross — cumulative precip is monotone, so the ladder is structurally a
one-touch that can only ratchet up). This bot prices every open rung as

    P(cross) = P( MTD_effective + Remaining > strike )

MTD_effective = latest CLI month-to-date
              + IEM daily obs for days after the CLI report date
              + today's obs-to-now (pday), double-count-guarded when the CLI
                already covers part of today (evening reports cover ~to 4pm).

Remaining     = Monte Carlo: sample a historical year's calendar-aligned
                remaining-days segment (autocorrelation + seasonality for
                free), then inject the NWS gridpoint forecast over its
                horizon: each in-horizon day is replaced, with probability
                RMM_FORECAST_WEIGHT, by a forecast-conditional draw
                (wet ~ Bernoulli(PoP), amount ~ Gamma with mean QPF/PoP),
                with a shared per-scenario lognormal regime multiplier so a
                big synoptic system moves all horizon days together.

VERIFIED STATION MAPPING (2026-07-28, from market rules + ACIS + a live
cross-check of CLI MTD against Kalshi's settled rungs — 9/9 consistent):
monthlies differ from the KXRAIN dailies for two cities — CHI settles at
MIDWAY (CLIMDW, not O'Hare) and HOU at HOBBY (CLIHOU, not IAH).

Trading (DRY-RUN BY DEFAULT — prints/records recommendations only):
  python rain_monthly.py                 # scan + table + JSON snapshot
  python rain_monthly.py --live         # ALSO place capped taker orders
  python rain_monthly.py --city MIA --samples 50000   # focused run
Order safety: taker-only limit orders at the current touch with a 90s
exchange-side expiration, client id prefix rmm-, per-order/market/city/run
caps, BOUNDARY guard (skip rungs within RMM_BOUNDARY_IN of MTD where obs
vs CLI rounding could differ), stale-CLI guard, and net positions from the
account (shared with IMM's frozen rain inventory + manual trades) count
against the caps. NO-side fills lock collateral to month-end settlement —
that frozen-inventory cost is why RMM_MIN_EDGE_NO > RMM_MIN_EDGE_YES.
"""

import argparse
import base64
import calendar
import json
import math
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pytz
import requests

UA = {"User-Agent": "KL rain monthly model (jackdu224@gmail.com)"}


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


# ---------------------------------------------------------------------------
# Stations (verified 2026-07-28; see module docstring). iem = IEM ASOS id.
# ---------------------------------------------------------------------------
STATIONS: Dict[str, dict] = {
    "AUS": {"series": "KXRAINAUSM", "icao": "KAUS", "iem": "AUS", "net": "TX_ASOS",
            "name": "Austin Bergstrom", "lat": 30.183, "lon": -97.680, "tz": "US/Central"},
    "CHI": {"series": "KXRAINCHIM", "icao": "KMDW", "iem": "MDW", "net": "IL_ASOS",
            "name": "Chicago MIDWAY", "lat": 41.786, "lon": -87.752, "tz": "US/Central"},
    "DAL": {"series": "KXRAINDALM", "icao": "KDFW", "iem": "DFW", "net": "TX_ASOS",
            "name": "Dallas-Fort Worth", "lat": 32.898, "lon": -97.019, "tz": "US/Central"},
    "DEN": {"series": "KXRAINDENM", "icao": "KDEN", "iem": "DEN", "net": "CO_ASOS",
            "name": "Denver Intl", "lat": 39.847, "lon": -104.656, "tz": "US/Mountain"},
    "HOU": {"series": "KXRAINHOUM", "icao": "KHOU", "iem": "HOU", "net": "TX_ASOS",
            "name": "Houston HOBBY", "lat": 29.646, "lon": -95.277, "tz": "US/Central"},
    "MIA": {"series": "KXRAINMIAM", "icao": "KMIA", "iem": "MIA", "net": "FL_ASOS",
            "name": "Miami Intl", "lat": 25.788, "lon": -80.317, "tz": "US/Eastern"},
    "NYC": {"series": "KXRAINNYCM", "icao": "KNYC", "iem": "NYC", "net": "NY_ASOS",
            "name": "NY Central Park", "lat": 40.779, "lon": -73.969, "tz": "US/Eastern"},
    "SEA": {"series": "KXRAINSEAM", "icao": "KSEA", "iem": "SEA", "net": "WA_ASOS",
            "name": "Seattle-Tacoma", "lat": 47.444, "lon": -122.314, "tz": "US/Pacific"},
    "STP": {"series": "KXRAINSTPM", "icao": "KSPG", "iem": "SPG", "net": "FL_ASOS",
            "name": "St Pete Whitted", "lat": 27.765, "lon": -82.627, "tz": "US/Eastern"},
}
SERIES_TO_CITY = {v["series"]: k for k, v in STATIONS.items()}

STATE_DIR = os.environ.get(
    "RMM_STATE_DIR", r"C:\Users\jackd\Documents\KL\run-logs\rain-monthly")
CACHE_DIR = os.path.join(STATE_DIR, "cache")

# Model knobs
N_SAMPLES = _env_int("RMM_SAMPLES", 20000)
HIST_YEARS = _env_int("RMM_HIST_YEARS", 46)          # ACIS window (1980+)
FORECAST_WEIGHT = _env_float("RMM_FORECAST_WEIGHT", 0.7)  # P(horizon day uses
#   the forecast draw instead of the historical segment's day) — hedges
#   deterministic-QPF underdispersion with climatological variety
GAMMA_SHAPE = _env_float("RMM_GAMMA_SHAPE", 0.8)     # daily wet-amount shape
REGIME_SIGMA = _env_float("RMM_REGIME_SIGMA", 0.35)  # lognormal on forecast
#   amounts, shared across a scenario's horizon days (synoptic correlation)
EPS = 1e-6

# Trading knobs (all conservative; Jack tunes)
MIN_EDGE_YES = _env_float("RMM_MIN_EDGE_YES", 6.0)   # cents net of fee
MIN_EDGE_NO = _env_float("RMM_MIN_EDGE_NO", 9.0)     # NO locks collateral to
#   month-end (the IMM frozen-inventory lesson) -> demand more edge
MAX_ORDER = _env_int("RMM_MAX_ORDER", 10)            # contracts per order
MAX_POS = _env_float("RMM_MAX_POS", 50)              # |net| per market incl. existing
MAX_RUN_COLLATERAL = _env_float("RMM_MAX_RUN_COLLATERAL", 250.0)   # $ new per run
MAX_CITY_COLLATERAL = _env_float("RMM_MAX_CITY_COLLATERAL", 100.0)  # $ new per run
MAX_SPREAD = _env_int("RMM_MAX_SPREAD", 15)          # skip books wider than this
BOUNDARY_IN = _env_float("RMM_BOUNDARY_IN", 0.05)    # |MTD-strike| guard, inches
STALE_CLI_HOURS = _env_float("RMM_STALE_CLI_HOURS", 40.0)
ORDER_EXPIRE_SECS = _env_int("RMM_ORDER_EXPIRE_SECS", 90)

KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Small disk cache for the weather feeds (NOT for Kalshi market data)
# ---------------------------------------------------------------------------
def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".json")


def cached_get_json(key: str, ttl_secs: float, fetch) -> dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(key)
    try:
        if time.time() - os.path.getmtime(path) < ttl_secs:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    data = fetch()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def http_json(url: str, post_body: Optional[dict] = None, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            if post_body is not None:
                r = requests.post(url, json=post_body, headers=UA, timeout=30)
            else:
                r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:            # noqa: BLE001 - caller decides
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def parse_precip(v) -> Optional[float]:
    """IEM/ACIS precip value -> inches. 'T'/0.0001 trace -> 0.0 (the CLI
    monthly total counts trace as zero). 'M'/None -> None (missing)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().rstrip("A")         # ACIS accumulation flag
        if s in ("T", "t"):
            return 0.0
        if s in ("M", "m", ""):
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    v = float(v)
    if 0 < v < 0.005:                     # IEM trace marker 0.0001
        return 0.0
    return v


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
def fetch_cli_state(city: str, year: int, month: int) -> dict:
    """Latest CLI report for the station in (year, month): month-to-date
    precip, the report's date, and that date's daily precip (for the
    obs double-count guard)."""
    st = STATIONS[city]
    data = cached_get_json(
        f"cli_{city}_{year}", 1800,
        lambda: http_json(f"https://mesonet.agron.iastate.edu/json/cli.py"
                          f"?station={st['icao']}&year={year}"))
    rows = [r for r in (data.get("results") or [])
            if str(r.get("valid", "")).startswith(f"{year}-{month:02d}")]
    if not rows:
        return {"mtd": None, "date": None, "day_precip": None}
    last = rows[-1]                        # IEM returns chronological order
    return {"mtd": parse_precip(last.get("precip_month")),
            "date": str(last.get("valid")),
            "day_precip": parse_precip(last.get("precip"))}


def fetch_iem_daily(city: str, year: int, month: int) -> Dict[str, float]:
    """IEM ASOS daily summaries {YYYY-MM-DD: precip inches} — used for days
    after the latest CLI report (report cadence varies by station)."""
    st = STATIONS[city]
    data = cached_get_json(
        f"daily_{city}_{year}_{month:02d}", 1800,
        lambda: http_json(f"https://mesonet.agron.iastate.edu/api/1/daily.json"
                          f"?station={st['iem']}&network={st['net']}"
                          f"&year={year}&month={month}"))
    out = {}
    for r in data.get("data") or []:
        p = parse_precip(r.get("precip"))
        if p is not None and r.get("date"):
            out[str(r["date"])] = p
    return out


def fetch_pday(city: str) -> Tuple[Optional[float], Optional[str]]:
    """Today's obs-to-now precip (local day) from IEM currents."""
    st = STATIONS[city]
    try:
        data = http_json(f"https://mesonet.agron.iastate.edu/api/1/currents.json"
                         f"?station={st['iem']}&network={st['net']}")
        row = (data.get("data") or [{}])[0]
        return parse_precip(row.get("pday")), \
            str(row.get("local_valid") or row.get("utc_valid") or "")
    except RuntimeError:
        return None, None


def fetch_history(city: str, month: int) -> Dict[int, List[Optional[float]]]:
    """ACIS daily precip for `month` across HIST_YEARS: {year: [day1..dayN]}.
    Trace -> 0.0, missing -> None (year filtering happens at segment build)."""
    st = STATIONS[city]
    end_year = datetime.now(timezone.utc).year - 1
    start_year = end_year - HIST_YEARS + 1
    ndays = calendar.monthrange(2001, month)[1]      # non-leap default; Feb
    data = cached_get_json(
        f"acis_{city}_{month:02d}_{start_year}_{end_year}", 7 * 86400,
        lambda: http_json("https://data.rcc-acis.org/StnData", post_body={
            "sid": st["icao"],
            "sdate": f"{start_year}-{month:02d}-01",
            "edate": f"{end_year}-{month:02d}-{ndays}",
            "elems": [{"name": "pcpn"}]}))
    hist: Dict[int, List[Optional[float]]] = {}
    for date_s, val in data.get("data") or []:
        y, m, d = int(date_s[:4]), int(date_s[5:7]), int(date_s[8:10])
        if m != month:
            continue
        days_in = calendar.monthrange(y, month)[1]
        hist.setdefault(y, [None] * days_in)
        hist[y][d - 1] = parse_precip(val)
    return hist


def fetch_forecast_days(city: str, now_utc: datetime) -> Dict[str, dict]:
    """NWS gridpoint QPF+PoP aggregated to LOCAL days:
    {YYYY-MM-DD: {qpf_in, pop, complete}} — for today only periods that end
    after `now` count (rain already fallen is in the obs, not the forecast).
    `complete` False on the horizon's ragged last day."""
    st = STATIONS[city]
    tz = pytz.timezone(st["tz"])

    def _fetch():
        pts = http_json(f"https://api.weather.gov/points/{st['lat']:.4f},{st['lon']:.4f}")
        grid_url = pts["properties"]["forecastGridData"]
        g = http_json(grid_url)
        return {"qpf": g["properties"].get("quantitativePrecipitation", {}),
                "pop": g["properties"].get("probabilityOfPrecipitation", {})}

    data = cached_get_json(f"qpf_{city}", 3600, _fetch)

    def _periods(block):
        out = []
        for v in (block.get("values") or []):
            try:
                t0_s, dur_s = v["validTime"].split("/")
                t0 = datetime.fromisoformat(t0_s)
                hours = _iso_duration_hours(dur_s)
                out.append((t0, hours, v.get("value")))
            except (KeyError, ValueError):
                continue
        return out

    days: Dict[str, dict] = {}
    for t0, hours, val in _periods(data["qpf"]):
        mm = float(val or 0.0)
        # split the period across local days proportionally
        for t_loc, frac in _split_local_days(t0, hours, tz, now_utc):
            d = days.setdefault(t_loc, {"qpf_in": 0.0, "pop": 0.0, "hours": 0.0})
            d["qpf_in"] += (mm / 25.4) * frac
            d["hours"] += hours * frac
    for t0, hours, val in _periods(data["pop"]):
        p = float(val or 0.0) / 100.0
        for t_loc, frac in _split_local_days(t0, hours, tz, now_utc):
            if t_loc in days and frac > 0:
                days[t_loc]["pop"] = max(days[t_loc]["pop"], p)
    for d, v in days.items():
        # a deterministic QPF with a tiny PoP is incoherent; floor it
        if v["qpf_in"] >= 0.01 and v["pop"] < 0.2:
            v["pop"] = 0.2
        v["complete"] = v["hours"] >= 18.0 or d == _local_date_iso(now_utc, tz)
    return days


def _iso_duration_hours(dur: str) -> float:
    """'PT6H' / 'PT4H' / 'P1DT6H' -> hours."""
    h = 0.0
    dur = dur.upper().lstrip("P")
    if "D" in dur.split("T")[0]:
        h += 24.0 * float(dur.split("D")[0])
    if "T" in dur:
        t = dur.split("T")[1]
        if "H" in t:
            h += float(t.split("H")[0])
    return h or 1.0


def _split_local_days(t0: datetime, hours: float, tz, now_utc: datetime):
    """Yield (local_date_iso, fraction_of_period) with the pre-now part of
    the period dropped (that rain is observation territory, not forecast)."""
    t0 = t0.astimezone(timezone.utc)
    t1 = t0 + timedelta(hours=hours)
    if t1 <= now_utc:
        return
    if t0 < now_utc:                       # drop the already-elapsed part
        frac_live = (t1 - now_utc).total_seconds() / (t1 - t0).total_seconds()
        t0 = now_utc
    else:
        frac_live = 1.0
    # assign by hour to local dates
    total_h = max((t1 - t0).total_seconds() / 3600.0, 1e-9)
    cur = t0
    while cur < t1:
        nxt = min(t1, (cur.astimezone(tz).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
                       + timedelta(days=1)).astimezone(timezone.utc))
        frac = (nxt - cur).total_seconds() / 3600.0 / total_h * frac_live
        yield _local_date_iso(cur, tz), frac
        cur = nxt


def _local_date_iso(dt_utc: datetime, tz) -> str:
    return dt_utc.astimezone(tz).date().isoformat()


# ---------------------------------------------------------------------------
# Effective month-to-date
# ---------------------------------------------------------------------------
def effective_mtd(city: str, year: int, month: int, now_utc: datetime) -> dict:
    """CLI MTD + obs for days the CLI hasn't covered yet. Returns dict with
    mtd, cli fields, obs adjustments and staleness/boundary metadata."""
    tz = pytz.timezone(STATIONS[city]["tz"])
    today_iso = _local_date_iso(now_utc, tz)
    cli = fetch_cli_state(city, year, month)
    out = {"city": city, "cli_mtd": cli["mtd"], "cli_date": cli["date"],
           "obs_added": 0.0, "mtd": cli["mtd"], "stale": False, "notes": []}
    if cli["mtd"] is None:
        # No CLI yet this month (fresh month) -> build purely from obs
        base, cli_date = 0.0, f"{year}-{month:02d}-00"
        out["notes"].append("no CLI report yet this month; obs-only MTD")
    else:
        base, cli_date = cli["mtd"], cli["date"]
    daily = fetch_iem_daily(city, year, month)
    added = 0.0
    d = cli_date
    # gap days strictly after the CLI report date, before today
    for day_iso in sorted(daily):
        if day_iso > d[:10] and day_iso < today_iso:
            added += daily[day_iso]
    pday, obs_time = fetch_pday(city)
    if today_iso.startswith(f"{year}-{month:02d}"):
        if pday is not None:
            if cli["date"] == today_iso:
                # evening CLI covers part of today; only rain beyond what the
                # report already counted is new
                already = cli["day_precip"] or 0.0
                added += max(0.0, pday - already)
            else:
                added += pday
        elif obs_time is None:
            out["notes"].append("pday unavailable; today's rain missing from MTD")
    out["obs_added"] = round(added, 3)
    out["mtd"] = round(base + added, 3)
    out["pday"] = pday
    out["obs_time"] = obs_time
    if cli["mtd"] is not None:
        try:
            age_h = (now_utc - tz.localize(
                datetime.strptime(cli["date"], "%Y-%m-%d")).astimezone(
                    timezone.utc)).total_seconds() / 3600.0
            out["stale"] = age_h > STALE_CLI_HOURS
        except (ValueError, TypeError):
            out["stale"] = True
    return out


# ---------------------------------------------------------------------------
# Remaining-month simulation
# ---------------------------------------------------------------------------
def build_segments(hist: Dict[int, List[Optional[float]]], start_day: int,
                   ndays: int) -> List[List[float]]:
    """Historical remaining-days segments [start_day..ndays], per year.
    Years with >1 missing day in the window are dropped; a single missing
    day becomes 0.0 (bias small vs dropping wet years wholesale)."""
    segs = []
    for _y, days in sorted(hist.items()):
        window = days[start_day - 1:ndays]
        if len(window) < ndays - start_day + 1:
            continue
        missing = sum(1 for v in window if v is None)
        if missing > 1:
            continue
        segs.append([v if v is not None else 0.0 for v in window])
    return segs


def simulate_remaining(city: str, year: int, month: int, now_utc: datetime,
                       n_samples: int = N_SAMPLES,
                       rng: Optional[random.Random] = None) -> dict:
    """Monte Carlo distribution of precip from NOW to month end (inches).
    Today's remaining hours ride on the forecast only; subsequent days are
    historical segments with forecast injection inside the QPF horizon."""
    rng = rng or random.Random()
    tz = pytz.timezone(STATIONS[city]["tz"])
    today = now_utc.astimezone(tz).date()
    ndays = calendar.monthrange(year, month)[1]
    first_hist_day = None
    if (today.year, today.month) == (year, month):
        first_hist_day = today.day + 1          # tomorrow onward from history
    elif (year, month) > (today.year, today.month):
        first_hist_day = 1                      # future month: whole month
    else:
        raise ValueError("month is in the past")

    hist = fetch_history(city, month)
    segs = build_segments(hist, first_hist_day, ndays) if first_hist_day <= ndays else [[]]
    if not segs:
        segs = [[]]
    fc = fetch_forecast_days(city, now_utc)

    def fc_for(day_iso):
        f = fc.get(day_iso)
        if not f or not f.get("complete"):
            return None
        return f

    # today's remaining-hours forecast (only when pricing the current month)
    today_iso = today.isoformat()
    fc_today = fc_for(today_iso) if first_hist_day and \
        (today.year, today.month) == (year, month) else None

    day_isos = [f"{year}-{month:02d}-{d:02d}" for d in range(first_hist_day, ndays + 1)]
    day_fc = [fc_for(di) for di in day_isos]

    totals = []
    for _ in range(n_samples):
        total = 0.0
        regime = math.exp(rng.gauss(0.0, REGIME_SIGMA))
        if fc_today is not None:
            total += _draw_day(fc_today, regime, rng)
        seg = segs[rng.randrange(len(segs))]
        for i, base_val in enumerate(seg):
            f = day_fc[i] if i < len(day_fc) else None
            if f is not None and rng.random() < FORECAST_WEIGHT:
                total += _draw_day(f, regime, rng)
            else:
                total += base_val
        totals.append(total)
    totals.sort()
    return {"totals": totals, "n_segments": len([s for s in segs if s]),
            "horizon_days": sum(1 for f in day_fc if f is not None),
            "fc_today": fc_today}


def _draw_day(f: dict, regime: float, rng: random.Random) -> float:
    pop = min(max(f["pop"], 0.0), 0.99)
    if rng.random() >= pop:
        return 0.0
    mean_wet = max(f["qpf_in"], 0.002) / max(pop, 0.05)
    scale = mean_wet / GAMMA_SHAPE
    return rng.gammavariate(GAMMA_SHAPE, scale) * regime


def price_rungs(mtd: float, totals: List[float], strikes: List[float]) -> Dict[float, float]:
    """P(final > strike) per strike from sorted remaining-total samples."""
    import bisect
    n = len(totals)
    out = {}
    for x in strikes:
        need = x - mtd + EPS
        if need <= 0:
            out[x] = 1.0
        elif n == 0:
            out[x] = 0.0
        else:
            idx = bisect.bisect_right(totals, need)
            out[x] = (n - idx) / n
    return out


# ---------------------------------------------------------------------------
# Kalshi layer
# ---------------------------------------------------------------------------
def build_client():
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
    path = KEY_PATH if os.path.exists(KEY_PATH) else None
    if path:
        with open(path, "rb") as f:
            pem = f.read()
    else:
        b64 = os.environ.get("KALSHI_PRIVATE_KEY", "")
        pem = base64.b64decode(b64) if b64 else None
        if pem is None:
            raise FileNotFoundError("no Kalshi private key")
    key = serialization.load_pem_private_key(pem, password=None,
                                             backend=default_backend())
    return ExchangeClient(API_BASE, KEY_ID, key)


def d2c(m: dict, k: str) -> Optional[int]:
    v = m.get(k)
    return round(float(v) * 100) if v is not None else None


def fetch_open_markets(client, series: str) -> List[dict]:
    out, cursor = [], None
    while True:
        kw = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            kw["cursor"] = cursor
        r = client.get_markets(**kw)
        out += r.get("markets") or []
        cursor = r.get("cursor")
        if not cursor:
            break
    return out


MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_event_month(event_ticker: str) -> Optional[Tuple[int, int]]:
    """KXRAINMIAM-26JUL -> (2026, 7)."""
    try:
        seg = event_ticker.split("-")[1]
        return 2000 + int(seg[:2]), MONTHS[seg[2:5]]
    except (IndexError, KeyError, ValueError):
        return None


def taker_fee_cents(price_c: int, count: int) -> float:
    """Kalshi quadratic fee, per contract in cents (order-level ceil)."""
    p = price_c / 100.0
    fee_dollars = math.ceil(0.07 * count * p * (1 - p) * 100) / 100.0
    return fee_dollars * 100.0 / max(count, 1)


def account_positions(client, prefix: str = "KXRAIN") -> Dict[str, float]:
    pos, cursor = {}, None
    while True:
        kw = {"limit": 200}
        if cursor:
            kw["cursor"] = cursor
        r = client.get_positions(**kw)
        for p in r.get("market_positions") or []:
            t = p.get("ticker", "")
            if t.startswith(prefix):
                v = p.get("position")
                if v is None:
                    v = p.get("position_fp")
                try:
                    pos[t] = float(v)
                except (TypeError, ValueError):
                    pass
        cursor = r.get("cursor")
        if not cursor:
            break
    return pos


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def evaluate_market(m: dict, p_model: float, mtd: float, stale_cli: bool,
                    positions: Dict[str, float]) -> dict:
    """One rung -> row with model, market, and any actionable edge."""
    t = m["ticker"]
    strike = float(m.get("floor_strike") or 0)
    bid, ask = d2c(m, "yes_bid_dollars"), d2c(m, "yes_ask_dollars")
    bid_sz = float(m.get("yes_bid_size_fp") or 0)
    ask_sz = float(m.get("yes_ask_size_fp") or 0)
    row = {"ticker": t, "strike": strike, "bid": bid, "ask": ask,
           "p": round(p_model, 4), "fair_c": round(p_model * 100, 1),
           "action": None, "size": 0, "why": []}
    net = positions.get(t, 0.0)
    if net:
        row["existing_pos"] = net

    if mtd - strike > EPS:
        # actually crossed (MTD above strike) — settles YES shortly; nothing
        # to trade. A future-month rung can price p~1.0 WITHOUT being
        # crossed (MTD=0) — that must fall through to the edge logic, not
        # get labeled crossed (bug caught on the first live scan: MIA AUG
        # rungs 1-3 wore "crossed" with the month not yet started).
        row["why"].append("crossed")
        return row
    if abs(mtd - strike) < BOUNDARY_IN and mtd <= strike:
        row["why"].append(f"BOUNDARY |mtd-strike|<{BOUNDARY_IN}")
        return row
    if bid is None or ask is None or ask - bid > MAX_SPREAD:
        row["why"].append("wide/empty book")
        return row
    if stale_cli:
        row["why"].append("stale CLI (report only)")
        return row

    fair = p_model * 100.0
    size = MAX_ORDER
    yes_edge = fair - ask - taker_fee_cents(ask, size)
    no_edge = bid - fair - taker_fee_cents(bid, size)
    row["yes_edge"] = round(yes_edge, 1)
    row["no_edge"] = round(no_edge, 1)
    if yes_edge >= MIN_EDGE_YES and ask_sz >= 1:
        n = int(min(size, ask_sz, MAX_POS - net))
        if n >= 1:
            row.update(action="BUY_YES", size=n, px=ask)
    elif no_edge >= MIN_EDGE_NO and bid_sz >= 1:
        n = int(min(size, bid_sz, MAX_POS + net))
        if n >= 1:
            row.update(action="BUY_NO", size=n, px=100 - bid)
    return row


def place_order(client, row: dict) -> Optional[str]:
    """Taker limit at the touch, short exchange-side expiration."""
    side = "yes" if row["action"] == "BUY_YES" else "no"
    kw = {"ticker": row["ticker"], "client_order_id": f"rmm-{uuid.uuid4()}",
          "side": side, "action": "buy", "count": row["size"], "type": "limit",
          "expiration_ts": int(time.time()) + ORDER_EXPIRE_SECS}
    if side == "yes":
        kw["yes_price"] = row["px"]
    else:
        kw["no_price"] = row["px"]
    resp = client.create_order(**kw)
    return ((resp or {}).get("order") or {}).get("order_id")


def append_ledger(rows: List[dict], live: bool) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "rmm_ledger.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("ts,mode,ticker,action,size,px,fair_c,bid,ask,mtd,order_id\n")
        ts = datetime.now(timezone.utc).isoformat()
        for r in rows:
            f.write(f"{ts},{'LIVE' if live else 'DRY'},{r['ticker']},"
                    f"{r['action']},{r['size']},{r.get('px','')},{r['fair_c']},"
                    f"{r['bid']},{r['ask']},{r.get('mtd','')},"
                    f"{r.get('order_id','')}\n")


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
def run_scan(cities: List[str], live: bool, n_samples: int,
             seed: Optional[int] = None) -> dict:
    now_utc = datetime.now(timezone.utc)
    client = build_client()
    positions = account_positions(client)
    rng = random.Random(seed)
    all_rows, orders, run_collateral = [], [], 0.0
    snapshot = {"at": now_utc.isoformat(), "cities": {}}

    for city in cities:
        series = STATIONS[city]["series"]
        try:
            markets = fetch_open_markets(client, series)
        except Exception as e:            # noqa: BLE001
            log(f"! {city}: markets fetch failed: {e}")
            continue
        by_month: Dict[Tuple[int, int], List[dict]] = {}
        for m in markets:
            ym = parse_event_month(m.get("event_ticker", ""))
            if ym:
                by_month.setdefault(ym, []).append(m)

        city_collateral = 0.0
        for (year, month), ms in sorted(by_month.items()):
            try:
                state = effective_mtd(city, year, month, now_utc)
                sim = simulate_remaining(city, year, month, now_utc,
                                         n_samples=n_samples, rng=rng)
            except Exception as e:        # noqa: BLE001
                log(f"! {city} {year}-{month:02d}: model failed: {e}")
                continue
            mtd = state["mtd"] if state["mtd"] is not None else 0.0
            strikes = sorted(float(m.get("floor_strike") or 0) for m in ms)
            probs = price_rungs(mtd, sim["totals"], strikes)
            n = len(sim["totals"])
            q = lambda f: sim["totals"][min(n - 1, int(f * n))] if n else 0.0
            log(f"{city} {year}-{month:02d}: MTD {mtd:.2f}\" "
                f"(CLI {state['cli_mtd']} @ {state['cli_date']}, "
                f"obs +{state['obs_added']:.2f}) | remaining q10/50/90 = "
                f"{q(.1):.2f}/{q(.5):.2f}/{q(.9):.2f}\" | "
                f"{sim['n_segments']} hist yrs, {sim['horizon_days']}d QPF"
                + (" | STALE CLI" if state["stale"] else ""))
            month_rows = []
            for m in sorted(ms, key=lambda x: float(x.get("floor_strike") or 0)):
                row = evaluate_market(m, probs[float(m.get("floor_strike") or 0)],
                                      mtd, state["stale"], positions)
                row["mtd"] = mtd
                month_rows.append(row)
                mark = ""
                if row["action"]:
                    mark = f"  << {row['action']} {row['size']} @ {row['px']}c"
                log(f"   >{row['strike']:>4} {str(row['bid']):>4}x"
                    f"{str(row['ask']):<4} fair {row['fair_c']:>5}c "
                    f"yes_e={row.get('yes_edge', '')} no_e={row.get('no_edge', '')}"
                    f" {'/'.join(row['why'])}{mark}")
            snapshot["cities"].setdefault(city, {})[f"{year}-{month:02d}"] = {
                "state": state, "rows": month_rows,
                "remaining_q": [q(.1), q(.5), q(.9)]}
            for row in month_rows:
                if not row["action"]:
                    continue
                cost = row["px"] / 100.0 * row["size"]
                if run_collateral + cost > MAX_RUN_COLLATERAL:
                    row["why"].append("run collateral cap")
                    row["action"] = None
                    continue
                if city_collateral + cost > MAX_CITY_COLLATERAL:
                    row["why"].append("city collateral cap")
                    row["action"] = None
                    continue
                run_collateral += cost
                city_collateral += cost
                if live:
                    try:
                        oid = place_order(client, row)
                        row["order_id"] = oid
                        log(f"   LIVE order {row['action']} {row['size']} "
                            f"{row['ticker']} @ {row['px']}c -> {oid}")
                    except Exception as e:  # noqa: BLE001
                        log(f"   ! order failed {row['ticker']}: {e}")
                orders.append(row)
            all_rows += month_rows

    if orders:
        append_ledger(orders, live)
    os.makedirs(STATE_DIR, exist_ok=True)
    snap_path = os.path.join(
        STATE_DIR, f"scan_{now_utc.strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=1, default=str)
    log(f"scan complete: {len(all_rows)} rungs, {len(orders)} action(s) "
        f"({'LIVE' if live else 'DRY'}), ${run_collateral:.0f} collateral "
        f"-> {snap_path}")
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--city", action="append",
                    help="city code (repeatable); default all")
    ap.add_argument("--live", action="store_true",
                    help="place capped taker orders (default: dry-run)")
    ap.add_argument("--samples", type=int, default=N_SAMPLES)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    cities = args.city or list(STATIONS)
    bad = [c for c in cities if c not in STATIONS]
    if bad:
        print(f"unknown cities: {bad}; valid: {list(STATIONS)}")
        return 2
    if args.live:
        log("=== LIVE MODE: orders will be placed (caps: "
            f"order {MAX_ORDER}, pos {MAX_POS}, run ${MAX_RUN_COLLATERAL}, "
            f"city ${MAX_CITY_COLLATERAL}) ===")
    run_scan(cities, live=args.live, n_samples=args.samples, seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
