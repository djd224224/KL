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
        self.orders = {}             # order id -> order; default: executed, |position| filled, NO
        self.post_error = None

    def get(self, path, params):
        self.gets.append((path, dict(params)))
        if path == "/markets" and "event_ticker" in params:
            return {"markets": [m for m in self.mk.values() if m.get("event_ticker") == params["event_ticker"]]}
        if path == "/markets":
            return {"markets": [self.mk[t] for t in params["tickers"].split(",") if t in self.mk]}
        if path.endswith("/orderbook"):
            return getattr(self, "book", None) or {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        if path.startswith("/portfolio/orders/"):
            oid = path.rsplit("/", 1)[1]
            return {"order": self.orders.get(oid) or {
                "order_id": oid, "status": "executed", "side": "no", "action": "buy",
                "fill_count_fp": f"{abs(self.position):.2f}", "maker_fees_dollars": "0.100000",
                "taker_fees_dollars": "0.000000"}}
        if path == "/portfolio/orders":
            return {"orders": [o for o in self.orders.values() if o.get("ticker") == params.get("ticker")]}
        if path == "/portfolio/positions":
            return {"market_positions": [{"ticker": params["ticker"], "position_fp": f"{self.position:.2f}"}]}
        raise AssertionError(path)

    def post(self, path, body):
        if self.post_error is not None:
            raise self.post_error
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
        m = self.maker(live=True, max_leg_move=0.0)    # the edge test alone (InPlay covers the move guard)
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
        self.assertAlmostEqual(self.book.tickets[0]["fee_est"], 0.035 * (10 / 0.231) * 0.231 * 0.769)  # table
        m.settle()                                                   # not before Kalshi's fill check
        self.assertEqual(self.book.tickets[0]["status"], "executed")
        self.clock[0] += 5
        m.upkeep()                                                   # books 40 filled, fee $0.10
        self.client.mk["KXMVECROSSCATEGORY-S2026X-Y"] = {"ticker": "KXMVECROSSCATEGORY-S2026X-Y", "result": "no"}
        m.settle()
        t = self.book.tickets[0]
        self.assertEqual(t["status"], "settled")
        self.assertAlmostEqual(t["pnl"], 40.0 * 0.231 - 0.10)        # kept the premium, less Kalshi's fee
        self.assertEqual(self.book.open(), [])


class FillCheck(unittest.TestCase):
    """11/06 03:04Z: quote_executed came for an order Kalshi canceled 5 s later
    with 0 filled; the maker booked it. Tickets now take Kalshi's order."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.clock = [T0]
        self.w = W()
        self.c = FakeClient(MK)
        self.book = cm.Book(os.path.join(self.d.name, "state.json"))
        self.m = cm.Maker(self.c, cm.Config(live=True, markup_per_leg=0.0), self.w, self.book, self.d.name,
                          clock=lambda: self.clock[0])

    def fill(self, executed=True):
        self.m.handle(rfq_msg())
        self.m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        if executed:
            self.m.handle({"type": "quote_executed", "msg": {"quote_id": "q1", "order_id": "o1"}})

    def order(self, status, filled, side="no", fee="0.050000", ticker="KXMVECROSSCATEGORY-S2026X-Y"):
        return {"order_id": "o1", "ticker": ticker, "status": status, "side": side, "action": "buy",
                "fill_count_fp": f"{filled:.2f}", "maker_fees_dollars": fee, "taker_fees_dollars": "0.000000"}

    def tick(self, s):
        self.clock[0] += s
        self.m.upkeep()

    def test_canceled_with_nothing_filled_is_dropped(self):
        self.fill()
        self.c.orders["o1"] = self.order("canceled", 0.0)
        self.tick(5)
        t = self.book.tickets[0]
        self.assertEqual((t["status"], t["contracts"], t["fee_est"]), ("unfilled", 0.0, 0.0))
        self.assertEqual((self.book.open(), self.book.open_loss()), ([], 0.0))
        self.assertEqual(self.book.day_count(t["day"]), 0)
        self.assertEqual(len(self.w.k("unfilled")), 1)
        self.assertFalse(any(p == "/portfolio/positions" for p, _ in self.c.gets))   # no position check on it

    def test_partial_fill_takes_kalshis_count_and_fee(self):
        self.fill()
        self.c.orders["o1"] = self.order("executed", 20.0, fee="0.041000")
        self.tick(5)
        t = self.book.tickets[0]
        self.assertEqual((t["status"], t["contracts"], t["fee_est"]), ("executed", 20.0, 0.041))
        self.assertAlmostEqual(t["contracts_quoted"], 10 / 0.231)
        self.assertAlmostEqual(self.book.open_loss(), 20.0 * 0.769)
        self.assertEqual(self.w.k("filled")[0]["contracts"], 20.0)

    def test_a_resting_order_is_read_again(self):
        self.fill()
        self.c.orders["o1"] = self.order("resting", 0.0)
        self.tick(5)
        self.assertNotIn("fill_checked", self.book.tickets[0])
        self.tick(1)                                          # within 5 s: no second read
        self.assertEqual(sum(1 for p, _ in self.c.gets if p == "/portfolio/orders/o1"), 1)
        self.c.orders["o1"] = self.order("executed", 43.29)
        self.tick(5)
        self.assertEqual((self.book.tickets[0]["status"], self.book.tickets[0]["contracts"]), ("executed", 43.29))

    def test_confirmed_without_an_executed_message(self):
        self.fill(executed=False)
        self.tick(5)                                          # no order yet, under a minute: wait
        self.assertEqual(self.book.tickets[0]["status"], "confirmed")
        self.tick(60)                                         # still none: nothing traded
        self.assertEqual(self.book.tickets[0]["status"], "unfilled")
        self.assertEqual(self.w.k("unfilled")[0]["why"], "no order on Kalshi")

    def test_confirmed_without_an_executed_message_but_filled(self):
        self.fill(executed=False)
        self.c.orders["o7"] = dict(self.order("executed", 43.29), order_id="o7")
        self.tick(5)
        t = self.book.tickets[0]
        self.assertEqual((t["status"], t["order_id"], t["contracts"]), ("executed", "o7", 43.29))

    def test_an_order_that_bought_yes_writes_stop(self):
        self.fill()
        self.c.orders["o1"] = self.order("executed", 43.29, side="yes")
        self.clock[0] += 5
        with self.assertRaises(SystemExit) as cx:
            self.m.upkeep()
        self.assertEqual(cx.exception.code, 5)
        self.assertTrue(os.path.exists(os.path.join(self.d.name, cm.STOP_NAME)))

    def test_a_ticket_booked_before_the_check_is_reconciled_on_start(self):
        old = {"quote_id": "qz", "mt": "KXMVECROSSCATEGORY-S2026Z-Z", "legs": LEGS, "games": ["g"], "ask": 0.166,
               "contracts": 21.0, "status": "executed", "order_id": "oz", "day": "2026-11-05", "at": T0 - 600,
               "executed_at": T0 - 599, "fee_est": 0.1, "dir_ok": False}
        self.book.tickets.append(old)
        self.c.orders["oz"] = dict(self.order("canceled", 0.0), order_id="oz")
        self.tick(0)
        self.assertEqual(self.book.tickets[0]["status"], "unfilled")
        again = cm.Book(os.path.join(self.d.name, "state.json"))
        self.assertEqual(again.open(), [])

    def test_rfq_closed_is_not_an_error(self):
        self.c.post_error = Exception('HttpError: {"error":{"code":"rfq_closed","message":"rfq closed"}}')
        self.m.handle(rfq_msg())
        self.assertEqual((self.m.errors, self.m.st["errors"], self.w.k("err")), (0, 0, []))
        self.assertEqual(self.m.st["why"].get("rfq_closed"), 1)
        self.assertEqual(self.m.pause_until, 0.0)


class Horizon(unittest.TestCase):
    """Jack 2026-11-05: 'dont quote anything further than 4 days out, to not
    tie up cash.'"""

    def test_a_leg_that_settles_past_the_horizon(self):
        cfg = cm.Config()
        far = dict(MK, **{L2: mkt(L2, 0.49, 0.51, start_in_h=94)})            # settles in 97 h > 96
        near = dict(MK, **{L2: mkt(L2, 0.49, 0.51, start_in_h=92)})           # settles in 95 h
        self.assertEqual(cm.price_check(LEGS, far, T0, cfg)[0], "too_far")
        self.assertIsNone(cm.price_check(LEGS, near, T0, cfg)[0])
        self.assertIsNone(cm.price_check(LEGS, far, T0, cm.Config(max_days_out=0))[0])   # off
        self.assertIsNone(cm.price_check(LEGS, far, T0, cm.Config(max_days_out=5))[0])

    def test_a_known_far_leg_is_skipped_without_a_read(self):
        mk = dict(MK, **{L2: mkt(L2, 0.49, 0.51, start_in_h=120)})
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(c, cm.Config(), W(), cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.handle(rfq_msg(rid="r1"))
        self.assertEqual(m.st["why"].get("too_far"), 1)
        n = len(c.gets)
        m.handle(rfq_msg(rid="r2"))                                           # L2 is known: no read
        self.assertEqual((m.st["why"].get("too_far"), len(c.gets)), (2, n))

    def test_cli(self):
        with self.assertRaises(SystemExit):
            cm.main(["--max-days-out", "0.1"])


class TicketLoss(unittest.TestCase):
    """Jack 2026-11-05: 'allow a single loss of up to $300'."""

    def test_one_tickets_loss_is_capped(self):
        cfg = cm.Config(max_ticket_loss=300, max_payout=500, max_game_payout=600)
        b = cm.Book()
        day = cm.et_day(T0)
        self.assertEqual(cm.caps_check(b, ["g1"], 429, 0.30, cfg, day), "ticket_loss_cap")   # loses $300.30
        self.assertIsNone(cm.caps_check(b, ["g1"], 428, 0.30, cfg, day))                    # loses $299.60
        self.assertEqual(cm.caps_check(b, ["g1"], 311, 0.035, cfg, day), "ticket_loss_cap")  # $300.12
        self.assertIsNone(cm.caps_check(b, ["g1"], 310, 0.035, cfg, day))                   # $299.15
        self.assertEqual(cm.caps_check(b, ["g1"], 501, 0.45, cfg, day), "payout_cap")      # the backstop
        # off by default: only the $150 payout limits a ticket
        self.assertIsNone(cm.Config().max_ticket_loss)
        self.assertEqual(cm.caps_check(b, ["g1"], 200, 0.30, cm.Config(), day), "payout_cap")

    def test_cli_bounds(self):
        for bad in (["--max-ticket-loss", "5"], ["--max-ticket-loss", "6000"]):
            with self.assertRaises(SystemExit):
                cm.main(bad)


class Senders(unittest.TestCase):
    """11/06 04:05Z: at 150 quotes a minute the one loop was full (each POST ~270
    ms, each leg read ~133 ms) and RFQs reached it 1.2 s old. Quotes go out from
    sender threads now."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.w = W()

    def maker(self, c, workers=2):
        cfg = cm.Config(live=True, quote_workers=workers, markup_per_leg=0.0)
        m = cm.Maker(c, cfg, self.w, cm.Book(os.path.join(self.d.name, "s.json")), self.d.name,
                     clock=lambda: T0)
        self.addCleanup(m.close)
        return m

    def test_quotes_go_out_from_the_pool(self):
        c = FakeClient(MK)
        m = self.maker(c)
        for i in range(3):
            m.handle(rfq_msg(rid=f"r{i}"))
        m.close()
        self.assertEqual(len(c.posts), 3)
        self.assertEqual(len(self.w.k("quote")), 3)
        self.assertEqual(set(m.quotes), {"q1", "q2", "q3"})
        self.assertEqual((m.pending, m.st["quoted"]), ({}, 3))

    def test_an_accept_that_beats_our_posts_response(self):
        import threading

        class Slow(FakeClient):
            gate = threading.Event()

            def post(self, path, body):
                self.gate.wait(5)
                return super().post(path, body)

        c = Slow(MK)
        m = self.maker(c)
        m.handle(rfq_msg(rid="r1"))
        self.assertIn("r1", m.pending)                       # the POST is still out
        nb = f"{m.pending['r1']['no_bid']:.4f}"
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "qX", "rfq_id": "r1", "no_bid_dollars": nb}})
        self.assertEqual(c.puts, [FakeClient.host + "/communications/rfqs/r1/quotes/qX/confirm"])
        self.assertEqual(m.book.tickets[0]["quote_id"], "qX")
        Slow.gate.set()
        m.close()
        self.assertEqual(m.pending, {})

    def test_a_senders_error_counts_and_pauses(self):
        c = FakeClient(MK)
        c.post_error = Exception('HttpError: {"error":{"code":"insufficient_balance","message":"insufficient balance"}}')
        m = self.maker(c)
        m.handle(rfq_msg(rid="r1"))
        m.close()
        self.assertEqual((m.errors, len(self.w.k("err")), m.pending), (1, 1, {}))
        self.assertEqual(m.pause_until, T0 + 300)

    def test_min_gap_sleeps_only_what_is_left(self):
        t, slept = [100.0], []

        def sleep(s):
            slept.append(round(s, 6))
            t[0] += s
        g = cm.MinGap(0.05, clock=lambda: t[0], sleep=sleep)
        g()                                                  # first call: no wait
        t[0] += 0.02
        g()                                                  # 20 ms after: sleeps the other 30
        t[0] += 0.10
        g()                                                  # 100 ms after: none
        self.assertEqual(slept, [0.03])

    def test_cli(self):
        with self.assertRaises(SystemExit):
            cm.main(["--quote-workers", "17"])


class InPlay(unittest.TestCase):
    """11/06 04:21-04:33Z: 6 tickets sold on ITF women's matches already in play."""
    ITF = "KXITFWMATCH-26NOV05SHAWAN-SHA"

    def test_jacks_sports_only(self):
        legs = [[self.ITF, "yes"], [L2, "yes"]]
        self.assertEqual(cm.pre_check(legs, cm.Config(min_legs=2)), "sport_off")            # tennis
        self.assertIsNone(cm.pre_check(legs, cm.Config(min_legs=2, sports={"TENNIS", "NHL"})))
        self.assertIsNone(cm.pre_check(LEGS, cm.Config()))                                  # NBA NHL NCAAMB
        ufc = [["KXUFCFIGHT-26NOV06CORWAL-WAL", "yes"], [L2, "yes"]]
        self.assertIsNone(cm.pre_check(ufc, cm.Config(min_legs=2)))
        box = [["KXBOXING-26NOV07ABCDEF-ABC", "yes"], [L2, "yes"]]
        self.assertEqual(cm.pre_check(box, cm.Config(min_legs=2)), "sport_off")
        self.assertEqual([cm.sport_of(s) for s in ("KXNBAPTS", "KXEPLGAME", "KXNCAAF1HSPREAD", "KXATPMATCH")],
                         ["NBA", "SOCCER", "NCAAF", "TENNIS"])
        with self.assertRaises(SystemExit):
            cm.main(["--sports", "NBA,CRICKET"])
        exp = T0 + 20 * 3600
        self.assertEqual(cm.start_of("KXUFCFIGHT-26NOV06CORWAL-WAL", exp), exp - 8 * 3600)

    def test_ncaaf_starts_six_hours_before_its_expiry(self):
        exp = T0 + 10 * 3600
        self.assertEqual(cm.start_of("KXNCAAFGAME-26NOV07IOWANW-IOWA", exp), exp - 6 * 3600)
        self.assertEqual(cm.start_of("KXNCAAFSPREAD-26NOV07IOWANW-IOWA3", exp), exp - 6 * 3600)
        self.assertEqual(cm.start_of("KXNFLGAME-26NOV08LVSF-LV", exp), exp - 3 * 3600)
        cfg = cm.Config()
        n = "KXNCAAFGAME-26NOV07IOWANW-IOWA"
        mk = dict(MK, **{n: mkt(n, 0.49, 0.51, start_in_h=2.0)})       # expires 5 h out: started 1 h ago
        self.assertEqual(cm.price_check([[L1, "yes"], [n, "yes"], [L3, "no"]], mk, T0, cfg)[0], "starts_soon")

    def test_a_moving_leg_is_held_out(self):
        c = FakeClient(MK)
        clock = [T0]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, cm.Config(live=True, markup_per_leg=0.0), w, cm.Book(os.path.join(d.name, "s.json")),
                     d.name, clock=lambda: clock[0])
        m.handle(rfq_msg(rid="r1"))
        self.assertEqual(len(c.posts), 1)
        def fresh(rid):
            msg = rfq_msg(rid=rid)
            msg["msg"]["created_ts"] = iso(clock[0] - 0.2)
            return msg
        clock[0] += 60
        c.mk[L2] = mkt(L2, 0.55, 0.57)                              # 0.50 -> 0.56 in a minute: in play
        m.handle(fresh("r2"))
        self.assertEqual((len(c.posts), m.st["why"].get("moving")), (1, 1))
        clock[0] += 60
        n = len(c.gets)
        m.handle(fresh("r3"))                                       # held out: skipped before any read
        self.assertEqual((m.st["why"].get("moving"), len(c.gets)), (2, n))
        clock[0] += cm.MOVE_HOLD_S + 1
        m.handle(fresh("r4"))                                       # the hold is over, the leg is still
        self.assertEqual(len(c.posts), 2)

    def test_the_last_look_declines_a_moving_leg(self):
        c = FakeClient(MK)
        clock = [T0]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, cm.Config(live=True, markup_per_leg=0.0), w, cm.Book(os.path.join(d.name, "s.json")),
                     d.name, clock=lambda: clock[0])
        m.handle(rfq_msg(rid="r1"))
        nb = c.posts[0][1]["no_bid"]
        clock[0] += 5
        c.mk[L3] = mkt(L3, 0.22, 0.24)                              # 0.30 -> 0.23 between quote and accept
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": nb}})
        self.assertEqual(c.puts, [])
        self.assertEqual(w.k("decline")[0]["why"], "moving")


