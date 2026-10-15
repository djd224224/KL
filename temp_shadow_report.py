#!/usr/bin/env python3
r"""temp_shadow_report.py -- the hourly-temp shadow test's DAILY REPORT
(TEMP_SHADOW.md "Daily report", "Tasks").

Jack 2026-10-14: "build it and shadow test for a week, reporting results and
suggestion tweaks daily". Every morning (task "KL temp-shadow report", 08:30
ET) this sims the previous ET day (temp_shadow_sim.run_day), reads every
sim/<ET day>/<EVENT>.json since the shadow start, and emails one HTML page:

  1 headline       "Day N of 7"; the best CONS variant by the day's net with
                   reward / trading / net for the day and per event-hour,
                   and the best OPT variant beside it
  2 variants       opt and cons side by side: reward, contracts, trading P&L,
                   net, avg share, fills within 3 s after a weather update
  3 breakdowns     the headline variant by ET hour of the event, strike class
                   (live / decided), tau bucket, side, and its worst 5 events
  4 market         % of strike-seconds paying without us, % locked at 99,
                   live / decided split, depth within 3c of the touch
  5 model          Brier / log loss of the fair vs the book mid by tau; the
                   index replica's exact-match rate on the day's finalized
                   minutes (temp_shadow_model.replica_index vs official v)
  6 data health    ws uptime, reconnects, gaps, book-trusted %, wx polls and
                   429s, our observation lag (rx - newest station
                   received_at for minutes that just became complete),
                   missing events with the likely reason
  7 suggestions    a rule engine over the variant pairs (cons fill model):
                   window B-A, back vs pull C-B, size D-B ($ per extra
                   contract of q_live), decided E-B and G alone, full F vs
                   best, guard B-H; the opt-cons fill-model gap; tau-bucket
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
  8 cumulative     per variant since day 1: sum, per-day mean, days positive
                   (the shadow's partial last day -- SHADOW_UNTIL falls inside
                   it -- counts as its share of 24 event-hours in per-day
                   figures and is left out of days positive)
  9 week summary   from day 7: GO / NO-GO / EXTEND for the best cons variant
                   over FULL days (GO = cumulative net > 0, >= 5 of 7 days
                   positive and a mean > $500/day; NO-GO = negative overall;
                   else EXTEND; the partial last day is shown beside it),
                   its params (temp_shadow_sim.VARIANTS over DEFAULT_POLICY)
                   as the starting config, and the key risks

Variant letters: the pair rules look variants up by their leading letter
(exact name "B", or "B" followed by a non-alphanumeric, e.g. "B_pull"); a
pair whose variant is absent is skipped and named in the report.

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
import datetime as _dt
import glob
import gzip
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import temp_shadow_common as C

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DAY1 = os.environ.get("TEMP_SHADOW_DAY1", "2026-10-15")   # day 1 of 7
WEEK_DAYS = 7
TAU_BUCKETS = ("0-5", "5-10", "10-20", "20-40", "40-60")
FILLS = ("cons", "opt")
HEADLINE_FILL = "cons"
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

# Week verdict.
GO_MEAN_USD = 500.0
GO_DAYS_POS = 5            # of 7 (scaled for a longer window)

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


def _new_agg() -> dict:
    a = {f: 0.0 for f in _SUM_FIELDS + _MAX_FIELDS}
    a.update({"n_events": 0, "unsettled": 0, "_sw": 0.0, "_ss": 0.0,
              "avg_share": None, "by_class": {}, "by_tau": {}, "by_side": {},
              "fills_after_wx": {"contracts": 0.0, "pnl": 0.0},
              "markouts": {}, "_mow": 0.0})
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

    def per_day(self, key: str, field: str = "net") -> float:
        n = self.eff_days(self.days_with(key))
        return self.total(key, field) / n if n else 0.0

    def best(self, fill: str) -> Optional[str]:
        names = self.names(fill)
        if not names:
            return None
        return max(names, key=lambda n: (self.total(f"{n}|{fill}"), n))


def best_variant(agg: Dict[str, dict], fill: str) -> Optional[str]:
    cands = [(a["net"], split_key(k)[0]) for k, a in agg.items() if split_key(k)[1] == fill]
    if not cands:
        return None
    return max(cands)[1]


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
)


def suggest(win: Window, today: str, policy: Policy, market: dict, model: dict,
            health: Optional[dict] = None) -> Tuple[List[dict], List[str]]:
    """(suggestions, notes). Suggestions are sorted: actionable first by $,
    then risks, ops, notes; suppressed ones are kept (rendered separately)."""
    out: List[dict] = []
    notes: List[str] = []
    fill = HEADLINE_FILL
    names = win.names(fill)
    L = {c: resolve_variant(c, names) for c in "ABCDEFGH"}
    k = lambda n: f"{n}|{fill}"   # noqa: E731
    missing = [c for c, n in L.items() if n is None]
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
    # 4 decided E - B, and G alone
    if L["B"] and (L["E"] or L["G"]):
        st_e = pair_stats(win, k(L["B"]), k(L["E"]), today) if L["E"] else None
        st_g = single_stats(win, k(L["G"]), today) if L["G"] else None
        parts = []
        if st_e:
            parts.append(evidence(_PAIR_RULES[3][3], st_e))
        if st_g:
            parts.append(evidence(f"{L['G']} alone (decided only)", st_g))
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
            src = L["E"] or L["G"]
            if d >= 0:
                out.append(_sugg("decided", "policy",
                                 f"Decided strikes alone earn {usd(d)}/day",
                                 ev, f"enable decided strikes ({policy.diff_text(L['B'], src)})",
                                 d, gate(st_g)))
            else:
                out.append(_sugg("decided", "policy",
                                 f"Decided strikes alone lose {usd(d)}/day: leave them off",
                                 ev, f"no change (not {policy.diff_text(L['B'], src)})",
                                 d, gate(st_g)))
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
    # 7 fill-model gap (opt - cons) on the best variant
    if best and f"{best}|opt" in win.by_key:
        st = pair_stats(win, k(best), f"{best}|opt", today)
        cons_pd = win.per_day(k(best))
        opt_pd = win.per_day(f"{best}|opt")
        gap = st["per_day"]
        q = _opt_num(policy.get(best, "q_live"))
        half = f"q_live {q:g} -> {q / 2:g}" if q else "half size"
        flips = (cons_pd > 0) != (opt_pd > 0) and abs(gap) > 0
        big = abs(gap) > FILL_GAP_FRAC * abs(cons_pd)
        why = [] if (flips or big) else [f"gap is under {FILL_GAP_FRAC:.0%} of the cons net"]
        why += [w for w in gate(st) if "SE" not in w]   # a gap is a range, not a test
        out.append(_sugg("fillgap", "risk",
                         f"Fill-model risk on {best}: opt {money(opt_pd)}/day vs cons "
                         f"{money(cons_pd)}/day",
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
    key = f"{best}|{HEADLINE_FILL}"
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
    key = f"{best}|{HEADLINE_FILL}"
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
    out.append(_sugg("hours", "policy",
                     f"{best} loses {usd(st['per_day'])}/day in ET hours "
                     f"{', '.join(f'{h:02d}' for h in bad)}",
                     evidence("losing hours", st) + f"; per hour/day {per}; each hour passed {rule}",
                     "halve size in those hours"
                     + (f" (q_live {q:g} -> {q / 2:g} there, an hour multiplier of 0.5)" if q else
                        " (an hour multiplier of 0.5)"),
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
        st = pair_stats(win, f"{L['A']}|{HEADLINE_FILL}", f"{L['B']}|{HEADLINE_FILL}", today)
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
    rew = win.per_day(f"{best}|{HEADLINE_FILL}", "reward") if best else 0.0
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
# Week summary
# --------------------------------------------------------------------------


def week_summary(win: Window, policy: Policy, model: dict,
                 today_health: Optional[dict] = None) -> Optional[dict]:
    fill = HEADLINE_FILL
    best = win.best(fill)
    if not best:
        return {"verdict": "EXTEND", "best": None,
                "reason": "no cons results in the window", "risks": [], "config": {}}
    key = f"{best}|{fill}"
    # the verdict is judged on full days: the shadow's partial last day
    # (SHADOW_UNTIL inside it) is reported beside it, not averaged in
    days = win.full_days_with(key) or win.days_with(key)
    part_days = [d for d in win.days_with(key) if d not in days]
    nets = [_num(win.day_aggs[d][key]["net"]) for d in days]
    total, n = sum(nets), len(nets)
    mean = total / n if n else 0.0
    pos = sum(1 for x in nets if x > 0)
    need = math.ceil(GO_DAYS_POS * n / WEEK_DAYS) if n else GO_DAYS_POS
    need = max(need, GO_DAYS_POS) if n <= WEEK_DAYS else need
    if total > 0 and pos >= need and mean > GO_MEAN_USD:
        verdict = "GO"
        reason = (f"{best} made {money(total)} over {n} days ({money(mean)}/day), "
                  f"positive on {pos} of {n}")
    elif total < 0:
        verdict = "NO-GO"
        reason = f"the best cons variant, {best}, lost {money(total)} over {n} days"
    else:
        verdict = "EXTEND"
        gaps = []
        if total <= 0:
            gaps.append("cumulative net is not positive")
        if pos < need:
            gaps.append(f"positive on {pos} of {n} days (GO needs {need})")
        if mean <= GO_MEAN_USD:
            gaps.append(f"mean {money(mean)}/day (GO needs > {money(GO_MEAN_USD)})")
        reason = f"{best} is positive but short of GO: " + "; ".join(gaps)
    partial = []
    for d in part_days:
        a = win.day_aggs[d][key]
        partial.append(f"{d} ({a['n_events']} events, {_fmt_days(24 * win.weights.get(d, 1.0))} "
                       f"of 24 event-hours before the shadow ended): {money(a['net'])}")
    if partial:
        reason += (f" (full days only; the partial last day {'; '.join(partial)} is not in "
                   "the verdict)")
    risks = []
    if f"{best}|opt" in win.agg:
        opt_t = win.total(f"{best}|opt")
        gap = opt_t - total
        rel = abs(gap) / abs(total) if total else None
        risks.append(f"Fill model: opt {money(opt_t)} vs cons {money(total)} "
                     f"(gap {money(gap)}" + (f", {rel:.0%} of cons" if rel is not None else "")
                     + "): the live queue position decides which is closer.")
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
    return {"verdict": verdict, "best": best, "reason": reason, "total": total,
            "mean": mean, "days": n, "pos": pos, "need": need, "partial_days": part_days,
            "config": policy.params(best), "overrides": dict(policy.variants.get(best) or {}),
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


def ws_health(t0: float, t1: float) -> dict:
    """Markers and per-minute presence of ws rows, without json-parsing the
    (large) message rows: rx is read off the line's prefix."""
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
    up, i = 0, 0
    for kmin in range(n_min):
        mid = t0 + 60 * kmin + 30
        while i < len(markers) and markers[i][0] <= mid:
            if markers[i][1] == "connect":
                state = True
            elif markers[i][1] == "disconnect":
                state = False
            i += 1
        # quiet books can skip a minute: data within +/- 1 min counts
        if state and (kmin in minutes or kmin - 1 in minutes or kmin + 1 in minutes):
            up += 1
    cnt = lambda kind: sum(1 for m in markers if m[1] == kind)   # noqa: E731
    disc = [m[2] for m in markers if m[1] == "disconnect" and m[2]]
    return {"rows": n, "minutes": n_min, "data_minutes": len(minutes),
            "uptime": (up / n_min) if n_min else None,
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
        (due if s is not None and s + 7200 <= now else later).append(ev)
    return due, later


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
    try:
        out["ws"] = ws_health(t0h, t1e)
    except Exception as e:  # noqa: BLE001
        out["ws"] = {"error": f"{type(e).__name__}: {e}"}
    n_min = max(int((t1e - t0h) // 60), 0)
    try:
        out["poll"], meta = poll_health(t0h, t1e, n_min)
    except Exception as e:  # noqa: BLE001
        out["poll"], meta = {"error": f"{type(e).__name__}: {e}"}, {"errors": {}, "events_seen": set()}
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


def run_sim(day: str) -> Tuple[Optional[str], dict]:
    """temp_shadow_sim.run_day(day) -> (error text or None, summary of its
    per-event results: {"ok", "pending": [events], "errors": [(event, err)]})."""
    summary: dict = {"ok": 0, "pending": [], "errors": []}
    try:
        import temp_shadow_sim as sim   # noqa: WPS433
        res = sim.run_day(day)
    except BaseException as e:          # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return ("".join(traceback.format_exception(type(e), e, e.__traceback__))[-3000:],
                summary)
    for r in res or []:
        if not isinstance(r, dict):
            continue
        if r.get("error"):
            summary["errors"].append((str(r.get("event")), str(r.get("error"))[:300]))
        elif r.get("pending"):
            summary["pending"].append(str(r.get("event")))
        else:
            summary["ok"] += 1
    return None, summary


def build_context(day: str, sim_error: Optional[str] = None, sim_ran: bool = False,
                  policy: Optional[Policy] = None, with_health: bool = True,
                  now: Optional[float] = None, sim_summary: Optional[dict] = None) -> dict:
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
    ctx = {"day": day, "day_n": day_number(day), "label": day_label(day),
           "generated": now, "sim_error": sim_error, "sim_ran": sim_ran,
           "events": events, "agg": agg, "market": market, "model": model,
           "coverage": cov, "health": health, "win": win, "policy": policy,
           "suggestions": sugg, "notes": notes, "sim_summary": sim_summary or {},
           "best_cons": best_variant(agg, "cons"), "best_opt": best_variant(agg, "opt"),
           "day0_events": 0}
    if day_number(day) >= 1:
        d0 = (_dt.date.fromisoformat(DAY1) - _dt.timedelta(days=1)).isoformat()
        ctx["day0_events"] = len(in_shadow(load_sim_day(d0), rec_start))
    ctx["week"] = (week_summary(win, policy, agg_model(win.all_events), health)
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


def _variant_rows(ctx: dict) -> List[List[Any]]:
    agg = ctx["agg"]
    names = sorted({split_key(k)[0] for k in agg},
                   key=lambda n: -(agg.get(f"{n}|cons") or agg.get(f"{n}|opt") or {}).get("net", 0))
    rows = []
    for n in names:
        row: List[Any] = [R(f"<b>{esc(n)}</b>" if n == ctx["best_cons"] else esc(n))]
        for f in FILLS:
            a = agg.get(f"{n}|{f}")
            if not a:
                row += ["-"] * 6
                continue
            faw = a["fills_after_wx"]
            row += [M(a["reward"]), qty(a["contracts"]), M(a["trading_pnl"]), M(a["net"]),
                    pct(a["avg_share"]),
                    R(f"{esc(qty(faw['contracts']))} / {m_html(faw['pnl'])}")]
        rows.append(row)
    return rows


def _breakdowns(ctx: dict) -> str:
    best = ctx["best_cons"]
    key = f"{best}|cons"
    a = ctx["agg"].get(key)
    if not a:
        return note("No cons results for the day.")
    out = [note(f"Variant {best} (cons fills).")]
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
    best = ctx["best_cons"]
    share = (ctx["agg"].get(f"{best}|cons") or {}).get("avg_share") if best else None
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
                     f"{pct(ws.get('uptime'))} of {ws.get('minutes', 0)} min; {ws.get('rows', 0):,} rows"])
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
                    f"|delta| >= {MIN_Z:g} SE (per-event spread). Pairs use the cons fill model "
                    "over the cumulative window."))
    for n in ctx.get("notes") or []:
        out.append(note(n))
    return "".join(out)


