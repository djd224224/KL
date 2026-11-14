"""wave_fair.py: the Friday-close reference and the per-source fallback (no network)."""
import importlib.util, os, sys, unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wave_fair_t", os.path.join(HERE, "wave_fair.py"))
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)
NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)


def ok(ref=1.0, cur=1.01):
    return lambda sym: (ref, cur)


def boom(sym):
    raise OSError("down")


class WaveFairTests(unittest.TestCase):
    def test_ref_is_the_median_of_the_four_close_bars(self):
        bars = {t: v for t, v in zip(wf.REF_STARTS, [1.42793, 1.42792, 1.42711, 1.42791])}
        self.assertAlmostEqual(wf._ref(bars), 1.427915)
        bars[wf.T0] = 9.9                                  # a bar after the close is ignored
        self.assertAlmostEqual(wf._ref(bars), 1.427915)

    def test_ref_needs_two_bars(self):
        with self.assertRaises(ValueError):
            wf._ref({wf.REF_STARTS[0]: 1.0})

    def test_every_series_has_a_source(self):
        self.assertEqual(set(wf.PROXY), set(wf.CLOSE))

    def test_all_sources_up(self):
        live, failed = wf.live_moves({}, NOW, {"hl": ok(), "kraken": ok(), "bitso": ok()})
        self.assertEqual(failed, {})
        self.assertEqual(len(live), 9)
        self.assertAlmostEqual(live["KXGOLDEVENT"]["ratio"], 1.01)

    def test_a_failed_source_reuses_a_fresh_previous_price(self):
        prev = {"at": (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "live": {"KXUSDMXNEVENT": {"src": "bitso:usd_mxn", "ref": 18.54, "now": 18.6, "ratio": 1.003}}}
        live, failed = wf.live_moves(prev, NOW, {"hl": ok(), "kraken": ok(), "bitso": boom})
        self.assertEqual(failed, {})
        self.assertAlmostEqual(live["KXUSDMXNEVENT"]["ratio"], 1.003)
        self.assertIn("from_prev", live["KXUSDMXNEVENT"])

    def test_a_failed_source_with_a_stale_previous_price_gets_no_fair(self):
        prev = {"at": (NOW - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "live": {"KXUSDMXNEVENT": {"ratio": 1.003}}}
        live, failed = wf.live_moves(prev, NOW, {"hl": ok(), "kraken": ok(), "bitso": boom})
        self.assertEqual(set(failed), {"KXUSDMXNEVENT"})
        self.assertNotIn("KXUSDMXNEVENT", live)

    def test_bad_prices_count_as_a_failure(self):
        live, failed = wf.live_moves({}, NOW, {"hl": ok(), "kraken": ok(ref=0.0), "bitso": ok()})
        self.assertEqual(set(failed), {"KXAUDUSDEVENT", "KXUSDCADEVENT"})

    def test_weekend_hours_add_no_trading_time(self):
        sat = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(wf.trading_days(sat), 0.02 + wf.WEEKEND_GAP_DAYS)


if __name__ == "__main__":
    unittest.main()
