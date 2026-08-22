#!/usr/bin/env python3
"""Unit tests for crypto_touch_mm_weekly.py (no network, no real orders).

Run:  python -m unittest test_crypto_touch_mm_weekly -v
Both suites together (the isolation tests below are the point of doing so):
      python -m unittest test_crypto_touch_mm test_crypto_touch_mm_weekly
"""

import contextlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import crypto_touch_mm as mm
import crypto_touch_mm_weekly as wk


def _frozen_datetime(fixed):
    """datetime stand-in whose now() is fixed (other constructors pass through)."""
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    return _DT


@contextlib.contextmanager
def frozen(fixed):
    """Freeze the clock in BOTH modules. run_cycle() reads the time twice — once
    in the weekly module for discovery, once in the monthly one for pricing —
    and leaving either on the wall clock makes these tests expire."""
    stub = _frozen_datetime(fixed)
    with mock.patch.object(wk, "datetime", stub), mock.patch.object(mm, "datetime", stub):
        yield

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
BTC_MAX = wk.MARKETS["BTC-MAX"]
EV = "KXBTCMAXW-26AUG09"
EV_NEXT = "KXBTCMAXW-26AUG16"


def mkt(ticker, strike, event=EV, open_time="2026-08-03T04:00:00Z",
        end="2026-08-10T03:59:59Z", status="active", **extra):
    m = {"ticker": ticker, "event_ticker": event, "status": status,
         "strike_type": "greater", "floor_strike": strike,
         "open_time": open_time, "close_time": end,
         "expected_expiration_time": end}
    m.update(extra)
    return m


class FakeClient:
    """ExchangeClient stand-in covering only what the weekly bot calls."""

    def __init__(self, market_pages=None, event_list=None, positions=None,
                 orderbook=None):
        self.market_pages = market_pages if market_pages is not None else [
            {"markets": [], "cursor": None}]
        self.event_list = event_list or []
        self.positions = positions or {}
        self.orderbook = orderbook or {"orderbook": {"yes": [[10, 50]], "no": [[75, 50]]}}
        self.get_markets_calls = []
        self.get_events_calls = []
        self.get_orders_calls = []
        self.cancelled = []
        self.orders_by_event = {}

    def get_markets(self, **kw):
        self.get_markets_calls.append(kw)
        idx = min(len(self.get_markets_calls) - 1, len(self.market_pages) - 1)
        return self.market_pages[idx]

    def get_events(self, **kw):
        self.get_events_calls.append(kw)
        return {"events": [{"event_ticker": t} for t in self.event_list]}

    def get_orders(self, **kw):
        self.get_orders_calls.append(kw)
        return {"orders": self.orders_by_event.get(kw.get("event_ticker"), []),
                "cursor": None}

    def get_order(self, order_id):
        return {"order": {"status": "canceled"}}

    def cancel_order(self, order_id, exchange_index=None, market_ticker=None):
        self.cancelled.append(order_id)
        return {}

    def get_positions(self, **kw):
        return {"market_positions": [{"ticker": t, "position": p}
                                     for t, p in self.positions.items()]}

    def get_orderbook(self, ticker, **kw):
        return self.orderbook

    def __getattr__(self, name):
        raise AssertionError(f"unexpected client call: {name}")


def bot(client=None, live=False, **kw):
    return wk.WeeklyTouchMarketMaker(BTC_MAX, client or FakeClient(), live=live, **kw)


# ---------------------------------------------------------------------------
# Fleet isolation — the weekly bot must not be able to retune the live monthly
# fleet, and vice versa. This is the whole reason the settings are class
# attributes rather than module globals.
# ---------------------------------------------------------------------------

