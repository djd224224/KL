#!/usr/bin/env python3
"""
Kalshi High Temperature Trading Bot — ORDER PLACEMENT
=====================================================
Places NO limit orders on Kalshi KXHIGH daily high temperature markets
across 20 US cities.

Pipeline:
  1. Collect high temp forecasts from 3 sources (NWS, Weather Underground, AccuWeather)
  2. Average them → probability model (normal CDF) for each 2°F temperature bucket
  3. Pull Kalshi order books → find mispriced NO contracts
  4. Place tiered NO limit orders where model sees edge vs market

Run via GitHub Actions (cron) or Google Colab.
Monitoring/reconciliation (fills, settlements) is in high_temp_monitoring.py.

GitHub Actions secrets:
    KALSHI_API_KEY_ID       - Kalshi API key ID
    KALSHI_PRIVATE_KEY      - RSA private key (raw PEM text)
    GCP_PROJECT_ID          - Google Cloud project ID
    GCP_DATASET_ID          - BigQuery dataset name
    WEATHER_UNDERGROUND_KEY - Weather Underground API key
    ACCUWEATHER_KEY         - AccuWeather API key
"""

# =====================================================================
# IMPORTS — all at top for GitHub Actions compatibility
# =====================================================================
import os
import sys
import re
import json
import uuid
import base64
import logging
import time
from datetime import datetime, timedelta

# Auto-install missing packages when running in Google Colab
IS_COLAB = "google.colab" in sys.modules or "COLAB_RELEASE_TAG" in os.environ
if IS_COLAB:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "google-cloud-bigquery", "db-dtypes", "pyarrow"])

import pytz
import numpy as np
import pandas as pd
import requests
from scipy.stats import norm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient, HttpError

# =====================================================================
# LOGGING
# =====================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kalshi_high_temp")

# =====================================================================
# CONFIGURATION
# Env vars with Colab-friendly defaults. GitHub Actions overrides via secrets.
# =====================================================================

# --- Kalshi API ---
KALSHI_API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
KALSHI_PRIVATE_KEY      = os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_API_BASE         = "https://api.elections.kalshi.com/trade-api/v2"

# --- BigQuery ---
BQ_PROJECT = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
BQ_DATASET = os.environ.get("GCP_DATASET_ID", "Kalshi")

# --- Weather APIs ---
WEATHER_UNDERGROUND_KEY = os.environ.get("WEATHER_UNDERGROUND_KEY", "a828c2a178844147a8c2a17884a147a5")
ACCUWEATHER_KEY         = os.environ.get("ACCUWEATHER_KEY", "lEl0lfAft6PncVXwatr92Y2YjGJL5YKs")

# --- Table prefix (shared with monitoring script) ---
BQ_TABLE_PREFIX = "KXHIGH_"

# --- Order placement parameters ---
NUM_PRICE_LEVELS         = 8    # Tiered orders per market (depth)
INCREMENT                = 3    # Cents between each NO bid (regular markets)
INCREMENT_TAIL           = 6    # Cents between each NO bid (tail markets)
CONTRACTS_PER_TIER       = 25   # Fixed contract count per tier (no step-up)
ADJACENT_MULTIPLIER      = 1.5  # Contract multiplier for markets 1-2°F from forecast center
MAX_DISTANCE_FROM_FORECAST = 2.0 # Skip markets >2°F from forecast center (lost money historically)
MAX_CONTRACTS            = 500  # Hard cap per market (was 1000, data shows 500+ is optimal)
CUTOFF_PROBABILITY       = 0.20 # Only trade markets where P(yes) > 20%
CEILING_PROBABILITY      = 0.75 # Skip markets where P(yes) > 75% (overconfident, -17.7% ROI historically)
MIN_EDGE_CENTS           = 2    # Only trade if model NO price > market NO offer by this much
MAX_SPREAD_CENTS         = 15   # Skip markets with spread > 15¢ (>15¢ = -23% ROI historically)
MAX_FORECAST_DISAGREEMENT = 5.0 # Skip city if sources disagree by >5°F (unreliable, -34% ROI)
AUTO_CALIBRATE_MIN_OBS   = 30   # Min observations per city before using auto-calibrated floor std

# =====================================================================
# CITY COORDINATES — single source of truth
# Exact NWS METAR/ASOS stations Kalshi uses for CLI settlement.
# =====================================================================
CITIES = {
    "Austin":         {"lat": 30.18304, "lon": -97.67987},   # KAUS Bergstrom
    "Miami":          {"lat": 25.79056, "lon": -80.31639},   # KMIA
    "Houston":        {"lat": 29.64542, "lon": -95.27889},   # KHOU Hobby
    "Denver":         {"lat": 39.84658, "lon": -104.65622},  # KDEN
    "New York City":  {"lat": 40.78333, "lon": -73.96667},   # KNYC Central Park
    "Philadelphia":   {"lat": 39.87327, "lon": -75.22678},   # KPHL
    "Chicago":        {"lat": 41.78417, "lon": -87.75528},   # KMDW Midway
    "Los Angeles":    {"lat": 33.94250, "lon": -118.40806},  # KLAX
    "Atlanta":        {"lat": 33.64068, "lon": -84.42694},   # KATL
    "Washington DC":  {"lat": 38.85208, "lon": -77.03772},   # KDCA Reagan
    "Phoenix":        {"lat": 33.43722, "lon": -112.00778},  # KPHX
    "Dallas":         {"lat": 32.89681, "lon": -97.03781},   # KDFW
    "Las Vegas":      {"lat": 36.08000, "lon": -115.15222},  # KLAS
    "Oklahoma City":  {"lat": 35.39306, "lon": -97.60056},   # KOKC
    "Seattle":        {"lat": 47.44889, "lon": -122.30917},  # KSEA
    "San Francisco":  {"lat": 37.61961, "lon": -122.36558},  # KSFO
    "San Antonio":    {"lat": 29.53389, "lon": -98.46917},   # KSAT
    "Minneapolis":    {"lat": 44.88306, "lon": -93.22889},   # KMSP
    "New Orleans":    {"lat": 29.99333, "lon": -90.25806},   # KMSY
}

