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
        # the settled table is gone (Jack 2026-09-29); settled events stay in
        # the movers table, tagged
        self.assertNotIn("Settled since yesterday", text)
        self.assertNotIn("Settled since yesterday", html)
        self.assertNotIn("ALL SETTLED", html)

    def test_day_change_splits_exactly_with_the_replay(self):
        # trading -225.09 (the table) + reward credits +100.00 + Kalshi's
        # pricing vs mid = the -500.00 move on Kalshi's valuation
        p = self._pf()
        p["no_trade_cash"], p["net_transfers"] = 100.0, 0.0
        subject, text, html = pf.build_email(p, [], chart_ok=False)
        self.assertIn("vs yesterday: -500.00  =  trading (at mid) -225.09  +  "
                      "reward credits +100.00  +  Kalshi's pricing vs mid -374.91", text)
        self.assertIn("(account value moved -500.00 = this table -225.09  +  "
                      "reward credits +100.00  +  Kalshi's pricing vs mid -374.91)", text)
        self.assertIn("trading -225.09", subject)
        self.assertNotIn("settled", subject)
        self.assertIn("reward credits", html)
        # a deposit is split out of the no-trade cash
        p["net_transfers"] = 250.0
        _, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("reward credits -150.00  +  deposits/withdrawals +250.00  +  "
                      "Kalshi's pricing vs mid -374.91", text)
        # transfers unreadable: one lumped line, still exact
        p["net_transfers"] = None
        _, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("reward credits & deposits +100.00  +  Kalshi's pricing vs mid -374.91", text)

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


def _fill(ts, tk, book_side, n, yes_px, fee=0.0):
    return {"ts": ts, "ticker": tk, "book_side": book_side,
            "count_fp": f"{n:.2f}", "yes_price_dollars": f"{yes_px:.4f}",
            "fee_cost": f"{fee:.6f}"}


class ReplayDayTests(unittest.TestCase):
    """Jack 2026-09-29: "double confirm that the 'Biggest movers since
    yesterday, by family' is accurate. i dont trust it". The old event-rollup
    diff double-counted events traded both ways that then settled (9/29: RT
    -256.60 shown, -9.02 real). The replay starts from the prior snapshot's
    positions and marks and books only what happened since."""

    EV = staticmethod(lambda tk: tk.rsplit("-", 1)[0])

    def test_two_way_traded_market_that_settles(self):
        # 10 YES held at a 30c mark (worth 3.00); sold at 40c (+1.00 vs the
        # mark), then 20 NO opened at 65c, 5 of them closed at 80c (+0.75),
        # the last 15 NO pay $1 at a NO settlement (+5.25): +7.00, which is
        # exactly the cash (4 - 13 + 4 + 15 = 10) minus the 3.00 it was worth
        start = {"KXRT-HEA-80": (10.0, 3.0)}
        fills = [_fill(1, "KXRT-HEA-80", "ask", 10, 0.40),
                 _fill(2, "KXRT-HEA-80", "ask", 20, 0.35),
                 _fill(3, "KXRT-HEA-80", "bid", 5, 0.20)]
        setts = [{"ticker": "KXRT-HEA-80", "revenue": 1500, "fee_cost": "0",
                  "market_result": "no", "settled_time": "1970-01-01T00:00:04Z"}]
        evs, cash, mism, settled = pf.replay_day(start, fills, setts, {}, self.EV)
        self.assertAlmostEqual(cash, 10.0)
        self.assertAlmostEqual(evs["KXRT-HEA"]["realized"], 7.0)
        self.assertAlmostEqual(evs["KXRT-HEA"]["value_d"], 0.0)
        self.assertEqual(mism, [])
        self.assertEqual(settled, {"KXRT-HEA": {"no"}})

    def test_held_position_marks_from_its_prior_value(self):
        # 5 NO held at cost 2.50 (no mark), now marked at a 40c YES mid:
        # 5 x 60c = 3.00, so +0.50 unrealized and nothing realized
        evs, cash, mism, _ = pf.replay_day({"KXA-26-T1": (-5.0, 2.5)}, [], [],
                                           {"KXA-26-T1": (-5.0, 3.0)}, self.EV)
        self.assertAlmostEqual(evs["KXA-26"]["realized"], 0.0)
        self.assertAlmostEqual(evs["KXA-26"]["value_d"], 0.5)
        self.assertAlmostEqual(cash, 0.0)
        self.assertEqual(mism, [])

    def test_new_position_with_a_fee(self):
        # 10 YES bought at 50c with a 7c fee, marked at 55c: +0.50 on the
        # mark, -0.07 realized fee, cash -5.07
        evs, cash, _, _ = pf.replay_day(
            {}, [_fill(1, "KXB-26-T2", "bid", 10, 0.50, fee=0.07)], [],
            {"KXB-26-T2": (10.0, 5.5)}, self.EV)
        self.assertAlmostEqual(cash, -5.07)
        self.assertAlmostEqual(evs["KXB-26"]["realized"], -0.07)
        self.assertAlmostEqual(evs["KXB-26"]["value_d"], 0.5)

    def test_yes_bid_against_held_no_nets_pairs_for_a_dollar(self):
        # 20 NO held (basis 5.80); a 20-lot YES bid fill at 71c closes them:
        # cash +20 x 29c = +5.80 (the live 9/28 balance move), realized 0
        evs, cash, mism, _ = pf.replay_day(
            {"KXT-26-IRAN": (-20.0, 5.8)},
            [_fill(1, "KXT-26-IRAN", "bid", 20, 0.71)], [], {}, self.EV)
        self.assertAlmostEqual(cash, 5.8)
        self.assertAlmostEqual(evs["KXT-26"]["realized"], 0.0)
        self.assertEqual(mism, [])

    def test_disagreement_with_the_positions_endpoint_is_reported(self):
        _, _, mism, _ = pf.replay_day({"KXC-26-T3": (5.0, 1.0)}, [], [],
                                      {"KXC-26-T3": (7.0, 1.4)}, self.EV)
        self.assertEqual(mism, ["KXC-26-T3"])

    def test_side_value(self):
        self.assertAlmostEqual(pf.side_value(10.0, 0.3, 9.0), 3.0)
        self.assertAlmostEqual(pf.side_value(-10.0, 0.3, 9.0), 7.0)
        self.assertAlmostEqual(pf.side_value(-10.0, None, 9.0), 9.0)


