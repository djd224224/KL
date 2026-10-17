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
import time              # noqa: E402
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
        self.report_sub = True      # GetOrder reports subaccount_number
        self.scoped = True          # cancels and resting reads reach only the named
                                    # subaccount (Kalshi's `subaccount` query param)
        self.batch_ignores_sub = False   # a batch that answers not_found for everything
        self.fail_reads = False     # the confirm reads time out

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
        return [dict(o) for o in self.orders.values() if o["status"] == "resting"
                and (not self.scoped or o["subaccount_number"] == self.sub)]

    def _read_fail(self):
        if self.fail_reads:
            raise L.ApiError("ReadTimeout: read timed out", None)

    def event_orders(self, events):
        self.calls.append(("event_orders", tuple(events)))
        self._read_fail()
        if not self.listable:
            return []
        return [dict(o) for o in self.orders.values()
                if o["ticker"].rsplit("-", 1)[0] in events and o["subaccount_number"] == self.sub]

    def get_order(self, oid, sync=False):
        """GetOrder takes no subaccount param: any order of the account."""
        self.calls.append(("get_order", oid, sync))
        self._read_fail()
        o = self.orders.get(oid)
        if o is None or not self.readable:
            return None
        r = dict(o)
        if not self.report_sub:
            r.pop("subaccount_number", None)
        return r

    def ticker_orders(self, ticker, sync=False):
        self.calls.append(("ticker_orders", ticker, sync))
        self._read_fail()
        if not self.listable:
            return []
        return [dict(o) for o in self.orders.values()
                if o["ticker"] == ticker and o["subaccount_number"] == self.sub]

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

    def _reach(self, r, sub):
        return (r is not None and r["status"] == "resting"
                and (not self.scoped or r["subaccount_number"] == (self.sub if sub is None else sub)))

    def cancel(self, oid, ticker, subaccount=None):
        self.calls.append(("cancel", oid, ticker, subaccount))
        self._maybe_fail("cancel")
        r = self.orders.get(oid)
        if not self._reach(r, subaccount):
            raise L.ApiError("HttpError(404 Not Found)", 404, "not found")
        r["status"] = "canceled"
        return {"order_id": oid, "reduced_by": r["remaining_count_fp"]}

    def batch_cancel(self, items):
        self.calls.append(("batch_cancel", tuple(items)))
        self._maybe_fail("batch_cancel")
        out = []
        for it in items:
            oid = it[0]
            r = self.orders.get(oid)
            if self.batch_ignores_sub or not self._reach(r, it[2] if len(it) > 2 else None):
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
        # ... a move DOWN is never debounced (review 10/16: we would rest above
        # the policy's price, alone at the top, while others back away)
        ops = L.plan_market(T_MID, [o], {39: 600.0}, {}, {}, 100.0, self.cfg, {40: 500.0},
                            {55: 500.0}, caps={"yes": 45}, stable={"yes": False})
        self.assertEqual([(x.kind, x.price, x.urgent) for x in ops], [("amend", 39, False)])
        # ... a move UP (more aggressive) waits until the price has held
        ops = L.plan_market(T_MID, [o], {41: 600.0}, {}, {}, 100.0, self.cfg, {40: 500.0},
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

    def test_position_mismatch_halts_after_a_fills_sweep(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes_mid = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes_mid.oid, 10.0, "tr1")))
        clock.t = T0 + 0.5
        bot.step(clock.t)
        ex.positions_rows[EVENT] = [{"ticker": T_MID, "position_fp": "35.00"}]
        bot._on_reader(("positions", EVENT, ex.positions(EVENT), clock.t), clock.t)
        self.assertIsNone(bot.halt_reason)
        self.assertIn(T_MID, bot.suspect)                    # held, a sweep forced
        self.assertTrue({"fills", "verify"} <= bot.reader.force)
        # the same mismatch again without a fills sweep in between: still check 1
        bot._on_reader(("positions", EVENT, ex.positions(EVENT), clock.t), clock.t)
        self.assertIsNone(bot.halt_reason)
        # the next verify (every verify_s) sweeps fills, then reads positions:
        # still 35 vs 10 -> HALT
        for dt in (1.0, 1.5, 6.0):
            clock.t = T0 + dt
            bot.step(clock.t)
        self.assertTrue(bot.halt_reason and "position mismatch" in bot.halt_reason)
        self.assertEqual(ex.resting_ours(), {})

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
    def test_reference_flicker_up_costs_no_writes_drop_followed_at_once(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        n0 = len(ex.kinds("amend"))
        # the YES reference RISES 40 -> 41 for 1.2 s, then comes back: no write
        feed.books[T_MID] = ({41: 300.0, 40: 500.0, 39: 1000.0}, BOOKS[T_MID][1])
        for dt in (0.3, 0.6, 0.9, 1.2):
            clock.t = T0 + dt
            bot.step(clock.t)
        feed.books[T_MID] = BOOKS[T_MID]
        for dt in (1.5, 1.8, 3.5, 5.0):
            clock.t = T0 + dt
            bot.step(clock.t)
        self.assertEqual(len(ex.kinds("amend")), n0)
        self.assertEqual(by_market(bot)[T_MID]["yes"], {40: 600.0})
        # a lasting rise is followed after the debounce
        feed.books[T_MID] = ({41: 300.0, 40: 500.0, 39: 1000.0}, BOOKS[T_MID][1])
        for dt in (6.0, 7.0, 8.5):
            clock.t = T0 + dt
            bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_MID]["yes"], {41: 600.0})
        # a DROP (others pull 41 and 40) is followed at the next paced moment,
        # not after 2 s: we never rest above the policy's price for long
        feed.books[T_MID] = ({40: 100.0, 39: 1000.0}, BOOKS[T_MID][1])
        clock.t = T0 + 9.6
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
        # back: fills may have been missed while it was down -> every market of
        # the traded event is held until a fills sweep + positions read agree
        self.assertEqual(set(bot.suspect), set(TICKERS))
        self.assertEqual(bot.orders, {})
        n_pos = len(ex.kinds("positions"))
        clock.t = T0 + 1.6
        bot.step(clock.t)
        self.assertGreater(len(ex.kinds("positions")), n_pos)
        self.assertEqual(bot.suspect, {})
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
        e._clients = {"w": rec, "r": rec, "s": rec}
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
        self.assertEqual(e.get_order("x")["subaccount_number"], 5)
        self.assertNotIn("subaccount", calls[-1][1])          # GetOrder takes no params
        e.ticker_orders(T_MID)
        self.assertEqual(calls[-1], ("/portfolio/orders",
                                     {"ticker": T_MID, "limit": 1000, "subaccount": 5}))
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


