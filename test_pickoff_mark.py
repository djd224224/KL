"""pvd_waves pickoffs: a wide book is marked at its outside touch facing the fill, not its
mid (Jack 2026-11-14: "yes fix that", after a -$142 pick-off text on three NH-D Senate
long-shot asks marked at the mid of a ~3c x ~50c book). No network, no state files:
the fills, the books and our resting orders are stubbed.

test_pvd_waves is imported FIRST, whatever order a runner loads the modules in: it points
pvd_waves' registry, pick-off state and paths at a temp dir before pvd_waves is imported.
2026-11-15 02:29Z this file imported pvd_waves itself ahead of it, and test_pvd_waves'
fixtures then wrote two fake waves and a fake pin into the live waves.json."""
import argparse
import os
import tempfile
import unittest
from unittest import mock

import test_pvd_waves as base     # noqa: F401 -- its temp paths, before pvd_waves loads
import kalshi_reads               # test_pvd_waves' stand-in
import pvd_waves as pw

assert "pvdw_test_" in pw.REGISTRY and "pvdw_test_" in pw.PICK_STATE, (pw.REGISTRY, pw.PICK_STATE)

T0 = 1794707433.0     # 2026-11-15T01:50:33Z


def fill(ticker, side, n, px):
    return {"ticker": ticker, "ts": T0, "our_book_side": side, "count": n, "yes_price_cents": px,
            "pos_before": 0.0, "pos_after": n if side == "bid" else -n, "fill_id": ticker + "-f"}


class WideBookMark(unittest.TestCase):
    def scan(self, fills, touch, **kw):
        a = argparse.Namespace(name="all", hours=6.0, min_c=5.0, min_usd=25.0, series_trip=3, **kw)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(pw, "LIST", os.path.join(d, "none.json")), \
                mock.patch.object(pw, "STATE_FILE", os.path.join(d, "none.json")), \
                mock.patch.object(pw, "legacy_series", return_value={}), \
                mock.patch.object(pw, "wave_of", return_value=("w1111-statenom", True)), \
                mock.patch.object(pw, "fills_since", return_value=fills), \
                mock.patch.object(pw, "orderbook_exact_levels", return_value=([], [])), \
                mock.patch.object(pw, "external_touch", side_effect=lambda *_a: touch), \
                mock.patch.object(kalshi_reads, "kalshi_get_all", return_value=[], create=True), \
                mock.patch.object(kalshi_reads, "read", return_value={}, create=True):
            return {g["ticker"]: g for g in pw.pickoff_scan(a, {"waves": {}})["rows"]}

    def test_the_nh_long_shots(self):
        # our 200 asks at 2.9 / 3.1 / 3.5c, the book after: 2.8 x 53 -- the mid (27.9c) read
        # -$50; the outside bid (2.8c) is under our price, so nothing against us
        r = self.scan([fill("KXSENATENOM-NHD-28-COST", "ask", 200, 2.9)], (2.8, 53.0))["KXSENATENOM-NHD-28-COST"]
        self.assertTrue(r["wide"])
        self.assertEqual(r["mark_c"], 2.8)
        self.assertEqual((r["adverse_c"], r["adverse_usd"], r["flag"]), (0.0, 0.0, False))

    def test_a_wide_book_that_moved_through_us_still_flags(self):
        # an ask sold at 3c, then outside buyers bid 20c: 17c against us on 200 = $34
        r = self.scan([fill("X", "ask", 200, 3.0)], (20.0, 60.0))["X"]
        self.assertEqual((r["mark_c"], r["adverse_c"], r["flag"]), (20.0, 17.0, True))
        # a bid bought at 60c, then outside sellers ask 40c: 20c against us
        r = self.scan([fill("Y", "bid", 200, 60.0)], (5.0, 40.0))["Y"]
        self.assertEqual((r["mark_c"], r["adverse_c"], r["flag"]), (40.0, 20.0, True))

    def test_a_narrow_book_keeps_its_mid(self):
        r = self.scan([fill("Z", "bid", 500, 50.0)], (42.0, 46.0))["Z"]      # 6c x 500 = $30
        self.assertFalse(r["wide"])
        self.assertEqual((r["mark_c"], r["adverse_c"], r["flag"]), (44.0, 6.0, True))
        # exactly --wide-c wide is still narrow
        r = self.scan([fill("Z", "ask", 300, 50.0)], (50.0, 60.0))["Z"]
        self.assertEqual((r["wide"], r["mark_c"]), (False, 55.0))

    def test_the_threshold_is_a_flag(self):
        r = self.scan([fill("X", "ask", 200, 3.0)], (2.8, 53.0), wide_c=60.0)["X"]
        self.assertEqual((r["wide"], r["flag"]), (False, True))       # the old mid mark
        self.assertEqual(pw.WIDE_C, 10.0)

    def test_a_one_sided_book_is_unchanged(self):
        r = self.scan([fill("X", "ask", 200, 3.0)], (None, 12.0))["X"]
        self.assertEqual((r["wide"], r["mark_c"], r["adverse_c"]), (False, 12.0, 9.0))


if __name__ == "__main__":
    unittest.main()
