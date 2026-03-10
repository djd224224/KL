#!/usr/bin/env python3
"""
Kalshi High Temperature Monitoring — FILLS, SETTLEMENTS, FINALIZED MARKETS
===========================================================================
Runs AFTER markets settle (evening session only) to collect:
  1. Fills   — every execution of your orders (links back to orders via order_id)
  2. Settlements — P&L per settled market
  3. Finalized markets — post-settlement volume, winner, and trade stats

LINEAGE for reporting:
  KXHIGH_orders.client_order_id  →  KXHIGH_fills.order_id  →  KXHIGH_settlements.ticker
                                                              →  KXHIGH_finalized_markets.market_ticker

  Example BigQuery join:
    SELECT o.city, o.market_ticker, o.no_price, o.contracts,
           f.no_price as fill_price, f.count as fill_count,
           s.profit, s.revenue,
           fm.winning_temp, fm.volume_traded
    FROM `project.Kalshi.KXHIGH_orders` o
    LEFT JOIN `project.Kalshi.KXHIGH_fills` f ON o.client_order_id = f.order_id
    LEFT JOIN `project.Kalshi.KXHIGH_settlements` s ON o.market_ticker = s.ticker
    LEFT JOIN `project.Kalshi.KXHIGH_finalized_markets` fm ON o.market_ticker = fm.market_ticker

GitHub Actions: run this as a separate scheduled job (evening only)
  or call it from the same workflow after the trading script.
Colab: run interactively to inspect yesterday's results.

Secrets: same as kalshi_weather_bot.py
"""

# =====================================================================
# IMPORTS
# =====================================================================
import os
import sys
import base64
import logging
from datetime import datetime, timedelta

IS_COLAB = "google.colab" in sys.modules or "COLAB_RELEASE_TAG" in os.environ
if IS_COLAB:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "google-cloud-bigquery", "db-dtypes", "pyarrow"])

import pytz
import pandas as pd
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient, HttpError

# =====================================================================
# LOGGING
# =====================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kalshi_high_temp_monitor")

# =====================================================================
# CONFIGURATION (shared with trading script)
# =====================================================================
KALSHI_API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
KALSHI_PRIVATE_KEY      = os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_API_BASE         = "https://api.elections.kalshi.com/trade-api/v2"

BQ_PROJECT = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
BQ_DATASET = os.environ.get("GCP_DATASET_ID", "Kalshi")
BQ_TABLE_PREFIX = "KXHIGH_"

# Event ticker definitions (same as trading script — needed to build yesterday's tickers)
EVENT_TICKER_DEFS = [
    ("KXHIGHCHI",  "Chicago",       56, 1.7), ("KXHIGHNY",   "New York City", 41, 1.5),
    ("KXHIGHDEN",  "Denver",        60, 2.4), ("KXHIGHPHIL", "Philadelphia",  47, 1.6),
    ("KXHIGHAUS",  "Austin",        60, 1.7), ("KXHIGHMIA",  "Miami",         46, 1.1),
    ("KXHIGHLAX",  "Los Angeles",   55, 2.0), ("KXHIGHTATL", "Atlanta",       55, 1.8),
    ("KXHIGHTDC",  "Washington DC", 50, 1.6), ("KXHIGHTPHX", "Phoenix",       55, 1.5),
    ("KXHIGHTDAL", "Dallas",        55, 1.8), ("KXHIGHTLV",  "Las Vegas",     55, 1.8),
    ("KXHIGHTOKC", "Oklahoma City", 55, 2.0), ("KXHIGHTSEA", "Seattle",       50, 1.5),
    ("KXHIGHTSFO", "San Francisco", 50, 1.5), ("KXHIGHTHOU", "Houston",       59, 2.0),
    ("KXHIGHTSATX","San Antonio",   55, 1.8), ("KXHIGHTMIN", "Minneapolis",   55, 2.0),
    ("KXHIGHTNOLA","New Orleans",   55, 1.8),
]

# City abbreviations (same order as trading script)
CITY_ABV_KEYS = ["THOU","SATX","TMIN","NOLA","CHI","AUS","DEN","NY-","PHI","MIA",
                 "LAX","ATL","TDC","PHX","DAL","TLV","OKC","SEA","SFO","HOU"]


# =====================================================================
# HELPERS
# =====================================================================

def decode_private_key(b64_key="", file_path=""):
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f"No private key found.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

