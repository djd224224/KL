#!/usr/bin/env python3
"""Quoting policy for the hourly-temp market maker (KXTEMPMIAH): pure logic.

Jack 2026-10-15: "yes build F, which is a superset of G right?" -- after the
shadow test (TEMP_SHADOW.md) showed variant F_full netting ~+$73/hour. This
module IS the policy: temp_shadow_sim replays it against the recording, and
the live executor runs the same functions against the exchange, so the
shadow's numbers describe the code that trades. Stdlib only: no I/O, no
clock, no replay or exchange imports.

Conventions (TEMP_SHADOW.md "Interfaces"):
  * Prices are integer cents in their own book's space: a YES bid at 45 is
    45, a NO bid at 55 is 55 (= a YES ask at 45). Book levels are
    Dict[int, float] (cents -> contracts) and are the EXTERNAL book, without
    our own orders (external_book() strips ours from a live book).
  * A strike market "above K" pays YES iff the index > K. An event's strikes
    are nested: with strikes ascending, outcome scenario j (j = 0..n) = the
    index above strikes 0..j-1 and below strikes j..n-1.
  * Times are epoch seconds; tau = minutes to close.

The rules (TEMP_SHADOW.md "Policy and variants"); each has a switch in P:
  live strikes [live]          fair 10-90c: per side size_full = floor(q_live *
                               g(tau)) at the side's reward reference price
                               (cumulative size >= target/5), never crossing;
                               g(tau) is 1 above live_tau_full, tapers linearly
                               to live_tau_min_frac at live_stop_min, 0 below.
  fair guard [fair_guard]      the live price is capped at floor(100*fair_side
                               - buffer), buffer = max(guard_min_c, guard_k *
                               slope_c); slope_c = the fair's price move in
                               cents per 0.36 F index step (FairModel.slopes;
                               normal_slope_c() is the plain-normal fallback).
  landing window [window_mode] when the orders' resting interval touches
                               second-of-minute [win_lo, win_hi] (station
                               readings land), 'pull' rests no live quotes,
                               'back' rests them back_ticks lower, 'none'
                               ignores the window.
  decided strikes [decided]    fair < 3% or > 97%: the longshot side rests
                               q_pad at 1c; the favorite side joins its
                               reference with q_fav when its fair >= fav_min_p,
                               capped by fair - buffer (always guarded).
                               Near-decided strikes (3-10% / 90-97%) are quoted
                               as live at half size [near_decided_half]; that
                               is part of the decided feature (needs decided,
                               not live) and books as class "decided".
  qualification pads [qual_pads]  on a market we quote, a 1c order lifting a
                               thin side to target + pad_extra.
  skew [skew]                  live/near quotes with fair in 5-30c or 70-95c:
                               NO-bid size x(1+skew), YES-bid x(1-skew).
  risk, pre-trade              per market |net YES| <= pos_cap (the adding
                               side is trimmed, worst prices first); event
                               worst-case loss: each side's orders, filled in
                               full, keep the loss in that side's losing
                               scenarios <= event_wcl_cap (a bid at p risks p
                               cents a contract); a side whose losing scenarios
                               are already at the cap stops. This bounds FILLED
                               positions only: with nothing filled, every
                               strike may rest up to the cap, so the orders
                               that RESTS can lose more than event_wcl_cap if
                               only the losing side fills (an 8-strike F book
                               at tau 50: $1,662 vs the $1,500 cap).
  resting WCL [wcl_resting]    opt-in (default off: the shadow measured F
                               without it): one event budget for what rests --
                               in every scenario j, the filled loss plus every
                               desired order that loses in j (YES on strikes
                               >= j, NO on strikes < j), filled in full, stays
                               <= event_wcl_cap; the orders losing in the worst
                               scenario are scaled down proportionally until
                               none exceeds it (trim_resting_wcl).
  program_only                 quote only while the market's reward program is
                               active during the orders' resting interval.
  quote hours [quote_hours_et] Jack 2026-10-16 ("1": favorite-only and
                               overnight-favorite-only in the shadow): a list
                               of ET clock hours; an event is quoted only when
                               the ET hour of its SETTLEMENT (the ticker's
                               hour, KXTEMPMIAH-26OCT1607 = 07:00 ET) is in
                               it (quote_hour_ok; et_hour is DST-safe without
                               tz files: US Eastern, EDT from the second
                               Sunday of March 07:00Z to the first Sunday of
                               November 06:00Z). Outside them nothing rests
                               on any of the event's markets (the program
                               test fails; reason "off_hours"). None (the
                               default) = every hour. FAV_night = settle
                               hours 20-23 and 0-7 (events settling 20:00-
                               07:00 ET).
  pad hold [pad_hold_open_s]   Jack 2026-10-16: "hold the pads for the first 3
                               minutes of each hour going forward" (live G's
                               first hour: sweepers took 4,659 of its 1c
                               fillers within ~45 s of the program opening).
                               No 1c filler -- longshot pad ("pad") or
                               qualification pad ("qual") -- while the orders'
                               resting interval starts before the event's open
                               + pad_hold_open_s (pad_hold_flag; the open is
                               the market open, close - 3600 when the caller
                               does not know it). Favorite joins, live and
                               near-decided quotes are unaffected. 0 = off.

F vs G ("F is a superset of G"): F_full = q_live 600 + decided + skew 0.3;
G_decided_only = live off + decided. On a decided strike (fair < 3% / > 97%)
F's orders are exactly G's. On a live strike G rests nothing. On a
near-decided strike both quote it as live at half size, but F's is
q_live 600 * 0.5 = 300 a side (skewed inside 5-10% / 90-95%) vs G's 150
unskewed. Both share one event risk budget (pos_cap, event_wcl_cap), so F's
live fills can trim the decided orders G would rest. So: structurally a
superset, not order-for-order (test_temp_mm_policy.TestFSupersetOfG).
G_decided_only (what runs live since 10/16) also holds its 1c pads for the
first 180 s after the open (pad_hold_open_s); G0_nohold is the G before the
hold, kept for comparison. F has no hold.

FAV_only / FAV_night (Jack 2026-10-16, "1"): the favorite joins of G with no
1c filler at all -- no longshot pad (q_pad 0), no qualification pads, no
near-decided half-size quotes. Under the sweep-aware fill model (swp,
temp_shadow_sweep) every 1c-filler variant loses (sweepers take the fillers
within seconds) and only these favorite joins are positive in expectation,
almost all of it in the events settling 20:00-07:00 ET: FAV_night is
FAV_only quoted in those settle hours only (quote_hours_et).

API:
  DEFAULT_POLICY, VARIANTS, make_policy(variant=None, **overrides) -> dict
  decide(P, ctx: EventContext, markets: List[MarketState]) -> EventDecision
  diff_orders(resting, dy, dn) -> List[OrderOp]
  resting_scenario_losses, trim_resting_wcl (wcl_resting; decide only -- the
  sim's per-market cache does not apply it and refuses the switch)
  building blocks (the sim calls these directly): BookView, quote_market,
  quote_price, classify, guard_buffer, normal_slope_c, g_tau, live_size,
  overlap_window, window_flag, program_flag, pad_hold_flag, risk_headroom,
  worst_case_loss, et_hour, quote_hour_ok, check_quote_hours,
  scenario_losses, reference_price, best_price, would_cross, external_book.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

POLICY_VERSION = "3"   # 2: pad_hold_open_s (10/16); 3: quote_hours_et, FAV_only /
                      # FAV_night (10/16)

DEFAULT_POLICY = dict(live=True, q_live=300, live_tau_full=40.0, live_tau_min_frac=0.3, live_stop_min=3.0, fair_guard=True, guard_min_c=1, guard_k=1.0, window_mode="pull", win_lo=20, win_hi=45, back_ticks=2, decided=False, q_pad=1000, q_fav=300, fav_min_p=0.97, near_decided_half=True, qual_pads=True, pad_extra=50, skew=0.0, pos_cap=1500, event_wcl_cap=1500.0, program_only=True, react_s=0.5, order_latency_s=0.15, maker_fee_per_contract=0.0, wcl_resting=False, pad_hold_open_s=0.0, quote_hours_et=None)
VARIANTS = {"A_live300_nowin": dict(window_mode="none"), "B_live300_pull": dict(), "C_live300_back": dict(window_mode="back"), "D_live600_pull": dict(q_live=600), "E_live300_pull_decided": dict(decided=True), "F_full": dict(q_live=600, decided=True, skew=0.3), "G_decided_only": dict(live=False, decided=True, pad_hold_open_s=180.0), "G0_nohold": dict(live=False, decided=True), "H_noguard_pull": dict(fair_guard=False), "I_pads_only": dict(live=False, decided=True, q_fav=0), "J_decided_fav100": dict(live=False, decided=True, q_fav=100), "K_full_wcl500": dict(q_live=600, decided=True, skew=0.3, event_wcl_cap=500.0), "L_decided_pad3000": dict(live=False, decided=True, q_pad=3000), "FAV_only": dict(live=False, decided=True, q_pad=0, qual_pads=False, near_decided_half=False), "FAV_night": dict(live=False, decided=True, q_pad=0, qual_pads=False, near_decided_half=False, quote_hours_et=[20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7])}

TARGET = 1000.0      # target_size_fp of every KXTEMPMIAH program (10/14)
DF = 0.5             # discount_factor_bps 5000
STEP_F = 0.36        # one index step (F) = one whole-degree-C station step
EPS = 1e-9
WINDOW_MODES = ("none", "pull", "back")
SIDES = ("yes", "no")

_SQRT2PI = math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------
# Policy dicts
# --------------------------------------------------------------------------

def make_policy(variant: Optional[str] = None, **overrides) -> dict:
    """DEFAULT_POLICY, then VARIANTS[variant] (if given), then overrides.
    Unknown variants or keys raise ValueError."""
    P = dict(DEFAULT_POLICY)
    if variant is not None:
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}; known: {sorted(VARIANTS)}")
        P.update(VARIANTS[variant])
    bad = set(overrides) - set(DEFAULT_POLICY)
    if bad:
        raise ValueError(f"unknown policy keys {sorted(bad)}")
    P.update(overrides)
    return P


def _check_policy(P: dict) -> None:
    missing = set(DEFAULT_POLICY) - set(P)
    if missing:
        raise ValueError(f"policy is missing keys {sorted(missing)} (use make_policy)")
    bad = set(P) - set(DEFAULT_POLICY)
    if bad:
        raise ValueError(f"unknown policy keys {sorted(bad)}")
    if P["window_mode"] not in WINDOW_MODES:
        raise ValueError(f"window_mode must be one of {WINDOW_MODES}, not {P['window_mode']!r}")
    check_quote_hours(P)


def check_quote_hours(P: dict) -> None:
    """quote_hours_et must be None or a list / tuple of whole ET hours 0..23
    (ValueError otherwise)."""
    hrs = P.get("quote_hours_et")
    if hrs is None:
        return
    if isinstance(hrs, (str, bytes)) or not isinstance(hrs, (list, tuple)):
        raise ValueError(f"quote_hours_et must be None or a list of ET hours, not {hrs!r}")
    for h in hrs:
        if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23:
            raise ValueError(f"quote_hours_et holds {h!r}: ET hours are whole numbers 0..23")


# --------------------------------------------------------------------------
# Book helpers (same rules as temp_shadow_scoring; the tests cross-check)
# --------------------------------------------------------------------------

def _floor(x: float) -> int:
    return int(math.floor(x + 1e-9))


def reference_price(levels: Dict[int, float], target: float = TARGET) -> Optional[int]:
    """The first level, best price first, where cumulative size reaches
    target/5; None if never (or target <= 0). = scoring.reference_price."""
    if target <= 0:
        return None
    cum = 0.0
    for px, size in sorted(levels.items(), reverse=True):
        cum += size
        if cum >= target / 5.0:
            return px
    return None


def best_price(levels: Optional[Dict[int, float]]) -> Optional[int]:
    """Best (highest) price with size > 1e-9, or None. = scoring.best_price."""
    if not levels:
        return None
    best = None
    for px, q in levels.items():
        if q > EPS and (best is None or px > best):
            best = px
    return best


def would_cross(book_yes: Dict[int, float], book_no: Dict[int, float],
                side: str, price: int) -> bool:
    """Would a post-only bid cross the opposite book? A YES bid at `price`
    crosses iff price + best NO bid >= 100; a NO bid iff price + best YES bid
    >= 100. An empty opposite side never crosses. = scoring.would_cross."""
    s = side.lower()
    if s in ("yes", "yes_bid"):
        opp = best_price(book_no)
    elif s in ("no", "no_bid"):
        opp = best_price(book_yes)
    else:
        raise ValueError(f"side must be 'yes' or 'no', not {side!r}")
    return opp is not None and price + opp >= 100


def external_book(book_yes: Dict[int, float], book_no: Dict[int, float],
                  own_yes: Optional[Dict[int, float]] = None,
                  own_no: Optional[Dict[int, float]] = None
                  ) -> Tuple[Dict[int, float], Dict[int, float]]:
    """A live book (which includes our resting orders) minus our own resting
    sizes, per price; levels left <= 1e-9 are dropped. The policy always
    takes the external book (the shadow's recorded book never held us)."""
    def strip(levels, own):
        out = {}
        own = own or {}
        for px, q in (levels or {}).items():
            left = float(q) - float(own.get(px, 0.0))
            if left > EPS:
                out[int(px)] = round(left, 2)
        return out
    return strip(book_yes, own_yes), strip(book_no, own_no)


class BookView:
    """What the quoting rules read from one market's external book. Built
    once per book version (the sim shares them across variants)."""
    __slots__ = ("by", "bn", "best_y", "best_n", "sum_y", "sum_n", "ref_y",
                 "ref_n", "mid", "two_sided")

    def __init__(self, by: Dict[int, float], bn: Dict[int, float], target: float = TARGET):
        self.by, self.bn = by, bn
        self.best_y = max(by) if by else None
        self.best_n = max(bn) if bn else None
        self.sum_y = sum(by.values()) if by else 0.0
        self.sum_n = sum(bn.values()) if bn else 0.0
        self.ref_y = reference_price(by, target) if by else None
        self.ref_n = reference_price(bn, target) if bn else None
        if self.best_y is not None and self.best_n is not None:
            self.mid = (self.best_y + 100 - self.best_n) / 2.0
            self.two_sided = True
        elif self.best_y is not None:
            self.mid, self.two_sided = float(self.best_y), False
        elif self.best_n is not None:
            self.mid, self.two_sided = float(100 - self.best_n), False
        else:
            self.mid, self.two_sided = None, False


# --------------------------------------------------------------------------
# Inputs: fair, guard slope, time, window, program
# --------------------------------------------------------------------------

def clean_prob(f) -> Optional[float]:
    """A model P(YES) as the policy takes it: None if missing or not finite,
    else clamped to [0, 1]."""
    return None if f is None or not math.isfinite(float(f)) else min(1.0, max(0.0, float(f)))


def clean_slope(g) -> Optional[float]:
    """A guard slope (cents per index step): None if missing or not finite."""
    return float(g) if g is not None and math.isfinite(float(g)) else None


def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / _SQRT2PI


def normal_slope_c(mu: float, sigma: float, thr: float, step_f: float = STEP_F) -> float:
    """The spec's plain-normal guard slope: 100 * phi((thr - mu)/sigma) /
    sigma * step_f (cents per index step) for a model that only gives
    (mu, sigma). FairModel.slopes (the fat-tail mixture on fair()'s own cut)
    is preferred: on its core sigma this is 0.85x the slope at the centre and
    up to 2.8x too small at 3-7% / 93-97%."""
    return 100.0 * _phi((thr - mu) / sigma) / sigma * step_f


def guard_buffer(P: dict, slope_c: Optional[float]) -> float:
    """Cents kept between a guarded price and the fair: max(guard_min_c,
    guard_k * slope_c)."""
    buf = float(P["guard_min_c"])
    if slope_c is not None:
        buf = max(buf, float(P["guard_k"]) * slope_c)
    return buf


def classify(fx: float) -> str:
    """'live' (10-90%), 'near' (3-10% / 90-97%) or 'decided'."""
    if 0.10 <= fx <= 0.90:
        return "live"
    if 0.03 <= fx <= 0.97:
        return "near"
    return "decided"


def g_tau(P: dict, tau: float) -> float:
    """The live-size taper at tau minutes to close."""
    full, stop, lo = P["live_tau_full"], P["live_stop_min"], P["live_tau_min_frac"]
    if tau >= full:
        return 1.0
    if tau < stop:
        return 0.0
    if full <= stop:
        return 1.0
    return lo + (1.0 - lo) * (tau - stop) / (full - stop)


def live_size(P: dict, tau: float) -> int:
    """size_full: live contracts a side at tau minutes to close."""
    return math.floor(float(P["q_live"]) * g_tau(P, tau) + 1e-9)


def overlap_window(a: float, b: float, lo: float, hi: float) -> bool:
    """Does [a, b) touch second-of-minute window [lo, hi+1) in any minute?
    (a, b epoch seconds: a % 60 is the second of the minute.)"""
    base = a - (a % 60.0)
    while base < b:
        if a < base + hi + 1.0 and b > base + lo:
            return True
        base += 60.0
    return False


def window_flag(P: dict, rest_from: float, rest_until: float) -> bool:
    """Do orders resting over [rest_from, rest_until) touch the landing
    window? Always False under window_mode 'none'."""
    return P["window_mode"] != "none" and overlap_window(
        rest_from, rest_until, float(P["win_lo"]), float(P["win_hi"]))


def program_flag(P: dict, rest_from: float, rest_until: float,
                 prog_start: Optional[float], prog_end: Optional[float],
                 close_ts: float, active: Optional[bool] = None) -> bool:
    """May the market quote over [rest_from, rest_until)? Under program_only:
    `active` when the caller passes it, else the program window overlaps the
    interval (no program -> False). Otherwise: until close."""
    if P["program_only"]:
        if active is not None:
            return bool(active)
        if prog_start is None or prog_end is None:
            return False
        return rest_from < prog_end and rest_until > prog_start
    return rest_from < close_ts


def pad_hold_flag(P: dict, rest_from: float, open_ts: float) -> bool:
    """Are the 1c fillers (longshot and qualification pads) held for orders
    resting over [rest_from, ...)? True while pad_hold_open_s > 0 and the
    resting interval starts before open_ts + pad_hold_open_s -- it touches
    the hold window [open_ts, open_ts + hold) (or lies before the open), the
    same overlap test program_flag / window_flag apply to [rest_from,
    rest_until). An order that would start resting before the hold ends is
    held; the first one resting at or after it is placed. rest_from only
    grows with the decision time, so the flag turns off once per event and
    never comes back. open_ts = the event's market open (close - 3600 for
    the hourly events when the caller does not know it)."""
    hold = float(P.get("pad_hold_open_s") or 0.0)
    return hold > 0.0 and rest_from < float(open_ts) + hold


def _nth_sunday_utc(year: int, month: int, n: int, hour_utc: int) -> float:
    d = _dt.date(year, month, 1)
    day = 1 + (6 - d.weekday()) % 7 + 7 * (n - 1)          # weekday(): Monday 0
    return _dt.datetime(year, month, day, hour_utc, tzinfo=_dt.timezone.utc).timestamp()


def et_offset_h(ts: float) -> int:
    """US Eastern's UTC offset (hours) at epoch ts: -4 (EDT) from the second
    Sunday of March 07:00Z (02:00 EST) to the first Sunday of November 06:00Z
    (02:00 EDT), else -5 (EST) -- the US rule since 2007, computed without tz
    files (the policy does no I/O; temp_shadow_common's zoneinfo agrees, see
    test_temp_mm_policy)."""
    y = _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).year
    return -4 if _nth_sunday_utc(y, 3, 2, 7) <= float(ts) < _nth_sunday_utc(y, 11, 1, 6) else -5


