"""Tests for rain_wknd_fair (the KXRAINWKND fair-value writer, 2026-10-11)."""
import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

import rain_wknd_fair as r

UTC = timezone.utc
SAT = date(2026, 10, 17)
RULES = ("If the total precipitation at CLI{code} in Somewhere on any day within "
         "October 17, 2026 through October 18, 2026 is strictly greater than 0 "
         "inches, then the market resolves to Yes.")


def periods(run: datetime, ps):
    """12-hour periods ending at 00Z/12Z after `run`, p12 in 0..1."""
    return [(run + timedelta(hours=12 * (i + 1)), p) for i, p in enumerate(ps)]


class TestParsing(unittest.TestCase):
    def test_station_and_window_from_the_rules(self):
        self.assertEqual(r.station_code(RULES.format(code="DJT")), "DJT")
        self.assertEqual(r.station_code("... precipitation at CLL in College Station on"),
                         "CLL")
        self.assertIsNone(r.station_code("no station named here"))
        self.assertEqual(r.rules_span(RULES.format(code="NYC")),
                         (date(2026, 10, 17), date(2026, 10, 18)))
        self.assertIsNone(r.rules_span("within soon through later"))

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
        self.assertEqual(seen, [("/markets", "KXRAINWKND", "open")])
        self.assertEqual(list(ev), ["KXRAINWKND-26OCT17"])       # not a Saturday: out
        self.assertEqual(ev["KXRAINWKND-26OCT17"]["markets"]["KXRAINWKND-26OCT17-WPB"],
                         {"code": "DJT", "span": ["2026-10-17", "2026-10-18"]})

    def test_events_are_cached_for_an_hour_unless_none_are_open(self):
        n = []
        listed = {"markets": [{"ticker": "KXRAINWKND-26OCT17-NYC",
                               "event_ticker": "KXRAINWKND-26OCT17",
                               "rules_primary": RULES.format(code="NYC")}]}
        def get_json(path, params):
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


if __name__ == "__main__":
    unittest.main()
