#!/usr/bin/env python3
"""
Kalshi Insider Activity Monitor (Phase 1 — Polling)
=====================================================
Detects anomalous trading activity in non-sports prediction markets that may
indicate informed/insider trading. Monitors volume spikes, price dislocations,
orderbook imbalances, and computes a composite "smart money" score.

Deployment: GitHub Actions cron (every 2–3 min) or local/VM polling loop.
State: BigQuery tables for cross-run persistence and historical calibration.
Alerts: SMS via T-Mobile gateway + email.

Usage:
    python kalshi_insider_monitor.py                 # Normal monitoring run
    python kalshi_insider_monitor.py --calibrate     # Diagnostic: show threshold distributions
    python kalshi_insider_monitor.py --list-markets  # Diagnostic: show market universe

Environment Variables (secrets):
    KALSHI_API_KEY          — Kalshi API key
    KALSHI_PRIVATE_KEY_B64  — Base64-encoded RSA private key (or KALSHI_PRIVATE_KEY_PATH)
    GCP_CREDENTIALS_JSON    — GCP service account JSON for BigQuery
    ALERT_EMAIL_FROM        — Gmail sender address
    ALERT_EMAIL_PASSWORD    — Gmail app password
    ALERT_EMAIL_TO          — Recipient email (optional)
    ALERT_SMS_GATEWAY       — T-Mobile gateway (default: 9729713381@tmomail.net)
"""

import argparse
import base64
import json
import logging
import os
import smtplib
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Optional

import requests

# Optional imports — degrade gracefully
try:
    from google.cloud import bigquery
    from google.cloud.bigquery import SchemaField
    from google.oauth2 import service_account
    HAS_BIGQUERY = True
except ImportError:
    HAS_BIGQUERY = False

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("insider_monitor")


# ===========================================================================
#  CONFIGURATION
# ===========================================================================

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY = os.getenv("KALSHI_API_KEY", "c3204983-77fc-491b-99f7-136600698178")

