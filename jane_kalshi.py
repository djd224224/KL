# -*- coding: utf-8 -*-
# jane_kalshi.py — patched with orderbook_fp, floor std, None params, try/except, rate limiting
#
# 2026-07-10 repair pass — ported the fixes high_temp_trading.py (same strategy,
# same Kalshi V2 migration) accumulated since June:
#   1. `variable` off-by-one: the `< 23` upper bound made the forecast section
#      and the ticker section disagree at 23:xx CT (WU returns None for today's
#      retired slot). Bound dropped so both definitions read identically.
#   2. Tail bid calc crashed on int(NaN) when every forecast source failed for
#      a city (2026-05-02 incident in high_temp). NaN rows dropped instead.
#   3. Orderbook fetch and event fetch wrapped in try/except — one bad market
#      no longer kills the whole run.
#   4. Cancel sweep: V2 partially-filled orders (remaining_count > 0) are
#      cancelled too, and a cancel race (order fills between get_orders and
#      cancel_order → 4xx) no longer aborts the run.
#   5. Positions are fractional post-V2 ('196.74') — position column is float.
#   6. Buy loop skipped markets with a one-sided book instead of crashing on
#      int('') when the NO side was empty; cap check now uses the real
#      escalated contract size instead of the stale `i * 10` estimate.
#   7. get_unix_time_for_tomorrow rolls to the next day when the target is in
#      the past — a 9:05-10:59 CT run used to produce an already-expired
#      expiration_ts and Kalshi rejected every order of the run.
#   8. V2 read-field renames (the writes were already fixed in the shared
#      client): settlements yes/no_total_cost -> *_total_cost_dollars,
#      public trades count/yes_price/no_price -> count_fp / *_price_dollars,
#      fills count -> count_fp, prices -> *_price_dollars. The P&L, finalized-
#      market-data, and fills sheet sections read the new fields (legacy
#      fallbacks kept). Settlement P&L had been silently writing NOTHING since
#      the rename because `settlement.get('yes_total_cost', 0)` was always 0.
#      NOTE: 'profit' on the PnL tab is now in DOLLARS (was cents pre-V2).

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
import KalshiClientsBaseV2ApiKey_FIXED as _kalshi_client_module
import pytz
import time
import json
import uuid
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import re
import os
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

def load_private_key(b64_key="", file_path=""):
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f"No private key. Set KALSHI_PRIVATE_KEY or place at '{file_path}'.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

prod_key_id = os.environ.get("KALSHI_API_KEY_ID", "e55e8ada-c606-4cfe-b991-7d9a19b3332f")
prod_private_key = load_private_key(
    b64_key=os.environ.get("KALSHI_PRIVATE_KEY", ""),
    file_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Jane Kalshi.txt")
)
prod_api_base = "https://api.elections.kalshi.com/trade-api/v2"

# ====================================================================
# FIX 9: legacy-client shim. Python imports whichever
# KalshiClientsBaseV2ApiKey_FIXED.py sits next to this script. The KL-repo
# copy was migrated to the V2 order endpoints on 2026-06-29 (Kalshi retired
# POST/DELETE /portfolio/orders* -> HTTP 410 Gone). If an OLD copy is on the
# path (symptom: every order fails with 410 and the legacy params dict is
# printed instead of a "[create_order->V2]" line), translate orders here and
# post to /portfolio/events/orders ourselves. Mirrors the KL client's
# YES-book mapping: buy NO at p cents == ask YES at (100-p) cents.
# ====================================================================
print(f"Kalshi client module: {_kalshi_client_module.__file__}")

def _shim_cents_to_dollar_str(cents):
    c = int(round(float(cents)))
    sign = '-' if c < 0 else ''
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"

def _shim_v2_create_order(self, ticker, client_order_id=None, side=None,
                          action='buy', count=None, type='limit',
                          yes_price=None, no_price=None, post_only=None,
                          expiration_ts=None, **_ignored):
    s = (side or '').strip().lower()
    a = (action or 'buy').strip().lower()
    if s == 'yes':
        if yes_price is None:
            raise ValueError("create_order: yes_price required for side='yes'")
        cents = int(yes_price)
        book_side = 'bid' if a == 'buy' else 'ask'
    elif s == 'no':
        if no_price is None:
            raise ValueError("create_order: no_price required for side='no'")
        cents = 100 - int(no_price)
        book_side = 'ask' if a == 'buy' else 'bid'
    else:
        raise ValueError(f"create_order: unknown side {side!r}")
    if count is None:
        raise ValueError("create_order: count required")
    if (type or 'limit').strip().lower() != 'limit':
        raise NotImplementedError("V2 shim supports limit orders only")
    body = {
        'ticker': ticker,
        'side': book_side,
        'count': f"{float(count):.2f}",
        'self_trade_prevention_type': os.environ.get('KALSHI_STP_TYPE', 'taker_at_cross'),
        'price': _shim_cents_to_dollar_str(cents),
        'time_in_force': 'good_till_canceled',
    }
    if expiration_ts is not None:
        body['expiration_time'] = int(expiration_ts)
    if client_order_id is not None:
        body['client_order_id'] = client_order_id
    if post_only:
        body['post_only'] = True
    print(f"[create_order->V2-shim] {body}")
    result = self.post(path='/portfolio/events/orders', body=json.dumps(body))
    if isinstance(result, dict):
        return {'order': dict(result), **result}
    return {'order': {}, 'raw': result}

