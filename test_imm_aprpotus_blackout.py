#!/usr/bin/env python3
"""Tests for the RCP weekly's weekday morning stand-down (Jack 2026-11-26: "Yes,
build it"): KXAPRPOTUS quotes nothing Mon-Fri 08:00-12:00 ET, through the same
blackout path as the CPI pilot and the 10-year's 06-07 ET window; the twin
daily's rules and the weekly's size are unchanged.

Run: python -m unittest test_imm_aprpotus_blackout
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incentive_mm as imm                                           # noqa: E402

ET = ZoneInfo("America/New_York")


def at(mo, d, h, mi=0):
    return datetime(2026, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


class AprpotusMorningStandDown(unittest.TestCase):
    def test_weekday_mornings_only(self):
        s = "KXAPRPOTUS"
        # Tue 11/17 (26NOV20's losing morning), Sat 11/21, Sun 11/22, Fri 11/27
        self.assertFalse(imm.series_in_blackout(s, at(11, 17, 7, 59)))
        self.assertTrue(imm.series_in_blackout(s, at(11, 17, 8, 0)))
        self.assertTrue(imm.series_in_blackout(s, at(11, 17, 11, 59)))
        self.assertFalse(imm.series_in_blackout(s, at(11, 17, 12, 0)))
        self.assertFalse(imm.series_in_blackout(s, at(11, 17, 22, 38)))
        self.assertFalse(imm.series_in_blackout(s, at(11, 21, 9, 0)))
        self.assertFalse(imm.series_in_blackout(s, at(11, 22, 9, 0)))
        self.assertTrue(imm.series_in_blackout(s, at(11, 27, 9, 0)))

    def test_the_twin_and_the_weekly_size_are_unchanged(self):
        ov = imm.SERIES_OVERRIDES["KXAPRPOTUS"]
        tw = imm.SERIES_OVERRIDES["KXTRUMPAPPROVE"]
        self.assertEqual(ov.size_mult, imm.APRPOTUS_SIZE_MULT)
        self.assertEqual(ov.event_day_cutoff_et, tw.event_day_cutoff_et)
        self.assertEqual((ov.blackout_et, ov.blackout_weekdays_only), (("08:00", "12:00"), True))
        self.assertIsNone(tw.blackout_et)
        self.assertFalse(imm.series_in_blackout("KXTRUMPAPPROVE", at(11, 17, 9, 0)))

    def test_the_window_parses_and_turns_off(self):
        self.assertEqual(imm.aprpotus_blackout("08:00-12:00"), ("08:00", "12:00"))
        self.assertEqual(imm.aprpotus_blackout(" 07:30 - 13:00 "), ("07:30", "13:00"))
        for raw in ("", "off", "8-12", "08:00", "08:00-12:00-13:00", None):
            self.assertIsNone(imm.aprpotus_blackout(raw), raw)

    def test_the_knob_is_recorded_and_the_guard_classified(self):
        self.assertIn("APRPOTUS_BLACKOUT_ET", imm._CONFIG_CODE_KNOBS)
        import imm_risk_controls as irc
        self.assertIn("aprpotus_blackout", irc.BLACKOUT_GUARDS)
        self.assertIn("aprpotus_blackout", irc.GUARD_LABELS)


if __name__ == "__main__":
    unittest.main()
