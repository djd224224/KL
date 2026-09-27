"""Tests for vercel_fair (the pre-D Vercel gate's fair values)."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import vercel_fair as vf


def history(lab_metric, days, start=date(2026, 7, 20), base=20.0, step=None):
    """A synthetic export table: one (lab, metric) series of `days` values."""
    vals = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        vals[d] = base + (step(i) if step else 0.0)
    return {lab_metric: vals}


class TestTickersAndStrikes(unittest.TestCase):
    def test_measured_day_by_series(self):
        self.assertEqual(vf.measured_day("KXOPENVREQ-28SEP26"), date(2026, 9, 28))
        self.assertEqual(vf.measured_day("KXOPENVREQ-05OCT26"), date(2026, 10, 5))
        # open-weights: YYMMMDD is the day AFTER D
        self.assertEqual(vf.measured_day("KXOPENSOURCESHARE-26SEP29"), date(2026, 9, 28))
        self.assertIsNone(vf.measured_day("KXOPENVREQ-31SEP26"))
        self.assertIsNone(vf.measured_day("KXFOO-28SEP26"))
        self.assertIsNone(vf.measured_day("KXOPENVREQ"))

    def test_event_ticker_round_trip(self):
        for ev in ("KXOPENVREQ-28SEP26", "KXANTHVSPEND-05OCT26", "KXOPENSOURCESHARE-26SEP29"):
            s = ev.split("-")[0]
            self.assertEqual(vf.event_ticker_for(s, vf.measured_day(ev)), ev)

    def test_strike_and_rounding_threshold(self):
        self.assertEqual(vf.strike_of("KXANTHVREQ-28SEP26-T5P5"), 5.5)
        self.assertEqual(vf.strike_of("KXOPENVREQ-28SEP26-T21"), 21.0)
        self.assertEqual(vf.strike_of("KXOPENSOURCESHARE-26SEP29-T67.5"), 67.5)
        self.assertIsNone(vf.strike_of("KXOPENVREQ-28SEP26-B21"))
        # strictly above K after rounding to 1 dp (half up)
        self.assertAlmostEqual(vf.yes_threshold(5.5), 5.55)
        self.assertAlmostEqual(vf.yes_threshold(21.0), 21.05)
        self.assertAlmostEqual(vf.yes_threshold(72.25), 72.25)


class TestFairModel(unittest.TestCase):
    def test_entry_uses_h_day_changes_widened(self):
        # alternating +1 / -1 day to day -> every 2-day change is 0, every
        # 1-day change +-1
        tab = history(("openai", "requests"), 80, step=lambda i: 1.0 if i % 2 else 0.0)
        dates = sorted(tab[("openai", "requests")])
        x = vf.series_values(tab, "KXOPENVREQ", dates)
        last = date.fromisoformat(dates[-1])
        e2 = vf.fair_entry(x, last + timedelta(days=2), last, widen=1.5)
        self.assertEqual(e2["h"], 2)
        self.assertEqual(set(e2["errs"]), {0.0})
        e1 = vf.fair_entry(x, last + timedelta(days=1), last, widen=1.5)
        self.assertEqual(set(e1["errs"]), {-1.5, 1.5})             # +-1 widened
        self.assertIsNone(vf.fair_entry(x, last, last, widen=1.5))  # h < 1
        short = {d: v for d, v in list(x.items())[-10:]}
        self.assertIsNone(vf.fair_entry(short, last + timedelta(days=2), last, 1.5))

    def test_p_yes_counts_the_sample(self):
        entry = {"x_l": 20.0, "errs": [-1.0, -0.5, 0.0, 0.5, 1.0] * 4}
        self.assertAlmostEqual(vf.p_yes(entry, 20.0), 8 / 20)   # needs >= 20.05
        self.assertAlmostEqual(vf.p_yes(entry, 19.0), 16 / 20)  # 19.0 is not > 19
        self.assertAlmostEqual(vf.p_yes(entry, 18.9), 0.99)     # all -> clipped
        self.assertAlmostEqual(vf.p_yes(entry, 25.0), 0.01)

    def test_open_weights_sum(self):
        tab = {("deepseek", "tokens"): {"2026-09-01": 30.0},
               ("zai", "tokens"): {"2026-09-01": 20.0},
               ("openai", "tokens"): {"2026-09-01": 40.0}}
        x = vf.series_values(tab, "KXOPENSOURCESHARE", ["2026-09-01"])
        self.assertEqual(x["2026-09-01"], 50.0)

    def test_write_fair_file_keys_events_by_ticker(self):
        tab = {}
        for s, (lab, met) in vf.LAB_SERIES.items():
            tab.update(history((lab, met), 80, start=date(2026, 7, 10), base=20.0,
                               step=lambda i: (i % 5) * 0.3))
        for lab in vf.OPEN_WEIGHT_LABS[:2]:
            tab.update(history((lab, "tokens"), 80, start=date(2026, 7, 10), base=30.0))
        now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
        path = os.path.join(tempfile.mkdtemp(), "vf.json")
        n, miss = vf.write_fair_file(path, now=now, tab=tab)
        self.assertEqual((n, miss), (8 * vf.HORIZON_DAYS, 0))
        data = json.load(open(path, encoding="utf-8"))
        e = data["entries"]["KXOPENVREQ-28SEP26"]
        self.assertEqual((e["d"], e["last"], e["h"]), ("2026-09-28", "2026-09-26", 2))
        self.assertIn("KXOPENSOURCESHARE-26SEP29", data["entries"])
        self.assertEqual(data["model"]["lab_widen"], vf.LAB_WIDEN)

    def test_fetch_uses_a_fresh_distinct_to(self):
        r = mock.Mock()
        r.json.return_value = {"rows": [
            {"date": "2026-09-26", "name": "openai", "metric": "requests", "share_percent": 18.9},
            {"date": "2026-09-26", "name": "openai", "metric": "imageCount", "share_percent": 50}]}
        r.raise_for_status.return_value = None
        now = datetime(2026, 9, 27, 18, 7, tzinfo=timezone.utc)
        with mock.patch.object(vf.requests, "get", return_value=r) as g:
            tab = vf.fetch_labs(now)
        params = g.call_args.kwargs["params"]
        self.assertEqual((params["dataset"], params["modality"]), ("labs", "all"))
        self.assertGreater(params["to"], "2026-09-27")            # a distinct cache key
        self.assertEqual(tab, {("openai", "requests"): {"2026-09-26": 18.9}})
        r.json.return_value = {"rows": []}
        with mock.patch.object(vf.requests, "get", return_value=r):
            with self.assertRaises(RuntimeError):
                vf.fetch_labs(now)


if __name__ == "__main__":
    unittest.main()
