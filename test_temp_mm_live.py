#!/usr/bin/env python3
"""Tests for temp_mm_live (the hourly-temp live executor).

NO NETWORK, NO ORDERS: a fake exchange (records every write, books orders
in memory, answers the account reads), a fake WebSocket feed, fake station
rows and a fake fair drive Bot.step() on a fake clock. The few tests of the
real Exchange class swap its HTTP client for a recorder.

SANDBOXED like the other test_temp_* files: TEMP_SHADOW_DATA and
TEMP_MM_DATA point at fresh temp dirs BEFORE temp_mm_live is imported, and
TMM_EMAIL=0 so main() can never send mail.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_mm_live -v
"""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="tmm_live_")
os.environ["TEMP_SHADOW_DATA"] = os.path.join(_SANDBOX, "shadow")
os.environ["TEMP_MM_DATA"] = os.path.join(_SANDBOX, "mm")
os.environ["TMM_EMAIL"] = "0"
for _k in list(os.environ):
    if _k.startswith("TMM_") and _k != "TMM_EMAIL":
        del os.environ[_k]

import datetime as _dt   # noqa: E402
import json              # noqa: E402
import unittest          # noqa: E402

import temp_mm_live as L          # noqa: E402
import temp_mm_policy as pol      # noqa: E402
import temp_shadow_common as C    # noqa: E402

assert C.DATA_ROOT == os.environ["TEMP_SHADOW_DATA"]
assert L.DATA_DIR == os.environ["TEMP_MM_DATA"]
assert os.path.realpath(L.DATA_DIR).startswith(os.path.realpath(tempfile.gettempdir()))

EVENT = "KXTEMPMIAH-26OCT1409"
S = C.parse_event(EVENT)[1]
OPEN = S - 3600.0
M = OPEN + 600.0                      # a minute boundary inside the program
T0 = M + 5.0                          # second 5: outside the landing window
K_HI, K_MID, K_LO = 78.99, 80.99, 82.99
T_HI, T_MID, T_LO = (f"{EVENT}-T{k:.2f}" for k in (K_HI, K_MID, K_LO))
TICKERS = [T_HI, T_MID, T_LO]
BOOKS = {T_HI: ({95: 300.0, 94: 500.0}, {3: 200.0, 2: 400.0}),
         T_MID: ({40: 500.0, 39: 1000.0}, {55: 500.0, 54: 1000.0}),
         T_LO: ({3: 200.0, 2: 400.0}, {95: 300.0, 94: 500.0})}
FAIRS = {T_HI: 0.99, T_MID: 0.5, T_LO: 0.01}
SLOPES = {T_HI: 0.1, T_MID: 2.0, T_LO: 0.1}


def market_rows():
    return [{"ticker": t, "event_ticker": EVENT, "floor_strike": k, "status": "active",
             "open_time": C.iso(OPEN), "close_time": C.iso(S)}
            for t, k in zip(TICKERS, (K_HI, K_MID, K_LO))]


def program_rows(tickers=TICKERS):
    return [{"id": f"p{i}", "market_ticker": t, "start_date": C.iso(OPEN + 120),
             "end_date": C.iso(S), "period_reward": 1000000, "target_size_fp": "1000.00",
             "discount_factor_bps": 5000, "incentive_type": "liquidity"}
            for i, t in enumerate(tickers)]


class FakeFeed:
    def __init__(self):
        self.books = {t: (dict(y), dict(n)) for t, (y, n) in BOOKS.items()}
        self.ok = True
        self.events = []
        self.dirty = set()
        self.markets = set()
        self.bot = None
        self.started = False

    def healthy(self):
        return self.ok

    def book_levels(self, t):
        if not self.ok or t not in self.books:
            return None
        y, n = (dict(x) for x in self.books[t])
        if self.bot is not None and self.bot.live:   # the live book holds our orders
            oy, on = self.bot.own_levels(t)
            for p, q in oy.items():
                y[p] = y.get(p, 0.0) + q
            for p, q in on.items():
                n[p] = n.get(p, 0.0) + q
        return ({p / 100.0: q for p, q in y.items()}, {p / 100.0: q for p, q in n.items()})

    def set_markets(self, ts):
        self.markets = set(ts)

    def drain(self):
        d, e = self.dirty, self.events
        self.dirty, self.events = set(), []
        return d, e, 0

    def status(self):
        return {"healthy": self.ok}

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeWx:
    def __init__(self):
        self.rows = []

    def start(self, now):
        return []

    def poll(self, now):
        r, self.rows = self.rows, []
        return r

    def status(self, now):
        return {"source": "fake"}

    def stop(self):
        pass


class FakeFair:
    def __init__(self):
        self.fairs = dict(FAIRS)
        self.slopes = dict(SLOPES)
        self.stale = False
        self.added = []

    def add(self, rows, now):
        self.added += rows

    def result(self, ev, now, stale_s):
        return L.FairResult(dict(self.fairs), dict(self.slopes), self.stale,
                            "test_stale" if self.stale else "")


class MarketData:
    """The read side every fake exchange shares."""
    markets_rows = None
    programs_rows = None

    def markets(self, min_close_ts):
        return [m for m in (self.markets_rows if self.markets_rows is not None else market_rows())]

    def event_markets(self, event):
        return []

    def programs(self, now):
        return list(self.programs_rows if self.programs_rows is not None else program_rows())


class DryExchange(MarketData):
    """Dry mode: any write is a test failure."""

    def __init__(self):
        self.stats = L.CallStats()

    def _no(self, *a, **k):
        raise AssertionError("a dry run called an exchange write")
    create = amend = decrease = cancel = batch_cancel = _no

    def balance(self):
        raise AssertionError("a dry run without a subaccount read the balance")
    resting = fills = positions = event_orders = get_order = balance


