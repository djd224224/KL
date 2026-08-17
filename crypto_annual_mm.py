#!/usr/bin/env python3
"""
crypto_annual_mm.py — Market maker for Kalshi crypto ANNUAL events: the
year-scale siblings of the monthly one-touch fleet and the weekly above/below
fleet, three series per asset under ONE bot:

    KX{ASSET}MAXY  "How high will {ASSET} get this year?"   one-touch, greater
    KX{ASSET}MINY  "How low will {ASSET} fall this year?"   one-touch, less
    KX{ASSET}Y     "{ASSET} price on Dec 31"                TERMINAL, mixed rungs
                   (SOL's terminal series is irregularly named KXSOLD26)

TWO MODELS LIVE HERE, AND MIXING THEM UP IS THE FAMILIAR ~2x ERROR:

  * MAXY/MINY settle EARLY the moment the barrier trades (rules: "if BTC is
    above $X ... before Dec 31 ... resolves Yes", early_close_condition set)
    -> crypto_touch_mm.touch_prob, direction max/min.
  * KX{ASSET}Y settles on the TERMINAL price (trimmed-mean CF index at year
    end) -> crypto_updown_mm.terminal_prob. Its rungs come in THREE shapes:
    T-suffix greater (floor_strike), T-suffix less (cap_strike, the bottom
    tail), and B-suffix between (floor+cap range buckets), all in one event.

    The model is chosen by SERIES, never by strike_type — strike_type
    "greater" appears in both families and is exactly the trap that made
    crypto_updown_mm a separate module. test_crypto_annual_mm.py pins the
    dispatch and pins the two models apart.

Model risk on a ~5-month horizon is the real enemy: driftless GBM understates
fat-tailed touch probabilities far more over months than over the weekly
fleet's 7 days. Three compounding guards:
  * quote band 10-90c (the tails are where the model error concentrates);
  * fair-vs-mid divergence stand-down (these books are DEEP and tight —
    observed 1-2c wide at 5-6 figure size — so the market's own mid is a
    strong anchor; >15c disagreement means we'd be taking a view, not making
    a market);
  * join-don't-lead (inherited): we only ever match the best external level.
Plus the touch-family breach guard: a strike the year's realised extreme has
already crossed is settled money walking through settlement lag — daily
candles back past the window start (Kraken 1440m spans ~2y), the in-progress
candle included, plus session extremes from every live-price poll.

Positions here are ACCOUNT positions: the incentive bot carried frozen
KXBNBMAXY inventory (+25/-26/-13) when this bot was built, and those count
toward the per-market/event caps by design — the caps bound the ACCOUNT's
exposure per market, whoever created it. (incentive_mm blocklists all annual
crypto series as of 2026-08-13 — this fleet's book now, same arrangement as
MAXMON/MINMON.)

Reuses the multi-event machinery of crypto_updown_mm (which itself rides on
crypto_touch_mm's ledger/caps/fail-safe/alerting): this module only swaps in
multi-SERIES discovery, per-series model dispatch, year-scale event selection
(cadence "annual" = window > ~400h) and the YTD breach guard.

PILOT SETTINGS (2026-08-13, matching the updown pilot's opening size): 3 x 1
ladder, 3c off fair, 2c apart, band 10-90c, <=8 markets/event, caps 40/200/400
(mkt/event/asset), poll 60s. Sizes are pinned by tests — changing one must
break a test.

SAFETY: DRY RUN by default. --live is explicit. --cancel-all always operates
on the REAL book.

Usage:
    python crypto_annual_mm.py --discover
    python crypto_annual_mm.py --asset BTC --once
    python crypto_annual_mm.py --asset BTC --live
    python crypto_annual_mm.py --asset BTC --cancel-all
"""

import argparse
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# --- env shim: must precede the crypto_touch_mm import (see the weekly touch
# bot's header for the rationale). Only operator-set overrides are forwarded.
_PASSTHROUGH = {
    "CAY_SUMMARY_HOUR_CT":     "CMM_SUMMARY_HOUR_CT",
    "CAY_ALERT_TO":            "CMM_ALERT_TO",
}
for _cay, _cmm in _PASSTHROUGH.items():
    if os.environ.get(_cay):
        os.environ[_cmm] = os.environ[_cay]

import crypto_touch_mm as mm            # noqa: E402
import crypto_updown_mm as ud           # noqa: E402
from crypto_touch_mm import DataError, log   # noqa: E402

MODEL_VERSION = "crypto_annual_mm_v1.0"
CLIENT_ORDER_PREFIX = os.environ.get("CAY_CLIENT_ORDER_PREFIX", "cay")


