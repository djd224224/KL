"""Tests for the portfolio digest's "biggest movers" section (Jack
2026-09-22: "include the position families that moved the most (even if
unrealized) since the last daily update") and the family grouping it leans on.

Rows are what build_portfolio() emits per event: day == realized + value_d."""
import unittest

import send_portfolio_digest as pf

# families must not depend on the live bot's state file
pf._SERIES_CATS = {}


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
        self.assertEqual(names[0], "Crypto")                    # |-432.86| biggest
        self.assertEqual(names[1], "Weather & quakes")          # |+109.33|
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
        crypto = dict(shown)["Crypto"]
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

    def test_default_shows_every_dashboard_family(self):
        # Jack 2026-09-22: "show up to 25 families, not 10"; the dashboard's
        # taxonomy has fewer than that, so every family shows
        shown, tot, (hidden_n, _) = pf.family_movers(ROWS)
        self.assertEqual(len(shown), tot["n_families"])
        self.assertEqual(hidden_n, 0)
        # unknown series share the dashboard's "Other prints" family
        rows = [_row(f"KXSERIES{i:02d}-26DEC31", value_d=float(40 - i)) for i in range(40)]
        shown, tot, _ = pf.family_movers(rows)
        self.assertEqual([n for n, _ in shown], ["Other prints"])
        self.assertEqual(len(shown[0][1]["rows"]), 40)

    def test_empty(self):
        shown, tot, hidden = pf.family_movers([])
        self.assertEqual(shown, [])
        self.assertEqual(tot["n_families"], 0)
        self.assertEqual(hidden, (0, 0.0))


class FamilyRulesTests(unittest.TestCase):
    """Jack 2026-10-03: "mirror the family/event/market in the table in
    imm_dashboard.html#drivers when grouping families in the table in the
    email e.g. AI & tech, Company KPIs, Crypto, Elections"."""

    def test_families_are_the_dashboards(self):
        import imm_dashboard
        for ev in ("KXNFLTD-26SEP21-A", "KXRT-PRI", "KXANFCC-26OCT07", "KXDKNGAPP-26OCT08",
                   "KXNFLXAPP-26OCT08", "KXEARNINGSMENTIONCOST-26SEP24",
                   "KXBTCMAXMON-BTC-26SEP30", "KXCPIYOY-26NOV", "KXVOTEGENERAL-GOVAK-26JKRE",
                   "KXOPENSHARE-26OCT05", "KXHOOD-26NOVFUNDED", "KXLARGECUT-26"):
            series = ev.split("-", 1)[0]
            self.assertEqual(pf.family_and_group(ev), imm_dashboard.family_of(series), ev)
        self.assertEqual(pf.family_for("KXCPIYOY-26NOV"), "Econ & rates")
        self.assertEqual(pf.family_for("KXBTCMAXMON-BTC-26SEP30"), "Crypto")
        self.assertEqual(pf.family_for("KXVOTEGENERAL-GOVAK-26JKRE"), "Elections")
        self.assertEqual(pf.family_for("KXOPENSHARE-26OCT05"), "AI & tech")
        self.assertEqual(pf.family_for("KXHOOD-26NOVFUNDED"), "Company KPIs")
        # Netflix is not football, as on the dashboard
        self.assertEqual(pf.family_for("KXNFLXAPP-26OCT08"), "Carbon Arc consumer")
        self.assertEqual(pf.family_for("KXLARGECUT-26"), "Other prints")

    def test_kalshi_category_places_an_unknown_series(self):
        saved = pf._SERIES_CATS
        try:
            pf._SERIES_CATS = {"KXLARGECUT": "Economics"}
            self.assertEqual(pf.family_for("KXLARGECUT-26"), "Econ & rates")
        finally:
            pf._SERIES_CATS = saved


