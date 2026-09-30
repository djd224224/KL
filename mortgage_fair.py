#!/usr/bin/env python3
"""Freddie Mac PMMS fair values for incentive_mm's long-dated mortgage gate.

Jack 2026-09-28, "yes" to: block the weekly KX30YMORTW, and build a fair-value
gate for KXFM30YMTG and KXMORTGAGERATE that stops quoting when its data goes
stale, starts from Freddie's latest weekly number and uses the weekly
market's own implied rate as a free live reading.

THE MARKETS (rules text read 2026-09-28; the gate models these two shapes and
nothing else):
  FINAL  KXFM30YMTG-<YY>EOY-T<K>: "... reported in Freddie Mac's final PMMS
         release published during <YEAR> is above <K>%" -- one print decides.
  MAX    KXMORTGAGERATE-<YYMMMDD>-T<K>: "... is above <K>% in any Primary
         Mortgage Market Survey (PMMS) release published in <YEAR>" -- any
         print of the year (the market closes early once one crosses).
Anything else in these series is an IN-YEAR touch market whose weekly prints
are live settlement events (KXFM30YMTG-26DEC31 "below 5.75% ... between
Issuance and Dec 31, 2026", KXMORTGAGERATE-26DEC "above 6.6% in 2026"): not
modelled -- entries carry an `err` and the gate stands them aside.

WHY NOT THE WEEKLY (blocked the same day): the Thursday print averages Thu-Wed
applications and the market opens Thursday 13:00 ET, 13 hours into that
window; strikes are 1bp apart against a median 6bp weekly move. Optimal
Blue's daily lock index (FRED OBMMIC30YF) pins the print to 4.1bp RMSE by
Friday, 2.7bp by Monday, 1.7bp by Wednesday (194 weeks), and the informed
side reads MBS live. Measured: -$55.90 on 242 open-scan contracts.

MODEL -- the weekly PMMS 30Y as a driftless Gaussian walk:
  X0      today's level: the MEDIAN print implied by the open KX30YMORTW
          ladder (its traders price rates live), dated at that event's
          Thursday; past the ladder's end (no mid crosses 50c) the edge
          strike's mid read through Phi^-1 at ANCHOR_EXTRAP_SD_BP (2026-09-30).
          Fallback, within PRINT_ONLY_HOURS of a release: the print itself
          (Kalshi's settled `expiration_value`, e.g. '7.03').
  sd(n)   the n-week sd, SD_A_BP * n ** SD_H: fit to 2000-2026 weekly PMMS
          changes, sd 22 / 41 / 58 / 85 / 98 bp at 4 / 13 / 26 / 52 / 65
          weeks -- ~1.2x the sqrt(n) scaling of the 9.6bp weekly sd (weekly
          changes are positively autocorrelated, lag-1 0.11), plus ANCHOR_SD
          for the anchor's own error.
  FINAL   P(X_T > K) = 1 - Phi((K + 0.005 - X0) / sqrt(sd(n)^2 + a^2)), n the
          weeks from the anchor's Thursday to the deciding release (the
          market's close date).
  MAX     P = P(X_F > K) + E[min(1, 2 (1 - Phi((K + 0.005 + shift - x)
          / sd(m)))) ; X_F <= K]: F the first release of the year, m the weeks
          F -> the last release (the close date); the reflection principle
          with the barrier raised by MAX_SHIFT_BP (15bp) for weekly
          monitoring. The plain Broadie-Glasserman-Kou shift (0.5826 x the
          weekly sd, ~6bp) read up to 7c high on the upper strikes against a
          sign-symmetrized 13-week block bootstrap of the 2000-2026 changes
          (autocorrelation kept, the sample's -7bp/13wk drift removed); 15bp
          is within ~3c of it on strikes 7.00-9.00 from three starts. FINAL
          is within ~3c of the same bootstrap as it stands.
  PMMS publishes two decimals and "above K" is strict, hence K + 0.005.

DATA -- Kalshi only, signed through incentive_mm's reader (public endpoint
as the fallback): the open KX30YMORTW ladder (anchor), its settled markets
(the last print) and the open family markets (rules, strike, close).
"""