def _env_f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Ladder shape. Pilot = the updown fleet's opening size (3x1, 2026-08-05
# vintage): unproven market, positions can sit until Dec 31.
NUM_LEVELS = _env_i("CAY_NUM_LEVELS", 3)
CONTRACTS_PER_LEVEL = _env_i("CAY_CONTRACTS_PER_LEVEL", 1)
QUOTE_OFFSET_CENTS = _env_i("CAY_QUOTE_OFFSET_CENTS", 3)
LEVEL_SPACING_CENTS = _env_i("CAY_LEVEL_SPACING_CENTS", 2)

# Risk. Non-binding backstops behind the ladder size (3 resting per market
# per side), sized like the updown pilot's opening caps.
MAX_POSITION_CONTRACTS = _env_f("CAY_MAX_POSITION", 40)   # per market
MAX_EVENT_CONTRACTS = _env_f("CAY_MAX_EVENT", 200)        # per event
MAX_ASSET_CONTRACTS = _env_f("CAY_MAX_ASSET", 400)        # across all 3 series

# Band + divergence: see module docstring.
SKIP_FAIR_ABOVE_CENTS = _env_i("CAY_SKIP_FAIR_ABOVE_CENTS", 90)
SKIP_FAIR_BELOW_CENTS = _env_i("CAY_SKIP_FAIR_BELOW_CENTS", 10)
MAX_MARKETS_PER_EVENT = _env_i("CAY_MAX_MARKETS_PER_EVENT", 8)
MAX_FAIR_DIVERGENCE_CENTS = _env_i("CAY_MAX_FAIR_DIVERGENCE_CENTS", 15)

# Year-scale markets move slowly; 60s keeps 8 bots light on the shared API.
POLL_SECS = _env_i("CAY_POLL_SECS", 60)
IDLE_POLL_SECS = _env_f("CAY_IDLE_POLL_SECS", 600)
SWEEP_RECENT_EVENTS = _env_i("CAY_SWEEP_RECENT_EVENTS", 3)   # per series

# Separate heartbeat dir: send_daily_digest.py prices each fleet's status
# files with fleet-specific logic, and the shared Kraken/Coinbase data cache
# stays in the touch fleet's STATUS_DIR regardless (module-level in mm).
STATUS_DIR = os.environ.get(
    "CAY_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\crypto-annual")

# Terminal (end-of-year price) series tickers are KX{ASSET}Y — except SOL,
# whose 2026 series is KXSOLD26 ("SOL price end of 2026?"). Note KXSOLD26's
# ticker STARTS with "KXSOLD": never prefix-match it against the KXSOLD
# above/below series. Next year's listing may need this table updated —
# --discover flags a terminal series that lists nothing.
TERMINAL_SERIES_OVERRIDES = {"SOL": "KXSOLD26"}


def annual_series_for(asset: str) -> Dict[str, str]:
    """series ticker -> model kind for one asset. The kind is decided by the
    SERIES (never by strike_type — see module docstring)."""
    return {
        f"KX{asset}MAXY": "touch_max",
        f"KX{asset}MINY": "touch_min",
        TERMINAL_SERIES_OVERRIDES.get(asset, f"KX{asset}Y"): "terminal",
    }


def kind_for_event(event_ticker: str, kind_by_series: Dict[str, str]) -> Optional[str]:
    """Event tickers start with their series ticker: KXBTCMAXY-26DEC31,
    KXBNBMAXY-BNB-26DEC31 (asset infix), KXSOLD26-27JAN0100 all resolve on
    the first dash-separated segment."""
    return kind_by_series.get((event_ticker or "").split("-", 1)[0])


# One config per asset, same data plumbing as the sibling fleets (derived
# from the monthly table so the Kraken/Coinbase pairs can never drift).
# cfg.series is display-only here — discovery iterates annual_series_for().
ASSETS: Dict[str, mm.MarketConfig] = {
    cfg.asset: replace(cfg, key=cfg.asset,
                       series="/".join(annual_series_for(cfg.asset)),
                       direction="above")
    for key, cfg in mm.MARKETS.items() if cfg.direction == "max"
}


# ---------------------------------------------------------------------------
# Pricing dispatch (pure functions)
# ---------------------------------------------------------------------------

def _strike_f(market: dict, key: str) -> Optional[float]:
    try:
        return float(market[key])
    except (KeyError, TypeError, ValueError):
        return None


