"""Tests for send_imm_digest's settlement valuation (2026-09-29).

Kalshi settles the NFL ladders / escalators (and the early-closed KXBKNUGGETS
strikes) as result "scalar" at a fractional settlement_value_dollars, and a
void refunds cost. The digest booked only yes / no: a scalar-settled position
stayed "open" and was marked at the last trade, because a settled book reads
0 / 100. KXNFLFFPTSLADDER-26SEP27LVNO-LVAJEANTY2 settled at 12c and was
carried at 82c; the bot was short 90 YES at 16c there, so the digest showed
-$59.40 on a position that made +$3.60.

send_imm_digest is imported inside setUp, as test_imm_pickoff does: importing
it mirrors the live launcher env into os.environ, and a module-level import
would do that during discovery, ahead of other test modules' imports."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

LADDER = "KXNFLFFPTSLADDER-26SEP27LVNO-LVAJEANTY2"
TS = 1_790_000_000


def _market(ticker, result="", status="active", value=None,
            bid="0.0000", ask="1.0000", last="0.8200"):
    """A Kalshi market record; the defaults are LVAJEANTY2's settled book."""
    m = {"ticker": ticker, "result": result, "status": status,
         "yes_bid_dollars": bid, "yes_ask_dollars": ask, "last_price_dollars": last}
    if value is not None:
        m["settlement_value_dollars"] = value
    return m


def _fill(ticker, side, action, count, yes_cents, ts=TS, fee=0.0):
    """A fill as the normalized client returns it (legacy customer view)."""
    return {"ticker": ticker, "side": side, "action": action,
            "count_fp": f"{count:.2f}", "yes_price_dollars": f"{yes_cents / 100:.4f}",
            "fee_cost": f"{fee:.4f}", "ts": ts}


class _Client:
    """get_markets(tickers=..., limit=...) over fixed market records."""

    def __init__(self, *markets):
        self.markets = {m["ticker"]: m for m in markets}

    def get_markets(self, tickers="", limit=None):
        return {"markets": [self.markets[t] for t in tickers.split(",")
                            if t in self.markets]}


class _DigestTest(unittest.TestCase):
    def setUp(self):
        import send_imm_digest as sd
        self.sd = sd

    def raw(self, fills, *markets):
        """raw_pnl_for_fills totals, with mids / results read the digest's
        way from `markets`."""
        client = _Client(*markets)
        mids, results = self.sd.current_mids(client, {f["ticker"] for f in fills})
        tot, per_event, _pnl = self.sd.raw_pnl_for_fills(client, fills, mids, results)
        return tot, per_event


class SettlementCentsTests(_DigestTest):
    def test_yes_no_pay_100_0_at_any_status(self):
        # unchanged: a yes / no result is booked as soon as Kalshi reports it
        for status in ("determined", "finalized", "closed"):
            self.assertEqual(self.sd.settlement_cents(_market("A", "yes", status)), 100.0)
            self.assertEqual(self.sd.settlement_cents(_market("A", "no", status)), 0.0)

    def test_finalized_scalar_pays_its_exact_value(self):
        m = _market(LADDER, "scalar", "finalized", "0.2160")
        self.assertEqual(self.sd.settlement_cents(m), 21.6)        # not 22
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "scalar", "settled", "0.1200")), 12.0)
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "Scalar", "FINALIZED", "0.0000")), 0.0)
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "scalar", "finalized", "1.0000")), 100.0)

    def test_scalar_and_void_wait_for_finalized(self):
        for status in ("determined", "closed", "active", ""):
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "scalar", status, "0.1200")))
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "void", status)))

    def test_scalar_without_a_readable_value_is_not_settled(self):
        for value in (None, "", "n/a", "1.5000", "-0.0100"):
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "scalar", "finalized", value)),
                              value)

    def test_void_and_unsettled(self):
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "void", "finalized")), self.sd.VOID)
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER)))
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "", "finalized")))
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "mystery", "finalized", "0.5")))


