#!/usr/bin/env python3
"""
crypto_touch_mm_weekly.py — Market maker for Kalshi crypto WEEKLY one-touch
events. The weekly sibling of crypto_touch_mm.py (monthly), which it SUBCLASSES
rather than forks: pricing, the order ledger, position/level/event caps, the
fail-safe loop, alerting and the daily summary are all the monthly bot's
battle-tested code. Only the measurement window differs, and that lives behind
horizon hooks on TouchMarketMaker: window_start_utc / window_end_utc /
window_label / min_hours_left / skip_reason_for_fair / active_markets /
maybe_rollover / poll_interval / startup_event_and_sweep / status_dir /
status_extra, plus the per-fleet class attributes model_version /
client_order_prefix / poll_secs / max_position / max_event.

  "How high will {ASSET} get this week?"  (KX{A}MAXW, strike_type=greater)
  "How low  will {ASSET} get this week?"  (KX{A}MINW,  strike_type=less)

LISTING STATUS (checked against the live API 2026-08-05): Kalshi currently
lists NO open weekly crypto one-touch events. Of 12,472 series exchange-wide,
exactly two match this family — KXBTCMAXW ("How high will Bitcoin get this
week?") and KXDOGEMAXW ("Dogecoin max") — and both have been dormant since
November 2024 (last events KXBTCMAXW-24-NOV22, KXDOGEMAXW-24). There is no
weekly MIN series for any asset. So this bot's normal state today is IDLE: it
polls its series slowly, logs "no open weekly event", places nothing, and
starts quoting by itself the day Kalshi relists. Run --discover to see what is
live right now.

That dormancy is also why the event ticker is DISCOVERED, not computed. The
monthly bot derives `KXSOLMAXMON-SOL-26AUG31` from the calendar, but the two
historical weekly events used two different and non-monthly conventions
(`KXBTCMAXW-24-NOV22`, `KXDOGEMAXW-24`), so any formula would be a guess.
Instead each cycle asks the exchange which markets on the series are open,
groups them by event, and trades the FRONT week (soonest window end still in
the future). The measurement window comes from those markets' own open_time /
expected_expiration_time — which also means a short "stub" week, a mid-week
listing, or an odd close hour is handled correctly with no code change.

Differences from the monthly bot, and why:
  * Event discovery + weekly rollover (above); orphan sweeps walk the series'
    recent events instead of a computed previous ticker.
  * Window-to-date extreme ("WTD") for the already-touched guard, measured
    from the event's own open_time rather than the 1st of the month.
  * min_hours_left 2.0 (vs 1.0): the last hours of a 7-day one-touch are where
    gamma is most violent, and 1h is a far smaller share of a week.
  * NEW low-fair stand-down (SKIP_FAIR_BELOW_CENTS, default 3c). The monthly
    only guards the high tail (>=97c, near-certain touch). Over 7 days the
    cheap strikes are the dangerous tail: driftless GBM UNDERSTATES far-barrier
    touch probability for fat-tailed crypto, and the ask side of a 2c market
    risks ~98c to win 2c. Deep-OTM dust is where that model error is
    concentrated, so we stand aside instead of selling it.
  * Caps start small — 100 per market, 500 per event (vs the monthly's
    267/1667) — because this is an unproven market with no P&L history. Ladder
    spec is UNCHANGED (5 levels x 10 contracts, 5c off fair, 2c apart,
    post-only, join-don't-lead); raise the caps once there's a ledger.
  * Its own env namespace (CTW_*), its own client_order_id prefix ("cmw-"),
    and its own heartbeat directory, so it can never be confused with — or
    accidentally cancel the orders of — the 16 live monthly bots.

The Kraken/Coinbase data cache is DELIBERATELY still shared with the monthly
fleet (same STATUS_DIR): MAX/MIN bots on the same asset need identical candles,
and independent fetchers once got this IP rate-limited by Kraken and Coinbase
at the same time.

SAFETY: DRY RUN by default — reads are live, order writes are simulated and
logged. Pass --live to actually place orders. --cancel-all always operates on
the REAL book.

Usage:
    python crypto_touch_mm_weekly.py --discover            # what's live now
    python crypto_touch_mm_weekly.py --market BTC-MAX --once
    python crypto_touch_mm_weekly.py --market BTC-MAX --live
    python crypto_touch_mm_weekly.py --market BTC-MAX --cancel-all
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Env shim — MUST run before importing crypto_touch_mm
# ---------------------------------------------------------------------------
# crypto_touch_mm reads the ladder/alert tunables from CMM_* at IMPORT time,
# and there is no CTW_ equivalent inside it, so an explicit weekly override has
# to be forwarded under the monthly name before the import.
#
# Only vars the operator ACTUALLY SET are forwarded. An unconditional write
# would also retune a monthly bot that happens to share this interpreter (a
# test run, a tooling script) — the same leak the class attributes below exist
# to prevent. Defaults therefore live on the class, not in os.environ.
#
# CMM_STATUS_DIR is deliberately NOT forwarded: it also locates the shared
# Kraken/Coinbase data cache, and the whole crypto fleet must keep sharing it
# (independent fetchers once got this IP rate-limited by Kraken AND Coinbase at
# the same time). Weekly heartbeats go to STATUS_DIR via the status_dir hook.
_PASSTHROUGH = {
    "CTW_QUOTE_OFFSET_CENTS":  "CMM_QUOTE_OFFSET_CENTS",
    "CTW_LEVEL_SPACING_CENTS": "CMM_LEVEL_SPACING_CENTS",
    "CTW_NUM_LEVELS":          "CMM_NUM_LEVELS",
    "CTW_CONTRACTS_PER_LEVEL": "CMM_CONTRACTS_PER_LEVEL",
    "CTW_SUMMARY_HOUR_CT":     "CMM_SUMMARY_HOUR_CT",
    "CTW_ALERT_TO":            "CMM_ALERT_TO",
}
for _weekly_var, _monthly_var in _PASSTHROUGH.items():
    if os.environ.get(_weekly_var):
        os.environ[_monthly_var] = os.environ[_weekly_var]

import crypto_touch_mm as mm            # noqa: E402  (must follow the env shim)
from crypto_touch_mm import log         # noqa: E402

# Identity and risk limits that MUST differ from the monthly fleet. Read here,
# bound to the class below — never written onto the monthly module.
_RISK = {
    "poll_secs":    ("CTW_POLL_SECS",    "30",  int),
    "max_position": ("CTW_MAX_POSITION", "100", float),
    "max_event":    ("CTW_MAX_EVENT",    "500", float),
}


def _risk(name: str):
    env_var, default, cast = _RISK[name]
    return cast(os.environ.get(env_var, default))


MODEL_VERSION = "crypto_touch_mm_weekly_v1.0"
# Order ids and the orphan sweep key on this prefix. A distinct one keeps the
# weekly and monthly sweeps disjoint no matter what events they end up on.
CLIENT_ORDER_PREFIX = os.environ.get("CTW_CLIENT_ORDER_PREFIX", "cmw")


# ---------------------------------------------------------------------------
# Weekly configuration
# ---------------------------------------------------------------------------

WEEK_DAYS = 7.0
# Stop quoting this close to the window end. 2h on a 7-day one-touch, vs the
# monthly's 1h on a 30-day one: proportionally similar, and the terminal hours
# of a weekly are where the touch probability moves fastest.
MIN_HOURS_LEFT = float(os.environ.get("CTW_MIN_HOURS_LEFT", 2.0))
# Deep-OTM stand-down; see the module docstring. 0 disables.
SKIP_FAIR_BELOW_CENTS = int(os.environ.get("CTW_SKIP_FAIR_BELOW_CENTS", 3))
# While no weekly event is listed there is nothing to do and nothing at risk,
# so poll slowly instead of hammering the API every 30s.
IDLE_POLL_SECS = float(os.environ.get("CTW_IDLE_POLL_SECS", 300))
# How many of the series' recent events a full sweep walks. A restart that
# straddles a weekly roll must still clear last week's book.
SWEEP_RECENT_EVENTS = int(os.environ.get("CTW_SWEEP_RECENT_EVENTS", 4))
# Separate from the monthly fleet's: send_daily_digest.py globs status_*.json
# out of crypto_touch_mm.STATUS_DIR and prices each bot's event with a MONTHLY
# ticker, so weekly heartbeats landing there would corrupt the morning digest.
STATUS_DIR = os.environ.get(
    "CTW_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\crypto-touch-weekly")

# Same assets and data sources as the monthly fleet — derived from its table so
# the Kraken/Coinbase pairs can never drift — but pointed at the weekly series.
# Assets Kalshi does not currently list weekly (i.e. all but BTC/DOGE MAX) stay
# configured on purpose: each simply idles, and needs no code change if Kalshi
# relists it.
MARKETS: Dict[str, mm.MarketConfig] = {
    key: replace(cfg, series=f"KX{cfg.asset}{'MAX' if cfg.direction == 'max' else 'MIN'}W")
    for key, cfg in mm.MARKETS.items()
}


# ---------------------------------------------------------------------------
# Event discovery (pure functions)
# ---------------------------------------------------------------------------

def market_end_utc(market: dict) -> Optional[datetime]:
    """End of a market's measurement window, preferring the most specific field."""
    for key in ("expected_expiration_time", "close_time", "latest_expiration_time"):
        dt = mm.parse_iso_utc(market.get(key, ""))
        if dt is not None:
            return dt
    return None


