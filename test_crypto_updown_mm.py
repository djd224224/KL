#!/usr/bin/env python3
"""Unit tests for crypto_updown_mm.py (no network, no real orders).

Run:  python -m unittest test_crypto_updown_mm -v
All three fleets together (the isolation tests are the point of doing so):
      python -m unittest test_crypto_touch_mm test_crypto_touch_mm_weekly test_crypto_updown_mm
"""

import contextlib
import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import crypto_touch_mm as mm
import crypto_updown_mm as ud

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
BTC = ud.ASSETS["BTC"]

EV_H = "KXBTCD-26AUG0514"     # hourly, ends 14:00Z
EV_D = "KXBTCD-26AUG0517"     # daily
EV_W = "KXBTCD-26AUG0717"     # weekly


def _frozen(fixed):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    return _DT


@contextlib.contextmanager
def frozen(fixed=NOW):
    stub = _frozen(fixed)
    with mock.patch.object(ud, "datetime", stub), mock.patch.object(mm, "datetime", stub):
        yield


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def mkt(strike, event=EV_H, open_dt=None, end_dt=None, status="active",
        strike_type="greater", ticker=None):
    open_dt = open_dt or NOW - timedelta(hours=1)
    end_dt = end_dt or NOW + timedelta(hours=2)
    # Kalshi listing reality (verified live 2026-08-14): expected_expiration
    # runs 300s AFTER close on every KX*D market. Fixtures reproduce the skew
    # so every gate in the suite is exercised on the same basis as production.
    return {"ticker": ticker or f"{event}-T{strike}", "event_ticker": event,
            "status": status, "market_type": "binary", "strike_type": strike_type,
            "floor_strike": strike, "cap_strike": None,
            "open_time": iso(open_dt), "close_time": iso(end_dt),
            "expected_expiration_time": iso(end_dt + timedelta(seconds=300))}


