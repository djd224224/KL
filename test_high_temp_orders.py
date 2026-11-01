"""high_temp_trading.fetch_all_orders leaves the IMM's orders out (2026-10-23),
and the caps count only this bot's own fills (2026-11-01).

high_temp_trading.py talks to Kalshi on import, so -- like
test_high_temp_sizing.py -- this lifts the function out of the source and
runs it against a stub client: the cancel sweep and the resting-cap counts
read orders only through it, so an "imm-" order is never cancelled or
counted by the high bot.

    python -m unittest test_high_temp_orders
"""
import os
import unittest
import uuid

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "high_temp_trading.py")


def _fetch_all_orders(pages):
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    start = src.index("def fetch_all_orders(ticker, max_pages=10):")
    end = src.index("    return orders\n", start) + len("    return orders\n")

    class Client:
        def __init__(self):
            self.calls = 0

        def get_orders(self, ticker, limit, cursor):
            page = pages[self.calls]
            self.calls += 1
            return page

    g = {"exchange_client": Client()}
    exec(src[start:end], g)
    return g["fetch_all_orders"]


class TestFetchAllOrders(unittest.TestCase):

    def test_the_imms_orders_are_left_out_across_pages(self):
        fetch = _fetch_all_orders([
            {"orders": [{"order_id": "a", "client_order_id": "3f2a-uuid"},
                        {"order_id": "b", "client_order_id": "imm-91ab-4c2d"}],
             "cursor": "next"},
            {"orders": [{"order_id": "c", "client_order_id": None},
                        {"order_id": "d", "client_order_id": "imm-77"}],
             "cursor": None}])
        self.assertEqual([o["order_id"] for o in fetch("KXHIGHNY-26OCT24-B62.5")], ["a", "c"])


def _own_helpers():
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _is_bot_order(o):")
    end = src.index("def fetch_market_filled_cash(", start)
    g = {"uuid": uuid}
    exec(src[start:end], g)
    return g


def _filled_cash(pages):
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    start = src.index("def fetch_market_filled_cash(")
    marker = "    return cash, False  # page cap hit"
    end = src.index("\n", src.index(marker, start)) + 1

    class Client:
        def __init__(self):
            self.calls = 0

        def get_fills(self, ticker, limit, cursor):
            page = pages[self.calls]
            self.calls += 1
            return page

    g = {"exchange_client": Client()}
    exec(src[start:end], g)
    return g["fetch_market_filled_cash"]


BOT_A, BOT_B = str(uuid.uuid4()), str(uuid.uuid4())


class TestOwnPositionCaps(unittest.TestCase):
    """Jack 2026-11-01: "have the bot count only its own positions against its
    caps" -- the account's position and fills hold the IMM's KXHIGH too."""

    def test_only_this_bots_uuid_orders_are_its_own(self):
        own = _own_helpers()["_is_bot_order"]
        self.assertTrue(own({"client_order_id": BOT_A}))
        for coid in ("imm-91ab-4c2d", None, "", "3f2a-uuid", BOT_A.upper(), BOT_A + "x"):
            self.assertFalse(own({"client_order_id": coid}), coid)

    def test_position_is_this_bots_fills_only(self):
        pos = _own_helpers()["own_position"]
        orders = [
            {"client_order_id": BOT_A, "side": "no", "action": "buy", "filled_count": 120.0},
            {"client_order_id": BOT_B, "side": "no", "action": "buy", "filled_count": 30.5},
            {"client_order_id": BOT_B, "side": "no", "action": "sell", "filled_count": 10.0},
            {"client_order_id": "imm-1-ab", "side": "yes", "action": "buy", "filled_count": 900.0},
            {"client_order_id": None, "side": "no", "action": "buy", "filled_count": 5.0},
            {"client_order_id": BOT_A, "side": "no", "action": "buy", "filled_count": None},
        ]
        self.assertEqual(pos(orders), 140.5)
        # YES bought offsets NO held; the size is what counts against the cap
        self.assertEqual(pos([{"client_order_id": BOT_A, "side": "yes", "action": "buy",
                               "filled_count": 25.0}]), 25.0)
        self.assertEqual(pos([]), 0.0)

    def test_cash_cap_counts_only_the_given_orders_fills(self):
        pages = [{"fills": [{"order_id": "o1", "side": "no", "action": "buy",
                             "count_fp": "100.00", "no_price_dollars": "0.9100"},
                            {"order_id": "imm1", "side": "no", "action": "buy",
                             "count_fp": "3000.00", "no_price_dollars": "0.8000"}],
                  "cursor": "next"},
                 {"fills": [{"order_id": "o2", "side": "no", "action": "buy",
                             "count_fp": "20.00", "no_price_dollars": "0.5000"}],
                  "cursor": None}]
        cash, complete = _filled_cash([dict(p) for p in pages])("T", order_ids={"o1", "o2"})
        self.assertTrue(complete)
        self.assertAlmostEqual(cash, 91.0 + 10.0)
        cash, _ = _filled_cash([dict(p) for p in pages])("T")       # the account's, as before
        self.assertAlmostEqual(cash, 91.0 + 2400.0 + 10.0)


if __name__ == "__main__":
    unittest.main()