def market_open_utc(market: dict) -> Optional[datetime]:
    return mm.parse_iso_utc(market.get("open_time", ""))


def group_by_event(markets: List[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker")
        if ev:
            out.setdefault(ev, []).append(m)
    return out


def pick_front_event(markets: List[dict], now_utc: datetime) -> Optional[str]:
    """The FRONT weekly: of the events represented in `markets`, the one whose
    window ends soonest while still ending in the future. Kalshi sometimes
    lists next week alongside this week; we always make markets on the nearer
    one. Ties break on ticker so the choice is deterministic."""
    best: Optional[Tuple[datetime, str]] = None
    for ev, ms in group_by_event(markets).items():
        ends = [e for e in (market_end_utc(m) for m in ms) if e is not None]
        if not ends:
            continue
        end = max(ends)
        if end <= now_utc:
            continue
        if best is None or (end, ev) < best:
            best = (end, ev)
    return best[1] if best else None


def event_window(markets_for_event: List[dict],
                 now_utc: datetime) -> Tuple[datetime, datetime]:
    """(start, end) of one weekly event's measurement window, taken from the
    markets themselves. open_time is authoritative — it handles stub weeks,
    mid-week listings and non-midnight closes that no calendar formula would.
    Falls back to a 7-day window only when the exchange omits the times."""
    ends = [e for e in (market_end_utc(m) for m in markets_for_event) if e is not None]
    end = max(ends) if ends else now_utc + timedelta(days=WEEK_DAYS)
    opens = [o for o in (market_open_utc(m) for m in markets_for_event) if o is not None]
    start = min(opens) if opens else end - timedelta(days=WEEK_DAYS)
    if start >= end:      # nonsense metadata: fall back rather than trust it
        start = end - timedelta(days=WEEK_DAYS)
    return start, end


# ---------------------------------------------------------------------------
# The weekly market maker
# ---------------------------------------------------------------------------

@dataclass
class WeeklyState:
    series_markets: List[dict] = field(default_factory=list)  # all open on the series
    event_markets: List[dict] = field(default_factory=list)   # front event's only
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    idle_reason: str = ""


class WeeklyTouchMarketMaker(mm.TouchMarketMaker):
    window_label = "WTD"
    min_hours_left = MIN_HOURS_LEFT
    status_dir = STATUS_DIR
    model_version = MODEL_VERSION
    client_order_prefix = CLIENT_ORDER_PREFIX
    poll_secs = _risk("poll_secs")
    max_position = _risk("max_position")
    max_event = _risk("max_event")

    def __init__(self, cfg: mm.MarketConfig, client, live: bool,
                 pinned_event: Optional[str] = None):
        super().__init__(cfg, client, live)
        self.tag = f"[{cfg.key} wk]"
        self.w = WeeklyState()
        self.pinned_event = pinned_event

    # ---- horizon hooks ------------------------------------------------------

    def window_start_utc(self, now_utc: datetime) -> datetime:
        # Defensive fallbacks only: the cycle returns early while idle, so a
        # discovered window is always set by the time these are consulted.
        return self.w.window_start or (now_utc - timedelta(days=WEEK_DAYS))

    def window_end_utc(self, now_utc: datetime) -> datetime:
        return self.w.window_end or (now_utc + timedelta(days=WEEK_DAYS))

    def skip_reason_for_fair(self, fair: int) -> Optional[str]:
        near_touch = super().skip_reason_for_fair(fair)
        if near_touch:
            return near_touch
        if SKIP_FAIR_BELOW_CENTS > 0 and fair <= SKIP_FAIR_BELOW_CENTS:
            return (f"fair={fair}c <= {SKIP_FAIR_BELOW_CENTS}c (deep OTM: GBM understates "
                    f"fat-tailed weekly touches, and the ask side risks ~{100 - fair}c "
                    f"to win {fair}c)")
        return None

    def poll_interval(self) -> float:
        return IDLE_POLL_SECS if self.w.idle_reason else self.poll_secs

    def maybe_rollover(self, now_utc: datetime) -> None:
        """No-op: run_cycle() discovers and rolls before delegating upward.
        The monthly's computed-ticker rollover would be wrong here."""
        return

    def active_markets(self) -> List[dict]:
        """Already fetched by discover() — no second round-trip per cycle."""
        return list(self.w.event_markets)

    def status_extra(self) -> Dict[str, object]:
        return {
            "fleet": "weekly",
            "series": self.cfg.series,
            "idle_reason": self.w.idle_reason,
            "window_start": (self.w.window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
                             if self.w.window_start else ""),
            "window_end": (self.w.window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
                           if self.w.window_end else ""),
        }

    # ---- discovery ----------------------------------------------------------

    def fetch_series_markets(self) -> List[dict]:
        """Every open market on this bot's weekly series, paginated. An
        unlisted series is not an error — Kalshi answers 200 with zero
        markets, which is exactly the idle path."""
        out: List[dict] = []
        cursor = None
        for _page in range(5):
            resp = self.client.get_markets(series_ticker=self.cfg.series,
                                           status="open", limit=200, cursor=cursor)
            batch = resp.get("markets") or []
            out.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        # The status QUERY value ("open") and a market's own status field
        # ("active") are different vocabularies; accept both.
        return [m for m in out if m.get("status") in ("active", "open")]

    def recent_event_tickers(self) -> List[str]:
        """The series' most recent event tickers, newest first — the sweep set
        for orders orphaned by a roll we weren't running for."""
        try:
            resp = self.client.get_events(series_ticker=self.cfg.series,
                                          limit=max(1, SWEEP_RECENT_EVENTS))
            return [e["event_ticker"] for e in (resp.get("events") or [])
                    if e.get("event_ticker")]
        except Exception as e:
            log(f"{self.tag} ! could not list recent {self.cfg.series} events: {e}")
            return []

    def discover(self, now_utc: datetime) -> bool:
        """Resolve the front weekly event and its window. Returns False when
        nothing is listed (the idle path). Fetch failures propagate: a cycle
        that can't see the exchange must count as an error, not as 'idle'.

        Refetched EVERY cycle, deliberately. One-touch strikes settle the
        moment they are touched, so a cached market list would leave us
        quoting a market that has already resolved. This is one API call and
        it replaces the monthly bot's per-cycle get_event(), so it is not extra
        load; while idle the slow poll_interval() throttles it anyway."""
        self.w.series_markets = self.fetch_series_markets()

        if self.pinned_event:
            ev = self.pinned_event if any(
                m.get("event_ticker") == self.pinned_event
                for m in self.w.series_markets) else None
            missing = f"pinned event {self.pinned_event} has no open markets"
        else:
            ev = pick_front_event(self.w.series_markets, now_utc)
            missing = f"no open weekly event on series {self.cfg.series}"

        if ev is None:
            if self.state.event_ticker:
                # The event we were trading is gone (settled or delisted):
                # clear our book on it before forgetting the ticker.
                log(f"{self.tag} event {self.state.event_ticker} no longer open; "
                    f"cancelling its orders and going idle")
                self.cancel_all_bot_orders()
                self.state.event_ticker = ""
            self.w.event_markets = []
            self.w.window_start = self.w.window_end = None
            self.w.idle_reason = missing
            return False

        self.w.event_markets = [m for m in self.w.series_markets
                                if m.get("event_ticker") == ev]
        self.w.window_start, self.w.window_end = event_window(
            self.w.event_markets, now_utc)
        self.w.idle_reason = ""
        self.roll_to(ev)
        return True

    def roll_to(self, ev: str) -> None:
        """Switch to a newly discovered event, clearing the old one's book and
        every window-scoped piece of state."""
        if ev == self.state.event_ticker:
            return
        if self.state.event_ticker:
            log(f"{self.tag} weekly rollover: {self.state.event_ticker} -> {ev}; "
                f"cancelling old-event orders")
            self.cancel_all_bot_orders()   # still scoped to the OLD ticker
        self.state.event_ticker = ev
        self.state.session_extreme = None
        self.state.mtd_extreme = None      # WTD extreme; window-scoped
        self.state.blind_streak.clear()
        window = ""
        if self.w.window_start and self.w.window_end:
            window = (f" window {self.w.window_start:%Y-%m-%d %H:%MZ} -> "
                      f"{self.w.window_end:%Y-%m-%d %H:%MZ}")
        log(f"{self.tag} trading event {ev}{window}")
        self.alerter.alert("rollover", f"now trading {ev}", key=ev, urgent=False)

    # ---- lifecycle ----------------------------------------------------------

    def startup_event_and_sweep(self) -> None:
        """The monthly computes its event ticker up front; ours is discovered
        on the first cycle. The startup sweep therefore walks the series'
        recent events rather than a computed current/previous pair."""
        self.state.event_ticker = ""
        if self.live:
            n = self.cancel_all_bot_orders(include_prev_month=True)
            log(f"{self.tag} startup: cancelled {n} leftover bot orders")

    def cancel_all_bot_orders(self, include_prev_month: bool = False) -> int:
        """Cancel this bot's resting orders on the current event, and — when
        `include_prev_month` is set (the base class's name for 'sweep wider',
        kept so inherited callers work) — on the series' recent PRIOR events
        too, which is how a restart across a weekly roll gets cleaned up."""
        if not self.live:
            return super().cancel_all_bot_orders(include_prev_month)
        events: List[str] = []
        if self.state.event_ticker:
            events.append(self.state.event_ticker)
        if include_prev_month:
            for ev in self.recent_event_tickers():
                if ev not in events:
                    events.append(ev)
        n = 0
        for ev in events:
            try:
                orders = self._get_resting_orders(ev)
                if ev == self.state.event_ticker:
                    # Include orders still in flight per the local ledger.
                    orders = self._merge_ledger(orders, time.time())
                for o in orders:
                    if self.cancel_order(o["order_id"]):
                        n += 1
            except Exception as e:
                log(f"{self.tag} ! cancel-all sweep of {ev} failed: {e}")
        return n

    def note_idle(self, now_utc: datetime) -> None:
        self.state.cycles_today += 1
        self.state.last_markets_line = f"idle: {self.w.idle_reason}"
        log(f"{self.tag} {self.w.idle_reason}; idle (next check in "
            f"{IDLE_POLL_SECS:.0f}s)")

    def run_cycle(self) -> None:
        # Discovery first — the window, the market list and the event ticker
        # all come from it. Everything after is the monthly bot's cycle.
        now_utc = datetime.now(timezone.utc)
        if not self.discover(now_utc):
            self.note_idle(now_utc)
            return
        super().run_cycle()


# ---------------------------------------------------------------------------
# --discover: what weekly one-touch events exist right now
# ---------------------------------------------------------------------------

def discover_report(client, now_utc: Optional[datetime] = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    lines = ["Live Kalshi crypto WEEKLY one-touch events:"]
    found = 0
    for key in sorted(MARKETS):
        cfg = MARKETS[key]
        try:
            resp = client.get_markets(series_ticker=cfg.series, status="open", limit=200)
            markets = [m for m in (resp.get("markets") or [])
                       if m.get("status") in ("active", "open")]
        except Exception as e:
            lines.append(f"  {key:9s} {cfg.series:12s} ERROR {e}")
            continue
        if not markets:
            continue
        found += 1
        for ev, ms in sorted(group_by_event(markets).items()):
            start, end = event_window(ms, now_utc)
            front = " <- front" if pick_front_event(markets, now_utc) == ev else ""
            lines.append(f"  {key:9s} {ev:28s} {len(ms):3d} strikes  "
                         f"{start:%Y-%m-%d %H:%MZ} -> {end:%Y-%m-%d %H:%MZ}{front}")
    if not found:
        lines.append("  (none - no configured KX*MAXW / KX*MINW series has an open event)")
        lines.append("  Every weekly bot will sit idle until Kalshi relists one.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", default="BTC-MAX", choices=sorted(MARKETS),
                    help="which crypto weekly one-touch series to make markets on")
    ap.add_argument("--event", default=None,
                    help="pin a specific event ticker instead of auto-picking the front week")
    ap.add_argument("--live", action="store_true",
                    help="actually place/cancel orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--discover", action="store_true",
                    help="list every open weekly one-touch event across all series, then exit")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel this bot's REAL resting orders on the series' recent events, "
                         "then exit (always live — cancelling only reduces risk)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test message via the configured alert route, then exit")
    args = ap.parse_args(argv)

    cfg = MARKETS[args.market]

    if args.test_alert:
        alerter = mm.Alerter(cfg.key, live=False)
        if not alerter.enabled:
            log("cannot test: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not set")
            return 1
        ok = alerter.send_message(f"[{cfg.key} wk] test alert from {MODEL_VERSION} — "
                                  f"alert route working",
                                  subject=f"{cfg.key} weekly test alert")
        log(f"test alert to {mm.ALERT_RECIPIENTS}: {'sent' if ok else 'FAILED'}")
        return 0 if ok else 1

    client = mm.build_client()

    if args.discover:
        print(discover_report(client))
        return 0

    if args.cancel_all:
        # Cancellation only reduces risk: always run it against the real book.
        bot = WeeklyTouchMarketMaker(cfg, client, live=True, pinned_event=args.event)
        try:
            bot.discover(datetime.now(timezone.utc))
        except Exception as e:
            log(f"! discovery failed ({e}); sweeping recent events only")
        n = bot.cancel_all_bot_orders(include_prev_month=True)
        log(f"cancelled {n} real resting orders for {cfg.key} weekly "
            f"(current + {SWEEP_RECENT_EVENTS} recent {cfg.series} events)")
        return 0

    bot = WeeklyTouchMarketMaker(cfg, client, live=args.live, pinned_event=args.event)
    log(f"=== {MODEL_VERSION} market={cfg.key} series={cfg.series} "
        f"prefix={CLIENT_ORDER_PREFIX}- status_dir={STATUS_DIR} ===")
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
