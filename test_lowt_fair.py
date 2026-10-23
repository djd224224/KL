#!/usr/bin/env python3
"""Unit tests for lowt_fair.py (the IMM's low-temperature fair). No network:
Kalshi, the NWS and the station lookups are stubbed; the file goes to a temp
dir. Run: python -m unittest test_lowt_fair
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
import lowt_fair as L  # noqa: E402

TZ = "America/New_York"


def _hours(day: date, temps: dict, tz_offset_h: int = -4) -> list:
    """NWS hourly periods for `day` (local hour -> temp), EDT offsets."""
    out = []
    for h, t in sorted(temps.items()):
        st = datetime(day.year, day.month, day.day, h, tzinfo=timezone(timedelta(hours=tz_offset_h)))
        out.append({"startTime": st.isoformat(), "temperature": t})
    return out


def _profile_temps(low: float = 40.0) -> dict:
    """A clear-night day: the low at 06:00, warm afternoon, mild evening."""
    t = {h: low + 8 for h in range(24)}
    t.update({0: low + 6, 1: low + 5, 2: low + 4, 3: low + 3, 4: low + 2, 5: low + 1,
              6: low, 7: low + 1, 14: low + 18, 18: low + 12, 21: low + 9, 23: low + 7})
    return t


def _event_markets(ev: str, lo_band: int = 36) -> list:
    """Kalshi's KXLOWT shape: a cold tail, four 2F bands, a warm tail."""
    ms = [{"ticker": f"{ev}-T{lo_band}", "event_ticker": ev, "strike_type": "less",
           "cap_strike": lo_band}]
    for i in range(4):
        f = lo_band + 2 * i
        ms.append({"ticker": f"{ev}-B{f}.5", "event_ticker": ev, "strike_type": "between",
                   "floor_strike": f, "cap_strike": f + 1})
    ms.append({"ticker": f"{ev}-T{lo_band + 7}", "event_ticker": ev, "strike_type": "greater",
               "floor_strike": lo_band + 7})
    return ms


class TestModel(unittest.TestCase):

    def test_market_ranges(self):
        self.assertEqual(L.market_range({"strike_type": "between", "floor_strike": 38, "cap_strike": 39}),
                         (37.5, 39.5))
        self.assertEqual(L.market_range({"strike_type": "less", "cap_strike": 38}), (-math.inf, 37.5))
        self.assertEqual(L.market_range({"strike_type": "greater", "floor_strike": 45}), (45.5, math.inf))
        self.assertIsNone(L.market_range({"strike_type": "between", "floor_strike": 38}))

    def test_an_events_markets_cover_every_outcome_once(self):
        ms = _event_markets("KXLOWTEWR-26OCT24")
        e = L.event_fair("KXLOWTEWR", 38.0, 39.0, {"n_hours": 24}, ms)
        self.assertIsNone(e["skip"])
        self.assertAlmostEqual(e["mu"], 38.5 + L.BIAS_F)
        self.assertAlmostEqual(sum(e["markets"].values()), 1.0, places=3)
        # the Laplace is fat-tailed: more weight 4F out than a normal of the
        # same mean |err| would put there
        b = e["b"]
        self.assertAlmostEqual(b, L.B_POOLED * L.B_MULT, places=2)
        self.assertGreater(L.range_probability(44.5, math.inf, 40.0, b),
                           0.5 * math.erfc((4.5 / (b * math.sqrt(math.pi / 2))) / math.sqrt(2)))

    def test_one_source_widens_and_the_city_scale_applies(self):
        ms = _event_markets("KXLOWTHOU-26OCT24", 60)
        two = L.event_fair("KXLOWTHOU", 63.0, 63.0, {"n_hours": 24}, ms)
        one = L.event_fair("KXLOWTHOU", 63.0, None, {"n_hours": 24}, ms)
        self.assertAlmostEqual(two["b"], max(L.B_FLOOR, 0.90) * L.B_MULT, places=3)
        self.assertAlmostEqual(one["b"], two["b"] * L.ONE_SOURCE_MULT, places=3)

    def test_fails_closed(self):
        ms = _event_markets("KXLOWTEWR-26OCT24")
        self.assertIn("no NWS", L.event_fair("KXLOWTEWR", None, 39.0, {"n_hours": 0}, ms)["skip"])
        self.assertIn("covers only", L.event_fair("KXLOWTEWR", 38.0, None, {"n_hours": 12}, ms)["skip"])
        e = L.event_fair("KXLOWTEWR", 38.0, None,
                         {"n_hours": 24, "morning_min": 38, "late_min": 40, "t00": 46, "day_min": 38}, ms)
        self.assertIn("late-day drop", e["skip"])
        self.assertEqual(e["markets"], {})

    def test_shape_filters(self):
        d = date(2026, 10, 24)
        clear = L.nws_profile(_hours(d, _profile_temps(40.0)), TZ, d)
        self.assertEqual((clear["day_min"], clear["min_hour"], clear["n_hours"]), (40.0, 6, 24))
        self.assertIsNone(L.shape_skip(clear))
        # a front: the evening falls to within 4.5F of the morning low
        front = _profile_temps(40.0)
        front.update({21: 43.0, 22: 42.0, 23: 41.0})
        self.assertIn("late-day drop", L.shape_skip(L.nws_profile(_hours(d, front), TZ, d)))
        # warming overnight: the low prints at midnight
        warm = {h: 50.0 + h * 0.5 for h in range(24)}
        warm.update({18: 70.0, 21: 68.0, 23: 66.0})
        self.assertIn("midnight print", L.shape_skip(L.nws_profile(_hours(d, warm), TZ, d)))
        # hours of other days never count
        other = _hours(d + timedelta(days=1), {3: 10.0}) + _hours(d, _profile_temps(40.0))
        self.assertEqual(L.nws_profile(other, TZ, d)["day_min"], 40.0)

    def test_wu_reads_the_calendar_day_by_date(self):
        js = {"validTimeLocal": ["2026-10-23T07:00:00-0400", "2026-10-24T07:00:00-0400"],
              "calendarDayTemperatureMin": [44, 37], "temperatureMin": [36, 30]}
        self.assertEqual(L.wu_day_min(js, date(2026, 10, 24)), 37.0)
        self.assertIsNone(L.wu_day_min(js, date(2026, 10, 26)))
        self.assertIsNone(L.wu_day_min({}, date(2026, 10, 24)))

    def test_event_date(self):
        self.assertEqual(L.event_date("KXLOWTEWR-26OCT24"), date(2026, 10, 24))
        self.assertIsNone(L.event_date("KXLOWTEWR"))


