"""Tests for imm_live_tests.py (the portfolio email's Live A/B tests block;
Jack 2026-11-05: "add section on live A/B and results right after the risk
controls. make it clear and succinct and visually easy to digest")."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import imm_live_tests as lt

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)      # the 7:00 ET run
T_1104 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc).timestamp()
FAKE_BOT = SimpleNamespace(TAPE_HOLD_SECS=60.0, TAPE_AB_MIN_CT=100.0)


def tape_ctx(**kw):
    """imm_tape_ab_report.build's result as on 11/05 (scored through 11/04)."""
    ctx = {"wins": [{"at": T_1104}], "first": date(2026, 11, 2), "n_days": 3,
           "p": [None] * 1067, "c": [None] * 1051, "verdict": "RUNNING",
           "eff": {"5m": 12.37, "4h": 34.03},
           "ci": {"5m": (-17.86, 42.63), "4h": (-4.42, 72.57)}, "short": 51.43,
           "daily": [{"day": "2026-11-04", "pause": 700, "control": 679,
                      "forgone": 9.4, "impact_4h": 8.88, "impact_5m": -0.66,
                      "all_on_4h": 17.5, "so_far_4h": 51.43}]}
    ctx.update(kw)
    return ctx


def tape(ctx):
    with mock.patch.dict(sys.modules, {"incentive_mm": FAKE_BOT}):
        return lt.tape_card("unused", NOW, ctx=ctx)


START = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc).timestamp()


def size_sc(n=10, net=44.6, se=149.2, stop_avg=277.3):
    days = [(date(2026, 10, 26) + timedelta(days=i)).isoformat() for i in range(n)]
    return {"test_days": days, "base_days": ["b"] * 14, "n_days": 14,
            "net": {"mean": net, "se": se, "daily": [0.0] * (n - 1) + [795.0]},
            "rew": {"mean": -22.1, "se": 50.2}, "pnl": {"mean": 66.7, "se": 175.2},
            "test": {"series": 216}, "control": {"series": 203}, "stop_avg": stop_avg}


def size(sc, now=NOW, status_dir="unused"):
    """size_card with imm_size_test_report (and the bot under it) faked."""
    bot = SimpleNamespace(size_test_window=lambda: (START, END),
                          ET=timezone(timedelta(hours=-5)),
                          SIZE_TEST_FROM=date(2026, 10, 26),
                          SIZE_TEST_STOP_FILE="size_test_stop.txt", SIZE_TEST_MULT=1.5)
    score = mock.Mock(return_value=sc)
    st = SimpleNamespace(imm=bot, score=score, STOP_USD=400.0,
                         verdict=lambda s: ("KEEP" if s["net"]["mean"] - s["net"]["se"] > 0
                                            else "STOP" if s["net"]["mean"] + s["net"]["se"] < 0
                                            else "WATCH"))
    with mock.patch.dict(sys.modules, {"imm_size_test_report": st}):
        return lt.size_card(status_dir, now, D={"days": {}}), score


class TapeCardTests(unittest.TestCase):
    def test_reads_the_ab_through_yesterday(self):
        c = tape(tape_ctx())
        self.assertEqual((c["status"], c["day"], c["of"], c["ends"]),
                         ("RUNNING", 3, 21, date(2026, 11, 22)))
        self.assertEqual((c["est"], c["lo"], c["hi"]), (34.03, -4.42, 72.57))
        self.assertIn("60 s after a 100+ lot", c["what"])
        self.assertIn("1,067 paused, 1,051 controls", c["what"])
        vals = [(f["label"], f["value"]) for f in c["facts"]]
        self.assertEqual(vals, [("Yesterday", "+$9"), ("So far", "+$51"),
                                ("5 min check", "+$12/day")])
        self.assertEqual(c["facts"][0]["sub"], "700 paused / 679 controls")
        self.assertEqual(c["facts"][1]["bar"], 0.0)          # ahead: no way to the stop
        self.assertIn("GO needs day 21 (Sun 11/22)", c["next"])

    def test_hidden_without_a_recent_window(self):
        self.assertIsNone(tape(tape_ctx(wins=[])))
        old = (NOW - timedelta(days=9)).timestamp()
        self.assertIsNone(tape(tape_ctx(wins=[{"at": old}])))

    def test_verdict_shows_three_mornings_then_goes(self):
        c = tape(tape_ctx(n_days=23, verdict="GO"))
        self.assertEqual(c["status"], "GO")
        self.assertIn("IMM_TAPE_AB_FRAC=1", c["next"])
        self.assertIsNone(tape(tape_ctx(n_days=25, verdict="GO")))

    def test_the_way_to_the_stop_line(self):
        c = tape(tape_ctx(short=-90.0))
        self.assertAlmostEqual(c["stop_frac"], 0.6)
        words, col = lt.summary([c])
        self.assertEqual(col, lt.AMBER)
        self.assertIn("near the stop line: Tape trigger A/B", words)
        words, col = lt.summary([tape(tape_ctx(short=-160.0, verdict="STOP"))])
        self.assertEqual((col, words), (lt.RED, "1 test; Tape trigger A/B STOP"))

    def test_a_day_without_windows(self):
        ctx = tape_ctx()
        ctx["daily"][-1].update(pause=0, control=0, impact_4h=None)
        self.assertEqual(tape(ctx)["facts"][0]["value"], "none")


