#!/usr/bin/env python3
"""Unit tests for incentive_mm.py — run: python -m unittest test_incentive_mm"""

import csv
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import incentive_mm as imm

# Wall-clock hazard: during the first HOURLY_ACTIVATION_WINDOW_SECS of every
# real hour the universe-refresh gate is bypassed, so tests that rely on
# `universe_at = time.time()` to suppress a refresh go flaky for 12 minutes
# each hour (observed 2026-07-21: 4 failures at 12:10Z, green at 11:59Z).
# Neutralize globally; the activation-window tests re-enable it locally.
imm.HOURLY_ACTIVATION_WINDOW_SECS = 0


def setUpModule():
    """Sandbox all file side effects (HALT file, persisted state, status
    heartbeat) away from the production run-logs directory — a test must
    never delete a deliberately-placed live HALT file or pollute live state."""
    tmp = tempfile.mkdtemp(prefix="imm_test_")
    imm.STATUS_DIR = tmp
    imm.HALT_FILE = os.path.join(tmp, "HALT")
    imm.IncentiveMarketMaker.PERSIST_PATH = os.path.join(tmp, "imm_state.json")
    # baked-at-import file paths must ALL be redirected or run_cycle-driven
    # tests write into production run-logs (bit us 2026-07-28: gate-test dry
    # takes landed in the LIVE rain_directional_ledger.csv)
    imm.RAIN_DIR_LEDGER = os.path.join(tmp, "rain_directional_ledger.csv")
    # These paths were ALSO resolved from the live status dir at import time.
    # ORDER_JOURNAL_PATH is the dangerous one — _clean_persist() DELETES it, so
    # before 2026-07-28 every test run removed the LIVE bot's crash journal
    # (self-healing within one _save_persist fold, but a hard-kill in that
    # window would have orphaned the current wave's order ids). The other two
    # made tests READ live override/allowlist state — hermeticity, not safety.
    imm.IncentiveMarketMaker.ORDER_JOURNAL_PATH = os.path.join(
        tmp, "imm_order_journal.jsonl")
    imm.EVENT_OVERRIDES_FILE = os.path.join(tmp, "event_start_overrides.json")
    imm.EXTRA_ALLOW_FILE = os.path.join(tmp, "extra_allow_series.json")
    imm.RAIN_FAIR_FILE = os.path.join(tmp, "rain_fair_values.json")
    # Fixture series (KXGOOD, KXWIDE, ...) aren't in the production allowlist;
    # universe policy has its own dedicated tests.
    imm.ALLOWLIST_ONLY = False


def _clean_persist():
    for p in (imm.IncentiveMarketMaker.PERSIST_PATH,
              imm.IncentiveMarketMaker.ORDER_JOURNAL_PATH):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
from incentive_mm import (
    Quote, MarketMeta, PnlTracker, IncentiveMarketMaker,
    parse_event_date, trade_cutoff_utc, dollars_to_cents, market_cents,
    orderbook_levels, external_best, order_yes_book_cents, order_remaining,
    estimate_reward_share, _side_share, build_side_ladder, skewed_side_room,
    diff_orders, ladder_collateral_dollars,
)


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------
# Event-start cutoff
# ----------------------------------------------------------------------------

class TestParseEventDate(unittest.TestCase):
    def test_game_ticker(self):
        dt = parse_event_date("KXWCMENTION-26JUL11ARGSUI")
        self.assertIsNotNone(dt)
        et = dt.astimezone(imm.ET)
        self.assertEqual((et.year, et.month, et.day, et.hour), (2026, 7, 11, 0))

    def test_plain_date(self):
        dt = parse_event_date("KXLOVEISLMENTION-26JUL10")
        self.assertEqual(dt.astimezone(imm.ET).day, 10)

    def test_date_with_team_suffix(self):
        self.assertIsNotNone(parse_event_date("KXMLBMENTION-26JUL10BOSNYM"))

    def test_no_day(self):
        self.assertIsNone(parse_event_date("KXRIVN-26OCTDELIV"))

    def test_short_segments(self):
        self.assertIsNone(parse_event_date("KXMLBALLSTAR-26NL"))
        self.assertIsNone(parse_event_date("KXBOND-30"))
        self.assertIsNone(parse_event_date("KXFEATURE-26SET"))

    def test_no_segment(self):
        self.assertIsNone(parse_event_date("KXFOO"))

    def test_bad_month(self):
        self.assertIsNone(parse_event_date("KXFOO-26XXX11"))

    def test_bad_day(self):
        self.assertIsNone(parse_event_date("KXFOO-26FEB31"))


class TestTradeCutoff(unittest.TestCase):
    def test_ticker_date_only(self):
        cut = trade_cutoff_utc("KXWCMENTION-26JUL11ARGSUI", None, None)
        self.assertEqual(cut, parse_event_date("KXWCMENTION-26JUL11ARGSUI"))

    def test_occurrence_before_expiration(self):
        occ, exp = utc(2026, 10, 3), utc(2027, 1, 29)
        cut = trade_cutoff_utc("KXRIVN-26OCTDELIV", occ, exp)
        self.assertEqual(cut, occ)

    def test_occurrence_equals_expiration_ignored(self):
        occ = exp = utc(2026, 7, 26, 1)
        self.assertIsNone(trade_cutoff_utc("KXBOND-30", occ, exp))

    def test_min_of_both(self):
        occ, exp = utc(2026, 7, 12), utc(2026, 8, 1)
        cut = trade_cutoff_utc("KXWCMENTION-26JUL11ARGSUI", occ, exp)
        self.assertEqual(cut, parse_event_date("KXWCMENTION-26JUL11ARGSUI"))

    def test_none(self):
        self.assertIsNone(trade_cutoff_utc("KXBOND-30", None, None))


# ----------------------------------------------------------------------------
# Price / book helpers
# ----------------------------------------------------------------------------

class TestMarketCents(unittest.TestCase):
    def test_dollars_string(self):
        self.assertEqual(market_cents({"yes_bid_dollars": "0.4500"}, "yes_bid"), 45)

    def test_legacy_int(self):
        self.assertEqual(market_cents({"yes_bid": 45}, "yes_bid"), 45)

    def test_zero_is_absent(self):
        self.assertIsNone(market_cents({"yes_bid_dollars": "0.0000"}, "yes_bid"))

    def test_missing(self):
        self.assertIsNone(market_cents({}, "yes_bid"))

    def test_dollars_to_cents_bounds(self):
        self.assertIsNone(dollars_to_cents("1.5"))
        self.assertIsNone(dollars_to_cents("junk"))
        self.assertEqual(dollars_to_cents("0.99"), 99)


class TestOrderbook(unittest.TestCase):
    def test_fp_shape(self):
        y, n = orderbook_levels({"orderbook_fp": {
            "yes_dollars": [["0.40", "10"], ["0.45", "5.5"]],
            "no_dollars": [["0.50", "7"]]}})
        self.assertEqual(y, [[40, 10.0], [45, 5.5]])
        self.assertEqual(n, [[50, 7.0]])

    def test_legacy_shape(self):
        y, n = orderbook_levels({"orderbook": {"yes": [[40, 10]], "no": [[50, 7]]}})
        self.assertEqual(y, [[40.0, 10.0]])
        self.assertEqual(n, [[50.0, 7.0]])

    def test_external_best_plain(self):
        bid, ask = external_best([[40, 10], [45, 5]], [[50, 7]])
        self.assertEqual((bid, ask), (45, 50))

    def test_external_best_nets_own(self):
        # our 5 @45 is the whole level -> external best bid is 40
        bid, ask = external_best([[40, 10], [45, 5]], [[50, 7]],
                                 own_orders=[("bid", 45, 5.0)])
        self.assertEqual((bid, ask), (40, 50))

    def test_external_best_nets_own_ask(self):
        # our ask @50 rests on the NO book at 50; whole level ours -> no ext ask
        bid, ask = external_best([[40, 10]], [[50, 7]], own_orders=[("ask", 50, 7.0)])
        self.assertEqual((bid, ask), (40, None))

    def test_empty_sides(self):
        self.assertEqual(external_best([], []), (None, None))


class TestOrderParse(unittest.TestCase):
    def test_book_side_price_dollars(self):
        self.assertEqual(order_yes_book_cents(
            {"book_side": "bid", "price_dollars": "0.45"}), ("bid", 45))

    def test_legacy_no_price(self):
        self.assertEqual(order_yes_book_cents(
            {"side": "no", "action": "buy", "no_price": 30}), ("ask", 70))

    def test_unparseable(self):
        self.assertIsNone(order_yes_book_cents({"side": "??"}))

    def test_remaining(self):
        self.assertEqual(order_remaining({"remaining_count": 5}), 5.0)
        self.assertEqual(order_remaining({"remaining_count_fp": "3.00"}), 3.0)
        self.assertEqual(order_remaining({}), 0.0)


# ----------------------------------------------------------------------------
# Reward-share estimator
# ----------------------------------------------------------------------------

class TestSideShare(unittest.TestCase):
    def test_all_ours_at_best(self):
        share, q = _side_share([(50, 100.0)], {50: 100.0}, target=100, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 1.0)

    def test_discount_per_tick(self):
        # 100 external at 50 (w=1), our 100 at 49 (w=0.5): share = 50/150
        share, q = _side_share([(50, 100.0), (49, 100.0)], {49: 100.0},
                               target=200, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 50.0 / 150.0)

    def test_thin_book_no_qualify(self):
        share, q = _side_share([(50, 100.0)], {50: 100.0}, target=1000, df=0.5)
        self.assertEqual((share, q), (0.0, False))

    def test_walk_stops_at_target(self):
        # target reached at the second level; third level does not score
        share, q = _side_share([(50, 600.0), (49, 500.0), (48, 500.0)],
                               {48: 500.0}, target=1000, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 0.0)

    def test_best_at_99_disqualifies(self):
        share, q = _side_share([(99, 5000.0)], {}, target=100, df=0.5)
        self.assertEqual((share, q), (0.0, False))

    def test_empty(self):
        self.assertEqual(_side_share([], {}, 100, 0.5), (0.0, False))


class TestEstimateRewardShare(unittest.TestCase):
    BOOK_YES = [[48, 500.0], [49, 600.0]]     # ascending, best last
    BOOK_NO = [[50, 1200.0]]

    def test_both_sides_qualify(self):
        frac, sides = estimate_reward_share(
            self.BOOK_YES, self.BOOK_NO, [], target=1000, df=0.5, own_in_book=True)
        self.assertEqual(sides, 2)
        self.assertEqual(frac, 0.0)

    def test_overlay_dry_run(self):
        own = [("bid", 49, 100.0)]
        frac, sides = estimate_reward_share(
            self.BOOK_YES, self.BOOK_NO, own, target=1000, df=0.5, own_in_book=False)
        self.assertEqual(sides, 2)
        # yes side: 700@49(w1, ours 100) + 500@48(w.5) -> ours 100/950
        self.assertAlmostEqual(frac, (100.0 / 950.0 + 0.0) / 2)

    def test_live_own_included(self):
        book_yes = [[48, 500.0], [49, 700.0]]   # our 100 already inside 49
        frac, sides = estimate_reward_share(
            book_yes, self.BOOK_NO, [("bid", 49, 100.0)],
            target=1000, df=0.5, own_in_book=True)
        self.assertAlmostEqual(frac, (100.0 / 950.0) / 2)

    def test_one_side_qualifies(self):
        frac, sides = estimate_reward_share(
            [[49, 50.0]], self.BOOK_NO, [("ask", 50, 100.0)],
            target=1000, df=0.5, own_in_book=False)
        self.assertEqual(sides, 1)   # yes side too thin
        # no side: 1200 ext + our 100 overlay at no-price 50
        self.assertAlmostEqual(frac, 100.0 / 1300.0)

    def test_nothing_qualifies(self):
        frac, sides = estimate_reward_share([[49, 5.0]], [[50, 5.0]], [],
                                            target=1000, df=0.5, own_in_book=True)
        self.assertEqual((frac, sides), (0.0, 0))


# ----------------------------------------------------------------------------
# Ladder construction
# ----------------------------------------------------------------------------

class TestBuildSideLadder(unittest.TestCase):
    def test_bid_ladder(self):
        qs = build_side_ladder("T", "bid", 49, 51, room=35)
        self.assertEqual([(q.price_cents, q.count) for q in qs],
                         [(49, 5), (48, 10), (47, 20)])

    def test_ask_ladder(self):
        qs = build_side_ladder("T", "ask", 51, 49, room=35)
        self.assertEqual([(q.price_cents, q.count) for q in qs],
                         [(51, 5), (52, 10), (53, 20)])

    def test_room_shaves(self):
        qs = build_side_ladder("T", "bid", 49, None, room=12)
        self.assertEqual([(q.price_cents, q.count) for q in qs],
                         [(49, 5), (48, 7)])

    def test_zero_room(self):
        self.assertEqual(build_side_ladder("T", "bid", 49, None, room=0), [])

    def test_never_cross(self):
        # anchor bid at/above the opposite best gets pushed 1 below it
        qs = build_side_ladder("T", "bid", 50, 50, room=35)
        self.assertEqual(qs[0].price_cents, 49)
        qs = build_side_ladder("T", "ask", 50, 50, room=35)
        self.assertEqual(qs[0].price_cents, 51)

    def test_price_floor(self):
        # bid levels below PRICE_MIN are skipped, not clamped
        qs = build_side_ladder("T", "bid", imm.PRICE_MIN_CENTS, None, room=35)
        self.assertEqual([q.price_cents for q in qs], [imm.PRICE_MIN_CENTS])

    def test_price_ceiling(self):
        qs = build_side_ladder("T", "ask", imm.PRICE_MAX_CENTS, None, room=35)
        self.assertEqual([q.price_cents for q in qs], [imm.PRICE_MAX_CENTS])


class TestSkew(unittest.TestCase):
    def test_below_soft(self):
        self.assertEqual(skewed_side_room(35, 10, accumulating=True), 35)

    def test_soft_halves(self):
        self.assertEqual(skewed_side_room(35, imm.SKEW_SOFT_CONTRACTS, True),
                         imm.SIDE_MAX_CONTRACTS / 2.0)

    def test_hard_pulls(self):
        self.assertEqual(skewed_side_room(35, imm.SKEW_HARD_CONTRACTS, True), 0.0)

    def test_reducing_side_untouched(self):
        self.assertEqual(skewed_side_room(35, 80, accumulating=False), 35)


# ----------------------------------------------------------------------------
# Order diffing
# ----------------------------------------------------------------------------

def _resting(oid, ticker, book_side, px, remaining):
    return {"order_id": oid, "ticker": ticker, "book_side": book_side,
            "yes_price": px, "remaining_count": remaining, "status": "resting"}


class TestDiffOrders(unittest.TestCase):
    def test_exact_match_kept(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual((place, cancel), ([], []))

    def test_price_drift_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 48, 5.0)]
        place, cancel = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(place, [Quote("T", "bid", 49, 5)])

    def test_partial_fill_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 3.0)]   # was 5, partially filled
        place, cancel = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual(cancel, ["a"])

    def test_ttl_stale_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - imm.ORDER_REFRESH_SECS - 1}, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(len(place), 1)

    def test_undesired_cancelled(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel = diff_orders([], resting, {"a": now}, now)
        self.assertEqual(cancel, ["a"])

    def test_blind_preserved(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 40, 5.0)]
        place, cancel = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now}, now, preserve_tickers={"T"})
        self.assertEqual((place, cancel), ([], []))

    def test_unparseable_cancelled(self):
        now = time.time()
        place, cancel = diff_orders([], [{"order_id": "x", "ticker": "T"}], {}, now)
        self.assertEqual(cancel, ["x"])


class TestCollateral(unittest.TestCase):
    def test_two_sided(self):
        # bids at 49/48/47 x 5/10/20 + asks (NO cost) at 100-51.. etc
        d = ladder_collateral_dollars(49, 51)
        bid_cost = (49 * 5 + 48 * 10 + 47 * 20) / 100.0
        ask_cost = ((100 - 51) * 5 + (100 - 52) * 10 + (100 - 53) * 20) / 100.0
        self.assertAlmostEqual(d, bid_cost + ask_cost)

    def test_one_sided(self):
        self.assertGreater(ladder_collateral_dollars(50, None), 0)


# ----------------------------------------------------------------------------
# P&L tracker
# ----------------------------------------------------------------------------

class TestPnlTracker(unittest.TestCase):
    def test_round_trip_profit(self):
        p = PnlTracker()
        p.on_fill("T", "yes", "buy", 10, 40)
        p.on_fill("T", "yes", "sell", 10, 45)
        self.assertAlmostEqual(p.total_realized(), 10 * 5 / 100.0)
        self.assertAlmostEqual(p.pos["T"], 0.0)

    def test_no_side_conversion(self):
        # Buying NO at no-price 55 == selling YES at yes-price 45. Fills carry
        # yes_price_dollars regardless of side, so on_fill receives 45 and
        # must ONLY flip the action — inverting the price again was the
        # review-confirmed P&L corruption bug.
        p = PnlTracker()
        p.on_fill("T", "yes", "buy", 10, 40)
        p.on_fill("T", "no", "buy", 10, 45)
        self.assertAlmostEqual(p.total_realized(), 10 * 5 / 100.0)

    def test_no_side_real_fill_shape(self):
        # Mirrors a real V2 fill: side=no action=buy yes_price_dollars=0.08.
        # Economically: sold YES at 8c. If we'd shorted at 10c earlier, closing
        # buy at 8c banks 2c.
        p = PnlTracker()
        p.on_fill("T", "yes", "sell", 5, 10)
        p.on_fill("T", "no", "sell", 5, 8)   # sell NO at 92 == buy YES at 8
        self.assertAlmostEqual(p.total_realized(), 5 * 2 / 100.0)
        self.assertAlmostEqual(p.pos["T"], 0.0)

    def test_avg_cost_extension(self):
        p = PnlTracker()
        p.on_fill("T", "yes", "buy", 10, 40)
        p.on_fill("T", "yes", "buy", 10, 50)
        self.assertAlmostEqual(p.avg["T"], 45.0)

    def test_flip_through_zero(self):
        p = PnlTracker()
        p.on_fill("T", "yes", "buy", 10, 40)
        p.on_fill("T", "yes", "sell", 15, 50)
        self.assertAlmostEqual(p.total_realized(), 10 * 10 / 100.0)
        self.assertAlmostEqual(p.pos["T"], -5.0)
        self.assertAlmostEqual(p.avg["T"], 50.0)

    def test_short_side(self):
        p = PnlTracker()
        p.on_fill("T", "yes", "sell", 10, 60)
        p.on_fill("T", "yes", "buy", 10, 50)
        self.assertAlmostEqual(p.total_realized(), 10 * 10 / 100.0)


