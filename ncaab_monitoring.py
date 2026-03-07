#!/usr/bin/env python3
# =============================================================================
# KXNCAABMENTION MONITORING SCRIPT -- GitHub Actions Compatible
# =============================================================================
# Pulls fills + settlements from Kalshi API, computes P&L, uploads to BigQuery.
# Run daily to keep reporting tables fresh.
#
# ENVIRONMENT VARIABLES (set as GitHub Secrets):
#   KALSHI_API_KEY_ID          -- Kalshi API key ID
#   KALSHI_PRIVATE_KEY_PATH    -- Path to PEM file (written by workflow)
#   GCP_PROJECT_ID             -- BigQuery project ID
#   GCP_DATASET_ID             -- BigQuery dataset name
#   GOOGLE_APPLICATION_CREDENTIALS -- Path to GCP service account JSON
# =============================================================================

from __future__ import annotations
import os
import sys
import time
from datetime import datetime, UTC
from typing import Any, Dict, List

import pandas as pd
import numpy as np

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# =============================================================================
# CONFIG
# =============================================================================
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "/content/Lisa_Kalshi.txt")
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXNCAABMENTION"

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
DATASET_ID = os.environ.get("GCP_DATASET_ID", "Kalshi")

SLEEP_BETWEEN_CALLS_SEC = 0.05

# =============================================================================
# AUTHENTICATION
# =============================================================================
def load_private_key_from_file(file_path: str):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key

PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

exchange_client = ExchangeClient(
    exchange_api_base=API_BASE,
    key_id=API_KEY_ID,
    private_key=PRIVATE_KEY
)

# GCP auth
try:
    from google.cloud import bigquery
    import pyarrow  # noqa: F401

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            from google.colab import auth
            auth.authenticate_user()
            print("Authenticated via Colab")
        except ImportError:
            print("No GOOGLE_APPLICATION_CREDENTIALS and not in Colab")

    bq_client = bigquery.Client(project=PROJECT_ID)
    print(f"BigQuery client initialized (project: {PROJECT_ID})")

except ImportError as e:
    print(f"BigQuery libraries not available: {e}")
    bq_client = None

print("Testing Kalshi connection...")
try:
    status = exchange_client.get_exchange_status()
    print(f"Kalshi connected! Trading active: {status['trading_active']}")
except Exception as e:
    print(f"Kalshi connection failed: {e}")
    raise


# =============================================================================
# TICKER PARSING
# =============================================================================
def parse_event_code(event_code: str) -> Dict[str, str]:
    event_code = event_code or ""
    date_code = event_code[:7] if len(event_code) >= 7 else ""
    teams_code = event_code[7:] if len(event_code) > 7 else ""

    event_date_iso = ""
    if date_code:
        yy = date_code[:2]
        mmm = date_code[2:5]
        dd = date_code[5:7]
        dc = dd + mmm.title() + yy
        try:
            event_date_iso = datetime.strptime(dc, "%d%b%y").date().isoformat()
        except ValueError:
            event_date_iso = ""

    team_1 = teams_code[:3] if len(teams_code) >= 3 else ""
    team_2 = teams_code[3:] if len(teams_code) > 3 else ""

    return {
        "date_code": date_code,
        "event_date": event_date_iso,
        "teams_code": teams_code,
        "team_1": team_1,
        "team_2": team_2,
    }


def split_market_ticker(ticker: str) -> Dict[str, str]:
    parts = (ticker or "").split("-")
    series = parts[0] if len(parts) > 0 else ""
    event_code = parts[1] if len(parts) > 1 else ""
    market_code = parts[2] if len(parts) > 2 else ""
    parsed = parse_event_code(event_code)
    return {
        "ticker_part_1_series": series,
        "ticker_part_2_event_code": event_code,
        "ticker_part_3_market_code": market_code,
        **parsed,
    }


# =============================================================================
# LOAD ORDERS FROM BIGQUERY (deduplicated, for lineage)
# =============================================================================
def load_orders_from_bigquery(series_ticker: str) -> pd.DataFrame:
    if bq_client is None:
        print("No BigQuery client -- skipping order lineage")
        return pd.DataFrame()

    table_id = f"{PROJECT_ID}.{DATASET_ID}.{series_ticker}_orders"

    query = f"""
        SELECT * FROM (
            SELECT
                order_id,
                ticker,
                side,
                price,
                contracts,
                spread_config,
                pricing_strategy,
                time_multiplier,
                volume_multiplier,
                total_multiplier,
                hours_until_event,
                open_interest,
                base_contracts,
                created_at,
                ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY created_at DESC) AS rn
            FROM `{table_id}`
            WHERE order_id IS NOT NULL
        )
        WHERE rn = 1
    """

    try:
        df = bq_client.query(query).to_dataframe()
        print(f"Loaded {len(df)} deduplicated orders from BigQuery")
        return df
    except Exception as e:
        print(f"Could not load orders from BigQuery: {e}")
        return pd.DataFrame()