# --------------------------------------------------------------------------
# Review fixes (2026-10-16 reviews of the live executor): one regression
# test (or more) per finding, named f<N>_ after the review's order (1-26):
#  f1 positions blind spot, f2 resting WCL (policy, see test_temp_mm_policy),
#  f3/f10 cancels that cannot reach / do not land, f4 ambiguous create,
#  f5 urgent reprice failure + expiry on renewals only, f6 size cut with a
#  price move, f7/f12 --cancel-all under the lock, f8/f22 dry HALT,
#  f9 429 storm, f11 GetOrder is not scoped, f13 409 is not gone, f14 fill
#  price, f15 missed fills on cancel 404 / reconnect, f16 downward moves not
#  debounced, f17 guard-forced moves batched, f18 default model, f19 short
#  history, f20 results after a restart, f21 outage, f23 per-event state
#  pruned, f24 poller cadence, f25 client bounds, f26 settle vs close.
# --------------------------------------------------------------------------

class NoFillsEx(FakeExchange):
    """Neither the WebSocket nor GET /portfolio/fills delivers our fills."""

    def fills(self, min_ts):
        self.calls.append(("fills", min_ts))
        return []


def step_until(bot, clock, t_end, dt=0.25, stop=None):
    t = clock.t
    while t < t_end - 1e-9:
        t = min(t_end, t + dt)
        clock.t = t
        bot.step(t)
        if bot.halt_reason or (stop is not None and stop()):
            break
    return clock.t


def creates_on(ex, ticker, side):
    return [c for c in ex.kinds("create") if c[1] == ticker and c[2] == side]


