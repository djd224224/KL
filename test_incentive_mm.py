#!/usr/bin/env python3
"""Unit tests for incentive_mm.py — run: python -m unittest test_incentive_mm"""

import csv
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
    # The suite must never SMTP the user. Pre-existing hazard, noticed while
    # fixing the 2026-08-06 bench bug: TestWatchdog.test_selected_but_not_
    # resting_pages fires alerter.alert(..., urgent=True) with no stub, so on
    # any shell with ALERT_EMAIL_FROM/PASSWORD set (the trading box has them)
    # every `python -m unittest test_incentive_mm` sent a real watchdog page to
    # jackdu224@gmail.com. Nothing about the alert LOGIC is stubbed here —
    # alerter.today still records every alert, which is what tests assert on.
    imm.ALERT_RECIPIENTS = []


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
from KalshiClientsBaseV2ApiKey_FIXED import HttpError


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
        # exp within the listing-gap bar so the ticker date stays a candidate
        # (a 21d exp on a MENTION ticker now means "listing date" — see below)
        occ, exp = utc(2026, 7, 12), utc(2026, 7, 13)
        cut = trade_cutoff_utc("KXWCMENTION-26JUL11ARGSUI", occ, exp)
        self.assertEqual(cut, parse_event_date("KXWCMENTION-26JUL11ARGSUI"))

    def test_none(self):
        self.assertIsNone(trade_cutoff_utc("KXBOND-30", None, None))


class TestListingDateCutoff(unittest.TestCase):
    """2026-08-14 fix: long-window mention programs embed the LISTING date in
    the ticker (KXMAMDANIMENTION-26AUG14 ran to Sep 4). When the market's own
    expiration proves that, the whole window is quotable and the cutoff is the
    expiration itself — NOT None, which would trip _screen's no_event_window
    stand-down for mention-family series."""

    def test_mamdani_listing_window_quotes_to_expiration(self):
        exp = utc(2026, 9, 4, 14)
        cut = trade_cutoff_utc("KXMAMDANIMENTION-26AUG14", None, exp)
        self.assertEqual(cut, exp)

    def test_occurrence_still_wins_on_listing_dated_event(self):
        occ, exp = utc(2026, 8, 20, 15), utc(2026, 9, 4, 14)
        cut = trade_cutoff_utc("KXMAMDANIMENTION-26AUG14", occ, exp)
        self.assertEqual(cut, occ)

    def test_same_day_mention_keeps_ticker_cutoff(self):
        # earnings-mention settles on call day: gap under the bar
        exp = utc(2026, 8, 8, 20)
        cut = trade_cutoff_utc("KXEARNINGSMENTIONDKNG-26AUG07", None, exp)
        self.assertEqual(cut, parse_event_date("KXEARNINGSMENTIONDKNG-26AUG07"))

    def test_non_mention_long_gap_keeps_ticker_cutoff(self):
        # only the mention family may flip: an event-dated series with a long
        # verification window keeps failing toward quoting LESS
        exp = utc(2026, 10, 15)
        cut = trade_cutoff_utc("KXDEBATE-26SEP15", None, exp)
        self.assertEqual(cut, parse_event_date("KXDEBATE-26SEP15"))

    def test_no_expiration_keeps_ticker_cutoff(self):
        # no expiration = no proof of a listing date: conservative reading
        cut = trade_cutoff_utc("KXMAMDANIMENTION-26AUG14", None, None)
        self.assertEqual(cut, parse_event_date("KXMAMDANIMENTION-26AUG14"))


class TestListingDatedMentionSelection(unittest.TestCase):
    """End-to-end guard for the 24h ticker PRE-FILTER (the third copy of the
    listing-date assumption): KXTRUMPMENTION-26AUG13 was pre-dropped on the
    ticker string at td+24h before the fixed trade_cutoff_utc ever saw it.
    A mention market with a PAST ticker date and a far expiration must
    hydrate, select, and carry cutoff = expiration."""

    def test_past_ticker_date_mention_with_far_expiration_selects(self):
        _clean_persist()
        client = FakeClient()
        now = datetime.now(timezone.utc)
        ev = "KXFOOMENTION-" + (now - timedelta(days=10)).strftime("%y%b%d").upper()
        t = ev + "-AI"
        exp = (now + timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.programs = [dict(client.programs[0], market_ticker=t, end_date=exp)]
        client.markets = {t: dict(client.markets["KXGOOD-99DEC31-A"], ticker=t,
                                  event_ticker=ev, close_time=exp,
                                  expected_expiration_time=exp)}
        client.books = {t: {"orderbook_fp": {
            "yes_dollars": [["0.48", "500"], ["0.49", "600"]],
            "no_dollars": [["0.49", "1200"]]}}}
        bot = IncentiveMarketMaker(client=client, live=False)
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)
        self.assertEqual(bot.state.selected[t].cutoff, imm.parse_iso_utc(exp))


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
    """Post-2026-07-30 rules: reference = level where cumulative >= target/5;
    full weight at/above reference; decay below it; walk to target."""

    def test_all_ours_at_best(self):
        share, q = _side_share([(50, 100.0)], {50: 100.0}, target=100, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 1.0)

    def test_reference_at_touch_when_touch_is_deep(self):
        # touch has 100 >= target/5 (40) -> ref=50; our 100 at 49 decays 0.5:
        # share = 50/150 (same as the old rules for a deep touch)
        share, q = _side_share([(50, 100.0), (49, 100.0)], {49: 100.0},
                               target=200, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 50.0 / 150.0)

    def test_reference_below_thin_touch_full_weight_band(self):
        # target 500 -> ref needs cum >= 100: 10@50 (no), +100@49 -> ref=49.
        # Walk to >= 500: +400@48 stops. Weights: 50 -> max(49-50,0)=0 -> 1.0
        # (above ref, N clamped), 49 -> 1.0, 48 -> 0.5.
        # Ours 10@50 + 100@49: share = 110 / (10 + 100 + 200) = 110/310.
        share, q = _side_share([(50, 10.0), (49, 100.0), (48, 400.0)],
                               {50: 10.0, 49: 100.0}, target=500, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 110.0 / 310.0)

    def test_tiny_touch_no_longer_dominates(self):
        # Old rules: our 5 at the touch out-scored 995 one tick behind
        # (5 vs 497.5). New rules: ref lands on the size level; both full
        # weight -> we are 5/1000.
        share, q = _side_share([(50, 5.0), (49, 995.0)], {50: 5.0},
                               target=1000, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 5.0 / 1000.0)

    def test_thin_book_no_qualify(self):
        share, q = _side_share([(50, 100.0)], {50: 100.0}, target=1000, df=0.5)
        self.assertEqual((share, q), (0.0, False))

    def test_walk_stops_at_target(self):
        # ref at 50 (600 >= 200); target reached at the second level; third
        # level does not score
        share, q = _side_share([(50, 600.0), (49, 500.0), (48, 500.0)],
                               {48: 500.0}, target=1000, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 0.0)

    def test_best_at_99_now_qualifies(self):
        # The old "touch must improve on 99" disqualifier is gone.
        share, q = _side_share([(99, 5000.0)], {99: 100.0}, target=100, df=0.5)
        self.assertTrue(q)
        self.assertAlmostEqual(share, 100.0 / 5000.0)

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

    def test_one_side_qualifies_pays_nothing(self):
        # Post-7/30: a snapshot without two-sided-to-target liquidity is
        # EXCLUDED — one good side earns zero (it used to earn share/1).
        frac, sides = estimate_reward_share(
            [[49, 50.0]], self.BOOK_NO, [("ask", 50, 100.0)],
            target=1000, df=0.5, own_in_book=False)
        self.assertEqual(sides, 1)   # yes side too thin
        self.assertEqual(frac, 0.0)

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


class TestAtRefLadder(unittest.TestCase):
    """LADDER_MODE=atref: whole side collapses to one rung at the amended
    rules' reference level (deepest full-weight price)."""

    def setUp(self):
        self._mode = imm.LADDER_MODE
        imm.LADDER_MODE = "atref"

    def tearDown(self):
        imm.LADDER_MODE = self._mode

    def test_bid_collapses_to_ref(self):
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=45)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(45, 68)])  # depth 5 -> 2.25x

    def test_bid_ref_below_band_allowed(self):
        # at-ref rungs are band-exempt (Jack 2026-08-01): a 2c reference
        # rests at 2c, not pinned to the 5c series floor
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=2)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(5, 90)])   # envelope floor 5

    def test_ask_ref_above_band_allowed(self):
        # default (fresh) callers cap at the SERIES band; members pass their
        # widened band and cap at the sticky 93
        qs = build_side_ladder("T", "ask", 50, 45, room=99,
                               levels=[(0, 30)], ref_px=95)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(90, 90)])
        qs = build_side_ladder("T", "ask", 50, 45, room=99,
                               levels=[(0, 30)], ref_px=95, band=(5, 93))
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(93, 90)])

    def test_deep_rung_floor_follows_ref_on_healthy_books(self):
        # Jack 2026-08-03: in-band markets may rest below 5c when the
        # reference is lower (caller passes the relaxed band); default
        # callers keep the 5c floor; 1c stays pad-only.
        qs = build_side_ladder("T", "bid", 7, 29, room=99,
                               levels=[(0, 30)], ref_px=3, band=(2, 93))
        self.assertEqual(qs[0].price_cents, 3)
        qs = build_side_ladder("T", "bid", 7, 29, room=99,
                               levels=[(0, 30)], ref_px=3)
        self.assertEqual(qs[0].price_cents, 5)
        qs = build_side_ladder("T", "bid", 7, 29, room=99,
                               levels=[(0, 30)], ref_px=0, band=(2, 93))
        self.assertEqual(qs[0].price_cents, 2)      # hard floor: never 1c

    def test_per_side_top_in_band(self):
        # Padding is hourly-TEMP-only since 2026-08-05; this test's
        # SUBJECT is the pad machinery, so enable it explicitly rather
        # than relying on a default that no longer holds.
        _pad = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = True
        self.addCleanup(setattr, imm, 'PAD_TO_TARGET_GLOBAL', _pad)
        # Jack 2026-08-03 "Do 1": a wide live book (3c x 29c) stands down
        # only its out-of-band BID side; the ask keeps quoting and the bid
        # pad still qualifies the snapshot.
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.run_cycle()
        t = "KXGOOD-99DEC31-A"
        self.assertIn(t, bot.state.selected)
        bot.client.books[t] = {"orderbook_fp": {
            "yes_dollars": [["0.03", "400"]], "no_dollars": [["0.71", "400"]]}}
        bot.client.markets[t]["yes_bid_dollars"] = "0.0300"
        bot.client.markets[t]["yes_ask_dollars"] = "0.2900"
        bot.state.universe_at = time.time()
        bot.run_cycle()
        orders = list(bot.state.sim_orders.values())
        asks = [o for o in orders if o["ticker"] == t and o["book_side"] == "ask"
                and o["yes_price"] < imm.PAD_ASK_CENTS]
        bad_bids = [o for o in orders if o["ticker"] == t
                    and o["book_side"] == "bid"
                    and o["yes_price"] > imm.PAD_BID_CENTS]
        self.assertTrue(asks)                 # healthy side quotes
        self.assertEqual(bad_bids, [])        # stood-down side: no rungs

    def test_ref_absolute_bounds(self):
        # Absolute envelope = the sticky band (5-93 since 2026-08-03)
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=0)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(5, 90)])   # 3x cap

    def test_ask_collapses_to_ref(self):
        # ask side price space = YES-ask cents; deeper = higher
        qs = build_side_ladder("T", "ask", 50, 45, room=99,
                               levels=[(0, 30)], ref_px=58)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(58, 90)])  # depth 8 -> 3x cap

    def test_ref_never_improves_anchor(self):
        # reference above the anchor (tight book): stay at the anchor join
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=52)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(50, 30)])

    def test_multi_rung_levels_sum(self):
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 5), (1, 10), (2, 20)], ref_px=47)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(47, 61)])  # 35 x 1.75

    def test_room_still_shaves(self):
        qs = build_side_ladder("T", "bid", 50, 55, room=12,
                               levels=[(0, 30)], ref_px=45)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(45, 12)])

    def test_no_ref_falls_back_to_offsets(self):
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 5), (1, 10)], ref_px=None)
        self.assertEqual([(q.price_cents, q.count) for q in qs],
                         [(50, 5), (49, 10)])

    def test_offsets_mode_ignores_ref(self):
        imm.LADDER_MODE = "offsets"
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 5)], ref_px=45)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(50, 5)])

    def test_deep_ref_scales_size(self):
        # 2026-08-02 sim-confirmed curve: +25%/tick, capped 3x.
        # depth 5 -> 2.25x: 30 -> 68
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=45)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(45, 68)])
        # depth 10 -> capped 3.0x: 30 -> 90
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=40)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(40, 90)])
        # depth 20 -> still 3.0x
        qs = build_side_ladder("T", "bid", 50, 55, room=200,
                               levels=[(0, 30)], ref_px=30)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(30, 90)])
        # at the touch (ref >= anchor): 1.0x
        qs = build_side_ladder("T", "bid", 50, 55, room=99,
                               levels=[(0, 30)], ref_px=50)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(50, 30)])
        # ask side: depth = ref above anchor; depth 5 -> 2.25x -> 68
        qs = build_side_ladder("T", "ask", 50, 45, room=99,
                               levels=[(0, 30)], ref_px=55)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(55, 68)])
        # room still caps the scaled size
        qs = build_side_ladder("T", "bid", 50, 55, room=50,
                               levels=[(0, 30)], ref_px=40)
        self.assertEqual([(q.price_cents, q.count) for q in qs], [(40, 50)])

    def test_ref_depth_mult_direct(self):
        # 2026-08-02 sim-confirmed curve: 0.25/tick, cap 3.0 (was 0.1/2.0)
        imm.LADDER_MODE = "atref"
        self.assertEqual(imm.ref_depth_mult(50, 45, "bid"), 2.25)
        self.assertEqual(imm.ref_depth_mult(50, 40, "bid"), 3.0)
        self.assertEqual(imm.ref_depth_mult(50, 50, "bid"), 1.0)
        self.assertEqual(imm.ref_depth_mult(50, 55, "ask"), 2.25)
        self.assertEqual(imm.ref_depth_mult(None, 45, "bid"), 1.0)
        self.assertEqual(imm.ref_depth_mult(50, None, "bid"), 1.0)
        imm.LADDER_MODE = "offsets"
        self.assertEqual(imm.ref_depth_mult(50, 40, "bid"), 1.0)


class TestLadderReferencePrices(unittest.TestCase):
    YES = [[48, 150.0], [49, 100.0]]     # desc: 100@49, 150@48
    NO = [[50, 400.0]]

    def test_mode_off(self):
        self.assertEqual(imm.ladder_reference_prices(self.YES, self.NO, 1000),
                         (None, None))

    def test_refs_computed(self):
        old = imm.LADDER_MODE
        imm.LADDER_MODE = "atref"
        try:
            # target/5 = 200: yes walk 100@49 -> 250@48 => ref 48;
            # no walk 400@50 => ref 50 -> yes-ask space 100-50 = 50
            self.assertEqual(
                imm.ladder_reference_prices(self.YES, self.NO, 1000), (48, 50))
        finally:
            imm.LADDER_MODE = old

    def test_side_reference_level_thin(self):
        self.assertIsNone(imm.side_reference_level([(50, 10.0)], 1000))
        self.assertIsNone(imm.side_reference_level([], 1000))


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


class TestAtRefDiffHysteresis(unittest.TestCase):
    """atref requote matching. 2026-08-03: the global price tolerance is 0
    (amend-in-place made repricing free, and a rung 1 tick below the
    reference earns HALF weight), so every series re-pins in BOTH
    directions. Tests that exercise the tolerant regime patch
    ATREF_PRICE_TOL_TICKS explicitly."""

    def setUp(self):
        self._mode = imm.LADDER_MODE
        imm.LADDER_MODE = "atref"

    def tearDown(self):
        imm.LADDER_MODE = self._mode

    def _diff(self, desired, resting, touch=None):
        return imm.diff_orders(desired, resting, {}, 0.0,
                               touch_by_ticker=touch)

    def test_one_tick_behind_amended_at_zero_tol(self):
        # default tol 0: a rung 1 tick below the reference is half weight,
        # so it is amended back (gapless) rather than left to its TTL.
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 44, 30)]
        place, cancel, amend = self._diff(d, r)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 45)])

    def test_one_tick_behind_kept_when_tolerant(self):
        old = imm.ATREF_PRICE_TOL_TICKS
        imm.ATREF_PRICE_TOL_TICKS = 1
        try:
            d = [imm.Quote("T", "bid", 45, 30)]
            r = [_resting("o1", "T", "bid", 44, 30)]
            self.assertEqual(self._diff(d, r), ([], [], []))
        finally:
            imm.ATREF_PRICE_TOL_TICKS = old

    def test_aggressive_drift_amended_without_touch(self):
        # No touch data -> the aggressive-drift keep is disabled; the rung is
        # AMENDED back to desired in place (2026-08-02: no cancel+place gap).
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 46, 30)]
        place, cancel, amend = self._diff(d, r)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 45)])

    def test_aggressive_drift_kept_inside_touch(self):
        # Asymmetric chase (2026-08-02): the reference moved DEEPER, so the
        # rung above desired still earns full weight — keep it while it is
        # not leading the current touch.
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 46, 30)]
        old = imm.ATREF_PRICE_TOL_TICKS
        imm.ATREF_PRICE_TOL_TICKS = 1          # keep path needs a tolerant series
        try:
            self.assertEqual(self._diff(d, r, touch={"T": (47, 53)}), ([], [], []))
        finally:
            imm.ATREF_PRICE_TOL_TICKS = old
        # at the default tol 0 the same rung re-pins to the deeper reference
        # (same full weight, further from the touch = less fill risk)
        place, cancel, amend = self._diff(d, r, touch={"T": (47, 53)})
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 45)])

    def test_aggressive_keep_respects_safe_join_net(self):
        # Safe-join series: the kept rung must stay >= 2 ticks off a tight
        # touch (live audit: a gas bid ended 1 tick off after the book
        # drifted). Inside the net -> amend back to desired.
        t = "KXAAAGASD-26AUG03-4.090"       # safe-join series
        d = [imm.Quote(t, "bid", 68, 30)]
        r = [_resting("o1", t, "bid", 69, 30)]
        old = imm.ATREF_PRICE_TOL_TICKS
        imm.ATREF_PRICE_TOL_TICKS = 1          # keep path needs a tolerant series
        try:
            # tight spread (70/74): 69 is only 1 off the 70 touch -> amend
            place, cancel, amend = self._diff(d, r, touch={t: (70, 74)})
            self.assertEqual((place, cancel), ([], []))
            self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                             [("o1", 68)])
            # wide spread (70/76): the spread IS the net -> keep
            self.assertEqual(self._diff(d, r, touch={t: (70, 76)}), ([], [], []))
        finally:
            imm.ATREF_PRICE_TOL_TICKS = old

    def test_aggressive_leading_book_amended(self):
        # ...but a rung ABOVE the current touch is leading the book: fix it.
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 46, 30)]
        place, cancel, amend = self._diff(d, r, touch={"T": (44, 53)})
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 45)])

    def test_count_within_tolerance_kept(self):
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 45, 25)]     # |25-30|=5 <= 6: keep
        place, cancel, amend = self._diff(d, r)
        self.assertEqual((place, cancel, amend), ([], [], []))

    def test_count_beyond_tolerance_amended(self):
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 45, 20)]     # |20-30|=10 > 6: amend
        place, cancel, amend = self._diff(d, r)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.count) for o, q in amend],
                         [("o1", 30)])

    def test_ask_side_direction(self):
        d = [imm.Quote("T", "ask", 55, 30)]
        r_ok = [_resting("o1", "T", "ask", 56, 30)]   # 1 behind: re-pinned at tol 0
        place, cancel, amend = self._diff(d, r_ok)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([o["order_id"] for o, _q in amend], ["o1"])
        r_ag = [_resting("o2", "T", "ask", 54, 30)]   # aggressive, no touch
        place, cancel, amend = self._diff(d, r_ag)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([o["order_id"] for o, _q in amend], ["o2"])
        # aggressive ask at/above the touch is kept only in the tolerant regime
        old = imm.ATREF_PRICE_TOL_TICKS
        imm.ATREF_PRICE_TOL_TICKS = 1
        try:
            self.assertEqual(self._diff(d, r_ag, touch={"T": (50, 54)}),
                             ([], [], []))
        finally:
            imm.ATREF_PRICE_TOL_TICKS = old

    def test_stale_ttl_still_cancel_places(self):
        # amend cannot extend the exchange-side expiration -> TTL-stale
        # orders keep the cancel + fresh-place path.
        now = time.time()
        d = [imm.Quote("T", "bid", 45, 30)]
        r = [_resting("o1", "T", "bid", 44, 30)]      # would otherwise keep
        place, cancel, amend = imm.diff_orders(
            d, r, {"o1": now - imm.ORDER_REFRESH_SECS - 1}, now)
        self.assertEqual(cancel, ["o1"])
        self.assertEqual(amend, [])
        self.assertEqual([(q.price_cents, q.count) for q in place], [(45, 30)])

    def test_temp_repins_both_directions(self):
        # Jack 2026-08-03: zero-tol series never use the aggressive-keep —
        # a temp rung closer to the touch than desired amends back to the
        # protected reference even when touch data would otherwise keep it.
        t = "KXTEMPDCH-26AUG0223-T75.99"
        d = [imm.Quote(t, "ask", 93, 40)]
        r = [_resting("o1", t, "ask", 62, 40)]      # aggressive vs desired
        place, cancel, amend = self._diff(d, r, touch={t: (7, 50)})
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 93)])

    def test_temp_zero_tol_amends_on_any_drift(self):
        # Jack 2026-08-02: KXTEMP hysteresis is 0 — a rung even 1 tick behind
        # desired reprices on the next tick; with the amend path that is an
        # in-place amend, not a cancel+place gap.
        t = "KXTEMPDCH-26AUG0210-T80.99"
        d = [imm.Quote(t, "bid", 45, 30)]
        r = [_resting("o1", t, "bid", 44, 30)]
        place, cancel, amend = self._diff(d, r)
        self.assertEqual((place, cancel), ([], []))
        self.assertEqual([(o["order_id"], q.price_cents) for o, q in amend],
                         [("o1", 45)])

    def test_series_atref_tol_helper(self):
        self.assertEqual(imm.series_atref_price_tol("KXTEMPDCH"), 0)
        old = imm.ATREF_PRICE_TOL_TICKS
        imm.ATREF_PRICE_TOL_TICKS = 3
        try:
            self.assertEqual(imm.series_atref_price_tol("KXGOOD"), 3)
            self.assertEqual(imm.series_atref_price_tol("KXTEMPDCH"), 0)
        finally:
            imm.ATREF_PRICE_TOL_TICKS = old

    def test_safe_join_clamps_tight_books(self):
        # Re-entry safety net (Jack 2026-08-02): tight spread -> rest >= 2
        # ticks behind the touch, BUT never past the reference (Jack
        # 2026-08-05) — behind the reference scores zero, so the old
        # unconditional 2-tick step made such markets unquotable rather than
        # safe. 5+ tick spread is its own safety net -> normal at-ref.
        t = "KXBA-26AUGDELIV-T30"          # KXBA carries the re-entry override
        # tight book, ref AT the touch: the net would earn nothing, so we
        # rest at the reference and take the diluted share instead
        (q,) = imm.build_side_ladder(t, "bid", 50, 53, 100, ref_px=50)
        self.assertEqual(q.price_cents, 50)
        (q,) = imm.build_side_ladder(t, "ask", 53, 50, 100, ref_px=53)
        self.assertEqual(q.price_cents, 53)
        # tight book, ref 1 tick back: land on the reference, not anchor-2
        (q,) = imm.build_side_ladder(t, "bid", 50, 53, 100, ref_px=49)
        self.assertEqual(q.price_cents, 49)
        # tight book, ref 2+ ticks back: the net binds exactly as before
        (q,) = imm.build_side_ladder(t, "bid", 50, 53, 100, ref_px=48)
        self.assertEqual(q.price_cents, 48)
        # wide book: bid 40 / ask 60 (spread 20 >= 5) -> at-ref untouched
        (q,) = imm.build_side_ladder(t, "bid", 40, 60, 100, ref_px=40)
        self.assertEqual(q.price_cents, 40)
        # deep reference already past the net -> unchanged by the clamp
        (q,) = imm.build_side_ladder(t, "bid", 50, 53, 100, ref_px=45)
        self.assertEqual(q.price_cents, 45)
        # non-safe series: tight book still joins at the reference
        (q,) = imm.build_side_ladder("KXGOOD-99DEC31-A", "bid", 50, 53, 100,
                                     ref_px=50)
        self.assertEqual(q.price_cents, 50)


