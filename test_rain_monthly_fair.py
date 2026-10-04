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
        # KXRAINNYCM names the place, not a CLI product
        self.assertEqual(rmf.station_code(
            "If the total precipitation at Central Park, New York City in Oct 2026 "
            "is strictly greater than 2 inches"), "NYC")

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
        # every code the October rules name (2026-10-01, "add all the
        # cities"), plus Central Park for KXRAINNYCM
        for code in ("AUS", "ORD", "CLL", "CMH", "DFW", "DEN", "HOU", "LAX", "LEX",
                     "MIA", "MKE", "PVD", "SEA", "SFO", "SPG", "NYC"):
            self.assertIn(code, rmf.CLI_STATIONS, code)
            self.assertEqual(rmf.CLI_STATIONS[code]["icao"], "K" + code, code)
        self.assertEqual(rmf.CLI_STATIONS["LAX"]["tz"], "US/Pacific")
        self.assertEqual(rmf.CLI_STATIONS["CLL"]["net"], "TX_ASOS")
        self.assertEqual(len(rmf.SERIES), 16)

    def test_events_from_the_signed_reader(self):
        books = {"KXRAINCHIM": [market("KXRAINCHIM-26OCT", 3), market("KXRAINCHIM-26OCT", 4),
                                market("KXRAINCHIM-26OCT", 5, strike_type="between")],
                 "KXRAINAUSM": [market("KXRAINAUSM-26OCT", 1, code="AUS"),
                                market("KXRAINAUSM-26OCT", 2, code="ATT")]}
        calls = []

        def reader(path, params):
            calls.append((path, params["series_ticker"], params["status"]))
            return {"markets": books.get(params["series_ticker"], [])}
        with mock.patch.object(rmf, "SERIES", ("KXRAINCHIM", "KXRAINAUSM")), \
                mock.patch.object(rmf, "PERIOD_SERIES", ()):
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
        with mock.patch.object(rmf, "SERIES", ("KXRAINCHIM",)), \
                mock.patch.object(rmf, "PERIOD_SERIES", ()):
            rmf.write_fair_file(self.path, now=NOW, get_json=reader, **kw)
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=30), get_json=reader, **kw)
            self.assertEqual(calls, ["KXRAINCHIM"])
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=61), get_json=reader, **kw)
            self.assertEqual(calls, ["KXRAINCHIM", "KXRAINCHIM"])
            # a period series the bot's feed shows that the cached read
            # lacks: re-read at once, not at the hour
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=62),
                                get_json=reader, period_series=["KXRAINNAPAM"], **kw)
            self.assertEqual(calls, ["KXRAINCHIM", "KXRAINCHIM", "KXRAINCHIM",
                                     "KXRAINNAPAM"])
            self.assertEqual(self._read()["events_series"], ["KXRAINCHIM", "KXRAINNAPAM"])
            rmf.write_fair_file(self.path, now=NOW + timedelta(minutes=63),
                                get_json=reader, period_series=["KXRAINNAPAM"], **kw)
            self.assertEqual(len(calls), 4)

    def test_a_file_from_before_the_period_families_is_reread(self):
        # no events_series: the cache predates the period families, so the
        # first write re-reads Kalshi instead of waiting out the hour
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"events": self.events, "events_at": NOW.timestamp()}, f)
        calls = []

        def reader(path, params):
            calls.append(params["series_ticker"])
            return {"markets": []}
        with mock.patch.object(rmf, "SERIES", ("KXRAINCHIM",)), \
                mock.patch.object(rmf, "PERIOD_SERIES", ("KXRAINNAPAM",)):
            rmf.write_fair_file(self.path, now=NOW, get_json=reader, fair_fn=self.fair,
                                obs_fn=lambda s, n: {"wet": False, "obs_time": None,
                                                     "phour": 0, "wx": ""})
        self.assertEqual(calls, ["KXRAINCHIM", "KXRAINNAPAM"])


# ---------------------------------------------------------------------------
# PERIOD TOTALS (2026-10-04, "quote KXRAINNAPAM and similar families based on
# rain feed")
# ---------------------------------------------------------------------------
NAPA_RULES = ("If the total precipitation at KAPC in Napa in {when} is strictly "
              "greater than {k} inches, then the market resolves to Yes.")
NAPA_RULES2 = ("The total is the sum of The Weather Company's daily precipitation "
               "values at KAPC from {a} through {b}, inclusive, using its reporting "
               "days. If The Weather Company publishes a total for that exact "
               "period, that total takes precedence. Data: https://weather.com/kalshi.")