def et_hour(ts: float) -> int:
    """The ET clock hour (0..23) at epoch ts, DST-safe. In the November
    fall-back the repeated 01:00 hour is 1 both times."""
    t = float(ts) + 3600.0 * et_offset_h(ts)
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).hour


def quote_hour_ok(P: dict, settle_ts: Optional[float]) -> bool:
    """May an event settling at settle_ts be quoted? quote_hours_et None ->
    always; else the ET hour of the settlement must be listed (an hourly
    event KXTEMPMIAH-26OCT1607 settles at 07:00 ET: hour 7). No settlement
    time with a list -> False (fail closed)."""
    hrs = P.get("quote_hours_et")
    if hrs is None:
        return True
    if settle_ts is None:
        return False
    return et_hour(float(settle_ts)) in {int(h) for h in hrs}


# --------------------------------------------------------------------------
# Risk (event level, on FILLED positions; strikes ascending)
# --------------------------------------------------------------------------

def scenario_losses(yes_q: Sequence[float], no_q: Sequence[float],
                    cost: Sequence[float]) -> List[float]:
    """loss[j] (dollars, positive = loss) in scenario j = the index above
    strikes 0..j-1: YES on strikes < j and NO on strikes >= j pay $1."""
    tot_cost = sum(cost)
    return [tot_cost - (sum(yes_q[:j]) + sum(no_q[j:])) for j in range(len(yes_q) + 1)]


