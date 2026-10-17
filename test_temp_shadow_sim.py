#!/usr/bin/env python3
"""Tests for temp_shadow_sim (hourly-temp shadow test, TEMP_SHADOW.md).

SANDBOXED: TEMP_SHADOW_DATA points at a fresh temp dir BEFORE any
temp_shadow module is imported (temp_shadow_common reads it at import), so
nothing here can touch the live recording. A synthetic fixture generator
writes ws / wx / meta rows plus settled/<EVENT>.json for a fake event in the
exact live shapes (temp_shadow_collector docstring), and the REAL scoring /
model modules replay them. A deterministic stand-in FairModel is passed via
run_event(model=...) so fairs are known numbers.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_shadow_sim -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tss_sim_")

import json        # noqa: E402
import math        # noqa: E402
import shutil      # noqa: E402
import time        # noqa: E402
import unittest    # noqa: E402

import temp_shadow_common as C     # noqa: E402
import temp_shadow_scoring as scoring  # noqa: E402
import temp_shadow_sim as sim      # noqa: E402

assert C.DATA_ROOT == os.environ["TEMP_SHADOW_DATA"]
assert os.path.realpath(C.DATA_ROOT).startswith(os.path.realpath(tempfile.gettempdir()))

EVENT = "KXTEMPMIAH-26OCT1409"
S = C.parse_event(EVENT)[1]
OPEN = S - 3600.0
STATIONS = C.STATIONS["miami"]
K = 80.99
TICKER = f"{EVENT}-T{K:.2f}"
LIVE_C = [27, 27, 27, 27, 28]      # S=136 -> 80.96 F (T80.99 needs S >= 137)
DROP_C = [23, 23, 23, 23, 23]      # S=115 -> 73.40 F (decided NO)
BOOK_Y = {40: 500.0, 39: 1000.0}
BOOK_N = {45: 500.0, 44: 1000.0}


def _ncdf(z):
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


class FakeModel:
    """P(YES) = 1 - Phi((thr - index)/sigma); thr = lattice threshold - 0.18."""

    def __init__(self, sigma=3.0):
        self.sigma = sigma

    def dist(self, snap, settle_ts):
        if snap is None or snap.index_latest is None:
            return None
        return (float(snap.index_latest), self.sigma)

    def fair(self, snap, settle_ts, strikes):
        d = self.dist(snap, settle_ts)
        if d is None:
            return {}
        mu, s = d
        return {k: 1.0 - _ncdf((C.index_from_sum(C.sum_threshold(k)) - 0.18 - mu) / s)
                for k in strikes}


def _wipe():
    root = os.path.realpath(C.DATA_ROOT)
    assert root.startswith(os.path.realpath(tempfile.gettempdir()))
    for sub in ("raw", "settled", "sim"):
        shutil.rmtree(os.path.join(root, sub), ignore_errors=True)


def _iso_us(ts):
    whole = math.floor(ts)
    us = int(round((ts - whole) * 1e6))
    return C.iso(whole)[:-1] + f".{us:06d}Z"


def _dollars(c):
    return f"{c / 100.0:.4f}"


def write_fixture(event=EVENT, strikes=(K,), books=None, deltas=(), trades=(),
                  temps=None, results=None, expiration_value=None,
                  prog_window=(120.0, 3600.0), settled=True, extra_wx=(), wx_from=-900.0):
    """Write a fake event. Times are seconds after open.

    books   {ticker: (yes_levels, no_levels)} snapshot at open-650 s
    deltas  [(t, ticker, side, cents, delta)]
    trades  [(t, ticker, taker_side, yes_cents, count)]
    temps   f(minute_offset_s) -> five whole-C readings; one pending row at
            minute+145 s and one final row at minute+310 s per minute
    extra_wx [(rx_offset, minute_offset_s, five C)] extra (revised) rows
    wx_from first station minute, seconds after open (default 15 min before)
    """
    pe = C.parse_event(event)
    s_ts = pe[1]
    op = s_ts - 3600.0
    tickers = [f"{event}-T{k:.2f}" for k in strikes]
    books = books or {t: (BOOK_Y, BOOK_N) for t in tickers}
    temps = temps or (lambda m: LIVE_C)
    meta, ws, wx = [], [], []
    mk = [dict(ticker=t, event_ticker=event, floor_strike=k, strike_type="greater",
               open_time=C.iso(op), close_time=C.iso(s_ts), status="active",
               result="") for t, k in zip(tickers, strikes)]
    meta.append({"rx": op - 600.0, "kind": "markets",
                 "data": {"event_ticker": event, "markets": mk}})
    progs = None
    if prog_window is not None:
        progs = [dict(id=f"p{i}", market_ticker=t, start_date=_iso_us(op + prog_window[0]),
                      end_date=_iso_us(op + prog_window[1]), period_reward=1000000,
                      target_size_fp="1000.00", discount_factor_bps=5000,
                      incentive_type="liquidity")
                 for i, t in enumerate(tickers)]
        meta.append({"rx": op + 60.0, "kind": "programs", "data": {"programs": progs}})
    # ws: connect, subscribed, snapshots, deltas, trades
    ws.append((op - 700.0, {"rx": op - 700.0, "marker": "connect", "detail": "test"}))
    ws.append((op - 699.0, {"rx": op - 699.0, "m": {"type": "subscribed", "id": 1,
                                                       "msg": {"channel": "orderbook_delta", "sid": 1}}}))
    ws.append((op - 699.0, {"rx": op - 699.0, "m": {"type": "subscribed", "id": 1,
                                                       "msg": {"channel": "trade", "sid": 2}}}))
    book_rows = []
    for t in tickers:
        y, n = books[t]
        msg = {"market_ticker": t,
               "yes_dollars_fp": [[_dollars(p), f"{q:.2f}"] for p, q in sorted(y.items())],
               "no_dollars_fp": [[_dollars(p), f"{q:.2f}"] for p, q in sorted(n.items())]}
        book_rows.append((op - 650.0, "orderbook_snapshot", msg))
    for (dt, t, side, c, d) in deltas:
        book_rows.append((op + dt, "orderbook_delta",
                          {"market_ticker": t, "price_dollars": _dollars(c),
                           "delta_fp": f"{d:.2f}", "side": side}))
    book_rows.sort(key=lambda r: r[0])
    for seq, (rx, typ, msg) in enumerate(book_rows, start=1):
        ws.append((rx, {"rx": rx, "m": {"type": typ, "sid": 1, "seq": seq, "msg": msg}}))
    rest = {t: [] for t in tickers}
    for seq, (dt, t, side, yc, cnt) in enumerate(sorted(trades), start=1):
        ts = op + dt
        tid = f"tr-{seq:05d}"
        body = {"trade_id": tid, "market_ticker": t, "yes_price_dollars": _dollars(yc),
                "no_price_dollars": _dollars(100 - yc), "count_fp": f"{cnt:.2f}",
                "taker_side": side, "taker_outcome_side": side, "is_block_trade": False,
                "ts": int(math.floor(ts)), "ts_ms": int(round(ts * 1000))}
        ws.append((ts + 0.05, {"rx": ts + 0.05, "m": {"type": "trade", "sid": 2, "seq": seq,
                                                       "msg": body}}))
        rest[t].append({k: v for k, v in body.items() if k not in ("ts", "ts_ms")}
                       | {"ticker": t, "created_time": _iso_us(ts)})

    def wx_row(rx, minute, cs, final):
        idx = C.index_from_sum(sum(cs))
        return {"rx": rx, "city": "miami", "t": int(minute * 1000),
                "status": "normal" if final else "incomplete",
                "v": idx if final else None, "contributors": 5 if final else None,
                "stations": [{"station_id": sid, "temp_f": round(c * 1.8 + 32.0, 1),
                              "code": "ok" if final else "pending", "source": "synoptic",
                              "received_at_ms": int((minute + 144) * 1000)}
                             for sid, c in zip(STATIONS, cs)]}
    m = op + wx_from
    while m < s_ts:
        cs = temps(m - op)
        for lag, final in ((145.0, False), (310.0, True)):
            if m + lag < s_ts:
                wx.append(wx_row(m + lag, m, cs, final))
        m += 60.0
    for (rxo, mo, cs) in extra_wx:
        wx.append(wx_row(op + rxo, op + mo, cs, False))
    wx.sort(key=lambda r: r["rx"])
    for rec in sorted(meta, key=lambda r: r["rx"]):
        C.append_jsonl(C.raw_file("meta", rec["rx"]), rec)
    for rx, rec in sorted(ws, key=lambda r: r[0]):
        C.append_jsonl(C.raw_file("ws", rx), rec)
    for rec in wx:
        C.append_jsonl(C.raw_file("wx", rec["rx"]), rec)
    if settled:
        res = results or {t: "no" for t in tickers}
        smk = [dict(m_, result=res[m_["ticker"]], status="finalized",
                    expiration_value=str(expiration_value if expiration_value is not None
                                         else 80.96)) for m_ in mk]
        C.write_json_atomic(C.settled_file(event), {
            "event_ticker": event, "fetched_rx": s_ts + 600, "markets": smk,
            "trades": rest, "index": [], "programs": progs or []})
    return tickers


V_NOWIN = {"T": dict(window_mode="none")}


def run_keys(names=None):
    """The variant x fill-model keys run_event / run_day write: opt / cons /
    swp for every variant, stress for the STRESS_SCOPE ones."""
    names = list(sim.VARIANTS) if names is None else list(names)
    return ({f"{v}|{f}" for v in names for f in sim.FILL_MODELS}
            | {f"{v}|stress" for v in names if sim.in_stress_scope(sim.VARIANTS[v])})


class SimTestBase(unittest.TestCase):
    def setUp(self):
        _wipe()


class TestReward(SimTestBase):
    def test_known_book_known_share(self):
        write_fixture(prog_window=(120.0, 1200.0))
        res = sim.run_event(EVENT, variants=V_NOWIN, model=FakeModel())
        v = res["variants"]["T|opt"]
        exp_frac = scoring.score(BOOK_Y, BOOK_N, {40: 300.0}, {45: 300.0})[0]
        self.assertAlmostEqual(exp_frac, 300.0 / 1300.0, places=9)
        self.assertEqual(v["quoted_secs"], 1080)
        self.assertAlmostEqual(v["avg_share"], exp_frac, places=5)
        self.assertAlmostEqual(v["reward"], 100.0 * exp_frac, places=3)   # $23.08
        self.assertAlmostEqual(v["by_tau"]["40-60"]["reward"], v["reward"], places=4)
        self.assertAlmostEqual(v["by_class"]["live"]["reward"], v["reward"], places=4)
        self.assertEqual(v["fills_n"], 0)
        self.assertEqual(res["coverage"]["program_secs"], 1080)
        self.assertEqual(res["coverage"]["programs_src"], "meta")
        self.assertAlmostEqual(res["coverage"]["book_trusted_frac"], 1.0)
        # same numbers for cons (no trades)
        self.assertAlmostEqual(res["variants"]["T|cons"]["reward"], v["reward"], places=6)

    def test_pull_window_quotes_only_outside_landing_window(self):
        write_fixture(prog_window=(120.0, 1200.0))
        res = sim.run_event(EVENT, variants=["B_live300_pull"], model=FakeModel())
        v = res["variants"]["B_live300_pull|opt"]
        # out over [19.65, 46.65) of each minute -> scored at :47.5..:19.5 = 33 of 60
        self.assertEqual(v["quoted_secs"], 18 * 33)
        self.assertAlmostEqual(v["reward"], 18 * 33 * (100.0 / 1080) * 300 / 1300, places=3)

    def test_untrusted_book_no_reward(self):
        write_fixture(prog_window=(120.0, 1200.0))
        # a disconnect at +600 s with no resnapshot -> the rest is untrusted
        C.append_jsonl(C.raw_file("ws", OPEN + 600.0),
                       {"rx": OPEN + 600.0, "marker": "disconnect", "detail": "t"})
        # rows must stay rx-sorted per file: rewrite the hour file sorted
        for p in C.raw_files("ws", OPEN - 3600, S + 3600):
            rows = sorted(C.iter_jsonl(p), key=lambda r: r["rx"])
            with open(p, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        res = sim.run_event(EVENT, variants=V_NOWIN, model=FakeModel())
        v = res["variants"]["T|opt"]
        self.assertEqual(v["quoted_secs"], 600 - 120)
        self.assertLess(res["coverage"]["book_trusted_frac"], 0.2)


class TestFills(SimTestBase):
    def _run(self, result):
        trades = [(300.25, TICKER, "no", 40, 200.0),   # equal price on our YES 40
                  (400.25, TICKER, "no", 39, 100.0),   # trade-through
                  (500.25, TICKER, "yes", 55, 600.0),  # equal price on our NO 45
                  (600.25, TICKER, "yes", 56, 50.0)]   # trade-through
        write_fixture(trades=trades, results={TICKER: result})
        return sim.run_event(EVENT, variants=V_NOWIN, model=FakeModel())

    def test_trade_through_vs_queue(self):
        res = self._run("yes")
        o, c = res["variants"]["T|opt"], res["variants"]["T|cons"]
        self.assertAlmostEqual(o["by_side"]["yes_bid"]["contracts"], 300.0)  # 200 + 100
        self.assertAlmostEqual(c["by_side"]["yes_bid"]["contracts"], 100.0)  # queue ate 200
        self.assertAlmostEqual(o["by_side"]["no_bid"]["contracts"], 350.0)   # 300 + 50
        self.assertAlmostEqual(c["by_side"]["no_bid"]["contracts"], 150.0)   # 600-500 + 50
        self.assertEqual(o["fills_n"], 4)
        self.assertEqual(c["fills_n"], 3)
        # settlement signs: YES pays $1 on yes; NO bought at 45 loses
        self.assertAlmostEqual(o["by_side"]["yes_bid"]["pnl"], 300 * 0.60, places=6)
        self.assertAlmostEqual(o["by_side"]["no_bid"]["pnl"], -350 * 0.45, places=6)
        self.assertAlmostEqual(o["trading_pnl"], 180.0 - 157.5, places=6)
        self.assertAlmostEqual(c["trading_pnl"], 60.0 - 67.5, places=6)
        self.assertAlmostEqual(o["net"], o["reward"] + o["trading_pnl"], places=5)
        self.assertAlmostEqual(o["by_tau"]["40-60"]["contracts"], 650.0)
        # static book: mid 47.5 -> YES@40 marks +7.5, NO@45 marks 52.5-45 = +7.5
        self.assertAlmostEqual(o["markouts"]["m60"], 7.5, places=3)

    def test_settlement_signs_result_no(self):
        res = self._run("no")
        o = res["variants"]["T|opt"]
        self.assertAlmostEqual(o["by_side"]["yes_bid"]["pnl"], -300 * 0.40, places=6)
        self.assertAlmostEqual(o["by_side"]["no_bid"]["pnl"], 350 * 0.55, places=6)
        self.assertEqual(res["results"], {TICKER: "no"})
        self.assertTrue(res["settled"])

    def test_pos_cap(self):
        trades = [(300.25, TICKER, "no", 39, 100.0), (400.25, TICKER, "no", 39, 100.0),
                  (500.25, TICKER, "no", 39, 100.0)]
        write_fixture(trades=trades)
        res = sim.run_event(EVENT, variants={"T": dict(window_mode="none", pos_cap=150)},
                            model=FakeModel())
        for fm in ("opt", "cons"):
            v = res["variants"][f"T|{fm}"]
            self.assertAlmostEqual(v["contracts"], 150.0)
            self.assertAlmostEqual(v["max_abs_pos"], 150.0)

    def test_event_worst_case_cap(self):
        trades = [(300.25, TICKER, "no", 39, 100.0), (400.25, TICKER, "no", 39, 100.0),
                  (500.25, TICKER, "yes", 56, 100.0)]
        write_fixture(trades=trades)
        res = sim.run_event(EVENT, variants={"T": dict(window_mode="none", event_wcl_cap=10.0)},
                            model=FakeModel())
        for fm in ("opt", "cons"):
            v = res["variants"][f"T|{fm}"]
            # pre-trade: YES @40 rests only $10 / 0.40 = 25; once filled the
            # index-below scenario is at the cap, YES stops. NO (which hedges)
            # gets $10 + the $15 the YES fill wins above: 25 / 0.45 -> 55.
            self.assertAlmostEqual(v["by_side"]["yes_bid"]["contracts"], 25.0)
            self.assertAlmostEqual(v["by_side"]["no_bid"]["contracts"], 55.0)
            self.assertAlmostEqual(v["worst_case_loss"], 10.0, places=6)

    def test_event_wcl_caps_resting_size_before_a_burst(self):
        # review 10/14: the cap only stopped sides AFTER a breach, so one sweep
        # overshot it by the whole resting order (300 @40 = $120 vs $100)
        write_fixture(trades=[(300.25, TICKER, "no", 30, 1000.0)])
        res = sim.run_event(EVENT, variants={"T": dict(window_mode="none", event_wcl_cap=100.0)},
                            model=FakeModel())
        for fm in ("opt", "cons"):
            v = res["variants"][f"T|{fm}"]
            self.assertAlmostEqual(v["by_side"]["yes_bid"]["contracts"], 250.0)
            self.assertAlmostEqual(v["worst_case_loss"], 100.0, places=6)


class TestLandingWindow(SimTestBase):
    def test_pull_avoids_stale_fill_after_wx(self):
        # minute +23:00 lands at +25:25 (som 25) with a crash to 73.40 F; a taker
        # sells YES 0.25 s later, before anyone could react (react 0.5 + 0.15)
        drop_min = 1380.0
        write_fixture(temps=lambda m: DROP_C if m >= drop_min else LIVE_C,
                      trades=[(1525.25, TICKER, "no", 39, 100.0)],
                      results={TICKER: "no"}, expiration_value=73.4)
        res = sim.run_event(EVENT, variants=["A_live300_nowin", "B_live300_pull"],
                            model=FakeModel())
        for fm in ("opt", "cons"):
            a = res["variants"][f"A_live300_nowin|{fm}"]
            b = res["variants"][f"B_live300_pull|{fm}"]
            self.assertAlmostEqual(a["contracts"], 100.0)
            self.assertAlmostEqual(a["trading_pnl"], -40.0, places=6)
            self.assertAlmostEqual(a["fills_after_wx"]["contracts"], 100.0)
            self.assertAlmostEqual(a["fills_after_wx"]["pnl"], -40.0, places=6)
            self.assertEqual(b["fills_n"], 0)
            self.assertEqual(b["trading_pnl"], 0.0)
            self.assertGreater(a["reward"], b["reward"])
        # after the crash the strike is decided: A stops quoting it
        self.assertGreater(res["model_eval"]["n"], 0)


NEAR_C = [25, 25, 24, 24, 25]      # S=123 -> 76.28 F: FakeModel P(YES T80.99) ~ 0.053


class TestReviewFixes(SimTestBase):
    """Regression tests for the 10/14 review findings."""

    def test_size_cut_with_fill_in_flight_keeps_belief_true(self):
        # YES 300 @40 rests. Minute +10:00 lands at +12:25 (all five pending)
        # and makes the strike near-decided: E-style rules cut YES to 150 at
        # the same price (decision 745.5, active 745.65). A taker sells 200
        # through 40 at 745.55, inside the latency. Kalshi reduce_by 150 on
        # the 100 left cancels it; the bot then rests 150 -> a later sweep
        # fills 150 (350 total). The old absolute "trim to 150" left 100
        # untracked under a fresh 150 (450 total).
        trades = [(745.55, TICKER, "no", 39, 200.0), (800.25, TICKER, "no", 30, 1000.0)]
        write_fixture(temps=lambda m: NEAR_C if m >= 600.0 else LIVE_C, trades=trades)
        P = dict(window_mode="none", decided=True, fair_guard=False)
        res = sim.run_event(EVENT, variants={"T": P, "T300": dict(P, pos_cap=300)},
                            model=FakeModel(), until_ts=OPEN + 801.0)
        for fm in ("opt", "cons"):
            v = res["variants"][f"T|{fm}"]
            self.assertAlmostEqual(v["by_side"]["yes_bid"]["contracts"], 350.0)
            self.assertEqual(v["fills_n"], 2)
            # with a 300 cap the second order is sized to the 100 left
            c = res["variants"][f"T300|{fm}"]
            self.assertAlmostEqual(c["by_side"]["yes_bid"]["contracts"], 300.0)
            self.assertLessEqual(c["max_abs_pos"], 300.0)

    def test_pass_belief_matches_actual_plus_pending(self):
        # white-box: after every event of a pass, the belief equals the actual
        # resting orders with the in-flight ops applied
        trades = [(745.55, TICKER, "no", 39, 200.0), (745.6, TICKER, "yes", 56, 120.0),
                  (800.25, TICKER, "no", 30, 1000.0), (1525.55, TICKER, "no", 39, 77.0)]
        write_fixture(temps=lambda m: NEAR_C if 600.0 <= m < 1380.0 else LIVE_C,
                      trades=trades)
        inp = sim.load_event_inputs(EVENT)
        sh = sim._Shared(inp, FakeModel(), None)
        P = sim._resolve_variants({"T": dict(window_mode="none", decided=True,
                                             fair_guard=False)})["T"]
        for fm in ("opt", "cons"):
            ps = sim._Pass(sh, P, fm)
            n_chk = [0, 0]

            def check(_ps=ps):
                for m in range(sh.nm):
                    self.assertEqual(_ps.belief[m], _ps._projected(m))
                    if not any(it[1] == m for it in _ps.pending):
                        act = {sd: {p: sum(l[0] for l in lots)
                                    for p, lots in _ps.orders[m][sd].items()}
                               for sd in ("yes", "no")}
                        self.assertEqual(_ps.belief[m], act)
                        n_chk[1] += 1
                n_chk[0] += 1

            def wrap(fn):
                def inner(*a, **k):
                    r = fn(*a, **k)
                    check()
                    return r
                return inner
            ps._activate = wrap(ps._activate)
            ps._decide_all = wrap(ps._decide_all)
            ps._on_trade = wrap(ps._on_trade)
            out = ps.run()
            self.assertGreaterEqual(out["fills_n"], 4, fm)
            self.assertGreater(n_chk[1], 1000)

    def test_near_decided_needs_decided(self):
        # fair ~0.053 all hour: near-decided. Spec: near strikes are part of
        # "Decided strikes (if enabled)" -> B (decided off) does not quote;
        # E and G (decided on) quote them as live at half size, class decided.
        write_fixture(temps=lambda m: NEAR_C, prog_window=(120.0, 1200.0),
                      trades=[(300.25, TICKER, "no", 39, 1000.0)])
        res = sim.run_event(EVENT, variants=["B_live300_pull", "E_live300_pull_decided",
                                             "G_decided_only", "H_noguard_pull"],
                            model=FakeModel())
        for name in ("B_live300_pull", "H_noguard_pull"):
            v = res["variants"][f"{name}|opt"]
            self.assertEqual(v["quoted_secs"], 0, name)
            self.assertEqual(v["fills_n"], 0, name)
        for name in ("E_live300_pull_decided", "G_decided_only"):
            v = res["variants"][f"{name}|opt"]
            self.assertGreater(v["quoted_secs"], 0, name)
            self.assertAlmostEqual(v["by_class"]["decided"]["reward"], v["reward"], places=6)
            self.assertEqual(v["by_class"]["live"]["reward"], 0.0)
        # E and G quote the near strike identically (half of q_live = 150)
        e, g = (res["variants"][f"{n}|opt"] for n in ("E_live300_pull_decided", "G_decided_only"))
        self.assertEqual(e["quoted_secs"], g["quoted_secs"])
        self.assertAlmostEqual(e["reward"], g["reward"], places=6)

    def test_wx_lookback_gives_the_model_an_hour_of_history(self):
        # the model's features use 60 min of history (rv60, anomaly >= 45 min)

        class SpanModel(FakeModel):
            def fair(self, snap, settle_ts, strikes):
                h = snap.history
                span = (h[-1][0] - h[0][0]) / 60000.0 if h else 0.0
                return {k: min(0.99, span / 100.0) for k in strikes}

        write_fixture(wx_from=-4500.0)
        inp = sim.load_event_inputs(EVENT)
        sh = sim._Shared(inp, SpanModel(), None)
        tick_fs, _ = sh.react_view(0.5)
        self.assertGreaterEqual(tick_fs[0], 0)
        self.assertAlmostEqual(sh.st_f[tick_fs[0]][0], 0.59, places=6)   # 59 min at open
        self.assertNotIn("wx_history_short", sh.flags)
        self.assertGreater(sh.wx_rows_lookback, 0)
        # wx_rows counts the market's life only: 2 rows per minute, the last at
        # S-145 (pending), S-310 (final) -> minutes landing in [open, close)
        n_in = sum(1 for r in C.iter_raw("wx", OPEN, S))
        self.assertEqual(sh.wx_rows, n_in)

    def test_fair_recomputed_when_only_history_values_change(self):
        # a revision of an OLDER minute changes the model's history (trend,
        # rv, anomaly) but not the latest minute/index/stations or hist[-1]

        class HistModel(FakeModel):
            def fair(self, snap, settle_ts, strikes):
                vals = [v for _, v in snap.history]
                return {k: min(0.99, max(0.01, (sum(vals) / len(vals) - 70.0) / 20.0))
                        for k in strikes} if vals else {}

        # minute +20:00 revised to a cold reading, received at +25:40.2 (the
        # newest minute by then is +23:00 or so)
        write_fixture(extra_wx=[(1540.2, 1200.0, DROP_C)])
        inp = sim.load_event_inputs(EVENT)
        sh = sim._Shared(inp, HistModel(), None)
        k = sh.st_rx.index(OPEN + 1540.2)
        self.assertLess(sh.st_f[k][0], sh.st_f[k - 1][0])

    def test_run_day_reruns_subset_or_old_version(self):
        write_fixture()
        day = C.et_day(S)
        sim.run_day(day, variants=["A_live300_nowin"], workers=1)
        out = sim.run_day(day, workers=1)
        doc = [d for d in out if d["event"] == EVENT][0]
        self.assertEqual(set(doc["variants"]), run_keys())
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        m0 = os.path.getmtime(path)
        time.sleep(0.05)
        sim.run_day(day, variants=["B_live300_pull"], workers=1)     # subset: reuse
        self.assertEqual(os.path.getmtime(path), m0)
        old = C.read_json(path)
        old["sim_version"] = "0"
        C.write_json_atomic(path, old)
        m1 = os.path.getmtime(path)
        time.sleep(0.05)
        out = sim.run_day(day, variants=["B_live300_pull"], workers=1)
        self.assertGreater(os.path.getmtime(path), m1)
        self.assertEqual(C.read_json(path)["sim_version"], sim.SIM_VERSION)
        # a changed override for the same name is not reused either
        out = sim.run_day(day, variants={"B_live300_pull": dict(q_live=111)}, workers=1)
        self.assertEqual(out[0]["policies"]["B_live300_pull"], {"q_live": 111})


class TestNoLookahead(SimTestBase):
    UNTIL = 1500.0

    def _fixture(self, extra):
        base_trades = [(300.25, TICKER, "no", 40, 150.0), (700.25, TICKER, "yes", 55, 400.0),
                       (1496.75, TICKER, "no", 39, 50.0), (2000.25, TICKER, "no", 39, 50.0)]
        trades = list(base_trades)
        deltas = [(800.2, TICKER, "yes", 41, 300.0)]
        extra_wx = []
        if extra:
            trades += [(1500.25, TICKER, "no", 30, 500.0), (1501.25, TICKER, "yes", 70, 500.0)]
            deltas += [(1499.6, TICKER, "yes", 42, 5000.0), (1500.2, TICKER, "no", 46, 900.0)]
            # rows received in (UNTIL - react, UNTIL) are acted on only after
            # UNTIL; the rest arrive after it. (A fill within 3 s after the
            # 1499.6 row would legitimately be tagged fills_after_wx, so the
            # last pre-UNTIL trade sits at 1496.75.)
            extra_wx = [(1499.6, 1320.0, DROP_C), (1500.4, 1340.0, DROP_C)]
        write_fixture(trades=trades, deltas=deltas, extra_wx=extra_wx,
                      results={TICKER: "yes"})

    @staticmethod
    def _strip(res):
        out = {}
        for k, v in res["variants"].items():
            v = dict(v)
            v.pop("markouts")
            out[k] = v
        return out, res["market_stats"], res["model_eval"]

    def test_future_rows_do_not_change_the_past(self):
        vs = ["A_live300_nowin", "B_live300_pull", "F_full"]
        self._fixture(extra=False)
        base = sim.run_event(EVENT, variants=vs, model=FakeModel(),
                             until_ts=OPEN + self.UNTIL)
        full_base = sim.run_event(EVENT, variants=vs, model=FakeModel())
        _wipe()
        self._fixture(extra=True)
        more = sim.run_event(EVENT, variants=vs, model=FakeModel(),
                             until_ts=OPEN + self.UNTIL)
        full_more = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertEqual(self._strip(base), self._strip(more))
        self.assertGreater(base["variants"]["A_live300_nowin|opt"]["fills_n"], 0)
        # sanity: the appended rows DO matter for the full run
        self.assertNotEqual(self._strip(full_base)[0], self._strip(full_more)[0])


class TestSchemaAndDay(SimTestBase):
    SCHEMA = {
        "top": ["event", "open_ts", "close_ts", "settle_ts", "settled", "settlement_value",
                "results", "coverage", "market_stats", "model_eval", "variants"],
        "coverage": ["book_trusted_frac", "wx_rows", "trades", "trades_src", "reconnects",
                     "gaps", "program_secs"],
        "market_stats": ["strike_secs", "paying_frac_without_us", "locked99_frac",
                         "live_frac", "decided_frac", "depth3c_median"],
        "model_eval": ["n", "brier_model", "brier_mid", "logloss_model", "logloss_mid",
                       "by_tau"],
        "variant": ["reward", "fills_n", "contracts", "trading_pnl", "fees", "net",
                    "avg_share", "quoted_secs", "max_abs_pos", "worst_case_loss",
                    "by_class", "by_tau", "by_side", "fills_after_wx", "markouts"],
    }

    def test_output_schema_all_variants_real_model(self):
        tickers = write_fixture(strikes=(79.99, 80.99, 81.99),
                                trades=[(900.25, f"{EVENT}-T80.99", "no", 39, 20.0)],
                                results={f"{EVENT}-T79.99": "yes", f"{EVENT}-T80.99": "no",
                                         f"{EVENT}-T81.99": "no"})
        res = sim.run_event(EVENT)          # real FairModel (defaults in the sandbox)
        json.dumps(res)
        for k in self.SCHEMA["top"]:
            self.assertIn(k, res)
        for sect in ("coverage", "market_stats", "model_eval"):
            for k in self.SCHEMA[sect]:
                self.assertIn(k, res[sect], (sect, k))
        self.assertEqual(set(res["model_eval"]["by_tau"]), set(sim.TAU_BUCKETS))
        self.assertEqual(set(res["variants"]), run_keys())
        for name, v in res["variants"].items():
            for k in self.SCHEMA["variant"]:
                self.assertIn(k, v, (name, k))
            self.assertEqual(set(v["by_class"]), {"live", "decided"})
            self.assertEqual(set(v["by_tau"]), set(sim.TAU_BUCKETS))
            self.assertEqual(set(v["by_side"]), {"yes_bid", "no_bid"})
            for b in v["by_tau"].values():
                self.assertEqual(set(b), {"reward", "contracts", "pnl"})
            self.assertEqual(set(v["markouts"]), {"m60", "m300"})
            self.assertEqual(set(v["fills_after_wx"]), {"contracts", "pnl"})
        self.assertEqual(res["results"][tickers[0]], "yes")
        self.assertEqual(res["coverage"]["trades"], 1)
        self.assertEqual(res["market_stats"]["strike_secs"], 3 * 3480)
        self.assertAlmostEqual(res["market_stats"]["paying_frac_without_us"], 1.0)
        # G (decided only) never quotes a live strike; B quotes something
        self.assertGreater(res["variants"]["B_live300_pull|opt"]["quoted_secs"], 0)

    def test_run_day_writes_skips_and_reports_pending(self):
        write_fixture()
        # a second, unsettled event of the same ET day: meta only
        ev2 = "KXTEMPMIAH-26OCT1410"
        write_fixture(event=ev2, settled=False)
        day = C.et_day(S)
        out = sim.run_day(day, variants=["A_live300_nowin"], workers=1)
        by = {d["event"]: d for d in out}
        self.assertTrue(by[ev2].get("pending"))
        self.assertIn("A_live300_nowin|cons", by[EVENT]["variants"])
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(C.DATA_ROOT, "sim", day, f"{ev2}.json")))
        m0 = os.path.getmtime(path)
        time.sleep(0.05)
        out2 = sim.run_day(day, variants=["A_live300_nowin"], workers=1)
        self.assertEqual(os.path.getmtime(path), m0)            # skipped, loaded
        self.assertEqual({d["event"] for d in out2}, {EVENT, ev2})
        sim.run_day(day, variants=["A_live300_nowin"], force=True, workers=1)
        self.assertGreater(os.path.getmtime(path), m0)
        self.assertEqual(sim.day_events(day), sorted([EVENT, ev2]))

    def test_default_program_flagged(self):
        write_fixture(prog_window=None)
        res = sim.run_event(EVENT, variants=V_NOWIN, model=FakeModel())
        self.assertIn(f"program_default:{TICKER}", res["flags"])
        self.assertEqual(res["coverage"]["programs_src"], "default")
        self.assertGreater(res["variants"]["T|opt"]["reward"], 0)


class TestPadHold(SimTestBase):
    """Jack 2026-10-16: "hold the pads for the first 3 minutes of each hour
    going forward". G holds its 1c pads (pad_hold_open_s 180); G0_nohold is
    the G before. A decided strike (index 73.4 F vs T80.99): G0 rests a YES
    pad at 1c from the program start (+120 s) and a NO qualification pad;
    sweeps sell YES at 1c at +150 s and either side of the hold's end."""

    def _fixture(self):
        write_fixture(temps=lambda m: DROP_C, books={TICKER: ({3: 300.0}, {95: 300.0})},
                      trades=[(150.25, TICKER, "no", 1, 60.0),     # inside the hold
                              (180.40, TICKER, "no", 1, 100.0),    # G's pad not active yet
                              (180.75, TICKER, "no", 1, 100.0)],   # active from 180.65
                      results={TICKER: "no"}, expiration_value=73.4)

    def _run(self, variants):
        self._fixture()
        return sim.run_event(EVENT, variants=variants, model=FakeModel())

    def test_g_holds_pads_g0_does_not(self):
        res = self._run(["G_decided_only", "G0_nohold"])
        for fm in ("opt", "cons"):                 # the 1c level is empty: no queue either way
            g, g0 = (res["variants"][f"{n}|{fm}"] for n in ("G_decided_only", "G0_nohold"))
            self.assertAlmostEqual(g0["by_side"]["yes_bid"]["contracts"], 260.0, msg=fm)
            self.assertAlmostEqual(g0["trading_pnl"], -2.6, places=6)        # 260 x 1c, result no
            self.assertEqual(g["fills_n"], 1, fm)                            # only the +180.75 sweep
            self.assertAlmostEqual(g["contracts"], 100.0)
            self.assertAlmostEqual(g["trading_pnl"], -1.0, places=6)
            # in the hold G rests only the favorite: its YES side (300 deep) does
            # not qualify, so the strike pays it nothing until the pads go up;
            # from +180.65 both rest the same orders (the pad refilled after
            # each fill), so the whole gap is the 61 program seconds of the hold
            self.assertGreater(g0["reward"], g["reward"])
            self.assertLessEqual(g0["reward"] - g["reward"], 61 * 100.0 / 3480.0 + 1e-6)
            self.assertEqual(g["quoted_secs"], g0["quoted_secs"])            # the favorite quotes
        self.assertEqual(res["policies"]["G_decided_only"]["pad_hold_open_s"], 180.0)

    def test_hold_boundary_is_the_policys(self):
        self._fixture()
        inp = sim.load_event_inputs(EVENT)
        sh = sim._Shared(inp, FakeModel(), None)
        P = sim._resolve_variants(["G_decided_only"])["G_decided_only"]
        ps = sim._Pass(sh, P, "opt")
        seen = []
        orig = ps._activate

        def act(item):
            orig(item)
            o = ps.orders[0]
            seen.append((item[0], sorted(o["yes"]), sorted(o["no"])))
        ps._activate = act
        ps.run()
        first_pad = min(t for t, ys, ns in seen if 1 in ys)
        self.assertGreaterEqual(first_pad, OPEN + 180.0)
        self.assertLessEqual(first_pad, OPEN + 180.65 + 1e-6)
        self.assertFalse(any(1 in ys or 1 in ns for t, ys, ns in seen if t < OPEN + 180.0))
        self.assertTrue(any(95 in ns for t, ys, ns in seen if t < OPEN + 180.0))   # the favorite
        # the NO qualification pad (300 deep + 300 fav < 1,000) went up with it
        self.assertIn(1, [ns for t, ys, ns in seen if t == first_pad][0])

    def test_zero_hold_is_g0(self):
        res = self._run({"G0": dict(live=False, decided=True),
                         "Gh0": dict(live=False, decided=True, pad_hold_open_s=0)})
        for fm in ("opt", "cons"):
            self.assertEqual(res["variants"][f"G0|{fm}"], res["variants"][f"Gh0|{fm}"])


