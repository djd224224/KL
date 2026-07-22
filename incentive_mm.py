#!/usr/bin/env python3
"""
incentive_mm.py — Conservative market maker for Kalshi LIQUIDITY INCENTIVE markets.

Discovers every active liquidity incentive program via
GET /trade-api/v2/incentive_programs (period_reward is in CENTI-CENTS:
1,000,000 = $100), ranks markets by reward dollars per day, and rests
two-sided post-only ladders on the best-paying ones, subject to a collateral
budget and a stack of adverse-selection guards.

Reward mechanics (Kalshi Volume & Liquidity Incentive Program, CFTC filing
Aug 2025): once per second a random-time book snapshot is scored. On each
side (YES bids and NO bids — a YES ask IS a NO bid), levels are walked from
the best price until cumulative size reaches the program's target size
(usually 1000); if the side's whole book is thinner than target, NOBODY
scores that side. Each qualifying order scores
    DiscountFactor^(ticks behind best) x size          (factor 0.5 today)
and the period reward is split pro-rata by total score share. Therefore:
join the best (never improve it) with a small lot and stack more size 1-2
ticks behind — exactly a conservative ladder.

Quoting per selected market (post-only, YES-book cents):
  per side: 5 contracts AT the external best (join, never lead, never alone),
  10 at 1c behind, 20 at 2c behind. A side with no external quotes gets
  nothing. Net position per market capped at +/-100 (position + full ladder),
  +/-500 net across all markets of one event, and a global collateral budget
  (default $1,000) caps how many markets are quoted at once.

Guards (all deliberate, see INCENTIVE_MM_HANDOFF.md):
  - EVENT-START CUTOFF: markets whose event ticker embeds a date
    (e.g. KXWCMENTION-26JUL11ARGSUI) are abandoned at 00:00 ET on that date;
    markets with an occurrence_datetime earlier than expiration stop there.
    Live-broadcast "mention" markets are therefore traded pre-event only.
  - MID-MOVE BREAKER: mid moved >15c between cycles -> stand down 30 min.
  - FILL-BURST BREAKER: position moved >=15 contracts in one cycle ->
    cancel both sides, stand down 60 min, urgent alert (insider sweep).
  - Price band 3..97c, mid band 5..95c, max join spread 25c, min volume.
  - Inventory skew: |pos|>=30 halves the accumulating side, >=60 pulls it.
  - Daily realized-loss halt (default -$50) cancels everything until the
    next ET day. A HALT file in the status dir does the same on demand.
  - Fail-safe: 4 consecutive errored cycles cancel every resting order.
    TTL (600s) bounds orphan risk if the process dies uncleanly.

SAFETY: DRY RUN by default — reads are live, writes are simulated. Pass
--live to place real orders. --cancel-all always operates on the real book.

Usage:
    python incentive_mm.py                 # dry run, loop
    python incentive_mm.py --status        # print current market selection, exit
    python incentive_mm.py --once          # single dry-run cycle
    python incentive_mm.py --live          # real orders
    python incentive_mm.py --cancel-all    # cancel every resting imm- order
"""

import argparse
import atexit
import base64
import json
import math
import os
import re
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

MODEL_VERSION = "incentive_mm_v1.2"
RUN_ID = uuid.uuid4().hex[:8]
CLIENT_ORDER_PREFIX = "imm"   # client_order_ids: imm-<run>-<hex>

# ----------------------------------------------------------------------------
# Configuration (env-overridable, prefix IMM_)
# ----------------------------------------------------------------------------

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")

ET = pytz.timezone("US/Eastern")
CT = pytz.timezone("US/Central")


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# Ladder: (ticks behind the join anchor, contracts). Reward credit halves per
# tick behind the best, so the at-best lot is small (pickoff exposure) and the
# deeper lots carry the size.
def _parse_levels(spec: str) -> List[Tuple[int, int]]:
    out = []
    for part in spec.split(","):
        ticks, size = part.split(":")
        out.append((int(ticks), int(size)))
    if not out or any(t < 0 or s <= 0 for t, s in out):
        raise ValueError(f"bad IMM_LEVELS spec: {spec!r}")
    return out


LEVELS = _parse_levels(os.environ.get("IMM_LEVELS", "0:5,1:10,2:20"))
SIDE_MAX_CONTRACTS = sum(s for _t, s in LEVELS)      # 35 with the default spec


@dataclass(frozen=True)
class SeriesOverride:
    """Per-series overrides of the global quoting spec (user-directed)."""
    levels: Optional[List[Tuple[int, int]]] = None      # ladder shape
    max_position: Optional[float] = None                # net cap per market
    quote_all: bool = False                             # quote EVERY market of the
    #   event, exempt from yield ranking / MAX_MARKETS / collateral budget
    hard_expiry_et: Optional[Tuple[int, int]] = None    # (hour, minute) ET on the
    #   event date — a floor on the cutoff so nothing rests past it
    start_buffer_min: Optional[int] = None              # override the global
    #   EVENT_START_BUFFER_MIN for this series (0 = quote right up to the start)
    pad_to_target: bool = False                         # when a quoted side's total
    #   depth is below the reward target size, add throwaway contracts at the 1c
    #   mark (bid) / 99c mark (ask) to reach it, so the near-touch ladder qualifies
    cutoff_from_close_min: Optional[int] = None         # cutoff = close_time minus
    #   this many minutes, REPLACING the ticker-date/occurrence rules — for series
    #   whose whole life IS the event (hourly weather markets) and which the
    #   midnight-ET rule would otherwise kill on sight
    min_hours_to_close: Optional[float] = None          # override the global
    #   MIN_HOURS_TO_CLOSE screen (hourly markets live less than the 1h default)
    pre_cutoff_reduce_only_secs: Optional[int] = None   # override the global
    #   PRE_CUTOFF_REDUCE_ONLY_SECS (1h) — hourly markets are BORN closer to
    #   their cutoff than that, so the default makes them reduce-only for life
    price_min_cents: Optional[int] = None               # per-series order price
    price_max_cents: Optional[int] = None               #   band (else global)


# Depth-padding (pad_to_target): fill a thin side up to the reward target with
# cheap far-from-touch contracts, rounded up to the nearest PAD_ROUND. 1c bids /
# 99c asks cost ~1c collateral and ~1c max loss each; they earn ~0 (weight
# 0.5^(~47 ticks) ≈ 0) but unlock the near-touch ladder's rewards.
PAD_BID_CENTS = _env_int("IMM_PAD_BID_CENTS", 1)
PAD_ASK_CENTS = _env_int("IMM_PAD_ASK_CENTS", 99)
PAD_ROUND = _env_int("IMM_PAD_ROUND", 100)
PAD_MAX_CONTRACTS = _env_int("IMM_PAD_MAX", 5000)   # safety ceiling per side
PAD_TO_TARGET_GLOBAL = os.environ.get("IMM_PAD_TO_TARGET", "0") == "1"


# User decision 2026-07-12: Love Island mention pools are high incentive-per-minute
# (live only ~1 day), so go bigger and quote the whole event, but with a hard 8:30pm
# ET episode expiry and a tighter 50-contract per-market net cap.
SERIES_OVERRIDES: Dict[str, SeriesOverride] = {
    "KXLOVEISLMENTION": SeriesOverride(
        levels=_parse_levels(os.environ.get("IMM_LOVEISL_LEVELS", "0:5,1:5,2:5")),
        max_position=_env_float("IMM_LOVEISL_MAX_POSITION", 50),
        quote_all=True,
        hard_expiry_et=(21, 0),    # 9:00pm ET (episode start; user: quote until 9p)
        start_buffer_min=0,        # no pre-broadcast buffer — quote right up to 9pm
        pad_to_target=True,
    ),
}

# Hourly temperature markets (user decision 2026-07-14): 5 cities x 10 strikes,
# the richest pools in the feed (~$100/market-hour). No pre-event window exists —
# the observation hour IS the market's life (opens 1h before close, settles ~5min
# after) — so the midnight-ET ticker-date rule and the 1h closing screen are
# replaced with: quote until IMM_TEMP_CUTOFF_FROM_CLOSE_MIN before close. The
# final minutes are the most informed (live METAR watchers), hence the buffer;
# mid-move/fill-burst breakers and inventory skew are the rest of the protection.
# Only mid-band strikes ever quote (far strikes are one_sided/extreme_mid).
# Temp tuning (Jack 2026-07-21): tighter, earlier-exiting, shallower —
# 5/2/2 ladders (9/side vs global 15), net cap 50/market (global was 100),
# quotes only in the 5..90c band (no 1c-scrap quoting on pinned strikes),
# out 15 min before the reading (was 5; reduce-only starts 5 min before that).
for _s in os.environ.get(
        "IMM_TEMP_SERIES",
        "KXTEMPAUSH,KXTEMPCHIH,KXTEMPDCH,KXTEMPLAXH,KXTEMPNYCH").split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            levels=_parse_levels(os.environ.get("IMM_TEMP_LEVELS", "0:5,1:2,2:2")),
            max_position=_env_float("IMM_TEMP_MAX_POSITION", 50),
            price_min_cents=_env_int("IMM_TEMP_PRICE_MIN", 5),
            price_max_cents=_env_int("IMM_TEMP_PRICE_MAX", 90),
            cutoff_from_close_min=_env_int("IMM_TEMP_CUTOFF_FROM_CLOSE_MIN", 15),
            min_hours_to_close=_env_float("IMM_TEMP_MIN_HOURS_TO_CLOSE", 0.05),
            pre_cutoff_reduce_only_secs=_env_int("IMM_TEMP_PRE_CUTOFF_RO", 300))

# Air-quality-index markets (user decision 2026-07-15). Series is KXAQICITY; the
# CITY lives in the event segment (KXAQICITY-NYC26JUL19), so one override covers
# every city. Each market is a POINT AQI reading at a fixed time ("AQI above 130
# in NYC Jul 19 3pm ET"); it opens ~2 days early and close_time IS the reading
# time. The city-prefixed ticker date is unparseable, so anchor the cutoff to
# close_time like weather — but with a bigger pre-reading buffer (30 min) since a
# point reading is highly autocorrelated in its final minutes. Multi-day market,
# so the default 1h reduce-only + 1h closing screen stand (no override needed).
for _s in os.environ.get("IMM_AQI_SERIES", "KXAQICITY").split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            cutoff_from_close_min=_env_int("IMM_AQI_CUTOFF_FROM_CLOSE_MIN", 30))


def series_pad_to_target(series: str) -> bool:
    if PAD_TO_TARGET_GLOBAL:
        return True
    ov = SERIES_OVERRIDES.get(series)
    return bool(ov and ov.pad_to_target)


def pad_quantity(external_and_touch_depth: float, target: float) -> int:
    """Contracts to add at the pad price so the side's total depth reaches the
    reward target, rounded UP to the nearest PAD_ROUND. 0 if already at target."""
    gap = target - external_and_touch_depth
    if gap <= 0:
        return 0
    n = int(math.ceil(gap / PAD_ROUND) * PAD_ROUND)
    return min(n, PAD_MAX_CONTRACTS)


def series_override(series: str) -> Optional[SeriesOverride]:
    return SERIES_OVERRIDES.get(series)


def series_levels(series: str) -> List[Tuple[int, int]]:
    ov = SERIES_OVERRIDES.get(series)
    return ov.levels if (ov and ov.levels) else LEVELS


def series_side_max(series: str) -> int:
    return sum(s for _t, s in series_levels(series))


def series_max_position(series: str) -> float:
    ov = SERIES_OVERRIDES.get(series)
    return ov.max_position if (ov and ov.max_position is not None) \
        else MAX_POSITION_CONTRACTS


def series_price_min(series: str) -> int:
    ov = SERIES_OVERRIDES.get(series)
    return ov.price_min_cents if (ov and ov.price_min_cents is not None) \
        else PRICE_MIN_CENTS


def series_price_max(series: str) -> int:
    ov = SERIES_OVERRIDES.get(series)
    return ov.price_max_cents if (ov and ov.price_max_cents is not None) \
        else PRICE_MAX_CENTS


def series_min_hours_to_close(series: str) -> float:
    ov = SERIES_OVERRIDES.get(series)
    return ov.min_hours_to_close if (ov and ov.min_hours_to_close is not None) \
        else MIN_HOURS_TO_CLOSE


def series_pre_cutoff_reduce_only_secs(series: str) -> int:
    ov = SERIES_OVERRIDES.get(series)
    return ov.pre_cutoff_reduce_only_secs \
        if (ov and ov.pre_cutoff_reduce_only_secs is not None) \
        else PRE_CUTOFF_REDUCE_ONLY_SECS


def series_of(ticker: str) -> str:
    return ticker.split("-")[0]


MAX_POSITION_CONTRACTS = _env_float("IMM_MAX_POSITION", 100)   # per market (user spec)
MAX_EVENT_CONTRACTS = _env_float("IMM_MAX_EVENT", 500)         # net per event (user spec)
COLLATERAL_BUDGET = _env_float("IMM_COLLATERAL_BUDGET", 1000.0)  # $ resting + inventory
# Selection reserves worst-case (full two-sided ladder at the touch) collateral
# per market, but skew / one-sided books / churn / partial fills mean only a
# fraction actually rests (observed ~0.62 with two rich earnings events live),
# so worst-case reservation under-funds the budget and skips markets that would
# comfortably fit. Reserve REALIZATION x worst-case instead (user 2026-07-15),
# so the budget funds ~1/REALIZATION more markets and actual deployment tracks
# the cap. Kept slightly above the observed ratio for margin; the account
# balance (not this soft cap) is the real backstop if realization spikes.
COLLATERAL_REALIZATION = _env_float("IMM_COLLATERAL_REALIZATION", 0.65)
MAX_MARKETS = _env_int("IMM_MAX_MARKETS", 35)   # max distinct EVENTS quoted at
#   once; <=0 = UNLIMITED (collateral budget becomes the sole breadth governor,
#   user decision 2026-07-14). All markets within an opened event are eligible
#   regardless — budget-bounded.

POLL_SECS = _env_int("IMM_POLL_SECS", 90)
UNIVERSE_REFRESH_SECS = _env_int("IMM_UNIVERSE_REFRESH_SECS", 600)
# Kalshi publishes each hour's hourly-series (KXTEMP) programs LATE — absent
# when the hour-crossed refresh fires at ~hh:01, present by ~hh:11 (observed
# at the 01:00Z and 02:00Z boundaries, 2026-07-21) — so for the first N
# seconds of each hour the refresh gate drops to per-cycle: pickup lands
# within ~2 min of whenever Kalshi actually publishes, no race, no assumption
# about their timing. A refresh costs a few seconds under keep-alive, so the
# worst case is ~7 cheap extra refreshes/hour.
HOURLY_ACTIVATION_WINDOW_SECS = _env_int("IMM_HOURLY_ACTIVATION_WINDOW", 720)
ORDER_TTL_SECS = _env_int("IMM_ORDER_TTL_SECS", 600)
ORDER_REFRESH_SECS = _env_int("IMM_ORDER_REFRESH_SECS", 420)

PRICE_MIN_CENTS = _env_int("IMM_PRICE_MIN_CENTS", 1)    # never quote below (bid side)
PRICE_MAX_CENTS = _env_int("IMM_PRICE_MAX_CENTS", 95)   # never quote above (ask side)
MID_BAND_LO = _env_int("IMM_MID_BAND_LO", 1)            # skip markets with mid outside
MID_BAND_HI = _env_int("IMM_MID_BAND_HI", 95)
MAX_JOIN_SPREAD_CENTS = _env_int("IMM_MAX_JOIN_SPREAD", 25)
MIN_VOLUME_CONTRACTS = _env_float("IMM_MIN_VOLUME", 25)   # lifetime volume: some price discovery
MIN_HOURS_TO_CLOSE = _env_float("IMM_MIN_HOURS_TO_CLOSE", 1.0)

MID_MOVE_BREAKER_CENTS = _env_int("IMM_MID_MOVE_BREAKER", 15)
BREAKER_COOLDOWN_SECS = _env_int("IMM_BREAKER_COOLDOWN", 1800)
FILL_BURST_CONTRACTS = _env_float("IMM_FILL_BURST", 15)
FILL_BURST_COOLDOWN_SECS = _env_int("IMM_FILL_BURST_COOLDOWN", 3600)
# Stand-down breakers REMOVED (Jack 2026-07-21): on hourly temp markets the
# mid-move breaker fires AFTER the move (damage done) and its 30-min cooldown
# forfeits the rest of the hour's rent, every hour. IMM_BREAKERS=1 restores
# all three (mid-move, fill-burst, one-sided-transition). Still active with
# breakers off: skew caps, per-market/event position caps, reduce-only
# windows, crossed/wide-book cancels, the daily loss halt, and marks/MTM.
BREAKERS_ENABLED = os.environ.get("IMM_BREAKERS", "0") == "1"
SKEW_SOFT_CONTRACTS = _env_float("IMM_SKEW_SOFT", 30)   # halve accumulating side
SKEW_HARD_CONTRACTS = _env_float("IMM_SKEW_HARD", 60)   # pull accumulating side
REDUCE_ONLY_MIN_CONTRACTS = _env_float("IMM_REDUCE_ONLY_MIN", 5)
PRE_CUTOFF_REDUCE_ONLY_SECS = _env_int("IMM_PRE_CUTOFF_REDUCE_ONLY", 3600)

DAILY_LOSS_LIMIT = _env_float("IMM_DAILY_LOSS_LIMIT", 1200.0)  # realized+unrealized $, halts to next ET day (user raises: 50->150 7/14; ->500 7/19; ->800 7/20; ->1200 7/21)
MAX_TOTAL_RESTING_ORDERS = _env_int("IMM_MAX_TOTAL_RESTING", 450)
MAX_PLACEMENTS_PER_CYCLE = _env_int("IMM_MAX_PLACEMENTS_PER_CYCLE", 120)
QUALIFY_PATIENCE_CYCLES = _env_int("IMM_QUALIFY_PATIENCE", 30)  # bench zero-reward markets
BENCH_COOLDOWN_SECS = _env_int("IMM_BENCH_COOLDOWN", 4 * 3600)
FAILSAFE_CANCEL_AFTER = _env_int("IMM_FAILSAFE_AFTER", 4)
WAKE_GAP_SECS = 600
WAKE_GRACE_SECS = 120
BLIND_PRESERVE_CYCLES = 3