def _shim_v2_cancel_order(self, order_id):
    # legacy DELETE /portfolio/orders/{id} is also 410 Gone
    return self.delete(path='/portfolio/events/orders/' + order_id)

if not hasattr(ExchangeClient, '_build_v2_order_body'):
    print("WARNING: pre-V2 Kalshi client detected at the path above - patching "
          "order create/cancel to the V2 endpoints. Replace that file with the "
          "current KalshiClientsBaseV2ApiKey_FIXED.py from the KL repo.")
    ExchangeClient.create_order = _shim_v2_create_order
    ExchangeClient.cancel_order = _shim_v2_cancel_order

exchange_client = ExchangeClient(exchange_api_base=prod_api_base, key_id=prod_key_id, private_key=prod_private_key)
print(exchange_client.get_exchange_status())

###### GSHEET (non-fatal)

from google.oauth2 import service_account
from googleapiclient.discovery import build

def append_to_gsheet(df, spreadsheet_id, range_name):
    try:
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
        SERVICE_ACCOUNT_FILE = 'google_credentials.json'
        if not os.path.exists(SERVICE_ACCOUNT_FILE) or os.path.getsize(SERVICE_ACCOUNT_FILE) < 10:
            print(f"GSheet skipped: {SERVICE_ACCOUNT_FILE} missing or empty")
            return False
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('sheets', 'v4', credentials=creds)
        body = {'values': df.values.tolist()}
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range_name,
            valueInputOption='RAW', insertDataOption='INSERT_ROWS', body=body).execute()
        print(f"Append successful: {result.get('updates').get('updatedRows')} rows added")
        return True
    except Exception as e:
        print(f"GSheet error (non-fatal): {e}")
        return False

# Determine if current time in Central Time is after 2 PM. variable=1 means
# "trade tomorrow's market" (index 1 in forecast arrays); variable=0 = today.
# FIX 1: no `< 23` upper bound — must match the second definition below, or a
# 23:xx CT run pulls today's (retired) forecast slot for tomorrow's markets.
central_time = datetime.now(pytz.timezone('US/Central'))
variable = 1 if central_time.hour >= 14 else 0

cities = {
    "Austin": (30.18304, -97.67987),
    "Miami": (25.79056, -80.31639),
    "Houston": (29.63750, -95.28250),
    "Denver": (39.84658, -104.65622),
    "New York City": (40.78333, -73.96667),
    "Philadelphia": (39.87327, -75.22678),
    "Chicago": (41.78417, -87.75528),
    "Los Angeles": (33.942791, -118.410042)
}

def get_accuweather_forecast(coords):
    try:
        api_key = "lEl0lfAft6PncVXwatr92Y2YjGJL5YKs"
        lat, lon = coords
        location_url = f"http://dataservice.accuweather.com/locations/v1/cities/geoposition/search"
        location_params = {"apikey": api_key, "q": f"{lat},{lon}"}
        location_response = requests.get(location_url, params=location_params)
        location_response.raise_for_status()
        location_key = location_response.json()["Key"]
        forecast_url = f"http://dataservice.accuweather.com/forecasts/v1/daily/5day/{location_key}"
        forecast_params = {"apikey": api_key, "metric": "false"}
        forecast_response = requests.get(forecast_url, params=forecast_params)
        forecast_response.raise_for_status()
        forecast = forecast_response.json()
        return int(forecast["DailyForecasts"][variable]["Temperature"]["Maximum"]["Value"])
    except Exception as e:
        print(f"Error fetching AccuWeather forecast for {coords}: {e}")
        return "N/A"

def get_nws_forecast(lat, lon):
    try:
        base_url = f"https://api.weather.gov/points/{lat},{lon}"
        grid_response = requests.get(base_url)
        grid_response.raise_for_status()
        forecast_url = grid_response.json()['properties']['forecast']
        forecast_response = requests.get(forecast_url)
        forecast_response.raise_for_status()
        periods = forecast_response.json()['properties']['periods']
        dayswitch = (central_time + timedelta(days=variable)).strftime('%Y-%m-%d')
        for period in periods:
            if dayswitch in period['startTime'] and "day" in period['name'].lower():
                return period['temperature']
        return "N/A"
    except Exception as e:
        print(f"Error fetching NWS forecast for {lat}, {lon}: {e}")
        return "N/A"

def get_weatherunderground_forecast(city, coords):
    API_KEY = "a828c2a178844147a8c2a17884a147a5"
    lat, lon = coords
    URL = f"https://api.weather.com/v3/wx/forecast/daily/5day?apiKey={API_KEY}&geocode={lat},{lon}&format=json&units=e&language=en-US"
    response = requests.get(URL)
    if response.status_code == 200:
        data = response.json()
        if "temperatureMax" in data:
            return data["temperatureMax"][variable]
        else:
            print(f"Error: 'temperatureMax' not found for {city}.")
            return "N/A"
    else:
        print(f"Failed to fetch data for {city}. Status: {response.status_code}")
        return "N/A"

central_tz = pytz.timezone("US/Central")
run_date = datetime.now(central_tz).strftime('%Y-%m-%d %H:%M:%S')

forecast_data = []
for city, coords in cities.items():
    nws_temp = get_nws_forecast(*coords)
    accuweather_temp = get_accuweather_forecast(coords)
    weatherundergroud_temp = get_weatherunderground_forecast(city, coords)
    forecast_data.append({
        "City": city,
        "Forecast Date": (datetime.now(pytz.timezone('US/Central')) + timedelta(days=variable)).strftime('%Y-%m-%d'),
        "Run Date": run_date,
        "Weather Underground": weatherundergroud_temp,
        "Accuweather": accuweather_temp,
        "NWS": nws_temp
    })

