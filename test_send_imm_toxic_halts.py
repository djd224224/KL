#!/usr/bin/env python3
"""Unit tests for send_imm_toxic_halts.py -- the daily "what did the
toxic-flow halt stand down yesterday" email (Jack 2026-09-29).

Run: python -m unittest test_send_imm_toxic_halts

Worth testing: the ET-day window (a record at 23:30 ET belongs to that day
even though its UTC sink file is the next day's), the pick-offs attached to
each halt, the price-during-halt read off the marks sink, the forgone
reward, and that a day with nothing still produces an email. The PROFIT
block (Jack 2026-10-19: "in daily toxic halt emil tell me profit"): the
quotes each halt pulled or put back, the public trades through them, a
settled market's value, failed reads, and the 7-day store. No test touches
the network: Kalshi's public reads are canned (FakeKalshi).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_imm_toxic_halts as th                                   # noqa: E402
import incentive_mm as imm                                          # noqa: E402


class FakeKalshi:
    """Kalshi's public GETs, canned: markets and trades by ticker. A ticker
    in `fail` raises, like a run of 429s."""

    def __init__(self, markets=None, trades=None, fail=()):
        self.markets, self.trades, self.fail = markets or {}, trades or {}, set(fail)
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/markets/trades":
            t = params["ticker"]
            if t in self.fail:
                raise RuntimeError("429s on /markets/trades")
            rows = [r for r in self.trades.get(t, [])
                    if params["min_ts"] <= _epoch(r["created_time"]) <= params["max_ts"]]
            return {"trades": rows, "cursor": ""}
        t = path.rsplit("/", 1)[-1]
        if t in self.fail:
            raise RuntimeError(f"429s on {path}")
        return {"market": self.markets.get(t, {})}


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP,
    touch the network or lower the test process's priority."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_toxic_email_test_")
    imm.ALERT_RECIPIENTS = []
    th._get = FakeKalshi()
    th.low_priority = lambda: None


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _trade(ts, yes_c, n, taker):
    return {"created_time": _iso(ts).replace("+00:00", "Z"),
            "yes_price_dollars": f"{yes_c / 100:.4f}", "count_fp": f"{n:.2f}",
            "taker_side": taker}


def _mkt(bid, ask):
    return {"status": "active", "result": "", "settlement_value_dollars": None,
            "yes_bid_dollars": f"{bid / 100:.4f}", "yes_ask_dollars": f"{ask / 100:.4f}"}


def _write(name, rows):
    with open(os.path.join(imm.STATUS_DIR, name), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _clear():
    for f in os.listdir(imm.STATUS_DIR):
        os.remove(os.path.join(imm.STATUS_DIR, f))


def _fixture():
    """A side halt and an event halt on 9/28 (EDT: 04:00Z -> 04:00Z)."""
    _clear()
    mk = "KXGAS-26SEP29-4.48"
    ev = "KXAAAGASD-26SEP29"
    a, b = f"{ev}-4.4750", f"{ev}-4.4800"
    t0 = _ts(2026, 9, 28, 18, 0)                 # 14:00 ET
    _write("toxic_halts_2026-09-28.jsonl", [
        # side halt on mk: two bid pick-offs, then the halt
        {"kind": "pickoff", "ts": t0, "ticker": mk, "event": "KXGAS-26SEP29",
         "side": "bid", "fill_px": 50.0, "fill_ts": t0 - 300, "mark": 44.0},
        {"kind": "pickoff", "ts": t0 + 600, "ticker": mk,
         "event": "KXGAS-26SEP29", "side": "bid", "fill_px": 48.0,
         "fill_ts": t0 + 300, "mark": 42.0},
        {"kind": "side_halt", "ts": t0 + 600, "until": t0 + 2400,
         "ticker": mk, "event": "KXGAS-26SEP29", "side": "bid",
         "pickoffs": 2, "window_secs": 86400, "est_per_day": 4.8},
        # an event halt on the AAA state daily
        {"kind": "pickoff", "ts": t0 + 1000, "ticker": a, "event": ev,
         "side": "bid", "fill_px": 67.0, "fill_ts": t0 + 700, "mark": 60.0},
        {"kind": "pickoff", "ts": t0 + 1200, "ticker": b, "event": ev,
         "side": "bid", "fill_px": 50.0, "fill_ts": t0 + 900, "mark": 44.0},
        {"kind": "event_halt", "ts": t0 + 1200, "until": t0 + 3000,
         "event": ev, "markets_picked": [a, b], "pickoffs": 2,
         "window_secs": 3600, "markets_halted": 3, "est_per_day": 24.0},
        # the day BEFORE (22:00 ET 9/27 = 02:00Z 9/28): not this report
        {"kind": "pickoff", "ts": _ts(2026, 9, 28, 2, 0), "ticker": "OLD-X",
         "event": "OLD", "side": "ask", "fill_px": 30.0, "fill_ts": 0,
         "mark": 40.0}])
    # 23:30 ET 9/28 = 03:30Z 9/29 -> the next UTC file, same ET day
    _write("toxic_halts_2026-09-29.jsonl", [
        {"kind": "pickoff", "ts": _ts(2026, 9, 29, 3, 30), "ticker": "LATE-A",
         "event": "LATE", "side": "ask", "fill_px": 20.0, "fill_ts": 0,
         "mark": 27.0}])
    # the side halt's market kept falling 42 -> 39 while halted
    _write("marks_2026-09-28.jsonl", [
        {"ts": _iso(t0 + 2460), "ticker": mk, "mark_cents": 39.0}])
    return mk, ev, a, b, t0


def _profit_sinks(mk, ev, a, t0):
    """The fills behind the pick-offs and the quotes each halt took down or
    put back."""
    c = f"{ev}-4.4850"
    _write("fills_2026-09-28.jsonl", [
        {"ts": t0 - 300, "ticker": mk, "our_book_side": "bid",       # a taker fill:
         "yes_price_cents": 50.0, "count": 999.0, "is_taker": True},  # never a pick-off
        {"ts": t0 - 300, "ticker": mk, "our_book_side": "bid",
         "yes_price_cents": 50.0, "count": 30.0, "is_taker": False},
        {"ts": t0 + 300, "ticker": mk, "our_book_side": "bid",
         "yes_price_cents": 48.0, "count": 20.0, "is_taker": False},
        {"ts": t0 + 700, "ticker": a, "our_book_side": "bid",
         "yes_price_cents": 67.0, "count": 10.0, "is_taker": False}])
    _write("orders_2026-09-28.jsonl", [
        # the side halt pulls the 41c bid 10s after firing
        {"ts": _iso(t0 + 610), "cycle_ts": None, "kind": "cancel", "order_id": "o1",
         "ticker": mk, "event_ticker": "KXGAS-26SEP29", "book_side": "bid",
         "price_cents": 41, "count": 25.0, "is_pad": False, "reason": "requote_diff"},
        {"ts": _iso(t0 + 611), "cycle_ts": None, "kind": "cancel", "order_id": "o2",
         "ticker": mk, "event_ticker": "KXGAS-26SEP29", "book_side": "bid",
         "price_cents": 1, "count": 100.0, "is_pad": True},                  # a pad
        {"ts": _iso(t0 + 612), "cycle_ts": None, "kind": "cancel", "order_id": "o3",
         "ticker": mk, "event_ticker": "KXGAS-26SEP29", "book_side": "ask",
         "price_cents": 60, "count": 25.0, "is_pad": False},                # other side
        # the event had nothing up when it halted (another stand-down); the
        # first cycle after it ends puts a bid back on A and an ask on C
        {"ts": _iso(t0 + 3010), "cycle_ts": t0 + 3005, "kind": "place", "order_id": "o5",
         "ticker": a, "event_ticker": ev, "book_side": "bid", "price_cents": 60,
         "count": 40, "is_pad": False},
        {"ts": _iso(t0 + 3011), "cycle_ts": t0 + 3005, "kind": "place", "order_id": "o6",
         "ticker": c, "event_ticker": ev, "book_side": "ask", "price_cents": 55,
         "count": 50, "is_pad": False},
        {"ts": _iso(t0 + 3040), "cycle_ts": t0 + 3035, "kind": "place", "order_id": "o7",
         "ticker": a, "event_ticker": ev, "book_side": "bid", "price_cents": 62,
         "count": 40, "is_pad": False}])                                  # a later cycle
    kal = FakeKalshi(
        markets={mk: _mkt(29, 31), a: _mkt(69, 71)},
        trades={mk: [_trade(t0 + 900, 40, 10, "no"),        # sells through the 41 bid
                     _trade(t0 + 1200, 41, 30, "no"),       # 15 left of the 25
                     _trade(t0 + 1300, 45, 50, "yes")],     # a buy: not our side
                a: [_trade(t0 + 2000, 58, 100, "no")]})     # 40 of the put-back bid
    return c, kal


class TestToxicHaltsEmail(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 9, 28)
        self.mk, self.ev, self.a, self.b, self.t0 = _fixture()

    def test_report_covers_the_et_day_and_reads_each_halt(self):
        ctx = th.build_report(self.day)
        self.assertEqual(ctx["n_picks"], 5)          # not OLD-X; LATE-A counts
        self.assertEqual(len(ctx["side_rows"]), 1)
        s = ctx["side_rows"][0]
        self.assertEqual(s["picks"], [(50.0, 44.0), (48.0, 42.0)])
        self.assertEqual((s["m0"], s["m1"], s["move"]), (42.0, 39.0, 3.0))
        self.assertAlmostEqual(s["forgone"], 4.8 * 1800 / 86400)
        e = ctx["event_rows"][0]
        self.assertEqual(e["picked"], ["4.4750 (bid)", "4.4800 (bid)"])
        self.assertEqual(e["halted"], 3)
        self.assertAlmostEqual(e["forgone"], 24.0 * 1800 / 86400)
        self.assertAlmostEqual(ctx["forgone"], (4.8 + 24.0) * 1800 / 86400)
        text = th.render_text(ctx)
        self.assertIn("EVENT HALTS", text)
        self.assertIn("42->39 (kept moving against 3c)", text)
        self.assertIn("LATE-A", text)                # most picked-off table
        text.encode("ascii")                         # the task console is cp1252
        th.render_html(ctx)
        # no orders sink here: neither halt had quotes up, so neither cost a thing
        self.assertIn("Halts: $0.00 -- none had quotes up", text)
        self.assertEqual((s["quotes"], s["forgone_eff"], s["net"]), ([], 0.0, 0.0))
        self.assertIn("none up", text)
        self.assertIn("Pick-offs: n/a -- none of the 5 is in the fills sink.", text)
        self.assertEqual(th.subject_for(ctx), "IMM toxic halts Mon Sep 28: halts $0.00 net, "
                                              "pick-offs n/a | 1 event, 1 side (5 pick-offs)")
        old = th.build_report(self.day, with_profit=False)
        self.assertNotIn("profit", old)
        self.assertEqual(th.subject_for(old),
                         "IMM toxic halts Mon Sep 28: 1 event, 1 side (5 pick-offs)")
        self.assertNotIn("PROFIT", th.render_text(old))

    def test_exempt_mention_pick_offs_are_tagged(self):
        """Jack 2026-10-09: mention series never halt. Their pick-offs still
        show in the most-picked-off table, tagged, and the rule says why."""
        m = "KXTRUMPMENTION-26SEP28-ISRA"
        t0 = _ts(2026, 9, 28, 20, 0)
        _write("toxic_halts_2026-09-28.jsonl", [
            {"kind": "pickoff", "ts": t0, "ticker": m,
             "event": "KXTRUMPMENTION-26SEP28", "side": "ask", "fill_px": 30.0,
             "fill_ts": t0 - 300, "mark": 37.0, "exempt": True}])
        ctx = th.build_report(self.day)
        self.assertEqual(ctx["n_picks"], 6)
        text, html = th.render_text(ctx), th.render_html(ctx)
        self.assertIn("Exempt: series containing MENTION", text)
        lines = text.splitlines()
        self.assertTrue(next(l for l in lines if m in l).endswith("37 (exempt)"))
        self.assertNotIn("(exempt)", next(l for l in lines if "LATE-A" in l))
        self.assertIn(f"{m} (exempt)", html)
        text.encode("ascii")

    def test_a_quiet_day_still_reports(self):
        ctx = th.build_report(date(2026, 9, 20))
        self.assertFalse(ctx["seen_file"])
        text = th.render_text(ctx)
        self.assertIn("Nothing was halted.", text)
        self.assertIn("No toxic_halts sink file", text)
        self.assertIn("Halts: none, so no profit or cost.", text)
        self.assertEqual(th.subject_for(ctx), "IMM toxic halts Sun Sep 20: none (0 pick-off(s))")

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(imm.Alerter, "send_message") as send:
            self.assertEqual(th.main(["--dry", "--day", "2026-09-28"]), 0)
            send.assert_not_called()
        self.assertFalse(os.path.exists(th.store_path()))     # only the scheduled run keeps


class TestToxicHaltsProfit(unittest.TestCase):
    """Jack 2026-10-19: "in daily toxic halt emil tell me profit"."""

    def setUp(self):
        self.day = date(2026, 9, 28)
        self.mk, self.ev, self.a, self.b, self.t0 = _fixture()
        self.c, self.kal = _profit_sinks(self.mk, self.ev, self.a, self.t0)

    def test_each_halt_and_pick_off_in_dollars(self):
        with mock.patch.object(th, "_get", self.kal):
            ctx = th.build_report(self.day)
        s, e, pr = ctx["side_rows"][0], ctx["event_rows"][0], ctx["profit"]
        # the side halt pulled 25 @ 41; 25 sold through it during the halt;
        # the market is 30 now: they would have lost (30-41) x 25 = $2.75
        self.assertEqual(s["quotes"], [(self.mk, "bid", 41.0, 25.0, "pulled")])
        self.assertAlmostEqual(s["blocked"], -2.75)
        self.assertEqual(s["blocked_ct"], 25.0)
        self.assertAlmostEqual(s["forgone_eff"], 0.1)
        self.assertAlmostEqual(s["net"], 2.65)                # the halt paid
        # the event had nothing up as it fired: the first cycle after it put
        # a bid back on A and an ask on C (2 of its 3 markets); 40 sold
        # through the 60 bid, A is 70 now: the halt blocked +$4.00
        self.assertEqual(e["quotes"], [(self.a, "bid", 60.0, 40.0, "put back"),
                                       (self.c, "ask", 55.0, 50.0, "put back")])
        self.assertAlmostEqual(e["share"], 2 / 3)
        self.assertAlmostEqual(e["blocked"], 4.0)
        self.assertAlmostEqual(e["forgone_eff"], 0.5 * 2 / 3)
        self.assertAlmostEqual(e["net"], -4.0 - 0.5 * 2 / 3)
        self.assertAlmostEqual(pr["halts_net"], 2.65 - 4.0 - 1 / 3, places=3)
        self.assertAlmostEqual(pr["blocked"], 1.25)
        self.assertEqual(pr["blocked_ct"], 65.0)
        # pick-offs: 30 @ 50 and 20 @ 48 on mk (30 now), 10 @ 67 on A (70 now);
        # B and LATE-A have no fill row
        self.assertEqual((pr["n_matched"], pr["pick_ct"]), (3, 60.0))
        self.assertAlmostEqual(pr["picks_5m"], -1.8 - 1.2 - 0.7)
        self.assertAlmostEqual(pr["picks_now"], -6.0 - 3.6 + 0.3)
        self.assertTrue(pr["ok"])
        # one price read per market (A, mk); none for C, which nothing traded through
        prices = [p for p, _ in self.kal.calls if p != "/markets/trades"]
        self.assertEqual(sorted(prices), [f"/markets/{self.a}", f"/markets/{self.mk}"])
        text, html = th.render_text(ctx), th.render_html(ctx)
        text.encode("ascii")
        self.assertIn("Halts: net -$1.68 -- the fills they blocked would have made $1.25 "
                      "(65 contracts); reward forgone $0.43.", text)
        self.assertIn("Pick-offs: -$9.30 now (-$3.70 at the 5-min check) on 3 fills / 60 "
                      "contracts; 2 not in the fills sink.", text)
        side = next(l for l in text.splitlines() if l.strip().startswith("14:10"))
        for bit in ("25 @ 41 pulled", "-$2.75", "+$2.65", "50->44 30ct -$1.80"):
            self.assertIn(bit, side)
        event = next(l for l in text.splitlines() if self.ev in l and "mkts" in l)
        for bit in ("2/3 mkts put back", "+$4.00", "-$4.33"):
            self.assertIn(bit, event)
        self.assertIn("50ct -$3.00 at 5 min, -$9.60 now", text)    # most picked-off: mk
        self.assertIn("Halts: net -$1.68", html)
        self.assertEqual(th.subject_for(ctx), "IMM toxic halts Mon Sep 28: halts -$1.68 net, "
                                              "pick-offs -$9.30 | 1 event, 1 side (5 pick-offs)")

    def test_failed_reads_read_n_a_and_never_cost_the_email(self):
        kal = FakeKalshi(fail={self.mk, self.a, self.c})
        with mock.patch.object(th, "_get", kal):
            ctx = th.build_report(self.day)
        self.assertFalse(ctx["profit"]["ok"])
        self.assertIsNone(ctx["side_rows"][0]["net"])
        text = th.render_text(ctx)
        self.assertIn("Halts: n/a -- the Kalshi reads failed.", text)
        self.assertIn("! Some Kalshi reads failed", text)
        self.assertTrue(th.subject_for(ctx).startswith(
            "IMM toxic halts Mon Sep 28: halts n/a net, pick-offs n/a |"))

    def test_seven_day_store(self):
        with mock.patch.object(th, "_get", self.kal):
            ctx = th.build_report(self.day)
            # a dry / test run keeps nothing; 9/27 (OLD-X, 22:00 ET) is built
            # from its sinks
            self.assertEqual(th.trail_for(self.day, ctx, save=False)["n_days"], 2)
            self.assertFalse(os.path.exists(th.store_path()))
            # the scheduled run keeps the day
            tr = th.trail_for(self.day, ctx, save=True)
            self.assertAlmostEqual(tr["halts_net"], ctx["profit"]["halts_net"])
            self.assertEqual(sorted(th.load_store()), ["2026-09-27", "2026-09-28"])
            # a missing earlier day is built from its sinks: 9/26 halted a
            # side with nothing up (no cost); 9/20 is outside the 7 days
            t = _ts(2026, 9, 26, 15, 0)
            _write("toxic_halts_2026-09-26.jsonl", [
                {"kind": "side_halt", "ts": t, "until": t + 1800, "ticker": "KXQ-1",
                 "event": "KXQ", "side": "ask", "pickoffs": 2, "window_secs": 86400,
                 "est_per_day": 9.6}])
            _write("toxic_halts_2026-09-20.jsonl", [
                {"kind": "side_halt", "ts": _ts(2026, 9, 20, 15), "until": 0,
                 "ticker": "KXOLD-1", "event": "KXOLD", "side": "ask"}])
            store = th.load_store()
            store["2026-09-28"]["halts_net"] = 7.0          # kept days are read, not rebuilt
            th.save_store(store)
            later = th.build_report(self.day + timedelta(days=1))
            tr = th.trail_for(self.day + timedelta(days=1), later, save=True)
        self.assertEqual((tr["n_days"], tr["first"], tr["last"]),
                         (4, "2026-09-26", "2026-09-29"))
        self.assertAlmostEqual(tr["halts_net"], 7.0)
        self.assertEqual(tr["n_halts"], 3)                  # 9/26's one + 9/28's two
        self.assertEqual(sorted(th.load_store()),
                         ["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"])
        later["trail"] = tr
        self.assertIn("Last 7 days (Sep 26-Sep 29, 4 with data): halts net +$7.00 over 3 "
                      "halts", th.render_text(later))


class TestProfitPieces(unittest.TestCase):
    def test_market_value(self):
        # a settled book reads bid 0 / ask 100: the settlement, not the ask
        self.assertEqual(th.market_value(
            {"status": "finalized", "result": "no", "yes_bid_dollars": "0.0000",
             "yes_ask_dollars": "1.0000", "settlement_value_dollars": "0.0000"}), 0.0)
        self.assertEqual(th.market_value({"result": "yes", "settlement_value_dollars": None}),
                         100.0)
        self.assertEqual(th.market_value(_mkt(61, 62)), 61.5)
        self.assertEqual(th.market_value(_mkt(99, 100)), 99.0)     # no asks
        self.assertEqual(th.market_value(_mkt(0, 5)), 5.0)         # no bids
        self.assertEqual(th.market_value(dict(_mkt(0, 100), last_price_dollars="0.0200")), 2.0)
        self.assertIsNone(th.market_value(_mkt(0, 100)))

    def test_blocked_fills_best_price_first_capped_in_the_window(self):
        q41 = dict(side="bid", px=41.0, size=25.0, start=100.0, end=200.0)
        q38 = dict(side="bid", px=38.0, size=10.0, start=100.0, end=200.0)
        a60 = dict(side="ask", px=60.0, size=5.0, start=100.0, end=200.0)
        got = th.blocked_fills([q41, q38, a60], [
            _trade(120, 42, 10, "no"),        # sold above our bids: no fill
            _trade(150, 37, 30, "no"),        # 25 at 41, then 5 at 38
            _trade(160, 38, 10, "no"),        # the 5 left at 38
            _trade(170, 61, 3, "yes"),        # bought through the 60 ask
            _trade(210, 30, 10, "no")])       # after the halt
        self.assertEqual([(q["px"], n) for q, n in got],
                         [(41.0, 25.0), (38.0, 5.0), (38.0, 5.0), (60.0, 3.0)])


if __name__ == "__main__":
    unittest.main()