class TestDiffOrders(unittest.TestCase):
    def test_exact_match_kept(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel, _ = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual((place, cancel), ([], []))

    def test_price_drift_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 48, 5.0)]
        place, cancel, _ = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(place, [Quote("T", "bid", 49, 5)])

    def test_partial_fill_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 3.0)]   # was 5, partially filled
        place, cancel, _ = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - 60}, now)
        self.assertEqual(cancel, ["a"])

    def test_ttl_stale_replaced(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel, _ = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now - imm.ORDER_REFRESH_SECS - 1}, now)
        self.assertEqual(cancel, ["a"])
        self.assertEqual(len(place), 1)

    def test_undesired_cancelled(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 49, 5.0)]
        place, cancel, _ = diff_orders([], resting, {"a": now}, now)
        self.assertEqual(cancel, ["a"])

    def test_blind_preserved(self):
        now = time.time()
        resting = [_resting("a", "T", "bid", 40, 5.0)]
        place, cancel, _ = diff_orders([Quote("T", "bid", 49, 5)], resting,
                                    {"a": now}, now, preserve_tickers={"T"})
        self.assertEqual((place, cancel), ([], []))

    def test_unparseable_cancelled(self):
        now = time.time()
        place, cancel, _ = diff_orders([], [{"order_id": "x", "ticker": "T"}], {}, now)
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

    def test_annual_crypto_blocked(self):
        # crypto_annual_mm.py's book as of 2026-08-13 — all three annual
        # families, same arrangement as *MAXMON/*MINMON (same-account STP).
        b = IncentiveMarketMaker._blocked
        self.assertTrue(b("KXBTCMAXY-26DEC31-109999.99"))
        self.assertTrue(b("KXBNBMAXY-BNB-26DEC31-64000"))
        self.assertTrue(b("KXHYPEMINY-HYPE-26DEC31-30"))
        self.assertTrue(b("KXBTCY-27JAN0100-B122500"))
        self.assertTrue(b("KXSOLD26-27JAN0100-T249.99"))
        # KXSOLD26 must be blocked WITHOUT eating KXSOLDATHOLDINGS
        self.assertFalse(b("KXSOLDATHOLDINGS-26AUG14-T2500000"))


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
        self.assertTrue(a("KXCHINAUNBANBTC-26JUL08-30JAN01"))
        self.assertTrue(a("KXCRYPTORETURNY-27JAN01-BTC"))
        # the fleet's MONTHLY series must stay excluded (same-account STP)
        self.assertFalse(a("KXBNBMAXMON-BNB-26JUL31-64000"))
        self.assertFalse(a("KXHYPEMINMON-HYPE-26JUL31-5250"))
        # ... and since 2026-08-13 the ANNUAL series too: the yearly pairs
        # (allowlisted 2026-07-22 while no fleet bot quoted them) are
        # crypto_annual_mm.py's book now, out of the allowlist entirely.
        self.assertFalse(a("KXBTCMAXY-26-T150"))
        self.assertFalse(a("KXBNBMAXY-BNB-26DEC31"))
        self.assertFalse(a("KXHYPEMINY-HYPE-26DEC31"))
        self.assertFalse(a("KXZECMAXY-ZEC-26DEC31"))

    def test_company_metric_series(self):
        # 2026-08-02 RE-ENTRY (Jack): company family quotes again (freeze
        # default emptied) behind the $2/day rate floor + safe-join rule.
        a = IncentiveMarketMaker._allowed
        self.assertTrue(a("KXBA-26JULDELIV-130"))
        self.assertTrue(a("KXHOOD-26JULFUNDED-28300000"))
        self.assertTrue(a("KXCOINBASE-26JULVOL-240000000000"))
        self.assertTrue(a("KXWINGA-27FEBREST-3400"))
        self.assertTrue(a("KXSBUXSAR-26AUG02-T5.09"))
        self.assertTrue(a("KXCHIPBURRITO-26AUG02-T9.77"))
        import incentive_mm as _imm
        old = _imm.FREEZE_SERIES
        _imm.FREEZE_SERIES = frozenset({"KXBA"})
        try:
            # IMM_FREEZE_SERIES still refreezes on demand
            self.assertFalse(a("KXBA-26JULDELIV-130"))
        finally:
            _imm.FREEZE_SERIES = old
        # non-ticker lookalikes still excluded
        self.assertFalse(a("KXMUSKNW-26JUL31-T950"))
        self.assertFalse(a("KXTRUTHSOCIAL-26JUL25-T240"))

    def test_econ_series(self):
        a = IncentiveMarketMaker._allowed
        for t in ("KXAAAGASD-26JUL23-4.150", "KXAAAGASW-26JUL27-4.040",
                  "KXAAAGASM-26JUL31-3.10", "KXNHSALES-26JUL24-T620000",
                  "KXUSGASCPI-26AUG12-T320",
                  # diesel enrolled 2026-08-02 evening under re-entry guards
                  "KXDIESELD-26AUG03-T5.350", "KXDIESELW-26AUG09-T5.30"):
            self.assertTrue(a(t), t)
        # Truflation's OTHER Kalshi index stays out — never enrolled
        self.assertFalse(a("KXTRUFAIDP-26AUG26-T50"))
        # KXTRUEV BLOCKED 2026-08-25 (Jack), the morning after the 8/24
        # enrollment saga: blocklist wins over its (kept) allowlist entry and
        # overrides — zero orders, positions ride. Re-enable = delete the
        # one SERIES_BLOCKLIST_PREFIXES entry.
        b = IncentiveMarketMaker._blocked
        self.assertTrue(b("KXTRUEV-26AUG26-T1241.88"))
        self.assertFalse(a("KXTRUEV-26AUG26-T1241.88"))
        # the enrollment machinery is deliberately kept for re-enable: the
        # close-anchored cutoff override (the print-day-listing fix) stays
        self.assertEqual(
            imm.series_override("KXTRUEV").cutoff_from_close_min, 60)
        # Rate bar KEPT (Jack 2026-08-05) but scoped to the first strike of
        # an event the bot is not already working — see
        # TestRateBarScopedToNewEvents.
        for _s in ("KXDIESELD", "KXAAAGASD", "KXUSGASCPI"):
            self.assertEqual(imm.series_min_est_rate(_s), 2.0, _s)
            self.assertTrue(imm.series_safe_join(_s), _s)
        # KXTRUEV: bar OFF (2026-08-25, the KXDIESELW pattern) — at program
        # open its strikes est within pennies of the $2 bar, which decided
        # a $89/day/market event on book noise. Safe-join + $1 floor stay.
        self.assertEqual(imm.series_min_est_rate("KXTRUEV"), 0.0)
        self.assertTrue(imm.series_safe_join("KXTRUEV"))
        # KXDIESELW override (Jack 2026-08-03): rate bar off, safe-join kept
        self.assertEqual(imm.series_min_est_rate("KXDIESELW"), 0.0)
        self.assertTrue(imm.series_safe_join("KXDIESELW"))
        # KXSCFI: frozen 7/29, RE-ALLOWED 2026-08-02 with the re-entry
        # guards ($2/day rate floor + safe-join).
        self.assertTrue(a("KXSCFI-26DEC25-T1500"))
        self.assertEqual(imm.series_min_est_rate("KXSCFI"), 2.0)
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

    def test_fast_tick_touches_nothing_without_fast_series(self):
        # Fast-lane mini-cycle (Jack 2026-08-02) on a universe with no
        # fast-lane series: every order the full cycle placed must survive
        # untouched (preserved through the diff, NOT cancelled as unmatched),
        # and the shared accrual stamp / cycle counter must not move (fast
        # ticks defer accrual so other markets' integrals aren't robbed).
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        before = dict(bot.state.sim_orders)
        stamp = bot.state.reward_accrue_at
        cycles = bot.state.cycles_today
        bot.run_cycle(fast_only=True)
        self.assertEqual(bot.state.sim_orders, before)
        self.assertEqual(bot.state.reward_accrue_at, stamp)
        self.assertEqual(bot.state.cycles_today, cycles)

    def test_fast_lane_membership(self):
        self.assertTrue(imm.series_fast_lane("KXTEMPDCH"))
        self.assertFalse(imm.series_fast_lane("KXGOOD"))

    def test_total_size_mult_cap(self):
        # Jack 2026-08-02: hour x ref <= 5. Deep ref (cap 3) with overnight
        # 2x -> ref trimmed to 2.5; daytime (hour 1) keeps the full 3; an
        # extreme hour mult floors the ref contribution at 1.0.
        old = imm.LADDER_MODE
        imm.LADDER_MODE = "atref"
        try:
            self.assertEqual(imm.capped_ref_mult(50, 30, "bid", hour_mult=1.0), 3.0)
            self.assertEqual(imm.capped_ref_mult(50, 30, "bid", hour_mult=2.0), 2.5)
            self.assertEqual(imm.capped_ref_mult(50, 30, "bid", hour_mult=6.0), 1.0)
            # cap trims only when the ref mult would exceed the headroom
            self.assertEqual(imm.capped_ref_mult(50, 48, "bid", hour_mult=2.0), 1.5)
        finally:
            imm.LADDER_MODE = old

    def test_member_price_band(self):
        # 2-98 (8/2) -> 5-93 (Jack 2026-08-03 after the Austin 96c pickoff):
        # members ride modestly past the series top; fresh keeps series.
        self.assertEqual(imm.member_price_band("KXGOOD", False), (5, 90))
        self.assertEqual(imm.member_price_band("KXGOOD", True), (5, 93))
        self.assertEqual(imm.member_price_band("KXTEMPDCH", True), (5, 93))

    def test_sticky_member_rides_to_extreme_band(self):
        # Select at a normal book, then drift the top to 91x92 (over the
        # series 90 but inside the member 93): the member keeps quoting
        # where the fresh-band rule would stand it down.
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.run_cycle()
        self.assertIn(t, bot.state.selected)
        # >= target on both sides so the two-sided depth gate (2026-08-05)
        # is not what this test measures
        bot.client.books[t] = {"orderbook_fp": {
            "yes_dollars": [["0.91", "1200"]],
            "no_dollars": [["0.08", "1200"]]}}      # touch 91c / 92c
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertTrue(any(o["ticker"] == t
                            for o in bot.state.sim_orders.values()))

    def test_pad_band_gate(self):
        # Jack 2026-08-02: no pads on markets outside the 5-90 series band —
        # extreme-mid and one-sided books get no qualification depth.
        self.assertTrue(imm.pad_band_ok("KXGOOD", 40, 60))     # mid 50
        self.assertTrue(imm.pad_band_ok("KXGOOD", 5, 9))       # mid 7
        self.assertFalse(imm.pad_band_ok("KXGOOD", 2, 4))      # mid 3 < 5
        self.assertFalse(imm.pad_band_ok("KXGOOD", 93, 97))    # mid 95 > 90
        self.assertFalse(imm.pad_band_ok("KXGOOD", None, 50))  # one-sided
        self.assertFalse(imm.pad_band_ok("KXGOOD", 50, None))

    def test_pad_missing_side_for_members(self):
        # Coverage-leak fix (2026-08-02): quoting only an ask rung still pads
        # the BID side to target for members — a one-sided-qualified snapshot
        # pays nobody, so the ask rung was pure fill risk. Flag off = old
        # single-side behavior (reduce-only tails).
        bot = self._bot()
        nt = [imm.Quote("T", "ask", 55, 20)]
        yes_lv, no_lv = [[40, 100]], [[45, 1500]]   # yes thin, no deep
        on = bot._pad_quotes("T", nt, yes_lv, no_lv, [], 1000.0,
                             pad_missing_side=True, ext_bid=40, ext_ask=55)
        self.assertTrue(any(q.book_side == "bid" and q.is_pad for q in on))
        off = bot._pad_quotes("T", nt, yes_lv, no_lv, [], 1000.0,
                              ext_bid=40, ext_ask=55)
        self.assertFalse(any(q.book_side == "bid" for q in off))

    def test_pad_distance_gates(self):
        # Jack 2026-08-03 "dont pad if 1c is the top of the book": a pad
        # must rest >= 2 ticks behind its side's external touch, per side.
        bot = self._bot()
        nt = [imm.Quote("T", "bid", 2, 20), imm.Quote("T", "ask", 97, 20)]
        yes_lv, no_lv = [[2, 50]], [[2, 50]]        # both sides thin
        # bid touch 2c: 1c pad would sit 1 tick behind -> no bid pad;
        # ask touch 98c: 99c pad 1 behind -> no ask pad
        pads = bot._pad_quotes("T", nt, yes_lv, no_lv, [], 1000.0,
                               pad_missing_side=True, ext_bid=2, ext_ask=98)
        self.assertEqual(pads, [])
        # healthy distances: both pads allowed
        pads = bot._pad_quotes("T", nt, yes_lv, no_lv, [], 1000.0,
                               pad_missing_side=True, ext_bid=10, ext_ask=90)
        self.assertEqual({q.book_side for q in pads}, {"bid", "ask"})
        # unknown touch = no pad on that side
        pads = bot._pad_quotes("T", nt, yes_lv, no_lv, [], 1000.0,
                               pad_missing_side=True, ext_bid=None, ext_ask=90)
        self.assertEqual({q.book_side for q in pads}, {"ask"})

    def test_dry_amend_updates_sim_order(self):
        # amend executor (2026-08-02): dry mode mutates the sim order in
        # place — same id, new price/size — mirroring the live V2 amend.
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)
        oid, so = next(iter(bot.state.sim_orders.items()))
        q = imm.Quote(so["ticker"], so["book_side"], 33, 7)
        self.assertTrue(bot.amend_order_inplace(
            dict(so, order_id=oid), q, time.time()))
        self.assertEqual(bot.state.sim_orders[oid]["yes_price"], 33)
        self.assertEqual(bot.state.sim_orders[oid]["remaining_count"], 7.0)
        self.assertIn(oid, bot.state.sim_orders)      # same id survives

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
        # KXINTC is company family — this test is about cutoff mechanics, so
        # exempt the fixture from the (env-restorable) gates and from the
        # 2026-08-02 re-entry $2/day rate floor (which outranks curation).
        old_no_new = imm.NO_NEW_SERIES
        imm.NO_NEW_SERIES = frozenset()
        old_freeze = imm.FREEZE_SERIES
        imm.FREEZE_SERIES = frozenset()
        old_ov = imm.SERIES_OVERRIDES.get("KXINTC")
        imm.SERIES_OVERRIDES["KXINTC"] = imm.SeriesOverride()
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
            if old_ov is None:
                imm.SERIES_OVERRIDES.pop("KXINTC", None)
            else:
                imm.SERIES_OVERRIDES["KXINTC"] = old_ov


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
        # dated-observation FT/APP families enroll by family (2026-09-01,
        # after 12 new *APP series appeared overnight)
        self.assertEqual(self._classify("KXGROKAPP", "KXGROKAPP-26OCT08-T500")[0],
                         "enroll")
        self.assertEqual(self._classify("KXWINGSFT",
                                        "KXWINGSFT-26OCT08-T104.5")[0],
                         "enroll")
        # ...but the suffix alone is not membership: person-tail events stay out
        self.assertEqual(self._classify("KXNFLDRAFT",
                                        "KXNFLDRAFT-26APR30-JSMITH")[0],
                         "review")

    def test_state_gas_prefix_allow_and_family_override(self):
        # A state never seen before is allowed by the KXAAAGASD prefix...
        self.assertTrue(IncentiveMarketMaker._allowed(
            "KXAAAGASDOH-26SEP02-3.1500"))
        # ...and clones the national guard set (safe-join + rate floor +
        # AAA blackout) on first sight.
        fake = "KXAAAGASDZZ"
        self.assertNotIn(fake, imm.SERIES_OVERRIDES)
        try:
            imm.ensure_family_override(fake)
            self.assertTrue(imm.series_safe_join(fake))
            self.assertEqual(imm.series_min_est_rate(fake),
                             imm.series_min_est_rate("KXAAAGASD"))
            self.assertEqual(imm.SERIES_OVERRIDES[fake].blackout_et,
                             imm.SERIES_OVERRIDES["KXAAAGASD"].blackout_et)
        finally:
            imm.SERIES_OVERRIDES.pop(fake, None)

    def test_event_top_n_gas_cap(self):
        # Jack 2026-09-02: gas events quote only the 3 highest-ROI markets
        # (all strikes settle on the same AAA print — correlated inventory).
        def m(t, est, expo=10.0, coll=0.0):
            return imm.MarketMeta(
                ticker=t, event_ticker=t.rsplit("-", 1)[0],
                series=t.split("-")[0], dollars_per_day=20.0,
                program_end=None, target_size=1000, discount_factor=0.5,
                cutoff=None, close_time=None, est_dollars_per_day=est,
                est_exposure_dollars=expo, est_collateral_dollars=coll)
        gas = [m("KXAAAGASDIL-26SEP03-4.20", 2.0),
               m("KXAAAGASDIL-26SEP03-4.21", 3.0),
               m("KXAAAGASDIL-26SEP03-4.22", 1.0),
               m("KXAAAGASDIL-26SEP03-4.23", 4.0),
               m("KXAAAGASDIL-26SEP03-4.24", 0.5)]
        rain = [m("KXRAINNYC-26SEP03-X", 0.01) for _ in range(5)]
        cut = imm.event_top_n_cut(gas + rain, incumbent=set())
        # keep ROI 0.4/0.3/0.2 -> cut the 1.0 and 0.5 est markets; rain
        # (uncapped series) untouched however weak.
        self.assertEqual(cut, {"KXAAAGASDIL-26SEP03-4.22",
                               "KXAAAGASDIL-26SEP03-4.24"})
        # national daily + weekly + states all resolve to N=3 via the
        # KXAAAGAS prefix; non-gas families are uncapped.
        for s in ("KXAAAGASD", "KXAAAGASDOH", "KXAAAGASW", "KXAAAGASM"):
            self.assertEqual(imm.event_top_n_for(s), 3, s)
        for s in ("KXRAINNYC", "KXDIESELD", "KXBKFT", "KXCLAUDEAPP"):
            self.assertEqual(imm.event_top_n_for(s), 0, s)
        # incumbency (1.15x) holds a member's slot on a near-tie: fresh 2.05
        # vs member 2.0 -> member ROI 0.2*1.15=0.23 beats 0.205.
        tie = [m("KXAAAGASDTX-26SEP03-3.60", 2.0),
               m("KXAAAGASDTX-26SEP03-3.61", 2.05),
               m("KXAAAGASDTX-26SEP03-3.62", 5.0),
               m("KXAAAGASDTX-26SEP03-3.63", 5.0)]
        cut = imm.event_top_n_cut(tie, incumbent={"KXAAAGASDTX-26SEP03-3.60"})
        self.assertEqual(cut, {"KXAAAGASDTX-26SEP03-3.61"})
        # exposure fallback: no exposure -> plain collateral ranks it; no
        # denominator at all -> ROI 0 (cut first).
        fb = [m("KXAAAGASDCA-26SEP03-5.60", 4.0, expo=0.0, coll=8.0),
              m("KXAAAGASDCA-26SEP03-5.61", 4.0),
              m("KXAAAGASDCA-26SEP03-5.62", 4.0, expo=0.0, coll=0.0),
              m("KXAAAGASDCA-26SEP03-5.63", 1.0)]
        cut = imm.event_top_n_cut(fb, incumbent=set())
        self.assertEqual(cut, {"KXAAAGASDCA-26SEP03-5.62"})
        # a group at/below N is never touched
        self.assertEqual(imm.event_top_n_cut(gas[:3], set()), set())
        # env spec sanity
        self.assertEqual(imm._parse_event_top_n("KXAAAGAS:3,KXDIESEL:2"),
                         (("KXAAAGAS", 3), ("KXDIESEL", 2)))
        with self.assertRaises(ValueError):
            imm._parse_event_top_n("KXAAAGAS")

    def test_family_override_suffix_requires_membership(self):
        # *FT suffix alone (KXNFLDRAFT is never allowlisted) clones nothing...
        try:
            imm.ensure_family_override("KXNFLDRAFT")
            self.assertNotIn("KXNFLDRAFT", imm.SERIES_OVERRIDES)
            # ...while an extra-allow member inherits the company archetype.
            imm.EXTRA_ALLOW_SERIES.add("KXZZFT")
            imm.ensure_family_override("KXZZFT")
            self.assertTrue(imm.series_safe_join("KXZZFT"))
            self.assertEqual(imm.SERIES_OVERRIDES["KXZZFT"],
                             imm.SERIES_OVERRIDES["KXBKFT"])
            # consumer-observation families carry NO fresh-candidate rate
            # bar (2026-09-01 "start quoting things as if normal") — but
            # keep safe-join; gas keeps the national $2 bar.
            for s in ("KXZZFT", "KXBKFT", "KXCLAUDEAPP"):
                self.assertEqual(imm.series_min_est_rate(s), 0.0, s)
            self.assertEqual(imm.series_min_est_rate("KXAAAGASD"), 2.0)
        finally:
            imm.EXTRA_ALLOW_SERIES.discard("KXZZFT")
            imm.SERIES_OVERRIDES.pop("KXZZFT", None)
            imm.SERIES_OVERRIDES.pop("KXNFLDRAFT", None)

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


