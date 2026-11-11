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


if __name__ == "__main__":
    unittest.main()
