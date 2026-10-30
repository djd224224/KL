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


M1, M2, M3 = "KXHIGHNY-26OCT28-B60.5", "KXHIGHNY-26OCT28-B62.5", "KXHIGHNY-26OCT28-B64.5"


def cards_for(bot_positions):
    """One NY card: M1 this bot's and settled, M2 only the IMM's, M3 this
    bot's and still open (YES 20x30, so NO marks at 75c)."""
    snap = pd.DataFrame([{
        "market_ticker": t, "low_range": lo, "high_range": lo + 2, "yes_probability": 0.2,
        "forecast_date": "2026-10-28", "forecast_avg": 63.0, "forecast_std": 2.0, "nws": 63.0,
        "accuweather": 62.0, "weather_underground": 64.0, "run_date": "2026-10-28 08:00:00",
        "position": 0,
    } for t, lo in ((M1, 59.5), (M2, 61.5), (M3, 63.5))])
    book = pd.DataFrame([{
        "market_ticker": t, "no_highest_bid": 100 - ya, "no_lowest_offer": 100 - yb,
        "yes_highest_bid": yb, "yes_lowest_offer": ya, "polled_at": "2026-10-29 00:00:00",
        "market_status": "open",
    } for t, yb, ya in ((M1, 1, 2), (M2, 98, 99), (M3, 20, 30))])
    # the whole account, the IMM's inventory in it
    positions = pd.DataFrame([
        {"market_ticker": M2, "position": -500.0, "market_exposure_dollars": 400.0,
         "realized_pnl_dollars": 0.0, "total_traded_dollars": 400.0, "fees_paid_dollars": 0.0,
         "resting_orders_count": 3},
        {"market_ticker": M3, "position": -510.0, "market_exposure_dollars": 403.0,
         "realized_pnl_dollars": 0.0, "total_traded_dollars": 403.0, "fees_paid_dollars": 0.0,
         "resting_orders_count": 2}])
    settled = pd.DataFrame([
        {"market_ticker": M1, "settled_pnl_dollars": 999.0, "settled_cost_dollars": 777.0,
         "settled_fees_dollars": 0.0, "settled_payout_dollars": 0.0, "settled_result": "no",
         "settled_net_position": -800.0},
        {"market_ticker": M2, "settled_pnl_dollars": -300.0, "settled_cost_dollars": 400.0,
         "settled_fees_dollars": 0.0, "settled_payout_dollars": 0.0, "settled_result": "yes",
         "settled_net_position": -500.0}])
    return ld.per_city_view(snap, book, positions, settled_pnl=settled,
                            bot_positions=bot_positions)


class CardPnlTests(unittest.TestCase):
    """Jack 2026-10-29: "Yes, fix cards" -- the cards count this bot's own
    fills, not the whole account's positions (the IMM's KXHIGH inventory)."""

    def test_cards_count_this_bots_own_fills(self):
        bot = pd.DataFrame([
            {"market_ticker": M1, "bot_position": -40.0, "bot_cost": 12.0, "bot_fees": 0.0,
             "bot_settled_pnl": 28.0},                      # 40 NO @ 30c, settled NO
            {"market_ticker": M3, "bot_position": -10.0, "bot_cost": 3.0, "bot_fees": 0.0,
             "bot_settled_pnl": float("nan")}])
        (card,) = cards_for(bot)
        self.assertEqual([b["ticker"] for b in card["positions"]], [M1, M3])   # not the IMM's M2
        b1, b2, b3 = card["buckets"]
        self.assertEqual((b1["position"], b1["exposure_dollars"], b1["realized_pnl"]), (-40.0, 12.0, 28.0))
        self.assertTrue(b1["closed"])
        self.assertIsNone(b1["open_pnl"])                   # settled: no open P&L on top
        self.assertEqual((b2["position"], b2["exposure_dollars"], b2["realized_pnl"]), (0.0, None, None))
        self.assertFalse(b3["closed"])
        self.assertAlmostEqual(b3["open_pnl"], 10 * 0.75 - 3.0)
        self.assertEqual(card["cost_basis"], 15.0)
        self.assertEqual(card["realized_pnl_today"], 28.0)
        self.assertAlmostEqual(card["live_pnl"], 28.0 + 4.5)
        # an empty frame: this bot holds nothing, whatever the account holds
        (card,) = cards_for(pd.DataFrame())
        self.assertEqual(card["positions"], [])
        self.assertEqual(card["realized_pnl_today"], 0)

    def test_a_failed_query_falls_back_to_the_account(self):
        (card,) = cards_for(None)
        self.assertEqual([b["ticker"] for b in card["positions"]], [M1, M2, M3])
        self.assertEqual(card["realized_pnl_today"], 999.0 - 300.0)


if __name__ == "__main__":
    unittest.main()