class FakeExchange(MarketData):
    def __init__(self, sub=7, cash=1000.0, booked_sub=None):
        self.sub = sub
        self.live = True
        self.api_base = "fake"
        self.cash = cash
        self.booked_sub = sub if booked_sub is None else booked_sub
        self.stats = L.CallStats()
        self.calls = []
        self.orders = {}            # oid -> record
        self.fills_rows = []
        self.positions_rows = {}
        self.fail = {}              # kind -> [ApiError, ...]
        self.n = 0
        self.readable = True
        self.listable = True

    def _maybe_fail(self, kind):
        lst = self.fail.get(kind)
        if lst:
            raise lst.pop(0)

    # reads
    def balance(self):
        self.calls.append(("balance",))
        return self.cash

    def resting(self):
        self.calls.append(("resting",))
        return [dict(o) for o in self.orders.values() if o["status"] == "resting"]

    def event_orders(self, events):
        self.calls.append(("event_orders", tuple(events)))
        if not self.listable:
            return []
        return [dict(o) for o in self.orders.values()
                if o["ticker"].rsplit("-", 1)[0] in events and o["subaccount_number"] == self.sub]

    def get_order(self, oid, scoped=True):
        self.calls.append(("get_order", oid, scoped))
        o = self.orders.get(oid)
        if o is None or not self.readable:
            return None
        if scoped and o["subaccount_number"] != self.sub:
            return None
        return dict(o)

    def fills(self, min_ts):
        self.calls.append(("fills", min_ts))
        return list(self.fills_rows)

    def positions(self, event):
        self.calls.append(("positions", event))
        return list(self.positions_rows.get(event, []))

    # writes
    def create(self, ticker, side, price, count, coid, expiration_ts):
        self.calls.append(("create", ticker, side, price, count, coid, expiration_ts))
        self._maybe_fail("create")
        self.n += 1
        oid = f"ord{self.n}"
        self.orders[oid] = {"order_id": oid, "client_order_id": coid, "ticker": ticker,
                            "side_ours": side, "price_ours": price, "status": "resting",
                            "remaining_count_fp": f"{count:.2f}", "fill_count_fp": "0.00",
                            "subaccount_number": self.booked_sub, "expiration_ts": expiration_ts}
        return {"order_id": oid, "fill_count": "0.00", "remaining_count": f"{count:.2f}"}

    def amend(self, o, price, total, expiration_ts=None):
        self.calls.append(("amend", o.oid, price, total, expiration_ts))
        self._maybe_fail("amend")
        r = self.orders[o.oid]
        filled = float(r["fill_count_fp"])
        r["price_ours"] = price
        r["remaining_count_fp"] = f"{total - filled:.2f}"
        if expiration_ts is not None:
            r["expiration_ts"] = expiration_ts
        return {"order_id": o.oid}

    def decrease(self, o, k):
        self.calls.append(("decrease", o.oid, k))
        self._maybe_fail("decrease")
        r = self.orders[o.oid]
        rem = float(r["remaining_count_fp"]) - k
        r["remaining_count_fp"] = f"{rem:.2f}"
        return {"order_id": o.oid, "remaining_count": f"{rem:.2f}"}

    def cancel(self, oid, ticker):
        self.calls.append(("cancel", oid, ticker))
        self._maybe_fail("cancel")
        r = self.orders.get(oid)
        if r is None or r["status"] != "resting":
            raise L.ApiError("HttpError(404 Not Found)", 404, "not found")
        r["status"] = "canceled"
        return {"order_id": oid, "reduced_by": r["remaining_count_fp"]}

    def batch_cancel(self, items):
        self.calls.append(("batch_cancel", tuple(items)))
        self._maybe_fail("batch_cancel")
        out = []
        for oid, _t in items:
            r = self.orders.get(oid)
            if r is None or r["status"] != "resting":
                out.append({"order_id": oid, "reduced_by": "0.00",
                            "error": {"code": "not_found", "message": "order not found"}})
            else:
                r["status"] = "canceled"
                out.append({"order_id": oid, "reduced_by": r["remaining_count_fp"]})
        return out

    # helpers
    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def resting_ours(self):
        return {oid: o for oid, o in self.orders.items() if o["status"] == "resting"}

    def fill(self, oid, n, trade_id):
        """Fill n of a resting order: Kalshi's side; returns the WS fill body."""
        r = self.orders[oid]
        r["fill_count_fp"] = f"{float(r['fill_count_fp']) + n:.2f}"
        rem = float(r["remaining_count_fp"]) - n
        r["remaining_count_fp"] = f"{rem:.2f}"
        if rem <= 1e-9:
            r["status"] = "executed"
        side = r["side_ours"]
        price = r["price_ours"]
        yes_c = price if side == "yes" else 100 - price
        body = {"trade_id": trade_id, "order_id": oid, "client_order_id": r["client_order_id"],
                "market_ticker": r["ticker"], "is_taker": False, "side": side,
                "yes_price_dollars": f"{yes_c / 100:.4f}", "count_fp": f"{n:.2f}",
                "fee_cost": "0.00", "ts_ms": 0, "subaccount": r["subaccount_number"],
                "purchased_side": side, "outcome_side": side,
                "book_side": "bid" if side == "yes" else "ask"}
        self.fills_rows.append({**body, "ticker": r["ticker"], "fill_id": "f" + trade_id,
                                "subaccount_number": r["subaccount_number"],
                                "no_price_dollars": f"{(100 - yes_c) / 100:.4f}"})
        return body


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_bot(live=False, sub=7, variant="F_full", cfg=None, pol_over=None, ex=None,
             cash=1000.0, booked_sub=None):
    c = L.Config(email=False, **(cfg or {}))
    P = pol.make_policy(variant, **(pol_over or {}))
    if ex is None:
        ex = FakeExchange(sub=sub, cash=cash, booked_sub=booked_sub) if live else DryExchange()
    feed, wx, fair = FakeFeed(), FakeWx(), FakeFair()
    mails = []
    d = tempfile.mkdtemp(dir=_SANDBOX, prefix="bot_")
    clock = Clock(T0)
    bot = L.Bot(c, P, variant, live, sub if live else 0, ex, feed, wx, fair, data_dir=d,
                clock=clock, threaded=False,
                mailer=lambda s, b: mails.append((s, b)) or True, echo=False,
                policy_overrides=pol_over)
    feed.bot = bot
    return bot, ex, feed, wx, fair, mails, clock


def by_market(bot):
    out = {}
    for o in bot.orders.values():
        out.setdefault(o.ticker, {}).setdefault(o.side, {})
        out[o.ticker][o.side][o.price] = out[o.ticker][o.side].get(o.price, 0.0) + o.remaining
    return out


def started(bot, clock, t=T0):
    clock.t = t
    assert bot.startup(t) == L.EXIT_OK
    bot.step(t)
    return bot


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# --------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_env_and_variants(self):
        c = L.Config.from_env({"TMM_LOOP_S": "0.5", "TMM_EMAIL": "0", "TMM_INC_MODE": "amend",
                               "TMM_MAX_CONTRACTS_ORDER": "500"})
        self.assertEqual((c.loop_s, c.email, c.inc_mode, c.max_contracts_order),
                         (0.5, False, "amend", 500))
        self.assertEqual(L.resolve_variant("F"), "F_full")
        self.assertEqual(L.resolve_variant("g"), "G_decided_only")
        self.assertEqual(L.resolve_variant("J_decided_fav100"), "J_decided_fav100")
        with self.assertRaises(ValueError):
            L.resolve_variant("Z")
        over = L.policy_overrides_from_env({"TMM_Q_LIVE": "300", "TMM_LIVE": "0",
                                            "TMM_EVENT_WCL_CAP": "500", "TMM_LOOP_S": "1"})
        self.assertEqual(over, {"q_live": 300, "live": False, "event_wcl_cap": 500.0})
        P = pol.make_policy("F_full", **over)
        self.assertEqual((P["q_live"], P["live"], P["decided"]), (300, False, True))

    def test_no_name_collision_and_mode_is_cli_only(self):
        names = {f.name for f in L.fields(L.Config)}
        self.assertFalse(names & set(pol.DEFAULT_POLICY))
        self.assertNotIn("live", names)
        self.assertNotIn("subaccount", names)

    def test_stale_policy_and_ttl(self):
        Q = L.stale_policy(pol.make_policy("F_full"))
        self.assertEqual((Q["live"], Q["near_decided_half"], Q["q_fav"], Q["decided"]),
                         (False, False, 0, True))
        c = L.Config()
        self.assertEqual(L.ttl_for(c, "pad"), 300.0)
        self.assertEqual(L.ttl_for(c, "pad+qual"), 300.0)
        self.assertEqual(L.ttl_for(c, "live"), 90.0)
        self.assertEqual(L.ttl_for(c, "fav+qual"), 90.0)
        self.assertEqual(L.ttl_for(c, ""), 90.0)

    def test_program_params(self):
        p = L.program_params(program_rows()[0])
        self.assertAlmostEqual(p["reward"], 100.0)
        self.assertEqual((p["target"], p["df"]), (1000.0, 0.5))
        self.assertAlmostEqual(p["end"] - p["start"], 3480.0)

    def test_build_events(self):
        evs = L.build_events(market_rows() + [{"ticker": "KXOTHER-1"}])
        self.assertEqual(list(evs), [EVENT])
        e = evs[EVENT]
        self.assertEqual([t for _, t in e.markets], TICKERS)
        self.assertEqual((e.open_ts, e.close_ts, e.settle_ts), (OPEN, S, S))