# Series other repo bots trade (self-trade / infighting exclusion) + anything
# broadcast-reactive the user wants out entirely. Prefix match on the ticker.
# NOTE 2026-07-11: liquidity programs now exist on some crypto-fleet events
# (KXXRPMAXMON, KXDOGEMINMON) — the fleet's own resting ladders already earn
# those passively; this bot must never quote the same books (join-don't-lead
# would anchor two of our bots to each other's quotes).
# NOTE 2026-07-13: several series are owned by OTHER order-placing bots that run
# on GitHub Actions (cloud, bare-UUID client ids — they look "manual" to the
# standoff). Blocklisted per user decision: crossing them would trip same-account
# STP and cancel the other bot's incoming orders. The cloud trading fleet
# (.github/workflows/run_*_trading.yml):
#   mlb_trading.py -> KXMLBMENTION ; nbamention_v4_4.py -> KXNBAMENTION
#   ncaab_order_script_v42.py -> KXNCAABMENTION ; high_temp_trading.py -> KXHIGH*
_CRYPTO_ASSETS = ("SOL", "ETH", "BTC", "XRP", "ZEC", "HYPE", "DOGE", "BNB")
SERIES_BLOCKLIST_PREFIXES = tuple(
    [f"KX{a}MAXMON" for a in _CRYPTO_ASSETS] + [f"KX{a}MINMON" for a in _CRYPTO_ASSETS]
    + ["KXHIGH"]                                    # high_temp_trading.py (cloud)
    + ["KXMLBMENTION", "KXNBAMENTION", "KXNCAABMENTION"]   # mlb/nba/ncaa (cloud)
    + [p for p in os.environ.get("IMM_BLOCKLIST", "").split(",") if p]
)

# ---- universe allowlist (user decision 2026-07-11: MENTION + CRYPTO only) ----
# Mention/broadcast markets have DEFINED information windows (nothing to know
# before the broadcast starts) and the crypto structural markets have no
# insiders at all — the two lowest-adverse-selection families with the
# richest per-period pools. EXACT series matching (substring matching once
# caught KXHEGSETHOUT via 'ETH').
ALLOWLIST_ONLY = os.environ.get("IMM_ALLOWLIST_ONLY", "1") == "1"
ALLOW_SERIES_SUFFIXES = tuple(
    s for s in os.environ.get("IMM_ALLOW_SUFFIXES", "MENTION").split(",") if s)
# Series-name PREFIXES — for mention/incentive families that append a variable
# tail so the "MENTION" suffix match misses:
#   KXTEMP<CITY>            weather temp (covers new cities automatically)
#   KXEARNINGSMENTION<TKR>  per-company earnings-call mention markets, e.g.
#     KXEARNINGSMENTIONUAL (United). Same low-adverse-selection structure as the
#     other MENTIONs — nothing knowable before the call — and the midnight-ET
#     ticker-date cutoff keeps the bot out on report day (user decision 2026-07-15).
# KXTEMP re-enabled (Jack 2026-07-22 morning; was retired 7/21 evening after
# the adverse-selection post-mortem — see analysis: -6c/contract uniform).
# The 7/21 SeriesOverride tuning (5/2/2, cap 50, 5-90c, close-15min) applies.
ALLOW_SERIES_PREFIXES = tuple(
    p for p in os.environ.get(
        "IMM_ALLOW_PREFIXES", "KXTEMP,KXEARNINGSMENTION,KXAQICITY").split(",") if p)
_DEFAULT_CRYPTO_SERIES = (
    "KXCHINAUNBANBTC,KXETHMINY,KXETHMAXY,KXBTCMINY,KXBTCMAXY,KXSOLMINY,KXSOLMAXY,"
    "KXDOGEMINY,KXDOGEMAXY,KXXRPMINY,KXXRPMAXY,KXCRYPTORETURNY,KXBTCRESERVE,"
    "KXCRYPTOSTRUCTURE,KXBTCVSGOLD,KXINXVSBTC,KXBTC50VS100,"
    # Yearly pairs for the newer fleet assets (Jack 2026-07-22, KXBNBMAXY had
    # 6 live program markets). YEARLY only — the monthly *MAXMON/*MINMON
    # series stay excluded (crypto fleet's book; crossing trips same-account
    # STP and cancels its orders).
    "KXBNBMINY,KXBNBMAXY,KXHYPEMINY,KXHYPEMAXY,KXZECMINY,KXZECMAXY")
ALLOW_SERIES = frozenset(
    s for s in os.environ.get("IMM_ALLOW_SERIES", _DEFAULT_CRYPTO_SERIES).split(",") if s)

# Event-start resolution for mention markets: the ticker date's midnight-ET
# cutoff forfeits game-day daytime, but programs run ~1-2 days INCLUDING game
# day — resolve real start times where a reliable source exists, cut off
# EVENT_START_BUFFER_MIN before it, fall back to midnight ET.
EVENT_START_BUFFER_MIN = _env_int("IMM_EVENT_START_BUFFER_MIN", 30)
# Series with a real schedule API. Their games can be POSTPONED past the
# ticker date (NYDAL 7/16 -> makeup 7/20 while 18 program markets kept paying
# $100/day each), so the cheap 24h ticker-date pre-drop must not apply — the
# resolver + _screen decide from the live schedule instead.
SCHEDULE_RESOLVED_SERIES = frozenset(
    s for s in os.environ.get(
        "IMM_SCHEDULE_RESOLVED_SERIES",
        "KXWNBAMENTION,KXMLBMENTION,KXWCMENTION").split(",") if s)
# Fixed broadcast hours (ET) for series with no schedule API. Conservative early.
SERIES_START_ET = {}
for _pair in os.environ.get(
        "IMM_SERIES_START_ET",
        # LATENIGHT: generic "late night TV" series — currently Corden's WC
        # After Hours (nightly 11pm ET per Fox listings); 22:00 stays safe even
        # if a future episode is a 10pm-ET show (Gutfeld class). Without an
        # entry the midnight-ET fallback forfeits the whole day-of (2026-07-19).
        "KXLOVEISLMENTION=21:00,KXBIGBROTHERMENTION=20:00,KXFIGHTMENTION=17:00,"
        "KXLATENIGHTMENTION=22:00").split(","):
    if "=" in _pair:
        _s, _hm = _pair.split("=")
        try:
            _h, _m = _hm.split(":")
            SERIES_START_ET[_s.strip()] = (int(_h), int(_m))
        except ValueError:
            pass

MAX_CANDIDATE_BOOKS = _env_int("IMM_MAX_CANDIDATE_BOOKS", 250)
MIN_EST_DOLLARS_PER_DAY = _env_float("IMM_MIN_EST_PER_DAY", 0.50)  # ~min-payout floor (user 2026-07-14: 0.75->0.50, keep marginal strikes like LATENIGHT-VIKI in)
# STICKY SELECTION (Jack 2026-07-21): Kalshi pays a market's daily accrual
# only above a ~$1 minimum — deselecting a quoted market mid-life on a QUALITY
# screen (pinned book, spread jitter, estimator churn) strands that accrual
# below the threshold, pure waste. Once quoting starts, only a natural end
# (cutoff / closing / program_over / no_event_window) or a safety stop
# (manual standoff, halt) stops it.
STICKY_DEATH_REASONS = frozenset(
    {"cutoff", "no_event_window", "closing", "program_over"})

# THE BOT YIELDS TO THE HUMAN (user decision 2026-07-11). Jack trades some
# mention markets by hand on the same account. If the account's position on a
# market diverges from the bot's OWN book by this many contracts, or any
# non-imm resting order appears on a managed market, the bot cancels its
# quotes there and stands off until the manual activity is gone. Without this
# the inventory-skew logic would passively unwind his deliberate bets, his
# fills would pollute the loss halt, and STP would eat his crossing orders.
MANUAL_STANDOFF_CONTRACTS = _env_float("IMM_MANUAL_STANDOFF", 5)

# Self-trade prevention. The bot's orders are ALWAYS post-only (resting
# makers), so the only self-cross possible is the USER aggressing into a bot
# quote. 'maker' => the bot's resting order is the one cancelled on that
# cross, so the user's incoming manual order survives. 'taker_at_cross' (the
# other bots' default) only protects a resting order when OUR order is the
# taker — which post-only orders never are — so it left the user's crossing
# orders exposed. Set per-order here; the shared client default is untouched
# (the crypto fleet keeps its own).
STP_TYPE = os.environ.get("IMM_STP_TYPE", "maker")
# Yield at the EVENT level, not just the market: the user trades whole games /
# episodes by hand (positions or orders across several strikes of one event),
# so a manual footprint on ANY market of an event makes the bot avoid EVERY
# market of that event. Eliminates the self-cross surface at its source.
EVENT_LEVEL_STANDOFF = os.environ.get("IMM_EVENT_STANDOFF", "1") == "1"

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
HALT_FILE = os.path.join(STATUS_DIR, "HALT")

ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_RECIPIENTS = [r.strip() for r in os.environ.get(
    "IMM_ALERT_TO", "jackdu224@gmail.com").split(",") if r.strip()]
ALERT_DEDUPE_SECS = 6 * 3600
SMS_MAX_CHARS = 300
SUMMARY_HOUR_CT = _env_int("IMM_SUMMARY_HOUR_CT", 5)   # counters roll 6am ET


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}", flush=True)


# Rewards accrue only while orders REST — every sleep is lost rent plus a
# degraded post-wake cycle (DNS not back yet -> books/positions read as junk).
# SetThreadExecutionState(ES_SYSTEM_REQUIRED) vetoes S0/idle sleep while the
# process lives (display may still turn off; lid-close policy still wins).
# Cleared by the OS automatically when the process exits.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_keep_awake_state = {"ok": None}


def _keep_awake() -> None:
    if sys.platform != "win32" or os.environ.get("IMM_KEEP_AWAKE", "1") != "1":
        return
    try:
        import ctypes
        r = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        ok = bool(r)
    except Exception:
        ok = False
    if ok != _keep_awake_state["ok"]:   # log transitions only, not every cycle
        _keep_awake_state["ok"] = ok
        log(f"[IMM] keep-awake (block idle sleep): {'ON' if ok else 'FAILED'}")


def parse_iso_utc(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


# Manual EVENT-start cutoffs, event_ticker -> datetime (highest priority in the
# resolver). For earnings-mention markets this is the EARNINGS CALL start time
# (the market resolves on what's SAID DURING THE CALL) — NOT when earnings "come
# out" (the press release). Those can differ by a day: e.g. UAL releases Jul 15
# 4pm ET but the call is Jul 16 10:30am ET; NFLX releases Jul 15... no, releases
# AND interviews Jul 16 4:40pm ET. The ticker date and every automated feed
# (Kalshi occurrence/expiration, Nasdaq AMC/BMO) track the RELEASE or are stale,
# so call times are looked up per-company from the IR page (Kalshi
# settlement_sources) / press release and set here. Format env
# "EVENT_TICKER=ISO8601;...".
EVENT_START_OVERRIDES: Dict[str, datetime] = {}
for _pair in os.environ.get(
        "IMM_EVENT_START_OVERRIDE",
        "KXEARNINGSMENTIONNFLX-26JUL02=2026-07-16T16:40:00-04:00"      # NFLX call 4:40pm ET
        ";KXEARNINGSMENTIONUAL-26JUL16=2026-07-16T10:30:00-04:00"      # UAL call 10:30am ET
        # Jul 22 calls (looked up 2026-07-22; GEV omitted — its 7:30am ET call
        # already passed, midnight fallback correctly keeps it dead):
        ";KXEARNINGSMENTIONTSLA-26JUL22=2026-07-22T17:30:00-04:00"     # TSLA call 5:30pm ET
        ";KXEARNINGSMENTIONGOOGL-26JUL22=2026-07-22T16:30:00-04:00"    # GOOGL call 4:30pm ET
        ";KXEARNINGSMENTIONALK-26JUL22=2026-07-22T11:30:00-04:00"      # ALK call 11:30am ET
        ).split(";"):
    if "=" in _pair:
        _ev, _iso = _pair.split("=", 1)
        _dt = parse_iso_utc(_iso.strip())
        if _dt is not None:
            EVENT_START_OVERRIDES[_ev.strip()] = _dt

# File-based event-start overrides, written by imm_earnings_overrides.py (the
# daily call-time task) and hand-edits: {event_ticker: ISO8601}. Merged into
# EVENT_START_OVERRIDES at each universe refresh, hot-reloaded by mtime so a
# daytime write needs NO bot restart. Precedence: explicit env/code entries
# win; file entries may be updated/removed by later file writes.
EVENT_OVERRIDES_FILE = os.path.join(STATUS_DIR, "event_start_overrides.json")
_file_override_state = {"mtime": 0.0, "keys": set()}


def load_file_event_overrides() -> int:
    """Merge the overrides file into EVENT_START_OVERRIDES. Returns the
    number of entries added/changed; 0 when the file is unchanged/absent."""
    try:
        mtime = os.path.getmtime(EVENT_OVERRIDES_FILE)
    except OSError:
        return 0
    if mtime == _file_override_state["mtime"]:
        return 0
    _file_override_state["mtime"] = mtime
    changed = 0
    try:
        with open(EVENT_OVERRIDES_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError) as e:
        log(f"[IMM] ! event overrides file unreadable: {e}")
        return 0
    file_keys = set()
    for ev, iso in data.items():
        ev = str(ev).strip()
        dt_ = parse_iso_utc(str(iso).strip())
        if not ev or dt_ is None:
            log(f"[IMM] ! bad override entry ignored: {ev}={iso}")
            continue
        file_keys.add(ev)
        owned_by_env = (ev in EVENT_START_OVERRIDES
                        and ev not in _file_override_state["keys"])
        if owned_by_env:
            continue                      # env/code entry wins
        if EVENT_START_OVERRIDES.get(ev) != dt_:
            EVENT_START_OVERRIDES[ev] = dt_
            changed += 1
    for ev in _file_override_state["keys"] - file_keys:
        EVENT_START_OVERRIDES.pop(ev, None)   # removed from file -> forget
        changed += 1
    _file_override_state["keys"] = file_keys
    if changed:
        log(f"[IMM] event-start overrides file: {changed} entr(y/ies) merged")
    return changed


# Series stem for per-company earnings-call mentions (KXEARNINGSMENTION<SYMBOL>).
_EARNINGS_PREFIX = "KXEARNINGSMENTION"


# ----------------------------------------------------------------------------
# Event-start cutoff (user rule: never trade beyond the event start)
# ----------------------------------------------------------------------------

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_TICKER_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")


def parse_event_date(event_ticker: str) -> Optional[datetime]:
    """Date embedded in an event ticker's second segment, as 00:00 ET that day
    (UTC). 'KXWCMENTION-26JUL11ARGSUI' -> 2026-07-11 00:00 ET. Segments like
    '26OCTDELIV' (no day), '26NL', '30' don't parse -> None."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    m = _TICKER_DATE_RE.match(parts[1])
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        naive = datetime(2000 + int(yy), month, int(dd))
    except ValueError:
        return None
    return ET.localize(naive).astimezone(timezone.utc)


_MENTION_GAME_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})$")


def parse_mention_game(event_ticker: str) -> Optional[Tuple[datetime, str, str]]:
    """'KXMLBMENTION-26JUL12MILPIT' -> (event date as midnight ET, 'MIL', 'PIT').
    None when the segment isn't date+two-3-letter-codes."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    m = _MENTION_GAME_RE.match(parts[1])
    if not m:
        return None
    yy, mon, dd, a, b = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        naive = datetime(2000 + int(yy), month, int(dd))
    except ValueError:
        return None
    return ET.localize(naive).astimezone(timezone.utc), a, b


class EventStartResolver:
    """Best-effort real event start times for mention markets.

    MLB: statsapi.mlb.com (free, no auth — same source as mlb_trading.py).
    World Cup: ESPN public scoreboard. Fixed-hour series from SERIES_START_ET.
    Everything else / any failure -> None, and the caller falls back to the
    midnight-ET ticker-date rule (the safe direction). Results cached; failures
    cached briefly so a dead API can't stall the refresh loop."""

    NEG_TTL = 1800
    POS_TTL = 6 * 3600

    def __init__(self, http_get_json=None):
        self._get = http_get_json or self._default_get
        self.cache: Dict[str, Tuple[float, Optional[datetime]]] = {}

    @staticmethod
    def _default_get(url: str):
        # Full browser UA: Nasdaq's earnings API 403s minimal UAs; harmless for
        # statsapi/ESPN.
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    def resolve(self, series: str, event_ticker: str) -> Optional[datetime]:
        now = time.time()
        hit = self.cache.get(event_ticker)
        if hit and now < hit[0]:
            return hit[1]
        try:
            start = self._resolve_uncached(series, event_ticker)
        except Exception as e:
            log(f"[IMM] ! event-start resolve failed for {event_ticker}: {e}")
            start = None
        ttl = self.POS_TTL if start is not None else self.NEG_TTL
        self.cache[event_ticker] = (now + ttl, start)
        return start

    def _resolve_uncached(self, series: str, event_ticker: str) -> Optional[datetime]:
        # Manual override wins over everything. Earnings-mention markets rely on
        # it for the CALL start time — no automated feed gives the call time
        # (Nasdaq/Kalshi give the release, which can be a different day), so
        # earnings without an override fall through to the conservative
        # ticker-date rule below rather than being anchored to the wrong event.
        if event_ticker in EVENT_START_OVERRIDES:
            return EVENT_START_OVERRIDES[event_ticker]
        if series in SERIES_START_ET:
            d = parse_event_date(event_ticker)
            if d is None:
                return None
            h, m = SERIES_START_ET[series]
            et_day = d.astimezone(ET)
            return ET.localize(datetime(et_day.year, et_day.month, et_day.day, h, m)) \
                .astimezone(timezone.utc)
        # WNBA team codes are variable length (NYIND = NY+IND, LADAL = LA+DAL),
        # so the 3+3 parse below never matches them — without this branch WNBA
        # events silently fell to the midnight-ET rule and forfeited game day
        # (observed 2026-07-19: LADAL, a 1pm-ET tip, skipped all morning).
        if series == "KXWNBAMENTION":
            return self._espn_wnba_start(event_ticker)
        game = parse_mention_game(event_ticker)
        if game is None:
            return None
        date_utc, a, b = game
        et_date = date_utc.astimezone(ET).date()
        if series == "KXMLBMENTION":
            return self._mlb_start(et_date, {a, b})
        if series == "KXWCMENTION":
            return self._espn_soccer_start(et_date, {a, b})
        return None

    def _mlb_start(self, et_date, teams: Set[str]) -> Optional[datetime]:
        data = self._get(
            f"https://statsapi.mlb.com/api/v1/schedule?sportId=1"
            f"&startDate={et_date.isoformat()}&endDate={et_date.isoformat()}&hydrate=team")
        for de in data.get("dates", []):
            for g in de.get("games", []):
                t = g.get("teams", {})
                abbrs = {t.get("away", {}).get("team", {}).get("abbreviation", ""),
                         t.get("home", {}).get("team", {}).get("abbreviation", "")}
                if abbrs == teams:
                    return parse_iso_utc(g.get("gameDate", ""))
        return None

    def _espn_soccer_start(self, et_date, teams: Set[str]) -> Optional[datetime]:
        data = self._get(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
            f"scoreboard?dates={et_date.strftime('%Y%m%d')}")
        for ev in data.get("events", []):
            comps = (ev.get("competitions") or [{}])[0].get("competitors") or []
            abbrs = {(c.get("team") or {}).get("abbreviation", "") for c in comps}
            if teams <= abbrs:
                return parse_iso_utc(ev.get("date", ""))
        return None

    def _espn_wnba_start(self, event_ticker: str) -> Optional[datetime]:
        # Variable-length team codes, and Kalshi's don't always equal ESPN's
        # (Kalshi CONN vs ESPN CON — cost the whole CONNPHX game day when the
        # exact-concat match failed, 2026-07-19). Try every split of the blob;
        # a part matches a team when it prefix-matches the abbreviation in
        # either direction, or the uppercased location (WAS vs WSH/WASHINGTON).
        d = parse_event_date(event_ticker)
        seg = event_ticker.split("-")[1] if "-" in event_ticker else ""
        m = re.match(r"^\d{2}[A-Z]{3}\d{2}([A-Z]{4,8})$", seg)
        if d is None or not m:
            return None
        blob = m.group(1)
        et_date = d.astimezone(ET).date()
        # Range query (ticker date + Kalshi's 14-day postponement window):
        # a postponed game (NYDAL 7/16 -> makeup 7/20) keeps its markets open,
        # so the start we want is the pair's earliest NON-postponed meeting in
        # the window. Makeup not scheduled yet -> None -> midnight fallback
        # (no quotes, safe direction); the 30-min negative cache re-checks.
        data = self._get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
            f"scoreboard?dates={et_date.strftime('%Y%m%d')}"
            f"-{(et_date + timedelta(days=14)).strftime('%Y%m%d')}")

        def team_hit(part: str, team: dict) -> bool:
            ab = (team.get("abbreviation") or "").upper()
            loc = (team.get("location") or "").upper()
            return bool(ab) and (ab.startswith(part) or part.startswith(ab)
                                 or bool(loc and loc.startswith(part)))

        starts = []
        for ev in data.get("events", []):
            status = ((ev.get("status") or {}).get("type") or {}).get("name", "")
            if status in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"):
                continue
            comps = (ev.get("competitions") or [{}])[0].get("competitors") or []
            teams = [(c.get("team") or {}) for c in comps]
            if len(teams) != 2:
                continue
            for i in range(2, len(blob) - 1):
                x, y = blob[:i], blob[i:]
                if (team_hit(x, teams[0]) and team_hit(y, teams[1])) \
                        or (team_hit(x, teams[1]) and team_hit(y, teams[0])):
                    s = parse_iso_utc(ev.get("date", ""))
                    if s is not None:
                        starts.append(s)
                    break
        return min(starts) if starts else None