class TestSwpFillModel(SimTestBase):
    """Jack 2026-10-16, "1": "ship the fixes + the sweep-aware report column,
    and add favorite-only and overnight-favorite-only to the shadow". Every
    variant gets "<variant>|swp" (temp_shadow_sweep's model, Monte Carlo with
    fixed seeds) beside opt / cons. The fixture: a decided strike (index
    73.4 F vs T80.99, NO the favorite) whose YES 1c level is empty, so G's
    1c pad rests alone there (the hidden-sweep hazard applies) and recorded
    1c taker clusters hit it."""

    def setUp(self):
        super().setUp()
        self._draws = sim.SWP_DRAWS
        self._seed = sim.SWP_SEED
        sim.SWP_DRAWS = 3

    def tearDown(self):
        sim.SWP_DRAWS = self._draws
        sim.SWP_SEED = self._seed

    def _fixture(self, event=EVENT):
        tk = f"{event}-T{K:.2f}"
        write_fixture(event=event, temps=lambda m: DROP_C,
                      books={tk: ({3: 300.0}, {95: 300.0})},
                      trades=[(400.25, tk, "no", 1, 60.0), (1500.25, tk, "no", 1, 2000.0),
                              (2500.25, tk, "no", 2, 40.0)],
                      results={tk: "no"}, expiration_value=73.4)
        return tk

    def test_swp_for_every_variant_in_run_day(self):
        self._fixture()
        day = C.et_day(S)
        out = sim.run_day(day, workers=1)
        doc = [d for d in out if d["event"] == EVENT][0]
        self.assertEqual(sim.FILL_MODELS, ("opt", "cons", "swp"))
        self.assertEqual(sim.RUN_FILL_MODELS, ("opt", "cons", "swp", "stress"))
        self.assertEqual(set(doc["variants"]), run_keys())
        self.assertIn("FAV_only", sim.VARIANTS)
        self.assertIn("FAV_night", sim.VARIANTS)
        sw = doc["swp"]
        cfg = sim.swp_config()["ident"]
        self.assertEqual((sw["draws"], sw["rec_mode"], sw["channels"], sw["seed"]),
                         (3, "level", "abc", "tmm-sweep-1"))
        self.assertEqual(sw["hazard_id"], cfg["hazard_id"])
        self.assertEqual(os.path.basename(sw["hazard_src"]), "temp_shadow_sweep_hazard.json")
        self.assertEqual(os.path.basename(sw["tails_src"]), "temp_shadow_sweep_tails.json")
        keys = TestSchemaAndDay.SCHEMA["variant"]
        for v in sim.VARIANTS:
            r = doc["variants"][f"{v}|swp"]
            for k in keys + ["draws", "net_sd", "net_se", "draw_net", "swp_exact",
                             "filler_contracts", "net_cal"]:
                self.assertIn(k, r, (v, k))
            self.assertEqual(r["draws"], 3)
            self.assertEqual(len(r["draw_net"]), 3)
            self.assertAlmostEqual(r["net"], sum(r["draw_net"]) / 3, places=3)
            self.assertAlmostEqual(r["net"], r["reward"] + r["trading_pnl"] - r["fees"], places=4)
            self.assertAlmostEqual(r["net_se"], r["net_sd"] / math.sqrt(3), places=5)
        g0 = doc["variants"]["G0_nohold|swp"]
        # sweepers took G0's 1c pad (the cons model never filled the 2,000 sweep fully)
        self.assertGreater(g0["filler_contracts"], doc["variants"]["G0_nohold|cons"]["contracts"])
        self.assertGreater(g0["sweep_src"].get("haz", 0) + g0["sweep_src"].get("rec", 0), 0)
        # a cached file is reused while the swp configuration is the same ...
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        m0 = os.path.getmtime(path)
        time.sleep(0.05)
        sim.run_day(day, workers=1)
        self.assertEqual(os.path.getmtime(path), m0)
        # ... and re-simmed when the draw count (or the hazard / tails table) changes
        sim.SWP_DRAWS = 2
        again = sim.run_day(day, workers=1)
        self.assertGreater(os.path.getmtime(path), m0)
        self.assertEqual(again[0]["variants"]["G0_nohold|swp"]["draws"], 2)
        self.assertEqual(again[0]["variants"]["G0_nohold|swp"]["draw_net"],
                         g0["draw_net"][:2])            # draw k = the same arrivals

    def test_reproducible_with_fixed_seeds(self):
        self._fixture()
        vs = ["G0_nohold", "G_decided_only", "FAV_only"]
        a = sim.run_event(EVENT, variants=vs, model=FakeModel())
        b = sim.run_event(EVENT, variants=vs, model=FakeModel())
        for v in vs:
            self.assertEqual(a["variants"][f"{v}|swp"], b["variants"][f"{v}|swp"], v)
        self.assertGreater(a["variants"]["G0_nohold|swp"]["net_sd"], 0.0)   # the draws differ
        sim.SWP_SEED = "another-seed"
        c = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertNotEqual(c["variants"]["G0_nohold|swp"]["draw_net"],
                            a["variants"]["G0_nohold|swp"]["draw_net"])
        self.assertEqual(c["variants"]["FAV_only|swp"], a["variants"]["FAV_only|swp"])
        for fm in ("opt", "cons"):               # the deterministic models never see the seed
            self.assertEqual(c["variants"][f"G0_nohold|{fm}"], a["variants"][f"G0_nohold|{fm}"])

    def test_shortcuts_are_exact(self):
        import temp_shadow_sweep as SW
        self._fixture()
        inp = sim.load_event_inputs(EVENT)
        sh = SW.SweepShared(inp, FakeModel(), None)
        Ps = sim._resolve_variants(None)
        cfg = sim.swp_config(4)
        fast = sim.run_swp(sh, Ps, cfg, screen=True)
        full = sim.run_swp(sh, Ps, cfg, screen=False)
        for v in Ps:
            self.assertEqual(json.dumps(fast[v], sort_keys=True),
                             json.dumps(full[v], sort_keys=True), v)
        self.assertTrue(fast["FAV_only"]["swp_exact"])
        self.assertFalse(fast["G0_nohold"]["swp_exact"])
        # opt / cons on the sweep's shared state = on the sim's own
        sh0 = sim._Shared(inp, FakeModel(), None)
        for v in ("G0_nohold", "F_full", "B_live300_pull"):
            for fm in ("opt", "cons"):
                self.assertEqual(sim._Pass(sh, Ps[v], fm).run(), sim._Pass(sh0, Ps[v], fm).run())

    def test_fav_only_places_no_1c_orders(self):
        import temp_shadow_sweep as SW
        self._fixture()
        inp = sim.load_event_inputs(EVENT)
        sh = SW.SweepShared(inp, FakeModel(), None)
        Ps = sim._resolve_variants(["FAV_only", "G0_nohold"])
        placed = {}
        for name in Ps:
            for fm in ("opt", "cons", "swp"):
                ps = (SW.VPass(sh, Ps[name], "swp", hazard=sim.swp_config()["hazard"],
                               cands=SW.hazard_candidates(sh, 0, 0.3))
                      if fm == "swp" else sim._Pass(sh, Ps[name], fm))
                seen = placed.setdefault((name, fm), [])
                orig = ps._activate

                def act(item, _orig=orig, _seen=seen):
                    _seen.extend((side, op, p) for side, op, p, s in item[2] if op in
                                 ("place", "inc"))
                    _orig(item)
                ps._activate = act
                ps.run()
        for fm in ("opt", "cons", "swp"):
            fav = placed[("FAV_only", fm)]
            self.assertTrue(fav, fm)                                   # it quotes ...
            self.assertFalse([x for x in fav if x[2] == 1], fm)        # ... never at 1c
            self.assertTrue([x for x in fav if x[0] == "no" and x[2] == 95], fm)  # the favorite
            self.assertTrue([x for x in placed[("G0_nohold", fm)] if x[2] == 1], fm)
        res = sim.run_event(EVENT, variants=["FAV_only"], model=FakeModel())
        v = res["variants"]
        self.assertTrue(v["FAV_only|swp"]["swp_exact"])
        self.assertEqual(v["FAV_only|swp"]["filler_contracts"], 0.0)
        for k in ("reward", "contracts", "trading_pnl", "net", "quoted_secs"):
            self.assertEqual(v["FAV_only|swp"][k], v["FAV_only|cons"][k], k)

    def test_fav_night_quotes_only_its_et_settle_hours_across_dst(self):
        """FAV_night quotes the events SETTLING 20:00-07:00 ET. Around the
        2026-11-01 switch (EDT -> EST at 06:00Z) the 07:00 events settle at
        11:00Z (10/31) and 12:00Z (11/01): both quoted; the 08:00 ones
        (12:00Z / 13:00Z) are not; the repeated 01:00 (the ticker's first,
        05:00Z) is."""
        cases = (("KXTEMPMIAH-26OCT3107", True), ("KXTEMPMIAH-26OCT3108", False),
                 ("KXTEMPMIAH-26NOV0101", True), ("KXTEMPMIAH-26NOV0107", True),
                 ("KXTEMPMIAH-26NOV0108", False), ("KXTEMPMIAH-26NOV0121", True))
        for ev, _ in cases:
            self._fixture(ev)
        self.assertEqual(C.parse_event("KXTEMPMIAH-26NOV0107")[1], C.parse_iso("2026-11-01T12:00:00Z"))
        self.assertEqual(C.parse_event("KXTEMPMIAH-26OCT3108")[1], C.parse_iso("2026-10-31T12:00:00Z"))
        for ev, quoted in cases:
            res = sim.run_event(ev, variants=["FAV_only", "FAV_night"], model=FakeModel())
            v = res["variants"]
            for fm in sim.FILL_MODELS:
                fo, fn = v[f"FAV_only|{fm}"], v[f"FAV_night|{fm}"]
                self.assertGreater(fo["quoted_secs"], 0, (ev, fm))
                if quoted:
                    self.assertEqual(fn, fo, (ev, fm))
                else:
                    self.assertEqual((fn["quoted_secs"], fn["reward"], fn["contracts"]),
                                     (0, 0.0, 0.0), (ev, fm))
            self.assertEqual(res["policies"]["FAV_night"]["quote_hours_et"],
                             [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7])