forecast_table = pd.DataFrame(forecast_data)
forecast_table[["Weather Underground","Accuweather","NWS"]] = forecast_table[["Weather Underground","Accuweather","NWS"]].apply(pd.to_numeric, errors='coerce')
forecast_table["Average"] = forecast_table[["Weather Underground","Accuweather","NWS"]].mean(axis=1)
forecast_table["Standard Deviation"] = forecast_table[["Weather Underground","Accuweather","NWS"]].std(axis=1)
forecast_table["Highest Minus Lowest"] = forecast_table[["Weather Underground","Accuweather","NWS"]].max(axis=1) - forecast_table[["Weather Underground","Accuweather","NWS"]].min(axis=1)

##### GET CONDITIONS FROM NWS

cities = {
    "Austin": {"lat": 30.2672, "lon": -97.7431},
    "Miami": {"lat": 25.7617, "lon": -80.1918},
    "Houston": {"lat": 29.7604, "lon": -95.3698},
    "Denver": {"lat": 39.7392, "lon": -104.9903},
    "New York City": {"lat": 40.7128, "lon": -74.0060},
    "Philadelphia": {"lat": 39.9526, "lon": -75.1652},
    "Chicago": {"lat": 41.8781, "lon": -87.6298},
    "Los Angeles": {"lat": 33.942791, "lon": -118.410042},
}

def fetch_nws_conditions(city):
    url = f"https://api.weather.gov/points/{city['lat']},{city['lon']}"
    response = requests.get(url)
    if response.status_code == 200:
        grid_data = response.json()
        forecast_url = grid_data['properties']['forecast']
        forecast_response = requests.get(forecast_url)
        if forecast_response.status_code == 200:
            forecast_data = forecast_response.json()
            target_date = (datetime.now(pytz.timezone('US/Central')) + timedelta(days=variable)).strftime("%Y-%m-%d")
            for period in forecast_data['properties']['periods']:
                if target_date in period['startTime']:
                    return period['detailedForecast'], period['shortForecast']
    return None, None

forecast_conditions = []
for city_name, city_coords in cities.items():
    nws_detailed, nws_short = fetch_nws_conditions(city_coords)
    forecast_conditions.append({"City": city_name, "NWS Detailed Conditions": nws_detailed, "NWS Short Conditions": nws_short})

forecast_table = pd.merge(forecast_table, pd.DataFrame(forecast_conditions), on='City', how='inner')

############# GET MIDNIGHT TEMPS FROM NWS

def fetch_midnight_forecast(coords):
    url = f"https://api.weather.gov/points/{coords['lat']},{coords['lon']}"
    response = requests.get(url)
    if response.status_code == 200:
        grid_data = response.json()
        forecast_url = grid_data['properties']['forecastHourly']
        hourly_response = requests.get(forecast_url)
        if hourly_response.status_code == 200:
            hourly_data = hourly_response.json()
            for period in hourly_data['properties']['periods']:
                start_time = re.sub(r"([-+]\d{2}):(\d{1})$", r"\1:0\2", period['startTime'])
                forecast_time = datetime.fromisoformat(start_time).astimezone(pytz.timezone('US/Central'))
                if forecast_time.hour == 0 and forecast_time.minute == 0 and forecast_time.date() == datetime.now(pytz.timezone('US/Central')).date() + timedelta(days=variable):
                    return period['temperature']
    return None

forecasts = []
for city, coords in cities.items():
    forecasts.append({"City": city, "Midnight Temperature": fetch_midnight_forecast(coords)})

forecast_table = pd.merge(forecast_table, pd.DataFrame(forecasts), on='City', how='inner')
if variable == 0:
    forecast_table['concat'] = forecast_table['City'] + forecast_table['Forecast Date'] + 'Day'
if variable == 1:
    forecast_table['concat'] = forecast_table['City'] + forecast_table['Forecast Date'] + 'Night'

spreadsheet_id = '1SDwPpdK6H-78sWY5YYlToi043fYjlc2w2TuBvxUKs3Y'
forecast_table = forecast_table.fillna("")
success = append_to_gsheet(forecast_table, spreadsheet_id, 'City Summary!A2')

########## DROP ANY WHERE MIDNIGHT TEMP IS WITHIN 5 DEGREES OF TOMORROW'S HIGH

if variable == 1:
    forecast_table['Average'] = pd.to_numeric(forecast_table['Average'], errors='coerce')
    forecast_table['Midnight Temperature'] = pd.to_numeric(forecast_table['Midnight Temperature'], errors='coerce')
    forecast_table = forecast_table[abs(forecast_table['Average'] - forecast_table['Midnight Temperature']) >= 4.5]

########### INPUT EVENT TICKERS

central_time = datetime.now(pytz.timezone('US/Central'))
variable = 1 if central_time.hour >= 14 else 0
day = central_time + timedelta(days=variable)
month = day.strftime("%b").upper()
day = day.strftime("%d")

