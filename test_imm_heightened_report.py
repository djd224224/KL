#!/usr/bin/env python3
"""Tests for imm_heightened_report.py (the heightened bait mode's dry-run
score). Run: python -m unittest test_imm_heightened_report"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import imm_heightened_report as R

DAY0 = datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp()
A, B = "KXEVT-26NOV14-A", "KXEVT-26NOV14-B"
EV = "KXEVT-26NOV14"


class TestVerdict(unittest.TestCase):

    def test_the_pre_registered_lines(self):
        self.assertEqual(R.verdict(3, 1, 50, 1)[0], "TOO EARLY")
        self.assertEqual(R.verdict(7, 31, 50, 1)[0], "STOP")        # too noisy
        self.assertEqual(R.verdict(7, 5, 10, 8)[0], "GO")
        self.assertEqual(R.verdict(7, 5, 4, 3)[0], "WATCH")          # under $5/day
        self.assertEqual(R.verdict(7, 5, 3, 10)[0], "WATCH")
        self.assertEqual(R.verdict(7, 5, 2, 10)[0], "STOP")


class TestReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = self.tmp.name
        t0 = DAY0 + 3600 * 5
        self.t0 = t0
        hz = [
            {"ev": "pull", "ts": t0 - 100, "ticker": A, "side": "bid", "joined": True},
            {"ev": "trip", "ts": t0, "key": "m|" + A, "scope": "market", "ticker": A,
             "event": EV, "until": t0 + 3600, "exempt": None},
            {"ev": "trip", "ts": t0, "key": "e|" + EV, "scope": "event", "ticker": A,
             "event": EV, "until": t0 + 3600, "exempt": None},
            {"ev": "fill", "ts": t0 + 60, "ticker": A, "side": "bid", "px": 55.0,
             "count": 100.0, "order_id": "o1", "scope": "market", "exempt": None},
            {"ev": "clear", "ts": t0 + 1800, "key": "m|" + A, "scope": "market",
             "since": t0, "until": t0 + 1800},
            {"ev": "clear", "ts": t0 + 1800, "key": "e|" + EV, "scope": "event",
             "since": t0, "until": t0 + 1800},
        ]
        with open(os.path.join(d, "heightened_2026-11-14.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in hz) + "\n")
        fills = [{"ts": t0 + 60, "ticker": A, "count": 100.0, "is_taker": False},
                 {"ts": t0 + 7200, "ticker": A, "count": 50.0, "is_taker": False}]
        with open(os.path.join(d, "fills_2026-11-14.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in fills) + "\n")
        os.makedirs(os.path.join(d, "dashboard"))
        D = {"fills": [{"ts": int(t0 + 60), "t": A, "n": 100.0, "m5c": -10.0, "m4c": -12.0},
                       {"ts": int(t0 + 7200), "t": A, "n": 50.0, "m5c": 1.0, "m4c": 2.0}]}
        with open(os.path.join(d, "dashboard", "imm_dashboard.html"), "w") as f:
            f.write("<script>\nconst D = " + json.dumps(D) + ";\n</script>\n")
        h = int(t0 // 3600)
        cache = {"2026-11-14": {"markets": {A: {str(h): 2.0}, B: {str(h): 1.0}}}}
        with open(os.path.join(d, "reward_est_hourly_cache.json"), "w") as f:
            json.dump(cache, f)

    def test_lost_forgone_and_inside_vs_outside(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            R.main(["--dir", self.d, "--since", "2026-11-14", "--until", "2026-11-14"])
        path = buf.getvalue().strip()
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("**Verdict: TOO EARLY**", txt)
        # 100 contracts at -12c = $12 lost; the event window held A and B for
        # 30 min at $3/hour = $1.50 (the market window overlaps, counted once)
        self.assertIn("lost **$12.00** at 4 h", txt)
        self.assertIn("against **$1.50** of reward", txt)
        self.assertIn("marked -12.00c a contract at 4 h (1 fills", txt)
        self.assertIn("everywhere else +2.00c (1 fills", txt)
        self.assertIn("Pulls: 1 (1.0 a day; our rung had followed 1)", txt)

    def test_windows_close_at_their_clear(self):
        rows = R.load_rows(self.d, datetime(2026, 11, 14).date(),
                           datetime(2026, 11, 14).date())
        wins = R.windows_of(rows, self.t0 + 10 ** 6)
        self.assertEqual(sorted((w["key"], w["until"] - w["since"]) for w in wins),
                         [("e|" + EV, 1800.0), ("m|" + A, 1800.0)])
        self.assertTrue(R.covers(wins[0], B) or R.covers(wins[1], B))


if __name__ == "__main__":
    unittest.main()