class FillPrice(unittest.TestCase):
    """11/06: two orders filled as the taker at a better NO price than quoted."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.w = W()
        self.c = FakeClient(MK)
        self.book = cm.Book(os.path.join(self.d.name, "s.json"))
        self.clock = [T0]
        self.m = cm.Maker(self.c, cm.Config(live=True, markup_per_leg=0.0), self.w, self.book, self.d.name,
                          clock=lambda: self.clock[0])

    def test_a_taker_fill_at_a_better_price_is_booked_at_it(self):
        self.m.handle(rfq_msg())
        self.m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1", "no_bid_dollars": "0.7690"}})
        self.m.handle({"type": "quote_executed", "msg": {"quote_id": "q1", "order_id": "o1"}})
        self.c.orders["o1"] = {"order_id": "o1", "status": "executed", "side": "no", "action": "buy",
                               "fill_count_fp": "17.47", "maker_fill_cost_dollars": "0.000000",
                               "taker_fill_cost_dollars": f"{17.47 * 0.759:.6f}", "maker_fees_dollars": "0.000000",
                               "taker_fees_dollars": "0.258300"}
        self.clock[0] += 5
        self.m.upkeep()
        t = self.book.tickets[0]
        self.assertAlmostEqual(t["ask"], 0.241, places=6)                    # 1 - 0.759, not the quoted 0.231
        self.assertEqual((t["ask_quoted"], t["taker_fill"], t["fee_est"]), (0.231, True, 0.2583))
        self.assertAlmostEqual(self.book.open_loss(), 17.47 * 0.759, places=6)

    def test_tickets_checked_before_the_price_was_read_get_it_once(self):
        old = {"quote_id": "qz", "mt": "KXMVECROSSCATEGORY-S2026Z-Z", "legs": LEGS, "games": ["g"], "ask": 0.293,
               "contracts": 17.47, "status": "executed", "order_id": "oz", "day": "2026-11-05", "at": T0 - 600,
               "executed_at": T0 - 599, "fee_est": 0.2583, "fill_checked": True, "dir_ok": True}
        self.book.tickets.append(old)
        self.c.orders["oz"] = {"order_id": "oz", "status": "executed", "side": "no", "action": "buy",
                               "fill_count_fp": "17.47", "taker_fill_cost_dollars": f"{17.47 * 0.697:.6f}",
                               "taker_fees_dollars": "0.258300"}
        self.m.upkeep()
        t = self.book.tickets[0]
        self.assertAlmostEqual(t["ask"], 0.303, places=6)
        self.assertTrue(t["px_checked"])
        n = len(self.c.gets)
        self.clock[0] += 10
        self.m.upkeep()                                                       # read once
        self.assertEqual(len([g for g in self.c.gets[n:] if g[0].startswith("/portfolio/orders/")]), 0)


class MakerOnly(unittest.TestCase):
    """Jack 2026-11-06: "it should only ever be a maker"."""

    def maker(self, c):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.w = W()
        return cm.Maker(c, cm.Config(live=True, markup_per_leg=0.0), self.w, cm.Book(os.path.join(d.name, "s.json")),
                        d.name, clock=lambda: T0)

    def accept(self, m, c):
        m.handle(rfq_msg())
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": c.posts[0][1]["no_bid"]}})

    def test_a_resting_yes_bid_at_or_over_our_price_is_declined(self):
        c = FakeClient(MK)
        c.book = {"orderbook_fp": {"yes_dollars": [["0.2410", "17.47"]], "no_dollars": []}}    # we sell at 0.231
        m = self.maker(c)
        self.accept(m, c)
        self.assertEqual(c.puts, [])
        self.assertEqual(self.w.k("decline")[0]["why"], "would_take")

    def test_an_empty_or_lower_book_confirms(self):
        for book in (None, {"orderbook_fp": {"yes_dollars": [["0.2200", "40.00"]], "no_dollars": [["0.7000", "5"]]}}):
            c = FakeClient(MK)
            c.book = book
            m = self.maker(c)
            self.accept(m, c)
            self.assertEqual(len(c.puts), 1)

    def test_the_book_forms(self):
        self.assertTrue(cm.would_take({"orderbook_fp": {"yes_dollars": [["0.3030", "17.47"]]}}, 0.293))
        self.assertTrue(cm.would_take({"orderbook_fp": {"yes_dollars": [["0.2930", "1.00"]]}}, 0.293))   # equal
        self.assertFalse(cm.would_take({"orderbook_fp": {"yes_dollars": [["0.2920", "9"]]}}, 0.293))
        self.assertTrue(cm.would_take({"orderbook": {"yes": [[31, 10]]}}, 0.293))                       # cents form
        self.assertFalse(cm.would_take({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, 0.293))


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
              ((self.N1, 0.59), (self.N2, 0.49), (self.N3, 0.29), ("KXNFLGAME-26NOV08DETMIA-DET", 0.6))}
        c = FakeClient(mk)                                  # (the spread's game: its winner market)
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


class NflCut(unittest.TestCase):
    """Jack 2026-11-06: "NFL parlays require 2c less edge because there are no maker fees"."""
    N1, N2, N3 = NflOnly.N1, NflOnly.N2, NflOnly.N3

    def cfg(self, **kw):
        return cm.Config(markup=0.04, markup_per_leg=0.01, nfl_markup_cut=0.02, confirm_edge_floor=0.01, **kw)

    def test_two_points_off_at_the_quote_and_at_the_last_look(self):
        cfg = self.cfg()
        nfl, nfl3 = [[self.N1, "yes"], [self.N2, "yes"]], [[self.N1, "yes"], [self.N2, "yes"], [self.N3, "no"]]
        mixed = [[self.N1, "yes"], [L1, "yes"]]
        self.assertAlmostEqual(cfg.markup_for(2, nfl), 0.02)
        self.assertAlmostEqual(cfg.markup_for(3, nfl3), 0.03)
        self.assertAlmostEqual(cfg.markup_for(2, mixed), 0.04)
        self.assertAlmostEqual(cfg.markup_for(2), 0.04)                       # no legs given: as before
        self.assertAlmostEqual(cfg.confirm_edge_for(2, nfl), 0.0)             # the half-markup bar: 2% - 2%
        self.assertAlmostEqual(cfg.confirm_edge_for(3, nfl3), 0.005)
        nfl5 = nfl3 + [["KXNFLGAME-26NOV09NYGPHI-NYG", "yes"], ["KXNFLGAME-26NOV09GBCHI-GB", "no"]]
        self.assertAlmostEqual(cfg.confirm_edge_for(5, nfl5), 0.015)
        self.assertAlmostEqual(cm.net_edge(0.306, 0.30, nfl, cfg), 0.02)      # no fee: the 1% floor is gross
        self.assertAlmostEqual(cfg.confirm_edge_for(2, mixed), 0.02)
        self.assertEqual(cm.quote_price(0.30, cfg, n_legs=2, legs=nfl), (0.306, 0.694))
        self.assertEqual(cm.quote_price(0.30, cfg, n_legs=2, legs=mixed), (0.312, 0.688))
        self.assertAlmostEqual(cm.Config(markup=0.04).markup_for(2, nfl), 0.04)  # a bare Config: no cut

    def run_one(self, legs, after):
        mk = {t: mkt(t, b, b + 0.02, start_in_h=60) for t, b in ((self.N1, 0.59), (self.N2, 0.49), (L1, 0.49))}
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, self.cfg(live=True, min_legs=2), w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(legs=legs))                                          # fair 0.60 x 0.50 = 0.30
        for t, (b, a) in after.items():
            c.mk[t] = mkt(t, b, a, start_in_h=60)
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": c.posts[0][1]["no_bid"]}})
        return c, w

    def test_an_all_nfl_parlay_is_quoted_and_confirmed_2_points_lower(self):
        nfl = [[self.N1, "yes"], [self.N2, "yes"]]
        c, w = self.run_one(nfl, {self.N1: (0.594, 0.614)})                  # N1 0.60 -> 0.604: fair 0.302
        self.assertEqual(c.posts[0][1]["no_bid"], "0.6940")                  # 0.306 = 0.30 x 1.02, not 0.312
        self.assertAlmostEqual(w.k("quote")[0]["mu"], 0.02)
        self.assertEqual(len(c.puts), 1)                                     # 0.306 >= 0.302 x 1.01 = 0.30502
        c, w = self.run_one(nfl, {self.N1: (0.60, 0.62)})                    # N1 -> 0.61: fair 0.305
        self.assertEqual(c.puts, [])                                         # 0.306 < 0.305 x 1.01: the floor
        self.assertEqual(w.k("decline")[0]["why"], "net_edge")

    def test_a_parlay_with_any_other_leg_keeps_the_markup(self):
        c, w = self.run_one([[self.N1, "yes"], [L1, "yes"]], {})
        self.assertEqual(c.posts[0][1]["no_bid"], "0.6880")                  # 0.312 = 0.30 x 1.04
        self.assertAlmostEqual(w.k("quote")[0]["mu"], 0.04)

    def test_cli_bounds(self):
        for bad in (["--nfl-markup-cut", "-0.01"],
                    ["--allow-thin", "--markup", "0.04", "--nfl-markup-cut", "0.04"],         # none left
                    ["--confirm-edge-floor", "-0.01"],
                    ["--allow-thin", "--markup", "0.04", "--confirm-edge-floor", "0.02"]):    # = 4% - 2%
            with self.assertRaises(SystemExit):
                cm.main(bad)


class FeeFloor(unittest.TestCase):
    """Jack 2026-11-06: "i want last look to have at least 1% edge" -- "i mean 1% edge, after fees"."""
    N1, N2 = NflOnly.N1, NflOnly.N2

    def cfg(self, **kw):
        return cm.Config(markup=0.04, markup_per_leg=0.01, nfl_markup_cut=0.02, confirm_edge_floor=0.01, **kw)

    def test_the_edge_after_the_fee(self):
        cfg, two, nfl = self.cfg(), [[L1, "yes"], [L2, "yes"]], [[self.N1, "yes"], [self.N2, "yes"]]
        self.assertAlmostEqual(cm.fee_per_contract(0.30, two, cfg), 0.035 * 0.30 * 0.70)
        self.assertEqual(cm.fee_per_contract(0.30, nfl, cfg), 0.0)
        self.assertAlmostEqual(cm.net_edge(0.312, 0.30, two, cfg), (0.312 - 0.035 * 0.312 * 0.688) / 0.30 - 1)
        # the lowest price that keeps 1% after the fee: fair 10c -> 10.5c (10.4c leaves 0.74%); all-NFL 10.1c
        self.assertEqual(cm.net_floor_price(0.10, two, cfg), 0.105)
        self.assertEqual(cm.net_floor_price(0.10, nfl, cfg), 0.101)
        self.assertLess(cm.net_edge(0.104, 0.10, two, cfg), 0.01)
        self.assertGreaterEqual(cm.net_edge(0.105, 0.10, two, cfg), 0.01)
        for f in (0.02, 0.031, 0.077, 0.15, 0.2999, 0.40):                      # the lowest that clears
            p = cm.net_floor_price(f, two, cfg)
            self.assertGreaterEqual(cm.net_edge(p, f, two, cfg), 0.01 - 1e-9)
            self.assertLess(cm.net_edge(p - 0.001, f, two, cfg), 0.01)

    def maker(self, mk):
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, self.cfg(live=True, min_legs=2), w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        return c, w, m

    def accept(self, c, m):
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": c.posts[0][1]["no_bid"]}})

    def test_a_quote_the_last_look_would_refuse_goes_out_a_tick_up(self):
        c, w, m = self.maker({L1: mkt(L1, 0.19, 0.21), L2: mkt(L2, 0.49, 0.51)})    # fair 0.20 x 0.50 = 0.10
        m.handle(rfq_msg(legs=[[L1, "yes"], [L2, "yes"]]))
        self.assertEqual(c.posts[0][1]["no_bid"], "0.8950")                  # 10.5c, not the markup's 10.4c
        q = w.k("quote")[0]
        self.assertEqual((q["raised_from"], q["ask"], q["mu"]), (0.104, 0.105, 0.04))
        self.accept(c, m)
        self.assertEqual(len(c.puts), 1)                                     # the fair held: it confirms

    def test_the_last_look_wants_1pct_after_the_fee(self):
        c, w, m = self.maker(MK)                                             # fair 0.60 x 0.50 = 0.30
        m.handle(rfq_msg(legs=[[L1, "yes"], [L2, "yes"]]))
        self.assertEqual(c.posts[0][1]["no_bid"], "0.6880")                  # 31.2c: 1.5% after the fee, kept
        self.assertNotIn("raised_from", w.k("quote")[0])
        c.mk[L1] = mkt(L1, 0.594, 0.614)                                     # fair 0.302: 0.312 clears the 2%
        self.accept(c, m)                                                    # bar (0.3080) but not 1% net
        self.assertEqual(c.puts, [])
        self.assertEqual(w.k("decline")[0]["why"], "net_edge")

    def test_all_nfl_pays_no_fee(self):
        mk = {t: mkt(t, b, b + 0.02, start_in_h=60) for t, b in ((self.N1, 0.59), (self.N2, 0.49))}
        c, w, m = self.maker(mk)
        m.handle(rfq_msg(legs=[[self.N1, "yes"], [self.N2, "yes"]]))
        self.assertEqual(c.posts[0][1]["no_bid"], "0.6940")                  # 30.6c: 2%, no fee, not raised
        self.assertNotIn("raised_from", w.k("quote")[0])
        self.accept(c, m)
        self.assertEqual(len(c.puts), 1)

    def test_off_at_zero(self):
        c, w, m = self.maker({L1: mkt(L1, 0.19, 0.21), L2: mkt(L2, 0.49, 0.51)})
        m.cfg.confirm_edge_floor = 0.0
        m.handle(rfq_msg(legs=[[L1, "yes"], [L2, "yes"]]))
        self.assertEqual(c.posts[0][1]["no_bid"], "0.8960")                  # the markup's 10.4c


class FloorStep(unittest.TestCase):
    """Jack 2026-11-06: "1% would be the floor for 2 legs. and then step it up .5% each leg so 1.5% 3 leg,
    2% 4 leg, 2.5% 5 leg, etc"."""

    def cfg(self, **kw):
        return cm.Config(markup=0.04, markup_per_leg=0.01, nfl_markup_cut=0.02, confirm_edge_floor=0.01,
                         confirm_floor_per_leg=0.005, **kw)

    def test_the_ladder(self):
        cfg = self.cfg()
        for n, want in ((2, 0.01), (3, 0.015), (4, 0.02), (5, 0.025), (8, 0.04)):
            self.assertAlmostEqual(cfg.net_floor_for(n), want)
        self.assertAlmostEqual(cm.Config(confirm_edge_floor=0.01).net_floor_for(5), 0.01)   # no step: flat

    def test_a_3_leg_parlay_wants_1_5pct_after_the_fee(self):
        legs = LEGS                                                          # NBA / NHL / NCAAMB: fee-paying
        cfg = self.cfg()
        # the floor price at fair 0.21: 1.5% after the fee
        p = cm.net_floor_price(FAIR, legs, cfg)
        self.assertGreaterEqual(cm.net_edge(p, FAIR, legs, cfg), 0.015 - 1e-9)
        self.assertLess(cm.net_edge(p - 0.001, FAIR, legs, cfg), 0.015)
        c = FakeClient(MK)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, self.cfg(live=True), w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(legs=legs))                                         # 3 legs: 5% -> 0.21 x 1.05 = 0.2205
        ask = cm.grid_up(FAIR * 1.05)
        self.assertEqual(c.posts[0][1]["no_bid"], f"{1 - ask:.4f}")          # 2.5% after the fee: kept
        self.assertNotIn("raised_from", w.k("quote")[0])
        # our 0.221; fair 0.21 -> 0.21245 (L1 0.60 -> 0.607): 1.19% after the fee, under the 3-leg 1.5%
        # -> declined, though it clears the 2-leg 1% and the half-markup 2.5% gross bar (0.21776)
        c.mk[L1] = mkt(L1, 0.597, 0.617)
        fair_now = 0.607 * 0.50 * 0.70
        self.assertGreater(ask, fair_now * 1.025)
        self.assertLess(cm.net_edge(ask, fair_now, legs, cfg), 0.015)
        self.assertGreaterEqual(cm.net_edge(ask, fair_now, legs, cfg), 0.01 - 1e-9)
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": c.posts[0][1]["no_bid"]}})
        self.assertEqual(c.puts, [])
        self.assertEqual(w.k("decline")[0]["why"], "net_edge")

    def test_cli_bounds(self):
        for bad in (["--confirm-floor-per-leg", "-0.001"],
                    ["--allow-thin", "--markup", "0.04", "--confirm-floor-per-leg", "0.02"]):   # > the 1% step
            with self.assertRaises(SystemExit):
                cm.main(bad)


