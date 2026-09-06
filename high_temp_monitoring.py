#!/usr/bin/env python3
"""
Kalshi High Temperature Monitoring — FULL REFRESH
==================================================
Pulls ALL fills + settlements from Kalshi API, computes P&L per settled
market, joins fills→orders for strategy lineage, uploads to BigQuery
with WRITE_TRUNCATE (full refresh every run — nothing slips through).

Tables written:
  KXHIGH_fills         — every execution, with order metadata (TRUNCATE)
  KXHIGH_settlements   — every settled market with P&L (TRUNCATE)

LINEAGE (BigQuery join example):
  SELECT o.city, o.market_ticker, o.no_price as order_price, o.contracts,
         f.fill_price, f.filled_count, f.side,
         f.spread_config, f.pricing_strategy,
         s.result, s.pnl, s.total_cost, s.total_payout,
         s.winning_temp, s.event_date, s.city_name
  FROM `project.Kalshi.KXHIGH_orders` o
  LEFT JOIN `project.Kalshi.KXHIGH_fills` f ON o.client_order_id = f.order_id
  LEFT JOIN `project.Kalshi.KXHIGH_settlements` s ON f.market_ticker = s.market_ticker

Run daily via GitHub Actions or manually in Colab.

GitHub Actions secrets (same as trading script):
  KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
  GCP_PROJECT_ID, GCP_DATASET_ID, GOOGLE_APPLICATION_CREDENTIALS
"""

from __future__ import annotations
import os
import sys
import time
import base64
from datetime import datetime, UTC
from typing import Any, Dict, List

IS_COLAB = "google.colab" in sys.modules or "COLAB_RELEASE_TAG" in os.environ
if IS_COLAB:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "google-cloud-bigquery", "db-dtypes", "pyarrow"])

import pandas as pd
import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# =====================================================================
# CONFIG
# =====================================================================
API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
PRIVATE_KEY_B64  = os.environ.get("KALSHI_PRIVATE_KEY", "")
API_BASE         = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER    = "KXHIGH"  # Prefix for all high temp markets

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
DATASET_ID = os.environ.get("GCP_DATASET_ID", "Kalshi")
BQ_TABLE_PREFIX = "KXHIGH_"

SLEEP_BETWEEN_CALLS_SEC = 0.05

# =====================================================================
# AUTHENTICATION
# =====================================================================
def load_private_key(b64_key="", file_path=""):
    """Load Kalshi RSA key from file or env var."""
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f"No private key. Set KALSHI_PRIVATE_KEY or place at '{file_path}'.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

PRIVATE_KEY = load_private_key(b64_key=PRIVATE_KEY_B64, file_path=PRIVATE_KEY_PATH)

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


# =====================================================================
# TICKER PARSING — adapted for KXHIGH format
# Ticker format: KXHIGHCHI-26MAR10-B31
#   Part 1: KXHIGHCHI      (series + city)
#   Part 2: 26MAR10        (date code: YY + MON + DD)
#   Part 3: B31 or T29.5   (market type: Between or Tail + temp)
# =====================================================================
# City abbreviation mapping for display
CITY_ABV_TO_NAME = {
    "CHI": "Chicago", "NY": "New York City", "DEN": "Denver",
    "PHIL": "Philadelphia", "AUS": "Austin", "MIA": "Miami",
    "LAX": "Los Angeles", "TATL": "Atlanta", "TDC": "Washington DC",
    "TPHX": "Phoenix", "TDAL": "Dallas", "TLV": "Las Vegas",
    "TOKC": "Oklahoma City", "TSEA": "Seattle", "TSFO": "San Francisco",
    "THOU": "Houston", "TSATX": "San Antonio", "TMIN": "Minneapolis",
    "TNOLA": "New Orleans",
}