def _cumulative_html(ctx: dict) -> str:
    win: Window = ctx["win"]
    if not win.all_events:
        return note("No sim output in the window yet.")
    names = sorted({split_key(k)[0] for k in win.agg},
                   key=lambda n: -win.total(f"{n}|cons") if f"{n}|cons" in win.agg else 0)
    show_days = [d for d in win.days if win.days_events[d]][-9:]
    head = ["variant", "cons sum", "cons /day", "days +", "opt sum", "opt /day"]
    head += [(f"d{day_number(d)}" if day_number(d) >= 1 else d[5:])
             + ("*" if d in win.partial else "") for d in show_days]
    rows = []
    for n in names:
        row: List[Any] = [n]
        for f in FILLS:
            key = f"{n}|{f}"
            if key not in win.agg:
                row += ["-", "-"] + (["-"] if f == "cons" else [])
                continue
            dd = win.full_days_with(key) or win.days_with(key)
            row += [M(win.total(key)), M(win.per_day(key))]
            if f == "cons":
                row.append(f"{sum(1 for d in dd if win.day_aggs[d][key]['net'] > 0)}/{len(dd)}")
        for d in show_days:
            a = win.day_aggs[d].get(f"{n}|cons")
            row.append(M(a["net"], 0) if a else "-")
        rows.append(row)
    out = [table(head, rows)]
    part = ""
    for d in sorted(win.partial):
        part += (f" * {d} is partial (the shadow ended inside it; "
                 f"{_fmt_days(24 * win.weights.get(d, 1.0))} of 24 event-hours): its net is in "
                 "the sums, counts as that fraction of a day in /day, and is left out of "
                 "days + and the week verdict.")
    out.append(note(f"Window {win.days[0]} .. {win.days[-1]}; day columns are cons net."
                    + (f" Day 0 (partial, {ctx['day0_events']} events) is excluded."
                       if ctx.get("day0_events") else "") + part))
    return "".join(out)


