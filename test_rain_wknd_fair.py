"""Tests for rain_wknd_fair (the KXRAINWKND fair-value writer, 2026-10-11)."""
import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import rain_wknd_fair as r

UTC = timezone.utc
SAT = date(2026, 10, 17)
RULES = ("If the total precipitation at CLI{code} in Somewhere on any day within "
         "October 17, 2026 through October 18, 2026 is strictly greater than 0 "
         "inches, then the market resolves to Yes.")


def periods(run: datetime, ps):
    """12-hour periods ending at 00Z/12Z after `run`, p12 in 0..1."""
    return [(run + timedelta(hours=12 * (i + 1)), p) for i, p in enumerate(ps)]


_NET = []
REAL_FETCH_NBS_HOURLY = r.fetch_nbs_hourly      # the reader itself, for its tests


def setUpModule():
    """No test reaches the network for the composite's short-range sources:
    a test that wants them passes nbs_fn / nbs_hourly_fn itself. Nor for
    Kalshi: a get_json fake that refuses a read would fall back to a public
    one."""
    for name in ("fetch_nbs", "fetch_nbs_hourly"):
        p = mock.patch.object(r, name, side_effect=RuntimeError("no network in tests"))
        p.start()
        _NET.append(p)
    p = mock.patch.object(r.requests, "get", side_effect=RuntimeError("no network in tests"))
    p.start()
    _NET.append(p)
    # ...nor for ACIS (LONG LEADS' climatology): a test passes clim_fn
    p = mock.patch.object(r.requests, "post", side_effect=RuntimeError("no network in tests"))
    p.start()
    _NET.append(p)


def tearDownModule():
    for p in _NET:
        p.stop()


# KATL's block of NOAA's 10/29 13Z NBS bulletin (blend_nbstx.t13z), rows the
# parser reads plus two it skips
KATL_13Z = """\
 KATL    NBM V5.0 NBS GUIDANCE    10/29/2026  1300 UTC
 DT /OCT  29/OCT  30                /OCT  31                /NOV   1
 UTC  18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21 00 03 06 09 12
 FHR  05 08 11 14 17 20 23 26 29 32 35 38 41 44 47 50 53 56 59 62 65 68 71
 TMP  74 76 66 61 58 56 55 68 77 78 68 63 62 61 61 66 71 71 66 63 61 58 57
 P06         0     0     0     0     0     0     0     0     2     9    14
 P12                     0           0           0           2          16
 SLV  78 81 82 83 83 82 83 84 84 84 85 85 86 86 87 92 88 91 96 99 97100100
"""

# KNYC's block of NOAA's 11/22 23Z bulletin: since the 11/03 12Z cycle the
# header carries no version ("NBM  NBS GUIDANCE", once "NBM V5.0 NBS GUIDANCE")
KNYC_23Z = """\
 KNYC    NBM  NBS GUIDANCE    11/22/2026  2300 UTC
 DT /NOV  23               /NOV  24                /NOV  25
 UTC  03 06 09 12 15 18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21
 FHR  04 07 10 13 16 19 22 25 28 31 34 37 40 43 46 49 52 55 58 61 64 67 70
 TMP  59 58 57 56 62 67 63 56 51 48 45 42 46 48 47 43 40 38 36 35 41 46 46
 P06     34     6     5    11     2     0     0     0     0     0     0
 P12           37           5           2           0           0
 SLV  95 84 76 80 72 79 82 66 47 25 19 12  7  7  8  2  0  0  0  0  0  2 11
"""


class TestParsing(unittest.TestCase):
    def test_station_and_window_from_the_rules(self):
        self.assertEqual(r.station_code(RULES.format(code="DJT")), "DJT")
        self.assertEqual(r.station_code("... precipitation at CLL in College Station on"),
                         "CLL")
        self.assertIsNone(r.station_code("no station named here"))
        self.assertEqual(r.rules_span(RULES.format(code="NYC")),
                         (date(2026, 10, 17), date(2026, 10, 18)))
        self.assertIsNone(r.rules_span("within soon through later"))

    def test_a_one_day_markets_station_day_and_window(self):
        # Jack 2026-10-31: "KXRAINELECTION should be quoted", "in same way as
        # weekend rain family"
        rules = ("If the total precipitation at {name} (K{code}) in {city} in "
                 "November 3, 2026 is strictly greater than 0 inches, then the "
                 "market resolves to Yes.")
        sd = rules.format(name="San Diego", code="SAN", city="San Diego")
        self.assertEqual(r.station_code(sd), "SAN")
        self.assertEqual(r.station_code(rules.format(name="Houston (Hobby)", code="HOU",
                                                     city="Houston")), "HOU")
        self.assertEqual(r.rules_span(sd), (date(2026, 11, 3), date(2026, 11, 3)))
        # the ticker's day comes first
        self.assertEqual(r.event_day("KXRAINELECTION-03NOV26"), date(2026, 11, 3))
        self.assertIsNone(r.event_day("KXRAINELECTION-3NOV26"))
        self.assertIsNone(r.event_day("KXRAINELECTION-03XYZ26"))
        # one climate day, local standard time
        self.assertEqual(r.window(date(2026, 11, 3), 5, 1),
                         (datetime(2026, 11, 3, 5, tzinfo=UTC),
                          datetime(2026, 11, 4, 5, tzinfo=UTC)))

    def test_event_saturday(self):
        self.assertEqual(r.event_saturday("KXRAINWKND-26OCT17"), SAT)
        self.assertIsNone(r.event_saturday("KXRAINWKND-26OCT18"))     # a Sunday
        self.assertIsNone(r.event_saturday("KXRAINWKND-26XYZ17"))
        self.assertIsNone(r.event_saturday("KXRAINWKND"))

    def test_window_is_local_standard_time(self):
        # New York: 00:00 EST = 05:00Z all year (the climate day ignores DST)
        self.assertEqual(r.window(SAT, 5), (datetime(2026, 10, 17, 5, tzinfo=UTC),
                                            datetime(2026, 10, 19, 5, tzinfo=UTC)))
        self.assertEqual(r.window(SAT, 8)[0], datetime(2026, 10, 17, 8, tzinfo=UTC))

    def test_parse_nbe_keeps_the_latest_run(self):
        js = {"data": [
            {"runtime": "2026-10-11 00:00", "ftime": "2026-10-11 12:00", "p12": 90},
            {"runtime": "2026-10-11 12:00", "ftime": "2026-10-12 12:00", "p12": 40},
            {"runtime": "2026-10-11 12:00", "ftime": "2026-10-12 00:00", "p12": 20},
            {"runtime": "2026-10-11 12:00", "ftime": "2026-10-13 00:00", "p12": None}]}
        rt, per = r.parse_nbe(js)
        self.assertEqual(rt, datetime(2026, 10, 11, 12, tzinfo=UTC))
        self.assertEqual(per, [(datetime(2026, 10, 12, 0, tzinfo=UTC), 0.2),
                               (datetime(2026, 10, 12, 12, tzinfo=UTC), 0.4)])
        self.assertEqual(r.parse_nbe({}), (None, []))


    def test_codes_outside_the_lower_48_read_as_written(self):
        # Jack 2026-11-22: "Add" (Honolulu and Anchorage on Thanksgiving)
        one = ("If the total precipitation at {name} ({code}) in {name} on November 26, "
               "2026 is strictly greater than 0 inches, then the market resolves to Yes.")
        for code in ("PHNL", "PANC", "PAFA", "TJSJ", "TIST"):
            self.assertEqual(r.station_code(one.format(name="X", code=code)), code)
        # the lower 48 as before, and a K code wins over any other parenthesis
        self.assertEqual(r.station_code(one.format(name="New York City", code="KNYC")), "NYC")
        self.assertEqual(r.station_code("per the Weather Company (PTWC) at Central Park "
                                        "(KNYC) on"), "NYC")
        # not a US station code: nothing read
        for code in ("EGLL", "CYYZ", "MMMX", "PHN", "TXYZ"):
            self.assertIsNone(r.station_code(one.format(name="X", code=code)), code)


class TestModel(unittest.TestCase):
    RUN = datetime(2026, 10, 15, 0, tzinfo=UTC)

    def test_weekend_p_weights_the_overlap(self):
        ws, we = r.window(SAT, 5)                     # 10/17 05Z .. 10/19 05Z
        ps = [0.0] * 4 + [0.2, 0.3, 0.1, 0.4, 0.5] + [0.9] * 3
        per = periods(self.RUN, ps)                   # ends 10/15 12Z .. 10/21 00Z
        p_raw, cov = r.weekend_p(per, ws, we)
        self.assertAlmostEqual(cov, 4.0)
        # inside: 10/17 12Z (7/12), 10/18 00Z, 10/18 12Z, 10/19 00Z (1 each),
        # 10/19 12Z (5/12)
        L = (7 / 12 * math.log(0.8) + math.log(0.7) + math.log(0.9)
             + math.log(0.6) + 5 / 12 * math.log(0.5))
        self.assertAlmostEqual(p_raw, max(0.4, 1 - math.exp(r.W * L)), places=9)
        # a period set short of the weekend prices nothing
        self.assertIsNone(r.weekend_p(per[:6], ws, we))

    def test_one_wet_half_day_sets_a_floor(self):
        ws, we = r.window(SAT, 5)
        per = periods(self.RUN, [0.0] * 5 + [0.8, 0.0, 0.0, 0.0] + [0.0] * 3)
        self.assertAlmostEqual(r.weekend_p(per, ws, we)[0], 0.8)

    def test_market_fair_recalibrates_by_lead_and_fails_closed(self):
        per = periods(self.RUN, [0.3] * 12)
        f = r.market_fair(per, self.RUN, SAT, 5)
        lead = (datetime(2026, 10, 17, 5, tzinfo=UTC) - self.RUN).total_seconds() / 3600 - 3
        self.assertEqual(f["lead_h"], lead)                       # 50h: (0.17, 1.12)
        z = 0.17 + 1.12 * math.log(f["p_raw"] / (1 - f["p_raw"]))
        self.assertAlmostEqual(f["p"], 1 / (1 + math.exp(-z)), places=3)
        # the weekend has started (lead < 0) / the run is past the fit
        self.assertIsNone(r.market_fair(per, datetime(2026, 10, 17, 3, tzinfo=UTC), SAT, 5))
        old = self.RUN - timedelta(hours=r.MAX_LEAD_HOURS)
        self.assertIsNone(r.market_fair(periods(old, [0.3] * 30), old, SAT, 5))
        # monotone in the PoPs, inside [0.01, 0.99]
        lo = r.market_fair(periods(self.RUN, [0.0] * 12), self.RUN, SAT, 5)["p"]
        hi = r.market_fair(periods(self.RUN, [0.99] * 12), self.RUN, SAT, 5)["p"]
        self.assertEqual((lo, hi), (0.01, 0.99))
        self.assertLess(f["p"], r.market_fair(periods(self.RUN, [0.4] * 12),
                                              self.RUN, SAT, 5)["p"])


