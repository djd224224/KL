#!/usr/bin/env python3
"""Unit tests for crypto_touch_mm.py (no network, no real orders).

Run:  python -m unittest test_crypto_touch_mm -v
"""

import math
import unittest
from datetime import datetime, timezone
from unittest import mock

import crypto_touch_mm as mm
from KalshiClientsBaseV2ApiKey_FIXED import HttpError


def utc(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


SOL_MAX = mm.MARKETS["SOL-MAX"]
SOL_MIN = mm.MARKETS["SOL-MIN"]


class TestEventTicker(unittest.TestCase):
    def test_july_max_and_min(self):
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 7, 2)),
                         "KXSOLMAXMON-SOL-26JUL31")
        self.assertEqual(mm.event_ticker_for(SOL_MIN, utc(2026, 7, 2)),
                         "KXSOLMINMON-SOL-26JUL31")

    def test_month_lengths_and_rollover(self):
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 8, 1, 12)),
                         "KXSOLMAXMON-SOL-26AUG31")
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 9, 15)),
                         "KXSOLMAXMON-SOL-26SEP30")
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 2, 10)),
                         "KXSOLMAXMON-SOL-26FEB28")
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2028, 2, 10)),
                         "KXSOLMAXMON-SOL-28FEB29")
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2027, 1, 1, 12)),
                         "KXSOLMAXMON-SOL-27JAN31")

    def test_et_month_boundary(self):
        # 2026-08-01 03:00 UTC is still Jul 31 11pm ET -> July event
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 8, 1, 3)),
                         "KXSOLMAXMON-SOL-26JUL31")
        self.assertEqual(mm.event_ticker_for(SOL_MAX, utc(2026, 8, 1, 4, 1)),
                         "KXSOLMAXMON-SOL-26AUG31")

    def test_prev_event_ticker(self):
        self.assertEqual(mm.prev_event_ticker_for(SOL_MAX, utc(2026, 8, 5)),
                         "KXSOLMAXMON-SOL-26JUL31")
        # January -> previous December, year decrement
        self.assertEqual(mm.prev_event_ticker_for(SOL_MIN, utc(2027, 1, 5)),
                         "KXSOLMINMON-SOL-26DEC31")

    def test_other_assets(self):
        self.assertEqual(mm.event_ticker_for(mm.MARKETS["ETH-MIN"], utc(2026, 7, 2)),
                         "KXETHMINMON-ETH-26JUL31")
        self.assertEqual(mm.event_ticker_for(mm.MARKETS["BTC-MAX"], utc(2026, 7, 2)),
                         "KXBTCMAXMON-BTC-26JUL31")

    def test_month_end_utc_is_1159pm_et(self):
        end = mm.month_end_utc(utc(2026, 7, 2))
        self.assertEqual(end, datetime(2026, 8, 1, 3, 59, 59, tzinfo=timezone.utc))


class TestStrikeExtraction(unittest.TestCase):
    def test_max_uses_floor_strike(self):
        m = {"strike_type": "greater", "floor_strike": 90, "cap_strike": None}
        self.assertEqual(mm.market_strike(m, "max"), 90.0)

    def test_min_uses_cap_strike(self):
        m = {"strike_type": "less", "floor_strike": None, "cap_strike": 70}
        self.assertEqual(mm.market_strike(m, "min"), 70.0)

    def test_direction_mismatch_returns_none(self):
        m = {"strike_type": "less", "cap_strike": 70}
        self.assertIsNone(mm.market_strike(m, "max"))
        m = {"strike_type": "greater", "floor_strike": 90}
        self.assertIsNone(mm.market_strike(m, "min"))

    def test_missing_strike_returns_none(self):
        self.assertIsNone(mm.market_strike({"strike_type": "greater"}, "max"))


class TestTouchProb(unittest.TestCase):
    def test_already_touched(self):
        self.assertEqual(mm.touch_prob(100, 90, 0.03, 10, "max"), 1.0)
        self.assertEqual(mm.touch_prob(100, 110, 0.03, 10, "min"), 1.0)
        self.assertEqual(mm.touch_prob(100, 100, 0.03, 10, "max"), 1.0)

    def test_no_time_left(self):
        self.assertEqual(mm.touch_prob(100, 110, 0.03, 0, "max"), 0.0)
        self.assertEqual(mm.touch_prob(100, 90, 0.03, -1, "min"), 0.0)

    def test_monotone_in_barrier_both_directions(self):
        up = [mm.touch_prob(81.4, b, 0.0361, 29.5, "max") for b in (85, 90, 95, 100, 110)]
        for a, b in zip(up, up[1:]):
            self.assertGreater(a, b)
        down = [mm.touch_prob(81.4, b, 0.0361, 29.5, "min") for b in (75, 70, 65, 60, 55)]
        for a, b in zip(down, down[1:]):
            self.assertGreater(a, b)

    def test_monte_carlo_reference_max(self):
        # 20k-path hourly MC: S0=81.37, sigma=3.61%/d, T=29.5d
        self.assertAlmostEqual(mm.touch_prob(81.37, 90, 0.0361, 29.5, "max"), 0.576, delta=0.02)
        self.assertAlmostEqual(mm.touch_prob(81.37, 100, 0.0361, 29.5, "max"), 0.264, delta=0.02)
        self.assertAlmostEqual(mm.touch_prob(81.37, 115, 0.0361, 29.5, "max"), 0.065, delta=0.015)

    def test_monte_carlo_reference_min(self):
        # 20k-path hourly MC: S0=81.37, sigma=3.61%/d, T=29d
        self.assertAlmostEqual(mm.touch_prob(81.37, 70, 0.0361, 29.0, "min"), 0.472, delta=0.02)
        self.assertAlmostEqual(mm.touch_prob(81.37, 60, 0.0361, 29.0, "min"), 0.136, delta=0.02)
        self.assertAlmostEqual(mm.touch_prob(81.37, 55, 0.0361, 29.0, "min"), 0.053, delta=0.015)

    def test_bounds_and_clamps(self):
        for d, bs in (("max", (81, 90, 300, 10000)), ("min", (81, 40, 1, 0.01))):
            for b in bs:
                p = mm.touch_prob(81.37, b, 0.0361, 29.5, d)
                self.assertGreaterEqual(p, 0.0)
                self.assertLessEqual(p, 1.0)
        self.assertEqual(mm.fair_value_cents(100, 90, 0.03, 10, "max"), 99)
        self.assertEqual(mm.fair_value_cents(100, 10000, 0.03, 10, "max"), 1)
        self.assertEqual(mm.fair_value_cents(100, 110, 0.03, 10, "min"), 99)
        self.assertEqual(mm.fair_value_cents(100, 0.01, 0.03, 10, "min"), 1)


class TestVol(unittest.TestCase):
    def test_known_vol_recovered(self):
        import random
        random.seed(7)
        sigma = 0.03
        closes = [100.0]
        for _ in range(400):
            closes.append(closes[-1] * math.exp(random.gauss(0, sigma)))
        self.assertAlmostEqual(mm.blended_daily_vol(closes), sigma, delta=0.01)

    def test_bad_input(self):
        with self.assertRaises(mm.DataError):
            mm.ewma_vol([])
        with self.assertRaises(mm.DataError):
            mm.simple_vol([0.01])