event_ticker1 = ['KXHIGHCHI-26' + month + day, 'Chicago', 56, 1.7]
event_ticker2 = ['KXHIGHNY-26' + month + day, 'New York City', 41, 1.5]
event_ticker3 = ['KXHIGHDEN-26' + month + day, 'Denver', 60, 2.4]
event_ticker4 = ['KXHIGHPHIL-26' + month + day, 'Philadelphia', 47, 1.6]
event_ticker5 = ['KXHIGHAUS-26' + month + day, 'Austin', 60, 1.7]
event_ticker6 = ['KXHIGHMIA-26' + month + day, 'Miami', 46, 1.1]
event_ticker8 = ['KXHIGHLAX-26' + month + day, 'Los Angeles', 55, 2.0]

############ PULL MARKETS
all_event_tickers = [event_ticker1, event_ticker2, event_ticker3, event_ticker4, event_ticker5, event_ticker6, event_ticker8]
event_tickers = pd.DataFrame(all_event_tickers, columns=['Ticker', 'City', 'hi_no_price', 'var'])
event_tickers['var_sqrt'] = np.sqrt(event_tickers['var'])
markets_table = pd.DataFrame(columns=['event_ticker', 'market_ticker', 'City', 'low_range', 'high_range', 'hi_no_price', 'var', 'var_sqrt'])

for event_ticker in event_tickers['Ticker']:
    event_params = {'event_ticker': event_ticker}
    print(event_ticker)
    # FIX 3: one 404/500 on a single event no longer kills the whole run
    try:
        event_response = exchange_client.get_event(**event_params)
    except Exception as e:
        print(f"  event fetch failed for {event_ticker} (skipped): {e}")
        continue
    for market in event_response['markets']:
        city = event_tickers.loc[event_tickers['Ticker'] == event_ticker, 'City'].iloc[0]
        hi_no_price = event_tickers.loc[event_tickers['Ticker'] == event_ticker, 'hi_no_price'].iloc[0]
        historical_variance = event_tickers.loc[event_tickers['Ticker'] == event_ticker, 'var'].iloc[0]
        historical_variance_sqrt = event_tickers.loc[event_tickers['Ticker'] == event_ticker, 'var_sqrt'].iloc[0]
        if "-B" in market['ticker']:
            match = re.search(r"-B(\d+(\.\d+)?)", market['ticker'])
            if match:
                n = float(match.group(1))
                markets_table = pd.concat([markets_table, pd.DataFrame([[event_ticker, market['ticker'], city, n-1, n+1, hi_no_price, historical_variance, historical_variance_sqrt]], columns=markets_table.columns)], ignore_index=True)
        if "-T" in market['ticker']:
            match = re.search(r"-T(\d+(\.\d+)?)", market['ticker'])
            if match:
                n = float(match.group(1))
                markets_table = pd.concat([markets_table, pd.DataFrame([[event_ticker, market['ticker'], city, 0, n-0.5, hi_no_price, historical_variance, historical_variance_sqrt]], columns=markets_table.columns)], ignore_index=True)

markets_table['market_ticker_prev'] = markets_table['market_ticker'].shift(1)
for index, row in markets_table.iterrows():
    if index == 0: continue
    if "-B" in str(row['market_ticker_prev']) and "-T" in row['market_ticker']:
        markets_table.loc[index, 'low_range'] = row['high_range'] + 1
        markets_table.loc[index, 'high_range'] = 150
markets_table.drop(columns=['market_ticker_prev'], inplace=True)

##### COMBINE FORECAST AND MARKET TABLES, CALCULATE PROBABILITIES
from scipy.stats import norm

combined_table = pd.merge(forecast_table, markets_table, on='City', how='inner')
combined_table['Average'] = pd.to_numeric(combined_table['Average'], errors='coerce')
combined_table['Standard Deviation'] = pd.to_numeric(combined_table['Standard Deviation'], errors='coerce')

# FIX: City floor std dev (prevents 0 std when sources agree)
CITY_FLOOR_STD = {
    "Austin": 1.2, "Miami": 1.0, "Houston": 1.9, "Denver": 1.4,
    "New York City": 1.2, "Philadelphia": 1.3, "Chicago": 1.3, "Los Angeles": 1.2,
}
combined_table['City Floor Std'] = combined_table['City'].map(CITY_FLOOR_STD).fillna(1.2)
combined_table['Standard Deviation'] = combined_table[['Standard Deviation', 'City Floor Std']].max(axis=1)
combined_table['Standard Deviation'] = combined_table['Standard Deviation'].replace({0: 1.2, np.nan: 1.2})

combined_table['high_range'] = pd.to_numeric(combined_table['high_range'], errors='coerce')
combined_table['low_range'] = pd.to_numeric(combined_table['low_range'], errors='coerce')
combined_table['yes_probability'] = (norm.cdf(combined_table['high_range'], loc=combined_table['Average'], scale=combined_table['Standard Deviation'])
                                     - norm.cdf(combined_table['low_range'], loc=combined_table['Average'], scale=combined_table['Standard Deviation'])).round(2)
combined_table['fair_no_price'] = 1 - combined_table['yes_probability']

####PULL ORDER BOOK OF MARKETS — FIX: handles orderbook_fp format

combined_table['no_highest_bid'] = ''
combined_table['no_lowest_offer'] = ''
combined_table['no_orderbook'] = ''
combined_table['yes_orderbook'] = ''