class SizeCardTests(unittest.TestCase):
    def test_reads_the_split_test(self):
        c, score = size(size_sc())
        score.assert_called_once()
        self.assertEqual(score.call_args.args[1], date(2026, 11, 5))   # ET today
        self.assertEqual((c["status"], c["day"], c["of"], c["ends"]),
                         ("RUNNING", 10, 14, date(2026, 11, 8)))
        self.assertAlmostEqual(c["lo"], 44.6 - 149.2)
        self.assertAlmostEqual(c["hi"], 44.6 + 149.2)
        self.assertEqual([(f["label"], f["value"]) for f in c["facts"]],
                         [("Yesterday", "+$795"), ("Last 3 days", "+$277/day"),
                          ("Rewards / fills", "-$22 / +$67")])
        self.assertIn("(216) vs the other half (203)", c["what"])
        self.assertIn("It reads WATCH today", c["next"])
        self.assertIn("Sun 11/8", c["next"])

    def test_final_day_takes_the_rules_verdict(self):
        c, _ = size(size_sc(n=14, net=200.0, se=100.0),
                    now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(c["status"], "KEEP")
        self.assertIn("x1.5 paid", c["next"])

    def test_the_stop_file_reads_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "size_test_stop.txt"), "w").close()
            c, _ = size(size_sc(stop_avg=-450.0), status_dir=d)
        self.assertEqual(c["status"], "STOPPED")
        self.assertGreater(c["stop_frac"], 1.0)

    def test_outside_its_window_it_is_not_scored(self):
        for now in (datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),   # day 1 not done
                    datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)):   # 3 days past
            c, score = size(size_sc(), now=now)
            self.assertIsNone(c)
            score.assert_not_called()


class RenderTests(unittest.TestCase):
    def test_range_bar_widths_and_colours(self):
        for lo, est, hi in ((-4.42, 34.03, 72.57), (5.0, 30.0, 60.0), (-90.0, -40.0, -2.0),
                            (-104.6, 44.6, 193.8)):
            runs = lt.range_runs(lo, est, hi, width=140)
            self.assertEqual(sum(w for w, _ in runs), 140)
            cols = {col for _, col in runs}
            self.assertEqual(lt.GREEN in cols, lo > 0)        # a clear win reads green
            self.assertEqual(lt.RED in cols, hi < 0)          # a clear loss reads red
            if lo < 0 < hi:
                self.assertIn("#444", cols)
                self.assertIn("#666", cols)                   # the $0 tick, in range

    def test_the_result_is_coloured_only_when_its_range_clears_zero(self):
        straddle = lt.html([tape(tape_ctx())])
        self.assertIn(f'color:{lt.INK}">+$34', straddle)
        self.assertIn("can't tell yet", straddle)
        clear = lt.html([tape(tape_ctx(ci={"5m": (1.0, 2.0), "4h": (3.0, 60.0)}))])
        self.assertIn(f'color:{lt.GREEN}">+$34', clear)
        self.assertIn("pausing ahead", clear)

    def test_text_block(self):
        L = lt.text_lines([tape(tape_ctx()), size(size_sc())[0]])
        self.assertEqual(L[0], "LIVE A/B TESTS -- results through yesterday; 2 running, "
                               "none near its stop line")
        txt = "\n".join(L)
        txt.encode("ascii")                                   # the cp1252 task console
        self.assertIn("* Tape trigger A/B: RUNNING, day 3 of 21 (ends Sun 11/22)", txt)
        self.assertIn("range -$4 to +$73 (90%): can't tell yet", txt)
        self.assertIn("* Base size x1.5 split test: RUNNING, day 10 of 14", txt)
        self.assertLess(txt.index("Tape trigger"), txt.index("Base size"))

    def test_nothing_running_is_no_block(self):
        self.assertEqual(lt.text_lines([]), [])
        self.assertEqual(lt.html([]), "")

    def test_usd(self):
        self.assertEqual([lt.usd(x) for x in (34.03, -4.42, 0.3, 1234.6, None)],
                         ["+$34", "-$4", "$0", "+$1,235", "n/a"])


class CollectTests(unittest.TestCase):
    def test_a_failing_test_is_one_line_not_a_lost_block(self):
        with mock.patch.object(lt, "size_card", side_effect=ValueError("bad D")), \
                mock.patch.object(lt, "tape_card", side_effect=OSError("no sink")):
            cards = lt.collect("unused", NOW)
        self.assertEqual([c["name"] for c in cards], ["Tape trigger A/B", "Base size split test"])
        txt = "\n".join(lt.text_lines(cards))
        self.assertIn("Tape trigger A/B: not available (OSError: no sink)", txt)
        self.assertIn("not available (ValueError: bad D)", lt.html(cards))

    def test_tests_not_running_leave_no_card(self):
        with mock.patch.object(lt, "size_card", return_value=None), \
                mock.patch.object(lt, "tape_card", return_value=None):
            self.assertEqual(lt.collect("unused", NOW), [])


if __name__ == "__main__":
    unittest.main()