def _o(oid, side, price, size, placed=0.0, exp=1e12, tag="live"):
    return L.Order(oid, "tmm-" + oid, T_MID, side, price, float(size), placed, exp, tag)


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.cfg = L.Config()

    def plan(self, orders, want_y=None, want_n=None, now=100.0, ext_y=None, ext_n=None,
             tags=None, cfg=None):
        return L.plan_market(T_MID, orders, want_y or {}, want_n or {}, tags or {}, now,
                             cfg or self.cfg, ext_y or {40: 500.0}, ext_n or {55: 500.0})

    def test_new_and_unchanged(self):
        ops = self.plan([], {40: 600.0}, {48: 600.0}, tags={("yes", 40): "live"})
        self.assertEqual(sorted((o.kind, o.side, o.price, o.size) for o in ops),
                         [("create", "no", 48, 600.0), ("create", "yes", 40, 600.0)])
        self.assertTrue(all(not o.urgent for o in ops))
        self.assertEqual([o.tag for o in ops if o.side == "yes"], ["live"])
        self.assertEqual(self.plan([_o("a", "yes", 40, 600)], {40: 600.0}), [])

    def test_cancel_and_decrease_are_urgent(self):
        ops = self.plan([_o("a", "yes", 40, 600)], {})
        self.assertEqual([(o.kind, o.urgent, o.reason) for o in ops], [("cancel", True, "pull")])
        ops = self.plan([_o("a", "yes", 40, 600)], {40: 450.0})
        self.assertEqual([(o.kind, o.size, o.urgent) for o in ops], [("decrease", 150.0, True)])

    def test_decrease_newest_first_and_whole_orders(self):
        a, b = _o("a", "yes", 40, 400, placed=1.0), _o("b", "yes", 40, 200, placed=2.0)
        ops = self.plan([a, b], {40: 300.0})
        self.assertEqual([(o.kind, o.order.oid, o.size) for o in ops],
                         [("cancel", "b", 200.0), ("decrease", "a", 100.0)])

    def test_size_tolerance(self):
        o = _o("a", "yes", 40, 600)
        self.assertEqual(self.plan([o], {40: 590.0}), [])            # 10 < 3% of 600
        self.assertEqual([(x.kind, x.size) for x in self.plan([o], {40: 580.0})],
                         [("decrease", 20.0)])
        c = L.Config(size_tol_abs=0.0, size_tol_frac=0.0)
        self.assertEqual([(x.kind, x.size) for x in self.plan([o], {40: 599.0}, cfg=c)],
                         [("decrease", 1.0)])
        self.assertEqual([x.kind for x in self.plan([o], {})], ["cancel"])   # never tolerated

    def test_increase_new_order_or_amend(self):
        ops = self.plan([_o("a", "yes", 40, 400)], {40: 600.0})
        self.assertEqual([(o.kind, o.size, o.urgent) for o in ops], [("create", 200.0, False)])
        c = L.Config(inc_mode="amend")
        ops = self.plan([_o("a", "yes", 40, 400)], {40: 600.0}, cfg=c)
        self.assertEqual([(o.kind, o.order.oid, o.size) for o in ops], [("amend", "a", 600.0)])

    def test_reprice_in_place(self):
        ops = self.plan([_o("a", "yes", 40, 600)], {38: 600.0})
        self.assertEqual([(o.kind, o.price, o.urgent) for o in ops], [("amend", 38, True)])
        ops = self.plan([_o("a", "yes", 38, 600)], {40: 600.0})
        self.assertEqual([(o.kind, o.price, o.urgent) for o in ops], [("amend", 40, False)])
        c = L.Config(amend_price=False)
        ops = self.plan([_o("a", "yes", 40, 600)], {38: 600.0}, cfg=c)
        self.assertEqual(sorted((o.kind, o.price) for o in ops), [("cancel", 40), ("create", 38)])

    def test_guard_cap_decides_urgency_and_debounce(self):
        o = _o("a", "yes", 40, 600)
        # the reference fell 1c but 40 is still under the guard cap (45): paced
        ops = L.plan_market(T_MID, [o], {39: 600.0}, {}, {}, 100.0, self.cfg, {40: 500.0},
                            {55: 500.0}, caps={"yes": 45}, stable={"yes": True})
        self.assertEqual([(x.kind, x.price, x.urgent) for x in ops], [("amend", 39, False)])
        # ... and not yet stable: nothing this round
        ops = L.plan_market(T_MID, [o], {39: 600.0}, {}, {}, 100.0, self.cfg, {40: 500.0},
                            {55: 500.0}, caps={"yes": 45}, stable={"yes": False})
        self.assertEqual(ops, [])
        # the fair fell: 40 is over the cap (35) -> urgent, debounce or not
        ops = L.plan_market(T_MID, [o], {35: 600.0}, {}, {}, 100.0, self.cfg, {40: 500.0},
                            {55: 500.0}, caps={"yes": 35}, stable={"yes": False})
        self.assertEqual([(x.kind, x.price, x.urgent) for x in ops], [("amend", 35, True)])
        # a new quote (no old price to move) is never debounced
        ops = L.plan_market(T_MID, [], {39: 600.0}, {}, {}, 100.0, self.cfg, {}, {},
                            caps={"yes": 45}, stable={"yes": False})
        self.assertEqual([x.kind for x in ops], ["create"])

    def test_guard_caps_from_diag(self):
        P = pol.make_policy("F_full")
        self.assertEqual(L.guard_caps(P, {"fx": 0.5, "buf": 2.0, "fair_src": "model"}),
                         {"yes": 48, "no": 48})
        self.assertEqual(L.guard_caps(P, {"fx": 0.5, "buf": 2.0, "fair_src": "mid"}),
                         {"yes": None, "no": None})
        self.assertEqual(L.guard_caps(pol.make_policy("H_noguard_pull"),
                                      {"fx": 0.5, "buf": 2.0, "fair_src": "model"}),
                         {"yes": None, "no": None})

    def test_reprice_only_within_a_kind(self):
        fav = _o("f", "yes", 95, 300, tag="fav")
        ops = L.plan_market(T_MID, [fav], {1: 250.0}, {}, {("yes", 1): "qual"}, 100.0,
                            self.cfg, {95: 300.0}, {3: 200.0}, caps={"yes": 98},
                            stable={"yes": False})
        self.assertEqual(sorted((x.kind, x.price, x.urgent) for x in ops),
                         [("cancel", 95, True), ("create", 1, False)])
        self.assertEqual(L.tag_kind("near"), "live")
        self.assertEqual(L.tag_kind("pad+qual"), "pad")
        self.assertEqual(L.tag_kind("fav+qual"), "fav")
        self.assertEqual(L.tag_kind(""), "")

    def test_never_amend_through_the_book(self):
        # a YES bid at 46 would cross the NO bid at 55 (46 + 55 >= 100)
        ops = self.plan([_o("a", "yes", 40, 600)], {46: 600.0}, ext_n={55: 500.0})
        self.assertEqual(sorted((o.kind, o.price) for o in ops), [("cancel", 40), ("create", 46)])

    def test_two_orders_at_old_price(self):
        a, b = _o("a", "yes", 40, 500, placed=1.0), _o("b", "yes", 40, 100, placed=2.0)
        ops = self.plan([a, b], {39: 600.0})
        self.assertEqual(sorted((o.kind, o.order.oid) for o in ops),
                         [("amend", "a"), ("cancel", "b")])

    def test_renewal(self):
        o = _o("a", "yes", 40, 600, exp=100.0 + 24.0)
        ops = self.plan([o], {40: 600.0})
        self.assertEqual([(op.kind, op.renew, op.urgent, op.price, op.size) for op in ops],
                         [("amend", True, False, 40, 600.0)])
        o.expires_at = 100.0 + 5.0
        self.assertTrue(self.plan([o], {40: 600.0})[0].urgent)
        o.expires_at = 100.0 + 60.0
        self.assertEqual(self.plan([o], {40: 600.0}), [])