class TestSwpFixPass(SimTestBase):
    """Fix pass 10/16 (review of the swp build): a failure in the sweep path
    costs swp only (the event keeps opt / cons, swp_error, no |swp keys, and
    is re-simmed next time); a model/ hazard or tails override that is not a
    usable table is rejected (the repo default is used and the rejection is
    recorded) instead of silently zeroing the hidden-sweep channel; run_day
    takes a deadline (the report's re-sim time budget)."""

    def setUp(self):
        super().setUp()
        self._draws = sim.SWP_DRAWS
        self._hrepo, self._trepo = sim.SWP_HAZARD_REPO, sim.SWP_TAILS_REPO
        sim.SWP_DRAWS = 2
        sim._SWP_CACHE.clear()
        self._model_dir = os.path.join(C.DATA_ROOT, "model")

    def tearDown(self):
        sim.SWP_DRAWS = self._draws
        sim.SWP_HAZARD_REPO, sim.SWP_TAILS_REPO = self._hrepo, self._trepo
        sim._SWP_CACHE.clear()
        for n in ("sweep_hazard.json", "sweep_tails.json"):
            p = os.path.join(self._model_dir, n)
            if os.path.exists(p):
                os.remove(p)

    def _fixture(self, event=EVENT):
        return TestSwpFillModel._fixture(self, event)

    def _write_model(self, name, doc_or_text):
        os.makedirs(self._model_dir, exist_ok=True)
        p = os.path.join(self._model_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(doc_or_text if isinstance(doc_or_text, str) else json.dumps(doc_or_text))
        sim._SWP_CACHE.clear()
        return p

    def test_swp_failure_keeps_opt_and_cons(self):
        import temp_shadow_sweep as SW
        from unittest import mock
        self._fixture()
        vs = ["G0_nohold", "FAV_only"]
        good = sim.run_event(EVENT, variants=vs, model=FakeModel())
        with mock.patch.object(SW.VPass, "run", side_effect=RuntimeError("boom")):
            bad = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertEqual(set(bad["variants"]), {f"{v}|{f}" for v in vs for f in ("opt", "cons")})
        self.assertTrue(bad["swp_error"].startswith("swp run failed: RuntimeError: boom"),
                        bad["swp_error"])
        self.assertIsNone(bad["swp"])
        self.assertEqual(bad["fill_models"], ["opt", "cons"])
        for v in vs:
            for fm in ("opt", "cons"):
                self.assertEqual(bad["variants"][f"{v}|{fm}"], good["variants"][f"{v}|{fm}"])
        self.assertNotIn("swp_error", good)
        # a setup failure (swp_config / SweepShared) is contained the same way
        with mock.patch.object(sim, "swp_config", side_effect=ValueError("no table")):
            bad2 = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertTrue(bad2["swp_error"].startswith("swp setup failed: ValueError: no table"))
        for v in vs:
            for fm in ("opt", "cons"):
                self.assertEqual(bad2["variants"][f"{v}|{fm}"], good["variants"][f"{v}|{fm}"])
        # the cache wants every fill model: such a file is re-simmed ...
        want = sim._policy_overrides(vs)
        ident = sim.swp_config()["ident"]
        self.assertTrue(sim._cached_ok(good, want, ident))
        self.assertFalse(sim._cached_ok(bad, want, ident))
        # ... unless swp itself is broken (a re-sim could not add it)
        self.assertTrue(sim._cached_ok(bad, want, None, swp_broken=True))

    def test_run_day_retries_an_event_whose_swp_failed(self):
        import temp_shadow_sweep as SW
        from unittest import mock
        self._fixture()
        day = C.et_day(S)
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        with mock.patch.object(SW.VPass, "run", side_effect=RuntimeError("boom")):
            out = sim.run_day(day, workers=1)
        doc = [d for d in out if d["event"] == EVENT][0]
        self.assertNotIn("error", doc)
        self.assertIn("swp_error", doc)
        self.assertIn("G0_nohold|cons", doc["variants"])
        self.assertNotIn("G0_nohold|swp", doc["variants"])
        self.assertIn("swp_error", C.read_json(path))
        again = sim.run_day(day, workers=1)
        doc2 = [d for d in again if d["event"] == EVENT][0]
        self.assertNotIn("swp_error", doc2)
        self.assertIn("G0_nohold|swp", doc2["variants"])
        self.assertEqual(doc2["variants"]["G0_nohold|cons"], doc["variants"]["G0_nohold|cons"])

    def test_run_day_with_swp_config_broken(self):
        from unittest import mock
        self._fixture()
        day = C.et_day(S)
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        with mock.patch.object(sim, "swp_config", side_effect=ValueError("repo table gone")):
            out = sim.run_day(day, workers=1)
            doc = [d for d in out if d["event"] == EVENT][0]
            self.assertIn("swp setup failed: ValueError: repo table gone", doc["swp_error"])
            self.assertIn("F_full|opt", doc["variants"])
            m0 = os.path.getmtime(path)
            time.sleep(0.05)
            sim.run_day(day, workers=1)          # reused: a re-sim could not add swp
            self.assertEqual(os.path.getmtime(path), m0)
        sim.run_day(day, workers=1)              # swp back: re-simmed with it
        self.assertGreater(os.path.getmtime(path), m0)
        self.assertIn("F_full|swp", C.read_json(path)["variants"])

    def test_hazard_and_tails_overrides_are_validated(self):
        repo = sim.swp_config()["ident"]
        self.assertEqual((repo["hazard_kind"], repo["tails_kind"]), ("repo", "repo"))
        self.assertIsNone(repo["hazard_rejected"])
        with open(self._hrepo, encoding="utf-8") as f:
            hdoc = json.load(f)
        with open(self._trepo, encoding="utf-8") as f:
            tdoc = json.load(f)
        zero = json.loads(json.dumps(hdoc))
        zero["hazard"]["a"] = {fb: {ab: 0.0 for ab in d} for fb, d in zero["hazard"]["a"].items()}
        zero["hazard"]["lam_pool"] = {k: 0.0 for k in zero["hazard"].get("lam_pool") or {}}
        nosize = json.loads(json.dumps(hdoc))
        nosize.pop("sizes_all")
        # the review's file: no 'hazard' fit -> Hazard() would read it as all zero
        for bad, why in (({"version": 1, "built": "x"}, "no 'hazard' fit"),
                         (zero, "lam_max 0 <= 0"), (nosize, "no 'sizes_all'"),
                         ("{not json", "")):
            self._write_model("sweep_hazard.json", bad)
            cfg = sim.swp_config()
            ident = cfg["ident"]
            self.assertEqual(ident["hazard_kind"], "repo", why)
            self.assertEqual(ident["hazard_id"], repo["hazard_id"], why)
            self.assertIn("sweep_hazard.json", ident["hazard_rejected"])
            self.assertIn(why, ident["hazard_rejected"])
            self.assertGreater(cfg["hazard"].lam_max, 0.0)
        # a usable recalibration is used (and re-keys the cache)
        better = json.loads(json.dumps(hdoc))
        better["hazard"]["a"] = {fb: {ab: 2.0 * float(r) for ab, r in d.items()}
                                 for fb, d in better["hazard"]["a"].items()}
        self._write_model("sweep_hazard.json", better)
        ident = sim.swp_config()["ident"]
        self.assertEqual(ident["hazard_kind"], "override")
        self.assertIsNone(ident["hazard_rejected"])
        self.assertNotEqual(ident["hazard_id"], repo["hazard_id"])
        # tails: no bucket rows / out of range -> rejected; a copy -> used
        for bad, why in (({"buckets": []}, "no 'buckets'"),
                         ({"buckets": [[0.0, 0.6, 0.1, 0.1, 0.0, 0.2]]}, "out of range"),
                         ({"buckets": [[0.0, 0.1]]}, "not 6 numbers")):
            self._write_model("sweep_tails.json", bad)
            ident = sim.swp_config()["ident"]
            self.assertEqual((ident["tails_kind"], ident["tails_id"]), ("repo", repo["tails_id"]))
            self.assertIn(why, ident["tails_rejected"])
        self._write_model("sweep_tails.json", tdoc)
        ident = sim.swp_config()["ident"]
        self.assertEqual((ident["tails_kind"], ident["tails_id"]), ("override", repo["tails_id"]))
        # an unusable REPO table raises (run_event contains it)
        badrepo = os.path.join(C.DATA_ROOT, "bad_repo_hazard.json")
        with open(badrepo, "w", encoding="utf-8") as f:
            json.dump({"version": 1}, f)
        os.remove(os.path.join(self._model_dir, "sweep_hazard.json"))
        sim.SWP_HAZARD_REPO = badrepo
        sim._SWP_CACHE.clear()
        with self.assertRaisesRegex(ValueError, "not usable"):
            sim.swp_config()

    def test_bad_override_does_not_zero_the_hidden_sweeps(self):
        # the review's scenario: the malformed override used to give lam_max
        # 0 and no hidden sweeps at all; now the event sims on the repo table
        self._fixture()
        vs = ["G0_nohold"]
        a = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self._write_model("sweep_hazard.json", {"version": 1, "built": "x"})
        b = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertEqual(b["variants"]["G0_nohold|swp"], a["variants"]["G0_nohold|swp"])
        self.assertGreater(b["swp"]["lam_max"], 0.0)
        self.assertIn("no 'hazard' fit", b["swp"]["hazard_rejected"])

    def test_run_day_deadline_defers_unstarted_events(self):
        evs = [f"KXTEMPMIAH-26OCT14{h:02d}" for h in (9, 10, 11, 12, 13)]
        for ev in evs:
            self._fixture(ev)
        day = C.et_day(S)
        d = os.path.join(C.DATA_ROOT, "sim", day)
        # sequential: past the deadline nothing starts; an older file stays
        os.makedirs(d, exist_ok=True)
        old = {"event": evs[0], "settled": True, "sim_version": "4", "variants": {}}
        C.write_json_atomic(os.path.join(d, f"{evs[0]}.json"), old)
        out = sim.run_day(day, workers=1, deadline=time.time() - 1.0)
        mine = [r for r in out if r["event"] in evs]
        self.assertEqual(sorted(r["event"] for r in mine), evs)
        self.assertTrue(all(r.get("deferred") and r.get("settled") for r in mine), mine)
        self.assertEqual(C.read_json(os.path.join(d, f"{evs[0]}.json")), old)
        self.assertEqual(sorted(os.listdir(d)), [f"{evs[0]}.json"])
        # the pool path (the report task's 2 workers), on threads so the
        # sandbox's data root is shared (a spawned process would re-import
        # this module and make a new one): what has not started is
        # deferred, what started finishes and is written
        import concurrent.futures as cf
        from unittest import mock
        pool = mock.patch.object(cf, "ProcessPoolExecutor", cf.ThreadPoolExecutor)
        prio = mock.patch.object(sim, "_lower_priority", lambda: None)
        with pool, prio:
            out = sim.run_day(day, workers=2, deadline=time.time() - 1.0)
        mine = {r["event"]: r for r in out if r["event"] in evs}
        self.assertEqual(sorted(mine), evs)
        done = [e for e, r in mine.items() if not r.get("deferred")]
        deferred = [e for e, r in mine.items() if r.get("deferred")]
        self.assertGreaterEqual(len(deferred), 2)          # at most the 2 workers had started
        for e in done:
            self.assertIn("variants", mine[e])
            self.assertTrue(os.path.exists(os.path.join(d, f"{e}.json")))
        for e in deferred:
            if e != evs[0]:
                self.assertFalse(os.path.exists(os.path.join(d, f"{e}.json")), e)
        # no deadline: everything is simmed
        with pool, prio:
            out = sim.run_day(day, workers=2)
        self.assertFalse([r for r in out if r["event"] in evs and not r.get("variants")])


NIGHT_EVENT = "KXTEMPMIAH-26OCT1421"          # settles 21:00 ET: FAV_night quotes it


class TestStressFillModel(SimTestBase):
    """Jack 2026-10-17, "1": the adopted go-live rules decide on the STRESS
    fill model (temp_shadow_sweep: swp + level-clear (d) + hidden favorite
    takers (e)), so the daily pipeline writes "<variant>|stress" next to opt
    / cons / swp for the STRESS_SCOPE variants (default: live=False), with
    swp's draws and seeds and the favorite-taker table's id. The fixture: a
    decided strike (NO the favorite at 95c) whose favorite level a recorded
    taker walks through (95 then 94 within a second), plus swp's 1c sweeps."""

    def setUp(self):
        super().setUp()
        self._saved = (sim.SWP_DRAWS, sim.STRESS_SCOPE, sim.STRESS_FAVHAZ_REPO)
        sim.SWP_DRAWS = 3
        sim._SWP_CACHE.clear()
        self._model_dir = os.path.join(C.DATA_ROOT, "model")

    def tearDown(self):
        sim.SWP_DRAWS, sim.STRESS_SCOPE, sim.STRESS_FAVHAZ_REPO = self._saved
        sim._SWP_CACHE.clear()
        p = os.path.join(self._model_dir, "sweep_favhazard.json")
        if os.path.exists(p):
            os.remove(p)

    def _fixture(self, event=EVENT, books=None, walk=True):
        tk = f"{event}-T{K:.2f}"
        trades = [(400.25, tk, "no", 1, 60.0), (1500.25, tk, "no", 1, 2000.0),
                  (2500.25, tk, "no", 2, 40.0)]
        if walk:      # a taker buys YES at 5c then 6c: NO bids at 95 then 94
            trades += [(900.25, tk, "yes", 5, 300.0), (900.30, tk, "yes", 6, 50.0)]
        write_fixture(event=event, temps=lambda m: DROP_C,
                      books=books or {tk: ({3: 300.0}, {95: 300.0})},
                      trades=trades, results={tk: "no"}, expiration_value=73.4)
        return tk

    def test_stress_for_the_scope_in_run_day(self):
        self._fixture()
        day = C.et_day(S)
        out = sim.run_day(day, workers=1)
        doc = [d for d in out if d["event"] == EVENT][0]
        self.assertEqual(sim.STRESS_SCOPE, "nolive")
        nolive = sorted(n for n, ov in sim.VARIANTS.items() if sim.in_stress_scope(ov, "nolive"))
        self.assertEqual(set(doc["variants"]), run_keys())
        self.assertEqual(sorted(k.split("|")[0] for k in doc["variants"] if k.endswith("|stress")),
                         nolive)
        self.assertIn("FAV_night_agree|stress", doc["variants"])
        self.assertNotIn("F_full|stress", doc["variants"])
        self.assertEqual(doc["fill_models"], ["opt", "cons", "swp", "stress"])
        st = doc["stress"]
        cfg = sim.stress_config()["ident"]
        self.assertEqual(st["fav_hazard_id"], cfg["fav_hazard_id"])
        self.assertEqual(os.path.basename(st["fav_hazard_src"]), "temp_shadow_sweep_favhazard.json")
        self.assertEqual((st["fav_hazard_kind"], st["scope"], st["draws"], st["seed"],
                          st["fav_seed"], st["take"], st["hole"]),
                         ("repo", "nolive", 3, "tmm-sweep-1", "tmm-favhaz-1", "full", False))
        self.assertEqual(st["variants"], nolive)
        keys = TestSchemaAndDay.SCHEMA["variant"]
        for v in nolive:
            r = doc["variants"][f"{v}|stress"]
            for k in keys + ["draws", "net_sd", "net_se", "draw_net", "stress_exact",
                             "filler_contracts", "net_cal", "clear_events", "favhaz_events",
                             "above1c_contracts", "fav_fills"]:
                self.assertIn(k, r, (v, k))
            self.assertEqual(len(r["draw_net"]), 3)
            self.assertAlmostEqual(r["net"], sum(r["draw_net"]) / 3, places=3)
            self.assertAlmostEqual(r["net"], r["reward"] + r["trading_pnl"] - r["fees"], places=4)
        # the walking taker takes FAV_only's whole join under stress (cons /
        # swp: only the 50 it printed below our price)
        fs, fw = doc["variants"]["FAV_only|stress"], doc["variants"]["FAV_only|swp"]
        self.assertGreater(fs["clear_events"], 0)
        self.assertGreater(fs["contracts"], fw["contracts"])
        self.assertGreater(fs["above1c_contracts"], 0)
        # cached while the stress configuration is the same; re-simmed when
        # the scope changes (stress for every variant)
        path = os.path.join(C.DATA_ROOT, "sim", day, f"{EVENT}.json")
        m0 = os.path.getmtime(path)
        time.sleep(0.05)
        sim.run_day(day, workers=1)
        self.assertEqual(os.path.getmtime(path), m0)
        sim.STRESS_SCOPE = "all"
        again = [d for d in sim.run_day(day, workers=1) if d["event"] == EVENT][0]
        self.assertGreater(os.path.getmtime(path), m0)
        self.assertIn("F_full|stress", again["variants"])
        self.assertEqual(again["stress"]["scope"], "all")
        self.assertEqual(again["variants"]["FAV_only|stress"], fs)     # same draws
        sim.STRESS_SCOPE = "bogus"
        with self.assertRaises(ValueError):
            sim.stress_scope()

    def test_stress_shortcuts_are_exact(self):
        import temp_shadow_sweep as SW
        self._fixture()
        inp = sim.load_event_inputs(EVENT)
        sh = SW.SweepShared(inp, FakeModel(), None)
        Ps = sim._resolve_variants(None)
        cfg = sim.swp_config(4)
        st = sim.stress_config(cfg)
        fast = sim.run_stress(sh, Ps, cfg, st, screen=True)
        full = sim.run_stress(sh, Ps, cfg, st, screen=False)
        for v in Ps:
            self.assertEqual(json.dumps(fast[v], sort_keys=True),
                             json.dumps(full[v], sort_keys=True), v)
        self.assertFalse(fast["G0_nohold"]["stress_exact"])

    def test_stress_matches_the_standalone_sweep(self):
        # the pipeline's stress = temp_shadow_sweep.run_event_variants (the
        # 10/17 audit's stress-table) on the same draws: G0_nohold = its "G",
        # FAV_only = its "FAV_only"
        import temp_shadow_sweep as SW
        self._fixture()
        res = sim.run_event(EVENT, variants=["G0_nohold", "FAV_only"], model=FakeModel())
        cfg = sim.swp_config()
        st = sim.stress_config(cfg)
        alone = SW.run_event_variants(
            EVENT, {"G": SW.SWEEP_VARIANTS["G"], "FAV_only": SW.SWEEP_VARIANTS["FAV_only"]},
            draws=3, hazard=cfg["hazard"], model=FakeModel(), fill_models=("stress",),
            tail=cfg["tails"], fav_hazard=st["fav_hazard"])
        for mine, theirs in (("G0_nohold", "G"), ("FAV_only", "FAV_only")):
            a, b = res["variants"][f"{mine}|stress"], alone["variants"][f"{theirs}|stress"]
            self.assertEqual(a["draw_net"], b["draw_net"], mine)
            for k in ("net", "reward", "trading_pnl", "contracts", "net_cal", "clear_events",
                      "favhaz_events", "filler_contracts"):
                self.assertAlmostEqual(a[k], b[k], places=4, msg=(mine, k))
        self.assertEqual(res["stress"]["fav_hazard_id"], alone["stress"]["fav_hazard_id"])

    def test_stress_failure_is_contained(self):
        from unittest import mock
        self._fixture()
        vs = ["G0_nohold", "FAV_only"]
        good = sim.run_event(EVENT, variants=vs, model=FakeModel())
        with mock.patch.object(sim, "run_stress", side_effect=RuntimeError("boom")):
            bad = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertEqual(set(bad["variants"]),
                         {f"{v}|{f}" for v in vs for f in ("opt", "cons", "swp")})
        self.assertTrue(bad["stress_error"].startswith("stress run failed: RuntimeError: boom"))
        self.assertNotIn("stress", bad)
        self.assertEqual(bad["fill_models"], ["opt", "cons", "swp"])
        for k in bad["variants"]:
            self.assertEqual(bad["variants"][k], good["variants"][k], k)
        want = sim._policy_overrides(vs)
        sw = sim.swp_config()
        ident, sid = sw["ident"], sim.stress_config(sw)["ident"]
        self.assertTrue(sim._cached_ok(good, want, ident, False, sid))
        self.assertFalse(sim._cached_ok(bad, want, ident, False, sid))     # re-simmed next time
        self.assertTrue(sim._cached_ok(bad, want, ident, False, None, stress_broken=True))
        # a changed favorite-taker table re-keys the cache
        self.assertFalse(sim._cached_ok(good, want, ident, False,
                                        dict(sid, fav_hazard_id="feedfacefeedface")))
        # stress needs swp: a sweep setup failure costs both
        with mock.patch.object(sim, "swp_config", side_effect=ValueError("no table")):
            bad2 = sim.run_event(EVENT, variants=vs, model=FakeModel())
        self.assertIn("swp setup failed: ValueError: no table", bad2["swp_error"])
        self.assertIn("stress setup failed: the sweep setup it builds on failed",
                      bad2["stress_error"])
        self.assertEqual(set(bad2["variants"]), {f"{v}|{f}" for v in vs for f in ("opt", "cons")})

    def test_a_request_outside_the_scope_needs_no_stress(self):
        # live-quoting variants only (outside the default scope): no stress
        # block, and the cached file is reused (it was re-simmed every time)
        self._fixture()
        res = sim.run_event(EVENT, variants=["A_live300_nowin"], model=FakeModel())
        self.assertNotIn("stress", res)
        self.assertNotIn("stress_error", res)
        self.assertEqual(res["fill_models"], ["opt", "cons", "swp"])
        sw = sim.swp_config()
        self.assertTrue(sim._cached_ok(res, sim._policy_overrides(["A_live300_nowin"]),
                                       sw["ident"], False, sim.stress_config(sw)["ident"]))

    def test_favhazard_override_is_validated(self):
        repo = sim.stress_config()["ident"]
        self.assertEqual((repo["fav_hazard_kind"], repo["fav_hazard_rejected"]), ("repo", None))
        os.makedirs(self._model_dir, exist_ok=True)
        p = os.path.join(self._model_dir, "sweep_favhazard.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1}, f)
        sim._SWP_CACHE.clear()
        ident = sim.stress_config()["ident"]
        self.assertEqual((ident["fav_hazard_kind"], ident["fav_hazard_id"]),
                         ("repo", repo["fav_hazard_id"]))
        self.assertIn("no 'hazard' fit", ident["fav_hazard_rejected"])
        with open(sim.STRESS_FAVHAZ_REPO, encoding="utf-8") as f:
            doc = json.load(f)
        doc["hazard"]["a"] = {fb: {ab: 2.0 * float(r) for ab, r in d.items()}
                              for fb, d in doc["hazard"]["a"].items()}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        sim._SWP_CACHE.clear()
        ident = sim.stress_config()["ident"]
        self.assertEqual(ident["fav_hazard_kind"], "override")
        self.assertNotEqual(ident["fav_hazard_id"], repo["fav_hazard_id"])

    def test_fav_market_min_p_in_the_sim(self):
        """FAV_night_agree = FAV_night + fav_market_min_p 0.95 (the market
        must price the favorite >= 0.95 on the external book). A night
        event: on a book where the market agrees (NO 95 / YES 3: 0.96) it is
        FAV_night exactly, under every fill model; where it does not (NO 90
        / YES 8: 0.91) it rests nothing."""
        tk = self._fixture(NIGHT_EVENT, walk=False)
        res = sim.run_event(NIGHT_EVENT, variants=["FAV_night", "FAV_night_agree"],
                            model=FakeModel())
        for fm in sim.RUN_FILL_MODELS:
            a, b = res["variants"][f"FAV_night|{fm}"], res["variants"][f"FAV_night_agree|{fm}"]
            self.assertGreater(a["quoted_secs"], 0, fm)
            self.assertEqual(a, b, fm)
        self.assertEqual(res["policies"]["FAV_night_agree"]["fav_market_min_p"], 0.95)
        _wipe()
        self._fixture(NIGHT_EVENT, books={tk: ({8: 300.0}, {90: 300.0})}, walk=False)
        res = sim.run_event(NIGHT_EVENT, variants=["FAV_night", "FAV_night_agree"],
                            model=FakeModel())
        for fm in sim.RUN_FILL_MODELS:
            a, b = res["variants"][f"FAV_night|{fm}"], res["variants"][f"FAV_night_agree|{fm}"]
            self.assertGreater(a["quoted_secs"], 0, fm)
            self.assertEqual((b["quoted_secs"], b["reward"], b["contracts"]), (0, 0.0, 0.0), fm)

    def test_fav_hits_recorded(self):
        import temp_shadow_sweep as SW
        tk = self._fixture()
        res = sim.run_event(EVENT, variants=["FAV_only"], model=FakeModel())
        rows = res["fav_hits"]
        # the walking taker hit the NO favorite's bids at 95 (one cluster: 95
        # and 94 within a second); NO won, so no upset; the 1c sweeps are not
        # favorite-bid hits
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r[0], tk)
        self.assertIn(r[1], SW.FAV_LABELS)
        self.assertEqual((r[2], r[3], r[5], r[6]), (1, 0, 350.0, 0.0))
        self.assertGreater(r[4], 0.0)                       # the model's longshot probability

if __name__ == "__main__":
    unittest.main()
