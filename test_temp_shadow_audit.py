#!/usr/bin/env python3
"""Tests for temp_shadow_audit.py (the 2026-10-17 order-level fill audit, the
favorite-level clears and the market-reaction check).

SANDBOXED: TEMP_SHADOW_DATA and TEMP_MM_LIVE_DIR point at fresh temp dirs
BEFORE any temp_shadow module is imported, so nothing here reads or writes
the live recording or the live executor's logs. Synthetic events in the live
shapes (as test_temp_shadow_sweep writes them) are replayed by the REAL
scoring / sim / sweep / audit code; a constant FairModel stand-in fixes fairs.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_shadow_audit -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tss_audit_")
os.environ["TEMP_MM_LIVE_DIR"] = tempfile.mkdtemp(prefix="tss_audit_live_")
os.environ.pop("TEMP_SHADOW_SWEEP_OUT", None)
os.environ.pop("TEMP_SHADOW_AUDIT_OUT", None)
os.environ.pop("TEMP_SHADOW_SUBTRACT_LIVE", None)

import json        # noqa: E402
import math        # noqa: E402
import shutil      # noqa: E402
import unittest    # noqa: E402

import temp_shadow_audit as A       # noqa: E402
import temp_shadow_common as C      # noqa: E402
import temp_shadow_scoring as scoring  # noqa: E402
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


class ConstSlope:
    """fair() = a constant P(YES); a tiny guard slope (buffer 1c)."""

    def __init__(self, p=0.985):
        self.p = p

    def dist(self, snap, settle_ts):
        return (80.0, 1.0) if snap is not None else None

    def fair(self, snap, settle_ts, strikes):
        return {k: self.p for k in strikes} if snap is not None else {}

    def slopes(self, snap, settle_ts, strikes):
        return {k: 0.1 for k in strikes} if snap is not None else {}


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


def write_event(event=EVENT, deltas=(), trades=(), books=None, result="yes"):
    """A one-strike fake event (test_temp_shadow_sweep's harness). deltas
    [(t, side, cents, delta)], trades [(t, taker_side, yes_cents, count,
    trade_id)]; t = seconds after open."""
    s_ = C.parse_event(event)[1]
    op = s_ - 3600.0
    tk = f"{event}-T{K:.2f}"
    by, bn = books or ({98: 500.0, 97: 1000.0}, {})
    mk = [dict(ticker=tk, event_ticker=event, floor_strike=K, strike_type="greater",
               open_time=C.iso(op), close_time=C.iso(s_), status="active", result="")]
    C.append_jsonl(C.raw_file("meta", op - 600), {"rx": op - 600, "kind": "markets",
                                                  "data": {"event_ticker": event, "markets": mk}})
    progs = [dict(id="p0", market_ticker=tk, start_date=_iso_us(op + 120.0),
                  end_date=_iso_us(op + 3600.0), period_reward=1000000,
                  target_size_fp="1000.00", discount_factor_bps=5000)]
    C.append_jsonl(C.raw_file("meta", op + 60), {"rx": op + 60, "kind": "programs",
                                                 "data": {"programs": progs}})
    ws = [(op - 700, {"rx": op - 700, "marker": "connect", "detail": "test"})]
    rows = [(op - 650, "orderbook_snapshot",
             {"market_ticker": tk,
              "yes_dollars_fp": [[_d(p), f"{q:.2f}"] for p, q in sorted(by.items())],
              "no_dollars_fp": [[_d(p), f"{q:.2f}"] for p, q in sorted(bn.items())]})]
    for t, side, c, dq in deltas:
        rows.append((op + t, "orderbook_delta", {"market_ticker": tk, "price_dollars": _d(c),
                                                 "delta_fp": f"{dq:.2f}", "side": side}))
    rows.sort(key=lambda r: r[0])
    for seq, (rx, typ, msg) in enumerate(rows, start=1):
        ws.append((rx, {"rx": rx, "m": {"type": typ, "sid": 1, "seq": seq, "msg": msg}}))
    rest = []
    for seq, (t, side, yc, cnt, tid) in enumerate(sorted(trades), start=1):
        ts = op + t
        body = {"trade_id": tid, "market_ticker": tk, "yes_price_dollars": _d(yc),
                "no_price_dollars": _d(100 - yc), "count_fp": f"{cnt:.2f}", "taker_side": side,
                "is_block_trade": False, "ts": int(math.floor(ts)), "ts_ms": int(round(ts * 1000))}
        ws.append((ts + 0.05, {"rx": ts + 0.05, "m": {"type": "trade", "sid": 2, "seq": seq,
                                                       "msg": body}}))
        rest.append(dict({k: v for k, v in body.items() if k not in ("ts", "ts_ms")},
                         ticker=tk, created_time=_iso_us(ts)))
    for rx, rec in sorted(ws, key=lambda r: r[0]):
        C.append_jsonl(C.raw_file("ws", rx), rec)
    m = op - 900
    while m < s_:
        for lag, final in ((145.0, False), (310.0, True)):
            if m + lag < s_:
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
    C.write_json_atomic(C.settled_file(event), {
        "event_ticker": event, "markets": [dict(mk[0], result=result, status="finalized",
                                                expiration_value="85.0")],
        "trades": {tk: rest}, "programs": progs})


def write_live(orders=(), fills=()):
    """Live executor logs in its shapes (orders_/fills_<UTC day>.jsonl), in
    the given (file) order."""
    root = scoring.live_dir()
    day = C.iso(OPEN)[:10]
    with open(os.path.join(root, f"orders_{day}.jsonl"), "a", encoding="utf-8") as f:
        for r in orders:
            f.write(json.dumps(dict({"mode": "live", "ticker": TICKER}, **r)) + "\n")
    with open(os.path.join(root, f"fills_{day}.jsonl"), "a", encoding="utf-8") as f:
        for r in fills:
            f.write(json.dumps(dict({"mode": "live", "ticker": TICKER}, **r)) + "\n")
    scoring._LIVE_CACHE.clear()


def hz_doc(rate=0.0, cleared=True, size=1000.0):
    return {"built": "test", "hazard": {"a": {fb: {ab: rate for ab in SW.AGE_LABELS}
                                              for fb in SW.FAV_LABELS},
                                        "mult": {mb: 1.0 for mb in SW.MIN_LABELS},
                                        "lam_pool": {fb: rate for fb in SW.FAV_LABELS}},
            "sizes": {}, "sizes_all": [[size, int(cleared)]]}


# the live hour every audit test uses: our NO 1c pad A (opened an empty
# level) swept alone at t=160 by a 1000-lot taker (our print "own1"); our YES
# favorite B at 97 behind 1,000, amended to 96 at t=300, cancelled at 400; a
# 1c qual C that expires unrenewed
def live_hour():
    write_event(deltas=[(150.6, "no", 1, 1000), (160.0, "no", 1, -1000),
                        (151.2, "yes", 97, 300), (300.1, "yes", 97, -300), (300.1, "yes", 96, 300),
                        (350.0, "yes", 96, -100), (400.1, "yes", 96, -200),
                        (170.3, "no", 1, 500), (200.0, "no", 1, -500)],
                trades=[(160.0, "yes", 99, 1000, "own1"), (250.0, "no", 97, 600, "x1"),
                        (350.0, "no", 96, 100, "x2")])
    write_live(orders=[
        dict(ts=OPEN + 150.0, kind="place", oid="A", side="no", price=1, size=1000, tag="pad",
             latency_ms=1000.0, expiration_ts=OPEN + 450),
        dict(ts=OPEN + 150.0, kind="place", oid="B", side="yes", price=97, size=300, tag="fav",
             latency_ms=50.0, expiration_ts=OPEN + 240),
        dict(ts=OPEN + 170.0, kind="place", oid="C", side="no", price=1, size=500, tag="qual",
             latency_ms=40.0, expiration_ts=OPEN + 200),
        dict(ts=OPEN + 215.0, kind="renew", oid="B", side="yes", price=97, size=300, total=300,
             latency_ms=40.0, expiration_ts=OPEN + 600),
        dict(ts=OPEN + 300.0, kind="amend", oid="B", side="yes", price=96, size=300, total=300,
             prev_price=97, latency_ms=40.0, expiration_ts=None),
        dict(ts=OPEN + 400.0, kind="cancel", oid="B"),
    ], fills=[dict(ts=OPEN + 160.05, order_id="A", count=1000, price_ours=1, side_ours="no",
                   trade_id="own1")])


class Base(unittest.TestCase):
    def setUp(self):
        _wipe()

    def shared(self, subtract=True, strip=None):
        return SW.shared_for(EVENT, ConstSlope(), subtract=subtract, strip_1c=strip)

    def plan(self, latency=True):
        sh, full = self.shared(), self.shared(subtract=False)
        return sh, A.OrderPlan(EVENT, sh, full, scoring.load_live_orders(EVENT), latency=latency)


class TestOrderPlan(Base):

    def test_lifecycle_and_effective_times(self):
        live_hour()
        sh, plan = self.plan()
        a, b, c = plan.orders["A"], plan.orders["B"], plan.orders["C"]
        self.assertAlmostEqual(a["placed"] - OPEN, 150.5, places=6)        # half its 1 s
        self.assertAlmostEqual(b["placed"] - OPEN, 151.025, places=6)      # after A's 1 s
        self.assertEqual(a["end_kind"], "filled")
        self.assertAlmostEqual(a["end"] - OPEN, 160.0, places=3)           # the tape's time
        self.assertEqual([(round(t - OPEN, 3), q, p) for t, q, p in a["fills"]],
                         [(160.0, 1000.0, 1)])
        self.assertEqual(c["end_kind"], "expired")
        self.assertAlmostEqual(c["end"] - OPEN, 200.0, places=6)
        self.assertEqual(b["end_kind"], "cancel")
        self.assertEqual([p for _, p in b["prices"]], [97, 96])
        self.assertEqual(plan.unmatched_fills, 0)
        _, raw = self.plan(latency=False)
        self.assertAlmostEqual(raw.orders["B"]["placed"] - OPEN, 150.0, places=6)
        # B rests behind the recorded 1,000 at 97 (the book before it)
        place_b = [it for it in plan.items if it[2][0][0] == "place" and it[2][0][1] == "B"][0]
        self.assertEqual(place_b[2][0][5], 1000.0)

    def test_classes(self):
        self.assertEqual(A.order_class("pad", 1), "c1")
        self.assertEqual(A.order_class("near+qual", 1), "c1")
        self.assertEqual(A.order_class("near", 1), "near")
        self.assertEqual(A.order_class("fav", 96), "fav90")
        self.assertEqual(A.order_class("fav", 79), "fav_lo")


class TestAuditPass(Base):

    def test_models_on_the_actual_orders(self):
        live_hour()
        sh, plan = self.plan()
        hz0 = SW.Hazard(hz_doc(rate=0.0))
        opt = A.run_config(sh, plan, "opt", {}, 1, hz0, None)
        cons = A.run_config(sh, plan, "cons", {}, 1, hz0, None)
        # opt: the 600-lot print at 97 fills B's 300 ahead of the queue
        self.assertEqual(opt["orders"]["B"]["filled"], 300.0)
        # cons: 1,000 ahead at 97 -> nothing there; at 96 (amend: a new queue
        # place, nobody ahead) the 100-lot print fills 100
        self.assertEqual(cons["orders"]["B"]["filled"], 100.0)
        self.assertEqual(cons["classes"]["fav90"]["contracts"], 100.0)
        self.assertAlmostEqual(cons["classes"]["fav90"]["pnl"], 100 * 0.04, places=6)
        # the pad's sweeper hit only us, alone: no recorded taker for it
        self.assertEqual(cons["orders"]["A"]["filled"], 0.0)
        swa = A.run_config(sh, plan, "swp", {"channels": "a"}, 1, hz0, None)
        self.assertEqual(swa["orders"]["A"]["filled"], 0.0)
        # a huge hidden-sweep rate takes the pad within its 9.5 s alone
        hot = SW.Hazard(hz_doc(rate=2.0))
        sw = A.run_config(sh, plan, "swp", {}, 3, hot, None)
        self.assertEqual(sw["draws"], 3)
        self.assertEqual(sw["orders"]["A"]["filled"], 1000.0)
        self.assertEqual(sw["orders"]["A"]["p_fill"], 1.0)
        for r in (opt, cons, swa, sw):
            self.assertEqual(r["attr_err"], 0.0)

    def test_audit_event_and_format(self):
        live_hour()
        doc = A.audit_event(EVENT, draws=2, hazard=SW.Hazard(hz_doc(rate=0.0)),
                            fav=SW.Hazard(hz_doc(rate=0.0)), model=ConstSlope())
        self.assertEqual(doc["actual"]["classes"]["c1"]["contracts"], 1000.0)
        self.assertAlmostEqual(doc["actual"]["pnl"], -10.0, places=6)      # NO@1 lost
        self.assertEqual(set(doc["configs"]), {k for k, *_ in A.AUDIT_CONFIGS})
        self.assertEqual(doc["joiner_sides"], {f"{TICKER}|no": 150})
        cp = doc["compensator"]["c1_alone"]["total"]
        self.assertEqual(cp["hits"], 1)
        txt = A.format_audit(doc)
        self.assertIn("CONTRACTS filled", txt)
        self.assertIn("hazard over the ACTUAL exposure", txt)

    def test_compensator_sums_lambda(self):
        live_hour()
        sh, plan = self.plan()
        r = 0.01
        cp = A.hazard_compensator(sh, plan, SW.Hazard(hz_doc(rate=r)), None)
        t = cp["c1_alone"]["total"]
        self.assertGreaterEqual(t["secs"], 9)
        self.assertAlmostEqual(t["expected"], r * t["secs"], places=3)
        self.assertEqual(t["hits"], 1)
        self.assertEqual(t["contracts"], 1000.0)


class TestClears(Base):

    def test_fav_clusters_and_tape(self):
        write_event(deltas=[(600, "yes", 98, -500), (600, "yes", 97, -1000),
                            (800, "yes", 98, 300), (900, "yes", 98, -100)],
                    trades=[(600, "no", 98, 500, "t1"), (600, "no", 97, 1000, "t2"),
                            (900, "no", 98, 100, "t3")])
        row = A.fav_clusters_event(EVENT, model=ConstSlope())
        cl = sorted(row["clusters"], key=lambda c: c["t"])
        self.assertEqual([(c["walked"], c["cleared"], c["top_cleared"], c["next_within2"], c["won"])
                          for c in cl], [(True, True, True, True, True),
                                         (False, False, False, False, True)])
        self.assertGreater(row["expo"]["0.970-0.990"], 3000)
        prints = [(EVENT, TICKER, OPEN + 10, 0.985, 98, "no", 50, 0, "backfill"),
                  (EVENT, TICKER, OPEN + 10, 0.985, 97, "no", 20, 0, "backfill"),
                  (EVENT, TICKER, OPEN + 20, 0.985, 98, "no", 5, 0, "backfill")]
        tc = A.tape_clusters(prints)
        self.assertEqual(sorted((c["walked"], c["won"]) for c in tc), [(False, False), (True, False)])
        rep = A.clears_report([EVENT], prints={"backfill": prints, "recorded": []}, workers=1,
                              model=ConstSlope())
        self.assertEqual(rep["frequency"]["0.970-0.990"]["clears"], 1)
        self.assertEqual(rep["appetite"], {"n": 1, "walked": 1, "frac": 1.0})
        kinds = {(r["bucket"], r["kind"]): r for r in rep["tape"]}
        self.assertEqual(kinds[("backfill 0.970-0.990", "walked")]["upset_rate"], 1.0)
        self.assertIn("upset rate after a favorite-bid hit", A.format_clears(rep))


class TestReaction(Base):

    def test_our_pad_draws_joiners_and_a_locker(self):
        # our pad opens the empty NO 1c level at 150; others join +300 at
        # 153 and a 99c YES bid appears at 155
        write_event(deltas=[(150.6, "no", 1, 1000), (153.0, "no", 1, 300), (155.0, "yes", 99, 50)])
        write_live(orders=[dict(ts=OPEN + 150.0, kind="place", oid="A", side="no", price=1,
                                size=1000, tag="pad", latency_ms=40.0,
                                expiration_ts=OPEN + 3000)])
        r = A.our_reactions(EVENT, model=ConstSlope())
        self.assertEqual(r["n_pads"], 1)
        self.assertEqual(r["pads"][5]["joined"], 1.0)
        self.assertEqual(r["pads"][12]["locker"], 1.0)
        self.assertEqual(r["pads"][2]["locker"], 0.0)

    def test_baseline_pad_and_join(self):
        write_event(deltas=[(500.0, "no", 1, 200), (504.0, "no", 1, 300),
                            (900.0, "yes", 98, 150), (906.0, "yes", 99, 10)])
        b = A.baseline_reactions(EVENT, model=ConstSlope())
        self.assertEqual(len(b["pads"]), 1)
        self.assertTrue(b["pads"][0][12]["joined"])
        self.assertFalse(b["pads"][0][2]["joined"])
        self.assertEqual(len(b["favs"]), 1)
        self.assertTrue(b["favs"][0][12]["ahead"])                    # the 99 above 98
        self.assertFalse(b["favs"][0][2]["ahead"])
        self.assertFalse(b["favs"][0][30]["joined"])                  # its own 150 is not a join
        self.assertFalse(b["favs"][0][30]["cancel"])


if __name__ == "__main__":
    unittest.main()
