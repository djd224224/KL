"""wave_detect's live-price ladder rule (2026-11-14): every "after economic events"
KX<asset>EVENT series in Commodities / Financials / Crypto is CHECK -- the 11/13 metals
came back FIT (Commodities). No network: kalshi_reads is a stand-in."""
import sys
import types
import unittest

if "kalshi_reads" not in sys.modules:
    _m = types.ModuleType("kalshi_reads")
    _m.kalshi_get = lambda *a, **k: {}
    _m.kalshi_get_all = lambda *a, **k: []
    sys.modules["kalshi_reads"] = _m
import wave_detect as wd  # noqa: E402


class LivePriceLadder(unittest.TestCase):
    def test_after_economic_events_series_are_ladders(self):
        for s, cat in (("KXGOLDEVENT", "Commodities"), ("KXCOPPEREVENT", "Commodities"),
                       ("KXUSDJPYEVENT", "Financials"), ("KXBTCEVENT", "Crypto"), ("KX10YRRATEEVENT", "Financials")):
            self.assertTrue(wd.live_price_ladder(s, cat), s)

    def test_other_series_are_not(self):
        for s, cat in (("KXSENATENOM", "Elections"), ("KXGOLDW", "Commodities"), ("KXSOMEEVENT", "Politics"),
                       ("KXEVENTS", "Commodities"), ("", "Crypto")):
            self.assertFalse(wd.live_price_ladder(s, cat), s)


if __name__ == "__main__":
    unittest.main()
