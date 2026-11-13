#!/usr/bin/env python3
"""Unit tests for lowt_period_fair.py (the IMM's KXLOWTPERIOD fair). No
network: Kalshi, IEM and the NWS are stubbed; the file goes to a temp dir.
Run: python -m unittest test_lowt_period_fair
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lowt_period_fair as P  # noqa: E402

EST = timezone(timedelta(hours=-5))
RULES = ("If the minimum temperature recorded at Providence (KPVD) for Nov 12 to 15, "
         "2026 (inclusive, America/New_York local calendar dates), is less than 34° "
         "Fahrenheit according to The Weather Company, then the market resolves to Yes.")
EV = "KXLOWTPERIOD-26NOV15PVD"


def _hours(start: datetime, temps: list) -> list:
    return [{"startTime": (start + timedelta(hours=i)).isoformat(), "temperature": t}
            for i, t in enumerate(temps)]


def _less(k: int) -> dict:
    return {"ticker": f"{EV}-T{k}", "event_ticker": EV, "strike_type": "less",
            "cap_strike": k, "rules_primary": RULES.replace("34", str(k))}


class TestRules(unittest.TestCase):
    def test_parse(self):
        i = P.parse_rules(RULES)
        self.assertEqual((i["icao"], i["start"], i["end"], i["tz"]),
                         ("KPVD", date(2026, 11, 12), date(2026, 11, 15), "America/New_York"))
        i = P.parse_rules("recorded at Newark (KEWR) for Nov 28 to Dec 2, 2026 (inclusive, "
                          "America/New_York local calendar dates)")
        self.assertEqual((i["start"], i["end"]), (date(2026, 11, 28), date(2026, 12, 2)))
        i = P.parse_rules("recorded at X (KBOS) for Dec 30 to Jan 2, 2027 (inclusive")
        self.assertEqual((i["start"], i["end"]), (date(2026, 12, 30), date(2027, 1, 2)))
        self.assertIsNone(P.parse_rules("the daily low at CLIBOS on Nov 14, 2026"))

    def test_whole_degrees_round_half_up(self):
        self.assertEqual([P.whole_f(v) for v in (38.0, 37.94, 38.5, 39.02, None, "M")],
                         [38, 38, 39, 39, None, None])

    def test_city_scale(self):
        self.assertEqual(P.city_scale("KXLOWTPERIOD-26NOV15BOS"), 1.20)      # KXLOWTBOS
        self.assertEqual(P.city_scale("KXLOWTPERIOD-26NOV15PVD"), P.LF.B_POOLED)


class TestShift(unittest.TestCase):
    obs_at = datetime(2026, 11, 13, 7, 54, tzinfo=EST)

    def test_a_level_miss_shifts_the_next_hours_and_fades(self):
        # NYC 11/13: 38F at 07:51 with the hourly at 44-45F all morning
        periods = _hours(datetime(2026, 11, 13, 7, tzinfo=EST), [45] + [44] * 70)
        days, shift, why = P.remaining_days(periods, "America/New_York", 38, self.obs_at,
                                            date(2026, 11, 12), date(2026, 11, 15), 1.43)
        self.assertIsNone(why)
        self.assertEqual(shift, -6.0)                    # vs the window's coldest, 44F
        today = days[0]
        self.assertEqual(today["day"], "2026-11-13")
        self.assertAlmostEqual(today["f"], 44 - 6 * (1 - (6 / 60) / P.SHIFT_DECAY_H), 2)
        self.assertEqual(days[1]["f"], 44.0)             # faded by tomorrow

    def test_an_early_front_is_timing_not_level(self):
        # BOS 11/13: 37F at 07:54 with the hourly at 43F to 08:00, 36F from 09:00
        periods = _hours(datetime(2026, 11, 13, 7, tzinfo=EST), [43, 43] + [36] * 70)
        days, shift, _ = P.remaining_days(periods, "America/New_York", 37, self.obs_at,
                                          date(2026, 11, 12), date(2026, 11, 15), 1.2)
        self.assertEqual(shift, 0.0)
        self.assertEqual(days[0]["f"], 36.0)

    def test_fails_closed_when_the_forecast_stops_short(self):
        periods = _hours(datetime(2026, 11, 13, 7, tzinfo=EST), [44] * 30)
        _d, _s, why = P.remaining_days(periods, "America/New_York", 40, self.obs_at,
                                       date(2026, 11, 12), date(2026, 11, 15), 1.43)
        self.assertIn("ends before", why)

    def test_lead_widens_the_scale(self):
        self.assertEqual(P.lead_factor(0), 0.75)
        self.assertAlmostEqual(P.lead_factor(10), 1.0)
        self.assertAlmostEqual(P.lead_factor(20), 1.25)                   # lowt_fair's B_MULT
        self.assertEqual(P.lead_factor(200), 2.0)


class TestProbability(unittest.TestCase):
    days = [{"day": "2026-11-14", "f": 36.0, "lead_h": 22, "b": 2.0, "mu": 36.3}]

    def test_less_strikes(self):
        obs = {"now_f": 41, "min_hi": 39, "min_lo": 39}
        p34 = P.market_p(_less(34), obs, self.days)
        p36 = P.market_p(_less(36), obs, self.days)
        p38 = P.market_p(_less(38), obs, self.days)
        self.assertTrue(0 < p34 < p36 < p38 < 1)
        self.assertAlmostEqual(p36, P.LF.laplace_cdf(35.5, 36.3, 2.0), 6)
        self.assertEqual(P.market_p(_less(40), obs, self.days), P.P_DECIDED)
        self.assertTrue(P.decided(_less(40), obs))
        self.assertFalse(P.decided(_less(39), obs))       # 39F is not under 39F

    def test_the_cli_alone_under_it_counts_half(self):
        obs = {"now_f": 45, "min_hi": 44, "min_lo": 42}   # KPVD 11/12: METAR 44, CLI 42
        p = P.market_p(_less(43), obs, [])
        self.assertAlmostEqual(p, 0.5)
        self.assertFalse(P.decided(_less(43), obs))

    def test_greater_and_band_strikes(self):
        obs = {"now_f": 41, "min_hi": 39, "min_lo": 39}
        gt = {"ticker": f"{EV}-T37", "strike_type": "greater", "floor_strike": 37}
        band = {"ticker": f"{EV}-B36.5", "strike_type": "between", "floor_strike": 36,
                "cap_strike": 37}
        lt36 = P.market_p(_less(36), obs, self.days)
        lt38 = P.market_p(_less(38), obs, self.days)
        self.assertAlmostEqual(P.market_p(gt, obs, self.days), 1 - lt38, 6)
        self.assertAlmostEqual(P.market_p(band, obs, self.days), lt38 - lt36, 6)
        low = {"now_f": 41, "min_hi": 35, "min_lo": 35}
        self.assertTrue(P.decided(gt, low))               # the low is under 37.5: NO
        self.assertTrue(P.decided(band, low))

    def test_near(self):
        obs = {"now_f": 39, "min_hi": 39, "min_lo": 39}
        self.assertTrue(P.near(_less(38), obs))           # two degrees from 37F
        self.assertFalse(P.near(_less(37), obs))          # three from 36F
        self.assertFalse(P.near(_less(40), obs))          # decided, not near
        self.assertTrue(P.near(_less(39), obs))           # one from 38F
        # the latest reading, not the low so far: 45F this afternoon
        self.assertFalse(P.near(_less(38), {"now_f": 45, "min_hi": 39, "min_lo": 39}))


class _Feeds:
    """Stub IEM / NWS / Kalshi reads for KPVD on 11/13 at 08:00 ET."""

    def __init__(self, now_f=39, obs_at="2026-11-13T12:51:00Z"):
        self.now_f, self.obs_at = now_f, obs_at
        self.urls = []

    def http(self, url):
        self.urls.append(url)
        if "api.weather.gov/stations/KPVD" in url:
            return {"properties": {"county": "https://api.weather.gov/zones/county/RIC003"}}
        if "currents.json" in url:
            return {"data": [{"utc_valid": self.obs_at, "tmpf": self.now_f, "min_tmpf": 39.0}]}
        if "daily.json" in url:
            return {"data": [{"date": "2026-11-11", "min_tmpf": 42.0},
                             {"date": "2026-11-12", "min_tmpf": 44.0},
                             {"date": "2026-11-13", "min_tmpf": 39.0}]}
        if "cli.py" in url:
            return {"results": [{"valid": "2026-11-12", "low": 42}]}
        if "gridpoints" in url:
            return {"properties": {"periods": _hours(
                datetime(2026, 11, 13, 8, tzinfo=EST), [42, 39] + [38] * 16 + [36] * 6
                + [44] * 12 + [34] * 12 + [45] * 12 + [31] * 6 + [40] * 20)}}
        raise AssertionError(url)

    def kalshi(self, path, params):
        return {"markets": [_less(k) for k in (30, 32, 34, 36, 38, 40)]}


class TestWrite(unittest.TestCase):
    now = datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc)

    def setUp(self):
        self._grid = P.rain_fair.resolve_hourly_url
        P.rain_fair.resolve_hourly_url = lambda city, st=None: "https://api.weather.gov/gridpoints/BOX/64,70/forecast/hourly"

    def tearDown(self):
        P.rain_fair.resolve_hourly_url = self._grid

    def _write(self, feeds):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lowt_period_fair.json")
        out = P.write_fair_file(path, get_json=feeds.kalshi, now_utc=self.now,
                                http_get=feeds.http)
        with open(path, encoding="utf-8") as f:
            return out, json.load(f)

    def test_end_to_end(self):
        (ok, skipped, failed), js = self._write(_Feeds())
        self.assertEqual((ok, skipped, failed), (1, 0, 0))
        e = js["events"][EV]
        self.assertEqual((e["station"], e["period"], e["date"]),
                         ("KPVD", ["2026-11-12", "2026-11-15"], "2026-11-15"))
        self.assertEqual(js["stations"]["KPVD"]["net"], "RI_ASOS")
        # the low so far: 39F on the METARs (today), the CLI's 42F for 11/12
        self.assertEqual((e["obs"]["min_hi"], e["obs"]["min_lo"]), (39, 39))
        self.assertEqual(e["obs"]["days"]["2026-11-12"], {"metar": 44, "cli": 42})
        self.assertEqual([d["day"] for d in e["days"]],
                         ["2026-11-13", "2026-11-14", "2026-11-15"])
        self.assertEqual(e["decided"], [f"{EV}-T40"])
        self.assertEqual(sorted(e["near"]), [f"{EV}-T38"])
        ps = [e["markets"][f"{EV}-T{k}"] for k in (30, 32, 34, 36, 38, 40)]
        self.assertEqual(ps, sorted(ps))
        self.assertGreater(ps[1], 0.3)                    # the 31F forecast on 11/15

    def test_a_stale_observation_fails_closed_and_keeps_the_old_entry(self):
        _o, js = self._write(_Feeds())
        d = tempfile.mkdtemp()
        path = os.path.join(d, "f.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(js, f)
        (ok, skipped, failed) = P.write_fair_file(
            path, get_json=_Feeds().kalshi, now_utc=self.now,
            http_get=_Feeds(obs_at="2026-11-13T10:00:00Z").http)
        self.assertEqual((ok, failed), (0, 1))
        with open(path, encoding="utf-8") as f:
            js2 = json.load(f)
        self.assertEqual(js2["events"][EV]["fetched_at"], js["events"][EV]["fetched_at"])
        self.assertIn("observation within", js2["errors"][0])

    def test_no_rules_no_fair(self):
        feeds = _Feeds()
        feeds.kalshi = lambda path, params: {"markets": [
            dict(_less(34), rules_primary="something else")]}
        (ok, skipped, failed), js = self._write(feeds)
        self.assertEqual((ok, skipped), (0, 1))
        self.assertEqual(js["events"][EV]["skip"], "no station or period in the rules")


if __name__ == "__main__":
    unittest.main()