def resolve_gcp_credentials():
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            from google.colab import auth; auth.authenticate_user()
            log.info("Authenticated via Colab")
        except ImportError: pass

def get_city_abv(ticker):
    for key in CITY_ABV_KEYS:
        if key in ticker: return key
    return ""

def build_yesterday_tickers(central_time):
    """Build event tickers for yesterday's markets."""
    yesterday = central_time - timedelta(days=1)
    m, d = yesterday.strftime("%b").upper(), yesterday.strftime("%d")
    return [(f"{prefix}-26{m}{d}", get_city_abv(prefix)) for prefix, *_ in EVENT_TICKER_DEFS]


# =====================================================================
# BIGQUERY — creates monitoring-specific tables
# =====================================================================

def setup_bigquery():
    from google.cloud import bigquery
    client = bigquery.Client(project=BQ_PROJECT)
    dataset_ref = bigquery.DatasetReference(BQ_PROJECT, BQ_DATASET)
    try: client.get_dataset(dataset_ref)
    except Exception:
        ds = bigquery.Dataset(dataset_ref); ds.location = "US"
        client.create_dataset(ds)

    SF = bigquery.SchemaField
    schemas = {
        # fills: every execution — join to orders via order_id ↔ client_order_id
        f"{BQ_TABLE_PREFIX}fills": [
            SF("trade_id","STRING"),     # Kalshi's unique fill ID
            SF("ticker","STRING"),       # Market ticker (e.g. KXHIGHCHI-26MAR10-B31)
            SF("event_ticker","STRING"), # Event ticker (e.g. KXHIGHCHI-26MAR10)
            SF("side","STRING"),         # "yes" or "no"
            SF("action","STRING"),       # "buy" or "sell"
            SF("count","INT64"),         # Contracts filled
            SF("yes_price","INT64"),     # YES price in cents
            SF("no_price","INT64"),      # NO price in cents
            SF("taker_side","STRING"),   # Who crossed the spread
            SF("created_time_utc","TIMESTAMP"),
            SF("created_time_central","TIMESTAMP"),
            SF("order_id","STRING"),     # ← JOIN KEY to KXHIGH_orders.client_order_id
            SF("concat_key","STRING"),   # ticker + no_price (for dedup)
        ],
        # settlements: P&L per market — join to orders via ticker ↔ market_ticker
        f"{BQ_TABLE_PREFIX}settlements": [
            SF("ticker","STRING"),           # Market ticker ← JOIN KEY
            SF("market_result","STRING"),     # "yes" or "no"
            SF("city_abv","STRING"),
            SF("trade_date","DATE"),
            SF("settled_time","TIMESTAMP"),
            SF("yes_count","INT64"),          # Contracts settled YES side
            SF("no_count","INT64"),           # Contracts settled NO side
            SF("yes_total_cost","FLOAT64"),   # Total $ spent on YES
            SF("no_total_cost","FLOAT64"),    # Total $ spent on NO
            SF("revenue","FLOAT64"),          # Payout received
            SF("profit","FLOAT64"),           # revenue - costs
        ],
        # finalized_markets: post-settlement stats — join via market_ticker
        f"{BQ_TABLE_PREFIX}finalized_markets": [
            SF("event_ticker","STRING"),
            SF("market_ticker","STRING"),     # ← JOIN KEY
            SF("city_abv","STRING"),
            SF("volume_traded","INT64"),      # Total contracts traded
            SF("avg_trade_price_yes","FLOAT64"),
            SF("avg_trade_price_no","FLOAT64"),
            SF("pct_volume_yes_taker","FLOAT64"),  # % of volume where taker was YES
            SF("pct_volume_no_taker","FLOAT64"),
            SF("trade_date","DATE"),
            SF("winning_temp","STRING"),      # Actual high temp that settled the market
            SF("winner","STRING"),            # "yes" or "no"
        ],
    }

    for name, schema in schemas.items():
        ref = dataset_ref.table(name)
        try: client.get_table(ref)
        except Exception:
            client.create_table(bigquery.Table(ref, schema=schema))
            log.info("Created table %s", name)

    return client, schemas


def write_to_bq(bq_client, schemas, df, table_name):
    from google.cloud import bigquery
    full = f"{BQ_TABLE_PREFIX}{table_name}" if not table_name.startswith(BQ_TABLE_PREFIX) else table_name
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{full}"
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", schema=schemas.get(full))
    try:
        job = bq_client.load_table_from_dataframe(df, table_id, job_config=cfg); job.result()
        log.info("  -> %s: %s rows", full, job.output_rows); return True
    except Exception as e:
        log.error("  -> %s ERROR: %s", full, e); return False


