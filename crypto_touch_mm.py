#!/usr/bin/env python3
"""
crypto_touch_mm.py — Market maker for Kalshi crypto monthly ONE-TOUCH events:
  "How high will {ASSET} get in {month}?"  (KX{A}MAXMON, strike_type=greater)
  "How low  will {ASSET} get in {month}?"  (KX{A}MINMON, strike_type=less)

Each strike is a one-touch binary: it resolves YES if the CF Benchmarks
trimmed-mean minute price ever crosses the strike (above for MAX, below for
MIN) between issuance and 11:59 PM ET on the last day of the month; touched
strikes settle early.

Model: driftless-GBM barrier-touch probability (reflection principle) with
historical vol from Kraken daily closes (EWMA 0.94 blended 60/40 with a 90-day
stdev; the in-progress day's candle is excluded from returns). Fair YES price
per strike = P(touch before month end).

Quoting per active strike (post-only, YES-book cents):
  bids at fair-5, fair-7, fair-9; asks at fair+5, fair+7, fair+9
  up to 5 contracts per level (HARD per-level cap: never more than
  CONTRACTS_PER_LEVEL resting at one price on a market), shaved to fit the
  per-market net-per-direction cap (80) and the per-event aggregate cap
  (500) counting the whole ladder as if it fills; GTC with a short TTL so
  quotes die if the bot does.
  JOIN, DON'T LEAD: a quote may at most match the best EXTERNAL level (book
  net of our own orders) and a side with no external quotes gets nothing —
  we are never alone at the top of the book. The per-market cap is NET per
  direction: position plus a fully-filled ladder never exceeds it either way.

The event ticker is derived from the current month in ET and rolls over
automatically (…-26JUL31 -> …-26AUG31 -> …-26SEP30). New markets are one
config entry in MARKETS.

Fail-safe posture: any two consecutive cycle errors (price feed, positions
read, Kalshi errors) cancel every resting order; the loop also cancels on any
exit path (finally + SIGINT/SIGTERM/SIGBREAK + atexit). A market is skipped
when the month-to-date extreme suggests its strike already touched, or when
the live book (best bid >= 85c) disagrees with our fair by 20c+ (someone knows
about a touch we can't see).

SAFETY: DRY RUN by default — reads are live, order writes are simulated and
logged. Pass --live to actually place orders.
--cancel-all always operates on the REAL book (with or without --live) and
sweeps both the current and previous month's events.

Before first live run, verify with a single small order that Kalshi accepts
our client_order_id format ('cmm-<run>-<hex>'); the orphan sweep keys on it.

Usage:
    python crypto_touch_mm.py                        # SOL-MAX, dry run, loop
    python crypto_touch_mm.py --market SOL-MIN --once
    python crypto_touch_mm.py --market ETH-MAX --live
    python crypto_touch_mm.py --market SOL-MAX --cancel-all
"""

import argparse
import atexit
import base64
import calendar
import json
import math
import os
import signal
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Set, Tuple

import pytz
import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient, HttpError

MODEL_VERSION = "crypto_touch_mm_v1.9"
RUN_ID = uuid.uuid4().hex[:8]
CLIENT_ORDER_PREFIX = "cmm"  # all client_order_ids look like cmm-<run>-<uuid>

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")

ET = pytz.timezone("US/Eastern")


@dataclass(frozen=True)
class MarketConfig:
    key: str              # CLI name, e.g. "SOL-MAX"
    asset: str            # ticker fragment used in the Kalshi event name
    series: str           # Kalshi series ticker
    direction: str        # 'max' (touch above) or 'min' (touch below)
    kraken_pair: str      # Kraken public API pair name
    coinbase_product: str # Coinbase spot product id (fallback price source)


def _mk(asset: str, direction: str, kraken_pair: str, coinbase: str) -> MarketConfig:
    series = f"KX{asset}{'MAX' if direction == 'max' else 'MIN'}MON"
    return MarketConfig(f"{asset}-{direction.upper()}", asset, series, direction,
                        kraken_pair, coinbase)


_PAIRS = {  # asset -> (kraken pair, coinbase product); all verified live 2026-07-03
    "SOL": ("SOLUSD", "SOL-USD"),
    "ETH": ("ETHUSD", "ETH-USD"),
    "BTC": ("XBTUSD", "BTC-USD"),
    "XRP": ("XRPUSD", "XRP-USD"),
    "ZEC": ("ZECUSD", "ZEC-USD"),
    "HYPE": ("HYPEUSD", "HYPE-USD"),
    "DOGE": ("DOGEUSD", "DOGE-USD"),
    "BNB": ("BNBUSD", "BNB-USD"),
}

MARKETS: Dict[str, MarketConfig] = {m.key: m for m in [
    _mk(asset, direction, kraken, coinbase)
    for asset, (kraken, coinbase) in _PAIRS.items()
    for direction in ("max", "min")
]}

# Quoting parameters (user spec: post-only, 5c off fair, 3 levels 2c apart, 10 lots)
QUOTE_OFFSET_CENTS = int(os.environ.get("CMM_QUOTE_OFFSET_CENTS", 5))
LEVEL_SPACING_CENTS = int(os.environ.get("CMM_LEVEL_SPACING_CENTS", 2))
NUM_LEVELS = int(os.environ.get("CMM_NUM_LEVELS", 3))
CONTRACTS_PER_LEVEL = int(os.environ.get("CMM_CONTRACTS_PER_LEVEL", 8))

# Risk / hygiene parameters
MAX_POSITION_CONTRACTS = float(os.environ.get("CMM_MAX_POSITION", 128))  # per market, either sign
# Net cap across all strikes of one event (they are one correlated bet on the
# same spot). 500 comfortably exceeds 8 full 3x10 ladders (480): non-binding
# at rest so every bucket gets the full spec ladder; binds as fills
# accumulate. The event budget is split evenly across quotable markets each
# cycle.
MAX_EVENT_CONTRACTS = float(os.environ.get("CMM_MAX_EVENT", 800))
SKIP_FAIR_ABOVE_CENTS = 97       # near-certain touch: model risk dominates, stand down
BOOK_DIVERGENCE_BID = 85         # best bid >= this while our fair is 20c+ lower ->
BOOK_DIVERGENCE_GAP = 20         # ... suspected touch we can't see; stand down
MAX_FAIR_DIVERGENCE_CENTS = 30   # |fair - book mid| beyond this (two-sided book):
                                 # model and market fundamentally disagree -> we'd be
                                 # taking a view, not making a market; stand down
# Join, don't lead: quotes may at most MATCH the best external level (the book
# net of our own orders) and are never placed on an empty side. Being alone at
# the top of the book is pure adverse selection.
MIN_HOURS_LEFT = 1.0             # stop quoting within the last hour of the window
REQUOTE_TOLERANCE_CENTS = 1      # keep a resting order if within this of desired price
ORDER_TTL_SECS = 600             # exchange-side expiration: quotes die if the bot does
ORDER_REFRESH_SECS = 420         # replace our own orders before their TTL runs out
# (TTL - refresh gives ~3 min of headroom for slow placement waves, and
# refresh must comfortably exceed the fleet's worst-case CYCLE time: with 16
# bots sharing the account's API throughput a cycle can take 2-4 minutes, and
# a refresh shorter than that makes every order "stale" every cycle — a
# self-sustaining full-churn spiral. Expirations are stamped per-send below.)
FAILSAFE_CANCEL_AFTER = 4        # consecutive cycle errors before cancelling everything
                                 # (with backoff ~3-5 min of sustained failure; brief
                                 # network blips ride through, TTL still bounds risk)
WAKE_GAP_SECS = 600              # loop-iteration gap that indicates suspend/resume
WAKE_GRACE_SECS = 120            # post-resume window where cycle errors don't count
                                 # toward the fail-safe (network needs time to return)
BLIND_PRESERVE_CYCLES = 3        # keep resting quotes through this many orderbook blackouts
PRICE_JUMP_GUARD = 0.20          # reject a live price >20% away from the previous poll
VOL_REFRESH_SECS = 6 * 3600
MTD_REFRESH_SECS = 300           # hourly-candle MTD refresh cadence; every cycle
                                 # would exceed Kraken's per-IP rate with a 16-bot
                                 # fleet (session extremes cover between fetches)
