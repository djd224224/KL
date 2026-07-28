#!/usr/bin/env python3
"""
gas_bot.py — Model-driven trader for Kalshi AAA gas-price markets
(KXAAAGASD daily / KXAAAGASW weekly / KXAAAGASM monthly).

The settlement variable is the AAA national average (gasprices.aaa.com),
published once per day ~3-4am ET. It is a heavily smoothed series (daily
moves ~0.1-1c, strongly trending), while Kalshi strikes are 0.5-2c apart, so
near-dated buckets are largely forecastable.

Model: settle_value = last_print + drift*h + eps
  - drift = EWMA (half-life GAS_DRIFT_HALFLIFE_DAYS) of recent daily deltas,
    clamped to +/-GAS_DRIFT_MAX_CENTS_PER_DAY.
  - eps ~ Normal(0, sigma_h), sigma_h = sigma1 * h^power calibrated at
    startup from gas_data/aaa_history.csv (built by gas_data.py).
  - P(settle > K) via the normal CDF -> fair cents per strike.

Two modes:
  carry (default): loop every GAS_POLL_SECS. Post-only maker orders (YES or
    NO) wherever |fair - price| >= GAS_EDGE_MAKE_CENTS, subject to caps.
  snipe: within the ET window GAS_SNIPE_WINDOW (default 02:50-04:40), poll
    the AAA page every GAS_SNIPE_POLL_SECS. The moment today's print
    appears, recompute fairs and take mispriced quotes with IOC limit
    orders (edge >= GAS_EDGE_TAKE_CENTS net of taker fees), then exit.

Risk rails (all env-overridable, prefix GAS_):
  - total open-exposure budget, per-event budget, per-strike contract cap
  - per-ET-day new-cost cap; balance floor on the shared account
  - stale-data refusal: no trading if the newest AAA print is older than
    GAS_STALE_PRINT_MAX_AGE_HOURS
  - HALT file (gas_data/HALT) stops placements and cancels resting orders
  - only orders with client_order_id prefix 'gas-' are ever cancelled;
    Jack's manual orders are never touched.

SAFETY: DRY RUN by default — reads are live, writes are logged only.
Pass --live to place real orders.

Usage:
    python gas_bot.py --status              # fair-value vs market table
    python gas_bot.py --calibrate           # model calibration diagnostics
    python gas_bot.py --once                # one dry carry cycle
    python gas_bot.py --mode snipe          # dry snipe (waits for window)
    python gas_bot.py --live                # live carry loop
    python gas_bot.py --live --mode snipe   # live snipe window
    python gas_bot.py --cancel-all          # cancel resting gas- orders (real)
"""

import argparse
import json
import math
import os
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple

import pytz

import gas_data
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient, HttpError

MODEL_VERSION = "gas_bot_v1.0"
RUN_ID = uuid.uuid4().hex[:8]
ORDER_PREFIX = "gas"          # client_order_ids: gas-<run>-<hex>

ET = pytz.timezone("US/Eastern")

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")

# ----------------------------------------------------------------------------
# Config (env-overridable, prefix GAS_)
# ----------------------------------------------------------------------------

def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SERIES = [s.strip() for s in os.environ.get(
    "GAS_SERIES", "KXAAAGASD,KXAAAGASW,KXAAAGASM").split(",") if s.strip()]

POLL_SECS = _env_i("GAS_POLL_SECS", 300)
SNIPE_POLL_SECS = _env_i("GAS_SNIPE_POLL_SECS", 20)
SNIPE_WINDOW = os.environ.get("GAS_SNIPE_WINDOW", "02:45-07:30")   # ET

EDGE_MAKE_CENTS = _env_f("GAS_EDGE_MAKE_CENTS", 4.0)
EDGE_TAKE_CENTS = _env_f("GAS_EDGE_TAKE_CENTS", 6.0)   # net of taker fee
SIZE_CONTRACTS = _env_i("GAS_SIZE_CONTRACTS", 10)      # maker order size
# Snipe sizing (Jack 2026-07-28): per-MORNING contract envelope, spread
# best-net-edge-first in per-strike units. All same-morning takes are one
# correlated bet on the same AAA path, so the envelope (not any per-event
# split) is the risk unit; unit-per-strike spreads it across strikes instead
# of concentrating on the single most model-fragile point. Envelope counts
# PLACEMENTS (conservative: an unfilled IOC still consumes it).
SNIPE_MORNING_CAP = _env_i("GAS_SNIPE_MORNING_CAP", 5)
SNIPE_UNIT_CONTRACTS = _env_i("GAS_SNIPE_UNIT_CONTRACTS", 1)

MAX_PER_STRIKE = _env_i("GAS_MAX_CONTRACTS_PER_STRIKE", 50)
BUDGET_DOLLARS = _env_f("GAS_BUDGET_DOLLARS", 500.0)
EVENT_BUDGET_DOLLARS = _env_f("GAS_EVENT_BUDGET_DOLLARS", 250.0)
DAILY_COST_CAP_DOLLARS = _env_f("GAS_DAILY_COST_CAP", 300.0)
BALANCE_FLOOR_DOLLARS = _env_f("GAS_BALANCE_FLOOR", 2500.0)

PRICE_MIN = _env_i("GAS_PRICE_MIN", 2)     # never quote/take outside [min,max]
PRICE_MAX = _env_i("GAS_PRICE_MAX", 98)