class TestWrite(unittest.TestCase):
    NOW = datetime(2026, 10, 15, 6, tzinfo=UTC)
    RUN = datetime(2026, 10, 15, 0, tzinfo=UTC)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rain_wknd_fair.json")
        span = ["2026-10-17", "2026-10-18"]
        self.events = {
            "KXRAINWKND-26OCT17": {"sat": "2026-10-17", "markets": {
                "KXRAINWKND-26OCT17-NYC": {"code": "NYC", "span": span},
                "KXRAINWKND-26OCT17-WPB": {"code": "DJT", "span": span},
                "KXRAINWKND-26OCT17-XXX": {"code": "XYZ", "span": span},
                "KXRAINWKND-26OCT17-BAD": {"code": "BOS",
                                           "span": ["2026-10-17", "2026-10-19"]}}},
            "KXRAINWKND-26OCT10": {"sat": "2026-10-10", "markets": {
                "KXRAINWKND-26OCT10-NYC": {"code": "NYC", "span": None}}}}
        self.calls = []
        # XYZ stays unmapped here, and no test reaches the network to map it
        p = mock.patch.object(r, "resolve_station",
                              side_effect=RuntimeError("KXYZ: not a station (test)"))
        p.start()
        self.addCleanup(p.stop)

    def nbe(self, sid):
        self.calls.append(sid)
        return self.RUN, periods(self.RUN, [0.3] * 20)

    def test_writes_markets_in_the_loaders_shape(self):
        ok, miss = r.write_fair_file(self.path, now=self.NOW, events=self.events,
                                     nbe_fn=self.nbe)
        self.assertEqual((ok, miss), (2, 3))
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        m = d["markets"]["KXRAINWKND-26OCT17-NYC"]
        self.assertEqual((m["event"], m["station"], m["fetched_at"]),
                         ("KXRAINWKND-26OCT17", "KNYC", self.NOW.isoformat()))
        self.assertTrue(0.01 <= m["p"] <= 0.99)
        self.assertEqual(d["markets"]["KXRAINWKND-26OCT17-WPB"]["station"], "KPBI")
        self.assertIn("unmapped", d["missing"]["KXRAINWKND-26OCT17-XXX"])
        self.assertIn("not the weekend", d["missing"]["KXRAINWKND-26OCT17-BAD"])
        self.assertEqual(d["missing"]["KXRAINWKND-26OCT10-NYC"], "weekend started")
        self.assertEqual(sorted(self.calls), ["KNYC", "KPBI"])   # one read a station

    def test_a_failed_read_reuses_the_files_run_while_young(self):
        r.write_fair_file(self.path, now=self.NOW, events=self.events, nbe_fn=self.nbe)
        def down(sid):
            raise ConnectionError("IEM down")
        later = self.NOW + timedelta(hours=6)
        ok, _ = r.write_fair_file(self.path, now=later, events=self.events, nbe_fn=down)
        self.assertEqual(ok, 2)
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["markets"]["KXRAINWKND-26OCT17-NYC"]["run"], self.RUN.isoformat())
        # a short run is a failed read too; past the age limit nothing prices
        short = lambda sid: (self.RUN + timedelta(hours=12), periods(self.RUN, [0.5] * 3))
        self.assertEqual(r.write_fair_file(self.path, now=later, events=self.events,
                                           nbe_fn=short)[0], 2)
        stale = self.RUN + timedelta(hours=r.NBE_MAX_AGE_HOURS + 1)
        ok, _ = r.write_fair_file(self.path, now=stale, events=self.events, nbe_fn=down)
        self.assertEqual(ok, 0)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["missing"]["KXRAINWKND-26OCT17-NYC"],
                             "no fresh NBE run")

    def test_fetch_events_reads_station_and_span(self):
        mk = lambda t, ev, code: {"ticker": t, "event_ticker": ev,
                                  "rules_primary": RULES.format(code=code)}
        js = {"markets": [mk("KXRAINWKND-26OCT17-NYC", "KXRAINWKND-26OCT17", "NYC"),
                          mk("KXRAINWKND-26OCT17-WPB", "KXRAINWKND-26OCT17", "DJT"),
                          mk("KXRAINWKND-26OCT18-NYC", "KXRAINWKND-26OCT18", "NYC")]}
        seen = []
        def get_json(path, params):
            seen.append((path, params["series_ticker"], params["status"]))
            return js
        ev = r.fetch_events(get_json)
        self.assertEqual(seen, [("/markets", "KXRAINWKND", "open"),
                                ("/markets", "KXRAINELECTION", "open")])
        self.assertEqual(list(ev), ["KXRAINWKND-26OCT17"])       # not a Saturday: out
        self.assertEqual(ev["KXRAINWKND-26OCT17"]["markets"]["KXRAINWKND-26OCT17-WPB"],
                         {"code": "DJT", "span": ["2026-10-17", "2026-10-18"]})
        self.assertEqual(ev["KXRAINWKND-26OCT17"]["days"], 2)

    def test_fetch_events_reads_the_one_day_series_too(self):
        rules = ("If the total precipitation at Houston (Hobby) (KHOU) in Houston in "
                 "November 3, 2026 is strictly greater than 0 inches, then the market "
                 "resolves to Yes.")
        by = {"KXRAINWKND": {"markets": []},
              "KXRAINELECTION": {"markets": [
                  {"ticker": "KXRAINELECTION-03NOV26-HOU",
                   "event_ticker": "KXRAINELECTION-03NOV26", "rules_primary": rules}]}}
        ev = r.fetch_events(lambda path, params: by[params["series_ticker"]])
        self.assertEqual(ev, {"KXRAINELECTION-03NOV26": {
            "sat": "2026-11-03", "days": 1, "markets": {"KXRAINELECTION-03NOV26-HOU": {
                "code": "HOU", "span": ["2026-11-03", "2026-11-03"]}}}})

    def test_a_one_day_market_prices_its_day_and_stops_at_its_start(self):
        now, run = datetime(2026, 11, 1, 6, tzinfo=UTC), datetime(2026, 11, 1, 0, tzinfo=UTC)
        day = ["2026-11-03", "2026-11-03"]
        events = {"KXRAINELECTION-03NOV26": {"sat": "2026-11-03", "days": 1, "markets": {
            "KXRAINELECTION-03NOV26-NYC": {"code": "NYC", "span": day},
            "KXRAINELECTION-03NOV26-HOU": {"code": "HOU", "span": day},
            "KXRAINELECTION-03NOV26-BAD": {"code": "BOS",
                                           "span": ["2026-11-03", "2026-11-04"]}}}}
        nbe = lambda sid: (run, periods(run, [0.3] * 20))
        ok, miss = r.write_fair_file(self.path, now=now, events=events, nbe_fn=nbe)
        self.assertEqual((ok, miss), (2, 1))
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        m = d["markets"]["KXRAINELECTION-03NOV26-NYC"]
        one = r.market_fair(periods(run, [0.3] * 20), run, date(2026, 11, 3), 5, days=1)
        self.assertEqual((m["p"], m["coverage"], m["station"]), (one["p"], 2.0, "KNYC"))
        # a day of 30% half-days is drier than a weekend of them
        two = r.market_fair(periods(run, [0.3] * 20), run, date(2026, 11, 3), 5)
        self.assertLess(one["p"], two["p"])
        self.assertIn("not the ticker's day", d["missing"]["KXRAINELECTION-03NOV26-BAD"])
        # out from 00:00 local standard time on the day: New York at 05:00Z,
        # Houston (UTC-6) an hour later
        run2 = datetime(2026, 11, 3, 0, tzinfo=UTC)
        r.write_fair_file(self.path, now=datetime(2026, 11, 3, 5, 30, tzinfo=UTC),
                          events=events, nbe_fn=lambda sid: (run2, periods(run2, [0.3] * 20)))
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["missing"]["KXRAINELECTION-03NOV26-NYC"], "the day started")
        self.assertIn("KXRAINELECTION-03NOV26-HOU", d["markets"])

    def test_events_are_cached_for_an_hour_unless_none_are_open(self):
        n = []
        listed = {"markets": [{"ticker": "KXRAINWKND-26OCT17-NYC",
                               "event_ticker": "KXRAINWKND-26OCT17",
                               "rules_primary": RULES.format(code="NYC")}]}
        def get_json(path, params):
            if path == "/series":                             # the weather category
                return {"series": []}
            if params["series_ticker"] != "KXRAINWKND":      # the one-day series
                return {"markets": []}
            n.append(1)
            return listed
        r.write_fair_file(self.path, now=self.NOW, get_json=get_json, nbe_fn=self.nbe)
        ok, _ = r.write_fair_file(self.path, now=self.NOW + timedelta(minutes=30),
                                  get_json=get_json, nbe_fn=self.nbe)
        self.assertEqual((len(n), ok), (1, 1))
        r.write_fair_file(self.path, now=self.NOW + timedelta(minutes=61),
                          get_json=get_json, nbe_fn=self.nbe)
        self.assertEqual(len(n), 2)
        # nothing open (Monday between the close and the listing): every pass
        listed = {"markets": []}
        for m in (130, 140):
            r.write_fair_file(self.path, now=self.NOW + timedelta(minutes=m),
                              get_json=get_json, nbe_fn=self.nbe)
        self.assertEqual(len(n), 4)