import json
import math
import os
import re
import time
from statistics import NormalDist
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = timezone(timedelta(hours=-5))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ANCHOR_SERIES = os.environ.get("IMM_MORT_ANCHOR_SERIES", "KX30YMORTW")
SD_A_BP = _env_float("IMM_MORT_SD_A_BP", 10.5)       # sd(n) = A * n ** H (bp)
SD_H = _env_float("IMM_MORT_SD_H", 0.536)
ANCHOR_SD_BP = _env_float("IMM_MORT_ANCHOR_SD_BP", 3.0)
# the MAX barrier's weekly-monitoring shift (calibrated, see the docstring)
MAX_SHIFT_BP = _env_float("IMM_MORT_MAX_SHIFT_BP", 15.0)
# anchor quality: a strike counts when two-sided with a spread <= this, and
# the 50c crossing must fall between counted strikes at most GAP apart
ANCHOR_MAX_SPREAD_C = _env_float("IMM_MORT_ANCHOR_MAX_SPREAD_C", 20)
ANCHOR_MAX_GAP_BP = _env_float("IMM_MORT_ANCHOR_MAX_GAP_BP", 6)
# an anchor further than this from the last print is refused (a weekly move
# of 28bp is the largest since the 2022 method change)
ANCHOR_MAX_DEV_BP = _env_float("IMM_MORT_ANCHOR_MAX_DEV_BP", 30)
# PAST THE END OF THE LADDER (Jack 2026-09-30, "yes want that"): when rates
# run past Kalshi's last strike no mid crosses 50c (9/29: the OCT01 ladder
# topped out at T7.23 bid 85 / ask 88 and the gate stood the family aside
# for a day). The median is then read off the edge strike: m = K + s *
# Phi^-1(P(print > K)), s = ANCHOR_EXTRAP_SD_BP -- the spread of this week's
# print as the market sees it mid-week (a daily lock index pins it to 2-4bp
# RMSE by Monday; 9/29 T7.23 at 86.5c -> 7.26). Only while the edge strike's
# fitted mid is inside [100 - EDGE, EDGE] (at 95c it is at most 1.6 sd away;
# past that the ladder says only "higher") -- else fail closed as before.
ANCHOR_EXTRAP_SD_BP = _env_float("IMM_MORT_ANCHOR_EXTRAP_SD_BP", 3.0)
ANCHOR_EXTRAP_EDGE_C = _env_float("IMM_MORT_ANCHOR_EXTRAP_EDGE_C", 95.0)
ANCHOR_TTL_SECS = _env_float("IMM_MORT_ANCHOR_TTL_SECS", 1200)
PRINT_MAX_AGE_DAYS = _env_float("IMM_MORT_PRINT_MAX_AGE_D", 8)
PRINT_ONLY_HOURS = _env_float("IMM_MORT_PRINT_ONLY_H", 24)
META_REFRESH_SECS = _env_float("IMM_MORT_META_REFRESH_SECS", 900)
FAMILY_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_MORT_SERIES", "KXFM30YMTG,KXMORTGAGERATE").split(",") if s.strip())

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_TIMEOUT = 20.0

_FINAL_RE = re.compile(
    r"final PMMS release published during (\d{4}) is above (\d+(?:\.\d+)?)%")
_MAX_RE = re.compile(
    r"is above (\d+(?:\.\d+)?)% in any Primary Mortgage Market Survey "
    r"\(PMMS\) release published in (\d{4})")


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[mort-fair] {msg}", flush=True)


# ---------------------------------------------------------------- calendar

def first_thursday(year: int) -> date:
    d = date(year, 1, 1)
    return d + timedelta(days=(3 - d.weekday()) % 7)


def last_thursday(year: int) -> date:
    d = date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - 3) % 7)


def et_date(ts_utc: datetime) -> date:
    return ts_utc.astimezone(ET).date()