def terminal_fair_prob(market: dict, spot: float, sigma_daily: float,
                       t_days: float) -> Optional[Tuple[float, float]]:
    """(P(YES), display_strike) for one KX{ASSET}Y rung, or None if the rung's
    shape isn't one we price. Three shapes observed live (2026-08-13):
    greater (T-rung upper tail), less (T-rung lower tail), between (B-rung
    range buckets). 'custom' falls back to crypto_updown_mm's validated
    ticker+subtitle recovery (the DOGE shape) and prices as greater."""
    st = market.get("strike_type")
    if st == "greater":
        k = _strike_f(market, "floor_strike")
        if k is None:
            return None
        return ud.terminal_prob(spot, k, sigma_daily, t_days), k
    if st == "less":
        k = _strike_f(market, "cap_strike")
        if k is None:
            return None
        return 1.0 - ud.terminal_prob(spot, k, sigma_daily, t_days), k
    if st == "between":
        lo = _strike_f(market, "floor_strike")
        hi = _strike_f(market, "cap_strike")
        if lo is None or hi is None or hi <= lo:
            return None
        p = (ud.terminal_prob(spot, lo, sigma_daily, t_days)
             - ud.terminal_prob(spot, hi, sigma_daily, t_days))
        return p, (lo + hi) / 2.0
    if st == "custom":
        k = ud.updown_strike(market)     # only when ticker+subtitle agree
        if k is None:
            return None
        return ud.terminal_prob(spot, k, sigma_daily, t_days), k
    return None


