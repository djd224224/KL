"""Night fresh quotes in high_temp_trading.py (Jack 2026-11-01: "1+2", then
"just switch overnight to the new strategy").

high_temp_trading.py talks to Kalshi on import, so -- like
test_high_temp_sizing.py -- this lifts the pilot's block out of the source and
executes it. What it pins down:
  - evening runs at 20 and 23 CT are base runs (both arms); any other evening
    hour is an extra run that quotes the fresh arm only;
  - every evening market is fresh by default; NIGHT_PILOT_PROPORTION < 1
    brings back a sticky control arm;
  - a fresh market's evening order expires 60 minutes after placement (or at
    its usual 01:59 expiry if sooner); control keeps the usual expiry;
  - with NIGHT_PILOT_ENABLED=false every market is "off", and an off-hour
    evening run exits without touching anything.

    python -m unittest test_high_temp_night
"""
import hashlib
import os
import unittest
from datetime import datetime
from unittest import mock

import pytz

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "high_temp_trading.py")
NOW = 1793600000                  # any epoch second
TICKERS = [f"KXHIGHNY-26NOV{d:02d}-B{t}.5" for d in range(1, 29) for t in (50, 52, 54, 56)]


class _Exit(Exception):
    pass


class _Sys:
    @staticmethod
    def exit(code=0):
        raise _Exit(code)


def night(hour, slot="evening", env=None):
    """Execute the pilot's block for a run at `hour` CT in `slot` under `env`;
    returns (globals, write_run_row calls); a no-op exit raises _Exit."""
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    start = src.index("NIGHT_PILOT_NAME = ")
    end = src.index("    sys.exit(0)\n", start) + len("    sys.exit(0)\n")
    calls = []
    g = {"os": os, "hashlib": hashlib, "RUN_SLOT": slot, "variable": 1 if slot == "evening" else 0,
         "central_time": pytz.timezone("US/Central").localize(datetime(2026, 11, 2, hour, 2)),
         "write_run_row": lambda *a, **k: calls.append((a, k)), "datetime": datetime,
         "pytz": pytz, "sys": _Sys}
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("NIGHT_", "EVENING_BASE_HOURS"))}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True):
        exec(src[start:end], g)
    return g, calls


class TestNightPilot(unittest.TestCase):

    def test_base_and_extra_hours(self):
        for hour in (20, 23):
            self.assertFalse(night(hour)[0]["EXTRA_NIGHT_RUN"], hour)
        for hour in (19, 21, 22):
            self.assertTrue(night(hour)[0]["EXTRA_NIGHT_RUN"], hour)
        g, _ = night(5, slot="morning")
        self.assertFalse(g["EXTRA_NIGHT_RUN"])
        self.assertEqual({g["night_arm"](t) for t in TICKERS}, {"off"})
        g, _ = night(22, env={"EVENING_BASE_HOURS_CT": "20,22,23"})
        self.assertFalse(g["EXTRA_NIGHT_RUN"])

    def test_every_evening_market_is_fresh_by_default(self):
        for hour in (19, 20, 21, 22, 23):
            g, _ = night(hour)
            self.assertEqual({g["night_arm"](t) for t in TICKERS}, {"fresh"}, hour)
            self.assertEqual(g["night_expiration"](NOW + 5 * 3600, TICKERS[0], NOW), NOW + 3600)

    def test_a_proportion_brings_back_a_sticky_control(self):
        half = {"NIGHT_PILOT_PROPORTION": "0.5"}
        g, _ = night(20, env=half)
        arms = {t: g["night_arm"](t) for t in TICKERS}
        fresh = sum(a == "fresh" for a in arms.values())
        self.assertEqual(set(arms.values()), {"fresh", "control"})
        self.assertTrue(0.35 < fresh / len(arms) < 0.65, fresh)
        # the same arm on every run of the night, base or extra
        self.assertEqual(arms, {t: night(22, env=half)[0]["night_arm"](t) for t in TICKERS})
        g, _ = night(20, env={"NIGHT_PILOT_PROPORTION": "0"})
        self.assertEqual({g["night_arm"](t) for t in TICKERS}, {"control"})

    def test_fresh_orders_expire_after_60_minutes(self):
        g, _ = night(21, env={"NIGHT_PILOT_PROPORTION": "0.5"})
        usual = NOW + 5 * 3600
        for t in TICKERS[:40]:
            want = NOW + 3600 if g["night_arm"](t) == "fresh" else usual
            self.assertEqual(g["night_expiration"](usual, t, NOW), want, t)
            # the usual expiry wins when it comes first
            self.assertEqual(g["night_expiration"](NOW + 600, t, NOW), NOW + 600)
        g, _ = night(21, env={"NIGHT_ORDER_TTL_MIN": "45"})
        fresh = next(t for t in TICKERS if g["night_arm"](t) == "fresh")
        self.assertEqual(g["night_expiration"](usual, fresh, NOW), NOW + 2700)

    def test_switched_off(self):
        off = {"NIGHT_PILOT_ENABLED": "false"}
        g, calls = night(20, env=off)                   # a base hour runs as before
        self.assertEqual({g["night_arm"](t) for t in TICKERS}, {"off"})
        self.assertEqual(g["night_expiration"](NOW + 9999, TICKERS[0], NOW), NOW + 9999)
        self.assertEqual(calls, [])
        with self.assertRaises(_Exit):                  # an off hour does nothing
            night(21, env=off)

    def test_off_hour_noop_writes_both_run_rows(self):
        # night() loses the calls when the block exits, so run it by hand here
        with open(SRC, encoding="utf-8") as f:
            src = f.read()
        start = src.index("NIGHT_PILOT_NAME = ")
        end = src.index("    sys.exit(0)\n", start) + len("    sys.exit(0)\n")
        rows = []
        g = {"os": os, "hashlib": hashlib, "RUN_SLOT": "evening", "variable": 1,
             "central_time": pytz.timezone("US/Central").localize(datetime(2026, 11, 2, 22, 2)),
             "write_run_row": lambda *a, **k: rows.append((a, k)), "datetime": datetime,
             "pytz": pytz, "sys": _Sys}
        with mock.patch.dict(os.environ, {"NIGHT_PILOT_ENABLED": "false"}):
            with self.assertRaises(_Exit):
                exec(src[start:end], g)
        self.assertEqual([a[0] for a, _k in rows], ["start", "end"])
        self.assertEqual(rows[1][1]["exit_status"], "noop")
        self.assertEqual(rows[0][1]["central_time_hour"], 22)


if __name__ == "__main__":
    unittest.main()
