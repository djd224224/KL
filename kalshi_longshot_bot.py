#!/usr/bin/env python3
"""
Kalshi Longshot Seller Bot
==========================
Systematically sells overpriced Yes contracts on longshot outcomes
for curated series with discrete resolution events.

Exploits two well-documented biases:
  1. Favorite-longshot bias (longshots win less often than price implies)
  2. Yes/affirmation bias (people prefer buying Yes over No)

Safety mechanism: pulls all quotes QUOTE_PULLBACK_HOURS before each
market's close_time (fetched dynamically from Kalshi API, same approach
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
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ════════════════════════════════════════════════════════════
#  CONFIGURATION — Edit everything in this section
# ════════════════════════════════════════════════════════════

# Kalshi API base URL
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ── Strategy Parameters ──────────────────────────────────

# Only sell Yes contracts priced in this range (dollars)
MIN_YES_PRICE = 0.03
MAX_YES_PRICE = 0.15

# Bias discount: estimates true probability as a fraction of
# the market price. 0.65 means a $0.10 contract truly wins
# ~6.5% of the time. From Whelan (2025) Kalshi data.
BIAS_DISCOUNT = 0.65

# Minimum expected value per contract after fees to trade
MIN_EV_DOLLARS = 0.01

# Approximate Kalshi fee rate on winnings
FEE_RATE = 0.02

# Minimum recent volume on a contract to consider it
MIN_VOLUME = 10

# Hours before market close_time to pull all unfilled quotes
# and stop placing new orders (matches NBA script's approach).
# close_time is fetched dynamically from each market via the API.
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

# ── Curated Series ───────────────────────────────────────
#
# The bot ONLY trades contracts in these series.
# No event_time needed — the bot pulls close_time dynamically
# from each market via the Kalshi API and uses
# (close_time - QUOTE_PULLBACK_HOURS) as the cutoff.
#
# Fields:
#   series_ticker:  Kalshi series ticker (exact match)
#   category:       Your label for grouping/diversification
#   entity:         Underlying entity for correlation tracking
#   description:    Human-readable note

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

    # Add your series here. The bot ignores everything not listed.
]

# ════════════════════════════════════════════════════════════
#  END CONFIGURATION — Code below
# ════════════════════════════════════════════════════════════


# ────────────────────────────────────────────────────────────
# Kalshi API Client
# ────────────────────────────────────────────────────────────

class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Kalshi API {status_code}: {message}")


class KalshiClient:
    """
    Minimal Kalshi REST client.
    Auth via RSA-signed headers using env vars:
        KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
    """

    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self._token_expiry: float = 0

        self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        self._private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

        if not self.api_key_id:
            logging.warning("KALSHI_API_KEY_ID not set. API calls will fail.")
        if not self._private_key_path:
            logging.warning(
                "KALSHI_PRIVATE_KEY_PATH not set. API calls will fail."
            )

    def _ensure_auth(self):
        now = time.time()
        if now < self._token_expiry - 60:
            return

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            import base64

            key_bytes = open(self._private_key_path, "rb").read()
            private_key = serialization.load_pem_private_key(
                key_bytes, password=None
            )

            timestamp_ms = str(int(now * 1000))
            message = timestamp_ms.encode()
            signature = private_key.sign(
                message, padding.PKCS1v15(), hashes.SHA256()
            )
            sig_b64 = base64.b64encode(signature).decode()

            self.session.headers.update({
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": sig_b64,
                "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            self._token_expiry = now + 1800

        except ImportError:
            logging.error(
                "cryptography package not installed. "
                "Run: pip install cryptography"
            )
            sys.exit(1)
        except Exception as e:
            logging.error(f"Auth failed: {e}")
            sys.exit(1)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        self._ensure_auth()
        url = f"{self.api_base}{path}"
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logging.warning(f"Rate limited. Sleeping {retry_after}s.")
            time.sleep(retry_after)
            return self._request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("message", body.get("error", resp.text))
            except Exception:
                msg = resp.text
            raise KalshiAPIError(resp.status_code, msg)
        return resp.json() if resp.content else {}

    def get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict = None) -> dict:
        return self._request("POST", path, json=data)

    def delete(self, path: str, params: dict = None) -> dict:
        return self._request("DELETE", path, params=params)

    # ── Market Data ──────────────────────────────────────

    def get_events(self, series_ticker: str = None, status: str = "open",
                   limit: int = 100) -> list[dict]:
        params = {
            "status": status,
            "limit": limit,
            "with_nested_markets": True,
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        all_events = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/events", params=params)
            events = resp.get("events", [])
            all_events.extend(events)
            cursor = resp.get("cursor", "")
            if not cursor or len(events) < limit:
                break
        return all_events

    def get_event(self, event_ticker: str) -> dict:
        resp = self.get(
            f"/events/{event_ticker}",
            params={"with_nested_markets": True},
        )
        return resp.get("event", {})

    def get_market(self, ticker: str) -> dict:
        resp = self.get(f"/markets/{ticker}")
        return resp.get("market", {})

    # ── Trading ──────────────────────────────────────────

    def place_order(self, ticker: str, yes_price: int, count: int) -> dict:
        """Sell Yes as a maker (limit order)."""
        payload = {
            "ticker": ticker,
            "action": "sell",
            "side": "yes",
            "type": "limit",
            "count": count,
            "yes_price": yes_price,
        }
        return self.post("/portfolio/orders", data=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self.delete(f"/portfolio/orders/{order_id}")

    def get_orders(self, status: str = "resting") -> list[dict]:
        params = {"status": status, "limit": 200}
        all_orders = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/portfolio/orders", params=params)
            orders = resp.get("orders", [])
            all_orders.extend(orders)
            cursor = resp.get("cursor", "")
            if not cursor or len(orders) < 200:
                break
        return all_orders

    def get_positions(self) -> list[dict]:
        params = {"limit": 200}
        all_positions = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/portfolio/positions", params=params)
            positions = resp.get("market_positions", [])
            all_positions.extend(positions)
            cursor = resp.get("cursor", "")
            if not cursor or len(positions) < 200:
                break
        return all_positions

    def get_balance(self) -> dict:
        return self.get("/portfolio/balance")


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def parse_close_time(close_time_str: Optional[str]) -> Optional[datetime]:
    """Parse Kalshi close_time string to timezone-aware datetime."""
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
    """Check if we're within QUOTE_PULLBACK_HOURS of close_time."""
    if close_time is None:
        return False
    cutoff = close_time - timedelta(hours=QUOTE_PULLBACK_HOURS)
    return datetime.now(timezone.utc) >= cutoff