class TestAutoDaySeries(unittest.TestCase):
    """Jack 2026-10-31: "make sure new markets with 'RAIN', 'TEMP', etc are
    always considered for auto-quoting as part of these families"."""

    NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
    ONE = ("If the total precipitation at {name} (K{code}) in {name} in November 26, "
           "2026 is strictly greater than 0 inches, then the market resolves to Yes.")

    def market(self, t, rules):
        return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "rules_primary": rules}

    def turkey(self):
        return [self.market("KXRAINTURKEY-26NOV26-NYC", self.ONE.format(name="NYC", code="NYC")),
                self.market("KXRAINTURKEY-26NOV26-BOS", self.ONE.format(name="Boston", code="BOS"))]

    def test_the_contract_is_a_market_per_city_rain_over_named_days(self):
        got, why = r.classify_day_markets(self.turkey())
        self.assertEqual((got, why), ({"events": {"KXRAINTURKEY-26NOV26": ["2026-11-26", 1]},
                                       "sample": "KXRAINTURKEY-26NOV26-NYC"}, ""))
        # a weekend-style span of the CLI form
        got, _ = r.classify_day_markets([self.market("KXRAINXMAS-26DEC25-ATL",
                                                     RULES.format(code="ATL"))])
        self.assertEqual(got["events"], {"KXRAINXMAS-26DEC25": ["2026-10-17", 2]})
        # not the contract: a threshold, a strike or a day ticker, a month,
        # four days, nothing open
        for ms, bit in (
                ([self.market("KXRAINT-26NOV26-NYC", self.ONE.format(name="NYC", code="NYC")
                              .replace("than 0 inches", "than 0.5 inches"))], "strictly"),
                ([self.market("KXRAINT-26NOV26-T0", self.ONE.format(name="NYC", code="NYC"))],
                 "per city"),
                ([self.market("KXRAINDNYC-261126-0", self.ONE.format(name="NYC", code="NYC"))],
                 "per city"),
                ([self.market("KXRAINT-26NOV-NYC", "If the total precipitation at NYC (KNYC) "
                              "in November 2026 is strictly greater than 0 inches")], "no day"),
                ([self.market("KXRAINT-26NOV26-NYC", RULES.format(code="NYC").replace(
                    "October 18, 2026", "October 20, 2026"))], "4-day"),
                ([], "no open market")):
            got, why = r.classify_day_markets(ms)
            self.assertIsNone(got)
            self.assertIn(bit, why)

    def fake(self, by_series, calls):
        def get_json(path, params):
            calls.append((path, params.get("series_ticker") or params.get("category")))
            if path == "/series":
                return {"series": [{"ticker": s} for s in
                                   ("KXRAINTURKEY", "KXRAINWKND", "KXRAINATLM", "KXRAINDNYC",
                                    "KXSNOWNYCM", "KXRAINELECTION")]}
            return {"markets": by_series.get(params["series_ticker"], [])}
        return get_json

    def test_discovery_reads_the_unclaimed_rain_series_and_caches_them(self):
        calls = []
        by = {"KXRAINTURKEY": self.turkey(),
              "KXRAINDNYC": [self.market("KXRAINDNYC-261126-0",
                                         self.ONE.format(name="NYC", code="NYC"))]}
        get = self.fake(by, calls)
        # the bot's rain and snow monthlies
        skip = lambda s: s in ("KXRAINATLM", "KXSNOWNYCM")
        ts = self.NOW.timestamp()
        found, rej, at = r.discover_day_series({}, ts, get, skip)
        self.assertEqual(set(found), {"KXRAINTURKEY"})
        self.assertIn("per city", rej["KXRAINDNYC"]["why"])
        self.assertEqual(at, ts)
        # the category, then only the unclaimed rain (and snow: SNOW DAYS)
        # series: not the weekend, the named day series, or the monthlies the
        # bot owns
        self.assertEqual(calls, [("/series", "Climate and Weather"),
                                 ("/markets", "KXRAINDNYC"), ("/markets", "KXRAINTURKEY")])
        old = {"day_series": found, "day_rejected": rej, "day_discovered_at": at}
        calls.clear()
        self.assertEqual(r.discover_day_series(old, ts + 60, get, skip)[0], found)
        self.assertEqual(calls, [])                                # inside the hour
        r.discover_day_series(old, ts + r.DAY_DISCOVER_SECS + 1, get, skip)
        self.assertEqual(calls, [("/series", "Climate and Weather")])   # verdicts kept
        calls.clear()
        r.discover_day_series(old, ts + r.DAY_RETRY_SECS + 1, get, skip)
        self.assertEqual(len(calls), 3)                            # and read again
        with mock.patch.object(r, "AUTO_DAY_ENABLE", False):
            self.assertEqual(r.discover_day_series(old, ts + 1e6, get, skip)[0], {})

    def test_a_series_listed_before_its_markets_open_is_read_at_the_next_pass(self):
        # 2026-11-26: the 00:19:55Z pass read KXSNOWCHRISTMAS 14 minutes before
        # its markets opened; the 6-hour retry kept it out until ~06:20Z
        calls = []
        by = {"KXRAINDNYC": [self.market("KXRAINDNYC-261126-0",
                                         self.ONE.format(name="NYC", code="NYC"))]}
        get = self.fake(by, calls)
        ts = self.NOW.timestamp()
        found, rej, at = r.discover_day_series({}, ts, get)
        self.assertEqual(found, {})
        self.assertEqual(rej["KXRAINTURKEY"]["why"], r.NO_OPEN_MARKET)
        self.assertIn("per city", rej["KXRAINDNYC"]["why"])
        old = {"day_series": found, "day_rejected": rej, "day_discovered_at": at}
        by["KXRAINTURKEY"] = self.turkey()           # its markets open
        calls.clear()
        self.assertEqual(r.discover_day_series(old, ts + 60, get)[0], {})
        self.assertEqual(calls, [])                    # inside the hour
        found, rej, _ = r.discover_day_series(old, ts + r.DAY_DISCOVER_SECS + 1, get)
        self.assertEqual(set(found), {"KXRAINTURKEY"})
        self.assertNotIn("KXRAINTURKEY", rej)
        # a real rejection still waits DAY_RETRY_SECS: KXRAINDNYC was not re-read
        read = [t for p, t in calls if p == "/markets"]
        self.assertIn("KXRAINTURKEY", read)
        self.assertNotIn("KXRAINDNYC", read)

    def test_a_new_series_is_priced_on_its_rules_window_in_the_same_pass(self):
        path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        calls = []
        get = self.fake({"KXRAINTURKEY": self.turkey()}, calls)
        run = datetime(2026, 11, 23, 0, tzinfo=UTC)
        nbe = lambda sid: (run, periods(run, [0.3] * 20))
        # a fresh events cache from before the series listed
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"open_events": {"KXRAINWKND-26NOV21": {"sat": "2026-11-21",
                                                              "markets": {}}},
                       "events_at": self.NOW.timestamp() - 60}, f)
        ok, _miss = r.write_fair_file(path, now=self.NOW, get_json=get, nbe_fn=nbe)
        self.assertEqual(ok, 2)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(set(d["day_series"]), {"KXRAINTURKEY"})
        self.assertEqual(d["open_events"]["KXRAINTURKEY-26NOV26"]["sat"], "2026-11-26")
        m = d["markets"]["KXRAINTURKEY-26NOV26-BOS"]
        self.assertEqual((m["station"], m["coverage"]), ("KBOS", 2.0))
        one = r.market_fair(periods(run, [0.3] * 20), run, date(2026, 11, 26), 5, days=1)
        self.assertEqual(m["p"], one["p"])


