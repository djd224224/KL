"""Tests for carbon_arc_fair.py (Carbon Arc month-to-date -> first-print
fair values for incentive_mm's Carbon Arc gate, 2026-09-26)."""

import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
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
        # the last MTD point defines the observed stretch, whatever order
        # the feed lists them in
        e2 = _entity("Amazon", [("2026-09-23", 108.64), ("2026-09-22", 1.0)],
                     HIST)
        self.assertAlmostEqual(caf.entity_entry(p, e2, NOW)["mu"], 108.64)

    def test_entry_refuses_thin_or_stale_reads(self):
        p = _prism("P", [])
        # 2 days of a 30-day month < CA_MIN_OBS_FRAC
        thin = _entity("X", [("2026-09-02", 101.0)], HIST)
        self.assertIsNone(caf.entity_entry(p, thin, NOW))
        # a read 11+ days old is not the month in progress
        old = _entity("X", [("2026-09-14", 101.0)], HIST)
        self.assertIsNone(caf.entity_entry(p, old, NOW))
        self.assertIsNone(caf.entity_entry(p, _entity("X", [], HIST), NOW))
        # too little history -> the default relative spread
        short = _entity("X", [("2026-09-23", 100.0)], HIST[-2:])
        out = caf.entity_entry(p, short, NOW)
        self.assertAlmostEqual(out["sigma_m"], caf.CA_SIGMA_M_DEFAULT_REL * 100.0)

    def test_p_above(self):
        self.assertAlmostEqual(caf.p_above(100.0, 100.0, 2.0), 0.5)
        self.assertAlmostEqual(caf.p_above(102.0, 100.0, 2.0), 0.158655, 5)
        self.assertEqual(caf.p_above(99.0, 100.0, 0.0), 1.0)


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
                mock.patch.dict(os.environ, {"IMM_CA_FEED_URL": ""}):
            self.assertEqual(caf.feed_settings(), ("", ""))

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


if __name__ == "__main__":
    unittest.main()
