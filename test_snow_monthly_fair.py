"""Tests for snow_monthly_fair.py (monthly snowfall fair values for
incentive_mm's KX<CITY>SNOWM gate, 2026-10-04 "build the snow feed"). No
network: the feeds are mocked."""

import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import rain_monthly as rm
import rain_monthly_fair as rmf
import snow_monthly_fair as smf

NOW = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 10, 18, 0, tzinfo=timezone.utc)       # 12:00 CST
RULES = ("If the total snowfall in {place} in {month} 2026 is strictly greater "
         "than {k} inches, then the market resolves to Yes.")


def market(ev, k, place="Chicago (O'Hare)", month="December"):
    return {"event_ticker": ev, "ticker": f"{ev}-{k}", "floor_strike": k,
            "strike_type": "greater",
            "rules_primary": RULES.format(place=place, month=month, k=k)}


class TestStations(unittest.TestCase):

    def test_station_from_the_rules_place_and_month(self):
        ev = "KXCHISNOWM-26DEC"
        self.assertEqual(smf.station_code(RULES.format(
            place="Chicago (O'Hare)", month="December", k=8), ev), "ORD")
        for place, code in (("Boston", "BOS"), ("Washington DC", "DCA"), ("Denver", "DEN"),
                            ("Detroit", "DTW"), ("Milwaukee", "MKE"),
                            ("Minneapolis", "MSP"), ("New York City", "NYC"),
                            ("Philadelphia", "PHL"), ("Pittsburgh", "PIT"),
                            ("Seattle", "SEA")):
            self.assertEqual(smf.station_code(RULES.format(
                place=place, month="December", k=8), ev), code, place)
        # a bare "Chicago" is two stations; an unknown city; another month
        for rules in (RULES.format(place="Chicago", month="December", k=8),
                      RULES.format(place="Buffalo", month="December", k=8),
                      RULES.format(place="Boston", month="November", k=8), ""):
            self.assertIsNone(smf.station_code(rules, ev), rules)
        self.assertIsNone(smf.station_code(RULES.format(
            place="Boston", month="December", k=8), "KXBOSSNOWM-X"))

    def test_station_table(self):
        for code in ("BOS", "ORD", "DCA", "DEN", "DTW", "MKE", "MSP", "NYC", "PHL", "PIT",
                     "SEA"):
            spec = smf.SNOW_STATIONS[code]
            self.assertEqual(spec["icao"], "K" + code)
            for k in ("iem", "net", "lat", "lon", "tz"):
                self.assertIn(k, spec)
        # Denver's airport snow record starts 2006: history from the
        # threaded Denver Area record, settlement still the airport's CLI
        self.assertEqual(smf.SNOW_STATIONS["DEN"]["acis"], "DENthr")
        self.assertNotIn("acis", smf.SNOW_STATIONS["ORD"])
        self.assertEqual(len(smf.SERIES), 10)

    def test_events(self):
        books = {"KXCHISNOWM": [market("KXCHISNOWM-26DEC", 2), market("KXCHISNOWM-26DEC", 4)],
                 "KXBOSSNOWM": [market("KXBOSSNOWM-26DEC", 2, place="Boston"),
                                market("KXBOSSNOWM-26DEC", 4, place="Chicago (O'Hare)")]}

        def reader(path, params):
            return {"markets": books.get(params["series_ticker"], [])}
        ev = smf.fetch_events(reader, series=("KXCHISNOWM", "KXBOSSNOWM"))
        self.assertEqual(ev["KXCHISNOWM-26DEC"],
                         {"series": "KXCHISNOWM", "code": "ORD",
                          "strikes": {"KXCHISNOWM-26DEC-2": 2.0, "KXCHISNOWM-26DEC-4": 4.0}})
        self.assertIsNone(ev["KXBOSSNOWM-26DEC"]["code"])          # disagree


def _cli(rows):
    return {"results": rows}


