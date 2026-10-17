"""Tests for imm_fast_cancel_report.py (the faster fast cancel's 3-day report,
Jack 2026-10-17: "Yes, and email report in 3 days"). Synthetic logs in a temp
dir; nothing under run-logs is read."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import imm_fast_cancel_report as rep


class TestFastCancelReport(unittest.TestCase):
    T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, "dashboard"))
        self.stale, self.fills, self.dfills = [], [], []

    def _seen(self, oid, ts, arm, acts=True, gap=14.0, t="KXA-1"):
        self.stale.append({"ev": "seen", "ts": ts, "order_id": oid, "ticker": t,
                           "side": "bid", "px": 49.0, "gap_c": gap, "acts": acts,
                           "arm": arm})

    def _fill(self, oid, ts, n, m4c, m5c=0.0, t="KXA-1"):
        self.fills.append({"ts": ts, "order_id": oid, "ticker": t, "count": n,
                           "is_taker": False})
        self.dfills.append({"ts": ts, "t": t, "n": n, "m4c": m4c, "m5c": m5c})

    def _write(self):
        day = datetime.fromtimestamp(self.T0, timezone.utc).date().isoformat()
        with open(os.path.join(self.d, f"ws_stale_{day}.jsonl"), "w") as f:
            for r in self.stale:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.d, f"fills_{day}.jsonl"), "w") as f:
            for r in self.fills:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.d, "dashboard", "imm_dashboard.html"), "w") as f:
            f.write("<script>\nconst D = " + json.dumps({"fills": self.dfills}) + ";\n</script>\n")
        with open(os.path.join(self.d, "reward_est_hourly_cache.json"), "w") as f:
            json.dump({"x": {"markets": {"KXA-1": {str(int(self.T0 // 3600)): 36.0}}}}, f)

    def _run(self):
        self._write()
        path_holder = []
        orig = rep.finish

        def capture(d, lines):
            path_holder.append("\n".join(lines))
            return 0
        rep.finish = capture
        try:
            rep.main(["--dir", self.d, "--since", "2026-10-18T11:00:00Z",
                      "--until", "2026-10-18T14:00:00Z"])
        finally:
            rep.finish = orig
        return path_holder[0]

    def test_the_arms_are_scored_per_episode(self):
        t = self.T0
        # fast arm: 2 episodes, both cancelled at 0.3s, no fills
        for i in range(2):
            self._seen(f"f{i}", t + i * 100, "fast")
            self.stale.append({"ev": "cancel", "by": "fast", "ts": t + i * 100 + 0.3,
                               "order_id": f"f{i}", "arm": "fast", "seen_s": 0.3})
        # control: 2 episodes; one hit at 0.6s for -$5 (100 ct at -5c), then cancelled
        self._seen("c0", t + 300, "control")
        self._fill("c0", t + 300.6, 100, -5.0)
        self.stale.append({"ev": "cancel", "by": "fast", "ts": t + 301.1,
                           "order_id": "c0", "arm": "control", "seen_s": 1.1})
        self._seen("c1", t + 400, "control")
        self.stale.append({"ev": "unseen", "ts": t + 400.5, "order_id": "c1",
                           "why": "clear", "arm": "control", "secs": 0.5})
        out = self._run()
        self.assertIn("| episodes | 2 ", out)
        self.assertIn("| fast cancels | 2 | 1 |", out)
        self.assertIn("| median seconds from first sight to cancel | 0.30 | 1.10 |", out)
        self.assertIn("| their 4-hour mark-out, $ per 1,000 episodes | +0.0 | -2500.0 |", out)
        # the control lost $2.50 an episode; applied to 2 fast episodes = +$5 avoided
        self.assertIn("fills avoided +5.00 at 4 hours", out)
        self.assertIn("WATCH (too few episodes to call)", out)

    def test_the_small_gaps_are_sized_not_scored_as_arms(self):
        t = self.T0
        self._seen("s0", t, "fast", acts=False, gap=4.0)
        self._fill("s0", t + 0.8, 50, -10.0)               # 0.25-1.25s: reachable
        self._fill("s0", t + 3.0, 10, -10.0)               # later
        self.stale.append({"ev": "flag", "ts": t + 1.0, "order_id": "s0"})
        self.stale.append({"ev": "clear", "ts": t + 5.0, "order_id": "s0"})
        self._seen("s1", t + 50, "fast", acts=False, gap=1.0)
        self.stale.append({"ev": "unseen", "ts": t + 50.4, "order_id": "s1",
                           "why": "clear", "arm": "fast", "secs": 0.4})
        out = self._run()
        self.assertIn("| 3-5c | 8 | 0% | 1 | -5 | 1 | -1 |", out)
        self.assertIn("| 1-2c | 8 | 100% |", out)
        self.assertIn("| episodes | 0 (0/day) | 0 (0/day) |", out)

    def test_no_first_sightings_says_so(self):
        out = self._run()
        self.assertIn("No `seen` lines in the window", out)


if __name__ == "__main__":
    unittest.main()
