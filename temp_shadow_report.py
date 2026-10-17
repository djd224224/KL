#!/usr/bin/env python3
r"""temp_shadow_report.py -- the hourly-temp shadow test's DAILY REPORT
(TEMP_SHADOW.md "Daily report", "Tasks").

Jack 2026-10-14: "build it and shadow test for a week, reporting results and
suggestion tweaks daily". Every morning (task "KL temp-shadow report", 08:30
ET) this sims the previous ET day (temp_shadow_sim.run_day) and brings every
earlier day of the cumulative window up to the current SIM_VERSION (run_day
again: cached files that pass its check are only loaded), reads every
sim/<ET day>/<EVENT>.json since the shadow start, and emails one HTML page:

  1 headline       "Day N of 7"; the variants ranked on STRESS net over each
                   one's JUDGED hours (never the hours it was chosen from:
                   judged_from_ts) without its best and worst hour (the
                   "robust total"), beside the plain total, a 3-hour block-
                   bootstrap 90% CI, P(total > 0), a "noise-dominated" flag
                   (|best hour| + |worst hour| > |total|) and the in-sample
                   hours for information. The headline names the top variant
                   ONLY if its robust total > 0, P(total > 0) >= 0.8 and its
                   judged hours span 2 ET days, else "no variant passes" (with
                   the top robust total for information); the named
                   variant's noise flag goes into the subject. Always on stress: no stress results -> "no
                   variant passes (no stress results)". No single day's
                   realized net ranks anything (Jack 2026-10-17, "1": the
                   10/17 email headlined I_pads_only's +$1,480 on two upset
                   hours)
  1b pre-registered tests  Test A (FAV_night, 5 ET days from PREREG_TESTS'
                   start, 10/17 as registered) and Test B (FAV_night_agree,
                   10/18-10/22) exactly as TEMP_SHADOW.md "Pre-registered
                   tests" defines them: per-day stress nets over the expected
                   hours, each criterion met / not met, "in progress (n/5
                   days)" until the window's last hour is due and every due
                   hour is in, then PASS / FAIL -- INCOMPLETE when an expected
                   hour has no stress result, INVALID when a window file ran
                   another rule or model than the registered one (the pins);
                   Test A's hours known at registration labelled and its
                   verdict shown both ways; completed verdicts frozen to
                   <data root>/prereg/; the calibrated-tails expectation from
                   the tail table AND a recent-regime figure; the verdict
                   "pilot eligible: <variant>" only on a PASS, else "NO-GO (no
                   variant passes the pre-registered tests)", "in progress" or
                   "undecided"; the agreement gate's shadow pass share for the
                   live reconciliation
  2 variants       opt, cons, swp and stress side by side: reward, contracts,
                   trading P&L, net (swp / stress: +/- the Monte Carlo se, 1c
                   filler contracts, "expected (calibrated tails)" = net_cal;
                   stress: avg share, fills within 3 s after a weather
                   update), in the ranking's order
  3 breakdowns     the headline variant (else the top of the ranking; none
                   without stress results) on
                   stress fills by ET hour of the event, strike class (live /
                   decided), tau bucket, side, and its worst 5 events
  4 market         % of strike-seconds paying without us, % locked at 99,
                   live / decided split, depth within 3c of the touch
  5 model          Brier / log loss of the fair vs the book mid by tau; the
                   index replica's exact-match rate on the day's finalized
                   minutes (temp_shadow_model.replica_index vs official v)
  6 data health    ws uptime, reconnects, gaps, book-trusted %, wx polls and
                   429s, our observation lag (rx - newest station
                   received_at for minutes that just became complete),
                   missing events with the likely reason
  7 suggestions    a rule engine over the variant pairs (swp fill model):
                   window B-A, back vs pull C-B, size D-B ($ per extra
                   contract of q_live), decided E-B and decided strikes
                   alone (G0, the G without the pad hold, else G), the
                   pad hold G-G0, full F vs best, guard B-H; the fill-model
                   gap (cons - swp: what sweepers take); tau-bucket
                   losses at or above the current live_stop_min (->
                   live_stop_min); losing ET hours, each hour tested on its
                   own with a Bonferroni bar over 24 hours (-> smaller size
                   there); a model worse than the mid (-> guard_k); low
                   paying fraction; ops (429 rate, uptime, observation lag,
                   missing events) against the collector's LIVE poll
                   settings, read with ast from temp_shadow_collector.py.
                   Every suggestion carries its $ evidence (per day, +/- one
                   standard error from the per-event spread) and a concrete
                   parameter change; anything under $5/day, under 3 events
                   or within 1 SE of zero is listed as "below noise" instead
  8 cumulative     per variant since day 1 (stress; swp, cons and opt beside
                   it): robust total, sum, per-day mean, days positive, Monte
                   Carlo se, expected at calibrated tails
                   (the shadow's partial last day -- SHADOW_UNTIL falls inside
                   it -- counts as its share of 24 event-hours in per-day
                   figures and is left out of days positive)
  9 week summary   from day 7: the adopted go-live rules instead of the old
                   GO / NO-GO thresholds -- the pre-registered tests' verdict
                   ("pilot eligible: <variant>" only on a PASS; then the rule-4
                   pilot config -- the registered rule at 1/3 size (q_fav
                   100), night hours only, $300 daily halt, hourly
                   live-vs-shadow reconciliation, auto-halt after 2 hours
                   below the shadow's band), both ways while Test A holds
                   hours known at registration, and the key risks

Fill models (Jack 2026-10-16, "1": "ship the fixes + the sweep-aware report
column"): opt and cons fill us only from recorded trades; swp (temp_shadow_sim
since SIM_VERSION 6, temp_shadow_sweep's model) adds the sweepers that took
live G's 1c fillers within seconds on 10/16 -- the only model that reproduced
the live hour. stress (SIM_VERSION 7; Jack 2026-10-17 adopted the go-live
rules, TEMP_SHADOW.md "Pre-registered tests": "Decisions use the STRESS fill
model") adds level-clears at every price and hidden favorite takers: the
headline ranking, the pre-registered tests and the week verdict are judged
on stress (DECISION_FILL). The tweak rules (noise gates unchanged) still
compare variants on swp, which every variant has (stress may cover only the
variants that quote no live strikes: temp_shadow_sim.STRESS_SCOPE).
headline_fill() picks the rules' fill model (swp, else cons for sim output
from before SIM_VERSION 6). The ranking never falls back (review of 338b4a5):
without stress results it is empty and the headline says "no variant passes
(no stress results)".

Fix pass 10/16 (review of the swp build):
  * "Expected (calibrated tails)" (net_cal) is shown only for variants that
    quote no live strikes (policy live=False, cal_ok()); for a live-quoting
    variant a fill at a 10-90% fair keeps the model's own fair, so the cell
    reads "(circular)" and the headline, the email text and the week risks
    leave it out.
  * Events without swp in the window (swp failed in the sim -- the event
    keeps opt / cons -- or an older sim file) are counted in a banner and the
    subject; a failed event whose older sim file is still loaded is named as
    such instead of "left out of every figure".
  * The swp hazard / tails tables in use (id, repo default or model/
    override) are named under Variants and in the email text; a rejected
    model/ override (not a usable table: the repo default is used) is a
    banner and a subject flag.
  * run_sim re-sims earlier window days within RESIM_BUDGET_MIN (env
    TEMP_SHADOW_RESIM_BUDGET_MIN, 80) of its start: the report day is always
    simmed in full first; earlier-day events not started by then are
    deferred (their older files stay, flagged stale) and the next report
    continues. Measured under the machine's normal load a full 200-event
    window re-sim takes ~100 min against the task's 2 h limit.

Variant letters: the pair rules look variants up by their leading letter
(exact name "B", or "B" followed by a non-alphanumeric, e.g. "B_pull"); a
pair whose variant is absent is skipped and named in the report. "G0" finds
G0_nohold (G without the 10/16 pad hold), which "G" never matches; the
pad-hold rule and the decided-only baseline use it when present.

Sim freshness: run_sim re-runs temp_shadow_sim.run_day for every day of the
cumulative window, not only the report day; run_day reuses a cached file
only when it is from the current SIM_VERSION with the current variant
overrides, so a grid change re-sims the window. The earlier days go newest
first (since 10/17: the pre-registered test windows are the most recent
days, so a budget-deferred re-sim falls on the oldest days). Files still
older than the current SIM_VERSION (a --no-sim report, a failed re-sim) are
flagged at the top of the report.

Shadow days: day 1 = TEMP_SHADOW_DAY1 (2026-10-15, the first full ET day);
day 0 = the partial 10/14. Cumulative figures cover day 1 .. the report day
(a day-0 report covers day 0 alone). The last day (10/23: the collector stops
at SHADOW_UNTIL 12:00Z = 08:00 ET) is partial: events settling at or after
SHADOW_UNTIL are not due, and the verdict ignores the day. Collector health
starts at the recording's first row when that falls inside the day (day 0).

USAGE
  python temp_shadow_report.py                    # yesterday ET: sim + report
  python temp_shadow_report.py --day 2026-10-15   # a specific ET day
  python temp_shadow_report.py --no-sim           # report existing sim output
  python temp_shadow_report.py --send             # also email it
  python temp_shadow_report.py --out C:\tmp\r.html
Writes reports/<day>.html (or --out) and prints its path. Exit 0 ok, 1 the
sim failed (the report still goes out, with the failure on top), 2 the email
failed. Reads only the shadow's own data root (TEMP_SHADOW_DATA); never
places an order, never imports incentive_mm. Email goes through
send_html_email.py (ALERT_EMAIL_* env, falling back to HKCU\Environment the
way imm_quote_gaps does, since Task Scheduler strips the user env).
"""
from __future__ import annotations

import argparse
import ast
import bisect
import datetime as _dt
import glob
import gzip
import html
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import temp_shadow_common as C

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DAY1 = os.environ.get("TEMP_SHADOW_DAY1", "2026-10-15")   # day 1 of 7
WEEK_DAYS = 7
TAU_BUCKETS = ("0-5", "5-10", "10-20", "20-40", "40-60")
FILLS = ("opt", "cons", "swp", "stress")   # column order in the tables
HEADLINE_FILL = "swp"                # the tweak rules' fill model ...
FALLBACK_FILLS = ("swp", "cons", "opt")   # ... else the first one the output has
DECISION_FILL = "stress"             # the ranking, the pre-registered tests, the verdict
FILL_LABEL = {"swp": "swp (sweep-aware)", "cons": "cons", "opt": "opt",
              "stress": "stress (decision model)"}
STRESS_NOTE = ("stress = swp plus level-clears at every price (a recorded taker that walked "
               "through or cleared our price takes all of our lots there) and hidden favorite "
               "takers while our favorite lot rests alone at the top (temp_shadow_sweep, the "
               "10/17 audit). The adopted go-live rules decide on stress (rule 2): the "
               "ranking, the pre-registered tests and the week verdict use it; the tweak "
               "rules still pair variants on swp.")
# The headline ranking (Jack 2026-10-17, "1"): STRESS net over each
# variant's judged hours (never the hours it was chosen from) without its
# best and worst hour; the headline names the top variant only when that
# robust total is > 0 and P(total > 0) >= this.
HEADLINE_P_MIN = 0.8
# ... and only when its judged hours span at least this many ET days: no
# single-day ranking. With per-variant judged hours a variant's window can be
# one night (FAV_night on the 10/17 report: 19 hours, all >= 0, P = 1.00).
HEADLINE_MIN_DAYS = 2
BOOT_N = 4000                        # block-bootstrap resamples (fixed seed)
BOOT_SEED = 20261017
BOOT_BLOCK = 3                       # consecutive hours per bootstrap block
SETTLE_LAG_S = 7200                  # an hour is due 2 h after it settles (expected_events)
# The headline's first judged hour per variant (judged_from_ts): a tested
# variant from its test's start, every other FAV_* variant from Test A's,
# these from theirs, every other variant from the window start.
# G_decided_only's 180 s pad hold was chosen from 10/16's hours.
HEADLINE_JUDGED_FROM = {"G_decided_only": "2026-10-17T00:00:00-04:00"}
# TEMP_SHADOW.md "Pre-registered tests" was committed (eff9cd3) at this
# time; the 10/17 audit (6e6ebdf, 14:43:57) had published FAV_night's
# figures for the hours settled by then.
PREREG_REGISTERED_AT = "2026-10-17T14:57:35-04:00"
# What a test's numbers must come from (review of 338b4a5): every window sim
# file of a tested variant must carry this SIM_VERSION, these swp / stress
# identities (versions, tables, draws, seeds) and this fair model
# (temp_shadow_sim.fair_model_identity: a hash of the fitted params, model/
# model_params.json of 10/14 22:01), else the test is "INVALID (rule or
# model changed)". SIM_VERSION 8 = 7's numbers plus the identity fields.
# Changing any of these is an amendment to TEMP_SHADOW.md, not an edit.
PREREG_PIN_MODEL = {
    "sim_version": "8",
    "swp": {"sweep_version": "1", "draws": 6, "rec_mode": "level", "channels": "abc",
            "seed": "tmm-sweep-1", "hazard_id": "bfb9676a4e92a9d2",
            "tails_id": "46e671fb82353b0b"},
    "stress": {"stress_version": "1", "draws": 6, "seed": "tmm-sweep-1",
               "fav_seed": "tmm-favhaz-1", "fav_hazard_id": "cbe48e9d9ea66069",
               "take": "full", "hole": False},
    "fair_model_id": "4f5fd78c360d8717",
}
# The tested variants' rules as registered: the overrides of eff9cd3 and the
# full effective policy as of 338b4a5 (POLICY_VERSION 4 only added
# fav_market_min_p = None, the gate off).
_FAV_NIGHT_OVERRIDES = {"live": False, "decided": True, "q_pad": 0, "qual_pads": False,
                        "near_decided_half": False, "fav_min_price_c": 50,
                        "quote_hours_et": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7]}
_FAV_NIGHT_POLICY = {
    "back_ticks": 2, "decided": True, "event_wcl_cap": 1500.0, "fair_guard": True,
    "fav_market_min_p": None, "fav_min_p": 0.97, "fav_min_price_c": 50, "guard_k": 1.0,
    "guard_min_c": 1, "live": False, "live_stop_min": 3.0, "live_tau_full": 40.0,
    "live_tau_min_frac": 0.3, "maker_fee_per_contract": 0.0, "near_decided_half": False,
    "order_latency_s": 0.15, "pad_extra": 50, "pad_hold_open_s": 0.0, "pos_cap": 1500,
    "program_only": True, "q_fav": 300, "q_live": 300, "q_pad": 0, "qual_pads": False,
    "quote_hours_et": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7], "react_s": 0.5,
    "skew": 0.0, "wcl_resting": False, "win_hi": 45, "win_lo": 20, "window_mode": "pull"}
# TEMP_SHADOW.md "Pre-registered tests", implemented exactly as written: do
# not re-tune; a changed rule needs a new pre-registered window.
PREREG_TESTS = (
    {"id": "A", "variant": "FAV_night", "why": "rule 3",
     # Test A's window = "days" ET days from "start" (ONE constant). As
     # registered it starts 10/17 00:00 ET and so holds 26OCT1700-1714, which
     # settled before the registration: the report labels them and shows the
     # verdict without them too. The stricter window (Jack's call; then
     # TEMP_SHADOW.md gets "Amendment 1") is this one line set to
     # "2026-10-18T00:00:00-04:00".
     "start": "2026-10-17T00:00:00-04:00",
     "days": 5, "criteria": ("total", "days", "cal"), "info": ("robust", "recent"),
     "overrides": _FAV_NIGHT_OVERRIDES, "policy": _FAV_NIGHT_POLICY, "model": PREREG_PIN_MODEL},
    {"id": "B", "variant": "FAV_night_agree",
     "why": "new rule, chosen from the 10/16 21:00 and 22:00 ET losses: judged ONLY on later "
            "data",
     "start": "2026-10-18T00:00:00-04:00",
     "days": 5, "criteria": ("total", "days", "robust", "cal"), "info": ("recent",),
     "overrides": dict(_FAV_NIGHT_OVERRIDES, fav_market_min_p=0.95),
     "policy": dict(_FAV_NIGHT_POLICY, fav_market_min_p=0.95), "model": PREREG_PIN_MODEL},
)
PREREG_DAYS_POS = 4                  # of the 5 window days
PILOT_RULES = ("1/3 size, night hours only, a $300 daily halt, an hourly live-vs-shadow "
               "reconciliation, and an auto-halt after 2 consecutive hours below the shadow's "
               "band (rule 4); scale up only if the pilot earns at least 50% of the shadow's "
               "prediction (rule 5). A PASS only makes the variant eligible: it does not start "
               "trading (Jack alone flips dry -> live).")
# The hourly live-vs-shadow reconciliation (rule 4) of an agreement-gated
# variant also compares how often the gate lets a favorite join through.
GATE_RECON_NOTE = ("Live reconciliation (rule 4) of the agreement gate: compare the share of the "
                   "model's favorite seconds in which the gate passed -- live, the share of "
                   "the executor's decision samples whose fav_market.ok is true (temp_mm_live "
                   "decisions log); in the shadow, fav_gate gate_secs / model_secs per sim "
                   "file. Live, the gate reads the external book after other makers react to "
                   "our joins (on 26OCT1610 a better external bid appeared within 2 s after 36% "
                   "of our favorite joins, against 11% after other makers' joins), which the "
                   "recording never shows: the live share can differ either way, and a large "
                   "gap means live would not trade the hours the shadow books.")
RECENT_BOOT_N = 2000
SWP_NOTE = ("swp = the sweep-aware fill model (temp_shadow_sweep; it reproduced live G's "
            "first hour, 26OCT1610, where opt / cons did not): cons fills, plus sweepers "
            "who take our 1c orders -- recorded 1c taker clusters (all of ours when the "
            "taker cleared the level), hidden IOC sweeps at the calibrated hazard while we "
            "rest alone at 1c, and new 99c bids that cross us. Mean of {draws} Monte Carlo "
            "draws per event, +/- its standard error. The headline, the rules and the "
            "verdict use swp.")
CAL_NOTE = ("Expected (calibrated tails) = reward + every swp fill marked at the realized "
            "longshot rate of its model-probability bucket given a taker hit a bid at that "
            "price (temp_shadow_sweep_tails.json, 530 settled hours) instead of the hour's "
            "actual settlement: an ESTIMATE of the expectation, free of the few upsets that "
            "dominate realized nets. Shown only for variants that quote no live strikes "
            "(live=False): a fill at a 10-90% fair keeps the model's own fair, so for a "
            "live-quoting variant the figure would be circular -- those cells read "
            "\"(circular)\".")
CIRCULAR = "(circular)"
# run_sim: the earlier window days are re-simmed only within this many minutes
# of its start (the report day is always simmed in full first); the task's
# limit is 2 h (register_temp_shadow_report.ps1). Measured 10/16 under the
# machine's normal load: ~29.5 s of wall time per event at 2 workers, so a
# full 200-event window re-sim (~100 min) would not fit with the report.
RESIM_BUDGET_MIN = float(os.environ.get("TEMP_SHADOW_RESIM_BUDGET_MIN", "80") or 80)
SEND_SCRIPT = os.path.join(C.REPO, "send_html_email.py")
EMAIL_ENV = ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD", "ALERT_EMAIL_TO")

# Suggestion noise gates.
MIN_USD_PER_DAY = 5.0      # smaller $ evidence is noise
MIN_EVENTS = 3             # fewer differing events is noise
MIN_Z = 1.0                # |delta| must exceed one standard error
MODEL_MIN_N = 60           # strike-minutes before judging the model
MODEL_MARGIN = 0.002       # Brier points the model may trail the mid by
LOW_PAYING_FRAC = 0.25
FILL_GAP_FRAC = 0.5        # |opt - cons| > 50% of |cons| = fill-model risk
OPS_UPTIME_MIN = 0.95
OPS_429_MIN = 10           # 429s a day AND ...
OPS_429_RATE = 0.02        # ... more than 2% of station polls
OPS_LAG_P90_MAX = 3.0      # seconds
REPLICA_MIN_RATE = 0.98
# Losing-hours rule: each ET hour is tested on its own (z of its mean net
# against 0 with the pooled within-hour spread), Bonferroni over 24 hours
# (one-sided 0.05 / 24 -> z 2.86), and must lose on most of its days.
HOUR_Z = 2.9
HOUR_MIN_DAYS = 3
HOUR_NEG_FRAC = 0.75
SHORT_DAY_EVENTS = 22      # fewer events than this = a short day

# The collector's station-poll settings as the spec wrote them; the live
# values are read from temp_shadow_collector.py (+ its env overrides).
_COLLECTOR_DEFAULTS = {"WX_LO": 18.0, "WX_HI": 50.0, "WX_FAST": 1.0, "WX_SLOW": 5.0,
                       "WX_MIN_GAP": 0.8, "WX_YIELD_FAST": 2.0, "SIGN_AFTER_429": 3,
                       "SIGN_ON_429": True}

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _num(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _opt_num(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def money(x, dec: Optional[int] = None) -> str:
    """+$1,234 / -$56.20 / $0.00 (cents below $100, whole dollars above)."""
    v = _num(x)
    if dec is None:
        dec = 2 if abs(v) < 100 else 0
    sign = "-" if v < -0.005 else ("+" if v > 0.005 else "")
    return f"{sign}${abs(v):,.{dec}f}"


def usd(x, dec: Optional[int] = None) -> str:
    """Unsigned: $56.20 (for titles whose words carry the sign)."""
    return money(abs(_num(x)), dec).lstrip("+")


def pct(x, dec: int = 1) -> str:
    v = _opt_num(x)
    return "n/a" if v is None else f"{100.0 * v:.{dec}f}%"


def qty(x) -> str:
    v = _num(x)
    return f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.1f}"


def default_day(now: Optional[float] = None) -> str:
    """Yesterday's ET date."""
    now = C.utcnow() if now is None else now
    return (C.et_dt(now).date() - _dt.timedelta(days=1)).isoformat()


def day_number(day: str) -> int:
    return (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(DAY1)).days + 1


def day_label(day: str) -> str:
    n = day_number(day)
    if n <= 0:
        return f"Day {n} (partial) of {WEEK_DAYS}"
    if is_partial_end(day):
        end = C.et_dt(shadow_until()).strftime("%H:%M")
        return f"Day {n} of {WEEK_DAYS} (partial: the shadow ended {end} ET)"
    return f"Day {n} of {WEEK_DAYS}"


def window_days(day: str) -> List[str]:
    """Cumulative window: day 1 .. day (a pre-day-1 report: that day alone).
    The shadow's last ET day is partial (SHADOW_UNTIL falls inside it): it
    stays in the window but Window weights it (day_weight) and keeps it out
    of the verdict."""
    if day_number(day) < 1:
        return [day]
    out, d = [], _dt.date.fromisoformat(DAY1)
    end = _dt.date.fromisoformat(day)
    while d <= end:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def shadow_until() -> float:
    return C.parse_iso(C.SHADOW_UNTIL_UTC) or math.inf


def _day_tickers(day: str) -> List[Tuple[str, Optional[float]]]:
    """(event ticker, settle epoch) for the ET day's 24 hour labels."""
    d = _dt.date.fromisoformat(day)
    series = C.SERIES[0] if C.SERIES else "KXTEMPMIAH"
    mon = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
           "NOV", "DEC")[d.month - 1]
    out = []
    for h in range(24):
        ev = f"{series}-{d.year % 100:02d}{mon}{d.day:02d}{h:02d}"
        p = C.parse_event(ev)
        out.append((ev, p[1] if p else None))
    return out


def is_partial_end(day: str) -> bool:
    """The ET day SHADOW_UNTIL falls inside (the collector stops mid-day)."""
    t0, t1 = C.et_day_bounds(day)
    return t0 < shadow_until() < t1


def day_weight(day: str) -> float:
    """1.0, or for the shadow's partial last day the share of its 24 event-
    hours that settle before SHADOW_UNTIL (10/23 with a 12:00Z stop: 8/24)."""
    if not is_partial_end(day):
        return 1.0
    until = shadow_until()
    n = sum(1 for _ev, s in _day_tickers(day) if s is not None and s < until)
    return max(n, 1) / 24.0


