"""zillow_home_fair: the rules parse, the first-print model, the market
entries and the Zillow file sync (Jack 2026-11-12: "can you build a fair
value for this?")."""
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

import zillow_home_fair as z

SEA = ("If the Zillow Home Value Index for single-family homes in Seattle, WA "
       "(Zillow region ID 16037) is above $882,500 at November 2026, then the "
       "market resolves to Yes.")
VEN = ("If the Zillow Home Value Index for single-family homes in Venice, Los "
       "Angeles, CA (Zillow region ID 21056) is above $2.1 million at November "
       "2026, then the market resolves to Yes.")
HOU = ("If the Zillow Home Value Index for single-family homes in Houston, TX "
       "(Zillow region ID 39051) is below $111,000 at February 2027, then the "
       "market resolves to Yes.")

MONTHS = [(2023 + (i // 12), i % 12 + 1) for i in range(45)]   # 2023-01 .. 2026-09


def _series(v0=800000.0, g=0.002, n=45):
    return [v0 * math.exp(g * i) for i in range(n)]


def _csv(path, rows, months=MONTHS):
    hdr = ["RegionID", "SizeRank", "RegionName", "RegionType", "StateName",
           "State", "Metro", "CountyName"] + [f"{y}-{m:02d}-28" for y, m in months]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(hdr) + "\n")
        for rid, name, kind, vals in rows:
            f.write(",".join([str(rid), "1", name, kind, "WA", "WA",
                              '"Seattle-Tacoma-Bellevue, WA"', "King County"]
                             + [f"{v:.4f}" if v else "" for v in vals]) + "\n")


class TestRules(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(z.parse_rules(SEA), {
            "type": "single-family homes", "rid": 16037, "op": "above",
            "value": 882500, "inat": "at", "year": 2026, "month": 11})
        self.assertEqual(z.parse_rules(VEN)["value"], 2100000)
        self.assertEqual(z.parse_rules(VEN)["rid"], 21056)
        h = z.parse_rules(HOU)
        self.assertEqual((h["op"], h["year"], h["month"]), ("below", 2027, 2))
        self.assertIsNone(z.parse_rules("Will it rain?"))
        self.assertEqual(z.HOME_TYPE_CUT["single-family homes"],
                         "uc_sfr_tier_0.33_0.67_sm_sa")


class TestModel(unittest.TestCase):
    def test_ar_on_steady_growth(self):
        f, sd = z.ar_forecast(_series(g=0.002), 2)
        self.assertAlmostEqual(f, 0.004, places=6)
        self.assertLess(sd, 1e-9)
        self.assertEqual(z.ar_forecast(_series(), 0), (0.0, 0.0))

    def test_first_print_adds_the_reanchors(self):
        vals = _series(g=0.002)[-37:]
        with mock.patch.multiple(z, REANCHOR_MEAN_PCT=-0.5,
                                 REANCHOR_COMMON_SD_PCT=0.5, SD_FLOOR_PCT=0.1), \
                mock.patch.dict(z.REANCHOR_IDIO_SD_PCT, {"City": 0.7}):
            mu, sd = z.first_print_dist(vals, 2, "City")
        self.assertAlmostEqual(mu, math.log(vals[-1]) + 0.004 - 0.010, places=6)
        self.assertAlmostEqual(sd, math.sqrt(2 * (0.007 ** 2 + 0.005 ** 2)), places=6)
        # the floor
        with mock.patch.multiple(z, REANCHOR_COMMON_SD_PCT=0.0, SD_FLOOR_PCT=0.5), \
                mock.patch.dict(z.REANCHOR_IDIO_SD_PCT, {"City": 0.0}):
            self.assertAlmostEqual(z.first_print_dist(vals, 1, "City")[1], 0.005)
        self.assertAlmostEqual(z.p_above(math.log(100.0), 0.01, 100.0), 0.5)
        self.assertGreater(z.p_above(math.log(100.0), 0.01, 99.0), 0.8)


class TestMarketEntry(unittest.TestCase):
    def region(self, n=45, last_value=None):
        vals = _series(n=n)
        if last_value:
            vals[-1] = last_value
        return {"months": MONTHS[:n], "values": vals}

    def test_entries(self):
        m = {"ticker": "KXSEAHOME-30NOV26-GT882500", "rules_primary": SEA,
             "floor_strike": 882500}
        e = z.market_entry(m, self.region(), "City")
        self.assertEqual((e["h"], e["last_month"], e["target"]), (2, "2026-09", "2026-11"))
        self.assertTrue(0.0 < e["p"] < 1.0)
        self.assertNotIn("err", e)
        # the strike must match the rules
        self.assertIn("err", z.market_entry(dict(m, floor_strike=880000), self.region(), "City"))
        self.assertIn("err", z.market_entry(dict(m, floor_strike=None), self.region(), "City"))
        # "exactly" / unknown type / no region / no rules: not modelled
        self.assertIn("not modelled", z.market_entry(
            dict(m, rules_primary=SEA.replace("is above", "is exactly")),
            self.region(), "City")["err"])
        self.assertIn("err", z.market_entry(
            dict(m, rules_primary=SEA.replace("single-family homes", "5+ bedroom homes")),
            self.region(), "City"))
        self.assertIn("err", z.market_entry(m, None, None))
        self.assertIn("err", z.market_entry(dict(m, rules_primary="x"), self.region(), "City"))
        self.assertEqual(z.market_entry(m, self.region(n=30), "City")["err"],
                         "too little history")

    def test_below_and_at_least(self):
        # Jack 2026-11-13: "make the Zillow markets generalizable" -- KXHOUHOME
        # is a "below" ladder (cap_strike); it prices as 1 - P(above)
        nov = SEA.replace("November 2026", "November 2026")
        up = z.market_entry({"rules_primary": nov, "floor_strike": 882500},
                            self.region(), "City")
        below = SEA.replace("is above", "is below")
        dn = z.market_entry({"rules_primary": below, "cap_strike": 882500},
                            self.region(), "City")
        self.assertAlmostEqual(dn["p"], 1.0 - up["p"], places=9)
        self.assertEqual((dn["op"], dn["strike"]), ("below", 882500.0))
        # a "below" market carries cap_strike, not floor_strike
        self.assertEqual(z.market_entry({"rules_primary": below, "floor_strike": 882500},
                                        self.region(), "City")["err"], "no cap_strike")
        most = z.market_entry({"rules_primary": SEA.replace("is above", "is at most"),
                               "cap_strike": 882500}, self.region(), "City")
        self.assertAlmostEqual(most["p"], dn["p"], places=9)
        least = z.market_entry({"rules_primary": SEA.replace("is above", "is at least"),
                                "floor_strike": 882500}, self.region(), "City")
        self.assertAlmostEqual(least["p"], up["p"], places=9)
        # HOU's own rules: condos, February 2027 (h=5)
        h = z.market_entry({"rules_primary": HOU, "cap_strike": 111000},
                           self.region(), "City")
        self.assertEqual((h["h"], h["op"]), (5, "below"))
        self.assertTrue(0.0 <= h["p"] <= 1.0)

    def test_too_far_out_is_not_modelled(self):
        far = SEA.replace("November 2026", "December 2027")       # h = 15
        e = z.market_entry({"rules_primary": far, "floor_strike": 882500},
                           self.region(), "City")
        self.assertIn("not modelled: 15 months", e["err"])
        self.assertNotIn("p", e)
        mar = SEA.replace("November 2026", "March 2027")          # h = 6
        self.assertIn("p", z.market_entry({"rules_primary": mar, "floor_strike": 882500},
                                          self.region(), "City"))
        with mock.patch.object(z, "MAX_H", 20):
            self.assertIn("p", z.market_entry({"rules_primary": far, "floor_strike": 882500},
                                              self.region(), "City"))

    def test_target_month_published(self):
        sep = SEA.replace("November 2026", "September 2026")
        m = {"rules_primary": sep, "floor_strike": 882500}
        self.assertEqual(z.market_entry(m, self.region(last_value=900000), "City")["p"], 1.0)
        e = z.market_entry(m, self.region(last_value=882500), "City")
        self.assertEqual((e["p"], e["decided"]), (0.0, True))      # "above" is strict
        # "below" decided the other way; "at least" is not strict
        below = sep.replace("is above", "is below")
        self.assertEqual(z.market_entry({"rules_primary": below, "cap_strike": 882500},
                                        self.region(last_value=870000), "City")["p"], 1.0)
        self.assertEqual(z.market_entry({"rules_primary": below, "cap_strike": 882500},
                                        self.region(last_value=882500), "City")["p"], 0.0)
        least = sep.replace("is above", "is at least")
        self.assertEqual(z.market_entry({"rules_primary": least, "floor_strike": 882500},
                                        self.region(last_value=882500), "City")["p"], 1.0)
        aug = SEA.replace("November 2026", "August 2026")
        self.assertIn("first print is out", z.market_entry(
            {"rules_primary": aug, "floor_strike": 882500}, self.region(), "City")["err"])


class TestSync(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        _csv(os.path.join(self.dir, "City.csv"),
             [(16037, "Seattle", "city", _series(v0=850000.0))])
        _csv(os.path.join(self.dir, "Neighborhood.csv"),
             [(21056, "Venice", "neighborhood", _series(v0=1900000.0))])
        for geo in ("Metro", "County", "State"):
            _csv(os.path.join(self.dir, f"{geo}.csv"), [])
        self.lm = {"City": 1000.0, "Neighborhood": 1000.0}
        self.fail = set()
        self.loads = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _patches(self):
        def head(url):
            geo = os.path.basename(url).split(".")[0]
            if geo in self.fail:
                raise OSError("down")
            return self.lm.get(geo, 1.0), "e"

        def url(geo, cut):
            return os.path.join(self.dir, f"{geo}.csv")

        real = z.read_regions

        def read(u, rids):
            self.loads.append(os.path.basename(u))
            return real(u, rids)
        return mock.patch.multiple(z, _head=head, file_url=url, read_regions=read)

    def _watch(self):
        w = z.HomeWatch(None, ["KXSEAHOME", "KXVENICEHOME"])
        w.markets = [
            {"ticker": "KXSEAHOME-30NOV26-GT882500", "rules_primary": SEA,
             "floor_strike": 882500},
            {"ticker": "KXVENICEHOME-30NOV26-GT2100000", "rules_primary": VEN,
             "floor_strike": 2100000}]
        w.meta_at = 1e12                       # no Kalshi reads
        return w

    def test_locates_prices_and_rereads_only_on_a_new_release(self):
        w = self._watch()
        with self._patches():
            s = w.refresh(now_ts=5000.0)
            self.assertEqual(s["errors"], [])
            self.assertEqual(s["checked_ts"], 5000.0)
            self.assertEqual(s["release_ts"], 1000.0)
            self.assertEqual(w.where, {(16037, "uc_sfr_tier_0.33_0.67_sm_sa"): "City",
                                       (21056, "uc_sfr_tier_0.33_0.67_sm_sa"): "Neighborhood"})
            for t in ("KXSEAHOME-30NOV26-GT882500", "KXVENICEHOME-30NOV26-GT2100000"):
                self.assertIn("p", s["markets"][t], t)
            n = len(self.loads)
            w.refresh(now_ts=5000.0 + z.HEAD_SECS)          # same Last-Modified
            self.assertEqual(len(self.loads), n)
            self.lm["City"] = 2000.0                        # a new release
            s = w.refresh(now_ts=5000.0 + 2 * z.HEAD_SECS)
            self.assertEqual(self.loads[n:], ["City.csv"])
            self.assertEqual(s["release_ts"], 2000.0)

    def test_a_failed_check_leaves_checked_ts_behind(self):
        w = self._watch()
        with self._patches():
            w.refresh(now_ts=5000.0)
            self.fail.add("City")
            s = w.refresh(now_ts=5000.0 + z.HEAD_SECS)
            self.assertEqual(s["checked_ts"], 5000.0)
            self.assertTrue(s["errors"])
            self.assertIn("p", s["markets"]["KXSEAHOME-30NOV26-GT882500"])

    def test_a_restart_reads_the_disk_cache_until_a_new_release(self):
        cache = os.path.join(self.dir, "cache.json")
        w = self._watch()
        w.cache_path = cache
        with self._patches():
            s1 = w.refresh(now_ts=5000.0)
            n = len(self.loads)
            self.assertGreater(n, 0)
            w2 = z.HomeWatch(None, ["KXSEAHOME", "KXVENICEHOME"], cache_path=cache)
            w2.markets, w2.meta_at = w.markets, 1e12
            s2 = w2.refresh(now_ts=6000.0)
            self.assertEqual(len(self.loads), n)                # no download
            self.assertEqual(s2["markets"], s1["markets"])
            self.lm["Neighborhood"] = 3000.0
            w3 = z.HomeWatch(None, ["KXSEAHOME", "KXVENICEHOME"], cache_path=cache)
            w3.markets, w3.meta_at = w.markets, 1e12
            w3.refresh(now_ts=7000.0)
            self.assertEqual(self.loads[n:], ["Neighborhood.csv"])

    def test_a_missing_region_is_not_searched_again_at_the_same_release(self):
        w = self._watch()
        w.markets.append({"ticker": "KXNOHOME-30NOV26-GT1", "floor_strike": 1,
                          "rules_primary": SEA.replace("16037", "99999")})
        with self._patches():
            s = w.refresh(now_ts=5000.0)
            self.assertIn("region 99999 not found", " ".join(s["errors"]))
            self.assertIn("err", s["markets"]["KXNOHOME-30NOV26-GT1"])
            n = len(self.loads)
            w.refresh(now_ts=5000.0 + z.HEAD_SECS)
            self.assertEqual(len(self.loads), n)


MEM = ("If the Zillow Home Value Index for single-family homes in Memphis, TN "
       "(Zillow region ID 32811) is above $148,000 at December 2026, then the "
       "market resolves to Yes.")
FRED = ("If the Median Sales Price of Existing Homes (HOSMEDUSM052N) for "
        "November 2026 is above $435,000, then the market resolves to Yes.")


class TestDiscover(unittest.TestCase):
    """Jack 2026-11-13: "why isnt KXMEMHOME-31DEC26 quoting? make the Zillow
    markets generalizable and auto-quote new ones"."""

    def setUp(self):
        self.listed = {
            "KXSEAHOME": [{"ticker": "KXSEAHOME-30NOV26-GT882500",
                           "rules_primary": SEA, "floor_strike": 882500}],
            "KXMEMHOME": [{"ticker": "KXMEMHOME-31DEC26-GT148000",
                           "rules_primary": MEM, "floor_strike": 148000}],
            "KXEXUSHOME": [{"ticker": "KXEXUSHOME-26DEC09-T435000",
                            "rules_primary": FRED, "floor_strike": 435000}],
            "KXNEWHOME": [],
        }
        self.calls = []
        self.cands = ["KXSEAHOME", "KXMEMHOME", "KXEXUSHOME", "KXNEWHOME"]

    def _markets(self, gj, series):
        self.calls.append(series)
        return list(self.listed.get(series, []))

    def _refresh(self, w, ts):
        with mock.patch.object(z, "_markets", self._markets),                 mock.patch.object(z.HomeWatch, "_sync_zillow", lambda *a: None):
            return w.refresh(now_ts=ts)

    def test_zillow_rules_join_and_others_wait(self):
        w = z.HomeWatch(None, ["KXSEAHOME"], discover=lambda: self.cands)
        s = self._refresh(w, 10000.0)
        self.assertEqual(s["auto_series"], ["KXMEMHOME"])
        self.assertEqual(s["series"], ["KXMEMHOME", "KXSEAHOME"])
        self.assertEqual(s["probed"], {
            "KXEXUSHOME": "rules name no Zillow Home Value Index",
            "KXNEWHOME": "no open market yet"})
        self.assertIn("KXMEMHOME-31DEC26-GT148000", s["markets"])
        self.assertNotIn("KXEXUSHOME-26DEC09-T435000", s["markets"])
        # the listed series is never probed; nothing is probed twice inside
        # META_REFRESH_SECS
        self.assertNotIn("KXSEAHOME", [c for c in self.calls[1:]])
        n = len(self.calls)
        self._refresh(w, 10000.0 + 60)
        self.assertEqual(self.calls[n:], [])
        # KXNEWHOME lists its ladder: the next probe takes it
        self.listed["KXNEWHOME"] = [{"ticker": "KXNEWHOME-31DEC26-GT1",
                                     "rules_primary": MEM, "floor_strike": 1}]
        s = self._refresh(w, 10000.0 + z.META_REFRESH_SECS)
        self.assertEqual(s["auto_series"], ["KXMEMHOME", "KXNEWHOME"])
        self.assertEqual(s["probed"], {"KXEXUSHOME": "rules name no Zillow Home Value Index"})
        # a full listing covers the auto series too
        self.assertIn("KXMEMHOME", self.calls[len(self.calls) - 6:])

    def test_seeded_auto_is_listed_without_a_probe(self):
        w = z.HomeWatch(None, ["KXSEAHOME"], auto=["KXMEMHOME", "KXSEAHOME"])
        self.assertEqual(w.auto, {"KXMEMHOME"})
        s = self._refresh(w, 10000.0)
        self.assertEqual(self.calls, ["KXSEAHOME", "KXMEMHOME"])
        self.assertEqual(s["auto_series"], ["KXMEMHOME"])
        self.assertIn("KXMEMHOME-31DEC26-GT148000", s["markets"])

    def test_a_failing_discover_or_probe_is_an_error_not_a_crash(self):
        def boom():
            raise RuntimeError("feed down")
        w = z.HomeWatch(None, ["KXSEAHOME"], discover=boom)
        s = self._refresh(w, 10000.0)
        self.assertIn("discover: RuntimeError", " ".join(s["errors"]))
        w = z.HomeWatch(None, ["KXSEAHOME"], discover=lambda: ["KXMEMHOME"])
        real = self._markets

        def flaky(gj, series):
            if series == "KXMEMHOME":
                raise OSError("429")
            return real(gj, series)
        with mock.patch.object(z, "_markets", flaky),                 mock.patch.object(z.HomeWatch, "_sync_zillow", lambda *a: None):
            s = w.refresh(now_ts=10000.0)
        self.assertIn("probe KXMEMHOME: OSError", " ".join(s["errors"]))
        self.assertEqual(s["auto_series"], [])


if __name__ == "__main__":
    unittest.main()