# =============================================================================
# PULL FILLS FROM KALSHI API
# =============================================================================
def get_all_fills(series_ticker: str) -> List[Dict[str, Any]]:
    fills = []
    cursor = None
    page = 0

    try:
        while True:
            page += 1
            params = {"limit": 500}
            if cursor:
                params["cursor"] = cursor

            if page % 10 == 1:
                print(f"  Fetching fills page {page}...")

            response = exchange_client.get_fills(**params)
            batch_fills = response.get('fills', [])

            for fill in batch_fills:
                ticker = fill.get('ticker', '')
                if ticker.startswith(series_ticker):
                    fills.append(fill)

            cursor = response.get('cursor')
            if not cursor:
                break

            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    except Exception as e:
        print(f"Error fetching fills: {e}")
        import traceback
        traceback.print_exc()

    print(f"Retrieved {len(fills)} fills for {series_ticker}")
    return fills


# =============================================================================
# PULL SETTLEMENTS FROM KALSHI API
# =============================================================================
def get_all_settlements(series_ticker: str) -> List[Dict[str, Any]]:
    settlements = []

    try:
        statuses_to_try = ['settled', 'closed', None]

        for status_filter in statuses_to_try:
            cursor = None
            page = 0
            status_name = status_filter or 'all'
            print(f"  Trying status='{status_name}'...")

            while True:
                page += 1
                params = {
                    "series_ticker": series_ticker,
                    "limit": 200,
                }
                if status_filter:
                    params["status"] = status_filter
                if cursor:
                    params["cursor"] = cursor

                if page % 10 == 1:
                    print(f"    Fetching page {page}...")

                response = exchange_client.get_markets(**params)
                markets = response.get('markets', [])

                found_this_page = 0
                for market in markets:
                    ticker = market.get('ticker')
                    mstatus = market.get('status')
                    result = market.get('result')

                    if result and result not in ('', 'none', None):
                        if not any(s['market_ticker'] == ticker for s in settlements):
                            settlements.append({
                                'market_ticker': ticker,
                                'event_ticker': market.get('event_ticker'),
                                'market_status': mstatus,
                                'result': result,
                                'settlement_value_yes': 100 if result == 'yes' else 0,
                                'settlement_value_no': 100 if result == 'no' else 0,
                                'close_time': market.get('close_time'),
                                'expiration_time': market.get('expiration_time'),
                            })
                            found_this_page += 1

                cursor = response.get('cursor')
                if not cursor:
                    break
                time.sleep(SLEEP_BETWEEN_CALLS_SEC)

            if len(settlements) > 0:
                break

    except Exception as e:
        print(f"Error fetching settlements: {e}")
        import traceback
        traceback.print_exc()

    print(f"Retrieved {len(settlements)} settlements for {series_ticker}")
    return settlements


# =============================================================================
# BUILD FILLS DATAFRAME WITH LINEAGE
# =============================================================================
def build_fills_dataframe(fills: List[Dict[str, Any]], df_orders: pd.DataFrame) -> pd.DataFrame:
    if len(fills) == 0:
        return pd.DataFrame()

    df_fills = pd.DataFrame(fills)
    df_fills['pulled_at'] = datetime.now(UTC).isoformat()

    if 'fill_id' not in df_fills.columns:
        print("fill_id not found in fills -- deduplication may be incomplete")

    if 'order_id' not in df_fills.columns:
        print("order_id not found in fills -- lineage will be incomplete")
        df_fills['order_id'] = None

    df_fills['fill_price'] = df_fills.apply(
        lambda row: row.get('yes_price') or row.get('no_price') or row.get('price', 0),
        axis=1
    )

    if 'ticker' in df_fills.columns and 'market_ticker' not in df_fills.columns:
        df_fills = df_fills.rename(columns={'ticker': 'market_ticker'})
    elif 'ticker' in df_fills.columns and 'market_ticker' in df_fills.columns:
        df_fills = df_fills.drop(columns=['ticker'])
    elif 'market_ticker' not in df_fills.columns:
        df_fills['market_ticker'] = None

    if 'action' not in df_fills.columns:
        df_fills['action'] = 'buy'

    if 'count' in df_fills.columns and 'filled_count' not in df_fills.columns:
        df_fills = df_fills.rename(columns={'count': 'filled_count'})
    elif 'filled_count' not in df_fills.columns:
        df_fills['filled_count'] = 0

    if len(df_orders) > 0:
        df_orders['order_id'] = df_orders['order_id'].astype(str)
        df_fills['order_id'] = df_fills['order_id'].astype(str)

        df_fills = df_fills.merge(
            df_orders[['order_id', 'spread_config', 'pricing_strategy',
                       'time_multiplier', 'volume_multiplier', 'total_multiplier',
                       'hours_until_event', 'open_interest', 'base_contracts']],
            on='order_id',
            how='left',
            suffixes=('', '_order')
        )
        matched = df_fills['spread_config'].notna().sum()
        total = len(df_fills)
        pct = matched / total * 100 if total > 0 else 0
        print(f"Matched {matched}/{total} fills to orders ({pct:.1f}%)")
    else:
        print("No orders available for lineage matching")

    for col in ['spread_config', 'pricing_strategy', 'time_multiplier',
                'volume_multiplier', 'total_multiplier', 'hours_until_event',
                'open_interest', 'base_contracts']:
        if col not in df_fills.columns:
            df_fills[col] = None

    return df_fills


