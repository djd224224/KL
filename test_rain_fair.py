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


class TestAutoStations(unittest.TestCase):
    """Jack 2026-10-09: a city Kalshi adds is modeled from its own rules,
    and anything that can't be goes to "unmapped" (the watch alerts on it)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="rain_fair_test_")
        self.path = os.path.join(self.dir, "rain_fair_values.json")
        self.resolved = []

    def _resolver(self, cli):
        self.resolved.append(cli)
        if cli == "CLIBAD":
            raise RuntimeError("no NWS station for CLIBAD")
        return {"cli": cli, "name": "Test " + cli, "lat": 35.0, "lon": -90.0,
                "tz": "America/Chicago", "station": "K" + cli[3:], "auto": True}

    def _write(self, listing, **kw):
        from unittest import mock
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        pops = [(start + timedelta(hours=h), 0.4) for h in range(72)]
        with mock.patch.object(rf, "resolve_hourly_url", lambda c, st=None: "u:" + c), \
                mock.patch.object(rf, "fetch_hourly_pops", lambda url: pops), \
                mock.patch.object(rf, "STATIONS", {"NYC": rf.STATIONS["NYC"]}), \
                mock.patch.object(rf.time, "sleep", lambda s: None):
            ok, failed = rf.write_fair_file(self.path, lister=lambda gj: dict(listing),
                                            resolver=self._resolver, **kw)
        import json
        with open(self.path, encoding="utf-8") as f:
            return ok, failed, json.load(f)

    def test_a_new_city_is_priced_and_an_unresolvable_one_is_flagged(self):
        ok, failed, data = self._write({"NYC": "CLINYC", "BNA": "CLIBNA",
                                        "XYZ": None, "BAD": "CLIBAD"})
        self.assertEqual((ok, failed), (2, 0))                # NYC + the auto BNA
        self.assertEqual(self.resolved, ["CLIBAD", "CLIBNA"])  # the seed NYC is never resolved
        self.assertEqual(data["auto_stations"]["BNA"]["station"], "KBNA")
        self.assertTrue(any("BNA" in cities for cities in data["fair"].values()))
        self.assertEqual(set(data["unmapped"]), {"XYZ", "BAD"})
        self.assertIn("no CLI station", data["unmapped"]["XYZ"])

    def test_the_listing_is_cached_and_a_delisted_auto_city_is_dropped(self):
        self._write({"NYC": "CLINYC", "BNA": "CLIBNA"})
        _ok, _f, data = self._write({})                       # inside DISCOVER_SECS: no re-read
        self.assertIn("BNA", data["auto_stations"])
        self.assertEqual(self.resolved, ["CLIBNA"])           # resolved once, kept
        from unittest import mock
        with mock.patch.object(rf, "DISCOVER_SECS", 0):
            _ok, _f, data = self._write({"NYC": "CLINYC"})    # Kalshi dropped BNA
        self.assertNotIn("BNA", data["auto_stations"])
        self.assertEqual(data["unmapped"], {})

    def test_the_rules_name_the_station(self):
        pages = [{"markets": [
            {"ticker": "KXRAIN-26OCT10-TAM", "rules_primary":
             "If the total precipitation at CLITPA in Tampa in Oct 10, 2026 is strictly greater than 0"},
            {"ticker": "KXRAIN-26OCT10-ODD", "rules_primary": "no product here"},
            {"ticker": "KXRAINNYCM-26OCT-4", "rules_primary": "at CLINYC"}], "cursor": ""}]
        got = rf.listed_cities(lambda path, params: pages.pop(0))
        self.assertEqual(got, {"TAM": "CLITPA", "ODD": None})


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
