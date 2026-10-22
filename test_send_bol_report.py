#!/usr/bin/env python3
"""Unit tests for send_bol_report.py (Jack 2026-10-22: "Give me Daily email
report on it"). No network: a fake tape; the sinks are written into a temp
STATUS_DIR.

Run: python -m unittest test_send_bol_report
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_bol_report as br                                          # noqa: E402
import incentive_mm as imm                                           # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_bol_report_test_")
    imm.ALERT_RECIPIENTS = []


DAY = date(2026, 10, 21)        # ET: 04:00Z 10/21 -> 04:00Z 10/22
T = "KXAAA-26OCT21-T1"


def _z(h, mi=0, s=0, d=21):
    return datetime(2026, 10, d, h, mi, s, tzinfo=timezone.utc).timestamp()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _move(ts, oid="o1", ticker=T, side="bid", px=40, ahead=100.0, ext=400.0, rem=50.0,
          restored=True):
    return {"ts": ts, "mode": "on", "ticker": ticker, "order_id": oid, "side": side, "px": px,
            "rem": rem, "ahead": ahead, "ext": ext, "behind": ext - ahead, "bumps": 0,
            "ok": True, "restored": restored}


def _write(name, utc_day, rows):
    with open(os.path.join(imm.STATUS_DIR, f"{name}_{utc_day}.jsonl"), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class _Base(unittest.TestCase):
    def setUp(self):
        for n in os.listdir(imm.STATUS_DIR):
            p = os.path.join(imm.STATUS_DIR, n)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


class TestReplayMove(_Base):

    def test_the_old_spot_against_the_real_fills(self):
        t0 = _z(12)
        tape = [[t0 + 10, 120.0, 40.0, "no"],      # old spot: 20 past the 100 ahead
                [t0 + 15, 500.0, 41.0, "no"],      # another price: ignored
                [t0 + 18, 500.0, 40.0, "yes"],     # the other side: ignored
                [t0 + 20, 60.0, 40.0, "no"],       # old spot: the last 30 of our 50
                [t0 + 4 * 3600 - 60, 1.0, 30.0, "yes"]]   # the 4h mark: 30c
        times = [x[0] for x in tape]
        x = br.replay_move(_move(t0), tape, times, t0 + 300, real=10.0)
        self.assertEqual((x["old"], x["real"], x["avoided"]), (50.0, 10.0, 40.0))
        self.assertAlmostEqual(x["value"], 40 * (30 - 40) / 100.0)   # a loss avoided
        self.assertTrue(x["traded"])

    def test_an_ask_is_hit_by_yes_takers_and_marked_the_other_way(self):
        t0 = _z(12)
        tape = [[t0 + 5, 130.0, 60.0, "yes"], [t0 + 4 * 3600 - 1, 1.0, 70.0, "no"]]
        x = br.replay_move(_move(t0, side="ask", px=60), tape, [t[0] for t in tape],
                           t0 + 300, real=0.0)
        self.assertEqual(x["avoided"], 30.0)
        self.assertAlmostEqual(x["value"], 30 * (60 - 70) / 100.0)

    def test_nothing_reaches_the_old_spot(self):
        t0 = _z(12)
        tape = [[t0 + 5, 80.0, 40.0, "no"]]
        x = br.replay_move(_move(t0), tape, [t0 + 5], t0 + 300, real=0.0)
        self.assertEqual((x["old"], x["avoided"], x["value"]), (0.0, 0.0, 0.0))
        self.assertTrue(x["traded"])


class TestBuildReport(_Base):

    def _day(self, fills=(), restored=True):
        t0 = _z(12)
        _write("back_of_line", "2026-10-21", [_move(t0, restored=restored)])
        _write("orders", "2026-10-21", [
            {"ts": _iso(t0), "kind": "amend", "order_id": "o1", "ticker": T, "reason": "back_of_line",
             "price_cents": 40, "count": 51, "prev_price_cents": 40, "prev_count": 50.0},
            {"ts": _iso(t0 + 0.1), "kind": "amend", "order_id": "o1", "ticker": T,
             "reason": "back_of_line_restore", "price_cents": 40, "count": 50,
             "prev_price_cents": 40, "prev_count": 51.0},
            # the bot reprices the order 30s later: the window ends there
            {"ts": _iso(t0 + 30), "kind": "amend", "order_id": "o1", "ticker": T, "reason": None,
             "price_cents": 41, "count": 50, "prev_price_cents": 40, "prev_count": 50.0}])
        if fills:
            _write("fills", "2026-10-21", [{"ts": t0 + 12, "order_id": "o1", "count": c,
                                            "is_taker": False} for c in fills])
        self.tape = {T: [[t0 + 10, 150.0, 40.0, "no"],     # inside: 50 past the 100 ahead
                         [t0 + 60, 150.0, 40.0, "no"],     # after the reprice: out
                         [t0 + 4 * 3600 - 30, 1.0, 20.0, "yes"]]}

    def _build(self, **kw):
        with mock.patch.object(br, "fetch_tape",
                               side_effect=lambda tk, a, b, cache: self.tape[tk]):
            return br.build_report(DAY, **kw)

    def test_a_day_end_to_end(self):
        self._day()
        ctx = self._build()
        d = ctx["d"]
        self.assertEqual((d["moves"], d["markets"], d["covered"], d["mattered"]), (1, 1, 1, 1))
        self.assertEqual(d["avoided_ct"], 50.0)
        self.assertAlmostEqual(d["saved"], 50 * (40 - 20) / 100.0)        # +$10 saved
        self.assertEqual((d["losses_avoided"], d["wins_forgone"]), (10.0, 0.0))
        self.assertEqual((d["bol_amends"], d["writes"]), (2, 3))
        self.assertEqual(d["by_family"]["other"]["saved"], 10.0)
        self.assertEqual(br.verdict(d, ctx["run"])[0], "OK")
        self.assertEqual(br.subject_for(ctx),
                         "IMM back-of-line 10/21: saved $10.00 at 4h | 1 moves, 67% of writes | OK")
        text = br.render_text(ctx)
        self.assertIn("SAVED at 4h: $10.00 net -- $10.00 of losing fills avoided", text)
        self.assertIn("2 back-of-line amends = 67% of the bot's 3 order writes", text)

    def test_real_fills_take_back_the_avoided_ones(self):
        self._day(fills=(30.0,))
        d = self._build()["d"]
        self.assertEqual(d["avoided_ct"], 20.0)
        self.assertAlmostEqual(d["saved"], 20 * 0.20)

    def test_a_restore_fail_watches(self):
        self._day(restored=False)
        ctx = self._build()
        level, why = br.verdict(ctx["d"], ctx["run"])
        self.assertEqual(level, "WATCH")
        self.assertIn("not restored", why)

    def test_no_tape_reports_without_a_saved_figure(self):
        self._day()
        ctx = br.build_report(DAY, with_tape=False)
        self.assertEqual(ctx["d"]["covered"], 0)
        self.assertNotIn(DAY.isoformat(), ctx["store"].get("days") or {})
        self.assertIn("no tape", br.subject_for(ctx))


class TestVerdictAndRunning(_Base):

    def _d(self, saved, **kw):
        d = {"with_tape": True, "saved": saved, "restore_fails": 0, "markets": 10, "covered": 10}
        d.update(kw)
        return d

    def test_money_format(self):
        self.assertEqual([br.usd(x) for x in (-0.0, -0.001, 0.004, -1.5, 1234.567)],
                         ["$0.00", "$0.00", "$0.00", "-$1.50", "$1,234.57"])

    def test_levels(self):
        self.assertEqual(br.verdict(self._d(5.0), {"days": 1, "per_day": 5.0})[0], "OK")
        self.assertEqual(br.verdict(self._d(-3.0), {"days": 1, "per_day": -3.0})[0], "WATCH")
        self.assertEqual(br.verdict(self._d(5.0, covered=5), {"days": 1, "per_day": 5.0})[0],
                         "WATCH")
        level, why = br.verdict(self._d(-12.0), {"days": 3, "per_day": -12.0})
        self.assertEqual(level, "PROBLEM")
        self.assertIn("IMM_BACK_OF_LINE=dry", why)
        # two bad days are not yet a PROBLEM
        self.assertEqual(br.verdict(self._d(-12.0), {"days": 2, "per_day": -12.0})[0], "WATCH")

    def test_the_seven_day_total_reads_the_store(self):
        br.save_store({"days": {"2026-10-14": {"saved": 99.0}, "2026-10-19": {"saved": 30.0},
                                "2026-10-20": {"saved": -6.0}}})
        _write("back_of_line", "2026-10-21", [])
        ctx = br.build_report(DAY, with_tape=True)
        # 10/14 is outside the 7 days ending 10/21; 10/21 itself had no moves
        self.assertEqual(ctx["run"]["days"], 3)
        self.assertAlmostEqual(ctx["run"]["saved"], 24.0)
        self.assertAlmostEqual(ctx["run"]["per_day"], 8.0)


class TestMain(_Base):

    def test_dry_sends_nothing_and_writes_no_store(self):
        with mock.patch.object(br, "build_report", return_value={
                "day": DAY, "now": None, "store": {"days": {}},
                "run": {"days": 0, "saved": 0.0, "per_day": 0.0},
                "d": {"day": "2026-10-21", "moves": 0, "markets": 0, "covered": 0, "traded": 0,
                      "mattered": 0, "avoided_ct": 0.0, "saved": 0.0, "losses_avoided": 0.0,
                      "wins_forgone": 0.0, "bol_amends": 0, "writes": 0, "restore_fails": 0,
                      "with_tape": True, "by_family": {}}}), \
                mock.patch.object(imm.Alerter, "send_message") as send:
            self.assertEqual(br.main(["--dry", "--day", "2026-10-21"]), 0)
        send.assert_not_called()
        self.assertFalse(os.path.exists(br.store_path()))

    def test_old_caches_are_pruned(self):
        for d in ("2026-10-10", "2026-10-20"):
            os.makedirs(os.path.join(imm.STATUS_DIR, "bol_report_cache", d))
        br.prune_cache(date(2026, 10, 22))
        left = sorted(os.listdir(os.path.join(imm.STATUS_DIR, "bol_report_cache")))
        self.assertEqual(left, ["2026-10-20"])


if __name__ == "__main__":
    unittest.main()
