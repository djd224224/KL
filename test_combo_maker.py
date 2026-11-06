"""Tests for combo_maker.py (the Strategy 1 combo maker pilot, 2026-11-05).
A fake client; nothing reaches Kalshi."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import combo_maker as cm

T0 = 1793925000.0           # 2026-11-06 ~00:30Z


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def mkt(t, bid, ask, start_in_h=2.0, status="active", bid_size=5000.0, ask_size=5000.0, event=None):
    return {"ticker": t, "status": status, "yes_bid_dollars": f"{bid:.4f}",
            "yes_ask_dollars": f"{ask:.4f}", "expected_expiration_time": iso(T0 + (start_in_h + 3) * 3600),
            "yes_bid_size_fp": f"{bid_size:.2f}", "yes_ask_size_fp": f"{ask_size:.2f}",
            "event_ticker": event or t.rsplit("-", 1)[0]}


L1 = "KXNBAGAME-26NOV05MIAGSW-GSW"
L2 = "KXNHLGAME-26NOV05BOSTOR-BOS"
L3 = "KXNCAAMBGAME-26NOV05DUKEUNC-DUKE"
L4 = "KXNBASPREAD-26NOV05MIAGSW-GSW5"          # same game as L1
PROP = "KXNBAPTS-26NOV05ORLPHX-PHXDBOOKER15-25"
LEGS = [[L1, "yes"], [L2, "yes"], [L3, "no"]]
MK = {L1: mkt(L1, 0.59, 0.61), L2: mkt(L2, 0.49, 0.51), L3: mkt(L3, 0.29, 0.31), L4: mkt(L4, 0.4, 0.42)}
FAIR = 0.60 * 0.50 * 0.70                      # 0.21


class W:
    def __init__(self):
        self.rows = []

    def write(self, o):
        self.rows.append(o)

    def flush(self):
        pass

    def k(self, k):
        return [r for r in self.rows if r.get("k") == k]


class FakeClient:
    host = "https://x/trade-api/v2"
    http_timeout = 5
    key_id = "k"

    def __init__(self, markets, position=-40.0):
        self.mk = dict(markets)
        self.posts, self.puts, self.gets = [], [], []
        self.position = position

    def get(self, path, params):
        self.gets.append((path, dict(params)))
        if path == "/markets" and "event_ticker" in params:
            return {"markets": [m for m in self.mk.values() if m.get("event_ticker") == params["event_ticker"]]}
        if path == "/markets":
            return {"markets": [self.mk[t] for t in params["tickers"].split(",") if t in self.mk]}
        if path == "/portfolio/positions":
            return {"market_positions": [{"ticker": params["ticker"], "position_fp": f"{self.position:.2f}"}]}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, json.loads(body)))
        return {"id": f"q{len(self.posts)}"}

    # what put_json uses
    def rate_limit(self):
        pass

    def request_headers(self, method, path):
        return {}

    def raise_if_bad_response(self, r):
        pass

    def _http(self):
        outer = self

        class S:
            def put(self, url, data, headers, timeout):
                outer.puts.append(url)

                class R:
                    content = b""
                return R()
        return S()


def rfq_msg(rid="r1", legs=LEGS, tc="10.0000", cn="0.00"):
    return {"type": "rfq_created", "msg": {
        "id": rid, "creator_id": "abcdef0123", "market_ticker": "KXMVECROSSCATEGORY-S2026X-Y",
        "target_cost_dollars": tc, "contracts_fp": cn, "created_ts": iso(T0 - 0.2),
        "mve_collection_ticker": "KXMVECROSSCATEGORY-R",
        "mve_selected_legs": [{"event_ticker": l[0].rsplit("-", 1)[0], "market_ticker": l[0], "side": l[1]}
                              for l in legs]}}


class FiltersAndPricing(unittest.TestCase):
    cfg = cm.Config()

    def test_pre_check(self):
        self.assertIsNone(cm.pre_check(LEGS, self.cfg))
        self.assertEqual(cm.pre_check(LEGS[:2], self.cfg), "legs")               # 2 legs: < 3
        self.assertIsNone(cm.pre_check(LEGS + [[PROP, "yes"]], self.cfg))            # a prop on another game
        self.assertEqual(cm.pre_check(LEGS + [[PROP, "yes"]], cm.Config(allow_extras=False)), "not_main")
        self.assertEqual(cm.pre_check([[L1, "yes"], [L4, "yes"], [L2, "yes"]], self.cfg), "same_game")
        self.assertEqual(cm.pre_check([[L1, "yes", "0.5"], [L2, "yes"], [L3, "no"]], self.cfg), "side")

    def test_price_check_and_fair(self):
        why, fair = cm.price_check(LEGS, MK, T0, self.cfg)
        self.assertIsNone(why)
        self.assertAlmostEqual(fair, FAIR)
        self.assertEqual(cm.price_check(LEGS, dict(MK, **{L2: mkt(L2, 0.45, 0.50)}), T0, self.cfg)[0], "wide")
        self.assertEqual(cm.price_check(LEGS, dict(MK, **{L2: mkt(L2, 0.0, 0.50)}), T0, self.cfg)[0], "one_sided")
        self.assertEqual(cm.price_check(LEGS, dict(MK, **{L2: mkt(L2, 0.49, 0.51, start_in_h=0.4)}), T0,
                                        self.cfg)[0], "starts_soon")                 # 24 min < 30
        self.assertEqual(cm.price_check(LEGS, dict(MK, **{L2: mkt(L2, 0.49, 0.51, status="closed")}), T0,
                                        self.cfg)[0], "not_active")
        hi = {L1: mkt(L1, 0.89, 0.91), L2: mkt(L2, 0.89, 0.91), L3: mkt(L3, 0.09, 0.11)}
        self.assertEqual(cm.price_check(LEGS, hi, T0, self.cfg)[0], "fair_range")      # 0.73 > 0.40
        self.assertEqual(cm.price_check(LEGS, {L1: MK[L1]}, T0, self.cfg)[0], "no_market")

    def test_quote_price_rounds_our_price_up(self):
        self.assertEqual(cm.quote_price(0.20, self.cfg), (0.22, 0.78))
        self.assertEqual(cm.quote_price(0.1234, self.cfg), (0.136, 0.864))           # 0.13574 -> 0.136

    def test_contracts_and_caps(self):
        self.assertAlmostEqual(cm.contracts_of({"tc": "10.0000"}, 0.25), 40.0)
        self.assertAlmostEqual(cm.contracts_of({"tc": None, "cn": "120.00"}, 0.25), 120.0)
        b = cm.Book()
        day = cm.et_day(T0)
        self.assertIsNone(cm.caps_check(b, ["g1", "g2"], 100, 0.25, self.cfg, day))
        self.assertEqual(cm.caps_check(b, ["g1"], 1.0, 0.25, self.cfg, day), "dust")      # $0.25 premium
        self.assertEqual(cm.caps_check(b, ["g1"], 151, 0.25, self.cfg, day), "payout_cap")
        b.tickets = [{"status": "executed", "contracts": 150, "ask": 0.25, "games": ["g1"], "day": day},
                     {"status": "executed", "contracts": 140, "ask": 0.25, "games": ["g1", "g3"], "day": day}]
        self.assertEqual(cm.caps_check(b, ["g1"], 20, 0.25, self.cfg, day), "game_cap")   # 290 + 20 > 300
        self.assertIsNone(cm.caps_check(b, ["g2"], 100, 0.25, self.cfg, day))
        b.tickets.append({"status": "confirmed", "contracts": 150, "ask": 0.05, "games": ["g9"], "day": day})
        # open loss 150*.75 + 140*.75 + 150*.95 = 360; +100*.75 = 435 > 400
        self.assertEqual(cm.caps_check(b, ["g2"], 100, 0.25, self.cfg, day), "loss_cap")
        b.tickets = [{"status": "settled", "contracts": 1, "ask": 0.5, "games": [], "day": day}] * 30
        self.assertEqual(cm.caps_check(b, ["g2"], 10, 0.25, self.cfg, day), "ticket_cap")


class MakerFlow(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.clock = [T0]
        self.w = W()

    def tearDown(self):
        self.d.cleanup()

    def maker(self, live, client=None, **kw):
        self.client = client or FakeClient(MK)
        kw.setdefault("markup_per_leg", 0.0)        # these flows pin one flat 10%; LegMarkup tests the ladder
        cfg = cm.Config(live=live, **kw)
        self.book = cm.Book(os.path.join(self.d.name, "state.json"))
        return cm.Maker(self.client, cfg, self.w, self.book, self.d.name, clock=lambda: self.clock[0])

    def test_dry_prices_and_logs_but_sends_nothing(self):
        m = self.maker(live=False)
        m.handle(rfq_msg())
        wq = self.w.k("would_quote")
        self.assertEqual(len(wq), 1)
        self.assertAlmostEqual(wq[0]["fair"], FAIR)
        self.assertEqual((wq[0]["ask"], wq[0]["no_bid"]), (0.231, 0.769))
        self.assertEqual(self.client.posts, [])

    def test_live_quote_then_last_look_confirm(self):
        m = self.maker(live=True)
        m.handle(rfq_msg())
        path, body = self.client.posts[0]
        self.assertEqual(path, "/communications/quotes")
        self.assertEqual(body, {"rfq_id": "r1", "yes_bid": "0.0000", "no_bid": "0.7690", "rest_remainder": False})
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": "0.7690", "accepted_side": "yes",
                                                      "contracts_accepted_fp": "43.29"}})
        self.assertEqual(self.client.puts, [FakeClient.host + "/communications/rfqs/r1/quotes/q1/confirm"])
        t = self.book.tickets[0]
        self.assertEqual((t["status"], t["ask"], t["contracts"]), ("confirmed", 0.231, 43.29))
        # the fill, then the side read back: NO, as intended
        m.handle({"type": "quote_executed", "msg": {"quote_id": "q1", "order_id": "o1"}})
        self.clock[0] += 5
        m.upkeep()
        self.assertTrue(self.book.tickets[0]["dir_ok"])
        self.assertFalse(os.path.exists(os.path.join(self.d.name, cm.STOP_NAME)))

    def test_last_look_declines_when_the_legs_moved(self):
        m = self.maker(live=True)
        m.handle(rfq_msg())
        self.client.mk[L1] = mkt(L1, 0.65, 0.67)       # fair 0.66 x .5 x .7 = 0.231: edge gone
        self.clock[0] += 1
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        self.assertEqual(self.client.puts, [])
        self.assertEqual(self.w.k("decline")[0]["why"], "edge_gone")
        self.assertEqual(self.book.tickets, [])

    def test_a_yes_position_writes_stop_and_exits(self):
        m = self.maker(live=True, client=FakeClient(MK, position=+43.0))
        m.handle(rfq_msg())
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        m.handle({"type": "quote_executed", "msg": {"quote_id": "q1", "order_id": "o1"}})
        self.clock[0] += 5
        with self.assertRaises(SystemExit) as cx:
            m.upkeep()
        self.assertEqual(cx.exception.code, 5)
        self.assertTrue(os.path.exists(os.path.join(self.d.name, cm.STOP_NAME)))

    def test_a_stale_rfq_is_skipped_before_any_read(self):
        m = self.maker(live=True)
        msg = rfq_msg()
        msg["msg"]["created_ts"] = iso(T0 - 5.0)          # 5 s old: other makers answered long ago
        m.handle(msg)
        self.assertEqual((self.client.posts, self.client.gets), ([], []))
        self.assertEqual(m.st["why"].get("stale"), 1)

    def test_other_makers_quotes_and_unknown_accepts_are_ignored(self):
        m = self.maker(live=True)
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "not-ours", "rfq_id": "rX"}})
        self.assertEqual((self.client.puts, self.book.tickets), ([], []))

    def test_rate_and_read_caps(self):
        m = self.maker(live=False, max_quotes_per_min=2)
        for i in range(4):
            m.handle(rfq_msg(rid=f"r{i}"))
        self.assertEqual(len(self.w.k("would_quote")), 2)
        self.assertEqual(m.st["why"].get("rate_cap"), 2)

    def test_state_survives_a_restart(self):
        m = self.maker(live=True)
        m.handle(rfq_msg())
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        again = cm.Book(os.path.join(self.d.name, "state.json"))
        self.assertEqual(len(again.open()), 1)
        self.assertAlmostEqual(again.open_loss(), 10 / 0.231 * (1 - 0.231), places=6)   # $10 at 23.1c

    def test_settlement_frees_the_exposure(self):
        m = self.maker(live=True)
        m.handle(rfq_msg())
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        m.handle({"type": "quote_executed", "msg": {"quote_id": "q1", "order_id": "o1"}})
        self.client.mk["KXMVECROSSCATEGORY-S2026X-Y"] = {"ticker": "KXMVECROSSCATEGORY-S2026X-Y", "result": "no"}
        m.settle()
        t = self.book.tickets[0]
        self.assertEqual(t["status"], "settled")
        self.assertAlmostEqual(t["pnl"], 10.0 - t["fee_est"])        # kept the $10 premium, less the fee
        self.assertAlmostEqual(t["fee_est"], 0.035 * (10 / 0.231) * 0.231 * 0.769)
        self.assertEqual(self.book.open(), [])


class LegMarkup(unittest.TestCase):
    """Jack 2026-11-05: "quote a higher spread, the more legs"."""

    def test_markup_and_last_look_grow_a_step_a_leg(self):
        cfg = cm.Config(markup=0.04, markup_per_leg=0.01)
        self.assertAlmostEqual(cfg.markup_for(2), 0.04)
        self.assertAlmostEqual(cfg.markup_for(5), 0.07)
        self.assertAlmostEqual(cfg.confirm_edge_for(2), 0.02)
        self.assertAlmostEqual(cfg.confirm_edge_for(5), 0.035)
        self.assertEqual(cm.quote_price(0.1234, cfg, n_legs=2), (0.129, 0.871))      # 0.128336 up
        self.assertEqual(cm.quote_price(0.1234, cfg, n_legs=5), (0.133, 0.867))      # 0.132038 up
        self.assertAlmostEqual(cm.Config(markup=0.04, markup_per_leg=0.0).markup_for(5), 0.04)

    def test_posted_price_and_last_look_use_the_leg_count(self):
        mk = dict(MK)
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = cm.Config(live=True, markup=0.05, markup_per_leg=0.02)
        w = W()
        m = cm.Maker(c, cfg, w, cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.handle(rfq_msg(legs=LEGS))                                          # 3 legs: 7%
        ask = cm.grid_up(FAIR * 1.07)
        self.assertEqual(c.posts[0][1]["no_bid"], f"{1 - ask:.4f}")
        self.assertAlmostEqual(w.k("quote")[0]["mu"], 0.07)
        # the books move: fair 0.21 -> 0.2184. Our 0.225 still clears the 2-leg bar
        # (fair x 1.025 = 0.2239) but not the 3-leg one (2.5% + 1% = 3.5%: 0.2260) -> declined
        c.mk[L1] = mkt(L1, 0.6140, 0.6340)
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": c.posts[0][1]["no_bid"]}})
        self.assertEqual(c.puts, [])
        self.assertEqual(w.k("decline")[0]["why"], "edge_gone")

    def test_cli_step_bounds(self):
        for bad in (["--markup-per-leg", "-0.01"], ["--markup-per-leg", "0.2"]):
            with self.assertRaises(SystemExit):
                cm.main(bad)


class NflOnly(unittest.TestCase):
    """The fee-free mode: all-NFL main lines at a 3% markup."""
    N1, N2, N3 = ("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08DALIND-DAL",
                  "KXNFLSPREAD-26NOV08DETMIA-DET3")

    def test_only_nfl_legs_and_half_markup_last_look(self):
        cfg = cm.Config(nfl_only=True, markup=0.03)
        self.assertAlmostEqual(cfg.confirm_min_edge, 0.015)
        self.assertEqual(cm.pre_check(LEGS, cfg), "not_nfl")
        legs = [[self.N1, "yes"], [self.N2, "yes"], [self.N3, "no"]]
        self.assertIsNone(cm.pre_check(legs, cfg))
        self.assertTrue(cfg.fee_free(legs))
        self.assertFalse(cfg.fee_free(LEGS))
        self.assertAlmostEqual(cm.Config().confirm_min_edge, 0.05)          # 10% default -> 5%

    def test_nfl_tickets_carry_no_fee_estimate(self):
        mk = {t: mkt(t, b, b + 0.02, start_in_h=60) for t, b in
              ((self.N1, 0.59), (self.N2, 0.49), (self.N3, 0.29))}
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        book = cm.Book(os.path.join(d.name, "s.json"))
        m = cm.Maker(c, cm.Config(live=True, nfl_only=True, markup=0.03), W(), book, d.name, clock=lambda: T0)
        m.handle(rfq_msg(legs=[[self.N1, "yes"], [self.N2, "yes"], [self.N3, "no"]]))
        qid = "q1"
        nb = c.posts[0][1]["no_bid"]
        m.handle({"type": "quote_accepted", "msg": {"quote_id": qid, "rfq_id": "r1", "no_bid_dollars": nb}})
        self.assertEqual(len(c.puts), 1)                                     # confirmed at +3%
        self.assertEqual(book.tickets[0]["fee_est"], 0.0)

    def test_cli_markup_floors(self):
        with self.assertRaises(SystemExit):
            cm.main(["--markup", "0.03"])                      # 3% only fee-free
        with self.assertRaises(SystemExit):
            cm.main(["--nfl-only", "--markup", "0.02"])


class GamesAndKnobs(unittest.TestCase):
    """--games / --min-legs / --min-lead-min / --allow-thin (11/05 night: the
    two NBA games as components)."""

    def test_games_filter_any_and_all(self):
        cfg = cm.Config(games={"26NOV05MIAGSW"}, min_legs=2)
        self.assertIsNone(cm.pre_check([[L1, "yes"], [L2, "yes"]], cfg))           # a MIA@GSW leg
        self.assertEqual(cm.pre_check([[L2, "yes"], [L3, "no"]], cfg), "not_games")
        cfg_all = cm.Config(games={"26NOV05MIAGSW", "26NOV05BOSTOR"}, games_mode="all", min_legs=2)
        self.assertIsNone(cm.pre_check([[L1, "yes"], [L2, "yes"]], cfg_all))
        self.assertEqual(cm.pre_check([[L1, "yes"], [L2, "yes"], [L3, "no"]], cfg_all), "not_games")

    def test_two_legs_and_a_short_lead(self):
        cfg = cm.Config(min_legs=2, min_lead_min=5)
        self.assertIsNone(cm.pre_check([[L1, "yes"], [L2, "yes"]], cfg))
        mk = {L1: mkt(L1, 0.59, 0.61, start_in_h=0.15), L2: mkt(L2, 0.49, 0.51)}   # 9 min to go
        self.assertIsNone(cm.price_check([[L1, "yes"], [L2, "yes"]], mk, T0, cfg)[0])
        self.assertEqual(cm.price_check([[L1, "yes"], [L2, "yes"]], mk, T0, cm.Config())[0], "starts_soon")

    def test_max_legs(self):
        cfg = cm.Config(min_legs=2, max_legs=5)
        six = [[f"KXNBAGAME-26NOV05G{i}H{i}-G{i}", "yes"] for i in range(6)]
        self.assertEqual(cm.pre_check(six, cfg), "legs")
        self.assertIsNone(cm.pre_check(six[:5], cfg))

    def test_cli_guards(self):
        for bad in (["--markup", "0.03"], ["--min-legs", "1"], ["--min-lead-min", "1"],
                    ["--allow-thin", "--markup", "0.02"], ["--min-legs", "3", "--max-legs", "2"],
                    ["--max-legs", "13"]):
            with self.assertRaises(SystemExit):
                cm.main(bad)


class MoreSports(unittest.TestCase):
    """Soccer (start rule checked), tennis / fights / first half (unchecked:
    an hour's margin at least), and the start memory that saves reads."""
    EPL1, EPL2 = "KXEPLGAME-26NOV08ARSCHE-ARS", "KXLALIGAGAME-26NOV08RMABAR-TIE"
    TEN = "KXATPCHALLENGERMATCH-26NOV06MONCEC-CEC"

    def test_groups_and_filters(self):
        cfg = cm.Config(min_legs=2, min_lead_min=5)
        self.assertIsNone(cm.pre_check([[self.EPL1, "yes"], [self.EPL2, "no"], [L1, "yes"]], cfg))
        self.assertIsNone(cm.pre_check([[self.TEN, "yes"], [L2, "yes"]], cfg))
        self.assertIsNone(cm.pre_check([[PROP, "yes"], [self.EPL1, "yes"]], cfg))
        self.assertEqual(cm.pre_check([["KXNBAPTSPROT-26NOV05ORLPHX-PHXDBOOKER15-25", "yes"],
                                       [self.EPL1, "yes"]], cfg), "not_main")             # PROT left out
        self.assertEqual(cm.lead_min(self.TEN, cfg), 60.0)
        self.assertEqual(cm.lead_min(self.EPL1, cfg), 5.0)
        self.assertEqual(cm.lead_min(L1, cfg), 5.0)

    def test_unchecked_sports_stop_an_hour_out(self):
        cfg = cm.Config(min_legs=2, min_lead_min=5)
        legs = [[self.TEN, "yes"], [L2, "yes"]]
        mk = {self.TEN: mkt(self.TEN, 0.59, 0.61, start_in_h=0.8), L2: mkt(L2, 0.49, 0.51)}   # 48 min out
        self.assertEqual(cm.price_check(legs, mk, T0, cfg)[0], "starts_soon")
        mk[self.TEN] = mkt(self.TEN, 0.59, 0.61, start_in_h=1.2)                                # 72 min out
        self.assertIsNone(cm.price_check(legs, mk, T0, cfg)[0])
        legs2 = [[self.EPL1, "yes"], [L2, "yes"]]
        mk2 = {self.EPL1: mkt(self.EPL1, 0.59, 0.61, start_in_h=0.2), L2: mkt(L2, 0.49, 0.51)}  # 12 min out
        self.assertIsNone(cm.price_check(legs2, mk2, T0, cfg)[0])                              # soccer: 5 min

    def test_a_known_started_leg_costs_no_read(self):
        mk = {L1: mkt(L1, 0.59, 0.61, start_in_h=0.05), L2: mkt(L2, 0.49, 0.51), L3: mkt(L3, 0.29, 0.31)}
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(c, cm.Config(min_legs=2), W(), cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(rid="r1"))                        # reads, learns L1 starts in 3 min
        n = len(c.gets)
        m.cache.clear()                                    # even with the price cache empty
        m.handle(rfq_msg(rid="r2"))
        self.assertEqual(len(c.gets), n)                   # skipped from the start memory
        self.assertEqual(m.st["why"].get("starts_soon"), 2)

    def test_read_cap_flag(self):
        with self.assertRaises(SystemExit):
            cm.main(["--max-reads-per-min", "5"])
        self.assertEqual(cm.Config().max_reads_per_min, 120)


class Extras(unittest.TestCase):
    """First-half / quarter / team-total lines and props on other games: $1,000
    at both sides of the book, the start from the game's winner market."""
    GAME = "KXNBAGAME-26NOV05ORLPHX-PHX"
    H1 = "KXNBA1QTOTAL-26NOV05ORLPHX-55"

    def legs(self, extra=PROP):
        return [[extra, "yes"], [L2, "yes"]]

    def test_same_game_prop_is_out(self):
        self.assertEqual(cm.pre_check([[PROP, "yes"], [self.GAME, "yes"]], cm.Config(min_legs=2)), "same_game")

    def test_depth_on_both_sides_and_the_game_start(self):
        cfg = cm.Config(min_legs=2)
        start = {"26NOV05ORLPHX": T0 + 2 * 3600}
        ok = {PROP: mkt(PROP, 0.59, 0.61, start_in_h=8, bid_size=2000, ask_size=2000), L2: mkt(L2, 0.49, 0.51)}
        self.assertIsNone(cm.price_check(self.legs(), ok, T0, cfg, start)[0])           # $1,180 / $1,220
        thin = dict(ok, **{PROP: mkt(PROP, 0.59, 0.61, start_in_h=8, bid_size=2000, ask_size=1500)})
        self.assertEqual(cm.price_check(self.legs(), thin, T0, cfg, start)[0], "thin")  # $915 at the ask
        self.assertEqual(cm.price_check(self.legs(), ok, T0, cfg, {})[0], "no_game_start")
        # the prop's own expiry is 8 h out, but its GAME starts in 20 min: out (lead 30)
        self.assertEqual(cm.price_check(self.legs(), ok, T0, cfg, {"26NOV05ORLPHX": T0 + 1200})[0], "starts_soon")
        self.assertIsNone(cm.price_check(self.legs(), ok, T0, cm.Config(min_legs=2, min_depth_usd=500), start)[0])

    def test_the_maker_reads_the_game_once_for_its_start(self):
        mk = {PROP: mkt(PROP, 0.59, 0.61, start_in_h=8, bid_size=2000, ask_size=2000),
              self.H1: mkt(self.H1, 0.49, 0.51, start_in_h=8, bid_size=3000, ask_size=3000),
              self.GAME: mkt(self.GAME, 0.55, 0.56, start_in_h=2, event="KXNBAGAME-26NOV05ORLPHX"),
              L2: mkt(L2, 0.49, 0.51)}
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, cm.Config(min_legs=2), w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(rid="r1", legs=self.legs()))
        self.assertAlmostEqual(m.game_start["26NOV05ORLPHX"], T0 + 2 * 3600)
        self.assertEqual(len(w.k("would_quote")), 1)
        game_reads = [p for p, prm in c.gets if "event_ticker" in prm]
        m.handle(rfq_msg(rid="r2", legs=[[self.H1, "no"], [L2, "yes"]]))
        self.assertEqual(len([p for p, prm in c.gets if "event_ticker" in prm]), len(game_reads))   # kept
        self.assertEqual(len(w.k("would_quote")), 2)

    def test_the_last_look_rechecks_the_depth(self):
        mk = {PROP: mkt(PROP, 0.59, 0.61, start_in_h=8, bid_size=2000, ask_size=2000),
              self.GAME: mkt(self.GAME, 0.55, 0.56, start_in_h=2, event="KXNBAGAME-26NOV05ORLPHX"),
              L2: mkt(L2, 0.49, 0.51)}
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, cm.Config(live=True, min_legs=2), w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(rid="r1", legs=self.legs()))
        nb = c.posts[0][1]["no_bid"]
        c.mk[PROP] = mkt(PROP, 0.59, 0.61, start_in_h=8, bid_size=100, ask_size=2000)   # the bid pulled
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": nb}})
        self.assertEqual(c.puts, [])
        self.assertEqual(w.k("decline")[0]["why"], "thin")

    def test_cli(self):
        with self.assertRaises(SystemExit):
            cm.main(["--max-quotes-per-min", "0"])
        with self.assertRaises(SystemExit):
            cm.main(["--min-depth", "50"])


class RunGuards(unittest.TestCase):
    def test_stop_file_and_lock(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, cm.STOP_NAME), "w").close()
            self.assertEqual(cm.run(cm.Config(), 1.0, out_dir=d, url="wss://unused.invalid"), 3)
            os.remove(os.path.join(d, cm.STOP_NAME))
            held = cm.lock_file(d)
            try:
                self.assertEqual(cm.run(cm.Config(), 1.0, out_dir=d, url="wss://unused.invalid"), 4)
            finally:
                held.close()


if __name__ == "__main__":
    unittest.main()
