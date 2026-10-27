"""Tests for the Trump mentions B test report (imm_trumpb_test_report.py)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import imm_trumpb_test_report as r

B, M = "KXTRUMPMENTIONB", "KXTRUMPMENTION"


def ts(day, hour=16):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc).timestamp()


def dash(b_test_rew=10.0, b_test_own=0.0, ask_c=5.0, n=40, events=3):
    """Two full days each side of the 10/22 switch; three B and three main
    markets (one event each); fills of n contracts a market and side."""
    markets, days, fills = {}, {}, []
    for ser in (B, M):
        for k in range(3):
            t = f"{ser}-26OCT2{7 + k}-W"
            markets[t] = {"ser": ser, "ev": t.rsplit("-", 1)[0]}
    for d in ("2026-10-19", "2026-10-20", "2026-10-23", "2026-10-24"):
        test = d > r.SWITCH_DAY
        rows = {}
        for t, m in markets.items():
            if m["ser"] == B:
                rew, own = (b_test_rew, b_test_own) if test else (10.0, -10.0)
            else:
                rew, own = 20.0, 0.0
            rows[t] = [rew / 3, own / 3, 0.0, 0.0]
        days[d] = {"m": rows}
        for t, m in list(markets.items())[:events] if test else markets.items():
            if m["ser"] != B:
                continue
            fills.append({"ts": ts(d), "t": t, "side": "bid", "n": n, "mtc": -10.0})
            fills.append({"ts": ts(d), "t": t, "side": "ask", "n": n, "mtc": ask_c if test else 8.0})
    return {"day_fields": ["rew", "pnl", "cr", "cs"], "markets": markets, "days": days,
            "fills": fills}


class TrumpBReportTests(unittest.TestCase):
    TODAY = "2026-10-25"

    def test_windows_leave_out_the_switch_day_and_today(self):
        D = dash()
        D["days"]["2026-10-22"] = {"m": {}}
        D["days"]["2026-10-25"] = {"m": {}}
        self.assertEqual(r.windows(D, self.TODAY),
                         (["2026-10-19", "2026-10-20"], ["2026-10-23", "2026-10-24"]))

    def test_the_test_ends_when_both_series_are_sized_alike(self):
        # Jack 2026-10-27: the main series' bids halved ("do 1 and 3"), B's
        # asks back to 1x ("Trump mentions B -- 1x asks instead of 1.5")
        D = dash()
        for d in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-11-04"):
            D["days"][d] = {"m": {}}
        self.assertEqual(r.END_DAY, "2026-10-27")
        self.assertEqual(r.windows(D, "2026-11-05")[1],
                         ["2026-10-23", "2026-10-24", "2026-10-26"])
        # a fill on or after END_DAY is no test fill
        D["fills"].append({"ts": ts("2026-10-28"), "t": f"{B}-26OCT27-W", "side": "ask",
                           "n": 1000, "mtc": -90.0})
        by = r.summarize(D, "2026-11-05")["by"]
        self.assertEqual(by[B]["test"]["ask_ct"], 2 * 3 * 40)            # the fixture's two days

    def test_keep(self):
        # B's quoting net 0 -> +10 a day, the main series flat; asks +5c
        v, why = r.verdict(r.summarize(dash(), self.TODAY)["by"])
        self.assertEqual(v, "KEEP", why)
        self.assertIn("+10.00 $/day", why)

    def test_stop(self):
        v, why = r.verdict(r.summarize(dash(b_test_rew=5.0, b_test_own=-15.0, ask_c=-2.0),
                                       self.TODAY)["by"])
        self.assertEqual(v, "STOP", why)

    def test_mixed_and_thin_are_watch(self):
        v, _ = r.verdict(r.summarize(dash(ask_c=-1.0), self.TODAY)["by"])
        self.assertEqual(v, "WATCH")
        v, why = r.verdict(r.summarize(dash(n=10), self.TODAY)["by"])
        self.assertEqual(v, "WATCH")
        self.assertIn("too few B fills", why)

    def test_the_report_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        page = os.path.join(d, "dash.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write("<script>const D = " + json.dumps(dash()) + ";\n</script>")
        self.assertEqual(r.main(["--dash", page, "--out-dir", d, "--today", self.TODAY]), 0)
        with open(os.path.join(d, f"trumpb_test_report_{self.TODAY}.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertTrue(md.startswith("# Trump mentions B test: KEEP"))
        self.assertIn("| **quoting net (rewards + own fills)** | **+0.00** | **+10.00** | "
                      "**+20.00** | **+20.00** |", md)
        self.assertIn("| asks: c a contract to settlement | +8.0c | +5.0c |", md)


if __name__ == "__main__":
    unittest.main()
