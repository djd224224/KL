"""Tests for snow_days_fair (the KXSNOWDAYS fair-value writer, 2026-11-14:
Jack "KXSNOWDAYS-01DEC26-01APR27, KXRAINTHANKSGIVING-26NOV26 (and rain/snow
markets generally) should have fair value calculations and auto-quoted when
they come online")."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import snow_days_fair as s

UTC = timezone.utc
ATL_RULES = (
    "If Each Yes contract pays according to a Directional linear payout from $0.00 at 0 "
    "qualifying days to $1.00 at 4 days, capped at $0.00–$1.00 and rounded down once "
    "to $0.01 based on the number of days between December 1, 2026 and April 1, 2027 with "
    "at least 0.1 inches of official daily snowfall in Atlanta (Hartsfield-Jackson Atlanta "
    "International Airport, KATL). Each No contract pays $1.00 minus the Yes payout. Then "
    "the market resolves to Yes.")
SEC = ("Days run midnight–midnight in EST (UTC-05:00), fixed standard time; dates are "
       "inclusive. Trace snow counts as zero.")


def market(city="ATL", floor=0, cap=4, rules=ATL_RULES, sec=SEC):
    return {"ticker": f"KXSNOWDAYS-01DEC26-01APR27-{city}",
            "event_ticker": "KXSNOWDAYS-01DEC26-01APR27",
            "rules_primary": rules.replace("at 0 qualifying", f"at {floor} qualifying")
                                  .replace("at 4 days", f"at {cap} days"),
            "rules_secondary": sec,
            "custom_strike": {"scalar_floor": str(floor), "scalar_cap": str(cap),
                              "statistic": "Qualifying snow days"}}


def history(counts_by_year, thr=0.1, missing=()):
    """ACIS-shaped daily snowfall: `n` snowy days (at `thr`) from each Dec 1."""
    daily = {}
    for y, n in counts_by_year.items():
        d0 = date(y, 12, 1)
        for k in range((date(y + 1, 4, 1) - d0).days + 1):
            d = d0 + timedelta(days=k)
            daily[d.isoformat()] = thr if k < n else ("T" if k % 3 else "0.0")
    for d in missing:
        daily[d] = "M"
    return {k: s.parse_snow(v) for k, v in daily.items()}


def setUpModule():
    global _NET
    _NET = [mock.patch.object(s.requests, "get", side_effect=RuntimeError("no network")),
            mock.patch.object(s.requests, "post", side_effect=RuntimeError("no network"))]
    for p in _NET:
        p.start()


def tearDownModule():
    for p in _NET:
        p.stop()


class TestContract(unittest.TestCase):
    def test_reads_the_scalar_from_its_rules(self):
        spec, why = s.parse_market(market())
        self.assertEqual(why, "")
        self.assertEqual(spec, {"floor": 0.0, "cap": 4.0, "y0": 0.0, "y1": 1.0,
                                "start": "2026-12-01", "end": "2027-04-01", "thr": 0.1,
                                "station": "KATL", "city": "Atlanta", "utc_off": -5.0})
        # a 1-inch city, Chicago's range, Central time
        chi = market("CHI", 6, 16, ATL_RULES.replace("0.1 inches", "1 inch")
                     .replace("Atlanta (Hartsfield-Jackson Atlanta International Airport, KATL)",
                              "Chicago (Chicago O'Hare International Airport, KORD)"),
                     SEC.replace("EST (UTC-05:00)", "CST (UTC-06:00)"))
        spec, _ = s.parse_market(chi)
        self.assertEqual((spec["thr"], spec["station"], spec["floor"], spec["cap"],
                          spec["utc_off"]), (1.0, "KORD", 6.0, 16.0, -6.0))

    def test_anything_else_is_not_the_contract(self):
        for m, bit in ((dict(market(), rules_primary="Will it snow in Atlanta?"), "scalar"),
                       (dict(market(), custom_strike={"scalar_floor": "1",
                                                      "scalar_cap": "4"}), "custom strike"),
                       (dict(market(), rules_secondary="Days run whenever."), "midnight"),
                       (market(rules=ATL_RULES.replace(", KATL", "")), "K-station")):
            spec, why = s.parse_market(m)
            self.assertIsNone(spec)
            self.assertIn(bit, why)
        self.assertEqual(s.classify([])[1], "no open market")
        self.assertEqual(s.classify([market()])[0], {"sample": market()["ticker"]})

    def test_payout_is_linear_capped_and_rounded_down(self):
        spec = s.parse_market(market(floor=11, cap=31))[0]
        self.assertEqual([s.payout(spec, n) for n in (0, 11, 12, 21, 30, 31, 40)],
                         [0.0, 0.0, 0.05, 0.5, 0.95, 1.0, 1.0])
        spec = s.parse_market(market(floor=0, cap=3))[0]
        self.assertEqual(s.payout(spec, 1), 0.33)              # 0.333 rounds DOWN

    def test_season_counts_drop_a_season_missing_more_than_three_days(self):
        h = history({2023: 2, 2024: 5}, missing=["2024-12-25", "2025-01-02", "2025-01-03",
                                                 "2025-02-01"])
        got = s.season_counts(h, date(2026, 12, 1), date(2027, 4, 1), 0.1, seasons=3)
        self.assertEqual(got, [(2023, 2)])     # 2025 is not in the record, 2024 drops
        # a trace is no snow; a day under the threshold doesn't count
        h = history({2025: 3}, thr=0.5)
        self.assertEqual(s.season_counts(h, date(2026, 12, 1), date(2027, 4, 1), 1.0,
                                         seasons=1), [(2025, 0)])

    def test_fair_is_the_mean_payout(self):
        spec = s.parse_market(market())[0]
        counts = [(y, n) for y, n in zip(range(2025, 1985, -1), [0, 1, 2, 4, 8] * 8)]
        f = s.fair_from_counts(spec, counts)
        self.assertEqual(f["p"], round((0 + 0.25 + 0.5 + 1 + 1) / 5, 4))
        self.assertEqual((f["seasons"], f["n_med"]), (40, 2))

    def test_the_fair_reads_the_last_ten_calendar_seasons(self):
        """Jack 2026-11-15 "Yes, last 10": a dropped season is not replaced
        by an older one."""
        counts = [(y, 9) for y in range(2025, 2015, -1)] + [(y, 0) for y in range(2015, 1985, -1)]
        got = s.recent_counts(counts, date(2026, 12, 1))
        self.assertEqual([y for y, _n in got], list(range(2025, 2015, -1)))
        gap = [c for c in counts if c[0] != 2024]
        self.assertEqual(len(s.recent_counts(gap, date(2026, 12, 1))), 9)


class TestWrite(unittest.TestCase):
    NOW = datetime(2026, 11, 15, 1, 30, tzinfo=UTC)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "snow_days_fair.json")
        self.calls = []

    def fetch(self, sid, start, end):
        self.calls.append(sid)
        if sid == "KHOU":
            return history({y: 0 for y in range(2010, 2026)})     # 16 seasons: short
        if sid == "IAHthr":
            return history({y: (1 if y % 10 == 0 else 0) for y in range(1980, 2026)})
        return history({y: y % 5 for y in range(1980, 2026)})

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_prices_each_city_and_reads_history_once(self):
        hou = market("HOU", 0, 2, ATL_RULES.replace(
            "Atlanta (Hartsfield-Jackson Atlanta International Airport, KATL)",
            "Houston (Houston William P. Hobby Airport, KHOU)"),
            SEC.replace("EST (UTC-05:00)", "CST (UTC-06:00)"))
        bad = dict(market("XXX"), rules_primary="Will it snow?")
        ok, miss = s.write_fair_file(self.path, now=self.NOW,
                                     markets_in={"KXSNOWDAYS": [market(), hou, bad]},
                                     fetch=self.fetch)
        self.assertEqual((ok, miss), (2, 1))
        d = self.read()
        atl = d["markets"]["KXSNOWDAYS-01DEC26-01APR27-ATL"]
        self.assertEqual((atl["sid"], atl["seasons"], atl["seasons_all"], atl["event"],
                          atl["fetched_at"]),
                         ("KATL", 10, 40, "KXSNOWDAYS-01DEC26-01APR27", self.NOW.isoformat()))
        # counts 0..4 in turn on a 0-4 range: mean payout (0+.25+.5+.75+1)/5
        self.assertEqual((atl["p"], atl["p_all"]), (0.5, 0.5))
        # Houston's Hobby record is short: the Houston Area record stands in
        hou_f = d["markets"]["KXSNOWDAYS-01DEC26-01APR27-HOU"]
        self.assertEqual((hou_f["sid"], hou_f["p"]), ("IAHthr", 0.05))
        self.assertIn("scalar", d["missing"]["KXSNOWDAYS-01DEC26-01APR27-XXX"])
        n = len(self.calls)
        s.write_fair_file(self.path, now=self.NOW + timedelta(minutes=15),
                          markets_in={"KXSNOWDAYS": [market(), hou]}, fetch=self.fetch)
        self.assertEqual(len(self.calls), n)                  # the file's history

    def test_no_fair_once_the_season_starts_or_without_history(self):
        start = datetime(2026, 12, 1, 5, tzinfo=UTC)        # 00:00 EST Dec 1
        s.write_fair_file(self.path, now=start - timedelta(minutes=1),
                          markets_in={"KXSNOWDAYS": [market()]}, fetch=self.fetch)
        self.assertIn("KXSNOWDAYS-01DEC26-01APR27-ATL", self.read()["markets"])
        s.write_fair_file(self.path, now=start, markets_in={"KXSNOWDAYS": [market()]},
                          fetch=self.fetch)
        d = self.read()
        self.assertEqual(d["markets"], {})
        self.assertIn("season started", d["missing"]["KXSNOWDAYS-01DEC26-01APR27-ATL"])
        os.remove(self.path)
        s.write_fair_file(self.path, now=self.NOW, markets_in={"KXSNOWDAYS": [market()]},
                          fetch=lambda sid, a, b: {})
        self.assertIn("no history", self.read()["missing"]["KXSNOWDAYS-01DEC26-01APR27-ATL"])

    def test_a_snowy_past_does_not_lift_a_thin_decade(self):
        """The fair follows the last 10 seasons; under 8 of them complete -> no fair."""
        def fetch(sid, start, end):
            return history({y: (0 if y >= 2016 else 4) for y in range(1980, 2026)})
        s.write_fair_file(self.path, now=self.NOW, markets_in={"KXSNOWDAYS": [market()]},
                          fetch=fetch)
        atl = self.read()["markets"]["KXSNOWDAYS-01DEC26-01APR27-ATL"]
        self.assertEqual((atl["p"], atl["seasons"]), (0.0, 10))
        self.assertEqual(atl["p_all"], 0.75)               # 30 of 40 seasons at the cap
        os.remove(self.path)

        def gappy(sid, start, end):
            # 2016-2018 missing whole winters: 7 of the last 10 complete
            return history({y: 1 for y in range(1980, 2026) if y not in (2016, 2017, 2018)})
        s.write_fair_file(self.path, now=self.NOW, markets_in={"KXSNOWDAYS": [market()]},
                          fetch=gappy)
        d = self.read()
        self.assertEqual(d["markets"], {})
        self.assertIn("7 of the last 10 seasons complete (need 8)",
                      d["missing"]["KXSNOWDAYS-01DEC26-01APR27-ATL"])

    def test_discovery_enrolls_a_snow_series_on_this_contract_only(self):
        calls = []

        def get_json(path, params):
            calls.append((path, params.get("series_ticker")))
            if path == "/series":
                return {"series": [{"ticker": t} for t in (
                    "KXSNOWDAYS", "KXSNOWDAYSB", "KXCHISNOWM", "KXBEAVERSNOW",
                    "KXSNOWCITIES", "KXRAINNYC")]}
            if params.get("series_ticker") == "KXSNOWDAYSB":
                return {"markets": [market("BOS", 11, 31)]}
            if params.get("series_ticker") == "KXSNOWCITIES":
                return {"markets": [dict(market(), ticker="KXSNOWCITIES-26-NYC",
                                         rules_primary="Will NYC see more snow than Boston?")]}
            return {"markets": [market()]}
        ok, _miss = s.write_fair_file(self.path, now=self.NOW, get_json=get_json,
                                      fetch=self.fetch)
        d = self.read()
        self.assertEqual(sorted(d["series"]), ["KXSNOWDAYSB"])
        self.assertIn("KXSNOWCITIES", d["rejected"])
        # the other snow families' and the named series are never read here
        read = {t for p, t in calls if p == "/markets"}
        self.assertEqual(read, {"KXSNOWDAYS", "KXSNOWDAYSB", "KXSNOWCITIES"})
        self.assertEqual(ok, 2)


    def test_a_series_listed_before_its_markets_open_is_read_at_the_next_pass(self):
        # 2026-11-26 (KXSNOWCHRISTMAS): a series Kalshi lists before its
        # markets open is not held out for RETRY_SECS
        calls, opened = [], {"KXSNOWDAYSB": False}

        def get_json(path, params):
            calls.append((path, params.get("series_ticker")))
            if path == "/series":
                return {"series": [{"ticker": t} for t in ("KXSNOWDAYSB", "KXSNOWCITIES")]}
            if params.get("series_ticker") == "KXSNOWCITIES":
                return {"markets": [dict(market(), ticker="KXSNOWCITIES-26-NYC",
                                         rules_primary="Will NYC see more snow than Boston?")]}
            return {"markets": [market("BOS", 11, 31)] if opened["KXSNOWDAYSB"] else []}
        ts = self.NOW.timestamp()
        found, rej, at = s.discover({}, ts, get_json)
        self.assertEqual(found, {})
        self.assertEqual(rej["KXSNOWDAYSB"]["why"], s.NO_OPEN_MARKET)
        self.assertIn("KXSNOWCITIES", rej)
        opened["KXSNOWDAYSB"] = True
        calls.clear()
        old = {"series": found, "rejected": rej, "discovered_at": at}
        found, rej, _ = s.discover(old, ts + s.DISCOVER_SECS + 1, get_json)
        self.assertEqual(sorted(found), ["KXSNOWDAYSB"])
        self.assertNotIn("KXSNOWDAYSB", rej)
        # the real rejection waits RETRY_SECS as before
        self.assertEqual({t for p, t in calls if p == "/markets"}, {"KXSNOWDAYSB"})


if __name__ == "__main__":
    unittest.main()
