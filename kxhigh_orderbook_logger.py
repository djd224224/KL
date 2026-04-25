"""KXHIGH orderbook logger.

Polls Kalshi for the orderbook of every open KXHIGH market, every 15 min,
and appends a row to BigQuery `KXHIGH_orderbook_log`. Used as the data
foundation for two future analyses:

1. **Increment tuning** ("would 1¢ ladder rungs catch more fills than 2¢?"):
   reconstruct the no_lowest_offer trajectory per market over its lifetime
   and ask "did the offer ever touch <integer cent>?". Each touch is a
   hypothetical fill for a rung at that price.

2. **Cancel-time tuning** ("what if we kept orders open until X?"):
   for each canceled order, compare the cancel timestamp to the times
   the market subsequently dipped to the order's bid price. Missed fills
   = orders we'd have caught with later cancel times.

Schema (KXHIGH_orderbook_log, partitioned by polled_at, clustered by
market_ticker):
  polled_at          TIMESTAMP   when we polled
  market_ticker      STRING
  event_ticker       STRING
  city_abv           STRING      e.g. MIA, THOU, TBOS
  event_date         DATE        target high date
  bucket_type        STRING      B (between) or T (tail)
  bucket_val         FLOAT       e.g. 79.5 for B79.5
  market_status      STRING      open / closed / settled
  no_highest_bid     INT64       best NO bid (¢)
  no_lowest_offer    INT64       best NO offer (¢, derived from yes bids)
  yes_highest_bid    INT64       best YES bid (¢)
  yes_lowest_offer   INT64       best YES offer (¢)
  spread_c           INT64       no_lowest_offer − no_highest_bid
  n_no_levels        INT64
  n_yes_levels       INT64
  no_levels_json     STRING      full NO orderbook depth (JSON)
  yes_levels_json    STRING      full YES orderbook depth (JSON)

Run via GitHub Actions cron `*/15 * * * *` (every 15 minutes).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import pandas as pd
from google.cloud import bigquery

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

PROJECT = os.environ.get("BQ_PROJECT", "elite-contact-446323-q7")
DATASET = os.environ.get("BQ_DATASET", "Kalshi")
TABLE = f"{PROJECT}.{DATASET}.KXHIGH_orderbook_log"
LOCATION = "northamerica-northeast1"

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")

TICKER_RE = re.compile(r"^KXHIGH([A-Z]+)-26([A-Z]+)(\d+)-([BT])(\d+\.?\d*)$")


def _load_private_key():
    """Match the same env conventions as fetch_settlements_csv / trading bot."""
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_b64:
        try:
            pem = base64.b64decode(pem_b64)
        except Exception:
            pem = pem_b64.encode()
        return serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
    if not os.path.exists(pem_path):
        raise FileNotFoundError(
            f"No Kalshi private key. Set KALSHI_PRIVATE_KEY (b64) or place PEM at {pem_path!r}."
        )
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def _parse_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Parse KXHIGH ticker into structured fields. Returns None if mismatch."""
    m = TICKER_RE.match(ticker)
    if not m:
        return None
    abv, mon, dd, btype, bval = m.group(1), m.group(2), int(m.group(3)), m.group(4), float(m.group(5))
    try:
        ed = datetime.strptime(f"26{mon}{dd:02d}", "%y%b%d").date()
    except ValueError:
        return None
    return {
        "city_abv": abv,
        "event_ticker": f"KXHIGH{abv}-26{mon}{dd:02d}",
        "event_date": ed,
        "bucket_type": btype,
        "bucket_val": bval,
    }


def _orderbook_to_levels(orderbook_response: dict) -> Tuple[List[List[int]], List[List[int]]]:
    """Normalize Kalshi orderbook response (handles old + new format)."""
    no_levels: List[List[int]] = []
    yes_levels: List[List[int]] = []
    if "orderbook_fp" in orderbook_response:
        ob_fp = orderbook_response["orderbook_fp"] or {}
        for level in ob_fp.get("no_dollars", []) or []:
            no_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
        for level in ob_fp.get("yes_dollars", []) or []:
            yes_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
    elif "orderbook" in orderbook_response:
        ob = orderbook_response["orderbook"] or {}
        no_levels = ob.get("no", []) or []
        yes_levels = ob.get("yes", []) or []
    else:
        no_levels = orderbook_response.get("no", []) or []
        yes_levels = orderbook_response.get("yes", []) or []
    return no_levels, yes_levels


