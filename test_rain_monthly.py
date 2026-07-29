#!/usr/bin/env python3
"""Unit tests for rain_monthly.py — run: python -m unittest test_rain_monthly
All network access is monkeypatched; no test touches IEM/ACIS/NWS/Kalshi."""

import random
import unittest
from datetime import datetime, timezone

import pytz

import rain_monthly as rm


class TestParsePrecip(unittest.TestCase):
    def test_values(self):
        self.assertEqual(rm.parse_precip("T"), 0.0)
        self.assertEqual(rm.parse_precip(0.0001), 0.0)   # IEM trace marker
        self.assertEqual(rm.parse_precip("1.20A"), 1.20)  # ACIS accum flag
        self.assertEqual(rm.parse_precip("0.25"), 0.25)
        self.assertEqual(rm.parse_precip(0.25), 0.25)
        self.assertIsNone(rm.parse_precip("M"))
        self.assertIsNone(rm.parse_precip(None))
        self.assertIsNone(rm.parse_precip(""))


class TestDurations(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(rm._iso_duration_hours("PT6H"), 6.0)
        self.assertEqual(rm._iso_duration_hours("PT4H"), 4.0)
        self.assertEqual(rm._iso_duration_hours("P1DT6H"), 30.0)

    def test_split_drops_elapsed(self):
        tz = pytz.timezone("US/Eastern")
        now = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
        t0 = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)  # 6h period,
        parts = list(rm._split_local_days(t0, 12.0, tz, now))   # half elapsed
        total = sum(f for _d, f in parts)
        self.assertAlmostEqual(total, 0.5, places=3)            # half survives
        self.assertTrue(all(d == "2026-07-28" for d, _f in parts))

    def test_split_crosses_local_midnight(self):
        tz = pytz.timezone("US/Eastern")
        now = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)
        t0 = datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc)   # 10pm ET ->
        parts = dict(rm._split_local_days(t0, 6.0, tz, now))    # 4am ET
        self.assertAlmostEqual(parts["2026-07-28"], 2 / 6, places=3)
        self.assertAlmostEqual(parts["2026-07-29"], 4 / 6, places=3)


class TestEffectiveMtd(unittest.TestCase):
    def setUp(self):
        self._orig = (rm.fetch_cli_state, rm.fetch_iem_daily, rm.fetch_pday)
        self.now = datetime(2026, 7, 28, 23, 0, tzinfo=timezone.utc)  # 7pm ET

    def tearDown(self):
        rm.fetch_cli_state, rm.fetch_iem_daily, rm.fetch_pday = self._orig

    def test_gap_days_and_pday(self):
        # CLI through 7/26; obs 7/27 (0.30) is a gap day; today pday 0.10
        rm.fetch_cli_state = lambda c, y, m: {
            "mtd": 2.00, "date": "2026-07-26", "day_precip": 0.0}
        rm.fetch_iem_daily = lambda c, y, m: {
            "2026-07-26": 0.50, "2026-07-27": 0.30, "2026-07-28": 0.10}
        rm.fetch_pday = lambda c: (0.10, "2026-07-28T18:53:00")
        out = rm.effective_mtd("MIA", 2026, 7, self.now)
        self.assertAlmostEqual(out["mtd"], 2.40)      # 2.00 + .30 gap + .10 pday
        self.assertAlmostEqual(out["obs_added"], 0.40)

    def test_same_day_cli_double_count_guard(self):
        # Evening CLI for today already counted 0.05; pday now 0.12 ->
        # only the 0.07 beyond the report adds
        rm.fetch_cli_state = lambda c, y, m: {
            "mtd": 3.97, "date": "2026-07-28", "day_precip": 0.05}
        rm.fetch_iem_daily = lambda c, y, m: {}
        rm.fetch_pday = lambda c: (0.12, "2026-07-28T22:53:00")
        out = rm.effective_mtd("NYC", 2026, 7, self.now)
        self.assertAlmostEqual(out["mtd"], 4.04)
        self.assertFalse(out["stale"])

    def test_fresh_month_obs_only(self):
        rm.fetch_cli_state = lambda c, y, m: {"mtd": None, "date": None,
                                              "day_precip": None}
        rm.fetch_iem_daily = lambda c, y, m: {"2026-07-27": 0.20}
        rm.fetch_pday = lambda c: (0.05, "t")
        out = rm.effective_mtd("MIA", 2026, 7, self.now)
        self.assertAlmostEqual(out["mtd"], 0.25)

    def test_stale_cli_flag(self):
        rm.fetch_cli_state = lambda c, y, m: {
            "mtd": 1.0, "date": "2026-07-20", "day_precip": 0.0}
        rm.fetch_iem_daily = lambda c, y, m: {}
        rm.fetch_pday = lambda c: (0.0, "t")
        out = rm.effective_mtd("MIA", 2026, 7, self.now)
        self.assertTrue(out["stale"])