# =====================================================================
# DATA COLLECTORS
# =====================================================================

def collect_settlements(exchange_client, central_time):
    """Pull yesterday's settlement records from Kalshi API.
    Filters to KXHIGH markets matching yesterday's date.
    Computes profit = revenue - yes_cost - no_cost.
    """
    date = central_time.date() - timedelta(days=1)
    month, day = date.strftime("%b").upper(), date.strftime("%d")

    try:
        resp = exchange_client.get_portfolio_settlements(limit=1000, cursor=None)
    except Exception as e:
        log.error("Settlement fetch error: %s", e)
        return []

    rows = []
    for s in resp.get("settlements", []):
        # Skip zero-cost settlements (no position)
        if s["yes_total_cost"] <= 0 and s["no_total_cost"] <= 0:
            continue
        # Filter to HIGH temp markets matching yesterday
        if "HIGH" not in s["ticker"] or day not in s["ticker"] or month not in s["ticker"]:
            continue

        settled_dt = datetime.strptime(s["settled_time"], "%Y-%m-%dT%H:%M:%S.%fZ")
        trade_dt = settled_dt - timedelta(days=1)

        rows.append({
            "ticker": s["ticker"],
            "market_result": s.get("market_result", ""),
            "city_abv": get_city_abv(s["ticker"]),
            "trade_date": trade_dt.strftime("%Y-%m-%d"),
            "settled_time": s["settled_time"],
            "yes_count": s.get("yes_count", 0),
            "no_count": s.get("no_count", 0),
            "yes_total_cost": s["yes_total_cost"],
            "no_total_cost": s["no_total_cost"],
            "revenue": s["revenue"],
            "profit": s["revenue"] - s["yes_total_cost"] - s["no_total_cost"],
        })

    log.info("Found %d settlement records.", len(rows))
    return rows


def collect_finalized_markets(exchange_client, central_time):
    """Pull post-settlement data for yesterday's markets.
    For each market: total volume, VWAP, taker ratios, winning temp.
    """
    tickers = build_yesterday_tickers(central_time)
    trade_date = (central_time - timedelta(days=1)).strftime("%Y-%m-%d")

    rows = []
    for ticker, abv in tickers:
        try:
            resp = exchange_client.get_event(event_ticker=ticker)
        except (HttpError, Exception) as e:
            log.warning("Finalized event error %s: %s", ticker, e)
            continue

        for mkt in resp.get("markets", []):
            # Check if this market was the winner
            winning_temp, winner = None, None
            if mkt.get("result") == "yes":
                winning_temp = mkt.get("expiration_value")
                winner = "yes"

            # Pull all trades for volume/price stats
            try:
                tr = exchange_client.get_trades(limit=None, cursor=None, ticker=mkt["ticker"])
            except (HttpError, Exception) as e:
                log.warning("Trades error %s: %s", mkt["ticker"], e)
                continue

            vol = avg_yes = avg_no = pct_yes = pct_no = 0
            for t in tr.get("trades", []):
                vol += t["count"]
                avg_yes += t["yes_price"] * t["count"]
                avg_no += t["no_price"] * t["count"]
                if t["taker_side"] == "yes": pct_yes += t["count"]
                elif t["taker_side"] == "no": pct_no += t["count"]
            if vol > 0:
                avg_yes /= vol; avg_no /= vol
                pct_yes /= vol; pct_no /= vol

            rows.append({
                "event_ticker": ticker,
                "market_ticker": mkt["ticker"],
                "city_abv": abv,
                "volume_traded": vol,
                "avg_trade_price_yes": avg_yes,
                "avg_trade_price_no": avg_no,
                "pct_volume_yes_taker": pct_yes,
                "pct_volume_no_taker": pct_no,
                "trade_date": trade_date,
                "winning_temp": str(winning_temp) if winning_temp else None,
                "winner": winner,
            })

    log.info("Collected %d finalized market records.", len(rows))
    return rows


