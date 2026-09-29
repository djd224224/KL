"""Run-slot / day-of cutoff rules in high_temp_trading.py (2026-09-29).

high_temp_trading.py is a top-level script that talks to Kalshi on import,
so this test lifts the RUN SLOT block out of the source and executes it
against a fake clock. What it pins down:
  - morning / evening runs trade every city, as before;
  - day-run orders expire at the city's own cutoff, never tomorrow;
  - a late run touches only the west cities, until 10:05 LOCAL, across DST;
  - the late run is a no-op when disabled, too late, or fired after 14:00 CT.

    python -m unittest test_high_temp_run_slot
"""
import os
import re
import unittest
from datetime import datetime as _real_datetime, timedelta

import pytz

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "high_temp_trading.py")
CT = pytz.timezone("US/Central")

WEST = {"Los Angeles", "San Francisco", "Seattle", "Las Vegas", "San Diego",
        "Phoenix", "Denver"}
EAST = {"New York City", "Philadelphia", "Miami", "Atlanta", "Washington DC",
        "Boston", "Newark", "Trenton", "Louisville"}


def _read_src():
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _block():
    src = _read_src()
    start = src.index("CT_TZ = pytz.timezone('US/Central')")
    end = src.index("ACTIVE_CITIES = [c for c in CITY_COORDS")
    end = src.index("\n", end) + 1
    return src[start:end]


def _city_coords():
    """City names in the first `cities` dict of the script (every city the bot knows)."""
    src = _read_src()
    m = re.search(r"^cities = \{\n(.*?)^\}", src, re.S | re.M)
    return re.findall(r'^\s+"([^"]+)":\s+\(', m.group(1), re.M)


def run_block(now_ct, env=None):
    """Execute the RUN SLOT block with the wall clock at `now_ct` (naive CT)."""
    now = CT.localize(now_ct)

    class FakeDT(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.astimezone(CT).replace(tzinfo=None)

    env = dict(env or {})
    g = {
        "pytz": pytz, "timedelta": timedelta, "datetime": FakeDT,
        "os": type("os", (), {"environ": env}),
        "central_time": now,
        "variable": 1 if now.hour >= 14 else 0,
        "CITY_COORDS": {c: (0.0, 0.0) for c in _city_coords()},
    }
    exec(compile(_block(), SRC, "exec"), g)
    return g


class RunSlotTest(unittest.TestCase):
    def test_knows_24_cities_including_the_four_new_ones(self):
        cities = _city_coords()
        self.assertEqual(len(cities), 24)
        for c in ("San Diego", "Louisville", "Trenton", "Newark"):
            self.assertIn(c, cities)

    def test_morning_runs_trade_everything_until_the_old_cutoffs(self):
        for hhmm in ((5, 2), (7, 2)):
            g = run_block(_real_datetime(2026, 9, 29, *hhmm))
            self.assertEqual(g["RUN_SLOT"], "morning")
            self.assertEqual(set(g["ACTIVE_CITIES"]), set(g["CITY_COORDS"]))
            for c in g["ACTIVE_CITIES"]:
                cut = g["_dayof_cutoff"](c).astimezone(CT)
                want = 9 if c in EAST else 10
                self.assertEqual((cut.date().isoformat(), cut.hour, cut.minute),
                                 ("2026-09-29", want, 5), c)

    def test_winter_first_morning_run_expires_today_not_tomorrow(self):
        # 10:02 UTC = 04:02 CST: the old code rolled these to tomorrow 01:59.
        g = run_block(_real_datetime(2026, 11, 10, 4, 2))
        self.assertEqual(g["RUN_SLOT"], "morning")
        cut = g["_dayof_cutoff"]("Chicago").astimezone(CT)
        self.assertEqual((cut.day, cut.hour, cut.minute), (10, 10, 5))

    def test_late_morning_run_skips_cities_near_their_cutoff(self):
        g = run_block(_real_datetime(2026, 9, 29, 8, 50))   # East cutoff 09:05 CT
        self.assertEqual(g["RUN_SLOT"], "morning")
        self.assertFalse(EAST & set(g["ACTIVE_CITIES"]))
        self.assertIn("Chicago", g["ACTIVE_CITIES"])

    def test_scheduled_late_run_summer(self):
        g = run_block(_real_datetime(2026, 9, 29, 9, 7), {"KXHIGH_RUN_SLOT": "west_late"})
        self.assertEqual(g["RUN_SLOT"], "west_late")
        self.assertEqual(set(g["ACTIVE_CITIES"]), WEST)
        ct = {c: g["_dayof_cutoff"](c).astimezone(CT).strftime("%H:%M") for c in WEST}
        self.assertEqual(ct["Los Angeles"], "12:05")    # 10:05 PDT
        self.assertEqual(ct["San Diego"], "12:05")
        self.assertEqual(ct["Phoenix"], "12:05")        # 10:05 MST = PDT in summer
        self.assertEqual(ct["Denver"], "11:05")         # 10:05 MDT

    def test_scheduled_late_run_winter(self):
        g = run_block(_real_datetime(2026, 11, 10, 8, 7), {"KXHIGH_RUN_SLOT": "west_late"})
        self.assertEqual(set(g["ACTIVE_CITIES"]), WEST)
        ct = {c: g["_dayof_cutoff"](c).astimezone(CT).strftime("%H:%M") for c in WEST}
        self.assertEqual(ct["Seattle"], "12:05")        # 10:05 PST
        self.assertEqual(ct["Phoenix"], "11:05")        # 10:05 MST
        self.assertEqual(ct["Denver"], "11:05")

    def test_unlabeled_run_after_9_ct_is_held_to_west_rules(self):
        g = run_block(_real_datetime(2026, 9, 29, 10, 6))
        self.assertEqual(g["RUN_SLOT"], "west_late")
        self.assertEqual(set(g["ACTIVE_CITIES"]), WEST)

    def test_drifted_late_run_drops_cities_inside_min_lead(self):
        g = run_block(_real_datetime(2026, 9, 29, 10, 50), {"KXHIGH_RUN_SLOT": "west_late"})
        self.assertNotIn("Denver", g["ACTIVE_CITIES"])  # 11:05 CT is 15 min away
        self.assertIn("Los Angeles", g["ACTIVE_CITIES"])
        g = run_block(_real_datetime(2026, 9, 29, 11, 50), {"KXHIGH_RUN_SLOT": "west_late"})
        self.assertEqual(g["ACTIVE_CITIES"], [])

    def test_late_run_after_2pm_is_a_noop_not_an_evening_run(self):
        g = run_block(_real_datetime(2026, 9, 29, 14, 30), {"KXHIGH_RUN_SLOT": "west_late"})
        self.assertEqual(g["RUN_SLOT"], "west_late")
        self.assertEqual(g["ACTIVE_CITIES"], [])

    def test_kill_switch(self):
        for when, env in (((9, 7), {"KXHIGH_RUN_SLOT": "west_late"}), ((10, 6), {})):
            env = dict(env, WEST_LATE_ENABLED="false")
            g = run_block(_real_datetime(2026, 9, 29, *when), env)
            self.assertEqual(g["ACTIVE_CITIES"], [], when)

    def test_evening_runs_trade_everything(self):
        for hhmm in ((20, 2), (23, 2)):
            g = run_block(_real_datetime(2026, 9, 29, *hhmm))
            self.assertEqual(g["RUN_SLOT"], "evening")
            self.assertEqual(set(g["ACTIVE_CITIES"]), set(g["CITY_COORDS"]))


if __name__ == "__main__":
    unittest.main()