# =====================================================================
# EVENT TICKER DEFINITIONS
# Each tuple: (ticker_prefix, city, hi_no_price)
#   ticker_prefix → combined with date to make "KXHIGHCHI-26MAR10"
#   city          → joins forecasts to markets
#   hi_no_price   → starting NO bid price (cents). Higher = more aggressive.
# =====================================================================
EVENT_TICKER_DEFS = [
    # (ticker_prefix, city, hi_no_price)
    # hi_no_price calibrated from 9 months of historical P&L (Jan-Sep 2025)
    ("KXHIGHCHI",  "Chicago",       56), # ROI +1.2% → OK
    ("KXHIGHNY",   "New York City", 46), # ROI +58.4% → was 41, +5
    ("KXHIGHDEN",  "Denver",        57), # ROI -4.2% → was 60, -3
    ("KXHIGHPHIL", "Philadelphia",  52), # ROI +51.0% → was 47, +5
    ("KXHIGHAUS",  "Austin",        63), # ROI +9.5% → was 60, +3
    ("KXHIGHMIA",  "Miami",         43), # ROI -4.0% → was 46, -3
    ("KXHIGHLAX",  "Los Angeles",   58), # ROI +6.6% → was 55, +3
    ("KXHIGHTATL", "Atlanta",       55), # No historical data — keep default
    ("KXHIGHTDC",  "Washington DC", 50), # No historical data — keep default
    ("KXHIGHTPHX", "Phoenix",       55), # No historical data — keep default
    ("KXHIGHTDAL", "Dallas",        55), # No historical data — keep default
    ("KXHIGHTLV",  "Las Vegas",     55), # No historical data — keep default
    ("KXHIGHTOKC", "Oklahoma City", 55), # No historical data — keep default
    ("KXHIGHTSEA", "Seattle",       50), # No historical data — keep default
    ("KXHIGHTSFO", "San Francisco", 50), # No historical data — keep default
    ("KXHIGHTHOU", "Houston",       59), # ROI +108% but only n=3, keep ~same
    ("KXHIGHTSATX","San Antonio",   55), # No historical data — keep default
    ("KXHIGHTMIN", "Minneapolis",   55), # No historical data — keep default
    ("KXHIGHTNOLA","New Orleans",   55), # No historical data — keep default
]

# Cancel times: Eastern cities cancel at 9:05 CT, others at 10:05 CT
CITY_CANCEL_TIMES = {
    "CHI":(10,5),"AUS":(10,5),"HOU":(10,5),"DEN":(10,5),"NY-":(9,5),"PHI":(9,5),
    "MIA":(9,5),"LAX":(10,5),"ATL":(9,5),"TDC":(9,5),"PHX":(10,5),"DAL":(10,5),
    "TLV":(10,5),"OKC":(10,5),"SEA":(10,5),"SFO":(10,5),
    "THOU":(10,5),"SATX":(10,5),"TMIN":(10,5),"NOLA":(10,5),
}
# Longer abbreviations first so "THOU" matches before "HOU"
CITY_ABV_KEYS = ["THOU","SATX","TMIN","NOLA","CHI","AUS","DEN","NY-","PHI","MIA",
                 "LAX","ATL","TDC","PHX","DAL","TLV","OKC","SEA","SFO","HOU"]

# =====================================================================
# CITY_FLOOR_STD — minimum std dev (°F) per city for the probability model.
# Calibrated from 1,816 observations (Jan-Oct 2025), top 20% outliers removed.
# Cities without historical data use 1.2 (overall trimmed average).
# The model uses: max(inter_source_std, city_floor_std).
# =====================================================================
CITY_FLOOR_STD = {
    "Austin": 1.2,          # n=212 (trimmed), MAE=1.7
    "Miami": 1.0,           # n=215 (trimmed), MAE=1.3
    "Houston": 1.9,         # n=9 (trimmed, small sample)
    "Denver": 1.4,          # n=213 (trimmed), MAE=1.9
    "New York City": 1.2,   # n=214 (trimmed), MAE=1.5
    "Philadelphia": 1.3,    # n=212 (trimmed), MAE=1.7
    "Chicago": 1.3,         # n=209 (trimmed), MAE=1.8
    "Los Angeles": 1.2,     # n=197 (trimmed), MAE=1.6
    "Atlanta": 1.2,         # No data — overall trimmed average
    "Washington DC": 1.3,   # No data — similar to Philadelphia
    "Phoenix": 1.1,         # No data — arid = more predictable
    "Dallas": 1.2,          # No data — overall trimmed average
    "Las Vegas": 1.1,       # No data — arid = more predictable
    "Oklahoma City": 1.4,   # No data — Great Plains = variable
    "Seattle": 1.2,         # No data — maritime = moderate
    "San Francisco": 1.2,   # No data — maritime = moderate
    "San Antonio": 1.2,     # No data — similar to Austin
    "Minneapolis": 1.4,     # No data — continental = variable
    "New Orleans": 1.2,     # No data — Gulf Coast = moderate
}

# =====================================================================
# CITY_FORECAST_BIAS — systematic forecast bias per city (°F).
# Positive = consensus over-forecasts (actual is lower than predicted).
# Negative = consensus under-forecasts (actual is higher than predicted).
# Applied as: corrected_avg = avg_forecast - bias
# Calibrated from 1,816 observations (Jan-Oct 2025).
# Cities without data default to 0 (no correction).
# =====================================================================
CITY_FORECAST_BIAS = {
    "Chicago": -1.0,        # Under-forecasts by 1.0°F (actual runs hotter)
    "Los Angeles": -0.9,    # Under-forecasts by 0.9°F
    "Miami": -0.8,          # Under-forecasts by 0.8°F
    "Philadelphia": -0.7,   # Under-forecasts by 0.7°F
    "New York City": -0.3,  # Under-forecasts by 0.3°F
    "Denver": -0.2,         # Under-forecasts by 0.2°F (near zero)
    "Austin": 0.1,          # Over-forecasts by 0.1°F (near zero)
    "Houston": -1.9,        # Under-forecasts by 1.9°F (small sample n=12)
}

# =====================================================================
# WEATHER CONDITION BOOST — widen std dev when NWS forecasts mention
# conditions that historically increase forecast error.
# Calibrated from 1,816 observations:
#   "rain"         → +0.39°F MAE, +0.44°F std (n=565)
#   "shower"       → +0.30°F MAE, +0.32°F std (n=585)
#   "snow"         → +0.24°F MAE, +0.22°F std (n=64)
#   "thunderstorm" → +0.20°F MAE, +0.13°F std (n=416)
#   "cloudy"       → +0.31°F MAE, +0.33°F std (n=592)
# "front" and "wind advisory" never appeared in historical data.
# CONDITION_BOOST_STD is conservative — set below avg Δstd to avoid over-correction.
# =====================================================================
CONDITION_BOOST_PATTERNS = ["rain", "shower", "snow", "thunderstorm", "cloudy"]
CONDITION_BOOST_STD = 0.5  # °F added when any pattern matches (conservative)