def series_hard_expiry_utc(series: str, event_ticker: str) -> Optional[datetime]:
    """A per-series hard expiry (hour, minute ET on the event date), converted
    to UTC — a floor on the trade cutoff so nothing rests past it. None if the
    series has no override or the event date can't be parsed."""
    ov = SERIES_OVERRIDES.get(series)
    if not ov or ov.hard_expiry_et is None:
        return None
    d = parse_event_date(event_ticker)
    if d is None:
        return None
    et_day = d.astimezone(ET)
    h, m = ov.hard_expiry_et
    return ET.localize(datetime(et_day.year, et_day.month, et_day.day, h, m)) \
        .astimezone(timezone.utc)


def trade_cutoff_utc(event_ticker: str, occurrence: Optional[datetime],
                     expected_expiration: Optional[datetime]) -> Optional[datetime]:
    """When we must be OUT of this market. Ticker-embedded event dates cut off
    at ET midnight day-of (most conservative reading — kickoff hour isn't in
    the API). An occurrence_datetime meaningfully before expiration marks a
    scheduled underlying event (earnings report, game) — cut off there too.
    None = no known event start; breakers are the only protection."""
    candidates = []
    td = parse_event_date(event_ticker)
    if td is not None:
        candidates.append(td)
    if (occurrence is not None and expected_expiration is not None
            and occurrence < expected_expiration - timedelta(minutes=60)):
        candidates.append(occurrence)
    elif occurrence is not None and expected_expiration is None:
        candidates.append(occurrence)
    return min(candidates) if candidates else None


# ----------------------------------------------------------------------------
# Price/book helpers (same conventions as the crypto MM: YES-book cents)
# ----------------------------------------------------------------------------

def dollars_to_cents(v) -> Optional[int]:
    try:
        c = round(float(v) * 100)
    except (TypeError, ValueError):
        return None
    return c if 0 <= c <= 100 else None


def market_cents(m: dict, base: str) -> Optional[int]:
    """Read a price off a market object: prefers '<base>_dollars' (V2 string),
    falls back to legacy integer-cent '<base>'. Returns None when absent or 0
    (Kalshi reports an empty side as 0)."""
    v = m.get(base + "_dollars")
    c = dollars_to_cents(v) if v is not None else None
    if c is None:
        v = m.get(base)
        try:
            c = int(v) if v is not None else None
        except (TypeError, ValueError):
            c = None
    return c if c else None   # 0 == side absent


def orderbook_levels(orderbook_response: dict) -> Tuple[List[List[float]], List[List[float]]]:
    """(yes_levels, no_levels) as [price_cents, qty], ascending by price."""
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
    """(best_yes_bid, best_yes_ask) in cents EXCLUDING our own resting orders
    given as (book_side, yes_price_cents, remaining). Our asks rest on the NO
    book at (100 - yes_price)."""
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


def order_yes_book_cents(order: dict) -> Optional[Tuple[str, int]]:
    """(book_side, yes_price_cents) from a normalized V2 order dict."""
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


# ----------------------------------------------------------------------------
# Reward-share estimator (implements the program's snapshot scoring)
# ----------------------------------------------------------------------------

def _side_share(levels_best_first: List[Tuple[int, float]], own: Dict[int, float],
                target: float, df: float) -> Tuple[float, bool]:
    """Our expected score share for one side of the book.

    levels_best_first: (price_cents, total_size) best price first, sizes
    INCLUDING our own resting size. own: price -> our size. Returns
    (our_share, side_qualifies). Mirrors the program rules: reference = best
    price (must exist and improve on 99); walk levels accumulating size until
    target reached; if the book runs out first, nobody scores."""
    if not levels_best_first or target <= 0:
        return 0.0, False
    ref = levels_best_first[0][0]
    if ref >= 99:
        return 0.0, False
    total_score = 0.0
    our_score = 0.0
    accumulated = 0.0
    for px, size in levels_best_first:
        w = df ** (ref - px)
        total_score += w * size
        our_score += w * min(own.get(px, 0.0), size)
        accumulated += size
        if accumulated >= target:
            break
    else:
        return 0.0, False   # book thinner than target: side doesn't qualify
    if total_score <= 0:
        return 0.0, False
    return our_score / total_score, True


def estimate_reward_share(yes_levels: List[List[float]], no_levels: List[List[float]],
                          own_orders: List[Tuple[str, int, float]],
                          target: float, df: float,
                          own_in_book: bool) -> Tuple[float, int]:
    """(our_fraction_of_pool, qualifying_sides). own_in_book=True when the
    orderbook read already contains our resting orders (live mode); False
    overlays them (dry run). Pool fraction per program rules: each qualifying
    side normalizes to 1.0 of score share per snapshot, so our fraction is
    (our_yes_share + our_no_share) / qualifying_sides."""
    own_yes: Dict[int, float] = {}
    own_no: Dict[int, float] = {}
    for book_side, yes_px, remaining in own_orders:
        if book_side == "bid":
            own_yes[yes_px] = own_yes.get(yes_px, 0.0) + remaining
        else:
            own_no[100 - yes_px] = own_no.get(100 - yes_px, 0.0) + remaining

    def prep(levels: List[List[float]], own: Dict[int, float]) -> List[Tuple[int, float]]:
        by_px: Dict[int, float] = {int(px): float(q) for px, q in levels}
        if not own_in_book:
            for px, q in own.items():
                by_px[px] = by_px.get(px, 0.0) + q
        return sorted(by_px.items(), key=lambda kv: -kv[0])

    share_yes, q_yes = _side_share(prep(yes_levels, own_yes), own_yes, target, df)
    share_no, q_no = _side_share(prep(no_levels, own_no), own_no, target, df)
    sides = int(q_yes) + int(q_no)
    if sides == 0:
        return 0.0, 0
    return (share_yes + share_no) / sides, sides


# ----------------------------------------------------------------------------
# Quote construction
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    ticker: str
    book_side: str      # 'bid' (buy YES) or 'ask' (sell YES == buy NO)
    price_cents: int
    count: int
    is_pad: bool = False   # deep 1c/99c depth-padding order (see pad_to_target):
    #   fills the side to the reward target so the near-touch ladder qualifies;
    #   earns ~0 itself, exempt from the per-market ladder/position caps.


def build_side_ladder(ticker: str, book_side: str, anchor: int,
                      opposite_best: Optional[int], room: float,
                      levels: Optional[List[Tuple[int, int]]] = None) -> List[Quote]:
    """Ladder behind (never improving) the join anchor. `anchor` is the best
    EXTERNAL price on our side; `opposite_best` the best external price on the
    other side (post-only: never cross it). Sizes come from `levels` (the
    market's series ladder; global LEVELS if omitted), shaved to `room`
    (contracts we may still acquire on this side if everything fills)."""
    if levels is None:
        levels = LEVELS
    quotes: List[Quote] = []
    room = int(room)
    if room <= 0:
        return quotes
    if opposite_best is not None:
        if book_side == "bid" and anchor >= opposite_best:
            anchor = opposite_best - 1
        elif book_side == "ask" and anchor <= opposite_best:
            anchor = opposite_best + 1
    pmin, pmax = series_price_min(series_of(ticker)), series_price_max(series_of(ticker))
    for ticks, size in levels:
        px = anchor - ticks if book_side == "bid" else anchor + ticks
        # BOTH sides, BOTH bounds: the band was originally bid-min/ask-max
        # only, which let a BID rest at 97c on a likely-YES temp strike
        # (filled live 2026-07-21, AUSH-2118-T96.99) and would let asks rest
        # below the floor (= buying NO above the cap). No order may price
        # outside the series band, period.
        if px < pmin or px > pmax:
            continue
        if px < 1 or px > 99:
            continue
        count = min(size, room)
        if count <= 0:
            break
        quotes.append(Quote(ticker, book_side, px, count))
        room -= count
    return quotes


def skewed_side_room(base_room: float, pos: float, accumulating: bool,
                     side_max: Optional[int] = None) -> float:
    """Inventory skew on top of hard caps: past SKEW_SOFT net contracts the
    accumulating side is halved, past SKEW_HARD it is pulled entirely."""
    if not accumulating:
        return base_room
    if side_max is None:
        side_max = SIDE_MAX_CONTRACTS
    a = abs(pos)
    if a >= SKEW_HARD_CONTRACTS:
        return 0.0
    if a >= SKEW_SOFT_CONTRACTS:
        return min(base_room, side_max / 2.0)
    return base_room


def diff_orders(desired: List[Quote], resting: List[dict],
                order_ages: Dict[str, float], now_ts: float,
                preserve_tickers: Set[str] = frozenset()) -> Tuple[List[Quote], List[str]]:
    """Match desired quotes to resting orders EXACTLY (price and remaining
    size) — reward credit halves per tick, and a stale at-best order whose
    anchor faded would be leading the book. Stale-by-TTL orders are replaced.
    Orders on preserve_tickers (blind markets) are left untouched."""
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
                      and q.price_cents == px and round(remaining) == q.count), None)
        if match is not None and not stale:
            unmatched.remove(match)
        else:
            to_cancel.append(oid)

    to_place.extend(q for q in unmatched if q.ticker not in preserve_tickers)
    return to_place, to_cancel


def ladder_collateral_dollars(bid_anchor: Optional[int], ask_anchor: Optional[int],
                              levels: Optional[List[Tuple[int, int]]] = None) -> float:
    """Worst-case collateral if the full two-sided ladder rests: YES bids
    reserve px, NO bids (our asks) reserve 100-px, per contract."""
    if levels is None:
        levels = LEVELS
    total = 0.0
    if bid_anchor is not None:
        for ticks, size in levels:
            total += max(bid_anchor - ticks, 1) * size
    if ask_anchor is not None:
        for ticks, size in levels:
            total += max(100 - (ask_anchor + ticks), 1) * size
    return total / 100.0


# ----------------------------------------------------------------------------
# Realized P&L tracker (avg-cost round trips, from fills)
# ----------------------------------------------------------------------------

class PnlTracker:
    """Average-cost realized P&L per market from a fill stream. Signed YES
    position; buying NO at p == selling YES at 100-p (cash view)."""

    def __init__(self):
        self.pos: Dict[str, float] = {}
        self.avg: Dict[str, float] = {}     # avg entry price (cents) of open pos
        self.realized: Dict[str, float] = {}

    def on_fill(self, ticker: str, side: str, action: str, count: float,
                yes_price_cents: float) -> None:
        """side/action in legacy customer view (client normalizes).
        `yes_price_cents` is ALWAYS the YES price of the trade (fills carry
        yes_price_dollars regardless of side). Buying NO at no-price p is
        selling YES at the yes-price (100-p) — the price passed in is already
        that yes-price, so only the action flips for the NO side."""
        if side == "no":
            action = "sell" if action == "buy" else "buy"
        signed = count if action == "buy" else -count
        pos = self.pos.get(ticker, 0.0)
        avg = self.avg.get(ticker, 0.0)
        if pos * signed >= 0:   # extending (or opening) — new weighted avg
            new_pos = pos + signed
            if abs(new_pos) > 1e-9:
                self.avg[ticker] = (abs(pos) * avg + abs(signed) * yes_price_cents) / abs(new_pos)
            self.pos[ticker] = new_pos
            return
        # reducing / flipping: realize on the closed portion
        closed = min(abs(signed), abs(pos))
        pnl_per = (yes_price_cents - avg) if pos > 0 else (avg - yes_price_cents)
        self.realized[ticker] = self.realized.get(ticker, 0.0) + closed * pnl_per / 100.0
        new_pos = pos + signed
        self.pos[ticker] = new_pos
        if pos * new_pos < 0:      # flipped through zero: remainder opens at fill px
            self.avg[ticker] = yes_price_cents
        elif abs(new_pos) < 1e-9:
            self.avg[ticker] = 0.0

    def total_realized(self) -> float:
        return sum(self.realized.values())

    def unrealized(self, marks: Dict[str, float]) -> float:
        """Mark-to-market of the open book against `marks` (YES mid, cents).
        Unmarked positions are valued at cost (zero unrealized) — and logged
        upstream, since an unmarkable position hides real risk."""
        total = 0.0
        for t, p in self.pos.items():
            if abs(p) < 1e-9:
                continue
            mark = marks.get(t)
            if mark is None:
                continue
            total += p * (mark - self.avg.get(t, 0.0)) / 100.0
        return total

    def inventory_contracts(self) -> float:
        return sum(abs(p) for p in self.pos.values() if abs(p) > 1e-9)


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------

def load_private_key():
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
# Alerting (repo Gmail-SMTP convention; email push to the user's inbox)
# ----------------------------------------------------------------------------

class Alerter:
    def __init__(self, tag: str, live: bool):
        self.tag = tag
        self.live = live
        self.last_sent: Dict[Tuple[str, str], float] = {}
        self.today: List[Tuple[str, str]] = []
        self.last_summary_date = None
        self.last_summary_body: Optional[str] = None
        self.enabled = bool(ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD and ALERT_RECIPIENTS)
        if self.enabled:
            log(f"[{tag}] alerts -> {', '.join(ALERT_RECIPIENTS)}")
        else:
            log(f"[{tag}] alerting LOG-ONLY: set ALERT_EMAIL_FROM + ALERT_EMAIL_PASSWORD")

    def _mode(self) -> str:
        return "LIVE" if self.live else "DRY"

    @staticmethod
    def _is_sms_gateway(recipient: str) -> bool:
        local = recipient.split("@")[0]
        return local.isdigit() and len(local) >= 7

    def send_message(self, body: str, subject: str = "", html: Optional[str] = None) -> bool:
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
                msg["Subject"] = "" if is_sms else (subject or f"{self.tag} bot alert")
                msg["From"] = ALERT_EMAIL_FROM
                msg["To"] = recipient
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                    server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                    server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
                ok = True
            except Exception as e:
                log(f"[{self.tag}] ! alert to {recipient} failed: {e}")
        return ok

    def alert(self, category: str, message: str, key: str = "",
              urgent: bool = True, now_ts: Optional[float] = None) -> None:
        now_ts = time.time() if now_ts is None else now_ts
        log(f"[{self.tag}] ALERT [{category}] {message}")
        self.today.append((category, message))
        if not (urgent and self.enabled):
            return
        dedupe_key = (category, key)
        last = self.last_sent.get(dedupe_key)
        if last is not None and now_ts - last < ALERT_DEDUPE_SECS:
            return
        if self.send_message(f"[{self.tag} {self._mode()}] {category}: {message}",
                             subject=f"{self.tag} {category} alert"):
            self.last_sent[dedupe_key] = now_ts

    def maybe_daily_summary(self, now_utc: datetime, body_builder) -> None:
        """Send the daily summary email once per CT day at/after SUMMARY_HOUR_CT.
        This bot is standalone (no fleet digest), so it emails directly unless
        IMM_SUMMARY_EMAIL=0."""
        now_ct = now_utc.astimezone(CT)
        if self.last_summary_date is None:
            self.last_summary_date = (now_ct.date() if now_ct.hour >= SUMMARY_HOUR_CT
                                      else now_ct.date() - timedelta(days=1))
            return
        if now_ct.hour >= SUMMARY_HOUR_CT and now_ct.date() != self.last_summary_date:
            self.last_summary_date = now_ct.date()
            body = body_builder()
            self.last_summary_body = body
            log(f"[{self.tag}] daily summary: {body}")
            if self.enabled and os.environ.get("IMM_SUMMARY_EMAIL", "1") == "1":
                self.send_message(body, subject=f"{self.tag} daily summary")
            self.today.clear()


