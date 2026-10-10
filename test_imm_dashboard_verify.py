"""Tests for imm_dashboard_verify.py: which fills it counts as the bot's."""
import calendar
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import imm_dashboard as dash
import imm_dashboard_verify as verify


class TestBotOrderIds(unittest.TestCase):
    """2026-10-10: imm_state.json's our_order_ids keeps only the last few
    hours (ORDER_ID_KEEP_HOURS) instead of 7 days, and the 7d window read the
    orders log for its edge days only -- its middle days' fills were the
    bot's through the state alone."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_verify_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, dash, "STATUS_DIR", dash.STATUS_DIR)
        dash.STATUS_DIR = self.dir
        verify._PLACE_IDS.clear()
        self.addCleanup(verify._PLACE_IDS.clear)

    def _orders(self, day, *rows):
        with open(os.path.join(self.dir, f"orders_{day}.jsonl"), "w",
                  encoding="utf-8") as f:
            for kind, oid in rows:
                f.write(json.dumps({"kind": kind, "order_id": oid}) + "\n")

    def test_every_day_of_a_7d_window_the_rain_ledger_and_the_state(self):
        e1 = float(calendar.timegm((2026, 10, 10, 12, 0, 0)))
        e0 = e1 - 7 * 86400                       # 10/03 12:00Z
        for d in range(1, 11):                    # 10/01 (e0 - 2d) .. 10/10
            self._orders(f"2026-10-{d:02d}", ("place", f"p{d}"), ("amend", f"a{d}"))
        self._orders("2026-09-30", ("place", "too-early"))
        self._orders("2026-10-11", ("place", "too-late"))
        with open(os.path.join(self.dir, "rain_directional_ledger.csv"), "w",
                  encoding="utf-8") as f:
            f.write("ts,ticker,take_side,contracts,price_cents,fair_cents,ext_bid,"
                    "ext_ask,edge_cents,order_id\n"
                    "2026-10-05T16:51:52Z,KXRAIN-26OCT06-NOLA,yes,3,9,20,5,9,11,rain-1\n")
        with open(os.path.join(self.dir, "imm_state.json"), "w", encoding="utf-8") as f:
            json.dump({"our_order_ids": {"state-1": e1 - 600}}, f)
        self.assertEqual(verify.bot_order_ids(e0, e1),
                         {f"p{d}" for d in range(1, 11)} | {"rain-1", "state-1"})

    def test_missing_files_are_skipped(self):
        e1 = float(calendar.timegm((2026, 10, 10, 12, 0, 0)))
        self._orders("2026-10-10", ("place", "only"))
        self.assertEqual(verify.bot_order_ids(e1 - 86400, e1), {"only"})

    def test_a_day_file_is_read_once_until_it_changes(self):
        e1 = float(calendar.timegm((2026, 10, 10, 12, 0, 0)))
        self._orders("2026-10-10", ("place", "x"))
        real_open, opened = open, []

        def counting_open(path, *a, **kw):
            opened.append(os.path.basename(str(path)))
            return real_open(path, *a, **kw)
        with mock.patch("builtins.open", counting_open):
            verify.bot_order_ids(e1 - 3600, e1)
            verify.bot_order_ids(e1 - 7200, e1)
        self.assertEqual(opened.count("orders_2026-10-10.jsonl"), 1)
        self._orders("2026-10-10", ("place", "x"), ("place", "y"))
        self.assertIn("y", verify.bot_order_ids(e1 - 3600, e1))


if __name__ == "__main__":
    unittest.main()