class LongShots(unittest.TestCase):
    """Jack 2026-11-06: "longer shot should charge more edge right?" -- "Yes, add it"."""
    N1, N2 = NflOnly.N1, NflOnly.N2

    def cfg(self, **kw):
        return cm.Config(markup=0.04, markup_per_leg=0.01, nfl_markup_cut=0.02,
                         longshot_markup=((0.05, 0.02), (0.10, 0.01)), **kw)

    def test_the_steps(self):
        cfg, two = self.cfg(), [[L1, "yes"], [L2, "yes"]]
        six = [[f"KXNBAGAME-26NOV07G{i}-X", "yes"] for i in range(6)]
        self.assertAlmostEqual(cfg.markup_for(2, two, 0.03), 0.06)           # under 5c: +2
        self.assertAlmostEqual(cfg.markup_for(2, two, 0.0499), 0.06)
        self.assertAlmostEqual(cfg.markup_for(2, two, 0.05), 0.05)           # 5-10c: +1
        self.assertAlmostEqual(cfg.markup_for(2, two, 0.0999), 0.05)
        self.assertAlmostEqual(cfg.markup_for(2, two, 0.10), 0.04)           # 10c and up: none
        self.assertAlmostEqual(cfg.markup_for(6, six, 0.03), 0.10)           # 8% for 6 legs + 2
        self.assertAlmostEqual(cfg.markup_for(2, [[self.N1, "yes"], [self.N2, "yes"]], 0.04), 0.04)  # NFL 2% + 2
        self.assertAlmostEqual(cfg.markup_for(2, two), 0.04)                 # no fair: the leg ladder
        self.assertAlmostEqual(cm.Config(markup=0.04).markup_for(2, two, 0.03), 0.04)   # a bare Config: none
        self.assertAlmostEqual(cfg.confirm_edge_for(2, two), 0.02)           # the last look is unchanged
        self.assertEqual(cm.quote_price(0.04, cfg, n_legs=3, legs=LEGS), (0.043, 0.957))  # 0.04 x 1.07 up
        self.assertEqual(cm.quote_price(0.04, cm.Config(markup=0.04, markup_per_leg=0.01), n_legs=3, legs=LEGS),
                         (0.042, 0.958))

    def test_a_long_shot_is_quoted_at_the_higher_markup(self):
        mk = {L1: mkt(L1, 0.19, 0.21), L2: mkt(L2, 0.24, 0.26), L3: mkt(L3, 0.19, 0.21)}   # 0.2 x 0.25 x 0.8
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        w = W()
        m = cm.Maker(c, self.cfg(live=True, confirm_edge_floor=0.01, confirm_floor_per_leg=0.005), w,
                     cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.handle(rfq_msg(legs=LEGS, tc="5.0000"))
        self.assertEqual(c.posts[0][1]["no_bid"], "0.9570")                  # 4.3c: 5% + 2%, not 4.2c
        q = w.k("quote")[0]
        self.assertAlmostEqual(q["fair"], 0.04)
        self.assertAlmostEqual(q["mu"], 0.07)
        self.assertNotIn("raised_from", q)

    def test_cli(self):
        self.assertEqual(cm.parse_longshot("0.10:0.01,0.05:0.02"), ((0.05, 0.02), (0.10, 0.01)))
        self.assertEqual(cm.parse_longshot(""), ())
        for bad in ("0.05:-0.01", "abc", "0.5:0.01", "0.05:0.3"):
            with self.assertRaises(SystemExit):
                cm.main(["--longshot-markup", bad])

    def test_the_cli_default_has_three_steps(self):
        """Jack 2026-11-06: "and +0.5% when fair is under 20"."""
        got, run = {}, cm.run
        cm.run = lambda cfg, minutes: got.setdefault("cfg", cfg) and 0
        try:
            cm.main(["--allow-thin", "--markup", "0.04"])
        finally:
            cm.run = run
        cfg = got["cfg"]
        self.assertEqual(cfg.longshot_markup, ((0.05, 0.02), (0.10, 0.01), (0.20, 0.005)))
        two = [[L1, "yes"], [L2, "yes"]]
        for fair, extra in ((0.03, 0.02), (0.07, 0.01), (0.15, 0.005), (0.1999, 0.005), (0.20, 0.0), (0.35, 0.0)):
            self.assertAlmostEqual(cfg.markup_for(2, two, fair), 0.04 + extra)


class RequesterGuard(unittest.TestCase):
    """Jack 2026-11-06: "Yes, build it" -- 11/06 two requesters accepted 3 of 5 and 2 of 4 of our quotes (-$130)."""

    def maker(self, d, **kw):
        self.w = W()
        cfg = cm.Config(live=True, markup_per_leg=0.0, guard_min_accepts=2, guard_min_rate=0.10, **kw)
        return cm.Maker(self.c, cfg, self.w, cm.Book(os.path.join(d, "s.json")), d, clock=lambda: T0)

    def accept(self, m, qid, rid):
        m.handle({"type": "quote_accepted", "msg": {"quote_id": qid, "rfq_id": rid,
                                                      "no_bid_dollars": self.c.posts[-1][1]["no_bid"]}})

    def test_the_second_accept_at_a_high_rate_is_declined_and_blocks(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.c = FakeClient(MK)
        m = self.maker(d.name)
        m.handle(rfq_msg("r1"))
        self.accept(m, "q1", "r1")
        self.assertEqual(len(self.c.puts), 1)                               # the first fill confirms
        m.handle(rfq_msg("r2"))
        self.accept(m, "q2", "r2")
        self.assertEqual(len(self.c.puts), 1)                               # 2 of 2: declined
        self.assertEqual(self.w.k("decline")[-1]["why"], "requester_flagged")
        blk = self.w.k("requester_block")[0]
        self.assertEqual((blk["cr"], blk["quotes"], blk["accepts"]), ("abcdef01", 2, 2))
        m.handle(rfq_msg("r3"))
        self.assertEqual(len(self.c.posts), 2)                              # blocked: no quote
        self.assertEqual(m.st["why"]["requester_blocked"], 1)
        again = self.maker(d.name)                                          # a restart keeps the block
        self.assertGreater(again.blocked["abcdef01"], T0)
        self.assertEqual(again.req["abcdef01"]["a"], 2)
        self.assertEqual(m.book.tickets[0]["cr"], "abcdef01")                # tickets carry the requester

    def test_a_low_rate_requester_is_not_flagged(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.c = FakeClient(MK)
        m = self.maker(d.name)
        m.req["abcdef01"] = {"q": 98, "a": 1, "seen": T0}                    # one accept in 98 quotes
        m.handle(rfq_msg("r1"))
        self.accept(m, "q1", "r1")                                          # 2 of 99: 2%
        self.assertEqual(len(self.c.puts), 1)
        self.assertEqual(self.w.k("requester_block"), [])

    def test_off(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.c = FakeClient(MK)
        m = self.maker(d.name)
        m.cfg.guard_min_accepts = 0
        for rid, qid in (("r1", "q1"), ("r2", "q2")):
            m.handle(rfq_msg(rid))
            self.accept(m, qid, rid)
        self.assertEqual(len(self.c.puts), 2)


class OutcomeCap(unittest.TestCase):
    """Jack 2026-11-06: "Limit exposure by outcome, not just by game"."""

    def test_the_payout_riding_on_one_leg_result(self):
        book = cm.Book(None)
        book.tickets = [{"legs": [[L1, "yes"], [L2, "yes"]], "games": ["G1", "G2"], "contracts": 350.0, "ask": 0.2,
                         "status": "executed", "day": "d"}]
        cfg = cm.Config(max_outcome_payout=400.0, max_game_payout=5000.0, max_payout=500.0, max_open_loss=9999.0)
        self.assertAlmostEqual(book.outcome_payout(L1, "yes"), 350.0)
        self.assertEqual(book.outcome_payout(L1, "no"), 0.0)
        self.assertEqual(cm.caps_check(book, ["G1", "G3"], 60.0, 0.2, cfg, "d", [[L1, "yes"], [L3, "no"]]),
                         "outcome_cap")
        self.assertIsNone(cm.caps_check(book, ["G1", "G3"], 60.0, 0.2, cfg, "d", [[L1, "no"], [L3, "no"]]))
        self.assertIsNone(cm.caps_check(book, ["G1", "G3"], 40.0, 0.2, cfg, "d", [[L1, "yes"], [L3, "no"]]))
        cfg.max_outcome_payout = None
        self.assertIsNone(cm.caps_check(book, ["G1", "G3"], 60.0, 0.2, cfg, "d", [[L1, "yes"], [L3, "no"]]))

    def test_the_quote_is_skipped(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        c = FakeClient(MK)
        book = cm.Book(os.path.join(d.name, "s.json"))
        book.tickets = [{"legs": [[L1, "yes"]], "games": ["X"], "contracts": 30.0, "ask": 0.6, "status": "executed",
                         "day": "d"}]
        m = cm.Maker(c, cm.Config(live=True, markup_per_leg=0.0, max_outcome_payout=50.0), W(), book, d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg())                                                  # 43 contracts more on L1 yes
        self.assertEqual(c.posts, [])
        self.assertEqual(m.st["why"]["outcome_cap"], 1)


class BookFair(unittest.TestCase):
    """Jack 2026-11-06: "A second fair price ... do this if it doesnt slow us down materially"."""

    def maker(self, p_home):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        day = datetime.fromtimestamp(T0, timezone.utc).date().isoformat()
        start = datetime.fromtimestamp(T0 + 2 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")   # L1's start
        with open(os.path.join(d.name, f"odds_{day}.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"k": "odds", "t": T0 - 300, "lg": "KXNBAGAME", "eid": "9", "start": start,
                                "home": "GSW", "away": "MIA", "hml": -150.0, "aml": 130.0, "p_home": p_home,
                                "prov": "DraftKings", "state": "pre"}) + "\n")
        self.c = FakeClient(MK)
        self.w = W()
        m = cm.Maker(self.c, cm.Config(live=True, markup_per_leg=0.0, book_fair=True, max_book_gap=0.05,
                                       odds_dir=d.name), self.w, cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.reload_lines(T0)
        return m

    def test_the_book_raises_the_fair_on_a_covered_leg(self):
        m = self.maker(0.63)                                                 # GSW 63% at the book, 60% on Kalshi
        m.handle(rfq_msg())
        q = self.w.k("quote")[0]
        self.assertAlmostEqual(q["fair"], 0.63 * 0.50 * 0.70)               # the book's leg, Kalshi's others
        self.assertEqual(c_ask := q["ask"], cm.grid_up(0.63 * 0.5 * 0.7 * 1.10))
        self.assertTrue(c_ask > cm.grid_up(FAIR * 1.10))

    def test_a_lower_book_keeps_kalshi(self):
        m = self.maker(0.58)
        m.handle(rfq_msg())
        self.assertAlmostEqual(self.w.k("quote")[0]["fair"], FAIR)

    def test_a_gap_skips(self):
        m = self.maker(0.70)                                                 # 10 points from Kalshi's 60
        m.handle(rfq_msg())
        self.assertEqual(self.c.posts, [])
        self.assertEqual(m.st["why"]["book_gap"], 1)

    def test_the_last_look_uses_it_too(self):
        m = self.maker(0.60)
        m.handle(rfq_msg())                                                  # quoted at Kalshi's fair
        start = datetime.fromtimestamp(T0 + 7200, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        m.lines = cm.cbl.Lines([{"lg": "KXNBAGAME", "eid": "9", "t": T0 - 1, "p_home": 0.66, "start": start,
                                 "home": "GSW", "away": "MIA"}])
        m.line_match = {}
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": self.c.posts[0][1]["no_bid"]}})
        self.assertEqual(self.c.puts, [])                                    # the book moved 6 points: gap
        self.assertEqual(self.w.k("decline")[0]["why"], "book_gap")

    def test_cli(self):
        for bad in (["--max-book-gap", "0.9"], ["--guard-min-rate", "0"], ["--max-outcome-payout", "5"]):
            with self.assertRaises(SystemExit):
                cm.main(bad)


def poly_market(mid, slug, question, outcomes, bid, ask, liq=50000.0, git=None):
    return {"id": mid, "slug": slug, "question": question, "outcomes": json.dumps(outcomes), "bestBid": bid,
            "bestAsk": ask, "liquidityNum": liq, "groupItemTitle": git, "acceptingOrders": True, "closed": False}


class PolyFair(unittest.TestCase):
    """Jack 2026-11-06: "use Polymarket as another fair so DraftKings + Polymarket + Kalshi"."""
    EVE, COV = "KXEPLGAME-26NOV06EVECOV-EVE", "KXEPLGAME-26NOV06EVECOV-COV"
    SUA = "KXUFCFIGHT-26NOV06SUAJAN-SUA"

    def lines(self, eve=(0.62, 0.63), cov_liq=50000.0):
        rows = {"epl": [poly_market("1", "epl-eve-cov-2026-11-06-eve", "Will Everton FC win?", ["Yes", "No"],
                                    *eve, git="Everton FC"),
                        poly_market("2", "epl-eve-cov-2026-11-06-cov", "Will Coventry City FC win?", ["Yes", "No"],
                                    0.17, 0.18, liq=cov_liq, git="Coventry City FC")],
                "ufc": [poly_market("3", "ufc-tat-vir-2026-11-07", "Suarez vs. Jandiroba",
                                    ["Tatiana Suarez", "Virna Jandiroba"], 0.64, 0.65)]}
        tags = {"epl": "306", "ufc": "279"}

        def get(path, params):
            if path.startswith("/tags/slug/"):
                slug = path.rsplit("/", 1)[1]
                return {"id": tags[slug]} if slug in tags else {}
            return {v: k for k, v in tags.items()}.get(params.get("tag_id")) and rows[{v: k for k, v in tags.items()}[params["tag_id"]]] or []
        pl = cm.cpl.PolyLines(get=get, clock=lambda: T0, log=lambda s: None)
        pl.refresh(T0)
        return pl

    def maker(self, pl, legs_mk):
        self.c = FakeClient(legs_mk)
        self.w = W()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(self.c, cm.Config(live=True, markup_per_leg=0.0, poly_fair=True, max_book_gap=0.05,
                                       min_legs=2, max_poly_spread=0.03, min_poly_liquidity=2000.0),
                     self.w, cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.poly = pl
        return m

    def mk(self, eve_mid=0.60):
        return {self.EVE: dict(mkt(self.EVE, eve_mid - 0.01, eve_mid + 0.01), yes_sub_title="Everton"),
                L2: mkt(L2, 0.49, 0.51)}

    def test_a_higher_polymarket_raises_the_fair(self):
        m = self.maker(self.lines(eve=(0.62, 0.63)), self.mk(0.60))         # Kalshi 60, Polymarket 62.5
        m.handle(rfq_msg(legs=[[self.EVE, "yes"], [L2, "yes"]]))
        q = self.w.k("quote")[0]
        self.assertAlmostEqual(q["fair"], 0.625 * 0.50)
        self.assertAlmostEqual(q["fair_k"], 0.60 * 0.50)

    def test_lower_keeps_kalshi_and_a_gap_skips(self):
        m = self.maker(self.lines(eve=(0.57, 0.58)), self.mk(0.60))
        m.handle(rfq_msg(legs=[[self.EVE, "yes"], [L2, "yes"]]))
        self.assertAlmostEqual(self.w.k("quote")[0]["fair"], 0.30)
        self.assertNotIn("fair_k", self.w.k("quote")[0])
        m = self.maker(self.lines(eve=(0.69, 0.70)), self.mk(0.60))         # 9.5 points apart
        m.handle(rfq_msg(legs=[[self.EVE, "yes"], [L2, "yes"]]))
        self.assertEqual(self.c.posts, [])
        self.assertEqual(m.st["why"]["poly_gap"], 1)

    def test_a_thin_book_does_not_count(self):
        m = self.maker(self.lines(eve=(0.55, 0.70)), self.mk(0.60))         # 15c wide: ignored
        m.handle(rfq_msg(legs=[[self.EVE, "yes"], [L2, "yes"]]))
        self.assertAlmostEqual(self.w.k("quote")[0]["fair"], 0.30)


class PolyLinesMatch(unittest.TestCase):
    def pl(self, rows):
        def get(path, params):
            if path.startswith("/tags/slug/"):
                return {"id": "1"} if path.endswith(("/ufc", "/la-liga")) else {}
            return rows
        p = cm.cpl.PolyLines(get=get, clock=lambda: T0, log=lambda s: None)
        p.refresh(T0)
        return p

    def test_soccer_by_slug_draw_and_name(self):
        rows = [poly_market("1", "lal-sev-ala-2026-11-06-sev", "Will Sevilla FC win?", ["Yes", "No"], 0.49, 0.50,
                            git="Sevilla FC"),
                poly_market("2", "lal-sev-ala-2026-11-06-draw", "Draw?", ["Yes", "No"], 0.29, 0.30,
                            git="Draw (Sevilla FC vs. Deportivo Alaves)"),
                poly_market("3", "lal-sev-ala-2026-11-06-ala", "Will Alaves win?", ["Yes", "No"], 0.20, 0.21,
                            git="Deportivo Alavés")]
        p = self.pl(rows)
        self.assertEqual(p.match("KXLALIGAGAME-26NOV06SEVALA-SEV"), ("1", 0))
        self.assertEqual(p.match("KXLALIGAGAME-26NOV06SEVALA-TIE"), ("2", 0))
        self.assertEqual(p.match("KXLALIGAGAME-26NOV07SEVALA-ALA"), ("3", 0))              # a day off
        self.assertEqual(p.match("KXLALIGAGAME-26NOV06SVLALV-SVL", {"yes_sub_title": "Sevilla"}), ("1", 0))  # by name
        self.assertIsNone(p.match("KXLALIGAGAME-26NOV06SVLALV-TIE"))                          # no draw by name
        self.assertAlmostEqual(p.p_yes(("2", 0))[0], 0.295)

    def test_ufc_needs_both_fighters(self):
        rows = [poly_market("7", "ufc-aus7-car22-2026-11-07", "Bashi vs. Calderon", ["Austin Bashi", "Carlos Calderon"],
                            0.83, 0.84),
                poly_market("8", "ufc-tat-vir-2026-11-07", "Suarez vs. Jandiroba", ["Tatiana Suarez", "Virna Jandiroba"],
                            0.64, 0.65),
                poly_market("9", "ufc-wel-seo1-2026-11-07", "Turman vs. Ko", ["Wellington Turman", "Seokhyeon Ko"],
                            0.14, 0.15)]
        p = self.pl(rows)
        self.assertIsNone(p.match("KXUFCFIGHT-26NOV06BASBRE-BAS", {"yes_sub_title": "Austin Bashi"}))  # another bout
        self.assertEqual(p.match("KXUFCFIGHT-26NOV06SUAJAN-JAN", {"yes_sub_title": "Virna Jandiroba"}), ("8", 1))
        self.assertAlmostEqual(p.p_yes(("8", 1))[0], 1 - 0.645)
        self.assertEqual(p.match("KXUFCFIGHT-26NOV07SEOTUR-SEO", {"yes_sub_title": "Seok Hyun Ko"}), ("9", 1))
        self.assertEqual(cm.cpl.tokens("Deportivo Alavés FC"), {"deportivo", "alaves"})
        self.assertEqual(cm.cpl.kalshi_date("KXEPLGAME-26NOV08CRYLFC-LFC").isoformat(), "2026-11-08")


def poly_lines(rows_by_tag):
    """A PolyLines read from {tag slug: [markets]} (only the tags named here exist)."""
    ids = {slug: str(i + 1) for i, slug in enumerate(rows_by_tag)}
    by_id = {v: k for k, v in ids.items()}

    def get(path, params):
        if path.startswith("/tags/slug/"):
            slug = path.rsplit("/", 1)[1]
            return {"id": ids[slug]} if slug in ids else {}
        return rows_by_tag.get(by_id.get(params.get("tag_id")), [])
    p = cm.cpl.PolyLines(get=get, clock=lambda: T0, log=lambda s: None)
    p.refresh(T0)
    return p


def kmkt(name, a, b, what="college football game", day="Nov 7, 2026"):
    """A Kalshi leg's market as the matcher sees it: its side's name and its rules."""
    return {"yes_sub_title": name,
            "rules_primary": f"If {name}  wins the {a} vs {b} {what} originally scheduled for {day}, then the "
                             f"market resolves to Yes."}


def ufc_mkt(name, a, b):
    return kmkt(name, a, b, "professional MMA fight", "Nov 6, 2026")


BASHI_ROWS = {"ufc": [poly_market("7", "ufc-aus7-car22-2026-11-07", "Bashi vs. Calderon",
                                  ["Austin Bashi", "Carlos Calderon"], 0.83, 0.84),
                      poly_market("8", "ufc-tat-vir-2026-11-07", "Suarez vs. Jandiroba",
                                  ["Tatiana Suarez", "Virna Jandiroba"], 0.64, 0.65),
                      poly_market("9", "ufc-wel-seo1-2026-11-07", "Turman vs. Ko", ["Wellington Turman", "Seokhyeon Ko"],
                                  0.14, 0.15),
                      poly_market("10", "ufc-bru-mik-2026-11-07", "Silva vs. Johnson", ["Bruno Silva", "Mike Johnson"],
                                  0.50, 0.51)]}


class PolyUSAndNames(unittest.TestCase):
    """Jack 2026-11-06, "Yes, do both": Polymarket for the NBA / NFL / NHL / college
    football, one team's code apart, both sides by name."""

    def test_us_slug_both_sides(self):
        p = poly_lines({"nba": [poly_market("11", "nba-sas-por-2026-11-07", "Spurs vs. Trail Blazers",
                                            ["Spurs", "Trail Blazers"], 0.60, 0.61)],
                        "nfl": [poly_market("12", "nfl-cin-atl-2026-11-08", "Bengals vs. Falcons",
                                            ["Bengals", "Falcons"], 0.61, 0.62)]})
        self.assertEqual(p.match("KXNBAGAME-26NOV07SASPOR-SAS"), ("11", 0))
        self.assertEqual(p.match("KXNBAGAME-26NOV07SASPOR-POR"), ("11", 1))
        self.assertEqual(p.match("KXNBAGAME-26NOV07PORSAS-POR"), ("11", 1))          # Kalshi's order the other way
        self.assertAlmostEqual(p.p_yes(("11", 1))[0], 1 - 0.605)
        self.assertIsNone(p.match("KXNBAGAME-26NOV08SASPOR-SAS"))                     # NBA: back-to-backs, the day only
        self.assertEqual(p.match("KXNFLGAME-26NOV09CINATL-ATL"), ("12", 1))           # NFL: a day either side

    def test_one_code_apart(self):
        p = poly_lines({"nhl": [poly_market("21", "nhl-ana-cal-2026-11-06", "Ducks vs. Flames", ["Ducks", "Flames"],
                                            0.42, 0.43),
                                poly_market("22", "nhl-sj-min-2026-11-06", "Sharks vs. Wild", ["Sharks", "Wild"],
                                            0.28, 0.29)],
                        "la-liga": [poly_market("31", "lal-osa-bil-2026-11-07-osa", "Will CA Osasuna win?",
                                                ["Yes", "No"], 0.30, 0.31, git="CA Osasuna"),
                                    poly_market("32", "lal-osa-bil-2026-11-07-draw", "Draw?", ["Yes", "No"], 0.29, 0.30,
                                                git="Draw (CA Osasuna vs. Athletic Club)"),
                                    poly_market("33", "lal-osa-bil-2026-11-07-bil", "Will Athletic Club win?",
                                                ["Yes", "No"], 0.40, 0.41, git="Athletic Club")]})
        self.assertEqual(p.match("KXNHLGAME-26NOV06ANACGY-ANA"), ("21", 0))
        self.assertEqual(p.match("KXNHLGAME-26NOV06ANACGY-CGY"), ("21", 1))           # Kalshi CGY, Polymarket cal
        self.assertEqual(p.match("KXLALIGAGAME-26NOV07OSAATH-ATH"), ("33", 0))        # Kalshi ATH, Polymarket bil
        self.assertEqual(p.match("KXLALIGAGAME-26NOV07OSAATH-TIE"), ("32", 0))
        self.assertEqual(p.match("KXLALIGAGAME-26NOV07OSAATH-OSA"), ("31", 0))
        self.assertIsNone(p.match("KXNHLGAME-26NOV06SJANA-SJ"))                      # each code in another game
        self.assertIsNone(p.match("KXNHLGAME-26NOV06BOSCGY-CGY"))                    # neither code
        self.assertEqual(cm.cpl._split("anacgy", "cgy"), ("ana", "cgy"))
        self.assertEqual(cm.cpl._split("sjmin", "sj"), ("sj", "min"))
        self.assertIsNone(cm.cpl._split("sjmin", "draw"))

    def test_college_football_by_both_names_exactly(self):
        p = poly_lines({"cfb": [poly_market("41", "cfb-vtech-smu-2026-11-06", "Virginia Tech vs. SMU",
                                            ["Virginia Tech", "SMU"], 0.32, 0.33),
                                poly_market("42", "cfb-txtech-okst-2026-11-07", "Texas Tech vs. Oklahoma State",
                                            ["Texas Tech", "Oklahoma State"], 0.55, 0.56)]})
        vt = kmkt("Virginia Tech", "Virginia Tech", "SMU", day="Nov 6, 2026")
        self.assertEqual(cm.cpl.kalshi_sides(vt), ("Virginia Tech", "SMU"))
        self.assertEqual(p.match("KXNCAAFGAME-26NOV06VTSMU-VT", vt), ("41", 0))
        self.assertEqual(p.match("KXNCAAFGAME-26NOV06VTSMU-SMU", kmkt("SMU", "Virginia Tech", "SMU")), ("41", 1))
        self.assertIsNone(p.match("KXNCAAFGAME-26NOV07TEXOKLA-TEX", kmkt("Texas", "Texas", "Oklahoma")))  # not Tech
        self.assertIsNone(p.match("KXNCAAFGAME-26NOV06VTSMU-VT", {"yes_sub_title": "Virginia Tech"}))   # no rules

    def test_college_football_st_is_state(self):
        p = poly_lines({"cfb": [poly_market("43", "cfb-pennst-wash-2026-11-07", "Penn State vs. Washington",
                                            ["Penn State", "Washington"], 0.70, 0.71),
                                poly_market("44", "cfb-penn-cor-2026-11-07", "Penn vs. Cornell", ["Penn", "Cornell"],
                                            0.60, 0.61)]})
        self.assertEqual(p.match("KXNCAAFGAME-26NOV07PSUWASH-PSU", kmkt("Penn St.", "Penn St.", "Washington")),
                         ("43", 0))
        self.assertEqual(p.match("KXNCAAFGAME-26NOV07PENNCOR-PENN", kmkt("Penn", "Penn", "Cornell")), ("44", 0))
        self.assertIsNone(p.match("KXNCAAFGAME-26NOV07PENNWASH-PENN", kmkt("Penn", "Penn", "Washington")))

    def test_ufc_by_both_names(self):
        p = poly_lines(BASHI_ROWS)
        ko = ufc_mkt("Seokhyeon Ko", "Wellington Turman", "Seokhyeon Ko")
        self.assertEqual(p.match("KXUFCFIGHT-26NOV07TURKOS-KOS", ko), ("9", 1))      # codes off, both names on
        self.assertIsNone(p.match("KXUFCFIGHT-26NOV06BASBRE-BAS",
                                  ufc_mkt("Austin Bashi", "Austin Bashi", "Lucas Brennan")))     # another bout


def soccer_mkt(name, a, b, league):
    lead = "If Tie is the result of the" if name == "Tie" else f"If {name} wins the"
    return {"yes_sub_title": name,
            "rules_primary": f"{lead} {a} vs {b} professional {league} soccer game originally scheduled for Nov 7, "
                             f"2026 after 90 minutes plus stoppage time (does not include extra time or penalties)"}


class PolySoccerNames(unittest.TestCase):
    """Soccer by both team names: a draw whose codes both differ (Kalshi NFOMCI,
    Polymarket not-mac), a team named differently (Bayern Munich / München)."""

    def pl(self):
        yn = ["Yes", "No"]
        return poly_lines({
            "epl": [poly_market("61", "epl-not-mac-2026-11-07-not", "?", yn, 0.20, 0.21, git="Nottingham Forest FC"),
                    poly_market("62", "epl-not-mac-2026-11-07-draw", "?", yn, 0.24, 0.25,
                                git="Draw (Nottingham Forest FC vs. Manchester City FC)"),
                    poly_market("63", "epl-not-mac-2026-11-07-mac", "?", yn, 0.55, 0.56, git="Manchester City FC")],
            "bundesliga": [poly_market("51", "bun-mai-bay-2026-11-07-mai", "?", yn, 0.11, 0.12, git="1. FSV Mainz 05"),
                           poly_market("52", "bun-mai-bay-2026-11-07-draw", "?", yn, 0.14, 0.15,
                                       git="Draw (1. FSV Mainz 05 vs. FC Bayern München)"),
                           poly_market("53", "bun-mai-bay-2026-11-07-bay", "?", yn, 0.74, 0.75, git="FC Bayern München")],
            "ligue-1": [poly_market("71", "fl1-aja-pfc-2026-11-07-aja", "?", yn, 0.40, 0.41, git="AJ Auxerre"),
                        poly_market("72", "fl1-aja-pfc-2026-11-07-draw", "?", yn, 0.27, 0.28,
                                    git="Draw (AJ Auxerre vs. Paris FC)"),
                        poly_market("73", "fl1-aja-pfc-2026-11-07-pfc", "?", yn, 0.31, 0.32, git="Paris FC"),
                        poly_market("74", "fl1-psg-tro-2026-11-07-psg", "?", yn, 0.84, 0.85,
                                    git="Paris Saint-Germain FC"),
                        poly_market("76", "fl1-psg-tro-2026-11-07-tro", "?", yn, 0.05, 0.06, git="ES Troyes AC")]})

    def test_draw_and_a_renamed_team(self):
        p = self.pl()
        epl = lambda n: soccer_mkt(n, "Nottingham Forest", "Manchester City", "Premier League")   # noqa: E731
        self.assertEqual(p.match("KXEPLGAME-26NOV07NFOMCI-TIE", epl("Tie")), ("62", 0))
        self.assertEqual(p.match("KXEPLGAME-26NOV07NFOMCI-MCI", epl("Manchester City")), ("63", 0))
        bun = lambda n: soccer_mkt(n, "Mainz", "Bayern Munich", "Bundesliga")                       # noqa: E731
        self.assertEqual(p.match("KXBUNDESLIGAGAME-26NOV07M05BMU-BMU", bun("Bayern Munich")), ("53", 0))  # by Mainz
        self.assertEqual(p.match("KXBUNDESLIGAGAME-26NOV07M05BMU-M05", bun("Mainz")), ("51", 0))
        self.assertEqual(p.match("KXBUNDESLIGAGAME-26NOV07M05BMU-TIE", bun("Tie")), ("52", 0))

    def test_both_names_settle_a_shared_word(self):
        p = self.pl()
        par = soccer_mkt("Paris FC", "Auxerre", "Paris FC", "Ligue 1")
        self.assertEqual(p.match("KXLIGUE1GAME-26NOV07AUXPAR-PAR", par), ("73", 0))
        self.assertIsNone(p.match("KXLIGUE1GAME-26NOV07AUXPAR-PAR", {"yes_sub_title": "Paris FC"}))  # "Paris" twice
        self.assertIsNone(p.match("KXLIGUE1GAME-26NOV07AUXTRO-AUX",
                                  soccer_mkt("Auxerre", "Auxerre", "Troyes", "Ligue 1")))           # two games

    def test_sides_from_the_rules(self):
        sides = cm.cpl.kalshi_sides
        self.assertEqual(sides(soccer_mkt("Tie", "Malaga", "Santander", "La Liga")), ("Malaga", "Santander"))
        self.assertEqual(sides(soccer_mkt("Mainz", "Mainz", "1. FC Koln", "Bundesliga")), ("Mainz", "1. FC Koln"))
        self.assertEqual(sides({"rules_primary": "If San Antonio wins the San Antonio vs Portland Pro Basketball game "
                                                 "originally scheduled for Nov 7, 2026, then"}),
                         ("San Antonio", "Portland"))
        self.assertEqual(sides({"rules_primary": "If Anaheim wins the Anaheim vs Calgary NHL game originally "
                                                 "scheduled for Nov 6, 2026"}), ("Anaheim", "Calgary"))
        self.assertIsNone(sides({"rules_primary": "If the high is above 70 degrees ..."}))


class BoutGuard(unittest.TestCase):
    """Jack 2026-11-06, "Yes, do both": a UFC bout Polymarket shows changed skips
    both its legs, at the quote and the last look (Kalshi's Bashi vs Brennan,
    Polymarket's Bashi vs Calderon after Brennan's injury)."""
    BAS, BRE = "KXUFCFIGHT-26NOV06BASBRE-BAS", "KXUFCFIGHT-26NOV06BASBRE-BRE"
    SUA = "KXUFCFIGHT-26NOV06SUAJAN-SUA"

    def test_both_legs_and_only_a_changed_bout(self):
        p = poly_lines(BASHI_ROWS)
        changed = "Austin Bashi vs Carlos Calderon"
        self.assertEqual(p.bout_changed(self.BAS, ufc_mkt("Austin Bashi", "Austin Bashi", "Lucas Brennan")), changed)
        self.assertEqual(p.bout_changed(self.BRE, ufc_mkt("Lucas Brennan", "Austin Bashi", "Lucas Brennan")),
                         changed)                                                     # Brennan's leg too
        sua = lambda b: ufc_mkt("Tatiana Suarez", "Tatiana Suarez", b)              # noqa: E731
        self.assertIsNone(p.bout_changed(self.SUA, sua("Virna Jandiroba")))
        self.assertIsNone(p.bout_changed(self.SUA, sua("V. Jandiroba")))             # respelled: the same bout
        self.assertIsNone(p.bout_changed(self.SUA, sua("Virna Jandiroba Jr.")))
        self.assertIsNone(p.bout_changed("KXUFCFIGHT-26NOV06SILDOE-SIL",
                                         ufc_mkt("Jose Silva", "Jose Silva", "John Doe")))  # another Silva
        self.assertIsNone(p.bout_changed(self.BAS, {"yes_sub_title": "Austin Bashi"}))     # no rules: can't say
        self.assertIsNone(p.bout_changed("KXNBAGAME-26NOV07SASPOR-SAS",
                                         ufc_mkt("Austin Bashi", "Austin Bashi", "Lucas Brennan")))

    def test_a_failed_read_keeps_the_last_good_one(self):
        state = {"fail": False}

        def get(path, params):
            if state["fail"]:
                raise IOError("down")
            if path.startswith("/tags/slug/"):
                return {"id": "279"} if path.endswith("/ufc") else {}
            return BASHI_ROWS["ufc"]
        bas, changed = ufc_mkt("Austin Bashi", "Austin Bashi", "Lucas Brennan"), "Austin Bashi vs Carlos Calderon"
        p = cm.cpl.PolyLines(get=get, clock=lambda: T0, log=lambda s: None)
        p.refresh(T0)
        state["fail"] = True
        p.refresh(T0 + 600)                                      # 10 minutes on: the last good read stands
        self.assertEqual(p.bout_changed(self.BAS, bas), changed)
        self.assertIn("ufc: OSError", p.error)
        p.refresh(T0 + 1000)                                     # past 15: gone
        self.assertIsNone(p.bout_changed(self.BAS, bas))
        q = cm.cpl.PolyLines(get=get, clock=lambda: T0, log=lambda s: None)
        q.refresh(T0)                                            # the tag read fails: asked again, not forgotten
        self.assertNotIn("ufc", q.tag_ids)
        state["fail"] = False
        q.refresh(T0 + 180)
        self.assertEqual(q.bout_changed(self.BAS, bas), changed)

    def maker(self, guard=True):
        legs_mk = {self.BAS: dict(mkt(self.BAS, 0.63, 0.65, start_in_h=8.0),
                                  **ufc_mkt("Austin Bashi", "Austin Bashi", "Lucas Brennan")),
                   self.BRE: dict(mkt(self.BRE, 0.36, 0.38, start_in_h=8.0),
                                  **ufc_mkt("Lucas Brennan", "Austin Bashi", "Lucas Brennan")),
                   self.SUA: dict(mkt(self.SUA, 0.63, 0.65, start_in_h=8.0),
                                  **ufc_mkt("Tatiana Suarez", "Tatiana Suarez", "Virna Jandiroba")),
                   L2: mkt(L2, 0.49, 0.51)}
        self.c, self.w = FakeClient(legs_mk), W()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(self.c, cm.Config(live=True, markup_per_leg=0.0, poly_fair=True, bout_guard=guard,
                                       min_legs=2, max_poly_spread=0.03, min_poly_liquidity=2000.0),
                     self.w, cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.poly = poly_lines(BASHI_ROWS)
        return m

    def test_the_quote_skips_both_legs(self):
        m = self.maker()
        m.handle(rfq_msg(rid="r1", legs=[[self.BRE, "yes"], [L2, "yes"]]))
        m.handle(rfq_msg(rid="r2", legs=[[self.BAS, "no"], [L2, "yes"]]))
        self.assertEqual(self.c.posts, [])
        self.assertEqual(m.st["why"]["bout_changed"], 2)
        m.handle(rfq_msg(rid="r3", legs=[[self.SUA, "yes"], [L2, "yes"]]))        # an unchanged bout quotes
        self.assertEqual(len(self.c.posts), 1)

    def test_off_it_quotes(self):
        m = self.maker(guard=False)
        m.handle(rfq_msg(rid="r1", legs=[[self.BRE, "yes"], [L2, "yes"]]))
        self.assertEqual(len(self.c.posts), 1)

    def test_the_last_look_declines(self):
        m = self.maker()
        m.poly = None                                                        # not read yet: quoted
        m.handle(rfq_msg(rid="r1", legs=[[self.BAS, "yes"], [L2, "yes"]]))
        self.assertEqual(len(self.c.posts), 1)
        m.poly = poly_lines(BASHI_ROWS)                                      # the read lands before the accept
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": self.c.posts[0][1]["no_bid"]}})
        self.assertEqual(self.c.puts, [])
        self.assertEqual(self.w.k("decline")[0]["why"], "bout_changed")

    def test_skip_games_by_hand(self):
        cfg = cm.Config(min_legs=2, skip_games={"26NOV06BASBRE"})
        self.assertEqual(cm.pre_check([[self.BRE, "yes"], [L2, "yes"]], cfg), "game_skipped")
        self.assertIsNone(cm.pre_check([[self.SUA, "yes"], [L2, "yes"]], cfg))


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
        cfg = cm.Config(min_legs=2, min_lead_min=5, sports=set(cm.ALL_SPORTS))   # tennis is off by default (InPlay)
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



# ---- the guards (Jack 2026-11-06, "Yes, build both") ---------------------------------

S1, S1B = "KXEPLGAME-26NOV07ARSCHE-ARS", "KXEPLGAME-26NOV07ARSCHE-CHE"
S2, S3, S4 = "KXLALIGAGAME-26NOV07RMABAR-RMA", "KXSERIEAGAME-26NOV07INTMIL-INT", "KXLIGUE1GAME-26NOV07PSGOLY-PSG"
GMK = dict(MK, **{S1: mkt(S1, 0.49, 0.51), S1B: mkt(S1B, 0.24, 0.26), S2: mkt(S2, 0.59, 0.61),
                  S3: mkt(S3, 0.44, 0.46), S4: mkt(S4, 0.69, 0.71)})


def rfq_by(rid, legs, creator, now):
    m = rfq_msg(rid=rid, legs=legs)
    m["msg"]["creator_id"], m["msg"]["created_ts"] = creator, iso(now - 0.2)
    return m


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.clock = [T0]
        self.w = W()

    def cfg(self, **kw):
        kw.setdefault("markup_per_leg", 0.0)
        kw.setdefault("min_legs", 2)
        return cm.Config(live=True, **kw)

    def maker(self, **kw):
        self.c = FakeClient(GMK)
        self.book = cm.Book(os.path.join(self.d.name, "s.json"))
        return cm.Maker(self.c, self.cfg(**kw), self.w, self.book, self.d.name, clock=lambda: self.clock[0])

    def fresh(self, **kw):
        """A restart: a new maker on the same folder (the test's client and book stay)."""
        return cm.Maker(FakeClient(GMK), self.cfg(**kw), W(), cm.Book(os.path.join(self.d.name, "s.json")),
                        self.d.name, clock=lambda: self.clock[0])

    def sell(self, m, rid, legs, creator, minutes=0.0):
        """An RFQ, our quote, its accept: True when confirmed, False when declined,
        None when nothing was quoted."""
        self.clock[0] += minutes * 60
        n = len(self.c.posts)
        m.handle(rfq_by(rid, legs, creator, self.clock[0]))
        if len(self.c.posts) == n:
            return None
        puts = len(self.c.puts)
        m.handle({"type": "quote_accepted", "msg": {"quote_id": f"q{len(self.c.posts)}", "rfq_id": rid,
                                                      "no_bid_dollars": self.c.posts[-1][1]["no_bid"]}})
        return len(self.c.puts) > puts


class SportBurst(GuardBase):
    """11/06 replay: 3 accepts in one sport inside 30 min, from 2+ requesters,
    tripped only the in-play tennis run (at 04:25Z, its 3rd accept)."""

    def test_three_accepts_from_two_requesters_pause_the_sport(self):
        m = self.maker(burst_accepts=3)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [S2, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S3, "yes"], [S4, "yes"]], "bbbb0000", minutes=5))
        self.assertFalse(self.sell(m, "r3", [[S1, "yes"], [S3, "yes"]], "aaaa0000", minutes=5))   # the 3rd
        self.assertEqual(self.w.k("decline")[-1]["why"], "sport_burst")
        g = self.w.k("guard")[0]
        self.assertEqual((g["what"], g["key"]), ("sport_burst", "SOCCER"))
        self.assertAlmostEqual(g["until"], self.clock[0] + 3600)
        self.assertIsNone(self.sell(m, "r4", [[S2, "yes"], [L1, "yes"]], "cccc0000", minutes=1))  # a soccer leg
        self.assertEqual(m.st["why"]["sport_paused"], 1)
        self.assertTrue(self.sell(m, "r5", [[L1, "yes"], [L2, "yes"]], "cccc0000"))              # NBA + NHL
        self.assertGreater(self.fresh(burst_accepts=3).sport_pause["SOCCER"], self.clock[0])      # survives a restart
        self.assertTrue(self.sell(m, "r6", [[S1, "yes"], [S2, "yes"]], "dddd0000", minutes=61))   # an hour on

    def test_one_requester_alone_is_the_requester_guards(self):
        m = self.maker(burst_accepts=3)
        for i, legs in enumerate(([[S1, "yes"], [S2, "yes"]], [[S3, "yes"], [S4, "yes"]], [[S1, "yes"], [S3, "yes"]])):
            self.assertTrue(self.sell(m, f"r{i}", legs, "aaaa0000", minutes=2))
        self.assertEqual(self.w.k("guard"), [])

    def test_spread_out_or_off(self):
        m = self.maker(burst_accepts=3)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [S2, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S3, "yes"], [S4, "yes"]], "bbbb0000", minutes=20))
        self.assertTrue(self.sell(m, "r3", [[S1, "yes"], [S3, "yes"]], "aaaa0000", minutes=20))   # r1 is 40 min old
        self.assertEqual(self.w.k("guard"), [])
        self.w = W()
        m = self.maker(burst_accepts=0)
        for i, (legs, cr) in enumerate((([[S1, "yes"], [S2, "yes"]], "a"), ([[S3, "yes"], [S4, "yes"]], "b"),
                                        ([[S1, "yes"], [S3, "yes"]], "c"))):
            self.assertTrue(self.sell(m, f"r{i}", legs, cr, minutes=1))


class LegRush(GuardBase):
    """One leg (ticker and side) in accepts from 2+ requesters inside 30 min holds its game out."""

    def test_a_leg_two_requesters_take_holds_its_game(self):
        m = self.maker(rush_window_min=30)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [L1, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S1, "no"], [L2, "yes"]], "bbbb0000", minutes=5))     # the other side
        self.assertFalse(self.sell(m, "r3", [[S1, "yes"], [L3, "no"]], "bbbb0000", minutes=5))    # S1 YES again
        self.assertEqual(self.w.k("decline")[-1]["why"], "leg_rush")
        g = self.w.k("guard")[-1]
        self.assertEqual((g["what"], g["key"]), ("leg_rush", "26NOV07ARSCHE"))
        self.assertAlmostEqual(g["until"], self.clock[0] + 7200)
        self.assertIsNone(self.sell(m, "r4", [[S1B, "yes"], [L2, "yes"]], "cccc0000"))            # same game
        self.assertEqual(m.st["why"]["game_held"], 1)
        self.assertFalse(self.sell(m, "r5", [[S3, "yes"], [L2, "yes"]], "cccc0000"))             # L2 YES: bbbb's too
        self.assertTrue(self.sell(m, "r6", [[S3, "yes"], [L3, "yes"]], "cccc0000"))              # L3 YES: nobody's
        self.assertGreater(self.fresh(rush_window_min=30).game_hold["26NOV07ARSCHE"], self.clock[0])

    def test_under_the_game_loss_bar_the_game_stays_quoted(self):
        # Jack 2026-11-06: "there needs to be an exposure cap to not quote it", then
        # "set worse case threshold to $200 on a game"
        m = self.maker(rush_window_min=30, rush_game_loss=1e6)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [L1, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S1, "yes"], [L2, "yes"]], "bbbb0000", minutes=5))
        self.assertEqual(self.w.k("guard"), [])
        self.assertEqual(m.game_hold, {})
        self.assertTrue(self.sell(m, "r3", [[S1B, "yes"], [L3, "yes"]], "cccc0000"))           # same game

    def test_over_the_game_loss_bar_it_holds(self):
        m = self.maker(rush_window_min=30, rush_game_loss=1e-3)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [L1, "yes"]], "aaaa0000"))
        self.assertFalse(self.sell(m, "r2", [[S1, "yes"], [L2, "yes"]], "bbbb0000", minutes=5))
        self.assertEqual(self.w.k("guard")[-1]["what"], "leg_rush")

    def test_the_bar_counts_every_open_ticket_on_the_game(self):
        m = self.maker(rush_window_min=30, rush_game_loss=1e6)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [L1, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S1B, "yes"], [L2, "yes"]], "bbbb0000", minutes=5))  # same game, other leg
        g = cm.game_of(S1)
        on = [t for t in self.book.open() if g in t["games"]]
        self.assertEqual(len(on), 2)
        self.assertAlmostEqual(m.game_loss(g), sum(t["contracts"] * (1.0 - t["ask"]) for t in on))
        self.assertAlmostEqual(m.game_loss("26NOV99NOPE"), 0.0)
        # the bar just over the game's worst case now: a rushed S1 YES's own loss tips it
        m.cfg.rush_game_loss = m.game_loss(g) + 1e-6
        self.assertFalse(self.sell(m, "r3", [[S1, "yes"], [L3, "yes"]], "cccc0000", minutes=1))
        self.assertEqual(self.w.k("guard")[-1]["what"], "leg_rush")

    def test_one_requester_twice_or_far_apart_is_no_rush(self):
        m = self.maker(rush_window_min=30)
        self.assertTrue(self.sell(m, "r1", [[S1, "yes"], [L1, "yes"]], "aaaa0000"))
        self.assertTrue(self.sell(m, "r2", [[S1, "yes"], [L2, "yes"]], "aaaa0000", minutes=5))
        self.assertTrue(self.sell(m, "r3", [[S1, "yes"], [L3, "no"]], "bbbb0000", minutes=31))     # r2 31 min old
        self.assertEqual(self.w.k("guard"), [])


