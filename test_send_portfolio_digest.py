"""Tests for the portfolio digest's "biggest movers" section (Jack
2026-09-22: "include the position families that moved the most (even if
unrealized) since the last daily update") and the family grouping it leans on.

Rows are what build_portfolio() emits per event: day == realized + value_d."""
import unittest

import send_portfolio_digest as pf


def _row(event, realized=0.0, value_d=0.0, value_now=0.0, note=""):
    return {"event": event, "day": round(realized + value_d, 2),
            "realized": realized, "value_d": value_d,
            "value_now": value_now, "note": note}


ROWS = [
    # crypto monthly: a big UNREALIZED loss across two events, nothing settled
    _row("KXBTCMAXMON-BTC-26SEP30", value_d=-300.0, value_now=900.0),
    _row("KXETHMINMON-ETH-26SEP30", realized=11.37, value_d=-144.23, value_now=400.0),
    # high temps: realized loss on settlements, bigger unrealized gain elsewhere
    _row("KXHIGHNY-26SEP21", realized=-91.91, note="settled yes"),
    _row("KXHIGHAUS-26SEP23", value_d=201.24, value_now=350.0),
    # a lone unrecognised series with a pure mark move, and a new event
    _row("KXLARGECUT-26", value_d=60.0, value_now=291.0),
    _row("KXGOOG-26NOVHEAD", value_d=5.0, value_now=50.0, note="new"),
    # two NFL series that should roll up together
    _row("KXNFLTD-26SEP21-A", realized=82.0, note="settled yes"),
    _row("KXNFLSPREAD-26SEP21-B", realized=-48.56, note="settled no"),
]


class FamilyMoversTests(unittest.TestCase):
    def test_ranked_by_size_of_move_with_exact_split(self):
        shown, tot, (hidden_n, hidden_net) = pf.family_movers(ROWS, top_n=10)
        names = [n for n, _ in shown]
        self.assertEqual(names[0], "Crypto monthly touch")       # |-432.86| biggest
        self.assertEqual(names[1], "High temps (KXHIGH*)")      # |+109.33|
        crypto = shown[0][1]
        self.assertAlmostEqual(crypto["day"], -432.86, places=2)
        self.assertAlmostEqual(crypto["realized"], 11.37, places=2)
        self.assertAlmostEqual(crypto["unreal"], -444.23, places=2)
        self.assertAlmostEqual(crypto["value_now"], 1300.0, places=2)
        self.assertEqual(len(crypto["rows"]), 2)
        temps = shown[1][1]
        self.assertAlmostEqual(temps["day"], 109.33, places=2)
        self.assertAlmostEqual(temps["realized"], -91.91, places=2)
        self.assertAlmostEqual(temps["unreal"], 201.24, places=2)
        # per-family split is exact and the whole thing adds up
        for _, g in shown:
            self.assertAlmostEqual(g["day"], g["realized"] + g["unreal"], places=2)
        self.assertAlmostEqual(tot["day"], sum(r["day"] for r in ROWS), places=2)
        self.assertAlmostEqual(tot["day"], tot["realized"] + tot["unreal"], places=2)
        self.assertEqual(tot["n_events"], len(ROWS))
        self.assertEqual(hidden_n, 0)
        self.assertAlmostEqual(hidden_net, 0.0)

    def test_top_events_within_a_family_by_size_of_move(self):
        shown, _, _ = pf.family_movers(ROWS, per_family=1)
        crypto = dict(shown)["Crypto monthly touch"]
        self.assertEqual([r["event"] for r in crypto["top"]], ["KXBTCMAXMON-BTC-26SEP30"])

    def test_hidden_tail_reconciles_to_the_total(self):
        shown, tot, (hidden_n, hidden_net) = pf.family_movers(ROWS, top_n=2)
        self.assertEqual(len(shown), 2)
        self.assertEqual(hidden_n, tot["n_families"] - 2)
        self.assertAlmostEqual(sum(g["day"] for _, g in shown) + hidden_net,
                               tot["day"], places=2)

    def test_mover_tags(self):
        self.assertEqual(pf._mover_tag(_row("X", value_d=3.0)), "mark")
        self.assertEqual(pf._mover_tag(_row("X", realized=3.0)), "realized")
        self.assertEqual(pf._mover_tag(_row("X", realized=3.0, value_d=-1.0)), "partly realized")
        self.assertEqual(pf._mover_tag(_row("X", realized=3.0, note="settled yes")), "settled yes")
        # a strike settled but the open legs' mark drove the move: say so
        self.assertEqual(pf._mover_tag(_row("X", realized=3.0, value_d=-40.0, note="settled yes")),
                         "mostly mark (settled yes)")
        self.assertEqual(pf._mover_tag(_row("X", value_d=1.0, note="new")), "new")

    def test_default_shows_25_families(self):
        # Jack 2026-09-22: "show up to 25 families, not 10"
        rows = [_row(f"KXSERIES{i:02d}-26DEC31", value_d=float(40 - i)) for i in range(40)]
        shown, tot, (hidden_n, _) = pf.family_movers(rows)
        self.assertEqual(len(shown), 25)
        self.assertEqual(hidden_n, 15)
        self.assertEqual(tot["n_families"], 40)

    def test_empty(self):
        shown, tot, hidden = pf.family_movers([])
        self.assertEqual(shown, [])
        self.assertEqual(tot["n_families"], 0)
        self.assertEqual(hidden, (0, 0.0))


