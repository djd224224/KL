#!/usr/bin/env python3
"""
crypto_updown_mm.py — Market maker for Kalshi crypto ABOVE/BELOW events
(KX{ASSET}D): "What will {ASSET}'s price be on {date} at {hour} ET?"

  KXBTCD-26AUG0521-T54099.99  ->  YES if BTC >= $54,100 AT 9pm ET on Aug 5

*** THIS IS NOT A ONE-TOUCH MARKET. *** That distinction is the whole reason
this is a separate bot from crypto_touch_mm.py, and it is easy to miss: these
markets carry strike_type="greater" and a floor_strike, EXACTLY like the MAX
one-touch markets, so crypto_touch_mm.py would parse them without complaint
and price them with touch_prob(). It settles on the terminal price instead:

    rules_primary: "If the simple average of the sixty seconds of CF
    Benchmarks' Bitcoin Real-Time Index before 9 PM EDT is above 54099.99 AT
    9 PM EDT ... then the market resolves to Yes."

P(ever touches K) is ~2.0-2.1x P(ends above K) for the same strike and
horizon (measured across the tenors here; see test_crypto_updown_mm.py, which
pins the ratio). Pricing these with the one-touch model would roughly double
every fair value and systematically overpay for YES / undersell NO across the
whole ladder, with no existing guard catching it because the numbers still
look plausible. Hence a separate module, a separate fair(), and a regression
test asserting the two models disagree.

Fair value = P(S_T >= K) under driftless-in-price GBM (E[S_T] = S_0, log drift
-sigma^2/2), i.e. Black-Scholes N(d2) with r=0. Validated against exact
Monte Carlo to <0.001 at every tenor traded here.

Three tenors trade under one series, distinguished by WINDOW LENGTH (taken
from the exchange, not from ticker conventions):

    hourly  ~1h    e.g. KXBTCD-26AUG0521   188 strikes ($100 apart on BTC)
    daily   ~25h   e.g. KXBTCD-26AUG0517    80 strikes   (the 5pm ET events)
    weekly  ~169h  e.g. KXBTCD-26AUG0717    80 strikes   (also 5pm ET)

LIVE PILOT SETTINGS (2026-08-10): --cadence weekly, all assets, a 3 x 2
ladder 3c off fair and 2c apart, quoting only strikes fairing between 10c and
90c, at most 8 markets per event. Max 6 contracts resting per market per side
(pilot began 3x1 on 8/5; doubled + caps 2x'd to 80/400/800 on 8/10).

One bot quotes ALL selected tenors of one asset concurrently (--cadence),
because at any moment only ~3 events are open per asset and they are the same
product at different maturities. Volatility is horizon-matched: a 1-hour
market priced off 90-day daily candles would be badly stale, so the estimator
samples 5m / 60m / 1d candles by time-to-expiry and blends with the slow
daily estimate for stability.

Reuses crypto_touch_mm's hardened machinery by SUBCLASSING TouchMarketMaker:
the order ledger, cancel-confirm, per-level/per-side/per-event caps, the
fail-safe loop, wake grace, TTL handling, alerting, the daily summary and the
shared Kraken data cache are all that module's code. run_cycle() is replaced
(multi-event, different model) and so is the vol estimator.

Coverage as of 2026-08-05: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE are live.
KXZECD lists no events (configured anyway; it simply idles).

SAFETY: DRY RUN by default. --live is explicit. --cancel-all always operates
on the REAL book.

Usage:
    python crypto_updown_mm.py --discover
    python crypto_updown_mm.py --asset BTC --once
    python crypto_updown_mm.py --asset BTC --cadence daily,weekly --live
    python crypto_updown_mm.py --asset BTC --cancel-all
"""

import argparse
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# --- env shim: must precede the crypto_touch_mm import (see the weekly bot's
# header for the full rationale). Only operator-set overrides are forwarded, so
# importing this module can never retune a monthly bot sharing the interpreter.
# NB: the ladder shape (levels/size/offset/spacing) is NOT forwarded — it is
# passed to build_quotes() from the class attributes below, so this bot can run
# a 3x1 ladder without resizing the monthly fleet's 5x10 in a shared process.
_PASSTHROUGH = {
    "CUD_SUMMARY_HOUR_CT":     "CMM_SUMMARY_HOUR_CT",
    "CUD_ALERT_TO":            "CMM_ALERT_TO",
}
for _cud, _cmm in _PASSTHROUGH.items():
    if os.environ.get(_cud):
        os.environ[_cmm] = os.environ[_cud]

import crypto_touch_mm as mm            # noqa: E402
from crypto_touch_mm import DataError, log   # noqa: E402

MODEL_VERSION = "crypto_updown_mm_v1.0"
CLIENT_ORDER_PREFIX = os.environ.get("CUD_CLIENT_ORDER_PREFIX", "cud")


def _env_f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CADENCES = ("hourly", "daily", "weekly", "annual")

# Tenor boundaries in hours, applied to the event's OWN window (close - open).
# Classifying by exchange metadata rather than by the ticker's hour field means
# a new tenor, or a change to Kalshi's 5pm-ET convention, degrades gracefully.
HOURLY_MAX_HOURS = _env_f("CUD_HOURLY_MAX_HOURS", 3.0)
DAILY_MAX_HOURS = _env_f("CUD_DAILY_MAX_HOURS", 48.0)
# Above this the event is no KX*D tenor at all but a YEAR-scale window (the
# KX{ASSET}Y end-of-year events run ~8700h; observed weeklies run ~169h). The
# boundary exists so crypto_annual_mm.py can reuse this classifier, and so a
# stray long-dated listing on a weekly series is dropped rather than quoted
# with week-tuned settings.
WEEKLY_MAX_HOURS = _env_f("CUD_WEEKLY_MAX_HOURS", 400.0)

# Stop quoting this long before expiry, per tenor. A terminal binary's delta
# becomes a step function at expiry, and settlement is the average of the final
# 60 seconds (settlement_timer_seconds=60) — the worst possible moment to be
# resting size. Scaled to the tenor: 3 min is ~5% of an hourly window.
MIN_SECS_LEFT = {
    "hourly": _env_f("CUD_MIN_SECS_LEFT_HOURLY", 180),
    "daily": _env_f("CUD_MIN_SECS_LEFT_DAILY", 900),
    "weekly": _env_f("CUD_MIN_SECS_LEFT_WEEKLY", 3600),
    "annual": _env_f("CUD_MIN_SECS_LEFT_ANNUAL", 86400),
}