POLL_SECS = int(os.environ.get("CMM_POLL_SECS", 60))
EWMA_LAMBDA = 0.94
VOL_BLEND_EWMA_WEIGHT = 0.6      # blend: 0.6*EWMA + 0.4*simple 90d stdev
HTTP_TIMEOUT = 10

# Alerting (same Gmail-SMTP convention as the other bots). Recipients are a
# comma list; a recipient with an all-digit local part (1234567890@carrier) is
# treated as an SMS gateway: body truncated to 300 chars, no subject line.
# Default is the user's own inbox — T-Mobile's email-to-SMS gateway
# (9729713381@tmomail.net) has refused all mail from this account since
# ~2026-06-29 (452 greylisting, then permanent bounces after 48h of retries;
# see the mailer-daemon threads in jackdu224@gmail.com), so Gmail-app push
# notifications on the phone are the delivery mechanism.
# Without FROM/PASSWORD set, alerts are log-only.
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_RECIPIENTS = [r.strip() for r in os.environ.get(
    "CMM_ALERT_TO", "jackdu224@gmail.com").split(",") if r.strip()]
ALERT_DEDUPE_SECS = 6 * 3600     # one urgent alert per (category, market) per 6h
SMS_MAX_CHARS = 300              # carrier gateways silently drop longer bodies
# Bots roll their daily counters/summaries at 5am CT (6am ET) — one hour
# before the 7am ET digest email, so every bot has rolled by send time.
SUMMARY_HOUR_CT = int(os.environ.get("CMM_SUMMARY_HOUR_CT", 5))
CT = pytz.timezone("US/Central")
# Per-bot heartbeat/status files; send_daily_digest.py combines them into the
# single morning email.
STATUS_DIR = os.environ.get("CMM_STATUS_DIR",
                            r"C:\Users\jackd\Documents\KL\run-logs\crypto-touch")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}", flush=True)


# ----------------------------------------------------------------------------
# Event ticker / calendar logic (pure functions; ET drives the month boundary)
# ----------------------------------------------------------------------------

def month_end_utc(now_utc: datetime) -> datetime:
    """End of the measurement window for the current ET month: last day 11:59:59 PM ET."""
    now_et = now_utc.astimezone(ET)
    last_day = calendar.monthrange(now_et.year, now_et.month)[1]
    end_et = ET.localize(datetime(now_et.year, now_et.month, last_day, 23, 59, 59))
    return end_et.astimezone(timezone.utc)


def month_start_utc(now_utc: datetime) -> datetime:
    now_et = now_utc.astimezone(ET)
    start_et = ET.localize(datetime(now_et.year, now_et.month, 1))
    return start_et.astimezone(timezone.utc)


