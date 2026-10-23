#!/usr/bin/env python3
"""Unit tests for hight_fair.py (the IMM's high-temperature fair). No
network: Kalshi, the NWS and the station lookups are stubbed; the file goes
to a temp dir. Run: python -m unittest test_hight_fair
"""
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hight_fair as H  # noqa: E402
import lowt_fair as L  # noqa: E402

TZ = "America/New_York"


def _hours(day: date, temps: dict) -> list:
    out = []
    for h, t in sorted(temps.items()):
        st = datetime(day.year, day.month, day.day, h, tzinfo=timezone(timedelta(hours=-4)))
        out.append({"startTime": st.isoformat(), "temperature": t})
    return out


def _day(high: float = 64.0) -> dict:
    """A normal day: cool night, the high at 15:00."""
    t = {h: high - 10 for h in range(24)}
    t.update({0: high - 9, 6: high - 12, 12: high - 3, 14: high - 1, 15: high, 16: high - 1,
              20: high - 6, 23: high - 8})
    return t


def _event_markets(ev: str, lo: int = 59) -> list:
    ms = [{"ticker": f"{ev}-T{lo}", "event_ticker": ev, "strike_type": "less", "cap_strike": lo}]
    for i in range(4):
        f = lo + 2 * i
        ms.append({"ticker": f"{ev}-B{f}.5", "event_ticker": ev, "strike_type": "between",
                   "floor_strike": f, "cap_strike": f + 1})
    ms.append({"ticker": f"{ev}-T{lo + 7}", "event_ticker": ev, "strike_type": "greater",
               "floor_strike": lo + 7})
    return ms


class TestModel(unittest.TestCase):

    def test_profile_and_the_midnight_high_filter(self):
        d = date(2026, 10, 24)
        p = H.nws_profile(_hours(d, _day(64.0)), TZ, d)
        self.assertEqual((p["day_max"], p["max_hour"], p["t00"], p["n_hours"]), (64.0, 15, 55.0, 24))
        self.assertIsNone(H.shape_skip(p))
        front = {h: 60.0 - h * 0.5 for h in range(24)}          # warmest at midnight
        self.assertIn("midnight high", H.shape_skip(H.nws_profile(_hours(d, front), TZ, d)))

    def test_one_and_two_forecasts(self):
        ms = _event_markets("KXHIGHNY-26OCT24")
        prof = {"n_hours": 24, "t00": 55.0, "day_max": 64.0}
        one = H.event_fair("KXHIGHNY", 64.0, None, prof, ms)
        self.assertAlmostEqual(one["mu"], 64.0 + H.BIAS_NWS_F)
        self.assertAlmostEqual(one["b"], 1.80 * H.B_MULT, places=2)
        self.assertAlmostEqual(sum(one["markets"].values()), 1.0, places=3)
        two = H.event_fair("KXHIGHNY", 64.0, 66.0, prof, ms)
        self.assertAlmostEqual(two["mu"], 65.0 + H.BIAS_TWO_F)
        self.assertAlmostEqual(two["b"], one["b"] * H.TWO_SOURCE_MULT, places=2)
        new_city = H.event_fair("KXHIGHTZZZ", 64.0, None, prof, ms)
        self.assertAlmostEqual(new_city["b"], H.B_POOLED * H.B_MULT, places=2)

    def test_fails_closed(self):
        ms = _event_markets("KXHIGHNY-26OCT24")
        self.assertIn("no NWS", H.event_fair("KXHIGHNY", None, 66.0, {"n_hours": 0}, ms)["skip"])
        self.assertIn("covers only", H.event_fair("KXHIGHNY", 64.0, None, {"n_hours": 9}, ms)["skip"])
        e = H.event_fair("KXHIGHNY", 64.0, None, {"n_hours": 24, "t00": 62.0, "day_max": 64.0}, ms)
        self.assertIn("midnight high", e["skip"])
        self.assertEqual(e["markets"], {})


class TestWriter(unittest.TestCase):

    NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)

    def _kalshi(self, path, params, get_json=None):
        if path == "/series":
            return {"series": [{"ticker": "KXHIGHNY"}, {"ticker": "KXHIGHTEGLL"},
                               {"ticker": "KXLOWTNYC"}]}
        s = params["series_ticker"]
        if s == "KXHIGHTEGLL":
            return {"markets": [{"ticker": "KXHIGHTEGLL-26OCT24-T10",
                                 "rules_primary": "maximum temperature at London Heathrow (EGLL)"}]}
        out = []
        for d in ("26OCT23", "26OCT24"):
            for m in _event_markets(f"{s}-{d}"):
                out.append(dict(m, rules_primary="maximum temperature recorded at Central Park (CLINYC) ..."))
        return {"markets": out}

    def _nws(self, url, params=None, retries=3):
        return {"properties": {"periods": _hours(date(2026, 10, 24), _day(62.0))}}

    def test_next_day_highs_only(self):
        st = {"cli": "CLINYC", "lat": 40.78, "lon": -73.97, "tz": TZ}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(L, "_kalshi", self._kalshi), \
                mock.patch.object(L, "_get_json", self._nws), \
                mock.patch.object(L, "WU_API_KEY", ""), \
                mock.patch.object(L.rain_fair, "STATIONS", {}), \
                mock.patch.object(L.rain_fair, "resolve_station", lambda cli: dict(st)), \
                mock.patch.object(L.rain_fair, "resolve_hourly_url",
                                  lambda city, s=None: f"https://nws/{city[3:]}"), \
                mock.patch.object(L.time, "sleep", lambda s: None):
            path = os.path.join(d, "hight_fair.json")
            self.assertEqual(H.write_fair_file(path, now_utc=self.NOW), (1, 0, 0))
            with open(path, encoding="utf-8") as f:
                js = json.load(f)
        self.assertEqual(sorted(js["events"]), ["KXHIGHNY-26OCT24"])     # tomorrow only
        e = js["events"]["KXHIGHNY-26OCT24"]
        self.assertEqual((e["nws"], e["mu"]), (62.0, round(62.0 + H.BIAS_NWS_F, 2)))
        self.assertAlmostEqual(sum(e["markets"].values()), 1.0, places=3)
        self.assertNotIn("KXLOWTNYC", js["series"])                      # its own prefix
        self.assertIn("not a US station", js["unmapped"]["KXHIGHTEGLL"])
        self.assertEqual(js["model"]["bias_nws_f"], H.BIAS_NWS_F)


if __name__ == "__main__":
    unittest.main()