class TestUnknownEarningsTimeFailsSafe(unittest.TestCase):
    """KXEARNINGSMENTIONCELH-26AUG06 (2026-08-06).

    Nasdaq's earnings calendar carried NO time flag for Celsius, the resolver's
    else-branch synthesized 16:00 ET, Celsius in fact reported BEFORE the open
    with an 8:00am ET call, and the bot quoted 15 markets from midnight straight
    through that call with the Q2 results already public. Jack stopped it by
    hand at 12:31Z.

    The two ways of being wrong are not symmetric, and this is the test that
    pins the direction: guess before-open when the truth is after-close and the
    bot stands down early, forfeiting a day of reward accrual; guess after-close
    when the truth is before-open and the bot makes markets into a print."""

    def setUp(self):
        import imm_earnings_overrides as ieo
        self.ieo = ieo
        # the real run that wrote the bad value: 2026-08-02 16:45 ET
        self.now = datetime(2026, 8, 2, 20, 45, tzinfo=timezone.utc)
        ieo._nasdaq_cache.clear()
        for i in range(9):                      # calendar known-empty by default
            d = (self.now + timedelta(days=i)).astimezone(ieo.ET).date()
            ieo._nasdaq_cache[d.isoformat()] = {}

    def tearDown(self):
        self.ieo._nasdaq_cache.clear()

    def _on(self, days, **flags):
        d = (self.now + timedelta(days=days)).astimezone(self.ieo.ET).date()
        self.ieo._nasdaq_cache[d.isoformat()] = dict(flags)
        return d

    def test_no_time_supplied_lands_in_the_morning_not_after_the_close(self):
        d = self._on(4, CELH="time-not-supplied")     # the literal CELH row
        hit = self.ieo.nasdaq_release_datetime("CELH", self.now, 7)
        self.assertIsNotNone(hit)
        dt_et, label = hit
        et = dt_et.astimezone(self.ieo.ET)
        self.assertEqual((et.date(), et.hour), (d, 7))
        # and it must be RECORDED as a guess: a fail-safe 07:00 and a measured
        # pre-market 07:00 are byte-identical in the overrides file, so the
        # label is the only channel the digest has for telling them apart.
        self.assertEqual(self.ieo.provenance_of(label), "guess")

    def test_a_missing_time_FIELD_is_the_same_guess(self):
        # nasdaq_earnings_for_date does `row.get("time") or ""`, so an absent
        # field arrives as "" — falsy but NOT None, so it clears the
        # `if flag is None: continue` guard and reaches the same branch.
        self._on(2, FOO="")
        dt_et, label = self.ieo.nasdaq_release_datetime("FOO", self.now, 7)
        self.assertEqual(dt_et.astimezone(self.ieo.ET).hour, 7)
        self.assertEqual(self.ieo.provenance_of(label), "guess")

    def test_the_failsafe_cutoff_bites_before_the_call_it_missed(self):
        # end to end on the real numbers: 07:00 ET minus the bot's override
        # buffer must land before Celsius's 8:00am ET call. The old 16:00 put
        # the cutoff at 15:50 ET — nearly eight hours of quoting into the news.
        self._on(4, CELH="time-not-supplied")
        dt_et = self.ieo.nasdaq_release_datetime("CELH", self.now, 7)[0]
        cutoff = dt_et - timedelta(minutes=imm.OVERRIDE_BUFFER_MIN)
        self.assertLess(cutoff, self.ieo.ET.localize(datetime(2026, 8, 6, 8, 0)))

    def test_measured_flags_are_untouched_and_count_as_measurements(self):
        # the fail-safe must not swallow the 28 genuine after-close readings
        self._on(1, AAA="time-after-hours", BBB="time-pre-market")
        amc = self.ieo.nasdaq_release_datetime("AAA", self.now, 7)
        bmo = self.ieo.nasdaq_release_datetime("BBB", self.now, 7)
        self.assertEqual(amc[0].astimezone(self.ieo.ET).hour, 16)
        self.assertEqual(bmo[0].astimezone(self.ieo.ET).hour, 7)
        self.assertEqual(self.ieo.provenance_of(amc[1]), "read")
        self.assertEqual(self.ieo.provenance_of(bmo[1]), "read")

    def test_absent_from_the_calendar_is_still_None_not_a_guess(self):
        # "not on the calendar" and "on the calendar without a time" are
        # different unknowns and must not collapse into one another: None sends
        # the event to the IR scrape and then to the UNRESOLVED email.
        self.assertIsNone(self.ieo.nasdaq_release_datetime("NOPE", self.now, 7))


class TestProvisionalGuessRecheck(unittest.TestCase):
    """A fail-safe guess must stay OPEN until somebody measures it.

    The `covered` short-circuit made every written value permanent. Nasdaq
    filled CELH in as "time-pre-market" within days of guessing on it — the
    right answer sat in the same endpoint the resolver already calls — and the
    thirteen scheduled runs in between each skipped the event without a line of
    log output. A measurement stays taken; a guess gets re-opened."""

    EV = "KXEARNINGSMENTIONCELH-26AUG06"
    GUESS = "2026-08-06T07:00:00-04:00"

    def setUp(self):
        import imm_earnings_overrides as ieo
        self.ieo = ieo
        self.dir = tempfile.mkdtemp(prefix="imm_meta_")
        self.old_meta = ieo.OVERRIDE_META_FILE
        ieo.OVERRIDE_META_FILE = os.path.join(self.dir, "meta.json")

    def tearDown(self):
        self.ieo.OVERRIDE_META_FILE = self.old_meta

    def _meta(self, **rec):
        with open(self.ieo.OVERRIDE_META_FILE, "w", encoding="utf-8") as f:
            json.dump({self.EV: rec}, f)

    def test_a_standing_guess_is_reopened(self):
        self._meta(iso=self.GUESS, confidence="guess",
                   label="time n/a->7am ET BMO assumed (Nasdaq, fail-safe)")
        self.assertIn(self.EV,
                      self.ieo.provisional_events({self.EV: self.GUESS}))

    def test_a_measurement_is_never_reopened(self):
        self._meta(iso=self.GUESS, confidence="read",
                   label="before open (~7am ET, Nasdaq)")
        self.assertEqual(self.ieo.provisional_events({self.EV: self.GUESS}),
                         set())

    def test_a_hand_set_supersedes_and_can_never_be_overwritten(self):
        # Jack --set CELH to 07:00 at 08:41 on 2026-08-06. record_meta files a
        # --set as a reading, which is what takes it out of the re-check pool.
        self.ieo.record_meta([(self.EV, self.GUESS, "hand-set by operator")],
                             {self.EV: self.GUESS})
        self.assertEqual(self.ieo.load_meta()[self.EV]["confidence"], "read")
        self.assertEqual(self.ieo.provisional_events({self.EV: self.GUESS}),
                         set())

    def test_a_guess_whose_value_moved_is_not_ours_to_move_again(self):
        self._meta(iso=self.GUESS, confidence="guess", label="time n/a")
        live = {self.EV: "2026-08-06T08:30:00-04:00"}     # somebody edited it
        self.assertEqual(self.ieo.provisional_events(live), set())

    def test_the_guess_label_survives_the_round_trip_as_a_guess(self):
        # provenance_batch decodes the phase tuples; if the bracketed label is
        # lost the guess silently records as a measurement and never re-checks.
        rows = self.ieo.provenance_batch(
            [(self.EV, self.GUESS, "nasdaq:CELH",
              "call cutoff = earnings RELEASE [time n/a->7am ET BMO assumed "
              "(Nasdaq, fail-safe)] (safe: call is at/after the release)")],
            [], [], [])
        self.ieo.record_meta(rows, {self.EV: self.GUESS})
        self.assertEqual(self.ieo.load_meta()[self.EV]["confidence"], "guess")
        self.assertIn(self.EV,
                      self.ieo.provisional_events({self.EV: self.GUESS}))

    def test_pruning_drops_events_the_overrides_file_no_longer_has(self):
        self.ieo.record_meta([(self.EV, self.GUESS, "time n/a fail-safe")],
                             {self.EV: self.GUESS})
        self.ieo.record_meta([], {})              # event gone from the file
        self.assertEqual(self.ieo.load_meta(), {})