# =====================================================================
# HISTORICAL ACTUALS-VS-FORECAST DISTRIBUTION (currently unused)
# Calibrated from 1,816 forecast-vs-actual observations (Jan-Oct 2025).
# Rows = +5 to -5°F deviation from consensus forecast.
# Columns = cities. Cities without data use overall average distribution.
# Used by calculate_tail_bids() when enabled.
# =====================================================================
# ACTUALS_COLUMNS = ["Austin","Miami","Denver","Houston","Philadelphia","New York City",
#     "Chicago","Los Angeles","Atlanta","Washington DC","Phoenix","Dallas","Las Vegas",
#     "Oklahoma City","Seattle","San Francisco","San Antonio","Minneapolis","New Orleans"]
# ACTUALS_DATA = [
#     # +5°F above forecast
#     [.0228,.0194,.0536,.0402,.0498,.0383,.0536,.0418,.0402,.0402,.0402,.0402,.0402,.0402,.0402,.0402,.0402,.0402,.0402],
#     # +4°F
#     [.0114,.0349,.0268,.0352,.0421,.0192,.0651,.0418,.0352,.0352,.0352,.0352,.0352,.0352,.0352,.0352,.0352,.0352,.0352],
#     # +3°F
#     [.0532,.0659,.0651,.0672,.0805,.0536,.0843,.0669,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672],
#     # +2°F
#     [.1331,.1783,.1149,.1586,.1686,.1073,.1916,.2176,.1586,.1586,.1586,.1586,.1586,.1586,.1586,.1586,.1586,.1586,.1586],
#     # +1°F
#     [.2053,.2093,.1456,.1845,.1571,.1762,.1724,.2259,.1845,.1845,.1845,.1845,.1845,.1845,.1845,.1845,.1845,.1845,.1845],
#     # 0°F (forecast was exact)
#     [.2700,.3295,.2529,.2561,.2490,.2452,.2375,.2176,.2561,.2561,.2561,.2561,.2561,.2561,.2561,.2561,.2561,.2561,.2561],
#     # -1°F
#     [.1217,.1163,.1456,.1322,.1303,.1839,.1111,.1172,.1322,.1322,.1322,.1322,.1322,.1322,.1322,.1322,.1322,.1322,.1322],
#     # -2°F
#     [.0760,.0233,.0920,.0672,.0690,.1188,.0460,.0460,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672,.0672],
#     # -3°F
#     [.0342,.0116,.0230,.0231,.0230,.0345,.0192,.0167,.0231,.0231,.0231,.0231,.0231,.0231,.0231,.0231,.0231,.0231,.0231],
#     # -4°F
#     [.0190,.0078,.0307,.0149,.0192,.0115,.0077,.0042,.0149,.0149,.0149,.0149,.0149,.0149,.0149,.0149,.0149,.0149,.0149],
#     # -5°F below forecast
#     [.0532,.0039,.0498,.0209,.0115,.0115,.0115,.0042,.0209,.0209,.0209,.0209,.0209,.0209,.0209,.0209,.0209,.0209,.0209],
# ]
# ACTUALS_ROWS = ["5","4","3","2","1","0","-1","-2","-3","-4","-5"]


# =====================================================================
# HELPERS
# =====================================================================

def get_session_variable():
    """0 = morning (trade today), 1 = evening (trade tomorrow)."""
    ct = datetime.now(pytz.timezone("US/Central"))
    return 1 if 14 <= ct.hour < 23 else 0

def decode_private_key(b64_key="", file_path=""):
    """Load Kalshi RSA key from file (Colab/repo) or env var (GitHub Actions)."""
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f"No private key. Set KALSHI_PRIVATE_KEY or place at '{file_path}'.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

def resolve_gcp_credentials():
    """Colab: interactive login. GitHub Actions: GOOGLE_APPLICATION_CREDENTIALS env var."""
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            from google.colab import auth; auth.authenticate_user()
            log.info("Authenticated via Colab")
        except ImportError: pass

def get_city_abv(ticker):
    """'KXHIGHCHI-26MAR10-B31' → 'CHI'. Longer abbreviations checked first."""
    for key in CITY_ABV_KEYS:
        if key in ticker: return key
    return ""

def get_cancel_time(abv):
    """Order auto-cancel time for a city. Eastern = 9:05 AM CT, others = 10:05 AM CT."""
    return CITY_CANCEL_TIMES.get(abv, (10, 5))

def get_unix_time_for_target(hour, minute, variable):
    """Central Time hour:minute on target date → Unix timestamp."""
    tz = pytz.timezone("US/Central"); now = datetime.now(tz)
    target = tz.localize(datetime(now.year, now.month, now.day, hour, minute) + timedelta(days=variable))
    return int(target.timestamp())

# =====================================================================
# BIGQUERY — only creates tables for order placement
# Monitoring script (high_temp_monitoring.py) creates its own tables.
# =====================================================================

def setup_bigquery():
    from google.cloud import bigquery
    client = bigquery.Client(project=BQ_PROJECT)
    dataset_ref = bigquery.DatasetReference(BQ_PROJECT, BQ_DATASET)
    try: client.get_dataset(dataset_ref)
    except Exception:
        ds = bigquery.Dataset(dataset_ref); ds.location = "US"
        client.create_dataset(ds); log.info("Created dataset %s", BQ_DATASET)
    SF = bigquery.SchemaField
    schemas = {
        # market_snapshot: full state each run — for backtesting
        f"{BQ_TABLE_PREFIX}market_snapshot": [
            SF("city","STRING"),SF("forecast_date","DATE"),SF("run_date","TIMESTAMP"),
            SF("weather_underground","FLOAT64"),SF("accuweather","FLOAT64"),SF("nws","FLOAT64"),
            SF("forecast_avg","FLOAT64"),SF("forecast_std","FLOAT64"),SF("forecast_range","FLOAT64"),
            SF("nws_detailed_conditions","STRING"),SF("nws_short_conditions","STRING"),
            SF("midnight_temperature","FLOAT64"),SF("event_ticker","STRING"),SF("market_ticker","STRING"),
            SF("low_range","FLOAT64"),SF("high_range","FLOAT64"),SF("hi_no_price","FLOAT64"),
            SF("forecast_bias","FLOAT64"),SF("city_floor_std","FLOAT64"),SF("model_std","FLOAT64"),
            SF("yes_probability","FLOAT64"),SF("fair_no_price","FLOAT64"),
            SF("distance_from_forecast","FLOAT64"),
            SF("no_highest_bid","FLOAT64"),SF("no_lowest_offer","FLOAT64"),
            SF("no_orderbook","STRING"),SF("yes_orderbook","STRING"),SF("position","INT64"),
        ],
        # orders: every limit order placed — join to fills via client_order_id
        f"{BQ_TABLE_PREFIX}orders": [
            SF("city","STRING"),SF("forecast_date","DATE"),SF("run_date","TIMESTAMP"),
            SF("market_ticker","STRING"),SF("contracts","INT64"),SF("no_price","INT64"),
            SF("city_abv","STRING"),SF("client_order_id","STRING"),SF("expiration_ts","INT64"),
            SF("created_at","TIMESTAMP"),
        ],
    }
    for name, schema in schemas.items():
        ref = dataset_ref.table(name)
        try: client.get_table(ref)
        except Exception:
            client.create_table(bigquery.Table(ref, schema=schema))
            log.info("Created table %s", name)
    return client, schemas