for index, row in combined_table.iterrows():
    market_ticker = row['market_ticker']
    # FIX 3: skip this market on orderbook fetch failure instead of dying
    try:
        orderbook_response = exchange_client.get_orderbook(ticker=market_ticker, depth=3)
    except Exception as e:
        print(f"  orderbook fetch failed for {market_ticker} (skipped): {e}")
        continue

    no_levels = []
    yes_levels = []
    if 'orderbook_fp' in orderbook_response:
        ob_fp = orderbook_response['orderbook_fp']
        for level in ob_fp.get('no_dollars', []):
            no_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
        for level in ob_fp.get('yes_dollars', []):
            yes_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
    elif 'orderbook' in orderbook_response:
        ob = orderbook_response['orderbook']
        no_levels = ob.get('no', [])
        yes_levels = ob.get('yes', [])
    else:
        no_levels = orderbook_response.get('no', [])
        yes_levels = orderbook_response.get('yes', [])

    if no_levels:
        combined_table.loc[index, 'no_highest_bid'] = str(no_levels[-1][0])
    if yes_levels:
        combined_table.loc[index, 'no_lowest_offer'] = str(100 - yes_levels[-1][0])
    combined_table.loc[index, 'no_orderbook'] = str(no_levels)
    combined_table.loc[index, 'yes_orderbook'] = str(yes_levels)

######### ACTUALS VS FORECAST, FOR BETTING THE HIGH END

columns = ["Austin", "Miami", "Denver", "Houston", "Philadelphia", "New York City", "Chicago", "Los Angeles"]
rows = ["5", "4", "3", "2", "1", "0", "-1", "-2", "-3", "-4", "-5"]
data = [
    [0.0270, 0.0256, 0.1290, 0.0000, 0.0000, 0.0270, 0.0333, 0.0667],
    [0.0270, 0.0000, 0.0000, 0.0000, 0.0263, 0.0000, 0.0333, 0.0000],
    [0.0811, 0.0000, 0.0645, 0.1613, 0.1579, 0.1081, 0.1333, 0.0000],
    [0.2432, 0.2308, 0.1290, 0.1935, 0.1053, 0.1622, 0.2333, 0.0000],
    [0.1892, 0.2051, 0.1935, 0.1935, 0.2895, 0.2162, 0.3333, 0.2667],
    [0.1351, 0.2821, 0.1935, 0.0645, 0.1053, 0.2162, 0.0667, 0.2667],
    [0.1892, 0.1282, 0.0645, 0.2258, 0.1053, 0.0811, 0.1000, 0.1333],
    [0.0270, 0.0769, 0.0645, 0.0968, 0.1053, 0.1622, 0.0000, 0.1333],
    [0.0541, 0.0256, 0.0968, 0.0323, 0.0526, 0.0000, 0.0000, 0.0667],
    [0.0000, 0.0000, 0.0323, 0.0000, 0.0263, 0.0000, 0.0333, 0.0000],
    [0.0270, 0.0256, 0.0323, 0.0323, 0.0263, 0.0270, 0.0333, 0.0667]
]

actuals_vs_forecast_variation = pd.DataFrame(data, columns=columns, index=rows)
actuals_vs_forecast_variation = actuals_vs_forecast_variation.transpose()
actuals_vs_forecast_variation['City'] = actuals_vs_forecast_variation.index

##### CALCULATE HIGH RANGE BIDS

x = combined_table[combined_table['high_range'] == 150]
y = pd.DataFrame(columns=['City', 'low_range', 'Average', 'forecast_variation_vs_high_end', 'bid_price'])
forcast_data = round(x['low_range'] - x['Average'])
y['forecast_variation_vs_high_end'] = forcast_data
y['City'] = x['City']
y['low_range'] = x['low_range']
y['Average'] = x['Average']
y = pd.merge(y, actuals_vs_forecast_variation, on='City', how='inner')
# FIX 2: drop NaN rows before int() — a city whose every forecast source
# failed used to crash the entire run here (int(NaN) -> ValueError). Those
# cities just don't get tail orders this run.
_pre_drop_n = len(y)
y = y[y['forecast_variation_vs_high_end'].notna()].reset_index(drop=True)
if len(y) < _pre_drop_n:
    print(f"  ! Tail bid calc: dropped {_pre_drop_n - len(y)}/{_pre_drop_n} cities with NaN forecast variation")
chart_end_index = [int(v) for v in y['forecast_variation_vs_high_end'].tolist()]
filtered_data = [y[str(col)].tolist() for col in range(5, -6, -1)]

percentage_data = list()
for index, value in enumerate(chart_end_index):
    if value > 5: value = 5
    if value < -5: value = -5
    city_data = [x[index] for x in filtered_data]
    end_index = 5 - value + 1
    values_to_sum = city_data[0:end_index]
    percentage_data.append(sum(values_to_sum))

y['bid_price'] = [1 - x for x in percentage_data]

for index, row in combined_table.iterrows():
    if row['high_range'] == 150:
        # FIX 2 (cont.): city may have been dropped from y above — keep the
        # static hi_no_price instead of crashing on .iloc[0] of an empty slice
        try:
            bid_price = y.loc[y['City'] == row['City'], 'bid_price'].iloc[0]
            combined_table.loc[index, 'hi_no_price'] = bid_price * 100 - 15
        except (IndexError, KeyError):
            pass

####### PASTE COMBINED_TABLE INTO GSHEET

combined_table = combined_table.fillna("")
success = append_to_gsheet(combined_table, spreadsheet_id, 'Orderbook')

####### DELETE EXISTING ORDERS