class TestDigestUnverifiedCallTimes(unittest.TestCase):
    """send_imm_digest's CUTOFF AUDIT compares ET DATES. CELH's override date
    (Aug 6) MATCHED its ticker date (Aug 6) and was wrong by nine HOURS, so the
    audit skipped it at `if delta == 0: continue` and the 7:10am email said
    nothing. These tests pin the hour dimension, and pin that it stays quiet."""

    CELH = "KXEARNINGSMENTIONCELH-26AUG06"
    ABNB = "KXEARNINGSMENTIONABNB-26AUG06"
    BAD = "2026-08-06T16:00:00-04:00"          # what the resolver wrote

    class _Client:
        def __init__(self, events):
            self.events = events

        def get(self, path, params=None):
            if "incentive_programs" not in path:
                return {}
            progs = []
            for ev in self.events:
                for i in range(2):
                    progs.append({
                        "incentive_type": "liquidity", "paid_out": False,
                        "market_ticker": "{}-M{}".format(ev, i),
                        "start_date": "2026-08-05T00:00:00Z",
                        "end_date": "2026-08-07T00:00:00Z",
                        "period_reward": 6000000})
            return {"incentive_programs": progs, "next_cursor": None}

    def setUp(self):
        import send_imm_digest as sd
        self.sd = sd
        self.now = datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc)
        self.old_meta = sd.OVERRIDE_META_PATH
        self.dir = tempfile.mkdtemp(prefix="imm_digest_")
        sd.OVERRIDE_META_PATH = os.path.join(self.dir, "meta.json")
        self.client = self._Client([self.CELH, self.ABNB])

    def tearDown(self):
        self.sd.OVERRIDE_META_PATH = self.old_meta
        self._overrides({})                       # unload from the live dict
        imm.load_file_event_overrides()

    def _overrides(self, data):
        with open(imm.EVENT_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        imm._file_override_state["mtime"] = 0.0

    def _meta(self, data):
        with open(self.sd.OVERRIDE_META_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _audit(self, pos=None):
        return self.sd.cutoff_audit(self.client, self.now,
                                    pos or {self.CELH + "-M0": 240})

    def _flat(self, a):
        """The text block as one whitespace-normalized string. The block is
        wrapped to 76 cols, so a phrase assertion on the raw join would break
        purely on where a line happens to end."""
        return " ".join(" ".join(self.sd._unverified_lines(a)).split())

    def test_a_guessed_hour_surfaces_even_though_the_DATE_agrees(self):
        self._overrides({self.CELH: self.BAD, self.ABNB: self.BAD})
        self._meta({
            self.CELH: {"iso": self.BAD, "confidence": "guess",
                        "label": "time n/a->4pm ET (Nasdaq)"},
            self.ABNB: {"iso": self.BAD, "confidence": "read",
                        "label": "after close (4pm ET, Nasdaq)"}})
        a = self._audit()
        self.assertIsNone(a["error"])
        # the DATE audit is blind to this — that is the bug, asserted
        self.assertNotIn(self.CELH, [r["event"] for r in a["rows"]])
        flagged = [r["event"] for r in a["unverified"]]
        self.assertEqual(flagged, [self.CELH])
        self.assertTrue(a["unverified"][0]["unsafe"])
        self.assertEqual(a["unverified"][0]["contracts"], 240)
        self.assertIn("CELH", self.sd.cutoff_banner(a))

    def test_a_measured_after_close_hour_is_never_flagged(self):
        # 28 of the 29 live 16:00 overrides are a real Nasdaq after-close flag.
        # Flagging on hour-shape would ship 29 rows to catch one and the block
        # would be skimmed on the morning it matters.
        self._overrides({self.ABNB: self.BAD})
        self._meta({self.ABNB: {"iso": self.BAD, "confidence": "read",
                                "label": "after close (4pm ET, Nasdaq)"}})
        a = self._audit(pos={})
        self.assertEqual(a["unverified"], [])
        self.assertEqual(self.sd.cutoff_banner(a), "")

    def test_a_hand_set_value_stops_being_flagged_the_same_morning(self):
        # the sidecar records the ISO it describes; once --set moves the value
        # the record no longer applies and the row must go quiet, or a fixed
        # guess stays red forever and trains the reader to skip the block.
        fixed = "2026-08-06T07:00:00-04:00"
        self._overrides({self.CELH: fixed})
        self._meta({self.CELH: {"iso": self.BAD, "confidence": "guess",
                                "label": "time n/a->4pm ET (Nasdaq)"}})
        a = self._audit()
        self.assertEqual(a["unverified"], [])
        self.assertEqual(self.sd.cutoff_banner(a), "")
        self.assertGreaterEqual(a["no_prov"], 1)

    def test_a_failsafe_morning_guess_is_reported_but_never_shouted(self):
        # post-fix world: the guess lands at 07:00, the bot already stands down
        # early, so the row prints for the record and the banner stays silent.
        safe = "2026-08-06T07:00:00-04:00"
        self._overrides({self.CELH: safe})
        self._meta({self.CELH: {
            "iso": safe, "confidence": "guess",
            "label": "time n/a->7am ET BMO assumed (Nasdaq, fail-safe)"}})
        # 05:00 ET — before the 06:50 ET cutoff, i.e. while there is still
        # something to act on. That the SAME row disappears once 06:50 passes
        # is asserted by test_a_passed_cutoff_is_history_and_drops_out.
        a = self.sd.cutoff_audit(
            self.client, datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
            {self.CELH + "-M0": 240})
        self.assertEqual([r["event"] for r in a["unverified"]], [self.CELH])
        self.assertFalse(a["unverified"][0]["unsafe"])
        self.assertEqual(self.sd.cutoff_banner(a), "")
        self.assertTrue(any("UNVERIFIED CALL TIMES" in ln
                            for ln in self.sd._unverified_lines(a)))

    def test_a_passed_cutoff_is_history_and_drops_out(self):
        self._overrides({self.CELH: self.BAD})
        self._meta({self.CELH: {"iso": self.BAD, "confidence": "guess",
                                "label": "time n/a->4pm ET (Nasdaq)"}})
        a = self.sd.cutoff_audit(
            self.client, datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc), {})
        self.assertEqual(a["unverified"], [])

    def test_missing_provenance_reports_its_own_coverage_not_a_clean_bill(self):
        # an empty section must not look the same as a clean one
        self._overrides({self.CELH: self.BAD})
        self._meta({})
        a = self._audit()
        self.assertEqual(a["unverified"], [])
        line = self._flat(a)
        self.assertIn("0/{}".format(a["checked"]), line)
        self.assertIn("NOT covered by this check", line)

    def test_coverage_is_stated_as_a_fraction_at_every_level(self):
        """The honesty signal must not evaporate at 1/N coverage.

        The old copy gated "NOT yet effective" on no_prov >= checked, so a single
        new resolution flipped the section to "...the rest were measured, not
        guessed" — reassurance while the check could see 2 of 62, with CELH's own
        class ("written before provenance recording") listed among benign causes.
        """
        self._overrides({self.CELH: self.BAD, self.ABNB: self.BAD})
        self._meta({self.ABNB: {"iso": self.BAD, "confidence": "read",
                                "label": "after close (4pm ET, Nasdaq)"}})
        a = self._audit(pos={})
        self.assertEqual(a["unverified"], [])            # nothing to shout about
        line = self._flat(a)
        self.assertIn("1/{}".format(a["checked"]), line)
        # never closes on reassurance while most of the file is invisible
        self.assertNotIn("the rest were measured", line)
        self.assertIn("NOT covered by this check", line)
        # and it must not promise a convergence the writer cannot deliver:
        # imm_earnings_overrides.py's `covered` short-circuit never rewrites an
        # existing entry, so the unrecorded set does not fill in on its own.
        self.assertIn("does not fill in on its own", line)
        self.assertNotIn("fills in as", line)

    def test_the_coverage_line_survives_into_the_rows_branch_and_the_html(self):
        safe = "2026-08-06T07:00:00-04:00"
        self._overrides({self.CELH: safe, self.ABNB: self.BAD})
        self._meta({self.CELH: {
            "iso": safe, "confidence": "guess",
            "label": "time n/a->7am ET BMO assumed (Nasdaq, fail-safe)"}})
        a = self.sd.cutoff_audit(
            self.client, datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc), {})
        self.assertEqual([r["event"] for r in a["unverified"]], [self.CELH])
        self.assertIn("NOT covered by this check", self._flat(a))
        self.assertIn("NOT covered by this check", self.sd._unverified_html(a))

    def _meaning(self, unsafe: bool):
        return self.sd._unverified_meaning({
            "event": self.CELH, "dpd": 401.0, "unsafe": unsafe,
            "override_et": datetime(2026, 8, 20, 7 if not unsafe else 16, 0),
            "cutoff_et": datetime(2026, 8, 20, 6 if not unsafe else 15, 50)})

    def test_the_safe_side_row_forbids_the_accrual_chasing_edit(self):
        """Post-fix this is the section's ONLY steady-state output.

        Every future guess lands at 07:00, so this row is what Jack reads at
        7:10am on an ordinary morning. The old copy ended "Confirm only if you
        want the $401/day back" — a dangled dollar figure, no prohibition, no
        source named. Acting on it means pushing a safe 07:00 BMO guess out to
        16:00, i.e. re-creating CELH by hand."""
        why, action = self._meaning(unsafe=False)
        blob = (why + " " + action).lower()
        self.assertIn("do not push this override later", blob)
        self.assertIn("celh", blob)                       # names the incident
        self.assertIn("ir page", blob)                    # names a PRIMARY source
        # and disowns the source that already returned nothing here
        self.assertIn("not nasdaq", blob.replace("—", "").replace("  ", " "))
        self.assertNotIn("confirm only if you want", blob)
        self.assertTrue(action.startswith("ACTION: none"))

    def test_the_unsafe_side_row_still_asks_for_the_fail_safe_set(self):
        why, action = self._meaning(unsafe=True)
        self.assertIn("SYNTHESIZED", why)
        self.assertIn("--set", action)
        self.assertIn("07:00 ET", action)

    def test_a_no_action_row_is_not_the_loudest_thing_in_the_html(self):
        """ACTION: none rendered at font-weight:600 is how a no-op becomes a
        to-do. Bold is reserved for the row that actually wants a human."""
        base = {"ticker_date": datetime(2026, 8, 20).date(),
                "days_out": 1, "mkts": 18, "contracts": 0.0, "dpd": 401.0,
                "label": "time n/a->7am ET BMO assumed (Nasdaq, fail-safe)"}
        def row(unsafe):
            r = dict(base, event=self.CELH, unsafe=unsafe)
            r["override_et"] = datetime(2026, 8, 20, 16 if unsafe else 7, 0)
            r["cutoff_et"] = datetime(2026, 8, 20, 15 if unsafe else 6, 50)
            return r
        safe_html = self.sd._unverified_html(
            {"unverified": [row(False)], "checked": 1, "no_prov": 0})
        unsafe_html = self.sd._unverified_html(
            {"unverified": [row(True)], "checked": 1, "no_prov": 0})
        self.assertIn("color:#777;font-weight:400", safe_html)
        self.assertNotIn("color:#333;font-weight:600", safe_html)
        self.assertIn("color:#333;font-weight:600", unsafe_html)


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
    """Jack 2026-07-21: KXTEMP = 5/2/2 ladder, net cap 40, quotes only 5..90c.
    2026-08-02: out 10 min before the reading (was 15) + $0.70 min-payout
    floor (sub-hour windows made the $1 global bar unreachable for flanks)."""

    def test_temp_ladder_and_caps(self):
        self.assertEqual(imm.series_levels("KXTEMPDCH"), [(0, 5), (1, 2), (2, 2)])
        self.assertEqual(imm.series_side_max("KXTEMPDCH"), 9)
        self.assertEqual(imm.series_max_position("KXTEMPDCH"), 50)
        self.assertEqual(imm.series_price_min("KXTEMPDCH"), 5)
        self.assertEqual(imm.series_price_max("KXTEMPDCH"), 90)
        ov = imm.series_override("KXTEMPDCH")
        self.assertEqual(ov.cutoff_from_close_min, 10)
        # non-temp series keep the globals
        self.assertEqual(imm.series_price_min("KXGOOD"), imm.PRICE_MIN_CENTS)
        self.assertEqual(imm.series_price_max("KXGOOD"), imm.PRICE_MAX_CENTS)

    def test_member_quotes_to_true_cutoff(self):
        # Jack 2026-08-03: the 5-min fresh-entry buffer must not kill
        # members early — a member 3 min from cutoff still screens clean;
        # a fresh candidate inside the buffer is refused.
        bot = imm.IncentiveMarketMaker(client=None, live=False)
        meta = imm.MarketMeta(
            ticker="KXGOOD-99DEC31-A", event_ticker="KXGOOD-99DEC31",
            series="KXGOOD", dollars_per_day=10.0, program_end=None,
            target_size=1000.0, discount_factor=0.5,
            cutoff=datetime.now(timezone.utc) + timedelta(minutes=3),
            close_time=datetime.now(timezone.utc) + timedelta(hours=2),
            mid_cents=50.0, spread_cents=2, volume=100.0)
        now = datetime.now(timezone.utc)
        self.assertEqual(bot._screen(meta, now, member=False), "cutoff")
        self.assertIsNone(bot._screen(meta, now, member=True))

    def test_temp_min_payout_floor(self):
        # The temp override may still win DOWNWARD against a raised global,
        # but never below PAYOUT_FLOOR_DOLLARS — the exchange pays nothing
        # under $1.00 per market per program period (2026-08-04 statement).
        self.assertEqual(imm.series_min_est_total("KXTEMPDCH"), 1.00)
        old = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 123.0
        try:
            self.assertEqual(imm.series_min_est_total("KXGOOD"), 123.0)
            self.assertEqual(imm.series_min_est_total("KXTEMPDCH"), 1.00)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old

    def test_series_override_cannot_sit_below_the_payout_floor(self):
        # A sub-$1 series bar admits markets whose OPTIMISTIC projection still
        # pays zero. Guard the class, not just the KXTEMP instance.
        import dataclasses
        ov = imm.SERIES_OVERRIDES["KXTEMPDCH"]
        try:
            imm.SERIES_OVERRIDES["KXTEMPDCH"] = dataclasses.replace(
                ov, min_est_total=0.10)
            self.assertEqual(imm.series_min_est_total("KXTEMPDCH"),
                             imm.PAYOUT_FLOOR_DOLLARS)
        finally:
            imm.SERIES_OVERRIDES["KXTEMPDCH"] = ov
        for s, o in imm.SERIES_OVERRIDES.items():
            if o.min_est_total is not None:
                self.assertGreaterEqual(
                    imm.series_min_est_total(s), imm.PAYOUT_FLOOR_DOLLARS,
                    f"{s} entry floor sits under the exchange payout minimum")

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
        # at all — not even the in-band side. Member boundary is the sticky
        # band (93 since 2026-08-03, was 98): a 96x98 top stands a member
        # down again — the Austin 96c pickoff class.
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
        self.assertEqual(bot.state.sim_orders, {})   # beyond 93: stands aside
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
            # SUSTAINED, not a dip (Jack 2026-08-05): backdate the sub-bar
            # clock past the window, otherwise the dip guard rightly holds it.
            bot.state.hopeless_since[self.T] = (
                time.time() - imm.HOPELESS_SUSTAIN_SECS - 1)
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor

    def test_hopeless_member_survives_a_dip(self):
        # The same market, WITHOUT a sustained sub-bar history, must keep
        # quoting — this is the regression that cost KXUST7AM 8 of 15 strikes.
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        try:
            bot._est_peak.clear()
            bot.state.hopeless_since.clear()   # first cycle under the bar
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
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

    def test_rate_floor_beats_curated_override(self):
        # Jack 2026-08-02 TLN audit: an auto-written disclosure override
        # curated KXTLN-26AUGGEN past the $2 bar at ~$0.45/day est. The
        # re-entry RATE floor must gate FRESH candidates even on curated
        # events (members stay sticky; the est_TOTAL bypass rationale does
        # not apply to a per-day rate).
        bot = self._quoting_bot()
        old_ov = imm.SERIES_OVERRIDES.get("KXGOOD")
        imm.SERIES_OVERRIDES["KXGOOD"] = imm.SeriesOverride(min_est_per_day=1e9)
        imm.EVENT_START_OVERRIDES["KXGOOD-99DEC31"] = utc(2099, 1, 1)
        try:
            bot.state.selected.pop(self.T, None)      # make it FRESH
            bot.state.sticky_prev.discard(self.T)
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)   # rate-floored
        finally:
            imm.EVENT_START_OVERRIDES.pop("KXGOOD-99DEC31", None)
            if old_ov is None:
                imm.SERIES_OVERRIDES.pop("KXGOOD", None)
            else:
                imm.SERIES_OVERRIDES["KXGOOD"] = old_ov

    def test_aaa_print_blackout_window(self):
        # Jack 2026-08-04: AAA posts gas AND diesel 03:18-03:36 ET (5 days
        # observed at 5s resolution). Weekly/monthly markets stay open across
        # the print and measured -14.4c/contract in hour 3.
        def at(hh, mm):
            return imm.ET.localize(
                datetime(2026, 8, 5, hh, mm)).astimezone(timezone.utc)
        for s in ("KXAAAGASD", "KXAAAGASW", "KXAAAGASM",
                  "KXDIESELD", "KXDIESELW"):
            self.assertEqual(imm.series_override(s).blackout_et,
                             ("03:05", "04:00"), s)
            self.assertFalse(imm.series_in_blackout(s, at(2, 50)), s)
            self.assertTrue(imm.series_in_blackout(s, at(3, 18)), s)
            self.assertTrue(imm.series_in_blackout(s, at(3, 36)), s)
            self.assertFalse(imm.series_in_blackout(s, at(4, 5)), s)
        # KXDIESELW keeps its $0 rate floor: the blackout must not have
        # clobbered the series override it was merged into
        self.assertEqual(imm.series_min_est_rate("KXDIESELW"), 0.0)
        # non-AAA series unaffected at the same instant
        self.assertFalse(imm.series_in_blackout("KXTEMPDCH", at(3, 20)))

    def test_close_anchored_cutoff_applies_to_both_producers(self):
        # LIVE BUG 2026-08-04: refresh_universe applied the temp close-10 rule
        # but restore_orphan_metas did not, so orphan-restored temp markets
        # quoted to the CLOSE — 7 fills landed as late as 6.7 min to close.
        # The shared tightener must produce close-10 from a raw close-time
        # cutoff, which is exactly what the restore path feeds it.
        close = utc(2026, 8, 4, 4, 0)
        got = imm.apply_series_cutoff_adjustments(
            "KXTEMPAUSH", "KXTEMPAUSH-26AUG0400", close, close_time=close)
        self.assertEqual(got, utc(2026, 8, 4, 3, 50))
        # with no cutoff at all it still anchors off the close
        got = imm.apply_series_cutoff_adjustments(
            "KXTEMPAUSH", "KXTEMPAUSH-26AUG0400", None, close_time=close)
        self.assertEqual(got, utc(2026, 8, 4, 3, 50))
        # never LOOSENS an already-tighter cutoff
        tight = utc(2026, 8, 4, 3, 30)
        got = imm.apply_series_cutoff_adjustments(
            "KXTEMPAUSH", "KXTEMPAUSH-26AUG0400", tight, close_time=close)
        self.assertEqual(got, tight)
        # non-close-anchored series unaffected
        got = imm.apply_series_cutoff_adjustments(
            "KXGOOD", "KXGOOD-99DEC31", close, close_time=close)
        self.assertEqual(got, close)

    def test_truev_print_day_market_survives_its_own_ticker_date(self):
        # Kalshi lists each KXTRUEV daily ON its print day (Jack 2026-08-24:
        # the enrollment shipped dark — the midnight-ET rule's cutoff was
        # already past the moment each market appeared). The close-anchored
        # override governs instead: refresh_universe's cutoff_from_close_min
        # branch skips trade_cutoff_utc entirely, and the shared tightener
        # yields close-60 for both producers.
        close = utc(2026, 8, 25, 21, 0)   # print day Aug 25, 5pm ET close
        got = imm.apply_series_cutoff_adjustments(
            "KXTRUEV", "KXTRUEV-26AUG25", None, close_time=close)
        self.assertEqual(got, utc(2026, 8, 25, 20, 0))
        # 60 aligns with the 1h closing screen — a resting order must never
        # outlive the screen that would refresh it
        self.assertEqual(imm.series_override("KXTRUEV").cutoff_from_close_min,
                         int(imm.MIN_HOURS_TO_CLOSE * 60))

    def test_rate_floor_horizon_escape(self):
        # Jack 2026-08-03: a fresh candidate admits on est_rate >= the series
        # bar OR a projected TOTAL >= RATE_FLOOR_TOTAL_ALT. The projection is
        # bounded by the PROGRAM end, not the market close — the foot-traffic
        # case that prompted this closes in September but its programs end
        # 8/9, so a 5-day window keeps most of them out.
        bot = self._quoting_bot()
        old_ov = imm.SERIES_OVERRIDES.get("KXGOOD")
        old_alt = imm.RATE_FLOOR_TOTAL_ALT
        # rate bar the fixture's est cannot clear
        imm.SERIES_OVERRIDES["KXGOOD"] = imm.SeriesOverride(min_est_per_day=1e9)
        try:
            # alt threshold unreachable -> still rate-floored
            imm.RATE_FLOOR_TOTAL_ALT = 1e9
            bot.state.selected.pop(self.T, None)
            bot.state.sticky_prev.discard(self.T)
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)
            # alt threshold trivially reachable -> horizon escape admits it
            imm.RATE_FLOOR_TOTAL_ALT = 0.0
            bot.state.selected.pop(self.T, None)
            bot.state.sticky_prev.discard(self.T)
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            imm.RATE_FLOOR_TOTAL_ALT = old_alt
            if old_ov is None:
                imm.SERIES_OVERRIDES.pop("KXGOOD", None)
            else:
                imm.SERIES_OVERRIDES["KXGOOD"] = old_ov

    def test_quotable_days_bounded_by_program_end(self):
        # The horizon escape is only safe because _quotable_days caps at the
        # PROGRAM end when it precedes the market close (the 8/8-vs-September
        # trap Jack flagged). Pin that.
        now = utc(2026, 8, 4, 0, 0)
        meta = imm.MarketMeta(
            ticker="KXBKFT-26SEP07-T100", event_ticker="KXBKFT-26SEP07",
            series="KXBKFT", dollars_per_day=19.05,
            program_end=utc(2026, 8, 9, 3, 59), target_size=1000.0,
            discount_factor=0.5, cutoff=None,
            close_time=utc(2026, 9, 7, 2, 29))
        self.assertAlmostEqual(imm._quotable_days(meta, now), 5.166, places=2)
        # and the market close wins when IT is the earlier bound
        meta2 = imm.MarketMeta(
            ticker="X-1", event_ticker="X", series="X", dollars_per_day=1.0,
            program_end=utc(2026, 9, 1, 0, 0), target_size=1000.0,
            discount_factor=0.5, cutoff=None,
            close_time=utc(2026, 8, 6, 0, 0))
        self.assertAlmostEqual(imm._quotable_days(meta2, now), 2.0, places=2)

    def test_force_event_bypasses_floors_and_hopeless(self):
        # Jack 2026-08-03 (TRUMPMENTION AUG18-date bug): IMM_FORCE_EVENTS
        # entries are a deliberate per-event bypass of the floors and the
        # hopeless exit — screens/caps still apply.
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        old_force = imm.FORCE_EVENTS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        imm.FORCE_EVENTS = frozenset({"KXGOOD-99DEC31"})
        try:
            bot._est_peak.clear()
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)   # forced: stays
        finally:
            imm.MIN_EST_TOTAL_DOLLARS = old_floor
            imm.FORCE_EVENTS = old_force

    def test_override_event_no_longer_exempt_from_hopeless(self):
        # 2026-08-03 (Jack "same rules as other markets", supersedes the
        # 7/28 tele-rally exemption): an event-start override no longer
        # shields a member from the hopeless exit — the override is a
        # cutoff source only.
        bot = self._quoting_bot()
        old_floor = imm.MIN_EST_TOTAL_DOLLARS
        imm.MIN_EST_TOTAL_DOLLARS = 1e9
        imm.EVENT_START_OVERRIDES["KXGOOD-99DEC31"] = utc(2099, 1, 1)
        try:
            bot._est_peak.clear()
            bot.state.hopeless_since[self.T] = (
                time.time() - imm.HOPELESS_SUSTAIN_SECS - 1)
            bot.state.universe_at = 0.0
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)   # evicted like any
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

    def test_pads_qualify_a_thin_side_only_where_padding_is_ON(self):
        # Jack 2026-07-29 made pads global so the ESTIMATOR would model them
        # on thin sides; Jack 2026-08-05 restricted padding to hourly TEMP and
        # made every other thin book a stand-down. Both halves asserted.
        import incentive_mm as _imm
        self.assertTrue(_imm.PAD_TO_TARGET_GLOBAL)       # global again
        self.assertTrue(_imm.series_pad_to_target("KXTEMPAUSH"))
        self.assertTrue(_imm.series_pad_to_target("KXGOOD"))
        thin = {"orderbook_fp": {"yes_dollars": [["0.49", "300"]],
                                 "no_dollars": [["0.49", "200"]]}}
        t = "KXGOOD-99DEC31-A"

        # padding ON -> pads modelled, thin sides qualify, market is worth something
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        _pad = _imm.PAD_TO_TARGET_GLOBAL
        _imm.PAD_TO_TARGET_GLOBAL = True
        try:
            bot.client.books[t] = json.loads(json.dumps(thin))
            bot.run_cycle()
            self.assertIn(t, bot.state.selected)
            self.assertGreater(bot.state.selected[t].est_frac, 0.0)
        finally:
            _imm.PAD_TO_TARGET_GLOBAL = _pad

        # padding OFF -> the depth gate stands the market down instead
        _clean_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        _imm.PAD_TO_TARGET_GLOBAL = False
        try:
            bot2.client.books[t] = json.loads(json.dumps(thin))
            bot2.run_cycle()
            self.assertNotIn(t, bot2.state.selected)
        finally:
            _imm.PAD_TO_TARGET_GLOBAL = _pad

    def test_pads_do_not_drain_event_room(self):
        # Padding is hourly-TEMP-only since 2026-08-05; this test's
        # SUBJECT is the pad machinery, so enable it explicitly rather
        # than relying on a default that no longer holds.
        _pad = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = True
        self.addCleanup(setattr, imm, 'PAD_TO_TARGET_GLOBAL', _pad)
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
        self.assertEqual(c, utc(2026, 7, 30, 2, 0))     # 10pm ET day before (Jack 8/15)
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

    def test_rain_cutoff_10pm_day_before(self):
        # Jack 2026-08-15: rain dailies run until 10pm ET the day BEFORE the
        # rain day (ticker-date midnight minus 120 min; was 9pm since 8/1,
        # 6pm since 7/29 — paired with the 7pm size halving).
        ov = imm.series_override("KXRAIN")
        self.assertEqual(ov.cutoff_before_event_min, 120)
        td = imm.parse_event_date("KXRAIN-26JUL30")     # Jul 30 00:00 ET
        early = td - timedelta(minutes=120)             # Jul 29 22:00 ET
        self.assertEqual(early, utc(2026, 7, 30, 2, 0))
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
        # 2026-08-03 (Jack, TRUMPMENTION AUG03 "same rules as other
        # markets"): an event-start override supplies the CUTOFF only — it
        # no longer curates past the floors/hopeless.
        imm.EVENT_START_OVERRIDES["KXFOO-26AUG01"] = now
        try:
            self.assertFalse(imm.curated_event("KXFOO-26AUG01", "KXFOO", now))
        finally:
            imm.EVENT_START_OVERRIDES.pop("KXFOO-26AUG01", None)
        # 2026-08-02 (Jack) RE-ENTRY: company/econ freeze + no-new lifted;
        # they quote again behind the $2/day rate floor + safe-join rule.
        for s in ("KXSCFI", "KXNHSALES", "KXBA", "KXHOOD", "KXAAAGASD",
                  "KXUSGASCPI"):
            self.assertNotIn(s, imm.NO_NEW_SERIES)
            self.assertNotIn(s, imm.FREEZE_SERIES)
            self.assertEqual(imm.series_min_est_rate(s), 2.0)
            self.assertTrue(imm.series_safe_join(s))
        self.assertEqual(imm.series_min_est_rate("KXGOOD"), 0.0)
        self.assertFalse(imm.series_safe_join("KXTEMPDCH"))
        # 2026-08-02 (Jack): KXRT pulled out of the econ set — entertainment
        # reveals are not macro prints; the 7/29 econ run-off had swept it
        # by config placement. Still allowed, no longer no_new'd.
        self.assertNotIn("KXRT", imm.NO_NEW_SERIES)
        self.assertTrue(imm.IncentiveMarketMaker._allowed("KXRT-X"))

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
        # Padding is hourly-TEMP-only since 2026-08-05; this test's
        # SUBJECT is the pad machinery, so enable it explicitly rather
        # than relying on a default that no longer holds.
        _pad = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = True
        self.addCleanup(setattr, imm, 'PAD_TO_TARGET_GLOBAL', _pad)
        client, ev = self._client(n_markets=1, yes_depth="300", no_depth="1200")
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        t = f"{ev}-M0"
        bid_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_BID_CENTS]
        self.assertEqual(len(bid_pad), 1)
        # basis 300 + 15 near-touch -> pad up to 1000, rounded to 100s
        self.assertEqual(bid_pad[0]["remaining_count"], 1000)   # gap 685+300 -> 1000 (within PAD_MAX)
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
        # Padding is hourly-TEMP-only since 2026-08-05; this test's
        # SUBJECT is the pad machinery, so enable it explicitly rather
        # than relying on a default that no longer holds.
        _pad = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = True
        self.addCleanup(setattr, imm, 'PAD_TO_TARGET_GLOBAL', _pad)
        client, ev = self._client(n_markets=1, yes_depth="200", no_depth="200")
        bot = IncentiveMarketMaker(client=client, live=False)
        self._use_resolver(bot)
        bot.run_cycle()
        t = f"{ev}-M0"
        bid_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_BID_CENTS]
        ask_pad = [o for o in bot.state.sim_orders.values()
                   if o["ticker"] == t and o["yes_price"] == imm.PAD_ASK_CENTS]
        self.assertEqual(bid_pad[0]["remaining_count"], 1000)  # (1000-215)+300 -> capped at PAD_MAX 1000
        self.assertEqual(ask_pad[0]["remaining_count"], 1000)

    def test_pad_nets_out_own_pad_live_stable(self):
        """With our 1000 pad already resting inside the book, the desired pad
        stays 1000 (not 0) — no self-referential churn."""
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=True)
        yes_levels = [[48, 300.0], [1, 1000.0]]  # 300 external + our 1000 pad
        no_levels = [[49, 1200.0]]
        near_touch = [imm.Quote("T", "bid", 48, 5)]
        own = [("bid", imm.PAD_BID_CENTS, 700.0)]
        pads = bot._pad_quotes("T", near_touch, yes_levels, no_levels, own, 1000,
                               ext_bid=48, ext_ask=51)
        bid_pad = [p for p in pads if p.book_side == "bid"]
        self.assertEqual(len(bid_pad), 1)
        self.assertEqual(bid_pad[0].count, 700)
        self.assertTrue(bid_pad[0].is_pad)

    def test_pad_only_on_quoted_side(self):
        """No near-touch on a side (bot not quoting it) -> no pad there."""
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        pads = bot._pad_quotes("T", [imm.Quote("T", "bid", 48, 5)],
                               [[48, 100.0]], [[49, 100.0]], [], 1000,
                               ext_bid=48, ext_ask=51)
        self.assertEqual({p.book_side for p in pads}, {"bid"})   # no ask pad