def load_city_floor_std(bq_client):
    """Auto-calibrate CITY_FLOOR_STD from settled market data.
    Computes std dev of (forecast_avg - actual_temp) per city using:
      - market_snapshot for forecast_avg
      - settlements for actual temp (parsed from winning between-market ticker)
    Falls back to hardcoded CITY_FLOOR_STD for cities with < AUTO_CALIBRATE_MIN_OBS observations.
    """
    query = f"""
        WITH snapshots AS (
            SELECT city, forecast_avg, market_ticker,
                   ROW_NUMBER() OVER (PARTITION BY market_ticker ORDER BY run_date DESC) AS rn
            FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PREFIX}market_snapshot`
            WHERE forecast_avg IS NOT NULL
        ),
        actuals AS (
            SELECT market_ticker,
                   SAFE_CAST(REGEXP_EXTRACT(market_ticker, r'-B(\\d+\\.?\\d*)') AS FLOAT64) AS actual_temp
            FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PREFIX}settlements`
            WHERE UPPER(result) = 'YES' AND market_ticker LIKE '%-B%'
        )
        SELECT s.city, ROUND(STDDEV(s.forecast_avg - a.actual_temp), 1) AS floor_std, COUNT(*) AS n
        FROM snapshots s
        JOIN actuals a ON s.market_ticker = a.market_ticker
        WHERE s.rn = 1
        GROUP BY s.city
        HAVING COUNT(*) >= {AUTO_CALIBRATE_MIN_OBS}
    """
    try:
        df = bq_client.query(query).to_dataframe()
        if len(df) > 0:
            calibrated = dict(zip(df['city'], df['floor_std']))
            merged = {**CITY_FLOOR_STD, **calibrated}
            for _, row in df.iterrows():
                old = CITY_FLOOR_STD.get(row['city'], '—')
                log.info("  Auto-calibrated %s: %s → %.1f (n=%d)", row['city'], old, row['floor_std'], row['n'])
            return merged
    except Exception as e:
        log.warning("Auto-calibration failed (using defaults): %s", e)
    return CITY_FLOOR_STD


def write_to_bq(bq_client, schemas, df, table_name):
    from google.cloud import bigquery
    full = f"{BQ_TABLE_PREFIX}{table_name}" if not table_name.startswith(BQ_TABLE_PREFIX) else table_name
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{full}"
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", schema=schemas.get(full))
    try:
        job = bq_client.load_table_from_dataframe(df, table_id, job_config=cfg); job.result()
        log.info("  -> %s: %s rows", full, job.output_rows); return True
    except Exception as e:
        log.error("  -> %s ERROR: %s", full, e); return False

# =====================================================================
# FORECAST FETCHERS — each returns high temp (int °F) or "N/A"
# =====================================================================

def get_accuweather_forecast(coords, variable):
    try:
        r = requests.get("http://dataservice.accuweather.com/locations/v1/cities/geoposition/search",
                         params={"apikey":ACCUWEATHER_KEY,"q":f"{coords['lat']},{coords['lon']}"},timeout=10)
        r.raise_for_status(); key = r.json()["Key"]
        r2 = requests.get(f"http://dataservice.accuweather.com/forecasts/v1/daily/5day/{key}",
                          params={"apikey":ACCUWEATHER_KEY,"metric":"false"},timeout=10)
        r2.raise_for_status()
        return int(r2.json()["DailyForecasts"][variable]["Temperature"]["Maximum"]["Value"])
    except Exception as e: log.warning("AccuWeather error: %s", e); return "N/A"

def get_nws_forecast(coords, variable, central_time):
    try:
        r = requests.get(f"https://api.weather.gov/points/{coords['lat']},{coords['lon']}",timeout=10)
        r.raise_for_status()
        r2 = requests.get(r.json()["properties"]["forecast"],timeout=10); r2.raise_for_status()
        target = (central_time + timedelta(days=variable)).strftime("%Y-%m-%d")
        for p in r2.json()["properties"]["periods"]:
            if target in p["startTime"] and "day" in p["name"].lower(): return p["temperature"]
        return "N/A"
    except Exception as e: log.warning("NWS error: %s", e); return "N/A"

def get_wu_forecast(coords, variable):
    try:
        url = (f"https://api.weather.com/v3/wx/forecast/daily/5day"
               f"?apiKey={WEATHER_UNDERGROUND_KEY}&geocode={coords['lat']},{coords['lon']}"
               f"&format=json&units=e&language=en-US")
        r = requests.get(url, timeout=10)
        if r.status_code == 200 and "temperatureMax" in r.json(): return r.json()["temperatureMax"][variable]
        return "N/A"
    except Exception as e: log.warning("WU error: %s", e); return "N/A"

def fetch_nws_conditions(coords, variable, central_time):
    """NWS detailed + short forecast text (logged, not used in model)."""
    try:
        r = requests.get(f"https://api.weather.gov/points/{coords['lat']},{coords['lon']}",timeout=10)
        if r.status_code != 200: return None, None
        r2 = requests.get(r.json()["properties"]["forecast"],timeout=10)
        if r2.status_code != 200: return None, None
        target = (central_time + timedelta(days=variable)).strftime("%Y-%m-%d")
        for p in r2.json()["properties"]["periods"]:
            if target in p["startTime"]: return p["detailedForecast"], p["shortForecast"]
    except Exception as e: log.warning("NWS conditions error: %s", e)
    return None, None

def fetch_midnight_forecast(coords, variable, central_time):
    """NWS hourly temp at midnight — used by night filter."""
    try:
        r = requests.get(f"https://api.weather.gov/points/{coords['lat']},{coords['lon']}",timeout=10)
        if r.status_code != 200: return None
        r2 = requests.get(r.json()["properties"]["forecastHourly"],timeout=10)
        if r2.status_code != 200: return None
        target_date = central_time.date() + timedelta(days=variable)
        for p in r2.json()["properties"]["periods"]:
            start = re.sub(r"([-+]\d{2}):(\d{1})$", r"\1:0\2", p["startTime"])
            ft = datetime.fromisoformat(start).astimezone(pytz.timezone("US/Central"))
            if ft.hour == 0 and ft.minute == 0 and ft.date() == target_date: return p["temperature"]
    except Exception as e: log.warning("Midnight forecast error: %s", e)
    return None

# =====================================================================
# PIPELINE — forecast → market discovery → probability → order book
# =====================================================================