class TestFleetIsolation(unittest.TestCase):
    # NB: these assert RELATIONSHIPS, not the monthly fleet's absolute numbers.
    # Jack retunes the monthly ladder/caps in other sessions (v2.1 5x10 267/1667
    # -> v2.5 5x12 320/2000 on 8/5); hardcoding those values here broke 5 tests
    # while proving nothing. The property that matters is that importing a
    # variant fleet cannot CHANGE the monthly module, i.e. the monthly's values
    # never become the variant's.

    def test_importing_weekly_leaves_monthly_module_untouched(self):
        self.assertEqual(mm.CLIENT_ORDER_PREFIX, "cmm")
        self.assertTrue(mm.MODEL_VERSION.startswith("crypto_touch_mm_v"))
        self.assertNotEqual(mm.MODEL_VERSION, wk.MODEL_VERSION)
        # the weekly's env-shim leak would overwrite these with its 100/500;
        # since the monthly's 8/22 return to 3x5 its event cap is 500 by its
        # OWN config (coincidentally the weekly's number), so pin the monthly
        # to its configured defaults instead of asserting inequality
        self.assertEqual(mm.MAX_POSITION_CONTRACTS, 80.0)
        self.assertEqual(mm.MAX_EVENT_CONTRACTS, 500.0)
        self.assertNotEqual(mm.MAX_POSITION_CONTRACTS,
                            wk.WeeklyTouchMarketMaker.max_position)

    def test_monthly_class_defaults_intact(self):
        # The monthly class mirrors its module globals — a variant overriding
        # its own class attrs must never redirect the monthly's.
        m = mm.TouchMarketMaker
        self.assertEqual(m.client_order_prefix, "cmm")
        self.assertEqual((m.max_position, m.max_event),
                         (mm.MAX_POSITION_CONTRACTS, mm.MAX_EVENT_CONTRACTS))
        self.assertEqual(m.window_label, "MTD")
        self.assertEqual(m.min_hours_left, mm.MIN_HOURS_LEFT)

    def test_weekly_overrides_differ(self):
        w = wk.WeeklyTouchMarketMaker
        self.assertEqual(w.client_order_prefix, "cmw")
        self.assertEqual((w.max_position, w.max_event), (100.0, 500.0))
        self.assertEqual(w.window_label, "WTD")
        self.assertEqual(w.min_hours_left, 2.0)
        self.assertEqual(w.model_version, "crypto_touch_mm_weekly_v1.0")

    def test_order_prefixes_are_disjoint(self):
        # Each fleet's orphan sweep filters on "<prefix>-"; neither may match
        # the other's ids, or a restart could cancel the other fleet's book.
        a, b = mm.TouchMarketMaker.client_order_prefix, wk.WeeklyTouchMarketMaker.client_order_prefix
        self.assertNotEqual(a, b)
        self.assertFalse(f"{a}-x".startswith(f"{b}-"))
        self.assertFalse(f"{b}-x".startswith(f"{a}-"))

    def test_status_dirs_are_separate(self):
        # send_daily_digest.py globs status_*.json out of the monthly dir and
        # prices each bot with a MONTHLY event ticker; weekly heartbeats
        # landing there would corrupt the morning digest.
        self.assertNotEqual(os.path.normcase(wk.WeeklyTouchMarketMaker.status_dir),
                            os.path.normcase(mm.TouchMarketMaker.status_dir))

    def test_data_cache_stays_shared_with_the_monthly_fleet(self):
        # Kraken rate-limits per IP; MAX/MIN pairs must read identical candles.
        self.assertTrue(mm._cache_path("price", "XBTUSD").startswith(mm.STATUS_DIR))
        self.assertFalse(mm._cache_path("price", "XBTUSD").startswith(wk.STATUS_DIR))

    def test_ladder_spec_matches_the_monthly_by_default(self):
        # The weekly deliberately inherits whatever ladder the monthly runs.
        w = wk.WeeklyTouchMarketMaker
        self.assertEqual((w.num_levels, w.contracts_per_level),
                         (mm.NUM_LEVELS, mm.CONTRACTS_PER_LEVEL))
        self.assertEqual((w.quote_offset_cents, w.level_spacing_cents),
                         (mm.QUOTE_OFFSET_CENTS, mm.LEVEL_SPACING_CENTS))