class TestPadQuantity(unittest.TestCase):
    def test_rounds_up_with_slack(self):
        # gap + 300 slack, rounded up to 100 (slack survives external
        # withdrawal between requotes — the AUG0110-T82.99 lesson)
        self.assertEqual(imm.pad_quantity(315, 1000), 1000)   # gap 685+300 -> 1000 (PAD_MAX 1k)
        self.assertEqual(imm.pad_quantity(300, 1000), 1000)   # gap 700+300 -> 1000
        self.assertEqual(imm.pad_quantity(901, 1000), 400)    # gap 99 + 300
        self.assertEqual(imm.pad_quantity(900, 1000), 400)    # gap 100 + 300

    def test_at_or_over_target_no_pad(self):
        # no slack when the side already reaches target on its own
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

    def test_no_pre_cutoff_reduce_only_by_default(self):
        # Jack 2026-08-02: the pre-cutoff reduce-only window is OFF bot-wide
        # (default 0) — both sides keep quoting up to the cutoff; the
        # exchange-side expiration cap at the cutoff is the remaining guard.
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.run_cycle()
        bot.state.selected[t].cutoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertTrue(bot.state.sim_orders)   # still quoting inside 30min

    def test_pre_cutoff_reduce_only_env_restorable(self):
        # IMM_PRE_CUTOFF_REDUCE_ONLY > 0 restores the old behavior: inside
        # the window and flat -> nothing rests.
        bot = self._bot()
        t = "KXGOOD-99DEC31-A"
        bot.run_cycle()
        old = imm.PRE_CUTOFF_REDUCE_ONLY_SECS
        imm.PRE_CUTOFF_REDUCE_ONLY_SECS = 3600
        try:
            bot.state.selected[t].cutoff = \
                datetime.now(timezone.utc) + timedelta(minutes=30)
            bot.state.universe_at = time.time()
            bot.run_cycle()
            self.assertEqual(bot.state.sim_orders, {})
        finally:
            imm.PRE_CUTOFF_REDUCE_ONLY_SECS = old

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
        # Mechanism test with a PATCHED mult — the default is 1.0 since
        # 2026-07-30 (Jack removed the 7/28 x1.5; family runs the global 10s).
        self.assertEqual(imm.MENTION_SIZE_MULT, 1.0)
        self._old_mult = imm.MENTION_SIZE_MULT
        imm.MENTION_SIZE_MULT = 1.5
        self.addCleanup(lambda: setattr(imm, "MENTION_SIZE_MULT", self._old_mult))
        imm.HOUR_SIZE_MULTS = {}
        outside = utc(2026, 7, 28, 13, 0)      # 9am ET, no hour mult
        base = imm.series_levels("KXWCMENTION")
        self.assertEqual(imm.hour_scaled_levels("KXWCMENTION", outside),
                         [(t, int(s * 1.5 + 0.5)) for t, s in base])
        self.assertEqual(imm.hour_scaled_levels("KXEARNINGSMENTIONF", outside),
                         [(t, int(s * 1.5 + 0.5)) for t, s in base])
        # non-mention untouched
        self.assertIs(imm.hour_scaled_levels("KXGOOD", outside),
                      imm.series_levels("KXGOOD"))
        # composes with the quiet-hours window: x1.5 x2
        imm.HOUR_SIZE_MULTS = {3: 2.0}
        inside = utc(2026, 7, 28, 7, 30)       # 3:30am ET
        self.assertEqual(imm.hour_scaled_levels("KXWCMENTION", inside),
                         [(t, int(s * 3.0 + 0.5)) for t, s in base])

    def test_mention_caps_scale_commensurately(self):
        self.assertEqual(imm.MENTION_SIZE_MULT, 1.0)   # default off since 7/30
        self._old_mult = imm.MENTION_SIZE_MULT
        imm.MENTION_SIZE_MULT = 1.5
        self.addCleanup(lambda: setattr(imm, "MENTION_SIZE_MULT", self._old_mult))
        self.assertAlmostEqual(imm.series_max_position("KXWCMENTION"),
                               imm.MAX_POSITION_CONTRACTS * 1.5)
        # main TRUMPMENTION: hand-tuned 0:10 RETIRED 2026-08-03 (Jack "same
        # rules as other markets") -> global ladder, family mult applies
        self.assertEqual(imm.series_levels("KXTRUMPMENTION"), imm.LEVELS)
        self.assertEqual(imm.applied_mention_mult("KXTRUMPMENTION"), 1.5)
        self.assertAlmostEqual(imm.series_max_position("KXGOOD"),
                               imm.MAX_POSITION_CONTRACTS)
        # temp override cap unaffected (not mention)
        self.assertEqual(imm.series_max_position("KXTEMPDCH"), 50)
        # hand-tuned override series exempt even though it IS mention:
        # Love Island keeps its literal 7/12 spec (5/5/5, net 50)
        self.assertEqual(imm.series_max_position("KXLOVEISLMENTION"), 50)
        self.assertEqual(imm.applied_mention_mult("KXLOVEISLMENTION"), 1.0)
        self.assertAlmostEqual(
            imm.event_cap_contracts("KXWCMENTION-26JUL24ARGSUI"),
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
        # KXRAIN halves its ladder 16:00-01:59 ET (Jack 2026-08-04), and these
        # assertions are on literal rung sizes against the wall clock — so the
        # whole class failed from 4pm ET onward. Neutralise the hour windows;
        # their own coverage lives in TestPerSeriesHourMultiplier.
        self._saved_hour_mults = imm.SERIES_HOUR_MULTS
        imm.SERIES_HOUR_MULTS = []
        try:
            os.remove(imm.RAIN_FAIR_FILE)
        except FileNotFoundError:
            pass

    def tearDown(self):
        imm.SERIES_HOUR_MULTS = self._saved_hour_mults

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


class TestPayoutFloorAccounting(unittest.TestCase):
    """The exchange pays NOTHING for a market whose program-period payout
    lands under $1.00 (2026-08-04 statement: 2,720 LIQUIDITY credits, minimum
    exactly $1.00, none below). The raw integral therefore books revenue on markets
    that pay zero; the paid-basis counters apply that floor."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def _accrue(self, bot, rates, dt_days):
        """Drive the accrual arithmetic directly (run_cycle's inner block)."""
        paid_delta = 0.0
        for t, rate in rates.items():
            prev = bot.state.accrued_est.get(t, 0.0)
            new = prev + rate * dt_days
            bot.state.accrued_est[t] = new
            if t in bot.state.paid_crossed:
                paid_delta += new - prev
            elif new >= imm.PAYOUT_FLOOR_DOLLARS:
                bot.state.paid_crossed.add(t)
                paid_delta += new
        bot.state.reward_paid_today += paid_delta
        bot.state.reward_paid_lifetime += paid_delta

    def test_market_under_the_floor_never_counts(self):
        bot = self._bot()
        try:
            self._accrue(bot, {"A": 0.90}, 1.0)     # $0.90 -> pays zero
            self.assertAlmostEqual(bot.state.accrued_est["A"], 0.90)
            self.assertEqual(bot.state.reward_paid_today, 0.0)
            self.assertEqual(bot.state.reward_paid_lifetime, 0.0)
        finally:
            _clean_persist()

    def test_whole_backlog_lands_on_the_crossing_cycle(self):
        bot = self._bot()
        try:
            self._accrue(bot, {"A": 0.60}, 1.0)     # 0.60, still nothing
            self.assertEqual(bot.state.reward_paid_today, 0.0)
            self._accrue(bot, {"A": 0.60}, 1.0)     # 1.20 -> crosses
            self.assertAlmostEqual(bot.state.reward_paid_today, 1.20)
            self._accrue(bot, {"A": 0.50}, 1.0)     # already paid: increment
            self.assertAlmostEqual(bot.state.reward_paid_today, 1.70)
            self.assertAlmostEqual(bot.state.reward_paid_lifetime, 1.70)
            self.assertAlmostEqual(bot.state.accrued_est["A"], 1.70)
        finally:
            _clean_persist()

    def test_paid_basis_is_never_above_the_raw_integral(self):
        bot = self._bot()
        try:
            rates = {"A": 3.0, "B": 0.4, "C": 1.0, "D": 0.05}
            self._accrue(bot, rates, 1.0)
            raw = sum(bot.state.accrued_est.values())
            self.assertLessEqual(bot.state.reward_paid_lifetime, raw + 1e-9)
            # only A and C cleared $1
            self.assertAlmostEqual(bot.state.reward_paid_lifetime, 4.0)
        finally:
            _clean_persist()

    def test_crossed_set_persists_so_restarts_do_not_recredit(self):
        """paid_crossed MUST round-trip with accrued_est: without it the first
        cycle after a restart re-credits every carried market's whole
        backlog — one full book of phantom reward per restart, and this bot
        restarts many times a day."""
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.known_tickers = {"A"}
            self._accrue(bot, {"A": 2.0}, 1.0)
            self.assertAlmostEqual(bot.state.reward_paid_lifetime, 2.0)
            bot._save_persist()

            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertIn("A", bot2.state.paid_crossed)
            self.assertAlmostEqual(bot2.state.accrued_est["A"], 2.0)
            self.assertAlmostEqual(bot2.state.reward_paid_lifetime, 2.0)
            self._accrue(bot2, {"A": 0.25}, 1.0)     # increment only
            self.assertAlmostEqual(bot2.state.reward_paid_lifetime, 2.25)
        finally:
            _clean_persist()

    def test_crossed_set_pruned_with_accrued_est(self):
        """A ticker dropped from known_tickers leaves BOTH maps, so a re-listed
        ticker cannot resume in the already-paid state."""
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.known_tickers = {"A"}
            self._accrue(bot, {"A": 2.0, "GONE": 2.0}, 1.0)
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertIn("A", bot2.state.paid_crossed)
            self.assertNotIn("GONE", bot2.state.paid_crossed)
            self.assertNotIn("GONE", bot2.state.accrued_est)
        finally:
            _clean_persist()

    def test_migration_adopts_carried_markets_without_back_crediting(self):
        """First load after this shipped: the old state file has accrued_est
        and no paid_crossed. Those markets accrued over days that the raw
        counters already booked, so crossing them now would dump the whole
        backlog into TODAY."""
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.known_tickers = {"BIG", "SMALL"}
            bot.state.accrued_est = {"BIG": 40.0, "SMALL": 0.4}
            bot._save_persist()
            # simulate the pre-change file: drop the new key
            with open(bot.PERSIST_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data.pop("paid_crossed", None)
            with open(bot.PERSIST_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)

            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertEqual(bot2.state.paid_crossed, {"BIG"})
            self.assertEqual(bot2.state.reward_paid_today, 0.0)
            self.assertEqual(bot2.state.reward_paid_lifetime, 0.0)
            self._accrue(bot2, {"BIG": 2.0}, 1.0)      # forward accrual only
            self.assertAlmostEqual(bot2.state.reward_paid_lifetime, 2.0)
        finally:
            _clean_persist()

    def test_daily_roll_records_both_bases(self):
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.reward_est_today = 12.34
            bot.state.reward_paid_today = 9.99
            bot.state.last_markets_line = "x"
            bot.build_daily_summary()
            key = (datetime.now(timezone.utc).astimezone(imm.CT).date()
                   - timedelta(days=1)).isoformat()
            self.assertAlmostEqual(bot.state.reward_history[key], 12.34)
            self.assertAlmostEqual(bot.state.reward_paid_history[key], 9.99)
            self.assertEqual(bot.state.reward_est_today, 0.0)
            self.assertEqual(bot.state.reward_paid_today, 0.0)
        finally:
            _clean_persist()


class TestRateBarScopedToNewEvents(unittest.TestCase):
    """Jack 2026-08-05: "i want sticky quoting when a market is already
    quoted, unless it is hopeless. i do not want to open up new events that
    otherwise weren't being quoted."

    The rate bar is kept, but only gates the FIRST strike of an event the bot
    is not already working. A sibling on an event we already hold is depth on
    an exposure already taken; the first strike of an untouched event is a new
    exposure. Dropping the bar outright did both — measured +122 markets
    across +9 previously-excluded events, which is not what was asked for."""

    AFFECTED = ("KXUST7AM", "KXUST2AM", "KXUST10AM", "KXUST5AM", "KXUST30AM",
                "KXFSLR", "KXHOOD")

    def test_the_bar_is_still_in_force(self):
        for s in self.AFFECTED:
            self.assertEqual(imm.series_min_est_rate(s), 2.0, s)

    def test_safe_join_and_the_payout_floor_are_intact(self):
        for s in self.AFFECTED:
            self.assertTrue(imm.series_safe_join(s), s)
            self.assertGreaterEqual(imm.series_min_est_total(s),
                                    imm.PAYOUT_FLOOR_DOLLARS, s)

    def test_both_rate_bar_defaults_agree(self):
        """The re-entry loop and the rates loop each carry their own default.
        They diverged once (2026-08-05) and the Treasuries silently kept the
        old bar, so assert they cannot drift apart again."""
        self.assertEqual(imm.series_min_est_rate("KXAAAGASD"),
                         imm.series_min_est_rate("KXUST2AD"))

    # --- the gate itself, exercised as the selection loop evaluates it ------
    @staticmethod
    def _blocked(ticker, event, prev_selected, prev_events, est_per_day,
                 projected):
        """Mirror of the rate_floor branch in refresh_universe."""
        return (ticker not in prev_selected
                and event not in prev_events
                and event not in imm.FORCE_EVENTS
                and est_per_day < 2.0
                and projected < imm.RATE_FLOOR_TOTAL_ALT)

    def test_first_strike_of_an_untouched_event_is_blocked(self):
        self.assertTrue(self._blocked(
            "KXFSLR-26OCTX-1", "KXFSLR-26OCTX", set(), set(), 0.83, 3.4))

    def test_sibling_of_an_already_quoted_event_is_admitted(self):
        self.assertFalse(self._blocked(
            "KXFSLR-26OCTX-2", "KXFSLR-26OCTX",
            {"KXFSLR-26OCTX-1"}, {"KXFSLR-26OCTX"}, 0.83, 3.4))

    def test_a_sticky_event_does_not_unlock_a_DIFFERENT_event(self):
        """The leak that made the blanket change wrong."""
        self.assertTrue(self._blocked(
            "KXHOOD-26NOVY-1", "KXHOOD-26NOVY",
            {"KXFSLR-26OCTX-1"}, {"KXFSLR-26OCTX"}, 0.96, 3.9))

    def test_a_market_clearing_the_bar_still_opens_its_event(self):
        self.assertFalse(self._blocked(
            "KXNEW-26OCTZ-1", "KXNEW-26OCTZ", set(), set(), 9.0, 40.0))

    def test_horizon_escape_still_opens_an_event_on_total_value(self):
        self.assertFalse(self._blocked(
            "KXNEW-26OCTZ-1", "KXNEW-26OCTZ", set(), set(), 0.5,
            imm.RATE_FLOOR_TOTAL_ALT))

    def test_with_no_sticky_the_clause_is_inert(self):
        """prev_events empty => the added condition is always True => the gate
        is byte-for-byte the old behaviour on a cold start."""
        for est, proj in ((0.83, 3.4), (0.1, 0.2), (1.99, 4.99)):
            self.assertTrue(self._blocked("KXA-26X-1", "KXA-26X", set(), set(),
                                          est, proj))


class TestRateFloorEscapeHorizonCap(unittest.TestCase):
    """Jack 2026-08-07: cap the horizon escape at RATE_FLOOR_ESCAPE_DAYS.

    The escape admits on projected TOTAL >= $5, but a total dilutes with
    window length: KXCRYPTOSTRUCTURE entered at ~$0.36/day because its
    13.9-day program window stretched pennies to $5.04 — an effective bar
    5.5x looser than the $2/day rate bar. The cap makes the escape
    bar-neutral: <=2.5-day windows keep the full anti-flapping escape,
    longer windows face the bar undiluted."""

    def test_cryptostructure_shape_is_now_blocked(self):
        # the exact 2026-08-07 06:22Z admission: $0.36/day x 13.9d window
        projected = imm.rate_floor_projected(
            accrued=0.0, est_total=0.36 * 13.9, peak=0.0, quotable_days=13.9)
        self.assertAlmostEqual(projected, 0.36 * 2.5, places=6)
        self.assertLess(projected, imm.RATE_FLOOR_TOTAL_ALT)

    def test_short_window_keeps_the_full_escape(self):
        # a 1-day program at $5/day total: the anti-flapping case must
        # still clear (cap only shrinks windows LONGER than the escape days)
        projected = imm.rate_floor_projected(
            accrued=0.0, est_total=5.0, peak=0.0, quotable_days=1.0)
        self.assertAlmostEqual(projected, 5.0, places=6)
        self.assertGreaterEqual(projected, imm.RATE_FLOOR_TOTAL_ALT)

    def test_boundary_window_is_uncapped(self):
        projected = imm.rate_floor_projected(
            accrued=0.0, est_total=4.0, peak=0.0,
            quotable_days=imm.RATE_FLOOR_ESCAPE_DAYS)
        self.assertAlmostEqual(projected, 4.0, places=6)

    def test_banked_accrual_counts_in_full(self):
        # accrued credit is money, not a projection — never scaled
        projected = imm.rate_floor_projected(
            accrued=5.0, est_total=0.0, peak=0.0, quotable_days=14.0)
        self.assertAlmostEqual(projected, 5.0, places=6)

    def test_peak_is_scaled_like_the_total(self):
        # the 1h-peak memory must not smuggle the uncapped horizon back in
        projected = imm.rate_floor_projected(
            accrued=0.0, est_total=0.0, peak=5.04, quotable_days=13.9)
        self.assertAlmostEqual(projected, 5.04 * 2.5 / 13.9, places=6)
        self.assertLess(projected, imm.RATE_FLOOR_TOTAL_ALT)

    def test_huge_escape_days_restores_old_behaviour(self):
        # IMM_RATE_FLOOR_ESCAPE_DAYS=big = the pre-cap gate, byte for byte
        old = imm.RATE_FLOOR_ESCAPE_DAYS
        imm.RATE_FLOOR_ESCAPE_DAYS = 1e9
        try:
            projected = imm.rate_floor_projected(
                accrued=0.3, est_total=5.04, peak=6.0, quotable_days=13.9)
            self.assertAlmostEqual(projected, 0.3 + 6.0, places=6)
        finally:
            imm.RATE_FLOOR_ESCAPE_DAYS = old

    def test_tiny_window_denominator_is_safe(self):
        # sub-hour windows must not divide by zero or explode the cap
        projected = imm.rate_floor_projected(
            accrued=0.0, est_total=1.0, peak=0.0, quotable_days=0.0)
        self.assertAlmostEqual(projected, 1.0, places=6)

    def test_selection_branch_calls_the_real_function(self):
        """Wiring: the refresh_universe rate-floor branch must consult
        rate_floor_projected — a mirror that drifts is worthless."""
        bot_cls = TestStickySelection
        helper = bot_cls.__dict__.get("_quoting_bot")
        self.assertIsNotNone(helper, "fixture moved; rewire this test")
        bot = helper(bot_cls())
        t = bot_cls.T
        old_ov = imm.SERIES_OVERRIDES.get("KXGOOD")
        imm.SERIES_OVERRIDES["KXGOOD"] = imm.SeriesOverride(min_est_per_day=1e9)
        try:
            for forced_value, expect_in in ((-1.0, False), (1e9, True)):
                with mock.patch.object(imm, "rate_floor_projected",
                                       return_value=forced_value) as spy:
                    bot.state.selected.pop(t, None)
                    bot.state.sticky_prev.discard(t)
                    bot._est_peak.clear()
                    bot.state.universe_at = 0.0
                    bot.run_cycle()
                    self.assertTrue(spy.called,
                                    "branch no longer calls rate_floor_projected")
                self.assertEqual(t in bot.state.selected, expect_in,
                                 f"forced projected={forced_value}")
        finally:
            if old_ov is None:
                imm.SERIES_OVERRIDES.pop("KXGOOD", None)
            else:
                imm.SERIES_OVERRIDES["KXGOOD"] = old_ov
            _clean_persist()


class TestTwoSidedDepthGate(unittest.TestCase):
    """Jack 2026-08-05: "dont quote, and remove existing quotes, if either
    side has <1000 contracts. only pad on hourly TEMP markets."

    A snapshot is EXCLUDED unless both sides reach the program target, so a
    book that cannot get there pays nothing however well it is quoted. Hourly
    temp is exempt because its pads ARE the mechanism for reaching target."""

    T = "KXGOOD-99DEC31-A"

    def setUp(self):
        # Padding went back to GLOBAL on 2026-08-05 ("turn pads back on for
        # everything"), which makes this gate inert almost everywhere — a thin
        # side gets padded to target instead of standing the market down. The
        # gate is still live code and re-arms the moment padding is narrowed,
        # so exercise it with padding off.
        self._pad_default = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = False
        self.addCleanup(setattr, imm, "PAD_TO_TARGET_GLOBAL", self._pad_default)

    def _bot(self, yes, no):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.client.books[self.T] = {"orderbook_fp": {
            "yes_dollars": [["0.49", str(yes)]],
            "no_dollars": [["0.49", str(no)]]}}
        return bot

    def test_padding_is_global_again(self):
        """2026-08-05: temp-only -> global. The temp-only spell cost the
        near-miss tail (DUOL/MELI strikes at 584-981 vs a 1000 target stood
        down where a 20-420 contract pad would have qualified them)."""
        self.assertTrue(self._pad_default, "global padding expected by default")
        imm.PAD_TO_TARGET_GLOBAL = self._pad_default
        for s in ("KXTEMPAUSH", "KXGOOD", "KXUST7AM", "KXFSLR", "KXRAIN",
                  "KXDIESELD", "KXLOVEISLMENTION", "KXEARNINGSMENTIONUBER"):
            self.assertTrue(imm.series_pad_to_target(s), s)

    def test_gate_is_inert_while_padding_is_global(self):
        """The safety property of the revert: with pads on, a thin side is
        padded rather than stood down, so nothing is dropped by the gate."""
        imm.PAD_TO_TARGET_GLOBAL = True
        bot = self._bot(300, 1200)
        try:
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            _clean_persist()

    def test_both_sides_deep_quotes_normally(self):
        bot = self._bot(1200, 1200)
        try:
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
            self.assertTrue(bot.state.sim_orders)
        finally:
            _clean_persist()

    def test_one_thin_side_stands_the_market_down(self):
        for yes, no in ((300, 1200), (1200, 300)):
            bot = self._bot(yes, no)
            try:
                bot.run_cycle()
                self.assertNotIn(self.T, bot.state.selected,
                                 f"yes {yes} / no {no}")
                self.assertFalse([o for o in bot.state.sim_orders.values()
                                  if o["ticker"] == self.T])
            finally:
                _clean_persist()

    def test_exactly_at_target_still_quotes(self):
        bot = self._bot(1000, 1000)
        try:
            bot.run_cycle()
            self.assertIn(self.T, bot.state.selected)
        finally:
            _clean_persist()

    def test_existing_quotes_are_REMOVED_when_a_side_goes_thin(self):
        """'remove existing quotes' — a market already quoting must be
        cancelled, not merely left alone."""
        bot = self._bot(1200, 1200)
        try:
            bot.run_cycle()
            self.assertTrue([o for o in bot.state.sim_orders.values()
                             if o["ticker"] == self.T])
            bot.client.books[self.T] = {"orderbook_fp": {
                "yes_dollars": [["0.49", "200"]],
                "no_dollars": [["0.49", "1200"]]}}
            bot.state.universe_at = time.time()      # keep it a member
            bot.run_cycle()
            self.assertFalse([o for o in bot.state.sim_orders.values()
                              if o["ticker"] == self.T],
                             "thin side must cancel the resting quotes")
        finally:
            _clean_persist()

    def test_temp_is_exempt_and_pads_instead(self):
        """A thin hourly-temp book keeps quoting — the pad lifts it to
        target rather than standing it down."""
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        t = self.T
        try:
            bot.client.books[t] = {"orderbook_fp": {
                "yes_dollars": [["0.49", "200"]],
                "no_dollars": [["0.49", "200"]]}}
            _pad = imm.PAD_TO_TARGET_GLOBAL
            imm.PAD_TO_TARGET_GLOBAL = True          # stand in for a temp series
            try:
                bot.run_cycle()
                self.assertIn(t, bot.state.selected)
                pads = [o for o in bot.state.sim_orders.values()
                        if o["ticker"] == t and o["yes_price"] in
                        (imm.PAD_BID_CENTS, imm.PAD_ASK_CENTS)]
                self.assertTrue(pads, "expected pad orders on the thin sides")
            finally:
                imm.PAD_TO_TARGET_GLOBAL = _pad
        finally:
            _clean_persist()

    def test_estimator_agrees_with_the_quote_loop(self):
        """The gate lives in BOTH places on purpose: if selection still ranked
        a market the loop refuses to quote, it would hold budget and an event
        slot while resting nothing."""
        bot = self._bot(300, 1200)
        try:
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.selected)
        finally:
            _clean_persist()


class TestLiveEventDepthGate(unittest.TestCase):
    """Jack 2026-08-31: "Remove padding on TRUMPMENTION and MAMDANIMENTION
    markets. If <1k on either side of a market, stop quoting the whole
    event." These events' start times are not reliably known, so a thin
    EXTERNAL book on any quotable strike is read as the event going LIVE —
    and the WHOLE event stands down, not just the thin market (adverse
    selection protection, not reward math)."""

    EV = "KXGOOD-99DEC31"
    A = "KXGOOD-99DEC31-A"
    B = "KXGOOD-99DEC31-B"

    def setUp(self):
        self._prefixes = imm.EVENT_DEPTH_GATE_PREFIXES
        imm.EVENT_DEPTH_GATE_PREFIXES = ("KXGOOD",)
        self.addCleanup(setattr, imm, "EVENT_DEPTH_GATE_PREFIXES",
                        self._prefixes)
        self.addCleanup(_clean_persist)

    def _bot(self, yes_a=1200, no_a=1200, yes_b=1200, no_b=1200):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # second market in the SAME event as A
        bot.client.programs.append(
            {"market_ticker": self.B, "incentive_type": "liquidity",
             "period_reward": 7000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "paid_out": False,
             "start_date": start, "end_date": end})
        bot.client.markets[self.B] = {
            "ticker": self.B, "event_ticker": self.EV,
            "status": "active", "close_time": far,
            "yes_bid_dollars": "0.4900", "yes_ask_dollars": "0.5100",
            "volume_fp": "500.00"}
        self._books(bot, yes_a, no_a, yes_b, no_b)
        return bot

    def _books(self, bot, yes_a, no_a, yes_b, no_b):
        bot.client.books[self.A] = {"orderbook_fp": {
            "yes_dollars": [["0.49", str(yes_a)]],
            "no_dollars": [["0.49", str(no_a)]]}}
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.49", str(yes_b)]],
            "no_dollars": [["0.49", str(no_b)]]}}

    def _event_orders(self, bot):
        return [o for o in bot.state.sim_orders.values()
                if o["ticker"] in (self.A, self.B)]

    def test_gated_series_never_pad(self):
        """Padding is manufactured depth — it would blind the gate AND
        re-qualify exactly the book we no longer want to rest in. Prefix
        match: KXTRUMPMENTIONB rides the KXTRUMPMENTION entry."""
        imm.EVENT_DEPTH_GATE_PREFIXES = self._prefixes   # the REAL defaults
        self.assertTrue(imm.PAD_TO_TARGET_GLOBAL)
        for s in ("KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXMAMDANIMENTION"):
            self.assertTrue(imm.series_event_depth_gated(s), s)
            self.assertFalse(imm.series_pad_to_target(s), s)
        for s in ("KXGOOD", "KXTEMPAUSH", "KXLOVEISLMENTION"):
            self.assertFalse(imm.series_event_depth_gated(s), s)
            self.assertTrue(imm.series_pad_to_target(s), s)

    def test_external_depths_net_out_our_own_orders(self):
        """The gate measures the EXTERNAL book: our own ladder (or a legacy
        1k pad still resting through the config change) must not hold the
        reading over the bar while everyone else pulls out."""
        yes = [[49, 1200.0]]
        no = [[49, 800.0]]
        own = [("bid", 49, 300.0), ("ask", 51, 200.0)]
        self.assertEqual(imm.external_depths(yes, no, own), (900.0, 600.0))
        self.assertEqual(imm.external_depths(yes, no, []), (1200.0, 800.0))

    def test_both_markets_deep_quotes_normally(self):
        bot = self._bot()
        bot.run_cycle()
        self.assertIn(self.A, bot.state.selected)
        self.assertIn(self.B, bot.state.selected)
        self.assertTrue(self._event_orders(bot))
        self.assertNotIn(self.EV, bot.state.event_depth_halt)

    def test_thin_sibling_stops_the_whole_event(self):
        """The event-level point: B is thin so A — itself perfectly deep —
        must not quote either. B never even gets selected (est=0), so this
        is the estimator hook doing the marking."""
        bot = self._bot(yes_b=300)
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot))
        self.assertIn(self.EV, bot.state.event_depth_halt)
        self.assertTrue(any(c == "event_depth" for c, _m in bot.alerter.today))

    def test_going_thin_cancels_the_whole_quoting_event(self):
        """'stop quoting the whole event' on a LIVE transition: both markets
        already resting, then one side of B empties out."""
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))
        self._books(bot, 1200, 1200, 1200, 200)
        bot.state.universe_at = time.time()          # no refresh: quote loop only
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot),
                         "thin side on B must cancel A's quotes too")
        self.assertIn(self.EV, bot.state.event_depth_halt)

    def test_lost_touch_mid_band_halts_the_event(self):
        """A book that WAS two-sided in band and lost a touch entirely is
        the withdrawal signal at full volume (the one-sided breaker alone
        would stand down only that market for 30min)."""
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.49", "1200"]], "no_dollars": []}}
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot))
        self.assertIn(self.EV, bot.state.event_depth_halt)

    def test_extreme_pinned_strike_is_not_a_signal(self):
        """A 96c pin is thin by nature and proves nothing — the event keeps
        quoting (the strike itself is screened out individually)."""
        bot = self._bot()
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.95", "30"]], "no_dollars": [["0.03", "40"]]}}
        bot.run_cycle()
        self.assertNotIn(self.EV, bot.state.event_depth_halt)
        self.assertTrue([o for o in bot.state.sim_orders.values()
                         if o["ticker"] == self.A],
                        "deep sibling must keep quoting")

    def test_resume_needs_quiet_time_and_all_markets_healthy(self):
        bot = self._bot(yes_b=300)
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt)
        # book recovers, but the thin reading is FRESH -> still down
        self._books(bot, 1200, 1200, 1200, 1200)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt)
        self.assertFalse(self._event_orders(bot))
        # quiet for EVENT_DEPTH_RESUME_SECS -> resume pass clears it...
        bot.state.event_depth_halt[self.EV] = \
            time.time() - imm.EVENT_DEPTH_RESUME_SECS - 5
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertNotIn(self.EV, bot.state.event_depth_halt)
        # ...and the NEXT cycle quotes again (sticky selection kept A)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))

    def test_halt_survives_a_restart(self):
        """~20 restarts/day: a restart mid-speech must come back already
        standing down, not spend a cycle rediscovering the thin strike."""
        bot = self._bot(yes_b=300)
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt)
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn(self.EV, bot2.state.event_depth_halt)

    def test_settled_jump_out_of_band_kills_the_event_forever(self):
        """Jack 2026-08-31 #2: a deep in-band strike that jumps to 99c (the
        word got said) is the event CONFIRMED live — permanent halt, and no
        amount of book recovery or quiet time brings the event back."""
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))          # prev_mid seeded ~50
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.98", "150"]],
            "no_dollars": [["0.01", "300"]]}}             # 98x99, mid 98.5
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot))
        self.assertIn(self.EV, bot.state.event_live_halt)
        self.assertTrue(any(c == "event_live" for c, _m in bot.alerter.today))
        # books fully recover AND every timestamp goes stale -> STILL down
        self._books(bot, 1200, 1200, 1200, 1200)
        bot.state.event_depth_halt[self.EV] = \
            time.time() - imm.EVENT_DEPTH_RESUME_SECS - 5
        bot.state.event_live_halt[self.EV] = \
            time.time() - imm.EVENT_DEPTH_RESUME_SECS - 5
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot))
        self.assertIn(self.EV, bot.state.event_live_halt)
        self.assertIn(self.EV, bot.state.event_depth_halt)

    def test_live_confirm_survives_restart_and_refuses_reselection(self):
        bot = self._bot()
        bot.run_cycle()
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.98", "150"]],
            "no_dollars": [["0.01", "300"]]}}
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_live_halt)
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertIn(self.EV, bot2.state.event_live_halt)
        bot2.run_cycle()          # fresh process, healthy default A book
        self.assertFalse([o for o in bot2.state.sim_orders.values()
                          if o["ticker"] in (self.A, self.B)],
                         "live-confirmed event must never quote again")

    def test_pinned_strike_blocks_thin_halt_resume(self):
        """Jack 2026-08-31 #2: 'settled strike SHOULD hold an event down
        forever.' Even without jump history (restart amnesia), a strike
        sitting out of band counts as NOTHING for the resume pass — the
        halted event stays down until that strike is back in band or gone."""
        bot = self._bot()
        bot.run_cycle()
        self._books(bot, 1200, 1200, 300, 1200)           # B thin in-band
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt)
        bot.state.prev_mid.pop(self.B, None)              # amnesia: no jump
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.95", "30"]],
            "no_dollars": [["0.03", "40"]]}}              # pinned 95x97
        bot.state.event_depth_halt[self.EV] = \
            time.time() - imm.EVENT_DEPTH_RESUME_SECS - 5
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt,
                      "out-of-band pin must block resume")
        self.assertNotIn(self.EV, bot.state.event_live_halt)
        self.assertFalse(self._event_orders(bot))
        # only a fully healthy in-band event releases the thin-only halt
        self._books(bot, 1200, 1200, 1200, 1200)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertNotIn(self.EV, bot.state.event_depth_halt)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))

    def test_thin_then_pin_with_history_escalates_to_live_confirm(self):
        """With mid history intact the same pin IS the jump signature: the
        halt branch keeps prev_mid fresh precisely so a later settle-jump
        still confirms."""
        bot = self._bot()
        bot.run_cycle()
        self._books(bot, 1200, 1200, 300, 1200)           # thin first
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_depth_halt)
        bot.client.books[self.B] = {"orderbook_fp": {
            "yes_dollars": [["0.95", "30"]],
            "no_dollars": [["0.03", "40"]]}}              # then settles ~96
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_live_halt)

    def test_fill_burst_halts_the_whole_event(self):
        """Jack 2026-09-01 postmortem: fills ARE the adverse selection —
        one burst on a gated market stands the whole event down even with
        IMM_BREAKERS off (the box's default) and even though every book
        still reads deep."""
        self.assertFalse(imm.BREAKERS_ENABLED)
        bot = self._bot()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))
        # fills move the bot's own book AND the account position together
        # (a divergence would be the manual-standoff signature instead)
        bot.pnl.pos[self.B] = imm.EVENT_FILL_HALT_CONTRACTS + 5.0
        bot.client.positions[self.B] = imm.EVENT_FILL_HALT_CONTRACTS + 5.0
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertFalse(self._event_orders(bot),
                         "A's quotes must die with B's fill burst")
        self.assertIn(self.EV, bot.state.event_depth_halt)
        self.assertNotIn(self.EV, bot.state.event_live_halt)  # strike 1/2
        self.assertTrue(any(c == "event_fill" for c, _m in bot.alerter.today))

    def test_second_fill_burst_confirms_live_permanently(self):
        bot = self._bot()
        bot.run_cycle()
        bot.pnl.pos[self.B] = 20.0                        # burst 1
        bot.client.positions[self.B] = 20.0
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertEqual(bot.state.event_fill_strikes.get(self.EV), 1)
        # halt clears (books fine), event re-enters...
        bot.state.event_depth_halt.pop(self.EV, None)
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertTrue(self._event_orders(bot))
        bot.pnl.pos[self.B] = 45.0                        # burst 2 -> LIVE
        bot.client.positions[self.B] = 45.0
        bot.state.universe_at = time.time()
        bot.run_cycle()
        self.assertIn(self.EV, bot.state.event_live_halt)
        self.assertFalse(self._event_orders(bot))
        # strikes survive a restart, so the escalation cannot be reset
        bot._save_persist()
        bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
        self.assertEqual(bot2.state.event_fill_strikes.get(self.EV), 2)
        self.assertIn(self.EV, bot2.state.event_live_halt)


