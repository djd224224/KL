"""high_temp_trading.fetch_all_orders leaves the IMM's orders out (2026-10-23).

high_temp_trading.py talks to Kalshi on import, so -- like
test_high_temp_sizing.py -- this lifts the function out of the source and
runs it against a stub client: the cancel sweep and the resting-cap counts
read orders only through it, so an "imm-" order is never cancelled or
counted by the high bot.

    python -m unittest test_high_temp_orders
"""
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