def _event_ticker(cfg: MarketConfig, year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    mon = datetime(year, month, 1).strftime("%b").upper()
    yy = f"{year % 100:02d}"
    return f"{cfg.series}-{cfg.asset}-{yy}{mon}{last_day:02d}"


def event_ticker_for(cfg: MarketConfig, now_utc: datetime) -> str:
    """KX{A}(MAX|MIN)MON-{A}-{YY}{MON}{DD} for the current month in ET."""
    now_et = now_utc.astimezone(ET)
    return _event_ticker(cfg, now_et.year, now_et.month)


def prev_event_ticker_for(cfg: MarketConfig, now_utc: datetime) -> str:
    """Previous ET month's event ticker (for orphan sweeps across a rollover)."""
    now_et = now_utc.astimezone(ET)
    year, month = (now_et.year - 1, 12) if now_et.month == 1 else (now_et.year, now_et.month - 1)
    return _event_ticker(cfg, year, month)


def parse_iso_utc(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def market_strike(market: dict, direction: str) -> Optional[float]:
    """Strike for a one-touch market: floor_strike for MAX (strike_type=greater),
    cap_strike for MIN (strike_type=less). Returns None on any mismatch."""
    if direction == "max":
        expected_type, key = "greater", "floor_strike"
    else:
        expected_type, key = "less", "cap_strike"
    st = market.get("strike_type")
    if st is not None and st != expected_type:
        return None
    v = market.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# Market data: live price + history (Kraken primary, Coinbase fallback)
# ----------------------------------------------------------------------------

class DataError(Exception):
    pass


# --- shared market-data cache -------------------------------------------------
# 16 bots share one IP, and MAX/MIN bots of the same asset need IDENTICAL data.
# Kraken (and Coinbase under fallback load) temp-ban the IP when the whole
# fleet fetches independently — a small file cache dedupes across processes.

def _cache_path(kind: str, pair: str) -> str:
    return os.path.join(STATUS_DIR, f"cache_{kind}_{pair}.json")


def cache_get(kind: str, pair: str, max_age_secs: float):
    try:
        path = _cache_path(kind, pair)
        if time.time() - os.path.getmtime(path) <= max_age_secs:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def cache_put(kind: str, pair: str, data) -> None:
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = _cache_path(kind, pair) + f".tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _cache_path(kind, pair))
    except Exception:
        pass


PRICE_CACHE_SECS = 10            # live tick freshness shared across the fleet
OHLC_CACHE_SECS = {60: 240, 1440: 3600}   # per Kraken interval (minutes)


def _kraken_result(payload: dict) -> dict:
    if payload.get("error"):
        raise DataError(f"kraken error: {payload['error']}")
    result = payload.get("result") or {}
    keys = [k for k in result if k != "last"]
    if not keys:
        raise DataError("kraken: empty result")
    return result[keys[0]]


def fetch_kraken_price(cfg: MarketConfig) -> float:
    cached = cache_get("price", cfg.kraken_pair, PRICE_CACHE_SECS)
    if cached is not None:
        return float(cached)
    r = requests.get("https://api.kraken.com/0/public/Ticker",
                     params={"pair": cfg.kraken_pair}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    price = float(_kraken_result(r.json())["c"][0])
    if price <= 0:
        raise DataError(f"kraken: bad price {price}")
    cache_put("price", cfg.kraken_pair, price)
    return price


def fetch_coinbase_price(cfg: MarketConfig) -> float:
    r = requests.get(f"https://api.coinbase.com/v2/prices/{cfg.coinbase_product}/spot",
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    price = float(r.json()["data"]["amount"])
    if price <= 0:
        raise DataError(f"coinbase: bad price {price}")
    return price


def fetch_live_price(cfg: MarketConfig, prev_price: Optional[float]) -> float:
    """Kraken primary, Coinbase fallback; cross-checks large jumps between sources."""
    price = None
    errors = []
    for fetcher in (fetch_kraken_price, fetch_coinbase_price):
        try:
            price = fetcher(cfg)
            break
        except Exception as e:  # network, parse, DataError — all mean "try the next source"
            errors.append(f"{fetcher.__name__}: {e}")
    if price is None:
        raise DataError("all price sources failed: " + "; ".join(errors))

    if prev_price and abs(price / prev_price - 1) > PRICE_JUMP_GUARD:
        # A huge move in one poll is either bad data or exactly the moment our
        # quotes are most wrong; require the other source to agree before
        # accepting it. (Failing here counts as a cycle error, and two of
        # those cancel all resting orders — fail safe, not fail open.)
        try:
            other = fetch_coinbase_price(cfg) if not errors else fetch_kraken_price(cfg)
            if abs(price / other - 1) > 0.02:
                raise DataError(f"price jump unconfirmed: {prev_price} -> {price} (other src {other})")
        except DataError:
            raise
        except Exception as e:
            raise DataError(f"price jump {prev_price} -> {price}, confirm source failed: {e}")
    return price


def fetch_ohlc(cfg: MarketConfig, interval_minutes: int) -> List[Tuple[int, float, float, float]]:
    """Kraken OHLC -> list of (unix_ts, close, high, low), ascending, ~720 rows.
    Fleet-shared via the file cache (MAX and MIN bots need identical candles)."""
    kind = f"ohlc{interval_minutes}"
    cached = cache_get(kind, cfg.kraken_pair, OHLC_CACHE_SECS.get(interval_minutes, 240))
    if cached is not None:
        return [tuple(row) for row in cached]
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": cfg.kraken_pair, "interval": interval_minutes},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    rows = _kraken_result(r.json())
    out = [(int(row[0]), float(row[4]), float(row[2]), float(row[3])) for row in rows]
    if len(out) < 24:
        raise DataError(f"kraken OHLC({interval_minutes}m): only {len(out)} candles")
    cache_put(kind, cfg.kraken_pair, out)
    return out


# ----------------------------------------------------------------------------
# Volatility + one-touch pricing (pure functions)
# ----------------------------------------------------------------------------

def log_returns(closes: List[float]) -> List[float]:
    return [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def ewma_vol(returns: List[float], lam: float = EWMA_LAMBDA) -> float:
    """RiskMetrics EWMA daily vol (most recent return weighted highest)."""
    if not returns:
        raise DataError("no returns for EWMA vol")
    var = returns[0] ** 2
    for r in returns[1:]:
        var = lam * var + (1 - lam) * r ** 2
    return math.sqrt(var)


def simple_vol(returns: List[float], window: int = 90) -> float:
    tail = returns[-window:]
    if len(tail) < 2:
        raise DataError("not enough returns for simple vol")
    mean = sum(tail) / len(tail)
    return math.sqrt(sum((r - mean) ** 2 for r in tail) / (len(tail) - 1))


def blended_daily_vol(closes: List[float]) -> float:
    rets = log_returns(closes)
    return VOL_BLEND_EWMA_WEIGHT * ewma_vol(rets) + (1 - VOL_BLEND_EWMA_WEIGHT) * simple_vol(rets)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def touch_prob(spot: float, barrier: float, sigma_daily: float, t_days: float,
               direction: str = "max") -> float:
    """P(GBM touches barrier within [0, T]); barrier above spot for 'max',
    below spot for 'min'. Driftless in price space (log drift nu = -sigma^2/2),
    by the reflection principle. Validated against Monte Carlo both ways."""
    if (direction == "max" and barrier <= spot) or (direction == "min" and barrier >= spot):
        return 1.0
    if t_days <= 0 or sigma_daily <= 0:
        return 0.0
    b = math.log(barrier / spot)          # >0 for max, <0 for min
    sig_t = sigma_daily * math.sqrt(t_days)
    nu = -0.5 * sigma_daily * sigma_daily
    if direction == "max":
        p = (_norm_cdf((nu * t_days - b) / sig_t)
             + math.exp(2.0 * nu * b / (sigma_daily ** 2)) * _norm_cdf((-b - nu * t_days) / sig_t))
    else:
        p = (_norm_cdf((b - nu * t_days) / sig_t)
             + math.exp(2.0 * nu * b / (sigma_daily ** 2)) * _norm_cdf((b + nu * t_days) / sig_t))
    return min(max(p, 0.0), 1.0)


def fair_value_cents(spot: float, barrier: float, sigma_daily: float, t_days: float,
                     direction: str = "max") -> int:
    return max(1, min(99, round(100.0 * touch_prob(spot, barrier, sigma_daily, t_days, direction))))


# ----------------------------------------------------------------------------
# Quote construction (pure functions; prices in YES-book cents)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    ticker: str
    book_side: str      # 'bid' (buy YES) or 'ask' (sell YES == buy NO)
    price_cents: int    # YES-book price
    count: int


def build_quotes(ticker: str, fair: int,
                 ext_best_bid: Optional[int], ext_best_ask: Optional[int],
                 room_buy: float, room_sell: float) -> List[Quote]:
    """Ladder of post-only quotes around fair. `ext_best_bid`/`ext_best_ask`
    are the best EXTERNAL levels (book net of our own orders).

    Join, don't lead: a bid may at most MATCH the external best bid (never
    improve it), an ask may at most match the external best ask, and a side
    with no external quotes at all gets nothing — we are never alone at the
    top of the book. Levels are always exactly LEVEL_SPACING_CENTS apart
    (clamps move the whole ladder via its anchor, never squeeze the gaps).
    The ladder's total size is shaved to fit `room_buy`/`room_sell`
    (contracts we may still buy/sell before hitting the caps, assuming the
    whole ladder fills)."""
    quotes: List[Quote] = []

    # INVARIANT: consecutive levels on a side are always exactly
    # LEVEL_SPACING_CENTS apart. All clamping (join-don't-lead, never-cross)
    # is applied to a single ANCHOR (the top of the ladder); the remaining
    # levels are spaced deterministically off it.

    if ext_best_bid is not None:  # no external bids -> we never bid alone
        room = int(room_buy)
        anchor = min(fair - QUOTE_OFFSET_CENTS, ext_best_bid)   # match, never lead
        if ext_best_ask is not None:
            anchor = min(anchor, ext_best_ask - 1)              # post-only: never cross
        for i in range(NUM_LEVELS):
            if room <= 0:
                break
            px = anchor - i * LEVEL_SPACING_CENTS
            if px < 1:
                break
            count = min(CONTRACTS_PER_LEVEL, room)
            quotes.append(Quote(ticker, "bid", px, count))
            room -= count

    if ext_best_ask is not None:  # no external asks -> we never ask alone
        room = int(room_sell)
        anchor = max(fair + QUOTE_OFFSET_CENTS, ext_best_ask)   # match, never lead
        if ext_best_bid is not None:
            anchor = max(anchor, ext_best_bid + 1)              # post-only: never cross
        for i in range(NUM_LEVELS):
            if room <= 0:
                break
            px = anchor + i * LEVEL_SPACING_CENTS
            if px > 99:
                break
            count = min(CONTRACTS_PER_LEVEL, room)
            quotes.append(Quote(ticker, "ask", px, count))
            room -= count

    return quotes


def orderbook_levels(orderbook_response: dict) -> Tuple[List[List[float]], List[List[float]]]:
    """(yes_levels, no_levels) as [price_cents, qty] from a V2 or legacy
    orderbook, ascending by price (best bid on each book is the LAST level)."""
    yes_levels: List[List[float]] = []
    no_levels: List[List[float]] = []
    if "orderbook_fp" in orderbook_response:
        ob = orderbook_response.get("orderbook_fp") or {}
        yes_levels = [[round(float(p) * 100), float(q)] for p, q in (ob.get("yes_dollars") or [])]
        no_levels = [[round(float(p) * 100), float(q)] for p, q in (ob.get("no_dollars") or [])]
    elif "orderbook" in orderbook_response:
        ob = orderbook_response.get("orderbook") or {}
        yes_levels = [[float(p), float(q)] for p, q in (ob.get("yes") or [])]
        no_levels = [[float(p), float(q)] for p, q in (ob.get("no") or [])]
    return yes_levels, no_levels


def external_best(yes_levels: List[List[float]], no_levels: List[List[float]],
                  own_orders: List[Tuple[str, int, float]] = ()
                  ) -> Tuple[Optional[int], Optional[int]]:
    """(best_yes_bid, best_yes_ask) in cents EXCLUDING our own resting orders,
    given as (book_side, yes_price_cents, remaining_count). Our YES-book asks
    rest on the NO book at (100 - yes_price). Without `own_orders` this is
    just the plain best of book. The YES ask is implied by the best NO bid."""
    own_yes: Dict[int, float] = {}
    own_no: Dict[int, float] = {}
    for book_side, yes_px, remaining in own_orders:
        if book_side == "bid":
            own_yes[yes_px] = own_yes.get(yes_px, 0.0) + remaining
        else:
            own_no[100 - yes_px] = own_no.get(100 - yes_px, 0.0) + remaining

    def best(levels: List[List[float]], own: Dict[int, float]) -> Optional[int]:
        for px, qty in reversed(levels):
            if qty - own.get(int(px), 0.0) > 0.01:
                return int(px)
        return None

    best_yes_bid = best(yes_levels, own_yes)
    best_no_bid = best(no_levels, own_no)
    best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
    return best_yes_bid, best_yes_ask


def orderbook_best(orderbook_response: dict) -> Tuple[Optional[int], Optional[int]]:
    """(best_yes_bid, best_yes_ask) of the full book (own orders included)."""
    return external_best(*orderbook_levels(orderbook_response))


def candles_extreme(candles: List[Tuple[int, float, float, float]],
                    month_start_ts: float, interval_secs: int,
                    direction: str) -> Optional[float]:
    """Month-to-date extreme (high for 'max', low for 'min') over candles whose
    span overlaps the ET month. Uses candle END > month start so the candle
    CONTAINING the boundary is included (slight over-inclusion is the safe
    direction for a breach guard: it can only make us stand down)."""
    vals = [(high if direction == "max" else low)
            for ts, _c, high, low in candles if ts + interval_secs > month_start_ts]
    if not vals:
        return None
    return max(vals) if direction == "max" else min(vals)


def breached(strike: float, mtd_extreme: Optional[float], direction: str) -> bool:
    if mtd_extreme is None:
        return False
    return mtd_extreme >= strike if direction == "max" else mtd_extreme <= strike


# ----------------------------------------------------------------------------
# Order diffing
# ----------------------------------------------------------------------------

def order_yes_book_cents(order: dict) -> Optional[Tuple[str, int]]:
    """(book_side, yes_book_price_cents) from a (normalized) V2 order dict.
    Defensive against the field-name variants seen across the V2 migration."""
    book_side = order.get("book_side")
    if book_side not in ("bid", "ask"):
        side, action = order.get("side"), order.get("action")
        if side in ("yes", "no") and action in ("buy", "sell"):
            book_side = "bid" if (side == "yes") == (action == "buy") else "ask"
        else:
            return None
    for key in ("price_dollars", "yes_price_dollars"):
        v = order.get(key)
        if v is not None:
            return book_side, round(float(v) * 100)
    if order.get("yes_price") is not None:
        return book_side, int(order["yes_price"])
    if order.get("no_price_dollars") is not None:
        return book_side, 100 - round(float(order["no_price_dollars"]) * 100)
    if order.get("no_price") is not None:
        return book_side, 100 - int(order["no_price"])
    return None


def order_remaining(order: dict) -> float:
    for key in ("remaining_count", "remaining_count_fp", "count"):
        v = order.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def diff_orders(desired: List[Quote],
                resting: List[dict],
                order_ages: Dict[str, float],
                now_ts: float,
                preserve_tickers: Set[str] = frozenset()) -> Tuple[List[Quote], List[str]]:
    """Match desired quotes to resting orders. Returns (to_place, to_cancel).
    A resting order survives if a desired quote on the same ticker/side is
    within REQUOTE_TOLERANCE_CENTS at the same remaining size and the order
    isn't near its TTL. Orders on `preserve_tickers` (markets we're currently
    blind on) are left alone entirely."""
    to_place: List[Quote] = []
    to_cancel: List[str] = []
    unmatched = list(desired)

    for o in resting:
        oid = o.get("order_id", "")
        if o.get("ticker") in preserve_tickers:
            continue
        parsed = order_yes_book_cents(o)
        if parsed is None:
            log(f"  ! cannot parse resting order {oid}, cancelling defensively")
            to_cancel.append(oid)
            continue
        book_side, px = parsed
        remaining = order_remaining(o)
        age = now_ts - order_ages.get(oid, now_ts)
        stale = age > ORDER_REFRESH_SECS
        match = next((q for q in unmatched
                      if q.ticker == o.get("ticker") and q.book_side == book_side
                      and abs(q.price_cents - px) <= REQUOTE_TOLERANCE_CENTS
                      and round(remaining) == q.count), None)
        if match is not None and not stale:
            unmatched.remove(match)   # keep this resting order as-is
        else:
            to_cancel.append(oid)

    to_place.extend(q for q in unmatched if q.ticker not in preserve_tickers)
    return to_place, to_cancel


# ----------------------------------------------------------------------------
# Credentials / client
# ----------------------------------------------------------------------------

def load_private_key():
    """Same env conventions as the other bots, plus the local-machine default."""
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_b64:
        try:
            pem = base64.b64decode(pem_b64)
        except Exception:
            pem = pem_b64.encode()
        return serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
    if not os.path.exists(pem_path):
        local_default = r"C:/Users/jackd/Downloads/Lisa_Kalshi.txt"
        if os.path.exists(local_default):
            pem_path = local_default
        else:
            raise FileNotFoundError(
                f"No Kalshi private key. Set KALSHI_PRIVATE_KEY (b64) or place PEM at {pem_path!r}.")
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


# ----------------------------------------------------------------------------
# Alerting: urgent SMS on anything needing attention + a daily summary text
# ----------------------------------------------------------------------------

class Alerter:
    """Notifies the user via the repo's Gmail-SMTP convention (email push
    and/or carrier SMS gateways, per ALERT_RECIPIENTS). Urgent categories
    send immediately (deduped per market per 6h); everything is also
    collected for the daily summary. All sends are best-effort — alerting
    must never take the trading loop down."""

    def __init__(self, market_key: str, live: bool):
        self.market_key = market_key
        self.live = live
        self.last_sent: Dict[Tuple[str, str], float] = {}
        self.today: List[Tuple[str, str]] = []          # (category, message)
        self.last_summary_date = None                   # CT date; set on first tick
        self.last_summary_body: Optional[str] = None    # stored for the fleet digest
        self.enabled = bool(ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD and ALERT_RECIPIENTS)
        if self.enabled:
            log(f"[{market_key}] alerts -> {', '.join(ALERT_RECIPIENTS)}")
        else:
            log(f"[{market_key}] alerting LOG-ONLY: set ALERT_EMAIL_FROM + "
                f"ALERT_EMAIL_PASSWORD (Gmail app password) to enable alerts "
                f"to {ALERT_RECIPIENTS}")

    def _mode(self) -> str:
        return "LIVE" if self.live else "DRY"

    @staticmethod
    def _is_sms_gateway(recipient: str) -> bool:
        local = recipient.split("@")[0]
        return local.isdigit() and len(local) >= 7

    def send_message(self, body: str, subject: str = "",
                     html: Optional[str] = None) -> bool:
        """Deliver to every recipient; returns True if at least one accepted.
        When `html` is given, email recipients get a multipart message with
        the HTML rendering (plain `body` stays as the fallback part); SMS
        gateways always get truncated plain text."""
        ok = False
        for recipient in ALERT_RECIPIENTS:
            try:
                is_sms = self._is_sms_gateway(recipient)
                if html and not is_sms:
                    msg = MIMEMultipart("alternative")
                    msg.attach(MIMEText(body))
                    msg.attach(MIMEText(html, "html"))
                else:
                    msg = MIMEText(body[:SMS_MAX_CHARS] if is_sms else body)
                msg["Subject"] = "" if is_sms else (subject or f"{self.market_key} bot alert")
                msg["From"] = ALERT_EMAIL_FROM
                msg["To"] = recipient
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                    server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                    server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
                ok = True
            except Exception as e:
                log(f"[{self.market_key}] ! alert to {recipient} failed: {e}")
        return ok

    def alert(self, category: str, message: str, key: str = "",
              urgent: bool = True, now_ts: Optional[float] = None) -> None:
        now_ts = time.time() if now_ts is None else now_ts
        log(f"[{self.market_key}] ALERT [{category}] {message}")
        self.today.append((category, message))
        if not (urgent and self.enabled):
            return
        dedupe_key = (category, key)
        last = self.last_sent.get(dedupe_key)
        if last is not None and now_ts - last < ALERT_DEDUPE_SECS:
            return
        if self.send_message(f"[{self.market_key} {self._mode()}] {category}: {message}",
                             subject=f"{self.market_key} {category} alert"):
            self.last_sent[dedupe_key] = now_ts

    def maybe_daily_summary(self, now_utc: datetime, body_builder) -> None:
        """Compute the daily summary once per CT day at/after SUMMARY_HOUR_CT
        while running. The summary is STORED (picked up by the fleet digest,
        send_daily_digest.py) rather than emailed per-bot, unless
        CMM_SUMMARY_EMAIL_PER_BOT=1. Initialized on the first tick so a
        mid-day (re)start doesn't fire immediately."""
        now_ct = now_utc.astimezone(CT)
        if self.last_summary_date is None:
            self.last_summary_date = (now_ct.date() if now_ct.hour >= SUMMARY_HOUR_CT
                                      else now_ct.date() - timedelta(days=1))
            return
        if now_ct.hour >= SUMMARY_HOUR_CT and now_ct.date() != self.last_summary_date:
            self.last_summary_date = now_ct.date()
            body = body_builder()
            self.last_summary_body = body
            log(f"[{self.market_key}] daily summary: {body}")
            if self.enabled and os.environ.get("CMM_SUMMARY_EMAIL_PER_BOT") == "1":
                self.send_message(body, subject=f"{self.market_key} daily summary")
            self.today.clear()


# ----------------------------------------------------------------------------
# The market maker
# ----------------------------------------------------------------------------

@dataclass
class BotState:
    event_ticker: str = ""
    live_price: Optional[float] = None
    sigma_daily: Optional[float] = None
    vol_fetched_at: float = 0.0
    daily_candles: List[Tuple[int, float, float, float]] = field(default_factory=list)
    session_extreme: Optional[float] = None    # session high (max) / low (min)
    mtd_extreme: Optional[float] = None        # accumulated month-to-date extreme
    mtd_hourly_at: float = 0.0                 # last successful hourly-candle fetch
    order_ages: Dict[str, float] = field(default_factory=dict)   # order_id -> placed_at
    sim_orders: Dict[str, dict] = field(default_factory=dict)    # dry-run resting book
    # Local ledger of orders WE placed (live mode): authoritative against
    # Kalshi's eventually-consistent portfolio reads, so a lagging read can
    # never make us place a duplicate ladder. order_id -> order-shaped dict
    # with _placed_at/_confirmed metadata.
    ledger: Dict[str, dict] = field(default_factory=dict)
    blind_streak: Dict[str, int] = field(default_factory=dict)   # ticker -> consecutive orderbook failures
    consecutive_errors: int = 0
    # daily-summary counters (reset when the summary sends)
    cycles_today: int = 0
    placed_today: int = 0
    cancelled_today: int = 0
    errors_today: int = 0
    last_markets_line: str = ""    # most recent per-market snapshot for the summary
    last_event_net: float = 0.0


class TouchMarketMaker:
    def __init__(self, cfg: MarketConfig, client: Optional[ExchangeClient], live: bool):
        self.cfg = cfg
        self.client = client
        self.live = live
        self.state = BotState()
        self.tag = f"[{cfg.key}]"
        self.alerter = Alerter(cfg.key, live)
        self._shutdown_done = False

    # ---- exchange wrappers (all writes gated on self.live) -----------------

    def place_order(self, q: Quote, now_ts: float) -> None:
        client_order_id = f"{CLIENT_ORDER_PREFIX}-{RUN_ID}-{uuid.uuid4().hex[:12]}"
        # Stamp expiration at SEND time, not cycle start: a slow placement
        # wave (dozens of orders x ~2s) would otherwise burn minutes of TTL
        # before late orders even reach the exchange.
        expiration_ts = int(time.time() + ORDER_TTL_SECS)
        if q.book_side == "bid":
            kwargs = dict(side="yes", action="buy", yes_price=q.price_cents)
        else:  # sell YES == buy NO at (100 - yes_price)
            kwargs = dict(side="no", action="buy", no_price=100 - q.price_cents)
        label = (f"{q.ticker} {q.book_side.upper():4s} {q.count}x @ {q.price_cents}c "
                 f"(as {kwargs['side']} {kwargs['action']} "
                 f"{kwargs.get('yes_price', kwargs.get('no_price'))}c)")
        if not self.live:
            oid = f"sim-{uuid.uuid4().hex[:12]}"
            self.state.sim_orders[oid] = {
                "order_id": oid, "ticker": q.ticker, "book_side": q.book_side,
                "yes_price": q.price_cents, "remaining_count": float(q.count),
                "status": "resting", "client_order_id": client_order_id,
                "expire_at": now_ts + ORDER_TTL_SECS,
            }
            self.state.order_ages[oid] = now_ts
            log(f"{self.tag} [DRY] place {label}")
            return
        try:
            resp = self.client.create_order(
                ticker=q.ticker, client_order_id=client_order_id,
                count=q.count, type="limit", post_only=True,
                expiration_ts=expiration_ts, **kwargs)
            oid = (resp.get("order") or {}).get("order_id") or resp.get("order_id", "?")
            self.state.order_ages[oid] = now_ts
            self.state.ledger[oid] = {
                "order_id": oid, "ticker": q.ticker, "book_side": q.book_side,
                "yes_price": q.price_cents, "remaining_count": float(q.count),
                "status": "resting", "client_order_id": client_order_id,
                "_placed_at": now_ts, "_confirmed": False,
            }
            log(f"{self.tag} placed {label} -> {oid}")
        except HttpError as e:
            # Post-only orders that would cross are rejected — expected occasionally.
            log(f"{self.tag} ! place rejected ({e}): {label}")
        except Exception as e:
            log(f"{self.tag} ! place failed ({e}): {label}")

    def cancel_order(self, order_id: str) -> bool:
        """Returns True when the order is verifiably no longer resting."""
        if not self.live:
            self.state.sim_orders.pop(order_id, None)
            self.state.order_ages.pop(order_id, None)
            log(f"{self.tag} [DRY] cancel {order_id}")
            return True
        try:
            self.client.cancel_order(order_id)
            self.state.order_ages.pop(order_id, None)
            self.state.ledger.pop(order_id, None)
            log(f"{self.tag} cancelled {order_id}")
            return True
        except HttpError as e:
            if e.status in (404, 409):   # already cancelled / already filled
                self.state.order_ages.pop(order_id, None)
                self.state.ledger.pop(order_id, None)
                log(f"{self.tag} cancel {order_id}: already gone ({e.status})")
                return True
            log(f"{self.tag} ! cancel failed {order_id}: {e}")
            return False
        except Exception as e:
            log(f"{self.tag} ! cancel failed {order_id}: {e}")
            return False

    def _get_resting_orders(self, event_ticker: str) -> List[dict]:
        """All of this bot's resting orders on an event (live mode), paginated."""
        orders: List[dict] = []
        cursor = None
        for _page in range(20):
            resp = self.client.get_orders(event_ticker=event_ticker,
                                          status="resting", limit=200, cursor=cursor)
            batch = resp.get("orders") or []
            orders.extend(o for o in batch
                          if o.get("status") == "resting"
                          and str(o.get("client_order_id", "")).startswith(CLIENT_ORDER_PREFIX + "-"))
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        return orders

    def _merge_ledger(self, exchange_orders: List[dict], now_ts: float) -> List[dict]:
        """Union of the exchange's view and our local ledger. Kalshi portfolio
        reads are eventually consistent: an order we placed seconds ago may be
        missing from get_orders, and trusting that read verbatim would make
        the reconcile place a duplicate ladder. Ledger entries the read hasn't
        confirmed yet count as resting during a grace window; past it they are
        verified once via get_order. Entries confirmed earlier but now absent
        are done (filled/cancelled/expired) and dropped."""
        merged = list(exchange_orders)
        seen_ids = {o.get("order_id") for o in exchange_orders}
        grace = 2 * POLL_SECS + 15
        for oid, entry in list(self.state.ledger.items()):
            if oid in seen_ids:
                entry["_confirmed"] = True
                continue
            if entry["_confirmed"] or now_ts >= entry["_placed_at"] + ORDER_TTL_SECS:
                # Was resting and vanished, or exchange TTL killed it: done.
                self.state.ledger.pop(oid, None)
                self.state.order_ages.pop(oid, None)
                continue
            if now_ts - entry["_placed_at"] <= grace:
                merged.append(entry)   # in-flight; read is lagging
                continue
            try:   # unconfirmed past grace: ask the exchange directly, once
                order = (self.client.get_order(oid) or {}).get("order") or {}
                if order.get("status") == "resting":
                    entry["_confirmed"] = True
                    merged.append(entry)
                else:
                    self.state.ledger.pop(oid, None)
                    self.state.order_ages.pop(oid, None)
            except HttpError as e:
                if e.status == 404:
                    self.state.ledger.pop(oid, None)
                    self.state.order_ages.pop(oid, None)
                else:
                    merged.append(entry)   # can't verify -> assume resting (safe)
            except Exception:
                merged.append(entry)
        return merged

    def fetch_resting_orders(self, now_ts: float) -> List[dict]:
        """Our resting orders on the current event (dry-run: the simulated book,
        with TTL expiry applied). Live view is the exchange read merged with
        the local ledger (see _merge_ledger)."""
        if not self.live:
            expired = [oid for oid, o in self.state.sim_orders.items()
                       if o.get("expire_at", 0) <= now_ts]
            for oid in expired:
                self.state.sim_orders.pop(oid, None)
                self.state.order_ages.pop(oid, None)
                log(f"{self.tag} [DRY] order {oid} TTL-expired")
            return list(self.state.sim_orders.values())
        return self._merge_ledger(self._get_resting_orders(self.state.event_ticker), now_ts)

    def cancel_all_bot_orders(self, include_prev_month: bool = False) -> int:
        """Cancel every resting order placed by this bot (any run) on the
        current event — and optionally the previous month's event, to cover
        kills that straddled a rollover. Returns count of orders cancelled."""
        if not self.live:
            n = len(self.state.sim_orders)
            self.state.sim_orders.clear()
            self.state.order_ages.clear()
            if n:
                log(f"{self.tag} [DRY] cancelled all {n} simulated orders")
            return n
        events = [self.state.event_ticker]
        if include_prev_month:
            events.append(prev_event_ticker_for(self.cfg, datetime.now(timezone.utc)))
        n = 0
        for i, ev in enumerate(events):
            if not ev:
                continue
            try:
                orders = self._get_resting_orders(ev)
                if i == 0:  # current event: include in-flight ledger orders too
                    orders = self._merge_ledger(orders, time.time())
                for o in orders:
                    if self.cancel_order(o["order_id"]):
                        n += 1
            except Exception as e:
                log(f"{self.tag} ! cancel-all sweep of {ev} failed: {e}")
        return n

    def fetch_positions(self) -> Dict[str, float]:
        """ticker -> signed YES position for the current event. Raises DataError
        on failure so the cycle counts as errored (fail safe, never quote blind)."""
        try:
            resp = self.client.get_positions(event_ticker=self.state.event_ticker, limit=200)
        except Exception as e:
            raise DataError(f"get_positions failed: {e}")
        positions = {}
        for p in (resp.get("market_positions") or resp.get("positions") or []):
            pos = p.get("position")
            if pos is not None and p.get("ticker"):
                positions[p["ticker"]] = float(pos)
        return positions

    # ---- data refresh -------------------------------------------------------

    def refresh_vol(self, now_ts: float, now_utc: datetime) -> None:
        if self.state.sigma_daily and now_ts - self.state.vol_fetched_at < VOL_REFRESH_SECS:
            return
        candles = fetch_ohlc(self.cfg, 1440)
        closes = [c for _ts, c, _h, _l in candles]
        # The last candle is the in-progress UTC day: its partial return would
        # get the heaviest EWMA weight and bias vol low, so drop it from the
        # returns (the candle itself still counts for month-to-date extremes).
        today_utc_ts = int(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        if candles and candles[-1][0] >= today_utc_ts:
            closes = closes[:-1]
        sigma = blended_daily_vol(closes)
        if not (0.001 <= sigma <= 0.5):
            raise DataError(f"daily vol {sigma:.4f} outside sanity range")
        self.state.daily_candles = candles
        self.state.sigma_daily = sigma
        self.state.vol_fetched_at = now_ts
        log(f"{self.tag} vol refreshed: sigma_daily={sigma:.4f} "
            f"({sigma * math.sqrt(365):.1%} annualized), {len(candles)} daily candles")

    def refresh_mtd_extreme(self, now_utc: datetime) -> None:
        """Month-to-date extreme from hourly candles (refetched at most every
        MTD_REFRESH_SECS — Kraken rate limits per IP and the whole fleet
        shares one), plus daily candles and the session extreme, accumulated
        in state so spikes seen once are never forgotten. Failures degrade
        gracefully — the accumulated value stands."""
        start_ts = month_start_utc(now_utc).timestamp()
        candidates = [self.state.mtd_extreme, self.state.session_extreme]
        now_ts = now_utc.timestamp()
        if now_ts - self.state.mtd_hourly_at >= MTD_REFRESH_SECS:
            try:
                hourly = fetch_ohlc(self.cfg, 60)
                candidates.append(candles_extreme(hourly, start_ts, 3600, self.cfg.direction))
                self.state.mtd_hourly_at = now_ts
            except Exception as e:
                log(f"{self.tag} ! hourly MTD refresh failed ({e}); using accumulated extreme")
        candidates.append(candles_extreme(self.state.daily_candles, start_ts, 86400,
                                          self.cfg.direction))
        vals = [v for v in candidates if v is not None]
        if vals:
            self.state.mtd_extreme = max(vals) if self.cfg.direction == "max" else min(vals)

    def maybe_rollover(self, now_utc: datetime) -> None:
        ticker = event_ticker_for(self.cfg, now_utc)
        if ticker != self.state.event_ticker:
            if self.state.event_ticker:
                log(f"{self.tag} month rollover: {self.state.event_ticker} -> {ticker}; "
                    f"cancelling old-event orders")
                self.cancel_all_bot_orders()
            self.state.event_ticker = ticker
            self.state.session_extreme = None
            self.state.mtd_extreme = None
            self.state.blind_streak.clear()
            log(f"{self.tag} trading event {ticker}")
            self.alerter.alert("rollover", f"now trading {ticker}", key=ticker, urgent=False)

    def active_markets(self) -> List[dict]:
        resp = self.client.get_event(self.state.event_ticker)
        event = resp.get("event") or resp
        markets = event.get("markets") or resp.get("markets") or []
        return [m for m in markets if m.get("status") in ("active", "open")]

    # ---- one polling cycle --------------------------------------------------

    def run_cycle(self) -> None:
        now_utc = datetime.now(timezone.utc)
        now_ts = now_utc.timestamp()
        d = self.cfg.direction

        self.maybe_rollover(now_utc)

        price = fetch_live_price(self.cfg, self.state.live_price)
        self.state.live_price = price
        if self.state.session_extreme is None:
            self.state.session_extreme = price
        else:
            self.state.session_extreme = (max if d == "max" else min)(
                self.state.session_extreme, price)
        self.refresh_vol(now_ts, now_utc)
        self.refresh_mtd_extreme(now_utc)
        sigma = self.state.sigma_daily
        mtd = self.state.mtd_extreme

        end_utc = month_end_utc(now_utc)
        hours_left = (end_utc - now_utc).total_seconds() / 3600.0
        if hours_left < MIN_HOURS_LEFT:
            log(f"{self.tag} <{MIN_HOURS_LEFT}h left in measurement window; standing down")
            self.cancel_all_bot_orders()
            return

        try:
            markets = self.active_markets()
        except HttpError as e:
            if e.status == 404:
                log(f"{self.tag} event {self.state.event_ticker} not listed yet (404); waiting")
                return
            raise
        if not markets:
            log(f"{self.tag} no active markets on {self.state.event_ticker}")
            return

        positions = self.fetch_positions()   # raises DataError on failure (fail safe)
        event_net = sum(positions.values())
        self.state.last_event_net = event_net
        # Room left before the aggregate event cap, assuming every ladder fills.
        event_room_buy = MAX_EVENT_CONTRACTS - event_net
        event_room_sell = MAX_EVENT_CONTRACTS + event_net

        # Our resting orders, fetched up front so each market's book can be
        # netted of our own size (join-don't-lead needs the EXTERNAL best).
        resting = self.fetch_resting_orders(now_ts)
        own_by_ticker: Dict[str, List[Tuple[str, int, float]]] = {}
        for o in resting:
            parsed = order_yes_book_cents(o)
            if parsed is not None:
                own_by_ticker.setdefault(o.get("ticker", ""), []).append(
                    (parsed[0], parsed[1], order_remaining(o)))

        # Price all markets first, then allocate the event budget near-money-first.
        priced = []
        summary = [f"{self.tag} {self.cfg.asset}=${price:,.2f} dir={d} sigma={sigma:.4f} "
                   f"T={hours_left / 24:.1f}d MTD{'high' if d == 'max' else 'low'}="
                   f"${mtd:,.2f} event={self.state.event_ticker} "
                   f"net={event_net:+.0f}" if mtd is not None else
                   f"{self.tag} {self.cfg.asset}=${price:,.2f} dir={d} sigma={sigma:.4f} "
                   f"T={hours_left / 24:.1f}d MTD=? event={self.state.event_ticker}"]
        for m in markets:
            ticker = m["ticker"]
            strike = market_strike(m, d)
            if strike is None:
                log(f"{self.tag} ! {ticker} strike/strike_type mismatch for dir={d}; skipping")
                continue
            exp = parse_iso_utc(m.get("expected_expiration_time", "")) or end_utc
            t_days = max(0.0, (exp - now_utc).total_seconds() / 86400.0)

            if breached(strike, mtd, d):
                summary.append(f"  {ticker} strike={strike:g}: MTD extreme {mtd:,.2f} crossed "
                               f"strike; likely touched -> not quoting")
                continue

            fair = fair_value_cents(price, strike, sigma, t_days, d)
            if fair >= SKIP_FAIR_ABOVE_CENTS:
                summary.append(f"  {ticker} strike={strike:g}: fair={fair}c >= "
                               f"{SKIP_FAIR_ABOVE_CENTS}c (near touch) -> not quoting")
                continue
            priced.append((abs(fair - 50), ticker, strike, fair))

        desired: List[Quote] = []
        blind: Set[str] = set()
        remaining_markets = len(priced)
        for _moneyness, ticker, strike, fair in sorted(priced):
            # Even split of the event budget over the markets not yet quoted
            # (near-money first); shares a skipped market doesn't use roll
            # forward automatically since the budget is only drawn on quoting.
            share_buy = event_room_buy / remaining_markets if remaining_markets > 0 else 0.0
            share_sell = event_room_sell / remaining_markets if remaining_markets > 0 else 0.0
            remaining_markets -= 1
            try:
                yes_levels, no_levels = orderbook_levels(
                    self.client.get_orderbook(ticker=ticker))
                best_bid, best_ask = external_best(yes_levels, no_levels,
                                                   own_by_ticker.get(ticker, []))
                self.state.blind_streak.pop(ticker, None)
            except Exception as e:
                streak = self.state.blind_streak.get(ticker, 0) + 1
                self.state.blind_streak[ticker] = streak
                if streak <= BLIND_PRESERVE_CYCLES:
                    blind.add(ticker)   # keep existing quotes, place nothing new
                    log(f"{self.tag} ! orderbook fetch failed for {ticker} ({e}); "
                        f"blind cycle {streak}/{BLIND_PRESERVE_CYCLES}, preserving quotes")
                else:
                    log(f"{self.tag} ! orderbook blind on {ticker} for {streak} cycles; "
                        f"cancelling its quotes")
                continue

            if (best_bid is not None and best_bid >= BOOK_DIVERGENCE_BID
                    and fair <= best_bid - BOOK_DIVERGENCE_GAP):
                summary.append(f"  {ticker} strike={strike:g}: book bid {best_bid}c vs "
                               f"fair {fair}c — suspected touch we can't see -> not quoting")
                self.alerter.alert("divergence", f"{ticker} book bid {best_bid}c vs our fair "
                                   f"{fair}c; suspected unseen touch, stood down", key=ticker)
                continue

            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2.0
                if abs(fair - mid) > MAX_FAIR_DIVERGENCE_CENTS:
                    summary.append(f"  {ticker} strike={strike:g}: fair={fair}c vs book mid "
                                   f"{mid:.0f}c — model/market disagree by >"
                                   f"{MAX_FAIR_DIVERGENCE_CENTS}c -> not quoting")
                    self.alerter.alert("model_divergence",
                                       f"{ticker} fair {fair}c vs book mid {mid:.0f}c; "
                                       f"standing down", key=ticker, urgent=False)
                    continue

            pos = positions.get(ticker, 0.0)
            room_buy = min(MAX_POSITION_CONTRACTS - pos, share_buy)
            room_sell = min(MAX_POSITION_CONTRACTS + pos, share_sell)
            quotes = build_quotes(ticker, fair, best_bid, best_ask, room_buy, room_sell)
            desired.extend(quotes)
            event_room_buy -= sum(q.count for q in quotes if q.book_side == "bid")
            event_room_sell -= sum(q.count for q in quotes if q.book_side == "ask")

            mkt = (f"ext {best_bid if best_bid is not None else '--'}/"
                   f"{best_ask if best_ask is not None else '--'}")
            our_bids = sorted((q.price_cents for q in quotes if q.book_side == "bid"), reverse=True)
            our_asks = sorted(q.price_cents for q in quotes if q.book_side == "ask")
            summary.append(f"  {ticker} strike={strike:g}: fair={fair}c {mkt} pos={pos:+.0f} "
                           f"bids={our_bids} asks={our_asks}")

        to_place, to_cancel = diff_orders(desired, resting, self.state.order_ages,
                                          now_ts, preserve_tickers=blind)

        for line in summary:
            log(line)
        log(f"{self.tag} orders: {len(resting)} resting, {len(to_cancel)} to cancel, "
            f"{len(to_place)} to place {'' if self.live else '[DRY RUN]'}")

        cancel_failures = 0
        cancelled_ids = set()
        for oid in to_cancel:
            if self.cancel_order(oid):
                self.state.cancelled_today += 1
                cancelled_ids.add(oid)
            else:
                cancel_failures += 1
        if cancel_failures:
            # A cancel we can't confirm is live risk; let the fail-safe machinery see it.
            raise DataError(f"{cancel_failures} cancel(s) failed")
        placed = self.place_with_side_cap(to_place, resting, cancelled_ids, now_ts)
        self.state.placed_today += placed
        self.state.cycles_today += 1

        # Snapshot for the daily summary.
        quoted = len({q.ticker for q in desired})
        self.state.last_markets_line = (
            f"{quoted}/{len(markets)} mkts quoted ({len(desired)} quotes)"
            if markets else "no active markets")

    def write_status(self, now_utc: datetime) -> None:
        """Heartbeat for the fleet digest: current stats + the stored daily
        summary. Atomic write; failures are logged, never raised."""
        s = self.state
        cats: Dict[str, int] = {}
        for cat, _msg in self.alerter.today:
            cats[cat] = cats.get(cat, 0) + 1
        status = {
            "market": self.cfg.key,
            "mode": "LIVE" if self.live else "DRY",
            "pid": os.getpid(),   # restart tooling kills by PID, not cmdline match
            "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": s.event_ticker,
            "price": s.live_price,
            "sigma_daily": s.sigma_daily,
            "mtd_extreme": s.mtd_extreme,
            "net_position": s.last_event_net,
            "markets_line": s.last_markets_line,
            "cycles_today": s.cycles_today,
            "placed_today": s.placed_today,
            "cancelled_today": s.cancelled_today,
            "errors_today": s.errors_today,
            "alerts_today": cats,
            "summary_date": str(self.alerter.last_summary_date or ""),
            "summary_body": self.alerter.last_summary_body or "",
        }
        try:
            path = os.path.join(STATUS_DIR, f"status_{self.cfg.key}.json")
            tmp = path + ".tmp"
            os.makedirs(STATUS_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f)
            os.replace(tmp, path)
        except Exception as e:
            log(f"{self.tag} ! status write failed: {e}")

    def place_with_side_cap(self, to_place: List[Quote], resting: List[dict],
                            cancelled_ids: Set[str], now_ts: float) -> int:
        """Place orders behind two unconditional backstops (beyond the ledger
        merge): a (market, side) never exceeds NUM_LEVELS x
        CONTRACTS_PER_LEVEL resting contracts, and a single price LEVEL on a
        market never exceeds CONTRACTS_PER_LEVEL. Returns orders placed."""
        cap = NUM_LEVELS * CONTRACTS_PER_LEVEL
        side_totals: Dict[Tuple[str, str], float] = {}
        level_totals: Dict[Tuple[str, str, int], float] = {}
        for o in resting:
            if o.get("order_id") in cancelled_ids:
                continue
            parsed = order_yes_book_cents(o)
            if parsed is None:
                continue
            rem = order_remaining(o)
            skey = (o.get("ticker", ""), parsed[0])
            side_totals[skey] = side_totals.get(skey, 0.0) + rem
            lkey = (o.get("ticker", ""), parsed[0], parsed[1])
            level_totals[lkey] = level_totals.get(lkey, 0.0) + rem
        placed = 0
        for q in to_place:
            skey = (q.ticker, q.book_side)
            lkey = (q.ticker, q.book_side, q.price_cents)
            have_side = side_totals.get(skey, 0.0)
            have_level = level_totals.get(lkey, 0.0)
            if have_side + q.count > cap + 0.01:
                log(f"{self.tag} ! side cap: NOT placing {q.book_side} {q.count}x "
                    f"@ {q.price_cents}c on {q.ticker} ({have_side:.0f} already resting, "
                    f"cap {cap})")
                self.alerter.alert("side_cap", f"{q.ticker} {q.book_side}: placement "
                                   f"blocked at {have_side:.0f}/{cap} contracts",
                                   key=f"{q.ticker}-{q.book_side}", urgent=False)
                continue
            if have_level + q.count > CONTRACTS_PER_LEVEL + 0.01:
                log(f"{self.tag} ! level cap: NOT placing {q.book_side} {q.count}x "
                    f"@ {q.price_cents}c on {q.ticker} ({have_level:.0f} already at that "
                    f"level, cap {CONTRACTS_PER_LEVEL})")
                self.alerter.alert("level_cap", f"{q.ticker} {q.book_side}@{q.price_cents}c: "
                                   f"placement blocked at {have_level:.0f}/"
                                   f"{CONTRACTS_PER_LEVEL} contracts",
                                   key=f"{q.ticker}-{q.price_cents}", urgent=False)
                continue
            self.place_order(q, now_ts)
            side_totals[skey] = have_side + q.count
            level_totals[lkey] = have_level + q.count
            placed += 1
        return placed

    # ---- daily summary -------------------------------------------------------

    def build_daily_summary(self) -> str:
        s = self.state
        d = self.cfg.direction
        price = f"${s.live_price:,.2f}" if s.live_price is not None else "?"
        sigma = f"{s.sigma_daily * 100:.1f}%/d" if s.sigma_daily else "?"
        mtd = (f" MTD{'hi' if d == 'max' else 'lo'} ${s.mtd_extreme:,.2f}"
               if s.mtd_extreme is not None else "")
        alerts = self.alerter.today
        if alerts:
            cats: Dict[str, int] = {}
            for cat, _msg in alerts:
                cats[cat] = cats.get(cat, 0) + 1
            alert_str = " ".join(f"{c}x{n}" for c, n in sorted(cats.items()))
        else:
            alert_str = "none"
        body = (f"{self.cfg.key} daily ({'LIVE' if self.live else 'DRY'}): "
                f"{self.cfg.asset} {price} vol {sigma}{mtd} | "
                f"{s.last_markets_line}, net pos {s.last_event_net:+.0f} | "
                f"cycles {s.cycles_today}, placed {s.placed_today}, "
                f"cxl {s.cancelled_today}, errs {s.errors_today} | alerts: {alert_str}")
        s.cycles_today = s.placed_today = s.cancelled_today = s.errors_today = 0
        return body

    # ---- main loop ----------------------------------------------------------

    def shutdown_cancel(self) -> None:
        """Idempotent final cancel — called from finally, signals, and atexit."""
        if self._shutdown_done or not self.live:
            return
        self._shutdown_done = True
        try:
            n = self.cancel_all_bot_orders(include_prev_month=True)
            log(f"{self.tag} shutdown: cancelled {n} resting bot orders")
        except Exception as e:
            log(f"{self.tag} ! shutdown cancel failed: {e} "
                f"(orders TTL-expire within {ORDER_TTL_SECS}s)")

    def run(self, once: bool = False) -> None:
        mode = "LIVE" if self.live else "DRY RUN (no orders will be placed; use --live to trade)"
        log(f"=== {MODEL_VERSION} run={RUN_ID} market={self.cfg.key} mode={mode} ===")
        log(f"quoting: {NUM_LEVELS} levels x {CONTRACTS_PER_LEVEL} contracts, "
            f"{QUOTE_OFFSET_CENTS}c off fair, {LEVEL_SPACING_CENTS}c apart, post-only, "
            f"TTL {ORDER_TTL_SECS}s, per-market cap {MAX_POSITION_CONTRACTS:g}, "
            f"event cap {MAX_EVENT_CONTRACTS:g}")

        self.state.event_ticker = event_ticker_for(self.cfg, datetime.now(timezone.utc))
        if self.live:
            # Clean slate: remove our orders from any prior run, incl. across rollover.
            n = self.cancel_all_bot_orders(include_prev_month=True)
            log(f"{self.tag} startup: cancelled {n} leftover bot orders")

        stopping = {"flag": False}
        prev_top: Optional[float] = None
        grace_until = 0.0

        def _stop(signame):
            def handler(_sig, _frm):
                stopping["flag"] = True
                log(f"{signame}: shutting down after this cycle...")
            return handler

        signal.signal(signal.SIGINT, _stop("SIGINT"))
        for sig_name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, sig_name):
                try:
                    signal.signal(getattr(signal, sig_name), _stop(sig_name))
                except (OSError, ValueError):
                    pass
        atexit.register(self.shutdown_cancel)

        try:
            while True:
                top = time.time()
                if prev_top is not None and top - prev_top > WAKE_GAP_SECS:
                    grace_until = top + WAKE_GRACE_SECS
                    log(f"{self.tag} resume detected ({top - prev_top:.0f}s gap); "
                        f"errors won't count toward fail-safe for {WAKE_GRACE_SECS}s")
                prev_top = top
                try:
                    self.run_cycle()
                    self.state.consecutive_errors = 0
                except Exception as e:
                    # Broad by design: an unexpected KeyError from an API shape
                    # change must trigger the fail-safe, not kill the process.
                    now = time.time()
                    if now - top > WAKE_GAP_SECS:   # suspend hit mid-cycle
                        grace_until = now + WAKE_GRACE_SECS
                    self.state.errors_today += 1
                    if now < grace_until:
                        log(f"{self.tag} ! cycle error during wake grace "
                            f"(not counted for fail-safe): {e!r}")
                        continue_failsafe = False
                    else:
                        continue_failsafe = True
                        self.state.consecutive_errors += 1
                        log(f"{self.tag} ! cycle error #{self.state.consecutive_errors}: {e!r}")
                    if continue_failsafe and self.state.consecutive_errors >= FAILSAFE_CANCEL_AFTER:
                        log(f"{self.tag} fail-safe: cancelling all resting orders")
                        try:
                            self.cancel_all_bot_orders()
                        except Exception as e2:
                            log(f"{self.tag} ! fail-safe cancel failed: {e2} "
                                f"(orders TTL-expire within {ORDER_TTL_SECS}s)")
                        # Digest-only: standby-wake windows fire these across all
                        # 16 bots at once (an email storm); the digest error
                        # counts + STALE line carry the signal instead.
                        self.alerter.alert(
                            "failsafe",
                            f"{self.state.consecutive_errors} consecutive cycle errors "
                            f"(last: {e!r:.120}); cancelled all resting orders",
                            urgent=False,
                            key="failsafe")
                now_utc = datetime.now(timezone.utc)
                self.alerter.maybe_daily_summary(now_utc, self.build_daily_summary)
                self.write_status(now_utc)
                if once or stopping["flag"]:
                    break
                backoff = min(2 ** max(0, self.state.consecutive_errors - 1), 8)
                time.sleep(POLL_SECS * backoff if self.state.consecutive_errors else POLL_SECS)
        finally:
            self.shutdown_cancel()
            if not once:
                # Digest-only: routine restarts (config rollouts, watchdog) fired
                # 16 urgent emails per fleet restart — pure noise. Real fleet
                # death shows as STALE heartbeats in the digest.
                self.alerter.alert("shutdown",
                                   f"bot stopped (run {RUN_ID}); resting orders "
                                   f"{'cancelled' if self.live else 'were simulated'}",
                                   key="shutdown", urgent=False)
        log("=== done ===")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def build_client() -> ExchangeClient:
    private_key = load_private_key()
    client = ExchangeClient(exchange_api_base=KALSHI_API_BASE,
                            key_id=KEY_ID, private_key=private_key)
    status = client.get_exchange_status()
    log(f"exchange status: {status}")
    return client


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", default="SOL-MAX", choices=sorted(MARKETS),
                    help="which crypto monthly one-touch event to make markets on")
    ap.add_argument("--live", action="store_true",
                    help="actually place/cancel orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel this bot's REAL resting orders on the current and previous "
                         "month's events, then exit (always live — cancelling only reduces risk)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test SMS via the configured alert route, then exit")
    args = ap.parse_args(argv)

    cfg = MARKETS[args.market]

    if args.test_alert:
        alerter = Alerter(cfg.key, live=False)
        if not alerter.enabled:
            log("cannot test: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not set")
            return 1
        ok = alerter.send_message(f"[{cfg.key}] test alert from {MODEL_VERSION} — "
                                  f"alert route working",
                                  subject=f"{cfg.key} test alert")
        log(f"test alert to {ALERT_RECIPIENTS}: {'sent' if ok else 'FAILED'}")
        return 0 if ok else 1

    client = build_client()

    if args.cancel_all:
        # Cancellation is inherently risk-reducing: always run it against the
        # real book, whether or not --live was passed.
        bot = TouchMarketMaker(cfg, client, live=True)
        bot.state.event_ticker = event_ticker_for(cfg, datetime.now(timezone.utc))
        n = bot.cancel_all_bot_orders(include_prev_month=True)
        log(f"cancelled {n} real resting orders for {cfg.key} "
            f"(current + previous month events)")
        return 0

    bot = TouchMarketMaker(cfg, client, live=args.live)
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