DRIFT_MAX_CPD = _env_f("GAS_DRIFT_MAX_CENTS_PER_DAY", 1.5)
MODEL_WEIGHT = _env_f("GAS_MODEL_WEIGHT", 0.5)       # carry: model vs market mid
MAX_DISAGREE_CENTS = _env_f("GAS_MAX_DISAGREE_CENTS", 25.0)
SIGMA_MIN_CENTS = _env_f("GAS_SIGMA_MIN_CENTS", 0.25)
SIGMA_PAD = _env_f("GAS_SIGMA_PAD", 1.15)  # multiply calibrated sigma (fat tails)
MAX_HORIZON_DAYS = _env_i("GAS_MAX_HORIZON_DAYS", 8)
STALE_PRINT_MAX_AGE_HOURS = _env_f("GAS_STALE_PRINT_MAX_AGE_HOURS", 30.0)

ORDER_TTL_SECS = _env_i("GAS_ORDER_TTL_SECS", 3600)
MAX_ORDERS_PER_CYCLE = _env_i("GAS_MAX_ORDERS_PER_CYCLE", 40)

STATUS_PATH = os.path.join(gas_data.DATA_DIR, "status_gas_bot.json")
HALT_PATH = os.path.join(gas_data.DATA_DIR, "HALT")

ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_RECIPIENTS = [r.strip() for r in os.environ.get(
    "GAS_ALERT_RECIPIENTS", "jackdu224@gmail.com").split(",") if r.strip()]


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [gas] {msg}", flush=True)


# ----------------------------------------------------------------------------
# Alerting (repo Gmail-SMTP convention)
# ----------------------------------------------------------------------------

class Alerter:
    def __init__(self, live: bool):
        self.live = live
        self.enabled = bool(ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD and ALERT_RECIPIENTS)
        self.last_sent: Dict[str, float] = {}
        if not self.enabled:
            log("alerting LOG-ONLY: set ALERT_EMAIL_FROM + ALERT_EMAIL_PASSWORD")

    def send(self, subject: str, body: str, dedupe_key: str = "",
             dedupe_secs: float = 3600.0) -> None:
        mode = "LIVE" if self.live else "DRY"
        log(f"ALERT [{subject}] {body}")
        if not self.enabled:
            return
        now = time.time()
        if dedupe_key:
            last = self.last_sent.get(dedupe_key)
            if last is not None and now - last < dedupe_secs:
                return
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"[gas {mode}] {subject}"
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = ", ".join(ALERT_RECIPIENTS)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                server.sendmail(ALERT_EMAIL_FROM, ALERT_RECIPIENTS, msg.as_string())
            if dedupe_key:
                self.last_sent[dedupe_key] = now
        except Exception as e:
            log(f"! alert send failed: {e}")


# ----------------------------------------------------------------------------
# Model: drift + horizon sigma from the AAA history
# ----------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class DriftParams:
    halflife: float     # EWMA half-life, days
    lam_last: float     # extra weight on the single most recent delta
    rho: float          # per-day drift decay when projecting forward


@dataclass
class Calibration:
    sigma1_cents: float
    power: float
    n_days: int
    drift: DriftParams
    table: List[Tuple[int, float, int]]    # (h, sigma_cents, n)

    def sigma_cents(self, h: float) -> float:
        s = self.sigma1_cents * (max(h, 1.0) ** self.power) * SIGMA_PAD
        return max(s, SIGMA_MIN_CENTS)


def _drift_cpd(deltas: List[Tuple[date, float]], asof: date,
               p: DriftParams) -> float:
    """Drift estimate in cents/day as of `asof`, using only deltas known then.
    EWMA over recent deltas, optionally blended toward the most recent one
    (turning points: retail gas trends flip on wholesale reversals faster
    than a symmetric EWMA reacts)."""
    num = den = 0.0
    last_day = None
    last_dc = 0.0
    for day, dc in deltas:
        if day > asof:
            continue
        age = (asof - day).days
        w = 0.5 ** (age / p.halflife)
        num += w * dc
        den += w
        if last_day is None or day > last_day:
            last_day, last_dc = day, dc
    if den <= 0:
        return 0.0
    mu = num / den
    if last_day is not None and (asof - last_day).days <= 1:
        mu = (1.0 - p.lam_last) * mu + p.lam_last * last_dc
    return max(-DRIFT_MAX_CPD, min(DRIFT_MAX_CPD, mu))


def _cum_drift(mu_cpd: float, h: int, rho: float) -> float:
    """Cumulative expected move (cents) over h days with per-day decay rho:
    E[delta_i] = mu * rho^(i-1)."""
    if h <= 0:
        return 0.0
    if rho >= 0.999:
        return mu_cpd * h
    return mu_cpd * (1.0 - rho ** h) / (1.0 - rho)


def build_deltas(values: List[Tuple[date, float]]) -> List[Tuple[date, float]]:
    """Consecutive-calendar-day deltas in cents, keyed by the later day."""
    by_date = dict(values)
    out = []
    for d, v in values:
        prev = by_date.get(d - timedelta(days=1))
        if prev is not None:
            out.append((d, (v - prev) * 100.0))
    return out


DRIFT_GRID = [DriftParams(hl, lam, rho)
              for hl in (1.5, 3.0, 5.0, 10.0)
              for lam in (0.0, 0.3, 0.6)
              for rho in (0.6, 0.8, 1.0)]


def _residuals(values, by_date, deltas, p: DriftParams,
               horizons) -> Dict[int, List[float]]:
    resid: Dict[int, List[float]] = {h: [] for h in horizons}
    for d, v in values:
        mu = _drift_cpd(deltas, d, p)
        for h in horizons:
            tgt = by_date.get(d + timedelta(days=h))
            if tgt is None:
                continue
            pred = v + _cum_drift(mu, h, p.rho) / 100.0
            resid[h].append((tgt - pred) * 100.0)
    return resid


