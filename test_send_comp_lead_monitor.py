#!/usr/bin/env python3
"""Unit tests for send_comp_lead_monitor.py -- the daily complement-lead and
hedge email (Jack 2026-10-29: "I mean just a daily status email report on
this mechanism").

Run: python -m unittest test_send_comp_lead_monitor

Worth testing: leads read off the day log (moves, ends, restarts, the window
clip), lead fills matched to a lead's side and price, the hedge rows and the
open jobs, and the verdict rules.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_comp_lead_monitor as cm                                  # noqa: E402
import incentive_mm as imm                                           # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_comp_lead_monitor_test_")
    imm.ALERT_RECIPIENTS = []


def _ts(h, mi=0, s=0, d=29):
    return datetime(2026, 10, d, h, mi, s, tzinfo=timezone.utc).timestamp()


def lead(t, tk="KXK-30-JDON", side="ask", px=60, best=71, implied=38,
         who="its 11 siblings' books", verb="imply", d=29):
    return (f"2026-10-{d} {t}Z [IMM] comp lead {tk} {side}: 200 @ {px}c, ahead of its "
            f"own {best}c; {who} {verb} {implied}c\n")


def off(t, tk="KXK-30-JDON", side="ask", d=29):
    return (f"2026-10-{d} {t}Z [IMM] comp lead {tk} {side}: off -- its 11 siblings' books "
            f"no longer put it ahead of its own, or an order stepped ahead of it\n")


class TestLeads(unittest.TestCase):

    def test_moves_ends_restarts_and_the_window(self):
        a, b = cm.day_window(date(2026, 10, 29))          # 04:00Z 10/29 - 04:00Z 10/30
        lines = [
            lead("02:00:00"),                                # before the window: clipped
            lead("06:00:00", px=57),                         # a move: same lead, new price
            off("08:00:00"),
            lead("09:00:00", tk="KXW-26-D", side="bid", px=26, best=16, implied=70,
                 who="KXW-26-R's book", verb="implies"),
            "2026-10-29 10:00:00Z === incentive_mm_v1.2 run=b mode=LIVE config=y ===\n",
            lead("10:01:00", tk="KXW-26-D", side="bid", px=21, best=15, implied=70,
                 who="KXW-26-R's book", verb="implies"),
            "2026-10-29 10:02:00Z [IMM] comp lead: 125 mutually exclusive event(s) "
            "grouped, 78 of them two-market (125 event read(s) this refresh)\n",
            lead("05:00:00", d=30, tk="KXLATE-1"),           # after the window
            # a fought lead held at the cap (Jack 2026-10-29 "2.")
            "2026-10-29 11:00:00Z [IMM] comp lead KXK-30-KMIN ask: 200 @ 43c, held at the "
            "cap, behind its own 41c; its 11 siblings' books imply 38c\n",
        ]
        p = cm.parse_leads(lines, a, b)
        self.assertEqual(p["groups"], (125, 78))
        k = p["leads"][("KXK-30-JDON", "ask")]
        self.assertEqual(k["spans"], [[a, _ts(8)]])          # 04:00Z - 08:00Z
        self.assertEqual(k["px"], 57)
        self.assertFalse(k["leading_at_end"])
        d = p["leads"][("KXW-26-D", "bid")]
        self.assertEqual(d["spans"], [[_ts(9), _ts(10)], [_ts(10, 1), b]])
        self.assertEqual((d["px"], d["best"], d["implied"]), (21, 15, 70))
        self.assertEqual(d["pxs"], [26, 21])                 # the prices it moved through
        self.assertEqual(k["pxs"], [57])                     # the 02:00Z one is before the day
        self.assertTrue(d["leading_at_end"])
        self.assertNotIn(("KXLATE-1", "ask"), p["leads"])
        self.assertEqual(p["leads"][("KXK-30-KMIN", "ask")]["px"], 43)

    def test_lead_fills_need_the_side_the_price_and_a_maker(self):
        leads = {("KXK-30-JDON", "ask"): {"spans": [[_ts(5), _ts(9)]], "px": 60}}
        base = {"ticker": "KXK-30-JDON", "side": "no", "action": "buy", "count": 200.0,
                "is_taker": False, "yes_price_cents": 60, "ts": _ts(6)}
        self.assertEqual(len(cm.lead_fills([base], leads)), 1)
        for bad in (dict(base, is_taker=True), dict(base, yes_price_cents=61),
                    dict(base, action="sell"), dict(base, ts=_ts(12))):
            self.assertEqual(cm.lead_fills([bad], leads), [], bad)


class TestHedgesAndVerdict(unittest.TestCase):

    def _ctx(self, rows=(), jobs=(), fills=()):
        h = cm.hedge_summary(list(rows), list(jobs), _ts(12))
        ctx = dict(day="2026-10-29", leads={}, groups=None, fills=list(fills),
                   hedges=h, est={})
        ctx["level"], ctx["reasons"] = cm.verdict(ctx)
        return ctx

    def test_a_locked_set_is_ok(self):
        ctx = self._ctx(rows=[{"kind": "open", "job": "j1"},
                              {"kind": "send", "job": "j1", "ticker": "B"},
                              {"kind": "done", "job": "j1", "edge_c": 22.0, "count": 200}],
                        fills=[{"ticker": "A"}])
        self.assertEqual(ctx["level"], "OK")
        self.assertAlmostEqual(ctx["hedges"]["locked"], 44.0)
        self.assertIn("1 hedged (+$44.00 locked) | OK", cm.subject_for(ctx))

    def test_problems_and_watches(self):
        self.assertEqual(self._ctx(rows=[{"kind": "expired", "job": "j"}])["level"],
                         "PROBLEM")
        self.assertEqual(self._ctx(rows=[{"kind": "send", "error": "400"}])["level"],
                         "PROBLEM")
        # a post-only leg refused as a cross is priced again: not a problem
        self.assertEqual(self._ctx(rows=[{"kind": "send", "error": "400",
                                          "error_body": "post only cross"}])["level"], "OK")
        stuck = {"id": "j", "count": 200, "at": _ts(10), "legs": {"B": {"filled": 50}}}
        self.assertEqual(self._ctx(jobs=[stuck])["level"], "PROBLEM")
        young = dict(stuck, at=_ts(11, 50))
        self.assertEqual(self._ctx(jobs=[young])["level"], "WATCH")
        self.assertEqual(self._ctx(fills=[{"ticker": "A"}])["level"], "WATCH")  # no job
        # a lead fill that never locked is a PROBLEM; another fill that did not
        # lock just rides on, and one still in its watch is not reported
        self.assertEqual(self._ctx(rows=[{"kind": "nolock", "job": "j", "lead": True}])["level"],
                         "PROBLEM")
        self.assertEqual(self._ctx(rows=[{"kind": "nolock", "job": "j", "lead": False}])["level"],
                         "OK")
        watching = {"id": "j", "count": 20, "at": _ts(11, 58), "lead": False, "legs": {}}
        self.assertEqual(self._ctx(jobs=[watching])["level"], "OK")

    def test_the_other_fills_locks(self):
        rows = [{"kind": "open", "job": "a", "lock": "me_ask", "lead": False},
                {"kind": "open", "job": "b", "lock": "ladder_bid", "lead": False},
                {"kind": "open", "job": "c", "lock": "ladder_bid", "lead": False},
                {"kind": "done", "job": "b", "ticker": "KXR-26NOV-4", "lock": "ladder_bid",
                 "lead": False, "edge_c": 2.03, "count": 30, "legs": {"KXR-26NOV-5": 29}},
                {"kind": "nolock", "job": "a", "lead": False},
                {"kind": "unwound", "job": "c"}]
        ctx = self._ctx(rows=rows)
        lk = ctx["hedges"]["locks"]
        self.assertEqual((len(lk["opened"]), len(lk["done"]), len(lk["none"]),
                          len(lk["unwound"])), (3, 1, 1, 1))
        self.assertAlmostEqual(lk["locked"], 0.609)
        self.assertEqual(ctx["level"], "OK")
        text = cm.render_text(dict(ctx, est={}))
        self.assertIn("3 watched, 1 locked (+$0.61), 1 unwound first, 1 rode on as inventory",
                      text)
        self.assertIn("ladder_bid: 1 of 2 locked", text)
        self.assertIn("locked KXR-26NOV-4 30 at +2.0c a contract (ladder_bid: KXR-26NOV-5 29)",
                      text)
        self.assertIn("1 other fill locked (+$0.61)", cm.subject_for(ctx))

    def test_the_report_renders_from_files(self):
        day = date(2026, 10, 29)
        a, _b = cm.day_window(day)
        with open(os.path.join(imm.STATUS_DIR, "incentive-mm-2026-10-29.log"), "w",
                  encoding="utf-8") as f:
            f.write(lead("06:00:00"))
        with open(os.path.join(imm.STATUS_DIR, "fills_2026-10-29.jsonl"), "w") as f:
            f.write(json.dumps({"ticker": "KXK-30-JDON", "side": "no", "action": "buy",
                                "count": 200.0, "is_taker": False,
                                "yes_price_cents": 60, "ts": _ts(7)}) + "\n")
        with open(os.path.join(imm.STATUS_DIR, "comp_hedge_2026-10-29.jsonl"), "w") as f:
            for r in ({"kind": "open", "job": "j1"},
                      {"kind": "done", "job": "j1", "edge_c": 22.0, "count": 200}):
                f.write(json.dumps(dict(r, ts="2026-10-29T07:01:00+00:00")) + "\n")
        ctx = cm.build_report(day, now=datetime(2026, 10, 30, 11, 55, tzinfo=timezone.utc))
        text = cm.render_text(ctx)
        self.assertEqual(ctx["level"], "OK")
        self.assertIn("KXK-30-JDON ask: 200 @ 60c, own best 71c, siblings imply 38c", text)
        self.assertIn("LEAD FILLS: 1", text)
        self.assertIn("1 locked (+$44.00)", text)
        self.assertTrue(text.isascii())


if __name__ == "__main__":
    unittest.main()