def collect_forecasts(variable, central_time):
    """Call all 3 weather APIs, compute consensus average + std dev."""
    log.info("Collecting forecasts for %d cities...", len(CITIES))
    run_date = central_time.strftime("%Y-%m-%d %H:%M:%S")
    fc_date = (central_time + timedelta(days=variable)).strftime("%Y-%m-%d")
    rows = [{"City":city,"Forecast Date":fc_date,"Run Date":run_date,
             "Weather Underground":get_wu_forecast(c,variable),
             "Accuweather":get_accuweather_forecast(c,variable),
             "NWS":get_nws_forecast(c,variable,central_time)} for city,c in CITIES.items()]
    df = pd.DataFrame(rows)
    src = ["Weather Underground","Accuweather","NWS"]
    for col in src: df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Average"] = df[src].mean(axis=1)                # Consensus forecast
    df["Standard Deviation"] = df[src].std(axis=1)       # Inter-source disagreement
    df["Highest Minus Lowest"] = df[src].max(axis=1) - df[src].min(axis=1)

    # Apply city-specific forecast bias correction
    df["Bias"] = df["City"].map(CITY_FORECAST_BIAS).fillna(0)
    df["Average"] = df["Average"] - df["Bias"]
    biased = (df["Bias"] != 0).sum()
    if biased > 0:
        log.info("Bias correction applied to %d/%d cities", biased, len(df))

    # Filter out cities with extreme forecast disagreement (unreliable)
    before = len(df)
    df = df[df["Highest Minus Lowest"] <= MAX_FORECAST_DISAGREEMENT]
    dropped = before - len(df)
    if dropped > 0:
        log.info("Dropped %d cities: forecast disagreement > %.0f°F", dropped, MAX_FORECAST_DISAGREEMENT)
    return df

def add_nws_conditions(ft, variable, central_time):
    rows = [{"City":city,**dict(zip(["NWS Detailed Conditions","NWS Short Conditions"],
             fetch_nws_conditions(c,variable,central_time)))} for city,c in CITIES.items()]
    return pd.merge(ft, pd.DataFrame(rows), on="City", how="inner")

def apply_model_std(ft, city_floor_std=None):
    """Compute Model Std Dev = max(inter_source_std, city_floor_std) + condition boost.
    This replaces raw Standard Deviation for all probability calculations.
    city_floor_std: dict of city→floor values (auto-calibrated or hardcoded defaults).
    """
    if city_floor_std is None:
        city_floor_std = CITY_FLOOR_STD

    ft["Standard Deviation"] = pd.to_numeric(ft["Standard Deviation"], errors="coerce")

    # Step 1: Apply city floor — prevents 0 std when all sources agree
    ft["City Floor Std"] = ft["City"].map(city_floor_std).fillna(1.2)
    ft["Model Std"] = ft[["Standard Deviation", "City Floor Std"]].max(axis=1)

    # Step 2: Boost for volatile weather conditions
    def has_volatile_conditions(row):
        conditions = str(row.get("NWS Detailed Conditions", "")).lower()
        return any(p in conditions for p in CONDITION_BOOST_PATTERNS)

    boost_mask = ft.apply(has_volatile_conditions, axis=1)
    ft.loc[boost_mask, "Model Std"] = ft.loc[boost_mask, "Model Std"] + CONDITION_BOOST_STD

    boosted = boost_mask.sum()
    if boosted > 0:
        log.info("Weather condition boost applied to %d/%d cities (+%.1f°F std)",
                 boosted, len(ft), CONDITION_BOOST_STD)

    return ft

def add_midnight_temps(ft, variable, central_time):
    rows = [{"City":city,"Midnight Temperature":fetch_midnight_forecast(c,variable,central_time)}
            for city,c in CITIES.items()]
    ft = pd.merge(ft, pd.DataFrame(rows), on="City", how="inner")
    ft["concat"] = ft["City"] + ft["Forecast Date"] + ("Night" if variable else "Day")
    return ft

def filter_night_session(ft, variable):
    """Drop cities where midnight temp ≈ forecast high (no swing = no edge)."""
    if variable != 1: return ft
    ft["Average"] = pd.to_numeric(ft["Average"], errors="coerce")
    ft["Midnight Temperature"] = pd.to_numeric(ft["Midnight Temperature"], errors="coerce")
    before = len(ft)
    ft = ft[abs(ft["Average"] - ft["Midnight Temperature"]) >= 4.5]
    log.info("Night filter: %d -> %d cities", before, len(ft)); return ft

def build_event_tickers(variable, central_time):
    """Build Kalshi event tickers: 'KXHIGHCHI' + '-26MAR10'."""
    t = central_time + timedelta(days=variable)
    m, d = t.strftime("%b").upper(), t.strftime("%d")
    rows = [[f"{p}-26{m}{d}", city, hi_no] for p, city, hi_no in EVENT_TICKER_DEFS]
    df = pd.DataFrame(rows, columns=["Ticker", "City", "hi_no_price"])
    return df

def pull_markets(exchange_client, et_df):
    """Discover sub-markets (-B between, -T tail) from Kalshi API."""
    rows = []
    for _, et in et_df.iterrows():
        try: resp = exchange_client.get_event(event_ticker=et["Ticker"])
        except (HttpError, Exception) as e: log.warning("Event error %s: %s", et["Ticker"], e); continue
        log.info("Fetched %s (%d mkts)", et["Ticker"], len(resp.get("markets",[])))
        for mkt in resp.get("markets",[]):
            base = {"event_ticker":et["Ticker"],"market_ticker":mkt["ticker"],"City":et["City"],
                    "hi_no_price":et["hi_no_price"]}
            if "-B" in mkt["ticker"]:
                match = re.search(r"-B(\d+(\.\d+)?)", mkt["ticker"])
                if match: n=float(match.group(1)); rows.append({**base,"low_range":n-1,"high_range":n+1})
            elif "-T" in mkt["ticker"]:
                match = re.search(r"-T(\d+(\.\d+)?)", mkt["ticker"])
                if match: n=float(match.group(1)); rows.append({**base,"low_range":0,"high_range":n-0.5})
    df = pd.DataFrame(rows)
    if df.empty: return df
    # Fix high-end tail: when -B followed by -T, that -T = upper tail (≥X°F)
    df["prev"] = df["market_ticker"].shift(1)
    for idx in df.index[1:]:
        if isinstance(df.loc[idx,"prev"],str) and "-B" in df.loc[idx,"prev"] and "-T" in df.loc[idx,"market_ticker"]:
            df.loc[idx,"low_range"] = df.loc[idx,"high_range"]+1; df.loc[idx,"high_range"] = 150
    return df.drop(columns=["prev"])

