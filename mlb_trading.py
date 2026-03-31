# -*- coding: utf-8 -*-
"""MLBMENTIONS_TRADING v1.0
   
   MLB version of NBAMENTIONS_TRADING v4.6.
   Mirrors NBA script structure with:
     - MLB Stats API for exact game start times
     - BASE_CONTRACTS = 1 (no historical data yet)
     - DISCOVERY_MODE = True (trades unknown codes at reduced size)
     - Empty SIDE_MULTIPLIERS / TEAM_MULTIPLIERS (to be tuned from data)
"""

# STEP 1: FETCH DATA
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
import numpy as np
from google.cloud import bigquery
import json as _json
import urllib.request

# =========================
# ALERTING
# =========================
# Collects all fallback/warning events during the run.
# At end of run: uploads to BQ + sends Slack summary (if webhook configured).

_ALERTS: List[Dict[str, Any]] = []

# Notification config — set in GitHub Secrets
# ALERT_EMAIL_TO can be a regular email OR a carrier SMS gateway:
#   Verizon:  1234567890@vtext.com
#   AT&T:     1234567890@txt.att.net
#   T-Mobile: 1234567890@tmomail.net
#   Sprint:   1234567890@messaging.sprintpcs.com
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")      # e.g. yourname@gmail.com
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "") # Gmail app password
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")           # email or SMS gateway


def alert(category: str, message: str, context: Dict[str, Any] = None):
    """Record a fallback/warning event. Always prints; collected for end-of-run summary."""
    ts = datetime.now(UTC).isoformat()
    entry = {
        "timestamp": ts,
        "category": category,
        "message": message,
        **(context or {}),
    }
    _ALERTS.append(entry)
    ctx_str = f" | {context}" if context else ""
    print(f"  ⚡ ALERT [{category}]: {message}{ctx_str}")


def send_alert_notification():
    """Send alert summary via email/text if configured."""
    import smtplib
    from email.mime.text import MIMEText

    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASSWORD or not ALERT_EMAIL_TO:
        return
    if len(_ALERTS) == 0:
        return

    # Group by category
    cats = {}
    for a in _ALERTS:
        c = a['category']
        cats[c] = cats.get(c, 0) + 1

    # Build message body
    lines = [f"KXMLBMENTION Alerts: {len(_ALERTS)} total"]
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    lines.append("")
    for a in _ALERTS[:8]:
        lines.append(f"[{a['category']}] {a['message']}")
    if len(_ALERTS) > 8:
        lines.append(f"... +{len(_ALERTS) - 8} more")

    body = "\n".join(lines)

    # Carrier SMS gateways (tmomail.net, vtext.com, etc.) silently drop long messages.
    # Truncate body for SMS recipients to ~300 chars.
    SMS_GATEWAYS = ("tmomail.net", "vtext.com", "txt.att.net", "messaging.sprintpcs.com", "msg.fi.google.com")

    # Send to each recipient (comma-separated)
    recipients = [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]

    for recipient in recipients:
        try:
            is_sms = any(gw in recipient.lower() for gw in SMS_GATEWAYS)
            send_body = body[:300] if is_sms else body

            msg = MIMEText(send_body)
            msg["Subject"] = f"MLB Bot: {len(_ALERTS)} alerts" if not is_sms else ""
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = recipient

            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
        except Exception as e:
            print(f"  ⚠️ Alert to {recipient} failed: {e}")

    print(f"  ✓ Alert notification sent to {len(recipients)} recipient(s)")


def upload_alerts_to_bq(bq_client, project_id: str, dataset_id: str, series_ticker: str):
    """Upload alerts to BQ for historical tracking."""
    if bq_client is None or len(_ALERTS) == 0:
        return
    try:
        df_alerts = pd.DataFrame(_ALERTS)
        df_alerts['run_id'] = RUN_ID
        df_alerts['model_version'] = MODEL_VERSION
        table_id = f"{project_id}.{dataset_id}.{series_ticker}_alerts"
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            autodetect=True,
        )
        job = bq_client.load_table_from_dataframe(df_alerts, table_id, job_config=job_config)
        job.result()
        print(f"  ✓ Uploaded {len(df_alerts)} alerts to {table_id}")
    except Exception as e:
        print(f"  ⚠️ Alert upload failed: {e}")

# =========================
# CONFIG
# =========================

SERIES_TICKER = "KXMLBMENTION"
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

        params = {
            "series_ticker": series_ticker,
            "limit": 200,
        }
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


# Known MLB team abbreviations as used by Kalshi.
# 2-letter: SF, KC, SD, TB
# 3-letter: everything else
# Update this set as new tickers appear.
MLB_KNOWN_TEAMS = {
    "ARI", "ATL", "BAL", "BOS", "CHC", "CHW", "CIN", "CLE", "COL",
    "DET", "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM",
    "NYY", "OAK", "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB",
    "TEX", "TOR", "WAS",
}


def _split_teams(teams_str: str) -> Tuple[str, str]:
    """Split a concatenated team string like 'NYYSF' into ('NYY', 'SF').

    Tries all valid (2-or-3, 2-or-3) splits and picks the one where both
    halves are in MLB_KNOWN_TEAMS.  Falls back to (first_3, rest).
    """
    for first_len in (3, 2):
        t1 = teams_str[:first_len]
        t2 = teams_str[first_len:]
        if t1 in MLB_KNOWN_TEAMS and t2 in MLB_KNOWN_TEAMS:
            return t1, t2
    # Fallback: assume first team is 3 chars
    return teams_str[:3], teams_str[3:]


