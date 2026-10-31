#!/usr/bin/env python3
"""Unit tests for send_comp_lead_monitor.py -- the daily complement-lead and
hedge email (Jack 2026-10-29: "I mean just a daily status email report on
this mechanism").

Run: python -m unittest test_send_comp_lead_monitor

Worth testing: leads read off the day log (moves, ends, restarts, the window
clip), lead fills matched to a lead's side and price, the hedge rows and the
open jobs, the verdict rules, and the orders resting ahead of leads off the
book_depth rows (Jack 2026-10-30: "Daily email report on leads").
"""

import gzip
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

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
        self.assertIn("net +$44 (rewards $0, 1 fill $0, hedges +$44) | OK", cm.subject_for(ctx))

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
        # a job in flight (a leg filled under an hour ago) risks nothing past its
        # fill: a note, not a WATCH (Jack 2026-10-31: "make it succinct and clearer")
        young = dict(stuck, at=_ts(11, 50), ticker="KXB-1")
        ctx = self._ctx(jobs=[young])
        self.assertEqual(ctx["level"], "OK")
        self.assertEqual(cm.notes(ctx), ["1 hedge still working (1 with a leg filled); the "
                                         "oldest KXB-1, 10 min"])
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
        self.assertIn("3 checked, 1 hedged (+$0.61), 1 unwound first, 1 left as inventory",
                      text)
        self.assertIn("  by kind: ladder_bid 1 of 2, me_ask 0 of 1", text)
        self.assertIn("hedged KXR-26NOV-4 30 at +2.0c a contract (ladder_bid: KXR-26NOV-5 29)",
                      text)
        self.assertIn("net +$1 (rewards $0, no fills, hedges +$1) | OK", cm.subject_for(ctx))

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
        self.assertIn("NET +$44.00\n", text)
        self.assertIn("+$0.00  1 fill on leads, valued at the latest marks\n", text)
        self.assertIn("+$44.00  hedges completed", text)
        self.assertIn("1 fill (200 contracts) +$0.00, 1 with no price yet", text)
        self.assertIn("no price  KXK-30-JDON sold 200 @ 60c at 03:00 ET, no price yet  "
                      "(complement: no hedge)", text)
        self.assertTrue(text.isascii())


def fair_lead(t, tk="KXRAINWKND-26OCT31-SGF", side="bid", px=54, best=48, fair=69, d=29):
    return (f"2026-10-{d} {t}Z [IMM] fair lead {tk} {side}: 200 @ {px}c, ahead of its "
            f"own {best}c; the weekend rain fair implies {fair}c\n")