class BuildEmailTests(unittest.TestCase):
    def _pf(self, first=False):
        return {"today": "2026-09-22", "rows": list(ROWS), "first_run": first,
                "prior": None if first else {"equity_kalshi": 23000.0},
                "equity_kalshi": 22500.0, "cash": 10000.0,
                "kalshi_positions_value": 12500.0, "equity": 22900.0,
                "positions_value": 12900.0}

    def test_section_present_in_text_and_html(self):
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False)
        self.assertIn("Biggest movers since yesterday, by family / event / market", text)
        self.assertIn("Biggest movers since yesterday, by family / event / market", html)
        # ranked: crypto first, weather second, in both parts
        self.assertLess(text.index("Crypto"), text.index("Weather & quakes"))
        self.assertLess(html.index("Crypto"), html.index("Weather &amp; quakes"))
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
        self.assertIn("(day -$0.5k: trading -$0.2k, rewards +$0.1k)", subject)
        self.assertNotIn("settled", subject)
        self.assertIn("reward credits", html)
        # a deposit is split out of the no-trade cash, and left out of the
        # day change (Jack 2026-10-04: "make the day figure exclude
        # deposits"); the account value's own move still carries it
        p["net_transfers"] = 250.0
        subject, text, html = pf.build_email(p, [], chart_ok=False)
        self.assertIn("vs yesterday: -750.00  =  trading (at mid) -225.09  +  reward credits "
                      "-150.00  +  Kalshi's pricing vs mid -374.91  (excludes "
                      "deposits/withdrawals +250.00)", text)
        self.assertIn("(account value moved -500.00 = this table -225.09  +  reward credits "
                      "-150.00  +  deposits/withdrawals +250.00  +  Kalshi's pricing vs mid "
                      "-374.91)", text)
        self.assertIn("(excludes deposits/withdrawals +250.00)", html)
        self.assertIn("(day -$0.8k: trading -$0.2k, rewards -$0.1k)", subject)
        # transfers unreadable: one lumped line, still exact, and the day
        # figure says what it may hold
        p["net_transfers"] = None
        subject, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("vs yesterday: -500.00  =  trading (at mid) -225.09  +  reward credits "
                      "& deposits +100.00  +  Kalshi's pricing vs mid -374.91\n", text)
        self.assertIn("(day -$0.5k incl. any deposits: trading -$0.2k, "
                      "rewards & deposits +$0.1k)", subject)

    def test_events_list_their_markets(self):
        """The dashboard's third level: an event whose move came from two or
        more markets lists its biggest; a single moving market's strike rides
        on the event line."""
        p = self._pf()
        mk = [{"ticker": "KXCPIYOY-26NOV-T3.6", "day": -26.2, "realized": 0.0,
               "value_d": -26.2, "value_now": 15.0, "note": ""},
              {"ticker": "KXCPIYOY-26NOV-T3.7", "day": -11.07, "realized": -4.2,
               "value_d": -6.87, "value_now": 7.4, "note": ""},
              {"ticker": "KXCPIYOY-26NOV-T3.5", "day": 3.45, "realized": 0.0,
               "value_d": 3.45, "value_now": 27.7, "note": "settled yes"}]
        cpi = _row("KXCPIYOY-26NOV", realized=-4.2, value_d=-29.62, value_now=50.1)
        cpi["markets"] = mk
        one = _row("KXRT-YOUC", value_d=5.0, value_now=9.0)
        one["markets"] = [{"ticker": "KXRT-YOUC-96", "day": 5.0, "realized": 0.0,
                           "value_d": 5.0, "value_now": 9.0, "note": ""}]
        p["rows"] = list(ROWS) + [cpi, one]
        _, text, html = pf.build_email(p, [], chart_ok=False)
        self.assertIn("Econ & rates", text)
        self.assertIn("CPI & inflation", text)             # the dashboard's group label
        self.assertIn("    T3.6", text)                    # top two markets, indented
        self.assertIn("    T3.7", text)
        self.assertIn("+1 more market", text)
        self.assertIn("KXRT-YOUC  96", text)               # one market: on the event line
        self.assertNotIn("    96", text)
        self.assertIn(">T3.6<", html)
        self.assertIn("CPI &amp; inflation", html)

    def test_risk_block_sits_under_the_chart(self):
        """Jack 2026-10-03: "move the risk controls section right under the
        chart at the top"; the IMM section hands it over on its own."""
        imm = {"text": "INCENTIVE MM body", "html": "<div>IMM BODY</div>",
               "risk_text": "RISK CONTROLS & CAPACITY ...",
               "risk_html": "<div>RISK BLOCK</div>"}
        _, text, html = pf.build_email(self._pf(), [], chart_ok=True, imm=imm)
        self.assertLess(html.index("cid:balancechart"), html.index("RISK BLOCK"))
        self.assertLess(html.index("RISK BLOCK"), html.index("Biggest movers"))
        self.assertLess(html.index("Biggest movers"), html.index("IMM BODY"))
        self.assertEqual(html.count("RISK BLOCK"), 1)
        self.assertLess(text.index("RISK CONTROLS"), text.index("Biggest movers"))
        self.assertLess(text.index("Biggest movers"), text.index("INCENTIVE MM body"))

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


