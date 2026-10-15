"""Tests for yt_top1_fair (P(each listed video is #1 on the Global daily
chart) from live view counts, for incentive_mm's #1 leader-bid pilot)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import yt_top1_fair as F

DAY = date(2026, 10, 10)
EV = "KXYTDAILYTOPVIDEOG-26OCT10"
T0 = F.window_start(DAY)


def steady(rate_per_day, days_before=4, until=None, step=1200):
    """A video gaining views at an even rate: ([ts], [views])."""
    until = T0 + 24 * 3600 if until is None else until
    t, y, ts = [], [], T0 - days_before * 86400
    while ts <= until:
        t.append(ts)
        y.append(1e8 + rate_per_day * (ts - T0) / 86400)
        ts += step
    return t, y


def truncate(series, now):
    import bisect
    return {v: (t[:bisect.bisect_right(t, now)], y[:bisect.bisect_right(t, now)])
            for v, (t, y) in series.items()}


class TestModel(unittest.TestCase):
    def test_the_event_and_its_window(self):
        self.assertEqual(F.parse_event(EV), DAY)
        self.assertIsNone(F.parse_event("KXYTTOPVIDEOG2D-26OCT10"))
        self.assertIsNone(F.parse_event("KXYTDAILYTOPVIDEOG-26XXX10"))
        self.assertEqual(T0, datetime(2026, 10, 10, 1, 30, tzinfo=timezone.utc).timestamp())

    def test_a_steady_video_has_an_even_share(self):
        t, y = steady(2.4e6)
        self.assertAlmostEqual(F.own_share(t, y, DAY, 6.0), 0.25, places=3)
        self.assertIsNone(F.own_share(*steady(2.4e6, days_before=0), DAY, 6.0))

    def test_a_clear_leader_and_a_close_race(self):
        series = {"dai": steady(30e6), "cas": steady(18e6), "xam": steady(17e6),
                  "f1": steady(2e6), "f2": steady(1e6)}
        codes = {"DAI": "dai", "CAS": "cas", "XAM": "xam"}
        now = T0 + 12 * 3600
        e, why = F.event_fair(EV, codes, truncate(series, now), now, {})
        self.assertEqual(why, "")
        self.assertEqual(e["leader"], "DAI")
        self.assertGreater(e["p"], 0.8)                     # 30M vs 18M and 17M: sd ~0.27 each at h12 without ratios
        self.assertAlmostEqual(e["codes"]["DAI"]["proj"], 30e6, delta=1e5)
        self.assertTrue(e["top_listed"])
        self.assertAlmostEqual(e["h"], 12.0, places=2)
        # CAS vs XAM is a toss-up between themselves
        self.assertLess(abs(e["codes"]["CAS"]["p"] - e["codes"]["XAM"]["p"]), 0.06)
        # early in the day the same counts are far less sure
        now = T0 + 3 * 3600
        e3, _ = F.event_fair(EV, codes, truncate(series, now), now, {})
        self.assertLess(e3["p"], e["p"])
        # ... and by the end of the window surer still: no projection error
        # left, only the chart/API ratio's
        now = T0 + 24 * 3600
        e24, _ = F.event_fair(EV, codes, truncate(series, now), now, {})
        self.assertGreater(e24["p"], max(e["p"], 0.9))
        self.assertLess(e24["p"], 1.0)

    def test_an_unlisted_video_can_take_it(self):
        series = {"dai": steady(20e6), "newrel": steady(40e6), "cas": steady(10e6)}
        now = T0 + 18 * 3600
        e, _ = F.event_fair(EV, {"DAI": "dai", "CAS": "cas"}, truncate(series, now), now, {})
        self.assertEqual(e["leader"], "DAI")                 # the best LISTED code
        self.assertLess(e["p"], 0.05)                        # but the threat wins
        self.assertFalse(e["top_listed"])
        self.assertEqual(e["threat"]["id"], "newrel")
        self.assertGreater(e["threat"]["p"], 0.95)

    def test_ratios_move_the_ranking(self):
        series = {"a": steady(20e6), "b": steady(19e6)}
        now = T0 + 23.5 * 3600
        e, _ = F.event_fair(EV, {"A": "a", "B": "b"}, truncate(series, now), now, {"a": 1.0, "b": 1.4})
        self.assertEqual(e["leader"], "B")                   # 19M x 1.4 > 20M x 1.0

    def test_no_entry_too_early_or_unmapped(self):
        series = {"dai": steady(30e6)}
        now = T0 + 1.0 * 3600
        e, why = F.event_fair(EV, {"DAI": "dai"}, truncate(series, now), now, {})
        self.assertIsNone(e)
        self.assertIn("< 2", why)
        now = T0 + 6 * 3600
        e, why = F.event_fair(EV, {"DAI": None}, truncate(series, now), now, {})
        self.assertIsNone(e)
        self.assertIn("no candidate", why)
        e, why = F.event_fair("KXYTDAILYTOPVIDEOG-XYZ", {"DAI": "dai"}, series, now, {})
        self.assertIsNone(e)


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.a = os.path.join(self.dir, "a.jsonl")
        self.b = os.path.join(self.dir, "b.jsonl")
        self.ids = os.path.join(self.dir, "ids.json")
        self.out = os.path.join(self.dir, "fair.json")
        self.patches = [mock.patch.object(F, "SNAP_FILES", [self.a, self.b]),
                        mock.patch.object(F, "IDS_FILE", self.ids),
                        mock.patch.object(F, "RATIO_FILE", os.path.join(self.dir, "none.csv")),
                        mock.patch.object(F, "N_SIM", 500)]
        for p in self.patches:
            p.start()
        F._snap.update(files={}, series={}, last_ts=0.0)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        F._snap.update(files={}, series={}, last_ts=0.0)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, path, rows, mode="a"):
        with open(path, mode, encoding="utf-8") as f:
            for ts, vid, v in rows:
                f.write(json.dumps({"ts": ts, "id": vid, "views": v}) + "\n")

    def test_reads_only_what_was_appended_and_merges_both_files(self):
        self.write(self.a, [(100.0, "x", 10), (300.0, "x", 30)])
        self.write(self.b, [(200.0, "x", 20)])
        series, last = F.load_snapshots()
        self.assertEqual(series["x"], ([100.0, 200.0, 300.0], [10.0, 20.0, 30.0]))
        self.assertEqual(last, 300.0)
        self.write(self.a, [(400.0, "x", 40), (200.0, "x", 20)])   # the other file's time: skipped
        series, last = F.load_snapshots()
        self.assertEqual(series["x"][0], [100.0, 200.0, 300.0, 400.0])
        self.write(self.a, [(50.0, "y", 1)], mode="w")              # shrunk: read from scratch
        series, _ = F.load_snapshots()
        self.assertEqual(set(series), {"x", "y"})

    def test_the_fair_file_fails_closed_on_stale_data(self):
        now = T0 + 12 * 3600
        rows = []
        for vid, rate in (("dai", 30e6), ("cas", 15e6)):
            t, y = steady(rate, until=now)
            rows += [(ts, vid, v) for ts, v in zip(t, y)]
        self.write(self.a, rows)
        with open(self.ids, "w", encoding="utf-8") as f:
            json.dump({"events": {EV: {"DAI": {"id": "dai"}, "CAS": {"id": "cas"}},
                                  "KXYTDAILYTOPVIDEOG-26OCT01": {"DAI": {"id": "dai"}}}}, f)
        out = F.write_fair_file(self.out, now)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(list(out["entries"]), [EV])             # 10/01 is long closed
        self.assertEqual(out["entries"][EV]["leader"], "DAI")
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["entries"][EV]["leader"], "DAI")
        out = F.write_fair_file(self.out, now + F.API_STALE_SECS + 60)
        self.assertEqual(out["status"], "stale_api")
        self.assertEqual(out["entries"], {})


if __name__ == "__main__":
    unittest.main()
