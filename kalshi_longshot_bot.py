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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

# ════════════════════════════════════════════════════════════
#  CONFIGURATION — Edit everything in this section
# ════════════════════════════════════════════════════════════

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# ── Strategy Parameters ──────────────────────────────────

MIN_YES_PRICE = 0.03
MAX_YES_PRICE = 0.15
BIAS_DISCOUNT = 0.65
MIN_EV_DOLLARS = 0.01
FEE_RATE = 0.02
MIN_VOLUME = 10

# Hours before market close_time to stop placing / pull quotes
# (matches NBA script's EXPIRATION_HOURS_BEFORE_CLOSE = 5)
QUOTE_PULLBACK_HOURS = 5

# ── Risk Management ──────────────────────────────────────

MAX_RISK_PER_CONTRACT = 50
MAX_TOTAL_RISK = 2000
MAX_CONTRACTS_PER_EVENT = 5
MAX_CONTRACTS_PER_ENTITY = 3
MAX_SAME_DAY_EXPIRY = 15
DAILY_STOP_LOSS = -200
PRICE_SPIKE_THRESHOLD = 0.10
MAX_CONTRACTS_PER_MARKET = 10

# ── Bot Behavior ─────────────────────────────────────────

TRADE_LOG_PATH = "trades.jsonl"
LOG_LEVEL = "INFO"
SLEEP_BETWEEN_ORDERS = 0.05

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
# Auth (same pattern as NBA script)
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

print("Testing connection...")
try:
    status = exchange_client.get_exchange_status()
    print(f"✓ Connected! Trading active: {status['trading_active']}")
except Exception as e:
    print(f"✗ Connection failed: {e}")
    raise


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
    """Fetch all active markets for a series (same pattern as NBA script)."""
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


def log_trade(action: str, data: dict):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **data,
    }
    try:
        with open(TRADE_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.error(f"Failed to write trade log: {e}")


# ────────────────────────────────────────────────────────────
# Core Logic
# ────────────────────────────────────────────────────────────

def scan_and_trade():
    log = logging.getLogger("longshot-bot")

    log.info("=" * 60)
    log.info("Starting scan cycle...")

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

        for mkt in active:
            ticker = mkt.get("ticker", "")

            # ── Pullback check using close_time from API ──
            close_dt = parse_close_time(mkt.get("close_time"))
            if close_dt and is_within_pullback(close_dt):
                hours_left = (
                    close_dt - datetime.now(timezone.utc)
                ).total_seconds() / 3600
                log.debug(
                    f"  Skipping {ticker} — {hours_left:.1f}h to close"
                )
                continue

            # ── Get orderbook for bid/ask ──
            try:
                ob_resp = exchange_client.get_orderbook(
                    ticker=ticker, depth=5
                )
                ob = ob_resp.get("orderbook", ob_resp)
                yes_data = ob.get("yes", [])
                no_data = ob.get("no", [])

                yes_bid = max(l[0] for l in yes_data) if yes_data else None
                yes_ask = None
                if no_data:
                    best_no_bid = max(l[0] for l in no_data)
                    yes_ask = 100 - best_no_bid
                elif yes_bid is not None:
                    yes_ask = yes_bid + 2  # rough fallback
            except Exception:
                yes_bid = None
                yes_ask = None

            if yes_bid is None or yes_ask is None:
                continue

            # Normalize to dollars
            yes_bid_d = yes_bid / 100.0
            yes_ask_d = yes_ask / 100.0
            yes_mid = (yes_bid_d + yes_ask_d) / 2.0

            # ── Price filter ──
            if not (MIN_YES_PRICE <= yes_mid <= MAX_YES_PRICE):
                continue

            # ── EV calculation ──
            true_prob = yes_mid * BIAS_DISCOUNT
            sell_price = yes_ask_d
            profit_if_no = sell_price * (1 - FEE_RATE)
            loss_if_yes = (1.0 - sell_price) + (sell_price * FEE_RATE)
            ev = (1 - true_prob) * profit_if_no - true_prob * loss_if_yes

            if ev < MIN_EV_DOLLARS:
                continue

            risk_per = 1.0 - sell_price

            # ── Composite score ──
            price_range = MAX_YES_PRICE - MIN_YES_PRICE
            price_score = (
                (1.0 - (yes_mid - MIN_YES_PRICE) / price_range)
                if price_range > 0 else 0.5
            )
            ev_score = min(ev / 0.05, 1.0)
            score = 0.6 * ev_score + 0.4 * price_score

            all_candidates.append({
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
            })

    # Sort by score
    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(
        f"\nFiltered to {len(all_candidates)} candidates "
        f"(${MIN_YES_PRICE}-${MAX_YES_PRICE}, EV>${MIN_EV_DOLLARS})"
    )

    if not all_candidates:
        log.info("No candidates found. Done.")
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

        contracts = min(
            int(MAX_RISK_PER_CONTRACT / c["risk_per"]),
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
        return

    # ── Step 3: Place orders via ExchangeClient ──────────

    success_count = 0
    fail_count = 0

    for c in selected:
        ticker = c["ticker"]
        yes_price_cents = c["yes_ask"]
        count = c["contracts"]

        log.info(
            f"SELL YES {count}x {ticker} "
            f"@ {yes_price_cents}¢ | "
            f"EV={c['ev']:.3f} | "
            f"score={c['score']:.2f} | "
            f"\"{c['title']}\""
        )

        trade_data = {
            "ticker": ticker,
            "event_ticker": c["event_ticker"],
            "entity": c["entity"],
            "category": c["category"],
            "yes_price_cents": yes_price_cents,
            "count": count,
            "ev": round(c["ev"], 4),
            "true_prob": round(c["true_prob"], 4),
            "score": round(c["score"], 3),
            "close_time": c["close_time_str"],
            "risk_dollars": round(count * c["risk_per"], 2),
        }

        try:
            response = exchange_client.create_order(
                ticker=ticker,
                action="sell",
                side="yes",
                type="limit",
                count=count,
                yes_price=yes_price_cents,
                client_order_id=str(uuid.uuid4()),
            )
            order = response.get("order", {})
            order_id = order.get("order_id", "")

            trade_data["order_id"] = order_id
            log_trade("order_placed", trade_data)
            log.info(f"  ✓ Order placed: {order_id}")
            success_count += 1

        except Exception as e:
            log.error(f"  ✗ Failed: {e}")
            trade_data["error"] = str(e)
            log_trade("order_failed", trade_data)
            fail_count += 1

        time.sleep(SLEEP_BETWEEN_ORDERS)

    # ── Summary ──────────────────────────────────────────

    log.info("=" * 60)
    log.info(
        f"RESULTS: {success_count} placed, {fail_count} failed, "
        f"risk=${total_risk:.2f}"
    )
    log.info("=" * 60)


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
    log.info(f"  API: {API_BASE}")
    log.info(f"  Series: {len(CURATED_SERIES)}")
    for s in CURATED_SERIES:
        log.info(f"    {s['series_ticker']} ({s['description']})")
    log.info(f"  Price: ${MIN_YES_PRICE}-${MAX_YES_PRICE}")
    log.info(f"  Bias discount: {BIAS_DISCOUNT}")
    log.info(f"  Pullback: {QUOTE_PULLBACK_HOURS}h before close_time")
    log.info(f"  Max risk: ${MAX_TOTAL_RISK}")
    log.info("=" * 60)

    scan_and_trade()


if __name__ == "__main__":
    main()