def _fmt_days(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def split_key(key: str) -> Tuple[str, str]:
    v, sep, f = key.rpartition("|")
    return (v, f) if sep else (key, "")


def event_hour(event: str, ev: Optional[dict] = None) -> Optional[int]:
    """ET hour label of the event (KXTEMPMIAH-26OCT1409 -> 9)."""
    p = C.parse_event(event)
    if p:
        return C.et_dt(p[1]).hour
    ts = _opt_num((ev or {}).get("settle_ts")) or _opt_num((ev or {}).get("close_ts"))
    return C.et_dt(ts).hour if ts else None


def headline_fill(src) -> str:
    """The fill model the headline, rules and verdict use: HEADLINE_FILL
    (swp) when the output has it, else the first of FALLBACK_FILLS present
    (sim files from before SIM_VERSION 6 have no swp). src: a Window or an
    agg dict (keys '<variant>|<fill>')."""
    keys = src.by_key if isinstance(src, Window) else (src or {})
    have = {split_key(k)[1] for k in keys}
    for f in (HEADLINE_FILL,) + tuple(x for x in FALLBACK_FILLS if x != HEADLINE_FILL):
        if f in have:
            return f
    return HEADLINE_FILL


def cal_ok(policy: Optional["Policy"], name: Optional[str]) -> bool:
    """Is net_cal ("expected at calibrated tails") evidence for this variant?
    Only when it quotes no live strikes (policy live=False): its fills are
    on decided / near strikes, which the tails table covers. A live-quoting
    variant's fills at a 10-90% fair keep the model's own fair (circular).
    Unknown variants / no policy -> False."""
    return bool(name) and policy is not None and policy.get(name, "live") is False


def resolve_variant(letter: str, names: Sequence[str]) -> Optional[str]:
    """'B' -> 'B' or 'B_pull' (letter then a non-alphanumeric)."""
    if letter in names:
        return letter
    up = letter.upper()
    cands = [n for n in names if n.upper().startswith(up)
             and (len(n) == len(up) or not n[len(up)].isalnum())]
    return sorted(cands)[0] if cands else None


def _quantile(vals: Sequence[float], q: float) -> Optional[float]:
    v = sorted(vals)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


# --------------------------------------------------------------------------
# Policy definitions (temp_shadow_sim.VARIANTS / DEFAULT_POLICY), lazily
# --------------------------------------------------------------------------

_META_KEYS = ("desc", "description", "label", "name", "note", "notes")


def load_policy_defs() -> Tuple[dict, dict, Optional[str]]:
    """(DEFAULT_POLICY, VARIANTS, error) from temp_shadow_sim; ({}, {}, err)
    when the sim is not importable."""
    try:
        import temp_shadow_sim as sim   # noqa: WPS433 (lazy on purpose)
        return (dict(getattr(sim, "DEFAULT_POLICY", {}) or {}),
                dict(getattr(sim, "VARIANTS", {}) or {}), None)
    except BaseException as e:          # noqa: BLE001 -- report, never crash
        if isinstance(e, KeyboardInterrupt):
            raise
        return {}, {}, f"{type(e).__name__}: {e}"


class Policy:
    def __init__(self, defaults: dict, variants: dict, err: Optional[str] = None):
        self.defaults = defaults or {}
        self.variants = variants or {}
        self.err = err

    def params(self, name: str) -> dict:
        p = dict(self.defaults)
        p.update(self.variants.get(name) or {})
        return p

    def desc(self, name: str) -> str:
        v = self.variants.get(name) or {}
        for k in _META_KEYS:
            if isinstance(v.get(k), str) and v.get(k):
                return v[k]
        ov = {k: x for k, x in v.items() if not _skip_key(k)}
        if not ov:
            return "defaults" if name in self.variants else ""
        return ", ".join(f"{k}={_fmt_val(x)}" for k, x in sorted(ov.items()))[:160]

    def get(self, name: str, key: str):
        p = self.params(name)
        if key in p:
            return p[key]
        low = {k.lower(): k for k in p}
        return p.get(low.get(key.lower(), key))

    def diff(self, a: str, b: str) -> List[Tuple[str, Any, Any]]:
        pa, pb = self.params(a), self.params(b)
        out = []
        for k in sorted(set(pa) | set(pb)):
            if _skip_key(k):
                continue
            if pa.get(k) != pb.get(k):
                out.append((k, pa.get(k), pb.get(k)))
        return out

    def diff_text(self, a: str, b: str) -> str:
        """The settings that turn variant a into variant b."""
        d = self.diff(a, b)
        if not d:
            return f"use {b}'s settings" if not self.variants else f"{a} and {b} share settings"
        return ", ".join(f"{k} {_fmt_val(x)} -> {_fmt_val(y)}" for k, x, y in d)


def _skip_key(k: str) -> bool:
    return k.startswith("_") or k.lower() in _META_KEYS


def _fmt_val(x) -> str:
    if isinstance(x, float):
        return f"{x:g}"
    if isinstance(x, (list, tuple)):
        return "[" + ",".join(_fmt_val(i) for i in x) + "]"
    if isinstance(x, dict):
        return "{" + ",".join(f"{k}:{_fmt_val(v)}" for k, v in x.items()) + "}"
    return str(x)


# --------------------------------------------------------------------------
# Sim output loading and aggregation
# --------------------------------------------------------------------------


def sim_day_dir(day: str) -> str:
    return os.path.join(C.DATA_ROOT, "sim", day)


def load_sim_day(day: str) -> List[dict]:
    """Every sim/<day>/<EVENT>.json with a variants block, in event order."""
    d = sim_day_dir(day)
    out: List[dict] = []
    if not os.path.isdir(d):
        return out
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        base = os.path.splitext(os.path.basename(p))[0]
        if base.startswith("_"):
            continue
        rec = C.read_json(p)
        if not isinstance(rec, dict) or not isinstance(rec.get("variants"), dict):
            continue
        rec.setdefault("event", base)
        out.append(rec)
    out.sort(key=lambda r: (_num(r.get("settle_ts")) or _num(r.get("close_ts")),
                            str(r.get("event"))))
    return out


def in_shadow(events: Sequence[dict], rec_start: Optional[float]) -> List[dict]:
    """Drop sim outputs of events that settled at or before the recording's
    first row (the same "outside the shadow" rule as expected_events)."""
    if rec_start is None:
        return list(events)
    out = []
    for ev in events:
        p = C.parse_event(str(ev.get("event") or ""))
        s = p[1] if p else (_opt_num(ev.get("settle_ts")) or _opt_num(ev.get("close_ts")))
        if s is not None and s <= rec_start:
            continue
        out.append(ev)
    return out


_SUM_FIELDS = ("reward", "fills_n", "contracts", "trading_pnl", "fees", "net",
               "quoted_secs")
_MAX_FIELDS = ("max_abs_pos", "worst_case_loss")


# swp extras (summed when present): 1c filler contracts and the expected net
# at calibrated tails (net_cal, with its tail-table CI ends)
_SWP_SUM_FIELDS = ("filler_contracts", "net_cal", "net_cal_tlo", "net_cal_thi")


def _new_agg() -> dict:
    a = {f: 0.0 for f in _SUM_FIELDS + _MAX_FIELDS}
    a.update({"n_events": 0, "unsettled": 0, "_sw": 0.0, "_ss": 0.0,
              "avg_share": None, "by_class": {}, "by_tau": {}, "by_side": {},
              "fills_after_wx": {"contracts": 0.0, "pnl": 0.0},
              "markouts": {}, "_mow": 0.0, "_mcvar": 0.0, "_mc_n": 0, "draws": None})
    return a


def _add_nested(dst: dict, src) -> None:
    if not isinstance(src, dict):
        return
    for k, sub in src.items():
        if not isinstance(sub, dict):
            continue
        d = dst.setdefault(k, {})
        for f, x in sub.items():
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                d[f] = d.get(f, 0.0) + _num(x)


def agg_variants(events: Sequence[dict]) -> Dict[str, dict]:
    """key '<variant>|<fill>' -> summed metrics over the events."""
    out: Dict[str, dict] = {}
    for ev in events:
        prog = _num((ev.get("coverage") or {}).get("program_secs"))
        settled = ev.get("settled", True)
        for key, v in (ev.get("variants") or {}).items():
            if not isinstance(v, dict):
                continue
            a = out.get(key)
            if a is None:
                a = out[key] = _new_agg()
            a["n_events"] += 1
            if settled is False:
                a["unsettled"] += 1
            for f in _SUM_FIELDS:
                a[f] += _num(v.get(f))
            for f in _MAX_FIELDS:
                a[f] = max(a[f], abs(_num(v.get(f))))
            for f in _SWP_SUM_FIELDS:
                if _opt_num(v.get(f)) is not None:
                    a[f] = a.get(f, 0.0) + _num(v.get(f))
            se = _opt_num(v.get("net_se"))
            if se is not None:          # Monte Carlo variance of the summed net
                a["_mcvar"] += se * se
                a["_mc_n"] += 1
                a["draws"] = v.get("draws") if a["draws"] in (None, v.get("draws")) else "mixed"
            share = _opt_num(v.get("avg_share"))
            if share is not None:
                w = _num(v.get("quoted_secs")) if "quoted_secs" in v else (prog or 1.0)
                a["_sw"] += w
                a["_ss"] += share * w
            _add_nested(a["by_class"], v.get("by_class"))
            _add_nested(a["by_tau"], v.get("by_tau"))
            _add_nested(a["by_side"], v.get("by_side"))
            faw = v.get("fills_after_wx") or {}
            a["fills_after_wx"]["contracts"] += _num(faw.get("contracts"))
            a["fills_after_wx"]["pnl"] += _num(faw.get("pnl"))
            mo = v.get("markouts") or {}
            ct = _num(v.get("contracts"))
            if ct > 0:     # cents per contract per event -> contract-weighted
                a["_mow"] += ct
                for f, x in mo.items():
                    if _opt_num(x) is not None:
                        a["markouts"][f] = a["markouts"].get(f, 0.0) + _num(x) * ct
    for a in out.values():
        a["avg_share"] = a["_ss"] / a["_sw"] if a["_sw"] > 0 else None
        if a["_mow"] > 0:
            a["markouts"] = {f: x / a["_mow"] for f, x in a["markouts"].items()}
        a["net_se"] = math.sqrt(a["_mcvar"]) if a["_mc_n"] else None
    return out


def agg_market(events: Sequence[dict]) -> dict:
    tot = 0.0
    sums = {"paying_frac_without_us": 0.0, "locked99_frac": 0.0,
            "live_frac": 0.0, "decided_frac": 0.0}
    depth = []
    for ev in events:
        ms = ev.get("market_stats") or {}
        w = _num(ms.get("strike_secs"))
        if w > 0:
            tot += w
            for k in sums:
                sums[k] += _num(ms.get(k)) * w
        d = _opt_num(ms.get("depth3c_median"))
        if d is not None:
            depth.append(d)
    out = {"strike_secs": tot, "events": len(events)}
    for k, s in sums.items():
        out[k] = s / tot if tot > 0 else None
    out["depth3c_median"] = statistics.median(depth) if depth else None
    return out


def agg_model(events: Sequence[dict]) -> dict:
    n = 0.0
    s = {"brier_model": 0.0, "brier_mid": 0.0, "logloss_model": 0.0, "logloss_mid": 0.0}
    ll_n = 0.0
    by: Dict[str, Dict[str, float]] = {}
    for ev in events:
        me = ev.get("model_eval") or {}
        k = _num(me.get("n"))
        if k > 0:
            n += k
            s["brier_model"] += _num(me.get("brier_model")) * k
            s["brier_mid"] += _num(me.get("brier_mid")) * k
            if _opt_num(me.get("logloss_model")) is not None:
                ll_n += k
                s["logloss_model"] += _num(me.get("logloss_model")) * k
                s["logloss_mid"] += _num(me.get("logloss_mid")) * k
        for b, row in (me.get("by_tau") or {}).items():
            if not isinstance(row, dict):
                continue
            kb = _num(row.get("n"))
            if kb <= 0:
                continue
            d = by.setdefault(b, {"n": 0.0, "bm": 0.0, "bd": 0.0})
            d["n"] += kb
            d["bm"] += _num(row.get("brier_model")) * kb
            d["bd"] += _num(row.get("brier_mid")) * kb
    out = {"n": n,
           "brier_model": s["brier_model"] / n if n else None,
           "brier_mid": s["brier_mid"] / n if n else None,
           "logloss_model": s["logloss_model"] / ll_n if ll_n else None,
           "logloss_mid": s["logloss_mid"] / ll_n if ll_n else None,
           "by_tau": {}}
    for b in _tau_order(by):
        d = by[b]
        out["by_tau"][b] = {"n": d["n"], "brier_model": d["bm"] / d["n"],
                            "brier_mid": d["bd"] / d["n"]}
    return out


def _tau_order(keys) -> List[str]:
    ks = list(keys)
    known = [b for b in TAU_BUCKETS if b in ks]
    return known + sorted(k for k in ks if k not in TAU_BUCKETS)


def agg_coverage(events: Sequence[dict]) -> dict:
    w = tw = 0.0
    out = {"events": len(events), "trades": 0.0, "reconnects": 0.0, "gaps": 0.0,
           "wx_rows": 0.0, "program_secs": 0.0, "unsettled": 0, "src": {}, "flags": {}}
    fm = fw = 0.0
    for ev in events:
        cv = ev.get("coverage") or {}
        ps = _num(cv.get("program_secs"))
        fmf = _opt_num(cv.get("fair_missing_frac"))
        if fmf is not None:
            fm += fmf * (ps if ps > 0 else 1.0)
            fw += ps if ps > 0 else 1.0
        for fl in ev.get("flags") or []:
            key = str(fl).split(":")[0][:60]
            out["flags"][key] = out["flags"].get(key, 0) + 1
        f = _opt_num(cv.get("book_trusted_frac"))
        if f is not None:
            wt = ps if ps > 0 else 1.0
            w += f * wt
            tw += wt
        for k in ("trades", "reconnects", "gaps", "wx_rows", "program_secs"):
            out[k] += _num(cv.get(k))
        src = cv.get("trades_src")
        if src:
            out["src"][src] = out["src"].get(src, 0) + 1
        if ev.get("settled") is False:
            out["unsettled"] += 1
    out["book_trusted_frac"] = w / tw if tw > 0 else None
    out["fair_missing_frac"] = fm / fw if fw > 0 else None
    return out


class Window:
    """The cumulative window: per-day events, per-day aggregates and every
    (variant key, event) pair for the paired comparisons."""

    def __init__(self, days_events: Dict[str, List[dict]]):
        self.days = list(days_events)
        self.days_events = days_events
        # the shadow's partial last day: weighted in per-day figures, out of
        # the verdict's mean / days positive (a lone day is never weighted)
        self.partial = ({d for d in self.days if is_partial_end(d)}
                        if len(self.days) > 1 else set())
        self.weights = {d: (day_weight(d) if d in self.partial else 1.0) for d in self.days}
        self.day_aggs = {d: agg_variants(evs) for d, evs in days_events.items()}
        self.by_key: Dict[str, Dict[str, Tuple[str, dict, dict]]] = {}
        for d, evs in days_events.items():
            for ev in evs:
                for key, v in (ev.get("variants") or {}).items():
                    if isinstance(v, dict):
                        self.by_key.setdefault(key, {})[ev["event"]] = (d, v, ev)
        self.all_events = [ev for d in self.days for ev in days_events[d]]
        self.agg = agg_variants(self.all_events)

    def names(self, fill: str) -> List[str]:
        return sorted({split_key(k)[0] for k in self.by_key if split_key(k)[1] == fill})

    def days_with(self, key: str) -> List[str]:
        return [d for d in self.days if key in self.day_aggs[d]]

    def full_days_with(self, key: str) -> List[str]:
        return [d for d in self.days_with(key) if d not in self.partial]

    def eff_days(self, days) -> float:
        """Day count with the partial last day counted by its weight (>= 1
        when any day is present, so a lone short day is never scaled up)."""
        ds = list(days)
        if not ds:
            return 0.0
        return max(sum(self.weights.get(d, 1.0) for d in ds), 1.0)

    def total(self, key: str, field: str = "net") -> float:
        return sum(_num(self.day_aggs[d][key].get(field)) for d in self.days_with(key))

    def has(self, key: str, field: str) -> bool:
        return any(self.day_aggs[d][key].get(field) is not None for d in self.days_with(key))

    def mc_se(self, key: str) -> Optional[float]:
        """Monte Carlo standard error of total(key) (swp; None otherwise)."""
        ses = [self.day_aggs[d][key].get("net_se") for d in self.days_with(key)]
        ses = [x for x in ses if x is not None]
        return math.sqrt(sum(x * x for x in ses)) if ses else None

    def per_day(self, key: str, field: str = "net") -> float:
        n = self.eff_days(self.days_with(key))
        return self.total(key, field) / n if n else 0.0

    def best(self, fill: str) -> Optional[str]:
        names = self.names(fill)
        if not names:
            return None
        return max(names, key=lambda n: (self.total(f"{n}|{fill}"), n))


# --------------------------------------------------------------------------
# Statistics for the rule engine
# --------------------------------------------------------------------------


def _stats(vals: Sequence[float], n_days: float, today: float) -> dict:
    """n_days may be fractional (Window.eff_days: the partial last day)."""
    n = len(vals)
    total = float(sum(vals))
    nd = max(float(n_days), 1.0)
    se_total = None
    if n >= 2:
        se_total = statistics.stdev(vals) * math.sqrt(n)
    if se_total is not None and se_total > 1e-9:
        z = total / se_total
    elif n >= 2 and abs(total) > 1e-9:
        z = math.inf          # every event moved the same way by the same amount
    else:
        z = 0.0
    return {"n": n, "n_days": nd, "total": total, "per_day": total / nd,
            "se_per_day": (se_total / nd) if se_total is not None else None,
            "z": z, "n_nonzero": sum(1 for x in vals if abs(x) > 0.005),
            "today": today}


def pair_stats(win: Window, a_key: str, b_key: str, today: str,
               field: str = "net") -> dict:
    """b - a on the events both variants ran on."""
    ea, eb = win.by_key.get(a_key, {}), win.by_key.get(b_key, {})
    vals, days, tsum = [], set(), 0.0
    for e, (d, vb, _ev) in eb.items():
        if e not in ea:
            continue
        x = _num(vb.get(field)) - _num(ea[e][1].get(field))
        vals.append(x)
        days.add(d)
        if d == today:
            tsum += x
    return _stats(vals, win.eff_days(days), tsum)


def single_stats(win: Window, key: str, today: str,
                 fn: Optional[Callable[[dict, dict], float]] = None) -> dict:
    """Per-event value of one variant (default: net)."""
    vals, days, tsum = [], set(), 0.0
    for e, (d, v, ev) in win.by_key.get(key, {}).items():
        x = fn(v, ev) if fn else _num(v.get("net"))
        vals.append(x)
        days.add(d)
        if d == today:
            tsum += x
    return _stats(vals, win.eff_days(days), tsum)


def gate(st: dict, min_usd: float = MIN_USD_PER_DAY) -> List[str]:
    """Why this evidence is noise ([] = it passes)."""
    why = []
    if abs(st["per_day"]) < min_usd:
        why.append(f"{money(st['per_day'])}/day is under ${min_usd:.0f}")
    if st["n_nonzero"] < MIN_EVENTS:
        why.append(f"only {st['n_nonzero']} differing event(s)")
    if abs(st["z"]) < MIN_Z:
        why.append(f"within {abs(st['z']):.1f} SE of zero")
    return why


def evidence(label: str, st: dict) -> str:
    se = st.get("se_per_day")
    se_s = f" +/- {money(se).lstrip('+')}" if se is not None else ""
    days = st["n_days"]
    return (f"{label}: {money(st['per_day'])}/day{se_s} over {st['n']} events / "
            f"{_fmt_days(days)} day{'s' if days != 1 else ''} (report day {money(st['today'])})")


def _sugg(sid: str, kind: str, title: str, ev: str, change: str, usd: float,
          why: Optional[List[str]] = None) -> dict:
    return {"id": sid, "kind": kind, "title": title, "evidence": ev,
            "change": change, "usd": usd, "suppressed": bool(why),
            "why": "; ".join(why or []),
            # "keep ..." / "no change ..." confirm the current setting
            "action": not change.startswith(("keep", "no change"))}


# --------------------------------------------------------------------------
# The rule engine
# --------------------------------------------------------------------------

_PAIR_RULES = (
    # id, a, b (delta = b - a), label
    ("window", "A", "B", "Landing-window value (B-A)"),
    ("back", "B", "C", "Back 2 ticks vs pull (C-B)"),
    ("size", "B", "D", "Bigger live size (D-B)"),
    ("decided", "B", "E", "Decided strikes on (E-B)"),
    ("guard", "H", "B", "Guard value (B-H)"),
    ("padhold", "G0", "G", "Pad hold value (G-G0)"),
)


def _hold_s(policy: "Policy", name: Optional[str]) -> float:
    """The variant's pad_hold_open_s (0 when unset or not a number)."""
    if not name:
        return 0.0
    v = _opt_num(policy.get(name, "pad_hold_open_s"))
    return v if v is not None and v > 0 else 0.0


def suggest(win: Window, today: str, policy: Policy, market: dict, model: dict,
            health: Optional[dict] = None) -> Tuple[List[dict], List[str]]:
    """(suggestions, notes). Suggestions are sorted: actionable first by $,
    then risks, ops, notes; suppressed ones are kept (rendered separately)."""
    out: List[dict] = []
    notes: List[str] = []
    fill = headline_fill(win)
    if fill != HEADLINE_FILL:
        notes.append(f"No {HEADLINE_FILL} results in the window (sim output from before "
                     f"SIM_VERSION 6): the rules use the {fill} fill model.")
    names = win.names(fill)
    L = {c: resolve_variant(c, names) for c in "ABCDEFGH"}
    # G0 = G without the 10/16 pad hold (G0_nohold): the decided-only
    # baseline. Optional (older windows have no G0), so not in the
    # missing-letter note.
    L["G0"] = resolve_variant("G0", names)
    k = lambda n: f"{n}|{fill}"   # noqa: E731
    missing = [c for c in "ABCDEFGH" if L[c] is None]
    if names and missing:
        notes.append("Pair rules skipped for absent variant(s) "
                     + ", ".join(missing) + f" (variants present: {', '.join(names)}).")
    best = win.best(fill)

    def pair(rule_id, a, b, label):
        return pair_stats(win, k(a), k(b), today), label

    # 1 window value B - A
    if L["A"] and L["B"]:
        st, label = pair("window", L["A"], L["B"], _PAIR_RULES[0][3])
        d = st["per_day"]
        if d >= 0:
            out.append(_sugg("window", "policy",
                             f"Pulling in the landing window earns {usd(d)}/day: keep it",
                             evidence(label, st),
                             f"keep {L['B']}'s window ({policy.diff_text(L['A'], L['B'])})",
                             d, gate(st)))
        else:
            out.append(_sugg("window", "policy",
                             f"Pulling in the landing window costs {usd(d)}/day",
                             evidence(label, st),
                             f"quote through the window: {policy.diff_text(L['B'], L['A'])}",
                             d, gate(st)))
    # 2 back vs pull C - B
    if L["B"] and L["C"]:
        st, label = pair("back", L["B"], L["C"], _PAIR_RULES[1][3])
        d = st["per_day"]
        if d >= 0:
            out.append(_sugg("back", "policy",
                             f"Backing 2 ticks beats pulling by {usd(d)}/day",
                             evidence(label, st),
                             policy.diff_text(L["B"], L["C"]), d, gate(st)))
        else:
            out.append(_sugg("back", "policy",
                             f"Pulling beats backing 2 ticks by {usd(d)}/day: keep the pull",
                             evidence(label, st),
                             f"no change (keep {L['B']}: not {policy.diff_text(L['B'], L['C'])})",
                             d, gate(st)))
    # 3 size D - B, as $ per extra contract of q_live
    if L["B"] and L["D"]:
        st, label = pair("size", L["B"], L["D"], _PAIR_RULES[2][3])
        d = st["per_day"]
        qb = _opt_num(policy.get(L["B"], "q_live"))
        qd = _opt_num(policy.get(L["D"], "q_live"))
        if qb is not None and qd is not None and qd != qb:
            marg = d / (qd - qb)
            ev = evidence(label, st) + f"; {money(marg, 3)}/day per extra contract of q_live"
            if marg >= 0:
                nxt = qd + (qd - qb)
                out.append(_sugg("size", "policy",
                                 f"More live size pays {usd(marg, 3)}/day per extra contract",
                                 ev, f"raise q_live {qb:g} -> {qd:g} (next step to test: {nxt:g})",
                                 d, gate(st)))
            else:
                lower = max(1.0, qb - abs(qd - qb) / 2)
                out.append(_sugg("size", "policy",
                                 f"Extra live size loses {usd(marg, 3)}/day per contract",
                                 ev, f"keep q_live at {qb:g}, not {qd:g} (or test {lower:g})",
                                 d, gate(st)))
        else:
            change = (policy.diff_text(L["B"], L["D"]) if d >= 0
                      else f"keep {L['B']} ({policy.diff_text(L['D'], L['B'])})")
            out.append(_sugg("size", "policy",
                             f"Bigger size {'adds' if d >= 0 else 'costs'} {usd(d)}/day",
                             evidence(label, st), change, d, gate(st)))
    # 4 decided E - B, and decided strikes alone: G0 (no pad hold) when the
    # window has it, else G (labelled with its hold, if any)
    g_alone = L["G0"] or L["G"]
    g_hold = _hold_s(policy, g_alone)
    g_tag = f" with the {g_hold:g} s pad hold" if g_hold else ""
    if L["B"] and (L["E"] or g_alone):
        st_e = pair_stats(win, k(L["B"]), k(L["E"]), today) if L["E"] else None
        st_g = single_stats(win, k(g_alone), today) if g_alone else None
        parts = []
        if st_e:
            parts.append(evidence(_PAIR_RULES[3][3], st_e))
        if st_g:
            parts.append(evidence(f"{g_alone} alone (decided only"
                                  + (f" + {g_hold:g} s pad hold" if g_hold else "") + ")", st_g))
        ev = "; ".join(parts)
        if st_e and (not gate(st_e) or not st_g or gate(st_g)):
            d = st_e["per_day"]
            if d >= 0:
                out.append(_sugg("decided", "policy",
                                 f"Quoting decided strikes adds {usd(d)}/day",
                                 ev, policy.diff_text(L["B"], L["E"]), d, gate(st_e)))
            else:
                out.append(_sugg("decided", "policy",
                                 f"Quoting decided strikes costs {usd(d)}/day: leave them off",
                                 ev, f"no change (not {policy.diff_text(L['B'], L['E'])})",
                                 d, gate(st_e)))
        elif st_g:
            d = st_g["per_day"]
            src = L["E"] or g_alone
            if d >= 0:
                out.append(_sugg("decided", "policy",
                                 f"Decided strikes alone{g_tag} earn {usd(d)}/day",
                                 ev, f"enable decided strikes ({policy.diff_text(L['B'], src)})",
                                 d, gate(st_g)))
            else:
                out.append(_sugg("decided", "policy",
                                 f"Decided strikes alone{g_tag} lose {usd(d)}/day: "
                                 "leave them off",
                                 ev, f"no change (not {policy.diff_text(L['B'], src)})",
                                 d, gate(st_g)))
    # 4b pad hold G - G0: what holding the 1c pads after the open is worth
    # (Jack 2026-10-16: "hold the pads for the first 3 minutes of each hour
    # going forward"); paired on the events both ran on
    if L["G"] and L["G0"] and _hold_s(policy, L["G"]) != _hold_s(policy, L["G0"]):
        st, label = pair("padhold", L["G0"], L["G"], _PAIR_RULES[5][3])
        d = st["per_day"]
        h, h0 = _hold_s(policy, L["G"]), _hold_s(policy, L["G0"])
        if d >= 0:
            out.append(_sugg("padhold", "policy",
                             f"Holding the 1c pads for the first {h:g} s after the open "
                             f"saves {usd(d)}/day: keep it",
                             evidence(label, st),
                             f"keep pad_hold_open_s={h:g} ({L['G']}, not {L['G0']})",
                             d, gate(st)))
        else:
            out.append(_sugg("padhold", "policy",
                             f"Holding the 1c pads for the first {h:g} s after the open "
                             f"costs {usd(d)}/day",
                             evidence(label, st),
                             f"pad_hold_open_s {h:g} -> {h0:g} ({L['G0']}'s setting)",
                             d, gate(st)))
    # 5 full F vs best
    if L["F"] and best:
        if best == L["F"]:
            others = [n for n in names if n != best]
            runner = max(others, key=lambda n: win.total(k(n))) if others else None
            if runner:
                st = pair_stats(win, k(runner), k(best), today)
                out.append(_sugg("full", "policy",
                                 f"{best} (full stack) is the best variant, "
                                 f"{money(st['per_day'])}/day over {runner}",
                                 evidence(f"{best} - {runner}", st),
                                 f"start from {best}: {policy.diff_text(runner, best)}",
                                 st["per_day"], gate(st)))
        else:
            st = pair_stats(win, k(best), k(L["F"]), today)
            out.append(_sugg("full", "policy",
                             f"The full stack {L['F']} trails {best} by "
                             f"{usd(st['per_day'])}/day",
                             evidence(f"{L['F']} - {best}", st),
                             f"use {best}, not {L['F']}: {policy.diff_text(L['F'], best)}",
                             -st["per_day"], gate(st)))
    # 6 guard value B - H
    guard_st = None
    if L["B"] and L["H"]:
        st, label = pair("guard", L["H"], L["B"], _PAIR_RULES[4][3])
        guard_st = st
        d = st["per_day"]
        gk = _opt_num(policy.get(L["B"], "guard_k"))
        if d >= 0:
            out.append(_sugg("guard", "policy",
                             f"The fair-value guard saves {usd(d)}/day: keep it",
                             evidence(label, st),
                             f"keep the guard ({'guard_k=' + format(gk, 'g') if gk is not None else policy.diff_text(L['H'], L['B'])})",
                             d, gate(st)))
        else:
            wider = f"widen guard_k {gk:g} -> {gk * 1.5:g}" if gk is not None else "widen guard_k x1.5"
            out.append(_sugg("guard", "policy",
                             f"The fair-value guard costs {usd(d)}/day",
                             evidence(label, st),
                             f"{wider}, or drop it ({policy.diff_text(L['B'], L['H'])})",
                             d, gate(st)))
    # 7 fill-model gap on the best variant: cons - swp (what the sweepers
    # take) under swp; opt - cons (the queue assumption) on older output
    other = "cons" if fill == "swp" else ("opt" if fill == "cons" else None)
    if best and other and f"{best}|{other}" in win.by_key:
        st = pair_stats(win, k(best), f"{best}|{other}", today)
        base_pd = win.per_day(k(best))
        oth_pd = win.per_day(f"{best}|{other}")
        gap = st["per_day"]
        q = _opt_num(policy.get(best, "q_live"))
        half = f"q_live {q:g} -> {q / 2:g}" if q else "half size"
        flips = (base_pd > 0) != (oth_pd > 0) and abs(gap) > 0
        big = abs(gap) > FILL_GAP_FRAC * abs(base_pd)
        why = [] if (flips or big) else [f"gap is under {FILL_GAP_FRAC:.0%} of the {fill} net"]
        why += [w for w in gate(st) if "SE" not in w]   # a gap is a range, not a test
        if fill == "swp":
            ftc = win.total(f"{best}|swp", "filler_contracts") if win.has(
                f"{best}|swp", "filler_contracts") else None
            out.append(_sugg("fillgap", "risk",
                             f"Sweep risk on {best}: cons {money(oth_pd)}/day vs swp "
                             f"{money(base_pd)}/day",
                             evidence(f"cons - swp on {best}", st)
                             + (f"; swp sweeps {qty(ftc)} 1c filler contracts over the window"
                                if ftc else "")
                             + ("; the sign flips with the sweepers" if flips else ""),
                             f"size live by swp, not cons: start at {half} and compare live "
                             f"1c fills against the swp sweeps before scaling", gap, why))
        else:
            out.append(_sugg("fillgap", "risk",
                             f"Fill-model risk on {best}: opt {money(oth_pd)}/day vs cons "
                             f"{money(base_pd)}/day",
                             evidence(f"opt - cons on {best}", st)
                             + ("; the sign flips with the queue assumption" if flips else ""),
                             f"go live at {half} first and compare real queue fills against "
                             f"the sim's cons fills before scaling", gap, why))
    # 8 tau-bucket losses -> live_stop_min
    if best:
        _tau_rule(win, today, policy, best, out)
    # 9 losing ET hours -> smaller size there
    if best:
        _hour_rule(win, today, policy, best, out)
    # 10 model worse than the mid
    if model.get("n"):
        bm, bd = model.get("brier_model"), model.get("brier_mid")
        if bm is not None and bd is not None and bm > bd + MODEL_MARGIN:
            worse = [b for b, r in model["by_tau"].items()
                     if r["brier_model"] > r["brier_mid"] + MODEL_MARGIN]
            gk = _opt_num(policy.get(best, "guard_k")) if best else None
            wider = f"widen guard_k {gk:g} -> {gk * 1.5:g}" if gk is not None else "widen guard_k x1.5"
            ev = (f"Brier model {bm:.4f} vs book mid {bd:.4f} over {model['n']:.0f} "
                  f"strike-minutes" + (f"; worse at tau {', '.join(worse)}" if worse else ""))
            usd_v = 0.0
            if guard_st is not None:
                ev += "; " + evidence("guard value (B-H)", guard_st)
                usd_v = guard_st["per_day"]
            elif best:
                usd_v = win.per_day(k(best), "reward")
                ev += f"; the guard caps quotes earning {money(usd_v)}/day"
            why = [] if model["n"] >= MODEL_MIN_N else [f"only {model['n']:.0f} strike-minutes"]
            out.append(_sugg("model", "risk",
                             "The guard relies on a model worse than the market",
                             ev, f"{wider}, or cap sides at the book mid instead of the model",
                             usd_v, why))
    # 11 low paying fraction
    pf = market.get("paying_frac_without_us")
    if pf is not None and pf < LOW_PAYING_FRAC:
        pads = [kk for kk in (policy.params(best) if best else {}) if "pad" in kk.lower()]
        rew = win.per_day(k(best), "reward") if best else 0.0
        out.append(_sugg("paying", "note",
                         f"Only {pct(pf)} of strike-seconds pay without us",
                         f"most of the {usd(rew)}/day reward needs our orders to qualify "
                         f"the second side; locked at 99: {pct(market.get('locked99_frac'))}",
                         "keep the qualification pads on"
                         + (f" ({', '.join(f'{p}={_fmt_val(policy.get(best, p))}' for p in pads)})"
                            if pads else ""), rew))
    # 12 landing-window position vs where the station readings actually land
    if health:
        ref = L["B"] or best or resolve_variant("B", sorted(policy.variants))
        if ref:
            _window_position_rule(health, policy, ref, L, win, today, out)
    # 13 ops
    if health:
        _ops_rules(health, win, today, best, out)
    order = {"policy": 0, "risk": 1, "ops": 2, "note": 3}
    out.sort(key=lambda s: (s["suppressed"], order.get(s["kind"], 9), not s["action"],
                            -abs(s["usd"])))
    return out, notes


def _bucket_bounds(b: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        lo, hi = b.split("-")
        return float(lo), float(hi)
    except ValueError:
        return None, None


def _tau_rule(win: Window, today: str, policy: Policy, best: str, out: List[dict]) -> None:
    """Raise live_stop_min when the tau buckets it would newly switch off lose.

    Only buckets wholly at or above the current live_stop_min count: below it
    live quoting is already off (what remains there is pads and decided
    strikes, which live_stop_min does not touch). A bucket that straddles the
    current value mixes both, and the sim's by_tau has no class split, so it
    is left out of the $ evidence (named instead). No rule for a variant that
    quotes no live strikes (live=False)."""
    if policy.get(best, "live") is False:
        return
    key = f"{best}|{headline_fill(win)}"
    bt = win.agg.get(key, {}).get("by_tau", {})
    nd = win.eff_days(win.days_with(key)) or 1.0
    cur = _opt_num(policy.get(best, "live_stop_min"))
    c = cur if cur is not None else 0.0
    val = lambda b: _num((bt.get(b) or {}).get("reward")) + _num((bt.get(b) or {}).get("pnl"))  # noqa: E731
    best_cut, best_gain, run = None, 0.0, 0.0
    used: List[str] = []
    cut_buckets: List[str] = []
    straddle: Optional[str] = None
    for b in TAU_BUCKETS:
        lo, hi = _bucket_bounds(b)
        if lo is None or hi is None or hi <= c:
            continue                  # live quoting is already off there
        if lo < c:
            straddle = b              # mixes tau < live_stop_min: unpriceable
            continue
        run += val(b)
        used.append(b)
        if -run / nd > best_gain:
            best_cut, best_gain, cut_buckets = hi, -run / nd, list(used)
    cur_s = ("%g" % cur) if cur is not None else "?"
    if best_cut is None:
        if straddle and val(straddle) / nd <= -MIN_USD_PER_DAY:
            _lo, s_hi = _bucket_bounds(straddle)
            out.append(_sugg(
                "tau", "policy",
                f"{best} loses {usd(val(straddle) / nd)}/day at tau {straddle} min, where "
                f"live_stop_min {cur_s} already sits",
                f"tau {straddle} (reward + fills): {money(val(straddle) / nd)}/day",
                f"raise live_stop_min {cur_s} -> {s_hi:g} only after a sim variant prices it",
                val(straddle) / nd,
                [f"the {straddle} bucket mixes tau < {cur_s} min (pads and decided strikes, "
                 "which live_stop_min does not touch); the sim's by_tau has no class split"]))
        return

    def fn(v, _ev):
        r = v.get("by_tau") or {}
        return sum(_num((r.get(b) or {}).get("reward")) + _num((r.get(b) or {}).get("pnl"))
                   for b in cut_buckets)

    st = single_stats(win, key, today, fn)
    per = ", ".join(f"{b}: {money(val(b) / nd)}" for b in cut_buckets)
    first_lo = _bucket_bounds(cut_buckets[0])[0]
    unpriced = (f"; tau {c:g}-{first_lo:g} min (inside the {straddle} bucket) is not priced"
                if straddle and first_lo is not None and first_lo > c else "")
    out.append(_sugg("tau", "policy",
                     f"{best} loses {usd(best_gain)}/day at tau {first_lo:g}-{best_cut:g} min",
                     evidence(f"tau {first_lo:g}-{best_cut:g} min (reward + fills)", st)
                     + f"; by bucket/day {per} (by_tau mixes in decided-strike reward)"
                     + unpriced,
                     f"raise live_stop_min {cur_s} -> {best_cut:g}",
                     -best_gain, gate(st)))


def _hour_rule(win: Window, today: str, policy: Policy, best: str, out: List[dict]) -> None:
    """Halve size in ET hours that lose, tested hour by hour.

    Picking the hours whose sum is negative and then testing those same
    values passes noise almost every day (selection bias), so each hour
    stands on its own: z = its mean net / (spread / sqrt(n)), the spread
    being the larger of the pooled within-hour sd and the hour's own sd,
    with a Bonferroni bar over 24 hours (HOUR_Z), at least HOUR_MIN_DAYS days
    and a loss on >= HOUR_NEG_FRAC of them. Negative hours that miss the bar
    are listed below noise."""
    key = f"{best}|{headline_fill(win)}"
    by_h: Dict[int, List[Tuple[str, float]]] = {}
    for e, (d, v, ev) in win.by_key.get(key, {}).items():
        h = event_hour(e, ev)
        if h is not None:
            by_h.setdefault(h, []).append((d, _num(v.get("net"))))
    nd = win.eff_days(win.days_with(key)) or 1.0
    cands = sorted(h for h, xs in by_h.items()
                   if len(xs) >= 2 and sum(x for _, x in xs) < 0)
    if not cands:
        return
    ss, dof = 0.0, 0
    for xs in by_h.values():
        if len(xs) >= 2:
            vals_h = [x for _, x in xs]
            m = sum(vals_h) / len(vals_h)
            ss += sum((x - m) ** 2 for x in vals_h)
            dof += len(vals_h) - 1
    s_pool = math.sqrt(ss / dof) if dof >= 2 else 0.0
    rows: Dict[int, Tuple[float, float, int]] = {}
    for h in cands:
        xs = [x for _, x in by_h[h]]
        n = len(xs)
        m = sum(xs) / n
        s = max(s_pool, statistics.stdev(xs) if n >= 2 else 0.0)
        z = m / (s / math.sqrt(n)) if s > 1e-9 else (-math.inf if m < 0 else 0.0)
        neg = sum(1 for x in xs if x < 0) / n
        rows[h] = (z, neg, len({d for d, _ in by_h[h]}))
    bad = [h for h in cands if rows[h][0] <= -HOUR_Z and rows[h][1] >= HOUR_NEG_FRAC
           and rows[h][2] >= HOUR_MIN_DAYS]
    rule = (f"per-hour z <= -{HOUR_Z:g} (Bonferroni over 24 hours), >= {HOUR_MIN_DAYS} days, "
            f"negative on >= {HOUR_NEG_FRAC:.0%} of them")
    zs = lambda h: "-inf" if rows[h][0] == -math.inf else f"{rows[h][0]:.1f}"   # noqa: E731
    if not bad:
        worst = sorted(cands, key=lambda h: rows[h][0])[:4]
        tot = sum(x for h in worst for _, x in by_h[h])
        out.append(_sugg(
            "hours", "policy",
            f"{best} loses in ET hours {', '.join(f'{h:02d}' for h in sorted(worst))} "
            "(no hour is significant)",
            "per hour/day " + ", ".join(
                f"{h:02d}h {money(sum(x for _, x in by_h[h]) / nd)} (z {zs(h)}, "
                f"{rows[h][2]} day{'s' if rows[h][2] != 1 else ''})" for h in sorted(worst)),
            "no change", tot / nd, [f"no hour passes {rule}"]))
        return
    vals = [x for h in bad for _, x in by_h[h]]
    tsum = sum(x for h in bad for d, x in by_h[h] if d == today)
    st = _stats(vals, nd, tsum)
    q = _opt_num(policy.get(best, "q_live"))
    per = ", ".join(f"{h:02d}h {money(sum(x for _, x in by_h[h]) / nd)} (z {zs(h)}, "
                    f"{rows[h][1]:.0%} of {rows[h][2]} days negative)" for h in bad)
    if policy.get(best, "live") is False:
        # no live size to halve (decided-only / favorite-only variants): drop
        # the hours with the policy's own switch, quote_hours_et
        cur = policy.get(best, "quote_hours_et")
        keep = [h for h in (cur if isinstance(cur, (list, tuple)) else range(24)) if h not in bad]
        change = (f"stop quoting those settle hours: quote_hours_et "
                  f"{_fmt_val(cur) if cur is not None else 'all'} -> {_fmt_val(keep)}")
    else:
        change = ("halve size in those hours"
                  + (f" (q_live {q:g} -> {q / 2:g} there, an hour multiplier of 0.5)" if q else
                     " (an hour multiplier of 0.5)"))
    out.append(_sugg("hours", "policy",
                     f"{best} loses {usd(st['per_day'])}/day in ET hours "
                     f"{', '.join(f'{h:02d}' for h in bad)}",
                     evidence("losing hours", st) + f"; per hour/day {per}; each hour passed {rule}",
                     change,
                     st["per_day"],
                     # the per-hour test replaces the SE gate (re-testing the
                     # selected hours' values would be circular)
                     [w for w in gate(st) if "SE" not in w]))


def _window_position_rule(h: dict, policy: Policy, ref: str, L: dict, win: Window,
                          today: str, out: List[dict]) -> None:
    land = (h.get("wx") or {}).get("land_all") or []
    lo, hi = _opt_num(policy.get(ref, "win_lo")), _opt_num(policy.get(ref, "win_hi"))
    mode = policy.get(ref, "window_mode") or policy.get(ref, "win_mode")
    if lo is None or hi is None or mode in (None, "none", "off", False) or len(land) < 30:
        return
    inside = sum(1 for x in land if lo <= x <= hi) / len(land)
    if inside >= 0.5:
        return
    p10, p90 = _quantile(land, 0.1), _quantile(land, 0.9)
    new_lo, new_hi = max(0, int(p10) - 1), min(59, int(math.ceil(p90)) + 4)
    ev = (f"{pct(inside, 0)} of {len(land):,} station readings on {today} landed inside the "
          f":{lo:02.0f}-:{hi:02.0f} window; they land at :{p10:02.0f}-:{p90:02.0f} (p10-p90), "
          "and the informed burst is the 0-2 s after a landing")
    usd_v = 0.0
    if L.get("A") and L.get("B"):
        hf = headline_fill(win)
        st = pair_stats(win, f"{L['A']}|{hf}", f"{L['B']}|{hf}", today)
        ev += "; " + evidence("window value as configured (B-A)", st)
        usd_v = st["per_day"]
    out.append(_sugg("winpos", "policy",
                     f"The landing window :{lo:02.0f}-:{hi:02.0f} misses most station landings",
                     ev, f"win_lo {lo:g} -> {new_lo}, win_hi {hi:g} -> {new_hi} "
                         "(re-sim the window variants to price it)", usd_v))


def _env_get(node: ast.AST, env) -> Optional[str]:
    """os.environ.get("NAME", "default") -> env value or the default."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and len(node.args) == 2
            and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)):
        return env.get(node.args[0].value) or node.args[1].value
    return None


def _eval_setting(node: ast.AST, env):
    """A module-level setting: a number literal, float/int(os.environ.get(..))
    or os.environ.get(..) ==/!= "x". None when it is anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("float", "int") and len(node.args) == 1):
        raw = _env_get(node.args[0], env)
        if raw is not None:
            try:
                return (float if node.func.id == "float" else int)(raw)
            except ValueError:
                return None
    if (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Eq, ast.NotEq)) and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)):
        raw = _env_get(node.left, env)
        if raw is not None:
            same = raw == node.comparators[0].value
            return same if isinstance(node.ops[0], ast.Eq) else not same
    return None