# ----------------------------------------------------------------------------
# Market metadata & bot state
# ----------------------------------------------------------------------------

@dataclass
class MarketMeta:
    ticker: str
    event_ticker: str
    series: str
    dollars_per_day: float
    program_end: Optional[datetime]
    target_size: float
    discount_factor: float
    cutoff: Optional[datetime]          # never trade at/after this
    close_time: Optional[datetime]
    mid_cents: Optional[float] = None
    spread_cents: Optional[int] = None
    volume: float = 0.0
    status: str = ""
    open_time: Optional[datetime] = None
    est_frac: float = 0.0               # estimated pool share with our ladder resting
    est_dollars_per_day: float = 0.0    # est_frac x pool rate
    yield_per_contract: float = 0.0     # $/day per resting contract — the ranking metric


@dataclass
class BotState:
    selected: Dict[str, MarketMeta] = field(default_factory=dict)
    managed_extra: Dict[str, MarketMeta] = field(default_factory=dict)  # reduce-only tail
    universe_at: float = 0.0
    programs_count: int = 0
    prev_mid: Dict[str, float] = field(default_factory=dict)
    prev_pos: Dict[str, float] = field(default_factory=dict)
    breaker_until: Dict[str, float] = field(default_factory=dict)
    bench_until: Dict[str, float] = field(default_factory=dict)
    zero_share_streak: Dict[str, int] = field(default_factory=dict)
    blind_streak: Dict[str, int] = field(default_factory=dict)
    order_ages: Dict[str, float] = field(default_factory=dict)
    sim_orders: Dict[str, dict] = field(default_factory=dict)
    ledger: Dict[str, dict] = field(default_factory=dict)
    last_fill_ts: int = 0
    seen_fill_ids: Dict[str, int] = field(default_factory=dict)   # fill_id -> ts (dedupe)
    our_order_ids: Dict[str, float] = field(default_factory=dict)  # order_id -> placed ts
    known_tickers: Set[str] = field(default_factory=set)   # every market we ever quoted
    sticky_prev: Set[str] = field(default_factory=set)     # selected at last save — makes
    #   sticky selection survive a restart (state.selected itself is rebuilt live)
    manual_standoff: Dict[str, float] = field(default_factory=dict)  # ticker -> since ts
    cutoff_ts: Dict[str, float] = field(default_factory=dict)     # ticker -> cutoff epoch
    place_uncertain: Dict[Tuple[str, str, int], float] = field(default_factory=dict)
    realized_baseline: float = 0.0       # lifetime realized at last daily roll
    last_mark: Dict[str, float] = field(default_factory=dict)   # ticker -> YES mid cents
    day_baseline: Optional[float] = None  # realized+unrealized at last daily roll
    halted_until: float = 0.0            # daily-loss halt (epoch)
    consecutive_errors: int = 0
    # daily counters (reset when the summary sends)
    cycles_today: int = 0
    placed_today: int = 0
    cancelled_today: int = 0
    errors_today: int = 0
    fills_today: float = 0.0
    reward_est_today: float = 0.0
    reward_est_lifetime: float = 0.0      # cumulative est reward (NOT reset at the
    #   daily roll; persisted so it survives restarts) — the digest's running total
    contract_minutes_today: float = 0.0   # resting contracts x minutes quoted
    reward_accrue_at: float = 0.0
    last_markets_line: str = ""


# ----------------------------------------------------------------------------
# The market maker
# ----------------------------------------------------------------------------