# ----------------------------------------------------------------------------
# Screens & caps (bot instance, no client)
# ----------------------------------------------------------------------------

def _meta(**kw):
    base = dict(ticker="KXFOO-99DEC31-X", event_ticker="KXFOO-99DEC31",
                series="KXFOO", dollars_per_day=10.0,
                program_end=utc(2099, 1, 1), target_size=1000.0,
                discount_factor=0.5, cutoff=None,
                close_time=utc(2099, 1, 1), mid_cents=50.0, spread_cents=2,
                volume=500.0, status="active")
    base.update(kw)
    return MarketMeta(**base)


class TestScreen(unittest.TestCase):
    def setUp(self):
        self.bot = IncentiveMarketMaker(client=None, live=False)
        self.now = datetime.now(timezone.utc)

    def test_ok(self):
        self.assertIsNone(self.bot._screen(_meta(), self.now))

    def test_cutoff(self):
        self.assertEqual(self.bot._screen(
            _meta(cutoff=self.now - timedelta(hours=1)), self.now), "cutoff")

    def test_cutoff_imminent(self):
        self.assertEqual(self.bot._screen(
            _meta(cutoff=self.now + timedelta(minutes=2)), self.now), "cutoff")

    def test_closing(self):
        self.assertEqual(self.bot._screen(
            _meta(close_time=self.now + timedelta(minutes=30)), self.now), "closing")

    def test_program_over(self):
        self.assertEqual(self.bot._screen(
            _meta(program_end=self.now - timedelta(hours=1)), self.now), "program_over")

    def test_one_sided(self):
        self.assertEqual(self.bot._screen(_meta(mid_cents=None), self.now), "one_sided")

    def test_wide(self):
        self.assertEqual(self.bot._screen(
            _meta(spread_cents=imm.MAX_JOIN_SPREAD_CENTS + 1), self.now), "wide")

    def test_extreme_mid(self):
        # Track the configured band (1..95 since the 2026-07-13 widening).
        self.assertEqual(self.bot._screen(
            _meta(mid_cents=imm.MID_BAND_HI + 1.0), self.now), "extreme_mid")
        self.assertEqual(self.bot._screen(
            _meta(mid_cents=imm.MID_BAND_LO - 0.5), self.now), "extreme_mid")
        self.assertIsNone(self.bot._screen(
            _meta(mid_cents=float(imm.MID_BAND_LO)), self.now))

    def test_no_volume_screen_removed_by_default(self):
        # Jack 2026-07-22: dead books can't fill you — rewards without P&L
        # risk. Screen inert at the default (IMM_MIN_VOLUME=0)...
        self.assertIsNone(self.bot._screen(_meta(volume=0.0), self.now))
        # ...but the env knob restores it.
        old = imm.MIN_VOLUME_CONTRACTS
        imm.MIN_VOLUME_CONTRACTS = 25
        try:
            self.assertEqual(self.bot._screen(_meta(volume=1.0), self.now),
                             "no_volume")
        finally:
            imm.MIN_VOLUME_CONTRACTS = old

    def test_payout_floor_is_total_accrual(self):
        # $0.40/day on a 5-day program clears the $1 minimum; the same rate
        # on a 1-day program does not (the old per-day floor got this wrong
        # in both directions).
        now = datetime.now(timezone.utc)
        long_meta = _meta(program_end=now + timedelta(days=5))
        short_meta = _meta(program_end=now + timedelta(days=1))
        self.assertGreaterEqual(0.40 * imm._quotable_days(long_meta, now), 1.0)
        self.assertLess(0.40 * imm._quotable_days(short_meta, now), 1.0)
        # cutoff caps the window even when the program runs longer
        capped = _meta(program_end=now + timedelta(days=5),
                       cutoff=now + timedelta(days=1))
        self.assertLess(imm._quotable_days(capped, now), 1.01)

    def test_breaker(self):
        self.bot.state.breaker_until["KXFOO-99DEC31-X"] = time.time() + 100
        self.assertEqual(self.bot._screen(_meta(), self.now), "breaker")

    def test_benched(self):
        self.bot.state.bench_until["KXFOO-99DEC31-X"] = time.time() + 100
        self.assertEqual(self.bot._screen(_meta(), self.now), "benched")

    def test_no_target(self):
        self.assertEqual(self.bot._screen(_meta(target_size=0.0), self.now), "no_target")


class TestBlocklist(unittest.TestCase):
    def test_crypto_and_weather_blocked(self):
        b = IncentiveMarketMaker._blocked
        self.assertTrue(b("KXSOLMAXMON-SOL-26JUL31-40"))
        self.assertTrue(b("KXHIGHNY-26JUL10-B90"))
        self.assertFalse(b("KXWCMENTION-26JUL11ARGSUI-VAR"))


class TestAllowlist(unittest.TestCase):
    def setUp(self):
        self._old = imm.ALLOWLIST_ONLY
        imm.ALLOWLIST_ONLY = True

    def tearDown(self):
        imm.ALLOWLIST_ONLY = self._old

    def test_mention_suffix_allowed(self):
        a = IncentiveMarketMaker._allowed
        self.assertTrue(a("KXWCMENTION-26JUL11ARGSUI-VAR"))
        self.assertTrue(a("KXLOVEISLMENTION-26JUL10-LOYA"))

    def test_crypto_series_exact(self):
        a = IncentiveMarketMaker._allowed
        self.assertTrue(a("KXBTCMAXY-26-T150"))
        self.assertTrue(a("KXCHINAUNBANBTC-26JUL08-30JAN01"))
        # yearly pairs for the newer fleet assets (2026-07-22)
        self.assertTrue(a("KXBNBMAXY-BNB-26DEC31"))
        self.assertTrue(a("KXHYPEMINY-HYPE-26DEC31"))
        self.assertTrue(a("KXZECMAXY-ZEC-26DEC31"))
        # the fleet's MONTHLY series must stay excluded (same-account STP)
        self.assertFalse(a("KXBNBMAXMON-BNB-26JUL31-64000"))
        self.assertFalse(a("KXHYPEMINMON-HYPE-26JUL31-5250"))

    def test_company_metric_series(self):
        # 2026-07-29: the whole company family is FROZEN (Jack: "stop
        # quoting COMPANY events") — _allowed now refuses what the 7/22
        # enrollment admitted. The allowlist entries remain underneath;
        # clearing IMM_FREEZE_SERIES restores them.
        a = IncentiveMarketMaker._allowed
        self.assertFalse(a("KXBA-26JULDELIV-130"))
        self.assertFalse(a("KXHOOD-26JULFUNDED-28300000"))
        self.assertFalse(a("KXCOINBASE-26JULVOL-240000000000"))
        self.assertFalse(a("KXWINGA-27FEBREST-3400"))
        self.assertFalse(a("KXSBUXSAR-26AUG02-T5.09"))
        import incentive_mm as _imm
        old = _imm.FREEZE_SERIES
        _imm.FREEZE_SERIES = frozenset()
        try:
            # un-frozen, the 7/22 allowlist contract still holds
            self.assertTrue(a("KXBA-26JULDELIV-130"))
            self.assertTrue(a("KXCHIPBURRITO-26AUG02-T9.77"))
            self.assertTrue(a("KXBKNUGGETS-26AUG02-T3.54"))
        finally:
            _imm.FREEZE_SERIES = old
        # non-ticker lookalikes still excluded
        self.assertFalse(a("KXMUSKNW-26JUL31-T950"))
        self.assertFalse(a("KXTRUTHSOCIAL-26JUL25-T240"))

    def test_econ_series(self):
        a = IncentiveMarketMaker._allowed
        for t in ("KXAAAGASD-26JUL23-4.150", "KXAAAGASW-26JUL27-4.040",
                  "KXAAAGASM-26JUL31-3.10", "KXNHSALES-26JUL24-T620000",
                  "KXUSGASCPI-26AUG12-T320", "KXSCFI-26DEC25-T1500"):
            self.assertTrue(a(t), t)
        # GPU rental family HARD-EXCLUDED (blocklisted, not merely absent) so
        # the daily auto-enroll can never pull it in
        b = IncentiveMarketMaker._blocked
        for t in ("KXH100MS-26JUL-2.750", "KXA100MAX-26DEC31-1.990",
                  "KXB200MON-26JUL31-4.360", "KXH200WS-26JUL24-6.500",
                  "KXRTX5090MS-26JUL-0.250"):
            self.assertTrue(b(t), t)
            self.assertFalse(a(t), t)
        # Rotten Tomatoes scores (undated tickers)
        self.assertTrue(a("KXRT-DOG-45"))

    def test_substring_trap_rejected(self):
        # 'HEGSETH' contains 'ETH' — exact series matching must reject it
        self.assertFalse(IncentiveMarketMaker._allowed("KXHEGSETHOUT-26APR-SEP01"))

    def test_non_family_rejected(self):
        self.assertFalse(IncentiveMarketMaker._allowed("KXBOND-30-AP"))

    def test_blocklist_beats_allowlist(self):
        # fleet series stay excluded even though programs now exist on them
        self.assertFalse(IncentiveMarketMaker._allowed("KXXRPMAXMON-XRP-26JUL31-140"))
        self.assertFalse(IncentiveMarketMaker._allowed("KXDOGEMINMON-DOGE-26JUL31-006"))
        # mlb_trading.py's series (GitHub Actions bot) — MENTION suffix would
        # allow it, blocklist must win
        self.assertFalse(IncentiveMarketMaker._allowed("KXMLBMENTION-26JUL14ALNL-GRAN"))

    def test_allowlist_off_admits_everything_unblocked(self):
        imm.ALLOWLIST_ONLY = False
        self.assertTrue(IncentiveMarketMaker._allowed("KXBOND-30-AP"))


class TestMentionParse(unittest.TestCase):
    def test_game_ticker(self):
        parsed = imm.parse_mention_game("KXMLBMENTION-26JUL12MILPIT")
        self.assertIsNotNone(parsed)
        date_utc, a, b = parsed
        self.assertEqual((a, b), ("MIL", "PIT"))
        self.assertEqual(date_utc.astimezone(imm.ET).day, 12)

    def test_wc_ticker(self):
        _d, a, b = imm.parse_mention_game("KXWCMENTION-26JUL11ARGSUI")
        self.assertEqual((a, b), ("ARG", "SUI"))

    def test_no_teams(self):
        self.assertIsNone(imm.parse_mention_game("KXLOVEISLMENTION-26JUL10"))
        self.assertIsNone(imm.parse_mention_game("KXBOND-30"))