class TestStations(unittest.TestCase):
    """Jack 2026-10-29: "Yes, map them" (the 11 cities Kalshi added without a
    row here) and "also automatically map any future cities"."""
    NOW = datetime(2026, 10, 15, 6, tzinfo=UTC)
    RUN = datetime(2026, 10, 15, 0, tzinfo=UTC)

    def test_the_eleven_cities_of_26oct24_are_mapped(self):
        want = {"ABQ": ("KABQ", 7), "CMH": ("KCMH", 5), "DTW": ("KDTW", 5),
                "IND": ("KIND", 5), "LEX": ("KLEX", 5), "MKE": ("KMKE", 6),
                "PIT": ("KPIT", 5), "PVD": ("KPVD", 5), "SGF": ("KSGF", 6),
                "STL": ("KSTL", 6), "TPA": ("KTPA", 5)}
        self.assertEqual({c: r.STATIONS[c] for c in want}, want)
        self.assertEqual(len(r.STATIONS), 35)

    def test_lst_hours_takes_daylight_time_out(self):
        summer, winter = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)
        for tz, h in (("America/New_York", 5), ("America/Indiana/Indianapolis", 5),
                      ("America/Detroit", 5), ("America/Chicago", 6),
                      ("America/Denver", 7), ("America/Phoenix", 7),
                      ("America/Los_Angeles", 8), ("Pacific/Honolulu", 10),
                      ("America/Anchorage", 9), ("America/Puerto_Rico", 4)):
            self.assertEqual((r.lst_hours(tz, summer), r.lst_hours(tz, winter)), (h, h), tz)
        for bad in ("Mars/Olympus", "America/St_Johns"):        # unknown / off the hour
            with self.assertRaises(RuntimeError):
                r.lst_hours(bad, summer)

    def _get(self, tz="America/Chicago", last_cli="2026-10-14", nws=None):
        def get(url, params=None):
            if "weather.gov" in url:
                if nws is not None:
                    raise nws
                return {"properties": {"timeZone": tz, "name": "Xyz Intl"}}
            return {"results": [{"valid": "2026-10-01"}, {"valid": last_cli}]}
        return get

    def nbe(self, sid):
        return self.RUN, periods(self.RUN, [0.3] * 20)

    def test_resolve_station_checks_what_the_model_needs(self):
        row = r.resolve_station("XYZ", self.NOW, get=self._get(), nbe_fn=self.nbe)
        self.assertEqual({k: row[k] for k in ("icao", "lst", "tz", "name", "auto")},
                         {"icao": "KXYZ", "lst": 6, "tz": "America/Chicago",
                          "name": "Xyz Intl", "auto": True})
        cases = (
            (dict(get=self._get(tz="")), "no time zone"),
            (dict(get=self._get(last_cli="2026-09-30")), "no IEM CLI report"),
            (dict(get=self._get(), nbe_fn=lambda sid: (self.RUN, periods(self.RUN, [0.3] * 3))),
             "no NBE run"),
            (dict(get=self._get(), nbe_fn=lambda sid: (
                self.RUN - timedelta(hours=40), periods(self.RUN, [0.3] * 20))), "over 30h old"))
        for kw, msg in cases:
            kw.setdefault("nbe_fn", self.nbe)
            with self.assertRaises(RuntimeError) as cm:
                r.resolve_station("XYZ", self.NOW, **kw)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(ConnectionError):                 # the caller records it
            r.resolve_station("XYZ", self.NOW, get=self._get(nws=ConnectionError("down")),
                              nbe_fn=self.nbe)

    def test_a_four_letter_code_resolves_as_written(self):
        # 2026-11-22: there is no KPHNL; the IEM's CLI answers station=PHNL (and
        # refuses station=HNL)
        asked, seen = [], []

        def get(url, params=None):
            asked.append((url, (params or {}).get("station")))
            if "weather.gov" in url:
                return {"properties": {"timeZone": "Pacific/Honolulu",
                                       "name": "Daniel K Inouye International Airport"}}
            return {"results": [{"valid": "2026-10-14"}]}
        row = r.resolve_station("PHNL", self.NOW, get=get,
                                nbe_fn=lambda sid: seen.append(sid) or self.nbe(sid))
        self.assertEqual((row["icao"], row["lst"], row["tz"]), ("PHNL", 10, "Pacific/Honolulu"))
        self.assertEqual(asked, [(r.NWS_STATION_URL.format(sid="PHNL"), None),
                                 (r.IEM_CLI_URL, "PHNL")])
        self.assertEqual(seen, ["PHNL"])
        # the lower 48 as before
        self.assertEqual(r.resolve_station("XYZ", self.NOW, get=self._get(),
                                           nbe_fn=self.nbe)["icao"], "KXYZ")

    def _events(self, code="XYZ"):
        span = ["2026-10-17", "2026-10-18"]
        return {"KXRAINWKND-26OCT17": {"sat": "2026-10-17", "markets": {
            "KXRAINWKND-26OCT17-NYC": {"code": "NYC", "span": span},
            "KXRAINWKND-26OCT17-XXX": {"code": code, "span": span}}}}

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_a_new_city_is_mapped_once_and_priced(self):
        path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        seen = []
        def resolve(code, now):
            seen.append(code)
            return {"icao": f"K{code}", "lst": 6, "tz": "America/Chicago",
                    "name": "Xyz Intl", "auto": True, "checked_at": now.isoformat()}
        ok, miss = r.write_fair_file(path, now=self.NOW, events=self._events(),
                                     nbe_fn=self.nbe, resolve_fn=resolve)
        self.assertEqual((ok, miss, seen), (2, 0, ["XYZ"]))       # NYC is a hand row
        d = self._read(path)
        self.assertEqual(d["markets"]["KXRAINWKND-26OCT17-XXX"]["station"], "KXYZ")
        self.assertEqual(d["auto_stations"]["XYZ"]["lst"], 6)
        self.assertEqual(d["unmapped"], {})
        # kept in the file: the next pass prices it without asking again
        ok, _ = r.write_fair_file(path, now=self.NOW + timedelta(minutes=30),
                                  events=self._events(), nbe_fn=self.nbe, resolve_fn=resolve)
        self.assertEqual((ok, seen), (2, ["XYZ"]))

    def test_a_failed_mapping_stands_aside_and_is_retried_after_six_hours(self):
        path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        tries = []
        def down(code, now):
            tries.append(now)
            raise RuntimeError("KXYZ: no IEM CLI report within 10 days (last None)")
        ok, miss = r.write_fair_file(path, now=self.NOW, events=self._events(),
                                     nbe_fn=self.nbe, resolve_fn=down)
        self.assertEqual((ok, miss), (1, 1))
        d = self._read(path)
        self.assertEqual(d["missing"]["KXRAINWKND-26OCT17-XXX"],
                         "unmapped station XYZ (RuntimeError: KXYZ: no IEM CLI report "
                         "within 10 days (last None))")
        self.assertEqual(d["unmapped"]["XYZ"]["at"], self.NOW.timestamp())
        r.write_fair_file(path, now=self.NOW + timedelta(hours=5), events=self._events(),
                          nbe_fn=self.nbe, resolve_fn=down)
        self.assertEqual(len(tries), 1)                          # not yet
        ok, _ = r.write_fair_file(
            path, now=self.NOW + timedelta(hours=6, minutes=1), events=self._events(),
            nbe_fn=self.nbe,
            resolve_fn=lambda code, now: {"icao": "KXYZ", "lst": 5, "tz": "America/New_York",
                                          "name": "Xyz", "auto": True,
                                          "checked_at": now.isoformat()})
        self.assertEqual(ok, 2)
        self.assertEqual(self._read(path)["unmapped"], {})

    def test_a_network_failure_is_tried_again_in_minutes_not_hours(self):
        # 2026-11-22: the dry run's first PANC read was reset by the remote end
        path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        tries = []

        def reset(code, now):
            tries.append(now)
            raise r.requests.ConnectionError("('Connection aborted.', ConnectionResetError(10054))")
        r.write_fair_file(path, now=self.NOW, events=self._events(), nbe_fn=self.nbe,
                          resolve_fn=reset)
        self.assertIs(self._read(path)["unmapped"]["XYZ"]["net"], True)
        r.write_fair_file(path, now=self.NOW + timedelta(minutes=5), events=self._events(),
                          nbe_fn=self.nbe, resolve_fn=reset)
        self.assertEqual(len(tries), 1)                          # not inside the 10 minutes
        ok, _ = r.write_fair_file(
            path, now=self.NOW + timedelta(minutes=15), events=self._events(),
            nbe_fn=self.nbe,
            resolve_fn=lambda code, now: {"icao": "KXYZ", "lst": 5, "tz": "America/New_York",
                                          "name": "Xyz", "auto": True,
                                          "checked_at": now.isoformat()})
        self.assertEqual(ok, 2)
        self.assertEqual(self._read(path)["unmapped"], {})
        # a 429 / 5xx is the network too; a 404 or a failed check is a verdict
        for status, net in ((503, True), (429, True), (404, False), (422, False)):
            ex = r.requests.HTTPError(str(status), response=mock.Mock(status_code=status))
            self.assertIs(r._net_failure(ex), net, status)
        self.assertTrue(r._net_failure(ConnectionResetError(10054, "reset")))
        self.assertTrue(r._net_failure(r.requests.Timeout("read timed out")))
        self.assertFalse(r._net_failure(RuntimeError("KXYZ: NWS gives no time zone")))

    def test_a_hand_row_wins_over_an_auto_row(self):
        path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"auto_stations": {"NYC": {"icao": "KLGA", "lst": 5}}}, f)
        never = mock.Mock(side_effect=AssertionError("a hand code is never resolved"))
        r.write_fair_file(path, now=self.NOW, events=self._events(code="NYC"),
                          nbe_fn=self.nbe, resolve_fn=never)
        self.assertEqual(self._read(path)["markets"]["KXRAINWKND-26OCT17-NYC"]["station"],
                         "KNYC")
        never.assert_not_called()


