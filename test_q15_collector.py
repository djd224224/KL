#!/usr/bin/env python3
"""Unit tests for q15_collector.py (Jack 2026-10-22: "Yes use a live price
feed like Miami temp, with recorder and a week of testing"). No network.
Run in its own process (it points temp_shadow_common at a temp folder at
import): python -m unittest test_q15_collector
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="q15_collector_test_")
os.environ["Q15_SHADOW_DATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import q15_collector as Q                                             # noqa: E402
import temp_shadow_common as C                                       # noqa: E402

ISO = "2026-10-22T{}:00Z"


def _m(t, o, c, **kw):
    d = {"ticker": t, "open_time": ISO.format(o), "close_time": ISO.format(c), "status": "active"}
    d.update(kw)
    return d


class TestPaths(unittest.TestCase):

    def test_fx_and_the_crypto_race_without_pyth_by_default(self):
        self.assertEqual(Q.SERIES, ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M",
                                    "KXUSDCAD15M", "KXCRYPTOLEAD15M"))
        self.assertFalse(Q.PYTH_ON)

    def test_the_miami_helpers_point_at_this_test(self):
        self.assertEqual(os.path.abspath(C.DATA_ROOT), os.path.abspath(_TMP))
        self.assertTrue(Q.T.LOG_FILE.startswith(_TMP))
        self.assertTrue(C.LOCK_FILE.startswith(_TMP))
        self.assertTrue(Q.T._RAW_NAME.match("px_07.jsonl"))


class TestWantTickers(unittest.TestCase):

    def test_from_a_minute_before_open_to_ten_after_close(self):
        mk = {"A": _m("A", "20:15", "20:30"), "B": _m("B", "20:30", "20:45"),
              "C": _m("C", "19:45", "20:00")}
        at = C.parse_iso("2026-10-22T20:29:30Z")
        self.assertEqual(Q.want_tickers(mk, at), ["A", "B"])          # B opens in 30 s
        # 20:09: C closed 9 min ago (kept), A opens in 6 min (not yet)
        self.assertEqual(Q.want_tickers(mk, C.parse_iso("2026-10-22T20:09:00Z")), ["C"])
        self.assertEqual(Q.want_tickers(mk, C.parse_iso("2026-10-22T20:11:00Z")), [])
        self.assertEqual(Q.want_tickers(mk, C.parse_iso("2026-10-22T20:14:30Z")), ["A"])

    def test_quarter_start_and_signature(self):
        t = C.parse_iso("2026-10-22T20:29:30Z")
        self.assertEqual(Q.quarter_start(t), C.parse_iso("2026-10-22T20:15:00Z"))
        a = _m("A", "20:15", "20:30")
        self.assertEqual(Q.market_sig(a), Q.market_sig(dict(a, volume=5)))     # noise
        self.assertNotEqual(Q.market_sig(a), Q.market_sig(dict(a, result="yes")))


class TestParsers(unittest.TestCase):

    def test_kraken_ticker(self):
        msg = {"channel": "ticker", "type": "update", "data": [
            {"symbol": "EUR/USD", "bid": 1.16200, "ask": 1.16210},
            {"symbol": "BTC/USD", "bid": 1, "ask": 2},           # not one of ours
            {"symbol": "USD/JPY", "bid": "bad", "ask": 151.2}]}
        self.assertEqual(Q.parse_kraken(msg), {"EURUSD": [1.16205, 1.162, 1.1621]})
        self.assertEqual(Q.parse_kraken({"channel": "heartbeat"}), {})

    def test_hyperliquid_all_mids(self):
        msg = {"channel": "allMids", "data": {"mids": {"BTC": "67000.5", "ETH": "2500",
                                                         "DOGE": "0.1", "HYPE": "31.2"}}}
        self.assertEqual(Q.parse_hl(msg), {"BTC": 67000.5, "ETH": 2500.0, "HYPE": 31.2})
        self.assertEqual(Q.parse_hl({"channel": "subscriptionResponse"}), {})

    def test_pyth_update(self):
        fid = Q.PYTH_FEEDS["EURUSD"][1]
        js = {"parsed": [
            {"id": fid, "price": {"price": "116205", "conf": "12", "expo": -5, "publish_time": 1792700000}},
            {"id": "0x" + Q.PYTH_FEEDS["BRENT"][1],
             "price": {"price": 10850000, "conf": 900, "expo": -5, "publish_time": 1792700001}},
            {"id": "deadbeef", "price": {"price": 1, "conf": 1, "expo": 0, "publish_time": 1}}]}
        out = Q.parse_pyth(js)
        self.assertAlmostEqual(out["EURUSD"][0], 1.16205)
        self.assertAlmostEqual(out["BRENT"][0], 108.5)
        self.assertEqual(out["BRENT"][2], 1792700001)
        self.assertEqual(sorted(out), ["BRENT", "EURUSD"])

    def test_pyth_key_env_then_file_never_required(self):
        with mock.patch.dict(os.environ, {"PYTH_API_KEY": ""}), \
                mock.patch.object(Q, "PYTH_KEY_FILE", os.path.join(_TMP, "absent.txt")):
            self.assertEqual(Q.pyth_key(), "")
        p = os.path.join(_TMP, "key.txt")
        with open(p, "w") as f:
            f.write("  abc  \n")
        with mock.patch.dict(os.environ, {"PYTH_API_KEY": ""}), \
                mock.patch.object(Q, "PYTH_KEY_FILE", p):
            self.assertEqual(Q.pyth_key(), "abc")
        with mock.patch.dict(os.environ, {"PYTH_API_KEY": "envkey"}):
            self.assertEqual(Q.pyth_key(), "envkey")


class TestPxBook(unittest.TestCase):

    def test_only_changes_are_written_once_a_flush(self):
        b = Q.PxBook()
        b.put("hl", {"BTC": 1.0, "ETH": 2.0}, 10.0)
        self.assertEqual(b.flush(11.0), [{"rx": 11.0, "src": "hl", "px": {"BTC": [1.0], "ETH": [2.0]}}])
        b.put("hl", {"BTC": 1.0, "ETH": 2.5}, 11.5)           # BTC unchanged
        b.put("kraken", {"EURUSD": [1.1, 1.0, 1.2]}, 11.6)
        rows = sorted(b.flush(12.0), key=lambda r: r["src"])
        self.assertEqual(rows, [{"rx": 12.0, "src": "hl", "px": {"ETH": [2.5]}},
                                {"rx": 12.0, "src": "kraken", "px": {"EURUSD": [1.1, 1.0, 1.2]}}])
        self.assertEqual(b.flush(13.0), [])
        self.assertEqual(b.updates["hl"], 2)


class TestRestNotes(unittest.TestCase):

    def test_a_market_is_written_again_only_when_it_changes(self):
        coll = mock.Mock()
        rest = Q.Q15Rest.__new__(Q.Q15Rest)
        rest.coll, rest.markets, rest._sig = coll, {}, {}
        m = _m("A", "20:15", "20:30")
        rest._note(m, 1.0, "discover")
        rest._note(dict(m), 2.0, "discover")
        rest._note(dict(m, status="finalized", result="no"), 3.0, "settle")
        self.assertEqual(coll.writer.write.call_count, 2)
        self.assertEqual(rest.markets["A"]["result"], "no")


if __name__ == "__main__":
    unittest.main()