# An hourly event lists 188 strikes $100 apart; fetching an orderbook for each
# would be ~188 API calls per event per cycle. Quote only a near-money band.
# Tightened to 10-90c (Jack, 2026-08-05): outside that the contract is close
# enough to resolved that the spread is mostly model error, and a 1c tick is a
# large fraction of the price.
SKIP_FAIR_ABOVE_CENTS = _env_i("CUD_SKIP_FAIR_ABOVE_CENTS", 90)
SKIP_FAIR_BELOW_CENTS = _env_i("CUD_SKIP_FAIR_BELOW_CENTS", 10)
MAX_MARKETS_PER_EVENT = _env_i("CUD_MAX_MARKETS_PER_EVENT", 8)

# Ladder shape. Pilot started 3x1 (2026-08-05); doubled to 3x2 (Jack,
# 2026-08-10 "double it") after two clean days. Bound per (market, side):
# num_levels * contracts_per_level = 6.
NUM_LEVELS = _env_i("CUD_NUM_LEVELS", 3)
CONTRACTS_PER_LEVEL = _env_i("CUD_CONTRACTS_PER_LEVEL", 2)
# 3c off fair, not the one-touch fleet's 5c (Jack, 2026-08-05). These books are
# ~2c wide, so a 5c offset parked us 5-6c behind the touch on both sides and
# would almost never have filled. Join-don't-lead still caps how aggressive
# this can get: we reach the touch only where our fair beats the market's.
QUOTE_OFFSET_CENTS = _env_i("CUD_QUOTE_OFFSET_CENTS", 3)
LEVEL_SPACING_CENTS = _env_i("CUD_LEVEL_SPACING_CENTS", 2)

# Per-TENOR ladder overrides. Cadences absent from a table use the bot-wide
# value. The side/level-cap backstops in place_with_side_cap stay derived
# from the LARGEST shape; they are upper bounds, and the ladder actually
# built per event is what enforces the per-tenor size.
#
# History: daily went live 2026-08-14 at 1/1/1 (three 1-contract rungs, 3c
# off fair). First two settles lost -110.93, -105 of it KXBTCD near-money
# strikes picked off from both sides at the print — 3c off a coin-flip fair
# on $250-wide BTC strikes was not enough edge. Jack 2026-08-16 ("daily
# crypto bot is losing"): daily is now a 1/1 ladder (TWO 1-contract rungs)
# quoting 4c off fair. Weekly keeps 3x2 @3c (its first settled week: +252).
CONTRACTS_PER_LEVEL_BY_CADENCE = {
    "daily": _env_i("CUD_CONTRACTS_PER_LEVEL_DAILY", 1),
}
NUM_LEVELS_BY_CADENCE = {
    "daily": _env_i("CUD_NUM_LEVELS_DAILY", 2),
}
# Jack 2026-08-18 ("do 1 only on BTC, keep 1/1 on others"): BTC — where all
# of the daily loss lived — quotes a SINGLE 1-contract rung per side; the six
# benign assets keep two rungs. Looked up before the per-tenor table, like
# the offset override.
NUM_LEVELS_BY_ASSET_CADENCE = {
    ("BTC", "daily"): _env_i("CUD_NUM_LEVELS_DAILY_BTC", 1),
}
QUOTE_OFFSET_BY_CADENCE = {
    "daily": _env_i("CUD_QUOTE_OFFSET_DAILY", 4),
}
# Per-(asset, tenor) offset override, looked up before the per-tenor table.
# BTC carried -105 of the daily tenor's first -111 (its books are the most
# competitive, so our fills there are the most informed); the six benign
# assets stay at the tenor offset rather than being taxed for BTC's flow.
QUOTE_OFFSET_BY_ASSET_CADENCE = {
    ("BTC", "daily"): _env_i("CUD_QUOTE_OFFSET_DAILY_BTC", 6),
}

# ---- daily-tenor defenses (Jack 2026-08-17, after the fill autopsy) -------
# The 8/14-8/16 daily losses were NOT settlement-window gamma: per-fill P&L
# bucketed by hours-to-close showed the bleed spread across the whole day
# (worst 12-18h out = the overnight session; the final hour was POSITIVE),
# 3.3x heavier on the NO side, all of it BTC. Mechanism: driftless fair +
# no inventory memory = the bot re-offered YES into a rising market all
# night. Two defenses, both daily-only (weekly's warehousing made +252 its
# first week — do not generalize):
#
# INVENTORY SKEW: the quoting fair (never the divergence-guard fair) shifts
# against the EVENT's net position — long YES quotes cheaper, short YES
# quotes richer — saturating at +/-SKEW cents once |net| reaches FULL_AT
# contracts. cadence -> (max_cents, full_at_contracts).
SKEW_BY_CADENCE = {
    "daily": (_env_f("CUD_SKEW_CENTS_DAILY", 4.0),
              _env_f("CUD_SKEW_FULL_AT_DAILY", 25.0)),
}
# The skew-THINNED side's edge never drops below this floor (adversarial
# review 2026-08-17): event_net is ACCOUNT-level, so a manual position on the
# same event could otherwise pin skew and thin the bot's edge to 0-2c —
# recreating the original bleed while fading Jack's own deliberate bet. With
# a 3c floor the effective skew is +/-3c on BTC daily (offset 6) and +/-1c
# elsewhere (offset 4); the momentum guard carries sustained drift.
SKEW_EDGE_FLOOR_CENTS = _env_f("CUD_SKEW_EDGE_FLOOR", 3.0)
# MOMENTUM STAND-DOWN: when the 1h log-return exceeds MOMO_Z x sigma_1h
# (sigma_daily/sqrt(24)), stop quoting the side that FADES the move (asks in
# an up-move, bids in a down-move) on the listed cadences, and hold the
# block MOMO_HOLD_SECS past the last trigger. Same philosophy as the book-
# divergence guard, applied to the underlying: a moving market knows
# something the driftless fair does not.
MOMO_CADENCES = tuple(
    c.strip() for c in os.environ.get("CUD_MOMO_CADENCES", "daily").split(",")
    if c.strip() in CADENCES)
MOMO_Z = _env_f("CUD_MOMO_Z", 1.5)
MOMO_HOLD_SECS = _env_f("CUD_MOMO_HOLD_SECS", 1800)

# Two tenors of one series can settle on the SAME print: on Fridays the 5pm
# close IS the weekly event (no separate daily lists), and any daily/hourly
# event ending at the same moment as a longer-tenor event we quote would be
# the same terminal bet quoted twice. The shorter tenor stands down (Jack
# 2026-08-14: "do not let it collide with weekly").
TENOR_COLLISION_SECS = _env_f("CUD_TENOR_COLLISION_SECS", 300)

# Risk. Scaled 2x with the ladder (40/200/400 -> 80/400/800, 2026-08-10):
# still non-binding backstops (max 6 resting per market per side) — the
# ladder size IS the control. Revisit both together when scaling further.
MAX_POSITION_CONTRACTS = _env_f("CUD_MAX_POSITION", 80)    # per market
MAX_EVENT_CONTRACTS = _env_f("CUD_MAX_EVENT", 400)         # per event
MAX_ASSET_CONTRACTS = _env_f("CUD_MAX_ASSET", 800)         # across all tenors