class TestNbsBulletin(unittest.TestCase):
    """NOAA's hourly NBS text bulletin (THE COMPOSITE, Jack 2026-10-30:
    "incorporate both hourly and 6 hours for a composite")."""

    def test_a_block_reads_its_12_hour_pops_by_forecast_hour(self):
        rt, per = r.parse_nbs_block(KATL_13Z)
        self.assertEqual(rt, datetime(2026, 10, 29, 13, tzinfo=UTC))
        self.assertEqual(per, [(datetime(2026, 10, 30, 12, tzinfo=UTC), 0.0),
                               (datetime(2026, 10, 31, 0, tzinfo=UTC), 0.0),
                               (datetime(2026, 10, 31, 12, tzinfo=UTC), 0.0),
                               (datetime(2026, 11, 1, 0, tzinfo=UTC), 0.02),
                               (datetime(2026, 11, 1, 12, tzinfo=UTC), 0.16)])
        # a one-digit month is space-padded (" 9/24/2026"); no P12 row reads
        # nothing; anything else is no block at all
        sep = KATL_13Z.replace("10/29/2026  1300", " 9/24/2026  1100")
        self.assertEqual(r.parse_nbs_block(sep)[0], datetime(2026, 9, 24, 11, tzinfo=UTC))
        bare = "\n".join(ln for ln in KATL_13Z.splitlines() if not ln.startswith(" P12"))
        self.assertEqual(r.parse_nbs_block(bare)[1], [])
        self.assertEqual(r.parse_nbs_block("not a bulletin"), (None, []))
        self.assertEqual(r.parse_nbs_block(""), (None, []))

    def test_the_header_without_a_version_reads_too(self):
        # the header NOAA has written since the 11/03 12Z cycle: the pattern
        # that wanted "V5.0" read no station from then to 11/22
        rt, per = r.parse_nbs_block(KNYC_23Z)
        self.assertEqual(rt, datetime(2026, 11, 22, 23, tzinfo=UTC))
        self.assertEqual(per, [(datetime(2026, 11, 23, 12, tzinfo=UTC), 0.37),
                               (datetime(2026, 11, 24, 0, tzinfo=UTC), 0.05),
                               (datetime(2026, 11, 24, 12, tzinfo=UTC), 0.02),
                               (datetime(2026, 11, 25, 0, tzinfo=UTC), 0.0),
                               (datetime(2026, 11, 25, 12, tzinfo=UTC), 0.0)])
        # a version token back in, of any spelling, reads the same; another
        # bulletin (the NBE's) is no block
        for tok in ("V5.0", "V5.1", "v5.2.1"):
            got = r.parse_nbs_block(KNYC_23Z.replace("NBM  NBS", f"NBM {tok} NBS"))
            self.assertEqual(got, (rt, per))
        self.assertEqual(r.parse_nbs_block(KNYC_23Z.replace("NBS GUIDANCE", "NBE GUIDANCE")),
                         (None, []))
        # a whole bulletin of them: each station's block found where it sits
        data = self._bulletin(datetime(2026, 11, 22, 23, tzinfo=UTC), stations=("KNYC", "PHNL"),
                              tmpl=KNYC_23Z, sid="KNYC", stamp="11/22/2026  2300")
        blocks = r.nbs_blocks(data, ["KNYC", "PHNL", "KZZZ"])
        self.assertEqual(sorted(blocks), ["KNYC", "PHNL"])
        for sid, (off, ln, txt) in blocks.items():
            self.assertEqual(data[off:off + ln].decode("ascii"), txt)
            self.assertTrue(txt.startswith(f" {sid}    NBM  NBS GUIDANCE"))

    def test_a_bulletin_without_the_version_reads_whole_then_by_range(self):
        now = datetime(2026, 11, 23, 0, 50, tzinfo=UTC)
        c23, c00 = datetime(2026, 11, 22, 23, tzinfo=UTC), datetime(2026, 11, 23, 0, tzinfo=UTC)
        new = dict(stations=("KNYC", "PHNL"), tmpl=KNYC_23Z, sid="KNYC", stamp="11/22/2026  2300")
        http = self._Http({c23: self._bulletin(c23, **new)})
        with mock.patch.object(r, "NBS_HOURLY_MAX_BPS", 0):
            cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KNYC", "PHNL"], now, {}, http=http)
            self.assertEqual((cyc, sorted(got), http.wholes, http.ranges),
                             (c23, ["KNYC", "PHNL"], 1, 0))
            self.assertEqual(got["KNYC"][1][0], (datetime(2026, 11, 23, 12, tzinfo=UTC), 0.37))
            self.assertEqual(idx["sizes"][str(len(http.files[c23]))]["absent"], [])
            # the next cycle, same layout: by range
            http.files[c00] = self._bulletin(c00, **new)
            cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KNYC", "PHNL"], now + timedelta(hours=1),
                                                    idx, http=http)
            self.assertEqual((cyc, how, http.wholes, http.ranges), (c00, "by range", 1, 2))
            self.assertEqual(got["PHNL"][0], c00)

    def _bulletin(self, cyc: datetime, stations=("KATL", "KSEA"), filler=3,
                  tmpl=KATL_13Z, sid="KATL", stamp="10/29/2026  1300"):
        hh = f"{cyc:%m/%d/%Y  %H}00"
        out = [" 1\n", " " * 74 + "\n"]
        for i in range(filler):                                   # other stations
            out.append(tmpl.replace(sid, f"X{i:03d}").replace(stamp, hh))
        for s in stations:
            out.append(tmpl.replace(sid, s).replace(stamp, hh))
        return "".join(out).encode("ascii")

    class _Http:
        """S3 stand-in: `files` maps cycle -> bytes; counts the reads."""

        def __init__(self, files):
            self.files, self.heads, self.ranges, self.wholes = files, 0, 0, 0

        def _cycle(self, url):
            seg = url.split("/blend.")[1]
            return datetime.strptime(seg[:8] + seg[9:11], "%Y%m%d%H").replace(tzinfo=UTC)

        def head(self, url, headers=None, timeout=None):
            self.heads += 1
            data = self.files.get(self._cycle(url))
            return mock.Mock(status_code=200 if data is not None else 404,
                             headers={"Content-Length": str(len(data or b""))})

        def get(self, url, headers=None, timeout=None, stream=False):
            data = self.files[self._cycle(url)]
            rng = (headers or {}).get("Range")
            if rng:
                self.ranges += 1
                a, b = (int(x) for x in rng.split("=")[1].split("-"))
                return mock.Mock(status_code=206, content=data[a:b + 1])
            self.wholes += 1
            resp = mock.Mock(status_code=200)
            resp.iter_content = lambda n: (data[i:i + n] for i in range(0, len(data), n))
            return resp

    def test_the_newest_posted_cycle_by_range_once_its_layout_is_known(self):
        now = datetime(2026, 10, 30, 14, 20, tzinfo=UTC)
        c13, c12 = datetime(2026, 10, 30, 13, tzinfo=UTC), datetime(2026, 10, 30, 12, tzinfo=UTC)
        http = self._Http({c12: self._bulletin(c12), c13: self._bulletin(c13)})
        with mock.patch.object(r, "NBS_HOURLY_MAX_BPS", 0):
            cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KATL", "KSEA", "KZZZ"], now, {}, http=http)
        # 13Z is the newest posted (14Z is not); the first read is whole and
        # indexes the layout, KZZZ noted absent
        self.assertEqual((cyc, sorted(got), http.wholes, http.ranges), (c13, ["KATL", "KSEA"], 1, 0))
        self.assertIn("whole file", how)
        lay = idx["sizes"][str(len(http.files[c13]))]
        self.assertEqual(lay["absent"], ["KZZZ"])
        self.assertEqual(got["KATL"][0], c13)
        # the fixture is restamped 10/30 13Z: its 71-hour column ends 11/02 12Z
        self.assertEqual(got["KATL"][1][-1], (datetime(2026, 11, 2, 12, tzinfo=UTC), 0.16))
        # the next cycle, same layout: two byte ranges, no whole file
        c14 = datetime(2026, 10, 30, 14, tzinfo=UTC)
        http.files[c14] = self._bulletin(c14)
        cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KATL", "KSEA", "KZZZ"],
                                                now + timedelta(hours=1), idx, http=http)
        self.assertEqual((cyc, how, http.wholes, http.ranges), (c14, "by range", 1, 2))
        self.assertEqual(got["KSEA"][0], c14)

    def test_a_new_or_shifted_layout_is_read_whole_and_nothing_posted_is_none(self):
        now = datetime(2026, 10, 30, 14, 20, tzinfo=UTC)
        c13 = datetime(2026, 10, 30, 13, tzinfo=UTC)
        http = self._Http({c13: self._bulletin(c13)})
        with mock.patch.object(r, "NBS_HOURLY_MAX_BPS", 0):
            _c, _g, idx, _h = REAL_FETCH_NBS_HOURLY(["KATL"], now, {}, http=http)
            # a cycle with one more station ahead of ours: a new size, read whole
            c14 = datetime(2026, 10, 30, 14, tzinfo=UTC)
            http.files[c14] = self._bulletin(c14, filler=4)
            cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KATL"], now + timedelta(hours=1), idx,
                                                    http=http)
            self.assertEqual((cyc, http.wholes, len(idx["sizes"])), (c14, 2, 2))
            # same size as 13Z's but the stations reordered: the range reads
            # another station's block, so the file is read whole again
            c15 = datetime(2026, 10, 30, 15, tzinfo=UTC)
            http.files[c15] = self._bulletin(c15, stations=("KSEA", "KATL"))
            self.assertEqual(len(http.files[c15]), len(http.files[c13]))
            cyc, got, idx, how = REAL_FETCH_NBS_HOURLY(["KATL"], now + timedelta(hours=2), idx,
                                                    http=http)
            self.assertEqual((cyc, http.wholes, got["KATL"][0]), (c15, 3, c15))
        # nothing posted inside the lookback
        cyc, got, idx2, how = REAL_FETCH_NBS_HOURLY(["KATL"], now + timedelta(days=2), idx,
                                                 http=http)
        self.assertEqual((cyc, got), (None, {}))
        self.assertIn("no cycle posted", how)


