#!/usr/bin/env python3
"""
Kalshi Weather Temperature Trading Bot
=======================================
Trades NO contracts on Kalshi daily high temperature markets (KXHIGH series)
across 20 US cities. Uses forecast consensus from NWS, Weather Underground,
and AccuWeather to model probabilities and place tiered limit orders.

Runs in two environments:
  - Google Colab: interactive GCP auth, reads Kalshi key from file
  - GitHub Actions: reads all credentials from base64-encoded env vars

GitHub Actions secrets:
    KALSHI_API_KEY_ID       - Kalshi API key ID
    KALSHI_PRIVATE_KEY      - Base64-encoded RSA private key (PEM)
    GCP_PROJECT_ID          - Google Cloud project ID for BigQuery
    GCP_DATASET_ID          - BigQuery dataset name
    WEATHER_UNDERGROUND_KEY - Weather Underground (weather.com) API key
    ACCUWEATHER_KEY         - AccuWeather API key
"""

# ---------------------------------------------------------------------------
# Imports — all at top, including Kalshi client
# ---------------------------------------------------------------------------
import os
import sys
import re
import json
import uuid
import base64
import logging
from datetime import datetime, timedelta

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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kalshi_weather_bot")

# ---------------------------------------------------------------------------
# Configuration — env vars with Colab defaults
# ---------------------------------------------------------------------------
KALSHI_API_KEY_ID      = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "/content/Lisa_Kalshi.txt")
KALSHI_PRIVATE_KEY_B64 = os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_API_BASE        = "https://api.elections.kalshi.com/trade-api/v2"

BQ_PROJECT          = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
BQ_DATASET          = os.environ.get("GCP_DATASET_ID", "Kalshi")

WEATHER_UNDERGROUND_KEY = os.environ.get("WEATHER_UNDERGROUND_KEY", "a828c2a178844147a8c2a17884a147a5")
ACCUWEATHER_KEY         = os.environ.get("ACCUWEATHER_KEY", "lEl0lfAft6PncVXwatr92Y2YjGJL5YKs")

BQ_TABLE_PREFIX = "KXHIGH_"

# ---------------------------------------------------------------------------
# City coordinates — single source of truth
# Kalshi settles on NWS CLI from these METAR/ASOS stations
# ---------------------------------------------------------------------------
CITIES = {
    "Austin":         {"lat": 30.18304, "lon": -97.67987},   # KAUS Bergstrom
    "Miami":          {"lat": 25.79056, "lon": -80.31639},   # KMIA Miami Intl
    "Houston":        {"lat": 29.64542, "lon": -95.27889},   # KHOU Hobby
    "Denver":         {"lat": 39.84658, "lon": -104.65622},  # KDEN Denver Intl
    "New York City":  {"lat": 40.78333, "lon": -73.96667},   # KNYC Central Park
    "Philadelphia":   {"lat": 39.87327, "lon": -75.22678},   # KPHL Phila Intl
    "Chicago":        {"lat": 41.78417, "lon": -87.75528},   # KMDW Midway
    "Los Angeles":    {"lat": 33.94250, "lon": -118.40806},  # KLAX
    "Atlanta":        {"lat": 33.64068, "lon": -84.42694},   # KATL Hartsfield
    "Washington DC":  {"lat": 38.85208, "lon": -77.03772},   # KDCA Reagan
    "Phoenix":        {"lat": 33.43722, "lon": -112.00778},  # KPHX Sky Harbor
    "Dallas":         {"lat": 32.89681, "lon": -97.03781},   # KDFW
    "Las Vegas":      {"lat": 36.08000, "lon": -115.15222},  # KLAS Harry Reid
    "Oklahoma City":  {"lat": 35.39306, "lon": -97.60056},   # KOKC Will Rogers
    "Seattle":        {"lat": 47.44889, "lon": -122.30917},  # KSEA Sea-Tac
    "San Francisco":  {"lat": 37.61961, "lon": -122.36558},  # KSFO
    "San Antonio":    {"lat": 29.53389, "lon": -98.46917},   # KSAT
    "Minneapolis":    {"lat": 44.88306, "lon": -93.22889},   # KMSP
    "New Orleans":    {"lat": 29.99333, "lon": -90.25806},   # KMSY Armstrong
}

EVENT_TICKER_DEFS = [
    ("KXHIGHCHI",  "Chicago",       56, 1.7), ("KXHIGHNY",   "New York City", 41, 1.5),
    ("KXHIGHDEN",  "Denver",        60, 2.4), ("KXHIGHPHIL", "Philadelphia",  47, 1.6),
    ("KXHIGHAUS",  "Austin",        60, 1.7), ("KXHIGHMIA",  "Miami",         46, 1.1),
    ("KXHIGHLAX",  "Los Angeles",   55, 2.0), ("KXHIGHTATL", "Atlanta",       55, 1.8),
    ("KXHIGHTDC",  "Washington DC", 50, 1.6), ("KXHIGHTPHX", "Phoenix",       55, 1.5),
    ("KXHIGHTDAL", "Dallas",        55, 1.8), ("KXHIGHTLV",  "Las Vegas",     55, 1.8),
    ("KXHIGHTOKC", "Oklahoma City", 55, 2.0), ("KXHIGHTSEA", "Seattle",       50, 1.5),
    ("KXHIGHTSFO", "San Francisco", 50, 1.5), ("KXHIGHTHOU", "Houston",       59, 2.0),
    ("KXHIGHTSATX","San Antonio",   55, 1.8), ("KXHIGHTMIN", "Minneapolis",   55, 2.0),
    ("KXHIGHTNOLA","New Orleans",   55, 1.8),
]

