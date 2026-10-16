"""Tests for carbon_arc_fair.py (Carbon Arc month-to-date -> first-print
fair values for incentive_mm's Carbon Arc gate, 2026-09-26)."""

import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import carbon_arc_fair as caf


def _prism(pid, entities, data_through="2026-09-23",
           refreshed="2026-09-26T13:29:00+00:00", category="Credit Card"):
    return {"prism_id": pid, "category": category,
            "data_through": data_through, "last_refreshed_at": refreshed,
            "entities": entities}


def _entity(name, mtd, hist):
    return {"entity_name": name,
            "mtd_yoy": [{"date": d, "value": v} for d, v in mtd],
            "hist_yoy": [{"month": m, "value": v} for m, v in hist]}


HIST = [("2026-04", 100.0), ("2026-05", 102.0), ("2026-06", 101.0),
        ("2026-07", 104.0), ("2026-08", 103.0), ("2026-09", 70.0)]
NOW = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)


class TestSeriesMap(unittest.TestCase):

    def test_prism_ref_and_catalog_map(self):
        u = ("https://www.carbonarc.co/prisms?prism=abc-123"
             "&entity=Costco+Wholesale")
        self.assertEqual(caf.prism_ref(u), ("abc-123", "Costco Wholesale"))
        self.assertIsNone(caf.prism_ref("https://fiscal.ai"))
        self.assertIsNone(caf.prism_ref(
            "https://www.carbonarc.co/prisms?prism=abc"))      # no entity
        catalog = [
            {"ticker": "KXCOSTCC", "settlement_sources": [
                {"name": "Carbon Arc", "url": u}]},
            {"ticker": "KXBAA", "settlement_sources": [
                {"name": "Fiscal.ai", "url": "https://fiscal.ai"}]},
            {"ticker": "KXODD", "settlement_sources": None},
            "not a dict",
        ]
        self.assertEqual(caf.series_map_from_catalog(catalog), {
            "KXCOSTCC": {"prism": "abc-123", "entity": "Costco Wholesale"}})