class TestMarketTable(unittest.TestCase):
    def test_series_are_weekly(self):
        self.assertEqual(wk.MARKETS["BTC-MAX"].series, "KXBTCMAXW")
        self.assertEqual(wk.MARKETS["BTC-MIN"].series, "KXBTCMINW")
        self.assertEqual(wk.MARKETS["DOGE-MAX"].series, "KXDOGEMAXW")

    def test_same_keys_and_data_sources_as_monthly(self):
        self.assertEqual(sorted(wk.MARKETS), sorted(mm.MARKETS))
        for key, cfg in wk.MARKETS.items():
            src = mm.MARKETS[key]
            self.assertEqual((cfg.asset, cfg.direction, cfg.kraken_pair, cfg.coinbase_product),
                             (src.asset, src.direction, src.kraken_pair, src.coinbase_product))

    def test_no_monthly_series_leaked_in(self):
        for cfg in wk.MARKETS.values():
            self.assertTrue(cfg.series.endswith("W"), cfg.series)
            self.assertNotIn("MON", cfg.series)


# ---------------------------------------------------------------------------
# Pure discovery helpers
# ---------------------------------------------------------------------------

class TestMarketTimes(unittest.TestCase):
    def test_end_prefers_expected_expiration(self):
        m = mkt("t", 1, end="2026-08-10T03:59:59Z")
        m["close_time"] = "2026-08-11T00:00:00Z"
        self.assertEqual(wk.market_end_utc(m),
                         datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc))

    def test_end_falls_back_to_close_time(self):
        m = mkt("t", 1)
        del m["expected_expiration_time"]
        self.assertEqual(wk.market_end_utc(m),
                         datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc))

    def test_end_none_when_unparseable(self):
        m = mkt("t", 1, end="not-a-date")
        m["close_time"] = "also-bad"
        self.assertIsNone(wk.market_end_utc(m))

    def test_open_time(self):
        self.assertEqual(wk.market_open_utc(mkt("t", 1)),
                         datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc))
        self.assertIsNone(wk.market_open_utc({"open_time": ""}))


class TestPickFrontEvent(unittest.TestCase):
    def test_picks_the_nearer_week(self):
        ms = [mkt("a", 1, event=EV_NEXT, end="2026-08-17T03:59:59Z"),
              mkt("b", 2, event=EV, end="2026-08-10T03:59:59Z")]
        self.assertEqual(wk.pick_front_event(ms, NOW), EV)

    def test_skips_events_already_ended(self):
        ms = [mkt("a", 1, event="KXBTCMAXW-26AUG02", end="2026-08-03T03:59:59Z"),
              mkt("b", 2, event=EV, end="2026-08-10T03:59:59Z")]
        self.assertEqual(wk.pick_front_event(ms, NOW), EV)

    def test_none_when_all_past_or_empty(self):
        self.assertIsNone(wk.pick_front_event([], NOW))
        past = [mkt("a", 1, event="old", end="2026-08-01T00:00:00Z")]
        self.assertIsNone(wk.pick_front_event(past, NOW))

    def test_event_end_is_the_latest_of_its_markets(self):
        # An early-settling strike must not drag the event's end backwards.
        ms = [mkt("a", 1, event=EV, end="2026-08-06T00:00:00Z"),
              mkt("b", 2, event=EV, end="2026-08-10T03:59:59Z"),
              mkt("c", 3, event=EV_NEXT, end="2026-08-09T00:00:00Z")]
        self.assertEqual(wk.pick_front_event(ms, NOW), EV_NEXT)

    def test_markets_without_times_are_ignored(self):
        bad = mkt("a", 1, event="nodates", end="nope")
        bad["close_time"] = "nope"
        ms = [bad, mkt("b", 2, event=EV)]
        self.assertEqual(wk.pick_front_event(ms, NOW), EV)

    def test_tie_breaks_deterministically(self):
        ms = [mkt("a", 1, event="ZZZ"), mkt("b", 2, event="AAA")]
        self.assertEqual(wk.pick_front_event(ms, NOW), "AAA")


class TestEventWindow(unittest.TestCase):
    def test_uses_market_open_and_end(self):
        start, end = wk.event_window([mkt("a", 1), mkt("b", 2)], NOW)
        self.assertEqual(start, datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc))

    def test_stub_week_is_honoured_not_normalised_to_seven_days(self):
        # A 3-day listing must measure 3 days: widening it would compute the
        # touch guard over prices from before the window and stand us down on
        # perfectly good strikes.
        ms = [mkt("a", 1, open_time="2026-08-07T04:00:00Z")]
        start, end = wk.event_window(ms, NOW)
        self.assertEqual(start, datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc))
        self.assertLess((end - start).days, 4)

    def test_missing_open_time_falls_back_to_seven_days(self):
        m = mkt("a", 1)
        del m["open_time"]
        start, end = wk.event_window([m], NOW)
        self.assertEqual(end - start, timedelta(days=7))

    def test_nonsense_metadata_falls_back(self):
        # open after close: trust the fallback, not the metadata.
        ms = [mkt("a", 1, open_time="2027-01-01T00:00:00Z")]
        start, end = wk.event_window(ms, NOW)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertLess(start, end)

    def test_empty_markets_yield_a_forward_week(self):
        start, end = wk.event_window([], NOW)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertGreater(end, NOW)