def worst_case_loss(yes_q: Sequence[float], no_q: Sequence[float],
                    cost: Sequence[float]) -> Tuple[float, List[int]]:
    """(worst-case loss in dollars >= 0, the scenarios that reach it)."""
    nm = len(yes_q)
    tot_cost = sum(cost)
    pnl = []
    for j in range(nm + 1):        # scenario j: index above strikes 0..j-1
        v = sum(yes_q[:j]) + sum(no_q[j:]) - tot_cost
        pnl.append(v)
    mn = min(pnl)
    return max(0.0, -mn), [j for j, v in enumerate(pnl) if v <= mn + 1e-9]


def risk_headroom(P: dict, yes_q: Sequence[float], no_q: Sequence[float],
                  cost: Sequence[float]) -> List[Tuple[float, float, float, float]]:
    """Per market m: (yes_room, no_room, yes_usd, no_usd). *_room = contracts
    the side may add before |net YES| hits pos_cap; *_usd = dollars the
    side's resting orders may put at risk before the event's worst case in
    that side's losing scenarios reaches event_wcl_cap (YES on m loses in
    scenarios j <= m, NO in j > m; <= 0 once breached: the side stops)."""
    nm = len(yes_q)
    cap = float(P["pos_cap"])
    wcap = float(P["event_wcl_cap"])
    loss = scenario_losses(yes_q, no_q, cost)
    # pre[m] = max loss over j <= m; suf[m] = max loss over j > m
    pre, run = [], -math.inf
    for m in range(nm):
        run = max(run, loss[m])
        pre.append(run)
    suf, run = [0.0] * nm, -math.inf
    for m in range(nm - 1, -1, -1):
        run = max(run, loss[m + 1])
        suf[m] = run
    out = []
    for m in range(nm):
        net = yes_q[m] - no_q[m]
        ry = max(0.0, cap - net)
        rn = max(0.0, cap + net)
        out.append((ry, rn, wcap - pre[m], wcap - suf[m]))
    return out