class CurrentMidsTests(_DigestTest):
    def test_settled_markets_are_results_open_ones_are_mids(self):
        client = _Client(_market(LADDER, "scalar", "finalized", "0.1200"),
                         _market("V", "void", "finalized", last="0.4000"),
                         _market("Y", "yes", "determined", bid="0.0000", ask="0.0000", last="0.9900"),
                         _market("N", "no", "finalized", last="0.0300"),
                         _market("D", "scalar", "determined", "0.1200"),
                         _market("O", bid="0.3000", ask="0.3400", last="0.2900"))
        mids, results = self.sd.current_mids(client, ["O", LADDER, "V", "Y", "N", "D"])
        self.assertEqual(results, {LADDER: 12.0, "V": self.sd.VOID, "Y": 100.0, "N": 0.0})
        # a settled book reads 0 / 100, so its "mid" is the last trade: 82c
        # here, which is exactly why a settled position is valued from results
        self.assertEqual(mids[LADDER], 82.0)
        self.assertEqual(mids["D"], 82.0)       # not final yet: still marked
        self.assertEqual(mids["O"], 32.0)

    def test_a_one_sided_book_is_marked_at_the_last_trade_clamped_to_its_side(self):
        """2026-10-01: Kalshi reads an empty ask as $1.00, and the digest (like
        the bot's bulk mark refresh) averaged it in -- KXDDCOLDBREW-26OCT02-
        T4.45's stray 5c bid under a 97c last trade read 52.5. Now the bot's
        own rule, incentive_mm.bulk_mark_cents."""
        client = _Client(_market("C", bid="0.0500", ask="1.0000", last="0.9700"),
                         _market("B", bid="0.3200", ask="1.0000", last="0.2000"),
                         _market("A", bid="0.0000", ask="0.4000", last="0.5500"),
                         _market("Z", bid="0.0500", ask="1.0000", last="0.0000"),
                         # 50c+ wide (Jack 2026-10-01): the last trade inside
                         # the touch, not the 35.5 mid (KXDKNGAPP-26OCT08-T185)
                         _market("W", bid="0.0100", ask="0.7000", last="0.0100"))
        mids, results = self.sd.current_mids(client, ["C", "B", "A", "Z", "W"])
        self.assertEqual(mids, {"C": 97.0, "B": 32.0, "A": 40.0, "W": 1.0})
        self.assertEqual(results, {})


class RawPnlForFillsTests(_DigestTest):
    def test_lvajeanty2_short_books_the_scalar_settlement(self):
        # the ask fills read "buy NO" once normalized: short 90 YES at 16c
        tot, per_event = self.raw([_fill(LADDER, "no", "buy", 90, 16)],
                                  _market(LADDER, "scalar", "finalized", "0.1200"))
        self.assertAlmostEqual(tot["settle"], 3.60, places=6)       # -90 x (12 - 16)
        self.assertEqual(tot["unrealized"], 0.0)                    # was -90 x (82 - 16) = -59.40
        self.assertEqual(tot["open_markets"], 0)
        self.assertAlmostEqual(tot["raw"], 3.60, places=6)
        self.assertAlmostEqual(per_event["KXNFLFFPTSLADDER-26SEP27LVNO"]["settle"], 3.60, places=6)

    def test_a_sub_penny_value_is_not_rounded(self):
        tot, _ = self.raw([_fill(LADDER, "yes", "buy", 100, 20)],
                          _market(LADDER, "scalar", "finalized", "0.2160"))
        self.assertAlmostEqual(tot["settle"], 1.60, places=6)       # 22c would say 2.00

    def test_a_scalar_settled_at_zero_still_settles(self):
        tot, _ = self.raw([_fill(LADDER, "yes", "buy", 10, 5)],
                          _market(LADDER, "scalar", "finalized", "0.0000"))
        self.assertAlmostEqual(tot["settle"], -0.50, places=6)
        self.assertEqual((tot["unrealized"], tot["open_markets"]), (0.0, 0))

    def test_realized_and_fees_stay_on_top_of_the_residual_settle(self):
        fills = [_fill(LADDER, "yes", "buy", 50, 16, ts=TS),
                 _fill(LADDER, "yes", "sell", 20, 30, ts=TS + 60, fee=0.10)]
        tot, _ = self.raw(fills, _market(LADDER, "scalar", "finalized", "0.1200"))
        self.assertAlmostEqual(tot["realized"], 2.80, places=6)     # 20 x (30 - 16)
        self.assertAlmostEqual(tot["settle"], -1.20, places=6)      # 30 x (12 - 16)
        self.assertAlmostEqual(tot["raw"], 2.80 - 1.20 - 0.10, places=6)

    def test_a_void_refunds_cost(self):
        tot, _ = self.raw([_fill("V", "yes", "buy", 25, 40)],
                          _market("V", "void", "finalized", bid="0.0000", ask="0.0000", last="0.9000"))
        self.assertEqual((tot["settle"], tot["unrealized"], tot["raw"]), (0.0, 0.0, 0.0))
        self.assertEqual(tot["open_markets"], 0)

    def test_yes_no_settlement_unchanged(self):
        tot, _ = self.raw([_fill("Y", "yes", "buy", 10, 30), _fill("N", "yes", "sell", 10, 40)],
                          _market("Y", "yes", "determined", last="0.9900"),
                          _market("N", "no", "finalized", last="0.0100"))
        self.assertAlmostEqual(tot["settle"], 7.00 + 4.00, places=6)
        self.assertEqual((tot["unrealized"], tot["open_markets"]), (0.0, 0))

    def test_a_scalar_not_yet_finalized_is_still_marked_open(self):
        tot, _ = self.raw([_fill(LADDER, "yes", "buy", 10, 16)],
                          _market(LADDER, "scalar", "determined", "0.1200"))
        self.assertEqual(tot["settle"], 0.0)
        self.assertAlmostEqual(tot["unrealized"], 6.60, places=6)   # 10 x (82 - 16)
        self.assertEqual(tot["open_markets"], 1)


