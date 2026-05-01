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
  tz           — IANA timezone of the station; Kalshi settles each market
                 on the local 24h window (midnight-to-midnight in this tz)
                 of the ticker's event_date. The dashboard's "high so far"
                 must filter obs by this tz, not by ET.
                 Note: Phoenix uses "America/Phoenix" (no DST observed).
  lat, lon     — reference coords (from high_temp_trading.py bot config)
"""
from __future__ import annotations

# Lat/lon are the EXACT coordinates of the Kalshi settlement station, as
# reported by api.weather.gov/stations/{icao} — ensures Open-Meteo backfill
# and NWS grid-cell lookups sample the same point Kalshi settles on.
CITIES: dict[str, dict] = {
    "NY":    {"city": "New York City",  "icao": "KNYC", "cli_code": "NYC", "wfo": "OKX", "tz": "America/New_York",     "lat": 40.78333, "lon":  -73.96667},
    "CHI":   {"city": "Chicago",        "icao": "KMDW", "cli_code": "MDW", "wfo": "LOT", "tz": "America/Chicago",      "lat": 41.78417, "lon":  -87.75528},
    "MIA":   {"city": "Miami",          "icao": "KMIA", "cli_code": "MIA", "wfo": "MFL", "tz": "America/New_York",     "lat": 25.79056, "lon":  -80.31639},
    "LAX":   {"city": "Los Angeles",    "icao": "KLAX", "cli_code": "LAX", "wfo": "LOX", "tz": "America/Los_Angeles",  "lat": 33.93806, "lon": -118.38889},
    "DEN":   {"city": "Denver",         "icao": "KDEN", "cli_code": "DEN", "wfo": "BOU", "tz": "America/Denver",       "lat": 39.84658, "lon": -104.65622},
    "PHIL":  {"city": "Philadelphia",   "icao": "KPHL", "cli_code": "PHL", "wfo": "PHI", "tz": "America/New_York",     "lat": 39.87327, "lon":  -75.22678},
    "AUS":   {"city": "Austin",         "icao": "KAUS", "cli_code": "AUS", "wfo": "EWX", "tz": "America/Chicago",      "lat": 30.18304, "lon":  -97.67987},
    "THOU":  {"city": "Houston",        "icao": "KHOU", "cli_code": "HOU", "wfo": "HGX", "tz": "America/Chicago",      "lat": 29.63750, "lon":  -95.28250},
    "TATL":  {"city": "Atlanta",        "icao": "KATL", "cli_code": "ATL", "wfo": "FFC", "tz": "America/New_York",     "lat": 33.64028, "lon":  -84.42694},
    "TDC":   {"city": "Washington DC",  "icao": "KDCA", "cli_code": "DCA", "wfo": "LWX", "tz": "America/New_York",     "lat": 38.84833, "lon":  -77.03417},
    "TPHX":  {"city": "Phoenix",        "icao": "KPHX", "cli_code": "PHX", "wfo": "PSR", "tz": "America/Phoenix",      "lat": 33.42780, "lon": -112.00347},
    "TDAL":  {"city": "Dallas",         "icao": "KDFW", "cli_code": "DFW", "wfo": "FWD", "tz": "America/Chicago",      "lat": 32.89743, "lon":  -97.02196},
    "TLV":   {"city": "Las Vegas",      "icao": "KLAS", "cli_code": "LAS", "wfo": "VEF", "tz": "America/Los_Angeles",  "lat": 36.07188, "lon": -115.16340},
    "TOKC":  {"city": "Oklahoma City",  "icao": "KOKC", "cli_code": "OKC", "wfo": "OUN", "tz": "America/Chicago",      "lat": 35.38861, "lon":  -97.60028},
    "TSEA":  {"city": "Seattle",        "icao": "KSEA", "cli_code": "SEA", "wfo": "SEW", "tz": "America/Los_Angeles",  "lat": 47.44472, "lon": -122.31361},
    "TSFO":  {"city": "San Francisco",  "icao": "KSFO", "cli_code": "SFO", "wfo": "MTR", "tz": "America/Los_Angeles",  "lat": 37.61961, "lon": -122.36558},
    "TSATX": {"city": "San Antonio",    "icao": "KSAT", "cli_code": "SAT", "wfo": "EWX", "tz": "America/Chicago",      "lat": 29.53278, "lon":  -98.46361},
    "TMIN":  {"city": "Minneapolis",    "icao": "KMSP", "cli_code": "MSP", "wfo": "MPX", "tz": "America/Chicago",      "lat": 44.88306, "lon":  -93.22889},
    "TNOLA": {"city": "New Orleans",    "icao": "KMSY", "cli_code": "MSY", "wfo": "LIX", "tz": "America/Chicago",      "lat": 29.99278, "lon":  -90.25083},
    "TBOS":  {"city": "Boston",         "icao": "KBOS", "cli_code": "BOS", "wfo": "BOX", "tz": "America/New_York",     "lat": 42.36056, "lon":  -71.01056},
}

CITY_NAME_TO_KEY = {v["city"]: k for k, v in CITIES.items()}
ICAO_TO_KEY = {v["icao"]: k for k, v in CITIES.items()}