def calculate_probabilities(ft, mt):
    """Normal CDF: P(temp in bucket) using forecast avg ± model std.
    Model Std = max(inter_source_std, city_floor_std) + condition boost.
    Also computes distance from forecast center for each market.
    """
    ct = pd.merge(ft, mt, on="City", how="inner")
    ct["Average"] = pd.to_numeric(ct["Average"], errors="coerce")
    ct["Model Std"] = pd.to_numeric(ct["Model Std"], errors="coerce").replace({0:1e-6,np.nan:1e-6})
    ct["high_range"] = pd.to_numeric(ct["high_range"], errors="coerce")
    ct["low_range"] = pd.to_numeric(ct["low_range"], errors="coerce")
    ct["yes_probability"] = (norm.cdf(ct["high_range"],loc=ct["Average"],scale=ct["Model Std"])
                             - norm.cdf(ct["low_range"],loc=ct["Average"],scale=ct["Model Std"])).round(2)
    ct["fair_no_price"] = 1 - ct["yes_probability"]

    # Distance from forecast center: bucket midpoint vs forecast average
    # Between markets: midpoint = (low_range + high_range) / 2
    # Tail markets: use low_range (the threshold) since there's no real midpoint
    ct["bucket_mid"] = np.where(ct["high_range"] == 150, ct["low_range"],
                                (ct["low_range"] + ct["high_range"]) / 2)
    ct["distance_from_forecast"] = (ct["bucket_mid"] - ct["Average"]).abs()

    return ct

def pull_orderbooks(exchange_client, ct):
    """Fetch top-3 order book for every market."""
    ct["no_highest_bid"]=""; ct["no_lowest_offer"]=""; ct["no_orderbook"]=""; ct["yes_orderbook"]=""
    for idx, row in ct.iterrows():
        try:
            ob = exchange_client.get_orderbook(ticker=row["market_ticker"], depth=3)
            if ob["orderbook"]["no"]: ct.loc[idx,"no_highest_bid"] = ob["orderbook"]["no"][-1][0]
            if ob["orderbook"]["yes"]: ct.loc[idx,"no_lowest_offer"] = 100 - ob["orderbook"]["yes"][-1][0]
            ct.loc[idx,"no_orderbook"] = str(ob["orderbook"]["no"])
            ct.loc[idx,"yes_orderbook"] = str(ob["orderbook"]["yes"])
        except Exception as e: log.warning("Orderbook error %s: %s", row["market_ticker"], e)
    return ct

# --- calculate_tail_bids (currently unused — tails use hi_no_price directly) ---
# Uncomment to use historical distribution instead of hi_no_price for tail markets.
# Requires uncommenting ACTUALS_DATA, ACTUALS_COLUMNS, ACTUALS_ROWS above.
#
# def calculate_tail_bids(ct):
#     """For high-end tails (≥X°F): historical distribution → bid price with 15¢ buffer."""
#     avf = pd.DataFrame(ACTUALS_DATA, columns=ACTUALS_COLUMNS, index=ACTUALS_ROWS).T
#     avf["City"] = avf.index
#     tails = ct[ct["high_range"]==150].copy()
#     if tails.empty: return ct
#     fd = (tails["low_range"]-tails["Average"]).round()
#     chart_idx = [int(v) for v in fd.tolist()]
#     y = pd.DataFrame({"City":tails["City"].values,"low_range":tails["low_range"].values,"Average":tails["Average"].values})
#     y = pd.merge(y, avf, on="City", how="inner")
#     filtered = [y[str(c)].tolist() for c in range(5,-6,-1)]
#     pct = [sum([x[i] for x in filtered][:5-max(-5,min(5,v))+1]) for i,v in enumerate(chart_idx)]
#     y["bid_price"] = [1-x for x in pct]
#     for idx, row in ct.iterrows():
#         if row["high_range"]==150:
#             m = y.loc[y["City"]==row["City"],"bid_price"]
#             if not m.empty: ct.loc[idx,"hi_no_price"] = m.iloc[0]*100-15
#     return ct

def cancel_orders_and_pull_positions(exchange_client, ct):
    """Cancel all resting orders (clean slate) then check current positions."""
    log.info("Cancelling resting orders...")
    cancelled = 0
    for ticker in ct["market_ticker"].unique():
        try:
            for o in exchange_client.get_orders(ticker=ticker).get("orders",[]):
                if o.get("status")=="resting": exchange_client.cancel_order(order_id=o["order_id"]); cancelled+=1
        except Exception as e: log.warning("Cancel error %s: %s", ticker, e)
    log.info("Cancelled %d orders.", cancelled)
    if "position" not in ct.columns: ct["position"] = 0
    ct["resting_order_count"] = 0
    for ticker in ct["market_ticker"].unique():
        try:
            resp = exchange_client.get_positions(limit=None,cursor=None,settlement_status=None,ticker=ticker,event_ticker=None)
            if resp.get("market_positions"):
                pdf = pd.DataFrame(resp["market_positions"])
                if "position" in pdf.columns:
                    live = pdf[pdf["position"]!=0]
                    if not live.empty:
                        ri = ct.index[ct["market_ticker"]==ticker].tolist()
                        if ri: ct.loc[ri[0],"position"] = abs(live["position"].iloc[0])
        except Exception as e: log.warning("Position error %s: %s", ticker, e)
    ct["position"] = ct["position"].fillna(0).astype(int); return ct

# =====================================================================
# DIAGNOSTICS — printed for every market (traded or skipped)
# =====================================================================

def format_temp_range(low, high):
    if high == 150: return f"≥{int(low)}°F (high tail)"
    elif low == 0: return f"≤{int(high)}°F (low tail)"
    else: return f"{int(low)}–{int(high)}°F"

def classify_spread(spread):
    if spread <= 7: return "tight"
    elif spread <= 15: return "medium"
    elif spread <= 25: return "wide"
    else: return "very wide"