class YesMarkTests(unittest.TestCase):
    """2026-10-01: Kalshi reads an empty YES ask as $1.00, and the morning
    marks took (bid + ask) / 2 whenever both read above 0. A stray bid under
    no offer marked half way to $1. The rule is now incentive_mm.
    bulk_mark_cents's, in dollars."""

    def test_two_sided_book_is_the_mid(self):
        self.assertAlmostEqual(pf.yes_mark(0.30, 0.34, 0.20), 0.32)

    def test_no_offer_marks_the_last_trade_not_halfway_to_a_dollar(self):
        # KXDDCOLDBREW-26OCT02-T4.45 at 00:00 ET 10/1: bid 5c, no offer, last
        # trade 97c, settled YES -- (0.05 + 1.00) / 2 = 0.525 before the fix
        self.assertAlmostEqual(pf.yes_mark(0.05, 1.00, 0.97), 0.97)
        # the last trade under the bid: the bid
        self.assertAlmostEqual(pf.yes_mark(0.32, 1.00, 0.20), 0.32)

    def test_no_bid_marks_the_last_trade_capped_at_the_offer(self):
        self.assertAlmostEqual(pf.yes_mark(0.0, 0.40, 0.55), 0.40)
        self.assertAlmostEqual(pf.yes_mark(0.0, 0.40, 0.30), 0.30)

    def test_empty_book_marks_the_last_trade(self):
        # a closed or settled market reads 0 / 1.00
        self.assertAlmostEqual(pf.yes_mark(0.0, 1.00, 0.82), 0.82)

    def test_no_two_sided_book_and_no_last_trade_has_no_mark(self):
        for bid, ask in ((0.05, 1.00), (0.0, 0.40), (0.0, 1.00), (0.0, 0.0)):
            self.assertIsNone(pf.yes_mark(bid, ask, 0.0), (bid, ask))

    def test_a_real_sub_penny_offer_is_a_side(self):
        # escalators trade in 0.0001 steps: only the $1.00 placeholder is no offer
        self.assertAlmostEqual(pf.yes_mark(0.98, 0.9999, 0.50), 0.98995)
        self.assertTrue(pf.side_live(0.9999))
        self.assertFalse(pf.side_live(1.0))
        self.assertFalse(pf.side_live(0.0))

    def test_a_50c_wide_book_marks_the_last_trade_inside_the_touch(self):
        # Jack 2026-10-01, "Yes, at 50c+". KXDKNGAPP-26OCT08-T185: a 1/70 book
        # under a 1c last trade marked at its 35.5c mid
        self.assertAlmostEqual(pf.yes_mark(0.01, 0.70, 0.01), 0.01)
        self.assertAlmostEqual(pf.yes_mark(0.20, 0.70, 0.90), 0.70)     # clamped to the ask
        self.assertAlmostEqual(pf.yes_mark(0.20, 0.70, 0.05), 0.20)     # ... and the bid
        self.assertAlmostEqual(pf.yes_mark(0.20, 0.69, 0.90), 0.445)    # 49c: still the mid
        self.assertAlmostEqual(pf.yes_mark(0.01, 0.99, 0.0), 0.50)      # never traded: the mid
        # KXCMGFT-26OCT08-T108: 1c bid, 20c last, an offer flickering at 99c
        # no longer flips the mark between 20 and 50
        self.assertAlmostEqual(pf.yes_mark(0.01, 0.99, 0.20), 0.20)
        self.assertAlmostEqual(pf.yes_mark(0.01, 1.00, 0.20), 0.20)

    def test_the_wide_rule_follows_the_bot_s_kill_switch(self):
        self.addCleanup(setattr, pf, "MARK_WIDE_SPREAD_CENTS", pf.MARK_WIDE_SPREAD_CENTS)
        pf.MARK_WIDE_SPREAD_CENTS = 0               # IMM_MARK_WIDE_SPREAD=0
        self.assertAlmostEqual(pf.yes_mark(0.01, 0.70, 0.01), 0.355)
        self.assertAlmostEqual(pf.yes_mark(0.05, 1.00, 0.97), 0.97)     # one-sided unchanged
        pf.MARK_WIDE_SPREAD_CENTS = 30
        self.assertAlmostEqual(pf.yes_mark(0.20, 0.50, 0.90), 0.50)


class MarkPositionsTests(unittest.TestCase):
    INFO = {"KXDDCOLDBREW-26OCT02-T4.45": {"yes_bid": 0.05, "yes_ask": 1.00, "last": 0.97},
            "M": {"yes_bid": 0.30, "yes_ask": 0.34, "last": 0.20},
            "W": {"yes_bid": 0.01, "yes_ask": 0.70, "last": 0.01},
            "E": {"yes_bid": 0.0, "yes_ask": 1.00, "last": 0.82},
            "K": {"yes_bid": 0.05, "yes_ask": 1.00, "last": 0.0},
            "Z": {"yes_bid": 0.0, "yes_ask": 0.40, "last": 0.0}}

    def test_marks_and_sources(self):
        tickers = ["KXDDCOLDBREW-26OCT02-T4.45", "M", "W", "E", "K", "Z", "X"]
        marks, src = pf.mark_positions(tickers, self.INFO, {"K": 0.06, "X": 0.5})
        self.assertAlmostEqual(marks["KXDDCOLDBREW-26OCT02-T4.45"], 0.97)
        self.assertAlmostEqual(marks["M"], 0.32)
        self.assertAlmostEqual(marks["W"], 0.01)
        self.assertAlmostEqual(marks["E"], 0.82)
        self.assertAlmostEqual(marks["K"], 0.06)      # no last trade: yesterday's mark
        self.assertIsNone(marks["Z"])                 # nor that: at cost
        self.assertAlmostEqual(marks["X"], 0.5)       # unread: yesterday's mark
        self.assertEqual(src, {"mid": 1, "wide": 1, "one_sided": 1, "last": 1,
                               "carried": 2, "at_cost": 1})

    def test_the_cold_brew_short_is_valued_off_the_last_trade(self):
        # 58.64 NO (the IMM's short YES at 91.5c, cost 4.98) is worth
        # 58.64 x (1 - 0.97) = 1.76, not the 27.85 that the 52.5c mid said
        marks, _ = pf.mark_positions(["KXDDCOLDBREW-26OCT02-T4.45"], self.INFO, {})
        self.assertAlmostEqual(
            pf.side_value(-58.64, marks["KXDDCOLDBREW-26OCT02-T4.45"], 4.98), 1.7592)


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
        self.assertIn("day -$0.5k", subject)
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


