#!/usr/bin/env python3
"""Unit tests for q15_sim.py (Q15_PREREG.md). No network: a synthetic
recording in a temp KL-data folder. Run in its own process (the module
points temp_shadow_common at that folder at import):
python -m unittest test_q15_sim
"""
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

import numpy as np

_TMP = tempfile.mkdtemp(prefix="q15_sim_test_")
os.environ["Q15_SHADOW_DATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import q15_sim as Q                                                   # noqa: E402
import temp_shadow_common as C                                       # noqa: E402


class TestFairMath(unittest.TestCase):

    def test_fx_p_up(self):
        self.assertAlmostEqual(Q.fx_p_up(1.0, 1.0, 1e-4, 600, 0.0), 0.5)
        self.assertGreater(Q.fx_p_up(1.0, 1.0, 1e-4, 600, 1e-5), 0.5)      # ties go Yes
        self.assertGreater(Q.fx_p_up(1.0002, 1.0, 1e-5, 60, 1e-5), 0.99)
        self.assertLess(Q.fx_p_up(0.9998, 1.0, 1e-5, 60, 1e-5), 0.01)
        self.assertEqual(Q.fx_p_up(1.0, 1.0, 1e-5, 0, 1e-5), 1.0)           # at the close
        self.assertEqual(Q.fx_p_up(0.99, 1.0, 1e-5, 0, 1e-5), 0.0)

    def test_race_probs(self):
        rng = np.random.default_rng(1)
        cov = np.eye(5) * 1e-6
        p = Q.race_probs(np.zeros(5), cov, 600, rng, draws=20000)
        self.assertAlmostEqual(p.sum(), 1.0)
        self.assertTrue(all(abs(x - 0.2) < 0.02 for x in p))                # symmetric
        # 0.01 ahead with 60 s left = ~2 sd per rival -> ~93%; 0.03 ahead -> certain
        p = Q.race_probs(np.array([0.01, 0, 0, 0, 0]), cov, 60, rng, draws=20000)
        self.assertTrue(0.90 < p[0] < 0.96, p[0])
        p = Q.race_probs(np.array([0.03, 0, 0, 0, 0]), cov, 60, rng)
        self.assertGreater(p[0], 0.999)
        self.assertEqual(list(Q.race_probs(np.array([0, 0.1, 0, 0, 0]), cov, 0, rng)),
                         [0, 1, 0, 0, 0])


class TestQuotes(unittest.TestCase):
    YES = {40: 500.0}           # best YES bid 40
    NO = {55: 500.0}            # best NO bid 55 -> best YES ask 45

    def test_fair_margins_and_no_cross(self):
        # 38 bid behind the 40 best bid, 47 ask above the 45 best ask: neither crosses
        self.assertEqual(Q.desired("fair", 4, 0.425, 600, self.YES, self.NO), (38, 47))
        self.assertEqual(Q.desired("fair", 4, 0.43, 600, {30: 1.0}, {50: 1.0}), (39, 47))
        # clamped to 2-98c
        self.assertEqual(Q.desired("fair", 4, 0.99, 600, {}, {}), (95, None))
        # a bid at/through the best ask would cross: dropped
        self.assertEqual(Q.desired("fair", 1, 0.60, 600, {30: 1.0}, {45: 1.0})[0], None)

    def test_join_only_on_the_safe_side(self):
        self.assertEqual(Q.desired("join", 6, 0.425, 600, self.YES, self.NO), (None, None))
        self.assertEqual(Q.desired("join", 6, 0.47, 600, self.YES, self.NO), (40, None))
        self.assertEqual(Q.desired("join", 6, 0.38, 600, self.YES, self.NO), (None, 45))

    def test_fav_near_the_close_only(self):
        self.assertEqual(Q.desired("fav", 0, 0.999, 200, {}, {}), (97, None))
        self.assertEqual(Q.desired("fav", 0, 0.001, 200, {}, {}), (None, 3))
        self.assertEqual(Q.desired("fav", 0, 0.999, 400, {}, {}), (None, None))
        self.assertEqual(Q.desired("fav", 0, 0.99, 200, {}, {}), (None, None))

    def test_fill_pnl(self):
        # bought 100 YES at 40c and sold 100 at 60c: +$20 whatever settles
        fills = [(0, "bid", 40, 100.0), (1, "ask", 60, 100.0)]
        self.assertAlmostEqual(Q.fill_pnl(fills, "yes"), (100 - 40) + (60 - 100))
        self.assertAlmostEqual(Q.fill_pnl(fills, "no"), (0 - 40) + (60 - 0))
        self.assertAlmostEqual(Q.fill_pnl(fills[:1], "no"), -40.0)
        self.assertIsNone(Q.fill_pnl(fills, None))


def _w(stream, rx, rec):
    rec = dict(rec, rx=rx)
    p = C.raw_file(stream, rx, create=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class TestSyntheticDay(unittest.TestCase):
    """One EUR/USD window 2026-10-23 14:00-14:15Z (ET day 10/23): a steady
    Kraken mid with tiny noise, a two-sided book, a program, one taker selling
    YES through 45c, and the result Yes."""

    @classmethod
    def setUpClass(cls):
        o = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc).timestamp()
        c = o + 900
        t = "KXEURUSD15M-26OCT231015-30"
        _w("meta", o - 3000, {"kind": "market", "market": {
            "ticker": t, "open_time": C.iso(o), "close_time": C.iso(c), "status": "active",
            "result": "yes"}})
        _w("meta", o - 2990, {"kind": "programs", "programs": [{
            "market_ticker": t, "period_reward": 500000, "target_size_fp": "300.00",
            "discount_factor_bps": 5000, "start_date": C.iso(o), "end_date": C.iso(c)}]})
        # a mid that ticks up and back each second: sigma > 0, fair ~0.5 at every
        # second it quotes (at the open's level on even seconds)
        for s in range(int(o - 1800), int(c) + 30):
            p = 1.16 + (1e-5 if s % 2 else 0.0)
            _w("px", s + 0.2, {"src": "kraken", "px": {"EURUSD": [p, p - 1e-5, p + 1e-5]}})
        _w("ws", o - 50, {"marker": "connect", "detail": "x"})
        _w("ws", o - 40, {"m": {"type": "orderbook_snapshot", "msg": {
            "market_ticker": t, "yes_dollars_fp": [["0.4000", "400.00"], ["0.4500", "300.00"]],
            "no_dollars_fp": [["0.4500", "400.00"], ["0.5000", "300.00"]]}}})
        _w("ws", o + 300, {"m": {"type": "trade", "msg": {
            "market_ticker": t, "yes_price_dollars": "0.4400", "count_fp": "50.00",
            "taker_side": "no", "ts_ms": int((o + 300) * 1000), "trade_id": "x1"}}})
        cls.out = Q.simulate_day(date(2026, 10, 23))

    def test_it_quotes_scores_and_fills(self):
        d = self.out
        self.assertEqual((d["markets"], d["settled"], d["with_program"]), (1, 1, 1))
        v = d["variants"]["FAIR4"]
        self.assertGreater(v["reward"], 0)             # it rested inside a paying book
        self.assertGreaterEqual(v["fills"], 1)         # the 44c YES sale hit its bid (stress)
        self.assertGreater(v["pnl"], 0)                # bought YES below 50c, settled Yes
        self.assertAlmostEqual(v["net"], v["reward"] + v["pnl"])
        self.assertEqual(d["variants"]["FAIR4_night"]["net"], 0.0)   # 10:00 ET is day
        self.assertEqual(d["fidelity"]["n"], 1)
        self.assertTrue(os.path.exists(os.path.join(_TMP, "sim", "2026-10-23", "q15_day.json")))


if __name__ == "__main__":
    unittest.main()