class TestSegments(unittest.TestCase):
    def test_missing_filtering(self):
        hist = {2001: [0.1] * 31,
                2002: [0.1] * 20 + [None] * 11,        # too many missing
                2003: [0.2] * 30 + [None]}             # one missing -> 0.0
        segs = rm.build_segments(hist, 29, 31)
        self.assertEqual(len(segs), 2)                 # 2002 dropped
        self.assertEqual(segs[1], [0.2, 0.2, 0.0])


class TestPricing(unittest.TestCase):
    def test_price_rungs(self):
        totals = sorted([0.0, 0.5, 1.0, 2.0])
        p = rm.price_rungs(4.0, totals, [3.0, 4.2, 5.0, 7.0])
        self.assertEqual(p[3.0], 1.0)                  # already crossed
        self.assertEqual(p[4.2], 0.75)                 # needs >0.2
        self.assertEqual(p[5.0], 0.25)                 # needs STRICTLY >1.0:
        #   only the 2.0 sample crosses (1.0 exactly -> total 5.0 -> NO)
        self.assertEqual(p[7.0], 0.0)
        for a, b in zip([3.0, 4.2, 5.0, 7.0][:-1], [4.2, 5.0, 7.0]):
            self.assertGreaterEqual(p[a], p[b])        # monotone

    def test_empty_totals(self):
        p = rm.price_rungs(4.0, [], [3.0, 5.0])
        self.assertEqual(p[3.0], 1.0)
        self.assertEqual(p[5.0], 0.0)

    def test_draw_day_forecast_scales(self):
        rng = random.Random(7)
        wet = {"qpf_in": 1.0, "pop": 0.9}
        dry = {"qpf_in": 0.01, "pop": 0.1}
        wet_mean = sum(rm._draw_day(wet, 1.0, rng) for _ in range(4000)) / 4000
        dry_mean = sum(rm._draw_day(dry, 1.0, rng) for _ in range(4000)) / 4000
        self.assertGreater(wet_mean, 0.7)              # ~= QPF 1.0
        self.assertLess(abs(wet_mean - 1.0), 0.25)
        self.assertLess(dry_mean, 0.05)


class TestFeesAndParsing(unittest.TestCase):
    def test_taker_fee(self):
        # 10 contracts at 50c: 0.07*10*0.25 = $0.175 -> ceil $0.18 -> 1.8c/ct
        self.assertAlmostEqual(rm.taker_fee_cents(50, 10), 1.8)
        # cheap tails are near-free: 5c, 10 contracts: 0.07*10*0.0475=$0.03325
        self.assertAlmostEqual(rm.taker_fee_cents(5, 10), 0.4)  # ceil $0.04

    def test_parse_event_month(self):
        self.assertEqual(rm.parse_event_month("KXRAINMIAM-26JUL"), (2026, 7))
        self.assertEqual(rm.parse_event_month("KXRAINSTPM-26AUG"), (2026, 8))
        self.assertIsNone(rm.parse_event_month("BADTICKER"))


def _market(strike, bid, ask, bid_sz=100, ask_sz=100, ticker="KXRAINMIAM-26JUL-5"):
    return {"ticker": ticker, "floor_strike": strike,
            "yes_bid_dollars": f"{bid/100:.4f}", "yes_ask_dollars": f"{ask/100:.4f}",
            "yes_bid_size_fp": str(bid_sz), "yes_ask_size_fp": str(ask_sz)}