CITY_CANCEL_TIMES = {
    "CHI":(10,5),"AUS":(10,5),"HOU":(10,5),"DEN":(10,5),"NY-":(9,5),"PHI":(9,5),
    "MIA":(9,5),"LAX":(10,5),"ATL":(9,5),"TDC":(9,5),"PHX":(10,5),"DAL":(10,5),
    "TLV":(10,5),"OKC":(10,5),"SEA":(10,5),"SFO":(10,5),
    "THOU":(10,5),"SATX":(10,5),"TMIN":(10,5),"NOLA":(10,5),
}

# Longer abbreviations first to avoid partial matches (THOU before HOU)
CITY_ABV_KEYS = ["THOU","SATX","TMIN","NOLA","CHI","AUS","DEN","NY-","PHI","MIA",
                 "LAX","ATL","TDC","PHX","DAL","TLV","OKC","SEA","SFO","HOU"]

ACTUALS_COLUMNS = ["Austin","Miami","Denver","Houston","Philadelphia","New York City",
    "Chicago","Los Angeles","Atlanta","Washington DC","Phoenix","Dallas","Las Vegas",
    "Oklahoma City","Seattle","San Francisco","San Antonio","Minneapolis","New Orleans"]
ACTUALS_DATA = [
    [.0270,.0256,.1290,.0000,.0000,.0270,.0333,.0667,.0300,.0270,.0300,.0270,.0300,.0400,.0300,.0300,.0270,.0400,.0300],
    [.0270,.0000,.0000,.0000,.0263,.0000,.0333,.0000,.0300,.0263,.0300,.0270,.0300,.0300,.0300,.0300,.0270,.0300,.0300],
    [.0811,.0000,.0645,.1613,.1579,.1081,.1333,.0000,.0800,.1081,.0600,.0811,.0600,.1000,.0600,.0600,.0811,.1000,.0800],
    [.2432,.2308,.1290,.1935,.1053,.1622,.2333,.0000,.2000,.1622,.1500,.2432,.1500,.1800,.1500,.1500,.2432,.1800,.2000],
    [.1892,.2051,.1935,.1935,.2895,.2162,.3333,.2667,.2200,.2162,.2500,.1892,.2500,.2200,.2500,.2500,.1892,.2200,.2200],
    [.1351,.2821,.1935,.0645,.1053,.2162,.0667,.2667,.1800,.2162,.2200,.1351,.2200,.1500,.2200,.2200,.1351,.1500,.1800],
    [.1892,.1282,.0645,.2258,.1053,.0811,.1000,.1333,.1200,.1053,.1200,.1892,.1200,.1000,.1200,.1200,.1892,.1000,.1200],
    [.0270,.0769,.0645,.0968,.1053,.1622,.0000,.1333,.0600,.0800,.0600,.0270,.0600,.0500,.0600,.0600,.0270,.0500,.0600],
    [.0541,.0256,.0968,.0323,.0526,.0000,.0000,.0667,.0400,.0300,.0400,.0541,.0400,.0300,.0400,.0400,.0541,.0300,.0400],
    [.0000,.0000,.0323,.0000,.0263,.0000,.0333,.0000,.0100,.0263,.0100,.0000,.0100,.0200,.0100,.0100,.0000,.0200,.0100],
    [.0270,.0256,.0323,.0323,.0263,.0270,.0333,.0667,.0300,.0263,.0300,.0270,.0300,.0300,.0300,.0300,.0270,.0300,.0300],
]
ACTUALS_ROWS = ["5","4","3","2","1","0","-1","-2","-3","-4","-5"]

# ===========================================================================
# Helpers
# ===========================================================================
def get_session_variable():
    ct = datetime.now(pytz.timezone("US/Central"))
    return 1 if 14 <= ct.hour < 23 else 0