def napa_market(ev, k, a="November 1, 2026", b="March 31, 2027"):
    return {"event_ticker": ev, "ticker": f"{ev}-T{k}", "floor_strike": k,
            "strike_type": "greater",
            "rules_primary": NAPA_RULES.format(when="the period", k=k),
            "rules_secondary": NAPA_RULES2.format(a=a, b=b)}


class TestPeriodShape(unittest.TestCase):

    def test_station_codes_of_the_period_rules(self):
        self.assertEqual(rmf.station_code(NAPA_RULES.format(when="Dec 2026", k=8)), "APC")
        self.assertEqual(rmf.station_code(
            "If the total precipitation at NYC in New York City in September 28-October 4, "
            "2026 is strictly greater than 2 inches"), "NYC")
        self.assertEqual(rmf.station_code(
            "If the total precipitation at Boston Logan Airport (KBOS; BOS on the TWC daily "
            "dashboard) in Boston in September 26-27, 2026 is strictly greater than 0.5"), "BOS")
        self.assertEqual(rmf.station_code(
            "at New York City (KNYC; NYC on the TWC daily dashboard) in New York City"), "NYC")
        # the monthlies' CLI code still wins first
        self.assertEqual(rmf.station_code(RULES.format(k=3)), "ORD")
        spec = rmf.CLI_STATIONS["APC"]
        self.assertEqual((spec["icao"], spec["iem"], spec["net"], spec["tz"]),
                         ("KAPC", "APC", "CA_ASOS", "US/Pacific"))

    def test_period_from_the_ticker_day_first(self):
        d = datetime.fromisoformat
        self.assertEqual(rmf.parse_period("KXRAINNAPAM-01NOV26-31MAR27"),
                         (d("2026-11-01").date(), d("2027-03-31").date()))
        self.assertEqual(rmf.parse_period("KXRAINNYCW-28SEP26-04OCT26-T2"),
                         (d("2026-09-28").date(), d("2026-10-04").date()))
        self.assertEqual(rmf.parse_period("KXRAINNAPAM-01OCT26-31OCT26"),
                         (d("2026-10-01").date(), d("2026-10-31").date()))
        for ev in ("KXRAINCHIM-26OCT", "KXRAINWKND-26OCT10-NYC", "KXRAIN-26OCT05",
                   "KXRAINNAPAM-31MAR27-01NOV26",            # backwards
                   "KXRAINNAPAM-01JAN26-31DEC27",            # past 400 days
                   "KXRAINNAPAM-31FEB27-01MAR27", "KXRAINNAPAM-X"):
            self.assertIsNone(rmf.parse_period(ev), ev)
        self.assertEqual(rmf.rules_period(NAPA_RULES2.format(
            a="November 1, 2026", b="March 31, 2027")),
            (d("2026-11-01").date(), d("2027-03-31").date()))
        self.assertIsNone(rmf.rules_period("from somewhere through nowhere"))

    def test_events_carry_the_period_and_a_disagreeing_rule_has_no_station(self):
        books = {"KXRAINNAPAM": [
            napa_market("KXRAINNAPAM-01NOV26-31MAR27", 10),
            napa_market("KXRAINNAPAM-01NOV26-31MAR27", 15),
            napa_market("KXRAINNAPAM-01DEC26-31DEC26", 8, "December 2, 2026",
                        "December 31, 2026")]}

        def reader(path, params):
            return {"markets": books.get(params["series_ticker"], [])}
        with mock.patch.object(rmf, "SERIES", ()), \
                mock.patch.object(rmf, "PERIOD_SERIES", ("KXRAINNAPAM",)):
            ev = rmf.fetch_events(reader)
        self.assertEqual(ev["KXRAINNAPAM-01NOV26-31MAR27"],
                         {"series": "KXRAINNAPAM", "code": "APC",
                          "start": "2026-11-01", "end": "2027-03-31",
                          "strikes": {"KXRAINNAPAM-01NOV26-31MAR27-T10": 10.0,
                                      "KXRAINNAPAM-01NOV26-31MAR27-T15": 15.0}})
        self.assertIsNone(ev["KXRAINNAPAM-01DEC26-31DEC26"]["code"])   # Dec 2 != 01DEC26
        self.assertEqual(rmf.all_series(["KXRAINNAPAM", "KXRAINSONM"])[-2:],
                         ("KXRAINNYCW", "KXRAINSONM"))


