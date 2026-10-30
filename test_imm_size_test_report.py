#!/usr/bin/env python3
"""Unit tests for imm_size_test_report.py (Jack 2026-10-22: "I want to test
increasing base contract size by 1.5x" -> "Split test").

Run: python -m unittest test_imm_size_test_report
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_size_test_report as r                                     # noqa: E402
import incentive_mm as imm                                           # noqa: E402

FIELDS = ["rew", "pnl", "real"]


def dashboard(days):
    """A data object with per-market rows: days = {day: {series: (rew, pnl)}}."""
    return {"day_fields": FIELDS,
            "days": {d: {"ok": True, "m": {f"{s}-26DEC31-T1": [v[0], v[1], 0.0]
                                           for s, v in rows.items()}}
                     for d, rows in days.items()}}


class TestSizeTestReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_size_rep_")
        for p in (mock.patch.object(imm, "STATUS_DIR", self.dir),
                  mock.patch.dict(imm._size_test_cache, clear=True),
                  mock.patch.dict(imm._size_test_stop, {"checked": 0.0, "stopped": False})):
            p.start()
            self.addCleanup(p.stop)
        names = [f"KXS{i}" for i in range(40)]
        self.test = [s for s in names if imm.size_test_arm(s) == "test"][:3]
        self.ctrl = [s for s in names if imm.size_test_arm(s) == "control"][:3]

    def days(self, n_test, test_rew=12.0, test_pnl=-6.0):
        """14 baseline days (both halves rew 10 / pnl -5 a series) then
        n_test test days with the test half at test_rew / test_pnl."""
        out = {}
        start = date(2026, 10, 26)
        for k in range(-14, n_test):
            d = (start + timedelta(days=k)).isoformat()
            rows = {s: (10.0, -5.0) for s in self.ctrl}
            rows.update({s: (10.0, -5.0) if k < 0 else (test_rew, test_pnl) for s in self.test})
            out[d] = rows
        return out

    def test_the_score_is_a_difference_in_differences(self):
        sc = r.score(dashboard(self.days(7)), date(2026, 11, 2))
        self.assertEqual(len(sc["test_days"]), 7)
        self.assertAlmostEqual(sc["rew"]["mean"], 3 * 2.0)            # 3 series, +$2 each
        self.assertAlmostEqual(sc["pnl"]["mean"], 3 * -1.0)
        self.assertAlmostEqual(sc["net"]["mean"], 3.0)
        self.assertEqual(r.verdict(sc), "KEEP")                        # no noise: SE 0
        self.assertAlmostEqual(r._lift(sc, 0), 0.2)
        subject, text = r.body(sc, "WEEK 1")
        self.assertTrue(subject.startswith("IMM size test week 1 (7 of 14 days): KEEP -- net +$3/day"))
        self.assertIn("rewards +20% (size elasticity 0.45)", text)
        self.assertIsNone(r.score(dashboard(self.days(0)), date(2026, 10, 26)))   # no test day yet

    def test_saturdays_split_out_the_x3_half_in_dollars(self):
        # Jack 2026-10-26: "Add a Saturday split to the size-test report so
        # 10/31 and 11/07 show the x3 test half against the x2 control half
        # in dollars"
        days = self.days(7)
        days["2026-10-31"].update({s: (25.0, -9.0) for s in self.test})     # the x3 Saturday
        with mock.patch.object(imm, "SAT_SIZE_MULT", 2.0):
            sc = r.score(dashboard(days), date(2026, 11, 2))
            sp = sc["sat"]
            self.assertEqual(sp["saturdays"], ["2026-10-31"])
            self.assertEqual(sp["base_saturdays"], ["2026-10-17", "2026-10-24"])
            self.assertEqual(sp["mults"]["2026-10-31"], (3.0, 2.0))
            self.assertAlmostEqual(sp["rew"]["daily"][0], 3 * 15.0)        # 25 vs 10 a series, pre gap 0
            self.assertAlmostEqual(sp["pnl"]["daily"][0], 3 * -4.0)        # -9 vs -5
            self.assertAlmostEqual(sp["net"]["total"], 33.0)
            subject, text = r.body(sc, "WEEK 1")
            text.encode("cp1252")
            self.assertIn("Saturdays, long-dated series (the test half at x3 = Saturday x2 x 1.5, "
                          "the control at x2)", text)
            self.assertIn("10-31  rewards +$45, fills -$12, net +$33", text)
            self.assertIn("(test half rewards $75 / fills -$27; control $30 / -$15)", text)
            self.assertIn("Saturday rewards +150% (elasticity 2.26)", text)
        # a daily series is not sized by the Saturday mult: out of the split
        with mock.patch.object(imm, "is_daily_series", lambda s: s in self.test):
            self.assertEqual(r.score(dashboard(days), date(2026, 11, 2))["sat"]["rew"]["daily"], [0.0])
        # before a test Saturday: no section
        sc = r.score(dashboard(self.days(4)), date(2026, 10, 30))
        self.assertIsNone(sc["sat"])
        self.assertNotIn("Saturdays, long-dated", r.body(sc, "WEEK 1")[1])

    def hourly_and_fills(self, n_test):
        """The reward recon's hourly cache and the data object's fills over the
        14 baseline days and n_test test days: every series earns $1 at 03:00
        ET, $2 at 14:00 and $0.50 at 23:00 (the hour that marks a day covered),
        and fills 50 contracts at 04:01 at a 4h mark-out of -10c; on test days
        the test half earns $3 at 03:00 and fills 100 contracts."""
        cache, fills = {}, []
        start = date(2026, 10, 26)
        at = lambda d, h: imm.ET.localize(datetime(d.year, d.month, d.day, h)).timestamp()
        for k in range(-14, n_test):
            d = start + timedelta(days=k)
            mk = cache.setdefault(f"cycle_log_{d.isoformat()}.csv", {"sig": "", "markets": {}})["markets"]
            for s in self.test + self.ctrl:
                up = k >= 0 and s in self.test
                t = f"{s}-26DEC31-T1"
                mk[t] = {str(int(at(d, 3) // 3600)): 3.0 if up else 1.0,
                         str(int(at(d, 14) // 3600)): 2.0,
                         str(int(at(d, 23) // 3600)): 0.5}
                fills.append({"ts": at(d, 4) + 60, "t": t, "side": "bid", "px": 40.0,
                              "n": 100.0 if up else 50.0, "pad": False, "taker": False, "m4c": -10.0})
        return cache, fills

    def test_hour_blocks_split_out_where_the_extra_size_paid(self):
        # Jack 2026-10-29: "Size-test hour split" -- the test's x1.5 read per
        # ET hour block, the evidence for a time-of-day multiplier
        D = dashboard(self.days(7))
        cache, fills = self.hourly_and_fills(7)
        t0 = fills[-1]["ts"]
        D["fills"] = fills + [
            {"ts": t0, "t": f"{self.test[0]}-26DEC31-T1", "side": "ask", "px": 50.0, "n": 900.0,
             "pad": True, "taker": False, "m4c": -40.0},                       # a pad: left out
            {"ts": t0, "t": f"{self.test[0]}-26DEC31-T1", "side": "ask", "px": 50.0, "n": 900.0,
             "pad": False, "taker": False}]                                    # no 4h mark yet
        sc = r.score(D, date(2026, 11, 2))
        hs = r.hour_split(D, cache, sc["test_days"], sc["base_days"])
        self.assertEqual((len(hs["days"]), len(hs["base_days"]), hs["skipped"]), (7, 14, 1))
        q = hs["blocks"]["00-08"]
        self.assertAlmostEqual(q["rew"]["mean"], 3 * 2.0)              # $3 vs $1 at 03:00, 3 series
        self.assertAlmostEqual(q["pnl"]["mean"], 3 * -5.0)             # 100 vs 50 contracts at -10c
        self.assertAlmostEqual(q["net"]["mean"], -9.0)
        self.assertAlmostEqual(q["rew_lift"], 2.0)
        self.assertAlmostEqual(q["cts_lift"], 1.0)
        self.assertAlmostEqual(hs["blocks"]["13-17"]["rew"]["mean"], 0.0)
        self.assertIsNone(hs["blocks"]["13-17"]["cts_lift"])          # nothing filled there
        day8 = imm.ET.localize(datetime(2026, 11, 2, 7, 0)).astimezone(timezone.utc)
        with mock.patch.object(r, "load_hourly_cache", return_value=cache):
            _line, mail = r.run(D, {}, True, day8)
        text = mail[1]
        text.encode("cp1252")
        self.assertIn("By ET hour block, per day, test minus control less their pre-test gap "
                      "(7 test days and 14 before that the hourly reward cache covers)", text)
        self.assertIn("(1 fills with no 4h mark left out):", text)
        self.assertIn("  00-08  rewards +$6, fills -$15, net -$9 (+/- $0); size elasticity of "
                      "rewards 2.71, of contracts filled 1.71", text)
        self.assertIn("  13-17  rewards +$0, fills +$0, net +$0 (+/- $0); size elasticity of "
                      "rewards 0.00, of contracts filled -", text)
        # a day the cache does not cover through 23:00 ET drops out
        h23 = str(int(imm.ET.localize(datetime(2026, 11, 1, 23)).timestamp() // 3600))
        for by_h in cache["cycle_log_2026-11-01.csv"]["markets"].values():
            del by_h[h23]
        self.assertEqual(r.hour_split(D, cache, sc["test_days"], sc["base_days"])["days"][-1], "2026-10-31")
        # no cache (the temp STATUS_DIR has none): the email goes out without the section
        self.assertIsNone(r.load_hourly_cache())
        _line, mail = r.run(D, {}, True, day8)
        self.assertNotIn("By ET hour block", mail[1])
        self.assertIn("week 1", mail[0])

    def test_week_one_then_final_emails_once_each(self):
        st = {}
        D = dashboard(self.days(7))
        day8 = imm.ET.localize(datetime(2026, 11, 2, 7, 0)).astimezone(timezone.utc)
        line, mail = r.run(D, st, False, day8)
        self.assertIn("week 1", mail[0])
        self.assertIn("size test: day 7", line)
        self.assertEqual(r.run(D, st, False, day8 + timedelta(hours=1)), ("", None))   # once a day
        _line, mail = r.run(D, st, False, day8 + timedelta(days=1))
        self.assertIsNone(mail)                                        # day 8: nothing new
        D = dashboard(self.days(14))
        _line, mail = r.run(D, st, False, day8 + timedelta(days=7))
        self.assertIn("final (14 days)", mail[0])
        self.assertFalse(r.due(st, day8 + timedelta(days=8)))          # done after the final
        et = lambda *a: imm.ET.localize(datetime(*a)).astimezone(timezone.utc)
        self.assertFalse(r.due({}, et(2026, 10, 26, 12, 0)))           # first day not complete
        self.assertFalse(r.due({}, et(2026, 10, 27, 5, 0)))            # before 06:00 ET
        self.assertTrue(r.due({}, et(2026, 10, 27, 6, 0)))

    def test_the_stop_rule_writes_the_stop_file(self):
        st = {}
        D = dashboard(self.days(3, test_rew=10.0, test_pnl=-150.0))   # -$435/day on 3 series
        day4 = imm.ET.localize(datetime(2026, 10, 29, 7, 0)).astimezone(timezone.utc)
        line, mail = r.run(D, st, True, day4)                          # dry: no file
        self.assertIn("STOP RULE TRIPPED", line)
        self.assertFalse(os.path.exists(r.stop_path()))
        line, mail = r.run(D, {}, False, day4)
        self.assertIn("STOPPED after 3 day(s): STOP", mail[0])
        self.assertTrue(os.path.exists(r.stop_path()))
        self.assertEqual(imm.size_test_mult(self.test[0], day4.timestamp()), 1.0)  # bot at 1x


if __name__ == "__main__":
    unittest.main()