class DayLossStop(GuardBase):
    """No new tickets for the rest of the ET day once the day's P&L (settled + marked)
    is at or under -max_day_loss."""

    def ticket(self, **kw):
        t = {"quote_id": "qx", "mt": "KXMVECROSSCATEGORY-S2026X-Z", "legs": [[L1, "yes"], [L2, "yes"]],
             "games": [cm.game_of(L1), cm.game_of(L2)], "ask": 0.33, "fair": 0.30, "contracts": 100.0,
             "status": "executed", "fill_checked": True, "px_checked": True, "day": cm.et_day(self.clock[0]),
             "at": self.clock[0], "fee_est": 0.5}
        t.update(kw)
        return t

    def test_the_marks_trip_it_and_it_holds_the_day(self):
        m = self.maker(max_day_loss=10.0)
        m.handle(rfq_by("r1", [[S1, "yes"], [S2, "yes"]], "aaaa0000", self.clock[0]))   # quoted before the stop
        self.book.tickets.append(self.ticket())
        m.mark_book(self.clock[0])                       # 0.6 x 0.5 = 0.30: 100 x (0.33 - 0.30) - 0.5
        self.assertAlmostEqual(m.day_pnl, 2.5)
        self.c.mk[L1] = mkt(L1, 0.79, 0.81)              # 0.8 x 0.5 = 0.40
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, -7.5)
        self.assertIsNone(m.halt)
        self.c.mk[L2] = dict(mkt(L2, 0.0, 1.0), result="yes")     # L2 won: 0.8 is left
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, -47.5)
        self.assertEqual(m.halt["day"], cm.et_day(self.clock[0]))
        g = self.w.k("guard")[-1]
        self.assertEqual((g["what"], g["key"]), ("day_loss", cm.et_day(self.clock[0])))
        self.assertAlmostEqual(g["until"], cm.et_next_midnight(self.clock[0]))
        m.handle({"type": "quote_accepted", "msg": {"quote_id": "q1", "rfq_id": "r1",
                                                      "no_bid_dollars": self.c.posts[-1][1]["no_bid"]}})
        self.assertEqual((self.c.puts, self.w.k("decline")[-1]["why"]), ([], "day_loss"))
        self.assertIsNone(self.sell(m, "r2", [[S3, "yes"], [S4, "yes"]], "bbbb0000"))
        self.assertEqual(m.st["why"]["day_loss"], 1)
        self.assertTrue(self.fresh(max_day_loss=10.0)._halted(self.clock[0]))                 # survives a restart
        m.cfg.max_day_loss = 100.0                       # a limit over the loss lifts it
        self.assertIsNotNone(self.sell(m, "r3", [[S3, "yes"], [S4, "yes"]], "bbbb0000"))
        m.cfg.max_day_loss = 10.0
        self.clock[0] = cm.et_next_midnight(self.clock[0]) + 60                               # the next ET day
        self.assertFalse(m._halted(self.clock[0]))

    def test_a_lost_leg_and_an_earlier_days_ticket(self):
        m = self.maker(max_day_loss=500.0)
        self.book.tickets.append(self.ticket(day="2026-11-04"))
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, 0.0)           # counted from its mark at the day's first look (+2.5)
        self.c.mk[L1] = mkt(L1, 0.79, 0.81)
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, -10.0)         # -7.5 now, less the +2.5
        self.c.mk[L1] = dict(mkt(L1, 0.0, 1.0), result="no")    # a leg lost: the parlay is ours
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, 100 * 0.33 - 0.5 - 2.5)
        self.assertEqual(cm.Book(os.path.join(self.d.name, "s.json")).tickets[0]["m0"], 2.5)     # kept

    def test_settled_today_counts_other_days_do_not(self):
        m = self.maker(max_day_loss=30.0)
        today = cm.et_day(self.clock[0])
        self.book.tickets += [self.ticket(status="settled", pnl=-40.0, settled_day=today),
                              self.ticket(status="settled", pnl=-90.0, settled_day="2026-11-04")]
        m.mark_book(self.clock[0])
        self.assertAlmostEqual(m.day_pnl, -40.0)
        self.assertTrue(m._halted(self.clock[0]))
        m.cfg.max_day_loss = None                        # off: nothing held
        self.assertIsNone(m._guard_block([[L1, "yes"]], self.clock[0]))

    def test_upkeep_marks_a_minute_and_the_stat_line_carries_it(self):
        m = self.maker(max_day_loss=500.0)
        self.book.tickets.append(self.ticket())
        self.clock[0] += 61
        m.upkeep()
        self.assertAlmostEqual(m.day_pnl, 2.5)
        st = self.w.k("stat")[-1]
        self.assertEqual((st["day_pnl"], st["halted"]), (2.5, False))

    def test_settle_stamps_the_day(self):
        m = self.maker()
        self.book.tickets.append(self.ticket(mt="KXMVECROSSCATEGORY-S2026X-Z"))
        self.c.mk["KXMVECROSSCATEGORY-S2026X-Z"] = {"ticker": "KXMVECROSSCATEGORY-S2026X-Z", "result": "no"}
        m.settle()
        t = self.book.tickets[0]
        self.assertEqual((t["status"], t["settled_day"]), ("settled", cm.et_day(self.clock[0])))

    def test_leg_and_parlay_values(self):
        self.assertAlmostEqual(cm.leg_value(mkt(L1, 0.59, 0.61), "no"), 0.40)
        self.assertEqual(cm.leg_value({"result": "yes"}, "no"), 0.0)
        self.assertAlmostEqual(cm.leg_value({"yes_bid_dollars": "0", "yes_ask_dollars": "1",
                                             "last_price_dollars": "0.97"}, "yes"), 0.97)
        self.assertIsNone(cm.leg_value({"yes_bid_dollars": "0", "yes_ask_dollars": "1"}, "yes"))
        wide = {"yes_bid_dollars": "0.0100", "yes_ask_dollars": "0.9900"}             # in play
        self.assertAlmostEqual(cm.leg_value(dict(wide, last_price_dollars="0.8800"), "yes"), 0.88)
        self.assertAlmostEqual(cm.leg_value(wide, "yes"), 0.50)                       # no trade: the mid
        self.assertIsNone(cm.parlay_value([[L1, "yes"], [L2, "yes"]], {L1: MK[L1]}))      # L2 unread
        self.assertEqual(cm.parlay_value([[L1, "yes"], [L2, "yes"]], {L2: {"result": "no"}}), 0.0)   # lost

    def test_the_flags(self):
        for bad in (["--max-day-loss", "5"], ["--burst-accepts", "200"], ["--rush-window-min", "-1"]):
            with self.assertRaises(SystemExit):
                cm.main(bad)