class TestSidesCanQualify(unittest.TestCase):
    """Jack 2026-08-05: the gate must ask whether a pad WILL BE PLACED, not
    whether the series is the padding kind. A padding series still gets no pad
    when the mid is outside the band, when the pad price is not far enough
    behind the touch, or when the gap exceeds PAD_MAX_CONTRACTS — and the old
    test waved all three through to quote into a book that cannot qualify."""

    S = "KXGOOD"          # padding series (padding is global again)
    TGT = 1000.0

    def setUp(self):
        self.assertTrue(imm.series_pad_to_target(self.S))

    def q(self, dy, dn, eb=40, ea=44):
        return imm.sides_can_qualify(self.S, self.TGT, dy, dn, eb, ea)

    def test_deep_book_qualifies_without_any_pad(self):
        self.assertEqual(self.q(1200, 1200), (True, True))

    def test_thin_side_qualifies_when_a_pad_will_be_placed(self):
        self.assertEqual(self.q(300, 1200), (True, True))
        self.assertEqual(self.q(1200, 300), (True, True))

    def test_mid_outside_the_band_gets_NO_pad_so_cannot_qualify(self):
        # mid 96.5 -> pad_band_ok False. The old check called this a padding
        # series and let it quote anyway.
        self.assertFalse(imm.pad_band_ok(self.S, 96, 97))
        self.assertEqual(imm.sides_can_qualify(self.S, self.TGT, 300, 1200,
                                               96, 97), (False, True))

    def test_pad_too_close_to_the_touch_cannot_qualify(self):
        # a 2c touch leaves the 1c pad only 1 tick behind (< PAD_MIN_TICKS_BEHIND)
        self.assertLess(2 - imm.PAD_BID_CENTS, imm.PAD_MIN_TICKS_BEHIND)
        bid_ok, _ = imm.sides_can_qualify(self.S, self.TGT, 300, 1200, 2, 40)
        self.assertFalse(bid_ok)

    def test_gap_wider_than_the_pad_cap_cannot_qualify(self):
        big = imm.PAD_MAX_CONTRACTS + 500.0
        bid_ok, _ = imm.sides_can_qualify(self.S, big, 0, big, 40, 44)
        self.assertFalse(bid_ok)
        # ...but a gap the cap CAN close still qualifies
        ok, _ = imm.sides_can_qualify(self.S, float(imm.PAD_MAX_CONTRACTS),
                                      0, float(imm.PAD_MAX_CONTRACTS), 40, 44)
        self.assertTrue(ok)

    def test_non_padding_series_needs_real_depth(self):
        old = imm.PAD_TO_TARGET_GLOBAL
        imm.PAD_TO_TARGET_GLOBAL = False
        try:
            self.assertEqual(imm.sides_can_qualify("KXNOPAD", self.TGT,
                                                   300, 1200, 40, 44),
                             (False, True))
            self.assertEqual(imm.sides_can_qualify("KXNOPAD", self.TGT,
                                                   1200, 1200, 40, 44),
                             (True, True))
        finally:
            imm.PAD_TO_TARGET_GLOBAL = old

    def test_no_target_is_never_gated(self):
        self.assertEqual(imm.sides_can_qualify(self.S, 0.0, 0, 0, 40, 44),
                         (True, True))

    def test_one_sided_book_cannot_qualify(self):
        # pad_band_ok treats a missing touch as outside the band
        self.assertEqual(imm.sides_can_qualify(self.S, self.TGT, 300, 1200,
                                               None, 44)[0], False)