def release_utc(d: date) -> datetime:
    """A PMMS release instant: 12:00 ET on its day."""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET).astimezone(timezone.utc)


# ------------------------------------------------------------------- rules

def parse_rules(text: str) -> Optional[dict]:
    """{'kind': 'final' | 'max', 'year', 'k'} for the two modelled shapes,
    else None."""
    t = " ".join((text or "").split())
    m = _FINAL_RE.search(t)
    if m:
        return {"kind": "final", "year": int(m.group(1)), "k": float(m.group(2))}
    m = _MAX_RE.search(t)
    if m:
        return {"kind": "max", "year": int(m.group(2)), "k": float(m.group(1))}
    return None


# ------------------------------------------------------------------- model

def _phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def sd_weeks(n: float) -> float:
    """sd (percentage points) of the n-week change of the weekly print."""
    return 0.0 if n <= 0 else SD_A_BP * (n ** SD_H) / 100.0


def p_final(x0: float, n_weeks: float, k: float) -> float:
    s = math.hypot(sd_weeks(n_weeks), ANCHOR_SD_BP / 100.0)
    return 1.0 - _phi((k + 0.005 - x0) / s)


def p_max(x0: float, n1: float, m: float, k: float, steps: int = 240) -> float:
    """P(any weekly print from F to L is above K): n1 weeks anchor -> F, m
    weeks F -> L. The first print by its own normal law; below K there, the
    rest of the year by the reflection principle, barrier + MAX_SHIFT_BP."""
    thr = k + 0.005
    s1 = math.hypot(sd_weeks(n1), ANCHOR_SD_BP / 100.0)
    p = 1.0 - _phi((thr - x0) / s1)
    if m <= 0:
        return p
    sm = sd_weeks(m)
    barrier = thr + MAX_SHIFT_BP / 100.0
    lo = min(x0, thr) - 8.0 * s1
    h = (thr - lo) / steps
    acc = 0.0
    for i in range(steps + 1):
        x = lo + i * h
        dens = math.exp(-0.5 * ((x - x0) / s1) ** 2) / (s1 * math.sqrt(2 * math.pi))
        hit = min(1.0, 2.0 * (1.0 - _phi((barrier - x) / sm)))
        w = 0.5 if i in (0, steps) else 1.0
        acc += w * dens * hit
    return min(1.0, p + acc * h)


# ------------------------------------------------------------------ anchor