def print_market_diagnostic(row, market_orders, is_tail, starting, cutoff):
    ticker = row["market_ticker"]
    yes_prob = row["yes_probability"]
    hi_no = row["hi_no_price"]
    position = row.get("position", 0)
    rng = format_temp_range(row["low_range"], row["high_range"])
    no_bid = row.get("no_highest_bid", "")
    no_offer = row.get("no_lowest_offer", "")
    yes_bid = (100 - int(no_offer)) if no_offer != "" else "—"
    no_bid_str = f"{int(no_bid)}¢" if no_bid != "" else "—"
    no_offer_str = f"{int(no_offer)}¢" if no_offer != "" else "—"
    yes_bid_str = f"{yes_bid}¢" if yes_bid != "—" else "—"
    if no_bid != "" and no_offer != "":
        mid_yes = 100 - (int(no_bid) + int(no_offer)) / 2
        mid_str = f"(yes_bid={yes_bid_str} + no_bid={no_bid_str}) / 2 → mid_yes={mid_yes:.0f}¢"
        spread = int(no_offer) - int(no_bid)
        spread_str = f"{spread}¢ [{classify_spread(spread)}]"
    else:
        mid_str = "insufficient book"; spread_str = "no book"; spread = None
    fair_yes = int(round(yes_prob * 100))
    fair_no_c = int(round((1 - yes_prob) * 100))

    # Edge calculation
    market_no = int(no_offer) if no_offer != "" else None
    edge = fair_no_c - market_no if market_no is not None else None
    edge_str = f"{edge}¢" if edge is not None else "—"

    # Model std breakdown
    raw_std = row.get("Standard Deviation", 0)
    floor_std = row.get("City Floor Std", 0)
    model_std = row.get("Model Std", raw_std)

    print(f"\n{'─'*80}")
    print(f"  {ticker}:  {row['City']}  {rng}")
    print(f"    Forecast: WU={row.get('Weather Underground','—')}  AccuW={row.get('Accuweather','—')}  "
          f"NWS={row.get('NWS','—')}  → Avg={row['Average']:.1f}°F")
    bias = row.get('Bias', 0)
    if bias != 0:
        print(f"    Bias correction: {bias:+.1f}°F applied (raw avg was {row['Average']+bias:.1f}°F)")
    print(f"    Std Dev: raw={raw_std:.1f}  floor={floor_std:.1f}  → model={model_std:.1f}°F")
    dist = row.get('distance_from_forecast', 0)
    dist_label = "center" if dist <= 1 else "adjacent" if dist <= 2 else "far"
    mult_label = f"  ×{ADJACENT_MULTIPLIER}" if 1.0 <= dist <= 2.0 else ""
    print(f"    Distance from forecast: {dist:.1f}°F [{dist_label}]{mult_label}  |  contracts={starting}")
    strat = "Historical Tail" if is_tail else "Normal CDF"
    print(f"    Strategy [{strat}]: P(temp in range)={yes_prob*100:.1f}%  hi_no_price={hi_no:.0f}¢")
    print(f"    → Fair value: YES={fair_yes}¢, NO={fair_no_c}¢  |  Edge vs market: {edge_str}")
    print(f"    Orderbook: Yes={yes_bid_str}, No bid={no_bid_str}, No offer={no_offer_str}")
    print(f"    Mid-price: {mid_str}")
    print(f"    Spread: {spread_str}")
    if position > 0: print(f"    Position: {int(position)} contracts")

    if len(market_orders) == 0:
        if dist > MAX_DISTANCE_FROM_FORECAST: print(f"    → SKIP: distance={dist:.1f}°F > {MAX_DISTANCE_FROM_FORECAST}°F max")
        elif yes_prob <= cutoff and not is_tail: print(f"    → SKIP: P(yes)={yes_prob*100:.1f}% ≤ {cutoff*100:.0f}% cutoff")
        elif yes_prob >= CEILING_PROBABILITY and not is_tail: print(f"    → SKIP: P(yes)={yes_prob*100:.1f}% ≥ {CEILING_PROBABILITY*100:.0f}% ceiling")
        elif spread is not None and spread > MAX_SPREAD_CENTS: print(f"    → SKIP: spread={spread}¢ > {MAX_SPREAD_CENTS}¢ max")
        elif edge is not None and edge < MIN_EDGE_CENTS and not is_tail:
            print(f"    → SKIP: edge={edge}¢ < {MIN_EDGE_CENTS}¢ minimum")
        elif "-T" in ticker and not is_tail: print(f"    → SKIP: low-end tail (not traded)")
        elif no_offer == "" or no_bid == "": print(f"    → SKIP: no orderbook data")
        else: print(f"    → NO ORDERS: all bids failed price/position filters")
    else:
        strs = [f"{o['no_price']}¢({o['contracts']}c,ev={abs(fair_no_c-o['no_price'])}%)" for o in market_orders]
        if len(strs) > 6: strs = strs[:5] + [f"... +{len(strs)-5} more"]
        total = sum(o["contracts"] for o in market_orders)
        print(f"    → NO orders: [{', '.join(strs)}] ({total}c across {len(market_orders)} tiers)")

# =====================================================================
# ORDER PLACEMENT
# =====================================================================

def place_orders(exchange_client, ct, variable, central_time):
    """Place up to NUM_PRICE_LEVELS tiered NO limit orders per qualifying market."""
    all_orders = []
    print(f"\n{'═'*80}")
    print(f"  MARKET DIAGNOSTICS  |  {len(ct)} markets  |  contracts={CONTRACTS_PER_TIER}c  |  levels={NUM_PRICE_LEVELS}")
    print(f"  cutoff={CUTOFF_PROBABILITY*100:.0f}%  |  ceiling={CEILING_PROBABILITY*100:.0f}%  |  min_edge={MIN_EDGE_CENTS}¢  |  max_spread={MAX_SPREAD_CENTS}¢  |  max_contracts={MAX_CONTRACTS}")
    print(f"{'═'*80}")

    for idx, row in ct.iterrows():
        is_tail = "-T" in row["market_ticker"] and row["high_range"]==150
        market_orders = []

        # Edge check: model fair NO (cents) vs market NO offer
        fair_no_cents = (1 - row["yes_probability"]) * 100
        market_no = int(row["no_lowest_offer"]) if row["no_lowest_offer"] != "" else None
        edge_cents = (fair_no_cents - market_no) if market_no is not None else 0

        # Spread check
        no_bid = row.get("no_highest_bid", "")
        no_offer = row.get("no_lowest_offer", "")
        spread_cents = (int(no_offer) - int(no_bid)) if no_bid != "" and no_offer != "" else None

        # Distance from forecast center
        dist = row.get("distance_from_forecast", 0)

        qualifies = (
            row["yes_probability"] > CUTOFF_PROBABILITY                            # Min probability
            and row["yes_probability"] < CEILING_PROBABILITY                        # Max probability
            and ("-T" not in row["market_ticker"] or is_tail)                      # Skip low-end tails only
            and edge_cents >= MIN_EDGE_CENTS                                        # Min edge
            and (spread_cents is not None and spread_cents <= MAX_SPREAD_CENTS)     # Max spread
            and dist <= MAX_DISTANCE_FROM_FORECAST                                  # Max distance from forecast
        )

        # Contract sizing: 1.5x for adjacent buckets (1-2°F from center)
        if 1.0 <= dist <= 2.0:
            contracts = int(CONTRACTS_PER_TIER * ADJACENT_MULTIPLIER)
        else:
            contracts = CONTRACTS_PER_TIER

        if qualifies:
            for i in range(NUM_PRICE_LEVELS):
                inc = INCREMENT_TAIL if is_tail else INCREMENT
                bp = max(row["hi_no_price"]-i*inc, 1)
                if (row["no_lowest_offer"]!="" and bp<int(row["no_lowest_offer"])
                    and bp<int(row["no_highest_bid"])-3
                    and MAX_CONTRACTS>=row["position"]+row["resting_order_count"]+contracts):
                    abv = get_city_abv(row["market_ticker"])
                    ch,cm = get_cancel_time(abv)
                    oid = str(uuid.uuid4()); exp = get_unix_time_for_target(ch,cm,variable)

                    # Skip if cancel time already passed (e.g. running after 10:05 AM for Central cities)
                    if exp <= int(time.time()):
                        continue
                    params = {"ticker":row["market_ticker"],"client_order_id":oid,"type":"limit",
                              "action":"buy","side":"no","count":contracts,"yes_price":None,
                              "no_price":int(bp),"expiration_ts":exp,"sell_position_floor":None,"buy_max_cost":None}
                    try: exchange_client.create_order(**params)
                    except Exception as e: log.error("Order failed %s: %s", row["market_ticker"], e); continue
                    rec = {"city":row["City"],"forecast_date":row["Forecast Date"],"run_date":row["Run Date"],
                           "market_ticker":row["market_ticker"],"contracts":contracts,"no_price":int(bp),
                           "city_abv":abv,"client_order_id":oid,"expiration_ts":exp,
                           "created_at":central_time.strftime("%Y-%m-%d %H:%M:%S")}
                    market_orders.append(rec); all_orders.append(rec)
                    ct.loc[idx,"resting_order_count"] = row["resting_order_count"]+contracts
        print_market_diagnostic(row, market_orders, is_tail, contracts, CUTOFF_PROBABILITY)

    print(f"\n{'═'*80}")
    print(f"  SUMMARY: {len(all_orders)} orders placed across {len(ct)} markets")
    print(f"{'═'*80}\n")
    log.info("Placed %d orders.", len(all_orders)); return all_orders