def calibrate(values: List[Tuple[date, float]],
              max_h: int = 7) -> Calibration:
    """Pick drift params by walk-forward forecast error (h=1..3 combined RMS),
    then measure sigma_h about the chosen model for h=1..max_h.

    Selection and sigma share one history, so sigma is mildly optimistic —
    SIGMA_PAD (x1.15 default) is the offsetting conservatism."""
    by_date = dict(values)
    deltas = build_deltas(values)
    if len(deltas) < 20:
        raise RuntimeError(
            f"only {len(deltas)} consecutive-day deltas in history; need >= 20 "
            "(run gas_data.py backfill-wayback first)")

    env_hl = os.environ.get("GAS_DRIFT_HALFLIFE_DAYS")
    env_lam = os.environ.get("GAS_DRIFT_LAM_LAST")
    env_rho = os.environ.get("GAS_DRIFT_RHO")
    if env_hl or env_lam or env_rho:
        best = DriftParams(float(env_hl or 3.0), float(env_lam or 0.3),
                           float(env_rho or 0.8))
    else:
        best, best_score = None, None
        for p in DRIFT_GRID:
            resid = _residuals(values, by_date, deltas, p, (1, 2, 3))
            n = sum(len(r) for r in resid.values())
            if n < 30:
                continue
            score = sum(x * x for r in resid.values() for x in r) / n
            if best_score is None or score < best_score:
                best, best_score = p, score
        if best is None:
            raise RuntimeError("not enough overlapping history to fit drift model")

    resid = _residuals(values, by_date, deltas, best, range(1, max_h + 1))
    table = []
    xs, ys, ws = [], [], []
    for h in range(1, max_h + 1):
        r = resid[h]
        if len(r) < 8:
            continue
        # RMS about the model (mean NOT subtracted: an uncorrected mean bias
        # is real forecast error and must widen the distribution)
        rms = math.sqrt(sum(x * x for x in r) / len(r))
        table.append((h, rms, len(r)))
        xs.append(math.log(h))
        ys.append(math.log(max(rms, 1e-6)))
        ws.append(float(len(r)))
    if len(table) < 3:
        raise RuntimeError("not enough horizons with data to calibrate sigma")
    # weighted least squares: log sigma = log s1 + power * log h
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    var = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    power = cov / var if var > 0 else 0.5
    power = max(0.3, min(1.2, power))
    sigma1 = math.exp(my - power * mx)
    return Calibration(sigma1_cents=sigma1, power=power,
                       n_days=len(values), drift=best, table=table)


@dataclass
class Snapshot:
    """Latest AAA print + model state used for all fair values this cycle."""
    asof: date
    value: float
    drift_cpd: float
    calib: Calibration

    def fair_cents(self, strike: float, settle_day: date) -> Optional[float]:
        h = (settle_day - self.asof).days
        if h < 0 or h > MAX_HORIZON_DAYS:
            return None
        if h == 0:
            # settlement print already published: deterministic
            return 100.0 if self.value > strike else 0.0
        vhat = self.value + _cum_drift(self.drift_cpd, h, self.calib.drift.rho) / 100.0
        sig = self.calib.sigma_cents(h) / 100.0
        p = 1.0 - _norm_cdf((strike - vhat) / sig)
        return 100.0 * p


def load_snapshot(require_fresh: bool) -> Snapshot:
    rows = gas_data.load_history()
    values = gas_data.series_values(rows)
    if not values:
        raise RuntimeError("empty AAA history — run gas_data.py backfills")
    calib = calibrate(values)
    asof, value = values[-1]
    age_h = (datetime.now(ET) - ET.localize(
        datetime.combine(asof, datetime.min.time()).replace(hour=4))
    ).total_seconds() / 3600.0
    if require_fresh and age_h > STALE_PRINT_MAX_AGE_HOURS:
        raise RuntimeError(
            f"newest AAA print {asof} is ~{age_h:.0f}h old (> "
            f"{STALE_PRINT_MAX_AGE_HOURS}h) — refusing to trade on stale data")
    deltas = build_deltas(values)
    drift = _drift_cpd(deltas, asof, calib.drift)
    return Snapshot(asof=asof, value=value, drift_cpd=drift, calib=calib)


def fresh_snapshot() -> Snapshot:
    """load_snapshot(require_fresh=True), self-healing by scraping AAA when
    the on-disk history is stale (covers carry running before/without the
    snipe task having recorded today's print)."""
    try:
        return load_snapshot(require_fresh=True)
    except RuntimeError as e:
        log(f"history stale ({e}); scraping AAA directly")
        value, asof = gas_data.fetch_live()
        rows = gas_data.load_history()
        if gas_data.upsert(rows, asof, value, None, None, "live"):
            gas_data.save_history(rows)
            log(f"recorded AAA {value:.4f} as of {asof}")
        return load_snapshot(require_fresh=True)


# ----------------------------------------------------------------------------
# Market universe
# ----------------------------------------------------------------------------

@dataclass
class GasMarket:
    ticker: str
    event_ticker: str
    series: str
    strike: float
    settle_day: date
    close_time: datetime
    yes_bid: Optional[int]      # cents
    yes_ask: Optional[int]
    fair: Optional[float] = None


def _cents(m: dict, key: str) -> Optional[int]:
    v = m.get(key)
    if v is None:
        return None
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return None


