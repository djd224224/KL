#!/usr/bin/env python3
"""Unit tests for rain_fair.py's station table and day probability.

Run: python -m unittest test_rain_fair
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rain_fair as rf                                              # noqa: E402


class TestStations(unittest.TestCase):
    def test_every_station_is_complete(self):
        for city, st in rf.STATIONS.items():
            self.assertEqual(set(st), {"cli", "name", "lat", "lon", "tz"}, city)
            self.assertTrue(st["cli"].startswith("CLI") and len(st["cli"]) == 6, city)
            pytz.timezone(st["tz"])                                 # raises if unknown
            self.assertTrue(24 < st["lat"] < 50 and -125 < st["lon"] < -66, city)

    def test_the_cities_listed_after_july_are_priced(self):
        # Jack 2026-10-09 "fix": CMH/TAM/ABQ/LEX (and nine more) had no row
        for city in ("ABQ", "CLL", "CMH", "EWR", "IND", "LEX", "MKE", "PIT",
                     "PVD", "SGF", "STL", "TAM", "TTN"):
            self.assertIn(city, rf.STATIONS)
        self.assertEqual(rf.STATIONS["TAM"]["cli"], "CLITPA")        # Tampa settles on TPA
        self.assertEqual(len(rf.STATIONS), 33)


class TestDayProbability(unittest.TestCase):
    def test_remaining_hours_of_the_local_day_only(self):
        tz = "America/New_York"
        start = pytz.timezone(tz).localize(datetime(2026, 10, 10, 0)).astimezone(timezone.utc)
        pops = [(start + timedelta(hours=h), 0.5 if h == 15 else 0.0) for h in range(30)]
        now = start + timedelta(hours=1)
        e = rf.day_probability(pops, tz, "2026-10-10", now)
        self.assertEqual(e["max_hour"], 0.5)
        self.assertAlmostEqual(e["p"], 0.5)                          # max-hour floor binds
        late = start + timedelta(hours=16)                           # the wet hour has passed
        self.assertEqual(rf.day_probability(pops, tz, "2026-10-10", late)["p"], rf.P_FLOOR)


if __name__ == "__main__":
    unittest.main()
