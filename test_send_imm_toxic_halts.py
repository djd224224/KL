#!/usr/bin/env python3
"""Unit tests for send_imm_toxic_halts.py -- the daily "what did the
toxic-flow halt stand down yesterday" email (Jack 2026-09-29).

Run: python -m unittest test_send_imm_toxic_halts

Worth testing: the ET-day window (a record at 23:30 ET belongs to that day
even though its UTC sink file is the next day's), the pick-offs attached to
each halt, the price-during-halt read off the marks sink, the forgone
reward, and that a day with nothing still produces an email.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_imm_toxic_halts as th                                   # noqa: E402
import incentive_mm as imm                                          # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_toxic_email_test_")
    imm.ALERT_RECIPIENTS = []


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def _write(name, rows):
    with open(os.path.join(imm.STATUS_DIR, name), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestToxicHaltsEmail(unittest.TestCase):
    def setUp(self):
        for f in os.listdir(imm.STATUS_DIR):
            os.remove(os.path.join(imm.STATUS_DIR, f))
        self.day = date(2026, 9, 28)                 # EDT: 04:00Z -> 04:00Z
        mk = "KXGAS-26SEP29-4.48"
        ev = "KXAAAGASD-26SEP29"
        a, b = f"{ev}-4.4750", f"{ev}-4.4800"
        t0 = _ts(2026, 9, 28, 18, 0)                 # 14:00 ET
        _write("toxic_halts_2026-09-28.jsonl", [
            # side halt on mk: two bid pick-offs, then the halt
            {"kind": "pickoff", "ts": t0, "ticker": mk, "event": "KXGAS-26SEP29",
             "side": "bid", "fill_px": 50.0, "fill_ts": t0 - 300, "mark": 44.0},
            {"kind": "pickoff", "ts": t0 + 600, "ticker": mk,
             "event": "KXGAS-26SEP29", "side": "bid", "fill_px": 48.0,
             "fill_ts": t0 + 300, "mark": 42.0},
            {"kind": "side_halt", "ts": t0 + 600, "until": t0 + 2400,
             "ticker": mk, "event": "KXGAS-26SEP29", "side": "bid",
             "pickoffs": 2, "window_secs": 86400, "est_per_day": 4.8},
            # an event halt on the AAA state daily
            {"kind": "pickoff", "ts": t0 + 1000, "ticker": a, "event": ev,
             "side": "bid", "fill_px": 67.0, "fill_ts": t0 + 700, "mark": 60.0},
            {"kind": "pickoff", "ts": t0 + 1200, "ticker": b, "event": ev,
             "side": "bid", "fill_px": 50.0, "fill_ts": t0 + 900, "mark": 44.0},
            {"kind": "event_halt", "ts": t0 + 1200, "until": t0 + 3000,
             "event": ev, "markets_picked": [a, b], "pickoffs": 2,
             "window_secs": 3600, "markets_halted": 3, "est_per_day": 24.0},
            # the day BEFORE (22:00 ET 9/27 = 02:00Z 9/28): not this report
            {"kind": "pickoff", "ts": _ts(2026, 9, 28, 2, 0), "ticker": "OLD-X",
             "event": "OLD", "side": "ask", "fill_px": 30.0, "fill_ts": 0,
             "mark": 40.0}])
        # 23:30 ET 9/28 = 03:30Z 9/29 -> the next UTC file, same ET day
        _write("toxic_halts_2026-09-29.jsonl", [
            {"kind": "pickoff", "ts": _ts(2026, 9, 29, 3, 30), "ticker": "LATE-A",
             "event": "LATE", "side": "ask", "fill_px": 20.0, "fill_ts": 0,
             "mark": 27.0}])
        # the side halt's market kept falling 42 -> 39 while halted
        _write("marks_2026-09-28.jsonl", [
            {"ts": datetime.fromtimestamp(t0 + 2460, timezone.utc).isoformat(),
             "ticker": mk, "mark_cents": 39.0}])
        self.mk, self.ev = mk, ev

    def test_report_covers_the_et_day_and_reads_each_halt(self):
        ctx = th.build_report(self.day)
        self.assertEqual(ctx["n_picks"], 5)          # not OLD-X; LATE-A counts
        self.assertEqual(len(ctx["side_rows"]), 1)
        s = ctx["side_rows"][0]
        self.assertEqual(s["picks"], [(50.0, 44.0), (48.0, 42.0)])
        self.assertEqual((s["m0"], s["m1"], s["move"]), (42.0, 39.0, 3.0))
        self.assertAlmostEqual(s["forgone"], 4.8 * 1800 / 86400)
        e = ctx["event_rows"][0]
        self.assertEqual(e["picked"], ["4.4750 (bid)", "4.4800 (bid)"])
        self.assertEqual(e["halted"], 3)
        self.assertAlmostEqual(e["forgone"], 24.0 * 1800 / 86400)
        self.assertAlmostEqual(ctx["forgone"], (4.8 + 24.0) * 1800 / 86400)
        text = th.render_text(ctx)
        self.assertIn("EVENT HALTS", text)
        self.assertIn("42->39 (kept moving against 3c)", text)
        self.assertIn("LATE-A", text)                # most picked-off table
        text.encode("ascii")                         # the task console is cp1252
        th.render_html(ctx)
        self.assertEqual(th.subject_for(ctx),
                         "IMM toxic halts Mon Sep 28: 1 event, 1 side (5 pick-offs)")

    def test_a_quiet_day_still_reports(self):
        ctx = th.build_report(date(2026, 9, 20))
        self.assertFalse(ctx["seen_file"])
        text = th.render_text(ctx)
        self.assertIn("Nothing was halted.", text)
        self.assertIn("No toxic_halts sink file", text)
        self.assertEqual(th.subject_for(ctx), "IMM toxic halts Sun Sep 20: none (0 pick-off(s))")

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(imm.Alerter, "send_message") as send:
            self.assertEqual(th.main(["--dry", "--day", "2026-09-28"]), 0)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