class TestModel(unittest.TestCase):

    def test_month_spread_drops_the_partial_month(self):
        # changes 2, -1, 3, -1 (the partial 2026-09 read is ignored)
        self.assertAlmostEqual(caf.month_spread(
            [{"month": m, "value": v} for m, v in HIST]),
            math.sqrt(((2 - 0.75) ** 2 + (-1 - 0.75) ** 2 + (3 - 0.75) ** 2
                       + (-1 - 0.75) ** 2) / 4))
        self.assertIsNone(caf.month_spread(
            [{"month": "2026-07", "value": 1.0},
             {"month": "2026-08", "value": 2.0},
             {"month": "2026-09", "value": None}]))

    def test_entry_mu_sigma_and_weight(self):
        p = _prism("P", [])
        e = _entity("Amazon", [("2026-09-22", 108.0), ("2026-09-23", 108.64)],
                    HIST)
        out = caf.entity_entry(p, e, NOW)
        w = 23 / 30.0
        sig_m = caf.month_spread(e["hist_yoy"])
        floor = max(caf.CA_SIGMA_FLOOR_PTS, caf.CA_SIGMA_FLOOR_REL * 108.64)
        self.assertEqual(out["month"], "2026-09")
        self.assertEqual(out["data_through"], "2026-09-23")
        self.assertAlmostEqual(out["mu"], 108.64)
        self.assertAlmostEqual(out["w"], round(w, 4))
        self.assertAlmostEqual(out["sigma"], round(
            caf.CA_SIGMA_MULT * math.sqrt(((1 - w) * sig_m) ** 2
                                          + floor ** 2), 4))
        # day 23: past the early-month widening
        self.assertEqual(out["early_mult"], 1.0)
        # the last MTD point defines the observed stretch, whatever order
        # the feed lists them in
        e2 = _entity("Amazon", [("2026-09-23", 108.64), ("2026-09-22", 1.0)],
                     HIST)
        self.assertAlmostEqual(caf.entity_entry(p, e2, NOW)["mu"], 108.64)

    def test_entry_refuses_thin_or_stale_reads(self):
        p = _prism("P", [])
        # the fraction floor still refuses a thin read when it is set (the
        # pre-2026-10-04 0.10: 2 days of a 30-day month is under it)
        thin = _entity("X", [("2026-09-02", 101.0)], HIST)
        with mock.patch.object(caf, "CA_MIN_OBS_FRAC", 0.10):
            self.assertIsNone(caf.entity_entry(p, thin, NOW))
        # a read 11+ days old is not the month in progress
        old = _entity("X", [("2026-09-14", 101.0)], HIST)
        self.assertIsNone(caf.entity_entry(p, old, NOW))
        self.assertIsNone(caf.entity_entry(p, _entity("X", [], HIST), NOW))
        # too little history -> the default relative spread
        short = _entity("X", [("2026-09-23", 100.0)], HIST[-2:])
        out = caf.entity_entry(p, short, NOW)
        self.assertAlmostEqual(out["sigma_m"], caf.CA_SIGMA_M_DEFAULT_REL * 100.0)

    def _base_sigma(self, day, n_days, mu):
        w = day / float(n_days)
        sig_m = caf.month_spread([{"month": m, "value": v} for m, v in HIST])
        floor = max(caf.CA_SIGMA_FLOOR_PTS, caf.CA_SIGMA_FLOOR_REL * mu)
        return caf.CA_SIGMA_MULT * math.sqrt(((1 - w) * sig_m) ** 2
                                             + floor ** 2)

    def test_a_read_is_an_entry_from_its_first_day(self):
        # Jack 2026-10-04 "yes ship it": the October book quoted on no read
        # for days because a month waited for 10% observed. Point of sale's
        # day-1 read (rolled 10/02 23:00Z) is an entry, widened like card
        # spend's (its Oct 1-3 path swung with the weekday mix)
        now = datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)
        pos = _prism("P", [], data_through="2026-10-01",
                     category="Point of Sale")
        out = caf.entity_entry(pos, _entity("C4 Energy",
                                            [("2026-10-01", 131.03)], HIST), now)
        self.assertEqual(out["month"], "2026-10")
        self.assertAlmostEqual(out["mu"], 131.03)
        self.assertAlmostEqual(out["early_mult"], 3.4)
        self.assertAlmostEqual(out["sigma"],
                               round(self._base_sigma(1, 31, 131.03) * 3.4, 4))

    def test_early_reads_are_widened_by_category(self):

        def entry(category, day, mu=100.0):
            p = _prism("P", [], category=category)
            e = _entity("X", [("2026-10-%02d" % day, mu)], HIST)
            # read two days after its data day, as the feed lags
            now = datetime(2026, 10, day, 13, 0, tzinfo=timezone.utc) \
                + timedelta(days=2)
            return caf.entity_entry(p, e, now)

        # card spend: one day is past repair, then A = 3.4 / sqrt(days)
        self.assertIsNone(entry("Credit Card", 1))
        cc2 = entry("Credit Card", 2)
        self.assertAlmostEqual(cc2["early_mult"], round(3.4 / math.sqrt(2), 4))
        self.assertAlmostEqual(cc2["sigma"], round(
            self._base_sigma(2, 31, 100.0) * 3.4 / math.sqrt(2), 4))
        self.assertAlmostEqual(entry("Credit Card", 5)["early_mult"],
                               round(3.4 / math.sqrt(5), 4))
        # widening ends where A / sqrt(days) reaches 1 (card day 12 -> 0.98)
        self.assertEqual(entry("Credit Card", 12)["early_mult"], 1.0)
        # foot traffic 2.5, apps and ad spend 1.8, from day 1
        self.assertAlmostEqual(entry("Foot Traffic", 1)["early_mult"], 2.5)
        self.assertAlmostEqual(entry("App", 1)["early_mult"], 1.8)
        self.assertAlmostEqual(entry("advertising ", 1)["early_mult"], 1.8)
        self.assertEqual(entry("App", 4)["early_mult"], 1.0)
        # point of sale takes card spend's A but needs no second day; an
        # unknown category takes the widest A
        self.assertAlmostEqual(entry("Point of Sale", 1)["early_mult"], 3.4)
        self.assertAlmostEqual(entry("Point of Sale", 3)["early_mult"],
                               round(3.4 / math.sqrt(3), 4))
        self.assertEqual(entry("Point of Sale", 12)["early_mult"], 1.0)
        self.assertAlmostEqual(entry("Something New", 1)["early_mult"], 3.4)
        self.assertAlmostEqual(entry(None, 1)["early_mult"], 3.4)

    def test_early_sigma_knob_off_and_the_old_model(self):
        now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
        p = _prism("P", [], category="Foot Traffic")
        e = _entity("X", [("2026-10-02", 100.0)], HIST)
        with mock.patch.object(caf, "CA_EARLY_SIGMA_ENABLE", False):
            out = caf.entity_entry(p, e, now)
        self.assertEqual(out["early_mult"], 1.0)
        self.assertAlmostEqual(out["sigma"],
                               round(self._base_sigma(2, 31, 100.0), 4))
        # IMM_CA_MIN_OBS_FRAC=0.10 brings back the 10%-observed wait
        with mock.patch.object(caf, "CA_MIN_OBS_FRAC", 0.10):
            self.assertIsNone(caf.entity_entry(p, e, now))
            e4 = _entity("X", [("2026-10-04", 100.0)], HIST)
            self.assertIsNotNone(caf.entity_entry(p, e4, now))

    def test_p_above(self):
        self.assertAlmostEqual(caf.p_above(100.0, 100.0, 2.0), 0.5)
        self.assertAlmostEqual(caf.p_above(102.0, 100.0, 2.0), 0.158655, 5)
        self.assertEqual(caf.p_above(99.0, 100.0, 0.0), 1.0)


