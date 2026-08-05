#!/usr/bin/env python3
"""
incentive_mm.py — Conservative market maker for Kalshi LIQUIDITY INCENTIVE markets.

Discovers every active liquidity incentive program via
GET /trade-api/v2/incentive_programs (period_reward is in CENTI-CENTS:
1,000,000 = $100), ranks markets by reward dollars per day, and rests
two-sided post-only ladders on the best-paying ones, subject to a collateral
budget and a stack of adverse-selection guards.

Reward mechanics (Kalshi Liquidity Incentive Program as AMENDED effective
2026-07-30; CFTC filing 2026-07-15): once per second a random-time book
snapshot is scored. On each side (YES bids and NO bids — a YES ask IS a NO
bid), levels are walked from the best price; the REFERENCE price is the
level where cumulative size first reaches target/5, and the walk continues
until cumulative size reaches the target (usually 1000). A snapshot is
EXCLUDED entirely unless BOTH sides reach target, and the period's payout
is scaled by (non-excluded / total snapshots). Each qualifying order scores
    DiscountFactor^max(ticks below REFERENCE, 0) x size    (factor 0.5)
so everything at or above the reference — typically the top ~target/5 of
the book, not just the touch — earns FULL weight pro-rata by size, and the
period reward is split by total score share. Strategic consequences vs the
pre-7/30 rules: tiny at-touch lots no longer out-score size resting in the
band; depth pads that lift a thin side to target also un-exclude the whole
snapshot; and being 1-2 ticks behind the touch but at/above the reference
costs nothing in weight (see shape sim v2).

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
import threading
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
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
# Ladder placement mode (2026-08-01, post-amendment shape sim v2):
#   offsets — legacy: rungs at fixed tick offsets behind the join anchor.
#   atref   — collapse the side's total size to ONE rung at the book's
#             REFERENCE level (cumulative depth >= target/5): the deepest
#             price that still scores FULL weight under the amended program
#             rules. Same estimated reward as all-at-touch (+/-1% on 90
#             live books), ~half the fill exposure, ~2/3 the collateral.
#             Falls back to the offsets ladder when the book has no
#             reference (too thin) — there, position IS the touch anyway.
LADDER_MODE = os.environ.get("IMM_LADDER_MODE", "offsets").strip().lower()
# Deep-reference size multiplier (Jack 2026-08-01: "the deeper the reference
# point, the larger contract size can be placed — it's much safer"): an
# at-ref rung N ticks behind the touch only fills after the market eats the
# whole band above it, so it carries more size for the same fill risk.
# 1.0 at the touch, +SLOPE per tick of depth, capped at MAX_MULT. Scales the
# atref rung AND the side_max room component; position/event caps and skew
# still bind on top. 0.1/2.0 -> 0.25/3.0 (Jack 2026-08-02 "raise the cap...
# run it through the sim first"): ref_cap_sim on 120 live books — deeper
# curves IMPROVE est-per-unit-fill-risk in every engaged bucket (4-5 ticks:
# 0.104 -> 0.124; 6+ ticks: 1.62 -> 2.21, est $223 -> $318/day on 13 books
# at +4% risk-load). The 0.40/4.0 curve tested even better (2.70, $406/day)
# — step there via env after a day of live fill data.
REF_DEPTH_SLOPE = _env_float("IMM_REF_DEPTH_SLOPE", 0.25)
REF_DEPTH_MAX_MULT = _env_float("IMM_REF_DEPTH_MAX_MULT", 3.0)
# Total-multiplier cap (Jack 2026-08-02): hour mult x ref mult <= this — the
# quiet-hours 2x and deep-ref 3x each claim an independent risk discount and
# their product claimed both at once (worst rung 20x2x3=120). The cap trims
# the REF contribution only (never below 1.0), via capped_ref_mult at every
# sizing site (ladder total, estimator meta, live side rooms). 0 = off.
TOTAL_SIZE_MULT_CAP = _env_float("IMM_TOTAL_SIZE_MULT_CAP", 5.0)
# Requote hysteresis for atref rungs (2026-08-02 audit): full weight is flat
# across the band, so churn on 1-tick touch/ref wiggles buys nothing.
# 1 -> 0 (Jack 2026-08-03: "sticky quotes that dont adjust the quote by 1c is
# making markets like this not earn rewards"). The 1-tick tolerance existed to
# avoid cancel+place churn, but amend-in-place (same day) reprices in ONE call
# with no book gap, so the churn argument is gone — while a rung left 1 tick
# below the reference earns HALF weight until its TTL. Temp already ran 0.
ATREF_PRICE_TOL_TICKS = _env_int("IMM_ATREF_PRICE_TOL", 0)
ATREF_COUNT_TOL_FRAC = _env_float("IMM_ATREF_COUNT_TOL", 0.2)
# Safe-join placement (Jack 2026-08-02, company/econ re-entry): on a tight
# book (spread < MIN_SPREAD ticks) quotes rest at least OFFSET ticks behind
# the touch — an "ample safety net" against informed flow; a wide spread is
# its own net, so >= MIN_SPREAD books join normally. Applied inside
# build_side_ladder (single site: live loop AND estimator inherit).
SAFE_JOIN_OFFSET_TICKS = _env_int("IMM_SAFE_JOIN_OFFSET", 2)
SAFE_JOIN_MIN_SPREAD = _env_int("IMM_SAFE_JOIN_MIN_SPREAD", 5)
# Fast-lane (Jack 2026-08-02): between full cycles, re-quote ONLY fast-lane
# series (KXTEMP — hourly books gap on METAR updates and the ~50s full-cycle
# cadence left rungs ticks behind the reference) every FAST_LANE_SECS of the
# main sleep window. A fast tick is a FILTERED run_cycle: full managed set +
# full resting read (stray-cancel, caps and the diff stay correct), non-fast
# markets skipped at the quote-loop head with their resting orders preserved
# through the diff; universe refresh, accrual integration, bench streaks,
# watchdog, cycle log and counters run on FULL cycles only. 0 = off.
FAST_LANE_SECS = _env_int("IMM_FAST_LANE_SECS", 5)


def ref_depth_mult(anchor: Optional[int], ref_px: Optional[int],
                   book_side: str) -> float:
    """Size multiplier for an at-ref rung by its depth behind the touch.
    1.0 whenever atref is off, inputs are missing, or the reference is at/
    above the anchor (tight book: placement IS the touch)."""
    if LADDER_MODE != "atref" or anchor is None or ref_px is None:
        return 1.0
    depth = (anchor - ref_px) if book_side == "bid" else (ref_px - anchor)
    if depth <= 0:
        return 1.0
    return min(REF_DEPTH_MAX_MULT, 1.0 + REF_DEPTH_SLOPE * depth)


def capped_ref_mult(anchor: Optional[int], ref_px: Optional[int],
                    book_side: str, hour_mult: float = 1.0) -> float:
    """ref_depth_mult with the TOTAL-multiplier cap applied: hour_mult (the
    factor already baked into the hour-scaled ladder) x the returned ref
    mult never exceeds TOTAL_SIZE_MULT_CAP. Trims the ref contribution only
    — never below 1.0, so an hour mult alone can still exceed the cap by
    deliberate env choice. THE single accessor for every sizing site."""
    m = ref_depth_mult(anchor, ref_px, book_side)
    if TOTAL_SIZE_MULT_CAP > 0 and hour_mult > 0:
        m = min(m, max(1.0, TOTAL_SIZE_MULT_CAP / hour_mult))
    return m


def clamp_side_max_to_position_cap(side_max_bid: int, side_max_ask: int,
                                   maxpos: float) -> Tuple[int, int]:
    """No ONE side may rest more contracts than the market's whole net
    position cap.

    The cap is enforced when orders are SIZED, against the position as read at
    the start of the cycle — it is not a hard limit at fill time. So the real
    bound is (position last seen) + (whatever a single re-place can carry),
    and when a side ladder is bigger than the cap itself that second term
    dominates.

    Live 2026-08-04, KXTEMPAUSH-26AUG0409-T79.99, cap 50: the bot was long
    +25, so sell room was 50+25=75, trimmed to the ladder's side max of 60
    (20/side x the 3.0 deep-reference multiplier) and it rested 60. 35 filled,
    taking the position to -10, and 28 seconds later — before that fill was
    observed — it re-placed a full 60 sized off the stale +25. That filled
    too: 25 - 35 - 60 = -70 against a 50 cap. The fill-burst breaker would
    have caught the 35-lot move, but IMM_BREAKERS defaults to 0.

    Clamping here does not make the cap hard (only a pre-place position
    re-read would), but it removes the case where ONE order exceeds the whole
    cap, which is what turned a 35-contract surprise into a 20-contract
    breach. Non-temp series are unaffected: 20/side x 3.0 = 60 sits well under
    their 150 cap.

    NOTE the collateral reservation (market_cost) still charges the unclamped
    ref multiplier, so it now over-reserves slightly on clamped markets. That
    is the safe direction and is left alone deliberately."""
    if maxpos <= 0:
        return side_max_bid, side_max_ask
    cap = int(maxpos)
    return min(side_max_bid, cap), min(side_max_ask, cap)


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
    cutoff_before_event_min: Optional[int] = None       # stop this many minutes
    #   BEFORE the ticker-date midnight (Jack 2026-07-29: rain dailies stop
    #   quoting 6pm ET the day before -> 360). Applied as a min() on top of
    #   whatever the resolver/ticker rule produced.
    cutoff_from_close_min: Optional[int] = None         # cutoff = close_time minus
    #   this many minutes, REPLACING the ticker-date/occurrence rules — for series
    #   whose whole life IS the event (hourly weather markets) and which the
    #   midnight-ET rule would otherwise kill on sight
    event_day_cutoff_et: Optional[Tuple[int, int]] = None  # (hour, minute) ET ON the
    #   event date, replacing the ticker-date MIDNIGHT rule — the only lever here
    #   that moves a cutoff LATER. For day-dated series with no scheduled release
    #   to dodge, where midnight is arbitrary (Treasuries: 7:30am ET, ahead of the
    #   8:30 data). Never overrides an occurrence-based cutoff; clamped to close.
    min_hours_to_close: Optional[float] = None          # override the global
    #   MIN_HOURS_TO_CLOSE screen (hourly markets live less than the 1h default)
    min_est_total: Optional[float] = None               # override the global
    #   MIN_EST_TOTAL_DOLLARS entry/hopeless floor — sub-hour temp windows make
    #   $1 a high bar (Jack 2026-08-02: flanking strikes measured $0.6-0.8)
    atref_price_tol_ticks: Optional[int] = None         # override the global
    #   ATREF_PRICE_TOL_TICKS requote hysteresis. 0 = reprice on ANY reference
    #   move (Jack 2026-08-02 for KXTEMP: hourly books gap on METAR updates and
    #   a 1-tick-behind rung earns half weight; temp is ~24 orders, churn cheap)
    fast_lane: bool = False                             # requote this series on
    #   the FAST_LANE_SECS mini-cycles between full cycles (KXTEMP)
    blackout_et: Optional[Tuple[str, str]] = None       # ("HH:MM","HH:MM") ET
    #   daily window in which the series is NOT quoted and resting orders are
    #   cancelled — for a recurring scheduled data release that reprices the
    #   book (AAA gas/diesel prints, observed 03:18-03:36 ET). Distinct from
    #   `cutoff`, which is a one-time end; a blackout recurs every day and the
    #   market resumes quoting afterwards.
    min_est_per_day: Optional[float] = None             # RATE floor: skip fresh
    #   candidates whose est $/day is below this (re-entry quality bar; on TOP
    #   of the total-accrual payout floor). None/0 = off.
    safe_join: bool = False                             # safety-net placement:
    #   rest >= SAFE_JOIN_OFFSET_TICKS behind the touch unless the bid/ask
    #   spread is >= SAFE_JOIN_MIN_SPREAD ticks (re-entry company/econ)
    pre_cutoff_reduce_only_secs: Optional[int] = None   # override the global
    #   PRE_CUTOFF_REDUCE_ONLY_SECS (default 0 since 2026-08-02 — the window
    #   is removed bot-wide; set >0 here or via env to restore per-series)
    price_min_cents: Optional[int] = None               # per-series order price
    price_max_cents: Optional[int] = None               #   band (else global)


# Depth-padding (pad_to_target): fill a thin side up to the reward target with
# cheap far-from-touch contracts, rounded up to the nearest PAD_ROUND. 1c bids /
# 99c asks cost ~1c collateral and ~1c max loss each; they earn ~0 (weight
# 0.5^(~47 ticks) ≈ 0) but unlock the near-touch ladder's rewards.
# Pad price history 2026-08-02/03: 1/99 -> 2/98 ("don't place at 1c/99c")
# -> BACK to 1/99 same night (Jack "Set pads back to 1/99"). At 1/99 pads
# sit outside the 2-98 RUNG envelope, so pad-vs-rung price classification
# is unambiguous again; the 5-90 pad_band_ok mid gate still applies.
PAD_BID_CENTS = _env_int("IMM_PAD_BID_CENTS", 1)
PAD_ASK_CENTS = _env_int("IMM_PAD_ASK_CENTS", 99)
PAD_ROUND = _env_int("IMM_PAD_ROUND", 100)
# 5000 -> 800 ("Max do 800") -> 1000 (Jack 2026-08-03 "allow pads to go up
# to 1k"): bounds each pad order's contracts — worst-case ~$10/side at 1c.
# At 1000 a pad can single-handedly satisfy the standard 1000 target when a
# side is completely empty (the 800 cap left near-empty sides unable to
# qualify at all).
PAD_MAX_CONTRACTS = _env_int("IMM_PAD_MAX", 1000)
# A pad must rest AT LEAST this many ticks behind its side's external touch
# (Jack 2026-08-03 "dont pad if 1c is the top of the book, i dont want pads
# to get bought up easily"): a 1c pad under a 1-2c best bid IS the market —
# first in line for any seller. Per-side; mirrored in the estimator.
PAD_MIN_TICKS_BEHIND = _env_int("IMM_PAD_MIN_BEHIND", 2)
# Headroom added on top of the exact gap whenever a pad is needed (Jack
# 2026-08-01: AUG0110-T82.99 yes side padded to exactly ~1000, then external
# retail depth withdrew and the side sat below target — disqualified — until
# the next requote resized. Slack keeps the side qualified through external
# churn between requotes, and damps pad resize churn (hysteresis via
# rounding). ~$3 collateral per pad at 1c/99c.
PAD_SLACK_CONTRACTS = _env_int("IMM_PAD_SLACK", 300)
# GLOBAL since 2026-07-29 (Jack: "some quotes are not earning because there
# are not 1000 orders on that side... fix this systematically — quote how
# many orders are necessary at the very bottom of the orderbook"): every
# quoted market pads its thin sides to the reward target. Pads price at
# 1c/99c deliberately OUTSIDE the 5-90 band — they are qualification depth,
# not quotes; built in _pad_quotes (never through build_side_ladder), so
# the band never clips them, and stood-down markets never reach the pad
# site. The candidate estimator models them too, else thin markets read
# est=0 and die at the floor before a pad could ever rest.
PAD_TO_TARGET_GLOBAL = os.environ.get("IMM_PAD_TO_TARGET", "1") == "1"


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
# quotes only in the 5..90c band (no 1c-scrap quoting on pinned strikes).
# Cutoff close-15min -> close-10min + per-series $0.70 floor (Jack 2026-08-02:
# hourly programs activate ~hh:11 and the old cutoff left ~34 quotable
# minutes — flanking strikes projected $0.6-0.8 vs the $1 global floor and
# never entered; DCH 9am hour: T78.99 $0.81 / T80.99 $0.64 floored while
# T79.99/T81.99 quoted).
for _s in os.environ.get(
        "IMM_TEMP_SERIES",
        "KXTEMPAUSH,KXTEMPCHIH,KXTEMPDCH,KXTEMPLAXH,KXTEMPNYCH").split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            levels=_parse_levels(os.environ.get("IMM_TEMP_LEVELS", "0:5,1:2,2:2")),
            max_position=_env_float("IMM_TEMP_MAX_POSITION", 50),
            price_min_cents=_env_int("IMM_TEMP_PRICE_MIN", 5),
            price_max_cents=_env_int("IMM_TEMP_PRICE_MAX", 90),
            cutoff_from_close_min=_env_int("IMM_TEMP_CUTOFF_FROM_CLOSE_MIN", 10),
            min_hours_to_close=_env_float("IMM_TEMP_MIN_HOURS_TO_CLOSE", 0.05),
            # pre-cutoff reduce-only removed bot-wide 2026-08-02 (was 300s
            # here via IMM_TEMP_PRE_CUTOFF_RO) — temp follows the global 0.
            # 0.70 until 2026-08-04. The statement proved the exchange pays
            # NOTHING under $1.00 per market per program period, so the sub-$1
            # band this bought was quoted for exactly zero reward — it is
            # clamped back to PAYOUT_FLOOR_DOLLARS by series_min_est_total()
            # regardless, and the default now says so.
            min_est_total=_env_float("IMM_TEMP_MIN_EST_TOTAL", 1.00),
            atref_price_tol_ticks=_env_int("IMM_TEMP_ATREF_PRICE_TOL", 0),
            fast_lane=os.environ.get("IMM_TEMP_FAST_LANE", "1") == "1")

# Air-quality-index markets (user decision 2026-07-15). Series is KXAQICITY; the
# CITY lives in the event segment (KXAQICITY-NYC26JUL19), so one override covers
# every city. Each market is a POINT AQI reading at a fixed time ("AQI above 130
# in NYC Jul 19 3pm ET"); it opens ~2 days early and close_time IS the reading
# time. The city-prefixed ticker date is unparseable, so anchor the cutoff to
# close_time like weather — but with a bigger pre-reading buffer (30 min) since a
# point reading is highly autocorrelated in its final minutes. Multi-day market,
# so the default 1h reduce-only + 1h closing screen stand (no override needed).
# Main Trump-mention series (Jack 2026-07-30: re-enabled at a LITERAL 10
# contracts — a hand-tuned override ladder, so applied_mention_mult exempts
# it from the x1.5 that would otherwise make it 15). Quiet-hours mult still
# applies (3-7am ET doubles, like every non-TEMP ladder).
# Hand-tuned 0:10 RETIRED 2026-08-03 (Jack: "it should follow same rules as
# other markets") — KXTRUMPMENTION runs the global ladder/caps; set
# IMM_TRUMPMENTION_LEVELS to restore a literal ladder.
_TRUMP_LEVELS_SPEC = os.environ.get("IMM_TRUMPMENTION_LEVELS", "").strip()
if _TRUMP_LEVELS_SPEC:
    SERIES_OVERRIDES["KXTRUMPMENTION"] = SeriesOverride(
        levels=_parse_levels(_TRUMP_LEVELS_SPEC))

for _s in os.environ.get("IMM_AQI_SERIES", "KXAQICITY").split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            cutoff_from_close_min=_env_int("IMM_AQI_CUTOFF_FROM_CLOSE_MIN", 30))

# Rain markets (enrolled 2026-07-26): daily city binaries (KXRAIN) + monthly
# rain-day-count series (KXRAIN<CITY>M). Same 5..90c band as KXTEMP, BOTH
# sides BOTH bounds (Jack 2026-07-26, caught the bot at 2cx3c on a dead
# KXRAINHOUM strike: "i thought the range was 5-90c" — the 1..95 global band
# is for non-weather). A band also activates the top-in-band rule: an
# out-of-band book top stands the market down entirely, killing 1c-scrap
# quoting on dead strikes. Ladder/caps/cutoffs stay global (daily = midnight
# rule; monthly = continuous to close). NOTE: new rain cities must be added
# here (or via IMM_RAIN_SERIES) as well as the allowlist, like IMM_TEMP_SERIES.
_RAIN_LEVELS_SPEC = os.environ.get("IMM_RAIN_LEVELS", "").strip()
for _s in os.environ.get(
        "IMM_RAIN_SERIES",
        "KXRAIN,KXRAINAUSM,KXRAINCHIM,KXRAINDALM,KXRAINDENM,KXRAINHOUM,"
        "KXRAINMIAM,KXRAINNYCM,KXRAINSEAM,KXRAINSTPM").split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            # IMM_RAIN_LEVELS (e.g. "0:3", Jack 2026-07-28: rain re-entry at
            # 3/0/0) — empty = global ladder. A hand-tuned ladder here also
            # exempts rain from family multipliers via applied_mention_mult
            # (moot today: rain isn't mention).
            levels=_parse_levels(_RAIN_LEVELS_SPEC) if _RAIN_LEVELS_SPEC else None,
            price_min_cents=_env_int("IMM_RAIN_PRICE_MIN", 5),
            price_max_cents=_env_int("IMM_RAIN_PRICE_MAX", 90),
            # Jack 2026-08-01: run until 9pm ET the day before the rain day
            # (was 6pm since 7/29, midnight before that). Also gates the
            # directional module's entry window (its orders respect
            # cutoff_ts).
            cutoff_before_event_min=_env_int("IMM_RAIN_CUTOFF_BEFORE_MIN", 180))


def series_pad_to_target(series: str) -> bool:
    if PAD_TO_TARGET_GLOBAL:
        return True
    ov = SERIES_OVERRIDES.get(series)
    return bool(ov and ov.pad_to_target)


def pad_band_ok(series: str, ext_bid: Optional[int],
                ext_ask: Optional[int]) -> bool:
    """Pads only on markets whose EXTERNAL mid sits inside the SERIES band
    (5-90) — Jack 2026-08-02 "Do not pad on markets outside of 5-90 range":
    a 1000+-contract pad on a near-settled extreme is the closest thing to a
    standing pickoff (98c asks under near-certain YES). One-sided books
    count as outside. Shared by the quote loop AND the estimator overlay so
    floors/selection stay honest; the coverage alert exempts markets this
    gate leaves unpadded."""
    if ext_bid is None or ext_ask is None:
        return False
    mid = (ext_bid + ext_ask) / 2.0
    return series_price_min(series) <= mid <= series_price_max(series)


def pad_quantity(external_and_touch_depth: float, target: float) -> int:
    """Contracts to add at the pad price so the side's total depth reaches the
    reward target PLUS slack headroom (survives external withdrawal between
    requotes), rounded UP to the nearest PAD_ROUND. 0 if already at target."""
    gap = target - external_and_touch_depth
    if gap <= 0:
        return 0
    n = int(math.ceil((gap + PAD_SLACK_CONTRACTS) / PAD_ROUND) * PAD_ROUND)
    return min(n, PAD_MAX_CONTRACTS)


def series_override(series: str) -> Optional[SeriesOverride]:
    return SERIES_OVERRIDES.get(series)


def series_levels(series: str) -> List[Tuple[int, int]]:
    ov = SERIES_OVERRIDES.get(series)
    return ov.levels if (ov and ov.levels) else LEVELS


def series_side_max(series: str) -> int:
    return sum(s for _t, s in series_levels(series))


# Hour-of-day ladder size multipliers (Jack 2026-07-25, from the 8-day
# hour-of-day study): ET hours 3-7 carry ~1/3.3 the traded flow of the
# 9a-1p block, HALF the fill turnover per resting contract, and ~breakeven
# non-temp fill P&L — resting bigger ladders there is near-free rent.
# Spec "3-7:2.0,21-23:0.5": ET hour ranges (INCLUSIVE both ends, wrapping
# midnight allowed) -> multiplier on every rung of the series ladder.
# hour_scaled_levels() is the single source of ladder truth for the quote
# loop, the placement side/level caps, the collateral estimate and the
# candidate yield overlay — the 2026-07-14 lesson: EVERY consumer must see
# the same shape, or the placement guard blocks the boosted rungs.
# KXTEMP is excluded by default: its fill adverse selection is flat
# ~-6c/contract around the clock (measured on 7/17-7/25 settled fills), so
# more overnight temp size scales the bleed 1:1 with the rent.
def _parse_hour_mults(spec: str) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        rng, _, mult = part.partition(":")
        a, _, b = rng.partition("-")
        try:
            lo, hi, m = int(a), int(b or a), float(mult)
        except ValueError:
            raise ValueError(f"bad IMM_HOUR_SIZE_MULT part: {part!r}")
        if not (0 <= lo <= 23 and 0 <= hi <= 23 and m > 0):
            raise ValueError(f"bad IMM_HOUR_SIZE_MULT part: {part!r}")
        hours = list(range(lo, hi + 1)) if lo <= hi \
            else list(range(lo, 24)) + list(range(0, hi + 1))
        for h in hours:
            out[h] = m
    return out


HOUR_SIZE_MULTS = _parse_hour_mults(os.environ.get("IMM_HOUR_SIZE_MULT", ""))
HOUR_MULT_EXCLUDE = tuple(
    p for p in os.environ.get("IMM_HOUR_MULT_EXCLUDE", "KXTEMP").split(",") if p)


def _parse_series_hour_mults(spec: str) -> List[Tuple[str, Dict[int, float]]]:
    """'PREFIX:LO-HI:MULT,...' -> [(prefix, {hour: mult})], longest prefix
    first so the most specific rule wins. LO-HI carries the same semantics as
    IMM_HOUR_SIZE_MULT, wrap included (16-1 == 16:00 through 01:59 ET)."""
    out: List[Tuple[str, Dict[int, float]]] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        prefix, _, rest = part.partition(":")
        if not prefix or not rest:
            raise ValueError(f"bad IMM_SERIES_HOUR_MULT part: {part!r}")
        try:
            hours = _parse_hour_mults(rest)
        except ValueError:
            raise ValueError(f"bad IMM_SERIES_HOUR_MULT part: {part!r}")
        out.append((prefix, hours))
    out.sort(key=lambda kv: -len(kv[0]))
    return out


# PER-SERIES hour windows, which beat the global window AND the global
# exclude list — an explicit rule for a named family is never overridden by a
# blanket one. Jack 2026-08-04: "on KXDIESELD and KXAAAGASD events, cut the
# contract size in half starting at 4pm EST", extended to KXRAIN in the same
# breath. Window runs 16:00 ET to 01:59 ET rather than stopping at midnight:
# the gas/diesel DAILIES trade until 01:59 ET, so ending at midnight would
# hand back full size for the last two hours of their life. Prefixes are
# deliberately the DAILY tickers — KXAAAGASW/M weeklies and monthlies keep
# full size. Times are America/New_York (so EDT in summer), the convention
# every other window in this file uses.
SERIES_HOUR_MULTS = _parse_series_hour_mults(os.environ.get(
    "IMM_SERIES_HOUR_MULT",
    "KXDIESELD:16-1:0.5,KXAAAGASD:16-1:0.5,KXRAIN:16-1:0.5"))


def hour_size_mult(series: str, now_utc: datetime) -> float:
    """Active ladder multiplier for this series at this instant (1.0 outside
    configured windows and for excluded series prefixes)."""
    hour = now_utc.astimezone(ET).hour
    # A per-series rule wins ONLY for the hours it actually names. Returning
    # its default for every other hour would silently cancel the global
    # window: adding the 4pm halving to KXDIESELD/KXAAAGASD would have taken
    # away their quiet-hours 3-7am x2 as a side effect, which nobody asked for.
    for prefix, hours in SERIES_HOUR_MULTS:
        if series.startswith(prefix) and hour in hours:
            return hours[hour]
    if not HOUR_SIZE_MULTS:
        return 1.0
    if any(series.startswith(p) for p in HOUR_MULT_EXCLUDE):
        return 1.0
    return HOUR_SIZE_MULTS.get(hour, 1.0)


# Mention-family ladder multiplier. Jack 2026-07-28: x1.5 ("raise any caps
# commensurately") -> REMOVED 2026-07-30 ("remove 1.5x multiplier on
# MENTION, so that they are at 10 contracts"): default 1.0 = whole family
# on the global ladder, and the commensurate caps (150/750, skew 45/90)
# revert to 100/500/30-60 through applied_mention_mult automatically. The
# machinery stays env-tunable (IMM_MENTION_SIZE_MULT) if he wants it back.
MENTION_SIZE_MULT = _env_float("IMM_MENTION_SIZE_MULT", 1.0)


def mention_size_mult(series: str) -> float:
    if series.endswith("MENTION") or series.startswith("KXEARNINGSMENTION"):
        return MENTION_SIZE_MULT
    return 1.0


def applied_mention_mult(series: str) -> float:
    """The mention multiplier ACTUALLY applied to this series' geometry:
    1.0 for series with a hand-tuned SeriesOverride ladder/cap (Love Island
    5/5/5 net-50 stays literal — Jack's 2026-07-12 spec, not an accident of
    the family test); MENTION_SIZE_MULT for mention series riding the
    global geometry. Every consumer (ladder, per-market/event caps, skew)
    uses THIS, so the scaled pieces stay proportional to each other."""
    ov = SERIES_OVERRIDES.get(series)
    if ov and (ov.levels or ov.max_position is not None):
        return 1.0
    return mention_size_mult(series)


def hour_scaled_levels(series: str, now_utc: datetime) -> List[Tuple[int, int]]:
    """series_levels() with the family (mention) and hour multipliers applied
    to every rung size (half-up rounding, floor 1). The single ladder-shape
    source for the quote loop, the placement caps, the collateral estimate
    and the yield overlay."""
    lv = series_levels(series)
    m = hour_size_mult(series, now_utc) * applied_mention_mult(series)
    if m == 1.0:
        return lv
    return [(t, max(1, int(s * m + 0.5))) for t, s in lv]


def series_max_position(series: str) -> float:
    ov = SERIES_OVERRIDES.get(series)
    base = ov.max_position if (ov and ov.max_position is not None) \
        else MAX_POSITION_CONTRACTS
    # commensurate with the mention ladder multiplier (Jack 2026-07-28);
    # 1.0 for hand-tuned override series via applied_mention_mult
    return base * applied_mention_mult(series)


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


def member_price_band(series: str, is_member: bool) -> Tuple[int, int]:
    """Effective quoting band: the series band for fresh placement, widened
    to STICKY_PRICE_MIN/MAX (2-98) for markets already quoting — sticky
    accrual rides to the extremes instead of standing down at 5/90."""
    lo, hi = series_price_min(series), series_price_max(series)
    if is_member:
        lo, hi = min(lo, STICKY_PRICE_MIN), max(hi, STICKY_PRICE_MAX)
    return lo, hi


def series_min_est_total(series: str) -> float:
    """Per-series min-payout entry/hopeless floor, else the global
    MIN_EST_TOTAL_DOLLARS (read at call time so env/test patches apply).

    A per-series OVERRIDE is clamped up to PAYOUT_FLOOR_DOLLARS (2026-08-04):
    the floor is compared against an OPTIMISTIC projection (accrued + best of
    current/1h-peak), so a series bar below the exchange's own $1 minimum
    admits markets whose very best case is a payout of zero. KXTEMP's $0.70
    was exactly that (40 markets landed in the dead $0.70-$1.00 band over the
    2026-08-02..04 window, ~20/day, earning $0). The GLOBAL knob
    is left alone so setting it to 0 still disables floors wholesale, which is
    how the cutoff/mention-window tests isolate their subject."""
    ov = SERIES_OVERRIDES.get(series)
    if ov and ov.min_est_total is not None:
        return max(ov.min_est_total, PAYOUT_FLOOR_DOLLARS)
    return MIN_EST_TOTAL_DOLLARS


def series_atref_price_tol(series: str) -> int:
    """Per-series at-ref requote hysteresis (ticks), else the global
    ATREF_PRICE_TOL_TICKS (read at call time so env/test patches apply)."""
    ov = SERIES_OVERRIDES.get(series)
    if ov and ov.atref_price_tol_ticks is not None:
        return ov.atref_price_tol_ticks
    return ATREF_PRICE_TOL_TICKS


def series_in_blackout(series: str, now_utc: datetime) -> bool:
    """True while `series` sits inside its daily no-quote window (ET).
    Jack 2026-08-04: AAA posts gas AND diesel between 03:18 and 03:36 ET
    (observed 5 consecutive days by the gas sniper at 5s resolution). The
    DAILY markets close 01:59 ET so they are safe, but the WEEKLY and
    MONTHLY ones stay open straight through the print and were being picked
    off — hour 3 measured -14.4c/contract against a -2.3c book average."""
    ov = SERIES_OVERRIDES.get(series)
    if not ov or not ov.blackout_et:
        return False
    try:
        start_s, end_s = ov.blackout_et
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except (ValueError, AttributeError):
        return False
    et = now_utc.astimezone(ET)
    cur = et.hour * 60 + et.minute
    start, end = sh * 60 + sm, eh * 60 + em
    return start <= cur < end if start <= end else (cur >= start or cur < end)


def series_fast_lane(series: str) -> bool:
    """Membership in the FAST_LANE_SECS mini-cycle set (KXTEMP by default)."""
    ov = SERIES_OVERRIDES.get(series)
    return bool(ov and ov.fast_lane)


def series_min_est_rate(series: str) -> float:
    """Per-series est-$/day RATE floor for fresh candidates (0 = off)."""
    ov = SERIES_OVERRIDES.get(series)
    return ov.min_est_per_day if (ov and ov.min_est_per_day) else 0.0


def series_safe_join(series: str) -> bool:
    ov = SERIES_OVERRIDES.get(series)
    return bool(ov and ov.safe_join)


def series_pre_cutoff_reduce_only_secs(series: str) -> int:
    ov = SERIES_OVERRIDES.get(series)
    return ov.pre_cutoff_reduce_only_secs \
        if (ov and ov.pre_cutoff_reduce_only_secs is not None) \
        else PRE_CUTOFF_REDUCE_ONLY_SECS


def series_of(ticker: str) -> str:
    return ticker.split("-")[0]


MAX_POSITION_CONTRACTS = _env_float("IMM_MAX_POSITION", 100)   # per market (user spec)
MAX_EVENT_CONTRACTS = _env_float("IMM_MAX_EVENT", 500)         # net per event (user spec)


def event_cap_contracts(event_ticker: str) -> float:
    """Per-event net cap, scaled commensurately with the family ladder
    multiplier (mention x1.5 -> event cap 750; Jack 2026-07-28)."""
    return MAX_EVENT_CONTRACTS * applied_mention_mult(series_of(event_ticker))
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

# GLOBAL band 5..90 (Jack 2026-07-26: "i want 5-90 to be global not just
# weather" — supersedes the 7/13 widening to 1/95). Applies BOTH sides BOTH
# bounds via build_side_ladder, and the top-in-band rule stands a market
# down entirely when its book top is outside — extreme-priced books are
# scraps with tail risk, not rent. The mid-band selection screen aligns so
# the bot doesn't select markets it would never quote.
PRICE_MIN_CENTS = _env_int("IMM_PRICE_MIN_CENTS", 5)    # never quote below (bid side)
PRICE_MAX_CENTS = _env_int("IMM_PRICE_MAX_CENTS", 90)   # never quote above (ask side)
# Sticky-member band: a market ALREADY quoting rides its accrual past the
# series band before standing down — entry screens (extreme_mid 5-90) and
# fresh placement keep the tight band; only members widen. Top-in-band, the
# side gates and the rung clamps all consume this via member_price_band().
# 2-98 (Jack 2026-08-02) -> 5-93 (Jack 2026-08-03, after the Austin 10pm
# pickoff: the bot's 96c member ask sold 60 into near-certain YES in the
# final minutes — 93 keeps modest extreme-riding without the deep-ITM tail).
# This is also the ABSOLUTE rung envelope (at-ref px bounds + safe-join
# clamps); pads (1c/99c, capped 800, 5-90-mid gate) are deliberately outside.
STICKY_PRICE_MIN = _env_int("IMM_STICKY_PRICE_MIN", 5)
STICKY_PRICE_MAX = _env_int("IMM_STICKY_PRICE_MAX", 93)
# Deep-rung floor (Jack 2026-08-03 "allow quoting below 5 cents if the
# market is within 5-93 ... because the reference point is lower"): on a
# HEALTHY book (both touches in-band) an at-ref rung may follow the
# reference down to this hard floor. 1c stays reserved for pads.
RUNG_DEEP_FLOOR = _env_int("IMM_RUNG_DEEP_FLOOR", 2)
MID_BAND_LO = _env_int("IMM_MID_BAND_LO", 5)            # skip markets with mid outside
MID_BAND_HI = _env_int("IMM_MID_BAND_HI", 90)
# Wide screen effectively OFF (Jack 2026-07-23: 25->99): incentives are earned
# for RESTING regardless of spread, and a wide book means LESS competition ->
# HIGHER pool share, so wider is better, not worse. 99 lets every real book
# through; the separate crossed/locked check (ext_bid >= ext_ask) and the
# top-in-band price-bound check still stand. IMM_MAX_JOIN_SPREAD to restore.
MAX_JOIN_SPREAD_CENTS = _env_int("IMM_MAX_JOIN_SPREAD", 99)
# Volume screen REMOVED (Jack 2026-07-22): a book with no volume can't fill
# you — resting there is incentive rent with ~zero P&L risk, which is the
# whole point. Set IMM_MIN_VOLUME > 0 to restore the old anti-junk screen.
MIN_VOLUME_CONTRACTS = _env_float("IMM_MIN_VOLUME", 0)
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
# REMOVED across the bot (Jack 2026-08-02: "remove reduce-only X minutes
# ahead"): default 0 = both sides quote right up to the cutoff; the exchange-
# side order-expiration cap at the cutoff (place_order) is the guard, and the
# _screen cutoff-5min rule still stops FRESH selection. >0 restores the old
# window (per-series via SeriesOverride.pre_cutoff_reduce_only_secs).
PRE_CUTOFF_REDUCE_ONLY_SECS = _env_int("IMM_PRE_CUTOFF_REDUCE_ONLY", 0)

DAILY_LOSS_LIMIT = _env_float("IMM_DAILY_LOSS_LIMIT", 1200.0)  # realized+unrealized $, halts to next ET day (user raises: 50->150 7/14; ->500 7/19; ->800 7/20; ->1200 7/21)
MAX_TOTAL_RESTING_ORDERS = _env_int("IMM_MAX_TOTAL_RESTING", 2000)  # 450->1000->2000
#   (Jack 2026-07-23: universe grew to ~263 mkts/50 events wanting >1300 orders;
#   1000 pinned + order_cap alerts every cycle starved the low-yield tail like
#   KXRT-BRIN-45/50. Advanced tier + 24/s throughput handle 2000 fine. NOTE:
#   the $4000 collateral budget may become the next binding constraint.)
MAX_PLACEMENTS_PER_CYCLE = _env_int("IMM_MAX_PLACEMENTS_PER_CYCLE", 120)
QUALIFY_PATIENCE_CYCLES = _env_int("IMM_QUALIFY_PATIENCE", 30)  # bench zero-reward markets
BENCH_COOLDOWN_SECS = _env_int("IMM_BENCH_COOLDOWN", 4 * 3600)
# Post-2026-07-30 payouts scale by the ratio of non-excluded snapshots (a
# snapshot is excluded unless BOTH sides reach target size). Per-market EMA of
# our observed counted/excluded state approximates that ratio over the hours
# the bot runs; ~0.02/cycle at 30-45s cycles = ~half-hour memory.
COVERAGE_EMA_ALPHA = _env_float("IMM_COVERAGE_EMA_ALPHA", 0.02)
FAILSAFE_CANCEL_AFTER = _env_int("IMM_FAILSAFE_AFTER", 4)
WAKE_GAP_SECS = 600
# Hang watchdog: the bot is single-threaded, so one wedged syscall = silent
# death (observed 2026-07-23: an SSL read inside cancel_order blocked ~50 min
# with 0 CPU — likely a socket frozen across a lid-close sleep, read timeout
# never firing). A daemon thread hard-exits the process when no cycle
# heartbeat lands for this long; the launcher relaunches within 30s, and the
# per-placement journal + persisted halt/carry make a hard exit safe.
CYCLE_HANG_EXIT_SECS = _env_int("IMM_CYCLE_HANG_EXIT", 600)
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
# GPU rental-price family (Jack 2026-07-23: explicitly EXCLUDED — same
# structural class as the econ/company series but ~600 markets that would
# triple the candidate universe and refresh cost). Blocklisted (not merely
# absent from the allowlist) so the daily auto-enroll can NEVER pull them in;
# the prefixes cover every variant (MAX/MON/MS/WS). Re-include by removing
# these from the blocklist AND raising MAX_CANDIDATE_BOOKS.
_GPU_RENTAL_PREFIXES = ["KXA100", "KXB200", "KXH100", "KXH200", "KXRTX5090"]
SERIES_BLOCKLIST_PREFIXES = tuple(
    [f"KX{a}MAXMON" for a in _CRYPTO_ASSETS] + [f"KX{a}MINMON" for a in _CRYPTO_ASSETS]
    + ["KXHIGH"]                                    # high_temp_trading.py (cloud)
    + ["KXMLBMENTION", "KXNBAMENTION", "KXNCAABMENTION"]   # mlb/nba/ncaa (cloud)
    + _GPU_RENTAL_PREFIXES                           # GPU rental price (excluded)
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
# Company operating-metric series (Jack 2026-07-22): KX<stock ticker> with a
# <YY><MON><METRIC> event segment (KXBA-26JULDELIV = Boeing July deliveries,
# KXHOOD-26JULFUNDED = Robinhood funded accounts, ...), plus the branded
# consumer-price trackers (Jack same day: KXSBUXSAR / KXCFACHICKSAND /
# KXCHIPBURRITO etc. — menu/product prices measured on a dated observation).
# A-suffixed entries are the longer-horizon variants of the same metric.
# Month-granular ticker dates don't parse -> no midnight rule; cutoff comes
# from Kalshi's occurrence_datetime when meaningful, else these quote
# continuously like the crypto structural series. Day-dated consumer-price
# tickers (26AUG02) DO parse -> midnight-ET rule stops quoting before the
# observation day, the safe direction.
_DEFAULT_COMPANY_SERIES = (
    "KXBA,KXPM,KXHOOD,KXHOODA,KXWING,KXWINGA,KXMETA,KXRDDT,KXINTC,KXGOOG,"
    "KXSCHW,KXCMG,KXAMZN,KXCOINBASE,KXCVNA,KXDPZ,KXFSLR,KXFSLRA,KXLUV,"
    "KXNCLH,KXRBLX,KXSBUX,KXTLN,KXTLNA,KXWH,KXYOU,KXRACE,"
    "KXSBUXSAR,KXCFACHICKSAND,KXPOPCHICKSAND,KXCHIPBURRITO,KXDDCOLDBREW,"
    "KXBKNUGGETS,KXAMSAVO,"
    # Monthly FOOT TRAFFIC (Jack 2026-08-03 "why do KXBKFT and KXYUMTBFT not
    # get quoted"): never enrolled — the auto-classifier files day-dated
    # T-threshold shapes (26SEP07-T100) as REVIEW, not enroll. Same consumer-
    # metric class as the price trackers; re-entry guards apply.
    "KXBKFT,KXYUMTBFT")
# Economic-data / price-index prints (Jack 2026-07-23): recurring published
# statistics — AAA gas price (daily/weekly/monthly), new-home sales, gas CPI,
# the Shanghai freight index. Day-dated tickers -> the midnight-ET rule stops
# quoting before each observation/release day, the safe direction. The GPU
# rental-price family (KXA100*/B200*/H100*/H200*/RTX5090*, ~600 mkts) is the
# same structural class but deliberately NOT included pending a capacity
# decision — it alone would triple the candidate universe.
_DEFAULT_ECON_SERIES = (
    # KXDIESELD/W added 2026-08-02 (Jack "why did KXDIESELD not make it?" —
    # the series was never enrolled; $222/day/mkt x 21 strikes live). Same
    # day-dated midnight-ET safety as the gas trackers; re-entry guards apply.
    "KXAAAGASD,KXAAAGASW,KXAAAGASM,KXNHSALES,KXUSGASCPI,KXSCFI,"
    "KXDIESELD,KXDIESELW")
# Rotten Tomatoes score markets (Jack 2026-07-23): undated tickers
# (KXRT-<MOVIE>-<score>) -> occurrence-based cutoff when Kalshi provides
# one, else continuous quoting; bands/floor gate as usual.
# 2026-08-02 (Jack): pulled OUT of the econ set onto its own track — the
# 7/29 "block ECON going forward" no-new run-off had swept KXRT along by
# config placement, silently barring fresh RT events (SPI-89 est $7/day was
# no_new'd while its screens passed). Entertainment reveals are not macro
# prints; keep this list OUT of NO_NEW_SERIES.
_DEFAULT_ENTERTAINMENT_SERIES = "KXRT"
# US Treasury yield prints (Jack 2026-08-04: "allowlist KXUST10AD, KXUST2AD,
# KXUST30AD, KXUST5AD, KXUST7AD"). These have sat at the TOP of the
# quote-gaps ranking for days — $1,534/day pool per event x 5 tenors, 15
# strikes each, roughly half the whole unquoted estimate — held out only by
# never having been enrolled. Structurally the same as the gas/CPI prints: a
# published number on a known day, day-dated tickers (KXUST10AD-26AUG04) that
# PARSE, so the midnight-ET rule stops quoting before each print day, which
# is the safe direction. Enrolled through the guarded re-entry set below
# (safe-join placement + the $2/day rate bar; their ~$102/day/market pools
# clear that bar by two orders of magnitude, so it guards a dead book rather
# than gating these).
# MONTHLIES added 2026-08-04 (Jack: "also allowlist the treasury monthlies
# like KXUST2AM"). Identical contract — "par yield above X on <date>", 3:30pm
# ET close, same Treasury source — just settled ~4 weeks out instead of
# tomorrow, so every rule below reads across unchanged. They are the better
# COVERAGE story of the two: a monthly's program (e.g. Aug 3 -> Aug 8) sits
# entirely inside its quotable window, where a daily's midnight/7:30 cutoff
# still cedes the print day. Smaller pools ($19/day/market vs $102) over
# 15 strikes x 5 tenors. The trade-off is inventory: these can be held for
# weeks, so the per-market cap is what bounds them, not the clock.
_DEFAULT_RATES_SERIES = ("KXUST2AD,KXUST5AD,KXUST7AD,KXUST10AD,KXUST30AD,"
                         "KXUST2AM,KXUST5AM,KXUST7AM,KXUST10AM,KXUST30AM")
ALLOW_SERIES = frozenset(
    s for s in (os.environ.get("IMM_ALLOW_SERIES", _DEFAULT_CRYPTO_SERIES) + ","
                + os.environ.get("IMM_ALLOW_COMPANY_SERIES", _DEFAULT_COMPANY_SERIES)
                + "," + os.environ.get("IMM_ALLOW_ECON_SERIES", _DEFAULT_ECON_SERIES)
                + "," + os.environ.get("IMM_ALLOW_RATES_SERIES",
                                       _DEFAULT_RATES_SERIES)
                + "," + os.environ.get("IMM_ALLOW_ENTERTAINMENT_SERIES",
                                       _DEFAULT_ENTERTAINMENT_SERIES)
                ).split(",") if s)

# NO-NEW gate (Jack 2026-07-28 pm): series listed here admit NO fresh
# candidates; members ride sticky to natural death. History: company set
# gated 7/28, econ 7/29, then company FROZEN 7/29. RE-ENTRY (Jack 2026-08-02
# "Re allowlist company and econ markets... wade back safely"): default now
# EMPTY — admission reopened under the guarded re-entry overrides below
# ($2/day rate floor + safe-join placement). Env IMM_NO_NEW_SERIES restores
# a gate.
NO_NEW_SERIES = frozenset(
    s for s in os.environ.get("IMM_NO_NEW_SERIES", "").split(",") if s)

# Hand-FORCED events (Jack 2026-08-03, KXTRUMPMENTION-26AUG03: Kalshi
# stamped the program period to Aug 18 on a same-day event, diluting the
# $100/market period_reward to a phantom $6.46/day that no floor can pass):
# events listed here bypass the rate/total floors and the hopeless exit —
# a DELIBERATE per-event data-bug override, unlike the retired automatic
# curation. Screens, bands, caps, budget and manual-yield still apply.
FORCE_EVENTS = frozenset(
    e.strip() for e in os.environ.get("IMM_FORCE_EVENTS", "").split(",")
    if e.strip())

# FULL FREEZE by EXACT series (Jack 2026-07-29 am: "stop quoting COMPANY
# events" — escalates the 7/28 run-off to zero orders; positions ride to
# settlement per the standing freeze rule). EXACT matching, not prefix:
# 43 short company tickers as prefixes would repeat the KXHEGSETHOUT-via-
# 'ETH' substring trap (KXWH would swallow any future KXWH*). Wired into
# _blocked(), so every freeze path (candidates, orphan-restore, managed
# flush) inherits. Earnings-MENTION (KXEARNINGSMENTION*) is NOT company —
# it stays live. Default = the NO_NEW company set; env IMM_FREEZE_SERIES.
# History: company set frozen 7/29 am (43 names + variant sweeps + KXAC +
# KXSCFI); KXTRUMPMENTION passed through 7/29-7/30. RE-ENTRY (Jack
# 2026-08-02): default now EMPTY — company/econ quote again under the
# guarded re-entry overrides below ($2/day rate floor + safe-join
# placement). Gas trackers stay dark via the LAUNCHER blocklist (the gas
# sniper's book, a different mechanism/decision). Env IMM_FREEZE_SERIES
# restores a freeze.
FREEZE_SERIES = frozenset(
    s for s in os.environ.get("IMM_FREEZE_SERIES", "").split(",") if s)

# Guarded RE-ENTRY set (Jack 2026-08-02: "wade back safely into these
# markets and see if they are profitable"): every re-allowed company/econ
# series quotes only when the estimator clears IMM_REENTRY_MIN_RATE $/day
# (rate floor, ON TOP of the $1 total-payout floor) and places with the
# safe-join rule — quotes rest >= SAFE_JOIN_OFFSET_TICKS behind the touch
# unless the bid/ask spread is >= SAFE_JOIN_MIN_SPREAD ticks (a wide spread
# IS the safety net). The estimator overlays the same clamped ladder, so
# the rate floor is tested net of the safety penalty.
_REENTRY_SERIES = (
    _DEFAULT_COMPANY_SERIES + ",KXAAL,KXAALA,KXALK,KXALKA,KXAXP,KXAXPA,"
    "KXGOOGA,KXLUVA,KXSBUXA,KXCMGA,KXMETAA,KXVZ,KXVZA,KXWHA,KXYUM,KXYUMA,"
    # gas trackers back too (Jack 2026-08-02 "reconsider gas trackers";
    # launcher blocklist entry removed same day). Midnight-ET ticker rule
    # keeps IMM out of print-day books, so no 3:20am sniper collision.
    # Diesel enrolled 2026-08-02 evening (same class, never allowed before).
    "KXAC,KXNHSALES,KXSCFI,KXAAAGASD,KXAAAGASW,KXAAAGASM,KXUSGASCPI,"
    "KXDIESELD,KXDIESELW,KXBKFT,KXYUMTBFT,"
    # Treasury yields enrolled 2026-08-04 — a rate print is exactly the book
    # safe-join exists for: it sits tight and two-sided until the number
    # lands, so joining the touch is the expensive way to be there.
    + _DEFAULT_RATES_SERIES)
# RATE BAR 2.0 -> 0.0 (Jack 2026-08-05: "recover the markets lost"). The
# $2/day bar was a RISK gate for wading back into company/econ after the 7/29
# freeze, not an economics one — and as a per-DAY test it is horizon-blind, so
# it permanently locks out exactly the markets whose value comes from a long
# window. Measured across the seven affected events, EVERY lost market failed
# this one gate and nothing else: 53 markets, $55.13 of projected reward, all
# of which clear the $1.00 payout floor that actually determines whether
# Kalshi pays. It is also the far side of the eviction ratchet — a market that
# dips out can never return through it.
#
# The economics gate is now MEASURED rather than assumed (the 2026-08-04
# statement: nothing under $1.00 per market per program period is ever paid),
# so MIN_EST_TOTAL_DOLLARS is the right test and this was a horizon-blind
# proxy for it. Safe-join placement — the actual adverse-selection protection
# — is unchanged. Same call Jack already made for KXDIESELW on 2026-08-03,
# generalised.
#
# BLAST RADIUS: this admits every currently rate-floored market, not just the
# 53 on the named events (~111 in the last universe line, ~22% more markets).
# The collateral budget and the $1 payout floor are what bound it now.
for _s in os.environ.get("IMM_REENTRY_SERIES", _REENTRY_SERIES).split(","):
    if _s.strip() and _s.strip() not in SERIES_OVERRIDES:
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            min_est_per_day=_env_float("IMM_REENTRY_MIN_RATE", 0.0),
            safe_join=True)

# KXDIESELW quoted as an OVERRIDE (Jack 2026-08-03): exempt from the $2/day
# re-entry rate bar — its $18.58/day pools est $0.8-1.4/mkt, all floored.
# The $1 TOTAL payout floor still applies (~6 quotable days -> any material
# est passes) and safe-join placement is kept. Replaces the loop's entry.
SERIES_OVERRIDES["KXDIESELW"] = SeriesOverride(
    min_est_per_day=_env_float("IMM_DIESELW_MIN_RATE", 0.0), safe_join=True)

# TREASURY YIELDS (Jack 2026-08-04: "quote treasuries until 7:30am EST").
# Replaces the re-entry loop's entry so the safe-join + rate bar are kept.
# The default midnight-ET ticker rule cost the whole overnight half of each
# program for no safety gain: these settle on a 3:30pm ET snapshot of a
# continuously-traded rate, so there is no release to be out of the way of,
# and 00:00-07:30 ET is no more informed than the evening before. 7:30am ET
# puts the bot out ahead of the 08:30 releases that do move rates. Widens the
# quotable window from ~8h to ~15.5h of each 23.5h program.
for _s in os.environ.get("IMM_RATES_SERIES", _DEFAULT_RATES_SERIES).split(","):
    if _s.strip():
        SERIES_OVERRIDES[_s.strip()] = SeriesOverride(
            # 0.0 since 2026-08-05 — see the re-entry block above. This loop
            # had its own copy of the 2.0 default, so changing it there alone
            # left the Treasuries (the family the complaint was about) still
            # locked out. One default, read from the same env var.
            min_est_per_day=_env_float("IMM_REENTRY_MIN_RATE", 0.0),
            safe_join=True,
            event_day_cutoff_et=(
                _env_int("IMM_RATES_CUTOFF_HOUR_ET", 7),
                _env_int("IMM_RATES_CUTOFF_MIN_ET", 30)))

# AAA PRINT BLACKOUT (Jack 2026-08-04: "stop getting sniped ... ensure my
# quotes expire beforehand"). Observed post times, from the gas sniper's own
# 5s-resolution detection: 03:36:25, 03:18:25, 03:20:27, 03:22:53, 03:25:17 ET
# on Jul 30 - Aug 3. The window below brackets that with ~13 min of margin on
# the early side and ~24 on the late side. Applies to gas AND diesel, every
# horizon: the DAILY markets close 01:59 ET and are structurally safe, but the
# weekly/monthly ones stay open across the print and are what actually bleed.
for _s in os.environ.get(
        "IMM_AAA_PRINT_SERIES",
        "KXAAAGASD,KXAAAGASW,KXAAAGASM,KXDIESELD,KXDIESELW").split(","):
    _s = _s.strip()
    if _s and _s in SERIES_OVERRIDES:
        SERIES_OVERRIDES[_s] = replace(
            SERIES_OVERRIDES[_s],
            blackout_et=tuple(os.environ.get(
                "IMM_AAA_BLACKOUT_ET", "03:05-04:00").split("-")))


# Event-start resolution for mention markets: the ticker date's midnight-ET
# cutoff forfeits game-day daytime, but programs run ~1-2 days INCLUDING game
# day — resolve real start times where a reliable source exists, cut off
# EVENT_START_BUFFER_MIN before it, fall back to midnight ET.
EVENT_START_BUFFER_MIN = _env_int("IMM_EVENT_START_BUFFER_MIN", 30)
# Tighter buffer for FILE/CODE event-start OVERRIDES (earnings CALL times and
# company-disclosure RELEASE times): be fully OUT — every order expired
# exchange-side — this many minutes before the scheduled call/release. 10 min
# (Jack 2026-07-23: "all orders expire 10min before the release, 3:50pm for
# Intel"). Distinct from the 30-min game buffer because a hard scheduled
# disclosure is a precise instant, not an approximate kickoff.
OVERRIDE_BUFFER_MIN = _env_int("IMM_OVERRIDE_BUFFER_MIN", 10)
# Fresh candidates aren't admitted inside this many minutes of a cutoff
# (pointless entry + placement race); MEMBERS are exempt and quote to the
# true cutoff — the exchange-side order expiration is the hard stop there.
CUTOFF_SCREEN_BUFFER_MIN = _env_int("IMM_CUTOFF_SCREEN_BUFFER", 5)
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

# Candidate cap: the pre-meta list is sorted by raw pool $/day and truncated
# here — a SILENT exclusion (no skip count) that also fights the pass-2
# objective (small pools with empty books can out-yield big farmed pools).
# 250 was already binding on 2026-07-22 and swallowed the entire new
# company-metric class (~$19/day markets); the econ-series addition on 7/23
# pushed the allowed universe past 450 too. 700 covers ~544 with headroom.
# Refresh cost scales with this (book reads), fine under keep-alive.
# 700 -> 5000 (Jack 2026-08-03 "i dont want this to be a limiter"): the cap
# first BOUND at 700 on 8/3 02:38Z (diesel + re-entry + earnings-week
# listings). At 5000 every allowed program market becomes a candidate;
# refresh cost now scales with the allowed universe (~800-1000 book reads),
# lengthening hh:00-hh:11 activation-window cycles — accepted trade.
MAX_CANDIDATE_BOOKS = _env_int("IMM_MAX_CANDIDATE_BOOKS", 5000)
# Payout floor is the $1/market MINIMUM PAYOUT itself, tested against the
# bot's expected TOTAL accrual over the market's remaining quotable life
# (est $/day x days to program end, capped by cutoff/close) — NOT a per-day
# rate (Jack 2026-07-22): a $0.40/day estimate on a 5-day program clears the
# $1 minimum comfortably; the old per-day floor only existed to catch 1-2-day
# programs that couldn't. Margin above $1 via the env knob if wanted.
# Per-series override: SeriesOverride.min_est_total via series_min_est_total(),
# but NOTHING may sit below PAYOUT_FLOOR_DOLLARS — see series_min_est_total().
MIN_EST_TOTAL_DOLLARS = _env_float("IMM_MIN_EST_TOTAL", 1.0)
# The exchange's own minimum: a market whose payout for a program period comes
# to less than this is paid NOTHING. Measured, not assumed — across all 2,720
# LIQUIDITY credits on Jack's 2026-08-04 statement the smallest is exactly
# $1.00 and none is below it (the floor is a liquidity-program rule: the one
# VOLUME incentive on the statement is $0.31), and "one credit per market clearing $1" predicts
# the actual per-event credit count exactly on 94 of 99 clean-window events
# (272 predicted vs 273 actual). This is a hard threshold, so a per-series
# entry floor UNDER it can only admit markets that provably pay zero: they are
# pure fill risk. KXTEMP's $0.70 (Jack 2026-08-02) was exactly that and is now
# clamped away.
PAYOUT_FLOOR_DOLLARS = _env_float("IMM_PAYOUT_FLOOR", 1.0)
# Horizon escape from the per-series RATE floor (Jack 2026-08-03): a fresh
# candidate admits on EITHER est_rate >= the series bar OR a projected TOTAL
# >= this. The rate bar alone is horizon-blind — it rejects a slow market that
# would still earn well over its life exactly as hard as one that never pays.
# CAREFUL: the projection is bounded by the PROGRAM end, not the market close.
# The foot-traffic markets that prompted this close 2026-09-07 but their
# incentive programs end 2026-08-09, so the window is 5 days and only 1 of 22
# clears $5 — the rule is deliberately not a blanket admission.
RATE_FLOOR_TOTAL_ALT = _env_float("IMM_RATE_FLOOR_TOTAL_ALT", 5.0)
# The floor is a hard threshold on a NOISY estimate (thin books swing the
# share estimate ±50% between refreshes), so borderline markets could flap
# just under $1 at every sampling instant and never enter (observed
# 2026-07-23: 28 company markets measuring $1-2.6 in a spot check, all
# floored at the live refreshes). Entry therefore uses the market's PEAK
# estimate over the last hour; sticky selection holds it once in.
EST_PEAK_TTL_SECS = _env_int("IMM_EST_PEAK_TTL", 3600)
# STICKY EXIT for hopeless markets (Jack 2026-07-25: "quoting markets that
# don't hit $1 is a big drain" — if there's <5% chance of reaching the $1 min
# payout before the incentive expires, stop quoting EVEN IF already started).
# The one deliberate exception to sticky retention: sub-$1 accrual pays
# NOTHING, so riding a hopeless market to its natural end is pure fill risk
# for zero reward. "<5%" is encoded structurally, not statistically: even the
# OPTIMISTIC projection (accrued-so-far + the better of the current and
# 1h-peak remaining estimate) stays under the bar. quote_all series exempt.
HOPELESS_EXIT = os.environ.get("IMM_HOPELESS_EXIT", "1") == "1"
# ...but ONLY after the projection has been under the bar CONTINUOUSLY for
# this long (Jack 2026-08-05: "dont evict on dips under $1").
#
# The peak-TTL guard above was supposed to already do this — its comment
# claimed "eviction needs a full peak-TTL hour of sustained sub-$1 projection"
# — and it does not. `pts` is only refreshed when a NEW HIGH is set, so a
# market whose estimate DECLINES (the normal path: competitors arrive, our
# share falls) never refreshes it, the peak silently expires an hour later,
# and then a SINGLE low reading evicts. Measured on KXUST7AM-26AUG31: 15
# strikes -> 7 overnight, and the same signature on KXFSLR-26OCTMWSOLD and
# KXHOOD-26NOVECVOL (7 -> 5 each).
#
# Eviction is also effectively PERMANENT, which is what makes a false one
# expensive: an evicted market re-enters as a FRESH candidate and must clear
# the per-series rate bar, which these structurally cannot (est $0.37/day
# against a $2/day bar). The `rate_floor` skip bucket climbs monotonically as
# `hopeless` fires — 96 -> 106 over the same window. So the exit needs to be
# sure, not fast.
HOPELESS_SUSTAIN_SECS = _env_int("IMM_HOPELESS_SUSTAIN_SECS", 3600)


def _quotable_days(meta, now_utc: datetime) -> float:
    """Remaining accrual window in days: to the program end, capped by the
    market's cutoff/close — quoting (and thus accrual) stops at whichever
    comes first."""
    end = meta.program_end
    for cap in (meta.cutoff, meta.close_time):
        if cap is not None and (end is None or cap < end):
            end = cap
    if end is None:
        return 1.0          # undated: fall back to one day of rate
    return max((end - now_utc).total_seconds() / 86400.0, 0.0)
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

# Watchdog: N selected markets with almost nothing resting for several cycles
# is the signature of every silent-death class seen so far (midnight-ET
# earnings cutoff, program races, override gaps) — page instead of waiting
# for someone to look at the book.
WATCHDOG_MIN_SELECTED = _env_int("IMM_WATCHDOG_MIN_SELECTED", 10)
WATCHDOG_MIN_RESTING = _env_int("IMM_WATCHDOG_MIN_RESTING", 5)
WATCHDOG_CYCLES = _env_int("IMM_WATCHDOG_CYCLES", 3)
# Coverage alert (Jack 2026-08-02, the sides==1 leak): a SELECTED market
# quoted while only one side qualifies accrues ZERO with full fill risk.
# pad_missing_side should make this ~impossible; page if it persists anyway.
COVERAGE_ALERT_CYCLES = _env_int("IMM_COVERAGE_ALERT_CYCLES", 5)

# Account-balance hard floor: the bot's risk math sees only its OWN book, but
# the account is shared (crypto fleet, cloud bots, manual). If the balance
# drops this much below the daily anchor — whatever the source — halt and
# page. Deposits/withdrawals move the balance too; the alert says so. 0 = off.
BALANCE_DROP_HALT = _env_float("IMM_BALANCE_DROP_HALT", 2500.0)


def _halt_day_key(now_utc: datetime) -> str:
    """The 5am-CT roll day the halt/carry/balance anchors belong to."""
    return (now_utc.astimezone(CT)
            - timedelta(hours=SUMMARY_HOUR_CT)).date().isoformat()


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


# Auto-enrolled series (written by the daily imm_earnings_overrides.py run,
# which classifies newly-programmed series against Jack's strategy families):
# {"series": [...]}. Hot-reloaded by mtime, merged into the allow check.
# SAFETY: the blocklist always wins regardless, and the loader itself refuses
# fleet monthly (*MAXMON/*MINMON) entries even if hand-added.
EXTRA_ALLOW_FILE = os.path.join(STATUS_DIR, "extra_allow_series.json")
EXTRA_ALLOW_SERIES: Set[str] = set()
_extra_allow_state = {"mtime": 0.0}


def load_extra_allow_series() -> int:
    try:
        mtime = os.path.getmtime(EXTRA_ALLOW_FILE)
    except OSError:
        return 0
    if mtime == _extra_allow_state["mtime"]:
        return 0
    _extra_allow_state["mtime"] = mtime
    try:
        with open(EXTRA_ALLOW_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError) as e:
        log(f"[IMM] ! extra allow file unreadable: {e}")
        return 0
    fresh: Set[str] = set()
    for s in data.get("series") or []:
        s = str(s).strip()
        if not s or not s.startswith("KX"):
            continue
        if s.endswith("MAXMON") or s.endswith("MINMON"):
            log(f"[IMM] ! refused fleet monthly series in extra allow: {s}")
            continue
        if any(s.startswith(p) for p in SERIES_BLOCKLIST_PREFIXES):
            log(f"[IMM] ! refused blocklisted series in extra allow: {s}")
            continue
        fresh.add(s)
    changed = len(fresh ^ EXTRA_ALLOW_SERIES)
    EXTRA_ALLOW_SERIES.clear()
    EXTRA_ALLOW_SERIES.update(fresh)
    if changed:
        log(f"[IMM] extra allow series: {len(fresh)} enrolled ({changed} changed)")
    return changed


# ----------------------------------------------------------------------------
# Rain daily fair-value GATE (Jack 2026-07-28, "do 5"; reworked same day from
# a quote-anchor to a gate — Jack: "my primary goal is to stay near the top
# of the orderbooks to get incentive rewards", and reward credit halves per
# tick behind the touch, so fair must never move quotes off the join).
# rain_fair.py computes P(measurable rain on the local calendar day) per
# settlement station from the NWS hourly forecast and writes RAIN_FAIR_FILE
# (a daemon thread started in run() refreshes it every RAIN_FAIR_REFRESH_MIN
# minutes; the file is hot-reloaded by mtime like the other override files).
# The quote loop joins the touch UNCHANGED whenever it quotes; the gate only
# decides WHETHER: it stands the market down when the touch itself fights
# the forecast on the adverse side (bid touch > fair+TOL / ask touch <
# fair-TOL). Missing/stale entries (per-entry TTL) open the gate — every
# failure mode degrades to plain band-quoting. Fair CANNOT see rain that
# already fell (forecast-only) — moot for quoting since the midnight-ET
# ticker-date rule means dailies only ever quote the day BEFORE the
# measurement day (Jack 2026-07-28: tomorrow-only, never the radar-flow day).
RAIN_FAIR_ENABLE = os.environ.get("IMM_RAIN_FAIR_ENABLE", "1") == "1"
# Fair-gate exemption (Jack 2026-07-28, made ROLLING same night: "allowlist
# KXRAIN-26JUL30 and the NEXT DAY rain market (never the current day)"):
# the NEXT-ET-day daily event always quotes every in-band market — the NWS
# stand-aside never applies to it. The current-day event is already dead
# via the midnight rule (never quoted), and events further out keep the
# gate until they roll into next-day. IMM_RAIN_FAIR_EXEMPT adds hand-named
# extras. Band/cutoff/screens govern as usual.
RAIN_FAIR_EXEMPT_EVENTS = frozenset(
    s for s in os.environ.get("IMM_RAIN_FAIR_EXEMPT", "").split(",") if s)


def rain_fair_exempt(event_ticker: str, now_utc: datetime) -> bool:
    if event_ticker in RAIN_FAIR_EXEMPT_EVENTS:
        return True
    nxt = now_utc.astimezone(ET) + timedelta(days=1)
    return event_ticker == \
        f"{RAIN_FAIR_SERIES}-{nxt.strftime('%y%b%d').upper()}"


def curated_event(event_ticker: str, series: str, now_utc: datetime) -> bool:
    """The floor/hopeless-bypass tier. SINCE 2026-08-03 (Jack, TRUMPMENTION
    AUG03: "it should follow same rules as other markets") this is the
    rolling next-day rain event ONLY — an event_start_overrides entry now
    supplies the CUTOFF and nothing else; override events face the same
    floors, ladders and exits as everything else. (Supersedes the 7/24
    floor bypass and the 7/28 hopeless exemption for override events; the
    rain half keeps the 7/29 JUL30-NYC lesson.)"""
    return (series == RAIN_FAIR_SERIES
            and rain_fair_exempt(event_ticker, now_utc))
RAIN_FAIR_TOL_CENTS = _env_int("IMM_RAIN_FAIR_TOL_CENTS", 10)
RAIN_FAIR_TTL_MIN = _env_int("IMM_RAIN_FAIR_TTL_MIN", 240)
RAIN_FAIR_REFRESH_MIN = _env_int("IMM_RAIN_FAIR_REFRESH_MIN", 30)
RAIN_FAIR_SERIES = os.environ.get("IMM_RAIN_FAIR_SERIES", "KXRAIN")
RAIN_FAIR_FILE = os.environ.get(
    "IMM_RAIN_FAIR_FILE", os.path.join(STATUS_DIR, "rain_fair_values.json"))
_rain_fair_state: dict = {"mtime": 0.0, "fair": {}}   # (date_iso, city) -> (p, fetched_ts)

# RAIN DIRECTIONAL (Jack 2026-07-28: "if its misquoted, just take the
# directional bet... make it systematic with the normal 3/0/0 size"): when
# the fair gate finds the touch divergent (same TOL), TAKE the NWS side —
# RAIN_DIR_SIZE taker contracts at the touch, once per market per day,
# position rides to settlement (size < REDUCE_ONLY_MIN so no wind-down
# machinery touches it; the MM side is stood aside on these books by the
# gate, so there is nothing of ours to self-cross). Every entry appends to
# RAIN_DIR_LEDGER for the performance review (rain_dir_report.py joins
# settlements; the digest shows the running score). Manually-traded markets
# never reach the gate (the yield-to-human standoff exits the loop first).
RAIN_DIR_ENABLE = os.environ.get("IMM_RAIN_DIR_ENABLE", "1") == "1"
RAIN_DIR_SIZE = _env_int("IMM_RAIN_DIR_SIZE", 3)
RAIN_DIR_MAX_PER_DAY = _env_int("IMM_RAIN_DIR_MAX_PER_DAY", 30)  # contracts/24h
RAIN_DIR_LEDGER = os.environ.get(
    "IMM_RAIN_DIR_LEDGER", os.path.join(STATUS_DIR, "rain_directional_ledger.csv"))
RAIN_DIR_LEDGER_HEADER = ("ts,ticker,take_side,contracts,price_cents,fair_cents,"
                          "ext_bid,ext_ask,edge_cents,order_id\n")


def load_rain_fair() -> int:
    """Hot-reload RAIN_FAIR_FILE by mtime into _rain_fair_state. Returns the
    number of (date, city) entries loaded on a reload, else 0."""
    try:
        mtime = os.path.getmtime(RAIN_FAIR_FILE)
    except OSError:
        return 0
    if mtime == _rain_fair_state["mtime"]:
        return 0
    _rain_fair_state["mtime"] = mtime
    try:
        with open(RAIN_FAIR_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError) as e:
        log(f"[IMM] ! rain fair file unreadable: {e}")
        return 0
    fresh: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for date_iso, cities in (data.get("fair") or {}).items():
        for city, entry in (cities or {}).items():
            try:
                p = float(entry["p"])
                ts = parse_iso_utc(str(entry["fetched_at"]))
                if ts is not None and 0.0 <= p <= 1.0:
                    fresh[(str(date_iso), str(city))] = (p, ts.timestamp())
            except (KeyError, TypeError, ValueError):
                continue
    _rain_fair_state["fair"] = fresh
    log(f"[IMM] rain fair values reloaded: {len(fresh)} station-days")
    return len(fresh)


def rain_fair_p(ticker: str, now_ts: float) -> Optional[float]:
    """Fair P(YES) for a KXRAIN daily ticker (KXRAIN-26JUL29-SEA), or None
    when unavailable/stale — None must degrade to plain band quoting."""
    parts = ticker.split("-")
    if len(parts) != 3 or parts[0] != RAIN_FAIR_SERIES:
        return None
    try:
        date_iso = datetime.strptime(parts[1], "%y%b%d").date().isoformat()
    except ValueError:
        return None
    entry = _rain_fair_state["fair"].get((date_iso, parts[2]))
    if entry is None:
        return None
    p, fetched_ts = entry
    if now_ts - fetched_ts > RAIN_FAIR_TTL_MIN * 60:
        return None
    return p


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
        # Manual/file overrides are HOT-RELOADABLE (the daily task and --set
        # rewrite them mid-run) and cheap (dict lookup), so honor them live —
        # never serve a stale cached value. Caching one caused a changed INTC
        # override (5pm call -> 4pm release) to be ignored for the resolver's
        # 6h TTL while the bot kept quoting past the new cutoff (2026-07-23).
        if event_ticker in EVENT_START_OVERRIDES:
            return EVENT_START_OVERRIDES[event_ticker]
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


def apply_series_cutoff_adjustments(series: str, event_ticker: str,
                                    cutoff: Optional[datetime],
                                    close_time: Optional[datetime] = None,
                                    ) -> Optional[datetime]:
    """Series-level tighteners every cutoff PRODUCER must run: the
    CLOSE-ANCHORED rule (cutoff_from_close_min, e.g. hourly temp close-10),
    the early-stop (cutoff_before_event_min, e.g. rain 6pm-day-before) and
    the hard-expiry floor (Love Island 8:30pm). Exists because the 2026-07-29
    rain early-stop was applied only in refresh_universe — orphan-restored
    positions rebuilt their own cutoff via raw trade_cutoff_utc and kept
    reduce-only quoting past 6pm (the 7/14 duplicated-threshold class, in
    cutoff form).

    2026-08-04: the close-anchored rule was STILL missing here, so the same
    bug recurred one layer down — orphan-restored TEMP markets got cutoff =
    close instead of close-10 and quoted into the final ten minutes, the most
    informed window of the hour (7 fills observed as late as 6.7 min to
    close, on a -18c/contract-at-4am book). Pass close_time and the tightener
    is applied for BOTH producers."""
    ov = SERIES_OVERRIDES.get(series)
    # NOTE the event-day EXTENDER (event_day_cutoff_et) deliberately does NOT
    # live here — it shifts the ticker-date CANDIDATE inside trade_cutoff_utc,
    # before min() runs, so an occurrence-based cutoff still wins. Applying it
    # here could only ever compare against the already-minimised result and
    # would have quoted straight through a real event start.
    if ov and ov.cutoff_from_close_min is not None and close_time is not None:
        anchored = close_time - timedelta(minutes=ov.cutoff_from_close_min)
        cutoff = anchored if cutoff is None else min(cutoff, anchored)
    if ov and ov.cutoff_before_event_min is not None:
        td = parse_event_date(event_ticker)
        if td is not None:
            early = td - timedelta(minutes=ov.cutoff_before_event_min)
            cutoff = early if cutoff is None else min(cutoff, early)
    hard = series_hard_expiry_utc(series, event_ticker)
    if hard is not None:
        cutoff = hard if cutoff is None else min(cutoff, hard)
    return cutoff


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
        # EVENT-DAY EXTENDER (Jack 2026-08-04: "quote treasuries until 7:30am
        # EST"). Midnight is the right default for a market with a scheduled
        # RELEASE — be gone before the day the number drops. A Treasury yield
        # is not that: the underlying is continuously priced in a deep market
        # and the contract is a 3:30pm ET snapshot of it, so 00:00-07:30 ET is
        # no more informed than the evening before, while 07:30 puts the bot
        # out ahead of the 08:30 releases that DO move rates.
        #
        # It shifts the ticker-date CANDIDATE, deliberately, rather than the
        # finished cutoff: min() below then still lets a real
        # occurrence_datetime win, which a post-hoc adjustment could not do.
        # A caller that skips this function keeps midnight — failing toward
        # quoting LESS, which is the safe direction for a missed rule.
        ov = SERIES_OVERRIDES.get(series_of(event_ticker))
        if ov and ov.event_day_cutoff_et is not None:
            hh, mm = ov.event_day_cutoff_et
            td = td + timedelta(hours=hh, minutes=mm)
            if expected_expiration is not None:
                td = min(td, expected_expiration)
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

def levels_desc(levels: List[List[float]]) -> List[Tuple[int, float]]:
    """Orderbook side -> (price_cents, size) best price first (highest first;
    both YES-bid and NO-bid books quote their own price space)."""
    by: Dict[int, float] = {}
    for px, q in levels:
        by[int(px)] = by.get(int(px), 0.0) + float(q)
    return sorted(by.items(), key=lambda kv: -kv[0])


def side_reference_level(levels_best_first: List[Tuple[int, float]],
                         target: float) -> Optional[int]:
    """Post-2026-07-30 reference price for one side: the first level, best
    price first, where cumulative size reaches target/5. None if the walk
    never gets there (side can't qualify without more depth). Placing our
    own size AT the returned level cannot move the reference (levels above
    it are untouched), so it is a stable full-weight placement."""
    if target <= 0:
        return None
    cum = 0.0
    for px, size in levels_best_first:
        cum += size
        if cum >= target / 5.0:
            return px
    return None


def ladder_reference_prices(yes_levels: List[List[float]],
                            no_levels: List[List[float]],
                            target: float) -> Tuple[Optional[int], Optional[int]]:
    """(bid ref in YES cents, ask ref in YES-ASK cents) for atref placement;
    (None, None) when the mode is off or a side has no reference."""
    if LADDER_MODE != "atref" or target <= 0:
        return None, None
    rb = side_reference_level(levels_desc(yes_levels), target)
    rn = side_reference_level(levels_desc(no_levels), target)
    return rb, (100 - rn) if rn is not None else None


def _side_share(levels_best_first: List[Tuple[int, float]], own: Dict[int, float],
                target: float, df: float) -> Tuple[float, bool]:
    """Our expected score share for one side of the book.

    levels_best_first: (price_cents, total_size) best price first, sizes
    INCLUDING our own resting size. own: price -> our size. Returns
    (our_share, side_qualifies).

    Post-2026-07-30 program rules (CFTC amendment filed 2026-07-15): walk
    levels from the best price accumulating size; the REFERENCE price is the
    level where cumulative size first reaches target/5 (no longer simply the
    touch); every bid at or ABOVE the reference scores at full weight
    (N clamped to 0), bids below decay df^(ticks below reference); the
    qualifying walk continues until cumulative >= target and the side
    qualifies nobody if the book runs out first. The old touch>=99
    disqualifier is gone (only a missing best bid disqualifies)."""
    if not levels_best_first or target <= 0:
        return 0.0, False
    ref: Optional[int] = None
    accumulated = 0.0
    qualifying: List[Tuple[int, float]] = []
    for px, size in levels_best_first:
        qualifying.append((px, size))
        accumulated += size
        if ref is None and accumulated >= target / 5.0:
            ref = px
        if accumulated >= target:
            break
    else:
        return 0.0, False   # book thinner than target: side doesn't qualify
    total_score = 0.0
    our_score = 0.0
    for px, size in qualifying:
        w = df ** max(ref - px, 0)
        total_score += w * size
        our_score += w * min(own.get(px, 0.0), size)
    if total_score <= 0:
        return 0.0, False
    return our_score / total_score, True


def estimate_reward_share(yes_levels: List[List[float]], no_levels: List[List[float]],
                          own_orders: List[Tuple[str, int, float]],
                          target: float, df: float,
                          own_in_book: bool) -> Tuple[float, int]:
    """(our_fraction_of_pool, qualifying_sides). own_in_book=True when the
    orderbook read already contains our resting orders (live mode); False
    overlays them (dry run).

    Post-2026-07-30 rules: a snapshot only COUNTS when BOTH sides reach the
    target size ("two-sided liquidity" exclusion); a counted snapshot's total
    score is 2.0 (each side normalizes to 1.0), so our fraction is
    (our_yes_share + our_no_share) / 2 — and 0.0 whenever either side fails,
    because that snapshot is excluded outright (it also shrinks the paid pool
    via the non-excluded ratio; see coverage EMA at the call sites).
    `qualifying_sides` still reports 0/1/2 for observability."""
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
    if sides < 2:
        return 0.0, sides   # snapshot excluded: one qualifying side pays nobody
    return (share_yes + share_no) / 2.0, sides


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
                      levels: Optional[List[Tuple[int, int]]] = None,
                      ref_px: Optional[int] = None,
                      band: Optional[Tuple[int, int]] = None,
                      hour_mult: float = 1.0) -> List[Quote]:
    """Ladder behind (never improving) the join anchor. `anchor` is the best
    EXTERNAL price on our side; `opposite_best` the best external price on the
    other side (post-only: never cross it). Sizes come from `levels` (the
    market's series ladder; global LEVELS if omitted), shaved to `room`
    (contracts we may still acquire on this side if everything fills).

    `ref_px` (side's own price space: YES cents for bids, YES-ask cents for
    asks): the amended-rules reference level. In LADDER_MODE 'atref' the whole
    side collapses to one rung at that level — deepest full-weight price —
    clamped inside the series band and never improving the anchor."""
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
    # Safe-join (re-entry company/econ): on a tight book, never rest inside
    # the top SAFE_JOIN_OFFSET_TICKS of our side — the estimator overlays
    # this same clamp, so entry floors are tested net of the weight penalty.
    # An unreadable opposite side counts as tight (conservative).
    safe_cap: Optional[int] = None
    if series_safe_join(series_of(ticker)):
        spread = (opposite_best - anchor if book_side == "bid"
                  else anchor - opposite_best) if opposite_best is not None else None
        if spread is None or spread < SAFE_JOIN_MIN_SPREAD:
            safe_cap = (max(anchor - SAFE_JOIN_OFFSET_TICKS, STICKY_PRICE_MIN)
                        if book_side == "bid"
                        else min(anchor + SAFE_JOIN_OFFSET_TICKS, STICKY_PRICE_MAX))
    # `band` (2026-08-02): caller-supplied effective band — the quote loop
    # passes the member-widened 2-98 for sticky markets; default = series.
    pmin, pmax = band if band is not None else (
        series_price_min(series_of(ticker)), series_price_max(series_of(ticker)))
    if LADDER_MODE == "atref" and ref_px is not None:
        # Reference placement is EXEMPT from the series price band (Jack
        # 2026-08-01: "don't arbitrarily constrain to 5c... when it makes
        # sense to go lower", both sides). Safe by construction: bids only
        # ever rest AT/BELOW the touch and asks AT/ABOVE it, so the fill
        # the band guards against (touch-anchored quotes at extreme prices,
        # the 7/21 97c-bid incident) cannot occur here — deeper = cheaper
        # collateral and a smaller worst-case fill at identical full
        # weight. Pads (1c/99c) already price outside the band.
        total = sum(s for _t, s in levels)
        # hour_mult = the factor already inside `levels`; the cap bounds the
        # PRODUCT of the two size levers at TOTAL_SIZE_MULT_CAP
        total = int(round(total * capped_ref_mult(anchor, ref_px, book_side,
                                                  hour_mult=hour_mult)))
        count = min(total, int(room))
        if count <= 0:
            return quotes
        # Envelope for at-ref rungs = the caller band (member-widened and/or
        # deep-floor-relaxed), hard-bounded so 1c/99c stay pad-only and
        # nothing ever rests above the sticky cap.
        lo_env = max(pmin, RUNG_DEEP_FLOOR)
        hi_env = min(pmax, STICKY_PRICE_MAX)
        if book_side == "bid":
            px = min(anchor, max(ref_px, lo_env))
            if safe_cap is not None:
                px = min(px, safe_cap)
        else:
            px = max(anchor, min(ref_px, hi_env))
            if safe_cap is not None:
                px = max(px, safe_cap)
        quotes.append(Quote(ticker, book_side, px, count))
        return quotes
    for ticks, size in levels:
        px = anchor - ticks if book_side == "bid" else anchor + ticks
        if safe_cap is not None:
            px = min(px, safe_cap) if book_side == "bid" else max(px, safe_cap)
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
                     side_max: Optional[int] = None,
                     soft: Optional[float] = None,
                     hard: Optional[float] = None) -> float:
    """Inventory skew on top of hard caps: past `soft` net contracts the
    accumulating side is halved, past `hard` it is pulled entirely
    (defaults SKEW_SOFT/HARD; callers scale them for bigger family
    ladders so the skew geometry stays proportional)."""
    if not accumulating:
        return base_room
    if side_max is None:
        side_max = SIDE_MAX_CONTRACTS
    if soft is None:
        soft = SKEW_SOFT_CONTRACTS
    if hard is None:
        hard = SKEW_HARD_CONTRACTS
    a = abs(pos)
    if a >= hard:
        return 0.0
    if a >= soft:
        return min(base_room, side_max / 2.0)
    return base_room


def diff_orders(desired: List[Quote], resting: List[dict],
                order_ages: Dict[str, float], now_ts: float,
                preserve_tickers: Set[str] = frozenset(),
                touch_by_ticker: Optional[Dict[str, Tuple[Optional[int],
                                                          Optional[int]]]] = None,
                ) -> Tuple[List[Quote], List[str], List[Tuple[dict, Quote]]]:
    """Match desired quotes to resting orders. Three-way result
    (to_place, to_cancel, to_amend) since 2026-08-02:

    - exact/tolerance match (offsets: exact; atref: per-series hysteresis,
      never MORE aggressive than desired) -> keep untouched.
    - ASYMMETRIC CHASE (atref): a rung MORE aggressive than desired means
      the reference moved DEEPER — under amended-rules scoring everything
      at/above the reference earns identical full weight, so repricing buys
      zero reward and costs a book gap. Keep it while it is not leading the
      current touch (touch_by_ticker); without touch data fall through
      (strict behavior). TTL refresh still rewrites it eventually.
    - AMEND (atref rungs + pad resizes): a fresh rung on the right
      ticker+side whose price/size drifted the wrong way is amended IN
      PLACE (V2 endpoint, same order_id) instead of cancel+placed — the
      requote-downtime fix: the rung never leaves the book.
    - stale-by-TTL (ORDER_REFRESH_SECS) -> cancel + fresh place (amend
      cannot extend the exchange-side expiration).
    Orders on preserve_tickers (blind/fast-lane-skipped markets) are left
    untouched."""
    to_place: List[Quote] = []
    to_cancel: List[str] = []
    to_amend: List[Tuple[dict, Quote]] = []
    unmatched = list(desired)

    def matches(q: Quote, ticker: str, book_side: str, px: int,
                remaining: float) -> bool:
        if q.ticker != ticker or q.book_side != book_side:
            return False
        if LADDER_MODE == "atref" and not getattr(q, "is_pad", False):
            # per-series hysteresis (KXTEMP runs 0 — reprice on any ref move)
            if abs(px - q.price_cents) > series_atref_price_tol(series_of(q.ticker)):
                return False
            # never keep a rung MORE aggressive than desired via the plain
            # match — the asymmetric-chase check below owns that case
            if q.book_side == "bid" and px > q.price_cents:
                return False
            if q.book_side == "ask" and px < q.price_cents:
                return False
            tol = max(1.0, ATREF_COUNT_TOL_FRAC * q.count)
            return abs(remaining - q.count) <= tol
        return q.price_cents == px and round(remaining) == q.count

    def aggressive_keep(q: Quote, ticker: str, book_side: str, px: int,
                        remaining: float) -> bool:
        if q.ticker != ticker or q.book_side != book_side:
            return False
        if LADDER_MODE != "atref" or getattr(q, "is_pad", False):
            return False
        # Zero-tolerance series (KXTEMP) re-pin at the reference in BOTH
        # directions (Jack 2026-08-03, DCH T75.99: a kept 62c ask sat 30+
        # ticks toward the touch of an emptied book — reward-equivalent to
        # the deep reference but far more fill-exposed, which on temp is
        # the whole game). Amend-in-place makes the re-pin gapless.
        if series_atref_price_tol(series_of(ticker)) == 0:
            return False
        if touch_by_ticker is None:
            return False
        tol = max(1.0, ATREF_COUNT_TOL_FRAC * q.count)
        if abs(remaining - q.count) > tol:
            return False               # size drifted too: reprice properly
        tb, ta = touch_by_ticker.get(ticker, (None, None))
        # safe-join series keep their safety net CONTINUOUSLY: when the book
        # drifts toward a kept rung on a tight spread, the rung must stay
        # >= OFFSET ticks off the touch or be repriced (live audit 2026-08-02:
        # a gas bid ended 1 tick off after the touch dropped).
        bound_b, bound_a = tb, ta
        if series_safe_join(series_of(ticker)) and tb is not None and ta is not None \
                and (ta - tb) < SAFE_JOIN_MIN_SPREAD:
            bound_b = tb - SAFE_JOIN_OFFSET_TICKS
            bound_a = ta + SAFE_JOIN_OFFSET_TICKS
        if book_side == "bid":
            return px > q.price_cents and bound_b is not None and px <= bound_b
        return px < q.price_cents and bound_a is not None and px >= bound_a

    def amendable(q: Quote, ticker: str, book_side: str,
                  rest_is_pad: bool) -> bool:
        if q.ticker != ticker or q.book_side != book_side:
            return False
        if getattr(q, "is_pad", False) != rest_is_pad:
            return False               # rungs amend rungs; pads amend pads
        return LADDER_MODE == "atref" or rest_is_pad

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
        tk = o.get("ticker")
        match = next((q for q in unmatched
                      if matches(q, tk, book_side, px, remaining)), None)
        if match is not None and not stale:
            unmatched.remove(match)
            continue
        if not stale:
            keep = next((q for q in unmatched
                         if aggressive_keep(q, tk, book_side, px, remaining)),
                        None)
            if keep is not None:
                unmatched.remove(keep)
                continue
            rest_is_pad = (book_side == "bid" and px <= PAD_BID_CENTS) or \
                          (book_side == "ask" and px >= PAD_ASK_CENTS)
            am = next((q for q in unmatched
                       if amendable(q, tk, book_side, rest_is_pad)), None)
            if am is not None:
                unmatched.remove(am)
                to_amend.append((o, am))
                continue
        to_cancel.append(oid)

    to_place.extend(q for q in unmatched if q.ticker not in preserve_tickers)
    return to_place, to_cancel, to_amend


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
    # observed deep-reference size multipliers (atref mode), set by the
    # candidate estimator — the collateral reservation must scale rung sizes
    # by these or the budget under-reserves up to 2x (2026-08-02 audit)
    ref_mult_bid: float = 1.0
    ref_mult_ask: float = 1.0


@dataclass
class BotState:
    selected: Dict[str, MarketMeta] = field(default_factory=dict)
    managed_extra: Dict[str, MarketMeta] = field(default_factory=dict)  # reduce-only tail
    coverage_zero_streak: Dict[str, int] = field(default_factory=dict)  # sides<2 alert
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
    accrued_est: Dict[str, float] = field(default_factory=dict)   # ticker -> lifetime est
    # ticker -> ts the projection first went under the payout bar (cleared the
    # moment it recovers). The hopeless exit reads this so a DIP cannot evict —
    # see HOPELESS_SUSTAIN_SECS.
    hopeless_since: Dict[str, float] = field(default_factory=dict)
    #   reward accrued ($; live share x $/day integrated per cycle). Feeds the
    #   hopeless-exit / entry-floor credit: accrued + projection vs the $1 bar.
    rain_dir_done: Dict[str, float] = field(default_factory=dict)  # ticker -> entry ts
    #   (rain-directional once-per-market dedupe; persisted, pruned at 7d)
    programmed: Set[str] = field(default_factory=set)   # markets with a LIVE incentive
    #   program at the last universe refresh — the no-rent freeze (Jack 2026-07-26,
    #   KXRT: "why still quoting when the rewards have expired") keys off this
    place_uncertain: Dict[Tuple[str, str, int], float] = field(default_factory=dict)
    realized_baseline: float = 0.0       # lifetime realized at last daily roll
    last_mark: Dict[str, float] = field(default_factory=dict)   # ticker -> YES mid cents
    day_baseline: Optional[float] = None  # realized+unrealized at last daily roll
    halted_until: float = 0.0            # daily-loss halt (epoch); persisted
    pnl_carry: float = 0.0               # pnl_today carried across restarts (same
    #   roll-day only) — realized resets on restart, so baselines alone can't
    #   survive one; the halt used to hand every restart a fresh loss budget
    pnl_today_last: float = 0.0          # last measured pnl_today (for persist)
    balance_day_start: float = 0.0       # account balance at the daily anchor
    watchdog_streak: int = 0             # consecutive selected-but-not-resting cycles
    consecutive_errors: int = 0
    # daily counters (reset when the summary sends)
    cycles_today: int = 0
    placed_today: int = 0
    cancelled_today: int = 0
    errors_today: int = 0
    fills_today: float = 0.0
    reward_est_today: float = 0.0
    # Completed roll-days -> that day's total est reward (last 7). The digest
    # reads "last full day" from HERE (2026-08-03): reward_est_today is an
    # in-process counter and this bot restarts many times a day, so the
    # digest's old live-counter/summary-body sources reported a fraction of
    # the truth ($45.81 vs $959.47 measured on 8/3).
    reward_history: Dict[str, float] = field(default_factory=dict)
    # Cumulative REALIZED trading P&L across restarts (2026-08-03, Jack: "i do
    # hold multi-week inventory"). PnlTracker.realized resets with the process
    # while own_pos/own_avg persist, so nothing tracked lifetime realized —
    # and a multi-week position that SETTLES cannot be reconstructed from
    # fills either (fills carry no client_order_id and our_order_ids prunes at
    # 7 days). This counter folds in the tracker's delta each save, so
    # settlements booked by _settle_or_drop are captured permanently.
    realized_lifetime: float = 0.0
    realized_seen: float = 0.0            # tracker total already folded in
    #   (NOT persisted: the tracker starts empty each process)
    reward_est_lifetime: float = 0.0      # cumulative est reward (NOT reset at the
    #   daily roll; persisted so it survives restarts) — the digest's running total
    # PAID-BASIS twins of the three counters above. Kalshi does not pay a
    # market whose whole-program payout lands under $1.00 — proven against
    # Jack's 2026-08-04 statement: of 2,720 LIQUIDITY credits the minimum is
    # exactly $1.00 and NOT ONE falls below it, and predicting "one credit per market
    # whose estimate clears $1" reproduced the actual credit count exactly on
    # 94 of 99 events (272 predicted vs 273 actual rows). The raw counters
    # therefore book revenue on the ~30% of quoted markets that pay nothing.
    # A market is credited here only once its accrual crosses PAYOUT_FLOOR
    # (carrying its whole accrued-so-far across at that moment), which is the
    # floor applied exactly rather than approximated.
    reward_paid_today: float = 0.0
    reward_paid_lifetime: float = 0.0
    reward_paid_history: Dict[str, float] = field(default_factory=dict)
    # markets whose accrual has already crossed the floor (pruned with
    # accrued_est, so a settled market stops being carried)
    paid_crossed: Set[str] = field(default_factory=set)
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
        # ticker -> EMA of "snapshot would count" (both sides >= target).
        # Deliberately NOT persisted: warms up within ~an hour of cycles.
        self._coverage_ema: Dict[str, float] = {}
        self.resolver = EventStartResolver()
        self._foreign_resting: Dict[str, int] = {}
        self._shutdown_done = False
        self.wake_grace_until = 0.0   # epoch; cycles before this ran on a
        #   possibly half-connected post-sleep network (see run())
        self._reconciled = False      # one-time orphaned-own-fill cleanup pending
        self._reconcile_recheck_at = 0.0   # two-shot: second pass after sweep window
        self._est_peak: Dict[str, Tuple[float, float]] = {}   # ticker -> (est_total, ts)
        self._rain_fair_stood: Set[str] = set()   # rain-fair stand-asides (for edge logs)
        self._heartbeat = time.time()      # hang-watchdog liveness marker
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
            # realized_seen deliberately NOT restored: the tracker starts empty
            self.state.realized_lifetime = float(data.get("realized_lifetime") or 0.0)
            self.state.reward_history = {str(k): float(v) for k, v in
                                         (data.get("reward_history") or {}).items()}
            self.state.reward_paid_lifetime = float(
                data.get("reward_paid_lifetime") or 0.0)
            self.state.reward_paid_history = {str(k): float(v) for k, v in
                                              (data.get("reward_paid_history") or {}).items()}
            # Which markets already cleared the $1 floor MUST persist with
            # accrued_est: restoring the accrual without it would re-credit
            # every carried market's whole backlog on the first cycle after a
            # restart, inflating the paid basis by ~one full book per restart.
            self.state.paid_crossed = set(data.get("paid_crossed") or [])
            # Daily reward/contract-minutes survive restarts within the SAME
            # roll day (2026-08-03 fix): without this every restart zeroed the
            # counter the digest reports.
            if data.get("reward_day_key") == _halt_day_key(datetime.now(timezone.utc)):
                self.state.reward_est_today = float(data.get("reward_est_today") or 0.0)
                self.state.reward_paid_today = float(data.get("reward_paid_today") or 0.0)
                self.state.contract_minutes_today = float(
                    data.get("contract_minutes_today") or 0.0)
                if self.state.reward_est_today:
                    log(f"{self.tag} restored est reward today "
                        f"${self.state.reward_est_today:.2f} raw / "
                        f"${self.state.reward_paid_today:.2f} paid-basis "
                        f"(same roll-day restart)")
            self.state.sticky_prev = set(data.get("selected_tickers") or [])
            # Peak-entry memory survives restarts: it's in-memory otherwise, and
            # this bot restarts often (launcher-env changes, hang-watchdog), so
            # every restart used to wipe the 1h peak and make borderline markets
            # re-clear the floor from scratch for an hour. The read is already
            # TTL-guarded, so stale entries are harmless.
            self._est_peak = {str(t): (float(v[0]), float(v[1]))
                              for t, v in (data.get("est_peak") or {}).items()
                              if isinstance(v, (list, tuple)) and len(v) == 2}
            # Per-market accrued reward estimate: the hopeless-exit / entry
            # credit must survive restarts or every restart would re-evict
            # (or re-floor) markets that already banked most of their $1.
            self.state.accrued_est = {str(t): float(v) for t, v in
                                      (data.get("accrued_est") or {}).items()}
            self.state.hopeless_since = {str(t): float(v) for t, v in
                                         (data.get("hopeless_since") or {}).items()}
            # MIGRATION (2026-08-04, first load after the paid-basis counters
            # shipped): a state file written by the old code has accrued_est
            # but no paid_crossed, so every carried market sitting above the
            # floor would "cross" on the first cycle and dump its entire
            # backlog — accrued over days, already counted in the raw
            # counters — into TODAY's paid figure. Adopt their crossed status
            # silently instead; only accrual from here forward counts.
            if "paid_crossed" not in data:
                self.state.paid_crossed = {
                    t for t, v in self.state.accrued_est.items()
                    if v >= PAYOUT_FLOOR_DOLLARS}
                if self.state.paid_crossed:
                    log(f"{self.tag} paid-basis migration: adopted "
                        f"{len(self.state.paid_crossed)} market(s) already over "
                        f"${PAYOUT_FLOOR_DOLLARS:.2f} without back-crediting")
            # rain-directional once-per-market dedupe must survive restarts
            # (this bot restarts often) or every restart would re-take the
            # same divergent books.
            self.state.rain_dir_done = {str(t): float(v) for t, v in
                                        (data.get("rain_dir_done") or {}).items()}
            # Halt continuity (same roll-day only): a restart must NOT hand
            # the bot a fresh loss budget or clear an active halt. Use
            # --clear-halt (bot stopped) for a deliberate un-halt.
            if data.get("halt_day_key") == _halt_day_key(datetime.now(timezone.utc)):
                self.state.pnl_carry = float(data.get("pnl_today_carry") or 0.0)
                self.state.halted_until = float(data.get("halted_until") or 0.0)
                self.state.balance_day_start = float(data.get("balance_day_start") or 0.0)
                if self.state.halted_until > time.time():
                    log(f"{self.tag} restored ACTIVE daily-loss halt "
                        f"(pnl carry ${self.state.pnl_carry:+.2f})")
                elif abs(self.state.pnl_carry) > 0.005:
                    log(f"{self.tag} restored pnl_today carry "
                        f"${self.state.pnl_carry:+.2f} (same roll-day restart)")
            if self.state.known_tickers:
                log(f"{self.tag} restored {len(self.state.known_tickers)} known tickers")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"{self.tag} ! state restore failed ({e}); starting fresh")
        # After the main file (or its absence): fold in ids the previous run
        # journaled but never got to fold in itself.
        self._load_journal()

    def _fold_realized(self) -> None:
        """Accumulate the tracker's realized delta into the persistent
        lifetime counter (see BotState.realized_lifetime). Called on every
        save so a hard kill loses at most one cycle of realizations."""
        total = self.pnl.total_realized()
        delta = total - self.state.realized_seen
        if abs(delta) > 1e-9:
            self.state.realized_lifetime += delta
            self.state.realized_seen = total

    def _save_persist(self) -> None:
        try:
            self._fold_realized()
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
                           "reward_est_lifetime": self.state.reward_est_lifetime,
                           # lifetime realized trading P&L incl. settlements
                           "realized_lifetime": round(self.state.realized_lifetime, 4),
                           # daily reward must survive restarts (see BotState):
                           # anchored to the roll day so a NEW day starts clean
                           "reward_day_key": _halt_day_key(datetime.now(timezone.utc)),
                           "reward_est_today": round(self.state.reward_est_today, 4),
                           "reward_paid_today": round(self.state.reward_paid_today, 4),
                           "reward_paid_lifetime": round(
                               self.state.reward_paid_lifetime, 4),
                           "contract_minutes_today": round(
                               self.state.contract_minutes_today, 1),
                           # 60 days kept: the digest's daily table shows every
                           # prior day that earned (Jack 2026-08-03)
                           "reward_history": {k: round(v, 2) for k, v in sorted(
                               self.state.reward_history.items())[-60:]},
                           "reward_paid_history": {k: round(v, 2) for k, v in sorted(
                               self.state.reward_paid_history.items())[-60:]},
                           "halt_day_key": _halt_day_key(datetime.now(timezone.utc)),
                           "pnl_today_carry": round(self.state.pnl_today_last, 2),
                           "halted_until": self.state.halted_until,
                           "balance_day_start": round(self.state.balance_day_start, 2),
                           # peak-entry memory, pruned to TTL so it stays bounded
                           "est_peak": {t: [round(v[0], 4), round(v[1], 1)]
                                        for t, v in self._est_peak.items()
                                        if time.time() - v[1] <= EST_PEAK_TTL_SECS},
                           # accrued-est credit, pruned with known_tickers so
                           # settled markets don't grow it unboundedly
                           "accrued_est": {t: round(v, 4)
                                           for t, v in self.state.accrued_est.items()
                                           if v >= 1e-4
                                           and t in self.state.known_tickers},
                           # the sub-bar clock must survive restarts or this
                           # bot's ~20 deploys/day would keep resetting it and
                           # the hopeless exit could never fire at all
                           "hopeless_since": {
                               t: round(v, 1)
                               for t, v in self.state.hopeless_since.items()
                               if t in self.state.known_tickers},
                           # pruned on the SAME rule as accrued_est: a ticker
                           # that drops out of one must drop out of the other,
                           # or a re-listed ticker resumes "already paid"
                           "paid_crossed": sorted(
                               t for t in self.state.paid_crossed
                               if t in self.state.known_tickers),
                           "rain_dir_done": {t: round(v, 1)
                                             for t, v in self.state.rain_dir_done.items()
                                             if time.time() - v < 7 * 86400}}, f)
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
        self._heartbeat = time.time()      # see cancel_order heartbeat note
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
        # Heartbeat per order: a 1000-order sweep/wave legitimately outlasts
        # the hang-watchdog window; a WEDGED call still freezes these touches.
        self._heartbeat = time.time()
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

    def amend_order_inplace(self, o: dict, q: Quote, now_ts: float) -> bool:
        """Reprice/resize a resting order IN PLACE (V2 amend, 2026-08-02
        requote-downtime fix): the rung never leaves the book — no
        cancel->place gap — and the order_id survives, so ownership/fill
        matching and the crash journal need nothing new. V2 count semantics:
        total max-fillable = already-filled + desired remainder. Amend has
        no post_only; desired prices are already clamped to the last-read
        opposite touch upstream (a fast race can still take; accepted).
        order_ages is deliberately NOT touched: amend cannot extend the
        exchange-side expiration, so the 25-min TTL refresh must still
        rewrite the order for real."""
        self._heartbeat = time.time()
        oid = o.get("order_id", "")
        try:
            filled = float(o.get("fill_count_fp") or o.get("fill_count") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        total = float(q.count) + max(0.0, filled)
        label = (f"{q.ticker} {q.book_side.upper():4s} -> {q.count}x @ "
                 f"{q.price_cents}c (amend {oid[:8]})")
        if not self.live:
            so = self.state.sim_orders.get(oid)
            if so is None:
                return False
            so["yes_price"] = q.price_cents
            so["remaining_count"] = float(q.count)
            log(f"{self.tag} [DRY] amend {label}")
            return True
        try:
            self.client.amend_order(
                order_id=oid, ticker=q.ticker, book_side=q.book_side,
                yes_price_cents=q.price_cents, total_count=total)
            led = self.state.ledger.get(oid)
            if led is not None:
                led["yes_price"] = q.price_cents
                led["remaining_count"] = float(q.count)
            log(f"{self.tag} amended {label}")
            return True
        except HttpError as e:
            if e.status in (404, 409):
                # Order died mid-flight (filled/expired/cancelled): forget it;
                # the next cycle's diff places fresh.
                self.state.order_ages.pop(oid, None)
                self.state.ledger.pop(oid, None)
                return False
            log(f"{self.tag} ! amend failed {label}: {e}")
            return False
        except Exception as e:
            log(f"{self.tag} ! amend failed {label}: {e}")
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

    def rain_directional_take(self, t: str, ext_bid, ext_ask, fair_c: float,
                              bid_bad: bool, ask_bad: bool, now_ts: float) -> None:
        """Take the NWS side of a divergent rain book (see RAIN_DIR_ENABLE
        block). ask_bad: touch ask under fair-TOL -> buy YES at the ask.
        bid_bad: touch bid over fair+TOL -> buy NO at (100 - bid). Taker
        (post_only=False), once per market, capped per 24h, ledgered."""
        if not (RAIN_DIR_ENABLE and RAIN_DIR_SIZE > 0):
            return
        if t in self.state.rain_dir_done:
            return
        taken_24h = sum(1 for ts_ in self.state.rain_dir_done.values()
                        if now_ts - ts_ < 86400) * RAIN_DIR_SIZE
        if taken_24h + RAIN_DIR_SIZE > RAIN_DIR_MAX_PER_DAY:
            log(f"{self.tag} rain-DIR skip {t}: 24h cap "
                f"({taken_24h}/{RAIN_DIR_MAX_PER_DAY} contracts)")
            return
        if ask_bad and ext_ask is not None:
            take_side, px, kwargs = "yes", int(ext_ask), dict(
                side="yes", action="buy", yes_price=int(ext_ask))
            edge = fair_c - ext_ask
        elif bid_bad and ext_bid is not None:
            no_px = 100 - int(ext_bid)
            take_side, px, kwargs = "no", no_px, dict(
                side="no", action="buy", no_price=no_px)
            edge = ext_bid - fair_c
        else:
            return
        expiration_ts = int(now_ts + 600)
        cutoff_ts = self.state.cutoff_ts.get(t)
        if cutoff_ts is not None:
            expiration_ts = min(expiration_ts, int(cutoff_ts))
            if expiration_ts <= now_ts + 1:
                return
        client_order_id = f"{CLIENT_ORDER_PREFIX}-{RUN_ID}-{uuid.uuid4().hex[:12]}"
        oid = f"dry-{uuid.uuid4().hex[:8]}"
        if self.live:
            try:
                resp = self.client.create_order(
                    ticker=t, client_order_id=client_order_id,
                    count=RAIN_DIR_SIZE, type="limit", post_only=False,
                    expiration_ts=expiration_ts,
                    self_trade_prevention_type=STP_TYPE, **kwargs)
                oid = ((resp.get("order") or {}).get("order_id")
                       or resp.get("order_id", "?"))
                self.state.our_order_ids[oid] = now_ts
            except Exception as e:
                log(f"{self.tag} ! rain-DIR order failed {t}: {e}")
                return
        self.state.rain_dir_done[t] = now_ts
        try:
            new = not os.path.exists(RAIN_DIR_LEDGER)
            with open(RAIN_DIR_LEDGER, "a", encoding="utf-8") as f:
                if new:
                    f.write(RAIN_DIR_LEDGER_HEADER)
                f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')},"
                        f"{t},{take_side},{RAIN_DIR_SIZE},{px},{fair_c:.0f},"
                        f"{ext_bid if ext_bid is not None else ''},"
                        f"{ext_ask if ext_ask is not None else ''},"
                        f"{edge:.0f},{oid}\n")
        except OSError as e:
            log(f"{self.tag} ! rain-DIR ledger write failed: {e}")
        log(f"{self.tag} rain-DIR TAKE {t}: {take_side.upper()} {RAIN_DIR_SIZE}x @ "
            f"{px}c vs fair {fair_c:.0f}c (edge {edge:+.0f}c)"
            f"{'' if self.live else ' [DRY]'}")

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
        return (any(ticker.startswith(p) for p in SERIES_BLOCKLIST_PREFIXES)
                or series_of(ticker) in FREEZE_SERIES)

    @classmethod
    def _allowed(cls, ticker: str) -> bool:
        """Blocklist always wins; then the exact-series allowlist (MENTION
        suffix + named crypto series) unless ALLOWLIST_ONLY is off."""
        if cls._blocked(ticker):
            return False
        if not ALLOWLIST_ONLY:
            return True
        series = ticker.split("-")[0]
        return series in ALLOW_SERIES or series in EXTRA_ALLOW_SERIES or \
            any(series.endswith(suf) for suf in ALLOW_SERIES_SUFFIXES) or \
            any(series.startswith(p) for p in ALLOW_SERIES_PREFIXES)

    def refresh_universe(self, now_utc: datetime, positions: Dict[str, float]) -> None:
        now_ts = now_utc.timestamp()
        # Pick up call-time overrides and auto-enrolled series written since
        # the last refresh (daily task / hand edits) — cheap mtime checks,
        # no restart needed.
        load_file_event_overrides()
        load_extra_allow_series()
        load_rain_fair()
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
        # Live-program set for the no-rent freeze (empty on a failed/empty
        # read keeps the failsafe in the consumers: never mass-freeze on a
        # transient feed outage).
        if by_market:
            self.state.programmed = set(by_market)

        def ticker_cutoff_passed(t: str) -> bool:
            # (imm_quote_gaps.py mirrors this pre-filter — keep in sync.)
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

        # (imm_quote_gaps.py mirrors this meta construction — keep in sync.)
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
                    if event_ticker in EVENT_START_OVERRIDES:
                        # earnings call / disclosure release: tight, precise
                        # buffer — all orders expire OVERRIDE_BUFFER_MIN before.
                        buffer_min = OVERRIDE_BUFFER_MIN
                    elif ov and ov.start_buffer_min is not None:
                        buffer_min = ov.start_buffer_min
                    else:
                        buffer_min = EVENT_START_BUFFER_MIN
                    cutoff = resolved - timedelta(minutes=buffer_min)
                else:
                    cutoff = trade_cutoff_utc(
                        event_ticker, parse_iso_utc(m.get("occurrence_datetime", "")),
                        parse_iso_utc(m.get("expected_expiration_time", "")))
            # Series tighteners (early-stop + hard floor) — shared with the
            # orphan-restore path via apply_series_cutoff_adjustments.
            cutoff = apply_series_cutoff_adjustments(series, event_ticker, cutoff,
                                                     close_time=close_time)
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
            reason = self._screen(meta, now_utc,
                                  member=t in prev_selected)
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
                continue
            # Shared $1-min-payout projection for the entry floor AND the
            # hopeless exit: optimistic = accrued-so-far + max(current,
            # 1h-peak) remaining. NOTE the peak alone does NOT stop a dip from
            # evicting: `pts` only refreshes on a new HIGH, so a declining
            # estimate lets the peak expire and then one low reading is
            # enough. The explicit dip guard below is what actually holds.
            est_total = meta.est_dollars_per_day * _quotable_days(meta, now_utc)
            peak, pts = self._est_peak.get(meta.ticker, (0.0, 0.0))
            if now_ts - pts > EST_PEAK_TTL_SECS:
                peak = 0.0
            if est_total > peak:
                self._est_peak[meta.ticker] = (est_total, now_ts)
                peak = est_total
            accrued = self.state.accrued_est.get(meta.ticker, 0.0)
            reaches_min = accrued + max(est_total, peak) \
                >= series_min_est_total(meta.series)
            # DIP GUARD (Jack 2026-08-05). Track how long the projection has
            # been continuously under the bar; the exit below refuses to fire
            # until that exceeds HOPELESS_SUSTAIN_SECS. Any single reading at
            # or above the bar resets the clock, so a dip cannot evict.
            if reaches_min:
                self.state.hopeless_since.pop(meta.ticker, None)
            else:
                self.state.hopeless_since.setdefault(meta.ticker, now_ts)
            sub_bar_secs = now_ts - self.state.hopeless_since.get(meta.ticker, now_ts)
            if quote_all:
                # user wants EVERY market of the event (2026-07-12): exempt
                # from floors, the hopeless exit and the yield ranking.
                ranked.append(meta)
            elif meta.series in NO_NEW_SERIES \
                    and meta.ticker not in prev_selected:
                # no-new gate (Jack 2026-07-28): company family admits no
                # fresh markets; members above keep quoting via the branch
                # below until natural death.
                skipped["no_new"] = skipped.get("no_new", 0) + 1
            elif HOPELESS_EXIT and not reaches_min \
                    and sub_bar_secs >= HOPELESS_SUSTAIN_SECS \
                    and meta.ticker in prev_selected \
                    and meta.event_ticker not in FORCE_EVENTS \
                    and not curated_event(meta.event_ticker, meta.series, now_utc):
                # STICKY EXIT (Jack 2026-07-25): "<5% chance to reach $1 by
                # program end -> stop quoting, even if already started". The
                # deliberate exception to sticky retention — sub-$1 accrual
                # pays NOTHING, so riding a hopeless market to its natural
                # end is pure fill risk for zero reward. The accrued credit
                # keeps every market that's genuinely close to $1 (the 7/24
                # TRUMPMENTION rule). Wind-down continues reduce-only via
                # managed_extra. OVERRIDE-listed events are EXEMPT (both
                # states, so no flapping — Jack 2026-07-28, tele-rally 8/22
                # quoted: the drain rule targets the anonymous scrap tail,
                # not hand-curated event windows, and the estimator's
                # absolute $1 verdicts are least trustworthy on the short
                # windows overrides create).
                skipped["hopeless"] = skipped.get("hopeless", 0) + 1
            elif meta.ticker not in prev_selected \
                    and meta.event_ticker not in FORCE_EVENTS \
                    and meta.est_dollars_per_day < series_min_est_rate(meta.series) \
                    and (accrued + max(est_total, peak)) < RATE_FLOOR_TOTAL_ALT:
                # Rate-floored only when the market ALSO fails the horizon
                # escape. The projected total uses the same quantity as the
                # payout floor below (accrued + best of current/1h-peak) so the
                # two gates cannot disagree about what a market will earn.
                # Re-entry rate floor OUTRANKS event curation (Jack 2026-08-02
                # TLN audit: the auto-written company-disclosure override
                # curated KXTLN-26AUGGEN past the $2 bar at ~$0.45/day est).
                # The curated bypass below exists because SHORT windows sink
                # est_TOTAL — a per-day RATE has no such excuse. Members stay
                # sticky (rate dips never evict; hopeless owns exits).
                skipped["rate_floor"] = skipped.get("rate_floor", 0) + 1
            elif meta.ticker in prev_selected \
                    or meta.event_ticker in FORCE_EVENTS \
                    or curated_event(meta.event_ticker, meta.series, now_utc):
                # Sticky members and event-start-override events bypass the
                # entry floor / zero-yield / budget-race filters (original
                # sticky patch, commit 6f1e3d2 + the 2026-07-24 TRUMPMENTION
                # revival: a curated override window must not be vetoed by
                # the floor). Budget, pass-1 screens and yield-to-human
                # still govern.
                ranked.append(meta)
            elif meta.yield_per_contract <= 0:
                skipped["zero_yield"] = skipped.get("zero_yield", 0) + 1
            elif not reaches_min:
                # entry floor, now with the accrued credit: a re-admitted
                # market that already banked most of its $1 re-enters even
                # when the remaining window alone couldn't clear the bar.
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
            lv = hour_scaled_levels(meta.series, now_utc)
            # per-side deep-reference size multipliers (2026-08-02 audit):
            # atref rests up to 2x the spec size, so an unscaled reservation
            # under-charged the budget up to ~2x on mid-priced deep-ref books
            # (anchor pricing stays — actual rests at/below the anchor cost).
            worst = (ladder_collateral_dollars(bid, None, lv) * meta.ref_mult_bid
                     + ladder_collateral_dollars(None, ask, lv) * meta.ref_mult_ask)
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
        self._est_peak = {t: v for t, v in self._est_peak.items()
                          if now_ts - v[1] <= EST_PEAK_TTL_SECS}
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

    def _coverage(self, ticker: str, counted: bool) -> float:
        """Update + return the per-market counted-snapshot EMA.

        OBSERVABILITY ONLY since 2026-08-04 — it is NOT a factor in any reward
        estimate any more. It used to multiply the accrual rate as a proxy for
        the payout's non-excluded ratio, which double-counted the exclusion:
        `estimate_reward_share` already returns 0.0 for a snapshot that fails
        the two-sided target test, so an excluded snapshot contributes nothing
        to the integral before any EMA is applied. Writing out the program's
        arithmetic makes the double-count obvious — with C counted snapshots
        out of S, pool P, and per-snapshot share frac_i:

            payout = P x (C/S) x (sum_{i in C} frac_i)/C = (P/S) x sum_i frac_i

        i.e. exactly `sum(frac x dollars_per_day x dt)`, the raw integral. The
        (C/S) scaling cancels against the per-snapshot normalisation; applying
        an EMA of the same indicator on top is a second, unjustified haircut.

        Measured against Jack's 2026-08-04 statement on the only clean window
        (events that started after the post-amendment rewrite shipped
        2026-08-01 19:13Z, n=99 credited events, $960.96): the raw integral
        came in at 0.982x credited, the coverage-multiplied one at 1.057x.
        Applying the $1 payout floor as well lands the raw integral at 1.016x.
        """
        prev = self._coverage_ema.get(ticker)
        cur = 1.0 if counted else 0.0
        ema = cur if prev is None else prev + COVERAGE_EMA_ALPHA * (cur - prev)
        self._coverage_ema[ticker] = ema
        return ema

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
        ext_b, ext_a = external_best(yes_levels, no_levels)
        rb, ra = ladder_reference_prices(yes_levels, no_levels, meta.target_size)
        # capped so hour x ref <= TOTAL_SIZE_MULT_CAP; consumers (side rooms,
        # collateral reservation) inherit the cap through these meta fields
        _hm = hour_size_mult(meta.series, datetime.now(timezone.utc))
        meta.ref_mult_bid = capped_ref_mult(ext_b, rb, "bid", hour_mult=_hm)
        meta.ref_mult_ask = capped_ref_mult(ext_a, ra, "ask", hour_mult=_hm)
        if self.live and own_live:
            frac, sides = estimate_reward_share(
                yes_levels, no_levels, own_live,
                meta.target_size, meta.discount_factor, own_in_book=True)
            n_contracts = sum(r for _s, _p, r in own_live)
        else:
            lv = hour_scaled_levels(meta.series, datetime.now(timezone.utc))
            ext_bid, ext_ask, ref_bid_px, ref_ask_px = ext_b, ext_a, rb, ra
            base_side_max = sum(s for _t, s in lv)
            side_max_bid = int(round(base_side_max * meta.ref_mult_bid))
            side_max_ask = int(round(base_side_max * meta.ref_mult_ask))
            # same clamp the quote loop applies, or the estimate models a
            # ladder bigger than the bot will ever rest and overstates share
            side_max_bid, side_max_ask = clamp_side_max_to_position_cap(
                side_max_bid, side_max_ask, series_max_position(meta.series))
            quotes: List[Quote] = []
            # atref: the band gates PLACEMENT no longer follows the touch, so
            # a touch outside the band must not kill the side (rain books
            # trade whole cities under 5c).
            if ext_bid is not None and (
                    (LADDER_MODE == "atref" and ref_bid_px is not None)
                    or ext_bid >= series_price_min(meta.series)):
                quotes += build_side_ladder(meta.ticker, "bid", ext_bid, ext_ask,
                                            side_max_bid, levels=lv, ref_px=ref_bid_px,
                                            hour_mult=_hm)
            if ext_ask is not None and (
                    (LADDER_MODE == "atref" and ref_ask_px is not None)
                    or ext_ask <= series_price_max(meta.series)):
                quotes += build_side_ladder(meta.ticker, "ask", ext_ask, ext_bid,
                                            side_max_ask, levels=lv, ref_px=ref_ask_px,
                                            hour_mult=_hm)
            if not quotes:
                meta.est_frac = meta.est_dollars_per_day = meta.yield_per_contract = 0.0
                return True
            overlay = [(q.book_side, q.price_cents, float(q.count)) for q in quotes]
            # Model the depth pads on thin sides (global pad_to_target,
            # 2026-07-29): without them a side under the reward target reads
            # qualifies=False -> est 0 -> the market dies at the floor before
            # the quote loop could ever pad it. Pads stay OUT of n_contracts:
            # yield ranks incentive per near-touch contract; a 1c filler is
            # overhead, not deployed size.
            if series_pad_to_target(meta.series) and meta.target_size > 0 \
                    and pad_band_ok(meta.series, ext_bid, ext_ask):
                nt_bid = sum(q.count for q in quotes if q.book_side == "bid")
                nt_ask = sum(q.count for q in quotes if q.book_side == "ask")
                # mirror the quote loop's pad_missing_side (coverage-leak
                # fix) AND the pad distance gates: any quoting at all pads
                # BOTH sides for members, but only >= PAD_MIN_TICKS_BEHIND
                # the external touch
                if nt_bid > 0 or nt_ask > 0:
                    if ext_bid is not None \
                            and ext_bid - PAD_BID_CENTS >= PAD_MIN_TICKS_BEHIND:
                        n = pad_quantity(sum(sz for _px, sz in yes_levels) + nt_bid,
                                         meta.target_size)
                        if n > 0:
                            overlay.append(("bid", PAD_BID_CENTS, float(n)))
                    if ext_ask is not None \
                            and PAD_ASK_CENTS - ext_ask >= PAD_MIN_TICKS_BEHIND:
                        n = pad_quantity(sum(sz for _px, sz in no_levels) + nt_ask,
                                         meta.target_size)
                        if n > 0:
                            overlay.append(("ask", PAD_ASK_CENTS, float(n)))
            frac, sides = estimate_reward_share(
                yes_levels, no_levels, overlay,
                meta.target_size, meta.discount_factor, own_in_book=False)
            n_contracts = sum(q.count for q in quotes)
        self._coverage(meta.ticker, sides == 2)   # observability only (see _coverage)
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
                   # BLOCKLISTED = frozen entirely — not even reduce-only
                   # wind-down (Jack 2026-07-25: the gas retirement's
                   # wind-down joined pinned books, fire-selling +55/+31
                   # longs at 1-8c asks and covering shorts he wanted left
                   # alone). Positions ride to settlement; flatten by hand.
                   and not self._blocked(t)
                   # NO-RENT FREEZE (Jack 2026-07-26, KXRT after its movie
                   # programs expired): a market with no LIVE incentive
                   # program gets no orders either — quotes exist only where
                   # rent exists. Empty programmed set = failsafe open.
                   and (not self.state.programmed or t in self.state.programmed)
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
                    cutoff=apply_series_cutoff_adjustments(
                        t.split("-")[0], event_ticker,
                        trade_cutoff_utc(
                            event_ticker,
                            parse_iso_utc(m.get("occurrence_datetime", "")),
                            parse_iso_utc(m.get("expected_expiration_time", ""))),
                        # close_time is what makes the close-anchored rule
                        # (temp close-10) work on THIS producer too
                        close_time=parse_iso_utc(m.get("close_time", ""))),
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

    def _screen(self, meta: MarketMeta, now_utc: datetime,
                member: bool = False) -> Optional[str]:
        """Hard screens; returns a skip-reason or None if quotable.
        `member`: the 5-min cutoff pre-buffer exists to stop FRESH entry
        seconds before the end — members quote to the true cutoff (Jack
        2026-08-03: "bot stopped quoting 15min before"; the buffer was
        compounding with the 10-min cutoff into an effective 15)."""
        now_ts = now_utc.timestamp()
        if meta.cutoff is not None:
            buf = timedelta(minutes=0 if member else CUTOFF_SCREEN_BUFFER_MIN)
            if now_utc >= meta.cutoff - buf:
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
        if MIN_VOLUME_CONTRACTS > 0 and meta.volume < MIN_VOLUME_CONTRACTS:
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

    def _check_balance_floor(self, now_utc: datetime) -> bool:
        """True = floor breached and the bot halted. Anchors the day's
        starting balance on first sight; failure to read = no action."""
        try:
            bal = float(self.client.get_balance().get("balance_dollars") or 0.0)
        except Exception as e:
            log(f"{self.tag} ! balance read failed ({e}); floor check skipped")
            return False
        if bal <= 0:
            return False
        if self.state.balance_day_start <= 0:
            self.state.balance_day_start = bal
            return False
        drop = self.state.balance_day_start - bal
        if drop < BALANCE_DROP_HALT:
            return False
        next_day_et = (now_utc.astimezone(ET) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.state.halted_until = next_day_et.astimezone(timezone.utc).timestamp()
        n = self.cancel_all_bot_orders()
        self._save_persist()
        self.alerter.alert(
            "balance_floor", f"ACCOUNT balance dropped ${drop:.0f} since the "
            f"daily anchor (${self.state.balance_day_start:.0f} -> ${bal:.0f}) "
            f">= ${BALANCE_DROP_HALT:.0f}; cancelled {n} IMM orders and halted "
            f"to next ET day. NOTE: the account is shared — the cause may be "
            f"another bot, manual trading, or a withdrawal. --clear-halt to "
            f"resume deliberately.", key="balance_floor")
        return True

    def run_cycle(self, fast_only: bool = False) -> None:
        # fast_only = a FAST-LANE mini-cycle (see FAST_LANE_SECS): same managed
        # set, same resting read, but only fast-lane series are (re)quoted and
        # everything periodic (universe refresh, accrual, bench, watchdog,
        # cycle log, counters) is deferred to the next full cycle.
        now_utc = datetime.now(timezone.utc)
        now_ts = now_utc.timestamp()
        self._heartbeat = now_ts

        if os.path.exists(HALT_FILE):
            n = self.cancel_all_bot_orders()
            log(f"{self.tag} HALT file present ({HALT_FILE}); cancelled {n}; idle")
            return
        if self.state.halted_until > now_ts:
            log(f"{self.tag} daily-loss halt active until "
                f"{datetime.fromtimestamp(self.state.halted_until, timezone.utc)}; idle")
            return
        # Account-balance hard floor: shared-account backstop against ANY
        # bot's malfunction (or this one's blind spots). Anchored at the
        # daily roll; deposits/withdrawals also move it — the alert says so.
        if BALANCE_DROP_HALT > 0 and self.live and self._check_balance_floor(now_utc):
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
        if not self._reconciled and not in_grace and self.live and not fast_only:
            self._reconcile_orphaned_fills(positions)
            self._reconciled = True
            self._reconcile_recheck_at = now_ts + 240
        elif (self._reconciled and self._reconcile_recheck_at
                and now_ts >= self._reconcile_recheck_at
                and not in_grace and self.live and not fast_only):
            self._reconcile_recheck_at = 0.0
            self._reconcile_orphaned_fills(positions)
        # Release standoffs whose manual activity is gone — the market's own
        # divergence/foreign-order must be clear AND no sibling market of its
        # event is still manual (event-level yield). The yielded market never
        # re-enters the managed loop, so it must be released here.
        manual_evts = self.manual_events(positions)
        if not in_grace and not fast_only:
            for st in list(self.state.manual_standoff):
                manual = abs(positions.get(st, 0.0) - self.pnl.pos.get(st, 0.0))
                foreign_n = self._foreign_resting.get(st, 0) if self.live else 0
                if (manual < MANUAL_STANDOFF_CONTRACTS and not foreign_n
                        and self._event_of(st) not in manual_evts):
                    self.state.manual_standoff.pop(st, None)
                    log(f"{self.tag} {st}: manual activity cleared; market eligible again")
            self.refresh_universe(now_utc, positions)
        if not fast_only:
            self.restore_orphan_metas(positions)

        # Managed set = selected + any market we still hold inventory in.
        managed: Dict[str, MarketMeta] = dict(self.state.selected)
        for t, meta in list(self.state.managed_extra.items()):
            if self._blocked(t):
                # frozen series (see restore_orphan_metas): flush any entry
                # that predates the blocklisting so no wind-down orders rest
                self.state.managed_extra.pop(t, None)
            elif self.state.programmed and t not in self.state.programmed:
                # NO-RENT FREEZE: the market's incentive program ended, so
                # wind-down quoting ends with it (Jack 2026-07-26, KXRT).
                # Re-programming re-admits it through normal selection.
                self.state.managed_extra.pop(t, None)
            elif abs(positions.get(t, 0.0)) < REDUCE_ONLY_MIN_CONTRACTS:
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
        event_room_buy = {e: event_cap_contracts(e) - n for e, n in event_net.items()}
        event_room_sell = {e: event_cap_contracts(e) + n for e, n in event_net.items()}
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
                if self._is_pad_price(parsed[0], parsed[1]):
                    continue      # pads don't charge event room (see below)
                ev = managed[t].event_ticker
                if parsed[0] == "bid":
                    event_room_buy[ev] = event_room_buy.get(ev, event_cap_contracts(ev)) \
                        - order_remaining(o)
                else:
                    event_room_sell[ev] = event_room_sell.get(ev, event_cap_contracts(ev)) \
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
        # per-market external touch this cycle — the asymmetric-chase bound
        # in diff_orders (keep aggressive-drifted rungs while not leading)
        touch_map: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
        cycle_rows: List[str] = []        # cycle-logger panel (η/J calibration data)
        reward_frac_sum = 0.0
        cycle_rate: Dict[str, float] = {}   # ticker -> live est $/day this cycle
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
            # Fast tick: only fast-lane series are (re)quoted; everything else
            # is untouched this tick (its resting orders are preserved through
            # the diff below — see `preserve`).
            if fast_only and not series_fast_lane(meta.series):
                continue
            t = meta.ticker
            ev = meta.event_ticker
            pos = positions.get(t, 0.0)
            n_left = max(event_markets_left.get(ev, 1), 1)
            event_markets_left[ev] = n_left - 1
            share_buy = max(event_room_buy.get(ev, event_cap_contracts(ev)), 0.0) / n_left
            share_sell = max(event_room_sell.get(ev, event_cap_contracts(ev)), 0.0) / n_left

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
            # AAA PRINT BLACKOUT: a recurring scheduled release reprices this
            # book, so stand fully aside across it and resume after. Unlike
            # the cutoff this does NOT deselect — the market is still ours,
            # it just holds no orders while the print lands.
            if series_in_blackout(meta.series, now_utc):
                n = self.cancel_market_orders(t, resting)
                if n:
                    log(f"{self.tag} {t}: AAA print blackout; cancelled {n}")
                continue
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
            touch_map[t] = (ext_bid, ext_ask)

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
            # Member band (5-93 since 8/3); fresh keeps the series band.
            pmin_s, pmax_s = member_price_band(
                series_of(t), t in self.state.selected)
            # PER-SIDE top-in-band (Jack 2026-08-03 "Do 1", CHIH T69.99: a
            # live 3x29 book was fully stood down because its wide spread
            # put the bid touch under 5c): a side whose OWN touch is outside
            # the band stands down alone — its rungs die as diff-unmatched,
            # its pad stays to qualify the snapshot — while the healthy side
            # keeps earning. BOTH sides out (the Austin 96x98 case) still
            # stands the whole market down.
            bid_in_band = ext_bid is None or pmin_s <= ext_bid <= pmax_s
            ask_in_band = ext_ask is None or pmin_s <= ext_ask <= pmax_s
            if not bid_in_band and not ask_in_band:
                self.cancel_market_orders(t, resting)
                continue
            # Deep-rung floor: on a HEALTHY book (both touches present and
            # in-band) rungs may follow the reference below 5c, down to
            # RUNG_DEEP_FLOOR (Jack 2026-08-03).
            healthy = (ext_bid is not None and ext_ask is not None
                       and bid_in_band and ask_in_band)
            rung_lo = RUNG_DEEP_FLOOR if healthy else pmin_s

            # RAIN FAIR-VALUE GATE (Jack 2026-07-28; reworked same day —
            # "my primary goal is to stay near the top of the orderbooks
            # to get incentive rewards"): reward credit halves per tick
            # behind the touch, so the NWS fair must never re-price quotes
            # off the join. It is a SANITY SCREEN only: quotes always join
            # the touch unchanged; the gate stands the market down entirely
            # when the touch itself fights the forecast on the side that
            # would fill us badly — bid touch above fair+TOL (paying over
            # fair) or ask touch below fair-TOL (selling under fair). A
            # one-side breach parks BOTH sides (one-sided quoting earns no
            # reward and eats pickoff risk); sticky selection keeps the
            # market, so quoting auto-resumes when book and forecast agree
            # again. Missing/stale fair (TTL in rain_fair_p) -> gate open ->
            # plain band behavior.
            if RAIN_FAIR_ENABLE and meta.series == RAIN_FAIR_SERIES \
                    and not rain_fair_exempt(meta.event_ticker, now_utc):
                p_fair = rain_fair_p(t, now_ts)
                if p_fair is not None:
                    fair_c = p_fair * 100.0
                    bid_bad = (ext_bid is not None
                               and ext_bid > fair_c + RAIN_FAIR_TOL_CENTS)
                    ask_bad = (ext_ask is not None
                               and ext_ask < fair_c - RAIN_FAIR_TOL_CENTS)
                    if bid_bad or ask_bad:
                        if t not in self._rain_fair_stood:
                            self._rain_fair_stood.add(t)
                            log(f"{self.tag} rain-fair stand-aside {t}: book "
                                f"{ext_bid}x{ext_ask} vs fair {fair_c:.0f}c "
                                f"(tol {RAIN_FAIR_TOL_CENTS}c)")
                        self.cancel_market_orders(t, resting)
                        # divergent book = the systematic directional entry
                        # (Jack 2026-07-28); manual-standoff markets never
                        # reach here, so his own bets are never crossed.
                        self.rain_directional_take(
                            t, ext_bid, ext_ask, fair_c, bid_bad, ask_bad, now_ts)
                        continue
            if t in self._rain_fair_stood:
                self._rain_fair_stood.discard(t)
                log(f"{self.tag} rain-fair resume {t}")

            # Past-cutoff managed markets (only reduce-only EXTRAS can reach
            # here — selected members die at the _screen): cancel and go
            # silent. Without this, a restored rain position kept reduce-only
            # churn running past the 6pm early stop (2026-07-29, NYC).
            if meta.cutoff is not None and now_utc >= meta.cutoff:
                self.cancel_market_orders(t, resting)
                continue
            # Rooms: hard per-market cap, event share, then skew — all using
            # this market's SERIES ladder/cap (Love Island runs a bigger flat
            # 5/5/5 ladder with a tighter 50-contract net cap). For quote_all
            # series the cap/skew track the bot's OWN book, so the user's
            # coexisting manual position doesn't shrink the bot's room.
            lv = hour_scaled_levels(meta.series, now_utc)
            hm = hour_size_mult(meta.series, now_utc)
            # deep-reference size multiplier feeds the side_max component of
            # room (position/event caps and skew still bind unscaled);
            # capped so hour x ref <= TOTAL_SIZE_MULT_CAP
            ref_bid_px, ref_ask_px = ladder_reference_prices(
                yes_levels, no_levels, meta.target_size)
            base_side_max = sum(s for _t, s in lv)
            side_max_bid = int(round(base_side_max *
                                     capped_ref_mult(ext_bid, ref_bid_px, "bid",
                                                     hour_mult=hm)))
            side_max_ask = int(round(base_side_max *
                                     capped_ref_mult(ext_ask, ref_ask_px, "ask",
                                                     hour_mult=hm)))
            maxpos = series_max_position(meta.series)
            side_max_bid, side_max_ask = clamp_side_max_to_position_cap(
                side_max_bid, side_max_ask, maxpos)
            cap_pos = own_pos if quote_all else pos
            room_buy = min(maxpos - cap_pos, share_buy, side_max_bid)
            room_sell = min(maxpos + cap_pos, share_sell, side_max_ask)
            fam_m = applied_mention_mult(meta.series)
            room_buy = skewed_side_room(room_buy, cap_pos, accumulating=cap_pos > 0,
                                        side_max=side_max_bid,
                                        soft=SKEW_SOFT_CONTRACTS * fam_m,
                                        hard=SKEW_HARD_CONTRACTS * fam_m)
            room_sell = skewed_side_room(room_sell, cap_pos, accumulating=cap_pos < 0,
                                         side_max=side_max_ask,
                                         soft=SKEW_SOFT_CONTRACTS * fam_m,
                                         hard=SKEW_HARD_CONTRACTS * fam_m)
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
            # atref: placement no longer follows the touch, so a touch
            # outside the band must not kill the side (see build_side_ladder).
            # pmin_s/pmax_s carry the member-widened band (2-98 for sticky).
            if ext_bid is not None and room_buy > 0 and bid_in_band:
                px_ok = (LADDER_MODE == "atref" and ref_bid_px is not None) \
                    or ext_bid >= pmin_s
                if px_ok:
                    mq.extend(build_side_ladder(t, "bid", ext_bid, ext_ask, room_buy,
                                                levels=lv, ref_px=ref_bid_px,
                                                band=(rung_lo, pmax_s),
                                                hour_mult=hm))
            if ext_ask is not None and room_sell > 0 and ask_in_band:
                px_ok = (LADDER_MODE == "atref" and ref_ask_px is not None) \
                    or ext_ask <= pmax_s
                if px_ok:
                    mq.extend(build_side_ladder(t, "ask", ext_ask, ext_bid, room_sell,
                                                levels=lv, ref_px=ref_ask_px,
                                                band=(rung_lo, pmax_s),
                                                hour_mult=hm))

            # Depth padding: on a side we're actually quoting whose total depth
            # is below the reward target, add throwaway contracts at the 1c/99c
            # mark so the whole side (and thus our near-touch ladder) qualifies.
            if series_pad_to_target(meta.series) and meta.target_size > 0 \
                    and pad_band_ok(meta.series, ext_bid, ext_ask):
                mq.extend(self._pad_quotes(
                    t, mq, yes_levels, no_levels, own, meta.target_size,
                    # coverage-leak fix: rent-earning members pad the rungless
                    # side too (one-sided snapshots pay nobody); reduce-only
                    # tails keep the old single-side behavior
                    pad_missing_side=(t in self.state.selected
                                      and not reduce_only),
                    ext_bid=ext_bid, ext_ask=ext_ask))

            desired.extend(mq)
            if mq:
                quoted += 1
                # pads excluded: the event cap bounds NET INVENTORY risk, and
                # a 1c/99c pad's worst case is ~1c/contract. Counting them
                # burned the whole event's buy room after a few padded thin
                # strikes and silently killed every later sibling's YES side
                # (live bug 2026-07-29, TRUMPMENTIONB: ask-only on ~20
                # strikes the morning pads went global).
                bought = sum(q.count for q in mq
                             if q.book_side == "bid" and not q.is_pad)
                sold = sum(q.count for q in mq
                           if q.book_side == "ask" and not q.is_pad)
                event_room_buy[ev] = event_room_buy.get(ev, event_cap_contracts(ev)) - bought
                event_room_sell[ev] = event_room_sell.get(ev, event_cap_contracts(ev)) - sold

            # Reward-share estimate for status/summary + zero-share benching.
            # Live: our resting orders are already inside the fetched book.
            # Dry: overlay the DESIRED ladder (sim resting ~= desired; using
            # both would double-count).
            est_own = own if self.live else \
                [(q.book_side, q.price_cents, float(q.count)) for q in mq]
            frac, sides = estimate_reward_share(
                yes_levels, no_levels, est_own,
                meta.target_size, meta.discount_factor, own_in_book=self.live)
            self._coverage(t, sides == 2)   # observability only (see _coverage)
            # Coverage alert (2026-08-02): a member quoting into a snapshot
            # that can't count is the worst rent-per-risk state — page after
            # N consecutive FULL cycles (pads should make this ~impossible).
            if not fast_only and t in self.state.selected and mq \
                    and pad_band_ok(meta.series, ext_bid, ext_ask):
                if sides < 2:
                    streak = self.state.coverage_zero_streak.get(t, 0) + 1
                    self.state.coverage_zero_streak[t] = streak
                    if streak == COVERAGE_ALERT_CYCLES:
                        self.alerter.alert(
                            "coverage", f"{t}: quoted but only {sides} side(s) "
                            f"qualify for {streak} cycles (side under target "
                            f"{meta.target_size:.0f} despite pads?) — accruing "
                            f"ZERO while carrying fill risk", key=t)
                else:
                    self.state.coverage_zero_streak.pop(t, None)
            reward_frac_sum += frac * meta.dollars_per_day
            cycle_rate[t] = frac * meta.dollars_per_day
            cycle_rows.append(
                f"{now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')},{t},"
                f"{ext_bid if ext_bid is not None else ''},"
                f"{ext_ask if ext_ask is not None else ''},"
                f"{sum(q for _p, q in yes_levels):.0f},"
                f"{sum(q for _p, q in no_levels):.0f},"
                f"{meta.target_size:.0f},{frac:.5f},{sides},"
                f"{pos:.1f},{own_pos:.1f},{meta.dollars_per_day:.2f},"
                f"{sum(q.count for q in mq)}\n")
            if t in self.state.selected and not reduce_only and not fast_only:
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
        # Accrual integrates on FULL cycles only: a fast tick computes rates
        # for the fast-lane subset alone, and advancing the shared accrue
        # stamp on it would steal that interval from every other market's
        # integral (hopeless-exit credit corruption).
        if not fast_only:
            if self.state.reward_accrue_at:
                dt_days = (now_ts - self.state.reward_accrue_at) / 86400.0
                accrued = reward_frac_sum * dt_days
                self.state.reward_est_today += accrued
                self.state.reward_est_lifetime += accrued
                # Per-market accrual (same integral, split by ticker) — the
                # hopeless-exit / floor credit: accrued + projection vs $1.
                paid_delta = 0.0
                for t_, rate in cycle_rate.items():
                    if rate > 0:
                        prev_acc = self.state.accrued_est.get(t_, 0.0)
                        new_acc = prev_acc + rate * dt_days
                        self.state.accrued_est[t_] = new_acc
                        # PAID basis: nothing counts until this market's own
                        # accrual clears the $1 program floor, and the whole
                        # backlog lands in the cycle that crosses it.
                        if t_ in self.state.paid_crossed:
                            paid_delta += new_acc - prev_acc
                        elif new_acc >= PAYOUT_FLOOR_DOLLARS:
                            self.state.paid_crossed.add(t_)
                            paid_delta += new_acc
                self.state.reward_paid_today += paid_delta
                self.state.reward_paid_lifetime += paid_delta
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
        # pnl_carry composes the halt across restarts: realized resets with
        # the process, so the persisted carry (last measured pnl_today, same
        # roll-day) is added to the fresh measurement.
        pnl_today = total_pnl - self.state.day_baseline + self.state.pnl_carry
        self.state.pnl_today_last = pnl_today
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
            if not fast_only:
                self._write_cycle_log(cycle_rows)
            return

        # Fast tick: non-fast managed markets built no `desired` this tick —
        # preserve their resting orders through the diff or it would cancel
        # every one of them as unmatched.
        preserve = blind if not fast_only else \
            blind | {mt for mt, mm in managed.items()
                     if not series_fast_lane(mm.series)}
        to_place, to_cancel, to_amend = diff_orders(
            desired, resting, self.state.order_ages, now_ts,
            preserve_tickers=preserve, touch_by_ticker=touch_map)
        log(f"{self.tag} {quoted}/{len(managed)} mkts quoted, {len(resting)} resting, "
            f"{len(to_amend)} amend, {len(to_cancel)} cancel, {len(to_place)} place, "
            f"est ${reward_frac_sum:.2f}/day reward share, P&L today ${pnl_today:+.2f} "
            f"(real {realized:+.2f}/unreal {unrealized:+.2f}) "
            f"{'[fast] ' if fast_only else ''}{'' if self.live else '[DRY RUN]'}")

        # Watchdog: markets selected but the book nearly empty for several
        # consecutive cycles — the silent-death signature (dead cutoffs,
        # missing overrides, API trouble). Page, don't wait to be noticed.
        if fast_only:
            pass   # watchdog streaks count FULL cycles only (stable semantics)
        elif (len(self.state.selected) >= WATCHDOG_MIN_SELECTED
                and len(resting) < WATCHDOG_MIN_RESTING):
            self.state.watchdog_streak += 1
            if self.state.watchdog_streak == WATCHDOG_CYCLES:
                self.alerter.alert(
                    "watchdog", f"{len(self.state.selected)} markets selected but "
                    f"only {len(resting)} orders resting for {WATCHDOG_CYCLES} "
                    f"cycles — quoting looks silently dead (cutoffs? overrides? "
                    f"API?)", key="watchdog")
        else:
            self.state.watchdog_streak = 0

        # Amends FIRST (they're the urgent repricing and never free/consume
        # cap room), then cancels, then fresh placements.
        for o_am, q_am in to_amend[:MAX_PLACEMENTS_PER_CYCLE]:
            self.amend_order_inplace(o_am, q_am, now_ts)
        if len(to_amend) > MAX_PLACEMENTS_PER_CYCLE:
            log(f"{self.tag} amend cap: {len(to_amend) - MAX_PLACEMENTS_PER_CYCLE} "
                f"deferred to next cycle")

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
        if not fast_only:
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
                    own: List[Tuple[str, int, float]], target: float,
                    pad_missing_side: bool = False,
                    ext_bid: Optional[int] = None,
                    ext_ask: Optional[int] = None) -> List[Quote]:
        """Throwaway depth-padding at the 1c/99c mark so each side we quote
        reaches the reward target size. Computed on depth EXCLUDING our own pad
        (netted out / not-yet-in-book) to avoid a self-referential churn loop.
        By default only pads a side we have near-touch quotes on.
        pad_missing_side (2026-08-02 coverage-leak fix, SELECTED markets):
        also pad a side with NO rung whenever the OTHER side is quoted — a
        one-sided-qualified snapshot pays NOBODY, so the quoted side was
        carrying full fill risk for zero rent (the sides==1 class: 29 mkts
        in today's cycle log). A lone 1c/99c pad "leads" nothing worth
        leading and un-excludes the snapshot; if it ever fills it does so at
        the best possible price."""
        pads: List[Quote] = []
        nt_bid = sum(q.count for q in near_touch if q.book_side == "bid")
        nt_ask = sum(q.count for q in near_touch if q.book_side == "ask")
        own_pad_bid = sum(r for bs, px, r in own if self._is_pad_price(bs, px)
                          and bs == "bid")
        own_pad_ask = sum(r for bs, px, r in own if self._is_pad_price(bs, px)
                          and bs == "ask")
        yes_depth = sum(sz for _px, sz in yes_levels)
        no_depth = sum(sz for _px, sz in no_levels)
        # Distance gates (Jack 2026-08-03): a pad only rests when it sits
        # >= PAD_MIN_TICKS_BEHIND behind its side's EXTERNAL touch — a 1c pad
        # under a 1-2c best bid is the market itself, bought up easily.
        bid_pad_safe = (ext_bid is not None
                        and ext_bid - PAD_BID_CENTS >= PAD_MIN_TICKS_BEHIND)
        ask_pad_safe = (ext_ask is not None
                        and PAD_ASK_CENTS - ext_ask >= PAD_MIN_TICKS_BEHIND)
        if bid_pad_safe and (nt_bid > 0 or (pad_missing_side and nt_ask > 0)):
            # live: book already holds our near-touch, subtract only our pad;
            # dry: sim orders aren't in the book, add this cycle's near-touch.
            basis = (yes_depth - own_pad_bid) if self.live else (yes_depth + nt_bid)
            n = pad_quantity(basis, target)
            if n > 0:
                pads.append(Quote(ticker, "bid", PAD_BID_CENTS, n, is_pad=True))
        if ask_pad_safe and (nt_ask > 0 or (pad_missing_side and nt_bid > 0)):
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
            # Same hour-scaled shape as the quote loop that built q — an
            # unscaled cap here would block every boosted rung with
            # side_cap/level_cap alerts (the duplicated-threshold class).
            # In atref mode the construction layer may further scale a rung
            # by up to REF_DEPTH_MAX_MULT (deep-reference sizing, 2026-08-01
            # — this gate blocked every scaled rung for ~an hour, pads-only
            # books on fresh events); bracket by the max legitimate
            # multiplier — the precise per-book cap already ran in the
            # room/ladder layer, this is the runaway backstop.
            lv_now = hour_scaled_levels(
                series, datetime.fromtimestamp(now_ts, tz=timezone.utc))
            cap_mult = REF_DEPTH_MAX_MULT if LADDER_MODE == "atref" else 1.0
            side_cap = sum(s for _t, s in lv_now) * cap_mult
            # atref collapses the side's WHOLE ladder to one price level by
            # design, so the per-level bracket is the side bracket — the
            # per-rung max only applies to offsets-shaped ladders. (2026-08-02:
            # temp's multi-rung 5/2/2 override default collapsed to a 14-18
            # lot single rung > max-level 10 and every temp strike was
            # level_cap-blocked overnight.)
            max_level_size = side_cap if LADDER_MODE == "atref" \
                else max(s for _t, s in lv_now)
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
            # paid basis = the raw integral with the exchange's $1-per-market
            # program floor applied; this is what Kalshi actually credits
            "reward_paid_today": round(s.reward_paid_today, 2),
            "reward_paid_lifetime": round(s.reward_paid_lifetime, 2),
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
                f"(${s.reward_paid_today:.2f} paid basis) "
                f"({cm:,.0f} contract-min{eff}) | "
                f"realized today ${realized_today:+.2f} (lifetime "
                f"${self.pnl.total_realized():+.2f}), unrealized ${unrealized:+.2f} "
                f"on {inv:.0f} carried, fills {s.fills_today:.0f} | "
                f"inventory: {pos_str} | cycles {s.cycles_today}, placed {s.placed_today}, "
                f"cxl {s.cancelled_today}, errs {s.errors_today} | alerts: {alert_str}")
        s.cycles_today = s.placed_today = s.cancelled_today = s.errors_today = 0
        s.fills_today = 0.0
        # Record the COMPLETED day before zeroing — this is what the digest
        # reports as "last full day" (restart-proof, unlike the live counter).
        # The roll fires ~5am CT on day D covering ~5am D-1 -> 5am D, so the
        # period is labelled D-1.
        completed = (datetime.now(timezone.utc).astimezone(CT).date()
                     - timedelta(days=1)).isoformat()
        s.reward_history[completed] = round(s.reward_est_today, 2)
        s.reward_history = dict(sorted(s.reward_history.items())[-60:])
        s.reward_paid_history[completed] = round(s.reward_paid_today, 2)
        s.reward_paid_history = dict(sorted(s.reward_paid_history.items())[-60:])
        s.reward_est_today = 0.0
        s.reward_paid_today = 0.0
        s.contract_minutes_today = 0.0
        # roll both loss-halt windows + the restart-carry and balance anchor
        s.realized_baseline = self.pnl.total_realized()
        s.day_baseline = self.pnl.total_realized() + unrealized
        s.pnl_carry = 0.0
        s.pnl_today_last = 0.0
        s.balance_day_start = 0.0          # re-anchors on the next cycle
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
        if not once:
            def _hang_watchdog():
                while True:
                    time.sleep(30)
                    wedge = time.time() - self._heartbeat
                    if wedge > CYCLE_HANG_EXIT_SECS:
                        log(f"{self.tag} !! no cycle heartbeat for {wedge:.0f}s "
                            f"— wedged syscall? hard-exiting for launcher "
                            f"relaunch (state is journaled)")
                        os._exit(86)
            threading.Thread(target=_hang_watchdog, daemon=True,
                             name="hang-watchdog").start()
        if RAIN_FAIR_ENABLE and not once:
            # Fair-value refresher: fetch NWS forecasts OFF the trading thread
            # (a wedged 20s HTTP call must never stall a cycle / trip the hang
            # watchdog) and talk to the quote loop only through the JSON file,
            # like every other external writer. Import failure just means rain
            # quotes anchor to the book, as before this feature.
            def _rain_fair_refresh():
                try:
                    import rain_fair
                except Exception as e:
                    log(f"{self.tag} ! rain-fair refresher disabled: {e}")
                    return
                while True:
                    try:
                        ok, fail = rain_fair.write_fair_file(RAIN_FAIR_FILE)
                        log(f"{self.tag} rain-fair refresh: {ok} stations ok"
                            + (f", {fail} FAILED" if fail else ""))
                    except Exception as e:
                        log(f"{self.tag} ! rain-fair refresh failed: {e}")
                    time.sleep(max(300, RAIN_FAIR_REFRESH_MIN * 60))
            threading.Thread(target=_rain_fair_refresh, daemon=True,
                             name="rain-fair").start()
        if RAIN_FAIR_ENABLE:
            log(f"rain-fair gate: {RAIN_FAIR_SERIES} at-touch, tol "
                f"{RAIN_FAIR_TOL_CENTS}c, ttl {RAIN_FAIR_TTL_MIN}m, "
                f"refresh {RAIN_FAIR_REFRESH_MIN}m, file {RAIN_FAIR_FILE}")
        log(f"ladder {LEVELS} per side ({SIDE_MAX_CONTRACTS}/side, "
            f"mention x{MENTION_SIZE_MULT:g}), "
            f"caps: market ±{MAX_POSITION_CONTRACTS:g}, event ±{MAX_EVENT_CONTRACTS:g} "
            f"(mention-scaled), "
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
                sleep_total = POLL_SECS * backoff if self.state.consecutive_errors else POLL_SECS
                # Fast-lane: chunk the sleep window and re-quote ONLY fast-lane
                # series (KXTEMP) between chunks — see FAST_LANE_SECS. Skipped
                # during error backoff; fast-tick errors log without touching
                # the consecutive-error failsafe (full cycles own that).
                deadline = time.time() + sleep_total
                while True:
                    remain = deadline - time.time()
                    if remain <= 0:
                        break
                    fast_ok = (FAST_LANE_SECS > 0
                               and not self.state.consecutive_errors
                               and not stopping["flag"]
                               and any(series_fast_lane(m.series)
                                       for m in self.state.selected.values()))
                    if not fast_ok:
                        time.sleep(remain)
                        break
                    time.sleep(min(FAST_LANE_SECS, remain))
                    if stopping["flag"] or time.time() >= deadline:
                        break
                    try:
                        self.run_cycle(fast_only=True)
                    except Exception as e:
                        log(f"{self.tag} ! fast-lane cycle error: {e!r}")
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
    ap.add_argument("--clear-halt", action="store_true",
                    help="deliberately clear a persisted daily-loss/balance halt "
                         "and the pnl carry (run with the bot STOPPED)")
    args = ap.parse_args(argv)

    if args.clear_halt:
        path = IncentiveMarketMaker.PERSIST_PATH
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log(f"cannot read state file ({e}); nothing to clear")
            return 1
        log(f"clearing halt (was halted_until={data.get('halted_until')}, "
            f"pnl carry={data.get('pnl_today_carry')})")
        data["halted_until"] = 0.0
        data["pnl_today_carry"] = 0.0
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
        log("halt cleared — the bot starts with a FRESH loss budget on its "
            "next start; run this only while the bot is stopped")
        return 0

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

    if HOUR_SIZE_MULTS:
        log(f"[IMM] hour-size multipliers (ET hour -> x): "
            f"{dict(sorted(HOUR_SIZE_MULTS.items()))}; excluded prefixes: "
            f"{','.join(HOUR_MULT_EXCLUDE) or '(none)'}")
    for _pfx, _hrs in SERIES_HOUR_MULTS:
        _by_mult: Dict[float, List[int]] = {}
        for _h, _m in sorted(_hrs.items()):
            _by_mult.setdefault(_m, []).append(_h)
        log(f"[IMM] per-series hour multipliers {_pfx}*: "
            + "; ".join(f"x{m} at ET {','.join(str(h) for h in hs)}"
                        for m, hs in sorted(_by_mult.items())))

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