def fetch_universe(client: ExchangeClient, snap: Snapshot) -> List[GasMarket]:
    out: List[GasMarket] = []
    now = datetime.now(timezone.utc)
    for series in SERIES:
        cursor = None
        events: List[str] = []
        while True:
            d = client.get(f"/events?series_ticker={series}&status=open&limit=200"
                           + (f"&cursor={cursor}" if cursor else ""))
            evs = d.get("events", [])
            events += [e["event_ticker"] for e in evs]
            cursor = d.get("cursor")
            if not cursor or not evs:
                break
        for ev in events:
            d = client.get(f"/markets?event_ticker={ev}&limit=100")
            for m in d.get("markets", []):
                if m.get("status") != "active":
                    continue
                if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
                    continue
                exp = m.get("expected_expiration_time") or m.get("expiration_time")
                close = m.get("close_time")
                if not exp or not close:
                    continue
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                if close_dt <= now:
                    continue
                gm = GasMarket(
                    ticker=m["ticker"], event_ticker=ev, series=series,
                    strike=float(m["floor_strike"]),
                    settle_day=exp_dt.date(),
                    close_time=close_dt,
                    yes_bid=_cents(m, "yes_bid_dollars"),
                    yes_ask=_cents(m, "yes_ask_dollars"),
                )
                gm.fair = snap.fair_cents(gm.strike, gm.settle_day)
                out.append(gm)
    return out


def taker_fee_cents(price_cents: float) -> float:
    """Kalshi quadratic fee, fee_multiplier 1: 0.07 * p * (1-p) per contract,
    + pad for per-order cent rounding."""
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p) + 0.3


def order_yes_book_cents(order: dict) -> Optional[Tuple[str, int]]:
    """(book_side, yes_price_cents) from a normalized V2 order dict
    (same field fallbacks as incentive_mm)."""
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
                continue
    return 0.0


def order_collateral_dollars(order: dict) -> float:
    """Worst-case cost of a resting order: bid pays yes_price, ask (buying NO)
    pays 100 - yes_price."""
    bp = order_yes_book_cents(order)
    if bp is None:
        return order_remaining(order) * 0.5   # unknown: assume 50c
    book_side, yes_cents = bp
    cents = yes_cents if book_side == "bid" else 100 - yes_cents
    return order_remaining(order) * cents / 100.0


# ----------------------------------------------------------------------------
# Portfolio / risk reads
# ----------------------------------------------------------------------------

@dataclass
class Book:
    positions: Dict[str, float]              # ticker -> signed contracts (+yes)
    exposure_dollars: Dict[str, float]       # ticker -> collateral-ish cost
    our_orders: List[dict]                   # resting orders with gas- prefix
    balance_dollars: float


def read_book(client: ExchangeClient, tickers: List[str]) -> Book:
    positions: Dict[str, float] = {}
    exposure: Dict[str, float] = {}
    cursor = None
    while True:
        d = client.get_positions(limit=200, cursor=cursor,
                                 settlement_status="unsettled")
        for p in d.get("market_positions") or []:
            t = p.get("ticker", "")
            if t not in tickers:
                continue
            pos = p.get("position")
            if pos:
                positions[t] = float(pos)
            # market_exposure(_dollars): Kalshi's own collateral figure when
            # present; fall back to worst-case $1/contract
            exp = p.get("market_exposure_dollars")
            if exp is None and p.get("market_exposure") is not None:
                try:
                    exp = float(p["market_exposure"]) / 100.0
                except (TypeError, ValueError):
                    exp = None
            if exp is None and pos:
                exp = abs(float(pos))
            if exp is not None:
                try:
                    exposure[t] = abs(float(exp))
                except (TypeError, ValueError):
                    pass
        cursor = d.get("cursor")
        if not cursor:
            break
    ours: List[dict] = []
    cursor = None
    while True:
        d = client.get_orders(status="resting", limit=200, cursor=cursor)
        for o in d.get("orders") or []:
            if str(o.get("client_order_id", "")).startswith(ORDER_PREFIX + "-"):
                ours.append(o)
        cursor = d.get("cursor")
        if not cursor:
            break
    bal = 0.0
    try:
        b = client.get_balance()
        raw = b.get("balance_dollars", b.get("balance"))
        bal = float(raw) if raw is not None else 0.0
        if bal > 100000:      # cents, not dollars
            bal /= 100.0
    except Exception as e:
        log(f"! balance read failed: {e}")
    return Book(positions=positions, exposure_dollars=exposure,
                our_orders=ours, balance_dollars=bal)


# ----------------------------------------------------------------------------
# Persistent state (daily spend, halt)
# ----------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(gas_data.DATA_DIR, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATUS_PATH)


def et_day_key() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# Trading engine
# ----------------------------------------------------------------------------

