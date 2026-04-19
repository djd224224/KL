"""KXHIGH city → NWS settlement-station mapping.

Keys are the city codes as they appear in Kalshi market tickers (e.g., the
segment between "KXHIGH" and the "-" in "KXHIGHMIA-26APR17-B84.5"). Newer
cities carry a leading "T" (TDAL, TDC, etc.).

Verified sources:
  - Kalshi contract-terms PDFs (NHIGH, CHIHIGH, MIAHIGH, LAXHIGH, AUSHIGH,
    HOUHIGH, DENHIGH, PHILHIGH at kalshi-public-docs.s3.amazonaws.com)
  - Remaining 11 cities inferred from standard NWS CLI product conventions
    and cross-checked against high_temp_trading.py coordinates (all match
    the major airport / Central Park for NYC).

Each entry:
  city         — human name
  icao         — 4-letter station identifier for the IEM JSON API
  cli_code     — 3-letter CLI product suffix on the NWS AWIPS channel
  wfo          — Weather Forecast Office (for forecast.weather.gov URLs)
  lat, lon     — reference coords (from high_temp_trading.py bot config)
"""
from __future__ import annotations

CITIES: dict[str, dict] = {
    "NY":    {"city": "New York City",  "icao": "KNYC", "cli_code": "NYC", "wfo": "OKX", "lat": 40.78333, "lon":  -73.96667},
    "CHI":   {"city": "Chicago",        "icao": "KMDW", "cli_code": "MDW", "wfo": "LOT", "lat": 41.78417, "lon":  -87.75528},
    "MIA":   {"city": "Miami",          "icao": "KMIA", "cli_code": "MIA", "wfo": "MFL", "lat": 25.79056, "lon":  -80.31639},
    "LAX":   {"city": "Los Angeles",    "icao": "KLAX", "cli_code": "LAX", "wfo": "LOX", "lat": 33.94250, "lon": -118.40806},
    "DEN":   {"city": "Denver",         "icao": "KDEN", "cli_code": "DEN", "wfo": "BOU", "lat": 39.84658, "lon": -104.65622},
    "PHIL":  {"city": "Philadelphia",   "icao": "KPHL", "cli_code": "PHL", "wfo": "PHI", "lat": 39.87327, "lon":  -75.22678},
    "AUS":   {"city": "Austin",         "icao": "KAUS", "cli_code": "AUS", "wfo": "EWX", "lat": 30.18304, "lon":  -97.67987},
    "THOU":  {"city": "Houston",        "icao": "KHOU", "cli_code": "HOU", "wfo": "HGX", "lat": 29.64542, "lon":  -95.27889},
    "TATL":  {"city": "Atlanta",        "icao": "KATL", "cli_code": "ATL", "wfo": "FFC", "lat": 33.64068, "lon":  -84.42694},
    "TDC":   {"city": "Washington DC",  "icao": "KDCA", "cli_code": "DCA", "wfo": "LWX", "lat": 38.85208, "lon":  -77.03772},
    "TPHX":  {"city": "Phoenix",        "icao": "KPHX", "cli_code": "PHX", "wfo": "PSR", "lat": 33.43722, "lon": -112.00778},
    "TDAL":  {"city": "Dallas",         "icao": "KDFW", "cli_code": "DFW", "wfo": "FWD", "lat": 32.89681, "lon":  -97.03781},
    "TLV":   {"city": "Las Vegas",      "icao": "KLAS", "cli_code": "LAS", "wfo": "VEF", "lat": 36.08000, "lon": -115.15222},
    "TOKC":  {"city": "Oklahoma City",  "icao": "KOKC", "cli_code": "OKC", "wfo": "OUN", "lat": 35.39306, "lon":  -97.60056},
    "TSEA":  {"city": "Seattle",        "icao": "KSEA", "cli_code": "SEA", "wfo": "SEW", "lat": 47.44889, "lon": -122.30917},
    "TSFO":  {"city": "San Francisco",  "icao": "KSFO", "cli_code": "SFO", "wfo": "MTR", "lat": 37.61961, "lon": -122.36558},
    "TSATX": {"city": "San Antonio",    "icao": "KSAT", "cli_code": "SAT", "wfo": "EWX", "lat": 29.53389, "lon":  -98.46917},
    "TMIN":  {"city": "Minneapolis",    "icao": "KMSP", "cli_code": "MSP", "wfo": "MPX", "lat": 44.88306, "lon":  -93.22889},
    "TNOLA": {"city": "New Orleans",    "icao": "KMSY", "cli_code": "MSY", "wfo": "LIX", "lat": 29.99333, "lon":  -90.25806},
}

CITY_NAME_TO_KEY = {v["city"]: k for k, v in CITIES.items()}
ICAO_TO_KEY = {v["icao"]: k for k, v in CITIES.items()}
