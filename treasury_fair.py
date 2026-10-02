#!/usr/bin/env python3
"""Live Treasury yields + touch-probability fairs for incentive_mm's Treasury
touch gate.

Jack 2026-10-01: the how-high / how-low ladders (KX{2,5,7,10,30}YRDIR{H,L}M,
and the 10Y weeklies KX10YRDIR{H,L}W) were quoted PLAIN -- no yield feed --
since the 9/30 Treasury allowlist, and were swept at 11:29-12:25 ET on 10/1
when yields fell 5-10bp (-$76 on 23 fills, marked). Jack has CNBC's
permission (told in chat 2026-10-02 ~01:00Z) to read its quote service for
this gate.

CONTRACT. "How low": YES if Treasury's Daily Par Yield Curve rate for the
tenor (the first published value; NY Fed indicative quotes at or near 3:30pm
ET; intraday values not considered) is BELOW the strike on ANY business day
in the window; "how high": ABOVE. Published to 2 dp, so low YES <=> fix <
K - 0.005 and high YES <=> fix >= K + 0.005. A strike can resolve early.

FEED. CNBC's quote service: real-time Tradeweb on-the-run yields, one
request for all five tenors. On 10/1 its closes sat within ~1bp of the
published par (10Y 5.234 vs 5.24, 30Y 5.603 vs 5.61).

FAIR. A driftless Gaussian random walk in yield from the live read: daily
sd per tenor from the last SIGMA_DAYS par-yield changes (treasury.gov, the
settlement source) widened x SIGMA_WIDEN and floored at SIGMA_FLOOR_BP; a
fix at 15:30 ET each business day (SIFMA full closes skipped) through the
market's close date, each with BASIS_SD_BP of independent CNBC-vs-par noise;
P(touch) by Monte Carlo (numpy, fixed seed so an unchanged input never
flickers). From 15:30 ET to midnight the day's fix is PENDING at the read
nearest 15:30.

GATE (verdict, the quote loop's per-strike call): stand aside -- fail
CLOSED -- with no feed, a stale read (READ_TTL_SECS), a stale tenor quote
(QUOTE_TTL_SESSION_MIN 08:00-17:00 ET weekdays, QUOTE_TTL_OFF_MIN other
weekday hours), inside a weekday release window (RELEASE_WINDOWS_ET: the
08:30 and 10:00 ET prints), while the tenor is MOVING (a >= MOVE_BP range
within MOVE_WINDOW_MIN freezes it FREEZE_MIN), NEAR the money (|live -
K_eff| <= NEAR_BP), DECIDED (fair < MIN_P or > 1 - MIN_P) or when the touch
fights the fair by more than FAIR_TOL_CENTS on the adverse side.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pytz
import requests

try:
    import numpy as np
except Exception:  # pragma: no cover - the gate fails closed without it
    np = None

ET = pytz.timezone("US/Eastern")
CNBC_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
TENOR_SYMBOLS = {2: "US2Y", 5: "US5Y", 7: "US7Y", 10: "US10Y", 30: "US30Y"}
PAR_COLUMNS = {2: "2 Yr", 5: "5 Yr", 7: "7 Yr", 10: "10 Yr", 30: "30 Yr"}
TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all?type="
                "daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
UA = {"User-Agent": "KL-IMM-treasury-gate/1.0"}
# KX10YRDIRLM = 10-year, how LOW, Monthly; KX10YRDIRHW = how HIGH, Weekly
SERIES_RE = re.compile(r"^KX(2|5|7|10|30)YRDIR([HL])([MW])$")
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


POLL_SECS = _env_float("IMM_TREASURY_POLL_SECS", 10)
POLL_SECS_CLOSED = _env_float("IMM_TREASURY_POLL_SECS_CLOSED", 60)
READ_TTL_SECS = _env_float("IMM_TREASURY_READ_TTL_SECS", 60)
QUOTE_TTL_SESSION_MIN = _env_float("IMM_TREASURY_QUOTE_TTL_SESSION_MIN", 10)
QUOTE_TTL_OFF_MIN = _env_float("IMM_TREASURY_QUOTE_TTL_OFF_MIN", 90)
RELEASE_WINDOWS_ET = os.environ.get("IMM_TREASURY_RELEASE_WINDOWS_ET",
                                    "08:25-08:45,09:55-10:10")
MOVE_BP = _env_float("IMM_TREASURY_MOVE_BP", 3.0)
MOVE_WINDOW_MIN = _env_float("IMM_TREASURY_MOVE_WINDOW_MIN", 10)
FREEZE_MIN = _env_float("IMM_TREASURY_FREEZE_MIN", 10)
NEAR_BP = _env_float("IMM_TREASURY_NEAR_BP", 2.0)
FAIR_TOL_CENTS = _env_float("IMM_TREASURY_FAIR_TOL_CENTS", 10)
MIN_P = _env_float("IMM_TREASURY_MIN_P", 0.03)
SIGMA_DAYS = int(_env_float("IMM_TREASURY_SIGMA_DAYS", 60))
SIGMA_WIDEN = _env_float("IMM_TREASURY_SIGMA_WIDEN", 1.25)
SIGMA_FLOOR_BP = _env_float("IMM_TREASURY_SIGMA_FLOOR_BP", 4.0)
SIGMA_DEFAULT_BP = {2: 7.0, 5: 7.0, 7: 7.0, 10: 6.5, 30: 6.0}
BASIS_SD_BP = _env_float("IMM_TREASURY_BASIS_SD_BP", 1.0)
MC_PATHS = int(_env_float("IMM_TREASURY_MC_PATHS", 4000))
# the ladders are monthly / weekly; a longer window has no fair (stand aside)
MAX_WINDOW_DAYS = int(_env_float("IMM_TREASURY_MAX_WINDOW_DAYS", 45))
FIX_ET = (15, 30)
# SIFMA full closes (no par yields published)
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 "
    "2026-07-03 2026-09-07 2026-10-12 2026-11-11 2026-11-26 2026-12-25 "
    "2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 "
    "2027-07-05 2027-09-06 2027-10-11 2027-11-11 2027-11-25 2027-12-24").split())


def touch_series(series: str) -> Optional[Tuple[int, str]]:
    """'KX10YRDIRLM' -> (10, 'L'); None for anything else."""
    m = SERIES_RE.match(series or "")
    return (int(m.group(1)), m.group(2)) if m else None


def strike_of(ticker: str) -> Optional[float]:
    seg = (ticker or "").rsplit("-", 1)[-1]
    if not seg.startswith("T"):
        return None
    try:
        k = float(seg[1:])
    except ValueError:
        return None
    return k if math.isfinite(k) else None


def last_day_of(event_ticker: str, close_time: Optional[datetime] = None) -> Optional[date]:
    """The window's last day: the close's ET date, else the ticker's YYMMMDD
    ('KX10YRDIRLM-26OCT30L' -> 2026-10-30)."""
    if close_time is not None:
        return close_time.astimezone(ET).date()
    seg = (event_ticker or "").split("-")
    if len(seg) < 2:
        return None
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})", seg[1])
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3)))
    except (KeyError, ValueError):
        return None


def business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def fix_at(d: date) -> datetime:
    return ET.localize(datetime(d.year, d.month, d.day, *FIX_ET)).astimezone(timezone.utc)


def fix_schedule(now: datetime, last_day: date) -> Tuple[bool, List[float]]:
    """(pending, steps): whether today's fix is PENDING (business day, at or
    after 15:30 ET), and the variance weights (business days) of each FUTURE
    fix through last_day -- the first is the fraction of a day until it
    (a whole day when a closed day lies between), the rest one each."""
    now = now.astimezone(timezone.utc)
    today = now.astimezone(ET).date()
    pending = business_day(today) and today <= last_day and now >= fix_at(today)
    steps: List[float] = []
    d, prev = today, None
    while d <= last_day:
        if business_day(d) and fix_at(d) > now:
            if prev is None:
                gap = any(not business_day(today + timedelta(days=i))
                          for i in range((d - today).days))
                frac = (fix_at(d) - now).total_seconds() / 86400.0
                steps.append(1.0 if gap else min(1.0, max(0.02, frac)))
            else:
                steps.append(1.0)
            prev = d
        d += timedelta(days=1)
    return pending, steps


def extremes(y0: float, steps: List[float], sigma: float, kind: str,
             pending_y: Optional[float] = None, n: int = MC_PATHS,
             basis_sd: float = BASIS_SD_BP / 100.0, seed: int = 20261001):
    """Sorted per-path extreme (min for 'L', max for 'H') of the fixes, in %."""
    rng = np.random.default_rng(seed)
    cols = []
    if pending_y is not None:
        cols.append(pending_y + basis_sd * rng.standard_normal(n))
    if steps:
        z = rng.standard_normal((n, len(steps))) * (sigma * np.sqrt(np.asarray(steps)))
        path = y0 + np.cumsum(z, axis=1)
        path = path + basis_sd * rng.standard_normal(path.shape)
        cols.extend(path.T)
    if not cols:
        return None
    fixes = np.vstack(cols)
    ext = fixes.min(axis=0) if kind == "L" else fixes.max(axis=0)
    return np.sort(ext)


def p_touch(ext_sorted, k: float, kind: str) -> Optional[float]:
    """P(YES) from the sorted extremes: low <=> fix < K - 0.005; high <=>
    fix >= K + 0.005 (2-dp publication)."""
    if ext_sorted is None or len(ext_sorted) == 0:
        return None
    n = len(ext_sorted)
    if kind == "L":
        return float(np.searchsorted(ext_sorted, k - 0.005, side="left")) / n
    return 1.0 - float(np.searchsorted(ext_sorted, k + 0.005, side="left")) / n


def parse_cnbc(js: dict) -> Dict[int, dict]:
    """CNBC restQuote JSON -> {tenor: {'y': %, 'ts': epoch of the quote,
    'status': curmktstatus, 'realtime': bool}}."""
    q = (js or {}).get("FormattedQuoteResult", {}).get("FormattedQuote") or []
    q = q if isinstance(q, list) else [q]
    by_sym = {v: k for k, v in TENOR_SYMBOLS.items()}
    out: Dict[int, dict] = {}
    for x in q:
        tenor = by_sym.get(str(x.get("symbol")))
        if tenor is None:
            continue
        try:
            y = float(str(x.get("last", "")).replace("%", "").strip())
            ts = datetime.strptime(str(x.get("last_time")), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
        except (TypeError, ValueError):
            continue
        if math.isfinite(y) and 0.0 < y < 25.0:
            out[tenor] = {"y": y, "ts": ts, "status": str(x.get("curmktstatus") or ""),
                          "realtime": str(x.get("realTime")).lower() == "true"}
    return out


def fetch_cnbc(timeout: float = 10) -> Dict[int, dict]:
    r = requests.get(CNBC_URL, params={
        "symbols": "|".join(TENOR_SYMBOLS.values()), "requestMethod": "itv",
        "noform": "1", "partnerId": "2", "fund": "1", "exthrs": "1",
        "output": "json", "events": "1"}, headers=UA, timeout=timeout)
    r.raise_for_status()
    out = parse_cnbc(r.json())
    if not out:
        raise RuntimeError("CNBC returned no Treasury quotes")
    return out


def sigma_from_par_csv(texts: List[str], days: int = SIGMA_DAYS) -> Dict[int, float]:
    """Daily sd (in %) of the par-yield changes per tenor over the last
    `days` business days, from treasury.gov's yearly CSVs."""
    rows: Dict[str, Dict[int, float]] = {}
    for text in texts:
        for r in csv.DictReader(io.StringIO(text)):
            try:
                d = datetime.strptime(r["Date"], "%m/%d/%Y").date().isoformat()
            except (KeyError, ValueError):
                continue
            vals = {}
            for t, col in PAR_COLUMNS.items():
                try:
                    vals[t] = float(r[col])
                except (KeyError, TypeError, ValueError):
                    pass
            rows[d] = vals
    dates = sorted(rows)[-(days + 1):]
    out: Dict[int, float] = {}
    for t in PAR_COLUMNS:
        ch = [rows[b][t] - rows[a][t] for a, b in zip(dates, dates[1:])
              if t in rows[a] and t in rows[b]]
        if len(ch) >= 20:
            m = sum(ch) / len(ch)
            out[t] = math.sqrt(sum((c - m) ** 2 for c in ch) / (len(ch) - 1))
    return out


