#!/usr/bin/env python3
# KXNCAABMENTION v4.5-NCAAB
# STEP 1: FETCH DATA
# FIXED: Team-level historical rates now correct (no longer flipping based on team position)
# ADAPTED: Series ticker changed to KXNCAABMENTION for college basketball

from __future__ import annotations
import time

import pandas as pd
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import os
import uuid
from datetime import datetime, UTC, timedelta
from typing import Any, Dict, List, Optional, Tuple
import json
from urllib.request import urlopen, Request
import numpy as np
from google.cloud import bigquery
import smtplib
from email.mime.text import MIMEText

# =========================
# ALERTING
# =========================
_ALERTS = []
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")


def alert(category: str, message: str, context: dict = None):
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "category": category,
        "message": message,
        **(context or {}),
    }
    _ALERTS.append(entry)
    ctx_str = f" | {context}" if context else ""
    print(f"  ⚡ ALERT [{category}]: {message}{ctx_str}")


def send_alert_notification():
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASSWORD or not ALERT_EMAIL_TO:
        return
    if len(_ALERTS) == 0:
        return
    cats = {}
    for a in _ALERTS:
        c = a["category"]
        cats[c] = cats.get(c, 0) + 1
    lines = [f"KXNCAABMENTION Alerts: {len(_ALERTS)} total"]
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    lines.append("")
    for a in _ALERTS[:8]:
        lines.append(f"[{a['category']}] {a['message']}")
    if len(_ALERTS) > 8:
        lines.append(f"... +{len(_ALERTS) - 8} more")
    body = "\n".join(lines)

    # Carrier SMS gateways silently drop long messages — truncate for SMS recipients
    SMS_GATEWAYS = ("tmomail.net", "vtext.com", "txt.att.net", "messaging.sprintpcs.com", "msg.fi.google.com")
    recipients = [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]

    for recipient in recipients:
        try:
            is_sms = any(gw in recipient.lower() for gw in SMS_GATEWAYS)
            send_body = body[:300] if is_sms else body

            msg = MIMEText(send_body)
            msg["Subject"] = f"NCAAB Bot: {len(_ALERTS)} alerts" if not is_sms else ""
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = recipient

            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
        except Exception as e:
            print(f"  ⚠️ Alert to {recipient} failed: {e}")

    print(f"  ✓ Alert notification sent to {len(recipients)} recipient(s)")


def upload_alerts_to_bq(bq_client_ref, project_id, dataset_id, series_ticker):
    if bq_client_ref is None or len(_ALERTS) == 0:
        return
    try:
        df_alerts = pd.DataFrame(_ALERTS)
        df_alerts["run_id"] = RUN_ID
        df_alerts["model_version"] = MODEL_VERSION
        table_id = f"{project_id}.{dataset_id}.{series_ticker}_alerts"
        job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", autodetect=True)
        job = bq_client_ref.load_table_from_dataframe(df_alerts, table_id, job_config=job_config)
        job.result()
        print(f"  ✓ Uploaded {len(df_alerts)} alerts to {table_id}")
    except Exception as e:
        print(f"  ⚠️ Alert upload failed: {e}")

# =========================
# CONFIG
# =========================

SERIES_TICKER = "KXNCAABMENTION"  # [NCAAB-1]
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "/content/Lisa_Kalshi.txt")
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

SLEEP_BETWEEN_CALLS_SEC = 0.15 

# =========================
# AUTHENTICATION
# =========================
def load_private_key_from_file(file_path: str):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key

PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)

exchange_client = ExchangeClient(
    exchange_api_base=API_BASE,
    key_id=API_KEY_ID,
    private_key=PRIVATE_KEY
)

print("Testing connection...")
try:
    status = exchange_client.get_exchange_status()
    print(f"✓ Connected! Trading active: {status['trading_active']}")
except Exception as e:
    print(f"✗ Connection failed: {e}")
    raise

# =========================
# API CALLS
# =========================
def get_all_markets_for_series(series_ticker: str, status_filter: str = None) -> List[Dict[str, Any]]:
    all_markets = []
    cursor = None
    page = 0
    while True:
        page += 1
        print(f"  Fetching markets page {page}...")
        params = {"series_ticker": series_ticker, "limit": 200}
        if status_filter:
            params["status"] = status_filter
        if cursor:
            params["cursor"] = cursor
        try:
            response = exchange_client.get_markets(**params)
            markets = response.get('markets', [])
            all_markets.extend(markets)
            print(f"    Retrieved {len(markets)} markets (total: {len(all_markets)})")
            cursor = response.get('cursor')
            if not cursor:
                break
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)
        except Exception as e:
            print(f"Error fetching markets: {e}")
            import traceback
            traceback.print_exc()
            break
    return all_markets

# =========================
# PARSING
# =========================
def normalize_yes_no(result: Any) -> str:
    if result in ("yes", "no"):
        return str(result).upper()
    return ""

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
        "date_code": date_code, "event_date": event_date_iso,
        "teams_code": teams_code, "team_1": team_1, "team_2": team_2,
    }

def split_market_ticker(ticker: str) -> Dict[str, str]:
    parts = (ticker or "").split("-")
    series = parts[0] if len(parts) > 0 else ""
    event_code = parts[1] if len(parts) > 1 else ""
    market_code = parts[2] if len(parts) > 2 else ""
    parsed = parse_event_code(event_code)
    return {"ticker_part_1_series": series, "ticker_part_2_event_code": event_code,
            "ticker_part_3_market_code": market_code, **parsed}

# =========================
# MAIN DATA COLLECTION
# =========================
def main():
    print(f"\n{'='*70}")
    print(f"FETCHING DATA FOR {SERIES_TICKER}")
    print(f"{'='*70}\n")

    print("Fetching ALL markets...")
    all_markets = get_all_markets_for_series(SERIES_TICKER, status_filter=None)
    print(f"\n✓ Fetched {len(all_markets)} total markets")

    if len(all_markets) == 0:
        print("⚠️  No markets found - returning empty dataframes")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    for m in all_markets:
        market_ticker = m.get("ticker") or ""
        split_cols = split_market_ticker(market_ticker)
        row = {
            "series_ticker": m.get("series_ticker"),
            "event_ticker": m.get("event_ticker"),
            "event_title": m.get("title") or m.get("subtitle") or "",
            "market_ticker": market_ticker,
            "market_status": m.get("status"),
            "result_yes_no": normalize_yes_no(m.get("result")),
        }
        row.update(split_cols)
        rows.append(row)

    df_results = pd.DataFrame(rows)
    print(f"\nBuilt df_results: {df_results.shape}")

    status_counts = df_results['market_status'].value_counts()
    print(f"\nMarket status breakdown:")
    for status, count in status_counts.items():
        print(f"  {status}: {count}")

    active_statuses = ['open', 'active']
    settled_statuses = ['settled', 'finalized', 'closed']
    df_results['is_active'] = df_results['market_status'].isin(active_statuses)
    df_results['is_settled'] = df_results['market_status'].isin(settled_statuses) & (df_results['result_yes_no'] != '')
    print(f"\n  Active/tradeable markets: {df_results['is_active'].sum()}")
    print(f"  Settled markets with results: {df_results['is_settled'].sum()}")

    # BUILD ROLLING SUMMARY
    print("\nBuilding rolling summary (from SETTLED markets only)...")
    df_settled = df_results[df_results['is_settled']].copy()
    print(f"  Using {len(df_settled)} settled markets with results")

    if len(df_settled) == 0:
        print("⚠️  No settled markets found")
        return df_results, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df_by_date = (
        df_settled.query("ticker_part_3_market_code != '' and event_date != ''")
        .groupby(['ticker_part_3_market_code', 'event_date'], as_index=False)
        .agg(count_occurrences=('market_ticker', 'size'),
             count_yes=('result_yes_no', lambda s: (s == 'YES').sum()))
        .sort_values(['ticker_part_3_market_code', 'event_date'])
    )
    if len(df_by_date) == 0:
        return df_results, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df_summary_rolling = df_by_date.copy()
    df_summary_rolling['count_occurrences_rolling'] = df_summary_rolling.groupby('ticker_part_3_market_code')['count_occurrences'].cumsum()
    df_summary_rolling['count_yes_rolling'] = df_summary_rolling.groupby('ticker_part_3_market_code')['count_yes'].cumsum()
    df_summary_rolling['yes_rate_rolling'] = df_summary_rolling['count_yes_rolling'] / df_summary_rolling['count_occurrences_rolling']

    df_summary = (
        df_summary_rolling
        .sort_values(['ticker_part_3_market_code', 'event_date'], ascending=[True, False])
        .groupby('ticker_part_3_market_code', as_index=False).first()
        .drop(columns=['count_occurrences', 'count_yes'])
        .rename(columns={'count_occurrences_rolling': 'count_occurrences',
                         'count_yes_rolling': 'count_yes', 'yes_rate_rolling': 'yes_rate'})
    )
    print(f"  df_summary: {df_summary.shape}")

    # TEAM-LEVEL ROLLING
    print("\nBuilding team-level rolling summary...")
    df_filtered = df_settled.query("ticker_part_3_market_code != '' and event_date != '' and team_1 != '' and team_2 != ''").copy()

    if len(df_filtered) == 0:
        df_summary_filtered = df_summary[df_summary['count_occurrences'] > 3][["ticker_part_3_market_code", "yes_rate"]].copy()
        df_results = df_results.merge(df_summary_filtered, on="ticker_part_3_market_code", how="left")
        df_results["yes_rate"] = df_results.apply(lambda row: row["yes_rate"] if row["is_active"] else "", axis=1)
        return df_results, df_summary, df_summary_rolling, pd.DataFrame()

    df_team1 = df_filtered.copy(); df_team1['team'] = df_team1['team_1']; df_team1['is_team_1'] = True
    df_team2 = df_filtered.copy(); df_team2['team'] = df_team2['team_2']; df_team2['is_team_1'] = False
    df_team_exploded = pd.concat([df_team1, df_team2], ignore_index=True)
    df_team_exploded['team_result'] = df_team_exploded['result_yes_no']

    df_team_by_date = (
        df_team_exploded.groupby(['ticker_part_3_market_code', 'team', 'event_date'], as_index=False)
        .agg(count_occurrences=('market_ticker', 'size'),
             count_yes=('team_result', lambda s: (s == 'YES').sum()))
        .sort_values(['ticker_part_3_market_code', 'team', 'event_date'])
    )

    df_summary_rolling_by_team = df_team_by_date.copy()
    df_summary_rolling_by_team['count_occurrences_rolling'] = df_summary_rolling_by_team.groupby(['ticker_part_3_market_code', 'team'])['count_occurrences'].cumsum()
    df_summary_rolling_by_team['count_yes_rolling'] = df_summary_rolling_by_team.groupby(['ticker_part_3_market_code', 'team'])['count_yes'].cumsum()
    df_summary_rolling_by_team['yes_rate_rolling'] = df_summary_rolling_by_team['count_yes_rolling'] / df_summary_rolling_by_team['count_occurrences_rolling']
    print(f"  df_summary_rolling_by_team: {df_summary_rolling_by_team.shape}")

    # ADD yes_rate TO ACTIVE MARKETS
    df_summary_filtered = df_summary[df_summary['count_occurrences'] > 3][["ticker_part_3_market_code", "yes_rate"]].copy()
    df_results = df_results.merge(df_summary_filtered, on="ticker_part_3_market_code", how="left")
    df_results["yes_rate"] = df_results.apply(lambda row: row["yes_rate"] if row["is_active"] else "", axis=1)
    df_results.loc[df_results['is_active'], 'market_status'] = 'active'

    print(f"\nDataframes created:")
    print(f"  df_results: {df_results.shape}")
    print(f"  df_summary: {df_summary.shape}")
    print(f"  df_summary_rolling: {df_summary_rolling.shape}")
    print(f"  df_summary_rolling_by_team: {df_summary_rolling_by_team.shape}")
    return df_results, df_summary, df_summary_rolling, df_summary_rolling_by_team

df_results, df_summary, df_summary_rolling, df_summary_rolling_by_team = main()
print("\n✓ Data collection complete!")