class TestDryRun(unittest.TestCase):
    def test_places_nothing_and_logs_dry(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        got = by_market(bot)
        # the orders are exactly the policy's (decide on the same inputs)
        self.assertEqual(got[T_MID], {"yes": {40: 600.0}, "no": {48: 600.0}})
        self.assertEqual(got[T_HI], {"yes": {95: 300.0}, "no": {1: 1000.0}})
        self.assertEqual(got[T_LO], {"yes": {1: 1000.0}, "no": {95: 300.0}})
        self.assertTrue(all(o.oid.startswith("dry-") for o in bot.orders.values()))
        orows = rows(os.path.join(bot.dir, f"orders_{L.utc_day(T0)}.jsonl"))
        self.assertEqual(len([r for r in orows if r["kind"] == "place"]), 6)
        self.assertTrue(all(r["dry"] and r["mode"] == "dry" for r in orows))
        self.assertTrue(bot.dir.endswith("dry"))
        st = json.load(open(os.path.join(bot.dir, "status.json")))
        self.assertEqual((st["mode"], st["variant"], st["orders_open"]), ("dry", "F_full", 6))
        self.assertEqual(mails, [])

    def test_exchange_refuses_writes_unless_live(self):
        ex = L.Exchange(0, live=False)
        with self.assertRaises(RuntimeError):
            ex.create(T_MID, "yes", 40, 10, "tmm-x", 0)
        with self.assertRaises(RuntimeError):
            ex.batch_cancel([("a", T_MID)])
        self.assertEqual(ex._clients, {})          # never built a client

    def test_matches_policy_decide(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, variant="G_decided_only")
        started(bot, clock)
        states = [pol.MarketState(ticker=t, strike=k, book_yes=BOOKS[t][0], book_no=BOOKS[t][1],
                                  fair=FAIRS[t], slope_c=SLOPES[t], program_start=OPEN + 120,
                                  program_end=S) for t, k in zip(TICKERS, (K_HI, K_MID, K_LO))]
        dec = pol.decide(bot.P, pol.EventContext(T0, S), states)
        want = {md.ticker: {"yes": md.yes, "no": md.no} for md in dec.markets if md.yes or md.no}
        got = by_market(bot)
        self.assertEqual({t: {s: d for s, d in v.items() if d} for t, v in want.items()},
                         {t: {s: d for s, d in v.items() if d} for t, v in got.items()})
        self.assertNotIn(T_MID, got)               # G rests nothing on a live strike


class TestLive(unittest.TestCase):
    def test_refused_without_subaccount(self):
        with self.assertRaises(ValueError):
            make_bot(live=True, sub=0)
        self.assertEqual(L.main(["--live"]), L.EXIT_REFUSED)
        self.assertEqual(L.main(["--live", "--subaccount", "0"]), L.EXIT_REFUSED)

    def test_refuses_zero_cash(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, cash=0.0)
        self.assertEqual(bot.startup(T0), L.EXIT_REFUSED)
        self.assertEqual(ex.kinds("create"), [])
        self.assertTrue(any("refused" in s.lower() for s, _ in mails))

    def test_places_with_first_readback(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        creates = ex.kinds("create")
        self.assertEqual(len(creates), 6)
        self.assertTrue(all(c[5].startswith("tmm-") for c in creates))
        self.assertTrue(all(c[6] == int(T0 + (300 if c[3] == 1 else 90)) for c in creates))
        # the first order was read back before the second was placed
        i_first_get = next(i for i, c in enumerate(ex.calls) if c[0] == "get_order")
        i_second = [i for i, c in enumerate(ex.calls) if c[0] == "create"][1]
        self.assertLess(i_first_get, i_second)
        self.assertTrue(bot.first_confirmed)
        self.assertEqual(by_market(bot)[T_MID], {"yes": {40: 600.0}, "no": {48: 600.0}})

    def test_subaccount_mismatch_halts(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, booked_sub=0)
        started(bot, clock)
        self.assertTrue(bot.halt_reason and "subaccount 0" in bot.halt_reason)
        self.assertTrue(os.path.exists(bot.halt_path))
        self.assertEqual(len(ex.kinds("create")), 1)       # nothing after the first
        self.assertEqual(ex.resting_ours(), {})            # and that one cancelled
        self.assertTrue(any("HALTED" in s for s, _ in mails))

    def test_unconfirmable_first_order_blocks_then_halts(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        ex.readable = False
        ex.listable = False
        started(bot, clock)
        self.assertEqual(len(ex.kinds("create")), 1)
        clock.t = T0 + 2.0
        bot.step(clock.t)
        self.assertEqual(len(ex.kinds("create")), 1)
        clock.t = T0 + 61.0
        bot.step(clock.t)
        self.assertIsNotNone(bot.halt_reason)

    def test_batched_confirmation(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        self.assertEqual(len(bot.unconfirmed), 5)          # all but the first
        clock.t = T0 + 11.0
        bot.step(clock.t)
        self.assertEqual(bot.unconfirmed, {})
        self.assertTrue(ex.kinds("event_orders"))

    def test_landing_window_pull_and_restore(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        mid_ids = {o.oid for o in bot.orders.values() if o.ticker == T_MID}
        self.assertEqual(len(mid_ids), 2)
        clock.t = M + 18.80                    # rests to 19.95: before :20
        bot.step(clock.t)
        self.assertEqual({o.oid for o in bot.orders.values() if o.ticker == T_MID}, mid_ids)
        clock.t = M + 18.90                    # [19.05, 20.05) touches :20 -> pull
        bot.step(clock.t)
        self.assertEqual([o for o in bot.orders.values() if o.ticker == T_MID], [])
        batch = ex.kinds("batch_cancel")[-1]
        self.assertEqual({oid for oid, _ in batch[1]}, mid_ids)
        self.assertEqual(len([o for o in bot.orders.values() if o.ticker != T_MID]), 4)
        pulls = [p for p in bot.pulls if p["kind"] == "pull"]
        self.assertEqual(pulls[-1]["orders"], 2)
        self.assertAlmostEqual(pulls[-1]["second"], 18.9, places=2)
        clock.t = M + 45.80                    # still touches :45
        bot.step(clock.t)
        self.assertEqual([o for o in bot.orders.values() if o.ticker == T_MID], [])
        clock.t = M + 45.90                    # rests from 46.05: restore
        bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_MID], {"yes": {40: 600.0}, "no": {48: 600.0}})
        self.assertEqual(bot.pulls[-1]["kind"], "restore")

    def test_ttl_renewal_by_expiry_amend(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        clock.t = T0 + 66.0                    # 24 s left on the 90 s orders
        bot.step(clock.t)
        ren = [c for c in ex.kinds("amend")]
        self.assertEqual(len(ren), 4)          # live + fav orders; pads (300 s) not yet
        for _k, oid, price, total, exp in ren:
            self.assertEqual(price, ex.orders[oid]["price_ours"])
            self.assertEqual(exp, int(clock.t + 90))
        self.assertTrue(all(o.expires_at >= clock.t + 89 for o in bot.orders.values()
                            if o.price != 1))

    def test_expiry_amend_refused_falls_back_to_replace(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        ex.fail["amend"] = [L.ApiError("HttpError(400 Bad Request)", 400, "bad")]
        clock.t = T0 + 66.0
        bot.step(clock.t)
        self.assertFalse(bot.amend_expiry_ok)
        self.assertTrue(ex.kinds("cancel") or ex.kinds("batch_cancel"))
        n_before = len(ex.kinds("create"))
        clock.t = T0 + 68.0
        bot.step(clock.t)
        self.assertGreater(len(ex.kinds("create")), n_before)

    def test_fill_updates_position_and_risk(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, pol_over={"pos_cap": 200})
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        self.assertEqual(yes_mid.remaining, 200.0)        # pos_cap trims the opening size
        feed.events.append(("fill", ex.fill(yes_mid.oid, 150.0, "tr1")))
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_MID]["no"], {48: 200.0})   # the raise waits
        clock.t = T0 + 1.2
        bot.step(clock.t)
        mp = bot.pos[T_MID]
        self.assertEqual((mp.yes_q, mp.no_q), (150.0, 0.0))
        self.assertAlmostEqual(mp.cost, 150 * 0.40)
        # 50 left on the YES side (cap 200 - 150): the 50 resting already is it
        self.assertEqual(by_market(bot)[T_MID]["yes"], {40: 50.0})
        self.assertEqual(by_market(bot)[T_MID]["no"], {48: 350.0})   # room grows on NO
        frows = rows(os.path.join(bot.dir, f"fills_{L.utc_day(T0)}.jsonl"))
        self.assertEqual([(r["ticker"], r["count"], r["price_ours"]) for r in frows],
                         [(T_MID, 150.0, 40)])
        # the same fill again (REST sweep) is not booked twice
        bot._on_fill(ex.fills_rows[0], "rest", clock.t)
        self.assertEqual(bot.pos[T_MID].yes_q, 150.0)

    def test_event_loss_halt(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, cfg={"event_loss_halt": 100.0})
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 600.0, "tr1")))
        feed.books[T_MID] = ({10: 500.0}, {85: 500.0})      # mid 12.5c: -$165 on 600 @ 40
        clock.t = T0 + 1.5
        bot.step(clock.t)
        self.assertIn(EVENT, bot.halted_events)
        self.assertEqual(bot.orders, {})
        self.assertEqual(ex.resting_ours(), {})
        self.assertIsNone(bot.halt_reason)                   # the bot keeps running
        clock.t = T0 + 3.0
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})                     # pads stay off too
        self.assertTrue(any("event halt" in s for s, _ in mails))

    def test_daily_loss_halt(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, cfg={"event_loss_halt": 1e9, "daily_loss_halt": 100.0})
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 600.0, "tr1")))
        feed.books[T_MID] = ({10: 500.0}, {85: 500.0})
        clock.t = T0 + 1.5
        bot.step(clock.t)
        self.assertTrue(bot.halt_reason and "daily loss" in bot.halt_reason)
        self.assertTrue(os.path.exists(bot.halt_path))
        self.assertEqual(ex.resting_ours(), {})
        self.assertTrue(any("HALTED" in s for s, _ in mails))

    def test_cash_trims_and_skips(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, cash=50.0)
        started(bot, clock)
        for c in ex.kinds("create"):
            self.assertLessEqual(c[3] * c[4], 5000)          # every order fundable alone
        self.assertEqual(by_market(bot)[T_MID]["yes"], {40: 125.0})
        bot2, ex2, *_r, clock2 = make_bot(live=True, cash=0.3)
        started(bot2, clock2)
        self.assertEqual({(c[3]) for c in ex2.kinds("create")}, {1})   # only 1c orders fit
        self.assertTrue(all(c[4] <= 30 for c in ex2.kinds("create")))
        self.assertGreater(bot2.deferred["cash"], 0)

    def test_shutdown_cancels_all(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        # an order the bot lost track of (a previous run's) is swept too
        ex.orders["old1"] = {"order_id": "old1", "client_order_id": "tmm-old", "ticker": T_MID,
                             "side_ours": "yes", "price_ours": 30, "status": "resting",
                             "remaining_count_fp": "5.00", "fill_count_fp": "0.00",
                             "subaccount_number": 7}
        ex.orders["man1"] = {"order_id": "man1", "client_order_id": "manual-x", "ticker": T_MID,
                             "side_ours": "yes", "price_ours": 30, "status": "resting",
                             "remaining_count_fp": "5.00", "fill_count_fp": "0.00",
                             "subaccount_number": 7}
        bot.shutdown(T0 + 1, "test")
        self.assertEqual(set(ex.resting_ours()), {"man1"})   # never touch a non-tmm order
        self.assertEqual(bot.orders, {})

    def test_startup_cancels_strays(self):
        ex = FakeExchange()
        ex.orders["old1"] = {"order_id": "old1", "client_order_id": "tmm-old", "ticker": T_MID,
                             "side_ours": "yes", "price_ours": 30, "status": "resting",
                             "remaining_count_fp": "5.00", "fill_count_fp": "0.00",
                             "subaccount_number": 7}
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=ex)
        self.assertEqual(bot.startup(T0), L.EXIT_OK)
        self.assertEqual(ex.orders["old1"]["status"], "canceled")

    def test_stray_and_gone_reconciliation(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        ex.orders["x9"] = {"order_id": "x9", "client_order_id": "tmm-zz", "ticker": T_LO,
                           "side_ours": "yes", "price_ours": 2, "status": "resting",
                           "remaining_count_fp": "10.00", "fill_count_fp": "0.00",
                           "subaccount_number": 7}
        ex.orders["m9"] = {"order_id": "m9", "client_order_id": "other-1", "ticker": T_LO,
                           "side_ours": "yes", "price_ours": 2, "status": "resting",
                           "remaining_count_fp": "10.00", "fill_count_fp": "0.00",
                           "subaccount_number": 7}
        gone = next(o for o in bot.orders.values() if o.ticker == T_HI and o.side == "yes")
        ex.orders[gone.oid]["status"] = "canceled"           # cancelled elsewhere
        clock.t = T0 + 61.0
        bot.step(clock.t)
        self.assertEqual(ex.orders["x9"]["status"], "canceled")
        self.assertEqual(ex.orders["m9"]["status"], "resting")
        self.assertNotIn(gone.oid, bot.orders)

    def test_order_errors_alert_then_halt(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, cfg={"order_error_alert": 2, "order_error_halt": 4})
        ex.fail["create"] = [L.ApiError("HttpError(400)", 400, "insufficient balance")
                             for _ in range(20)]
        started(bot, clock)
        self.assertTrue(any("order errors" in s for s, _ in mails))
        self.assertIsNotNone(bot.halt_reason)

    def test_post_only_cross_rejects_do_not_alert(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, cfg={"order_error_alert": 2, "order_error_halt": 4})
        ex.fail["create"] = [L.ApiError("HttpError(400)", 400, '{"code":"post_only_cross"}')
                             for _ in range(5)]
        started(bot, clock)
        self.assertIsNone(bot.halt_reason)
        self.assertEqual(mails, [])
        orows = rows(os.path.join(bot.dir, f"orders_{L.utc_day(T0)}.jsonl"))
        self.assertTrue(any(r["kind"] == "reject" and "post_only" in r["error_body"]
                            for r in orows))

    def test_position_mismatch_halts_event(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 10.0, "tr1")))
        clock.t = T0 + 0.5
        bot.step(clock.t)
        ex.positions_rows[EVENT] = [{"ticker": T_MID, "position_fp": "35.00"}]
        bot._on_reader(("positions", EVENT, ex.positions(EVENT), clock.t), clock.t)
        self.assertNotIn(EVENT, bot.halted_events)
        bot._on_reader(("positions", EVENT, ex.positions(EVENT), clock.t), clock.t)
        self.assertIn(EVENT, bot.halted_events)

    def test_fills_restored_after_restart(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 25.0, "tr7")))
        clock.t = T0 + 0.5
        bot.step(clock.t)
        bot2 = L.Bot(bot.cfg, bot.P, bot.variant, True, 7, ex, FakeFeed(), FakeWx(), FakeFair(),
                     data_dir=bot.root_dir, clock=clock, threaded=False, mailer=None, echo=False)
        bot2.startup(clock.t)
        self.assertEqual(bot2.pos[T_MID].yes_q, 25.0)        # log + REST sweep, once