class TestFairLeads(unittest.TestCase):
    """Weekend rain's sides lead off the model fair since 2026-10-29 (Jack:
    "since I have a fair we should be willing to come in above the wall with
    up to 200 contracts"): their own section, and a fill rides as inventory,
    so it never asks for a hedge job."""

    def test_the_email_names_the_families_led_now_or_on_the_day(self):
        # the OpenRouter weekly left the bot's list 2026-10-31 (Jack: "Pull
        # it back"); 10/31's email still names it, its leads ran that day
        with mock.patch.object(imm, "FAIR_LEAD_SERIES", ("KXOPENVREQ", "KXRAINWKND")):
            self.assertEqual(cm._fair_names({}), "weekend rain, Vercel")
            self.assertEqual(cm._fair_names({("KXTOKENUSE-26NOV02-T197", "bid"): {}}),
                             "weekend rain, Vercel, OpenRouter weekly")
        with mock.patch.object(imm, "FAIR_LEAD_SERIES", ()):
            self.assertEqual(cm._fair_names({}), "none")

    def test_fair_leads_parse_report_apart_and_never_watch(self):
        a, b = cm.day_window(date(2026, 10, 29))
        lines = [fair_lead("06:00:00"),
                 "2026-10-29 08:00:00Z [IMM] fair lead KXRAINWKND-26OCT31-SGF bid: off -- "
                 "the weekend rain fair no longer puts it ahead of its own, or an order "
                 "stepped ahead of it\n",
                 lead("09:00:00")]
        p = cm.parse_leads(lines, a, b)
        f = p["leads"][("KXRAINWKND-26OCT31-SGF", "bid")]
        self.assertEqual((f["kind"], f["px"], f["best"], f["implied"]), ("fair", 54, 48, 69))
        self.assertEqual(f["spans"], [[_ts(6), _ts(8)]])
        self.assertEqual(p["leads"][("KXK-30-JDON", "ask")]["kind"], "comp")
        fills = cm.lead_fills([{"ticker": "KXRAINWKND-26OCT31-SGF", "side": "yes",
                                "action": "buy", "count": 200.0, "is_taker": False,
                                "yes_price_cents": 54, "ts": _ts(7)}], p["leads"])
        self.assertEqual([x["kind"] for x in fills], ["fair"])
        h = cm.hedge_summary([], [], _ts(12))
        ctx = dict(day="2026-10-29", leads=p["leads"], groups=None, fills=fills,
                   hedges=h, est={})
        ctx["level"], ctx["reasons"] = cm.verdict(ctx)
        self.assertEqual(ctx["level"], "OK")                 # no hedge job is expected
        self.assertEqual(cm.subject_for(ctx), "IMM leads 10/29: net $0 (rewards $0, 1 fill $0) "
                                              "| OK")
        text = cm.render_text(ctx)
        self.assertIn("  complement  1 side on 1 market (from 05:00 ET): rewards +$0.00; no fills",
                      text)
        self.assertIn("  fair        1 side on 1 market (from 02:00 ET): rewards +$0.00; 1 fill "
                      "(200 contracts) +$0.00, 1 with no price yet", text)
        self.assertIn(f"FAIR LEADS BY EVENT (top 1 of 1 by reward, at least "
                      f"{imm.FAIR_LEAD_MARGIN_C}c inside the fair", text)
        self.assertIn("NEEDS A LOOK: nothing.", text)
        self.assertIn("KXRAINWKND-26OCT31-SGF bid: 200 @ 54c, own best 48c, fair 69c; "
                      "led 2.0h, ended", text)
        self.assertIn("no price  KXRAINWKND-26OCT31-SGF bought 200 @ 54c at 03:00 ET, no price "
                      "yet  (fair: rides to settlement)", text)
        self.assertTrue(text.isascii())


