"""vercel_blend_fit.py: the VERCEL FAIR BLEND weights (Jack 2026-11-25:
"Yes, build the blend")."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock


def _row(ev, fair, mid, y):
    return {"ev": ev, "fair": fair, "mid": mid, "y": y}


class TestBlendFit(unittest.TestCase):
    def setUp(self):
        import vercel_blend_fit
        self.m = vercel_blend_fit

    def test_the_slope_through_the_origin(self):
        # outcome - mid = 0.5 (fair - mid) on every row
        rows = [_row("a", 60.0, 40.0, 0.5), _row("b", 30.0, 50.0, 0.4)]
        self.assertAlmostEqual(self.m.slope(rows), 0.5)
        self.assertIsNone(self.m.slope([_row("a", 50.0, 50.0, 1.0)]))

    def test_fit_rows_clips_and_counts_events(self):
        rows = [_row(f"e{i}", 60.0, 40.0, 1.0) for i in range(5)]   # w = 3
        f = self.m.fit_rows(rows, boot=50)
        self.assertEqual((f["w"], f["w_raw"], f["events"], f["rows"]), (1.0, 3.0, 5, 5))
        neg = [_row(f"e{i}", 60.0, 40.0, 0.0) for i in range(5)]     # w = -2
        self.assertEqual(self.m.fit_rows(neg, boot=50)["w"], 0.0)
        self.assertIsNone(self.m.fit_rows([]))
        self.assertIsNone(self.m.fit_rows([_row("a", 50.0, 50.0, 1.0)]))

    def test_the_brier_and_the_band(self):
        # half the events settle YES at a 70c fair over a 50c mid: w = 0
        rows = ([_row(f"y{i}", 70.0, 50.0, 1.0) for i in range(10)]
                + [_row(f"n{i}", 70.0, 50.0, 0.0) for i in range(10)])
        f = self.m.fit_rows(rows, boot=200)
        self.assertAlmostEqual(f["w_raw"], 0.0)
        self.assertAlmostEqual(f["brier_mid"], 0.25)
        self.assertAlmostEqual(f["brier_fair"], 0.29)
        self.assertAlmostEqual(f["brier_blend"], 0.25)              # w 0: the mid
        self.assertLessEqual(f["lo"], f["w_raw"])
        self.assertGreaterEqual(f["hi"], f["w_raw"])
        # one event's rows count as one event
        many = [_row("one", 70.0, 50.0, 1.0)] * 9 + [_row("two", 70.0, 50.0, 0.0)]
        self.assertAlmostEqual(self.m.fit_rows(many, boot=10)["brier_mid"], 0.25)

    def test_at_or_before(self):
        xs = [(100.0, 1), (200.0, 2)]
        self.assertEqual(self.m.at_or_before(xs, 250.0, 60), (200.0, 2))
        self.assertIsNone(self.m.at_or_before(xs, 300.0, 60))      # too old
        self.assertIsNone(self.m.at_or_before(xs, 50.0, 60))       # none yet

    def test_the_replay_keeps_the_strikes_the_blend_is_used_on(self):
        vf = self.m.vf
        ev = "KXDEEPVREQ-05DEC68"                 # D = 2068-12-05
        d0 = datetime(2068, 12, 5, tzinfo=timezone.utc)
        markets = {f"{ev}-T20": {"event": ev, "result": "yes"},
                   f"{ev}-T30": {"event": ev, "result": "no"},     # fair 1c: decided
                   f"{ev}-T21": {"event": ev, "result": "no"}}     # a one-sided book
        quotes = {}
        T = d0 - timedelta(days=self.m.LEAD_DAYS)
        while T <= d0:
            ts = T.timestamp()
            quotes.setdefault(f"{ev}-T20", []).append((ts - 30, 45.0, 55.0))
            quotes.setdefault(f"{ev}-T30", []).append((ts - 30, 20.0, 30.0))
            quotes.setdefault(f"{ev}-T21", []).append((ts - 30, 0.0, 55.0))
            T += timedelta(hours=1)
        entry = {"x_l": 20.0, "errs": [-1.0, -0.5, 0.0, 0.5, 1.0] * 12}
        running = mock.Mock()
        running.at.return_value = 20.0
        with mock.patch.object(vf, "run_entry", return_value=entry), \
                mock.patch.object(vf, "fair_entry", return_value=entry), \
                mock.patch.object(vf, "delta_samples", return_value=[0.0] * 5):
            rows = self.m.replay_series("KXDEEPVREQ", {}, markets, quotes, running,
                                        lambda d: {})
        self.assertTrue(rows)
        self.assertEqual({r["t"] for r in rows}, {f"{ev}-T20"})
        self.assertTrue(all(r["mid"] == 50.0 and abs(r["fair"] - 40.0) < 1e-9
                            and r["y"] == 1.0 for r in rows))
        # hourly from D - 4 days to the cutoff, D 00:00Z - 60 min
        cut = (d0 - timedelta(minutes=self.m.CUTOFF_MIN)).timestamp()
        self.assertLessEqual(max(r["ts"] for r in rows), cut)
        self.assertEqual(len(rows), self.m.LEAD_DAYS * 24)
        # no running share past 01:00Z: no row (the gate fails closed)
        running.at.return_value = None
        with mock.patch.object(vf, "run_entry", return_value=entry), \
                mock.patch.object(vf, "fair_entry", return_value=entry), \
                mock.patch.object(vf, "delta_samples", return_value=[0.0] * 5):
            rows = self.m.replay_series("KXDEEPVREQ", {}, markets, quotes, running,
                                        lambda d: {})
        self.assertEqual(len(rows), self.m.LEAD_DAYS)              # the 00:00Z hours
        # a quote more than 5 min old is no quote
        stale = {t: [(ts - 400, b, a) for ts, b, a in v] for t, v in quotes.items()}
        running.at.return_value = 20.0
        with mock.patch.object(vf, "run_entry", return_value=entry), \
                mock.patch.object(vf, "fair_entry", return_value=entry), \
                mock.patch.object(vf, "delta_samples", return_value=[0.0] * 5):
            self.assertEqual(self.m.replay_series("KXDEEPVREQ", {}, markets, stale,
                                                  running, lambda d: {}), [])

    def test_the_file_is_written_whole(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vercel_blend.json")
            self.m.write_atomic(p, {"series": {"KXDEEPVREQ": {"w": 0.26}}})
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["series"]["KXDEEPVREQ"]["w"], 0.26)
            self.assertFalse(os.path.exists(p + ".tmp"))


if __name__ == "__main__":
    unittest.main()