class GasBot:
    def __init__(self, client: Optional[ExchangeClient], live: bool):
        self.client = client
        self.live = live
        self.alerter = Alerter(live)
        self.state = load_state()

    # ---- helpers ----

    def _halted(self) -> Optional[str]:
        if os.path.exists(HALT_PATH):
            return "HALT file present"
        return None

    def day_spend(self) -> float:
        return float(self.state.get("day_spend", {}).get(et_day_key(), 0.0))

    def add_day_spend(self, dollars: float) -> None:
        ds = self.state.setdefault("day_spend", {})
        key = et_day_key()
        ds[key] = float(ds.get(key, 0.0)) + dollars
        # keep the map small
        for k in list(ds):
            if k < (datetime.now(ET) - timedelta(days=14)).strftime("%Y-%m-%d"):
                del ds[k]
        save_state(self.state)

    def _order(self, ticker: str, side: str, price_cents: int, count: int,
               tif: Optional[str], post_only: bool, why: str) -> bool:
        """Place one buy order (side yes/no at price_cents). Returns True if
        (dry- or live-) placed."""
        cost = count * price_cents / 100.0
        tag = "LIVE" if self.live else "DRY"
        log(f"{tag} ORDER {ticker} buy {side.upper()} {count}x @ {price_cents}c "
            f"(~${cost:.0f}) {'IOC' if tif == 'immediate_or_cancel' else 'post'}"
            f" | {why}")
        if not self.live:
            return True
        kwargs = dict(ticker=ticker,
                      client_order_id=f"{ORDER_PREFIX}-{RUN_ID}-{uuid.uuid4().hex[:12]}",
                      side=side, action="buy", count=count, type="limit")
        if side == "yes":
            kwargs["yes_price"] = price_cents
        else:
            kwargs["no_price"] = price_cents
        if tif:
            kwargs["time_in_force"] = tif
        else:
            kwargs["post_only"] = post_only
            kwargs["expiration_ts"] = int(time.time()) + ORDER_TTL_SECS
        try:
            self.client.create_order(**kwargs)
            self.add_day_spend(cost)
            return True
        except HttpError as e:
            log(f"! order rejected {ticker} {side}@{price_cents}: {e} {getattr(e, 'body', '')}")
            if tif == "immediate_or_cancel":
                # 3am resilience: if the venue rejects the IOC form, fall back
                # to a short-TTL GTC limit at the same (capped) price — fills
                # like a take if the edge is there, self-expires otherwise.
                log(f"  retrying as 120s-TTL GTC limit")
                kwargs.pop("time_in_force", None)
                kwargs["client_order_id"] = f"{ORDER_PREFIX}-{RUN_ID}-{uuid.uuid4().hex[:12]}"
                kwargs["expiration_ts"] = int(time.time()) + 120
                try:
                    self.client.create_order(**kwargs)
                    self.add_day_spend(cost)
                    return True
                except HttpError as e2:
                    log(f"! fallback also rejected: {e2} {getattr(e2, 'body', '')}")
            return False

    def cancel_ours(self, orders: List[dict], why: str) -> int:
        n = 0
        for o in orders:
            oid = o.get("order_id") or o.get("id")
            if not oid:
                continue
            tag = "LIVE" if self.live else "DRY"
            log(f"{tag} CANCEL {o.get('ticker')} {oid[:12]} | {why}")
            if self.live:
                try:
                    self.client.cancel_order(oid)
                    n += 1
                except HttpError as e:
                    log(f"! cancel failed {oid[:12]}: {e}")
            else:
                n += 1
        return n

    # ---- decision core ----

    def decide(self, gm: GasMarket, book: Book
               ) -> Optional[Tuple[str, int, int, str]]:
        """Maker (carry) decision: (side, price_cents, count, why) or None.
        The snipe path has its own inline take logic in snipe_once.

        Buy-only on each side (YES when market under fair, NO when over);
        exits happen at settlement — positions are short-dated by design."""
        if gm.fair is None or gm.yes_bid is None or gm.yes_ask is None:
            return None
        # Maker humility: the resting book may know things the drift model
        # doesn't (wholesale moves, station data). Quote around a blend,
        # and stand aside entirely when the model is wildly off-market on
        # a two-sided book — that's evidence of missing information, not
        # free money.
        fair = gm.fair
        mid = (gm.yes_bid + gm.yes_ask) / 2.0
        two_sided = gm.yes_bid >= 1 and gm.yes_ask <= 99 \
            and (gm.yes_ask - gm.yes_bid) <= 15
        if two_sided:
            if abs(gm.fair - mid) > MAX_DISAGREE_CENTS:
                return None
            fair = MODEL_WEIGHT * gm.fair + (1.0 - MODEL_WEIGHT) * mid
        pos = book.positions.get(gm.ticker, 0.0)
        no_bid = 100 - gm.yes_ask     # best NO bid implied by YES ask
        no_ask = 100 - gm.yes_bid

        def room(side: str) -> int:
            # cap net exposure per strike, direction-aware
            if side == "yes":
                return int(MAX_PER_STRIKE - max(pos, 0.0))
            return int(MAX_PER_STRIKE - max(-pos, 0.0))

        # YES side: passive join/improve bid up to (fair - edge); never cross
        if gm.yes_ask >= PRICE_MIN and gm.yes_ask <= PRICE_MAX:
            tgt = min(int(math.floor(fair - EDGE_MAKE_CENTS)), gm.yes_ask - 1)
            if tgt >= max(PRICE_MIN, 1) and room("yes") > 0 and tgt >= gm.yes_bid:
                count = min(room("yes"), SIZE_CONTRACTS)
                return ("yes", tgt, count,
                        f"fair {fair:.1f} bid {gm.yes_bid} ask {gm.yes_ask}")

        # NO side: market NO-ask below NO-fair  (YES bid above fair)
        no_fair = 100.0 - fair
        if PRICE_MIN <= no_ask <= PRICE_MAX:
            tgt = min(int(math.floor(no_fair - EDGE_MAKE_CENTS)), no_ask - 1)
            if tgt >= max(PRICE_MIN, 1) and room("no") > 0 and tgt >= no_bid:
                count = min(room("no"), SIZE_CONTRACTS)
                return ("no", tgt, count,
                        f"no_fair {no_fair:.1f} no_bid {no_bid} no_ask {no_ask}")
        return None

    def _budget_ok(self, book: Book, add_cost: float, event: str,
                   event_costs: Dict[str, float]) -> Optional[str]:
        if book.balance_dollars and book.balance_dollars < BALANCE_FLOOR_DOLLARS:
            return f"balance ${book.balance_dollars:.0f} < floor ${BALANCE_FLOOR_DOLLARS:.0f}"
        total = sum(book.exposure_dollars.values())
        resting = sum(order_collateral_dollars(o) for o in book.our_orders)
        if total + resting + add_cost > BUDGET_DOLLARS:
            return (f"budget: exposure ${total:.0f} + resting ~${resting:.0f} "
                    f"+ new ${add_cost:.0f} > ${BUDGET_DOLLARS:.0f}")
        if event_costs.get(event, 0.0) + add_cost > EVENT_BUDGET_DOLLARS:
            return f"event budget ${EVENT_BUDGET_DOLLARS:.0f} reached for {event}"
        if self.day_spend() + add_cost > DAILY_COST_CAP_DOLLARS:
            return (f"daily cost cap: spent ${self.day_spend():.0f} today, "
                    f"cap ${DAILY_COST_CAP_DOLLARS:.0f}")
        return None

    # ---- carry mode ----

    def carry_cycle(self, snap: Snapshot) -> None:
        halted = self._halted()
        universe = fetch_universe(self.client, snap)
        tickers = [g.ticker for g in universe]
        book = read_book(self.client, tickers)
        log(f"carry: {len(universe)} markets, {len(book.our_orders)} resting gas- "
            f"orders, balance ${book.balance_dollars:.0f}, "
            f"print {snap.asof} = {snap.value:.4f}, drift {snap.drift_cpd:+.2f}c/d")
        if halted:
            log(f"HALTED ({halted}) — cancelling ours, no placements")
            self.cancel_ours(book.our_orders, halted)
            return

        # per-event current exposure (positions only; resting handled in total)
        event_costs: Dict[str, float] = {}
        by_ticker = {g.ticker: g for g in universe}
        for t, exp in book.exposure_dollars.items():
            g = by_ticker.get(t)
            if g:
                event_costs[g.event_ticker] = event_costs.get(g.event_ticker, 0.0) + exp

        # cancel stale orders (price drifted off desired by >1c, or market gone)
        desired: Dict[Tuple[str, str], Tuple[int, int, str]] = {}
        for gm in universe:
            dec = self.decide(gm, book)
            if dec:
                side, px, cnt, why = dec
                desired[(gm.ticker, side)] = (px, cnt, why)
        # Our desired orders in YES-book terms: buy YES @ p -> bid @ p,
        # buy NO @ p -> ask @ (100-p). Keep a resting order iff it matches
        # the desired book side within 1c.
        to_cancel = []
        open_by_key: Dict[Tuple[str, str], dict] = {}
        for o in book.our_orders:
            t = o.get("ticker", "")
            bp = order_yes_book_cents(o)
            keep = False
            if bp is not None:
                book_side, yes_cents = bp
                side = "yes" if book_side == "bid" else "no"
                want = desired.get((t, side))
                if want is not None:
                    want_yes = want[0] if side == "yes" else 100 - want[0]
                    if abs(yes_cents - want_yes) <= 1:
                        open_by_key[(t, side)] = o
                        keep = True
            if not keep:
                to_cancel.append(o)
        self.cancel_ours(to_cancel, "requote")

        placed = 0
        for (ticker, side), (px, cnt, why) in sorted(desired.items()):
            if (ticker, side) in open_by_key:
                continue                      # already resting at right price
            if placed >= MAX_ORDERS_PER_CYCLE:
                log("placement cap reached this cycle")
                break
            gm = by_ticker[ticker]
            cost = cnt * px / 100.0
            veto = self._budget_ok(book, cost, gm.event_ticker, event_costs)
            if veto:
                log(f"skip {ticker} {side}@{px}: {veto}")
                continue
            if self._order(ticker, side, px, cnt, tif=None, post_only=True, why=why):
                event_costs[gm.event_ticker] = event_costs.get(gm.event_ticker, 0.0) + cost
                placed += 1

    # ---- snipe mode ----

    def snipe_window(self) -> None:
        """Poll AAA inside the ET window; on a new print, take stale quotes."""
        w_from, w_to = SNIPE_WINDOW.split("-")
        fh, fm = (int(x) for x in w_from.split(":"))
        th, tm = (int(x) for x in w_to.split(":"))
        rows = gas_data.load_history()
        prev = gas_data.series_values(rows)
        prev_asof = prev[-1][0] if prev else None
        log(f"snipe: window {SNIPE_WINDOW} ET, poll {SNIPE_POLL_SECS}s, "
            f"last known print {prev_asof}")
        fired = False
        while True:
            now_et = datetime.now(ET)
            after_start = (now_et.hour, now_et.minute) >= (fh, fm)
            before_end = (now_et.hour, now_et.minute) <= (th, tm)
            if not before_end:
                log("snipe: window over" + (" (no new print seen!)" if not fired else ""))
                if not fired:
                    self.alerter.send("snipe no-print",
                                      f"window {SNIPE_WINDOW} ET ended without a "
                                      f"new AAA print (last {prev_asof})")
                return
            if not after_start:
                time.sleep(30)
                continue
            try:
                value, asof = gas_data.fetch_live()
            except Exception as e:
                log(f"snipe: AAA fetch failed ({e})")
                time.sleep(SNIPE_POLL_SECS)
                continue
            if prev_asof is not None and asof <= prev_asof:
                time.sleep(SNIPE_POLL_SECS)
                continue
            # NEW PRINT
            log(f"snipe: NEW PRINT {asof} = {value:.4f}")
            try:
                snap_old = load_snapshot(require_fresh=False)
            except RuntimeError as e:
                log(f"snipe: no pre-print model ({e})")
                snap_old = None
            rows = gas_data.load_history()
            gas_data.upsert(rows, asof, value, None, None, "live")
            gas_data.save_history(rows)
            fired = True
            try:
                snap = load_snapshot(require_fresh=True)
            except RuntimeError as e:
                log(f"snipe: model refused: {e}")
                return
            self.snipe_once(snap, snap_old)
            return

    def snipe_once(self, snap: Snapshot, snap_old: Optional[Snapshot] = None) -> None:
        """Take stale quotes right after a new print.

        Effective fair per market = the PRE-print market mid shifted by the
        model's mechanical repricing (fair_new - fair_old), capped by the
        model's absolute fair on the aggressive side. This only harvests the
        repricing the fresh print justifies — it does not bet the model's
        absolute disagreement with the market (that disagreement usually
        means the market knows something, e.g. wholesale prices)."""
        halted = self._halted()
        if halted:
            log(f"HALTED ({halted}) — no snipe")
            return
        universe = fetch_universe(self.client, snap)
        tickers = [g.ticker for g in universe]
        book = read_book(self.client, tickers)
        event_costs: Dict[str, float] = {}
        by_ticker = {g.ticker: g for g in universe}
        for t, exp in book.exposure_dollars.items():
            g = by_ticker.get(t)
            if g:
                event_costs[g.event_ticker] = event_costs.get(g.event_ticker, 0.0) + exp
        # Pass 1: collect every qualifying take with its net edge.
        candidates = []   # (net_edge, gm, side, px, room)
        for gm in universe:
            if gm.fair is None or gm.yes_bid is None or gm.yes_ask is None:
                continue
            fair_yes_eff, fair_no_eff = gm.fair, 100.0 - gm.fair
            if snap_old is not None:
                fair_old = snap_old.fair_cents(gm.strike, gm.settle_day)
                if fair_old is not None:
                    mid = (gm.yes_bid + gm.yes_ask) / 2.0
                    anchor = mid + (gm.fair - fair_old)
                    fair_yes_eff = min(gm.fair, anchor)
                    fair_no_eff = 100.0 - max(gm.fair, anchor)
            pos = book.positions.get(gm.ticker, 0.0)
            for side, eff_fair, ask, room in (
                    ("yes", fair_yes_eff, gm.yes_ask,
                     int(MAX_PER_STRIKE - max(pos, 0.0))),
                    ("no", fair_no_eff, 100 - gm.yes_bid,
                     int(MAX_PER_STRIKE - max(-pos, 0.0)))):
                if ask is None or not (PRICE_MIN <= ask <= PRICE_MAX) or room <= 0:
                    continue
                net_edge = eff_fair - ask - EDGE_TAKE_CENTS - taker_fee_cents(ask)
                limit = int(math.floor(eff_fair - EDGE_TAKE_CENTS
                                       - taker_fee_cents(ask)))
                if net_edge < 0 or limit < ask:
                    continue
                candidates.append((net_edge, gm, side, min(limit, PRICE_MAX), room))

        # Pass 2: spread the morning envelope best-edge-first, one unit per
        # strike (at most one side of a strike can ever qualify: both would
        # need bid > ask).
        acted = []
        taken_total = 0
        taken_by_ticker: Dict[str, int] = {}
        for net_edge, gm, side, px, room in sorted(candidates, key=lambda c: -c[0]):
            if taken_total >= SNIPE_MORNING_CAP:
                log(f"morning envelope {SNIPE_MORNING_CAP} used; done")
                break
            unit_left = SNIPE_UNIT_CONTRACTS - taken_by_ticker.get(gm.ticker, 0)
            cnt = min(room, unit_left, SNIPE_MORNING_CAP - taken_total)
            if cnt <= 0:
                continue
            cost = cnt * px / 100.0
            veto = self._budget_ok(book, cost, gm.event_ticker, event_costs)
            if veto:
                log(f"skip {gm.ticker} {side}@{px}: {veto}")
                continue
            why = (f"edge {net_edge:+.1f}c net, eff_fair vs ask "
                   f"(model {gm.fair:.1f})")
            if self._order(gm.ticker, side, px, cnt,
                           tif="immediate_or_cancel", post_only=False, why=why):
                event_costs[gm.event_ticker] = \
                    event_costs.get(gm.event_ticker, 0.0) + cost
                taken_total += cnt
                taken_by_ticker[gm.ticker] = \
                    taken_by_ticker.get(gm.ticker, 0) + cnt
                acted.append(f"{gm.ticker} buy {side} {cnt}x<= {px}c ({why})")
        body = (f"print {snap.asof} = {snap.value:.4f}, drift {snap.drift_cpd:+.2f}c/d\n"
                f"envelope {taken_total}/{SNIPE_MORNING_CAP} used, "
                f"{SNIPE_UNIT_CONTRACTS}/strike units\n"
                + ("\n".join(acted) if acted else "no quotes worth taking"))
        self.alerter.send(f"snipe {snap.asof}: {len(acted)} takes", body)

    # ---- status table ----

    def status_table(self, snap: Snapshot) -> None:
        universe = fetch_universe(self.client, snap)
        print(f"\nAAA print {snap.asof} = {snap.value:.4f}  "
              f"drift {snap.drift_cpd:+.2f}c/day  "
              f"sigma1 {snap.calib.sigma_cents(1):.2f}c power {snap.calib.power:.2f} "
              f"(n={snap.calib.n_days}d)\n")
        cur_ev = None
        for gm in sorted(universe, key=lambda g: (g.settle_day, g.series, g.strike)):
            if gm.event_ticker != cur_ev:
                cur_ev = gm.event_ticker
                h = (gm.settle_day - snap.asof).days
                print(f"-- {cur_ev}  settles {gm.settle_day} (h={h}d, "
                      f"sigma {snap.calib.sigma_cents(max(h,1)):.2f}c)")
            fair = f"{gm.fair:5.1f}" if gm.fair is not None else "  n/a"
            bid = "--" if gm.yes_bid is None else f"{gm.yes_bid:3d}"
            ask = "--" if gm.yes_ask is None else f"{gm.yes_ask:3d}"
            edge = ""
            if gm.fair is not None and gm.yes_ask is not None and gm.yes_bid is not None:
                buy_edge = gm.fair - gm.yes_ask
                sell_edge = gm.yes_bid - gm.fair
                if buy_edge >= 2:
                    edge = f"  <-- YES +{buy_edge:.0f}c"
                elif sell_edge >= 2:
                    edge = f"  <-- NO  +{sell_edge:.0f}c"
            print(f"   >{gm.strike:<6.3f} fair {fair}  mkt {bid}/{ask}{edge}")