class TestMemberMidBand(unittest.TestCase):
    """Jack 2026-08-05. member_price_band() widens a quoting market's
    PLACEMENT to 5-93, but _screen kept testing the MID against 5-90 for
    everyone — so a member whose mid drifted past 90 was dropped from
    selection before the widened band could ever apply, and the widening was
    partly dead. Members are now screened against the same band they may
    quote in; FRESH entry is unchanged."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def _meta(self, mid):
        return imm.MarketMeta(
            ticker="KXHOOD-26NOVX-1", event_ticker="KXHOOD-26NOVX",
            series="KXHOOD", dollars_per_day=25.0, program_end=None,
            target_size=1000.0, discount_factor=0.5, cutoff=None,
            close_time=datetime.now(timezone.utc) + timedelta(days=30),
            mid_cents=mid, spread_cents=1, volume=100.0,
            open_time=datetime.now(timezone.utc) - timedelta(days=1))

    def test_the_two_bands_agree_by_construction(self):
        """Defaults track STICKY_PRICE_MAX rather than restating 93 — two
        copies of one number diverging is what stranded the Treasuries."""
        self.assertEqual(imm.MID_BAND_MEMBER_HI, imm.STICKY_PRICE_MAX)
        self.assertEqual(imm.member_price_band("KXHOOD", True),
                         (imm.MID_BAND_MEMBER_LO, imm.MID_BAND_MEMBER_HI))

    def test_member_survives_a_mid_between_90_and_93(self):
        bot = self._bot()
        try:
            for mid in (90.5, 92.0, 93.0):
                self.assertIsNone(bot._screen(self._meta(mid),
                                              datetime.now(timezone.utc),
                                              member=True), f"mid {mid}")
        finally:
            _clean_persist()

    def test_fresh_entry_is_unchanged_in_that_range(self):
        bot = self._bot()
        try:
            for mid in (90.5, 92.0, 93.0):
                self.assertEqual(bot._screen(self._meta(mid),
                                             datetime.now(timezone.utc),
                                             member=False), "extreme_mid",
                                 f"mid {mid}")
        finally:
            _clean_persist()

    def test_beyond_the_member_band_still_screens_out(self):
        bot = self._bot()
        try:
            for mid in (93.5, 96.0, 4.0):
                self.assertEqual(bot._screen(self._meta(mid),
                                             datetime.now(timezone.utc),
                                             member=True), "extreme_mid",
                                 f"mid {mid}")
        finally:
            _clean_persist()

    def test_normal_mids_unaffected_either_way(self):
        bot = self._bot()
        try:
            for mid in (5.0, 50.0, 90.0):
                for mem in (True, False):
                    self.assertIsNone(bot._screen(self._meta(mid),
                                                  datetime.now(timezone.utc),
                                                  member=mem), f"{mid} {mem}")
        finally:
            _clean_persist()


class TestSafeJoinCappedAtReference(unittest.TestCase):
    """Jack 2026-08-05. Under the amended rules only orders at or above the
    REFERENCE score, so on a book whose touch level alone exceeds the target
    the qualifying walk ends at the touch and standing two ticks behind it
    earns exactly zero — safe-join and getting paid become mutually exclusive.
    Capping safe-join at the reference keeps its full effect wherever the
    reference sits behind the touch (the thin-touch case it was written for)
    and stops exactly where it would only cost reward.

    Measured live: KXFSLR-26OCTMWSOLD-4200 frac 0.00000 -> 0.01392,
    KXHOOD-26NOVECVOL-12000000000 0.00000 -> 0.01861."""

    SERIES = "KXFSLR"          # safe-join is on for the re-entry set

    def setUp(self):
        self.assertTrue(imm.series_safe_join(self.SERIES),
                        "test assumes a safe-join series")
        self._mode = imm.LADDER_MODE
        imm.LADDER_MODE = "atref"

    def tearDown(self):
        imm.LADDER_MODE = self._mode

    def _px(self, side, anchor, opposite, ref):
        q = imm.build_side_ladder(f"{self.SERIES}-26OCTX-1", side, anchor,
                                  opposite, room=100, levels=[(0, 20)],
                                  ref_px=ref)
        return q[0].price_cents if q else None

    def test_reference_at_the_touch_is_not_pushed_behind(self):
        # tight book (spread 4 < SAFE_JOIN_MIN_SPREAD) whose touch already
        # exceeds target -> reference IS the touch
        self.assertEqual(self._px("bid", 48, 52, 48), 48)
        self.assertEqual(self._px("ask", 52, 48, 52), 52)

    def test_safe_join_still_applies_when_the_reference_is_deeper(self):
        # thin touch: reference sits 5 ticks back, safe-join's 2-tick cap is
        # not the binding constraint and placement lands on the reference
        self.assertEqual(self._px("bid", 48, 52, 43), 43)
        self.assertEqual(self._px("ask", 52, 48, 57), 57)

    def test_reference_one_tick_back_beats_the_two_tick_offset(self):
        # would previously have rested at 46 (anchor-2), scoring nothing
        # below the 47 reference
        self.assertEqual(self._px("bid", 48, 52, 47), 47)
        self.assertEqual(self._px("ask", 52, 48, 53), 53)

    def test_wide_book_never_engaged_safe_join_at_all(self):
        # spread 10 >= SAFE_JOIN_MIN_SPREAD -> no cap either way
        self.assertEqual(self._px("bid", 45, 55, 45), 45)

    def test_non_safe_join_series_unaffected(self):
        self.assertFalse(imm.series_safe_join("KXTEMPDCH"))
        q = imm.build_side_ladder("KXTEMPDCH-26AUG0512-T80.99", "bid", 48, 52,
                                  room=100, levels=[(0, 20)], ref_px=48)
        self.assertEqual(q[0].price_cents, 48)

    def test_cap_never_makes_us_cross_the_opposite_touch(self):
        """Post-only safety: the reference cap must not push a bid to or past
        the ask (build_side_ladder pulls the anchor inside first)."""
        px = self._px("bid", 51, 52, 60)      # absurd ref beyond the ask
        self.assertLess(px, 52)


class TestHopelessExitDipGuard(unittest.TestCase):
    """Jack 2026-08-05: "dont evict on dips under $1".

    The peak-TTL guard claimed to already do this and did not: `pts` only
    refreshes on a new HIGH, so a DECLINING estimate lets the peak expire and
    then one low reading evicts. Measured live: KXUST7AM-26AUG31 went 15
    strikes -> 7 overnight, KXFSLR-26OCTMWSOLD and KXHOOD-26NOVECVOL 7 -> 5.
    Eviction is near-permanent (re-entry must clear the fresh-candidate rate
    bar these cannot meet), so the exit has to be sure, not fast."""

    def _bot(self):
        _clean_persist()
        return IncentiveMarketMaker(client=FakeClient(), live=False)

    def _step(self, bot, ticker, reaches_min, now_ts):
        """The universe-refresh bookkeeping, in isolation."""
        if reaches_min:
            bot.state.hopeless_since.pop(ticker, None)
        else:
            bot.state.hopeless_since.setdefault(ticker, now_ts)
        sub = now_ts - bot.state.hopeless_since.get(ticker, now_ts)
        return (imm.HOPELESS_EXIT and not reaches_min
                and sub >= imm.HOPELESS_SUSTAIN_SECS)

    def test_a_single_dip_does_not_evict(self):
        bot = self._bot()
        try:
            t0 = 1_000_000.0
            self.assertFalse(self._step(bot, "A", True, t0))
            self.assertFalse(self._step(bot, "A", False, t0 + 60))   # the dip
            self.assertFalse(self._step(bot, "A", True, t0 + 120))   # recovers
            self.assertNotIn("A", bot.state.hopeless_since)
        finally:
            _clean_persist()

    def test_a_brief_run_of_dips_does_not_evict(self):
        bot = self._bot()
        try:
            t0 = 1_000_000.0
            for i in range(1, 30):        # ~29 minutes under the bar
                self.assertFalse(self._step(bot, "A", False, t0 + 60 * i),
                                 f"evicted after {i} min")
        finally:
            _clean_persist()

    def test_sustained_sub_bar_still_evicts(self):
        bot = self._bot()
        try:
            t0 = 1_000_000.0
            self._step(bot, "A", False, t0)
            self.assertFalse(self._step(bot, "A", False,
                                        t0 + imm.HOPELESS_SUSTAIN_SECS - 1))
            self.assertTrue(self._step(bot, "A", False,
                                       t0 + imm.HOPELESS_SUSTAIN_SECS))
        finally:
            _clean_persist()

    def test_one_recovery_resets_the_whole_clock(self):
        """A market that recovers even briefly starts over — the point is that
        only CONTINUOUS hopelessness counts."""
        bot = self._bot()
        try:
            t0 = 1_000_000.0
            self._step(bot, "A", False, t0)
            self._step(bot, "A", True, t0 + imm.HOPELESS_SUSTAIN_SECS - 10)
            self.assertFalse(self._step(bot, "A", False,
                                        t0 + imm.HOPELESS_SUSTAIN_SECS + 10))
        finally:
            _clean_persist()

    def test_clock_survives_a_restart(self):
        """~20 deploys/day would otherwise keep resetting it and the exit
        could never fire at all."""
        _clean_persist()
        try:
            bot = IncentiveMarketMaker(client=FakeClient(), live=False)
            bot.state.known_tickers = {"A"}
            bot.state.hopeless_since["A"] = 1_000_000.0
            bot._save_persist()
            bot2 = IncentiveMarketMaker(client=FakeClient(), live=False)
            self.assertAlmostEqual(bot2.state.hopeless_since.get("A"), 1_000_000.0)
        finally:
            _clean_persist()

    def test_sustain_window_is_an_hour_by_default(self):
        self.assertEqual(imm.HOPELESS_SUSTAIN_SECS, 3600)


class TestTreasuryYieldSeriesEnrolled(unittest.TestCase):
    """Jack 2026-08-04: allowlist the five daily Treasury-yield tenors."""

    DAILIES = ("KXUST2AD", "KXUST5AD", "KXUST7AD", "KXUST10AD", "KXUST30AD")
    MONTHLIES = ("KXUST2AM", "KXUST5AM", "KXUST7AM", "KXUST10AM", "KXUST30AM")
    TENORS = DAILIES + MONTHLIES

    def setUp(self):
        # Other suites toggle these and don't always restore them; pin them so
        # this asserts enrolment rather than whatever ran before it.
        self._saved = (imm.ALLOWLIST_ONLY, set(imm.EXTRA_ALLOW_SERIES))
        imm.ALLOWLIST_ONLY = True
        imm.EXTRA_ALLOW_SERIES.clear()

    def tearDown(self):
        imm.ALLOWLIST_ONLY = self._saved[0]
        imm.EXTRA_ALLOW_SERIES.clear()
        imm.EXTRA_ALLOW_SERIES.update(self._saved[1])

    def test_all_five_tenors_are_allowed(self):
        for s in self.TENORS:
            self.assertIn(s, imm.ALLOW_SERIES, s)
            self.assertTrue(
                imm.IncentiveMarketMaker._allowed(f"{s}-26AUG05-T4.25"), s)

    def test_they_carry_the_guarded_re_entry_treatment(self):
        """A rate print sits tight and two-sided until the number lands, so
        joining the touch is the expensive way to be there."""
        for s in self.TENORS:
            self.assertTrue(imm.series_safe_join(s), s)
            self.assertEqual(imm.series_min_est_rate(s), 2.0, s)
            self.assertGreaterEqual(imm.series_min_est_total(s),
                                    imm.PAYOUT_FLOOR_DOLLARS, s)

    def test_monthlies_enrolled_too(self):
        """Jack 2026-08-04, second pass: 'also allowlist the treasury
        monthlies like KXUST2AM'. Same contract, ~4 weeks out."""
        for s in self.MONTHLIES:
            self.assertIn(s, imm.ALLOW_SERIES, s)
            self.assertTrue(
                imm.IncentiveMarketMaker._allowed(f"{s}-26AUG31-T4.25"), s)

    def test_monthlies_carry_the_same_cutoff_and_guards(self):
        """The monthly settles on the same 3:30pm ET snapshot, so the 7:30am
        event-day rule reads across — it just does not bite until month end."""
        for s in self.MONTHLIES:
            self.assertTrue(imm.series_safe_join(s), s)
            self.assertEqual(
                self._cutoff(s, f"{s}-26AUG31",
                             close=self._et(31, 15, 30)),
                "2026-08-31 07:30", s)

    def test_no_prefix_bleed_onto_unenrolled_ust_shapes(self):
        # exact-series matching: a hypothetical weekly must not ride in on the
        # KXUST prefix just because the dailies and monthlies are enrolled
        for s in ("KXUST2AW", "KXUST3AD", "KXUSTFOO"):
            self.assertNotIn(s, imm.ALLOW_SERIES, s)
            self.assertFalse(
                imm.IncentiveMarketMaker._allowed(f"{s}-26AUG31-T4.25"), s)

    @staticmethod
    def _et(day, hh, mm):
        return imm.ET.localize(datetime(2026, 8, day, hh, mm)).astimezone(timezone.utc)

    def _cutoff(self, series, event, occurrence=None, close=None):
        close = close if close is not None else self._et(5, 15, 30)
        c = imm.trade_cutoff_utc(event, occurrence, close)
        c = imm.apply_series_cutoff_adjustments(series, event, c, close)
        return c.astimezone(imm.ET).strftime("%Y-%m-%d %H:%M") if c else None

    def test_quotes_until_730am_et_on_the_event_day(self):
        """Jack 2026-08-04: 'quote treasuries until 7:30am EST'. Midnight is
        the right default for a scheduled RELEASE; a Treasury yield settles on
        a 3:30pm ET snapshot of a continuously-traded rate, so the overnight
        hours are no more informed than the evening before. 7:30 puts the bot
        out ahead of the 08:30 data."""
        for s in self.TENORS:
            self.assertEqual(self._cutoff(s, f"{s}-26AUG05"),
                             "2026-08-05 07:30", s)

    def test_other_day_dated_series_keep_the_midnight_rule(self):
        for s in ("KXAAAGASD", "KXDIESELD", "KXUSGASCPI", "KXBKFT"):
            self.assertEqual(self._cutoff(s, f"{s}-26AUG05"),
                             "2026-08-05 00:00", s)

    def test_a_real_event_start_still_beats_the_extension(self):
        """The extender shifts the ticker-date CANDIDATE, so min() still lets
        an occurrence_datetime win. An earlier draft applied it to the
        finished cutoff and quoted straight through a 4am event start."""
        self.assertEqual(
            self._cutoff("KXUST10AD", "KXUST10AD-26AUG05",
                         occurrence=self._et(5, 4, 0)),
            "2026-08-05 04:00")

    def test_a_later_event_start_does_not_extend_us_further(self):
        self.assertEqual(
            self._cutoff("KXUST10AD", "KXUST10AD-26AUG05",
                         occurrence=self._et(5, 9, 0)),
            "2026-08-05 07:30")

    def test_extension_is_clamped_to_the_close(self):
        self.assertEqual(
            self._cutoff("KXUST10AD", "KXUST10AD-26AUG05",
                         close=self._et(5, 3, 0)),
            "2026-08-05 03:00")


class TestPerSeriesHourMultiplier(unittest.TestCase):
    """Jack 2026-08-04: halve the ladder on KXDIESELD / KXAAAGASD / KXRAIN
    from 4pm ET. Window runs to 01:59 ET because the gas/diesel dailies trade
    until then — stopping at midnight would restore full size for their last
    two hours. KXTRUEV (Jack 2026-08-24, with its allowlisting) halves an
    hour later, from 5pm ET."""

    def setUp(self):
        # Pin the config under test instead of reading whatever
        # IMM_SERIES_HOUR_MULT happens to be, so this asserts the RULE and
        # stays green under an env override.
        self._saved = imm.SERIES_HOUR_MULTS
        imm.SERIES_HOUR_MULTS = imm._parse_series_hour_mults(
            "KXDIESELD:16-1:0.5,KXAAAGASD:16-1:0.5,KXRAIN:16-1:0.5,"
            "KXTRUEV:17-1:0.5")

    def tearDown(self):
        imm.SERIES_HOUR_MULTS = self._saved

    def _at(self, et_hour):
        return imm.ET.localize(
            datetime(2026, 8, 4, et_hour, 30)).astimezone(timezone.utc)

    def test_halved_from_4pm_et(self):
        for s in ("KXDIESELD-26AUG05-T5.315", "KXAAAGASD-26AUG05-3.10",
                  "KXRAIN-26AUG05-DEN"):
            for h in (16, 20, 23):
                self.assertEqual(
                    imm.hour_size_mult(s.split("-")[0], self._at(h)), 0.5,
                    f"{s} at ET {h}")

    def test_window_runs_past_midnight_to_the_daily_close(self):
        # gas/diesel dailies close 01:59 ET
        for h in (0, 1):
            self.assertEqual(
                imm.hour_size_mult("KXDIESELD", self._at(h)), 0.5, f"ET {h}")
        self.assertEqual(imm.hour_size_mult("KXDIESELD", self._at(2)), 1.0)

    def test_truev_halved_from_5pm_et(self):
        # Jack 2026-08-24: KXTRUEV halves an hour later than gas/diesel —
        # full size through the 4pm hour, half from 5pm through the
        # overnight tail (later-dated siblings keep quoting past midnight).
        self.assertEqual(imm.hour_size_mult("KXTRUEV", self._at(16)), 1.0)
        for h in (17, 20, 23, 0, 1):
            self.assertEqual(
                imm.hour_size_mult("KXTRUEV", self._at(h)), 0.5, f"ET {h}")
        self.assertEqual(imm.hour_size_mult("KXTRUEV", self._at(2)), 1.0)

    def test_full_size_before_4pm(self):
        for h in (9, 12, 15):
            self.assertEqual(
                imm.hour_size_mult("KXRAIN", self._at(h)), 1.0, f"ET {h}")

    def test_weeklies_and_monthlies_keep_full_size(self):
        # the prefixes are the DAILY tickers on purpose
        for s in ("KXAAAGASW", "KXAAAGASM", "KXDIESELW"):
            self.assertEqual(imm.hour_size_mult(s, self._at(20)), 1.0, s)

    def test_ladder_actually_halves_on_the_production_shape(self):
        """The launcher runs IMM_LEVELS=0:20 (one rung of 20), so pin that
        rather than the 3-rung in-code default, whose per-rung half-up
        rounding makes 35 -> 18 rather than 17.5."""
        old = imm.LEVELS
        try:
            imm.LEVELS = [(0, 20)]
            self.assertEqual(imm.hour_scaled_levels("KXDIESELD", self._at(12)),
                             [(0, 20)])
            self.assertEqual(imm.hour_scaled_levels("KXDIESELD", self._at(20)),
                             [(0, 10)])
        finally:
            imm.LEVELS = old

    def test_ladder_never_rounds_a_rung_away(self):
        """Halving must not silently delete a rung — floor is 1 contract."""
        old = imm.LEVELS
        try:
            imm.LEVELS = [(0, 1)]
            self.assertEqual(imm.hour_scaled_levels("KXRAIN", self._at(20)),
                             [(0, 1)])
        finally:
            imm.LEVELS = old

    def test_per_series_rule_beats_the_global_window_and_exclude(self):
        old_g, old_x = imm.HOUR_SIZE_MULTS, imm.HOUR_MULT_EXCLUDE
        try:
            imm.HOUR_SIZE_MULTS = {20: 2.0}          # global says double
            imm.HOUR_MULT_EXCLUDE = ("KXRAIN",)      # and excludes rain
            # the explicit per-series rule still wins in both directions
            self.assertEqual(imm.hour_size_mult("KXRAIN", self._at(20)), 0.5)
            self.assertEqual(imm.hour_size_mult("KXOTHER", self._at(20)), 2.0)
        finally:
            imm.HOUR_SIZE_MULTS, imm.HOUR_MULT_EXCLUDE = old_g, old_x

    def test_global_window_survives_outside_the_per_series_hours(self):
        """Adding a 4pm rule must not cancel the quiet-hours 3-7am x2 these
        families already had — a per-series rule owns only its own hours."""
        old_g = imm.HOUR_SIZE_MULTS
        try:
            imm.HOUR_SIZE_MULTS = {3: 2.0, 4: 2.0, 5: 2.0, 6: 2.0, 7: 2.0}
            for s in ("KXDIESELD", "KXAAAGASD", "KXRAIN"):
                self.assertEqual(imm.hour_size_mult(s, self._at(5)), 2.0, s)
                self.assertEqual(imm.hour_size_mult(s, self._at(20)), 0.5, s)
                self.assertEqual(imm.hour_size_mult(s, self._at(12)), 1.0, s)
        finally:
            imm.HOUR_SIZE_MULTS = old_g

    def test_longest_prefix_wins(self):
        spec = imm._parse_series_hour_mults("KXA:0-23:2.0,KXAAAGASD:0-23:0.5")
        prefixes = [p for p, _h in spec]
        self.assertEqual(prefixes[0], "KXAAAGASD")   # most specific first

    def test_bad_spec_raises(self):
        for bad in ("KXA", "KXA:", ":1-2:0.5", "KXA:99-1:0.5", "KXA:1-2:x"):
            with self.assertRaises(ValueError, msg=bad):
                imm._parse_series_hour_mults(bad)


class TestShippedHourMultDefaults(unittest.TestCase):
    """TestPerSeriesHourMultiplier pins its own spec to assert the RULE, so
    a rule silently dropped from the shipped IMM_SERIES_HOUR_MULT default
    would still pass it. Assert the import-time default directly (skipped
    under an env override, like the allowlist-default tests effectively are)."""

    def setUp(self):
        if os.environ.get("IMM_SERIES_HOUR_MULT"):
            self.skipTest("IMM_SERIES_HOUR_MULT env override active")
        self.rules = dict(imm.SERIES_HOUR_MULTS)

    def test_truev_default_halves_at_5pm_not_4(self):
        # Jack 2026-08-24: "halve normal quote amounts starting at 5pm EST"
        self.assertIn("KXTRUEV", self.rules)
        self.assertNotIn(16, self.rules["KXTRUEV"])
        for h in (17, 23, 0, 1):
            self.assertEqual(self.rules["KXTRUEV"].get(h), 0.5, f"ET {h}")

    def test_gas_diesel_rain_defaults_still_present(self):
        self.assertEqual(self.rules["KXDIESELD"].get(16), 0.5)
        self.assertEqual(self.rules["KXAAAGASD"].get(16), 0.5)
        self.assertEqual(self.rules["KXRAIN"].get(19), 0.5)