# ---------------------------------------------------------------------------
# Horizon hooks
# ---------------------------------------------------------------------------

class TestSkipReasonForFair(unittest.TestCase):
    def test_inherits_the_near_touch_guard(self):
        self.assertIn("near touch", bot().skip_reason_for_fair(97))
        self.assertIn("near touch", bot().skip_reason_for_fair(99))

    def test_adds_the_deep_otm_guard(self):
        reason = bot().skip_reason_for_fair(3)
        self.assertIsNotNone(reason)
        self.assertIn("deep OTM", reason)
        self.assertIsNotNone(bot().skip_reason_for_fair(1))

    def test_quotes_the_middle(self):
        for fair in (4, 15, 50, 80, 96):
            self.assertIsNone(bot().skip_reason_for_fair(fair), fair)

    def test_monthly_has_no_low_side_guard(self):
        monthly = mm.TouchMarketMaker(mm.MARKETS["BTC-MAX"], FakeClient(), live=False)
        self.assertIsNone(monthly.skip_reason_for_fair(2))

    def test_guard_can_be_disabled(self):
        with mock.patch.object(wk, "SKIP_FAIR_BELOW_CENTS", 0):
            self.assertIsNone(bot().skip_reason_for_fair(1))


class TestWindowHooks(unittest.TestCase):
    def test_hooks_report_the_discovered_window(self):
        b = bot()
        b.w.window_start = datetime(2026, 8, 3, tzinfo=timezone.utc)
        b.w.window_end = datetime(2026, 8, 10, tzinfo=timezone.utc)
        self.assertEqual(b.window_start_utc(NOW), b.w.window_start)
        self.assertEqual(b.window_end_utc(NOW), b.w.window_end)

    def test_hooks_are_safe_before_discovery(self):
        b = bot()
        self.assertLess(b.window_start_utc(NOW), NOW)
        self.assertGreater(b.window_end_utc(NOW), NOW)

    def test_window_is_not_the_calendar_month(self):
        b = bot()
        b.w.window_start = datetime(2026, 8, 3, 4, tzinfo=timezone.utc)
        b.w.window_end = datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc)
        self.assertNotEqual(b.window_start_utc(NOW), mm.month_start_utc(NOW))
        self.assertNotEqual(b.window_end_utc(NOW), mm.month_end_utc(NOW))

    def test_maybe_rollover_is_inert(self):
        b = bot()
        b.state.event_ticker = EV
        b.maybe_rollover(NOW)          # monthly would overwrite with a computed ticker
        self.assertEqual(b.state.event_ticker, EV)


class TestPollInterval(unittest.TestCase):
    def test_active_uses_the_normal_cadence(self):
        b = bot()
        b.w.idle_reason = ""
        self.assertEqual(b.poll_interval(), wk.WeeklyTouchMarketMaker.poll_secs)

    def test_idle_backs_right_off(self):
        b = bot()
        b.w.idle_reason = "no open weekly event"
        self.assertEqual(b.poll_interval(), wk.IDLE_POLL_SECS)
        self.assertGreater(b.poll_interval(), b.poll_secs)


# ---------------------------------------------------------------------------
# Discovery against a fake exchange
# ---------------------------------------------------------------------------