for market_ticker in combined_table['market_ticker']:
    try:
        order_response = exchange_client.get_orders(ticker=market_ticker, limit=1000)
    except Exception as e:
        print(f"  get_orders failed for {market_ticker} (skipped): {e}")
        continue
    for order in order_response.get('orders') or []:
        # FIX 4: cancel anything still live. Primary signal is
        # remaining_count > 0 (normalized from V2 remaining_count_fp — Kalshi
        # zeroes it on cancel/execution/expiry). Status whitelist kept as a
        # fallback so partially-filled orders don't keep accruing fills.
        remaining = order.get('remaining_count')
        if remaining is None:
            # raw V2 field, present when a legacy client copy didn't normalize
            try: remaining = float(order.get('remaining_count_fp') or 0)
            except (TypeError, ValueError): remaining = 0
        status = order.get('status')
        if remaining > 0 or status in ('resting', 'partial_filled', 'partially_filled', 'pending'):
            try:
                exchange_client.cancel_order(order_id=order['order_id'])
            except Exception as ce:
                # A partial that completes between the get_orders snapshot and
                # the cancel call returns a 4xx — log and continue.
                print(f"  cancel failed for {order['order_id']}: {ce}")

############## PULL CURRENT POSITIONS
for market_ticker in combined_table['market_ticker']:
    positions_params = {'ticker': market_ticker}
    current_position = exchange_client.get_positions(**positions_params)
    if 'market_positions' in current_position and current_position['market_positions']:
        positions = pd.DataFrame(current_position['market_positions'])
        # raw V2 field, present when a legacy client copy didn't normalize
        if 'position' not in positions.columns and 'position_fp' in positions.columns:
            positions['position'] = pd.to_numeric(positions['position_fp'], errors='coerce').fillna(0)
        if 'position' in positions.columns:
            live_positions = positions[positions['position'] != 0]
            if not live_positions.empty:
                row_index = combined_table.index[combined_table['market_ticker'] == market_ticker].tolist()[0]
                combined_table.loc[row_index, 'position'] = abs(live_positions['position'].iloc[0])
    if 'position' not in combined_table.columns:
        combined_table['position'] = 0
    # FIX 5: float, not int — V2 contract quantities are fractional (e.g. 196.74)
    combined_table['position'] = combined_table['position'].fillna(0).astype(float)
    combined_table['resting_order_count'] = 0

##### PLACE ORDERS

increment = 3
increment1 = 6
price_count = list(range(0, 25))
if central_time.hour > 10 or central_time.hour < 6:
    starting_contracts = 225
else:
    starting_contracts = 150

max_contracts = 1500
market_cutoff_probability = .2

def get_unix_time_for_tomorrow(hour, minute, timezone='US/Central'):
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    target_time = tz.localize(datetime(year=now.year, month=now.month, day=now.day, hour=hour, minute=minute) + timedelta(days=variable))
    # FIX 7: if the target is already in the past (or within 5 minutes), roll
    # forward a day. A 9:05-10:59 CT run used to compute an already-expired
    # expiration_ts and Kalshi rejected every order of the run.
    if (target_time - now).total_seconds() < 300:
        target_time += timedelta(days=1)
        # ASCII only: non-ASCII glyphs raise UnicodeEncodeError when stdout is
        # piped to a cp1252 log file on Windows, killing the run mid-order
        print(f"  exp_ts target ({hour:02d}:{minute:02d} CT) was <=5min away; rolled to next day")
    return int(target_time.timestamp())

########## BUY CALCULATIONS
orders_placed = 0
for index, row in combined_table.iterrows():
    if row['yes_probability'] > market_cutoff_probability or ("-T" in row['market_ticker'] and row['high_range'] == 150):
        if "-T" not in row['market_ticker'] or ("-T" in row['market_ticker'] and row['high_range'] == 150):
            # FIX 6: need BOTH sides of the book. int('') on an empty NO side
            # used to crash the run mid-loop (one-sided books are common on
            # fresh tail markets).
            if row['no_lowest_offer'] == '' or row['no_highest_bid'] == '':
                print(f"  SKIP {row['market_ticker']}: one-sided/empty orderbook")
                continue
            no_offer = int(row['no_lowest_offer'])
            no_bid = int(row['no_highest_bid'])
            i1 = 0
            for i in price_count:
                if "-T" not in row['market_ticker']:
                    bid_price = max(row['hi_no_price'] - i * increment, 1)
                if "-T" in row['market_ticker'] and row['high_range'] == 150:
                    bid_price = max(row['hi_no_price'] - i * increment1, 1)

                # FIX 6 (cont.): cap check uses the real escalated size for
                # this rung, not the old `starting_contracts + i * 10` estimate
                contracts = starting_contracts + i1 * 15
                if bid_price < no_offer and bid_price < no_bid - 3 and (max_contracts >= row['position'] + row['resting_order_count'] + contracts):
                    i1 = i1 + 1

                    # City abbreviation + cancel time
                    abv = ''; cancel_hour = 10; cancel_minute = 5
                    ticker_str = row['market_ticker']
                    if 'CHI' in ticker_str: abv = 'CHI'; cancel_hour = 10
                    elif 'AUS' in ticker_str: abv = 'AUS'; cancel_hour = 10
                    elif 'HOU' in ticker_str: abv = 'HOU'; cancel_hour = 10
                    elif 'DEN' in ticker_str: abv = 'DEN'; cancel_hour = 10
                    elif 'NY-' in ticker_str: abv = 'NY-'; cancel_hour = 9
                    elif 'PHI' in ticker_str: abv = 'PHI'; cancel_hour = 9
                    elif 'MIA' in ticker_str: abv = 'MIA'; cancel_hour = 9
                    elif 'LAX' in ticker_str: abv = 'LAX'; cancel_hour = 10

                    central_time = datetime.now(pytz.timezone('US/Central'))
                    if central_time.hour > 10 or central_time.hour < 6:
                        cancel_hour = 1; cancel_minute = 59

                    trades = pd.DataFrame([[row['City'], row['Forecast Date'], row['Run Date'], row['market_ticker'], contracts, bid_price, abv]],
                                          columns=['City', 'Forecast Date', 'Run Date', 'Market', 'Contracts', 'Price', 'abv'])
                    order_params = {'ticker': row['market_ticker'],
                                    'client_order_id': str(uuid.uuid4()),
                                    'type': 'limit',
                                    'action': 'buy',
                                    'side': 'no',
                                    'count': contracts,
                                    'no_price': int(bid_price),
                                    'expiration_ts': get_unix_time_for_tomorrow(cancel_hour, cancel_minute)}
                    try:
                        exchange_client.create_order(**order_params)
                        time.sleep(0.1)
                        success = append_to_gsheet(trades, spreadsheet_id, 'Trades Placed')
                        row['resting_order_count'] = row['resting_order_count'] + contracts
                        orders_placed += 1
                    except Exception as e:
                        print(f"    x Order failed {row['market_ticker']} @{int(bid_price)}c: {e}")
                        time.sleep(0.2)

                    if bid_price <= 1:
                        break

