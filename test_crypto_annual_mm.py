#!/usr/bin/env python3
"""Unit tests for crypto_annual_mm.py (no network, no real orders).

Run:  python -m unittest test_crypto_annual_mm -v
All crypto fleets together (the isolation tests are the point of doing so):
      python -m unittest test_crypto_touch_mm test_crypto_touch_mm_weekly \
          test_crypto_updown_mm test_crypto_annual_mm
"""

import contextlib
import math
import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import crypto_touch_mm as mm
import crypto_updown_mm as ud
import crypto_annual_mm as ann

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
YEAR_OPEN = datetime(2026, 1, 2, 23, 0, tzinfo=timezone.utc)
YEAR_END = datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc)
T_DAYS = (YEAR_END - NOW).total_seconds() / 86400.0     # ~149 days
BTC = ann.ASSETS["BTC"]

EV_MAXY = "KXBTCMAXY-26DEC31"
EV_MINY = "KXBTCMINY-27JAN01"
EV_Y = "KXBTCY-27JAN0100"


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


def touch_mkt(strike, direction="max", event=EV_MAXY, open_dt=None, end_dt=None,
              ticker=None, status="active"):
    open_dt = open_dt or YEAR_OPEN
    end_dt = end_dt or YEAR_END
    if direction == "max":
        st, floor, cap = "greater", strike, None
    else:
        st, floor, cap = "less", None, strike
    # expected_expiration runs 300s after close on the real listings — see
    # the updown suite's mkt() note.
    return {"ticker": ticker or f"{event}-{strike:g}", "event_ticker": event,
            "status": status, "market_type": "binary", "strike_type": st,
            "floor_strike": floor, "cap_strike": cap,
            "open_time": iso(open_dt), "close_time": iso(end_dt),
            "expected_expiration_time": iso(end_dt + timedelta(seconds=300))}


def term_mkt(shape, floor=None, cap=None, event=EV_Y, ticker=None,
             open_dt=None, end_dt=None, yes_sub_title=None):
    open_dt = open_dt or YEAR_OPEN
    end_dt = end_dt or YEAR_END
    m = {"ticker": ticker or f"{event}-T{floor if floor is not None else cap}",
         "event_ticker": event, "status": "active", "market_type": "binary",
         "strike_type": shape, "floor_strike": floor, "cap_strike": cap,
         "open_time": iso(open_dt), "close_time": iso(end_dt),
         "expected_expiration_time": iso(end_dt + timedelta(seconds=300))}
    if yes_sub_title:
        m["yes_sub_title"] = yes_sub_title
    return m


def candles(n=400, interval_minutes=1440, sigma_bar=0.02, seed=3, start=100000.0):
    rng = random.Random(seed)
    out, px, ts = [], start, 1_700_000_000
    for _ in range(n):
        px *= math.exp(rng.gauss(0, sigma_bar))
        out.append((ts, px, px * 1.001, px * 0.999))
        ts += interval_minutes * 60
    return out


class FakeClient:
    """Like the updown suite's, but markets are keyed BY SERIES — the annual
    bot's discovery iterates three series per asset."""

    def __init__(self, markets_by_series=None, event_list=None, positions=None,
                 orderbook=None):
        self.markets_by_series = markets_by_series or {}
        self.event_list = event_list or []
        self.positions = positions or {}
        self.orderbook = orderbook or {"orderbook": {"yes": [[40, 50]], "no": [[45, 50]]}}
        self.get_markets_calls, self.get_events_calls = [], []
        self.orderbook_calls, self.position_calls = [], []
        self.cancelled, self.created = [], []
        self.orders_by_event = {}

    def get_markets(self, **kw):
        self.get_markets_calls.append(kw)
        return {"markets": self.markets_by_series.get(kw.get("series_ticker"), []),
                "cursor": None}

    def get_events(self, **kw):
        self.get_events_calls.append(kw)
        return {"events": [{"event_ticker": t} for t in self.event_list
                           if t.split("-", 1)[0] == kw.get("series_ticker")]}

    def get_orders(self, **kw):
        return {"orders": self.orders_by_event.get(kw.get("event_ticker"), []),
                "cursor": None}

    def get_order(self, order_id):
        return {"order": {"status": "canceled"}}

    def create_order(self, **kw):
        self.created.append(kw)
        return {"order": {"order_id": f"fake-{len(self.created)}"}}

    def cancel_order(self, order_id):
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


def bot(client=None, live=False):
    return ann.AnnualMarketMaker(BTC, client or FakeClient(), live=live)


