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

Station set = the 33 CLI stations named in the KXRAIN market rules: 20
fetched 2026-07-28 (note CHI=O'Hare, DAL=DFW, HOU=IAH, NYC=Central Park),
and the 13 cities Kalshi listed since, added 2026-10-09 (Jack: "fix"), after
CMH/TAM/ABQ/LEX went unpriced on a -$196 day. Their coordinates and time zones
are the NWS station records (api.weather.gov/stations/K...); note TAM=CLITPA.

NEW CITIES ARE MODELED AUTOMATICALLY (Jack 2026-10-09: "when new markets are
added onto an event that is modeled separately like cities in rain, they
should automatically be modeled generally"). Every DISCOVER_SECS the writer
reads Kalshi's open KXRAIN markets (signed reader when the bot hands one
over, else the public endpoint). Any city suffix without a STATIONS row is
resolved from its own rules: "total precipitation at CLITPA" names the CLI
product, whose airport is K + code (PHNL-style P + code as a fallback), and
the NWS station record gives coordinates and time zone. Resolved rows live
in the output file's "auto_stations" and are priced exactly like the seed
table, which always wins. A listed city that can't be resolved (no CLI code
in its rules, no NWS station) goes to "unmapped" with the reason. The IMM
family watch alerts on that list (model coverage), so a city the bot quotes
without a fair never goes unnoticed.
Per-station failures keep the previous entry (with its old fetched_at, so
the bot's TTL naturally expires it) — one flaky NWS endpoint must not
blank the other cities.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

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
    # added 2026-10-09: the cities Kalshi listed after 7/28
    "ABQ":  {"cli": "CLIABQ", "name": "Albuquerque Intl",      "lat": 35.042, "lon": -106.615, "tz": "America/Denver"},
    "CLL":  {"cli": "CLICLL", "name": "College Station Easterwood", "lat": 30.582, "lon": -96.362, "tz": "America/Chicago"},
    "CMH":  {"cli": "CLICMH", "name": "Columbus John Glenn",   "lat": 39.991, "lon": -82.877, "tz": "America/New_York"},
    "EWR":  {"cli": "CLIEWR", "name": "Newark Liberty",        "lat": 40.683, "lon": -74.169, "tz": "America/New_York"},
    "IND":  {"cli": "CLIIND", "name": "Indianapolis Intl",     "lat": 39.725, "lon": -86.282, "tz": "America/Indiana/Indianapolis"},
    "LEX":  {"cli": "CLILEX", "name": "Lexington Blue Grass",  "lat": 38.034, "lon": -84.612, "tz": "America/New_York"},
    "MKE":  {"cli": "CLIMKE", "name": "Milwaukee Mitchell",    "lat": 42.955, "lon": -87.904, "tz": "America/Chicago"},
    "PIT":  {"cli": "CLIPIT", "name": "Pittsburgh Intl",       "lat": 40.485, "lon": -80.215, "tz": "America/New_York"},
    "PVD":  {"cli": "CLIPVD", "name": "Providence TF Green",   "lat": 41.722, "lon": -71.428, "tz": "America/New_York"},
    "SGF":  {"cli": "CLISGF", "name": "Springfield-Branson",   "lat": 37.240, "lon": -93.390, "tz": "America/Chicago"},
    "STL":  {"cli": "CLISTL", "name": "St. Louis Lambert",     "lat": 38.753, "lon": -90.374, "tz": "America/Chicago"},
    "TAM":  {"cli": "CLITPA", "name": "Tampa Intl",            "lat": 27.961, "lon": -82.540, "tz": "America/New_York"},
    "TTN":  {"cli": "CLITTN", "name": "Trenton-Mercer",        "lat": 40.276, "lon": -74.816, "tz": "America/New_York"},
}

HOURLY_EXP = float(os.environ.get("RAIN_FAIR_HOURLY_EXP", "0.5"))
P_FLOOR, P_CAP = 0.02, 0.98
UA = {"User-Agent": "KL rain_fair (jackdu224@gmail.com)"}
TIMEOUT = 20
# auto-discovery of the cities Kalshi lists (see the module docstring)
SERIES = "KXRAIN"
KALSHI_MARKETS_URL = os.environ.get(
    "RAIN_FAIR_MARKETS_URL", "https://api.elections.kalshi.com/trade-api/v2/markets")
DISCOVER_SECS = float(os.environ.get("RAIN_FAIR_DISCOVER_SECS", str(6 * 3600)))
_CLI_RE = re.compile(r"\bCLI([A-Z0-9]{3})\b")


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