class IncentiveMarketMaker:
    TAG = "IMM"

    def __init__(self, client: Optional[ExchangeClient], live: bool):
        self.client = client
        self.live = live
        self.state = BotState()
        self.pnl = PnlTracker()
        self.tag = f"[{self.TAG}]"
        self.alerter = Alerter(self.TAG, live)
        self.resolver = EventStartResolver()
        self._foreign_resting: Dict[str, int] = {}
        self._shutdown_done = False
        self.wake_grace_until = 0.0   # epoch; cycles before this ran on a
        #   possibly half-connected post-sleep network (see run())
        self._reconciled = False      # one-time orphaned-own-fill cleanup pending
        self._reconcile_recheck_at = 0.0   # two-shot: second pass after sweep window
        self._load_persist()

    # ---- restart persistence (which markets are OURS) ------------------------
    # Realized P&L and breakers reset on restart (accepted); the set of tickers
    # this bot has ever quoted must not — it drives the fills filter and the
    # inventory budget reserve, and misclassifying the crypto fleet's positions
    # as ours (or ours as theirs) breaks both.

    PERSIST_PATH = os.path.join(STATUS_DIR, "imm_state.json")

    ORDER_JOURNAL_PATH = os.path.join(STATUS_DIR, "imm_order_journal.jsonl")

    def _journal_order_id(self, oid: str, ts: float) -> None:
        """Append-only crash journal for order ownership: one line per placed
        order so a hard-kill can never orphan a fill, without paying the full
        state dump per order. Folded into the main file (and truncated) by
        _save_persist."""
        try:
            with open(self.ORDER_JOURNAL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"oid": oid, "ts": ts}) + "\n")
        except OSError as e:
            log(f"{self.tag} ! order journal write failed: {e}")

    def _load_journal(self) -> None:
        """Merge journaled order ids the last run placed but never folded into
        the main state file (i.e. it was killed mid-wave)."""
        try:
            with open(self.ORDER_JOURNAL_PATH, encoding="utf-8") as f:
                n = 0
                for line in f:
                    try:
                        rec = json.loads(line)
                        oid = str(rec["oid"])
                        if oid not in self.state.our_order_ids:
                            self.state.our_order_ids[oid] = float(rec["ts"])
                            n += 1
                    except (ValueError, KeyError, TypeError):
                        continue
            if n:
                log(f"{self.tag} recovered {n} journaled order id(s) from an unclean shutdown")
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f"{self.tag} ! order journal read failed: {e}")

    def _load_persist(self) -> None:
        try:
            with open(self.PERSIST_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.state.known_tickers = set(data.get("known_tickers") or [])
            self.state.last_fill_ts = int(data.get("last_fill_ts") or 0)
            self.state.seen_fill_ids = {str(k): int(v) for k, v in
                                        (data.get("seen_fill_ids") or {}).items()}
            self.state.our_order_ids = {str(k): float(v) for k, v in
                                        (data.get("our_order_ids") or {}).items()}
            # The bot's own net book (positions AND entry costs), rebuilt so
            # manual-vs-bot divergence and mark-to-market both survive
            # restarts (realized P&L still resets).
            for t, p in (data.get("own_pos") or {}).items():
                self.pnl.pos[str(t)] = float(p)
            for t, a in (data.get("own_avg") or {}).items():
                self.pnl.avg[str(t)] = float(a)
            self.state.reward_est_lifetime = float(data.get("reward_est_lifetime") or 0.0)
            self.state.sticky_prev = set(data.get("selected_tickers") or [])
            if self.state.known_tickers:
                log(f"{self.tag} restored {len(self.state.known_tickers)} known tickers")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"{self.tag} ! state restore failed ({e}); starting fresh")
        # After the main file (or its absence): fold in ids the previous run
        # journaled but never got to fold in itself.
        self._load_journal()

    def _save_persist(self) -> None:
        try:
            os.makedirs(STATUS_DIR, exist_ok=True)
            tmp = self.PERSIST_PATH + ".tmp"
            # Bound our_order_ids: fills for week-old orders can't arrive
            # (orders die at TTL/cutoff; fills read starts hours back at most).
            horizon = time.time() - 7 * 86400
            self.state.our_order_ids = {k: v for k, v in self.state.our_order_ids.items()
                                        if v >= horizon}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"known_tickers": sorted(self.state.known_tickers),
                           "selected_tickers": sorted(set(self.state.selected)
                                                      | self.state.sticky_prev),
                           "last_fill_ts": self.state.last_fill_ts,
                           "seen_fill_ids": self.state.seen_fill_ids,
                           "our_order_ids": self.state.our_order_ids,
                           "own_pos": {t: p for t, p in self.pnl.pos.items()
                                       if abs(p) > 1e-9},
                           "own_avg": {t: self.pnl.avg.get(t, 0.0)
                                       for t, p in self.pnl.pos.items()
                                       if abs(p) > 1e-9},
                           "reward_est_lifetime": self.state.reward_est_lifetime}, f)
            os.replace(tmp, self.PERSIST_PATH)
            # Journal contents are now in the main file; truncate so a later
            # crash-load doesn't re-merge stale (already-pruned) ids.
            try:
                open(self.ORDER_JOURNAL_PATH, "w", encoding="utf-8").close()
            except OSError:
                pass
        except Exception as e:
            log(f"{self.tag} ! state save failed: {e}")

    # ---- exchange wrappers (writes gated on self.live) ----------------------

    def place_order(self, q: Quote, now_ts: float) -> bool:
        client_order_id = f"{CLIENT_ORDER_PREFIX}-{RUN_ID}-{uuid.uuid4().hex[:12]}"
        expiration_ts = int(time.time() + ORDER_TTL_SECS)   # stamped per-send
        # Never let an order outlive the market's event-start cutoff: the
        # exchange enforces the user's hard rule even if this process dies.
        cutoff_ts = self.state.cutoff_ts.get(q.ticker)
        if cutoff_ts is not None:
            expiration_ts = min(expiration_ts, int(cutoff_ts))
            if expiration_ts <= time.time() + 1:
                log(f"{self.tag} not placing {q.ticker} {q.book_side}: cutoff reached")
                return False
        if q.book_side == "bid":
            kwargs = dict(side="yes", action="buy", yes_price=q.price_cents)
        else:
            kwargs = dict(side="no", action="buy", no_price=100 - q.price_cents)
        kwargs["self_trade_prevention_type"] = STP_TYPE   # user's order wins a self-cross
        label = (f"{q.ticker} {q.book_side.upper():4s} {q.count}x @ {q.price_cents}c")
        if not self.live:
            oid = f"sim-{uuid.uuid4().hex[:12]}"
            self.state.sim_orders[oid] = {
                "order_id": oid, "ticker": q.ticker, "book_side": q.book_side,
                "yes_price": q.price_cents, "remaining_count": float(q.count),
                "status": "resting", "client_order_id": client_order_id,
                "expire_at": float(expiration_ts),
            }
            self.state.order_ages[oid] = now_ts
            log(f"{self.tag} [DRY] place {label}")
            return True
        try:
            resp = self.client.create_order(
                ticker=q.ticker, client_order_id=client_order_id,
                count=q.count, type="limit", post_only=True,
                expiration_ts=expiration_ts, **kwargs)
            oid = (resp.get("order") or {}).get("order_id") or resp.get("order_id", "?")
            self.state.order_ages[oid] = now_ts
            self.state.our_order_ids[oid] = now_ts   # fills are matched by ownership
            self.state.ledger[oid] = {
                "order_id": oid, "ticker": q.ticker, "book_side": q.book_side,
                "yes_price": q.price_cents, "remaining_count": float(q.count),
                "status": "resting", "client_order_id": client_order_id,
                "_placed_at": now_ts, "_confirmed": False,
            }
            log(f"{self.tag} placed {label} -> {oid}")
            # Durable PER ORDER, but cheap: append the id to a journal (<1ms)
            # instead of dumping the full ~3.4MB state (~450ms) — a hard-kill
            # mid-wave orphaned 3 fills on unsaved ids (2026-07-19, ~19:22Z
            # restart) and the false "manual" standoffs cost CONNPHX its
            # evening. _save_persist folds the journal in and truncates it.
            if self.live:
                self._journal_order_id(oid, now_ts)
            return True
        except HttpError as e:
            # A definitive rejection (post-only would cross, etc.) — no order exists.
            log(f"{self.tag} ! place rejected ({e}): {label}")
            return False
        except Exception as e:
            # Ambiguous failure (timeout mid-flight): the order MAY be live but
            # untracked, and the eventually-consistent read won't show it yet.
            # Cool this exact level down so the next cycle can't double-place
            # before the read catches up (TTL bounds the orphan either way).
            self.state.place_uncertain[(q.ticker, q.book_side, q.price_cents)] = now_ts
            log(f"{self.tag} ! place failed ({e}): {label}; level cooled "
                f"{2 * POLL_SECS}s (order may exist untracked)")
            return False

    def cancel_order(self, order_id: str) -> bool:
        if not self.live:
            self.state.sim_orders.pop(order_id, None)
            self.state.order_ages.pop(order_id, None)
            log(f"{self.tag} [DRY] cancel {order_id}")
            return True
        try:
            self.client.cancel_order(order_id)
            self.state.order_ages.pop(order_id, None)
            self.state.ledger.pop(order_id, None)
            return True
        except HttpError as e:
            if e.status in (404, 409):
                self.state.order_ages.pop(order_id, None)
                self.state.ledger.pop(order_id, None)
                return True
            log(f"{self.tag} ! cancel failed {order_id}: {e}")
            return False
        except Exception as e:
            log(f"{self.tag} ! cancel failed {order_id}: {e}")
            return False

    def _get_resting_orders_global(self) -> List[dict]:
        """All resting orders on the account with our client prefix, paginated.
        (Global read: this bot spans many events; other bots' prefixes differ.)
        Side effect: counts FOREIGN resting orders per ticker (manual orders,
        other bots) — a foreign order on a managed market triggers the
        manual-standoff yield."""
        orders: List[dict] = []
        foreign: Dict[str, int] = {}
        cursor = None
        for _page in range(40):
            resp = self.client.get_orders(status="resting", limit=200, cursor=cursor)
            batch = resp.get("orders") or []
            for o in batch:
                if o.get("status") != "resting":
                    continue
                if str(o.get("client_order_id", "")).startswith(CLIENT_ORDER_PREFIX + "-"):
                    orders.append(o)
                else:
                    t = o.get("ticker", "")
                    foreign[t] = foreign.get(t, 0) + 1
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        self._foreign_resting = foreign
        return orders

    def _merge_ledger(self, exchange_orders: List[dict], now_ts: float) -> List[dict]:
        """Union of the exchange view and the local ledger (Kalshi portfolio
        reads are eventually consistent — see crypto_touch_mm for the whole
        rationale). Unconfirmed young entries count as resting; older ones are
        verified once via get_order; confirmed-then-absent entries are done."""
        merged = list(exchange_orders)
        seen_ids = {o.get("order_id") for o in exchange_orders}
        grace = 2 * POLL_SECS + 15
        for oid, entry in list(self.state.ledger.items()):
            if oid in seen_ids:
                entry["_confirmed"] = True
                continue
            if entry["_confirmed"] or now_ts >= entry["_placed_at"] + ORDER_TTL_SECS:
                self.state.ledger.pop(oid, None)
                self.state.order_ages.pop(oid, None)
                continue
            if now_ts - entry["_placed_at"] <= grace:
                merged.append(entry)
                continue
            try:
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
                    merged.append(entry)
            except Exception:
                merged.append(entry)
        return merged

    def fetch_resting_orders(self, now_ts: float) -> List[dict]:
        if not self.live:
            expired = [oid for oid, o in self.state.sim_orders.items()
                       if o.get("expire_at", 0) <= now_ts]
            for oid in expired:
                self.state.sim_orders.pop(oid, None)
                self.state.order_ages.pop(oid, None)
            return list(self.state.sim_orders.values())
        return self._merge_ledger(self._get_resting_orders_global(), now_ts)

    def cancel_all_bot_orders(self) -> int:
        """Cancel every resting imm- order on the account (any run)."""
        if not self.live:
            n = len(self.state.sim_orders)
            self.state.sim_orders.clear()
            self.state.order_ages.clear()
            if n:
                log(f"{self.tag} [DRY] cancelled all {n} simulated orders")
            return n
        n = 0
        try:
            orders = self._merge_ledger(self._get_resting_orders_global(), time.time())
            for o in orders:
                if self.cancel_order(o["order_id"]):
                    n += 1
        except Exception as e:
            log(f"{self.tag} ! cancel-all sweep failed: {e}")
        return n

    def cancel_market_orders(self, ticker: str, resting: List[dict]) -> int:
        n = 0
        for o in resting:
            if o.get("ticker") == ticker and self.cancel_order(o.get("order_id", "")):
                n += 1
        return n

    def fetch_positions(self) -> Dict[str, float]:
        """ticker -> signed YES position, all unsettled markets on the account.
        Raises DataError-alike on failure so the cycle counts as errored."""
        positions: Dict[str, float] = {}
        cursor = None
        for _page in range(40):
            resp = self.client.get_positions(limit=200, cursor=cursor,
                                             settlement_status="unsettled")
            for p in (resp.get("market_positions") or resp.get("positions") or []):
                pos = p.get("position")
                if pos is not None and p.get("ticker") and abs(float(pos)) > 1e-9:
                    positions[p["ticker"]] = float(pos)
            cursor = resp.get("cursor")
            if not cursor:
                break
        return positions

    @staticmethod
    def _fill_ts(f: dict) -> int:
        ts = f.get("ts")
        if ts is not None:
            try:
                return int(ts)
            except (TypeError, ValueError):
                pass
        dt = parse_iso_utc(f.get("created_time", ""))
        return int(dt.timestamp()) if dt else 0

    def fetch_new_fills(self) -> List[dict]:
        """NEW fills of OUR OWN ORDERS since the last read (live only).
        Ownership is matched by order_id against the persisted our_order_ids
        set — NOT by ticker: the user trades some of the same mention markets
        by hand, and his fills must never enter this bot's P&L or loss halt.
        min_ts is inclusive and second-granular, so boundary fills reappear
        every read — fill_id dedupe stops them re-booking every 90s."""
        if not self.live:
            return []
        fills: List[dict] = []
        cursor = None
        newest_any = 0
        min_ts = (self.state.last_fill_ts - 2) if self.state.last_fill_ts \
            else int(time.time()) - 3600
        for _page in range(20):
            resp = self.client.get_fills(min_ts=min_ts, limit=200, cursor=cursor)
            batch = resp.get("fills") or []
            for f in batch:
                newest_any = max(newest_any, self._fill_ts(f))
                fid = f.get("fill_id") or f.get("trade_id") or ""
                if f.get("order_id") not in self.state.our_order_ids \
                        or fid in self.state.seen_fill_ids:
                    continue
                self.state.seen_fill_ids[fid] = self._fill_ts(f)
                fills.append(f)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        # Advance the cursor on ANY observed fill (the crypto fleet fills
        # constantly on this account) — anchoring it to our own fills only
        # would let the scan window grow past the pagination cap.
        if newest_any:
            self.state.last_fill_ts = max(self.state.last_fill_ts, newest_any)
        if len(self.state.seen_fill_ids) > 4000:   # bounded memory
            cut = sorted(self.state.seen_fill_ids.values())[len(self.state.seen_fill_ids) - 3000]
            self.state.seen_fill_ids = {k: v for k, v in self.state.seen_fill_ids.items()
                                        if v >= cut}
        return fills

    # ---- universe: programs -> candidate markets -> selection ---------------

    def fetch_programs(self) -> Dict[str, dict]:
        """market_ticker -> aggregated active liquidity program info."""
        now = datetime.now(timezone.utc)
        programs: List[dict] = []
        cursor = None
        for _page in range(20):
            params = {"limit": 1000, "status": "active"}
            if cursor:
                params["cursor"] = cursor
            resp = self.client.get("/incentive_programs", params=params)
            batch = resp.get("incentive_programs") or []
            programs.extend(batch)
            cursor = resp.get("next_cursor")
            if not cursor or not batch:
                break
        by_market: Dict[str, dict] = {}
        for p in programs:
            if p.get("incentive_type") != "liquidity" or p.get("paid_out"):
                continue
            start = parse_iso_utc(p.get("start_date", ""))
            end = parse_iso_utc(p.get("end_date", ""))
            if not start or not end or not (start <= now < end):
                continue
            days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
            dpd = (p.get("period_reward") or 0) / 10000.0 / days
            try:
                target = float(p.get("target_size_fp") or 0)
            except (TypeError, ValueError):
                target = 0.0
            df = (p.get("discount_factor_bps") or 5000) / 10000.0
            t = p.get("market_ticker") or ""
            cur = by_market.get(t)
            if cur is None:
                by_market[t] = {"dollars_per_day": dpd, "end": end,
                                "target": target, "df": df}
            else:
                cur["dollars_per_day"] += dpd
                cur["end"] = max(cur["end"], end)
                cur["target"] = max(cur["target"], target)
        self.state.programs_count = len(by_market)
        return by_market

    @staticmethod
    def _blocked(ticker: str) -> bool:
        return any(ticker.startswith(p) for p in SERIES_BLOCKLIST_PREFIXES)

    @classmethod
    def _allowed(cls, ticker: str) -> bool:
        """Blocklist always wins; then the exact-series allowlist (MENTION
        suffix + named crypto series) unless ALLOWLIST_ONLY is off."""
        if cls._blocked(ticker):
            return False
        if not ALLOWLIST_ONLY:
            return True
        series = ticker.split("-")[0]
        return series in ALLOW_SERIES or \
            any(series.endswith(suf) for suf in ALLOW_SERIES_SUFFIXES) or \
            any(series.startswith(p) for p in ALLOW_SERIES_PREFIXES)

    def refresh_universe(self, now_utc: datetime, positions: Dict[str, float]) -> None:
        now_ts = now_utc.timestamp()
        # Pick up call-time overrides written since the last refresh (daily
        # earnings task / hand edits) — cheap mtime check, no restart needed.
        load_file_event_overrides()
        # Hourly program families (KXTEMP) activate at the TOP OF THE HOUR —
        # but LATE (absent ~hh:01, present ~hh:11): a single hour-crossed
        # refresh reliably fires before Kalshi publishes now that keep-alive
        # keeps cycles short, and the 600s gate then sat blind to ~hh:11
        # (observed 2026-07-21: AUSH-2023 dark 12 min). So inside the
        # activation window the gate drops to per-cycle; hour_crossed still
        # covers long gaps (sleep/wake) that skip the window entirely.
        hour_crossed = int(now_ts // 3600) != int(self.state.universe_at // 3600)
        in_activation_window = (now_ts % 3600) < HOURLY_ACTIVATION_WINDOW_SECS
        if (now_ts - self.state.universe_at < UNIVERSE_REFRESH_SECS
                and not hour_crossed and not in_activation_window):
            return
        by_market = self.fetch_programs()

        def ticker_cutoff_passed(t: str) -> bool:
            # Cheap pre-filter: drop only when the ticker-embedded event DAY is
            # fully over. Same-day markets stay in — the schedule resolver may
            # grant them an intraday cutoff (quote until kickoff - buffer),
            # and the fallback midnight rule still kills them in _screen.
            # close-anchored series (hourly weather) ignore the ticker date
            # entirely; the closing/cutoff screens govern them instead.
            ov = series_override(series_of(t))
            if ov and ov.cutoff_from_close_min is not None:
                return False
            # Series whose ticker date is unreliable must reach the resolver
            # instead of being pre-dropped on the string: earnings-mention (the
            # ticker date is a Kalshi estimate, often stale) and any event with
            # a manual override. The resolver + _screen enforce the real cutoff.
            if series_of(t).startswith(_EARNINGS_PREFIX) \
                    or t.rsplit("-", 1)[0] in EVENT_START_OVERRIDES:
                return False
            # Schedule-API series: a postponed game moves past the ticker
            # date while its programs keep paying — never pre-drop on the
            # string; the resolver's live schedule + _screen govern.
            if series_of(t) in SCHEDULE_RESOLVED_SERIES:
                return False
            td = parse_event_date(t)
            return td is not None and now_utc >= td + timedelta(hours=24)

        candidates = sorted(
            ((t, info) for t, info in by_market.items()
             if self._allowed(t) and info["dollars_per_day"] > 0
             and not ticker_cutoff_passed(t)),
            key=lambda kv: -kv[1]["dollars_per_day"])[:MAX_CANDIDATE_BOOKS]

        metas: List[MarketMeta] = []
        tickers = [t for t, _info in candidates]
        markets: Dict[str, dict] = {}
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            try:
                resp = self.client.get_markets(tickers=",".join(chunk), limit=len(chunk))
                for m in (resp.get("markets") or []):
                    markets[m.get("ticker", "")] = m
            except Exception as e:
                log(f"{self.tag} ! bulk market read failed ({e}); chunk skipped")

        for t, info in candidates:
            m = markets.get(t)
            if not m or m.get("status") not in ("active", "open"):
                continue
            event_ticker = m.get("event_ticker") or t.rsplit("-", 1)[0]
            series = t.split("-")[0]
            close_time = parse_iso_utc(m.get("close_time", ""))
            # Close-anchored series (hourly weather): the whole life is the
            # event, so the cutoff is close_time minus a toxicity buffer and
            # the event-start machinery below doesn't apply.
            ov_close = series_override(series)
            if ov_close and ov_close.cutoff_from_close_min is not None:
                cutoff = (close_time - timedelta(minutes=ov_close.cutoff_from_close_min)
                          if close_time is not None else None)
            else:
                # Real event start (statsapi / ESPN / fixed broadcast hour) beats
                # the midnight-ET ticker-date fallback: mention programs run
                # through game day, and the pre-broadcast daytime is safe rent.
                resolved = self.resolver.resolve(series, event_ticker)
                if resolved is not None:
                    ov = series_override(series)
                    buffer_min = (ov.start_buffer_min if ov and ov.start_buffer_min is not None
                                  else EVENT_START_BUFFER_MIN)
                    cutoff = resolved - timedelta(minutes=buffer_min)
                else:
                    cutoff = trade_cutoff_utc(
                        event_ticker, parse_iso_utc(m.get("occurrence_datetime", "")),
                        parse_iso_utc(m.get("expected_expiration_time", "")))
            # Hard per-series expiry floor (Love Island: 8:30pm ET on event day).
            # Independent of the resolver so nothing can rest past it.
            hard = series_hard_expiry_utc(series, event_ticker)
            if hard is not None:
                cutoff = hard if cutoff is None else min(cutoff, hard)
            bid = market_cents(m, "yes_bid")
            ask = market_cents(m, "yes_ask")
            try:
                volume = float(m.get("volume_fp") or m.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            meta = MarketMeta(
                ticker=t, event_ticker=event_ticker, series=series,
                dollars_per_day=info["dollars_per_day"], program_end=info["end"],
                target_size=info["target"], discount_factor=info["df"],
                cutoff=cutoff, close_time=close_time,
                mid_cents=((bid + ask) / 2.0 if bid and ask else None),
                spread_cents=((ask - bid) if bid and ask else None),
                volume=volume, status=m.get("status", ""),
                open_time=parse_iso_utc(m.get("open_time", "")))
            metas.append(meta)

        # Pass 1: hard screens (+ yield-to-human: never select a market the
        # user is trading manually — divergence vs our own book, a foreign
        # resting order, an active standoff, OR any manual footprint elsewhere
        # in the same EVENT).
        manual_evts = self.manual_events(positions)
        # Sticky membership = the live selection PLUS the persisted one (a
        # restart wipes state.selected — without the persisted set, every
        # restart stranded all in-flight accruals; observed 2026-07-21:
        # CHIH-2109-T75.99 quoted 12:04, dropped 12:06 after the task restart).
        prev_selected = set(self.state.selected) | self.state.sticky_prev
        screened: List[MarketMeta] = []
        skipped: Dict[str, int] = {}
        for meta in metas:
            t = meta.ticker
            quote_all = (series_override(meta.series) or SeriesOverride()).quote_all
            manual = abs(positions.get(t, 0.0) - self.pnl.pos.get(t, 0.0))
            foreign_n = self._foreign_resting.get(t, 0) if self.live else 0
            # quote_all markets yield ONLY on a live foreign order here (or an
            # active standoff from one); positions and sibling activity don't
            # stop the bot — the user wants full event coverage.
            if quote_all:
                manual_skip = bool(foreign_n) or t in self.state.manual_standoff
            else:
                manual_skip = (t in self.state.manual_standoff or foreign_n
                               or manual >= MANUAL_STANDOFF_CONTRACTS
                               or meta.event_ticker in manual_evts)
            if manual_skip:
                skipped["manual"] = skipped.get("manual", 0) + 1
                continue
            reason = self._screen(meta, now_utc)
            if (reason and t in prev_selected
                    and reason not in STICKY_DEATH_REASONS):
                # Sticky: ride out transient quality states on a market we
                # already started quoting (see STICKY_DEATH_REASONS note).
                screened.append(meta)
            elif reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                screened.append(meta)

        # Pass 2: reward yield per resting contract from each candidate's LIVE
        # book — the objective is incentive per contract-minute quoted, so a
        # $25/day pool with an empty near-touch beats a $145/day pool where
        # farmers already stack the qualification walk.
        own_by_ticker: Dict[str, List[Tuple[str, int, float]]] = {}
        if self.live:
            for o in self.state.ledger.values():
                own_by_ticker.setdefault(o.get("ticker", ""), []).append(
                    (o.get("book_side", "bid"), int(o.get("yes_price", 0)),
                     float(o.get("remaining_count", 0))))
        ranked: List[MarketMeta] = []
        for meta in screened:
            quote_all = (series_override(meta.series) or SeriesOverride()).quote_all
            if not self._estimate_candidate_yield(meta, own_by_ticker.get(meta.ticker, [])):
                if meta.ticker in prev_selected:
                    ranked.append(meta)   # sticky: transient book-read failure
                else:
                    skipped["book_unreadable"] = skipped.get("book_unreadable", 0) + 1
            elif quote_all or meta.ticker in prev_selected:
                # quote_all: user wants EVERY market of the event. Sticky:
                # ALL previously-quoted markets bypass the optimization
                # filters — a below-floor estimate or lost budget race
                # mid-life must not strand the accrual (the original sticky
                # patch only rescued pass-1 screen failures; markets passing
                # screens cleanly still fell to payout_floor/budget here).
                ranked.append(meta)
            elif meta.yield_per_contract <= 0:
                skipped["zero_yield"] = skipped.get("zero_yield", 0) + 1
            elif meta.est_dollars_per_day < MIN_EST_DOLLARS_PER_DAY:
                skipped["payout_floor"] = skipped.get("payout_floor", 0) + 1
            else:
                ranked.append(meta)
        # Mild stickiness so estimator jitter doesn't churn the selection.
        ranked.sort(key=lambda m: -m.yield_per_contract
                    * (1.15 if m.ticker in self.state.selected else 1.0))

        selected: Dict[str, MarketMeta] = {}
        collateral = 0.0
        # Inventory reserve: the bot's OWN open book consumes budget at a
        # 50c/contract estimate. Own-book positions, not account positions —
        # the fleet's and the user's manual inventory must not starve this
        # bot's budget.
        inv_reserve = sum(abs(v) for v in self.pnl.pos.values()) * 0.50

        def market_cost(meta: MarketMeta) -> float:
            if meta.mid_cents is None or meta.spread_cents is None:
                # Retained one-sided/pinned market: no readable mid; actual
                # resting there is ~1c-side scraps, so don't charge the budget.
                return 0.0
            bid = int(meta.mid_cents - meta.spread_cents / 2)
            ask = int(meta.mid_cents + meta.spread_cents / 2)
            worst = ladder_collateral_dollars(bid, ask, series_levels(meta.series))
            return worst * COLLATERAL_REALIZATION   # realistic, not worst-case

        # quote_all series (e.g. Love Island) are force-included: EVERY market
        # of the event, exempt from the yield ranking, MAX_MARKETS, and the
        # collateral budget (user decision 2026-07-12 — high incentive/minute,
        # one-day pools). They're taken first so they always fit.
        # Sticky retention is seeded FIRST: the budget and the event cap can
        # never evict a market the bot already started quoting (its accrual
        # rides to the market's natural end). Their events count toward the
        # event cap — they are genuinely open.
        selected_events: Set[str] = set()
        for meta in ranked:
            if meta.ticker in prev_selected:
                collateral += market_cost(meta)
                selected[meta.ticker] = meta
                selected_events.add(meta.event_ticker)
        forced = [m for m in ranked
                  if m.ticker not in selected
                  and (series_override(m.series) or SeriesOverride()).quote_all]
        for meta in forced:
            collateral += market_cost(meta)
            selected[meta.ticker] = meta
        forced_collateral = collateral
        # MAX_MARKETS bounds the number of distinct EVENTS, not markets: once an
        # event is opened (its best-yielding market clears the budget), EVERY
        # other screened market of that event is eligible too — no per-event
        # market cap (user rule 2026-07-13). The collateral budget is then the
        # only governor on how many of an event's markets actually rest.
        # Iterate ALL ranked (no break): a sibling of an already-open event must
        # stay reachable even after the event cap is hit.
        for meta in ranked:
            if meta.ticker in selected:
                continue
            new_event = meta.event_ticker not in selected_events
            if MAX_MARKETS > 0 and new_event and len(selected_events) >= MAX_MARKETS:
                continue
            cost = market_cost(meta)
            if collateral + cost + inv_reserve > COLLATERAL_BUDGET:
                skipped["budget"] = skipped.get("budget", 0) + 1
                continue
            collateral += cost
            selected[meta.ticker] = meta
            selected_events.add(meta.event_ticker)

        dropped = [t for t in self.state.selected if t not in selected]
        added = [t for t in selected if t not in self.state.selected]
        self.state.selected = selected
        # The persisted sticky set is consumed by this refresh: survivors are
        # in state.selected now; the rest died a natural death and must not be
        # resurrected by later refreshes (or grow the persist unboundedly).
        self.state.sticky_prev = set()
        self.state.universe_at = now_ts
        est_total = sum(m.est_dollars_per_day for m in selected.values())
        log(f"{self.tag} universe: {self.state.programs_count} program markets -> "
            f"{len(metas)} candidates -> {len(selected)} selected "
            f"across {len(selected_events)}"
            f"{('/' + str(MAX_MARKETS)) if MAX_MARKETS > 0 else ''} events "
            f"({len(forced)} forced quote-all @ ~${forced_collateral:.0f}, "
            f"total ~${collateral:.0f} ladder collateral, "
            f"${inv_reserve:.0f} inventory reserve); skips {skipped}")
        if added:
            log(f"{self.tag} + selected: {', '.join(sorted(added)[:8])}"
                + (" ..." if len(added) > 8 else ""))
        if dropped:
            log(f"{self.tag} - deselected: {', '.join(sorted(dropped)[:8])}"
                + (" ..." if len(dropped) > 8 else ""))

    def _estimate_candidate_yield(self, meta: MarketMeta,
                                  own_live: List[Tuple[str, int, float]]) -> bool:
        """Fill meta.est_frac / est_dollars_per_day / yield_per_contract from
        the candidate's live orderbook. Live mode with resting orders: score
        what is actually resting (it's already in the book). Otherwise: overlay
        the default ladder joined to the current external best. Returns False
        when the book can't be read."""
        try:
            ob = self.client.get_orderbook(ticker=meta.ticker)
        except Exception:
            return False
        yes_levels, no_levels = orderbook_levels(ob)
        if self.live and own_live:
            frac, _sides = estimate_reward_share(
                yes_levels, no_levels, own_live,
                meta.target_size, meta.discount_factor, own_in_book=True)
            n_contracts = sum(r for _s, _p, r in own_live)
        else:
            lv = series_levels(meta.series)
            side_max = series_side_max(meta.series)
            ext_bid, ext_ask = external_best(yes_levels, no_levels)
            quotes: List[Quote] = []
            if ext_bid is not None and ext_bid >= series_price_min(meta.series):
                quotes += build_side_ladder(meta.ticker, "bid", ext_bid, ext_ask,
                                            side_max, levels=lv)
            if ext_ask is not None and ext_ask <= series_price_max(meta.series):
                quotes += build_side_ladder(meta.ticker, "ask", ext_ask, ext_bid,
                                            side_max, levels=lv)
            if not quotes:
                meta.est_frac = meta.est_dollars_per_day = meta.yield_per_contract = 0.0
                return True
            overlay = [(q.book_side, q.price_cents, float(q.count)) for q in quotes]
            frac, _sides = estimate_reward_share(
                yes_levels, no_levels, overlay,
                meta.target_size, meta.discount_factor, own_in_book=False)
            n_contracts = sum(q.count for q in quotes)
        meta.est_frac = frac
        meta.est_dollars_per_day = frac * meta.dollars_per_day
        meta.yield_per_contract = \
            (meta.est_dollars_per_day / n_contracts) if n_contracts else 0.0
        return True

    def _settle_or_drop(self, t: str) -> None:
        """Own book says we hold `t`, the account's unsettled positions don't.
        Either the market settled (book the settlement trade at 0/100 through
        the P&L tracker) or the user manually offset our lot after a standoff
        (drop the stale accounting entry)."""
        own = self.pnl.pos.get(t, 0.0)
        try:
            m = (self.client.get_market(t) or {}).get("market") or {}
        except Exception as e:
            log(f"{self.tag} ! settle check failed for {t} ({e}); retry next cycle")
            return
        result = str(m.get("result") or "").lower()
        if result in ("yes", "no"):
            px = 100.0 if result == "yes" else 0.0
            self.pnl.on_fill(t, "yes", "sell" if own > 0 else "buy", abs(own), px)
            log(f"{self.tag} {t}: settled {result.upper()}; booked {own:+.0f} @ {px:.0f}c "
                f"(market realized ${self.pnl.realized.get(t, 0.0):+.2f})")
        else:
            log(f"{self.tag} {t}: own book {own:+.0f} but account flat and market "
                f"unsettled — manually offset; dropping stale entry")
            self.pnl.pos.pop(t, None)
            self.pnl.avg.pop(t, None)
        self.state.last_mark.pop(t, None)

    def _refresh_marks(self, marked_this_cycle: Set[str]) -> None:
        """Ensure every open own-book position has a usable YES-mid mark.
        Managed markets were marked from their orderbooks this cycle; anything
        else with inventory gets a bulk market read."""
        stale = [t for t, p in self.pnl.pos.items()
                 if abs(p) > 1e-9 and t not in marked_this_cycle]
        for i in range(0, len(stale), 50):
            chunk = stale[i:i + 50]
            try:
                resp = self.client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            except Exception as e:
                log(f"{self.tag} ! mark refresh failed ({e}); stale marks stand")
                return
            for m in (resp.get("markets") or []):
                t = m.get("ticker", "")
                bid, ask = market_cents(m, "yes_bid"), market_cents(m, "yes_ask")
                if bid and ask:
                    self.state.last_mark[t] = (bid + ask) / 2.0
                else:
                    lp = market_cents(m, "last_price")
                    if lp:
                        self.state.last_mark[t] = float(lp)

    def restore_orphan_metas(self, positions: Dict[str, float]) -> None:
        """After a restart, positions can exist on persisted known tickers
        that are no longer selected and whose MarketMeta died with the old
        process — without a meta they get no reduce-only management and are
        invisible to the event-cap accounting. Rebuild metas for them."""
        missing = [t for t in self.state.known_tickers
                   if abs(positions.get(t, 0.0)) >= REDUCE_ONLY_MIN_CONTRACTS
                   and t not in self.state.selected
                   and t not in self.state.managed_extra
                   # only restore inventory that is genuinely OURS — a manual
                   # position on a once-quoted market is the user's business
                   and abs(positions.get(t, 0.0) - self.pnl.pos.get(t, 0.0))
                   < MANUAL_STANDOFF_CONTRACTS]
        if not missing:
            return
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            try:
                resp = self.client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            except Exception as e:
                log(f"{self.tag} ! orphan meta read failed ({e}); retry next cycle")
                return
            for m in (resp.get("markets") or []):
                t = m.get("ticker", "")
                event_ticker = m.get("event_ticker") or t.rsplit("-", 1)[0]
                self.state.managed_extra[t] = MarketMeta(
                    ticker=t, event_ticker=event_ticker, series=t.split("-")[0],
                    dollars_per_day=0.0, program_end=None, target_size=0.0,
                    discount_factor=0.5,
                    cutoff=trade_cutoff_utc(
                        event_ticker, parse_iso_utc(m.get("occurrence_datetime", "")),
                        parse_iso_utc(m.get("expected_expiration_time", ""))),
                    close_time=parse_iso_utc(m.get("close_time", "")))
                log(f"{self.tag} restored orphan position market {t} "
                    f"(pos {positions.get(t, 0):+.0f}, reduce-only)")

    @staticmethod
    def _event_of(ticker: str) -> str:
        return ticker.rsplit("-", 1)[0]

    def manual_events(self, positions: Dict[str, float]) -> Set[str]:
        """Events the user is trading by hand, from LIVE SIGNALS ONLY: any
        market of the event with a manual position (|account − own book| ≥
        threshold) or a foreign (non-imm) resting order. The bot avoids EVERY
        market of these events, so it never rests where the user's crossing
        order could hit it. Deliberately excludes the standoff set itself
        (downstream state) so a market releases once the real signal clears."""
        events: Set[str] = set()
        if not EVENT_LEVEL_STANDOFF:
            return events

        def quote_all(t: str) -> bool:
            return (series_override(series_of(t)) or SeriesOverride()).quote_all

        for t, v in positions.items():
            # quote_all series (Love Island): the user explicitly wants the bot
            # in these events, so his POSITIONS there don't yield — only a live
            # order on a specific market does (handled per-market below).
            if quote_all(t):
                continue
            if abs(v - self.pnl.pos.get(t, 0.0)) >= MANUAL_STANDOFF_CONTRACTS:
                events.add(self._event_of(t))
        if self.live:
            for t, n in self._foreign_resting.items():
                if n and not quote_all(t):
                    events.add(self._event_of(t))
        return events

    def _screen(self, meta: MarketMeta, now_utc: datetime) -> Optional[str]:
        """Hard screens; returns a skip-reason or None if quotable."""
        now_ts = now_utc.timestamp()
        if meta.cutoff is not None and now_utc >= meta.cutoff - timedelta(minutes=5):
            return "cutoff"
        # A mention/broadcast market with NO derivable event window is either
        # tournament-wide (e.g. KXWCMENTION-MENWORLDCUP: broadcasts already
        # running daily — quoting it is quoting through live events) or
        # unparseable. Both mean: no defined safe period, stand down.
        if meta.cutoff is None and \
                any(meta.series.endswith(suf) for suf in ALLOW_SERIES_SUFFIXES):
            return "no_event_window"
        ov = series_override(meta.series)
        if ov and ov.cutoff_from_close_min is not None and meta.close_time is None:
            return "no_event_window"   # close-anchored series with no close time
        min_hours = (ov.min_hours_to_close if ov and ov.min_hours_to_close is not None
                     else MIN_HOURS_TO_CLOSE)
        if meta.close_time is not None and \
                (meta.close_time - now_utc).total_seconds() < min_hours * 3600:
            return "closing"
        if meta.program_end is not None and now_utc >= meta.program_end:
            return "program_over"
        if meta.mid_cents is None or meta.spread_cents is None:
            return "one_sided"
        if meta.spread_cents > MAX_JOIN_SPREAD_CENTS:
            return "wide"
        if not (MID_BAND_LO <= meta.mid_cents <= MID_BAND_HI):
            return "extreme_mid"
        if meta.volume < MIN_VOLUME_CONTRACTS:
            # Fresh listings get a pass: mention markets list the day before
            # the game with zero volume, and that pre-event window is exactly
            # the rent we're here for. The anti-junk intent of the volume
            # screen only applies to markets old enough to have traded.
            age_ok = (meta.open_time is not None
                      and (now_utc - meta.open_time).total_seconds() < 24 * 3600)
            if not age_ok:
                return "no_volume"
        if self.state.breaker_until.get(meta.ticker, 0) > now_ts:
            return "breaker"
        if self.state.bench_until.get(meta.ticker, 0) > now_ts:
            return "benched"
        if meta.target_size <= 0:
            return "no_target"
        return None

    # ---- one polling cycle ---------------------------------------------------

    def _reconcile_orphaned_fills(self, positions: Dict[str, float]) -> None:
        """One-time post-restart cleanup for fills orphaned by an unclean
        shutdown: the bot placed an order, the process was hard-killed before
        the order_id/fill was persisted, so on restart the account holds a
        position the bot's own-book doesn't know — which then reads as "manual"
        and yields the whole event forever (observed 2026-07-14: an orphaned
        +5 LENO fill kept the entire ENGARG event yielded).

        Adopt those positions as the bot's own, scoped HARD so it can NEVER
        claim the user's manual book: (1) only markets the bot actually quotes
        (known_tickers) — the user trades MENWORLDCUP (tournament-wide, no
        window) and big crypto by hand, none of which are quoted; (2) only
        within the per-market position cap — a >cap position can't be ours.
        Runs once, on the first trusted (non-grace) cycle; afterwards the normal
        yield-to-human standoff handles genuinely NEW manual activity."""
        adopted = []
        for t in sorted(self.state.known_tickers):
            acct = positions.get(t, 0.0)
            own = self.pnl.pos.get(t, 0.0)
            if abs(acct - own) < MANUAL_STANDOFF_CONTRACTS:
                continue                      # below standoff threshold: harmless
            if abs(acct) > series_max_position(series_of(t)):
                continue                      # too big to be ours -> it's manual
            # Entry cost of the orphaned delta is unrecoverable, so mark the
            # whole adopted position at the current YES mid: ~0 unrealized now,
            # clean forward P&L. Skips (leaves as-is) if the book can't be read.
            try:
                m = (self.client.get_market(t) or {}).get("market") or {}
                bid = market_cents(m, "yes_bid")
                ask = market_cents(m, "yes_ask")
                mid = (bid + ask) / 2.0 if (bid and ask) else None
            except Exception:
                mid = None
            if mid is None:
                mid = self.pnl.avg.get(t) or 50.0
            adopted.append(f"{t} {own:+.1f}->{acct:+.0f}@{mid:.0f}c")
            self.pnl.pos[t] = acct
            self.pnl.avg[t] = mid
        if adopted:
            log(f"{self.tag} startup reconcile: adopted {len(adopted)} orphaned "
                f"own-fill position(s) (unclean-shutdown cleanup): "
                f"{', '.join(adopted[:8])}" + (" ..." if len(adopted) > 8 else ""))
            self.alerter.alert(
                "reconcile", f"adopted {len(adopted)} orphaned own-fill "
                f"position(s) on restart: {', '.join(adopted[:6])}",
                key="reconcile", urgent=False)
            self._save_persist()

    def run_cycle(self) -> None:
        now_utc = datetime.now(timezone.utc)
        now_ts = now_utc.timestamp()

        if os.path.exists(HALT_FILE):
            n = self.cancel_all_bot_orders()
            log(f"{self.tag} HALT file present ({HALT_FILE}); cancelled {n}; idle")
            return
        if self.state.halted_until > now_ts:
            log(f"{self.tag} daily-loss halt active until "
                f"{datetime.fromtimestamp(self.state.halted_until, timezone.utc)}; idle")
            return

        # Fills -> own book FIRST, before the positions read. Matched by ORDER
        # OWNERSHIP (our persisted order ids): the other bots AND the user's
        # manual trades share this account and must not pollute our P&L.
        # Ordering is load-bearing: booking fills AFTER fetch_positions left a
        # window where the account showed a fresh fill our own book didn't
        # know yet — the manual-divergence check then mistook the bot's OWN
        # fill for the user trading and dumped the whole event's quotes
        # (observed live 2026-07-14: a 5-lot TRUM fill deselected all 14
        # LATENIGHT markets and stray-cancelled 59 resting orders).
        for f in self.fetch_new_fills():
            try:
                side, action = f.get("side"), f.get("action")
                count = float(f.get("count_fp") or f.get("count") or 0)
                px = f.get("yes_price_dollars")
                px_cents = float(px) * 100 if px is not None else float(f.get("yes_price") or 0)
                if side in ("yes", "no") and action in ("buy", "sell") and count > 0:
                    self.pnl.on_fill(f.get("ticker", "?"), side, action, count, px_cents)
                    self.state.fills_today += count
            except Exception as e:
                log(f"{self.tag} ! unparseable fill skipped: {e}")

        positions = self.fetch_positions()
        # Post-wake grace: reads right after a sleep can SUCCEED with garbage
        # (network up, DNS not yet — partial positions/orders), which once
        # standoff-flagged 113/114 candidates as "manual". Freeze standoff
        # state changes and the universe selection until reads are trusted;
        # existing quotes keep being managed against the current selection.
        in_grace = now_ts < self.wake_grace_until
        if in_grace:
            log(f"{self.tag} wake grace: standoffs + universe frozen "
                f"({self.wake_grace_until - now_ts:.0f}s left)")
        # One-time orphaned-own-fill cleanup, BEFORE manual_events reads the
        # own-book — so an orphaned fill can't spend even one cycle masquerading
        # as manual. Deferred out of wake grace (positions must be trusted).
        # TWO-SHOT: fills can land on a killed run's not-yet-swept orders AFTER
        # the first pass reads positions (observed 2026-07-19: 3 orphans missed
        # because the kill hit mid-placement-wave), so re-run once ~4 min later;
        # the known_tickers + cap scoping makes the repeat adoption safe.
        if not self._reconciled and not in_grace and self.live:
            self._reconcile_orphaned_fills(positions)
            self._reconciled = True
            self._reconcile_recheck_at = now_ts + 240
        elif (self._reconciled and self._reconcile_recheck_at
                and now_ts >= self._reconcile_recheck_at
                and not in_grace and self.live):
            self._reconcile_recheck_at = 0.0
            self._reconcile_orphaned_fills(positions)
        # Release standoffs whose manual activity is gone — the market's own
        # divergence/foreign-order must be clear AND no sibling market of its
        # event is still manual (event-level yield). The yielded market never
        # re-enters the managed loop, so it must be released here.
        manual_evts = self.manual_events(positions)
        if not in_grace:
            for st in list(self.state.manual_standoff):
                manual = abs(positions.get(st, 0.0) - self.pnl.pos.get(st, 0.0))
                foreign_n = self._foreign_resting.get(st, 0) if self.live else 0
                if (manual < MANUAL_STANDOFF_CONTRACTS and not foreign_n
                        and self._event_of(st) not in manual_evts):
                    self.state.manual_standoff.pop(st, None)
                    log(f"{self.tag} {st}: manual activity cleared; market eligible again")
            self.refresh_universe(now_utc, positions)
        self.restore_orphan_metas(positions)

        # Managed set = selected + any market we still hold inventory in.
        managed: Dict[str, MarketMeta] = dict(self.state.selected)
        for t, meta in list(self.state.managed_extra.items()):
            if abs(positions.get(t, 0.0)) < REDUCE_ONLY_MIN_CONTRACTS:
                self.state.managed_extra.pop(t, None)
            elif t not in managed:
                managed[t] = meta
        for t in list(self.state.selected):
            # remember metas for later reduce-only management
            if abs(positions.get(t, 0.0)) >= REDUCE_ONLY_MIN_CONTRACTS:
                self.state.managed_extra[t] = self.state.selected[t]

        self.state.known_tickers |= set(managed)
        # Prune dead entries (unmanaged, flat) so the set stays bounded.
        self.state.known_tickers &= (set(managed) | set(positions))
        # Settle-or-drop own-book entries whose market vanished from the
        # unsettled-positions read. A settlement is REAL P&L that the fill
        # stream never reports — without booking it, a gapped position riding
        # to settlement is invisible to the loss halt (review-confirmed P0).
        for pt in list(self.pnl.pos):
            if abs(self.pnl.pos[pt]) > 1e-9 and pt not in positions and pt not in managed:
                self._settle_or_drop(pt)
        # (Fills were booked at the top of the cycle, before the positions
        # read — see the ordering note there.)
        # TODAY's realized P&L (lifetime minus the daily-roll baseline). The
        # LOSS HALT itself runs after the quoting loop, where fresh marks make
        # unrealized losses visible too — realized-only was blind to gapped
        # inventory riding to settlement (review-confirmed P0).
        realized = self.pnl.total_realized() - self.state.realized_baseline

        resting = self.fetch_resting_orders(now_ts)   # populates _foreign_resting
        own_by_ticker: Dict[str, List[Tuple[str, int, float]]] = {}
        for o in resting:
            parsed = order_yes_book_cents(o)
            if parsed is not None:
                own_by_ticker.setdefault(o.get("ticker", ""), []).append(
                    (parsed[0], parsed[1], order_remaining(o)))
        # Events the user is trading by hand THIS cycle (foreign orders now
        # fresh). The per-market loop below yields every market of these.
        manual_evts = self.manual_events(positions)

        # Cancel resting orders on markets we no longer manage at all.
        managed_set = set(managed)
        stray = [o for o in resting if o.get("ticker") not in managed_set]
        for o in stray:
            if self.cancel_order(o.get("order_id", "")):
                self.state.cancelled_today += 1
        if stray:
            resting = [o for o in resting if o.get("ticker") in managed_set]
            log(f"{self.tag} cancelled {len(stray)} stray orders on unmanaged markets")
        # Trackers for markets we no longer manage are stale — a months-old
        # prev_mid would fire a phantom move-breaker on re-selection.
        for d in (self.state.prev_mid, self.state.prev_pos, self.state.cutoff_ts):
            for t in [k for k in d if k not in managed_set]:
                d.pop(t, None)

        # Event exposure: net contracts per event across managed markets,
        # counted from the BOT'S OWN book — the user's manual positions
        # (on these or sibling markets of the event) are his risk budget,
        # not the bot's (user decision 2026-07-11).
        event_net: Dict[str, float] = {}
        for t, meta in managed.items():
            event_net[meta.event_ticker] = event_net.get(meta.event_ticker, 0.0) \
                + self.pnl.pos.get(t, 0.0)
        event_room_buy = {e: MAX_EVENT_CONTRACTS - n for e, n in event_net.items()}
        event_room_sell = {e: MAX_EVENT_CONTRACTS + n for e, n in event_net.items()}
        # Orders preserved on blind markets never enter `desired`, so charge
        # their resting size against the event budget up front — otherwise
        # sibling markets would hand out the room those orders may consume.
        blind_prev = {t for t, n in self.state.blind_streak.items() if n > 0}
        for o in resting:
            t = o.get("ticker", "")
            if t in blind_prev and t in managed:
                parsed = order_yes_book_cents(o)
                if parsed is None:
                    continue
                ev = managed[t].event_ticker
                if parsed[0] == "bid":
                    event_room_buy[ev] = event_room_buy.get(ev, MAX_EVENT_CONTRACTS) \
                        - order_remaining(o)
                else:
                    event_room_sell[ev] = event_room_sell.get(ev, MAX_EVENT_CONTRACTS) \
                        - order_remaining(o)
        # Per-event share: split remaining room across that event's quoted
        # markets, best-paying first (computed as we iterate).
        event_markets_left: Dict[str, int] = {}
        for t, meta in managed.items():
            event_markets_left[meta.event_ticker] = \
                event_markets_left.get(meta.event_ticker, 0) + 1

        desired: List[Quote] = []
        blind: Set[str] = set()
        marked: Set[str] = set()          # tickers marked from live books this cycle
        cycle_rows: List[str] = []        # cycle-logger panel (η/J calibration data)
        reward_frac_sum = 0.0
        quoted = 0
        # Round-robin across events (best-paying event first), NOT a flat
        # $/day sort: with many events managed, MAX_PLACEMENTS_PER_CYCLE would
        # be fully consumed by the top events every cycle and the tail (a
        # selected low-$/day event like AQI) never got reached — it stayed
        # selected but placed zero orders and earned nothing. Interleaving
        # guarantees every event's best markets are placed before any event's
        # deeper markets, so the per-cycle cap is shared fairly. Per-event
        # budget/share logic below is unchanged (it decrements per event).
        by_event: Dict[str, List[MarketMeta]] = {}
        for _m in managed.values():
            by_event.setdefault(_m.event_ticker, []).append(_m)
        for _grp in by_event.values():
            _grp.sort(key=lambda m: -m.dollars_per_day)
        _event_order = sorted(by_event, key=lambda e: -by_event[e][0].dollars_per_day)
        order_of_play: List[MarketMeta] = []
        _depth = 0
        while any(_depth < len(by_event[e]) for e in _event_order):
            for e in _event_order:
                if _depth < len(by_event[e]):
                    order_of_play.append(by_event[e][_depth])
            _depth += 1
        for meta in order_of_play:
            t = meta.ticker
            ev = meta.event_ticker
            pos = positions.get(t, 0.0)
            n_left = max(event_markets_left.get(ev, 1), 1)
            event_markets_left[ev] = n_left - 1
            share_buy = max(event_room_buy.get(ev, MAX_EVENT_CONTRACTS), 0.0) / n_left
            share_sell = max(event_room_sell.get(ev, MAX_EVENT_CONTRACTS), 0.0) / n_left

            # YIELD TO THE HUMAN: account position diverging from the bot's
            # own book, or a non-imm resting order here, means the user is
            # trading this market by hand — cancel our quotes and stand off
            # until the manual activity is gone. Without this, skew logic
            # would passively unwind his bets and STP would eat his orders.
            quote_all = (series_override(meta.series) or SeriesOverride()).quote_all
            own_pos = self.pnl.pos.get(t, 0.0)
            manual_pos = pos - own_pos
            foreign_n = self._foreign_resting.get(t, 0) if self.live else 0
            event_manual = (meta.event_ticker in manual_evts) and not quote_all
            pos_manual = (abs(manual_pos) >= MANUAL_STANDOFF_CONTRACTS) and not quote_all
            # quote_all (Love Island): yield a market ONLY when the user has a
            # live order resting on it (direct order-collision risk); his
            # positions and sibling markets never stop the bot there.
            if foreign_n or pos_manual or event_manual:
                if t not in self.state.manual_standoff:
                    if in_grace:
                        # Suspect post-wake read: don't record the standoff or
                        # cancel anything off it — skip this market this cycle
                        # and re-evaluate on trusted data. A REAL new manual
                        # conflict yields at most WAKE_GRACE_SECS later.
                        log(f"{self.tag} {t}: manual signal during wake grace "
                            f"(foreign={foreign_n}, manual pos {manual_pos:+.0f}); "
                            f"deferring judgement")
                        continue
                    self.state.manual_standoff[t] = now_ts
                    why = ("elsewhere in event " + meta.event_ticker) if event_manual \
                        and not (foreign_n or pos_manual) \
                        else f"foreign orders={foreign_n}, manual pos {manual_pos:+.0f}"
                    self.alerter.alert(
                        "manual_standoff",
                        f"{t}: manual activity detected ({why}); yielding",
                        key=t, urgent=False)
                n = self.cancel_market_orders(t, resting)
                if n:
                    log(f"{self.tag} {t}: cancelled {n} quotes, yielding to manual")
                self.state.selected.pop(t, None)
                self.state.managed_extra.pop(t, None)
                continue
            self.state.manual_standoff.pop(t, None)

            reduce_only = t not in self.state.selected
            # Registry for place_order: exchange-side expiration is capped at
            # the cutoff so resting orders cannot fill past event start.
            if meta.cutoff is not None:
                self.state.cutoff_ts[t] = meta.cutoff.timestamp()
            else:
                self.state.cutoff_ts.pop(t, None)
            if meta.cutoff is not None:
                if now_utc >= meta.cutoff:
                    n = self.cancel_market_orders(t, resting)
                    if n:
                        log(f"{self.tag} {t}: event-start cutoff passed; cancelled {n}")
                    self.state.selected.pop(t, None)
                    self.state.managed_extra.pop(t, None)
                    continue
                if (meta.cutoff - now_utc).total_seconds() \
                        < series_pre_cutoff_reduce_only_secs(meta.series):
                    reduce_only = True
            if meta.close_time is not None and \
                    (meta.close_time - now_utc).total_seconds() \
                    < series_min_hours_to_close(meta.series) * 3600:
                # Series-aware (hourly weather lives <1h; global default would
                # silently drop every KXTEMP market right after selection).
                self.cancel_market_orders(t, resting)
                self.state.selected.pop(t, None)
                self.state.managed_extra.pop(t, None)
                continue
            if self.state.breaker_until.get(t, 0) > now_ts:
                continue

            # Fill-burst breaker BEFORE quoting: a large move of OUR OWN book
            # in one cycle means someone is sweeping our ladder (news/insider).
            # Own-book delta, not account delta — the user's manual trades on a
            # nearby market must not false-alarm this.
            prev = self.state.prev_pos.get(t)
            if (BREAKERS_ENABLED and prev is not None
                    and abs(own_pos - prev) >= FILL_BURST_CONTRACTS):
                self.state.breaker_until[t] = now_ts + FILL_BURST_COOLDOWN_SECS
                n = self.cancel_market_orders(t, resting)
                self.alerter.alert(
                    "fill_burst", f"{t}: our book moved {own_pos - prev:+.0f} in one "
                    f"cycle; cancelled {n} orders, standing down "
                    f"{FILL_BURST_COOLDOWN_SECS // 60}min", key=t, urgent=False)
                self.state.prev_pos[t] = own_pos
                continue
            self.state.prev_pos[t] = own_pos

            try:
                ob = self.client.get_orderbook(ticker=t)
                yes_levels, no_levels = orderbook_levels(ob)
                self.state.blind_streak.pop(t, None)
            except Exception as e:
                streak = self.state.blind_streak.get(t, 0) + 1
                self.state.blind_streak[t] = streak
                if streak <= BLIND_PRESERVE_CYCLES:
                    blind.add(t)
                    log(f"{self.tag} ! orderbook failed for {t} ({e}); blind "
                        f"{streak}/{BLIND_PRESERVE_CYCLES}, preserving quotes")
                else:
                    log(f"{self.tag} ! blind on {t} {streak} cycles; cancelling quotes")
                continue

            own = own_by_ticker.get(t, [])
            # Dry-run sim orders were never sent, so they are NOT in the real
            # book — netting them out would erode phantom levels and walk our
            # anchor away from the true external best cycle after cycle.
            own_in_book = own if self.live else []
            ext_bid, ext_ask = external_best(yes_levels, no_levels, own_in_book)

            # A book that WAS two-sided and just lost a side is the classic
            # news signature (everyone pulled their quotes) — the mid-move
            # breaker can't see it because there is no mid anymore. Stand down.
            if BREAKERS_ENABLED and (ext_bid is None or ext_ask is None):
                pm = self.state.prev_mid.pop(t, None)
                if pm is not None:
                    self.state.breaker_until[t] = now_ts + BREAKER_COOLDOWN_SECS
                    n = self.cancel_market_orders(t, resting)
                    self.alerter.alert(
                        "one_sided", f"{t}: book went one-sided (was mid {pm:.0f}c); "
                        f"cancelled {n}, standing down "
                        f"{BREAKER_COOLDOWN_SECS // 60}min", key=t, urgent=False)
                    continue

            # Mid-move circuit breaker (external mid, so our own churn can't trip it).
            if ext_bid is not None and ext_ask is not None:
                mid = (ext_bid + ext_ask) / 2.0
                pm = self.state.prev_mid.get(t)
                self.state.prev_mid[t] = mid
                self.state.last_mark[t] = mid
                marked.add(t)
                if (BREAKERS_ENABLED and pm is not None
                        and abs(mid - pm) >= MID_MOVE_BREAKER_CENTS):
                    self.state.breaker_until[t] = now_ts + BREAKER_COOLDOWN_SECS
                    n = self.cancel_market_orders(t, resting)
                    self.alerter.alert(
                        "move_breaker", f"{t}: mid {pm:.0f}c -> {mid:.0f}c; cancelled {n}, "
                        f"standing down {BREAKER_COOLDOWN_SECS // 60}min", key=t, urgent=False)
                    continue
                if ext_bid >= ext_ask:      # crossed/locked external book: stay out
                    self.cancel_market_orders(t, resting)
                    continue
                if ext_ask - ext_bid > MAX_JOIN_SPREAD_CENTS:
                    self.cancel_market_orders(t, resting)
                    continue
            # TOP-IN-BAND (Jack 2026-07-21): if the top of book itself sits
            # outside the series price band, stand aside ENTIRELY — an in-band
            # rung under an out-of-band top (a 90c bid below a 92c touch) is
            # the same knife the band exists to dodge. Sticky keeps such a
            # market SELECTED (costless), so quoting resumes if the book
            # returns to the band.
            pmin_s = series_price_min(series_of(t))
            pmax_s = series_price_max(series_of(t))
            if ((ext_bid is not None and not pmin_s <= ext_bid <= pmax_s)
                    or (ext_ask is not None and not pmin_s <= ext_ask <= pmax_s)):
                self.cancel_market_orders(t, resting)
                continue

            # Rooms: hard per-market cap, event share, then skew — all using
            # this market's SERIES ladder/cap (Love Island runs a bigger flat
            # 5/5/5 ladder with a tighter 50-contract net cap). For quote_all
            # series the cap/skew track the bot's OWN book, so the user's
            # coexisting manual position doesn't shrink the bot's room.
            lv = series_levels(meta.series)
            side_max = series_side_max(meta.series)
            maxpos = series_max_position(meta.series)
            cap_pos = own_pos if quote_all else pos
            room_buy = min(maxpos - cap_pos, share_buy, side_max)
            room_sell = min(maxpos + cap_pos, share_sell, side_max)
            room_buy = skewed_side_room(room_buy, cap_pos, accumulating=cap_pos > 0,
                                        side_max=side_max)
            room_sell = skewed_side_room(room_sell, cap_pos, accumulating=cap_pos < 0,
                                         side_max=side_max)
            if reduce_only:
                # Only the reducing side, and never more than would flatten our
                # OWN book (a +5 position must not sell 35 and flip short 30).
                if own_pos > 0:
                    room_buy = 0.0
                    room_sell = min(room_sell, own_pos)
                elif own_pos < 0:
                    room_sell = 0.0
                    room_buy = min(room_buy, -own_pos)
                else:
                    room_buy = room_sell = 0.0

            mq: List[Quote] = []
            if ext_bid is not None and room_buy > 0:
                px_ok = ext_bid >= series_price_min(meta.series)
                if px_ok:
                    mq.extend(build_side_ladder(t, "bid", ext_bid, ext_ask, room_buy,
                                                levels=lv))
            if ext_ask is not None and room_sell > 0:
                px_ok = ext_ask <= series_price_max(meta.series)
                if px_ok:
                    mq.extend(build_side_ladder(t, "ask", ext_ask, ext_bid, room_sell,
                                                levels=lv))

            # Depth padding: on a side we're actually quoting whose total depth
            # is below the reward target, add throwaway contracts at the 1c/99c
            # mark so the whole side (and thus our near-touch ladder) qualifies.
            if series_pad_to_target(meta.series) and meta.target_size > 0:
                mq.extend(self._pad_quotes(t, mq, yes_levels, no_levels,
                                           own, meta.target_size))

            desired.extend(mq)
            if mq:
                quoted += 1
                bought = sum(q.count for q in mq if q.book_side == "bid")
                sold = sum(q.count for q in mq if q.book_side == "ask")
                event_room_buy[ev] = event_room_buy.get(ev, MAX_EVENT_CONTRACTS) - bought
                event_room_sell[ev] = event_room_sell.get(ev, MAX_EVENT_CONTRACTS) - sold

            # Reward-share estimate for status/summary + zero-share benching.
            # Live: our resting orders are already inside the fetched book.
            # Dry: overlay the DESIRED ladder (sim resting ~= desired; using
            # both would double-count).
            est_own = own if self.live else \
                [(q.book_side, q.price_cents, float(q.count)) for q in mq]
            frac, sides = estimate_reward_share(
                yes_levels, no_levels, est_own,
                meta.target_size, meta.discount_factor, own_in_book=self.live)
            reward_frac_sum += frac * meta.dollars_per_day
            cycle_rows.append(
                f"{now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')},{t},"
                f"{ext_bid if ext_bid is not None else ''},"
                f"{ext_ask if ext_ask is not None else ''},"
                f"{sum(q for _p, q in yes_levels):.0f},"
                f"{sum(q for _p, q in no_levels):.0f},"
                f"{meta.target_size:.0f},{frac:.5f},{sides},"
                f"{pos:.1f},{own_pos:.1f},{meta.dollars_per_day:.2f},"
                f"{sum(q.count for q in mq)}\n")
            if t in self.state.selected and not reduce_only:
                if frac <= 0.0 and mq:
                    streak = self.state.zero_share_streak.get(t, 0) + 1
                    self.state.zero_share_streak[t] = streak
                    if streak >= QUALIFY_PATIENCE_CYCLES:
                        self.state.bench_until[t] = now_ts + BENCH_COOLDOWN_SECS
                        self.state.zero_share_streak.pop(t, None)
                        self.cancel_market_orders(t, resting)
                        self.state.selected.pop(t, None)
                        desired = [q for q in desired if q.ticker != t]
                        self.alerter.alert(
                            "benched", f"{t}: zero reward share {QUALIFY_PATIENCE_CYCLES} "
                            f"cycles (book below target size?); benched "
                            f"{BENCH_COOLDOWN_SECS // 3600}h", key=t, urgent=False)
                else:
                    self.state.zero_share_streak.pop(t, None)

        # Reward accrual estimate between cycles (share x $/day x dt), plus the
        # objective's denominator: contract-minutes actually resting.
        resting_contracts = sum(order_remaining(o) for o in resting)
        if self.state.reward_accrue_at:
            dt_days = (now_ts - self.state.reward_accrue_at) / 86400.0
            accrued = reward_frac_sum * dt_days
            self.state.reward_est_today += accrued
            self.state.reward_est_lifetime += accrued
            self.state.contract_minutes_today += \
                resting_contracts * (now_ts - self.state.reward_accrue_at) / 60.0
        self.state.reward_accrue_at = now_ts

        # LOSS HALT on TOTAL P&L today (realized + mark-to-market), so gapped
        # inventory counts even before it settles. Runs before any placement.
        self._refresh_marks(marked)
        unrealized = self.pnl.unrealized(self.state.last_mark)
        unmarked = [t for t, p in self.pnl.pos.items()
                    if abs(p) > 1e-9 and t not in self.state.last_mark]
        if unmarked:
            log(f"{self.tag} ! {len(unmarked)} position(s) unmarkable (valued at "
                f"cost): {', '.join(unmarked[:5])}")
        total_pnl = self.pnl.total_realized() + unrealized
        if self.state.day_baseline is None:
            self.state.day_baseline = total_pnl
        pnl_today = total_pnl - self.state.day_baseline
        if pnl_today <= -DAILY_LOSS_LIMIT:
            next_day_et = (now_utc.astimezone(ET) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            self.state.halted_until = next_day_et.astimezone(timezone.utc).timestamp()
            n = self.cancel_all_bot_orders()
            self.alerter.alert(
                "loss_halt", f"P&L today ${pnl_today:.2f} (realized ${realized:.2f}, "
                f"unrealized ${unrealized:+.2f} on "
                f"{self.pnl.inventory_contracts():.0f} carried) <= "
                f"-${DAILY_LOSS_LIMIT:.0f}; cancelled {n} orders, halted until "
                f"next ET day", key="loss_halt")
            self._write_cycle_log(cycle_rows)
            return

        to_place, to_cancel = diff_orders(desired, resting, self.state.order_ages,
                                          now_ts, preserve_tickers=blind)
        log(f"{self.tag} {quoted}/{len(managed)} mkts quoted, {len(resting)} resting, "
            f"{len(to_cancel)} cancel, {len(to_place)} place, "
            f"est ${reward_frac_sum:.2f}/day reward share, P&L today ${pnl_today:+.2f} "
            f"(real {realized:+.2f}/unreal {unrealized:+.2f}) "
            f"{'' if self.live else '[DRY RUN]'}")

        cancel_failures = 0
        cancelled_ids: Set[str] = set()
        for oid in to_cancel:
            if self.cancel_order(oid):
                self.state.cancelled_today += 1
                cancelled_ids.add(oid)
            else:
                cancel_failures += 1
        if cancel_failures:
            raise RuntimeError(f"{cancel_failures} cancel(s) failed")

        placed = self.place_with_caps(to_place, resting, cancelled_ids, now_ts)
        if placed:
            # Persist order-ids NOW, right after placing — not at end-of-cycle.
            # A hard-kill in the window between placement and the end-of-cycle
            # save orphaned the fills (the order_id never hit disk, so the
            # fill read back as "manual"). Saving here shrinks that window to
            # near zero; the startup reconcile mops up anything still missed.
            self._save_persist()
        self.state.placed_today += placed
        self.state.cycles_today += 1
        self.state.last_markets_line = (
            f"{quoted}/{len(managed)} mkts quoted ({len(desired)} quotes), "
            f"est ${reward_frac_sum:.2f}/day")
        self._write_cycle_log(cycle_rows)

    CYCLE_LOG_HEADER = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,"
                        "est_frac,qual_sides,acct_pos,own_pos,pool_per_day,quoted\n")

    def _write_cycle_log(self, rows: List[str]) -> None:
        """Per-cycle book panel -> daily CSV. This is the jump-frequency /
        qualification-flap sensor the strategy's calibration runs on; every
        dry-run day without it is data thrown away. Never raises."""
        if not rows or os.environ.get("IMM_CYCLE_LOG", "1") != "1":
            return
        try:
            os.makedirs(STATUS_DIR, exist_ok=True)
            path = os.path.join(
                STATUS_DIR,
                f"cycle_log_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv")
            fresh = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if fresh:
                    f.write(self.CYCLE_LOG_HEADER)
                f.writelines(rows)
        except Exception as e:
            log(f"{self.tag} ! cycle log write failed: {e}")

    @staticmethod
    def _is_pad_price(book_side: str, px: int) -> bool:
        return (book_side == "bid" and px == PAD_BID_CENTS) or \
               (book_side == "ask" and px == PAD_ASK_CENTS)

    def _pad_quotes(self, ticker: str, near_touch: List[Quote],
                    yes_levels: List[List[float]], no_levels: List[List[float]],
                    own: List[Tuple[str, int, float]], target: float) -> List[Quote]:
        """Throwaway depth-padding at the 1c/99c mark so each side we quote
        reaches the reward target size. Computed on depth EXCLUDING our own pad
        (netted out / not-yet-in-book) to avoid a self-referential churn loop.
        Only pads a side we actually have near-touch quotes on (join-don't-lead
        preserved; leading the book alone at 1c is never useful)."""
        pads: List[Quote] = []
        nt_bid = sum(q.count for q in near_touch if q.book_side == "bid")
        nt_ask = sum(q.count for q in near_touch if q.book_side == "ask")
        own_pad_bid = sum(r for bs, px, r in own if self._is_pad_price(bs, px)
                          and bs == "bid")
        own_pad_ask = sum(r for bs, px, r in own if self._is_pad_price(bs, px)
                          and bs == "ask")
        yes_depth = sum(sz for _px, sz in yes_levels)
        no_depth = sum(sz for _px, sz in no_levels)
        if nt_bid > 0:
            # live: book already holds our near-touch, subtract only our pad;
            # dry: sim orders aren't in the book, add this cycle's near-touch.
            basis = (yes_depth - own_pad_bid) if self.live else (yes_depth + nt_bid)
            n = pad_quantity(basis, target)
            if n > 0:
                pads.append(Quote(ticker, "bid", PAD_BID_CENTS, n, is_pad=True))
        if nt_ask > 0:
            basis = (no_depth - own_pad_ask) if self.live else (no_depth + nt_ask)
            n = pad_quantity(basis, target)
            if n > 0:
                pads.append(Quote(ticker, "ask", PAD_ASK_CENTS, n, is_pad=True))
        return pads

    def place_with_caps(self, to_place: List[Quote], resting: List[dict],
                        cancelled_ids: Set[str], now_ts: float) -> int:
        """Unconditional backstops: per-(market,side) resting cap, per-level
        cap, global resting-order cap, per-cycle placement cap. Side/level
        caps are per the quote's SERIES ladder (Love Island is bigger). Pad
        orders (1c/99c depth fillers) are exempt from the ladder caps."""
        side_totals: Dict[Tuple[str, str], float] = {}
        level_totals: Dict[Tuple[str, str, int], float] = {}
        total_resting = 0
        for o in resting:
            if o.get("order_id") in cancelled_ids:
                continue
            parsed = order_yes_book_cents(o)
            if parsed is None:
                continue
            rem = order_remaining(o)
            total_resting += 1
            if self._is_pad_price(parsed[0], parsed[1]):
                continue   # pad orders don't count against the ladder caps
            skey = (o.get("ticker", ""), parsed[0])
            side_totals[skey] = side_totals.get(skey, 0.0) + rem
            lkey = (o.get("ticker", ""), parsed[0], parsed[1])
            level_totals[lkey] = level_totals.get(lkey, 0.0) + rem
        # Drop expired place-uncertainty cooldowns.
        for key, ts in list(self.state.place_uncertain.items()):
            if now_ts - ts > 2 * POLL_SECS:
                self.state.place_uncertain.pop(key, None)
        placed = 0
        for q in to_place:
            if (q.ticker, q.book_side, q.price_cents) in self.state.place_uncertain:
                continue   # a lost-response order may already rest at this level
            if placed >= MAX_PLACEMENTS_PER_CYCLE:
                log(f"{self.tag} placement cap {MAX_PLACEMENTS_PER_CYCLE}/cycle reached; "
                    f"{len(to_place) - placed} deferred to next cycle")
                break
            if total_resting >= MAX_TOTAL_RESTING_ORDERS:
                self.alerter.alert("order_cap", f"global resting-order cap "
                                   f"{MAX_TOTAL_RESTING_ORDERS} reached", key="order_cap",
                                   urgent=False)
                break
            if q.is_pad:
                # Depth filler: exempt from the ladder side/level caps (that's
                # its whole purpose), but still counts toward the global
                # resting-order and per-cycle placement caps above.
                if self.place_order(q, now_ts):
                    total_resting += 1
                    placed += 1
                continue
            series = series_of(q.ticker)
            side_cap = series_side_max(series)
            max_level_size = max(s for _t, s in series_levels(series))
            skey = (q.ticker, q.book_side)
            lkey = (q.ticker, q.book_side, q.price_cents)
            have_side = side_totals.get(skey, 0.0)
            have_level = level_totals.get(lkey, 0.0)
            if have_side + q.count > side_cap + 0.01:
                self.alerter.alert("side_cap", f"{q.ticker} {q.book_side}: blocked at "
                                   f"{have_side:.0f}/{side_cap}",
                                   key=f"{q.ticker}-{q.book_side}", urgent=False)
                continue
            if have_level + q.count > max_level_size + 0.01:
                self.alerter.alert("level_cap", f"{q.ticker} {q.book_side}@{q.price_cents}c: "
                                   f"blocked at {have_level:.0f}/{max_level_size}",
                                   key=f"{q.ticker}-{q.price_cents}", urgent=False)
                continue
            if self.place_order(q, now_ts):
                side_totals[skey] = have_side + q.count
                level_totals[lkey] = have_level + q.count
                total_resting += 1
                placed += 1
        return placed

    # ---- status / summary ----------------------------------------------------

    def write_status(self, now_utc: datetime) -> None:
        s = self.state
        cats: Dict[str, int] = {}
        for cat, _msg in self.alerter.today:
            cats[cat] = cats.get(cat, 0) + 1
        status = {
            "bot": "incentive_mm",
            "mode": "LIVE" if self.live else "DRY",
            "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "selected": len(s.selected),
            "managed_extra": len(s.managed_extra),
            "manual_standoff": sorted(s.manual_standoff),
            "programs_seen": s.programs_count,
            "markets_line": s.last_markets_line,
            "reward_est_today": round(s.reward_est_today, 2),
            "reward_est_lifetime": round(s.reward_est_lifetime, 2),
            "contract_minutes_today": round(s.contract_minutes_today),
            "cents_per_1k_contract_min": round(
                100000 * s.reward_est_today / s.contract_minutes_today, 2)
            if s.contract_minutes_today else 0,
            "realized_today": round(self.pnl.total_realized() - s.realized_baseline, 2),
            "realized_lifetime": round(self.pnl.total_realized(), 2),
            "unrealized_mtm": round(self.pnl.unrealized(s.last_mark), 2),
            "inventory_contracts": round(self.pnl.inventory_contracts(), 1),
            "pnl_today": round(self.pnl.total_realized()
                               + self.pnl.unrealized(s.last_mark)
                               - (s.day_baseline or 0.0), 2),
            "fills_today": s.fills_today,
            "cycles_today": s.cycles_today,
            "placed_today": s.placed_today,
            "cancelled_today": s.cancelled_today,
            "errors_today": s.errors_today,
            "alerts_today": cats,
            "halted_until": s.halted_until,
            "summary_date": str(self.alerter.last_summary_date or ""),
            "summary_body": self.alerter.last_summary_body or "",
        }
        try:
            os.makedirs(STATUS_DIR, exist_ok=True)
            path = os.path.join(STATUS_DIR, "status_incentive_mm.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f)
            os.replace(tmp, path)
        except Exception as e:
            log(f"{self.tag} ! status write failed: {e}")
        if self.live:   # dry-run markets must not contaminate the live "ours" set
            self._save_persist()

    def build_daily_summary(self) -> str:
        s = self.state
        alerts = self.alerter.today
        if alerts:
            cats: Dict[str, int] = {}
            for cat, _msg in alerts:
                cats[cat] = cats.get(cat, 0) + 1
            alert_str = " ".join(f"{c}x{n}" for c, n in sorted(cats.items()))
        else:
            alert_str = "none"
        top_pos = sorted(((t, p) for t, p in self.pnl.pos.items() if abs(p) > 0.5),
                         key=lambda kv: -abs(kv[1]))[:5]
        pos_str = ", ".join(f"{t} {p:+.0f}" for t, p in top_pos) or "flat"
        realized_today = self.pnl.total_realized() - s.realized_baseline
        unrealized = self.pnl.unrealized(s.last_mark)
        inv = self.pnl.inventory_contracts()
        cm = s.contract_minutes_today
        eff = f", {100000 * s.reward_est_today / cm:.1f}c/1k-contract-min" if cm else ""
        body = (f"incentive_mm daily ({'LIVE' if self.live else 'DRY'}): "
                f"{s.last_markets_line} | est reward today ${s.reward_est_today:.2f} "
                f"({cm:,.0f} contract-min{eff}) | "
                f"realized today ${realized_today:+.2f} (lifetime "
                f"${self.pnl.total_realized():+.2f}), unrealized ${unrealized:+.2f} "
                f"on {inv:.0f} carried, fills {s.fills_today:.0f} | "
                f"inventory: {pos_str} | cycles {s.cycles_today}, placed {s.placed_today}, "
                f"cxl {s.cancelled_today}, errs {s.errors_today} | alerts: {alert_str}")
        s.cycles_today = s.placed_today = s.cancelled_today = s.errors_today = 0
        s.fills_today = 0.0
        s.reward_est_today = 0.0
        s.contract_minutes_today = 0.0
        # roll both loss-halt windows
        s.realized_baseline = self.pnl.total_realized()
        s.day_baseline = self.pnl.total_realized() + unrealized
        return body

    # ---- main loop -----------------------------------------------------------

    def shutdown_cancel(self) -> None:
        if self._shutdown_done or not self.live:
            return
        self._shutdown_done = True
        try:
            n = self.cancel_all_bot_orders()
            log(f"{self.tag} shutdown: cancelled {n} resting bot orders")
        except Exception as e:
            log(f"{self.tag} ! shutdown cancel failed: {e} "
                f"(orders TTL-expire within {ORDER_TTL_SECS}s)")

    def run(self, once: bool = False) -> None:
        mode = "LIVE" if self.live else "DRY RUN (no orders placed; --live to trade)"
        log(f"=== {MODEL_VERSION} run={RUN_ID} mode={mode} ===")
        log(f"ladder {LEVELS} per side ({SIDE_MAX_CONTRACTS}/side), "
            f"caps: market ±{MAX_POSITION_CONTRACTS:g}, event ±{MAX_EVENT_CONTRACTS:g}, "
            f"budget ${COLLATERAL_BUDGET:g}, "
            f"{('max ' + str(MAX_MARKETS) + ' events') if MAX_MARKETS > 0 else 'events uncapped'}, "
            f"TTL {ORDER_TTL_SECS}s, poll {POLL_SECS}s")

        if self.live:
            n = self.cancel_all_bot_orders()
            log(f"{self.tag} startup: cancelled {n} leftover imm- orders")

        stopping = {"flag": False}
        prev_top: Optional[float] = None

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
                _keep_awake()   # re-assert every cycle (wakes can clear it)
                if prev_top is not None and top - prev_top > WAKE_GAP_SECS:
                    self.wake_grace_until = top + WAKE_GRACE_SECS
                    log(f"{self.tag} resume detected ({top - prev_top:.0f}s gap); "
                        f"wake grace {WAKE_GRACE_SECS}s")
                prev_top = top
                try:
                    self.run_cycle()
                    self.state.consecutive_errors = 0
                except Exception as e:
                    now = time.time()
                    if now - top > WAKE_GAP_SECS:
                        self.wake_grace_until = now + WAKE_GRACE_SECS
                    self.state.errors_today += 1
                    if now < self.wake_grace_until:
                        log(f"{self.tag} ! cycle error during wake grace: {e!r}")
                    else:
                        self.state.consecutive_errors += 1
                        log(f"{self.tag} ! cycle error #{self.state.consecutive_errors}: {e!r}")
                        if self.state.consecutive_errors >= FAILSAFE_CANCEL_AFTER:
                            log(f"{self.tag} fail-safe: cancelling all resting orders")
                            try:
                                self.cancel_all_bot_orders()
                            except Exception as e2:
                                log(f"{self.tag} ! fail-safe cancel failed: {e2}")
                            self.alerter.alert(
                                "failsafe", f"{self.state.consecutive_errors} consecutive "
                                f"cycle errors (last: {e!r:.120}); cancelled all",
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
                self.alerter.alert("shutdown",
                                   f"bot stopped (run {RUN_ID}); resting orders "
                                   f"{'cancelled' if self.live else 'were simulated'}",
                                   key="shutdown")
        log("=== done ===")

    # ---- --status: selection preview (read-only) -----------------------------

    def print_status_table(self) -> None:
        now_utc = datetime.now(timezone.utc)
        positions = self.fetch_positions()
        self.state.universe_at = 0.0
        self.refresh_universe(now_utc, positions)
        rows = sorted(self.state.selected.values(), key=lambda m: -m.yield_per_contract)
        print(f"\n{'TICKER':44s} {'pool$/d':>7s} {'est$/d':>7s} {'$/d/ct':>7s} "
              f"{'mid':>4s} {'sprd':>4s} {'cutoff (UTC)':17s}")
        for m in rows:
            cut = m.cutoff.strftime("%m-%d %H:%M") if m.cutoff else "-"
            print(f"{m.ticker:44s} {m.dollars_per_day:7.2f} {m.est_dollars_per_day:7.2f} "
                  f"{m.yield_per_contract:7.3f} {(m.mid_cents or 0):4.0f} "
                  f"{(m.spread_cents or 0):4d} {cut:17s}")
        est_total = sum(m.est_dollars_per_day for m in rows)
        print(f"\n{len(rows)} markets selected of {self.state.programs_count} active "
              f"program markets; est ${est_total:.0f}/day share at rest")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def build_client() -> ExchangeClient:
    # Keep-alive for this process only (fleet/cloud bots opt in separately):
    # order waves ran ~2.5s/order, ~1s of which was a fresh TLS handshake per
    # call (measured 2026-07-19). Explicit env still wins.
    os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
    private_key = load_private_key()
    client = ExchangeClient(exchange_api_base=KALSHI_API_BASE,
                            key_id=KEY_ID, private_key=private_key)
    status = client.get_exchange_status()
    log(f"exchange status: {status}")
    return client


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="place real orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="single cycle, then exit")
    ap.add_argument("--status", action="store_true",
                    help="print the current market selection table, then exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel ALL resting imm- orders (always real), then exit")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert via the configured route, then exit")
    args = ap.parse_args(argv)

    if args.test_alert:
        alerter = Alerter("IMM", live=False)
        if not alerter.enabled:
            log("cannot test: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not set")
            return 1
        ok = alerter.send_message(f"[IMM] test alert from {MODEL_VERSION}",
                                  subject="IMM test alert")
        log(f"test alert: {'sent' if ok else 'FAILED'}")
        return 0 if ok else 1

    client = build_client()

    if args.cancel_all:
        bot = IncentiveMarketMaker(client, live=True)
        n = bot.cancel_all_bot_orders()
        log(f"cancelled {n} real resting imm- orders")
        return 0

    bot = IncentiveMarketMaker(client, live=args.live)
    if args.status:
        bot.print_status_table()
        return 0
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