@contextlib.contextmanager
def priced(spot=100000.0, sigma_bar=0.02):
    with frozen(), \
         mock.patch.object(mm, "fetch_live_price", return_value=spot), \
         mock.patch.object(mm, "fetch_ohlc",
                           side_effect=lambda cfg, iv: candles(400, iv, sigma_bar)):
        yield


# ---------------------------------------------------------------------------
# Model dispatch: series decides the model, never strike_type
# ---------------------------------------------------------------------------

class TestKindDispatch(unittest.TestCase):
    def test_event_tickers_resolve_to_kinds(self):
        kinds = ann.annual_series_for("BTC")
        self.assertEqual(ann.kind_for_event("KXBTCMAXY-26DEC31", kinds), "touch_max")
        self.assertEqual(ann.kind_for_event("KXBTCMINY-27JAN01", kinds), "touch_min")
        self.assertEqual(ann.kind_for_event("KXBTCY-27JAN0100", kinds), "terminal")
        self.assertIsNone(ann.kind_for_event("KXBTCD-26AUG0517", kinds))
        self.assertIsNone(ann.kind_for_event("", kinds))

    def test_bnb_asset_infix_event_ticker(self):
        # Observed live 2026-08-13: KXBNBMAXY events carry an asset infix.
        kinds = ann.annual_series_for("BNB")
        self.assertEqual(ann.kind_for_event("KXBNBMAXY-BNB-26DEC31", kinds), "touch_max")

    def test_sol_terminal_series_is_kxsold26(self):
        kinds = ann.annual_series_for("SOL")
        self.assertEqual(kinds.get("KXSOLD26"), "terminal")
        self.assertNotIn("KXSOLY", kinds)
        self.assertEqual(ann.kind_for_event("KXSOLD26-27JAN0100", kinds), "terminal")
        # KXSOLD26 must never be confused with the KXSOLD above/below series.
        self.assertIsNone(ann.kind_for_event("KXSOLD-26AUG0517", kinds))

    def test_every_fleet_asset_has_three_series(self):
        for asset in ann.ASSETS:
            kinds = ann.annual_series_for(asset)
            self.assertEqual(sorted(kinds.values()),
                             ["terminal", "touch_max", "touch_min"], asset)

    def test_touch_strikes_never_priced_terminally_and_vice_versa(self):
        """A MAXY market and a Y-series T-rung carry IDENTICAL fields
        (strike_type=greater + floor_strike) — only the event ticker separates
        a ~2x-mispricing. Same strike, same shape, different kind."""
        kinds = ann.annual_series_for("BTC")
        self.assertEqual(ann.kind_for_event(EV_MAXY, kinds), "touch_max")
        self.assertEqual(ann.kind_for_event(EV_Y, kinds), "terminal")


class TestTouchVsTerminalAtAnnualHorizon(unittest.TestCase):
    def test_touch_is_about_double_terminal(self):
        spot, sigma = 100000.0, 0.02
        for z in (0.5, 1.0, 1.5):
            strike = spot * math.exp(z * sigma * math.sqrt(T_DAYS))
            touch = mm.touch_prob(spot, strike, sigma, T_DAYS, "max")
            term = ud.terminal_prob(spot, strike, sigma, T_DAYS)
            self.assertGreater(touch / term, 1.7, f"z={z}")
            self.assertLess(touch / term, 2.3, f"z={z}")

    def test_models_disagree_in_cents_where_it_matters(self):
        spot, sigma = 100000.0, 0.02
        strike = spot * math.exp(0.8 * sigma * math.sqrt(T_DAYS))
        touch_c = round(100 * mm.touch_prob(spot, strike, sigma, T_DAYS, "max"))
        term_c = round(100 * ud.terminal_prob(spot, strike, sigma, T_DAYS))
        self.assertGreaterEqual(touch_c - term_c, 15)


# ---------------------------------------------------------------------------
# Terminal rung shapes: greater / less / between / custom
# ---------------------------------------------------------------------------

