#!/usr/bin/env python3
"""NWS-forecast fair values for the KXRAIN daily city markets.

Each KXRAIN daily resolves YES when the settlement station's NWS daily
climate report (CLI) shows total precipitation strictly greater than 0
inches — i.e. measurable (>=0.01") rain on the local calendar day. The NWS
hourly forecast publishes probabilityOfPrecipitation per hour at the exact
settlement stations, so a calibrated day probability is directly buildable:

    p_day = max( max_h(p_h),  1 - prod_h (1 - p_h)^W )     clamped to [2%, 98%]

The exponent W (RAIN_FAIR_HOURLY_EXP, default 0.5) haircuts the
independence assumption — hourly PoPs are strongly correlated within a
rain event, so the raw complement product badly overshoots (six hours of
30% is nowhere near 88%). For TODAY's market only the remaining hours
count (rain that already fell is invisible here — the book reprices to
95+ on its own and incentive_mm's band then stands the market down).

Written for incentive_mm.py's rain fair-value anchor: a daemon thread in
the bot calls write_fair_file() every IMM_RAIN_FAIR_REFRESH_MIN minutes;
the bot hot-reloads the JSON by mtime. Also runnable standalone:

    python rain_fair.py [--out rain_fair_values.json] [--days 3]

Station set = the 20 CLI stations named in the KXRAIN market rules
(fetched 2026-07-28; note CHI=O'Hare, DAL=DFW, HOU=IAH, NYC=Central Park).
Per-station failures keep the previous entry (with its old fetched_at, so
the bot's TTL naturally expires it) — one flaky NWS endpoint must not
blank the other 19 cities.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pytz
import requests

# Market-ticker city suffix -> settlement station (CLI product, coords, tz).
STATIONS: Dict[str, dict] = {
    "NYC":  {"cli": "CLINYC", "name": "New York Central Park", "lat": 40.779, "lon": -73.969, "tz": "US/Eastern"},
    "CHI":  {"cli": "CLIORD", "name": "Chicago O'Hare",        "lat": 41.979, "lon": -87.905, "tz": "US/Central"},
    "AUS":  {"cli": "CLIAUS", "name": "Austin Bergstrom",      "lat": 30.183, "lon": -97.680, "tz": "US/Central"},
    "MIA":  {"cli": "CLIMIA", "name": "Miami Intl",            "lat": 25.788, "lon": -80.317, "tz": "US/Eastern"},
    "DEN":  {"cli": "CLIDEN", "name": "Denver Intl",           "lat": 39.847, "lon": -104.656, "tz": "US/Mountain"},
    "PHIL": {"cli": "CLIPHL", "name": "Philadelphia Intl",     "lat": 39.868, "lon": -75.231, "tz": "US/Eastern"},
    "LAX":  {"cli": "CLILAX", "name": "Los Angeles Intl",      "lat": 33.938, "lon": -118.389, "tz": "US/Pacific"},
    "LV":   {"cli": "CLILAS", "name": "Las Vegas Harry Reid",  "lat": 36.072, "lon": -115.163, "tz": "US/Pacific"},
    "NOLA": {"cli": "CLIMSY", "name": "New Orleans Intl",      "lat": 29.993, "lon": -90.251, "tz": "US/Central"},
    "SFO":  {"cli": "CLISFO", "name": "San Francisco Intl",    "lat": 37.620, "lon": -122.365, "tz": "US/Pacific"},
    "DC":   {"cli": "CLIDCA", "name": "Washington Reagan",     "lat": 38.848, "lon": -77.034, "tz": "US/Eastern"},
    "SEA":  {"cli": "CLISEA", "name": "Seattle-Tacoma",        "lat": 47.444, "lon": -122.314, "tz": "US/Pacific"},
    "BOS":  {"cli": "CLIBOS", "name": "Boston Logan",          "lat": 42.361, "lon": -71.010, "tz": "US/Eastern"},
    "PHX":  {"cli": "CLIPHX", "name": "Phoenix Sky Harbor",    "lat": 33.428, "lon": -112.004, "tz": "America/Phoenix"},
    "ATL":  {"cli": "CLIATL", "name": "Atlanta Hartsfield",    "lat": 33.630, "lon": -84.442, "tz": "US/Eastern"},
    "MIN":  {"cli": "CLIMSP", "name": "Minneapolis-St Paul",   "lat": 44.883, "lon": -93.229, "tz": "US/Central"},
    "DAL":  {"cli": "CLIDFW", "name": "Dallas-Fort Worth",     "lat": 32.898, "lon": -97.019, "tz": "US/Central"},
    "SATX": {"cli": "CLISAT", "name": "San Antonio Intl",      "lat": 29.533, "lon": -98.469, "tz": "US/Central"},
    "HOU":  {"cli": "CLIIAH", "name": "Houston Bush",          "lat": 29.980, "lon": -95.360, "tz": "US/Central"},
    "OKC":  {"cli": "CLIOKC", "name": "Oklahoma City Rogers",  "lat": 35.389, "lon": -97.600, "tz": "US/Central"},
}

HOURLY_EXP = float(os.environ.get("RAIN_FAIR_HOURLY_EXP", "0.5"))
P_FLOOR, P_CAP = 0.02, 0.98
UA = {"User-Agent": "KL rain_fair (jackdu224@gmail.com)"}
TIMEOUT = 20


def _get_json(url: str, retries: int = 2) -> dict:
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # noqa: BLE001 - caller logs
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url}: {last}")


def resolve_hourly_url(city: str) -> str:
    st = STATIONS[city]
    pts = _get_json(f"https://api.weather.gov/points/{st['lat']:.4f},{st['lon']:.4f}")
    url = (pts.get("properties") or {}).get("forecastHourly")
    if not url:
        raise RuntimeError(f"{city}: no forecastHourly in points response")
    return url


def fetch_hourly_pops(url: str) -> List[Tuple[datetime, float]]:
    """[(hour start UTC, PoP fraction)] from an NWS hourly forecast URL."""
    fc = _get_json(url)
    out: List[Tuple[datetime, float]] = []
    for per in (fc.get("properties") or {}).get("periods") or []:
        try:
            start = datetime.fromisoformat(per["startTime"])
        except (KeyError, ValueError):
            continue
        val = (per.get("probabilityOfPrecipitation") or {}).get("value")
        out.append((start.astimezone(timezone.utc), (val or 0) / 100.0))
    if not out:
        raise RuntimeError("hourly forecast had no periods")
    return out


def day_probability(pops: List[Tuple[datetime, float]], tzname: str,
                    date_iso: str, now_utc: datetime) -> Optional[dict]:
    """P(measurable rain in the REMAINING hours of local calendar day)."""
    tz = pytz.timezone(tzname)
    hours = [(ts, p) for ts, p in pops
             if ts.astimezone(tz).date().isoformat() == date_iso
             and ts + timedelta(hours=1) > now_utc]
    if not hours:
        return None
    prod = 1.0
    for _ts, p in hours:
        prod *= (1.0 - min(p, 0.99)) ** HOURLY_EXP
    p_max = max(p for _ts, p in hours)
    p_day = min(max(max(p_max, 1.0 - prod), P_FLOOR), P_CAP)
    return {"p": round(p_day, 4), "max_hour": round(p_max, 3),
            "n_hours": len(hours), "fetched_at": now_utc.isoformat()}


def write_fair_file(path: str, days: int = 3) -> Tuple[int, int]:
    """Refresh fair values for every station x next `days` local days.
    Merges over the existing file (failed stations keep old entries and old
    fetched_at). Returns (stations_ok, stations_failed)."""
    now_utc = datetime.now(timezone.utc)
    old: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f) or {}
    except (OSError, ValueError):
        pass
    fair: Dict[str, dict] = {d: dict(cities) for d, cities in (old.get("fair") or {}).items()}
    grid_urls: Dict[str, str] = dict(old.get("grid_urls") or {})

    ok = failed = 0
    errors: List[str] = []
    for city, st in STATIONS.items():
        try:
            url = grid_urls.get(city)
            if not url:
                url = resolve_hourly_url(city)
                grid_urls[city] = url
            try:
                pops = fetch_hourly_pops(url)
            except RuntimeError:
                # cached gridpoint may have gone stale/moved -> re-resolve once
                url = resolve_hourly_url(city)
                grid_urls[city] = url
                pops = fetch_hourly_pops(url)
            local_today = now_utc.astimezone(pytz.timezone(st["tz"])).date()
            for i in range(days):
                date_iso = (local_today + timedelta(days=i)).isoformat()
                entry = day_probability(pops, st["tz"], date_iso, now_utc)
                if entry is not None:
                    fair.setdefault(date_iso, {})[city] = entry
            ok += 1
        except Exception as e:                       # noqa: BLE001
            failed += 1
            errors.append(f"{city}: {e}")
        time.sleep(0.2)                              # be polite to api.weather.gov

    # drop dates older than yesterday (UTC) so the file can't grow forever
    cutoff = (now_utc - timedelta(days=1)).date().isoformat()
    fair = {d: c for d, c in fair.items() if d >= cutoff}

    out = {"generated_at": now_utc.isoformat(), "hourly_exp": HOURLY_EXP,
           "fair": fair, "grid_urls": grid_urls, "errors": errors[:10]}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return ok, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="rain_fair_values.json")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    ok, failed = write_fair_file(args.out, days=args.days)
    with open(args.out, encoding="utf-8") as f:
        data = json.load(f)
    print(f"wrote {args.out}: {ok} stations ok, {failed} failed "
          f"(hourly_exp={data['hourly_exp']})")
    for date_iso in sorted(data["fair"]):
        cities = data["fair"][date_iso]
        row = "  ".join(f"{c}:{int(round(e['p'] * 100)):>2}c"
                        for c, e in sorted(cities.items()))
        print(f"{date_iso}  {row}")
    for err in data.get("errors") or []:
        print(f"  ! {err}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
