#!/usr/bin/env python3
"""
Kalshi Longshot Seller Bot
==========================
Systematically sells overpriced Yes contracts on longshot outcomes
for curated series with discrete resolution events.

Uses ExchangeClient from KalshiClientsBaseV2ApiKey_FIXED (same auth
as the NBA mention trading script).

Environment variables (set as GitHub Actions secrets):
    KALSHI_API_KEY_ID           Your Kalshi API key ID
    KALSHI_PRIVATE_KEY_PATH     Path to your RSA private key PEM file
    GOOGLE_APPLICATION_CREDENTIALS  Path to GCP service account JSON
    GCP_PROJECT_ID              BigQuery project ID
    GCP_DATASET_ID              BigQuery dataset ID

Usage:
    python kalshi_longshot_bot.py
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from google.cloud import bigquery
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

# ════════════════════════════════════════════════════════════
#  CONFIGURATION — Edit everything in this section
# ════════════════════════════════════════════════════════════

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# BigQuery
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_DATASET_ID = os.environ.get("GCP_DATASET_ID", "")
BQ_ORDERS_TABLE = "KXLONGSHOT_orders"
BQ_SNAPSHOTS_TABLE = "KXLONGSHOT_market_snapshots"

# ── Strategy Parameters ──────────────────────────────────

MIN_YES_PRICE = 0.03
MAX_YES_PRICE = 0.15
BIAS_DISCOUNT = 0.65
MIN_EV_DOLLARS = 0.01
FEE_RATE = 0.02
MIN_VOLUME = 10

# Order ladder: place sell orders at 5 price levels stepping up
NUM_LEVELS = 5
LEVEL_INCREMENT_CENTS = 1
CONTRACTS_PER_LEVEL = 10

QUOTE_PULLBACK_HOURS = 5

# ── Risk Management ──────────────────────────────────────

MAX_RISK_PER_CONTRACT = 50
MAX_TOTAL_RISK = 2000
MAX_CONTRACTS_PER_EVENT = 25
MAX_CONTRACTS_PER_ENTITY = 15
MAX_SAME_DAY_EXPIRY = 15
DAILY_STOP_LOSS = -200
PRICE_SPIKE_THRESHOLD = 0.10
MAX_CONTRACTS_PER_MARKET = 50

# ── Bot Behavior ─────────────────────────────────────────

TRADE_LOG_PATH = "trades.jsonl"
LOG_LEVEL = "INFO"
SLEEP_BETWEEN_ORDERS = 0.05

RUN_ID = str(uuid.uuid4())
MODEL_VERSION = "longshot-v1"

# ── Curated Series ───────────────────────────────────────

CURATED_SERIES = [
    {
        "series_ticker": "KXPRESMENTION",
        "category": "political",
        "entity": "potus-mention",
        "description": "Presidential press conference mentions",
    },
    {
        "series_ticker": "KXMENTION",
        "category": "political",
        "entity": "mention",
        "description": "General mention contracts",
    },
]

# ════════════════════════════════════════════════════════════
#  END CONFIGURATION
# ════════════════════════════════════════════════════════════


# ────────────────────────────────────────────────────────────
# Auth
# ────────────────────────────────────────────────────────────

def load_private_key_from_file(file_path: str):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend(),
        )
    return private_key


PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)

exchange_client = ExchangeClient(
    exchange_api_base=API_BASE,
    key_id=API_KEY_ID,
    private_key=PRIVATE_KEY,
)

print("Testing Kalshi connection...")
try:
    status = exchange_client.get_exchange_status()
    print(f"✓ Connected! Trading active: {status['trading_active']}")
except Exception as e:
    print(f"✗ Connection failed: {e}")
    raise

# ────────────────────────────────────────────────────────────
# BigQuery (same pattern as NBA script)
# ────────────────────────────────────────────────────────────

bq_client = bigquery.Client(project=GCP_PROJECT_ID)
print(f"✓ BigQuery client initialized (project: {GCP_PROJECT_ID})")


def get_existing_table_schema(table_name: str) -> Optional[Dict[str, str]]:
    try:
        table_id = f"{GCP_PROJECT_ID}.{GCP_DATASET_ID}.{table_name}"
        table = bq_client.get_table(table_id)
        return {field.name: field.field_type for field in table.schema}
    except Exception:
        return None


def pandas_dtype_to_bq_type(dtype):
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    elif pd.api.types.is_float_dtype(dtype):
        return "FLOAT"
    elif pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    else:
        return "STRING"


def add_missing_columns_to_table(table_name: str, df: pd.DataFrame,
                                  existing_schema: dict):
    table_id = f"{GCP_PROJECT_ID}.{GCP_DATASET_ID}.{table_name}"
    table = bq_client.get_table(table_id)
    original_schema = table.schema
    new_schema = original_schema[:]
    for col_name in df.columns:
        if col_name not in existing_schema:
            bq_type = pandas_dtype_to_bq_type(df[col_name].dtype)
            print(f"  Adding column: {col_name} ({bq_type})")
            new_schema.append(
                bigquery.SchemaField(col_name, bq_type, mode="NULLABLE")
            )
    if len(new_schema) > len(original_schema):
        table.schema = new_schema
        bq_client.update_table(table, ["schema"])
        print(f"  ✓ Updated schema for {table_name}")


def convert_df_to_match_schema(df: pd.DataFrame,
                                schema_dict: dict) -> pd.DataFrame:
    df_converted = df.copy()
    for col_name, bq_type in schema_dict.items():
        if col_name not in df_converted.columns:
            continue
        current_dtype = df_converted[col_name].dtype
        if bq_type == "STRING" and not pd.api.types.is_string_dtype(current_dtype):
            df_converted[col_name] = (
                df_converted[col_name].astype(str)
                .replace("nan", None).replace("None", None)
            )
        elif bq_type == "FLOAT" and not pd.api.types.is_float_dtype(current_dtype):
            df_converted[col_name] = pd.to_numeric(
                df_converted[col_name], errors="coerce"
            )
        elif bq_type == "INTEGER" and not pd.api.types.is_integer_dtype(current_dtype):
            df_converted[col_name] = pd.to_numeric(
                df_converted[col_name], errors="coerce"
            ).astype("Int64")
        elif bq_type == "BOOLEAN" and not pd.api.types.is_bool_dtype(current_dtype):
            df_converted[col_name] = df_converted[col_name].astype(bool)
    return df_converted


def df_to_bq(df: pd.DataFrame, table_name: str,
             write_disposition: str = "WRITE_APPEND"):
    """Upload DataFrame to BigQuery (same as NBA script)."""
    if df is None or len(df) == 0:
        print(f"  ⚠️ Empty DataFrame — skipping upload to {table_name}")
        return

    table_id = f"{GCP_PROJECT_ID}.{GCP_DATASET_ID}.{table_name}"
    df_to_upload = df.copy()

    if write_disposition == "WRITE_APPEND":
        existing_schema = get_existing_table_schema(table_name)
        if existing_schema is not None:
            new_cols = set(df_to_upload.columns) - set(existing_schema.keys())
            if new_cols:
                print(f"  ⚠️ New columns: {new_cols}")
                add_missing_columns_to_table(
                    table_name, df_to_upload, existing_schema
                )
                existing_schema = get_existing_table_schema(table_name)
            df_to_upload = convert_df_to_match_schema(
                df_to_upload, existing_schema
            )

    job_config = bigquery.LoadJobConfig(
        write_disposition=write_disposition,
        autodetect=True,
    )
    job = bq_client.load_table_from_dataframe(
        df_to_upload, table_id, job_config=job_config
    )
    job.result()
    print(
        f"  ✓ Uploaded to {table_id}: "
        f"{bq_client.get_table(table_id).num_rows} rows"
    )


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def parse_close_time(close_time_str: Optional[str]) -> Optional[datetime]:
    if not close_time_str:
        return None
    try:
        ts = close_time_str
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def is_within_pullback(close_time: Optional[datetime]) -> bool:
    if close_time is None:
        return False
    cutoff = close_time - timedelta(hours=QUOTE_PULLBACK_HOURS)
    return datetime.now(timezone.utc) >= cutoff


def get_all_markets_for_series(series_ticker: str) -> List[Dict[str, Any]]:
    all_markets = []
    cursor = None
    while True:
        params = {"series_ticker": series_ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            response = exchange_client.get_markets(**params)
            markets = response.get("markets", [])
            all_markets.extend(markets)
            cursor = response.get("cursor")
            if not cursor:
                break
            time.sleep(0.05)
        except Exception as e:
            logging.error(f"Error fetching markets for {series_ticker}: {e}")
            break
    return all_markets


# ────────────────────────────────────────────────────────────
# Core Logic
# ────────────────────────────────────────────────────────────

def scan_and_trade():
    log = logging.getLogger("longshot-bot")

    log.info("=" * 60)
    log.info("Starting scan cycle...")

    # Collectors for BQ upload
    order_results: List[Dict[str, Any]] = []
    market_snapshots: List[Dict[str, Any]] = []

    # ── Step 1: Fetch markets for all curated series ─────

    all_candidates = []

    for series_cfg in CURATED_SERIES:
        series_ticker = series_cfg["series_ticker"]
        entity = series_cfg.get("entity", series_ticker)
        category = series_cfg.get("category", "unknown")

        log.info(f"\nFetching {series_ticker}...")
        markets = get_all_markets_for_series(series_ticker)
        log.info(f"  Got {len(markets)} total markets")

        active = [m for m in markets if m.get("status") in ("active", "open")]
        log.info(f"  Active: {len(active)}")

        for i, dbg_mkt in enumerate(active[:3]):
            log.info(
                f"  DEBUG mkt[{i}]: ticker={dbg_mkt.get('ticker')} "
                f"yes_bid_dollars={dbg_mkt.get('yes_bid_dollars')} "
                f"yes_ask_dollars={dbg_mkt.get('yes_ask_dollars')} "
                f"close_time={dbg_mkt.get('close_time')}"
            )

        skipped_pullback = 0
        skipped_no_price = 0
        skipped_price_range = 0
        skipped_ev = 0
        passed = 0

        for mkt in active:
            ticker = mkt.get("ticker", "")

            close_dt = parse_close_time(mkt.get("close_time"))
            if close_dt and is_within_pullback(close_dt):
                skipped_pullback += 1
                continue

            yes_ask_raw = mkt.get("yes_ask_dollars")
            yes_bid_raw = mkt.get("yes_bid_dollars")

            if yes_ask_raw is None or yes_bid_raw is None:
                skipped_no_price += 1
                continue

            try:
                yes_ask_d = float(yes_ask_raw)
                yes_bid_d = float(yes_bid_raw)
            except (ValueError, TypeError):
                skipped_no_price += 1
                continue

            if yes_ask_d <= 0 or yes_bid_d < 0:
                skipped_no_price += 1
                continue

            yes_ask = int(round(yes_ask_d * 100))
            yes_bid = int(round(yes_bid_d * 100))
            yes_mid = (yes_bid_d + yes_ask_d) / 2.0

            if not (MIN_YES_PRICE <= yes_mid <= MAX_YES_PRICE):
                skipped_price_range += 1
                continue

            true_prob = yes_mid * BIAS_DISCOUNT
            sell_price = yes_ask_d
            profit_if_no = sell_price * (1 - FEE_RATE)
            loss_if_yes = (1.0 - sell_price) + (sell_price * FEE_RATE)
            ev = (1 - true_prob) * profit_if_no - true_prob * loss_if_yes

            if ev < MIN_EV_DOLLARS:
                skipped_ev += 1
                continue

            passed += 1
            risk_per = 1.0 - sell_price

            price_range = MAX_YES_PRICE - MIN_YES_PRICE
            price_score = (
                (1.0 - (yes_mid - MIN_YES_PRICE) / price_range)
                if price_range > 0 else 0.5
            )
            ev_score = min(ev / 0.05, 1.0)
            score = 0.6 * ev_score + 0.4 * price_score

            candidate = {
                "ticker": ticker,
                "event_ticker": mkt.get("event_ticker", ""),
                "title": mkt.get("title") or mkt.get("subtitle") or ticker,
                "series_ticker": series_ticker,
                "entity": entity,
                "category": category,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "yes_mid": yes_mid,
                "sell_price_d": sell_price,
                "ev": ev,
                "true_prob": true_prob,
                "risk_per": risk_per,
                "score": score,
                "close_time": close_dt,
                "close_time_str": mkt.get("close_time"),
                "volume": mkt.get("volume", 0) or 0,
                "open_interest": mkt.get("open_interest", 0) or 0,
            }
            all_candidates.append(candidate)

            # Save market snapshot for BQ
            market_snapshots.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": RUN_ID,
                "model_version": MODEL_VERSION,
                "ticker": ticker,
                "event_ticker": mkt.get("event_ticker", ""),
                "series_ticker": series_ticker,
                "entity": entity,
                "category": category,
                "title": candidate["title"],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "yes_mid": round(yes_mid, 4),
                "ev": round(ev, 4),
                "true_prob": round(true_prob, 4),
                "bias_discount": BIAS_DISCOUNT,
                "score": round(score, 3),
                "risk_per_contract": round(risk_per, 4),
                "volume": candidate["volume"],
                "open_interest": candidate["open_interest"],
                "close_time": mkt.get("close_time"),
                "pullback_hours": QUOTE_PULLBACK_HOURS,
            })

        log.info(
            f"  Filter summary: pullback={skipped_pullback} "
            f"no_price={skipped_no_price} price_range={skipped_price_range} "
            f"ev={skipped_ev} passed={passed}"
        )

    # Sort by score
    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(
        f"\nFiltered to {len(all_candidates)} candidates "
        f"(${MIN_YES_PRICE}-${MAX_YES_PRICE}, EV>${MIN_EV_DOLLARS})"
    )

    if not all_candidates:
        log.info("No candidates found. Done.")
        _upload_to_bq(order_results, market_snapshots, log)
        return

    # ── Step 2: Apply risk limits and select ─────────────

    selected = []
    event_counts: Dict[str, int] = {}
    entity_counts: Dict[str, int] = {}
    day_counts: Dict[str, int] = {}
    total_risk = 0.0

    for c in all_candidates:
        ek = c["event_ticker"]
        if event_counts.get(ek, 0) >= MAX_CONTRACTS_PER_EVENT:
            continue

        ent = c["entity"]
        if entity_counts.get(ent, 0) >= MAX_CONTRACTS_PER_ENTITY:
            continue

        if c["close_time"]:
            day_key = c["close_time"].strftime("%Y-%m-%d")
            if day_counts.get(day_key, 0) >= MAX_SAME_DAY_EXPIRY:
                continue

        max_total_contracts = NUM_LEVELS * CONTRACTS_PER_LEVEL
        contracts = min(
            int(MAX_RISK_PER_CONTRACT / c["risk_per"]),
            max_total_contracts,
            MAX_CONTRACTS_PER_MARKET,
        )
        if contracts < 1:
            continue

        added_risk = contracts * c["risk_per"]
        if total_risk + added_risk > MAX_TOTAL_RISK:
            contracts = int(
                (MAX_TOTAL_RISK - total_risk) / c["risk_per"]
            )
            if contracts < 1:
                log.info("Total risk limit reached.")
                break

        c["contracts"] = contracts
        selected.append(c)

        event_counts[ek] = event_counts.get(ek, 0) + 1
        entity_counts[ent] = entity_counts.get(ent, 0) + 1
        if c["close_time"]:
            day_key = c["close_time"].strftime("%Y-%m-%d")
            day_counts[day_key] = day_counts.get(day_key, 0) + 1
        total_risk += contracts * c["risk_per"]

    log.info(f"Selected {len(selected)} positions (risk: ${total_risk:.2f})")

    if not selected:
        log.info("Nothing selected. Done.")
        _upload_to_bq(order_results, market_snapshots, log)
        return

    # ── Step 3: Place orders ─────────────────────────────

    success_count = 0
    fail_count = 0

    for c in selected:
        ticker = c["ticker"]
        base_price = c["yes_ask"]

        EXPIRATION_HOURS_BEFORE_CLOSE = 21
        expiration_ts = None
        if c["close_time"]:
            close_unix = int(c["close_time"].timestamp())
            expiration_ts = close_unix - (EXPIRATION_HOURS_BEFORE_CLOSE * 3600)
            if expiration_ts <= int(time.time()):
                expiration_ts = None

        max_yes_cents = int(MAX_YES_PRICE * 100)
        levels_placed = []

        for level in range(NUM_LEVELS):
            yes_price_cents = base_price + (level * LEVEL_INCREMENT_CENTS)

            if yes_price_cents > max_yes_cents:
                break

            sell_price_d = yes_price_cents / 100.0
            true_prob = c["true_prob"]
            profit_if_no = sell_price_d * (1 - FEE_RATE)
            loss_if_yes = (1.0 - sell_price_d) + (sell_price_d * FEE_RATE)
            level_ev = (1 - true_prob) * profit_if_no - true_prob * loss_if_yes

            if level_ev < MIN_EV_DOLLARS:
                break

            client_order_id = str(uuid.uuid4())

            order_params = {
                "ticker": ticker,
                "action": "sell",
                "side": "yes",
                "type": "limit",
                "count": CONTRACTS_PER_LEVEL,
                "yes_price": yes_price_cents,
                "client_order_id": client_order_id,
            }
            if expiration_ts is not None:
                order_params["expiration_ts"] = expiration_ts

            order_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": RUN_ID,
                "model_version": MODEL_VERSION,
                "ticker": ticker,
                "event_ticker": c["event_ticker"],
                "series_ticker": c["series_ticker"],
                "entity": c["entity"],
                "category": c["category"],
                "title": c["title"],
                "side": "yes",
                "action": "sell",
                "yes_price_cents": yes_price_cents,
                "count": CONTRACTS_PER_LEVEL,
                "level": level,
                "ev": round(level_ev, 4),
                "true_prob": round(true_prob, 4),
                "bias_discount": BIAS_DISCOUNT,
                "score": round(c["score"], 3),
                "close_time": c["close_time_str"],
                "expiration_ts": expiration_ts,
                "risk_dollars": round(
                    CONTRACTS_PER_LEVEL * (1.0 - sell_price_d), 2
                ),
                "yes_bid": c["yes_bid"],
                "yes_ask": c["yes_ask"],
                "yes_mid": round(c["yes_mid"], 4),
                "volume": c["volume"],
                "open_interest": c["open_interest"],
                "client_order_id": client_order_id,
            }

            try:
                response = exchange_client.create_order(**order_params)
                order = response.get("order", {})
                order_id = order.get("order_id", "")

                order_record["ok"] = True
                order_record["order_id"] = order_id
                order_record["error"] = None
                levels_placed.append(f"{yes_price_cents}¢")
                success_count += 1

            except Exception as e:
                log.error(f"  ✗ Level {level} @ {yes_price_cents}¢: {e}")
                order_record["ok"] = False
                order_record["order_id"] = None
                order_record["error"] = str(e)[:500]
                fail_count += 1

            order_results.append(order_record)
            time.sleep(SLEEP_BETWEEN_ORDERS)

        if levels_placed:
            log.info(
                f"  ✓ {ticker}: {len(levels_placed)} levels @ "
                f"{', '.join(levels_placed)} "
                f"({CONTRACTS_PER_LEVEL}c each) | "
                f"\"{c['title']}\""
            )
        else:
            log.info(f"  ✗ {ticker}: no levels placed")

        time.sleep(SLEEP_BETWEEN_ORDERS)

    # ── Summary ──────────────────────────────────────────

    log.info("=" * 60)
    log.info(
        f"RESULTS: {success_count} placed, {fail_count} failed, "
        f"risk=${total_risk:.2f}"
    )
    log.info("=" * 60)

    # ── Upload to BigQuery ───────────────────────────────

    _upload_to_bq(order_results, market_snapshots, log)


def _upload_to_bq(
    order_results: List[Dict[str, Any]],
    market_snapshots: List[Dict[str, Any]],
    log,
):
    """Upload orders and market snapshots to BigQuery."""
    log.info("\n" + "=" * 60)
    log.info("UPLOADING TO BIGQUERY")
    log.info("=" * 60)

    if order_results:
        df_orders = pd.DataFrame(order_results)
        log.info(f"  Orders: {len(df_orders)} rows")
        try:
            df_to_bq(df_orders, BQ_ORDERS_TABLE, "WRITE_APPEND")
        except Exception as e:
            log.error(f"  ✗ Orders upload failed: {e}")
    else:
        log.info("  Orders: 0 rows — skipping")

    if market_snapshots:
        df_snapshots = pd.DataFrame(market_snapshots)
        log.info(f"  Snapshots: {len(df_snapshots)} rows")
        try:
            df_to_bq(df_snapshots, BQ_SNAPSHOTS_TABLE, "WRITE_APPEND")
        except Exception as e:
            log.error(f"  ✗ Snapshots upload failed: {e}")
    else:
        log.info("  Snapshots: 0 rows — skipping")

    log.info("✓ BigQuery upload complete")


# ────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────

def main():
    level = getattr(logging, LOG_LEVEL)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("longshot-bot")

    log.info("=" * 60)
    log.info("Kalshi Longshot Seller Bot")
    log.info(f"  Run ID: {RUN_ID}")
    log.info(f"  Model: {MODEL_VERSION}")
    log.info(f"  API: {API_BASE}")
    log.info(f"  BQ: {GCP_PROJECT_ID}.{GCP_DATASET_ID}")
    log.info(f"  Series: {len(CURATED_SERIES)}")
    for s in CURATED_SERIES:
        log.info(f"    {s['series_ticker']} ({s['description']})")
    log.info(f"  Price: ${MIN_YES_PRICE}-${MAX_YES_PRICE}")
    log.info(f"  Levels: {NUM_LEVELS} × {CONTRACTS_PER_LEVEL}c @ +{LEVEL_INCREMENT_CENTS}¢")
    log.info(f"  Bias discount: {BIAS_DISCOUNT}")
    log.info(f"  Pullback: {QUOTE_PULLBACK_HOURS}h before close_time")
    log.info(f"  Max risk: ${MAX_TOTAL_RISK}")
    log.info("=" * 60)

    scan_and_trade()


if __name__ == "__main__":
    main()