# =====================================================================
# MAIN
# =====================================================================

def main():
    log.info("="*60)
    log.info("Kalshi High Temp Trading (%s)", "Colab" if IS_COLAB else "GitHub Actions / CLI")
    log.info("="*60)

    if not KALSHI_API_KEY_ID: log.error("KALSHI_API_KEY_ID not set."); sys.exit(1)
    if not KALSHI_PRIVATE_KEY and not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        log.error("No Kalshi private key found."); sys.exit(1)

    central_time = datetime.now(pytz.timezone("US/Central"))
    variable = get_session_variable()
    log.info("CT: %s | %s (var=%d)", central_time.strftime("%Y-%m-%d %H:%M:%S"),
             "evening" if variable else "morning", variable)

    pk = decode_private_key(b64_key=KALSHI_PRIVATE_KEY, file_path=KALSHI_PRIVATE_KEY_PATH)
    xc = ExchangeClient(exchange_api_base=KALSHI_API_BASE, key_id=KALSHI_API_KEY_ID, private_key=pk)
    st = xc.get_exchange_status(); log.info("Exchange: %s", st)
    if not st.get("trading_active"): log.warning("Trading inactive."); sys.exit(0)

    log.info("BigQuery: project=%s dataset=%s", BQ_PROJECT, BQ_DATASET)
    resolve_gcp_credentials()
    bq, schemas = setup_bigquery()

    # Auto-calibrate CITY_FLOOR_STD from settlement data (if enough observations)
    log.info("--- Auto-calibrating CITY_FLOOR_STD ---")
    calibrated_floor_std = load_city_floor_std(bq)

    # Phase 1: Forecasts
    log.info("--- PHASE 1: Forecasts ---")
    ft = collect_forecasts(variable, central_time)
    ft = add_nws_conditions(ft, variable, central_time)
    ft = apply_model_std(ft, city_floor_std=calibrated_floor_std)
    ft = add_midnight_temps(ft, variable, central_time)
    ft = filter_night_session(ft, variable)
    if ft.empty: log.warning("No cities after filters."); sys.exit(0)

    # Phase 2: Markets + probabilities
    log.info("--- PHASE 2: Markets ---")
    et = build_event_tickers(variable, central_time)
    mt = pull_markets(xc, et)
    if mt.empty: log.warning("No markets."); sys.exit(0)
    log.info("%d sub-markets found.", len(mt))
    ct = pull_orderbooks(xc, calculate_probabilities(ft, mt))
    # ct = calculate_tail_bids(ct)  # Uncomment to use historical distribution for tail pricing

    # Phase 3: Cancel + positions
    log.info("--- PHASE 3: Positions ---")
    ct = cancel_orders_and_pull_positions(xc, ct)

    # Phase 4: Write snapshot to BQ
    log.info("--- PHASE 4: Snapshot ---")
    snap = ct.rename(columns={"City":"city","Forecast Date":"forecast_date","Run Date":"run_date",
        "Weather Underground":"weather_underground","Accuweather":"accuweather","NWS":"nws",
        "Average":"forecast_avg","Standard Deviation":"forecast_std","Highest Minus Lowest":"forecast_range",
        "NWS Detailed Conditions":"nws_detailed_conditions","NWS Short Conditions":"nws_short_conditions",
        "Midnight Temperature":"midnight_temperature","Bias":"forecast_bias",
        "City Floor Std":"city_floor_std","Model Std":"model_std"}).copy()
    bq_cols = [f.name for f in schemas[f"{BQ_TABLE_PREFIX}market_snapshot"]]
    snap = snap[[c for c in bq_cols if c in snap.columns]]
    snap["forecast_date"]=pd.to_datetime(snap["forecast_date"]); snap["run_date"]=pd.to_datetime(snap["run_date"])
    for c in ["weather_underground","accuweather","nws","forecast_avg","forecast_std","forecast_range",
              "midnight_temperature","low_range","high_range","hi_no_price","forecast_bias","city_floor_std","model_std",
              "yes_probability","fair_no_price","distance_from_forecast","no_highest_bid","no_lowest_offer"]:
        if c in snap.columns: snap[c]=pd.to_numeric(snap[c],errors="coerce")
    if "position" in snap.columns: snap["position"]=pd.to_numeric(snap["position"],errors="coerce").fillna(0).astype("Int64")
    for c in ["no_orderbook","yes_orderbook","nws_detailed_conditions","nws_short_conditions"]:
        if c in snap.columns: snap[c]=snap[c].astype(str)
    write_to_bq(bq, schemas, snap, "market_snapshot")

    # Phase 5: Place orders
    log.info("--- PHASE 5: Orders ---")
    orders = place_orders(xc, ct, variable, central_time)
    if orders:
        odf = pd.DataFrame(orders)
        for c in ["forecast_date","run_date","created_at"]: odf[c]=pd.to_datetime(odf[c])
        write_to_bq(bq, schemas, odf, "orders")

    log.info("="*60); log.info("Trading run complete."); log.info("="*60)

if __name__ == "__main__":
    main()
