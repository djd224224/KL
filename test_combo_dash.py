"""Tests for combo_dash.py (the combo maker pilot's local dashboard,
2026-11-05). Synthetic files and a fake Kalshi client; no network."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import combo_dash as cd

NOW = 1793931000.0                 # 2026-11-06 02:30Z = 9:30 PM ET 11/05
LEGS = [["KXNBAGAME-26NOV05MIAGSW-GSW", "yes"], ["KXNHLGAME-26NOV05BOSTOR-BOS", "no"]]


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params):
        self.calls.append(path)
        if path == "/portfolio/balance":
            return {"balance_breakdown": [{"balance": "16120.25", "exchange_index": 0},
                                          {"balance": "232.10", "exchange_index": 1}]}
        if path == "/portfolio/fills":
            return {"fills": [{"market_ticker": "KXMVECROSSCATEGORY-S2026A-B", "count_fp": "30.00",
                               "yes_price_dollars": "0.3350", "outcome_side": "no", "fee_cost": "0.000000",
                               "created_time": "2026-11-06T02:10:05Z"},
                              {"market_ticker": "KXNBAGAME-26NOV05MIAGSW-GSW", "count_fp": "5.00",
                               "fee_cost": "0.10"}]}                       # not a combo: left out
        if path == "/markets":
            return {"markets": [{"ticker": "KXMVECROSSCATEGORY-S2026A-B", "status": "active", "result": ""}]}
        raise AssertionError(path)


def write_files(d, now=NOW):
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    rows = [
        {"k": "start", "t": now - 1800, "live": True,
         "cfg": {"markup": 0.04, "min_legs": 2, "max_legs": 5, "games": ["26NOV05MIAGSW"], "min_lead_min": 5,
                 "max_payout": 150, "max_game_payout": 300, "max_open_loss": 200}},
        {"k": "quote", "at": now - 1200, "mt": "KXMVECROSSCATEGORY-S2026A-B", "legs": LEGS, "fair": 0.32,
         "ask": 0.333, "contracts": 30.0, "sent_ms": 120.0},
        {"k": "accept", "quote_id": "q1", "mt": "KXMVECROSSCATEGORY-S2026A-B", "contracts": 30.0},
        {"k": "confirm", "at": now - 1199, "mt": "KXMVECROSSCATEGORY-S2026A-B", "legs": LEGS, "ask": 0.333,
         "contracts": 30.0, "confirm_ms": 210.0},
        {"k": "quote", "at": now - 900, "mt": "KXMVECROSSCATEGORY-S2026C-D", "legs": LEGS, "fair": 0.3,
         "ask": 0.312, "contracts": 20.0},
        {"k": "decline", "mt": "KXMVECROSSCATEGORY-S2026C-D", "why": "edge_gone", "ask": 0.312,
         "fair": 0.3, "fair_now": 0.31},
        {"k": "err", "what": "quote", "err": "HttpError: nope"},
        {"k": "stat", "t": now - 30, "rfq": 21000, "pre_ok": 1500, "priced": 12, "quoted": 10,
         "why": {"legs": 12000, "not_main": 5000}},
    ]
    with open(os.path.join(d, f"combo_{day}.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tickets = [{"quote_id": "q1", "mt": "KXMVECROSSCATEGORY-S2026A-B", "legs": LEGS, "ask": 0.333,
                "fair": 0.32, "fair_confirm": 0.321, "contracts": 30.0, "status": "executed",
                "at": now - 1199, "games": ["26NOV05MIAGSW", "26NOV05BOSTOR"]},
               {"quote_id": "q0", "mt": "KXMVECROSSCATEGORY-S2026E-F", "legs": LEGS, "ask": 0.25,
                "fair": 0.24, "contracts": 40.0, "status": "settled", "result": "no", "pnl": 9.65,
                "at": now - 5000, "games": []}]
    with open(os.path.join(d, "maker_state.json"), "w", encoding="utf-8") as f:
        json.dump({"tickets": tickets}, f)


class Dash(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        write_files(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_labels(self):
        self.assertEqual(cd.leg_label(LEGS[0]), "NBA MIAGSW: GSW win")
        self.assertEqual(cd.leg_label(LEGS[1]), "NOT (NHL BOSTOR: BOS win)")
        self.assertEqual(cd.leg_label(["KXNBASPREAD-26NOV05MIAGSW-GSW5", "yes"]), "NBA MIAGSW: GSW5 spread")
        self.assertEqual(cd.leg_label(["KXNBATOTAL-26NOV05MIAGSW-235", "yes"]), "NBA MIAGSW: over 235")
        self.assertEqual(cd.leg_label(["KXNCAAMBTOTAL-26NOV05CPARIZ-175", "no"]), "NCAAMB CPARIZ: under 175")

    def test_unstamped_lines_take_the_last_time(self):
        ev = cd.read_events(self.d.name, NOW)
        by = {e["k"]: e for e in ev}
        self.assertEqual(by["accept"]["_ts"], NOW - 1200)       # after the quote at -1200
        self.assertEqual(by["err"]["_ts"], NOW - 900)

    def test_snapshot_counts_and_kalshi(self):
        k = cd.KalshiReads(client_factory=FakeClient)
        s = cd.snapshot(self.d.name, self.d.name, NOW, k)
        self.assertTrue(s["running"] and s["live"])
        f = s["funnel"]
        self.assertEqual((f["quotes"], f["accepted"], f["confirmed"], f["declined"], f["errors"]), (2, 1, 1, 1, 1))
        self.assertEqual(s["declines"], {"edge_gone": 1})
        self.assertEqual(len(s["open"]), 1)
        self.assertAlmostEqual(s["open_loss"], 30 * (1 - 0.333))
        self.assertAlmostEqual(s["settled_pnl"], 9.65)
        self.assertEqual((s["wins"], s["losses"]), (1, 0))
        self.assertAlmostEqual(s["kalshi"]["cash"], 232.10)
        self.assertEqual(len(s["fills"]), 1)                     # the single-market fill is left out
        # cached: a second snapshot inside the TTL reads nothing new
        n = len(k.client.calls)
        cd.snapshot(self.d.name, self.d.name, NOW + 5, k)
        self.assertEqual(len(k.client.calls), n)

    def test_a_kalshi_canceled_fill_is_not_counted(self):
        day = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%d")
        with open(os.path.join(self.d.name, f"combo_{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"k": "executed", "quote_id": "q1", "mt": "KXMVECROSSCATEGORY-S2026A-B"}) + "\n")
            f.write(json.dumps({"k": "unfilled", "t": NOW - 20, "mt": "KXMVECROSSCATEGORY-S2026A-B",
                                "why": "canceled"}) + "\n")
        s = cd.snapshot(self.d.name, self.d.name, NOW)
        self.assertEqual((s["funnel"]["executed"], s["funnel"]["unfilled"]), (0, 1))
        self.assertIn("1 canceled by Kalshi, 0 filled", cd.render(s))

    def test_stopped_when_quiet(self):
        s = cd.snapshot(self.d.name, self.d.name, NOW + 600)
        self.assertFalse(s["running"])

    def test_page_renders(self):
        s = cd.snapshot(self.d.name, self.d.name, NOW, cd.KalshiReads(client_factory=FakeClient))
        page = cd.render(s)
        for want in ("Combo maker", "LIVE", "Open tickets (1)", "Settled (1)", "NBA MIAGSW: GSW win",
                     "edge_gone", "$232.10", "Fills on Kalshi (1)", "http-equiv='refresh'"):
            self.assertIn(want, page)

    def test_page_renders_with_nothing_there(self):
        with tempfile.TemporaryDirectory() as empty:
            page = cd.render(cd.snapshot(empty, empty, NOW))
        self.assertIn("NOT RUNNING", page)
        self.assertIn("No open tickets", page)

    def test_server_answers(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), cd.make_handler(self.d.name, self.d.name, None))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            port = srv.server_address[1]
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
            self.assertIn("Combo maker", body)
            js = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/data.json", timeout=5).read())
            self.assertIn("funnel", js)
        finally:
            srv.shutdown()
            srv.server_close()


class Pnl(unittest.TestCase):
    """Jack 2026-11-06: "show total pnl, settled and unrealized, premiums and trading"."""

    def test_parts_add_up(self):
        mk = {"A": {"yes_bid_dollars": "0.4900", "yes_ask_dollars": "0.5100"},      # mid 0.50
              "B": {"yes_bid_dollars": "0.2900", "yes_ask_dollars": "0.3100"},      # mid 0.30
              "C": {"result": "no"}}
        open_t = [{"legs": [["A", "yes"], ["B", "no"]], "contracts": 100.0, "ask": 0.36, "fair": 0.33,
                   "fee_est": 0.20}]                                                  # fair now 0.5 x 0.7 = 0.35
        settled = [{"legs": [["C", "yes"]], "contracts": 50.0, "ask": 0.25, "fair": 0.24, "fee_est": 0.10,
                    "result": "no", "pnl": 50 * 0.25 - 0.10}]
        p = cd.pnl_parts(open_t, settled, mk)
        self.assertAlmostEqual(p["unrealized"], 100 * (0.36 - 0.35) - 0.20)            # +0.80
        self.assertAlmostEqual(p["settled"], 12.40)
        self.assertAlmostEqual(p["premium"], 100 * 0.03 + 50 * 0.01)                   # 3.50
        self.assertAlmostEqual(p["trading"], 100 * (0.33 - 0.35) + 50 * 0.24)          # -2 + 12 = 10.0
        self.assertAlmostEqual(p["fees"], 0.30)
        self.assertAlmostEqual(p["total"], p["premium"] + p["trading"] - p["fees"])
        self.assertAlmostEqual(p["total"], p["settled"] + p["unrealized"])
        self.assertAlmostEqual(open_t[0]["_value"], 0.35)

    def test_an_unpriced_leg_holds_the_quote_fair(self):
        open_t = [{"legs": [["A", "yes"]], "contracts": 10.0, "ask": 0.40, "fair": 0.38, "fee_est": 0.0}]
        p = cd.pnl_parts(open_t, [], {"A": {"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.4100"}})
        self.assertEqual(p["unpriced"], 1)
        self.assertAlmostEqual(p["trading"], 0.0)
        self.assertAlmostEqual(p["unrealized"], 10 * 0.02)

    def test_the_page_shows_it(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        write_files(d.name)
        s = cd.snapshot(d.name, d.name, NOW)
        h = cd.render(s)
        for label in ("Total", "Settled", "Unrealized", "Premiums", "Trading", "Fees", "P&amp;L now"):
            self.assertIn(label, h)


class History(unittest.TestCase):
    """Jack 2026-11-06: "add time series of PnL and time series of fills"."""
    T0 = 1793925000.0

    def tickets(self):
        return [{"quote_id": "a", "mt": "M1", "legs": [["A", "yes"], ["B", "no"]], "contracts": 100.0, "ask": 0.36,
                 "fair": 0.33, "fee_est": 0.20, "status": "executed", "at": self.T0, "executed_at": self.T0 + 1},
                {"quote_id": "b", "mt": "M2", "legs": [["C", "yes"]], "contracts": 50.0, "ask": 0.25, "fair": 0.24,
                 "fee_est": 0.10, "status": "settled", "result": "no", "at": self.T0 + 600, "executed_at": self.T0 + 601}]

    def hist(self):
        T0 = self.T0
        return {"A": [(T0 + 300, 0.50), (T0 + 900, 0.60)], "B": [(T0 + 300, 0.30)],
                "C": [(T0 + 900, 0.25), (T0 + 1500, 0.10)]}

    def test_the_series(self):
        T0 = self.T0
        s = cd.pnl_series(self.tickets(), self.hist(), {}, {"M2": T0 + 1800}, T0 + 2100, step=300)
        by = {round(p["t"] - T0): p for p in s}
        # T0+300: ticket a only; fair now 0.5 x 0.7 = 0.35 -> trading 100 x (0.33 - 0.35) = -2
        self.assertAlmostEqual(by[300]["premium"], 3.0)
        self.assertAlmostEqual(by[300]["trading"], -2.0)
        self.assertAlmostEqual(by[300]["total"], 3.0 - 2.0 - 0.20)
        # T0+900: a at 0.6 x 0.7 = 0.42 (-9); b at C 0.25 (50 x (0.24 - 0.25) = -0.5)
        self.assertAlmostEqual(by[900]["trading"], -9.0 - 0.5)
        # T0+1800: b settled NO at its settlement -> 50 x 0.24 = +12
        self.assertAlmostEqual(by[1800]["trading"], -9.0 + 12.0)
        self.assertAlmostEqual(by[1800]["fees"], 0.30)

    def test_before_a_legs_first_candle_the_quote_fair_holds(self):
        T0 = self.T0
        s = cd.pnl_series(self.tickets()[:1], {"A": [(T0 + 900, 0.5)], "B": [(T0 + 300, 0.3)]}, {}, {}, T0 + 700, step=300)
        self.assertAlmostEqual(s[0]["trading"], 0.0)                     # T0+300: A has no candle yet

    def test_reads_render_and_tooltips(self):
        T0 = self.T0

        class C:
            def __init__(self):
                self.calls = []

            def get(self, path, params):
                self.calls.append((path, dict(params)))
                assert path == "/markets/candlesticks"
                out = []
                for tk in params["market_tickers"].split(","):
                    out.append({"market_ticker": tk, "candlesticks": [
                        {"end_period_ts": int(T0 + 300), "yes_bid": {"close_dollars": "0.4900"},
                         "yes_ask": {"close_dollars": "0.5100"}}]})
                return {"markets": out}

        class K:
            def __init__(self):
                self.c = C()

            def _client(self):
                return self.c

        k = K()
        h = cd.HistoryReads(k)
        d = h.get(T0 + 1200, self.tickets(), {}, {})
        self.assertEqual(set(d["legs"]), {"A", "B", "C"})
        self.assertTrue(d["series"])
        n = len(k.c.calls)
        self.assertEqual({c[1]["period_interval"] for c in k.c.calls}, {1})   # Kalshi: 1, 60 or 1440 only
        self.assertTrue(all(len(c[1]["market_tickers"].split(",")) <= 3 for c in k.c.calls))
        h.get(T0 + 1230, self.tickets(), {}, {})                          # cached
        self.assertEqual(len(k.c.calls), n)
        fp = cd.fill_points(self.tickets())
        self.assertEqual([round(f["premium"], 2) for f in fp], [36.0, 12.5])
        pts = d["series"] + [{"t": T0 + 1200, "premium": 3.5, "trading": -1.0, "fees": 0.3, "total": 2.2}]
        page = cd.chart_pnl(pts) + cd.chart_fills(fp, T0, T0 + 1200)
        for needle in ("id='pnl-svg'", "id='fill-svg'", "Total +$2.20", "Premiums", "Trading", "pnl-data", "fill-data",
                       "P&amp;L series as a table", "class='dothit'"):
            self.assertIn(needle, page)
        self.assertIn("viz-tip", cd.CHART_JS)

    def test_the_page_has_both_charts(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        write_files(d.name)
        s = cd.snapshot(d.name, d.name, NOW)
        self.assertEqual(len(s["fill_points"]), 2)                       # the executed + the settled ticket
        h = cd.render(s)
        self.assertIn("P&amp;L over time", h)
        self.assertIn("Fills over time", h)


if __name__ == "__main__":
    unittest.main()