class TestOpenDay(unittest.TestCase):
    """Jack 2026-10-10 "ship 1": since 10/07 14:43Z point of sale's last MTD
    point is the day still in progress (~(k-1)/k of the complete value,
    -11% to -14%). It is dropped and the previous complete day priced."""

    def setUp(self):
        self.pos = _prism("P", [], data_through="2026-10-09", category="Point of Sale")
        self.path = [("2026-10-07", 98.5), ("2026-10-08", 99.1), ("2026-10-09", 88.2)]

    def test_the_point_for_today_is_dropped(self):
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)      # 08:00 PT 10/09
        out = caf.entity_entry(self.pos, _entity("C4 Energy", self.path, HIST), now)
        self.assertEqual((out["mu"], out["data_through"]), (99.1, "2026-10-08"))
        self.assertEqual(out["open_day_dropped"], "2026-10-09")
        self.assertAlmostEqual(out["w"], round(8 / 31, 4))          # 8 complete days observed

    def test_the_us_evening_counts_as_the_same_day(self):
        # 03:00Z 10/10 is 20:00 PT 10/09: the 10/09 point is still in progress
        now = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
        out = caf.entity_entry(self.pos, _entity("C4 Energy", self.path, HIST), now)
        self.assertEqual(out["data_through"], "2026-10-08")

    def test_a_point_stays_open_until_the_next_refresh(self):
        # 10/10 07:00-11:55Z: Pacific midnight passed, but the prism was last
        # refreshed 10/09 11:58Z (04:58 PT 10/09), so its 10/09 point is the
        # partial one it carried then -- still dropped
        pos = _prism("P", [], data_through="2026-10-09", category="Point of Sale",
                     refreshed="2026-10-09T11:58:00+00:00")
        later = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
        out = caf.entity_entry(pos, _entity("C4 Energy", self.path, HIST), later)
        self.assertEqual((out["mu"], out["data_through"]), (99.1, "2026-10-08"))
        self.assertEqual(out["open_day_dropped"], "2026-10-09")
        # the 10/10 11:54Z refresh completes 10/09 and opens 10/10
        pos = _prism("P", [], data_through="2026-10-10", category="Point of Sale",
                     refreshed="2026-10-10T11:54:00+00:00")
        path = self.path[:2] + [("2026-10-09", 99.6), ("2026-10-10", 87.0)]
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        out = caf.entity_entry(pos, _entity("C4 Energy", path, HIST), now)
        self.assertEqual((out["mu"], out["data_through"]), (99.6, "2026-10-09"))
        self.assertEqual(out["open_day_dropped"], "2026-10-10")
        # a refresh after Pacific midnight with no point for its own day:
        # the last point is complete
        pos = _prism("P", [], data_through="2026-10-09", category="Point of Sale",
                     refreshed="2026-10-10T07:30:00+00:00")
        out = caf.entity_entry(pos, _entity("C4 Energy", self.path, HIST), later)
        self.assertEqual((out["mu"], out["data_through"]), (88.2, "2026-10-09"))
        self.assertNotIn("open_day_dropped", out)

    def test_a_missing_or_stale_refresh_stamp_falls_back_to_the_clock(self):
        later = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
        for stamp in ("", "not a time", "2026-09-26T13:29:00+00:00"):
            pos = _prism("P", [], data_through="2026-10-09",
                         category="Point of Sale", refreshed=stamp)
            out = caf.entity_entry(pos, _entity("C4 Energy", self.path, HIST), later)
            self.assertEqual(out["data_through"], "2026-10-09", stamp)

    def test_a_lone_open_day_is_no_read_and_the_knob_restores_it(self):
        now = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
        lone = _entity("C4 Energy", [("2026-10-01", 12.4)], HIST)    # day 1, an hour in
        self.assertIsNone(caf.entity_entry(self.pos, lone, now))
        with mock.patch.object(caf, "CA_DROP_OPEN_DAY", False):
            self.assertAlmostEqual(caf.entity_entry(self.pos, lone, now)["mu"], 12.4)

    def test_lagged_categories_are_untouched(self):
        card = _prism("C", [], category="Credit Card")
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        out = caf.entity_entry(card, _entity("Amazon", [("2026-10-05", 104.2),
                                                        ("2026-10-06", 104.8)], HIST), now)
        self.assertEqual((out["mu"], out["data_through"]), (104.8, "2026-10-06"))
        self.assertNotIn("open_day_dropped", out)


class TestBuildAndWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caf_test_")
        self.out = os.path.join(self.tmp, "carbon_arc_fair.json")
        self.vint = os.path.join(self.tmp, "vintages.jsonl")
        self.smap = {"KXAMZNCC": {"prism": "P1", "entity": "Amazon"},
                     "KXSTREAMINGADS": {"prism": "P2",
                                        "entity": "Film & TV Streaming"},
                     "KXGONE": {"prism": "P9", "entity": "Nobody"}}
        self.payload = {"prisms": [
            _prism("P1", [_entity("Amazon", [("2026-09-23", 108.64)], HIST)]),
            _prism("P2", [_entity("Film & Tv Streaming",
                                  [("2026-09-20", 83.98)], HIST)],
                   data_through="2026-09-20", category="Advertising"),
        ]}

    def test_build_matches_entities_case_insensitively(self):
        entries, missing = caf.build_entries(self.payload, self.smap, NOW)
        self.assertEqual(sorted(entries), ["KXAMZNCC", "KXSTREAMINGADS"])
        self.assertEqual(missing, ["KXGONE"])
        self.assertEqual(entries["KXSTREAMINGADS"]["category"], "Advertising")

    def test_write_file_and_vintages_only_on_a_new_read(self):
        ok, miss = caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                                       series_map=self.smap,
                                       vintage_path=self.vint)
        self.assertEqual((ok, miss), (2, 1))
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["entries"]["KXAMZNCC"]["fetched_at"],
                         NOW.isoformat())
        self.assertEqual(data["series_map"], self.smap)
        # the early-month knobs ride along in the model block
        self.assertEqual(data["model"]["min_obs_days"], {"credit card": 2})
        self.assertEqual(data["model"]["early_sigma_a"]["credit card"], 3.4)
        with open(self.vint, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        # same read again: file rewritten, no new vintage lines
        caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                            series_map=self.smap, vintage_path=self.vint)
        with open(self.vint, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        # Amazon's read moves one day: exactly one new vintage
        self.payload["prisms"][0]["entities"][0]["mtd_yoy"].append(
            {"date": "2026-09-24", "value": 108.10})
        caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                            series_map=self.smap, vintage_path=self.vint)
        with open(self.vint, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.readlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual((lines[-1]["series"], lines[-1]["data_through"]),
                         ("KXAMZNCC", "2026-09-24"))

    def test_hand_mapped_series_fill_only_what_the_catalog_left_out(self):
        # Jack 2026-10-16: KXDPZFT / KXSBUXFT / KXWMTFT settle on a generic
        # carbonarc.co/welcome link, so the catalog cannot map them
        self.assertEqual(caf.CA_SERIES_MAP_EXTRA["KXDPZFT"]["entity"], "Domino's Pizza")
        self.assertEqual(sorted(caf.CA_SERIES_MAP_EXTRA),
                         ["KXAFRMCC", "KXAFTERPAYCC", "KXDPZFT", "KXKLARCC",
                          "KXSBUXFT", "KXWMTFT"])
        # the BNPL card series share one prism (Jack 2026-10-16, same evening)
        self.assertEqual({caf.CA_SERIES_MAP_EXTRA[s]["prism"]
                          for s in ("KXAFRMCC", "KXAFTERPAYCC", "KXKLARCC")},
                         {"b528a1d1-ebf5-4a46-bc1c-eaec177a21e9"})
        self.assertEqual(caf.CA_SERIES_MAP_EXTRA["KXKLARCC"]["entity"], "Klarna")
        p = self.payload["prisms"][0]
        p["entities"].append(_entity("Walmart", [("2026-09-23", 101.5)], HIST))
        extra = {"KXWMTFT": {"prism": p["prism_id"], "entity": "walmart"},   # case-insensitive
                 "KXAMZNCC": {"prism": "WRONG", "entity": "Nope"}}
        with mock.patch.object(caf, "CA_SERIES_MAP_EXTRA", extra), \
                mock.patch.object(caf, "fetch_series_map", return_value=dict(self.smap)):
            caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                                vintage_path=self.vint)
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertAlmostEqual(data["entries"]["KXWMTFT"]["mu"], 101.5)
        self.assertEqual(data["series_map"]["KXWMTFT"]["entity"], "walmart")
        self.assertEqual(data["series_map"]["KXAMZNCC"], self.smap["KXAMZNCC"])  # the catalog wins
        # an explicit map is the caller's whole map: no extras
        with mock.patch.object(caf, "CA_SERIES_MAP_EXTRA", extra):
            caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                                series_map=self.smap, vintage_path=self.vint)
        with open(self.out, encoding="utf-8") as f:
            self.assertNotIn("KXWMTFT", json.load(f)["series_map"])

    @mock.patch.object(caf, "CA_SERIES_MAP_EXTRA", {})    # the catalog alone
    def test_series_map_reused_then_refreshed_and_kept_on_failure(self):
        caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                            series_map=self.smap, vintage_path=self.vint)
        # fresh map in the file -> no catalog read
        with mock.patch.object(caf, "fetch_series_map",
                               side_effect=AssertionError("no read")):
            caf.write_fair_file(self.out, payload=self.payload, now=NOW,
                                vintage_path=self.vint)
        # expired map + failed catalog read -> the old map is kept
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        data["series_map_ts"] = 0.0
        with open(self.out, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with mock.patch.object(caf, "fetch_series_map",
                               side_effect=RuntimeError("down")):
            ok, miss = caf.write_fair_file(self.out, payload=self.payload,
                                           now=NOW, vintage_path=self.vint)
        self.assertEqual((ok, miss), (2, 1))

    def test_no_feed_configured_writes_nothing(self):
        with mock.patch.object(caf, "feed_settings", return_value=("", "")):
            self.assertEqual(caf.write_fair_file(self.out), (0, 0))
        self.assertFalse(os.path.exists(self.out))


class TestArrayShadow(unittest.TestCase):
    """Jack 2026-10-16 (B2, shadow): a sector array rebuilt from the brand
    prism that holds its constituents."""

    @staticmethod
    def brand(name, mtd, ly_share, mtd_share):
        e = _entity(name, [("2026-09-23", mtd)], HIST)
        e["hist_share"] = [{"month": "2025-08", "value": 1.0},
                           {"month": "2025-09", "value": ly_share}]
        e["mtd_share"] = [{"date": "2026-09-22", "value": 1.0},
                          {"date": "2026-09-23", "value": mtd_share}]
        return e

    def prism(self, *ents):
        return _prism("BRANDS", list(ents), category="Point of Sale")

    def test_three_variants_and_a_share_weighted_mu(self):
        p = self.prism(self.brand("A", 90.0, 60.0, 55.0), self.brand("B", 110.0, 40.0, 45.0))
        e = caf.array_entry(p, NOW)
        self.assertEqual(e["month"], "2026-09")
        v = e["variants"]
        self.assertAlmostEqual(v["ly_share"], (60 * 90 + 40 * 110) / 100.0)      # 98.0
        self.assertAlmostEqual(v["mean"], 100.0)
        self.assertAlmostEqual(v["mtd_harmonic"], 100.0 / (55 / 90 + 45 / 110), places=3)
        self.assertEqual(e["mu"], round(v["ly_share"], 4))
        self.assertEqual(e["weights"], {"A": 60.0, "B": 40.0})
        self.assertTrue(e["shadow"])
        sa, sb = e["brands"]["A"][1], e["brands"]["B"][1]
        self.assertAlmostEqual(e["sigma"], (60 * sa + 40 * sb) / 100.0, places=3)

    def test_no_composite_without_every_brand_or_any_shares(self):
        dead = _entity("C", [], HIST)                        # a brand with no read
        self.assertIsNone(caf.array_entry(
            self.prism(self.brand("A", 90.0, 60.0, 55.0), dead), NOW))
        a, b = self.brand("A", 90.0, 60.0, 55.0), self.brand("B", 110.0, 40.0, 45.0)
        for x in (a, b):
            x["hist_share"], x["mtd_share"] = [], []
        self.assertIsNone(caf.array_entry(self.prism(a, b), NOW))
        # last year's shares missing: falls back to this month's
        a, b = self.brand("A", 90.0, 60.0, 55.0), self.brand("B", 110.0, 40.0, 45.0)
        a["hist_share"] = []
        e = caf.array_entry(self.prism(a, b), NOW)
        self.assertNotIn("ly_share", e["variants"])
        self.assertEqual(e["mu"], round(e["variants"]["mtd_harmonic"], 4))

    def test_written_under_its_own_key_never_the_gates(self):
        tmp = tempfile.mkdtemp(prefix="caf_arr_")
        out, vint = os.path.join(tmp, "fair.json"), os.path.join(tmp, "v.jsonl")
        p = self.prism(self.brand("A", 90.0, 60.0, 55.0), self.brand("B", 110.0, 40.0, 45.0))
        smap = {"KXAPOS": {"prism": "BRANDS", "entity": "A"},
                "KXFAKEARRAY": {"prism": "ARR", "entity": "Fake Brands"}}   # 404s
        with mock.patch.object(caf, "CA_ARRAY_BRANDS", {"KXFAKEARRAY": "KXAPOS"}):
            ok, miss = caf.write_fair_file(out, payload={"prisms": [p], "failed": []},
                                           now=NOW, series_map=smap, vintage_path=vint)
        self.assertEqual((ok, miss), (1, 1))                 # the array is still "missing"
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("KXFAKEARRAY", data["entries"])
        self.assertAlmostEqual(data["array_shadow"]["KXFAKEARRAY"]["mu"], 98.0)
        self.assertEqual(data["array_shadow"]["KXFAKEARRAY"]["source_prism"], "BRANDS")
        self.assertTrue(os.path.exists(vint + ".array"))    # its own vintage file
        with mock.patch.object(caf, "CA_ARRAY_SHADOW", False):
            caf.write_fair_file(out, payload={"prisms": [p], "failed": []},
                                now=NOW, series_map=smap, vintage_path=vint)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["array_shadow"], {})