def resting_scenario_losses(yes_q: Sequence[float], no_q: Sequence[float],
                            cost: Sequence[float], dys: Sequence[Dict[int, float]],
                            dns: Sequence[Dict[int, float]]) -> List[float]:
    """loss[j] (dollars) in scenario j if, on top of the filled positions,
    every desired order that LOSES in j fills in full and none that wins
    does (the adversarial case: a jump through the strikes lifts only the
    losing side). YES bids on market m lose in j <= m, NO bids in j > m; a
    bid at p loses p cents a contract."""
    n = len(yes_q)
    base = scenario_losses(yes_q, no_q, cost)
    yc = [sum(s * p / 100.0 for p, s in d.items()) for d in dys]
    nc = [sum(s * p / 100.0 for p, s in d.items()) for d in dns]
    return [base[j] + sum(yc[j:]) + sum(nc[:j]) for j in range(n + 1)]


def trim_resting_wcl(P: dict, yes_q: Sequence[float], no_q: Sequence[float],
                     cost: Sequence[float], dys: List[Dict[int, float]],
                     dns: List[Dict[int, float]]) -> int:
    """wcl_resting: scale the desired orders (in place) until no scenario's
    resting_scenario_losses exceeds event_wcl_cap. Each pass takes the worst
    scenario and scales every order losing in it by (cap - filled loss) /
    (their dollars at risk), floored to whole contracts (orders under 1
    contract dropped); a scenario whose filled loss alone reaches the cap
    clears them (and is then left at its filled loss). Returns the passes
    that trimmed (0 = within the cap)."""
    n = len(dys)
    wcap = float(P["event_wcl_cap"])
    base = scenario_losses(yes_q, no_q, cost)
    # a scenario whose filled loss alone is past the cap is done once the
    # orders losing in it are gone (nothing resting can make it smaller)
    lim = [max(wcap, b) for b in base]
    passes = 0
    for _ in range(4 * (n + 1)):
        loss = resting_scenario_losses(yes_q, no_q, cost, dys, dns)
        j = max(range(n + 1), key=lambda i: (loss[i] - lim[i], -i))
        if loss[j] <= lim[j] + 1e-9:
            break
        passes += 1
        losing = list(dys[j:]) + list(dns[:j])
        at_risk = sum(s * p / 100.0 for d in losing for p, s in d.items())
        room = wcap - base[j]
        f = room / at_risk if (room > 1e-9 and at_risk > 1e-9) else 0.0
        for d in losing:
            for p in list(d):
                q = float(math.floor(d[p] * f + 1e-9))
                if q < 1.0:
                    del d[p]
                else:
                    d[p] = q
    else:
        # cannot happen (each pass brings its scenario under the cap and
        # only lowers the others); fail closed
        for d in list(dys) + list(dns):
            d.clear()
    return passes


