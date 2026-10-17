"""Tests for imm_risk_dry_report.py (the 3-day report on the cross-event
breaker and the dollar limits + skew, Jack 2026-10-17: "#2 and #4/#5: #2
log-only, plus #4 and #5 together as one log-only build. Email report in 3
days"). Synthetic logs in a temp dir; nothing under run-logs is read."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import imm_risk_dry_report as rep


class TestRiskDryReport(unittest.TestCase):
    T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, "dashboard"))
        self.grp, self.risk, self.fills, self.dfills = [], [], [], []
        self.n = 0

    def _fill(self, ts, t, side, n, m4c, m5c=0.0):
        self.n += 1
        fid = f"tr{self.n}"
        self.fills.append({"ts": int(ts), "ticker": t, "our_book_side": side,
                           "count": n, "order_id": f"o{self.n}", "fill_id": fid,
                           "is_taker": False})
        self.dfills.append({"ts": int(ts), "t": t, "n": n, "m4c": m4c, "m5c": m5c})
        return fid

    def _write(self):
        day = datetime.fromtimestamp(self.T0, timezone.utc).date().isoformat()
        for name, rows in (("ws_group", self.grp), ("risk_dry", self.risk),
                           ("fills", self.fills)):
            with open(os.path.join(self.d, f"{name}_{day}.jsonl"), "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.d, "dashboard", "imm_dashboard.html"), "w") as f:
            f.write("<script>\nconst D = " + json.dumps({"fills": self.dfills}) + ";\n</script>\n")

    def _run(self):
        self._write()
        out = []
        orig = rep.finish
        rep.finish = lambda d, lines: out.append("\n".join(lines)) or 0
        try:
            rep.main(["--dir", self.d, "--since", "2026-10-18T11:00:00Z",
                      "--until", "2026-10-18T14:00:00Z"])
        finally:
            rep.finish = orig
        return out[0]

    def test_the_group_breaker_scores_held_fills_against_the_held_reward(self):
        t = self.T0
        for i in range(2):
            self.grp.append({"ev": "trip", "ts": t + i * 3000, "mode": "dry",
                             "group": "NFL game 26OCT19DALWSH", "side": "bid",
                             "hold_s": 1800.0, "side_day": 2.0})
        fid = self._fill(t + 100, "KXNFLA-26OCT19DALWSH-X", "bid", 100, -10.0, -4.0)
        for dt in (0.2, 0.3):                   # the feed sent the fill twice: counted once
            self.grp.append({"ev": "held_fill", "ts": t + 100 + dt, "mode": "dry",
                             "group": "NFL game 26OCT19DALWSH", "side": "bid",
                             "trade_id": fid})
        out = self._run()
        # $10 lost at 4h kept out, for 2 x 2.0/day x 30 min = $0.08 of reward
        self.assertIn("| NFL game 26OCT19DALWSH | 2 | 1 | 100 | -$10.00 | -$4.00 | $0.08 | $9.92 |", out)
        self.assertIn("WATCH (only 2 trips: too few to call)", out)
        self.assertIn("Net $9.92 over the window", out)

    def test_block_and_skew_spells_score_their_fills_and_reward(self):
        t = self.T0
        self.risk += [
            {"ev": "start", "ts": t},
            {"ev": "state", "ts": t + 100, "ticker": "KXA-1", "side": "bid",
             "state": "block", "prev": "ok", "by": "event", "event": "KXA",
             "group": None, "side_day": 1.0, "fit": 0.5},
            {"ev": "state", "ts": t + 200, "ticker": "KXB-1", "side": "ask",
             "state": "skew", "prev": "ok", "by": "group", "event": "KXB",
             "group": "Carbon Arc credit card", "side_day": 2.0},
            {"ev": "pass", "ts": t + 300, "block": 1, "skew": 1, "events": 2,
             "worst_total": -300.0, "exempt_worst": -50.0, "block_day": 1.0,
             "skew_day": 2.0, "groups": {}, "top_events": []},
            {"ev": "state", "ts": t + 3700, "ticker": "KXA-1", "side": "bid",
             "state": "none", "prev": "block"},
            {"ev": "pass", "ts": t + 3800, "block": 0, "skew": 1, "events": 2,
             "worst_total": -300.0, "exempt_worst": -50.0, "block_day": 0.0,
             "skew_day": 2.0, "groups": {"Carbon Arc credit card": {
                 "worst": -900.0, "limit": 750.0, "day": 10.0, "long": 100.0,
                 "short": 800.0}},
             "top_events": [[1.2, "KXA", -180.0, 150.0]]},
            {"ev": "start", "ts": t + 4000}]          # a restart ends the skew
        self._fill(t + 500, "KXA-1", "bid", 50, -20.0)     # in the block: -$10
        self._fill(t + 600, "KXB-1", "ask", 10, -50.0)     # in the skew: -$5
        self._fill(t + 650, "KXB-1", "bid", 10, -50.0)     # the other side: not counted
        self._fill(t + 3900, "KXA-1", "bid", 10, -50.0)    # after the block
        out = self._run()
        # block: $10 saved for 1.0/day x 1h = $0.04
        self.assertIn("| fills in the state | 1 (50 ct) | 1 (10 ct) |", out)
        self.assertIn("| their 4h mark-out | -$10.00 | -$5.00 |", out)
        self.assertIn("| reward given up | $0.04 | $0.04 |", out)
        self.assertIn("| net | $9.96 | $2.46 |", out)
        # the cap keeps half the block's side: half the saving, half the cost
        self.assertIn("about $4.98 net", out)
        self.assertIn("| event: KXA | $0.04 | $0.00 | 1 | -$10.00 |", out)
        self.assertIn("| Carbon Arc credit card | $900.00 | $750.00 | 120% |", out)
        self.assertIn("KXA $180.00 of $150.00 (120%)", out)

    def test_a_run_that_died_ends_its_states_ten_minutes_after_its_last_line(self):
        t = self.T0
        self.risk += [{"ev": "start", "ts": t},
                      {"ev": "state", "ts": t + 100, "ticker": "KXA-1", "side": "bid",
                       "state": "block", "prev": "ok", "by": "event", "event": "KXA",
                       "side_day": 86.4, "fit": 0.0}]
        spells = rep.risk_intervals(self.risk, t + 7200)
        self.assertEqual([(s["key"], s["state"], s["end"] - s["start"]) for s in spells],
                         [(("KXA-1", "bid"), "block", 600.0)])

    def test_no_lines_says_so(self):
        out = self._run()
        self.assertIn("No trips in the window", out)
        self.assertIn("No `risk_dry` lines in the window", out)


if __name__ == "__main__":
    unittest.main()
