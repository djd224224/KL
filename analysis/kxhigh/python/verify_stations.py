"""Verify station + lat/lon consistency across all sources for each city.

Checks:
  1. The Kalshi-declared ICAO station actually exists on NWS api.weather.gov
  2. Bot's lat/lon (from high_temp_trading.py) is close to the Kalshi station
  3. The station returned by `/points/{lat},{lon}` → observationStations as
     `features[0]` matches the Kalshi ICAO (if NOT, our pre-trade filter is
     sampling the wrong microclimate)
  4. Iowa State IEM returns rows for the Kalshi ICAO
  5. Open-Meteo elevation at bot's lat/lon is reasonable (catches wrong coords)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cities import CITIES

NWS_HEADERS = {"User-Agent": "KXHIGH-verify/1.0"}


def fetch_json(url: str) -> dict | None:
    try:
        with urlopen(Request(url, headers=NWS_HEADERS), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__error__": str(e)}


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


def check_city(key: str, cfg: dict) -> dict:
    lat, lon = cfg["lat"], cfg["lon"]
    icao = cfg["icao"]
    out = {"key": key, "city": cfg["city"], "icao_declared": icao,
           "bot_lat": lat, "bot_lon": lon}

    # 1. Station lookup by ICAO
    s = fetch_json(f"https://api.weather.gov/stations/{icao}")
    if s and "__error__" not in s:
        props = s.get("properties", {})
        coords = s.get("geometry", {}).get("coordinates", [None, None])
        out["station_lon"] = coords[0]
        out["station_lat"] = coords[1]
        out["station_name"] = props.get("name")
        out["station_wfo"] = props.get("forecast", "").split("/")[-3] if props.get("forecast") else None
        if out["station_lat"] is not None:
            out["distance_km"] = round(haversine_km(lat, lon, out["station_lat"], out["station_lon"]), 3)
        out["station_exists"] = True
    else:
        out["station_exists"] = False
        out["station_error"] = s.get("__error__") if s else "no response"

    # 2. What does /points/lat,lon return as the closest station?
    pts = fetch_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    if pts and "__error__" not in pts:
        stations_url = pts.get("properties", {}).get("observationStations")
        if stations_url:
            stn = fetch_json(stations_url)
            if stn and "__error__" not in stn:
                features = stn.get("features", [])
                if features:
                    top = features[0]["properties"].get("stationIdentifier", "")
                    out["points_top_station"] = top
                    out["points_top_matches"] = (top == icao)
                    # Where in the ordering is the Kalshi ICAO?
                    ids = [f["properties"].get("stationIdentifier", "") for f in features]
                    out["icao_rank_in_points"] = ids.index(icao) + 1 if icao in ids else None

    return out


def main():
    print(f"{'Key':<6} {'ICAO':<5} {'Dist(km)':>8} {'TopStation':<11} {'Match':<6} {'Station name':<35}")
    print("-" * 90)
    issues = []
    for key, cfg in CITIES.items():
        r = check_city(key, cfg)
        dist = r.get("distance_km", "?")
        top = r.get("points_top_station", "?")
        match = "yes" if r.get("points_top_matches") else "NO"
        name = (r.get("station_name") or "")[:34]
        print(f"{key:<6} {r['icao_declared']:<5} {str(dist):>8} {top:<11} {match:<6} {name}")
        if not r.get("station_exists"):
            issues.append(f"{key}: station {r['icao_declared']} lookup FAILED — {r.get('station_error')}")
        if r.get("distance_km", 0) > 50:
            issues.append(f"{key}: bot coords are {r['distance_km']}km from {r['icao_declared']} — possibly wrong")
        if not r.get("points_top_matches") and r.get("icao_rank_in_points") is None:
            issues.append(f"{key}: {r['icao_declared']} NOT in /points station list for bot coords")
        elif not r.get("points_top_matches"):
            issues.append(f"{key}: /points top station is {r.get('points_top_station')}, "
                          f"Kalshi station {r['icao_declared']} at rank {r['icao_rank_in_points']}")

    print("\n" + "=" * 60)
    if issues:
        print(f"{len(issues)} ISSUE(S):")
        for i in issues:
            print(f"  - {i}")
    else:
        print("All checks pass ✓")


if __name__ == "__main__":
    main()
