"""pvd_scan's per-wave budget (2026-11-14, Jack "Yes, do them" on the market-wave skill
update): with $691k of other waves' listings live, a new wave on the shared
PVD_SCAN_BUDGET + its own budget listed nothing. A registry wave's scan now also takes a
market while its OWN live listings stay within its own budget. No network."""
import sys
import unittest
from datetime import datetime, timedelta, timezone

# test_pvd_waves puts stand-ins for incentive_mm / kalshi_reads in sys.modules: import
# the real ones here, and put the stand-ins back after (as test_pvd_scan_tiers does)
_stubs = {m: sys.modules.pop(m) for m in ("incentive_mm", "kalshi_reads")
          if m in sys.modules and not getattr(sys.modules[m], "__file__", None)}
import pvd_scan as ps  # noqa: E402
sys.modules.update(_stubs)


def _x(t, coll, ext=False):
    return {"t": t, "coll": coll, "ext": ext, "rate": 1.0, "roi": 0.01}


class PickWithinBudget(unittest.TestCase):
    def setUp(self):
        self._b, self._o = ps.BUDGET, ps.OWN_BUDGET

    def tearDown(self):
        ps.BUDGET, ps.OWN_BUDGET = self._b, self._o

    def test_shared_pool_full_lists_nothing_without_an_own_budget(self):
        ps.BUDGET, ps.OWN_BUDGET = 350_000.0, 0.0
        taken, n_own = ps.pick_within_budget([_x("A", 100), _x("B", 100)], 691_453.0, 0.0)
        self.assertEqual(taken, [])
        self.assertEqual(n_own, 0)

    def test_own_budget_lists_when_the_shared_pool_is_full(self):
        ps.BUDGET, ps.OWN_BUDGET = 350_000.0, 50_000.0
        sc = [_x("A", 30_000), _x("B", 15_000), _x("C", 10_000)]
        taken, n_own = ps.pick_within_budget(sc, 691_453.0, 0.0)
        self.assertEqual([x["t"] for x in taken], ["A", "B"])   # C would pass 50k
        self.assertEqual(n_own, 2)

    def test_own_budget_counts_the_waves_live_listings(self):
        ps.BUDGET, ps.OWN_BUDGET = 350_000.0, 50_000.0
        taken, _n = ps.pick_within_budget([_x("A", 20_000)], 691_453.0, 40_000.0)
        self.assertEqual(taken, [])

    def test_shared_room_still_applies_first(self):
        ps.BUDGET, ps.OWN_BUDGET = 350_000.0, 50_000.0
        taken, n_own = ps.pick_within_budget([_x("A", 80_000)], 100_000.0, 0.0)
        self.assertEqual([x["t"] for x in taken], ["A"])        # under the shared rule, over its own
        self.assertEqual(n_own, 0)

    def test_extended_entries_are_never_charged(self):
        ps.BUDGET, ps.OWN_BUDGET = 1.0, 0.0
        taken, _n = ps.pick_within_budget([_x("E", 10_000, ext=True)], 5_000_000.0, 0.0)
        self.assertEqual([x["t"] for x in taken], ["E"])


class OwnLiveCollateral(unittest.TestCase):
    def setUp(self):
        self._own, self._pref = ps.OWN_SERIES, ps.SERIES_PREFIXES

    def tearDown(self):
        ps.OWN_SERIES, ps.SERIES_PREFIXES = self._own, self._pref

    def test_counts_only_this_waves_live_scan_adds(self):
        now = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)
        later = (now + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        gone = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        L = {"KXFOO-1": {"added": "scan 2026-11-14T10:00Z (new)", "stop": later, "coll_usd": 100},
             "KXFOO-2": {"added": "scan 2026-11-14T10:00Z (new)", "stop": gone, "coll_usd": 500},
             "KXFOO-3": {"added": "hand", "stop": later, "coll_usd": 700},
             "KXBAR-1": {"added": "scan 2026-11-14T10:00Z (new)", "stop": later, "coll_usd": 900},
             "KXFOO-4": {"added": "scan 2026-11-14T10:00Z (new)", "stop": later, "coll_usd": 40}}
        ps.OWN_SERIES, ps.SERIES_PREFIXES = ("KXFOO",), None
        self.assertEqual(ps.own_live_collateral(L, now, {"KXFOO-4": {}}), 100)
        ps.OWN_SERIES = ()
        self.assertEqual(ps.own_live_collateral(L, now, {}), 0.0)      # no wave: nothing is "own"


if __name__ == "__main__":
    unittest.main()