class _FakeTransfersClient:
    """/portfolio/deposits over two pages and /portfolio/withdrawals, shaped
    as Kalshi returned them on 2026-10-04."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail and path == "/portfolio/withdrawals":
            raise RuntimeError("HTTP 503")
        if path == "/portfolio/deposits":
            if not (params or {}).get("cursor"):
                return {"deposits": [
                    {"amount_cents": 250000, "status": "applied", "type": "debit",
                     "created_ts": 1770584924, "finalized_ts": 1770584924},
                    {"amount_cents": 50000, "status": "pending", "type": "apm",
                     "created_ts": 1791122652, "finalized_ts": 0}],
                    "cursor": "page2"}
            return {"deposits": [
                {"amount_cents": 350000, "status": "applied", "type": "apm",
                 "created_ts": 1791036240, "finalized_ts": 1791036240},
                {"amount_cents": 99900, "status": "failed", "type": "apm",
                 "created_ts": 1791036300, "finalized_ts": 1791036300}]}
        if path == "/portfolio/withdrawals":
            return {"withdrawals": [
                {"amount_cents": 100000, "status": "applied",
                 "created_ts": 1791000000, "finalized_ts": 1791000060}]}
        raise AssertionError(path)


class TotalProfitTests(unittest.TestCase):
    """Jack 2026-10-04: "in my daily portfolio email, at the top also show
    total profit, excluding deposits/withdrawals"."""

    def test_transfers_read_every_page_and_only_what_moved_money(self):
        c = _FakeTransfersClient()
        tr = pf.fetch_transfers(c)
        self.assertEqual(sorted(tr), [(1770584924.0, 2500.0), (1791000060.0, -1000.0),
                                      (1791036240.0, 3500.0)])
        self.assertEqual(c.calls[1], ("/portfolio/deposits", {"limit": 200, "cursor": "page2"}))

    def test_transfer_read_failure_is_none(self):
        self.assertIsNone(pf.fetch_transfers(_FakeTransfersClient(fail=True)))

    def test_sums_all_time_and_in_a_window(self):
        from datetime import datetime, timezone
        tr = pf.fetch_transfers(_FakeTransfersClient())
        now = datetime.fromtimestamp(1791122700, timezone.utc)
        self.assertEqual(pf.sum_transfers(tr, None, now), (6000.0, 1000.0))
        # the day window: only the 10/3 deposit after the prior morning
        t0 = datetime.fromtimestamp(1791000060, timezone.utc)
        self.assertEqual(pf.sum_transfers(tr, t0, now), (3500.0, 0.0))
        self.assertEqual(pf.sum_transfers(tr, None, t0), (2500.0, 1000.0))

    def _pf(self, profit=6624.95, deposited=16000.0, withdrawn=0.0, unpaid=1829.4):
        p = PerpsAndRewardsTests._pf(self, unpaid=unpaid)
        p.update(total_profit=profit, deposited=deposited, withdrawn=withdrawn)
        return p

    def test_headline_has_total_profit_under_the_account_value(self):
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=True)
        self.assertIn("Account value $22,624.95  (est. $24,454.35 after rewards are paid out)\n"
                      "  =  cash $10,000.00", text)
        self.assertIn("Total profit +$6,624.95  (est. +$8,454.35 after rewards are paid out)\n"
                      "  =  account value - $16,000.00 deposited, all time\n", text)
        self.assertLess(text.index("Total profit"), text.index("Biggest movers"))
        self.assertIn(f'Total profit <span style="color:{pf.C_POS}">+$6,624.95</span>', html)
        self.assertIn("(est. +$8,454.35 after rewards are paid out)", html)
        self.assertIn("total profit = account value &minus; $16,000.00 deposited, all time", html)
        # right under the account value, above its cash / positions line and the chart
        self.assertLess(html.index("Account value $22,624.95"), html.index("Total profit"))
        self.assertLess(html.index("Total profit"), html.index("cash <b>"))
        self.assertLess(html.index("Total profit"), html.index("cid:balancechart"))

    def test_subject_is_profit_then_the_day_without_deposits(self):
        # Jack 2026-10-04: "make the day figure exclude deposits. and then
        # shorten like this: portfolio 2026-10-04: profit +$18.2k (day
        # +$1.6k, trading -$0.7K)". A $5,000 deposit morning: the account
        # value is up 4,524.95 (4,500 on event contracts, 24.95 on perps)
        # but the day is -475.05
        p = self._pf()
        p.update(equity_kalshi=27500.0, account_value=27624.95, no_trade_cash=5100.0,
                 net_transfers=5000.0, total_profit=11624.95, deposited=16000.0)
        subject, text, html = pf.build_email(p, [], chart_ok=False)
        self.assertEqual(subject, "portfolio 2026-09-28: profit +$11.6k "
                                  "(day -$0.5k: trading -$0.2k, rewards +$0.1k)")
        self.assertIn("vs yesterday: -475.05  =  trading (at mid) -225.09  +  reward credits "
                      "+100.00  +  Kalshi's pricing vs mid -374.91  +  perpetuals +24.95  "
                      "(excludes deposits/withdrawals +5,000.00)", text)
        self.assertIn(f'day change <span style="color:{pf.C_NEG}">-475.05</span>', html)
        self.assertIn("account value ex-perpetuals moved +4,500.00", text)
        # the IMM's pick-off flag still rides on the end
        subject, _, _ = pf.build_email(p, [], chart_ok=False,
                                       imm={"text": "T", "html": "H",
                                            "subject_flag": " - PICK-OFF WINDOW"})
        self.assertEqual(subject, "portfolio 2026-09-28: profit +$11.6k (day -$0.5k: "
                                  "trading -$0.2k, rewards +$0.1k) - PICK-OFF WINDOW")
        p.update(first_run=True, prior=None)
        subject, _, _ = pf.build_email(p, [], chart_ok=False)
        self.assertEqual(subject, "portfolio 2026-09-28: profit +$11.6k (first baseline)")

    def test_subject_rewards_on_the_10_4_dry_run(self):
        # Jack 2026-10-04: "yes add rewards to the subject", after asking
        # whether day +1.1k and trading -$0.8k meant rewards +1.9k. The live
        # dry run vs the 10/3 snapshot: +1,064.13 = trading -799.67 + reward
        # credits +2,366.16 + Kalshi's pricing vs mid -507.25 + perps +4.89,
        # with $8,500 of deposits left out
        p = {"today": "2026-10-04", "rows": [_row("KXA-26", value_d=-799.67)],
             "first_run": False,
             "prior": {"equity_kalshi": 24409.13, "perps_equity": 127.23},
             "equity_kalshi": 33968.37, "cash": 10379.84,
             "kalshi_positions_value": 23588.53, "equity": 35579.0,
             "positions_value": 25199.16, "perps_equity": 132.12, "perps_stale": False,
             "account_value": 34100.49, "perps": {"equity": 132.12, "positions": []},
             "unpaid": None, "no_trade_cash": 10866.16, "net_transfers": 8500.0,
             "total_profit": 18100.49, "deposited": 16000.0, "withdrawn": 0.0}
        subject, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertEqual(subject, "portfolio 2026-10-04: profit +$18.1k "
                                  "(day +$1.1k: trading -$0.8k, rewards +$2.4k)")
        self.assertIn("vs yesterday: +1,064.13  =  trading (at mid) -799.67  +  reward "
                      "credits +2,366.16  +  Kalshi's pricing vs mid -507.25  +  perpetuals "
                      "+4.89  (excludes deposits/withdrawals +8,500.00)", text)

    def test_a_loss_and_withdrawals(self):
        _, text, html = pf.build_email(
            self._pf(profit=-1234.5, withdrawn=2500.0, unpaid=None), [], chart_ok=False)
        self.assertIn("Total profit -$1,234.50\n", text)
        self.assertIn("account value - $16,000.00 deposited + $2,500.00 withdrawn, all time",
                      text)
        self.assertIn(f'<span style="color:{pf.C_NEG}">-$1,234.50</span>', html)
        self.assertNotIn("after rewards are paid out", html)

    def test_unreadable_transfers_say_so(self):
        subject, text, html = pf.build_email(self._pf(profit=None, deposited=None), [],
                                             chart_ok=False)
        self.assertEqual(subject, "portfolio 2026-09-28: profit n/a "
                                  "(day -$0.5k incl. any deposits: trading -$0.2k)")
        self.assertIn("Total profit n/a today: Kalshi's deposit / withdrawal history "
                      "did not load", text)
        self.assertIn("Total profit n/a today", html)
        self.assertNotIn("all time", text)
        self.assertNotIn("total profit =", html)

    def test_signed_usd(self):
        self.assertEqual(pf._signed_usd(13138.5), "+$13,138.50")
        self.assertEqual(pf._signed_usd(-0.5), "-$0.50")
        self.assertEqual(pf._signed_usd(-0.004), "+$0.00")

    def test_signed_k(self):
        self.assertEqual(pf._signed_k(18159.31), "+$18.2k")
        self.assertEqual(pf._signed_k(-739.74), "-$0.7k")
        self.assertEqual(pf._signed_k(1234567.0), "+$1,234.6k")
        self.assertEqual(pf._signed_k(-40.0), "$0.0k")
        self.assertEqual(pf._signed_k(0.0), "$0.0k")