class TestFeedSettings(unittest.TestCase):

    def test_env_wins_then_home_file(self):
        tmp = tempfile.mkdtemp(prefix="caf_cfg_")
        cfg = os.path.join(tmp, "feed.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"url": "https://example.invalid/feed", "token": "t0"}, f)
        with mock.patch.object(caf, "CA_FEED_CONFIG", cfg), \
                mock.patch.dict(os.environ, {"IMM_CA_FEED_URL": "",
                                             "IMM_CA_FEED_TOKEN": ""}):
            self.assertEqual(caf.feed_settings(),
                             ("https://example.invalid/feed", "t0"))
        with mock.patch.object(caf, "CA_FEED_CONFIG", cfg), \
                mock.patch.dict(os.environ, {
                    "IMM_CA_FEED_URL": "https://env.invalid/x",
                    "IMM_CA_FEED_TOKEN": "t1"}):
            self.assertEqual(caf.feed_settings(),
                             ("https://env.invalid/x", "t1"))
        with mock.patch.object(caf, "CA_FEED_CONFIG",
                               os.path.join(tmp, "absent.json")), \
                mock.patch.dict(os.environ, {"IMM_CA_FEED_URL": "",
                                             "IMM_CA_FEED_TOKEN": ""}):
            self.assertEqual(caf.feed_settings(), ("", ""))
            self.assertFalse(caf.feed_configured())

    def test_token_only_config_with_a_notepad_bom(self):
        tmp = tempfile.mkdtemp(prefix="caf_cfg_")
        cfg = os.path.join(tmp, "feed.json")
        with open(cfg, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"token": "abc"}).encode())
        with mock.patch.object(caf, "CA_FEED_CONFIG", cfg), \
                mock.patch.dict(os.environ, {"IMM_CA_FEED_URL": "",
                                             "IMM_CA_FEED_TOKEN": ""}):
            self.assertEqual(caf.feed_settings(), ("", "abc"))
            self.assertTrue(caf.feed_configured())


