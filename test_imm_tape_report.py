"""Tests for imm_tape_report.py (the tape trigger's dry-run score)."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import imm_tape_report as rep

T0 = 1793577600.0                 # 2026-11-02 00:00:00Z
E = "KXEVT-26NOV05"


class TestTapeReport(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tape_report_")
        self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(rep, "family", lambda s: "Fam " + s)
        p.start()
        self.addCleanup(p.stop)
        trip = {"ev": "trip", "ts": T0 + 10.0, "mode": "dry", "event": E,
                "ticker": E + "-S", "hits": "bid", "count": 30.0, "levels": 2,
                "lag_s": 0.3, "hold_s": 600.0, "event_day": 86.4}
        held = [  # one 5s after the trip, one in the same second (the sweep itself)
            {"ev": "held_fill", "mode": "dry", "scope": "event", "ticker": E + "-A",
             "count": 20.0, "fill_ts": T0 + 15.0, "trip_ts": T0 + 10.0,
             "since_trip_s": 5.0, "trip_event": E},
            {"ev": "held_fill", "mode": "dry", "scope": "event", "ticker": E + "-A",
             "count": 10.0, "fill_ts": T0 + 10.2, "trip_ts": T0 + 10.0,
             "since_trip_s": 0.2, "trip_event": E}]
        burst = [{"ev": "burst", "ts": T0 + 14.0, "ticker": E + "-S", "event": E,
                  "count": 30.0, "levels": 2, "t0": T0 + 9.7, "t1": T0 + 10.0},
                 {"ev": "burst", "ts": T0 + 30.0, "ticker": E + "-B", "event": E,
                  "count": 12.0, "levels": 1, "t0": T0 + 20.0, "t1": T0 + 20.0}]
        with open(os.path.join(self.d, "ws_tape_2026-11-02.jsonl"), "w") as f:
            for r in [trip] + held + burst:
                f.write(json.dumps(r) + "\n")
        fills = [{"ts": int(T0 + 15), "ticker": E + "-A", "count": 20.0,
                  "is_taker": False, "order_id": "o1"},
                 {"ts": int(T0 + 10), "ticker": E + "-A", "count": 10.0,
                  "is_taker": False, "order_id": "o2"}]
        with open(os.path.join(self.d, "fills_2026-11-02.jsonl"), "w") as f:
            for r in fills:
                f.write(json.dumps(r) + "\n")
        # mark-outs: the 20-lot lost 5c/ct at 5 min and 10c/ct at 4 h
        D = {"fills": [{"ts": int(T0 + 15), "t": E + "-A", "n": 20.0,
                        "m5c": -5.0, "m4c": -10.0},
                       {"ts": int(T0 + 10), "t": E + "-A", "n": 10.0,
                        "m5c": -50.0, "m4c": -50.0}]}
        os.makedirs(os.path.join(self.d, "dashboard"))
        with open(os.path.join(self.d, "dashboard", "imm_dashboard.html"), "w") as f:
            f.write("<script>\nconst D = " + json.dumps(D) + ";\n</script>\n")
        hour = int((T0 + 10) // 3600)
        with open(os.path.join(self.d, "reward_est_hourly_cache.json"), "w") as f:
            json.dump({"2026-11-02": {"markets": {E + "-A": {str(hour): 3.6},
                                                  E + "-B": {str(hour): 0.0}}}}, f)

    def _report(self):
        with mock.patch("builtins.print"):
            rep.main(["--dir", self.d, "--since", "2026-11-02", "--until", "2026-11-02"])
        name = [n for n in os.listdir(self.d) if n.startswith("tape_report_")][0]
        with open(os.path.join(self.d, name), encoding="utf-8") as f:
            return f.read()

    def test_as_run_nets_the_held_fills_against_the_hold(self):
        md = self._report()
        # avoided: the 20-lot only ($1.00 at 5m, $2.00 at 4h); the same-second
        # 10-lot is the sweep itself. Forgone: $86.40/day x 630s = $0.63.
        self.assertIn("**As run:** 1 trips it would have acted on, 1 of our fills", md)
        self.assertIn("Avoided $+1.00 at 5 min / $+2.00 at 4 h, against $0.63", md)
        self.assertIn("net **$+0.37 / $+1.37**", md)
        self.assertIn("(the same sweep, not counted): 1.", md)
        self.assertIn("| Fam KXEVT | 1 | 1 | $+1.00 | $+2.00 | $0.63 | $+1.37 |", md)

    def test_rescored_lines_use_the_burst_lines(self):
        md = self._report()
        # the 30-lot over 2 prices trips "as run": its window from t1 holds the
        # 20-lot (5 s after t1); the hour's reward $3.60 x 630 s = $0.63
        self.assertIn("| as run: 25+ over 2+ prices, or 100+ | 1 | 1 | $+1.00 | $+2.00 "
                      "| $0.63 | $+1.37 |", md)
        # 25+ contracts: the 12-lot never trips, so the same one trip
        self.assertIn("| 25+ contracts | 1 | 1 |", md)
        # 50+ contracts: nothing trips
        self.assertIn("| 50+ contracts | 0 | 0 | $+0.00 | $+0.00 | $0.00 | $+0.00 |", md)


if __name__ == "__main__":
    unittest.main()