class TestComposite(unittest.TestCase):
    """THE COMPOSITE: NBS_W_HOURLY x the newest hourly cycle + the rest x the
    newest 6-hourly one on the periods both reach, either alone where only it
    does, the NBE for the rest; the lead off the freshest run used."""
    NOW = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
    E_RUN = datetime(2026, 10, 15, 12, tzinfo=UTC)       # the NBE's newest
    S_RUN = datetime(2026, 10, 15, 12, tzinfo=UTC)       # the IEM's 6-hourly
    H_RUN = datetime(2026, 10, 15, 13, tzinfo=UTC)       # NOAA's hourly
    T = "KXRAINWKND-26OCT17-NYC"

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "rain_wknd_fair.json")
        self.events = {"KXRAINWKND-26OCT17": {"sat": "2026-10-17", "markets": {
            self.T: {"code": "NYC", "span": ["2026-10-17", "2026-10-18"]}}}}
        self.calls = {"E": 0, "S": 0, "H": 0}

    def ep(self):
        return periods(self.E_RUN, [0.3] * 20)

    def sp(self):
        return periods(self.S_RUN, [0.6] * 6)            # to 10/18 12Z

    def hp(self):
        return periods(self.S_RUN, [0.2] * 7)            # one period further

    def e(self, sid):
        self.calls["E"] += 1
        return self.E_RUN, self.ep()

    def s(self, sid):
        self.calls["S"] += 1
        return self.S_RUN, self.sp()

    def h(self, sids, now, index):
        self.calls["H"] += 1
        return self.H_RUN, {sid: (self.H_RUN, self.hp()) for sid in sids}, {"sizes": {}}, "x"

    def _write(self, now=None, **kw):
        fns = dict(nbe_fn=self.e, nbs_fn=self.s, nbs_hourly_fn=self.h)
        fns.update(kw)
        r.write_fair_file(self.path, now=now or self.NOW, events=self.events, **fns)
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _expect(self, merged, rt, lag):
        return r.market_fair(sorted(merged.items()), rt, SAT, 5, lag_hours=lag)["p"]

    def test_hourly_and_6_hourly_blend_where_both_reach(self):
        d = self._write()
        m = d["markets"][self.T]
        sp, hp = dict(self.sp()), dict(self.hp())
        merged = dict(self.ep())
        for ft in set(sp) | set(hp):
            merged[ft] = (0.75 * hp[ft] + 0.25 * sp[ft]) if ft in sp and ft in hp else hp[ft]
        self.assertEqual(r.NBS_W_HOURLY, 0.75)                 # the backtested weight
        self.assertAlmostEqual(m["p"], self._expect(merged, self.H_RUN, 1.0), places=4)
        self.assertEqual((m["src"], m["run"]), ("H+S+E", self.H_RUN.isoformat()))
        self.assertEqual(m["runs"], {"E": self.E_RUN.isoformat(), "S": self.S_RUN.isoformat(),
                                     "H": self.H_RUN.isoformat()})
        self.assertEqual(d["nbs_hourly"]["cycle"], self.H_RUN.isoformat())
        # all weight on the hourly cycle: the 6-hourly run adds nothing it covers
        with mock.patch.object(r, "NBS_W_HOURLY", 1.0):
            m1 = self._write(now=self.NOW + timedelta(minutes=1))["markets"][self.T]
        only_h = dict(self.ep())
        only_h.update(hp)
        self.assertAlmostEqual(m1["p"], self._expect(only_h, self.H_RUN, 1.0), places=4)

    def test_a_source_that_fails_or_ages_out_leaves_the_composite(self):
        def down(*a, **k):
            raise ConnectionError("down")
        m = self._write(nbs_fn=down, nbs_hourly_fn=down)["markets"][self.T]
        self.assertEqual(m["src"], "E")
        self.assertAlmostEqual(m["p"], r.market_fair(self.ep(), self.E_RUN, SAT, 5)["p"],
                               places=4)
        # an hourly cycle older than NBS_HOURLY_MAX_AGE_HOURS drops out, the
        # 6-hourly run stays
        old = self.H_RUN - timedelta(hours=7)
        old_h = lambda sids, now, idx: (old, {s: (old, self.hp()) for s in sids}, {}, "x")
        os.remove(self.path)
        self.assertEqual(self._write(nbs_hourly_fn=old_h)["markets"][self.T]["src"], "S+E")
        with mock.patch.object(r, "NBS_ENABLE", False),                 mock.patch.object(r, "NBS_HOURLY_ENABLE", False):
            os.remove(self.path)
            self.assertEqual(self._write()["markets"][self.T]["src"], "E")

    def test_the_iem_and_noaa_are_read_only_when_a_newer_run_is_due(self):
        self._write()
        self.assertEqual(self.calls, {"E": 1, "S": 1, "H": 1})
        # 15 minutes on: nothing newer is due anywhere -- every source cached
        self._write(now=self.NOW + timedelta(minutes=15))
        self.assertEqual(self.calls, {"E": 1, "S": 1, "H": 1})
        # 15:00Z: the 14Z hourly cycle is due (13Z + 1h + its ~1h posting)
        self._write(now=datetime(2026, 10, 15, 15, 0, tzinfo=UTC))
        self.assertEqual(self.calls, {"E": 1, "S": 1, "H": 2})
        # 19:01Z: the IEM's 18Z 6-hourly run (12Z + 6h, an hour on)
        self._write(now=datetime(2026, 10, 15, 19, 1, tzinfo=UTC))
        self.assertEqual(self.calls["S"], 2)
        # 01:01Z next day: the NBE's 00Z run (12Z + 12h, an hour on)
        self._write(now=datetime(2026, 10, 16, 1, 1, tzinfo=UTC))
        self.assertEqual(self.calls["E"], 2)

    def test_an_hourly_cycle_the_old_parser_read_is_read_again_at_once(self):
        # the file's 13Z cycle, every station searched and none read (the
        # header pattern that wanted "V5.0"): read again on this pass, not when
        # 14Z is due at 15:00Z
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"nbs_hourly": {"cycle": self.H_RUN.isoformat(), "runs": {},
                                      "searched": ["KNYC"], "index": {"sizes": {
                                          "29577695": {"offsets": {}, "absent": ["KNYC"]}}}}}, f)
        d = self._write()
        self.assertEqual(self.calls["H"], 1)
        self.assertEqual((d["nbs_hourly"]["v"], d["markets"][self.T]["src"]),
                         (r.NBS_HOURLY_PARSE_VERSION, "H+S+E"))
        # this parser's own cycle is kept until the next one is due
        self._write(now=self.NOW + timedelta(minutes=15))
        self.assertEqual(self.calls["H"], 1)

    def test_a_cycle_that_reads_no_station_is_flagged(self):
        def none_read(sids, now, index):
            return self.H_RUN, {}, {"sizes": {}}, "whole file (its layout indexed)"
        with mock.patch.object(r, "_log") as log:
            m = self._write(nbs_hourly_fn=none_read)["markets"][self.T]
        said = [c.args[0] for c in log.call_args_list if "NBS hourly cycle" in c.args[0]]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("! NBS hourly cycle 2026-10-15 13Z: 0 of 1 station(s)"))
        self.assertIn("has NOAA changed the bulletin's format?", said[0])
        self.assertEqual(m["src"], "S+E")
        # a cycle that reads is not flagged
        os.remove(self.path)
        with mock.patch.object(r, "_log") as log:
            self._write()
        said = [c.args[0] for c in log.call_args_list if "NBS hourly cycle" in c.args[0]]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("NBS hourly cycle 2026-10-15 13Z: 1 of 1 station(s)"))