class TestEventStartResolver(unittest.TestCase):
    MLB_JSON = {"dates": [{"games": [{
        "gameDate": "2026-07-12T22:40:00Z",
        "teams": {"away": {"team": {"abbreviation": "MIL"}},
                  "home": {"team": {"abbreviation": "PIT"}}}}]}]}
    ESPN_JSON = {"events": [{
        "date": "2026-07-11T16:00Z",
        "competitions": [{"competitors": [
            {"team": {"abbreviation": "ARG"}}, {"team": {"abbreviation": "SUI"}}]}]}]}

    def test_mlb_resolves_game_time(self):
        r = imm.EventStartResolver(http_get_json=lambda url: self.MLB_JSON)
        start = r.resolve("KXMLBMENTION", "KXMLBMENTION-26JUL12MILPIT")
        self.assertEqual(start, utc(2026, 7, 12, 22, 40))

    def test_wc_resolves_kickoff(self):
        r = imm.EventStartResolver(http_get_json=lambda url: self.ESPN_JSON)
        start = r.resolve("KXWCMENTION", "KXWCMENTION-26JUL11ARGSUI")
        self.assertEqual(start, utc(2026, 7, 11, 16, 0))

    def test_fixed_hour_series(self):
        r = imm.EventStartResolver(http_get_json=lambda url: {})
        start = r.resolve("KXLOVEISLMENTION", "KXLOVEISLMENTION-26JUL10")
        et = start.astimezone(imm.ET)
        self.assertEqual((et.hour, et.minute, et.day), (21, 0, 10))

    def test_latenight_fixed_hour(self):
        r = imm.EventStartResolver(http_get_json=lambda url: {})
        start = r.resolve("KXLATENIGHTMENTION", "KXLATENIGHTMENTION-26JUL19")
        et = start.astimezone(imm.ET)
        self.assertEqual((et.hour, et.minute, et.day), (22, 0, 19))

    WNBA_JSON = {"events": [{
        "date": "2026-07-19T17:00Z",
        "competitions": [{"competitors": [
            {"team": {"abbreviation": "DAL"}}, {"team": {"abbreviation": "LA"}}]}]}]}

    def test_wnba_resolves_tip_either_order(self):
        # Ticker blob LADAL = away+home; ESPN lists home (DAL) first — the
        # variable-length codes can't be split, so both concat orders match.
        r = imm.EventStartResolver(http_get_json=lambda url: self.WNBA_JSON)
        start = r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL19LADAL")
        self.assertEqual(start, utc(2026, 7, 19, 17, 0))

    def test_wnba_no_game_match_returns_none(self):
        r = imm.EventStartResolver(http_get_json=lambda url: self.WNBA_JSON)
        self.assertIsNone(r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL19NYIND"))

    def test_wnba_kalshi_code_longer_than_espn(self):
        # Kalshi CONN vs ESPN CON (live miss 2026-07-19): prefix match must
        # recover the game.
        j = {"events": [{
            "date": "2026-07-19T23:00Z",
            "competitions": [{"competitors": [
                {"team": {"abbreviation": "PHX", "location": "Phoenix"}},
                {"team": {"abbreviation": "CON", "location": "Connecticut"}}]}]}]}
        r = imm.EventStartResolver(http_get_json=lambda url: j)
        start = r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL19CONNPHX")
        self.assertEqual(start, utc(2026, 7, 19, 23, 0))

    def test_wnba_location_prefix_match(self):
        # Kalshi WAS vs ESPN WSH: neither is a prefix of the other, but the
        # location (WASHINGTON) rescues it.
        j = {"events": [{
            "date": "2026-07-20T00:00Z",
            "competitions": [{"competitors": [
                {"team": {"abbreviation": "WSH", "location": "Washington"}},
                {"team": {"abbreviation": "LV", "location": "Las Vegas"}}]}]}]}
        r = imm.EventStartResolver(http_get_json=lambda url: j)
        start = r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL19WASLV")
        self.assertEqual(start, utc(2026, 7, 20, 0, 0))

    NYDAL_POSTPONED = {"events": [
        {"date": "2026-07-17T01:00Z",
         "status": {"type": {"name": "STATUS_POSTPONED"}},
         "competitions": [{"competitors": [
             {"team": {"abbreviation": "DAL"}}, {"team": {"abbreviation": "NY"}}]}]},
        {"date": "2026-07-21T00:00Z",
         "status": {"type": {"name": "STATUS_SCHEDULED"}},
         "competitions": [{"competitors": [
             {"team": {"abbreviation": "DAL"}}, {"team": {"abbreviation": "NY"}}]}]}]}

    def test_wnba_postponed_uses_makeup_start(self):
        # NYDAL 7/16: original postponed, makeup 7/20 8pm ET — the cutoff must
        # come from the makeup, not the (past) original.
        r = imm.EventStartResolver(http_get_json=lambda url: self.NYDAL_POSTPONED)
        start = r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL16NYDAL")
        self.assertEqual(start, utc(2026, 7, 21, 0, 0))

    def test_wnba_postponed_unscheduled_returns_none(self):
        j = {"events": [self.NYDAL_POSTPONED["events"][0]]}
        r = imm.EventStartResolver(http_get_json=lambda url: j)
        self.assertIsNone(r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL16NYDAL"))

    def test_wnba_final_game_not_resurrected_by_next_meeting(self):
        # A FINISHED game must keep anchoring its past start even when the
        # same pair meets again inside the 14-day range window.
        j = {"events": [
            {"date": "2026-07-17T01:00Z",
             "status": {"type": {"name": "STATUS_FINAL"}},
             "competitions": [{"competitors": [
                 {"team": {"abbreviation": "DAL"}}, {"team": {"abbreviation": "NY"}}]}]},
            self.NYDAL_POSTPONED["events"][1]]}
        r = imm.EventStartResolver(http_get_json=lambda url: j)
        start = r.resolve("KXWNBAMENTION", "KXWNBAMENTION-26JUL16NYDAL")
        self.assertEqual(start, utc(2026, 7, 17, 1, 0))

    def test_api_failure_returns_none_and_caches(self):
        calls = {"n": 0}

        def boom(url):
            calls["n"] += 1
            raise RuntimeError("down")
        r = imm.EventStartResolver(http_get_json=boom)
        self.assertIsNone(r.resolve("KXMLBMENTION", "KXMLBMENTION-26JUL12MILPIT"))
        self.assertIsNone(r.resolve("KXMLBMENTION", "KXMLBMENTION-26JUL12MILPIT"))
        self.assertEqual(calls["n"], 1)   # negative-cached

    def test_no_game_match_returns_none(self):
        r = imm.EventStartResolver(http_get_json=lambda url: {"dates": [{"games": []}]})
        self.assertIsNone(r.resolve("KXMLBMENTION", "KXMLBMENTION-26JUL12MILPIT"))

    def test_unresolvable_series(self):
        r = imm.EventStartResolver(http_get_json=lambda url: {})
        self.assertIsNone(r.resolve("KXBTCMAXY", "KXBTCMAXY-26"))

    def test_override_change_is_live_not_cached(self):
        # A hot-reloaded override CHANGE must take effect immediately, not sit
        # behind the resolver's 6h cache (live bug 2026-07-23: INTC 5pm->4pm
        # ignored while the bot kept quoting past the new cutoff).
        ev = "KXEARNINGSMENTIONZZZ-26JUL23"
        r = imm.EventStartResolver(http_get_json=lambda url: {})
        t1 = imm.parse_iso_utc("2026-07-23T17:00:00-04:00")
        t2 = imm.parse_iso_utc("2026-07-23T16:00:00-04:00")
        imm.EVENT_START_OVERRIDES[ev] = t1
        try:
            self.assertEqual(r.resolve("KXEARNINGSMENTIONZZZ", ev), t1)
            imm.EVENT_START_OVERRIDES[ev] = t2          # changed mid-run
            self.assertEqual(r.resolve("KXEARNINGSMENTIONZZZ", ev), t2)
        finally:
            imm.EVENT_START_OVERRIDES.pop(ev, None)


class TestPlaceWithCaps(unittest.TestCase):
    def setUp(self):
        self.bot = IncentiveMarketMaker(client=None, live=False)
        self.now = time.time()

    def test_places_fresh(self):
        placed = self.bot.place_with_caps(
            [Quote("T", "bid", 49, 5), Quote("T", "bid", 48, 10)], [], set(), self.now)
        self.assertEqual(placed, 2)
        self.assertEqual(len(self.bot.state.sim_orders), 2)

    def test_side_cap_blocks(self):
        resting = [_resting("a", "T", "bid", 49, imm.SIDE_MAX_CONTRACTS)]
        placed = self.bot.place_with_caps([Quote("T", "bid", 48, 5)], resting,
                                          set(), self.now)
        self.assertEqual(placed, 0)

    def test_side_cap_ignores_cancelled(self):
        resting = [_resting("a", "T", "bid", 49, imm.SIDE_MAX_CONTRACTS)]
        placed = self.bot.place_with_caps([Quote("T", "bid", 48, 5)], resting,
                                          {"a"}, self.now)
        self.assertEqual(placed, 1)

    def test_level_cap_blocks(self):
        biggest = max(s for _t, s in imm.LEVELS)
        resting = [_resting("a", "T", "bid", 49, biggest)]
        placed = self.bot.place_with_caps([Quote("T", "bid", 49, 5)], resting,
                                          set(), self.now)
        self.assertEqual(placed, 0)

    def test_cycle_cap(self):
        quotes = [Quote(f"T{i}", "bid", 40, 1)
                  for i in range(imm.MAX_PLACEMENTS_PER_CYCLE + 10)]
        placed = self.bot.place_with_caps(quotes, [], set(), self.now)
        self.assertEqual(placed, imm.MAX_PLACEMENTS_PER_CYCLE)


# ----------------------------------------------------------------------------
# Fake-client integration (dry-run cycles against a canned exchange)
# ----------------------------------------------------------------------------

class FakeClient:
    """Just enough of ExchangeClient for dry-run cycles."""

    def __init__(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.programs = [
            {"market_ticker": "KXGOOD-99DEC31-A", "incentive_type": "liquidity",
             "period_reward": 7000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "paid_out": False,
             "start_date": start, "end_date": end},
            {"market_ticker": "KXWIDE-99DEC31-B", "incentive_type": "liquidity",
             "period_reward": 7000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "paid_out": False,
             "start_date": start, "end_date": end},
            {"market_ticker": "KXHIGHNY-26JUL10-B90", "incentive_type": "liquidity",
             "period_reward": 9000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "paid_out": False,
             "start_date": start, "end_date": end},
        ]
        self.markets = {
            "KXGOOD-99DEC31-A": {
                "ticker": "KXGOOD-99DEC31-A", "event_ticker": "KXGOOD-99DEC31",
                "status": "active", "close_time": far,
                "yes_bid_dollars": "0.4900", "yes_ask_dollars": "0.5100",
                "volume_fp": "500.00"},
            "KXWIDE-99DEC31-B": {
                "ticker": "KXWIDE-99DEC31-B", "event_ticker": "KXWIDE-99DEC31",
                "status": "active", "close_time": far,
                "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.9000",
                "volume_fp": "500.00"},
        }
        self.books = {
            "KXGOOD-99DEC31-A": {"orderbook_fp": {
                "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
                "no_dollars": [["0.49", "1200"]]}},   # ext ask = 51
        }
        self.positions = {}

    def get(self, path, params=None):
        assert path == "/incentive_programs"
        return {"incentive_programs": self.programs, "next_cursor": None}

    def get_markets(self, **kw):
        wanted = (kw.get("tickers") or "").split(",")
        return {"markets": [self.markets[t] for t in wanted if t in self.markets]}

    def get_orderbook(self, ticker, depth=None):
        if ticker not in self.books:
            raise RuntimeError("no book")
        return self.books[ticker]

    def get_positions(self, **kw):
        mp = [{"ticker": t, "position": p} for t, p in self.positions.items()]
        return {"market_positions": mp, "cursor": None}

    def get_fills(self, **kw):
        self.fills_reads = getattr(self, "fills_reads", 0) + 1
        return {"fills": list(getattr(self, "fills", [])), "cursor": None}

    def get_orders(self, **kw):
        return {"orders": [], "cursor": None}

    def get_order(self, order_id):
        return {"order": dict(getattr(self, "order_lookup", {}).get(order_id) or {})}

    def get_market(self, ticker):
        return {"market": dict(self.markets.get(ticker) or {})}

    def create_order(self, **kw):
        self.created = getattr(self, "created", [])
        self.created.append(kw)
        return {"order": {"order_id": f"real-{len(self.created)}"}}

    def cancel_order(self, order_id):
        self.cancelled = getattr(self, "cancelled", [])
        self.cancelled.append(order_id)
        return {}


class TestDryRunCycle(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        return bot

    def test_selection_and_quotes(self):
        bot = self._bot()
        bot.run_cycle()
        # KXHIGH blocked, KXWIDE screened (80c spread), KXGOOD quoted
        self.assertEqual(list(bot.state.selected), ["KXGOOD-99DEC31-A"])
        quotes = sorted((o["book_side"], o["yes_price"], o["remaining_count"])
                        for o in bot.state.sim_orders.values())
        self.assertEqual(quotes, sorted([
            ("bid", 49, 5.0), ("bid", 48, 10.0), ("bid", 47, 20.0),
            ("ask", 51, 5.0), ("ask", 52, 10.0), ("ask", 53, 20.0)]))

    def test_stable_book_no_churn(self):
        bot = self._bot()
        bot.run_cycle()
        ids1 = set(bot.state.sim_orders)
        bot.run_cycle()
        self.assertEqual(set(bot.state.sim_orders), ids1)

    def test_mid_move_breaker(self):
        bot = self._bot()
        old = imm.BREAKERS_ENABLED
        imm.BREAKERS_ENABLED = True
        try:
            bot.run_cycle()
            self.assertTrue(bot.state.sim_orders)
            bot.client.books["KXGOOD-99DEC31-A"] = {"orderbook_fp": {
                "yes_dollars": [["0.68", "500"], ["0.69", "600"]],
                "no_dollars": [["0.29", "1200"]]}}      # mid 50 -> 70
            bot.run_cycle()
            self.assertEqual(bot.state.sim_orders, {})
            self.assertGreater(bot.state.breaker_until.get("KXGOOD-99DEC31-A", 0),
                               time.time())
        finally:
            imm.BREAKERS_ENABLED = old

    def test_mid_move_no_stand_down_when_breakers_removed(self):
        # Jack 2026-07-21: breakers removed by default — a mid gap must NOT
        # stand the market down; the ladder just reprices to the new mid.
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        bot.client.books["KXGOOD-99DEC31-A"] = {"orderbook_fp": {
            "yes_dollars": [["0.68", "500"], ["0.69", "600"]],
            "no_dollars": [["0.29", "1200"]]}}      # mid 50 -> 70
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)       # still quoting (repriced)
        self.assertEqual(bot.state.breaker_until, {})

    def test_fill_burst_breaker(self):
        bot = self._bot()
        old = imm.BREAKERS_ENABLED
        imm.BREAKERS_ENABLED = True
        try:
            bot.run_cycle()
            # a sweep of OUR ladder: own book and account move together
            bot.client.positions["KXGOOD-99DEC31-A"] = imm.FILL_BURST_CONTRACTS + 5
            bot.pnl.pos["KXGOOD-99DEC31-A"] = imm.FILL_BURST_CONTRACTS + 5
            bot.run_cycle()
            self.assertEqual(bot.state.sim_orders, {})
            self.assertGreater(bot.state.breaker_until.get("KXGOOD-99DEC31-A", 0),
                               time.time())
        finally:
            imm.BREAKERS_ENABLED = old

    def test_position_skew_hard_pulls_bids(self):
        bot = self._bot()
        bot.client.positions["KXGOOD-99DEC31-A"] = imm.SKEW_HARD_CONTRACTS
        bot.pnl.pos["KXGOOD-99DEC31-A"] = imm.SKEW_HARD_CONTRACTS   # ours, not manual
        bot.run_cycle()
        sides = {o["book_side"] for o in bot.state.sim_orders.values()}
        self.assertEqual(sides, {"ask"})

    def test_reduce_only_tail(self):
        """A market we hold but no longer select gets reduce-only asks <= |pos|."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.state.universe_at = time.time()          # skip refresh: nothing selected
        bot.state.managed_extra[t] = _meta(ticker=t, event_ticker="KXGOOD-99DEC31")
        bot.client.positions[t] = 10
        bot.pnl.pos[t] = 10                          # our inventory, not manual
        bot.run_cycle()
        orders = list(bot.state.sim_orders.values())
        self.assertTrue(orders)
        self.assertTrue(all(o["book_side"] == "ask" for o in orders))
        self.assertLessEqual(sum(o["remaining_count"] for o in orders), 10)

    def test_halt_file(self):
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        import os
        os.makedirs(imm.STATUS_DIR, exist_ok=True)
        with open(imm.HALT_FILE, "w") as f:
            f.write("halt")
        try:
            bot.run_cycle()
            self.assertEqual(bot.state.sim_orders, {})
        finally:
            os.remove(imm.HALT_FILE)

    def test_event_cap_respected(self):
        """Positions near the event cap shrink new buy room to the remainder."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.client.positions[t] = imm.MAX_EVENT_CONTRACTS - 10   # 490 of 500 used
        bot.pnl.pos[t] = imm.MAX_EVENT_CONTRACTS - 10            # ours, not manual
        # skew would zero the bid side long before, so relax it for this test
        old_soft, old_hard = imm.SKEW_SOFT_CONTRACTS, imm.SKEW_HARD_CONTRACTS
        imm.SKEW_SOFT_CONTRACTS = imm.SKEW_HARD_CONTRACTS = 10 ** 9
        old_pos = imm.MAX_POSITION_CONTRACTS
        imm.MAX_POSITION_CONTRACTS = 10 ** 9
        try:
            bot.run_cycle()
            bids = sum(o["remaining_count"] for o in bot.state.sim_orders.values()
                       if o["book_side"] == "bid")
            self.assertLessEqual(bids, 10)
        finally:
            imm.SKEW_SOFT_CONTRACTS, imm.SKEW_HARD_CONTRACTS = old_soft, old_hard
            imm.MAX_POSITION_CONTRACTS = old_pos


class TestYieldRanking(unittest.TestCase):
    def test_emptier_touch_outranks_bigger_pool(self):
        """Same-$ pools: the market where our ladder owns more of the walk wins
        the slot when only one fits (incentive per contract-minute objective)."""
        _clean_persist()
        client = FakeClient()
        client.programs.append(dict(client.programs[0],
                                    market_ticker="KXCROWDED-99DEC31-C"))
        client.markets["KXCROWDED-99DEC31-C"] = dict(
            client.markets["KXGOOD-99DEC31-A"], ticker="KXCROWDED-99DEC31-C",
            event_ticker="KXCROWDED-99DEC31")
        # crowded: 5000 already at each best -> our 5-lot is a drop in the walk
        client.books["KXCROWDED-99DEC31-C"] = {"orderbook_fp": {
            "yes_dollars": [["0.49", "5000"]], "no_dollars": [["0.49", "5000"]]}}
        bot = IncentiveMarketMaker(client=client, live=False)
        old = imm.MAX_MARKETS
        imm.MAX_MARKETS = 1
        try:
            bot.run_cycle()
            self.assertEqual(list(bot.state.selected), ["KXGOOD-99DEC31-A"])
            good = bot.state.selected["KXGOOD-99DEC31-A"]
            self.assertGreater(good.yield_per_contract, 0)
        finally:
            imm.MAX_MARKETS = old


class TestContractMinutes(unittest.TestCase):
    def test_accrues_resting_contract_minutes(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()                                   # 70 contracts resting
        bot.state.reward_accrue_at = time.time() - 60     # pretend 1 min passed
        bot.run_cycle()
        self.assertGreater(bot.state.contract_minutes_today, 55)
        self.assertLess(bot.state.contract_minutes_today, 85)


class TestMentionWindowPolicy(unittest.TestCase):
    def test_tournament_wide_mention_screened(self):
        """Mention market with no derivable event window (KXWCMENTION-
        MENWORLDCUP: broadcasts already live daily) must never be quoted."""
        bot = IncentiveMarketMaker(client=None, live=False)
        meta = _meta(ticker="KXWCMENTION-MENWORLDCUP-CAPT",
                     event_ticker="KXWCMENTION-MENWORLDCUP",
                     series="KXWCMENTION", cutoff=None)
        self.assertEqual(bot._screen(meta, datetime.now(timezone.utc)),
                         "no_event_window")

    def test_non_mention_without_cutoff_ok(self):
        bot = IncentiveMarketMaker(client=None, live=False)
        meta = _meta(series="KXBTCVSGOLD", cutoff=None)
        self.assertIsNone(bot._screen(meta, datetime.now(timezone.utc)))

    def test_same_day_game_quotable_until_kickoff(self):
        """A mention market whose game is TODAY must survive the pre-filter and
        get an intraday cutoff from the resolver (game-day morning rent)."""
        _clean_persist()
        now = datetime.now(timezone.utc)
        et_now = now.astimezone(imm.ET)
        seg = et_now.strftime("%y%b%d").upper() + "ARGSUI"
        t = f"KXWCMENTION-{seg}-WALK"
        kickoff = now + timedelta(hours=3)
        client = FakeClient()
        client.programs = [dict(client.programs[0], market_ticker=t)]
        client.markets = {t: dict(client.markets["KXGOOD-99DEC31-A"], ticker=t,
                                  event_ticker=f"KXWCMENTION-{seg}")}
        client.books = {t: {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}}
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.resolver = imm.EventStartResolver(http_get_json=lambda url: {
            "events": [{
                "date": kickoff.strftime("%Y-%m-%dT%H:%MZ"),
                "competitions": [{"competitors": [
                    {"team": {"abbreviation": "ARG"}},
                    {"team": {"abbreviation": "SUI"}}]}]}]})
        # subject under test is the CUTOFF wiring; the fixture pool is tiny and
        # would legitimately fall below the $1 total-accrual floor in a 3h window
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 0.0
        try:
            bot.run_cycle()
            self.assertIn(t, bot.state.selected)
            self.assertTrue(bot.state.sim_orders)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_postponed_game_quotable_until_makeup(self):
        """A WNBA game postponed past its ticker date (NYDAL 7/16 -> makeup
        7/20 while programs kept paying) must survive the 24h ticker pre-drop
        and quote until the makeup start."""
        _clean_persist()
        now = datetime.now(timezone.utc)
        et_stale = (now - timedelta(days=3)).astimezone(imm.ET)
        seg = et_stale.strftime("%y%b%d").upper() + "NYDAL"
        t = f"KXWNBAMENTION-{seg}-ROOK"
        makeup = now + timedelta(hours=30)
        client = FakeClient()
        client.programs = [dict(client.programs[0], market_ticker=t)]
        client.markets = {t: dict(client.markets["KXGOOD-99DEC31-A"], ticker=t,
                                  event_ticker=f"KXWNBAMENTION-{seg}")}
        client.books = {t: {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}}
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.resolver = imm.EventStartResolver(http_get_json=lambda url: {
            "events": [
                {"date": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%MZ"),
                 "status": {"type": {"name": "STATUS_POSTPONED"}},
                 "competitions": [{"competitors": [
                     {"team": {"abbreviation": "DAL"}},
                     {"team": {"abbreviation": "NY"}}]}]},
                {"date": makeup.strftime("%Y-%m-%dT%H:%MZ"),
                 "status": {"type": {"name": "STATUS_SCHEDULED"}},
                 "competitions": [{"competitors": [
                     {"team": {"abbreviation": "DAL"}},
                     {"team": {"abbreviation": "NY"}}]}]}]})
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 0.0   # cutoff wiring is the subject here
        try:
            bot.run_cycle()
            self.assertIn(t, bot.state.selected)
            self.assertTrue(bot.state.sim_orders)
            cutoff = bot.state.selected[t].cutoff
            self.assertIsNotNone(cutoff)
            self.assertGreater(cutoff, now)   # quoting NOW, not killed by ticker date
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor


class TestOverrideBuffer(unittest.TestCase):
    """Earnings call / disclosure-release overrides cut off OVERRIDE_BUFFER_MIN
    before the event (Jack: all orders expire 10 min before the release), vs
    the 30-min game buffer; and orders are expiration-capped at that cutoff."""

    def test_disclosure_release_cutoff_and_order_expiry(self):
        _clean_persist()
        now = datetime.now(timezone.utc)
        release = now + timedelta(hours=3)          # e.g. today's 4pm release
        ev = "KXINTC-26JULHEAD"
        t = f"{ev}-82000"
        client = FakeClient()
        client.programs = [dict(client.programs[0], market_ticker=t)]
        client.markets = {t: dict(client.markets["KXGOOD-99DEC31-A"], ticker=t,
                                  event_ticker=ev)}
        client.books = {t: {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}}
        imm.EVENT_START_OVERRIDES[ev] = release
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 0.0
        # KXINTC is company family — since the 7/28 no-new gate and the
        # 7/29 freeze a FRESH company market can't admit; this test is
        # about cutoff mechanics, so exempt the fixture from both.
        old_no_new = imm.NO_NEW_SERIES
        imm.NO_NEW_SERIES = frozenset()
        old_freeze = imm.FREEZE_SERIES
        imm.FREEZE_SERIES = frozenset()
        try:
            bot = IncentiveMarketMaker(client=client, live=False)
            bot.run_cycle()
            self.assertIn(t, bot.state.selected)
            cutoff = bot.state.selected[t].cutoff
            # cutoff = release - OVERRIDE_BUFFER_MIN (10), NOT the 30-min game buffer
            self.assertAlmostEqual(
                (release - cutoff).total_seconds() / 60.0,
                imm.OVERRIDE_BUFFER_MIN, delta=0.5)
            # every order is expiration-capped at that cutoff (exchange-side)
            for o in bot.state.sim_orders.values():
                self.assertLessEqual(o["expire_at"], cutoff.timestamp() + 1)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor
            imm.NO_NEW_SERIES = old_no_new
            imm.FREEZE_SERIES = old_freeze
            imm.EVENT_START_OVERRIDES.pop(ev, None)


class TestHaltRestartContinuity(unittest.TestCase):
    """Restarts must not reset the loss clock or clear an active halt."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_pnl_carry_and_halt_survive_restart(self):
        bot = self._bot()
        bot.state.pnl_today_last = -700.0
        bot.state.halted_until = time.time() + 3600
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertAlmostEqual(bot2.state.pnl_carry, -700.0)
        self.assertGreater(bot2.state.halted_until, time.time())

    def test_carry_discarded_on_new_roll_day(self):
        bot = self._bot()
        bot.state.pnl_today_last = -700.0
        bot.state.halted_until = time.time() + 3600
        bot._save_persist()
        with open(bot.PERSIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data["halt_day_key"] = "1999-01-01"      # stale day
        with open(bot.PERSIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        bot3 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertEqual(bot3.state.pnl_carry, 0.0)
        self.assertEqual(bot3.state.halted_until, 0.0)

    def test_carry_feeds_the_halt(self):
        bot = self._bot()
        bot.state.pnl_carry = -(imm.DAILY_LOSS_LIMIT + 5)   # carried loss
        bot.state.universe_at = time.time()
        bot.run_cycle()                       # fresh measurement ~0 + carry
        self.assertGreater(bot.state.halted_until, time.time())


class TestBalanceFloor(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_anchor_then_halt_on_drop(self):
        bot = self._bot()
        now = datetime.now(timezone.utc)
        bot.client.get_balance = lambda: {"balance_dollars": "6000.00"}
        self.assertFalse(bot._check_balance_floor(now))       # anchors
        self.assertEqual(bot.state.balance_day_start, 6000.0)
        bot.client.get_balance = lambda: {"balance_dollars":
                                          str(6000.0 - imm.BALANCE_DROP_HALT)}
        self.assertTrue(bot._check_balance_floor(now))        # halts
        self.assertGreater(bot.state.halted_until, time.time())
        self.assertTrue(any(c == "balance_floor" for c, _m in bot.alerter.today))

    def test_small_drop_and_read_failure_are_fine(self):
        bot = self._bot()
        now = datetime.now(timezone.utc)
        bot.client.get_balance = lambda: {"balance_dollars": "6000.00"}
        bot._check_balance_floor(now)
        bot.client.get_balance = lambda: {"balance_dollars": "5500.00"}
        self.assertFalse(bot._check_balance_floor(now))
        def boom():
            raise RuntimeError("api down")
        bot.client.get_balance = boom
        self.assertFalse(bot._check_balance_floor(now))
        self.assertEqual(bot.state.halted_until, 0.0)


class TestWatchdog(unittest.TestCase):
    def test_selected_but_not_resting_pages(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.state.universe_at = time.time()          # freeze selection
        # 12 selected markets whose books are unquotable -> nothing rests
        for i in range(12):
            t = f"KXGHOST-99DEC31-S{i}"
            bot.state.selected[t] = _meta(ticker=t, event_ticker="KXGHOST-99DEC31")
        for _ in range(imm.WATCHDOG_CYCLES):
            bot.run_cycle()
        self.assertTrue(any(c == "watchdog" for c, _m in bot.alerter.today))


class TestSeriesAutoEnroll(unittest.TestCase):
    """Daily series classifier + the bot's hot-reloaded extra-allow file."""

    def _classify(self, series, sample):
        import imm_earnings_overrides as ieo
        return ieo.classify_series(series, sample)

    def test_classifier(self):
        self.assertEqual(self._classify("KXTVMENTION", "KXTVMENTION-26AUG01-FOO")[0],
                         "enroll")
        self.assertEqual(self._classify("KXLTCMAXY", "KXLTCMAXY-LTC-26DEC31")[0],
                         "enroll")
        # company shapes: REVIEW since the 7/28 no-new company rule (they
        # were "enroll" until then — the classifier is the front door the
        # NO_NEW_SERIES gate can't see)
        self.assertEqual(self._classify("KXTSLA", "KXTSLA-26AUGDELIV-450000")[0],
                         "review")
        # fleet monthly / blocklist / ambiguous stay out
        self.assertEqual(self._classify("KXLTCMAXMON", "KXLTCMAXMON-LTC-26JUL31")[0],
                         "skip")
        self.assertEqual(self._classify("KXHURCAT", "KXHURCAT-26FAUSTO-T1")[0],
                         "review")
        self.assertEqual(self._classify("KXJOINCLUB",
                                        "KXJOINCLUB-26OCT02JALVAREZ-RMA")[0],
                         "review")   # day+name tail = person/event, not a metric
        self.assertEqual(self._classify("KXCEARAGOV", "KXCEARAGOV-26OCT04-EFRE")[0],
                         "review")
        self.assertEqual(self._classify("KXTEMPMIAH", "KXTEMPMIAH-26JUL2312-T88.99")[0],
                         "review")
        # GPU rental family is blocklisted -> classifier skips (never enrolls)
        self.assertEqual(self._classify("KXH100MS", "KXH100MS-26JUL-2.750")[0],
                         "skip")
        self.assertEqual(self._classify("KXA100MAX", "KXA100MAX-26DEC31-1.990")[0],
                         "skip")

    def test_extra_allow_file_reload_and_safety(self):
        old_path = imm.EXTRA_ALLOW_FILE
        tmp = os.path.join(os.path.dirname(imm.EXTRA_ALLOW_FILE),
                           "test_extra_allow.json")
        imm.EXTRA_ALLOW_FILE = tmp
        imm._extra_allow_state["mtime"] = 0.0
        snapshot = set(imm.EXTRA_ALLOW_SERIES)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"series": ["KXNEWCO", "KXBTCMAXMON", "KXHIGHNY"]}, f)
            imm.load_extra_allow_series()
            self.assertIn("KXNEWCO", imm.EXTRA_ALLOW_SERIES)
            self.assertNotIn("KXBTCMAXMON", imm.EXTRA_ALLOW_SERIES)  # fleet refused
            self.assertNotIn("KXHIGHNY", imm.EXTRA_ALLOW_SERIES)     # blocklisted
            self.assertTrue(IncentiveMarketMaker._allowed("KXNEWCO-26AUGDELIV-5"))
        finally:
            imm.EXTRA_ALLOW_FILE = old_path
            imm._extra_allow_state["mtime"] = 0.0
            imm.EXTRA_ALLOW_SERIES.clear()
            imm.EXTRA_ALLOW_SERIES.update(snapshot)
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass


class TestEarningsCallTimeParse(unittest.TestCase):
    """parse_call_time against the real IR phrasings that motivated the tool
    (Alphabet / Tesla / Alaska, Jul 2026)."""

    def _parse(self, text):
        import imm_earnings_overrides as ieo
        return ieo.parse_call_time(text)

    def test_alphabet_phrasing(self):
        hit = self._parse(
            "Alphabet Inc. will hold its quarterly conference call to discuss "
            "second quarter 2026 financial results on Wednesday, July 22, at "
            "1:30pm Pacific Time (4:30pm Eastern Time).")
        self.assertIsNotNone(hit)
        dt_et, _ = hit
        self.assertEqual((dt_et.month, dt_et.day, dt_et.hour, dt_et.minute),
                         (7, 22, 16, 30))

    def test_tesla_phrasing_skips_central(self):
        hit = self._parse(
            "Tesla will host a live question and answer webcast at 4:30 p.m. "
            "Central Time / 5:30 p.m. Eastern Time on Wednesday, July 22, 2026 "
            "to discuss the results.")
        self.assertIsNotNone(hit)
        dt_et, _ = hit
        self.assertEqual((dt_et.day, dt_et.hour, dt_et.minute), (22, 17, 30))

    def test_alaska_phrasing(self):
        hit = self._parse(
            "Alaska Air Group will hold its quarterly conference call July 22, "
            "2026 to review second-quarter financial results at 11:30 a.m. EDT "
            "/ 8:30 a.m. PDT.")
        self.assertIsNotNone(hit)
        dt_et, _ = hit
        self.assertEqual((dt_et.day, dt_et.hour, dt_et.minute), (22, 11, 30))

    def test_no_false_positive_without_call_context(self):
        self.assertIsNone(self._parse(
            "The company estimated revenue of $4.30 per share for July 22."))


class TestReleaseTimeParse(unittest.TestCase):
    """parse_release_time — the earnings PRESS-RELEASE resolver for company-
    disclosure cutoffs (after-close -> 4pm ET, before-open -> 7am ET)."""

    def _parse(self, text, year=2026):
        import imm_earnings_overrides as ieo
        return ieo.parse_release_time(text, year)

    def test_intel_after_close(self):
        hit = self._parse(
            "Intel will report second-quarter financial results on Thursday, "
            "July 23, 2026, promptly after close of market. Following the "
            "report, Intel will hold a conference call at 2 p.m. PDT.")
        self.assertIsNotNone(hit)
        dt, label, _ = hit
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (7, 23, 16, 0))
        self.assertIn("close", label)

    def test_before_market_open(self):
        hit = self._parse(
            "Boeing will report second-quarter 2026 financial results before "
            "the market opens on July 29, 2026.")
        self.assertIsNotNone(hit)
        dt, label, _ = hit
        self.assertEqual((dt.month, dt.day, dt.hour), (7, 29, 7))
        self.assertIn("open", label)

    def test_stated_release_time_not_the_call(self):
        # a specific RELEASE time is used; the later call time is NOT picked
        hit = self._parse(
            "The company will release its quarterly results at 6:00 a.m. ET on "
            "August 5, 2026, and host a conference call at 8:30 a.m. ET.")
        self.assertIsNotNone(hit)
        dt, _, _ = hit
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (8, 5, 6, 0))

    def test_no_report_context_returns_none(self):
        self.assertIsNone(self._parse(
            "Shares closed after the market close up 3% on Tuesday."))