class TestEvaluate(unittest.TestCase):
    def test_buy_yes_on_edge(self):
        row = rm.evaluate_market(_market(5, 27, 28), 0.45, 4.83, False, {})
        self.assertEqual(row["action"], "BUY_YES")
        self.assertEqual(row["px"], 28)
        self.assertGreaterEqual(row["yes_edge"], rm.MIN_EDGE_YES)

    def test_buy_no_on_edge(self):
        row = rm.evaluate_market(_market(5, 27, 28), 0.10, 4.0, False, {})
        self.assertEqual(row["action"], "BUY_NO")
        self.assertEqual(row["px"], 100 - 27)

    def test_no_action_inside_threshold(self):
        row = rm.evaluate_market(_market(5, 27, 28), 0.30, 4.0, False, {})
        self.assertIsNone(row["action"])

    def test_boundary_guard(self):
        row = rm.evaluate_market(_market(5, 27, 28), 0.45, 4.98, False, {})
        self.assertIsNone(row["action"])
        self.assertTrue(any("BOUNDARY" in w for w in row["why"]))

    def test_crossed_no_action(self):
        row = rm.evaluate_market(_market(5, 99, 100), 1.0, 5.5, False, {})
        self.assertIsNone(row["action"])
        self.assertIn("crossed", row["why"])

    def test_wide_book_guard(self):
        row = rm.evaluate_market(_market(5, 10, 40), 0.45, 4.0, False, {})
        self.assertIsNone(row["action"])

    def test_stale_cli_guard(self):
        row = rm.evaluate_market(_market(5, 27, 28), 0.45, 4.0, True, {})
        self.assertIsNone(row["action"])

    def test_position_cap_shrinks_and_blocks(self):
        pos = {"KXRAINMIAM-26JUL-5": rm.MAX_POS - 3}
        row = rm.evaluate_market(_market(5, 27, 28), 0.45, 4.0, False, pos)
        self.assertEqual(row["action"], "BUY_YES")
        self.assertEqual(row["size"], 3)               # room to cap only
        pos = {"KXRAINMIAM-26JUL-5": rm.MAX_POS}
        row = rm.evaluate_market(_market(5, 27, 28), 0.45, 4.0, False, pos)
        self.assertIsNone(row["action"])               # at cap

    def test_book_size_caps_order(self):
        row = rm.evaluate_market(_market(5, 27, 28, ask_sz=4), 0.45, 4.0,
                                 False, {})
        self.assertEqual(row["size"], 4)


class TestSimulate(unittest.TestCase):
    def setUp(self):
        self._orig = (rm.fetch_history, rm.fetch_forecast_days)

    def tearDown(self):
        rm.fetch_history, rm.fetch_forecast_days = self._orig

    def test_forecast_injection_raises_totals(self):
        # dry climatology, wet 3-day forecast -> totals reflect forecast
        rm.fetch_history = lambda c, m: {y: [0.0] * 31 for y in range(1990, 2020)}
        now = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        wet = {f"2026-07-{d}": {"qpf_in": 1.0, "pop": 0.95, "complete": True}
               for d in (28, 29, 30, 31)}
        rm.fetch_forecast_days = lambda c, n: wet
        sim = rm.simulate_remaining("MIA", 2026, 7, now, n_samples=3000,
                                    rng=random.Random(1))
        med = sim["totals"][len(sim["totals"]) // 2]
        self.assertGreater(med, 1.5)                   # several wet days land
        rm.fetch_forecast_days = lambda c, n: {}
        sim_dry = rm.simulate_remaining("MIA", 2026, 7, now, n_samples=1000,
                                        rng=random.Random(1))
        self.assertEqual(sim_dry["totals"][-1], 0.0)   # pure dry climatology

    def test_future_month_uses_full_month(self):
        rm.fetch_history = lambda c, m: {y: [0.1] * 31 for y in range(1990, 2020)}
        rm.fetch_forecast_days = lambda c, n: {}
        now = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        sim = rm.simulate_remaining("MIA", 2026, 8, now, n_samples=500,
                                    rng=random.Random(2))
        self.assertAlmostEqual(sim["totals"][0], 3.1, places=5)  # 31 x 0.1


if __name__ == "__main__":
    unittest.main()