# --------------------------------------------------------------------------
# The per-market rule
# --------------------------------------------------------------------------

def quote_price(P: dict, side: str, book: BookView, ref: Optional[int],
                f_side: Optional[float], buf: float, back: bool, guard: bool) -> Optional[int]:
    """Price for a bid on `side`: the reference (else the best level, else
    fair - buffer), capped at fair - buffer when guarded, back_ticks lower
    when backing off, never crossing (post-only). None = do not rest."""
    levels = book.by if side == "yes" else book.bn
    p = ref
    if p is None:
        if levels:
            p = max(levels)
        elif f_side is not None:
            p = _floor(100.0 * f_side - buf)
        else:
            return None
    if guard and f_side is not None:
        p = min(p, _floor(100.0 * f_side - buf))
    if p < 1:
        return None
    if back:
        p = max(1, p - int(P["back_ticks"]))
    best_opp = book.best_n if side == "yes" else book.best_y
    if best_opp is not None:
        p = min(p, 99 - best_opp)
    p = min(p, 99)
    while p >= 1 and would_cross(book.by, book.bn, side, p):
        p -= 1
    return p if p >= 1 else None


def _tag(tags, side, p, name):
    lst = tags.setdefault((side, p), [])
    if name not in lst:
        lst.append(name)


def quote_market(P: dict, book: Optional[BookView], fair: Optional[float],
                 slope_c: Optional[float], wflag: bool, pflag: bool, size_full: float,
                 risk: Tuple[float, float, float, float], target: float,
                 tags: Optional[dict] = None, diag: Optional[dict] = None,
                 hold_pads: bool = False
                 ) -> Tuple[Dict[int, float], Dict[int, float], Optional[str]]:
    """The desired resting orders on one market -> (dy, dn, rule).

    book     the external book (None = untrusted / unknown: no quotes)
    fair     model P(YES) (clean_prob'd) or None (falls back to the book mid)
    slope_c  guard slope, cents per index step, or None (buffer = guard_min_c)
    wflag    window_flag() for the orders' resting interval
    pflag    program_flag() for it
    size_full  live_size() at the decision's tau
    risk     this market's risk_headroom() tuple
    target   the program's target size (qualification pads)
    tags     optional dict, filled {(side, price): [tag, ...]} with tags
             'live' / 'near' / 'pad' / 'fav' / 'qual'
    diag     optional dict, filled with the decision's diagnostics
    hold_pads  pad_hold_flag() for the resting interval: no longshot pad and
             no qualification pads (the 1c fillers); every other rule as
             usual (a near quote that happens to sit at 1c stays)

    dy / dn = {price: contracts} YES bids / NO bids (whole contracts >= 1).
    rule = the reward class the order books as ('live' | 'decided'), None
    when the market is not quoted by any rule."""
    if book is None or not pflag:
        if diag is not None:
            diag["reason"] = "untrusted_book" if book is None else "no_program"
        return {}, {}, None
    fx = fair if fair is not None else (book.mid / 100.0 if book.mid is not None else None)
    if fx is None:
        if diag is not None:
            diag["reason"] = "no_fair_no_mid"
        return {}, {}, None
    cls = classify(fx)
    buf = guard_buffer(P, slope_c)
    dy: Dict[int, float] = {}
    dn: Dict[int, float] = {}
    rule = None
    wm = P["window_mode"]
    if diag is not None:
        diag.update(fx=fx, fair_src="model" if fair is not None else "mid", cls=cls,
                    buf=buf, wflag=wflag, pflag=pflag, size_full=size_full)
        if hold_pads:
            diag["pad_hold"] = True
    # Near-decided strikes are live at half size -- part of the decided
    # feature, so they need `decided` (not `live`) and book as "decided"
    near_live = cls == "near" and P["decided"] and P["near_decided_half"]
    if (P["live"] and cls == "live") or near_live:
        rule = "live" if cls == "live" else "decided"
        if not (wflag and wm == "pull"):
            size = size_full * (0.5 if cls == "near" else 1.0)
            ys = ns = size
            sk = float(P["skew"] or 0.0)
            if sk and (0.05 <= fx <= 0.30 or 0.70 <= fx <= 0.95):
                ns, ys = size * (1.0 + sk), size * (1.0 - sk)
            back = wflag and wm == "back"
            guard = bool(P["fair_guard"]) and fair is not None
            ys, ns = math.floor(ys + 1e-9), math.floor(ns + 1e-9)
            if diag is not None:
                diag.update(live_yes=ys, live_no=ns, back=back, guard=guard)
            tg = "live" if cls == "live" else "near"
            if ys >= 1:
                py = quote_price(P, "yes", book, book.ref_y, fx, buf, back, guard)
                if py is not None:
                    dy[py] = float(ys)
                    if tags is not None:
                        _tag(tags, "yes", py, tg)
            if ns >= 1:
                pn = quote_price(P, "no", book, book.ref_n, 1.0 - fx, buf, back, guard)
                if pn is not None:
                    dn[pn] = float(ns)
                    if tags is not None:
                        _tag(tags, "no", pn, tg)
        elif diag is not None:
            diag["pulled"] = True
    elif P["decided"] and cls != "live":
        rule = "decided"
        yes_long = fx < 0.5
        # longshot side rests q_pad at 1c (not while the pads are held)
        ls = "yes" if yes_long else "no"
        if not hold_pads and P["q_pad"] >= 1 and not would_cross(book.by, book.bn, ls, 1):
            (dy if ls == "yes" else dn)[1] = float(math.floor(P["q_pad"]))
            if tags is not None:
                _tag(tags, ls, 1, "pad")
        fav = "no" if yes_long else "yes"
        f_fav = 1.0 - fx if yes_long else fx
        if f_fav >= float(P["fav_min_p"]) and P["q_fav"] >= 1:
            ref = book.ref_n if fav == "no" else book.ref_y
            p = quote_price(P, fav, book, ref, f_fav, buf, False, True)
            if p is not None:
                d = dn if fav == "no" else dy
                d[p] = d.get(p, 0.0) + float(math.floor(P["q_fav"]))
                if tags is not None:
                    _tag(tags, fav, p, "fav")
    if not dy and not dn:
        return {}, {}, rule
    # qualification pads (1c) on a market we quote (not while the pads are held)
    if P["qual_pads"] and not hold_pads:
        tgt = float(target)
        for side, d, depth in (("yes", dy, book.sum_y), ("no", dn, book.sum_n)):
            tot = depth + sum(d.values())
            if tot < tgt and not would_cross(book.by, book.bn, side, 1):
                d[1] = d.get(1, 0.0) + float(math.ceil(tgt + P["pad_extra"] - tot))
                if tags is not None:
                    _tag(tags, side, 1, "qual")
    if diag is not None:
        diag["pre_risk"] = {"yes": dict(dy), "no": dict(dn)}
        diag["risk"] = tuple(risk)
    # risk, pre-trade (trim worst prices first): the position cap in
    # contracts, then the event worst-case-loss headroom in dollars (each
    # contract bid at p risks p cents in the side's losing scenarios)
    ry, rn, hy, hn = risk
    for d, room, usd in ((dy, ry, hy), (dn, rn, hn)):
        if not d:
            continue
        if usd <= 1e-9:
            d.clear()
            continue
        tot = sum(d.values())
        if tot > room + 1e-9:
            excess = tot - room
            for p in sorted(d):
                cut = min(d[p], excess)
                d[p] -= cut
                excess -= cut
                if excess <= 1e-9:
                    break
        for p in list(d):
            d[p] = float(math.floor(d[p] + 1e-9))
        at_risk = sum(s * p / 100.0 for p, s in d.items())
        if at_risk > usd + 1e-9:
            excess = at_risk - usd
            for p in sorted(d):
                per = p / 100.0
                cut = min(d[p], float(math.ceil(excess / per - 1e-9)))
                d[p] -= cut
                excess -= cut * per
                if excess <= 1e-9:
                    break
        for p in [p for p, s in d.items() if s < 1.0]:
            del d[p]
    # never self-cross
    if dy and dn:
        my = max(dy)
        for p in [p for p in dn if p + my >= 100]:
            del dn[p]
    return dy, dn, rule


