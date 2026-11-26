"""Tests for send_cheap_report.py's fill cost (Jack 2026-11-10: "Yes, fix it" --
SKNA's 9,880 sub-penny fill at 0.4c read as $0.00 in the daily email).

Every path is a temp dir: the tests never touch run-logs/.
Run with IMM_STATUS_DIR pointed at a scratch dir, like the other IMM tests.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import incentive_mm as imm
import send_cheap_report as rep


class TestFillCost(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cheapreport_test_")
        self.patch = mock.patch.object(imm, "STATUS_DIR", self.dir)
        self.patch.start()
        ts = datetime(2026, 11, 10, 6, 17, tzinfo=timezone.utc).timestamp()
        rows = [
            # a sub-penny rung: the ledger's whole-cent price is 0, the trade's 0.4
            {"ts": ts, "ticker": "KXFRENCHPRES-27-SKNA", "side": "yes", "action": "buy",
             "count": 9880.0, "yes_price_cents": 0.4, "our_book_side": "bid",
             "our_price_cents": 0},
            # a whole-cent ask: the NO bought at 100 - the YES price
            {"ts": ts + 60, "ticker": "KXFRENCHPRES-27-GATT", "side": "no", "action": "buy",
             "count": 500.0, "yes_price_cents": 99.0, "our_book_side": "ask",
             "our_price_cents": 99},
            # an old row with only our price still counts
            {"ts": ts + 120, "ticker": "KXMOONMAN-31-R", "side": "yes", "action": "buy",
             "count": 100.0, "our_book_side": "bid", "our_price_cents": 2},
        ]
        with open(os.path.join(self.dir, "fills_2026-11-10.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_exact_price_first(self):
        lo = datetime(2026, 11, 10, 5, tzinfo=timezone.utc)
        hi = datetime(2026, 11, 11, 5, tzinfo=timezone.utc)
        out = rep.fills_in({"KXFRENCHPRES-27-SKNA", "KXFRENCHPRES-27-GATT", "KXMOONMAN-31-R"}, lo, hi)
        skna = out["KXFRENCHPRES-27-SKNA"][0]
        self.assertEqual(skna["side"], "yes")
        self.assertAlmostEqual(skna["px"], 0.4)
        self.assertAlmostEqual(skna["n"] * skna["px"] / 100.0, 39.52)
        gatt = out["KXFRENCHPRES-27-GATT"][0]
        self.assertEqual(gatt["side"], "no")
        self.assertAlmostEqual(gatt["px"], 1.0)
        self.assertAlmostEqual(out["KXMOONMAN-31-R"][0]["px"], 2.0)


class TestSideBook(unittest.TestCase):
    """Jack 2026-11-26: "yes fix it" -- XBER's 25,000 bought at 0.3c and sold
    back at 0.3c on 11/25 read "50,000 filled ($25,000.00); holds 50,000": the
    sell-back (an ask) was booked as a second purchase, of NO at 99.7c."""

    def test_a_round_trip_is_flat_at_no_cost(self):
        fl = [{"side": "yes", "px": 0.3, "n": 25000.0},     # the rung's bid
              {"side": "no", "px": 99.7, "n": 25000.0}]     # the at-cap unwind's ask at 0.3c
        sb = rep.side_book(fl, "yes", v_yes=0.0035)
        self.assertAlmostEqual(sb["bought_usd"], 75.0)
        self.assertAlmostEqual(sb["sold_usd"], 75.0)
        self.assertEqual((sb["bought_n"], sb["sold_n"], sb["n"]), (25000.0, 25000.0, 0.0))
        self.assertAlmostEqual(sb["cost"], 0.0)
        self.assertAlmostEqual(sb["worth"], 0.0)

    def test_a_partial_sell_back_keeps_the_rest_at_net_cost(self):
        fl = [{"side": "yes", "px": 1.0, "n": 10000.0}, {"side": "no", "px": 99.0, "n": 4000.0}]
        sb = rep.side_book(fl, "yes", v_yes=0.008)
        self.assertEqual((sb["held_side"], sb["n"]), ("yes", 6000.0))
        self.assertAlmostEqual(sb["cost"], 60.0)              # $100 bought - $40 sold back
        self.assertAlmostEqual(sb["worth"], 48.0)

    def test_a_no_side_rung_holds_no(self):
        fl = [{"side": "no", "px": 2.0, "n": 10000.0}]        # a NO bid at 2c (YES ask at 98c)
        sb = rep.side_book(fl, "no", v_yes=0.985)
        self.assertEqual((sb["held_side"], sb["n"]), ("no", 10000.0))
        self.assertAlmostEqual(sb["cost"], 200.0)
        self.assertAlmostEqual(sb["worth"], 150.0)

    def test_selling_more_than_was_bought_holds_the_other_contract(self):
        sb = rep.side_book([{"side": "no", "px": 98.0, "n": 1000.0}], "yes", v_yes=0.02)
        self.assertEqual((sb["held_side"], sb["n"]), ("no", 1000.0))
        self.assertAlmostEqual(sb["cost"], 980.0)             # the NO at 98c
        self.assertAlmostEqual(sb["worth"], 980.0)            # worth - cost = the P&L, 0 here

    def test_the_side_defaults_to_the_first_fill_and_worth_waits_for_a_price(self):
        sb = rep.side_book([{"side": "no", "px": 1.0, "n": 500.0}])
        self.assertEqual(sb["side"], "no")
        self.assertIsNone(sb["worth"])


if __name__ == "__main__":
    unittest.main()