class TestNasdaqRelease(unittest.TestCase):
    """Nasdaq-calendar release resolver: after-hours->4pm ET, pre-market->7am,
    scanning forward from now (robust to Kalshi's wrong occurrence)."""

    def test_amc_and_bmo_from_calendar(self):
        import imm_earnings_overrides as ieo
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        ieo._nasdaq_cache.clear()
        for i in range(6):
            di = (now + timedelta(days=i)).astimezone(ieo.ET).date().isoformat()
            ieo._nasdaq_cache[di] = {}
        hit_date = (now + timedelta(days=2)).astimezone(ieo.ET).date().isoformat()
        ieo._nasdaq_cache[hit_date] = {"FOO": "time-after-hours",
                                       "BAR": "time-pre-market"}
        try:
            amc = ieo.nasdaq_release_datetime("FOO", now, 5)
            self.assertIsNotNone(amc)
            self.assertEqual(amc[0].astimezone(ieo.ET).hour, 16)   # 4pm ET
            bmo = ieo.nasdaq_release_datetime("BAR", now, 5)
            self.assertEqual(bmo[0].astimezone(ieo.ET).hour, 7)    # 7am ET
            self.assertIsNone(ieo.nasdaq_release_datetime("NOPE", now, 5))
        finally:
            ieo._nasdaq_cache.clear()


class TestFileEventOverrides(unittest.TestCase):
    """Hot-reloaded call-time overrides file (imm_earnings_overrides.py)."""

    def setUp(self):
        self.old_path = imm.EVENT_OVERRIDES_FILE
        self.tmp = os.path.join(os.path.dirname(imm.EVENT_OVERRIDES_FILE),
                                "test_event_overrides.json")
        imm.EVENT_OVERRIDES_FILE = self.tmp
        imm._file_override_state["mtime"] = 0.0
        imm._file_override_state["keys"] = set()
        self.snapshot = dict(imm.EVENT_START_OVERRIDES)

    def tearDown(self):
        imm.EVENT_OVERRIDES_FILE = self.old_path
        imm._file_override_state["mtime"] = 0.0
        imm._file_override_state["keys"] = set()
        imm.EVENT_START_OVERRIDES.clear()
        imm.EVENT_START_OVERRIDES.update(self.snapshot)
        try:
            os.remove(self.tmp)
        except FileNotFoundError:
            pass

    _mtime_seq = 0

    def _write(self, data):
        with open(self.tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        # Windows time.time() ticks ~15.6ms — consecutive writes in one tick
        # got identical mtimes and read as "unchanged". Force distinct mtimes.
        TestFileEventOverrides._mtime_seq += 60
        os.utime(self.tmp, (time.time(), 1_700_000_000 + self._mtime_seq))

    def test_merge_update_and_remove(self):
        self._write({"KXEARNINGSMENTIONFOO-26AUG01": "2026-08-01T16:30:00-04:00"})
        self.assertEqual(imm.load_file_event_overrides(), 1)
        self.assertIn("KXEARNINGSMENTIONFOO-26AUG01", imm.EVENT_START_OVERRIDES)
        # unchanged file -> no work
        self.assertEqual(imm.load_file_event_overrides(), 0)
        # update wins over previous FILE value
        self._write({"KXEARNINGSMENTIONFOO-26AUG01": "2026-08-01T17:00:00-04:00"})
        self.assertEqual(imm.load_file_event_overrides(), 1)
        et = imm.EVENT_START_OVERRIDES["KXEARNINGSMENTIONFOO-26AUG01"].astimezone(imm.ET)
        self.assertEqual(et.hour, 17)
        # removal from file -> forgotten
        self._write({})
        imm.load_file_event_overrides()
        self.assertNotIn("KXEARNINGSMENTIONFOO-26AUG01", imm.EVENT_START_OVERRIDES)

    def test_env_code_entry_wins(self):
        ev = "KXEARNINGSMENTIONBAR-26AUG02"
        code_dt = imm.parse_iso_utc("2026-08-02T10:00:00-04:00")
        imm.EVENT_START_OVERRIDES[ev] = code_dt
        self._write({ev: "2026-08-02T23:00:00-04:00"})
        imm.load_file_event_overrides()
        self.assertEqual(imm.EVENT_START_OVERRIDES[ev], code_dt)   # untouched


class TestTempSeriesTuning(unittest.TestCase):
    """Jack 2026-07-21: KXTEMP = 5/2/2 ladder, net cap 40, quotes only 5..90c,
    out 15 min before the reading."""

    def test_temp_ladder_and_caps(self):
        self.assertEqual(imm.series_levels("KXTEMPDCH"), [(0, 5), (1, 2), (2, 2)])
        self.assertEqual(imm.series_side_max("KXTEMPDCH"), 9)
        self.assertEqual(imm.series_max_position("KXTEMPDCH"), 50)
        self.assertEqual(imm.series_price_min("KXTEMPDCH"), 5)
        self.assertEqual(imm.series_price_max("KXTEMPDCH"), 90)
        ov = imm.series_override("KXTEMPDCH")
        self.assertEqual(ov.cutoff_from_close_min, 15)
        # non-temp series keep the globals
        self.assertEqual(imm.series_price_min("KXGOOD"), imm.PRICE_MIN_CENTS)
        self.assertEqual(imm.series_price_max("KXGOOD"), imm.PRICE_MAX_CENTS)

    def test_temp_band_trims_ladder(self):
        # bid anchor at 6c: levels at 6/5/4 -> the 4c rung is below the 5c
        # floor and must be dropped; ask anchor 89: rungs 89/90/91 -> 91 over.
        bids = imm.build_side_ladder("KXTEMPDCH-26JUL2112-T80.99", "bid",
                                     6, 10, 100)
        self.assertEqual([q.price_cents for q in bids], [6, 5])
        asks = imm.build_side_ladder("KXTEMPDCH-26JUL2112-T80.99", "ask",
                                     89, 80, 100)
        self.assertEqual([q.price_cents for q in asks], [89, 90])
        # non-temp tickers now share the same 5..90 global band (2026-07-26):
        # the 4c rung dies for them too
        bids2 = imm.build_side_ladder("KXGOOD-99DEC31-A", "bid", 6, 10, 100)
        self.assertEqual([q.price_cents for q in bids2], [6, 5])

    def test_rain_series_share_the_weather_band(self):
        # Jack 2026-07-26: rain (daily + monthly city series) uses the same
        # 5..90c band as KXTEMP — caught the bot at 2cx3c on a dead
        # KXRAINHOUM strike under the 1..95 global band.
        for s in ("KXRAIN", "KXRAINHOUM", "KXRAINSTPM"):
            self.assertEqual(imm.series_price_min(s), 5, s)
            self.assertEqual(imm.series_price_max(s), 90, s)
        # band trims the ladder exactly like temp
        bids = imm.build_side_ladder("KXRAINHOUM-26JUL-6", "bid", 6, 10, 100)
        self.assertEqual([q.price_cents for q in bids], [6, 5])
        # cutoffs/ladder stay global (band-only override)
        ov = imm.series_override("KXRAINHOUM")
        self.assertIsNone(ov.cutoff_from_close_min)
        self.assertIsNone(ov.levels)

    def test_out_of_band_top_stands_aside_entirely(self):
        # Jack 2026-07-21: if the TOP of book is outside the band, no quotes
        # at all — not even the in-band side. (Global band 1-95 here: top at
        # 96x98 is out; the sticky selection keeps the market, orders don't.)
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        self.assertTrue(bot.state.sim_orders)
        bot.client.books[t] = {"orderbook_fp": {
            "yes_dollars": [["0.96", "500"]], "no_dollars": [["0.02", "1200"]]}}
        bot.client.markets[t]["yes_bid_dollars"] = "0.9600"
        bot.client.markets[t]["yes_ask_dollars"] = "0.9800"
        bot.state.universe_at = time.time()      # no refresh: quote loop only
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})   # cancelled, standing aside
        self.assertIn(t, bot.state.selected)         # but still selected (sticky)

    def test_band_is_two_sided(self):
        # Live incident 2026-07-21: a BID at 97c filled on a likely-YES temp
        # strike — the band only floored bids and capped asks. Both bounds
        # must apply to both sides.
        bids = imm.build_side_ladder("KXTEMPDCH-26JUL2112-T80.99", "bid",
                                     97, 99, 100)     # rungs 97/96/95: all > 90
        self.assertEqual(bids, [])
        asks = imm.build_side_ladder("KXTEMPDCH-26JUL2112-T80.99", "ask",
                                     3, 1, 100)       # rungs 3/4/5: 3,4 < 5
        self.assertEqual([q.price_cents for q in asks], [5])
        # global band is 5..90 too (Jack 2026-07-26): rungs 97/96/95 all die
        bids2 = imm.build_side_ladder("KXGOOD-99DEC31-A", "bid", 97, 99, 100)
        self.assertEqual(bids2, [])
        bids3 = imm.build_side_ladder("KXGOOD-99DEC31-A", "bid", 91, 99, 100)
        self.assertEqual([q.price_cents for q in bids3], [90, 89])