class _FakeSubaccountClient:
    """The subaccount endpoints, shaped as Kalshi returned them on 10/9."""

    def __init__(self, fail=False):
        self.fail = fail

    def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("HTTP 503")
        if path == "/portfolio/subaccounts/balances":
            return {"subaccount_balances": [
                {"balance": "19859.5966", "exchange_index": 0, "subaccount_number": 0},
                {"balance": "319.2742", "exchange_index": 2, "subaccount_number": 0},
                {"balance": "290.0000", "exchange_index": 0, "subaccount_number": 1},
                {"balance": "40.0000", "exchange_index": 0, "subaccount_number": 2}]}
        if path == "/portfolio/subaccounts/transfers":
            return {"transfers": [
                {"amount_cents": 30000, "created_ts": 1791509617, "from_subaccount": 0,
                 "to_subaccount": 1},
                {"amount_cents": 5000, "created_ts": 1791520000, "from_subaccount": 1,
                 "to_subaccount": 0},
                {"amount_cents": 4000, "created_ts": 1791530000, "from_subaccount": 1,
                 "to_subaccount": 2}]}
        if path == "/portfolio/balance":
            n = (params or {}).get("subaccount")
            return {1: {"balance_dollars": "290.0000", "portfolio_value": 1225},
                    2: {"balance_dollars": "40.0000", "portfolio_value": 0}}[n]
        raise AssertionError(path)