def fetch_sigma(now: Optional[datetime] = None, timeout: float = 30) -> Dict[int, float]:
    now = now or datetime.now(timezone.utc)
    texts = []
    for year in sorted({now.year - 1, now.year}):
        r = requests.get(TREASURY_CSV.format(year=year), headers=UA, timeout=timeout)
        r.raise_for_status()
        texts.append(r.text)
    return sigma_from_par_csv(texts)


def _in_windows(now: datetime, spec: str) -> bool:
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    m = et.hour * 60 + et.minute
    for part in (spec or "").split(","):
        try:
            a, b = part.strip().split("-")
            lo = int(a[:2]) * 60 + int(a[3:5])
            hi = int(b[:2]) * 60 + int(b[3:5])
        except ValueError:
            continue
        if lo <= m < hi:
            return True
    return False


def quote_ttl_min(now: datetime) -> Optional[float]:
    """How old a tenor quote may be: the NY session (08:00-17:00 ET
    weekdays) QUOTE_TTL_SESSION_MIN, other weekday hours QUOTE_TTL_OFF_MIN,
    and no limit while the market is shut (Fri 17:00 -> Sun 18:00 ET, and
    SIFMA full closes) -- the yield cannot move then."""
    et = now.astimezone(ET)
    wd, m = et.weekday(), et.hour * 60 + et.minute
    if (wd == 4 and m >= 17 * 60) or wd == 5 or (wd == 6 and m < 18 * 60) \
            or et.date() in HOLIDAYS:
        return None
    if wd < 5 and 8 * 60 <= m < 17 * 60:
        return QUOTE_TTL_SESSION_MIN
    return QUOTE_TTL_OFF_MIN