# --------------------------------------------------------------------------
# Order diff
# --------------------------------------------------------------------------

class OrderOp(NamedTuple):
    """One change to what rests: op 'place' (new order of `size` at `price`),
    'inc' (add `size` at a price we rest: a new order at the back of the
    queue), 'dec' (reduce what rests at `price` by `size`, Kalshi reduce_by:
    trims that many, or what is left), 'cancel' (everything at `price`;
    size 0)."""
    side: str
    op: str
    price: int
    size: float


def diff_orders(resting: Dict[str, Dict[int, float]], dy: Dict[int, float],
                dn: Dict[int, float]) -> List[OrderOp]:
    """Ops turning `resting` ({'yes': {price: size}, 'no': {...}}: what we
    believe rests once in-flight ops land) into the desired dy / dn. Same
    price keeps its queue; a size cut is relative (reduce_by)."""
    ops: List[OrderOp] = []
    for side, d in (("yes", dy), ("no", dn)):
        bs = resting.get(side) or {}
        for p in bs:
            if p not in d:
                ops.append(OrderOp(side, "cancel", p, 0.0))
        for p, s in d.items():
            cur = bs.get(p)
            if cur is None:
                ops.append(OrderOp(side, "place", p, s))
            elif s > cur + 1e-9:
                ops.append(OrderOp(side, "inc", p, s - cur))
            elif s < cur - 1e-9:
                ops.append(OrderOp(side, "dec", p, cur - s))
    return ops


