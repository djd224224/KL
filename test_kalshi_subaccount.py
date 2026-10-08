"""Tests for kalshi_subaccount (the owner's subaccount helper). A fake client;
nothing reaches Kalshi."""

import json
import unittest

import kalshi_subaccount as ks


class FakeClient:
    def __init__(self, balances=None):
        self.posts, self.gets = [], []
        self.balances = balances or {}

    def get(self, path, params):
        self.gets.append((path, params))
        if path == "/portfolio/balance":
            n = (params or {}).get("subaccount", 0)
            return {"balance_dollars": f"{self.balances.get(n, 0.0):.4f}",
                    "balance_breakdown": [{"balance": "18373.1369", "exchange_index": 0}]}
        return {"transfers": []}

    def post(self, path, body):
        self.posts.append((path, json.loads(body)))
        if path == "/portfolio/subaccounts":
            return {"subaccount_number": 1}
        return {}


class TestSubaccountHelper(unittest.TestCase):
    def test_transfer_body(self):
        b = ks.transfer_body(0, 1, 300)
        self.assertEqual({k: b[k] for k in ("from_subaccount", "to_subaccount",
                                           "amount_cents", "exchange_index")},
                         {"from_subaccount": 0, "to_subaccount": 1,
                          "amount_cents": 30000, "exchange_index": 0})
        self.assertEqual(len(b["client_transfer_id"]), 36)          # a uuid
        self.assertEqual(ks.transfer_body(1, 0, 12.345)["amount_cents"], 1234)
        with self.assertRaises(ValueError):
            ks.transfer_body(0, 1, 0)
        with self.assertRaises(ValueError):
            ks.transfer_body(1, 1, 5)

    def test_create_needs_the_word(self):
        c = FakeClient()
        self.assertEqual(ks.main(["create"], c=c, ask=lambda p: "yes"), 1)
        self.assertEqual(c.posts, [])
        self.assertEqual(ks.main(["create"], c=c, ask=lambda p: "CREATE"), 0)
        self.assertEqual(c.posts, [("/portfolio/subaccounts", {})])

    def test_fund_and_withdraw(self):
        c = FakeClient(balances={1: 300.0})
        self.assertEqual(ks.main(["fund", "1", "300"], c=c, ask=lambda p: "no"), 1)
        self.assertEqual(c.posts, [])
        self.assertEqual(ks.main(["fund", "1", "300"], c=c, ask=lambda p: "MOVE"), 0)
        path, body = c.posts[-1]
        self.assertEqual(path, "/portfolio/subaccounts/transfer")
        self.assertEqual((body["from_subaccount"], body["to_subaccount"],
                          body["amount_cents"]), (0, 1, 30000))
        ks.main(["withdraw", "1", "50"], c=c, ask=lambda p: "MOVE")
        body = c.posts[-1][1]
        self.assertEqual((body["from_subaccount"], body["to_subaccount"],
                          body["amount_cents"]), (1, 0, 5000))

    def test_balance_is_the_subaccounts_own(self):
        c = FakeClient(balances={1: 42.5})
        self.assertEqual(ks.balance(c, 1), 42.5)                  # not the 18,373
        self.assertEqual(c.gets[-1], ("/portfolio/balance", {"subaccount": 1}))


if __name__ == "__main__":
    unittest.main()