def _hist(years, start_md=(11, 1), ndays=151, per_day=0.1, missing=()):
    """{iso: value} for whole seasons starting on start_md of each year."""
    out = {}
    for y in years:
        d0 = datetime(y, *start_md).date()
        for i in range(ndays):
            d = d0 + timedelta(days=i)
            out[d.isoformat()] = None if d.isoformat() in missing else per_day * (y - 1990)
    return out


class TestPeriodModel(unittest.TestCase):
    D = staticmethod(lambda s: datetime.fromisoformat(s).date())

    def test_segments_align_by_calendar_day_across_the_new_year(self):
        hist = _hist(range(2000, 2003), missing={"2001-12-25", "2001-12-26",
                                                 "2001-12-27", "2001-12-28"})
        days = [self.D("2026-11-01") + timedelta(days=i) for i in range(151)]
        segs = rmf.period_segments(hist, days)
        # windows k years back from Nov 2026: 2025 .. back to the record's
        # first year; only the 2000 and 2002 seasons are whole (2001 lost 4
        # days, over max(1, 151 // 50) = 3), and 2003+ is outside the record
        self.assertEqual(sorted(round(sum(s), 2) for s in segs),
                         [round(151 * 1.0, 2), round(151 * 1.2, 2)])
        self.assertTrue(all(len(s) == 151 for s in segs))
        self.assertEqual(rmf.period_segments(hist, []), [[]])
        self.assertEqual(rmf.period_segments({}, days), [])
        # a target Feb 29 reads the year's Feb 28
        leap = [self.D("2028-02-29")]
        self.assertEqual(rmf.period_segments({"2027-02-28": 0.4}, leap), [[0.4]])

    def _station(self):
        rm.STATIONS["TWCAPC"] = dict(rmf.CLI_STATIONS["APC"], series="KXRAINNAPAM")
        return "TWCAPC"

    def test_to_date_before_the_period_is_zero_with_no_reads(self):
        key = self._station()
        with mock.patch.object(rm, "fetch_iem_daily") as daily, \
                mock.patch.object(rm, "fetch_pday") as pday:
            td = rmf.period_to_date(key, self.D("2026-11-01"), self.D("2027-03-31"), NOW)
        self.assertEqual((td["obs"], td["in_period"], td["stale"]), (0.0, False, False))
        daily.assert_not_called()
        pday.assert_not_called()

    def test_to_date_in_the_period(self):
        key = self._station()
        now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)      # Oct 5, PDT
        iem = {"2026-10-01": 0.0, "2026-10-02": 0.31, "2026-10-04": 0.12}
        with mock.patch.object(rm, "fetch_iem_daily", return_value=iem) as daily, \
                mock.patch.object(rm, "fetch_pday", return_value=(0.05, "x")):
            # Oct 3 is unread at IEM; ACIS fills it
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                    hist={"2026-10-03": 0.2})
        daily.assert_called_once_with(key, 2026, 10)
        self.assertEqual((td["obs"], td["through"], td["missing"], td["stale"],
                          td["in_period"], td["pday"]),
                         (0.68, "2026-10-04", [], False, True, 0.05))
        # unread at both: one day is tolerated (counted 0) ...
        with mock.patch.object(rm, "fetch_iem_daily", return_value=iem), \
                mock.patch.object(rm, "fetch_pday", return_value=(0.0, "x")):
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now)
            self.assertEqual((td["obs"], td["missing"], td["stale"]),
                             (0.43, ["2026-10-03"], False))
            # ... unless it is the day the station was last seen wet (11:00
            # PDT Oct 3); a last wet reading on Oct 2 left Oct 3 dry
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                    last_wet_at="2026-10-03T18:00:00+00:00")
            self.assertTrue(td["stale"])
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                    last_wet_at="2026-10-02T18:00:00+00:00")
            self.assertFalse(td["stale"])
            # 03:00Z Oct 4 is still Oct 3 in Napa
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                    last_wet_at="2026-10-04T03:00:00Z")
            self.assertTrue(td["stale"])
        # two unread days are stale whatever the weather
        with mock.patch.object(rm, "fetch_iem_daily", return_value={"2026-10-01": 0.0}), \
                mock.patch.object(rm, "fetch_pday", return_value=(0.0, "x")):
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now)
        self.assertTrue(td["stale"])
        # wet today with no running total: the day's rain cannot be counted
        with mock.patch.object(rm, "fetch_iem_daily", return_value=dict(iem, **{"2026-10-03": 0})), \
                mock.patch.object(rm, "fetch_pday", return_value=(None, None)):
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                    last_wet_at="2026-10-05T19:00:00+00:00")
            self.assertTrue(td["stale"])
            td = rmf.period_to_date(key, self.D("2026-10-01"), self.D("2026-10-31"), now)
            self.assertFalse(td["stale"])
        # the period's first day: nothing past, today's total only
        with mock.patch.object(rm, "fetch_iem_daily") as daily, \
                mock.patch.object(rm, "fetch_pday", return_value=(0.2, "x")):
            td = rmf.period_to_date(key, self.D("2026-10-05"), self.D("2026-10-11"), now)
        daily.assert_not_called()
        self.assertEqual((td["obs"], td["through"]), (0.2, None))

    def test_simulation_without_a_forecast_is_the_smoothed_climatology(self):
        key = self._station()
        hist = {}
        for y, tot in ((2023, 10.0), (2024, 20.0)):
            d0, d1 = datetime(y, 11, 1).date(), datetime(y + 1, 3, 31).date()
            for i in range((d1 - d0).days + 1):    # 152 days across Feb 2024
                d = d0 + timedelta(days=i)
                # the target season has no Feb 29, so that day is never read
                hist[d.isoformat()] = 99.0 if (d.month, d.day) == (2, 29) else tot / 151
        with mock.patch.object(rm, "fetch_forecast_days", return_value={}), \
                mock.patch.object(rmf, "PERIOD_KERNEL_SIGMA", 0.0):
            sim = rmf.simulate_period(key, self.D("2026-11-01"), self.D("2027-03-31"), NOW,
                                      n_samples=400, rng=__import__("random").Random(1),
                                      hist=hist)
        self.assertEqual(sorted({round(t, 6) for t in sim["totals"]}), [10.0, 20.0])
        self.assertEqual((sim["n_segments"], sim["horizon_days"]), (2, 0))
        with mock.patch.object(rm, "fetch_forecast_days", return_value={}):
            sim = rmf.simulate_period(key, self.D("2026-11-01"), self.D("2027-03-31"), NOW,
                                      n_samples=20000, rng=__import__("random").Random(2),
                                      hist=hist)
        t = sim["totals"]
        self.assertAlmostEqual(sum(t) / len(t), 15.0, delta=0.2)     # mean-one kernel
        self.assertGreater(t[-1], 20.5)                              # smoothed tail
        self.assertLess(t[0], 9.5)
        with self.assertRaises(ValueError):
            rmf.simulate_period(key, self.D("2026-09-01"), self.D("2026-09-30"), NOW,
                                hist=hist)

    def test_simulation_injects_the_forecast_inside_the_period(self):
        key = self._station()
        now = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
        hist = {f"2025-10-{d:02d}": 0.0 for d in range(1, 32)}
        fc = {"2026-10-30": {"qpf_in": 1.0, "pop": 0.99, "complete": True},
              "2026-10-31": {"qpf_in": 2.0, "pop": 0.99, "complete": True}}
        with mock.patch.object(rm, "fetch_forecast_days", return_value=fc), \
                mock.patch.object(rm, "FORECAST_WEIGHT", 1.0):
            sim = rmf.simulate_period(key, self.D("2026-10-01"), self.D("2026-10-31"), now,
                                      n_samples=4000, rng=__import__("random").Random(3),
                                      hist=hist)
        self.assertEqual(sim["horizon_days"], 1)                     # Oct 31
        self.assertIsNotNone(sim["fc_today"])                        # Oct 30's rest
        self.assertAlmostEqual(sum(sim["totals"]) / 4000, 3.0, delta=0.3)

    def test_period_fair_end_to_end(self):
        info = {"series": "KXRAINNAPAM", "code": "APC", "start": "2026-10-01",
                "end": "2026-10-31", "last_wet_at": None,
                "strikes": {"KXRAINNAPAM-01OCT26-31OCT26-T1": 1.0,
                            "KXRAINNAPAM-01OCT26-31OCT26-T2": 2.0}}
        seen = {}

        def td(key, s, e, now, lw=None, hist=None):
            seen["td"] = (key, s.isoformat(), e.isoformat(), lw, hist)
            return {"obs": 0.97, "through": "2026-10-01", "stale": False, "missing": [],
                    "pday": 0.0, "notes": [], "in_period": True}

        def sim(key, s, e, now, n_samples=None, rng=None, hist=None):
            return {"totals": [0.0, 0.5, 1.5, 2.5], "horizon_days": 7, "n_segments": 28}
        with mock.patch.object(rmf, "period_history", return_value={"h": 1}), \
                mock.patch.object(rmf, "period_to_date", side_effect=td), \
                mock.patch.object(rmf, "simulate_period", side_effect=sim):
            f = rmf.event_fair("KXRAINNAPAM-01OCT26-31OCT26", info, NOW)
        self.assertEqual(seen["td"], ("TWCAPC", "2026-10-01", "2026-10-31", None, {"h": 1}))
        self.assertEqual(rm.STATIONS["TWCAPC"]["icao"], "KAPC")
        # 0.97 to date: T1 needs more than 0.03 (3 of 4 draws), T2 1.03 (2)
        self.assertEqual(f["p"], {"KXRAINNAPAM-01OCT26-31OCT26-T1": 0.75,
                                  "KXRAINNAPAM-01OCT26-31OCT26-T2": 0.5})
        self.assertEqual(f["boundary"], ["KXRAINNAPAM-01OCT26-31OCT26-T1"])
        self.assertEqual((f["mtd"], f["stale_cli"], f["in_period"], f["cli_date"]),
                         (0.97, False, True, "2026-10-01"))
        # an unknown station or a period already over: no fair
        self.assertIsNone(rmf.event_fair("E", dict(info, code="XYZ"), NOW))
        self.assertIsNone(rmf.event_fair("E", dict(info, end="2026-09-30"), NOW))