def parse_kxhigh_ticker(ticker: str) -> Dict[str, str]:
    """Parse a KXHIGH market ticker into components.
    e.g. 'KXHIGHCHI-26MAR10-B31' → {series, city_code, date_code, event_date, market_type, temp_value, ...}
    """
    parts = (ticker or "").split("-")
    series_city = parts[0] if len(parts) > 0 else ""
    date_code = parts[1] if len(parts) > 1 else ""
    market_code = parts[2] if len(parts) > 2 else ""

    # Extract city code from series prefix
    city_code = ""
    if series_city.startswith("KXHIGH"):
        city_code = series_city[6:]  # Everything after "KXHIGH"

    # Parse date: "26MAR10" → 2026-03-10
    event_date = ""
    if len(date_code) >= 7:
        yy = date_code[:2]
        mmm = date_code[2:5]
        dd = date_code[5:7]
        try:
            event_date = datetime.strptime(f"{dd}{mmm}{yy}", "%d%b%y").date().isoformat()
        except ValueError:
            event_date = ""

    # Parse market type: B31 (between 30-32°F) or T29.5 (tail at 29.5°F)
    market_type = ""
    temp_value = ""
    if market_code.startswith("B"):
        market_type = "between"
        temp_value = market_code[1:]
    elif market_code.startswith("T"):
        market_type = "tail"
        temp_value = market_code[1:]

    # City display name
    city_name = CITY_ABV_TO_NAME.get(city_code, city_code)

    # Event ticker (series+city + date)
    event_ticker = f"{series_city}-{date_code}" if date_code else series_city

    return {
        "series_city": series_city,
        "city_code": city_code,
        "city_name": city_name,
        "date_code": date_code,
        "event_date": event_date,
        "event_ticker": event_ticker,
        "market_code": market_code,
        "market_type": market_type,
        "temp_value": temp_value,
    }


# =====================================================================
# LOAD ORDERS FROM BIGQUERY (deduplicated, for lineage)
# =====================================================================
def load_orders_from_bigquery() -> pd.DataFrame:
    """Load deduplicated orders from KXHIGH_orders for fill→order lineage."""
    if bq_client is None:
        print("No BigQuery client — skipping order lineage")
        return pd.DataFrame()

    table_id = f"{PROJECT_ID}.{DATASET_ID}.{BQ_TABLE_PREFIX}orders"

    # Deduplicate by client_order_id (WRITE_APPEND creates dupes across runs)
    query = f"""
        SELECT * FROM (
            SELECT
                client_order_id,
                kalshi_order_id,
                market_ticker,
                city,
                city_abv,
                no_price,
                contracts,
                forecast_date,
                run_date,
                expiration_ts,
                created_at,
                ROW_NUMBER() OVER (PARTITION BY client_order_id ORDER BY created_at DESC) AS rn
            FROM `{table_id}`
            WHERE client_order_id IS NOT NULL
        )
        WHERE rn = 1
    """

    try:
        df = bq_client.query(query).to_dataframe()
        print(f"Loaded {len(df)} deduplicated orders from BigQuery")
        return df
    except Exception as e:
        print(f"Could not load orders with kalshi_order_id (column may not exist yet): {e}")
        # Fallback: load without kalshi_order_id column — lineage won't work for old rows
        fallback_query = f"""
            SELECT * FROM (
                SELECT
                    client_order_id, market_ticker, city, city_abv,
                    no_price, contracts, forecast_date, run_date,
                    expiration_ts, created_at,
                    ROW_NUMBER() OVER (PARTITION BY client_order_id ORDER BY created_at DESC) AS rn
                FROM `{table_id}`
                WHERE client_order_id IS NOT NULL
            )
            WHERE rn = 1
        """
        try:
            df = bq_client.query(fallback_query).to_dataframe()
            df['kalshi_order_id'] = None
            print(f"Loaded {len(df)} orders (fallback, no kalshi_order_id)")
            return df
        except Exception as e2:
            print(f"Fallback also failed: {e2}")
            return pd.DataFrame()


