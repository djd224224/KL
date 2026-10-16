#!/usr/bin/env python3
"""Unit tests for temp_shadow_collector.py -- pure logic only, no network.

The data root is sandboxed to a fresh temp dir BEFORE temp_shadow_common is
imported (it reads TEMP_SHADOW_DATA at import), so nothing here can touch the
live KL-data\\temp-shadow recording, lock or status file.
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="temp_shadow_collector_test_")
os.environ["TEMP_SHADOW_DATA"] = _SANDBOX

import datetime as dt   # noqa: E402
import gzip             # noqa: E402
import json             # noqa: E402
import shutil           # noqa: E402
import threading        # noqa: E402
import unittest         # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import temp_shadow_common as C          # noqa: E402
import temp_shadow_collector as TC      # noqa: E402

assert C.DATA_ROOT == _SANDBOX, "sandbox not applied"


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def station(sid, temp, code="ok", rec=1000):
    return {"station_id": sid, "temp_f": temp, "code": code, "source": "hf_asos",
            "received_at_ms": rec}


class WxDifferTest(unittest.TestCase):
    def test_first_rows_written_then_only_changes(self):
        d = TC.WxDiffer("miami")
        resp = {"config_version": "cv1", "timeseries": [
            {"t": 60000, "status": "normal", "v": 81.68, "contributors": 5,
             "stations": [station("KMIA1M", 82.4), station("KOPF1M", 82.4)]},
            {"t": 120000, "status": "incomplete", "stations": [
                station("KMIA1M", None, code="pending")]},
        ]}
        out = d.changed(resp, 100.0)
        self.assertEqual([r["t"] for r in out], [60000, 120000])
        self.assertEqual(out[0]["rx"], 100.0)
        self.assertEqual(out[0]["city"], "miami")
        self.assertEqual(out[0]["config_version"], "cv1")
        self.assertIsNone(out[1]["v"])
        for k in ("rx", "city", "t", "status", "v", "contributors", "stations"):
            self.assertIn(k, out[1])
        # identical response: nothing
        self.assertEqual(d.changed(resp, 101.0), [])
        # pending -> final on minute 120000 only
        resp2 = json.loads(json.dumps(resp))
        resp2["timeseries"][1] = {"t": 120000, "status": "normal", "v": 81.32,
                                  "contributors": 5,
                                  "stations": [station("KMIA1M", 80.6)]}
        out2 = d.changed(resp2, 102.0)
        self.assertEqual([r["t"] for r in out2], [120000])
        self.assertEqual(out2[0]["status"], "normal")
        self.assertEqual(out2[0]["v"], 81.32)

    def test_station_order_ignored_and_new_station_detected(self):
        d = TC.WxDiffer("miami")
        a = {"timeseries": [{"t": 1, "status": "incomplete",
                             "stations": [station("A", 1), station("B", 2)]}]}
        b = {"timeseries": [{"t": 1, "status": "incomplete",
                             "stations": [station("B", 2), station("A", 1)]}]}
        c = {"timeseries": [{"t": 1, "status": "incomplete",
                             "stations": [station("B", 2), station("A", 1), station("C", 3)]}]}
        self.assertEqual(len(d.changed(a, 1.0)), 1)
        self.assertEqual(d.changed(b, 2.0), [])
        self.assertEqual(len(d.changed(c, 3.0)), 1)

    def test_prune(self):
        d = TC.WxDiffer("miami", keep_ms=120000)
        d.changed({"timeseries": [{"t": 0, "status": "x"}]}, 1.0)
        d.changed({"timeseries": [{"t": 600000, "status": "x"}]}, 2.0)
        self.assertNotIn(0, d.last)
        self.assertIn(600000, d.last)

    def test_empty_or_bad(self):
        d = TC.WxDiffer("miami")
        self.assertEqual(d.changed({}, 1.0), [])
        self.assertEqual(d.changed(None, 1.0), [])
        self.assertEqual(d.changed({"timeseries": [{"no_t": 1}, "junk"]}, 1.0), [])


class SeqTrackerTest(unittest.TestCase):
    def test_in_order_and_gap(self):
        s = TC.SeqTracker()
        self.assertIsNone(s.check({"type": "orderbook_snapshot", "sid": 1, "seq": 1}))
        self.assertIsNone(s.check({"type": "orderbook_delta", "sid": 1, "seq": 2}))
        # an ok on the same sid takes a seq
        self.assertIsNone(s.check({"type": "ok", "id": 5, "sid": 1, "seq": 3}))
        self.assertIsNone(s.check({"type": "orderbook_delta", "sid": 1, "seq": 4}))
        self.assertEqual(s.check({"type": "orderbook_delta", "sid": 1, "seq": 6}), (1, 5, 6))
        # continues from the new value
        self.assertIsNone(s.check({"type": "orderbook_delta", "sid": 1, "seq": 7}))

    def test_independent_sids_and_untracked(self):
        s = TC.SeqTracker()
        self.assertIsNone(s.check({"sid": 1, "seq": 1}))
        self.assertIsNone(s.check({"sid": 2, "seq": 1}))
        self.assertIsNone(s.check({"sid": 2, "seq": 2}))
        self.assertIsNone(s.check({"sid": 1, "seq": 2}))
        self.assertIsNone(s.check({"type": "subscribed", "id": 1}))     # no seq
        self.assertIsNone(s.check({"sid": "x", "seq": "y"}))

    def test_first_seq_above_one_is_gap(self):
        s = TC.SeqTracker()
        self.assertEqual(s.check({"sid": 3, "seq": 4}), (3, 1, 4))

    def test_duplicate_is_gap_and_reset(self):
        s = TC.SeqTracker()
        s.check({"sid": 1, "seq": 1})
        s.check({"sid": 1, "seq": 2})
        self.assertEqual(s.check({"sid": 1, "seq": 2}), (1, 3, 2))
        s.reset()
        self.assertIsNone(s.check({"sid": 1, "seq": 1}))


class CadenceTest(unittest.TestCase):
    M = ts("2026-10-15T01:00:00Z")      # a minute boundary

    # the spec's [18, 50] window passed explicitly; the module default low
    # edge is 13 (WX_LO)
    def test_fast_inside_window(self):
        f = lambda t: TC.next_poll_time(t, lo=18, hi=50)
        self.assertAlmostEqual(f(self.M + 20.0), self.M + 21.0)
        self.assertAlmostEqual(f(self.M + 49.0), self.M + 50.0)
        self.assertAlmostEqual(f(self.M + 17.5), self.M + 18.5)

    def test_slow_outside_window(self):
        f = lambda t: TC.next_poll_time(t, lo=18, hi=50)
        self.assertAlmostEqual(f(self.M + 50.3), self.M + 55.3)
        self.assertAlmostEqual(f(self.M + 55.3), self.M + 60.3)
        self.assertAlmostEqual(f(self.M + 5.0), self.M + 10.0)

    def test_jumps_to_window_start(self):
        f = lambda t: TC.next_poll_time(t, lo=18, hi=50)
        self.assertAlmostEqual(f(self.M + 15.3), self.M + 18.0)
        self.assertAlmostEqual(f(self.M + 13.0), self.M + 18.0)

    def test_default_window(self):
        self.assertEqual((TC.WX_LO, TC.WX_HI), (13.0, 50.0))
        self.assertAlmostEqual(TC.next_poll_time(self.M + 10.3), self.M + 13.0)
        self.assertAlmostEqual(TC.next_poll_time(self.M + 13.0), self.M + 14.0)

    def test_rate_cap(self):
        # simulate a full minute: never more than 1.25 req/s, ~40 polls/min
        t = self.M
        starts = [t]
        while t < self.M + 600:
            t = TC.next_poll_time(t)
            starts.append(t)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreaterEqual(min(gaps), TC.WX_MIN_GAP - 1e-9)
        per_min = len([s for s in starts if self.M + 60 <= s < self.M + 120])
        self.assertTrue(38 <= per_min <= 46, per_min)
        inside = [s for s in starts if 13 <= s % 60 <= 50 and self.M + 60 <= s < self.M + 120]
        self.assertGreaterEqual(len(inside), 37)

    def test_rest_gap_doubles_in_landing_window(self):
        self.assertEqual(TC.rest_gap(self.M + 5.0), TC.REST_MIN_GAP)
        self.assertEqual(TC.rest_gap(self.M + 13.0), TC.REST_WINDOW_GAP)
        self.assertEqual(TC.rest_gap(self.M + 50.0), TC.REST_WINDOW_GAP)
        self.assertEqual(TC.rest_gap(self.M + 51.0), TC.REST_MIN_GAP)
        self.assertEqual(TC.rest_gap(self.M + 30.0, 1.05, 2.0), 2.0)

    def test_min_gap_param(self):
        self.assertAlmostEqual(TC.next_poll_time(100.0 * 60 + 20, fast=0.1), 100.0 * 60 + 20.8)


class RateStateTest(unittest.TestCase):
    def test_backoff_sequence_and_sign_switch(self):
        r = TC.RateState()
        now = 1000.0
        self.assertFalse(r.signed(now))
        self.assertEqual(r.on_429(now), 2.0)
        self.assertEqual(r.backoff_until, now + 2.0)
        self.assertFalse(r.signed(now))
        self.assertEqual(r.on_429(now), 4.0)
        self.assertFalse(r.signed(now))
        self.assertEqual(r.on_429(now), 8.0)          # 3rd consecutive -> sign
        self.assertTrue(r.signed(now))
        self.assertTrue(r.signed(now + 1799))
        self.assertFalse(r.signed(now + 1801))
        self.assertEqual(r.sign_switches, 1)
        for _ in range(10):
            w = r.on_429(now)
        self.assertEqual(w, 60.0)                      # capped
        self.assertEqual(r.n429, 13)

    def test_success_resets(self):
        r = TC.RateState()
        r.on_429(0.0)
        r.on_429(0.0)
        r.on_ok(1.0)
        self.assertEqual(r.consec, 0)
        self.assertEqual(r.backoff_until, 0.0)
        self.assertEqual(r.on_429(2.0), 2.0)
        self.assertFalse(r.signed(2.0))               # count restarted

    def test_signing_extends_while_429s_continue(self):
        r = TC.RateState()
        for _ in range(3):
            r.on_429(0.0)
        r.on_429(1000.0)
        self.assertTrue(r.signed(2700.0))
        self.assertEqual(r.sign_switches, 1)


class PollStatsTest(unittest.TestCase):
    def test_minute_rollover(self):
        p = TC.PollStats()
        base = ts("2026-10-15T01:00:00Z")
        self.assertIsNone(p.add(base + 1, 200, 50.0, False))
        self.assertIsNone(p.add(base + 2, 429, 30.0, False))
        self.assertIsNone(p.add(base + 3, 0, 5000.0, True))
        d = p.add(base + 61, 200, 40.0, False)
        self.assertEqual(d["n"], 3)
        self.assertEqual(d["n429"], 1)
        self.assertEqual(d["nerr"], 1)
        self.assertEqual(d["lat_ms_max"], 5000.0)
        self.assertEqual(d["lat_ms_p50"], 50.0)
        self.assertTrue(d["signed"])
        self.assertEqual(d["t"], base)
        self.assertEqual(p.total["n"], 4)


class ProgramsScheduleTest(unittest.TestCase):
    def test_interval(self):
        h = ts("2026-10-15T01:00:00Z")
        self.assertEqual(TC.programs_interval(h), 60.0)
        self.assertEqual(TC.programs_interval(h + 6 * 60 + 59), 60.0)
        self.assertEqual(TC.programs_interval(h + 7 * 60), 600.0)
        self.assertEqual(TC.programs_interval(h + 59 * 60), 600.0)

    def test_due(self):
        h = ts("2026-10-15T01:00:00Z")
        self.assertTrue(TC.programs_due(h, None))
        self.assertTrue(TC.programs_due(h, h - 300))      # hour start: poll at once
        self.assertFalse(TC.programs_due(h + 30, h))
        self.assertTrue(TC.programs_due(h + 60, h))
        self.assertFalse(TC.programs_due(h + 900, h + 420))
        self.assertTrue(TC.programs_due(h + 1020, h + 420))

    def test_signature(self):
        a = [{"id": "1", "market_ticker": "X", "start_date": "s", "end_date": "e"}]
        b = [{"id": "1", "market_ticker": "X", "start_date": "s", "end_date": "e",
              "other": "ignored"}]
        c = [{"id": "1", "market_ticker": "X", "start_date": "s", "end_date": "e2"}]
        self.assertEqual(TC.programs_signature(a), TC.programs_signature(b))
        self.assertNotEqual(TC.programs_signature(a), TC.programs_signature(c))


class SettlementTest(unittest.TestCase):
    def test_action(self):
        close = 10000.0
        self.assertEqual(TC.settle_action(close + 60, close, None, False), "wait")
        self.assertEqual(TC.settle_action(close + 419, close, None, False), "wait")
        self.assertEqual(TC.settle_action(close + 420, close, None, False), "attempt")
        self.assertEqual(TC.settle_action(close + 500, close, close + 420, False), "wait")
        self.assertEqual(TC.settle_action(close + 540, close, close + 420, False), "attempt")
        self.assertEqual(TC.settle_action(close + 540, close, None, True), "done")
        self.assertEqual(TC.settle_action(close + 540, None, None, False), "wait")

    def test_results_and_deadline(self):
        self.assertFalse(TC.all_results([]))
        self.assertFalse(TC.all_results([{"result": "yes"}, {"result": ""}]))
        self.assertFalse(TC.all_results([{"result": "yes"}, {}]))
        self.assertTrue(TC.all_results([{"result": "yes"}, {"result": "no"}]))
        close = 10000.0
        self.assertFalse(TC.settle_deadline_passed(close + 420 + 7200, close))
        self.assertTrue(TC.settle_deadline_passed(close + 420 + 7201, close))

    def test_event_tickers(self):
        s = ts("2026-10-14T13:00:00Z")           # 09:00 EDT
        self.assertEqual(TC.event_ticker_for_hour("KXTEMPMIAH", s), "KXTEMPMIAH-26OCT1409")
        self.assertEqual(C.parse_event("KXTEMPMIAH-26OCT1409")[1], s)
        evs = TC.recent_event_tickers("KXTEMPMIAH", s + 1800, 2)
        self.assertEqual([e for e, _ in evs], ["KXTEMPMIAH-26OCT1409", "KXTEMPMIAH-26OCT1408",
                                               "KXTEMPMIAH-26OCT1407"])
        self.assertEqual(evs[0][1], s)

    def test_markets_signature(self):
        m = [{"ticker": "A", "status": "active", "open_time": "o", "close_time": "c",
              "yes_bid_dollars": "0.1"}]
        m2 = [dict(m[0], yes_bid_dollars="0.2")]
        m3 = [dict(m[0], status="closed")]
        self.assertEqual(TC.markets_signature(m), TC.markets_signature(m2))
        self.assertNotEqual(TC.markets_signature(m), TC.markets_signature(m3))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=_SANDBOX)
        self.path = os.path.join(self.dir, "collector.lock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def probe(self, alive=True, exe="python.exe", ctime=None):
        return dict(alive=lambda pid: alive, exe=lambda pid: exe, ctime=lambda pid: ctime)

    def test_acquire_release(self):
        ok, why = TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        self.assertTrue(ok, why)
        self.assertEqual(TC.read_lock(self.path), (111, 5.0))
        self.assertFalse(TC.release_lock(self.path, pid=222))   # not ours
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(TC.release_lock(self.path, pid=111))
        self.assertFalse(os.path.exists(self.path))

    def test_refuse_when_live_python(self):
        TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        ok, why = TC.acquire_lock(self.path, pid=222, **self.probe(alive=True, ctime=1.0))
        self.assertFalse(ok)
        self.assertIn("111", why)
        self.assertEqual(TC.read_lock(self.path)[0], 111)

    def test_stale_dead_pid(self):
        TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        ok, _ = TC.acquire_lock(self.path, pid=222, **self.probe(alive=False))
        self.assertTrue(ok)
        self.assertEqual(TC.read_lock(self.path)[0], 222)

    def test_stale_not_python(self):
        TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        ok, _ = TC.acquire_lock(self.path, pid=222, **self.probe(exe="chrome.exe"))
        self.assertTrue(ok)

    def test_stale_reused_pid(self):
        TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        ok, _ = TC.acquire_lock(self.path, pid=222, **self.probe(ctime=500.0))
        self.assertTrue(ok)

    def test_unknown_exe_is_conservative(self):
        TC.acquire_lock(self.path, pid=111, now=5.0, **self.probe())
        ok, _ = TC.acquire_lock(self.path, pid=222, **self.probe(exe=""))
        self.assertFalse(ok)

    def test_plain_pid_file_and_garbage(self):
        with open(self.path, "w") as f:
            f.write("333")
        self.assertEqual(TC.read_lock(self.path)[0], 333)
        ok, _ = TC.acquire_lock(self.path, pid=222, **self.probe(alive=False))
        self.assertTrue(ok)
        with open(self.path, "w") as f:
            f.write("garbage")
        ok, _ = TC.acquire_lock(self.path, pid=444, **self.probe())
        self.assertTrue(ok)

    def test_real_probes_on_this_process(self):
        self.assertTrue(TC.pid_alive(os.getpid()))
        self.assertIn("python", TC.pid_exe(os.getpid()))
        self.assertFalse(TC.pid_alive(0))
        self.assertTrue(TC.lock_holder_live(os.getpid(), dt.datetime.now().timestamp()))


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=_SANDBOX)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, day, name, lines):
        d = os.path.join(self.root, "raw", day)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            for rec in lines:
                f.write(json.dumps(rec) + "\n")
        return p

    def test_due_and_gzip(self):
        now = ts("2026-10-15T05:30:00Z")
        old = self.make("2026-10-15", "ws_03.jsonl", [{"rx": 1}, {"rx": 2}])
        older = self.make("2026-10-14", "meta_23.jsonl", [{"rx": 3}])
        recent = self.make("2026-10-15", "wx_04.jsonl", [{"rx": 4}])
        current = self.make("2026-10-15", "ws_05.jsonl", [{"rx": 5}])
        junk = self.make("2026-10-15", "notes.txt", [])
        due = TC.gzip_due_files(self.root, now)
        self.assertEqual(sorted(due), sorted([old, older]))
        self.assertEqual(TC.raw_hour_of(old), ts("2026-10-15T03:00:00Z"))
        self.assertIsNone(TC.raw_hour_of(junk))
        for p in due:
            gz = TC.gzip_file(p)
            self.assertFalse(os.path.exists(p))
            self.assertTrue(os.path.exists(gz))
        self.assertEqual([r["rx"] for r in C.iter_jsonl(old + ".gz")], [1, 2])
        self.assertTrue(os.path.exists(recent) and os.path.exists(current))
        self.assertEqual(TC.gzip_due_files(self.root, now), [])

    def test_late_plain_merges_as_member(self):
        p = self.make("2026-10-15", "ws_03.jsonl", [{"rx": 1}])
        TC.gzip_file(p)
        self.make("2026-10-15", "ws_03.jsonl", [{"rx": 2}])
        TC.gzip_file(p)
        self.assertEqual([r["rx"] for r in C.iter_jsonl(p + ".gz")], [1, 2])


class RawWriterTest(unittest.TestCase):
    def test_hour_files_and_flush(self):
        w = TC.RawWriter()
        t0 = ts("2026-10-15T06:59:59Z")
        w.write("wx", {"rx": t0, "a": 1})
        w.write("wx", {"rx": t0 + 2, "a": 2})          # next hour
        w.write("wx", {"rx": t0 - 1, "a": 3})          # late row for the earlier hour
        w.write_line("ws", t0 + 2, '{"rx":%.6f,"m":{"type":"x"}}' % (t0 + 2))
        w.flush()
        p6, p7 = C.raw_file("wx", t0), C.raw_file("wx", t0 + 2)
        self.assertNotEqual(p6, p7)
        self.assertEqual([r["a"] for r in C.iter_jsonl(p6)], [1, 3])
        self.assertEqual([r["a"] for r in C.iter_jsonl(p7)], [2])
        self.assertEqual([r["m"]["type"] for r in C.iter_jsonl(C.raw_file("ws", t0 + 2))], ["x"])
        self.assertEqual(list(C.iter_raw("wx", t0 - 10, t0 + 10)),
                         [{"rx": t0, "a": 1}, {"rx": t0 - 1, "a": 3}, {"rx": t0 + 2, "a": 2}])
        w.close_path(p6)
        w.write("wx", {"rx": t0 - 1, "a": 4})          # reopens in append mode
        w.close_all()
        self.assertEqual([r["a"] for r in C.iter_jsonl(p6)], [1, 3, 4])


class FakeConn:
    def __init__(self):
        self.sent = []

    def send_json(self, obj):
        self.sent.append(obj)


class WSRecorderTest(unittest.TestCase):
    def setUp(self):
        self.writer = TC.RawWriter()
        self.rec = TC.WSRecorder(self.writer, lambda: {}, threading.Event())
        self.conn = FakeConn()
        self.rec.connected = True

    def tearDown(self):
        self.writer.close_all()

    def ws_rows(self, t0, t1):
        self.writer.flush()
        return list(C.iter_raw("ws", t0, t1))

    def test_subscribe_add_delete_cycle(self):
        r, conn = self.rec, self.conn
        r.set_markets({"A", "B"})
        r._apply(conn)
        subs = [m for m in conn.sent if m["cmd"] == "subscribe"]
        self.assertEqual(len(subs), 2)
        self.assertEqual({tuple(m["params"]["channels"]) for m in subs},
                         {("orderbook_delta",), ("trade",)})
        self.assertEqual(subs[0]["params"]["market_tickers"], ["A", "B"])
        r.handle({"type": "subscribed", "id": subs[0]["id"],
                  "msg": {"channel": subs[0]["params"]["channels"][0], "sid": 1}}, conn)
        r.handle({"type": "subscribed", "id": subs[1]["id"],
                  "msg": {"channel": subs[1]["params"]["channels"][0], "sid": 2}}, conn)
        self.assertEqual(r.chans["orderbook_delta"].sid, 1)
        self.assertIsNone(r.chans["trade"].pending)
        conn.sent.clear()
        r.set_markets({"B", "C"})
        r._apply(conn)        # deletes first (one command per channel per pass)
        dels = [m for m in conn.sent if m["params"].get("action") == "delete_markets"]
        self.assertEqual(len(dels), 2)
        self.assertEqual(dels[0]["params"]["market_tickers"], ["A"])
        for m in dels:
            r.handle({"type": "ok", "id": m["id"], "sid": m["params"]["sids"][0],
                      "seq": 1}, conn)
        conn.sent.clear()
        r._apply(conn)
        adds = [m for m in conn.sent if m["params"].get("action") == "add_markets"]
        self.assertEqual(len(adds), 2)
        self.assertEqual(adds[0]["params"]["market_tickers"], ["C"])
        self.assertEqual(r.chans["trade"].markets, {"B", "C"})
        # dropping everything unsubscribes and retires the sid
        for m in adds:
            r.handle({"type": "ok", "id": m["id"]}, conn)
        conn.sent.clear()
        r.set_markets(set())
        r._apply(conn)
        self.assertEqual([m["cmd"] for m in conn.sent], ["unsubscribe", "unsubscribe"])
        self.assertIsNone(r.chans["orderbook_delta"].sid)
        self.assertIn(1, r.retired)
        before = r.stats["gaps"]
        r.handle({"type": "orderbook_delta", "sid": 1, "seq": 99, "msg": {}}, conn)
        self.assertEqual(r.stats["gaps"], before)      # retired sid: no seq check

    def test_gap_writes_marker_and_resnapshots(self):
        r, conn = self.rec, self.conn
        ob = r.chans["orderbook_delta"]
        ob.sid, ob.markets = 1, {"A", "B"}
        t0 = TC.time.time()
        r.handle({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {}}, conn)
        r.handle({"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {}}, conn)
        self.assertEqual(conn.sent, [])
        r.handle({"type": "orderbook_delta", "sid": 1, "seq": 5, "msg": {}}, conn)
        self.assertEqual(r.stats["gaps"], 1)
        snap = [m for m in conn.sent if m["params"].get("action") == "get_snapshot"]
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["params"], {"sids": [1], "market_tickers": ["A", "B"],
                                             "action": "get_snapshot"})
        rows = [x for x in self.ws_rows(t0 - 5, t0 + 60) if x.get("marker") == "gap"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["expected"], rows[0]["got"]), (3, 5))
        # a second gap within 5 s: marker yes, re-snapshot deferred
        r.handle({"type": "orderbook_delta", "sid": 1, "seq": 9, "msg": {}}, conn)
        self.assertEqual(len([m for m in conn.sent
                              if m["params"].get("action") == "get_snapshot"]), 1)
        self.assertTrue(r._resnap_pending)

    def test_trade_gap_also_resnapshots_books(self):
        r, conn = self.rec, self.conn
        r.chans["orderbook_delta"].sid, r.chans["orderbook_delta"].markets = 1, {"A"}
        r.chans["trade"].sid, r.chans["trade"].markets = 2, {"A"}
        r.handle({"type": "trade", "sid": 2, "seq": 1, "msg": {}}, conn)
        r.handle({"type": "trade", "sid": 2, "seq": 3, "msg": {}}, conn)
        self.assertEqual(len([m for m in conn.sent
                              if m["params"].get("action") == "get_snapshot"]), 1)

    def test_on_message_writes_verbatim(self):
        r, conn = self.rec, self.conn
        t = TC.time.time()
        payload = b'{"type":"trade","sid":2,"seq":1,"msg":{"yes_price_dollars":"0.8700"}}'
        r.on_message(t, payload, conn)
        rows = [x for x in self.ws_rows(t - 1, t + 1) if "m" in x]
        self.assertEqual(rows[-1]["m"]["msg"]["yes_price_dollars"], "0.8700")
        self.assertAlmostEqual(rows[-1]["rx"], t, places=5)
        self.assertEqual(r.by_type["trade"], 1)

    def test_add_error_resubscribes_channel(self):
        r, conn = self.rec, self.conn
        tr = r.chans["trade"]
        tr.sid, tr.markets = 2, {"A"}
        r.set_markets({"A", "B"})
        r.chans["orderbook_delta"].sid = 1
        r.chans["orderbook_delta"].markets = {"A", "B"}
        r._apply(conn)
        add = [m for m in conn.sent if m["params"].get("action") == "add_markets"][0]
        conn.sent.clear()
        r.handle({"type": "error", "id": add["id"], "msg": {"code": 8, "msg": "nope"}}, conn)
        self.assertEqual([m["cmd"] for m in conn.sent], ["unsubscribe"])
        self.assertIsNone(tr.sid)
        self.assertIn(2, r.retired)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, path, params=None, tries=3):
        i = 0
        if params and params.get("cursor"):
            i = int(params["cursor"])
        self.calls += 1
        body = {"incentive_programs": self.pages[i],
                "next_cursor": str(i + 1) if i + 1 < len(self.pages) else ""}
        return TC.Resp(200, body, 1.0)


class FakeColl:
    def __init__(self):
        self.writer = TC.RawWriter()
        self.registry = TC.Registry(self.writer)
        self.programs = TC.Programs(self.writer)
        self.stop_evt = threading.Event()
        self.signer = TC.Signer()


class ProgramsPollTest(unittest.TestCase):
    def prog(self, tk, start):
        return {"id": tk + start, "market_ticker": tk, "start_date": start,
                "end_date": "2026-10-15T02:00:00Z", "period_reward": 1000000}

    def setUp(self):
        self.coll = FakeColl()
        now = ts("2026-10-15T01:03:00Z")
        self.now = now
        mk = [{"ticker": "KXTEMPMIAH-26OCT1422-T80.99", "status": "active",
               "open_time": "2026-10-15T01:00:00Z", "close_time": "2026-10-15T02:00:00Z"}]
        self.coll.registry.update("KXTEMPMIAH-26OCT1422", mk, now)
        self.pages = [
            [self.prog("KXTEMPMIAH-26OCT1422-T80.99", "2026-10-15T01:02:00Z"),
             self.prog("KXOTHER-1", "2026-10-14T20:00:00Z")],
            [self.prog("KXOTHER-2", "2026-10-14T10:00:00Z")],
            [self.prog("KXOTHER-3", "2026-10-13T10:00:00Z")],
        ]

    def tearDown(self):
        self.coll.writer.close_all()

    def poller(self):
        p = TC.RestPoller(self.coll)
        p.client = FakeClient(self.pages)
        return p

    def test_full_then_early_stop(self):
        p = self.poller()
        p.maybe_programs(self.now)                 # first poll is a full sweep
        self.assertEqual(p.client.calls, 3)
        self.assertTrue(p.early_stop_ok)
        self.assertEqual([x["market_ticker"] for x in self.coll.programs.active],
                         ["KXTEMPMIAH-26OCT1422-T80.99"])
        p.client.calls = 0
        p.maybe_programs(self.now + 60)            # hh:04 -> due; early stop after page 0
        self.assertEqual(p.client.calls, 1)
        p.client.calls = 0
        p.maybe_programs(self.now + 90)            # not due yet
        self.assertEqual(p.client.calls, 0)

    def test_full_sweep_disables_early_stop_when_wrong(self):
        self.pages[2].append(self.prog("KXTEMPMIAH-26OCT1422-T81.99", "2026-10-15T01:02:30Z"))
        p = self.poller()
        p.maybe_programs(self.now)
        self.assertFalse(p.early_stop_ok)
        self.assertEqual(len(self.coll.programs.active), 2)
        p.client.calls = 0
        p.maybe_programs(self.now + 60)
        self.assertEqual(p.client.calls, 3)        # always full from now on


class RegistryTest(unittest.TestCase):
    def test_markets_meta_on_change_only(self):
        w = TC.RawWriter()
        reg = TC.Registry(w)
        t = TC.time.time()
        mk = [{"ticker": "KXTEMPMIAH-26OCT1409-T81.99", "event_ticker": "KXTEMPMIAH-26OCT1409",
               "status": "active", "open_time": "2026-10-14T12:00:00Z",
               "close_time": "2026-10-14T13:00:00Z", "yes_bid_dollars": "0.10"}]
        self.assertTrue(reg.update("KXTEMPMIAH-26OCT1409", mk, t))
        mk2 = [dict(mk[0], yes_bid_dollars="0.20")]
        self.assertFalse(reg.update("KXTEMPMIAH-26OCT1409", mk2, t + 1))
        mk3 = [dict(mk[0], status="closed")]
        self.assertTrue(reg.update("KXTEMPMIAH-26OCT1409", mk3, t + 2))
        w.flush()
        rows = [r for r in C.iter_raw("meta", t - 1, t + 5) if r.get("kind") == "markets"
                and r["data"]["event_ticker"] == "KXTEMPMIAH-26OCT1409"]
        self.assertEqual(len(rows), 2)
        ev = reg.events["KXTEMPMIAH-26OCT1409"]
        self.assertEqual(ev.close_ts, ts("2026-10-14T13:00:00Z"))
        self.assertEqual(ev.open_ts, ts("2026-10-14T12:00:00Z"))
        # ws wants it until 10 min after close
        self.assertIn(mk[0]["ticker"], reg.want_tickers(ts("2026-10-14T13:09:00Z")))
        self.assertNotIn(mk[0]["ticker"], reg.want_tickers(ts("2026-10-14T13:11:00Z")))
        self.assertEqual(reg.current_event(ts("2026-10-14T12:30:00Z")), "KXTEMPMIAH-26OCT1409")
        w.close_all()


class RawLookupNoDirsTest(unittest.TestCase):
    """temp_shadow_common.raw_file / raw_files / iter_raw are lookups: they
    never create a folder (they used to make raw/<UTC day>/ for every hour
    they probed, leaving empty future-day folders in KL-data -- the sim's
    day_events reads 3 h past its day). Every writer still creates the
    folder before writing: the collector's RawWriter, append_jsonl /
    write_json_atomic (what the tests and the backfill use), or
    raw_file(create=True)."""

    def setUp(self):
        self.rd = os.path.join(C.DATA_ROOT, "raw")

    def days(self):
        return set(os.listdir(self.rd)) if os.path.isdir(self.rd) else set()

    def test_lookups_create_nothing(self):
        before = self.days()
        t0 = ts("2031-01-05T00:00:00Z")                  # far in the future: nothing there
        p = C.raw_file("ws", t0 + 7200)
        self.assertTrue(p.endswith(os.path.join("raw", "2031-01-05", "ws_02.jsonl")))
        self.assertEqual(C.raw_files("ws", t0, t0 + 9 * 86400), [])
        self.assertEqual(list(C.iter_raw("meta", t0 - 3 * 3600, t0 + 3 * 86400)), [])
        self.assertEqual(list(C.iter_raw("wx", t0, t0 + 3600)), [])
        self.assertEqual(self.days(), before)
        self.assertFalse(os.path.exists(os.path.dirname(p)))
        # the same through the sim's day lister (reads 3 h before / 1 h after)
        import temp_shadow_sim as sim
        self.assertEqual(sim.day_events("2031-01-07"), [])
        self.assertEqual(self.days(), before)
        # raw_dir keeps creating by default (a writer's helper); create=False looks only
        q = C.raw_dir("2031-02-01", create=False)
        self.assertFalse(os.path.exists(q))

    def test_writers_still_create_the_folder(self):
        t1 = ts("2031-03-01T05:30:00Z")
        w = TC.RawWriter()                               # the collector's write path
        w.write("wx", {"rx": t1, "a": 1})
        w.flush()
        w.close_all()
        p = C.raw_file("wx", t1)
        self.assertTrue(os.path.isfile(p))
        self.assertEqual([r["a"] for r in C.iter_jsonl(p)], [1])
        self.assertEqual(C.raw_files("wx", t1 - 10, t1 + 10), [p])
        t2 = ts("2031-03-02T05:30:00Z")                  # append_jsonl (tests, tools)
        C.append_jsonl(C.raw_file("meta", t2), {"rx": t2, "kind": "poll", "data": {}})
        self.assertEqual(len(list(C.iter_raw("meta", t2 - 1, t2 + 1))), 1)
        t3 = ts("2031-03-03T05:30:00Z")                  # an explicit writer
        p3 = C.raw_file("ws", t3, create=True)
        self.assertTrue(os.path.isdir(os.path.dirname(p3)))
        self.assertFalse(os.path.exists(p3))
        q = C.raw_dir("2031-03-04")
        self.assertTrue(os.path.isdir(q))

if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(_SANDBOX, ignore_errors=True)