class TestDiscovery(unittest.TestCase):
    def test_finds_front_event_and_window(self):
        c = FakeClient([{"markets": [mkt("m1", 110000), mkt("m2", 120000)], "cursor": None}])
        b = bot(c)
        self.assertTrue(b.discover(NOW))
        self.assertEqual(b.state.event_ticker, EV)
        self.assertEqual(len(b.w.event_markets), 2)
        self.assertEqual(b.w.window_end, datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(b.w.idle_reason, "")
        self.assertEqual(c.get_markets_calls[0]["series_ticker"], "KXBTCMAXW")

    def test_only_the_front_events_markets_are_kept(self):
        c = FakeClient([{"markets": [mkt("this", 1),
                                     mkt("next", 2, event=EV_NEXT,
                                         end="2026-08-17T03:59:59Z")],
                         "cursor": None}])
        b = bot(c)
        b.discover(NOW)
        self.assertEqual([m["ticker"] for m in b.active_markets()], ["this"])

    def test_unlisted_series_is_idle_not_an_error(self):
        b = bot(FakeClient([{"markets": [], "cursor": None}]))
        self.assertFalse(b.discover(NOW))
        self.assertIn("no open weekly event", b.w.idle_reason)
        self.assertEqual(b.state.event_ticker, "")
        self.assertEqual(b.active_markets(), [])

    def test_settled_only_markets_are_idle(self):
        c = FakeClient([{"markets": [mkt("m", 1, status="settled")], "cursor": None}])
        b = bot(c)
        self.assertFalse(b.discover(NOW))

    def test_pagination(self):
        c = FakeClient([{"markets": [mkt("m1", 1)], "cursor": "next"},
                        {"markets": [mkt("m2", 2)], "cursor": None}])
        b = bot(c)
        b.discover(NOW)
        self.assertEqual(len(b.w.series_markets), 2)
        self.assertEqual(c.get_markets_calls[1]["cursor"], "next")

    def test_market_list_is_refetched_every_cycle(self):
        # One-touch strikes settle the INSTANT they are touched, so a cached
        # market list would leave us quoting an already-resolved market.
        c = FakeClient([{"markets": [mkt("m1", 1)], "cursor": None}])
        b = bot(c)
        b.discover(NOW)
        b.discover(NOW)
        b.discover(NOW)
        self.assertEqual(len(c.get_markets_calls), 3)

    def test_settled_strike_drops_out_next_cycle(self):
        c = FakeClient([
            {"markets": [mkt("live", 1), mkt("touched", 2)], "cursor": None},
            {"markets": [mkt("live", 1),
                         mkt("touched", 2, status="settled")], "cursor": None},
        ])
        b = bot(c)
        b.discover(NOW)
        self.assertEqual(len(b.active_markets()), 2)
        b.discover(NOW)
        self.assertEqual([m["ticker"] for m in b.active_markets()], ["live"])

    def test_fetch_failure_propagates_to_the_failsafe(self):
        class Boom(FakeClient):
            def get_markets(self, **kw):
                raise RuntimeError("network down")
        b = bot(Boom())
        # Must NOT be swallowed as "idle": a blind cycle has to count as an error.
        with self.assertRaises(RuntimeError):
            b.discover(NOW)

    def test_pinned_event_is_used(self):
        c = FakeClient([{"markets": [mkt("this", 1),
                                     mkt("next", 2, event=EV_NEXT,
                                         end="2026-08-17T03:59:59Z")],
                         "cursor": None}])
        b = bot(c, pinned_event=EV_NEXT)
        self.assertTrue(b.discover(NOW))
        self.assertEqual(b.state.event_ticker, EV_NEXT)

    def test_pinned_event_absent_goes_idle(self):
        c = FakeClient([{"markets": [mkt("this", 1)], "cursor": None}])
        b = bot(c, pinned_event="KXBTCMAXW-NOPE")
        self.assertFalse(b.discover(NOW))
        self.assertIn("pinned event", b.w.idle_reason)

    def test_event_disappearing_cancels_our_book(self):
        c = FakeClient([{"markets": [mkt("m1", 1)], "cursor": None},
                        {"markets": [], "cursor": None}])
        b = bot(c)
        b.discover(NOW)
        self.assertEqual(b.state.event_ticker, EV)
        with mock.patch.object(b, "cancel_all_bot_orders", return_value=0) as cancel:
            self.assertFalse(b.discover(NOW))
        cancel.assert_called_once()
        self.assertEqual(b.state.event_ticker, "")


class TestRollover(unittest.TestCase):
    def test_roll_cancels_old_and_resets_window_state(self):
        b = bot()
        b.state.event_ticker = EV
        b.state.session_extreme = 123.0
        b.state.mtd_extreme = 456.0
        b.state.blind_streak["x"] = 2
        with mock.patch.object(b, "cancel_all_bot_orders", return_value=3) as cancel:
            b.roll_to(EV_NEXT)
        cancel.assert_called_once()
        self.assertEqual(b.state.event_ticker, EV_NEXT)
        self.assertIsNone(b.state.session_extreme)
        self.assertIsNone(b.state.mtd_extreme)     # WTD extreme is window-scoped
        self.assertEqual(b.state.blind_streak, {})

    def test_cancel_happens_before_the_ticker_changes(self):
        # The sweep is scoped to state.event_ticker; switching first would
        # strand the old week's orders.
        b = bot()
        b.state.event_ticker = EV
        seen = []
        with mock.patch.object(b, "cancel_all_bot_orders",
                               side_effect=lambda *a, **k: seen.append(b.state.event_ticker)):
            b.roll_to(EV_NEXT)
        self.assertEqual(seen, [EV])

    def test_first_event_does_not_cancel(self):
        b = bot()
        with mock.patch.object(b, "cancel_all_bot_orders") as cancel:
            b.roll_to(EV)
        cancel.assert_not_called()
        self.assertEqual(b.state.event_ticker, EV)

    def test_same_event_is_a_noop(self):
        b = bot()
        b.state.event_ticker = EV
        b.state.session_extreme = 5.0
        with mock.patch.object(b, "cancel_all_bot_orders") as cancel:
            b.roll_to(EV)
        cancel.assert_not_called()
        self.assertEqual(b.state.session_extreme, 5.0)


class TestSweeps(unittest.TestCase):
    def test_cancel_all_sweeps_current_plus_recent_events(self):
        c = FakeClient(event_list=[EV_NEXT, EV, "KXBTCMAXW-26AUG02"])
        c.orders_by_event = {
            EV: [{"order_id": "o1", "status": "resting",
                  "client_order_id": "cmw-run-1", "ticker": "m1",
                  "book_side": "bid", "yes_price": 10, "remaining_count": 5}],
            "KXBTCMAXW-26AUG02": [{"order_id": "o2", "status": "resting",
                                   "client_order_id": "cmw-run-2", "ticker": "m0",
                                   "book_side": "bid", "yes_price": 9,
                                   "remaining_count": 5}],
        }
        b = bot(c, live=True)
        b.state.event_ticker = EV
        n = b.cancel_all_bot_orders(include_prev_month=True)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(c.cancelled), ["o1", "o2"])
        swept = [call.get("event_ticker") for call in c.get_orders_calls]
        self.assertIn(EV, swept)
        self.assertIn("KXBTCMAXW-26AUG02", swept)

    def test_narrow_sweep_touches_only_the_current_event(self):
        c = FakeClient(event_list=[EV, "KXBTCMAXW-26AUG02"])
        b = bot(c, live=True)
        b.state.event_ticker = EV
        b.cancel_all_bot_orders(include_prev_month=False)
        self.assertEqual([call.get("event_ticker") for call in c.get_orders_calls], [EV])
        self.assertEqual(c.get_events_calls, [])

    def test_only_our_prefix_is_cancelled(self):
        c = FakeClient(event_list=[EV])
        c.orders_by_event = {EV: [
            {"order_id": "ours", "status": "resting", "client_order_id": "cmw-a",
             "ticker": "m", "book_side": "bid", "yes_price": 10, "remaining_count": 1},
            {"order_id": "monthly", "status": "resting", "client_order_id": "cmm-a",
             "ticker": "m", "book_side": "bid", "yes_price": 10, "remaining_count": 1},
            {"order_id": "manual", "status": "resting", "client_order_id": "hand-1",
             "ticker": "m", "book_side": "bid", "yes_price": 10, "remaining_count": 1},
        ]}
        b = bot(c, live=True)
        b.state.event_ticker = EV
        b.cancel_all_bot_orders()
        self.assertEqual(c.cancelled, ["ours"])

    def test_event_listing_failure_is_survivable(self):
        class NoEvents(FakeClient):
            def get_events(self, **kw):
                raise RuntimeError("events endpoint down")
        c = NoEvents()
        b = bot(c, live=True)
        b.state.event_ticker = EV
        self.assertEqual(b.cancel_all_bot_orders(include_prev_month=True), 0)

    def test_dry_run_never_reaches_the_exchange(self):
        c = FakeClient()
        b = bot(c, live=False)
        b.state.sim_orders = {"s1": {"order_id": "s1"}}
        self.assertEqual(b.cancel_all_bot_orders(include_prev_month=True), 1)
        self.assertEqual(c.get_orders_calls, [])
        self.assertEqual(c.cancelled, [])

    def test_startup_clears_ticker_and_sweeps_wide(self):
        c = FakeClient(event_list=[EV])
        b = bot(c, live=True)
        b.state.event_ticker = "stale"
        with mock.patch.object(b, "cancel_all_bot_orders", return_value=0) as cancel:
            b.startup_event_and_sweep()
        self.assertEqual(b.state.event_ticker, "")
        cancel.assert_called_once_with(include_prev_month=True)

    def test_startup_in_dry_run_places_no_calls(self):
        c = FakeClient()
        b = bot(c, live=False)
        b.startup_event_and_sweep()
        self.assertEqual(c.get_orders_calls, [])