# =====================================================================
# PULL ALL FILLS FROM KALSHI API
# Paginated, filters to KXHIGH series. Full pull every run.
# =====================================================================
def get_all_fills() -> List[Dict[str, Any]]:
    """Pull ALL fills from Kalshi API, filtered to KXHIGH markets.

    Raises on any mid-pagination failure instead of returning a partial
    list: the caller WRITE_TRUNCATEs KXHIGH_fills, so partial data would
    silently shrink the table. Failing keeps the old table intact.
    """
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

            # Filter to KXHIGH markets only
            for fill in batch_fills:
                ticker = fill.get('ticker', '')
                if ticker.startswith(SERIES_TICKER):
                    fills.append(fill)

            cursor = response.get('cursor')
            if not cursor:
                break
            if page >= 500:
                raise RuntimeError(
                    f"fills pagination still had a cursor after {page} pages "
                    f"({len(fills)} {SERIES_TICKER} fills so far)"
                )

            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    except Exception as e:
        print(f"Error fetching fills: {e}")
        import traceback
        traceback.print_exc()
        raise  # partial fills must not reach the WRITE_TRUNCATE below

    print(f"Retrieved {len(fills)} fills for {SERIES_TICKER}")
    return fills


# =====================================================================
# PULL ALL SETTLEMENTS FROM KALSHI API
# Gets settled/closed markets, extracts result + settlement info.
# =====================================================================
def get_all_settlements() -> List[Dict[str, Any]]:
    """Pull ALL settled KXHIGH markets from Kalshi portfolio settlements API.
    Uses get_portfolio_settlements() and filters by KXHIGH prefix.
    This returns markets where you had a position that settled.
    """
    settlements = []
    cursor = None
    page = 0
    seen_tickers = set()

    try:
        while True:
            page += 1
            params = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor

            if page % 10 == 1:
                print(f"  Fetching portfolio settlements page {page}...")

            response = exchange_client.get_portfolio_settlements(**params)
            batch = response.get('settlements', [])

            for s in batch:
                ticker = s.get('ticker', '')

                # Filter to KXHIGH markets only
                if not ticker.startswith(SERIES_TICKER):
                    continue

                # NEW API: *_total_cost_dollars are string dollars
                yes_cost = float(s.get('yes_total_cost_dollars', 0) or 0)
                no_cost = float(s.get('no_total_cost_dollars', 0) or 0)
                if yes_cost <= 0 and no_cost <= 0:
                    continue

                # Deduplicate by ticker
                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)

                # NEW API: market_result (stayed), *_count_fp are string fractional contracts
                settlements.append({
                    'market_ticker': ticker,
                    'result': s.get('market_result', ''),
                    'revenue': s.get('revenue', 0),  # cents, int
                    'value': s.get('value', 0),  # cents, int (residual position value)
                    'yes_total_cost': yes_cost,  # dollars, float
                    'no_total_cost': no_cost,  # dollars, float
                    'settled_time': str(s.get('settled_time', '')) if s.get('settled_time') is not None else None,
                    'yes_count': float(s.get('yes_count_fp', 0) or 0),
                    'no_count': float(s.get('no_count_fp', 0) or 0),
                    'fee_cost': float(s.get('fee_cost', 0) or 0),
                })

            # An empty batch can arrive mid-stream; only a missing cursor ends the data.
            cursor = response.get('cursor')
            if not cursor:
                break
            if page >= 500:
                print(f"  ⚠️ settlements pagination hit {page}-page cap; older settlements not fetched")
                break

            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    except Exception as e:
        print(f"Error fetching settlements: {e}")
        import traceback
        traceback.print_exc()

    print(f"Retrieved {len(settlements)} settlements for {SERIES_TICKER}")
    return settlements