class TestLiveMore(unittest.TestCase):
    def test_reference_flicker_costs_no_writes(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        n0 = len(ex.kinds("amend"))
        # the YES reference drops 40 -> 39 for 1.2 s, then comes back
        feed.books[T_MID] = ({40: 100.0, 39: 1000.0}, BOOKS[T_MID][1])
        for dt in (0.3, 0.6, 0.9, 1.2):
            clock.t = T0 + dt
            bot.step(clock.t)
        feed.books[T_MID] = BOOKS[T_MID]
        for dt in (1.5, 1.8, 3.5, 5.0):
            clock.t = T0 + dt
            bot.step(clock.t)
        self.assertEqual(len(ex.kinds("amend")), n0)
        self.assertEqual(by_market(bot)[T_MID]["yes"], {40: 600.0})
        # a lasting drop is followed after the debounce
        feed.books[T_MID] = ({40: 100.0, 39: 1000.0}, BOOKS[T_MID][1])
        for dt in (6.0, 7.0, 8.5):
            clock.t = T0 + dt
            bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_MID]["yes"], {39: 600.0})


    def test_size_cut_is_a_decrease(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        bot.P["pos_cap"] = 300                  # the risk room shrinks: 600 -> 300
        bot.P_stale = L.stale_policy(bot.P)
        clock.t = T0 + 0.3
        bot.step(clock.t)
        decs = ex.kinds("decrease")
        # live 600 -> 300 (by 300) and the 1c pads 1000 -> 300 (by 700)
        self.assertEqual(sorted(c[2] for c in decs), [300, 300, 700, 700])
        self.assertEqual(by_market(bot)[T_MID], {"yes": {40: 300.0}, "no": {48: 300.0}})
        for c in decs:
            self.assertEqual(float(ex.orders[c[1]]["remaining_count_fp"]),
                             bot.orders[c[1]].remaining)

    def test_periodic_fills_sweep_books_missed_fills(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        ex.fill(yes_mid.oid, 40.0, "tr9")        # the WebSocket never delivers it
        clock.t = T0 + 125.0
        bot.step(clock.t)
        self.assertEqual(bot.pos[T_MID].yes_q, 40.0)
        self.assertTrue(any(c[0] == "fills" for c in ex.calls[2:]))
        frows = rows(os.path.join(bot.dir, f"fills_{L.utc_day(T0)}.jsonl"))
        self.assertEqual(frows[-1]["src"], "rest")

    def test_settlement_results_mark_the_event(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 100.0, "tr1")))
        clock.t = T0 + 0.5
        bot.step(clock.t)
        settled = os.path.join(C.DATA_ROOT, "settled")
        os.makedirs(settled, exist_ok=True)
        with open(os.path.join(settled, f"{EVENT}.json"), "w") as f:
            json.dump({"markets": [{"ticker": T_MID, "result": "yes"},
                                   {"ticker": T_HI, "result": "yes"},
                                   {"ticker": T_LO, "result": "no"}]}, f)
        clock.t = S + 400.0
        bot.step(clock.t)
        clock.t = S + 401.0                       # the reader acts on the refreshed view
        bot.step(clock.t)
        self.assertEqual(bot.results.get(T_MID), "yes")
        self.assertAlmostEqual(bot.event_pnl(EVENT), 100 * (1.0 - 0.40))

    def test_day_roll_writes_summary(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        nxt = C.et_day_bounds(C.et_day(T0))[1] + 30.0
        clock.t = nxt
        bot.step(nxt)
        summ = rows(os.path.join(bot.dir, "summary.jsonl"))
        self.assertEqual(summ[-1]["day"], C.et_day(T0))
        self.assertEqual(summ[-1]["create"], 6)
        self.assertEqual(bot.et_day, C.et_day(nxt))


class TestGuards(unittest.TestCase):
    def test_ws_unhealthy_cancels_everything(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        feed.ok = False
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})
        self.assertEqual(ex.resting_ours(), {})
        feed.ok = True
        clock.t = T0 + 1.5
        bot.step(clock.t)
        self.assertEqual(len(bot.orders), 4)      # the write budget (3/s) paces the rest
        clock.t = T0 + 3.0
        bot.step(clock.t)
        self.assertEqual(len(bot.orders), 6)

    def test_untrusted_book_cancels_that_market(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        del feed.books[T_HI]
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertNotIn(T_HI, by_market(bot))
        self.assertIn(T_MID, by_market(bot))
        self.assertGreater(bot.guard_counts["book_untrusted"], 0)

    def test_stale_wx_pulls_live_near_fav_keeps_pads(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        fair.stale = True
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertNotIn(T_MID, by_market(bot))               # pulled at once
        self.assertFalse([o for o in bot.orders.values() if o.price > 1])
        clock.t = T0 + 1.5                                    # paced pads follow
        bot.step(clock.t)
        got = by_market(bot)
        # the favorite (95c) is pulled; the 1c longshot pad stays, and a 1c
        # qualification pad lifts the favorite side's thin book to 1,050
        self.assertEqual(got[T_HI], {"yes": {1: 250.0}, "no": {1: 1000.0}})
        self.assertEqual(got[T_LO], {"yes": {1: 1000.0}, "no": {1: 250.0}})
        self.assertTrue(all(o.price == 1 for o in bot.orders.values()))
        self.assertNotIn(T_MID, got)
        st = bot.status(clock.t)
        self.assertIn("wx_stale", st["events"][EVENT]["guards"])

    def test_no_fair_no_quotes(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        fair.fairs.pop(T_MID)
        started(bot, clock)
        self.assertNotIn(T_MID, by_market(bot))
        self.assertIn(T_HI, by_market(bot))

    def test_no_program_no_quotes(self):
        ex = DryExchange()
        ex.programs_rows = program_rows([T_HI, T_LO])
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, ex=ex)
        started(bot, clock)
        self.assertNotIn(T_MID, by_market(bot))
        self.assertIn(T_HI, by_market(bot))
        ex.programs_rows = []
        bot.reader.force.add("programs")
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})

    def test_eager_program_polling_after_open(self):
        ex = DryExchange()
        ex.programs_rows = []
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, ex=ex)
        started(bot, clock, t=OPEN + 30.0)
        self.assertTrue(bot.reader_view["eager_programs"])
        self.assertEqual(bot.reader.programs_every(OPEN + 31.0), 5.0)
        ex.programs_rows = program_rows()
        bot.reader.force.add("programs")
        clock.t = OPEN + 130.0
        bot.step(clock.t)
        bot.step(clock.t + 0.1)
        self.assertFalse(bot.reader_view["eager_programs"])
        self.assertEqual(bot.reader.programs_every(OPEN + 131.0), 15.0)
        self.assertEqual(bot.reader.programs_every(OPEN + 1200.0), 300.0)

    def test_before_program_start_nothing(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock, t=OPEN + 60.0)
        self.assertEqual(bot.orders, {})

    def test_after_close_nothing(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        clock.t = S - 0.5
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})

    def test_halt_file_idles_then_resumes(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        open(bot.halt_path, "w").close()
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})
        self.assertEqual(ex.resting_ours(), {})
        n = len(ex.kinds("create"))
        clock.t = T0 + 2.0
        bot.step(clock.t)
        self.assertEqual(len(ex.kinds("create")), n)
        self.assertIsNone(bot.halt_reason)
        os.remove(bot.halt_path)
        clock.t = T0 + 3.5
        bot.step(clock.t)
        self.assertEqual(len(bot.orders), 6)

    def test_paced_writes_and_caps(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=False, cfg={"write_burst": 2.0, "write_rate": 0.001})
        started(bot, clock)
        self.assertEqual(len(bot.orders), 2)
        self.assertGreater(bot.deferred["write_rate"], 0)
        bot2, *_r, clock2 = make_bot(live=False, cfg={"max_new_orders_min": 3})
        started(bot2, clock2)
        self.assertEqual(len(bot2.orders), 3)
        self.assertGreater(bot2.deferred["new_orders_cap"], 0)

    def test_min_act_paces_raises_not_cuts(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        # the NO reference rises; a raise must wait min_act_s ...
        feed.books[T_MID] = ({40: 500.0, 39: 1000.0}, {45: 500.0, 44: 1000.0})
        fair.fairs[T_MID] = 0.55
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_MID]["yes"], {40: 600.0})
        # ... a cut (a station reading drops the fair: the YES cap falls under
        # 40) goes at once, on the reading, not the next tick
        fair.fairs[T_MID] = 0.30
        wx.rows.append({"rx": T0 + 0.45, "t": 0})
        clock.t = T0 + 0.5
        bot.step(clock.t)
        ys = by_market(bot)[T_MID]["yes"]
        self.assertTrue(all(p < 40 for p in ys), ys)

    def test_max_contracts_clip(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False,
                                                         cfg={"max_contracts_order": 250})
        started(bot, clock)
        self.assertTrue(all(o.remaining <= 250 for o in bot.orders.values()))