def collector_settings(path: Optional[str] = None, env=None) -> dict:
    """The collector's live station-poll settings (WX_LO / WX_HI / WX_FAST /
    WX_SLOW / WX_YIELD_FAST / SIGN_AFTER_429 / SIGN_ON_429), read with ast from
    temp_shadow_collector.py with its env overrides (TEMP_SHADOW_WX_LO ...)
    applied, never imported. Falls back to the spec's values per setting."""
    env = os.environ if env is None else env
    out: Dict[str, Any] = dict(_COLLECTOR_DEFAULTS)
    out["_source"] = "spec defaults"
    path = path or os.path.join(C.REPO, "temp_shadow_collector.py")
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return out
    found = 0
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                pairs = [(tgt.id, node.value)]
            elif (isinstance(tgt, ast.Tuple) and isinstance(node.value, ast.Tuple)
                  and len(tgt.elts) == len(node.value.elts)):
                pairs = [(t.id, v) for t, v in zip(tgt.elts, node.value.elts)
                         if isinstance(t, ast.Name)]
            else:
                continue
            for name, val in pairs:
                if name in _COLLECTOR_DEFAULTS:
                    x = _eval_setting(val, env)
                    if x is not None:
                        out[name] = x
                        found += 1
    if found:
        out["_source"] = os.path.basename(path)
    return out


def _ops_rules(h: dict, win: Window, today: str, best: Optional[str], out: List[dict]) -> None:
    rew = win.per_day(f"{best}|{headline_fill(win)}", "reward") if best else 0.0
    cfg = h.get("collector_cfg") or collector_settings()
    lo_cur, hi_cur = _num(cfg.get("WX_LO"), 18.0), _num(cfg.get("WX_HI"), 50.0)
    poll = h.get("poll") or {}
    n, n429 = poll.get("n", 0), poll.get("n429", 0)
    signed = poll.get("signed_minutes", 0) or 0
    rate = n429 / n if n else 0.0
    if (n429 >= OPS_429_MIN and rate > OPS_429_RATE) or signed:
        # back off, never sign sooner: the signing key is the IMM's fleet
        # key, so signed polling spends the live bot's rate budget
        slow, yf = _num(cfg.get("WX_SLOW"), 5.0), _num(cfg.get("WX_YIELD_FAST"), 2.0)
        change = (f"collector: back off -- WX_SLOW {slow:g} -> {2 * slow:g} s (polls outside "
                  f":{lo_cur:02.0f}-:{hi_cur:02.0f}) and WX_YIELD_FAST {yf:g} -> {yf + 1:g} s "
                  "after a 429")
        if signed:
            change += ("; set TEMP_SHADOW_SIGN_ON_429=0 -- signed polling spends the IMM's "
                       "fleet-key budget")
        out.append(_sugg("ops429", "ops",
                         f"{n429:,} HTTP 429s from live_data on {today} ({pct(rate)} of polls)"
                         + (f"; signed for {signed} poll-minute(s)" if signed else ""),
                         f"{n429:,} of {n:,} station polls throttled ({pct(rate)}; the bar is "
                         f">= {OPS_429_MIN} and > {OPS_429_RATE:.0%}); signed-mode poll-minutes "
                         f"{signed} (the signing key is the IMM's fleet key)",
                         change, 0.0))
    ws = h.get("ws") or {}
    up = ws.get("uptime")
    if up is not None and ws.get("minutes", 0) > 0 and up < OPS_UPTIME_MIN:
        out.append(_sugg("opsws", "ops", f"WS uptime {pct(up)} on {today}",
                         f"{ws.get('connects', 0)} connects, {ws.get('disconnects', 0)} disconnects, "
                         f"{ws.get('gaps', 0)} seq gaps"
                         + (f"; up to {usd(rew * (1 - up))}/day of reward is scored on "
                            "untrusted books" if rew > 0 else ""),
                         "check collector.log for the disconnect reasons and that the "
                         "'KL temp-shadow collector' restart loop is running", 0.0))
    pc = poll.get("coverage")
    if pc is not None and poll.get("expected", 0) > 0 and pc < OPS_UPTIME_MIN:
        out.append(_sugg("opspoll", "ops", f"Station feed polled in only {pct(pc)} of minutes",
                         f"poll rows in {poll.get('minutes', poll.get('rows', 0))} of "
                         f"{poll.get('expected', 0)} minutes",
                         "check collector.log / collector_status.json; restart the collector task",
                         0.0))
    wx = h.get("wx") or {}
    lag = wx.get("obs_lag_p90")
    if lag is not None and lag > OPS_LAG_P90_MAX and wx.get("obs_lag_n", 0) >= MIN_EVENTS:
        lo, hi = wx.get("land_sec_p10"), wx.get("land_sec_p90")
        land = (f"; the last station lands at :{lo:02.0f}-:{hi:02.0f} of the minute (p10-p90)"
                if lo is not None and hi is not None else "")
        fast = _num(cfg.get("WX_FAST"), 1.0)
        gap = _num(cfg.get("WX_MIN_GAP"), 0.8)
        src = f" (current value from {cfg.get('_source')})"
        if lo is not None and lo < lo_cur:
            new_lo = max(0, int(math.floor(lo)) - 2)          # always below lo_cur
            change = (f"collector: WX_LO {lo_cur:g} -> {new_lo} (TEMP_SHADOW_WX_LO){src} so the "
                      f"{fast:g} s polling covers the landings (outside :{lo_cur:02.0f}-"
                      f":{hi_cur:02.0f} it polls every {_num(cfg.get('WX_SLOW'), 5.0):g} s)")
        elif fast > gap + 1e-9:
            change = (f"collector: WX_FAST {fast:g} -> {gap:g} s inside :{lo_cur:02.0f}-"
                      f":{hi_cur:02.0f} (the 1.25 req/s cap){src}; check poll latency and "
                      "CloudFront caching (max-age=1)")
        else:
            change = (f"no change to the collector: it already polls every {fast:g} s (the cap) inside "
                      f":{lo_cur:02.0f}-:{hi_cur:02.0f}; check poll latency and CloudFront "
                      "caching (max-age=1)")
        out.append(_sugg("opslag", "ops", f"Observation lag p90 {lag:.1f} s",
                         f"we see a minute's last station {wx.get('obs_lag_median') or 0:.1f} s "
                         f"(median) / {lag:.1f} s (p90) after Kalshi received it, over "
                         f"{wx.get('obs_lag_n')} minutes{land}; the informed burst is 0-2 s",
                         change, 0.0))
    miss = [m for m in (h.get("events") or {}).get("missing", []) if m.get("fixable")]
    if miss:
        out.append(_sugg("opsmiss", "ops", f"{len(miss)} event(s) settled but not simmed",
                         ", ".join(m["event"] for m in miss[:6]),
                         f"python temp_shadow_sim.py day --day {today} --force", 0.0))
    rep = h.get("replica") or {}
    if rep.get("n", 0) >= 100 and rep.get("rate") is not None and rep["rate"] < REPLICA_MIN_RATE:
        out.append(_sugg("replica", "risk", f"Index replica matched only {pct(rep['rate'])}",
                         f"{rep.get('exact', 0)} of {rep['n']} finalized minutes exact",
                         "refresh the station offsets (GET /live_data/weather/miami/calibrations) "
                         "in temp_shadow_model", 0.0))


# --------------------------------------------------------------------------
# The headline ranking: robust stress totals (Jack 2026-10-17, "1")
# --------------------------------------------------------------------------


def _ts(iso: str) -> float:
    t = C.parse_iso(iso)
    if t is None:
        raise ValueError(f"not an ISO time: {iso!r}")
    return t


def _settle(event: str) -> float:
    p = C.parse_event(str(event))
    return p[1] if p else 0.0


def _et_hm(ts: float) -> str:
    """'10/17 14:57 ET'."""
    d = C.et_dt(ts)
    return f"{d.month}/{d.day} {d.strftime('%H:%M')} ET"


def decision_fill(src=None) -> str:
    """The fill model the ranking, the pre-registered tests and the verdict
    use: always DECISION_FILL (stress). There is no fallback (review of
    338b4a5: a swp headline when stress was missing): without stress results
    the ranking is empty and the headline says "no variant passes (no stress
    results)"."""
    return DECISION_FILL


def boot_total(vals: Sequence[float], n: int = BOOT_N, seed: int = BOOT_SEED,
               block: int = BOOT_BLOCK
               ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Moving-block bootstrap of a total over hours given in settle order:
    ceil(k / block) blocks of `block` consecutive hours drawn with
    replacement (the last one cut so a resample has k hours), a fixed seed
    so a report is reproducible -> (5th percentile, 95th percentile, P(total
    > 0)); (None, None, None) without hours. Blocks of 3 because losses
    cluster in consecutive hours (one weather move fails the favorites of
    2-3 events: 26OCT1621 / 1622; review of 338b4a5); block 1 is the plain
    hour bootstrap. The settlement outcomes dominate this, not the Monte
    Carlo draws."""
    v = [float(x) for x in vals]
    k = len(v)
    if k == 0:
        return None, None, None
    L = max(1, min(int(block), k))
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    nb = -(-k // L)
    last = k - (nb - 1) * L
    rng = random.Random(seed)
    hi, hi_last = k - L, k - last
    tots = []
    for _ in range(n):
        t = 0.0
        for _b in range(nb - 1):
            s = rng.randint(0, hi)
            t += pre[s + L] - pre[s]
        s = rng.randint(0, hi_last)
        t += pre[s + last] - pre[s]
        tots.append(t)
    tots.sort()
    return (tots[int(0.05 * n)], tots[min(n - 1, int(0.95 * n))],
            sum(1 for t in tots if t > 0) / float(n))


def _hours(win: Window, key: str, days: Optional[Sequence[str]] = None
           ) -> List[Tuple[str, str, dict]]:
    """(event, day, result) for every window event-hour holding `key`
    (optionally only those of `days`), in settle order."""
    dd = set(days) if days is not None else None
    return sorted(((e, d, v) for e, (d, v, _ev) in win.by_key.get(key, {}).items()
                   if dd is None or d in dd), key=lambda x: (_settle(x[0]), x[0]))


def robust_total(nets: Sequence[float]) -> Optional[float]:
    """The total without its best and its worst hour (None under 2 hours)."""
    if len(nets) < 2:
        return None
    return sum(nets) - max(nets) - min(nets)


def judged_from_ts(name: str) -> Optional[float]:
    """The first settle time the headline may judge `name` on: it never
    counts the hours a variant was chosen from (review of 338b4a5). A tested
    variant from its test's start (FAV_night: Test A, FAV_night_agree: Test
    B); FAV_only and any other FAV_* variant (FAV_night's size / threshold
    variants) from Test A's start; a HEADLINE_JUDGED_FROM variant from its
    entry (G_decided_only: 10/17, its 180 s pad hold was chosen 10/16);
    every other variant from the window start (None)."""
    starts = {t["variant"]: _ts(t["start"]) for t in PREREG_TESTS}
    if name in starts:
        return starts[name]
    if name.startswith("FAV_"):
        return starts.get(PREREG_TESTS[0]["variant"])
    if name in HEADLINE_JUDGED_FROM:
        return _ts(HEADLINE_JUDGED_FROM[name])
    return None


def rank_variants(win: Window, policy: Optional[Policy] = None,
                  fill: Optional[str] = None) -> List[dict]:
    """Every variant with stress results in the cumulative window, ranked on
    its JUDGED hours only, those settling at or after judged_from_ts (the
    earlier ones are in-sample: shown for information, never ranked): the
    robust total = the total over those event-hours without the best and the
    worst hour (hours it rests nothing count $0: every hour is one of its
    hours). Beside it: the plain total, a 3-hour block bootstrap 90% CI and
    P(total > 0), the best / worst hour, "noise" (|best hour| + |worst hour|
    > |total|), the expected net at calibrated tails (variants that quote no
    live strikes only: cal_ok), and the in-sample hours' count and total.
    passes = robust > 0, P(total > 0) >= HEADLINE_P_MIN and judged hours on
    at least HEADLINE_MIN_DAYS ET days ("thin" below that: no single-day
    ranking). A variant without a judged hour yet comes after the ranked
    ones ("judged" False)."""
    fill = fill or decision_fill(win)
    rows, unjudged = [], []
    for name in win.names(fill):
        key = f"{name}|{fill}"
        allh = _hours(win, key)
        jf = judged_from_ts(name)
        ins = [h for h in allh if jf is not None and _settle(h[0]) < jf]
        hrs = [h for h in allh if not (jf is not None and _settle(h[0]) < jf)]
        base = {"name": name, "key": key, "fill": fill, "judged_from": jf,
                "in_hours": len(ins),
                "in_total": sum(_num(v.get("net")) for _e, _d, v in ins)}
        if not hrs:
            unjudged.append(dict(base, judged=False, hours=0, days=0, total=0.0, robust=None,
                                 best=None, best_event=None, worst=None, worst_event=None,
                                 ci_lo=None, ci_hi=None, p_pos=None, noise=False, cal=None,
                                 thin=True, passes=False, rank=None))
            continue
        nets = [_num(v.get("net")) for _e, _d, v in hrs]
        total = sum(nets)
        bi = max(range(len(nets)), key=lambda i: (nets[i], hrs[i][0]))
        wi = min(range(len(nets)), key=lambda i: (nets[i], hrs[i][0]))
        robust = robust_total(nets)
        lo, hi, ppos = boot_total(nets)
        cal = None
        if cal_ok(policy, name) and all(v.get("net_cal") is not None for _e, _d, v in hrs):
            cal = sum(_num(v.get("net_cal")) for _e, _d, v in hrs)
        rows.append(dict(base, judged=True, hours=len(nets),
                         days=len({d for _e, d, _v in hrs}), total=total, robust=robust,
                         best=nets[bi], best_event=hrs[bi][0], worst=nets[wi],
                         worst_event=hrs[wi][0], ci_lo=lo, ci_hi=hi, p_pos=ppos,
                         noise=abs(nets[bi]) + abs(nets[wi]) > abs(total), cal=cal,
                         thin=len({d for _e, d, _v in hrs}) < HEADLINE_MIN_DAYS,
                         passes=(robust is not None and robust > 0 and ppos is not None
                                 and ppos >= HEADLINE_P_MIN
                                 and len({d for _e, d, _v in hrs}) >= HEADLINE_MIN_DAYS)))
    rows.sort(key=lambda r: (r["robust"] if r["robust"] is not None else -math.inf,
                             r["total"], r["name"]), reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows + sorted(unjudged, key=lambda r: r["name"])


def headline_pick(rows: Sequence[dict]) -> dict:
    """{"top": the first judged row, "pick": the first judged row that passes
    (robust > 0 and P(total > 0) >= HEADLINE_P_MIN) or None, "pass": the top
    passes, "in_sample": the rows with in-sample hours (shown for
    information), "not_judged": the rows without a judged hour yet,
    "no_stress": no stress result in the window}. The headline names the
    top variant only when it passes; else "no variant passes" -- unless a
    lower-ranked one does, which is then named as such."""
    elig = [r for r in rows if r.get("judged")]
    top = elig[0] if elig else None
    pick = next((r for r in elig if r["passes"]), None)
    return {"top": top, "pick": pick, "pass": bool(top is not None and pick is top),
            "fill": DECISION_FILL, "no_stress": not rows,
            "in_sample": [r for r in rows if r.get("in_hours")],
            "not_judged": [r for r in rows if not r.get("judged")]}


def headline_phrase(hl: dict, subject: bool = False) -> str:
    """The ranking's one-line verdict (the subject's, or the headline's);
    the named variant's noise-dominated flag goes with it."""
    top, pick = hl.get("top"), hl.get("pick")
    if hl.get("no_stress"):
        return "no variant passes (no stress results)"
    if top is None:
        return ("no variant passes" if subject else "no variant passes (no variant has an "
                "hour after its first judged hour yet)")
    f = hl.get("fill") or DECISION_FILL

    def rob(r):
        return money(r["robust"], 0) if r["robust"] is not None else "n/a"

    def noise(r):
        if not r["noise"]:
            return ""
        return (" [noise-dominated]" if subject else
                "; noise-dominated (|best hour| + |worst hour| > |total|)")
    if hl.get("pass"):
        return (f"top {top['name']} robust {f} {rob(top)} (P>0 {top['p_pos']:.2f})" + noise(top)
                if subject else
                f"{top['name']} passes: robust {f} total {rob(top)} over {top['hours']} "
                f"event-hours, P(total > 0) {top['p_pos']:.2f}" + noise(top))
    if pick is not None:
        return (f"top robust {top['name']} does not pass; {pick['name']} does (robust "
                f"{rob(pick)}, P>0 {pick['p_pos']:.2f})" + noise(pick))
    thin = (f", {top['days']} judged day{'' if top['days'] == 1 else 's'}: the headline needs "
            f"{HEADLINE_MIN_DAYS}" if top.get("thin") else "")
    return ("no variant passes" if subject else
            f"no variant passes (top robust {f} total, for information: {top['name']} "
            f"{rob(top)}, P(total > 0) {top['p_pos']:.2f}{thin})")


