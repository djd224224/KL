"""Tests for send_mlb_nights_report.py -- the numbers, on a synthetic
STATUS_DIR (fills, cycle log, guard sink, bot state), marks handed in."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import send_mlb_nights_report as mr

HEAD = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,est_frac,qual_sides,acct_pos,"
        "own_pos,pool_per_day,quoted,own_bid_ct,own_ask_ct,own_bid_top,own_ask_top,own_pad_bid_ct,"
        "own_pad_ask_ct,want_bid_ct,want_ask_ct,want_pad_ct,hour_mult,rung_lo,room_buy,room_sell,"
        "vol24h,discount,is_scan,is_sticky,reduce_only,fast,run_id,config_hash")
A = "KXMLBNEXTTEAM-26GSPRINGER4-TOR"
B = "KXMLBNEXTTEAM-26MKING34-NYM"
OTHER = "KXGTEMP-27-T1.5"


def crow(ts, t, bid, ask, frac, pool, bct=60, act=60):
    cols = [ts, t, str(bid), str(ask), "1000", "1000", "1000", str(frac), "2", "0", "0",
            str(pool), "1", str(bct), str(act)] + ["0"] * 17 + ["run1", "cfg1"]
    return ",".join(cols)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class TestMlbNightsReport(unittest.TestCase):
    DAY = date(2026, 11, 27)                     # EST: the night is 05:00-12:00Z

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlbn_")
        for p in (mock.patch.object(mr, "STATUS_DIR", self.tmp),
                  mock.patch.object(mr, "OUT_DIR", os.path.join(self.tmp, "mlb_nights"))):
            p.start()
            self.addCleanup(p.stop)
        # cycles 30 s apart from 05:00:00Z; a 20-minute outage at 06:00 (capped
        # at 900 s); a cycle after the window (12:30Z) and one before (04:30Z)
        rows = [HEAD]
        stamps = ["2026-11-27T04:30:00Z", "2026-11-27T05:00:00Z", "2026-11-27T05:00:30Z",
                  "2026-11-27T05:01:00Z", "2026-11-27T05:21:00Z", "2026-11-27T12:30:00Z"]
        for s in stamps:
            rows.append(crow(s, OTHER, 40, 44, 0.5, 100.0))          # other series: ts only
            if s in ("2026-11-27T05:00:30Z", "2026-11-27T05:01:00Z", "2026-11-27T05:21:00Z"):
                rows.append(crow(s, A, 10, 14, 0.10, 400.0))
            if s == "2026-11-27T05:01:00Z":
                rows.append(crow(s, B, 40, 50, 0.05, 300.0))
        with open(os.path.join(self.tmp, "cycle_log_2026-11-27.csv"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        fills = [
            # bought 60 YES at 12c on A in the window (05:10Z)
            {"ts": utc(2026, 11, 27, 5, 10).timestamp(), "fill_id": "f1", "ticker": A,
             "series": "KXMLBNEXTTEAM", "side": "yes", "action": "buy", "count": 60.0,
             "yes_price_cents": 12.0, "is_taker": False, "our_book_side": "bid"},
            # the same fill twice (the WS feed and the poll)
            {"ts": utc(2026, 11, 27, 5, 10).timestamp(), "fill_id": "f1", "ticker": A,
             "series": "KXMLBNEXTTEAM", "side": "yes", "action": "buy", "count": 60.0,
             "yes_price_cents": 12.0, "is_taker": False, "our_book_side": "bid"},
            # sold 20 YES at 48c on B (06:30Z)
            {"ts": utc(2026, 11, 27, 6, 30).timestamp(), "fill_id": "f2", "ticker": B,
             "series": "KXMLBNEXTTEAM", "side": "no", "action": "buy", "count": 20.0,
             "yes_price_cents": 48.0, "is_taker": False, "our_book_side": "ask"},
            # after the window and another series: never in the night
            {"ts": utc(2026, 11, 27, 13, 0).timestamp(), "fill_id": "f3", "ticker": A,
             "series": "KXMLBNEXTTEAM", "side": "yes", "action": "buy", "count": 5.0,
             "yes_price_cents": 11.0, "is_taker": False, "our_book_side": "bid"},
            {"ts": utc(2026, 11, 27, 5, 30).timestamp(), "fill_id": "f4", "ticker": OTHER,
             "series": "KXGTEMP", "side": "yes", "action": "buy", "count": 9.0,
             "yes_price_cents": 40.0, "is_taker": False, "our_book_side": "bid"}]
        with open(os.path.join(self.tmp, "fills_2026-11-27.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(f) for f in fills) + "\n")
        guards = [{"ts": "2026-11-27T05:10:02+00:00", "kind": "enter", "ticker": A,
                   "guard": "astest_fill_pause"},
                  {"ts": "2026-11-27T05:10:02+00:00", "kind": "exit", "ticker": A,
                   "guard": "astest_fill_pause"},
                  {"ts": "2026-11-27T12:01:00+00:00", "kind": "enter", "ticker": A,
                   "guard": "astest_window"}]
        with open(os.path.join(self.tmp, "guard_skips_2026-11-27.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(g) for g in guards) + "\n")
        self.marks = {A: {"bid": 20.0, "ask": 30.0, "last": 25.0, "status": "active", "result": ""},
                      B: {"bid": 40.0, "ask": 50.0, "last": 44.0, "status": "active", "result": ""}}
        self.now = utc(2026, 11, 27, 12, 15)                         # 07:15 ET

    def test_the_night_is_00_to_07_et(self):
        self.assertEqual(mr.night_bounds(self.DAY), (utc(2026, 11, 27, 5, 0), utc(2026, 11, 27, 12, 0)))
        self.assertEqual(mr.night_bounds(self.DAY, ("00:00", "24:00")),
                         (utc(2026, 11, 27, 5, 0), utc(2026, 11, 28, 5, 0)))

    def test_rewards_are_the_reconcilers_accrual_for_the_window(self):
        cyc = mr.scan_cycles("KXMLBNEXTTEAM", *mr.night_bounds(self.DAY), today=date(2026, 11, 28))
        # A: 05:00:30 (30 s), 05:01:00 (30 s), 05:21:00 (20 min -> capped 900 s)
        exp_a = 0.10 * 400.0 * (30 + 30 + 900) / 86400.0
        exp_b = 0.05 * 300.0 * 30 / 86400.0
        self.assertAlmostEqual(cyc[A]["est"], exp_a, places=9)
        self.assertAlmostEqual(cyc[B]["est"], exp_b, places=9)
        self.assertEqual(cyc[A]["cycles"], 3)
        self.assertEqual((cyc[A]["first_mid"], cyc[A]["last_mid"]), (12.0, 12.0))
        self.assertNotIn(OTHER, cyc)
        # cached for a finished day: the same answer from the cache
        again = mr.scan_cycles("KXMLBNEXTTEAM", *mr.night_bounds(self.DAY), today=date(2026, 11, 28))
        self.assertAlmostEqual(again[A]["est"], exp_a, places=9)
        self.assertTrue(os.path.exists(os.path.join(mr.OUT_DIR, "cycle_cache_KXMLBNEXTTEAM.json")))

    def test_trading_is_every_fill_marked_to_the_last_trade(self):
        subject, page, text, ctx = mr.build(self.DAY, self.now, marks=self.marks)
        n = ctx["night"]
        self.assertEqual(len(n["fills"]), 2)                         # f1 once, f2; not f3/f4
        exp = 60 * (25 - 12) / 100.0 + (-1) * 20 * (44 - 48) / 100.0   # +7.80 +0.80
        self.assertAlmostEqual(n["pnl"], exp, places=9)
        self.assertEqual((n["bought"], n["sold"]), (60.0, 20.0))
        self.assertAlmostEqual(n["net"], n["rew"] + n["pnl"], places=9)
        # since start also holds the 13:00Z fill (to the run's now: it is later)
        later = mr.build(self.DAY, utc(2026, 11, 27, 13, 30), marks=self.marks)[3]
        self.assertEqual(len(later["since"]["fills"]), 3)
        pos = {p["ticker"]: p for p in later["since"]["positions"]}
        self.assertEqual(pos[A]["net"], 65.0)
        self.assertAlmostEqual(pos[A]["avg"], (60 * 12 + 5 * 11) / 65.0, places=9)
        self.assertEqual(pos[B]["net"], -20.0)
        # a settled market marks at 100/0
        settled = dict(self.marks, **{A: {"bid": None, "ask": None, "last": 99.0, "status": "finalized",
                                          "result": "yes"}})
        s = mr.build(self.DAY, self.now, marks=settled)[3]["night"]
        self.assertAlmostEqual(s["pnl"], 60 * (100 - 12) / 100.0 + 0.80, places=9)
        self.assertIn("MLB nights pilot, Fri Nov 27:", subject)
        self.assertIn("2 fills", subject)
        self.assertIn("GSPRINGER to TOR", page)
        stops = page.split("Stops this night")[1]
        self.assertIn("fill pause (event aside 15 min)", stops)       # the stop that fired
        self.assertNotIn("astest_window", stops)                     # the window itself is no stop

    def test_status_stop_file_halt_and_the_end(self):
        self.assertEqual(mr.build(self.DAY, self.now, marks=self.marks)[3]["status"], "ON")
        with open(os.path.join(self.tmp, "imm_state.json"), "w", encoding="utf-8") as fh:
            json.dump({"twk_halt_day": {"ASTEST:MLB": "2026-11-27"}}, fh)
        self.assertIn("HALTED 2026-11-27", mr.build(self.DAY, self.now, marks=self.marks)[3]["status"])
        open(os.path.join(self.tmp, mr.STOP_FILE), "w").close()
        self.assertEqual(mr.build(self.DAY, self.now, marks=self.marks)[3]["status"], "STOPPED (stop file)")
        os.remove(os.path.join(self.tmp, mr.STOP_FILE))
        os.remove(os.path.join(self.tmp, "imm_state.json"))
        end = mr.build(date(2026, 12, 3), utc(2026, 12, 3, 12, 15), marks=self.marks)[3]
        self.assertEqual(end["status"], "ENDED")
        self.assertEqual((end["nights_done"], end["nights_left"]), (6, 0))
        first = mr.build(self.DAY, self.now, marks=self.marks)[3]
        self.assertEqual((first["nights_done"], first["nights_left"]), (1, 5))

    def test_an_empty_night_and_one_email_a_day(self):
        subject, page, text, ctx = mr.build(date(2026, 11, 28), utc(2026, 11, 28, 12, 15), marks={})
        self.assertEqual(ctx["night"]["fills"], [])
        self.assertIn("No fills this night.", page)
        self.assertIn("0 fills", subject)
        sent = []
        with mock.patch.object(mr, "send", lambda s, p, t: sent.append(s) or True), \
                mock.patch.object(mr, "fetch_marks", lambda ts: {}):
            self.assertEqual(mr.main(["--day", "2026-11-27"]), 0)
            self.assertEqual(mr.main(["--day", "2026-11-27"]), 0)       # the marker: no resend
            self.assertEqual(len(sent), 1)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--force"]), 0)
            self.assertEqual(len(sent), 2)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--dry"]), 0)
            self.assertEqual(len(sent), 2)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--test"]), 0)
            self.assertTrue(sent[-1].startswith("[TEST] "))
        with mock.patch.object(mr, "send", lambda s, p, t: False), \
                mock.patch.object(mr, "fetch_marks", lambda ts: {}):
            self.assertEqual(mr.main(["--day", "2026-11-29"]), 1)       # a failed send: rc 1
            self.assertFalse(os.path.exists(os.path.join(mr.OUT_DIR, "sent_2026-11-29.marker")))


if __name__ == "__main__":
    unittest.main()