def parse_event_code(event_code: str) -> Dict[str, str]:
    """
    Parse MLB event code.

    Format: YYmmmDDHHMMTEAM1TEAM2
    Example: 26MAR252005NYYSF
      → date 2026-03-25, time 20:05 ET, teams NYY vs SF

    The HHMM block is 4 digits immediately after the 7-char date.
    Teams follow and can be 2 or 3 chars each.
    """
    event_code = event_code or ""

    date_code = event_code[:7] if len(event_code) >= 7 else ""
    rest = event_code[7:]  # e.g. "2005NYYSF"

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

    # Detect HHMM time component: if the first 4 chars of `rest` are all digits
    ticker_time_et = ""
    if len(rest) >= 4 and rest[:4].isdigit():
        ticker_time_et = rest[:4]          # e.g. "2005"
        teams_code = rest[4:]              # e.g. "NYYSF"
    else:
        teams_code = rest

    team_1, team_2 = _split_teams(teams_code) if teams_code else ("", "")

    return {
        "date_code": date_code,
        "event_date": event_date_iso,
        "teams_code": teams_code,
        "team_1": team_1,
        "team_2": team_2,
        "ticker_time_et": ticker_time_et,  # "2005" = 8:05 PM ET
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

    active_count = df_results['is_active'].sum()
    settled_count = df_results['is_settled'].sum()

    print(f"\n  Active/tradeable markets: {active_count}")
    print(f"  Settled markets with results: {settled_count}")

    print("\nBuilding rolling summary (from SETTLED markets only)...")
    df_settled = df_results[df_results['is_settled']].copy()
    print(f"  Using {len(df_settled)} settled markets with results")

    if len(df_settled) == 0:
        print("⚠️  No settled markets found — cannot build historical data (expected for new series)")
        df_results.loc[df_results['is_active'], 'market_status'] = 'active'
        return df_results, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df_by_date = (
        df_settled
        .query("ticker_part_3_market_code != '' and event_date != ''")
        .groupby(['ticker_part_3_market_code', 'event_date'], as_index=False)
        .agg(
            count_occurrences=('market_ticker', 'size'),
            count_yes=('result_yes_no', lambda s: (s == 'YES').sum()),
        )
        .sort_values(['ticker_part_3_market_code', 'event_date'])
    )

    if len(df_by_date) == 0:
        print("⚠️  No valid settled data for rolling summary")
        df_results.loc[df_results['is_active'], 'market_status'] = 'active'
        return df_results, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df_summary_rolling = df_by_date.copy()
    df_summary_rolling['count_occurrences_rolling'] = (
        df_summary_rolling.groupby('ticker_part_3_market_code')['count_occurrences'].cumsum()
    )
    df_summary_rolling['count_yes_rolling'] = (
        df_summary_rolling.groupby('ticker_part_3_market_code')['count_yes'].cumsum()
    )
    df_summary_rolling['yes_rate_rolling'] = (
        df_summary_rolling['count_yes_rolling'] / df_summary_rolling['count_occurrences_rolling']
    )

    df_summary = (
        df_summary_rolling
        .sort_values(['ticker_part_3_market_code', 'event_date'], ascending=[True, False])
        .groupby('ticker_part_3_market_code', as_index=False)
        .first()
        .drop(columns=['count_occurrences', 'count_yes'])
        .rename(columns={
            'count_occurrences_rolling': 'count_occurrences',
            'count_yes_rolling': 'count_yes',
            'yes_rate_rolling': 'yes_rate'
        })
    )

    # Team-level rolling summary
    print("\nBuilding team-level rolling summary...")
    df_filtered = df_settled.query(
        "ticker_part_3_market_code != '' and event_date != '' and team_1 != '' and team_2 != ''"
    ).copy()

    if len(df_filtered) == 0:
        print("⚠️  No valid data for team-level summary")
        df_summary_filtered = df_summary[df_summary['count_occurrences'] > 5][
            ["ticker_part_3_market_code", "yes_rate"]
        ].copy()
        df_results = df_results.merge(df_summary_filtered, on="ticker_part_3_market_code", how="left")
        df_results["yes_rate"] = df_results.apply(
            lambda row: row["yes_rate"] if row["is_active"] else "", axis=1
        )
        df_results.loc[df_results['is_active'], 'market_status'] = 'active'
        return df_results, df_summary, df_summary_rolling, pd.DataFrame()

    df_team1 = df_filtered.copy()
    df_team1['team'] = df_team1['team_1']
    df_team1['opponent'] = df_team1['team_2']
    df_team1['is_team_1'] = True

    df_team2 = df_filtered.copy()
    df_team2['team'] = df_team2['team_2']
    df_team2['opponent'] = df_team2['team_1']
    df_team2['is_team_1'] = False

    df_team_exploded = pd.concat([df_team1, df_team2], ignore_index=True)

    def determine_team_result(row):
        if row['result_yes_no'] == '':
            return ''
        return row['result_yes_no']

    df_team_exploded['team_result'] = df_team_exploded.apply(determine_team_result, axis=1)

    df_team_by_date = (
        df_team_exploded
        .groupby(['ticker_part_3_market_code', 'team', 'event_date'], as_index=False)
        .agg(
            count_occurrences=('market_ticker', 'size'),
            count_yes=('team_result', lambda s: (s == 'YES').sum()),
        )
        .sort_values(['ticker_part_3_market_code', 'team', 'event_date'])
    )

    df_summary_rolling_by_team = df_team_by_date.copy()
    df_summary_rolling_by_team['count_occurrences_rolling'] = (
        df_summary_rolling_by_team.groupby(['ticker_part_3_market_code', 'team'])['count_occurrences'].cumsum()
    )
    df_summary_rolling_by_team['count_yes_rolling'] = (
        df_summary_rolling_by_team.groupby(['ticker_part_3_market_code', 'team'])['count_yes'].cumsum()
    )
    df_summary_rolling_by_team['yes_rate_rolling'] = (
        df_summary_rolling_by_team['count_yes_rolling'] / df_summary_rolling_by_team['count_occurrences_rolling']
    )

    # Lower threshold for new series (5 instead of 25)
    df_summary_filtered = df_summary[df_summary['count_occurrences'] > 5][
        ["ticker_part_3_market_code", "yes_rate"]
    ].copy()

    df_results = df_results.merge(df_summary_filtered, on="ticker_part_3_market_code", how="left")
    df_results["yes_rate"] = df_results.apply(
        lambda row: row["yes_rate"] if row["is_active"] else "", axis=1
    )
    df_results.loc[df_results['is_active'], 'market_status'] = 'active'

    print(f"\nDataframes created:")
    print(f"  df_results: {df_results.shape}")
    print(f"  df_summary: {df_summary.shape}")
    print(f"  df_summary_rolling: {df_summary_rolling.shape}")
    print(f"  df_summary_rolling_by_team: {df_summary_rolling_by_team.shape}")

    return df_results, df_summary, df_summary_rolling, df_summary_rolling_by_team


# RUN THE DATA COLLECTION
df_results, df_summary, df_summary_rolling, df_summary_rolling_by_team = main()

print("\n✓ Data collection complete!")
if len(df_results) > 0:
    active_count = (df_results['market_status'] == 'active').sum()
    print(f"✓ df_results has {active_count} active markets ready to trade")
else:
    print("⚠️  No data collected")


###################################################################################
# MLBMENTION SCRIPT v1.0 — DISCOVERY MODE
# ==============================================================
# Based on NBAMENTION v4.6 with MLB-specific adaptations:
#   - MLB Stats API for exact game start times (statsapi.mlb.com)
#   - BASE_CONTRACTS = 1 (conservative sizing, no historical data)
#   - DISCOVERY_MODE = True (trades unknown codes at 0.5x for data collection)
#   - Empty SIDE_MULTIPLIERS / TEAM_MULTIPLIERS (to be tuned from settlements)
#   - All v4.6 structural features: ledger, pairing, overshoot cap, hedge EV
#
# Domain to allowlist for GitHub Actions: statsapi.mlb.com
# Also keep api.elections.kalshi.com on allowlist.
###################################################################################

RUN_ID = str(uuid.uuid4())
MODEL_VERSION = "mlb_v1.2"
PRICING_STRATEGY = 'hybrid'

# ---------- DISCOVERY MODE ----------
# When True: unknown market codes get traded at DISCOVERY_SIDE_MULT instead of blocked.
# This collects fill data + settlement outcomes for tuning SIDE_MULTIPLIERS.
# Set to False once you have ~200+ settlements and can build proper multipliers.
DISCOVERY_MODE = True
DISCOVERY_SIDE_MULT = 1.0  # base=1 contract already limits risk; 0.5x caused rounding to 0

# ---------- BAYESIAN HYBRID PRICING PARAMETERS ----------
BAYESIAN_K = 25
BAYESIAN_RECENCY_HALFLIFE_GAMES = 50
# [v1.1] 90/10 market/historical — historical is noise at n≤4 per code
# These are DEFAULTS — overridden by adaptive calibration if BQ data is available
BAYESIAN_MARKET_WEIGHT = 0.90
BAYESIAN_HISTORICAL_WEIGHT = 0.10
_BAYESIAN_WEIGHTS_PER_CODE: Dict[str, tuple] = {}  # populated by calibration

# =====================================================================
# BANKROLL-SCALED POSITION MANAGEMENT
# =====================================================================
BANKROLL_FALLBACK = 10000

def fetch_bankroll(client) -> Tuple[float, int, int]:
    try:
        response = client.get_balance()
        balance_cents = response.get("balance", 0)
        portfolio_cents = response.get("portfolio_value", 0)
        bankroll = (balance_cents + portfolio_cents) / 100.0
        print(f"  Kalshi bankroll: ${bankroll:,.2f} (cash: ${balance_cents/100:.2f}, positions: ${portfolio_cents/100:.2f})")
        return bankroll, balance_cents, portfolio_cents
    except Exception as e:
        alert("BANKROLL_FAILED", f"Using fallback ${BANKROLL_FALLBACK:,}: {e}")
        return BANKROLL_FALLBACK, 0, 0

def get_scaled_limits(bankroll: float) -> dict:
    scale = max(0.5, min(bankroll / 5000, 2.0))
    return {
        "MAX_NET_PER_MARKET": int(200 * scale),
        "POSITION_MODERATE_THRESHOLD": int(67 * scale),
        "POSITION_STOP_THRESHOLD": int(200 * scale),
        "MAX_NET_PER_EVENT": int(1000 * scale),
        "MAX_PAIRED_PER_MARKET": int(350 * scale),
        "PAIRING_MODE_NET_FLOOR": int(80 * scale),
        "PAIRING_MODE_NET_AGGRESSIVE": int(150 * scale),
        "scale": scale,
    }

BANKROLL, _balance_cents, _portfolio_cents = fetch_bankroll(exchange_client)
_limits = get_scaled_limits(BANKROLL)

df_bankroll_log = pd.DataFrame([{
    "timestamp": datetime.now(UTC).isoformat(),
    "run_id": RUN_ID,
    "model_version": MODEL_VERSION,
    "balance_cents": _balance_cents,
    "portfolio_cents": _portfolio_cents,
    "bankroll": BANKROLL,
    "scale": _limits["scale"],
    "max_net_per_market": _limits["MAX_NET_PER_MARKET"],
}])

MAX_NET_PER_MARKET = _limits["MAX_NET_PER_MARKET"]
POSITION_MODERATE_THRESHOLD = _limits["POSITION_MODERATE_THRESHOLD"]
POSITION_STOP_THRESHOLD = _limits["POSITION_STOP_THRESHOLD"]
MAX_ORDERBOOK_LEVELS_ABOVE = -1  # [v1.0] Never at top of book: orders must be below best bid

MAX_NET_PER_EVENT = _limits["MAX_NET_PER_EVENT"]
MAX_ORDERS_PER_EVENT = 5000

MIN_SPREAD_BOTH_SIDES = 0
MIN_EV_PER_ORDER = 0.02
SAFE_MODE_MULTIPLIER = 0.10

# =====================================================================
# PAIRING MODE THRESHOLDS
# =====================================================================
PAIRING_MODE_THRESHOLD = 0.40
PAIRING_MODE_AGGRESSIVE = 0.75
PAIRING_MODE_NET_FLOOR = _limits["PAIRING_MODE_NET_FLOOR"]
PAIRING_MODE_NET_AGGRESSIVE = _limits["PAIRING_MODE_NET_AGGRESSIVE"]

HEDGE_MIN_PRICE_YES = 8
HEDGE_MIN_PRICE_NO = 3
HEDGE_YES_MIN_PRICE = 40

MAX_PAIRED_PER_MARKET = _limits["MAX_PAIRED_PER_MARKET"]

# =====================================================================
# SIDE_MULTIPLIERS — EMPTY FOR NEW SERIES
# =====================================================================
# As settlement data comes in, populate this dict.
# In DISCOVERY_MODE, unknown codes trade at DISCOVERY_SIDE_MULT on both sides.
# Set a code to 0.0/0.0 to block it after seeing bad results.
SIDE_MULTIPLIERS = {
    # === INITIAL MLB CODES — add as you observe them ===
    # Format: "CODE": {"yes": mult, "no": mult}
    # Example after ~100 settlements:
    #   "HOMER": {"yes": 0.0, "no": 1.5},
    #   "STEAL": {"yes": 1.0, "no": 0.0},
    # 
    # Blocked codes (add here as you identify losers):
    # "BADCODE": {"yes": 0.0, "no": 0.0},
}

# =====================================================================
# TEAM MULTIPLIERS — EMPTY FOR NEW SERIES
# =====================================================================
# Populate after ~200 settlements with team-level P&L data.
# Boost: teams with >10% ROI, Penalize: teams with < -20% ROI.
TEAM_MULTIPLIERS = {
    # Example after data collection:
    #   "NYY": 1.3,   # high-mention, high-fill team
    #   "OAK": 0.5,   # low-liquidity, bad fills
}

# MLB team code mapping: MLB Stats API abbreviation → Kalshi 3-letter code
# Update this as you discover how Kalshi encodes MLB teams.
# Most should match directly (NYY, BOS, LAD, etc.)
MLB_TEAM_CODE_MAP = {
    # These are the standard MLB abbreviations — Kalshi likely uses the same
    "AZ": "ARI",     # Arizona Diamondbacks (MLB API uses "AZ", Kalshi may use "ARI")
    "CWS": "CHW",    # Chicago White Sox (varies by source)
    "KC": "KCR",     # Kansas City Royals (MLB API may use "KC")
    "SD": "SDP",     # San Diego Padres (MLB API may use "SD")
    "SF": "SFG",     # San Francisco Giants (MLB API may use "SF")
    "TB": "TBR",     # Tampa Bay Rays
    "WSH": "WAS",    # Washington Nationals
    # Most teams use the same 3-letter code in both systems:
    # NYY, BOS, LAD, ATL, HOU, PHI, SEA, MIN, DET, CLE, BAL, MIL, etc.
}

# Reverse mapping for lookups
MLB_TEAM_CODE_MAP_REVERSE = {v: k for k, v in MLB_TEAM_CODE_MAP.items()}

TOP_NO_MARKETS = set()  # Populate after data collection

# =====================================================================
# CORRELATION PENALTY — EMPTY FOR NEW SERIES
# =====================================================================
CORRELATED_PAIRS = {}
CORRELATION_PENALTY = 0.75

# =====================================================================
# Price filters — [v1.1] updated from 3-event audit
# =====================================================================
MIN_PRICE_YES = 15
MIN_PRICE_NO = 5
MAX_PRICE = 65              # [v1.1] was 75 — YES 60-75c bucket lost $6 at -50pp edge
YES_MIN_PRICE = 25
NO_MAX_YES_PRICE = 80       # [v1.1] was 88 — NO fills at implied YES >85c are death
NO_MIN_YES_PRICE = 15
YES_PROBABILITY_FLOOR = 30

MAX_PRICE_OVERRIDES = {}

NO_SWEET_SPOT_MIN = 35
NO_SWEET_SPOT_MAX = 65
NO_SWEET_SPOT_MULTIPLIER = 1.0

# ---------- ORDER CONFIGURATION ----------
# [v1.1] Asymmetric YES/NO offset levels based on 3-event audit:
#   YES 45-60c: +35pp edge (88% WR on 16 fills) → 7 levels, tight offsets
#   NO all buckets: -23pp adverse selection (p=0.019) → 5 levels, wide offsets
#   This concentrates YES near mid-price and pushes NO to deep discounts only.

NUM_YES_OFFSET_LEVELS = 7   # [v1.1] YES gets more levels (edge is real at 45-60c)
NUM_NO_OFFSET_LEVELS = 5    # [v1.1] NO gets fewer levels (adversely selected everywhere)

def generate_base_contracts(num_levels: int) -> List[int]:
    return [1] * num_levels  # 1 contract per level

BASE_YES_CONTRACTS = generate_base_contracts(NUM_YES_OFFSET_LEVELS)
BASE_NO_CONTRACTS = generate_base_contracts(NUM_NO_OFFSET_LEVELS)
MAX_CONTRACTS_PER_ORDER = 1000
MAX_CONTRACTS_PER_MARKET_PER_RUN = 10  # [v1.1] was 50 — PITC had 20 fills in one market

# ---------- DYNAMIC SIZING MULTIPLIERS ----------
# [v1.0] All multipliers flat 1.0 — no scaling until we have data to tune
TIME_MULTIPLIERS = [
    (999999, 1.0),
]

VOLUME_MULTIPLIERS = [
    (0, 1.0),
]

# ---------- SPREAD-BASED OFFSET CONFIGURATION ----------
def generate_offsets(start: int, increment: int, count: int) -> List[int]:
    return [start + i * increment for i in range(count)]

# [v1.1] Asymmetric offsets: YES tight (cluster near mid), NO wide (deep discounts only)
# YES edge is at 45-60c → tighter offsets keep orders in the profitable zone
# NO is adversely selected → wider offsets mean we only fill at big discounts
SPREAD_CONFIGS = {
    (0, 2): {
        'yes': generate_offsets(start=2, increment=2, count=NUM_YES_OFFSET_LEVELS),  # [2,4,6,8,10,12,14]
        'no':  generate_offsets(start=4, increment=3, count=NUM_NO_OFFSET_LEVELS),   # [4,7,10,13,16]
        'name': 'very_tight'
    },
    (2, 5): {
        'yes': generate_offsets(start=2, increment=2, count=NUM_YES_OFFSET_LEVELS),  # [2,4,6,8,10,12,14]
        'no':  generate_offsets(start=3, increment=3, count=NUM_NO_OFFSET_LEVELS),   # [3,6,9,12,15]
        'name': 'tight'
    },
    (5, 15): {
        'yes': generate_offsets(start=1, increment=2, count=NUM_YES_OFFSET_LEVELS),  # [1,3,5,7,9,11,13]
        'no':  generate_offsets(start=3, increment=3, count=NUM_NO_OFFSET_LEVELS),   # [3,6,9,12,15]
        'name': 'medium'
    },
    (15, 100): {
        'yes': generate_offsets(start=1, increment=2, count=NUM_YES_OFFSET_LEVELS),  # [1,3,5,7,9,11,13]
        'no':  generate_offsets(start=2, increment=3, count=NUM_NO_OFFSET_LEVELS),   # [2,5,8,11,14]
        'name': 'wide'
    },
}

PAIRING_OFFSETS = {
    'yes': generate_offsets(start=1, increment=1, count=NUM_YES_OFFSET_LEVELS),
    'no':  generate_offsets(start=1, increment=2, count=NUM_NO_OFFSET_LEVELS),  # wider even in hedge mode
    'name': 'pairing'
}

EXPIRATION_HOURS_BEFORE_CLOSE = 5
SLEEP_BETWEEN_ORDERS = 0.15  # [v1.0] 0.05 hit Kalshi 429 rate limit
FALLBACK_OFFSET = 15

# ---------- Re-init Kalshi client (same as above, kept for script structure) ----------
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
print(f"CONFIGURATION — MLB v1.2 (ADAPTIVE CALIBRATION)")
print(f"{'='*70}")
print(f"Run ID: {RUN_ID}")
print(f"Model version: {MODEL_VERSION}")
print(f"Bankroll: ${BANKROLL:,.2f} (scale: {_limits['scale']:.2f}x)")
print(f"Discovery mode: {'ON' if DISCOVERY_MODE else 'OFF'} (unknown codes trade at {DISCOVERY_SIDE_MULT:.1f}x)")
print(f"Pricing strategy: {PRICING_STRATEGY}")
if PRICING_STRATEGY == 'hybrid':
    print(f"  Market weight: {BAYESIAN_MARKET_WEIGHT} (was 0.6)")
    print(f"  Historical weight: {BAYESIAN_HISTORICAL_WEIGHT} (was 0.4)")
    print(f"  → 90/10 split: lean on orderbook, not noisy n≤4 historical")
print(f"Price filters:")
print(f"  YES: {MIN_PRICE_YES}–{MAX_PRICE}c (floor: {YES_MIN_PRICE}c)")
print(f"  NO: {MIN_PRICE_NO}–{MAX_PRICE}c (implied YES: {NO_MIN_YES_PRICE}–{NO_MAX_YES_PRICE}c)")
print(f"  Max price: {MAX_PRICE}c (was 75c — 60-75c bucket lost on both sides)")
print(f"Order structure:")
print(f"  YES: {NUM_YES_OFFSET_LEVELS} levels, tight offsets (edge at 45-60c)")
print(f"  NO: {NUM_NO_OFFSET_LEVELS} levels, wide offsets (adverse selection protection)")
print(f"  Per-market per-run cap: {MAX_CONTRACTS_PER_MARKET_PER_RUN} (was 50)")
print(f"  Never top of book: MAX_ORDERBOOK_LEVELS_ABOVE={MAX_ORDERBOOK_LEVELS_ABOVE}")
print(f"{'='*70}\n")

# =====================================================================
# FILLS-BASED POSITION LEDGER
# =====================================================================

def fetch_fills_from_bq(
    bq_client,
    project_id: str,
    dataset_id: str,
    series_ticker: str = "KXMLBMENTION",
    lookback_days: int = 30,
) -> pd.DataFrame:
    table_id = f"{project_id}.{dataset_id}.{series_ticker}_orders"
    query = f"""
    SELECT
        ticker, side, price, contracts, order_id, client_order_id,
        created_at, ev, p_hat, total_multiplier, spread_config,
        spread_cents, sweet_spot_applied, pricing_strategy, model_version, run_id
    FROM `{table_id}`
    WHERE ok = TRUE
      AND created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_days} DAY)
    ORDER BY ticker, created_at
    """
    print(f"  Fetching fills from {table_id} (last {lookback_days} days)...")
    try:
        df = bq_client.query(query).to_dataframe()
        print(f"  ✓ Loaded {len(df)} fills across {df['ticker'].nunique()} markets")
        return df
    except Exception as e:
        print(f"  ✗ Error fetching fills: {e}")
        return pd.DataFrame()


def fetch_fills_from_api(
    exchange_client_ref,
    series_ticker: str = "KXMLBMENTION",
) -> pd.DataFrame:
    print(f"  Fetching fills from Kalshi API for {series_ticker}...")
    all_fills = []
    cursor = None
    page = 0

    while True:
        page += 1
        params = {"limit": 1000}
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
            print(f"    Page {page}: {len(all_fills)} {series_ticker} fills so far")

    if not all_fills:
        print(f"  No fills found for {series_ticker}")
        return pd.DataFrame()

    df = pd.DataFrame(all_fills)
    print(f"  ✓ Loaded {len(df)} fills across {df['ticker'].nunique()} markets from API")

    # Normalize column names (API fields change silently)
    if 'contracts' not in df.columns:
        if 'count' in df.columns:
            df = df.rename(columns={'count': 'contracts'})
        elif 'count_fp' in df.columns:
            df['contracts'] = pd.to_numeric(df['count_fp'], errors='coerce').fillna(0).astype(int)

    if 'yes_price' not in df.columns:
        if 'yes_price_dollars' in df.columns:
            df['yes_price'] = pd.to_numeric(df['yes_price_dollars'], errors='coerce')
        elif 'yes_price_fixed' in df.columns:
            df['yes_price'] = pd.to_numeric(df['yes_price_fixed'], errors='coerce')

    # Build unified 'price' column
    if 'yes_price' in df.columns:
        yes_prices = pd.to_numeric(df['yes_price'], errors='coerce')
        max_val = yes_prices.max()

        if pd.isna(max_val) or max_val == 0:
            print(f"  ⚠️ yes_price is all NaN/zero")
        elif max_val <= 1.0:
            df['price'] = df.apply(
                lambda row: float(row['yes_price']) if row.get('side') == 'yes'
                            else (1.0 - float(row['yes_price'])), axis=1
            )
        else:
            df['price'] = df.apply(
                lambda row: float(row['yes_price']) if row.get('side') == 'yes'
                            else (100.0 - float(row['yes_price'])), axis=1
            )
    return df


def build_position_ledger(
    fills_df: pd.DataFrame,
    price_col: str = "price",
    contracts_col: str = "contracts",
) -> Dict[str, Dict[str, Any]]:
    if fills_df is None or len(fills_df) == 0:
        print("  No fills to build ledger from")
        return {}

    ledger: Dict[str, Dict[str, Any]] = {}
    df = fills_df.copy()
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

        yes_avg_price = (
            (yes_fills[price_col] * yes_fills[contracts_col]).sum() / yes_fills[contracts_col].sum()
            if yes_qty > 0 else 0.0
        )
        no_avg_price = (
            (no_fills[price_col] * no_fills[contracts_col]).sum() / no_fills[contracts_col].sum()
            if no_qty > 0 else 0.0
        )

        paired_qty = min(yes_qty, no_qty)
        net_qty = abs(yes_qty - no_qty)
        net_side = "yes" if yes_qty > no_qty else ("no" if no_qty > yes_qty else "flat")

        paired_cost = yes_avg_price + no_avg_price if paired_qty > 0 else 0.0
        paired_edge = 100.0 - paired_cost if paired_qty > 0 else 0.0

        yes_total_cost = yes_avg_price * yes_qty
        no_total_cost = no_avg_price * no_qty

        profit_if_yes = (yes_qty * 100) - yes_total_cost - no_total_cost
        profit_if_no = (no_qty * 100) - yes_total_cost - no_total_cost
        worst_case = min(profit_if_yes, profit_if_no)

        ledger[ticker] = {
            "yes_qty": yes_qty,
            "no_qty": no_qty,
            "yes_avg_price": round(yes_avg_price, 2),
            "no_avg_price": round(no_avg_price, 2),
            "paired_qty": paired_qty,
            "net_qty": net_qty,
            "net_side": net_side,
            "paired_cost": round(paired_cost, 2),
            "paired_edge": round(paired_edge, 2),
            "yes_total_cost": round(yes_total_cost, 2),
            "no_total_cost": round(no_total_cost, 2),
            "profit_if_yes_cents": round(profit_if_yes, 2),
            "profit_if_no_cents": round(profit_if_no, 2),
            "worst_case_cents": round(worst_case, 2),
            "profit_if_yes": round(profit_if_yes / 100, 2),
            "profit_if_no": round(profit_if_no / 100, 2),
            "worst_case": round(worst_case / 100, 2),
            "paired_profit": round(paired_edge * paired_qty / 100, 2),
        }

    print(f"  ✓ Built ledger for {len(ledger)} markets")
    return ledger


def print_ledger_summary(ledger: Dict[str, Dict[str, Any]]):
    if not ledger:
        print("  Ledger is empty — no fills to analyze")
        return

    print(f"\n{'='*70}")
    print("POSITION LEDGER SUMMARY")
    print(f"{'='*70}")

    total_paired = sum(v["paired_qty"] for v in ledger.values())
    total_net = sum(v["net_qty"] for v in ledger.values())
    total_paired_profit = sum(v["paired_profit"] for v in ledger.values())
    total_worst_case = sum(v["worst_case"] for v in ledger.values())
    total_yes_cost = sum(v["yes_total_cost"] for v in ledger.values()) / 100
    total_no_cost = sum(v["no_total_cost"] for v in ledger.values()) / 100

    print(f"\n  AGGREGATE:")
    print(f"    Markets with fills: {len(ledger)}")
    print(f"    Total paired contracts: {total_paired}")
    print(f"    Total net (directional) contracts: {total_net}")
    if (total_paired + total_net) > 0:
        print(f"    Paired/Total ratio: {total_paired/(total_paired+total_net)*100:.1f}%")
    print(f"    Total capital deployed: ${total_yes_cost + total_no_cost:.2f}")

    print(f"\n  PROFITABILITY:")
    print(f"    Guaranteed paired profit: ${total_paired_profit:.2f}")
    print(f"    Worst-case total P&L: ${total_worst_case:.2f}")

    sorted_by_profit = sorted(ledger.items(), key=lambda x: x[1]["paired_profit"], reverse=True)
    print(f"\n  TOP 10 BY PAIRED PROFIT:")
    print(f"    {'Ticker':<45} {'Paired':>6} {'Edge':>6} {'Profit':>8} {'Net':>5} {'Side':>4}")
    print(f"    {'-'*45} {'-'*6} {'-'*6} {'-'*8} {'-'*5} {'-'*4}")
    for ticker, data in sorted_by_profit[:10]:
        short_ticker = ticker[-40:] if len(ticker) > 40 else ticker
        print(f"    {short_ticker:<45} {data['paired_qty']:>6} "
              f"{data['paired_edge']:>5.1f}c ${data['paired_profit']:>7.2f} "
              f"{data['net_qty']:>5} {data['net_side']:>4}")

    print(f"{'='*70}\n")


def ledger_to_dataframe(ledger: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    if not ledger:
        return pd.DataFrame()
    rows = []
    for ticker, data in ledger.items():
        row = {"ticker": ticker, **data}
        parts = ticker.split("-")
        if len(parts) >= 3:
            row["market_code"] = parts[2] if len(parts) == 4 else parts[-1]
            row["event_ticker"] = "-".join(parts[:2])
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values("paired_profit", ascending=False).reset_index(drop=True)


def integrate_ledger_into_run(
    exchange_client_ref,
    bq_client=None,
    project_id: str = None,
    dataset_id: str = None,
    series_ticker: str = "KXMLBMENTION",
    use_api: bool = True,
    use_bq: bool = True,
    lookback_days: int = 30,
) -> Dict[str, Dict[str, Any]]:
    print(f"\n{'='*70}")
    print("BUILDING FILLS-BASED POSITION LEDGER")
    print(f"{'='*70}")

    fills_df = pd.DataFrame()

    if use_api:
        try:
            fills_df = fetch_fills_from_api(exchange_client_ref, series_ticker)
        except Exception as e:
            print(f"  ⚠️ API fills fetch failed: {e}")

    if len(fills_df) == 0 and use_bq and bq_client is not None:
        try:
            fills_df = fetch_fills_from_bq(bq_client, project_id, dataset_id, series_ticker, lookback_days)
        except Exception as e:
            print(f"  ⚠️ BQ fills fetch failed: {e}")

    if len(fills_df) == 0:
        print("  ⚠️ No fills available — ledger will be empty (expected for new series)")
        return {}

    ledger = build_position_ledger(fills_df)
    print_ledger_summary(ledger)
    return ledger


# =====================================================================
# HEDGE-ADJUSTED EV CALCULATION
# =====================================================================

def calculate_hedge_ev(
    side: str,
    bid_price: int,
    ledger_data: Optional[Dict[str, Any]],
) -> Tuple[float, bool]:
    if ledger_data is None:
        return 0.0, False

    net_side = ledger_data.get("net_side", "flat")
    net_qty = ledger_data.get("net_qty", 0)

    if net_qty == 0 or net_side == "flat":
        return 0.0, False
    if net_side == side:
        return 0.0, False

    if side == "no":
        opposite_avg = ledger_data.get("yes_avg_price", 0.0)
    else:
        opposite_avg = ledger_data.get("no_avg_price", 0.0)

    if opposite_avg <= 0:
        return 0.0, False

    hedge_ev = (100.0 - opposite_avg - bid_price) / 100.0
    if hedge_ev <= 0:
        return 0.0, True
    return hedge_ev, True


# =====================================================================
# PAIRING MODE DETECTION
# =====================================================================

def get_pairing_mode(
    ticker: str,
    ledger: Dict[str, Dict[str, Any]],
    net_position: int,
) -> Tuple[str, float]:
    ledger_data = ledger.get(ticker)
    if ledger_data is not None:
        net_qty = ledger_data.get("net_qty", 0)
    else:
        net_qty = abs(net_position)

    imbalance_ratio = net_qty / MAX_NET_PER_MARKET if MAX_NET_PER_MARKET > 0 else 0.0

    if net_qty >= PAIRING_MODE_NET_AGGRESSIVE:
        return "aggressive_pairing", imbalance_ratio
    elif net_qty >= PAIRING_MODE_NET_FLOOR:
        return "pairing", imbalance_ratio
    else:
        return "normal", imbalance_ratio


def get_effective_side_multiplier(
    base_side_mult: float,
    side: str,
    pairing_mode: str,
    net_side: str,
) -> float:
    if pairing_mode == "normal":
        return base_side_mult
    if net_side == side or net_side == "flat":
        return base_side_mult
    if pairing_mode == "aggressive_pairing":
        return max(base_side_mult, 1.5)
    elif pairing_mode == "pairing":
        return max(base_side_mult, 1.0)
    return base_side_mult


def calculate_ledger_position_multiplier(
    ticker: str,
    side: str,
    ledger: Dict[str, Dict[str, Any]],
    fallback_net_position: int,
) -> Tuple[float, str, float]:
    data = ledger.get(ticker)
    if data is None:
        return calculate_position_multiplier(fallback_net_position, side), "no_ledger_data", 0.0

    paired_qty = data["paired_qty"]
    net_qty = data["net_qty"]
    net_side = data["net_side"]
    paired_edge = data["paired_edge"]
    side_qty = data["yes_qty"] if side == "yes" else data["no_qty"]

    if side_qty >= MAX_PAIRED_PER_MARKET:
        return 0.0, f"hard_cap_{side}_qty={side_qty}", 0.0

    if net_side == side and net_qty > 0:
        if net_qty >= MAX_NET_PER_MARKET:
            return 0.0, f"net_cap_{side}={net_qty}", 0.0
        elif net_qty >= MAX_NET_PER_MARKET * 0.7:
            remaining_frac = (MAX_NET_PER_MARKET - net_qty) / (MAX_NET_PER_MARKET * 0.3)
            mult = max(0.1, remaining_frac)
            return mult, f"net_rampdown_{side}={net_qty}", 0.0
        else:
            return 1.0, f"net_ok_{side}={net_qty}", 0.0

    elif net_side != side and net_side != "flat" and net_qty > 0:
        if net_qty >= PAIRING_MODE_NET_AGGRESSIVE:
            return 2.0, f"aggressive_pair_boost_net_{net_side}={net_qty}", 1.5
        elif net_qty >= PAIRING_MODE_NET_FLOOR:
            return 1.5, f"pair_boost_net_{net_side}={net_qty}", 1.0
        elif net_qty >= int(MAX_NET_PER_MARKET * 0.25):
            return 1.2, f"mild_pair_boost_net_{net_side}={net_qty}", 0.8
        else:
            return 1.0, f"net_balanced", 0.0

    if paired_qty > MAX_PAIRED_PER_MARKET * 0.6 and paired_edge > 3:
        return 0.6, f"diminishing_returns_paired={paired_qty}_edge={paired_edge:.1f}", 0.0

    return 1.0, "default", 0.0


# ---------- Cancel orders ----------
def cancel_all_existing_orders_batch():
    print("\n" + "="*70)
    print(f"CANCELING ORDERS FOR {SERIES_TICKER}")
    print("="*70)

    try:
        all_orders = []
        cursor = None
        page = 0

        while True:
            page += 1
            params = {"limit": 500}
            if cursor:
                params["cursor"] = cursor
            response = exchange_client.get_orders(**params)
            batch = response.get('orders', [])
            all_orders.extend(batch)

            cursor = response.get('cursor')
            if not cursor:
                break

        series_orders = [
            o for o in all_orders
            if o.get('status') == 'resting'
            and o.get('ticker', '').startswith(SERIES_TICKER)
        ]

        print(f"Found {len(series_orders)} resting {SERIES_TICKER} orders to cancel")

        if len(series_orders) == 0:
            print("No orders to cancel\n")
            return 0

        order_ids = [o['order_id'] for o in series_orders]
        canceled_count = 0
        chunk_size = 100

        for i in range(0, len(order_ids), chunk_size):
            chunk = order_ids[i:i+chunk_size]
            try:
                if hasattr(exchange_client, 'cancel_orders'):
                    exchange_client.cancel_orders(order_ids=chunk)
                    canceled_count += len(chunk)
                else:
                    for order_id in chunk:
                        try:
                            exchange_client.cancel_order(order_id=order_id)
                            canceled_count += 1
                        except:
                            pass
                time.sleep(0.1)
            except Exception as e:
                for order_id in chunk:
                    try:
                        exchange_client.cancel_order(order_id=order_id)
                        canceled_count += 1
                    except:
                        pass
                    time.sleep(0.02)

        print(f"✓ Canceled {canceled_count}/{len(order_ids)} orders\n")
        return canceled_count

    except Exception as e:
        print(f"✗ Error: {e}\n")
        return 0


# =====================================================================
# POSITION FETCHING
# =====================================================================

def get_existing_positions() -> Tuple[Dict[str, Dict[str, int]], bool]:
    print("\n" + "="*70)
    print(f"CHECKING EXISTING POSITIONS FOR {SERIES_TICKER}")
    print("="*70)

    try:
        all_positions_raw = []
        cursor = None
        page = 0

        while True:
            page += 1
            params = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = exchange_client.get_positions(**params)
            batch = response.get('market_positions', [])
            all_positions_raw.extend(batch)
            cursor = response.get('cursor')
            if not cursor:
                break
            time.sleep(0.05)

        if len(all_positions_raw) == 0:
            print("  No existing positions found\n")
            return {}, True

        positions = {}
        for pos in all_positions_raw:
            ticker = pos.get('ticker', '')
            if not ticker or not ticker.startswith(SERIES_TICKER):
                continue
            net_position = pos.get('position', 0) or 0
            if net_position == 0:
                continue
            positions[ticker] = {'net_position': net_position}
            if abs(net_position) > 10:
                direction = "YES" if net_position > 0 else "NO"
                print(f"  {ticker}: {abs(net_position)} net {direction}")

        if positions:
            total_markets = len(positions)
            total_net_exposure = sum(abs(p['net_position']) for p in positions.values())
            print(f"\n✓ Position Summary: {total_markets} markets, {total_net_exposure} net contracts")
        else:
            print("  No non-zero positions found")

        print()
        return positions, True

    except Exception as e:
        alert("SAFE_MODE", f"Position fetch failed, sizing at {SAFE_MODE_MULTIPLIER:.0%}: {e}")
        return {}, False


def calculate_position_multiplier(net_position: int, side: str) -> float:
    abs_position = abs(net_position)

    if net_position > 0:
        if side == 'yes':
            if abs_position >= POSITION_STOP_THRESHOLD:
                return 0.0
            elif abs_position >= POSITION_MODERATE_THRESHOLD:
                progress = (abs_position - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
                return 1.0 - progress
            else:
                return 1.0
        else:
            if abs_position >= POSITION_STOP_THRESHOLD:
                return 2.0
            elif abs_position >= POSITION_MODERATE_THRESHOLD:
                progress = (abs_position - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
                return 1.0 + progress
            else:
                return 1.0
    elif net_position < 0:
        if side == 'no':
            if abs_position >= POSITION_STOP_THRESHOLD:
                return 0.0
            elif abs_position >= POSITION_MODERATE_THRESHOLD:
                progress = (abs_position - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
                return 1.0 - progress
            else:
                return 1.0
        else:
            if abs_position >= POSITION_STOP_THRESHOLD:
                return 2.0
            elif abs_position >= POSITION_MODERATE_THRESHOLD:
                progress = (abs_position - POSITION_MODERATE_THRESHOLD) / (POSITION_STOP_THRESHOLD - POSITION_MODERATE_THRESHOLD)
                return 1.0 + progress
            else:
                return 1.0
    else:
        return 1.0


# ---------- Utility helpers ----------
def cents_from_rate(rate: float) -> int:
    if rate is None or pd.isna(rate):
        raise ValueError("rate is NaN/None")
    return int(round(float(rate) * 100))


# =====================================================================
# MLB SCHEDULE — Fetch exact game start times from MLB Stats API
# =====================================================================
# statsapi.mlb.com must be in GitHub Actions network allowlist.
# Free API, no auth required.

_MLB_SCHEDULE_CACHE: Dict[str, int] = {}  # key: "YYYYMMDD-ABC-XYZ" → game_start_unix

def fetch_mlb_schedule() -> Dict[str, int]:
    """Fetch MLB schedule and build lookup: (date, teams) → game_start_utc_ts.
    
    Uses the MLB Stats API: https://statsapi.mlb.com/api/v1/schedule
    Called once per run. Returns dict keyed by "YYYYMMDD-{sorted_teams}"
    e.g. "20260401-BOS-NYY" → 1743033600
    
    Fetches today through 7 days out to cover upcoming games.
    """
    import urllib.request
    import json as _json
    
    schedule = {}
    
    # Fetch today through 7 days out
    today = datetime.now(UTC).date()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=7)).isoformat()
    
    url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1"
        f"&startDate={start_date}"
        f"&endDate={end_date}"
        f"&hydrate=team"
    )
    
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = _json.loads(resp.read())
        
        total_games = 0
        
        for date_entry in data.get('dates', []):
            for game in date_entry.get('games', []):
                game_date_str = game.get('gameDate', '')  # ISO format "2026-04-01T23:10:00Z"
                game_type = game.get('gameType', 'R')     # R=regular, S=spring, etc.
                
                # Get team abbreviations
                teams_info = game.get('teams', {})
                away_team = teams_info.get('away', {}).get('team', {})
                home_team = teams_info.get('home', {}).get('team', {})
                
                away_abbr = away_team.get('abbreviation', '')
                home_abbr = home_team.get('abbreviation', '')
                
                if not away_abbr or not home_abbr or not game_date_str:
                    continue
                
                # Map MLB API abbreviation to Kalshi code if needed
                away_code = MLB_TEAM_CODE_MAP.get(away_abbr, away_abbr)
                home_code = MLB_TEAM_CODE_MAP.get(home_abbr, home_abbr)
                
                # Parse game time
                try:
                    ts = game_date_str
                    if ts.endswith('Z'):
                        ts = ts[:-1] + '+00:00'
                    dt = datetime.fromisoformat(ts)
                    game_ts = int(dt.timestamp())
                except:
                    continue
                
                # Build key: date + sorted team codes (order-independent)
                game_date_key = dt.strftime('%Y%m%d')
                teams_sorted = sorted([away_code, home_code])
                key = f"{game_date_key}-{teams_sorted[0]}-{teams_sorted[1]}"
                schedule[key] = game_ts
                
                # Also store by previous day (games after midnight UTC)
                prev_date_key = (dt - timedelta(days=1)).strftime('%Y%m%d')
                key_prev = f"{prev_date_key}-{teams_sorted[0]}-{teams_sorted[1]}"
                if key_prev not in schedule:
                    schedule[key_prev] = game_ts
                
                # Also store with the original MLB API abbreviations as fallback
                if away_code != away_abbr or home_code != home_abbr:
                    orig_sorted = sorted([away_abbr, home_abbr])
                    orig_key = f"{game_date_key}-{orig_sorted[0]}-{orig_sorted[1]}"
                    if orig_key not in schedule:
                        schedule[orig_key] = game_ts
                    orig_key_prev = f"{prev_date_key}-{orig_sorted[0]}-{orig_sorted[1]}"
                    if orig_key_prev not in schedule:
                        schedule[orig_key_prev] = game_ts
                
                total_games += 1
        
        print(f"  [MLB] ✓ Loaded {total_games} games from MLB Stats API ({start_date} to {end_date})")
        
        # Log a sample of games for debugging
        if total_games > 0:
            sample_keys = list(schedule.keys())[:5]
            for k in sample_keys:
                ts = schedule[k]
                dt_display = datetime.fromtimestamp(ts, tz=UTC).strftime('%Y-%m-%d %H:%M UTC')
                print(f"    Sample: {k} → {dt_display}")
        
        return schedule
        
    except Exception as e:
        alert("SCHEDULE_FETCH_FAILED", f"MLB Stats API unreachable: {e}",
              {"fallback": "ticker_estimate"})
        print(f"  [MLB]   → Falling back to ticker-derived game times")
        return {}


def lookup_game_start(market_ticker: str, schedule: Dict[str, int]) -> Optional[int]:
    """Look up exact game start from MLB schedule cache.
    
    Uses parse_event_code() to correctly handle HHMM and 2-3 char teams.
    Ticker: KXMLBMENTION-26MAR252005NYYSF-JETE
    → date=2026-03-25, time=2005 ET, teams=NYY+SF
    → try keys: "20260325-NYY-SF", "20260326-NYY-SF", mapped variants
    """
    if not schedule:
        return None
    
    try:
        parsed = split_market_ticker(market_ticker)
        event_date = parsed.get("event_date", "")
        team_1 = parsed.get("team_1", "")
        team_2 = parsed.get("team_2", "")
        
        if not event_date or not team_1 or not team_2:
            return None
        
        game_date = datetime.strptime(event_date, "%Y-%m-%d")
        date_key = game_date.strftime('%Y%m%d')
        
        teams_sorted = sorted([team_1, team_2])
        key = f"{date_key}-{teams_sorted[0]}-{teams_sorted[1]}"
        
        if key in schedule:
            return schedule[key]
        
        # Try next day (evening ET games have UTC date = next day)
        next_date_key = (game_date + timedelta(days=1)).strftime('%Y%m%d')
        key_next = f"{next_date_key}-{teams_sorted[0]}-{teams_sorted[1]}"
        if key_next in schedule:
            return schedule[key_next]
        
        # Try with MLB_TEAM_CODE_MAP (Kalshi→MLB API) and reverse
        mapped_1 = MLB_TEAM_CODE_MAP.get(team_1, MLB_TEAM_CODE_MAP_REVERSE.get(team_1, team_1))
        mapped_2 = MLB_TEAM_CODE_MAP.get(team_2, MLB_TEAM_CODE_MAP_REVERSE.get(team_2, team_2))
        if mapped_1 != team_1 or mapped_2 != team_2:
            mapped_sorted = sorted([mapped_1, mapped_2])
            for dk in (date_key, next_date_key):
                mapped_key = f"{dk}-{mapped_sorted[0]}-{mapped_sorted[1]}"
                if mapped_key in schedule:
                    return schedule[mapped_key]
        
        return None
    except:
        return None


def estimate_game_start_from_ticker(market_ticker: str) -> Optional[int]:
    """Fallback: derive game start from the date + HHMM in the ticker.
    
    Ticker: KXMLBMENTION-26MAR252005NYYSF-JETE
    → date=2026-03-25, time=2005 → 8:05 PM ET → 00:05 UTC Mar 26
    
    If no HHMM in ticker, default to 7:10 PM ET.
    """
    try:
        parsed = split_market_ticker(market_ticker)
        event_date = parsed.get("event_date", "")
        ticker_time_et = parsed.get("ticker_time_et", "")
        
        if not event_date:
            return None
        
        game_date = datetime.strptime(event_date, "%Y-%m-%d")
        
        if ticker_time_et and len(ticker_time_et) == 4:
            # HHMM in ET (e.g. "2005" = 8:05 PM ET)
            hh = int(ticker_time_et[:2])
            mm = int(ticker_time_et[2:4])
            # Convert ET to UTC: +4h (EDT) or +5h (EST).
            # MLB regular season is mostly EDT. Use +4.
            utc_hour = hh + 4
            if utc_hour >= 24:
                game_start = game_date + timedelta(days=1, hours=utc_hour - 24, minutes=mm)
            else:
                game_start = game_date + timedelta(hours=utc_hour, minutes=mm)
        else:
            # No time in ticker — guess 7:10 PM ET = 11:10 PM UTC = +23h10m
            game_start = game_date + timedelta(hours=23, minutes=10)
        
        return int(game_start.timestamp())
    except Exception:
        return None


# Fetch MLB schedule once at startup
print("Fetching MLB schedule for exact game times...")
_MLB_SCHEDULE_CACHE = fetch_mlb_schedule()


def get_game_start_ts(market_ticker: str) -> Tuple[Optional[int], str]:
    """Get game start time. MLB API first, ticker estimate as fallback."""
    ts = lookup_game_start(market_ticker, _MLB_SCHEDULE_CACHE)
    if ts is not None:
        return ts, "mlb_api"
    
    ts = estimate_game_start_from_ticker(market_ticker)
    if ts is not None:
        alert("GAME_TIME_FALLBACK", f"MLB API miss, using ticker estimate",
              {"ticker": market_ticker, "fallback": "ticker_estimate"})
        return ts, "ticker_estimate"
    
    alert("GAME_TIME_MISSING", f"No game time found at all",
          {"ticker": market_ticker, "fallback": "none"})
    return None, "none"


def get_market_details(market_ticker: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int], str]:
    """Returns (expiration_ts, open_interest, volume, game_start_ts, time_source)."""
    try:
        market_response = exchange_client.get_market(ticker=market_ticker)
        market_data = market_response.get('market', {})

        game_start_ts, time_source = get_game_start_ts(market_ticker)
        
        if game_start_ts is None:
            close_time = market_data.get('close_time')
            if close_time:
                ts = close_time
                if ts.endswith('Z'):
                    ts = ts[:-1] + '+00:00'
                dt = datetime.fromisoformat(ts)
                game_start_ts = int(dt.timestamp())
                time_source = "close_time_fallback"
                alert("GAME_TIME_CLOSE_TIME", f"Using close_time (14-day offset, unreliable)",
                      {"ticker": market_ticker})
        
        # Order expiration timing
        SAFETY_BUFFER_SECONDS = 0
        expiration_ts = None
        if game_start_ts:
            if time_source == "mlb_api":
                expiration_ts = game_start_ts - SAFETY_BUFFER_SECONDS
            else:
                expiration_ts = game_start_ts - (EXPIRATION_HOURS_BEFORE_CLOSE * 3600)
            if expiration_ts <= time.time():
                expiration_ts = None

        # Handle renamed API fields
        open_interest = market_data.get('open_interest', None)
        if open_interest is None or open_interest == '':
            oi_fp = market_data.get('open_interest_fp', '0')
            open_interest = int(float(oi_fp)) if oi_fp else 0
        else:
            open_interest = int(float(open_interest))

        volume = market_data.get('volume', None)
        if volume is None or volume == '':
            vol_fp = market_data.get('volume_fp', '0')
            volume = int(float(vol_fp)) if vol_fp else 0
        else:
            volume = int(float(volume))

        return expiration_ts, open_interest, volume, game_start_ts, time_source

    except Exception as e:
        return None, None, None, None, "error"

def get_time_multiplier(hours_until_event: float) -> float:
    for threshold, multiplier in TIME_MULTIPLIERS:
        if hours_until_event < threshold:
            return multiplier
    return 0.5

def get_volume_multiplier(open_interest: int) -> float:
    for threshold, multiplier in VOLUME_MULTIPLIERS:
        if open_interest >= threshold:
            return multiplier
    return 0.4

def get_offsets_for_spread(spread_cents: int) -> Tuple[List[int], List[int], str]:
    for (min_spread, max_spread), config in SPREAD_CONFIGS.items():
        if min_spread <= spread_cents < max_spread:
            return config['yes'], config['no'], config['name']
    wide_config = SPREAD_CONFIGS[(15, 100)]
    return wide_config['yes'], wide_config['no'], wide_config['name']

def calculate_spread(yes_bid: int, no_bid: int) -> int:
    return 100 - (yes_bid + no_bid)


# ---------- BAYESIAN HYBRID PRICING ----------

def calculate_hybrid_price_bayesian(
    market_mid: float,
    market_yes_rate: Optional[float],
    market_sample_size: int,
    team_1_yes_rate: Optional[float],
    team_1_sample_size: int,
    team_2_yes_rate: Optional[float],
    team_2_sample_size: int,
    market_code: str = "",  # [v1.2] adaptive per-code weights
) -> Tuple[float, Dict[str, Any]]:
    intermediates: Dict[str, Any] = {}
    market_mid_rate = market_mid / 100

    if market_yes_rate is None or pd.isna(market_yes_rate):
        return market_mid_rate, {"fallback": "no_market_yes_rate"}

    # [v1.2] Use per-code adaptive weights if available, else global defaults
    if market_code and market_code in _BAYESIAN_WEIGHTS_PER_CODE:
        base_market_weight, base_historical_weight = _BAYESIAN_WEIGHTS_PER_CODE[market_code]
    else:
        base_market_weight = BAYESIAN_MARKET_WEIGHT
        base_historical_weight = BAYESIAN_HISTORICAL_WEIGHT

    team_rates = []
    team_weights = []

    if team_1_sample_size > 0 and team_1_yes_rate is not None and not pd.isna(team_1_yes_rate):
        team_rates.append(team_1_yes_rate)
        team_weights.append(team_1_sample_size)

    if team_2_sample_size > 0 and team_2_yes_rate is not None and not pd.isna(team_2_yes_rate):
        team_rates.append(team_2_yes_rate)
        team_weights.append(team_2_sample_size)

    if len(team_rates) > 0:
        total_team_weight = sum(team_weights)
        avg_team_yes_rate = sum(r * w for r, w in zip(team_rates, team_weights)) / total_team_weight
        combined_team_sample_size = total_team_weight
    else:
        avg_team_yes_rate = market_yes_rate
        combined_team_sample_size = 0

    team_credibility = min(combined_team_sample_size / BAYESIAN_K, 1.0) if combined_team_sample_size > 0 else 0.0

    if combined_team_sample_size > 0:
        posterior_rate = (
            (1 - team_credibility) * market_yes_rate +
            team_credibility * avg_team_yes_rate
        )
    else:
        posterior_rate = market_yes_rate

    n = max(market_sample_size, 0)
    recency_discount = 2 ** (-n / BAYESIAN_RECENCY_HALFLIFE_GAMES)
    historical_weight = base_historical_weight * (1.0 - 0.5 * (1.0 - recency_discount))
    market_weight = base_market_weight

    total_weight = market_weight + historical_weight
    market_weight /= total_weight
    historical_weight /= total_weight

    hybrid_yes_rate = (
        market_weight * market_mid_rate +
        historical_weight * posterior_rate
    )

    intermediates.update({
        "bayesian_market_mid_input": round(market_mid_rate, 4),
        "bayesian_team_credibility": round(team_credibility, 4),
        "bayesian_avg_team_yes_rate": round(avg_team_yes_rate, 4) if avg_team_yes_rate is not None else None,
        "bayesian_combined_team_sample_size": combined_team_sample_size,
        "bayesian_posterior_rate": round(posterior_rate, 4) if posterior_rate is not None else None,
        "bayesian_recency_discount": round(recency_discount, 4),
        "bayesian_final_market_weight": round(market_weight, 4),
        "bayesian_final_historical_weight": round(historical_weight, 4),
        "bayesian_adaptive_code": market_code,
    })

    return hybrid_yes_rate, intermediates


def calculate_bid_price(
    market_mid_yes: Optional[float],
    market_yes_rate: Optional[float],
    team_1_yes_rate: Optional[float],
    team_1_sample_size: int,
    team_2_yes_rate: Optional[float],
    team_2_sample_size: int,
    market_sample_size: int,
    strategy: str,
    market_code: str = "",  # [v1.2] for adaptive per-code weights
) -> Tuple[Optional[int], Optional[int], Dict[str, Any]]:
    empty_intermediates: Dict[str, Any] = {}

    if strategy == 'market':
        if market_mid_yes is not None:
            yes_price = int(round(market_mid_yes))
            return (yes_price, 100 - yes_price, empty_intermediates)
        return (None, None, empty_intermediates)

    elif strategy == 'historical':
        if market_yes_rate is None or pd.isna(market_yes_rate):
            if market_mid_yes is not None:
                yes_price = int(round(market_mid_yes))
                return (yes_price, 100 - yes_price, empty_intermediates)
            return (None, None, empty_intermediates)
        yes_price = cents_from_rate(market_yes_rate)
        no_price = 100 - yes_price
        return (yes_price, no_price, empty_intermediates)

    elif strategy == 'hybrid':
        if market_mid_yes is None:
            if market_yes_rate is None or pd.isna(market_yes_rate):
                return (None, None, empty_intermediates)
            yes_price = cents_from_rate(market_yes_rate)
            no_price = 100 - yes_price
            return (yes_price, no_price, {"fallback": "no_orderbook"})

        hybrid_yes_rate, intermediates = calculate_hybrid_price_bayesian(
            market_mid=market_mid_yes,
            market_yes_rate=market_yes_rate,
            market_sample_size=market_sample_size,
            team_1_yes_rate=team_1_yes_rate,
            team_1_sample_size=team_1_sample_size,
            team_2_yes_rate=team_2_yes_rate,
            team_2_sample_size=team_2_sample_size,
            market_code=market_code,
        )

        yes_price = int(hybrid_yes_rate * 100)
        no_price = 100 - yes_price
        return (yes_price, no_price, intermediates)

    else:
        raise ValueError(f"Unknown pricing strategy: {strategy}")


def parse_orderbook(orderbook_data, is_dollars: bool = False) -> Tuple[Optional[int], Optional[int]]:
    """Parse orderbook data. Handles multiple formats:
       - List of lists (cents):   [[50, 10], [48, 20]]  →  best=50c, size=10
       - List of lists (dollars): [['0.50', '10.00'], ['0.48', '20.00']]  →  best=50c, size=10
       - List of dicts:           [{"price": 50, "quantity": 10}, ...]
       
       When is_dollars=True, prices are in dollar strings and get converted to cents.
    """
    if not orderbook_data or len(orderbook_data) == 0:
        return None, None

    first = orderbook_data[0]

    if isinstance(first, (list, tuple)):
        # [[price, qty], ...] — price may be int cents or string dollars
        if is_dollars:
            # Dollar strings: [['0.1300', '1557.00'], ...] → cents
            prices_cents = [int(round(float(level[0]) * 100)) for level in orderbook_data]
            best_idx = prices_cents.index(max(prices_cents))
            best_bid = prices_cents[best_idx]
            bid_size = int(float(orderbook_data[best_idx][1]))
        else:
            # Integer cents: [[50, 10], ...]
            best_bid = max(int(level[0]) for level in orderbook_data)
            best_bid_idx = next(i for i, level in enumerate(orderbook_data) if int(level[0]) == best_bid)
            bid_size = int(orderbook_data[best_bid_idx][1])
        return best_bid, bid_size

    elif isinstance(first, dict):
        price_key = None
        for pk in ('price', 'yes_price', 'no_price', 'limit_price'):
            if pk in first:
                price_key = pk
                break
        qty_key = None
        for qk in ('quantity', 'count', 'size', 'count_fp', 'contracts'):
            if qk in first:
                qty_key = qk
                break

        if price_key is None:
            return None, None

        best_entry = max(orderbook_data, key=lambda x: float(x.get(price_key, 0)))
        raw_price = float(best_entry[price_key])
        best_bid = int(round(raw_price * 100)) if is_dollars or raw_price <= 1.0 else int(raw_price)
        bid_size = int(float(best_entry.get(qty_key, 0))) if qty_key else 0
        return best_bid, bid_size

    else:
        return None, None


market_snapshots = []

# =====================================================================
# BUILD ORDERS — v1.0 with discovery mode
# =====================================================================

def build_order_objects_for_market(
    market_row: Dict[str, Any],
    existing_positions: Dict[str, Dict[str, int]],
    event_net_exposure: Dict[str, int],
    event_orders_placed: Dict[str, int],
    safe_mode: bool,
    ledger: Dict[str, Dict[str, Any]],
    event_market_sides: Dict[str, set] = None,
) -> List[Dict[str, Any]]:
    """Build YES and NO orders with ledger-aware sizing + discovery mode for unknown codes."""
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

    if market_status != "active":
        return []

    # Get side multipliers
    orders_blocked = False
    blocked_reason = None
    is_discovery = False

    if ticker_part_3_market_code in SIDE_MULTIPLIERS:
        side_config = SIDE_MULTIPLIERS[ticker_part_3_market_code]
        yes_side_mult_base = side_config.get("yes", 0.0)
        no_side_mult_base = side_config.get("no", 0.0)

        if yes_side_mult_base == 0.0 and no_side_mult_base == 0.0:
            print(f"    ⛔ BOTH SIDES 0x for {ticker_part_3_market_code} — data-only")
            orders_blocked = True
            blocked_reason = "both_sides_0x"
    elif DISCOVERY_MODE:
        # Unknown code in discovery mode: trade at reduced size on both sides
        yes_side_mult_base = DISCOVERY_SIDE_MULT
        no_side_mult_base = DISCOVERY_SIDE_MULT
        is_discovery = True
        print(f"    🔍 DISCOVERY: {ticker_part_3_market_code} not in SIDE_MULTIPLIERS — trading at {DISCOVERY_SIDE_MULT}x both sides")
    else:
        # Unknown code, discovery off: block
        print(f"    ⛔ UNKNOWN CODE {ticker_part_3_market_code} — not in SIDE_MULTIPLIERS, data-only")
        orders_blocked = True
        blocked_reason = "unknown_code"
        yes_side_mult_base = 0.0
        no_side_mult_base = 0.0

    # For blocked markets: still collect orderbook/market data for analytics
    if orders_blocked:
        expiration_ts, open_interest, volume, game_start_ts, _time_src = get_market_details(ticker)
        hours_until_event = (game_start_ts - time.time()) / 3600.0 if game_start_ts else 0

        orderbook_yes_bid, orderbook_no_bid = None, None
        try:
            orderbook_response = exchange_client.get_orderbook(ticker=ticker, depth=10)
            yes_data = []
            no_data = []
            ob_is_dollars = False
            if 'orderbook' in orderbook_response:
                ob = orderbook_response['orderbook']
                yes_data = ob.get('yes', [])
                no_data = ob.get('no', [])
            if (not yes_data and not no_data) and 'orderbook_fp' in orderbook_response:
                ob = orderbook_response['orderbook_fp']
                yes_data = ob.get('yes_dollars', [])
                no_data = ob.get('no_dollars', [])
                ob_is_dollars = True
            if yes_data and len(yes_data) > 0:
                orderbook_yes_bid, _ = parse_orderbook(yes_data, is_dollars=ob_is_dollars)
            if no_data and len(no_data) > 0:
                orderbook_no_bid, _ = parse_orderbook(no_data, is_dollars=ob_is_dollars)
        except:
            pass

        market_mid_yes = None
        if orderbook_yes_bid is not None and orderbook_no_bid is not None:
            market_mid_yes = (orderbook_yes_bid + (100 - orderbook_no_bid)) / 2.0
        elif orderbook_yes_bid is not None:
            market_mid_yes = float(orderbook_yes_bid)
        elif orderbook_no_bid is not None:
            market_mid_yes = 100.0 - orderbook_no_bid

        market_snapshots.append({
            "pulled_at": datetime.now(UTC).isoformat(),
            "run_id": RUN_ID,
            "model_version": MODEL_VERSION,
            "market_ticker": ticker,
            "event_ticker": event_ticker,
            "market_status": market_status,
            "event_start_ts": expiration_ts,
            "game_start_ts": game_start_ts,
            "ticker_part_3_market_code": ticker_part_3_market_code,
            "team_1": team_1,
            "team_2": team_2,
            "yes_best_bid": orderbook_yes_bid,
            "no_best_bid": orderbook_no_bid,
            "market_mid_yes": round(market_mid_yes, 1) if market_mid_yes else None,
            "volume": volume or 0,
            "open_interest": open_interest or 0,
            "minutes_to_event_start": hours_until_event * 60 if hours_until_event else None,
            "strategy_name": PRICING_STRATEGY,
            "market_yes_rate": market_yes_rate,
            "market_sample_size": market_sample_size,
            "orders_blocked": True,
            "blocked_reason": blocked_reason,
            "yes_total_mult": 0.0,
            "no_total_mult": 0.0,
            "is_discovery": False,
        })
        return []

    # Detect pairing mode
    position_data = existing_positions.get(ticker, {'net_position': 0})
    net_position = position_data['net_position']
    ledger_data = ledger.get(ticker)

    pairing_mode_str, imbalance_ratio = get_pairing_mode(ticker, ledger, net_position)

    if ledger_data is not None:
        net_side = ledger_data.get("net_side", "flat")
    else:
        net_side = "yes" if net_position > 0 else ("no" if net_position < 0 else "flat")

    if pairing_mode_str != "normal":
        print(f"    🔄 PAIRING MODE: {pairing_mode_str.upper()} (imbalance: {imbalance_ratio:.0%}, net: {net_side})")
        if ledger_data:
            print(f"       Ledger: YES={ledger_data['yes_qty']}@{ledger_data['yes_avg_price']:.1f}c "
                  f"NO={ledger_data['no_qty']}@{ledger_data['no_avg_price']:.1f}c "
                  f"paired={ledger_data['paired_qty']} edge={ledger_data['paired_edge']:.1f}c")

    # Override side multipliers in pairing mode
    yes_side_mult = get_effective_side_multiplier(yes_side_mult_base, "yes", pairing_mode_str, net_side)
    no_side_mult = get_effective_side_multiplier(no_side_mult_base, "no", pairing_mode_str, net_side)

    expiration_ts, open_interest, volume, game_start_ts, time_source = get_market_details(ticker)

    if expiration_ts is None:
        print(f"    ✗ No valid expiration time — skipping")
        return []

    hours_until_event = (game_start_ts - time.time()) / 3600.0 if game_start_ts else 0
    minutes_to_event_start = hours_until_event * 60

    if open_interest is None:
        open_interest = 0
    if volume is None:
        volume = 0

    # Ledger-aware position multipliers
    yes_side_mult_floor = 0.0
    no_side_mult_floor = 0.0

    if ledger:
        yes_position_mult, yes_pos_reason, yes_side_mult_floor = calculate_ledger_position_multiplier(
            ticker, 'yes', ledger, net_position
        )
        no_position_mult, no_pos_reason, no_side_mult_floor = calculate_ledger_position_multiplier(
            ticker, 'no', ledger, net_position
        )
        print(f"    Ledger sizing: YES={yes_position_mult:.2f}x ({yes_pos_reason}) "
              f"NO={no_position_mult:.2f}x ({no_pos_reason})")
    else:
        yes_position_mult = calculate_position_multiplier(net_position, 'yes')
        no_position_mult = calculate_position_multiplier(net_position, 'no')

    # Apply side multiplier floors
    yes_side_mult = yes_side_mult_base
    no_side_mult = no_side_mult_base

    if yes_side_mult_floor > 0 and yes_side_mult < yes_side_mult_floor:
        yes_side_mult = yes_side_mult_floor
    if no_side_mult_floor > 0 and no_side_mult < no_side_mult_floor:
        no_side_mult = no_side_mult_floor

    yes_side_mult = get_effective_side_multiplier(yes_side_mult, "yes", pairing_mode_str, net_side)
    no_side_mult = get_effective_side_multiplier(no_side_mult, "no", pairing_mode_str, net_side)

    net_room_yes = MAX_NET_PER_MARKET - net_position
    net_room_no = MAX_NET_PER_MARKET + net_position

    time_mult = get_time_multiplier(hours_until_event)
    volume_mult = get_volume_multiplier(open_interest)
    base_mult = time_mult * volume_mult

    # Team multiplier
    team_1_mult = TEAM_MULTIPLIERS.get(team_1, 1.0)
    team_2_mult = TEAM_MULTIPLIERS.get(team_2, 1.0)
    if team_1_mult < 1.0 or team_2_mult < 1.0:
        team_mult = min(team_1_mult, team_2_mult)
    else:
        team_mult = max(team_1_mult, team_2_mult)
    if team_mult != 1.0:
        base_mult *= team_mult
        print(f"    Team mult: {team_mult:.1f}x (teams: {team_1}, {team_2})")

    # Correlation penalty
    corr_penalty_yes = 1.0
    corr_penalty_no = 1.0
    if event_market_sides is not None:
        evt = event_ticker or ""
        placed = event_market_sides.get(evt, set())
        for (mkt_a, mkt_b), r in CORRELATED_PAIRS.items():
            partner = None
            if ticker_part_3_market_code == mkt_a:
                partner = mkt_b
            elif ticker_part_3_market_code == mkt_b:
                partner = mkt_a
            if partner:
                if (partner, "yes") in placed and yes_side_mult > 0:
                    corr_penalty_yes = min(corr_penalty_yes, CORRELATION_PENALTY)
                if (partner, "no") in placed and no_side_mult > 0:
                    corr_penalty_no = min(corr_penalty_no, CORRELATION_PENALTY)

    safe_mult = SAFE_MODE_MULTIPLIER if safe_mode else 1.0

    # [v1.0] Flat sizing — no dynamic multipliers. Only 0x side blocks apply.
    yes_total_mult = (1.0 if yes_side_mult > 0 else 0.0) * safe_mult
    no_total_mult = (1.0 if no_side_mult > 0 else 0.0) * safe_mult

    safe_tag = " [SAFE MODE]" if safe_mode else ""
    disc_tag = " [DISCOVERY]" if is_discovery else ""
    position_status = f" | Pos: {abs(net_position)} net {'YES' if net_position > 0 else 'NO'}" if net_position != 0 else ""
    print(f"    Time: {hours_until_event:.1f}h [{time_source}] | OI: {open_interest} | Mult: YES={yes_total_mult:.1f}x NO={no_total_mult:.1f}x{position_status}{safe_tag}{disc_tag}")

    # Get orderbook
    orderbook_yes_bid = None
    yes_bid_size = None
    orderbook_no_bid = None
    no_bid_size = None

    try:
        orderbook_response = exchange_client.get_orderbook(ticker=ticker, depth=10)
        
        # Handle both old and new Kalshi API formats:
        #   Old: response['orderbook']['yes'] / ['no'] — [[cents, qty], ...]
        #   New: response['orderbook_fp']['yes_dollars'] / ['no_dollars'] — [['0.50', '10.00'], ...]
        yes_data = []
        no_data = []
        ob_is_dollars = False
        
        if 'orderbook' in orderbook_response:
            ob = orderbook_response['orderbook']
            yes_data = ob.get('yes', [])
            no_data = ob.get('no', [])
        
        if (not yes_data and not no_data) and 'orderbook_fp' in orderbook_response:
            ob = orderbook_response['orderbook_fp']
            yes_data = ob.get('yes_dollars', [])
            no_data = ob.get('no_dollars', [])
            ob_is_dollars = True

        if yes_data and len(yes_data) > 0:
            orderbook_yes_bid, yes_bid_size = parse_orderbook(yes_data, is_dollars=ob_is_dollars)
        if no_data and len(no_data) > 0:
            orderbook_no_bid, no_bid_size = parse_orderbook(no_data, is_dollars=ob_is_dollars)

        if orderbook_yes_bid is None and orderbook_no_bid is None:
            alert("ORDERBOOK_EMPTY", f"No bids on either side",
                  {"ticker": ticker})
        print(f"    Orderbook: Yes={orderbook_yes_bid if orderbook_yes_bid else 'N/A'}c, No={orderbook_no_bid if orderbook_no_bid else 'N/A'}c")

    except Exception as e:
        alert("ORDERBOOK_ERROR", f"Orderbook fetch failed: {e}",
              {"ticker": ticker})
        import traceback
        traceback.print_exc()

    # Mid-price anchor
    market_mid_yes = None
    if orderbook_yes_bid is not None and orderbook_no_bid is not None:
        yes_implied_from_no = 100 - orderbook_no_bid
        market_mid_yes = (orderbook_yes_bid + yes_implied_from_no) / 2.0
        print(f"    Mid-price: ({orderbook_yes_bid} + {yes_implied_from_no}) / 2 = {market_mid_yes:.1f}c")
    elif orderbook_yes_bid is not None:
        market_mid_yes = float(orderbook_yes_bid)
    elif orderbook_no_bid is not None:
        market_mid_yes = 100.0 - orderbook_no_bid

    # Fair-value prices
    yes_fair, no_fair, bayesian_intermediates = calculate_bid_price(
        market_mid_yes=market_mid_yes,
        market_yes_rate=market_yes_rate,
        team_1_yes_rate=team_1_yes_rate,
        team_1_sample_size=team_1_sample_size,
        team_2_yes_rate=team_2_yes_rate,
        team_2_sample_size=team_2_sample_size,
        market_sample_size=market_sample_size,
        strategy=PRICING_STRATEGY,
        market_code=ticker_part_3_market_code,  # [v1.2] adaptive per-code weights
    )

    if PRICING_STRATEGY == 'hybrid':
        team_info = []
        if team_1_yes_rate is not None and not pd.isna(team_1_yes_rate):
            team_info.append(f"{team_1}={team_1_yes_rate:.2%}(n={team_1_sample_size})")
        if team_2_yes_rate is not None and not pd.isna(team_2_yes_rate):
            team_info.append(f"{team_2}={team_2_yes_rate:.2%}(n={team_2_sample_size})")
        team_display = ", ".join(team_info) if team_info else "no team data"
        if market_yes_rate is not None and not pd.isna(market_yes_rate):
            print(f"    Strategy [Bayesian]: Market={market_yes_rate:.2%} (n={market_sample_size})")
        print(f"                         Teams: {team_display}")
        if yes_fair is not None and no_fair is not None:
            print(f"    → Fair value YES={yes_fair}c, NO={no_fair}c (sum={yes_fair+no_fair}c)")

    if yes_fair is None and market_yes_rate is not None and not pd.isna(market_yes_rate):
        yes_fair = cents_from_rate(market_yes_rate)
        no_fair = 100 - yes_fair

    if yes_fair is None or no_fair is None:
        print(f"    ✗ No data — skipping")
        return []

    p_hat = yes_fair / 100.0

    # Price floors — conditional on pairing mode
    if pairing_mode_str == "aggressive_pairing":
        active_min_price_yes = HEDGE_MIN_PRICE_YES
        active_min_price_no = HEDGE_MIN_PRICE_NO
        active_yes_min_price = HEDGE_YES_MIN_PRICE
    elif pairing_mode_str == "pairing":
        active_min_price_yes = (MIN_PRICE_YES + HEDGE_MIN_PRICE_YES) // 2
        active_min_price_no = (MIN_PRICE_NO + HEDGE_MIN_PRICE_NO) // 2
        active_yes_min_price = (YES_MIN_PRICE + HEDGE_YES_MIN_PRICE) // 2
    else:
        active_min_price_yes = MIN_PRICE_YES
        active_min_price_no = MIN_PRICE_NO
        active_yes_min_price = YES_MIN_PRICE

    # Probability floor
    yes_prob_blocked = False
    if yes_fair < YES_PROBABILITY_FLOOR and yes_side_mult > 0:
        if pairing_mode_str != "normal" and net_side == "no":
            pass  # Bypass for hedge
        else:
            print(f"    ⛔ YES blocked: fair={yes_fair}c < prob_floor={YES_PROBABILITY_FLOOR}c")
            yes_side_mult = 0.0
            yes_total_mult = 0.0
            yes_prob_blocked = True

    # Spread
    spread_gated = False
    if orderbook_yes_bid is not None and orderbook_no_bid is not None:
        spread_cents = calculate_spread(orderbook_yes_bid, orderbook_no_bid)
        if spread_cents < MIN_SPREAD_BOTH_SIDES:
            spread_gated = True
            if yes_total_mult > no_total_mult:
                no_side_mult = 0.0
                no_total_mult = 0.0
            elif no_total_mult > yes_total_mult:
                yes_side_mult = 0.0
                yes_total_mult = 0.0
            else:
                return []
    else:
        spread_cents = calculate_spread(yes_fair, no_fair)

    # Offsets
    yes_offsets, no_offsets, spread_config_name = get_offsets_for_spread(spread_cents)

    if ticker_part_3_market_code in TOP_NO_MARKETS:
        no_offsets = generate_offsets(start=1, increment=1, count=NUM_NO_OFFSET_LEVELS)
        if spread_config_name not in ('very_tight', 'tight'):
            spread_config_name = f"{spread_config_name}+top_no"

    if pairing_mode_str != "normal" and net_side != "flat":
        if net_side == "yes":
            no_offsets = PAIRING_OFFSETS['no']
            spread_config_name = f"{spread_config_name}+pair_no"
        elif net_side == "no":
            yes_offsets = PAIRING_OFFSETS['yes']
            spread_config_name = f"{spread_config_name}+pair_yes"

    # Event-level caps
    evt = event_ticker or ""
    current_event_net = event_net_exposure.get(evt, 0)
    current_event_orders = event_orders_placed.get(evt, 0)
    event_orders_remaining = MAX_ORDERS_PER_EVENT - current_event_orders

    if event_orders_remaining <= 0:
        return []

    # Save snapshot
    snapshot = {
        "pulled_at": datetime.now(UTC).isoformat(),
        "run_id": RUN_ID,
        "model_version": MODEL_VERSION,
        "market_ticker": ticker,
        "event_ticker": event_ticker,
        "market_status": market_status,
        "event_start_ts": expiration_ts,
        "game_start_ts": game_start_ts,
        "time_source": time_source,
        "ticker_part_3_market_code": ticker_part_3_market_code,
        "team_1": team_1,
        "team_2": team_2,
        "yes_best_bid": orderbook_yes_bid,
        "no_best_bid": orderbook_no_bid,
        "yes_bid_size": yes_bid_size,
        "no_bid_size": no_bid_size,
        "market_mid_yes": round(market_mid_yes, 1) if market_mid_yes else None,
        "volume": volume,
        "open_interest": open_interest,
        "minutes_to_event_start": minutes_to_event_start,
        "strategy_name": PRICING_STRATEGY,
        "market_yes_rate": market_yes_rate,
        "market_sample_size": market_sample_size,
        "team_1_yes_rate": team_1_yes_rate,
        "team_1_sample_size": team_1_sample_size,
        "team_2_yes_rate": team_2_yes_rate,
        "team_2_sample_size": team_2_sample_size,
        "yes_fair": yes_fair,
        "no_fair": no_fair,
        "p_hat": round(p_hat, 4),
        "net_position": net_position,
        "yes_side_mult": yes_side_mult,
        "no_side_mult": no_side_mult,
        "yes_side_mult_base": yes_side_mult_base,
        "no_side_mult_base": no_side_mult_base,
        "yes_position_mult": yes_position_mult,
        "no_position_mult": no_position_mult,
        "yes_total_mult": round(yes_total_mult, 4),
        "no_total_mult": round(no_total_mult, 4),
        "yes_prob_blocked": yes_prob_blocked,
        "spread_gated": spread_gated,
        "spread_config": spread_config_name,
        "spread_cents": spread_cents,
        "safe_mode": safe_mode,
        "event_net_exposure": current_event_net,
        "event_orders_placed": current_event_orders,
        "pairing_mode": pairing_mode_str,
        "imbalance_ratio": round(imbalance_ratio, 4),
        "ledger_yes_qty": ledger_data["yes_qty"] if ledger_data else None,
        "ledger_no_qty": ledger_data["no_qty"] if ledger_data else None,
        "ledger_paired_qty": ledger_data["paired_qty"] if ledger_data else None,
        "ledger_paired_edge": ledger_data["paired_edge"] if ledger_data else None,
        "ledger_net_side": ledger_data["net_side"] if ledger_data else None,
        "orders_blocked": False,
        "blocked_reason": None,
        "is_discovery": is_discovery,
    }
    snapshot.update(bayesian_intermediates)
    market_snapshots.append(snapshot)

    orders: List[Dict[str, Any]] = []

    # ==========================================================
    # YES limit orders
    # ==========================================================
    yes_prices_to_place = []
    max_yes_price_allowed = (orderbook_yes_bid + MAX_ORDERBOOK_LEVELS_ABOVE) if orderbook_yes_bid else MAX_PRICE
    yes_contracts_placed = 0

    if yes_total_mult > 0:
        for i, offset in enumerate(yes_offsets):
            if i >= len(BASE_YES_CONTRACTS):
                break

            bid_price = yes_fair - offset

            if bid_price > max_yes_price_allowed:
                continue
            if bid_price < active_min_price_yes or bid_price > MAX_PRICE:
                continue
            if bid_price < active_yes_min_price:
                continue

            standalone_ev = p_hat - (bid_price / 100.0)
            hedge_ev, is_pairing = calculate_hedge_ev("yes", bid_price, ledger_data)
            effective_ev = max(standalone_ev, hedge_ev) if is_pairing else standalone_ev

            if effective_ev < MIN_EV_PER_ORDER:
                continue

            base_contracts = BASE_YES_CONTRACTS[i]
            scaled_contracts = max(1, round(base_contracts * yes_total_mult))

            max_here = min(
                scaled_contracts,
                MAX_CONTRACTS_PER_ORDER,
                max(0, net_room_yes - yes_contracts_placed),
                max(0, event_orders_remaining - yes_contracts_placed),
                max(0, MAX_CONTRACTS_PER_MARKET_PER_RUN - yes_contracts_placed),
            )

            # Pairing overshoot cap
            if is_pairing and ledger_data and net_side == "no":
                remaining_hedge_room = ledger_data['no_qty'] - ledger_data['yes_qty'] - yes_contracts_placed
                max_here = min(max_here, max(0, remaining_hedge_room))

            if max_here < 1:
                continue

            ev_tag = "H" if is_pairing and hedge_ev > standalone_ev else "S"
            yes_prices_to_place.append((bid_price, max_here, round(effective_ev, 4), ev_tag))
            yes_contracts_placed += max_here

            orders.append({
                "ticker": ticker,
                "action": "buy",
                "side": "yes",
                "count": max_here,
                "type": "limit",
                "yes_price": bid_price,
                "no_price": None,
                "expiration_ts": expiration_ts,
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "run_id": RUN_ID,
                "model_version": MODEL_VERSION,
                "spread_config": spread_config_name,
                "spread_cents": spread_cents,
                "time_multiplier": time_mult,
                "volume_multiplier": volume_mult,
                "position_multiplier": yes_position_mult,
                "side_multiplier": yes_side_mult,
                "boost_multiplier": yes_side_mult,
                "total_multiplier": yes_total_mult,
                "base_contracts": base_contracts,
                "hours_until_event": hours_until_event,
                "open_interest": open_interest,
                "net_position": net_position,
                "ev": round(effective_ev, 4),
                "standalone_ev": round(standalone_ev, 4),
                "hedge_ev": round(hedge_ev, 4) if is_pairing else None,
                "is_pairing_order": is_pairing,
                "pairing_mode": pairing_mode_str,
                "p_hat": round(p_hat, 4),
                "pricing_strategy": PRICING_STRATEGY,
                "safe_mode": safe_mode,
                "event_ticker": event_ticker,
                "is_discovery": is_discovery,
            })

    if yes_prices_to_place:
        formatted = [f"{p}c({c}c,ev={ev:.0%}{t})" for p, c, ev, t in yes_prices_to_place[:5]]
        if len(yes_prices_to_place) > 5:
            formatted.append(f"... +{len(yes_prices_to_place)-5} more")
        hedge_count = sum(1 for _, _, _, t in yes_prices_to_place if t == "H")
        hedge_tag = f" [{hedge_count} hedge-EV]" if hedge_count > 0 else ""
        disc_tag = " [DISC]" if is_discovery else ""
        print(f"    → YES: {formatted} (mult: {yes_total_mult:.2f}x, placed: {yes_contracts_placed}){hedge_tag}{disc_tag}")
    elif yes_total_mult == 0:
        if yes_prob_blocked:
            print(f"    → YES: [0x — prob floor: fair={yes_fair}c < {YES_PROBABILITY_FLOOR}c]")
        elif ticker_part_3_market_code in SIDE_MULTIPLIERS and SIDE_MULTIPLIERS[ticker_part_3_market_code].get('yes', 0) == 0:
            print(f"    → YES: [0x — blocked by calibration/config]")
        else:
            print(f"    → YES: [0x multiplier]")
    else:
        print(f"    → YES: [no orders passed EV/price filters]")

    # ==========================================================
    # NO limit orders
    # ==========================================================
    no_prices_to_place = []
    market_max_price = MAX_PRICE_OVERRIDES.get(ticker_part_3_market_code, MAX_PRICE)
    max_no_price_allowed = (orderbook_no_bid + MAX_ORDERBOOK_LEVELS_ABOVE) if orderbook_no_bid else market_max_price
    no_contracts_placed = 0

    if no_total_mult > 0:
        for i, offset in enumerate(no_offsets):
            if i >= len(BASE_NO_CONTRACTS):
                break

            bid_price = no_fair - offset

            if bid_price > max_no_price_allowed:
                continue
            if bid_price < active_min_price_no or bid_price > market_max_price:
                continue

            implied_yes_price = 100 - bid_price
            if implied_yes_price < NO_MIN_YES_PRICE or implied_yes_price > NO_MAX_YES_PRICE:
                continue

            standalone_ev = (1.0 - p_hat) - (bid_price / 100.0)
            hedge_ev, is_pairing = calculate_hedge_ev("no", bid_price, ledger_data)
            effective_ev = max(standalone_ev, hedge_ev) if is_pairing else standalone_ev

            if effective_ev < MIN_EV_PER_ORDER:
                continue

            base_contracts = BASE_NO_CONTRACTS[i]
            scaled_contracts = max(1, round(base_contracts * no_total_mult))

            sweet_spot_applied = False
            if NO_SWEET_SPOT_MIN <= bid_price <= NO_SWEET_SPOT_MAX:
                scaled_contracts = max(1, round(scaled_contracts * NO_SWEET_SPOT_MULTIPLIER))
                sweet_spot_applied = True

            max_here = min(
                scaled_contracts,
                MAX_CONTRACTS_PER_ORDER,
                max(0, net_room_no - no_contracts_placed),
                max(0, event_orders_remaining - yes_contracts_placed - no_contracts_placed),
                max(0, MAX_CONTRACTS_PER_MARKET_PER_RUN - no_contracts_placed),
            )

            # Pairing overshoot cap
            if is_pairing and ledger_data and net_side == "yes":
                remaining_hedge_room = ledger_data['yes_qty'] - ledger_data['no_qty'] - no_contracts_placed
                max_here = min(max_here, max(0, remaining_hedge_room))

            if max_here < 1:
                continue

            ev_tag = "H" if is_pairing and hedge_ev > standalone_ev else "S"
            no_prices_to_place.append((bid_price, max_here, round(effective_ev, 4), ev_tag))
            no_contracts_placed += max_here

            orders.append({
                "ticker": ticker,
                "action": "buy",
                "side": "no",
                "count": max_here,
                "type": "limit",
                "yes_price": None,
                "no_price": bid_price,
                "expiration_ts": expiration_ts,
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "run_id": RUN_ID,
                "model_version": MODEL_VERSION,
                "spread_config": spread_config_name,
                "spread_cents": spread_cents,
                "time_multiplier": time_mult,
                "volume_multiplier": volume_mult,
                "position_multiplier": no_position_mult,
                "side_multiplier": no_side_mult,
                "boost_multiplier": no_side_mult,
                "total_multiplier": no_total_mult,
                "sweet_spot_applied": sweet_spot_applied,
                "base_contracts": base_contracts,
                "hours_until_event": hours_until_event,
                "open_interest": open_interest,
                "net_position": net_position,
                "ev": round(effective_ev, 4),
                "standalone_ev": round(standalone_ev, 4),
                "hedge_ev": round(hedge_ev, 4) if is_pairing else None,
                "is_pairing_order": is_pairing,
                "pairing_mode": pairing_mode_str,
                "p_hat": round(p_hat, 4),
                "pricing_strategy": PRICING_STRATEGY,
                "safe_mode": safe_mode,
                "event_ticker": event_ticker,
                "is_discovery": is_discovery,
            })

    if no_prices_to_place:
        formatted = [f"{p}c({c}c,ev={ev:.0%}{t})" for p, c, ev, t in no_prices_to_place[:5]]
        if len(no_prices_to_place) > 5:
            formatted.append(f"... +{len(no_prices_to_place)-5} more")
        hedge_count = sum(1 for _, _, _, t in no_prices_to_place if t == "H")
        hedge_tag = f" [{hedge_count} hedge-EV]" if hedge_count > 0 else ""
        disc_tag = " [DISC]" if is_discovery else ""
        print(f"    → NO:  {formatted} (mult: {no_total_mult:.2f}x, placed: {no_contracts_placed}){hedge_tag}{disc_tag}")
    elif no_total_mult == 0:
        if ticker_part_3_market_code in SIDE_MULTIPLIERS and SIDE_MULTIPLIERS[ticker_part_3_market_code].get('no', 0) == 0:
            print(f"    → NO:  [0x multiplier — blocked by calibration/config]")
        else:
            print(f"    → NO:  [0x multiplier]")
    else:
        print(f"    → NO:  [no orders passed EV/price filters]")

    # Update event-level trackers
    total_placed = yes_contracts_placed + no_contracts_placed
    event_orders_placed[evt] = event_orders_placed.get(evt, 0) + total_placed
    event_net_exposure[evt] = event_net_exposure.get(evt, 0) + abs(yes_contracts_placed - no_contracts_placed)

    if event_market_sides is not None and total_placed > 0:
        if evt not in event_market_sides:
            event_market_sides[evt] = set()
        if yes_contracts_placed > 0:
            event_market_sides[evt].add((ticker_part_3_market_code, "yes"))
        if no_contracts_placed > 0:
            event_market_sides[evt].add((ticker_part_3_market_code, "no"))

    return orders


# ---------- Submit orders ----------
def submit_orders_sequential(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    total = len(orders)

    print(f"Submitting {total} orders...")
    start_time = time.time()

    for i, order_params in enumerate(orders):
        if i % 50 == 0 and i > 0:
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (total - i) / rate if rate > 0 else 0
            print(f"  Progress: {i}/{total} ({i*100//total}%) - ~{remaining:.0f}s remaining")

        metadata = {
            "run_id": order_params.pop("run_id"),
            "model_version": order_params.pop("model_version"),
            "spread_config": order_params.pop("spread_config"),
            "spread_cents": order_params.pop("spread_cents"),
            "time_multiplier": order_params.pop("time_multiplier"),
            "volume_multiplier": order_params.pop("volume_multiplier"),
            "position_multiplier": order_params.pop("position_multiplier"),
            "side_multiplier": order_params.pop("side_multiplier"),
            "boost_multiplier": order_params.pop("boost_multiplier"),
            "total_multiplier": order_params.pop("total_multiplier"),
            "sweet_spot_applied": order_params.pop("sweet_spot_applied", False),
            "base_contracts": order_params.pop("base_contracts"),
            "hours_until_event": order_params.pop("hours_until_event"),
            "open_interest": order_params.pop("open_interest"),
            "net_position": order_params.pop("net_position"),
            "ev": order_params.pop("ev"),
            "standalone_ev": order_params.pop("standalone_ev", None),
            "hedge_ev": order_params.pop("hedge_ev", None),
            "is_pairing_order": order_params.pop("is_pairing_order", False),
            "pairing_mode": order_params.pop("pairing_mode", "normal"),
            "p_hat": order_params.pop("p_hat"),
            "pricing_strategy": order_params.pop("pricing_strategy"),
            "safe_mode": order_params.pop("safe_mode"),
            "event_ticker_meta": order_params.pop("event_ticker"),
            "is_discovery": order_params.pop("is_discovery", False),
        }

        order_timestamp = datetime.now(UTC).isoformat()

        # Strip None values — Kalshi API may reject them
        order_params = {k: v for k, v in order_params.items() if v is not None}

        # Log first 3 orders for debugging
        if i < 3:
            print(f"  [ORDER-DEBUG] #{i}: {order_params}")

        try:
            response = exchange_client.create_order(**order_params)
            results.append({
                "ok": True,
                "ticker": order_params["ticker"],
                "side": order_params["side"],
                "price": order_params.get("yes_price") or order_params.get("no_price"),
                "contracts": order_params["count"],
                "order_id": response.get("order", {}).get("order_id"),
                "client_order_id": order_params.get("client_order_id"),
                "error": None,
                "post_only": True,
                "created_at": order_timestamp,
                **metadata,
            })
        except Exception as e:
            error_detail = str(e)
            # Extract response body for debugging
            resp_body = ""
            status_code = 0
            if hasattr(e, 'response') and e.response is not None:
                try:
                    status_code = e.response.status_code
                    resp_body = e.response.text[:300]
                    error_detail = f"{str(e)} | body: {resp_body}"
                except:
                    pass
            elif hasattr(e, 'args') and len(e.args) > 1:
                error_detail = f"{str(e)} | args: {e.args}"
            
            # Retry once on 429 (rate limit)
            if '429' in str(e) or status_code == 429:
                time.sleep(2.0)  # back off 2s
                try:
                    response = exchange_client.create_order(**order_params)
                    results.append({
                        "ok": True,
                        "ticker": order_params["ticker"],
                        "side": order_params["side"],
                        "price": order_params.get("yes_price") or order_params.get("no_price"),
                        "contracts": order_params["count"],
                        "order_id": response.get("order", {}).get("order_id"),
                        "client_order_id": order_params.get("client_order_id"),
                        "error": None,
                        "post_only": True,
                        "created_at": order_timestamp,
                        **metadata,
                    })
                    time.sleep(SLEEP_BETWEEN_ORDERS)
                    continue
                except Exception as e2:
                    error_detail = f"429 retry failed: {e2}"

            # Alert on order failures
            if '400' in str(e) or status_code == 400:
                alert("ORDER_400", f"{order_params.get('ticker')} {order_params.get('side')}@{order_params.get('yes_price') or order_params.get('no_price')}c",
                      {"error": error_detail[:200]})
            elif '429' in str(e) or status_code == 429:
                alert("ORDER_429", f"Rate limited on {order_params.get('ticker')}",
                      {"error": error_detail[:200]})

            results.append({
                "ok": False,
                "ticker": order_params["ticker"],
                "side": order_params["side"],
                "price": order_params.get("yes_price") or order_params.get("no_price"),
                "contracts": order_params["count"],
                "error": error_detail,
                "order_id": None,
                "client_order_id": order_params.get("client_order_id"),
                "post_only": True,
                "created_at": order_timestamp,
                **metadata,
            })

        time.sleep(SLEEP_BETWEEN_ORDERS)

    elapsed = time.time() - start_time
    print(f"✓ Completed in {elapsed:.1f}s ({total/elapsed:.1f} orders/sec)\n")
    return results


# ---------- Main ----------
def place_orders_from_df(df_results: pd.DataFrame):

    cancel_all_existing_orders_batch()
    existing_positions, positions_reliable = get_existing_positions()
    safe_mode = not positions_reliable

    if safe_mode:
        print(f"\n{'!'*70}")
        print(f"  ⚠️  SAFE MODE ACTIVE — all sizes at {SAFE_MODE_MULTIPLIER:.0%}")
        print(f"{'!'*70}\n")

    # Build fills-based position ledger
    try:
        bq_client_ref = client if 'client' in dir() else None
        project_ref = PROJECT_ID if 'PROJECT_ID' in dir() else None
        dataset_ref = DATASET_ID if 'DATASET_ID' in dir() else None
    except:
        bq_client_ref = None
        project_ref = None
        dataset_ref = None

    ledger = integrate_ledger_into_run(
        exchange_client_ref=exchange_client,
        bq_client=bq_client_ref,
        project_id=project_ref,
        dataset_id=dataset_ref,
        series_ticker=SERIES_TICKER,
        use_api=True,
        use_bq=(bq_client_ref is not None),
        lookback_days=30,
    )

    all_orders: List[Dict[str, Any]] = []

    event_net_exposure: Dict[str, int] = {}
    for ticker, pos_data in existing_positions.items():
        parts = ticker.split("-")
        evt = "-".join(parts[:2]) if len(parts) >= 2 else ticker
        event_net_exposure[evt] = event_net_exposure.get(evt, 0) + abs(pos_data['net_position'])

    event_orders_placed: Dict[str, int] = {}
    event_market_sides: Dict[str, set] = {}

    # Merge with market-level historical data
    if 'df_summary' in globals() and len(df_summary) > 0:
        summary_data = df_summary[['ticker_part_3_market_code', 'yes_rate', 'count_occurrences']].copy()
        summary_data = summary_data.rename(columns={
            'yes_rate': 'market_yes_rate',
            'count_occurrences': 'market_sample_size'
        })
        df_results_with_data = df_results.merge(summary_data, on='ticker_part_3_market_code', how='left')
    else:
        df_results_with_data = df_results.copy()
        df_results_with_data['market_yes_rate'] = None
        df_results_with_data['market_sample_size'] = 0

    # Merge with team-level historical data
    if 'df_summary_rolling_by_team' in globals() and len(df_summary_rolling_by_team) > 0:
        team_latest = (
            df_summary_rolling_by_team
            .sort_values(['ticker_part_3_market_code', 'team', 'event_date'], ascending=[True, True, False])
            .groupby(['ticker_part_3_market_code', 'team'], as_index=False)
            .first()
            [['ticker_part_3_market_code', 'team', 'yes_rate_rolling', 'count_occurrences_rolling']]
        )

        team1_data = team_latest.copy().rename(columns={
            'team': 'team_1', 'yes_rate_rolling': 'team_1_yes_rate',
            'count_occurrences_rolling': 'team_1_sample_size'
        })
        team2_data = team_latest.copy().rename(columns={
            'team': 'team_2', 'yes_rate_rolling': 'team_2_yes_rate',
            'count_occurrences_rolling': 'team_2_sample_size'
        })

        df_results_with_data = df_results_with_data.merge(
            team1_data, on=['ticker_part_3_market_code', 'team_1'], how='left'
        )
        df_results_with_data = df_results_with_data.merge(
            team2_data, on=['ticker_part_3_market_code', 'team_2'], how='left'
        )
    else:
        df_results_with_data['team_1_yes_rate'] = None
        df_results_with_data['team_1_sample_size'] = 0
        df_results_with_data['team_2_yes_rate'] = None
        df_results_with_data['team_2_sample_size'] = 0

    for _, row in df_results_with_data.iterrows():
        if row.get("market_status") != "active":
            continue

        print(f"\n{row.get('market_ticker')}:")
        orders = build_order_objects_for_market(
            row.to_dict(),
            existing_positions,
            event_net_exposure,
            event_orders_placed,
            safe_mode,
            ledger,
            event_market_sides,
        )
        all_orders.extend(orders)

    print(f"\n{'='*70}")
    print(f"PREPARED {len(all_orders)} POST-ONLY LIMIT ORDERS (MLB v1.0)")
    print(f"{'='*70}")
    print(f"  Model: {MODEL_VERSION} | Strategy: {PRICING_STRATEGY}")
    print(f"  Discovery mode: {'ON' if DISCOVERY_MODE else 'OFF'}")
    print(f"  Safe mode: {'YES' if safe_mode else 'NO'}")
    print(f"  Ledger: {len(ledger)} markets with fill data")
    print(f"  Base contracts: {BASE_YES_CONTRACTS[0]} per level")

    if len(all_orders) == 0:
        print("No orders to place.")
        return pd.DataFrame(), pd.DataFrame(), ledger

    results = submit_orders_sequential(all_orders)

    global df_orders, df_market_snapshot_signals
    df_orders = pd.DataFrame(results)
    df_market_snapshot_signals = pd.DataFrame(market_snapshots)

    success = sum(1 for r in results if r["ok"])
    failed = len(results) - success

    print(f"\n{'='*70}")
    print(f"RESULTS: {success} successful, {failed} failed")
    print(f"{'='*70}\n")

    if failed > 0:
        print("Failed (first 20):")
        for r in [r for r in results if not r["ok"]][:20]:
            print(f"  ✗ {r['ticker']} {r['side']}@{r['price']}c: {r['error'][:80]}")

    if success > 0:
        print(f"\nSuccessful (first 10):")
        for r in [r for r in results if r["ok"]][:10]:
            disc_tag = " [DISC]" if r.get("is_discovery") else ""
            hedge_tag = " [HEDGE]" if r.get("is_pairing_order") else ""
            print(f"  ✓ {r['ticker']} {r['side']}@{r['price']}c ({r['contracts']}c) ev={r['ev']:.0%}{hedge_tag}{disc_tag}")

    if len(df_orders) > 0 and success > 0:
        ok_orders = df_orders[df_orders['ok'] == True]
        print(f"\nSIZING ANALYSIS:")
        print(f"  Total contracts placed: {ok_orders['contracts'].sum()}")
        print(f"  Avg total multiplier: {ok_orders['total_multiplier'].mean():.2f}x")
        print(f"  Avg EV: {ok_orders['ev'].mean():.1%}")

        disc_count = ok_orders['is_discovery'].sum() if 'is_discovery' in ok_orders.columns else 0
        if disc_count > 0:
            print(f"  Discovery orders: {disc_count} ({disc_count/len(ok_orders)*100:.0f}%)")

    return df_orders, df_market_snapshot_signals, ledger


# =====================================================================
# ADAPTIVE CALIBRATION — inlined from mlb_calibration.py
# =====================================================================
# Queries BQ for settlement + fill history, computes dynamic parameters.
# If BQ unavailable or tables don't exist, falls back to static defaults.

# Calibration thresholds
_CAL_MIN_HIST_WEIGHT = 0.05
_CAL_MAX_HIST_WEIGHT = 0.50
_CAL_FULL_CREDIBILITY_N = 100  # Legacy — step schedule below overrides
# Step schedule: (min_n, historical_weight)
# n < 10 → 5% (base), n ≥ 10 → 20%, n ≥ 20 → 30%, n ≥ 30 → 40%, n ≥ 40 → 50% (cap)
_CAL_HIST_WEIGHT_STEPS = [
    (40, 0.50),
    (30, 0.40),
    (20, 0.30),
    (10, 0.20),
]
_CAL_HIST_WEIGHT_BASE = 0.05  # n < 10
_CAL_MIN_SAMPLES_FOR_KELLY = 15
_CAL_KELLY_ENABLE = 0.03
_CAL_KELLY_BLOCK = -0.03
_CAL_KELLY_STRONG = 0.10
_CAL_MAX_SIDE_MULT = 2.0
_CAL_DISCOVERY_MULT = 1.0
_CAL_MIN_FILLS_PER_BUCKET = 20
_CAL_BUCKET_BLOCK_EDGE = -0.15
_CAL_PRICE_BUCKETS = [(0, 20), (20, 35), (35, 50), (50, 65), (65, 80), (80, 100)]


def _cal_bayesian_weights(settlement_counts: Dict[str, int]) -> Dict[str, tuple]:
    """Compute per-code (market_weight, hist_weight) using step schedule.
    n < 10 → 5% hist, n ≥ 10 → 20%, n ≥ 20 → 30%, n ≥ 30 → 40%, n ≥ 40 → 50% cap.
    """
    weights = {}
    for code, n in settlement_counts.items():
        hw = _CAL_HIST_WEIGHT_BASE
        for min_n, weight in _CAL_HIST_WEIGHT_STEPS:
            if n >= min_n:
                hw = weight
                break
        weights[code] = (round(1.0 - hw, 4), round(hw, 4))
    return weights


def _cal_kelly(wins: int, n: int, avg_price_cents: float) -> float:
    if n == 0 or avg_price_cents <= 0 or avg_price_cents >= 100:
        return 0.0
    p = wins / n
    b = (100 - avg_price_cents) / avg_price_cents
    return (p * b - (1 - p)) / b if b > 0 else 0.0


def _cal_side_multipliers(fills_df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    mults = {}
    if fills_df is None or len(fills_df) == 0:
        return mults
    for code, cg in fills_df.groupby('market_code'):
        cm = {}
        for side in ['yes', 'no']:
            sf = cg[cg['side'] == side]
            n = len(sf)
            if n < _CAL_MIN_SAMPLES_FOR_KELLY:
                cm[side] = _CAL_DISCOVERY_MULT
                continue
            kelly = _cal_kelly(int(sf['won'].sum()), n, sf['fill_price'].mean())
            if kelly >= _CAL_KELLY_STRONG:
                cm[side] = min(kelly / _CAL_KELLY_ENABLE, _CAL_MAX_SIDE_MULT)
            elif kelly >= _CAL_KELLY_ENABLE:
                cm[side] = max(1.0, kelly / _CAL_KELLY_ENABLE)
            elif kelly <= _CAL_KELLY_BLOCK:
                cm[side] = 0.0
            else:
                cm[side] = 0.5
        mults[code] = cm
    return mults


def _cal_price_caps(fills_df: pd.DataFrame) -> Tuple[int, int]:
    max_yes, max_no = 75, 75
    if fills_df is None or len(fills_df) == 0:
        return 65, 65
    for side, cap_ref in [('yes', 'max_yes'), ('no', 'max_no')]:
        sf = fills_df[fills_df['side'] == side]
        for lo, hi in _CAL_PRICE_BUCKETS:
            bucket = sf[(sf['fill_price'] >= lo) & (sf['fill_price'] < hi)]
            if len(bucket) < _CAL_MIN_FILLS_PER_BUCKET:
                continue
            wr = bucket['won'].mean()
            edge = wr - bucket['fill_price'].mean() / 100.0
            if edge < _CAL_BUCKET_BLOCK_EDGE:
                if side == 'yes':
                    max_yes = min(max_yes, lo)
                else:
                    max_no = min(max_no, lo)
    return max_yes, max_no


def run_calibration(bq_client, project_id: str, dataset_id: str,
                    series_ticker: str = "KXMLBMENTION") -> Dict[str, Any]:
    """Run adaptive calibration from BQ data. Returns dict of overrides."""
    print(f"\n{'='*70}")
    print(f"ADAPTIVE CALIBRATION — {series_ticker}")
    print(f"{'='*70}")

    result = {
        'market_weight': 0.90, 'historical_weight': 0.10,
        'weights_per_code': {}, 'side_multipliers': {},
        'max_price': 65, 'total_settlements': 0, 'codes_with_kelly': 0,
    }

    if bq_client is None:
        print("  No BQ client — using static defaults")
        return result

    # Load settlement counts
    settlement_counts = {}
    settlement_yes_rates = {}
    try:
        sc_query = f"""
        SELECT
            SPLIT(market_ticker, '-')[SAFE_OFFSET(2)] AS market_code,
            COUNT(*) AS n,
            COUNTIF(LOWER(result) = 'yes') AS n_yes
        FROM `{project_id}.{dataset_id}.{series_ticker}_settlements`
        WHERE result IS NOT NULL AND result != ''
        GROUP BY 1
        """
        sc_df = bq_client.query(sc_query).to_dataframe()
        settlement_counts = dict(zip(sc_df['market_code'], sc_df['n']))
        settlement_yes_rates = dict(zip(sc_df['market_code'], (sc_df['n_yes'] / sc_df['n']).round(3)))
        result['total_settlements'] = sum(settlement_counts.values())
        print(f"  Loaded settlement counts for {len(settlement_counts)} codes ({result['total_settlements']} total)")
    except Exception as e:
        print(f"  ⚠️ Could not load settlements: {e}")
        return result

    if result['total_settlements'] == 0:
        print("  No settlements — using static defaults")
        return result

    # Tier 1: Bayesian weights
    result['weights_per_code'] = _cal_bayesian_weights(settlement_counts)
    median_n = int(np.median(list(settlement_counts.values())))
    # Global weight uses median n with same step schedule
    _global_hw = _CAL_HIST_WEIGHT_BASE
    for min_n, weight in _CAL_HIST_WEIGHT_STEPS:
        if median_n >= min_n:
            _global_hw = weight
            break
    result['historical_weight'] = round(_global_hw, 4)
    result['market_weight'] = round(1.0 - _global_hw, 4)

    print(f"\n  TIER 1: Bayesian weights (median n={median_n})")
    for code, (mw, hw) in sorted(result['weights_per_code'].items()):
        n = settlement_counts.get(code, 0)
        yr = settlement_yes_rates.get(code, 0)
        print(f"    {code:<8}: n={n:>4}, yes_rate={yr:>5.0%} → market={mw:.0%}, hist={hw:.0%}")

    # Load fills matched to settlements
    fills_df = pd.DataFrame()
    try:
        fills_query = f"""
        SELECT
            f.market_ticker,
            f.side,
            f.fill_price,
            f.filled_count,
            SPLIT(f.market_ticker, '-')[SAFE_OFFSET(2)] AS market_code,
            CASE
                WHEN f.side = 'yes' AND UPPER(s.result) = 'YES' THEN TRUE
                WHEN f.side = 'no' AND UPPER(s.result) = 'NO' THEN TRUE
                ELSE FALSE
            END AS won
        FROM `{project_id}.{dataset_id}.{series_ticker}_fills` f
        JOIN `{project_id}.{dataset_id}.{series_ticker}_settlements` s
            ON f.market_ticker = s.market_ticker
        WHERE s.result IS NOT NULL AND s.result != ''
          AND f.fill_price > 0 AND f.filled_count > 0
        """
        fills_df = bq_client.query(fills_query).to_dataframe()
        fills_df['fill_price'] = pd.to_numeric(fills_df['fill_price'], errors='coerce')
        fills_df['won'] = fills_df['won'].astype(bool)
        print(f"  Loaded {len(fills_df)} fills matched to settlements")
    except Exception as e:
        print(f"  ⚠️ Could not load fills: {e}")

    # Tier 2: Side multipliers
    if len(fills_df) > 0:
        result['side_multipliers'] = _cal_side_multipliers(fills_df)
        result['codes_with_kelly'] = len(result['side_multipliers'])
        print(f"\n  TIER 2: Side multipliers ({result['codes_with_kelly']} codes, min_n={_CAL_MIN_SAMPLES_FOR_KELLY})")
        print(f"    Thresholds: block≤{_CAL_KELLY_BLOCK*100:.0f}% | neutral | enable≥{_CAL_KELLY_ENABLE*100:.0f}% | strong≥{_CAL_KELLY_STRONG*100:.0f}%")
        for code, m in sorted(result['side_multipliers'].items()):
            cf = fills_df[fills_df['market_code'] == code]
            lines = []
            for side_label, side_key in [('YES', 'yes'), ('NO ', 'no')]:
                sf = cf[cf['side'] == side_key]
                n = len(sf)
                mult_val = m[side_key]
                mult_str = "BLOCK" if mult_val == 0 else f"{mult_val:.1f}x"
                if n >= _CAL_MIN_SAMPLES_FOR_KELLY:
                    wins = int(sf['won'].sum())
                    avg_p = sf['fill_price'].mean()
                    k = _cal_kelly(wins, n, avg_p)
                    wr = wins / n * 100
                    lines.append(f"{side_label}: n={n}, W={wins}, avg={avg_p:.0f}c, WR={wr:.0f}%, K={k*100:+.0f}% → {mult_str}")
                elif n > 0:
                    lines.append(f"{side_label}: n={n} (< {_CAL_MIN_SAMPLES_FOR_KELLY} min) → {mult_str} (default)")
                else:
                    lines.append(f"{side_label}: n=0 → {mult_str} (default)")
            print(f"    {code:<8}: {lines[0]}")
            print(f"    {'':8}  {lines[1]}")
    else:
        print(f"\n  TIER 2: No fills — all codes at discovery default")

    # Tier 3: Price caps
    if len(fills_df) > 0:
        max_yes, max_no = _cal_price_caps(fills_df)
        result['max_price'] = min(max_yes, max_no)
        print(f"\n  TIER 3: Price caps → YES≤{max_yes}c, NO≤{max_no}c → MAX_PRICE={result['max_price']}c")
    else:
        print(f"\n  TIER 3: No fills — conservative cap (65c)")

    print(f"{'='*70}\n")
    return result


# ---------- Run Calibration ----------
_early_bq_client = None
try:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        _early_bq_client = bigquery.Client(
            project=os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
        )
    else:
        try:
            from google.colab import auth
            auth.authenticate_user()
            _early_bq_client = bigquery.Client(
                project=os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
            )
        except ImportError:
            pass
except Exception as e:
    print(f"  ⚠️ Early BQ init failed: {e}")

if _early_bq_client is not None:
    try:
        _cal = run_calibration(
            bq_client=_early_bq_client,
            project_id=os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7"),
            dataset_id=os.environ.get("GCP_DATASET_ID", "Kalshi"),
            series_ticker=SERIES_TICKER,
        )

        # Tier 1: Override Bayesian weights
        BAYESIAN_MARKET_WEIGHT = _cal['market_weight']
        BAYESIAN_HISTORICAL_WEIGHT = _cal['historical_weight']
        _BAYESIAN_WEIGHTS_PER_CODE = _cal['weights_per_code']

        # Tier 2: Merge calibrated side multipliers (manual blocks take priority)
        for code, mults in _cal['side_multipliers'].items():
            if code not in SIDE_MULTIPLIERS:
                SIDE_MULTIPLIERS[code] = mults

        # Tier 3: Override price caps
        MAX_PRICE = _cal['max_price']

        print(f"✓ Calibration applied: {_cal['total_settlements']} settlements, "
              f"{_cal['codes_with_kelly']} codes with Kelly, "
              f"weights={BAYESIAN_MARKET_WEIGHT:.0%}/{BAYESIAN_HISTORICAL_WEIGHT:.0%}, "
              f"max_price={MAX_PRICE}c, "
              f"side_mults={len(SIDE_MULTIPLIERS)} codes")

    except Exception as e:
        alert("CALIBRATION_FAILED", f"Using static defaults: {e}")
        import traceback
        traceback.print_exc()
else:
    alert("CALIBRATION_NO_BQ", "No BQ client for calibration — using static defaults")


# ---------- Run ----------
print("\nValidating df_results...")

to_trade = df_results[df_results["market_status"] == "active"]
print(f"Active markets: {len(to_trade)}")

if len(to_trade) == 0:
    df_orders = pd.DataFrame()
    df_market_snapshot_signals = pd.DataFrame()
    position_ledger = {}
else:
    df_orders, df_market_snapshot_signals, position_ledger = place_orders_from_df(df_results)

# === END RUN ===
# === START UPLOAD ===

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    try:
        from google.colab import auth
        auth.authenticate_user()
    except ImportError:
        pass

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
DATASET_ID = os.environ.get("GCP_DATASET_ID", "Kalshi")

client = bigquery.Client(project=PROJECT_ID)

df_results["yes_rate"] = pd.to_numeric(
    df_results["yes_rate"].replace("", np.nan),
    errors="coerce"
).infer_objects(copy=False)

for c in df_results.columns:
    if c != "yes_rate":
        df_results[c] = df_results[c].astype("string")

for col in ["event_date"]:
    if col in df_results.columns:
        df_results[col] = pd.to_datetime(df_results[col], errors="coerce").dt.date

def get_existing_table_schema(table_name: str):
    try:
        table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
        table = client.get_table(table_id)
        return {field.name: field.field_type for field in table.schema}
    except Exception as e:
        print(f"Table {table_name} doesn't exist yet or error: {e}")
        return None

def pandas_dtype_to_bq_type(dtype):
    dtype_str = str(dtype)
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    elif pd.api.types.is_float_dtype(dtype):
        return "FLOAT"
    elif pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    elif dtype_str == 'object' or dtype_str.startswith('string'):
        return "STRING"
    else:
        return "STRING"

def convert_df_to_match_schema(df: pd.DataFrame, schema_dict: dict) -> pd.DataFrame:
    df_converted = df.copy()
    for col_name, bq_type in schema_dict.items():
        if col_name in df_converted.columns:
            current_dtype = df_converted[col_name].dtype
            if bq_type == "STRING" and not pd.api.types.is_string_dtype(current_dtype):
                df_converted[col_name] = df_converted[col_name].astype(str).replace('nan', None).replace('None', None)
            elif bq_type == "FLOAT" and not pd.api.types.is_float_dtype(current_dtype):
                df_converted[col_name] = pd.to_numeric(df_converted[col_name], errors='coerce')
            elif bq_type == "INTEGER" and not pd.api.types.is_integer_dtype(current_dtype):
                df_converted[col_name] = pd.to_numeric(df_converted[col_name], errors='coerce').astype('Int64')
            elif bq_type == "BOOLEAN" and not pd.api.types.is_bool_dtype(current_dtype):
                df_converted[col_name] = df_converted[col_name].astype(bool)
    return df_converted

def add_missing_columns_to_table(table_name: str, df: pd.DataFrame, existing_schema: dict):
    table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    table = client.get_table(table_id)
    original_schema = table.schema
    new_schema = original_schema[:]
    for col_name in df.columns:
        if col_name not in existing_schema:
            bq_type = pandas_dtype_to_bq_type(df[col_name].dtype)
            new_schema.append(bigquery.SchemaField(col_name, bq_type, mode="NULLABLE"))
    if len(new_schema) > len(original_schema):
        table.schema = new_schema
        table = client.update_table(table, ["schema"])
        print(f"✓ Updated schema for {table_name}")

def df_to_bq(df, table_name: str, write_disposition="WRITE_TRUNCATE"):
    if df is None or len(df) == 0:
        print(f"⚠️ Skipping BQ upload for {table_name} — empty DataFrame")
        return
    table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    df_to_upload = df.copy()
    if write_disposition == "WRITE_APPEND":
        existing_schema = get_existing_table_schema(table_name)
        if existing_schema is not None:
            df_columns = set(df_to_upload.columns)
            existing_columns = set(existing_schema.keys())
            new_columns = df_columns - existing_columns
            if new_columns:
                print(f"⚠️  New columns detected: {new_columns}")
                add_missing_columns_to_table(table_name, df_to_upload, existing_schema)
                existing_schema = get_existing_table_schema(table_name)
            df_to_upload = convert_df_to_match_schema(df_to_upload, existing_schema)
    job_config = bigquery.LoadJobConfig(
        write_disposition=write_disposition,
        autodetect=True,
    )
    job = client.load_table_from_dataframe(df_to_upload, table_id, job_config=job_config)
    job.result()
    print("✓ Loaded:", table_id, "rows:", client.get_table(table_id).num_rows)

# Upload tables
df_to_bq(df_results, f"{SERIES_TICKER}_market_results", write_disposition="WRITE_TRUNCATE")
df_to_bq(df_summary, f"{SERIES_TICKER}_market_results_summary", write_disposition="WRITE_TRUNCATE")
df_to_bq(df_orders, f"{SERIES_TICKER}_orders", write_disposition="WRITE_APPEND")

if 'df_market_snapshot_signals' in globals() and len(df_market_snapshot_signals) > 0:
    df_to_bq(df_market_snapshot_signals, f"{SERIES_TICKER}_market_snapshot_signals", write_disposition="WRITE_APPEND")

if position_ledger:
    df_ledger = ledger_to_dataframe(position_ledger)
    if len(df_ledger) > 0:
        df_to_bq(df_ledger, f"{SERIES_TICKER}_position_ledger", write_disposition="WRITE_TRUNCATE")
        print(f"✓ Position ledger uploaded ({len(df_ledger)} markets)")

df_to_bq(df_bankroll_log, f"{SERIES_TICKER}_bankroll_log", write_disposition="WRITE_APPEND")

print("\n✓ All tables uploaded successfully!")

# =====================================================================
# END-OF-RUN ALERT SUMMARY
# =====================================================================
if _ALERTS:
    print(f"\n{'='*70}")
    print(f"⚡ ALERT SUMMARY: {len(_ALERTS)} alerts")
    print(f"{'='*70}")

    # Group by category
    cats: Dict[str, int] = {}
    for a in _ALERTS:
        c = a['category']
        cats[c] = cats.get(c, 0) + 1

    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    # Upload alerts to BQ
    upload_alerts_to_bq(client, PROJECT_ID, DATASET_ID, SERIES_TICKER)

    # Send email/text notification
    send_alert_notification()

    print(f"{'='*70}\n")
else:
    print("\n✓ No alerts — clean run")