print(f"\nTOTAL ORDERS PLACED: {orders_placed}")

# PASTE YESTERDAY'S P&L INTO GSHEET
# FIX 8: V2 renamed the settlement cost fields; the old
# settlement.get('yes_total_cost', 0) was always 0 post-migration, so the
# gate below never passed and this section silently wrote nothing.

def _cost_dollars(s, key):
    """Cost in DOLLARS from a V2 settlement ('yes_total_cost_dollars' string);
    falls back to the legacy cents field ('yes_total_cost') / 100."""
    v = s.get(key + '_dollars')
    if v is not None:
        try: return float(v or 0)
        except (TypeError, ValueError): return 0.0
    try: return float(s.get(key, 0) or 0) / 100.0
    except (TypeError, ValueError): return 0.0

central_tz = pytz.timezone("America/Chicago")
central_now = datetime.now(central_tz)
central_date = central_now.date()
date = central_date - timedelta(days=variable)
month = date.strftime("%b").upper()
day = date.strftime("%d")
settlement_params = {'limit': 1000}
settlements = exchange_client.get_portfolio_settlements(**settlement_params)

for settlement in settlements['settlements']:
    if variable == 1:
        yes_cost = _cost_dollars(settlement, 'yes_total_cost')
        no_cost = _cost_dollars(settlement, 'no_total_cost')
        if yes_cost > 0 or no_cost > 0:
            # FIX 8 (cont.): settled_time has variable sub-second precision
            # (sometimes none at all) — strict strptime('%f') silently dropped
            # those settlements
            try:
                settled_time_dt = date_parser.isoparse(settlement['settled_time'])
            except (KeyError, ValueError, TypeError):
                continue
            trade_date_dt = settled_time_dt - timedelta(days=variable)
            ticker = settlement.get('ticker', '')
            if 'HIGH' in ticker and day in ticker and month in ticker:
                stk = ticker
                if 'CHI' in stk: settlement['abv'] = 'CHI'
                elif 'AUS' in stk: settlement['abv'] = 'AUS'
                elif 'HOU' in stk: settlement['abv'] = 'HOU'
                elif 'DEN' in stk: settlement['abv'] = 'DEN'
                elif 'NY-' in stk: settlement['abv'] = 'NY-'
                elif 'PHI' in stk: settlement['abv'] = 'PHI'
                elif 'MIA' in stk: settlement['abv'] = 'MIA'
                elif 'LAX' in stk: settlement['abv'] = 'LAX'
                settlement['trade_date'] = trade_date_dt.strftime('%Y-%m-%d')
                # revenue is still integer CENTS on V2 — convert so profit is
                # consistently in DOLLARS (was cents pre-migration)
                revenue_dollars = float(settlement.get('revenue', 0) or 0) / 100.0
                settlement['profit'] = round(revenue_dollars - yes_cost - no_cost, 2)
                settlement_table = pd.DataFrame(settlement, index=[0]).fillna("")
                print(settlement_table)
                success = append_to_gsheet(settlement_table, spreadsheet_id, 'PnL')

# GET FINALIZED MARKET DATA
# FIX 8 (cont.): public trades renamed count -> count_fp (fractional string)
# and yes_price/no_price -> yes_price_dollars/no_price_dollars (dollar
# strings). The old keys raised KeyError, the try/except ate it, and every
# volume/price stat came out 0.

def _trade_count(t):
    v = t.get('count_fp')
    if v is not None:
        try: return float(v)
        except (TypeError, ValueError): return 0.0
    try: return float(t.get('count', 0) or 0)
    except (TypeError, ValueError): return 0.0

def _trade_price_cents(t, side):
    v = t.get(side + '_price_dollars')
    if v is not None:
        try: return float(v) * 100.0
        except (TypeError, ValueError): return 0.0
    try: return float(t.get(side + '_price', 0) or 0)
    except (TypeError, ValueError): return 0.0

central_tz = pytz.timezone("America/Chicago")
central_now = datetime.now(central_tz)
date = central_now.date() - timedelta(days=variable)
month = date.strftime("%b").upper()
day = date.strftime("%d")