def _week_html(ctx: dict) -> str:
    w = ctx["week"]
    color = {"GO": POS, "NO-GO": NEG}.get(w["verdict"], "#9a6700")
    out = [f'<p style="font-size:20px;margin:6px 0;"><b style="color:{color};">'
           f'{esc(w["verdict"])}</b></p>', note(w["reason"])]
    if w.get("best"):
        cfg, ov = w.get("config") or {}, w.get("overrides") or {}
        if cfg:
            out.append(h3(f"Recommended starting config ({w['best']}, DEFAULT_POLICY + overrides)"))
            out.append(table(["param", "value"],
                             [[R(f"<b>{esc(k)}</b>" if k in ov else esc(k)), _fmt_val(v)]
                              for k, v in sorted(cfg.items()) if not _skip_key(k)],
                             right=[False, False]))
            out.append(note("Bold = the variant's overrides."))
        else:
            out.append(note(f"Variant params unavailable ({w.get('policy_err') or 'not in VARIANTS'})."))
    out.append(h3("Key risks"))
    out.append('<ul style="padding-left:20px;margin:4px 0;">'
               + "".join(f"<li>{esc(r)}</li>" for r in w["risks"]) + "</ul>")
    return "".join(out)


def render_html(ctx: dict) -> str:
    day, agg = ctx["day"], ctx["agg"]
    best, bopt = ctx["best_cons"], ctx["best_opt"]
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
        parts.append(f'<div style="border:2px solid #9a6700;background:#fff8c5;padding:8px 10px;'
                     f'margin:8px 0;"><b style="color:#9a6700;">{len(ss["errors"])} event(s) '
                     f'failed in the sim</b> (left out of every figure below)'
                     + table(["event", "error"], [[e, err] for e, err in ss["errors"][:12]],
                             right=[False, False]) + "</div>")
    # 1 headline
    parts.append(h2("Headline"))
    if best:
        a = agg[f"{best}|cons"]
        n = max(a["n_events"], 1)
        desc = ctx["policy"].desc(best)
        parts.append(
            f'<p style="font-size:15px;margin:4px 0;">Best cons variant <b>{esc(best)}</b>'
            + (f' <span style="color:{MUTED};">({esc(desc)})</span>' if desc else "")
            + f': net <b>{m_html(a["net"])}</b> = reward {m_html(a["reward"])} + trading '
              f'{m_html(a["trading_pnl"])}'
            + (f' - fees {m_html(a["fees"])}' if abs(a["fees"]) > 0.005 else "")
            + f'.<br>Per event-hour ({a["n_events"]}): net {m_html(a["net"] / n)}, reward '
              f'{m_html(a["reward"] / n)}, trading {m_html(a["trading_pnl"] / n)}.</p>')
        if bopt and f"{bopt}|opt" in agg:
            o = agg[f"{bopt}|opt"]
            parts.append(note(f"Best opt variant {bopt}: net {money(o['net'])} (reward "
                              f"{money(o['reward'])}, trading {money(o['trading_pnl'])}). "
                              "opt = we fill at an equal price ahead of the queue; cons = the "
                              "queue ahead of us fills first."))
        if a["unsettled"]:
            parts.append(note(f"{a['unsettled']} event(s) not settled: their trading P&L is incomplete."))
    else:
        parts.append(f'<p style="font-size:15px;margin:4px 0;"><b>No sim results for {esc(day)}.</b> '
                     f'{esc(_empty_reason(ctx))}</p>')
    if ctx.get("week"):
        w = ctx["week"]
        parts.append(f'<p style="margin:4px 0;">Week verdict: <b>{esc(w["verdict"])}</b> '
                     f'(see the week summary).</p>')
    # 2 variant table
    parts.append(h2("Variants"))
    if agg:
        top = ("<tr>" + f'<th style="{S_TH}"></th>'
               + "".join(f'<th colspan="6" style="{S_TH}text-align:center;">{f}</th>' for f in FILLS)
               + "</tr>")
        sub = ["reward", "contracts", "trading", "net", "avg share", "after-wx ct / $"]
        parts.append(table(["variant"] + sub + sub, _variant_rows(ctx), top=top))
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
    day, n = ctx["day"], ctx["day_n"]
    best = ctx["best_cons"]
    if best:
        a = ctx["agg"][f"{best}|cons"]
        s = (f"Temp shadow day {n} ({day}): best {best} net {money(a['net'], 0)}/day "
             f"(reward {money(a['reward'], 0)}, fills {money(a['trading_pnl'], 0)})")
    else:
        s = f"Temp shadow day {n} ({day}): no sim results"
    if ctx.get("sim_error"):
        s += " [SIM FAILED]"
    elif (ctx.get("sim_summary") or {}).get("errors"):
        s += f" [{len(ctx['sim_summary']['errors'])} sim errors]"
    if ctx.get("week"):
        s += f" -- week: {ctx['week']['verdict']}"
    return s