class TestExchangeWire(unittest.TestCase):
    """The real Exchange's request shapes, with the HTTP client swapped
    for a recorder (no network)."""

    class Rec:
        events_orders_url = "/portfolio/events/orders"
        host = "https://fake/trade-api/v2"
        http_timeout = (1, 1)

        def __init__(self):
            self.calls = []
            from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
            self._build = ExchangeClient._build_v2_order_body
            self._cents_to_dollar_str = ExchangeClient._cents_to_dollar_str

        def _build_v2_order_body(self, **kw):
            return self._build(self, **kw)

        def post(self, path, body):
            self.calls.append(("POST", path, json.loads(body)))
            return {"order_id": "o1", "fill_count": "0.00", "remaining_count": "10.00"}

        def delete(self, path, params=None):
            self.calls.append(("DELETE", path, dict(params or {})))
            return {"order_id": "o1", "reduced_by": "10.00"}

        def get(self, path, params=None):
            self.calls.append(("GET", path, dict(params or {})))
            return {"orders": [], "balance_dollars": "12.34"}

    def ex(self, sub=5, live=True):
        e = L.Exchange(sub, live=live)
        rec = self.Rec()
        e._clients = {"w": rec, "r": rec}
        return e, rec

    def test_create_body(self):
        e, rec = self.ex()
        e.create(T_MID, "no", 48, 600, "tmm-abc", 1792000090)
        m, path, body = rec.calls[0]
        self.assertEqual((m, path), ("POST", "/portfolio/events/orders"))
        self.assertEqual(body["side"], "ask")                 # a NO bid = sell YES
        self.assertEqual(body["price"], "0.52")               # at YES 100 - 48
        self.assertEqual(body["count"], "600.00")
        self.assertTrue(body["post_only"])
        self.assertEqual(body["expiration_time"], 1792000090)
        self.assertEqual(body["time_in_force"], "good_till_canceled")
        self.assertEqual(body["subaccount"], 5)
        self.assertEqual(body["client_order_id"], "tmm-abc")
        e.create(T_MID, "yes", 40, 10, "tmm-d", 1)
        self.assertEqual((rec.calls[1][2]["side"], rec.calls[1][2]["price"]), ("bid", "0.40"))

    def test_amend_decrease_cancel_shapes(self):
        e, rec = self.ex()
        o = _o("oid9", "no", 48, 600)
        e.amend(o, 47, 600.0, 1792000100)
        m, path, body = rec.calls[-1]
        self.assertEqual(path, "/portfolio/events/orders/oid9/amend?subaccount=5")
        self.assertEqual(body, {"ticker": T_MID, "side": "ask", "price": "0.53",
                                "count": "600.00", "expiration_time": 1792000100})
        e.decrease(o, 100)
        self.assertEqual(rec.calls[-1][1], "/portfolio/events/orders/oid9/decrease?subaccount=5")
        self.assertEqual(rec.calls[-1][2], {"reduce_by": "100.00", "market_ticker": T_MID})
        e.cancel("oid9", T_MID)
        self.assertEqual(rec.calls[-1], ("DELETE", "/portfolio/events/orders/oid9",
                                         {"subaccount": 5, "exchange_index": -1,
                                          "market_ticker": T_MID}))

    def test_errors_become_api_errors(self):
        e, rec = self.ex()

        class HttpErr(Exception):
            status = 400
            body = '{"code":"invalid_parameters"}'

        def boom(path, body):
            raise HttpErr("HttpError(400 Bad Request)")
        rec.post = boom
        with self.assertRaises(L.ApiError) as cm:
            e.create(T_MID, "yes", 40, 10, "tmm-x", 1)
        self.assertEqual((cm.exception.status, cm.exception.body),
                         (400, '{"code":"invalid_parameters"}'))

    def test_read_wrappers(self):
        e, rec = self.ex(sub=5, live=False)
        calls = []

        def get(path, params=None):
            calls.append((path, dict(params or {})))
            if path == "/portfolio/orders/missing":
                raise L.ApiError("404", 404)
            if path.startswith("/portfolio/orders/"):
                return {"order": {"order_id": "x", "subaccount_number": 5}}
            if path == "/portfolio/fills":
                return {"fills": [{"ticker": T_MID, "trade_id": "a"}, {"ticker": "KXOTHER-1"}]}
            if path == "/portfolio/positions":
                return {"market_positions": [{"ticker": T_MID, "position_fp": "3.00"}]}
            return {"orders": [{"order_id": "o", "ticker": T_MID}]}
        rec.get = get
        self.assertIsNone(e.get_order("missing"))
        self.assertEqual(e.get_order("x", scoped=False)["subaccount_number"], 5)
        self.assertNotIn("subaccount", calls[-1][1])
        self.assertEqual([f["trade_id"] for f in e.fills(100)], ["a"])
        self.assertEqual(calls[-1][1], {"min_ts": 100, "limit": 1000, "subaccount": 5})
        self.assertEqual(e.positions(EVENT)[0]["position_fp"], "3.00")
        e.event_orders([EVENT, "KXTEMPMIAH-26OCT1410"])
        self.assertEqual(calls[-1][1]["event_ticker"], f"{EVENT},KXTEMPMIAH-26OCT1410")

    def test_programs_early_stop(self):
        e, rec = self.ex(sub=5, live=False)
        seen = []
        now = 3600.0 * 500000 + 20 * 60

        def md(kind, path, params):
            seen.append(dict(params))
            page = [{"market_ticker": f"{EVENT}-T80.99", "start_date": C.iso(now - 60)},
                    {"market_ticker": "KXOTHER-1", "start_date": C.iso(now - 9000)}]
            return {"incentive_programs": page, "next_cursor": "c2"}
        e._md = md
        got = e.programs(now)
        self.assertEqual(len(seen), 1)                         # stopped after page 1
        self.assertEqual(seen[0]["status"], "active")
        self.assertEqual([p["market_ticker"] for p in got], [f"{EVENT}-T80.99"])

    def test_reads_are_subaccount_scoped(self):
        e, rec = self.ex(sub=5, live=False)
        self.assertAlmostEqual(e.balance(), 12.34)
        e.resting()
        self.assertEqual(rec.calls[-1][2]["subaccount"], 5)
        self.assertEqual(rec.calls[-1][2]["status"], "resting")