def hourly_event(n=6, base=100000.0, spacing=100.0, end_hours=2.0):
    return [mkt(base + i * spacing - (n // 2) * spacing, EV_H,
                NOW - timedelta(hours=1), NOW + timedelta(hours=end_hours))
            for i in range(n)]


def daily_event(n=6, base=100000.0, spacing=500.0):
    return [mkt(base + i * spacing - (n // 2) * spacing, EV_D,
                NOW - timedelta(hours=16), NOW + timedelta(hours=9))
            for i in range(n)]


def weekly_event(n=6, base=100000.0, spacing=2000.0):
    return [mkt(base + i * spacing - (n // 2) * spacing, EV_W,
                NOW - timedelta(days=5), NOW + timedelta(days=2))
            for i in range(n)]


def candles(n=400, interval_minutes=1440, sigma_bar=0.01, seed=3, start=100.0):
    """Synthetic OHLC rows shaped like Kraken's: (ts, close, high, low)."""
    rng = random.Random(seed)
    out, px, ts = [], start, 1_700_000_000
    for _ in range(n):
        px *= math.exp(rng.gauss(0, sigma_bar))
        out.append((ts, px, px * 1.001, px * 0.999))
        ts += interval_minutes * 60
    return out


class FakeClient:
    def __init__(self, markets=None, event_list=None, positions=None, orderbook=None):
        self.markets = markets if markets is not None else []
        self.event_list = event_list or []
        self.positions = positions or {}
        self.orderbook = orderbook or {"orderbook": {"yes": [[40, 50]], "no": [[45, 50]]}}
        self.get_markets_calls, self.get_events_calls = [], []
        self.get_orders_calls, self.orderbook_calls = [], []
        self.position_calls, self.cancelled, self.created = [], [], []
        self.orders_by_event = {}

    def get_markets(self, **kw):
        self.get_markets_calls.append(kw)
        return {"markets": self.markets, "cursor": None}

    def get_events(self, **kw):
        self.get_events_calls.append(kw)
        return {"events": [{"event_ticker": t} for t in self.event_list]}

    def get_orders(self, **kw):
        self.get_orders_calls.append(kw)
        return {"orders": self.orders_by_event.get(kw.get("event_ticker"), []),
                "cursor": None}

    def get_order(self, order_id):
        return {"order": {"status": "canceled"}}

    def create_order(self, **kw):
        self.created.append(kw)
        return {"order": {"order_id": f"fake-{len(self.created)}"}}

    def cancel_order(self, order_id, exchange_index=None, market_ticker=None):
        self.cancelled.append(order_id)
        return {}

    def get_positions(self, **kw):
        self.position_calls.append(kw)
        return {"market_positions": [
            {"ticker": t, "position": p}
            for t, p in self.positions.get(kw.get("event_ticker"), {}).items()]}

    def get_orderbook(self, ticker, **kw):
        self.orderbook_calls.append(ticker)
        return self.orderbook

    def __getattr__(self, name):
        raise AssertionError(f"unexpected client call: {name}")


def bot(client=None, live=False, cadences=("hourly", "daily")):
    return ud.UpDownMarketMaker(BTC, client or FakeClient(), live=live, cadences=cadences)


def _candles_ending_at(n, interval_minutes, sigma_bar, spot):
    """Synthetic candles rescaled so the final close equals `spot` — the
    momentum guard compares live spot to hourly-candle closes, and fixtures
    with inconsistent units would trip it in every run_cycle test (exactly
    the data-inconsistency its |ret|>1.0 sanity bound exists for)."""
    rows = candles(400 if n is None else n, interval_minutes, sigma_bar)
    factor = spot / rows[-1][1]
    return [(ts, c * factor, h * factor, lo * factor) for ts, c, h, lo in rows]


@contextlib.contextmanager
def priced(spot=100000.0, sigma_bar=0.01):
    with frozen(), \
         mock.patch.object(mm, "fetch_live_price", return_value=spot), \
         mock.patch.object(mm, "fetch_ohlc",
                           side_effect=lambda cfg, iv:
                           _candles_ending_at(400, iv, sigma_bar, spot)):
        yield


# ---------------------------------------------------------------------------
# The reason this module exists at all
# ---------------------------------------------------------------------------

class TestTerminalVsTouch(unittest.TestCase):
    """These markets look like MAX one-touch markets (strike_type='greater' +
    floor_strike) but settle on the TERMINAL price. Pricing them with the
    one-touch model roughly doubles every fair. Pin that difference."""

    def test_one_touch_is_about_double(self):
        for strike, t_days in [(101, 1 / 24), (102, 25 / 24), (110, 7.0), (130, 7.0)]:
            term = ud.terminal_prob(100, strike, 0.04, t_days)
            touch = mm.touch_prob(100, strike, 0.04, t_days, "max")
            self.assertGreater(touch, term)
            self.assertAlmostEqual(touch / term, 2.0, delta=0.15,
                                   msg=f"K={strike} T={t_days}")

    def test_the_two_models_are_not_interchangeable_in_cents(self):
        # A market truly worth 31c would be quoted at 62c by the touch model.
        self.assertEqual(ud.fair_value_cents(100, 102, 0.04, 25 / 24), 31)
        self.assertEqual(mm.fair_value_cents(100, 102, 0.04, 25 / 24, "max"), 62)


class TestTerminalProb(unittest.TestCase):
    def test_matches_exact_monte_carlo(self):
        rng = random.Random(11)
        for spot, strike, sigma, t in [(100, 101, 0.03, 1 / 24), (100, 100, 0.03, 1 / 24),
                                       (100, 95, 0.04, 25 / 24), (100, 110, 0.05, 7.0)]:
            nu = -0.5 * sigma * sigma
            mu, sd, lk = nu * t, sigma * math.sqrt(t), math.log(strike / spot)
            n = 200000
            hits = sum(1 for _ in range(n) if rng.gauss(mu, sd) >= lk)
            self.assertAlmostEqual(ud.terminal_prob(spot, strike, sigma, t),
                                   hits / n, delta=0.006,
                                   msg=f"{spot}->{strike} sig={sigma} T={t}")

    def test_at_the_money_is_just_under_half(self):
        # Driftless in PRICE means the log drift is -sig^2/2, so the median
        # finishes just below spot.
        p = ud.terminal_prob(100, 100, 0.03, 1.0)
        self.assertLess(p, 0.5)
        self.assertGreater(p, 0.48)

    def test_monotonic_in_strike_and_vol(self):
        prev = 1.0
        for strike in range(96, 106):
            p = ud.terminal_prob(100, strike, 0.04, 1.0)
            self.assertLess(p, prev)
            prev = p
        # a far OTM strike gets likelier as vol rises
        self.assertLess(ud.terminal_prob(100, 110, 0.02, 1.0),
                        ud.terminal_prob(100, 110, 0.08, 1.0))

    def test_degenerate_inputs(self):
        self.assertEqual(ud.terminal_prob(100, 99, 0.04, 0.0), 1.0)
        self.assertEqual(ud.terminal_prob(100, 101, 0.04, 0.0), 0.0)
        self.assertEqual(ud.terminal_prob(100, 101, 0.0, 1.0), 0.0)
        with self.assertRaises(mm.DataError):
            ud.terminal_prob(0, 101, 0.04, 1.0)

    def test_fair_value_is_clamped_to_a_tradeable_cent(self):
        self.assertEqual(ud.fair_value_cents(100, 1e9, 0.04, 1.0), 1)
        self.assertEqual(ud.fair_value_cents(100, 1e-9, 0.04, 1.0), 99)


class TestVol(unittest.TestCase):
    def test_scales_bar_vol_to_a_daily_number(self):
        # 5m bars with sigma 0.001/bar -> 0.001*sqrt(288) per day
        sigma = ud.scaled_daily_vol(candles(400, 5, 0.001), 5)
        self.assertAlmostEqual(sigma, 0.001 * math.sqrt(288), delta=0.006)

    def test_interval_matches_the_horizon(self):
        self.assertEqual(ud.vol_interval_for_horizon(1 / 24), 5)      # hourly
        self.assertEqual(ud.vol_interval_for_horizon(25 / 24), 60)    # daily
        self.assertEqual(ud.vol_interval_for_horizon(7.0), 1440)      # weekly

    def test_unknown_interval_rejected(self):
        with self.assertRaises(mm.DataError):
            ud.scaled_daily_vol(candles(50, 7), 7)

    def test_too_few_returns_rejected(self):
        with self.assertRaises(mm.DataError):
            ud.scaled_daily_vol(candles(5, 60), 60)

    def test_blend_sits_between_fast_and_slow(self):
        b = bot()
        with mock.patch.object(mm, "fetch_ohlc",
                               side_effect=lambda cfg, iv: candles(
                                   400, iv, 0.02 if iv == 5 else 0.005)):
            fast = b._sigma_at(5, 0.0)
            slow = b._sigma_at(1440, 0.0)
            blended = b.sigma_for_horizon(1 / 24, 0.0)
        self.assertGreater(fast, slow)
        self.assertGreater(blended, slow)
        self.assertLess(blended, fast)

    def test_falls_back_to_daily_when_intraday_unavailable(self):
        b = bot()
        def ohlc(cfg, iv):
            if iv == 5:
                raise RuntimeError("kraken 5m down")
            return candles(400, iv, 0.01)
        with mock.patch.object(mm, "fetch_ohlc", side_effect=ohlc):
            self.assertAlmostEqual(b.sigma_for_horizon(1 / 24, 0.0),
                                   b._sigma_at(1440, 0.0), places=9)

    def test_insane_vol_fails_the_cycle(self):
        b = bot()
        with mock.patch.object(mm, "fetch_ohlc",
                               side_effect=lambda cfg, iv: candles(400, iv, 5.0)):
            with self.assertRaises(mm.DataError):
                b._sigma_at(1440, 0.0)

    def test_sigma_is_cached(self):
        b = bot()
        with mock.patch.object(mm, "fetch_ohlc",
                               side_effect=lambda cfg, iv: candles(400, iv, 0.01)) as f:
            b._sigma_at(1440, 1000.0)
            b._sigma_at(1440, 1000.0 + ud.VOL_REFRESH_SECS - 1)
            self.assertEqual(f.call_count, 1)
            b._sigma_at(1440, 1000.0 + ud.VOL_REFRESH_SECS + 1)
            self.assertEqual(f.call_count, 2)


# ---------------------------------------------------------------------------
# Market/event structure
# ---------------------------------------------------------------------------

class TestStrikeExtraction(unittest.TestCase):
    def test_greater_markets_only(self):
        self.assertEqual(ud.updown_strike(mkt(54099.99)), 54099.99)
        self.assertIsNone(ud.updown_strike(mkt(1, strike_type="less")))
        self.assertIsNone(ud.updown_strike(mkt(1, strike_type="between")))

    def test_custom_doge_rungs_are_recovered_from_the_ticker(self):
        """ALL of DOGE uses strike_type='custom' with floor_strike=None; the
        strike lives only in the ticker. Unhandled, DOGE quoted nothing."""
        m = {"ticker": "KXDOGED-26AUG0717-T0.1799999", "strike_type": "custom",
             "floor_strike": None, "cap_strike": None,
             "yes_sub_title": "$0.18 or above"}
        self.assertAlmostEqual(ud.updown_strike(m), 0.1799999, places=7)

    def test_custom_rung_needs_the_subtitle_to_agree(self):
        base = {"ticker": "KXDOGED-26AUG0717-T0.1799999", "strike_type": "custom",
                "floor_strike": None, "yes_sub_title": "$0.18 or above"}
        # subtitle number disagrees with the ticker -> refuse to guess
        self.assertIsNone(ud.updown_strike(dict(base, yes_sub_title="$0.25 or above")))
        # not a greater-payoff rung
        self.assertIsNone(ud.updown_strike(dict(base, yes_sub_title="$0.18 or below")))
        self.assertIsNone(ud.updown_strike(dict(base, yes_sub_title="")))
        # no parseable strike in the ticker
        self.assertIsNone(ud.updown_strike(dict(base, ticker="KXDOGED-26AUG0717-XYZ")))
        # a genuinely different structure is still skipped
        self.assertIsNone(ud.updown_strike(dict(base, strike_type="between")))

    def test_custom_rung_handles_thousands_separators(self):
        m = {"ticker": "KXBTCD-26AUG0717-T63999.99", "strike_type": "custom",
             "floor_strike": None, "yes_sub_title": "$64,000 or above"}
        self.assertAlmostEqual(ud.updown_strike(m), 63999.99, places=2)

    def test_greater_path_is_unchanged_by_the_custom_support(self):
        self.assertEqual(ud.updown_strike(mkt(54099.99)), 54099.99)
        m = mkt(1); m["strike_type"] = "greater"; m["floor_strike"] = None
        self.assertIsNone(ud.updown_strike(m))

    def test_missing_or_bad_strike(self):
        m = mkt(1)
        m["floor_strike"] = None
        self.assertIsNone(ud.updown_strike(m))
        del m["floor_strike"]
        self.assertIsNone(ud.updown_strike(m))


class TestCadenceClassification(unittest.TestCase):
    def test_real_observed_windows(self):
        # Measured from the live API on 2026-08-05; annual added 2026-08-13
        # (KX{ASSET}Y end-of-year events run ~8700h from their January open).
        self.assertEqual(ud.classify_cadence(1.0), "hourly")
        self.assertEqual(ud.classify_cadence(25.0), "daily")
        self.assertEqual(ud.classify_cadence(169.0), "weekly")
        self.assertEqual(ud.classify_cadence(8700.0), "annual")

    def test_boundaries(self):
        self.assertEqual(ud.classify_cadence(ud.HOURLY_MAX_HOURS), "hourly")
        self.assertEqual(ud.classify_cadence(ud.HOURLY_MAX_HOURS + 0.1), "daily")
        self.assertEqual(ud.classify_cadence(ud.DAILY_MAX_HOURS), "daily")
        self.assertEqual(ud.classify_cadence(ud.DAILY_MAX_HOURS + 0.1), "weekly")
        self.assertEqual(ud.classify_cadence(ud.WEEKLY_MAX_HOURS), "weekly")
        self.assertEqual(ud.classify_cadence(ud.WEEKLY_MAX_HOURS + 0.1), "annual")

    def test_parse_cadences(self):
        self.assertEqual(ud.parse_cadences("both"), ("hourly", "daily"))
        self.assertEqual(ud.parse_cadences(""), ("hourly", "daily"))
        self.assertEqual(ud.parse_cadences("all"),
                         ("hourly", "daily", "weekly", "annual"))
        self.assertEqual(ud.parse_cadences("weekly"), ("weekly",))
        self.assertEqual(ud.parse_cadences("daily, hourly"), ("daily", "hourly"))
        self.assertEqual(ud.parse_cadences("daily,daily"), ("daily",))
        with self.assertRaises(ValueError):
            ud.parse_cadences("monthly")


class TestEventViews(unittest.TestCase):
    def test_groups_and_classifies(self):
        views = ud.build_event_views(hourly_event() + daily_event() + weekly_event(), NOW)
        by = {v.ticker: v for v in views}
        self.assertEqual(by[EV_H].cadence, "hourly")
        self.assertEqual(by[EV_D].cadence, "daily")
        self.assertEqual(by[EV_W].cadence, "weekly")

    def test_sorted_soonest_first(self):
        views = ud.build_event_views(weekly_event() + daily_event() + hourly_event(), NOW)
        self.assertEqual([v.ticker for v in views], [EV_H, EV_D, EV_W])

    def test_window_uses_event_extremes(self):
        v = ud.build_event_views(hourly_event(end_hours=2.0), NOW)[0]
        self.assertAlmostEqual(v.window_hours, 3.0, places=6)
        self.assertAlmostEqual(v.secs_left(NOW) / 3600, 2.0, places=6)

    def test_markets_without_end_times_ignored(self):
        bad = mkt(1, "nodates")
        for k in ("expected_expiration_time", "close_time", "latest_expiration_time"):
            bad[k] = "nope"
        views = ud.build_event_views([bad] + hourly_event(), NOW)
        self.assertEqual([v.ticker for v in views], [EV_H])

    def test_selection_filters_by_cadence(self):
        views = ud.build_event_views(hourly_event() + daily_event() + weekly_event(), NOW)
        self.assertEqual([v.ticker for v in ud.select_events(views, ("daily",), NOW)], [EV_D])
        self.assertEqual([v.ticker for v in ud.select_events(views, ud.CADENCES, NOW)],
                         [EV_H, EV_D, EV_W])

    def test_end_is_the_settlement_print_not_expected_expiration(self):
        """The print is the 60s average ending at close_time;
        expected_expiration runs 300s later. Gating on the latter shifted
        every stand-down 5 minutes late (adversarial review, 2026-08-14)."""
        m = mkt(100000)
        self.assertEqual(ud.market_end_utc(m), NOW + timedelta(hours=2))
        no_close = dict(m)
        del no_close["close_time"]
        self.assertEqual(ud.market_end_utc(no_close),
                         NOW + timedelta(hours=2, seconds=300))

    def test_hourly_stand_down_fires_on_the_close_basis(self):
        # On the expected_expiration basis secs_left never dropped below 300,
        # so the 180s hourly gate was dead code.
        soon = [mkt(100000, EV_H, NOW - timedelta(hours=1),
                    NOW + timedelta(seconds=170))]
        views = ud.build_event_views(soon, NOW)
        self.assertEqual(views[0].cadence, "hourly")
        self.assertEqual(ud.select_events(views, ("hourly",), NOW), [])

    def test_daily_sharing_the_weekly_print_stands_down(self):
        """Jack 2026-08-14: on Fridays the 5pm close IS the weekly event, and
        a daily event ending on the same print would be the same bet quoted
        twice — the shorter tenor stands down, the weekly keeps quoting."""
        wk_end = NOW + timedelta(days=2)
        weekly = [mkt(100000, EV_W, NOW - timedelta(days=5), wk_end)]
        colliding_daily = [mkt(100000, EV_D, wk_end - timedelta(hours=25), wk_end,
                               ticker=f"{EV_D}-T100000x")]
        views = ud.build_event_views(weekly + colliding_daily, NOW)
        selected = ud.select_events(views, ("daily", "weekly"), NOW)
        self.assertEqual([v.ticker for v in selected], [EV_W])

    def test_daily_on_its_own_print_is_kept(self):
        # Saturday's daily ends 9h from now, the weekly 2d from now: no clash.
        views = ud.build_event_views(daily_event() + weekly_event(), NOW)
        selected = ud.select_events(views, ("daily", "weekly"), NOW)
        self.assertEqual({v.cadence for v in selected}, {"daily", "weekly"})

    def test_collision_rule_generalizes_to_hourly(self):
        # The 5pm hourly ends on the daily/weekly print every day.
        wk_end = NOW + timedelta(days=2)
        weekly = [mkt(100000, EV_W, NOW - timedelta(days=5), wk_end)]
        hourly = [mkt(100000, EV_H, wk_end - timedelta(hours=1), wk_end,
                      ticker=f"{EV_H}-T100000x")]
        views = ud.build_event_views(weekly + hourly, NOW)
        selected = ud.select_events(views, ud.CADENCES, NOW)
        self.assertEqual([v.cadence for v in selected], ["weekly"])

    def test_collision_needs_the_longer_tenor_to_be_selected(self):
        # A lone daily (weekly cadence not requested / weekly event absent)
        # has nothing of ours to collide with.
        wk_end = NOW + timedelta(days=2)
        daily = [mkt(100000, EV_D, wk_end - timedelta(hours=25), wk_end)]
        views = ud.build_event_views(daily, NOW)
        self.assertEqual(len(ud.select_events(views, ("daily",), NOW)), 1)

    def test_same_print_daily_stays_benched_through_weekly_stand_down(self):
        """When the weekly enters its own final-hour stand-down it leaves the
        expiry-filtered set — the shared print is at its most dangerous right
        then, and a same-print daily must NOT wake up for the finale. The
        collision check therefore runs against all cadence-matched views."""
        wk_end = NOW + timedelta(minutes=30)
        weekly = [mkt(100000, EV_W, wk_end - timedelta(days=7), wk_end)]
        daily = [mkt(100000, EV_D, wk_end - timedelta(hours=25), wk_end,
                     ticker=f"{EV_D}-T100000x")]
        views = ud.build_event_views(weekly + daily, NOW)
        self.assertEqual({v.cadence for v in views}, {"daily", "weekly"})
        self.assertEqual(ud.select_events(views, ("daily", "weekly"), NOW), [])

    def test_selection_drops_events_near_expiry(self):
        # 1 minute left on an hourly event: inside the 3-minute stand-down.
        soon = [mkt(100000, EV_H, NOW - timedelta(hours=1), NOW + timedelta(minutes=1))]
        views = ud.build_event_views(soon, NOW)
        self.assertEqual(views[0].cadence, "hourly")
        self.assertEqual(ud.select_events(views, ("hourly",), NOW), [])

    def test_expired_events_dropped(self):
        past = [mkt(100000, EV_H, NOW - timedelta(hours=2), NOW - timedelta(minutes=5))]
        views = ud.build_event_views(past, NOW)
        self.assertEqual(ud.select_events(views, ud.CADENCES, NOW), [])


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------

class TestQuotableBand(unittest.TestCase):
    def band(self, markets, spot=100000.0, sigma_bar=0.01):
        b = bot()
        view = ud.build_event_views(markets, NOW)[0]
        with mock.patch.object(mm, "fetch_ohlc",
                               side_effect=lambda cfg, iv: candles(400, iv, sigma_bar)):
            return b.quotable_markets(view, spot, NOW, 0.0)

    def test_drops_strikes_outside_the_fair_band(self):
        far = [mkt(100000 + d, EV_H, NOW - timedelta(hours=1), NOW + timedelta(hours=2))
               for d in (-90000, -50000, 0, 50000, 90000)]
        band = self.band(far)
        for _m, _t, _k, fair in band:
            self.assertGreater(fair, ud.SKIP_FAIR_BELOW_CENTS)
            self.assertLess(fair, ud.SKIP_FAIR_ABOVE_CENTS)

    def test_capped_and_ordered_near_money_first(self):
        many = [mkt(100000 + i * 20 - 2000, EV_H,
                    NOW - timedelta(hours=1), NOW + timedelta(hours=2),
                    ticker=f"{EV_H}-T{i}")
                for i in range(200)]
        band = self.band(many)
        self.assertLessEqual(len(band), ud.MAX_MARKETS_PER_EVENT)
        moneyness = [row[0] for row in band]
        self.assertEqual(moneyness, sorted(moneyness))
        self.assertLessEqual(moneyness[0], 12)      # nearest is close to 50c

    def test_non_greater_markets_skipped(self):
        ms = hourly_event()
        ms.append(mkt(100000, EV_H, NOW - timedelta(hours=1),
                      NOW + timedelta(hours=2), strike_type="less",
                      ticker=f"{EV_H}-LESS"))
        self.assertNotIn(f"{EV_H}-LESS", [row[1] for row in self.band(ms)])


class TestRunCycle(unittest.TestCase):
    def test_idle_when_nothing_open(self):
        b = bot(FakeClient(markets=[]))
        with priced():
            b.run_cycle()
        self.assertIn("no open", b.u.idle_reason)
        self.assertEqual(b.poll_interval(), ud.IDLE_POLL_SECS)
        self.assertEqual(b.state.sim_orders, {})

    def test_idle_reports_expiry_filtering(self):
        soon = [mkt(100000, EV_H, NOW - timedelta(hours=1), NOW + timedelta(minutes=1))]
        b = bot(FakeClient(markets=soon))
        with priced():
            b.run_cycle()
        self.assertIn("too close to expiry", b.u.idle_reason)

    def test_quotes_multiple_tenors_at_once(self):
        c = FakeClient(markets=hourly_event() + daily_event() + weekly_event())
        b = bot(c, cadences=ud.CADENCES)
        with priced():
            b.run_cycle()
        self.assertEqual(len(b.u.events), 3)
        quoted_events = {o["ticker"].rsplit("-T", 1)[0] for o in b.state.sim_orders.values()}
        self.assertEqual(quoted_events, {EV_H, EV_D, EV_W})
        # one positions read per event, not per market
        self.assertEqual(len(c.position_calls), 3)

    def test_cadence_filter_is_honoured(self):
        c = FakeClient(markets=hourly_event() + daily_event() + weekly_event())
        b = bot(c, cadences=("weekly",))
        with priced():
            b.run_cycle()
        self.assertEqual([v.ticker for v in b.u.events], [EV_W])
        self.assertTrue(all(o["ticker"].startswith(EV_W)
                            for o in b.state.sim_orders.values()))

    def test_ladder_is_the_live_pilot_shape(self):
        """Pins the deployed config: 3 levels x 2 contracts (doubled from 3x1
        on 2026-08-10), 3c off fair, 2c apart, joining and never leading.
        Changing any of these is a size change on a live bot and should have
        to break a test first."""
        c = FakeClient(markets=hourly_event(n=1))
        b = bot(c, cadences=("hourly",))
        with priced():
            b.run_cycle()
        placed = list(b.state.sim_orders.values())
        bids = sorted((o["yes_price"] for o in placed if o["book_side"] == "bid"),
                      reverse=True)
        asks = sorted(o["yes_price"] for o in placed if o["book_side"] == "ask")
        self.assertEqual(len(bids), 3)
        self.assertEqual(len(asks), 3)
        self.assertTrue(all(o["remaining_count"] == 2 for o in placed))
        self.assertLessEqual(bids[0], 40)      # external bid; join, never lead
        self.assertGreaterEqual(asks[0], 55)   # external ask = 100-45
        self.assertEqual([b1 - b2 for b1, b2 in zip(bids, bids[1:])],
                         [ud.LEVEL_SPACING_CENTS] * 2)
        self.assertEqual([a2 - a1 for a1, a2 in zip(asks, asks[1:])],
                         [ud.LEVEL_SPACING_CENTS] * 2)

    def test_at_most_six_contracts_rest_per_market_side(self):
        c = FakeClient(markets=hourly_event(n=3))
        b = bot(c, cadences=("hourly",))
        with priced():
            b.run_cycle()
        totals = {}
        for o in b.state.sim_orders.values():
            key = (o["ticker"], o["book_side"])
            totals[key] = totals.get(key, 0) + o["remaining_count"]
        self.assertTrue(totals)
        self.assertLessEqual(max(totals.values()), 6)

    def test_daily_tenor_ladder_is_the_live_shape(self):
        """BTC daily quotes a SINGLE 1-contract rung per side; the six benign
        assets keep 1/1 (two rungs) — Jack 2026-08-18 "do 1 only on BTC,
        keep 1/1 on others". Weekly keeps 3x2 @3c everywhere. Changing any
        of these is a size change on a live bot and should have to break a
        test first."""
        self.assertEqual(ud.CONTRACTS_PER_LEVEL_BY_CADENCE, {"daily": 1})
        self.assertEqual(ud.NUM_LEVELS_BY_CADENCE, {"daily": 2})
        self.assertEqual(ud.NUM_LEVELS_BY_ASSET_CADENCE, {("BTC", "daily"): 1})
        self.assertEqual(ud.QUOTE_OFFSET_BY_CADENCE, {"daily": 4})
        c = FakeClient(markets=daily_event() + weekly_event())
        b = bot(c, cadences=("daily", "weekly"))
        with priced():
            b.run_cycle()
        sizes, side_counts = {}, {}
        for o in b.state.sim_orders.values():
            cad = "daily" if o["ticker"].startswith(EV_D) else "weekly"
            sizes.setdefault(cad, set()).add(o["remaining_count"])
            key = (cad, o["ticker"], o["book_side"])
            side_counts[key] = side_counts.get(key, 0) + 1
        self.assertEqual(sizes["daily"], {1})
        self.assertEqual(sizes["weekly"], {2})
        daily_levels = {n for (cad, _t, _s), n in side_counts.items() if cad == "daily"}
        weekly_levels = {n for (cad, _t, _s), n in side_counts.items() if cad == "weekly"}
        self.assertEqual(max(daily_levels), 1)         # BTC: single rung per side
        self.assertEqual(max(weekly_levels), 3)        # weekly unchanged
        # the six benign assets keep two daily rungs
        eth = ud.UpDownMarketMaker(ud.ASSETS["ETH"],
                                   FakeClient(markets=daily_event() + weekly_event()),
                                   live=False, cadences=("daily", "weekly"))
        with priced():
            eth.run_cycle()
        eth_counts = {}
        for o in eth.state.sim_orders.values():
            if o["ticker"].startswith(EV_D):
                key = (o["ticker"], o["book_side"])
                eth_counts[key] = eth_counts.get(key, 0) + 1
        self.assertEqual(max(eth_counts.values()), 2)

    def test_daily_offset_is_four_cents_minimum_edge(self):
        """Same args run_cycle passes for a daily event: 2 rungs x 1 @4c.
        The offset is the MINIMUM edge vs fair — join-don't-lead still caps
        the anchor at the external best, so it binds only when fair sits
        within 4c of the touch (exactly the near-money pick-off zone that
        lost the money)."""
        # fair 43 on a 40/60 book: bid anchor min(43-4, 40) = 39 — the 4c
        # edge holds us 1c BEHIND a touch the old 3c offset would have joined.
        q = mm.build_quotes("T", 43, 40, 60, 99, 99, 2, 1, 4, 2)
        self.assertEqual(sorted(x.price_cents for x in q if x.book_side == "bid"),
                         [37, 39])
        self.assertEqual(sorted(x.price_cents for x in q if x.book_side == "ask"),
                         [60, 62])
        # mirror image on the ask side: fair 57 -> ask anchor max(61, 60) = 61
        q = mm.build_quotes("T", 57, 40, 60, 99, 99, 2, 1, 4, 2)
        self.assertEqual(sorted(x.price_cents for x in q if x.book_side == "ask"),
                         [61, 63])
        self.assertEqual(sorted(x.price_cents for x in q if x.book_side == "bid"),
                         [38, 40])

    def test_deselected_event_orders_are_cleared_not_left_to_ttl(self):
        """An event that leaves selection (stand-down/collision/delisting)
        must have its book actively cleared — leaving it to the exchange TTL
        parks up to 600s of stale quotes in the final minutes before the
        settlement print (adversarial review, 2026-08-14)."""
        c = FakeClient(markets=daily_event() + weekly_event())
        b = bot(c, cadences=("daily", "weekly"))
        with priced():
            b.run_cycle()
        self.assertTrue(any(o["ticker"].startswith(EV_D)
                            for o in b.state.sim_orders.values()))
        # the daily event is now inside its 15-minute stand-down
        dying = [dict(m, close_time=iso(NOW + timedelta(minutes=10)),
                      expected_expiration_time=iso(NOW + timedelta(minutes=15)))
                 for m in daily_event()]
        c.markets = dying + weekly_event()
        with priced():
            b.run_cycle()
        self.assertFalse(any(o["ticker"].startswith(EV_D)
                             for o in b.state.sim_orders.values()))
        self.assertTrue(any(o["ticker"].startswith(EV_W)
                            for o in b.state.sim_orders.values()))

    def test_live_deselect_sweep_cancels_on_the_exchange(self):
        c = FakeClient()
        b = bot(c, live=True, cadences=("daily", "weekly"))
        c.orders_by_event[EV_D] = [
            {"order_id": "o1", "client_order_id": "cud-x-1", "status": "resting"},
            {"order_id": "o2", "client_order_id": "cud-x-2", "status": "resting"},
        ]
        b.u.sweep_pending = {EV_D: 0}
        b.sweep_deselected()
        self.assertEqual(sorted(c.cancelled), ["o1", "o2"])
        self.assertIn(EV_D, b.u.sweep_pending)   # not proven clean yet
        # two consecutive clean reads retire the sweep — an order in flight
        # during the first sweep is caught by the second
        c.orders_by_event[EV_D] = []
        b.sweep_deselected()
        self.assertEqual(b.u.sweep_pending, {EV_D: 1})
        b.sweep_deselected()
        self.assertEqual(b.u.sweep_pending, {})

    def test_singleton_lock_lands_in_the_fleet_status_dir(self):
        # The updown/annual fleets lock in their OWN heartbeat dirs — a lock
        # in the touch fleet's dir would collide across fleets on cfg.key.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(mm.acquire_singleton("BTC", status_dir=td))
            self.assertTrue(os.path.exists(os.path.join(td, "lock_BTC.pid")))
            # same-pid re-acquire must not deadlock the restart loop
            self.assertTrue(mm.acquire_singleton("BTC", status_dir=td))

    def test_band_is_ten_to_ninety(self):
        self.assertEqual((ud.SKIP_FAIR_BELOW_CENTS, ud.SKIP_FAIR_ABOVE_CENTS), (10, 90))
        self.assertEqual(ud.MAX_MARKETS_PER_EVENT, 8)


class TestDailyDefenses(unittest.TestCase):
    """The 2026-08-17 daily-tenor defenses (fill autopsy: all-day bleed,
    worst overnight, 3.3x on the NO side, all BTC): inventory skew,
    momentum stand-down, per-asset offset. All daily-only — weekly's
    warehousing made +252 its first settled week and stays untouched."""

    def test_config_is_pinned(self):
        self.assertEqual(ud.SKEW_BY_CADENCE, {"daily": (4.0, 25.0)})
        self.assertEqual(ud.SKEW_EDGE_FLOOR_CENTS, 3.0)
        self.assertEqual(ud.MOMO_CADENCES, ("daily",))
        self.assertEqual(ud.MOMO_Z, 1.5)
        self.assertEqual(ud.MOMO_HOLD_SECS, 1800)
        self.assertEqual(ud.QUOTE_OFFSET_BY_ASSET_CADENCE, {("BTC", "daily"): 6})

    def test_one_hour_ref_uses_candle_close_times(self):
        """Kraken candle timestamps are OPEN times. The reference must be the
        close of the latest bar that CLOSED >= 1h ago — an hourly-candle
        filter on open times anchored the reference to the top of the
        CURRENT hour (0-60min window, blind after each boundary; caught in
        adversarial review 2026-08-17)."""
        now = NOW.timestamp()
        # 5m bars, open-stamped, close price encodes the bar's close time so
        # the selection is self-evident in the assertion
        rows = [(int(now - k * 300), float(now - (k - 1) * 300), 0.0, 0.0)
                for k in range(30, 0, -1)]
        b = bot()
        with mock.patch.object(mm, "fetch_ohlc", return_value=rows):
            ref = b.price_one_hour_ago(now)
        # latest bar with close (open+300) <= now-3600 closed exactly at now-3600
        self.assertEqual(ref, now - 3600)

    def test_momentum_triggers_through_real_candles(self):
        """End-to-end with NOTHING mocked below fetch_ohlc: flat until 1h
        ago, then a +1% ramp — the trailing-1h window must see the full move
        (the old top-of-hour anchoring saw only a fragment)."""
        now = NOW.timestamp()
        rows = []
        for k in range(48, 0, -1):
            open_ts = int(now - k * 300)
            px = 100000.0 if open_ts + 300 <= now - 3600 else 101000.0
            rows.append((open_ts, px, px, px))
        b = bot()
        b.state.sigma_daily = 0.024      # sigma_1h ~ 0.49% < 1% move
        with mock.patch.object(mm, "fetch_ohlc", return_value=rows):
            self.assertEqual(b.refresh_momentum_block(101000.0, now), "ask")
            self.assertAlmostEqual(b.u.last_ret_1h, math.log(1.01), places=6)

    def test_skew_thinned_edge_never_drops_below_the_floor(self):
        """Account-level net can be someone else's position (manual trades
        share the account), so the skew-thinned side keeps >= 3c of edge:
        BTC daily (offset 6) allows +/-3c of effective skew, others (offset
        4) +/-1c. Saturated short on BTC must shift quotes by exactly +3."""
        def daily_bids(asset_key, positions):
            cfg = ud.ASSETS[asset_key]
            c = FakeClient(markets=daily_event() + weekly_event(),
                           positions=positions)
            b = ud.UpDownMarketMaker(cfg, c, live=False,
                                     cadences=("daily", "weekly"))
            with priced():
                b.run_cycle()
            return {(o["ticker"], o["book_side"]): o["yes_price"]
                    for o in b.state.sim_orders.values()
                    if o["ticker"].startswith(EV_D)}

        for asset, max_shift in (("BTC", 3), ("ETH", 1)):
            flat = daily_bids(asset, {})
            short = daily_bids(asset, {EV_D: {f"{EV_D}-T100000.0": -25.0}})
            diffs = [short[k] - flat[k] for k in short if k in flat]
            self.assertTrue(diffs, asset)
            self.assertLessEqual(max(diffs), max_shift, asset)
            self.assertEqual(max(diffs), max_shift, asset)   # binds somewhere
            self.assertGreaterEqual(min(diffs), 0, asset)    # never wrong way

    def test_skew_sign_and_saturation(self):
        # short YES (the BTC failure) -> POSITIVE skew: quote richer, buy back
        self.assertGreater(ud.inventory_skew_cents(-10, 4, 25), 0)
        # long YES -> negative skew: quote cheaper, shed
        self.assertLess(ud.inventory_skew_cents(10, 4, 25), 0)
        self.assertEqual(ud.inventory_skew_cents(0, 4, 25), 0.0)
        self.assertEqual(ud.inventory_skew_cents(-25, 4, 25), 4.0)
        self.assertEqual(ud.inventory_skew_cents(-250, 4, 25), 4.0)   # saturates
        self.assertEqual(ud.inventory_skew_cents(25, 4, 25), -4.0)
        self.assertAlmostEqual(ud.inventory_skew_cents(-10, 4, 25), 1.6)
        self.assertEqual(ud.inventory_skew_cents(-10, 0, 25), 0.0)    # disabled
        self.assertEqual(ud.inventory_skew_cents(-10, 4, 0), 0.0)

    def test_momentum_block_side(self):
        sigma_d = 0.024                      # sigma_1h ~ 0.49%
        self.assertEqual(ud.momentum_block_side(+0.010, sigma_d), "ask")
        self.assertEqual(ud.momentum_block_side(-0.010, sigma_d), "bid")
        self.assertIsNone(ud.momentum_block_side(+0.004, sigma_d))    # < 1.5 sigma
        self.assertIsNone(ud.momentum_block_side(+0.010, 0.0))        # no vol -> off

    def test_short_inventory_lifts_daily_quotes(self):
        """Same book, same fair; a short event position must move BOTH sides
        of the daily ladder UP (stop re-offering cheap, bid closer to buying
        back) and leave the weekly ladder alone."""
        def quotes_with(positions):
            c = FakeClient(markets=daily_event() + weekly_event(),
                           positions=positions)
            b = bot(c, cadences=("daily", "weekly"))
            with priced():
                b.run_cycle()
            out = {"daily": [], "weekly": []}
            for o in b.state.sim_orders.values():
                cad = "daily" if o["ticker"].startswith(EV_D) else "weekly"
                out[cad].append((o["ticker"], o["book_side"], o["yes_price"]))
            return out

        flat = quotes_with({})
        # -25 on one daily market saturates the skew at +4c
        short = quotes_with({EV_D: {f"{EV_D}-T100000.0": -25.0}})
        flat_by_key = {(t, s): p for t, s, p in flat["daily"]}
        moved = 0
        for t, s, p in short["daily"]:
            if (t, s) in flat_by_key and p > flat_by_key[(t, s)]:
                moved += 1
        self.assertGreater(moved, 0)   # daily quotes shifted up
        self.assertEqual(sorted(flat["weekly"]), sorted(short["weekly"]))

    def test_momentum_blocks_asks_on_daily_only(self):
        c = FakeClient(markets=daily_event() + weekly_event())
        b = bot(c, cadences=("daily", "weekly"))
        with priced():
            # trip the guard directly: strong up-move already observed
            b.u.momo_side, b.u.momo_until = "ask", NOW.timestamp() + 900
            with mock.patch.object(b, "refresh_momentum_block",
                                   return_value="ask"):
                b.run_cycle()
        daily_sides = {o["book_side"] for o in b.state.sim_orders.values()
                       if o["ticker"].startswith(EV_D)}
        weekly_sides = {o["book_side"] for o in b.state.sim_orders.values()
                        if o["ticker"].startswith(EV_W)}
        self.assertEqual(daily_sides, {"bid"})          # asks suppressed
        self.assertEqual(weekly_sides, {"bid", "ask"})  # weekly untouched

    def test_momentum_hold_persists_after_trigger_clears(self):
        b = bot()
        b.state.sigma_daily = 0.024
        now = NOW.timestamp()
        with mock.patch.object(b, "price_one_hour_ago", return_value=100000.0):
            # +1% in 1h vs sigma_1h 0.49% -> trigger
            self.assertEqual(b.refresh_momentum_block(101000.0, now), "ask")
            # move faded: still blocked inside the hold window...
            self.assertEqual(b.refresh_momentum_block(100100.0, now + 600), "ask")
            # ...and released after it
            self.assertIsNone(
                b.refresh_momentum_block(100100.0, now + ud.MOMO_HOLD_SECS + 601))

    def test_momentum_failure_leaves_guard_inactive(self):
        b = bot()
        b.state.sigma_daily = 0.024
        with mock.patch.object(b, "price_one_hour_ago",
                               side_effect=RuntimeError("kraken down")):
            self.assertIsNone(b.refresh_momentum_block(100000.0, NOW.timestamp()))

    def test_btc_daily_offset_is_six_cents_others_four(self):
        b = bot()   # BTC
        self.assertEqual(
            b.quote_offset_by_asset_cadence.get(("BTC", "daily")), 6)
        self.assertIsNone(
            b.quote_offset_by_asset_cadence.get(("ETH", "daily")))
        # ETH daily falls through to the tenor offset
        self.assertEqual(ud.QUOTE_OFFSET_BY_CADENCE["daily"], 4)


class TestRunCycleBudgets(unittest.TestCase):
    """Offset/band/cap behaviors of the cycle (split out of TestRunCycle when
    TestDailyDefenses was inserted above them, 2026-08-17)."""

    def test_banner_offset_matches_the_ladder_actually_quoted(self):
        # The inherited startup banner read the module global and printed "5c"
        # while this fleet quoted 3c. The restart procedure verifies banners.
        self.assertEqual(ud.UpDownMarketMaker.quote_offset_cents, 3)
        self.assertEqual(mm.TouchMarketMaker.quote_offset_cents, 5)

    def test_offset_is_three_cents(self):
        # These books are ~2c wide; 5c parked us 5-6c behind the touch.
        self.assertEqual(ud.QUOTE_OFFSET_CENTS, 3)
        self.assertEqual(mm.QUOTE_OFFSET_CENTS, 5)   # one-touch fleet unchanged

    def test_offset_reaches_the_touch_when_our_fair_beats_the_market(self):
        """With a 2c-wide book, 3c off fair joins the bid once our fair is 3c
        above the market's bid — 5c never would have. Join-don't-lead still
        forbids improving on it."""
        # book 52/54; fair 55 -> bid anchor min(55-3, 52) = 52 == the touch
        q3 = mm.build_quotes("T", 55, 52, 54, 99, 99, 3, 1, 3, 2)
        q5 = mm.build_quotes("T", 55, 52, 54, 99, 99, 3, 1, 5, 2)
        self.assertEqual(max(q.price_cents for q in q3 if q.book_side == "bid"), 52)
        self.assertEqual(max(q.price_cents for q in q5 if q.book_side == "bid"), 50)
        # never better than the external best, at either offset
        for q in q3 + q5:
            if q.book_side == "bid":
                self.assertLessEqual(q.price_cents, 52)
            else:
                self.assertGreaterEqual(q.price_cents, 54)

    def test_band_excludes_strikes_outside_ten_to_ninety(self):
        wide = [mkt(100000 + d, EV_H, NOW - timedelta(hours=1), NOW + timedelta(hours=2),
                    ticker=f"{EV_H}-T{d}")
                for d in range(-4000, 4001, 100)]
        b = bot()
        view = ud.build_event_views(wide, NOW)[0]
        with mock.patch.object(mm, "fetch_ohlc",
                               side_effect=lambda cfg, iv: candles(400, iv, 0.01)):
            band = b.quotable_markets(view, 100000.0, NOW, 0.0)
        self.assertTrue(band)
        self.assertLessEqual(len(band), 8)
        for _m, _t, _k, fair in band:
            self.assertGreater(fair, 10)
            self.assertLess(fair, 90)

    def test_orderbook_calls_are_bounded_by_the_band(self):
        many = [mkt(100000 + i * 20 - 2000, EV_H, NOW - timedelta(hours=1),
                    NOW + timedelta(hours=2), ticker=f"{EV_H}-T{i}")
                for i in range(200)]
        c = FakeClient(markets=many)
        b = bot(c, cadences=("hourly",))
        with priced():
            b.run_cycle()
        self.assertLessEqual(len(c.orderbook_calls), ud.MAX_MARKETS_PER_EVENT)

    def test_position_cap_blocks_further_buying(self):
        ms = hourly_event(n=1)
        c = FakeClient(markets=ms,
                       positions={EV_H: {ms[0]["ticker"]: ud.MAX_POSITION_CONTRACTS}})
        b = bot(c, cadences=("hourly",))
        with priced():
            b.run_cycle()
        placed = list(b.state.sim_orders.values())
        self.assertFalse([o for o in placed if o["book_side"] == "bid"])
        self.assertTrue([o for o in placed if o["book_side"] == "ask"])

    def test_asset_cap_spans_events(self):
        ms = hourly_event(n=1) + daily_event(n=1)
        c = FakeClient(markets=ms, positions={
            EV_H: {ms[0]["ticker"]: ud.MAX_ASSET_CONTRACTS}})
        b = bot(c, cadences=("hourly", "daily"))
        with priced():
            b.run_cycle()
        # asset-wide net is already at the cap: nothing may be bought anywhere,
        # including on the OTHER event.
        self.assertFalse([o for o in b.state.sim_orders.values()
                          if o["book_side"] == "bid"])

    def test_no_tenor_is_starved_by_the_asset_budget(self):
        # Regression: draining the asset budget greedily in expiry order let
        # hourly+daily consume all of it, so the weekly was priced every cycle
        # and quoted on neither side.
        c = FakeClient(markets=hourly_event(n=8) + daily_event(n=8) + weekly_event(n=8))
        b = bot(c, cadences=ud.CADENCES)
        with priced():
            b.run_cycle()
        for ev in (EV_H, EV_D, EV_W):
            sides = {o["book_side"] for o in b.state.sim_orders.values()
                     if o["ticker"].startswith(ev)}
            self.assertEqual(sides, {"bid", "ask"}, f"{ev} quoted only {sides}")

    def test_asset_budget_still_respected_across_events(self):
        c = FakeClient(markets=hourly_event(n=8) + daily_event(n=8) + weekly_event(n=8))
        b = bot(c, cadences=ud.CADENCES)
        with priced():
            b.run_cycle()
        for side in ("bid", "ask"):
            total = sum(o["remaining_count"] for o in b.state.sim_orders.values()
                        if o["book_side"] == side)
            self.assertLessEqual(total, ud.MAX_ASSET_CONTRACTS + 0.01)

    def test_model_market_divergence_stands_down(self):
        c = FakeClient(markets=hourly_event(n=1),
                       orderbook={"orderbook": {"yes": [[2, 50]], "no": [[95, 50]]}})
        b = bot(c, cadences=("hourly",))
        with priced():
            b.run_cycle()
        self.assertEqual(b.state.sim_orders, {})

    def test_positions_failure_raises_for_the_failsafe(self):
        class NoPos(FakeClient):
            def get_positions(self, **kw):
                raise RuntimeError("portfolio down")
        b = bot(NoPos(markets=hourly_event()), cadences=("hourly",))
        with priced():
            with self.assertRaises(mm.DataError):
                b.run_cycle()


# ---------------------------------------------------------------------------
# Multi-event order plumbing
# ---------------------------------------------------------------------------

def resting_order(oid, ticker, price=40, count=10, prefix="cud"):
    return {"order_id": oid, "ticker": ticker, "status": "resting",
            "client_order_id": f"{prefix}-run-{oid}", "book_side": "bid",
            "yes_price": price, "remaining_count": count}


class TestMultiEventOrders(unittest.TestCase):
    def test_resting_orders_unioned_across_events(self):
        c = FakeClient()
        c.orders_by_event = {EV_H: [resting_order("h1", f"{EV_H}-T1")],
                             EV_D: [resting_order("d1", f"{EV_D}-T1")]}
        b = bot(c, live=True)
        b.u.events = ud.build_event_views(hourly_event() + daily_event(), NOW)
        got = b.fetch_resting_orders(1000.0)
        self.assertEqual(sorted(o["order_id"] for o in got), ["d1", "h1"])

    def test_ledger_merged_once_not_per_event(self):
        # _merge_ledger walks the WHOLE ledger; running it per event would add
        # the same in-flight order once per event and double-count our size.
        c = FakeClient()
        b = bot(c, live=True)
        b.u.events = ud.build_event_views(hourly_event() + daily_event(), NOW)
        b.state.ledger["inflight"] = {
            "order_id": "inflight", "ticker": f"{EV_H}-T1", "book_side": "bid",
            "yes_price": 40, "remaining_count": 10.0, "status": "resting",
            "client_order_id": "cud-run-inflight",
            "_placed_at": 1000.0, "_confirmed": False}
        got = b.fetch_resting_orders(1000.0 + 1)
        self.assertEqual([o["order_id"] for o in got].count("inflight"), 1)

    def test_only_our_prefix_survives(self):
        c = FakeClient()
        c.orders_by_event = {EV_H: [
            resting_order("ours", f"{EV_H}-T1"),
            resting_order("touch", f"{EV_H}-T1", prefix="cmm"),
            resting_order("weekly", f"{EV_H}-T1", prefix="cmw"),
        ]}
        b = bot(c, live=True)
        b.u.events = ud.build_event_views(hourly_event(), NOW)
        self.assertEqual([o["order_id"] for o in b.fetch_resting_orders(1000.0)],
                         ["ours"])

    def test_cancel_all_covers_open_and_recent_events(self):
        c = FakeClient(event_list=[EV_H, EV_D, "KXBTCD-26AUG0513"])
        c.orders_by_event = {EV_H: [resting_order("h1", f"{EV_H}-T1")],
                             "KXBTCD-26AUG0513": [resting_order("old", "x")]}
        b = bot(c, live=True)
        b.u.events = ud.build_event_views(hourly_event(), NOW)
        n = b.cancel_all_bot_orders(include_prev_month=True)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(c.cancelled), ["h1", "old"])

    def test_narrow_cancel_skips_the_event_listing(self):
        c = FakeClient(event_list=[EV_D])
        b = bot(c, live=True)
        b.u.events = ud.build_event_views(hourly_event(), NOW)
        b.cancel_all_bot_orders(include_prev_month=False)
        self.assertEqual(c.get_events_calls, [])

    def test_dry_run_never_reaches_the_exchange(self):
        c = FakeClient()
        b = bot(c, live=False)
        b.state.sim_orders = {"s1": {"order_id": "s1"}}
        self.assertEqual(b.cancel_all_bot_orders(include_prev_month=True), 1)
        self.assertEqual(c.get_orders_calls, [])

    def test_startup_sweeps_wide(self):
        b = bot(FakeClient(), live=True)
        with mock.patch.object(b, "cancel_all_bot_orders", return_value=0) as cancel:
            b.startup_event_and_sweep()
        cancel.assert_called_once_with(include_prev_month=True)
        self.assertEqual(b.state.event_ticker, "")


# ---------------------------------------------------------------------------
# Fleet isolation — now three fleets share this repo and this interpreter
# ---------------------------------------------------------------------------

class TestFleetIsolation(unittest.TestCase):
    def test_monthly_module_untouched(self):
        # Relationships, not the monthly's absolute numbers — Jack retunes
        # those in other sessions (5x10/267/1667 -> 5x12/320/2000 on 8/5).
        self.assertEqual(mm.CLIENT_ORDER_PREFIX, "cmm")
        self.assertTrue(mm.MODEL_VERSION.startswith("crypto_touch_mm_v"))
        self.assertNotEqual(mm.MODEL_VERSION, ud.MODEL_VERSION)
        self.assertNotEqual((mm.MAX_POSITION_CONTRACTS, mm.MAX_EVENT_CONTRACTS),
                            (ud.UpDownMarketMaker.max_position,
                             ud.UpDownMarketMaker.max_event))

    def test_all_three_prefixes_are_pairwise_disjoint(self):
        import crypto_touch_mm_weekly as wk
        prefixes = [mm.TouchMarketMaker.client_order_prefix,
                    wk.WeeklyTouchMarketMaker.client_order_prefix,
                    ud.UpDownMarketMaker.client_order_prefix]
        self.assertEqual(len(set(prefixes)), 3)
        for a in prefixes:
            for b_ in prefixes:
                if a != b_:
                    self.assertFalse(f"{a}-x".startswith(f"{b_}-"))

    def test_status_dirs_are_all_distinct(self):
        import crypto_touch_mm_weekly as wk
        dirs = {os.path.normcase(mm.TouchMarketMaker.status_dir),
                os.path.normcase(wk.WeeklyTouchMarketMaker.status_dir),
                os.path.normcase(ud.UpDownMarketMaker.status_dir)}
        self.assertEqual(len(dirs), 3)

    def test_each_fleet_has_its_own_ladder_shape(self):
        import crypto_touch_mm_weekly as wk
        # The updown pilot's 3x1 IS pinned — that is this fleet's live risk
        # control. The one-touch fleets track whatever the monthly runs.
        self.assertEqual((ud.UpDownMarketMaker.num_levels,
                          ud.UpDownMarketMaker.contracts_per_level), (3, 2))
        for other in (mm.TouchMarketMaker, wk.WeeklyTouchMarketMaker):
            self.assertEqual((other.num_levels, other.contracts_per_level),
                             (mm.NUM_LEVELS, mm.CONTRACTS_PER_LEVEL))
        self.assertNotEqual((mm.NUM_LEVELS, mm.CONTRACTS_PER_LEVEL), (3, 2))

    def test_updown_caps_are_its_own(self):
        C = ud.UpDownMarketMaker
        self.assertEqual((C.max_position, C.max_event), (80.0, 400.0))
        self.assertNotEqual(C.max_position, mm.TouchMarketMaker.max_position)

    def test_data_cache_still_shared(self):
        self.assertTrue(mm._cache_path("price", "XBTUSD").startswith(mm.STATUS_DIR))

    def test_asset_table_matches_the_monthly_data_sources(self):
        for asset, cfg in ud.ASSETS.items():
            src = mm.MARKETS[f"{asset}-MAX"]
            self.assertEqual((cfg.kraken_pair, cfg.coinbase_product),
                             (src.kraken_pair, src.coinbase_product))
            self.assertEqual(cfg.series, f"KX{asset}D")


class TestStatus(unittest.TestCase):
    def test_heartbeat_has_the_event_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = bot(FakeClient(markets=hourly_event() + daily_event()),
                    cadences=("hourly", "daily"))
            b.status_dir = tmp
            with priced():
                b.run_cycle()
                b.write_status(NOW)
            with open(os.path.join(tmp, "status_BTC.json"), encoding="utf-8") as f:
                st = json.load(f)
        self.assertEqual(st["fleet"], "updown")
        self.assertEqual(st["series"], "KXBTCD")
        self.assertEqual(st["cadences"], "hourly,daily")
        self.assertEqual({e["cadence"] for e in st["events"]}, {"hourly", "daily"})


class TestDiscoverReport(unittest.TestCase):
    def test_lists_tenors(self):
        out = ud.discover_report(
            FakeClient(markets=hourly_event() + daily_event() + weekly_event()), NOW)
        for token in (EV_H, EV_D, EV_W, "hourly", "daily", "weekly"):
            self.assertIn(token, out)

    def test_handles_empty_and_errors(self):
        self.assertIn("no open events", ud.discover_report(FakeClient(markets=[]), NOW))

        class Flaky(FakeClient):
            def get_markets(self, **kw):
                raise RuntimeError("boom")
        self.assertIn("ERROR", ud.discover_report(Flaky(), NOW))


if __name__ == "__main__":
    unittest.main()
