"""Backfill historical weather forecasts from Open-Meteo.

Open-Meteo's historical-forecast-api archives past forecasts from multiple
NWP models. We use two models — GFS (NOAA, the basis of NWS public forecasts)
and ECMWF IFS (European, a standard "second opinion") — and compute the mean
and std across them as a proxy for what the bot's ensemble forecast would
have produced on any given day.

Why this exists:
  KXHIGH_market_snapshot only covers Mar 9-10 due to a silent logging bug
  (fixed in the main bot going forward, but the gap is historical). This
  backfill lets us do forecast-vs-actual analysis for the full 6 weeks of
  settled markets.

Table: KXHIGH_historical_forecasts
  keyed on (city_abv, event_date), MERGE upsert.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

os.environ.setdefault("CLOUDSDK_PYTHON", r"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe")

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cities import CITIES

PROJECT = "elite-contact-446323-q7"
DATASET = "Kalshi"
LOCATION = "northamerica-northeast1"
TABLE = f"{PROJECT}.{DATASET}.KXHIGH_historical_forecasts"

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# GFS = NOAA (closest proxy for NWS); ECMWF IFS = independent second opinion.
MODELS = ["gfs_seamless", "ecmwf_ifs025"]
DAILY_VARS = ["temperature_2m_max", "temperature_2m_min"]


def _get(url: str, timeout: int = 20) -> dict:
    req = Request(url, headers={"User-Agent": "KXHIGH-validation/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_city_range(cfg: dict, start: date, end: date) -> pd.DataFrame:
    """Pull historical forecasts for one city across a date range."""
    q = urlencode({
        "latitude": cfg["lat"],
        "longitude": cfg["lon"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_VARS),
        "models": ",".join(MODELS),
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",  # event dates are always US local
    })
    data = _get(f"{API_URL}?{q}")
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    rows = []
    for i, d in enumerate(dates):
        highs = []
        row = {
            "event_date": d,
            "forecast_elevation_m": data.get("elevation"),
            "forecast_lat": data.get("latitude"),
            "forecast_lon": data.get("longitude"),
        }
        for model in MODELS:
            key = f"temperature_2m_max_{model}"
            v = daily.get(key, [None] * len(dates))[i] if key in daily else None
            row[f"high_f_{model}"] = v
            if v is not None:
                highs.append(v)
            min_key = f"temperature_2m_min_{model}"
            row[f"low_f_{model}"] = daily.get(min_key, [None] * len(dates))[i] if min_key in daily else None
        if highs:
            row["forecast_high_avg"] = sum(highs) / len(highs)
            if len(highs) > 1:
                mean = row["forecast_high_avg"]
                row["forecast_high_std"] = (sum((h - mean) ** 2 for h in highs) / (len(highs) - 1)) ** 0.5
                row["forecast_high_range"] = max(highs) - min(highs)
            else:
                row["forecast_high_std"] = None
                row["forecast_high_range"] = 0.0
            row["forecast_n_models"] = len(highs)
        else:
            row["forecast_high_avg"] = None
            row["forecast_high_std"] = None
            row["forecast_high_range"] = None
            row["forecast_n_models"] = 0
        rows.append(row)
    return pd.DataFrame(rows)


def ensure_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("city_abv",            "STRING", "REQUIRED"),
        bigquery.SchemaField("city",                "STRING"),
        bigquery.SchemaField("lat",                 "FLOAT"),
        bigquery.SchemaField("lon",                 "FLOAT"),
        bigquery.SchemaField("event_date",          "DATE",   "REQUIRED"),
        bigquery.SchemaField("forecast_high_avg",   "FLOAT"),
        bigquery.SchemaField("forecast_high_std",   "FLOAT"),
        bigquery.SchemaField("forecast_high_range", "FLOAT"),
        bigquery.SchemaField("forecast_n_models",   "INTEGER"),
        bigquery.SchemaField("high_f_gfs_seamless",  "FLOAT"),
        bigquery.SchemaField("high_f_ecmwf_ifs025",  "FLOAT"),
        bigquery.SchemaField("low_f_gfs_seamless",   "FLOAT"),
        bigquery.SchemaField("low_f_ecmwf_ifs025",   "FLOAT"),
        bigquery.SchemaField("forecast_elevation_m", "FLOAT"),
        bigquery.SchemaField("forecast_lat",         "FLOAT"),
        bigquery.SchemaField("forecast_lon",         "FLOAT"),
        bigquery.SchemaField("source",               "STRING"),
        bigquery.SchemaField("fetched_at",           "TIMESTAMP"),
    ]
    table = bigquery.Table(TABLE, schema=schema)
    table.clustering_fields = ["city_abv", "event_date"]
    try:
        client.create_table(table)
        print(f"Created {TABLE}")
    except Exception:
        pass


_SCHEMA_ORDER = [
    "city_abv", "city", "lat", "lon", "event_date",
    "forecast_high_avg", "forecast_high_std", "forecast_high_range",
    "forecast_n_models",
    "high_f_gfs_seamless", "high_f_ecmwf_ifs025",
    "low_f_gfs_seamless", "low_f_ecmwf_ifs025",
    "forecast_elevation_m", "forecast_lat", "forecast_lon",
    "source", "fetched_at",
]


def upsert(client: bigquery.Client, df: pd.DataFrame) -> None:
    if df.empty:
        return
    stg = f"{TABLE}_staging"
    df_bq = df.copy()
    df_bq["event_date"] = pd.to_datetime(df_bq["event_date"]).dt.date
    df_bq["fetched_at"] = pd.to_datetime(df_bq["fetched_at"], utc=True)
    for col in _SCHEMA_ORDER:
        if col not in df_bq.columns:
            df_bq[col] = None
    df_bq = df_bq[_SCHEMA_ORDER]
    job = client.load_table_from_dataframe(
        df_bq, stg,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            schema=[
                bigquery.SchemaField("city_abv",            "STRING"),
                bigquery.SchemaField("city",                "STRING"),
                bigquery.SchemaField("lat",                 "FLOAT"),
                bigquery.SchemaField("lon",                 "FLOAT"),
                bigquery.SchemaField("event_date",          "DATE"),
                bigquery.SchemaField("forecast_high_avg",   "FLOAT"),
                bigquery.SchemaField("forecast_high_std",   "FLOAT"),
                bigquery.SchemaField("forecast_high_range", "FLOAT"),
                bigquery.SchemaField("forecast_n_models",   "INTEGER"),
                bigquery.SchemaField("high_f_gfs_seamless",  "FLOAT"),
                bigquery.SchemaField("high_f_ecmwf_ifs025",  "FLOAT"),
                bigquery.SchemaField("low_f_gfs_seamless",   "FLOAT"),
                bigquery.SchemaField("low_f_ecmwf_ifs025",   "FLOAT"),
                bigquery.SchemaField("forecast_elevation_m", "FLOAT"),
                bigquery.SchemaField("forecast_lat",         "FLOAT"),
                bigquery.SchemaField("forecast_lon",         "FLOAT"),
                bigquery.SchemaField("source",               "STRING"),
                bigquery.SchemaField("fetched_at",           "TIMESTAMP"),
            ],
        ),
    )
    job.result()
    merge = f"""
    MERGE `{TABLE}` T
    USING `{stg}` S
    ON T.city_abv = S.city_abv AND T.event_date = S.event_date
    WHEN MATCHED THEN UPDATE SET
      city = S.city, lat = S.lat, lon = S.lon,
      forecast_high_avg = S.forecast_high_avg,
      forecast_high_std = S.forecast_high_std,
      forecast_high_range = S.forecast_high_range,
      forecast_n_models = S.forecast_n_models,
      high_f_gfs_seamless = S.high_f_gfs_seamless,
      high_f_ecmwf_ifs025 = S.high_f_ecmwf_ifs025,
      low_f_gfs_seamless = S.low_f_gfs_seamless,
      low_f_ecmwf_ifs025 = S.low_f_ecmwf_ifs025,
      forecast_elevation_m = S.forecast_elevation_m,
      forecast_lat = S.forecast_lat,
      forecast_lon = S.forecast_lon,
      source = S.source, fetched_at = S.fetched_at
    WHEN NOT MATCHED THEN INSERT ROW
    """
    client.query(merge, location=LOCATION).result()
    client.delete_table(stg, not_found_ok=True)
    print(f"  Upserted {len(df)} rows")


def backfill(start: date, end: date, cities: Iterable[str] | None = None) -> pd.DataFrame:
    keys = list(cities) if cities else list(CITIES.keys())
    frames = []
    for key in keys:
        cfg = CITIES[key]
        print(f"Fetching {key} ({cfg['city']}) {start}..{end}...")
        try:
            df = fetch_city_range(cfg, start, end)
        except Exception as e:
            print(f"  FAILED {key}: {e}")
            continue
        df["city_abv"] = key
        df["city"] = cfg["city"]
        df["lat"] = cfg["lat"]
        df["lon"] = cfg["lon"]
        df["source"] = "open_meteo"
        df["fetched_at"] = datetime.utcnow().isoformat() + "Z"
        frames.append(df)
        time.sleep(0.4)  # Open-Meteo courtesy
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end",   default=date.today().isoformat())
    ap.add_argument("--cities", nargs="*", default=None)
    args = ap.parse_args()

    client = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(client)

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end   = datetime.strptime(args.end,   "%Y-%m-%d").date()
    df = backfill(start, end, args.cities)
    print(f"\n{len(df)} rows fetched.")
    if not df.empty:
        upsert(client, df)


if __name__ == "__main__":
    main()