class TestStickySelection(unittest.TestCase):
    """Jack 2026-07-21: Kalshi pays a market's daily accrual only above ~$1 —
    a quoted market must not be deselected by QUALITY screens mid-life, only
    by its natural end (cutoff/closing/program_over) or a safety stop."""

    T = "KXGOOD-99DEC31-A"

    def _quoting_bot(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()
        assert self.T in bot.state.selected
        return bot

    def test_quality_screen_does_not_drop_quoted_market(self):
        bot = self._quoting_bot()
        # book pins outside the mid band (mid 97 > MID_BAND_HI): a NEW market
        # would be screened extreme_mid, but a quoted one must be retained.
        bot.client.books[self.T] = {"orderbook_fp": {
            "yes_dollars": [["0.96", "500"]], "no_dollars": [["0.02", "1200"]]}}
        bot.client.markets[self.T]["yes_bid_dollars"] = "0.9600"
        bot.client.markets[self.T]["yes_ask_dollars"] = "0.9800"
        bot.state.universe_at = 0.0          # force refresh deterministically
        bot.run_cycle()
        self.assertIn(self.T, bot.state.selected)

    def test_one_sided_book_does_not_drop_quoted_market(self):
        bot = self._quoting_bot()
        bot.client.markets[self.T]["yes_bid_dollars"] = "0.0000"   # bid side gone
        bot.state.universe_at = 0.0
        bot.run_cycle()
        self.assertIn(self.T, bot.state.selected)

    def test_natural_end_still_drops(self):
        bot = self._quoting_bot()
        # close_time now inside MIN_HOURS_TO_CLOSE -> 'closing' is a death
        # reason; sticky must NOT override it.
        soon = (datetime.now(timezone.utc) + timedelta(minutes=20))
        bot.client.markets[self.T]["close_time"] = soon.strftime("%Y-%m-%dT%H:%M:%SZ")
        bot.state.universe_at = 0.0
        bot.run_cycle()
        self.assertNotIn(self.T, bot.state.selected)

    def test_hopeless_member_is_evicted(self):
        # Jack 2026-07-25 (REVERSES the 7/21 unconditional retention for this
        # one case): a member whose accrued + optimistic projection can't
        # reach the $1 min payout stops quoting mid-life — sub-$1 accrual
        # pays nothing, so riding it out is pure fill risk for zero reward.
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9        # projection can never reach
        try:
            bot._est_peak.clear()              # no stale peak credit
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_accrued_credit_retains_member(self):
        # ...but a member that already banked (most of) the $1 keeps quoting:
        # accrued + projection clears the bar even when the remaining window
        # alone can't (the 7/24 TRUMPMENTION rule — protect earned accruals
        # whenever finishing the job is plausible).
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        try:
            bot.state.accrued_est[self.T] = 2e9
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_hopeless_exit_kill_switch(self):
        # IMM_HOPELESS_EXIT=0 restores the unconditional 7/21 retention.
        bot = self._quoting_bot()
        old_floor, old_flag = imm.MIN_EST_TOTAL_DOLLARS, imm.HOPELESS_EXIT
        imm.MIN_EST_TOTAL_DOLLARS, imm.HOPELESS_EXIT = 1e9, False
        try:
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS, imm.HOPELESS_EXIT = old_floor, old_flag

    def test_override_event_exempt_from_hopeless(self):
        # Jack 2026-07-28 (tele-rally, 8/22 quoted): hand-curated override
        # windows bypass the hopeless/floor veto in BOTH membership states;
        # ordinary members keep the 7/25 eviction.
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        imm.EVENT_START_OVERRIDES["KXGOOD-99DEC31"] = utc(2099, 1, 1)
        try:
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)   # exempt, stays
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor
            imm.EVENT_START_OVERRIDES.pop("KXGOOD-99DEC31", None)

    def test_accrued_credit_admits_returning_market(self):
        # ENTRY floor credit: a NON-member with banked accrual re-enters even
        # when the remaining window alone is below the bar (the re-admitted
        # TRUMPMENTION class).
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        try:
            bot.state.accrued_est[self.T] = 2e9
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_accrual_integrates_per_market(self):
        bot = self._quoting_bot()          # cycle 1 stamps reward_accrue_at
        bot.state.reward_accrue_at -= 60   # pretend a minute passed
        bot.state.universe_at = time.time()   # quote loop only, no refresh
        bot.run_cycle()                    # cycle 2 integrates
        self.assertGreater(bot.state.accrued_est.get(self.T, 0.0), 0.0)

    def test_blocklisted_positions_are_frozen(self):
        # Jack 2026-07-25: blocklist = ZERO orders, not even reduce-only
        # wind-down (the gas retirement's wind-down fire-sold longs at 1-8c
        # into pinned books). Neither restore path may create a meta.
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        t = "KXGOOD-99DEC31-A"
        old = imm.SERIES_BLOCKLIST_PREFIXES
        imm.SERIES_BLOCKLIST_PREFIXES = tuple(old) + ("KXGOOD",)
        try:
            bot.state.known_tickers.add(t)
            bot.pnl.pos[t] = -40.0
            bot.restore_orphan_metas({t: -40.0})
            self.assertNotIn(t, bot.state.managed_extra)   # not restored
            # a pre-existing entry (blocklisted after the fact) is flushed
            bot.state.managed_extra[t] = MarketMeta(
                ticker=t, event_ticker="KXGOOD-99DEC31", series="KXGOOD",
                dollars_per_day=0.0, program_end=None, target_size=0.0,
                discount_factor=0.5, cutoff=None, close_time=None)
            bot.run_cycle()
            self.assertNotIn(t, bot.state.managed_extra)
            self.assertNotIn(t, bot.state.selected)
            self.assertFalse([o for o in bot.state.sim_orders.values()
                              if o.get("ticker") == t])
        finally:
            imm.SERIES_BLOCKLIST_PREFIXES = old

    def test_no_rent_freeze(self):
        # Jack 2026-07-26 (KXRT after its movie programs expired): a market
        # with no LIVE incentive program gets no orders — not even reduce-only
        # wind-down. Empty programmed set = failsafe open (a transient feed
        # outage must not mass-freeze the book).
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        t = "KXDEAD-99DEC31-A"
        meta = MarketMeta(
            ticker=t, event_ticker="KXDEAD-99DEC31", series="KXDEAD",
            dollars_per_day=0.0, program_end=None, target_size=0.0,
            discount_factor=0.5, cutoff=None, close_time=None)
        # programmed set known and t NOT in it -> flushed from managed_extra
        bot.state.programmed = {"KXGOOD-99DEC31-A"}
        bot.state.managed_extra[t] = meta
        bot.run_cycle()
        self.assertNotIn(t, bot.state.managed_extra)
        # ...and restore_orphan_metas refuses to recreate it
        bot.state.known_tickers.add(t)
        bot.pnl.pos[t] = -40.0
        bot.restore_orphan_metas({t: -40.0})
        self.assertNotIn(t, bot.state.managed_extra)
        # failsafe: with an EMPTY programmed set the entry survives
        # (register the market so the fixture's get_markets can serve it)
        bot.client.markets[t] = dict(
            bot.client.markets["KXGOOD-99DEC31-A"],
            ticker=t, event_ticker="KXDEAD-99DEC31")
        bot.state.programmed = set()
        bot.restore_orphan_metas({t: -40.0})
        self.assertIn(t, bot.state.managed_extra)

    def test_global_pad_qualifies_thin_side_in_estimator(self):
        # Jack 2026-07-29: pads are global — the ESTIMATOR must model them
        # on thin sides or the market reads est=0 and dies at the floor
        # before the quote loop pads it.
        import incentive_mm as _imm
        self.assertTrue(_imm.PAD_TO_TARGET_GLOBAL)   # global default ON
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        t = "KXGOOD-99DEC31-A"
        # thin both sides: 300 yes / 200 no vs target 1000 — without pads
        # nobody qualifies and est would be 0
        bot.client.books[t] = {"orderbook_fp": {
            "yes_dollars": [["0.49", "300"]], "no_dollars": [["0.49", "200"]]}}
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)
        meta = bot.state.selected[t]
        self.assertGreater(meta.est_frac, 0.0)       # padded sides qualify
        # and the quote loop actually rests the pads
        pads = [o for o in bot.state.sim_orders.values()
                if o["yes_price"] in (_imm.PAD_BID_CENTS, _imm.PAD_ASK_CENTS)]
        self.assertTrue(pads, "expected 1c/99c pad orders resting")

    def test_pads_do_not_drain_event_room(self):
        # Live bug 2026-07-29 (TRUMPMENTIONB ask-only): bid-side pads on
        # thin strikes consumed event_room_buy, zeroing room for every
        # later sibling. Two thin markets, one event: BOTH must carry bids.
        _clean_persist()
        client = FakeClient()
        base = client.markets["KXGOOD-99DEC31-A"]
        ev = "KXGOOD-99DEC31"
        t2 = f"{ev}-B"
        client.markets[t2] = dict(base, ticker=t2)
        client.programs = [dict(client.programs[0], market_ticker=t)
                           for t in (f"{ev}-A", t2)]
        thin = {"orderbook_fp": {"yes_dollars": [["0.49", "300"]],
                                 "no_dollars": [["0.49", "1200"]]}}
        client.books = {f"{ev}-A": json.loads(json.dumps(thin)),
                        t2: json.loads(json.dumps(thin))}
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.client = client
        bot.run_cycle()
        for t in (f"{ev}-A", t2):
            bids = [o for o in bot.state.sim_orders.values()
                    if o["ticker"] == t and o["yes_price"] > 1
                    and o.get("book_side") == "bid"]
            self.assertTrue(bids, f"{t} lost its YES side to pad room-drain")

    def test_freeze_series_exact_match(self):
        # Jack 2026-07-29: company freeze is EXACT-series, not prefix — no
        # KXHEGSETHOUT-via-'ETH' style swallowing of future tickers.
        import incentive_mm as _imm
        old = _imm.FREEZE_SERIES
        _imm.FREEZE_SERIES = frozenset({"KXWH"})
        try:
            self.assertTrue(IncentiveMarketMaker._blocked("KXWH-26AUGCOMP-5"))
            self.assertFalse(IncentiveMarketMaker._blocked("KXWHX-26AUGFOO-1"))
            self.assertFalse(IncentiveMarketMaker._blocked("KXGOOD-99DEC31-A"))
        finally:
            _imm.FREEZE_SERIES = old

    def test_rain_fair_exempt_rolls_to_next_day(self):
        # Jack 2026-07-28: the NEXT-ET-day rain daily always bypasses the
        # NWS gate; further-out events keep it; env extras honored.
        et_evening = utc(2026, 7, 29, 0, 30)   # 8:30pm ET Jul 28
        self.assertTrue(imm.rain_fair_exempt("KXRAIN-26JUL29", et_evening))
        self.assertFalse(imm.rain_fair_exempt("KXRAIN-26JUL30", et_evening))
        after_midnight = utc(2026, 7, 29, 4, 30)   # 12:30am ET Jul 29
        self.assertTrue(imm.rain_fair_exempt("KXRAIN-26JUL30", after_midnight))
        self.assertFalse(imm.rain_fair_exempt("KXRAIN-26JUL29", after_midnight))
        self.assertFalse(imm.rain_fair_exempt("KXOTHER-26JUL29", et_evening))
        import incentive_mm as _imm
        old = _imm.RAIN_FAIR_EXEMPT_EVENTS
        _imm.RAIN_FAIR_EXEMPT_EVENTS = frozenset({"KXRAIN-26AUG05"})
        try:
            self.assertTrue(imm.rain_fair_exempt("KXRAIN-26AUG05", et_evening))
        finally:
            _imm.RAIN_FAIR_EXEMPT_EVENTS = old

    def test_cutoff_adjustments_shared_by_both_producers(self):
        # 2026-07-29 night: the early stop must bind in EVERY cutoff
        # producer — refresh AND orphan-restore (NYC kept reduce-only
        # quoting past 6pm because restore built a raw midnight cutoff).
        c = imm.apply_series_cutoff_adjustments(
            "KXRAIN", "KXRAIN-26JUL30", imm.parse_event_date("KXRAIN-26JUL30"))
        self.assertEqual(c, utc(2026, 7, 29, 22, 0))    # 6pm ET day before
        # hard-expiry floor rides along (Love Island 8:30pm ET event day)
        c2 = imm.apply_series_cutoff_adjustments(
            "KXLOVEISLMENTION", "KXLOVEISLMENTION-26AUG02", None)
        self.assertIsNotNone(c2)
        # None stays None for plain series
        self.assertIsNone(imm.apply_series_cutoff_adjustments(
            "KXGOOD", "KXGOOD-99DEC31", None))

    def test_past_cutoff_extra_goes_silent(self):
        # a reduce-only extra whose cutoff passed must cancel, not churn
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        meta = bot.state.selected[t]
        from dataclasses import replace as _replace
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        bot.state.selected = {}
        bot.state.sticky_prev = set()
        bot.state.managed_extra[t] = _replace(meta, cutoff=past)
        bot.pnl.pos[t] = -10.0
        bot.state.universe_at = time.time()      # no refresh, quote loop only
        bot.run_cycle()
        live_orders = [o for o in bot.state.sim_orders.values()
                       if o["ticker"] == t and o.get("status") == "resting"
                       and o.get("expire_at", 0) > time.time()]
        self.assertFalse([o for o in live_orders])

    def test_rain_cutoff_6pm_day_before(self):
        # Jack 2026-07-29: rain dailies stop at 6pm ET the day BEFORE the
        # rain day (ticker-date midnight minus 360 min).
        ov = imm.series_override("KXRAIN")
        self.assertEqual(ov.cutoff_before_event_min, 360)
        td = imm.parse_event_date("KXRAIN-26JUL30")     # Jul 30 00:00 ET
        early = td - timedelta(minutes=360)             # Jul 29 18:00 ET
        self.assertEqual(early, utc(2026, 7, 29, 22, 0))
        # non-rain series unaffected
        self.assertIsNone((imm.series_override("KXLOVEISLMENTION")
                           or imm.SeriesOverride()).cutoff_before_event_min)

    def test_curated_tier_and_econ_runoff(self):
        # 2026-07-29 pm: (a) the rolling next-day rain event is CURATED —
        # floor/hopeless bypass rolls with the gate exemption (JUL30-NYC
        # was hopeless-evicted when only the gate rolled); (b) econ series
        # join the no-new run-off (SCFI-26EOY grandfathered).
        now = utc(2026, 7, 29, 18, 0)      # 2pm ET Jul 29 -> next day is JUL30
        self.assertTrue(imm.curated_event("KXRAIN-26JUL30", "KXRAIN", now))
        self.assertFalse(imm.curated_event("KXRAIN-26JUL31", "KXRAIN", now))
        self.assertFalse(imm.curated_event("KXOTHER-26JUL30", "KXOTHER", now))
        imm.EVENT_START_OVERRIDES["KXFOO-26AUG01"] = now
        try:
            self.assertTrue(imm.curated_event("KXFOO-26AUG01", "KXFOO", now))
        finally:
            imm.EVENT_START_OVERRIDES.pop("KXFOO-26AUG01", None)
        for s in ("KXSCFI", "KXNHSALES", "KXRT"):
            self.assertIn(s, imm.NO_NEW_SERIES)

    def test_no_new_series_gate(self):
        # Jack 2026-07-28: company family admits no FRESH markets; existing
        # members keep quoting (grandfathered), including across restarts.
        import incentive_mm as _imm
        old = _imm.NO_NEW_SERIES
        try:
            # fresh candidate of a gated series: not selected
            _clean_persist()
            _imm.NO_NEW_SERIES = frozenset({"KXGOOD"})
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.run_cycle()
            self.assertNotIn("KXGOOD-99DEC31-A", bot.state.selected)
            # member grandfathered: select first with gate off, then gate on
            _clean_persist()
            _imm.NO_NEW_SERIES = frozenset()
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.run_cycle()
            self.assertIn("KXGOOD-99DEC31-A", bot.state.selected)
            _imm.NO_NEW_SERIES = frozenset({"KXGOOD"})
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn("KXGOOD-99DEC31-A", bot.state.selected)   # stays
            # ...and across a restart (sticky_prev grandfathers too)
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot2.run_cycle()
            self.assertIn("KXGOOD-99DEC31-A", bot2.state.selected)
        finally:
            _imm.NO_NEW_SERIES = old

    def test_rain_directional_take(self):
        # Jack 2026-07-28: divergent rain book -> take the NWS side, 3x,
        # once per market, ledgered. Dry mode writes the ledger + dedupe
        # without an exchange call.
        _clean_persist()
        import incentive_mm as _imm
        old_ledger = _imm.RAIN_DIR_LEDGER
        _imm.RAIN_DIR_LEDGER = os.path.join(_imm.STATUS_DIR, "test_rain_dir.csv")
        try:
            try:
                os.remove(_imm.RAIN_DIR_LEDGER)
            except FileNotFoundError:
                pass
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            now_ts = time.time()
            t = "KXRAIN-26JUL29-MIN"
            # ask 33 < fair 60 - tol -> buy YES at the ask
            bot.rain_directional_take(t, 32, 33, 60.0, False, True, now_ts)
            self.assertIn(t, bot.state.rain_dir_done)
            with open(_imm.RAIN_DIR_LEDGER, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["take_side"], "yes")
            self.assertEqual(rows[0]["price_cents"], "33")
            self.assertEqual(rows[0]["contracts"], str(_imm.RAIN_DIR_SIZE))
            # dedupe: second call is a no-op
            bot.rain_directional_take(t, 32, 33, 60.0, False, True, now_ts)
            with open(_imm.RAIN_DIR_LEDGER, newline="", encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)
            # bid_bad direction: bid 14 > fair 2 + tol -> buy NO at 100-14
            t2 = "KXRAIN-26JUL29-AUS"
            bot.rain_directional_take(t2, 14, 16, 2.0, True, False, now_ts)
            with open(_imm.RAIN_DIR_LEDGER, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[1]["take_side"], "no")
            self.assertEqual(rows[1]["price_cents"], "86")
            # dedupe survives restart
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertIn(t, bot2.state.rain_dir_done)
            # 24h cap blocks further takes
            old_cap = _imm.RAIN_DIR_MAX_PER_DAY
            _imm.RAIN_DIR_MAX_PER_DAY = 2 * _imm.RAIN_DIR_SIZE
            try:
                bot.rain_directional_take("KXRAIN-26JUL29-DC", 18, 19, 29.0,
                                          False, True, now_ts)
                self.assertNotIn("KXRAIN-26JUL29-DC", bot.state.rain_dir_done)
            finally:
                _imm.RAIN_DIR_MAX_PER_DAY = old_cap
        finally:
            try:
                os.remove(_imm.RAIN_DIR_LEDGER)
            except FileNotFoundError:
                pass
            _imm.RAIN_DIR_LEDGER = old_ledger

    def test_accrued_est_survives_restart(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.state.known_tickers.add(self.T)
        bot.state.accrued_est[self.T] = 0.4321
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertAlmostEqual(bot2.state.accrued_est.get(self.T, 0.0),
                               0.4321, places=3)

    def test_budget_race_does_not_drop_quoted_market(self):
        bot = self._quoting_bot()
        old_budget = imm.COLLATERAL_BUDGET
        imm.COLLATERAL_BUDGET = 0.0              # nothing NEW can fit
        try:
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            imm.COLLATERAL_BUDGET = old_budget

    def test_peak_entry_carries_flapping_market_over_floor(self):
        # A noisy estimate that cleared the floor within the last hour keeps
        # the market entry-eligible even if THIS refresh's sample is below.
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9        # today's sample is always below
        try:
            bot._est_peak["KXGOOD-99DEC31-A"] = (2e9, time.time())  # recent peak
            bot.run_cycle()
            self.assertIn("KXGOOD-99DEC31-A", bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_est_peak_survives_restart(self):
        # The 1h peak-entry memory must persist so restarts (frequent) don't
        # wipe it and force borderline markets to re-clear the floor.
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot._est_peak["KXGOOD-99DEC31-A"] = (2.5, time.time())
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn("KXGOOD-99DEC31-A", bot2._est_peak)
        self.assertAlmostEqual(bot2._est_peak["KXGOOD-99DEC31-A"][0], 2.5, places=3)
        # and it actually carries a below-floor sample over the floor
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        try:
            bot2._est_peak["KXGOOD-99DEC31-A"] = (2e9, time.time())
            bot2.run_cycle()
            self.assertIn("KXGOOD-99DEC31-A", bot2.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_stale_peak_does_not_carry(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        try:
            bot._est_peak["KXGOOD-99DEC31-A"] = (
                2e9, time.time() - imm.EST_PEAK_TTL_SECS - 60)   # expired
            bot.run_cycle()
            self.assertNotIn("KXGOOD-99DEC31-A", bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_sticky_survives_restart(self):
        # state.selected is rebuilt live; without the persisted sticky set a
        # restart stranded every in-flight accrual (observed 2026-07-21).
        # Since the 2026-07-25 hopeless exit, restart retention additionally
        # requires the market NOT be hopeless — the persisted accrued credit
        # (also restored here) is what carries a mid-accrual market across.
        bot = self._quoting_bot()
        bot.state.accrued_est[self.T] = 2e9    # banked accrual, persists too
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn(self.T, bot2.state.sticky_prev)
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9        # would exclude it as a NEW pick
        try:
            bot2.run_cycle()
            self.assertIn(self.T, bot2.state.selected)     # retained via persist
            self.assertEqual(bot2.state.sticky_prev, set())  # consumed
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor


class TestOrderJournal(unittest.TestCase):
    """Per-order crash journal: ids appended at placement must survive a
    hard-kill (merge on load) and be folded/truncated by _save_persist."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_journaled_ids_survive_crash(self):
        bot = self._bot()
        bot._journal_order_id("oid-crash-1", 111.0)
        bot._journal_order_id("oid-crash-2", 222.0)
        # simulate hard-kill: no _save_persist; fresh process loads
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn("oid-crash-1", bot2.state.our_order_ids)
        self.assertEqual(bot2.state.our_order_ids["oid-crash-2"], 222.0)

    def test_save_folds_and_truncates_journal(self):
        bot = self._bot()
        now = time.time()
        bot._journal_order_id("oid-fold", now)
        bot.state.our_order_ids["oid-fold"] = now
        bot._save_persist()
        self.assertEqual(os.path.getsize(bot.ORDER_JOURNAL_PATH), 0)
        bot3 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn("oid-fold", bot3.state.our_order_ids)   # via main file

    def test_journal_merge_does_not_clobber_main(self):
        bot = self._bot()
        now = time.time()
        bot.state.our_order_ids["oid-a"] = now
        bot._save_persist()
        bot._journal_order_id("oid-a", 1.0)    # stale duplicate line
        bot4 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertEqual(bot4.state.our_order_ids["oid-a"], now)   # main wins


class TestHourBoundaryRefresh(unittest.TestCase):
    """Hourly programs (KXTEMP) activate at hh:00 — crossing an hour boundary
    must force a universe refresh even inside the 600s elapsed gate."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_hour_cross_forces_refresh(self):
        bot = self._bot()
        now = datetime(2099, 1, 1, 5, 2, 0, tzinfo=timezone.utc)
        bot.state.universe_at = datetime(
            2099, 1, 1, 4, 58, 0, tzinfo=timezone.utc).timestamp()  # 240s ago
        bot.refresh_universe(now, {})
        self.assertEqual(bot.state.universe_at, now.timestamp())

    def test_same_hour_inside_gate_no_refresh(self):
        bot = self._bot()
        now = datetime(2099, 1, 1, 5, 32, 0, tzinfo=timezone.utc)
        prev = datetime(2099, 1, 1, 5, 28, 0, tzinfo=timezone.utc).timestamp()
        bot.state.universe_at = prev
        bot.refresh_universe(now, {})
        self.assertEqual(bot.state.universe_at, prev)   # gate held

    def test_activation_window_refreshes_every_cycle(self):
        # Kalshi publishes hourly programs minutes AFTER hh:00: inside the
        # first HOURLY_ACTIVATION_WINDOW_SECS the 600s gate must not hold,
        # even same-hour and seconds after the last refresh.
        bot = self._bot()
        imm.HOURLY_ACTIVATION_WINDOW_SECS = 720
        try:
            now = datetime(2099, 1, 1, 5, 5, 0, tzinfo=timezone.utc)
            bot.state.universe_at = datetime(
                2099, 1, 1, 5, 3, 30, tzinfo=timezone.utc).timestamp()  # 90s ago
            bot.refresh_universe(now, {})
            self.assertEqual(bot.state.universe_at, now.timestamp())
        finally:
            imm.HOURLY_ACTIVATION_WINDOW_SECS = 0

    def test_after_activation_window_gate_holds(self):
        bot = self._bot()
        imm.HOURLY_ACTIVATION_WINDOW_SECS = 720
        try:
            now = datetime(2099, 1, 1, 5, 13, 0, tzinfo=timezone.utc)  # 780s in
            prev = datetime(2099, 1, 1, 5, 11, 0, tzinfo=timezone.utc).timestamp()
            bot.state.universe_at = prev
            bot.refresh_universe(now, {})
            self.assertEqual(bot.state.universe_at, prev)
        finally:
            imm.HOURLY_ACTIVATION_WINDOW_SECS = 0


class TestResolverCutoffWiring(unittest.TestCase):
    def test_mention_market_cutoff_from_schedule(self):
        _clean_persist()
        client = FakeClient()
        t = "KXWCMENTION-99JUL12ARGSUI-WALK"
        client.programs = [dict(client.programs[0], market_ticker=t)]
        client.markets = {t: dict(client.markets["KXGOOD-99DEC31-A"], ticker=t,
                                  event_ticker="KXWCMENTION-99JUL12ARGSUI")}
        client.books = {t: {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}}
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.resolver = imm.EventStartResolver(http_get_json=lambda url: {
            "events": [{
                "date": "2099-07-12T22:40Z",
                "competitions": [{"competitors": [
                    {"team": {"abbreviation": "ARG"}},
                    {"team": {"abbreviation": "SUI"}}]}]}]})
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)
        cutoff = bot.state.selected[t].cutoff
        # kickoff minus the 30-min buffer — NOT midnight-ET day-of
        self.assertEqual(cutoff, utc(2099, 7, 12, 22, 40)
                         - timedelta(minutes=imm.EVENT_START_BUFFER_MIN))


class TestSeriesOverrides(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(imm.series_levels("KXLOVEISLMENTION"),
                         [(0, 5), (1, 5), (2, 5)])
        self.assertEqual(imm.series_side_max("KXLOVEISLMENTION"), 15)
        self.assertEqual(imm.series_max_position("KXLOVEISLMENTION"), 50)
        # non-override series fall back to globals — mention-scaled since
        # 2026-07-28 (x1.5 ladder -> commensurate net cap)
        self.assertEqual(imm.series_levels("KXWCMENTION"), imm.LEVELS)
        self.assertEqual(imm.series_max_position("KXWCMENTION"),
                         imm.MAX_POSITION_CONTRACTS * imm.MENTION_SIZE_MULT)

    def test_loveisl_ladder_555(self):
        qs = imm.build_side_ladder("X", "bid", 49, 51, room=15,
                                   levels=imm.series_levels("KXLOVEISLMENTION"))
        self.assertEqual([(q.price_cents, q.count) for q in qs],
                         [(49, 5), (48, 5), (47, 5)])

    def test_hard_expiry_9pm_et(self):
        exp = imm.series_hard_expiry_utc("KXLOVEISLMENTION", "KXLOVEISLMENTION-26JUL12")
        et = exp.astimezone(imm.ET)
        self.assertEqual((et.hour, et.minute, et.month, et.day), (21, 0, 7, 12))

    def test_cloud_bot_mention_series_blocklisted(self):
        # cloud trading bots (GitHub Actions) own these MENTION series; the
        # allowlist suffix must not override the blocklist
        a = IncentiveMarketMaker._allowed
        self.assertFalse(a("KXMLBMENTION-26JUL14ALNL-GRAN"))    # mlb_trading
        self.assertFalse(a("KXNBAMENTION-26JUL14-XYZ"))        # nbamention_v4_4
        self.assertFalse(a("KXNCAABMENTION-26JUL14-XYZ"))      # ncaab_order_script
        self.assertFalse(a("KXHIGHNY-26JUL14-B90"))            # high_temp_trading

    def test_hard_expiry_none_for_other_series(self):
        self.assertIsNone(imm.series_hard_expiry_utc("KXWCMENTION",
                                                     "KXWCMENTION-26JUL12ABCDEF"))


class TestLoveIslandCycle(unittest.TestCase):
    def _client(self, n_markets=4, mid_bid="0.48", mid_ask="0.49",
                no_bid="0.49", yes_depth="1200", no_depth="1200"):
        _clean_persist()
        client = FakeClient()
        client.programs = []
        client.markets = {}
        client.books = {}
        # TODAY's episode (dynamic — a hardcoded date breaks the moment that
        # ET day ends: the pre-filter drops date-expired tickers). Past ~8pm
        # ET, today's 9pm cutoff is imminent, so use tomorrow's episode.
        now = datetime.now(timezone.utc)
        et_now = now.astimezone(imm.ET)
        day = et_now if et_now.hour < 20 else et_now + timedelta(days=1)
        seg = day.strftime("%y%b%d").upper()
        ev = f"KXLOVEISLMENTION-{seg}"
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(hours=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = "2099-01-01T00:00:00Z"
        for i in range(n_markets):
            t = f"{ev}-M{i}"
            client.programs.append({
                "market_ticker": t, "incentive_type": "liquidity",
                "period_reward": 3000000, "target_size_fp": "1000.00",
                "discount_factor_bps": 5000, "paid_out": False,
                "start_date": start, "end_date": end})
            client.markets[t] = {
                "ticker": t, "event_ticker": ev, "status": "active",
                "close_time": far, "open_time": start,
                "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.50",
                "volume_fp": "500.00"}
            client.books[t] = {"orderbook_fp": {
                "yes_dollars": [[mid_bid, yes_depth]],
                "no_dollars": [[no_bid, no_depth]]}}
        return client, ev

    def _use_resolver(self, bot):
        # Fixed 9pm ET start via SERIES_START_ET; Love Island buffer=0 -> cutoff
        # 9:00pm ET. Real resolver (no HTTP needed for the fixed-hour path).
        bot.resolver = imm.EventStartResolver(http_get_json=lambda url: {})

    def test_quotes_all_markets_555(self):
        client, ev = self._client(n_markets=6)
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        # every market selected (quote_all), each with a 5/5/5 x2 ladder
        self.assertEqual(len(bot.state.selected), 6)
        per_market = {}
        for o in bot.state.sim_orders.values():
            per_market.setdefault(o["ticker"], []).append(o["remaining_count"])
        for t, sizes in per_market.items():
            self.assertEqual(sorted(sizes), [5, 5, 5, 5, 5, 5])   # 3 bids + 3 asks

    def test_bypasses_max_markets(self):
        client, ev = self._client(n_markets=12)
        old = imm.MAX_MARKETS
        imm.MAX_MARKETS = 3
        try:
            bot = IncentiveMarketMaker(client=client, live=False)
            self._use_resolver(bot)
            bot.run_cycle()
            self.assertEqual(len(bot.state.selected), 12)   # all, despite cap 3
        finally:
            imm.MAX_MARKETS = old

    def test_max_position_50(self):
        client, ev = self._client(n_markets=1)
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        t = f"{ev}-M0"
        bot.pnl.pos[t] = 47          # near the 50 cap on the long side
        bot.run_cycle()
        bids = sum(o["remaining_count"] for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["book_side"] == "bid")
        self.assertLessEqual(bids, 3)    # only 3 more before hitting 50

    def test_orders_expire_by_cutoff(self):
        client, ev = self._client(n_markets=1)
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        cutoff = imm.series_hard_expiry_utc("KXLOVEISLMENTION", ev).timestamp()
        for o in bot.state.sim_orders.values():
            self.assertLessEqual(o["expire_at"], cutoff + 0.001)

    def test_effective_cutoff_is_9pm_no_buffer(self):
        """The selected meta's cutoff (what actually stops quoting) is 9:00pm ET
        — the 30-min pre-broadcast buffer is zeroed for Love Island."""
        client, ev = self._client(n_markets=1)
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        meta = bot.state.selected[f"{ev}-M0"]
        et = meta.cutoff.astimezone(imm.ET)
        self.assertEqual((et.hour, et.minute), (21, 0))

    def test_user_position_does_not_yield_loveisl(self):
        """The user holding a manual Love Island position must NOT stop the bot
        — it quotes the whole event anyway (unlike non-quote_all series)."""
        client, ev = self._client(n_markets=3)
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        client.positions[f"{ev}-M0"] = 80    # big manual position, not bot's book
        bot.run_cycle()
        self.assertEqual(len(bot.state.selected), 3)   # all still quoted
        self.assertEqual(bot.manual_events(client.positions), set())

    def test_user_order_yields_only_that_market(self):
        """A live foreign order on ONE Love Island market yields just that
        market; the rest of the event keeps quoting."""
        _clean_persist()
        client, ev = self._client(n_markets=3)
        blocked = f"{ev}-M1"
        base_get = client.get_orders

        def get_orders(**kw):
            return {"orders": [
                {"order_id": "jack1", "ticker": blocked, "status": "resting",
                 "client_order_id": "", "book_side": "bid", "yes_price": 40,
                 "remaining_count": 100}], "cursor": None}
        client.get_orders = get_orders
        bot = IncentiveMarketMaker(client=client, live=True)
        self._use_resolver(bot)
        bot.run_cycle()
        self.assertIn(blocked, bot.state.manual_standoff)
        self.assertNotIn(blocked, bot.state.selected)
        self.assertIn(f"{ev}-M0", bot.state.selected)
        self.assertIn(f"{ev}-M2", bot.state.selected)

    # ---- depth padding (pad_to_target) ----

    def test_deep_book_no_pad(self):
        client, ev = self._client(n_markets=1)          # 1200/1200 both sides
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        pads = [o for o in bot.state.sim_orders.values()
                if o["yes_price"] in (imm.PAD_BID_CENTS, imm.PAD_ASK_CENTS)]
        self.assertEqual(pads, [])

    def test_thin_bid_side_padded_at_1c(self):
        client, ev = self._client(n_markets=1, yes_depth="300", no_depth="1200")
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        t = f"{ev}-M0"
        bid_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_BID_CENTS]
        self.assertEqual(len(bid_pad), 1)
        # basis 300 + 15 near-touch -> pad up to 1000, rounded to 100s
        self.assertEqual(bid_pad[0]["remaining_count"], 700)
        # deep NO side gets no pad
        ask_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_ASK_CENTS]
        self.assertEqual(ask_pad, [])
        # near-touch ladder still present alongside the pad
        near = [o for o in bot.state.sim_orders.values()
                if o["ticker"] == t and o["book_side"] == "bid"
                and o["yes_price"] != imm.PAD_BID_CENTS]
        self.assertEqual(sorted(o["remaining_count"] for o in near), [5, 5, 5])

    def test_both_sides_thin_padded(self):
        client, ev = self._client(n_markets=1, yes_depth="200", no_depth="200")
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        t = f"{ev}-M0"
        bid_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_BID_CENTS]
        ask_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_ASK_CENTS]
        self.assertEqual(bid_pad[0]["remaining_count"], 800)   # (1000-215)->800
        self.assertEqual(ask_pad[0]["remaining_count"], 800)

    def test_pad_nets_out_own_pad_live_stable(self):
        """With our 700 pad already resting inside the book, the desired pad
        stays 700 (not 0) — no self-referential churn."""
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=True)
        yes_levels = [[48, 300.0], [1, 700.0]]   # 300 external + our 700 pad
        no_levels = [[49, 1200.0]]
        near_touch = [imm.Quote("T", "bid", 48, 5)]
        own = [("bid", imm.PAD_BID_CENTS, 700.0)]
        pads = bot._pad_quotes("T", near_touch, yes_levels, no_levels, own, 1000)
        bid_pad = [p for p in pads if p.book_side == "bid"]
        self.assertEqual(len(bid_pad), 1)
        self.assertEqual(bid_pad[0].count, 700)
        self.assertTrue(bid_pad[0].is_pad)

    def test_pad_only_on_quoted_side(self):
        """No near-touch on a side (bot not quoting it) -> no pad there."""
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        pads = bot._pad_quotes("T", [imm.Quote("T", "bid", 48, 5)],
                               [[48, 100.0]], [[49, 100.0]], [], 1000)
        self.assertEqual({p.book_side for p in pads}, {"bid"})   # no ask pad


class TestPadQuantity(unittest.TestCase):
    def test_rounds_up_to_100(self):
        self.assertEqual(imm.pad_quantity(315, 1000), 700)
        self.assertEqual(imm.pad_quantity(300, 1000), 700)
        self.assertEqual(imm.pad_quantity(901, 1000), 100)
        self.assertEqual(imm.pad_quantity(900, 1000), 100)

    def test_at_or_over_target_no_pad(self):
        self.assertEqual(imm.pad_quantity(1000, 1000), 0)
        self.assertEqual(imm.pad_quantity(1500, 1000), 0)

    def test_capped(self):
        self.assertEqual(imm.pad_quantity(0, 10 ** 9), imm.PAD_MAX_CONTRACTS)


class TestCutoffEnforcement(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_cutoff_cancels_and_abandons(self):
        bot = self._bot()
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        self.assertTrue(bot.state.sim_orders)
        bot.state.selected[t].cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        bot.state.universe_at = time.time()   # keep the mutated meta
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})
        self.assertNotIn(t, bot.state.selected)

    def test_pre_cutoff_reduce_only_flat_means_no_quotes(self):
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.run_cycle()
        bot.state.selected[t].cutoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})   # flat + reduce-only = nothing

    def test_order_expiration_capped_at_cutoff(self):
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        cutoff = time.time() + 120   # inside the TTL
        bot.state.cutoff_ts[t] = cutoff
        bot.place_order(Quote(t, "bid", 49, 5), time.time())
        (order,) = bot.state.sim_orders.values()
        self.assertLessEqual(order["expire_at"], cutoff)

    def test_place_refused_at_cutoff(self):
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.state.cutoff_ts[t] = time.time() - 1
        self.assertFalse(bot.place_order(Quote(t, "bid", 49, 5), time.time()))
        self.assertEqual(bot.state.sim_orders, {})