def decode_private_key(b64_key="", file_path=""):
    if b64_key:
        pem = base64.b64decode(b64_key)
    elif file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    else:
        raise FileNotFoundError(f"No private key. Set KALSHI_PRIVATE_KEY_B64 or place at '{file_path}'.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

def resolve_gcp_credentials():
    """Authenticate to GCP. Colab: interactive login. GitHub Actions: uses ADC."""
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            from google.colab import auth
            auth.authenticate_user()
            log.info("Authenticated via Colab")
        except ImportError:
            pass

def get_city_abv(ticker):
    for key in CITY_ABV_KEYS:
        if key in ticker: return key
    return ""

def get_cancel_time(abv, central_time):
    if central_time.hour > 10 or central_time.hour < 6: return 1, 59
    return CITY_CANCEL_TIMES.get(abv, (10, 5))

def get_unix_time_for_target(hour, minute, variable):
    tz = pytz.timezone("US/Central"); now = datetime.now(tz)
    target = tz.localize(datetime(now.year, now.month, now.day, hour, minute) + timedelta(days=variable))
    return int(target.timestamp())

def build_yesterday_tickers(central_time, variable):
    y = central_time - timedelta(days=variable)
    m, d = y.strftime("%b").upper(), y.strftime("%d")
    return [(f"{p}-26{m}{d}", get_city_abv(p)) for p, *_ in EVENT_TICKER_DEFS]

# ===========================================================================
# BigQuery
# ===========================================================================
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
        f"{BQ_TABLE_PREFIX}market_snapshot": [
            SF("city","STRING"),SF("forecast_date","DATE"),SF("run_date","TIMESTAMP"),
            SF("weather_underground","FLOAT64"),SF("accuweather","FLOAT64"),SF("nws","FLOAT64"),
            SF("forecast_avg","FLOAT64"),SF("forecast_std","FLOAT64"),SF("forecast_range","FLOAT64"),
            SF("nws_detailed_conditions","STRING"),SF("nws_short_conditions","STRING"),
            SF("midnight_temperature","FLOAT64"),SF("event_ticker","STRING"),SF("market_ticker","STRING"),
            SF("low_range","FLOAT64"),SF("high_range","FLOAT64"),SF("hi_no_price","FLOAT64"),
            SF("historical_var","FLOAT64"),SF("historical_var_sqrt","FLOAT64"),
            SF("yes_probability","FLOAT64"),SF("fair_no_price","FLOAT64"),
            SF("no_highest_bid","FLOAT64"),SF("no_lowest_offer","FLOAT64"),
            SF("no_orderbook","STRING"),SF("yes_orderbook","STRING"),SF("position","INT64"),
        ],
        f"{BQ_TABLE_PREFIX}orders": [
            SF("city","STRING"),SF("forecast_date","DATE"),SF("run_date","TIMESTAMP"),
            SF("market_ticker","STRING"),SF("contracts","INT64"),SF("no_price","INT64"),
            SF("city_abv","STRING"),SF("client_order_id","STRING"),SF("expiration_ts","INT64"),
            SF("created_at","TIMESTAMP"),
        ],
        f"{BQ_TABLE_PREFIX}fills": [
            SF("trade_id","STRING"),SF("ticker","STRING"),SF("event_ticker","STRING"),
            SF("side","STRING"),SF("action","STRING"),SF("count","INT64"),
            SF("yes_price","INT64"),SF("no_price","INT64"),SF("taker_side","STRING"),
            SF("created_time_utc","TIMESTAMP"),SF("created_time_central","TIMESTAMP"),
            SF("order_id","STRING"),SF("concat_key","STRING"),
        ],
        f"{BQ_TABLE_PREFIX}settlements": [
            SF("ticker","STRING"),SF("market_result","STRING"),SF("city_abv","STRING"),
            SF("trade_date","DATE"),SF("settled_time","TIMESTAMP"),
            SF("yes_count","INT64"),SF("no_count","INT64"),
            SF("yes_total_cost","FLOAT64"),SF("no_total_cost","FLOAT64"),
            SF("revenue","FLOAT64"),SF("profit","FLOAT64"),
        ],
        f"{BQ_TABLE_PREFIX}finalized_markets": [
            SF("event_ticker","STRING"),SF("market_ticker","STRING"),SF("city_abv","STRING"),
            SF("volume_traded","INT64"),SF("avg_trade_price_yes","FLOAT64"),
            SF("avg_trade_price_no","FLOAT64"),SF("pct_volume_yes_taker","FLOAT64"),
            SF("pct_volume_no_taker","FLOAT64"),SF("trade_date","DATE"),
            SF("winning_temp","STRING"),SF("winner","STRING"),
        ],
    }
    for name, schema in schemas.items():
        ref = dataset_ref.table(name)
        try: client.get_table(ref)
        except Exception:
            client.create_table(bigquery.Table(ref, schema=schema))
            log.info("Created table %s", name)
    return client, schemas

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

# ===========================================================================
# Forecast fetchers — all use CITIES dict directly
# ===========================================================================
def get_accuweather_forecast(coords, variable):
    try:
        r = requests.get("http://dataservice.accuweather.com/locations/v1/cities/geoposition/search",
                         params={"apikey":ACCUWEATHER_KEY,"q":f"{coords['lat']},{coords['lon']}"},timeout=10)
        r.raise_for_status(); key = r.json()["Key"]
        r2 = requests.get(f"http://dataservice.accuweather.com/forecasts/v1/daily/5day/{key}",
                          params={"apikey":ACCUWEATHER_KEY,"metric":"false"},timeout=10)
        r2.raise_for_status()
        return int(r2.json()["DailyForecasts"][variable]["Temperature"]["Maximum"]["Value"])
    except Exception as e:
        log.warning("AccuWeather error: %s", e); return "N/A"

