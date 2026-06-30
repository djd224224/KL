#!/usr/bin/env python
"""Apply the KXHIGH analysis view DDLs to BigQuery in dependency order.

The KXHIGH analysis views (KXHIGH_settlements_clean, _fills_clean,
_orders_clean, _model_call_snapshots, _resolved_markets, _fills_enriched,
_orders_fills) are defined in ``analysis/kxhigh/sql/*.sql`` and are NOT
maintained by any other process: ``load.py`` only reads them and the live
dashboard loop only renders. A base-table rebuild — e.g. the 2026-05-08
``KXHIGH_market_snapshot`` / ``KXHIGH_settlements`` drop-and-recreate — leaves
every dependent view broken or missing, which then fails *silently*
(dashboard history shows "unsettled", bias correction raises BIAS_QUERY_FAILED
and trades on raw forecasts).

Run this after any base-table rebuild, or on a schedule, to keep the view
layer in sync. Files are applied in sorted filename order; the numeric
prefixes encode the dependency order (05 before 20 before 30, ...). Only files
containing ``CREATE OR REPLACE`` are applied, so the SELECT-only sanity-check
(00_*) and report (90_*) files are skipped. Every DDL is ``CREATE OR REPLACE``,
so this is idempotent and safe to re-run.
"""
from __future__ import annotations

import glob
import os
import sys

os.environ.setdefault(
    "CLOUDSDK_PYTHON", r"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
)

from google.cloud import bigquery

PROJECT = "elite-contact-446323-q7"
DATASET = "Kalshi"
LOCATION = "northamerica-northeast1"
SQL_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sql"))


def main() -> int:
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    if not files:
        print(f"No .sql files found in {SQL_DIR}")
        return 1

    applied, failures = [], []
    for path in files:
        name = os.path.basename(path)
        sql = open(path, encoding="utf-8").read()
        if "CREATE OR REPLACE" not in sql.upper():
            print(f"SKIP  {name} (no CREATE OR REPLACE — sanity/report file)")
            continue
        try:
            client.query(sql, location=LOCATION).result()
            print(f"OK    {name}")
            applied.append(name)
        except Exception as e:  # noqa: BLE001 — report all, decide at end
            print(f"FAIL  {name}: {str(e)[:300]}")
            failures.append((name, str(e)))

    print(f"\nApplied {len(applied)} view DDL(s); {len(failures)} failed.")
    if failures:
        for n, e in failures:
            print(f"  - {n}: {e[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