def _resp(status, body):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    if status >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    else:
        r.raise_for_status.return_value = None
    return r


class TestOfficialApi(unittest.TestCase):
    """The account-token path: the Prisms API the carbonarc SDK wraps
    (client.prisms.get_prisms / get_prism)."""

    BASE = "https://api.carbonarc.co/v2/prisms"

    def _prism(self, pid, iid, name="Amazon"):
        p = _prism(pid, [_entity(name, [("2026-09-23", 108.64)], HIST)])
        p["insight_id"] = iid
        return p

    def test_groups_by_known_insight_then_falls_back_per_prism(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, dict(params or {}), headers))
            if url == self.BASE and (params or {}).get("insight_id") == 245:
                return _resp(200, {"prisms": [self._prism("P1", 245),
                                              self._prism("PX", 245)]})
            if url == self.BASE + "/P2":
                p = self._prism("P2", 700)
                del p["prism_id"]              # the single read may omit it
                return _resp(200, p)
            if url == self.BASE + "/P3":
                return _resp(404, {"detail": "not found"})
            raise AssertionError(f"unexpected GET {url} {params}")

        insights = {"P1": 245}
        with mock.patch.object(caf.requests, "get", side_effect=fake_get):
            out = caf.fetch_prisms_api(["P1", "P2", "P3"], "tok", insights)
        self.assertEqual([p["prism_id"] for p in out["prisms"]], ["P1", "P2"])
        self.assertEqual(insights, {"P1": 245, "P2": 700})   # learned
        self.assertEqual(len(calls), 3)                      # 1 grouped + 2
        for _url, _params, headers in calls:
            self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_other_http_errors_raise(self):
        with mock.patch.object(caf.requests, "get",
                               return_value=_resp(500, {})):
            with self.assertRaises(RuntimeError):   # every read failed
                caf.fetch_prisms_api(["P1"], "tok", {})
        with mock.patch.object(caf.requests, "get",
                               return_value=_resp(401, {})):
            with self.assertRaises(RuntimeError):   # auth: still the whole sweep
                caf.fetch_prisms_api(["P1"], "tok", {})

    def test_a_server_error_on_one_prism_skips_only_that_prism(self):
        # 2026-10-16: an unpublished array prism's 500 used to fail all 93 series
        def fake_get(url, params=None, headers=None, timeout=None):
            if url == self.BASE + "/P1":
                return _resp(200, self._prism("P1", 245))
            if url == self.BASE + "/P2":
                return _resp(500, {})
            if url == self.BASE + "/P3":
                raise caf.requests.Timeout("read timed out")
            if url == self.BASE + "/P4":
                return _resp(404, {"detail": "not found"})
            raise AssertionError(f"unexpected GET {url} {params}")

        with mock.patch.object(caf.requests, "get", side_effect=fake_get), \
                mock.patch.object(caf, "_prism_err", None):
            out = caf.fetch_prisms_api(["P1", "P2", "P3", "P4"], "tok", {})
        self.assertEqual([p["prism_id"] for p in out["prisms"]], ["P1"])
        self.assertEqual(out["failed"], ["P2", "P3"])    # a 404 is not a failure

    def test_a_failed_insight_batch_falls_back_to_per_prism_reads(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            if url == self.BASE:
                return _resp(502, {})
            return _resp(200, self._prism(url.rsplit("/", 1)[1], 245))

        with mock.patch.object(caf.requests, "get", side_effect=fake_get), \
                mock.patch.object(caf, "_prism_err", None):
            out = caf.fetch_prisms_api(["P1", "P2"], "tok", {"P1": 245, "P2": 245})
        self.assertEqual([p["prism_id"] for p in out["prisms"]], ["P1", "P2"])
        self.assertEqual(out["failed"], [])
        self.assertEqual(calls, [self.BASE, self.BASE + "/P1", self.BASE + "/P2"])

    def test_a_failed_prism_keeps_its_series_last_entry(self):
        tmp = tempfile.mkdtemp(prefix="caf_carry_")
        out = os.path.join(tmp, "fair.json")
        vint = os.path.join(tmp, "v.jsonl")
        smap = {"KXAMZNCC": {"prism": "P1", "entity": "Amazon"},
                "KXWMTCC": {"prism": "P2", "entity": "Walmart"}}
        first = {"prisms": [self._prism("P1", 245),
                            self._prism("P2", 246, name="Walmart")],
                 "failed": []}
        self.assertEqual(caf.write_fair_file(out, payload=first, now=NOW,
                                             series_map=smap,
                                             vintage_path=vint), (2, 0))
        later = NOW + timedelta(minutes=2)
        second = {"prisms": [self._prism("P1", 245)], "failed": ["P2"]}
        self.assertEqual(caf.write_fair_file(out, payload=second, now=later,
                                             series_map=smap,
                                             vintage_path=vint), (2, 0))
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["missing"], [])
        self.assertEqual(data["entries"]["KXAMZNCC"]["fetched_at"], later.isoformat())
        # the carried read keeps its own age, so the gate's TTL still applies
        self.assertEqual(data["entries"]["KXWMTCC"]["fetched_at"], NOW.isoformat())
        # ...but a prism that merely has no read (404) is still missing
        third = {"prisms": [self._prism("P1", 245)], "failed": []}
        self.assertEqual(caf.write_fair_file(out, payload=third, now=later,
                                             series_map=smap,
                                             vintage_path=vint), (1, 1))

    def test_token_only_write_reads_the_api_and_keeps_the_insight_map(self):
        tmp = tempfile.mkdtemp(prefix="caf_api_")
        out = os.path.join(tmp, "fair.json")
        vint = os.path.join(tmp, "v.jsonl")
        smap = {"KXAMZNCC": {"prism": "P1", "entity": "Amazon"}}
        sched_urls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            if url.startswith(TestSchedules.BASE):          # the panels' schedules
                sched_urls.append(url)
                return _resp(200, TestSchedules.sched(NOW))
            self.assertEqual(url, self.BASE + "/P1")
            return _resp(200, self._prism("P1", 245))

        with mock.patch.object(caf, "feed_settings", return_value=("", "tok")), \
                mock.patch.object(caf.requests, "get", side_effect=fake_get):
            ok, miss = caf.write_fair_file(out, now=NOW, series_map=smap,
                                           vintage_path=vint)
        self.assertEqual((ok, miss), (1, 0))
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["prism_insights"], {"P1": 245})
        self.assertAlmostEqual(data["entries"]["KXAMZNCC"]["mu"], 108.64)
        # ...and every panel's schedule, read once
        self.assertEqual(sorted(data["schedules"]), sorted(caf.CA_SCHEDULE_DATASETS))
        self.assertEqual(data["schedules_ts"], NOW.timestamp())
        self.assertEqual(len(sched_urls), len(caf.CA_SCHEDULE_DATASETS))

        # next sweep: one grouped call per insight, no per-prism reads (and,
        # inside the refresh interval with every window ahead, no schedules)
        def grouped(url, params=None, headers=None, timeout=None):
            self.assertEqual((url, params), (self.BASE, {"insight_id": 245}))
            return _resp(200, {"prisms": [self._prism("P1", 245)]})

        with mock.patch.object(caf, "feed_settings", return_value=("", "tok")), \
                mock.patch.object(caf.requests, "get", side_effect=grouped):
            self.assertEqual(caf.write_fair_file(out, now=NOW, series_map=smap,
                                                 vintage_path=vint), (1, 0))

    def test_fetch_sends_bearer_and_checks_shape(self):
        resp = mock.Mock()
        resp.json.return_value = {"prisms": []}
        with mock.patch.object(caf.requests, "get", return_value=resp) as g:
            self.assertEqual(caf.fetch_payload("https://x.invalid/p", "tok"),
                             {"prisms": []})
            self.assertEqual(g.call_args.kwargs["headers"]["Authorization"],
                             "Bearer tok")
        resp.json.return_value = {"nope": 1}
        with mock.patch.object(caf.requests, "get", return_value=resp):
            with self.assertRaises(ValueError):
                caf.fetch_payload("https://x.invalid/p", "")


