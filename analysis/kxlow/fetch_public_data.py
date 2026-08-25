# -*- coding: utf-8 -*-
"""Pull PUBLIC KXLOW market data for the low-temp bot analysis.

No Kalshi account credentials involved — everything here is public market
data (markets/results/volumes + the anonymous trade tape), public NWS CLI
climate reports via IEM (the same product the markets settle on), and
Open-Meteo previous-run forecasts (a proxy for "what the evening-before
forecast said", used to measure forecast error by city).

Designed to run in GitHub Actions (this repo's cloud sessions can't reach
these hosts directly) and write compact gzipped CSVs into
analysis/kxlow/data/ for the analysis step.
"""

import csv
import gzip
import json
import os
import time
from datetime import date

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT_DIR, exist_ok=True)

# Same city universe as low_temp_trading.py (CITY_ABV / CITY_TO_KALSHI_STATION).
CITY_ABV = {
    "Chicago": "CHI", "New York City": "NY", "Denver": "DEN",
    "Philadelphia": "PHIL", "Austin": "AUS", "Miami": "MIA",
    "Houston": "THOU", "Los Angeles": "LAX", "Atlanta": "TATL",
    "Washington DC": "TDC", "Phoenix": "TPHX", "Dallas": "TDAL",
    "Las Vegas": "TLV", "Oklahoma City": "TOKC", "Seattle": "TSEA",
    "San Francisco": "TSFO", "San Antonio": "TSATX", "Minneapolis": "TMIN",
    "New Orleans": "TNOLA", "Boston": "TBOS",
}
CITY_STATION = {
    "New York City": "KNYC", "Chicago": "KMDW", "Miami": "KMIA",
    "Los Angeles": "KLAX", "Denver": "KDEN", "Philadelphia": "KPHL",
    "Austin": "KAUS", "Houston": "KHOU", "Atlanta": "KATL",
    "Washington DC": "KDCA", "Phoenix": "KPHX", "Dallas": "KDFW",
    "Las Vegas": "KLAS", "Oklahoma City": "KOKC", "Seattle": "KSEA",
    "San Francisco": "KSFO", "San Antonio": "KSAT", "Minneapolis": "KMSP",
    "New Orleans": "KMSY", "Boston": "KBOS",
}
# Station-exact coordinates (same as low_temp_trading.py `cities`).
CITY_COORDS = {
    "Austin": (30.18304, -97.67987), "Miami": (25.79056, -80.31639),
    "Houston": (29.63750, -95.28250), "Denver": (39.84658, -104.65622),
    "New York City": (40.78333, -73.96667), "Philadelphia": (39.87327, -75.22678),
    "Chicago": (41.78417, -87.75528), "Los Angeles": (33.93806, -118.38889),
    "Atlanta": (33.64028, -84.42694), "Washington DC": (38.84833, -77.03417),
    "Phoenix": (33.42780, -112.00347), "Dallas": (32.89743, -97.02196),
    "Las Vegas": (36.07188, -115.16340), "Oklahoma City": (35.38861, -97.60028),
    "Seattle": (47.44472, -122.31361), "San Francisco": (37.61961, -122.36558),
    "San Antonio": (29.53278, -98.46361), "Minneapolis": (44.88306, -93.22889),
    "New Orleans": (29.99278, -90.25083), "Boston": (42.36056, -71.01056),
}

sess = requests.Session()
sess.headers["User-Agent"] = "KL-kxlow-analysis/1.0"


def get(url, params=None, tries=5, timeout=30):
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def _cents(obj, cents_key, dollars_key):
    """Price in cents from either an int-cents field or a string-dollars field."""
    v = obj.get(cents_key)
    if v is not None:
        return int(v)
    d = obj.get(dollars_key)
    if d is not None:
        try:
            return int(round(float(d) * 100))
        except (TypeError, ValueError):
            return None
    return None


def _count(obj):
    for k in ("count", "count_fp"):
        v = obj.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def fetch_markets():
    rows = []
    for city, abv in CITY_ABV.items():
        series = f"KXLOW{abv}"
        cursor, n = None, 0
        while True:
            params = {"series_ticker": series, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = get(f"{BASE}/markets", params)
            batch = data.get("markets", [])
            for m in batch:
                rows.append({
                    "city": city,
                    "series": series,
                    "ticker": m.get("ticker"),
                    "event_ticker": m.get("event_ticker"),
                    "status": m.get("status"),
                    "result": m.get("result"),
                    "strike_type": m.get("strike_type"),
                    "floor_strike": m.get("floor_strike"),
                    "cap_strike": m.get("cap_strike"),
                    "volume": m.get("volume"),
                    "open_interest": m.get("open_interest"),
                    "last_price": _cents(m, "last_price", "last_price_dollars"),
                    "open_time": m.get("open_time"),
                    "close_time": m.get("close_time"),
                })
            n += len(batch)
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
            time.sleep(0.1)
        print(f"{series:12s} {city:15s} -> {n} markets", flush=True)
        time.sleep(0.1)

    path = os.path.join(OUT_DIR, "kxlow_markets.csv.gz")
    cols = list(rows[0].keys()) if rows else ["city"]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"markets: {len(rows)} -> {path}")
    return rows