# --------------------------------------------------------------------------
# Event-level API (the live executor's entry point)
# --------------------------------------------------------------------------

@dataclass
class MarketState:
    """One strike market at decision time.

    book_yes / book_no  EXTERNAL book levels, cents -> contracts (without our
                        orders: external_book() strips them from a live book)
    trusted             False = book unknown or stale -> no quotes
    fair                model P(YES) or None (falls back to the book mid)
    slope_c             guard slope, cents per 0.36 F step (FairModel.slopes)
    mu, sigma, threshold_f  (mu_F, sigma_F) of a model without slopes() and
                        the strike's index threshold (F): slope_c falls back
                        to normal_slope_c when slope_c is None and all three
                        are given
    program_active      True/False overrides the program window test;
                        None -> [program_start, program_end) vs the orders'
                        resting interval (no program -> no quotes under
                        program_only)
    target, df          the program's target size / discount factor (df is
                        not used by the quoting rules, only by scoring)
    yes_pos, no_pos     FILLED contracts held (YES / NO), cost = dollars paid
    resting_yes / resting_no  what we believe rests (price -> size), for the
                        returned ops
    """
    ticker: str
    strike: float
    book_yes: Dict[int, float] = field(default_factory=dict)
    book_no: Dict[int, float] = field(default_factory=dict)
    trusted: bool = True
    fair: Optional[float] = None
    slope_c: Optional[float] = None
    mu: Optional[float] = None
    sigma: Optional[float] = None
    threshold_f: Optional[float] = None
    program_active: Optional[bool] = None
    program_start: Optional[float] = None
    program_end: Optional[float] = None
    target: float = TARGET
    df: float = DF
    yes_pos: float = 0.0
    no_pos: float = 0.0
    cost: float = 0.0
    resting_yes: Dict[int, float] = field(default_factory=dict)
    resting_no: Dict[int, float] = field(default_factory=dict)


@dataclass
class EventContext:
    """now: decision time; close_ts: the event's close. The orders rest over
    [rest_from, rest_until): default now + order_latency_s for one second
    (the next decision). The landing window, program and pad-hold tests use
    it. open_ts: the event's market open (the pad hold counts from it);
    None -> close_ts - 3600 (the hourly events open an hour before close).
    settle_ts: the event's settlement time (the ticker's hour; quote_hours_et
    tests its ET hour); None -> close_ts."""
    now: float
    close_ts: float
    rest_from: Optional[float] = None
    rest_until: Optional[float] = None
    open_ts: Optional[float] = None
    settle_ts: Optional[float] = None

    def __post_init__(self):
        if self.open_ts is None:
            self.open_ts = float(self.close_ts) - 3600.0
        if self.settle_ts is None:
            self.settle_ts = float(self.close_ts)


@dataclass
class DesiredOrder:
    """A resting bid we want: side 'yes' (buy YES at price) or 'no' (buy NO
    at price = sell YES at 100 - price); size in whole contracts; tag = the
    rule(s) behind it, '+'-joined when they share a price ('live', 'near',
    'pad', 'fav', 'qual')."""
    side: str
    price: int
    size: float
    tag: str