def collect_fills(exchange_client, central_time):
    """Pull fill records for yesterday's markets.
    Each fill links back to your order via order_id → client_order_id.
    """
    tickers = build_yesterday_tickers(central_time)

    rows = []
    for ticker, _ in tickers:
        try:
            resp = exchange_client.get_event(event_ticker=ticker)
        except (HttpError, Exception) as e:
            log.warning("Fills event error %s: %s", ticker, e)
            continue

        for mkt in resp.get("markets", []):
            try:
                fr = exchange_client.get_fills(limit=1000, cursor=None, ticker=mkt["ticker"])
            except Exception as e:
                log.warning("Fills error %s: %s", mkt["ticker"], e)
                continue

            for f in fr.get("fills", []):
                utc = pd.to_datetime(f.get("created_time"), utc=True)
                rows.append({
                    "trade_id": f.get("trade_id", ""),
                    "ticker": f.get("ticker", ""),
                    "event_ticker": ticker,
                    "side": f.get("side", ""),
                    "action": f.get("action", ""),
                    "count": f.get("count", 0),
                    "yes_price": f.get("yes_price", 0),
                    "no_price": f.get("no_price", 0),
                    "taker_side": f.get("taker_side", ""),
                    "created_time_utc": utc,
                    "created_time_central": utc.tz_convert("US/Central"),
                    "order_id": f.get("order_id", ""),       # ← links to KXHIGH_orders.client_order_id
                    "concat_key": f.get("ticker", "") + str(f.get("no_price", "")),
                })

    log.info("Collected %d fill records.", len(rows))
    return rows


# =====================================================================
# MAIN
# =====================================================================

def main():
    log.info("=" * 60)
    log.info("Kalshi High Temp Monitoring (%s)", "Colab" if IS_COLAB else "GitHub Actions / CLI")
    log.info("=" * 60)

    if not KALSHI_API_KEY_ID:
        log.error("KALSHI_API_KEY_ID not set.")
        sys.exit(1)
    if not KALSHI_PRIVATE_KEY and not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        log.error("No Kalshi private key found.")
        sys.exit(1)

    central_time = datetime.now(pytz.timezone("US/Central"))
    log.info("CT: %s", central_time.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Collecting data for yesterday: %s",
             (central_time - timedelta(days=1)).strftime("%Y-%m-%d"))

    # --- Connect to Kalshi ---
    pk = decode_private_key(b64_key=KALSHI_PRIVATE_KEY, file_path=KALSHI_PRIVATE_KEY_PATH)
    xc = ExchangeClient(exchange_api_base=KALSHI_API_BASE, key_id=KALSHI_API_KEY_ID, private_key=pk)
    st = xc.get_exchange_status()
    log.info("Exchange: %s", st)

    # --- Connect to BigQuery ---
    log.info("BigQuery: project=%s dataset=%s", BQ_PROJECT, BQ_DATASET)
    resolve_gcp_credentials()
    bq, schemas = setup_bigquery()

    # ===== Settlements =====
    log.info("--- Collecting settlements ---")
    settlements = collect_settlements(xc, central_time)
    if settlements:
        sdf = pd.DataFrame(settlements)
        sdf["trade_date"] = pd.to_datetime(sdf["trade_date"])
        sdf["settled_time"] = pd.to_datetime(sdf["settled_time"])
        write_to_bq(bq, schemas, sdf, "settlements")

        # Print P&L summary
        total_profit = sdf["profit"].sum()
        winners = sdf[sdf["profit"] > 0]
        losers = sdf[sdf["profit"] < 0]
        log.info("P&L: $%.2f (%d winners, %d losers)", total_profit, len(winners), len(losers))
    else:
        log.info("No settlements found for yesterday.")

    # ===== Finalized Markets =====
    log.info("--- Collecting finalized markets ---")
    fin = collect_finalized_markets(xc, central_time)
    if fin:
        fdf = pd.DataFrame(fin)
        fdf["trade_date"] = pd.to_datetime(fdf["trade_date"])
        fdf["volume_traded"] = pd.to_numeric(fdf["volume_traded"], errors="coerce").fillna(0).astype("Int64")
        for c in ["avg_trade_price_yes", "avg_trade_price_no", "pct_volume_yes_taker", "pct_volume_no_taker"]:
            fdf[c] = pd.to_numeric(fdf[c], errors="coerce")
        write_to_bq(bq, schemas, fdf, "finalized_markets")
    else:
        log.info("No finalized market data.")

    # ===== Fills =====
    log.info("--- Collecting fills ---")
    fills = collect_fills(xc, central_time)
    if fills:
        write_to_bq(bq, schemas, pd.DataFrame(fills), "fills")
    else:
        log.info("No fills found.")

    log.info("=" * 60)
    log.info("Monitoring complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