def _cents(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x * 100.0 if math.isfinite(x) else None


def _isotonic_decreasing(ys: List[float]) -> List[float]:
    """Pool-adjacent-violators: the closest non-increasing sequence."""
    blocks: List[List[float]] = []                  # [sum, count]
    for y in ys:
        blocks.append([y, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: List[float] = []
    for s, c in blocks:
        out.extend([s / c] * int(c))
    return out


def anchor_from_ladder(markets: List[dict]) -> Tuple[Optional[float], str]:
    """(median print, why-not) -- ladder_anchor without the detail."""
    d = ladder_anchor(markets)
    return d["x"], d["why"]


def ladder_anchor(markets: List[dict]) -> dict:
    """{"x": median print implied by one weekly event's ladder or None,
    "why": why-not, "extrap": None or {"k", "p", "side"}}. Each strike's mid
    is P(print > K); counted strikes are two-sided with a spread of at most
    ANCHOR_MAX_SPREAD_C; the mids are made non-increasing in K and the 50c
    crossing interpolated between neighbours at most ANCHOR_MAX_GAP_BP apart.
    A ladder that does not bracket 50c is read past its end strike while
    that strike's fitted mid is inside the ANCHOR_EXTRAP_EDGE_C band."""
    out = {"x": None, "why": "", "extrap": None}
    pts = []
    for m in markets:
        k = m.get("floor_strike")
        b, a = _cents(m.get("yes_bid_dollars")), _cents(m.get("yes_ask_dollars"))
        try:
            k = float(k)
        except (TypeError, ValueError):
            continue
        if b is None or a is None or b <= 0 or a >= 100 or a < b:
            continue
        if a - b > ANCHOR_MAX_SPREAD_C:
            continue
        pts.append((k, (a + b) / 2.0))
    if len(pts) < 2:
        return dict(out, why=f"{len(pts)} two-sided strikes")
    pts.sort()
    ks = [k for k, _ in pts]
    fit = _isotonic_decreasing([p for _, p in pts])
    for i in range(len(ks) - 1):
        if fit[i] >= 50.0 > fit[i + 1]:
            if (ks[i + 1] - ks[i]) * 100.0 > ANCHOR_MAX_GAP_BP + 1e-9:
                return dict(out, why=(f"50c crossing between {ks[i]:.2f} and "
                                      f"{ks[i + 1]:.2f} is too wide"))
            x = ks[i] + (fit[i] - 50.0) / (fit[i] - fit[i + 1]) * (ks[i + 1] - ks[i])
            return dict(out, x=round(x, 4))
    # no crossing: the print sits past one end of the ladder
    side, k, p = ("top", ks[-1], fit[-1]) if fit[-1] >= 50.0 else ("bottom", ks[0], fit[0])
    edge = ANCHOR_EXTRAP_EDGE_C
    if 100.0 - edge <= p <= edge:
        x = k + ANCHOR_EXTRAP_SD_BP / 100.0 * NormalDist().inv_cdf(p / 100.0)
        return dict(out, x=round(x, 4), extrap={"k": k, "p": round(p, 2), "side": side})
    return dict(out, why=(f"ladder {ks[0]:.2f}-{ks[-1]:.2f} does not bracket 50c "
                          f"(fitted {fit[0]:.0f}c..{fit[-1]:.0f}c) and its {side} strike "
                          f"is past the {edge:g}c extrapolation edge"))


# ------------------------------------------------------------------- reads

_signed_err: Optional[str] = None


def _read(get_json: Optional[Callable[[str, dict], dict]], path: str,
          params: dict) -> dict:
    """A Kalshi read: signed through `get_json` when given, else -- or when
    that fails -- the public endpoint (raises on a public failure)."""
    global _signed_err
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if not isinstance(js, dict):
                raise ValueError(f"signed read returned {type(js).__name__}")
            if _signed_err is not None:
                _log("signed Kalshi reads back")
                _signed_err = None
            return js
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
            if err != _signed_err:
                _log(f"! signed Kalshi read failed ({err}); reading the public endpoint")
            _signed_err = err
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(KALSHI_BASE + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1) ** 2)
    raise RuntimeError(f"Kalshi {path} failed: {last}")


def _markets(get_json, params: dict) -> List[dict]:
    out: List[dict] = []
    cursor = None
    for _ in range(10):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        js = _read(get_json, "/markets", p)
        out.extend(js.get("markets") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def _parse_ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def latest_print(settled: List[dict]) -> Optional[Tuple[float, date]]:
    """(value, release day) of the newest settled weekly market with a
    numeric expiration_value."""
    best = None
    for m in settled:
        ct = _parse_ts(m.get("close_time"))
        try:
            v = float(m.get("expiration_value"))
        except (TypeError, ValueError):
            continue
        if ct is None or not math.isfinite(v) or v <= 0:
            continue
        if best is None or ct > best[0]:
            best = (ct, v)
    if best is None:
        return None
    return best[1], et_date(best[0])


def open_anchor_event(open_markets: List[dict], now: datetime
                      ) -> Tuple[Optional[str], List[dict], Optional[date]]:
    """(event, its markets, its release day): the open weekly event closing
    soonest after `now`."""
    by_ev: Dict[str, List[dict]] = {}
    closes: Dict[str, datetime] = {}
    for m in open_markets:
        ct = _parse_ts(m.get("close_time"))
        ev = m.get("event_ticker") or ""
        if ct is None or ct <= now or not ev:
            continue
        by_ev.setdefault(ev, []).append(m)
        closes[ev] = min(ct, closes.get(ev, ct))
    if not by_ev:
        return None, [], None
    ev = min(by_ev, key=lambda e: closes[e])
    return ev, by_ev[ev], et_date(closes[ev])


# ------------------------------------------------------------------- watch

class MortgageWatch:
    """The refresher's state: the anchor every call, the last print and the
    family markets every META_REFRESH_SECS; `snap` is what the gate reads."""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]] = None):
        self.get_json = get_json
        self.meta_at = 0.0
        self.print_: Optional[Tuple[float, date]] = None
        self.family: List[dict] = []
        self.anchor: Optional[dict] = None           # last GOOD anchor
        self.snap: Optional[dict] = None

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        errors: List[str] = []
        if now_ts - self.meta_at >= META_REFRESH_SECS or not self.family:
            try:
                settled = _markets(self.get_json, {
                    "series_ticker": ANCHOR_SERIES, "status": "settled",
                    "min_close_ts": int(now_ts - 21 * 86400), "limit": 200})
                pr = latest_print(settled)
                if pr is not None:
                    self.print_ = pr
                fam: List[dict] = []
                for s in FAMILY_SERIES:
                    fam.extend(_markets(self.get_json, {
                        "series_ticker": s, "status": "open", "limit": 1000}))
                self.family = fam
                self.meta_at = now_ts
            except Exception as e:                  # noqa: BLE001
                errors.append(f"meta: {type(e).__name__}: {str(e)[:100]}")
        try:
            ladder = _markets(self.get_json, {"series_ticker": ANCHOR_SERIES,
                                              "status": "open", "limit": 200})
            ev, mk, day = open_anchor_event(ladder, now)
            if ev is None:
                why = "no open weekly event"
            else:
                d = ladder_anchor(mk)
                x, why = d["x"], d["why"]
                if x is not None:
                    self.anchor = {"x": x, "event": ev, "day": day.isoformat(),
                                   "ts": now_ts, "extrap": d["extrap"]}
            if why:
                errors.append(f"anchor: {why}")
        except Exception as e:                      # noqa: BLE001
            errors.append(f"anchor: {type(e).__name__}: {str(e)[:100]}")
        self.snap = build_snapshot(now_ts, self.anchor, self.print_,
                                   self.family, errors)
        return self.snap