class TestReviewPositions(unittest.TestCase):
    def test_f1_positions_read_for_a_traded_event_without_a_believed_position(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        self.assertEqual(bot.pos, {})
        clock.t = T0 + 301.0
        bot.step(clock.t)
        self.assertIn(("positions", EVENT), ex.calls)

    def test_f1_f15_unseen_fill_is_never_requoted_and_halts(self):
        # Kalshi fills our whole T_MID YES order; no fill reaches the bot. The
        # :18.9 pull finds it gone (not_found, then a single cancel's 404): the
        # market is held, positions show +600 vs our 0 twice across a fills
        # sweep -> HALT, and the 600 is never re-placed (the review's demo
        # grew it to 2,840 against pos_cap 1,500)
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=NoFillsEx())
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        ex.fill(yes.oid, 600.0, "trX")
        ex.positions_rows[EVENT] = [{"ticker": T_MID, "position_fp": "600.00"}]
        n0 = len(creates_on(ex, T_MID, "yes"))
        step_until(bot, clock, M + 60.0)
        self.assertTrue(bot.halt_reason and "position mismatch" in bot.halt_reason,
                        bot.halt_reason)
        self.assertEqual(len(creates_on(ex, T_MID, "yes")), n0)
        self.assertEqual(ex.resting_ours(), {})
        self.assertTrue(any("HALTED" in s for s, _ in mails))

    def test_f1_kalshi_fill_count_enters_the_risk_inputs_at_once(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=NoFillsEx())
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        ex.fill(yes.oid, 300.0, "trP")                       # a resting read will show it
        seen = []
        orig = L.pol.decide

        def spy(P, ctx, states):
            seen.append({s.ticker: s for s in states})
            return orig(P, ctx, states)
        L.pol.decide = spy
        try:
            clock.t = T0 + 61.0                              # the 60 s resting reconcile
            bot.step(clock.t)
        finally:
            L.pol.decide = orig
        st = seen[-1][T_MID]
        self.assertEqual((st.yes_pos, st.no_pos), (300.0, 0.0))
        self.assertAlmostEqual(st.cost, 120.0)
        self.assertIn(T_MID, bot.suspect)
        self.assertEqual(bot.pos.get(T_MID, L.MarketPos()).yes_q, 0.0)   # not booked as a fill

    def test_f15_cancel_404_holds_the_market_until_fills_and_positions_agree(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        ex.fill(yes.oid, 600.0, "trQ")                       # the REST sweep has it, WS not
        ex.positions_rows[EVENT] = [{"ticker": T_MID, "position_fp": "600.00"}]
        step_until(bot, clock, M + 19.0)                     # the :18.9 pull
        self.assertIn(T_MID, bot.suspect)
        self.assertTrue({"fills", "verify"} & bot.reader.force or bot.reader_view["verify_events"])
        step_until(bot, clock, M + 19.5)
        self.assertNotIn(T_MID, bot.suspect)
        self.assertEqual(bot.pos[T_MID].yes_q, 600.0)
        i_fills = max(i for i, c in enumerate(ex.calls) if c[0] == "fills")
        step_until(bot, clock, M + 47.0)                     # the :45.9 restore
        i_new = [i for i, c in enumerate(ex.calls)
                 if c[0] == "create" and c[1] == T_MID and c[2] == "yes"][-1]
        self.assertLess(i_fills, i_new)                      # booked before re-quoting

    def test_f15_suspect_market_holds_creates_not_cancels(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        y = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        bot.suspect[T_MID] = clock.t
        n = len(ex.kinds("create"))
        clock.t = T0 + 2.0
        bot._execute([L.Op("create", T_MID, "no", 30, 100, None, False, "new", "live"),
                      L.Op("cancel", T_MID, "yes", 40, 600, y, True, "pull", "live")], clock.t)
        self.assertEqual(len(ex.kinds("create")), n)          # held
        self.assertGreater(bot.deferred["suspect"], 0)
        self.assertEqual(ex.orders[y.oid]["status"], "canceled")   # a cancel always goes


class TestReviewCancels(unittest.TestCase):
    def test_f3_misbooked_order_cancelled_where_it_booked_and_reported(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, booked_sub=0)
        started(bot, clock)
        self.assertIn(("cancel", "ord1", T_HI, 0), ex.calls)    # the primary, not ?subaccount=7
        self.assertEqual(ex.orders["ord1"]["status"], "canceled")
        body = next(b for s, b in mails if "HALTED" in s)
        self.assertIn("booked to subaccount 0: cancelled (read back)", body)

    def test_f3_misbooked_order_that_cannot_be_cancelled_is_named_in_the_email(self):
        class Stuck(FakeExchange):
            def cancel(self, oid, ticker, subaccount=None):
                if subaccount is not None:
                    self.calls.append(("cancel", oid, ticker, subaccount))
                    raise L.ApiError("HttpError(404 Not Found)", 404, "not found")
                return super().cancel(oid, ticker, subaccount)
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=Stuck(booked_sub=0))
        started(bot, clock)
        self.assertEqual(ex.orders["ord1"]["status"], "resting")
        body = next(b for s, b in mails if "HALTED" in s)
        self.assertIn("NOT everything was cancelled", body)
        self.assertIn("ord1", body)
        self.assertIn("by hand", body)

    def test_f3_batch_not_found_is_confirmed_by_a_single_cancel(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        mid = [o.oid for o in bot.orders.values() if o.ticker == T_MID]
        ex.batch_ignores_sub = True          # the batch answers not_found for orders that rest
        step_until(bot, clock, M + 19.0)
        self.assertTrue(all(ex.orders[oid]["status"] == "canceled" for oid in mid))
        self.assertEqual({c[1] for c in ex.kinds("cancel")}, set(mid))
        self.assertEqual(bot.suspect, {})                    # the singles worked: nothing gone

    def test_f3_order_still_resting_after_our_cancel_halts(self):
        class Lying(FakeExchange):
            def batch_cancel(self, items):
                self.calls.append(("batch_cancel", tuple(items)))
                return [{"order_id": it[0], "reduced_by": "1.00"} for it in items]

            def cancel(self, oid, ticker, subaccount=None):
                self.calls.append(("cancel", oid, ticker, subaccount))
                return {"order_id": oid, "reduced_by": "1.00"}
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=Lying())
        started(bot, clock)
        step_until(bot, clock, M + 70.0)
        self.assertTrue(bot.halt_reason and "still rests" in bot.halt_reason, bot.halt_reason)

    def test_f13_409_is_not_gone_and_not_counted(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        mid = {o.oid for o in bot.orders.values() if o.ticker == T_MID}
        ex.fail["batch_cancel"] = [L.ApiError("HttpError(409 Conflict)", 409,
                                              '{"code":"trading_is_paused"}')]
        step_until(bot, clock, M + 19.0)
        self.assertEqual({o.oid for o in bot.orders.values() if o.ticker == T_MID}, mid)
        self.assertEqual(len(bot.errors), 0)
        self.assertGreater(bot.write_backoff_until, clock.t)
        step_until(bot, clock, M + 20.5)                     # after the backoff: cancelled
        self.assertEqual([o for o in bot.orders.values() if o.ticker == T_MID], [])
        self.assertTrue(all(ex.orders[oid]["status"] == "canceled" for oid in mid))
        self.assertEqual(bot.suspect, {})

    def test_f7_cancel_all_runs_while_a_live_bot_holds_the_lock(self):
        lock = L.SingleInstance(os.path.join(L.DATA_DIR, "tmm_live.lock"))
        self.assertTrue(lock.acquire())
        state = {"rows": [{"order_id": "a1", "client_order_id": "tmm-1", "ticker": T_MID},
                          {"order_id": "m1", "client_order_id": "manual", "ticker": T_MID}],
                 "cancels": []}

        class StubEx:
            def __init__(self, sub, live=False, **kw):
                self.sub, self.live = sub, live

            def resting(self):
                return [r for r in state["rows"] if r["order_id"] not in state["cancels"]]

            def cancel(self, oid, ticker, subaccount=None):
                state["cancels"].append(oid)
                return {}
        orig = L.Exchange
        L.Exchange = StubEx
        hp = os.path.join(L.DATA_DIR, "HALT")
        try:
            code = L.main(["--live", "--subaccount", "5", "--cancel-all", "--quiet"])
            self.assertTrue(os.path.exists(hp))              # a running bot pauses too
        finally:
            L.Exchange = orig
            lock.release()
            if os.path.exists(hp):
                os.remove(hp)
        self.assertEqual(code, L.EXIT_OK)
        self.assertEqual(state["cancels"], ["a1"])           # never a non-tmm order

    def test_f8_f22_dry_halt_never_pauses_live(self):
        dry, *_r, dclock = make_bot(live=False)
        started(dry, dclock)
        dry.halt(dclock.t, "test: a dry step-exception storm")
        self.assertTrue(os.path.exists(os.path.join(dry.root_dir, "dry", "HALT")))
        self.assertFalse(os.path.exists(os.path.join(dry.root_dir, "HALT")))
        ex = FakeExchange()
        feed = FakeFeed()
        clock = Clock(T0)
        live = L.Bot(L.Config(email=False), pol.make_policy("F_full"), "F_full", True, 7, ex,
                     feed, FakeWx(), FakeFair(), data_dir=dry.root_dir, clock=clock,
                     threaded=False, mailer=None, echo=False)
        feed.bot = live
        started(live, clock)
        self.assertEqual(len(live.orders), 6)                # the dry HALT is not the live one
        open(os.path.join(dry.root_dir, "HALT"), "w").close()   # Jack's pause: both obey
        clock.t = T0 + 0.5
        live.step(clock.t)
        self.assertEqual(live.orders, {})


class TestReviewWrites(unittest.TestCase):
    def test_f4_ambiguous_create_is_adopted_not_duplicated(self):
        class TimeoutOnce(FakeExchange):
            def create(self, ticker, side, price, count, coid, expiration_ts):
                r = super().create(ticker, side, price, count, coid, expiration_ts)
                if ticker == T_MID and side == "yes" and not getattr(self, "_done", False):
                    self._done = True
                    raise L.ApiError("ReadTimeout: read timed out", None)   # booked, no answer
                return r
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=TimeoutOnce())
        started(bot, clock)
        self.assertEqual(len(bot.ambiguous), 1)
        self.assertIn("resting", bot.reader.force)
        step_until(bot, clock, T0 + 3.0)
        rest = [o for o in ex.resting_ours().values()
                if o["ticker"] == T_MID and o["side_ours"] == "yes"]
        self.assertEqual(len(rest), 1)                       # no duplicate at 40
        self.assertIn(rest[0]["order_id"], bot.orders)       # and it is tracked
        self.assertEqual(bot.ambiguous, {})
        self.assertEqual(len(bot.errors), 0)

    def test_f5_urgent_reprice_refused_cancels_the_order(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        fair.fairs[T_MID] = 0.30                             # YES cap 28 < 40
        ex.fail["amend"] = [L.ApiError("HttpError(400 Bad Request)", 400, "invalid")]
        wx.rows.append({"rx": T0 + 0.2, "t": 0})
        clock.t = T0 + 0.25
        bot.step(clock.t)
        self.assertEqual(ex.orders[yes.oid]["status"], "canceled")
        self.assertIsNone(bot.halt_reason)

    def test_f5_urgent_reprice_throttled_is_cancelled_after_the_backoff(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        fair.fairs[T_MID] = 0.30
        ex.fail["amend"] = [L.ApiError("HttpError(429)", 429, "too many requests")]
        wx.rows.append({"rx": T0 + 0.2, "t": 0})
        clock.t = T0 + 0.25
        bot.step(clock.t)
        self.assertTrue(yes.must_cancel)
        step_until(bot, clock, T0 + 1.0)
        self.assertEqual(ex.orders[yes.oid]["status"], "canceled")
        self.assertEqual(len(bot.errors), 0)

    def test_f5_only_renewals_carry_an_expiry(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        feed.books[T_MID] = ({39: 500.0, 38: 1000.0}, BOOKS[T_MID][1])   # YES ref 40 -> 39
        step_until(bot, clock, T0 + 2.0)
        rep = [c for c in ex.kinds("amend") if c[2] == 39]
        self.assertTrue(rep)
        self.assertTrue(all(c[4] is None for c in rep))      # a reprice keeps its expiry
        step_until(bot, clock, T0 + 67.0)
        ren = [c for c in ex.kinds("amend") if c[4] is not None]
        self.assertTrue(ren)
        self.assertTrue(all(c[4] >= T0 + 60 for c in ren))

    def test_f5_silently_unextended_expiry_switches_to_replace(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        o = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        o.renewed = True
        o.expires_at = T0 + 500.0                            # what we believe
        clock.t = T0 + 61.0                                  # Kalshi still says T0 + 90
        bot.step(clock.t)
        self.assertFalse(bot.amend_expiry_ok)
        self.assertEqual(o.expires_at, int(T0 + 90))

    def test_f9_429_storm_backs_off_and_never_halts(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        mid = {o.oid for o in bot.orders.values() if o.ticker == T_MID}
        e429 = lambda: L.ApiError("HttpError(429 Too Many Requests)", 429, "")   # noqa: E731
        for k in ("batch_cancel", "cancel", "amend", "create", "decrease"):
            ex.fail[k] = [e429() for _ in range(1000)]
        step_until(bot, clock, M + 18.75)
        n0 = len([c for c in ex.calls if c[0] in ("batch_cancel", "cancel", "amend", "create")])
        step_until(bot, clock, M + 22.0)                     # the pull meets the throttle
        writes = [c for c in ex.calls[:] if c[0] in ("batch_cancel", "cancel", "amend", "create")]
        self.assertIsNone(bot.halt_reason)
        self.assertEqual(len(bot.errors), 0)
        self.assertLessEqual(len(writes) - n0, 4)            # 0.5, 1, 2 s backoff: not 58 in 0.55 s
        self.assertEqual(ex.kinds("cancel"), [])             # no one-by-one after a batch 429
        for k in list(ex.fail):
            ex.fail[k] = []
        step_until(bot, clock, M + 32.0)
        self.assertTrue(all(ex.orders[oid]["status"] == "canceled" for oid in mid))

    def test_f21_outage_backs_off_without_halting(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        e503 = lambda: L.ApiError("HttpError(503 Service Unavailable)", 503, "")   # noqa: E731
        for k in ("batch_cancel", "cancel", "amend", "create", "decrease"):
            ex.fail[k] = [e503() for _ in range(10000)]
        n0 = len(ex.calls)
        step_until(bot, clock, T0 + 120.0)
        self.assertIsNone(bot.halt_reason)
        self.assertIsNotNone(bot.unhealthy_since)
        writes = [c for c in ex.calls[n0:]
                  if c[0] in ("batch_cancel", "cancel", "amend", "create", "decrease")]
        self.assertLess(len(writes), 30)                     # ~ one per 8 s
        for k in list(ex.fail):
            ex.fail[k] = []
        step_until(bot, clock, T0 + 135.0)
        self.assertIsNone(bot.unhealthy_since)               # recovered by itself

    def test_f21_confirm_timeout_while_reads_fail_holds_instead_of_halting(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        self.assertEqual(len(bot.unconfirmed), 5)
        ex.fail_reads = True
        step_until(bot, clock, T0 + 70.0, dt=1.0)
        self.assertIsNone(bot.halt_reason)
        self.assertTrue(bot.confirm_hold)
        ex.fail_reads = False
        step_until(bot, clock, T0 + 82.0, dt=1.0)
        self.assertEqual(bot.unconfirmed, {})
        self.assertFalse(bot.confirm_hold)

    def test_f17_guard_moves_go_as_one_batched_cancel(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        y = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        n = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "no")
        na = len(ex.kinds("amend"))
        ops = [L.Op("amend", T_MID, "yes", 30, 600.0, y, True, "reprice", "live", excess=6000.0),
               L.Op("amend", T_MID, "no", 40, 600.0, n, True, "reprice", "live", excess=4800.0)]
        clock.t = T0 + 2.0
        bot._execute(ops, clock.t)
        self.assertEqual(len(ex.kinds("amend")), na)         # no serial amends
        self.assertEqual({c[0] for c in ex.kinds("batch_cancel")[-1][1]}, {y.oid, n.oid})
        got = {(c[2], c[3]) for c in ex.kinds("create")[-2:]}
        self.assertEqual(got, {("yes", 30), ("no", 40)})     # re-placed post_only

    def test_f17_most_exposed_urgent_amend_first(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        y = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        f = next(o for o in bot.orders.values() if o.ticker == T_HI and o.side == "yes")
        ops = [L.Op("amend", T_HI, "yes", f.price, 300.0, f, True, "renew", "fav", renew=True),
               L.Op("amend", T_MID, "yes", 30, 600.0, y, True, "reprice", "live", excess=6000.0)]
        clock.t = T0 + 2.0
        bot._execute(ops, clock.t)
        self.assertEqual([c[1] for c in ex.kinds("amend")[-2:]], [y.oid, f.oid])


class TestReviewPlan(unittest.TestCase):
    def test_f6_size_cut_with_a_price_move_cuts_at_once(self):
        cfg = L.Config()
        o = _o("o1", "yes", 45, 500)
        tags = {("yes", 44): "live", ("yes", 46): "live"}
        ops = L.plan_side(T_MID, "yes", [o], {44: 100.0}, tags, 100.0, cfg, {44: 500.0},
                          {50: 500.0}, cap=47, stable=False)
        self.assertEqual([(x.kind, x.price, x.size, x.urgent) for x in ops],
                         [("decrease", 45, 400.0, True), ("amend", 44, 100.0, False)])
        # a move UP is debounced, the cut is not
        ops = L.plan_side(T_MID, "yes", [o], {46: 100.0}, tags, 100.0, cfg, {44: 500.0},
                          {50: 500.0}, cap=47, stable=False)
        self.assertEqual([(x.kind, x.price, x.size, x.urgent) for x in ops],
                         [("decrease", 45, 400.0, True)])
        # two orders at the old price: the smaller is cancelled, the larger cut
        a, b = _o("a", "yes", 45, 300, placed=1.0), _o("b", "yes", 45, 200, placed=2.0)
        ops = L.plan_side(T_MID, "yes", [a, b], {44: 100.0}, tags, 100.0, cfg, {44: 500.0},
                          {50: 500.0}, cap=47, stable=True)
        self.assertEqual(sorted((x.kind, x.order.oid, x.size, x.urgent) for x in ops),
                         [("amend", "a", 100.0, False), ("cancel", "b", 200.0, True),
                          ("decrease", "a", 200.0, True)])


class TestReviewFills(unittest.TestCase):
    def test_f14_fill_booked_at_its_own_price(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        body = ex.fill(yes.oid, 100.0, "trF")                # executed at 40c
        yes.price = 38                                       # the bot amended it since
        feed.events.append(("fill", body))
        clock.t = T0 + 0.3
        bot.step(clock.t)
        self.assertAlmostEqual(bot.pos[T_MID].cost, 40.0)
        self.assertEqual(L.fill_price_cents({"yes_price_dollars": "0.4000"}, "no"), 60)
        self.assertEqual(L.fill_price_cents({"yes_price_dollars": "0.40",
                                             "no_price_dollars": "0.61"}, "no"), 61)
        self.assertIsNone(L.fill_price_cents({}, "yes"))

    def test_f20_restart_after_the_close_keeps_the_event_loss(self):
        class Closing(FakeExchange):
            def markets(self, min_close_ts):
                return [m for m in market_rows() if C.parse_iso(m["close_time"]) >= min_close_ts]

            def event_markets(self, event):
                return [{"ticker": t, "result": "no"} for t in TICKERS]
        settled = os.path.join(C.DATA_ROOT, "settled", f"{EVENT}.json")
        if os.path.exists(settled):          # another test's shadow settlement file
            os.remove(settled)
        ex = Closing(cash=5000.0)
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, ex=ex, cfg={"event_loss_halt": 1e9, "daily_loss_halt": 200.0})
        started(bot, clock)
        yes = next(o for o in bot.orders.values() if o.ticker == T_MID and o.side == "yes")
        feed.events.append(("fill", ex.fill(yes.oid, 600.0, "trR")))   # 600 YES @ 40 = $240
        clock.t = T0 + 0.5
        bot.step(clock.t)
        clock.t = S + 180.0                                  # restart 3 min after the close
        bot2 = L.Bot(bot.cfg, bot.P, bot.variant, True, 7, ex, FakeFeed(), FakeWx(), FakeFair(),
                     data_dir=bot.root_dir, clock=clock, threaded=False,
                     mailer=lambda s, b: mails.append((s, b)) or True, echo=False)
        self.assertEqual(bot2.startup(clock.t), L.EXIT_OK)
        self.assertNotIn(EVENT, bot2.events)                 # discovery no longer lists it
        self.assertEqual(bot2.pos[T_MID].yes_q, 600.0)
        step_until(bot2, clock, S + 420.0, dt=1.0)
        self.assertEqual(bot2.results.get(T_MID), "no")
        self.assertAlmostEqual(bot2.event_pnl(EVENT), -240.0)
        self.assertTrue(bot2.halt_reason and "daily loss" in bot2.halt_reason)
        st = json.load(open(bot2.state_path))
        self.assertEqual(st["results"].get(T_MID), "no")     # saved as soon as fetched


class TestReviewFair(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=_SANDBOX, prefix="fair_")

    def test_f18_live_refuses_the_built_in_default_model(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True)
        bot.fair = L.FairEngine(params_path=os.path.join(self.root, "missing.json"),
                                cals_path=os.path.join(self.root, "missing_cals.json"))
        self.assertEqual(bot.fair.params_source, "defaults")
        self.assertEqual(bot.startup(T0), L.EXIT_REFUSED)
        self.assertEqual(ex.kinds("create"), [])
        self.assertTrue(any("refused" in s.lower() for s, _ in mails))
        # a dry run says so and goes on
        dry, *_r, dclock = make_bot(live=False)
        dry.fair = L.FairEngine(params_path=os.path.join(self.root, "missing.json"))
        self.assertEqual(dry.startup(T0), L.EXIT_OK)

    def test_f18_hourly_reload_keeps_the_model_when_the_file_goes(self):
        import temp_shadow_model as tm
        path = os.path.join(self.root, "model_params.json")
        with open(path, "w") as f:
            json.dump(tm.default_params(), f)
        fe = L.FairEngine(params_path=path)
        self.assertEqual(fe.params_source, path)
        model = fe.model
        os.remove(path)
        fe.loaded_at -= 3601.0
        note = fe.maybe_reload(time.time())
        self.assertTrue(note and note.startswith("!"), note)
        self.assertIs(fe.model, model)
        self.assertEqual(fe.params_source, path)

    @staticmethod
    def row(rx, t_ms, c=27):
        return TestWx.row(rx, t_ms, temps=(c, c, c, c, c))

    def test_f19_history_seeded_when_the_collector_files_are_short(self):
        h = 3600.0 * 500010
        now = h + 1800.0
        ts = []
        for i in range(80):                                  # 80 minutes of history
            m = now - 4800 + 60 * i
            st = self.row(m + 140, int(m * 1000))["stations"]
            ts.append({"t": int(m * 1000), "status": "incomplete", "v": None, "stations": st})
        calls = []

        def fetch(a, b):
            calls.append((a, b))
            return 200, {"timeseries": ts}
        src = L.WxSource(L.Config(), L.WxTail(root=self.root), None,
                         status_path=os.path.join(self.root, "none.json"), threaded=False,
                         history_fetch=fetch)
        rows_ = src.start(now)
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0][0], now - L.WX_LOOKBACK_S)
        self.assertGreater(len(rows_), 60)
        self.assertTrue(all(float(r["rx"]) <= now for r in rows_))
        self.assertIn("seeded", src.seed_note)
        fe = L.FairEngine(min_span_s=3600.0)
        fe.add(rows_, now)
        self.assertFalse(fe.short_history())       # from the rows: no snapshot needed (the
        fe.snapshot(now)                           # reader must not seed at every start)
        self.assertGreater(fe.history_span_s(), 3600.0)

    def test_f19_fair_stale_until_the_history_spans_an_hour(self):
        h = 3600.0 * 500011
        rows_ = [self.row(h - 2400 + 60 * i + 150, int((h - 2400 + 60 * i) * 1000))
                 for i in range(38)]                         # 38 minutes
        fe = L.FairEngine(min_span_s=3600.0)
        fe.add(rows_, h + 60)
        ev = L.EventInfo(EVENT, h, h + 3600, h + 3600, [(80.99, "A")])
        r = fe.result(ev, h + 60, 180.0)
        self.assertTrue(r.stale)
        self.assertTrue(r.reason.startswith("short_history"), r.reason)
        older = [self.row(h - 4200 + 60 * i + 150, int((h - 4200 + 60 * i) * 1000))
                 for i in range(30)]                         # 30 more, earlier
        fe.add(older, h + 60)
        r = fe.result(ev, h + 60, 180.0)
        self.assertFalse(r.stale, r.reason)

    def test_f24_fallback_poller_uses_the_collectors_cadence(self):
        calls = []
        resp = []

        def fetch():
            calls.append(1)
            return resp.pop(0) if resp else (200, {"timeseries": []})
        p = L.WxPoller(1.0, fetch=fetch)
        h = 3600.0 * 500012
        self.assertEqual(p.poll_once(h + 5.0), 0)            # outside :13-:50: one per 5 s
        p.poll_once(h + 7.0)
        p.poll_once(h + 10.0)
        self.assertEqual(len(calls), 2)                      # 5.0 and 10.0
        p.poll_once(h + 20.0)
        p.poll_once(h + 21.0)                                # inside: one per second
        self.assertEqual(len(calls), 4)
        resp.append((429, None))
        p.poll_once(h + 22.0)                                # 429: back off 2 s, then yield
        self.assertEqual(len(calls), 5)
        p.poll_once(h + 24.0)
        p.poll_once(h + 25.0)                                # yield: 2 s inside the window
        self.assertEqual(len(calls), 6)
        p.poll_once(h + 26.0)
        self.assertEqual(len(calls), 7)


class TestReviewMisc(unittest.TestCase):
    def test_f11_getorder_without_subaccount_number_does_not_confirm(self):
        ex = FakeExchange(booked_sub=0)
        ex.report_sub = False            # GetOrder leaves subaccount_number out
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, ex=ex)
        started(bot, clock)
        self.assertFalse(bot.first_confirmed)
        self.assertEqual(len(ex.kinds("create")), 1)
        step_until(bot, clock, T0 + 62.0, dt=1.0)
        self.assertEqual(len(ex.kinds("create")), 1)         # nothing more placed
        self.assertTrue(bot.halt_reason and "could not be confirmed" in bot.halt_reason)
        self.assertEqual(ex.orders["ord1"]["status"], "canceled")   # probed in the primary
        # booked where it should be: the subaccount's own order list confirms it
        ex2 = FakeExchange()
        ex2.report_sub = False
        bot2, ex2, *_r, clock2 = make_bot(live=True, ex=ex2)
        started(bot2, clock2)
        self.assertTrue(bot2.first_confirmed)
        self.assertEqual(len(ex2.kinds("create")), 6)

    def test_f25_client_bounds(self):
        e = L.Exchange(5, write_timeout_s=3.0, read_timeout_s=10.0, read_attempts=2,
                       sync_read_timeout_s=3.0)

        class Dummy:
            http_timeout = (5, 20)
            get_attempts = 3
        w, r, s = (e.apply_bounds(Dummy(), k) for k in ("w", "r", "s"))
        self.assertEqual((w.http_timeout, w.get_attempts), ((2.0, 3.0), 1))
        self.assertEqual((r.http_timeout, r.get_attempts), ((5.0, 10.0), 2))
        self.assertEqual((s.http_timeout, s.get_attempts), ((2.0, 3.0), 1))

    def test_f23_closed_event_state_is_pruned(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False)
        started(bot, clock)
        clock.t = S - 0.5
        bot.step(clock.t)
        self.assertIn(EVENT, bot.guards_now)
        clock.t = S + 10.0
        bot.step(clock.t)
        for d in (bot.guards_now, bot.wflag_prev, bot.last_sample, bot.fair_last):
            self.assertNotIn(EVENT, d)

    def test_f26_close_time_off_the_tickers_hour_is_not_quoted(self):
        ex = DryExchange()
        rows_ = market_rows()
        for m in rows_:
            m["close_time"] = C.iso(S + 3600.0)              # e.g. a repeated DST hour
        ex.markets_rows = rows_
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, ex=ex)
        started(bot, clock)
        self.assertEqual(bot.orders, {})
        self.assertIn("settle_vs_close", bot.guards_now[EVENT])
        self.assertEqual(L.build_events(rows_)[EVENT].settle_gap_s, 3600.0)



class TestPadHold(unittest.TestCase):
    """Jack 2026-10-16: "hold the pads for the first 3 minutes of each hour
    going forward" -- live G's first hour (13:02Z): sweepers took 4,659 of
    its 1c fillers within ~45 s of the program opening. G (pad_hold_open_s
    180) rests no 1c longshot or qualification pad while its orders would
    start resting before the open + 180 s; the favorites quote as usual."""

    @staticmethod
    def writes(calls):
        return [c for c in calls if c[0] in ("create", "amend", "decrease", "cancel",
                                             "batch_cancel")]

    def test_no_pads_before_open_plus_180_then_at_once(self):
        self.assertEqual(L.resolve_variant("G"), "G_decided_only")
        self.assertEqual(L.resolve_variant("g0"), "G0_nohold")
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, variant="G_decided_only")
        self.assertEqual(bot.P["pad_hold_open_s"], 180.0)
        started(bot, clock, t=OPEN + 125.0)             # the programs started at +120
        self.assertEqual(sorted((c[1], c[2], c[3]) for c in ex.kinds("create")),
                         [(T_HI, "yes", 95), (T_LO, "no", 95)])    # the favorites only
        for dt in (126.0, 130.0, 150.0, 170.0, 179.5, 179.84):    # rests from <= +179.99
            clock.t = OPEN + dt
            bot.step(clock.t)
        self.assertFalse([c for c in ex.kinds("create") if c[3] == 1])
        self.assertEqual(bot.status(clock.t)["events"][EVENT]["pad_hold_until"],
                         C.iso(OPEN + 180.0))
        last = bot.last_decide
        n = len(ex.calls)
        clock.t = OPEN + 179.86                          # 0.02 s later: rests from +180.01
        bot.step(clock.t)
        self.assertEqual(bot.last_decide, clock.t)       # decided on the edge, not the 250 ms tick
        self.assertLess(clock.t - last, bot.cfg.loop_s)
        new = self.writes(ex.calls[n:])
        self.assertEqual(sorted((c[0], c[1], c[2], c[3], c[4]) for c in new),
                         [("create", T_HI, "no", 1, 1000), ("create", T_LO, "yes", 1, 1000)])
        self.assertTrue(all(c[6] == int(clock.t + 300) for c in new))   # the pads' TTL
        self.assertEqual(by_market(bot)[T_HI], {"yes": {95: 300.0}, "no": {1: 1000.0}})
        self.assertEqual(by_market(bot)[T_LO], {"yes": {1: 1000.0}, "no": {95: 300.0}})
        dec = [r for r in rows(os.path.join(bot.dir, f"decisions_{L.utc_day(clock.t)}.jsonl"))
               if r.get("kind") == "pad_hold_end"]
        self.assertEqual([(r["event"], r["after_open_s"]) for r in dec], [(EVENT, 180.01)])
        # no churn after the edge: the pads just rest; the only writes are the
        # favorites' expiry renewals (placed at +125 with a 90 s TTL)
        pad_ids = {o.oid for o in bot.orders.values() if o.price == 1}
        n = len(ex.calls)
        for dt in (180.2, 180.5, 181.0, 183.0, 190.0, 200.0):
            clock.t = OPEN + dt
            bot.step(clock.t)
        later = self.writes(ex.calls[n:])
        self.assertTrue(all(c[0] == "amend" and c[1] not in pad_ids and c[2] == 95
                            and c[4] is not None for c in later), later)
        self.assertEqual({o.oid for o in bot.orders.values() if o.price == 1}, pad_ids)

    def test_g0_nohold_places_pads_with_the_favorites(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, variant="G0_nohold")
        started(bot, clock, t=OPEN + 125.0)
        self.assertEqual(sorted((c[1], c[2], c[3]) for c in ex.kinds("create")),
                         [(T_HI, "no", 1), (T_HI, "yes", 95), (T_LO, "no", 95),
                          (T_LO, "yes", 1)])

    def test_the_hold_end_does_not_delay_a_favorites_move(self):
        # thin favorite side: after the hold a 1c qualification pad joins the
        # favorite's side; it must not restart the favorite's 2 s debounce
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, variant="G_decided_only")
        feed.books[T_HI] = ({95: 300.0}, dict(BOOKS[T_HI][1]))
        started(bot, clock, t=OPEN + 125.0)
        self.assertEqual(by_market(bot)[T_HI], {"yes": {95: 300.0}})
        feed.books[T_HI] = ({96: 300.0}, dict(BOOKS[T_HI][1]))   # the reference rises
        for dt in (178.5, 179.0, 179.84):
            clock.t = OPEN + dt
            bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_HI], {"yes": {95: 300.0}})    # debounced
        clock.t = OPEN + 179.86
        bot.step(clock.t)                                # the hold ends: pads, the move still waits
        self.assertEqual(by_market(bot)[T_HI], {"yes": {95: 300.0, 1: 450.0},
                                                "no": {1: 1000.0}})
        clock.t = OPEN + 180.9                           # 2.4 s after the rise, 1.04 s after the pads
        bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_HI], {"yes": {96: 300.0, 1: 450.0},
                                                "no": {1: 1000.0}})

    def test_hold_counts_from_the_events_open_time(self):
        # an event whose markets opened a minute early: the hold ends at +120
        ex = FakeExchange()
        ex.markets_rows = [dict(m, open_time=C.iso(OPEN - 60.0)) for m in market_rows()]
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, variant="G_decided_only",
                                                         ex=ex)
        started(bot, clock, t=OPEN + 125.0)
        self.assertEqual(bot.events[EVENT].open_ts, OPEN - 60.0)
        self.assertEqual(sorted((c[1], c[2], c[3]) for c in ex.kinds("create")),
                         [(T_HI, "no", 1), (T_HI, "yes", 95), (T_LO, "no", 95),
                          (T_LO, "yes", 1)])

    def test_stale_wx_rests_nothing_in_the_hold(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, variant="G_decided_only")
        fair.stale = True                                # stale: 1c pads only ...
        started(bot, clock, t=OPEN + 125.0)
        bot.step(OPEN + 150.0)
        self.assertEqual(bot.orders, {})                 # ... and those are held
        for dt in (179.86, 181.0):
            clock.t = OPEN + dt
            bot.step(clock.t)
        self.assertTrue(bot.orders)
        self.assertTrue(all(o.price == 1 for o in bot.orders.values()))


