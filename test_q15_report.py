#!/usr/bin/env python3
"""Unit tests for q15_report.py (the verdict rules of Q15_PREREG.md). No
network, no email. Run in its own process: python -m unittest test_q15_report
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

_TMP = tempfile.mkdtemp(prefix="q15_report_test_")
os.environ["Q15_SHADOW_DATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import q15_report as R                                                # noqa: E402
import temp_shadow_common as C                                       # noqa: E402


def _day(d: date, nets: dict, brier=(0.20, 0.22)):
    out = {"day": d.isoformat(), "markets": 960, "settled": 960, "with_program": 960,
           "coverage": {"ws_rows": 1, "trades": 1, "untrusts": 0},
           "fidelity": {"n": 480, "agree": 470},
           "brier": {"KXEURUSD15M|60": {"n": 100, "fair": brier[0], "mid": brier[1]}},
           "variants": {}}
    for vid in R.Q.VARIANTS:
        net = nets.get(vid, 0.0)
        out["variants"][vid] = {"net": net, "reward": max(net, 0), "pnl": min(net, 0), "fills": 3,
                                "night": net / 2, "by_hour": {"1": net * 0.4, "2": net * 0.35,
                                                              "3": net * 0.25}}
    C.write_json_atomic(os.path.join(_TMP, "sim", d.isoformat(), "q15_day.json"), out)


class TestVerdict(unittest.TestCase):

    def setUp(self):
        import shutil
        shutil.rmtree(os.path.join(_TMP, "sim"), ignore_errors=True)

    def _week(self, f, brier=(0.20, 0.22)):
        for i in range(7):
            d = R.VERDICT_START + timedelta(days=i)
            _day(d, f(i), brier)
        return R.window_days(R.VERDICT_START + timedelta(days=6))

    def test_a_steady_winner_is_pilot_eligible(self):
        days = self._week(lambda i: {"FAIR8": 40.0 + i})
        v = R.verdict("FAIR8", days)
        self.assertEqual((v["days"], v["pos"], v["level"]), (7, 7, "PILOT ELIGIBLE"))
        self.assertAlmostEqual(v["total"], sum(40.0 + i for i in range(7)))

    def test_one_lucky_day_does_not_pass(self):
        days = self._week(lambda i: {"FAIR4": 500.0 if i == 0 else -20.0})
        v = R.verdict("FAIR4", days)
        self.assertGreater(v["total"], 0)
        self.assertEqual(v["level"], "NO-GO")
        self.assertFalse(v["checks"][">= 5/7 days positive"])

    def test_a_fair_that_does_not_beat_the_mid_fails_everything(self):
        days = self._week(lambda i: {"JOIN": 30.0}, brier=(0.25, 0.20))
        self.assertEqual(R.verdict("JOIN", days)["level"], "NO-GO")

    def test_fewer_than_seven_days_extend(self):
        for i in range(3):
            _day(R.VERDICT_START + timedelta(days=i), {"FAIR8_night": 25.0})
        days = R.window_days(R.VERDICT_START + timedelta(days=2))
        self.assertEqual(R.verdict("FAIR8_night", days)["level"], "EXTEND")
        self.assertEqual(R.verdict("FAV_night", days)["level"], "NO-GO")    # nets 0: net > 0 fails
        self.assertEqual(R.verdict("FAIR4", [])["level"], "NO DATA")

    def test_the_email(self):
        days = self._week(lambda i: {"FAIR8": 40.0})
        subj, page, text = R.build(R.VERDICT_START + timedelta(days=6))
        self.assertEqual(subj, "Q15 shadow 10/29: best FAIR8 $280.00 over 7 day(s) | PILOT ELIGIBLE")
        self.assertIn("day 7 of 7", text)
        self.assertIn("agreed with Kalshi on 470 of 480 closes (98%)", text)
        self.assertIn("the fair adds information", text)
        self.assertIn("<pre>", page)

    def test_bootstrap_low(self):
        self.assertEqual(R.bootstrap_lo([]), 0.0)
        self.assertAlmostEqual(R.bootstrap_lo([10.0] * 7), 70.0)
        self.assertLess(R.bootstrap_lo([100.0, -100.0] * 3), 0.0)


if __name__ == "__main__":
    unittest.main()