class TestBuildQuotes(unittest.TestCase):
    # Default external book sits just outside the first ladder level so the
    # classic ladder shape comes through; joins/caps are tested explicitly.
    def q(self, fair, bb=None, ba=None, room_buy=1000, room_sell=1000):
        return mm.build_quotes("T", fair, bb, ba, room_buy, room_sell)

    def bids(self, quotes):
        return sorted(((x.price_cents, x.count) for x in quotes if x.book_side == "bid"),
                      reverse=True)

    def asks(self, quotes):
        return sorted((x.price_cents, x.count) for x in quotes if x.book_side == "ask")

    def test_standard_ladder(self):
        C, N, S = mm.CONTRACTS_PER_LEVEL, mm.NUM_LEVELS, mm.LEVEL_SPACING_CENTS
        quotes = self.q(50, bb=46, ba=54)
        self.assertEqual(self.bids(quotes), [(45 - S * i, C) for i in range(N)])
        self.assertEqual(self.asks(quotes), [(55 + S * i, C) for i in range(N)])

    def test_never_lead_bid_joins_external_best(self):
        # fair 50 but the best external bid is only 40: join it, never improve;
        # deeper levels keep the exact 2c spacing off the clamped anchor
        quotes = self.q(50, bb=40, ba=54)
        self.assertEqual([p for p, _c in self.bids(quotes)],
                         [40 - mm.LEVEL_SPACING_CENTS * i for i in range(mm.NUM_LEVELS)])

    def test_never_lead_ask_joins_external_best(self):
        # fair 50 but the best external ask is 60: join it, never undercut
        quotes = self.q(50, bb=46, ba=60)
        self.assertEqual([p for p, _c in self.asks(quotes)],
                         [60 + mm.LEVEL_SPACING_CENTS * i for i in range(mm.NUM_LEVELS)])

    def test_empty_side_never_quoted_alone(self):
        self.assertEqual(self.bids(self.q(50, bb=None, ba=54)), [])
        self.assertEqual(len(self.asks(self.q(50, bb=None, ba=54))), mm.NUM_LEVELS)
        self.assertEqual(self.asks(self.q(50, bb=46, ba=None)), [])
        self.assertEqual(len(self.bids(self.q(50, bb=46, ba=None))), mm.NUM_LEVELS)
        self.assertEqual(self.q(50, bb=None, ba=None), [])

    def test_low_fair_drops_negative_bids(self):
        quotes = self.q(3, bb=2, ba=4)
        self.assertEqual(self.bids(quotes), [])   # ladder wants -2,-4,-6: dropped
        C = mm.CONTRACTS_PER_LEVEL
        self.assertEqual(self.asks(quotes),
                         [(8 + mm.LEVEL_SPACING_CENTS * i, C) for i in range(mm.NUM_LEVELS)])

    def test_high_fair_drops_over_99_asks(self):
        quotes = self.q(96, bb=92, ba=99)
        self.assertEqual(self.asks(quotes), [])   # ladder wants 101+: dropped
        C = mm.CONTRACTS_PER_LEVEL
        self.assertEqual(self.bids(quotes),
                         [(91 - mm.LEVEL_SPACING_CENTS * i, C) for i in range(mm.NUM_LEVELS)])

    def test_cross_clamp_bid_never_crosses_ask(self):
        quotes = self.q(60, bb=48, ba=50)
        self.assertEqual([p for p, _c in self.bids(quotes)],
                         [48 - mm.LEVEL_SPACING_CENTS * i for i in range(mm.NUM_LEVELS)])
        self.assertTrue(all(p < 50 for p, _c in self.bids(quotes)))

    def test_cross_clamp_ask_never_crosses_bid(self):
        quotes = self.q(40, bb=52, ba=55)
        self.assertEqual([p for p, _c in self.asks(quotes)],
                         [55 + mm.LEVEL_SPACING_CENTS * i for i in range(mm.NUM_LEVELS)])
        self.assertTrue(all(p > 52 for p, _c in self.asks(quotes)))

    def test_room_shaves_ladder(self):
        C = mm.CONTRACTS_PER_LEVEL
        quotes = self.q(50, bb=46, ba=54, room_buy=2 * C + 2, room_sell=0)
        self.assertEqual(self.bids(quotes), [(45, C), (43, C), (41, 2)])
        self.assertEqual(self.asks(quotes), [])
        quotes = self.q(50, bb=46, ba=54, room_buy=0, room_sell=3)
        self.assertEqual(self.asks(quotes), [(55, 3)])
        self.assertEqual(self.bids(quotes), [])

    def test_negative_or_fractional_room(self):
        quotes = self.q(50, bb=46, ba=54, room_buy=-5, room_sell=0.9)
        self.assertEqual(quotes, [])

    def test_ladder_total_never_exceeds_room(self):
        for room in range(0, 40):
            quotes = self.q(50, bb=46, ba=54, room_buy=room, room_sell=room)
            for side in ("bid", "ask"):
                total = sum(q.count for q in quotes if q.book_side == side)
                self.assertLessEqual(total, room)

    def test_no_duplicate_levels_and_invariants(self):
        for fair in range(1, 100):
            for bb, ba in ((3, 5), (40, 42), (49, 51), (90, 95), (1, 99)):
                quotes = mm.build_quotes("T", fair, bb, ba, 1000, 1000)
                keys = [(x.book_side, x.price_cents) for x in quotes]
                self.assertEqual(len(keys), len(set(keys)), f"dup fair={fair} {bb}/{ba}")
                for x in quotes:
                    self.assertTrue(1 <= x.price_cents <= 99)
                    if x.book_side == "bid":
                        self.assertLessEqual(x.price_cents, bb)   # never lead
                        self.assertLess(x.price_cents, ba)        # never cross
                    else:
                        self.assertGreaterEqual(x.price_cents, ba)
                        self.assertGreater(x.price_cents, bb)
                # INVARIANT: consecutive levels exactly LEVEL_SPACING_CENTS apart
                for side, sort_desc in (("bid", True), ("ask", False)):
                    px = sorted((x.price_cents for x in quotes if x.book_side == side),
                                reverse=sort_desc)
                    for a, b in zip(px, px[1:]):
                        self.assertEqual(abs(a - b), mm.LEVEL_SPACING_CENTS,
                                         f"gap violation fair={fair} {bb}/{ba} {side}: {px}")


class TestOrderbookBest(unittest.TestCase):
    def test_v2_fp_format(self):
        ob = {"orderbook_fp": {
            "yes_dollars": [["0.4000", "10"], ["0.5400", "20"]],
            "no_dollars": [["0.3000", "5"], ["0.4500", "7"]],
        }}
        self.assertEqual(mm.orderbook_best(ob), (54, 55))

    def test_legacy_format(self):
        ob = {"orderbook": {"yes": [[40, 10], [54, 20]], "no": [[30, 5], [45, 7]]}}
        self.assertEqual(mm.orderbook_best(ob), (54, 55))

    def test_empty_book(self):
        self.assertEqual(mm.orderbook_best({"orderbook_fp": {}}), (None, None))
        self.assertEqual(mm.orderbook_best({}), (None, None))


class TestExternalBest(unittest.TestCase):
    YES = [[40.0, 10.0], [54.0, 10.0]]
    NO = [[30.0, 5.0], [45.0, 7.0]]

    def test_no_own_orders_is_plain_best(self):
        self.assertEqual(mm.external_best(self.YES, self.NO), (54, 55))

    def test_own_bid_consumes_top_level(self):
        # Our 10-lot bid at 54 IS the whole 54 level -> external best is 40
        own = [("bid", 54, 10.0)]
        self.assertEqual(mm.external_best(self.YES, self.NO, own), (40, 55))

    def test_own_bid_partial_level_still_counts(self):
        yes = [[40.0, 10.0], [54.0, 25.0]]
        own = [("bid", 54, 10.0)]  # others still have 15 at 54
        self.assertEqual(mm.external_best(yes, self.NO, own), (54, 55))

    def test_own_ask_consumes_no_level(self):
        # Our YES-book ask at 55 rests as NO at 45; the 45 NO level is 7 < our 10
        own = [("ask", 55, 10.0)]
        self.assertEqual(mm.external_best(self.YES, self.NO, own), (54, 70))

    def test_side_fully_ours_is_none(self):
        own = [("bid", 54, 10.0), ("bid", 40, 10.0)]
        self.assertEqual(mm.external_best(self.YES, self.NO, own), (None, 55))

    def test_stacked_own_orders_at_same_price(self):
        yes = [[54.0, 20.0]]
        own = [("bid", 54, 10.0), ("bid", 54, 10.0)]
        self.assertEqual(mm.external_best(yes, [], own), (None, None))


class TestMtdExtreme(unittest.TestCase):
    # candles: (ts, close, high, low)
    HIST = [(0, 50.0, 55.0, 45.0), (100, 60.0, 66.0, 54.0), (200, 70.0, 77.0, 63.0)]

    def test_max_direction(self):
        self.assertEqual(mm.candles_extreme(self.HIST, 150, 100, "max"), 77.0)
        self.assertEqual(mm.candles_extreme(self.HIST, 350, 100, "max"), None)

    def test_min_direction(self):
        self.assertEqual(mm.candles_extreme(self.HIST, 150, 100, "min"), 54.0)

    def test_boundary_candle_included(self):
        # month starts mid-candle: candle [100,200) contains ts=150 -> included
        self.assertEqual(mm.candles_extreme(self.HIST, 150, 100, "max"), 77.0)
        # month starts exactly at a candle end: candle [0,100) excluded
        self.assertEqual(mm.candles_extreme(self.HIST, 100, 100, "min"), 54.0)

    def test_breached(self):
        self.assertTrue(mm.breached(90, 92.0, "max"))
        self.assertFalse(mm.breached(90, 88.0, "max"))
        self.assertTrue(mm.breached(70, 69.5, "min"))
        self.assertFalse(mm.breached(70, 71.0, "min"))
        self.assertFalse(mm.breached(70, None, "min"))


# A resting order as the live V2 API returns it after the client's read
# normalization (book_side/outcome_side are raw V2; side/action rewritten to
# the legacy customer view; counts synthesized as floats; prices as *_dollars).
REAL_V2_ORDER = {
    "order_id": "ord-123", "ticker": "KXSOLMAXMON-SOL-26JUL31-9000",
    "client_order_id": "cmm-abcd1234-deadbeef0123", "status": "resting",
    "book_side": "bid", "outcome_side": "yes", "side": "yes", "action": "buy",
    "yes_price_dollars": "0.4500", "no_price_dollars": "0.5500",
    "initial_count_fp": "10.00", "remaining_count_fp": "10.00", "fill_count_fp": "0.00",
    "count": 10.0, "remaining_count": 10.0, "filled_count": 0.0,
}