def render_text(ctx: dict) -> str:
    lines = [subject_line(ctx), ""]
    if ctx.get("sim_error"):
        lines.append("THE SIM FAILED: " + ctx["sim_error"].strip().splitlines()[-1])
    errs = (ctx.get("sim_summary") or {}).get("errors") or []
    if errs:
        lines.append(f"{len(errs)} event(s) failed in the sim, e.g. {errs[0][0]}: {errs[0][1]}")
    best = ctx["best_cons"]
    if best:
        a = ctx["agg"][f"{best}|cons"]
        lines.append(f"{best} (cons): reward {money(a['reward'])}, trading {money(a['trading_pnl'])}, "
                     f"net {money(a['net'])} over {a['n_events']} events.")
    else:
        lines.append(_empty_reason(ctx))
    act = [s for s in ctx["suggestions"] if not s["suppressed"]][:5]
    if act:
        lines.append("")
        lines.append("Suggested tweaks:")
        for s in act:
            lines.append(f"- {s['title']}: {s['change']}")
    if ctx.get("week"):
        lines += ["", f"Week verdict: {ctx['week']['verdict']} -- {ctx['week']['reason']}"]
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
            print(f"[{C.iso(C.utcnow())}] sim: {summary['ok']} ok, {len(summary['pending'])} "
                  f"pending, {len(summary['errors'])} failed")
    ctx = build_context(day, sim_error=sim_error, sim_ran=not args.no_sim,
                        sim_summary=summary)
    page = render_html(ctx)
    path = args.out or os.path.join(C.report_dir(), f"{day}.html")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, path)
    print(path)
    rc = 1 if sim_error or (summary and summary["errors"]) else 0
    if args.send:
        if not send_report(subject_line(ctx), path, render_text(ctx)):
            rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
