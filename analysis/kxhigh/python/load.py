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
    return _q(f"SELECT * FROM `{PROJECT}.{DATASET}.KXHIGH_settlements`")


def load_all() -> dict:
    return {
        "resolved": resolved_markets(),
        "fills": fills_enriched(),
        "orders": orders_fills(),
        "snapshots": model_call_snapshots(),
        "settlements": settlements(),
    }


if __name__ == "__main__":
    data = load_all()
    for name, df in data.items():
        print(f"{name}: {len(df)} rows, {len(df.columns)} cols")