# Model/market disagreement guard (same spirit as the one-touch bot).
MAX_FAIR_DIVERGENCE_CENTS = _env_i("CUD_MAX_FAIR_DIVERGENCE_CENTS", 15)

# Volatility. Horizon-matched sampling: a 1h market wants recent realised vol,
# a 7d market wants the stable daily estimate. Kraken spans: 5m->60h,
# 60m->30d, 1440m->2y.
VOL_INTERVAL_BY_HORIZON = ((0.25, 5), (3.0, 60), (float("inf"), 1440))
BARS_PER_DAY = {1: 1440, 5: 288, 15: 96, 60: 24, 1440: 1}
VOL_FAST_WEIGHT = _env_f("CUD_VOL_FAST_WEIGHT", 0.7)   # fast vs daily blend
VOL_REFRESH_SECS = _env_f("CUD_VOL_REFRESH_SECS", 300)
VOL_MIN, VOL_MAX = 0.001, 0.5                          # per-day sanity band

POLL_SECS = _env_i("CUD_POLL_SECS", 30)
IDLE_POLL_SECS = _env_f("CUD_IDLE_POLL_SECS", 300)
SWEEP_RECENT_EVENTS = _env_i("CUD_SWEEP_RECENT_EVENTS", 8)
STATUS_DIR = os.environ.get(
    "CUD_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\crypto-updown")

# One config per ASSET — unlike the one-touch fleet there is no MAX/MIN split:
# every market here is strike_type="greater" and the YES side is "above".
# Data sources are taken from the monthly table so the pairs cannot drift.
ASSETS: Dict[str, mm.MarketConfig] = {
    cfg.asset: replace(cfg, key=cfg.asset, series=f"KX{cfg.asset}D",
                       direction="above")
    for key, cfg in mm.MARKETS.items() if cfg.direction == "max"
}


def parse_cadences(spec: str) -> Tuple[str, ...]:
    """'both' -> hourly+daily, 'all' -> everything, else a comma list."""
    spec = (spec or "").strip().lower()
    if spec in ("", "both"):
        return ("hourly", "daily")
    if spec == "all":
        return CADENCES
    out = []
    for part in spec.split(","):
        part = part.strip()
        if part not in CADENCES:
            raise ValueError(f"unknown cadence {part!r}; pick from "
                             f"{', '.join(CADENCES)}, 'both' or 'all'")
        if part not in out:
            out.append(part)
    return tuple(out)


# ---------------------------------------------------------------------------
# Pricing (pure functions)
# ---------------------------------------------------------------------------

