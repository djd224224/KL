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
        # below / unknown type / no region / no rules: not modelled
        self.assertIn("not modelled", z.market_entry(
            {"rules_primary": HOU, "floor_strike": 111000}, self.region(), "City")["err"])
        self.assertIn("err", z.market_entry(
            dict(m, rules_primary=SEA.replace("single-family homes", "5+ bedroom homes")),
            self.region(), "City"))
        self.assertIn("err", z.market_entry(m, None, None))
        self.assertIn("err", z.market_entry(dict(m, rules_primary="x"), self.region(), "City"))
        self.assertEqual(z.market_entry(m, self.region(n=30), "City")["err"],
                         "too little history")

    def test_target_month_published(self):
        sep = SEA.replace("November 2026", "September 2026")
        m = {"rules_primary": sep, "floor_strike": 882500}
        self.assertEqual(z.market_entry(m, self.region(last_value=900000), "City")["p"], 1.0)
        e = z.market_entry(m, self.region(last_value=882500), "City")
        self.assertEqual((e["p"], e["decided"]), (0.0, True))      # "above" is strict
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


if __name__ == "__main__":
    unittest.main()
