"""Load KXHIGH views from BigQuery into pandas DataFrames."""
from __future__ import annotations

import os
from functools import lru_cache

os.environ.setdefault("CLOUDSDK_PYTHON", r"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe")

from google.cloud import bigquery

PROJECT = "elite-contact-446323-q7"
DATASET = "Kalshi"
LOCATION = "northamerica-northeast1"


@lru_cache(maxsize=1)
def client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _q(sql: str):
    return client().query(sql, location=LOCATION).to_dataframe()


def resolved_markets():
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_resolved_markets`")


def fills_enriched():
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_fills_enriched`")


def orders_fills():
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_orders_fills`")


def model_call_snapshots():
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_model_call_snapshots`")


def settlements():
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_settlements_clean`")


def event_forecast_accuracy():
    """One row per (city, event_date) with a bot snapshot AND a CLI reading.
    Decoupled from settlements so we get forecast-vs-actual for every day the
    bot saw a forecast, even if it didn't take a position that day."""
    sql = f"""
    WITH forecast_per_event AS (
      SELECT city, forecast_date,
        ANY_VALUE(forecast_avg)         AS forecast_avg,
        ANY_VALUE(forecast_std)         AS forecast_std,
        ANY_VALUE(forecast_range)       AS forecast_range,
        ANY_VALUE(nws)                  AS nws,
        ANY_VALUE(weather_underground)  AS weather_underground
      FROM `{PROJECT}.{DATASET}.KXHIGH_model_call_snapshots`
      WHERE forecast_avg IS NOT NULL
      GROUP BY city, forecast_date
    ),
    city_lookup AS (
      SELECT DISTINCT city, city_abv FROM `{PROJECT}.{DATASET}.KXHIGH_cli_readings`
    )
    SELECT
      cl.city_abv, cl.city, f.forecast_date,
      f.forecast_avg, f.forecast_std, f.forecast_range,
      f.nws, f.weather_underground,
      CAST(cli.high_temp_f AS FLOAT64) AS actual_high_f,
      EXISTS(
        SELECT 1 FROM `{PROJECT}.{DATASET}.KXHIGH_settlements_clean` sc
        WHERE sc.city_abv = cl.city_abv AND sc.event_date = f.forecast_date
      ) AS was_traded
    FROM forecast_per_event f
    JOIN city_lookup cl USING (city)
    JOIN `{PROJECT}.{DATASET}.KXHIGH_cli_readings` cli
      ON cli.city_abv = cl.city_abv AND cli.event_date = f.forecast_date
    """
    return _q(sql)


def load_all() -> dict:
    return {
        "resolved": resolved_markets(),
        "fills": fills_enriched(),
        "orders": orders_fills(),
        "snapshots": model_call_snapshots(),
        "settlements": settlements(),
        "event_accuracy": event_forecast_accuracy(),
    }


if __name__ == "__main__":
    data = load_all()
    for name, df in data.items():
        print(f"{name}: {len(df)} rows, {len(df.columns)} cols")
