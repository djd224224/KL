#!/usr/bin/env python3
"""Tests for the sweep-aware shadow (temp_shadow_sweep.py) and the live-order
subtraction in temp_shadow_scoring.py (Jack 2026-10-16: "run the sweep-aware
shadow, without pausing").

SANDBOXED: TEMP_SHADOW_DATA and TEMP_MM_LIVE_DIR point at fresh temp dirs
BEFORE any temp_shadow module is imported, so nothing here reads or writes
the live recording or the live executor's logs. Synthetic events are written
in the exact live shapes (as test_temp_shadow_sim does) and replayed by the
REAL scoring / sim / policy code; a constant FairModel stand-in fixes fairs.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_shadow_sweep -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tss_sweep_")
os.environ["TEMP_MM_LIVE_DIR"] = tempfile.mkdtemp(prefix="tss_sweep_live_")
os.environ.pop("TEMP_SHADOW_SWEEP_OUT", None)
os.environ.pop("TEMP_SHADOW_SUBTRACT_LIVE", None)

import json        # noqa: E402
import math        # noqa: E402
import shutil      # noqa: E402
import unittest    # noqa: E402

import temp_mm_policy as pol         # noqa: E402
import temp_shadow_common as C      # noqa: E402
import temp_shadow_scoring as scoring  # noqa: E402
import temp_shadow_sim as sim       # noqa: E402
import temp_shadow_sweep as SW      # noqa: E402

TMP = os.path.realpath(tempfile.gettempdir())
assert os.path.realpath(C.DATA_ROOT).startswith(TMP)
assert os.path.realpath(scoring.live_dir()).startswith(TMP)

EVENT = "KXTEMPMIAH-26OCT1409"
S = C.parse_event(EVENT)[1]
OPEN = S - 3600.0
K = 80.99
TICKER = f"{EVENT}-T{K:.2f}"
STATIONS = C.STATIONS["miami"]
BOOK_Y = {98: 500.0, 97: 1000.0}      # YES favorite; NO side empty: our NO 1c pad
BOOK_N = {}                           # (the longshot side) rests alone


class ConstModel:
    """fair() = a constant P(YES) for every strike (needs a wx snapshot)."""

    def __init__(self, p=0.985):
        self.p = p

    def dist(self, snap, settle_ts):
        return (80.0, 1.0) if snap is not None else None

    def fair(self, snap, settle_ts, strikes):
        return {k: self.p for k in strikes} if snap is not None else {}


def _wipe():
    for root in (C.DATA_ROOT, scoring.live_dir()):
        r = os.path.realpath(root)
        assert r.startswith(TMP)
        for sub in os.listdir(r):
            p = os.path.join(r, sub)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
    scoring._LIVE_CACHE.clear()


def _iso_us(ts):
    whole = math.floor(ts)
    us = int(round((ts - whole) * 1e6))
    return C.iso(whole)[:-1] + f".{us:06d}Z"


def _d(c):
    return f"{c / 100.0:.4f}"


def write_event(deltas=(), trades=(), books=None, results=None, prog=(120.0, 3600.0)):
    """A one-strike fake event. deltas [(t, side, cents, delta)], trades
    [(t, taker_side, yes_cents, count, trade_id)]; t = seconds after open."""
    by, bn = books or (BOOK_Y, BOOK_N)
    mk = [dict(ticker=TICKER, event_ticker=EVENT, floor_strike=K, strike_type="greater",
               open_time=C.iso(OPEN), close_time=C.iso(S), status="active", result="")]
    C.append_jsonl(C.raw_file("meta", OPEN - 600), {"rx": OPEN - 600, "kind": "markets",
                                                    "data": {"event_ticker": EVENT, "markets": mk}})
    progs = [dict(id="p0", market_ticker=TICKER, start_date=_iso_us(OPEN + prog[0]),
                  end_date=_iso_us(OPEN + prog[1]), period_reward=1000000,
                  target_size_fp="1000.00", discount_factor_bps=5000)]
    C.append_jsonl(C.raw_file("meta", OPEN + 60), {"rx": OPEN + 60, "kind": "programs",
                                                   "data": {"programs": progs}})
    ws = [(OPEN - 700, {"rx": OPEN - 700, "marker": "connect", "detail": "test"})]
    rows = [(OPEN - 650, "orderbook_snapshot",
             {"market_ticker": TICKER,
              "yes_dollars_fp": [[_d(p), f"{q:.2f}"] for p, q in sorted(by.items())],
              "no_dollars_fp": [[_d(p), f"{q:.2f}"] for p, q in sorted(bn.items())]})]
    for t, side, c, dq in deltas:
        rows.append((OPEN + t, "orderbook_delta", {"market_ticker": TICKER, "price_dollars": _d(c),
                                                   "delta_fp": f"{dq:.2f}", "side": side}))
    rows.sort(key=lambda r: r[0])
    for seq, (rx, typ, msg) in enumerate(rows, start=1):
        ws.append((rx, {"rx": rx, "m": {"type": typ, "sid": 1, "seq": seq, "msg": msg}}))
    rest = []
    for seq, (t, side, yc, cnt, tid) in enumerate(sorted(trades), start=1):
        ts = OPEN + t
        body = {"trade_id": tid, "market_ticker": TICKER, "yes_price_dollars": _d(yc),
                "no_price_dollars": _d(100 - yc), "count_fp": f"{cnt:.2f}", "taker_side": side,
                "is_block_trade": False, "ts": int(math.floor(ts)), "ts_ms": int(round(ts * 1000))}
        ws.append((ts + 0.05, {"rx": ts + 0.05, "m": {"type": "trade", "sid": 2, "seq": seq,
                                                       "msg": body}}))
        rest.append(dict({k: v for k, v in body.items() if k not in ("ts", "ts_ms")},
                         ticker=TICKER, created_time=_iso_us(ts)))
    for rx, rec in sorted(ws, key=lambda r: r[0]):
        C.append_jsonl(C.raw_file("ws", rx), rec)
    m = OPEN - 900
    while m < S:
        for lag, final in ((145.0, False), (310.0, True)):
            if m + lag < S:
                cs = [27, 27, 27, 27, 28]
                C.append_jsonl(C.raw_file("wx", m + lag), {
                    "rx": m + lag, "city": "miami", "t": int(m * 1000),
                    "status": "normal" if final else "incomplete",
                    "v": C.index_from_sum(sum(cs)) if final else None,
                    "stations": [{"station_id": sid, "temp_f": round(c * 1.8 + 32, 1),
                                  "code": "ok" if final else "pending",
                                  "received_at_ms": int((m + 144) * 1000)}
                                 for sid, c in zip(STATIONS, cs)]})
        m += 60.0
    res = results or {TICKER: "yes"}
    C.write_json_atomic(C.settled_file(EVENT), {
        "event_ticker": EVENT, "markets": [dict(mk[0], result=res[TICKER], status="finalized",
                                                expiration_value="85.0")],
        "trades": {TICKER: rest}, "programs": progs})


def write_live(orders=(), fills=()):
    """Live executor logs in its shapes (orders_/fills_<UTC day>.jsonl)."""
    root = scoring.live_dir()
    day = C.iso(OPEN)[:10]
    with open(os.path.join(root, f"orders_{day}.jsonl"), "a", encoding="utf-8") as f:
        for r in orders:
            f.write(json.dumps(dict({"mode": "live", "ticker": TICKER}, **r)) + "\n")
    with open(os.path.join(root, f"fills_{day}.jsonl"), "a", encoding="utf-8") as f:
        for r in fills:
            f.write(json.dumps(dict({"mode": "live", "ticker": TICKER}, **r)) + "\n")
    scoring._LIVE_CACHE.clear()


G = SW.resolve_variant(SW.SWEEP_VARIANTS["G"])[0]


def hz_doc(rate=0.0, cleared=True, size=1000.0):
    return {"built": "test", "hazard": {"a": {fb: {ab: rate for ab in SW.AGE_LABELS}
                                              for fb in SW.FAV_LABELS},
                                        "mult": {mb: 1.0 for mb in SW.MIN_LABELS},
                                        "lam_pool": {fb: rate for fb in SW.FAV_LABELS}},
            "sizes": {}, "sizes_all": [[size, int(cleared)]]}


class Base(unittest.TestCase):
    def setUp(self):
        _wipe()

    def shared(self, subtract=True, p=0.985):
        return SW.shared_for(EVENT, ConstModel(p), subtract=subtract)


# --------------------------------------------------------------------------
# 1. live-order reconstruction + subtraction (temp_shadow_scoring)
# --------------------------------------------------------------------------

class TestLiveOrders(Base):

    def test_timeline_kinds(self):
        T = OPEN
        write_live(orders=[
            dict(ts=T + 10, kind="place", oid="A", side="no", price=1, size=1000,
                 expiration_ts=T + 40, tag="pad"),
            dict(ts=T + 10, kind="place", oid="B", side="yes", price=97, size=300,
                 expiration_ts=T + 100, tag="fav"),
            dict(ts=T + 15, kind="amend", oid="B", side="yes", price=96, size=200, total=200),
            dict(ts=T + 20, kind="renew", oid="A", side="no", price=1, size=1000, total=1000,
                 expiration_ts=T + 80),
            dict(ts=T + 25, kind="decrease", oid="B", side="yes", price=96, size=150, reduce_by=50),
            dict(ts=T + 30, kind="place", oid="C", side="no", price=1, size=500,
                 expiration_ts=T + 35, tag="qual"),            # expires, never renewed
            dict(ts=T + 45, kind="cancel", oid="B"),
            dict(ts=T + 50, kind="place", oid="D", side="no", price=2, size=9, mode="dry"),
            dict(ts=T + 60, kind="reject", oid="E", side="no", price=1, size=5),
        ], fills=[dict(ts=T + 22, order_id="A", count=400, price_ours=1, side_ours="no",
                       trade_id="t1")])
        own = scoring.live_own_orders(EVENT, int(T), int(T) + 90)
        self.assertEqual(own[5], {})                                      # T+5.5
        self.assertEqual(own[12][TICKER], ({97: 300.0}, {1: 1000.0}))
        self.assertEqual(own[16][TICKER], ({96: 200.0}, {1: 1000.0}))     # amend total
        self.assertEqual(own[23][TICKER], ({96: 200.0}, {1: 600.0}))      # fill
        self.assertEqual(own[26][TICKER], ({96: 150.0}, {1: 600.0}))      # decrease
        self.assertEqual(own[31][TICKER], ({96: 150.0}, {1: 1100.0}))     # + C
        self.assertEqual(own[36][TICKER], ({96: 150.0}, {1: 600.0}))      # C expired
        self.assertEqual(own[46][TICKER], ({}, {1: 600.0}))               # B cancelled
        self.assertEqual(own[55][TICKER], ({}, {1: 600.0}))               # dry row ignored
        self.assertEqual(own[79][TICKER], ({}, {1: 600.0}))               # renewed to T+80
        self.assertEqual(own[80], {})                                      # expired at T+80
        self.assertIs(own[50], own[51])                                   # copy-on-write

    def test_none_without_logs(self):
        self.assertIsNone(scoring.live_own_orders(EVENT, int(OPEN), int(OPEN) + 10))
        write_live(orders=[dict(ts=OPEN + 1, kind="place", oid="X", side="no", price=1, size=5,
                                ticker="KXTEMPMIAH-26OCT1410-T80.99")])
        self.assertIsNone(scoring.live_own_orders(EVENT, int(OPEN), int(OPEN) + 10))

    def test_gone_and_adopt(self):
        write_live(orders=[
            dict(ts=OPEN + 5, kind="adopt", oid="A", side="no", price=1, size=700),
            dict(ts=OPEN + 9, kind="gone", oid="A")])
        own = scoring.live_own_orders(EVENT, int(OPEN), int(OPEN) + 12)
        self.assertEqual(own[6][TICKER], ({}, {1: 700.0}))
        self.assertEqual(own[10], {})


class TestSubtraction(Base):

    def setUp(self):
        super().setUp()
        # recorded book: someone's 300 NO@1 from t=50; OUR 1000 NO@1 joins at
        # t=100 (in the recording, as the live book showed it); a sweep at
        # t=200 takes ours (trade id "ours") + 100 of theirs ("theirs")
        write_event(deltas=[(50, "no", 1, 300), (100, "no", 1, 1000),
                            (200, "no", 1, -1100)],
                    trades=[(200, "yes", 99, 1000, "ours"), (200, "yes", 99, 100, "theirs")])

    def test_no_logs_is_noop_same_objects(self):
        a = scoring.load_event_stream(EVENT, OPEN, S)
        b = scoring.load_event_stream(EVENT, OPEN, S, subtract_live=False)
        ba, bb = a.books_per_second(int(OPEN), int(S)), b.books_per_second(int(OPEN), int(S))
        self.assertEqual(ba, bb)
        self.assertIsNone(a.live_own)
        self.assertNotIn("live_own", a.coverage)
        self.assertEqual(len(a.trades), 2)

    def test_subtracts_books_and_trades(self):
        write_live(orders=[dict(ts=OPEN + 99.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 400)],
                   fills=[dict(ts=OPEN + 200.05, order_id="A", count=1000, price_ours=1,
                               side_ours="no", trade_id="ours")])
        es = scoring.load_event_stream(EVENT, OPEN, S)
        raw = scoring.load_event_stream(EVENT, OPEN, S, subtract_live=False)
        b = es.books_per_second(int(OPEN), int(S))[TICKER]
        r = raw.books_per_second(int(OPEN), int(S))[TICKER]
        self.assertEqual(r[150][1], {1: 1300.0})
        self.assertEqual(b[150][1], {1: 300.0})            # ours out
        self.assertEqual(b[60], r[60])                     # before ours: unchanged
        self.assertEqual(b[250][1], {1: 200.0})            # after the fill: nothing of ours
        self.assertEqual([t.count for t in es.trades], [100.0])
        self.assertEqual([t.count for t in es.own_trades], [1000.0])
        self.assertEqual(es.coverage["live_own"]["trades_removed"], 1)
        self.assertEqual(es.book_at(TICKER, OPEN + 150.5)[1], {1: 300.0})
        self.assertEqual(raw.book_at(TICKER, OPEN + 150.5)[1], {1: 1300.0})

    def test_clips_at_zero(self):
        write_live(orders=[dict(ts=OPEN + 90, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 400)])
        es = scoring.load_event_stream(EVENT, OPEN, S)
        b = es.books_per_second(int(OPEN), int(S))[TICKER]
        self.assertEqual(b[95][1], {})                     # 300 recorded - 1000 ours -> 0

    def test_env_switch(self):
        write_live(orders=[dict(ts=OPEN + 99.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 400)])
        old = scoring.SUBTRACT_LIVE
        try:
            scoring.SUBTRACT_LIVE = False
            es = scoring.load_event_stream(EVENT, OPEN, S)
            self.assertEqual(es.books_per_second(int(OPEN), int(S))[TICKER][150][1], {1: 1300.0})
        finally:
            scoring.SUBTRACT_LIVE = old


# --------------------------------------------------------------------------
# 2. the swp fill model
# --------------------------------------------------------------------------

class TestSweepModel(Base):

    def test_vpass_reproduces_sim_pass(self):
        write_event(deltas=[(300, "no", 2, 50), (900, "yes", 98, -200)],
                    trades=[(400, "no", 98, 120, "a"), (800, "yes", 99, 30, "b")])
        sh = self.shared()
        for fm in ("opt", "cons"):
            a = sim._Pass(sh, G, fm).run()
            b = SW.VPass(sh, G, fm).run()
            for k in a:
                self.assertEqual(a[k], b[k], (fm, k))

    def test_recorded_sweep_clears_our_1c(self):
        # someone rests 500 NO@1 BEFORE our pad (queue ahead of us); a 200-lot
        # sweep at t=600 takes part of the level
        write_event(deltas=[(60, "no", 1, 500), (600, "no", 1, -200)],
                    trades=[(600, "yes", 99, 200, "s1")])
        sh = self.shared()
        cons = SW.VPass(sh, G, "cons").run()
        self.assertEqual(cons["filler_contracts"], 0.0)        # 500 ahead of us
        size = SW.VPass(sh, G, "swp", channels="a", rec_mode="size").run()
        self.assertEqual(size["filler_contracts"], 200.0)      # min(ours, taker size)
        front = SW.VPass(sh, G, "swp", channels="a", rec_mode="front").run()
        self.assertEqual(front["filler_contracts"], 200.0)     # not cleared: we first
        level = SW.VPass(sh, G, "swp", channels="a").run()     # default 'level'
        self.assertEqual(level["filler_contracts"], 0.0)       # the 500 ahead of us take it
        allm = SW.VPass(sh, G, "swp", channels="a", rec_mode="all").run()
        self.assertEqual(allm["filler_contracts"], 1000.0)     # the whole pad
        self.assertEqual(allm["pos"][TICKER], -1000.0)
        self.assertEqual(allm["sweep_src"], {"rec": 1000.0})
        # NO bids at 1c lose: the result is YES
        self.assertAlmostEqual(allm["trading_pnl"], -10.0, places=6)

    def test_level_mode_clearing_taker_takes_all(self):
        # 40 others at NO@1, a 40-lot taker clears the recorded level: ours too
        write_event(deltas=[(30, "no", 1, 40), (600, "no", 1, -40)],
                    trades=[(600, "yes", 99, 40, "s1")])
        sh = self.shared()
        self.assertEqual([(a[3], a[4]) for a in sh.arrivals], [(40.0, True)])
        r = SW.VPass(sh, G, "swp", channels="a").run()
        self.assertEqual(r["filler_contracts"], 1000.0)
        s = SW.VPass(sh, G, "swp", channels="a", rec_mode="size").run()
        self.assertEqual(s["filler_contracts"], 40.0)

    def test_arrivals_with_our_live_prints(self):
        # live hour: our live 1000 NO@1 alone at t=130..300 swept by a 1000-lot
        # taker (our print, removed from the tape) -> no recorded arrival (the
        # hazard predicts it); at t=600 someone else rests 300 and a taker
        # takes our 500 (front of the queue) -> kept, full size 500, not cleared
        write_event(deltas=[(130, "no", 1, 1000), (300, "no", 1, -1000),
                            (550, "no", 1, 300), (560, "no", 1, 500), (600, "no", 1, -500)],
                    trades=[(300, "yes", 99, 1000, "ours1"), (600, "yes", 99, 500, "ours2")])
        write_live(orders=[dict(ts=OPEN + 129.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 3000),
                           dict(ts=OPEN + 559.8, kind="place", oid="B", side="no", price=1,
                                size=500, expiration_ts=OPEN + 3000)],
                   fills=[dict(ts=OPEN + 300.05, order_id="A", count=1000, price_ours=1,
                               side_ours="no", trade_id="ours1"),
                          dict(ts=OPEN + 600.05, order_id="B", count=500, price_ours=1,
                               side_ours="no", trade_id="ours2")])
        sh = self.shared()
        self.assertEqual(sh.trades, [])                            # both prints were ours
        self.assertEqual(len(sh.arrivals), 1)
        t0, m, side, q, cleared, q_own = sh.arrivals[0]
        self.assertEqual((round(t0 - OPEN), side, q, cleared, q_own), (600, "no", 500.0, False, 500.0))
        r = SW.VPass(sh, G, "swp", channels="a").run()
        self.assertEqual(r["filler_contracts"], 500.0)

    def test_repad_after_sweep_until_pos_cap(self):
        trades = [(t, "yes", 99, 50, f"s{t}") for t in (600, 700, 800, 900)]
        write_event(trades=trades)
        sh = self.shared()
        r = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", detail=True).run()
        # pad 1000 swept at 600, re-pad of 500 (pos_cap 1500) swept at 700,
        # then capped: no more 1c orders, no more fills
        self.assertEqual(r["filler_contracts"], 1500.0)
        self.assertEqual(r["pos"][TICKER], -1500.0)
        self.assertEqual([round(f[0] - OPEN) for f in r["fills_detail"]], [600, 700])

    def test_hazard_sweeps_fixed_seed(self):
        write_event()
        sh = self.shared()
        hz = SW.Hazard(hz_doc(rate=0.02))
        c0 = SW.hazard_candidates(sh, 0, hz.lam_max)
        self.assertEqual(c0, SW.hazard_candidates(sh, 0, hz.lam_max))     # deterministic
        self.assertNotEqual(c0, SW.hazard_candidates(sh, 1, hz.lam_max))
        r1 = SW.VPass(sh, G, "swp", hazard=hz, cands=c0, detail=True).run()
        r2 = SW.VPass(sh, G, "swp", hazard=hz, cands=c0, detail=True).run()
        self.assertEqual(r1["fills_detail"], r2["fills_detail"])
        self.assertGreater(r1["haz_events"], 0)
        self.assertEqual(r1["pos"][TICKER], -1500.0)                       # capped
        self.assertEqual(r1["sweep_src"], {"haz": 1500.0})
        cons = SW.VPass(sh, G, "cons").run()
        self.assertEqual(cons["filler_contracts"], 0.0)
        # cons rests the pad alone all hour and owns the NO side
        self.assertGreater(cons["reward"], r1["reward"])
        # zero hazard: nothing
        z = SW.Hazard(hz_doc(rate=0.02), scale=0.0)
        r0 = SW.VPass(sh, G, "swp", hazard=z, cands=c0).run()
        self.assertEqual(r0["filler_contracts"], 0.0)
        # partial takes: a 100-lot taker that does not clear the level
        hp = SW.Hazard(hz_doc(rate=0.02, cleared=False, size=100.0))
        rp = SW.VPass(sh, G, "swp", hazard=hp, cands=c0, detail=True).run()
        self.assertTrue(all(abs(q - 100.0) < 1e-9 for ts, tk, side, q, src in rp["sweep_detail"]))

    def test_hazard_only_when_uncovered(self):
        # someone else rests 400 NO@1 all hour: the tape governs, no hidden sweeps
        write_event(deltas=[(30, "no", 1, 400)])
        sh = self.shared()
        hz = SW.Hazard(hz_doc(rate=0.05))
        r = SW.VPass(sh, G, "swp", hazard=hz, cands=SW.hazard_candidates(sh, 0, hz.lam_max)).run()
        self.assertEqual(r["haz_events"], 0)
        self.assertEqual(r["filler_contracts"], 0.0)

    def test_run_swp_mean_and_common_numbers(self):
        write_event()
        sh = self.shared()
        hz = SW.Hazard(hz_doc(rate=0.01))
        m = SW.run_swp(sh, G, hz, draws=4)
        self.assertEqual(m["draws"], 4)
        self.assertEqual(len(m["draw_net"]), 4)
        self.assertAlmostEqual(m["net"], sum(m["draw_net"]) / 4, places=3)
        m2 = SW.run_swp(sh, G, hz, draws=4)
        self.assertEqual(m["draw_net"], m2["draw_net"])

    def test_posting_sweeper_crosses_our_pad(self):
        # a 300-lot YES bid at 99 appears at t=900 (it would have hit our NO@1)
        write_event(deltas=[(900, "yes", 99, 300)])
        sh = self.shared()
        cons = SW.VPass(sh, G, "cons").run()
        self.assertEqual(cons["filler_contracts"], 0.0)
        r = SW.VPass(sh, G, "swp", channels="c").run()
        self.assertEqual(r["sweep_src"], {"cross": 300.0})
        self.assertEqual(r["pos"][TICKER], -300.0)

    def test_hold_drops_pads_first_180s(self):
        write_event(trades=[(150, "yes", 99, 50, "s1"), (300, "yes", 99, 50, "s2")])
        sh = self.shared()
        g = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", detail=True).run()
        h = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", hold_pads_s=180.0,
                     detail=True).run()
        self.assertEqual(round(g["fills_detail"][0][0] - OPEN), 150)
        self.assertEqual(round(h["fills_detail"][0][0] - OPEN), 300)     # no pad at 150
        self.assertEqual(h["filler_contracts"], 1000.0)

    def test_variants_are_the_live_g_with_and_without_the_hold(self):
        # merge into temp-integrate (10/16): G = VARIANTS G0_nohold, G_hold180
        # = G_decided_only (the policy's pad_hold_open_s, what runs live)
        GH, hold = SW.resolve_variant(SW.SWEEP_VARIANTS["G_hold180"])
        self.assertIsNone(hold)
        self.assertEqual(GH, pol.make_policy("G_decided_only"))
        self.assertEqual(G, pol.make_policy("G0_nohold"))
        self.assertEqual(G["pad_hold_open_s"], 0.0)

    def test_policy_pad_hold_through_vpass(self):
        # sim._Pass passes the policy's pad hold (pad_hold_open_s) to
        # _desired since SIM_VERSION 5: VPass must take it and keep reproducing
        # the sim's pass, and the hold must drop the pads exactly like the
        # decision-time hold (hold_pads_s) on this event
        write_event(trades=[(150, "yes", 99, 50, "s1"), (300, "yes", 99, 50, "s2")])
        sh = self.shared()
        GH = SW.resolve_variant(SW.SWEEP_VARIANTS["G_hold180"])[0]
        for fm in ("opt", "cons"):
            a = sim._Pass(sh, GH, fm).run()
            b = SW.VPass(sh, GH, fm).run()
            for k in a:
                self.assertEqual(a[k], b[k], (fm, k))
        p = SW.VPass(sh, GH, "swp", channels="a", rec_mode="all", detail=True).run()
        h = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", hold_pads_s=180.0,
                     detail=True).run()
        self.assertEqual(round(p["fills_detail"][0][0] - OPEN), 300)     # no pad at 150
        self.assertEqual(p["filler_contracts"], 1000.0)
        for k in h:
            self.assertEqual(p[k], h[k], k)


# --------------------------------------------------------------------------
# 3. hazard fit, calibration and validation helpers
# --------------------------------------------------------------------------

class TestCalibrationValidation(Base):

    def test_fit_hazard_recovers_rates(self):
        rows = [{"expo": {"joint": {">0.995": {"10-20|0-5s": 10000.0, "10-20|600s+": 10000.0},
                                    "0.97-0.99": {"10-20|600s+": 20000.0}}}}]
        cl = ([dict(fb=">0.995", mb="10-20", ab="0-5s", posting=False)] * 1000
              + [dict(fb=">0.995", mb="10-20", ab="600s+", posting=False)] * 50
              + [dict(fb=">0.995", mb="10-20", ab="600s+", posting=True)] * 7
              + [dict(fb="0.97-0.99", mb="10-20", ab="600s+", posting=False)] * 40)
        hz = SW.fit_hazard(rows, cl)
        self.assertAlmostEqual(hz["a"][">0.995"]["0-5s"] * hz["mult"]["10-20"], 0.1, delta=0.03)
        self.assertAlmostEqual(hz["a"][">0.995"]["600s+"] * hz["mult"]["10-20"], 0.005, delta=0.001)
        self.assertAlmostEqual(hz["a"]["0.97-0.99"]["600s+"] * hz["mult"]["10-20"], 0.002,
                               delta=0.0005)
        h = SW.Hazard({"hazard": hz, "sizes": {}, "sizes_all": [[10, 1]]})
        self.assertGreater(h.rate(0.999, 15.0, 1.0), h.rate(0.999, 15.0, 900.0))
        self.assertEqual(h.rate(None, 15.0, 1.0), 0.0)

    def test_calibrate_event_finds_sweep(self):
        write_event(deltas=[(300, "no", 1, 40), (310, "no", 1, -40), (311, "yes", 99, 500)],
                    trades=[(310, "yes", 99, 40, "s1")])
        row = SW.calibrate_event(EVENT, model=ConstModel(0.985))
        self.assertEqual(len(row["clusters"]), 1)
        c = row["clusters"][0]
        self.assertEqual((c["side"], c["q"], c["L"], c["cleared"]), ("no", 40.0, 40.0, True))
        self.assertTrue(c["posting"])                     # a 99 bid 1 s later
        self.assertEqual(c["fb"], "0.97-0.99")
        e = sum(sum(d.values()) for d in row["expo"]["fav_min"].values())
        self.assertAlmostEqual(e, 10.0, delta=1.0)        # 1c rested ~10 s

    def test_live_actuals_and_measure(self):
        write_event(deltas=[(130, "no", 1, 1000), (500, "no", 1, -1000)],
                    trades=[(500, "yes", 99, 1000, "ours")])
        write_live(orders=[dict(ts=OPEN + 129.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 430, tag="pad"),
                           dict(ts=OPEN + 420, kind="renew", oid="A", side="no", price=1,
                                size=1000, total=1000, expiration_ts=OPEN + 800)],
                   fills=[dict(ts=OPEN + 500.05, order_id="A", count=1000, price_ours=1,
                               side_ours="no", trade_id="ours")])
        act = SW.live_actuals(EVENT, model=ConstModel(0.985))
        self.assertEqual(act["filler_contracts"], 1000.0)
        self.assertEqual(act["pos"][TICKER], -1000.0)
        self.assertAlmostEqual(act["trading_pnl"], -10.0, places=6)
        # reward: our 1000 NO@1 is the whole NO side; YES side 1500 others
        sh = SW.shared_for(EVENT, ConstModel(0.985), subtract=False)
        pr = sh.progs[0]
        frac = scoring.score(BOOK_Y, {}, {}, {1: 1000.0}, pr["target"], pr["df"])[0]
        secs = sum(1 for i in range(sh.n) if pr["start"] <= sh.sec0 + i + 0.5 < pr["end"]
                   and 130 <= sh.sec0 + i + 0.5 - OPEN < 500)
        self.assertAlmostEqual(act["reward"], pr["pool"] * frac * secs, delta=pr["pool"] * 2)
        meas = SW.live_measure(EVENT, model=ConstModel(0.985))
        self.assertEqual(len(meas["clusters"]), 1)
        self.assertEqual(meas["clusters"][0]["cov"], "alone")
        self.assertAlmostEqual(meas["orders_1c"][0]["first_fill_s"], 370.25, places=2)

    def test_validate_event_runs(self):
        write_event(deltas=[(130, "no", 1, 1000), (500, "no", 1, -1000)],
                    trades=[(500, "yes", 99, 1000, "ours")])
        write_live(orders=[dict(ts=OPEN + 129.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 3000, tag="pad")],
                   fills=[dict(ts=OPEN + 500.05, order_id="A", count=1000, price_ours=1,
                               side_ours="no", trade_id="ours")])
        v = SW.validate_event(EVENT, draws=2, hazard=SW.Hazard(hz_doc(rate=0.0)),
                              model=ConstModel(0.985))
        # with our trade out of the tape and no hazard, nothing sweeps the sim's pad
        self.assertEqual(v["predicted"]["swp"]["filler_contracts"], 0.0)
        self.assertEqual(v["actual"]["filler_contracts"], 1000.0)
        self.assertIn("swp", SW.format_validation(v))
        self.assertEqual(SW._cum_by_5min([(OPEN + 400, TICKER, "no", 1, 10.0)], OPEN)[5], 10.0)


# --------------------------------------------------------------------------
# 4. review fixes (2026-10-16 final pass)
# --------------------------------------------------------------------------

class TestQueueAhead(Base):
    """Review finding 6: a non-clearing recorded taker reaches our 1c only
    after the external size that rested there before our lot."""

    def test_others_behind_us_fill_after(self):
        # 100 NO@1 before our pad (ahead), 400 join after it (behind); a
        # 300-lot taker does not clear the 500 level
        write_event(deltas=[(60, "no", 1, 100), (300, "no", 1, 400), (600, "no", 1, -300)],
                    trades=[(600, "yes", 99, 300, "s1")])
        sh = self.shared()
        self.assertEqual([(a[3], a[4]) for a in sh.arrivals], [(300.0, False)])
        lv = SW.VPass(sh, G, "swp", channels="a").run()
        self.assertEqual(lv["filler_contracts"], 200.0)        # 300 - 100 ahead
        fr = SW.VPass(sh, G, "swp", channels="a", rec_mode="front").run()
        self.assertEqual(fr["filler_contracts"], 300.0)

    def test_queue_ahead_decays_with_the_level(self):
        # 100 ahead of our pad; 80 of them cancel at t=400 (no trade then),
        # 300 others join behind at t=450; a 200-lot taker at t=600
        write_event(deltas=[(60, "no", 1, 100), (400, "no", 1, -80), (450, "no", 1, 300),
                            (600, "no", 1, -200)],
                    trades=[(600, "yes", 99, 200, "s1")])
        sh = self.shared()
        lv = SW.VPass(sh, G, "swp", channels="a").run()
        self.assertEqual(lv["filler_contracts"], 180.0)        # ahead clamped to 20


class TestHazardRuns(Base):
    """Review findings 3 / 7 (--hazard-scale ignored) and 8 (the run cache
    ignored the hazard)."""

    VAR = {"G": SW.SWEEP_VARIANTS["G"]}

    def setUp(self):
        super().setUp()
        write_event(trades=[(600, "yes", 99, 50, "s1")])
        self.out = os.path.join(C.DATA_ROOT, "sweep_out")
        self.calls = []
        real = SW.run_event_variants

        def spy(ev, variants=None, draws=1, hazard=None, **kw):
            self.calls.append((hazard.scale, hazard.kind, round(hazard.lam_max, 9)))
            return real(ev, variants, draws, hazard, model=ConstModel(), **kw)
        self._real = real
        SW.run_event_variants = spy

    def tearDown(self):
        SW.run_event_variants = self._real

    def run_ev(self, hz, **kw):
        return SW.run_events([EVENT], self.out, self.VAR, draws=1, hazard=hz, workers=1,
                             tail=SW.TailCal({}), **kw)

    def test_scale_reaches_the_worker(self):
        base = SW.Hazard(hz_doc(rate=0.02))
        half = SW.Hazard(hz_doc(rate=0.02), scale=0.5)
        r = self.run_ev(half)[0]
        self.assertEqual(self.calls[-1][0], 0.5)
        self.assertAlmostEqual(self.calls[-1][2], base.lam_max * 0.5, places=9)
        self.assertEqual(r["swp"]["hazard_scale"], 0.5)
        self.assertAlmostEqual(r["swp"]["lam_max"], round(base.lam_max * 0.5, 6), places=6)
        self.assertEqual(r["swp"]["hazard_id"], half.ident())
        self.assertNotEqual(half.ident(), base.ident())

    def test_cache_is_keyed_by_the_hazard(self):
        a = SW.Hazard(hz_doc(rate=0.02))
        self.run_ev(a)
        self.assertEqual(len(self.calls), 1)
        self.run_ev(a)                                       # same inputs: reused
        self.assertEqual(len(self.calls), 1)
        self.run_ev(SW.Hazard(hz_doc(rate=0.02), scale=1.5))  # scale -> recompute
        self.assertEqual(len(self.calls), 2)
        recal = hz_doc(rate=0.2)
        recal["built"] = "test"                              # same stamp, other rates
        self.run_ev(SW.Hazard(recal))
        self.assertEqual(len(self.calls), 3)
        self.run_ev(SW.Hazard(recal), join=(800.0, 5.0))      # joiners -> recompute
        self.assertEqual(len(self.calls), 4)
        self.run_ev(SW.Hazard(recal), tag="t1")               # a tag writes elsewhere
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(os.path.isdir(SW.runs_dir(self.out, "t1")))
        self.assertEqual(len(SW.load_runs(self.out)), 1)
        self.assertEqual(len(SW.load_runs(self.out, tag="t1")), 1)
        self.assertEqual(SW.sens_tags(self.out), ["t1"])

    def test_cache_is_keyed_by_the_sim_version(self):
        # VPass inherits the sim's pass: a run file from another SIM_VERSION
        # (e.g. 4, before the pad hold went into _decide_all) is re-run
        a = SW.Hazard(hz_doc(rate=0.02))
        self.run_ev(a)
        self.run_ev(a)
        self.assertEqual(len(self.calls), 1)
        path = os.path.join(SW.runs_dir(self.out), C.et_day(S), f"{EVENT}.json")
        self.assertTrue(os.path.exists(path))
        doc = C.read_json(path)
        self.assertEqual(doc["sim_version"], sim.SIM_VERSION)
        doc["sim_version"] = "4"
        C.write_json_atomic(path, doc)
        self.run_ev(a)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(C.read_json(path)["sim_version"], sim.SIM_VERSION)

    def test_noflash_kind(self):
        with self.assertRaises(ValueError):
            SW.Hazard(hz_doc(rate=0.02), kind="noflash")
        d = hz_doc(rate=0.02)
        d["hazard_noflash"] = hz_doc(rate=0.01)["hazard"]
        h = SW.Hazard(d, kind="noflash")
        self.assertAlmostEqual(h.lam_max, SW.Hazard(hz_doc(rate=0.01)).lam_max)
        self.assertNotEqual(h.ident(), SW.Hazard(d).ident())
        r = self.run_ev(h)[0]
        self.assertEqual(r["swp"]["hazard_kind"], "noflash")


class TestFlashCalibration(Base):
    """Review finding 5: flash sweeps get FLASH_EXPO_S of exposure each and a
    cleared flag from the level after them only; a flash-free fit exists."""

    def test_flash_exposure_and_cleared(self):
        write_event(deltas=[(300.6, "no", 1, 40), (300.85, "no", 1, -40),
                            (600.6, "no", 1, 500), (600.85, "no", 1, -40)],
                    trades=[(300.8, "yes", 99, 40, "f1"), (600.8, "yes", 99, 40, "f2")])
        row = SW.calibrate_event(EVENT, model=ConstModel(0.985))
        cl = sorted(row["clusters"], key=lambda c: c["t"])
        self.assertEqual([(c["flash"], c["cleared"]) for c in cl], [(True, True), (True, False)])
        fe = row["expo"]["flash"]["fav_age"]["0.97-0.99"]["0-5s"]
        self.assertAlmostEqual(fe, 2 * SW.FLASH_EXPO_S)
        agg = SW.aggregate_calibration([row])
        fr = agg["recorded"]["all"]["fresh"]["0.97-0.99"]
        self.assertEqual((fr["flash"], fr["nonflash"]), (2, 0))
        self.assertIn("hazard_noflash", agg)
        self.assertGreater(agg["hazard"]["a"]["0.97-0.99"]["0-5s"],
                           agg["hazard_noflash"]["a"]["0.97-0.99"]["0-5s"])
        self.assertEqual(agg["sizes_noflash"]["0-5s"], [])
        self.assertIn("flash / non-flash", SW.format_calibration(agg))
        SW.Hazard(SW.compact_hazard(agg), kind="noflash")      # compact keeps the fit


class TestJoinersAndStrip(Base):
    """Review finding 4: joiner dilution (reward only) and the joiner-free
    replay of a live hour."""

    def test_join_dilutes_reward_only(self):
        write_event()
        sh = self.shared()
        a = SW.VPass(sh, G, "swp", channels="").run()
        b = SW.VPass(sh, G, "swp", channels="", join_q=800.0, join_s=5.0).run()
        self.assertLess(b["reward"], a["reward"])
        self.assertEqual((a["contracts"], b["contracts"]), (0.0, 0.0))
        with self.assertRaises(ValueError):
            SW.VPass(sh, G, "cons", join_q=800.0)

    def test_joiner_sides_and_strip(self):
        # our live 1000 NO@1 opens the empty level at t=130; 600 others join
        # at t=133; a taker at t=500 takes ours (live print) and 100 of theirs
        write_event(deltas=[(130, "no", 1, 1000), (133, "no", 1, 600), (500, "no", 1, -1100)],
                    trades=[(500, "yes", 99, 1000, "ours"), (500, "yes", 99, 100, "theirs")])
        write_live(orders=[dict(ts=OPEN + 129.8, kind="place", oid="A", side="no", price=1,
                                size=1000, expiration_ts=OPEN + 3000, tag="pad")],
                   fills=[dict(ts=OPEN + 500.05, order_id="A", count=1000, price_ours=1,
                               side_ours="no", trade_id="ours")])
        full = SW.shared_for(EVENT, ConstModel(0.985), subtract=False)
        js = SW.joiner_sides(full)
        self.assertEqual(list(js), [(0, "no")])
        sh = self.shared()
        self.assertEqual(sh.level1(0, "no", 200), 600.0)            # joiners stay
        self.assertEqual(len(sh.arrivals), 1)
        nj = SW.shared_for(EVENT, ConstModel(0.985), subtract=True, strip_1c=js)
        self.assertEqual(nj.level1(0, "no", 200), 0.0)
        self.assertEqual(nj.level1(0, "no", 100), sh.level1(0, "no", 100))
        self.assertEqual((len(nj.arrivals), nj.stripped_arrivals), (0, 1))


class TestStatsAndTails(Base):
    """Review finding 1: outcome uncertainty (bootstrap over hours) and the
    realized-tail calibration."""

    def test_variant_table_bootstrap(self):
        res = [{"event": f"E{i}", "settled": True, "et_day": "2026-10-15", "hour_et": i,
                "variants": {"G|swp": {"net": -10.0, "reward": 1.0, "draws": 2, "net_sd": 1.0}}}
               for i in range(10)]
        res.append({"event": "E10", "settled": True, "et_day": "2026-10-15", "hour_et": 10,
                    "variants": {"G|swp": {"net": 200.0, "reward": 1.0, "draws": 2,
                                           "net_sd": 1.0}}})
        t = SW.variant_table(res, boot_n=2000)["table"]["G|swp"]
        self.assertEqual(t["total"]["net"], 100.0)
        self.assertEqual(t["ex_best"], -100.0)
        self.assertLess(t["boot_p05"], 0.0)
        self.assertGreater(t["boot_p95"], 100.0)
        self.assertTrue(0.3 < t["p_pos"] < 0.8)            # P(the upset hour is drawn)
        self.assertAlmostEqual(t["net_se"], math.sqrt(11 * 0.5), places=2)
        self.assertIn("90% CI", SW.format_table(SW.variant_table(res, boot_n=200), ["G"]))

    def test_tail_table_and_tailcal(self):
        rows = []
        # event A: strike s1 fav 0.985 all hour, longshot WINS; s2 fav 0.999, loses
        for k in range(10):
            rows.append(("A", "A-s1", k, 0.015, 1, "t"))     # YES is the 1.5% longshot, won
            rows.append(("A", "A-s2", k, 0.9995, 1, "t"))    # NO is the 0.05% longshot
        # event B: s1 fav 0.985, longshot loses
        for k in range(10):
            rows.append(("B", "B-s1", k, 0.985, 1, "t"))     # NO is the 1.5% longshot, lost
        t = SW.tail_table(rows, boot_n=200)
        b = {(r[0], r[1]): r for r in t["buckets"]}
        self.assertEqual(b[(0.01, 0.02)][3], 0.5)             # 10 of 20 minutes
        self.assertEqual(b[(0.01, 0.02)][7], 1)               # one upset strike
        self.assertEqual(b[(0.0, 0.001)][3], 0.0)
        fc = t["first_crossing"]["0.97"]
        self.assertEqual((fc["strikes"], fc["realized"]), (3, 1))
        self.assertAlmostEqual(fc["model_expected"], 0.015 + 0.0005 + 0.015, places=4)
        tc = SW.TailCal({"buckets": [r[:6] for r in t["buckets"]]})
        self.assertEqual(tc.p(0.015), 0.5)                    # our side the longshot
        self.assertEqual(tc.p(0.985), 0.5)                    # ... the favorite: 1 - rate
        self.assertEqual(tc.p(0.5), 0.5)                      # outside: the model
        self.assertEqual(tc.p(0.9995), 1.0)

    def test_cond_table_and_price_conditioning(self):
        # strike fair YES 0.985 (NO the 1.5% longshot); sweepers buy YES at 99
        # (hit NO bids at 1c): event A 100 lots, NO lost; event B 50 lots, NO won
        pr = [("A", "A-s", 1.0, 0.985, 99, "yes", 100.0, 1, "t"),
              ("B", "B-s", 1.0, 0.985, 99, "yes", 50.0, 0, "t"),
              # a taker sells YES at 97 (hits a YES bid at 97c, our favorite); YES won
              ("A", "A-s", 2.0, 0.985, 97, "no", 10.0, 1, "t"),
              # a mid-price print: no class
              ("A", "A-s", 3.0, 0.985, 50, "no", 10.0, 1, "t")]
        cd = SW.cond_table(pr, boot_n=200)
        self.assertEqual(sorted(cd), ["c1", "c97_99"])
        r = cd["c1"][0]
        self.assertEqual((r[0], r[1]), (0.01, 0.02))
        self.assertAlmostEqual(r[3], 50.0 / 150.0, places=6)    # contract-weighted
        self.assertEqual((r[6], r[7], r[8], r[9]), (150.0, 2, 2, 1))
        self.assertAlmostEqual(r[10], 0.5)                      # per print
        self.assertEqual(cd["c97_99"][0][3], 0.0)
        self.assertEqual([SW.fill_class(x) for x in (1, 2, 3, 4, 96, 97, 99)],
                         ["c1", "c2_3", "c2_3", None, None, "c97_99", "c97_99"])
        tc = SW.TailCal({"buckets": [[0.01, 0.02, 0.015, 0.008, 0.005, 0.011]],
                         "cond": {"c1": [[0.01, 0.02, 0.015, 0.001, 0.0, 0.002]]}})
        self.assertAlmostEqual(tc.p(0.015, price=1), 0.001)      # a 1c fill: conditional
        self.assertAlmostEqual(tc.p(0.015, price=5), 0.008)      # no class: unconditional
        self.assertAlmostEqual(tc.p(0.985, price=97), 0.992)     # class without a table
        self.assertAlmostEqual(tc.p(0.015, "hi", price=1), 0.002)
        self.assertNotEqual(tc.ident(), SW.TailCal({"buckets": tc.doc["buckets"]}).ident())

    def test_vpass_pnl_cal(self):
        write_event(trades=[(600, "yes", 99, 50, "s1")])
        sh = self.shared()                                    # fair YES 0.985: NO is 1.5%
        tc = SW.TailCal({"buckets": [[0.01, 0.02, 0.015, 0.004, 0.002, 0.008]]})
        r = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", tail_cal=tc).run()
        q = r["filler_contracts"]
        self.assertGreater(q, 0)
        self.assertAlmostEqual(r["pnl_cal"], q * (0.004 - 0.01), places=4)
        self.assertAlmostEqual(r["pnl_cal_thi"], q * (0.008 - 0.01), places=4)
        self.assertAlmostEqual(r["net_cal"], r["reward"] + r["pnl_cal"], places=4)
        # with a 1c fill-conditional table, our 1c fills are marked at it
        tc2 = SW.TailCal({"buckets": tc.doc["buckets"],
                          "cond": {"c1": [[0.01, 0.02, 0.015, 0.001, 0.0, 0.003]]}})
        r2 = SW.VPass(sh, G, "swp", channels="a", rec_mode="all", tail_cal=tc2).run()
        self.assertAlmostEqual(r2["pnl_cal"], q * (0.001 - 0.01), places=4)
        self.assertAlmostEqual(r2["pnl_cal_tlo"], q * (0.0 - 0.01), places=4)
        self.assertNotIn("pnl_cal", SW.VPass(sh, G, "cons").run())


class TestNoFutureRawDirs(Base):
    """temp_shadow_common.raw_files creates the folder of every hour it
    probes; recorded_events must not walk into empty future folders (it used
    to leave one more empty raw/<day> behind per call -- in KL-data)."""

    def test_recorded_events_leaves_no_future_dirs(self):
        import datetime as _dt
        import time as _time
        write_event(trades=[(600, "yes", 99, 50, "s1")])     # a ws row inside the hour
        rd = os.path.join(C.DATA_ROOT, "raw")
        os.makedirs(os.path.join(rd, "2030-01-01"))
        self.assertEqual(SW.recorded_events(None), [EVENT])
        limit = (_dt.datetime.fromtimestamp(_time.time(), _dt.timezone.utc).date()
                 + _dt.timedelta(days=2)).isoformat()
        extra = [d for d in os.listdir(rd) if d != "2030-01-01" and d > limit]
        self.assertEqual(extra, [])


class TestLiveDirSandbox(unittest.TestCase):
    """Review finding 9: a sandboxed recording never reads the real executor
    logs; TEMP_MM_DATA (the executor's own relocation) is honoured."""

    def test_live_dir(self):
        saved = {k: os.environ.pop(k, None) for k in ("TEMP_MM_LIVE_DIR", "TEMP_MM_DATA")}
        old_default = scoring.LIVE_DIR_DEFAULT
        try:
            fake = tempfile.mkdtemp(prefix="tss_fake_live_")
            day = C.iso(OPEN)[:10]
            with open(os.path.join(fake, f"orders_{day}.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"mode": "live", "ticker": TICKER, "ts": OPEN + 5,
                                    "kind": "place", "oid": "Z", "side": "no", "price": 1,
                                    "size": 10}) + "\n")
            scoring._LIVE_CACHE.clear()
            scoring.LIVE_DIR_DEFAULT = fake            # as if KL-data\temp-mm had rows
            self.assertIsNone(scoring.live_dir())      # TEMP_SHADOW_DATA is a sandbox
            self.assertIsNone(scoring.load_live_orders(EVENT))
            os.environ["TEMP_MM_DATA"] = fake          # the executor relocated
            self.assertEqual(scoring.live_dir(), fake)
            self.assertIsNotNone(scoring.load_live_orders(EVENT))
            self.assertEqual(SW.live_events(settled_only=False), [EVENT])
            os.environ["TEMP_MM_LIVE_DIR"] = os.path.join(fake, "nope")
            self.assertEqual(scoring.live_dir(), os.path.join(fake, "nope"))
        finally:
            scoring.LIVE_DIR_DEFAULT = old_default
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            scoring._LIVE_CACHE.clear()


