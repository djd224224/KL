"""Tests for rain_monthly_fair.py (monthly rain fair values for
incentive_mm's KXRAIN<CITY>M gate, 2026-10-01). No network: rain_monthly's
fetches are mocked."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import rain_monthly as rm
import rain_monthly_fair as rmf

NOW = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
RULES = ("If the total precipitation at CLIORD in Chicago in Oct 2026 is strictly "
         "greater than {k} inches, then the market resolves to Yes.")


def market(ev, k, code="ORD", strike_type="greater"):
    return {"event_ticker": ev, "ticker": f"{ev}-{k}", "floor_strike": k,
            "strike_type": strike_type,
            "rules_primary": RULES.format(k=k).replace("CLIORD", f"CLI{code}")}


class TestStations(unittest.TestCase):

    def test_station_from_the_rules(self):
        self.assertEqual(rmf.station_code(RULES.format(k=3)), "ORD")
        self.assertEqual(rmf.station_code("total precipitation at CLIAUS in Austin"), "AUS")
        self.assertIsNone(rmf.station_code("total precipitation in Chicago"))
        self.assertIsNone(rmf.station_code(""))

    def test_station_table(self):
        # O'Hare for October's KXRAINCHIM; the verified July stations stay
        self.assertEqual(rmf.CLI_STATIONS["ORD"]["icao"], "KORD")
        self.assertEqual(rmf.CLI_STATIONS["ORD"]["net"], "IL_ASOS")
        self.assertEqual(rmf.CLI_STATIONS["AUS"]["icao"], "KAUS")
        self.assertEqual(rmf.CLI_STATIONS["MDW"]["icao"], "KMDW")
        self.assertEqual(rmf.CLI_STATIONS["SPG"]["icao"], "KSPG")
        for spec in rmf.CLI_STATIONS.values():
            for k in ("icao", "iem", "net", "lat", "lon", "tz"):
                self.assertIn(k, spec)

    def test_events_from_the_signed_reader(self):
        books = {"KXRAINCHIM": [market("KXRAINCHIM-26OCT", 3), market("KXRAINCHIM-26OCT", 4),
                                market("KXRAINCHIM-26OCT", 5, strike_type="between")],
                 "KXRAINAUSM": [market("KXRAINAUSM-26OCT", 1, code="AUS"),
                                market("KXRAINAUSM-26OCT", 2, code="ATT")]}
        calls = []

        def reader(path, params):
            calls.append((path, params["series_ticker"], params["status"]))
            return {"markets": books.get(params["series_ticker"], [])}
        with mock.patch.object(rmf, "SERIES", ("KXRAINCHIM", "KXRAINAUSM")):
            ev = rmf.fetch_events(reader)
        self.assertEqual(ev["KXRAINCHIM-26OCT"],
                         {"series": "KXRAINCHIM", "code": "ORD",
                          "strikes": {"KXRAINCHIM-26OCT-3": 3.0, "KXRAINCHIM-26OCT-4": 4.0}})
        self.assertIsNone(ev["KXRAINAUSM-26OCT"]["code"])        # codes disagree
        self.assertEqual(calls, [("/markets", "KXRAINCHIM", "open"),
                                 ("/markets", "KXRAINAUSM", "open")])


class TestObservations(unittest.TestCase):
    SPEC = rmf.CLI_STATIONS["ORD"]

    def _obs(self, row=None, err=False):
        def fake(url, post_body=None, tries=3):
            if err:
                raise RuntimeError("down")
            return {"data": [row or {}]}
        with mock.patch.object(rm, "http_json", side_effect=fake):
            return rmf.obs_state(self.SPEC, NOW)

    def test_wet_dry_and_unknown(self):
        t = (NOW - timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        o = self._obs({"utc_valid": t, "phour": 0.13, "wxcodes": "RA BR"})
        self.assertEqual((o["wet"], o["phour"]), (True, 0.13))
        self.assertTrue(self._obs({"utc_valid": t, "phour": 0, "wxcodes": "-TSRA"})["wet"])
        self.assertTrue(self._obs({"utc_valid": t, "phour": None, "wxcodes": "VCSH"})["wet"])
        self.assertFalse(self._obs({"utc_valid": t, "phour": 0, "wxcodes": "BR"})["wet"])
        self.assertFalse(self._obs({"utc_valid": t, "phour": 0, "wxcodes": ""})["wet"])
        old = (NOW - timedelta(minutes=rmf.OBS_MAX_AGE_MIN + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(self._obs({"utc_valid": old, "phour": 0})["wet"])     # stale
        self.assertIsNone(self._obs({"phour": 0})["wet"])                       # no time
        self.assertIsNone(self._obs(err=True)["wet"])                           # feed down


class TestFair(unittest.TestCase):

    def test_prices_the_rungs_from_the_model(self):
        info = {"series": "KXRAINCHIM", "code": "ORD",
                "strikes": {"KXRAINCHIM-26OCT-1": 1.0, "KXRAINCHIM-26OCT-3": 3.0,
                            "KXRAINCHIM-26OCT-5": 5.0}}
        totals = sorted([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0])
        seen = {}

        def mtd(key, y, m, now):
            seen["mtd"] = (key, y, m)
            return {"mtd": 0.97, "cli_mtd": 0.41, "cli_date": "2026-10-01", "stale": False,
                    "pday": 0.53, "notes": []}

        def sim(key, y, m, now, n_samples=None, rng=None):
            seen["sim"] = (key, n_samples)
            return {"totals": totals, "horizon_days": 3, "n_segments": 46}
        with mock.patch.object(rm, "effective_mtd", side_effect=mtd), \
                mock.patch.object(rm, "simulate_remaining", side_effect=sim):
            f = rmf.event_fair("KXRAINCHIM-26OCT", info, NOW, n_samples=500)
        self.assertEqual(seen["mtd"], ("CLIORD", 2026, 10))
        self.assertEqual(seen["sim"], ("CLIORD", 500))
        self.assertEqual(rm.STATIONS["CLIORD"]["icao"], "KORD")
        want = rm.price_rungs(0.97, totals, [1.0, 3.0, 5.0])
        self.assertEqual(f["p"], {"KXRAINCHIM-26OCT-1": round(want[1.0], 4),
                                  "KXRAINCHIM-26OCT-3": round(want[3.0], 4),
                                  "KXRAINCHIM-26OCT-5": round(want[5.0], 4)})
        self.assertEqual(f["boundary"], ["KXRAINCHIM-26OCT-1"])   # 0.97 vs 1.0
        self.assertEqual((f["mtd"], f["stale_cli"], f["horizon_days"]), (0.97, False, 3))

    def test_unknown_station_or_month_is_no_fair(self):
        info = {"series": "KXRAINCHIM", "code": "XYZ", "strikes": {"KXRAINCHIM-26OCT-3": 3.0}}
        self.assertIsNone(rmf.event_fair("KXRAINCHIM-26OCT", info, NOW))
        info["code"] = "ORD"
        self.assertIsNone(rmf.event_fair("KXRAINCHIM-BAD", info, NOW))
        self.assertIsNone(rmf.event_fair("KXRAINCHIM-26OCT", dict(info, strikes={}), NOW))


class TestWriter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rmf_")
        self.path = os.path.join(self.tmp, "fair.json")
        self.events = {"KXRAINCHIM-26OCT": {"series": "KXRAINCHIM", "code": "ORD",
                                            "strikes": {"KXRAINCHIM-26OCT-3": 3.0,
                                                        "KXRAINCHIM-26OCT-4": 4.0}},
                       "KXRAINAUSM-26OCT": {"series": "KXRAINAUSM", "code": None,
                                            "strikes": {"KXRAINAUSM-26OCT-1": 1.0}}}

    @staticmethod
    def fair(ev, info, now):
        return {"p": {t: 0.5 for t in info["strikes"]}, "boundary": ["KXRAINCHIM-26OCT-3"],
                "mtd": 2.97, "cli_mtd": 2.9, "cli_date": "2026-10-09", "stale_cli": False,
                "pday": 0.07, "notes": [], "horizon_days": 7, "n_segments": 46}

    def _write(self, now, wet):
        obs = {"wet": wet, "obs_time": now.isoformat(), "phour": 0.1 if wet else 0.0,
               "wx": "RA" if wet else ""}
        return rmf.write_fair_file(self.path, now=now, events=self.events,
                                   fair_fn=self.fair, obs_fn=lambda spec, n: obs)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_markets_state_missing_and_the_wet_memory(self):
        self.assertEqual(self._write(NOW, True), (1, 1))
        d = self._read()
        self.assertEqual(d["missing"], ["KXRAINAUSM-26OCT"])          # no station
        self.assertEqual(d["event_state"]["KXRAINAUSM-26OCT"]["error"], "no station")
        m = d["markets"]["KXRAINCHIM-26OCT-3"]
        self.assertEqual((m["event"], m["p"], m["boundary"]), ("KXRAINCHIM-26OCT", 0.5, True))
        self.assertFalse(d["markets"]["KXRAINCHIM-26OCT-4"]["boundary"])
        st = d["event_state"]["KXRAINCHIM-26OCT"]
        self.assertEqual((st["wet"], st["mtd"], st["last_wet_at"]), (True, 2.97, NOW.isoformat()))
        # 40 minutes later it is dry: the last wet observation is kept for
        # the gate's drying window
        later = NOW + timedelta(minutes=40)
        self._write(later, False)
        st = self._read()["event_state"]["KXRAINCHIM-26OCT"]
        self.assertEqual((st["wet"], st["last_wet_at"]), (False, NOW.isoformat()))
        self.assertEqual(self._read()["model"]["boundary_in"], rmf.BOUNDARY_IN)

    def test_a_failing_fair_is_missing_not_fatal(self):
        def boom(ev, info, now):
            raise RuntimeError("acis down")
        n = rmf.write_fair_file(self.path, now=NOW, events=self.events, fair_fn=boom,
                                obs_fn=lambda s, n: {"wet": False, "obs_time": None,
                                                     "phour": 0, "wx": ""})
        self.assertEqual(n, (0, 2))
        self.assertEqual(self._read()["markets"], {})

    def test_events_cached_for_an_hour(self):
        calls = []

        def reader(path, params):
            calls.append(params["series_ticker"])
            return {"markets": [market("KXRAINCHIM-26OCT", 3)]
                    if params["series_ticker"] == "KXRAINCHIM" else []}
        kw = dict(fair_fn=self.fair, obs_fn=lambda s, n: {"wet": False, "obs_time": None,
                                                          "phour": 0, "wx": ""})
        with mock.patch.object(rmf, "SERIES", ("KXRAINCHIM",)):
            rmf.write_fair_file(self.path, now=NOW, get_json=reader, **kw)
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=30), get_json=reader, **kw)
            self.assertEqual(calls, ["KXRAINCHIM"])
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=61), get_json=reader, **kw)
            self.assertEqual(calls, ["KXRAINCHIM", "KXRAINCHIM"])


if __name__ == "__main__":
    unittest.main()