class TestTerminalFairProb(unittest.TestCase):
    spot, sigma = 100000.0, 0.02

    def prob(self, m):
        out = ann.terminal_fair_prob(m, self.spot, self.sigma, T_DAYS)
        return None if out is None else out[0]

    def test_greater_and_less_are_complements(self):
        k = 105000.0
        pg = self.prob(term_mkt("greater", floor=k))
        pl = self.prob(term_mkt("less", cap=k))
        self.assertAlmostEqual(pg + pl, 1.0, places=12)

    def test_partition_sums_to_one(self):
        """A full Y-event ladder — lower tail, range buckets, upper tail — is
        a partition of the terminal price line; the fairs must telescope to 1
        exactly (this is what makes the between formula the right one)."""
        cuts = [80000.0, 90000.0, 100000.0, 110000.0, 120000.0]
        rungs = [term_mkt("less", cap=cuts[0])]
        rungs += [term_mkt("between", floor=lo, cap=hi)
                  for lo, hi in zip(cuts, cuts[1:])]
        rungs += [term_mkt("greater", floor=cuts[-1])]
        total = sum(self.prob(m) for m in rungs)
        self.assertAlmostEqual(total, 1.0, places=12)

    def test_between_peaks_at_the_money(self):
        at = self.prob(term_mkt("between", floor=95000, cap=105000))
        away = self.prob(term_mkt("between", floor=125000, cap=135000))
        self.assertGreater(at, away)

    def test_display_strikes(self):
        _p, k = ann.terminal_fair_prob(term_mkt("greater", floor=110000),
                                       self.spot, self.sigma, T_DAYS)
        self.assertEqual(k, 110000)
        _p, k = ann.terminal_fair_prob(term_mkt("less", cap=90000),
                                       self.spot, self.sigma, T_DAYS)
        self.assertEqual(k, 90000)
        _p, k = ann.terminal_fair_prob(term_mkt("between", floor=90000, cap=100000),
                                       self.spot, self.sigma, T_DAYS)
        self.assertEqual(k, 95000)

    def test_malformed_rungs_return_none(self):
        self.assertIsNone(self.prob(term_mkt("greater", floor=None)))
        self.assertIsNone(self.prob(term_mkt("less", cap=None)))
        self.assertIsNone(self.prob(term_mkt("between", floor=100000, cap=90000)))
        self.assertIsNone(self.prob(term_mkt("between", floor=None, cap=90000)))
        self.assertIsNone(self.prob(term_mkt("weird", floor=100000)))

    def test_custom_rung_uses_the_validated_ticker_recovery(self):
        good = term_mkt("custom", ticker=f"{EV_Y}-T110000",
                        yes_sub_title="$110,000 or above")
        bad = term_mkt("custom", ticker=f"{EV_Y}-T110000",
                       yes_sub_title="$99,000 or above")   # disagrees with ticker
        self.assertIsNotNone(self.prob(good))
        self.assertIsNone(self.prob(bad))


# ---------------------------------------------------------------------------
# Event selection: annual windows only
# ---------------------------------------------------------------------------

class TestAnnualSelection(unittest.TestCase):
    def test_annual_window_classifies_and_selects(self):
        views = ud.build_event_views([touch_mkt(120000)], NOW)
        self.assertEqual(views[0].cadence, "annual")
        self.assertEqual(ud.select_events(views, ("annual",), NOW), views)

    def test_weekly_scale_window_is_rejected(self):
        wk = [touch_mkt(120000, open_dt=NOW - timedelta(days=5),
                        end_dt=NOW + timedelta(days=2))]
        views = ud.build_event_views(wk, NOW)
        self.assertEqual(views[0].cadence, "weekly")
        self.assertEqual(ud.select_events(views, ("annual",), NOW), [])

    def test_last_day_stand_down(self):
        soon = [touch_mkt(120000, end_dt=NOW + timedelta(hours=12))]
        views = ud.build_event_views(soon, NOW)
        self.assertEqual(views[0].cadence, "annual")
        self.assertEqual(ud.select_events(views, ("annual",), NOW), [])


# ---------------------------------------------------------------------------
# The YTD breach guard
# ---------------------------------------------------------------------------

def guarded_bot(daily_candles, sigma=0.02):
    b = bot()
    b.state.daily_candles = daily_candles
    b.state.sigma_daily = sigma
    return b


def spike_candle(when_utc, high, low):
    ts = int(when_utc.timestamp())
    mid = (high + low) / 2.0
    return (ts, mid, high, low)


