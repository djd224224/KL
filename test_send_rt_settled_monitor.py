"""Tests for send_rt_settled_monitor.py (the daily RT SETTLED STRIKES email).
Run with IMM_STATUS_DIR at a scratch directory: the end-to-end test writes a
day log, sinks and the reward cache there.

Worth testing: the on / off spans (restart banners end them, the window clips
them), which fills count, the swing loss of a held position, the verdict, and
one report built off files."""
import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone

import send_rt_settled_monitor as rs

imm = rs.imm
T = "KXRT-GOD-80"


def _at(h, m=0, d=1):
    return datetime(2026, 11, d, h, m, tzinfo=timezone.utc)


def _line(dt, rest):
    return f"{dt:%Y-%m-%d %H:%M:%S}Z [IMM] {rest}"


def _on(dt, t=T, n=52, score=92, k=80, model=99.1, mid=96.5):
    return _line(dt, f"rt settled {t}: on -- {n} reviews, score {score}%, strike {k}, "
                     f"model {model}%, book mid {mid}c; a touch outside the normal band")


class TestSpans(unittest.TestCase):
    def test_on_off_banner_and_the_window(self):
        a, b = _at(4).timestamp(), _at(4, d=2).timestamp()
        lines = [_on(_at(2)),                                          # before the window
                 _line(_at(6), f"rt settled {T}: off -- the book's 85c mid disagrees"),
                 _on(_at(8)),
                 f"{_at(9):%Y-%m-%d %H:%M:%S}Z === incentive_mm_v1.2 run=x mode=LIVE ===",
                 _on(_at(9, 1)),
                 _on(_at(10), t="KXRT-IPL-70", n=54, score=85, k=70, model=98.6, mid=97.5)]
        sp = rs.parse_spans(lines, a, b)
        self.assertEqual(sp[T]["spans"], [[a, _at(6).timestamp()], [_at(8).timestamp(),
                                                                    _at(9).timestamp()],
                                          [_at(9, 1).timestamp(), b]])
        self.assertTrue(sp[T]["on_at_end"])
        self.assertEqual(sp[T]["info"], dict(n=52, score=92.0, strike=80.0, model=99.1,
                                             mid=96.5))
        self.assertEqual(sp["KXRT-IPL-70"]["info"]["strike"], 70.0)
        # an off line with no span open, and a span wholly before the window
        sp = rs.parse_spans([_on(_at(1)), _line(_at(2), f"rt settled {T}: off -- x"),
                             _line(_at(5), "rt settled KXRT-X-9: off -- y")], a, b)
        self.assertEqual(sp, {})