class GameStart(unittest.TestCase):
    """A spread / total starts no later than its game's winner market: 11/06 Kalshi's
    soccer spreads / totals expired 1 h after their games' winner markets, and the
    maker quoted Friday's soccer totals 13-45 min into the matches."""
    TOT, WIN = "KXEPLTOTAL-26NOV07EVECOV-2", "KXEPLGAME-26NOV07EVECOV-EVE"

    def test_winner_markets_and_their_events(self):
        for tk, win, ev in ((self.TOT, False, "KXEPLGAME-26NOV07EVECOV"), (self.WIN, True, None),
                            ("KXLIGUE1TOTAL-26NOV06TFCRCS-3", False, "KXLIGUE1GAME-26NOV06TFCRCS"),
                            ("KXNCAAF1HSPREAD-26NOV07UKTENN-TENN7", False, "KXNCAAFGAME-26NOV07UKTENN"),
                            ("KXNBATEAMTOTAL-26NOV06BOSCHA-BOS110", False, "KXNBAGAME-26NOV06BOSCHA"),
                            (PROP, False, "KXNBAGAME-26NOV05ORLPHX"), ("KXUFCFIGHT-26NOV06NAKSCH-SCH", True, None)):
            self.assertEqual((cm.is_winner(tk), cm.winner_event(tk)), (win, ev), tk)
        g = {"26NOV07EVECOV": T0 + 600}
        self.assertEqual(cm.leg_start(self.TOT, T0 + 5 * 3600, g), T0 + 600)          # the game's, earlier
        self.assertEqual(cm.leg_start(self.TOT, T0 + 5 * 3600, {}), T0 + 2 * 3600)    # its own, unknown game
        self.assertEqual(cm.leg_start(self.WIN, T0 + 5 * 3600, g), T0 + 2 * 3600)     # a winner: its own

    def test_the_price_check_takes_the_games_start(self):
        cfg = cm.Config(min_legs=2)
        legs = [[self.TOT, "yes"], [L1, "yes"]]
        mk = {self.TOT: mkt(self.TOT, 0.59, 0.61, start_in_h=2.0), L1: MK[L1]}       # its own expiry: 2 h out
        self.assertEqual(cm.price_check(legs, mk, T0, cfg, {})[0], "no_game_start")
        self.assertEqual(cm.price_check(legs, mk, T0, cfg, {"26NOV07EVECOV": T0 + 600})[0], "starts_soon")
        self.assertIsNone(cm.price_check(legs, mk, T0, cfg, {"26NOV07EVECOV": T0 + 2 * 3600})[0])

    def test_the_maker_reads_the_winner_market_and_skips_the_total(self):
        mk = dict(MK, **{self.TOT: mkt(self.TOT, 0.59, 0.61, start_in_h=2.0),         # +1 h: says 2 h out
                         self.WIN: mkt(self.WIN, 0.44, 0.46, start_in_h=0.25)})       # kickoff in 15 min
        c = FakeClient(mk)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(c, cm.Config(min_legs=2), W(), cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: T0)
        m.handle(rfq_msg(rid="r1", legs=[[self.TOT, "yes"], [L2, "yes"]]))
        self.assertIn(("/markets", {"event_ticker": "KXEPLGAME-26NOV07EVECOV", "limit": 10}), c.gets)
        self.assertAlmostEqual(m.game_start["26NOV07EVECOV"], T0 + 900)
        self.assertEqual(m.st["why"].get("starts_soon"), 1)
        n = len(c.gets)
        m.handle(rfq_msg(rid="r2", legs=[[self.TOT, "yes"], [L2, "yes"]]))
        self.assertEqual(len(c.gets), n)                                               # known now: no read
        self.assertEqual(m.st["why"].get("starts_soon"), 2)

    def test_a_total_read_no_longer_times_its_game(self):
        c = FakeClient({self.TOT: mkt(self.TOT, 0.59, 0.61, start_in_h=2.0), L1: MK[L1]})
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(c, cm.Config(), W(), cm.Book(os.path.join(d.name, "s.json")), d.name, clock=lambda: T0)
        m.markets([self.TOT], 15.0)
        self.assertNotIn("26NOV07EVECOV", m.game_start)
        m.markets([L1], 15.0)
        self.assertIn(cm.game_of(L1), m.game_start)
        m.learn_game_starts([[self.TOT, "yes"]], T0)                                   # nothing found...
        m.learn_game_starts([[self.TOT, "yes"]], T0 + 60)                              # ...not asked again soon
        self.assertEqual(sum(1 for p, q in c.gets if q.get("event_ticker")), 1)


