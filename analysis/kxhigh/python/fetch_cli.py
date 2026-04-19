"""Fetch realized high temperatures from NWS Daily Climate Reports (CLI).

Two sources:
  - Iowa State IEM JSON API (primary): pre-parsed CLI records for the whole
    year, per station. Use for backfill AND current (latest CLI issued).
    https://mesonet.agron.iastate.edu/json/cli.py?station=<ICAO>&year=<YYYY>

  - NWS direct (fallback): raw CLI text product, useful if IEM is missing
    a record or for intra-day verification.
    https://forecast.weather.gov/product.php?site=<WFO>&issuedby=<CLI_CODE>
      &product=CLI&format=txt&version=1&glossary=0

Writes to BigQuery table KXHIGH_cli_readings (city_abv + event_date unique).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime
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
TABLE = f"{PROJECT}.{DATASET}.KXHIGH_cli_readings"


# ---------- fetchers ----------
def _get(url: str, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": "KXHIGH-validation/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_iem_year(icao: str, year: int) -> list[dict]:
    """Pull the full year of CLI records for one station from IEM."""
    url = f"https://mesonet.agron.iastate.edu/json/cli.py?station={icao}&year={year}"
    import json
    data = json.loads(_get(url))
    return data.get("results", [])


_NWS_HIGH_RE = re.compile(
    r"MAXIMUM\s+(\d+)\s+\d+\s*(?:AM|PM)\b",
    re.IGNORECASE,
)


def fetch_nws_direct(wfo: str, cli_code: str) -> tuple[date, int] | None:
    """Fetch the latest CLI text product from NWS and parse out the max temp.

    Returns (event_date, high_f) or None on failure. Only useful for "today's"
    reading when IEM hasn't yet ingested the product; date = yesterday (the
    day the CLI is reporting on).
    """
    q = urlencode({
        "site": wfo, "issuedby": cli_code, "product": "CLI",
        "format": "txt", "version": "1", "glossary": "0",
    })
    url = f"https://forecast.weather.gov/product.php?{q}"
    try:
        html = _get(url)
    except Exception as e:
        print(f"  NWS fetch failed for {wfo}/{cli_code}: {e}")
        return None
    # Strip HTML for the <pre>-wrapped report
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)
    # The "YESTERDAY" section contains the MAXIMUM line we want
    ys = body.upper().find("YESTERDAY")
    if ys < 0:
        return None
    segment = body[ys:ys + 2000]
    m2 = _NWS_HIGH_RE.search(segment)
    if not m2:
        return None
    # Event date: date reported on (yesterday, relative to issue time)
    # The report usually says "CLIMATE REPORT / NATIONAL WEATHER SERVICE ... / <DAY OF WEEK> <MONTH> <DAY> <YYYY>"
    date_m = re.search(
        r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+(\d{1,2})\s+(\d{4})",
        body.upper(),
    )
    if date_m:
        try:
            issue_d = datetime.strptime(
                f"{date_m.group(1)[:3]} {date_m.group(2)} {date_m.group(3)}", "%b %d %Y"
            ).date()
        except ValueError:
            return None
    else:
        return None
    from datetime import timedelta
    event_d = issue_d - timedelta(days=1)
    return event_d, int(m2.group(1))


# ---------- BQ io ----------
def ensure_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("city_abv",    "STRING", "REQUIRED"),
        bigquery.SchemaField("city",        "STRING"),
        bigquery.SchemaField("icao",        "STRING"),
        bigquery.SchemaField("cli_code",    "STRING"),
        bigquery.SchemaField("wfo",         "STRING"),
        bigquery.SchemaField("event_date",  "DATE",   "REQUIRED"),
        bigquery.SchemaField("high_temp_f", "INTEGER"),
        bigquery.SchemaField("low_temp_f",  "INTEGER"),
        bigquery.SchemaField("high_time",   "STRING"),
        bigquery.SchemaField("product_id",  "STRING"),
        bigquery.SchemaField("source",      "STRING"),
        bigquery.SchemaField("fetched_at",  "TIMESTAMP"),
    ]
    table = bigquery.Table(TABLE, schema=schema)
    table.clustering_fields = ["city_abv", "event_date"]
    try:
        client.create_table(table)
        print(f"Created {TABLE}")
    except Exception:
        pass  # Already exists


def upsert(client: bigquery.Client, df: pd.DataFrame) -> None:
    """MERGE upsert on (city_abv, event_date)."""
    if df.empty:
        return
    stg = f"{TABLE}_staging"
    # Load to staging (truncate)
    df_bq = df.copy()
    df_bq["event_date"] = pd.to_datetime(df_bq["event_date"]).dt.date
    df_bq["fetched_at"] = pd.to_datetime(df_bq["fetched_at"], utc=True)
    job = client.load_table_from_dataframe(
        df_bq, stg,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            schema=[
                bigquery.SchemaField("city_abv",    "STRING"),
                bigquery.SchemaField("city",        "STRING"),
                bigquery.SchemaField("icao",        "STRING"),
                bigquery.SchemaField("cli_code",    "STRING"),
                bigquery.SchemaField("wfo",         "STRING"),
                bigquery.SchemaField("event_date",  "DATE"),
                bigquery.SchemaField("high_temp_f", "INTEGER"),
                bigquery.SchemaField("low_temp_f",  "INTEGER"),
                bigquery.SchemaField("high_time",   "STRING"),
                bigquery.SchemaField("product_id",  "STRING"),
                bigquery.SchemaField("source",      "STRING"),
                bigquery.SchemaField("fetched_at",  "TIMESTAMP"),
            ],
        ),
    )
    job.result()
    merge = f"""
    MERGE `{TABLE}` T
    USING `{stg}` S
    ON T.city_abv = S.city_abv AND T.event_date = S.event_date
    WHEN MATCHED THEN UPDATE SET
      city = S.city, icao = S.icao, cli_code = S.cli_code, wfo = S.wfo,
      high_temp_f = S.high_temp_f, low_temp_f = S.low_temp_f,
      high_time = S.high_time, product_id = S.product_id,
      source = S.source, fetched_at = S.fetched_at
    WHEN NOT MATCHED THEN INSERT ROW
    """
    client.query(merge, location=LOCATION).result()
    client.delete_table(stg, not_found_ok=True)
    print(f"  Upserted {len(df)} rows")


# ---------- main flow ----------
def _coerce_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def backfill_iem(year: int, cities: Iterable[str] | None = None) -> pd.DataFrame:
    """Backfill an entire year for specified city_abv keys (or all)."""
    keys = list(cities) if cities else list(CITIES.keys())
    rows = []
    for key in keys:
        cfg = CITIES[key]
        print(f"Fetching {key} ({cfg['icao']}) for {year}...")
        try:
            records = fetch_iem_year(cfg["icao"], year)
        except Exception as e:
            print(f"  FAILED {key}: {e}")
            continue
        for r in records:
            high = _coerce_int(r.get("high"))
            if high is None:
                continue
            rows.append({
                "city_abv": key,
                "city": cfg["city"],
                "icao": cfg["icao"],
                "cli_code": cfg["cli_code"],
                "wfo": cfg["wfo"],
                "event_date": r.get("valid"),
                "high_temp_f": high,
                "low_temp_f": _coerce_int(r.get("low")),
                "high_time": r.get("high_time"),
                "product_id": r.get("product"),
                "source": "iem",
                "fetched_at": datetime.utcnow().isoformat() + "Z",
            })
        time.sleep(0.3)  # IEM rate-limit courtesy
    return pd.DataFrame(rows)


def pull_current_nws() -> pd.DataFrame:
    """Fetch the latest CLI product for every city via NWS direct."""
    rows = []
    for key, cfg in CITIES.items():
        print(f"NWS direct: {key} ({cfg['wfo']}/{cfg['cli_code']})...")
        result = fetch_nws_direct(cfg["wfo"], cfg["cli_code"])
        if result is None:
            print("  (no parse)")
            continue
        event_d, high = result
        rows.append({
            "city_abv": key,
            "city": cfg["city"],
            "icao": cfg["icao"],
            "cli_code": cfg["cli_code"],
            "wfo": cfg["wfo"],
            "event_date": event_d.isoformat(),
            "high_temp_f": high,
            "low_temp_f": None,
            "high_time": None,
            "product_id": None,
            "source": "nws_direct",
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })
        time.sleep(0.5)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "current"],
                    help="backfill = pull year via IEM; current = NWS direct for latest")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--cities", nargs="*", default=None,
                    help="Subset of city_abv keys (e.g. NY- CHI MIA). Default: all.")
    args = ap.parse_args()

    client = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(client)

    if args.mode == "backfill":
        df = backfill_iem(args.year, args.cities)
    else:
        df = pull_current_nws()

    print(f"\n{len(df)} rows fetched. Upserting...")
    if not df.empty:
        upsert(client, df)
        print(df.groupby("city_abv").size().to_string())


if __name__ == "__main__":
    main()