@dataclass
class MarketDecision:
    ticker: str
    strike: float
    orders: List[DesiredOrder]
    yes: Dict[int, float]            # desired YES bids, price -> size
    no: Dict[int, float]             # desired NO bids
    rule: Optional[str]              # reward class: 'live' | 'decided' | None
    cls: Optional[str]               # 'live' | 'near' | 'decided' | None (no fair/mid)
    ops: List[OrderOp]               # resting_yes/no -> yes/no
    diag: dict


@dataclass
class EventDecision:
    markets: List[MarketDecision]    # strikes ascending
    tau_min: float
    size_full: int
    wflag: bool
    rest_from: float
    rest_until: float
    worst_case_loss: float           # dollars, on filled positions
    wcl_scenarios: List[int]
    headroom: List[Tuple[float, float, float, float]]
    resting_wcl: float = 0.0         # dollars: filled + every losing-side desired order filled
                                     # in full, worst scenario (resting_scenario_losses)
    pad_hold: bool = False           # pad_hold_flag: the 1c fillers are held (event open + hold)
    quote_hour: bool = True          # quote_hour_ok: the settle hour is in quote_hours_et

    def by_ticker(self) -> Dict[str, MarketDecision]:
        return {d.ticker: d for d in self.markets}


def decide(P: dict, ctx: EventContext, markets: Sequence[MarketState]) -> EventDecision:
    """The policy for one event at one instant: every market's desired
    resting orders (and the ops from what rests now), with diagnostics.
    P = a full policy dict (make_policy). Markets are sorted by strike: the
    event risk treats them as nested strikes."""
    _check_policy(P)
    mk = sorted(markets, key=lambda s: float(s.strike))
    lat = float(P["order_latency_s"])
    a = float(ctx.rest_from) if ctx.rest_from is not None else float(ctx.now) + lat
    b = float(ctx.rest_until) if ctx.rest_until is not None else a + 1.0
    wflag = window_flag(P, a, b)
    open_ts = float(ctx.open_ts) if ctx.open_ts is not None else float(ctx.close_ts) - 3600.0
    hold = pad_hold_flag(P, a, open_ts)
    settle = float(ctx.settle_ts) if ctx.settle_ts is not None else float(ctx.close_ts)
    hours_ok = quote_hour_ok(P, settle)
    tau = (float(ctx.close_ts) - float(ctx.now)) / 60.0
    size_full = live_size(P, tau)
    yes_q = [float(s.yes_pos) for s in mk]
    no_q = [float(s.no_pos) for s in mk]
    cost = [float(s.cost) for s in mk]
    risk = risk_headroom(P, yes_q, no_q, cost)
    wcl, scen = worst_case_loss(yes_q, no_q, cost)
    rows = []
    for m, s in enumerate(mk):
        # outside quote_hours_et the event quotes nothing: its markets fail
        # the program test (the sim applies the same rule)
        pflag = program_flag(P, a, b, s.program_start, s.program_end, ctx.close_ts,
                             s.program_active) and hours_ok
        book = None
        if s.trusted:
            book = BookView({int(p): float(q) for p, q in (s.book_yes or {}).items()},
                            {int(p): float(q) for p, q in (s.book_no or {}).items()},
                            float(s.target))
        fair = clean_prob(s.fair)
        slope = clean_slope(s.slope_c)
        slope_src = "slopes" if slope is not None else None
        if (slope is None and fair is not None and s.mu is not None and s.sigma
                and s.sigma > 0 and s.threshold_f is not None):
            slope = normal_slope_c(float(s.mu), float(s.sigma), float(s.threshold_f))
            slope_src = "normal"
        tags: dict = {}
        diag: dict = {"second_of_minute": a % 60.0, "tau_min": tau, "slope_c": slope,
                      "slope_src": slope_src}
        dy, dn, rule = quote_market(P, book, fair, slope, wflag, pflag, size_full,
                                    risk[m], float(s.target), tags, diag, hold_pads=hold)
        if not hours_ok:
            diag["reason"] = "off_hours"
            diag["settle_hour_et"] = et_hour(settle)
        rows.append((s, dy, dn, rule, tags, diag))
    if P.get("wcl_resting"):
        dys, dns = [r[1] for r in rows], [r[2] for r in rows]
        pre = [(dict(y), dict(n)) for y, n in zip(dys, dns)]
        if trim_resting_wcl(P, yes_q, no_q, cost, dys, dns):
            for r, (y0, n0) in zip(rows, pre):
                if y0 != r[1] or n0 != r[2]:
                    r[5]["wcl_resting_trim"] = {"yes": y0, "no": n0}   # before the trim
    rloss = resting_scenario_losses(yes_q, no_q, cost, [r[1] for r in rows], [r[2] for r in rows])
    out = []
    for s, dy, dn, rule, tags, diag in rows:
        orders = [DesiredOrder(side, p, d[p], "+".join(tags.get((side, p), [])))
                  for side, d in (("yes", dy), ("no", dn)) for p in sorted(d, reverse=True)]
        ops = diff_orders({"yes": dict(s.resting_yes or {}), "no": dict(s.resting_no or {})},
                          dy, dn)
        out.append(MarketDecision(ticker=s.ticker, strike=float(s.strike), orders=orders,
                                  yes=dy, no=dn, rule=rule, cls=diag.get("cls"), ops=ops,
                                  diag=diag))
    return EventDecision(markets=out, tau_min=tau, size_full=size_full, wflag=wflag,
                         rest_from=a, rest_until=b, worst_case_loss=wcl,
                         wcl_scenarios=scen, headroom=risk, resting_wcl=max(0.0, max(rloss)),
                         pad_hold=hold, quote_hour=hours_ok)
