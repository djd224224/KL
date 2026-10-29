"""high_temp_digest_pnl: the high-temp digest's P&L is this bot's own fills,
each valued by how its market settled, with the IMM's fills on the same
account kept apart (2026-10-28).

No BigQuery and no network: build() takes plain rows.

    python -m unittest test_high_temp_digest_pnl
"""
import datetime as dt
import json
import os
import tempfile
import unittest

import high_temp_digest_pnl as P

D = dt.date(2026, 10, 27)


def fill(fid, ticker, side, count, yes_px, coid="", action="buy", fee=0.0):
    return {"fill_id": fid, "client_order_id": coid, "market_ticker": ticker, "side": side,
            "action": action, "filled_count": count, "raw_yes_price_dollars": yes_px,
            "raw_no_price_dollars": round(1 - yes_px, 4), "fee_cost_dollars": fee, "fill_ts": fid}


def settlement(ticker, result, revenue, yes_cost, no_cost):
    return {"market_ticker": ticker, "result": result, "revenue_dollars": revenue,
            "yes_total_cost_dollars": yes_cost, "no_total_cost_dollars": no_cost,
            "fee_cost_dollars": 0}


class TestFillValue(unittest.TestCase):

    def test_a_fill_is_valued_by_how_its_market_settled(self):
        self.assertEqual(P.fill_value("no", "buy", 275, 0.16, 0.84, 0, True), (-231.0, 231.0))
        pnl, cost = P.fill_value("yes", "buy", 100, 0.35, 0.65, 0, True)
        self.assertAlmostEqual(pnl, 65.0)
        self.assertAlmostEqual(cost, 35.0)
        pnl, cost = P.fill_value("no", "buy", 100, 0.20, 0.80, 0.5, False)
        self.assertAlmostEqual(pnl, 19.5)

    def test_a_sell_counts_the_other_way_round(self):
        pnl, cost = P.fill_value("yes", "sell", 100, 0.40, 0.60, 0, False)
        self.assertAlmostEqual(pnl, 40.0)
        self.assertEqual(cost, 0.0)


class TestResults(unittest.TestCase):

    def test_a_band_is_yes_on_its_two_degrees(self):
        self.assertTrue(P.estimate_yes("B", 61.5, "", 61))
        self.assertTrue(P.estimate_yes("B", 61.5, "", 62))
        self.assertFalse(P.estimate_yes("B", 59.5, "", 61))

    def test_a_tail_needs_kalshis_strike_type(self):
        self.assertTrue(P.estimate_yes("T", 80, "less", 75))
        self.assertFalse(P.estimate_yes("T", 77, "greater", 75))
        self.assertTrue(P.estimate_yes("T", 77, "greater", 78))
        self.assertIsNone(P.estimate_yes("T", 80, "", 75))
        self.assertIsNone(P.estimate_yes("B", 61.5, "", None))

    def test_labels_and_codes(self):
        self.assertEqual(P.date_code(D), "26OCT27")
        self.assertEqual(P.date_code(dt.date(2026, 1, 5)), "26JAN05")
        self.assertEqual(P.parse_ticker("KXHIGHTATL-26OCT27-B72.5"), ("TATL", "B", 72.5))
        self.assertEqual(P.band_label("B", 82.5), "82-83F")
        self.assertEqual(P.band_label("T", 80.0, "less"), "<80F")
        self.assertEqual(P.band_label("T", 77.0, "greater"), ">77F")
        self.assertEqual(P.band_label("T", 80.0), "T80")

    def test_money(self):
        self.assertEqual(P.money(-1420.27, sign=True), "-$1,420.27")
        self.assertEqual(P.money(20.04, sign=True), "+$20.04")
        self.assertEqual(P.money(4583.73), "$4,583.73")
        self.assertEqual(P.money(-0.001, sign=True), "+$0.00")


class TestOwner(unittest.TestCase):

    def test_whose_fill(self):
        imm = {"f-imm"}
        self.assertEqual(P.owner_of({"fill_id": "f-bot", "client_order_id": "8d25ef01"}, imm), "bot")
        self.assertEqual(P.owner_of({"fill_id": "f-imm", "client_order_id": ""}, imm), "imm")
        self.assertEqual(P.owner_of({"fill_id": "f-x", "client_order_id": None}, imm), "unattributed")


