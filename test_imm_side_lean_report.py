"""Tests for the side-sizing tests report (imm_side_lean_report.py)."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

import imm_side_lean_report as r

FIELDS = ["rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
          "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled", "cr", "cs", "fx"]
BEFORE, TEST = ("2026-10-19", "2026-10-20"), ("2026-10-23", "2026-10-24")


def ts(day, hour=16):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc).timestamp()


def row(rew=0.0, pnl=0.0, carry=0.0):
    v = [0.0] * len(FIELDS)
    v[FIELDS.index("rew")], v[FIELDS.index("pnl")] = rew, pnl + carry
    v[FIELDS.index("cr")] = carry
    v[FIELDS.index("fx")] = 0
    return v


def dash(ask_test=(-5.0, 5.0), bid_test=(-8.0, 5.0), ask_q=(20.0, 30.0),
         bid_q=(20.0, 10.0), long_test=(-9.0, 2.0), long_q=(10.0, 12.0), n=200, events=6):
    """Four full days (two each side of the 10/22 switch). Families: Rotten
    Tomatoes (ASK lean), Commodities & shipping (BID lean), Elections (the
    long-shot cut), Gas & diesel (the control), TRUMP mentions (left out).
    `ask_test` / `bid_test` = a lean group's test fills' (bid, ask) cents a
    contract; `long_test` = the cut fills' and the other fills' cents in the
    test (Elections: a bid at 20c and an ask at 80c are cut, a bid at 50c and
    an ask at 40c are not); `*_q` = (before, test) quoting net a day per
    market, as rewards; `events` markets a family, n contracts a fill."""
    fams = {"KXRT": "Rotten Tomatoes", "KXSUEZ": "Commodities & shipping",
            "KXSENATE": "Elections", "KXGAS": "Gas & diesel",
            "KXTRUMPMENTION": "TRUMP mentions"}
    markets, days, fills = {}, {}, []
    for ser, fam in fams.items():
        for k in range(events):
            t = f"{ser}-26NOV{10 + k}-X"
            markets[t] = {"ser": ser, "fam": fam, "ev": t.rsplit("-", 1)[0]}
    for d in BEFORE + TEST:
        test = d > r.SWITCH_DAY
        m = {}
        for t, info in markets.items():
            fam = info["fam"]
            if fam == "Elections":
                m[t] = row(rew=long_q[1] if test else long_q[0])
                cut_c, rest_c = long_test if test else (-6.0, 1.0)
                for side, px, c in (("bid", 20.0, cut_c), ("ask", 80.0, cut_c),
                                    ("bid", 50.0, rest_c), ("ask", 40.0, rest_c)):
                    fills.append({"ts": ts(d), "t": t, "side": side, "px": px, "n": n,
                                  "pad": False, "mtc": c})
                continue
            if fam == "Rotten Tomatoes":
                m[t] = row(rew=ask_q[1] if test else ask_q[0])
                bc, ac = ask_test if test else (-6.0, -2.0)
            elif fam == "Commodities & shipping":
                m[t] = row(rew=bid_q[1] if test else bid_q[0])
                bc, ac = bid_test if test else (0.0, -11.0)
            elif fam == "TRUMP mentions":
                m[t] = row(rew=500.0)                  # would swamp the control
                bc, ac = (-50.0, 50.0)
            else:
                m[t] = row(rew=10.0, pnl=-2.0, carry=1.0)
                bc, ac = (-1.0, -1.0)
            fills.append({"ts": ts(d), "t": t, "side": "bid", "px": 40.0, "n": n,
                          "pad": False, "mtc": bc})
            fills.append({"ts": ts(d), "t": t, "side": "ask", "px": 60.0, "n": n,
                          "pad": False, "mtc": ac})
        days[d] = {"m": m}
    # a pad and an unscored fill: the pad never counts, the unscored one
    # counts in contracts but not in cents
    fills.append({"ts": ts(TEST[0]), "t": "KXRT-26NOV10-X", "side": "bid", "px": 1.0,
                  "n": 5000.0, "pad": True, "mtc": -1.0})
    fills.append({"ts": ts(TEST[0]), "t": "KXRT-26NOV10-X", "side": "ask", "px": 60.0,
                  "n": 100.0, "pad": False})
    return {"day_fields": FIELDS, "days": days, "markets": markets, "fills": fills}


class SideLeanReportTests(unittest.TestCase):

    def test_windows_leave_out_the_switch_day_and_today(self):
        D = dash()
        D["days"]["2026-10-22"] = {"m": {}}
        D["days"]["2026-10-25"] = {"m": {}}
        self.assertEqual(r.windows(D, "2026-10-25"), (list(BEFORE), list(TEST)))

    def test_summary_by_group_and_family(self):
        by = r.summarize(dash(), "2026-10-25")["by"]
        # six RT markets: 20 -> 30 $ a day each
        self.assertAlmostEqual(r.quoting(by["ASK"]["before"]), 120.0)
        self.assertAlmostEqual(r.quoting(by["ASK"]["test"]), 180.0)
        self.assertAlmostEqual(r.quoting(by["fam:Rotten Tomatoes"]["test"]), 180.0)
        # the pad is out; the unscored ask counts in contracts, not cents
        self.assertAlmostEqual(r.per_day(by["ASK"]["test"], "bid_ct"), 6 * 200.0)
        self.assertAlmostEqual(r.per_day(by["ASK"]["test"], "ask_ct"), 6 * 200.0 + 50.0)
        self.assertAlmostEqual(r.cents(by["ASK"]["test"], "ask"), 5.0)
        self.assertAlmostEqual(r.cents(by["ASK"]["test"], "bid"), -5.0)
        # the long-shot cut: bids at 20c and asks at 80c are its cut fills
        self.assertAlmostEqual(r.per_day(by["LONG"]["test"], "cut_ct"), 6 * 400.0)
        self.assertAlmostEqual(r.cents(by["LONG"]["test"], "cut"), -9.0)
        self.assertAlmostEqual(r.cents(by["LONG"]["test"], "rest"), 2.0)
        # TRUMP mentions is in nothing; a group's control is the rest of the
        # book: RT's = Commodities (20 x 6) + Elections (10 x 6) + gas (10 x 6)
        self.assertAlmostEqual(r.per_day(r.control(by, "ASK", "before"), "rew"), 240.0)
        self.assertAlmostEqual(r.per_day(r.control(by, "ASK", "before"), "carry"), 6.0)
        self.assertAlmostEqual(r.per_day(by["ALL"]["before"], "rew"), 360.0)
        self.assertNotIn("fam:TRUMP mentions", by)
        self.assertEqual(by["ASK"]["test"]["events"], 6)

    def test_verdicts(self):
        by = r.summarize(dash(), "2026-10-25")["by"]
        v, why = r.verdict(by, "ASK")
        self.assertEqual(v, "KEEP")
        self.assertIn("asks +5.0c vs bids -5.0c a contract", why)
        # BID group: its bids lost more than its asks and its quoting fell
        v, why = r.verdict(by, "BID")
        self.assertEqual(v, "STOP")
        self.assertIn("bids -8.0c vs asks +5.0c", why)
        # the cut fills kept losing and the events' quoting rose: KEEP
        v, why = r.verdict(by, "LONG")
        self.assertEqual(v, "KEEP")
        self.assertIn("the other fills +2.0c vs the cut fills -9.0c", why)
        # the long shots won and the quoting fell: STOP
        by2 = r.summarize(dash(long_test=(4.0, -1.0), long_q=(10.0, 6.0)), "2026-10-25")["by"]
        self.assertEqual(r.verdict(by2, "LONG")[0], "STOP")
        # mixed: the premise holds but the quoting fell behind the control
        by = r.summarize(dash(ask_q=(30.0, 10.0)), "2026-10-25")["by"]
        self.assertEqual(r.verdict(by, "ASK")[0], "WATCH")
        # too few fills to call
        by = r.summarize(dash(n=10), "2026-10-25")["by"]
        v, why = r.verdict(by, "ASK")
        self.assertEqual(v, "WATCH")
        self.assertIn("too few fills", why)
        # no test day yet
        by = r.summarize(dash(), "2026-10-22")["by"]
        self.assertEqual(r.verdict(by, "LONG"), ("WATCH", "no full test day yet"))

    def test_report_and_main(self):
        md = r.report(dash(), "2026-10-25")
        self.assertTrue(md.startswith(
            "# IMM side tests: ASK lean KEEP, BID lean STOP, long-shot cut KEEP\n"))
        self.assertIn("| Rotten Tomatoes | ASK | +120.00 | +180.00 | +5.0c | -5.0c |", md)
        self.assertIn("| Elections | LONG | +60.00 | +72.00 | +2.0c | -9.0c |", md)
        self.assertIn("| long-shot cut: cut fills (bids <30c, asks 70c+) | 2,400 | 2,400 | "
                      "-6.0c | -9.0c |", md)
        self.assertIn("| Company KPIs | BID | n/a |", md)
        md.encode("cp1252")                        # the task console
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "dash.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write("<script>const D = " + json.dumps(dash()) + ";\n</script>")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(r.main(["--dash", path, "--out-dir", tmp,
                                     "--today", "2026-10-25"]), 0)
        out = buf.getvalue().strip()
        self.assertTrue(out.endswith("side_lean_report_2026-10-25.md"))
        with open(out, encoding="utf-8") as f:
            self.assertTrue(f.readline().startswith("# IMM side tests: ASK lean KEEP"))


if __name__ == "__main__":
    unittest.main()