class TestMoney(unittest.TestCase):
    def test_fills_rewards_and_the_swing(self):
        s, e = _at(8).timestamp(), _at(12).timestamp()
        spans = {T: {"spans": [[s, e]], "info": {"score": 92.0, "strike": 80.0}}}
        fills = [dict(ticker=T, ts=int(_at(9).timestamp()), yes_price_cents=96, count=20,
                      is_taker=False, our_book_side="bid"),
                 dict(ticker=T, ts=int(_at(13).timestamp()), yes_price_cents=96, count=5,
                      is_taker=False, our_book_side="bid"),               # after the span
                 dict(ticker=T, ts=int(_at(10).timestamp()), yes_price_cents=97, count=7,
                      is_taker=True, our_book_side="ask"),                # our own take
                 dict(ticker="KXRT-ZZ-50", ts=int(_at(10).timestamp()), yes_price_cents=50,
                      count=3, is_taker=False, our_book_side="ask")]       # not quoted by it
        got = rs.span_fills(fills, spans)
        self.assertEqual([(f["count"], f["px"], f["side"]) for f in got], [(20.0, 96.0, "bid")])
        rs.value(got, {T: 98.0})
        self.assertAlmostEqual(got[0]["usd"], 0.40)
        h = int(s // 3600)
        rew = rs.rewards(spans, {T: {h: 1.0, h + 1: 1.0, h + 2: 2.0, h + 3: 1.0, h + 4: 9.0}})
        self.assertAlmostEqual(rew[T], 5.0)                      # 08-12Z: 1 + 1 + 2 + 1
        # a long YES under the score loses its cost on a swing; a short YES
        # over the score loses the rest of the dollar; the other way, nothing
        yes = {"score": 92.0, "strike": 80.0}
        no = {"score": 70.0, "strike": 80.0}
        self.assertAlmostEqual(rs.swing_loss(100, 96.0, yes), 96.0)
        self.assertAlmostEqual(rs.swing_loss(-100, 3.0, no), 97.0)
        self.assertEqual(rs.swing_loss(-100, 97.0, yes), 0.0)
        self.assertEqual(rs.swing_loss(100, 3.0, no), 0.0)

    def test_verdict(self):
        ctx = dict(settled=[], since={"net": 4.0}, net=1.0, open={"at_risk": 50.0})
        self.assertEqual(rs.verdict(ctx), ("OK", []))
        self.assertEqual(rs.verdict(dict(ctx, net=-0.5))[0], "WATCH")
        self.assertEqual(rs.verdict(dict(ctx, open={"at_risk": 400.0}))[0], "WATCH")
        self.assertEqual(rs.verdict(dict(ctx, since={"net": -60.0}))[0], "STOP")
        lvl, why = rs.verdict(dict(ctx, settled=[{"as_called": False}]))
        self.assertEqual(lvl, "STOP")
        self.assertIn("against the side the model called", why[0])


class TestReport(unittest.TestCase):
    """One day off files in the scratch STATUS_DIR."""

    def setUp(self):
        self.d = imm.STATUS_DIR
        self.made = []

    def tearDown(self):
        for p in self.made:
            try:
                os.remove(p)
            except OSError:
                pass

    def _write(self, name, text):
        p = os.path.join(self.d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        self.made.append(p)

    def test_a_day_off_files(self):
        day = date(2026, 10, 30)                       # 04:00Z 10/30 -> 04:00Z 10/31 (EDT)
        a, b = rs.lm.day_window(day)
        on_at = datetime.fromtimestamp(a, timezone.utc) + timedelta(hours=4)
        self._write("incentive-mm-2026-10-30.log", "\n".join([
            _on(on_at),
            _line(on_at + timedelta(hours=6), f"rt settled {T}: off -- the RT read is stale"),
        ]) + "\n")
        f1 = dict(ticker=T, ts=int((on_at + timedelta(hours=1)).timestamp()),
                  yes_price_cents=96, count=10, is_taker=False, our_book_side="bid",
                  order_id="o1")
        self._write("fills_2026-10-30.jsonl", json.dumps(f1) + "\n")
        h0 = int(on_at.timestamp() // 3600)
        cache = {"cycle_log_2026-10-30.csv": {"markets": {T: {str(h0 + i): 0.5
                                                             for i in range(8)}}}}
        self._write(rs.lm.HOURLY_CACHE_FILE, json.dumps(cache))
        self._write(rs.STORE_FILE, json.dumps({"days": {"2026-10-29": {"net": -2.0}}}))
        now = datetime.fromtimestamp(b, timezone.utc) + timedelta(hours=8)
        ctx = rs.build_report(day, now=now)
        self.assertEqual(list(ctx["spans"]), [T])
        self.assertAlmostEqual(ctx["net_rewards"], 3.0)          # 6 hours x $0.50
        self.assertEqual(len(ctx["fills"]), 1)
        self.assertIsNone(ctx["fills"][0]["usd"])                # no mark in the sinks
        self.assertAlmostEqual(ctx["since"]["net"], 1.0)         # -2 + 3
        self.assertEqual(ctx["level"], "OK")
        text = rs.render_text(ctx)
        self.assertIn("NET +$3.00: rewards +$3.00 on 1 strike(s) over 6.0 market-hours", text)
        self.assertIn("Since it went live (2026-10-29): net +$1.00 over 2 day(s)", text)
        self.assertIn("RT settled strikes 10/30: net +$3.00", rs.subject_for(ctx))
        self.assertIn("<li>", rs.render_html(ctx))


if __name__ == "__main__":
    unittest.main()