class TestBuild(unittest.TestCase):
    """A 10/27-shaped day: the bot on two markets, the IMM on two (one of them
    two-sided), one fill nobody owns."""

    PHIL = "KXHIGHPHIL-26OCT27-B68.5"
    DAL = "KXHIGHTDAL-26OCT27-B84.5"
    AUS = "KXHIGHAUS-26OCT27-B82.5"
    SFO = "KXHIGHTSFO-26OCT27-B66.5"
    MIN = "KXHIGHTMIN-26OCT27-B61.5"

    def setUp(self):
        self.fills = [
            fill("b1", self.DAL, "no", 141, 0.494, coid="c1"),      # NO wins: +141 x 0.494
            fill("b2", self.AUS, "no", 70, 0.433, coid="c2"),       # NO loses: -70 x 0.567
            fill("i1", self.PHIL, "yes", 300, 0.20),                # the IMM, both sides
            fill("i2", self.PHIL, "no", 500, 0.20),
            fill("i3", self.SFO, "no", 275, 0.16),
            fill("u1", self.SFO, "no", 10, 0.16),
            fill("i1", self.PHIL, "yes", 300, 0.20),                # a duplicate row
        ]
        self.settlements = [
            settlement(self.DAL, "NO", 141, 0, 71.35),
            settlement(self.AUS, "YES", 0, 0, 39.69),
            # Kalshi netted 300 pairs ($300 paid then): 200 NO left, revenue 200.
            settlement(self.PHIL, "NO", 200, 60, 400),
            settlement(self.SFO, "YES", 0, 0, 239.4),
        ]
        self.actuals = {"TDAL": ("Dallas", 83), "AUS": ("Austin", 83), "PHIL": ("Philadelphia", 66),
                        "TSFO": ("San Francisco", 66), "TMIN": ("Minneapolis", 61)}

    def build(self, **kw):
        args = dict(fills=self.fills, settlements=self.settlements, actuals=self.actuals,
                    meta={}, imm_fill_ids={"i1", "i2", "i3"})
        args.update(kw)
        return P.build(D, args["fills"], args["settlements"], args["actuals"], args["meta"],
                       args["imm_fill_ids"])

    def test_the_headline_is_the_bot_alone(self):
        s = self.build()
        bot = s["totals"]["bot"]
        self.assertAlmostEqual(bot["pnl"], round(141 * 0.494 - 70 * 0.567, 2))
        self.assertAlmostEqual(bot["cost"], round(141 * 0.506 + 70 * 0.567, 2))
        self.assertEqual((bot["wins"], bot["losses"], bot["fills"], bot["markets"]), (1, 1, 2, 2))
        self.assertEqual(s["status"], "settled")
        self.assertEqual(s["summary_line"], f"+$29.96 on $111.04, 1W/1L, settled")

    def test_the_imm_is_one_total_and_its_netted_pairs_count(self):
        s = self.build()
        imm = s["totals"]["imm"]
        # PHIL: 300 YES lose 0.20, 500 NO win 0.20 -> +40 (the record says -260)
        self.assertAlmostEqual(imm["pnl"], round(-60 + 100 - 275 * 0.84, 2))
        self.assertEqual((imm["fills"], imm["markets"]), (3, 2))
        self.assertEqual(s["totals"]["unattributed"]["fills"], 1)
        c = s["check"]
        self.assertEqual(c["netted_pairs"], 300)
        self.assertAlmostEqual(c["fills_sum"] - c["settlement_records"], 300, places=2)
        self.assertTrue(c["ok"])

    def test_missing_fills_fail_the_account_check(self):
        s = self.build(fills=[f for f in self.fills if f["fill_id"] != "b2"])
        self.assertFalse(s["check"]["ok"])
        self.assertIn("WARNING", P.render(s))

    def test_an_unsettled_market_is_estimated_from_the_actual(self):
        fills = self.fills + [fill("b3", self.MIN, "no", 125, 0.07, coid="c3")]
        s = self.build(fills=fills)
        row = next(m for m in s["markets"] if m["ticker"] == self.MIN)
        self.assertEqual((row["result"], row["status"]), ("YES", "est."))
        self.assertAlmostEqual(row["pnl"], -125 * 0.93)
        self.assertEqual(s["status"], "partly settled")
        self.assertTrue(s["summary_line"].endswith("1W/2L, partly settled"))

    def test_kalshis_final_result_counts_as_settled(self):
        fills = self.fills + [fill("b3", self.MIN, "no", 125, 0.07, coid="c3")]
        s = self.build(fills=fills, meta={self.MIN: {"strike_type": "between", "result": "no"}})
        row = next(m for m in s["markets"] if m["ticker"] == self.MIN)
        self.assertEqual((row["result"], row["status"]), ("NO", "settled"))
        self.assertEqual(s["status"], "settled")

    def test_no_actual_leaves_it_pending_and_out_of_the_pnl(self):
        fills = self.fills + [fill("b3", "KXHIGHTLV-26OCT27-B79.5", "no", 50, 0.3, coid="c3")]
        s = self.build(fills=fills)
        self.assertEqual(s["totals"]["bot"]["pending"], 1)
        self.assertEqual(s["totals"]["bot"]["wins"] + s["totals"]["bot"]["losses"], 2)
        self.assertIn("pending, not in the P&L", P.render(s))

    def test_no_bot_fills(self):
        s = self.build(fills=[f for f in self.fills if not f["client_order_id"]])
        self.assertEqual(s["summary_line"], "+$0.00 on $0.00, 0W/0L, no fills")

    def test_the_render_carries_the_summary_and_the_imm_line(self):
        out = P.render(self.build())
        self.assertIn("DIGEST_SUMMARY: +$29.96 on $111.04, 1W/1L, settled", out)
        self.assertIn("IMM market making on the same account (counted in IMM's own P&L, not here): ", out)
        self.assertIn("Unattributed by market:", out)


class TestImmLog(unittest.TestCase):

    def test_reads_the_imms_kxhigh_fills_on_ds_markets(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [
                {"fill_id": "a", "ticker": "KXHIGHTSFO-26OCT27-B66.5"},
                {"fill_id": "b", "ticker": "KXHIGHTSFO-26OCT28-B66.5"},   # another day's market
                {"fill_id": "c", "ticker": "KXLOWTAUS-26OCT27-B60.5"},    # not a high
            ]
            with open(os.path.join(d, "fills_2026-10-26.jsonl"), "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
                f.write("{not json KXHIGH -26OCT27-\n")
            self.assertEqual(P.load_imm_fill_ids(D, d), ({"a"}, True))
            self.assertEqual(P.load_imm_fill_ids(D, os.path.join(d, "none")), (set(), False))


if __name__ == "__main__":
    unittest.main()