BQ_PROJECT = "elite-contact-446323-q7"
BQ_DATASET = "Kalshi"
BQ_SNAPSHOTS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.insider_monitor_snapshots"
BQ_ALERTS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.insider_monitor_alerts"

ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.getenv("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
ALERT_SMS_GATEWAY = os.getenv("ALERT_SMS_GATEWAY", "9729713381@tmomail.net")
ALERT_COOLDOWN_MINUTES = 15

# --- Category-based exclusion (primary filter, from Kalshi event.category) ---
EXCLUDED_CATEGORIES = {"Sports", "Elections", "Crypto"}

# --- Series prefix exclusion (backup for markets missing category metadata) ---
EXCLUDED_SERIES_PREFIXES = [
    # Sports
    "KXNBA", "KXNBAMENTION", "KXNBAMVP", "KXNBAWEST", "KXNBAEAST",
    "KXNCAA", "KXNCAAB", "KXNCAABMENTION", "KXMARMAD",
    "KXNFL", "KXNFLMENTION", "KXNFLGAME",
    "KXMLB", "KXMLBMENTION",
    "KXWNBA",
    "KXUFCFIGHT", "KXUFCDISTANCE", "KXUFCMOV", "KXFIGHTMENTION",
    "KXPREMIERLEAGUE", "KXPGATOUR", "KXF1RACE",
    "KXPERFORM",
    # Elections
    "KXPRESNOMD", "KXPRESNOMR", "KXPRESPERSON",
    "KXSENATE", "KXGOV",
    "KXTRUMPOUT", "KXCITRINI",
    "CONTROLH", "CONTROLS",
    # Crypto
    "KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP",
    # Weather (public forecast data)
    "KXHIGH",
]

# Signal thresholds (run --calibrate to tune from real data)
VOLUME_Z_THRESHOLD = 2.5
VOLUME_ABS_FLOOR = 50
VOLUME_BASELINE_HOURS = 72

# All prices stored internally as CENTS (0-100).
PRICE_MOVE_THRESHOLD_CENTS = 8
PRICE_MOVE_LARGE_CENTS = 15

BOOK_IMBALANCE_THRESHOLD = 3.0
BOOK_MIN_TOTAL_SIZE = 10  # min contracts on book to evaluate imbalance

OI_CHANGE_THRESHOLD = 30

W_VOLUME = 0.35
W_PRICE = 0.30
W_BOOK = 0.20
W_OI = 0.10
W_TIME_OF_DAY = 0.05

COMPOSITE_ALERT_THRESHOLD = 0.50
MAX_ORDERBOOK_FETCHES = 80
POLL_INTERVAL_SECONDS = 120

# Warm-up: a market must have this many snapshots before alerts fire.
# At 3-min polling, 6 snapshots ≈ 18 min of baseline collection.
# Data is still collected during warm-up; only alerting is suppressed.
WARMUP_SNAPSHOTS = 6


# ===========================================================================
#  KALSHI API CLIENT
# ===========================================================================

class KalshiClient:
    """Lightweight Kalshi API v2 client with RSA authentication."""

    def __init__(self):
        self.base_url = KALSHI_BASE_URL
        self.api_key = API_KEY
        self.private_key = self._load_private_key()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _load_private_key(self):
        if not HAS_CRYPTO:
            log.warning("cryptography not installed — RSA auth disabled")
            return None
        key_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "")
        if key_b64:
            return serialization.load_pem_private_key(base64.b64decode(key_b64), password=None)
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        log.warning(f"No private key found (checked KALSHI_PRIVATE_KEY_B64 and {key_path})")
        return None

    def _sign_request(self, method: str, path: str, timestamp_ms: str) -> str:
        if not self.private_key:
            return ""
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                         salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _request(self, method: str, path: str, params: dict = None,
                 data: dict = None, max_retries: int = 3) -> Optional[dict]:
        url = f"{self.base_url}{path}"

        for attempt in range(max_retries):
            # FIX: Regenerate timestamp+signature on EVERY attempt so retries
            # after 429 don't send stale auth headers.
            timestamp_ms = str(int(time.time() * 1000))
            signature = self._sign_request(method.upper(), path, timestamp_ms)
            headers = {
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
                "KALSHI-ACCESS-SIGNATURE": signature,
            }
            try:
                resp = self.session.request(
                    method, url, params=params, json=data,
                    headers=headers, timeout=15,
                )
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    log.warning(f"Rate limited (429) on {path}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                elif resp.status_code == 400:
                    log.debug(f"400 on {path}: {resp.text[:200]}")
                    return None
                else:
                    log.warning(f"HTTP {resp.status_code} on {path}: {resp.text[:200]}")
                    if attempt < max_retries - 1:
                        time.sleep(1)
            except requests.exceptions.RequestException as e:
                log.warning(f"Request error on {path}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
        return None

    def get(self, path: str, params: dict = None) -> Optional[dict]:
        return self._request("GET", path, params=params)

    def get_events(self, status: str = "open", limit: int = 200) -> list:
        """Fetch all open events with nested markets. Limit=200 supported per Kalshi changelog."""
        all_events = []
        cursor = None
        while True:
            params = {"status": status, "limit": limit, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/events", params=params)
            if not resp:
                break
            events = resp.get("events", [])
            all_events.extend(events)
            cursor = resp.get("cursor")
            if not cursor or len(events) < limit:
                break
        return all_events

    def get_markets(self, status: str = "open", limit: int = 1000,
                    event_ticker: str = None) -> list:
        """Fetch markets. Limit 1-1000 per Kalshi docs."""
        all_markets = []
        cursor = None
        while True:
            params = {"status": status, "limit": limit}
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/markets", params=params)
            if not resp:
                break
            markets = resp.get("markets", [])
            all_markets.extend(markets)
            cursor = resp.get("cursor")
            if not cursor or len(markets) < limit:
                break
        return all_markets

    def get_orderbook(self, ticker: str) -> Optional[dict]:
        resp = self.get(f"/markets/{ticker}/orderbook")
        if not resp:
            return None
        return self._normalize_orderbook(resp)

    def _normalize_orderbook(self, raw: dict) -> dict:
        """Normalize to {yes: [[price_cents, size], ...], no: [...]}"""
        result = {"yes": [], "no": []}

        # New format: orderbook_fp with dollar strings
        ob_fp = raw.get("orderbook_fp", {})
        if ob_fp:
            for side, keys in [("yes", ["yes_dollars", "yes"]),
                               ("no", ["no_dollars", "no"])]:
                for key in keys:
                    entries = ob_fp.get(key, [])
                    if entries:
                        for entry in entries:
                            if isinstance(entry, list) and len(entry) == 2:
                                p = entry[0]
                                s = entry[1]
                                pc = round(float(p) * 100) if isinstance(p, str) else int(p)
                                si = int(round(float(s))) if isinstance(s, str) else int(s)
                                result[side].append([pc, si])
                        break
            if result["yes"] or result["no"]:
                return result

        # Legacy format
        ob = raw.get("orderbook", {})
        for side in ["yes", "no"]:
            for entry in ob.get(side, []):
                if isinstance(entry, list) and len(entry) == 2:
                    result[side].append([int(entry[0]), int(entry[1])])
        return result

    def get_trades(self, ticker: str = None, limit: int = 1000) -> dict:
        """Fetch public trade tape via GET /markets/trades."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.get("/markets/trades", params=params) or {"trades": [], "cursor": ""}


# ===========================================================================
#  FIELD EXTRACTION (handles API migration: dollar strings vs legacy cents)
# ===========================================================================

def _parse_numeric(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return 0.0
    return 0.0


def _extract_volume(m: dict) -> float:
    """Current API: volume_fp (string). Legacy: volume (int)."""
    val = m.get("volume_fp")
    if val is not None:
        return _parse_numeric(val)
    return _parse_numeric(m.get("volume", 0))


def _extract_oi(m: dict) -> float:
    """Current API: open_interest_fp. Legacy: open_interest."""
    val = m.get("open_interest_fp")
    if val is not None:
        return _parse_numeric(val)
    return _parse_numeric(m.get("open_interest", 0))


def _extract_last_price_cents(m: dict) -> float:
    """
    Returns price in CENTS (0-100).
    Current API: last_price_dollars="0.5600" → 56 cents.
    Legacy: last_price=56 (already cents).
    """
    for dollar_field in ["last_price_dollars", "yes_bid_dollars"]:
        val = m.get(dollar_field)
        if val is not None:
            return round(_parse_numeric(val) * 100.0, 2)
    for cents_field in ["last_price", "yes_bid"]:
        val = m.get(cents_field)
        if val is not None:
            return _parse_numeric(val)
    return 0.0


def _extract_yes_bid_cents(m: dict) -> float:
    val = m.get("yes_bid_dollars")
    if val is not None:
        return round(_parse_numeric(val) * 100.0, 2)
    return _parse_numeric(m.get("yes_bid", 0))


def _extract_yes_ask_cents(m: dict) -> float:
    val = m.get("yes_ask_dollars")
    if val is not None:
        return round(_parse_numeric(val) * 100.0, 2)
    return _parse_numeric(m.get("yes_ask", 0))


# ===========================================================================
#  MARKET DISCOVERY
# ===========================================================================

def is_excluded_series(ticker: str) -> bool:
    upper = ticker.upper()
    return any(upper.startswith(p) for p in EXCLUDED_SERIES_PREFIXES)


def extract_series(ticker: str) -> str:
    parts = ticker.split("-")
    return parts[0] if parts else ticker


def discover_monitored_markets(client: KalshiClient) -> list:
    log.info("Discovering monitored market universe...")
    all_markets = []
    event_meta = {}

    events = client.get_events(status="open")
    if events:
        for event in events:
            et = event.get("event_ticker", "")
            cat = event.get("category", "")
            title = event.get("title", "")
            event_meta[et] = {"category": cat, "title": title}
            for m in event.get("markets", []):
                m["_event_category"] = cat
                m["_event_title"] = title
                m["_event_ticker"] = et
            all_markets.extend(event.get("markets", []))
        log.info(f"  Found {len(all_markets)} markets across {len(events)} events")

    # Fallback if events returned no nested markets
    if not all_markets:
        log.info("  No nested markets from events, falling back to /markets")
        all_markets = client.get_markets(status="open")
        for m in all_markets:
            et = m.get("event_ticker", "")
            meta = event_meta.get(et, {})
            m["_event_category"] = meta.get("category", "")
            m["_event_title"] = meta.get("title", m.get("title", ""))
            m["_event_ticker"] = et
        log.info(f"  Found {len(all_markets)} markets via /markets")

    monitored = []
    excluded_count = 0
    dead_count = 0

    for m in all_markets:
        ticker = m.get("ticker", "")
        category = m.get("_event_category", "")

        # Primary filter: exclude by Kalshi event category
        if category in EXCLUDED_CATEGORIES:
            excluded_count += 1
            continue

        # Backup filter: exclude by series prefix (for markets missing category)
        if is_excluded_series(ticker):
            excluded_count += 1
            continue

        volume = _extract_volume(m)
        oi = _extract_oi(m)
        if volume == 0 and oi == 0:
            dead_count += 1
            continue
        m["_volume"] = volume
        m["_oi"] = oi
        m["_series"] = extract_series(ticker)
        monitored.append(m)

    monitored.sort(key=lambda x: x["_volume"], reverse=True)
    log.info(f"  Monitoring {len(monitored)} markets "
             f"(excluded {excluded_count} sports/elections/crypto, {dead_count} dead)")
    return monitored


# ===========================================================================
#  STATE MANAGEMENT
# ===========================================================================

class MarketState:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.snapshots: list[dict] = []
        self.last_alert_ts: float = 0.0

    def add_snapshot(self, volume, oi, last_price_cents, yes_bid_cents, yes_ask_cents):
        self.snapshots.append({
            "ts": time.time(), "volume": volume, "oi": oi,
            "last_price_cents": last_price_cents,
            "yes_bid_cents": yes_bid_cents, "yes_ask_cents": yes_ask_cents,
        })
        cutoff = time.time() - (VOLUME_BASELINE_HOURS * 3600)
        self.snapshots = [s for s in self.snapshots if s["ts"] >= cutoff]

    def volume_deltas(self) -> list[float]:
        deltas = []
        for i in range(1, len(self.snapshots)):
            d = self.snapshots[i]["volume"] - self.snapshots[i - 1]["volume"]
            if d >= 0:
                deltas.append(d)
        return deltas

    def latest_volume_delta(self) -> float:
        if len(self.snapshots) < 2:
            return 0.0
        return max(self.snapshots[-1]["volume"] - self.snapshots[-2]["volume"], 0.0)

    def latest_price_change_cents(self) -> float:
        if len(self.snapshots) < 2:
            return 0.0
        prev = self.snapshots[-2]["last_price_cents"]
        curr = self.snapshots[-1]["last_price_cents"]
        return abs(curr - prev) if prev > 0 and curr > 0 else 0.0

    def latest_oi_change(self) -> float:
        if len(self.snapshots) < 2:
            return 0.0
        return abs(self.snapshots[-1]["oi"] - self.snapshots[-2]["oi"])

    def can_alert(self) -> bool:
        return (time.time() - self.last_alert_ts) > (ALERT_COOLDOWN_MINUTES * 60)

    def mark_alerted(self):
        self.last_alert_ts = time.time()


class MonitorStateStore:
    def __init__(self):
        self.states: dict[str, MarketState] = {}

    def get_or_create(self, ticker: str) -> MarketState:
        if ticker not in self.states:
            self.states[ticker] = MarketState(ticker)
        return self.states[ticker]

    def save_to_bigquery(self, bq_client):
        if not bq_client:
            return
        rows = []
        now = datetime.now(timezone.utc).isoformat()
        for ticker, state in self.states.items():
            if not state.snapshots:
                continue
            latest = state.snapshots[-1]
            rows.append({
                "ticker": ticker, "poll_ts": now,
                "volume": latest["volume"], "open_interest": latest["oi"],
                "last_price_cents": latest["last_price_cents"],
                "yes_bid_cents": latest["yes_bid_cents"],
                "yes_ask_cents": latest["yes_ask_cents"],
                "snapshot_count": len(state.snapshots),
                "last_alert_ts": datetime.fromtimestamp(
                    state.last_alert_ts, tz=timezone.utc
                ).isoformat() if state.last_alert_ts > 0 else None,
            })
        if rows:
            _bq_insert_rows(bq_client, BQ_SNAPSHOTS_TABLE, rows)
            log.info(f"Saved {len(rows)} snapshots to BigQuery")

    def load_from_bigquery(self, bq_client):
        if not bq_client:
            return
        query = f"""
            SELECT ticker, poll_ts, volume, open_interest, last_price_cents,
                   yes_bid_cents, yes_ask_cents, last_alert_ts
            FROM `{BQ_SNAPSHOTS_TABLE}`
            WHERE poll_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {VOLUME_BASELINE_HOURS} HOUR)
            ORDER BY ticker, poll_ts ASC
        """
        try:
            count = 0
            for row in bq_client.query(query).result():
                state = self.get_or_create(row.ticker)
                state.snapshots.append({
                    "ts": row.poll_ts.timestamp() if hasattr(row.poll_ts, 'timestamp') else time.time(),
                    "volume": float(row.volume or 0),
                    "oi": float(row.open_interest or 0),
                    "last_price_cents": float(row.last_price_cents or 0),
                    "yes_bid_cents": float(row.yes_bid_cents or 0),
                    "yes_ask_cents": float(row.yes_ask_cents or 0),
                })
                if row.last_alert_ts:
                    ts = row.last_alert_ts.timestamp() if hasattr(row.last_alert_ts, 'timestamp') else 0
                    state.last_alert_ts = max(state.last_alert_ts, ts)
                count += 1
            log.info(f"Loaded {count} historical snapshots ({len(self.states)} markets)")
        except Exception as e:
            log.warning(f"Could not load state from BigQuery (table may not exist yet): {e}")


# ===========================================================================
#  SIGNAL DETECTION
# ===========================================================================

def detect_volume_spike(state: MarketState) -> dict:
    delta = state.latest_volume_delta()
    all_deltas = state.volume_deltas()
    result = {"triggered": False, "z_score": 0.0, "delta": delta,
              "baseline_mean": 0.0, "baseline_std": 0.0, "data_points": len(all_deltas)}

    if delta >= VOLUME_ABS_FLOOR:
        result["triggered"] = True
        result["z_score"] = 5.0
        return result

    # FIX: Exclude current delta from baseline so outlier doesn't dilute its own z-score
    baseline = all_deltas[:-1] if len(all_deltas) > 1 else []
    if len(baseline) < 5:
        return result

    mean = statistics.mean(baseline)
    std = statistics.stdev(baseline) if len(baseline) > 1 else 0.0
    result["baseline_mean"] = mean
    result["baseline_std"] = std

    if std > 0:
        z = (delta - mean) / std
        result["z_score"] = z
        if z >= VOLUME_Z_THRESHOLD:
            result["triggered"] = True
    elif delta > 0 and mean == 0:
        result["z_score"] = 4.0
        result["triggered"] = True
    return result


def detect_price_dislocation(state: MarketState) -> dict:
    move = state.latest_price_change_cents()
    result = {"triggered": False, "move_cents": move, "severity": "none"}
    if move >= PRICE_MOVE_LARGE_CENTS:
        result["triggered"] = True
        result["severity"] = "large"
    elif move >= PRICE_MOVE_THRESHOLD_CENTS:
        result["triggered"] = True
        result["severity"] = "moderate"
    return result


def detect_book_imbalance(orderbook: dict) -> dict:
    bid_size = sum(level[1] for level in orderbook.get("yes", []))
    ask_size = sum(level[1] for level in orderbook.get("no", []))
    total_size = bid_size + ask_size
    result = {"triggered": False, "ratio": 0.0, "bid_size": bid_size,
              "ask_size": ask_size, "direction": "neutral",
              "top_bid_size": 0, "top_ask_size": 0}
    if orderbook.get("yes"):
        result["top_bid_size"] = orderbook["yes"][0][1]
    if orderbook.get("no"):
        result["top_ask_size"] = orderbook["no"][0][1]

    # FIX: Skip thin books to avoid false positives
    if total_size < BOOK_MIN_TOTAL_SIZE:
        return result

    if ask_size == 0 and bid_size > 0:
        result.update(triggered=True, ratio=float("inf"), direction="aggressive_bid")
        return result
    if bid_size == 0 and ask_size > 0:
        result.update(triggered=True, ratio=0.0, direction="aggressive_ask")
        return result
    if bid_size == 0 and ask_size == 0:
        return result

    ratio = bid_size / ask_size
    result["ratio"] = ratio
    if ratio >= BOOK_IMBALANCE_THRESHOLD:
        result.update(triggered=True, direction="bid_heavy")
    elif ratio <= (1.0 / BOOK_IMBALANCE_THRESHOLD):
        result.update(triggered=True, direction="ask_heavy")
    return result


def detect_oi_spike(state: MarketState) -> dict:
    change = state.latest_oi_change()
    return {"triggered": change >= OI_CHANGE_THRESHOLD, "change": change}


def time_of_day_score() -> float:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return 0.8
    if 2 <= now.hour <= 12:  # UTC 2-12 = ~10pm-8am ET
        return 0.6
    return 0.1


# ===========================================================================
#  COMPOSITE SCORING
# ===========================================================================

def compute_composite_score(vol_signal, price_signal, book_signal, oi_signal) -> dict:
    # Volume (0-1)
    if vol_signal["triggered"]:
        vol_norm = min(vol_signal["z_score"] / 8.0, 1.0)
    else:
        vol_norm = min(vol_signal["z_score"] / 8.0, 0.3) if vol_signal["z_score"] > 0 else 0.0

    # Price (0-1)
    move = price_signal["move_cents"]
    if price_signal["severity"] == "large":
        price_norm = min(move / 30.0, 1.0)
    elif price_signal["severity"] == "moderate":
        price_norm = min(move / 30.0, 0.7)
    else:
        price_norm = min(move / 30.0, 0.2) if move > 0 else 0.0

    # Book imbalance (0-1)
    if book_signal["triggered"]:
        r = book_signal["ratio"]
        if r == float("inf") or r == 0.0:
            book_norm = 1.0
        else:
            book_norm = min(max(r, 1.0 / r if r > 0 else 1.0) / 10.0, 1.0)
    else:
        book_norm = 0.0

    # OI (0-1)
    oi_norm = min(oi_signal["change"] / 100.0, 1.0) if oi_signal["triggered"] else 0.0

    tod = time_of_day_score()

    raw_score = (W_VOLUME * vol_norm + W_PRICE * price_norm +
                 W_BOOK * book_norm + W_OI * oi_norm + W_TIME_OF_DAY * tod)

    signals_firing = sum([vol_signal["triggered"], price_signal["triggered"],
                          book_signal["triggered"], oi_signal["triggered"]])
    bonus = 0.15 if signals_firing >= 3 else (0.08 if signals_firing >= 2 else 0.0)
    final_score = min(raw_score + bonus, 1.0)

    return {
        "score": round(final_score, 4),
        "components": {
            "volume": round(vol_norm, 4), "price": round(price_norm, 4),
            "book_imbalance": round(book_norm, 4), "oi": round(oi_norm, 4),
            "time_of_day": round(tod, 4), "corroboration_bonus": round(bonus, 4),
        },
        "signals_firing": signals_firing,
        "triggered": final_score >= COMPOSITE_ALERT_THRESHOLD,
    }


# ===========================================================================
#  ALERTING
# ===========================================================================

def send_alert(ticker: str, score: float, signals: dict, market_info: dict):
    title = market_info.get("_event_title", market_info.get("title", ticker))
    series = market_info.get("_series", "")
    last_price = market_info.get("_last_price_cents", 0)

    components = signals.get("components", {})
    firing = []
    if components.get("volume", 0) > 0.3: firing.append("VOL")
    if components.get("price", 0) > 0.3: firing.append("PRICE")
    if components.get("book_imbalance", 0) > 0.3: firing.append("BOOK")
    if components.get("oi", 0) > 0.3: firing.append("OI")
    firing_str = "+".join(firing) if firing else "COMPOSITE"

    sms_body = (f"INSIDER ALERT: {ticker}\n"
                f"Score: {score:.2f} | Signals: {firing_str}\n"
                f"Price: {last_price:.0f}c | {title[:80]}")[:300]

    email_body = (
        f"INSIDER ACTIVITY ALERT\n{'='*50}\n"
        f"Market:  {ticker}\nTitle:   {title}\nSeries:  {series}\n"
        f"Score:   {score:.4f} (threshold: {COMPOSITE_ALERT_THRESHOLD})\n"
        f"Price:   {last_price:.0f}c\n\nSignal Breakdown:\n"
        f"  Volume:     {components.get('volume', 0):.3f} (w={W_VOLUME})\n"
        f"  Price:      {components.get('price', 0):.3f} (w={W_PRICE})\n"
        f"  Book:       {components.get('book_imbalance', 0):.3f} (w={W_BOOK})\n"
        f"  OI:         {components.get('oi', 0):.3f} (w={W_OI})\n"
        f"  Time bonus: {components.get('time_of_day', 0):.3f} (w={W_TIME_OF_DAY})\n"
        f"  Corroboration: +{components.get('corroboration_bonus', 0):.3f}\n"
        f"  Signals firing: {signals.get('signals_firing', 0)}/4\n"
        f"\nTimestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    if ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD:
        try:
            msg = MIMEText(sms_body)
            msg["Subject"] = ""
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = ALERT_SMS_GATEWAY
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                s.sendmail(ALERT_EMAIL_FROM, [ALERT_SMS_GATEWAY], msg.as_string())
            log.info(f"  SMS alert sent for {ticker}")
        except Exception as e:
            log.error(f"  SMS alert failed: {e}")

    if ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD and ALERT_EMAIL_TO:
        try:
            msg = MIMEText(email_body)
            msg["Subject"] = f"Kalshi Insider Alert: {ticker} (score={score:.2f})"
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = ALERT_EMAIL_TO
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                s.sendmail(ALERT_EMAIL_FROM, [ALERT_EMAIL_TO], msg.as_string())
            log.info(f"  Email alert sent for {ticker}")
        except Exception as e:
            log.error(f"  Email alert failed: {e}")

    log.info(f"  ALERT: {ticker} score={score:.4f} signals={firing_str}")


# ===========================================================================
#  BIGQUERY HELPERS
# ===========================================================================

def get_bq_client():
    if not HAS_BIGQUERY:
        log.warning("google-cloud-bigquery not installed — BigQuery disabled")
        return None
    creds_json = os.getenv("GCP_CREDENTIALS_JSON", "")
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    try:
        if creds_json:
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(info)
            return bigquery.Client(project=BQ_PROJECT, credentials=creds)
        elif creds_path and os.path.exists(creds_path):
            creds = service_account.Credentials.from_service_account_file(creds_path)
            return bigquery.Client(project=BQ_PROJECT, credentials=creds)
        else:
            return bigquery.Client(project=BQ_PROJECT)
    except Exception as e:
        log.warning(f"Could not initialize BigQuery client: {e}")
        return None


def _bq_insert_rows(bq_client, table_id: str, rows: list):
    try:
        errors = bq_client.insert_rows_json(table_id, rows)
        if errors:
            log.warning(f"BigQuery insert errors for {table_id}: {errors[:3]}")
    except Exception as e:
        if "Not found" in str(e):
            log.info(f"Table {table_id} not found, creating...")
            _create_bq_tables(bq_client)
            try:
                errors = bq_client.insert_rows_json(table_id, rows)
                if errors:
                    log.warning(f"BigQuery insert errors (retry): {errors[:3]}")
            except Exception as e2:
                log.error(f"BigQuery insert failed after table creation: {e2}")
        else:
            log.error(f"BigQuery insert failed: {e}")


def _create_bq_tables(bq_client):
    snap_schema = [
        SchemaField("ticker", "STRING"), SchemaField("poll_ts", "TIMESTAMP"),
        SchemaField("volume", "FLOAT64"), SchemaField("open_interest", "FLOAT64"),
        SchemaField("last_price_cents", "FLOAT64"),
        SchemaField("yes_bid_cents", "FLOAT64"), SchemaField("yes_ask_cents", "FLOAT64"),
        SchemaField("snapshot_count", "INT64"), SchemaField("last_alert_ts", "TIMESTAMP"),
    ]
    snap_table = bigquery.Table(BQ_SNAPSHOTS_TABLE, schema=snap_schema)
    snap_table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="poll_ts")
    try:
        bq_client.create_table(snap_table, exists_ok=True)
        log.info(f"Created/verified {BQ_SNAPSHOTS_TABLE}")
    except Exception as e:
        log.error(f"Could not create snapshots table: {e}")

    alert_schema = [
        SchemaField("ticker", "STRING"), SchemaField("alert_ts", "TIMESTAMP"),
        SchemaField("composite_score", "FLOAT64"), SchemaField("signals_firing", "INT64"),
        SchemaField("volume_component", "FLOAT64"), SchemaField("price_component", "FLOAT64"),
        SchemaField("book_component", "FLOAT64"), SchemaField("oi_component", "FLOAT64"),
        SchemaField("event_title", "STRING"), SchemaField("last_price_cents", "FLOAT64"),
        SchemaField("volume_delta", "FLOAT64"), SchemaField("price_move_cents", "FLOAT64"),
        SchemaField("book_ratio", "FLOAT64"),
    ]
    alert_table = bigquery.Table(BQ_ALERTS_TABLE, schema=alert_schema)
    alert_table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="alert_ts")
    try:
        bq_client.create_table(alert_table, exists_ok=True)
        log.info(f"Created/verified {BQ_ALERTS_TABLE}")
    except Exception as e:
        log.error(f"Could not create alerts table: {e}")


def log_alert_to_bq(bq_client, ticker, score_result, vol_signal, price_signal,
                     book_signal, market_info):
    if not bq_client:
        return
    br = book_signal["ratio"]
    _bq_insert_rows(bq_client, BQ_ALERTS_TABLE, [{
        "ticker": ticker, "alert_ts": datetime.now(timezone.utc).isoformat(),
        "composite_score": score_result["score"],
        "signals_firing": score_result["signals_firing"],
        "volume_component": score_result["components"]["volume"],
        "price_component": score_result["components"]["price"],
        "book_component": score_result["components"]["book_imbalance"],
        "oi_component": score_result["components"]["oi"],
        "event_title": market_info.get("_event_title", "")[:500],
        "last_price_cents": market_info.get("_last_price_cents", 0),
        "volume_delta": vol_signal["delta"],
        "price_move_cents": price_signal["move_cents"],
        "book_ratio": br if br != float("inf") else 999.0,
    }])


# ===========================================================================
#  CALIBRATION
# ===========================================================================

def run_calibration(client: KalshiClient, bq_client):
    log.info("=" * 60)
    log.info("CALIBRATION MODE")
    log.info("=" * 60)

    vol_deltas_all, price_changes_all = [], []

    if bq_client:
        log.info("Loading historical snapshots from BigQuery...")
        query = f"""
            WITH ordered AS (
                SELECT ticker, poll_ts, volume, last_price_cents,
                       LAG(volume) OVER (PARTITION BY ticker ORDER BY poll_ts) AS prev_vol,
                       LAG(last_price_cents) OVER (PARTITION BY ticker ORDER BY poll_ts) AS prev_price
                FROM `{BQ_SNAPSHOTS_TABLE}`
                WHERE poll_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
            )
            SELECT ticker, GREATEST(volume - prev_vol, 0) AS vol_delta,
                   ABS(last_price_cents - prev_price) AS price_change
            FROM ordered
            WHERE prev_vol IS NOT NULL AND prev_price IS NOT NULL AND volume >= prev_vol
        """
        try:
            for row in bq_client.query(query).result():
                vol_deltas_all.append(float(row.vol_delta))
                price_changes_all.append(float(row.price_change))
            log.info(f"  Loaded {len(vol_deltas_all)} historical deltas")
        except Exception as e:
            log.warning(f"  Could not query historical data: {e}")

    if len(vol_deltas_all) < 50:
        log.info("Insufficient historical data. Scanning current market state...")
        markets = discover_monitored_markets(client)
        volumes = [m["_volume"] for m in markets[:200]]
        ois = [m["_oi"] for m in markets[:200]]

        print(f"\n{'='*60}\nCURRENT MARKET UNIVERSE STATISTICS\n{'='*60}")
        if volumes:
            sorted_v = sorted(volumes)
            print(f"\nVolume: count={len(volumes)}, mean={statistics.mean(volumes):.1f}, "
                  f"median={statistics.median(volumes):.1f}")
            for pct in [50, 75, 90, 95, 99]:
                print(f"  P{pct}: {sorted_v[min(int(len(sorted_v)*pct/100), len(sorted_v)-1)]:.1f}")

        print("\nTop 20 markets by volume:")
        for m in markets[:20]:
            t = m.get("ticker", "")
            title = m.get("_event_title", m.get("title", ""))[:60]
            print(f"  {t:<45} vol={m['_volume']:>8.0f}  oi={m['_oi']:>6.0f}  {title}")

        cats = defaultdict(int)
        for m in markets:
            cats[m.get("_event_category", "Unknown") or "Unknown"] += 1
        print("\nCategory breakdown:")
        for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"  {cat:<30} {cnt:>5}")

    if len(vol_deltas_all) >= 50:
        print(f"\n{'='*60}\nHISTORICAL DELTA ANALYSIS (last 7 days)\n{'='*60}")
        sorted_vd = sorted(vol_deltas_all)
        nz_vd = [x for x in sorted_vd if x > 0]
        print(f"\nVolume deltas: {len(sorted_vd)} total, {len(nz_vd)} non-zero")
        if nz_vd:
            for pct in [75, 90, 95, 99]:
                print(f"  P{pct}: {sorted_vd[min(int(len(sorted_vd)*pct/100), len(sorted_vd)-1)]:.1f}")

        sorted_pc = sorted(price_changes_all)
        nz_pc = [x for x in sorted_pc if x > 0]
        print(f"\nPrice changes (cents): {len(sorted_pc)} total, {len(nz_pc)} non-zero")
        if nz_pc:
            for pct in [75, 90, 95, 99]:
                print(f"  P{pct}: {sorted_pc[min(int(len(sorted_pc)*pct/100), len(sorted_pc)-1)]:.1f}c")

        print(f"\n{'='*60}\nSUGGESTED THRESHOLDS\n{'='*60}")
        if nz_vd:
            print(f"\nVOLUME_ABS_FLOOR:  P99={sorted_vd[int(len(sorted_vd)*0.99)]:.0f}  "
                  f"P95={sorted_vd[int(len(sorted_vd)*0.95)]:.0f}  current={VOLUME_ABS_FLOOR}")
        if nz_pc:
            print(f"PRICE_MOVE_CENTS:  P99={sorted_pc[int(len(sorted_pc)*0.99)]:.1f}c  "
                  f"P95={sorted_pc[int(len(sorted_pc)*0.95)]:.1f}c  current={PRICE_MOVE_THRESHOLD_CENTS}c")
    else:
        print(f"\n  Insufficient data for calibration. Run monitor 24-48h first.")
        print(f"  Current: VOLUME_ABS_FLOOR={VOLUME_ABS_FLOOR}, "
              f"PRICE_MOVE={PRICE_MOVE_THRESHOLD_CENTS}c, "
              f"BOOK_MIN_SIZE={BOOK_MIN_TOTAL_SIZE}, "
              f"COMPOSITE_THRESHOLD={COMPOSITE_ALERT_THRESHOLD}")


# ===========================================================================
#  MAIN MONITORING LOOP
# ===========================================================================

def run_monitor(client: KalshiClient, bq_client):
    state_store = MonitorStateStore()
    state_store.load_from_bigquery(bq_client)

    markets = discover_monitored_markets(client)
    if not markets:
        log.warning("No markets to monitor!")
        return

    log.info(f"Polling {len(markets)} markets...")

    # Pass 1: Bulk data from market objects
    flagged_for_orderbook = []
    alerts_triggered = []
    warming_up = 0

    for m in markets:
        ticker = m.get("ticker", "")
        state = state_store.get_or_create(ticker)

        volume = m["_volume"]
        oi = m["_oi"]
        last_price_cents = _extract_last_price_cents(m)
        yes_bid_cents = _extract_yes_bid_cents(m)
        yes_ask_cents = _extract_yes_ask_cents(m)

        state.add_snapshot(volume, oi, last_price_cents, yes_bid_cents, yes_ask_cents)

        vol_signal = detect_volume_spike(state)
        price_signal = detect_price_dislocation(state)
        oi_signal = detect_oi_spike(state)

        if vol_signal["triggered"] or price_signal["triggered"] or oi_signal["triggered"]:
            flagged_for_orderbook.append((m, state, vol_signal, price_signal, oi_signal))
        elif volume > 100 and len(flagged_for_orderbook) < MAX_ORDERBOOK_FETCHES:
            flagged_for_orderbook.append((m, state, vol_signal, price_signal, oi_signal))

    log.info(f"  {len(flagged_for_orderbook)} markets flagged for orderbook fetch")

    # Pass 2: Orderbook fetch + composite scoring
    for m, state, vol_signal, price_signal, oi_signal in flagged_for_orderbook[:MAX_ORDERBOOK_FETCHES]:
        ticker = m.get("ticker", "")
        orderbook = client.get_orderbook(ticker) or {"yes": [], "no": []}
        book_signal = detect_book_imbalance(orderbook)
        score_result = compute_composite_score(vol_signal, price_signal, book_signal, oi_signal)
        m["_last_price_cents"] = state.snapshots[-1]["last_price_cents"] if state.snapshots else 0

        if score_result["triggered"]:
            # Warm-up gate: suppress alerts until we have enough baseline data.
            # Data collection continues normally; only alerting is gated.
            if len(state.snapshots) < WARMUP_SNAPSHOTS:
                warming_up += 1
                continue

            if state.can_alert():
                ratio_str = f"{book_signal['ratio']:.2f}" if book_signal['ratio'] != float("inf") else "inf"
                log.info(f"\n{'!'*60}")
                log.info(f"  ALERT: {ticker} | score={score_result['score']:.4f}")
                log.info(f"  vol: delta={vol_signal['delta']:.0f} z={vol_signal['z_score']:.2f} | "
                         f"price: {price_signal['move_cents']:.1f}c {price_signal['severity']} | "
                         f"book: ratio={ratio_str} {book_signal['direction']} | "
                         f"oi: {oi_signal['change']:.0f}")
                log.info(f"  {m.get('_event_title', 'N/A')}")
                log.info(f"{'!'*60}")

                send_alert(ticker, score_result["score"], score_result, m)
                state.mark_alerted()

                log_alert_to_bq(bq_client, ticker, score_result, vol_signal,
                                price_signal, book_signal, m)
                alerts_triggered.append(ticker)
            else:
                log.info(f"  {ticker} score={score_result['score']:.3f} (cooldown)")

    state_store.save_to_bigquery(bq_client)

    log.info(f"\nPoll complete: {len(markets)} scanned, "
             f"{len(flagged_for_orderbook)} orderbooks, "
             f"{len(alerts_triggered)} alerts"
             + (f", {warming_up} warming up" if warming_up else ""))


def list_markets(client: KalshiClient):
    markets = discover_monitored_markets(client)
    print(f"\nMonitored Market Universe: {len(markets)} markets")
    print(f"{'='*120}")
    print(f"{'Ticker':<45} {'Series':<20} {'Volume':>8} {'OI':>8} {'Category':<15} {'Title'}")
    print(f"{'-'*120}")
    for m in markets[:100]:
        cat = m.get("_event_category", "?") or "?"
        title = m.get("_event_title", m.get("title", ""))[:50]
        print(f"{m.get('ticker',''):<45} {m.get('_series',''):<20} "
              f"{m['_volume']:>8.0f} {m['_oi']:>8.0f} {cat:<15} {title}")
    if len(markets) > 100:
        print(f"  ... and {len(markets) - 100} more")


# ===========================================================================
#  ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Kalshi Insider Activity Monitor")
    parser.add_argument("--calibrate", action="store_true",
                        help="Diagnostic: show threshold distributions from historical data")
    parser.add_argument("--list-markets", action="store_true",
                        help="Diagnostic: print monitored market universe and exit")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously (for VM deployment, not GH Actions)")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info("Kalshi Insider Activity Monitor starting...")
    client = KalshiClient()
    bq_client = get_bq_client()

    if args.list_markets:
        list_markets(client)
    elif args.calibrate:
        run_calibration(client, bq_client)
    elif args.loop:
        log.info(f"Continuous loop mode (interval={args.interval}s)")
        while True:
            try:
                run_monitor(client, bq_client)
            except Exception as e:
                log.error(f"Error in monitoring loop: {e}", exc_info=True)
            log.info(f"Sleeping {args.interval}s...")
            time.sleep(args.interval)
    else:
        run_monitor(client, bq_client)


if __name__ == "__main__":
    main()