# ---------------------------------------------------------------------------
# Cycle behaviour
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def priced(spot=100000.0, sigma=0.03):
    """Freeze the clock and stub the market-data layer for a whole cycle."""
    with frozen(NOW), \
         mock.patch.object(mm, "fetch_live_price", return_value=spot), \
         mock.patch.object(wk.WeeklyTouchMarketMaker, "refresh_vol", autospec=True,
                           side_effect=lambda self, *a: setattr(
                               self.state, "sigma_daily", sigma)), \
         mock.patch.object(mm, "fetch_ohlc", return_value=[]):
        yield


class TestRunCycle(unittest.TestCase):
    def test_idle_short_circuits_before_the_monthly_cycle(self):
        b = bot(FakeClient([{"markets": [], "cursor": None}]))
        with frozen(NOW), mock.patch.object(mm.TouchMarketMaker, "run_cycle") as parent:
            b.run_cycle()
        parent.assert_not_called()
        self.assertIn("idle:", b.state.last_markets_line)
        self.assertEqual(b.state.cycles_today, 1)

    def test_active_delegates_to_the_monthly_cycle(self):
        c = FakeClient([{"markets": [mkt("m1", 110000)], "cursor": None}])
        b = bot(c)
        with frozen(NOW), mock.patch.object(mm.TouchMarketMaker, "run_cycle") as parent:
            b.run_cycle()
        parent.assert_called_once()

    def test_full_dry_run_cycle_quotes_the_ladder(self):
        c = FakeClient([{"markets": [mkt("KXBTCMAXW-26AUG09-T110", 110000)],
                         "cursor": None}])
        b = bot(c)
        with priced():
            b.run_cycle()
        self.assertEqual(b.state.event_ticker, EV)
        placed = list(b.state.sim_orders.values())
        self.assertTrue(placed, "expected a simulated ladder")
        bids = sorted((o["yes_price"] for o in placed if o["book_side"] == "bid"),
                      reverse=True)
        asks = sorted(o["yes_price"] for o in placed if o["book_side"] == "ask")
        # join-don't-lead: never better than the external best (bid 10 / ask 25)
        self.assertLessEqual(bids[0], 10)
        self.assertGreaterEqual(asks[0], 25)
        # exactly LEVEL_SPACING_CENTS apart, both sides
        self.assertEqual([bids[i] - bids[i + 1] for i in range(len(bids) - 1)],
                         [mm.LEVEL_SPACING_CENTS] * (len(bids) - 1))
        self.assertEqual([asks[i + 1] - asks[i] for i in range(len(asks) - 1)],
                         [mm.LEVEL_SPACING_CENTS] * (len(asks) - 1))
        self.assertTrue(all(o["remaining_count"] <= mm.CONTRACTS_PER_LEVEL for o in placed))

    def test_last_hours_stand_down(self):
        end = NOW + timedelta(hours=1)      # inside min_hours_left (2h)
        c = FakeClient([{"markets": [mkt("m1", 110000,
                                         end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))],
                         "cursor": None}])
        b = bot(c)
        with priced():
            b.run_cycle()
        self.assertEqual(b.state.sim_orders, {})

    def test_weekly_stands_down_earlier_than_the_monthly(self):
        # 90 minutes left: past the weekly's 2h guard, still inside the
        # monthly's 1h one. The extra margin is the weekly bot's own.
        hours = 1.5
        self.assertLess(hours, wk.WeeklyTouchMarketMaker.min_hours_left)
        self.assertGreater(hours, mm.TouchMarketMaker.min_hours_left)

        end = NOW + timedelta(hours=hours)
        c = FakeClient([{"markets": [mkt("m1", 110000,
                                         end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))],
                         "cursor": None}])
        b = bot(c)
        with priced():
            b.run_cycle()
        self.assertEqual(b.state.sim_orders, {})

    def test_deep_otm_strike_is_not_quoted(self):
        # 100k spot, 400k strike, 3%/day vol, 5 days -> fair rounds to ~0c.
        c = FakeClient([{"markets": [mkt("far", 400000)], "cursor": None}])
        b = bot(c)
        with priced():
            b.run_cycle()
        self.assertEqual(b.state.sim_orders, {})

    def test_breached_strike_is_pulled(self):
        c = FakeClient([{"markets": [mkt("hit", 110000)], "cursor": None}])
        b = bot(c)
        with priced():
            b.run_cycle()
            self.assertTrue(b.state.sim_orders, "expected a ladder on cycle 1")
            # The week's high crosses the strike: it has almost certainly
            # touched, so cycle 2 must stand down AND pull what's resting.
            # (Set after cycle 1 — roll_to() clears the window extreme, which
            # is exactly the reset a new week needs.)
            b.state.mtd_extreme = 115000.0
            b.run_cycle()
        self.assertEqual(b.state.sim_orders, {})