class TestToDate(unittest.TestCase):
    SPEC = smf.SNOW_STATIONS["ORD"]

    def _td(self, rows, now=DEC, last_snow_at=None):
        with mock.patch.object(rm, "cached_get_json", return_value=_cli(rows)):
            return smf.snow_to_date(self.SPEC, 2026, 12, now, last_snow_at)

    def test_reads_the_latest_report_and_its_issue_time(self):
        rows = [{"valid": "2026-11-30", "snow_month": 3.0, "snow": 0,
                 "product": "202612010635-KLOT-CDUS43-CLIORD"},
                {"valid": "2026-12-09", "snow_month": 4.1, "snow": "T",
                 "product": "202612100636-KLOT-CDUS43-CLIORD"}]
        td = self._td(rows)
        self.assertEqual((td["mtd"], td["cli_date"], td["stale"]), (4.1, "2026-12-09", False))
        self.assertEqual(td["cli_issued"], "2026-12-10T06:36:00+00:00")
        self.assertEqual(smf._issued("junk"), None)

    def test_snow_after_the_report_is_stale_until_a_later_one(self):
        rows = [{"valid": "2026-12-09", "snow_month": 4.1, "snow": 0,
                 "product": "202612100636-KLOT-CDUS43-CLIORD"}]
        td = self._td(rows, last_snow_at="2026-12-10T09:00:00+00:00")
        self.assertEqual((td["stale"], td["why"]),
                         (True, "snow since the last climate report"))
        # snow before the report was issued is in it
        self.assertFalse(self._td(rows, last_snow_at="2026-12-10T05:00:00+00:00")["stale"])
        # snow last month is not this month's business
        self.assertFalse(self._td(rows, last_snow_at="2026-11-20T05:00:00+00:00")["stale"])

    def test_no_report_yet_old_report_and_no_total(self):
        dec1 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
        td = self._td([], now=dec1)
        self.assertEqual((td["mtd"], td["stale"]), (0.0, False))
        td = self._td([], now=dec1, last_snow_at="2026-12-01T09:00:00+00:00")
        self.assertEqual((td["mtd"], td["stale"]), (0.0, True))
        old = [{"valid": "2026-12-07", "snow_month": 2.0, "snow": 0,
                "product": "202612080636-KLOT-CDUS43-CLIORD"}]
        self.assertTrue(self._td(old)["stale"])                      # 54h old
        none = [{"valid": "2026-12-09", "snow_month": "M", "snow": "M",
                 "product": "202612100636-KLOT-CDUS43-CLIORD"}]
        td = self._td(none)
        self.assertEqual((td["stale"], td["why"]),
                         (True, "the climate report has no month total"))


