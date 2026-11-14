"""Tests for crypto_event_fair (Jack 2026-11-13: "you can quote both sides on
these, built around fair value"). No network: the feeds are injected."""

import math
import unittest

import crypto_event_fair as cef
import crypto_updown_mm as ud

EVENT_RULES = ("If the simple average of CF Benchmarks' Bitcoin Real-Time Index "
               "(BRTI) over the 60 seconds immediately before 3:30pm EST on Dec 9, "
               "2026 is above $78000 per bitcoin, then the market resolves to Yes.")
TOUCH_RULES = ("If a qualifying one-minute average of the CF Bitcoin Real-Time Index "
               "(BRTI) is above $120,000 after issuance at Nov 10, 2026 at 12:00 PM "
               "EST and before Jan 1, 2027 at 12:00 AM EST, then the market resolves to Yes.")
NOW = 1794617729.0                      # 2026-11-14 ~01:00Z
CLOSE = "2026-12-09T20:30:00Z"


def market(ticker="KXBTCEVENT-09DEC261530-T7800000", strike=78000, rules=EVENT_RULES,
           early=None, st="greater", close=CLOSE):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
            "strike_type": st, "floor_strike": strike, "cap_strike": None,
            "rules_primary": rules, "early_close_condition": early,
            "close_time": close}


class TestShapes(unittest.TestCase):
    def test_series_asset(self):
        self.assertEqual(cef.series_asset("KXBTCEVENT"), "BTC")
        self.assertEqual(cef.series_asset("KXHYPEEVENT"), "HYPE")
        self.assertIsNone(cef.series_asset("KXBTCHITBY"))
        self.assertIsNone(cef.series_asset("KXFOOEVENT"))        # no fleet pair

    def test_only_terminal_strikes_price(self):
        self.assertEqual(cef.terminal_shape(market()), ("greater", 78000.0, None))
        # the touch markets: early close, "before Jan 1"
        self.assertEqual(cef.terminal_shape(market(rules=TOUCH_RULES, early="touch")),
                         (None, None, None))
        self.assertEqual(cef.terminal_shape(market(rules=TOUCH_RULES)), (None, None, None))
        self.assertEqual(cef.terminal_shape(market(st="custom")), (None, None, None))
        for rules in (  # the ETH / SOL / HYPE wording (11/14)
                "If the simple average of CF Benchmarks' CME CF Ether-Dollar Real Time Index "
                "(ETHUSD_RTI) over the 60 seconds immediately before 11:00am EST on Dec 1, "
                "2026 is above $2100 per unit of Ethereum (ETH), then the market resolves to Yes.",
                "If the simple average of CF Benchmarks' CF Hyperliquid-Dollar Spot Rate "
                "(HYPEUSD_RTI) over the 60 seconds immediately before 9:30am EST on Nov 17, "
                "2026 is above $70 per unit of HYPE (HYPE), then the market resolves to Yes."):
            self.assertEqual(cef.terminal_shape(market(rules=rules))[0], "greater")
        m = market(st="between")
        m["cap_strike"] = 80000
        self.assertEqual(cef.terminal_shape(m), ("between", 78000.0, 80000.0))

    def test_the_fleets_terminal_model(self):
        for spot, k, sig, t in ((68238.6, 78000, 0.0211, 25.82), (68238.6, 70000, 0.0211, 3.57),
                                (1929.0, 1800, 0.0338, 10.0)):
            self.assertAlmostEqual(cef.terminal_prob(spot, k, sig, t),
                                   ud.terminal_prob(spot, k, sig, t), places=12)
        self.assertEqual(cef.terminal_prob(100, 90, 0.02, 0.0), 1.0)

    def test_the_band_holds_the_fair_and_widens_when_the_spot_matters(self):
        p, lo, hi = cef.price_band("greater", 71500.0, None, 68238.6, 0.0211, 25.82)
        self.assertLess(lo, p)
        self.assertLess(p, hi)
        self.assertAlmostEqual(100 * p, 31.2, delta=0.2)          # the 01:00Z read
        p3, lo3, hi3 = cef.price_band("greater", 69000.0, None, 68238.6, 0.0211, 3.57)
        self.assertGreater(hi3 - lo3, hi - lo)                   # 3.6 days: wider
        # a 'between' bucket's grid includes the middle, so its peak is in it
        p, lo, hi = cef.price_band("between", 67000.0, 69000.0, 68000.0, 0.0211, 3.0)
        self.assertLessEqual(lo, p)
        self.assertLessEqual(p, hi)