# NCAABMENTION ORDER SCRIPT v4.5-NCAAB
# ==============================================================
# [v4.5] Changes from v4.4 (driven by code×direction P&L analysis):
#
#   SIDE_MULTIPLIERS rewritten based on directional edge analysis:
#     - SCHE/DRAF/NIL/MARC: Block Yes entirely (combined Yes drain: -$592)
#     - ANKL/RECO/ALL/ALLA: Flipped to Yes-favored (No was losing)
#     - TRAN: Yes promoted to 1.0x (5/5 in tournament)
#     - DOUB: No promoted to 1.2x (+$174/+81% in 7d tournament data)
#     - BUZZ No blocked (-$113/-55%/-16pp edge, worst in dataset)
#     - WALK fully blocked (catastrophic variance at 75¢ avg No price)
#     - AIRB Yes reduced 0.4→0.1 (-$65/-26% all-time)
#     - ELBO Yes reduced 0.5→0.2 (deteriorated to -$77/-26% in 7d)
#
#   TIER1_MARKETS: Added DOUB (tournament regime shift)
#   TEAM_MARKET_OVERRIDES: Flipped MIC/RECO to match new Yes-favored RECO
# ==============================================================


RUN_ID = str(uuid.uuid4())
MODEL_VERSION = "v4.5-NCAAB"
PRICING_STRATEGY = 'hybrid'

# =========================
# [NCAAB-1] CONFIG
# =========================
# SERIES_TICKER already defined in data fetch cell
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "/content/Lisa_Kalshi.txt")
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SLEEP_BETWEEN_CALLS_SEC = 0.05

# ---------- BAYESIAN HYBRID PRICING PARAMETERS ----------
BAYESIAN_K = 25
BAYESIAN_RECENCY_HALFLIFE_GAMES = 15
BAYESIAN_MARKET_WEIGHT = 0.70
BAYESIAN_HISTORICAL_WEIGHT = 0.30

# =========================
# POSITION MANAGEMENT
# =========================
MAX_NET_PER_MARKET = 250
# [v4.5] DOUB added to Tier 1 — tournament regime shift (+$174/+81%/+42pp in 7d)
TIER1_MARKETS = {"SCHE", "DRAF", "MARC", "NIL"}
# [v4.5] Top NO markets get tightest offsets (1¢ increments) — most profitable NO codes
TOP_NO_MARKETS = {"SCHE", "DRAF", "NIL"}

# =====================================================================
# [v4.5] AGGRESSIVE MARKET PRICING — easy on/off toggle
# Markets here use 100% orderbook mid (no historical blend) and
# NO offsets start at 0 (bid AT fair value on first level).
# To disable: set to empty set  →  AGGRESSIVE_MARKET_PRICING = set()
# =====================================================================
# AGGRESSIVE_MARKET_PRICING = {"SCHE", "DRAF", "NIL"}   # ← toggle here
AGGRESSIVE_MARKET_PRICING = {}   # ← toggle here
TIER2_MAX_NET = 100
POSITION_MODERATE_THRESHOLD = 125
POSITION_STOP_THRESHOLD = 250
MAX_ORDERBOOK_LEVELS_ABOVE = 2

MAX_NET_PER_EVENT = 1000
MAX_ORDERS_PER_EVENT = 2000

MIN_SPREAD_BOTH_SIDES = 0
MIN_EV_PER_ORDER = 0.02
SAFE_MODE_MULTIPLIER = 0.10

# =========================
# PAIRING MODE
# =========================
PAIRING_MODE_THRESHOLD = 0.60
PAIRING_MODE_AGGRESSIVE = 0.85
PAIRING_MODE_NET_FLOOR = 150      # [v4.5] was 60 — 63 net No shouldn't trigger pairing
PAIRING_MODE_NET_AGGRESSIVE = 200  # [v4.5] was 100 — scaled up with floor

HEDGE_MIN_PRICE_YES = 15
HEDGE_MIN_PRICE_NO = 5
HEDGE_YES_MIN_PRICE = 15

MAX_PAIRED_PER_MARKET = 250

# =========================
# [v4.5] SIDE MULTIPLIERS — rewritten from code×direction P&L analysis
# =========================
SIDE_MULTIPLIERS = {
    # --- BLOCKED: proven losers or catastrophic variance ---
    "WALK": {"yes": 0.0, "no": 0.0},   # [v4.5] 5/6 WR but -$54/-45% ROI. 1 loss at 75¢ wipes 3 wins.
    "BUZZ": {"yes": 0.0, "no": 0.0},   # [v4.5b] Blocked both sides

    # --- TIER 1: Strong No edge, block Yes ---
    "SCHE": {"yes": 0.0, "no": 2.5},   # [v4.5] Yes: -$204/-49%/-11.7pp. No: +$649/+67%/+23.3pp. Block Yes.
    "NIL":  {"yes": 0.0, "no": 2.0},   # [v4.5] Yes: 0/7 all-time. No: +$382/+25%/+15.3pp. Block Yes.
    "DRAF": {"yes": 0.0, "no": 2.5},   # [v4.5] Yes: -$293/-65%/-21.9pp. No: +$752/+92%/+17pp. Block Yes.
    "MARC": {"yes": 0.0, "no": 1.5},   # [v4.5] Yes: 0/6 all-time. No: +$205/+12%/+6.4pp. Block Yes.
    "ELBO": {"yes": 0.2, "no": 0.2},   # [v4.5] Yes deteriorated: -$77/-26% in 7d. Reduced from 0.5.
    "DOUB": {"yes": 0.0, "no": 0.0},   # [v4.5] 7d No: +$174/+81%/+42pp. Tournament regime. Promoted.

    # --- YES-FAVORED: Flipped from directional analysis ---
    "ANKL": {"yes": 1.2, "no": 0.1},   # [v4.5] Yes: +$177/+102%/+24.7pp. No: -$242/-30%/-5.2pp. Flipped.
    "RECO": {"yes": 1.0, "no": 0.0},   # [v4.5] Yes: +$128/+36%/+21.2pp. No: -$190/-29%. Block No.
    "ALL":  {"yes": 0.8, "no": 0.1},   # [v4.5] Yes: +$63/+48%/+13.1pp. No: -$34/-8%. Flipped.
    "ALLA": {"yes": 0.8, "no": 0.0},   # [v4.5] Yes: +$47/+93%/+33.9pp. No: -$17/-22%. Block No.
    "TRAN": {"yes": 1.0, "no": 0.1},   # [v4.5] 7d Yes: 5/5, +$60/+55%. Tournament-driven. Boost Yes.

    # --- REDUCED/CONSERVATIVE ---
    "AIRB": {"yes": 0.3, "no": 0.3},   # [v4.5b] Blocked both sides
    "OVER": {"yes": 0.0, "no": 0.0},   # [v4.5b] Blocked both sides
    "RECR": {"yes": 0.2, "no": 0.3},   # Neither side has clear edge. Keep minimal.
    "ALLE": {"yes": 0.1, "no": 0.5},   # [v4.5] No edge +6.3pp but ROI flat. Yes 0/4 in 7d. Conservative.
}