class SubaccountTests(unittest.TestCase):
    """Jack 2026-10-09: "the reward credit is wrong in the email". The $300
    that funded the NFL sniper's subaccount 1 left the primary's cash with no
    trade behind it, so it read as reward credits +53.24 for +353.24, and
    the subaccount's cash was in neither the account value nor the profit."""

    def test_reads_numbered_subaccounts_and_moves_to_and_from_the_primary(self):
        s = pf.fetch_subaccounts(_FakeSubaccountClient())
        self.assertEqual(s["accounts"], {1: 302.25, 2: 40.0})
        self.assertEqual(s["equity"], 342.25)
        # 1 -> 2 never touches the primary
        self.assertEqual(s["moves"], [(1791509617.0, -300.0), (1791520000.0, 50.0)])

    def test_read_failure_is_none(self):
        self.assertIsNone(pf.fetch_subaccounts(_FakeSubaccountClient(fail=True)))

    def _pf(self, prior_subs=None, subs=300.0, moves=-300.0, ntc=53.24, ek=40287.76,
            stale=False):
        prior = {"equity_kalshi": 40174.75, "perps_equity": 120.93}
        if prior_subs is not None:
            prior["subs_equity"] = prior_subs
        acct = round(ek + 120.27 + (subs or 0.0), 2)
        return {"today": "2026-10-09", "rows": [_row("KXA-26", value_d=-551.18)],
                "first_run": False, "prior": prior, "equity_kalshi": ek,
                "cash": 20127.28, "kalshi_positions_value": 20160.48,
                "equity": 43125.0, "positions_value": 22997.7,
                "perps_equity": 120.27, "perps_stale": False,
                "perps": {"equity": 120.27, "positions": []},
                "subs_equity": subs, "subs_stale": stale,
                "subs": None if stale else {"accounts": {1: subs}, "moves": []},
                "sub_moves": moves, "account_value": acct, "unpaid": None,
                "no_trade_cash": ntc, "net_transfers": 0.0,
                "total_profit": round(acct - 16000.0, 2), "deposited": 16000.0,
                "withdrawn": 0.0}

    def test_the_10_9_email(self):
        # the 10/9 7am run: trading -551.18, no-trade cash +53.24 of which
        # -300.00 was the move to subaccount 1, Kalshi's pricing +610.95,
        # perps -0.66; subaccount 1 held its $300 untouched
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False)
        self.assertEqual(subject, "portfolio 2026-10-09: profit +$24.7k "
                                  "(day +$0.4k: trading -$0.6k, rewards +$0.4k)")
        self.assertIn("Account value $40,708.03\n", text)
        self.assertIn("perpetuals $120.27  +  subaccount 1 $300.00 (first counted today, "
                      "so not in the day change)", text)
        self.assertIn("Total profit +$24,708.03", text)
        self.assertIn("vs yesterday: +412.35  =  trading (at mid) -551.18  +  reward "
                      "credits +353.24  +  Kalshi's pricing vs mid +610.95  +  perpetuals "
                      "-0.66  (excludes subaccount transfers -300.00)", text)
        self.assertIn("(account value ex-perpetuals & subaccounts moved +113.01 = this "
                      "table -551.18  +  reward credits +353.24  +  subaccount transfers "
                      "-300.00  +  Kalshi's pricing vs mid +610.95)", text)
        self.assertIn("subaccount 1 <b>$300.00</b>", html)
        self.assertIn("Account value ex-perpetuals &amp; subaccounts moved", html)
        self.assertIn("(excludes subaccount transfers -300.00)", html)

    def test_next_morning_the_subaccount_is_in_the_day_change(self):
        # the sniper made +12.25; nothing moved
        p = self._pf(prior_subs=300.0, subs=312.25, moves=0.0, ntc=353.24, ek=40587.76)
        subject, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("vs yesterday: +424.60  =  trading (at mid) -551.18  +  reward "
                      "credits +353.24  +  Kalshi's pricing vs mid +610.95  +  perpetuals "
                      "-0.66  +  subaccount 1 +12.25\n", text)
        self.assertNotIn("first counted today", text)
        self.assertIn("rewards +$0.4k", subject)

    def test_a_move_with_both_mornings_counted_nets_out(self):
        # $200 more funded, the sniper +12.25: the subaccount is up 212.25,
        # the primary's no-trade cash down 200, and neither is the day
        p = self._pf(prior_subs=300.0, subs=512.25, moves=-200.0, ntc=153.24, ek=40387.76)
        _, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("vs yesterday: +424.60  =  trading (at mid) -551.18  +  reward "
                      "credits +353.24  +  Kalshi's pricing vs mid +610.95  +  perpetuals "
                      "-0.66  +  subaccount 1 +12.25  (excludes subaccount transfers "
                      "-200.00)", text)

    def test_unread_moves_are_lumped_and_said(self):
        p = self._pf(prior_subs=300.0, moves=None, stale=True)
        subject, text, _ = pf.build_email(p, [], chart_ok=False)
        self.assertIn("reward credits & subaccount moves +53.24", text)
        self.assertIn("rewards & subaccount moves +$0.1k", subject)
        self.assertIn("subaccounts $300.00 (yesterday's value: today's read failed)", text)

    def test_history_and_chart_carry_the_subaccounts(self):
        import os
        import tempfile
        rows = pf.upsert_history([], "2026-10-09", 20127.28, 22997.7, 43125.0,
                                 20160.48, 120.27, 1897.31, 300.0)
        tmp = tempfile.mkdtemp()
        old = (pf.DATA_DIR, pf.HISTORY_CSV)
        pf.DATA_DIR, pf.HISTORY_CSV = tmp, os.path.join(tmp, "h.csv")
        try:
            pf.write_history(rows)
            back = pf.load_history()
        finally:
            pf.DATA_DIR, pf.HISTORY_CSV = old
        self.assertEqual(back[0]["subs_equity"], 300.0)


