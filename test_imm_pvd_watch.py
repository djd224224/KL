"""imm_pvd_watch's page-row helpers (the wave dashboards)."""
import os
import tempfile
import unittest

os.environ.setdefault("IMM_STATUS_DIR", os.path.join(tempfile.gettempdir(), "imm_pvd_watch_test_status"))
os.makedirs(os.environ["IMM_STATUS_DIR"], exist_ok=True)

import imm_pvd_watch as w  # noqa: E402


class TestUnwindState(unittest.TestCase):
    """Jack 2026-11-19: "make that clear in the dashboard, what has resting positions
    already and are unwinding"."""

    def row(self, pos, bid=None, ask=None):
        rest = 300 if (bid is not None or ask is not None) else 0
        return {"pos": pos, "our_bid": bid, "our_ask": ask, "rest_ct": rest}

    def test_flat_is_blank(self):
        self.assertEqual(w.unwind_state(self.row(0, 5, 95)), "")
        self.assertEqual(w.unwind_state(self.row(0.4, 5, None)), "")

    def test_only_the_closing_order_is_unwinding(self):
        # short YES: the closing order is our bid; long YES: our ask
        self.assertEqual(w.unwind_state(self.row(-200, bid=25)), "unwinding")
        self.assertEqual(w.unwind_state(self.row(200, ask=10)), "unwinding")

    def test_the_adding_side_resting_is_holding_and_quoting(self):
        self.assertEqual(w.unwind_state(self.row(-200, bid=25, ask=26)), "holding + quoting")
        self.assertEqual(w.unwind_state(self.row(200, bid=9)), "holding + quoting")

    def test_nothing_resting(self):
        self.assertEqual(w.unwind_state(self.row(-4)), "unwinding, nothing resting")
        self.assertIn("unwinding, nothing resting", w.UNWIND_STATES)
        self.assertNotIn("holding + quoting", w.UNWIND_STATES)


def mkt(last=None, bid=None, ask=None, result="", status="active", value=None):
    """A /markets record in the V2 dollar fields (an empty bid reads 0, an empty ask 1.00)."""
    return {"last_price_dollars": f"{last / 100:.4f}" if last is not None else "0.0000",
            "yes_bid_dollars": f"{bid / 100:.4f}" if bid is not None else "0.0000",
            "yes_ask_dollars": f"{ask / 100:.4f}" if ask is not None else "1.0000",
            "result": result, "status": status,
            "settlement_value_dollars": None if value is None else f"{value / 100:.4f}"}


class TestTradingPL(unittest.TestCase):
    """Jack 2026-11-19, the w1113-fxmetals page: "the P&L looks broken"."""

    def test_a_settled_market_is_realized_at_what_it_paid(self):
        # KXUSDJPYEVENT-15NOV261950-T15600: 180 YES sold at 93c, settled YES 11/16. The page
        # showed -$10.80 OPEN at the 99c last trade; Kalshi paid -$12.60
        m = mkt(last=99, ask=None, result="yes", status="finalized", value=100)
        fx = w.fifo([(-180, 93.0)], w.mark_fn(m), w.settle_cents(m))
        self.assertAlmostEqual(fx["real"], -12.60)
        self.assertEqual((fx["open"], fx["net"], fx["cost"], fx["lots"]), (0.0, 0, 0.0, []))
        # KXGBPUSDEVENT-17NOV260930-T13550: 10 bought at 2c, settled NO
        m = mkt(last=2, result="no", status="finalized", value=0)
        fx = w.fifo([(10, 2.0)], w.mark_fn(m), w.settle_cents(m))
        self.assertAlmostEqual(fx["real"], -0.20)
        self.assertEqual(fx["cost"], 0.0)

    def test_settle_cents_follows_the_bots_settlement_rule(self):
        self.assertEqual(w.settle_cents(mkt(result="yes", status="determined")), 100.0)
        self.assertEqual(w.settle_cents(mkt(result="no")), 0.0)
        self.assertIsNone(w.settle_cents(mkt(last=40, bid=39, ask=41)))
        # a scalar pays its settlement value, once Kalshi finalizes it
        self.assertAlmostEqual(w.settle_cents(mkt(result="scalar", status="finalized", value=21.6)), 21.6)
        self.assertIsNone(w.settle_cents(mkt(result="scalar", status="determined", value=21.6)))
        # a void refunds cost: the lots close at their own price
        m = mkt(result="void", status="settled")
        self.assertEqual(w.settle_cents(m), "void")
        fx = w.fifo([(50, 30.0), (-20, 35.0)], w.mark_fn(m), w.settle_cents(m))
        self.assertAlmostEqual(fx["real"], 1.0)              # the 20 matched at 35 vs 30 only
        self.assertEqual((fx["open"], fx["net"]), (0.0, 0))

    def test_open_positions_stay_at_the_last_trade(self):
        # Kalshi values the account at the last trade (Jack 11/09: "conform to last-traded"):
        # T16150, 825 bought at 17c, last 4c on a 7 x 15 book
        fx = w.fifo([(825, 17.0)], w.mark_fn(mkt(last=4, bid=7, ask=15)))
        self.assertAlmostEqual(fx["open"], -107.25)
        self.assertEqual(fx["mark"], 4.0)
        self.assertAlmostEqual(fx["cost"], 140.25)

    def test_no_last_trade_is_never_the_mid_of_a_wide_book(self):
        # 11:53Z: T16150 read moments before its first trade (our 825 at 17c) on a 17 x 80
        # book. The plain mid, 48.5c, showed +$259.87; the bot's rule exits a long at the bid
        fx = w.fifo([(825, 17.0)], w.mark_fn(mkt(bid=17, ask=80)))
        self.assertEqual(fx["mark"], 17.0)
        self.assertAlmostEqual(fx["open"], 0.0)
        # a short exits at the ask; a narrow two-sided book marks at its mid
        self.assertEqual(w.fifo([(-100, 60.0)], w.mark_fn(mkt(bid=5, ask=70)))["mark"], 70.0)
        self.assertEqual(w.fifo([(100, 10.0)], w.mark_fn(mkt(bid=10, ask=14)))["mark"], 12.0)
        # nothing on the book, never traded: cost (the empty ask's $1.00 is no price)
        fx = w.fifo([(100, 10.0)], w.mark_fn(mkt()))
        self.assertEqual((fx["mark"], fx["open"]), (10.0, 0.0))
        # a flat market needs no mark
        self.assertIsNone(w.fifo([(100, 10.0), (-100, 12.0)], w.mark_fn(mkt()))["mark"])

    def test_the_latest_block_keeps_every_period_still_running(self):
        # 11/19 17:01Z: 439 new strikes listed on a period of their own while the 336 of the
        # 11/18 relight still ran; the block showed only the newest and read $0.02
        rows = {"old": {"period": "2026-11-13T18:01:49Z", "active": False},
                "a": {"period": "2026-11-18T19:01:20Z", "active": True},
                "b": {"period": "2026-11-19T17:01:01Z", "active": True},
                "x": {"period": "wave-1104", "active": True}}
        self.assertEqual(w.latest_periods(rows), {"2026-11-18T19:01:20Z", "2026-11-19T17:01:01Z"})
        # once every period has ended: the newest one, as before
        for r in rows.values():
            r["active"] = False
        self.assertEqual(w.latest_periods(rows), {"2026-11-19T17:01:01Z"})
        self.assertEqual(w.latest_periods({"x": {"period": "wave-1104", "active": True}}), set())


if __name__ == "__main__":
    unittest.main()