# =========================
# [v4.5] TEAM-SPECIFIC OVERRIDES — updated
# =========================
TEAM_MARKET_OVERRIDES = {
    # MARC — block teams where MARC settled Yes in March
    ("UKF", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("AUB", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("ARK", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("VAN", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("ISU", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("PUR", "MARC"): {"yes_mult": 0.0, "no_mult": 0.0},

    # ALLE — block high-loss teams
    ("UKF", "ALLE"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("TEN", "ALLE"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("UNC", "ALLE"): {"yes_mult": 0.0, "no_mult": 0.0},

    # DRAF — boost reliable No teams
    ("VAN", "DRAF"): {"yes_mult": 0.0, "no_mult": 1.8},
    ("ILL", "DRAF"): {"yes_mult": 0.0, "no_mult": 1.8},
    ("PUR", "DRAF"): {"yes_mult": 0.0, "no_mult": 1.5},
    ("ALA", "DRAF"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("DUK", "DRAF"): {"yes_mult": 0.0, "no_mult": 0.0},
    ("FLA", "DRAF"): {"yes_mult": 0.0, "no_mult": 0.0},

    # [v4.5] RECO flipped to Yes-favored — MIC override now boosts Yes
    ("MIC", "RECO"): {"yes_mult": 1.5, "no_mult": 0.0},
}

# [P2] GAME-LEVEL RISK MULTIPLIER
TEAM_RISK_MULTIPLIER = {
    "ALA": 0.5, "ARK": 0.5, "FLA": 0.5, "BYU": 0.5,
    "MIC": 1.3, "TTU": 1.3, "ISU": 1.2,
}

# =========================
# [v4.5] THE ODDS API — GAME START TIME SOURCE
# =========================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "e2d4375f44ff9cc272da93bee587a46a")
ODDS_API_NCAAB_SPORT_KEY = "basketball_ncaab"
ODDS_API_EXPIRATION_BUFFER_SEC = 0

NCAAB_TEAM_NAME_TO_CODE = [
    ("north carolina state", "NCS"), ("nc state", "NCS"),
    ("north carolina", "UNC"),
    ("south carolina", "SCA"),
    ("michigan state", "MSU"), ("michigan", "MIC"),
    ("mississippi state", "MSS"), ("ole miss", "MIS"), ("mississippi", "MIS"),
    ("iowa state", "ISU"), ("iowa", "IOW"),
    ("arkansas", "ARK"),
    ("kansas state", "KSU"), ("kansas", "KAN"),
    ("florida state", "FSU"), ("florida", "FLA"),
    ("arizona state", "ASU"), ("arizona", "ARI"),
    ("colorado state", "CSU"), ("colorado", "COL"),
    ("ohio state", "OHS"), ("ohio", "OHI"),
    ("oklahoma state", "OKS"), ("oklahoma", "OKL"),
    ("oregon state", "ORS"), ("oregon", "ORE"),
    ("penn state", "PEN"),
    ("virginia tech", "VIT"), ("west virginia", "WVU"), ("virginia", "VIR"),
    ("texas a&m", "TAM"), ("texas tech", "TTU"), ("texas", "TEX"),
    ("georgia tech", "GAT"), ("georgetown", "GEO"), ("georgia", "UGA"),
    ("washington state", "WSU"), ("washington", "WAS"),
    ("saint joseph", "JOES"), ("st. joseph", "JOES"),
    ("saint john", "STJ"), ("st. john", "STJ"),
    ("saint mary", "SMC"), ("st. mary", "SMC"),
    ("san diego state", "SDS"),
    ("southern california", "USC"), ("usc trojans", "USC"),
    ("grand canyon", "GCU"),
    ("boise state", "BOI"),
    ("wake forest", "WAK"),
    ("seton hall", "SET"),
    ("notre dame", "NOT"),
    ("alabama", "ALA"), ("auburn", "AUB"),
    ("baylor", "BAY"), ("brigham young", "BYU"), ("byu", "BYU"),
    ("butler", "BUT"), ("cincinnati", "CIN"), ("clemson", "CLE"),
    ("connecticut", "CON"), ("uconn", "CON"),
    ("creighton", "CRE"), ("dayton", "DAY"), ("drake", "DRA"),
    ("duke", "DUK"), ("gonzaga", "GON"), ("houston", "HOU"),
    ("illinois", "ILL"), ("indiana", "IND"),
    ("kentucky", "UKF"),
    ("louisville", "LOU"), ("lsu", "LSU"),
    ("marquette", "MRQ"), ("maryland", "MAR"),
    ("memphis", "MEM"), ("miami", "MIA"), ("minnesota", "MIN"),
    ("missouri", "MIZ"), ("nebraska", "NEB"), ("nevada", "NEV"),
    ("new mexico state", "NMSU"), ("new mexico", "UNM"),
    ("middle tennessee", "MTSU"), ("east tennessee", "ETSU"),
    ("northwestern", "NOR"),
    ("pittsburgh", "PIT"), ("providence", "PRO"), ("purdue", "PUR"),
    ("rutgers", "RUT"), ("stanford", "STA"), ("syracuse", "SYR"),
    ("tcu", "TCU"), ("tennessee", "TEN"), ("tulane", "TUL"),
    ("ucla", "UCL"), ("utah", "UTA"), ("vanderbilt", "VAN"),
    ("villanova", "VIL"), ("wisconsin", "WIS"), ("xavier", "XAV"),
]

ncaab_schedule_cache = {}
ncaab_schedule_events = {}


def ncaab_team_to_code(full_name):
    """Convert Odds API full team name to Kalshi 3-letter code."""
    name_lower = full_name.lower().strip()
    for key, code in NCAAB_TEAM_NAME_TO_CODE:
        if key in name_lower:
            return code
    parts = full_name.strip().split()
    if parts:
        return parts[0][:3].upper()
    return None


KALSHI_CODE_ALIASES = {
    "SJU":  "john",
    "ILST": "illinois",
    "UKF":  "kentucky",
    "NCS":  "nc state",
    "MSU":  "michigan state",
    "ISU":  "iowa state",
    "KSU":  "kansas state",
    "FSU":  "florida state",
    "ASU":  "arizona state",
    "CSU":  "colorado state",
    "OHS":  "ohio state",
    "OKS":  "oklahoma state",
    "ORS":  "oregon state",
    "VIT":  "virginia tech",
    "WVU":  "west virginia",
    "TAM":  "texas a",
    "GAT":  "georgia tech",
    "GEO":  "georgetown",
    "UGA":  "georgia",
    "SDS":  "san diego state",
    "USC":  "southern california",
    "GCU":  "grand canyon",
    "BOI":  "boise state",
    "MRQ":  "marquette",
    "STJ":  "john",
    "SMC":  "mary",
    "WSU":  "washington state",
    "SCA":  "south carolina",
    "CON":  "connecticut",
    "NOT":  "notre dame",
    "UNM":  "new mexico",
    "NMSU": "new mexico state",
    "SDSU": "san diego state",
    "ETSU": "east tennessee",
    "MTSU": "middle tennessee",
    "JOES": "joseph",
}


def _code_matches_team(code, full_name):
    """Check if a Kalshi ticker code segment plausibly matches an Odds API team name."""
    code_upper = code.upper()
    name_clean = full_name.upper().replace("'", "").replace(".", " ").replace("-", " ")
    words = name_clean.split()

    if any(w.startswith(code_upper) for w in words):
        return True

    alias = KALSHI_CODE_ALIASES.get(code_upper)
    if alias and alias.upper() in full_name.upper():
        return True

    name_compressed = "".join(words)
    if len(code_upper) >= 3 and code_upper in name_compressed:
        return True

    return False


def _match_teams_code_to_event(teams_code, home_full, away_full):
    """Try ALL possible split points of the raw Kalshi teams_code string."""
    tc = teams_code.upper()
    for k in range(2, len(tc) - 1):
        left, right = tc[:k], tc[k:]
        if (_code_matches_team(left, home_full) and _code_matches_team(right, away_full)):
            return True
        if (_code_matches_team(left, away_full) and _code_matches_team(right, home_full)):
            return True
    return False


def fetch_ncaab_schedule():
    """Fetch NCAAB events from The Odds API. Populates global caches."""
    global ncaab_schedule_cache, ncaab_schedule_events
    ncaab_schedule_cache = {}
    ncaab_schedule_events = {}

    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_NCAAB_SPORT_KEY}/events?apiKey={ODDS_API_KEY}"
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        print(f"  ✓ Fetched {len(data)} NCAAB events from Odds API")

        matched = 0
        for event in data:
            commence = event.get("commence_time")
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            if not commence or not home or not away:
                continue

            ct = commence
            if ct.endswith("Z"):
                ct = ct[:-1] + "+00:00"
            game_dt = datetime.fromisoformat(ct)
            game_unix = int(game_dt.timestamp())
            game_date = game_dt.strftime("%Y%m%d")

            ncaab_schedule_events.setdefault(game_date, []).append({
                "home": home, "away": away, "ts": game_unix
            })

            home_code = ncaab_team_to_code(home)
            away_code = ncaab_team_to_code(away)
            if home_code and away_code:
                teams_sorted = sorted([home_code, away_code])
                cache_key = f"{game_date}-{teams_sorted[0]}-{teams_sorted[1]}"
                ncaab_schedule_cache[cache_key] = game_unix
                matched += 1

        print(f"  ✓ Code-matched {matched}/{len(data)} events")
        print(f"  ✓ Schedule cache: {len(ncaab_schedule_cache)} code entries, {len(ncaab_schedule_events)} date buckets")
    except Exception as e:
        alert("SCHEDULE_FETCH_FAILED", f"Odds API unreachable: {e}")
        print(f"  ⚠️ Falling back to ticker-estimated game times (~7pm ET)")


# =====================================================================
# MANUAL GAME START OVERRIDES — DELETE THIS BLOCK WHEN NO LONGER NEEDED
# =====================================================================
# Keyed by event_code (2nd segment of ticker, e.g. "MARMAD" from KXNCAABMENTION-MARMAD-SCHE)
# MARMAD = Michigan vs UConn, 8:50pm EDT Apr 6 2026
MANUAL_GAME_START_OVERRIDES = {
    "MARMAD": int(datetime(2026, 4, 7, 0, 50, tzinfo=UTC).timestamp()),
}
# =====================================================================
# END MANUAL OVERRIDES
# =====================================================================


def get_game_start_ts(team_1, team_2, event_date_str):
    """Look up game start time from Odds API schedule cache."""
    if not ncaab_schedule_cache and not ncaab_schedule_events:
        return None, "ticker_estimate"
    if not team_1 or not team_2 or not event_date_str:
        return None, "ticker_estimate"

    try:
        dt = datetime.strptime(str(event_date_str), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None, "ticker_estimate"

    teams_code = (team_1 + team_2).upper()

    dates_to_check = [
        dt.strftime("%Y%m%d"),
        (dt + timedelta(days=1)).strftime("%Y%m%d"),
        (dt + timedelta(days=2)).strftime("%Y%m%d"),
    ]

    for date_key in dates_to_check:
        teams_sorted = sorted([team_1.upper(), team_2.upper()])
        cache_key = f"{date_key}-{teams_sorted[0]}-{teams_sorted[1]}"
        if cache_key in ncaab_schedule_cache:
            return ncaab_schedule_cache[cache_key], "odds_api"

        events_on_date = ncaab_schedule_events.get(date_key, [])
        for evt in events_on_date:
            if _match_teams_code_to_event(teams_code, evt["home"], evt["away"]):
                primary_cache_key = f"{dates_to_check[0]}-{teams_sorted[0]}-{teams_sorted[1]}"
                ncaab_schedule_cache[primary_cache_key] = evt["ts"]
                return evt["ts"], "odds_api"

    return None, "ticker_estimate"


# =========================
# PRICE FILTERS
# =========================
MIN_PRICE_YES = 20
MIN_PRICE_NO = 15
MAX_PRICE = 75
# [v4.5] Per-market NO MAX_PRICE overrides
MAX_PRICE_OVERRIDES = {
    "NIL": 80,    # [v4.5] fair NO ~82c, default 75 blocks profitable fills
    "ELBO": 45,   # [v4.5b] cap NO bids
    "RECR": 45,   # [v4.5b] cap NO bids
    "ANKL": 40,   # [v4.5b] cap NO bids
}
# [v4.5b] Per-market YES MAX_PRICE overrides
YES_MAX_PRICE_OVERRIDES = {
    "RECR": 60,   # [v4.5b] cap YES bids
    "RECO": 60,   # [v4.5b] cap YES bids
    "ANKL": 55,   # [v4.5b] cap YES bids
}
YES_MIN_PRICE = 20
NO_MAX_YES_PRICE = 80
NO_MIN_YES_PRICE = 20

# =========================
# NO SWEET SPOT
# =========================
NO_SWEET_SPOT_MIN = 65
NO_SWEET_SPOT_MAX = 75
NO_SWEET_SPOT_MULTIPLIER = 1.5
MAX_COMBINED_SWEET_BOOST = 2.0

# =========================
# ORDER CONFIGURATION
# =========================
def generate_base_contracts(num_levels: int) -> List[int]:
    contracts = []
    for i in range(num_levels):
        if i < 3:
            contracts.append(15)
        elif i < 5:
            contracts.append(18)
        elif i < 8:
            contracts.append(22)
        else:
            contracts.append(25)
    return contracts

NUM_OFFSET_LEVELS = 12
BASE_YES_CONTRACTS = generate_base_contracts(NUM_OFFSET_LEVELS)
BASE_NO_CONTRACTS = generate_base_contracts(NUM_OFFSET_LEVELS)
MAX_CONTRACTS_PER_ORDER = 300
MAX_CONTRACTS_PER_MARKET_PER_RUN = 75

TIME_MULTIPLIERS = [
    (1, 1.8), (3, 1.5), (6, 1.3), (12, 1.1),
    (24, 1.0), (48, 0.9), (999999, 0.7),
]

VOLUME_MULTIPLIERS = [
    (50000, 1.8), (20000, 1.5), (10000, 1.3), (5000, 1.1),
    (2000, 1.0), (1000, 0.9), (0, 0.8),
]

def generate_offsets(start: int, increment: int, count: int) -> List[int]:
    return [start + i * increment for i in range(count)]

SPREAD_CONFIGS = {
    "tight":    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14],
    "medium":   [2, 3, 5, 7, 9, 11, 13, 15, 17, 19, 22, 25],
    "wide":     [3, 5, 8, 11, 14, 17, 20, 23, 26, 29, 33, 37],
}

TIER1_SPREAD_OVERRIDE = "tight"

PAIRING_OFFSETS = {
    'yes': generate_offsets(start=1, increment=1, count=NUM_OFFSET_LEVELS),
    'no':  generate_offsets(start=1, increment=1, count=NUM_OFFSET_LEVELS),
    'name': 'pairing'
}

EXPIRATION_HOURS_BEFORE_CLOSE = 4
SLEEP_BETWEEN_ORDERS = 0.05
FALLBACK_OFFSET = 15


# =========================
# TEAM-SPECIFIC OVERRIDE APPLICATION
# =========================

def apply_team_overrides(market_code, team_1, team_2, base_yes_mult, base_no_mult):
    override_1 = TEAM_MARKET_OVERRIDES.get((team_1, market_code))
    override_2 = TEAM_MARKET_OVERRIDES.get((team_2, market_code))
    if override_1 is None and override_2 is None:
        return base_yes_mult, base_no_mult, False, "no_override"
    yes_mult, no_mult = base_yes_mult, base_no_mult
    reasons = []
    if override_1 is not None:
        yes_mult = override_1["yes_mult"]
        no_mult = override_1["no_mult"]
        reasons.append(f"{team_1}->Y={yes_mult}x,N={no_mult}x")
    if override_2 is not None:
        if override_1 is not None:
            yes_mult = min(yes_mult, override_2["yes_mult"])
            no_mult = min(no_mult, override_2["no_mult"])
        else:
            yes_mult = override_2["yes_mult"]
            no_mult = override_2["no_mult"]
        reasons.append(f"{team_2}->Y={yes_mult}x,N={no_mult}x")
    return yes_mult, no_mult, True, "; ".join(reasons)

# =========================
# AUTHENTICATION (for order script)
# =========================
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)

exchange_client = ExchangeClient(
    exchange_api_base=API_BASE,
    key_id=API_KEY_ID,
    private_key=PRIVATE_KEY
)

print("Testing connection...")
try:
    status = exchange_client.get_exchange_status()
    print(f"✓ Connected! Trading active: {status['trading_active']}")
except Exception as e:
    print(f"✗ Connection failed: {e}")
    raise

print(f"\n{'='*70}")
print(f"CONFIGURATION — {MODEL_VERSION}")
print(f"{'='*70}")
print(f"Run ID: {RUN_ID}")
print(f"Series: {SERIES_TICKER}")
print(f"Pricing: {PRICING_STRATEGY} | Bayesian K={BAYESIAN_K}")
print(f"Position caps: market={MAX_NET_PER_MARKET} event={MAX_NET_PER_EVENT}")
print(f"  Moderate at {POSITION_MODERATE_THRESHOLD}, stop at {POSITION_STOP_THRESHOLD}")
print(f"Pairing: threshold={PAIRING_MODE_NET_FLOOR} aggressive={PAIRING_MODE_NET_AGGRESSIVE}")
print(f"Price filters:")
print(f"  YES: {MIN_PRICE_YES}-{MAX_PRICE}c floor={YES_MIN_PRICE}c (Kelly gate replaces prob_floor)")
print(f"  NO:  {MIN_PRICE_NO}-{MAX_PRICE}c sweet={NO_SWEET_SPOT_MIN}-{NO_SWEET_SPOT_MAX}c @{NO_SWEET_SPOT_MULTIPLIER}x")
print(f"  EV gate: {MIN_EV_PER_ORDER:.0%}")
blocked = [k for k, v in SIDE_MULTIPLIERS.items() if v['yes'] == 0 and v['no'] == 0]
safe = [k for k, v in SIDE_MULTIPLIERS.items() if v['no'] >= 1.5]
yes_favored = [k for k, v in SIDE_MULTIPLIERS.items() if v['yes'] >= 0.8]
print(f"BLOCKED: {blocked}")
print(f"SAFE BUNDLE (≥1.5x No): {safe}")
print(f"YES-FAVORED (≥0.8x Yes): {yes_favored}")
print(f"Team overrides: {len(TEAM_MARKET_OVERRIDES)} combos")
print(f"Base contracts: {BASE_YES_CONTRACTS[:5]}...")
print(f"{'='*70}\n")

# =====================================================================
# [v4.5] FETCH NCAAB GAME SCHEDULE FROM ODDS API
# =====================================================================
print(f"{'='*70}")
print("FETCHING NCAAB SCHEDULE (The Odds API)")
print(f"{'='*70}")
fetch_ncaab_schedule()
print()

# =====================================================================
# FILLS-BASED POSITION LEDGER
# =====================================================================

def fetch_fills_from_api(exchange_client_ref, series_ticker=SERIES_TICKER):
    print(f"  Fetching fills from Kalshi API for {series_ticker}...")
    all_fills = []
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            response = exchange_client_ref.get_fills(**params)
        except Exception as e:
            print(f"  ✗ Error on page {page}: {e}")
            break
        batch = response.get("fills", [])
        if not batch:
            break
        series_fills = [f for f in batch if f.get("ticker", "").startswith(series_ticker)]
        all_fills.extend(series_fills)
        cursor = response.get("cursor")
        if not cursor:
            break
        if page % 10 == 0:
            print(f"    Page {page}: {len(all_fills)} fills so far")

    if not all_fills:
        print(f"  No fills found for {series_ticker}")
        return pd.DataFrame()

    df = pd.DataFrame(all_fills)
    print(f"  ✓ Loaded {len(df)} fills across {df['ticker'].nunique()} markets from API")

    if 'contracts' not in df.columns:
        if 'count_fp' in df.columns:
            df['contracts'] = pd.to_numeric(df['count_fp'], errors='coerce').astype(int)
            print(f"  Mapped 'count_fp' -> 'contracts'")
        elif 'count' in df.columns:
            df = df.rename(columns={'count': 'contracts'})
            print(f"  Mapped 'count' -> 'contracts'")
        else:
            print(f"  ⚠️ No count column found! Columns: {list(df.columns)}")
            df['contracts'] = 0

    if 'yes_price' not in df.columns:
        if 'yes_price_dollars' in df.columns:
            df['yes_price'] = pd.to_numeric(df['yes_price_dollars'], errors='coerce')
            print(f"  Mapped 'yes_price_dollars' -> 'yes_price'")
        elif 'yes_price_fixed' in df.columns:
            df['yes_price'] = pd.to_numeric(df['yes_price_fixed'], errors='coerce')
            print(f"  Mapped 'yes_price_fixed' -> 'yes_price'")

    if 'yes_price' in df.columns:
        yes_prices = pd.to_numeric(df['yes_price'], errors='coerce')
        max_val = yes_prices.max()
        if max_val <= 1.0:
            df['price'] = df.apply(
                lambda row: float(row['yes_price']) if row.get('side') == 'yes'
                            else (1.0 - float(row['yes_price'])), axis=1)
        else:
            df['price'] = df.apply(
                lambda row: float(row['yes_price']) if row.get('side') == 'yes'
                            else (100.0 - float(row['yes_price'])), axis=1)
    elif 'no_price' in df.columns or 'no_price_dollars' in df.columns:
        no_col = 'no_price' if 'no_price' in df.columns else 'no_price_dollars'
        df['no_price_val'] = pd.to_numeric(df[no_col], errors='coerce')
        max_val = df['no_price_val'].max()
        if max_val <= 1.0:
            df['price'] = df.apply(
                lambda row: (1.0 - float(row['no_price_val'])) if row.get('side') == 'yes'
                            else float(row['no_price_val']), axis=1)
        else:
            df['price'] = df.apply(
                lambda row: (100.0 - float(row['no_price_val'])) if row.get('side') == 'yes'
                            else float(row['no_price_val']), axis=1)
        print(f"  Built 'price' from '{no_col}' (fallback)")
    else:
        print(f"  ⚠️ No price column found! Columns: {list(df.columns)}")
        df['price'] = 0

    return df


def fetch_fills_from_bq(bq_client, project_id, dataset_id, series_ticker=SERIES_TICKER, lookback_days=30):
    table_id = f"{project_id}.{dataset_id}.{series_ticker}_orders"
    query = f"""
    SELECT ticker, side, price, contracts, order_id, client_order_id, created_at,
           ev, p_hat, total_multiplier, spread_config, spread_cents,
           sweet_spot_applied, pricing_strategy, model_version, run_id
    FROM `{table_id}`
    WHERE ok = TRUE AND created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_days} DAY)
    ORDER BY ticker, created_at
    """
    try:
        df = bq_client.query(query).to_dataframe()
        print(f"  ✓ Loaded {len(df)} fills from BQ")
        return df
    except Exception as e:
        print(f"  ✗ BQ error: {e}")
        return pd.DataFrame()


def build_position_ledger(fills_df, price_col="price", contracts_col="contracts"):
    if fills_df is None or len(fills_df) == 0:
        return {}
    ledger = {}
    df = fills_df.copy()

    if price_col not in df.columns:
        for fallback in ['yes_price', 'yes_price_dollars', 'yes_price_fixed']:
            if fallback in df.columns:
                df[price_col] = pd.to_numeric(df[fallback], errors='coerce')
                print(f"  Ledger: mapped '{fallback}' -> '{price_col}'")
                break
    if contracts_col not in df.columns:
        for fallback in ['count_fp', 'count', 'filled_count']:
            if fallback in df.columns:
                df[contracts_col] = pd.to_numeric(df[fallback], errors='coerce').astype(int)
                print(f"  Ledger: mapped '{fallback}' -> '{contracts_col}'")
                break
    if price_col not in df.columns:
        print(f"  ⚠️ '{price_col}' not found. Columns: {list(df.columns)}")
        return {}
    if contracts_col not in df.columns:
        print(f"  ⚠️ '{contracts_col}' not found. Columns: {list(df.columns)}")
        return {}

    df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
    df[contracts_col] = pd.to_numeric(df[contracts_col], errors="coerce")
    df = df.dropna(subset=[price_col, contracts_col])

    max_price_val = df[price_col].max()
    if max_price_val <= 1.0:
        df[price_col] = df[price_col] * 100

    for ticker, group in df.groupby("ticker"):
        yes_fills = group[group["side"] == "yes"]
        no_fills = group[group["side"] == "no"]
        yes_qty = int(yes_fills[contracts_col].sum())
        no_qty = int(no_fills[contracts_col].sum())
        yes_avg = (yes_fills[price_col] * yes_fills[contracts_col]).sum() / yes_fills[contracts_col].sum() if yes_qty > 0 else 0.0
        no_avg = (no_fills[price_col] * no_fills[contracts_col]).sum() / no_fills[contracts_col].sum() if no_qty > 0 else 0.0
        paired_qty = min(yes_qty, no_qty)
        net_qty = abs(yes_qty - no_qty)
        net_side = "yes" if yes_qty > no_qty else ("no" if no_qty > yes_qty else "flat")
        paired_cost = yes_avg + no_avg if paired_qty > 0 else 0.0
        paired_edge = 100.0 - paired_cost if paired_qty > 0 else 0.0
        yes_total_cost = yes_avg * yes_qty
        no_total_cost = no_avg * no_qty
        profit_if_yes = (yes_qty * 100) - yes_total_cost - no_total_cost
        profit_if_no = (no_qty * 100) - yes_total_cost - no_total_cost
        worst_case = min(profit_if_yes, profit_if_no)
        ledger[ticker] = {
            "yes_qty": yes_qty, "no_qty": no_qty,
            "yes_avg_price": round(yes_avg, 2), "no_avg_price": round(no_avg, 2),
            "paired_qty": paired_qty, "net_qty": net_qty, "net_side": net_side,
            "paired_cost": round(paired_cost, 2), "paired_edge": round(paired_edge, 2),
            "yes_total_cost": round(yes_total_cost, 2), "no_total_cost": round(no_total_cost, 2),
            "profit_if_yes_cents": round(profit_if_yes, 2), "profit_if_no_cents": round(profit_if_no, 2),
            "worst_case_cents": round(worst_case, 2),
            "profit_if_yes": round(profit_if_yes / 100, 2),
            "profit_if_no": round(profit_if_no / 100, 2),
            "worst_case": round(worst_case / 100, 2),
            "paired_profit": round(paired_edge * paired_qty / 100, 2),
        }
    print(f"  ✓ Built ledger for {len(ledger)} markets")
    return ledger


def print_ledger_summary(ledger):
    if not ledger:
        print("  Ledger is empty")
        return
    print(f"\n{'='*70}")
    print("POSITION LEDGER SUMMARY")
    print(f"{'='*70}")
    total_paired = sum(v["paired_qty"] for v in ledger.values())
    total_net = sum(v["net_qty"] for v in ledger.values())
    total_paired_profit = sum(v["paired_profit"] for v in ledger.values())
    total_worst_case = sum(v["worst_case"] for v in ledger.values())
    print(f"  Markets: {len(ledger)} | Paired: {total_paired} | Net: {total_net}")
    if (total_paired + total_net) > 0:
        print(f"  Paired ratio: {total_paired/(total_paired+total_net)*100:.1f}%")
    print(f"  Guaranteed paired profit: ${total_paired_profit:.2f}")
    print(f"  Worst-case total P&L: ${total_worst_case:.2f}")
    sorted_by_profit = sorted(ledger.items(), key=lambda x: x[1]["paired_profit"], reverse=True)
    print(f"\n  TOP 5 BY PAIRED PROFIT:")
    for ticker, data in sorted_by_profit[:5]:
        short = ticker[-40:]
        print(f"    {short:<45} paired={data['paired_qty']} edge={data['paired_edge']:.1f}¢ profit=${data['paired_profit']:.2f}")
    print(f"{'='*70}\n")


def ledger_to_dataframe(ledger):
    if not ledger:
        return pd.DataFrame()
    rows = []
    for ticker, data in ledger.items():
        row = {"ticker": ticker, **data}
        parts = ticker.split("-")
        if len(parts) >= 3:
            row["market_code"] = parts[-1]
            row["event_ticker"] = "-".join(parts[:2])
        rows.append(row)
    return pd.DataFrame(rows).sort_values("paired_profit", ascending=False).reset_index(drop=True)


def integrate_ledger_into_run(exchange_client_ref, bq_client=None, project_id=None,
                               dataset_id=None, series_ticker=SERIES_TICKER,
                               use_api=True, use_bq=True, lookback_days=30):
    print(f"\n{'='*70}")
    print("BUILDING FILLS-BASED POSITION LEDGER")
    print(f"{'='*70}")
    fills_df = pd.DataFrame()
    if use_api:
        try:
            fills_df = fetch_fills_from_api(exchange_client_ref, series_ticker)
        except Exception as e:
            print(f"  ⚠️ API failed: {e}")
    if len(fills_df) == 0 and use_bq and bq_client is not None:
        try:
            fills_df = fetch_fills_from_bq(bq_client, project_id, dataset_id, series_ticker, lookback_days)
        except Exception as e:
            print(f"  ⚠️ BQ failed: {e}")
    if len(fills_df) == 0:
        print("  ⚠️ No fills available")
        return {}
    ledger = build_position_ledger(fills_df)
    print_ledger_summary(ledger)
    return ledger

# =====================================================================
# HEDGE-ADJUSTED EV
# =====================================================================
def calculate_hedge_ev(side, bid_price, ledger_data):
    if ledger_data is None:
        return 0.0, False
    net_side = ledger_data.get("net_side", "flat")
    net_qty = ledger_data.get("net_qty", 0)
    if net_qty == 0 or net_side == "flat" or net_side == side:
        return 0.0, False
    opposite_avg = ledger_data.get("yes_avg_price", 0.0) if side == "no" else ledger_data.get("no_avg_price", 0.0)
    if opposite_avg <= 0:
        return 0.0, False
    hedge_ev = (100.0 - opposite_avg - bid_price) / 100.0
    if hedge_ev <= 0:
        return 0.0, True
    return hedge_ev, True

# =====================================================================
# PAIRING MODE
# =====================================================================
def get_pairing_mode(ticker, ledger, net_position):
    ledger_data = ledger.get(ticker)
    net_qty = ledger_data.get("net_qty", 0) if ledger_data else abs(net_position)
    imbalance_ratio = net_qty / MAX_NET_PER_MARKET if MAX_NET_PER_MARKET > 0 else 0.0
    if net_qty >= PAIRING_MODE_NET_AGGRESSIVE:
        return "aggressive_pairing", imbalance_ratio
    elif net_qty >= PAIRING_MODE_NET_FLOOR:
        return "pairing", imbalance_ratio
    return "normal", imbalance_ratio

# =====================================================================
# SIDE MULTIPLIER OVERRIDE
# =====================================================================
def get_effective_side_multiplier(base_side_mult, side, pairing_mode, net_side):
    if pairing_mode == "normal" or net_side == side or net_side == "flat":
        return base_side_mult
    if pairing_mode == "aggressive_pairing":
        return max(base_side_mult, 1.5)
    elif pairing_mode == "pairing":
        return max(base_side_mult, 1.0)
    return base_side_mult

# =====================================================================
# LEDGER-AWARE POSITION MULTIPLIER
# =====================================================================
def calculate_ledger_position_multiplier(ticker, side, ledger, fallback_net_position, market_net_cap=None):
    cap = market_net_cap if market_net_cap is not None else MAX_NET_PER_MARKET
    data = ledger.get(ticker)
    if data is None:
        return calculate_position_multiplier(fallback_net_position, side), "no_ledger_data", 0.0
    paired_qty = data["paired_qty"]
    net_qty = data["net_qty"]
    net_side = data["net_side"]
    side_qty = data["yes_qty"] if side == "yes" else data["no_qty"]
    if side_qty >= cap:
        return 0.0, f"hard_cap_{side}_qty={side_qty}", 0.0
    if net_side == side and net_qty > 0:
        if net_qty >= cap:
            return 0.0, f"net_cap_{side}={net_qty}", 0.0
        elif net_qty >= cap * 0.7:
            remaining_frac = (cap - net_qty) / (cap * 0.3)
            return max(0.1, remaining_frac), f"net_rampdown_{side}={net_qty}", 0.0
        return 1.0, f"net_ok_{side}={net_qty}", 0.0
    elif net_side != side and net_side != "flat" and net_qty > 0:
        if net_qty >= PAIRING_MODE_NET_AGGRESSIVE:
            return 2.0, f"aggressive_pair_boost", 1.5
        elif net_qty >= PAIRING_MODE_NET_FLOOR:
            return 1.5, f"pair_boost", 1.0
        elif net_qty >= int(cap * 0.25):
            return 1.2, f"mild_pair_boost", 0.8
        return 1.0, "net_balanced", 0.0
    if paired_qty > cap * 0.6 and data["paired_edge"] > 3:
        return 0.6, f"diminishing_returns", 0.0
    return 1.0, "default", 0.0

def calculate_position_multiplier(net_position, side):
    abs_pos = abs(net_position)
    if net_position > 0:
        if side == 'yes':
            if abs_pos >= POSITION_STOP_THRESHOLD: return 0.0
            elif abs_pos >= POSITION_MODERATE_THRESHOLD:
                return 1.0 - (abs_pos - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
            return 1.0
        else:
            if abs_pos >= POSITION_STOP_THRESHOLD: return 2.0
            elif abs_pos >= POSITION_MODERATE_THRESHOLD:
                return 1.0 + (abs_pos - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
            return 1.0
    elif net_position < 0:
        if side == 'no':
            if abs_pos >= POSITION_STOP_THRESHOLD: return 0.0
            elif abs_pos >= POSITION_MODERATE_THRESHOLD:
                return 1.0 - (abs_pos - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
            return 1.0
        else:
            if abs_pos >= POSITION_STOP_THRESHOLD: return 2.0
            elif abs_pos >= POSITION_MODERATE_THRESHOLD:
                return 1.0 + (abs_pos - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
            return 1.0
    return 1.0

# =====================================================================
# CANCEL ORDERS + GET POSITIONS
# =====================================================================
def cancel_all_existing_orders_batch():
    print(f"\n{'='*70}")
    print(f"CANCELING ORDERS FOR {SERIES_TICKER}")
    print(f"{'='*70}")
    try:
        all_orders = []
        cursor = None
        page = 0
        while True:
            page += 1
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            response = exchange_client.get_orders(**params)
            batch = response.get('orders', [])
            all_orders.extend(batch)
            cursor = response.get('cursor')
            if not cursor: break
        series_orders = [o for o in all_orders if o.get('status') == 'resting' and o.get('ticker', '').startswith(SERIES_TICKER)]
        print(f"Found {len(series_orders)} resting {SERIES_TICKER} orders to cancel")
        if len(series_orders) == 0: return 0
        order_ids = [o['order_id'] for o in series_orders]
        canceled = 0
        for i in range(0, len(order_ids), 100):
            chunk = order_ids[i:i+100]
            try:
                if hasattr(exchange_client, 'cancel_orders'):
                    exchange_client.cancel_orders(order_ids=chunk)
                    canceled += len(chunk)
                else:
                    for oid in chunk:
                        try: exchange_client.cancel_order(order_id=oid); canceled += 1
                        except: pass
                time.sleep(0.1)
            except:
                for oid in chunk:
                    try: exchange_client.cancel_order(order_id=oid); canceled += 1
                    except: pass
        print(f"✓ Canceled {canceled}/{len(order_ids)} orders\n")
        return canceled
    except Exception as e:
        print(f"✗ Error: {e}\n")
        return 0


def get_existing_positions():
    print(f"\n{'='*70}")
    print(f"CHECKING EXISTING POSITIONS FOR {SERIES_TICKER}")
    print(f"{'='*70}")
    try:
        all_positions_raw = []
        cursor = None
        page = 0
        while True:
            page += 1
            params = {"limit": 1000}
            if cursor: params["cursor"] = cursor
            response = exchange_client.get_positions(**params)
            batch = response.get('market_positions', [])
            all_positions_raw.extend(batch)
            cursor = response.get('cursor')
            if not cursor: break
            time.sleep(0.05)
        positions = {}
        for pos in all_positions_raw:
            ticker = pos.get('ticker', '')
            if not ticker.startswith(SERIES_TICKER): continue
            net_position = pos.get('position', 0) or 0
            if net_position == 0: continue
            positions[ticker] = {'net_position': net_position}
        if positions:
            total_net = sum(abs(p['net_position']) for p in positions.values())
            print(f"✓ {len(positions)} markets with positions, {total_net} total net exposure")
        else:
            print("  No non-zero positions found")
        print()
        return positions, True
    except Exception as e:
        alert("SAFE_MODE", f"Position fetch failed: {e}")
        print(f"⚠️  Error: {e}")
        print(f"  ⚠️  SAFE MODE — {SAFE_MODE_MULTIPLIER:.0%} sizing\n")
        return {}, False

# =====================================================================
# UTILITY HELPERS + PRICING
# =====================================================================
def cents_from_rate(rate):
    if rate is None or pd.isna(rate): raise ValueError("rate is NaN")
    return int(round(float(rate) * 100))


def estimate_game_start_from_ticker(event_date_str):
    """Fallback: derive estimated game start from the event date in the ticker."""
    if not event_date_str:
        return None
    try:
        dt = datetime.strptime(str(event_date_str), "%Y-%m-%d")
        game_start = dt + timedelta(days=1, hours=0)
        return int(game_start.timestamp())
    except (ValueError, TypeError):
        return None

def get_market_details(market_ticker, team_1=None, team_2=None, event_date=None):
    """Fetch market details from Kalshi, then look up actual game start from Odds API.
    Returns 5-tuple: (expiration_ts, open_interest, volume, game_start_ts, time_source)
    """
    try:
        resp = exchange_client.get_market(ticker=market_ticker)
        md = resp.get('market', {})

        oi = md.get('open_interest')
        if oi is None or oi == 0:
            oi_fp = md.get('open_interest_fp')
            if oi_fp is not None:
                try:
                    oi = int(float(oi_fp))
                except (ValueError, TypeError):
                    oi = 0
        if oi is None:
            oi = 0

        vol = md.get('volume')
        if vol is None or vol == 0:
            vol_fp = md.get('volume_fp')
            if vol_fp is not None:
                try:
                    vol = int(float(vol_fp))
                except (ValueError, TypeError):
                    vol = 0
        if vol is None:
            vol = 0

        # Check manual game start overrides first (keyed by event_code)
        event_code = market_ticker.split("-")[1] if len(market_ticker.split("-")) > 1 else ""
        if event_code in MANUAL_GAME_START_OVERRIDES:
            game_start_ts = MANUAL_GAME_START_OVERRIDES[event_code]
            time_source = "manual_override"
        else:
            game_start_ts, time_source = get_game_start_ts(team_1, team_2, event_date)

        if game_start_ts is None:
            game_start_ts = estimate_game_start_from_ticker(event_date)
            if game_start_ts is not None:
                time_source = "ticker_estimate"
                alert("GAME_TIME_FALLBACK", f"Odds API miss, using ticker estimate",
                      {"ticker": market_ticker})

        expiration_ts = None
        if game_start_ts:
            if time_source in ("odds_api", "manual_override"):
                expiration_ts = game_start_ts - ODDS_API_EXPIRATION_BUFFER_SEC
            else:
                expiration_ts = game_start_ts - (EXPIRATION_HOURS_BEFORE_CLOSE * 3600)
            if expiration_ts <= time.time():
                expiration_ts = None

        return expiration_ts, oi, vol, game_start_ts, time_source
    except Exception as e:
        return None, None, None, None, "error"

def get_time_multiplier(hours):
    for threshold, mult in TIME_MULTIPLIERS:
        if hours < threshold: return mult
    return 0.5

def get_volume_multiplier(oi):
    for threshold, mult in VOLUME_MULTIPLIERS:
        if oi >= threshold: return mult
    return 0.4

def get_offsets_for_spread(spread_cents):
    if spread_cents < 5:
        name = "tight"
    elif spread_cents < 15:
        name = "medium"
    else:
        name = "wide"
    offsets = SPREAD_CONFIGS[name]
    return offsets, offsets, name

def calculate_spread(yes_bid, no_bid):
    return 100 - (yes_bid + no_bid)

def parse_orderbook(orderbook_data):
    """Parse orderbook data. Handles both formats:
    - Legacy: [[42, 13], [40, 5]]  (int cents, int contracts)
    - New fp: [["0.4200", "13.00"], ["0.4000", "5.00"]]  (dollar strings)
    Returns (best_bid_cents, best_bid_size) or (None, None).
    """
    if not orderbook_data: return None, None
    sample = orderbook_data[0][0]
    if isinstance(sample, str):
        parsed = [(int(round(float(level[0]) * 100)), int(float(level[1]))) for level in orderbook_data]
    else:
        parsed = [(int(level[0]), int(level[1])) for level in orderbook_data]
    if not parsed: return None, None
    best_bid = max(p[0] for p in parsed)
    best_size = next(p[1] for p in parsed if p[0] == best_bid)
    return best_bid, best_size

# BAYESIAN HYBRID PRICING
def calculate_hybrid_price_bayesian(market_mid, market_yes_rate, market_sample_size,
                                     team_1_yes_rate, team_1_sample_size,
                                     team_2_yes_rate, team_2_sample_size):
    intermediates = {}
    market_mid_rate = market_mid / 100
    if market_yes_rate is None or pd.isna(market_yes_rate):
        return market_mid_rate, {"fallback": "no_market_yes_rate"}
    team_rates, team_weights = [], []
    if team_1_sample_size > 0 and team_1_yes_rate is not None and not pd.isna(team_1_yes_rate):
        team_rates.append(team_1_yes_rate); team_weights.append(team_1_sample_size)
    if team_2_sample_size > 0 and team_2_yes_rate is not None and not pd.isna(team_2_yes_rate):
        team_rates.append(team_2_yes_rate); team_weights.append(team_2_sample_size)
    if team_rates:
        ttw = sum(team_weights)
        avg_team = sum(r*w for r,w in zip(team_rates, team_weights))/ttw
        combined_n = ttw
    else:
        avg_team = market_yes_rate; combined_n = 0
    cred = min(combined_n / BAYESIAN_K, 1.0) if combined_n > 0 else 0.0
    posterior = (1-cred)*market_yes_rate + cred*avg_team if combined_n > 0 else market_yes_rate
    n = max(market_sample_size, 0)
    recency = 2 ** (-n / BAYESIAN_RECENCY_HALFLIFE_GAMES)
    hw = BAYESIAN_HISTORICAL_WEIGHT * (1.0 - 0.5*(1.0-recency))
    mw = BAYESIAN_MARKET_WEIGHT
    tw = mw + hw; mw /= tw; hw /= tw
    hybrid_yes = mw * market_mid_rate + hw * posterior
    return hybrid_yes, intermediates

def calculate_bid_price(market_mid_yes, market_yes_rate, team_1_yes_rate, team_1_sample_size,
                        team_2_yes_rate, team_2_sample_size, market_sample_size, strategy):
    if strategy == 'hybrid':
        if market_mid_yes is None:
            if market_yes_rate is None or pd.isna(market_yes_rate): return None, None, {}
            yp = cents_from_rate(market_yes_rate)
            return yp, 100-yp, {"fallback": "no_orderbook"}
        hyb, inter = calculate_hybrid_price_bayesian(
            market_mid_yes, market_yes_rate, market_sample_size,
            team_1_yes_rate, team_1_sample_size, team_2_yes_rate, team_2_sample_size)
        yp = int(hyb * 100)
        return yp, 100-yp, inter
    elif strategy == 'market':
        if market_mid_yes is not None:
            yp = int(round(market_mid_yes))
            return yp, 100-yp, {}
        return None, None, {}
    elif strategy == 'historical':
        if market_yes_rate is None or pd.isna(market_yes_rate): return None, None, {}
        yp = cents_from_rate(market_yes_rate)
        return yp, 100-yp, {}
    raise ValueError(f"Unknown strategy: {strategy}")

market_snapshots = []

# =====================================================================
# BUILD ORDERS
# =====================================================================

def build_order_objects_for_market(market_row, existing_positions, event_net_exposure,
                                    event_orders_placed, safe_mode, ledger):
    ticker = market_row.get("market_ticker")
    market_status = market_row.get("market_status")
    market_yes_rate = market_row.get("market_yes_rate")
    market_sample_size = market_row.get("market_sample_size", 0)
    team_1_yes_rate = market_row.get("team_1_yes_rate")
    team_1_sample_size = market_row.get("team_1_sample_size", 0)
    team_2_yes_rate = market_row.get("team_2_yes_rate")
    team_2_sample_size = market_row.get("team_2_sample_size", 0)
    event_ticker = market_row.get("event_ticker")
    ticker_part_3_market_code = market_row.get("ticker_part_3_market_code")
    team_1 = market_row.get("team_1")
    team_2 = market_row.get("team_2")

    effective_max_net = MAX_NET_PER_MARKET if ticker_part_3_market_code in TIER1_MARKETS else TIER2_MAX_NET

    if market_status != "active":
        return []

    side_config = SIDE_MULTIPLIERS.get(ticker_part_3_market_code, {"yes": 0.0, "no": 0.0})
    yes_side_mult_base = side_config.get("yes", 1.0)
    no_side_mult_base = side_config.get("no", 1.0)

    # Log side multipliers
    mult_flags = []
    if yes_side_mult_base > 0: mult_flags.append(f"{yes_side_mult_base:.1f}x YES")
    if no_side_mult_base > 0: mult_flags.append(f"{no_side_mult_base:.1f}x NO")
    if mult_flags:
        print(f"    📉 {' | '.join(mult_flags)} for {ticker_part_3_market_code}")

    # TEAM-SPECIFIC OVERRIDES
    team_yes, team_no, override_applied, override_reason = apply_team_overrides(
        market_code=ticker_part_3_market_code,
        team_1=team_1 or "", team_2=team_2 or "",
        base_yes_mult=yes_side_mult_base, base_no_mult=no_side_mult_base,
    )
    if override_applied:
        print(f"    🏀 [NCAAB-7] Team override: {override_reason}")
        print(f"       Base: Y={yes_side_mult_base}x N={no_side_mult_base}x → Override: Y={team_yes}x N={team_no}x")
        yes_side_mult_base = team_yes
        no_side_mult_base = team_no

    # [P2] Game-level risk multiplier
    game_risk = 1.0
    for team in [team_1 or "", team_2 or ""]:
        if team in TEAM_RISK_MULTIPLIER:
            game_risk = min(game_risk, TEAM_RISK_MULTIPLIER[team])
    if game_risk != 1.0:
        yes_side_mult_base *= game_risk
        no_side_mult_base *= game_risk
        print(f"    🎯 [P2] Game risk multiplier: {game_risk:.1f}x (teams: {team_1}, {team_2})")

    if yes_side_mult_base == 0.0 and no_side_mult_base == 0.0:
        reason = f"team override ({override_reason})" if override_applied else "config"
        print(f"    ⛔ BOTH SIDES 0x for {ticker_part_3_market_code} — skipping ({reason})")
        return []

    # Pairing mode
    position_data = existing_positions.get(ticker, {'net_position': 0})
    net_position = position_data['net_position']
    ledger_data = ledger.get(ticker)
    pairing_mode_str, imbalance_ratio = get_pairing_mode(ticker, ledger, net_position)
    net_side = ledger_data.get("net_side", "flat") if ledger_data else ("yes" if net_position > 0 else ("no" if net_position < 0 else "flat"))

    if pairing_mode_str != "normal":
        print(f"    🔄 PAIRING: {pairing_mode_str.upper()} (imbalance: {imbalance_ratio:.0%}, net: {net_side})")

    yes_side_mult = yes_side_mult_base
    no_side_mult = no_side_mult_base

    # Market details — [v4.5] returns game start time from Odds API
    expiration_ts, open_interest, volume, game_start_ts, time_source = get_market_details(
        ticker, team_1, team_2, market_row.get("event_date"))
    if expiration_ts is None:
        print(f"    ✗ No expiration — skipping")
        return []
    if game_start_ts:
        hours_until_event = (game_start_ts - time.time()) / 3600.0
    else:
        hours_until_event = (expiration_ts - time.time()) / 3600.0
    if game_start_ts:
        game_time_str = datetime.fromtimestamp(game_start_ts, tz=UTC).strftime('%H:%M UTC')
    else:
        game_time_str = "unknown"
    exp_time_str = datetime.fromtimestamp(expiration_ts, tz=UTC).strftime('%H:%M UTC')
    print(f"    ⏰ [{time_source}] Game: {game_time_str} | Exp: {exp_time_str}")

    if open_interest is None: open_interest = 0
    if volume is None: volume = 0

    # Ledger position multipliers
    yes_side_mult_floor, no_side_mult_floor = 0.0, 0.0
    if ledger:
        yes_position_mult, yes_pos_reason, yes_side_mult_floor = calculate_ledger_position_multiplier(ticker, 'yes', ledger, net_position, effective_max_net)
        no_position_mult, no_pos_reason, no_side_mult_floor = calculate_ledger_position_multiplier(ticker, 'no', ledger, net_position, effective_max_net)
    else:
        yes_position_mult = calculate_position_multiplier(net_position, 'yes')
        no_position_mult = calculate_position_multiplier(net_position, 'no')
        yes_pos_reason = "no_ledger_data"
        no_pos_reason = "no_ledger_data"

    # Log ledger sizing
    print(f"    Ledger sizing: YES={yes_position_mult:.2f}x ({yes_pos_reason}) NO={no_position_mult:.2f}x ({no_pos_reason})")

    if yes_side_mult_floor > 0 and yes_side_mult < yes_side_mult_floor:
        yes_side_mult = yes_side_mult_floor
    if no_side_mult_floor > 0 and no_side_mult < no_side_mult_floor:
        no_side_mult = no_side_mult_floor
    yes_side_mult = get_effective_side_multiplier(yes_side_mult, "yes", pairing_mode_str, net_side)
    no_side_mult = get_effective_side_multiplier(no_side_mult, "no", pairing_mode_str, net_side)

    ledger_net_qty = 0
    ledger_net_side_str = "flat"
    if ledger_data:
        ledger_net_qty = ledger_data.get("net_qty", 0)
        ledger_net_side_str = ledger_data.get("net_side", "flat")
        ledger_yes_qty = ledger_data.get("yes_qty", 0)
        ledger_no_qty = ledger_data.get("no_qty", 0)
    else:
        ledger_yes_qty = max(0, net_position) if net_position > 0 else 0
        ledger_no_qty = max(0, -net_position) if net_position < 0 else 0

    effective_yes_pos = max(net_position, 0) if net_position > 0 else 0
    effective_no_pos = max(-net_position, 0) if net_position < 0 else 0
    net_room_yes = max(0, effective_max_net - max(effective_yes_pos, ledger_yes_qty))
    net_room_no = max(0, effective_max_net - max(effective_no_pos, ledger_no_qty))
    if ledger_data and (ledger_yes_qty > effective_max_net * 0.8 or ledger_no_qty > effective_max_net * 0.8):
        print(f"    📊 Ledger: Yes={ledger_yes_qty} No={ledger_no_qty} cap={effective_max_net} room_y={net_room_yes} room_n={net_room_no}")
    time_mult = get_time_multiplier(hours_until_event)
    volume_mult = get_volume_multiplier(open_interest)
    base_mult = time_mult * volume_mult
    safe_mult = SAFE_MODE_MULTIPLIER if safe_mode else 1.0
    yes_total_mult = base_mult * yes_position_mult * yes_side_mult * safe_mult
    no_total_mult = base_mult * no_position_mult * no_side_mult * safe_mult

    print(f"    Time: {hours_until_event:.1f}h ({time_mult:.2f}x) | OI: {open_interest} ({volume_mult:.2f}x) | Base: {base_mult:.2f}x")

    # Orderbook
    orderbook_yes_bid, yes_bid_size, orderbook_no_bid, no_bid_size = None, None, None, None
    try:
        ob_resp = exchange_client.get_orderbook(ticker=ticker, depth=10)
        # New format: orderbook_fp.yes_dollars/no_dollars (March 2026 migration)
        ob_fp = ob_resp.get('orderbook_fp', {})
        ob_legacy = ob_resp.get('orderbook', ob_resp)
        yes_data = ob_fp.get('yes_dollars', []) or ob_legacy.get('yes', [])
        no_data = ob_fp.get('no_dollars', []) or ob_legacy.get('no', [])
        if yes_data: orderbook_yes_bid, yes_bid_size = parse_orderbook(yes_data)
        if no_data: orderbook_no_bid, no_bid_size = parse_orderbook(no_data)
    except Exception as e:
        alert("ORDERBOOK_ERROR", f"Orderbook fetch failed: {e}", {"ticker": ticker})

    # Log orderbook
    ob_yes_str = f"{orderbook_yes_bid}¢" if orderbook_yes_bid is not None else "N/A"
    ob_no_str = f"{orderbook_no_bid}¢" if orderbook_no_bid is not None else "N/A"
    print(f"    Orderbook: Yes={ob_yes_str}, No={ob_no_str}")

    market_mid_yes = None
    if orderbook_yes_bid is not None and orderbook_no_bid is not None:
        market_mid_yes = (orderbook_yes_bid + (100 - orderbook_no_bid)) / 2.0
        print(f"    Mid-price: ({orderbook_yes_bid} + {100 - orderbook_no_bid}) / 2 = {market_mid_yes:.1f}¢")
    elif orderbook_yes_bid is not None: market_mid_yes = float(orderbook_yes_bid)
    elif orderbook_no_bid is not None: market_mid_yes = 100.0 - orderbook_no_bid

    # [v4.5] Aggressive markets use pure orderbook mid (no historical blend)
    effective_strategy = 'market' if ticker_part_3_market_code in AGGRESSIVE_MARKET_PRICING else PRICING_STRATEGY

    yes_fair, no_fair, bayesian_inter = calculate_bid_price(
        market_mid_yes, market_yes_rate, team_1_yes_rate, team_1_sample_size,
        team_2_yes_rate, team_2_sample_size, market_sample_size, effective_strategy)

    # Log pricing strategy
    myr_str = f"{market_yes_rate*100:.1f}%" if market_yes_rate is not None and not pd.isna(market_yes_rate) else "N/A"
    ms_str = f"n={market_sample_size:.0f}" if market_sample_size else ""
    t1_str = f"{team_1}={team_1_yes_rate*100:.1f}%(n={team_1_sample_size:.0f})" if team_1_yes_rate is not None and not pd.isna(team_1_yes_rate) and team_1_sample_size > 0 else ""
    t2_str = f"{team_2}={team_2_yes_rate*100:.1f}%(n={team_2_sample_size:.0f})" if team_2_yes_rate is not None and not pd.isna(team_2_yes_rate) and team_2_sample_size > 0 else ""
    teams_str = ", ".join(filter(None, [t1_str, t2_str]))
    strategy_label = f"{effective_strategy}" + (" ⚡AGGRESSIVE" if effective_strategy == 'market' and PRICING_STRATEGY != 'market' else "")
    print(f"    Strategy [{strategy_label}]: Market={myr_str} ({ms_str}) Teams: {teams_str or 'none'}")

    if yes_fair is not None and no_fair is not None:
        print(f"    → Fair value YES={yes_fair}¢, NO={no_fair}¢ (sum={yes_fair+no_fair}¢)")

    if yes_fair is None and market_yes_rate is not None and not pd.isna(market_yes_rate):
        yes_fair = cents_from_rate(market_yes_rate)
        no_fair = 100 - yes_fair
    if yes_fair is None or no_fair is None:
        if market_mid_yes is not None:
            yes_fair = int(round(market_mid_yes))
            no_fair = 100 - yes_fair
            print(f"    ⚠️ No historical rate — using orderbook mid: YES={yes_fair}¢ NO={no_fair}¢")
        else:
            yes_fair = 50
            no_fair = 50
            print(f"    ⚠️ No data at all — using 50/50 prior. Kelly gate will filter.")
    p_hat = yes_fair / 100.0

    print(f"    Mults: YES={yes_total_mult:.2f}x NO={no_total_mult:.2f}x")

    # Price floors
    if pairing_mode_str == "aggressive_pairing":
        act_min_yes, act_min_no, act_yes_min = HEDGE_MIN_PRICE_YES, HEDGE_MIN_PRICE_NO, HEDGE_YES_MIN_PRICE
    elif pairing_mode_str == "pairing":
        act_min_yes = (MIN_PRICE_YES + HEDGE_MIN_PRICE_YES) // 2
        act_min_no = (MIN_PRICE_NO + HEDGE_MIN_PRICE_NO) // 2
        act_yes_min = (YES_MIN_PRICE + HEDGE_YES_MIN_PRICE) // 2
    else:
        act_min_yes, act_min_no, act_yes_min = MIN_PRICE_YES, MIN_PRICE_NO, YES_MIN_PRICE

    # Spread + offsets
    spread_cents = calculate_spread(orderbook_yes_bid or yes_fair, orderbook_no_bid or no_fair)
    yes_offsets, no_offsets, spread_config_name = get_offsets_for_spread(spread_cents)
    ob_sum = (orderbook_yes_bid or yes_fair) + (orderbook_no_bid or no_fair)
    print(f"    Spread: {spread_cents}¢ (yes={orderbook_yes_bid or yes_fair} + no={orderbook_no_bid or no_fair} = {ob_sum})")
    if ticker_part_3_market_code in TIER1_MARKETS:
        if TIER1_SPREAD_OVERRIDE in SPREAD_CONFIGS:
            no_offsets = SPREAD_CONFIGS[TIER1_SPREAD_OVERRIDE]
            spread_config_name = TIER1_SPREAD_OVERRIDE + "_tier1_no"
    # [v4.5] Top NO markets: pure 1¢ increments for maximum fill rate
    if ticker_part_3_market_code in TOP_NO_MARKETS:
        # [v4.5] Aggressive pricing: start at 0 (bid AT fair value)
        if ticker_part_3_market_code in AGGRESSIVE_MARKET_PRICING:
            no_offsets = generate_offsets(start=0, increment=1, count=NUM_OFFSET_LEVELS)
            spread_config_name = "aggressive_0c"
        else:
            no_offsets = generate_offsets(start=1, increment=1, count=NUM_OFFSET_LEVELS)
            spread_config_name = "top_no_1c"
    yes_offsets = SPREAD_CONFIGS["wide"]
    if pairing_mode_str != "normal" and net_side != "flat":
        if net_side == "yes": no_offsets = PAIRING_OFFSETS['no']; spread_config_name += "+pair_no"
        elif net_side == "no": yes_offsets = PAIRING_OFFSETS['yes']; spread_config_name += "+pair_yes"

    evt = event_ticker or ""
    current_event_orders = event_orders_placed.get(evt, 0)
    event_orders_remaining = MAX_ORDERS_PER_EVENT - current_event_orders
    if event_orders_remaining <= 0:
        print(f"    ⛔ Event cap reached — skipping")
        return []

    market_snapshots.append({"market_ticker": ticker, "run_id": RUN_ID, "model_version": MODEL_VERSION,
        "yes_fair": yes_fair, "no_fair": no_fair, "pairing_mode": pairing_mode_str,
        "time_source": time_source})

    orders = []

    # === YES LIMIT ORDERS ===
    # [v4.5b] Per-market YES MAX_PRICE override
    yes_market_max_price = YES_MAX_PRICE_OVERRIDES.get(ticker_part_3_market_code, MAX_PRICE)
    max_yes_price = (orderbook_yes_bid + MAX_ORDERBOOK_LEVELS_ABOVE) if orderbook_yes_bid else yes_market_max_price
    yes_placed = 0
    no_placed = 0
    if yes_total_mult > 0:
        for i, offset in enumerate(yes_offsets):
            if i >= len(BASE_YES_CONTRACTS): break
            bid = yes_fair - offset
            if bid > max_yes_price or bid < act_min_yes or bid > yes_market_max_price or bid < act_yes_min: continue
            standalone_ev = p_hat - (bid / 100.0)
            hedge_ev, is_pairing = calculate_hedge_ev("yes", bid, ledger_data)
            effective_ev = max(standalone_ev, hedge_ev) if is_pairing else standalone_ev
            if effective_ev < MIN_EV_PER_ORDER - 1e-9: continue  # [v4.5] float-safe
            if bid < 100:
                implied_kelly_yes = (p_hat - (bid / 100)) / (1 - bid / 100)
                if implied_kelly_yes < 0.02:
                    if i == 0: print(f"      Kelly blocked YES @{bid}¢ (kelly={implied_kelly_yes:.3f})")
                    continue
            base_c = BASE_YES_CONTRACTS[i]
            scaled = int(base_c * yes_total_mult)
            game_no_so_far = event_orders_placed.get(evt + "_no", 0)
            dampener = 1.0 / (1 + game_no_so_far / 400)
            scaled = int(scaled * dampener)
            max_here = min(scaled, MAX_CONTRACTS_PER_ORDER, max(0, net_room_yes - yes_placed), max(0, event_orders_remaining - yes_placed), max(0, MAX_CONTRACTS_PER_MARKET_PER_RUN - yes_placed - no_placed))
            if max_here < 1: continue
            yes_placed += max_here
            orders.append({
                "ticker": ticker, "action": "buy", "side": "yes", "count": max_here,
                "type": "limit", "yes_price": bid, "no_price": None,
                "expiration_ts": expiration_ts, "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "run_id": RUN_ID, "model_version": MODEL_VERSION,
                "spread_config": spread_config_name, "spread_cents": spread_cents,
                "time_multiplier": time_mult, "volume_multiplier": volume_mult,
                "position_multiplier": yes_position_mult, "side_multiplier": yes_side_mult,
                "boost_multiplier": yes_side_mult, "total_multiplier": yes_total_mult,
                "base_contracts": base_c, "hours_until_event": hours_until_event,
                "open_interest": open_interest, "net_position": net_position,
                "ev": round(effective_ev, 4), "standalone_ev": round(standalone_ev, 4),
                "hedge_ev": round(hedge_ev, 4) if is_pairing else None,
                "is_pairing_order": is_pairing, "pairing_mode": pairing_mode_str,
                "p_hat": round(p_hat, 4), "pricing_strategy": effective_strategy,
                "safe_mode": safe_mode, "event_ticker": event_ticker,
            })

    # === NO LIMIT ORDERS ===
    # [v4.5] Per-market MAX_PRICE override
    market_max_price = MAX_PRICE_OVERRIDES.get(ticker_part_3_market_code, MAX_PRICE)
    max_no_price = (orderbook_no_bid + MAX_ORDERBOOK_LEVELS_ABOVE) if orderbook_no_bid else market_max_price
    if no_total_mult > 0:
        for i, offset in enumerate(no_offsets):
            if i >= len(BASE_NO_CONTRACTS): break
            bid = no_fair - offset
            if bid > max_no_price or bid < act_min_no or bid > market_max_price: continue
            implied_yes = 100 - bid
            if implied_yes < NO_MIN_YES_PRICE or implied_yes > NO_MAX_YES_PRICE: continue
            standalone_ev = (1.0 - p_hat) - (bid / 100.0)
            hedge_ev, is_pairing = calculate_hedge_ev("no", bid, ledger_data)
            effective_ev = max(standalone_ev, hedge_ev) if is_pairing else standalone_ev
            if effective_ev < MIN_EV_PER_ORDER - 1e-9: continue  # [v4.5] float-safe
            if bid < 100:
                implied_kelly = ((1 - p_hat) - (bid / 100)) / (1 - bid / 100)
                if implied_kelly < 0.02:
                    if i == 0: print(f"      Kelly blocked NO @{bid}¢ (kelly={implied_kelly:.3f})")
                    continue
            base_c = BASE_NO_CONTRACTS[i]
            scaled = int(base_c * no_total_mult)
            game_no_so_far = event_orders_placed.get(evt + "_no", 0)
            dampener = 1.0 / (1 + game_no_so_far / 300)
            scaled = int(scaled * dampener)
            sweet_applied = False
            if NO_SWEET_SPOT_MIN <= bid <= NO_SWEET_SPOT_MAX:
                effective_sweet = min(NO_SWEET_SPOT_MULTIPLIER, MAX_COMBINED_SWEET_BOOST / max(no_side_mult, 0.1))
                scaled = int(scaled * effective_sweet); sweet_applied = True
            max_here = min(scaled, MAX_CONTRACTS_PER_ORDER, max(0, net_room_no - no_placed),
                          max(0, event_orders_remaining - yes_placed - no_placed), max(0, MAX_CONTRACTS_PER_MARKET_PER_RUN - yes_placed - no_placed))
            if max_here < 1: continue
            no_placed += max_here
            orders.append({
                "ticker": ticker, "action": "buy", "side": "no", "count": max_here,
                "type": "limit", "yes_price": None, "no_price": bid,
                "expiration_ts": expiration_ts, "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "run_id": RUN_ID, "model_version": MODEL_VERSION,
                "spread_config": spread_config_name, "spread_cents": spread_cents,
                "time_multiplier": time_mult, "volume_multiplier": volume_mult,
                "position_multiplier": no_position_mult, "side_multiplier": no_side_mult,
                "boost_multiplier": no_side_mult, "total_multiplier": no_total_mult,
                "sweet_spot_applied": sweet_applied, "base_contracts": base_c,
                "hours_until_event": hours_until_event, "open_interest": open_interest,
                "net_position": net_position,
                "ev": round(effective_ev, 4), "standalone_ev": round(standalone_ev, 4),
                "hedge_ev": round(hedge_ev, 4) if is_pairing else None,
                "is_pairing_order": is_pairing, "pairing_mode": pairing_mode_str,
                "p_hat": round(p_hat, 4), "pricing_strategy": effective_strategy,
                "safe_mode": safe_mode, "event_ticker": event_ticker,
            })

    # Per-order detail summary
    yes_orders = [o for o in orders if o['side'] == 'yes']
    no_orders = [o for o in orders if o['side'] == 'no']
    if yes_orders:
        details = [f"{o['yes_price']}¢({o['count']}c,ev={o.get('ev',0)*100:.0f}%{'H' if o.get('is_pairing_order') else 'S'})" for o in yes_orders[:6]]
        more = f", ... +{len(yes_orders)-6} more" if len(yes_orders) > 6 else ""
        print(f"    Spread config: [{spread_config_name}]")
        print(f"    → YES: [{', '.join(details)}{more}] (mult: {yes_total_mult:.2f}x, placed: {yes_placed})")
    if no_orders:
        details = [f"{o['no_price']}¢({o['count']}c,ev={o.get('ev',0)*100:.0f}%{'H' if o.get('is_pairing_order') else 'S'})" for o in no_orders[:6]]
        more = f", ... +{len(no_orders)-6} more" if len(no_orders) > 6 else ""
        if not yes_orders:
            print(f"    Spread config: [{spread_config_name}]")
        print(f"    → NO:  [{', '.join(details)}{more}] (mult: {no_total_mult:.2f}x, placed: {no_placed})")
    if not yes_orders and not no_orders:
        print(f"    → No orders passed filters")

    evt = event_ticker or ""
    event_orders_placed[evt] = event_orders_placed.get(evt, 0) + yes_placed + no_placed
    event_orders_placed[evt + "_no"] = event_orders_placed.get(evt + "_no", 0) + no_placed
    event_net_exposure[evt] = event_net_exposure.get(evt, 0) + abs(yes_placed - no_placed)
    return orders


# =====================================================================
# SUBMIT ORDERS + MAIN RUNNER
# =====================================================================
def submit_orders_sequential(orders):
    results = []
    total = len(orders)
    print(f"Submitting {total} orders...")
    start = time.time()
    for i, op in enumerate(orders):
        if i % 50 == 0 and i > 0:
            elapsed = time.time() - start
            print(f"  Progress: {i}/{total} ({i*100//total}%)")
        metadata = {k: op.pop(k) for k in [
            "run_id","model_version","spread_config","spread_cents","time_multiplier",
            "volume_multiplier","position_multiplier","side_multiplier","boost_multiplier",
            "total_multiplier","base_contracts","hours_until_event","open_interest",
            "net_position","ev","standalone_ev","hedge_ev","is_pairing_order","pairing_mode",
            "p_hat","pricing_strategy","safe_mode","event_ticker",
        ] if k in op}
        metadata["sweet_spot_applied"] = op.pop("sweet_spot_applied", False)
        ts = datetime.now(UTC).isoformat()
        try:
            response = exchange_client.create_order(**op)
            results.append({"ok": True, "ticker": op["ticker"], "side": op["side"],
                "price": op.get("yes_price") or op.get("no_price"), "contracts": op["count"],
                "order_id": response.get("order",{}).get("order_id"),
                "error": None, "created_at": ts, **metadata})
        except Exception as e:
            _err_str = str(e)
            # Log order failures (no alert/text — expected for closed markets and rate limits)
            if '400' in _err_str:
                print(f"    ⚠️ ORDER_400: {op['ticker']} {op['side']}@{op.get('yes_price') or op.get('no_price')}c ({_err_str[:150]})")
            elif '429' in _err_str:
                print(f"    ⚠️ ORDER_429: Rate limited on {op['ticker']} ({_err_str[:150]})")
            results.append({"ok": False, "ticker": op["ticker"], "side": op["side"],
                "price": op.get("yes_price") or op.get("no_price"), "contracts": op["count"],
                "error": _err_str, "order_id": None, "created_at": ts, **metadata})
        time.sleep(SLEEP_BETWEEN_ORDERS)
    elapsed = time.time() - start
    print(f"✓ Completed in {elapsed:.1f}s\n")
    return results


def place_orders_from_df(df_results):
    cancel_all_existing_orders_batch()
    existing_positions, positions_reliable = get_existing_positions()
    safe_mode = not positions_reliable

    try:
        bq_ref = client if 'client' in dir() else None
        proj_ref = PROJECT_ID if 'PROJECT_ID' in dir() else None
        ds_ref = DATASET_ID if 'DATASET_ID' in dir() else None
    except:
        bq_ref, proj_ref, ds_ref = None, None, None

    ledger = integrate_ledger_into_run(
        exchange_client, bq_ref, proj_ref, ds_ref, SERIES_TICKER, True, bq_ref is not None, 30)

    all_orders = []
    event_net_exposure = {}
    for ticker, pos_data in existing_positions.items():
        parts = ticker.split("-")
        evt = "-".join(parts[:2]) if len(parts) >= 2 else ticker
        event_net_exposure[evt] = event_net_exposure.get(evt, 0) + abs(pos_data['net_position'])
    event_orders_placed = {}

    if 'df_summary' in globals() and len(df_summary) > 0:
        summary_data = df_summary[['ticker_part_3_market_code','yes_rate','count_occurrences']].copy()
        summary_data = summary_data.rename(columns={'yes_rate':'market_yes_rate','count_occurrences':'market_sample_size'})
        df_merged = df_results.merge(summary_data, on='ticker_part_3_market_code', how='left')
    else:
        df_merged = df_results.copy()
        df_merged['market_yes_rate'] = None; df_merged['market_sample_size'] = 0

    if 'df_summary_rolling_by_team' in globals() and len(df_summary_rolling_by_team) > 0:
        team_latest = (
            df_summary_rolling_by_team
            .sort_values(['ticker_part_3_market_code','team','event_date'], ascending=[True,True,False])
            .groupby(['ticker_part_3_market_code','team'], as_index=False).first()
            [['ticker_part_3_market_code','team','yes_rate_rolling','count_occurrences_rolling']]
        )
        t1 = team_latest.copy().rename(columns={'team':'team_1','yes_rate_rolling':'team_1_yes_rate','count_occurrences_rolling':'team_1_sample_size'})
        t2 = team_latest.copy().rename(columns={'team':'team_2','yes_rate_rolling':'team_2_yes_rate','count_occurrences_rolling':'team_2_sample_size'})
        df_merged = df_merged.merge(t1, on=['ticker_part_3_market_code','team_1'], how='left')
        df_merged = df_merged.merge(t2, on=['ticker_part_3_market_code','team_2'], how='left')
    else:
        df_merged['team_1_yes_rate'] = None; df_merged['team_1_sample_size'] = 0
        df_merged['team_2_yes_rate'] = None; df_merged['team_2_sample_size'] = 0

    for _, row in df_merged.iterrows():
        if row.get("market_status") != "active": continue
        print(f"\n{row.get('market_ticker')}:")
        orders = build_order_objects_for_market(row.to_dict(), existing_positions,
            event_net_exposure, event_orders_placed, safe_mode, ledger)
        all_orders.extend(orders)

    print(f"\n{'='*70}")
    print(f"PREPARED {len(all_orders)} POST-ONLY LIMIT ORDERS ({MODEL_VERSION})")
    print(f"{'='*70}\n")

    if len(all_orders) == 0:
        print("No orders to place.")
        return pd.DataFrame(), pd.DataFrame(), ledger

    results = submit_orders_sequential(all_orders)
    global df_orders, df_market_snapshot_signals
    df_orders = pd.DataFrame(results)
    df_market_snapshot_signals = pd.DataFrame(market_snapshots)

    success = sum(1 for r in results if r["ok"])
    failed = len(results) - success
    print(f"RESULTS: {success} successful, {failed} failed")

    if success > 0:
        ok = df_orders[df_orders['ok']==True]
        print(f"  Total contracts: {ok['contracts'].sum()}")
        for side, grp in ok.groupby('side'):
            print(f"  {side.upper()}: {len(grp)} orders, {grp['contracts'].sum()} contracts, avg {grp['price'].mean():.0f}¢")

    return df_orders, df_market_snapshot_signals, ledger

# =====================================================================
# RUN
# =====================================================================
print("\nValidating df_results...")
to_trade = df_results[df_results["market_status"] == "active"]
print(f"Active markets: {len(to_trade)}")

if len(to_trade) == 0:
    df_orders = pd.DataFrame()
    df_market_snapshot_signals = pd.DataFrame()
    position_ledger = {}
else:
    df_orders, df_market_snapshot_signals, position_ledger = place_orders_from_df(df_results)

# =====================================================================
# UPLOAD TO BIGQUERY
# =====================================================================
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    try:
        from google.colab import auth
        auth.authenticate_user()
    except ImportError:
        pass

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
DATASET_ID = os.environ.get("GCP_DATASET_ID", "Kalshi")
client = bigquery.Client(project=PROJECT_ID)

df_results["yes_rate"] = pd.to_numeric(df_results["yes_rate"].replace("", np.nan), errors="coerce").infer_objects(copy=False)
for c in df_results.columns:
    if c != "yes_rate": df_results[c] = df_results[c].astype("string")
for col in ["event_date"]:
    if col in df_results.columns:
        df_results[col] = pd.to_datetime(df_results[col], errors="coerce").dt.date

def get_existing_table_schema(table_name):
    try:
        table = client.get_table(f"{PROJECT_ID}.{DATASET_ID}.{table_name}")
        return {f.name: f.field_type for f in table.schema}
    except: return None

def pandas_dtype_to_bq_type(dtype):
    s = str(dtype)
    if pd.api.types.is_integer_dtype(dtype): return "INTEGER"
    elif pd.api.types.is_float_dtype(dtype): return "FLOAT"
    elif pd.api.types.is_bool_dtype(dtype): return "BOOLEAN"
    elif pd.api.types.is_datetime64_any_dtype(dtype): return "TIMESTAMP"
    return "STRING"

def convert_df_to_match_schema(df, schema_dict):
    df_c = df.copy()
    for col, bq_type in schema_dict.items():
        if col not in df_c.columns: continue
        if bq_type == "STRING" and not pd.api.types.is_string_dtype(df_c[col].dtype):
            df_c[col] = df_c[col].astype(str).replace('nan', None).replace('None', None)
        elif bq_type == "FLOAT": df_c[col] = pd.to_numeric(df_c[col], errors='coerce')
        elif bq_type == "INTEGER": df_c[col] = pd.to_numeric(df_c[col], errors='coerce').astype('Int64')
        elif bq_type == "BOOLEAN": df_c[col] = df_c[col].astype(bool)
    return df_c

def add_missing_columns_to_table(table_name, df, existing_schema):
    table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    table = client.get_table(table_id)
    new_schema = list(table.schema)
    for col in df.columns:
        if col not in existing_schema:
            new_schema.append(bigquery.SchemaField(col, pandas_dtype_to_bq_type(df[col].dtype), mode="NULLABLE"))
    if len(new_schema) > len(table.schema):
        table.schema = new_schema
        client.update_table(table, ["schema"])

def df_to_bq(df, table_name, write_disposition="WRITE_TRUNCATE"):
    table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    df_up = df.copy()
    if write_disposition == "WRITE_APPEND":
        existing = get_existing_table_schema(table_name)
        if existing:
            new_cols = set(df_up.columns) - set(existing.keys())
            if new_cols: add_missing_columns_to_table(table_name, df_up, existing); existing = get_existing_table_schema(table_name)
            df_up = convert_df_to_match_schema(df_up, existing)
    job_config = bigquery.LoadJobConfig(write_disposition=write_disposition, autodetect=True)
    job = client.load_table_from_dataframe(df_up, table_id, job_config=job_config)
    job.result()
    print(f"✓ Loaded: {table_id} rows: {client.get_table(table_id).num_rows}")

df_to_bq(df_results, f"{SERIES_TICKER}_market_results", "WRITE_TRUNCATE")
df_to_bq(df_summary, f"{SERIES_TICKER}_market_results_summary", "WRITE_TRUNCATE")
if 'df_orders' in dir() and len(df_orders) > 0:
    df_to_bq(df_orders, f"{SERIES_TICKER}_orders", "WRITE_APPEND")
if 'df_market_snapshot_signals' in dir() and len(df_market_snapshot_signals) > 0:
    df_to_bq(df_market_snapshot_signals, f"{SERIES_TICKER}_market_snapshot_signals", "WRITE_APPEND")
if 'position_ledger' in dir() and position_ledger:
    df_ledger = ledger_to_dataframe(position_ledger)
    if len(df_ledger) > 0:
        df_to_bq(df_ledger, f"{SERIES_TICKER}_position_ledger", "WRITE_TRUNCATE")

print("\n✓ All tables uploaded successfully!")

# =====================================================================
# END-OF-RUN ALERT SUMMARY
# =====================================================================
if _ALERTS:
    print(f"\n{'='*70}")
    print(f"⚡ ALERT SUMMARY: {len(_ALERTS)} alerts")
    print(f"{'='*70}")
    _cats = {}
    for _a in _ALERTS:
        _c = _a['category']
        _cats[_c] = _cats.get(_c, 0) + 1
    for _cat, _count in sorted(_cats.items(), key=lambda x: -x[1]):
        print(f"  {_cat}: {_count}")
    upload_alerts_to_bq(client, PROJECT_ID, DATASET_ID, SERIES_TICKER)
    send_alert_notification()
    print(f"{'='*70}\n")
else:
    print("\n✓ No alerts — clean run")