class TestLongLeads(unittest.TestCase):
    """Jack 2026-11-14: "KXSNOWDAYS-01DEC26-01APR27, KXRAINTHANKSGIVING-26NOV26
    (and rain/snow markets generally) should have fair value calculations and
    auto-quoted when they come online"."""

    TURKEY = ("If the total precipitation at New York City (KNYC) in New York City on "
              "November 26, 2026 is strictly greater than 0 inches, then the market "
              "resolves to Yes.")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rain_wknd_fair.json")
        day = ["2026-11-26", "2026-11-26"]
        self.events = {"KXRAINTHANKSGIVING-26NOV26": {"sat": "2026-11-26", "days": 1, "markets": {
            "KXRAINTHANKSGIVING-26NOV26-NYC": {"code": "NYC", "span": day},
            "KXRAINTHANKSGIVING-26NOV26-SEA": {"code": "SEA", "span": day}}}}
        self.clim_calls = []

    def clim(self, sid, first, days):
        self.clim_calls.append((sid, first, days))
        if sid == "KSEA":
            raise RuntimeError("ACIS KSEA: under 150 complete windows")
        return 0.33, 450

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_thanksgivings_on_wording_names_its_day(self):
        # the 11/13 listing said "on November 26, 2026 is"; the parser knew
        # only Election Day's "in November 3, 2026 is", so all 48 cities were
        # rejected ("its rules name no station or no day")
        self.assertEqual(r.rules_span(self.TURKEY), (date(2026, 11, 26), date(2026, 11, 26)))
        got, why = r.classify_day_markets([{"ticker": "KXRAINTHANKSGIVING-26NOV26-NYC",
                                            "event_ticker": "KXRAINTHANKSGIVING-26NOV26",
                                            "rules_primary": self.TURKEY}])
        self.assertEqual((got["events"], why), ({"KXRAINTHANKSGIVING-26NOV26": ["2026-11-26", 1]},
                                                ""))

    def test_an_older_classifiers_rejection_is_read_again_at_once(self):
        # the live file rejected Thanksgiving at 16:57Z 11/14 with the old
        # parser; its 6-hour retry would have run past the 01:25Z start
        now = datetime(2026, 11, 14, 22, tzinfo=UTC).timestamp()
        old = {"day_discovered_at": now - 600, "day_series": {},
               "day_rejected": {"KXRAINTHANKSGIVING": {
                   "at": now - 3600, "why": "KXRAINTHANKSGIVING-26NOV26-TTN: its rules "
                                            "name no station or no day"}}}
        calls = []

        def get_json(path, params):
            calls.append(path)
            if path == "/series":
                return {"series": [{"ticker": "KXRAINTHANKSGIVING"}]}
            return {"markets": [{"ticker": "KXRAINTHANKSGIVING-26NOV26-NYC",
                                 "event_ticker": "KXRAINTHANKSGIVING-26NOV26",
                                 "rules_primary": self.TURKEY}]}
        found, rejected, at = r.discover_day_series(old, now, get_json)
        self.assertEqual(list(found), ["KXRAINTHANKSGIVING"])
        self.assertEqual((rejected, at), ({}, now))
        # a current verdict waits out the hour as before
        calls.clear()
        again = {"day_discovered_at": now, "day_series": found,
                 "day_rejected": {"KXRAINX": {"at": now, "why": "no open market",
                                              "v": r.DAY_CLASSIFY_VERSION}}}
        self.assertEqual(r.discover_day_series(again, now + 60, get_json)[2], now)
        self.assertEqual(calls, [])

    HNL = ("If the total precipitation at Honolulu (PHNL) in Honolulu on November 26, "
           "2026 is strictly greater than 0 inches, then the market resolves to Yes.")
    # a code no reader here knows (a station outside the US)
    ABROAD = ("If the total precipitation at London (EGLL) in London on November 26, "
              "2026 is strictly greater than 0 inches, then the market resolves to Yes.")

    def _mk(self, code, rules):
        return {"ticker": f"KXRAINTHANKSGIVING-26NOV26-{code}",
                "event_ticker": "KXRAINTHANKSGIVING-26NOV26", "rules_primary": rules}

    def test_a_city_with_no_readable_station_stands_aside_alone(self):
        # 2026-11-22: Kalshi added HNL / ANC ("(PHNL)", "(PANC)") on 11/21 and
        # the first city station_code couldn't read rejected all 51 cities.
        # P codes read since version 5; a foreign code stands in for them here
        self.assertIsNone(r.station_code(self.ABROAD))
        got, why = r.classify_day_markets([self._mk("LON", self.ABROAD),
                                           self._mk("NYC", self.TURKEY)])
        self.assertEqual(why, "")
        self.assertEqual(got["events"], {"KXRAINTHANKSGIVING-26NOV26": ["2026-11-26", 1]})
        self.assertEqual(got["sample"], "KXRAINTHANKSGIVING-26NOV26-NYC")
        self.assertEqual(list(got["skipped"]), ["KXRAINTHANKSGIVING-26NOV26-LON"])
        # no skip, no key: the rows the file already holds read the same
        got, _ = r.classify_day_markets([self._mk("NYC", self.TURKEY)])
        self.assertNotIn("skipped", got)
        # and Honolulu is a city like any other now
        got, _ = r.classify_day_markets([self._mk("HNL", self.HNL),
                                         self._mk("NYC", self.TURKEY)])
        self.assertEqual(got["sample"], "KXRAINTHANKSGIVING-26NOV26-HNL")
        self.assertNotIn("skipped", got)

    def test_no_city_readable_still_rejects_the_series(self):
        got, why = r.classify_day_markets([self._mk("LON", self.ABROAD)])
        self.assertIsNone(got)
        self.assertEqual(why, "KXRAINTHANKSGIVING-26NOV26-LON: its rules name no "
                              "station or no day")
        # a city with no day is still the series' problem, not the city's
        noday = self.TURKEY.replace(" on November 26, 2026 is", " is")
        got, why = r.classify_day_markets([self._mk("NYC", self.TURKEY),
                                           self._mk("SEA", noday)])
        self.assertIsNone(got)
        self.assertIn("SEA: its rules name no station or no day", why)

    def test_the_hnl_rejection_is_read_again_and_the_series_rejoins(self):
        # the live file on 11/22: rejected at 13:13Z by version 3. Since
        # version 5 Honolulu itself reads, and is priced on its own station
        # and its own day (Jack 2026-11-22: "Add")
        now = datetime(2026, 11, 22, 16, tzinfo=UTC).timestamp()
        old = {"day_discovered_at": now - 600, "day_series": {},
               "day_rejected": {"KXRAINTHANKSGIVING": {
                   "at": now - 3 * 3600, "v": 3,
                   "why": "KXRAINTHANKSGIVING-26NOV26-HNL: its rules name no station "
                          "or no day"}}}

        def get_json(path, params):
            if path == "/series":
                return {"series": [{"ticker": "KXRAINTHANKSGIVING"}]}
            return {"markets": [self._mk("HNL", self.HNL), self._mk("NYC", self.TURKEY)]}
        found, rejected, at = r.discover_day_series(old, now, get_json)
        self.assertEqual((list(found), rejected, at), (["KXRAINTHANKSGIVING"], {}, now))
        self.assertNotIn("skipped", found["KXRAINTHANKSGIVING"])
        # and its markets: both priced, Honolulu on PHNL's day (UTC-10)
        events = r.fetch_events(get_json=lambda path, params: {"markets": [
            self._mk("HNL", self.HNL), self._mk("NYC", self.TURKEY)]}, day_series=found)
        mk = events["KXRAINTHANKSGIVING-26NOV26"]["markets"]
        self.assertEqual((mk["KXRAINTHANKSGIVING-26NOV26-HNL"]["code"],
                          mk["KXRAINTHANKSGIVING-26NOV26-NYC"]["code"]), ("PHNL", "NYC"))
        run = datetime(2026, 11, 22, 12, tzinfo=UTC)
        resolved = []

        def resolve(code, now):
            resolved.append(code)
            return {"icao": code, "lst": 10, "tz": "Pacific/Honolulu", "name": "Honolulu",
                    "auto": True, "checked_at": now.isoformat()}

        def down(*a, **k):
            raise RuntimeError("offline in the test")
        ok, miss = r.write_fair_file(self.path, now=datetime(2026, 11, 22, 16, tzinfo=UTC),
                                     events=events,
                                     nbe_fn=lambda sid: (run, periods(run, [0.4] * 21)),
                                     resolve_fn=resolve,
                                     nbs_fn=down, nbs_hourly_fn=down, clim_fn=self.clim)
        d = self.read()
        self.assertEqual((ok, miss, resolved), (2, 0, ["PHNL"]))
        self.assertEqual(d["auto_stations"]["PHNL"]["lst"], 10)
        hnl = d["markets"]["KXRAINTHANKSGIVING-26NOV26-HNL"]
        one = r.market_fair(periods(run, [0.4] * 21), run, date(2026, 11, 26), 10, days=1)
        self.assertEqual((hnl["station"], hnl["p"]), ("PHNL", one["p"]))
        self.assertIn("KXRAINTHANKSGIVING-26NOV26-NYC", d["markets"])
        self.assertNotIn("KXRAINTHANKSGIVING-26NOV26-HNL", d.get("missing") or {})

    def test_an_older_classifiers_acceptance_is_read_again_at_once(self):
        # the live file at 20:00Z 11/22: Thanksgiving accepted by version 4 at
        # 16:17Z, its "skipped" naming Honolulu (rows before version 5 carry
        # no "v"); waiting out the 6 hours would have left the note stale
        now = datetime(2026, 11, 22, 21, tzinfo=UTC).timestamp()
        row = {"events": {"KXRAINTHANKSGIVING-26NOV26": ["2026-11-26", 1]},
               "sample": "KXRAINTHANKSGIVING-26NOV26-NYC", "at": now - 3600,
               "skipped": {"KXRAINTHANKSGIVING-26NOV26-HNL":
                           "its rules name no station this writer reads"}}
        old = {"day_discovered_at": now - 600, "day_series": {"KXRAINTHANKSGIVING": row},
               "day_rejected": {}}
        calls = []

        def get_json(path, params):
            calls.append(path)
            if path == "/series":
                return {"series": [{"ticker": "KXRAINTHANKSGIVING"}]}
            return {"markets": [self._mk("HNL", self.HNL), self._mk("NYC", self.TURKEY)]}
        found, rejected, at = r.discover_day_series(old, now, get_json)
        self.assertEqual((calls, at), (["/series", "/markets"], now))
        got = found["KXRAINTHANKSGIVING"]
        self.assertNotIn("skipped", got)
        self.assertEqual((got["v"], got["at"]), (r.DAY_CLASSIFY_VERSION, now))
        # read once: the current row waits out its hour, then its 6 hours
        calls.clear()
        again = {"day_discovered_at": at, "day_series": found, "day_rejected": rejected}
        self.assertEqual(r.discover_day_series(again, now + 60, get_json)[0], found)
        self.assertEqual(calls, [])
        r.discover_day_series(again, now + r.DAY_DISCOVER_SECS + 1, get_json)
        self.assertEqual(calls, ["/series"])

    def test_climatology_counts_wet_windows_and_drops_missing_ones(self):
        daily = {}
        for y in range(1996, 2026):
            for k in range(-7, 8):
                d = date(y, 11, 26) + timedelta(days=k)
                daily[d.isoformat()] = 0.2 if k == 0 else (0.0 if k % 2 else 0.0)
        p, n = r.climatology(daily, date(2026, 11, 26), 1)
        self.assertEqual(n, 450)
        self.assertAlmostEqual(p, 30 / 450)
        # a missing day drops its window; under CLIM_MIN_WINDOWS -> None
        for y in range(1996, 2026):
            for k in range(-7, 4):                 # 4 windows a year left: 120
                daily[(date(y, 11, 26) + timedelta(days=k)).isoformat()] = None
        self.assertIsNone(r.climatology(daily, date(2026, 11, 26), 1))
        self.assertEqual(r.parse_pcpn("T"), 0.0)
        self.assertIsNone(r.parse_pcpn("M"))
        self.assertIsNone(r.parse_pcpn("0.50A"))
        self.assertEqual(r.parse_pcpn("0.01"), 0.01)

    def test_past_the_fit_the_fair_is_the_climatology(self):
        # 11/15 01:30Z, the program's start: NYC's day starts 11/26 05:00Z,
        # ~267 hours out -- past the NBE's 264, so no model weight
        now = datetime(2026, 11, 15, 1, 30, tzinfo=UTC)
        run = datetime(2026, 11, 14, 12, tzinfo=UTC)
        nbe = lambda sid: (run, periods(run, [0.6] * 21))
        ok, miss = r.write_fair_file(self.path, now=now, events=self.events, nbe_fn=nbe,
                                     clim_fn=self.clim)
        self.assertEqual((ok, miss), (1, 1))
        d = self.read()
        m = d["markets"]["KXRAINTHANKSGIVING-26NOV26-NYC"]
        self.assertEqual((m["p"], m["src"], m["w_model"], m["clim"]), (0.33, "C", 0.0, 0.33))
        self.assertIn("no climatology", d["missing"]["KXRAINTHANKSGIVING-26NOV26-SEA"])
        self.assertIn("ACIS KSEA", d["missing"]["KXRAINTHANKSGIVING-26NOV26-SEA"])
        # read once: the next pass reuses the file's (and retries the failure
        # only after AUTO_RETRY_SECS)
        r.write_fair_file(self.path, now=now + timedelta(minutes=15), events=self.events,
                          nbe_fn=nbe, clim_fn=self.clim)
        self.assertEqual(len(self.clim_calls), 2)

    def test_the_nbm_blends_in_toward_the_fits_edge(self):
        # lead 204h from the run (+3h lag): weight (264 - 204) / 120 = 0.5
        run = datetime(2026, 11, 17, 14, tzinfo=UTC)
        now = run + timedelta(hours=4)
        nbe = lambda sid: (run, periods(run, [0.6] * 21))
        r.write_fair_file(self.path, now=now, events=self.events, nbe_fn=nbe, clim_fn=self.clim)
        m = self.read()["markets"]["KXRAINTHANKSGIVING-26NOV26-NYC"]
        self.assertEqual((m["lead_h"], m["w_model"], m["src"]), (204.0, 0.5, "E+C"))
        self.assertAlmostEqual(m["p"], round(0.5 * m["p_model"] + 0.5 * 0.33, 4), places=4)
        # inside the fit the model alone, as before
        run2 = datetime(2026, 11, 21, 12, tzinfo=UTC)
        r.write_fair_file(self.path, now=run2 + timedelta(hours=4), events=self.events,
                          nbe_fn=lambda sid: (run2, periods(run2, [0.6] * 21)),
                          clim_fn=self.clim)
        m = self.read()["markets"]["KXRAINTHANKSGIVING-26NOV26-NYC"]
        self.assertNotIn("clim", m)
        self.assertEqual(m["src"], "E")

    def test_the_switch_leaves_the_far_day_without_a_fair(self):
        now = datetime(2026, 11, 15, 1, 30, tzinfo=UTC)
        run = datetime(2026, 11, 14, 12, tzinfo=UTC)
        with mock.patch.object(r, "CLIM_ENABLE", False):
            ok, _ = r.write_fair_file(self.path, now=now, events=self.events,
                                      nbe_fn=lambda sid: (run, periods(run, [0.6] * 21)),
                                      clim_fn=self.clim)
        self.assertEqual(ok, 0)
        self.assertEqual(self.clim_calls, [])
        self.assertIn("lead past the fit",
                      self.read()["missing"]["KXRAINTHANKSGIVING-26NOV26-NYC"])


