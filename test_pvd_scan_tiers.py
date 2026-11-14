"""pvd_scan.py judges a registry wave's market the way its tune will rest it (Jack
2026-11-11: "should quote larger size if <=5c, otherwise if still under the $1.1 bar
then yes dont quote it"): cheap sides at the wave's --cheap-tiers size, quotable sides
at its own band / cushion / follow-the-book. No network: the reads are stubbed."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# test_pvd_waves puts stand-ins for incentive_mm / kalshi_reads in sys.modules: import
# the real ones for pvd_scan here, then put the stand-ins back for that module's tests
_stubs = {m: sys.modules.pop(m) for m in ("incentive_mm", "kalshi_reads")
          if m in sys.modules and not getattr(sys.modules[m], "__file__", None)}
import incentive_mm as imm  # noqa: E402
import pvd_scan as ps  # noqa: E402
sys.modules.update(_stubs)

T = "KXFOOPRIMARY-28XX-ABC"


def book(yes, no):
    return {"orderbook_fp": {"yes_dollars": [[f"{p / 100:.2f}", str(q)] for p, q in yes],
                             "no_dollars": [[f"{p / 100:.2f}", str(q)] for p, q in no]}}


class _Client:
    def get(self, path, params=None):
        return {"orders": []}


class CheapSideTiers(unittest.TestCase):
    # a long shot: 5,000 bid at 1c, 3,000 offered at 2c (NO bid 98)
    YES, NO = [(1, 5000.0)], [(98, 3000.0)]

    def score(self, pd, **env):
        now = datetime.now(timezone.utc)
        cands = {T: {"start": now - timedelta(hours=1), "end": now + timedelta(hours=24), "pd": pd,
                     "target": 1000.0, "df": 0.5, "extend": False}}
        mk = {"ticker": T, "event_ticker": "KXFOOPRIMARY-28XX", "status": "active",
              "last_price_dollars": None}

        def pget(path, params=None):
            if path == "/markets":
                return {"markets": [mk]}
            return book(self.YES, self.NO)
        knobs = dict(VOTE_CHECK=False, LIVE_SIZE=200, DEAD_SIZE=200, BAR=1.10, SIM_HOURS=0, REPORT=None,
                     BUDGET=1e9, CHEAP_TIERS=[], TIER_SKIP=set(), BAND_LO=3, BAND_HI=97,
                     WAVE_CUSHION=None, WAVE_FOLLOW_BOOK=False)
        knobs.update(env)
        summary = {}
        with mock.patch.multiple(ps, pget=pget, **knobs), mock.patch.object(ps.time, "sleep", lambda s: None), \
                mock.patch.object(ps, "log", lambda *a, **k: None):
            taken = ps.score_and_pick(_Client(), cands, {}, now, summary)
        return taken, summary

    def rates(self):
        y, n = [[p, q] for p, q in self.YES], [[p, q] for p, q in self.NO]
        f = lambda b, a: imm.estimate_reward_share(y, n, [("bid", 1, b), ("ask", 2, a)], 1000.0, 0.5,
                                                   own_in_book=False)[0]
        return f(200, 200), f(2000, 200)

    def test_tier_parse(self):
        with mock.patch.object(ps, "CHEAP_TIERS", [(2.0, 10.0), (5.0, 5.0)]):
            self.assertEqual((ps.tier_mult(1), ps.tier_mult(2), ps.tier_mult(4.9), ps.tier_mult(6)),
                             (10.0, 10.0, 5.0, 1.0))

    def test_a_cheap_side_is_judged_at_the_wave_size(self):
        flat, tier = self.rates()
        self.assertGreater(tier, flat)
        pd = 1.10 / ((flat + tier) / 2)          # 24 h: under the bar at 200, over it at 2,000 on the 1c bid
        wave = dict(BAND_LO=1, BAND_HI=99, WAVE_CUSHION=0, WAVE_FOLLOW_BOOK=True)
        # the old scan: a 1c bid and a 2c ask sit outside 3-97c -- no quotable side at all
        taken, s = self.score(pd)
        self.assertEqual((taken, s["skipped"]["no quotable side"]), ([], 1))
        # the wave's own band, flat 200 a side: under the bar, left out
        taken, s = self.score(pd, **wave)
        self.assertEqual((taken, s["skipped"]["under bar"]), ([], 1))
        # ...with its cheap tiers: the 1c bid at 10 x 200 clears it -- listed at the FLAT
        # size (the tune sizes the cheap side), collateral counted at the tier size
        taken, s = self.score(pd, CHEAP_TIERS=[(2.0, 10.0), (5.0, 5.0)], **wave)
        self.assertEqual(len(taken), 1)
        x = taken[0]
        self.assertEqual((x["bs"], x["as_"], x["size"], x["tiered"]), (200, 200, 200, (2000, 200)))
        self.assertAlmostEqual(x["coll"], 2000 * 1 / 100 + 200 * 98 / 100)
        self.assertEqual(s["tiered_taken"], 1)
        # a pinned series takes no tiers: under the bar again
        taken, _s = self.score(pd, CHEAP_TIERS=[(2.0, 10.0)], TIER_SKIP={"KXFOOPRIMARY"}, **wave)
        self.assertEqual(taken, [])
        # still under the bar even at the tier size: left out ("otherwise ... dont quote it")
        taken, s = self.score(pd * flat / tier * 0.9, CHEAP_TIERS=[(2.0, 10.0), (5.0, 5.0)], **wave)
        self.assertEqual((taken, s["skipped"]["under bar"]), ([], 1))


if __name__ == "__main__":
    unittest.main()
