"""Tests for imm_tape_ab_report.py (the tape trigger's live A/B score)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import imm_tape_ab_report as rep

T0 = 1793577600.0                 # 2026-11-02 00:00:00Z
E = "KXEVT-26NOV05"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def write_tape(d, trips):
    with open(os.path.join(d, "ws_tape_2026-11-02.jsonl"), "w") as f:
        for r in trips:
            f.write(json.dumps(r) + "\n")


def make_fixture(d):
    """One UTC day (11/02) of A/B logs in `d`: a paused window extended once,
    two control windows, fills, our order placements and the dashboard's
    marks. Returns the trip lines (rewrite them with write_tape)."""
    trips = [
        # a PAUSE window at +100 (to +160), extended at +130 to +190
        {"ev": "trip", "ts": T0 + 100.0, "mode": "on", "event": E, "ticker": E + "-S",
         "count": 120.0, "t1": T0 + 99.8, "hold_s": 60.0, "event_day": 86.4,
         "ab": "pause", "ab_frac": 0.5, "window_at": T0 + 100.0, "until": T0 + 160.0,
         "cross_tid": "P1", "ab_u": round(rep.draw_of(E, "P1"), 6)},
        {"ev": "trip", "ts": T0 + 130.0, "mode": "inside", "event": E, "ticker": E + "-B",
         "count": 100.0, "hold_s": 60.0, "event_day": 0.0, "ab": "pause",
         "ab_frac": 0.5, "window_at": T0 + 100.0, "until": T0 + 190.0,
         "cross_tid": "P9", "extend": 2},
        # two CONTROL windows; the first one's span is cut at the second
        {"ev": "trip", "ts": T0 + 1000.0, "mode": "dry", "event": E, "ticker": E + "-S",
         "count": 100.0, "t1": T0 + 999.9, "hold_s": 60.0, "event_day": 86.4,
         "ab": "control", "ab_frac": 0.5, "window_at": T0 + 1000.0,
         "until": T0 + 1060.0, "cross_tid": "P2", "ab_u": round(rep.draw_of(E, "P2"), 6)},
        {"ev": "trip", "ts": T0 + 1200.0, "mode": "dry", "event": E, "ticker": E + "-A",
         "count": 150.0, "t1": T0 + 1199.9, "hold_s": 60.0, "event_day": 86.4,
         "ab": "control", "ab_frac": 0.5, "window_at": T0 + 1200.0,
         "until": T0 + 1260.0, "cross_tid": "P3", "ab_u": round(rep.draw_of(E, "P3"), 6)},
        # a dry-mode trip line (no A/B): ignored
        {"ev": "trip", "ts": T0 + 5000.0, "mode": "dry", "event": E, "ticker": E + "-A",
         "count": 30.0, "hold_s": 600.0, "event_day": 86.4}]
    write_tape(d, trips)
    fills = [
        # after the pause's re-entry, inside its span: 5m -$0.20, 4h -$0.40
        {"ts": int(T0 + 400), "fill_id": "f1", "ticker": E + "-A", "count": 10.0,
         "is_taker": False},
        {"ts": int(T0 + 400), "fill_id": "f1", "ticker": E + "-A", "count": 10.0,
         "is_taker": False},                                      # logged twice
        # the first control window: -$1.00 / -$2.00
        {"ts": int(T0 + 1030), "fill_id": "f2", "ticker": E + "-B", "count": 20.0,
         "is_taker": False},
        # the second control window: +$0.10 / +$0.20
        {"ts": int(T0 + 1300), "fill_id": "f3", "ticker": E + "-A", "count": 10.0,
         "is_taker": False},
        # a taker fill of ours: never a maker's mark-out
        {"ts": int(T0 + 1031), "fill_id": "f4", "ticker": E + "-B", "count": 5.0,
         "is_taker": True}]
    with open(os.path.join(d, "fills_2026-11-02.jsonl"), "w") as f:
        for r in fills:
            f.write(json.dumps(r) + "\n")
    # our first order back in the event after the pause: +205 (dark 105 s)
    with open(os.path.join(d, "orders_2026-11-02.jsonl"), "w") as f:
        for ts, kind in ((T0 + 90.0, "place"), (T0 + 120.0, "cancel"),
                         (T0 + 205.0, "place"), (T0 + 230.0, "place")):
            f.write(json.dumps({"ts": _iso(ts), "kind": kind, "ticker": E + "-A"}) + "\n")
    D = {"fills": [{"ts": int(T0 + 400), "t": E + "-A", "n": 10.0, "m5c": -2.0, "m4c": -4.0},
                   {"ts": int(T0 + 1030), "t": E + "-B", "n": 20.0, "m5c": -5.0, "m4c": -10.0},
                   {"ts": int(T0 + 1300), "t": E + "-A", "n": 10.0, "m5c": 1.0, "m4c": 2.0}]}
    os.makedirs(os.path.join(d, "dashboard"))
    with open(os.path.join(d, "dashboard", "imm_dashboard.html"), "w") as f:
        f.write("<script>\nconst D = " + json.dumps(D) + ";\n</script>\n")
    return trips


class TestTapeABReport(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tape_ab_report_")
        self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(rep, "family", lambda s: "Fam " + s)
        p.start()
        self.addCleanup(p.stop)
        self.trips = make_fixture(self.d)

    def _run(self):
        with mock.patch("builtins.print"):
            rep.main(["--dir", self.d, "--since", "2026-11-02", "--until", "2026-11-02"])
        stamp = [n for n in os.listdir(self.d) if n.startswith("tape_ab_report_")
                 and n.endswith(".json")][0]
        with open(os.path.join(self.d, stamp), encoding="utf-8") as f:
            js = json.load(f)
        with open(os.path.join(self.d, stamp[:-5] + ".md"), encoding="utf-8") as f:
            return js, f.read()

    def test_windows_extend_and_a_span_stops_at_the_next_window(self):
        wins = rep.windows_of(self.trips)
        self.assertEqual([(w["arm"], w["at"] - T0, w["until"] - T0, w["trips"]) for w in wins],
                         [("pause", 100.0, 190.0, 2), ("control", 1000.0, 1060.0, 1),
                          ("control", 1200.0, 1260.0, 1)])
        self.assertEqual([w["span_end"] - T0 for w in wins], [610.0, 1200.0, 1680.0])

    def test_values_effect_and_the_running_verdict(self):
        js, md = self._run()
        # pause: fills -$0.20 / -$0.40, forgone $86.40/day x 105 s x 1.25 =
        # $0.13125; controls -$1.00 / -$2.00 and +$0.10 / +$0.20 (mean -$0.45 /
        # -$0.90): +$0.11875 / +$0.36875 a window, x 3 windows on 1 day
        self.assertAlmostEqual(js["effect_per_day"]["5m"], 0.35625, places=5)
        self.assertAlmostEqual(js["effect_per_day"]["4h"], 1.10625, places=5)
        self.assertAlmostEqual(js["shortfall_4h"], 0.36875, places=5)
        self.assertEqual((js["verdict"], js["days"], js["pause"], js["control"]),
                         ("RUNNING", 1, 1, 2))
        self.assertIn("**Verdict: RUNNING**", md)
        self.assertIn("Day 1 of 21", md)
        self.assertIn("| pause | 1 | 2 | 1 | 0 | $-0.331 | $-0.531 | 105 | $0.13 |", md)
        self.assertIn("| control | 2 | 2 | 2 | 0 | $-0.450 | $-0.900 | 0 | $0.00 |", md)
        self.assertIn("1 of 3 windows drew pause (expected 50%); 0 draw(s) that do "
                      "not recompute from the log.", md)
        # the by-day table: the day's pauses vs its controls, x1 paused / x3 all
        self.assertIn("| 2026-11-02 | 1 | 2 | $+0.37 | $+0.12 | $+1.11 | $+0.37 |", md)
        row, = js["daily"]
        self.assertAlmostEqual(row["forgone"], 0.13125, places=5)

    def test_daily_rows_and_the_pooled_running_total(self):
        def w(day, arm, v4, v5):
            return {"arm": arm, "mode": "on" if arm == "pause" else "dry",
                    "at": T0 + 86400 * day + 100, "forgone": 0.5 if arm == "pause" else 0.0,
                    "value": {"5m": v5, "4h": v4}}
        wins = [w(0, "pause", 2.0, 1.0), w(0, "control", 1.0, 1.0), w(0, "control", 0.0, 1.0),
                w(1, "pause", -1.0, 0.0), w(1, "control", 1.0, 0.0)]
        days = [rep._day(T0 + 86400 * k) for k in range(3)]
        r1, r2, r3 = rep.daily_of(wins, days)
        # day 1: the pause 2.0 vs controls 0.5 -> +1.50 (x1 paused), +4.50 (x3)
        self.assertEqual((r1["pause"], r1["control"]), (1, 2))
        self.assertAlmostEqual(r1["impact_4h"], 1.5)
        self.assertAlmostEqual(r1["all_on_4h"], 4.5)
        self.assertAlmostEqual(r1["so_far_4h"], 1.5)
        self.assertAlmostEqual(r1["impact_5m"], 0.0)
        # day 2: -1.0 vs 1.0 -> -2.00; pooled: pauses 0.5 vs controls 0.667, x2
        self.assertAlmostEqual(r2["impact_4h"], -2.0)
        self.assertAlmostEqual(r2["so_far_4h"], (0.5 - 2.0 / 3.0) * 2)
        # day 3: no windows: no day result, the pooled total carries on
        self.assertEqual((r3["pause"], r3["impact_4h"], r3["forgone"]), (0, None, 0))
        self.assertAlmostEqual(r3["so_far_4h"], r2["so_far_4h"])

    def test_a_draw_that_does_not_recompute_is_flagged(self):
        self.trips[2]["ab_u"] = 0.999
        write_tape(self.d, self.trips)
        _js, md = self._run()
        self.assertIn("1 draw(s) that do not recompute from the log", md)

    def test_a_pause_window_that_ran_dry_is_left_out(self):
        self.trips[0]["mode"] = "dry"              # the sweep breaker was off
        write_tape(self.d, self.trips)
        js, md = self._run()
        self.assertEqual((js["pause"], js["control"], js["effect_per_day"]), (0, 2, None))
        self.assertIn("1 pause window(s) ran dry", md)

    def test_verdict_rules(self):
        ci = {"5m": (-1.0, 3.0), "4h": (2.0, 9.0)}
        v = rep.verdict_of
        self.assertEqual(v({"5m": 1.0, "4h": 5.0}, ci, -151.0, 3), "STOP")     # any day
        self.assertEqual(v({"5m": 1.0, "4h": 5.0}, ci, 10.0, 20), "RUNNING")
        self.assertEqual(v({"5m": 1.0, "4h": 5.0}, ci, 10.0, 21), "GO")
        self.assertEqual(v({"5m": -0.1, "4h": 5.0}, ci, 10.0, 21), "INCONCLUSIVE")
        self.assertEqual(v({"5m": 1.0, "4h": 5.0}, {"5m": ci["5m"], "4h": (-1.0, 9.0)},
                           10.0, 21), "INCONCLUSIVE")
        self.assertEqual(v({"5m": 1.0, "4h": -0.5}, ci, -10.0, 21), "NO-GO")

    def test_no_windows_yet(self):
        write_tape(self.d, [self.trips[-1]])       # only a plain dry trip
        js, md = self._run()
        self.assertEqual(js["verdict"], "RUNNING")
        self.assertIn("No A/B windows yet", md)


if __name__ == "__main__":
    unittest.main()