class TestOrderParsing(unittest.TestCase):
    def test_real_v2_shape(self):
        self.assertEqual(mm.order_yes_book_cents(REAL_V2_ORDER), ("bid", 45))
        self.assertEqual(mm.order_remaining(REAL_V2_ORDER), 10.0)

    def test_real_v2_ask_shape(self):
        o = dict(REAL_V2_ORDER, book_side="ask", outcome_side="no", side="no",
                 action="buy", yes_price_dollars="0.5500", no_price_dollars="0.4500")
        self.assertEqual(mm.order_yes_book_cents(o), ("ask", 55))

    def test_price_dollars_variant(self):
        o = {"book_side": "bid", "price_dollars": "0.4500"}
        self.assertEqual(mm.order_yes_book_cents(o), ("bid", 45))

    def test_side_action_fallbacks(self):
        self.assertEqual(mm.order_yes_book_cents(
            {"side": "yes", "action": "buy", "yes_price": 45}), ("bid", 45))
        self.assertEqual(mm.order_yes_book_cents(
            {"side": "no", "action": "buy", "no_price": 45}), ("ask", 55))

    def test_unparseable(self):
        self.assertIsNone(mm.order_yes_book_cents({"ticker": "X"}))


class TestDiffOrders(unittest.TestCase):
    def desired(self):
        return [mm.Quote("T1", "bid", 45, 10), mm.Quote("T1", "ask", 55, 10)]

    def resting(self, px_bid=45, px_ask=55, rem=10.0):
        return [
            {"order_id": "a", "ticker": "T1", "book_side": "bid",
             "yes_price_dollars": f"{px_bid / 100:.4f}", "remaining_count": rem,
             "status": "resting"},
            {"order_id": "b", "ticker": "T1", "book_side": "ask",
             "yes_price_dollars": f"{px_ask / 100:.4f}", "remaining_count": rem,
             "status": "resting"},
        ]

    def test_perfect_match_no_churn(self):
        now = 1000.0
        ages = {"a": now - 10, "b": now - 10}
        place, cancel = mm.diff_orders(self.desired(), self.resting(), ages, now)
        self.assertEqual((place, cancel), ([], []))

    def test_within_tolerance_kept(self):
        now = 1000.0
        ages = {"a": now - 10, "b": now - 10}
        place, cancel = mm.diff_orders(self.desired(), self.resting(px_bid=44), ages, now)
        self.assertEqual((place, cancel), ([], []))

    def test_price_moved_replaces(self):
        now = 1000.0
        ages = {"a": now - 10, "b": now - 10}
        place, cancel = mm.diff_orders(self.desired(), self.resting(px_bid=40), ages, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(place, [mm.Quote("T1", "bid", 45, 10)])

    def test_partial_fill_replaced(self):
        # remaining 4 of 10 -> replace to restore full level size
        now = 1000.0
        ages = {"a": now - 10, "b": now - 10}
        place, cancel = mm.diff_orders(self.desired(), self.resting(rem=4.0), ages, now)
        self.assertEqual(sorted(cancel), ["a", "b"])
        self.assertEqual(len(place), 2)

    def test_stale_order_refreshed(self):
        now = 1000.0
        ages = {"a": now - mm.ORDER_REFRESH_SECS - 1, "b": now - 10}
        place, cancel = mm.diff_orders(self.desired(), self.resting(), ages, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(place, [mm.Quote("T1", "bid", 45, 10)])

    def test_orphan_resting_cancelled(self):
        now = 1000.0
        extra = self.resting() + [{"order_id": "c", "ticker": "T2", "book_side": "bid",
                                   "yes_price_dollars": "0.1000", "remaining_count": 10.0,
                                   "status": "resting"}]
        ages = {"a": now, "b": now, "c": now}
        place, cancel = mm.diff_orders(self.desired(), extra, ages, now)
        self.assertEqual((place, cancel), ([], ["c"]))

    def test_blind_ticker_preserved(self):
        # No desired quotes for T1 (blind) but its resting orders survive,
        # and nothing new is placed on it.
        now = 1000.0
        ages = {"a": now, "b": now}
        place, cancel = mm.diff_orders([], self.resting(), ages, now,
                                       preserve_tickers={"T1"})
        self.assertEqual((place, cancel), ([], []))
        # desired quotes for a blind ticker are also suppressed
        place, cancel = mm.diff_orders(self.desired(), [], ages, now,
                                       preserve_tickers={"T1"})
        self.assertEqual((place, cancel), ([], []))

    def test_unparseable_resting_cancelled_defensively(self):
        place, cancel = mm.diff_orders([], [{"order_id": "z", "ticker": "T1"}], {}, 0.0)
        self.assertEqual(cancel, ["z"])

    def test_duplicate_resting_one_kept(self):
        now = 1000.0
        dup = self.resting() + [{"order_id": "a2", "ticker": "T1", "book_side": "bid",
                                 "yes_price_dollars": "0.4500", "remaining_count": 10.0,
                                 "status": "resting"}]
        ages = {"a": now, "b": now, "a2": now}
        place, cancel = mm.diff_orders(self.desired(), dup, ages, now)
        self.assertEqual((place, cancel), ([], ["a2"]))


class FakeClient:
    """Minimal ExchangeClient stand-in for live-path order-management tests."""

    def __init__(self, order_pages=None):
        self.order_pages = order_pages or [{"orders": [], "cursor": None}]
        self.get_orders_calls = []
        self.cancelled = []
        self.cancel_routing = []   # (exchange_index, market_ticker) per cancel
        self.cancel_error = None
        self.created = []
        self.single_orders = {}   # order_id -> order dict served by get_order

    def get_orders(self, **kwargs):
        self.get_orders_calls.append(kwargs)
        idx = min(len(self.get_orders_calls) - 1, len(self.order_pages) - 1)
        return self.order_pages[idx]

    def get_order(self, order_id):
        if order_id not in self.single_orders:
            raise HttpError("not found", 404)
        return {"order": self.single_orders[order_id]}

    def create_order(self, **kwargs):
        oid = f"fake-{len(self.created)}"
        self.created.append(kwargs)
        return {"order": {"order_id": oid}}

    def cancel_order(self, order_id, exchange_index=None, market_ticker=None):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(order_id)
        self.cancel_routing.append((exchange_index, market_ticker))
        return {}

    def __getattr__(self, name):
        raise AssertionError(f"unexpected client call: {name}")


class TestFetchRestingOrders(unittest.TestCase):
    def bot(self, client):
        b = mm.TouchMarketMaker(SOL_MAX, client, live=True)
        b.state.event_ticker = "KXSOLMAXMON-SOL-26JUL31"
        return b

    def test_filters_status_and_prefix(self):
        ours = dict(REAL_V2_ORDER)
        not_ours = dict(REAL_V2_ORDER, order_id="ord-2", client_order_id="manual-1")
        executed = dict(REAL_V2_ORDER, order_id="ord-3", status="executed")
        client = FakeClient([{"orders": [ours, not_ours, executed], "cursor": None}])
        got = self.bot(client).fetch_resting_orders(0.0)
        self.assertEqual([o["order_id"] for o in got], ["ord-123"])
        # status filter is also requested server-side
        self.assertEqual(client.get_orders_calls[0].get("status"), "resting")

    def test_pagination_followed(self):
        p1 = {"orders": [dict(REAL_V2_ORDER, order_id="o1")], "cursor": "next"}
        p2 = {"orders": [dict(REAL_V2_ORDER, order_id="o2")], "cursor": None}
        client = FakeClient([p1, p2])
        got = self.bot(client).fetch_resting_orders(0.0)
        self.assertEqual([o["order_id"] for o in got], ["o1", "o2"])
        self.assertEqual(len(client.get_orders_calls), 2)
        self.assertEqual(client.get_orders_calls[1].get("cursor"), "next")

    def test_cancel_all_sweeps_prev_month(self):
        client = FakeClient([{"orders": [dict(REAL_V2_ORDER)], "cursor": None},
                             {"orders": [], "cursor": None}])
        bot = self.bot(client)
        n = bot.cancel_all_bot_orders(include_prev_month=True)
        self.assertEqual(n, 1)
        events = [c.get("event_ticker") for c in client.get_orders_calls]
        self.assertIn("KXSOLMAXMON-SOL-26JUL31", events)   # the bot's own event
        # The prev-month sweep uses the real clock: compute the expectation
        # with independent date math instead of a hardcoded month.
        import calendar as _cal
        from datetime import datetime as _dt, timezone as _tz
        now_et = _dt.now(_tz.utc).astimezone(mm.ET)
        y, m = (now_et.year - 1, 12) if now_et.month == 1 else (now_et.year, now_et.month - 1)
        exp = (f"KXSOLMAXMON-SOL-{y % 100:02d}"
               f"{_dt(y, m, 1).strftime('%b').upper()}{_cal.monthrange(y, m)[1]:02d}")
        self.assertIn(exp, events)


class TestLedgerMerge(unittest.TestCase):
    """The eventual-consistency fix: a lagging get_orders read must never
    cause duplicate ladders."""

    def bot(self, client):
        b = mm.TouchMarketMaker(SOL_MAX, client, live=True)
        b.state.event_ticker = "KXSOLMAXMON-SOL-26JUL31"
        b.alerter.enabled = False
        return b

    def test_lagging_read_does_not_duplicate(self):
        # Read always returns [] (max lag); ledger must stand in.
        client = FakeClient([{"orders": [], "cursor": None}])
        bot = self.bot(client)
        now = 1000.0
        desired = [mm.Quote("T1", "bid", 45, 10), mm.Quote("T1", "ask", 55, 10)]
        for q in desired:
            bot.place_order(q, now)
        self.assertEqual(len(client.created), 2)
        # Next cycle: exchange read is empty but merged view has the ledger.
        resting = bot.fetch_resting_orders(now + 30)
        self.assertEqual(len(resting), 2)
        place, cancel = mm.diff_orders(desired, resting, bot.state.order_ages, now + 30)
        self.assertEqual((place, cancel), ([], []))   # NO duplicate placement

    def test_confirmed_then_absent_is_dropped(self):
        # Read 1 confirms the order; read 2 loses it (filled) -> ledger drops it.
        client = FakeClient([{"orders": [], "cursor": None}])
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        oid = list(bot.state.ledger)[0]
        exchange_view = dict(REAL_V2_ORDER, order_id=oid)
        merged = bot._merge_ledger([exchange_view], 1030.0)
        self.assertTrue(bot.state.ledger[oid]["_confirmed"])
        self.assertEqual(len(merged), 1)
        merged = bot._merge_ledger([], 1060.0)   # now gone from the read
        self.assertEqual(merged, [])
        self.assertEqual(bot.state.ledger, {})

    def test_unconfirmed_past_grace_verified_via_get_order(self):
        client = FakeClient([{"orders": [], "cursor": None}])
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        oid = list(bot.state.ledger)[0]
        past_grace = 1000.0 + 2 * mm.POLL_SECS + 16
        # get_order says it's resting -> kept and confirmed
        client.single_orders[oid] = {"order_id": oid, "status": "resting"}
        merged = bot._merge_ledger([], past_grace)
        self.assertEqual(len(merged), 1)
        self.assertTrue(bot.state.ledger[oid]["_confirmed"])
        # ...and if instead it had been executed -> dropped
        bot.state.ledger[oid]["_confirmed"] = False
        client.single_orders[oid] = {"order_id": oid, "status": "executed"}
        merged = bot._merge_ledger([], past_grace + 1)
        self.assertEqual(merged, [])
        self.assertEqual(bot.state.ledger, {})

    def test_unconfirmed_404_dropped_and_ttl_expiry(self):
        client = FakeClient([{"orders": [], "cursor": None}])
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        oid = list(bot.state.ledger)[0]
        # 404 past grace -> dropped
        merged = bot._merge_ledger([], 1000.0 + 2 * mm.POLL_SECS + 16)
        self.assertEqual(merged, [])
        self.assertNotIn(oid, bot.state.ledger)
        # TTL expiry drops even a confirmed-never entry
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 2000.0)
        merged = bot._merge_ledger([], 2000.0 + mm.ORDER_TTL_SECS + 1)
        self.assertEqual(merged, [])

    def test_cancel_removes_from_ledger(self):
        client = FakeClient()
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        oid = list(bot.state.ledger)[0]
        self.assertTrue(bot.cancel_order(oid))
        self.assertEqual(bot.state.ledger, {})

    def test_place_routes_across_shards(self):
        """place_order must send exchange_index=-1 (auto-route by ticker):
        without it the create lands on shard 0 and 404s for every crypto
        market created after the 2026-08-24 exchange sharding."""
        client = FakeClient()
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        self.assertEqual(client.created[-1].get("exchange_index"), -1)

    def test_cancel_routes_by_market_ticker(self):
        """Sharded exchanges: a cancel without market_ticker lands on shard 0
        and 404s for shard-2 (crypto) orders — misread as 'already gone' while
        the quote rests until TTL. Explicit ticker wins, ledger is the
        fallback, and only a truly unknown ticker falls back to legacy."""
        client = FakeClient()
        bot = self.bot(client)
        bot.place_order(mm.Quote("T1", "bid", 45, 10), 1000.0)
        oid = list(bot.state.ledger)[0]
        bot.cancel_order(oid)                      # ticker from ledger
        self.assertEqual(client.cancel_routing[-1], (-1, "T1"))
        bot.cancel_order("unknown-oid", "T9")      # explicit ticker wins
        self.assertEqual(client.cancel_routing[-1], (-1, "T9"))
        bot.cancel_order("unknown-oid")            # no ticker anywhere: legacy
        self.assertEqual(client.cancel_routing[-1], (None, None))


class TestSideCap(unittest.TestCase):
    def bot(self):
        b = mm.TouchMarketMaker(SOL_MAX, FakeClient(), live=True)
        b.alerter.enabled = False
        return b

    def resting(self, n, side="bid", ticker="T1"):
        C = mm.CONTRACTS_PER_LEVEL
        return [{"order_id": f"r{i}", "ticker": ticker, "book_side": side,
                 "yes_price": 45 - 2 * i, "remaining_count": float(C),
                 "status": "resting"}
                for i in range(n)]

    def test_blocks_beyond_side_cap(self):
        bot = self.bot()
        C = mm.CONTRACTS_PER_LEVEL
        to_place = [mm.Quote("T1", "bid", 39, C)]
        placed = bot.place_with_side_cap(to_place, self.resting(mm.NUM_LEVELS),
                                         set(), 0.0)
        self.assertEqual(placed, 0, "full ladder resting: must refuse another level")
        self.assertEqual(bot.state.ledger, {})

    def test_allows_up_to_cap_and_counts_own_placements(self):
        bot = self.bot()
        C = mm.CONTRACTS_PER_LEVEL
        to_place = [mm.Quote("T1", "bid", 45 - 2 * i, C)
                    for i in range(mm.NUM_LEVELS + 1)]
        placed = bot.place_with_side_cap(to_place, [], set(), 0.0)
        self.assertEqual(placed, mm.NUM_LEVELS,
                         "one level beyond the side cap must be refused")

    def test_cancelled_orders_free_room_and_sides_independent(self):
        bot = self.bot()
        C = mm.CONTRACTS_PER_LEVEL
        resting = self.resting(mm.NUM_LEVELS) + self.resting(mm.NUM_LEVELS, side="ask")
        cancelled = {"r0"}   # one bid cancelled this cycle -> room for one new bid
        # probe prices BELOW the resting ladder so only the side cap governs
        lo = 45 - 2 * mm.NUM_LEVELS
        to_place = [mm.Quote("T1", "bid", lo, C), mm.Quote("T1", "bid", lo - 2, C)]
        placed = bot.place_with_side_cap(to_place, resting, cancelled, 0.0)
        self.assertEqual(placed, 1)

    def test_other_market_not_affected(self):
        bot = self.bot()
        placed = bot.place_with_side_cap([mm.Quote("T2", "bid", 40, mm.CONTRACTS_PER_LEVEL)],
                                         self.resting(mm.NUM_LEVELS, ticker="T1"), set(), 0.0)
        self.assertEqual(placed, 1)


class TestLevelCap(unittest.TestCase):
    # INVARIANT: never more than CONTRACTS_PER_LEVEL contracts resting at a
    # single price level on a market.

    def bot(self):
        b = mm.TouchMarketMaker(SOL_MAX, FakeClient(), live=True)
        b.alerter.enabled = False
        return b

    def rest_at(self, px, qty, oid="r0", side="bid"):
        return {"order_id": oid, "ticker": "T1", "book_side": side,
                "yes_price": px, "remaining_count": float(qty), "status": "resting"}

    def test_full_level_blocks_same_price(self):
        bot = self.bot()
        placed = bot.place_with_side_cap([mm.Quote("T1", "bid", 45, mm.CONTRACTS_PER_LEVEL)],
                                         [self.rest_at(45, mm.CONTRACTS_PER_LEVEL)], set(), 0.0)
        self.assertEqual(placed, 0)

    def test_partial_level_blocks_overfill(self):
        bot = self.bot()
        placed = bot.place_with_side_cap([mm.Quote("T1", "bid", 45, mm.CONTRACTS_PER_LEVEL)],
                                         [self.rest_at(45, 2)], set(), 0.0)
        self.assertEqual(placed, 0)

    def test_other_price_unaffected(self):
        bot = self.bot()
        placed = bot.place_with_side_cap([mm.Quote("T1", "bid", 43, mm.CONTRACTS_PER_LEVEL)],
                                         [self.rest_at(45, mm.CONTRACTS_PER_LEVEL)], set(), 0.0)
        self.assertEqual(placed, 1)

    def test_own_wave_cannot_stack_a_level(self):
        bot = self.bot()
        to_place = [mm.Quote("T1", "bid", 45, mm.CONTRACTS_PER_LEVEL), mm.Quote("T1", "bid", 45, mm.CONTRACTS_PER_LEVEL)]
        placed = bot.place_with_side_cap(to_place, [], set(), 0.0)
        self.assertEqual(placed, 1)

    def test_cancelled_level_frees_it(self):
        bot = self.bot()
        placed = bot.place_with_side_cap([mm.Quote("T1", "bid", 45, mm.CONTRACTS_PER_LEVEL)],
                                         [self.rest_at(45, mm.CONTRACTS_PER_LEVEL)], {"r0"}, 0.0)
        self.assertEqual(placed, 1)


class TestCancelOrder(unittest.TestCase):
    def test_404_409_treated_as_gone_410_not(self):
        client = FakeClient()
        bot = mm.TouchMarketMaker(SOL_MAX, client, live=True)
        for status, ok in ((404, True), (409, True), (410, False), (500, False)):
            client.cancel_error = HttpError("err", status)
            self.assertEqual(bot.cancel_order("x"), ok, f"status {status}")
        client.cancel_error = None
        self.assertTrue(bot.cancel_order("y"))
        self.assertEqual(client.cancelled, ["y"])


class TestDryRunSafety(unittest.TestCase):
    """A bot constructed with live=False must never touch order endpoints."""

    class ExplodingClient:
        def __getattr__(self, name):
            if name in ("create_order", "cancel_order", "batch_create_orders",
                        "batch_cancel_orders", "decrease_order"):
                raise AssertionError(f"dry-run bot called {name}!")
            raise AttributeError(name)

    def test_place_cancel_ttl_simulated(self):
        bot = mm.TouchMarketMaker(SOL_MAX, self.ExplodingClient(), live=False)
        now = 1000.0
        bot.place_order(mm.Quote("T1", "bid", 45, 10), now)
        bot.place_order(mm.Quote("T1", "ask", 55, 10), now)
        resting = bot.fetch_resting_orders(now + 1)
        self.assertEqual(sorted(mm.order_yes_book_cents(o) for o in resting),
                         [("ask", 55), ("bid", 45)])
        self.assertEqual(bot.fetch_resting_orders(now + mm.ORDER_TTL_SECS + 1), [])
        bot.place_order(mm.Quote("T1", "bid", 45, 10), now)
        self.assertEqual(bot.cancel_all_bot_orders(), 1)
        self.assertEqual(bot.state.sim_orders, {})

    def test_sim_orders_diff_roundtrip(self):
        bot = mm.TouchMarketMaker(SOL_MAX, self.ExplodingClient(), live=False)
        now = 1000.0
        desired = [mm.Quote("T1", "bid", 45, 7), mm.Quote("T1", "ask", 55, 10)]
        for q in desired:
            bot.place_order(q, now)
        place, cancel = mm.diff_orders(desired, bot.fetch_resting_orders(now + 60),
                                       bot.state.order_ages, now + 60)
        self.assertEqual((place, cancel), ([], []))


class TestSingletonLock(unittest.TestCase):
    """One live bot per market: duplicates each keep their own ledger, so the
    per-side/level caps would silently double."""

    def test_acquire_release_and_takeover_of_stale_lock(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mm, "STATUS_DIR", d):
                self.assertTrue(mm.acquire_singleton("SOL-MAX"))
                path = os.path.join(d, "lock_SOL-MAX.pid")
                self.assertEqual(open(path).read().strip(), str(os.getpid()))
                # a dead owner's lock is taken over
                with open(path, "w") as f:
                    f.write("999999")
                with mock.patch.object(mm, "_pid_alive", return_value=False):
                    self.assertTrue(mm.acquire_singleton("SOL-MAX"))
                self.assertEqual(open(path).read().strip(), str(os.getpid()))
                # release only removes our own lock
                mm._release_singleton(path)
                self.assertFalse(os.path.exists(path))

    def test_refuses_when_other_instance_alive(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mm, "STATUS_DIR", d):
                with open(os.path.join(d, "lock_SOL-MAX.pid"), "w") as f:
                    f.write("424242")
                with mock.patch.object(mm, "_pid_alive", return_value=True),                         mock.patch.object(mm, "_heartbeat_fresh", return_value=True):
                    self.assertFalse(mm.acquire_singleton("SOL-MAX"))

    def test_live_pid_but_stale_heartbeat_is_taken_over(self):
        # PID reuse must not park a market forever: without a fresh heartbeat
        # the lock is treated as abandoned.
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mm, "STATUS_DIR", d):
                with open(os.path.join(d, "lock_SOL-MAX.pid"), "w") as f:
                    f.write("424242")
                with mock.patch.object(mm, "_pid_alive", return_value=True),                         mock.patch.object(mm, "_heartbeat_fresh", return_value=False):
                    self.assertTrue(mm.acquire_singleton("SOL-MAX"))

    def test_heartbeat_fresh_reads_status_mtime(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mm, "STATUS_DIR", d):
                self.assertFalse(mm._heartbeat_fresh("SOL-MAX"))
                p = os.path.join(d, "status_SOL-MAX.json")
                with open(p, "w") as f:
                    json.dump({"market": "SOL-MAX"}, f)
                self.assertTrue(mm._heartbeat_fresh("SOL-MAX"))
                old = __import__("time").time() - 9999
                os.utime(p, (old, old))
                self.assertFalse(mm._heartbeat_fresh("SOL-MAX"))

    def test_unreadable_lock_dir_does_not_block_trading(self):
        with mock.patch.object(mm, "STATUS_DIR", "\\\\?\\Z:\\nonexistent"):
            self.assertTrue(mm.acquire_singleton("SOL-MAX"))