# =====================================================================
# BUILD FILLS DATAFRAME WITH ORDER LINEAGE
# Joins fills → orders via order_id ↔ client_order_id
# =====================================================================
def build_fills_dataframe(fills: List[Dict[str, Any]], df_orders: pd.DataFrame) -> pd.DataFrame:
    """Build fills DataFrame with order metadata for strategy analysis."""
    if len(fills) == 0:
        return pd.DataFrame()

    df_fills = pd.DataFrame(fills)
    df_fills['pulled_at'] = datetime.now(UTC).isoformat()

    # --- Normalize column names ---
    # Kalshi API uses 'ticker', we want 'market_ticker'
    if 'ticker' in df_fills.columns and 'market_ticker' not in df_fills.columns:
        df_fills = df_fills.rename(columns={'ticker': 'market_ticker'})
    elif 'ticker' in df_fills.columns and 'market_ticker' in df_fills.columns:
        df_fills = df_fills.drop(columns=['ticker'])
    elif 'market_ticker' not in df_fills.columns:
        df_fills['market_ticker'] = None

    # NEW API: count_fp is fractional contracts as string (e.g. '25.00' or '0.37')
    if 'count_fp' in df_fills.columns:
        df_fills['filled_count'] = pd.to_numeric(df_fills['count_fp'], errors='coerce').fillna(0)
    elif 'count' in df_fills.columns:
        df_fills['filled_count'] = pd.to_numeric(df_fills['count'], errors='coerce').fillna(0)
    elif 'filled_count' not in df_fills.columns:
        df_fills['filled_count'] = 0

    # NEW API: prices are yes_price_dollars / no_price_dollars as strings in dollars
    #
    # ⚠ DATA-ORIENTATION NOTE (discovered Apr 2026 via settlement reconciliation):
    # For side=no fills, Kalshi's `yes_price_dollars` field contains the actual
    # NO-side execution price (what the bot paid per contract). The `no_price`
    # field contains the complementary YES market price. Using no_price as "cost
    # of NO" leaves a ~$66 gap across the dataset; using yes_price reconciles
    # exactly with settlements.total_cost. See analysis/kxhigh/sql/06_fills_clean.sql
    # for the authoritative view that swaps them. Not fixing here to avoid mixing
    # old and new rows in the raw table; downstream queries should use the _clean
    # views or swap the columns explicitly.
    # Convert to cents int for compatibility with existing P&L formulas
    if 'yes_price_dollars' in df_fills.columns:
        df_fills['yes_price'] = (
            pd.to_numeric(df_fills['yes_price_dollars'], errors='coerce') * 100
        ).round().astype('Int64')
    if 'no_price_dollars' in df_fills.columns:
        df_fills['no_price'] = (
            pd.to_numeric(df_fills['no_price_dollars'], errors='coerce') * 100
        ).round().astype('Int64')

    if 'action' not in df_fills.columns:
        df_fills['action'] = 'buy'

    if 'order_id' not in df_fills.columns:
        print("order_id not found in fills — lineage will be incomplete")
        df_fills['order_id'] = None

    # --- fill_price = the price paid for THAT SIDE ---
    # YES fill: fill_price = yes_price. NO fill: fill_price = no_price.
    def _calc_fill_price(row):
        side = row.get('side', '')
        if side == 'yes':
            return row.get('yes_price') if pd.notna(row.get('yes_price')) else 0
        elif side == 'no':
            return row.get('no_price') if pd.notna(row.get('no_price')) else 0
        return row.get('price', 0)
    df_fills['fill_price'] = df_fills.apply(_calc_fill_price, axis=1)
    df_fills['fill_price'] = pd.to_numeric(df_fills['fill_price'], errors='coerce').fillna(0)

    # --- Join with deduplicated orders for strategy metadata ---
    # fills.order_id (Kalshi exchange ID) ↔ orders.kalshi_order_id
    if len(df_orders) > 0 and 'kalshi_order_id' in df_orders.columns:
        df_orders_for_join = df_orders[df_orders['kalshi_order_id'].notna()
                                         & (df_orders['kalshi_order_id'].astype(str) != '')].copy()
        df_orders_for_join['kalshi_order_id'] = df_orders_for_join['kalshi_order_id'].astype(str)
        df_fills['order_id'] = df_fills['order_id'].astype(str)

        df_fills = df_fills.merge(
            df_orders_for_join[[
                'kalshi_order_id', 'client_order_id', 'city', 'city_abv',
                'no_price', 'contracts', 'forecast_date',
            ]].rename(columns={
                'kalshi_order_id': 'order_id',
                'no_price': 'order_no_price',
                'contracts': 'order_contracts',
            }),
            on='order_id',
            how='left',
            suffixes=('', '_order')
        )
        matched = df_fills['city'].notna().sum()
        total = len(df_fills)
        pct = matched / total * 100 if total > 0 else 0
        print(f"Matched {matched}/{total} fills to orders ({pct:.1f}%)")
        if matched == 0 and total > 0:
            print("  NOTE: 0 matches is expected if no orders have kalshi_order_id yet.")
            print("  The trading script must write kalshi_order_id before lineage will work.")
    else:
        print("No orders available for lineage matching (or kalshi_order_id column missing)")
        for col in ['city', 'city_abv', 'order_no_price', 'order_contracts', 'forecast_date', 'client_order_id']:
            if col not in df_fills.columns:
                df_fills[col] = None

    # --- Parse ticker for additional metadata ---
    ticker_parts = df_fills['market_ticker'].apply(parse_kxhigh_ticker)
    ticker_df = pd.DataFrame(ticker_parts.tolist())
    df_fills = pd.concat([df_fills, ticker_df], axis=1)

    return df_fills