class TestMoney(unittest.TestCase):
    """The email leads with the money (Jack 2026-10-30: "clean up this email
    and make it clear the takeaway"): the reward each market accrued while it
    led, the lead fills marked to now, the locks, and their net."""

    def test_reward_while_leading_is_pro_rata_by_hour_and_counted_once(self):
        h0 = _ts(10)
        leads = {("T1", "bid"): {"spans": [[h0 + 1800, h0 + 5400]]},
                 ("T1", "ask"): {"spans": [[h0 + 3600, h0 + 5400]]},   # inside the bid's
                 ("T2", "ask"): {"spans": [[h0, h0 + 600]]}}
        hourly = {"T1": {int(h0 // 3600): 1.0, int(h0 // 3600) + 1: 2.0},
                  "T2": {int(h0 // 3600): 6.0}}
        r = cm.lead_rewards(leads, hourly)
        self.assertAlmostEqual(r["T1"], 0.5 * 1.0 + 0.5 * 2.0)   # half of each hour
        self.assertAlmostEqual(r["T2"], 1.0)                     # 10 of 60 minutes
        self.assertEqual(cm.lead_rewards(leads, {})["T1"], 0.0)

    def test_the_cache_marks_and_settlements_are_read(self):
        d = imm.STATUS_DIR
        a, b = cm.day_window(date(2026, 10, 29))
        hour = int(a // 3600) + 2
        with open(os.path.join(d, cm.HOURLY_CACHE_FILE), "w", encoding="utf-8") as f:
            json.dump({"cycle_log_2026-10-29.csv": {"sig": "x", "markets": {
                "T1": {str(hour): 0.75}}}}, f)
        hourly, covered = cm.load_hourly(a, b)
        self.assertEqual((hourly["T1"][hour], covered), (0.75, (hour + 1) * 3600.0))
        with open(os.path.join(d, "marks_2026-10-30.jsonl"), "w", encoding="utf-8") as f:
            for t, m in (("T1", 50.0), ("T2", 30.0), ("T1", 55.0)):
                f.write(json.dumps({"ticker": t, "mark_cents": m}) + "\n")
        with open(os.path.join(d, "settlements_2026-10-30.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ticker": "T2", "result": "yes", "ts": _ts(1, d=30)}) + "\n")
        marks = cm.load_marks(a, _ts(12, d=30))
        self.assertEqual((marks["T1"], marks["T2"]), (55.0, 100.0))   # latest; settled
        fills = cm.value_fills([dict(ticker="T1", side="bid", count=100.0, px=40.0),
                                dict(ticker="T2", side="ask", count=10.0, px=60.0),
                                dict(ticker="T9", side="bid", count=5.0, px=10.0)], marks)
        self.assertEqual([f["usd"] for f in fills], [15.0, -4.0, None])

    def test_the_takeaway_nets_reward_fills_and_locks_without_double_counting(self):
        leads = {("T1", "bid"): {"spans": [[_ts(6), _ts(9)]], "kind": "comp", "px": 40,
                                 "best": 35, "implied": 50, "who": "T2's book",
                                 "leading_at_end": False},
                 ("W1", "ask"): {"spans": [[_ts(6), _ts(7)]], "kind": "fair", "px": 62,
                                 "best": 59, "implied": 57, "leading_at_end": True}}
        fills = [dict(ticker="T1", side="bid", count=100.0, px=40.0, kind="comp", ts=_ts(7),
                      mark=45.0, usd=5.0),
                 dict(ticker="W1", side="ask", count=200.0, px=62.0, kind="fair", ts=_ts(8),
                      mark=42.0, usd=40.0)]
        h = cm.hedge_summary([{"kind": "open", "job": "j1"},
                              {"kind": "done", "job": "j1", "ticker": "T1", "side": "bid",
                               "edge_c": 3.0, "count": 100}], [], _ts(12))
        ctx = dict(day="2026-10-29", leads=leads, groups=None, fills=fills, hedges=h, est={},
                   rewards={"T1": 1.25, "W1": 0.25}, now="2026-10-30T12:00:00+00:00")
        ctx["level"], ctx["reasons"] = cm.verdict(ctx)
        T = cm.totals(ctx)
        # T1's fill is locked: its +3c a contract counts once, in the locks
        self.assertEqual((T["comp"]["fills_usd"], T["fair"]["fills_usd"], T["locks"]),
                         (0.0, 40.0, 3.0))
        self.assertAlmostEqual(T["net"], 1.25 + 0.25 + 40.0 + 3.0)
        # (Jack 2026-10-31: "make it succinct and clearer") the subject in whole
        # dollars, half up; the email opens on the same sum, line by line
        self.assertEqual(cm.subject_for(ctx), "IMM leads 10/29: net +$45 (rewards +$2, 2 fills "
                                              "+$40, hedges +$3) | OK")
        text = cm.render_text(ctx)
        self.assertIn("NET +$44.50\n"
                      "     +$1.50  rewards on the led markets during their lead hours", text)
        self.assertIn("    +$40.00  2 fills on leads, valued at the latest marks (a hedged one counts "
                      "in the hedges)\n"
                      "             biggest: W1 sold 200 @ 62c at 04:00 ET, now 42c: +$40.00\n"
                      "     +$3.00  hedges completed", text)
        self.assertIn("$  1.25  T1 bid 40c (complement, led 3.0h)", text)
        self.assertIn("    +$40.00  W1 sold 200 @ 62c at 04:00 ET, now 42c  (fair: rides to "
                      "settlement)\n"
                      "     +$5.00  T1 bought 100 @ 40c at 03:00 ET, now 45c  (complement: "
                      "hedged, counted in the hedges)", text)

    def test_a_stuck_lock_names_its_market_what_is_left_and_its_dollars(self):
        # 10/29's: sold 12 @ 53c on KXAPRPOTUS, 12 of one sibling sold @ 25c
        job = {"id": "KXA-1-ask-x", "ticker": "KXA-1", "side": "ask", "kind": "me_ask",
               "count": 12.0, "price": 53.0, "at": _ts(9), "until": _ts(9, 10), "best": 2.1,
               "waiting": True,
               "legs": {"KXA-2": {"filled": 12.0, "value": 300.0, "fee": 4.0},
                        "KXA-3": {"filled": 0.0, "value": 0.0, "fee": 0.0}}}
        ctx = self._ctx(job)
        # (Jack 2026-10-30: "Yes, size-aware") -- it can lose $2.68 at worst:
        # a WATCH, not a PROBLEM
        self.assertEqual(ctx["level"], "WATCH")
        self.assertIn("KXA-1: a hedge half done for 3h -- we sold 12 @ 53c and 12 of KXA-2 "
                      "filled, the other legs not; worst case -$2.68, best +$9.32; its legs' "
                      "books are off the feed; its watch ended", ctx["reasons"][0])
        self.assertTrue(cm.subject_for(ctx).endswith(
            "| WATCH: 1 hedge half done 3h (worst -$3)"))
        self.assertIn("(a half-done hedge is a PROBLEM when it can lose $10 or more as it "
                      "stands, a WATCH under that)", cm.render_text(ctx))
        # the same lock on 200 contracts can lose $44.66: a PROBLEM
        big = dict(job, count=200.0,
                   legs={"KXA-2": {"filled": 200.0, "value": 5000.0, "fee": 66.0}})
        ctx = self._ctx(big)
        self.assertEqual(ctx["level"], "PROBLEM")
        self.assertIn("worst case -$44.66, best +$155.34", ctx["reasons"][0])
        self.assertTrue(cm.subject_for(ctx).endswith(
            "| PROBLEM: 1 hedge half done 3h (worst -$45)"))
        # one it cannot price (its legs' value unknown) stays a PROBLEM
        ctx = self._ctx(dict(job, legs={"KXA-2": {"filled": 12.0}}))
        self.assertEqual(ctx["level"], "PROBLEM")
        self.assertIn("its dollars could not be priced", ctx["reasons"][0])
        self.assertTrue(cm.subject_for(ctx).endswith("| PROBLEM: 1 hedge half done 3h"))

    def test_a_half_done_lock_whose_markets_settled_is_final_not_a_watch(self):
        # Jack 2026-10-31 ("make it succinct and clearer"): 10/30's email called
        # 10/29's KXAPRPOTUS lock half done for 44h, worst -$2.68, though its
        # markets had settled on 10/30 at that worst case (38.4 YES, 38.7 NO)
        job = {"id": "KXA-1-ask-x", "ticker": "KXA-1", "side": "ask", "kind": "me_ask",
               "count": 12.0, "price": 53.0, "at": _ts(9), "until": _ts(9, 10), "lead": False,
               "legs": {"KXA-2": {"filled": 12.0, "value": 300.0, "fee": 4.0},
                        "KXA-3": {"filled": 0.0, "value": 0.0, "fee": 0.0}}}

        def report(results):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(imm, "STATUS_DIR", d):
                with open(os.path.join(d, imm.COMP_HEDGE_FILE), "w", encoding="utf-8") as f:
                    json.dump({"jobs": [job]}, f)
                with open(os.path.join(d, "settlements_2026-10-30.jsonl"), "w",
                          encoding="utf-8") as f:
                    for t, res in results.items():
                        f.write(json.dumps({"ticker": t, "result": res,
                                            "ts": "2026-10-30T16:12:02+00:00"}) + "\n")
                return cm.build_report(date(2026, 10, 30),
                                       now=datetime(2026, 10, 31, 11, 55, tzinfo=timezone.utc))
        ctx = report({"KXA-1": "yes", "KXA-2": "no"})
        self.assertEqual((ctx["level"], ctx["reasons"]), ("OK", []))
        self.assertEqual(cm.notes(ctx), ["KXA-1: its half-done hedge settled at -$2.68, its "
                                         "worst case"])
        self.assertTrue(cm.subject_for(ctx).endswith("| OK"))
        self.assertIn("NEEDS A LOOK: nothing.\n"
                      "  FYI: KXA-1: its half-done hedge settled at -$2.68, its worst case",
                      cm.render_text(ctx))
        # while one of its markets is open it is still half done: a WATCH
        ctx = report({"KXA-1": "yes"})
        self.assertEqual(ctx["level"], "WATCH")
        self.assertIn("KXA-1: a hedge half done for 51h", ctx["reasons"][0])

    def _ctx(self, job):
        h = cm.hedge_summary([], [job], _ts(12))
        ctx = dict(day="2026-10-29", leads={}, groups=None, fills=[], hedges=h, est={},
                   now=datetime.fromtimestamp(_ts(12), timezone.utc).isoformat())
        ctx["level"], ctx["reasons"] = cm.verdict(ctx)
        return ctx

    def test_each_lock_kind_is_priced_as_the_bot_floors_it(self):
        # (kind, price, count, legs) -> (worst, best) in dollars
        cases = [
            # sold 12 @ 53c, 12 of a sibling sold @ 25c: at most one wins
            (("me_ask", 53, 12, {"B": (12, 300, 4)}), (-2.68, 9.32)),
            # bought 100 @ 40c, 60 of the twin bought @ 55c
            (("pair_bid", 40, 100, {"B": (60, 3300, 5)}), (-13.05, 26.95)),
            # sold 100 @ 60c, 40 of an easier strike bought @ 30c
            (("ladder_ask", 60, 100, {"B": (40, 1200, 3)}), (-12.03, 87.97)),
            # bought 100 @ 40c, 70 of a harder strike sold @ 30c
            (("ladder_bid", 40, 100, {"B": (70, 2100, 3)}), (-19.03, 80.97)),
        ]
        for args, (worst, best) in cases:
            got = cm.lock_outcomes(*args)
            self.assertAlmostEqual(got[0], worst, places=6, msg=args[0])
            self.assertAlmostEqual(got[1], best, places=6, msg=args[0])
            # the worst case is the bot's own lock floor for the whole fill
            kind, px, q, legs = args
            self.assertAlmostEqual(imm.lock_plan(kind, px, q, {}, legs)[0] * q / 100.0, worst,
                                   places=6, msg=kind)
        self.assertIsNone(cm.lock_outcomes(None, 50, 10, {}))
        # once all of its markets settle, a lock lands on one of those cases
        for (kind, px, q, legs), results, want in (
                (("me_ask", 53, 12, {"B": (12, 300, 4)}), {"A": 100.0, "B": 0.0}, -2.68),
                (("me_ask", 53, 12, {"B": (12, 300, 4)}), {"A": 0.0, "B": 0.0}, 9.32),
                (("pair_bid", 40, 100, {"B": (60, 3300, 5)}), {"A": 0.0, "B": 100.0}, -13.05),
                (("ladder_ask", 60, 100, {"B": (40, 1200, 3)}), {"A": 100.0, "B": 100.0},
                 -12.03),
                (("ladder_bid", 40, 100, {"B": (70, 2100, 3)}), {"A": 0.0, "B": 0.0}, -19.03),
                (("ladder_bid", 40, 100, {"B": (70, 2100, 3)}), {"A": 100.0, "B": 0.0}, 80.97)):
            self.assertAlmostEqual(cm.settled_outcome(kind, "A", px, q, legs, results), want,
                                   places=6, msg=(kind, results))
        # ...and has no final dollars while one of them is open
        self.assertIsNone(cm.settled_outcome("me_ask", "A", 53, 12, {"B": (12, 300, 4)},
                                             {"A": 100.0}))

    def test_a_lock_that_gave_up_is_priced_from_its_rows(self):
        d = imm.STATUS_DIR
        job = f"KXA-1-ask-abcd1234-{int(_ts(9, d=18))}"
        with open(os.path.join(d, "comp_hedge_2026-10-18.jsonl"), "w", encoding="utf-8") as f:
            for r in ({"ts": "2026-10-18T09:00:00+00:00", "kind": "open", "job": job,
                       "ticker": "KXA-1", "side": "ask", "lock": "me_ask", "lead": False,
                       "count": 12.0, "px": 53.0, "legs": ["KXA-2", "KXA-3"]},
                      {"ts": "2026-10-18T09:30:00+00:00", "kind": "leg_fill", "job": job,
                       "ticker": "KXA-2", "count": 5.0, "px": 25.0, "order_id": "o1"},
                      {"ts": "2026-10-18T09:40:00+00:00", "kind": "leg_fill", "job": "other",
                       "ticker": "KXA-2", "count": 50.0, "px": 25.0, "order_id": "o9"},
                      {"ts": "2026-10-18T10:00:00+00:00", "kind": "leg_fill", "job": job,
                       "ticker": "KXA-2", "count": 7.0, "px": 25.0, "order_id": "o2"}):
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(d, "comp_hedge_2026-10-20.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-20T09:00:30+00:00", "kind": "expired",
                                "job": job, "ticker": "KXA-1", "lock": "me_ask",
                                "legs": {"KXA-2": 12.0}}) + "\n")
        # 12 sold @ 25c in two fills, each booked the bot's maker fee (2c + 3c)
        fee = (imm.taker_fee_cents(25, 5, imm.LOCK_MAKER_FEE_MULT)
               + imm.taker_fee_cents(25, 7, imm.LOCK_MAKER_FEE_MULT))
        worst, best = cm.gave_up_outcome({"ts": "2026-10-20T09:00:30+00:00", "job": job,
                                          "lock": "me_ask"})
        self.assertAlmostEqual(worst, (53 * 12 + 300 - fee - 1200) / 100.0)
        self.assertAlmostEqual(best, (53 * 12 + 300 - fee) / 100.0)
        self.assertIsNone(cm.gave_up_outcome({"ts": "2026-10-20T09:00:30+00:00", "job": "j"}))
        ctx = cm.build_report(date(2026, 10, 20),
                              now=datetime(2026, 10, 21, 11, 55, tzinfo=timezone.utc))
        self.assertEqual(ctx["level"], "WATCH")
        self.assertIn("KXA-1: its hedge gave up half done -- the filled legs now ride as "
                      f"inventory; worst case {cm.usd(worst)}, best {cm.usd(best)}",
                      ctx["reasons"][0])
        self.assertTrue(cm.subject_for(ctx).endswith(
            f"| WATCH: 1 hedge gave up half done (worst {cm.usd0(worst)})"))


WALL = [(15, 13238.0), (16, 21.0)]
T50 = "KXFOO-26NOV02-T50"


def book(ts, ours=None, ahead=(), source="managed", tk=T50, side="bid"):
    """One book_depth row as the bot writes it (compact JSON, dollars): the
    lead side holds the 13,238-lot wall, our rung `ours` (price, size) and
    the external orders `ahead` [(price, size)], in that side's own cents."""
    lv = dict(WALL)
    for p, q in ([ours] if ours else []) + list(ahead):
        lv[p] = lv.get(p, 0.0) + q
    levels = [[f"{p / 100:.4f}", f"{q:.2f}"] for p, q in sorted(lv.items())]
    own = [[float(ours[0]), float(ours[1])]] if ours else []
    return json.dumps({
        "ts": round(ts, 3), "cycle_ts": "", "ticker": tk, "source": source, "fast": False,
        "unit": "dollars", "yes": levels if side == "bid" else [],
        "no": levels if side == "ask" else [],
        "own_yes_cents": own if side == "bid" else [], "own_no_cents": own if side == "ask" else [],
        "own_src": "resting_read", "target": 1000.0, "discount": 0.5, "pool_per_day": 10.0,
        "run_id": "r", "config_hash": "c"}, separators=(",", ":"))


def write_book(d, name, rows, tail=b""):
    os.makedirs(os.path.join(d, "book_depth"), exist_ok=True)
    with open(os.path.join(d, "book_depth", name), "wb") as f:
        f.write(gzip.compress(("\n".join(rows) + "\n").encode()) + tail)


def lead50(t, px=26, d=29):
    return lead(t, tk=T50, side="bid", px=px, best=16, implied=70,
                who="its harder strikes' books", verb="imply", d=d)


class TestOrdersAhead(unittest.TestCase):
    """Jack 2026-10-30: "Daily email report on leads", after "Hold at one
    half": every spell an order rested ahead of a lead, off the bot's own
    book_depth rows, with what the lead kept of its side and what the bot did."""

    def _run(self, lines, files):
        a, b = cm.day_window(date(2026, 10, 29))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(imm, "STATUS_DIR", d):
            for name, rows, *tail in files:
                write_book(d, name, rows, *tail)
            leads = cm.parse_leads(lines, a, b)["leads"]
            return leads, cm.orders_ahead(leads, a, b)

    def test_spells_are_held_fought_chased_or_stood_down(self):
        lines = [lead50("05:00:00"),
                 lead50("05:01:40", px=32),          # the fight's note, in the row's second
                 "2026-10-29 05:04:00Z [IMM] comp lead KXFOO-26NOV02-T50 bid: off -- its "
                 "harder strikes' books no longer put it ahead of its own, or an order "
                 "stepped ahead of it\n"]
        rows = [book(_ts(5, 0, 20), (26, 200)),                       # alone: no spell
                book(_ts(5, 0, 40), (26, 200), [(27, 31)]),           # a 31-lot jumper
                book(_ts(5, 1, 0), (26, 200), [(27, 31)]),
                book(_ts(5, 1, 0), (40, 999), [(41, 5)], source="candidate"),   # not the loop's
                book(_ts(5, 1, 20), (26, 200)),                       # it left: HELD
                book(_ts(5, 1, 40), (26, 200), [(27, 300)]),          # 300 move the reference
                book(_ts(5, 2, 0), (32, 200), [(27, 300)]),           # FOUGHT +6
                book(_ts(5, 2, 20), (32, 200), [(33, 31)]),
                book(_ts(5, 2, 40), (34, 200)),                       # CHASED +2 (no note)
                book(_ts(5, 3, 20), (34, 160), [(35, 31)]),           # a cap trims the lead
                book(_ts(5, 3, 40), (34, 120), [(35, 31)]),           # ...any size at its price
                book(_ts(5, 4, 20), (34, 120), [(35, 31)]),           # off: stood down
                book(_ts(5, 4, 40), (34, 120), [(35, 31)], tk="KXOTHER-1")]
        leads, res = self._run(lines, [("book_depth_2026-10-29_r1.jsonl.gz", rows)])
        ev = sorted(res["events"], key=lambda e: e["ts"])
        self.assertEqual([(e["outcome"], e["top"], e["moved"]) for e in ev],
                         [("held", 26, 0), ("fought", 26, 6), ("fought", 32, 2),
                          ("gone", 34, None)])
        self.assertAlmostEqual(ev[0]["kept"], 200 / (231 + 21 / 2 ** 10 + 13238 / 2 ** 11), 4)
        self.assertEqual(ev[0]["ahead"], 31)
        self.assertLess(ev[1]["kept"], 0.5)
        self.assertGreater(ev[2]["kept"], 0.5)
        self.assertEqual(res["rows"], 11)                    # the loop's rows of T50 only
        S = cm.ahead_summary(ev, 0.5)
        self.assertEqual({k: len(v) for k, v in S.items()},
                         dict(held=1, fought=1, chased=1, under=0, down=0, gone=1))
        with mock.patch.object(imm, "COMP_LEAD_HOLD_SHARE", 0.95):     # the old bar
            self.assertEqual(cm.hold_bar(), 0.95)
        with mock.patch.object(imm, "COMP_LEAD_HOLD_SMALL", False):
            self.assertGreater(cm.hold_bar(), 1.0)

    def test_files_read_in_run_order_and_a_restart_does_not_end_a_spell(self):
        lines = [lead50("05:00:00"),
                 "2026-10-29 05:05:00Z === incentive_mm_v1.2 run=b mode=LIVE config=y ===\n",
                 lead50("05:06:00")]
        run_a = [book(_ts(5, 4, 0), (26, 200)), book(_ts(5, 4, 40), (26, 200), [(27, 31)])]
        part = [book(_ts(5, 4, 50), (26, 200), [(27, 31)])]
        run_b = [book(_ts(5, 5, 30), (26, 200), [(27, 31)]),     # handed over, not yet re-noted
                 book(_ts(5, 6, 20), (26, 200), [(27, 31)]),
                 book(_ts(5, 6, 40), (26, 200))]
        # "aaaa" (the later run) sorts first by name; a truncated tail member is
        # what a hard kill leaves
        _leads, res = self._run(lines, [
            ("book_depth_2026-10-29_bbbb.jsonl.gz", run_a, b"\x1f\x8b\x08\x00broken"),
            ("book_depth_2026-10-29_bbbb.p1.jsonl.gz", part),
            ("book_depth_2026-10-29_aaaa.jsonl.gz", run_b)])
        self.assertEqual([(e["outcome"], e["ts"], e["last"]) for e in res["events"]],
                         [("held", _ts(5, 4, 40), _ts(5, 6, 20))])
        self.assertEqual((res["files"], res["rows"]), (3, 6))

    def test_the_section_and_the_takeaway(self):
        lines = [lead50("05:00:00"), lead50("05:01:40", px=32)]
        rows = [book(_ts(5, 0, 40), (26, 200), [(27, 31)]),
                book(_ts(5, 1, 20), (26, 200)),
                book(_ts(5, 1, 40), (26, 200), [(27, 300)]),
                book(_ts(5, 2, 0), (32, 200), [(27, 300)]),
                book(_ts(5, 2, 20), (32, 200), [(33, 31)]),
                book(_ts(5, 2, 40), (34, 200))]
        leads, res = self._run(lines, [("book_depth_2026-10-29_r1.jsonl.gz", rows)])
        h = cm.hedge_summary([], [], _ts(12))
        ctx = dict(day="2026-10-29", leads=leads, groups=None, fills=[], hedges=h, est={},
                   ahead=res)
        ctx["level"], ctx["reasons"] = cm.verdict(ctx)
        text = cm.render_text(ctx)
        self.assertIn("ORDERS IN FRONT OF A LEAD: 3 times, on 1 lead side (one spell at one lead "
                      "price, however the orders in front came and went)\n"
                      "  rule: a lead stays put while it keeps 50% of its side, and moves up "
                      "toward the model below that", text)
        self.assertIn("  held 1: stayed put, kept a median 84% of its side (lowest 84%), a median "
                      "31 lots in front", text)
        self.assertIn("  fought 1: kept under 50%, moved up a median 6c toward the model (6c in "
                      "all; a median 300 lots in front)", text)
        self.assertIn("  chased 1: moved up though it kept 50% or more, a median 2c toward the "
                      "model (2c in all; a median 31 lots in front); all of them before the rule "
                      "went live", text)
        self.assertIn(f"    {T50} bid (complement): 3 -- held 1 (84%-84%), fought 1, chased 1",
                      text)
        self.assertTrue(text.isascii())
        # since the half-side hold, a chase is not expected: the line says none
        ctx["ahead"] = dict(res, events=[e for e in res["events"] if e["moved"] != 2])
        self.assertIn("  chased 0: none moved up while it kept 50% of its side",
                      cm.render_text(ctx))
        # a scan that failed says so; a report built without one has no section
        ctx["ahead"] = {"events": [], "files": 0, "rows": 0, "error": "OSError('x')"}
        self.assertIn("  not read: OSError('x')", cm.render_text(ctx))
        del ctx["ahead"]
        self.assertNotIn("ORDERS IN FRONT", cm.render_text(ctx))


if __name__ == "__main__":
    unittest.main()
