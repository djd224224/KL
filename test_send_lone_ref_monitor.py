#!/usr/bin/env python3
"""Unit tests for send_lone_ref_monitor.py -- the daily lone-reference guard
email (Jack 2026-10-16: "turn it on, and track the results").

Run: python -m unittest test_send_lone_ref_monitor

Worth testing: holds read off the day log (clears, restarts, the dry tag,
the window clip), the reward a held side gives up (the bot's share model
with our rung put back), the would-be fills a hold avoided, the jumped
fills the guard let through, and the verdict rules.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_lone_ref_monitor as lm                                    # noqa: E402
import incentive_mm as imm                                           # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_lone_ref_monitor_test_")
    imm.ALERT_RECIPIENTS = []


def _ts(h, mi=0, s=0, d=16):
    return datetime(2026, 10, d, h, mi, s, tzinfo=timezone.utc).timestamp()


HOLD = ("2026-10-16 {t}Z [IMM] lone-ref guard{dry} {tk} {side}: the reference {ref}c rests "
        "on 200 external contracts (< 3 x 200), the rest of the book's is {rest}c; "
        "{how} -- side stood aside\n")


def hold(t, tk="KXA-1", side="bid", ref=69, rest=6, how="our rung jumped 63c in 60m",
         dry=""):
    return HOLD.format(t=t, tk=tk, side=side, ref=ref, rest=rest, how=how, dry=dry)


def clear(t, tk="KXA-1", side="bid"):
    return f"2026-10-16 {t}Z [IMM] lone-ref guard {tk} {side}: clear\n"


class TestEpisodes(unittest.TestCase):

    def test_hold_clear_restart_and_window(self):
        lines = [
            "2026-10-16 16:17:00Z === incentive_mm_v1.2 run=a mode=LIVE config=x ===\n",
            "2026-10-16 16:17:00Z lone-reference guard (2026-10-16): on -- a side whose ...\n",
            hold("16:20:00"),
            hold("16:20:30"),                                   # still held: no new episode
            clear("16:21:00"),
            hold("16:30:00", tk="KXB-2", side="ask", ref=31, rest=85, how="no rung there before"),
            "2026-10-16 16:40:00Z === incentive_mm_v1.2 run=b mode=LIVE config=y ===\n",
            hold("16:50:00", dry=" (dry)"),                     # runs past the window's end
        ]
        eps, mode = lm.parse_episodes(lines, _ts(16, 19), _ts(16, 55))
        self.assertEqual(mode, "on")
        self.assertEqual([(e["ticker"], e["side"]) for e in eps],
                         [("KXA-1", "bid"), ("KXB-2", "ask"), ("KXA-1", "bid")])
        a, b, c = eps
        self.assertEqual((a["start"], a["end"], a["ref"], a["rest"], a["jump"]),
                         (_ts(16, 20), _ts(16, 21), 69, 6, 63))
        self.assertEqual((b["end"], b["fresh"], b["jump"]), (_ts(16, 40), True, None))   # restart
        self.assertEqual((c["start"], c["end"]), (_ts(16, 50), _ts(16, 55)))           # clipped

    def test_a_hold_from_before_the_window_is_clipped(self):
        eps, _m = lm.parse_episodes([hold("15:00:00"), clear("16:30:00")],
                                    _ts(16, 0), _ts(17, 0))
        self.assertEqual((eps[0]["start"], eps[0]["end"]), (_ts(16, 0), _ts(16, 30)))
        eps, _m = lm.parse_episodes([hold("15:00:00"), clear("15:30:00")],
                                    _ts(16, 0), _ts(17, 0))
        self.assertEqual(eps, [])


def _row(ts, yes, no, own_yes=(), own_no=(), tk="KXA-1"):
    return ts, {"ticker": tk, "source": "managed", "unit": "dollars", "target": 1000.0,
                "discount": 0.5, "pool_per_day": 100.0,
                "yes": [[f"{p / 100:.4f}", str(q)] for p, q in yes],
                "no": [[f"{p / 100:.4f}", str(q)] for p, q in no],
                "own_yes_cents": [list(x) for x in own_yes],
                "own_no_cents": [list(x) for x in own_no]}


class TestHoldCost(unittest.TestCase):
    # the Teemo shape: a 200-lot at 69 over 2,000 at 6; NO side deep at 27
    YES = [(69, 200.0), (6, 2000.0), (1, 2105.0)]
    NO = [(27, 1500.0)]

    def test_rate_puts_our_rung_back_at_the_reference(self):
        full = {"yes": lm._levels([[f"{p / 100}", q] for p, q in self.YES], True),
                "no": lm._levels([[f"{p / 100}", q] for p, q in self.NO], True)}
        own = {"yes": {}, "no": {}}
        rate, dist = lm.held_side_rate(full, own, "yes", 1000.0, 0.5, 100.0, 60.0)
        self.assertEqual(dist, 0)                     # the reference IS the touch
        # 60 of the 260 at 69 score full weight; the side's share halves into the pool
        self.assertAlmostEqual(rate, 100.0 * (60.0 / (260.0 + 0.5 ** 63 * 2000.0)) / 2.0, 4)

    def test_cost_accrues_over_the_hold_and_classes_it(self):
        eps = [dict(ticker="KXA-1", side="bid", start=_ts(17, 0), end=_ts(17, 2), ref=69,
                    top=200, rest=6, fresh=False, jump=63)]
        rows = [_row(_ts(16, 59), [(6, 2060.0), (1, 2105.0)], self.NO, own_yes=[(6, 60.0)]),
                _row(_ts(17, 0, 30), self.YES, self.NO),
                _row(_ts(17, 1, 30), self.YES, self.NO),
                _row(_ts(17, 2, 30), [(6, 2000.0)], self.NO)]
        lm.score_holds(eps, _ts(16, 0), _ts(18, 0), rows=rows)
        e = eps[0]
        self.assertEqual((e["cls"], e["size"]), ("at the touch", 60.0))
        rate = 100.0 * (60.0 / (260.0 + 0.5 ** 63 * 2000.0)) / 2.0
        self.assertAlmostEqual(e["cost"], rate * 120.0 / 86400.0, 6)

    def test_a_reference_under_a_real_touch_is_inside_the_book(self):
        eps = [dict(ticker="KXA-1", side="bid", start=_ts(17, 0), end=_ts(17, 5), ref=30,
                    top=330, rest=3, fresh=False, jump=22)]
        ladder = [(56, 20.0), (50, 40.0), (40, 60.0), (30, 90.0), (3, 3000.0)]
        lm.score_holds(eps, _ts(16, 0), _ts(18, 0),
                       rows=[_row(_ts(17, 1), ladder, [(43, 2000.0)])])
        self.assertEqual(eps[0]["cls"], "inside the book")


def _trade(h, mi, yes_c, taker, n):
    return {"created_time": f"2026-10-16T{h:02d}:{mi:02d}:00Z",
            "yes_price_dollars": f"{yes_c / 100:.4f}", "taker_side": taker, "count_fp": str(n)}


class TestWouldBeFills(unittest.TestCase):

    def test_trades_through_held_prices_capped_at_the_rung(self):
        bid = dict(ticker="KXA-1", side="bid", start=_ts(17), end=_ts(18), ref=29, size=60.0,
                   cls="at the touch")
        ask = dict(ticker="KXA-1", side="ask", start=_ts(17), end=_ts(18), ref=40, size=30.0,
                   cls="at the touch")
        trades = [_trade(17, 5, 29, "no", 50),      # sold into the held bid: 50 of 60
                  _trade(17, 6, 28, "no", 100),     # ...the last 10
                  _trade(17, 7, 30, "no", 100),     # above the bid: would not reach it
                  _trade(17, 8, 41, "yes", 100),    # bought through the held ask: 30
                  _trade(18, 30, 20, "no", 100)]    # after the hold
        got = lm.would_be_fills([bid, ask], trades, 19.5)
        self.assertEqual([(e["side"], n) for e, n, _u in got],
                         [("bid", 50.0), ("bid", 10.0), ("ask", 30.0)])
        self.assertAlmostEqual(sum(u for _e, _n, u in got),
                               (19.5 - 29) * 0.60 + (40 - 19.5) * 0.30, 6)


class TestJumpedFills(unittest.TestCase):

    def setUp(self):
        d = imm.STATUS_DIR
        fills = [
            dict(ts=_ts(17, 10), ticker="KXLOLTEEMO-WORLDS26-Y", series="KXLOLTEEMO",
                 our_book_side="bid", our_price_cents=69, yes_price_cents=69.0, count=60.0,
                 our_left_before=60.0, is_taker=False, is_pad=False),
            dict(ts=_ts(17, 12), ticker="KXTRUMPMENTION-26OCT19-BOEI",
                 series="KXTRUMPMENTION", our_book_side="ask", our_price_cents=25,
                 yes_price_cents=25.0, count=20.0, our_left_before=20.0, is_taker=False,
                 is_pad=False),
            dict(ts=_ts(17, 14), ticker="KXLOLTEEMO-WORLDS26-Y", series="KXLOLTEEMO",
                 our_book_side="bid", our_price_cents=69, yes_price_cents=69.0, count=10.0,
                 our_left_before=60.0, is_taker=False, is_pad=False),        # partial
        ]
        with open(os.path.join(d, "fills_2026-10-16.jsonl"), "w") as f:
            for r in fills:
                f.write(json.dumps(r) + "\n")
        orders = [
            dict(ts="2026-10-16T17:00:00+00:00", kind="place", ticker="KXLOLTEEMO-WORLDS26-Y",
                 book_side="bid", price_cents=6, is_pad=False),
            dict(ts="2026-10-16T17:09:00+00:00", kind="amend", ticker="KXLOLTEEMO-WORLDS26-Y",
                 book_side="bid", price_cents=69, is_pad=False),
            dict(ts="2026-10-16T17:00:00+00:00", kind="place",
                 ticker="KXTRUMPMENTION-26OCT19-BOEI", book_side="ask", price_cents=73,
                 is_pad=False),
            dict(ts="2026-10-16T17:11:00+00:00", kind="amend",
                 ticker="KXTRUMPMENTION-26OCT19-BOEI", book_side="ask", price_cents=25,
                 is_pad=False),
        ]
        with open(os.path.join(d, "orders_2026-10-16.jsonl"), "w") as f:
            for r in orders:
                f.write(json.dumps(r) + "\n")

    def test_full_rung_fills_after_a_jump(self):
        got = lm.jumped_fills(_ts(16), _ts(18), {}, with_marks=False)
        self.assertEqual([(f["ticker"], j, g) for f, j, g, _u in got],
                         [("KXLOLTEEMO-WORLDS26-Y", 63, True),
                          ("KXTRUMPMENTION-26OCT19-BOEI", 48, False)])


class TestVerdict(unittest.TestCase):

    def day(self, **kw):
        d = dict(mode="on", hours=24.0, let_thru_guarded=0.0, cost=10.0, net=5.0,
                 max_flaps=3, max_flaps_side="KXA-1 bid")
        d.update(kw)
        return d

    RUN = dict(days=1.0, net=5.0)

    def test_levels(self):
        self.assertEqual(lm.verdict(self.day(), self.RUN)[0], "OK")
        self.assertEqual(lm.verdict(self.day(mode="dry"), self.RUN)[0], "PROBLEM")
        self.assertEqual(lm.verdict(self.day(let_thru_guarded=-30.0), self.RUN)[0], "WATCH")
        self.assertEqual(lm.verdict(self.day(let_thru_guarded=-120.0), self.RUN)[0], "PROBLEM")
        self.assertEqual(lm.verdict(self.day(cost=40.0), self.RUN)[0], "WATCH")
        self.assertEqual(lm.verdict(self.day(net=-20.0), self.RUN)[0], "WATCH")
        self.assertEqual(lm.verdict(self.day(max_flaps=150), self.RUN)[0], "OK")
        self.assertEqual(lm.verdict(self.day(max_flaps=201), self.RUN)[0], "WATCH")
        self.assertEqual(lm.verdict(self.day(), dict(days=4.0, net=-60.0))[0], "PROBLEM")
        # a half day halves the cost bars
        self.assertEqual(lm.verdict(self.day(hours=12.0, cost=20.0), self.RUN)[0], "WATCH")

    def test_subject_and_text(self):
        ctx = dict(level="OK", reasons=[], run=dict(days=0.5, net=12.0),
                   d=dict(day="2026-10-16", net=12.4, would_be=-20.0, cost=7.6, mode="on",
                          a_ts=_ts(16, 17), b_ts=_ts(23, 59), holds=342, sides=92, markets=88,
                          at_once=13.0, at_once_max=21, median_s=57.0,
                          cls={"at the touch": 163, "inside the book": 179, "no snapshot": 0},
                          max_flaps=28, max_flaps_side="KXRAINWKND-26OCT17-NYC bid",
                          would_be_trades=3, tape_done=True, let_thru_guarded_n=0,
                          would_be_by_cls={"at the touch": -5.0, "inside the book": 25.0},
                          gates={"none": [300, 6.0, -12.0], "carbon arc": [42, 1.6, -8.0]},
                          let_thru_guarded=0.0, let_thru_skipped_n=13, let_thru_skipped=-19.0))
        self.assertEqual(lm.subject_for(ctx),
                         "IMM lone-ref guard 10/16: +$12 (blocking fills +$20, reward -$8) | OK")
        text = lm.render_text(ctx)
        self.assertTrue(text.startswith("OK: net +$12. The fills its holds blocked would have "
                                        "lost $20, and it gave up $8 of reward."))
        self.assertIn("would be -$20 at the current mid -- at the touch -$5, inside the book +$25", text)
        ctx["d"]["would_be"] = 21.0
        self.assertIn("blocked would have made $21", lm.render_text(ctx))
        self.assertIn("Held 342 times on 92 sides of 88 markets", text)


class TestFairGates(unittest.TestCase):
    """Jack 2026-10-17: "Keep as is but monitor in daily emails" -- the email
    splits the guard by whether the market has a fair gate."""

    def test_the_bot_s_own_checks_name_the_gate(self):
        with mock.patch.object(imm, "CA_FAIR_ENABLE", True), \
                mock.patch.object(imm, "carbon_arc_settled", lambda s: s == "KXAMZNCC"), \
                mock.patch.object(imm, "rain_wknd_series", lambda s: s == "KXRAINWKND"), \
                mock.patch.object(imm, "vercel_series", lambda s: s == "KXVERCEL"):
            self.assertEqual(lm.fair_gate_of("KXAMZNCC"), "carbon arc")
            self.assertEqual(lm.fair_gate_of("KXRAINWKND"), "rain weekend")
            self.assertEqual(lm.fair_gate_of("KXVERCEL"), "vercel")
            self.assertEqual(lm.fair_gate_of("KXLOLTEEMO"), "")

    def test_totals_and_the_watch(self):
        gate = {"KXAMZNCC": "carbon arc", "KXRAINWKND": "rain weekend"}.get
        eps = [dict(ticker="KXAMZNCC-1", cost=3.0), dict(ticker="KXLOLTEEMO-Y", cost=1.0),
               dict(ticker="KXRAINWKND-NYC", cost=0.5)]
        top = [(35.1, "KXRAINWKND-NYC"), (-6.3, "KXAMZNCC-1"), (-10.0, "KXLOLTEEMO-Y")]
        g = lm.gate_totals(eps, top, lambda tk: gate(tk.split("-")[0], ""))
        self.assertEqual(g, {"carbon arc": [1, 3.0, -6.3], "none": [1, 1.0, -10.0],
                             "rain weekend": [1, 0.5, 35.1]})
        self.assertAlmostEqual(lm.gate_net(g["rain weekend"]), -35.6)
        day = dict(mode="on", hours=24.0, let_thru_guarded=0.0, cost=4.5, net=-19.3,
                   max_flaps=3, max_flaps_side="")
        lvl, why = lm.verdict(day, dict(days=4.0, net=60.0, gates=g))
        self.assertEqual(lvl, "WATCH")
        self.assertTrue(any("fair-gated rain weekend" in r for r in why))
        self.assertFalse(any("carbon arc" in r for r in why))      # it saved money
        # not before RUN_MIN_DAYS
        self.assertFalse(any("fair-gated" in r for r in
                             lm.verdict(day, dict(days=1.0, net=60.0, gates=g))[1]))
        line = lm.fair_line(dict(gates=g), dict(gates=g))
        self.assertTrue(line.startswith("Fair value: markets with a fair gate net -$32 today"))
        self.assertIn("markets without +$9", line)
        self.assertIn("Carbon Arc +$3 (+$3 running; replay -$2/day)", line)


class TestDryRun(unittest.TestCase):

    def test_dry_run_on_an_empty_status_dir_needs_no_network(self):
        with mock.patch.object(lm, "_get", side_effect=AssertionError("network")):
            self.assertEqual(lm.main(["--dry", "--no-tape", "--day", "2026-10-16"]), 0)
        self.assertFalse(os.path.exists(lm.store_path()))          # --dry writes nothing


if __name__ == "__main__":
    unittest.main()