def snow_periods(run: datetime, psn, p12=0.4, tmp=30.0):
    """12-hour NBE periods ending at 00Z/12Z after `run`: psn in 0..1."""
    return [(run + timedelta(hours=12 * (i + 1)),
             {"p12": p12, "psn": p, "tmp": tmp, "txn": tmp + 2}) for i, p in enumerate(psn)]


class TestSnowDays(unittest.TestCase):
    """Jack 2026-11-20: "KXSNOWTHANKSGIVING should quote" (11/14: "rain/snow
    markets generally should have fair value calculations and auto-quoted
    when they come online")."""

    SNOW = ("If the total new snowfall in {name} (K{code}) on November 26, 2026 is "
            "strictly greater than 0 inches, including a reported trace, then the market "
            "resolves to Yes.")
    RAIN = TestLongLeads.TURKEY

    def market(self, code, rules=None, series="KXSNOWTHANKSGIVING"):
        return {"ticker": f"{series}-26NOV26-{code}", "event_ticker": f"{series}-26NOV26",
                "rules_primary": rules or self.SNOW.format(name=code, code=code)}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rain_wknd_fair.json")
        self.snow_calls, self.clim_calls = [], []

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_snow_contract_is_a_day_series_of_kind_snow(self):
        got, why = r.classify_day_markets([self.market("DTW"), self.market("NYC")])
        self.assertEqual((got, why), ({"events": {"KXSNOWTHANKSGIVING-26NOV26": ["2026-11-26", 1]},
                                       "sample": "KXSNOWTHANKSGIVING-26NOV26-DTW",
                                       "kind": "snow"}, ""))
        self.assertEqual(r.station_code(self.SNOW.format(name="Detroit", code="DTW")), "DTW")
        # a trace that does not count is another outcome than the model's; a
        # threshold; rain and snow mixed
        no_trace = self.SNOW.format(name="DTW", code="DTW").replace(
            ", including a reported trace,", ", a trace counting as 0,")
        for ms, bit in (([self.market("DTW", no_trace)], "trace does not count"),
                        ([self.market("DTW", self.SNOW.format(name="DTW", code="DTW").replace(
                            "than 0 inches", "than 1 inches"))], "total precipitation"),
                        ([self.market("DTW"), self.market("NYC", self.RAIN)], "rain and snow")):
            got, why = r.classify_day_markets(ms)
            self.assertIsNone(got)
            self.assertIn(bit, why)
        with mock.patch.object(r, "SNOW_DAY_ENABLE", False):
            self.assertIsNone(r.classify_day_markets([self.market("DTW")])[0])

    def test_discovery_reads_unclaimed_snow_series_and_events_carry_the_kind(self):
        calls = []

        def get_json(path, params):
            calls.append(params.get("series_ticker") or params.get("category"))
            if path == "/series":
                return {"series": [{"ticker": s} for s in
                                   ("KXSNOWTHANKSGIVING", "KXSNOWDAYS", "KXCHISNOWM")]}
            if params["series_ticker"] == "KXSNOWTHANKSGIVING":
                return {"markets": [self.market("DTW"), self.market("SEA")]}
            return {"markets": []}
        skip = lambda s: s in ("KXSNOWDAYS", "KXCHISNOWM")       # the bot's snow families
        ts = datetime(2026, 11, 20, 13, tzinfo=UTC).timestamp()
        found, _rej, _at = r.discover_day_series({}, ts, get_json, skip)
        self.assertEqual(list(found), ["KXSNOWTHANKSGIVING"])
        self.assertEqual(found["KXSNOWTHANKSGIVING"]["kind"], "snow")
        self.assertEqual(calls, ["Climate and Weather", "KXSNOWTHANKSGIVING"])
        ev = r.fetch_events(get_json, found)
        self.assertEqual(ev["KXSNOWTHANKSGIVING-26NOV26"]["kind"], "snow")
        self.assertEqual(ev["KXSNOWTHANKSGIVING-26NOV26"]["sat"], "2026-11-26")
        with mock.patch.object(r, "SNOW_DAY_ENABLE", False):
            calls.clear()
            self.assertEqual(r.discover_day_series({}, ts, get_json, skip)[0], {})
            self.assertEqual(calls, ["Climate and Weather"])

    def snow_nbe(self, run, psn):
        def fn(sid):
            self.snow_calls.append(sid)
            return run, snow_periods(run, psn)
        return fn

    def clim(self, sid, first, days):
        self.clim_calls.append((sid, first, days))
        if sid == "KSEA":
            raise RuntimeError("ACIS KSEA: under 150 complete snow windows")
        return 0.2, 450

    def events(self):
        day = ["2026-11-26", "2026-11-26"]
        return {
            "KXSNOWTHANKSGIVING-26NOV26": {"sat": "2026-11-26", "days": 1, "kind": "snow",
                                           "markets": {
                "KXSNOWTHANKSGIVING-26NOV26-DTW": {"code": "DTW", "span": day},
                "KXSNOWTHANKSGIVING-26NOV26-SEA": {"code": "SEA", "span": day}}},
            "KXRAINTHANKSGIVING-26NOV26": {"sat": "2026-11-26", "days": 1, "markets": {
                "KXRAINTHANKSGIVING-26NOV26-NYC": {"code": "NYC", "span": day}}}}

    def test_a_snow_market_prices_on_the_snow_model_and_fails_closed(self):
        run = datetime(2026, 11, 22, 12, tzinfo=UTC)
        now = run + timedelta(hours=4)
        rain_calls = []

        def rain_nbe(sid):
            rain_calls.append(sid)
            return run, periods(run, [0.3] * 20)
        ok, miss = r.write_fair_file(self.path, now=now, events=self.events(), nbe_fn=rain_nbe,
                                     snow_nbe_fn=self.snow_nbe(run, [0.3] * 20),
                                     snow_clim_fn=self.clim)
        self.assertEqual((ok, miss), (2, 1))
        d = self.read()
        m = d["markets"]["KXSNOWTHANKSGIVING-26NOV26-DTW"]
        want = r.snow_market_fair(snow_periods(run, [0.3] * 20), run, date(2026, 11, 26), 5, 0.2)
        self.assertEqual((m["p"], m["kind"], m["station"], m["src"], m["clim"], m["coverage"]),
                         (want["p"], "snow", "KDTW", "E", 0.2, 2.0))
        # no snow climatology: no fair (a model input at every lead)
        self.assertIn("no snow climatology", d["missing"]["KXSNOWTHANKSGIVING-26NOV26-SEA"])
        # the rain market priced as before, and each source read for its own
        # family only
        self.assertIn("KXRAINTHANKSGIVING-26NOV26-NYC", d["markets"])
        self.assertNotIn("kind", d["markets"]["KXRAINTHANKSGIVING-26NOV26-NYC"])
        self.assertEqual((rain_calls, sorted(self.snow_calls)), (["KNYC"], ["KDTW", "KSEA"]))
        # the next pass inside the run's 13 hours reads neither again
        r.write_fair_file(self.path, now=now + timedelta(minutes=15), events=self.events(),
                          nbe_fn=rain_nbe, snow_nbe_fn=self.snow_nbe(run, [0.3] * 20),
                          snow_clim_fn=self.clim)
        self.assertEqual((len(self.snow_calls), len(self.clim_calls)), (2, 2))
        # out once the day starts: Detroit at 05:00Z
        r.write_fair_file(self.path, now=datetime(2026, 11, 26, 5, 1, tzinfo=UTC),
                          events=self.events(), nbe_fn=rain_nbe,
                          snow_nbe_fn=self.snow_nbe(run, [0.3] * 20), snow_clim_fn=self.clim)
        self.assertEqual(self.read()["missing"]["KXSNOWTHANKSGIVING-26NOV26-DTW"],
                         "the day started")

    def test_more_snow_in_the_guidance_is_a_dearer_fair(self):
        run = datetime(2026, 11, 22, 12, tzinfo=UTC)
        lo = r.snow_market_fair(snow_periods(run, [0.02] * 20), run, date(2026, 11, 26), 5, 0.2)
        hi = r.snow_market_fair(snow_periods(run, [0.6] * 20), run, date(2026, 11, 26), 5, 0.2)
        self.assertLess(lo["p"], hi["p"])
        self.assertEqual((lo["coverage"], lo["lead_h"]), (2.0, 86.0))
        # a period without its snow probability is no fair; nor a stale lead
        gap = snow_periods(run, [0.3] * 20)
        gap[7][1]["psn"] = None
        self.assertIsNone(r.snow_market_fair(gap, run, date(2026, 11, 26), 5, 0.2))
        self.assertIsNone(r.snow_market_fair(snow_periods(run, [0.3] * 30), run,
                                             date(2026, 12, 20), 5, 0.2))

    def test_snow_values_and_climatology_count_a_trace(self):
        self.assertEqual([r.snow_day_value(v) for v in ("T", "0.0", "1.2", "M", "S")],
                         [1, 0, 1, None, None])
        daily = {}
        for y in range(1996, 2026):
            for k in range(-7, 8):
                d = date(y, 11, 26) + timedelta(days=k)
                daily[d.isoformat()] = 1 if k in (0, 3) else 0
        p, n = r.snow_climatology(daily, date(2026, 11, 26), 1)
        self.assertEqual((n, round(p, 4)), (450, round(60 / 450, 4)))
        payload = {"data": [{"runtime": "2026-11-20 00:00", "ftime": "2026-11-26 12:00",
                             "p12": 38, "psn": 12, "tmp": 33, "txn": 33},
                            {"runtime": "2026-11-19 12:00", "ftime": "2026-11-26 12:00",
                             "p12": 50, "psn": 50, "tmp": 30, "txn": 30}]}
        rt, per = r.parse_nbe_snow(payload)
        self.assertEqual(rt, datetime(2026, 11, 20, tzinfo=UTC))
        self.assertEqual(per, [(datetime(2026, 11, 26, 12, tzinfo=UTC),
                                {"p12": 0.38, "psn": 0.12, "tmp": 33.0, "txn": 33.0})])


if __name__ == "__main__":
    unittest.main()