class TestLossHalt(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_daily_loss_halts_and_cancels(self):
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        bot.pnl.realized["X"] = -(imm.DAILY_LOSS_LIMIT + 10)
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})
        self.assertGreater(bot.state.halted_until, time.time())

    def test_baseline_prevents_permanent_rehalt(self):
        """Yesterday's -$60 must not halt today once the baseline rolled."""
        bot = self._bot()
        bot.pnl.realized["X"] = -(imm.DAILY_LOSS_LIMIT + 10)
        bot.state.realized_baseline = -(imm.DAILY_LOSS_LIMIT + 10)
        bot.run_cycle()
        self.assertEqual(bot.state.halted_until, 0.0)
        self.assertTrue(bot.state.sim_orders)

    def test_banked_profit_does_not_mask_todays_loss(self):
        bot = self._bot()
        bot.pnl.realized["X"] = 200.0 - (imm.DAILY_LOSS_LIMIT + 5)
        bot.state.realized_baseline = 200.0
        bot.state.day_baseline = 200.0       # as rolled at the last summary
        bot.run_cycle()
        self.assertGreater(bot.state.halted_until, time.time())


class TestMtmAndSettlement(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_unrealized_math(self):
        p = imm.PnlTracker()
        p.pos["L"], p.avg["L"] = 10.0, 40.0
        p.pos["S"], p.avg["S"] = -10.0, 40.0
        marks = {"L": 50.0, "S": 30.0}
        # long +10c x 10 = +$1; short: -10 x (30-40)/100 = +$1
        self.assertAlmostEqual(p.unrealized(marks), 2.0)
        self.assertAlmostEqual(p.unrealized({}), 0.0)   # unmarked -> at cost
        self.assertAlmostEqual(p.inventory_contracts(), 20.0)

    def test_settlement_loss_booked(self):
        """Own position rides to a NO settlement: the loss must reach the
        P&L tracker even though no fill ever reports it."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.state.universe_at = time.time()   # nothing selected
        bot.pnl.pos[t], bot.pnl.avg[t] = 10.0, 40.0
        bot.client.markets[t]["result"] = "no"
        bot.run_cycle()                        # account flat -> settle path
        self.assertAlmostEqual(bot.pnl.realized[t], -4.0)
        self.assertAlmostEqual(bot.pnl.pos.get(t, 0.0), 0.0)

    def test_settlement_win_booked(self):
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.state.universe_at = time.time()
        bot.pnl.pos[t], bot.pnl.avg[t] = 10.0, 40.0
        bot.client.markets[t]["result"] = "yes"
        bot.run_cycle()
        self.assertAlmostEqual(bot.pnl.realized[t], 6.0)

    def test_manual_offset_dropped_without_pnl(self):
        """Account flat, market still open: user manually closed our lot —
        drop the stale entry, book nothing."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.state.universe_at = time.time()
        bot.pnl.pos[t], bot.pnl.avg[t] = 10.0, 40.0   # result stays ""
        bot.run_cycle()
        self.assertNotIn(t, bot.pnl.pos)
        self.assertAlmostEqual(bot.pnl.total_realized(), 0.0)

    def test_mtm_loss_trips_halt(self):
        """A gapped position that has NOT settled must still trip the halt.
        Position sized off DAILY_LOSS_LIMIT so the 40c gap always clears the
        configured limit (was hardcoded 150 lots = -$60, stale once the limit
        passed $60)."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        n = float(int(imm.DAILY_LOSS_LIMIT * 4))   # 40c gap -> 1.6x the limit
        bot.state.universe_at = time.time()
        bot.client.positions[t] = n
        bot.pnl.pos[t], bot.pnl.avg[t] = n, 50.0
        bot.run_cycle()                        # mark ~50 -> baseline ~0
        self.assertEqual(bot.state.halted_until, 0.0)
        bot.client.markets[t]["yes_bid_dollars"] = "0.0900"   # gap to mid 10
        bot.client.markets[t]["yes_ask_dollars"] = "0.1100"
        bot.client.books[t] = {"orderbook_fp": {
            "yes_dollars": [["0.09", "600"]], "no_dollars": [["0.89", "1200"]]}}
        bot.run_cycle()                        # unrealized -$60 today
        self.assertGreater(bot.state.halted_until, time.time())
        self.assertEqual(bot.state.sim_orders, {})


class TestCycleLog(unittest.TestCase):
    def test_panel_row_written(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()
        path = os.path.join(
            imm.STATUS_DIR,
            f"cycle_log_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("ts,ticker,ext_bid", content)
        self.assertIn("KXGOOD-99DEC31-A", content)


class TestEventBudgetSplit(unittest.TestCase):
    def test_two_markets_one_event_share_remaining_room(self):
        _clean_persist()
        client = FakeClient()
        # second market on the SAME event as KXGOOD-99DEC31-A
        client.programs.append({
            "market_ticker": "KXGOOD-99DEC31-B", "incentive_type": "liquidity",
            "period_reward": 7000000, "target_size_fp": "1000.00",
            "discount_factor_bps": 5000, "paid_out": False,
            "start_date": client.programs[0]["start_date"],
            "end_date": client.programs[0]["end_date"]})
        client.markets["KXGOOD-99DEC31-B"] = dict(
            client.markets["KXGOOD-99DEC31-A"], ticker="KXGOOD-99DEC31-B")
        client.books["KXGOOD-99DEC31-B"] = client.books["KXGOOD-99DEC31-A"]
        bot = IncentiveMarketMaker(client=client, live=False)
        # 480 of the 500 event budget already used (all on market A, all ours)
        client.positions["KXGOOD-99DEC31-A"] = imm.MAX_EVENT_CONTRACTS - 20
        bot.pnl.pos["KXGOOD-99DEC31-A"] = imm.MAX_EVENT_CONTRACTS - 20
        old = (imm.SKEW_SOFT_CONTRACTS, imm.SKEW_HARD_CONTRACTS, imm.MAX_POSITION_CONTRACTS)
        imm.SKEW_SOFT_CONTRACTS = imm.SKEW_HARD_CONTRACTS = 10 ** 9
        imm.MAX_POSITION_CONTRACTS = 10 ** 9
        try:
            bot.run_cycle()
            bids = sum(o["remaining_count"] for o in bot.state.sim_orders.values()
                       if o["book_side"] == "bid")
            self.assertLessEqual(bids, 20)
        finally:
            (imm.SKEW_SOFT_CONTRACTS, imm.SKEW_HARD_CONTRACTS,
             imm.MAX_POSITION_CONTRACTS) = old


class TestEventLevelStandoff(unittest.TestCase):
    def test_manual_sibling_position_yields_whole_event(self):
        """User holds a manual position on market B; the bot must avoid EVERY
        market of that event, including market A it would otherwise quote."""
        _clean_persist()
        client = FakeClient()
        b = "KXGOOD-99DEC31-B"
        client.markets[b] = dict(client.markets["KXGOOD-99DEC31-A"], ticker=b)
        client.positions[b] = 50   # manual (not in bot's own book)
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.run_cycle()
        self.assertNotIn("KXGOOD-99DEC31-A", bot.state.selected)
        self.assertEqual(bot.state.sim_orders, {})

    def test_bot_own_positions_do_not_self_yield(self):
        """The bot's OWN positions across an event must not trigger the
        event-level standoff against itself."""
        _clean_persist()
        client = FakeClient()
        t = "KXGOOD-99DEC31-A"
        client.positions[t] = 20
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.pnl.pos[t] = 20   # ours
        self.assertEqual(bot.manual_events(client.positions), set())
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)

    def test_event_standoff_releases_when_manual_gone(self):
        _clean_persist()
        client = FakeClient()
        b = "KXGOOD-99DEC31-B"
        client.markets[b] = dict(client.markets["KXGOOD-99DEC31-A"], ticker=b)
        client.positions[b] = 50
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.run_cycle()
        self.assertNotIn("KXGOOD-99DEC31-A", bot.state.selected)
        client.positions.pop(b)          # user exits the event
        bot.state.universe_at = 0.0
        bot.run_cycle()
        self.assertIn("KXGOOD-99DEC31-A", bot.state.selected)

    def test_can_disable(self):
        old = imm.EVENT_LEVEL_STANDOFF
        imm.EVENT_LEVEL_STANDOFF = False
        try:
            bot = IncentiveMarketMaker(client=None, live=False)
            self.assertEqual(bot.manual_events({"KXX-1-A": 50}), set())
        finally:
            imm.EVENT_LEVEL_STANDOFF = old


class TestStpProtectsUser(unittest.TestCase):
    def test_orders_placed_with_maker_stp(self):
        """Bot orders (always post-only makers) must carry STP that yields the
        bot's resting order on a self-cross, so the user's crossing manual
        order survives."""
        _clean_persist()
        client = FakeClient()
        bot = IncentiveMarketMaker(client=client, live=True)
        bot.place_order(Quote("KXGOOD-99DEC31-A", "bid", 49, 5), time.time())
        self.assertTrue(client.created)
        self.assertEqual(client.created[0]["self_trade_prevention_type"], "maker")
        self.assertEqual(imm.STP_TYPE, "maker")


class TestDeselectionSweep(unittest.TestCase):
    def test_deselected_market_orders_swept(self):
        _clean_persist()
        client = FakeClient()
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        client.programs = []            # program gone
        bot.state.universe_at = 0.0     # force refresh
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})
        self.assertEqual(bot.state.selected, {})


class TestOneSidedBreaker(unittest.TestCase):
    def test_two_sided_going_one_sided_stands_down(self):
        _clean_persist()
        client = FakeClient()
        bot = IncentiveMarketMaker(client=client, live=False)
        old = imm.BREAKERS_ENABLED
        imm.BREAKERS_ENABLED = True
        try:
            bot.run_cycle()
            self.assertTrue(bot.state.sim_orders)
            client.books["KXGOOD-99DEC31-A"] = {"orderbook_fp": {
                "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
                "no_dollars": []}}          # everyone pulled the asks
            bot.run_cycle()
            self.assertEqual(bot.state.sim_orders, {})
            self.assertGreater(bot.state.breaker_until.get("KXGOOD-99DEC31-A", 0),
                               time.time())
        finally:
            imm.BREAKERS_ENABLED = old


class TestFillsPipeline(unittest.TestCase):
    def _live_bot(self, client):
        _clean_persist()
        return IncentiveMarketMaker(client=client, live=True)

    @staticmethod
    def _fill(fid, ts, ticker="T", side="yes", action="buy", count="5.00",
              yes="0.40", order_id="ours-1"):
        return {"fill_id": fid, "ts": ts, "ticker": ticker, "side": side,
                "action": action, "count_fp": count, "yes_price_dollars": yes,
                "order_id": order_id,
                "created_time": datetime.fromtimestamp(ts, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")}

    def test_boundary_fills_not_reprocessed(self):
        client = FakeClient()
        bot = self._live_bot(client)
        bot.state.our_order_ids["ours-1"] = time.time()
        now = int(time.time())
        client.fills = [self._fill("f1", now), self._fill("f2", now)]
        first = bot.fetch_new_fills()
        self.assertEqual(len(first), 2)
        again = bot.fetch_new_fills()   # same fills at the min_ts boundary
        self.assertEqual(again, [])

    def test_foreign_fills_filtered_by_ownership(self):
        """Manual / other-bot fills (unknown order_id) never reach our P&L —
        even on a market this bot quotes."""
        client = FakeClient()
        bot = self._live_bot(client)
        bot.state.our_order_ids["ours-1"] = time.time()
        now = int(time.time())
        client.fills = [
            self._fill("f1", now, order_id="jack-manual"),
            self._fill("f2", now, order_id="cmm-fleet-order"),
            self._fill("f3", now, order_id="ours-1"),
        ]
        got = bot.fetch_new_fills()
        self.assertEqual([f["fill_id"] for f in got], ["f3"])

    def test_v2_fill_parses_into_pnl(self):
        """End-to-end through run_cycle: count_fp + yes_price_dollars parsing."""
        client = FakeClient()
        bot = self._live_bot(client)
        t = "KXGOOD-99DEC31-A"
        bot.state.universe_at = time.time()
        bot.state.our_order_ids["ours-1"] = time.time()
        now = int(time.time())
        client.fills = [
            self._fill("f1", now - 10, ticker=t, side="yes", action="buy",
                       count="10.00", yes="0.40"),
            self._fill("f2", now - 5, ticker=t, side="yes", action="sell",
                       count="10.00", yes="0.45"),
        ]
        bot.run_cycle()
        self.assertAlmostEqual(bot.pnl.total_realized(), 10 * 5 / 100.0)
        self.assertAlmostEqual(bot.state.fills_today, 20.0)


class TestLedgerMerge(unittest.TestCase):
    def _live_bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=True)

    def test_young_unconfirmed_counts_as_resting(self):
        bot = self._live_bot()
        now = time.time()
        bot.state.ledger["o1"] = {"order_id": "o1", "ticker": "T", "book_side": "bid",
                                  "yes_price": 49, "remaining_count": 5.0,
                                  "status": "resting", "_placed_at": now,
                                  "_confirmed": False}
        merged = bot._merge_ledger([], now)
        self.assertEqual([o["order_id"] for o in merged], ["o1"])

    def test_confirmed_then_absent_is_dropped(self):
        bot = self._live_bot()
        now = time.time()
        bot.state.ledger["o1"] = {"order_id": "o1", "ticker": "T", "book_side": "bid",
                                  "yes_price": 49, "remaining_count": 5.0,
                                  "status": "resting", "_placed_at": now - 100,
                                  "_confirmed": True}
        merged = bot._merge_ledger([], now)
        self.assertEqual(merged, [])
        self.assertNotIn("o1", bot.state.ledger)

    def test_past_grace_verified_via_get_order(self):
        bot = self._live_bot()
        now = time.time()
        bot.client.order_lookup = {"o1": {"order_id": "o1", "status": "resting"}}
        bot.state.ledger["o1"] = {"order_id": "o1", "ticker": "T", "book_side": "bid",
                                  "yes_price": 49, "remaining_count": 5.0,
                                  "status": "resting",
                                  "_placed_at": now - (2 * imm.POLL_SECS + 30),
                                  "_confirmed": False}
        merged = bot._merge_ledger([], now)
        self.assertEqual([o["order_id"] for o in merged], ["o1"])
        self.assertTrue(bot.state.ledger["o1"]["_confirmed"])


class TestPlaceUncertain(unittest.TestCase):
    def test_uncertain_level_skipped_then_expires(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        now = time.time()
        bot.state.place_uncertain[("T", "bid", 49)] = now
        placed = bot.place_with_caps(
            [Quote("T", "bid", 49, 5), Quote("T", "bid", 48, 10)], [], set(), now)
        self.assertEqual(placed, 1)   # only the 48c level
        # cooldown expired -> level placeable again
        bot.state.place_uncertain[("T", "bid", 49)] = now - 2 * imm.POLL_SECS - 1
        placed = bot.place_with_caps([Quote("T", "bid", 49, 5)], [], set(), now)
        self.assertEqual(placed, 1)
        self.assertNotIn(("T", "bid", 49), bot.state.place_uncertain)


class TestPersistence(unittest.TestCase):
    def test_round_trip(self):
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.known_tickers = {"A", "B"}
            bot.state.last_fill_ts = 1234
            bot.state.seen_fill_ids = {"f1": 1234}
            bot.state.our_order_ids = {"o1": time.time()}
            bot.pnl.pos["A"] = 12.0
            bot.pnl.avg["A"] = 37.5
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertEqual(bot2.state.known_tickers, {"A", "B"})
            self.assertEqual(bot2.state.last_fill_ts, 1234)
            self.assertEqual(bot2.state.seen_fill_ids, {"f1": 1234})
            self.assertIn("o1", bot2.state.our_order_ids)
            self.assertAlmostEqual(bot2.pnl.pos["A"], 12.0)
            self.assertAlmostEqual(bot2.pnl.avg["A"], 37.5)   # MTM needs entry cost
        finally:
            _clean_persist()

    def test_stale_order_ids_pruned_on_save(self):
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.our_order_ids = {"old": time.time() - 8 * 86400,
                                       "new": time.time()}
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertEqual(set(bot2.state.our_order_ids), {"new"})
        finally:
            _clean_persist()

    def test_orphan_position_restored_reduce_only(self):
        """Restart with OUR inventory on a no-longer-selected market: meta is
        rebuilt from the market read and only the reducing side is quoted."""
        _clean_persist()
        client = FakeClient()
        bot = IncentiveMarketMaker(client=client, live=False)
        t = "KXGOOD-99DEC31-A"
        client.programs = []               # not selectable anymore
        client.positions[t] = 12
        bot.state.known_tickers = {t}      # as restored from disk
        bot.pnl.pos[t] = 12.0              # own book restored from disk too
        bot.run_cycle()
        self.assertIn(t, bot.state.managed_extra)
        orders = list(bot.state.sim_orders.values())
        self.assertTrue(orders)
        self.assertTrue(all(o["book_side"] == "ask" for o in orders))
        self.assertLessEqual(sum(o["remaining_count"] for o in orders), 12)

    def test_orphan_manual_position_not_adopted(self):
        """A MANUAL position on a once-quoted market is the user's business:
        no reduce-only management, no quotes."""
        _clean_persist()
        client = FakeClient()
        bot = IncentiveMarketMaker(client=client, live=False)
        t = "KXGOOD-99DEC31-A"
        client.programs = []
        client.positions[t] = 12           # account holds it...
        bot.state.known_tickers = {t}      # ...on a market we once quoted...
        bot.run_cycle()                    # ...but our own book is empty
        self.assertNotIn(t, bot.state.managed_extra)
        self.assertEqual(bot.state.sim_orders, {})


class TestManualStandoff(unittest.TestCase):
    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def test_manual_position_yields_market(self):
        """User trades a market the bot is quoting -> quotes cancelled,
        market deselected, standoff recorded."""
        bot = self._bot()
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        self.assertTrue(bot.state.sim_orders)
        bot.client.positions[t] = 50       # manual: our own book is still 0
        bot.run_cycle()
        self.assertEqual(bot.state.sim_orders, {})
        self.assertNotIn(t, bot.state.selected)
        self.assertIn(t, bot.state.manual_standoff)

    def test_standoff_blocks_reselection(self):
        bot = self._bot()
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        bot.client.positions[t] = 50
        bot.run_cycle()                    # yields
        bot.state.universe_at = 0.0        # force refresh
        bot.run_cycle()
        self.assertNotIn(t, bot.state.selected)
        self.assertEqual(bot.state.sim_orders, {})

    def test_release_when_manual_position_gone(self):
        bot = self._bot()
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        bot.client.positions[t] = 50
        bot.run_cycle()                    # yields
        bot.client.positions.pop(t)        # user closed the position
        bot.state.universe_at = 0.0
        bot.run_cycle()                    # refresh skips (standoff entry)...
        bot.state.universe_at = 0.0
        bot.run_cycle()                    # ...loop cleared it; next refresh reselects
        self.assertIn(t, bot.state.selected)
        self.assertTrue(bot.state.sim_orders)

    def test_own_inventory_does_not_yield(self):
        """Positions the bot earned itself are managed, not yielded."""
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.client.positions[t] = 20
        bot.pnl.pos[t] = 20
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)
        self.assertNotIn(t, bot.state.manual_standoff)
        self.assertTrue(bot.state.sim_orders)

    def test_foreign_resting_order_yields_live(self):
        """A non-imm resting order on a managed market (user's manual quote)
        triggers the standoff in live mode."""
        _clean_persist()
        client = FakeClient()
        t = "KXGOOD-99DEC31-A"
        client.get_orders = lambda **kw: {"orders": [
            {"order_id": "jack1", "ticker": t, "status": "resting",
             "client_order_id": "", "book_side": "bid", "yes_price": 40,
             "remaining_count": 100}], "cursor": None}
        bot = IncentiveMarketMaker(client=client, live=True)
        bot.run_cycle()
        self.assertIn(t, bot.state.manual_standoff)
        self.assertNotIn(t, bot.state.selected)
        self.assertEqual(getattr(client, "created", []), [])


class TestHourSizeMult(unittest.TestCase):
    """Quiet-hours ladder multiplier (IMM_HOUR_SIZE_MULT): parsing, ET window
    membership, series exclusion, rounding. July dates => EDT (UTC-4)."""

    def setUp(self):
        self._mults = imm.HOUR_SIZE_MULTS
        self._excl = imm.HOUR_MULT_EXCLUDE

    def tearDown(self):
        imm.HOUR_SIZE_MULTS = self._mults
        imm.HOUR_MULT_EXCLUDE = self._excl

    def test_parse_range_inclusive_both_ends(self):
        self.assertEqual(imm._parse_hour_mults("3-7:2.0"),
                         {3: 2.0, 4: 2.0, 5: 2.0, 6: 2.0, 7: 2.0})

    def test_parse_wrap_and_single_hour(self):
        self.assertEqual(imm._parse_hour_mults("22-1:0.5,13:1.5"),
                         {22: 0.5, 23: 0.5, 0: 0.5, 1: 0.5, 13: 1.5})

    def test_parse_rejects_garbage(self):
        for bad in ("3-25:2.0", "x-7:2", "3-7:0", "3-7:-1", "3-7"):
            with self.assertRaises(ValueError):
                imm._parse_hour_mults(bad)

    def test_empty_spec_is_off(self):
        self.assertEqual(imm._parse_hour_mults(""), {})
        imm.HOUR_SIZE_MULTS = {}
        base = imm.series_levels("KXFOO")
        self.assertIs(imm.hour_scaled_levels("KXFOO", utc(2026, 7, 25, 7, 30)),
                      base)

    def test_window_membership_et(self):
        imm.HOUR_SIZE_MULTS = imm._parse_hour_mults("3-7:2.0")
        imm.HOUR_MULT_EXCLUDE = ("KXTEMP",)
        base = imm.series_levels("KXFOO")
        doubled = [(t, s * 2) for t, s in base]
        # 07:30Z = 3:30am EDT -> in; 11:59Z = 7:59am EDT -> still in
        # (hour 7 inclusive); 12:00Z = 8:00am EDT -> out.
        self.assertEqual(
            imm.hour_scaled_levels("KXFOO", utc(2026, 7, 25, 7, 30)), doubled)
        self.assertEqual(
            imm.hour_scaled_levels("KXFOO", utc(2026, 7, 25, 11, 59)), doubled)
        self.assertEqual(
            imm.hour_scaled_levels("KXFOO", utc(2026, 7, 25, 12, 0)), base)
        # 06:59Z = 2:59am EDT -> out (window starts at 3).
        self.assertEqual(
            imm.hour_scaled_levels("KXFOO", utc(2026, 7, 25, 6, 59)), base)

    def test_excluded_prefix_never_scales(self):
        imm.HOUR_SIZE_MULTS = imm._parse_hour_mults("3-7:2.0")
        imm.HOUR_MULT_EXCLUDE = ("KXTEMP",)
        inside = utc(2026, 7, 25, 7, 30)
        self.assertEqual(imm.hour_scaled_levels("KXTEMPAUSH", inside),
                         imm.series_levels("KXTEMPAUSH"))
        self.assertEqual(imm.hour_size_mult("KXTEMPNYCH", inside), 1.0)

    def test_mention_family_multiplier(self):
        # Jack 2026-07-28: mention series x1.5 (15/0/0 on the 10-base),
        # caps commensurate. "Mention" = MENTION suffix + KXEARNINGSMENTION*.
        imm.HOUR_SIZE_MULTS = {}
        outside = utc(2026, 7, 28, 13, 0)      # 9am ET, no hour mult
        base = imm.series_levels("KXTRUMPMENTION")
        self.assertEqual(imm.hour_scaled_levels("KXTRUMPMENTION", outside),
                         [(t, int(s * 1.5 + 0.5)) for t, s in base])
        self.assertEqual(imm.hour_scaled_levels("KXEARNINGSMENTIONF", outside),
                         [(t, int(s * 1.5 + 0.5)) for t, s in base])
        # non-mention untouched
        self.assertIs(imm.hour_scaled_levels("KXGOOD", outside),
                      imm.series_levels("KXGOOD"))
        # composes with the quiet-hours window: x1.5 x2
        imm.HOUR_SIZE_MULTS = {3: 2.0}
        inside = utc(2026, 7, 28, 7, 30)       # 3:30am ET
        self.assertEqual(imm.hour_scaled_levels("KXTRUMPMENTION", inside),
                         [(t, int(s * 3.0 + 0.5)) for t, s in base])

    def test_mention_caps_scale_commensurately(self):
        self.assertAlmostEqual(imm.series_max_position("KXTRUMPMENTION"),
                               imm.MAX_POSITION_CONTRACTS * 1.5)
        self.assertAlmostEqual(imm.series_max_position("KXGOOD"),
                               imm.MAX_POSITION_CONTRACTS)
        # temp override cap unaffected (not mention)
        self.assertEqual(imm.series_max_position("KXTEMPDCH"), 50)
        # hand-tuned override series exempt even though it IS mention:
        # Love Island keeps its literal 7/12 spec (5/5/5, net 50)
        self.assertEqual(imm.series_max_position("KXLOVEISLMENTION"), 50)
        self.assertEqual(imm.applied_mention_mult("KXLOVEISLMENTION"), 1.0)
        self.assertAlmostEqual(
            imm.event_cap_contracts("KXTRUMPMENTION-26JUL24"),
            imm.MAX_EVENT_CONTRACTS * 1.5)
        self.assertAlmostEqual(imm.event_cap_contracts("KXGOOD-99DEC31"),
                               imm.MAX_EVENT_CONTRACTS)
        # skew thresholds scale via the explicit params
        room = imm.skewed_side_room(100, 40, accumulating=True,
                                    side_max=100, soft=45, hard=90)
        self.assertGreater(room, 0)            # 40 < scaled soft 45: no clamp
        room2 = imm.skewed_side_room(100, 40, accumulating=True, side_max=100)
        self.assertLess(room2, 100)            # default soft 30: halved

    def test_rounding_half_up_floor_one(self):
        imm.HOUR_SIZE_MULTS = {3: 0.5}
        imm.HOUR_MULT_EXCLUDE = ()
        inside = utc(2026, 7, 25, 7, 30)          # 3:30am EDT
        base = imm.series_levels("KXFOO")
        got = imm.hour_scaled_levels("KXFOO", inside)
        self.assertEqual(got, [(t, max(1, int(s * 0.5 + 0.5)))
                               for t, s in base])
        self.assertTrue(all(s >= 1 for _t, s in got))


# ----------------------------------------------------------------------------
# Rain daily fair-value anchor (2026-07-28)
# ----------------------------------------------------------------------------

class TestRainFairAnchor(unittest.TestCase):
    """rain_fair_values.json -> load_rain_fair/rain_fair_p -> at-touch GATE
    in the quote loop: quotes always join the touch (rewards need the top of
    book); fair only decides WHETHER to quote (stand aside when the touch
    fights the forecast beyond TOL). Fixture ticker KXRAIN-68DEC31-SEA
    (date parses via %y%b%d like production dailies; %y maps 99->1999)."""

    T = "KXRAIN-68DEC31-SEA"

    def setUp(self):
        _clean_persist()
        imm._rain_fair_state["mtime"] = 0.0
        imm._rain_fair_state["fair"] = {}
        try:
            os.remove(imm.RAIN_FAIR_FILE)
        except FileNotFoundError:
            pass

    def _write_fair(self, p, age_secs=0.0):
        fetched = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
        with open(imm.RAIN_FAIR_FILE, "w", encoding="utf-8") as f:
            json.dump({"fair": {"2068-12-31": {
                "SEA": {"p": p, "fetched_at": fetched.isoformat()}}}}, f)
        # mtime-gated reload: a rewrite inside the same mtime tick must not
        # be silently skipped
        os.utime(imm.RAIN_FAIR_FILE,
                 (time.time(), time.time() + self._bump))
        self._bump += 1
        self.assertEqual(imm.load_rain_fair(), 1)

    _bump = 1

    def _bot(self):
        client = FakeClient()
        now = datetime.now(timezone.utc)
        far = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.programs.append(
            {"market_ticker": self.T, "incentive_type": "liquidity",
             "period_reward": 7000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "paid_out": False,
             "start_date": client.programs[0]["start_date"],
             "end_date": client.programs[0]["end_date"]})
        client.markets[self.T] = {
            "ticker": self.T, "event_ticker": "KXRAIN-68DEC31",
            "status": "active", "close_time": far,
            "yes_bid_dollars": "0.4900", "yes_ask_dollars": "0.5100",
            "volume_fp": "500.00"}
        client.books[self.T] = {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}          # 49 x 51
        return IncentiveMarketMaker(client=client, live=False)

    def _rain_quotes(self, bot):
        return sorted((o["book_side"], o["yes_price"], o["remaining_count"])
                      for o in bot.state.sim_orders.values()
                      if o["ticker"] == self.T)

    def test_lookup_parsing_and_ttl(self):
        self._write_fair(0.30)
        now_ts = time.time()
        self.assertAlmostEqual(imm.rain_fair_p(self.T, now_ts), 0.30)
        self.assertIsNone(imm.rain_fair_p("KXRAIN-68DEC31-NYC", now_ts))   # absent city
        self.assertIsNone(imm.rain_fair_p("KXRAINMIAM-99DEC-5", now_ts))   # monthly series
        self.assertIsNone(imm.rain_fair_p("KXGOOD-99DEC31-A", now_ts))     # other series
        self.assertIsNone(imm.rain_fair_p("KXRAIN-BADDATE-SEA", now_ts))   # unparseable
        self.assertIsNone(                                                  # TTL expiry
            imm.rain_fair_p(self.T, now_ts + imm.RAIN_FAIR_TTL_MIN * 60 + 5))

    def test_stale_entry_degrades_to_plain_join(self):
        self._write_fair(0.30, age_secs=imm.RAIN_FAIR_TTL_MIN * 60 + 60)
        bot = self._bot()
        bot.run_cycle()
        self.assertEqual(self._rain_quotes(bot), sorted([
            ("bid", 49, 5.0), ("bid", 48, 10.0), ("bid", 47, 20.0),
            ("ask", 51, 5.0), ("ask", 52, 10.0), ("ask", 53, 20.0)]))

    PLAIN_JOIN = sorted([
        ("bid", 49, 5.0), ("bid", 48, 10.0), ("bid", 47, 20.0),
        ("ask", 51, 5.0), ("ask", 52, 10.0), ("ask", 53, 20.0)])

    def test_agreeing_fair_quotes_at_touch(self):
        self._write_fair(0.45)     # |touch - fair| within tol 10 -> AT TOUCH,
        bot = self._bot()          # never re-priced (rewards need the join)
        bot.run_cycle()
        self.assertEqual(self._rain_quotes(bot), self.PLAIN_JOIN)

    def test_tol_boundary_inclusive(self):
        self._write_fair(0.39)     # bid touch 49 == fair 39 + tol 10: NOT a
        bot = self._bot()          # breach (strict >), still quotes at touch
        bot.run_cycle()
        self.assertEqual(self._rain_quotes(bot), self.PLAIN_JOIN)

    def test_bid_touch_over_fair_stands_aside_then_resumes(self):
        self._write_fair(0.30)     # bid touch 49 > 30+10 -> paying over fair
        bot = self._bot()
        bot.run_cycle()
        self.assertEqual(self._rain_quotes(bot), [])
        self.assertIn(self.T, bot._rain_fair_stood)
        self.assertIn(self.T, bot.state.selected)     # sticky: still selected
        # non-rain market in the same cycle keeps its plain join
        good = sorted((o["book_side"], o["yes_price"]) for o in
                      bot.state.sim_orders.values()
                      if o["ticker"] == "KXGOOD-99DEC31-A")
        self.assertIn(("bid", 49), good)
        self.assertIn(("ask", 51), good)
        self._write_fair(0.50)     # forecast agrees again -> resume AT TOUCH
        bot.run_cycle()
        self.assertNotIn(self.T, bot._rain_fair_stood)
        self.assertEqual(self._rain_quotes(bot), self.PLAIN_JOIN)

    def test_ask_touch_under_fair_stands_aside(self):
        self._write_fair(0.70)     # ask touch 51 < 70-10 -> selling under fair
        bot = self._bot()
        bot.run_cycle()
        self.assertEqual(self._rain_quotes(bot), [])
        self.assertIn(self.T, bot._rain_fair_stood)

    def test_disabled_flag_restores_plain_join(self):
        self._write_fair(0.30)     # would gate if enabled
        old = imm.RAIN_FAIR_ENABLE
        imm.RAIN_FAIR_ENABLE = False
        try:
            bot = self._bot()
            bot.run_cycle()
            self.assertEqual(self._rain_quotes(bot), self.PLAIN_JOIN)
        finally:
            imm.RAIN_FAIR_ENABLE = old


if __name__ == "__main__":
    unittest.main()