def fetch_trades(markets):
    traded = [m for m in markets if (m.get("volume") or 0) > 0]
    print(f"markets with volume>0: {len(traded)}")
    rows = []
    for i, m in enumerate(traded):
        t = m["ticker"]
        cursor = None
        while True:
            params = {"ticker": t, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = get(f"{BASE}/markets/trades", params)
            batch = data.get("trades", [])
            for tr in batch:
                rows.append({
                    "ticker": t,
                    "city": m["city"],
                    "created_time": tr.get("created_time"),
                    "count": _count(tr),
                    "yes_price": _cents(tr, "yes_price", "yes_price_dollars"),
                    "no_price": _cents(tr, "no_price", "no_price_dollars"),
                    "taker_side": tr.get("taker_side"),
                })
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
            time.sleep(0.04)
        if (i + 1) % 100 == 0:
            print(f"  trades: {i + 1}/{len(traded)} markets, {len(rows)} rows", flush=True)
        time.sleep(0.04)

    path = os.path.join(OUT_DIR, "kxlow_trades.csv.gz")
    cols = ["ticker", "city", "created_time", "count", "yes_price", "no_price", "taker_side"]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"trades: {len(rows)} -> {path}")


def fetch_cli():
    rows = []
    for city, icao in CITY_STATION.items():
        url = f"https://mesonet.agron.iastate.edu/json/cli.py?station={icao}&year=2026"
        try:
            data = get(url)
            results = data.get("results", [])
            for r in results:
                rows.append({
                    "city": city,
                    "station": icao,
                    "date": r.get("valid"),
                    "low": r.get("low"),
                    "low_time": r.get("low_time"),
                    "high": r.get("high"),
                })
            print(f"CLI {icao} {city:15s} -> {len(results)} days", flush=True)
        except Exception as e:
            print(f"CLI {icao} {city} FAILED: {e}", flush=True)
        time.sleep(0.3)

    path = os.path.join(OUT_DIR, "cli_lows.csv.gz")
    cols = ["city", "station", "date", "low", "low_time", "high"]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"cli: {len(rows)} -> {path}")


def fetch_forecast_proxy(start="2026-07-15", end=None):
    """Open-Meteo previous-runs hourly temps: what yesterday's model run
    forecast for each hour. Local-day min of _previous_day1 ≈ the Tmin
    forecast available the evening before — the bot's main-run vantage."""
    end = end or date.today().isoformat()
    rows = []
    for city, (lat, lon) in CITY_COORDS.items():
        for model in ("gfs_seamless", "ecmwf_ifs025"):
            try:
                data = get(
                    "https://previous-runs-api.open-meteo.com/v1/forecast",
                    {
                        "latitude": lat, "longitude": lon,
                        "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
                        "temperature_unit": "fahrenheit",
                        "timezone": "auto",
                        "start_date": start, "end_date": end,
                        "models": model,
                    }, timeout=60)
                h = data.get("hourly", {})
                times = h.get("time", [])
                p1 = h.get("temperature_2m_previous_day1", [])
                p2 = h.get("temperature_2m_previous_day2", [])
                for j, ts in enumerate(times):
                    rows.append({
                        "city": city, "model": model, "time_local": ts,
                        "t_prev1": p1[j] if j < len(p1) else None,
                        "t_prev2": p2[j] if j < len(p2) else None,
                    })
                print(f"OM {model:13s} {city:15s} -> {len(times)} hours", flush=True)
            except Exception as e:
                print(f"OM {model} {city} FAILED: {e}", flush=True)
            time.sleep(0.3)

    path = os.path.join(OUT_DIR, "forecast_proxy.csv.gz")
    cols = ["city", "model", "time_local", "t_prev1", "t_prev2"]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"forecast proxy: {len(rows)} -> {path}")


if __name__ == "__main__":
    markets = fetch_markets()
    fetch_trades(markets)
    fetch_cli()
    fetch_forecast_proxy()
    print("DONE")
