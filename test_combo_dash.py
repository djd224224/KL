"""Tests for combo_dash.py (the combo maker pilot's local dashboard,
2026-11-05). Synthetic files and a fake Kalshi client; no network."""
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

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


class FillRate(unittest.TestCase):
    def test_quotes_filled(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        write_files(d.name)
        s = cd.snapshot(d.name, d.name, NOW)
        fr = s["fill_rate"]
        # 2 quotes (from -1200), 1 accept; the executed ticket (-1199) counts, the settled one (-5000) is older
        self.assertEqual((fr["quotes"], fr["accepted"], fr["filled"]), (2, 1, 1))
        h = cd.render(s)
        self.assertIn("Quotes filled", h)
        self.assertIn("50.000%", h)
        self.assertIn("1 in 2", h)


# ---- the incremental reader (Jack 2026-11-06 "http://127.0.0.1:8772/ broken": 8-22 s loads) ----

H = 3600.0


def _line(o, compact=True) -> str:
    return (json.dumps(o, separators=(",", ":")) if compact else json.dumps(o)) + "\n"


def two_days():
    """A log over two UTC days (NOW is 02:30Z, so the older day holds the end of
    the ET day before as well): two starts with a stop between, unstamped lines
    (the newer day opens with two), declines on both sides of ET midnight, both
    JSON spacings, stats, a settlement."""
    LG = LEGS
    a = [{"k": "start", "t": NOW - 26 * H, "live": False, "cfg": {"markup": 0.03}},
         {"k": "quote", "at": NOW - 25.5 * H, "mt": "M1", "legs": LG, "fair": 0.3, "ask": 0.31, "contracts": 5.0},
         {"k": "accept", "quote_id": "q1", "mt": "M1", "contracts": 5.0},
         {"k": "decline", "mt": "M1", "why": "edge_gone", "ask": 0.31, "fair": 0.3},
         {"k": "quote", "at": NOW - 20 * H, "mt": "M2", "legs": LG, "fair": 0.2, "ask": 0.21, "contracts": 9.0},
         {"k": "accept", "quote_id": "q2", "mt": "M2", "contracts": 9.0},
         {"k": "confirm", "at": NOW - 20 * H + 1, "mt": "M2", "legs": LG, "ask": 0.21, "contracts": 9.0},
         {"k": "decline", "mt": "M2", "why": "moving", "ask": 0.21, "fair": 0.2},
         {"k": "err", "what": "quote", "err": "HttpError: {\"k\":\"quote\"}"},
         {"k": "stat", "t": NOW - 19 * H, "rfq": 10, "pre_ok": 5, "priced": 2, "quoted": 1, "why": {}},
         {"k": "executed", "quote_id": "q2", "mt": "M2"},
         {"k": "filled", "t": NOW - 18 * H, "mt": "M2", "contracts": 9.0},
         {"k": "settled", "mt": "M9", "result": "no", "pnl": 1.5},
         {"k": "stop", "t": NOW - 10 * H, "open": 1},
         {"k": "start", "t": NOW - 9 * H, "live": True, "cfg": {"markup": 0.04, "max_legs": 8}},
         {"k": "quote", "at": NOW - 3 * H, "mt": "M3", "legs": LG, "fair": 0.4, "ask": 0.41, "contracts": 2.0},
         {"k": "would_quote", "at": NOW - 2.75 * H, "mt": "M4", "legs": LG, "fair": 0.1, "ask": 0.11,
          "contracts": 1.0}]
    b = [{"k": "accept", "quote_id": "q3", "mt": "M3", "contracts": 2.0},
         {"k": "err", "what": "book read", "err": "Timeout"},
         {"k": "quote", "at": NOW - 2 * H, "mt": "M5", "legs": LG, "fair": 0.5, "ask": 0.52, "contracts": 3.0},
         {"k": "quote", "at": NOW - 1.5 * H, "mt": "M6", "legs": LG, "fair": 0.25, "ask": 0.26, "contracts": 4.0},
         {"k": "decline", "mt": "M6", "why": "would_take", "ask": 0.26, "fair": 0.25},
         {"k": "position", "mt": "M2", "position": -9.0, "try": 1},
         {"k": "unfilled", "t": NOW - 100, "mt": "M6", "why": "canceled"},
         {"k": "stat", "t": NOW - 30, "rfq": 20, "pre_ok": 8, "priced": 3, "quoted": 2, "why": {"legs": 4}}]
    return a, b


def old_numbers(maker_dir, now):
    """What snapshot() made of the log before MakerLog, from a full parse of every
    line: the reference the summaries must match."""
    events = cd.read_events(maker_dir, now)
    day0 = cd.et_midnight(now)
    starts = [e for e in events if e.get("k") == "start"]
    last_start = starts[-1] if starts else None
    after = events[events.index(last_start):] if last_start else []
    last_t = max((cd._ev_ts(e) for e in after), default=0.0)
    today = [e for e in events if cd._ev_ts(e) >= day0]
    count = lambda k: sum(1 for e in today if e.get("k") == k)  # noqa: E731
    declines = {}
    for e in today:
        if e.get("k") == "decline":
            declines[e.get("why")] = declines.get(e.get("why"), 0) + 1
    stats = [e for e in events if e.get("k") == "stat"]
    return {"started": (last_start or {}).get("t"), "cfg": (last_start or {}).get("cfg") or {},
            "stopped": any(e.get("k") == "stop" for e in after),
            "last_line_age": (now - last_t) if last_t else None,
            "funnel": {"quotes": count("quote"), "would": count("would_quote"), "accepted": count("accept"),
                       "confirmed": count("confirm"), "declined": count("decline"),
                       "executed": count("executed") - count("unfilled"), "unfilled": count("unfilled"),
                       "errors": count("err")},
            "declines": declines, "last_stat": stats[-1] if stats else None,
            "fill_rate": cd.fill_rate(events, []),
            "settle_ts": {e.get("mt"): cd._ev_ts(e) for e in events if e.get("k") == "settled"},
            "feed": [(e.get("k"), cd._ev_ts(e), e.get("mt")) for e in events
                     if e.get("k") in cd.FEED_KINDS][-cd.FEED_N:][::-1]}


class Incremental(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.a, self.b = two_days()
        self.pa = os.path.join(self.d.name, "combo_2026-11-05.jsonl")
        self.pb = os.path.join(self.d.name, "combo_2026-11-06.jsonl")

    def write(self, p, rows, mode="w"):
        with open(p, mode, encoding="utf-8", newline="\r\n" if mode == "w" else None) as f:
            for i, r in enumerate(rows):
                f.write(_line(r, compact=i % 3 != 2))          # the maker's compact lines, some spaced

    def same(self, s, now=NOW, logs=None):
        want = old_numbers(self.d.name, now)
        for key in ("started", "cfg", "stopped", "last_line_age", "funnel", "declines", "last_stat", "fill_rate"):
            self.assertEqual(s[key], want[key], key)
        self.assertEqual([(e.get("k"), cd._ev_ts(e), e.get("mt")) for e in s["feed"]], want["feed"])
        days = (logs or cd.MakerLog(self.d.name)).update(now)
        merged = {}
        for x in days:
            merged.update(x.settle_ts)
        self.assertEqual(merged, want["settle_ts"])

    def test_the_summaries_match_a_full_parse(self):
        self.write(self.pa, self.a)
        self.write(self.pb, self.b)
        s = cd.snapshot(self.d.name, self.d.name, NOW)
        self.same(s)
        # the newer day's two unstamped lines took the older day's last time; today = since 04:00Z 11/05
        self.assertEqual(s["funnel"]["quotes"], 4)
        self.assertEqual(s["declines"], {"moving": 1, "would_take": 1})
        self.assertTrue(s["live"] and not s["stopped"])
        self.assertEqual(s["cfg"], {"markup": 0.04, "max_legs": 8})

    def test_only_the_new_lines_are_read(self):
        self.write(self.pa, self.a)
        self.write(self.pb, self.b[:3])
        logs = cd.MakerLog(self.d.name)
        self.same(cd.snapshot(self.d.name, self.d.name, NOW, logs=logs), logs=logs)
        sb = logs.sums["2026-11-06"]
        off = sb.off
        self.assertEqual(off, os.path.getsize(self.pb))
        with open(self.pb, "a", encoding="utf-8") as f:                # the rest, and half a line
            for r in self.b[3:]:
                f.write(_line(r))
            f.write('{"k":"quote","at":%r,"mt":"M7","legs":[],"fair":0.1,"ask":0.' % (NOW - 10))
        s = cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        self.assertIs(logs.sums["2026-11-06"], sb)                      # the same summary, read on from `off`
        self.same(s, logs=logs)
        self.assertLess(sb.off, os.path.getsize(self.pb))               # the half line waits
        with open(self.pb, "a", encoding="utf-8") as f:
            f.write('11,"contracts":1.0}\n')
        s = cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        self.same(s, logs=logs)
        self.assertEqual(s["fill_rate"]["quotes"], 6)
        self.assertEqual(sb.off, os.path.getsize(self.pb))

    def test_the_gzipped_day_is_read_once_and_not_twice(self):
        self.write(self.pa, self.a)
        self.write(self.pb, self.b)
        logs = cd.MakerLog(self.d.name)
        before = cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        with open(self.pa, "rb") as src, gzip.open(self.pa + ".gz", "wb") as dst:   # the UTC day roll
            dst.write(src.read())
        os.remove(self.pa)
        after = cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        self.same(after, logs=logs)
        self.assertEqual(after["funnel"], before["funnel"])
        self.assertTrue(logs.sums["2026-11-05"].final)
        fresh = cd.snapshot(self.d.name, self.d.name, NOW)              # a page started after the roll
        self.same(fresh)
        self.assertEqual(fresh["cfg"], {"markup": 0.04, "max_legs": 8})

    def test_a_replaced_file_is_read_again(self):
        self.write(self.pb, self.b)
        logs = cd.MakerLog(self.d.name)
        cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        self.write(self.pb, self.b[:2])                                  # shorter: a new file
        s = cd.snapshot(self.d.name, self.d.name, NOW, logs=logs)
        self.same(s, logs=logs)

    def test_quote_lines_skip_the_parse(self):
        self.assertEqual(cd._quote_at(b'{"id":"x","at":1793974322.62,"k":"quote","sent_ms":4.1}\r'), 1793974322.62)
        self.assertEqual(cd._quote_at(b'{"k": "quote", "at": 1.5}'), 1.5)
        self.assertIsNone(cd._quote_at(b'{"k":"would_quote","at":1.5}'))
        self.assertIsNone(cd._quote_at(b'{"k":"quote","t":2.0,"at":1.5}'))     # "t" first: parsed in full
        self.assertIsNone(cd._quote_at(b'{"k":"err","err":"HttpError: {\\"k\\":\\"quote\\"}"}'))


class Background(unittest.TestCase):
    def test_a_stale_read_refreshes_behind_the_page(self):
        gate = threading.Event()

        class Slow(FakeClient):
            n = 0

            def get(self, path, params):
                if path == "/portfolio/balance":
                    Slow.n += 1
                    if Slow.n > 1:
                        assert gate.wait(10)
                        return {"balance_breakdown": [{"balance": "99.00", "exchange_index": 1}]}
                return FakeClient.get(self, path, params)

        k = cd.KalshiReads(client_factory=Slow)
        self.assertAlmostEqual(k.get(NOW, [], NOW)["cash"], 232.10)        # the first read: inline
        t = time.perf_counter()
        d = k.get(NOW + 61, [], NOW)                                        # stale: refreshed behind the page
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertAlmostEqual(d["cash"], 232.10)                           # the last reads meanwhile
        self.assertIs(k.get(NOW + 62, [], NOW), d)
        gate.set()
        k.thread.join(5)
        self.assertAlmostEqual(k.get(NOW + 63, [], NOW)["cash"], 99.0)
        self.assertEqual(Slow.n, 2)                                         # one refresh at a time

    def test_the_candles_too(self):
        T0 = History.T0
        gate = threading.Event()

        class C:
            n = 0

            def get(self, path, params):
                C.n += 1
                if C.n > 1:                                                 # the first read is inline
                    assert gate.wait(10)
                return {"markets": [{"market_ticker": tk, "candlesticks": [
                    {"end_period_ts": int(T0 + 300), "yes_bid": {"close_dollars": "0.4900"},
                     "yes_ask": {"close_dollars": "0.5100"}}]} for tk in params["market_tickers"].split(",")]}

        class K:
            io = threading.Lock()

            def _client(self):
                return C()

        h = cd.HistoryReads(K())
        tickets = History().tickets()
        first = h.get(T0 + 1200, tickets, {}, {})
        self.assertTrue(first["series"])
        t = time.perf_counter()
        self.assertIs(h.get(T0 + 1600, tickets, {}, {}), first)             # stale: the last series meanwhile
        self.assertLess(time.perf_counter() - t, 1.0)
        gate.set()
        h.thread.join(5)
        self.assertEqual(h.data["at"], T0 + 1600)


class Task(unittest.TestCase):
    """The "KL combo-dash" task keeps the page up (Jack 2026-11-06: "Yes, add the task")."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        write_files(self.d.name)

    def server(self, port=0):
        return cd.DashServer(("127.0.0.1", port), cd.make_handler(self.d.name, self.d.name, None))

    def test_a_second_dash_cannot_take_the_port(self):
        a = self.server()
        self.addCleanup(a.server_close)
        with self.assertRaises(OSError):
            self.server(a.server_address[1]).server_close()

    def test_the_stop_file_ends_it(self):
        srv = self.server()
        stop = os.path.join(self.d.name, "combo_dash.stop")
        out = []
        th = threading.Thread(target=lambda: out.append(cd.serve(srv, stop, check_s=0.05)), daemon=True)
        th.start()
        page = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5).read().decode()
        self.assertIn("Combo maker", page)
        open(stop, "w").close()
        th.join(5)
        self.assertEqual(out, [cd.EXIT_STOP])

    def test_main_exits_3_on_the_stop_file_and_4_on_a_taken_port(self):
        stop = os.path.join(self.d.name, "combo_dash.stop")
        with mock.patch.object(cd, "STOP_FILE", stop), mock.patch.object(cd, "MAKER_DIR", self.d.name), \
                mock.patch.object(cd, "SHADOW_DIR", self.d.name):
            open(stop, "w").close()
            self.assertEqual(cd.main(["--no-kalshi", "--port", "0"]), cd.EXIT_STOP)
            os.remove(stop)
            a = self.server()
            self.addCleanup(a.server_close)
            self.assertEqual(cd.main(["--no-kalshi", "--port", str(a.server_address[1])]), cd.EXIT_PORT)

    def test_the_header_shows_the_nfl_cut(self):
        """Jack 2026-11-06: "NFL parlays require 2c less edge because there are no maker fees"."""
        s = cd.snapshot(self.d.name, self.d.name, NOW)
        s["cfg"]["nfl_markup_cut"], s["cfg"]["confirm_edge_floor"] = 0.02, 0.01
        page = cd.render(s)
        self.assertIn("all-NFL 2 pts less", page)
        self.assertIn("last look &ge; 1%", page)
        s["cfg"]["nfl_markup_cut"] = 0.0
        self.assertNotIn("all-NFL", cd.render(s))

    def test_the_tables_have_totals(self):
        """Jack 2026-11-06: the rows' P&L now plus the settled P&L "doesnt add up to the total"."""
        s = cd.snapshot(self.d.name, self.d.name, NOW, cd.KalshiReads(client_factory=FakeClient))
        page = cd.render(s)
        self.assertIn("Total of 1 &middot; the Unrealized card", page)
        self.assertIn("Total of 1 &middot; the Settled card", page)
        self.assertAlmostEqual(s["pnl"]["unrealized"], sum(t["_pnl"] for t in s["open"]))


if __name__ == "__main__":
    unittest.main()