class TestPipelineFiles(unittest.TestCase):
    """The fit-only files the daily pipeline (temp_shadow_sim's swp) loads:
    export keeps the hazard's size draws in order, so the id -- and every
    Monte Carlo draw -- is the full calibration's."""

    def test_repo_defaults_and_export(self):
        hz_doc = C.read_json(sim.SWP_HAZARD_REPO)
        tl_doc = C.read_json(sim.SWP_TAILS_REPO)
        self.assertEqual(SW.Hazard(hz_doc).ident(), "bfb9676a4e92a9d2")     # the 10/16 pre-live fit
        self.assertEqual(len(hz_doc["events"]), 37)
        self.assertNotIn("KXTEMPMIAH-26OCT1610", hz_doc["events"])          # the live hour left out
        self.assertEqual(SW.TailCal(tl_doc).ident(), "46e671fb82353b0b")
        self.assertTrue(SW.TailCal(tl_doc).cond.get("c1"))
        # a full calibrate-shaped doc -> the fit-only file: same id, diagnostics dropped
        full = dict(hz_doc, clusters=[{"x": 1}], recorded={"all": {}}, live={"events": []})
        fit = SW.hazard_fit_doc(full)
        self.assertEqual(SW.Hazard(fit).ident(), SW.Hazard(full).ident())
        self.assertFalse({"clusters", "recorded", "live"} & set(fit))
        d = tempfile.mkdtemp(prefix="tss_fit_")
        p = os.path.join(d, "sweep_hazard.json")
        SW.write_fit_file(p, fit)
        self.assertEqual(SW.Hazard(C.read_json(p)).ident(), SW.Hazard(full).ident())
        # re-ordering the size draws changes the fit (they are indexed)
        shuf = json.loads(json.dumps(fit))
        shuf["sizes"]["0-5s"] = list(reversed(shuf["sizes"]["0-5s"]))
        self.assertNotEqual(SW.Hazard(shuf).ident(), SW.Hazard(fit).ident())
        self.assertEqual(SW.TailCal(SW.tails_fit_doc(tl_doc)).ident(), SW.TailCal(tl_doc).ident())
        with self.assertRaises(ValueError):
            SW.hazard_fit_doc({"events": []})
        # the CLI writes the same file
        q = os.path.join(d, "via_cli.json")
        self.assertEqual(SW.main(["export", "--hazard", p, "--to", q]), 0)
        self.assertEqual(SW.Hazard(C.read_json(q)).ident(), SW.Hazard(fit).ident())

    def test_sim_loads_the_data_root_override(self):
        cfg = sim.swp_config()
        self.assertEqual(cfg["ident"]["hazard_src"], sim.SWP_HAZARD_REPO)
        p = os.path.join(C.DATA_ROOT, "model", "sweep_hazard.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        doc = C.read_json(sim.SWP_HAZARD_REPO)
        doc["hazard"] = dict(doc["hazard"], mult={k: 2 * v for k, v in doc["hazard"]["mult"].items()})
        SW.write_fit_file(p, doc)
        try:
            cfg2 = sim.swp_config()
            self.assertEqual(cfg2["ident"]["hazard_src"], p)
            self.assertNotEqual(cfg2["ident"]["hazard_id"], cfg["ident"]["hazard_id"])
            self.assertAlmostEqual(cfg2["hazard"].lam_max, 2 * cfg["hazard"].lam_max)
        finally:
            os.remove(p)
        self.assertEqual(sim.swp_config()["ident"]["hazard_src"], sim.SWP_HAZARD_REPO)

if __name__ == "__main__":
    unittest.main()