class _FakeMarginClient:
    def __init__(self, fail=False):
        self.fail = fail

    def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("HTTP 503")
        if path == "/margin/balance":
            return {"settled_funds": "99.1490", "subaccount_balances": [
                {"subaccount": 64, "account_equity": "99.8510", "position_value": "251.4120"},
                {"subaccount": 0, "account_equity": "25.0943", "position_value": "0.0000"}]}
        if path == "/margin/positions":
            return {"positions": [{"market_ticker": "KXBTCPERP", "position": "30.00",
                                   "unrealized_pnl": "54.6570"}]}
        raise AssertionError(path)


class PerpsAndRewardsTests(unittest.TestCase):
    """Jack 2026-09-28: "include the value of perps as well in portfolio
    value ... also include an estimated portfolio value after earnings are
    paid out in parenthesis"."""

    def test_perps_value_is_summed_subaccount_equity(self):
        p = pf.fetch_perps(_FakeMarginClient())
        self.assertAlmostEqual(p["equity"], 124.95)
        self.assertEqual(p["positions"], [{"ticker": "KXBTCPERP", "position": 30.0,
                                           "unrealized": 54.66}])

    def test_perps_read_failure_is_none(self):
        self.assertIsNone(pf.fetch_perps(_FakeMarginClient(fail=True)))

    def _pf(self, prior_perps=100.0, unpaid=1829.4, stale=False):
        prior = {"equity_kalshi": 23000.0}
        if prior_perps is not None:
            prior["perps_equity"] = prior_perps
        return {"today": "2026-09-28", "rows": list(ROWS), "first_run": False,
                "prior": prior, "equity_kalshi": 22500.0, "cash": 10000.0,
                "kalshi_positions_value": 12500.0, "equity": 22900.0,
                "positions_value": 12900.0, "perps_equity": 124.95,
                "perps_stale": stale, "account_value": 22624.95,
                "perps": {"equity": 124.95, "positions": [
                    {"ticker": "KXBTCPERP", "position": 30.0, "unrealized": 54.66}]},
                "unpaid": (None if unpaid is None else
                           {"total": unpaid, "raw": 1577.1, "market_periods": 412,
                            "markets": 390, "since": "2026-09-27T04:00:00+00:00"})}

    def test_headline_has_perps_and_the_after_rewards_estimate(self):
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False)
        self.assertIn("Account value $22,624.95  (est. $24,454.35 after rewards are paid out)", text)
        self.assertIn("(est. $24,454.35 after rewards are paid out)", html)
        self.assertIn("perpetuals $124.95 (KXBTCPERP +30, unrealized +54.66)", text)
        self.assertIn("412 program periods", text)
        # the day change carries the perps move: -500 on event contracts plus
        # +24.95 on perps
        self.assertIn("vs yesterday: -475.05", text)
        self.assertIn("perpetuals +24.95", text)
        self.assertIn("day -475.05", subject)
        # the movers residual stays on event contracts, where the table lives
        self.assertIn("account value ex-perpetuals moved -500.00", text)
        self.assertIn("-274.91", text)

    def test_first_morning_with_perps_keeps_the_day_change_like_for_like(self):
        _, text, _ = pf.build_email(self._pf(prior_perps=None), [], chart_ok=False)
        self.assertIn("vs yesterday: -500.00", text)
        self.assertIn("first counted today, so not in the day change", text)

    def test_stale_perps_are_flagged(self):
        _, text, _ = pf.build_email(self._pf(stale=True), [], chart_ok=False)
        self.assertIn("yesterday's value: today's read failed", text)

    def test_no_estimate_no_parenthesis(self):
        _, text, html = pf.build_email(self._pf(unpaid=None), [], chart_ok=False)
        self.assertNotIn("after rewards are paid out", text)
        self.assertNotIn("after rewards are paid out", html)
        self.assertIn("Account value $22,624.95", text)

    def test_history_round_trips_the_new_columns(self):
        import os
        import tempfile
        rows = pf.upsert_history([], "2026-09-28", 10000.0, 12900.0, 22900.0,
                                 12500.0, 124.95, 1829.4)
        tmp = tempfile.mkdtemp()
        old = (pf.DATA_DIR, pf.HISTORY_CSV)
        pf.DATA_DIR, pf.HISTORY_CSV = tmp, os.path.join(tmp, "h.csv")
        try:
            pf.write_history(rows)
            back = pf.load_history()
        finally:
            pf.DATA_DIR, pf.HISTORY_CSV = old
        self.assertEqual(back[0]["perps_equity"], 124.95)
        self.assertEqual(back[0]["unpaid_rewards_est"], 1829.4)


if __name__ == "__main__":
    unittest.main()