def _markets_page(params: dict, get_json: Optional[Callable] = None) -> dict:
    """One /markets page: the bot's signed reader when it handed one over,
    else (or on its failure) the public endpoint."""
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:                            # noqa: BLE001 - public fallback
            pass
    r = requests.get(KALSHI_MARKETS_URL, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def listed_cities(get_json: Optional[Callable] = None) -> Dict[str, Optional[str]]:
    """{city suffix: CLI product named in its rules, or None} for every open
    KXRAIN market Kalshi lists right now."""
    out: Dict[str, Optional[str]] = {}
    cursor = None
    for _ in range(20):
        params = {"series_ticker": SERIES, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        js = _markets_page(params, get_json)
        for m in js.get("markets") or []:
            parts = str(m.get("ticker") or "").split("-")
            if len(parts) != 3 or parts[0] != SERIES:
                continue
            hit = _CLI_RE.search(str(m.get("rules_primary") or ""))
            if out.get(parts[2]) is None:
                out[parts[2]] = hit.group(0) if hit else None
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def resolve_station(cli: str) -> dict:
    """A STATIONS-shaped row for a CLI product ("CLITPA") from the NWS
    record of its airport: K + code, then P + code (Alaska / Hawaii)."""
    code = cli[3:]
    errs = []
    for sid in (f"K{code}", f"P{code}"):
        try:
            js = _get_json(f"https://api.weather.gov/stations/{sid}", retries=1)
        except RuntimeError as e:
            errs.append(str(e)[:80])
            continue
        lon, lat = (js.get("geometry") or {}).get("coordinates")[:2]
        props = js.get("properties") or {}
        tz = props.get("timeZone")
        pytz.timezone(tz)                            # raises on an unknown zone
        return {"cli": cli, "name": props.get("name") or sid, "lat": round(float(lat), 3),
                "lon": round(float(lon), 3), "tz": tz, "station": sid, "auto": True}
    raise RuntimeError(f"no NWS station for {cli} ({'; '.join(errs)})")


def resolve_hourly_url(city: str, st: Optional[dict] = None) -> str:
    st = st or STATIONS[city]
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


def discover_stations(old: dict, now_ts: float, get_json: Optional[Callable] = None,
                      lister: Callable = listed_cities,
                      resolver: Callable = resolve_station
                      ) -> Tuple[Dict[str, dict], Dict[str, Optional[str]],
                                 Optional[float], Dict[str, str], List[str]]:
    """The auto-modeled cities: (auto_stations, listed, listed_at, unmapped,
    errors). Kalshi's listing is re-read every DISCOVER_SECS (a failed read
    keeps the previous one). Every listed city without a STATIONS row gets
    an auto row from its rules' CLI product, or an "unmapped" reason; an auto
    row whose city Kalshi no longer lists is dropped."""
    auto: Dict[str, dict] = dict(old.get("auto_stations") or {})
    listed: Dict[str, Optional[str]] = dict(old.get("listed") or {})
    listed_at = old.get("listed_at")
    errors: List[str] = []
    if listed_at is None or now_ts - float(listed_at) >= DISCOVER_SECS:
        try:
            fresh = lister(get_json)
            if fresh:
                listed, listed_at = fresh, now_ts
            else:
                errors.append("discovery: Kalshi listed no open KXRAIN market")
        except Exception as e:                       # noqa: BLE001 - keep the old listing
            errors.append(f"discovery: {e}"[:160])
    if listed:
        auto = {c: v for c, v in auto.items() if c in listed}
    unmapped: Dict[str, str] = {}
    for city, cli in sorted(listed.items()):
        if city in STATIONS:
            continue
        have = auto.get(city)
        if have and (cli is None or have.get("cli") == cli):
            continue
        if not cli:
            unmapped[city] = "no CLI station named in the market rules"
            continue
        try:
            auto[city] = resolver(cli)
        except Exception as e:                       # noqa: BLE001 - retried next write
            unmapped[city] = f"{cli}: {e}"[:160]
    return auto, listed, listed_at, unmapped, errors


def write_fair_file(path: str, days: int = 3, get_json: Optional[Callable] = None,
                    discover: bool = True, lister: Callable = listed_cities,
                    resolver: Callable = resolve_station) -> Tuple[int, int]:
    """Refresh fair values for every station x next `days` local days: the
    seed STATIONS plus the auto-modeled cities (discover_stations).
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
    auto: Dict[str, dict] = dict(old.get("auto_stations") or {})
    listed = dict(old.get("listed") or {})
    listed_at, unmapped = old.get("listed_at"), dict(old.get("unmapped") or {})
    if discover:
        auto, listed, listed_at, unmapped, errs = discover_stations(
            old, now_utc.timestamp(), get_json, lister, resolver)
        errors += errs
    stations: Dict[str, dict] = dict(auto)
    stations.update(STATIONS)                        # the verified seed rows win
    for city, st in stations.items():
        try:
            url = grid_urls.get(city)
            if not url:
                url = resolve_hourly_url(city, st)
                grid_urls[city] = url
            try:
                pops = fetch_hourly_pops(url)
            except RuntimeError:
                # cached gridpoint may have gone stale/moved -> re-resolve once
                url = resolve_hourly_url(city, st)
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
           "fair": fair, "grid_urls": grid_urls, "errors": errors[:10],
           # model coverage: auto rows, Kalshi's listing, the cities with none
           "auto_stations": auto, "listed": listed, "listed_at": listed_at,
           "unmapped": unmapped}
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
    for city, st in sorted((data.get("auto_stations") or {}).items()):
        print(f"  auto: {city} = {st.get('cli')} {st.get('station')} {st.get('name')}")
    for city, why in sorted((data.get("unmapped") or {}).items()):
        print(f"  ! UNMAPPED {city}: {why}")
    for err in data.get("errors") or []:
        print(f"  ! {err}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