def combine_extreme(direction: str, *vals: Optional[float]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    return max(xs) if direction == "max" else min(xs)


# ---------------------------------------------------------------------------
# The market maker
# ---------------------------------------------------------------------------

@dataclass
class AnnualState:
    session_hi: Optional[float] = None    # live-price extremes this run: a
    session_lo: Optional[float] = None    # touch seen live gates immediately,
                                          # candle caches refresh 6-hourly
    skip_counts: Dict[str, int] = field(default_factory=dict)
    skip_logged: set = field(default_factory=set)


class AnnualMarketMaker(ud.UpDownMarketMaker):
    window_label = "SESS"
    model_version = MODEL_VERSION
    client_order_prefix = CLIENT_ORDER_PREFIX
    poll_secs = POLL_SECS
    idle_poll_secs = IDLE_POLL_SECS
    max_position = MAX_POSITION_CONTRACTS
    max_event = MAX_EVENT_CONTRACTS
    max_asset = MAX_ASSET_CONTRACTS
    max_markets_per_event = MAX_MARKETS_PER_EVENT
    skip_fair_above_cents = SKIP_FAIR_ABOVE_CENTS
    skip_fair_below_cents = SKIP_FAIR_BELOW_CENTS
    max_fair_divergence_cents = MAX_FAIR_DIVERGENCE_CENTS
    num_levels = NUM_LEVELS
    contracts_per_level = CONTRACTS_PER_LEVEL
    quote_offset_cents = QUOTE_OFFSET_CENTS
    level_spacing_cents = LEVEL_SPACING_CENTS
    status_dir = STATUS_DIR
    # No per-tenor overrides here: everything this bot quotes is "annual",
    # and inheriting the updown fleet's daily tables would be dead config at
    # best and a surprise at worst.
    contracts_per_level_by_cadence = {}
    num_levels_by_cadence = {}
    quote_offset_by_cadence = {}

    def __init__(self, cfg: mm.MarketConfig, client, live: bool):
        super().__init__(cfg, client, live, cadences=("annual",))
        self.tag = f"[{cfg.asset} yr]"
        self.kind_by_series = annual_series_for(cfg.asset)
        self.a = AnnualState()

    # ---- volatility ---------------------------------------------------------
    # A ~5-month horizon wants the monthly fleet's STABLE estimator (EWMA
    # blended with the 90d stdev), not the updown bot's horizon-matched
    # intraday sampling. TouchMarketMaker.refresh_vol also stores the daily
    # candles we reuse for the YTD breach guard.

    def refresh_vol(self, now_ts: float, now_utc: datetime) -> None:
        mm.TouchMarketMaker.refresh_vol(self, now_ts, now_utc)

    def sigma_for_horizon(self, t_days: float, now_ts: float) -> float:
        return self.state.sigma_daily

    # ---- discovery: three series, one bot -----------------------------------

    def fetch_series_markets(self) -> List[dict]:
        out: List[dict] = []
        for series in self.kind_by_series:
            cursor = None
            for _page in range(10):
                resp = self.client.get_markets(series_ticker=series,
                                               status="open", limit=1000,
                                               cursor=cursor)
                batch = resp.get("markets") or []
                out.extend(batch)
                cursor = resp.get("cursor")
                if not cursor or not batch:
                    break
        return [m for m in out if m.get("status") in ("active", "open")]

    def recent_event_tickers(self) -> List[str]:
        tickers: List[str] = []
        for series in self.kind_by_series:
            try:
                resp = self.client.get_events(series_ticker=series,
                                              limit=max(1, SWEEP_RECENT_EVENTS))
                tickers.extend(e["event_ticker"] for e in (resp.get("events") or [])
                               if e.get("event_ticker"))
            except Exception as e:
                log(f"{self.tag} ! could not list recent {series} events: {e}")
        return tickers

    # ---- YTD breach guard ---------------------------------------------------

    def window_extremes(self, window_start_ts: float,
                        spot: float) -> Tuple[Optional[float], Optional[float]]:
        """(high, low) realised over [window_start, now]: daily candles (the
        in-progress candle included — refresh_vol keeps it for exactly this),
        the session's live-price extremes, and the current spot.

        The session extremes are since BOT start, not window start, so a
        strike listed mid-session could see a pre-listing spike. That
        over-inclusion only ever benches a market (never quotes a dead one),
        the 'breach' alert + skip counts make it visible, and the candle
        refresh restores the true window within 6h."""
        candles = self.state.daily_candles
        hi = combine_extreme(
            "max", mm.candles_extreme(candles, window_start_ts, 86400, "max"),
            self.a.session_hi, spot)
        lo = combine_extreme(
            "min", mm.candles_extreme(candles, window_start_ts, 86400, "min"),
            self.a.session_lo, spot)
        return hi, lo

    def _skip(self, ticker: str, reason: str) -> None:
        """Tally every market we decline to price — silent exclusions are how
        a whole asset quotes nothing without anyone noticing (the DOGE
        lesson). Logged once per (ticker, reason); counts go in the status
        heartbeat for the digest."""
        self.a.skip_counts[reason] = self.a.skip_counts.get(reason, 0) + 1
        key = (ticker, reason)
        if key not in self.a.skip_logged:
            self.a.skip_logged.add(key)
            log(f"{self.tag} skip {ticker}: {reason}")

    # ---- pricing one event's band -------------------------------------------

    def quotable_markets(self, view: ud.EventView, spot: float,
                         now_utc: datetime,
                         now_ts: float) -> List[Tuple[int, str, float, int]]:
        self.a.session_hi = combine_extreme("max", self.a.session_hi, spot)
        self.a.session_lo = combine_extreme("min", self.a.session_lo, spot)

        kind = kind_for_event(view.ticker, self.kind_by_series)
        if kind is None:
            self._skip(view.ticker, "event from unrecognized series")
            return []
        sigma = self.state.sigma_daily

        priced = []
        for m in view.markets:
            ticker = m.get("ticker", "")
            end = ud.market_end_utc(m) or view.end
            t_days = max(0.0, (end - now_utc).total_seconds() / 86400.0)
            if t_days <= 0:
                continue

            if kind in ("touch_max", "touch_min"):
                direction = "max" if kind == "touch_max" else "min"
                strike = mm.market_strike(m, direction)
                if strike is None:
                    self._skip(ticker, f"unpriced touch shape "
                                       f"(strike_type={m.get('strike_type')!r})")
                    continue
                # The measurement window runs "from market issuance" (rules
                # verified 2026-08-13), so the breach window is the MARKET's
                # own open_time — a strike listed in March is not touched by
                # a February spike. Falls back to the event-wide start.
                m_open = mm.parse_iso_utc(m.get("open_time", "")) or view.start
                win_hi, win_lo = self.window_extremes(m_open.timestamp(), spot)
                extreme = win_hi if direction == "max" else win_lo
                if mm.breached(strike, extreme, direction):
                    # Already touched: settled money in settlement lag.
                    self._skip(ticker, f"breached (YTD {'hi' if direction == 'max' else 'lo'}"
                                       f" {extreme:,.2f} crossed {strike:,.2f})")
                    self.alerter.alert("breach", f"{ticker}: YTD extreme "
                                       f"{extreme:,.2f} already crossed strike "
                                       f"{strike:,.2f}; not quoting",
                                       key=ticker, urgent=False)
                    continue
                p = mm.touch_prob(spot, strike, sigma, t_days, direction)
                shown = strike
            else:
                out = terminal_fair_prob(m, spot, sigma, t_days)
                if out is None:
                    self._skip(ticker, f"unpriced terminal shape "
                                       f"(strike_type={m.get('strike_type')!r})")
                    continue
                p, shown = out

            fair = max(1, min(99, round(100.0 * p)))
            if fair >= self.skip_fair_above_cents or fair <= self.skip_fair_below_cents:
                continue
            priced.append((abs(fair - 50), ticker, shown, fair))
        priced.sort()
        return priced[:self.max_markets_per_event]

    def status_extra(self) -> Dict[str, object]:
        extra = super().status_extra()
        extra.update({
            "fleet": "annual",
            "series": list(self.kind_by_series),
            "session_hi": self.a.session_hi,
            "session_lo": self.a.session_lo,
            "skip_counts": dict(self.a.skip_counts),
        })
        return extra


# ---------------------------------------------------------------------------
# --discover
# ---------------------------------------------------------------------------

def discover_report(client, now_utc: Optional[datetime] = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    lines = ["Live Kalshi crypto ANNUAL events (KX*MAXY / KX*MINY / KX*Y):",
             f"  {'asset':6s} {'series':12s} {'kind':10s} {'event':26s} "
             f"{'left':>8s} {'strikes':>8s}"]
    total = 0
    for asset in sorted(ASSETS):
        for series, kind in annual_series_for(asset).items():
            try:
                resp = client.get_markets(series_ticker=series, status="open",
                                          limit=1000)
                markets = [m for m in (resp.get("markets") or [])
                           if m.get("status") in ("active", "open")]
            except Exception as e:
                lines.append(f"  {asset:6s} {series:12s} ERROR {e}")
                continue
            if not markets:
                lines.append(f"  {asset:6s} {series:12s} {kind:10s} (nothing open)")
                continue
            for view in ud.build_event_views(markets, now_utc):
                total += 1
                note = "" if view.cadence == "annual" else \
                    f"  [! window {view.window_hours:.0f}h -> {view.cadence}: NOT quoted]"
                lines.append(f"  {asset:6s} {series:12s} {kind:10s} {view.ticker:26s} "
                             f"{view.secs_left(now_utc) / 86400.0:6.1f}d "
                             f"{len(view.markets):8d}{note}")
    lines.append(f"  -> {total} open annual events across {len(ASSETS)} assets")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="BTC", choices=sorted(ASSETS),
                    help="which asset's annual series to make markets on")
    ap.add_argument("--live", action="store_true",
                    help="actually place/cancel orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--discover", action="store_true",
                    help="list every open annual event per asset, then exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel this bot's REAL resting orders on the asset's annual "
                         "series (open + recent events), then exit (always live)")
    args = ap.parse_args(argv)

    cfg = ASSETS[args.asset]
    client = mm.build_client()

    if args.discover:
        print(discover_report(client))
        return 0

    if args.cancel_all:
        bot = AnnualMarketMaker(cfg, client, live=True)
        try:   # populate the open-event set so the sweep covers it
            bot.u.events = ud.build_event_views(bot.fetch_series_markets(),
                                                datetime.now(timezone.utc))
        except Exception as e:
            log(f"! discovery failed ({e}); sweeping recent events only")
        n = bot.cancel_all_bot_orders(include_prev_month=True)
        log(f"cancelled {n} real resting orders for {cfg.asset} annual")
        return 0

    # One live looping bot per asset (same duplicate-money risk as the
    # sibling fleets). --once and dry runs are exempt, like the monthly bot's.
    if args.live and not args.once and not mm.acquire_singleton(
            cfg.asset, status_dir=AnnualMarketMaker.status_dir,
            heartbeat_max_age_secs=3 * IDLE_POLL_SECS):
        return 1

    bot = AnnualMarketMaker(cfg, client, live=args.live)
    log(f"=== {MODEL_VERSION} asset={cfg.asset} series={cfg.series} "
        f"prefix={CLIENT_ORDER_PREFIX}- "
        f"ladder {NUM_LEVELS}x{CONTRACTS_PER_LEVEL} @{QUOTE_OFFSET_CENTS}c off fair "
        f"{LEVEL_SPACING_CENTS}c apart | band {SKIP_FAIR_BELOW_CENTS}-"
        f"{SKIP_FAIR_ABOVE_CENTS}c, <={MAX_MARKETS_PER_EVENT} mkts/event | "
        f"caps {MAX_POSITION_CONTRACTS:g}/{MAX_EVENT_CONTRACTS:g}/"
        f"{MAX_ASSET_CONTRACTS:g} (mkt/event/asset) ===")
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