def get_nws_forecast(coords, variable, central_time):
    try:
        r = requests.get(f"https://api.weather.gov/points/{coords['lat']},{coords['lon']}",timeout=10)
        r.raise_for_status()
        r2 = requests.get(r.json()["properties"]["forecast"],timeout=10); r2.raise_for_status()
        target = (central_time + timedelta(days=variable)).strftime("%Y-%m-%d")
        for p in r2.json()["properties"]["periods"]:
            if target in p["startTime"] and "day" in p["name"].lower(): return p["temperature"]
        return "N/A"
    except Exception as e:
        log.warning("NWS error: %s", e); return "N/A"

def get_wu_forecast(coords, variable):
    try:
        url = (f"https://api.weather.com/v3/wx/forecast/daily/5day"
               f"?apiKey={WEATHER_UNDERGROUND_KEY}&geocode={coords['lat']},{coords['lon']}"
               f"&format=json&units=e&language=en-US")
        r = requests.get(url, timeout=10)
        if r.status_code == 200 and "temperatureMax" in r.json(): return r.json()["temperatureMax"][variable]
        return "N/A"
    except Exception as e:
        log.warning("WU error: %s", e); return "N/A"

def fetch_nws_conditions(coords, variable, central_time):
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

# ===========================================================================
# Core pipeline
# ===========================================================================
def collect_forecasts(variable, central_time):
    log.info("Collecting forecasts for %d cities...", len(CITIES))
    run_date = central_time.strftime("%Y-%m-%d %H:%M:%S")
    fc_date = (central_time + timedelta(days=variable)).strftime("%Y-%m-%d")
    rows = [{"City":city,"Forecast Date":fc_date,"Run Date":run_date,
             "Weather Underground":get_wu_forecast(c,variable),
             "Accuweather":get_accuweather_forecast(c,variable),
             "NWS":get_nws_forecast(c,variable,central_time)} for city,c in CITIES.items()]
    df = pd.DataFrame(rows)
    for col in ["Weather Underground","Accuweather","NWS"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    src = ["Weather Underground","Accuweather","NWS"]
    df["Average"] = df[src].mean(axis=1)
    df["Standard Deviation"] = df[src].std(axis=1)
    df["Highest Minus Lowest"] = df[src].max(axis=1) - df[src].min(axis=1)
    return df

def add_nws_conditions(ft, variable, central_time):
    rows = [{"City":city,**dict(zip(["NWS Detailed Conditions","NWS Short Conditions"],
             fetch_nws_conditions(c,variable,central_time)))} for city,c in CITIES.items()]
    return pd.merge(ft, pd.DataFrame(rows), on="City", how="inner")

def add_midnight_temps(ft, variable, central_time):
    rows = [{"City":city,"Midnight Temperature":fetch_midnight_forecast(c,variable,central_time)}
            for city,c in CITIES.items()]
    ft = pd.merge(ft, pd.DataFrame(rows), on="City", how="inner")
    ft["concat"] = ft["City"] + ft["Forecast Date"] + ("Night" if variable else "Day")
    return ft

def filter_night_session(ft, variable):
    if variable != 1: return ft
    ft["Average"] = pd.to_numeric(ft["Average"], errors="coerce")
    ft["Midnight Temperature"] = pd.to_numeric(ft["Midnight Temperature"], errors="coerce")
    before = len(ft)
    ft = ft[abs(ft["Average"] - ft["Midnight Temperature"]) >= 4.5]
    log.info("Night filter: %d -> %d cities", before, len(ft)); return ft

def build_event_tickers(variable, central_time):
    t = central_time + timedelta(days=variable)
    m, d = t.strftime("%b").upper(), t.strftime("%d")
    rows = [[f"{p}-26{m}{d}",city,hi,v] for p,city,hi,v in EVENT_TICKER_DEFS]
    df = pd.DataFrame(rows, columns=["Ticker","City","hi_no_price","var"])
    df["var_sqrt"] = np.sqrt(df["var"]); return df

def pull_markets(exchange_client, et_df):
    rows = []
    for _, et in et_df.iterrows():
        try: resp = exchange_client.get_event(event_ticker=et["Ticker"])
        except (HttpError, Exception) as e:
            log.warning("Event error %s: %s", et["Ticker"], e); continue
        log.info("Fetched %s (%d mkts)", et["Ticker"], len(resp.get("markets",[])))
        for mkt in resp.get("markets",[]):
            base = {"event_ticker":et["Ticker"],"market_ticker":mkt["ticker"],"City":et["City"],
                    "hi_no_price":et["hi_no_price"],"var":et["var"],"var_sqrt":et["var_sqrt"]}
            if "-B" in mkt["ticker"]:
                m = re.search(r"-B(\d+(\.\d+)?)", mkt["ticker"])
                if m: n=float(m.group(1)); rows.append({**base,"low_range":n-1,"high_range":n+1})
            elif "-T" in mkt["ticker"]:
                m = re.search(r"-T(\d+(\.\d+)?)", mkt["ticker"])
                if m: n=float(m.group(1)); rows.append({**base,"low_range":0,"high_range":n-0.5})
    df = pd.DataFrame(rows)
    if df.empty: return df
    df["prev"] = df["market_ticker"].shift(1)
    for idx in df.index[1:]:
        if isinstance(df.loc[idx,"prev"],str) and "-B" in df.loc[idx,"prev"] and "-T" in df.loc[idx,"market_ticker"]:
            df.loc[idx,"low_range"] = df.loc[idx,"high_range"]+1; df.loc[idx,"high_range"] = 150
    return df.drop(columns=["prev"])

def calculate_probabilities(ft, mt):
    ct = pd.merge(ft, mt, on="City", how="inner")
    ct["Average"] = pd.to_numeric(ct["Average"], errors="coerce")
    ct["Standard Deviation"] = pd.to_numeric(ct["Standard Deviation"], errors="coerce").replace({0:1e-6,np.nan:1e-6})
    ct["high_range"] = pd.to_numeric(ct["high_range"], errors="coerce")
    ct["low_range"] = pd.to_numeric(ct["low_range"], errors="coerce")
    ct["yes_probability"] = (norm.cdf(ct["high_range"],loc=ct["Average"],scale=ct["Standard Deviation"])
                             - norm.cdf(ct["low_range"],loc=ct["Average"],scale=ct["Standard Deviation"])).round(2)
    ct["fair_no_price"] = 1 - ct["yes_probability"]; return ct

def pull_orderbooks(exchange_client, ct):
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

def calculate_tail_bids(ct):
    avf = pd.DataFrame(ACTUALS_DATA, columns=ACTUALS_COLUMNS, index=ACTUALS_ROWS).T
    avf["City"] = avf.index
    tails = ct[ct["high_range"]==150].copy()
    if tails.empty: return ct
    fd = (tails["low_range"]-tails["Average"]).round()
    chart_idx = [int(v) for v in fd.tolist()]
    y = pd.DataFrame({"City":tails["City"].values,"low_range":tails["low_range"].values,
                       "Average":tails["Average"].values})
    y = pd.merge(y, avf, on="City", how="inner")
    filtered = [y[str(c)].tolist() for c in range(5,-6,-1)]
    pct = [sum([x[i] for x in filtered][:5-max(-5,min(5,v))+1]) for i,v in enumerate(chart_idx)]
    y["bid_price"] = [1-x for x in pct]
    for idx, row in ct.iterrows():
        if row["high_range"]==150:
            m = y.loc[y["City"]==row["City"],"bid_price"]
            if not m.empty: ct.loc[idx,"hi_no_price"] = m.iloc[0]*100-15
    return ct

def cancel_orders_and_pull_positions(exchange_client, ct):
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

def format_temp_range(low, high):
    """Human-readable temperature range label."""
    if high == 150:
        return f"≥{int(low)}°F (high tail)"
    elif low == 0:
        return f"≤{int(high)}°F (low tail)"
    else:
        return f"{int(low)}–{int(high)}°F"

def classify_spread(spread):
    if spread <= 7: return "tight"
    elif spread <= 15: return "medium"
    elif spread <= 25: return "wide"
    else: return "very wide"

def print_market_diagnostic(row, market_orders, is_tail, increment, starting, cutoff):
    """Print contextual diagnostic for a single market."""
    ticker = row["market_ticker"]
    city = row["City"]
    avg_temp = row["Average"]
    std_dev = row["Standard Deviation"]
    yes_prob = row["yes_probability"]
    fair_no = row.get("fair_no_price", 1 - yes_prob)
    hi_no = row["hi_no_price"]
    position = row.get("position", 0)
    rng = format_temp_range(row["low_range"], row["high_range"])

    # Orderbook
    no_bid = row.get("no_highest_bid", "")
    no_offer = row.get("no_lowest_offer", "")
    yes_bid = (100 - int(no_offer)) if no_offer != "" else "—"
    no_bid_str = f"{int(no_bid)}¢" if no_bid != "" else "—"
    no_offer_str = f"{int(no_offer)}¢" if no_offer != "" else "—"
    yes_bid_str = f"{yes_bid}¢" if yes_bid != "—" else "—"

    # Mid-price
    if no_bid != "" and no_offer != "":
        mid_no = (int(no_bid) + int(no_offer)) / 2
        mid_yes = 100 - mid_no
        mid_str = f"(yes_bid={yes_bid_str} + no_bid={no_bid_str}) / 2 → mid_yes={mid_yes:.0f}¢"
    else:
        mid_str = "insufficient book"

    # Spread
    if no_bid != "" and no_offer != "":
        spread = int(no_offer) - int(no_bid)
        book_sum = int(no_bid) + (100 - int(no_offer))
        spread_str = f"{spread}¢ (no_bid={no_bid_str} + yes_bid={yes_bid_str} = {book_sum}¢)"
        spread_class = classify_spread(spread)
    else:
        spread_str = "no book"
        spread_class = "no book"

    # Forecast sources
    wu = row.get("Weather Underground", "—")
    accu = row.get("Accuweather", "—")
    nws = row.get("NWS", "—")

    # Print header
    print(f"\n{'─'*80}")
    print(f"  {ticker}:  {city}  {rng}")

    # Forecast line
    print(f"    Forecast: WU={wu}  AccuW={accu}  NWS={nws}  "
          f"→ Avg={avg_temp:.1f}°F ± {std_dev:.1f}°F")

    # Model probability
    if is_tail:
        print(f"    Strategy [Historical Tail]: P(temp in range)={yes_prob*100:.1f}%  "
              f"hi_no_price={hi_no:.0f}¢ (incl 15¢ buffer)")
    else:
        print(f"    Strategy [Normal CDF]: P(temp in range)={yes_prob*100:.1f}%  "
              f"hi_no_price={hi_no:.0f}¢")

    # Fair values
    fair_yes = int(round(yes_prob * 100))
    fair_no_c = int(round((1 - yes_prob) * 100))
    print(f"    → Fair value: YES={fair_yes}¢, NO={fair_no_c}¢ (sum=100¢)")

    # Orderbook
    print(f"    Orderbook: Yes={yes_bid_str}, No bid={no_bid_str}, No offer={no_offer_str}")
    print(f"    Mid-price: {mid_str}")
    print(f"    Spread: {spread_str}  [{spread_class}]")

    # Position
    if position > 0:
        print(f"    Position: {int(position)} contracts")

    # Skip reason
    skipped = len(market_orders) == 0
    if skipped:
        if yes_prob <= cutoff and not is_tail:
            print(f"    → SKIP: P(yes)={yes_prob*100:.1f}% ≤ {cutoff*100:.0f}% cutoff")
        elif "-T" in ticker and not is_tail:
            print(f"    → SKIP: low-end tail market (not traded)")
        elif no_offer == "" or no_bid == "":
            print(f"    → SKIP: no orderbook data")
        else:
            print(f"    → NO ORDERS: all bids failed price/position filters")
    else:
        # Format order list
        order_strs = []
        total_contracts = 0
        for o in market_orders:
            ev_pct = abs(fair_no_c - o["no_price"])
            order_strs.append(f"{o['no_price']}¢({o['contracts']}c,ev={ev_pct}%)")
            total_contracts += o["contracts"]
        if len(order_strs) > 6:
            shown = order_strs[:5] + [f"... +{len(order_strs)-5} more"]
        else:
            shown = order_strs
        print(f"    → NO orders: [{', '.join(shown)}] (placed: {total_contracts}c across {len(market_orders)} tiers)")


def place_orders(exchange_client, ct, variable, central_time):
    increment, increment_tail = 3, 6
    starting = 75 if (central_time.hour > 10 or central_time.hour < 6) else 50
    max_contracts, cutoff = 1000, 0.2
    all_orders = []

    print(f"\n{'═'*80}")
    print(f"  MARKET DIAGNOSTICS  |  {len(ct)} markets  |  starting={starting}c  |  cutoff={cutoff*100:.0f}%")
    print(f"{'═'*80}")

    for idx, row in ct.iterrows():
        is_tail = "-T" in row["market_ticker"] and row["high_range"]==150
        market_orders = []

        # Check if this market qualifies at all
        qualifies = (row["yes_probability"] > cutoff or is_tail) and (
            "-T" not in row["market_ticker"] or is_tail)

        if qualifies:
            i1 = 0
            for i in range(25):
                bp = max(row["hi_no_price"]-i*(increment_tail if is_tail else increment),1)
                if (row["no_lowest_offer"]!="" and bp<int(row["no_lowest_offer"])
                    and bp<int(row["no_highest_bid"])-3
                    and max_contracts>=row["position"]+row["resting_order_count"]+starting+i*10):
                    contracts = starting+i1*10; i1+=1
                    abv = get_city_abv(row["market_ticker"])
                    ch,cm = get_cancel_time(abv, central_time)
                    oid = str(uuid.uuid4()); exp = get_unix_time_for_target(ch,cm,variable)
                    params = {"ticker":row["market_ticker"],"client_order_id":oid,"type":"limit",
                              "action":"buy","side":"no","count":contracts,"yes_price":None,
                              "no_price":int(bp),"expiration_ts":exp,"sell_position_floor":None,"buy_max_cost":None}
                    try:
                        exchange_client.create_order(**params)
                    except Exception as e:
                        log.error("Order failed %s: %s", row["market_ticker"], e); continue

                    order_rec = {"city":row["City"],"forecast_date":row["Forecast Date"],
                        "run_date":row["Run Date"],"market_ticker":row["market_ticker"],
                        "contracts":contracts,"no_price":int(bp),"city_abv":abv,
                        "client_order_id":oid,"expiration_ts":exp,
                        "created_at":central_time.strftime("%Y-%m-%d %H:%M:%S")}
                    market_orders.append(order_rec)
                    all_orders.append(order_rec)
                    ct.loc[idx,"resting_order_count"] = row["resting_order_count"]+contracts

        # Print diagnostic for every market
        print_market_diagnostic(row, market_orders, is_tail, increment, starting, cutoff)

    print(f"\n{'═'*80}")
    print(f"  SUMMARY: {len(all_orders)} orders placed across {len(ct)} markets")
    print(f"{'═'*80}\n")

    log.info("Placed %d orders.", len(all_orders))
    return all_orders

def collect_settlements(exchange_client, variable, central_time):
    if variable != 1: return []
    date = central_time.date()-timedelta(days=variable)
    month, day = date.strftime("%b").upper(), date.strftime("%d")
    try: resp = exchange_client.get_portfolio_settlements(limit=1000,cursor=None)
    except Exception as e: log.error("Settlement error: %s",e); return []
    rows = []
    for s in resp.get("settlements",[]):
        if s["yes_total_cost"]<=0 and s["no_total_cost"]<=0: continue
        if "HIGH" not in s["ticker"] or day not in s["ticker"] or month not in s["ticker"]: continue
        sdt = datetime.strptime(s["settled_time"],"%Y-%m-%dT%H:%M:%S.%fZ")
        tdt = sdt-timedelta(days=variable)
        rows.append({"ticker":s["ticker"],"market_result":s.get("market_result",""),
            "city_abv":get_city_abv(s["ticker"]),"trade_date":tdt.strftime("%Y-%m-%d"),
            "settled_time":s["settled_time"],"yes_count":s.get("yes_count",0),
            "no_count":s.get("no_count",0),"yes_total_cost":s["yes_total_cost"],
            "no_total_cost":s["no_total_cost"],"revenue":s["revenue"],
            "profit":s["revenue"]-s["yes_total_cost"]-s["no_total_cost"]})
    log.info("Found %d settlements.", len(rows)); return rows

def collect_finalized_markets(exchange_client, variable, central_time):
    if variable != 1: return []
    tickers = build_yesterday_tickers(central_time, variable)
    trade_date = (central_time-timedelta(days=variable)).strftime("%Y-%m-%d")
    rows = []
    for ticker, abv in tickers:
        try: resp = exchange_client.get_event(event_ticker=ticker)
        except (HttpError,Exception) as e: log.warning("Finalized error %s: %s",ticker,e); continue
        for mkt in resp.get("markets",[]):
            wt, w = (mkt.get("expiration_value"),"yes") if mkt.get("result")=="yes" else (None,None)
            try: tr = exchange_client.get_trades(limit=None,cursor=None,ticker=mkt["ticker"])
            except (HttpError,Exception) as e: log.warning("Trades error %s: %s",mkt["ticker"],e); continue
            vol=ay=an=py=pn=0
            for t in tr.get("trades",[]):
                vol+=t["count"]; ay+=t["yes_price"]*t["count"]; an+=t["no_price"]*t["count"]
                if t["taker_side"]=="yes": py+=t["count"]
                elif t["taker_side"]=="no": pn+=t["count"]
            if vol>0: ay/=vol; an/=vol; py/=vol; pn/=vol
            rows.append({"event_ticker":ticker,"market_ticker":mkt["ticker"],"city_abv":abv,
                "volume_traded":vol,"avg_trade_price_yes":ay,"avg_trade_price_no":an,
                "pct_volume_yes_taker":py,"pct_volume_no_taker":pn,"trade_date":trade_date,
                "winning_temp":str(wt) if wt else None,"winner":w})
    log.info("Collected %d finalized records.", len(rows)); return rows

def collect_fills(exchange_client, variable, central_time):
    if variable != 1: return []
    tickers = build_yesterday_tickers(central_time, variable)
    rows = []
    for ticker, _ in tickers:
        try: resp = exchange_client.get_event(event_ticker=ticker)
        except (HttpError,Exception) as e: log.warning("Fills error %s: %s",ticker,e); continue
        for mkt in resp.get("markets",[]):
            try: fr = exchange_client.get_fills(limit=1000,cursor=None,ticker=mkt["ticker"])
            except Exception as e: log.warning("Fills error %s: %s",mkt["ticker"],e); continue
            for f in fr.get("fills",[]):
                utc = pd.to_datetime(f.get("created_time"),utc=True)
                rows.append({"trade_id":f.get("trade_id",""),"ticker":f.get("ticker",""),
                    "event_ticker":ticker,"side":f.get("side",""),"action":f.get("action",""),
                    "count":f.get("count",0),"yes_price":f.get("yes_price",0),
                    "no_price":f.get("no_price",0),"taker_side":f.get("taker_side",""),
                    "created_time_utc":utc,"created_time_central":utc.tz_convert("US/Central"),
                    "order_id":f.get("order_id",""),
                    "concat_key":f.get("ticker","")+str(f.get("no_price",""))})
    log.info("Collected %d fills.", len(rows)); return rows

# ===========================================================================
# Main
# ===========================================================================
def main():
    log.info("="*60)
    log.info("Kalshi Weather Bot (%s)", "Colab" if IS_COLAB else "GitHub Actions / CLI")
    log.info("="*60)

    if not KALSHI_API_KEY_ID: log.error("KALSHI_API_KEY_ID not set."); sys.exit(1)
    if not KALSHI_PRIVATE_KEY_B64 and not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        log.error("No Kalshi private key found."); sys.exit(1)

    central_time = datetime.now(pytz.timezone("US/Central"))
    variable = get_session_variable()
    log.info("CT: %s | %s (var=%d)", central_time.strftime("%Y-%m-%d %H:%M:%S"),
             "evening" if variable else "morning", variable)

    pk = decode_private_key(b64_key=KALSHI_PRIVATE_KEY_B64, file_path=KALSHI_PRIVATE_KEY_PATH)
    xc = ExchangeClient(exchange_api_base=KALSHI_API_BASE, key_id=KALSHI_API_KEY_ID, private_key=pk)
    st = xc.get_exchange_status(); log.info("Exchange: %s", st)
    if not st.get("trading_active"): log.warning("Trading inactive."); sys.exit(0)

    log.info("BigQuery: project=%s dataset=%s", BQ_PROJECT, BQ_DATASET)
    resolve_gcp_credentials()
    bq, schemas = setup_bigquery()

    # Phase 1: Forecasts
    log.info("--- PHASE 1: Forecasts ---")
    ft = collect_forecasts(variable, central_time)
    ft = add_nws_conditions(ft, variable, central_time)
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
    ct = calculate_tail_bids(ct)

    # Phase 3: Positions
    log.info("--- PHASE 3: Positions ---")
    ct = cancel_orders_and_pull_positions(xc, ct)

    # Phase 4: Snapshot
    log.info("--- PHASE 4: Snapshot ---")
    snap = ct.rename(columns={"City":"city","Forecast Date":"forecast_date","Run Date":"run_date",
        "Weather Underground":"weather_underground","Accuweather":"accuweather","NWS":"nws",
        "Average":"forecast_avg","Standard Deviation":"forecast_std","Highest Minus Lowest":"forecast_range",
        "NWS Detailed Conditions":"nws_detailed_conditions","NWS Short Conditions":"nws_short_conditions",
        "Midnight Temperature":"midnight_temperature","var":"historical_var","var_sqrt":"historical_var_sqrt"}).copy()
    bq_cols = [f.name for f in schemas[f"{BQ_TABLE_PREFIX}market_snapshot"]]
    snap = snap[[c for c in bq_cols if c in snap.columns]]
    snap["forecast_date"]=pd.to_datetime(snap["forecast_date"]); snap["run_date"]=pd.to_datetime(snap["run_date"])
    for c in ["weather_underground","accuweather","nws","forecast_avg","forecast_std","forecast_range",
              "midnight_temperature","low_range","high_range","hi_no_price","historical_var",
              "historical_var_sqrt","yes_probability","fair_no_price","no_highest_bid","no_lowest_offer"]:
        if c in snap.columns: snap[c]=pd.to_numeric(snap[c],errors="coerce")
    if "position" in snap.columns: snap["position"]=pd.to_numeric(snap["position"],errors="coerce").fillna(0).astype("Int64")
    for c in ["no_orderbook","yes_orderbook","nws_detailed_conditions","nws_short_conditions"]:
        if c in snap.columns: snap[c]=snap[c].astype(str)
    write_to_bq(bq, schemas, snap, "market_snapshot")

    # Phase 5: Orders
    log.info("--- PHASE 5: Orders ---")
    orders = place_orders(xc, ct, variable, central_time)
    if orders:
        odf = pd.DataFrame(orders)
        for c in ["forecast_date","run_date","created_at"]: odf[c]=pd.to_datetime(odf[c])
        write_to_bq(bq, schemas, odf, "orders")

    # Phase 6: Evening reconciliation
    if variable == 1:
        log.info("--- PHASE 6: Reconciliation ---")
        s = collect_settlements(xc, variable, central_time)
        if s:
            sdf=pd.DataFrame(s); sdf["trade_date"]=pd.to_datetime(sdf["trade_date"])
            sdf["settled_time"]=pd.to_datetime(sdf["settled_time"]); write_to_bq(bq,schemas,sdf,"settlements")
        fm = collect_finalized_markets(xc, variable, central_time)
        if fm:
            fdf=pd.DataFrame(fm); fdf["trade_date"]=pd.to_datetime(fdf["trade_date"])
            fdf["volume_traded"]=pd.to_numeric(fdf["volume_traded"],errors="coerce").fillna(0).astype("Int64")
            for c in ["avg_trade_price_yes","avg_trade_price_no","pct_volume_yes_taker","pct_volume_no_taker"]:
                fdf[c]=pd.to_numeric(fdf[c],errors="coerce")
            write_to_bq(bq,schemas,fdf,"finalized_markets")
        fl = collect_fills(xc, variable, central_time)
        if fl: write_to_bq(bq, schemas, pd.DataFrame(fl), "fills")

    log.info("="*60); log.info("Done."); log.info("="*60)

if __name__ == "__main__":
    main()