class TestObservations(unittest.TestCase):
    SPEC = smf.SNOW_STATIONS["MSP"]

    def _obs(self, row):
        with mock.patch.object(rm, "http_json", return_value={"data": [row]}):
            return smf.obs_state(self.SPEC, NOW)

    def test_snowing(self):
        t = (NOW - timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for wx in ("-SN", "SN BR", "BLSN", "SHSN", "PL", "-FZRA UP", "SG"):
            self.assertTrue(self._obs({"utc_valid": t, "wxcodes": wx, "tmpf": 30})["wet"], wx)
        # rain is not snow unless it falls at 35F or below
        self.assertFalse(self._obs({"utc_valid": t, "wxcodes": "-RA", "phour": 0.05,
                                    "tmpf": 41})["wet"])
        self.assertTrue(self._obs({"utc_valid": t, "wxcodes": "", "phour": 0.05,
                                   "tmpf": 34})["wet"])
        self.assertFalse(self._obs({"utc_valid": t, "wxcodes": "BR", "tmpf": 20})["wet"])
        old = (NOW - timedelta(minutes=smf.OBS_MAX_AGE_MIN + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(self._obs({"utc_valid": old, "wxcodes": "SN"})["wet"])


class TestFair(unittest.TestCase):
    INFO = {"series": "KXCHISNOWM", "code": "ORD",
            "strikes": {"KXCHISNOWM-26DEC-2": 2.0, "KXCHISNOWM-26DEC-4": 4.0}}

    def test_future_month_is_climatology_on_the_snow_record(self):
        seen = {}

        def hist(key, now, elem="pcpn"):
            seen["hist"] = (key, elem)
            return {"h": 1}

        def sim(key, s, e, now, n_samples=None, rng=None, hist=None, element=None,
                kernel_sigma=None):
            seen["sim"] = (key, s.isoformat(), e.isoformat(), hist, element, kernel_sigma)
            return {"totals": [0.0, 1.0, 3.0, 5.0], "horizon_days": 0, "n_segments": 46}
        with mock.patch.object(rmf, "period_history", side_effect=hist), \
                mock.patch.object(rmf, "simulate_period", side_effect=sim), \
                mock.patch.object(smf, "snow_to_date") as td:
            f = smf.event_fair("KXCHISNOWM-26DEC", self.INFO, NOW)
        td.assert_not_called()                                   # no reads in October
        self.assertEqual(seen["hist"], ("SNOWORD", "snow"))
        self.assertEqual(seen["sim"], ("SNOWORD", "2026-12-01", "2026-12-31", {"h": 1},
                                       "snowfallAmount", smf.KERNEL_SIGMA))
        self.assertEqual(rm.STATIONS["SNOWORD"]["icao"], "KORD")
        self.assertEqual(f["p"], {"KXCHISNOWM-26DEC-2": 0.5, "KXCHISNOWM-26DEC-4": 0.25})
        self.assertEqual((f["mtd"], f["stale_cli"], f["in_period"]), (0.0, False, False))

    def test_in_month_uses_the_report_and_its_staleness(self):
        def td(spec, y, m, now, last=None):
            return {"mtd": 1.95, "cli_date": "2026-12-09", "cli_issued": "x", "stale": True,
                    "why": "snow since the last climate report", "notes": []}
        with mock.patch.object(rmf, "period_history", return_value={}), \
                mock.patch.object(rmf, "simulate_period",
                                  return_value={"totals": [0.0, 1.0, 3.0],
                                                "horizon_days": 7, "n_segments": 46}), \
                mock.patch.object(smf, "snow_to_date", side_effect=td):
            f = smf.event_fair("KXCHISNOWM-26DEC", self.INFO, DEC)
        # 1.95 to date: T2 needs > 0.05 (2 of 3), T4 > 2.05 (1 of 3); T2 is
        # within the 0.1" boundary of the to-date
        self.assertEqual(f["p"], {"KXCHISNOWM-26DEC-2": round(2 / 3, 4),
                                  "KXCHISNOWM-26DEC-4": round(1 / 3, 4)})
        self.assertEqual(f["boundary"], ["KXCHISNOWM-26DEC-2"])
        self.assertEqual((f["stale_cli"], f["stale_why"], f["in_period"]),
                         (True, "snow since the last climate report", True))

    def test_no_station_no_month_or_month_over(self):
        self.assertIsNone(smf.event_fair("KXCHISNOWM-26DEC", dict(self.INFO, code=None), NOW))
        self.assertIsNone(smf.event_fair("KXCHISNOWM-BAD", self.INFO, NOW))
        self.assertIsNone(smf.event_fair("KXCHISNOWM-26SEP", self.INFO, NOW))

    def test_snowfall_history_and_kernel_through_the_rain_machinery(self):
        # two Decembers, 4" and 10", no forecast: raw atoms, then the kernel
        key = "SNOWORD"
        rm.STATIONS[key] = dict(smf.SNOW_STATIONS["ORD"], series="KXCHISNOWM")
        hist = {}
        for y, tot in ((2024, 4.0), (2025, 10.0)):
            for d in range(1, 32):
                hist[f"{y}-12-{d:02d}"] = tot / 31
        with mock.patch.object(rm, "fetch_forecast_days", return_value={}) as fc:
            sim = rmf.simulate_period(key, datetime(2026, 12, 1).date(),
                                      datetime(2026, 12, 31).date(), NOW, n_samples=300,
                                      rng=random.Random(1), hist=hist,
                                      element="snowfallAmount", kernel_sigma=0.0)
        fc.assert_called_once_with(key, NOW, element="snowfallAmount")
        self.assertEqual(sorted({round(t, 6) for t in sim["totals"]}), [4.0, 10.0])


class TestWriter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smf_")
        self.path = os.path.join(self.tmp, "snow.json")
        self.events = {"KXCHISNOWM-26DEC": {"series": "KXCHISNOWM", "code": "ORD",
                                            "strikes": {"KXCHISNOWM-26DEC-8": 8.0}},
                       "KXBOSSNOWM-26DEC": {"series": "KXBOSSNOWM", "code": None,
                                            "strikes": {"KXBOSSNOWM-26DEC-8": 8.0}}}
        self.infos = {}

    def fair(self, ev, info, now):
        self.infos[ev] = info
        return {"p": {t: 0.3 for t in info["strikes"]}, "boundary": [], "mtd": 0.0,
                "cli_mtd": None, "cli_date": None, "cli_issued": None,
                "stale_cli": False, "stale_why": None, "notes": [], "horizon_days": 0,
                "n_segments": 46, "in_period": now.month == 12}

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_october_reads_dry_december_reads_the_snow(self):
        calls = []

        def obs(spec, now):
            calls.append(now)
            return {"wet": True, "obs_time": now.isoformat(), "phour": 0.1, "wx": "SN"}
        self.assertEqual(smf.write_fair_file(self.path, now=NOW, events=self.events,
                                             fair_fn=self.fair, obs_fn=obs), (1, 1))
        d = self._read()
        st = d["event_state"]["KXCHISNOWM-26DEC"]
        self.assertEqual((st["wet"], st["last_wet_at"]), (False, None))
        self.assertEqual(calls, [])                               # no read in October
        self.assertEqual(d["event_state"]["KXBOSSNOWM-26DEC"]["error"], "no station")
        self.assertEqual(d["markets"]["KXCHISNOWM-26DEC-8"]["p"], 0.3)
        # in December it snows: wet, remembered, and handed to the fair
        smf.write_fair_file(self.path, now=DEC, events=self.events, fair_fn=self.fair,
                            obs_fn=obs)
        st = self._read()["event_state"]["KXCHISNOWM-26DEC"]
        self.assertEqual((st["wet"], st["last_wet_at"]), (True, DEC.isoformat()))
        self.assertEqual(self.infos["KXCHISNOWM-26DEC"]["last_wet_at"], DEC.isoformat())
        self.assertEqual(self._read()["model"]["kernel_sigma"], smf.KERNEL_SIGMA)

    def test_events_hourly_and_at_once_for_a_new_feed_series(self):
        calls = []

        def reader(path, params):
            calls.append(params["series_ticker"])
            return {"markets": [market("KXCHISNOWM-26DEC", 8)]
                    if params["series_ticker"] == "KXCHISNOWM" else []}
        kw = dict(fair_fn=self.fair, obs_fn=lambda s, n: {"wet": False, "obs_time": None,
                                                          "phour": 0, "wx": ""})
        with mock.patch.object(smf, "SERIES", ("KXCHISNOWM",)):
            smf.write_fair_file(self.path, now=NOW, get_json=reader, **kw)
            smf.write_fair_file(self.path, now=NOW + timedelta(minutes=30),
                                get_json=reader, **kw)
            self.assertEqual(calls, ["KXCHISNOWM"])
            smf.write_fair_file(self.path, now=NOW + timedelta(minutes=31), get_json=reader,
                                extra_series=("KXBUFSNOWM",), **kw)
            self.assertEqual(calls, ["KXCHISNOWM", "KXCHISNOWM", "KXBUFSNOWM"])
            self.assertEqual(self._read()["events_series"], ["KXCHISNOWM", "KXBUFSNOWM"])
            smf.write_fair_file(self.path, now=NOW + timedelta(minutes=62), get_json=reader,
                                extra_series=("KXBUFSNOWM",), **kw)
            self.assertEqual(len(calls), 3)                       # read at +31
            smf.write_fair_file(self.path, now=NOW + timedelta(minutes=92), get_json=reader,
                                extra_series=("KXBUFSNOWM",), **kw)
            self.assertEqual(len(calls), 5)                       # the hour


if __name__ == "__main__":
    unittest.main()