# =============================================================================
# BUILD SETTLEMENTS DATAFRAME WITH P&L
# =============================================================================
def build_settlements_dataframe(settlements, df_fills):
    if len(settlements) == 0:
        return pd.DataFrame()

    df_settlements = pd.DataFrame(settlements)
    df_settlements['pulled_at'] = datetime.now(UTC).isoformat()

    print("Deduplicating fills...")
    fills_before = len(df_fills)

    if 'fill_id' in df_fills.columns:
        df_fills_deduped = df_fills.drop_duplicates(subset=['fill_id'], keep='first')
        print(f"  Using fill_id deduplication")
    else:
        df_fills['dedup_key'] = (
            df_fills['order_id'].astype(str) + '_' +
            df_fills['market_ticker'].astype(str) + '_' +
            df_fills['side'].astype(str) + '_' +
            df_fills['fill_price'].astype(str) + '_' +
            df_fills['filled_count'].astype(str)
        )
        df_fills_deduped = df_fills.drop_duplicates(subset=['dedup_key'], keep='first')
        print(f"  fill_id not found, using composite key")

    fills_after = len(df_fills_deduped)
    print(f"  {fills_before} -> {fills_after} (removed {fills_before - fills_after} duplicates)")

    positions = []

    for _, settlement in df_settlements.iterrows():
        ticker = settlement['market_ticker']
        result = settlement['result'].upper() if settlement['result'] else ''

        market_fills = df_fills_deduped[df_fills_deduped['market_ticker'] == ticker]

        if len(market_fills) == 0:
            positions.append({
                'market_ticker': ticker,
                'position_yes': 0, 'position_no': 0, 'net_position': 0,
                'total_cost': 0, 'total_payout': 0, 'pnl': 0, 'num_fills': 0,
            })
            continue

        yes_fills = market_fills[market_fills['side'] == 'yes']
        no_fills = market_fills[market_fills['side'] == 'no']

        position_yes = int(yes_fills['filled_count'].sum())
        position_no = int(no_fills['filled_count'].sum())

        pnl_cents = 0.0
        for _, fill in market_fills.iterrows():
            fp = float(fill['fill_price'])
            cnt = float(fill['filled_count'])
            side = fill['side']

            if side == 'yes' and result == 'YES':
                pnl_cents += cnt * (100 - fp)
            elif side == 'yes' and result == 'NO':
                pnl_cents -= cnt * fp
            elif side == 'no' and result == 'NO':
                pnl_cents += cnt * fp
            elif side == 'no' and result == 'YES':
                pnl_cents -= cnt * (100 - fp)

        pnl_dollars = pnl_cents / 100.0

        yes_cost = (yes_fills['fill_price'] * yes_fills['filled_count']).sum() / 100.0
        no_cost = 0.0
        if len(no_fills) > 0:
            no_cost = ((100 - no_fills['fill_price']) * no_fills['filled_count']).sum() / 100.0
        total_cost = yes_cost + no_cost

        if result == 'YES':
            total_payout = position_yes * 1.0
        elif result == 'NO':
            total_payout = position_no * 1.0
        else:
            total_payout = 0

        positions.append({
            'market_ticker': ticker,
            'position_yes': position_yes,
            'position_no': position_no,
            'net_position': position_yes - position_no,
            'total_cost': round(total_cost, 2),
            'total_payout': round(total_payout, 2),
            'pnl': round(pnl_dollars, 2),
            'num_fills': len(market_fills),
        })

    df_positions = pd.DataFrame(positions)
    df_settlements = df_settlements.merge(df_positions, on='market_ticker', how='left')
    df_settlements['result'] = df_settlements['result'].str.upper()

    print("Parsing market tickers...")
    ticker_parts = df_settlements['market_ticker'].apply(split_market_ticker)
    ticker_df = pd.DataFrame(ticker_parts.tolist())
    df_settlements = pd.concat([df_settlements, ticker_df], axis=1)

    return df_settlements


