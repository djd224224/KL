"""Tests for imm_backfill_daily_pnl's per-day record (2026-09-29).

A scalar-settled market (the NFL ladders / escalators settle "scalar" at a
fractional settlement_value_dollars) used to get neither a settle nor a mark:
its result is not yes / no, and its book reads bid 0 / ask 100, which the
mid-mark skips. The position vanished from the rebuilt daily_pnl.json.

imm_backfill_daily_pnl is imported inside setUp. A module-level import here
would make this file the first to import incentive_mm during discovery, ahead
of the modules whose env setup the rest of the suite runs under."""
import unittest

LADDER = "KXNFLFFPTSLADDER-26SEP27LVNO-LVAJEANTY2"
TS = 1_790_000_000


def _market(ticker, result="", status="active", value=None,
            bid="0.0000", ask="1.0000", last="0.8200"):
    m = {"ticker": ticker, "result": result, "status": status,
         "yes_bid_dollars": bid, "yes_ask_dollars": ask, "last_price_dollars": last}
    if value is not None:
        m["settlement_value_dollars"] = value
    return m


def _fill(ticker, side, action, count, yes_cents, ts=TS, fee=0.0):
    return {"ticker": ticker, "side": side, "action": action,
            "count_fp": f"{count:.2f}", "yes_price_dollars": f"{yes_cents / 100:.4f}",
            "fee_cost": f"{fee:.4f}", "ts": ts}


class _BackfillTest(unittest.TestCase):
    def setUp(self):
        import imm_backfill_daily_pnl as bf
        self.bf = bf


class SettlePriceTests(_BackfillTest):
    def test_yes_no_scalar_void(self):
        px = self.bf.settle_price_cents
        self.assertEqual(px(_market("A", "yes", "determined"), 30.0), 100.0)
        self.assertEqual(px(_market("A", "no", "finalized"), 30.0), 0.0)
        self.assertEqual(px(_market(LADDER, "scalar", "finalized", "0.2160"), 30.0), 21.6)
        self.assertEqual(px(_market(LADDER, "scalar", "settled", "0.0000"), 30.0), 0.0)
        self.assertEqual(px(_market(LADDER, "void", "finalized"), 37.5), 37.5)    # refunds cost

    def test_unsettled_not_final_or_unreadable_is_none(self):
        px = self.bf.settle_price_cents
        self.assertIsNone(px(_market(LADDER), 30.0))
        self.assertIsNone(px(_market(LADDER, "scalar", "determined", "0.1200"), 30.0))
        self.assertIsNone(px(_market(LADDER, "void", "closed"), 30.0))
        self.assertIsNone(px(_market(LADDER, "scalar", "finalized"), 30.0))
        self.assertIsNone(px(_market(LADDER, "scalar", "finalized", "1.2"), 30.0))


class DayPnlTests(_BackfillTest):
    def test_a_scalar_settled_residual_is_booked_not_dropped(self):
        rec = self.bf.day_pnl([_fill(LADDER, "no", "buy", 90, 16)],
                              {LADDER: _market(LADDER, "scalar", "finalized", "0.1200")})
        self.assertEqual((rec["settle"], rec["mtm"], rec["raw"]), (3.6, 0.0, 3.6))   # was 0 / 0 / 0
        self.assertEqual((rec["contracts"], rec["fills"]), (90.0, 1))

    def test_a_void_refunds_cost_and_realized_stands(self):
        fills = [_fill("V", "yes", "buy", 30, 40, ts=TS), _fill("V", "yes", "sell", 10, 45, ts=TS + 1)]
        rec = self.bf.day_pnl(fills, {"V": _market("V", "void", "finalized")})
        self.assertEqual((rec["realized"], rec["settle"], rec["mtm"], rec["raw"]), (0.5, 0.0, 0.0, 0.5))

    def test_yes_no_settle_and_open_mark_unchanged(self):
        fills = [_fill("Y", "yes", "buy", 10, 30), _fill("O", "yes", "buy", 10, 20, fee=0.07)]
        rec = self.bf.day_pnl(fills, {"Y": _market("Y", "yes", "determined"),
                                      "O": _market("O", bid="0.3000", ask="0.3400")})
        self.assertEqual((rec["settle"], rec["mtm"], rec["fees"]), (7.0, 1.2, 0.07))
        self.assertAlmostEqual(rec["raw"], 7.0 + 1.2 - 0.07, places=6)

    def test_a_scalar_not_yet_finalized_is_marked_at_the_last_trade_like_the_digest(self):
        # bid 0 / ask 100 with a last trade of 82: imm.bulk_mark_cents marks
        # it there, as send_imm_digest's daily_pnl.json upsert does
        # (test_send_imm_digest: +6.60); it books once Kalshi finalizes. This
        # rebuild used to leave it unmarked, so the store's two writers
        # disagreed on the same day.
        rec = self.bf.day_pnl([_fill(LADDER, "yes", "buy", 10, 16)],
                              {LADDER: _market(LADDER, "scalar", "determined", "0.1200")})
        self.assertEqual((rec["settle"], rec["mtm"], rec["raw"]), (0.0, 6.6, 6.6))
        # with no trade to go on it stays unmarked
        rec = self.bf.day_pnl([_fill(LADDER, "yes", "buy", 10, 16)],
                              {LADDER: _market(LADDER, "scalar", "determined", "0.1200", last="0.0000")})
        self.assertEqual((rec["settle"], rec["mtm"], rec["raw"]), (0.0, 0.0, 0.0))

    def test_a_one_sided_book_marks_at_the_last_trade_clamped_to_its_side(self):
        """2026-10-01: Kalshi reads an empty ask as $1.00, and the mark took
        (bid + ask) / 2 whenever both read nonzero. KXDDCOLDBREW-26OCT02-T4.45:
        the bot short 58.64 YES at 91.5c, a stray 5c bid, no offer, a 97c last
        trade -- marked 52.5 (+22.87) before the fix, settled YES."""
        rec = self.bf.day_pnl([_fill("C", "no", "buy", 58.64, 91.5)],
                              {"C": _market("C", bid="0.0500", ask="1.0000", last="0.9700")})
        self.assertEqual((rec["settle"], rec["mtm"]), (0.0, -3.23))      # -58.64 x (97 - 91.5)
        # only an offer: the last trade capped at the ask (was unmarked)
        rec = self.bf.day_pnl([_fill("A", "yes", "buy", 10, 20)],
                              {"A": _market("A", bid="0.0000", ask="0.4000", last="0.5500")})
        self.assertEqual(rec["mtm"], 2.0)                                # 10 x (40 - 20)
        # one-sided with no last trade: no mark
        rec = self.bf.day_pnl([_fill("Z", "yes", "buy", 10, 20)],
                              {"Z": _market("Z", bid="0.0500", ask="1.0000", last="0.0000")})
        self.assertEqual(rec["mtm"], 0.0)

    def test_a_50c_wide_book_marks_at_the_last_trade_inside_the_touch(self):
        # Jack 2026-10-01, "Yes, at 50c+": KXDKNGAPP-26OCT08-T185, long 80 on
        # a 1/70 book under a 1c last trade, marked at the 35.5 mid before
        rec = self.bf.day_pnl([_fill("W", "yes", "buy", 80, 10)],
                              {"W": _market("W", bid="0.0100", ask="0.7000", last="0.0100")})
        self.assertEqual(rec["mtm"], -7.2)                               # 80 x (1 - 10), not +20.40


if __name__ == "__main__":
    unittest.main()