# =====================================================================
# BUILD SETTLEMENTS DATAFRAME WITH P&L
# Joins settlements → fills → computes per-market P&L
# =====================================================================
def build_settlements_dataframe(settlements, df_fills):
    """Build settlements DataFrame with fill-based P&L per settled market."""
    if len(settlements) == 0:
        return pd.DataFrame()

    df_settlements = pd.DataFrame(settlements)
    df_settlements['pulled_at'] = datetime.now(UTC).isoformat()

    # --- Deduplicate fills ---
    print("Deduplicating fills...")
    fills_before = len(df_fills)

    if 'fill_id' in df_fills.columns:
        df_fills_deduped = df_fills.drop_duplicates(subset=['fill_id'], keep='first')
        print(f"  Using fill_id deduplication")
    elif 'trade_id' in df_fills.columns:
        df_fills_deduped = df_fills.drop_duplicates(subset=['trade_id'], keep='first')
        print(f"  Using trade_id deduplication")
    else:
        # Composite key fallback
        df_fills['dedup_key'] = (
            df_fills['order_id'].astype(str) + '_' +
            df_fills['market_ticker'].astype(str) + '_' +
            df_fills['side'].astype(str) + '_' +
            df_fills['fill_price'].astype(str) + '_' +
            df_fills['filled_count'].astype(str)
        )
        df_fills_deduped = df_fills.drop_duplicates(subset=['dedup_key'], keep='first')
        print(f"  Using composite key deduplication")

    fills_after = len(df_fills_deduped)
    print(f"  {fills_before} -> {fills_after} (removed {fills_before - fills_after} duplicates)")

    # --- P&L per settled market ---
    # fill_price = raw yes_price from Kalshi API for ALL fills.
    #
    # P&L formulas (cents per contract):
    #   YES fill + YES wins:  +(100 - fill_price)   profit = payout - cost
    #   YES fill + NO wins:   -(fill_price)          loss = cost
    #   NO fill  + NO wins:   +(fill_price)           profit: we paid (100-fill_price), get 100
    #   NO fill  + YES wins:  -(100 - fill_price)    loss = what we paid for NO

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

        # P&L in cents
        pnl_cents = 0.0
        for _, fill in market_fills.iterrows():
            fp = float(fill['fill_price'])
            cnt = float(fill['filled_count'])
            side = fill['side']

            if side == 'yes' and result == 'YES':
                pnl_cents += cnt * (100 - fp)       # Bought YES, YES wins
            elif side == 'yes' and result == 'NO':
                pnl_cents -= cnt * fp                # Bought YES, NO wins
            elif side == 'no' and result == 'NO':
                pnl_cents += cnt * fp                # Bought NO, NO wins
            elif side == 'no' and result == 'YES':
                pnl_cents -= cnt * (100 - fp)        # Bought NO, YES wins

        pnl_dollars = pnl_cents / 100.0

        # Cost (what we actually paid)
        yes_cost = (yes_fills['fill_price'] * yes_fills['filled_count']).sum() / 100.0
        no_cost = 0.0
        if len(no_fills) > 0:
            no_cost = ((100 - no_fills['fill_price']) * no_fills['filled_count']).sum() / 100.0
        total_cost = yes_cost + no_cost

        # Payout
        if result == 'YES':
            total_payout = position_yes * 1.0   # $1 per YES contract
        elif result == 'NO':
            total_payout = position_no * 1.0    # $1 per NO contract
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

    # --- Parse tickers for city/date/market type ---
    print("Parsing market tickers...")
    ticker_parts = df_settlements['market_ticker'].apply(parse_kxhigh_ticker)
    ticker_df = pd.DataFrame(ticker_parts.tolist())
    df_settlements = pd.concat([df_settlements, ticker_df], axis=1)

    return df_settlements


