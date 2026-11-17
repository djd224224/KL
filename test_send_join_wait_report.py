#!/usr/bin/env python3
"""Tests for send_join_wait_report.py, the daily join-wait email (Jack
2026-11-02: "give me daily email report on its performance")."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_join_wait_report as jw                                   # noqa: E402
import incentive_mm as imm                                           # noqa: E402


def setUpModule():
    # never the live folder: every path below resolves under STATUS_DIR
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_join_wait_report_test_")


def _ts(h, mi=0, s=0, d=3):
    return datetime(2026, 11, d, h, mi, s, tzinfo=timezone.utc).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


T = "KXRAINDPARIS-03SEP27-T0"


def _row(ts, side="bid", want=55, rest=43, size=4000.0, ev="hold", mode="on", held=0.0):
    return {"ts": ts, "mode": mode, "ticker": T, "side": side, "event": ev,
            "want": want, "rest": rest, "size": size, "held_s": held}


def _tr(ts, yes_c, taker, n):
    return {"created_time": _iso(ts), "yes_price_dollars": f"{yes_c / 100:.4f}",
            "taker_side": taker, "count_fp": str(n)}


# the 11/02 Paris bait's book with our bid held at 43c: a 10,456-lot at 55c
# over our 4,000 at 43c and 7,000 more at 42c; our ask 5,000 of NO 42c's 20,000
BOOK = {"ticker": T, "source": "managed", "unit": "dollars", "target": 1000.0,
        "discount": 0.5, "pool_per_day": 168.4,
        "yes": [["0.5500", "10456"], ["0.4300", "4000"], ["0.4200", "7000"]],
        "no": [["0.4200", "20000"], ["0.4000", "5000"]],
        "own_yes_cents": [[43.0, 4000.0]], "own_no_cents": [[42.0, 5000.0]]}
HOLD_LINE = ("2026-11-03 14:40:16Z [IMM] join wait KXRAINDPARIS-03SEP27-T0 bid: wants 55c, "
             "rests 43c -- 4000 contracts x 13c over its 30-min anchor 42c = $520 at risk, "
             "the first $50 free: 55c must stand 940s\n")
MODE_LINE = "2026-11-03 02:13:01Z join wait (2026-11-02): on -- an at-ref rung joins ...\n"


class TestEpisodes(unittest.TestCase):
    def test_a_hold_closes_on_join_or_gone(self):
        eps = jw.episodes([_row(_ts(10)), _row(_ts(10, 15), ev="gone"),
                           _row(_ts(11), side="ask", want=45, rest=57),
                           _row(_ts(11, 2), side="ask", ev="join", want=45, rest=45)],
                          _ts(3), _ts(23))
        self.assertEqual([(e["side"], e["how"], e["dur"]) for e in eps],
                         [("bid", "gone", 900.0), ("ask", "join", 120.0)])
        self.assertEqual((eps[0]["want"], eps[0]["rest"], eps[0]["size"]), (55, 43, 4000.0))

    def test_waits_are_clipped_to_the_window(self):
        a, b = _ts(3), _ts(23)
        eps = jw.episodes([_row(_ts(1)), _row(_ts(4), ev="join"), _row(_ts(22), side="ask")], a, b)
        self.assertEqual([(e["start"], e["end"], e["how"]) for e in eps],
                         [(a, _ts(4), "join"), (_ts(22), b, "waiting")])
        # wholly before the window: dropped; a close with no hold: ignored
        self.assertEqual(jw.episodes([_row(_ts(1)), _row(_ts(2), ev="gone"),
                                      _row(_ts(5), ev="join")], a, b), [])


class TestLogs(unittest.TestCase):
    def test_hold_lines_the_mode_and_failures(self):
        lines = [MODE_LINE,
                 "2026-11-03 02:19:37Z [IMM] join wait KXOPENSOURCESHARE-26NOV06-T78.3 ask: "
                 "wants 29c, rests 35c -- 400 contracts x 18c over its 30-min anchor 47c = $72 "
                 "at risk, the first $50 free: 29c must stand 44s\n",
                 HOLD_LINE,
                 "2026-11-03 15:00:00Z [IMM] ! join wait failed on X bid (ValueError()); rests "
                 "where it wants (logged once)\n"]
        with mock.patch.object(jw.slr, "day_log_lines", return_value=iter(lines)):
            det, mode, fails = jw.read_logs(_ts(2), _ts(23))
        self.assertEqual((mode, fails), ("on", 1))
        (ts, d), = det[(T, "bid")]
        self.assertEqual((ts, d), (_ts(14, 40, 16), dict(anchor=42, at_risk=520.0, need=940.0)))
        self.assertEqual(det[("KXOPENSOURCESHARE-26NOV06-T78.3", "ask")][0][1]["need"], 44.0)


class TestRewardCost(unittest.TestCase):
    def _sides(self):
        full = {"yes": jw.slr._levels(BOOK["yes"], True), "no": jw.slr._levels(BOOK["no"], True)}
        own = {"yes": jw.slr._own(BOOK["own_yes_cents"]), "no": jw.slr._own(BOOK["own_no_cents"])}
        return full, own

    def test_the_rung_moved_to_the_wanted_price(self):
        full, own = self._sides()
        # at 43c the bid scores nothing (55c alone fills the target); at 55c
        # it is 4,000 of 14,456; the ask's 5,000 of 20,000 stands either way
        rate = jw.wait_rate(full, own, "yes", 55, 1000.0, 0.5, 168.4)
        self.assertAlmostEqual(rate, (4000 / 14456) / 2 * 168.4, places=6)
        # resting at (or past) the wanted price: nothing given up
        self.assertEqual(jw.wait_rate(full, own, "yes", 43, 1000.0, 0.5, 168.4), 0.0)
        self.assertEqual(jw.wait_rate(full, {"yes": {}, "no": own["no"]}, "yes", 55,
                                      1000.0, 0.5, 168.4), 0.0)

    def test_score_waits_accrues_between_snapshots(self):
        full, own = self._sides()
        rate = jw.wait_rate(full, own, "yes", 55, 1000.0, 0.5, 168.4)
        e = dict(ticker=T, side="bid", start=_ts(14, 40, 16), end=_ts(14, 41, 5), want=55)
        jw.score_waits([e], _ts(3), _ts(23), rows=[(_ts(14, 40, 30), BOOK),
                                                   (_ts(14, 40, 47), BOOK)])
        self.assertAlmostEqual(e["cost"], rate * 17 / 86400, places=9)


class TestFills(unittest.TestCase):
    def _e(self, side="bid", want=55, rest=43):
        return dict(ticker=T, side=side, start=_ts(14), end=_ts(15), want=want, rest=rest,
                    size=4000.0)

    def test_trades_through_the_wanted_price_are_kept_out(self):
        trades = [_tr(_ts(14, 40, 50), 55, "no", 4456),   # the bait sale at 55c: 4,000 kept out
                  _tr(_ts(14, 41), 60, "no", 100),        # a sale at 60c never reached 55c
                  _tr(_ts(14, 42), 43, "no", 500),        # our own fill at 43c
                  _tr(_ts(14, 50), 55, "yes", 300),       # a buy: the other side
                  _tr(_ts(16), 50, "no", 300)]            # after the wait
        got = jw.classify_trades([self._e()], trades, {(T, "bid"): [_ts(14, 42, 1)]}, mid=45.0)
        self.assertEqual([(n, round(made, 2)) for _e, n, made in got], [(4000.0, -400.0)])
        # an ask wanting 45c, resting 57c: a buy at 45c is kept out
        got = jw.classify_trades([self._e("ask", 45, 57)], [_tr(_ts(14, 47, 16), 45, "yes", 4465)],
                                 {}, mid=50.0)
        self.assertEqual([(n, round(made, 2)) for _e, n, made in got], [(4000.0, -200.0)])
        # no mark: counted, at $0
        got = jw.classify_trades([self._e()], [_tr(_ts(14, 40, 50), 55, "no", 10)], {}, mid=None)
        self.assertEqual(got[0][1:], (10.0, 0.0))

    def test_the_tape_is_read_most_at_risk_first(self):
        # 11/14's read went in ticker order and its budget ran out before the
        # KXLOWTPERIOD bid that saved $229; now the biggest waits go first
        def e(t, risk, **kw):
            d = dict(ticker=t, side="bid", start=_ts(14), end=_ts(15), want=55, rest=43,
                     size=100.0, at_risk=risk)
            d.update(kw)
            return d
        eps = [e("KXAUDUSDEVENT-A", 50.0), e("KXLOWTPERIOD-B", 400.0),
               e("KXAUDUSDEVENT-A", 60.0, side="ask"),
               # no hold line: size x the gap, 1,000 x 12c = $120
               e("KXBTCEVENT-C", None, size=1000.0)]
        eps[3].pop("at_risk")
        read = []
        with mock.patch.object(jw, "_trades", side_effect=lambda t, a, b: read.append(t) or []):
            got = jw.kept_out(eps, [], {})
        self.assertTrue(got["done"])
        self.assertEqual(read, ["KXLOWTPERIOD-B", "KXBTCEVENT-C", "KXAUDUSDEVENT-A"])
        # out of time after the first read: the smallest are the ones missed
        read.clear()
        with mock.patch.object(jw, "_trades", side_effect=lambda t, a, b: read.append(t) or []), \
                mock.patch.object(jw.time, "time", side_effect=[0.0, 0.0, 1e9]):
            got = jw.kept_out(eps, [], {})
        self.assertFalse(got["done"])
        self.assertEqual(read, ["KXLOWTPERIOD-B"])

    def test_our_own_fills_at_the_resting_price(self):
        fills = [{"ticker": T, "our_book_side": "bid", "ts": _ts(14, 42, 1),
                  "our_price_cents": 43, "count": 500},
                 {"ticker": T, "our_book_side": "ask", "ts": _ts(14, 42, 1),
                  "our_price_cents": 58, "count": 10},
                 {"ticker": T, "our_book_side": "bid", "ts": _ts(16),
                  "our_price_cents": 43, "count": 10}]
        got = jw.better_fills([self._e()], fills)
        self.assertEqual((got["fills"], got["contracts"], round(got["usd"], 2)), (1, 500.0, 60.0))


class TestVerdict(unittest.TestCase):
    RUN = dict(days=1.0, net=0.0)

    def _d(self, **kw):
        d = dict(hours=24.0, mode="on", errors=0, cost=1.0, good=0.0, net=0.0, max_s=60.0,
                 max_side="X bid", max_flaps=1, max_flaps_side="X bid")
        d.update(kw)
        return d

    def test_the_bars(self):
        self.assertEqual(jw.verdict(self._d(), self.RUN), ("OK", []))
        for kw, level in ((dict(cost=6.0), "WATCH"), (dict(cost=16.0), "PROBLEM"),
                          # a partial day scales the reward bars: $1 in 2 h
                          (dict(hours=2.0, cost=1.0), "WATCH"),
                          (dict(good=30.0), "WATCH"), (dict(net=-12.0), "WATCH"),
                          (dict(max_s=3 * 3600), "WATCH"), (dict(max_flaps=30), "WATCH"),
                          (dict(mode="dry"), "WATCH"), (dict(mode="off"), "PROBLEM"),
                          (dict(mode=None), "PROBLEM"), (dict(errors=2), "PROBLEM")):
            self.assertEqual(jw.verdict(self._d(**kw), self.RUN)[0], level, kw)
        # the PROBLEM bar is the last 7 days' net, not the net since launch
        # (Jack 2026-11-17: "Yes, fix the report"): election night's -$156 on
        # 11/03 had held every email at PROBLEM from 11/09
        wk = dict(n=7, days=7, first="2026-11-10", last="2026-11-16")
        self.assertEqual(jw.verdict(self._d(), dict(days=14.0, net=-389.0,
                                                    week=dict(wk, net=-2.0)))[0], "OK")
        level, why = jw.verdict(self._d(), dict(days=14.0, net=0.0, week=dict(wk, net=-51.0)))
        self.assertEqual(level, "PROBLEM")
        self.assertEqual(why, ["the last 7 days' net -$51 (2026-11-10 to 2026-11-16; bar -$50)"])
        # no stored day in the window: nothing to judge
        self.assertEqual(jw.verdict(self._d(), dict(days=1.0, net=-60.0,
                                                    week=dict(wk, days=0, net=0.0)))[0], "OK")

    def test_the_week_window(self):
        def day(net):
            return dict(net=net, saved=0.0, better=0.0, cost=-net if net < 0 else 0.0)
        store = {"2026-11-03": day(-156.0), "2026-11-09": day(-1.0), "2026-11-10": day(-0.5),
                 "2026-11-14": day(93.0), "2026-11-16": day(-2.5), "2026-11-17": day(-9.0)}
        w = jw.window(store, "2026-11-16")
        self.assertEqual((w["days"], w["first"], w["last"]), (3, "2026-11-10", "2026-11-16"))
        self.assertAlmostEqual(w["net"], 90.0)
        self.assertAlmostEqual(w["cost"], 3.0)
        self.assertEqual(jw.window({}, "2026-11-16")["days"], 0)

    def test_money(self):
        self.assertEqual([jw.money(x) for x in (-0.0001, 1.234, -3.5, 400.0, -1234.4)],
                         ["$0.00", "+$1.23", "-$3.50", "+$400", "-$1,234"])


class TestReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_join_wait_report_case_")
        p = mock.patch.object(imm, "STATUS_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, name, rows):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_the_paris_bait_day(self):
        self._write("join_wait_2026-11-03.jsonl", [
            _row(_ts(14, 40, 16)), _row(_ts(14, 41, 5), ev="gone", want=42, rest=42, held=49.0)])
        rows = [(_ts(14, 40, 30), BOOK), (_ts(14, 40, 47), BOOK)]
        trades = [_tr(_ts(14, 40, 50), 55, "no", 4456)]
        with mock.patch.object(jw.slr, "day_log_lines",
                               side_effect=lambda a, b: iter([MODE_LINE, HOLD_LINE])), \
                mock.patch.object(jw, "_trades", return_value=trades), \
                mock.patch.object(jw.slr, "mid_now", return_value=45.0):
            ctx = jw.build_report(date(2026, 11, 3), with_tape=True,
                                  now=datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc),
                                  rows=rows)
        d = ctx["d"]
        self.assertEqual((d["waits"], d["gone"], d["joined"], d["mode"], d["hours"]),
                         (1, 1, 0, "on", 24.0))
        self.assertEqual((d["kept_trades"], d["kept_contracts"]), (1, 4000.0))
        self.assertAlmostEqual(d["saved"], 400.0)
        self.assertGreater(d["cost"], 0.0)
        self.assertEqual(ctx["level"], "OK")
        self.assertEqual(jw.subject_for(ctx),
                         "IMM join wait 11/03: net +$400 | 1 wait, 1 went away | OK")
        text = jw.render_text(ctx)
        self.assertIn("kept us out of 1 trade through a price we waited on -- those fills would "
                      "have lost $400.00 by now", text)
        self.assertIn("wanted 55c, rested 43c; 4,000 contracts, $520 at risk, must stand 16 min "
                      "-> went away after 49s", text)
        self.assertIn("2026-11-03", ctx["store"])

    def _stored(self, day, net, done=True, **kw):
        a = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 5 * 3600
        d = dict(day=day, a_ts=a, b_ts=a + 86400, hours=24.0, net=net, cost=1.0, better=0.0,
                 saved=net + 1.0, good=0.0, kept_trades=0, kept_contracts=0.0, waits=5, gone=1,
                 tape_read=True, tape_done=done)
        d.update(kw)
        return d

    def test_cut_short_days_are_read_again(self):
        # the 11/14 shape: stored at -$203 with its read cut short
        store = {"2026-11-14": self._stored("2026-11-14", -202.77, done=False, cost=49.79,
                                            better=-10.18, saved=-142.80),
                 "2026-11-13": self._stored("2026-11-13", -22.99, done=False),
                 "2026-11-12": self._stored("2026-11-12", -1.74, done=False),
                 "2026-11-10": self._stored("2026-11-10", -0.33),
                 "2026-11-02": self._stored("2026-11-02", -5.0, done=False),   # too old
                 "2026-11-16": self._stored("2026-11-16", -2.39, done=False)}  # today's own
        reads = []

        def kept(eps, fills, mids):
            reads.append(len(eps))
            done = len(reads) == 1
            return dict(trades=3, contracts=400.0, saved=153.40, good=88.18, top=[], done=done)

        now = datetime(2026, 11, 17, 13, 20, tzinfo=timezone.utc)
        with mock.patch.object(jw, "kept_out", side_effect=kept), \
                mock.patch.object(jw, "sink_rows", side_effect=lambda a, b: [_row(a + 3600)]), \
                mock.patch.object(jw.slr, "maker_fills", return_value=[]):
            got = jw.rescore_cut_short(store, "2026-11-16", now)
        # newest first, at most RESCORE_MAX_DAYS; 11/02 is past RESCORE_MAX_AGE_DAYS
        self.assertEqual([(k, done) for k, _o, _n, done in got],
                         [("2026-11-14", True), ("2026-11-13", False)])
        v = store["2026-11-14"]
        self.assertAlmostEqual(v["net"], 153.40 - 10.18 - 49.79)
        self.assertEqual((v["tape_done"], v["kept_trades"], v["good"], v["cost"]),
                         (True, 3, 88.18, 49.79))
        self.assertIn("rescored", v)
        # cut short again: kept as stored, read again next run
        self.assertEqual((store["2026-11-13"]["net"], store["2026-11-13"]["tape_done"]),
                         (-22.99, False))
        self.assertEqual(store["2026-11-12"]["net"], -1.74)
        self.assertNotIn("rescored", store["2026-11-02"])

    def test_the_email_shows_the_week_and_a_re_read(self):
        with open(jw.store_path(), "w", encoding="utf-8") as f:
            json.dump({"2026-11-02": self._stored("2026-11-02", -60.0, done=False, cost=2.0,
                                                  better=-1.0, saved=-57.0)}, f)
        with mock.patch.object(jw.slr, "day_log_lines", side_effect=lambda a, b: iter([MODE_LINE])):
            ctx = jw.build_report(date(2026, 11, 3), with_tape=True,
                                  now=datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc))
        # 11/02 had no waits in the sink: the finished re-read finds nothing kept out
        self.assertEqual(ctx["rescored"], [("2026-11-02", -60.0, -3.0, True)])
        self.assertEqual(ctx["level"], "OK")
        text = jw.render_text(ctx)
        self.assertIn("Re-read 11/02's public trades (its first read did not finish): "
                      "net -$60 -> -$3.00.", text)
        self.assertIn("Last 7 days (11/02-11/03, the PROBLEM bar -$50): net -$3.00", text)
        # --no-rescore leaves a stored day as it was
        with open(jw.store_path(), "w", encoding="utf-8") as f:
            json.dump({"2026-11-02": self._stored("2026-11-02", -60.0, done=False)}, f)
        with mock.patch.object(jw.slr, "day_log_lines", side_effect=lambda a, b: iter([MODE_LINE])):
            ctx = jw.build_report(date(2026, 11, 3), with_tape=True, rescore=False,
                                  now=datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc))
        self.assertEqual((ctx["rescored"], ctx["level"]), ([], "PROBLEM"))

    def test_a_quiet_day_and_a_dry_run(self):
        with mock.patch.object(jw.slr, "day_log_lines", side_effect=lambda a, b: iter([MODE_LINE])):
            ctx = jw.build_report(date(2026, 11, 3), with_tape=False,
                                  now=datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(jw.subject_for(ctx), "IMM join wait 11/03: no waits | OK")
            self.assertIn("no waits today", jw.render_text(ctx))
            # --dry: built and printed, nothing stored or sent
            with mock.patch.object(imm, "Alerter") as al:
                self.assertEqual(jw.main(["--dry", "--day", "2026-11-03", "--no-tape"]), 0)
            al.assert_not_called()
        self.assertFalse(os.path.exists(jw.store_path()))


if __name__ == "__main__":
    unittest.main()