class FamilyRulesTests(unittest.TestCase):
    def test_new_families_roll_up(self):
        self.assertEqual(pf.family_for("KXNFLTD-26SEP21-A"), "NFL (KXNFL*)")
        self.assertEqual(pf.family_for("KXNFLSPREAD-26SEP21-B"), "NFL (KXNFL*)")
        self.assertEqual(pf.family_for("KXRT-PRI"), "Rotten Tomatoes (KXRT)")
        self.assertEqual(pf.family_for("KXANFCC-26OCT07"), "Carbon Arc cards (KX*CC)")
        self.assertEqual(pf.family_for("KXDKNGAPP-26OCT08"), "App downloads (KX*APP)")
        # Netflix is not football: the NFL prefix must not swallow KXNFLX*
        self.assertEqual(pf.family_for("KXNFLXAPP-26OCT08"), "App downloads (KX*APP)")
        # earlier rules still win where they overlap
        self.assertEqual(pf.family_for("KXEARNINGSMENTIONCOST-26SEP24"), "Mention markets")
        self.assertEqual(pf.family_for("KXBTCMAXMON-BTC-26SEP30"), "Crypto monthly touch")
        # unknown series still shows as itself
        self.assertEqual(pf.family_for("KXLARGECUT-26"), "KXLARGECUT")


class BuildEmailTests(unittest.TestCase):
    def _pf(self, first=False):
        return {"today": "2026-09-22", "rows": list(ROWS), "first_run": first,
                "prior": None if first else {"equity_kalshi": 23000.0},
                "equity_kalshi": 22500.0, "cash": 10000.0,
                "kalshi_positions_value": 12500.0, "equity": 22900.0,
                "positions_value": 12900.0}

    def test_section_present_in_text_and_html(self):
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False)
        self.assertIn("Biggest movers since yesterday, by family", text)
        self.assertIn("Biggest movers since yesterday, by family", html)
        # ranked: crypto first, temps second, in both parts
        self.assertLess(text.index("Crypto monthly touch"), text.index("High temps (KXHIGH*)"))
        self.assertLess(html.index("Crypto monthly touch"), html.index("High temps (KXHIGH*)"))
        # the split and the total row
        self.assertIn("-444.23", text)
        self.assertIn("ALL FAMILIES", text)
        self.assertIn("ALL FAMILIES", html)
        # tags explain the move
        self.assertIn("mark", text)
        self.assertIn("settled yes", html)
        # residual line reconciles to the account-value change: -500.00 on
        # Kalshi's valuation vs -225.09 of trading in the table -> -274.91
        self.assertIn("account value moved -500.00", text)
        self.assertIn("-274.91", text)
        # the settled table is still there, after the movers
        self.assertLess(html.index("Biggest movers"), html.index("Settled since yesterday, by series"))

    def test_first_run_has_no_residual_line(self):
        _, text, html = pf.build_email(self._pf(first=True), [], chart_ok=False)
        self.assertIn("Biggest movers", text)
        self.assertNotIn("account value moved", text)
        self.assertNotIn("Account value moved", html)

    def test_nothing_moved(self):
        p = self._pf()
        p["rows"] = []
        _, text, html = pf.build_email(p, [], chart_ok=False)
        self.assertIn("nothing moved since the prior morning", text)
        self.assertIn("Nothing moved since the prior morning", html)


if __name__ == "__main__":
    unittest.main()