class YieldWatch:
    """The refresher thread's in-memory view of the live yields; the quote
    loop asks verdict() per strike."""

    def __init__(self):
        self.lock = threading.Lock()
        self.quotes: Dict[int, dict] = {}
        self.read_at = 0.0
        self.hist: Dict[int, deque] = {t: deque(maxlen=720) for t in TENOR_SYMBOLS}
        self.freeze_until: Dict[int, float] = {t: 0.0 for t in TENOR_SYMBOLS}
        self.fix_est: Dict[int, Tuple[str, float, float]] = {}   # tenor -> (ET date, y, |dt| s)
        self.sigma: Dict[int, float] = {}
        self.sigma_day: Optional[str] = None
        self._ext_cache: Dict[tuple, object] = {}

    def update(self, quotes: Dict[int, dict], read_ts: float) -> List[str]:
        """Take a read; returns one note per tenor that just froze."""
        notes = []
        now = datetime.fromtimestamp(read_ts, timezone.utc)
        et = now.astimezone(ET)
        fix_ts = fix_at(et.date()).timestamp()
        with self.lock:
            self.read_at = read_ts
            for t, q in quotes.items():
                self.quotes[t] = q
                h = self.hist[t]
                h.append((read_ts, q["y"]))
                lo_ts = read_ts - MOVE_WINDOW_MIN * 60
                ys = [y for ts, y in h if ts >= lo_ts]
                if ys and (max(ys) - min(ys)) * 100 >= MOVE_BP - 1e-9:
                    if read_ts >= self.freeze_until[t]:
                        notes.append(f"{TENOR_SYMBOLS[t]} moved "
                                     f"{(max(ys) - min(ys)) * 100:.1f}bp in "
                                     f"{MOVE_WINDOW_MIN:g}m")
                    self.freeze_until[t] = read_ts + FREEZE_MIN * 60
                # the read nearest today's 15:30 ET fix (within 15 min)
                if business_day(et.date()):
                    dt_s = abs(read_ts - fix_ts)
                    cur = self.fix_est.get(t)
                    if dt_s <= 900 and (cur is None or cur[0] != et.date().isoformat()
                                        or dt_s < cur[2]):
                        self.fix_est[t] = (et.date().isoformat(), q["y"], dt_s)
        return notes

    def set_sigma(self, sig: Dict[int, float], day: str) -> None:
        with self.lock:
            self.sigma = dict(sig)
            self.sigma_day = day
            self._ext_cache.clear()

    def sigma_of(self, tenor: int) -> float:
        s = self.sigma.get(tenor)
        if s is None:
            s = SIGMA_DEFAULT_BP[tenor] / 100.0 / SIGMA_WIDEN
        return max(SIGMA_FLOOR_BP / 100.0, s * SIGMA_WIDEN)

    def fair(self, tenor: int, kind: str, k: float, last_day: date,
             now: datetime) -> Optional[float]:
        with self.lock:
            q = self.quotes.get(tenor)
            fe = self.fix_est.get(tenor)
            sigma = self.sigma_of(tenor)
        if q is None or np is None or (last_day - now.astimezone(ET).date()).days > MAX_WINDOW_DAYS:
            return None
        pending, steps = fix_schedule(now, last_day)
        today = now.astimezone(ET).date().isoformat()
        pending_y = None
        if pending:
            pending_y = fe[1] if fe and fe[0] == today else q["y"]
        key = (tenor, kind, last_day.isoformat(), round(q["y"], 4),
               None if pending_y is None else round(pending_y, 4),
               len(steps), round(steps[0], 2) if steps else None, round(sigma, 5))
        ext = self._ext_cache.get(key)
        if ext is None:
            ext = extremes(q["y"], steps, sigma, kind, pending_y)
            if len(self._ext_cache) > 256:
                self._ext_cache.clear()
            self._ext_cache[key] = ext
        return p_touch(ext, k, kind)

    def verdict(self, series: str, ticker: str, now_ts: float,
                ext_bid: Optional[float], ext_ask: Optional[float],
                close_time: Optional[datetime] = None) -> Tuple[str, dict]:
        """('', {}) when the strike may quote, else (why, guard inputs)."""
        tk = touch_series(series)
        k = strike_of(ticker)
        last_day = last_day_of(ticker.rsplit("-", 1)[0], close_time)
        if tk is None or k is None or last_day is None:
            return f"unparseable Treasury touch ticker {ticker}", {"reason": "ticker"}
        tenor, kind = tk
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        with self.lock:
            read_age = now_ts - self.read_at
            q = self.quotes.get(tenor)
            frozen = now_ts < self.freeze_until.get(tenor, 0.0)
        if read_age > READ_TTL_SECS:
            return (f"CNBC read is {read_age:.0f}s old", {"reason": "stale_read",
                                                          "age_s": round(read_age)})
        if q is None:
            return f"no CNBC quote for {TENOR_SYMBOLS[tenor]}", {"reason": "no_quote"}
        ttl = quote_ttl_min(now)
        if ttl is not None and now_ts - q["ts"] > ttl * 60:
            return (f"{TENOR_SYMBOLS[tenor]} last traded "
                    f"{(now_ts - q['ts']) / 60:.0f}m ago", {"reason": "stale_quote"})
        if _in_windows(now, RELEASE_WINDOWS_ET):
            return "release window", {"reason": "release"}
        if frozen:
            return (f"{TENOR_SYMBOLS[tenor]} moving (>= {MOVE_BP:g}bp in "
                    f"{MOVE_WINDOW_MIN:g}m)", {"reason": "moving", "y": q["y"]})
        k_eff = k - 0.005 if kind == "L" else k + 0.005
        if abs(q["y"] - k_eff) * 100 <= NEAR_BP:
            return (f"{TENOR_SYMBOLS[tenor]} {q['y']:.3f}% within {NEAR_BP:g}bp "
                    f"of {k:g}", {"reason": "near", "y": q["y"]})
        p = self.fair(tenor, kind, k, last_day, now)
        if p is None:
            return "no fair", {"reason": "no_fair"}
        fair_c = p * 100.0
        inputs = {"y": q["y"], "fair": round(fair_c, 2), "sigma_bp": round(
            self.sigma_of(tenor) * 100, 2)}
        if p < MIN_P or p > 1.0 - MIN_P:
            return (f"decided: fair {fair_c:.0f}c ({TENOR_SYMBOLS[tenor]} "
                    f"{q['y']:.3f}% vs {k:g})", dict(inputs, reason="decided"))
        bid_bad = ext_bid is not None and ext_bid > fair_c + FAIR_TOL_CENTS
        ask_bad = ext_ask is not None and ext_ask < fair_c - FAIR_TOL_CENTS
        if bid_bad or ask_bad:
            return (f"book {ext_bid}x{ext_ask} vs fair {fair_c:.0f}c (tol "
                    f"{FAIR_TOL_CENTS:g}c, {'bid' if bid_bad else 'ask'} side; "
                    f"{TENOR_SYMBOLS[tenor]} {q['y']:.3f}%)",
                    dict(inputs, reason="band", bid_bad=bid_bad, ask_bad=ask_bad))
        return "", {}

    def status(self) -> dict:
        with self.lock:
            return {"read_at": datetime.fromtimestamp(self.read_at, timezone.utc).isoformat(
                        timespec="seconds") if self.read_at else None,
                    "quotes": {TENOR_SYMBOLS[t]: {"y": q["y"], "ts": datetime.fromtimestamp(
                        q["ts"], timezone.utc).isoformat(timespec="seconds"),
                        "status": q["status"]} for t, q in self.quotes.items()},
                    "sigma_bp": {TENOR_SYMBOLS[t]: round(self.sigma_of(t) * 100, 2)
                                 for t in TENOR_SYMBOLS},
                    "sigma_day": self.sigma_day,
                    "frozen": {TENOR_SYMBOLS[t]: datetime.fromtimestamp(u, timezone.utc).isoformat(
                        timespec="seconds") for t, u in self.freeze_until.items()
                        if u > time.time()},
                    "fix_est": {TENOR_SYMBOLS[t]: v[:2] for t, v in self.fix_est.items()}}


def poll_secs(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    return POLL_SECS if quote_ttl_min(now) is not None else POLL_SECS_CLOSED


if __name__ == "__main__":
    w = YieldWatch()
    w.update(fetch_cnbc(), time.time())
    try:
        w.set_sigma(fetch_sigma(), datetime.now(timezone.utc).date().isoformat())
    except Exception as e:  # noqa: BLE001
        print("sigma fetch failed, defaults:", e)
    print(json.dumps(w.status(), indent=1))