class TestStatusFile(unittest.TestCase):
    def test_written_to_the_weekly_dir_with_weekly_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = bot()
            b.status_dir = tmp
            b.state.event_ticker = EV
            b.w.window_start = datetime(2026, 8, 3, 4, tzinfo=timezone.utc)
            b.w.window_end = datetime(2026, 8, 10, 3, 59, 59, tzinfo=timezone.utc)
            b.write_status(NOW)
            with open(os.path.join(tmp, "status_BTC-MAX.json"), encoding="utf-8") as f:
                st = json.load(f)
        self.assertEqual(st["fleet"], "weekly")
        self.assertEqual(st["series"], "KXBTCMAXW")
        self.assertEqual(st["event"], EV)
        self.assertEqual(st["window_end"], "2026-08-10T03:59:59Z")
        self.assertIn("pid", st)          # the restart procedure kills by PID

    def test_idle_reason_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = bot()
            b.status_dir = tmp
            b.w.idle_reason = "no open weekly event on series KXBTCMAXW"
            b.write_status(NOW)
            with open(os.path.join(tmp, "status_BTC-MAX.json"), encoding="utf-8") as f:
                st = json.load(f)
        self.assertIn("no open weekly event", st["idle_reason"])

    def test_summary_uses_the_weekly_label(self):
        b = bot()
        b.state.live_price = 100000.0
        b.state.sigma_daily = 0.03
        b.state.mtd_extreme = 105000.0
        body = b.build_daily_summary()
        self.assertIn("WTDhi", body)
        self.assertNotIn("MTD", body)


class TestDiscoverReport(unittest.TestCase):
    def test_reports_nothing_live(self):
        out = wk.discover_report(FakeClient([{"markets": [], "cursor": None}]), NOW)
        self.assertIn("none", out)
        self.assertIn("idle", out)

    def test_reports_a_live_event(self):
        out = wk.discover_report(
            FakeClient([{"markets": [mkt("m1", 1), mkt("m2", 2)], "cursor": None}]), NOW)
        self.assertIn(EV, out)
        self.assertIn("front", out)

    def test_survives_a_series_error(self):
        class Flaky(FakeClient):
            def get_markets(self, **kw):
                raise RuntimeError("boom")
        self.assertIn("ERROR", wk.discover_report(Flaky(), NOW))


if __name__ == "__main__":
    unittest.main()
