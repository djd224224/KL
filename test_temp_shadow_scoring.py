#!/usr/bin/env python3
"""Tests for temp_shadow_scoring.py (hourly-temp shadow test, TEMP_SHADOW.md).

The data root is sandboxed BEFORE any temp_shadow import (temp_shadow_common
reads TEMP_SHADOW_DATA at import): these tests never touch the live recording.
The scorer is checked bit-for-bit against the ORIGINAL incentive_mm functions,
extracted with `ast` (incentive_mm.py is never imported).

    PYTHONIOENCODING=utf-8 python -m unittest test_temp_shadow_scoring -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tss_scoring_")

import ast            # noqa: E402
import gzip           # noqa: E402
import json           # noqa: E402
import random         # noqa: E402
import shutil         # noqa: E402
import time           # noqa: E402
import unittest       # noqa: E402

import temp_shadow_common as C    # noqa: E402
import temp_shadow_scoring as S   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
IMM_PATH = os.path.join(HERE, "incentive_mm.py")
STUDY_OB = os.path.join(
    r"C:\Users\jackd\Documents\KL-data\temp-shadow\backfill\study",
    "agentB_data", "ob", "ob_log.jsonl")

ORIG = {}
_ENV_ROOT = os.environ["TEMP_SHADOW_DATA"]
_SAVED_ROOT = []


def setUpModule():
    # A private root for this module's run, even when another test module
    # imported temp_shadow_common first: every helper reads C.DATA_ROOT at
    # call time, so swapping it isolates these tests completely.
    _SAVED_ROOT.append(C.DATA_ROOT)
    C.DATA_ROOT = tempfile.mkdtemp(prefix="tss_scoring_run_")
    assert os.path.realpath(C.DATA_ROOT).startswith(
        os.path.realpath(tempfile.gettempdir())), C.DATA_ROOT
    ORIG.update(load_original())


def tearDownModule():
    root = C.DATA_ROOT
    if _SAVED_ROOT:
        C.DATA_ROOT = _SAVED_ROOT.pop()
    if os.path.basename(root).startswith("tss_scoring_run_"):
        shutil.rmtree(root, ignore_errors=True)
    try:
        os.rmdir(_ENV_ROOT)          # only if nothing else wrote there
    except OSError:
        pass


def load_original():
    """The IMM scorer functions, exec'd from their source in an isolated
    namespace (no import of incentive_mm)."""
    with open(IMM_PATH, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    lines = src.splitlines(True)
    want = {"levels_desc", "side_reference_level", "_side_share",
            "estimate_reward_share"}
    ns = {}
    exec("from typing import Dict, List, Optional, Tuple", ns)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            seg = "".join(lines[node.lineno - 1:node.end_lineno])
            exec(compile(seg, IMM_PATH, "exec"), ns)
            found.add(node.name)
    assert found == want, want - found
    return ns


# --------------------------------------------------------------------------
# scoring equivalence
# --------------------------------------------------------------------------

def rand_size(rng):
    r = rng.random()
    if r < 0.12:
        return 200.0                      # exactly target/5
    if r < 0.24:
        return float(rng.choice([1, 5, 10, 100, 250, 500, 800, 1000]))
    if r < 0.36:
        return round(rng.uniform(0.01, 5), 2)
    return round(rng.expovariate(1 / 150.0), 2) or 0.01


def rand_book(rng):
    n = rng.choice([0, 1, 2, 3, 4, 6, 8, 12, 20, 35, 60])
    book = {p: rand_size(rng) for p in rng.sample(range(1, 100), n)}
    if book and rng.random() < 0.15:
        # make the cumulative walk hit the target exactly at some level
        tot = 0.0
        for p in sorted(book, reverse=True):
            if tot + book[p] >= 1000.0:
                book[p] = 1000.0 - tot if 1000.0 - tot > 0 else book[p]
                break
            tot += book[p]
    return book


def rand_own(rng, book):
    """List of (price, size) own orders (duplicates allowed)."""
    out = []
    for _ in range(rng.choice([0, 0, 1, 1, 2, 3, 5])):
        if book and rng.random() < 0.6:
            p = rng.choice(list(book))
        else:
            p = rng.randint(1, 99)
        out.append((p, rand_size(rng)))
    if book and rng.random() < 0.3:
        ref = S.reference_price(book)
        if ref is not None:
            out.append((ref, float(rng.choice([250, 500, 1000]))))
    if rng.random() < 0.15:
        out.append((1, float(rng.choice([300, 1000, 1200]))))
    return out


def own_dict(orders):
    d = {}
    for p, q in orders:
        d[p] = d.get(p, 0.0) + q
    return d


class TestScoringEquivalence(unittest.TestCase):

    def check(self, by, bn, oy_list, on_list, target, df):
        o = ORIG
        oy, on = own_dict(oy_list), own_dict(on_list)
        orders = ([("bid", p, q) for p, q in oy_list]
                  + [("ask", 100 - p, q) for p, q in on_list])
        yl = [[p, q] for p, q in by.items()]
        nl = [[p, q] for p, q in bn.items()]
        ofrac, osides = o["estimate_reward_share"](yl, nl, orders, target, df,
                                                   False)
        frac, counted, qy, qn = S.score(by, bn, oy, on, target, df)
        self.assertEqual(frac, ofrac, (by, bn, oy, on, target, df))
        self.assertEqual(counted, osides == 2)
        self.assertEqual(int(qy) + int(qn), osides)
        for book, own, q in ((by, oy, qy), (bn, on, qn)):
            prep = S.overlay(book, own)
            lv = sorted(prep.items(), key=lambda kv: -kv[0])
            os_, oq = o["_side_share"](lv, own, target, df)
            ms, mq = S.side_share(prep, own, target, df)
            self.assertEqual((ms, mq), (os_, oq))
            self.assertEqual(mq, q)
            self.assertEqual(
                S.reference_price(book, target),
                o["side_reference_level"](
                    o["levels_desc"]([[p, s] for p, s in book.items()]),
                    target))
            self.assertEqual(
                S.reference_price(prep, target),
                o["side_reference_level"](lv, target))
        return counted, frac

    def test_random_books(self):
        rng = random.Random(20261014)
        n = 6000
        counted = positive = 0
        for i in range(n):
            by, bn = rand_book(rng), rand_book(rng)
            target = 1000.0 if rng.random() < 0.8 else rng.choice(
                [500.0, 250.0, 1.0, 0.0, -5.0, 1e6, 333.3])
            df = 0.5 if rng.random() < 0.8 else rng.choice([0.25, 0.9, 1.0])
            c, f = self.check(by, bn, rand_own(rng, by), rand_own(rng, bn),
                              target, df)
            counted += c
            positive += f > 0
        # the generator must exercise both outcomes
        self.assertGreater(counted, 500)
        self.assertGreater(positive, 300)
        self.assertLess(counted, n)

    def test_study_books(self):
        if not os.path.exists(STUDY_OB):
            self.skipTest("study orderbook log not present")
        rng = random.Random(7)
        n = 0
        with open(STUDY_OB, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("kind") != "orderbook":
                    continue
                by, bn = S.book_from_rest(r["data"])
                n += 1
                self.check(by, bn, [], [], 1000.0, 0.5)
                for Q in (250.0, 500.0, 1000.0):
                    oy, on = [], []
                    ry, rn = S.reference_price(by), S.reference_price(bn)
                    if ry is not None and not S.would_cross(by, bn, "yes", ry):
                        oy.append((ry, Q))
                    if rn is not None and not S.would_cross(by, bn, "no", rn):
                        on.append((rn, Q))
                    oy.append((1, 1000.0))
                    on.append((1, 1000.0))
                    self.check(by, bn, oy, on, 1000.0, 0.5)
                for _ in range(4):
                    self.check(by, bn, rand_own(rng, by), rand_own(rng, bn),
                               1000.0, 0.5)
        self.assertEqual(n, 590)

    def test_known_values(self):
        # YES side: 300 @ 50 (ref = 50 since 300 >= 200), 800 @ 48 -> qualifies
        by = {50: 300.0, 48: 800.0}
        bn = {45: 1000.0}
        frac, counted, qy, qn = S.score(by, bn, {48: 100.0}, {45: 100.0})
        # yes: levels incl own: 50:300, 48:900; total = 300 + .25*900 = 525
        # ours = .25*100 = 25 -> 25/525; no: 45:1100 (counts in full) ->
        # 100/1100
        self.assertTrue(counted and qy and qn)
        self.assertAlmostEqual(frac, (25 / 525 + 100 / 1100) / 2, places=12)
        # thin NO side: excluded, pays nobody
        self.assertEqual(S.score(by, {45: 999.0}), (0.0, False, True, False))
        self.assertIsNone(S.reference_price({40: 199.0}))
        self.assertEqual(S.reference_price({40: 199.0, 39: 1.0}), 39)
        self.assertEqual(S.share(by, bn, {48: 100.0}, {45: 100.0}), frac)

    def test_inputs_not_mutated(self):
        by, bn = {50: 300.0, 48: 800.0}, {45: 1000.0}
        oy, on = {48: 100.0}, {45: 100.0, 1: 5.0}
        S.score(by, bn, oy, on)
        self.assertEqual(by, {50: 300.0, 48: 800.0})
        self.assertEqual(bn, {45: 1000.0})
        self.assertEqual(on, {45: 100.0, 1: 5.0})

    def test_would_cross(self):
        by, bn = {40: 10.0, 30: 5.0}, {55: 3.0, 20: 1.0}
        self.assertFalse(S.would_cross(by, bn, "yes", 44))
        self.assertTrue(S.would_cross(by, bn, "yes", 45))
        self.assertFalse(S.would_cross(by, bn, "no", 59))
        self.assertTrue(S.would_cross(by, bn, "no", 60))
        self.assertTrue(S.would_cross(by, bn, "no_bid", 61))
        self.assertFalse(S.would_cross({}, {}, "yes", 99))
        # a YES bid at 99 locks the NO side at 1
        self.assertTrue(S.would_cross({99: 5.0}, {}, "no", 1))
        # zero-size levels are not a best price
        self.assertFalse(S.would_cross(by, {70: 0.0}, "yes", 40))
        with self.assertRaises(ValueError):
            S.would_cross(by, bn, "bid", 40)


# --------------------------------------------------------------------------
# event stream: synthetic recordings
# --------------------------------------------------------------------------

EVT = "KXTEMPMIAH-26OCT1409"
A, B, CC = EVT + "-T81.99", EVT + "-T82.99", EVT + "-T80.99"
OTHER = "KXTEMPMIAH-26OCT1410-T81.99"
SETTLE = C.parse_event(EVT)[1]
T = SETTLE - 3600.0          # open


def d2(c):
    return f"{c / 100:.4f}"


def snap(tk, yes, no, sid=1, seq=None, fmt="fp"):
    if fmt == "fp":
        body = {"market_ticker": tk,
                "yes_dollars_fp": [[d2(p), f"{q:.2f}"] for p, q in yes.items()],
                "no_dollars_fp": [[d2(p), f"{q:.2f}"] for p, q in no.items()]}
    else:
        body = {"market_ticker": tk, "yes": [[p, q] for p, q in yes.items()],
                "no": [[p, q] for p, q in no.items()]}
    m = {"type": "orderbook_snapshot", "sid": sid, "msg": body}
    if seq is not None:
        m["seq"] = seq
    return m


def delta(tk, side, p, d, sid=1, seq=None, fmt="fp"):
    if fmt == "fp":
        body = {"market_ticker": tk, "price_dollars": d2(p),
                "delta_fp": f"{d:.2f}", "side": side}
    else:
        body = {"market_ticker": tk, "price": p, "delta": d, "side": side}
    m = {"type": "orderbook_delta", "sid": sid, "msg": body}
    if seq is not None:
        m["seq"] = seq
    return m


def ws_trade(tk, side, yc, cnt, ts, tid=None, fmt="a"):
    if fmt == "a":
        body = {"market_ticker": tk, "yes_price": yc, "no_price": 100 - yc,
                "count": cnt, "taker_side": side, "ts": ts}
    else:
        body = {"market_ticker": tk, "yes_price_dollars": d2(yc),
                "no_price_dollars": d2(100 - yc), "count_fp": f"{cnt:.2f}",
                "taker_side": side, "ts": ts}
    if tid:
        body["trade_id"] = tid
    return {"type": "trade", "sid": 9, "msg": body}


def rest_trade(tk, side, yc, cnt, ts, tid, block=False):
    us = C.iso(int(ts))[:-1] + f".{int(round((ts % 1) * 1e6)):06d}Z"
    return {"count_fp": f"{cnt:.2f}", "created_time": us,
            "is_block_trade": block, "no_price_dollars": d2(100 - yc),
            "taker_book_side": "bid" if side == "yes" else "ask",
            "taker_outcome_side": side, "taker_side": side, "ticker": tk,
            "trade_id": tid, "yes_price_dollars": d2(yc)}


def put(rx, m=None, **marker):
    rec = {"rx": rx}
    if m is not None:
        rec["m"] = m
    rec.update(marker)
    C.append_jsonl(C.raw_file("ws", rx), rec)


def put_meta(rx, kind, data):
    C.append_jsonl(C.raw_file("meta", rx), {"rx": rx, "kind": kind,
                                            "data": data})


class StreamCase(unittest.TestCase):
    def setUp(self):
        root = os.path.realpath(C.DATA_ROOT)
        assert root.startswith(os.path.realpath(tempfile.gettempdir()))
        for sub in ("raw", "settled", "backfill"):
            shutil.rmtree(os.path.join(C.DATA_ROOT, sub), ignore_errors=True)

    def load(self, t0=T, t1=T + 60, **kw):
        return S.load_event_stream(EVT, t0, t1, **kw)


class TestBooks(StreamCase):

    def test_snapshot_deltas_and_sampling(self):
        put(T + 0.1, delta(A, "yes", 40, 999))           # before snapshot: ignored
        put(T + 0.2, snap(A, {40: 100, 39: 1e-12}, {55: 300}))
        put(T + 1.5, delta(A, "yes", 40, 50))            # exactly on boundary
        put(T + 2.5001, delta(A, "no", 55, -300))        # just after boundary
        put(T + 2.6, delta(A, "no", 54, 7))
        put(T + 3.2, delta(OTHER, "no", 54, 7))          # other event
        es = self.load(T - 2, T + 60)
        bp = es.books_per_second(int(T) - 2, int(T) + 6)
        st = bp[A]
        self.assertEqual(len(st), 8)
        self.assertIsNone(st[0])
        self.assertIsNone(st[1])
        self.assertEqual(st[2], ({40: 100.0}, {55: 300.0}))   # tiny level dropped
        self.assertEqual(st[3], ({40: 150.0}, {55: 300.0}))   # rx == sec+0.5 in
        self.assertEqual(st[4], ({40: 150.0}, {55: 300.0}))   # +0.0001 not yet
        self.assertEqual(st[5], ({40: 150.0}, {54: 7.0}))
        # copy-on-write: unchanged seconds share objects, old ones untouched
        self.assertIs(st[3], st[4])
        self.assertIs(st[5], st[6])
        self.assertIs(st[5][0], st[4][0])      # YES side unchanged since sec 3
        self.assertIsNot(st[5][1], st[4][1])
        self.assertEqual(st[2][0], {40: 100.0})
        self.assertEqual(es.tickers, [A])
        self.assertNotIn(OTHER, bp)
        # replays are repeatable (op dicts are never mutated)
        self.assertEqual(es.books_per_second(int(T) - 2, int(T) + 6)[A], st)
        self.assertEqual(es.book_at(A, T + 2.55), ({40: 150.0}, {}))

    def test_legacy_formats_and_level_removal(self):
        put(T + 0.2, snap(A, {40: 100, 30: 5}, {55: 300}, fmt="legacy"))
        put(T + 1.2, delta(A, "yes", 30, -5, fmt="legacy"))
        put(T + 1.3, delta(A, "yes", 1, 12.5, fmt="legacy"))
        put(T + 2.2, delta(A, "no", 55, -299.9999999999, fmt="legacy"))
        es = self.load()
        st = es.books_per_second(int(T), int(T) + 3)[A]
        self.assertEqual(st[0], ({40: 100.0, 30: 5.0}, {55: 300.0}))
        self.assertEqual(st[1], ({40: 100.0, 1: 12.5}, {55: 300.0}))
        self.assertEqual(st[2][1], {})          # size <= 1e-9 dropped

    def test_snapshot_from_lookback(self):
        put(T - 600, snap(A, {40: 100}, {55: 300}))
        put(T - 300, delta(A, "yes", 41, 10))
        es = self.load()
        st = es.books_per_second(int(T), int(T) + 2)[A]
        self.assertEqual(st[0], ({40: 100.0, 41: 10.0}, {55: 300.0}))
        self.assertEqual(es.coverage["trusted_frac"][A], 1.0)
        es2 = self.load(lookback=400)            # snapshot out of reach,
        self.assertIn(A, es2.tickers)            # the later delta is not
        self.assertIsNone(es2.books_per_second(int(T), int(T) + 2)[A][0])
        self.assertEqual(es2.coverage["trusted_frac"][A], 0.0)

    def test_gap_marker_named_sid_and_unspecified(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1))
        put(T + 0.3, snap(B, {20: 10}, {70: 30}, sid=2))
        put(T + 0.4, snap(OTHER, {20: 10}, {70: 30}, sid=7))
        put(T + 5.0, marker="gap", detail="seq", markets=[A])
        put(T + 6.0, delta(A, "yes", 40, 5))     # ignored while untrusted
        put(T + 8.0, snap(A, {41: 1}, {55: 1}, sid=1))
        put(T + 10.0, marker="gap", sid=2, detail="sid 2 seq 4->6")
        put(T + 12.0, snap(B, {21: 1}, {70: 1}, sid=2))
        put(T + 14.0, marker="gap", sid=7)       # not one of ours
        put(T + 20.0, marker="gap", detail="unspecified")
        put(T + 22.0, snap(B, {22: 1}, {70: 1}, sid=2))
        es = self.load()
        bp = es.books_per_second(int(T), int(T) + 30)
        a, b = bp[A], bp[B]
        self.assertIsNotNone(a[4])
        self.assertIsNone(a[5])
        self.assertIsNone(a[6])
        self.assertIsNone(a[7])
        self.assertEqual(a[8], ({41: 1.0}, {55: 1.0}))
        self.assertIsNotNone(b[5])               # A's gap leaves B alone
        self.assertIsNone(b[10])
        self.assertIsNone(b[11])
        self.assertIsNotNone(b[12])
        self.assertIsNotNone(a[14])              # sid 7 gap: no effect
        self.assertIsNotNone(b[14])
        self.assertIsNone(a[20])                 # unspecified: all
        self.assertIsNone(a[29])
        self.assertIsNone(b[21])
        self.assertIsNotNone(b[22])
        self.assertEqual(es.coverage["gaps"], 3)

    def test_disconnect_connect_and_coverage(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1))
        put(T + 0.3, snap(B, {20: 10}, {70: 30}, sid=1))
        put(T + 10.0, marker="disconnect", detail="timeout")
        put(T + 12.0, marker="connect", detail="ok")
        put(T + 12.8, snap(A, {40: 100}, {55: 300}, sid=1))
        put(T + 15.0, snap(B, {20: 10}, {70: 30}, sid=1))
        es = self.load()
        bp = es.books_per_second(int(T), int(T) + 60)
        self.assertIsNotNone(bp[A][9])
        self.assertIsNone(bp[A][10])
        self.assertIsNone(bp[A][11])
        self.assertIsNone(bp[A][12])             # snapshot at 12.8 > 12.5
        self.assertIsNotNone(bp[A][13])
        cov = es.coverage
        # A trusted secs 0..9 and 13..59; B 0..9 and 15..59
        self.assertAlmostEqual(cov["trusted_frac"][A], (10 + 47) / 60, 5)
        self.assertAlmostEqual(cov["trusted_frac"][B], (10 + 45) / 60, 5)
        self.assertEqual(cov["reconnects"], 1)
        self.assertEqual(cov["disconnects"], 1)
        self.assertAlmostEqual(cov["ws_down_secs"], 2.0)
        self.assertAlmostEqual(cov["book_trusted_frac"],
                               ((10 + 47) / 60 + (10 + 45) / 60) / 2, 5)

    def test_silent_death_retro_untrust(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1))
        put(T + 20.0, delta(OTHER, "yes", 3, 1, sid=7))  # last sign of life
        put(T + 30.0, marker="connect", detail="new process")
        put(T + 31.0, snap(A, {40: 100}, {55: 300}, sid=1))
        es = self.load()
        a = es.books_per_second(int(T), int(T) + 40)[A]
        self.assertIsNotNone(a[19])
        self.assertIsNone(a[20])                 # dead from T+20 (+1e-6)
        self.assertIsNone(a[30])
        self.assertIsNotNone(a[31])
        self.assertEqual(es.coverage["silent_deaths"], 1)
        self.assertEqual(es.coverage["reconnects"], 1)
        (a0, b0), = es.ws_down_spans()
        self.assertAlmostEqual(a0, T + 20.0, 4)
        self.assertEqual(b0, T + 30.0)

    def beats(self, pid, start, stop, step=60.0, connected=True, age=0.4,
              started=None, ws=True):
        """meta status rows shaped like temp_shadow_collector.status()."""
        r = start
        if started is None:
            started = C.iso(start - 1)
        while r < stop:
            d = {"pid": pid, "started": started, "rx": r,
                 "connected": connected}
            if ws:
                d["ws"] = {"connected": connected, "last_msg_age_s": age}
            put_meta(r, "status", d)
            r += step

    def test_heartbeats_extend_trust_before_a_crash(self):
        # old recorder (pid 100) alive and quiet until its T+120 heartbeat,
        # dead by T+180; new one (pid 200) from T+400, connects at T+401
        self.beats(100, T - 600, T + 121)
        self.beats(200, T + 400, T + 700)
        put(T - 610, marker="connect", detail="c1")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        put(T + 401.0, marker="connect", detail="c2")
        put(T + 402.0, snap(A, {41: 100}, {55: 300}))
        es = self.load(T, T + 600)
        a = es.books_per_second(int(T), int(T) + 600)[A]
        self.assertIsNotNone(a[117])             # heartbeat T+120 - 2 s
        self.assertIsNone(a[118])
        self.assertIsNone(a[401])
        self.assertEqual(a[402], ({41: 100.0}, {55: 300.0}))
        self.assertEqual(es.coverage["silent_deaths"], 1)
        self.assertEqual(es.coverage["recorder_gaps"], 1)
        self.assertAlmostEqual(es.coverage["trusted_frac"][A],
                               (118 + 198) / 600, 5)

    def busy_book_until(self, stop, step=0.5):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        i = 1
        while T + 0.2 + i * step < stop:
            put(T + 0.2 + i * step, delta(A, "yes", 40, 1))
            i += 1
        return T + 0.2 + (i - 1) * step          # last row

    def test_other_process_heartbeats_do_not_extend_trust(self):
        # review finding: A (pid 1) busy then killed silently ~T+599.7;
        # B (pid 2) starts, heartbeats 10 min, never connects, exits;
        # C (pid 3) connects at T+1240.6. B's heartbeats must not vouch
        # for A's frozen book.
        put(T - 610, marker="connect", detail="A")
        self.beats(1, T - 600, T + 600)           # last one T+540
        last = self.busy_book_until(T + 600)
        self.beats(2, T + 620, T + 1220, connected=False, age=None)
        self.beats(3, T + 1239, T + 1800, age=None)
        put(T + 1240.6, marker="connect", detail="C")
        put(T + 1241.0, snap(A, {41: 100}, {55: 300}))
        es = self.load(T, T + 1800)
        a = es.books_per_second(int(T), int(T) + 1800)[A]
        self.assertIsNotNone(a[598])
        self.assertTrue(all(x is None for x in a[600:1241]))
        self.assertIsNotNone(a[1241])
        (a0, b0), = es.ws_down_spans()
        self.assertAlmostEqual(a0, last, 4)
        self.assertAlmostEqual(b0, T + 1240.6, 4)
        self.assertEqual(es.coverage["silent_deaths"], 1)

    def test_reused_pid_is_another_process(self):
        # Windows reuses pids: same pid, later `started` = a new process
        # (B's pre-connect heartbeat says connected, as in the collector's
        # set-flag-then-write-marker window: identity alone must reject it)
        put(T - 610, marker="connect", detail="A")
        self.beats(7, T - 590, T + 31, started="2026-10-14T11:49:00Z")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        put(T + 30.0, delta(OTHER, "yes", 3, 1, sid=7))   # A's last row
        self.beats(7, T + 100, T + 600, started="2026-10-14T12:01:39Z")
        put(T + 105.0, marker="connect", detail="B")
        es = self.load(T, T + 400)
        a = es.books_per_second(int(T), int(T) + 400)[A]
        self.assertIsNotNone(a[29])
        self.assertTrue(all(x is None for x in a[30:400]))

    def test_heartbeat_extension_bounded_by_link_evidence(self):
        # the same process keeps heartbeating "connected" on a quiet link:
        # trust extends only to hb - min(last_msg_age_s, 35 s) (its last
        # text frame, or the 30 s stale-timer bound), never past hb - 2 s
        cases = (
            (0.4, True, 118),     # busy link: hb T+120 - 2 s
            (100.0, True, 85),    # quiet link: hb T+120 - 35 s
            (None, True, 85),     # age unknown: the stale-timer bound
            (10.0, True, 110),    # last text frame at hb - 10 s
            (0.4, False, 30),     # ws reported down: last row only
        )
        for age, connected, first_none in cases:
            with self.subTest(age=age, connected=connected):
                self.setUp()
                self.beats(100, T - 600, T + 1, age=0.4)
                self.beats(100, T + 60, T + 121, age=age,
                           connected=connected, started=C.iso(T - 601))
                self.beats(200, T + 400, T + 700)
                put(T - 610, marker="connect", detail="c1")
                put(T + 0.2, snap(A, {40: 100}, {55: 300}))
                put(T + 30.0, delta(OTHER, "yes", 3, 1, sid=7))  # last row
                put(T + 401.0, marker="connect", detail="c2")
                es = self.load(T, T + 450)
                a = es.books_per_second(int(T), int(T) + 450)[A]
                self.assertIsNotNone(a[first_none - 1])
                self.assertIsNone(a[first_none])

    def test_stale_timer_disconnect_untrusts_back(self):
        # review finding: the collector tears down after 30 s without a
        # frame; the books were frozen since the link died, not since the
        # marker. Untrust from rx - N - 1.5 s, never before the last row.
        last = self.busy_book_until(T + 300)       # last row T+299.7
        put(T + 330.6, marker="disconnect",
            detail="WSError: no frames for 31s")
        es = self.load(T, T + 400)
        a = es.books_per_second(int(T), int(T) + 400)[A]
        self.assertIsNotNone(a[299])               # 299.5 < 299.7
        self.assertTrue(all(x is None for x in a[300:]))
        self.assertAlmostEqual(es.coverage["ws_down_secs"], T + 400 - last, 3)
        self.assertEqual(es.coverage["disconnects"], 1)
        # rows stop well before the stale window: untrust from rx - 32.5
        self.setUp()
        self.busy_book_until(T + 250)
        put(T + 330.6, marker="disconnect",
            detail="WSError: no frames for 31s")
        es = self.load(T, T + 400)
        a = es.books_per_second(int(T), int(T) + 400)[A]
        self.assertIsNotNone(a[297])               # 297.5 < 298.1
        self.assertIsNone(a[298])
        self.assertAlmostEqual(es.coverage["ws_down_secs"], 400 - 298.1, 3)
        # an EOF disconnect is detected at once: untrust at the marker
        self.setUp()
        self.busy_book_until(T + 250)
        put(T + 330.6, marker="disconnect", detail="WSClosed: socket EOF")
        es = self.load(T, T + 400)
        a = es.books_per_second(int(T), int(T) + 400)[A]
        self.assertIsNotNone(a[330])               # 330.5 < 330.6
        self.assertIsNone(a[331])

    def test_heartbeat_gap_blacks_out_a_stall(self):
        # the process froze T+60..T+300 without dropping the socket: the
        # backlog arrives at T+299 complete, no connect marker, no snapshot
        self.beats(100, T - 600, T + 61)
        self.beats(100, T + 300, T + 700, started=C.iso(T - 601))
        put(T - 610, marker="connect", detail="c1")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        put(T + 299.0, delta(A, "yes", 40, 5))
        put(T + 299.1, delta(A, "yes", 39, 7))
        es = self.load(T, T + 600)
        a = es.books_per_second(int(T), int(T) + 600)[A]
        self.assertIsNotNone(a[59])
        self.assertTrue(all(x is None for x in a[60:302]))
        self.assertEqual(a[302], ({40: 105.0, 39: 7.0}, {55: 300.0}))
        self.assertEqual(es.coverage["recorder_gaps"], 1)
        self.assertEqual(es.coverage["silent_deaths"], 0)

    def test_heartbeats_stop_before_t1(self):
        self.beats(100, T - 570, T + 31)          # last heartbeat T+30
        put(T - 610, marker="connect", detail="c1")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        es = self.load(T, T + 600)
        a = es.books_per_second(int(T), int(T) + 600)[A]
        self.assertIsNotNone(a[29])
        self.assertTrue(all(x is None for x in a[30:]))
        self.assertAlmostEqual(es.coverage["ws_down_secs"], 570.0, 3)
        self.assertIsNone(es.book_at(A, T + 100))
        self.assertIsNotNone(es.book_at(A, T + 10))

    def test_resubscribe_marker_and_gz(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        put(T + 3.0, marker="resubscribe", markets=[A, OTHER])
        put(T + 4.0, snap(A, {41: 100}, {55: 300}))
        p = C.raw_file("ws", T)
        with open(p, "rb") as f, gzip.open(p + ".gz", "wb") as g:
            g.write(f.read())
        os.remove(p)
        es = self.load()
        a = es.books_per_second(int(T), int(T) + 6)[A]
        self.assertIsNotNone(a[2])
        self.assertIsNone(a[3])
        self.assertEqual(a[4], ({41: 100.0}, {55: 300.0}))

    def test_live_formats_from_collector_docstring(self):
        # verbatim shapes from temp_shadow_collector.py (verified 10/15)
        put(T + 0.2, {"type": "subscribed", "id": 1,
                      "msg": {"channel": "orderbook_delta", "sid": 1},
                      "sending_ts_ms": 1})
        put(T + 0.3, {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": A, "market_id": "x",
                              "yes_dollars_fp": [["0.0100", "810.00"],
                                                 ["0.0200", "6.00"]]},
                      "sending_ts_ms": 1})                 # NO side omitted
        put(T + 1.1, {"type": "orderbook_delta", "sid": 1, "seq": 2,
                      "msg": {"market_ticker": A, "market_id": "x",
                              "price_dollars": "0.9800", "delta_fp": "20.00",
                              "side": "no", "ts": "2026-10-14T12:00:01.029Z",
                              "ts_ms": int((T + 1.029) * 1000)},
                      "sending_ts_ms": 1})
        put(T + 2.4, {"type": "trade", "sid": 2, "seq": 1,
                      "msg": {"trade_id": "tid-1", "market_ticker": A,
                              "yes_price_dollars": "0.8700",
                              "no_price_dollars": "0.1300",
                              "count_fp": "2.27", "taker_side": "yes",
                              "taker_outcome_side": "yes",
                              "taker_book_side": "bid",
                              "is_block_trade": False,
                              "ts": int(T + 2), "ts_ms": int((T + 2.3) * 1000)},
                      "sending_ts_ms": 1})
        put(T + 2.5, {"type": "trade", "sid": 2, "seq": 2,
                      "msg": {"trade_id": "tid-2", "market_ticker": A,
                              "yes_price_dollars": "0.8700",
                              "count_fp": "50.00", "taker_side": "yes",
                              "is_block_trade": True, "ts": int(T + 2)}})
        es = self.load()
        st = es.books_per_second(int(T), int(T) + 2)[A]
        self.assertEqual(st[0], ({1: 810.0, 2: 6.0}, {}))
        self.assertEqual(st[1], ({1: 810.0, 2: 6.0}, {98: 20.0}))
        self.assertEqual(len(es.trades), 1)       # block trade skipped
        tr = es.trades[0]
        self.assertAlmostEqual(tr.ts, T + 2.3, 6)  # ts_ms preferred over ts
        self.assertEqual((tr.rx, tr.ticker, tr.taker_side, tr.yes_cents,
                          tr.count), (T + 2.4, A, "yes", 87, 2.27))
        self.assertEqual(es.coverage["seq_anomalies"], 0)

    def test_gap_marker_channels_as_collector_writes_them(self):
        put(T + 0.1, marker="connect", detail="wss://...")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1, seq=1))
        put(T + 0.3, snap(B, {20: 10}, {70: 30}, sid=3, seq=1))
        put(T + 0.4, ws_trade(A, "yes", 45, 1, int(T), "t1"))
        # trade-channel gap (sid 2): books stay trusted
        put(T + 3.0, marker="gap", sid=2, channel="trade", expected=4, got=6,
            detail="sid 2 (trade) expected seq 4 got 6")
        # book gap on A's sid only, channel from the detail string
        put(T + 5.0, marker="gap", detail="sid 1 (orderbook_delta) expected "
            "seq 5 got 7")
        put(T + 7.0, snap(A, {40: 100}, {55: 300}, sid=1, seq=8))
        # resubscribe-after-error gap on B's sid
        put(T + 9.0, marker="gap", detail="resubscribe orderbook_delta after "
            "add error (code 8)", resubscribe=True,
            channel="orderbook_delta", sid=3)
        put(T + 11.0, snap(B, {20: 10}, {70: 30}, sid=4, seq=1))
        es = self.load()
        bp = es.books_per_second(int(T), int(T) + 13)
        a, b = bp[A], bp[B]
        self.assertTrue(all(x is not None for x in a[0:5]))
        self.assertTrue(all(x is None for x in a[5:7]))
        self.assertTrue(all(x is not None for x in a[7:13]))
        self.assertTrue(all(x is not None for x in b[0:9]))
        self.assertTrue(all(x is None for x in b[9:11]))
        self.assertTrue(all(x is not None for x in b[11:13]))
        self.assertEqual(es.coverage["gaps"], 2)

    def test_sids_reset_on_reconnect(self):
        # connection 1: books on sid 1; connection 2: books on sid 2 and the
        # trade channel reuses sid 1 -> a sid-1 gap (no channel) is not ours
        put(T + 0.1, marker="connect", detail="c1")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1, seq=1))
        put(T + 2.0, marker="disconnect", detail="x")
        put(T + 2.5, marker="connect", detail="c2")
        put(T + 2.6, snap(A, {40: 100}, {55: 300}, sid=2, seq=1))
        tr = ws_trade(A, "yes", 45, 1, int(T + 2), "t1")
        tr["sid"] = 1                         # trade channel now owns sid 1
        put(T + 2.7, tr)
        put(T + 4.0, marker="gap", sid=1)     # no channel field

        es = self.load()
        a = es.books_per_second(int(T), int(T) + 6)[A]
        self.assertIsNone(a[2])
        self.assertIsNotNone(a[3])
        self.assertIsNotNone(a[5])

    def test_duplicate_rows_after_gzip_crash(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1, seq=1))
        for i in range(1, 6):
            put(T + i + 0.1, delta(A, "yes", 40, 10, sid=1, seq=i + 1))
        p = C.raw_file("ws", T)
        with open(p, "rb") as f, gzip.open(p + ".gz", "wb") as g:
            g.write(f.read())                 # crash before os.remove(path)
        es = self.load()
        a = es.books_per_second(int(T), int(T) + 7)[A]
        self.assertEqual(a[6], ({40: 150.0}, {55: 300.0}))
        self.assertEqual(es.coverage["dup_rows"], 6)
        self.assertEqual(es.coverage["deltas"], 5)

    def same_tick_gap_markers(self, rx):
        put(rx, marker="gap", sid=2, channel="trade", expected=4, got=6,
            detail="sid 2 (trade) expected seq 4 got 6")
        put(rx, marker="gap", sid=1, channel="orderbook_delta", expected=5,
            got=7, detail="sid 1 (orderbook_delta) expected seq 5 got 7")

    def test_same_rx_markers_are_not_duplicates(self):
        # review finding: the coarse clock gives a trade-sid gap marker and
        # the book-sid one the same rx; the second must not be dropped
        put(T + 0.1, marker="connect", detail="c")
        put(T + 0.2, snap(A, {40: 100}, {55: 300}, sid=1, seq=1))
        put(T + 0.3, ws_trade(A, "yes", 45, 1, int(T), "t1"))
        put(T + 0.4, snap(B, {20: 10}, {70: 30}, sid=1, seq=2))
        put(T + 0.4, delta(B, "yes", 20, 5, sid=1, seq=3))   # same tick too
        self.same_tick_gap_markers(T + 10.0)
        es = self.load()
        a = es.books_per_second(int(T), int(T) + 20)[A]
        self.assertIsNotNone(a[9])
        self.assertTrue(all(x is None for x in a[10:]))
        self.assertEqual(es.coverage["dup_rows"], 0)
        self.assertEqual(es.coverage["gaps"], 1)
        self.assertEqual(es.book_at(B, T + 5), ({20: 15.0}, {70: 30.0}))
        # ... while the same rows copied into .gz AND .jsonl (and appended
        # to the .gz twice by the restart's gzip step) still dedupe exactly
        p = C.raw_file("ws", T)
        with open(p, "rb") as f:
            raw = f.read()
        with gzip.open(p + ".gz", "wb") as g:
            g.write(raw)
        with open(p + ".gz", "ab") as f:
            f.write(gzip.compress(raw))
        es = self.load()
        a = es.books_per_second(int(T), int(T) + 20)[A]
        self.assertIsNotNone(a[9])
        self.assertTrue(all(x is None for x in a[10:]))
        self.assertEqual(es.coverage["dup_rows"], 2 * 7)
        self.assertEqual(es.coverage["gaps"], 1)
        self.assertEqual(es.coverage["deltas"], 1)
        self.assertEqual(es.book_at(B, T + 5), ({20: 15.0}, {70: 30.0}))

    def test_levels_stay_in_fixed_point(self):
        # review finding: 721.93 + 307.15 - 829.08 is exactly 200.00 on the
        # exchange but 199.9999999999999 in floats, which moved the
        # reference from 50 to 40 (frac 0.2917 vs 0.1262 for our overlay)
        put(T + 0.2, snap(A, {50: 721.93, 40: 800.0}, {30: 1500.0}))
        put(T + 1.1, delta(A, "yes", 50, 307.15))
        put(T + 1.2, delta(A, "yes", 50, -829.08))
        es = self.load()
        y, n = es.books_per_second(int(T), int(T) + 3)[A][2]
        self.assertEqual(y, {50: 200.0, 40: 800.0})
        self.assertEqual(S.reference_price(y), 50)
        exact = S.score({50: 200.0, 40: 800.0}, {30: 1500.0},
                        {40: 500.0}, {30: 500.0})
        self.assertEqual(S.score(y, n, {40: 500.0}, {30: 500.0}), exact)
        self.assertAlmostEqual(exact[0], 0.1262130034, 9)

    def test_random_fractional_deltas_match_decimal_replay(self):
        from decimal import Decimal
        rng = random.Random(11)
        book = {p: Decimal(rng.randint(1, 90000)) / 100 for p in range(30, 60)}
        put(T + 0.1, snap(A, {p: float(q) for p, q in book.items()}, {}))
        exact = dict(book)
        rx = T + 0.2
        for _ in range(3000):
            p = rng.randint(30, 60)
            cur = exact.get(p, Decimal(0))
            d = Decimal(rng.randint(-int(cur * 100), 50000)) / 100
            if d == 0:
                continue
            exact[p] = cur + d
            if exact[p] == 0:
                del exact[p]
            put(rx, delta(A, "yes", p, float(d)))
            rx += 0.01
        es = self.load(T, T + 60)
        y, _ = es.books_per_second(int(T), int(T) + 60)[A][59]
        self.assertEqual(y, {p: float(q) for p, q in exact.items()})

    def test_tickers_from_meta_sorted(self):
        put_meta(T - 600, "markets", {"event_ticker": EVT, "markets": [
            {"ticker": B}, {"ticker": CC}, {"ticker": A}]})
        put_meta(T - 600, "markets", {"event_ticker": "KXTEMPMIAH-26OCT1410",
                                      "markets": [{"ticker": OTHER}]})
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        es = self.load()
        self.assertEqual(es.tickers, [CC, A, B])
        bp = es.books_per_second(int(T), int(T) + 3)
        self.assertEqual(bp[CC], [None, None, None])
        self.assertEqual(es.coverage["trusted_frac"][CC], 0.0)

    def test_empty_recording(self):
        es = self.load()
        self.assertEqual(es.tickers, [])
        self.assertEqual(es.trades, [])
        self.assertEqual(es.coverage["trades_src"], "none")
        self.assertAlmostEqual(es.coverage["ws_down_secs"], 60.0)

    def test_hour_ten_markets_fast_and_light(self):
        rng = random.Random(3)
        ticks = [f"{EVT}-T{k}.99" for k in range(76, 86)]
        rows = []
        for i, tk in enumerate(ticks):
            rows.append((T - 300 + i, snap(tk, {p: 50.0 for p in range(1, 40)},
                                           {p: 50.0 for p in range(1, 40)})))
        for _ in range(30000):
            rows.append((T + rng.uniform(0, 3600),
                         delta(rng.choice(ticks), rng.choice(("yes", "no")),
                               rng.randint(1, 45), rng.choice((-20, 10, 30)))))
        rows.sort(key=lambda r: r[0])
        by_file = {}
        for rx, m in rows:
            # a writer: raw_file(create=True) makes the folder (lookups never do)
            by_file.setdefault(C.raw_file("ws", rx, create=True), []).append(
                json.dumps({"rx": rx, "m": m}, separators=(",", ":")))
        for p, lines in by_file.items():
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        t_a = time.perf_counter()
        es = self.load(T, T + 3600)
        t_b = time.perf_counter()
        bp = es.books_per_second(int(T), int(T) + 3600)
        t_c = time.perf_counter()
        self.assertEqual(len(bp), 10)
        self.assertTrue(all(len(v) == 3600 for v in bp.values()))
        distinct = {id(s) for v in bp.values() for s in v}
        self.assertLessEqual(len(distinct), 10 * 3600)
        self.assertLess(t_c - t_b, 3.0, "books_per_second too slow")
        self.assertLess(t_b - t_a, 10.0, "load too slow")
        # spot-check against a brute-force rebuild for one ticker
        tk = ticks[3]
        yes = {p: 50.0 for p in range(1, 40)}
        no = {p: 50.0 for p in range(1, 40)}
        j = 0
        mine = [r for r in rows if r[1]["msg"]["market_ticker"] == tk]
        for i in range(0, 3600, 97):
            b = int(T) + i + 0.5
            while j < len(mine) and mine[j][0] <= b:
                m = mine[j][1]
                j += 1
                if m["type"] != "orderbook_delta":
                    continue
                side = yes if m["msg"]["side"] == "yes" else no
                p = C.dollars_to_cents(m["msg"]["price_dollars"])
                q = side.get(p, 0.0) + float(m["msg"]["delta_fp"])
                if q > 1e-9:
                    side[p] = q
                else:
                    side.pop(p, None)
            self.assertEqual(bp[tk][i], (yes, no), i)


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------

class TestTrades(StreamCase):

    def write_settled(self, trades, bare=False, path=None):
        by = {}
        for r in trades:
            by.setdefault(r["ticker"], []).append(r)
        obj = by if bare else {"event_ticker": EVT, "markets": [
            {"ticker": A, "result": "yes"}], "trades": by}
        C.write_json_atomic(path or C.settled_file(EVT), obj)

    def test_parse_both_ws_formats(self):
        put(T + 5.3, ws_trade(A, "yes", 45, 12, int(T + 5), "id1", fmt="a"))
        put(T + 6.3, ws_trade(B, "no", 3, 0.93, int(T + 6), "id2", fmt="b"))
        put(T + 7.3, ws_trade(B, "no", 3, 2, (T + 7) * 1000, "id3", fmt="b"))
        put(T + 8.3, ws_trade(OTHER, "no", 3, 2, int(T + 8), "id4"))
        es = self.load()
        self.assertEqual(es.trades, [
            S.Trade(int(T + 5), T + 5.3, A, "yes", 45, 12.0),
            S.Trade(int(T + 6), T + 6.3, B, "no", 3, 0.93),
            S.Trade(T + 7, T + 7.3, B, "no", 3, 2.0)])
        self.assertEqual(es.coverage["trades_src"], "ws")
        self.assertEqual(es.coverage["trades"], 3)
        p = S.parse_ws_trade({"market_ticker": A, "yes_price": 1, "count": 3,
                              "taker_side": "no", "ts": 5}, 6.0)
        self.assertEqual(p[0].yes_cents, 1)      # legacy 1 cent stays 1 cent

    def test_rest_fill_in_and_dedupe(self):
        put(T + 5.3, ws_trade(A, "yes", 45, 12, int(T + 5), "id1"))
        put(T + 5.4, ws_trade(A, "yes", 45, 12, int(T + 5), "id1"))   # dup
        put(T + 9.0, marker="disconnect")
        put(T + 30.0, marker="connect")
        put(T + 40.2, ws_trade(A, "no", 44, 3, int(T + 40), "id5"))
        self.write_settled([
            rest_trade(A, "yes", 45, 12, T + 5.25, "id1"),
            rest_trade(A, "no", 46, 7, T + 20.123456, "id2"),     # ws hole
            rest_trade(A, "no", 46, 9, T + 21.5, "id3", block=True),
            rest_trade(A, "yes", 47, 1, T + 75.0, "id9"),        # out of window
            rest_trade(A, "no", 44, 3, T + 40.1, "id5"),
        ])
        es = self.load()
        self.assertEqual([round(t.ts - T, 6) for t in es.trades],
                         [5.25, 20.123456, 40.1])
        t1, t2, t3 = es.trades
        self.assertEqual(t1.rx, T + 5.3)          # ws copy, REST precise ts
        self.assertEqual(t2.rx, t2.ts)            # REST: rx = ts
        self.assertEqual((t2.taker_side, t2.yes_cents, t2.count),
                         ("no", 46, 7.0))
        self.assertEqual(es.trade_info, [("ws", "id1"), ("rest", "id2"),
                                         ("ws", "id5")])
        self.assertEqual(es.coverage["trades_src"], "mixed")
        self.assertEqual(es.coverage["trades_rest"], 1)
        for i in range(1, len(es.trades)):
            self.assertLessEqual(es.trades[i - 1].ts, es.trades[i].ts)

    def test_whole_event_rest_when_ws_has_no_trades(self):
        put(T + 0.2, snap(A, {40: 100}, {55: 300}))
        self.write_settled([
            rest_trade(A, "no", 46, 7, T + 20.5, "a"),
            rest_trade(B, "yes", 3, 0.5, T + 10.5, "b")], bare=True)
        es = self.load()
        self.assertEqual([(t.ticker, t.ts) for t in es.trades],
                         [(B, T + 10.5), (A, T + 20.5)])
        self.assertEqual(es.coverage["trades_src"], "rest")
        self.assertIn(B, es.tickers)

    def test_backfill_settled_fallback(self):
        p = os.path.join(C.DATA_ROOT, "backfill", "settled", f"{EVT}.json")
        self.write_settled([rest_trade(A, "no", 46, 7, T + 20.5, "a")], path=p)
        es = self.load()
        self.assertEqual(len(es.trades), 1)
        self.assertTrue(es.coverage["settled_file"])

    def test_fuzzy_dedupe_without_trade_id(self):
        put(T + 5.3, ws_trade(A, "yes", 45, 12, int(T + 5)))         # no id
        put(T + 6.3, ws_trade(A, "yes", 45, 12, int(T + 6)))         # no id
        self.write_settled([
            rest_trade(A, "yes", 45, 12, T + 5.6, "r1"),     # = first ws
            rest_trade(A, "yes", 45, 12, T + 6.2, "r2"),     # = second ws
            rest_trade(A, "yes", 45, 12, T + 6.9, "r3"),     # unmatched
            rest_trade(A, "yes", 45, 11, T + 5.5, "r4"),     # other count
        ])
        es = self.load()
        self.assertEqual(sorted(src for src, _ in es.trade_info),
                         ["rest", "rest", "ws", "ws"])
        self.assertEqual(len(es.trades), 4)
        self.assertEqual({tid for src, tid in es.trade_info if src == "rest"},
                         {"r3", "r4"})


if __name__ == "__main__":
    unittest.main()