def choose_x0(now_ts: float, anchor: Optional[dict],
              print_: Optional[Tuple[float, date]]
              ) -> Tuple[Optional[float], Optional[date], str]:
    """(X0, the Thursday it is dated at, source or why-not)."""
    now = datetime.fromtimestamp(now_ts, timezone.utc)
    if print_ is None:
        return None, None, "no settled weekly print"
    pv, pday = print_
    age_d = (et_date(now) - pday).days
    if age_d > PRINT_MAX_AGE_DAYS:
        return None, None, f"last print {pday} is {age_d} days old"
    if anchor is not None and now_ts - anchor["ts"] <= ANCHOR_TTL_SECS:
        dev = abs(anchor["x"] - pv) * 100.0
        aday = date.fromisoformat(anchor["day"])
        if dev > ANCHOR_MAX_DEV_BP:
            why = f"anchor {anchor['x']:.3f} is {dev:.0f}bp from the print {pv:.2f}"
        elif aday <= pday:
            why = f"anchor event {anchor['event']} is not after the print {pday}"
        else:
            ex = anchor.get("extrap")
            return anchor["x"], aday, (f"anchor {anchor['event']}"
                                       + (f" (past the {ex['side']} strike {ex['k']:.2f} at "
                                          f"{ex['p']:.1f}c)" if ex else ""))
    else:
        why = "no fresh anchor"
    if now - release_utc(pday) <= timedelta(hours=PRINT_ONLY_HOURS):
        return pv, pday, f"print {pday} ({why})"
    return None, None, why


