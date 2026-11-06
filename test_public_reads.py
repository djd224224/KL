"""The box's Kalshi readers route through kalshi_reads (2026-11-06: Kalshi refused
this box's unsigned list reads for over an hour). List reads go signed first, one
market public first; a failure on both routes keeps each caller's own contract.
No network: kalshi_reads.kalshi_get is stubbed."""
import unittest
from unittest import mock

import kalshi_reads as kr
import send_imm_toxic_halts as th
import send_lone_ref_monitor as lm
import imm_pvd_watch as pw
import pvd_scan as ps

# test_send_imm_toxic_halts swaps th._get for a fake in its setUpModule (never put
# back): keep the real readers as imported
REAL = {"toxic": th._get, "lone": lm._get, "watch": pw._get, "scan": ps.pget}


class Routes(unittest.TestCase):
    def test_prefer_for(self):
        for path, want in (("/markets", "signed"), ("/markets/trades", "signed"), ("/markets/candlesticks", "signed"),
                           ("/events", "signed"), ("/series/KXRAIN", "signed"), ("/incentive_programs", "signed"),
                           ("/markets/KXNBAGAME-26NOV06BOSCHA-BOS", "public"),
                           ("/markets/KXNBAGAME-26NOV06BOSCHA-BOS/orderbook", "public")):
            self.assertEqual(kr.prefer_for(path), want, path)

    def test_read_takes_the_paths_route(self):
        with mock.patch.object(kr, "kalshi_get", return_value={"ok": 1}) as g:
            self.assertEqual(kr.read("/markets/trades", {"ticker": "X"}), {"ok": 1})
            self.assertEqual(g.call_args.kwargs["prefer"], "signed")
            kr.read("/markets/X-1/orderbook")
            self.assertEqual(g.call_args.kwargs["prefer"], "public")


class Readers(unittest.TestCase):
    def test_the_reports_read_through_kalshi_reads_and_raise_runtime_errors(self):
        for name in ("toxic", "lone"):
            get = REAL[name]
            with mock.patch.object(kr, "kalshi_get", return_value={"trades": []}) as g:
                self.assertEqual(get("/markets/trades", {"ticker": "X"}), {"trades": []})
                self.assertEqual((g.call_args.args[0], g.call_args.kwargs["prefer"]), ("/markets/trades", "signed"))
                get("/markets/X-1", {})
                self.assertEqual(g.call_args.kwargs["prefer"], "public")
            with mock.patch.object(kr, "kalshi_get", side_effect=kr.KalshiReadError("signed: 429; public: 429")):
                with self.assertRaises(RuntimeError) as cx:
                    get("/markets/trades", {"ticker": "X"})
                self.assertIn("read failed on /markets/trades", str(cx.exception))

    def test_the_pvd_page_and_scan(self):
        with mock.patch.object(kr, "kalshi_get", return_value={"markets": []}) as g:
            REAL["watch"]("/markets", {"tickers": "A,B"})
            self.assertEqual(g.call_args.kwargs["prefer"], "signed")
            REAL["watch"]("/markets/A/orderbook")
            self.assertEqual(g.call_args.kwargs["prefer"], "public")
            self.assertEqual(REAL["scan"]("/markets", {"tickers": "A"}), {"markets": []})
        with mock.patch.object(kr, "kalshi_get", side_effect=kr.KalshiReadError("both")):
            self.assertIsNone(REAL["scan"]("/markets/A/orderbook"))        # the scan's contract: None
            with self.assertRaises(kr.KalshiReadError):                     # the page's: raise, keep the last read
                REAL["watch"]("/markets", {"tickers": "A"})


if __name__ == "__main__":
    unittest.main()