# 20 city event-ticker prefixes matching high_temp_trading.py:728-747.
# Each city has its own daily event; we enumerate today + tomorrow + day-after
# so we cover whatever's currently tradeable.
CITY_EVENT_PREFIXES = [
    "KXHIGHCHI", "KXHIGHNY", "KXHIGHDEN", "KXHIGHPHIL", "KXHIGHAUS",
    "KXHIGHMIA", "KXHIGHTHOU", "KXHIGHLAX", "KXHIGHTATL", "KXHIGHTDC",
    "KXHIGHTPHX", "KXHIGHTDAL", "KXHIGHTLV", "KXHIGHTOKC", "KXHIGHTSEA",
    "KXHIGHTSFO", "KXHIGHTSATX", "KXHIGHTMIN", "KXHIGHTNOLA", "KXHIGHTBOS",
]


def list_open_kxhigh_markets(client: ExchangeClient) -> List[Dict[str, Any]]:
    """Enumerate today/tomorrow/day-after events per city and collect their
    open markets. Mirrors high_temp_trading's pattern (get_event per ticker)."""
    import pytz
    from datetime import timedelta as _td
    central = pytz.timezone("US/Central")
    today = datetime.now(central).date()
    candidate_dates = [today, today + _td(days=1), today + _td(days=2)]
    out: List[Dict[str, Any]] = []
    seen_tickers = set()
    for d in candidate_dates:
        date_code = d.strftime("%y%b%d").upper().lstrip("0")
        # event ticker format: KXHIGHCHI-26APR25 (no leading zero on day for some cities,
        # but actually let's match the on-the-wire format used by the trading script)
        date_code = "26" + d.strftime("%b").upper() + d.strftime("%d")
        for prefix in CITY_EVENT_PREFIXES:
            ev_ticker = f"{prefix}-{date_code}"
            try:
                resp = client.get_event(event_ticker=ev_ticker)
            except Exception:
                continue  # event for that day doesn't exist
            for m in resp.get("markets", []) or []:
                tk = m.get("ticker")
                if not tk or tk in seen_tickers:
                    continue
                # Kalshi returns "active" for tradeable markets, sometimes "open"
                if m.get("status") not in ("open", "active"):
                    continue
                seen_tickers.add(tk)
                out.append(m)
    return out


def ensure_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("polled_at",        "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("market_ticker",    "STRING",    "REQUIRED"),
        bigquery.SchemaField("event_ticker",     "STRING"),
        bigquery.SchemaField("city_abv",         "STRING"),
        bigquery.SchemaField("event_date",       "DATE"),
        bigquery.SchemaField("bucket_type",      "STRING"),
        bigquery.SchemaField("bucket_val",       "FLOAT"),
        bigquery.SchemaField("market_status",    "STRING"),
        bigquery.SchemaField("no_highest_bid",   "INTEGER"),
        bigquery.SchemaField("no_lowest_offer",  "INTEGER"),
        bigquery.SchemaField("yes_highest_bid",  "INTEGER"),
        bigquery.SchemaField("yes_lowest_offer", "INTEGER"),
        bigquery.SchemaField("spread_c",         "INTEGER"),
        bigquery.SchemaField("n_no_levels",      "INTEGER"),
        bigquery.SchemaField("n_yes_levels",     "INTEGER"),
        bigquery.SchemaField("no_levels_json",   "STRING"),
        bigquery.SchemaField("yes_levels_json",  "STRING"),
    ]
    table = bigquery.Table(TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="polled_at",
    )
    table.clustering_fields = ["market_ticker"]
    try:
        client.create_table(table)
        print(f"Created {TABLE}")
    except Exception:
        pass  # already exists


def write_run_marker(bq_client, event, run_id, started_at, **fields):
    """Append one row to KXHIGH_runs (shared with high_temp_trading)."""
    import uuid as _uuid
    table_id = f"{PROJECT}.{DATASET}.KXHIGH_runs"
    row = {
        "run_id": run_id,
        "event": event,
        "event_at": datetime.now(timezone.utc),
        "started_at": started_at,
        "script_name": "kxhigh_orderbook_logger.py",
        "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
    }
    row.update(fields)
    try:
        df = pd.DataFrame([row])
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            schema_update_options=["ALLOW_FIELD_ADDITION"],
        )
        bq_client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    except Exception as e:
        print(f"  RUNS marker fail ({event}): {e}")