def market_entry(m: dict, x0: Optional[float], x0_day: Optional[date]) -> dict:
    """One family market's gate entry: {'p', 'kind', 'year', 'k', ...} or
    {'err': why}."""
    t = m.get("ticker") or ""
    spec = parse_rules(m.get("rules_primary") or "")
    if spec is None:
        return {"err": "rules: not a modelled shape"}
    try:
        fk = float(m.get("floor_strike"))
    except (TypeError, ValueError):
        fk = None
    tk = t.rsplit("-", 1)[-1]
    try:
        tkv = float(tk[1:]) if tk.startswith("T") else None
    except ValueError:
        tkv = None
    if fk is None or abs(fk - spec["k"]) > 1e-6 or tkv is None \
            or abs(tkv - spec["k"]) > 1e-6:
        return {"err": f"strike mismatch: rules {spec['k']} floor {fk} ticker {tk}"}
    if (m.get("strike_type") or "greater") != "greater":
        return {"err": f"strike_type {m.get('strike_type')}"}
    ct = _parse_ts(m.get("close_time"))
    if ct is None:
        return {"err": "no close_time"}
    last = et_date(ct)
    e = {"kind": spec["kind"], "year": spec["year"], "k": spec["k"],
         "last": last.isoformat()}
    if last.year != spec["year"]:
        return dict(e, err=f"close {last} is not in {spec['year']}")
    if x0 is None:
        return dict(e, err="no rate level")
    if spec["kind"] == "final":
        n = (last - x0_day).days / 7.0
        if n < 1:
            return dict(e, err=f"the deciding print {last} is under a week out")
        e.update(n=round(n, 3), p=p_final(x0, n, spec["k"]))
    else:
        first = first_thursday(spec["year"])
        n1 = (first - x0_day).days / 7.0
        if n1 < 1:
            return dict(e, err=f"inside the counting year (first print {first})")
        mw = (last - first).days / 7.0
        e.update(n1=round(n1, 3), m=round(mw, 3), p=p_max(x0, n1, mw, spec["k"]))
    return e


def build_snapshot(now_ts: float, anchor: Optional[dict],
                   print_: Optional[Tuple[float, date]], family: List[dict],
                   errors: Optional[List[str]] = None) -> dict:
    x0, x0_day, src = choose_x0(now_ts, anchor, print_)
    entries = {}
    for m in family:
        t = m.get("ticker")
        if t:
            entries[t] = market_entry(m, x0, x0_day)
    return {
        "ts": now_ts,
        "fetched_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "x0": x0, "x0_day": x0_day.isoformat() if x0_day else None,
        "x0_src": src,
        "print": ({"value": print_[0], "day": print_[1].isoformat()}
                  if print_ else None),
        "anchor": anchor, "errors": list(errors or []),
        "markets": entries,
        "model": {"sd_a_bp": SD_A_BP, "sd_h": SD_H, "anchor_sd_bp": ANCHOR_SD_BP,
                  "max_shift_bp": MAX_SHIFT_BP,
                  "anchor_max_spread_c": ANCHOR_MAX_SPREAD_C,
                  "anchor_max_gap_bp": ANCHOR_MAX_GAP_BP,
                  "anchor_max_dev_bp": ANCHOR_MAX_DEV_BP,
                  "anchor_extrap_sd_bp": ANCHOR_EXTRAP_SD_BP,
                  "anchor_extrap_edge_c": ANCHOR_EXTRAP_EDGE_C,
                  "anchor_ttl_secs": ANCHOR_TTL_SECS,
                  "print_max_age_d": PRINT_MAX_AGE_DAYS,
                  "print_only_h": PRINT_ONLY_HOURS},
    }


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


if __name__ == "__main__":                          # a dry read, no orders
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from kalshi_reads import kalshi_get
        gj = lambda p, q: kalshi_get(p, q)          # noqa: E731
    except Exception:                               # pragma: no cover
        gj = None
    snap = MortgageWatch(gj).refresh()
    print(f"x0 {snap['x0']} ({snap['x0_src']}), print {snap['print']}, "
          f"errors {snap['errors']}")
    for t, e in sorted(snap["markets"].items()):
        print(f"  {t:32} " + (f"p {e['p'] * 100:5.1f}c" if "p" in e else e.get("err", "")))