class TestWriter(unittest.TestCase):
    """write_fair_file end to end on stubs: next-day events only, the
    shape filter, the international series unmapped, the file contract."""

    NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)      # 2 PM EDT

    def _kalshi(self, path, params, get_json=None):
        if path == "/series":
            return {"series": [{"ticker": "KXLOWTEWR"}, {"ticker": "KXLOWTPHIL"},
                               {"ticker": "KXLOWTEGLL"}, {"ticker": "KXHIGHNY"}]}
        s = params["series_ticker"]
        if s == "KXLOWTEGLL":
            return {"markets": [{"ticker": "KXLOWTEGLL-26OCT24-T10", "event_ticker": "KXLOWTEGLL-26OCT24",
                                 "rules_primary": "minimum temperature at London Heathrow (EGLL)"}]}
        cli = {"KXLOWTEWR": "CLIEWR", "KXLOWTPHIL": "CLIPHL"}[s]
        out = []
        for d in ("26OCT23", "26OCT24"):
            for m in _event_markets(f"{s}-{d}"):
                out.append(dict(m, rules_primary=f"minimum temperature recorded at X ({cli}) for ..."))
        return {"markets": out}

    def _nws(self, url, params=None, retries=3):
        day = date(2026, 10, 24)
        temps = _profile_temps(38.0)
        if "PHL" in url:                        # Philadelphia: a front tomorrow night
            temps.update({21: 40.0, 22: 39.5, 23: 39.0})
        return {"properties": {"periods": _hours(day, temps)}}

    def test_writes_next_day_events_only(self):
        st = {"CLIEWR": {"cli": "CLIEWR", "lat": 40.7, "lon": -74.2, "tz": TZ},
              "CLIPHL": {"cli": "CLIPHL", "lat": 39.9, "lon": -75.2, "tz": TZ}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(L, "_kalshi", self._kalshi), \
                mock.patch.object(L, "_get_json", self._nws), \
                mock.patch.object(L, "WU_API_KEY", ""), \
                mock.patch.object(L.rain_fair, "STATIONS", {}), \
                mock.patch.object(L.rain_fair, "resolve_station", lambda cli: dict(st[cli])), \
                mock.patch.object(L.rain_fair, "resolve_hourly_url",
                                  lambda city, s=None: f"https://nws/{city[3:]}"), \
                mock.patch.object(L.time, "sleep", lambda s: None):
            path = os.path.join(d, "lowt_fair.json")
            self.assertEqual(L.write_fair_file(path, now_utc=self.NOW), (1, 1, 0))
            with open(path, encoding="utf-8") as f:
                js = json.load(f)
        ev = js["events"]
        # today's events (Oct 23) are never priced: the IMM quotes the day before only
        self.assertEqual(sorted(ev), ["KXLOWTEWR-26OCT24", "KXLOWTPHIL-26OCT24"])
        e = ev["KXLOWTEWR-26OCT24"]
        self.assertIsNone(e["skip"])
        self.assertEqual((e["nws"], e["wu"], e["cli"], e["date"]), (38.0, None, "CLIEWR", "2026-10-24"))
        self.assertAlmostEqual(sum(e["markets"].values()), 1.0, places=3)
        self.assertIn("late-day drop", ev["KXLOWTPHIL-26OCT24"]["skip"])
        self.assertEqual(js["series"]["KXLOWTEGLL"], None)
        self.assertIn("not a US station", js["unmapped"]["KXLOWTEGLL"])
        self.assertNotIn("KXHIGHNY", js["series"])
        self.assertEqual(js["grid_urls"]["CLIEWR"], "https://nws/EWR")


if __name__ == "__main__":
    unittest.main()