class TestWatch(unittest.TestCase):
    def _watch(self, prices, markets=None, closes=None):
        calls = []
        prices = list(prices)
        mk = markets if markets is not None else [market(), market(
            "KXBTCHITBY-01JAN270000-12000000-YES", 120000, TOUCH_RULES, "touch")]

        def get_json(path, params):
            calls.append((path, dict(params)))
            if params.get("series_ticker") == "KXBTCEVENT":
                return {"markets": [m for m in mk if m["ticker"].startswith("KXBTCEVENT")]}
            return {"markets": []}

        def price(asset, prev):
            v = prices.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        cl = closes or [60000 * math.exp(0.02 * math.sin(i)) for i in range(400)]
        w = cef.CryptoEventWatch(get_json, lambda: ["KXBTCEVENT", "KXBTCHITBY"],
                                 fetch_price=price, fetch_closes=lambda a, n: cl)
        return w, calls

    def test_refresh_prices_the_terminal_strikes(self):
        w, calls = self._watch([68238.6])
        snap = w.refresh(NOW)
        self.assertEqual([c[1]["series_ticker"] for c in calls], ["KXBTCEVENT"])   # EVENT only
        e = snap["markets"]["KXBTCEVENT-09DEC261530-T7800000"]
        self.assertEqual((e["asset"], e["kind"], e["spot"]), ("BTC", "greater", 68238.6))
        self.assertLess(e["p_lo"], e["p"])
        self.assertLess(e["p"], e["p_hi"])
        self.assertAlmostEqual(e["t_days"], (1796848200.0 - NOW) / 86400.0, places=3)
        self.assertIsNone(snap["assets"]["BTC"]["hold_until"])
        self.assertEqual(snap["errors"], [])

    def test_a_jump_holds_the_asset(self):
        w, _ = self._watch([68000.0, 68100.0, 68900.0, 68950.0])
        w.refresh(NOW)
        w.refresh(NOW + 30)
        snap = w.refresh(NOW + 60)                    # +1.3% inside 5 min
        self.assertEqual(snap["assets"]["BTC"]["hold_until"], NOW + 60 + cef.JUMP_HOLD_SECS)
        self.assertEqual(snap["markets"]["KXBTCEVENT-09DEC261530-T7800000"]["hold_until"],
                         NOW + 60 + cef.JUMP_HOLD_SECS)
        snap = w.refresh(NOW + 400)                   # the window has moved on
        self.assertEqual(snap["assets"]["BTC"]["hold_until"], NOW + 60 + cef.JUMP_HOLD_SECS)

    def test_a_failed_spot_ages_and_a_first_one_errs(self):
        w, _ = self._watch([RuntimeError("down")])
        snap = w.refresh(NOW)
        self.assertIn("err", snap["markets"]["KXBTCEVENT-09DEC261530-T7800000"])
        w, _ = self._watch([68000.0, RuntimeError("down")])
        w.refresh(NOW)
        snap = w.refresh(NOW + 30)
        e = snap["markets"]["KXBTCEVENT-09DEC261530-T7800000"]
        self.assertEqual(e["spot_ts"], NOW)           # the gate's TTL ages it out
        self.assertTrue(any("spot" in x for x in snap["errors"]))


if __name__ == "__main__":
    unittest.main()