# ----------------------------------------------------------------------------

def load_private_key():
    import base64
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_b64:
        try:
            pem = base64.b64decode(pem_b64)
        except Exception:
            pem = pem_b64.encode()
        return serialization.load_pem_private_key(pem, password=None,
                                                  backend=default_backend())
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
    if not os.path.exists(pem_path):
        local_default = r"C:/Users/jackd/Downloads/Lisa_Kalshi.txt"
        if os.path.exists(local_default):
            pem_path = local_default
        else:
            raise FileNotFoundError(
                f"No Kalshi private key. Set KALSHI_PRIVATE_KEY (b64) or place "
                f"PEM at {pem_path!r}.")
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None,
                                                  backend=default_backend())


def build_client() -> ExchangeClient:
    os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
    client = ExchangeClient(exchange_api_base=KALSHI_API_BASE,
                            key_id=KEY_ID, private_key=load_private_key())
    status = client.get_exchange_status()
    log(f"exchange status: {status}")
    return client


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="place real orders (default: dry run)")
    ap.add_argument("--mode", choices=["carry", "snipe"], default="carry")
    ap.add_argument("--once", action="store_true", help="single carry cycle")
    ap.add_argument("--snipe-now", action="store_true",
                    help="plumbing test: run one snipe pass immediately with "
                         "zero print-surprise (anchor = market mid), so it "
                         "only acts on genuinely crossed/stale books")
    ap.add_argument("--status", action="store_true",
                    help="print fair-value vs market table, then exit")
    ap.add_argument("--calibrate", action="store_true",
                    help="print model calibration diagnostics, then exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel ALL resting gas- orders (always real), exit")
    args = ap.parse_args(argv)

    if args.calibrate:
        values = gas_data.series_values(gas_data.load_history())
        calib = calibrate(values)
        deltas = build_deltas(values)
        drift = _drift_cpd(deltas, values[-1][0], calib.drift) if values else 0.0
        exact = sum(1 for r in gas_data.load_history().values()
                    if r["value"] is not None)
        print(f"history days: {len(values)} ({values[0][0]} .. {values[-1][0]}), "
              f"{exact} exact-value")
        print(f"consecutive deltas: {len(deltas)}")
        print(f"latest print: {values[-1][0]} = {values[-1][1]:.4f}")
        print(f"drift params (walk-forward selected): halflife "
              f"{calib.drift.halflife}d, last-delta weight {calib.drift.lam_last}, "
              f"decay rho {calib.drift.rho}")
        print(f"drift now: {drift:+.3f} c/day "
              f"(3-day cum {_cum_drift(drift, 3, calib.drift.rho):+.2f}c)")
        print(f"sigma fit: sigma_h = {calib.sigma1_cents:.3f} * h^{calib.power:.3f} "
              f"cents (x{SIGMA_PAD} pad at runtime)")
        for h, s, n in calib.table:
            print(f"  h={h}d  rms {s:.3f}c  (n={n})  -> runtime "
                  f"{calib.sigma_cents(h):.3f}c")
        return 0

    live = bool(args.live)
    client = build_client()
    bot = GasBot(client, live=live)

    if args.cancel_all:
        book = read_book(client, tickers=[])
        # read_book filters positions by tickers but returns ALL gas- orders
        bot.live = True
        n = bot.cancel_ours(book.our_orders, "cancel-all")
        log(f"cancelled {n} orders")
        return 0

    if args.mode == "carry" and not args.status:
        snap = fresh_snapshot()
    else:
        snap = load_snapshot(require_fresh=False)
    log(f"model: print {snap.asof} = {snap.value:.4f}, drift {snap.drift_cpd:+.2f}c/d, "
        f"sigma1 {snap.calib.sigma_cents(1):.2f}c (n={snap.calib.n_days})")

    if args.status:
        bot.status_table(snap)
        return 0

    if args.mode == "snipe":
        if args.snipe_now:
            bot.snipe_once(snap, snap_old=snap)   # zero surprise: mechanics test
        else:
            bot.snipe_window()
        return 0

    # carry loop
    while True:
        if snap is not None:
            try:
                bot.carry_cycle(snap)
            except HttpError as e:
                log(f"! cycle error: {e} {getattr(e, 'body', '')}")
            except Exception as e:
                log(f"! cycle error: {e}")
        if args.once:
            return 0
        time.sleep(POLL_SECS)
        try:
            snap = fresh_snapshot()
        except Exception as e:
            # no fresh print obtainable -> stand down (no cycles) until it is
            log(f"! snapshot refresh failed, standing down: {e}")
            snap = None


if __name__ == "__main__":
    sys.exit(main())