class TestSchedules(unittest.TestCase):
    """The panels' update schedules (Jack 2026-10-15: "do 2 and 3", option
    2): GET /v2/library/schedule/<dataset> with the account token, written
    to the fair file for incentive_mm's scheduled-data stand-aside."""

    BASE = "https://api.carbonarc.co/v2/library/schedule"

    @staticmethod
    def sched(now, start_h=24.0, end_h=24.5, last_h=-4.0):
        iso = lambda h: (now + timedelta(hours=h)).isoformat()   # noqa: E731
        return {"schedules": [{"next_run_start": iso(start_h),
                               "next_run_end": iso(end_h),
                               "last_update": iso(last_h)}]}

    def test_defaults(self):
        self.assertEqual(caf.CA_SCHEDULE_DATASETS, {
            "credit card": "CA0056", "point of sale": "CA0029",
            "foot traffic": "CA0060", "app": "CA0054", "advertising": "CA009"})

    def test_fetch_reads_each_panel_with_the_token(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, headers))
            return _resp(200, {"schedules": [{
                "next_run_start": "2026-10-16T19:30:00+00:00",
                "next_run_end": "2026-10-16T20:00:00+00:00",
                "last_update": "2026-10-15T15:40:27+00:00"}]})

        with mock.patch.object(caf.requests, "get", side_effect=fake_get):
            out = caf.fetch_schedules("tok", {"credit card": "CA0056",
                                              "advertising": "CA009"})
        self.assertEqual(out["credit card"], {
            "dataset": "CA0056", "next_run_start": "2026-10-16T19:30:00+00:00",
            "next_run_end": "2026-10-16T20:00:00+00:00",
            "last_update": "2026-10-15T15:40:27+00:00"})
        self.assertEqual(sorted(u for u, _h in calls),
                         [self.BASE + "/CA0056", self.BASE + "/CA009"])
        for _u, h in calls:
            self.assertEqual(h["Authorization"], "Bearer tok")
        with mock.patch.object(caf.requests, "get", return_value=_resp(500, {})):
            with self.assertRaises(RuntimeError):
                caf.fetch_schedules("tok", {"credit card": "CA0056"})

    def test_refresh_cadence_and_failures(self):
        reads = []

        def fetch(tok):
            reads.append(tok)
            return {c: dict(self.sched(NOW)["schedules"][0], dataset=d)
                    for c, d in caf.CA_SCHEDULE_DATASETS.items()}

        self.assertEqual(caf.refresh_schedules({}, "", NOW, fetch), ({}, 0.0))
        self.assertEqual(reads, [])                          # no token: no read
        s, at = caf.refresh_schedules({}, "tok", NOW, fetch)
        self.assertEqual((len(reads), at), (1, NOW.timestamp()))
        prev = {"schedules": s, "schedules_ts": at}
        # inside the interval, every window still ahead: kept
        self.assertEqual(caf.refresh_schedules(prev, "tok", NOW + timedelta(minutes=5),
                                               fetch), (s, at))
        self.assertEqual(len(reads), 1)
        # a window closed: re-read once the 2-minute recheck has passed
        closed = {c: dict(v, next_run_end=(NOW - timedelta(minutes=1)).isoformat())
                  for c, v in s.items()}
        caf.refresh_schedules({"schedules": closed, "schedules_ts": at}, "tok",
                              NOW + timedelta(minutes=1), fetch)
        self.assertEqual(len(reads), 1)
        caf.refresh_schedules({"schedules": closed, "schedules_ts": at}, "tok",
                              NOW + timedelta(minutes=3), fetch)
        self.assertEqual(len(reads), 2)
        # past the interval: re-read
        caf.refresh_schedules(prev, "tok", NOW + timedelta(
            seconds=caf.CA_SCHEDULE_REFRESH_SECS), fetch)
        self.assertEqual(len(reads), 3)

        def down(tok):
            raise RuntimeError("HTTP 503")
        self.assertEqual(caf.refresh_schedules(prev, "tok", NOW + timedelta(hours=2),
                                               down), (s, at))   # kept

    def test_a_handed_in_payload_reads_no_schedules(self):
        tmp = tempfile.mkdtemp(prefix="caf_sched_")
        out = os.path.join(tmp, "fair.json")
        payload = {"prisms": [_prism("P1", [_entity("Amazon", [("2026-09-23", 108.64)],
                                                    HIST)])]}
        with mock.patch.object(caf, "feed_settings", return_value=("", "tok")), \
                mock.patch.object(caf.requests, "get",
                                  side_effect=AssertionError("no network")):
            caf.write_fair_file(out, payload=payload, now=NOW,
                                series_map={"KXAMZNCC": {"prism": "P1", "entity": "Amazon"}},
                                vintage_path=os.path.join(tmp, "v.jsonl"))
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["schedules"], data["schedules_ts"]), ({}, 0.0))