class TestBreachGuard(unittest.TestCase):
    FEB = datetime(2026, 2, 10, tzinfo=timezone.utc)

    def band(self, b, markets, spot=100000.0):
        view = ud.build_event_views(markets, NOW)[0]
        return b.quotable_markets(view, spot, NOW, NOW.timestamp())

    def test_touched_strike_is_benched(self):
        b = guarded_bot([spike_candle(self.FEB, 120000, 90000)])
        rows = self.band(b, [touch_mkt(115000)])          # Feb high crossed it
        self.assertEqual(rows, [])
        self.assertTrue(any(r.startswith("breached") for r in b.a.skip_counts))

    def test_untouched_strike_still_quotes(self):
        b = guarded_bot([spike_candle(self.FEB, 120000, 90000)])
        rows = self.band(b, [touch_mkt(125000)])
        self.assertEqual(len(rows), 1)

    def test_min_side_uses_the_low(self):
        b = guarded_bot([spike_candle(self.FEB, 120000, 70000)])
        benched = self.band(b, [touch_mkt(75000, direction="min", event=EV_MINY)])
        self.assertEqual(benched, [])
        b2 = guarded_bot([spike_candle(self.FEB, 120000, 90000)])
        alive = self.band(b2, [touch_mkt(85000, direction="min", event=EV_MINY)])
        self.assertEqual(len(alive), 1)

    def test_strike_listed_after_the_spike_is_not_benched(self):
        """Measurement runs from market ISSUANCE: a rung listed in March is
        not touched by a February spike."""
        b = guarded_bot([spike_candle(self.FEB, 120000, 90000)])
        march = datetime(2026, 3, 15, tzinfo=timezone.utc)
        rows = self.band(b, [touch_mkt(115000, open_dt=march)])
        self.assertEqual(len(rows), 1)

    def test_session_extreme_gates_immediately(self):
        """A touch seen on the live price benches the strike even with no
        candle covering it yet."""
        b = guarded_bot([])
        view = ud.build_event_views([touch_mkt(115000)], NOW)[0]
        b.quotable_markets(view, 116000.0, NOW, NOW.timestamp())   # spike seen
        rows = b.quotable_markets(view, 100000.0, NOW, NOW.timestamp())
        self.assertEqual(rows, [])

    def test_terminal_rungs_are_never_breach_benched(self):
        # Terminal markets settle at year end regardless of the path.
        b = guarded_bot([spike_candle(self.FEB, 120000, 90000)])
        rows = self.band(b, [term_mkt("greater", floor=105000)])
        self.assertEqual(len(rows), 1)

    def test_unknown_event_prices_nothing(self):
        b = guarded_bot([])
        rows = self.band(b, [touch_mkt(120000, event="KXWEIRD-26DEC31")])
        self.assertEqual(rows, [])
        self.assertIn("event from unrecognized series", b.a.skip_counts)


# ---------------------------------------------------------------------------
# Full dry-run cycles
# ---------------------------------------------------------------------------

def three_series_markets():
    return {
        "KXBTCMAXY": [touch_mkt(120000)],
        "KXBTCMINY": [touch_mkt(80000, direction="min", event=EV_MINY)],
        "KXBTCY": [term_mkt("greater", floor=100000)],
    }


class TestRunCycle(unittest.TestCase):
    def test_idle_when_nothing_open(self):
        b = bot(FakeClient())
        with priced():
            b.run_cycle()
        self.assertIn("no open annual event", b.u.idle_reason)
        self.assertEqual(b.poll_interval(), ann.IDLE_POLL_SECS)
        self.assertEqual(b.state.sim_orders, {})

    def test_quotes_all_three_series_at_once(self):
        c = FakeClient(markets_by_series=three_series_markets())
        b = bot(c)
        with priced():
            b.run_cycle()
        self.assertEqual(len(b.u.events), 3)
        quoted = {o["ticker"].rsplit("-", 1)[0] for o in b.state.sim_orders.values()}
        self.assertEqual(quoted, {EV_MAXY, EV_MINY, EV_Y})
        self.assertEqual(len(c.position_calls), 3)   # one read per event
        fetched = {kw.get("series_ticker") for kw in c.get_markets_calls}
        self.assertEqual(fetched, set(ann.annual_series_for("BTC")))

    def test_ladder_is_the_live_pilot_shape(self):
        """Pins the deployed config: 3 levels x 1 contract, 3c off fair, 2c
        apart, joining and never leading. Changing any of these is a size
        change on a live bot and should have to break a test first."""
        c = FakeClient(markets_by_series={"KXBTCY": [term_mkt("greater", floor=100000)]})
        b = bot(c)
        with priced():
            b.run_cycle()
        placed = list(b.state.sim_orders.values())
        bids = sorted((o["yes_price"] for o in placed if o["book_side"] == "bid"),
                      reverse=True)
        asks = sorted(o["yes_price"] for o in placed if o["book_side"] == "ask")
        self.assertEqual(len(bids), 3)
        self.assertEqual(len(asks), 3)
        self.assertTrue(all(o["remaining_count"] == 1 for o in placed))
        self.assertLessEqual(bids[0], 40)      # external bid; join, never lead
        self.assertGreaterEqual(asks[0], 55)   # external ask = 100-45
        self.assertEqual([b1 - b2 for b1, b2 in zip(bids, bids[1:])],
                         [ann.LEVEL_SPACING_CENTS] * 2)
        self.assertEqual([a2 - a1 for a1, a2 in zip(asks, asks[1:])],
                         [ann.LEVEL_SPACING_CENTS] * 2)

    def test_divergence_stand_down(self):
        """Fair far from a two-sided book's mid -> we'd be taking a view; the
        deep OTM strike prices ~27c against a 47.5c mid and gets nothing."""
        c = FakeClient(markets_by_series={"KXBTCMAXY": [touch_mkt(120000),
                                                        touch_mkt(160000)]})
        b = bot(c)
        with priced():
            b.run_cycle()
        quoted = {o["ticker"] for o in b.state.sim_orders.values()}
        self.assertIn(f"{EV_MAXY}-120000", quoted)
        self.assertNotIn(f"{EV_MAXY}-160000", quoted)

    def test_at_most_three_contracts_rest_per_market_side(self):
        c = FakeClient(markets_by_series=three_series_markets())
        b = bot(c)
        with priced():
            b.run_cycle()
        totals = {}
        for o in b.state.sim_orders.values():
            key = (o["ticker"], o["book_side"])
            totals[key] = totals.get(key, 0) + o["remaining_count"]
        self.assertTrue(totals)
        self.assertLessEqual(max(totals.values()),
                             ann.NUM_LEVELS * ann.CONTRACTS_PER_LEVEL)