def in_sample_text(r: dict) -> str:
    """A variant's in-sample hours (before its first judged hour), for
    information."""
    if not r.get("in_hours"):
        return ""
    return (f"{r['name']}: {r['in_hours']} hours before {_et_hm(r['judged_from'])}, stress "
            f"{money(r['in_total'])}")


# --------------------------------------------------------------------------
# The pre-registered tests (TEMP_SHADOW.md "Pre-registered tests")
# --------------------------------------------------------------------------


def _mmdd(day: str) -> str:
    return f"{int(day[5:7])}/{int(day[8:10])}"


def recent_rates(events: Sequence[dict]) -> dict:
    """The recent regime: every loaded sim file's fav_hits
    (temp_shadow_sweep.fav_hit_summary -- taker clusters that hit a favorite
    side's bids at >= 90c, with the favorite's outcome) pooled over the
    recorded period, per favorite fair bucket: clusters, the clusters the
    favorite lost, the upset rate (cluster-weighted, the 10/17 audit's
    measure), a strike-clustered bootstrap 90% CI and the model's mean
    longshot probability. {"buckets": {bucket: {...}}, "hours", "first",
    "last"}; hours = files with fav_hits."""
    by: Dict[str, List[list]] = {}
    hours = []
    for ev in events:
        fh = ev.get("fav_hits")
        if not isinstance(fh, list):
            continue
        hours.append(str(ev.get("event")))
        for row in fh:
            try:
                tk, b, n, u, sl = row[0], str(row[1]), int(row[2]), int(row[3]), float(row[4])
            except (TypeError, ValueError, IndexError):
                continue
            if n > 0:
                by.setdefault(b, []).append([f"{ev.get('event')}|{tk}", n, u, sl])
    out = {}
    for b, grp in sorted(by.items()):
        n = sum(g[1] for g in grp)
        u = sum(g[2] for g in grp)
        rng = random.Random(BOOT_SEED)
        rates = []
        for _ in range(RECENT_BOOT_N):
            s = [grp[rng.randrange(len(grp))] for _ in grp]
            nn = sum(g[1] for g in s)
            rates.append(sum(g[2] for g in s) / nn if nn else 0.0)
        rates.sort()
        out[b] = {"clusters": n, "upsets": u, "rate": u / n if n else None,
                  "ci": (rates[int(0.05 * RECENT_BOOT_N)],
                         rates[min(RECENT_BOOT_N - 1, int(0.95 * RECENT_BOOT_N))]),
                  "model": sum(g[3] for g in grp) / n if n else None,
                  "upset_strikes": sum(1 for g in grp if g[2] > 0)}
    hs = sorted(hours, key=_settle)
    first = last = None
    if hs:
        first, last = C.et_day(_settle(hs[0])), C.et_day(_settle(hs[-1]))
    return {"buckets": out, "hours": len(hs), "first": first, "last": last}


def recent_cal(results: Sequence[dict], rates: dict) -> Optional[float]:
    """The calibrated-tails expectation re-marked at the recent regime: per
    hour net_cal - (the table-marked P&L of its favorite-side fills) +
    sum over fair buckets of contracts x (1 - the recent upset rate) - cost.
    A bucket without a recent estimate keeps the table's marks; None when
    an hour lacks net_cal or fav_fills (older sim files)."""
    bk = (rates or {}).get("buckets") or {}
    tot = 0.0
    for v in results:
        if v.get("net_cal") is None or not isinstance(v.get("fav_fills"), dict):
            return None
        x = _num(v.get("net_cal"))
        for b, a in v["fav_fills"].items():
            r = (bk.get(b) or {}).get("rate")
            if r is None:
                continue
            x += -_num(a.get("pnl_cal")) + _num(a.get("contracts")) * (1.0 - r) - _num(a.get("cost"))
        tot += x
    return tot


def test_days(spec: dict) -> List[str]:
    """The window's ET days: spec["days"] consecutive days from its start's."""
    d0 = _dt.date.fromisoformat(C.et_day(_ts(spec["start"])))
    return [(d0 + _dt.timedelta(days=i)).isoformat() for i in range(int(spec["days"]))]


def test_bounds(spec: dict) -> Tuple[float, float]:
    """(start, end) epoch: the events settling in [start, end) are the
    window's hours; end = the end of its last ET day."""
    return _ts(spec["start"]), C.et_day_bounds(test_days(spec)[-1])[1]