# =============================================================================
# BIGQUERY UPLOAD
# =============================================================================
def df_to_bq(df, table_name, write_disposition="WRITE_TRUNCATE"):
    if bq_client is None:
        print(f"Skipping {table_name} -- no BigQuery client")
        return
    if len(df) == 0:
        print(f"Skipping {table_name} -- no data")
        return

    df_clean = df.copy()
    df_clean = df_clean.loc[:, ~df_clean.columns.duplicated()]

    for col in df_clean.columns:
        if len(df_clean) > 0:
            first_valid = df_clean[col].dropna().iloc[0] if len(df_clean[col].dropna()) > 0 else None
            if first_valid is not None and isinstance(first_valid, (list, dict)):
                df_clean[col] = df_clean[col].apply(lambda x: str(x) if x is not None else None)

    for col in df_clean.columns:
        if pd.api.types.is_object_dtype(df_clean[col]):
            df_clean[col] = df_clean[col].astype(str).replace('nan', None).replace('None', None)

    df_clean = df_clean.where(pd.notna(df_clean), None)

    table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=write_disposition,
        autodetect=True,
    )

    try:
        job = bq_client.load_table_from_dataframe(df_clean, table_id, job_config=job_config)
        job.result()
        table = bq_client.get_table(table_id)
        print(f"Loaded {table_id}: {table.num_rows} rows")
    except Exception as e:
        print(f"Error loading {table_id}: {e}")
        import traceback
        traceback.print_exc()


# =============================================================================
# MAIN MONITORING FUNCTION
# =============================================================================
def run_monitoring(series_ticker, upload_to_bq=True):
    print(f"\n{'='*70}")
    print(f"MONITORING: {series_ticker}")
    print(f"Time: {datetime.now(UTC).isoformat()}")
    print(f"{'='*70}\n")

    print("Step 1: Loading orders from BigQuery...")
    df_orders = load_orders_from_bigquery(series_ticker)

    print("\nStep 2: Pulling fills from Kalshi API...")
    fills = get_all_fills(series_ticker)

    print("\nStep 3: Pulling settlements from Kalshi API...")
    settlements = get_all_settlements(series_ticker)

    print("\nStep 4: Building df_fills with lineage...")
    df_fills = build_fills_dataframe(fills, df_orders)

    print("\nStep 5: Building df_settlements with P&L...")
    df_settlements = build_settlements_dataframe(settlements, df_fills)

    if upload_to_bq:
        print("\nStep 6: Uploading to BigQuery...")
        df_to_bq(df_fills, f"{series_ticker}_fills", write_disposition="WRITE_TRUNCATE")
        df_to_bq(df_settlements, f"{series_ticker}_settlements", write_disposition="WRITE_TRUNCATE")

    print(f"\n{'='*70}")
    print(f"MONITORING COMPLETE")
    print(f"{'='*70}")
    print(f"df_fills: {len(df_fills)} rows")
    print(f"df_settlements: {len(df_settlements)} rows")

    if len(df_fills) > 0:
        print(f"\nFills summary:")
        print(f"  Total filled contracts: {df_fills['filled_count'].sum()}")
        print(f"  Unique markets: {df_fills['market_ticker'].nunique()}")
        if 'spread_config' in df_fills.columns:
            matched = df_fills['spread_config'].notna().sum()
            total = len(df_fills)
            pct = matched / total * 100 if total > 0 else 0
            print(f"  Fills with lineage: {matched} ({pct:.1f}%)")

    if len(df_settlements) > 0:
        total_pnl = df_settlements['pnl'].sum()
        winners = (df_settlements['pnl'] > 0).sum()
        losers = (df_settlements['pnl'] < 0).sum()
        breakeven = (df_settlements['pnl'] == 0).sum()
        print(f"\nSettlements summary:")
        print(f"  Total P&L: ${total_pnl:,.2f}")
        print(f"  Winners: {winners} | Losers: {losers} | Break-even: {breakeven}")

    print(f"\n{'='*70}\n")
    return df_fills, df_settlements


# =============================================================================
# RUN
# =============================================================================
df_fills, df_settlements = run_monitoring(SERIES_TICKER, upload_to_bq=True)
print("Monitoring run complete")