class TestSignedSeriesCatalog(unittest.TestCase):
    """incentive_mm's signed Kalshi reader (2026-09-27): the series catalog
    read goes through it first, the public endpoint is only the fallback."""

    URL = "https://www.carbonarc.co/prisms?prism=abc-123&entity=Amazon"
    CATALOG = {"series": [
        {"ticker": "KXAMZNCC", "settlement_sources": [{"name": "Carbon Arc", "url": URL}]},
        {"ticker": "KXBAA", "settlement_sources": [{"url": "https://fiscal.ai"}]}]}

    def setUp(self):
        p = mock.patch.object(caf, "_signed_err", None)
        p.start()
        self.addCleanup(p.stop)

    def test_signed_read_first(self):
        seen = []

        def get_json(path, params):
            seen.append((path, params))
            return self.CATALOG

        with mock.patch.object(caf.requests, "get",
                               side_effect=AssertionError("public read")):
            m = caf.fetch_series_map(get_json=get_json)
        self.assertEqual(seen, [("/series", {})])
        self.assertEqual(m, {"KXAMZNCC": {"prism": "abc-123", "entity": "Amazon"}})

    def test_signed_failure_falls_back_to_the_public_catalog(self):
        resp = mock.Mock()
        resp.json.return_value = self.CATALOG
        resp.raise_for_status.return_value = None
        with mock.patch.object(caf.requests, "get", return_value=resp) as g, \
                mock.patch.object(caf, "_log") as lg:
            m = caf.fetch_series_map(get_json=mock.Mock(side_effect=OSError("reset")))
        g.assert_called_once()
        lg.assert_called_once()
        self.assertEqual(list(m), ["KXAMZNCC"])

    @mock.patch.object(caf, "CA_SERIES_MAP_EXTRA", {})    # the catalog alone
    def test_write_fair_file_hands_the_reader_to_the_catalog_read(self):
        tmp = tempfile.mkdtemp(prefix="caf_signed_")
        payload = {"prisms": [
            _prism("P1", [_entity("Amazon", [("2026-09-23", 108.64)], HIST)])]}
        reader = mock.Mock()
        with mock.patch.object(caf, "fetch_series_map", return_value={
                "KXAMZNCC": {"prism": "P1", "entity": "Amazon"}}) as fm:
            ok, miss = caf.write_fair_file(
                os.path.join(tmp, "carbon_arc_fair.json"), payload=payload, now=NOW,
                vintage_path=os.path.join(tmp, "vint.jsonl"), get_json=reader)
        fm.assert_called_once_with(get_json=reader)
        self.assertEqual((ok, miss), (1, 0))


if __name__ == "__main__":
    unittest.main()