def window_listing(t0: float, t1: float) -> dict:
    """One pass over the meta stream around a window: {"seen": the events
    the collector saw listed, "mk_rx": the rx of every markets row (sorted),
    "poll_min": the minutes with a station-poll row} -- for whether Kalshi
    listed an hour the window has no sim file for."""
    seen, mk, pm = set(), [], set()
    for rec in _iter_raw("meta", t0 - 4 * 3600, t1 + 3600):
        rx = _opt_num(rec.get("rx"))
        if rx is None:
            continue
        kind = rec.get("kind")
        if kind == "markets":
            ev = (rec.get("data") or {}).get("event_ticker")
            if ev:
                seen.add(ev)
            mk.append(rx)
        elif kind == "poll":
            pm.add(int(rx // 60))
    mk.sort()
    return {"seen": seen, "mk_rx": mk, "poll_min": pm}


def listed_by_kalshi(ev: str, lst: dict) -> Optional[bool]:
    """Did Kalshi list the hour? True: it has a settled file, or the
    collector saw its markets. False: the collector was recording across its
    listing (station polls in >= 80% of the 70 minutes before the
    settlement, and market discovery writing rows within [S - 3 h, S + 1 h],
    when every listed hour gets its open / close rows) and never saw it --
    not listed, so not expected. None: unknown (the collector was not
    recording then)."""
    if ev in lst["seen"] or os.path.exists(C.settled_file(ev)):
        return True
    cov, alive = _listing_coverage(ev, lst)
    return False if (cov >= 0.8 and alive) else None


def _listing_coverage(ev: str, lst: dict) -> Tuple[float, bool]:
    """(the share of the 70 minutes before the settlement with a station
    poll, whether market discovery wrote a row within [S - 3 h, S + 1 h])."""
    S = _settle(ev)
    mins = range(int((S - 4200) // 60), int(S // 60))
    cov = sum(1 for m in mins if m in lst["poll_min"]) / max(1, len(mins))
    i = bisect.bisect_left(lst["mk_rx"], S - 3 * 3600)
    return cov, (i < len(lst["mk_rx"]) and lst["mk_rx"][i] <= S + 3600)


def _missing_reason(ev: str, lst: dict, sim_summary: Optional[dict]) -> str:
    """Why an expected hour has no sim file, from run_sim's summary and the
    data root."""
    ss = sim_summary or {}
    ws = ss.get("window") or {}
    errs = {}
    for row in list(ss.get("errors") or []) + list(ws.get("errors") or []):
        try:
            errs[str(row[0])] = str(row[1])
        except (TypeError, IndexError):
            continue
    pend = set(ss.get("pending") or []) | set(ws.get("pending") or [])
    dfr = set(ss.get("deferred") or []) | set(ws.get("deferred") or [])
    if ev in errs:
        first = errs[ev].strip().splitlines()[0] if errs[ev].strip() else ""
        return ("the sim raised" + (": " + first if first else ""))[:200]
    if ev in pend:
        return "unsettled when the sim ran (the settlement pull was incomplete)"
    if ev in dfr:
        return "its re-sim was deferred (the time budget)"
    if os.path.exists(C.settled_file(ev)):
        return "settled, but no sim file (re-sim the day)"
    if ev in lst["seen"]:
        return "listed, but no settled file (the settlement pull failed?)"
    cov, _alive = _listing_coverage(ev, lst)
    if cov < 0.8:
        return "never seen: the collector was not recording then"
    return ("never seen, with no market discovery rows around it: whether Kalshi listed it is "
            "unknown")


_ABSENT = "(absent)"


def _pin_model(spec: dict) -> dict:
    """The test's model pin (its "model" entry: PREREG_PIN_MODEL)."""
    return spec.get("model") or PREREG_PIN_MODEL


def _pin_want(spec: dict) -> Dict[str, Any]:
    """The registered values a window file must carry, by field."""
    want: Dict[str, Any] = {}
    for k, x in spec["overrides"].items():
        want[f"overrides.{k}"] = x
    for k, x in spec["policy"].items():
        want[f"policy.{k}"] = x
    pm = _pin_model(spec)
    want["sim_version"] = str(pm["sim_version"])
    for blk in ("swp", "stress"):
        for k, x in pm[blk].items():
            want[f"{blk}.{k}"] = x
    want["fair_model.id"] = pm["fair_model_id"]
    return json.loads(json.dumps(want))          # tuples -> lists, as in the files


def _file_fields(ev: dict, spec: dict, full: bool) -> Dict[str, Any]:
    """The same fields from a sim file: the variant's overrides (policies)
    and the swp / stress identity always; with `full` (a file of the pinned
    SIM_VERSION, or of the current code's) also sim_version, the full
    effective policy (policy_full) and the fair model's id. A missing block
    reads as one "(absent)" field."""
    v = spec["variant"]
    got: Dict[str, Any] = {}
    pols = ev.get("policies")
    ov = pols.get(v) if isinstance(pols, dict) else None
    if isinstance(ov, dict):
        for k in set(spec["overrides"]) | set(ov):
            got[f"overrides.{k}"] = ov.get(k, _ABSENT)
    else:
        got["overrides"] = _ABSENT
    for blk in ("swp", "stress"):
        b = ev.get(blk)
        if not isinstance(b, dict):
            got[blk] = _ABSENT
            continue
        for k in _pin_model(spec)[blk]:
            got[f"{blk}.{k}"] = b.get(k, _ABSENT)
    if full:
        got["sim_version"] = (_ABSENT if ev.get("sim_version") is None
                              else str(ev.get("sim_version")))
        pfs = ev.get("policy_full")
        pf = pfs.get(v) if isinstance(pfs, dict) else None
        if isinstance(pf, dict):
            for k in set(spec["policy"]) | set(pf):
                got[f"policy.{k}"] = pf.get(k, _ABSENT)
        else:
            got["policy"] = _ABSENT
        fm = ev.get("fair_model")
        got["fair_model.id"] = fm.get("id", _ABSENT) if isinstance(fm, dict) else _ABSENT
    return json.loads(json.dumps(got, default=str))


def pin_check(spec: dict, files: Sequence[dict], full: Sequence[bool]) -> List[dict]:
    """The window files that ran another rule or model than the registered
    one: [{"field", "pinned", "found", "files": n, "first": event}] per
    distinct difference (none: the pin holds). Pinned (PREREG_TESTS /
    PREREG_PIN_MODEL): the variant's overrides and the swp / stress
    identity on every file; on the files read in full (full[i]) also
    sim_version, the full effective policy and the fair model."""
    want = _pin_want(spec)
    acc: Dict[tuple, dict] = {}
    for ev, f in zip(files, full):
        got = _file_fields(ev, spec, f)
        for k in sorted(got):
            if k in ("overrides", "policy", "swp", "stress"):      # a whole block missing
                pinned = ("(the registered rule)" if k in ("overrides", "policy")
                          else f"(the registered {k} identity)")
            else:
                pinned = want.get(k, _ABSENT)
            if got[k] == pinned:
                continue
            d = acc.setdefault((k, json.dumps(got[k], sort_keys=True, default=str)),
                               {"field": k, "pinned": pinned, "found": got[k], "files": 0,
                                "first": str(ev.get("event"))})
            d["files"] += 1
    return sorted(acc.values(), key=lambda d: (d["field"], -d["files"]))


_UNSET = object()


def prereg_test(spec: dict, win: Window, day: str, rates: Optional[dict] = None,
                now: Optional[float] = None, sim_summary: Optional[dict] = None,
                rec_start: Optional[float] = None, listing: Optional[Callable] = None,
                start_ts: Optional[float] = None, cur_version: Any = _UNSET) -> dict:
    """One pre-registered test on the report's data, exactly as written
    (TEMP_SHADOW.md "Pre-registered tests"; review of 338b4a5):
    * window: spec["days"] ET days from spec["start"] (ONE constant); its
      hours = the events settling in [start, end). start_ts (the "after
      registration" view) moves the start inside the same days.
    * expected hours: expected_events() per window day (one per ET hour,
      due SETTLE_LAG_S after its settlement, inside the shadow); an hour
      Kalshi never listed (listed_by_kalshi False) is not expected.
    * a window file counts only if it ran the registered rule and model
      (pin_check). A file of another SIM_VERSION than the pin and the
      current code's (an older file awaiting its re-sim) is not a result:
      its hour is missing, unless its rule or tables differ (INVALID).
    * status: "not started" before its first day; "INVALID (rule or model
      changed)" when any window file ran another rule or model (never PASS);
      "in progress (n/5 days)" until the window's last hour is due and
      every due hour is in; "INCOMPLETE" once it is over with expected hours
      lacking a stress result (named, with why: never PASS); else PASS
      (every criterion met) or FAIL.
    * criteria: total (> 0), days (>= PREREG_DAYS_POS of the days positive),
      robust (the total without the best and worst hour > 0), cal (the
      stress net_cal sum > 0); the recent-regime figure is information.
    The registered view also carries "known": its hours that settled before
    PREREG_REGISTERED_AT (known at registration), and "alt": the same test
    without them (start_ts = the registration) when there are any."""
    now = C.utcnow() if now is None else now
    if cur_version is _UNSET:
        cur_version = current_sim_version()
    v = spec["variant"]
    key = f"{v}|{DECISION_FILL}"
    reg0, end = test_bounds(spec)
    start = reg0 if start_ts is None else max(reg0, float(start_ts))
    wdays = test_days(spec)
    until = shadow_until()
    reg_at = _ts(PREREG_REGISTERED_AT)
    pin_v = str(_pin_model(spec)["sim_version"])
    cache: Dict[str, dict] = {}

    def lst() -> dict:
        if "x" not in cache:
            cache["x"] = listing() if listing else window_listing(reg0, end)
        return cache["x"]

    def inwin(e: str) -> bool:
        return start <= _settle(e) < end
    rows, results, files, full, missing, not_listed, stale = [], [], [], [], [], [], []
    later_n = 0
    for d in wdays:
        if d > day:
            rows.append({"day": d, "future": True})
            continue
        due, later = expected_events(d, now, until=until, rec_start=rec_start)
        later_set = {e for e in later if inwin(e)}
        evs = {str(ev.get("event")): ev for ev in (win.days_events.get(d) or [])
               if inwin(str(ev.get("event")))}
        res_d, miss_d, nl_d, late_d = [], [], [], 0
        for e in sorted({x for x in due if inwin(x)} | later_set | set(evs), key=_settle):
            ev = evs.get(e)
            r = (ev.get("variants") or {}).get(key) if ev else None
            if isinstance(r, dict):
                fv = None if ev.get("sim_version") is None else str(ev.get("sim_version"))
                if fv != pin_v and fv != cur_version:
                    # an older file awaiting its re-sim: its rule and tables
                    # are still checked, its numbers do not count
                    files.append(ev)
                    full.append(False)
                    stale.append(e)
                    miss_d.append((e, f"older sim file (SIM_VERSION {fv}; the tests read "
                                      f"{pin_v}): re-sim the day"))
                    continue
                res_d.append((e, r))
                files.append(ev)
                full.append(True)
                continue
            if e in later_set:
                late_d += 1                   # not due yet: the test is still running
                continue
            if ev is None:
                if listed_by_kalshi(e, lst()) is False:
                    nl_d.append(e)
                    continue
                miss_d.append((e, _missing_reason(e, lst(), sim_summary)))
            elif ev.get("stress_error"):
                miss_d.append((e, "stress failed: "
                               + str(ev["stress_error"]).strip().splitlines()[0][:160]))
            else:
                miss_d.append((e, f"its sim file has no {DECISION_FILL} result for {v}"
                               + (f" (SIM_VERSION {ev.get('sim_version')})"
                                  if ev.get("sim_version") is not None else "")))
        later_n += late_d
        results += [r for _e, r in res_d]
        missing += miss_d
        not_listed += nl_d
        known_d = [(e, _num(r.get("net"))) for e, r in res_d if _settle(e) < reg_at]
        rows.append({"day": d, "future": False, "events": len(res_d),
                     "expected": len(res_d) + len(miss_d) + late_d, "missing": len(miss_d),
                     "not_listed": len(nl_d), "later": late_d,
                     "net": sum(_num(r.get("net")) for _e, r in res_d),
                     "reward": sum(_num(r.get("reward")) for _e, r in res_d),
                     "trading": sum(_num(r.get("trading_pnl")) for _e, r in res_d),
                     "known": len(known_d), "known_net": sum(n for _e, n in known_d),
                     "hours": [(e, _num(r.get("net"))) for e, r in res_d]})
    done = [r for r in rows if not r["future"]]
    hours = [(e, n) for r in done for e, n in r["hours"]]
    nets = [n for _e, n in hours]
    total = sum(r["net"] for r in done)
    pos = sum(1 for r in done if r["net"] > 0)
    robust = robust_total(nets)
    cal = (sum(_num(r.get("net_cal")) for r in results)
           if results and all(r.get("net_cal") is not None for r in results) else None)
    rec = recent_cal(results, rates) if (results and rates) else None
    left = len(wdays) - len(done)
    crit = {
        "total": {"label": "the window's total stress net > 0", "value": total,
                  "met": total > 0},
        "days": {"label": f"at least {PREREG_DAYS_POS} of the {len(wdays)} days have stress "
                          "net > 0", "value": pos, "met": pos >= PREREG_DAYS_POS,
                 "possible": pos + left >= PREREG_DAYS_POS},
        "robust": {"label": "the total excluding the variant's best and worst hour > 0",
                   "value": robust, "met": robust is not None and robust > 0},
        "cal": {"label": "the calibrated-tails expectation > 0", "value": cal,
                "met": cal is not None and cal > 0},
    }
    bad = pin_check(spec, files, full)
    last_settle = max([s for _e, s in _day_tickers(wdays[-1])
                       if s is not None and start <= s < end and s < until] or [start])
    over = day >= wdays[-1] and last_settle + SETTLE_LAG_S <= now
    if day < wdays[0]:
        status, text = "not started", f"starts {_mmdd(wdays[0])}"
    elif bad:
        status, text = "INVALID", "INVALID (rule or model changed)"
    elif not over or later_n:
        status = "in progress"
        text = (f"in progress ({len(done)}/{len(wdays)} days"
                + (f"; {len(missing)} expected hour(s) without a {DECISION_FILL} result so far"
                   if missing else "") + ")")
    elif missing:
        status = "INCOMPLETE"
        text = (f"INCOMPLETE ({len(missing)} expected hour(s) without a {DECISION_FILL} "
                "result)")
    else:
        ok = all(crit[c]["met"] for c in spec["criteria"])
        status = text = "PASS" if ok else "FAIL"
    out = dict(spec, key=key, rows=rows, status=status, text=text, total=total,
               pos_days=pos, days_done=len(done), n_days=len(wdays), robust=robust, cal=cal,
               recent=rec, crit=crit, hours=len(nets), hour_nets=hours, missing=missing,
               not_listed=not_listed, stale=stale, pin_diffs=bad, files=len(files),
               first_day=wdays[0], last_day=wdays[-1], days_list=wdays, start_ts=start,
               end_ts=end, over=over, best=max(nets) if nets else None,
               worst=min(nets) if nets else None,
               known=[(e, n) for e, n in hours if _settle(e) < reg_at],
               known_n=sum(1 for d in wdays for _e, st in _day_tickers(d)
                           if st is not None and start <= st < min(reg_at, end)),
               alt=None,
               fav_gate=None, frozen=None, frozen_other=None, frozen_diffs=[])
    gates = [(ev.get("fav_gate") or {}).get(v) for ev, f in zip(files, full) if f]
    gates = [g for g in gates if isinstance(g, dict)]
    if gates:
        out["fav_gate"] = {"model_secs": sum(int(_num(g.get("model_secs"))) for g in gates),
                           "gate_secs": sum(int(_num(g.get("gate_secs"))) for g in gates),
                           "files": len(gates)}
    if start_ts is None and start < reg_at and day >= wdays[0]:
        # the hours that settled before the registration commit were known
        # when the test was written: the same test without them, beside it
        out["alt"] = prereg_test(spec, win, day, rates, now, sim_summary, rec_start,
                                 listing=lst, start_ts=reg_at, cur_version=cur_version)
    return out


def prereg_verdict(tests: Sequence[dict], alt: bool = False) -> str:
    """'pilot eligible: <variant>' only when a test PASSes; 'NO-GO (no
    variant passes the pre-registered tests)' when every test FAILed; else
    'in progress' while a test runs, or 'undecided' -- with the INCOMPLETE /
    INVALID tests named. alt: each test's "after registration" view where it
    has one (Test A as registered)."""
    sts = [(t.get("alt") or t) if alt else t for t in tests]
    passed = [t["variant"] for t, s in zip(tests, sts) if s["status"] == "PASS"]
    if passed:
        return "pilot eligible: " + ", ".join(passed)
    if sts and all(s["status"] == "FAIL" for s in sts):
        return "NO-GO (no variant passes the pre-registered tests)"
    odd = [f"Test {t['id']} {s['status']}" for t, s in zip(tests, sts)
           if s["status"] in ("INCOMPLETE", "INVALID")]
    running = any(s["status"] in ("in progress", "not started") for s in sts)
    return ("in progress" if running else "undecided") + (f" ({'; '.join(odd)})" if odd else "")


def test_subject(t: dict) -> str:
    """A test's part of the email subject (with Test A's "after
    registration" figure while the registered window holds known hours)."""
    a = t.get("alt")
    aft = ""
    if a:
        st = a["status"] if a["status"] in ("PASS", "FAIL", "INVALID", "INCOMPLETE") else ""
        aft = f"; after registration {st + ' ' if st else ''}{money(a['total'], 0)}"
    if t["status"] == "not started":
        return f"Test {t['id']} {t['text']}"
    if t["status"] == "INVALID":
        return f"Test {t['id']} INVALID (rule or model changed)"
    nm = len(t.get("missing") or [])
    hm = f"{nm} hour{'' if nm == 1 else 's'} missing"
    if t["status"] == "INCOMPLETE":
        return f"Test {t['id']} INCOMPLETE ({hm}; stress {money(t['total'], 0)}{aft})"
    if t["status"] == "in progress":
        return (f"Test {t['id']} in progress {t['days_done']}/{t['n_days']} (stress "
                f"{money(t['total'], 0)}{aft}" + (f"; {hm}" if nm else "") + ")")
    return (f"Test {t['id']} {t['status']} (stress {money(t['total'], 0)}{aft})"
            + (" [frozen verdict; a recompute differs]" if t.get("frozen_diffs") else ""))


# --------------------------------------------------------------------------
# Frozen verdicts (<data root>/prereg/<test>.json)
# --------------------------------------------------------------------------


def test_definition(spec: dict) -> dict:
    """What a frozen verdict was computed under (a frozen file of another
    definition is never used): the window, the criteria, the registration
    time and the pins."""
    return json.loads(json.dumps({
        "id": spec["id"], "variant": spec["variant"], "start": spec["start"],
        "days": spec["days"], "criteria": list(spec["criteria"]),
        "registered_at": PREREG_REGISTERED_AT, "overrides": spec["overrides"],
        "policy": spec["policy"], "model": _pin_model(spec)}, default=str))


def frozen_path(tid: str) -> str:
    return os.path.join(C.DATA_ROOT, "prereg", f"{tid}.json")


def _verdict_summary(t: dict) -> dict:
    def r4(x):
        return None if x is None else round(float(x), 4)
    return {"status": t["status"], "total": r4(t["total"]), "pos_days": t["pos_days"],
            "robust": r4(t["robust"]), "cal": r4(t["cal"]), "recent": r4(t.get("recent")),
            "days": {r["day"]: r4(r["net"]) for r in t["rows"] if not r["future"]},
            "hours": [[e, r4(n)] for e, n in t["hour_nets"]],
            "not_listed": list(t.get("not_listed") or []), "files": t.get("files")}


def freeze_doc(t: dict, day: str, now: float) -> dict:
    return {"definition": test_definition(t), "frozen_at": C.iso(now), "report_day": day,
            "verdict": _verdict_summary(t),
            "after_registration": _verdict_summary(t["alt"]) if t.get("alt") else None}


_CMP_KEYS = ("status", "total", "pos_days", "robust", "cal", "days", "hours")


def apply_frozen(t: dict, day: str, now: float, freeze: bool = False) -> None:
    """A completed test's verdict is frozen once: freeze=True (the report's
    main run) writes <data root>/prereg/<test>.json the first time the window
    completes with PASS or FAIL (Test A: both views). Later runs show the
    frozen verdict and name what a recompute changes ("frozen_diffs";
    "recomputed" holds the recompute). A frozen file of another definition
    is named ("frozen_other") and not used. A report for a day before the
    window's last day (a re-run of an older report) shows the computation
    as of that day, never the frozen file."""
    if day < t["last_day"]:
        return
    path = frozen_path(t["id"])
    doc = C.read_json(path) if os.path.exists(path) else None
    if isinstance(doc, dict):
        if doc.get("definition") != test_definition(t):
            t["frozen_other"] = {"path": path, "frozen_at": doc.get("frozen_at"),
                                 "status": (doc.get("verdict") or {}).get("status")}
            return
        t["frozen"] = {"path": path, "frozen_at": doc.get("frozen_at"),
                       "report_day": doc.get("report_day")}
        views = [(t, doc.get("verdict") or {})]
        if t.get("alt") and isinstance(doc.get("after_registration"), dict):
            views.append((t["alt"], doc["after_registration"]))
        for view, was in views:
            now_v = _verdict_summary(view)
            view["recomputed"] = {"status": view["status"], "text": view["text"],
                                  "total": view["total"]}
            view["frozen_diffs"] = [k for k in _CMP_KEYS if was.get(k) != now_v.get(k)]
            if was.get("status"):
                view["status"] = was["status"]
                view["total"] = _num(was.get("total"))
                view["text"] = f"{was['status']} (frozen {str(doc.get('frozen_at') or '?')[:10]})"
        t["frozen_diffs"] = sorted(set(t["frozen_diffs"])
                                   | set((t.get("alt") or {}).get("frozen_diffs") or []))
        return
    if freeze and t["status"] in ("PASS", "FAIL") and (
            not t.get("alt") or t["alt"]["status"] in ("PASS", "FAIL")):
        C.write_json_atomic(path, freeze_doc(t, day, now))
        t["frozen"] = {"path": path, "frozen_at": C.iso(now), "report_day": day, "new": True}


# --------------------------------------------------------------------------
# Week summary (day 7 on): the adopted go-live rules, not GO / NO-GO bars
# --------------------------------------------------------------------------


def pilot_policy(params: dict) -> dict:
    """The rule-4 pilot's policy: the tested variant at 1/3 size (q_fav,
    q_pad and q_live / 3, whole contracts; caps unchanged)."""
    out = dict(params)
    for k in ("q_fav", "q_pad", "q_live"):
        x = _opt_num(out.get(k))
        if x:
            out[k] = int(round(x / 3.0))
    return out


def week_summary(win: Window, policy: Policy, model: dict,
                 today_health: Optional[dict] = None,
                 tests: Optional[Sequence[dict]] = None,
                 rank: Optional[Sequence[dict]] = None) -> Optional[dict]:
    """The week verdict from the adopted go-live rules (Jack 2026-10-17,
    "Yes. And Assume rewards are paid"): the pre-registered tests decide --
    "pilot eligible: <variant>" only when a test PASSes (then the rule-4
    pilot config at 1/3 size: pilot_config), "NO-GO (no variant passes the
    pre-registered tests)" when every test has failed, else "in progress" /
    "undecided". Test A's "after registration" view is beside it. The old GO
    / NO-GO / EXTEND thresholds on the best variant's realized net are
    gone."""
    tests = list(tests or [])
    verdict = prereg_verdict(tests)
    alt_v = prereg_verdict(tests, alt=True) if any(t.get("alt") for t in tests) else None
    eligible = [t["variant"] for t in tests if t["status"] == "PASS"]
    reason = "; ".join(f"Test {t['id']} ({t['variant']}, {_mmdd(t['first_day'])}-"
                       f"{_mmdd(t['last_day'])}): {t['text']}, stress {money(t['total'])}"
                       + (f" (after registration: {t['alt']['text']}, "
                          f"{money(t['alt']['total'])})" if t.get("alt") else "")
                       for t in tests) or "no pre-registered test"
    best = eligible[0] if eligible else None
    focus = best or next((t["variant"] for t in tests), None)
    risks = []
    if focus:
        parts = []
        for f in (DECISION_FILL, "swp", "cons", "opt"):
            k = f"{focus}|{f}"
            if k in win.agg:
                parts.append(f"{f} {money(win.total(k))}")
        if parts:
            risks.append(f"Fill model ({focus}, the cumulative window): " + ", ".join(parts)
                         + ". stress adds level-clears at every price and hidden favorite "
                           "takers; on the live hour 26OCT1610 a better external bid appeared "
                           "within 2 s after 36% of our favorite joins (11% after other "
                           "makers'), which no fill model replays: live favorite joins would "
                           "do worse than the shadow.")
        tt = [t for t in tests if t["variant"] == focus]
        if tt and tt[0].get("cal") is not None:
            risks.append(f"Tails ({focus}, its test window): the table-based expectation "
                         f"{money(tt[0]['cal'])} comes from a calmer period; re-marked at the "
                         "recorded period's upset rates after favorite-bid hits it is "
                         + (money(tt[0]["recent"]) if tt[0].get("recent") is not None
                            else "n/a") + ".")
    if model.get("n"):
        risks.append(f"Model: Brier {model['brier_model']:.4f} vs book mid "
                     f"{model['brier_mid']:.4f} over {model['n']:.0f} strike-minutes"
                     + (" -- the guard is worse than the market" if
                        model["brier_model"] > model["brier_mid"] + MODEL_MARGIN else "") + ".")
    else:
        risks.append("Model: no model evaluation in the window.")
    short_days = [f"{d} ({len(win.days_events[d])} events)" for d in win.days
                  if len(win.days_events[d]) < SHORT_DAY_EVENTS and d not in win.partial]
    cov = agg_coverage(win.all_events)
    risks.append("Data: book trusted " + pct(cov.get("book_trusted_frac"))
                 + (f"; short days: {', '.join(short_days)}" if short_days else "; no short days")
                 + (f"; {cov['unsettled']} unsettled event(s)" if cov["unsettled"] else "") + ".")
    if today_health and (today_health.get("ws") or {}).get("uptime") is not None:
        risks.append(f"Ops (report day): ws uptime {pct(today_health['ws']['uptime'])}, "
                     f"{(today_health.get('poll') or {}).get('n429', 0)} HTTP 429s.")
    # the pilot runs the rule that passed: the registered policy of the test
    # (the current grid's only for a variant no test registered)
    spec = next((t for t in tests if t["variant"] == best), None) if best else None
    cfg = (dict(spec["policy"]) if spec and spec.get("policy") else
           policy.params(best) if best else {})
    ov = (dict(spec["overrides"]) if spec and spec.get("overrides") else
          dict(policy.variants.get(best) or {}) if best else {})
    return {"verdict": verdict, "verdict_after_registration": alt_v, "eligible": eligible,
            "best": best, "reason": reason, "tests": tests,
            "pilot": PILOT_RULES if eligible else None, "config": cfg,
            "pilot_config": pilot_policy(cfg) if cfg else {}, "overrides": ov,
            "policy_err": policy.err, "risks": risks}


# --------------------------------------------------------------------------
# Collector health from the raw streams
# --------------------------------------------------------------------------

_RX_RE = re.compile(r'^\{\s*"rx"\s*:\s*(-?[0-9.]+(?:[eE][+-]?[0-9]+)?)')


def _raw_paths(stream: str, t0: float, t1: float) -> List[str]:
    """C.raw_files without its side effect: C.raw_file -> C.raw_dir creates
    raw/<UTC day>/ for every hour it probes, and a report must not write
    into the recording."""
    out: List[str] = []
    h = math.floor(t0 / 3600) * 3600
    while h < t1:
        t = _dt.datetime.fromtimestamp(h, _dt.timezone.utc)
        p = os.path.join(C.DATA_ROOT, "raw", t.strftime("%Y-%m-%d"),
                         f"{stream}_{t.strftime('%H')}.jsonl")
        if os.path.exists(p + ".gz"):
            out.append(p + ".gz")
        if os.path.exists(p):
            out.append(p)
        h += 3600
    return out


def _iter_raw(stream: str, t0: float, t1: float) -> Iterator[dict]:
    """C.iter_raw over _raw_paths (rows with t0 <= rx < t1)."""
    for p in _raw_paths(stream, t0, t1):
        for rec in C.iter_jsonl(p):
            rx = rec.get("rx")
            if rx is None or rx < t0 or rx >= t1:
                continue
            yield rec


def _iter_lines(path: str) -> Iterator[str]:
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line
    except (EOFError, OSError, gzip.BadGzipFile):
        return


def ws_health(t0: float, t1: float,
              spans: Optional[Sequence[Tuple[float, float]]] = None) -> dict:
    """Markers and per-minute presence of ws rows, without json-parsing the
    (large) message rows: rx is read off the line's prefix.

    spans: when given, the [a, b) windows in which a KXTEMPMIAH market was
    subscribed (open - 10 min .. close + 10 min). Minutes outside every span
    are neither uptime nor downtime: with no market listed (Kalshi skipped
    04:00/05:00 ET on 10/15) a healthy connection carries no rows, and those
    hours read as a 9% "outage" (2026-10-15)."""
    minutes = set()
    markers: List[Tuple[float, str, str]] = []
    n = 0
    for p in _raw_paths("ws", t0, t1):
        for line in _iter_lines(p):
            if '"marker"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and "marker" in rec:
                    rx = _opt_num(rec.get("rx"))
                    if rx is not None and t0 <= rx < t1:
                        markers.append((rx, str(rec.get("marker")), str(rec.get("detail") or "")))
                    continue
            m = _RX_RE.match(line)
            if m:
                rx = float(m.group(1))
            else:
                try:
                    rx = _num(json.loads(line).get("rx"), -1)
                except (ValueError, AttributeError):
                    continue
            if t0 <= rx < t1:
                n += 1
                minutes.add(int((rx - t0) // 60))
    markers.sort()
    n_min = max(int((t1 - t0) // 60), 0)
    # Before the day's first marker the data decide: a hard-killed collector
    # writes no "disconnect", so a lone "connect" (its relaunch) says nothing
    # about the minutes before it; a WS that really was down then has no rows
    # there, and the data test below counts those minutes down anyway.
    state = True
    up, i, counted, no_market = 0, 0, 0, 0
    for kmin in range(n_min):
        mid = t0 + 60 * kmin + 30
        while i < len(markers) and markers[i][0] <= mid:
            if markers[i][1] == "connect":
                state = True
            elif markers[i][1] == "disconnect":
                state = False
            i += 1
        if spans is not None and not any(a <= mid < b for a, b in spans):
            no_market += 1
            continue
        counted += 1
        # quiet books can skip a minute: data within +/- 1 min counts
        if state and (kmin in minutes or kmin - 1 in minutes or kmin + 1 in minutes):
            up += 1
    cnt = lambda kind: sum(1 for m in markers if m[1] == kind)   # noqa: E731
    disc = [m[2] for m in markers if m[1] == "disconnect" and m[2]]
    return {"rows": n, "minutes": counted, "data_minutes": len(minutes),
            "minutes_no_market": no_market,
            "uptime": (up / counted) if counted else None,
            "connects": cnt("connect"), "disconnects": cnt("disconnect"),
            "gaps": cnt("gap"), "resubscribes": cnt("resubscribe"),
            "disconnect_reasons": disc[:5]}


def poll_health(t0: float, t1: float, n_min: int) -> Tuple[dict, dict]:
    """(poll stats, meta summary) from the meta stream."""
    rows = n = n429 = nerr = signed = 0
    poll_minutes = set()
    p50s: List[float] = []
    lmax = 0.0
    errors: Dict[str, int] = {}
    events_seen = set()
    for rec in _iter_raw("meta", t0 - 7200, t1):
        kind = rec.get("kind")
        data = rec.get("data") or {}
        rx = _num(rec.get("rx"))
        if kind == "markets":
            ev = data.get("event_ticker")
            if ev:
                events_seen.add(ev)
            continue
        if rx < t0:
            continue
        if kind == "poll":
            rows += 1
            if rx < t1:
                # a restart flushes an extra row: coverage counts minutes
                poll_minutes.add(int((rx - t0) // 60))
            n += int(_num(data.get("n")))
            n429 += int(_num(data.get("n429")))
            nerr += int(_num(data.get("nerr")))
            if data.get("signed"):
                signed += 1
            if _opt_num(data.get("lat_ms_p50")) is not None:
                p50s.append(_num(data.get("lat_ms_p50")))
            lmax = max(lmax, _num(data.get("lat_ms_max")))
        elif kind == "error":
            w = str(data.get("where") or "?")
            errors[w] = errors.get(w, 0) + 1
    poll = {"rows": rows, "minutes": len(poll_minutes), "expected": n_min,
            "coverage": min(len(poll_minutes) / n_min, 1.0) if n_min else None,
            "n": n, "n429": n429, "nerr": nerr, "signed_minutes": signed,
            "lat_p50_median": statistics.median(p50s) if p50s else None,
            "lat_max": lmax if rows else None}
    meta = {"errors": errors, "events_seen": events_seen}
    return poll, meta


_N_STATIONS = len(C.STATIONS.get("miami", ())) or 5


def _complete(row: dict) -> bool:
    """Every station's reading received. NOT "no pending codes": the live feed
    keeps a received reading at code "pending" until the official v lands
    (~310 s), so that test would measure the publication delay instead."""
    st = row.get("stations") or []
    return sum(1 for s in st if s.get("temp_f") is not None
               and s.get("code") != "missing") >= _N_STATIONS


def _final(row: dict) -> bool:
    return row.get("v") is not None and row.get("status") != "incomplete"


def wx_health(t0: float, t1: float, expect_from: Optional[float] = None
              ) -> Tuple[dict, Dict[int, List[dict]]]:
    """Minute coverage and our observation lag from the wx stream; also
    returns the rows grouped by minute (for the replica check). Minutes are
    kept from t0; minutes_expected counts from expect_from (the recording's
    start on day 0) when given."""
    by_t: Dict[int, List[dict]] = {}
    for rec in _iter_raw("wx", t0, t1 + 900):
        t = rec.get("t")
        if not isinstance(t, (int, float)) or not (t0 * 1000 <= t < t1 * 1000):
            continue
        by_t.setdefault(int(t), []).append(rec)
    lags, off_lags, st_lags, land_sec, land_all = [], [], [], [], []
    finalized = 0
    for t, rows in by_t.items():
        rows.sort(key=lambda r: _num(r.get("rx")))
        first_seen: Dict[str, Tuple[float, float]] = {}
        for r in rows:
            for s in r.get("stations") or []:
                sid, ra = s.get("station_id"), _opt_num(s.get("received_at_ms"))
                if sid and ra is not None and s.get("temp_f") is not None and sid not in first_seen:
                    first_seen[sid] = (_num(r.get("rx")) - ra / 1000.0, ra / 1000.0)
        for lag_s, ra_s in first_seen.values():
            if lag_s < 60:            # not a reading first seen at a collector restart
                st_lags.append(lag_s)
                land_all.append(ra_s % 60.0)
        seen_incomplete = False
        for r in rows:
            if not _complete(r):
                seen_incomplete = True
                continue
            # only a minute we watched turn complete measures our lag (one
            # already complete in our first look -- e.g. a collector restart --
            # says nothing about how fast we see a station land)
            if seen_incomplete:
                recv = [_num(s.get("received_at_ms")) for s in r.get("stations") or []
                        if _opt_num(s.get("received_at_ms")) is not None]
                if recv:
                    lags.append(_num(r.get("rx")) - max(recv) / 1000.0)
                    land_sec.append((max(recv) / 1000.0) % 60.0)
            break
        fin = next((r for r in rows if _final(r)), None)
        if fin is not None:
            finalized += 1
            off_lags.append(_num(fin.get("rx")) - t / 1000.0)
    n_min = max(int((t1 - max(t0, expect_from or t0)) // 60), 0)
    return ({"rows": sum(len(v) for v in by_t.values()), "minutes_seen": len(by_t),
             "minutes_finalized": finalized, "minutes_expected": n_min,
             "obs_lag_n": len(lags), "obs_lag_median": _quantile(lags, 0.5),
             "obs_lag_p90": _quantile(lags, 0.9),
             "station_lag_median": _quantile(st_lags, 0.5),
             "station_lag_p90": _quantile(st_lags, 0.9),
             "land_sec_p10": _quantile(land_sec, 0.1), "land_sec_p90": _quantile(land_sec, 0.9),
             "land_all": land_all,
             "official_lag_median": _quantile(off_lags, 0.5)}, by_t)


_DEFAULT_OFFSETS_C = {"KMIA1M": 0.0, "KOPF1M": -0.5, "KFLL1M": 0.0, "KFXE1M": 0.0,
                      "KPMP1M": 0.0}
_DEFAULT_B_CITY_C = -0.10


def _load_calibrations() -> List[dict]:
    """Kalshi calibration history if any shadow file holds it (newest file
    wins): [{"effective_at_ms", "city_reference_c", "stations": [...]}]."""
    pats = [os.path.join(C.DATA_ROOT, "calibrations*.json"),
            os.path.join(C.DATA_ROOT, "model", "calibrations*.json"),
            os.path.join(C.DATA_ROOT, "backfill", "**", "calibrations*.json")]
    files = []
    for p in pats:
        files.extend(glob.glob(p, recursive=True))
    for f in sorted(set(files), key=lambda x: os.path.getmtime(x), reverse=True):
        d = C.read_json(f)
        cals = d.get("calibrations") if isinstance(d, dict) else None
        if isinstance(cals, list) and cals:
            return sorted((c for c in cals if isinstance(c, dict)),
                          key=lambda c: _num(c.get("effective_at_ms")))
    return []


def _offsets_at(cals: List[dict], t_ms: int) -> Tuple[Dict[str, float], float]:
    cur = None
    for c in cals:
        if _num(c.get("effective_at_ms")) <= t_ms:
            cur = c
    if not cur:
        return dict(_DEFAULT_OFFSETS_C), _DEFAULT_B_CITY_C
    offs = {s.get("station_id"): _num(s.get("offset_c")) for s in cur.get("stations") or []
            if s.get("station_id")}
    return offs, _num(cur.get("city_reference_c"), _DEFAULT_B_CITY_C)


def replica_check(by_t: Dict[int, List[dict]]) -> dict:
    """Exact-match rate of temp_shadow_model.replica_index vs the official v
    on the day's finalized minutes. Degrades to {"error": ...}."""
    try:
        import temp_shadow_model as model   # noqa: WPS433 (lazy on purpose)
        fn = model.replica_index
    except BaseException as e:              # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return {"error": f"temp_shadow_model unavailable ({type(e).__name__}: {e})"}
    mcals = None
    if hasattr(model, "Calibrations"):
        try:
            mcals = model.Calibrations.load()
        except Exception:  # noqa: BLE001
            mcals = None
    cals = [] if mcals is not None else _load_calibrations()
    n = exact = near = none = 0
    bad: List[Tuple[int, float, Optional[float], int]] = []
    for t in sorted(by_t):
        fin = None
        for r in by_t[t]:
            if _final(r):
                fin = r          # the latest finalized row (rows are rx-sorted)
        if fin is None:
            continue
        st = {s.get("station_id"): float(s["temp_f"]) for s in fin.get("stations") or []
              if s.get("temp_f") is not None and s.get("code") in (None, "", "ok")}
        try:
            if mcals is not None:
                cal = mcals.for_minute(t)
                try:
                    rep = fn(st, cal.offsets, cal.b_city_c, cal.weights)
                except TypeError:          # the contract's 3-argument form
                    rep = fn(st, cal.offsets, cal.b_city_c)
            else:
                offs, b = _offsets_at(cals, t)
                rep = fn(st, offs, b)
        except Exception as e:  # noqa: BLE001
            return {"error": f"replica_index raised {type(e).__name__}: {e}"}
        v = _num(fin.get("v"))
        n += 1
        if rep is None:
            none += 1
            ok = False
        else:
            ok = abs(round(rep, 2) - round(v, 2)) < 0.005
            near += abs(rep - v) <= 0.05
        exact += ok
        if not ok and len(bad) < 6:
            bad.append((t, v, rep, len(st)))
    src = (getattr(mcals, "source", "model") if mcals is not None
           else (f"{len(cals)} records" if cals else "built-in defaults"))
    return {"n": n, "exact": exact, "near": near, "none": none,
            "rate": (exact / n) if n else None, "calibrations": src,
            "mismatches": bad}


_RAW_DAY_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
_RAW_FILE_RE = re.compile(r"^(ws|wx|meta)_(\d\d)\.jsonl(\.gz)?$")


def recording_start() -> Optional[float]:
    """rx of the first row the collector ever wrote (raw/, earliest UTC hour);
    None when there is no recording. SHADOW_START_UTC is a planning value
    (10/14 00:00Z) -- the recording really began 10/15 00:39Z."""
    root = os.path.join(C.DATA_ROOT, "raw")
    try:
        days = sorted(d for d in os.listdir(root) if _RAW_DAY_RE.match(d))
    except OSError:
        return None
    for d in days:
        try:
            names = os.listdir(os.path.join(root, d))
        except OSError:
            continue
        by_hour: Dict[str, List[str]] = {}
        for f in names:
            m = _RAW_FILE_RE.match(f)
            if m:
                by_hour.setdefault(m.group(2), []).append(f)
        for hh in sorted(by_hour):
            first = None
            for f in by_hour[hh]:
                for rec in C.iter_jsonl(os.path.join(root, d, f)):
                    rx = _opt_num(rec.get("rx")) if isinstance(rec, dict) else None
                    if rx is not None:
                        first = rx if first is None else min(first, rx)
                        break
            if first is not None:
                return first
    return None


def expected_events(day: str, now: float, until: Optional[float] = None,
                    rec_start: Optional[float] = None) -> Tuple[List[str], List[str]]:
    """(due, not yet due) event tickers of the ET day, one per ET hour; due =
    its settlement window (settle + 2 h) has passed. Events outside the
    shadow are in neither list: settling at or after `until` (SHADOW_UNTIL:
    the collector is gone before their settlement pull) or at or before
    `rec_start` (closed before the recording began)."""
    due, later = [], []
    for ev, s in _day_tickers(day):
        if s is not None and ((until is not None and s >= until)
                              or (rec_start is not None and s <= rec_start)):
            continue
        (due if s is not None and s + SETTLE_LAG_S <= now else later).append(ev)
    return due, later


def market_spans(sim_events: Sequence[dict], seen: Iterable[str] = ()
                 ) -> Optional[List[Tuple[float, float]]]:
    """[open - 10 min, close + 10 min) for every event with markets: the simmed
    ones (their own open/close) and any other the collector saw listed (an
    hour before its settlement to 10 min after). None when nothing is known,
    which keeps ws_health's old every-minute denominator."""
    spans: List[Tuple[float, float]] = []
    have = set()
    for ev in sim_events or []:
        a, b = _opt_num(ev.get("open_ts")), _opt_num(ev.get("close_ts"))
        if a is not None and b is not None:
            spans.append((a - 600.0, b + 600.0))
            have.add(ev.get("event"))
    for ev in seen or ():
        if ev in have:
            continue
        pe = C.parse_event(str(ev))
        if pe:
            spans.append((pe[1] - 3600.0 - 600.0, pe[1] + 600.0))
    return spans or None


def collector_health(day: str, sim_events: Sequence[dict], now: Optional[float] = None,
                     sim_summary: Optional[dict] = None) -> dict:
    now = C.utcnow() if now is None else now
    t0, t1 = C.et_day_bounds(day)
    until = shadow_until()
    t1e = min(t1, now, until)
    t1e = max(t1e, t0)
    # health starts at the recording's first minute when that is inside the
    # day (day 0 began 20:39 ET): minutes before the collector existed are
    # neither uptime nor downtime
    rec_start = recording_start()
    t0h = t0
    if rec_start is not None:
        t0h = min(max(t0, math.floor(rec_start / 60.0) * 60.0), t1e)
    out: Dict[str, Any] = {"t0": t0h, "t1": t1e, "day_t0": t0, "day_t1": t1,
                           "partial": t1e < t1 or t0h > t0,
                           "started_late": t0h > t0, "ended_early": t1e < t1,
                           "shadow_ended": until < t1 and t1e >= until,
                           "recording_start": rec_start}
    n_min = max(int((t1e - t0h) // 60), 0)
    try:
        out["poll"], meta = poll_health(t0h, t1e, n_min)
    except Exception as e:  # noqa: BLE001
        out["poll"], meta = {"error": f"{type(e).__name__}: {e}"}, {"errors": {}, "events_seen": set()}
    try:
        out["ws"] = ws_health(t0h, t1e, spans=market_spans(sim_events,
                                                           meta.get("events_seen", set())))
    except Exception as e:  # noqa: BLE001
        out["ws"] = {"error": f"{type(e).__name__}: {e}"}
    out["errors"] = meta.get("errors", {})
    try:
        out["wx"], by_t = wx_health(t0, t1e, expect_from=t0h)
    except Exception as e:  # noqa: BLE001
        out["wx"], by_t = {"error": f"{type(e).__name__}: {e}"}, {}
    out["replica"] = replica_check(by_t) if by_t else {"n": 0, "rate": None}
    out["collector_cfg"] = collector_settings()
    status = C.read_json(C.STATUS_FILE)
    out["status"] = status if isinstance(status, dict) else None
    due, later = expected_events(day, now, until=until, rec_start=rec_start)
    have = {ev.get("event") for ev in sim_events}
    seen = meta.get("events_seen", set())
    sim_err = dict((sim_summary or {}).get("errors") or [])
    sim_pend = set((sim_summary or {}).get("pending") or [])
    missing = []
    for ev in due:
        if ev in have:
            continue
        settled = os.path.exists(os.path.join(C.DATA_ROOT, "settled", f"{ev}.json"))
        if ev in sim_err:
            kind, why, fix = "sim_error", f"the sim raised: {sim_err[ev]}", False
        elif ev in sim_pend:
            kind, why, fix = ("pending", "the sim found it unsettled (settlement pull incomplete)",
                              False)
        elif settled:
            kind, why, fix = "fixable", "settled file present but no sim output", True
        elif ev in seen:
            kind, why, fix = "no_settled", "seen by the collector but no settled file", False
        else:
            kind, why, fix = ("never_seen", "never seen by the collector (not listed by Kalshi, "
                              "or collector down)", False)
        missing.append({"event": ev, "why": why, "fixable": fix, "kind": kind})
    out["events"] = {"due": len(due), "not_due": len(later), "simmed": len(have),
                     "outside": 24 - len(due) - len(later), "missing": missing}
    return out


# --------------------------------------------------------------------------
# Context assembly
# --------------------------------------------------------------------------


def _tally(res, into: dict) -> None:
    """Add run_day's per-event results to {"ok", "pending", "errors"}."""
    for r in res or []:
        if not isinstance(r, dict):
            continue
        if r.get("error"):
            into["errors"].append((str(r.get("event")), str(r.get("error"))[:300]))
        elif r.get("pending"):
            into["pending"].append(str(r.get("event")))
        elif r.get("deferred"):
            into.setdefault("deferred", []).append(str(r.get("event")))
        else:
            into["ok"] += 1
            if r.get("swp_error"):
                into.setdefault("swp_errors", []).append(
                    (str(r.get("event")), str(r.get("swp_error")).splitlines()[0][:300]))
            if r.get("stress_error"):
                into.setdefault("stress_errors", []).append(
                    (str(r.get("event")), str(r.get("stress_error")).splitlines()[0][:300]))


def run_sim(day: str, budget_min: Optional[float] = None) -> Tuple[Optional[str], dict]:
    """temp_shadow_sim.run_day(day), then run_day for every earlier day of the
    cumulative window (window_days) -> (error text or None, summary).

    The summary is the report day's per-event results, {"ok", "pending":
    [events], "errors": [(event, err)]}, plus "window": the same tally over
    the earlier window days ({"days", "ok", "pending", "errors"}; a run_day
    that raises adds (day, error)). run_day only loads a cached file that
    passes its own check (settled, this SIM_VERSION, every variant with the
    same overrides), so a SIM_VERSION bump or a VARIANTS change re-sims the
    whole window here instead of mixing old and new output in the cumulative
    figures (review fix 10/16: 10/15 stayed at SIM_VERSION 4, where
    G_decided_only had no pad hold and G0_nohold did not exist).

    Time budget (fix pass 10/16): the earlier days run with run_day's
    deadline = start + budget_min (RESIM_BUDGET_MIN); events not started by
    then come back deferred (window "deferred": [event]) and keep their
    older files (flagged stale); the next report continues. The report day
    itself has no deadline. The earlier days go newest first (10/17: the
    pre-registered test windows are the latest days; a deferral falls on
    the oldest). "swp_config_error": swp_config() raised (an unusable repo
    table): every event is simmed without swp; "stress_config_error" the
    same for stress."""
    summary: dict = {"ok": 0, "pending": [], "errors": [], "deferred": [],
                     "swp_config_error": None, "stress_config_error": None,
                     "window": {"days": [], "ok": 0, "pending": [], "errors": [],
                                "deferred": [], "budget_min": None}}
    t0 = time.time()
    budget_min = RESIM_BUDGET_MIN if budget_min is None else float(budget_min)
    summary["window"]["budget_min"] = budget_min
    try:
        import temp_shadow_sim as sim   # noqa: WPS433
        swp_cfg = None
        if "swp" in getattr(sim, "FILL_MODELS", ()):
            try:
                swp_cfg = sim.swp_config()
            except Exception as e:      # noqa: BLE001 -- the sim records it per event
                summary["swp_config_error"] = f"{type(e).__name__}: {e}"[:400]
        if hasattr(sim, "stress_config"):
            try:
                sim.stress_config(swp_cfg)
            except Exception as e:      # noqa: BLE001 -- the sim records it per event
                summary["stress_config_error"] = f"{type(e).__name__}: {e}"[:400]
        res = sim.run_day(day)
    except BaseException as e:          # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return ("".join(traceback.format_exception(type(e), e, e.__traceback__))[-3000:],
                summary)
    _tally(res, summary)
    win = summary["window"]
    deadline = t0 + 60.0 * budget_min
    for d in reversed(window_days(day)):        # newest first: the test windows
        if d == day:
            continue
        win["days"].append(d)
        try:
            res = sim.run_day(d, deadline=deadline)
        except BaseException as e:      # noqa: BLE001 -- the report day still reports
            if isinstance(e, KeyboardInterrupt):
                raise
            win["errors"].append((d, f"run_day raised {type(e).__name__}: {e}"[:300]))
            continue
        _tally(res, win)
    return None, summary


def current_sim_version() -> Optional[str]:
    """temp_shadow_sim.SIM_VERSION, or None when the sim is not importable."""
    try:
        import temp_shadow_sim as sim   # noqa: WPS433 (lazy on purpose)
    except BaseException as e:          # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return None
    v = getattr(sim, "SIM_VERSION", None)
    return None if v is None else str(v)


def stale_sim_days(days_events: Dict[str, List[dict]],
                   version: Optional[str]) -> Dict[str, int]:
    """day -> count of loaded sim files whose sim_version is not `version`
    (files without a sim_version are not judged)."""
    out: Dict[str, int] = {}
    if version is None:
        return out
    for d, evs in days_events.items():
        n = sum(1 for ev in evs
                if ev.get("sim_version") is not None and str(ev.get("sim_version")) != version)
        if n:
            out[d] = n
    return out


def _has_swp(ev: dict) -> bool:
    return any(split_key(k)[1] == "swp" for k in (ev.get("variants") or {}))


def swp_missing(days_events: Dict[str, List[dict]], day: str,
                version: Optional[str]) -> dict:
    """The loaded events without swp results (the headline is on swp):
    {"day": [(event, why)], "by_day": {day: n}, "total": n, "events": n}.
    why: the sim file's swp_error (swp failed; opt / cons kept), or an older
    SIM_VERSION, or no swp run."""
    out = {"day": [], "by_day": {}, "total": 0, "events": 0}
    for d, evs in days_events.items():
        for ev in evs:
            out["events"] += 1
            if _has_swp(ev):
                continue
            if ev.get("swp_error"):
                why = "swp failed: " + str(ev["swp_error"]).splitlines()[0][:200]
            elif version is not None and ev.get("sim_version") is not None \
                    and str(ev.get("sim_version")) != version:
                why = f"older sim file (SIM_VERSION {ev.get('sim_version')})"
            else:
                why = "no swp in the sim file"
            out["by_day"][d] = out["by_day"].get(d, 0) + 1
            out["total"] += 1
            if d == day:
                out["day"].append((str(ev.get("event")), why))
    return out


def _table_kind(src: Optional[str], kind: Optional[str]) -> str:
    if kind == "override" or (kind is None and src and os.sep + "model" + os.sep in src):
        return "model/ override"
    return "repo default"


def swp_tables(days_events: Dict[str, List[dict]]) -> List[dict]:
    """The swp hazard / tails tables that produced the loaded swp numbers:
    [{"hazard_id", "hazard", "tails_id", "tails", "draws", "events"}]
    (one row per distinct combination, most events first); "hazard" /
    "tails" = 'repo default' or 'model/ override'."""
    acc: Dict[tuple, dict] = {}
    for evs in days_events.values():
        for ev in evs:
            sw = ev.get("swp")
            if not isinstance(sw, dict) or not _has_swp(ev):
                continue
            k = (sw.get("hazard_id"), _table_kind(sw.get("hazard_src"), sw.get("hazard_kind")),
                 sw.get("tails_id"), _table_kind(sw.get("tails_src"), sw.get("tails_kind")),
                 sw.get("draws"))
            row = acc.setdefault(k, {"hazard_id": k[0], "hazard": k[1], "tails_id": k[2],
                                     "tails": k[3], "draws": k[4], "events": 0})
            row["events"] += 1
    return sorted(acc.values(), key=lambda r: -r["events"])


def current_swp_tables() -> Optional[dict]:
    """temp_shadow_sim.swp_config()'s identity now (which tables the next sim
    uses, and a rejected model/ override), {"error": text} when it raises,
    None when the sim is not importable."""
    try:
        import temp_shadow_sim as sim   # noqa: WPS433 (lazy on purpose)
    except BaseException as e:          # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return None
    if not hasattr(sim, "swp_config"):
        return None
    try:
        return dict(sim.swp_config()["ident"])
    except Exception as e:              # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:400]}


def swp_tables_text(ctx: dict) -> str:
    """One line naming the swp tables behind the figures (and the current
    ones when they differ)."""
    rows = ctx.get("swp_tables") or []
    if not rows:
        return ""
    def one(r):
        return (f"hazard {r['hazard_id']} ({r['hazard']}), tails {r['tails_id']} ({r['tails']}), "
                f"{r['draws']} draws")
    if len(rows) == 1:
        s = "swp tables: " + one(rows[0]) + "."
    else:
        s = ("swp tables differ across the window: "
             + "; ".join(f"{one(r)} on {r['events']} events" for r in rows) + ".")
    cur = ctx.get("swp_current") or {}
    if cur.get("hazard_id") and (cur.get("hazard_id"), cur.get("tails_id")) != (
            rows[0]["hazard_id"], rows[0]["tails_id"]):
        s += (f" The next sim uses hazard {cur['hazard_id']} "
              f"({_table_kind(cur.get('hazard_src'), cur.get('hazard_kind'))}), tails "
              f"{cur.get('tails_id')} ({_table_kind(cur.get('tails_src'), cur.get('tails_kind'))}).")
    return s


def swp_rejections(ctx: dict) -> List[str]:
    """A model/ override the sim rejected (it used the repo default), or
    swp_config() / stress_config() raising."""
    cur = ctx.get("swp_current") or {}
    out = [str(cur[k]) for k in ("hazard_rejected", "tails_rejected") if cur.get(k)]
    if cur.get("error"):
        out.append("swp_config() raised: " + str(cur["error"]))
    st = ctx.get("stress_current") or {}
    if st.get("fav_hazard_rejected"):
        out.append(str(st["fav_hazard_rejected"]))
    if st.get("error"):
        out.append("stress_config() raised: " + str(st["error"]))
    return out


def stress_missing(days_events: Dict[str, List[dict]], day: str,
                   version: Optional[str]) -> dict:
    """The loaded events without any stress result (the ranking and the
    tests are on stress): {"day": [(event, why)], "by_day": {day: n},
    "total": n, "events": n}. why: the sim file's stress_error (stress
    failed; opt / cons / swp kept), or an older SIM_VERSION, or no stress
    run."""
    out = {"day": [], "by_day": {}, "total": 0, "events": 0}
    for d, evs in days_events.items():
        for ev in evs:
            out["events"] += 1
            if any(split_key(k)[1] == DECISION_FILL for k in (ev.get("variants") or {})):
                continue
            if ev.get("stress_error"):
                why = "stress failed: " + str(ev["stress_error"]).splitlines()[0][:200]
            elif version is not None and ev.get("sim_version") is not None \
                    and str(ev.get("sim_version")) != version:
                why = f"older sim file (SIM_VERSION {ev.get('sim_version')})"
            else:
                why = "no stress in the sim file"
            out["by_day"][d] = out["by_day"].get(d, 0) + 1
            out["total"] += 1
            if d == day:
                out["day"].append((str(ev.get("event")), why))
    return out


def current_stress_tables(swp_current: Optional[dict] = None) -> Optional[dict]:
    """temp_shadow_sim.stress_config()'s identity now ({"error": text} when
    it raises; None when the sim has no stress)."""
    try:
        import temp_shadow_sim as sim   # noqa: WPS433 (lazy on purpose)
    except BaseException as e:          # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return None
    if not hasattr(sim, "stress_config"):
        return None
    try:
        return dict(sim.stress_config()["ident"])
    except Exception as e:              # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:400]}


def stress_text(ctx: dict) -> str:
    """One line naming the stress table and scope behind the figures."""
    rows: Dict[tuple, int] = {}
    scope_vars: Dict[str, set] = {}
    for evs in (ctx["win"].days_events or {}).values():
        for ev in evs:
            st = ev.get("stress")
            if not isinstance(st, dict):
                continue
            k = (st.get("fav_hazard_id"),
                 _table_kind(st.get("fav_hazard_src"), st.get("fav_hazard_kind")),
                 st.get("scope"), st.get("draws"))
            rows[k] = rows.get(k, 0) + 1
            scope_vars.setdefault(str(st.get("scope")), set()).update(st.get("variants") or [])
    if not rows:
        return ""
    parts = []
    for (fid, kind, scope, draws), n in sorted(rows.items(), key=lambda x: -x[1]):
        sc = ("every variant" if scope == "all" else
              "the variants that quote no live strikes (live=False)" if scope == "nolive"
              else f"scope {scope}")
        parts.append(f"favorite-taker table {fid} ({kind}), {draws} draws, stress for {sc}"
                     + (f" on {n} events" if len(rows) > 1 else ""))
    s = "stress: " + "; ".join(parts) + "."
    nolive = scope_vars.get("nolive")
    if nolive and len(rows) == 1:
        s += (" The live-quoting variants have opt / cons / swp only (stress there would add "
              "~40% to the sim's time and the decision rules use none of them; "
              "temp_shadow_sim.STRESS_SCOPE).")
    return s


def build_context(day: str, sim_error: Optional[str] = None, sim_ran: bool = False,
                  policy: Optional[Policy] = None, with_health: bool = True,
                  now: Optional[float] = None, sim_summary: Optional[dict] = None,
                  freeze: bool = False) -> dict:
    """Everything the report renders. freeze=True (main): a pre-registered
    test whose window completes with PASS / FAIL is frozen to <data root>/
    prereg/<test>.json (apply_frozen); later reports for the window's last
    day or after always show a frozen verdict."""
    now = C.utcnow() if now is None else now
    days = window_days(day)
    # events that closed before the recording began are outside the shadow
    # (expected_events' rule): the sim still writes them (their settled files
    # come from the collector's 26 h recovery pull) but with no book they
    # would only add zero hours to the per-event-hour figures (day 0: 21 of 24)
    rec_start = recording_start()
    days_events = {d: in_shadow(load_sim_day(d), rec_start) for d in days}
    if policy is None:
        policy = Policy(*load_policy_defs())
    # a variant the current grid no longer lists keeps the overrides its sim
    # output recorded ("policies")
    for evs in days_events.values():
        for ev in evs:
            for name, ov in (ev.get("policies") or {}).items():
                if name not in policy.variants and isinstance(ov, dict):
                    policy.variants[name] = dict(ov)
    win = Window(days_events)
    events = days_events.get(day, [])
    agg = agg_variants(events)
    market = agg_market(events)
    model = agg_model(events)
    cov = agg_coverage(events)
    health = collector_health(day, events, now, sim_summary) if with_health else None
    sugg, notes = suggest(win, day, policy, agg_market(win.all_events),
                          agg_model(win.all_events), health)
    if policy.err:
        notes.append(f"temp_shadow_sim not importable for variant params ({policy.err}).")
    # sim files older than the current SIM_VERSION (a --no-sim report, or a
    # window day whose re-sim failed): flagged at the top of the report
    sim_version = current_sim_version()
    stale = stale_sim_days(days_events, sim_version)
    ctx = {"day": day, "day_n": day_number(day), "label": day_label(day),
           "stale_sim": stale, "sim_version": sim_version,
           "generated": now, "sim_error": sim_error, "sim_ran": sim_ran,
           "events": events, "agg": agg, "market": market, "model": model,
           "coverage": cov, "health": health, "win": win, "policy": policy,
           "suggestions": sugg, "notes": notes, "sim_summary": sim_summary or {},
           "day0_events": 0}
    # the rules' fill model (swp, else cons on older output) and the
    # decision model (always stress: no fallback to the rules' model)
    src = win if win.by_key else agg
    ctx["rules_fill"] = rfill = headline_fill(src)
    ctx["fill"] = fill = decision_fill(src)
    # the headline: the robust stress ranking over the cumulative window --
    # never a single day's realized net
    ctx["rank"] = rank_variants(win, policy, fill) if win.by_key else []
    ctx["headline"] = hl = headline_pick(ctx["rank"])
    # the variant the breakdowns follow: the headline's, else the top of the
    # ranking (for information), else the day's only fill model's first
    ctx["best"] = ((hl["pick"] or hl["top"] or {}).get("name")
                   or (sorted({split_key(k)[0] for k in agg if split_key(k)[1] == fill})
                       or [None])[0])
    ctx["swp_missing"] = (swp_missing(days_events, day, sim_version)
                          if rfill == "swp" else None)
    # always: the ranking and the tests are on stress, whatever the window has
    ctx["stress_missing"] = stress_missing(days_events, day, sim_version)
    ctx["swp_tables"] = swp_tables(days_events)
    ctx["swp_current"] = current_swp_tables()
    ctx["stress_current"] = current_stress_tables()
    d0_events: List[dict] = []
    if day_number(day) >= 1:
        d0 = (_dt.date.fromisoformat(DAY1) - _dt.timedelta(days=1)).isoformat()
        d0_events = in_shadow(load_sim_day(d0), rec_start)
        ctx["day0_events"] = len(d0_events)
    # the recent regime: favorite-bid hits over the whole recorded period
    # (day 0 .. the report day) -> the tests' second calibrated-tails figure
    ctx["recent"] = recent_rates(d0_events + win.all_events)
    ctx["tests"] = []
    for spec in PREREG_TESTS:
        t = prereg_test(spec, win, day, ctx["recent"], now=now, sim_summary=sim_summary,
                        rec_start=rec_start, cur_version=sim_version)
        apply_frozen(t, day, now, freeze=freeze)
        ctx["tests"].append(t)
    ctx["prereg_verdict"] = prereg_verdict(ctx["tests"])
    # Test A as registered holds hours that settled before the registration:
    # the verdict without them, beside it (Jack decides which window counts)
    ctx["prereg_verdict_alt"] = (prereg_verdict(ctx["tests"], alt=True)
                                 if any(t.get("alt") for t in ctx["tests"]) else None)
    ctx["week"] = (week_summary(win, policy, agg_model(win.all_events), health,
                                ctx["tests"], ctx["rank"])
                   if day_number(day) >= WEEK_DAYS else None)
    return ctx


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

POS, NEG, MUTED = "#1a7f37", "#c62828", "#57606a"
S_TABLE = "border-collapse:collapse;font-size:13px;margin:4px 0 14px 0;"
S_TH = ("text-align:left;padding:4px 8px;border-bottom:2px solid #d0d7de;"
        "background:#f6f8fa;font-weight:600;white-space:nowrap;")
S_THR = S_TH.replace("text-align:left", "text-align:right")
S_TD = "padding:3px 8px;border-bottom:1px solid #eaeef2;vertical-align:top;"
S_TDR = S_TD + "text-align:right;white-space:nowrap;"
S_H2 = "font-size:17px;margin:22px 0 6px 0;padding-bottom:3px;border-bottom:1px solid #d0d7de;"
S_H3 = "font-size:14px;margin:12px 0 4px 0;"
S_NOTE = f"color:{MUTED};font-size:12px;margin:2px 0 8px 0;"


def m_html(x, dec: Optional[int] = None) -> str:
    v = _num(x)
    color = POS if v > 0.005 else (NEG if v < -0.005 else MUTED)
    return f'<span style="color:{color};">{esc(money(v, dec))}</span>'


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
          right: Optional[Sequence[bool]] = None, top: Optional[str] = None) -> str:
    """rows hold pre-rendered HTML cells (str) or plain values (escaped)."""
    right = list(right) if right is not None else [i > 0 for i in range(len(headers))]
    h = [f'<div style="overflow-x:auto;"><table style="{S_TABLE}">']
    if top:
        h.append(top)
    h.append("<tr>" + "".join(
        f'<th style="{S_THR if right[i] else S_TH}">{esc(c)}</th>'
        for i, c in enumerate(headers)) + "</tr>")
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            s = S_TDR if i < len(right) and right[i] else S_TD
            cells.append(f'<td style="{s}">{c if isinstance(c, _Raw) else esc(c)}</td>')
        h.append("<tr>" + "".join(cells) + "</tr>")
    h.append("</table></div>")
    return "".join(h)


class _Raw(str):
    """An already-escaped HTML cell."""


def R(s: str) -> _Raw:
    return _Raw(s)


def M(x, dec: Optional[int] = None) -> _Raw:
    return _Raw(m_html(x, dec))


def h2(t: str) -> str:
    return f'<h2 style="{S_H2}">{esc(t)}</h2>'


def h3(t: str) -> str:
    return f'<h3 style="{S_H3}">{esc(t)}</h3>'


def note(t: str, raw: bool = False) -> str:
    return f'<p style="{S_NOTE}">{t if raw else esc(t)}</p>'


def _empty_reason(ctx: dict) -> str:
    """Why a day has no sim results, from the sim run's own summary first
    (per-event errors / unsettled) and then the missing events' reasons."""
    if ctx.get("sim_error"):
        return "The sim failed (see the error above), so there is nothing to score."
    h = ctx.get("health")
    hh = h or {}
    ws, wx = hh.get("ws") or {}, hh.get("wx") or {}
    ev = hh.get("events") or {}
    miss = ev.get("missing") or []
    kinds: Dict[str, int] = {}
    for m in miss:
        k = m.get("kind") or ("fixable" if m.get("fixable") else "other")
        kinds[k] = kinds.get(k, 0) + 1
    ss = ctx.get("sim_summary") or {}
    n_err, n_pend = len(ss.get("errors") or []), len(ss.get("pending") or [])
    reasons = []
    if n_err:
        every = bool(miss) and kinds.get("sim_error", 0) == len(miss)
        reasons.append(f"{'every due event' if every else f'{n_err} event(s)'} raised in the "
                       "sim (errors listed above): fix the sim, then re-run python "
                       f"temp_shadow_sim.py day --day {ctx['day']} --force")
    if n_pend or kinds.get("pending"):
        reasons.append(f"{max(n_pend, kinds.get('pending', 0))} event(s) were unsettled when "
                       "the sim ran (settlement pull incomplete): re-run the report later")
    if h is not None and not ws.get("rows") and not wx.get("rows"):
        reasons.append("no raw recording for the day at all: was the collector down "
                       "(collector_status.json, collector.log)?")
    else:
        no_file = kinds.get("no_settled", 0) + kinds.get("never_seen", 0)
        if no_file:
            reasons.append(f"{no_file} due event(s) have no settled file: the settlement "
                           "pull may not have run (not settled yet?)"
                           + (f"; {kinds['never_seen']} were never seen by the collector"
                              if kinds.get("never_seen") else ""))
    if kinds.get("fixable"):
        reasons.append("settled files exist but the sim wrote nothing: re-run "
                       f"python temp_shadow_sim.py day --day {ctx['day']} --force")
    if not reasons and h is not None and not ev.get("due"):
        reasons.append("no event of the day was due inside the shadow window")
    if not ctx.get("sim_ran") and not reasons:
        reasons.append("the sim was not run (--no-sim) and sim/ holds nothing for the day")
    return " ".join(reasons) or "No sim output for the day."


def _fill_cols(f: str, agg: Dict[str, dict], primary: Optional[str] = None) -> List[str]:
    """Column heads of one fill model's group in the variants table: opt /
    cons the totals; swp / stress add the Monte Carlo se, the 1c fillers and
    the expected net at calibrated tails; the decision model's group
    (`primary`, default the last fill model present) adds avg share and the
    fills after a weather update."""
    cols = ["reward", "contracts", "trading", "net"]
    if f in ("swp", DECISION_FILL):
        cols += ["+/- mc se", "1c fillers"]
        if any(split_key(k)[1] == f and a.get("net_cal") is not None for k, a in agg.items()):
            cols.append("expected (cal. tails)")
    if primary is None:
        have = [x for x in FILLS if any(split_key(k)[1] == x for k in agg)]
        primary = have[-1] if have else f
    return cols + (["avg share", "after-wx ct / $"] if f == primary else [])


def _rank_order(ctx: dict, names: Iterable[str]) -> List[str]:
    """The ranking's order (robust stress total), then the variants without
    a ranking entry by their cumulative total on the rules' fill model --
    never the report day's net (Jack 2026-10-17, "1")."""
    names = set(names)
    ranked = [r["name"] for r in ctx.get("rank") or [] if r["name"] in names]
    win = ctx.get("win")
    rf = ctx.get("rules_fill") or HEADLINE_FILL
    rest = sorted(names - set(ranked),
                  key=lambda n: (-(win.total(f"{n}|{rf}") if win is not None
                                   and f"{n}|{rf}" in win.agg else 0.0), n))
    return ranked + rest


def _variant_rows(ctx: dict, fills: Sequence[str]) -> List[List[Any]]:
    agg = ctx["agg"]
    primary = ctx.get("fill") if ctx.get("fill") in fills else (list(fills) or [None])[-1]
    names = _rank_order(ctx, {split_key(k)[0] for k in agg})
    best = ctx.get("best")
    rows = []
    for n in names:
        row: List[Any] = [R(f"<b>{esc(n)}</b>" if n == best else esc(n))]
        for f in fills:
            cols = _fill_cols(f, agg, primary)
            a = agg.get(f"{n}|{f}")
            if not a:
                row += ["-"] * len(cols)
                continue
            faw = a["fills_after_wx"]
            row += [M(a["reward"]), qty(a["contracts"]), M(a["trading_pnl"]), M(a["net"])]
            if f in ("swp", DECISION_FILL):
                row.append(usd(a["net_se"]) if a.get("net_se") is not None else "-")
                row.append(qty(a["filler_contracts"]) if a.get("filler_contracts") is not None
                           else "-")
                if "expected (cal. tails)" in cols:
                    row.append("-" if a.get("net_cal") is None else
                               M(a["net_cal"]) if cal_ok(ctx.get("policy"), n) else CIRCULAR)
            if f == primary:
                row += [pct(a["avg_share"]),
                        R(f"{esc(qty(faw['contracts']))} / {m_html(faw['pnl'])}")]
        rows.append(row)
    return rows


def _breakdowns(ctx: dict) -> str:
    best, fill = ctx["best"], ctx["fill"]
    key = f"{best}|{fill}"
    a = ctx["agg"].get(key)
    if not a:
        return note(f"No {fill} results for {best} on the day.")
    hl = ctx.get("headline") or {}
    why = ("the headline variant" if (hl.get("pick") or {}).get("name") == best
           else "the top of the robust ranking, for information: it does not pass"
           if (hl.get("top") or {}).get("name") == best else "")
    out = [note(f"Variant {best} ({fill} fills"
                + (", the sweep-aware model" if fill == "swp" else "")
                + (", the decision model" if fill == DECISION_FILL else "")
                + (f"; means over {a['draws']} Monte Carlo draws" if a.get("draws") else "")
                + (f"; {why}" if why else "") + "), the report day only.")]
    # by ET hour of the event
    hrs: Dict[int, Dict[str, float]] = {}
    for ev in ctx["events"]:
        v = (ev.get("variants") or {}).get(key)
        if not isinstance(v, dict):
            continue
        h = event_hour(ev["event"], ev)
        d = hrs.setdefault(h if h is not None else -1,
                           {"n": 0, "reward": 0.0, "contracts": 0.0, "trading_pnl": 0.0, "net": 0.0})
        d["n"] += 1
        for f in ("reward", "contracts", "trading_pnl", "net"):
            d[f] += _num(v.get(f))
    out.append(h3("By ET hour of the event"))
    out.append(table(["hour", "events", "reward", "contracts", "trading", "net"],
                     [[f"{h:02d}:00" if h >= 0 else "?", d["n"], M(d["reward"]),
                       qty(d["contracts"]), M(d["trading_pnl"]), M(d["net"])]
                      for h, d in sorted(hrs.items())]))
    # class / tau / side
    for title, field, order in (("By strike class", "by_class", ("live", "decided")),
                                ("By minutes to close (tau)", "by_tau", TAU_BUCKETS)):
        data = a.get(field) or {}
        keys = [x for x in order if x in data] + sorted(x for x in data if x not in order)
        out.append(h3(title))
        out.append(table(["bucket", "reward", "contracts", "fills P&L", "net"],
                         [[b, M(data[b].get("reward")), qty(data[b].get("contracts")),
                           M(data[b].get("pnl")),
                           M(_num(data[b].get("reward")) + _num(data[b].get("pnl")))]
                          for b in keys]) if keys else note("(none)"))
    sides = a.get("by_side") or {}
    out.append(h3("By side"))
    out.append(table(["side", "contracts", "fills P&L", "per contract"],
                     [[s, qty(d.get("contracts")), M(d.get("pnl")),
                       (f"{100 * _num(d.get('pnl')) / _num(d.get('contracts')):+.2f}c"
                        if _num(d.get("contracts")) else "-")]
                      for s, d in sorted(sides.items())]) if sides else note("(none)"))
    mo = a.get("markouts") or {}
    extra = [f"fills within 3 s of a weather update: {qty(a['fills_after_wx']['contracts'])} "
             f"contracts, {money(a['fills_after_wx']['pnl'])}",
             f"max |position| {qty(a['max_abs_pos'])}, worst-case event loss {money(a['worst_case_loss'])}"]
    if mo:
        extra.append("markouts vs mid (c/contract, + = in our favour): "
                     + ", ".join(f"{k} {x:+.2f}c" for k, x in sorted(mo.items())))
    out.append(note(" | ".join(extra)))
    # worst events
    evs = []
    for ev in ctx["events"]:
        v = (ev.get("variants") or {}).get(key)
        if isinstance(v, dict):
            evs.append((_num(v.get("net")), ev, v))
    evs.sort(key=lambda x: x[0])
    out.append(h3("Worst 5 events"))
    out.append(table(["event", "settled at", "reward", "contracts", "trading", "net"],
                     [[ev["event"], (f"{_num(ev.get('settlement_value')):.2f}F"
                                     if ev.get("settlement_value") is not None else "unsettled"),
                       M(v.get("reward")), qty(v.get("contracts")), M(v.get("trading_pnl")),
                       M(v.get("net"))] for _, ev, v in evs[:5]]))
    return "".join(out)


def _market_html(ctx: dict) -> str:
    m, cov = ctx["market"], ctx["coverage"]
    best = ctx["best"]
    share = ((ctx["agg"].get(f"{best}|{ctx['fill']}") or {}).get("avg_share")
             if best else None)
    rows = [["strike-seconds", qty(m["strike_secs"])],
            ["paying without us", pct(m["paying_frac_without_us"])],
            ["locked at 99 (pays nobody)", pct(m["locked99_frac"])],
            ["live / decided", f"{pct(m['live_frac'])} / {pct(m['decided_frac'])}"],
            ["depth within 3c (median of events)",
             qty(m["depth3c_median"]) if m["depth3c_median"] is not None else "n/a"],
            [f"our avg share ({best})" if best else "our avg share", pct(share)],
            ["book trusted (program-weighted)", pct(cov.get("book_trusted_frac"))],
            ["trades source", ", ".join(f"{k} x{v}" for k, v in sorted(cov["src"].items())) or "n/a"]]
    return table(["metric", "value"], rows, right=[False, False])


def _model_html(ctx: dict) -> str:
    me = ctx["model"]
    out = []
    if me.get("n"):
        rows = [[b, qty(r["n"]), f"{r['brier_model']:.4f}", f"{r['brier_mid']:.4f}",
                 "model" if r["brier_model"] < r["brier_mid"] else "mid"]
                for b, r in me["by_tau"].items()]
        rows.append([R("<b>all</b>"), qty(me["n"]), f"{me['brier_model']:.4f}",
                     f"{me['brier_mid']:.4f}",
                     "model" if me["brier_model"] < me["brier_mid"] else "mid"])
        out.append(table(["tau (min)", "n", "Brier model", "Brier mid", "better"], rows))
        if me.get("logloss_model") is not None:
            out.append(note(f"Log loss: model {me['logloss_model']:.4f}, mid {me['logloss_mid']:.4f}."))
    else:
        out.append(note("No model evaluation in the day's sim output."))
    fmf = ctx["coverage"].get("fair_missing_frac")
    if fmf is not None:
        out.append(note(f"No fair available for {pct(fmf)} of program time (guard and "
                        "live/decided labels fall back to the book there)."))
    rep = (ctx.get("health") or {}).get("replica") or {}
    if rep.get("error"):
        out.append(note(f"Replica check unavailable: {rep['error']}"))
    elif rep.get("n"):
        out.append(note(f"Index replica: {rep['exact']} of {rep['n']} finalized minutes exact "
                        f"({pct(rep['rate'])}); within 0.05F {rep['near']}; under 4 stations "
                        f"{rep['none']}; calibrations: {rep['calibrations']}."))
        if rep.get("mismatches"):
            out.append(table(["minute (ET)", "official v", "replica", "stations"],
                             [[C.et_dt(t / 1000).strftime("%H:%M"), f"{v:.2f}",
                               f"{r:.2f}" if r is not None else "none", ns]
                              for t, v, r, ns in rep["mismatches"]]))
    elif ctx.get("health") is not None:
        out.append(note("Index replica: no finalized minutes recorded for the day."))
    return "".join(out)


def _health_html(ctx: dict) -> str:
    h = ctx.get("health")
    if not h:
        return note("Collector health not computed.")
    ws, poll, wx = h.get("ws") or {}, h.get("poll") or {}, h.get("wx") or {}
    rows = []
    st = h.get("status")
    if st:
        hb = None
        for k in ("rx", "ts", "time", "heartbeat", "updated"):
            if _opt_num(st.get(k)) is not None:
                hb = _num(st.get(k))
                break
        keys = ("state", "pid", "started", "uptime_s", "connected", "exit_reason", "signing_key")
        bits = [f"{k}={st[k]}" for k in keys if k in st and st[k] not in (None, "")]
        if not bits:
            bits = [f"{k}={v}" for k, v in st.items()
                    if isinstance(v, (str, int, float, bool)) and k not in ("rx", "ts")][:8]
        errs = st.get("errors")
        if isinstance(errs, dict) and errs.get("count"):
            bits.append(f"errors={errs['count']}")
        rows.append(["collector status (now)",
                     (f"heartbeat {C.iso(hb)} ({(ctx['generated'] - hb) / 60:.0f} min before "
                      "this report); " if hb else "") + ", ".join(bits)])
    else:
        rows.append(["collector status", "collector_status.json missing"])
    if ws.get("error"):
        rows.append(["ws", ws["error"]])
    else:
        rows.append(["ws uptime (connected, data flowing)",
                     f"{pct(ws.get('uptime'))} of {ws.get('minutes', 0)} min with a market listed"
                     + (f" ({ws['minutes_no_market']} min with none, not counted)"
                        if ws.get('minutes_no_market') else "")
                     + f"; {ws.get('rows', 0):,} rows"])
        rows.append(["ws reconnects / disconnects / seq gaps / resubscribes",
                     f"{ws.get('connects', 0)} / {ws.get('disconnects', 0)} / {ws.get('gaps', 0)} / "
                     f"{ws.get('resubscribes', 0)}"
                     + (f" (last reasons: {'; '.join(ws['disconnect_reasons'])})"
                        if ws.get("disconnect_reasons") else "")])
    rows.append(["book trusted (sim, program-weighted)", pct(ctx["coverage"].get("book_trusted_frac"))])
    if poll.get("error"):
        rows.append(["wx polls", poll["error"]])
    else:
        lat = poll.get("lat_p50_median")
        rows.append(["wx polls / 429s / errors",
                     f"{poll.get('n', 0):,} / {poll.get('n429', 0):,} / {poll.get('nerr', 0):,}; "
                     f"poll rows in {poll.get('minutes', poll.get('rows', 0))} of "
                     f"{poll.get('expected', 0)} min "
                     f"({pct(poll.get('coverage'))}); signed minutes {poll.get('signed_minutes', 0)}"])
        rows.append(["poll latency",
                     (f"p50 median {lat:.0f} ms, max {poll.get('lat_max') or 0:.0f} ms"
                      if lat is not None else "n/a")])
    if wx.get("error"):
        rows.append(["wx rows", wx["error"]])
    else:
        rows.append(["wx minutes seen / finalized",
                     f"{wx.get('minutes_seen', 0)} / {wx.get('minutes_finalized', 0)} of "
                     f"{wx.get('minutes_expected', 0)}; {wx.get('rows', 0):,} rows"])
        if wx.get("obs_lag_n"):
            rows.append(["our observation lag (rx - last station received_at)",
                         f"median {wx['obs_lag_median']:.2f} s, p90 {wx['obs_lag_p90']:.2f} s "
                         f"over {wx['obs_lag_n']} minutes"
                         + (f"; last station lands :{wx['land_sec_p10']:02.0f}-"
                            f":{wx['land_sec_p90']:02.0f} (p10-p90)"
                            if wx.get("land_sec_p10") is not None else "")
                         + (f"; any station first seen median {wx['station_lag_median']:.2f} s, "
                            f"p90 {wx['station_lag_p90']:.2f} s"
                            if wx.get("station_lag_median") is not None else "")
                         + (f"; official v at +{wx['official_lag_median']:.0f} s"
                            if wx.get("official_lag_median") is not None else "")])
        else:
            rows.append(["our observation lag", "n/a (no completed minutes)"])
    ev = h.get("events") or {}
    rows.append(["events simmed / due",
                 f"{ev.get('simmed', 0)} / {ev.get('due', 0)}"
                 + (f" ({ev.get('not_due')} not due yet)" if ev.get("not_due") else "")
                 + (f"; {ev.get('outside')} hour(s) outside the shadow (before the recording "
                    "began / after it ended)" if ev.get("outside") else "")
                 + (f"; {ctx['coverage']['unsettled']} unsettled" if ctx["coverage"]["unsettled"] else "")])
    ss = ctx.get("sim_summary") or {}
    if ss:
        rows.append(["sim run (this report)",
                     f"{ss.get('ok', 0)} ok, {len(ss.get('pending') or [])} pending (unsettled), "
                     f"{len(ss.get('errors') or [])} failed"])
        ws = ss.get("window") or {}
        if ws.get("days"):
            rows.append(["sim run (earlier window days)",
                         f"{ws.get('ok', 0)} ok, {len(ws.get('pending') or [])} pending "
                         f"(unsettled), {len(ws.get('errors') or [])} failed over "
                         f"{len(ws['days'])} day(s) (cached files that pass the sim's check "
                         "are reused)"])
    flags = ctx["coverage"].get("flags") or {}
    if flags:
        rows.append(["sim flags (events)",
                     ", ".join(f"{k} x{v}" for k, v in sorted(flags.items(), key=lambda x: -x[1])[:8])])
    if h.get("errors"):
        rows.append(["collector error rows",
                     ", ".join(f"{k} x{v}" for k, v in sorted(h["errors"].items(), key=lambda x: -x[1])[:5])])
    out = [table(["check", "value"], rows, right=[False, False])]
    miss = ev.get("missing") or []
    if miss:
        out.append(h3(f"Missing events ({len(miss)})"))
        out.append(table(["event", "likely reason"], [[m["event"], m["why"]] for m in miss[:24]],
                         right=[False, False]))
    if h.get("partial"):
        why = []
        if h.get("started_late"):
            why.append("the recording began then")
        if h.get("shadow_ended"):
            why.append("the shadow ended then")
        elif h.get("ended_early"):
            why.append("the day is not over")
        span = ""
        if _opt_num(h.get("t0")) is not None and _opt_num(h.get("t1")) is not None:
            span = (f" {C.et_dt(h['t0']).strftime('%H:%M')}-"
                    f"{C.et_dt(h['t1']).strftime('%H:%M')} ET")
        out.append(note(f"Health covers{span} only ({'; '.join(why) or 'part of the day'}): "
                        "uptime and coverage are over that span."))
    return "".join(out)


def _suggestions_html(ctx: dict) -> str:
    act = [s for s in ctx["suggestions"] if not s["suppressed"]]
    quiet = [s for s in ctx["suggestions"] if s["suppressed"]]
    out = []
    if act:
        items = []
        for s in act:
            tag = {"policy": "", "risk": "RISK: ", "ops": "OPS: ", "note": "NOTE: "}.get(s["kind"], "")
            items.append(
                f'<li style="margin:0 0 8px 0;"><b>{esc(tag + s["title"])}</b><br>'
                f'<span style="color:{MUTED};">Evidence:</span> {esc(s["evidence"])}<br>'
                f'<span style="color:{MUTED};">Change:</span> <code style="background:#f6f8fa;'
                f'padding:1px 4px;">{esc(s["change"])}</code></li>')
        out.append('<ol style="padding-left:20px;margin:6px 0;">' + "".join(items) + "</ol>")
    else:
        out.append(note("No suggestion clears the noise gates yet."))
    if quiet:
        out.append(h3("Below noise (not acted on)"))
        out.append('<ul style="padding-left:20px;margin:4px 0;font-size:12px;color:#57606a;">'
                   + "".join(f'<li>{esc(s["title"])} -- {esc(s["why"])}</li>' for s in quiet)
                   + "</ul>")
    out.append(note(f"Gates: >= ${MIN_USD_PER_DAY:.0f}/day, >= {MIN_EVENTS} differing events, "
                    f"|delta| >= {MIN_Z:g} SE (per-event spread, Monte Carlo noise included). "
                    f"Pairs use the {ctx.get('rules_fill', HEADLINE_FILL)} fill model over the "
                    "cumulative window"
                    + (" (every variant has it; the decisions above use stress, and only a "
                       "pre-registered test makes a variant eligible for a pilot)"
                       if ctx.get("fill") == DECISION_FILL else "") + "."))
    for n in ctx.get("notes") or []:
        out.append(note(n))
    return "".join(out)


def _ci_text(r: dict) -> str:
    if r.get("ci_lo") is None:
        return "-"
    return f"{money(r['ci_lo'], 0)} .. {money(r['ci_hi'], 0)}"


def _reg_label() -> str:
    """The registration time as the report prints it."""
    return C.et_dt(_ts(PREREG_REGISTERED_AT)).strftime("%Y-%m-%d %H:%M:%S") + " ET (eff9cd3)"


def _judged_text(r: dict) -> str:
    return _et_hm(r["judged_from"]) if r.get("judged_from") is not None else "window start"


def judged_rule_text() -> str:
    """Which hours the headline judges each variant on, in words."""
    a = PREREG_TESTS[0]
    parts = [f"{a['variant']} and the other FAV_* variants from Test {a['id']}'s start "
             f"({_et_hm(_ts(a['start']))})"]
    for t in PREREG_TESTS[1:]:
        parts.append(f"{t['variant']} from Test {t['id']}'s ({_et_hm(_ts(t['start']))})")
    for n, s in sorted(HEADLINE_JUDGED_FROM.items()):
        parts.append(f"{n} from {_et_hm(_ts(s))}")
    return ", ".join(parts) + ", every other variant from the window start"


def _crit_value(t: dict, c: str) -> str:
    """A criterion's value in one view of a test ("so far" until it is
    decided)."""
    cr = t["crit"][c]
    val = cr["value"]
    fin = t["status"] in ("PASS", "FAIL")
    if c == "days":
        n = t["n_days"] if fin else t["days_done"]
        s = f"{val} of {n} day{'' if n == 1 else 's'}" + ("" if fin else " so far")
        if not fin and not cr["met"]:
            s += "; still possible" if cr.get("possible") else "; no longer possible"
        return s
    return (money(val) if val is not None else "n/a") + ("" if fin else " so far")


def _met_html(ok: bool) -> str:
    return f'<b style="color:{POS if ok else NEG};">{"met" if ok else "not met"}</b>'


def _test_status_html(label: str, t: dict) -> str:
    sc = {"PASS": POS, "FAIL": NEG}.get(t["status"], "#9a6700")
    return (f'<p style="margin:2px 0 4px 0;">{esc(label)}: <b style="color:{sc};">'
            f'{esc(t["text"])}</b></p>')


def _frozen_text(t: dict) -> str:
    """The frozen-verdict line of a test ('' when nothing is frozen)."""
    out = []
    fz = t.get("frozen")
    if fz and fz.get("new"):
        out.append(f"Verdict frozen now ({fz['path']}, report day {fz['report_day']}): later "
                   "reports show it as frozen.")
    elif fz:
        rc = t.get("recomputed") or {}
        out.append(f"Frozen verdict ({fz.get('frozen_at')}, report day {fz.get('report_day')}): "
                   + ("a recompute today matches it." if not t.get("frozen_diffs") else
                      f"A RECOMPUTE TODAY DIFFERS in {', '.join(t['frozen_diffs'])} (now: "
                      f"{rc.get('text')}, stress {money(rc.get('total'))}); the frozen verdict "
                      "stands -- find out what changed a window file."))
    fo = t.get("frozen_other")
    if fo:
        out.append(f"A frozen verdict of another test definition is on disk ({fo['path']}, "
                   f"{fo.get('frozen_at')}, {fo.get('status')}): not used.")
    return " ".join(out)


def _pin_table(t: dict) -> str:
    rows = [[d["field"], _fmt_val(d["pinned"]), _fmt_val(d["found"]), d["files"], d["first"]]
            for d in t["pin_diffs"][:24]]
    return (table(["field", "registered", "found", "window files", "first"], rows,
                  right=[False, False, False, True, False])
            + note(f"{len(t['pin_diffs'])} difference(s) between the registered rule / model "
                   "(PREREG_TESTS, PREREG_PIN_MODEL) and the window's sim files: the test is "
                   "INVALID and never passes. Restore the registered rule and model and re-sim "
                   "the window, or pre-register a new window (TEMP_SHADOW.md: a changed rule "
                   "needs one)."))


def _prereg_test_html(t: dict) -> str:
    a = t.get("alt")
    out = [h3(f"Test {t['id']} -- {t['variant']} ({t['why']}): ET days {t['first_day']} .. "
              f"{t['last_day']} (from {_et_hm(_ts(t['start']))})")]
    out.append(_test_status_html("As registered" if a else "Status", t))
    if a:
        out.append(_test_status_html(
            f"Excluding the {t['known_n']} hours known at registration (settled before "
            f"{_reg_label()})", a))
    ft = _frozen_text(t)
    if ft:
        out.append(note(ft))
    if t["status"] == "not started":
        out.append('<ul style="padding-left:20px;margin:4px 0;">'
                   + "".join(f"<li>{esc(t['crit'][c]['label'])}</li>" for c in t["criteria"])
                   + "</ul>")
        out.append(note(f"PASS iff all of the above over ET days {t['first_day']} .. "
                        f"{t['last_day']}; nothing is judged before its window starts."))
        return "".join(out)
    if t["pin_diffs"]:
        out.append(_pin_table(t))
    drows = []
    for r in t["rows"]:
        ar = next((x for x in a["rows"] if x["day"] == r["day"]), None) if a else None
        if r["future"]:
            drows.append([r["day"], "-", "-", "-", "-", "not yet"] + (["", "-"] if a else []))
            continue
        flag = []
        if r["missing"]:
            flag.append(f"{r['missing']} expected hour(s) without stress")
        if r["later"]:
            flag.append(f"{r['later']} not due yet")
        if r["not_listed"]:
            flag.append(f"{r['not_listed']} not listed by Kalshi")
        row = [r["day"], f"{r['events']} / {r['expected']}", M(r["reward"]), M(r["trading"]),
               M(r["net"]), ("yes" if r["net"] > 0 else "no")
               + (f" ({'; '.join(flag)})" if flag else "")]
        if a:
            row += [R(f"{r['known']} hours, {m_html(r['known_net'])}") if r["known"] else "",
                    M(ar["net"]) if ar and not ar["future"] else "-"]
        drows.append(row)
    head = ["ET day", "hours in / expected", "reward", "trading", "stress net", "net > 0"]
    if a:
        head += ["known at registration", "net after registration"]
    out.append(table(head, drows, right=[False, True, True, True, True, False]
                     + ([True, True] if a else [])))
    if t["known"]:
        out.append(note(f"Known at registration: these {len(t['known'])} hours settled before "
                        f"{_reg_label()}, when the tests were committed (the 10/17 audit, "
                        f"6e6ebdf, had published {t['variant']}'s figures for them). They are in "
                        "the window as registered; the second status and the last column leave "
                        "them out (Jack decides which window counts): "
                        + ", ".join(f"{e.split('-', 1)[-1]} {money(n)}" for e, n in t["known"])
                        + "."))
    if t["missing"]:
        out.append(table(["expected hour without a stress result", "why"],
                         [[e, w] for e, w in t["missing"][:30]], right=[False, False])
                   + (note(f"... and {len(t['missing']) - 30} more.")
                      if len(t["missing"]) > 30 else ""))
    if t["not_listed"]:
        out.append(note("Not listed by Kalshi (the collector was recording and never saw "
                        "them: not expected): " + ", ".join(t["not_listed"]) + "."))
    crow = []
    for c in t["criteria"]:
        row = [t["crit"][c]["label"],
               R(f"{esc(_crit_value(t, c))} -- {_met_html(t['crit'][c]['met'])}")]
        if a:
            row.append(R(f"{esc(_crit_value(a, c))} -- {_met_html(a['crit'][c]['met'])}"))
        crow.append(row)
    fin = "" if t["status"] in ("PASS", "FAIL") else " so far"
    if "robust" in t.get("info", ()):
        row = ["for information: the total excluding the best and worst hour",
               money(t["robust"]) + fin if t["robust"] is not None else "n/a"]
        if a:
            row.append(money(a["robust"]) + fin if a["robust"] is not None else "n/a")
        crow.append(row)
    if "recent" in t.get("info", ()):
        def rec(x):
            return (f"{money(x['recent']) if x.get('recent') is not None else 'n/a'}{fin} (table-"
                    f"based {money(x['cal']) if x.get('cal') is not None else 'n/a'})")
        row = ["for information: the calibrated-tails expectation at the RECENT REGIME (the "
               "favorite's upset rate after a favorite-bid hit, over the recorded period)",
               rec(t)]
        if a:
            row.append(rec(a))
        crow.append(row)
    out.append(table(["criterion", "as registered" if a else "value"]
                     + (["excluding known hours"] if a else []), crow,
                     right=[False, False] + ([False] if a else [])))
    out.append(note(f"{t['hours']} event-hours of {t['variant']} with stress results in the "
                    "window so far (it rests nothing outside its settle hours 20:00-07:00 ET: "
                    "those hours count $0). PASS iff every criterion is met once the window's "
                    "last hour is due (its settlement + 2 h) and every expected hour has a "
                    "stress result; an expected hour without one makes the test INCOMPLETE, a "
                    "window file run under another rule or model makes it INVALID (rule or "
                    "model changed): neither ever passes. Best / worst hour so far: "
                    f"{money(t['best']) if t.get('best') is not None else 'n/a'} / "
                    f"{money(t['worst']) if t.get('worst') is not None else 'n/a'}."))
    g = t.get("fav_gate")
    if g:
        share = g["gate_secs"] / g["model_secs"] if g["model_secs"] else None
        out.append(note(f"Agreement gate in the shadow: it let a favorite join through in "
                        f"{pct(share)} of the {g['model_secs']:,} strike-seconds where the "
                        f"model alone would join ({g['files']} window files). "
                        + GATE_RECON_NOTE))
    return "".join(out)


def _ranking_html(ctx: dict) -> str:
    """The headline ranking table: robust total first, each variant on its
    judged hours (its in-sample hours beside it, never ranked)."""
    rows = ctx.get("rank") or []
    if not rows:
        return note("No stress results to rank in the window yet.")
    fill = rows[0]["fill"]
    pol_ = ctx.get("policy")
    show_cal = any(r.get("cal") is not None for r in rows)
    show_in = any(r.get("in_hours") for r in rows)
    head = ["#", "variant", "judged from", f"robust {fill} total", "total",
            "90% CI (3-h blocks)", "P(total > 0)", "best hour", "worst hour", "noise-dominated",
            "hours"]
    head += ["in-sample hours (not ranked)"] if show_in else []
    head += ["expected (cal. tails)"] if show_cal else []
    out_rows = []
    for r in rows:
        name = esc(r["name"])
        if not r.get("judged"):
            row: List[Any] = ["-", R(f'{name} <span style="color:{MUTED};">(no judged hour '
                                     'yet)</span>'), _judged_text(r), "-", "-", "-", "-", "-",
                              "-", "", 0]
        else:
            if r.get("thin"):
                name += (f' <span style="color:{MUTED};">({r["days"]} judged day'
                         f'{"" if r["days"] == 1 else "s"}: not named before '
                         f'{HEADLINE_MIN_DAYS})</span>')
            row = [r["rank"], R(f"<b>{name}</b>" if r["passes"] else name), _judged_text(r),
                   M(r["robust"]) if r["robust"] is not None else "-", M(r["total"]),
                   _ci_text(r), f"{r['p_pos']:.2f}" if r["p_pos"] is not None else "-",
                   R(f"{m_html(r['best'])} ({esc(r['best_event'][-4:])})"),
                   R(f"{m_html(r['worst'])} ({esc(r['worst_event'][-4:])})"),
                   "NOISE" if r["noise"] else "", r["hours"]]
        if show_in:
            row.append(R(f"{r['in_hours']} hours, {m_html(r['in_total'])}")
                       if r.get("in_hours") else "")
        if show_cal:
            row.append(M(r["cal"]) if r.get("cal") is not None else
                       ("-" if cal_ok(pol_, r["name"]) else CIRCULAR))
        out_rows.append(row)
    win = ctx["win"]
    return (table(head, out_rows, right=[True, False, False] + [True] * (len(head) - 3))
            + note(f"Ranked on {fill} net over each variant's judged hours in the cumulative "
                   f"window ({win.days[0]} .. {win.days[-1]}) without its best and worst "
                   f"event-hour (\"robust total\"; an hour it rests nothing counts $0). Judged "
                   f"from = the first settle time a variant is judged on, never the hours it was "
                   f"chosen from: {judged_rule_text()}; earlier hours are in-sample (shown, "
                   f"never ranked). The headline names the top variant only when its robust "
                   f"total > 0, P(total > 0) >= {HEADLINE_P_MIN:g} and its judged hours span at "
                   f"least {HEADLINE_MIN_DAYS} ET days -- no single-day ranking (bold = "
                   f"passes); the "
                   f"90% CI and P(total > 0) are a moving-block bootstrap of {BOOT_BLOCK} "
                   f"consecutive event-hours ({BOOT_N} resamples: losses cluster in consecutive "
                   f"hours); noise-dominated = |best hour| + |worst hour| > |total|. Best / "
                   f"worst hour: the event's ET date and hour (DDHH). No single day's realized "
                   f"net ranks anything."))


def _rate_text(b: str, r: dict) -> str:
    ci = r.get("ci") or (None, None)
    return (f"{b}: {pct(r.get('rate'), 1)} [{pct(ci[0], 1)}, {pct(ci[1], 1)}] of "
            f"{r.get('clusters', 0)} hits ({r.get('upset_strikes', 0)} upset strikes), model "
            f"{pct(r.get('model'), 2)}")


def _prereg_html(ctx: dict) -> str:
    """The pre-registered tests, exactly as TEMP_SHADOW.md defines them."""
    tests = ctx.get("tests") or []
    rec = ctx.get("recent") or {}
    v, va = ctx.get("prereg_verdict") or "", ctx.get("prereg_verdict_alt")

    def col(x):
        return POS if x.startswith("pilot eligible") else NEG if x.startswith("NO-GO") \
            else "#9a6700"
    out = [f'<p style="font-size:16px;margin:4px 0;">Pre-registered verdict: '
           f'<b style="color:{col(v)};">{esc(v)}</b>'
           + (f' (as registered); excluding the hours known at registration: '
              f'<b style="color:{col(va)};">{esc(va)}</b>' if va else "") + '</p>',
           note("Adopted go-live rules (Jack 2026-10-17): rewards are assumed paid; decisions "
                "use the STRESS fill model; a variant passes only on hours AFTER it was "
                "chosen. Defined in TEMP_SHADOW.md \"Pre-registered tests\" (committed "
                f"{_reg_label()}); nothing is re-tuned on window data, and every window sim "
                "file must carry the registered rule and model (PREREG_PIN_MODEL: SIM_VERSION "
                f"{PREREG_PIN_MODEL['sim_version']}, the swp / stress tables, draws and seeds, "
                "the fair model's params). " + PILOT_RULES)]
    for t in tests:
        out.append(_prereg_test_html(t))
    bk = rec.get("buckets") or {}
    if bk:
        out.append(note("Recent regime (the tests' second calibrated-tails figure): the "
                        "favorite's upset rate after a taker hit a favorite side's bids at 90-99c "
                        f"(cluster-weighted, strike-clustered bootstrap 90% CI), over the "
                        f"{rec.get('hours')} recorded hours {rec.get('first')} .. "
                        f"{rec.get('last')}, by the favorite's model fair: "
                        + "; ".join(_rate_text(b, r) for b, r in bk.items())
                        + ". Each favorite-side stress fill is re-marked at its bucket's rate "
                          "instead of the tail table's (the 10/17 audit: favorite-bid takers "
                          "were far more informed in the recorded hours than in the table's "
                          "530 hours). It is information, not a criterion."))
    return "".join(out)


def _cumulative_html(ctx: dict) -> str:
    win: Window = ctx["win"]
    if not win.all_events:
        return note("No sim output in the window yet.")
    # the decision model's columns first; a window without any stress result
    # (older output, stress failed everywhere) shows the rules' model here --
    # information only: the ranking and the headline never leave stress
    hf = ctx.get("fill") or decision_fill(win)
    if not any(split_key(k)[1] == hf for k in win.agg):
        hf = ctx.get("rules_fill") or hf
    names = _rank_order(ctx, {split_key(k)[0] for k in win.agg})
    show_days = [d for d in win.days if win.days_events[d]][-9:]
    others = [f for f in reversed(FILLS) if f != hf and any(split_key(k)[1] == f
                                                             for k in win.agg)]
    mc = hf in ("swp", DECISION_FILL)
    cal = mc and any(win.has(f"{n}|{hf}", "net_cal") for n in names if f"{n}|{hf}" in win.agg)
    rob = {r["name"]: r for r in ctx.get("rank") or [] if r["fill"] == hf}
    head = ["variant"] + ([f"{hf} robust"] if rob else []) + [f"{hf} sum", f"{hf} /day",
                                                              "days +"]
    head += (["+/- mc se"] if mc else []) + (["expected (cal. tails)"] if cal else [])
    for f in others:
        head += [f"{f} sum", f"{f} /day"]
    head += [(f"d{day_number(d)}" if day_number(d) >= 1 else d[5:])
             + ("*" if d in win.partial else "") for d in show_days]
    rows = []
    for n in names:
        row: List[Any] = [n]
        key = f"{n}|{hf}"
        if rob:
            rr = rob.get(n)
            row.append(M(rr["robust"]) if rr and rr["robust"] is not None else "-")
        if key in win.agg:
            dd = win.full_days_with(key) or win.days_with(key)
            row += [M(win.total(key)), M(win.per_day(key)),
                    f"{sum(1 for d in dd if win.day_aggs[d][key]['net'] > 0)}/{len(dd)}"]
            if mc:
                se = win.mc_se(key)
                row.append(usd(se) if se is not None else "-")
            if cal:
                row.append("-" if not win.has(key, "net_cal") else
                           M(win.total(key, "net_cal")) if cal_ok(ctx.get("policy"), n)
                           else CIRCULAR)
        else:
            row += ["-"] * (3 + (1 if mc else 0) + (1 if cal else 0))
        for f in others:
            k2 = f"{n}|{f}"
            row += [M(win.total(k2)), M(win.per_day(k2))] if k2 in win.agg else ["-", "-"]
        for d in show_days:
            a = win.day_aggs[d].get(key)
            row.append(M(a["net"], 0) if a else "-")
        rows.append(row)
    out = [table(head, rows)]
    part = ""
    for d in sorted(win.partial):
        part += (f" * {d} is partial (the shadow ended inside it; "
                 f"{_fmt_days(24 * win.weights.get(d, 1.0))} of 24 event-hours): its net is in "
                 "the sums, counts as that fraction of a day in /day, and is left out of "
                 "days +.")
    out.append(note(f"Window {win.days[0]} .. {win.days[-1]}; days + and the day columns are "
                    f"{hf} net"
                    + (" (a variant without stress results shows '-' there)"
                       if hf == DECISION_FILL else "") + "."
                    + (f" Day 0 (partial, {ctx['day0_events']} events) is excluded."
                       if ctx.get("day0_events") else "") + part
                    + (" " + CAL_NOTE if cal else "")))
    return "".join(out)


def _week_html(ctx: dict) -> str:
    w = ctx["week"]
    v = w["verdict"]
    va = w.get("verdict_after_registration")

    def col(x):
        return POS if x.startswith("pilot eligible") else NEG if x.startswith("NO-GO") \
            else "#9a6700"
    out = [f'<p style="font-size:20px;margin:6px 0;"><b style="color:{col(v)};">'
           f'{esc(v)}</b></p>']
    if va:
        out.append(f'<p style="margin:2px 0 6px 0;">Excluding the hours known at registration: '
                   f'<b style="color:{col(va)};">{esc(va)}</b></p>')
    out.append(note("From the adopted go-live rules (Jack 2026-10-17): the pre-registered tests "
                    "decide; the old GO / NO-GO thresholds on the best variant's realized net "
                    "are gone. " + w["reason"] + "."))
    if w.get("best"):
        cfg, pc, ov = w.get("config") or {}, w.get("pilot_config") or {}, w.get("overrides") or {}
        out.append(h3(f"Pilot config ({w['best']}, rule 4: 1/3 size)"))
        out.append(note(w.get("pilot") or PILOT_RULES))
        if pc:
            prow = []
            for k, v_ in sorted(pc.items()):
                if _skip_key(k):
                    continue
                val = _fmt_val(v_) + (f" (1/3 of the variant's {_fmt_val(cfg.get(k))})"
                                      if cfg.get(k) != v_ else "")
                prow.append([R(f"<b>{esc(k)}</b>" if k in ov else esc(k)), val])
            out.append(table(["param", "pilot value"], prow, right=[False, False]))
            out.append(note("Bold = the variant's overrides. The pilot runs the tested variant "
                            f"at 1/3 size (q_fav {_fmt_val(cfg.get('q_fav'))} -> "
                            f"{_fmt_val(pc.get('q_fav'))}; the caps unchanged) in its night "
                            "hours, with the $300 daily halt and the hourly live-vs-shadow "
                            "reconciliation."
                            + (" " + GATE_RECON_NOTE if pc.get("fav_market_min_p") is not None
                               else "")))
        else:
            out.append(note(f"Variant params unavailable ({w.get('policy_err') or 'not in VARIANTS'})."))
    else:
        out.append(note("No variant is eligible for a live pilot."))
    out.append(h3("Key risks"))
    out.append('<ul style="padding-left:20px;margin:4px 0;">'
               + "".join(f"<li>{esc(r)}</li>" for r in w["risks"]) + "</ul>")
    return "".join(out)


def _failed_but_loaded(ctx: dict) -> Dict[str, Any]:
    """Report-day events that failed in this run's sim but whose older sim
    file is still loaded (event -> its sim_version)."""
    errs = {e for e, _ in ((ctx.get("sim_summary") or {}).get("errors") or [])}
    return {str(ev.get("event")): ev.get("sim_version") for ev in ctx.get("events") or []
            if str(ev.get("event")) in errs}


def render_html(ctx: dict) -> str:
    day, agg = ctx["day"], ctx["agg"]
    best, fill = ctx["best"], ctx["fill"]
    n_ev = len(ctx["events"])
    parts = [f'<div style="max-width:1000px;">',
             f'<h1 style="font-size:21px;margin:0 0 2px 0;">Temp shadow (KXTEMPMIAH) -- '
             f'{esc(ctx["label"])}</h1>',
             note(f"ET day {day}; {n_ev} events simmed; generated {C.iso(ctx['generated'])}. "
                  "Shadow only: no orders were placed.")]
    if ctx.get("sim_error"):
        parts.append(f'<div style="border:2px solid {NEG};background:#fff5f5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:{NEG};">THE SIM FAILED</b> -- the figures '
                     f'below are whatever sim output was already on disk.<pre style="white-space:'
                     f'pre-wrap;font-size:11px;margin:6px 0 0 0;">{esc(ctx["sim_error"])}</pre></div>')
    ss = ctx.get("sim_summary") or {}
    if ss.get("errors"):
        old = _failed_but_loaded(ctx)
        if not old:
            how = " (left out of every figure below)"
        elif len(old) == len(ss["errors"]):
            how = (" -- an older sim file of each is still loaded and in the figures below "
                   "(flagged stale)")
        else:
            how = (f" -- {len(old)} of them still show an older sim file in the figures below "
                   "(flagged stale); the rest are left out")
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{len(ss["errors"])} event(s) '
                     f'failed in the sim</b>{esc(how)}'
                     + table(["event", "error", "in the figures"],
                             [[e, err, (f"older file (SIM_VERSION {old[e]})" if e in old
                                        else "no")] for e, err in ss["errors"][:12]],
                             right=[False, False, False]) + "</div>")
    sm = ctx.get("swp_missing") or {}
    if sm.get("total"):
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{sm["total"]} of {sm["events"]} '
                     f'event(s) in the window have no swp results</b> ('
                     + esc(", ".join(f"{d} x{n}" for d, n in sorted(sm["by_day"].items())))
                     + '): the swp columns, the headline, the rules and the verdict cover the '
                     'other events; opt and cons cover all of them.'
                     + (table(["event (report day)", "why"], [[e, w] for e, w in sm["day"][:12]],
                              right=[False, False]) if sm["day"] else "") + "</div>")
    stm = ctx.get("stress_missing") or {}
    if stm.get("total"):
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{stm["total"]} of {stm["events"]} '
                     f'event(s) in the window have no stress results</b> ('
                     + esc(", ".join(f"{d} x{n}" for d, n in sorted(stm["by_day"].items())))
                     + '): the ranking and the pre-registered tests cover the other events; a '
                     'test counts a missing hour of its window as missing: "in progress" until '
                     'the window is over, then INCOMPLETE (never PASS) until a re-sim fills it.'
                     + (table(["event (report day)", "why"], [[e, w] for e, w in stm["day"][:12]],
                              right=[False, False]) if stm["day"] else "") + "</div>")
    rej = swp_rejections(ctx)
    if rej:
        parts.append(f'<div style="border:2px solid {NEG};background:#fff5f5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:{NEG};">swp table problem</b> -- '
                     + esc("; ".join(rej))
                     + '. A rejected model/ override is not used: swp / stress run on the repo '
                     'default table. Fix or remove the file (TEMP_SHADOW.md "Fill models").</div>')
    wdef = (ss.get("window") or {}).get("deferred") or []
    if wdef:
        bm = (ss.get("window") or {}).get("budget_min")
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{len(wdef)} earlier-day event(s) '
                     f'not re-simmed within the time budget</b> ({esc(f"{bm:g}" if bm else "?")} '
                     'min): their older sim files, if any, stay in the cumulative figures '
                     '(flagged stale) and the next report continues the re-sim.</div>')
    w_err = (ss.get("window") or {}).get("errors") or []
    if w_err:
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{len(w_err)} re-sim failure(s) on '
                     f'earlier window days</b> (the older sim output, if any, stays in the '
                     f'cumulative figures)'
                     + table(["event / day", "error"], [[e, err] for e, err in w_err[:12]],
                             right=[False, False]) + "</div>")
    stale = ctx.get("stale_sim") or {}
    if stale:
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{sum(stale.values())} sim file(s) '
                     f'in the cumulative window are from an older SIM_VERSION than '
                     f'{esc(str(ctx.get("sim_version")))}</b> ('
                     + esc(", ".join(f"{d} x{n}" for d, n in sorted(stale.items())))
                     + '): a variant name may hold different settings on those days, so the '
                     'cumulative figures mix the two. Re-sim with <code>python temp_shadow_sim.py '
                     'day --day &lt;day&gt;</code>.</div>')
    # 1 headline: the robust ranking over the cumulative window
    parts.append(h2("Headline"))
    hl = ctx.get("headline") or {}
    phrase = headline_phrase(hl)
    parts.append(f'<p style="font-size:15px;margin:4px 0;"><b>'
                 f'{esc(phrase[:1].upper() + phrase[1:])}</b></p>')
    feat = hl.get("pick") or hl.get("top")
    if feat:
        desc = ctx["policy"].desc(feat["name"])
        lbl = ("Headline variant" if hl.get("pick") is feat and hl.get("pass") else
               "Passes the bar below the top" if hl.get("pick") is feat else
               "Top robust total, for information (it does not pass)")
        parts.append(
            f'<p style="margin:4px 0;">{esc(lbl)}: <b>{esc(feat["name"])}</b>'
            + (f' <span style="color:{MUTED};">({esc(desc)})</span>' if desc else "")
            + f' -- robust {esc(feat["fill"])} total '
            + (f'<b>{m_html(feat["robust"])}</b>' if feat["robust"] is not None else "n/a")
            + f' over {feat["hours"]} event-hours ({feat["days"]} '
              f'day{"" if feat["days"] == 1 else "s"}, judged from '
              f'{esc(_judged_text(feat))}) = the total '
              f'{m_html(feat["total"])} without its best hour {m_html(feat["best"])} '
              f'({esc(feat["best_event"])}) and its worst {m_html(feat["worst"])} '
              f'({esc(feat["worst_event"])}); 90% CI {esc(_ci_text(feat))}; P(total &gt; 0) '
            + (f'{feat["p_pos"]:.2f}' if feat["p_pos"] is not None else "n/a")
            + ("; <b>noise-dominated</b> (|best hour| + |worst hour| &gt; |total|)"
               if feat["noise"] else "")
            + (f'; expected (calibrated tails) {m_html(feat["cal"])}'
               if feat.get("cal") is not None else "")
            + ("; no calibrated-tails expectation (it quotes live strikes: circular)"
               if feat.get("cal") is None and not cal_ok(ctx.get("policy"), feat["name"])
               else "") + '.</p>')
    ins = [in_sample_text(r) for r in hl.get("in_sample") or []]
    if ins:
        parts.append(note("In-sample hours, before each variant's first judged hour (the hours "
                          "it was chosen from, or before its registration; shown for "
                          "information, never ranked): " + "; ".join(ins) + "."))
    if best and f"{best}|{fill}" in agg:
        a = agg[f"{best}|{fill}"]
        se = a.get("net_se")
        parts.append(note(f"{best} on the report day ({fill}): net {money(a['net'])}"
                          + (f" (+/- {usd(se)} Monte Carlo se)" if se is not None else "")
                          + f" = reward {money(a['reward'])} + trading "
                            f"{money(a['trading_pnl'])} over {a['n_events']} events. A single "
                            "day's net ranks nothing."
                          + (f" {a['unsettled']} event(s) not settled: their trading P&L is "
                             "incomplete." if a.get("unsettled") else "")))
    if not ctx["events"]:
        parts.append(f'<p style="font-size:15px;margin:4px 0;"><b>No sim results for {esc(day)}.</b> '
                     f'{esc(_empty_reason(ctx))}</p>')
    tests = ctx.get("tests") or []
    if tests:
        va = ctx.get("prereg_verdict_alt")
        parts.append(f'<p style="margin:4px 0;">Pre-registered tests: <b>'
                     f'{esc(ctx.get("prereg_verdict"))}</b>'
                     + (f' (as registered; excluding the hours known at registration: '
                        f'<b>{esc(va)}</b>)' if va and va != ctx.get("prereg_verdict") else "")
                     + ' -- '
                     + esc("; ".join(f"Test {t['id']} ({t['variant']}) {t['text']}"
                                     + (f" (after registration: {t['alt']['text']})"
                                        if t.get("alt") else "") for t in tests))
                     + ' (the section below).</p>')
    if ctx.get("week"):
        w = ctx["week"]
        wa = w.get("verdict_after_registration")
        parts.append(f'<p style="margin:4px 0;">Week verdict: <b>{esc(w["verdict"])}</b>'
                     + (f' (excluding the hours known at registration: <b>{esc(wa)}</b>)'
                        if wa and wa != w["verdict"] else "")
                     + ' (see the week summary).</p>')
    parts.append(_ranking_html(ctx))
    # 1b the pre-registered tests
    if tests:
        parts.append(h2("Pre-registered tests"))
        parts.append(_prereg_html(ctx))
    # 2 variant table
    parts.append(h2("Variants (the report day)"))
    if agg:
        fills = [f for f in FILLS if any(split_key(k)[1] == f for k in agg)]
        primary = fill if fill in fills else (fills or [None])[-1]
        top = ("<tr>" + f'<th style="{S_TH}"></th>'
               + "".join(f'<th colspan="{len(_fill_cols(f, agg, primary))}" style="{S_TH}'
                         f'text-align:center;">{esc(FILL_LABEL.get(f, f))}</th>' for f in fills)
               + "</tr>")
        head = ["variant"] + [c for f in fills for c in _fill_cols(f, agg, primary)]
        parts.append(table(head, _variant_rows(ctx, fills), top=top))
        parts.append(note("In the robust ranking's order (bold = the variant the breakdowns "
                          "follow); the report day's own nets rank nothing."))
        if "swp" in fills:
            dr = {a.get("draws") for k, a in agg.items() if split_key(k)[1] == "swp"}
            dr.discard(None)
            parts.append(note(SWP_NOTE.format(draws=(dr.pop() if len(dr) == 1 else "N"))
                              + (" " + swp_tables_text(ctx) if swp_tables_text(ctx) else "")))
        if DECISION_FILL in fills:
            parts.append(note(STRESS_NOTE + (" " + stress_text(ctx) if stress_text(ctx) else "")))
        if any(split_key(k)[1] in ("swp", DECISION_FILL) and a.get("net_cal") is not None
               for k, a in agg.items()):
            parts.append(note(CAL_NOTE))
        descs = [(n, ctx["policy"].desc(n)) for n in sorted({split_key(k)[0] for k in agg})]
        descs = [(n, d) for n, d in descs if d]
        if descs:
            parts.append(note("; ".join(f"{n}: {d}" for n, d in descs)))
    else:
        parts.append(note("No variants to show."))
    # 3 breakdowns
    parts.append(h2(f"Breakdown of {best}" if best else "Breakdown"))
    parts.append(_breakdowns(ctx) if best else note("No headline variant."))
    # 4 market
    parts.append(h2("Market structure"))
    parts.append(_market_html(ctx) if ctx["events"] else note("No events."))
    # 5 model
    parts.append(h2("Model"))
    parts.append(_model_html(ctx))
    # 6 health
    parts.append(h2("Data health"))
    parts.append(_health_html(ctx))
    # 7 suggestions
    parts.append(h2("Suggested tweaks"))
    parts.append(_suggestions_html(ctx))
    # 8 cumulative
    parts.append(h2("Cumulative to date"))
    parts.append(_cumulative_html(ctx))
    # 9 week
    if ctx.get("week"):
        parts.append(h2("Week summary"))
        parts.append(_week_html(ctx))
    parts.append(note("temp_shadow_report.py; spec TEMP_SHADOW.md. Stop the test: create "
                      "KL-data\\temp-shadow\\HALT and unregister the two 'KL temp-shadow' tasks."))
    parts.append("</div>")
    body = "".join(parts)
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Temp shadow {esc(day)}</title><style>"
            "body{background:#ffffff;color:#1f2328;font-family:-apple-system,'Segoe UI',"
            "Helvetica,Arial,sans-serif;font-size:14px;line-height:1.4;margin:0;padding:16px;}"
            "</style></head><body>" + body + "</body></html>")


def subject_line(ctx: dict) -> str:
    """The email subject: the robust ranking's verdict (never a single
    day's net; the named variant's noise-dominated flag with it), the
    pre-registered tests' status (Test A's "after registration" figure
    beside it), the problem flags, and from day 7 the week verdict. E.g.
    "Temp shadow day 3 (2026-10-17): no variant passes; Test A in progress
    1/5 (stress -$X; after registration -$Y), Test B starts 10/18"."""
    day, n = ctx["day"], ctx["day_n"]
    s = f"Temp shadow day {n} ({day}): "
    hl = ctx.get("headline") or {}
    if not ctx.get("events"):
        s += "no sim results"
        if hl.get("top"):
            s += "; " + headline_phrase(hl, subject=True)
    else:
        s += headline_phrase(hl, subject=True)
    tests = ctx.get("tests") or []
    if tests:
        s += "; " + ", ".join(test_subject(t) for t in tests)
    if ctx.get("sim_error"):
        s += " [SIM FAILED]"
    elif (ctx.get("sim_summary") or {}).get("errors"):
        s += f" [{len(ctx['sim_summary']['errors'])} sim errors]"
    w_err = ((ctx.get("sim_summary") or {}).get("window") or {}).get("errors") or []
    if w_err:
        s += f" [{len(w_err)} window re-sim errors]"
    if ctx.get("stale_sim"):
        s += f" [{sum(ctx['stale_sim'].values())} stale sim files]"
    sm = ctx.get("swp_missing") or {}
    if sm.get("total"):
        s += f" [{sm['total']} events without swp]"
    stm = ctx.get("stress_missing") or {}
    if stm.get("total"):
        s += f" [{stm['total']} events without stress]"
    if swp_rejections(ctx):
        s += " [swp table problem]"
    wdef = ((ctx.get("sim_summary") or {}).get("window") or {}).get("deferred") or []
    if wdef:
        s += f" [{len(wdef)} re-sims deferred]"
    if ctx.get("week"):
        w = ctx["week"]
        s += f" -- week: {w['verdict']}"
        va = w.get("verdict_after_registration")
        if va and va != w["verdict"]:
            s += f" (excluding known hours: {va})"
    return s


def _test_text(t: dict) -> List[str]:
    """A pre-registered test in plain text: status (both views for Test A),
    day nets, pin differences, missing hours, criteria."""
    a = t.get("alt")
    head = f"Test {t['id']} ({t['variant']}, ET {t['first_day']} .. {t['last_day']}): "
    if t["status"] == "not started":
        return [head + f"{t['text']}; PASS iff all of: "
                + "; ".join(t["crit"][c]["label"] for c in t["criteria"])]
    days = ", ".join(f"{_mmdd(r['day'])} {money(r['net'], 0)}"
                     + (f" ({r['known']} hours known at registration)" if r.get("known") else "")
                     for r in t["rows"] if not r["future"]) or "none yet"
    out = [head + ("as registered " if a else "") + t["text"]
           + (f"; excluding the {t['known_n']} hours known at registration (settled before "
              f"{_reg_label()}): {a['text']}" if a else "")
           + f"; stress net by day: {days}"]
    ft = _frozen_text(t)
    if ft:
        out.append("  - " + ft)
    if t["pin_diffs"]:
        out.append("  - INVALID (rule or model changed): "
                   + "; ".join(f"{d['field']} registered {_fmt_val(d['pinned'])}, found "
                               f"{_fmt_val(d['found'])} in {d['files']} file(s) (first "
                               f"{d['first']})" for d in t["pin_diffs"][:6])
                   + (f"; and {len(t['pin_diffs']) - 6} more" if len(t["pin_diffs"]) > 6
                      else ""))
    if t["missing"]:
        out.append("  - expected hours without a stress result: "
                   + "; ".join(f"{e} ({w})" for e, w in t["missing"][:8])
                   + (f"; and {len(t['missing']) - 8} more" if len(t["missing"]) > 8 else ""))
    if t["not_listed"]:
        out.append("  - not listed by Kalshi (not expected): " + ", ".join(t["not_listed"]))
    if t["known"]:
        out.append(f"  - known at registration ({len(t['known'])} hours): "
                   + ", ".join(f"{e.split('-', 1)[-1]} {money(n, 0)}" for e, n in t["known"]))
    for c in t["criteria"]:
        cr = t["crit"][c]
        s = f"  - {cr['label']}: {_crit_value(t, c)} -- {'met' if cr['met'] else 'not met'}"
        if a:
            s += (f" | excluding known hours: {_crit_value(a, c)} -- "
                  f"{'met' if a['crit'][c]['met'] else 'not met'}")
        out.append(s)
    fin = "" if t["status"] in ("PASS", "FAIL") else " so far"
    extra = []
    if "robust" in t.get("info", ()):
        extra.append(f"total without the best and worst hour "
                     f"{money(t['robust']) if t['robust'] is not None else 'n/a'}{fin}"
                     + (f" (excluding known hours "
                        f"{money(a['robust']) if a['robust'] is not None else 'n/a'})"
                        if a else ""))
    if "recent" in t.get("info", ()):
        extra.append(f"calibrated tails at the recent regime "
                     f"{money(t['recent']) if t.get('recent') is not None else 'n/a'}{fin} vs "
                     f"the table-based {money(t['cal']) if t.get('cal') is not None else 'n/a'}")
    if extra:
        out.append("  - for information: " + "; ".join(extra))
    g = t.get("fav_gate")
    if g and g["model_secs"]:
        out.append(f"  - agreement gate in the shadow: passed {pct(g['gate_secs'] / g['model_secs'])}"
                   f" of {g['model_secs']:,} model-favorite strike-seconds ({g['files']} files)")
    return out


def render_text(ctx: dict) -> str:
    lines = [subject_line(ctx), ""]
    if ctx.get("sim_error"):
        lines.append("THE SIM FAILED: " + ctx["sim_error"].strip().splitlines()[-1])
    errs = (ctx.get("sim_summary") or {}).get("errors") or []
    if errs:
        old = _failed_but_loaded(ctx)
        lines.append(f"{len(errs)} event(s) failed in the sim, e.g. {errs[0][0]}: {errs[0][1]}"
                     + (f" ({len(old)} still show an older sim file: "
                        + ", ".join(sorted(old)) + ")" if old else ""))
    w_err = ((ctx.get("sim_summary") or {}).get("window") or {}).get("errors") or []
    if w_err:
        lines.append(f"{len(w_err)} re-sim failure(s) on earlier window days (older output "
                     f"stays in the cumulative figures), e.g. {w_err[0][0]}: {w_err[0][1]}")
    sm = ctx.get("swp_missing") or {}
    if sm.get("total"):
        lines.append(f"{sm['total']} of {sm['events']} event(s) in the window have no swp "
                     "results (" + ", ".join(f"{d} x{n}" for d, n in sorted(sm["by_day"].items()))
                     + "): swp figures cover the other events"
                     + (f", e.g. {sm['day'][0][0]}: {sm['day'][0][1]}" if sm["day"] else "") + ".")
    stm = ctx.get("stress_missing") or {}
    if stm.get("total"):
        lines.append(f"{stm['total']} of {stm['events']} event(s) in the window have no stress "
                     "results (" + ", ".join(f"{d} x{n}" for d, n in sorted(stm["by_day"].items()))
                     + "): the ranking and the tests cover the other events"
                     + (f", e.g. {stm['day'][0][0]}: {stm['day'][0][1]}" if stm["day"] else "")
                     + ".")
    for r in swp_rejections(ctx):
        lines.append("SWP TABLE PROBLEM: " + r + " (the repo default table is used).")
    wdef = ((ctx.get("sim_summary") or {}).get("window") or {}).get("deferred") or []
    if wdef:
        lines.append(f"{len(wdef)} earlier-day event(s) not re-simmed within the time budget: "
                     "older files stay (flagged stale); the next report continues.")
    stale = ctx.get("stale_sim") or {}
    if stale:
        lines.append(f"{sum(stale.values())} sim file(s) in the window are from an older "
                     f"SIM_VERSION than {ctx.get('sim_version')} ("
                     + ", ".join(f"{d} x{n}" for d, n in sorted(stale.items()))
                     + "): the cumulative figures mix two grids; re-sim those days.")
    hl = ctx.get("headline") or {}
    rank = ctx.get("rank") or []
    if rank:
        win = ctx["win"]
        lines.append(f"Ranking on robust {rank[0]['fill']} totals over each variant's judged hours "
                     f"({win.days[0]} .. {win.days[-1]}; judged from: {judged_rule_text()}; each "
                     f"variant's best and worst event-hour left out; the headline needs robust > 0, "
                     f"P(total > 0) >= {HEADLINE_P_MIN:g} (a {BOOT_BLOCK}-hour block bootstrap) "
                     f"and {HEADLINE_MIN_DAYS} judged days): " + headline_phrase(hl) + ".")
        for r in [x for x in rank if x.get("judged")][:5]:
            lines.append(f"  {r['rank']}. {r['name']} (from {_judged_text(r)}, {r['hours']} hours"
                         + (f", {r['days']} judged day: not named before {HEADLINE_MIN_DAYS}"
                            if r.get("thin") else "") + "): "
                         f"robust {money(r['robust']) if r['robust'] is not None else 'n/a'}, total "
                         f"{money(r['total'])}, 90% CI {_ci_text(r)}, P(total > 0) "
                         f"{r['p_pos']:.2f}" + (" [noise-dominated]" if r["noise"] else "")
                         + (f", expected (calibrated tails) {money(r['cal'])}"
                            if r.get("cal") is not None else ""))
        nj = [f"{r['name']} (from {_judged_text(r)})" for r in rank if not r.get("judged")]
        if nj:
            lines.append("  not judged yet: " + ", ".join(nj))
        ins = [in_sample_text(r) for r in hl.get("in_sample") or []]
        if ins:
            lines.append("  in-sample hours, before each variant's first judged hour (shown for "
                         "information, never ranked): " + "; ".join(ins))
    else:
        lines.append("Headline: " + headline_phrase(hl) + ".")
        if not ctx.get("events"):
            lines.append(_empty_reason(ctx))
    tests = ctx.get("tests") or []
    if tests:
        va = ctx.get("prereg_verdict_alt")
        lines += ["", f"Pre-registered tests: {ctx.get('prereg_verdict')}"
                  + (f" (as registered; excluding the hours known at registration: {va})"
                     if va else "")]
        for t in tests:
            lines += _test_text(t)
        if any((t.get("policy") or {}).get("fav_market_min_p") is not None for t in tests):
            lines.append(GATE_RECON_NOTE)
    best, fill = ctx["best"], ctx["fill"]
    if best and f"{best}|{fill}" in ctx["agg"]:
        a = ctx["agg"][f"{best}|{fill}"]
        se = a.get("net_se")
        lines += ["", f"{best} on the report day ({fill}): reward {money(a['reward'])}, trading "
                      f"{money(a['trading_pnl'])}, net {money(a['net'])}"
                      + (f" +/- {usd(se)} (Monte Carlo se)" if se is not None else "")
                      + f" over {a['n_events']} events"
                      + (f"; expected at calibrated tails {money(a['net_cal'])} (an estimate)"
                         if a.get("net_cal") is not None and cal_ok(ctx.get("policy"), best)
                         else "")
                      + ("; no calibrated-tails expectation (it quotes live strikes: circular)"
                         if a.get("net_cal") is not None and not cal_ok(ctx.get("policy"), best)
                         else "") + "."]
    if not ctx.get("events") and rank:
        lines.append(_empty_reason(ctx))
    notes_ = []
    if "swp" in {split_key(k)[1] for k in ctx["agg"]} and swp_tables_text(ctx):
        notes_.append(swp_tables_text(ctx))
    if fill == DECISION_FILL and stress_text(ctx):
        notes_.append(stress_text(ctx))
    if notes_:
        lines += ["", "stress = swp + level-clears at every price + hidden favorite takers (the "
                      "decision model); swp = the sweep-aware fill model (sweepers take our 1c "
                      "orders; the tweak rules pair variants on it). " + " ".join(notes_)]
    act = [s for s in ctx["suggestions"] if not s["suppressed"]][:5]
    if act:
        lines.append("")
        lines.append("Suggested tweaks (swp pair diagnostics; only a pre-registered test makes "
                     "a variant eligible):")
        for s in act:
            lines.append(f"- {s['title']}: {s['change']}")
    if ctx.get("week"):
        w = ctx["week"]
        wa = w.get("verdict_after_registration")
        lines += ["", f"Week verdict: {w['verdict']}"
                  + (f" (excluding the hours known at registration: {wa})"
                     if wa and wa != w["verdict"] else "") + f" -- {w['reason']}"]
        if w.get("best") and w.get("pilot_config"):
            lines.append(f"Pilot config ({w['best']}, rule 4, 1/3 size): q_fav "
                         f"{_fmt_val(w['pilot_config'].get('q_fav'))} (the variant's "
                         f"{_fmt_val(w['config'].get('q_fav'))}); " + PILOT_RULES)
    lines += ["", "Full report attached (HTML)."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):
        return ""


def email_env() -> dict:
    env = dict(os.environ)
    for v in EMAIL_ENV:
        if not env.get(v):
            val = _env_from_registry(v)
            if val:
                env[v] = val
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def send_report(subject: str, path: str, text: str,
                runner: Optional[Callable[..., Any]] = None,
                sleeper: Optional[Callable[[float], Any]] = None,
                attempts: int = 3, delay: float = 120.0) -> bool:
    """send_html_email.py SUBJECT HTML TEXT with the same python; retried."""
    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    cmd = [sys.executable, SEND_SCRIPT, subject, path, text]
    for i in range(attempts):
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=300, env=email_env())
            if getattr(r, "returncode", 1) == 0:
                print(f"[{C.iso(C.utcnow())}] sent: {subject}")
                return True
            print(f"[{C.iso(C.utcnow())}] send attempt {i + 1} failed rc={r.returncode}: "
                  f"{(r.stderr or r.stdout or '').strip()[-400:]}")
        except Exception as e:  # noqa: BLE001
            print(f"[{C.iso(C.utcnow())}] send attempt {i + 1} raised {type(e).__name__}: {e}")
        if i + 1 < attempts:
            sleeper(delay)
    return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hourly-temp shadow daily report")
    ap.add_argument("--day", help="ET day YYYY-MM-DD (default: yesterday ET)")
    ap.add_argument("--no-sim", action="store_true", help="report existing sim output only")
    ap.add_argument("--send", action="store_true", help="email the report")
    ap.add_argument("--out", help="write the HTML here instead of reports/<day>.html")
    args = ap.parse_args(argv)
    day = args.day or default_day()
    _dt.date.fromisoformat(day)          # validate
    print(f"[{C.iso(C.utcnow())}] temp_shadow_report day={day} sim={not args.no_sim} "
          f"send={args.send}")
    sim_error, summary = None, None
    if not args.no_sim:
        sim_error, summary = run_sim(day)
        if sim_error:
            print(f"[{C.iso(C.utcnow())}] SIM FAILED:\n{sim_error}")
        else:
            ws = summary.get("window") or {}
            print(f"[{C.iso(C.utcnow())}] sim: {summary['ok']} ok, {len(summary['pending'])} "
                  f"pending, {len(summary['errors'])} failed, "
                  f"{len(summary.get('swp_errors') or [])} without swp (swp failed); earlier "
                  f"window days {len(ws.get('days') or [])}: {ws.get('ok', 0)} ok, "
                  f"{len(ws.get('pending') or [])} pending, {len(ws.get('errors') or [])} failed, "
                  f"{len(ws.get('deferred') or [])} deferred (budget "
                  f"{ws.get('budget_min')} min), "
                  f"{len(ws.get('swp_errors') or [])} without swp, "
                  f"{len(ws.get('stress_errors') or [])} without stress; report day "
                  f"{len(summary.get('stress_errors') or [])} without stress"
                  + (f"; swp_config: {summary['swp_config_error']}"
                     if summary.get("swp_config_error") else "")
                  + (f"; stress_config: {summary['stress_config_error']}"
                     if summary.get("stress_config_error") else ""))
    ctx = build_context(day, sim_error=sim_error, sim_ran=not args.no_sim,
                        sim_summary=summary, freeze=True)
    print(f"[{C.iso(C.utcnow())}] pre-registered tests: {ctx['prereg_verdict']}"
          + (f" (excluding the hours known at registration: {ctx['prereg_verdict_alt']})"
             if ctx.get("prereg_verdict_alt") else "") + " -- "
          + "; ".join(test_subject(t) for t in ctx["tests"]))
    page = render_html(ctx)
    path = args.out or os.path.join(C.report_dir(), f"{day}.html")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, path)
    print(path)
    # 1 = a sim problem: an event failed, or swp / stress failed for an event
    # (it kept the other fill models) or for the whole run (swp_config /
    # stress_config); deferred re-sims are not
    ws_ = (summary or {}).get("window") or {}
    rc = 1 if sim_error or (summary and (summary["errors"] or ws_.get("errors")
                                          or summary.get("swp_errors") or ws_.get("swp_errors")
                                          or summary.get("stress_errors")
                                          or ws_.get("stress_errors")
                                          or summary.get("swp_config_error")
                                          or summary.get("stress_config_error"))) else 0
    if rc == 0 and swp_rejections(ctx):      # a model/ override was rejected (repo table used)
        rc = 1
    if args.send:
        if not send_report(subject_line(ctx), path, render_text(ctx)):
            rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