# =====================================================================
# BIGQUERY UPLOAD — WRITE_TRUNCATE (full refresh every run)
# =====================================================================
def df_to_bq(df, table_name, write_disposition="WRITE_TRUNCATE"):
    """Upload DataFrame to BigQuery. Default WRITE_TRUNCATE = full replace."""
    if bq_client is None:
        print(f"Skipping {table_name} — no BigQuery client")
        return
    if len(df) == 0:
        print(f"Skipping {table_name} — no data")
        return

    df_clean = df.copy()
    # Remove duplicate columns
    df_clean = df_clean.loc[:, ~df_clean.columns.duplicated()]

    # Convert complex types (lists, dicts) to strings
    for col in df_clean.columns:
        if len(df_clean) > 0:
            first_valid = df_clean[col].dropna().iloc[0] if len(df_clean[col].dropna()) > 0 else None
            if first_valid is not None and isinstance(first_valid, (list, dict)):
                df_clean[col] = df_clean[col].apply(lambda x: str(x) if x is not None else None)

    # Ensure object columns are clean strings
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
        print(f"✓ Loaded {table_id}: {table.num_rows} rows")
    except Exception as e:
        print(f"Error loading {table_id}: {e}")
        import traceback
        traceback.print_exc()


# =====================================================================
# MAIN MONITORING FUNCTION
# =====================================================================
def run_monitoring(upload_to_bq=True):
    print(f"\n{'='*70}")
    print(f"MONITORING: {SERIES_TICKER} (High Temperature Markets)")
    print(f"Time: {datetime.now(UTC).isoformat()}")
    print(f"Mode: FULL REFRESH (WRITE_TRUNCATE)")
    print(f"{'='*70}\n")

    # Step 1: Load deduplicated orders from BQ for lineage
    print("Step 1: Loading orders from BigQuery (for fill→order lineage)...")
    df_orders = load_orders_from_bigquery()

    # Step 2: Pull ALL fills from Kalshi API
    print("\nStep 2: Pulling ALL fills from Kalshi API...")
    fills = get_all_fills()

    # Step 3: Pull ALL settlements from Kalshi API
    print("\nStep 3: Pulling ALL settlements from Kalshi API...")
    settlements = get_all_settlements()

    # Step 4: Build df_fills with order lineage
    print("\nStep 4: Building fills DataFrame with order lineage...")
    df_fills = build_fills_dataframe(fills, df_orders)

    # Step 5: Build df_settlements with P&L
    print("\nStep 5: Building settlements DataFrame with P&L...")
    df_settlements = build_settlements_dataframe(settlements, df_fills)

    # Step 6: Upload to BigQuery
    # Fills: WRITE_TRUNCATE (full history pulled every run, safe to replace)
    # Settlements: MERGE (preserves rows outside Kalshi's rolling ~5-day API window)
    if upload_to_bq:
        print("\nStep 6: Uploading to BigQuery...")
        df_to_bq(df_fills, f"{BQ_TABLE_PREFIX}fills", write_disposition="WRITE_TRUNCATE")

        # Settlements: upload to staging, then MERGE into main table
        if len(df_settlements) > 0 and bq_client is not None:
            staging_table = f"{BQ_TABLE_PREFIX}settlements_staging"
            main_table = f"{BQ_TABLE_PREFIX}settlements"
            main_table_id = f"{PROJECT_ID}.{DATASET_ID}.{main_table}"

            df_to_bq(df_settlements, staging_table, write_disposition="WRITE_TRUNCATE")

            # If main table doesn't exist, create it from staging and we're done
            try:
                bq_client.get_table(main_table_id)
                table_exists = True
            except Exception:
                table_exists = False

            if not table_exists:
                print(f"  {main_table} does not exist — creating from staging")
                create_sql = f"""
                CREATE TABLE `{main_table_id}` AS
                SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.{staging_table}`
                """
                bq_client.query(create_sql).result()
                print(f"  ✓ Created {main_table_id}")
            else:
                # MERGE: update existing rows, insert new ones, keep old ones untouched
                # Wrap source in subquery to cast settled_time to STRING
                # (existing main table column is STRING; new API may return numeric ts)
                merge_sql = f"""
                MERGE `{main_table_id}` T
                USING (
                    SELECT
                        * EXCEPT(settled_time),
                        CAST(settled_time AS STRING) AS settled_time
                    FROM `{PROJECT_ID}.{DATASET_ID}.{staging_table}`
                ) S
                ON T.market_ticker = S.market_ticker
                WHEN MATCHED THEN UPDATE SET
                    result = S.result,
                    revenue = S.revenue,
                    value = S.value,
                    yes_total_cost = S.yes_total_cost,
                    no_total_cost = S.no_total_cost,
                    yes_count = S.yes_count,
                    no_count = S.no_count,
                    fee_cost = S.fee_cost,
                    settled_time = S.settled_time,
                    pulled_at = S.pulled_at,
                    position_yes = S.position_yes,
                    position_no = S.position_no,
                    net_position = S.net_position,
                    total_cost = S.total_cost,
                    total_payout = S.total_payout,
                    pnl = S.pnl,
                    num_fills = S.num_fills,
                    -- Ticker-derived dimensions. Staging parses these
                    -- correctly, but this MERGE listed only 18 of the table's
                    -- 27 columns, so all 9 landed empty on every promotion --
                    -- 924 of 2,369 rows (39%), and 100% of every row settled
                    -- since 2026-07-08. It stayed invisible because
                    -- KXHIGH_settlements_clean and _resolved_markets re-parse
                    -- them from market_ticker rather than trusting the base
                    -- table. Included in UPDATE as well as INSERT so an
                    -- existing hollow row heals if it is ever re-merged.
                    series_city = S.series_city,
                    city_code = S.city_code,
                    city_name = S.city_name,
                    date_code = S.date_code,
                    event_date = S.event_date,
                    event_ticker = S.event_ticker,
                    market_code = S.market_code,
                    market_type = S.market_type,
                    temp_value = S.temp_value
                WHEN NOT MATCHED THEN INSERT (
                    market_ticker, result, revenue, value,
                    yes_total_cost, no_total_cost, yes_count, no_count,
                    fee_cost, settled_time, pulled_at,
                    position_yes, position_no, net_position,
                    total_cost, total_payout, pnl, num_fills,
                    series_city, city_code, city_name, date_code, event_date,
                    event_ticker, market_code, market_type, temp_value
                ) VALUES (
                    S.market_ticker, S.result, S.revenue, S.value,
                    S.yes_total_cost, S.no_total_cost, S.yes_count, S.no_count,
                    S.fee_cost, S.settled_time, S.pulled_at,
                    S.position_yes, S.position_no, S.net_position,
                    S.total_cost, S.total_payout, S.pnl, S.num_fills,
                    S.series_city, S.city_code, S.city_name, S.date_code,
                    S.event_date, S.event_ticker, S.market_code,
                    S.market_type, S.temp_value
                )
                """
                try:
                    job = bq_client.query(merge_sql)
                    job.result()
                    print(f"  ✓ Merged {len(df_settlements)} settlements into {main_table_id}")
                    print(f"    (DML stats: {job.num_dml_affected_rows} rows affected)")
                except Exception as e:
                    print(f"  ⚠ MERGE failed: {e}")
                    import traceback
                    traceback.print_exc()
        else:
            print("  Skipping settlements upload — no data")

    # Step 7: Fetch real NWS CLI high temperatures for the current year.
    # Safe to run every time — MERGE upsert in fetch_cli.py is idempotent.
    # Keeps KXHIGH_cli_readings current so settlements_clean.winning_high_temp
    # stays populated for every settled market.
    if upload_to_bq and bq_client is not None:
        print("\nStep 7: Fetching NWS CLI high temperatures (Iowa State IEM)...")
        try:
            _repo_root = os.path.dirname(os.path.abspath(__file__))
            _cli_path = os.path.join(_repo_root, "analysis", "kxhigh", "python")
            if _cli_path not in sys.path:
                sys.path.insert(0, _cli_path)
            import fetch_cli as _cli  # noqa: WPS433
            _cli_year = datetime.now(UTC).year
            _cli.ensure_table(bq_client)
            _cli_df = _cli.backfill_iem(_cli_year)
            if not _cli_df.empty:
                _cli.upsert(bq_client, _cli_df)
                print(f"  ✓ Upserted {len(_cli_df)} CLI rows for {_cli_year}")
            else:
                print("  (no CLI rows returned — check IEM API availability)")
        except Exception as _cli_e:
            # Non-fatal: monitoring should succeed even if CLI fetch fails
            print(f"  ⚠ CLI fetch failed (non-fatal): {_cli_e}")
            import traceback
            traceback.print_exc()

    # ===== SUMMARY =====
    print(f"\n{'='*70}")
    print(f"MONITORING COMPLETE")
    print(f"{'='*70}")
    print(f"  {BQ_TABLE_PREFIX}fills: {len(df_fills)} rows")
    print(f"  {BQ_TABLE_PREFIX}settlements: {len(df_settlements)} rows")

    if len(df_fills) > 0:
        print(f"\n  FILLS SUMMARY:")
        print(f"    Total filled contracts: {df_fills['filled_count'].sum()}")
        print(f"    Unique markets: {df_fills['market_ticker'].nunique()}")
        if 'city' in df_fills.columns:
            matched = df_fills['city'].notna().sum()
            total = len(df_fills)
            pct = matched / total * 100 if total > 0 else 0
            print(f"    Fills with order lineage: {matched} ({pct:.1f}%)")

        # Fills by side
        side_summary = df_fills.groupby('side')['filled_count'].sum()
        for side, cnt in side_summary.items():
            print(f"    {side.upper()} fills: {int(cnt)} contracts")

    if len(df_settlements) > 0:
        total_pnl = df_settlements['pnl'].sum()
        total_cost = df_settlements['total_cost'].sum()
        total_payout = df_settlements['total_payout'].sum()
        winners = (df_settlements['pnl'] > 0).sum()
        losers = (df_settlements['pnl'] < 0).sum()
        breakeven = (df_settlements['pnl'] == 0).sum()
        with_fills = (df_settlements['num_fills'] > 0).sum()

        print(f"\n  SETTLEMENTS SUMMARY:")
        print(f"    Markets settled: {len(df_settlements)}")
        print(f"    Markets with fills: {with_fills}")
        print(f"    Total cost: ${total_cost:,.2f}")
        print(f"    Total payout: ${total_payout:,.2f}")
        print(f"    Total P&L: ${total_pnl:,.2f}")
        print(f"    Winners: {winners} | Losers: {losers} | Break-even: {breakeven}")

        if total_cost > 0:
            roi = total_pnl / total_cost * 100
            print(f"    ROI: {roi:.1f}%")

        # P&L by city
        if 'city_name' in df_settlements.columns:
            city_pnl = (df_settlements[df_settlements['num_fills'] > 0]
                        .groupby('city_name')['pnl'].sum()
                        .sort_values(ascending=False))
            if len(city_pnl) > 0:
                print(f"\n  P&L BY CITY (top 10):")
                for city, pnl in city_pnl.head(10).items():
                    emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
                    print(f"    {emoji} {city}: ${pnl:,.2f}")

        # P&L by date
        if 'event_date' in df_settlements.columns:
            date_pnl = (df_settlements[df_settlements['num_fills'] > 0]
                        .groupby('event_date')['pnl'].sum()
                        .sort_index(ascending=False))
            if len(date_pnl) > 0:
                print(f"\n  P&L BY DATE (last 10):")
                for date, pnl in date_pnl.head(10).items():
                    emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
                    print(f"    {emoji} {date}: ${pnl:,.2f}")

    print(f"\n{'='*70}\n")
    return df_fills, df_settlements


# =====================================================================
# RUN
# =====================================================================
df_fills, df_settlements = run_monitoring(upload_to_bq=True)
print("Monitoring run complete")
