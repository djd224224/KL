#!/usr/bin/env python3
"""Index replica + fair-value model for the hourly-temp shadow test.

TEMP_SHADOW.md "Model" and "Interfaces" are the contract. KXTEMPMIAH settles
on Kalshi's Weather Index for Miami at minute S (the event's close).

Replica (Kalshi Weather Index methodology v1.0, Appendix B)
-----------------------------------------------------------
  x_i = station i's reading in C (exact: (temp_f - 32) / 1.8; HF-ASOS readings
        are whole C, METAR fallbacks tenths of C)
  z_i = x_i - b_i + B_c          (b_i = station offset, B_c = city reference)
  index_C = sum(w_i z_i) / sum(w_i) over the stations present (w_i = 0.2)
  needs >= 4 stations AND a present weight share >= 0.8; published in F,
  rounded to 0.01. With all five present the offsets cancel (B_c = mean b_i)
  and the index = 32 + 0.36 * (sum of the five whole-C readings).
  Calibrations (offsets) change weekly; the one whose effective_at_ms <= the
  minute applies. Sources, first found: <DATA>/backfill/calibrations.json,
  <DATA>/backfill/study/agentC/kalshi_wx_calibrations.json, built-in v1.0
  defaults (KOPF1M -0.5 C, others 0, B_c -0.10 C).
  Stations counted: code "ok" or "pending" with a temp_f. "late" (received
  after the 300 s deadline) and "missing" are excluded - this reproduces the
  official v on 10,094 / 10,094 study minutes (9,750 normal, 344 degraded).

Tape and snapshots (look-ahead safety)
--------------------------------------
  WxTape(rows) takes wx stream rows (temp_shadow_common shapes). A row is the
  whole minute record as our poll saw it at row["rx"]; snapshot(at_rx) uses
  only rows with rx <= at_rx. For each minute the newest such row defines the
  known stations; the official v (status normal/degraded) replaces the
  replica once a row carries it.
  WxTape.from_detailed(...) builds a tape from historical
  /live_data/weather/miami?detailed=true responses, which have no rx. It
  synthesizes the rows a 1 s poller would have seen:
    station rx  = received_at_ms / 1000 + 1.0 s     (SYN_RX_LAG_S)
                  no received_at_ms -> t + 150 s     (SYN_NO_RECV_S)
                  METAR fallback (source metar / primary_code set): not before
                  the 300 s deadline -> max(rx, t + 301 s); its received_at_ms
                  is the METAR's own receipt, often 20+ minutes earlier
    official v  = t + 311 s (Kalshi publishes ~305-317 s after the minute),
                  never before the last counted station
  Pending rows carry code "pending" (a "late" station keeps "late").

Fair value
----------
  tau = (S - latest_minute) in minutes; station data for minute m lands
  ~2.4-3.5 min after m, so tau at the close is ~3.
  I0  = five-station estimate at the latest minute (a station not in yet,
        usually KFLL, is filled with its own newest reading <= 5 min old;
        falls back to the replica)
  mu  = I0 + a(tau)*drift(S, tau) + b(tau)*trend15*tau + c(tau)*dev
           + e(tau)*anom
        drift = climatological mean change over (S - tau, S] from an EST-hour x
                month table (IEM 1-minute history), interpolated across hours
                and months
        trend15 = OLS slope (F/min) of the last 15 min of history
        dev = (that line's value at the latest minute) - I0: the index is
              quantized to 0.36 F, so a fresh step up sits above the line and
              tends to give some back
        anom = realized change over the last >= 45 min minus its climatological
               drift (0 with less history; heavily ridged, weak so far)
        Every feature (trend, dev, anom, rv, rv20 below) reads ONE series: I0
        at the latest minute plus the final / five-station history minutes.
        A pending four-station minute (KFLL lags ~97% of the time) is a
        replica of another station set; its step to a five-station neighbour
        is composition, not temperature.
        Default / ridge-prior coefficients DEFAULT_MEAN_COEFS (a, b, c, e) =
        (0.75, 0.04, 0, 0): a and b from one joint regression (agentD).
  sigma = sigma_core(tau, hour bucket) * hour_mult[EST hour] * season_mult
          * vol_mult    (hour_mult: within-bucket diurnal shape from IEM, mean 1
          per bucket, kept by `fit` only if it raises the likelihood)
        vol_mult = clip(((rv60 + eps) / (rv_ref + eps)) ** vol_gamma
                        * ((rv20 + eps) / (rv20_ref + eps)) ** (vol_gamma_short
                                                               * max(0, 1 - tau/15)))
        rv60 = rms of 5-minute index changes over the last hour (rain cells,
        fronts), rv20 = rms of 1-minute changes over the last 20 minutes (a
        station dithering on a whole-C boundary; matters at short tau only)
  P(index_S > k.99) = P(X >= index_from_sum(sum_threshold(k)) - 0.18), X ~
  (1 - w) N(mu, s) + w N(mu, k s) (fat tails; continuity-corrected onto the
  0.36 F lattice). Once the settlement minute is known with all five stations
  (or the official v) the answer is exactly 0/1. dist() returns (mu, s): s is
  the mixture's CORE sd; density() / slopes() give the mixture's pdf and the
  price slope (cents per 0.36 F) for consumers that need one.
  Basis lattice: when the estimate is a four-station replica (a station out
  >= 10 min) or carries a METAR fallback (tenths of C), the index moves on a
  different lattice (0.45 F, or 0.36 F shifted). P is then the mixture
  q * P(cut on that lattice) + (1 - q) * P(five-station cut), q = p0 *
  exp(-tau / T) = how often S still shares it (LATTICE_KEEP, 6-week backfill).
  Hours are EST (UTC-5, no DST) so the diurnal tables stay solar-aligned all
  year; buckets night 0-6, morning 7-11, afternoon 12-17, evening 18-23 EST.

CLI
---
  python temp_shadow_model.py fit [--until YYYY-MM-DD] [--wx-dir D] [--iem CSV]
  python temp_shadow_model.py replica-check [--dir DIR | --day YYYY-MM-DD]
  python temp_shadow_model.py eval [--split YYYY-MM-DD] [--wx-dir D]

  fit   writes <DATA>/model/model_params.json: drift(hour, month) from the IEM
        1-minute history, sigma/a/b/c/tails/vol from the Kalshi station history
        (<DATA>/backfill/wx, else the study chunks). Fit targets every 2 min
        overlap (the change over (S - tau, S] of neighbours shares most of its
        path), so every weight uses EFFECTIVE samples n_eff = n * min(1,
        step/knot): each (knot, EST bucket) sigma is shrunk toward the knot's
        pooled Kalshi MLE by n_eff / (n_eff + 200); the mean-coefficient ridge
        weighs 10 effective pseudo-samples; the tail / vol / hour-shape
        choices maximize the n_eff-weighted likelihood. Re-runnable; --until
        for time splits.
  eval  time-split calibration backtest on settled events (<DATA>/settled,
        <DATA>/backfill/settled, else the study markets + trades): fits in
        memory on data before --split, scores events on/after it at wall-clock
        tau in (60, 57, 45, 30, 15, 10, 5, 3) min (57 because nothing trades
        before the open at 60) against the market's last REST trade before
        that time: Brier / log loss, near strikes (|strike - mu| < 1.5 F), a
        50/50 log-odds blend (below the market = the model adds information),
        calibration, z_rms by EST bucket x tau, and event-clustered bootstrap
        CIs of (model - market) and (blend - market) - the strike-rows of an
        event are not independent, ~100 events is a small sample; writes
        <DATA>/model/eval_<ET date>.json. The default split leaves >= 3 days
        to fit on.
Never places orders; no network calls.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as _dt
import glob
import json
import math
import os
import sys
import time
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import temp_shadow_common as C

CITY = "miami"
STATIONS: Tuple[str, ...] = tuple(C.STATIONS[CITY])
VALID_CODES = frozenset(("ok", "pending"))
# codes whose temp_f is a real reading (late = after the 300 s deadline:
# excluded from the official index, still a valid temperature for the fill)
READING_CODES = frozenset(("ok", "pending", "late"))
FINAL_STATUSES = frozenset(("normal", "degraded"))
PLAUSIBLE_F = (20.0, 120.0)

SYN_RX_LAG_S = 1.0
SYN_NO_RECV_S = 150.0
SYN_DEADLINE_S = 300.0
SYN_OFFICIAL_S = 311.0

HALF_STEP_F = C.STEP_F / 2.0          # 0.18 F continuity correction
EST_OFFSET_S = 5 * 3600

TAU_KNOTS = [1, 2, 3, 5, 7, 10, 15, 20, 30, 45, 60]
# Total std of the index change by tau (F), IEM-based (study agentD); 7 interpolated.
DEFAULT_SIGMA_TOTAL = [0.23, 0.27, 0.30, 0.34, 0.37, 0.42, 0.50, 0.57, 0.70, 0.90, 1.11]
BUCKETS = (("night", 0, 6), ("morning", 7, 11), ("afternoon", 12, 17), ("evening", 18, 23))
BUCKET_NAMES = tuple(b[0] for b in BUCKETS)
DEFAULT_BUCKET_MULT = {"night": 0.55, "morning": 1.10, "afternoon": 1.45, "evening": 1.05}
SEASONS = ("JJAS", "ON", "DJFMAM")

# Mean 60-min index change (F/hour) by EST hour, per month: IEM 1-minute
# five-station history Jun 2025 - Oct 2026 (study agentD CSV), same-station-set
# 1-minute differences, shrunk toward the all-month hour mean (n0=200 per
# cell), daily closure (each month's hours sum to 0). Regenerate with `fit`.
DEFAULT_DRIFT60 = {
    "1": [-0.65, -0.57, -0.33, -0.12, -0.21, 0.1, 0.48, 1.62, 2.82, 2.6, 1.82, 1.35, 0.52, 0.48, -1.25, -0.53, -1.07, -1.43, -1.22, -0.98, -0.81, -0.93, -1.01, -0.69],
    "2": [-0.72, -0.99, -0.66, -0.66, -0.36, -0.58, 0.8, 3.56, 4.22, 3.06, 1.55, 1.29, 0.2, 0.19, -0.53, -0.53, -1.45, -2.22, -1.6, -1.06, -0.74, -0.98, -1.1, -0.69],
    "3": [-0.19, -0.69, -0.58, -0.56, -0.8, -0.2, 0.88, 2.81, 2.82, 1.92, 1.09, 1.11, -0.23, -0.01, -0.35, -0.84, -0.87, -1.41, -1.03, -0.47, -0.47, -0.77, -0.66, -0.51],
    "4": [-0.59, -0.21, -0.33, -0.26, -0.51, 0.01, 1.82, 2.07, 1.8, 1.25, 1.15, 0.83, 0.23, -0.41, -0.12, -1.22, -1.51, -1.05, -0.98, -0.41, -0.14, -0.61, -0.36, -0.47],
    "5": [-0.51, -0.42, -0.65, -0.33, -0.34, 0.15, 1.87, 2.16, 1.85, 1.55, 1.44, 0.69, 0.22, -0.19, -0.51, -0.76, -1.23, -1.29, -1.23, -0.66, -0.47, -0.53, -0.33, -0.49],
    "6": [-0.2, -0.26, -0.29, -0.33, -0.16, 0.41, 2.19, 1.82, 1.44, 0.94, 0.78, 0.52, 0.02, -0.14, -0.17, -0.77, -1.1, -1.56, -1.15, -0.84, -0.34, -0.3, -0.24, -0.3],
    "7": [-0.34, -0.42, -0.4, -0.23, -0.13, 0.23, 1.93, 2.33, 1.82, 1.1, 0.84, 0.36, 0.11, -0.12, -0.57, -0.6, -1.18, -1.26, -1.28, -0.7, -0.27, -0.46, -0.46, -0.3],
    "8": [-0.39, -0.47, -0.24, -0.21, -0.22, -0.01, 1.97, 2.52, 1.85, 1.68, 0.83, 0.08, -0.13, -0.7, -0.15, -0.83, -1.19, -1.36, -1.04, -0.67, -0.26, -0.31, -0.38, -0.35],
    "9": [-0.15, -0.38, -0.33, -0.22, -0.12, 0.01, 1.16, 2.45, 2.36, 2.02, 0.92, 0.01, -0.72, -1.03, -1.02, -0.29, -0.63, -1.37, -0.89, -0.45, -0.33, -0.38, -0.36, -0.27],
    "10": [-0.33, -0.39, -0.54, -0.26, -0.12, -0.07, 0.85, 2.4, 2.26, 1.55, 1.04, 0.51, -0.28, 0.18, -0.8, -0.71, -1.12, -1.31, -0.71, -0.52, -0.52, -0.21, -0.66, -0.23],
    "11": [-0.68, -0.56, -0.37, -0.45, -0.39, -0.04, 0.06, 3.3, 3.12, 2.75, 1.29, 0.84, 0.27, -0.11, -0.83, -0.76, -1.36, -1.46, -0.86, -0.74, -0.86, -0.79, -0.77, -0.58],
    "12": [-0.65, -0.55, -0.37, -0.32, -0.37, -0.08, 0.39, 2.08, 2.94, 2.4, 1.84, 0.63, 0.53, -0.01, -0.45, -0.59, -1.58, -1.63, -1.06, -0.82, -0.74, -0.84, -0.5, -0.23],
}

FIT_KNOTS = [3, 5, 7, 10, 15, 20, 30, 45, 60]
FIT_LAGS = [0, 2, 4, 7, 12, 17, 27, 42, 57]          # wall-clock lag (min) per FIT_KNOTS entry
EVAL_TAUS = (60, 57, 45, 30, 15, 10, 5, 3)

# Default / ridge-prior mean coefficients (drift, trend15*tau, dev, anom). a and
# b come from ONE joint regression (study agentD analyze.py, predictor_coefs_
# train.csv: b_clim ~0.75 with b_trend ~0.02-0.05). The trend-only coefficient
# (alpha_trend15 ~0.28) must not be paired with a=0.75: both carry the diurnal
# warming, so together they double-count it.
DEFAULT_MEAN_COEFS = (0.75, 0.04, 0.0, 0.0)


def n_eff_factor(knot: float, step_min: float) -> float:
    """Effective / nominal sample count for fit targets step_min minutes apart
    at horizon knot: the index changes over (S - knot, S] of neighbouring
    targets overlap, so their residuals are autocorrelated (~0.95 at a 2-min
    lag for knot 60); the integrated autocorrelation time of a random-walk
    increment is ~knot/step samples."""
    return min(1.0, float(step_min) / max(float(knot), 1e-9))


# --------------------------------------------------------------------------
# Time helpers (EST = UTC-5 all year: solar-aligned diurnal tables)
# --------------------------------------------------------------------------

def est_dt(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts - EST_OFFSET_S, C.UTC).replace(tzinfo=None)


def est_hour(ts: float) -> int:
    return est_dt(ts).hour


def est_month(ts: float) -> int:
    return est_dt(ts).month


def est_minute_of_day(ts: float) -> float:
    d = est_dt(ts)
    return d.hour * 60 + d.minute + (d.second + d.microsecond / 1e6) / 60.0


def bucket_of_hour(h: int) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= h <= hi:
            return name
    return "evening"


def season_of_month(m: int) -> str:
    if 6 <= m <= 9:
        return "JJAS"
    if m in (10, 11):
        return "ON"
    return "DJFMAM"


def _ncdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


# --------------------------------------------------------------------------
# Calibrations + replica
# --------------------------------------------------------------------------

Cal = namedtuple("Cal", "config_version effective_at_ms b_city_c offsets weights")

DEFAULT_CAL = Cal("miami-temperature-v1.0-default", 0, -0.10,
                  {s: (-0.5 if s == "KOPF1M" else 0.0) for s in STATIONS},
                  {s: 0.2 for s in STATIONS})


def calibration_paths() -> List[str]:
    return [os.path.join(C.DATA_ROOT, "backfill", "calibrations.json"),
            os.path.join(C.DATA_ROOT, "backfill", "study", "agentC",
                         "kalshi_wx_calibrations.json")]


def parse_calibrations(obj) -> List[Cal]:
    """Calibrations endpoint body (or its list / a single entry) -> [Cal]."""
    if isinstance(obj, dict):
        if "calibrations" in obj:
            obj = obj["calibrations"]
        elif "resp" in obj:
            return parse_calibrations(obj["resp"])
        elif "stations" in obj:
            obj = [obj]
    if not isinstance(obj, list):
        return []
    out: List[Cal] = []
    for c in obj:
        try:
            offs, wts = {}, {}
            for s in c["stations"]:
                sid = s["station_id"]
                offs[sid] = float(s.get("offset_c") or 0.0)
                w = s.get("weight")
                wts[sid] = float(w) if w is not None else 0.2
            out.append(Cal(str(c.get("config_version") or ""),
                           int(c.get("effective_at_ms") or 0),
                           float(c.get("city_reference_c") or 0.0), offs, wts))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda k: k.effective_at_ms)
    return out


class Calibrations:
    """Time-ordered calibrations; for_minute(t_ms) = the one effective at t."""

    def __init__(self, cals: Optional[Sequence[Cal]] = None, source: str = "given"):
        cals = list(cals or [])
        if not cals:
            cals, source = [DEFAULT_CAL], "defaults"
        cals.sort(key=lambda k: k.effective_at_ms)
        self.cals = cals
        self._eff = [c.effective_at_ms for c in cals]
        self.source = source

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Calibrations":
        for p in ([path] if path else calibration_paths()):
            if p and os.path.exists(p):
                cals = parse_calibrations(C.read_json(p))
                if cals:
                    return cls(cals, source=p)
        return cls(None)

    def for_minute(self, t_ms: int) -> Cal:
        i = bisect.bisect_right(self._eff, t_ms) - 1
        return self.cals[max(i, 0)]

    def by_version(self, config_version: str) -> Optional[Cal]:
        for c in self.cals:
            if c.config_version == config_version:
                return c
        return None


def replica_index(stations_f: Dict[str, float], offsets_c: Dict[str, float],
                  b_city_c: float, weights: Optional[Dict[str, float]] = None,
                  min_stations: int = 4, min_weight_share: float = 0.8
                  ) -> Optional[float]:
    """Methodology v1.0 App. B index (F, 0.01) or None (< 4 stations or
    present weight share < 0.8). Stations outside the configured set (the
    offsets' keys, or the five Miami stations when offsets is empty) are
    ignored."""
    cfg = list(offsets_c.keys()) if offsets_c else list(STATIONS)
    weights = weights or {}
    w_all = sum(weights.get(s, 0.2) for s in cfg)
    num = den = 0.0
    n = 0
    for sid, f in stations_f.items():
        if f is None or sid not in cfg:
            continue
        w = weights.get(sid, 0.2)
        x = (float(f) - 32.0) / 1.8
        num += w * (x - offsets_c.get(sid, 0.0) + b_city_c)
        den += w
        n += 1
    if n < min_stations or den <= 0 or den < min_weight_share * w_all - 1e-9:
        return None
    return round(num / den * 1.8 + 32.0, 2)


def plausible_f(f) -> bool:
    """Physically possible Miami reading. QC-failed readings carry codes like
    "range" / "provider_qc" with temp_f -77.8 (Sep 1 2026); this guards the
    replica and the fill even if such a reading ever shows up as pending."""
    return f is not None and PLAUSIBLE_F[0] <= float(f) <= PLAUSIBLE_F[1]


def row_stations(row: dict, codes=VALID_CODES) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in row.get("stations") or []:
        f = s.get("temp_f")
        if f is None or s.get("code") not in codes or not plausible_f(f):
            continue
        sid = s.get("station_id")
        if sid:
            out[sid] = float(f)
    return out


def replica_for_row(row: dict, cals: Calibrations, codes=VALID_CODES,
                    cal: Optional[Cal] = None) -> Optional[float]:
    cal = cal or cals.for_minute(int(row["t"]))
    return replica_index(row_stations(row, codes), cal.offsets, cal.b_city_c,
                         cal.weights)


# How likely the settlement minute S shares a non-default basis lattice:
# keep(tau) = p0 * exp(-tau / T). Measured on the 6-week backfill (Sep 1 -
# Oct 14 2026, 2-min targets): a station out >= 10 min (4-station basis) -> S
# on that lattice 94% at tau ~3, 78% at 15, 61% at 30, 36% at 60; out 5-10
# min -> 10% at tau ~3 (short gaps end; use the default lattice); a METAR
# fallback (5 stations, lattice shifted by its tenths of C, a new METAR each
# hour) -> 71% / 53% / 43% / 25%.
LATTICE_KEEP = {"outage": (0.97, 60.0), "metar": (0.75, 55.0)}
OUTAGE_MIN_AGE_MIN = 10.0


def basis_lattice(stations_f: Dict[str, float], cal: Cal
                  ) -> Optional[Tuple[float, float]]:
    """Index values reachable from a station basis when the same stations
    stay in and each reading moves in whole C: base_F + k * step_F.

    None = the default lattice (all configured stations, whole-C readings,
    equal weights, B_c = mean offset): 32 + 0.36 * sum, which is what
    FairModel.threshold() assumes. A four-station basis (a station out longer
    than the fill age) is 1.8/4 = 0.45 F wide and shifted by the present
    stations' offsets; a METAR fallback (tenths of C) shifts the lattice too.
    Unequal weights -> (base, 0.0): no regular lattice (continuous)."""
    cfg = list(cal.offsets.keys()) if cal.offsets else list(STATIONS)
    present = [s for s in cfg if stations_f.get(s) is not None]
    if not present:
        return None
    ws = [float(cal.weights.get(s, 0.2)) for s in present]
    xs = [(float(stations_f[s]) - 32.0) / 1.8 for s in present]
    equal_w = max(ws) - min(ws) <= 1e-9
    mean_b = sum(cal.offsets.get(s, 0.0) for s in cfg) / len(cfg)
    if (len(present) == len(cfg) and equal_w and abs(cal.b_city_c - mean_b) < 1e-9
            and all(abs(x - round(x)) < 1e-3 for x in xs)):
        return None
    wsum = sum(ws)
    base = 32.0 + 1.8 * sum(w * (x - cal.offsets.get(s, 0.0) + cal.b_city_c)
                            for s, w, x in zip(present, ws, xs)) / wsum
    return (base, 1.8 / len(present) if equal_w else 0.0)


# --------------------------------------------------------------------------
# Detailed responses -> rows; synthetic rx
# --------------------------------------------------------------------------

def detailed_rows(obj) -> List[dict]:
    """Minute rows from a detailed response / list of responses / list of rows
    / {"resp": ...} wrapper."""
    if obj is None:
        return []
    if isinstance(obj, dict):
        if "timeseries" in obj and isinstance(obj["timeseries"], list):
            return [r for r in obj["timeseries"] if isinstance(r, dict)]
        if "resp" in obj:
            return detailed_rows(obj["resp"])
        if "t" in obj:
            return [obj]
        return []
    out: List[dict] = []
    for x in obj:
        if isinstance(x, dict) and "t" in x and "timeseries" not in x:
            out.append(x)
        else:
            out.extend(detailed_rows(x))
    return out


def synth_rows_from_detailed(rows: Iterable[dict], city: str = CITY) -> List[dict]:
    """Historical minute rows (no rx) -> the wx stream rows a 1 s poller would
    have written (module docstring, "Tape and snapshots")."""
    out: List[dict] = []
    for r in rows:
        if r.get("t") is None:
            continue
        t = int(r["t"])
        ts = t / 1000.0
        arrivals = []
        for s in r.get("stations") or []:
            # only real readings arrive; a QC-failed one ("range",
            # "provider_qc": temp_f -77.8) must not be relabelled pending
            if s.get("temp_f") is None or s.get("code") not in READING_CODES:
                continue
            recv = s.get("received_at_ms")
            rx = ts + SYN_NO_RECV_S if recv is None else recv / 1000.0 + SYN_RX_LAG_S
            if s.get("source") == "metar" or s.get("primary_code"):
                rx = max(rx, ts + SYN_DEADLINE_S + SYN_RX_LAG_S)
            arrivals.append((rx, s))
        arrivals.sort(key=lambda a: a[0])
        final = r.get("v") is not None and r.get("status") in FINAL_STATUSES
        final_rx = None
        if final:
            final_rx = ts + SYN_OFFICIAL_S
            for rx, s in arrivals:
                if s.get("code") == "ok":
                    final_rx = max(final_rx, rx)
        known: List[dict] = []
        i = 0
        while i < len(arrivals):
            rx = arrivals[i][0]
            if final_rx is not None and rx >= final_rx:
                break
            while i < len(arrivals) and arrivals[i][0] == rx:
                s = dict(arrivals[i][1])
                s["code"] = "late" if s.get("code") == "late" else "pending"
                known.append(s)
                i += 1
            out.append({"rx": rx, "city": city, "t": t, "status": "incomplete",
                        "v": None, "contributors": None,
                        "stations": [dict(s) for s in known]})
        if final:
            early = [dict(s) for rx, s in arrivals if rx <= final_rx]
            late = [rx for rx, s in arrivals if rx > final_rx]
            base = {"city": city, "t": t, "status": r.get("status"),
                    "v": r.get("v"), "contributors": r.get("contributors")}
            out.append(dict(base, rx=final_rx, stations=early))
            if late:
                out.append(dict(base, rx=max(late),
                                stations=[dict(s) for s in r.get("stations") or []]))
    return out


# --------------------------------------------------------------------------
# WxTape / WxSnapshot
# --------------------------------------------------------------------------

class WxSnapshot:
    """State known at some rx. Treat as immutable: WxTape hands the same object
    back while no new rows arrive.

    latest_minute_ms  latest minute with >= 4 stations received (or official v)
    index_latest      replica index (official v once published) at that minute
    history           [(minute_ms, index)] qualifying minutes in the last 60
    stations_latest   station -> (minute_ms, temp_f) newest reading of each
    Extras (optional): est_latest (five-station estimate, see FairModel),
    n_latest (stations known at the latest minute), full_minutes (history
    minutes with all five stations or the official v), lattice (the
    estimate's basis lattice: None = the default five-station whole-C
    lattice; else (base_F, step_F, keep_p0, keep_tau_min) - basis_lattice()
    plus how likely the settlement minute shares it, LATTICE_KEEP: a station
    out >= 10 min leaves a 0.45 F four-station lattice, a METAR fallback a
    shifted 0.36 F one)."""

    __slots__ = ("latest_minute_ms", "index_latest", "history", "stations_latest",
                 "est_latest", "n_latest", "full_minutes", "lattice", "_feat")

    def __init__(self, latest_minute_ms: Optional[int] = None,
                 index_latest: Optional[float] = None,
                 history: Optional[List[Tuple[int, float]]] = None,
                 stations_latest: Optional[Dict[str, Tuple[int, float]]] = None,
                 est_latest: Optional[float] = None, n_latest: int = 0,
                 full_minutes: Optional[frozenset] = None,
                 lattice: Optional[Tuple[float, float]] = None):
        self.latest_minute_ms = latest_minute_ms
        self.index_latest = index_latest
        self.history = history if history is not None else []
        self.stations_latest = stations_latest if stations_latest is not None else {}
        self.est_latest = est_latest
        self.n_latest = n_latest
        self.full_minutes = full_minutes
        self.lattice = lattice
        self._feat = None

    def __repr__(self) -> str:
        return (f"WxSnapshot(latest={self.latest_minute_ms}, index={self.index_latest}, "
                f"est={self.est_latest}, n={self.n_latest}, hist={len(self.history)}, "
                f"lattice={self.lattice})")


class WxTape:
    """wx stream rows (any order; sorted by rx inside) -> snapshot(at_rx).
    Monotone non-decreasing at_rx calls apply only the new rows; an earlier
    at_rx rebuilds from the start."""

    def __init__(self, rows: Iterable[dict], calibrations=None, city: str = CITY,
                 fill_max_age_min: float = 5.0):
        rs = [r for r in rows if isinstance(r, dict) and r.get("t") is not None
              and r.get("rx") is not None and r.get("city", city) in (None, city)]
        rs.sort(key=lambda r: r["rx"])
        self.rows = rs
        self._rxs = [float(r["rx"]) for r in rs]
        if isinstance(calibrations, Calibrations):
            self.cals = calibrations
        elif calibrations is None:
            self.cals = Calibrations.load()
        else:
            self.cals = Calibrations(calibrations)
        self.fill_max_age_ms = int(fill_max_age_min * 60000)
        known = {c.config_version for c in self.cals.cals}
        # config_version strings the rows name but our calibrations lack (a new
        # weekly calibration): 4-station replicas may then be off by ~0.0x F
        self.unknown_cal_versions = sorted({str(r["config_version"]) for r in rs
                                            if r.get("config_version")
                                            and r["config_version"] not in known})
        self._reset()

    @classmethod
    def from_detailed(cls, responses, calibrations=None, city: str = CITY) -> "WxTape":
        """Historical detailed responses (no rx) -> tape with synthetic rx
        (module docstring)."""
        return cls(synth_rows_from_detailed(detailed_rows(responses), city),
                   calibrations=calibrations, city=city)

    def _reset(self) -> None:
        self._ptr = 0
        self._at = -math.inf
        self._st: Dict[int, Dict[str, float]] = {}
        self._idx: Dict[int, float] = {}
        self._full: Dict[int, bool] = {}
        self._qual: List[int] = []
        self._latest: Optional[int] = None
        self._sta: Dict[str, Tuple[int, float]] = {}
        self._snap: Optional[WxSnapshot] = None

    def _apply(self, row: dict) -> None:
        t = int(row["t"])
        st = row_stations(row)
        self._st[t] = st
        for s in row.get("stations") or []:
            f = s.get("temp_f")
            sid = s.get("station_id")
            # QC-failed readings (code "range" / "provider_qc", -77.8 F) are
            # not readings: never a fill value or a stations_latest entry
            if (f is None or not sid or s.get("code") not in READING_CODES
                    or not plausible_f(f)):
                continue
            cur = self._sta.get(sid)
            if cur is None or t >= cur[0]:
                self._sta[sid] = (t, float(f))
        v = row.get("v")
        if v is not None and row.get("status") in FINAL_STATUSES:
            idx, full = float(v), True
        elif len(st) >= 4:
            cal = self.cals.for_minute(t)
            idx = replica_index(st, cal.offsets, cal.b_city_c, cal.weights)
            full = len(st) >= len(STATIONS)
        else:
            idx, full = None, False
        if idx is None:
            if t in self._idx:
                del self._idx[t]
                del self._full[t]
                j = bisect.bisect_left(self._qual, t)
                if j < len(self._qual) and self._qual[j] == t:
                    self._qual.pop(j)
                if self._latest == t:
                    self._latest = self._qual[-1] if self._qual else None
            return
        if t not in self._idx:
            bisect.insort(self._qual, t)
        self._idx[t] = idx
        self._full[t] = full
        if self._latest is None or t > self._latest:
            self._latest = t

    def _lattice(self, vals: Dict[str, float], m: int):
        """Snapshot lattice extra for a basis: None (default lattice) or
        (base_F, step_F, keep_p0, keep_tau_min), see LATTICE_KEEP."""
        lat = basis_lattice(vals, self.cals.for_minute(m))
        if lat is None:
            return None
        if sum(1 for s in STATIONS if s in vals) >= len(STATIONS):
            return lat + LATTICE_KEEP["metar"]
        # a station out: how long since its last reading (a "late" reading of
        # minute m itself = alive, age 0); short gaps end before S
        age = min(((m - self._sta[s][0]) / 60000.0 if s in self._sta else 1e9)
                  for s in STATIONS if s not in vals)
        if age < OUTAGE_MIN_AGE_MIN:
            return None
        return lat + LATTICE_KEEP["outage"]

    def _estimate(self, m: int):
        """(five-station estimate of minute m, its basis lattice extra)."""
        st = self._st.get(m, {})
        if self._full.get(m):
            # official v (its stations = the row's counted ones) or 5 stations
            return self._idx[m], self._lattice(st, m)
        vals = dict(st)
        for sid in STATIONS:
            if sid in vals:
                continue
            cur = self._sta.get(sid)
            # a reading of minute m itself that row_stations dropped is "late"
            # (excluded from the official index) - do not fill with it
            if cur is not None and cur[0] != m and abs(cur[0] - m) <= self.fill_max_age_ms:
                vals[sid] = cur[1]
        if all(s in vals for s in STATIONS):
            return sum(vals[s] for s in STATIONS) / len(STATIONS), self._lattice(vals, m)
        # a station out longer than the fill age: the 4-station replica, on
        # its own 0.45 F lattice (S likely shares it if the outage is old)
        return self._idx[m], self._lattice(st, m)

    def _build(self) -> WxSnapshot:
        L = self._latest
        sta = dict(self._sta)
        if L is None:
            return WxSnapshot(stations_latest=sta)
        lo = bisect.bisect_left(self._qual, L - 59 * 60000)
        hi = bisect.bisect_right(self._qual, L)
        mins = self._qual[lo:hi]
        hist = [(m, self._idx[m]) for m in mins]
        full = frozenset(m for m in mins if self._full[m])
        est, lat = self._estimate(L)
        return WxSnapshot(L, self._idx[L], hist, sta, est,
                          len(self._st.get(L, {})), full, lat)

    def snapshot(self, at_rx: float) -> WxSnapshot:
        if at_rx < self._at:
            self._reset()
        self._at = at_rx
        j = bisect.bisect_right(self._rxs, at_rx, lo=self._ptr)
        if j > self._ptr:
            for r in self.rows[self._ptr:j]:
                self._apply(r)
            self._ptr = j
            self._snap = None
        if self._snap is None:
            self._snap = self._build()
        return self._snap

    def minute_index(self, t_ms: int) -> Optional[float]:
        """Index of a minute as of the last snapshot() call (None if < 4)."""
        return self._idx.get(int(t_ms))


# --------------------------------------------------------------------------
# Params
# --------------------------------------------------------------------------

def default_params() -> dict:
    w, k = 0.05, 2.5
    infl = math.sqrt(1 - w + w * k * k)          # total std / core std
    core = {b: [round(s * DEFAULT_BUCKET_MULT[b] / infl, 4) for s in DEFAULT_SIGMA_TOTAL]
            for b in BUCKET_NAMES}
    n = len(TAU_KNOTS)
    return {
        "version": 1, "source": "defaults",
        "tau_knots": list(TAU_KNOTS),
        "sigma_core": core,
        "season_mult": {s: 1.0 for s in SEASONS},
        "hour_mult": [1.0] * 24,
        "tail_w": w, "tail_k": k,
        "vol_gamma": 0.0, "vol_clip": [0.6, 2.5],
        "rv_ref": {b: 0.25 for b in BUCKET_NAMES},
        "vol_gamma_short": 0.0, "rv20_ref": {b: 0.15 for b in BUCKET_NAMES},
        "vol_eps": 0.05, "vol_short_tau": 15.0,
        "a": [DEFAULT_MEAN_COEFS[0]] * n, "b": [DEFAULT_MEAN_COEFS[1]] * n,
        "c": [DEFAULT_MEAN_COEFS[2]] * n, "e": [DEFAULT_MEAN_COEFS[3]] * n,
        "trend_window_min": 15,
        "fill_max_age_min": 5,
        "partial_sigma": 0.2,
        "drift60": {m: list(v) for m, v in DEFAULT_DRIFT60.items()},
    }


def merge_params(base: dict, over: Optional[dict]) -> dict:
    """Top-level overlay, dropping shape-inconsistent entries (falls back to
    the base value for them)."""
    out = json.loads(json.dumps(base))
    if not over:
        return out
    for k, v in over.items():
        out[k] = v
    knots = out.get("tau_knots") or TAU_KNOTS
    n = len(knots)
    ok = True
    if not isinstance(out.get("e"), list) or len(out["e"]) != n:
        out["e"] = [0.0] * n              # params written before the anomaly term
    for key in ("a", "b", "c", "e"):
        if not isinstance(out.get(key), list) or len(out[key]) != n:
            ok = False
    sc = out.get("sigma_core")
    if not isinstance(sc, dict) or any(
            not isinstance(sc.get(b), list) or len(sc[b]) != n for b in BUCKET_NAMES):
        ok = False
    if not ok:                       # inconsistent knot arrays -> base arrays
        for key in ("tau_knots", "a", "b", "c", "e", "sigma_core"):
            out[key] = json.loads(json.dumps(base[key]))
        out["params_warning"] = "inconsistent knot arrays; used defaults for them"
    d60 = out.get("drift60")
    if not isinstance(d60, dict) or any(
            len(d60.get(str(m), []) or []) != 24 for m in range(1, 13)):
        out["drift60"] = json.loads(json.dumps(base["drift60"]))
    return out


def _cum_curve(rates60: Sequence[float]) -> List[float]:
    """Hourly mean 60-min changes (EST hour h covers [h, h+1)) -> cumulative
    mean change per minute of day (1441 entries, cum[1440] == 0 after
    closure). Rates are linearly interpolated between hour centres."""
    r = [float(x) / 60.0 for x in rates60]
    per_min = []
    for x in range(1440):
        hpos = (x + 0.5) / 60.0 - 0.5
        h0 = math.floor(hpos)
        f = hpos - h0
        per_min.append((1 - f) * r[h0 % 24] + f * r[(h0 + 1) % 24])
    mean = sum(per_min) / 1440.0
    cum = [0.0]
    for v in per_min:
        cum.append(cum[-1] + v - mean)
    return cum


# --------------------------------------------------------------------------
# FairModel
# --------------------------------------------------------------------------

class FairModel:
    """FairModel(params=None): None loads MODEL_PARAMS_FILE (defaults if absent
    or unreadable); a str is a params path; a dict overlays the defaults."""

    def __init__(self, params=None):
        p = None
        if params is None:
            if os.path.exists(C.MODEL_PARAMS_FILE):
                p = C.read_json(C.MODEL_PARAMS_FILE)
            self.params_source = C.MODEL_PARAMS_FILE if p else "defaults"
        elif isinstance(params, str):
            p = C.read_json(params)
            self.params_source = params if p else "defaults"
        else:
            p = params
            self.params_source = str(params.get("source", "dict"))
        self.params = merge_params(default_params(), p if isinstance(p, dict) else None)
        self._prepare()
        self._last = None
        self._thr: Dict[float, float] = {}

    def _prepare(self) -> None:
        P = self.params
        self.knots = [float(k) for k in P["tau_knots"]]
        self.a = [float(x) for x in P["a"]]
        self.b = [float(x) for x in P["b"]]
        self.c = [float(x) for x in P["c"]]
        self.e = [float(x) for x in P["e"]]
        self.sig = {b: [float(x) for x in P["sigma_core"][b]] for b in BUCKET_NAMES}
        self.season_mult = {s: float(P.get("season_mult", {}).get(s, 1.0)) for s in SEASONS}
        hm = P.get("hour_mult")
        self.hour_mult = ([float(x) for x in hm] if isinstance(hm, list) and len(hm) == 24
                          else [1.0] * 24)
        self.tail_w = float(P.get("tail_w", 0.0))
        self.tail_k = float(P.get("tail_k", 2.5))
        self.vol_gamma = float(P.get("vol_gamma", 0.0))
        clip = P.get("vol_clip") or [0.6, 2.5]
        self.vol_lo, self.vol_hi = float(clip[0]), float(clip[1])
        self.rv_ref = {b: float(P.get("rv_ref", {}).get(b, 0.25)) for b in BUCKET_NAMES}
        self.rv20_ref = {b: float(P.get("rv20_ref", {}).get(b, 0.15)) for b in BUCKET_NAMES}
        self.vol_gamma_short = float(P.get("vol_gamma_short", 0.0))
        self.vol_eps = float(P.get("vol_eps", 0.05))
        self.vol_short_tau = float(P.get("vol_short_tau", 15.0))
        self.trend_window = float(P.get("trend_window_min", 15))
        self.partial_sigma = float(P.get("partial_sigma", 0.2))
        self.cum = {m: _cum_curve(P["drift60"][str(m)]) for m in range(1, 13)}

    # -- pieces ----------------------------------------------------------

    def _interp(self, arr: List[float], tau: float, sigma: bool = False) -> float:
        k = self.knots
        if tau <= k[0]:
            return arr[0] * (math.sqrt(max(tau, 0.0) / k[0]) if sigma else 1.0)
        if tau >= k[-1]:
            return arr[-1] * (math.sqrt(tau / k[-1]) if sigma else 1.0)
        j = bisect.bisect_right(k, tau)
        lo, hi = k[j - 1], k[j]
        f = (tau - lo) / (hi - lo)
        return arr[j - 1] + f * (arr[j] - arr[j - 1])

    def _cumval(self, month: int, x: float) -> float:
        cum = self.cum[month]
        wraps = math.floor(x / 1440.0)
        xm = x - wraps * 1440.0
        i = min(int(xm), 1439)
        f = xm - i
        return cum[i] + f * (cum[i + 1] - cum[i]) + wraps * cum[1440]

    def drift(self, settle_ts: float, tau: float) -> float:
        """Climatological mean index change (F) over (S - tau min, S]."""
        d = est_dt(settle_ts)
        x1 = d.hour * 60 + d.minute + d.second / 60.0
        m = d.month
        dim = (_dt.date(d.year + (m == 12), m % 12 + 1, 1) - _dt.date(d.year, m, 1)).days
        pos = d.day - 1 + x1 / 1440.0
        half = dim / 2.0
        if pos >= half:
            m2, w = m % 12 + 1, (pos - half) / dim
        else:
            m2, w = (m - 2) % 12 + 1, (half - pos) / dim

        def one(mm):
            return self._cumval(mm, x1) - self._cumval(mm, x1 - tau)
        return (1 - w) * one(m) + w * one(m2)

    def vol_mult(self, tau: float, bucket: str, rv: Optional[float],
                 rv20: Optional[float]) -> float:
        eps = self.vol_eps
        vm = 1.0
        if self.vol_gamma and rv is not None:
            vm *= ((rv + eps) / (self.rv_ref[bucket] + eps)) ** self.vol_gamma
        g20 = self.vol_gamma_short * max(0.0, 1.0 - tau / self.vol_short_tau)
        if g20 and rv20 is not None:
            vm *= ((rv20 + eps) / (self.rv20_ref[bucket] + eps)) ** g20
        return min(max(vm, self.vol_lo), self.vol_hi)

    def sigma(self, tau: float, settle_ts: float, rv: Optional[float] = None,
              rv20: Optional[float] = None) -> float:
        d = est_dt(settle_ts)
        b = bucket_of_hour(d.hour)
        s = self._interp(self.sig[b], tau, sigma=True) * self.hour_mult[d.hour]
        s *= self.season_mult.get(season_of_month(d.month), 1.0)
        return s * self.vol_mult(tau, b, rv, rv20)

    def features(self, snap: WxSnapshot) -> Optional[dict]:
        """Settle-time-independent features of a snapshot (cached on it)."""
        cached = getattr(snap, "_feat", None)
        if cached is not None and cached[0] is self:
            return cached[1]
        m = snap.latest_minute_ms
        if m is None:
            feat = None
        else:
            est = getattr(snap, "est_latest", None)
            i0 = est if est is not None else snap.index_latest
            hist = list(getattr(snap, "history", None) or [(m, snap.index_latest)])
            full = getattr(snap, "full_minutes", None)
            # One consistent series for every feature: the latest minute is the
            # five-station estimate i0, other minutes only when final or
            # five-station. A non-final four-station minute is a replica of a
            # different station set (KFLL lags ~97% of wall-clock time; the
            # no-KFLL replica is off the official value by rms 0.25 F), so its
            # step to/from a five-station neighbour is composition, not
            # temperature, and would leak into trend, rv and rv20.
            ser = [(t, i0 if t == m else v) for t, v in hist
                   if v is not None and (t == m or full is None or t in full)]
            if not ser or ser[-1][0] != m:
                ser.append((m, i0))
            lo = m - self.trend_window * 60000
            xs, ys = [], []
            for t, v in ser:
                if t >= lo:
                    xs.append((t - m) / 60000.0)
                    ys.append(v)
            trend = dev = 0.0
            if len(xs) >= 5 and xs[-1] == 0.0:
                n = len(xs)
                mx, my = sum(xs) / n, sum(ys) / n
                sxx = sum((x - mx) ** 2 for x in xs)
                if sxx > 0:
                    trend = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
                    dev = (my - trend * mx) - i0
            hv = dict(ser)
            sq = [(v - hv[t - 300000]) ** 2 for t, v in ser if (t - 300000) in hv]
            rv = math.sqrt(sum(sq) / len(sq)) if len(sq) >= 6 else None
            d1 = [(v - hv[t - 60000]) ** 2 for t, v in ser
                  if t >= m - 20 * 60000 and (t - 60000) in hv]
            rv20 = math.sqrt(sum(d1) / len(d1)) if len(d1) >= 10 else None
            anom = 0.0
            t_old, v_old = ser[0]
            span = (m - t_old) / 60000.0
            if span >= 45:
                anom = (i0 - v_old) - self.drift(m / 1000.0, span)
            feat = {"i0": i0, "trend": trend, "dev": dev, "rv": rv, "rv20": rv20,
                    "anom": anom, "n_hist": len(hist)}
        try:
            snap._feat = (self, feat)
        except AttributeError:
            pass
        return feat

    def _settled_value(self, snap: WxSnapshot, settle_ts: float):
        s_ms = int(math.floor(settle_ts / 60.0)) * 60000
        cands = [(t, v) for t, v in (getattr(snap, "history", None) or [])
                 if s_ms - 3600000 < t <= s_ms]
        if not cands and snap.latest_minute_ms is not None and snap.latest_minute_ms <= s_ms:
            cands = [(snap.latest_minute_ms, snap.index_latest)]
        if not cands:
            return None
        t, v = cands[-1]
        full = getattr(snap, "full_minutes", None)
        if full is None or t in full:
            return (float(v), 0.0)
        est = getattr(snap, "est_latest", None)
        if t == snap.latest_minute_ms and est is not None:
            v = est
        return (float(v), self.partial_sigma)

    # -- contract ----------------------------------------------------------

    def dist(self, snap: WxSnapshot, settle_ts: float) -> Optional[Tuple[float, float]]:
        """(mu_F, sigma_F) of the settlement index. sigma_F is the CORE sd of
        the fat-tail mixture (1 - w) N(mu, s) + w N(mu, k s) that fair()
        prices, not its total std (that is s * sqrt(1 - w + w k^2), 1.26x with
        w=0.2, k=2). Do not turn it into probabilities or price slopes with a
        plain normal: use prob_above / density / slopes."""
        d = self.dist_lattice(snap, settle_ts)
        return None if d is None else (d[0], d[1])

    def dist_lattice(self, snap: WxSnapshot, settle_ts: float):
        """(mu_F, core sigma_F, lattice) or None. lattice: None = the
        five-station whole-C lattice, else (base_F, step_F, q) with q the
        weight of the basis lattice in the price (the rest on the default
        lattice) at this tau. Cached per (snap, S)."""
        last = self._last
        if last is not None and last[0] is snap and last[1] == settle_ts:
            return last[2]
        res = self._dist(snap, settle_ts)
        self._last = (snap, settle_ts, res)
        return res

    @staticmethod
    def _resolve_lattice(lat, tau: float):
        if lat is None:
            return None
        if len(lat) >= 4:
            q = float(lat[2]) * math.exp(-max(tau, 0.0) / max(float(lat[3]), 1e-9))
        else:
            q = float(lat[2]) if len(lat) == 3 else 1.0
        return (float(lat[0]), float(lat[1]), q)

    def _dist(self, snap: WxSnapshot, settle_ts: float):
        if (snap is None or getattr(snap, "latest_minute_ms", None) is None
                or getattr(snap, "index_latest", None) is None):
            return None
        tau = (settle_ts - snap.latest_minute_ms / 1000.0) / 60.0
        lat = self._resolve_lattice(getattr(snap, "lattice", None), tau)
        if tau <= 1e-9:
            sv = self._settled_value(snap, settle_ts)
            return None if sv is None else (sv[0], sv[1], lat)
        f = self.features(snap)
        D = self.drift(settle_ts, tau)
        mu = (f["i0"] + self._interp(self.a, tau) * D
              + self._interp(self.b, tau) * f["trend"] * tau
              + self._interp(self.c, tau) * f["dev"]
              + self._interp(self.e, tau) * f["anom"])
        return (mu, self.sigma(tau, settle_ts, f["rv"], f["rv20"]), lat)

    def mixcdf(self, z: float) -> float:
        w = self.tail_w
        if w <= 0:
            return _ncdf(z)
        return (1 - w) * _ncdf(z) + w * _ncdf(z / self.tail_k)

    def density(self, mu: float, sd: float, x: float) -> float:
        """pdf (per F) at x of the mixture fair() prices; sd = the core sigma
        dist() returns. dP(YES)/dmu at a strike = density(mu, sd, cut)."""
        if sd <= 0:
            return 0.0
        z = (x - mu) / sd
        phi = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        w, k = self.tail_w, self.tail_k
        if w <= 0:
            return phi / sd
        return ((1 - w) * phi + w * math.exp(-0.5 * (z / k) ** 2)
                / (math.sqrt(2.0 * math.pi) * k)) / sd

    def threshold(self, strike: float) -> float:
        """Smallest five-station lattice index value above the strike."""
        thr = self._thr.get(strike)
        if thr is None:
            thr = C.index_from_sum(C.sum_threshold(strike))
            self._thr[strike] = thr
        return thr

    def cut(self, strike: float, lattice=None) -> float:
        """Continuity-corrected cut on one lattice: P(YES) = P(X >= cut), the
        midpoint of the two lattice values around the strike (yes_wins
        semantics). lattice None = five-station whole-C; else (base_F,
        step_F, ...) - extra entries (the weight) are ignored here."""
        if lattice is None:
            return self.threshold(strike) - HALF_STEP_F
        base, step = float(lattice[0]), float(lattice[1])
        r = round(strike, 2)
        if step <= 0:
            return r + 0.005
        k = math.floor((r - base) / step)
        while round(base + k * step, 2) <= r + 1e-9:
            k += 1
        while round(base + (k - 1) * step, 2) > r + 1e-9:
            k -= 1
        return (round(base + (k - 1) * step, 2) + round(base + k * step, 2)) / 2.0

    def _cuts(self, strike: float, lattice) -> List[Tuple[float, float]]:
        """[(weight, cut)]: the basis lattice with weight q (lattice[2], 1 if
        absent), the default five-station lattice with 1 - q."""
        if lattice is None:
            return [(1.0, self.cut(strike))]
        q = float(lattice[2]) if len(lattice) > 2 else 1.0
        q = min(max(q, 0.0), 1.0)
        out = [(q, self.cut(strike, lattice))] if q > 0 else []
        if q < 1:
            out.append((1.0 - q, self.cut(strike)))
        return out

    def prob_above(self, mu: float, sd: float, strike: float, lattice=None) -> float:
        """P(index_S > strike). lattice as dist_lattice() returns it (None =
        five-station whole-C; (base, step[, q]) = q on that lattice, 1 - q on
        the default one)."""
        if sd <= 0:
            return 1.0 if C.yes_wins(strike, mu) else 0.0
        p = sum(w * (1.0 - self.mixcdf((c - mu) / sd)) for w, c in self._cuts(strike, lattice))
        return min(max(p, 1e-6), 1 - 1e-6)

    def fair(self, snap: WxSnapshot, settle_ts: float, strikes: List[float]
             ) -> Dict[float, float]:
        d = self.dist_lattice(snap, settle_ts)
        if d is None:
            return {}
        mu, sd, lat = d
        return {k: self.prob_above(mu, sd, k, lat) for k in strikes}

    def slopes(self, snap: WxSnapshot, settle_ts: float, strikes: List[float],
               step_f: float = C.STEP_F) -> Dict[float, float]:
        """Fair-price sensitivity per strike in cents per step_f F move of mu
        (default one 0.36 F lattice step): 100 * density(cut) * step_f, from
        the same mixture and cut as fair(). This is the k*phi(z)/sigma*0.36*100
        price-buffer slope of TEMP_SHADOW.md without the plain-normal error
        (with w=0.2, k=2 the mixture slope is 0.90x the core-sigma normal's at
        the cut and 1.25x / 3.7x it at z = 2 / 3). {} if no data; 0.0 once
        decided (sd 0)."""
        d = self.dist_lattice(snap, settle_ts)
        if d is None:
            return {}
        mu, sd, lat = d
        return {k: 100.0 * step_f * sum(w * self.density(mu, sd, c) for w, c in self._cuts(k, lat))
                for k in strikes}


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def _study(*parts: str) -> str:
    return os.path.join(C.DATA_ROOT, "backfill", "study", *parts)


def _load_any_json(path: str):
    if path.endswith(".jsonl") or path.endswith(".jsonl.gz"):
        return list(C.iter_jsonl(path))
    return C.read_json(path)


def wx_history_files(wx_dir: Optional[str] = None) -> List[str]:
    dirs = [wx_dir] if wx_dir else [os.path.join(C.DATA_ROOT, "backfill", "wx"),
                                    _study("agentB_data", "wxindex")]
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        fs = sorted(f for f in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                    if os.path.isfile(f) and f.endswith((".json", ".jsonl", ".jsonl.gz"))
                    and "calibration" not in os.path.basename(f).lower())
        if d.endswith("wxindex"):
            fs = [f for f in fs if os.path.basename(f).startswith("detailed_")]
        if fs:
            return fs
    return []


def load_wx_history(wx_dir: Optional[str] = None, final_only: bool = True
                    ) -> Tuple[List[dict], List[str]]:
    """Kalshi detailed minute rows (deduplicated by t, later files win),
    sorted by t. Accepts detailed responses, row lists, {"resp": ...}
    wrappers and wx stream jsonl (their rx is dropped; the tape re-derives
    it). final_only keeps rows with an official v."""
    files = wx_history_files(wx_dir)
    by_t: Dict[int, dict] = {}
    for f in files:
        try:
            obj = _load_any_json(f)
        except Exception:
            continue
        for r in detailed_rows(obj):
            if r.get("t") is None:
                continue
            if r.get("city") not in (None, CITY):
                continue
            if final_only and not (r.get("v") is not None and r.get("status") in FINAL_STATUSES):
                continue
            by_t[int(r["t"])] = r
    return [by_t[t] for t in sorted(by_t)], files


def iem_path_default() -> Optional[str]:
    for p in sorted(glob.glob(os.path.join(C.DATA_ROOT, "backfill", "iem", "*.csv"))):
        try:
            with open(p, encoding="utf-8") as f:
                head = f.readline()
            if "valid_utc" in head and "index_f" in head:
                return p
        except OSError:
            continue
    p = _study("agentD", "miami_index_minute.csv")
    return p if os.path.exists(p) else None


def load_iem(path: Optional[str] = None, until_ms: Optional[int] = None):
    """IEM minute CSV (valid_utc, station cols, n, index_f) -> DataFrame on a
    complete minute grid: columns index_f, key (station-set bitmask), n."""
    import numpy as np
    import pandas as pd
    path = path or iem_path_default()
    if not path or not os.path.exists(path):
        return None, None
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["valid_utc"], utc=True, format="mixed")
    df.index = ts
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if until_ms is not None:
        df = df[df.index.asi8 // 10 ** 6 < until_ms]
    st_cols = [c for c in df.columns if c not in ("valid_utc", "n", "index_c", "index_f")]
    key = np.zeros(len(df), dtype=np.int64)
    for i, c in enumerate(st_cols):
        key |= (df[c].notna().to_numpy().astype(np.int64) << i)
    out = pd.DataFrame({"index_f": df["index_f"].astype(float).to_numpy(),
                        "key": key, "n": df["n"].fillna(0).astype(int).to_numpy()},
                       index=df.index)
    full = pd.date_range(out.index[0], out.index[-1], freq="1min")
    out = out.reindex(full)
    out["key"] = out["key"].fillna(-1).astype(np.int64)
    out["n"] = out["n"].fillna(0).astype(int)
    return out, path


def _norm_trades(trades: list) -> Tuple[List[float], List[float]]:
    rows = []
    for tr in trades or []:
        ts = C.parse_iso(tr.get("created_time") or "")
        px = tr.get("yes_price_dollars")
        if px is None:
            px = tr.get("yes_price")
        if ts is None or px is None:
            continue
        rows.append((ts, C.dollars_to_cents(px) / 100.0))
    rows.sort()
    return [r[0] for r in rows], [r[1] for r in rows]


def _event_record(event: str, markets: List[dict], trades: Dict[str, list]) -> Optional[dict]:
    pe = C.parse_event(event)
    settle = None
    mk = []
    for m in markets:
        tk = m.get("ticker")
        if not tk:
            continue
        if settle is None and m.get("close_time"):
            settle = C.parse_iso(m["close_time"])
        strike = m.get("floor_strike")
        strike = float(strike) if strike is not None else C.strike_of(tk)
        res = m.get("result")
        ev = m.get("expiration_value")
        try:
            ev = float(ev) if ev not in (None, "") else None
        except (TypeError, ValueError):
            ev = None
        if res not in ("yes", "no"):
            if ev is None or strike is None:
                continue
            res = "yes" if C.yes_wins(strike, ev) else "no"
        if strike is None:
            continue
        tt, pp = _norm_trades(trades.get(tk, []))
        mk.append({"ticker": tk, "strike": strike, "y": 1 if res == "yes" else 0,
                   "expiration_value": ev, "t_times": tt, "t_px": pp})
    if not mk:
        return None
    if settle is None and pe:
        settle = pe[1]
    if settle is None:
        return None
    value = next((m["expiration_value"] for m in mk if m["expiration_value"] is not None), None)
    return {"event": event, "settle_ts": settle, "markets": mk, "value": value}


def load_settled_events(settled_dirs: Optional[List[str]] = None) -> Tuple[List[dict], List[str]]:
    """Settled events with outcomes and REST trades. Priority (later wins):
    study agentB markets+trades < backfill/settled < settled/."""
    events: Dict[str, dict] = {}
    sources: List[str] = []
    if settled_dirs is None:
        mpath = _study("agentB_data", "markets", "markets_settled.json")
        if os.path.exists(mpath):
            allm = C.read_json(mpath, []) or []
            by_ev: Dict[str, list] = {}
            for m in allm:
                if m.get("event_ticker", "").startswith(C.SERIES):
                    by_ev.setdefault(m["event_ticker"], []).append(m)
            n0 = len(events)
            for ev, ms in by_ev.items():
                tpath = _study("agentB_data", "trades", f"{ev}.json")
                trades = C.read_json(tpath, {}) or {}
                rec = _event_record(ev, ms, trades)
                if rec:
                    events[ev] = rec
            if len(events) > n0:
                sources.append(mpath)
        settled_dirs = [os.path.join(C.DATA_ROOT, "backfill", "settled"),
                        os.path.join(C.DATA_ROOT, "settled")]
    for d in settled_dirs:
        fs = sorted(glob.glob(os.path.join(d, "*.json")))
        n0 = 0
        for f in fs:
            obj = C.read_json(f)
            if not isinstance(obj, dict):
                continue
            ev = obj.get("event_ticker") or os.path.splitext(os.path.basename(f))[0]
            rec = _event_record(ev, obj.get("markets") or [], obj.get("trades") or {})
            if rec:
                events[ev] = rec
                n0 += 1
        if n0:
            sources.append(d)
    return [events[k] for k in sorted(events, key=lambda e: events[e]["settle_ts"])], sources


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------

def fit_drift(iem, n0: float = 200.0) -> Tuple[dict, dict]:
    """Mean 60-min change (F/h) per (month, EST hour) from same-station-set
    1-minute differences, shrunk toward the all-month hour mean, with daily
    closure."""
    import numpy as np
    I = iem["index_f"].to_numpy()
    K = iem["key"].to_numpy()
    N = iem["n"].to_numpy()
    tms = iem.index.asi8 // 10 ** 6
    ok = ((K[1:] == K[:-1]) & (K[1:] >= 0) & (N[1:] >= 4)
          & np.isfinite(I[1:]) & np.isfinite(I[:-1]))
    d = (I[1:] - I[:-1])[ok]
    mid = tms[1:][ok] - 30000 - EST_OFFSET_S * 1000
    hour = (mid // 3600000) % 24
    import pandas as pd
    month = pd.to_datetime(mid, unit="ms").month.to_numpy()
    sums = np.zeros((13, 24))
    cnts = np.zeros((13, 24))
    np.add.at(sums, (month, hour), d)
    np.add.at(cnts, (month, hour), 1)
    all_h = sums[1:].sum(axis=0) / np.maximum(cnts[1:].sum(axis=0), 1)
    table, nmeta = {}, {}
    for m in range(1, 13):
        r = (sums[m] + n0 * all_h) / (cnts[m] + n0)
        r = r - r.mean()
        table[str(m)] = [round(float(x) * 60.0, 3) for x in r]
        nmeta[str(m)] = int(cnts[m].sum())
    return table, nmeta


def _drift_vec(cum_arr, months, x1, tau):
    """Vectorized drift without month blending (fit priors only)."""
    import numpy as np
    x0 = x1 - tau
    def cv(x):
        w = np.floor(x / 1440.0)
        xm = x - w * 1440.0
        i = np.minimum(xm.astype(int), 1439)
        f = xm - i
        return cum_arr[months, i] + f * (cum_arr[months, i + 1] - cum_arr[months, i]) + w * cum_arr[months, 1440]
    return cv(x1) - cv(x0)


def iem_sigma_prior(iem, drift60: dict, a: float = 0.75, step: int = 5) -> dict:
    """Total-std prior per (tau knot, bucket, season) from the IEM minute index:
    rms of I(S) - I(S - tau) - a*drift, same station set, top 0.5% trimmed."""
    import numpy as np
    import pandas as pd
    cum = np.array([[0.0] * 1441] + [_cum_curve(drift60[str(m)]) for m in range(1, 13)])
    I = iem["index_f"].to_numpy()
    K = iem["key"].to_numpy()
    N = iem["n"].to_numpy()
    tms = iem.index.asi8 // 10 ** 6
    est = pd.to_datetime(tms - EST_OFFSET_S * 1000, unit="ms")
    hours = est.hour.to_numpy()
    months = est.month.to_numpy()
    mod = (est.hour * 60 + est.minute).to_numpy().astype(float)
    bidx = np.array([BUCKET_NAMES.index(bucket_of_hour(h)) for h in range(24)])[hours]
    sidx = np.array([0] + [SEASONS.index(season_of_month(m)) for m in range(1, 13)])[months]
    out = {"all": {}, "season": {}, "hour": {}}
    sel = np.arange(len(I)) % step == 0
    for tau in TAU_KNOTS:
        y = np.roll(I, 0)[tau:]
        x = I[:-tau]
        ok = ((K[tau:] == K[:-tau]) & (K[tau:] >= 0) & (N[tau:] >= 4)
              & np.isfinite(y) & np.isfinite(x) & sel[tau:])
        S = np.nonzero(ok)[0] + tau
        D = _drift_vec(cum, months[S], mod[S], float(tau))
        e = I[S] - I[S - tau] - a * D
        for bi, b in enumerate(BUCKET_NAMES):
            mb = bidx[S] == bi
            out["all"].setdefault(b, {})[tau] = _trim_rms(e[mb])
            for si, s in enumerate(SEASONS):
                ms = mb & (sidx[S] == si)
                out["season"].setdefault(s, {}).setdefault(b, {})[tau] = _trim_rms(e[ms])
        for h in range(24):
            out["hour"].setdefault(h, {})[tau] = _trim_rms(e[hours[S] == h])
    return out


def hour_mult_from_prior(prior: dict, min_tau: int = 10) -> List[float]:
    """Within-bucket diurnal sigma shape: median over tau >= min_tau of
    rms(hour) / rms(bucket), normalized to mean 1 inside each bucket."""
    import numpy as np
    raw = []
    for h in range(24):
        b = bucket_of_hour(h)
        rs = [prior["hour"][h][t][0] / prior["all"][b][t][0] for t in TAU_KNOTS
              if t >= min_tau and prior["hour"][h].get(t) and prior["all"][b].get(t)]
        raw.append(float(np.median(rs)) if rs else 1.0)
    out = [1.0] * 24
    for name, lo, hi in BUCKETS:
        hs = list(range(lo, hi + 1))
        mean = sum(raw[h] for h in hs) / len(hs)
        for h in hs:
            out[h] = round(raw[h] / mean, 4) if mean > 0 else 1.0
    return out


def _trim_rms(e) -> Optional[Tuple[float, int]]:
    import numpy as np
    if len(e) < 30:
        return None
    a = np.abs(e)
    cut = np.quantile(a, 0.995)
    e = e[a <= cut]
    return (float(np.sqrt(np.mean(e * e))), int(len(e)))


def _u01(S_ms: int, L: int) -> float:
    return (((S_ms // 60000) * 2654435761 + L * 40503) % 1000003) / 1000003.0


def kalshi_samples(rows: List[dict], cals: Calibrations, model: FairModel,
                   since_ms: Optional[int] = None, until_ms: Optional[int] = None,
                   step_min: int = 2, verbose: bool = False) -> dict:
    """Replay the station history through WxTape at wall-clock lags FIT_LAGS
    before each target minute S (every step_min minutes, official v known) and
    record model features + the realized index. Look-ahead safe by rx."""
    import numpy as np
    finals = {int(r["t"]): float(r["v"]) for r in rows
              if r.get("v") is not None and r.get("status") in FINAL_STATUSES}
    targets = sorted(t for t in finals
                     if (since_ms is None or t >= since_ms)
                     and (until_ms is None or t < until_ms)
                     and (t // 60000) % step_min == 0)
    use = [r for r in rows if until_ms is None or int(r["t"]) < until_ms]
    tape = WxTape(synth_rows_from_detailed(use), calibrations=cals,
                  fill_max_age_min=float(model.params.get("fill_max_age_min", 5)))
    cols = {k: [] for k in ("S", "tau", "knot", "i0", "D", "trend", "dev", "anom", "rv",
                            "rv20", "y", "bucket", "month", "n_latest")}
    for knot, L in zip(FIT_KNOTS, FIT_LAGS):
        t0 = time.time()
        for S in targets:
            d = S / 1000.0 - L * 60.0 - 60.0 * _u01(S, L)
            snap = tape.snapshot(d)
            if snap.latest_minute_ms is None:
                continue
            tau = (S - snap.latest_minute_ms) / 60000.0
            if tau <= 0 or tau > 90:
                continue
            f = model.features(snap)
            cols["S"].append(S)
            cols["tau"].append(tau)
            cols["knot"].append(knot)
            cols["i0"].append(f["i0"])
            cols["D"].append(model.drift(S / 1000.0, tau))
            cols["trend"].append(f["trend"])
            cols["dev"].append(f["dev"])
            cols["anom"].append(f["anom"])
            cols["rv"].append(f["rv"] if f["rv"] is not None else np.nan)
            cols["rv20"].append(f["rv20"] if f["rv20"] is not None else np.nan)
            cols["y"].append(finals[S])
            cols["bucket"].append(BUCKET_NAMES.index(bucket_of_hour(est_hour(S / 1000.0))))
            cols["month"].append(est_month(S / 1000.0))
            cols["n_latest"].append(snap.n_latest)
        if verbose:
            print(f"  samples lag {L:>2} min (knot {knot}): {len(targets)} targets "
                  f"{time.time() - t0:.1f}s", flush=True)
    return {k: np.array(v, dtype=float) for k, v in cols.items()}


def _sig_shape(tau, knots, totals):
    """Default total-std shape at tau (sqrt extrapolation outside knots)."""
    import numpy as np
    tau = np.asarray(tau, dtype=float)
    s = np.interp(tau, knots, totals)
    s = np.where(tau < knots[0], totals[0] * np.sqrt(np.maximum(tau, 1e-9) / knots[0]), s)
    s = np.where(tau > knots[-1], totals[-1] * np.sqrt(tau / knots[-1]), s)
    return s


def _mixcdf_np(z, w, k):
    from scipy.special import ndtr
    return (1 - w) * ndtr(z) + w * ndtr(z / k)


def _cell_ll(e, g, s, w, k):
    import numpy as np
    sd = s * g
    p = _mixcdf_np((e + HALF_STEP_F) / sd, w, k) - _mixcdf_np((e - HALF_STEP_F) / sd, w, k)
    return float(np.sum(np.log(np.maximum(p, 1e-12))))


def _golden_sigma(e, g, w, k, lo=0.02, hi=6.0, iters=40):
    a, b = math.log(lo), math.log(hi)
    gr = (math.sqrt(5) - 1) / 2
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc = _cell_ll(e, g, math.exp(c), w, k)
    fd = _cell_ll(e, g, math.exp(d), w, k)
    for _ in range(iters):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = _cell_ll(e, g, math.exp(c), w, k)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = _cell_ll(e, g, math.exp(d), w, k)
    s = math.exp((a + b) / 2)
    return s, _cell_ll(e, g, s, w, k)


def fit_mean_coefs(sm: dict, ridge_n: float = 10.0, step_min: float = 2.0,
                   ridge_neff: bool = True) -> Tuple[dict, dict]:
    """Per fit knot: ridge OLS of (y - i0) on [drift, trend*tau, dev, anom]
    toward DEFAULT_MEAN_COEFS. The prior weighs ridge_n EFFECTIVE samples:
    the targets overlap (n_eff = n * n_eff_factor(knot, step_min));
    ridge_neff=False weighs it against the nominal n (the pre-10/15 rule,
    with ridge_n 50). ridge_n 10 effective = the old prior strength at knot
    10 / step 2; 50 effective pulled the long knots toward the IEM-based
    a = 0.75 (Kalshi fits say 0.85-1.2) and cost 0.5% log loss out of sample
    (fit Sep, test Oct 1-6)."""
    import numpy as np
    prior = np.array(DEFAULT_MEAN_COEFS, dtype=float)
    out, meta = {}, {}
    for knot in FIT_KNOTS:
        m = sm["knot"] == knot
        n = int(m.sum())
        if n < 30:
            continue
        n_eff = max(1.0, n * n_eff_factor(knot, step_min)) if ridge_neff else float(n)
        X = np.column_stack([sm["D"][m], sm["trend"][m] * sm["tau"][m], sm["dev"][m],
                             sm["anom"][m]])
        yv = sm["y"][m] - sm["i0"][m]
        XtX = X.T @ X
        # the anomaly term (weak in the 10/07-10/13 study) gets 10x the prior weight
        lam = (ridge_n * (n / n_eff) * np.array([1.0, 1.0, 1.0, 10.0])
               * np.maximum(np.diag(XtX) / n, 1e-6))
        beta = np.linalg.solve(XtX + np.diag(lam), X.T @ yv + lam * prior)
        res0 = yv - X @ prior
        res1 = yv - X @ beta
        out[knot] = [float(x) for x in beta]
        meta[str(knot)] = {"n": n, "n_eff": round(n_eff, 1),
                           "a": round(float(beta[0]), 4), "b": round(float(beta[1]), 4),
                           "c": round(float(beta[2]), 4), "e": round(float(beta[3]), 4),
                           "rms_prior": round(float(np.sqrt(np.mean(res0 ** 2))), 4),
                           "rms_fit": round(float(np.sqrt(np.mean(res1 ** 2))), 4),
                           "bias": round(float(np.mean(res1)), 4)}
    return out, meta


def fit_params(rows: List[dict], iem_path: Optional[str] = None,
               until_ms: Optional[int] = None, since_ms: Optional[int] = None,
               step_min: int = 2, cals: Optional[Calibrations] = None,
               verbose: bool = True, shrink_n0: float = 200.0,
               sigma_shrink: str = "neff_pooled", select_neff: bool = True,
               ridge_n: float = 10.0, ridge_neff: bool = True) -> dict:
    """Fit model params (see module docstring). rows = Kalshi detailed minute
    rows; only t < until_ms is used (station history and IEM).
    sigma_shrink "neff_pooled" (default): each (knot, bucket) sigma MLE is
    shrunk toward the knot's pooled Kalshi MLE with weight n_eff/(n_eff + n0);
    "legacy": nominal n toward the IEM level (the pre-10/15 rule, kept for
    comparisons). select_neff weighs the model-selection likelihood by n_eff."""
    import numpy as np
    t_start = time.time()
    cals = cals or Calibrations.load()
    P = default_params()
    meta: dict = {"generated_at": C.iso(C.utcnow()), "until_ms": until_ms,
                  "since_ms": since_ms, "step_min": step_min}

    # 1. drift from IEM -------------------------------------------------
    iem, ipath = load_iem(iem_path, until_ms)
    prior = None
    if iem is not None and len(iem) > 10000:
        P["drift60"], nmeta = fit_drift(iem)
        meta["iem"] = {"path": ipath, "from": str(iem.index[0]), "to": str(iem.index[-1]),
                       "n_diffs_by_month": nmeta}
        prior = iem_sigma_prior(iem, P["drift60"])
        if verbose:
            print(f"drift: IEM {ipath} {iem.index[0]} .. {iem.index[-1]}", flush=True)
    else:
        meta["iem"] = {"path": ipath, "note": "no IEM data; built-in drift table"}

    # 2. Kalshi samples ---------------------------------------------------
    rows = [r for r in rows if (until_ms is None or int(r["t"]) < until_ms)
            and (since_ms is None or int(r["t"]) >= since_ms - 3 * 3600000)]
    model0 = FairModel(P)
    sm = kalshi_samples(rows, cals, model0, since_ms, until_ms, step_min, verbose)
    n_s = len(sm["y"])
    meta["wx"] = {"rows": len(rows),
                  "from": C.iso(rows[0]["t"] / 1000) if rows else None,
                  "to": C.iso(rows[-1]["t"] / 1000) if rows else None,
                  "n_samples": n_s}
    if verbose:
        print(f"samples: {n_s} from {len(rows)} minute rows", flush=True)

    # Season multipliers from IEM, normalized to the Kalshi sample's mix.
    ratio = {s: 1.0 for s in SEASONS}
    if prior:
        for s in SEASONS:
            rs = []
            for b in BUCKET_NAMES:
                for tau in TAU_KNOTS:
                    if tau < 10:
                        continue
                    a_ = prior["all"][b].get(tau)
                    s_ = prior["season"][s][b].get(tau)
                    if a_ and s_ and s_[1] >= 200:
                        rs.append(s_[0] / a_[0])
            if rs:
                ratio[s] = float(np.median(rs))
    if n_s:
        fr = {s: float(np.mean([season_of_month(int(m)) == s for m in sm["month"]])) for s in SEASONS}
    else:
        fr = {"ON": 1.0, "JJAS": 0.0, "DJFMAM": 0.0}
    mix = sum(fr[s] * ratio[s] for s in SEASONS) or 1.0
    P["season_mult"] = {s: round(ratio[s] / mix, 4) for s in SEASONS}
    meta["season"] = {"iem_ratio": ratio, "sample_mix": fr}
    if prior:
        P["hour_mult"] = hour_mult_from_prior(prior)

    if n_s < 200:
        meta["note"] = "too few Kalshi samples; sigma/coefs left at defaults"
        P["source"] = "fit"
        P["fit"] = meta
        return P

    # 3. mean coefficients ------------------------------------------------
    coefs, cmeta = fit_mean_coefs(sm, ridge_n=ridge_n, step_min=step_min,
                                  ridge_neff=ridge_neff)
    meta["mean_coefs"] = cmeta
    fitted = sorted(coefs)
    for i, kn in enumerate(TAU_KNOTS):
        if not fitted:
            break
        src = kn if kn in coefs else min(fitted, key=lambda f: abs(math.log(f / kn)))
        P["a"][i], P["b"][i], P["c"][i], P["e"][i] = [round(x, 4) for x in coefs[src]]
    knots = np.array(TAU_KNOTS, dtype=float)
    mu = (sm["i0"] + np.interp(sm["tau"], knots, P["a"]) * sm["D"]
          + np.interp(sm["tau"], knots, P["b"]) * sm["trend"] * sm["tau"]
          + np.interp(sm["tau"], knots, P["c"]) * sm["dev"]
          + np.interp(sm["tau"], knots, P["e"]) * sm["anom"])
    e = sm["y"] - mu
    meta["residual_rms_by_knot"] = {str(k): round(float(np.sqrt(np.mean(e[sm["knot"] == k] ** 2))), 4)
                                    for k in FIT_KNOTS if np.any(sm["knot"] == k)}

    # 4. sigma, tails, vol --------------------------------------------------
    shape = _sig_shape(sm["tau"], knots, DEFAULT_SIGMA_TOTAL)
    kshape = _sig_shape(sm["knot"], knots, DEFAULT_SIGMA_TOTAL)
    g0 = shape / kshape
    seas = np.array([P["season_mult"][season_of_month(int(m))] for m in sm["month"]])
    hrs = ((sm["S"].astype(np.int64) - EST_OFFSET_S * 1000) // 3600000) % 24
    g0 = g0 * seas
    hm_iem = np.array(P["hour_mult"])[hrs]
    rv, rv20 = sm["rv"], sm["rv20"]
    rv_ref, rv20_ref = {}, {}
    for bi, b in enumerate(BUCKET_NAMES):
        x = rv[(sm["bucket"] == bi) & np.isfinite(rv)]
        rv_ref[b] = round(float(np.median(x)), 4) if len(x) >= 50 else 0.25
        x = rv20[(sm["bucket"] == bi) & np.isfinite(rv20)]
        rv20_ref[b] = round(float(np.median(x)), 4) if len(x) >= 50 else 0.15
    rvr = np.array([rv_ref[BUCKET_NAMES[int(b)]] for b in sm["bucket"]])
    rvr20 = np.array([rv20_ref[BUCKET_NAMES[int(b)]] for b in sm["bucket"]])
    vlo, vhi = P["vol_clip"]
    eps, short_tau = float(P["vol_eps"]), float(P["vol_short_tau"])
    ok60 = np.isfinite(rv)
    ok20 = np.isfinite(rv20)
    l60 = np.where(ok60, np.log((np.where(ok60, rv, 0) + eps) / (rvr + eps)), 0.0)
    l20 = np.where(ok20, np.log((np.where(ok20, rv20, 0) + eps) / (rvr20 + eps)), 0.0)
    w20 = np.maximum(0.0, 1.0 - sm["tau"] / short_tau)
    cells = [(kn, bi) for kn in FIT_KNOTS for bi in range(len(BUCKET_NAMES))]
    masks = {c: (sm["knot"] == c[0]) & (sm["bucket"] == c[1]) for c in cells}
    cells = [c for c in cells if masks[c].sum() >= 40]
    # the (tail, vol) grid search runs on a deterministic subsample when big;
    # the per-cell sigmas are then refit on everything at the chosen point
    every = max(1, int(math.ceil(n_s / 80000.0)))
    sub = (np.arange(n_s) % every) == 0
    full_masks = masks
    masks = {c: full_masks[c] & sub for c in cells}

    # Each cell's log-likelihood counts its overlapping targets once per
    # EFFECTIVE sample (select_neff), so the tail / vol / hour-shape choices
    # are not driven by a sum that overstates the evidence ~knot/step times.
    llw = {c: (n_eff_factor(c[0], step_min) if select_neff else 1.0) for c in cells}

    def vol_g(gamma, gamma20):
        return g0 * np.clip(np.exp(gamma * l60 + gamma20 * w20 * l20), vlo, vhi)

    def total_ll(w, k, gamma, gamma20=0.0):
        g = vol_g(gamma, gamma20)
        tot, sig = 0.0, {}
        for c in cells:
            mk = masks[c]
            s, ll = _golden_sigma(e[mk], g[mk], w, k, iters=28)
            sig[c] = s
            tot += ll * llw[c]
        return tot, sig

    grid_wk = [(w, k) for w in (0.0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30) for k in (1.6, 2.0, 2.5, 3.0, 4.0)]
    best = None
    t0 = time.time()
    for w, k in grid_wk:
        ll, sig = total_ll(w, k, 0.0)
        if best is None or ll > best[0]:
            best = (ll, w, k, 0.0, sig)
    best = best + (0.0,)
    _, bw, bk, _, _, _ = best
    for gamma in (0.25, 0.5, 0.75, 1.0):
        ll, sig = total_ll(bw, bk, gamma)
        if ll > best[0]:
            best = (ll, bw, bk, gamma, sig, 0.0)
    bg = best[3]
    for g20 in (0.25, 0.5, 0.75, 1.0):
        ll, sig = total_ll(bw, bk, bg, g20)
        if ll > best[0]:
            best = (ll, bw, bk, bg, sig, g20)
    bg20 = best[5]
    if bg or bg20:
        for w, k in grid_wk:
            ll, sig = total_ll(w, k, bg, bg20)
            if ll > best[0]:
                best = (ll, w, k, bg, sig, bg20)
    ll, bw, bk, bg, sig, bg20 = best
    # the IEM within-bucket hour shape is kept only if it raises the likelihood
    g_plain = g0
    g0 = g_plain * hm_iem
    ll_h, sig_h = total_ll(bw, bk, bg, bg20)
    use_hour = bool(ll_h > ll)
    meta["hour_mult_iem"] = P["hour_mult"]
    meta["hour_mult_ll"] = {"with": round(float(ll_h), 2), "without": round(float(ll), 2)}
    if use_hour:
        ll, sig = ll_h, sig_h
    else:
        g0 = g_plain
        P["hour_mult"] = [1.0] * 24
    if every > 1:
        masks = full_masks
        ll, sig = total_ll(bw, bk, bg, bg20)
    if verbose:
        print(f"tails w={bw} k={bk} vol_gamma={bg} vol_gamma_short={bg20} ll={ll:.1f} "
              f"({time.time() - t0:.1f}s)", flush=True)
    P["tail_w"], P["tail_k"], P["vol_gamma"], P["rv_ref"] = bw, bk, bg, rv_ref
    P["vol_gamma_short"], P["rv20_ref"] = bg20, rv20_ref
    infl = math.sqrt(1 - bw + bw * bk * bk)
    n_cell = {c: int(masks[c].sum()) for c in cells}
    # Per-knot pooled Kalshi MLE (all buckets): the shrinkage target. A cell
    # holds only a few independent days (n_eff = n * step/knot: ~17 for a
    # 3-day 60|afternoon cell of nominal n 510), so its MLE is shrunk toward
    # the pooled level by n_eff, not by the overlapping-sample count. The IEM
    # bucket levels made the out-of-sample fit worse and are only a fallback.
    g_fin = vol_g(bg, bg20)
    pooled: Dict[int, float] = {}
    for kn in FIT_KNOTS:
        mk = np.zeros(n_s, dtype=bool)
        for c in cells:
            if c[0] == kn:
                mk |= masks[c]
        if mk.sum() >= 40:
            pooled[kn] = _golden_sigma(e[mk], g_fin[mk], bw, bk, iters=40)[0]
    meta["sigma_pooled"] = {str(k): round(v, 4) for k, v in pooled.items()}
    meta["sigma_shrink"] = {"mode": sigma_shrink, "n0": shrink_n0, "step_min": step_min,
                            "select_neff": select_neff}
    core = {}
    cell_meta = {}
    for bi, b in enumerate(BUCKET_NAMES):
        vals: Dict[int, float] = {}
        for kn in FIT_KNOTS:
            c = (kn, bi)
            pr_iem = None
            if prior and prior["all"][b].get(kn):
                pr_iem = prior["all"][b][kn][0] * mix / infl
            if sigma_shrink == "legacy":         # nominal n toward the IEM level
                pr, n_w = pr_iem, n_cell.get(c, 0)
            else:                                # n_eff toward the pooled Kalshi level
                pr = pooled.get(kn, pr_iem)
                n_w = n_cell.get(c, 0) * n_eff_factor(kn, step_min)
            if c in sig:
                s = sig[c] if pr is None else (n_w * sig[c] + shrink_n0 * pr) / (n_w + shrink_n0)
                vals[kn] = s
                cell_meta[f"{kn}|{b}"] = {"n": n_cell[c], "n_eff": round(n_w, 1),
                                          "mle": round(sig[c], 4),
                                          "prior": round(pr, 4) if pr else None,
                                          "iem": round(pr_iem, 4) if pr_iem else None,
                                          "final": round(s, 4)}
            elif pr is not None:
                vals[kn] = pr
        arr = []
        for i, kn in enumerate(TAU_KNOTS):
            if kn in vals:
                arr.append(vals[kn])
            else:
                fitted_k = [k for k in vals]
                if fitted_k:
                    src = min(fitted_k, key=lambda f: abs(math.log(f / kn)))
                    arr.append(vals[src] * DEFAULT_SIGMA_TOTAL[i] / DEFAULT_SIGMA_TOTAL[TAU_KNOTS.index(src)])
                else:
                    arr.append(P["sigma_core"][b][i])
        core[b] = [round(float(x), 4) for x in arr]
    P["sigma_core"] = core
    meta["sigma_cells"] = cell_meta
    meta["ll"] = round(float(ll), 2)
    meta["seconds"] = round(time.time() - t_start, 1)
    P["source"] = "fit"
    P["fit"] = meta
    return P


# --------------------------------------------------------------------------
# Eval
# --------------------------------------------------------------------------

def _last_trade(ev_m: dict, d: float) -> Optional[Tuple[float, float]]:
    tt = ev_m["t_times"]
    i = bisect.bisect_left(tt, d) - 1
    if i < 0:
        return None
    return ev_m["t_px"][i], tt[i]


def _ll(p: float, y: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _metrics(rows: List[tuple]) -> dict:
    """rows: (p_model, p_mkt or None, y[, ...]). Brier/logloss on the common
    set (market present) plus model-only Brier on all rows."""
    common = [r for r in rows if r[1] is not None]
    out = {"n_all": len(rows), "n": len(common)}
    if rows:
        out["brier_model_all"] = round(sum((r[0] - r[2]) ** 2 for r in rows) / len(rows), 5)
    if common:
        n = len(common)
        out["brier_model"] = round(sum((r[0] - r[2]) ** 2 for r in common) / n, 5)
        out["brier_mkt"] = round(sum((r[1] - r[2]) ** 2 for r in common) / n, 5)
        out["ll_model"] = round(sum(_ll(r[0], r[2]) for r in common) / n, 5)
        out["ll_mkt"] = round(sum(_ll(r[1], r[2]) for r in common) / n, 5)
        # 50/50 log-odds blend: < brier_mkt means the model adds information
        out["brier_blend"] = round(sum((_blend(r[0], r[1]) - r[2]) ** 2 for r in common) / n, 5)
    return out


def _bootstrap(rows: List[tuple], n_boot: int = 2000, seed: int = 7) -> dict:
    """Event-clustered bootstrap of (model - market) and (blend - market)
    Brier and log-loss differences. rows: (p_model, p_mkt, y, event_id); rows
    without a market price are skipped. Strike-rows of one event (10 strikes x
    8 taus) are strongly dependent, so events are resampled whole. Negative =
    better than the market. Returns mean, 95% CI and P(diff < 0)."""
    import numpy as np
    by_ev: Dict[object, list] = {}
    for r in rows:
        if r[1] is None:
            continue
        p, m, y = r[0], r[1], r[2]
        bm, bb = (p - y) ** 2 - (m - y) ** 2, (_blend(p, m) - y) ** 2 - (m - y) ** 2
        lm, lb = _ll(p, y) - _ll(m, y), _ll(_blend(p, m), y) - _ll(m, y)
        a = by_ev.setdefault(r[3], [0, 0.0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += bm
        a[2] += bb
        a[3] += lm
        a[4] += lb
    if len(by_ev) < 2:
        return {"n_events": len(by_ev)}
    A = np.array(list(by_ev.values()), dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(A), size=(n_boot, len(A)))
    cnt = A[idx, 0].sum(axis=1)
    out = {"n_events": int(len(A)), "n": int(A[:, 0].sum()), "n_boot": n_boot}
    for j, key in ((1, "brier_model_minus_mkt"), (2, "brier_blend_minus_mkt"),
                   (3, "ll_model_minus_mkt"), (4, "ll_blend_minus_mkt")):
        draws = A[idx, j].sum(axis=1) / cnt
        out[key] = {"mean": round(float(A[:, j].sum() / A[:, 0].sum()), 5),
                    "lo": round(float(np.quantile(draws, 0.025)), 5),
                    "hi": round(float(np.quantile(draws, 0.975)), 5),
                    "p_neg": round(float(np.mean(draws < 0)), 3)}
    return out


def _blend(p: float, m: float) -> float:
    def lg(x):
        x = min(max(x, 0.005), 0.995)
        return math.log(x / (1 - x))
    return 1.0 / (1.0 + math.exp(-0.5 * (lg(p) + lg(m))))


def default_split(rows: List[dict], events: Optional[List[dict]] = None) -> Optional[str]:
    """ET day: fit strictly before it, evaluate on/after. All station history
    before the first covered settled event when that leaves >= 3 days to fit
    on; otherwise the first ET midnight >= max(3 days, 40% of the history)."""
    if not rows:
        return None
    t0, t1 = rows[0]["t"] / 1000.0, rows[-1]["t"] / 1000.0

    def ceil_day(ts):
        day = C.et_day(ts)
        a, _ = C.et_day_bounds(day)
        return day if a >= ts else C.et_day(a + 86400 + 3600)
    evs = sorted(e["settle_ts"] for e in (events or []) if t0 <= e["settle_ts"] <= t1)
    if evs:
        first = C.et_day(evs[0])
        if C.et_day_bounds(first)[0] >= t0 + 3 * 86400.0:
            return first
    return ceil_day(t0 + max(3 * 86400.0, 0.4 * (t1 - t0)))


def run_eval(split_day: Optional[str] = None, wx_dir: Optional[str] = None,
             settled_dirs: Optional[List[str]] = None, iem_path: Optional[str] = None,
             step_min: int = 2, write: bool = True, verbose: bool = True,
             extra_models: Optional[Dict[str, FairModel]] = None) -> dict:
    rows, wx_files = load_wx_history(wx_dir)
    events, ev_sources = load_settled_events(settled_dirs)
    if not rows or not events:
        raise SystemExit(f"eval: need wx history ({len(rows)} rows) and settled events ({len(events)})")
    split_day = split_day or default_split(rows, events)
    split_ts = C.et_day_bounds(split_day)[0]
    split_ms = int(split_ts * 1000)
    cals = Calibrations.load()
    if verbose:
        print(f"eval: wx {C.iso(rows[0]['t'] / 1000)} .. {C.iso(rows[-1]['t'] / 1000)} "
              f"({len(rows)} rows); events {len(events)}; split {split_day} (fit before, eval on/after)",
              flush=True)
    P = fit_params(rows, iem_path=iem_path, until_ms=split_ms, step_min=step_min,
                   cals=cals, verbose=verbose)
    models: Dict[str, FairModel] = {"fitted": FairModel(P), "defaults": FairModel(default_params())}
    pf_note = None
    if os.path.exists(C.MODEL_PARAMS_FILE):
        mp = FairModel(None)
        if mp.params_source != "defaults":
            until = (mp.params.get("fit") or {}).get("until_ms")
            pf_note = "out-of-sample" if (until is not None and until <= split_ms) else "IN-SAMPLE (fit overlaps eval)"
            models["params_file"] = mp
    if extra_models:
        models.update(extra_models)
    wx_t0, wx_t1 = rows[0]["t"] / 1000.0, rows[-1]["t"] / 1000.0
    evs = [e for e in events if e["settle_ts"] >= split_ts
           and e["settle_ts"] - 3 * 3600 >= wx_t0 and e["settle_ts"] <= wx_t1]
    tape = WxTape.from_detailed(rows, calibrations=cals)
    finals = {int(r["t"]): float(r["v"]) for r in rows
              if r.get("v") is not None and r.get("status") in FINAL_STATUSES}
    queries = sorted((e["settle_ts"] - tau * 60.0, tau, ei)
                     for ei, e in enumerate(evs) for tau in EVAL_TAUS)
    recs: Dict[str, Dict[int, list]] = {m: {t: [] for t in EVAL_TAUS} for m in models}
    near: Dict[str, Dict[int, list]] = {m: {t: [] for t in EVAL_TAUS} for m in models}
    calib: Dict[int, List[int]] = {}
    # per (EST bucket, tau): squared standardized error of the settlement
    # value against the fitted mixture's TOTAL std (z_rms ~1 if calibrated)
    zcal: Dict[Tuple[str, int], List[float]] = {}
    fm_fit = models["fitted"]
    infl_fit = math.sqrt(1 - fm_fit.tail_w + fm_fit.tail_w * fm_fit.tail_k ** 2)
    stale = []
    for d, tau, ei in queries:
        e = evs[ei]
        snap = tape.snapshot(d)
        S = e["settle_ts"]
        mus = {}
        for name, mdl in models.items():
            ds = mdl.dist_lattice(snap, S)
            if ds is None:
                continue
            mus[name] = ds[0]
            for mk in e["markets"]:
                p = mdl.prob_above(ds[0], ds[1], mk["strike"], ds[2])
                lt = _last_trade(mk, d)
                pm = lt[0] if lt else None
                r = (p, pm, mk["y"], ei)
                recs[name][tau].append(r)
                if abs(mk["strike"] - mus.get("fitted", ds[0])) < 1.5:
                    near[name][tau].append(r)
                if name == "fitted":
                    calib.setdefault(min(int(p * 10), 9), []).append(mk["y"])
                    if lt:
                        stale.append(d - lt[1])
            if name == "fitted" and ds[1] > 0:
                yv = e.get("value")
                if yv is None:
                    yv = finals.get(int(math.floor(S / 60.0)) * 60000)
                if yv is not None:
                    b = bucket_of_hour(est_hour(S))
                    zcal.setdefault((b, tau), []).append(((yv - ds[0]) / (ds[1] * infl_fit)) ** 2)
    res = {"generated_at": C.iso(C.utcnow()), "split_day": split_day,
           "wx_files": len(wx_files), "wx_range": [C.iso(wx_t0), C.iso(wx_t1)],
           "event_sources": ev_sources, "events_total": len(events), "events_eval": len(evs),
           "eval_range": [C.iso(evs[0]["settle_ts"]), C.iso(evs[-1]["settle_ts"])] if evs else None,
           "params_file_note": pf_note,
           "fit": {k: P[k] for k in ("a", "b", "c", "e", "tail_w", "tail_k", "vol_gamma",
                                     "vol_gamma_short", "season_mult", "hour_mult")},
           "fit_meta": {k: P["fit"].get(k) for k in ("wx", "iem", "mean_coefs", "residual_rms_by_knot")},
           "sigma_core": P["sigma_core"],
           "market_price": "last REST trade before the decision time",
           "trade_age_s_median": round(sorted(stale)[len(stale) // 2], 1) if stale else None,
           "models": {}}
    for name in models:
        allr = [r for t in EVAL_TAUS for r in recs[name][t]]
        alln = [r for t in EVAL_TAUS for r in near[name][t]]
        res["models"][name] = {
            "overall": _metrics(allr), "near": _metrics(alln),
            "by_tau": {str(t): dict(_metrics(recs[name][t]), near=_metrics(near[name][t]))
                       for t in EVAL_TAUS},
            # event-clustered CIs: the strike-rows are ~n_events independent draws
            "bootstrap": {"overall": _bootstrap(allr),
                          "by_tau": {str(t): _bootstrap(recs[name][t]) for t in EVAL_TAUS}}}
    res["calibration_fitted"] = [{"bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v),
                                  "yes_rate": round(sum(v) / len(v), 4)}
                                 for b, v in sorted(calib.items()) if v]
    res["calibration_by_bucket"] = {
        b: {str(t): {"n": len(zcal[(b, t)]),
                     "z_rms": round(math.sqrt(sum(zcal[(b, t)]) / len(zcal[(b, t)])), 3)}
            for t in EVAL_TAUS if zcal.get((b, t))}
        for b in BUCKET_NAMES}
    res["calibration_by_bucket_note"] = ("rms of (settlement - mu) / total mixture std, "
                                         "fitted model; ~1 if calibrated, < 1 sigma too wide, "
                                         "> 1 too narrow")
    if write:
        out = C.data_path("model", f"eval_{C.et_day(C.utcnow())}.json")
        C.write_json_atomic(out, res)
        res["written"] = out
    if verbose:
        print_eval(res)
    return res


def print_eval(res: dict) -> None:
    print(f"\nEval split {res['split_day']}: {res['events_eval']} events "
          f"{res['eval_range']}; market = {res['market_price']}"
          f" (median trade age {res['trade_age_s_median']} s)")
    for name, m in res["models"].items():
        print(f"\n[{name}]" + (f" {res['params_file_note']}" if name == "params_file" else ""))
        print(f"{'tau':>5} {'n':>6} {'Brier mdl':>9} {'Brier mkt':>9} {'blend':>7} {'LL mdl':>7} {'LL mkt':>7} | "
              f"{'near n':>6} {'Bri mdl':>8} {'Bri mkt':>8} {'blend':>7} {'LL mdl':>7} {'LL mkt':>7} | {'Bri all':>7}")
        rows = [(t, m["by_tau"][t]) for t in m["by_tau"]] + [("all", dict(m["overall"], near=m["near"]))]
        for t, r in rows:
            nr = r["near"]
            def g(x, k):
                v = x.get(k)
                return f"{v:.4f}" if isinstance(v, float) else "   -  "
            print(f"{t:>5} {r['n']:>6} {g(r, 'brier_model'):>9} {g(r, 'brier_mkt'):>9} "
                  f"{g(r, 'brier_blend'):>7} {g(r, 'll_model'):>7} {g(r, 'll_mkt'):>7} | "
                  f"{nr['n']:>6} {g(nr, 'brier_model'):>8} {g(nr, 'brier_mkt'):>8} "
                  f"{g(nr, 'brier_blend'):>7} {g(nr, 'll_model'):>7} {g(nr, 'll_mkt'):>7} | "
                  f"{g(r, 'brier_model_all'):>7}")
    for name, m in res["models"].items():
        bs = m.get("bootstrap")
        if not bs:
            continue
        print(f"\n[{name}] Brier minus market, event-clustered bootstrap: mean [95% CI] P(<0)")
        for t, b in list(bs["by_tau"].items()) + [("all", bs["overall"])]:
            if "brier_model_minus_mkt" not in b:
                continue
            mm, bb = b["brier_model_minus_mkt"], b["brier_blend_minus_mkt"]
            print(f"{t:>5} events={b['n_events']:>4}  model {mm['mean']:+.4f} "
                  f"[{mm['lo']:+.4f}, {mm['hi']:+.4f}] {mm['p_neg']:.2f}   "
                  f"blend {bb['mean']:+.4f} [{bb['lo']:+.4f}, {bb['hi']:+.4f}] {bb['p_neg']:.2f}")
    if res.get("calibration_fitted"):
        print("\ncalibration (fitted): " + "  ".join(
            f"{c['bucket']}:{c['yes_rate']:.2f}(n={c['n']})" for c in res["calibration_fitted"]))
    cb = res.get("calibration_by_bucket")
    if cb:
        print("\nz_rms by EST bucket x tau (fitted; ~1 calibrated, <1 sigma too wide):")
        for b, row in cb.items():
            print(f"  {b:<9} " + " ".join(f"{t}:{v['z_rms']:.2f}(n={v['n']})" for t, v in row.items()))
    if res.get("written"):
        print(f"\nwrote {res['written']}")


# --------------------------------------------------------------------------
# Replica check
# --------------------------------------------------------------------------

def replica_check(rows: List[dict], cals: Optional[Calibrations] = None,
                  max_examples: int = 10) -> dict:
    """Replica (final codes: ok only) vs official v, by status."""
    cals = cals or Calibrations.load()
    by: Dict[str, dict] = {}
    ex = []
    for r in rows:
        v = r.get("v")
        st = r.get("status")
        if v is None or st not in FINAL_STATUSES:
            continue
        rep = replica_for_row(r, cals, codes=frozenset(("ok",)))
        b = by.setdefault(st, {"n": 0, "exact": 0, "none": 0})
        b["n"] += 1
        if rep is None:
            b["none"] += 1
        elif abs(rep - float(v)) < 0.005:
            b["exact"] += 1
            continue
        if len(ex) < max_examples:
            ex.append({"t": C.iso(int(r["t"]) / 1000), "status": st, "v": v, "replica": rep,
                       "cal": cals.for_minute(int(r["t"])).config_version,
                       "stations": [(s.get("station_id"), s.get("code"), s.get("source"),
                                     s.get("temp_f")) for s in r.get("stations") or []]})
    for b in by.values():
        b["rate"] = round(b["exact"] / b["n"], 6) if b["n"] else None
    known = {c.config_version for c in cals.cals}
    unknown = sorted({str(r["config_version"]) for r in rows
                      if r.get("config_version") and r["config_version"] not in known})
    return {"by_status": by, "examples": ex, "calibrations": cals.source,
            "unknown_config_versions": unknown}


def final_rows_between(t0: float, t1: float) -> List[dict]:
    """Final (official-v) rows per minute from the live wx stream with
    t0 <= rx < t1 (epoch s), last row per minute wins."""
    by_t: Dict[int, dict] = {}
    for r in C.iter_raw("wx", t0, t1):
        if r.get("t") is None:
            continue
        if r.get("v") is not None and r.get("status") in FINAL_STATUSES:
            by_t[int(r["t"])] = r
    return [by_t[t] for t in sorted(by_t)]


def replica_check_range(t0: float, t1: float, cals: Optional[Calibrations] = None) -> dict:
    """Replica match rate on the live recording for rx in [t0, t1) - for the
    daily report (e.g. C.et_day_bounds(day))."""
    return replica_check(final_rows_between(t0, t1), cals)


def _rows_for_day(day: str) -> List[dict]:
    """Final rows per minute from the live wx stream of a UTC day folder."""
    by_t: Dict[int, dict] = {}
    d = os.path.join(C.DATA_ROOT, "raw", day)
    for p in sorted(glob.glob(os.path.join(d, "wx_*.jsonl*"))):
        for r in C.iter_jsonl(p):
            if r.get("t") is None:
                continue
            if r.get("v") is not None and r.get("status") in FINAL_STATUSES:
                by_t[int(r["t"])] = r
    return [by_t[t] for t in sorted(by_t)]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _day_ms(day: Optional[str]) -> Optional[int]:
    return int(C.et_day_bounds(day)[0] * 1000) if day else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit", help="fit model params -> model/model_params.json")
    f.add_argument("--until", help="ET day; use data strictly before it")
    f.add_argument("--since", help="ET day; Kalshi targets on/after it")
    f.add_argument("--wx-dir")
    f.add_argument("--iem")
    f.add_argument("--step", type=int, default=2, help="target minute step")
    f.add_argument("--out", default=C.MODEL_PARAMS_FILE)
    r = sub.add_parser("replica-check", help="replica vs official v")
    r.add_argument("--dir")
    r.add_argument("--day", help="UTC day folder under raw/ (live recording)")
    e = sub.add_parser("eval", help="time-split calibration backtest vs market")
    e.add_argument("--split", help="ET day: fit before, evaluate on/after")
    e.add_argument("--wx-dir")
    e.add_argument("--settled-dir", action="append")
    e.add_argument("--iem")
    e.add_argument("--step", type=int, default=2)
    e.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "replica-check":
        if a.day:
            rows, src = _rows_for_day(a.day), os.path.join(C.DATA_ROOT, "raw", a.day)
        else:
            rows, files = load_wx_history(a.dir)
            src = os.path.dirname(files[0]) if files else a.dir
        res = replica_check(rows)
        print(f"replica-check {src}: {sum(b['n'] for b in res['by_status'].values())} final minutes "
              f"(calibrations: {res['calibrations']})")
        for st, b in sorted(res["by_status"].items()):
            print(f"  {st:<9} n={b['n']:>6} exact={b['exact']:>6} rate={b['rate']} "
                  f"(replica None: {b['none']})")
        for x in res["examples"]:
            print("  mismatch", json.dumps(x))
        if res["unknown_config_versions"]:
            print("  WARNING: rows name calibrations missing from our file "
                  f"{res['unknown_config_versions']} -> refresh backfill/calibrations.json "
                  "(GET /live_data/weather/miami/calibrations)")
        return 0
    if a.cmd == "fit":
        rows, files = load_wx_history(a.wx_dir)
        if not rows:
            print("fit: no Kalshi station history found", file=sys.stderr)
        P = fit_params(rows, iem_path=a.iem, until_ms=_day_ms(a.until),
                       since_ms=_day_ms(a.since), step_min=a.step)
        P["fit"]["wx_files"] = len(files)
        C.write_json_atomic(a.out, P)
        print(f"wrote {a.out}: a={P['a']} b={P['b']} c={P['c']} e={P['e']} tail_w={P['tail_w']} "
              f"tail_k={P['tail_k']} vol_gamma={P['vol_gamma']} "
              f"vol_gamma_short={P['vol_gamma_short']}")
        for b in BUCKET_NAMES:
            print(f"  sigma_core {b:<9} " + " ".join(f"{x:.3f}" for x in P["sigma_core"][b]))
        return 0
    if a.cmd == "eval":
        run_eval(a.split, a.wx_dir, a.settled_dir, a.iem, a.step, write=not a.no_write)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