class CollateralTests(unittest.TestCase):
    """Jack 2026-10-09, after the subaccount fix: "still seems wrong. i see
    $700+ of incentive rewards this morning". Kalshi hands back collateral
    on positions across mutually exclusive outcomes and takes it back when
    they change or settle: cash with no fill or settlement behind it, which
    Kalshi's valuation offsets. It read as reward credits."""

    def test_returned_is_markets_exposure_less_events_exposure(self):
        # Quebec 4th place, NO on three parties (80 + 40 + 60): at most one
        # NO loses, so $100 is guaranteed and came back up front
        events = {"KXQUEBEC4TH-26OCT05": {"realized": 0.0, "fees": 0.0, "exposure": 52.0},
                  "KXHIGHNY-26OCT09": {"realized": 0.0, "fees": 0.0, "exposure": 12.5}}
        mkt = {"KXQUEBEC4TH-26OCT05-4-QS": {"pos": -80.0, "cost": 72.0},
               "KXQUEBEC4TH-26OCT05-4-PCQ": {"pos": -40.0, "cost": 30.0},
               "KXQUEBEC4TH-26OCT05-4-CAQ": {"pos": -60.0, "cost": 50.0},
               "KXHIGHNY-26OCT09-B70.5": {"pos": 25.0, "cost": 12.5}}
        self.assertEqual(pf.collateral_returned(events, mkt), 100.0)

    def _pf(self, collateral_d):
        p = SubaccountTests._pf(self)
        p["collateral_d"] = collateral_d
        return p

    def test_a_take_back_leaves_the_reward_credits(self):
        # the 10/9 email with Kalshi taking back $450 of collateral: the
        # credits are the $803.24 that really came in, Kalshi's pricing vs
        # mid carries the -450, the day is unchanged
        subject, text, html = pf.build_email(self._pf(-450.0), [], chart_ok=False)
        self.assertIn("vs yesterday: +412.35  =  trading (at mid) -551.18  +  reward "
                      "credits +803.24  +  Kalshi's pricing vs mid +160.95  +  perpetuals "
                      "-0.66  (excludes subaccount transfers -300.00)", text)
        self.assertIn("(Kalshi's pricing vs mid includes -450.00 of collateral Kalshi took "
                      "back on mutually exclusive positions", text)
        self.assertIn("rewards +$0.8k", subject)
        self.assertIn("of collateral Kalshi took back on mutually exclusive positions", html)

    def test_a_release_is_not_a_reward(self):
        _, text, _ = pf.build_email(self._pf(200.0), [], chart_ok=False)
        self.assertIn("reward credits +153.24", text)
        self.assertIn("includes +200.00 of collateral Kalshi released", text)

    def test_unrecorded_prior_lumps_and_says_so(self):
        # the first morning after the change: the prior snapshot has no
        # collateral reading, so the credits still hold its move
        subject, text, html = pf.build_email(self._pf(None), [], chart_ok=False)
        self.assertIn("reward credits & Kalshi collateral +353.24", text)
        self.assertIn("rewards & collateral +$0.4k", subject)
        self.assertIn("the prior morning did not record it", html)