class LegTape(unittest.TestCase):
    """The scorecard's prices: each leg we read, its book and outside fairs when
    they change (or every LEG_REFRESH_S), in legs_<day>.jsonl."""

    def test_changes_refreshes_and_the_outside_fairs(self):
        lw, c, clock = W(), FakeClient(MK), [T0]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = cm.Maker(c, cm.Config(min_legs=2), W(), cm.Book(os.path.join(d.name, "s.json")), d.name,
                     clock=lambda: clock[0], leg_writer=lw)

        def step(rid, advance=61):
            m.cache.clear()
            m.handle(rfq_by(rid, LEGS, "aaaa0000", clock[0]))
            clock[0] += advance
            m.upkeep()

        step("r1")
        rec = lw.k("legs")[0]
        self.assertEqual(rec["m"][L1], [0.59, 0.61, None, None, round(T0 + 5 * 3600)])   # + its expiry, once
        step("r2")
        self.assertEqual(len(lw.k("legs")), 1)                                          # unchanged: no line
        c.mk[L1] = mkt(L1, 0.61, 0.63)
        step("r3")
        self.assertEqual(lw.k("legs")[-1]["m"], {L1: [0.61, 0.63, None, None]})
        m._tape_src(L2, 0.52, None)                                                     # DraftKings on L2
        step("r4")
        self.assertEqual(lw.k("legs")[-1]["m"], {L2: [0.49, 0.51, 0.52, None]})
        clock[0] += cm.LEG_REFRESH_S
        step("r5")
        self.assertEqual(sorted(lw.k("legs")[-1]["m"]), sorted([L1, L2, L3]))           # refreshed
        n = len(lw.k("legs"))
        clock[0] += 120
        m.upkeep()
        self.assertEqual(len(lw.k("legs")), n)                                          # not read since: nothing


if __name__ == "__main__":
    unittest.main()
