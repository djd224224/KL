#!/usr/bin/env python3
"""Unit tests for ca_settled_dry.py -- the Carbon Arc settled-strike dry run
(Jack 2026-11-01: "Yes, both").

Run: python -m unittest test_ca_settled_dry

Worth testing: which month a ticker measures, the settled margin (category
floor, unobserved share, November's holiday rules), the read a strike is
judged by (the fair file, else the vintage log's final read after a rollover),
the book parser (the public API's dollar strings), what the mode would rest
and when the market disagrees, a pass end to end, and the tape fills the
report credits.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ca_settled_dry as csd                                        # noqa: E402
import incentive_mm as imm                                          # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_ca_settled_test_")
    imm.ALERT_RECIPIENTS = []


def _read(mtd, cat="Advertising", w=1.0, sigma_m=4.0, month="2026-10", thru="2026-10-31",
          fetched="2026-11-01T12:00:00+00:00"):
    return {"mtd": mtd, "category": cat, "w": w, "sigma_m": sigma_m, "month": month,
            "data_through": thru, "fetched_at": fetched}


def _prog(start="2026-10-29T00:00:00Z", end="2026-11-05T00:00:00Z", reward=700000):
    # 700000 centi-cents over 7 days = $10/day
    return {"start_date": start, "end_date": end, "period_reward": reward,
            "target_size_fp": "1000.00", "discount_factor_bps": 5000}


VER = {s: {"carbon_arc": True, "ts": 1.0, "title": ""}
       for s in ("KXFAKEADS", "KXFAKECC", "KXAMZNCC")}


class TestRules(unittest.TestCase):

    def test_measurement_month_is_the_month_before_the_print_code(self):
        self.assertEqual(csd.measurement_month("KXFAKECC-26NOV07-T110"), "2026-10")
        self.assertEqual(csd.measurement_month("KXFAKECC-26DEC07-T110"), "2026-11")
        self.assertEqual(csd.strike_of("KXFAKECC-26NOV07-T110"), 110.0)
        self.assertEqual(csd.strike_of("KXFAKECC-26NOV07-T-4.5"), -4.5)
        self.assertIsNone(csd.strike_of("KXFAKECC-26NOV07"))

    def test_margin_floor_unobserved_share_and_holiday(self):
        # a complete ad-spend month: the floor alone
        self.assertEqual(csd.margin(_read(100.0), "KXFAKEADS", "2026-10", True), 3.0)
        # card spend, 28 of 31 days seen: 5 + 3 x (3/31) x 4
        m = csd.margin(_read(100.0, cat="Credit Card", w=28 / 31), "KXFAKECC", "2026-10", True)
        self.assertAlmostEqual(m, 5.0 + 3.0 * (3 / 31) * 4.0)
        # big-scale series: 3% of the read
        self.assertAlmostEqual(csd.margin(_read(1900.0, cat="App"), "KXFAKEAPP", "2026-10", True), 57.0)
        # November: card and ad spend +3 (Cyber Monday moves into the month) ...
        self.assertEqual(csd.margin(_read(100.0, month="2026-11"), "KXFAKEADS", "2026-11", False), 6.0)
        # ... and the online-heavy card names sit out once the month is over
        nov_cc = _read(100.0, cat="Credit Card", month="2026-11")
        self.assertIsNone(csd.margin(nov_cc, "KXAMZNCC", "2026-11", True))
        self.assertEqual(csd.margin(nov_cc, "KXAMZNCC", "2026-11", False), 8.0)
        self.assertIsNone(csd.margin(_read(100.0, cat="Weather"), "KXFAKEX", "2026-10", True))

    def test_settled_side(self):
        self.assertEqual(csd.settled_side(100.0, 96.0, 3.0), "yes")
        self.assertEqual(csd.settled_side(100.0, 103.0, 3.0), "no")
        self.assertIsNone(csd.settled_side(100.0, 98.0, 3.0))

    def test_reads_prefer_the_newest_and_keep_a_rolled_month(self):
        d = tempfile.mkdtemp(prefix="imm_ca_settled_reads_")
        vint, fair = os.path.join(d, "v.jsonl"), os.path.join(d, "f.json")
        with open(vint, "w") as f:
            for r in (_read(90.0, thru="2026-10-29"), _read(91.0, thru="2026-10-30"),
                      _read(5.0, month="2026-11", thru="2026-11-01")):
                f.write(json.dumps(dict(r, series="KXFAKEADS")) + "\n")
        # the fair file has rolled to November: October survives in the vintages
        with open(fair, "w") as f:
            json.dump({"entries": {"KXFAKEADS": _read(6.0, month="2026-11", thru="2026-11-02")}}, f)
        reads = csd.load_reads(fair, vint)
        self.assertEqual(reads[("KXFAKEADS", "2026-10")]["mtd"], 91.0)
        self.assertEqual(reads[("KXFAKEADS", "2026-11")]["mtd"], 6.0)

    def test_parse_book_reads_dollar_strings(self):
        yes, no = csd.parse_book({"orderbook_fp": {"yes_dollars": [["0.0100", "1099.64"], ["0.0200", "5761.00"]],
                                                   "no_dollars": [["0.8600", "9161.00"]]}})
        self.assertEqual(yes, [[1, 1099.64], [2, 5761.0]])
        self.assertEqual(no, [[86, 9161.0]])
        self.assertEqual(csd.parse_book({"orderbook": {"yes": [[3, 10]], "no": None}}), ([[3, 10.0]], []))


class TestProposal(unittest.TestCase):

    def test_joins_the_settled_side_and_rests_the_other_at_3c_or_1c(self):
        yes = [[1, 2000.0], [2, 500.0]]              # bids for YES (the dead side here)
        no = [[86, 9000.0], [87, 14.0]]              # bids for NO (settled)
        p = csd.proposal(yes, no, "no", 1000.0, 0.5)
        self.assertEqual(p["status"], "ok")
        self.assertEqual((p["right_px"], p["wrong_px"]), (87, 2))
        # the other side's book is thin at the reference: 1,000 lots there earn most
        self.assertGreater(p["est"]["20/1000"], 5 * p["est"]["20/20"])
        self.assertGreater(p["est"]["150/1000"], p["est"]["20/1000"])
        # a dead side bid above 3c: rest at 1c instead
        p = csd.proposal([[5, 100.0]], no, "no", 1000.0, 0.5)
        self.assertEqual(p["wrong_px"], 1)

    def test_market_disagrees_when_real_money_bids_the_dead_side(self):
        # Amazon T106 on 11/01: read 98.1, the market bid YES ~40c
        p = csd.proposal([[40, 300.0]], [[55, 300.0]], "no", 1000.0, 0.5)
        self.assertEqual(p["status"], "disagree")
        # 1c placeholders on both sides say nothing: not a disagreement
        p = csd.proposal([[1, 22000.0]], [[1, 22000.0]], "no", 1000.0, 0.5)
        self.assertEqual(p["status"], "ok")
        self.assertEqual(p["mid"], 50.0)
        p = csd.proposal([[1, 100.0]], [], "no", 1000.0, 0.5)
        self.assertEqual(p["status"], "empty")


class TestPass(unittest.TestCase):

    def test_a_pass_logs_settled_strikes_only(self):
        now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        reads = {("KXFAKEADS", "2026-10"): _read(100.0),
                 ("KXFAKECC", "2026-10"): _read(0.0, cat="Credit Card")}
        progs = {"KXFAKEADS-26NOV06-T110": _prog(),      # settled NO (10 over)
                 "KXFAKEADS-26NOV06-T101": _prog(),      # near the money
                 "KXFAKEADS-26NOV06-T90": _prog(),       # settled YES, but the market disagrees
                 "KXFAKECC-26NOV07-T90": _prog(),        # a zero read: vendor dropout
                 "KXFAKEADS-26NOV06-T80": _prog(start="2026-11-02T00:00:00Z")}
        books = {"KXFAKEADS-26NOV06-T110": ([[1, 2000.0]], [[97, 500.0]]),
                 "KXFAKEADS-26NOV06-T90": ([[60, 100.0]], [[35, 100.0]])}
        path = os.path.join(imm.STATUS_DIR, "dry_pass.jsonl")
        with mock.patch.dict(imm.FAMILY_VERDICTS, VER):
            res = csd.run_pass(progs, now=now, reads=reads, path=path,
                               books=lambda ts: {t: books[t] for t in ts if t in books})
        self.assertEqual(res["strikes"], 1)
        self.assertEqual(res["skip"], {"near the money": 1, "disagree": 1, "read is zero": 1,
                                       "program not running": 1})
        self.assertAlmostEqual(res["pool"], 10.0)
        with open(path) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([(r["t"], r["side"], r["rpx"], r["wpx"]) for r in rows],
                         [("KXFAKEADS-26NOV06-T110", "no", 97, 1)])
        # dry: nothing written
        with mock.patch.dict(imm.FAMILY_VERDICTS, VER):
            csd.run_pass(progs, now=now, reads=reads, path=path, dry=True,
                         books=lambda ts: {t: books[t] for t in ts if t in books})
        with open(path) as f:
            self.assertEqual(len(f.readlines()), 1)


class TestReport(unittest.TestCase):

    def test_tape_fills_and_their_value(self):
        t0 = 1_000_000
        rows = [{"ts": t0, "side": "no", "rpx": 97, "wpx": 2},
                {"ts": t0 + 1800, "side": "no", "rpx": 96, "wpx": 2}]
        trades = [
            {"ts": t0 + 10, "taker": "yes", "yes": 3, "no": 97, "n": 50.0},     # sells NO at 97: fills our NO bid
            {"ts": t0 + 20, "taker": "yes", "yes": 2, "no": 98, "n": 50.0},     # 98 > our 97: no fill
            {"ts": t0 + 30, "taker": "no", "yes": 2, "no": 98, "n": 30.0},      # sells YES at 2: our 2c YES bid
            {"ts": t0 + 1900, "taker": "no", "yes": 1, "no": 99, "n": 2000.0},  # through 2c: capped at 1,000
            {"ts": t0 + 4000, "taker": "no", "yes": 1, "no": 99, "n": 500.0},   # after the last row stands
        ]
        f = csd.fills_for(rows, trades, "20/1000", won=True)
        self.assertEqual((f["right"], f["wrong"]), (20.0, 1030.0))
        # NO won: +3c on 20 settled-side contracts, -2c on 1,030 dead-side ones
        self.assertAlmostEqual(f["pnl"], (20 * 3 - 1030 * 2) / 100.0)
        f = csd.fills_for(rows, trades, "20/1000", won=False)
        self.assertAlmostEqual(f["pnl"], (-20 * 97 + 1030 * 98) / 100.0)

    def test_report_reads_the_logs_and_flags_a_read_that_lost(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        path = os.path.join(imm.STATUS_DIR, "ca_settled_dry_2026-11-02.jsonl")
        base = {"cat": "Advertising", "month": "2026-10", "thru": "2026-10-31", "need": 3.0,
                "mid": 98.0, "usd": 48.0, "est": {"20/20": 0.01, "20/1000": 0.2, "150/1000": 0.25}}
        with open(path, "w") as f:
            for i in range(2):
                f.write(json.dumps(dict(base, ts=int(now.timestamp()) - 7200 + 1800 * i,
                                        t="KXFAKEADS-26NOV06-T110", mu=100.0, k=110.0,
                                        side="no", rpx=97, wpx=1)) + "\n")
            f.write(json.dumps(dict(base, ts=int(now.timestamp()) - 3600, t="KXFAKEADS-26NOV06-T90",
                                    mu=100.0, k=90.0, side="yes", rpx=98, wpx=1)) + "\n")
        out = csd.report(1.0, now=now, trades_of=lambda t, a, b: [],
                         result_of=lambda t: "no" if t.endswith("T90") else None)
        self.assertIn("Advertising (2 strikes, 1 settled)", out)
        self.assertIn("settled AGAINST the read (1)", out)
        self.assertIn("KXFAKEADS-26NOV06-T90 read 100.0 vs strike 90.0, settled no", out)
        # reward: 0.2 x $48/day for 3 half-hours = $0.60 at 20/1000
        self.assertIn("20/1000: $    0.60", out)


if __name__ == "__main__":
    unittest.main()