class TestCodeChangeSelfRestart(unittest.TestCase):
    """Jack 2026-08-24: deploys sat inert because nothing restarted the bot
    after sync pulled new code (KXTRUEV shipped allowlisted and still quoted
    $0 against live programs). The bot now exits for the launcher at the
    next safe moment when its own source file changes on disk."""

    CHANGED = imm._SOURCE_MTIME + 1000.0
    AGED = CHANGED + 3600.0          # now_ts making the new mtime >=60s old
    TEMP = {"KXTEMPMIAH-26AUG2415-T80.99": None}

    def _at(self, minute):
        return imm.ET.localize(
            datetime(2026, 8, 24, 12, minute)).astimezone(timezone.utc)

    def test_no_change_no_exit(self):
        self.assertFalse(imm.code_change_exit_due(
            self.TEMP, self._at(55), current_mtime=imm._SOURCE_MTIME,
            now_ts=self.AGED))

    def test_changed_and_no_hourly_temp_exits_any_minute(self):
        for m in (7, 30, 44):
            self.assertTrue(imm.code_change_exit_due(
                {}, self._at(m), current_mtime=self.CHANGED,
                now_ts=self.AGED), m)
        # weekly average-temp is NOT hourly temp — no window hold
        self.assertTrue(imm.code_change_exit_due(
            {"KXAVGTKDFW-26AUG24-B85.5": None}, self._at(30),
            current_mtime=self.CHANGED, now_ts=self.AGED))

    def test_changed_with_hourly_temp_waits_for_the_window(self):
        for m in (7, 30, 49):
            self.assertFalse(imm.code_change_exit_due(
                self.TEMP, self._at(m), current_mtime=self.CHANGED,
                now_ts=self.AGED), m)
        for m in (50, 55, 0, 5):
            self.assertTrue(imm.code_change_exit_due(
                self.TEMP, self._at(m), current_mtime=self.CHANGED,
                now_ts=self.AGED), m)

    def test_fresh_mtime_settles_first(self):
        # a file the sync is still writing must never be half-imported
        self.assertFalse(imm.code_change_exit_due(
            {}, self._at(55), current_mtime=self.CHANGED,
            now_ts=self.CHANGED + 10))

    def test_kill_switch(self):
        old = imm.EXIT_ON_CODE_CHANGE
        try:
            imm.EXIT_ON_CODE_CHANGE = False
            self.assertFalse(imm.code_change_exit_due(
                {}, self._at(55), current_mtime=self.CHANGED,
                now_ts=self.AGED))
        finally:
            imm.EXIT_ON_CODE_CHANGE = old


class TestSideMaxClampedToPositionCap(unittest.TestCase):
    """A single side may not rest more than the market's whole net position
    cap. Live breach 2026-08-04 (KXTEMPAUSH-26AUG0409-T79.99, cap 50): long
    +25 -> sell room 50+25=75 trimmed to a 60-contract ladder -> 35 filled ->
    a full 60 re-placed 28s later off the stale +25 -> position -70."""

    def test_temp_ladder_cannot_exceed_its_own_position_cap(self):
        # 20/side x the 3.0 deep-reference multiplier = 60, cap is 50
        b, a = imm.clamp_side_max_to_position_cap(60, 60, 50.0)
        self.assertEqual((b, a), (50, 50))

    def test_sides_are_clamped_independently(self):
        b, a = imm.clamp_side_max_to_position_cap(60, 20, 50.0)
        self.assertEqual((b, a), (50, 20))

    def test_no_effect_when_the_ladder_already_fits(self):
        # non-temp: 20/side x 3.0 = 60 against a 150 cap — untouched
        self.assertEqual(imm.clamp_side_max_to_position_cap(60, 60, 150.0),
                         (60, 60))

    def test_zero_or_absent_cap_is_a_no_op(self):
        self.assertEqual(imm.clamp_side_max_to_position_cap(60, 60, 0.0),
                         (60, 60))
        self.assertEqual(imm.clamp_side_max_to_position_cap(60, 60, -1.0),
                         (60, 60))

    def test_live_regression_temp_ladder_of_twenty_per_side(self):
        """The PRODUCTION shape, pinned: the launcher runs IMM_TEMP_LEVELS=0:20
        (not the in-code default), which is what made 20 x 3.0 = 60 exceed the
        50 cap. Set it explicitly so this asserts the live config rather than
        whatever env the suite happens to run under."""
        import dataclasses
        ov = imm.SERIES_OVERRIDES["KXTEMPAUSH"]
        try:
            imm.SERIES_OVERRIDES["KXTEMPAUSH"] = dataclasses.replace(
                ov, levels=[(0, 20)])
            lv = imm.hour_scaled_levels(
                "KXTEMPAUSH", datetime(2026, 8, 4, 12, tzinfo=timezone.utc))
            base = sum(s for _t, s in lv)
            side_max = int(round(base * imm.REF_DEPTH_MAX_MULT))
            maxpos = imm.series_max_position("KXTEMPAUSH")
            self.assertEqual(side_max, 60)        # the live 2026-08-04 number
            self.assertGreater(side_max, maxpos)  # the hole, unclamped
            b, a = imm.clamp_side_max_to_position_cap(side_max, side_max, maxpos)
            self.assertEqual((b, a), (50, 50))
        finally:
            imm.SERIES_OVERRIDES["KXTEMPAUSH"] = ov


class TestCoverageIsNotAnEstimateFactor(unittest.TestCase):
    """The exclusion rule is already inside estimate_reward_share (an excluded
    snapshot returns frac 0.0), so multiplying the rate by the coverage EMA
    applied it twice. Measured on the clean post-amendment window: raw 0.982x
    credited, coverage-multiplied 1.057x."""

    def test_excluded_snapshot_contributes_nothing(self):
        thin = [[50, 10.0]]                      # far below target
        deep = [[50, 5000.0]]
        frac, sides = imm.estimate_reward_share(
            deep, thin, [("bid", 50, 100.0)], 1000.0, 0.5, own_in_book=True)
        self.assertEqual(frac, 0.0)
        self.assertLess(sides, 2)

    def test_estimate_rate_has_no_coverage_term(self):
        """est_dollars_per_day must equal frac x pool exactly — a market whose
        book has been one-sided in the past must not be discounted twice."""
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot._coverage_ema["KXGOOD-99DEC31-A"] = 0.25   # ugly history
        meta = imm.MarketMeta(
            ticker="KXGOOD-99DEC31-A", event_ticker="KXGOOD-99DEC31",
            series="KXGOOD", dollars_per_day=100.0, program_end=None,
            target_size=1000.0, discount_factor=0.5, cutoff=None,
            close_time=datetime.now(timezone.utc) + timedelta(hours=2),
            mid_cents=50.0, spread_cents=2, volume=100.0)
        self.assertTrue(bot._estimate_candidate_yield(meta, []))
        self.assertAlmostEqual(meta.est_dollars_per_day,
                               meta.est_frac * meta.dollars_per_day, places=9)


# ---- zero-share strike: is a zero share about the BOOK or about US? --------

class TestZeroShareStrikeCounts(unittest.TestCase):
    """The 2026-08-06 Kalshi maintenance outage, in predicate form.

    Kalshi went down 07:16-09:00Z (19,949 x 503 in 07Z, 18,737 in 08Z). The
    failsafe cancelled every order at 07:18:44Z; post-only 409s and our own
    429 throttle then kept us off the book for two hours (36,321 rejections,
    ZERO successful placements — and the 08Z half had no 503s at all). The old
    guard scored `frac` from our RESTING orders but tested our DESIRED quotes,
    so every selected market struck every cycle against books that never
    stopped qualifying. 307 markets benched 07:45:59-08:13:29Z: 293 of 471
    candidates, selected 390 -> 31, est reward ~$404/day -> ~$159/day.

    A zero share is only a verdict on the MARKET when our real non-pad size is
    actually resting on both sides."""

    FULL = [("bid", 47, 100.0), ("ask", 53, 100.0)]

    def test_a_full_non_pad_ladder_earning_nothing_IS_a_verdict(self):
        self.assertTrue(imm.zero_share_strike_counts(0.0, self.FULL))

    def test_nothing_resting_is_a_verdict_on_US_not_the_book(self):
        # the measured outage state: failsafe cancel-all, own == []
        self.assertFalse(imm.zero_share_strike_counts(0.0, []))

    def test_pads_only_cannot_score_so_cannot_convict(self):
        # a 1c/99c pad is thousands of ticks below the qualifying walk, so
        # frac is 0.0 BY CONSTRUCTION — and `mq` is non-empty, which is
        # exactly what the old `and mq` test waved through.
        pads = [("bid", imm.PAD_BID_CENTS, 900.0),
                ("ask", imm.PAD_ASK_CENTS, 900.0)]
        self.assertFalse(imm.zero_share_strike_counts(0.0, pads))

    def test_a_real_rung_beside_a_pad_still_needs_BOTH_sides(self):
        self.assertFalse(imm.zero_share_strike_counts(
            0.0, [("bid", 49, 100.0), ("ask", imm.PAD_ASK_CENTS, 900.0)]))

    def test_one_side_only_is_our_own_cap_or_skew_not_the_book(self):
        self.assertFalse(imm.zero_share_strike_counts(0.0, [("bid", 49, 100.0)]))
        self.assertFalse(imm.zero_share_strike_counts(0.0, [("ask", 51, 100.0)]))

    def test_zero_remaining_ghosts_are_not_resting_size(self):
        # if the count field is ever renamed, order_remaining falls through to
        # 0.0 and own_by_ticker fills with all-zero entries — the class of the
        # _fp rename that silently killed position reads for two weeks.
        self.assertFalse(imm.zero_share_strike_counts(
            0.0, [("bid", 47, 0.0), ("ask", 53, 0.0)]))

    def test_any_positive_share_is_never_a_strike(self):
        self.assertFalse(imm.zero_share_strike_counts(1e-9, self.FULL))
        self.assertFalse(imm.zero_share_strike_counts(0.5, []))

    def test_pads_alongside_real_rungs_still_convict(self):
        self.assertTrue(imm.zero_share_strike_counts(0.0, [
            ("bid", imm.PAD_BID_CENTS, 900.0), ("bid", 47, 100.0),
            ("ask", imm.PAD_ASK_CENTS, 900.0), ("ask", 53, 100.0)]))

    def test_the_estimator_agrees_with_the_predicate_on_the_outage_state(self):
        """Pin the algebra the predicate rests on: with our orders absent the
        REAL estimator returns exactly 0.0 while BOTH sides still qualify. So
        `sides` is 2 during an outage — a fix keyed on `sides` would be wrong,
        and the coverage alert (sides < 2) never overlaps with the bench."""
        yes = [[49, 1200.0]]
        no = [[49, 1200.0]]
        frac, sides = imm.estimate_reward_share(yes, no, [], 1000.0, 0.5,
                                                own_in_book=True)
        self.assertEqual(frac, 0.0)
        self.assertEqual(sides, 2)
        self.assertFalse(imm.zero_share_strike_counts(frac, []))
        # ...and our real size at the touch does earn something
        own = [("bid", 49, 100.0), ("ask", 51, 100.0)]
        frac2, sides2 = imm.estimate_reward_share(yes, no, own, 1000.0, 0.5,
                                                  own_in_book=True)
        self.assertGreater(frac2, 0.0)
        self.assertEqual(sides2, 2)


def _resting(oid, ticker, book_side, yes_px, n=100):
    """A resting order of OURS, in the shape fetch_resting_orders accepts."""
    return {"order_id": oid, "ticker": ticker, "status": "resting",
            "client_order_id": f"{imm.CLIENT_ORDER_PREFIX}-test-{oid}",
            "book_side": book_side, "yes_price": yes_px,
            "remaining_count": n}


class TestOutageDoesNotBench(unittest.TestCase):
    """Full-cycle regression for the 2026-08-06 bench storm.

    live=True is MANDATORY here: in dry mode incentive_mm.py sets est_own to
    the DESIRED ladder, so frac > 0 by construction, the streak never
    increments, and a live=False version of every test below would pass
    identically before and after the fix and prove nothing."""

    T = "KXGOOD-99DEC31-A"

    def _bot(self):
        _clean_persist()
        bot = IncentiveMarketMaker(client=FakeClient(), live=True)
        # a book that never stopped qualifying — 1200 a side vs a 1000 target,
        # which is what the cycle log showed throughout the outage (both sides
        # at or above target on 97-98% of rows in every 10-minute bucket)
        bot.client.books[self.T] = {"orderbook_fp": {
            "yes_dollars": [["0.49", "1200"]],
            "no_dollars": [["0.49", "1200"]]}}
        return bot

    def _second_cycle(self, bot, orders):
        """Arm cycle 2 with exactly `orders` of ours resting."""
        # _merge_ledger unions the exchange view with our local ledger and
        # counts young unconfirmed entries as resting, so stubbing get_orders
        # alone does NOT empty `own` — cycle 1's placements survive.
        bot.state.ledger.clear()
        bot.client.get_orders = lambda **kw: {"orders": list(orders),
                                              "cursor": None}
        bot.state.universe_at = time.time()   # no refresh between cycles

    def test_an_outage_cannot_bench_a_1200x1200_book(self):
        bot = self._bot()
        try:
            bot.run_cycle()
            self._second_cycle(bot, [])
            # name the real cause: placements rejected, nothing ever rests
            def _boom(**kw):
                raise HttpError("Service Unavailable", 503)
            bot.client.create_order = _boom
            bot.state.zero_share_streak[self.T] = imm.QUALIFY_PATIENCE_CYCLES - 1
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.bench_until)
            self.assertIn(self.T, bot.state.selected)
            self.assertFalse(any(c == "benched" for c, _m in bot.alerter.today))
        finally:
            _clean_persist()

    def test_the_streak_is_RESET_not_paused_so_a_flapping_outage_cannot_bank(self):
        bot = self._bot()
        try:
            bot.run_cycle()
            self._second_cycle(bot, [])
            # deliberately NOT one short of the bench: at 29 the old code also
            # ends at {} — by benching. Mid-streak is the discriminating probe.
            # An untrusted cycle must ZERO the count, not merely skip it, or a
            # flapping outage banks strikes across the good cycles between
            # blackouts and benches anyway.
            partial = imm.QUALIFY_PATIENCE_CYCLES // 2
            bot.state.zero_share_streak[self.T] = partial
            bot.run_cycle()
            self.assertEqual(bot.state.zero_share_streak, {})
            self.assertNotIn(self.T, bot.state.bench_until)
        finally:
            _clean_persist()

    def test_a_healthy_first_cycle_scores_NO_strike(self):
        # measured pre-fix: {'KXGOOD-99DEC31-A': 1} after cycle 1, because the
        # counter at the top of the quote loop judges a placement that has not
        # happened yet. The counter was off-by-one from its stated meaning
        # even on the happy path.
        bot = self._bot()
        try:
            bot.run_cycle()
            self.assertEqual(bot.state.zero_share_streak, {})
        finally:
            _clean_persist()

    def test_pads_only_is_not_a_verdict_on_the_book(self):
        bot = self._bot()
        try:
            bot.run_cycle()
            self._second_cycle(bot, [
                _resting("p1", self.T, "bid", imm.PAD_BID_CENTS, 900),
                _resting("p2", self.T, "ask", imm.PAD_ASK_CENTS, 900)])
            bot.state.zero_share_streak[self.T] = imm.QUALIFY_PATIENCE_CYCLES - 1
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.bench_until)
            self.assertIn(self.T, bot.state.selected)
        finally:
            _clean_persist()

    def test_a_full_ladder_earning_nothing_STILL_benches(self):
        """The anti-deletion test. Without this the fix is indistinguishable
        from deleting the bench. Our real non-pad size rests on BOTH sides,
        far below the qualifying walk (which stops at 49c once 1200 >= the
        1000 target), so we carry fill risk for exactly zero rent."""
        bot = self._bot()
        try:
            bot.run_cycle()
            self._second_cycle(bot, [
                _resting("r1", self.T, "bid", 40, 100),
                _resting("r2", self.T, "ask", 60, 100)])
            bot.state.zero_share_streak[self.T] = imm.QUALIFY_PATIENCE_CYCLES - 1
            bot.run_cycle()
            self.assertIn(self.T, bot.state.bench_until)
            self.assertNotIn(self.T, bot.state.selected)
            self.assertTrue(any(c == "benched" for c, _m in bot.alerter.today))
        finally:
            _clean_persist()

    def test_wake_grace_never_strikes(self):
        """Post-sleep reads "can SUCCEED with garbage (partial positions/
        orders)" — the gate standoffs and universe selection already had and
        this counter never did. Same fixture as the legitimate bench above."""
        bot = self._bot()
        try:
            bot.run_cycle()
            self._second_cycle(bot, [
                _resting("r1", self.T, "bid", 40, 100),
                _resting("r2", self.T, "ask", 60, 100)])
            bot.wake_grace_until = time.time() + 300
            bot.state.zero_share_streak[self.T] = imm.QUALIFY_PATIENCE_CYCLES - 1
            bot.run_cycle()
            self.assertNotIn(self.T, bot.state.bench_until)
            self.assertEqual(bot.state.zero_share_streak, {})
        finally:
            _clean_persist()


class TestCallWindowFreeze(unittest.TestCase):
    """Jack 2026-08-06: "sit out completely during call windows".

    Once a scheduled earnings call has STARTED, the market gets no orders at
    all — not even reduce-only wind-down. Positions ride to settlement.

    The bug this closes: restore_orphan_metas built its cutoff from
    trade_cutoff_utc() alone, which knows nothing about EVENT_START_OVERRIDES
    (only the selection path consults resolver.resolve()). So
    KXEARNINGSMENTIONDKNG-26AUG07, whose call ran 16:00 ET on Aug 6, resolved
    to midnight-ET-of-Aug-7 — still in the future — and kept 7 reduce-only
    orders resting 1.2h into the live call. MEASURED, not hypothetical.
    """

    T = "KXGOOD-99DEC31-A"
    EV = "KXGOOD-99DEC31"

    def setUp(self):
        _clean_persist()
        self.addCleanup(_clean_persist)
        self._saved = dict(imm.EVENT_START_OVERRIDES)
        self.addCleanup(lambda: (imm.EVENT_START_OVERRIDES.clear(),
                                 imm.EVENT_START_OVERRIDES.update(self._saved)))

    def _bot(self):
        bot = IncentiveMarketMaker(client=FakeClient(), live=False)
        bot.state.known_tickers.add(self.T)
        bot.pnl.pos[self.T] = -40.0
        return bot

    def _meta_for(self):
        return MarketMeta(
            ticker=self.T, event_ticker=self.EV, series="KXGOOD",
            dollars_per_day=0.0, program_end=None, target_size=0.0,
            discount_factor=0.5, cutoff=None, close_time=None)

    def _set_call(self, minutes_from_now):
        imm.EVENT_START_OVERRIDES[self.EV] = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now))

    def test_started_call_blocks_orphan_restore(self):
        self._set_call(-60)                      # call began an hour ago
        bot = self._bot()
        bot.restore_orphan_metas({self.T: -40.0})
        self.assertNotIn(self.T, bot.state.managed_extra,
                         "orphan restored into a live call window")

    def test_started_call_flushes_an_existing_entry_and_rests_nothing(self):
        self._set_call(-60)
        bot = self._bot()
        # the position must be visible to run_cycle, or managed_extra is
        # dropped for being flat and the test passes without exercising the
        # call-window branch at all
        bot.client.positions[self.T] = -40.0
        bot.state.managed_extra[self.T] = self._meta_for()
        bot.run_cycle()
        self.assertNotIn(self.T, bot.state.managed_extra)
        self.assertNotIn(self.T, bot.state.selected)
        self.assertFalse([o for o in bot.state.sim_orders.values()
                          if o.get("ticker") == self.T],
                         "orders rested during a live call window")

    def test_buffer_counts_as_started(self):
        """Orders must be gone OVERRIDE_BUFFER_MIN BEFORE the call, so the
        freeze has to bite inside the buffer too, not exactly at the start."""
        self._set_call(imm.OVERRIDE_BUFFER_MIN - 1)
        bot = self._bot()
        bot.restore_orphan_metas({self.T: -40.0})
        self.assertNotIn(self.T, bot.state.managed_extra)

    def test_future_call_still_winds_down_normally(self):
        """The freeze must not become a blanket ban on reduce-only exits —
        before the call, working out inventory is exactly what we want."""
        self._set_call(24 * 60)                  # call is tomorrow
        bot = self._bot()
        bot.restore_orphan_metas({self.T: -40.0})
        self.assertIn(self.T, bot.state.managed_extra,
                      "wind-down wrongly frozen well before the call")

    def test_no_override_is_unaffected(self):
        """Series with no scheduled call (rates, gas, rain) keep their
        existing wind-down behaviour — this change is scoped to call windows,
        and freezing them would strand inventory at settlement instead."""
        imm.EVENT_START_OVERRIDES.pop(self.EV, None)
        bot = self._bot()
        bot.restore_orphan_metas({self.T: -40.0})
        self.assertIn(self.T, bot.state.managed_extra)


if __name__ == "__main__":
    unittest.main()