def terminal_prob(spot: float, strike: float, sigma_daily: float,
                  t_days: float) -> float:
    """P(S_T >= strike) under GBM that is driftless IN PRICE (E[S_T] = S_0, so
    the log drift is -sigma^2/2) — Black-Scholes N(d2) with r=0. Same
    convention as crypto_touch_mm.touch_prob, so the two are directly
    comparable; validated against exact Monte Carlo to <0.001.

    NOTE the contrast with touch_prob(): this is the probability of finishing
    above the strike, NOT of ever reaching it. For a strike above spot the
    touch probability is roughly twice this."""
    if spot <= 0 or strike <= 0:
        raise DataError(f"bad spot/strike: {spot}/{strike}")
    if t_days <= 0 or sigma_daily <= 0:
        return 1.0 if spot >= strike else 0.0
    nu = -0.5 * sigma_daily * sigma_daily
    d2 = (math.log(spot / strike) + nu * t_days) / (sigma_daily * math.sqrt(t_days))
    return min(max(_norm_cdf(d2), 0.0), 1.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value_cents(spot: float, strike: float, sigma_daily: float,
                     t_days: float) -> int:
    return max(1, min(99, round(100.0 * terminal_prob(spot, strike, sigma_daily, t_days))))


def scaled_daily_vol(candles: Sequence[Tuple[int, float, float, float]],
                     interval_minutes: int, drop_last: bool = True) -> float:
    """Per-DAY sigma from candles of an arbitrary interval: EWMA of the bar
    log-returns scaled by sqrt(bars per day). The final bar is in progress —
    its partial return would carry the heaviest EWMA weight and bias vol low."""
    bars = BARS_PER_DAY.get(interval_minutes)
    if not bars:
        raise DataError(f"no bars-per-day for interval {interval_minutes}m")
    closes = [c for _ts, c, _h, _l in candles]
    if drop_last and len(closes) > 2:
        closes = closes[:-1]
    rets = mm.log_returns(closes)
    if len(rets) < 20:
        raise DataError(f"only {len(rets)} returns at {interval_minutes}m")
    return mm.ewma_vol(rets) * math.sqrt(bars)


def vol_interval_for_horizon(t_days: float) -> int:
    for max_days, interval in VOL_INTERVAL_BY_HORIZON:
        if t_days <= max_days:
            return interval
    return 1440


def inventory_skew_cents(event_net: float, max_cents: float,
                         full_at: float) -> float:
    """Cents to ADD to the quoting fair given the event's net YES position:
    negative when long (quote cheaper, shed), positive when short (quote
    richer, stop re-offering / buy back). Linear, saturating at +/-max_cents
    once |net| reaches full_at contracts."""
    if max_cents <= 0 or full_at <= 0:
        return 0.0
    frac = max(-1.0, min(1.0, event_net / full_at))
    return -max_cents * frac


def momentum_block_side(ret_1h: float, sigma_daily: float,
                        z: float = MOMO_Z) -> Optional[str]:
    """'ask' when the 1h up-move exceeds z x sigma_1h (do not sell YES into
    strength), 'bid' on the mirror down-move, else None."""
    if sigma_daily <= 0:
        return None
    sigma_1h = sigma_daily / math.sqrt(24.0)
    if ret_1h > z * sigma_1h:
        return "ask"
    if ret_1h < -z * sigma_1h:
        return "bid"
    return None


# ---------------------------------------------------------------------------
# Event discovery (pure functions)
# ---------------------------------------------------------------------------

def updown_strike(market: dict) -> Optional[float]:
    """Strike of an above/below market, or None if it isn't one. Range
    ('between') and other structures are skipped rather than guessed at.

    Two shapes exist in this family:
      * strike_type="greater" with a numeric floor_strike — every asset but
        DOGE (verified across all 3 tenors, 2026-08-05).
      * strike_type="custom" with floor_strike=None — ALL of DOGE (118 markets
        across hourly/daily/weekly). The payoff is identical ("$0.18 or above")
        but the numeric strike exists only in the ticker suffix. Left
        unhandled, DOGE silently priced 0 strikes and quoted nothing.

    For the custom shape the strike is recovered from the ticker, but only when
    an INDEPENDENT source agrees: yes_sub_title must say "or above" (confirming
    the direction) and must carry the same number to within 1%. If the two
    disagree we skip the market rather than trade a guessed strike."""
    if market.get("strike_type") == "greater":
        try:
            return float(market["floor_strike"])
        except (KeyError, TypeError, ValueError):
            return None
    if market.get("strike_type") != "custom":
        return None

    subtitle = (market.get("yes_sub_title") or "").lower()
    if "or above" not in subtitle:
        return None            # not a greater-payoff rung; don't guess
    m = re.search(r"-T([0-9]*\.?[0-9]+)$", market.get("ticker", ""))
    if not m:
        return None
    try:
        strike = float(m.group(1))
    except ValueError:
        return None
    shown = re.search(r"([0-9][0-9,]*\.?[0-9]*)", subtitle.replace("$", ""))
    if not shown:
        return None
    try:
        displayed = float(shown.group(1).replace(",", ""))
    except ValueError:
        return None
    if displayed <= 0 or strike <= 0 or abs(strike / displayed - 1) > 0.01:
        return None            # ticker and subtitle disagree -> not safe
    return strike


def market_end_utc(market: dict) -> Optional[datetime]:
    """End of the market for GATING and PRICING purposes = close_time.

    The settlement print is the 60s average ENDING at close_time, while
    expected_expiration_time runs 300s LATER on every KX*D and annual market
    (verified against the live API 2026-08-14: +300s on 100% of 1000+ open
    markets checked). Preferring expected_expiration here — as this function
    originally did — silently shifted every MIN_SECS_LEFT stand-down 5
    minutes later relative to the print, which left the hourly gate (180s)
    unable to fire at all and let daily TTL tails rest through the
    settlement-averaging window."""
    for key in ("close_time", "expected_expiration_time", "latest_expiration_time"):
        dt = mm.parse_iso_utc(market.get(key, ""))
        if dt is not None:
            return dt
    return None


def classify_cadence(window_hours: float) -> str:
    if window_hours <= HOURLY_MAX_HOURS:
        return "hourly"
    if window_hours <= DAILY_MAX_HOURS:
        return "daily"
    if window_hours <= WEEKLY_MAX_HOURS:
        return "weekly"
    return "annual"


@dataclass
class EventView:
    ticker: str
    cadence: str
    markets: List[dict]
    start: datetime
    end: datetime

    @property
    def window_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def secs_left(self, now_utc: datetime) -> float:
        return (self.end - now_utc).total_seconds()


def build_event_views(markets: List[dict], now_utc: datetime) -> List[EventView]:
    """Group open markets into events and classify each event's tenor by its
    own window. Sorted soonest-expiring first."""
    grouped: Dict[str, List[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker")
        if ev:
            grouped.setdefault(ev, []).append(m)
    views = []
    for ev, ms in grouped.items():
        ends = [e for e in (market_end_utc(m) for m in ms) if e is not None]
        opens = [o for o in (mm.parse_iso_utc(m.get("open_time", "")) for m in ms)
                 if o is not None]
        if not ends:
            continue
        end = max(ends)
        start = min(opens) if opens else end - timedelta(hours=1)
        if start >= end:
            start = end - timedelta(hours=1)
        views.append(EventView(ev, classify_cadence((end - start).total_seconds() / 3600.0),
                               ms, start, end))
    views.sort(key=lambda v: (v.end, v.ticker))
    return views


_CADENCE_RANK = {c: i for i, c in enumerate(CADENCES)}


def _collides_with_longer(view: EventView, others: Sequence[EventView]) -> bool:
    """True when a LONGER-tenor event ends on (essentially) the same print as
    this one — same settlement quoted twice; the shorter stands down."""
    return any(_CADENCE_RANK[o.cadence] > _CADENCE_RANK[view.cadence]
               and abs((o.end - view.end).total_seconds()) <= TENOR_COLLISION_SECS
               for o in others)


def select_events(views: Sequence[EventView], cadences: Sequence[str],
                  now_utc: datetime) -> List[EventView]:
    """Events we should be quoting: right tenor, far enough from expiry, and
    not settling on the same print as a longer-tenor event of ours
    (TENOR_COLLISION_SECS).

    The collision check runs against ALL cadence-matched views, not just the
    expiry-filtered ones: when the longer tenor enters its own stand-down
    window (weekly at T-1h) the shared print is at its most dangerous, and a
    same-print shorter event must NOT wake up for the final stretch."""
    candidates = [v for v in views if v.cadence in cadences]
    out = [v for v in candidates
           if v.secs_left(now_utc) >= MIN_SECS_LEFT.get(v.cadence, 900)]
    return [v for v in out if not _collides_with_longer(v, candidates)]


# ---------------------------------------------------------------------------
# The market maker
# ---------------------------------------------------------------------------

@dataclass
class UpDownState:
    events: List[EventView] = field(default_factory=list)
    sigma_cache: Dict[int, Tuple[float, float]] = field(default_factory=dict)  # iv -> (sigma, ts)
    idle_reason: str = ""
    last_summary_events: str = ""
    # Events that left selection and may still carry our resting orders:
    # ticker -> consecutive clean (zero-order) sweeps. Removed at 2 so an
    # order in flight during the first sweep is caught by the second.
    sweep_pending: Dict[str, int] = field(default_factory=dict)
    # Momentum stand-down: which book side is blocked and until when.
    momo_side: Optional[str] = None
    momo_until: float = 0.0
    last_ret_1h: Optional[float] = None


class UpDownMarketMaker(mm.TouchMarketMaker):
    window_label = "SESS"          # no window-to-date breach guard applies here
    model_version = MODEL_VERSION
    client_order_prefix = CLIENT_ORDER_PREFIX
    poll_secs = POLL_SECS
    max_position = MAX_POSITION_CONTRACTS
    max_event = MAX_EVENT_CONTRACTS
    num_levels = NUM_LEVELS
    contracts_per_level = CONTRACTS_PER_LEVEL
    quote_offset_cents = QUOTE_OFFSET_CENTS
    level_spacing_cents = LEVEL_SPACING_CENTS
    status_dir = STATUS_DIR
    # Bound as CLASS attributes (not read as module globals inside the cycle)
    # so a subclass fleet — crypto_annual_mm's — can run different risk and
    # band settings without editing this module's tuning. Same pattern as the
    # ladder-shape attributes above.
    max_asset = MAX_ASSET_CONTRACTS
    max_markets_per_event = MAX_MARKETS_PER_EVENT
    skip_fair_above_cents = SKIP_FAIR_ABOVE_CENTS
    skip_fair_below_cents = SKIP_FAIR_BELOW_CENTS
    max_fair_divergence_cents = MAX_FAIR_DIVERGENCE_CENTS
    idle_poll_secs = IDLE_POLL_SECS
    contracts_per_level_by_cadence = CONTRACTS_PER_LEVEL_BY_CADENCE
    num_levels_by_cadence = NUM_LEVELS_BY_CADENCE
    num_levels_by_asset_cadence = NUM_LEVELS_BY_ASSET_CADENCE
    quote_offset_by_cadence = QUOTE_OFFSET_BY_CADENCE
    quote_offset_by_asset_cadence = QUOTE_OFFSET_BY_ASSET_CADENCE
    skew_by_cadence = SKEW_BY_CADENCE
    skew_edge_floor_cents = SKEW_EDGE_FLOOR_CENTS
    momo_cadences = MOMO_CADENCES
    momo_z = MOMO_Z
    momo_hold_secs = MOMO_HOLD_SECS

    def __init__(self, cfg: mm.MarketConfig, client, live: bool,
                 cadences: Sequence[str] = ("hourly", "daily")):
        super().__init__(cfg, client, live)
        self.cadences = tuple(cadences)
        self.tag = f"[{cfg.asset} ud]"
        self.u = UpDownState()

    # ---- volatility (horizon-matched) --------------------------------------

    def _sigma_at(self, interval_minutes: int, now_ts: float) -> float:
        cached = self.u.sigma_cache.get(interval_minutes)
        if cached and now_ts - cached[1] < VOL_REFRESH_SECS:
            return cached[0]
        candles = mm.fetch_ohlc(self.cfg, interval_minutes)
        sigma = scaled_daily_vol(candles, interval_minutes)
        if not (VOL_MIN <= sigma <= VOL_MAX):
            raise DataError(f"{interval_minutes}m vol {sigma:.4f} outside sanity range")
        self.u.sigma_cache[interval_minutes] = (sigma, now_ts)
        return sigma

    def sigma_for_horizon(self, t_days: float, now_ts: float) -> float:
        """Per-day sigma appropriate to a t_days horizon: the horizon-matched
        sampling frequency blended with the stable daily estimate. A 1-hour
        market priced off 90-day candles ignores the regime it is expiring
        into; one priced purely off 5m bars chases microstructure noise."""
        slow = self._sigma_at(1440, now_ts)
        interval = vol_interval_for_horizon(t_days)
        if interval == 1440:
            return slow
        try:
            fast = self._sigma_at(interval, now_ts)
        except Exception as e:
            log(f"{self.tag} ! {interval}m vol unavailable ({e}); using daily estimate")
            return slow
        return VOL_FAST_WEIGHT * fast + (1.0 - VOL_FAST_WEIGHT) * slow

    def refresh_vol(self, now_ts: float, now_utc: datetime) -> None:
        """Base-class hook: the daily estimate underpins every blend, so make
        sure it exists (and fails the cycle if it can't be built)."""
        sigma = self._sigma_at(1440, now_ts)
        self.state.sigma_daily = sigma
        self.state.vol_fetched_at = now_ts

    # ---- discovery ----------------------------------------------------------

    def fetch_series_markets(self) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _page in range(10):
            resp = self.client.get_markets(series_ticker=self.cfg.series,
                                           status="open", limit=1000, cursor=cursor)
            batch = resp.get("markets") or []
            out.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        return [m for m in out if m.get("status") in ("active", "open")]

    def active_event_tickers(self) -> List[str]:
        return [v.ticker for v in self.u.events]

    def recent_event_tickers(self) -> List[str]:
        try:
            resp = self.client.get_events(series_ticker=self.cfg.series,
                                          limit=max(1, SWEEP_RECENT_EVENTS))
            return [e["event_ticker"] for e in (resp.get("events") or [])
                    if e.get("event_ticker")]
        except Exception as e:
            log(f"{self.tag} ! could not list recent {self.cfg.series} events: {e}")
            return []

    # ---- order plumbing across MULTIPLE concurrent events -------------------

    def fetch_resting_orders(self, now_ts: float) -> List[dict]:
        """Our resting orders across every selected event. The ledger merge runs
        ONCE over the union: _merge_ledger walks the whole ledger, so calling it
        per event would splice other events' in-flight orders into each list."""
        if not self.live:
            return super().fetch_resting_orders(now_ts)
        orders: List[dict] = []
        for ticker in self.active_event_tickers():
            orders.extend(self._get_resting_orders(ticker))
        return self._merge_ledger(orders, now_ts)

    def positions_for(self, event_ticker: str) -> Dict[str, float]:
        try:
            resp = self.client.get_positions(event_ticker=event_ticker, limit=500)
        except Exception as e:
            raise DataError(f"get_positions({event_ticker}) failed: {e}")
        out = {}
        for p in (resp.get("market_positions") or resp.get("positions") or []):
            if p.get("ticker") and p.get("position") is not None:
                out[p["ticker"]] = float(p["position"])
        return out

    def cancel_all_bot_orders(self, include_prev_month: bool = False) -> int:
        """Cancel our resting orders on every selected event — and, when asked
        to sweep wide, on the series' recent events too (tenors roll hourly, so
        a restart strands orders fast)."""
        if not self.live:
            return super().cancel_all_bot_orders(include_prev_month)
        events = list(self.active_event_tickers())
        if include_prev_month:
            for ev in self.recent_event_tickers():
                if ev not in events:
                    events.append(ev)
        n = 0
        for ev in events:
            try:
                orders = self._get_resting_orders(ev)
                orders = self._merge_ledger(orders, time.time())
                for o in orders:
                    if self.cancel_order(o["order_id"]):
                        n += 1
            except Exception as e:
                log(f"{self.tag} ! cancel-all sweep of {ev} failed: {e}")
        return n

    def startup_event_and_sweep(self) -> None:
        self.state.event_ticker = ""
        if self.live:
            n = self.cancel_all_bot_orders(include_prev_month=True)
            log(f"{self.tag} startup: cancelled {n} leftover bot orders")

    def sweep_deselected(self) -> None:
        """Actively cancel resting orders on events that just left selection
        (expiry stand-down, tenor collision, or delisting).

        Without this, a deselected event's orders simply vanish from
        fetch_resting_orders' scope (it only walks selected events) and ride
        the exchange TTL — up to 600s of stale two-sided quotes resting
        exactly where the MIN_SECS_LEFT stand-down was meant to protect us,
        the final minutes before a settlement print. The touch parent cancels
        at its stand-down threshold; this is the multi-event equivalent.

        An event leaves sweep_pending only after TWO consecutive clean
        (zero-order) reads, so an order still in flight during the first
        sweep is caught by the second. Failures retry next cycle."""
        for ev in sorted(self.u.sweep_pending):
            if not self.live:
                gone = [oid for oid, o in self.state.sim_orders.items()
                        if str(o.get("ticker", "")).startswith(ev + "-")]
                for oid in gone:
                    del self.state.sim_orders[oid]
                if gone:
                    log(f"{self.tag} deselected {ev}: dropped {len(gone)} sim orders")
                self.u.sweep_pending.pop(ev, None)
                continue
            try:
                orders = self._get_resting_orders(ev)
            except Exception as e:
                log(f"{self.tag} ! deselect sweep of {ev} failed ({e}); "
                    f"retrying next cycle")
                self.u.sweep_pending[ev] = 0
                continue
            if not orders:
                self.u.sweep_pending[ev] += 1
                if self.u.sweep_pending[ev] >= 2:
                    self.u.sweep_pending.pop(ev, None)
                continue
            self.u.sweep_pending[ev] = 0
            cancelled = failed = 0
            for o in orders:
                if self.cancel_order(o["order_id"]):
                    self.state.cancelled_today += 1
                    cancelled += 1
                else:
                    failed += 1
            log(f"{self.tag} deselected {ev}: cancelled {cancelled} resting orders"
                + (f" ({failed} failed; retrying next cycle)" if failed else ""))

    def poll_interval(self) -> float:
        return self.idle_poll_secs if self.u.idle_reason else self.poll_secs

    def status_extra(self) -> Dict[str, object]:
        return {
            "fleet": "updown",
            "series": self.cfg.series,
            "cadences": ",".join(self.cadences),
            "idle_reason": self.u.idle_reason,
            "momo_blocked": self.u.momo_side or "",
            "last_ret_1h": self.u.last_ret_1h,
            "events": [{"ticker": v.ticker, "cadence": v.cadence,
                        "end": v.end.strftime("%Y-%m-%dT%H:%M:%SZ")}
                       for v in self.u.events],
        }

    # ---- momentum stand-down -----------------------------------------------

    def price_one_hour_ago(self, now_ts: float) -> Optional[float]:
        """Close of the latest 5m candle that CLOSED at least 1h ago.

        Kraken stamps candles with their OPEN time (the ts+interval
        convention candles_extreme also relies on). An hourly-candle version
        of this filter therefore anchored the reference to the top of the
        CURRENT hour — measuring a 0-60min return that left the guard blind
        right after every hour boundary and ~sqrt(2) under-sensitive on
        average (caught in adversarial review, 2026-08-17). 5m bars put the
        reference 60-65 minutes back at any wall time; the shared fleet
        cache makes this a file read most cycles."""
        candles = mm.fetch_ohlc(self.cfg, 5)
        past = [c for c in candles if c[0] + 300 <= now_ts - 3600]
        return past[-1][1] if past else None

    def refresh_momentum_block(self, spot: float, now_ts: float) -> Optional[str]:
        """Evaluate the 1h-momentum trigger and return the blocked book side
        (with MOMO_HOLD_SECS of hysteresis past the last trigger), or None.
        Failures leave the guard inactive for the cycle — it is a defense,
        not a dependency."""
        try:
            ref = self.price_one_hour_ago(now_ts)
            if ref and ref > 0 and spot > 0:
                ret_1h = math.log(spot / ref)
                self.u.last_ret_1h = ret_1h
                if abs(ret_1h) > 1.0:
                    # |1h return| > 172% is not a market move, it is a data
                    # inconsistency (e.g. candle source vs spot source after
                    # a fallback, or a bad candle) — ln(10) unit slips land
                    # here. A guard must never act on broken data.
                    log(f"{self.tag} ! momentum ref implausible "
                        f"(ret_1h={ret_1h:+.2f}); guard inactive this cycle")
                    side = None
                else:
                    side = momentum_block_side(ret_1h, self.state.sigma_daily,
                                               self.momo_z)
                if side:
                    if side != self.u.momo_side:
                        log(f"{self.tag} momentum stand-down: 1h ret "
                            f"{ret_1h:+.4f} > {self.momo_z:g} sigma_1h; "
                            f"blocking {side}s on {'/'.join(self.momo_cadences)}")
                        self.alerter.alert(
                            "momentum", f"1h ret {ret_1h:+.4f}; blocking {side}s",
                            key=side, urgent=False)
                    self.u.momo_side = side
                    self.u.momo_until = now_ts + self.momo_hold_secs
        except Exception as e:
            log(f"{self.tag} ! momentum check failed ({e}); guard inactive this cycle")
        if self.u.momo_side and now_ts < self.u.momo_until:
            return self.u.momo_side
        self.u.momo_side = None
        return None

    # ---- one cycle ----------------------------------------------------------

    def quotable_markets(self, view: EventView, spot: float, now_utc: datetime,
                         now_ts: float) -> List[Tuple[int, str, float, int]]:
        """(moneyness, ticker, strike, fair) for the near-money band of one
        event, nearest-to-50c first, capped at MAX_MARKETS_PER_EVENT.

        The band is what makes this tractable: an hourly event lists 188
        strikes and we need one orderbook call per market we intend to quote."""
        priced = []
        for m in view.markets:
            strike = updown_strike(m)
            if strike is None:
                continue
            end = market_end_utc(m) or view.end
            t_days = max(0.0, (end - now_utc).total_seconds() / 86400.0)
            if t_days <= 0:
                continue
            sigma = self.sigma_for_horizon(t_days, now_ts)
            fair = fair_value_cents(spot, strike, sigma, t_days)
            if fair >= self.skip_fair_above_cents or fair <= self.skip_fair_below_cents:
                continue
            priced.append((abs(fair - 50), m["ticker"], strike, fair))
        priced.sort()
        return priced[:self.max_markets_per_event]

    def run_cycle(self) -> None:
        now_utc = datetime.now(timezone.utc)
        now_ts = now_utc.timestamp()

        views = build_event_views(self.fetch_series_markets(), now_utc)
        selected = select_events(views, self.cadences, now_utc)
        dropped = [v for v in views if v.cadence in self.cadences and v not in selected]
        selected_tickers = {v.ticker for v in selected}
        for ev in {v.ticker for v in self.u.events} - selected_tickers:
            self.u.sweep_pending.setdefault(ev, 0)
        for ev in selected_tickers:
            self.u.sweep_pending.pop(ev, None)
        self.u.events = selected
        self.sweep_deselected()   # before the idle return: a lone dying event
                                  # still needs its book cleared
        if not selected:
            self.u.idle_reason = (
                f"no open {'/'.join(self.cadences)} event on {self.cfg.series}"
                + (f" ({len(dropped)} too close to expiry or on a shared print)"
                   if dropped else ""))
            self.state.event_ticker = ""
            self.state.cycles_today += 1
            self.state.last_markets_line = f"idle: {self.u.idle_reason}"
            log(f"{self.tag} {self.u.idle_reason}; idle "
                f"(next check in {self.idle_poll_secs:.0f}s)")
            return
        self.u.idle_reason = ""
        self.state.event_ticker = selected[0].ticker   # nearest, for the heartbeat

        spot = mm.fetch_live_price(self.cfg, self.state.live_price)
        self.state.live_price = spot
        self.refresh_vol(now_ts, now_utc)

        momo_block = None
        if any(v.cadence in self.momo_cadences for v in selected):
            momo_block = self.refresh_momentum_block(spot, now_ts)

        resting = self.fetch_resting_orders(now_ts)
        own_by_ticker: Dict[str, List[Tuple[str, int, float]]] = {}
        for o in resting:
            parsed = mm.order_yes_book_cents(o)
            if parsed is not None:
                own_by_ticker.setdefault(o.get("ticker", ""), []).append(
                    (parsed[0], parsed[1], mm.order_remaining(o)))

        asset_net = 0.0
        per_event_positions = {}
        for v in selected:
            pos = self.positions_for(v.ticker)   # DataError -> fail-safe
            per_event_positions[v.ticker] = pos
            asset_net += sum(pos.values())
        self.state.last_event_net = asset_net
        asset_room_buy = self.max_asset - asset_net
        asset_room_sell = self.max_asset + asset_net

        summary = [f"{self.tag} {self.cfg.asset}=${spot:,.2f} "
                   f"sigma_d={self.state.sigma_daily:.4f} net={asset_net:+.0f} "
                   f"events={len(selected)}"
                   + (f" MOMO-BLOCK:{momo_block}s" if momo_block else "")]
        desired: List[mm.Quote] = []
        blind = set()

        events_left = len(selected)
        for v in selected:
            positions = per_event_positions[v.ticker]
            event_net = sum(positions.values())
            # Even share of the REMAINING asset budget. Draining it greedily in
            # expiry order let the hourly and daily tenors consume all 400
            # contracts and left the weekly with room_sell=0 — it was priced
            # every cycle and quoted on neither side. Unused share rolls
            # forward, so a starved-out tenor still gets what the others skip.
            event_share_buy = asset_room_buy / events_left if events_left else 0.0
            event_share_sell = asset_room_sell / events_left if events_left else 0.0
            events_left -= 1
            event_room_buy = min(self.max_event - event_net, event_share_buy)
            event_room_sell = min(self.max_event + event_net, event_share_sell)
            cpl = self.contracts_per_level_by_cadence.get(
                v.cadence, self.contracts_per_level)
            n_levels = self.num_levels_by_asset_cadence.get(
                (self.cfg.asset, v.cadence))
            if n_levels is None:
                n_levels = self.num_levels_by_cadence.get(
                    v.cadence, self.num_levels)
            offset = self.quote_offset_by_asset_cadence.get(
                (self.cfg.asset, v.cadence))
            if offset is None:
                offset = self.quote_offset_by_cadence.get(
                    v.cadence, self.quote_offset_cents)
            skew_cfg = self.skew_by_cadence.get(v.cadence)
            skew = (inventory_skew_cents(event_net, *skew_cfg)
                    if skew_cfg else 0.0)
            if skew:
                # The thinned side's edge never drops below the floor — see
                # SKEW_EDGE_FLOOR_CENTS (account-level net can be someone
                # else's position).
                max_shift = max(0.0, offset - self.skew_edge_floor_cents)
                skew = max(-max_shift, min(max_shift, skew))
                if abs(event_net) >= skew_cfg[1]:
                    self.alerter.alert(
                        "skew_saturated",
                        f"{v.ticker} net {event_net:+.0f} saturates the "
                        f"inventory skew (account-level: may include non-bot "
                        f"positions)", key=v.ticker, urgent=False)
            band = self.quotable_markets(v, spot, now_utc, now_ts)
            summary.append(
                f"  {v.ticker} [{v.cadence}] {v.secs_left(now_utc) / 3600:.2f}h left, "
                f"{len(v.markets)} strikes -> {len(band)} quotable, net={event_net:+.0f}"
                + (f", skew={skew:+.1f}c" if skew else ""))
            remaining = len(band)
            for _moneyness, ticker, strike, fair in band:
                share_buy = event_room_buy / remaining if remaining > 0 else 0.0
                share_sell = event_room_sell / remaining if remaining > 0 else 0.0
                remaining -= 1
                try:
                    yes_levels, no_levels = mm.orderbook_levels(
                        self.client.get_orderbook(ticker=ticker))
                    best_bid, best_ask = mm.external_best(
                        yes_levels, no_levels, own_by_ticker.get(ticker, []))
                    self.state.blind_streak.pop(ticker, None)
                except Exception as e:
                    streak = self.state.blind_streak.get(ticker, 0) + 1
                    self.state.blind_streak[ticker] = streak
                    if streak <= mm.BLIND_PRESERVE_CYCLES:
                        blind.add(ticker)
                        log(f"{self.tag} ! orderbook fetch failed for {ticker} ({e}); "
                            f"blind cycle {streak}/{mm.BLIND_PRESERVE_CYCLES}, preserving")
                    else:
                        log(f"{self.tag} ! orderbook blind on {ticker} for {streak} "
                            f"cycles; cancelling its quotes")
                    continue

                if best_bid is not None and best_ask is not None:
                    mid = (best_bid + best_ask) / 2.0
                    if abs(fair - mid) > self.max_fair_divergence_cents:
                        summary.append(
                            f"    {ticker} strike={strike:g}: fair={fair}c vs mid "
                            f"{mid:.0f}c — disagree by >{self.max_fair_divergence_cents}c "
                            f"-> not quoting")
                        self.alerter.alert("model_divergence",
                                           f"{ticker} fair {fair}c vs mid {mid:.0f}c; "
                                           f"standing down", key=ticker, urgent=False)
                        continue

                pos = positions.get(ticker, 0.0)
                room_buy = min(self.max_position - pos, share_buy)
                room_sell = min(self.max_position + pos, share_sell)
                # Inventory skew moves only the QUOTING fair — band selection
                # and the divergence guard above judged the model fair.
                q_fair = int(round(max(1, min(99, fair + skew))))
                quotes = mm.build_quotes(ticker, q_fair, best_bid, best_ask,
                                         room_buy, room_sell,
                                         n_levels, cpl,
                                         offset, self.level_spacing_cents)
                if momo_block and v.cadence in self.momo_cadences:
                    quotes = [q for q in quotes if q.book_side != momo_block]
                desired.extend(quotes)
                bought = sum(q.count for q in quotes if q.book_side == "bid")
                sold = sum(q.count for q in quotes if q.book_side == "ask")
                event_room_buy -= bought
                event_room_sell -= sold
                asset_room_buy -= bought
                asset_room_sell -= sold

                summary.append(
                    f"    {ticker} K={strike:g} fair={fair}c ext "
                    f"{best_bid if best_bid is not None else '--'}/"
                    f"{best_ask if best_ask is not None else '--'} pos={pos:+.0f} "
                    f"bids={sorted((q.price_cents for q in quotes if q.book_side == 'bid'), reverse=True)} "
                    f"asks={sorted(q.price_cents for q in quotes if q.book_side == 'ask')}")

        to_place, to_cancel = mm.diff_orders(desired, resting, self.state.order_ages,
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
            raise DataError(f"{cancel_failures} cancel(s) failed")
        self.state.placed_today += self.place_with_side_cap(
            to_place, resting, cancelled_ids, now_ts)
        self.state.cycles_today += 1
        self.u.last_summary_events = ", ".join(
            f"{v.cadence}:{v.ticker.rsplit('-', 1)[-1]}" for v in selected)
        self.state.last_markets_line = (
            f"{len({q.ticker for q in desired})} mkts quoted "
            f"({len(desired)} quotes) across {len(selected)} events "
            f"[{self.u.last_summary_events}]")


# ---------------------------------------------------------------------------
# --discover
# ---------------------------------------------------------------------------

def discover_report(client, now_utc: Optional[datetime] = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    lines = ["Live Kalshi crypto ABOVE/BELOW events (KX<ASSET>D):",
             f"  {'asset':6s} {'event':22s} {'tenor':7s} {'window':>8s} "
             f"{'left':>8s} {'strikes':>8s}"]
    total = 0
    for asset in sorted(ASSETS):
        cfg = ASSETS[asset]
        try:
            resp = client.get_markets(series_ticker=cfg.series, status="open", limit=1000)
            markets = [m for m in (resp.get("markets") or [])
                       if m.get("status") in ("active", "open")]
        except Exception as e:
            lines.append(f"  {asset:6s} ERROR {e}")
            continue
        views = build_event_views(markets, now_utc)
        if not views:
            lines.append(f"  {asset:6s} (no open events)")
            continue
        for v in views:
            total += 1
            lines.append(f"  {asset:6s} {v.ticker:22s} {v.cadence:7s} "
                         f"{v.window_hours:7.1f}h {v.secs_left(now_utc) / 3600:7.2f}h "
                         f"{len(v.markets):8d}")
    lines.append(f"  -> {total} open events across {len(ASSETS)} configured assets")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="BTC", choices=sorted(ASSETS),
                    help="which crypto above/below series to make markets on")
    ap.add_argument("--cadence", default="both",
                    help="tenors to quote: hourly, daily, weekly (comma list), "
                         "'both' (hourly+daily, default) or 'all'")
    ap.add_argument("--live", action="store_true",
                    help="actually place/cancel orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--discover", action="store_true",
                    help="list every open above/below event per asset, then exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel this bot's REAL resting orders on the series' open and "
                         "recent events, then exit (always live)")
    args = ap.parse_args(argv)

    try:
        cadences = parse_cadences(args.cadence)
    except ValueError as e:
        ap.error(str(e))

    cfg = ASSETS[args.asset]
    client = mm.build_client()

    if args.discover:
        print(discover_report(client))
        return 0

    if args.cancel_all:
        bot = UpDownMarketMaker(cfg, client, live=True, cadences=CADENCES)
        try:   # populate the open-event set so the sweep covers it
            bot.u.events = build_event_views(bot.fetch_series_markets(),
                                             datetime.now(timezone.utc))
        except Exception as e:
            log(f"! discovery failed ({e}); sweeping recent events only")
        n = bot.cancel_all_bot_orders(include_prev_month=True)
        log(f"cancelled {n} real resting orders for {cfg.asset} above/below")
        return 0

    # One live looping bot per asset (same duplicate-money risk as the touch
    # fleet; the 2026-08-09 incident minted a second updown generation).
    # --once and dry runs are exempt, like the monthly bot's.
    if args.live and not args.once and not mm.acquire_singleton(
            cfg.asset, status_dir=UpDownMarketMaker.status_dir,
            heartbeat_max_age_secs=3 * IDLE_POLL_SECS):
        return 1

    bot = UpDownMarketMaker(cfg, client, live=args.live, cadences=cadences)
    overridden = (set(CONTRACTS_PER_LEVEL_BY_CADENCE) | set(NUM_LEVELS_BY_CADENCE)
                  | set(QUOTE_OFFSET_BY_CADENCE))
    per_tenor = "".join(
        f" ({c} {NUM_LEVELS_BY_CADENCE.get(c, NUM_LEVELS)}x"
        f"{CONTRACTS_PER_LEVEL_BY_CADENCE.get(c, CONTRACTS_PER_LEVEL)}"
        f" @{QUOTE_OFFSET_BY_CADENCE.get(c, QUOTE_OFFSET_CENTS)}c)"
        for c in sorted(overridden) if c in cadences)
    log(f"=== {MODEL_VERSION} asset={cfg.asset} series={cfg.series} "
        f"cadence={','.join(cadences)} prefix={CLIENT_ORDER_PREFIX}- "
        f"ladder {NUM_LEVELS}x{CONTRACTS_PER_LEVEL}{per_tenor} @{QUOTE_OFFSET_CENTS}c off fair "
        f"{LEVEL_SPACING_CENTS}c apart | band {SKIP_FAIR_BELOW_CENTS}-"
        f"{SKIP_FAIR_ABOVE_CENTS}c, <={MAX_MARKETS_PER_EVENT} mkts/event | "
        f"caps {MAX_POSITION_CONTRACTS:g}/{MAX_EVENT_CONTRACTS:g}/"
        f"{MAX_ASSET_CONTRACTS:g} (mkt/event/asset) ===")
    defenses = []
    for c, (mx, full) in sorted(SKEW_BY_CADENCE.items()):
        if c in cadences:
            defenses.append(f"{c} skew +/-{mx:g}c full@{full:g}cts")
    if any(c in cadences for c in MOMO_CADENCES):
        defenses.append(f"momo z={MOMO_Z:g} hold {MOMO_HOLD_SECS:g}s "
                        f"on {'/'.join(MOMO_CADENCES)}")
    for (a, c) in sorted(set(QUOTE_OFFSET_BY_ASSET_CADENCE)
                         | set(NUM_LEVELS_BY_ASSET_CADENCE)):
        if a != cfg.asset or c not in cadences:
            continue
        parts = []
        off = QUOTE_OFFSET_BY_ASSET_CADENCE.get((a, c))
        if off is not None:
            parts.append(f"offset {off}c")
        n = NUM_LEVELS_BY_ASSET_CADENCE.get((a, c))
        if n is not None:
            parts.append(f"{n} rung{'s' if n != 1 else ''}")
        defenses.append(f"{a} {c} {', '.join(parts)}")
    if defenses:
        log(f"=== defenses: {'; '.join(defenses)} ===")
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
