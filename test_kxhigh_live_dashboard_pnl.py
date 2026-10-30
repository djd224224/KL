"""The KXHIGH live dashboard's History P&L (analysis/kxhigh/python/live_dashboard.py):
Kalshi's settlement records before the IMM shared the account, this bot's own
fills from then on, kept in GCS once computed (Jack 2026-10-29: "PnL history in
this is wrong")."""
import datetime as dt
import os
import re
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "analysis", "kxhigh", "python"))
import live_dashboard as ld  # noqa: E402


def frame(rows, window_from=None):
    out = []
    for d, n, w, cost, pnl in rows:
        r = {"event_date": dt.date.fromisoformat(d), "n_markets": n, "n_wins": w,
             "total_cost": cost, "pnl": pnl}
        if window_from:
            r["window_from"] = dt.date.fromisoformat(window_from)
        out.append(r)
    return pd.DataFrame(out)


class DailyPnlTests(unittest.TestCase):
    def test_settlements_before_the_imm_and_bot_fills_after(self):
        settled = frame([("2026-10-22", 10, 6, 328.0, 63.0),
                         ("2026-10-27", 30, 15, 4339.98, -1326.52)])   # the account, IMM in
        fills = frame([("2026-10-27", 6, 3, 170.96, 20.04),
                       ("2026-10-24", 6, 2, 117.98, -10.98)], window_from="2026-08-24")
        df, keep = ld.merge_daily_pnl(settled, fills, {})
        self.assertEqual(list(df.columns), ["event_date", "n_markets", "n_wins", "total_cost", "pnl"])
        self.assertEqual(list(df["event_date"]), ["2026-10-27", "2026-10-24", "2026-10-22"])
        self.assertEqual(df.iloc[0]["pnl"], 20.04)        # this bot's, not the account's
        self.assertEqual(df.iloc[2]["pnl"], 63.0)
        self.assertEqual(set(keep), {"2026-10-24", "2026-10-27"})
        self.assertEqual(keep["2026-10-27"], {"n_markets": 6, "n_wins": 3,
                                              "total_cost": 170.96, "pnl": 20.04})

    def test_kept_days_outlive_the_fills_window(self):
        stored = {"2026-10-25": {"n_markets": 6, "n_wins": 1, "total_cost": 85.86, "pnl": -56.61},
                  "2026-10-26": {"n_markets": 5, "n_wins": 3, "total_cost": 65.18, "pnl": 32.99}}
        # Kalshi's window now starts on 10/26 and may have cut it part-way
        fills = frame([("2026-10-26", 2, 1, 20.0, 5.0),
                       ("2026-10-27", 6, 3, 170.96, 20.04)], window_from="2026-10-26")
        df, keep = ld.merge_daily_pnl(pd.DataFrame(), fills, stored)
        self.assertEqual(keep["2026-10-25"]["pnl"], -56.61)   # gone from the fills: kept
        self.assertEqual(keep["2026-10-26"]["pnl"], 32.99)    # the window's first day: stored wins
        self.assertEqual(keep["2026-10-27"]["pnl"], 20.04)
        self.assertEqual(list(df["event_date"]), ["2026-10-27", "2026-10-26", "2026-10-25"])

    def test_a_day_inside_the_window_is_taken_fresh(self):
        stored = {"2026-10-28": {"n_markets": 7, "n_wins": 3, "total_cost": 60.0, "pnl": 4.0}}
        fills = frame([("2026-10-28", 10, 4, 100.32, -2.08)], window_from="2026-08-24")
        _, keep = ld.merge_daily_pnl(pd.DataFrame(), fills, stored)
        self.assertEqual(keep["2026-10-28"]["pnl"], -2.08)   # more of it settled since
        # the window's first day with nothing stored is taken as it is
        fills = frame([("2026-10-28", 10, 4, 100.32, -2.08)], window_from="2026-10-28")
        _, keep = ld.merge_daily_pnl(pd.DataFrame(), fills, {})
        self.assertEqual(keep["2026-10-28"]["pnl"], -2.08)

    def test_nothing_loaded(self):
        df, keep = ld.merge_daily_pnl(pd.DataFrame(), pd.DataFrame(), {})
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), ["event_date", "n_markets", "n_wins", "total_cost", "pnl"])
        self.assertEqual(keep, {})
        # a failed bot-fills query leaves the kept days as they were
        stored = {"2026-10-25": {"n_markets": 6, "n_wins": 1, "total_cost": 85.86, "pnl": -56.61}}
        df, keep = ld.merge_daily_pnl(pd.DataFrame(), pd.DataFrame(), stored)
        self.assertEqual(keep, stored)
        self.assertEqual(list(df["event_date"]), ["2026-10-25"])

    def test_queries_use_kalshis_basis(self):
        # settlements_clean's pnl_dollars / total_cost_dollars book NO fills at the YES price
        for sql in (ld.SQL_DAILY_PNL_SETTLED, ld.SQL_DAILY_PNL_BOT_FILLS):
            self.assertIsNone(re.search(r"(?<![a-z_])(pnl_dollars|total_cost_dollars)", sql))
            self.assertIn(ld.IMM_ERA_FROM, sql)
        self.assertIn("raw_no_price_dollars", ld.SQL_DAILY_PNL_BOT_FILLS)
        self.assertIn("client_order_id", ld.SQL_DAILY_PNL_BOT_FILLS)
        self.assertNotIn("INTERVAL 30 DAY", ld.SQL_DAILY_PNL_SETTLED)


if __name__ == "__main__":
    unittest.main()