def main():
    import uuid
    run_id = str(uuid.uuid4())
    poll_ts = datetime.now(timezone.utc)
    print(f"[{poll_ts.isoformat()}] KXHIGH orderbook poll starting (run_id={run_id})")

    private_key = _load_private_key()
    ex = ExchangeClient(exchange_api_base=KALSHI_API_BASE, key_id=KEY_ID, private_key=private_key)

    bq_client = bigquery.Client(project=PROJECT)
    write_run_marker(bq_client, "start", run_id, poll_ts)

    # 1. List open KXHIGH markets
    markets = list_open_kxhigh_markets(ex)
    print(f"  found {len(markets)} open KXHIGH markets")
    if not markets:
        print("  nothing to log; exiting")
        write_run_marker(bq_client, "end", run_id, poll_ts,
                         finished_at=datetime.now(timezone.utc),
                         n_markets_seen=0, n_orderbook_rows=0,
                         duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                         exit_status="empty_market_list")
        return

    ensure_table(bq_client)

    # 2. Poll orderbook per market and build rows
    rows: List[Dict[str, Any]] = []
    fails = 0
    for i, m in enumerate(markets):
        ticker = m.get("ticker")
        if not ticker:
            continue
        parsed = _parse_ticker(ticker) or {
            "city_abv": None, "event_ticker": None, "event_date": None,
            "bucket_type": None, "bucket_val": None,
        }
        try:
            ob = ex.get_orderbook(ticker=ticker)
        except Exception as e:
            fails += 1
            if fails < 5:
                print(f"  ! orderbook fail for {ticker}: {e}")
            continue
        no_levels, yes_levels = _orderbook_to_levels(ob)
        no_bid = no_levels[-1][0] if no_levels else None
        yes_bid = yes_levels[-1][0] if yes_levels else None
        # NO offer is implied by the highest YES bid (Kalshi convention)
        no_offer = (100 - yes_bid) if yes_bid is not None else None
        yes_offer = (100 - no_bid) if no_bid is not None else None
        spread = (no_offer - no_bid) if (no_bid is not None and no_offer is not None) else None
        rows.append({
            "polled_at": poll_ts,
            "market_ticker": ticker,
            "event_ticker": parsed["event_ticker"],
            "city_abv": parsed["city_abv"],
            "event_date": parsed["event_date"],
            "bucket_type": parsed["bucket_type"],
            "bucket_val": parsed["bucket_val"],
            "market_status": m.get("status"),
            "no_highest_bid": no_bid,
            "no_lowest_offer": no_offer,
            "yes_highest_bid": yes_bid,
            "yes_lowest_offer": yes_offer,
            "spread_c": spread,
            "n_no_levels": len(no_levels),
            "n_yes_levels": len(yes_levels),
            "no_levels_json": json.dumps(no_levels),
            "yes_levels_json": json.dumps(yes_levels),
        })
        # gentle pacing (~5 markets/s; Kalshi public endpoint allows 100/s but
        # we don't need fast and want to be polite)
        time.sleep(0.05)

    print(f"  collected {len(rows)} orderbook rows ({fails} failures)")
    if not rows:
        write_run_marker(bq_client, "end", run_id, poll_ts,
                         finished_at=datetime.now(timezone.utc),
                         n_markets_seen=len(markets), n_orderbook_rows=0,
                         n_fails=fails,
                         duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                         exit_status="no_rows_collected")
        return

    # 3. Append to BQ
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    job = bq_client.load_table_from_dataframe(df, TABLE, job_config=job_config)
    job.result()
    print(f"  wrote {len(df)} rows -> {TABLE}")
    write_run_marker(bq_client, "end", run_id, poll_ts,
                     finished_at=datetime.now(timezone.utc),
                     n_markets_seen=len(markets), n_orderbook_rows=len(df),
                     n_fails=fails,
                     duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                     exit_status="success")


if __name__ == "__main__":
    main()