class TestQuoteHours(unittest.TestCase):
    """Jack 2026-10-16, "1": FAV_night = favorite joins only, in the events
    that settle 20:00-07:00 ET (policy quote_hours_et). The executor passes
    the event's settle time (the ticker's hour) to decide(), which rests
    nothing outside the hours -- the shadow sim's rule. EVENT settles at
    09:00 ET (EDT)."""

    @staticmethod
    def writes(calls):
        return [c for c in calls if c[0] in ("create", "amend", "decrease", "cancel",
                                             "batch_cancel")]

    def test_variant_names_and_env(self):
        self.assertEqual(L.resolve_variant("FAV_night"), "FAV_night")
        self.assertEqual(L.resolve_variant("FAV_only"), "FAV_only")
        self.assertEqual(L.resolve_variant("F"), "F_full")          # FAV is not F
        with self.assertRaises(ValueError):
            L.resolve_variant("FAV")                                 # two FAV_* variants
        env = {"TMM_QUOTE_HOURS_ET": "20, 21,22 23,0,1,2,3,4,5,6,7"}
        self.assertEqual(L.policy_overrides_from_env(env),
                         {"quote_hours_et": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7]})
        self.assertEqual(L.policy_overrides_from_env({"TMM_QUOTE_HOURS_ET": "[9]"}),
                         {"quote_hours_et": [9]})
        for raw in ("all", "ALL", " any "):
            self.assertEqual(L.policy_overrides_from_env({"TMM_QUOTE_HOURS_ET": raw}),
                             {"quote_hours_et": None})
        # fix pass 10/16: "[]" / "none" / "null" used to mean every hour (the
        # policy's [] quotes NO hour): ambiguous, so the bot refuses to start
        for raw in ("24", "7.5", "x", "-1", "[]", "[ ]", "none", "None", "null", "NULL", ",",
                    "[,]"):
            with self.assertRaises(ValueError, msg=raw):
                L.policy_overrides_from_env({"TMM_QUOTE_HOURS_ET": raw})
        # unset / empty: no override, the variant's own hours stay
        for env_ in ({}, {"TMM_QUOTE_HOURS_ET": ""}, {"TMM_QUOTE_HOURS_ET": "  "}):
            self.assertEqual(L.policy_overrides_from_env(env_), {})
            self.assertEqual(pol.make_policy("FAV_night", **L.policy_overrides_from_env(env_))
                             ["quote_hours_et"], [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7])
        P = pol.make_policy("FAV_night", **L.policy_overrides_from_env(
            {"TMM_QUOTE_HOURS_ET": "9"}))
        pol._check_policy(P)
        self.assertEqual(P["quote_hours_et"], [9])

    def test_outside_the_hours_nothing_rests(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=True, variant="FAV_night")
        self.assertEqual(L.event_settle_ts(EVENT), S)
        self.assertEqual(pol.et_hour(S), 9)
        started(bot, clock, t=OPEN + 125.0)
        for dt in (126.0, 130.0, 200.0, 600.0, 1800.0):
            clock.t = OPEN + dt
            bot.step(clock.t)
        self.assertEqual(self.writes(ex.calls), [])
        self.assertEqual(bot.orders, {})
        st = bot.status(clock.t)
        self.assertEqual(st["quote_hours_et"], [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7])
        self.assertFalse(st["events"][EVENT]["quote_hour"])
        self.assertEqual(st["events"][EVENT]["guards"].get("off_hours"), 9)
        samples = [r for r in rows(os.path.join(bot.dir, f"decisions_{L.utc_day(clock.t)}.jsonl"))
                   if r.get("kind") == "sample"]
        self.assertTrue(samples)
        self.assertTrue(all(r["reason"] == "off_hours" and r["quote_hour"] is False
                            for r in samples))
        with open(os.path.join(bot.dir, f"tmm_{L.utc_day(clock.t)}.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertEqual(log.count("outside quote_hours_et"), 1)     # logged once per event

    def test_inside_the_hours_favorites_only(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, variant="FAV_night", pol_over={"quote_hours_et": [8, 9]})
        started(bot, clock, t=OPEN + 125.0)
        for dt in (126.0, 150.0, 180.0, 200.0):
            clock.t = OPEN + dt
            bot.step(clock.t)
        # the favorite joins of G at the reference, no 1c anything
        self.assertEqual(sorted((c[1], c[2], c[3], c[4]) for c in ex.kinds("create")),
                         [(T_HI, "yes", 95, 300), (T_LO, "no", 95, 300)])
        self.assertTrue(bot.status(clock.t)["events"][EVENT]["quote_hour"])
        self.assertFalse([o for o in bot.orders.values() if o.price == 1])

    def test_hours_change_cancels_what_rests(self):
        # a restart with a narrower TMM_QUOTE_HOURS_ET: what rests on an
        # off-hours event is cancelled on the first decision (dry book)
        bot, ex, feed, wx, fair, mails, clock = make_bot(live=False, variant="FAV_only")
        started(bot, clock, t=OPEN + 125.0)
        self.assertEqual(len(bot.orders), 2)
        bot.P = dict(bot.P, quote_hours_et=[21])
        bot.P_stale = L.stale_policy(bot.P)
        clock.t = OPEN + 126.5
        bot.step(clock.t)
        self.assertEqual(bot.orders, {})

    def test_dst_settle_hours_from_tickers(self):
        # build_events takes the settle time from the ticker (fold 0 on the
        # repeated 01:00); the policy's ET hour is DST-safe
        P = pol.make_policy("FAV_night")
        for ev, h, ok in (("KXTEMPMIAH-26NOV0101", 1, True), ("KXTEMPMIAH-26NOV0107", 7, True),
                          ("KXTEMPMIAH-26NOV0108", 8, False), ("KXTEMPMIAH-26OCT3107", 7, True),
                          ("KXTEMPMIAH-26OCT3108", 8, False)):
            s = C.parse_event(ev)[1]
            rows_ = [{"ticker": f"{ev}-T80.99", "event_ticker": ev, "floor_strike": 80.99,
                      "status": "active", "open_time": C.iso(s - 3600), "close_time": C.iso(s)}]
            info = L.build_events(rows_)[ev]
            self.assertEqual(info.settle_ts, s)
            self.assertEqual(pol.et_hour(info.settle_ts), h, ev)
            self.assertEqual(pol.quote_hour_ok(P, info.settle_ts), ok, ev)


class TestMarketAgreement(unittest.TestCase):
    """Jack 2026-10-17, "1": pre-register the agreement rule. FAV_night_agree
    = FAV_night + fav_market_min_p 0.95: a favorite join only while the
    market's favorite probability on the EXTERNAL book (the live book minus
    our resting orders) is >= 0.95 -- the same temp_mm_policy.decide() rule
    the shadow sim replays. EVENT settles 09:00 ET, so the tests list hour 9."""

    @staticmethod
    def writes(calls):
        return [c for c in calls if c[0] in ("create", "amend", "decrease", "cancel",
                                             "batch_cancel")]

    def test_variant_and_env(self):
        self.assertEqual(L.resolve_variant("FAV_night_agree"), "FAV_night_agree")
        with self.assertRaises(ValueError):
            L.resolve_variant("FAV")                                   # three FAV_* variants
        self.assertEqual(L.policy_overrides_from_env({"TMM_FAV_MARKET_MIN_P": "0.95"}),
                         {"fav_market_min_p": 0.95})
        for raw in ("off", "OFF", "none", " null "):
            self.assertEqual(L.policy_overrides_from_env({"TMM_FAV_MARKET_MIN_P": raw}),
                             {"fav_market_min_p": None})
        for env in ({}, {"TMM_FAV_MARKET_MIN_P": ""}, {"TMM_FAV_MARKET_MIN_P": "  "}):
            self.assertEqual(L.policy_overrides_from_env(env), {})
            self.assertEqual(pol.make_policy("FAV_night_agree",
                                             **L.policy_overrides_from_env(env))
                             ["fav_market_min_p"], 0.95)
        with self.assertRaises(ValueError):
            L.policy_overrides_from_env({"TMM_FAV_MARKET_MIN_P": "high"})
        P = pol.make_policy("FAV_night", **L.policy_overrides_from_env(
            {"TMM_FAV_MARKET_MIN_P": "1.5"}))
        with self.assertRaises(ValueError):                            # main(): config error
            pol._check_policy(P)
        self.assertIsNone(pol.make_policy("FAV_night")["fav_market_min_p"])

    def test_joins_only_while_the_external_book_agrees(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, variant="FAV_night_agree", pol_over={"quote_hours_et": [9]})
        self.assertEqual(bot.P["fav_market_min_p"], 0.95)
        started(bot, clock, t=OPEN + 125.0)
        # T_HI YES 95 / NO 3 and T_LO NO 95 / YES 3: the market at 0.96 -> joins
        self.assertEqual(sorted((c[1], c[2], c[3], c[4]) for c in ex.kinds("create")),
                         [(T_HI, "yes", 95, 300), (T_LO, "no", 95, 300)])
        samples = [r for r in rows(os.path.join(bot.dir, f"decisions_{L.utc_day(clock.t)}.jsonl"))
                   if r.get("kind") == "sample" and r.get("ticker") == T_HI]
        self.assertEqual(samples[0]["fav_market"],
                         {"side": "yes", "p": 0.96, "min": 0.95, "ok": True})
        # the market drops to YES 93 / NO 5 (0.94). The LIVE book still holds
        # our 95 x300 on top (FakeFeed adds it): counted, it would read 0.95
        # and keep the join; the external book reads 0.94 -> cancelled at once
        feed.books[T_HI] = ({93: 300.0}, {5: 200.0})
        n = len(ex.calls)
        clock.t = OPEN + 126.5
        bot.step(clock.t)
        new = self.writes(ex.calls[n:])
        self.assertTrue(any(c[0] in ("cancel", "batch_cancel") for c in new), new)
        self.assertNotIn(T_HI, by_market(bot))
        self.assertEqual(by_market(bot)[T_LO], {"no": {95: 300.0}})
        # the market agrees again (YES 96 / NO 3: 0.965): a new join
        feed.books[T_HI] = ({96: 300.0}, {3: 200.0})
        for dt in (127.0, 128.0, 129.0):
            clock.t = OPEN + dt
            bot.step(clock.t)
        self.assertEqual(by_market(bot)[T_HI], {"yes": {96: 300.0}})

    def test_fav_night_ignores_the_market(self):
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=True, variant="FAV_night", pol_over={"quote_hours_et": [9]})
        feed.books[T_HI] = ({93: 300.0}, {5: 200.0})                  # market 0.94
        started(bot, clock, t=OPEN + 125.0)
        self.assertIn((T_HI, "yes", 93, 300),
                      [(c[1], c[2], c[3], c[4]) for c in ex.kinds("create")])

    def test_dry_book_is_the_market(self):
        # dry: the dry orders never reached the exchange, so the feed's book
        # is the external book as it stands
        bot, ex, feed, wx, fair, mails, clock = make_bot(
            live=False, variant="FAV_night_agree", pol_over={"quote_hours_et": [9]})
        feed.books[T_HI] = ({94: 300.0}, {4: 200.0})                  # (94 + 96) / 200 = 0.95
        feed.books[T_LO] = ({8: 200.0}, {90: 300.0})                  # (90 + 92) / 200 = 0.91
        started(bot, clock, t=OPEN + 125.0)
        self.assertEqual(by_market(bot), {T_HI: {"yes": {94: 300.0}}})

if __name__ == "__main__":
    unittest.main()