# ---------------------------------------------------------------------------
# Fleet isolation: annual settings must never leak into the siblings
# ---------------------------------------------------------------------------

class TestFleetIsolation(unittest.TestCase):
    def test_pilot_settings_are_pinned(self):
        self.assertEqual((ann.NUM_LEVELS, ann.CONTRACTS_PER_LEVEL), (3, 1))
        self.assertEqual((ann.QUOTE_OFFSET_CENTS, ann.LEVEL_SPACING_CENTS), (3, 2))
        self.assertEqual((ann.MAX_POSITION_CONTRACTS, ann.MAX_EVENT_CONTRACTS,
                          ann.MAX_ASSET_CONTRACTS), (40, 200, 400))
        self.assertEqual((ann.SKIP_FAIR_BELOW_CENTS, ann.SKIP_FAIR_ABOVE_CENTS), (10, 90))
        self.assertEqual(ann.MAX_FAIR_DIVERGENCE_CENTS, 15)
        self.assertEqual(ann.POLL_SECS, 60)

    def test_annual_class_attrs_do_not_touch_the_updown_fleet(self):
        self.assertEqual(ann.AnnualMarketMaker.contracts_per_level, 1)
        self.assertEqual(ud.UpDownMarketMaker.contracts_per_level, 2)
        # the updown fleet's per-tenor daily tables must not leak in here
        self.assertEqual(ann.AnnualMarketMaker.contracts_per_level_by_cadence, {})
        self.assertEqual(ann.AnnualMarketMaker.num_levels_by_cadence, {})
        self.assertEqual(ann.AnnualMarketMaker.quote_offset_by_cadence, {})
        self.assertEqual(ud.UpDownMarketMaker.contracts_per_level_by_cadence,
                         {"daily": 1})
        self.assertEqual(ud.UpDownMarketMaker.num_levels_by_cadence, {"daily": 2})
        self.assertEqual(ud.UpDownMarketMaker.quote_offset_by_cadence, {"daily": 4})
        self.assertEqual(ann.AnnualMarketMaker.max_asset, 400)
        self.assertEqual(ud.UpDownMarketMaker.max_asset, 800)
        self.assertEqual(ann.AnnualMarketMaker.poll_secs, 60)
        self.assertEqual(ud.UpDownMarketMaker.poll_secs, 30)

    def test_monthly_fleet_globals_untouched(self):
        self.assertEqual(mm.NUM_LEVELS, 5)
        self.assertEqual(mm.CONTRACTS_PER_LEVEL, 12)
        self.assertEqual(mm.QUOTE_OFFSET_CENTS, 5)

    def test_own_order_prefix_and_status_dir(self):
        self.assertEqual(ann.CLIENT_ORDER_PREFIX, "cay")
        self.assertNotEqual(ann.CLIENT_ORDER_PREFIX, ud.CLIENT_ORDER_PREFIX)
        self.assertNotEqual(ann.CLIENT_ORDER_PREFIX, mm.CLIENT_ORDER_PREFIX)
        self.assertIn("crypto-annual", ann.STATUS_DIR)
        self.assertNotEqual(ann.STATUS_DIR, ud.STATUS_DIR)
        self.assertNotEqual(ann.STATUS_DIR, mm.STATUS_DIR)


if __name__ == "__main__":
    unittest.main()