event_ticker1 = ['KXHIGHCHI-26' + month + day, 'CHI']
event_ticker2 = ['KXHIGHNY-26' + month + day, 'NY-']
event_ticker3 = ['KXHIGHDEN-26' + month + day, 'DEN']
event_ticker4 = ['KXHIGHPHIL-26' + month + day, 'PHI']
event_ticker5 = ['KXHIGHAUS-26' + month + day, 'AUS']
event_ticker6 = ['KXHIGHMIA-26' + month + day, 'MIA']
event_ticker8 = ['KXHIGHLAX-26' + month + day, 'LAX']
all_event_tickers = [event_ticker1, event_ticker2, event_ticker3, event_ticker4, event_ticker5, event_ticker6, event_ticker8]
event_tickers = pd.DataFrame(all_event_tickers, columns=['Ticker', 'ABV'])
markets_table = pd.DataFrame(columns=['event_ticker', 'market_ticker', 'ABV', 'volume_traded', 'avg_trade_price_yes', 'avg_trade_price_no', 'pct_volume_traded_yes_taker', 'pct_volume_traded_no_taker', 'trade_date', 'winning_temp', 'winner', 'concat'])

if variable == 1:
    trade_date = (datetime.now(pytz.timezone('US/Central')) - timedelta(days=variable)).strftime('%Y-%m-%d')
    for event_ticker in event_tickers['Ticker']:
        event_params = {'event_ticker': event_ticker}
        try:
            event_response = exchange_client.get_event(**event_params)
        except Exception as e:
            print(f"  event fetch failed for {event_ticker} (skipped): {e}")
            continue
        abv = event_tickers.loc[event_tickers['Ticker'] == event_ticker, 'ABV'].iloc[0]
        for market in event_response['markets']:
            winning_temp = None; winner = None; concat = abv + trade_date
            if market['result'] == 'yes':
                winning_temp = market['expiration_value']; winner = 'yes'; concat = abv + trade_date + winner
            volume_traded = 0; pct_volume_traded_yes_taker = 0; pct_volume_traded_no_taker = 0
            avg_trade_price_yes = 0; avg_trade_price_no = 0

            try:
                trade_response = exchange_client.get_trades(limit=1000, ticker=market['ticker'])
                for trade in trade_response['trades']:
                    t_count = _trade_count(trade)
                    volume_traded += t_count
                    avg_trade_price_yes += _trade_price_cents(trade, 'yes') * t_count
                    avg_trade_price_no += _trade_price_cents(trade, 'no') * t_count
                    if trade.get('taker_side') == 'yes': pct_volume_traded_yes_taker += t_count
                    if trade.get('taker_side') == 'no': pct_volume_traded_no_taker += t_count
                if volume_traded != 0:
                    avg_trade_price_yes /= volume_traded; avg_trade_price_no /= volume_traded
                    pct_volume_traded_yes_taker /= volume_traded; pct_volume_traded_no_taker /= volume_traded
            except Exception as e:
                print(f"Error fetching trades for {market['ticker']}: {e}")

            markets_table = pd.concat([markets_table, pd.DataFrame([[event_ticker, market['ticker'], abv, volume_traded, avg_trade_price_yes, avg_trade_price_no, pct_volume_traded_yes_taker, pct_volume_traded_no_taker, trade_date, winning_temp, winner, concat]], columns=markets_table.columns)], ignore_index=True)

    append_to_gsheet(markets_table, spreadsheet_id, 'Finalized Market Data')

### GET FILLS
if variable == 1:
    for event_ticker in event_tickers['Ticker']:
        try:
            event_response = exchange_client.get_event(event_ticker=event_ticker)
        except Exception as e:
            print(f"  event fetch failed for {event_ticker} (skipped): {e}")
            continue
        for market in event_response['markets']:
            fill_response = exchange_client.get_fills(limit=1000, ticker=market['ticker'])
            if fill_response and fill_response.get('fills'):
                fill_params_df = pd.DataFrame(fill_response['fills'])
                # FIX 8 (cont.): V2 fills — count_fp is a fractional string,
                # prices are *_price_dollars strings. Rebuild the legacy cents
                # columns the sheet expects.
                if 'count_fp' in fill_params_df.columns:
                    fill_params_df['count'] = pd.to_numeric(fill_params_df['count_fp'], errors='coerce').fillna(0)
                # Int64 (not float) so the concat key stays '...99' like the
                # sheet's legacy rows, not '...99.0'
                if 'yes_price_dollars' in fill_params_df.columns:
                    fill_params_df['yes_price'] = (pd.to_numeric(fill_params_df['yes_price_dollars'], errors='coerce') * 100).round().astype('Int64')
                if 'no_price_dollars' in fill_params_df.columns:
                    fill_params_df['no_price'] = (pd.to_numeric(fill_params_df['no_price_dollars'], errors='coerce') * 100).round().astype('Int64')
                if 'ticker' in fill_params_df.columns and 'no_price' in fill_params_df.columns:
                    fill_params_df['concat'] = fill_params_df['ticker'] + fill_params_df['no_price'].astype(str)
                fill_params_df['created_time_central'] = pd.to_datetime(fill_params_df['created_time'], utc=True).dt.tz_convert('US/Central').dt.strftime('%Y-%m-%d %H:%M:%S')
                fill_params_df = fill_params_df.fillna("")
                append_to_gsheet(fill_params_df, spreadsheet_id, 'Fills')

print("\nTrading run complete.")