class TestPeriodWriter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rmf_")
        self.path = os.path.join(self.tmp, "fair.json")
        self.events = {
            "KXRAINNAPAM-01NOV26-31MAR27": {
                "series": "KXRAINNAPAM", "code": "APC", "start": "2026-11-01",
                "end": "2027-03-31", "strikes": {"KXRAINNAPAM-01NOV26-31MAR27-T20": 20.0}},
            "KXRAINNAPAM-01OCT26-31OCT26": {
                "series": "KXRAINNAPAM", "code": "APC", "start": "2026-10-01",
                "end": "2026-10-31", "strikes": {"KXRAINNAPAM-01OCT26-31OCT26-T1": 1.0}}}
        self.infos = {}

    def fair(self, ev, info, now):
        self.infos[ev] = info
        return {"p": {t: 0.2 for t in info["strikes"]}, "boundary": [], "mtd": 0.0,
                "cli_mtd": None, "cli_date": None, "stale_cli": False, "pday": None,
                "notes": [], "horizon_days": 0, "n_segments": 28,
                "in_period": ev.endswith("31OCT26"), "missing_days": []}

    def test_only_an_open_period_reads_the_weather(self):
        obs_calls = []

        def obs(spec, now):
            obs_calls.append(spec["iem"])
            return {"wet": True, "obs_time": now.isoformat(), "phour": 0.1, "wx": "RA"}
        rmf.write_fair_file(self.path, now=NOW, events=self.events, fair_fn=self.fair,
                            obs_fn=obs)
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        season = d["event_state"]["KXRAINNAPAM-01NOV26-31MAR27"]
        october = d["event_state"]["KXRAINNAPAM-01OCT26-31OCT26"]
        # raining in Napa on Oct 1: the season (from Nov 1) reads dry, the
        # October event is wet and remembers it
        self.assertEqual((season["wet"], season["last_wet_at"], season["in_period"],
                          season["start"]), (False, None, False, "2026-11-01"))
        self.assertEqual((october["wet"], october["last_wet_at"], october["in_period"]),
                         (True, NOW.isoformat(), True))
        self.assertEqual(obs_calls, ["APC"])
        # the fair sees the last wet observation (the to-date's stale rule)
        self.assertEqual(self.infos["KXRAINNAPAM-01OCT26-31OCT26"]["last_wet_at"],
                         NOW.isoformat())
        self.assertIsNone(self.infos["KXRAINNAPAM-01NOV26-31MAR27"]["last_wet_at"])
        self.assertEqual(d["markets"]["KXRAINNAPAM-01NOV26-31MAR27-T20"]["p"], 0.2)
        self.assertEqual(d["model"]["period_kernel_sigma"], rmf.PERIOD_KERNEL_SIGMA)


if __name__ == "__main__":
    unittest.main()