# ────────────────────────────────────────────────────────────
# Data Models
# ────────────────────────────────────────────────────────────

class CuratedSeries:
    """A series from the curated list."""

    def __init__(self, raw: dict):
        self.series_ticker: str = raw["series_ticker"]
        self.category: str = raw.get("category", "unknown")
        self.entity: str = raw.get("entity", self.series_ticker)
        self.description: str = raw.get("description", "")

    def __repr__(self):
        return f"CuratedSeries({self.series_ticker})"


class Candidate:
    """A contract that passes all filters."""

    def __init__(self):
        self.market_ticker: str = ""
        self.event_ticker: str = ""
        self.curated_series: Optional[CuratedSeries] = None
        self.title: str = ""
        self.yes_price: float = 0.0
        self.yes_ask: float = 0.0
        self.yes_bid: float = 0.0
        self.volume: int = 0
        self.close_time: Optional[datetime] = None
        self.estimated_true_prob: float = 0.0
        self.expected_value: float = 0.0
        self.composite_score: float = 0.0
        self.risk_per_contract: float = 0.0
        self.contracts_to_trade: int = 0


# ────────────────────────────────────────────────────────────
# Core Bot Logic
# ────────────────────────────────────────────────────────────

class LongshotBot:

    def __init__(self):
        self._setup_logging()

        self.client = KalshiClient(api_base=API_BASE)
        self.curated_series = [CuratedSeries(s) for s in CURATED_SERIES]
        self.log.info(f"Loaded {len(self.curated_series)} curated series.")

        # In-memory state
        self.active_orders: dict[str, dict] = {}
        self.filled_positions: dict[str, dict] = {}
        self.total_risk: float = 0.0
        self.daily_pnl: float = 0.0
        self.halted: bool = False

    def _setup_logging(self):
        level = getattr(logging, LOG_LEVEL)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.log = logging.getLogger("longshot-bot")

    def _log_trade(self, action: str, data: dict):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **data,
        }
        try:
            with open(TRADE_LOG_PATH, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            self.log.error(f"Failed to write trade log: {e}")

    # ── Step 1: Find Markets for Curated Series ──────────

    def _get_curated_markets(self) -> list[tuple[CuratedSeries, dict]]:
        """
        Fetch all active markets for curated series.
        Skips individual markets whose close_time is within
        the pullback window (like the NBA script's approach).
        """
        results = []

        for cs in self.curated_series:
            try:
                events = self.client.get_events(
                    series_ticker=cs.series_ticker
                )

                for event in events:
                    for mkt in event.get("markets", []):
                        if mkt.get("status") != "active":
                            continue

                        # Check pullback per-market using close_time
                        close_dt = parse_close_time(mkt.get("close_time"))
                        if close_dt and is_within_pullback(close_dt):
                            hours_left = (
                                close_dt - datetime.now(timezone.utc)
                            ).total_seconds() / 3600
                            self.log.debug(
                                f"Skipping {mkt.get('ticker')} — "
                                f"{hours_left:.1f}h to close "
                                f"(pullback={QUOTE_PULLBACK_HOURS}h)"
                            )
                            continue

                        results.append((cs, mkt))

            except KalshiAPIError as e:
                self.log.warning(
                    f"API error fetching {cs.series_ticker}: {e}"
                )
            except Exception as e:
                self.log.error(f"Error fetching {cs.series_ticker}: {e}")

        self.log.info(
            f"Found {len(results)} active markets across curated series."
        )
        return results

    # ── Step 2: Filter to Longshot Candidates ────────────

    def _filter_candidates(
        self, markets: list[tuple[CuratedSeries, dict]]
    ) -> list[Candidate]:
        candidates = []

        for cs, mkt in markets:
            ticker = mkt.get("ticker", "")

            yes_ask_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
            yes_bid_raw = mkt.get("yes_bid_dollars") or mkt.get("yes_bid")
            if yes_ask_raw is None or yes_bid_raw is None:
                continue

            yes_ask = float(yes_ask_raw)
            yes_bid = float(yes_bid_raw)
            if yes_ask > 1:  # cents → dollars
                yes_ask /= 100
                yes_bid /= 100

            yes_mid = (
                (yes_ask + yes_bid) / 2 if yes_bid > 0 else yes_ask
            )

            if not (MIN_YES_PRICE <= yes_mid <= MAX_YES_PRICE):
                continue

            volume = mkt.get("volume", 0) or mkt.get("volume_24h", 0)
            if isinstance(volume, str):
                volume = int(float(volume))

            # Expected value calculation
            true_prob = yes_mid * BIAS_DISCOUNT
            sell_price = yes_ask
            profit_if_no = sell_price * (1 - FEE_RATE)
            loss_if_yes = (1.0 - sell_price) + (sell_price * FEE_RATE)
            ev = (1 - true_prob) * profit_if_no - true_prob * loss_if_yes

            if ev < MIN_EV_DOLLARS:
                continue

            c = Candidate()
            c.market_ticker = ticker
            c.event_ticker = mkt.get("event_ticker", "")
            c.curated_series = cs
            c.title = mkt.get("title", ticker)
            c.yes_price = yes_mid
            c.yes_ask = yes_ask
            c.yes_bid = yes_bid
            c.volume = volume
            c.estimated_true_prob = true_prob
            c.expected_value = ev
            c.risk_per_contract = 1.0 - sell_price
            c.close_time = parse_close_time(mkt.get("close_time"))

            price_range = MAX_YES_PRICE - MIN_YES_PRICE
            price_score = (
                (1.0 - (yes_mid - MIN_YES_PRICE) / price_range)
                if price_range > 0
                else 0.5
            )
            ev_score = min(ev / 0.05, 1.0)
            c.composite_score = 0.6 * ev_score + 0.4 * price_score

            candidates.append(c)

        candidates.sort(key=lambda x: x.composite_score, reverse=True)
        self.log.info(
            f"Filtered to {len(candidates)} candidates "
            f"(${MIN_YES_PRICE}-${MAX_YES_PRICE}, EV>${MIN_EV_DOLLARS})."
        )
        return candidates

    # ── Step 3: Apply Risk Limits ────────────────────────

    def _apply_risk_limits(
        self, candidates: list[Candidate]
    ) -> list[Candidate]:
        selected = []
        event_counts: dict[str, int] = {}
        entity_counts: dict[str, int] = {}
        day_counts: dict[str, int] = {}

        for _, pos in self.filled_positions.items():
            et = pos.get("event_ticker", "")
            ent = pos.get("entity", "")
            event_counts[et] = event_counts.get(et, 0) + 1
            entity_counts[ent] = entity_counts.get(ent, 0) + 1

        for c in candidates:
            if c.market_ticker in self.filled_positions:
                continue
            if any(
                o.get("market_ticker") == c.market_ticker
                for o in self.active_orders.values()
            ):
                continue

            ek = c.event_ticker
            if event_counts.get(ek, 0) >= MAX_CONTRACTS_PER_EVENT:
                continue

            entity = c.curated_series.entity
            if entity_counts.get(entity, 0) >= MAX_CONTRACTS_PER_ENTITY:
                continue

            if c.close_time:
                day_key = c.close_time.strftime("%Y-%m-%d")
                if day_counts.get(day_key, 0) >= MAX_SAME_DAY_EXPIRY:
                    continue

            contracts = min(
                int(MAX_RISK_PER_CONTRACT / c.risk_per_contract),
                MAX_CONTRACTS_PER_MARKET,
            )
            if contracts < 1:
                continue

            added_risk = contracts * c.risk_per_contract
            if self.total_risk + added_risk > MAX_TOTAL_RISK:
                contracts = int(
                    (MAX_TOTAL_RISK - self.total_risk) / c.risk_per_contract
                )
                if contracts < 1:
                    self.log.info("Total risk limit reached.")
                    break

            c.contracts_to_trade = contracts
            selected.append(c)

            event_counts[ek] = event_counts.get(ek, 0) + 1
            entity_counts[entity] = entity_counts.get(entity, 0) + 1
            if c.close_time:
                day_key = c.close_time.strftime("%Y-%m-%d")
                day_counts[day_key] = day_counts.get(day_key, 0) + 1
            self.total_risk += contracts * c.risk_per_contract

        self.log.info(
            f"Selected {len(selected)} positions within risk limits."
        )
        return selected

    # ── Step 4: Place Orders ─────────────────────────────

    def _place_orders(self, selected: list[Candidate]):
        for c in selected:
            count = c.contracts_to_trade
            yes_cents = int(round(c.yes_ask * 100))

            # Derive pullback_time from market close_time
            close_iso = c.close_time.isoformat() if c.close_time else None
            pullback_iso = (
                (c.close_time - timedelta(hours=QUOTE_PULLBACK_HOURS)).isoformat()
                if c.close_time else None
            )

            self.log.info(
                f"SELL YES {count}x {c.market_ticker} "
                f"@ ${c.yes_ask:.2f} ({yes_cents}¢) | "
                f"EV=${c.expected_value:.3f} | "
                f"score={c.composite_score:.2f} | "
                f"\"{c.title}\""
            )

            trade_data = {
                "market_ticker": c.market_ticker,
                "event_ticker": c.event_ticker,
                "entity": c.curated_series.entity,
                "category": c.curated_series.category,
                "yes_price_cents": yes_cents,
                "count": count,
                "ev": round(c.expected_value, 4),
                "true_prob": round(c.estimated_true_prob, 4),
                "score": round(c.composite_score, 3),
                "close_time": close_iso,
                "pullback_time": pullback_iso,
                "risk_dollars": round(count * c.risk_per_contract, 2),
            }

            try:
                resp = self.client.place_order(
                    ticker=c.market_ticker,
                    yes_price=yes_cents,
                    count=count,
                )
                order = resp.get("order", {})
                order_id = order.get("order_id", "")

                self.active_orders[order_id] = {
                    "order_id": order_id,
                    "market_ticker": c.market_ticker,
                    "event_ticker": c.event_ticker,
                    "entity": c.curated_series.entity,
                    "yes_price_cents": yes_cents,
                    "count": count,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "close_time": close_iso,
                    "pullback_time": pullback_iso,
                }

                trade_data["order_id"] = order_id
                self._log_trade("order_placed", trade_data)
                self.log.info(f"  → Order placed: {order_id}")

            except KalshiAPIError as e:
                self.log.error(f"  → Failed: {e}")
                self._log_trade(
                    "order_failed", {**trade_data, "error": str(e)}
                )

    # ── Step 5: Monitor and Manage ───────────────────────

    def _pull_quotes_near_events(self):
        """Cancel resting orders approaching pullback window."""
        now = datetime.now(timezone.utc)
        to_cancel = []

        for order_id, info in list(self.active_orders.items()):
            pullback_str = info.get("pullback_time")
            if not pullback_str:
                continue
            if now >= datetime.fromisoformat(pullback_str):
                to_cancel.append(order_id)

        if not to_cancel:
            return

        self.log.warning(
            f"PULLBACK: Cancelling {len(to_cancel)} orders "
            f"(markets within {QUOTE_PULLBACK_HOURS}h of close)."
        )

        for order_id in to_cancel:
            info = self.active_orders.get(order_id, {})
            ticker = info.get("market_ticker", "?")
            self.log.info(f"  Cancelling {order_id} on {ticker}")

            try:
                self.client.cancel_order(order_id)
            except KalshiAPIError as e:
                self.log.error(f"  Cancel failed {order_id}: {e}")

            self._log_trade("order_cancelled_pullback", {
                "order_id": order_id,
                "market_ticker": ticker,
            })
            self.active_orders.pop(order_id, None)

    def _check_price_spikes(self):
        """Cancel orders if Yes price has spiked."""
        to_cancel = []

        for order_id, info in list(self.active_orders.items()):
            ticker = info.get("market_ticker")
            entry = info.get("yes_price_cents", 0) / 100.0
            if not ticker:
                continue

            try:
                mkt = self.client.get_market(ticker)
                raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask", 0)
                current = float(raw)
                if current > 1:
                    current /= 100

                move = current - entry
                if move >= PRICE_SPIKE_THRESHOLD:
                    self.log.warning(
                        f"SPIKE on {ticker}: ${entry:.2f} → "
                        f"${current:.2f} (+${move:.2f}). Pulling."
                    )
                    to_cancel.append(order_id)

            except Exception as e:
                self.log.debug(f"Price check error {ticker}: {e}")

        for order_id in to_cancel:
            info = self.active_orders.get(order_id, {})
            try:
                self.client.cancel_order(order_id)
            except KalshiAPIError:
                pass
            self._log_trade("order_cancelled_spike", {
                "order_id": order_id,
                "market_ticker": info.get("market_ticker"),
            })
            self.active_orders.pop(order_id, None)

    def _sync_orders(self):
        """Detect fills by checking which orders still rest."""
        try:
            resting = self.client.get_orders(status="resting")
            resting_ids = {o["order_id"] for o in resting}

            for order_id in list(self.active_orders.keys()):
                if order_id not in resting_ids:
                    info = self.active_orders.pop(order_id)
                    self.log.info(
                        f"Order {order_id} filled/cancelled "
                        f"({info.get('market_ticker')})"
                    )
                    self.filled_positions[
                        info.get("market_ticker", "")
                    ] = info
                    self._log_trade("order_filled_or_cancelled", {
                        "order_id": order_id,
                        "market_ticker": info.get("market_ticker"),
                    })
        except Exception as e:
            self.log.error(f"Sync error: {e}")

    def _check_daily_stop(self):
        if self.halted:
            return
        if self.daily_pnl <= DAILY_STOP_LOSS:
            self.log.critical(
                f"DAILY STOP: P&L=${self.daily_pnl:.2f} "
                f"(limit=${DAILY_STOP_LOSS}). Halting."
            )
            self.halted = True
            self._cancel_all_orders()
            self._log_trade("daily_stop_hit", {"pnl": self.daily_pnl})

    def _cancel_all_orders(self):
        self.log.warning("Cancelling ALL resting orders.")
        for order_id in list(self.active_orders.keys()):
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass
            self.active_orders.pop(order_id, None)

    # ── Scan Cycle ────────────────────────────────────────

    def scan_and_trade(self):
        """One full scan → filter → score → trade cycle."""
        if self.halted:
            self.log.warning("Bot halted. Skipping.")
            return

        self.log.info("=" * 60)
        self.log.info("Starting scan cycle...")

        self._pull_quotes_near_events()
        self._check_price_spikes()
        self._sync_orders()
        self._check_daily_stop()
        if self.halted:
            return

        markets = self._get_curated_markets()
        candidates = self._filter_candidates(markets)
        selected = self._apply_risk_limits(candidates)
        self._place_orders(selected)

        self.log.info(
            f"Scan complete. Orders: {len(self.active_orders)} | "
            f"Positions: {len(self.filled_positions)} | "
            f"Risk: ${self.total_risk:.2f}"
        )

    def run(self):
        self.log.info("=" * 60)
        self.log.info("Kalshi Longshot Seller Bot")
        self.log.info(f"  API: {API_BASE}")
        self.log.info(f"  Series: {len(self.curated_series)}")
        for cs in self.curated_series:
            self.log.info(f"    {cs.series_ticker} ({cs.description})")
        self.log.info(f"  Price: ${MIN_YES_PRICE}-${MAX_YES_PRICE}")
        self.log.info(f"  Bias discount: {BIAS_DISCOUNT}")
        self.log.info(f"  Pullback: {QUOTE_PULLBACK_HOURS}h before close_time")
        self.log.info(f"  Max risk: ${MAX_TOTAL_RISK}")
        self.log.info("=" * 60)

        self.scan_and_trade()


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────

def main():
    bot = LongshotBot()
    bot.run()


if __name__ == "__main__":
    main()