class TestWx(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=_SANDBOX, prefix="wx_")

    def _write(self, ts, recs, partial=b""):
        t = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
        d = os.path.join(self.root, "raw", t.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"wx_{t.strftime('%H')}.jsonl")
        with open(p, "ab") as f:
            for r in recs:
                f.write((json.dumps(r) + "\n").encode())
            f.write(partial)
        return p

    @staticmethod
    def row(rx, t_ms, temps=(27, 27, 27, 27, 27), status="incomplete"):
        st = [{"station_id": s, "temp_f": round(c * 1.8 + 32, 1), "code": "pending",
               "source": "hf_asos", "received_at_ms": int(rx * 1000)}
              for s, c in zip(C.STATIONS["miami"], temps)]
        return {"rx": rx, "city": "miami", "t": t_ms, "status": status, "v": None,
                "contributors": None, "stations": st}

    def test_tail_history_poll_partial_and_roll(self):
        h = 3600.0 * 500000
        tail = L.WxTail(root=self.root)
        self._write(h - 1200, [self.row(h - 1200, int((h - 1500) * 1000))])
        self._write(h + 10, [self.row(h + 10, int((h - 200) * 1000))])
        hist = tail.history(h + 20)
        self.assertEqual([r["rx"] for r in hist], [h - 1200, h + 10])
        self.assertEqual(tail.poll(h + 21), [])
        line = json.dumps(self.row(h + 30, int((h - 140) * 1000))).encode()
        self._write(h + 30, [], partial=line[:40])
        self.assertEqual(tail.poll(h + 31), [])               # a torn line waits
        self._write(h + 30, [], partial=line[40:] + b"\n")
        got = tail.poll(h + 32)
        self.assertEqual([r["rx"] for r in got], [h + 30])
        self.assertEqual(tail.last_row_at, h + 32)
        # the next hour's file is picked up from its start
        self._write(h + 3601, [self.row(h + 3601, int((h + 3400) * 1000))])
        got = tail.poll(h + 3602)
        self.assertEqual([r["rx"] for r in got], [h + 3601])

    def test_fallback_poller_engages_and_backs_off(self):
        h = 3600.0 * 500001
        tail = L.WxTail(root=self.root)
        calls = []
        body = {"timeseries": [{"t": int((h - 180) * 1000), "status": "incomplete",
                                "v": None, "stations": self.row(h, 0)["stations"]}]}
        resp = [(429, None), (200, body)]

        def fetch():
            calls.append(1)
            return resp.pop(0) if resp else (200, body)
        poller = L.WxPoller(1.0, fetch=fetch)
        src = L.WxSource(L.Config(), tail, poller, status_path=os.path.join(self.root, "none.json"),
                         threaded=False)
        now = h + 30.0                                        # inside :13-:50, no file
        self.assertEqual(src.poll(now), [])                   # 429
        self.assertTrue(src.polling)
        self.assertGreater(poller.backoff_until, now)
        self.assertEqual(src.poll(now + 1.0), [])             # backing off: no request
        self.assertEqual(len(calls), 1)
        rows_ = src.poll(now + 2.5)
        self.assertEqual(len(rows_), 1)
        self.assertEqual(rows_[0]["city"], "miami")
        # the collector's stream comes back: polling stops
        self._write(now + 3, [self.row(now + 3, int((h - 120) * 1000))])
        src.poll(now + 3.5)
        self.assertFalse(src.polling)

    def test_collector_heartbeat_means_quiet_not_stale(self):
        h = 3600.0 * 500002
        self._write(h + 1, [self.row(h + 1, int((h - 180) * 1000))])
        tail = L.WxTail(root=self.root)
        tail.history(h + 2)
        tail.last_row_at = h + 2
        sp = os.path.join(self.root, "collector_status.json")
        now = h + 40.0                                        # 38 s without a row, in window
        with open(sp, "w") as f:
            json.dump({"rx": now - 3, "wx": {"miami": {"last_ok_age_s": 1.2}}}, f)
        src = L.WxSource(L.Config(), tail, None, status_path=sp, threaded=False)
        self.assertFalse(src.tail_stale(now))
        # alive but backing off a 429 for 30 s: still trusted (no crowding)
        with open(sp, "w") as f:
            json.dump({"rx": now - 3, "wx": {"miami": {"last_ok_age_s": 30.0}}}, f)
        src._coll = (-1e18, (False, 0.0))
        self.assertFalse(src.tail_stale(now))
        # alive, but no successful station poll for > 60 s and no row for > 90 s
        tail.last_row_at = now - 95
        src._coll = (-1e18, (False, 0.0))
        with open(sp, "w") as f:
            json.dump({"rx": now - 3, "wx": {"miami": {"last_ok_age_s": 70.0}}}, f)
        self.assertTrue(src.tail_stale(now))
        # the heartbeat is stale (collector dead): inside :13-:50 = stale
        tail.last_row_at = h + 2
        with open(sp, "w") as f:
            json.dump({"rx": now - 100, "wx": {"miami": {"last_ok_age_s": 1.2}}}, f)
        src._coll = (-1e18, (False, 0.0))
        self.assertTrue(src.tail_stale(now))

    def test_fair_engine_real_model(self):
        h = 3600.0 * 500003
        ev = L.EventInfo(EVENT, h, h + 3600, h + 3600, [(80.99, "A"), (79.99, "B")])
        fe = L.FairEngine()
        rows_ = []
        for i in range(38):                                   # 38 minutes of 5 stations
            m = h - 2400 + 60 * i
            rows_.append(self.row(m + 150, int(m * 1000), temps=(27, 27, 27, 27, 28)))
        fe.add(rows_, h + 60)
        r = fe.result(ev, h + 60, 180.0)
        self.assertFalse(r.stale, r.reason)
        self.assertEqual(set(r.fairs), {"A", "B"})
        self.assertTrue(all(0.0 <= f <= 1.0 for f in r.fairs.values()))
        self.assertGreater(r.fairs["B"], r.fairs["A"])        # the lower strike is likelier
        self.assertTrue(all(g >= 0 for g in r.slopes.values()))
        r2 = fe.result(ev, h + 60 + 200, 180.0)               # no new minute for 200 s
        self.assertTrue(r2.stale)
        self.assertEqual(L.FairEngine().result(ev, h, 180.0).stale, True)   # no data


if __name__ == "__main__":
    unittest.main()