class ImmSectionTests(unittest.TestCase):
    """Jack 2026-10-02: "cut it as a standalone email and add it into the
    Kalshi portfolio ... email" -- the IMM digest is a section of this one."""

    def _pf(self):
        return BuildEmailTests._pf(self)

    def test_section_follows_the_movers_and_flags_the_subject(self):
        imm = {"text": "INCENTIVE MM body", "html": "<div>IMM-HTML</div>",
               "subject_flag": " - PICK-OFF WINDOW"}
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False, imm=imm)
        self.assertTrue(subject.endswith("trading -$0.2k) - PICK-OFF WINDOW"), subject)
        self.assertLess(text.index("ALL FAMILIES"), text.index("INCENTIVE MM body"))
        self.assertLess(html.index("ALL FAMILIES"), html.index("<div>IMM-HTML</div>"))
        self.assertTrue(html.endswith("<div>IMM-HTML</div></div>"))

    def test_a_failed_section_is_one_line_not_a_held_email(self):
        subject, text, html = pf.build_email(self._pf(), [], chart_ok=False,
                                             imm={"error": "exit 1, see <log>"})
        self.assertIn("INCENTIVE MM: section unavailable (exit 1, see <log>)", text)
        self.assertIn("Incentive MM section unavailable: exit 1, see &lt;log>", html)
        self.assertNotIn("PICK-OFF", subject)

    def test_no_section_leaves_the_email_as_it_was(self):
        self.assertEqual(pf.build_email(self._pf(), [], chart_ok=False),
                         pf.build_email(self._pf(), [], chart_ok=False, imm=None))

    def test_the_section_is_built_in_a_child_process(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        old = (pf.LOG_DIR, pf.IMM_SECTION_LOG)
        pf.LOG_DIR, pf.IMM_SECTION_LOG = d, os.path.join(d, "imm-section.log")

        def script(name, body):
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            return p
        try:
            # stands in for send_imm_digest.py --section-out PATH; the child
            # writes UTF-8 whatever the task console's code page
            ok = script("ok.py", "import json, os, sys\n"
                                 "assert os.environ['PYTHONIOENCODING'] == 'utf-8'\n"
                                 "print('\\u2014 building')\n"
                                 "with open(sys.argv[2], 'w', encoding='utf-8') as f:\n"
                                 "    json.dump({'text': 'T \\u2014', 'html': 'H',"
                                 " 'subject_flag': ''}, f)\n")
            sec = pf.imm_section("2026-10-02", script=ok, timeout=60)
            self.assertEqual((sec.get("text"), sec.get("html")), ("T \u2014", "H"), sec)
            self.assertTrue(os.path.exists(os.path.join(d, "imm_section_2026-10-02.json")))
            sec = pf.imm_section("2026-10-02", timeout=60,
                                 script=script("bad.py", "print('boom')\nraise SystemExit(3)\n"))
            self.assertIn("exit 3", sec["error"])
            with open(pf.IMM_SECTION_LOG, encoding="utf-8") as f:
                log_text = f.read()
            self.assertIn("boom", log_text)
            self.assertIn("\u2014 building", log_text)
            sec = pf.imm_section("2026-10-02", timeout=1,
                                 script=script("slow.py", "import time\ntime.sleep(30)\n"))
            self.assertIn("timed out", sec["error"])
            sec = pf.imm_section("2026-10-02", timeout=60,
                                 script=script("empty.py", "raise SystemExit(0)\n"))
            self.assertIn("unreadable output", sec["error"])
        finally:
            pf.LOG_DIR, pf.IMM_SECTION_LOG = old
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