class TestMarketDataCache(unittest.TestCase):
    def test_roundtrip_expiry_and_types(self):
        import os, tempfile, time as _time
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mm, "STATUS_DIR", d):
                self.assertIsNone(mm.cache_get("price", "TESTUSD", 10))
                mm.cache_put("price", "TESTUSD", 81.37)
                self.assertEqual(mm.cache_get("price", "TESTUSD", 10), 81.37)
                # candle rows survive the JSON roundtrip as 4-tuples
                rows = [(100, 1.0, 2.0, 0.5), (200, 1.1, 2.1, 0.6)]
                mm.cache_put("ohlc60", "TESTUSD", rows)
                back = [tuple(r) for r in mm.cache_get("ohlc60", "TESTUSD", 10)]
                self.assertEqual(back, rows)
                # age out via mtime
                p = os.path.join(d, "cache_price_TESTUSD.json")
                old = _time.time() - 999
                os.utime(p, (old, old))
                self.assertIsNone(mm.cache_get("price", "TESTUSD", 10))


class TestFailSafeLoop(unittest.TestCase):
    class FailingBot(mm.TouchMarketMaker):
        def run_cycle(self):
            raise mm.DataError("boom")

    def failing_bot(self):
        bot = self.FailingBot(SOL_MAX, TestDryRunSafety.ExplodingClient(), live=False)
        bot.state.sim_orders["x"] = {"order_id": "x", "ticker": "T1", "book_side": "bid",
                                     "yes_price": 45, "remaining_count": 10.0,
                                     "status": "resting", "expire_at": 1e12}
        return bot

    def test_failsafe_after_four_consecutive_errors(self):
        bot = self.failing_bot()
        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= mm.FAILSAFE_CANCEL_AFTER + 1:
                raise SystemExit  # break the loop; finally must still run

        with mock.patch.object(mm.time, "sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                bot.run()
        self.assertGreaterEqual(bot.state.consecutive_errors, mm.FAILSAFE_CANCEL_AFTER)
        self.assertEqual(bot.state.sim_orders, {}, "fail-safe should cancel resting orders")
        # backoff engaged: sleeps longer than the base poll interval
        self.assertTrue(all(s >= mm.POLL_SECS for s in sleeps))
        self.assertTrue(any(s > mm.POLL_SECS for s in sleeps))

    def test_fewer_errors_do_not_trip_failsafe(self):
        bot = self.failing_bot()
        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= mm.FAILSAFE_CANCEL_AFTER - 1:
                raise SystemExit

        with mock.patch.object(mm.time, "sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                bot.run()
        self.assertLess(bot.state.consecutive_errors, mm.FAILSAFE_CANCEL_AFTER)
        self.assertIn("x", bot.state.sim_orders, "orders must survive brief error runs")

    def test_wake_grace_suppresses_failsafe(self):
        bot = self.failing_bot()
        clock = {"t": 1_000_000.0}
        sleeps = []

        def fake_time():
            return clock["t"]

        def fake_sleep(secs):
            sleeps.append(secs)
            # First sleep spans a simulated 2h suspend; later sleeps are normal.
            clock["t"] += 7200.0 if len(sleeps) == 1 else float(secs)
            # Exit while still inside the post-resume grace window: one counted
            # pre-suspend error + two suppressed in-grace errors.
            if len(sleeps) >= 3:
                raise SystemExit

        with mock.patch.object(mm.time, "time", fake_time), \
                mock.patch.object(mm.time, "sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                bot.run()
        # Error #1 counted pre-suspend; all errors inside the post-resume grace
        # window are ignored, so the fail-safe never fires.
        self.assertLessEqual(bot.state.consecutive_errors, 1)
        self.assertIn("x", bot.state.sim_orders, "grace must prevent the fail-safe")
        self.assertGreater(bot.state.errors_today, 1, "errors still tallied for the digest")

    def test_unexpected_exception_type_does_not_kill_loop(self):
        calls = {"n": 0}

        class KeyErrorBot(mm.TouchMarketMaker):
            def run_cycle(self):
                calls["n"] += 1
                raise KeyError("ticker")  # not a DataError/HttpError

        bot = KeyErrorBot(SOL_MAX, TestDryRunSafety.ExplodingClient(), live=False)

        def fake_sleep(_secs):
            if calls["n"] >= 2:
                raise SystemExit

        with mock.patch.object(mm.time, "sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                bot.run()
        self.assertGreaterEqual(calls["n"], 2, "loop must survive unexpected exceptions")


class TestAlerter(unittest.TestCase):
    def creds(self, recipients=("9729713381@tmomail.net",)):
        return mock.patch.multiple(mm, ALERT_EMAIL_FROM="bot@gmail.com",
                                   ALERT_EMAIL_PASSWORD="app-pass",
                                   ALERT_RECIPIENTS=list(recipients))

    def smtp(self):
        return mock.patch.object(mm.smtplib, "SMTP_SSL")

    def sends(self, smtp_mock):
        return smtp_mock.return_value.__enter__.return_value.sendmail.call_args_list

    def test_disabled_without_creds(self):
        with mock.patch.multiple(mm, ALERT_EMAIL_FROM="", ALERT_EMAIL_PASSWORD=""):
            a = mm.Alerter("SOL-MAX", live=False)
            self.assertFalse(a.enabled)
            with self.smtp() as s:
                a.alert("breach", "test", key="T1")
            s.assert_not_called()
            self.assertEqual(a.today, [("breach", "test")])  # still recorded for daily

    def test_urgent_alert_sends_and_dedupes(self):
        with self.creds(), self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            a.alert("breach", "strike 90 touched", key="T1", now_ts=1000.0)
            a.alert("breach", "strike 90 touched again", key="T1", now_ts=2000.0)  # deduped
            a.alert("breach", "strike 95 touched", key="T2", now_ts=2000.0)        # new key
            a.alert("breach", "strike 90 later", key="T1",
                    now_ts=1000.0 + mm.ALERT_DEDUPE_SECS + 1)                      # window past
            self.assertEqual(len(self.sends(s)), 3)
            body = self.sends(s)[0][0][2]
            self.assertIn("DRY", body)
            self.assertIn("breach", body)

    def test_non_urgent_never_sends(self):
        with self.creds(), self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            a.alert("rollover", "now trading X", urgent=False)
            self.assertEqual(len(self.sends(s)), 0)
            self.assertEqual(len(a.today), 1)

    def test_sms_gateway_truncated_no_subject(self):
        with self.creds(), self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            a.send_message("x" * 1000, subject="ignored for sms")
            payload = self.sends(s)[0][0][2]
            # MIMEText payload: body is after the blank header separator
            headers, body = payload.split("\n\n", 1)
            self.assertLessEqual(len(body.strip()), mm.SMS_MAX_CHARS)
            self.assertIn("Subject: \n", headers + "\n")

    def test_email_recipient_full_body_with_subject(self):
        with self.creds(recipients=["jackdu224@gmail.com"]), self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            a.send_message("x" * 1000, subject="daily summary")
            payload = self.sends(s)[0][0][2]
            headers, body = payload.split("\n\n", 1)
            self.assertGreater(len(body.strip()), mm.SMS_MAX_CHARS)
            self.assertIn("Subject: daily summary", headers)
            self.assertEqual(self.sends(s)[0][0][1], ["jackdu224@gmail.com"])

    def test_html_multipart_for_email_plain_for_sms(self):
        with self.creds(recipients=["jackdu224@gmail.com", "9729713381@tmomail.net"]), \
                self.smtp() as s:
            a = mm.Alerter("FLEET", live=True)
            a.send_message("plain body", subject="digest", html="<table><tr><td>x</td></tr></table>")
            payloads = {call[0][1][0]: call[0][2] for call in self.sends(s)}
            email_payload = payloads["jackdu224@gmail.com"]
            self.assertIn("multipart/alternative", email_payload)
            self.assertIn("text/html", email_payload)
            self.assertIn("plain body", email_payload)
            sms_payload = payloads["9729713381@tmomail.net"]
            self.assertNotIn("text/html", sms_payload)

    def test_multiple_recipients_mixed(self):
        with self.creds(recipients=["9729713381@tmomail.net", "jackdu224@gmail.com"]), \
                self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            self.assertTrue(a.send_message("hello"))
            self.assertEqual(len(self.sends(s)), 2)

    def test_is_sms_gateway(self):
        self.assertTrue(mm.Alerter._is_sms_gateway("9729713381@tmomail.net"))
        self.assertFalse(mm.Alerter._is_sms_gateway("jackdu224@gmail.com"))
        self.assertFalse(mm.Alerter._is_sms_gateway("bot123@x.com"))

    def test_send_failure_never_raises(self):
        with self.creds(), self.smtp() as s:
            s.side_effect = OSError("smtp down")
            a = mm.Alerter("SOL-MAX", live=False)
            a.alert("failsafe", "boom", key="x")  # must not raise

    def test_daily_summary_timing(self):
        with self.creds(), self.smtp() as s,                 mock.patch.object(mm, "SUMMARY_HOUR_CT", 8):
            a = mm.Alerter("SOL-MAX", live=False)
            built = {"n": 0}

            def builder():
                built["n"] += 1
                return "summary body"

            # First tick at 07:00 CT (12:00 UTC in July, CDT=UTC-5): initializes only.
            a.maybe_daily_summary(utc(2026, 7, 10, 12, 0), builder)
            self.assertEqual(built["n"], 0)
            # Still before 08:00 CT.
            a.maybe_daily_summary(utc(2026, 7, 10, 12, 59), builder)
            self.assertEqual(built["n"], 0)
            # 08:01 CT crosses the hour -> fires once.
            a.maybe_daily_summary(utc(2026, 7, 10, 13, 1), builder)
            self.assertEqual(built["n"], 1)
            self.assertEqual(a.last_summary_body, "summary body")
            a.maybe_daily_summary(utc(2026, 7, 10, 14, 0), builder)
            self.assertEqual(built["n"], 1)  # not again the same day
            # Next day at 08:05 CT -> fires again.
            a.maybe_daily_summary(utc(2026, 7, 11, 13, 5), builder)
            self.assertEqual(built["n"], 2)
            # Summaries are STORED for the fleet digest, not emailed per-bot.
            self.assertEqual(len(self.sends(s)), 0)

    def test_daily_summary_per_bot_email_optin(self):
        with self.creds(), self.smtp() as s, \
                mock.patch.object(mm, "SUMMARY_HOUR_CT", 8), \
                mock.patch.dict(mm.os.environ, {"CMM_SUMMARY_EMAIL_PER_BOT": "1"}):
            a = mm.Alerter("SOL-MAX", live=False)
            a.maybe_daily_summary(utc(2026, 7, 10, 12, 0), lambda: "x")   # init
            a.maybe_daily_summary(utc(2026, 7, 10, 13, 1), lambda: "x")   # fires
            self.assertEqual(len(self.sends(s)), 1)

    def test_midday_start_does_not_blast(self):
        with self.creds(), self.smtp() as s:
            a = mm.Alerter("SOL-MAX", live=False)
            # First-ever tick already past the summary hour: initialize, no send.
            a.maybe_daily_summary(utc(2026, 7, 10, 18, 0), builder := (lambda: "x"))
            a.maybe_daily_summary(utc(2026, 7, 10, 23, 0), builder)
            self.assertEqual(len(self.sends(s)), 0)

    def test_build_daily_summary_resets_counters(self):
        bot = mm.TouchMarketMaker(SOL_MAX, None, live=False)
        st = bot.state
        st.live_price, st.sigma_daily, st.mtd_extreme = 81.5, 0.032, 82.65
        st.cycles_today, st.placed_today, st.cancelled_today, st.errors_today = 100, 40, 2, 1
        st.last_markets_line = "7/7 mkts quoted (38 quotes)"
        bot.alerter.today.append(("breach", "x"))
        body = bot.build_daily_summary()
        self.assertIn("SOL-MAX daily (DRY)", body)
        self.assertIn("$81.50", body)
        self.assertIn("cycles 100", body)
        self.assertIn("breachx1", body)
        self.assertLessEqual(len(body), mm.SMS_MAX_CHARS)
        self.assertEqual((st.cycles_today, st.placed_today), (0, 0))


class TestRefreshVol(unittest.TestCase):
    def test_partial_day_candle_excluded_from_returns(self):
        import random
        random.seed(3)
        sigma = 0.03
        closes = [100.0]
        for _ in range(200):
            closes.append(closes[-1] * math.exp(random.gauss(0, sigma)))
        now_utc = utc(2026, 7, 3, 0, 30)  # just after UTC midnight
        today_ts = int(utc(2026, 7, 3, 0, 0).timestamp())
        # last candle is "today" with a barely-moved close (the bias scenario)
        candles = [(today_ts - 86400 * (len(closes) - i), c, c, c)
                   for i, c in enumerate(closes)]
        candles.append((today_ts, closes[-1] * 1.0001, closes[-1], closes[-1]))
        expected = mm.blended_daily_vol(closes)

        bot = mm.TouchMarketMaker(SOL_MAX, None, live=False)
        with mock.patch.object(mm, "fetch_ohlc", return_value=candles):
            bot.refresh_vol(now_ts=1.0, now_utc=now_utc)
        self.assertAlmostEqual(bot.state.sigma_daily, expected, places=12)
        # the partial candle stays available for month-to-date extremes
        self.assertEqual(len(bot.state.daily_candles), len(candles))



# ---------------------------------------------------------------------------
# v2.6 monthly risk rules (Jack 2026-09-12 "ship 1, 2, 3, 4"): vol shrinkage
# toward the long-run median, ask floor on cheap tails, dollars-at-risk cap,
# inventory skew + reduce-only. ON only for MonthlyTouchMarketMaker (the live
# monthly entrypoint); the base class — reused by the annual and weekly
# variants — must behave exactly as before. These tests PIN the live values:
# changing a rule's parameter must break a test here.
# ---------------------------------------------------------------------------
import os
import statistics
import time
from datetime import timedelta


def _gbm_closes(n, sigma, seed, start=100.0):
    import random
    random.seed(seed)
    closes = [start]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(random.gauss(0, sigma)))
    return closes


class TestLiveRiskRuleConfig(unittest.TestCase):
    def test_monthly_class_pins_the_shipped_values(self):
        m = mm.MonthlyTouchMarketMaker
        self.assertEqual(m.vol_shrink_w, 0.25)
        self.assertEqual(m.ask_min_fair_cents, 15)
        self.assertEqual(m.max_event_risk_dollars, 300)
        self.assertEqual(m.skew_max_cents, 4)
        self.assertEqual(m.skew_full_at, 300)
        self.assertEqual(m.skew_edge_floor_cents, 3)
        self.assertEqual(m.reduce_only_at, 600)
        self.assertEqual(mm.MODEL_VERSION, "crypto_touch_mm_v2.6")

    def test_base_class_rules_are_off(self):
        b = mm.TouchMarketMaker
        self.assertEqual(b.vol_shrink_w, 1.0)
        self.assertEqual(b.ask_min_fair_cents, 0)
        self.assertEqual(b.max_event_risk_dollars, float("inf"))
        self.assertEqual(b.skew_max_cents, 0)
        self.assertEqual(b.reduce_only_at, float("inf"))
        # the shipped ladder/caps are inherited unchanged
        self.assertEqual((mm.MonthlyTouchMarketMaker.num_levels,
                          mm.MonthlyTouchMarketMaker.contracts_per_level,
                          mm.MonthlyTouchMarketMaker.max_position,
                          mm.MonthlyTouchMarketMaker.max_event), (3, 5, 200, 1000))


class TestShrunkVol(unittest.TestCase):
    def test_w1_is_raw_bit_for_bit(self):
        closes = _gbm_closes(400, 0.03, 1)
        sigma, raw, med = mm.shrunk_daily_vol(closes, 1.0)
        self.assertEqual(sigma, mm.blended_daily_vol(closes))
        self.assertEqual(raw, sigma)
        self.assertIsNone(med)

    def test_w0_is_the_long_run_median(self):
        closes = _gbm_closes(400, 0.03, 2)
        sigma, raw, med = mm.shrunk_daily_vol(closes, 0.0)
        expected = statistics.median(mm.blended_daily_vol(closes[:i])
                                     for i in range(mm.VOL_MEDIAN_MIN_HISTORY, len(closes) + 1))
        self.assertAlmostEqual(med, expected, places=12)
        self.assertAlmostEqual(sigma, expected, places=12)
        self.assertEqual(mm.long_run_median_vol(closes), med)

    def test_variance_shrinkage_formula(self):
        closes = _gbm_closes(400, 0.03, 3)
        sigma, raw, med = mm.shrunk_daily_vol(closes, 0.25)
        self.assertAlmostEqual(sigma, math.sqrt(0.25 * raw ** 2 + 0.75 * med ** 2), places=12)
        lo, hi = sorted((raw, med))
        self.assertTrue(lo <= sigma <= hi)

    def test_low_vol_regime_is_lifted_toward_the_median(self):
        # 300 days at 4%/day then 100 quiet days at 1%: the raw EWMA/90d blend
        # sits near the quiet regime, the long-run median near 4% -> shrinkage
        # lifts sigma well above raw (the August 2026 failure mode).
        closes = _gbm_closes(300, 0.04, 4)
        quiet = _gbm_closes(100, 0.01, 5)
        closes += [closes[-1] * c / 100.0 for c in quiet[1:]]
        sigma, raw, med = mm.shrunk_daily_vol(closes, 0.25)
        self.assertGreater(med, raw * 1.5)
        self.assertGreater(sigma, raw * 1.3)

    def test_high_vol_regime_is_pulled_down(self):
        closes = _gbm_closes(300, 0.02, 8)
        wild = _gbm_closes(60, 0.06, 9)
        closes += [closes[-1] * c / 100.0 for c in wild[1:]]
        sigma, raw, med = mm.shrunk_daily_vol(closes, 0.25)
        self.assertLess(med, raw)
        self.assertLess(sigma, raw)

    def test_short_history_falls_back_to_raw(self):
        closes = _gbm_closes(100, 0.03, 6)   # <= VOL_MEDIAN_MIN_HISTORY closes
        sigma, raw, med = mm.shrunk_daily_vol(closes, 0.25)
        self.assertIsNone(med)
        self.assertEqual(sigma, raw)
        self.assertIsNone(mm.long_run_median_vol(closes))

    def test_refresh_vol_applies_the_class_weight(self):
        closes = _gbm_closes(400, 0.03, 7)
        today_ts = int(utc(2026, 9, 12, 0, 0).timestamp())
        candles = [(today_ts - 86400 * (len(closes) - i), c, c, c) for i, c in enumerate(closes)]
        base = mm.TouchMarketMaker(SOL_MAX, None, live=False)
        monthly = mm.MonthlyTouchMarketMaker(SOL_MAX, None, live=False)
        with mock.patch.object(mm, "fetch_ohlc", return_value=candles):
            base.refresh_vol(now_ts=1.0, now_utc=utc(2026, 9, 12, 0, 30))
            monthly.refresh_vol(now_ts=1.0, now_utc=utc(2026, 9, 12, 0, 30))
        raw = mm.blended_daily_vol(closes)
        self.assertEqual(base.state.sigma_daily, raw)
        self.assertIsNone(base.state.sigma_median)
        self.assertEqual(monthly.state.sigma_raw, raw)
        self.assertIsNotNone(monthly.state.sigma_median)
        w = mm.MonthlyTouchMarketMaker.vol_shrink_w
        self.assertAlmostEqual(monthly.state.sigma_daily,
                               math.sqrt(w * raw ** 2 + (1 - w) * monthly.state.sigma_median ** 2),
                               places=12)


class TestSkewedFairs(unittest.TestCase):
    def test_flat_or_disabled_is_identity(self):
        self.assertEqual(mm.skewed_fairs(50, 0, 4, 300, 5, 3), (50, 50))
        self.assertEqual(mm.skewed_fairs(50, 250, 0, 300, 5, 3), (50, 50))
        self.assertEqual(mm.skewed_fairs(50, 250, 4, 0, 5, 3), (50, 50))

    def test_long_inventory_shifts_down_with_edge_floor_on_asks(self):
        # full skew 4c on the discouraged side (bids), asks come in only 2c so
        # they keep 5-2 = 3c of edge vs the unskewed fair
        self.assertEqual(mm.skewed_fairs(50, 300, 4, 300, 5, 3), (46, 48))
        fair_bid, fair_ask = mm.skewed_fairs(50, 300, 4, 300, 5, 3)
        self.assertGreaterEqual(fair_ask + 5, 50 + 3)

    def test_short_inventory_is_the_mirror_image(self):
        self.assertEqual(mm.skewed_fairs(50, -300, 4, 300, 5, 3), (52, 54))

    def test_skew_is_proportional_and_saturates(self):
        self.assertEqual(mm.skewed_fairs(50, 150, 4, 300, 5, 3), (48, 48))
        self.assertEqual(mm.skewed_fairs(50, 900, 4, 300, 5, 3), (46, 48))
        self.assertEqual(mm.skewed_fairs(50, -900, 4, 300, 5, 3), (52, 54))

    def test_clamped_to_price_range(self):
        self.assertEqual(mm.skewed_fairs(2, 300, 4, 300, 5, 3), (1, 1))
        self.assertEqual(mm.skewed_fairs(98, -300, 4, 300, 5, 3), (99, 99))


class TestEventRisk(unittest.TestCase):
    def test_risk_dollars_by_direction(self):
        short, long = mm.event_risk_dollars({"A": -100, "B": 50, "C": -20}, {"A": 20, "B": 40})
        self.assertAlmostEqual(short, 100 * 0.8 + 20 * 0.5)   # C unpriced -> 50c
        self.assertAlmostEqual(long, 50 * 0.4)

    def test_risk_room(self):
        self.assertAlmostEqual(mm.risk_room_contracts(80, 300, 0.8), 275)
        self.assertEqual(mm.risk_room_contracts(320, 300, 0.8), 0.0)
        self.assertEqual(mm.risk_room_contracts(0, float("inf"), 0.8), float("inf"))
        self.assertEqual(mm.risk_room_contracts(0, 300, 0.0), float("inf"))


class RulesFakeClient(FakeClient):
    """FakeClient + the read endpoints one quoting cycle needs."""

    def __init__(self, markets, positions, books):
        super().__init__()
        self.markets, self.positions, self.books = markets, positions, books

    def get_event(self, event_ticker):
        return {"event": {"markets": self.markets}}

    def get_positions(self, **kwargs):
        return {"market_positions": [{"ticker": t, "position": p}
                                     for t, p in self.positions.items()]}

    def get_orderbook(self, ticker):
        bb, ba = self.books[ticker]
        return {"orderbook": {"yes": [[bb, 50]], "no": [[100 - ba, 50]]}}


def _mk(strike):
    return {"ticker": f"KXSOLMAXMON-SOL-26SEP30-{int(strike)}", "status": "active",
            "strike_type": "greater", "floor_strike": strike}


def _cycle(cls, markets, positions, books, fairs):
    """One live cycle with fairs pinned per strike; returns (bids, asks) per
    ticker as sorted YES-price lists plus the raw created orders."""
    client = RulesFakeClient(markets, positions, {m["ticker"]: books[m["floor_strike"]] for m in markets})
    bot = cls(SOL_MAX, client, live=True)
    bot.state.sigma_daily = 0.03
    bot.state.vol_fetched_at = time.time()      # skip the vol fetch
    bot.state.mtd_hourly_at = time.time()       # skip the hourly fetch (session extreme = spot)
    with mock.patch.object(bot.alerter, "alert"), \
         mock.patch.object(mm, "fetch_live_price", return_value=100.0), \
         mock.patch.object(mm, "fair_value_cents",
                           side_effect=lambda spot, strike, sigma, t, d: fairs[strike]), \
         mock.patch.object(bot, "window_end_utc",
                           return_value=datetime.now(timezone.utc) + timedelta(days=10)):
        bot.run_cycle()
    bids, asks = {}, {}
    for o in client.created:
        if o["side"] == "yes":
            bids.setdefault(o["ticker"], []).append((o["yes_price"], o["count"]))
        else:
            asks.setdefault(o["ticker"], []).append((100 - o["no_price"], o["count"]))
    return ({t: sorted(v, reverse=True) for t, v in bids.items()},
            {t: sorted(v) for t, v in asks.items()}, client.created)


class TestMonthlyRulesCycle(unittest.TestCase):
    CHEAP, MID = 116.0, 108.0

    def _markets(self):
        return [_mk(self.CHEAP), _mk(self.MID)]

    def test_ask_floor_suppresses_cheap_asks_only_for_the_monthly_class(self):
        books = {self.CHEAP: (10, 12), self.MID: (35, 45)}
        fairs = {self.CHEAP: 12, self.MID: 40}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        b_bids, b_asks, _ = _cycle(mm.TouchMarketMaker, self._markets(), {}, books, fairs)
        self.assertEqual(b_asks[t_cheap], [(17, 5), (19, 5), (21, 5)])   # base still sells
        self.assertEqual(b_bids[t_cheap], [(7, 5), (5, 5), (3, 5)])
        m_bids, m_asks, _ = _cycle(mm.MonthlyTouchMarketMaker, self._markets(), {}, books, fairs)
        self.assertNotIn(t_cheap, m_asks)                                 # floor: no asks
        self.assertEqual(m_bids[t_cheap], [(7, 5), (5, 5), (3, 5)])       # bids untouched
        self.assertEqual(m_asks[t_mid], [(45, 5), (47, 5), (49, 5)])      # fair 40 >= 15: sells
        self.assertEqual(m_bids[t_mid], [(35, 5), (33, 5), (31, 5)])

    def test_reduce_only_when_long_past_threshold(self):
        books = {self.CHEAP: (30, 34), self.MID: (35, 45)}
        fairs = {self.CHEAP: 32, self.MID: 40}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        positions = {t_cheap: 650.0}          # event net +650 >= 600
        b_bids, b_asks, _ = _cycle(mm.TouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertIn(t_mid, b_bids)          # base keeps buying the other strike
        m_bids, m_asks, _ = _cycle(mm.MonthlyTouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertEqual(m_bids, {})          # monthly: no bids anywhere
        self.assertIn(t_mid, m_asks)          # asks (reducing) still quote
        self.assertIn(t_cheap, m_asks)

    def test_reduce_only_when_short_past_threshold(self):
        books = {self.CHEAP: (30, 34), self.MID: (35, 45)}
        fairs = {self.CHEAP: 32, self.MID: 40}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        # -650 short at fair 32 also puts $442 at risk (> $300 cap): both rules
        # agree — no asks; bids quote on both strikes.
        m_bids, m_asks, _ = _cycle(mm.MonthlyTouchMarketMaker, self._markets(),
                                   {t_cheap: -650.0}, books, fairs)
        self.assertEqual(m_asks, {})
        self.assertIn(t_mid, m_bids)

    def test_risk_cap_blocks_asks_across_the_event(self):
        books = {self.CHEAP: (15, 25), self.MID: (35, 45)}
        fairs = {self.CHEAP: 20, self.MID: 40}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        positions = {t_cheap: -400.0}         # 400 x (100-20)c = $320 >= $300 cap; net -400 < 600
        b_bids, b_asks, _ = _cycle(mm.TouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertIn(t_mid, b_asks)
        m_bids, m_asks, _ = _cycle(mm.MonthlyTouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertEqual(m_asks, {})          # every strike's sell room is zero
        self.assertIn(t_mid, m_bids)          # buying (reducing) still allowed

    def test_risk_cap_shaves_the_ladder(self):
        class TightRisk(mm.MonthlyTouchMarketMaker):
            max_event_risk_dollars = 20.0
            skew_max_cents = 0

        books = {self.CHEAP: (15, 25), self.MID: (35, 45)}
        fairs = {self.CHEAP: 20, self.MID: 40}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        _b, asks, _ = _cycle(TightRisk, self._markets(), {}, books, fairs)
        # near-money first: MID (fair 40) quotes 15 asks = $9 at risk; CHEAP
        # (fair 20, 80c/ct) then gets (20-9)/0.8 = 13 contracts, not 15.
        self.assertEqual(sum(c for _p, c in asks[t_mid]), 15)
        self.assertEqual(sum(c for _p, c in asks[t_cheap]), 13)

    def test_inventory_skew_moves_quotes_against_the_position(self):
        books = {self.CHEAP: (30, 34), self.MID: (46, 54)}
        fairs = {self.CHEAP: 32, self.MID: 50}
        t_cheap, t_mid = _mk(self.CHEAP)["ticker"], _mk(self.MID)["ticker"]
        positions = {t_cheap: 150.0}          # half of skew_full_at -> 2c skew
        b_bids, b_asks, _ = _cycle(mm.TouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertEqual(b_bids[t_mid], [(45, 5), (43, 5), (41, 5)])
        self.assertEqual(b_asks[t_mid], [(55, 5), (57, 5), (59, 5)])
        m_bids, m_asks, _ = _cycle(mm.MonthlyTouchMarketMaker, self._markets(), positions, books, fairs)
        self.assertEqual(m_bids[t_mid], [(43, 5), (41, 5), (39, 5)])   # bids back off 2c
        self.assertEqual(m_asks[t_mid], [(54, 5), (56, 5), (58, 5)])   # asks join the book's 54

    def test_flat_book_monthly_matches_base_when_fair_is_above_floor(self):
        books = {self.CHEAP: (30, 34), self.MID: (35, 45)}
        fairs = {self.CHEAP: 32, self.MID: 40}
        b = _cycle(mm.TouchMarketMaker, self._markets(), {}, books, fairs)
        m = _cycle(mm.MonthlyTouchMarketMaker, self._markets(), {}, books, fairs)
        self.assertEqual(b[:2], m[:2])

    def test_status_carries_risk_and_sigma_fields(self):
        books = {self.CHEAP: (15, 25), self.MID: (35, 45)}
        fairs = {self.CHEAP: 20, self.MID: 40}
        t_cheap = _mk(self.CHEAP)["ticker"]
        client = RulesFakeClient(self._markets(), {t_cheap: -100.0},
                                 {m["ticker"]: books[m["floor_strike"]] for m in self._markets()})
        bot = mm.MonthlyTouchMarketMaker(SOL_MAX, client, live=True)
        bot.state.sigma_daily = 0.03
        bot.state.vol_fetched_at = time.time()
        bot.state.mtd_hourly_at = time.time()
        with mock.patch.object(bot.alerter, "alert"), \
             mock.patch.object(mm, "fetch_live_price", return_value=100.0), \
             mock.patch.object(mm, "fair_value_cents",
                               side_effect=lambda spot, strike, sigma, t, d: fairs[strike]), \
             mock.patch.object(bot, "window_end_utc",
                               return_value=datetime.now(timezone.utc) + timedelta(days=10)):
            bot.run_cycle()
        self.assertAlmostEqual(bot.state.risk_short, 100 * 0.8)
        self.assertEqual(bot.state.risk_long, 0.0)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bot, "status_dir", d):
                bot.write_status(datetime.now(timezone.utc))
            import json as _json
            st = _json.load(open(os.path.join(d, f"status_{SOL_MAX.key}.json")))
        self.assertEqual(st["risk_short_dollars"], 80.0)
        self.assertIn("sigma_median", st)

if __name__ == "__main__":
    unittest.main(verbosity=2)