class RainDirSectionTests(_DigestTest):
    """The rain-directional block's open MTM took (bid + ask) / 2 whenever
    both read nonzero, and Kalshi reads an empty ask as $1.00 (2026-10-01).
    It now marks with the bot's bulk_mark_cents, like current_mids."""

    def test_a_one_sided_book_marks_at_the_last_trade(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = self.sd.STATUS_PATH
        self.sd.STATUS_PATH = os.path.join(tmp.name, "status_incentive_mm.json")
        self.addCleanup(setattr, self.sd, "STATUS_PATH", old)
        with open(os.path.join(tmp.name, "rain_directional_ledger.csv"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("ts,ticker,take_side,contracts,price_cents,fair_cents,"
                    "ext_bid,ext_ask,edge_cents,order_id\n"
                    "2026-09-30T16:00:00Z,KXRAINNYC-26OCT01,yes,3,40,60,38,40,20,a\n"
                    "2026-09-30T16:00:00Z,KXRAINCHI-26OCT01,no,3,60,30,30,34,10,b\n"
                    "2026-09-29T16:00:00Z,KXRAINDC-26SEP30,yes,3,40,60,38,40,20,c\n")
        client = _Client(_market("KXRAINNYC-26OCT01", bid="0.0500", ask="1.0000", last="0.9700"),
                         _market("KXRAINCHI-26OCT01", bid="0.3000", ask="0.3400", last="0.3100"),
                         _market("KXRAINDC-26SEP30", "yes", "finalized"))
        text, _html = self.sd.rain_dir_section(client)
        # 3 x (97 - 40) + 3 x ((100 - 32) - 60) = 1.71 + 0.24; the stray 5c
        # bid under no offer read 52.5 before, +0.375
        self.assertIn("settled P&L +1.80 | open 2 MTM +1.95 | total +3.75", text[0])


class DailySeriesTests(_DigestTest):
    """daily_series feeds the DAILY P&L table and upserts daily_pnl.json."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.old_path = self.sd.DAILY_PNL_PATH
        self.sd.DAILY_PNL_PATH = os.path.join(tmp.name, "daily_pnl.json")
        self.addCleanup(setattr, self.sd, "DAILY_PNL_PATH", self.old_path)

    def test_the_scalar_settlement_lands_in_the_day_and_the_store(self):
        day = datetime.now(timezone.utc).astimezone(self.sd.ET).date() - timedelta(days=2)
        ts = self.sd.ET.localize(datetime(day.year, day.month, day.day, 12)).timestamp()
        fills = [_fill(LADDER, "no", "buy", 90, 16, ts=ts)]
        client = _Client(_market(LADDER, "scalar", "finalized", "0.1200"))
        mids, results = self.sd.current_mids(client, [LADDER])
        series = self.sd.daily_series(client, fills, mids, results, {})
        self.assertEqual([(d, round(raw, 2), nf) for d, raw, _rew, _ct, nf in series],
                         [(day, 3.60, 1)])
        with open(self.sd.DAILY_PNL_PATH, encoding="utf-8") as f:
            rec = json.load(f)[day.isoformat()]
        self.assertEqual((rec["raw"], rec["settle"], rec["mtm"]), (3.6, 3.6, 0.0))


if __name__ == "__main__":
    unittest.main()
