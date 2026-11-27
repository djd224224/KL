"""--size-mult-file tests for the scratch pvd_wave_tune.py (temp list, no network)."""
import importlib.util, io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name="pvd_wave_tune"):
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A, B = "KXGOLDEVENT-17NOV260930-T388000", "KXGOLDEVENT-17NOV260930-T390000"


def entry(**kw):
    e = {"bid_max": 40, "ask_min": 60, "size": 200, "bid_size": 2000, "stop": "2099-01-01T00:00:00Z",
         "period_start": "2026-11-13T18:01:49Z", "follow_book": True}
    e.update(kw)
    return e


class TickerMultTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lst = os.path.join(self.d, "list.json")
        json.dump({"markets": {A: entry(), B: entry()}}, open(self.lst, "w"))

    def run_tune(self, mult=None, extra=(), mod_name="pvd_wave_tune"):
        mod = load(mod_name)
        mod.LIST, mod.LOCK = self.lst, os.path.join(self.d, "scan.lock")
        mod.read_touches = lambda ts_: ({}, 0)
        args = ["x", "--series", "KXGOLDEVENT", "--since", "2026-11-13T17:30:00Z", "--write", *extra]
        if mult is not None:
            mf = os.path.join(self.d, "mult.json")
            with open(mf, "w") as fh:
                fh.write(mult if isinstance(mult, str) else json.dumps(mult))
            args += ["--size-mult-file", mf]
        old, sys.argv = sys.argv, args
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.argv = old
        with open(self.lst) as fh:
            return json.load(fh)["markets"], buf.getvalue()

    def test_scales_only_the_listed_ticker_once(self):
        L, out = self.run_tune({A: 2.0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["size_mult"]), (400, 4000, 2.0))
        self.assertEqual((L[B]["size"], L[B]["bid_size"]), (200, 2000))
        self.assertNotIn("size_mult", L[B])
        self.assertIn("1 markets with their own multiple", out)
        L, _ = self.run_tune({A: 2.0})                          # idempotent
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (400, 4000))

    def test_dropping_a_ticker_scales_it_back(self):
        self.run_tune({A: 2.5})
        L, _ = self.run_tune({})
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (200, 2000))
        self.assertNotIn("size_mult", L[A])

    def test_combines_with_the_wave_multiple(self):
        L, _ = self.run_tune({A: 2.0}, extra=("--size-mult", "1.5"))
        self.assertEqual((L[A]["size"], L[A]["size_mult"]), (600, 3.0))
        self.assertEqual((L[B]["size"], L[B]["size_mult"]), (300, 1.5))

    def test_zero_is_off(self):
        L, out = self.run_tune({A: 0, B: 2.0})
        self.assertEqual(L[A]["size"], 0)
        # both sides 0: the bot reads "size": 0 alone as its default 40 a side
        self.assertEqual((L[A]["bid_size"], L[A]["ask_size"]), (0, 0))
        self.assertEqual(L[B]["size"], 400)
        L, _ = self.run_tune({A: 0, B: 2.0})                    # idempotent
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["ask_size"]), (0, 0, 0))

    def test_an_old_size_only_off_gets_both_sides_zeroed(self):
        # an entry pinned off before the fix: "size": 0, no side sizes, sizes in "pre_off"
        L, _ = self.run_tune({A: 0})
        for k in ("bid_size", "ask_size", "cheap_sides"):
            L[A].pop(k, None)
        json.dump({"markets": L}, open(self.lst, "w"))
        L, _ = self.run_tune({A: 0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["ask_size"]), (0, 0, 0))
        L, _ = self.run_tune({A: 1.0})                          # lifting still restores
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (200, 2000))
        self.assertNotIn("ask_size", L[A])

    def test_zero_then_back_on_restores_the_sizes(self):
        self.run_tune({A: 2.0})
        L, _ = self.run_tune({A: 0})
        self.assertEqual(L[A]["size"], 0)
        L, _ = self.run_tune({A: 5.0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["size_mult"]), (1000, 10000, 5.0))
        self.assertNotIn("pinned", L[A])
        self.assertNotIn("ask_size", L[A])

    def test_unreadable_file_is_x1(self):
        L, out = self.run_tune("{not json")
        self.assertEqual(L[A]["size"], 200)


class TierReachTests(unittest.TestCase):
    """--tier-file (Jack 2026-11-19, the FX walls past the 15c tier: "Up to 25c on cheap tier
    multiplier if needed"): a market in it rests the last tier's multiple out to its C."""
    TIERS = ("--cheap-tiers", "2:10,5:5,15:5")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lst = os.path.join(self.d, "list.json")
        ents = {}
        for t in (A, B):
            e = entry(size=300)
            e.pop("bid_size")
            ents[t] = e
        json.dump({"markets": ents}, open(self.lst, "w"))
        self.tf = os.path.join(self.d, "tier.json")
        self.touch = {A: (21.0, 96.0, 0.0), B: (21.0, 96.0, 0.0)}

    def tier(self, d):
        with open(self.tf, "w") as fh:
            fh.write(d if isinstance(d, str) else json.dumps(d))

    def run_tune(self, extra=()):
        mod = load()
        mod.LIST, mod.LOCK = self.lst, os.path.join(self.d, "scan.lock")
        mod.read_touches = lambda ts_: (dict(self.touch), 0)
        old, sys.argv = sys.argv, ["x", "--series", "KXGOLDEVENT", "--since", "2026-11-13T17:30:00Z", "--write",
                                   *self.TIERS, *extra]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.argv = old
        with open(self.lst) as fh:
            return json.load(fh)["markets"], buf.getvalue()

    def test_only_a_market_in_the_file_reaches_past_the_last_tier(self):
        self.tier({"to": 25, "markets": {A: {"why": "x"}}})
        L, out = self.run_tune(("--tier-file", self.tf))
        self.assertEqual((L[A]["bid_size"], L[A]["ask_size"]), (1500, 1500))     # 21c bid x5 by the reach
        self.assertIn("bid 1500 (x5, tier to 25c)", L[A]["cheap"])
        self.assertIn("ask 1500 (x5)", L[A]["cheap"])
        self.assertEqual(L[B]["ask_size"], 1500)                                 # 4c ask: the 5c tier
        self.assertNotIn("bid_size", L[B])                                       # 21c bid: the size, 300
        self.assertIn("tier reach (--tier-file): 1 markets out to 25c, 1 side(s) of 1 at x5 by it", out)

    def test_never_past_its_c_and_back_to_the_size_when_it_goes(self):
        self.tier({"to": 25, "markets": {A: {}}})
        self.touch[A] = (27.0, 96.0, 0.0)
        L, _ = self.run_tune(("--tier-file", self.tf))
        self.assertNotIn("bid_size", L[A])                                       # 27c: past the reach
        self.touch[A] = (21.0, 96.0, 0.0)
        L, _ = self.run_tune(("--tier-file", self.tf))
        self.assertEqual(L[A]["bid_size"], 1500)
        self.tier({"to": 25, "markets": {}})                                     # the reach went
        L, _ = self.run_tune(("--tier-file", self.tf))
        self.assertNotIn("bid_size", L[A])
        self.assertEqual(L[A]["ask_size"], 1500)

    def test_no_reach_without_the_flag_a_file_a_c_or_a_tier(self):
        self.tier({"to": 25, "markets": {A: {}}})
        L, _ = self.run_tune()                                                   # no --tier-file
        self.assertNotIn("bid_size", L[A])
        self.tier({"to": 0, "markets": {A: {}}})                                 # "to" 0: off
        L, _ = self.run_tune(("--tier-file", self.tf))
        self.assertNotIn("bid_size", L[A])
        os.remove(self.tf)                                                       # nothing granted yet
        L, out = self.run_tune(("--tier-file", self.tf))
        self.assertNotIn("bid_size", L[A])
        self.assertIn("tier reach (--tier-file): 0 markets", out)
        self.tier("{not json")
        L, out = self.run_tune(("--tier-file", self.tf))
        self.assertNotIn("bid_size", L[A])
        self.assertIn("tier file unreadable", out)


class CostMultTests(unittest.TestCase):
    """--cost-mult (Jack 2026-11-26 on the 2028 R-primary long shots at a 1c wall: "5x only on
    1c though"): a side whose touch costs <= C rests as if its market's multiple were M."""
    R1, R2 = "KXRPRESPRIMARY-28NM-TCAR", "KXRPRESPRIMARY-28TX-TCAR"
    TIERS = ("--cheap-tiers", "2:15,7.5:15,15:6.75", "--side-cap", "ALL:90.01:300")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lst = os.path.join(self.d, "list.json")
        ents = {}
        for t in (self.R1, self.R2):
            e = entry(size=285)
            e.pop("bid_size")
            ents[t] = e
        json.dump({"markets": ents}, open(self.lst, "w"))
        self.mf = os.path.join(self.d, "mult.json")
        json.dump({self.R1: 2.5}, open(self.mf, "w"))       # R2 stays x1
        self.touch = {self.R1: (1.0, 8.0, 0.0), self.R2: (1.0, 7.0, 0.0)}

    def run_tune(self, extra=(), env=None):
        mod = load()
        mod.LIST, mod.LOCK = self.lst, os.path.join(self.d, "scan.lock")
        mod.read_touches = lambda ts_: (dict(self.touch), 0)
        old, sys.argv = sys.argv, ["x", "--series", "KXRPRESPRIMARY", "--since", "2026-11-13T17:30:00Z",
                                   "--write", "--size-mult-file", self.mf, *self.TIERS, *extra]
        old_env = os.environ.get("PVD_COST_MULT")
        if env is not None:
            os.environ["PVD_COST_MULT"] = env
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.argv = old
            if env is not None:
                if old_env is None:
                    os.environ.pop("PVD_COST_MULT", None)
                else:
                    os.environ["PVD_COST_MULT"] = old_env
        with open(self.lst) as fh:
            return json.load(fh)["markets"], buf.getvalue()

    CM = ("--cost-mult", "KXRPRESPRIMARY:bid:1:5")

    def test_a_1c_bid_rests_as_x5_whatever_its_own_multiple(self):
        L, out = self.run_tune(self.CM)
        # R1 at x2.5: 285 x 2.5 = 712 a side -> 1c bid 712 x 15 x 2 (as x5 = 285 x 15 x 5)
        self.assertEqual(L[self.R1]["size"], 712)
        self.assertEqual(L[self.R1]["bid_size"], 21360)
        self.assertEqual(L[self.R2]["bid_size"], 21375)                     # x1 -> as x5: 285 x 15 x 5
        self.assertEqual((L[self.R1]["ask_size"], L[self.R2]["ask_size"]), (300, 300))   # 92-93c asks: the cap
        self.assertIn("<= 1c as x5", L[self.R1]["cheap"])
        self.assertIn("cost multiple (--cost-mult KXRPRESPRIMARY:bid:<=1c:x5): 2 side(s) of 2 markets", out)

    def test_only_at_1c_back_to_its_own_size_off_it(self):
        self.run_tune(self.CM)
        self.touch[self.R1] = (3.0, 8.0, 0.0)                              # the bids came back to 3c
        L, _ = self.run_tune(self.CM)
        self.assertEqual(L[self.R1]["bid_size"], 10680)                     # 712 x 15: x2.5 again
        self.assertEqual(L[self.R2]["bid_size"], 21375)                     # still at 1c
        self.touch[self.R1] = (1.5, 8.0, 0.0)                              # 1.5c is past 1c
        L, _ = self.run_tune(self.CM)
        self.assertEqual(L[self.R1]["bid_size"], 10680)

    def test_never_lowers_a_market_already_past_m(self):
        json.dump({self.R1: 6.0}, open(self.mf, "w"))
        L, _ = self.run_tune(self.CM)
        self.assertEqual(L[self.R1]["bid_size"], 25650)                     # 1710 x 15: its own x6

    def test_other_series_sides_and_the_kill_switch(self):
        L, _ = self.run_tune(("--cost-mult", "KXDPRESPRIMARY:bid:1:5"))    # another series
        self.assertEqual(L[self.R1]["bid_size"], 10680)
        L, _ = self.run_tune(("--cost-mult", "KXRPRESPRIMARY:ask:1:5"))    # asks only: none cost 1c
        self.assertEqual(L[self.R1]["bid_size"], 10680)
        L, out = self.run_tune(self.CM, env="0")
        self.assertEqual(L[self.R1]["bid_size"], 10680)
        self.assertIn("cost multiple: off (PVD_COST_MULT=0)", out)
        L, out = self.run_tune()                                            # flag dropped
        self.assertEqual(L[self.R1]["bid_size"], 10680)
        self.assertNotIn("cost multiple", out)

    def test_an_off_market_stays_off(self):
        json.dump({self.R1: 0}, open(self.mf, "w"))
        L, _ = self.run_tune(self.CM)
        self.assertEqual((L[self.R1]["bid_size"], L[self.R1]["ask_size"]), (0, 0))



class SideMultTests(unittest.TestCase):
    """--side-mult-file (Jack 2026-11-27 on the 2028 wave's markets pacing under $1: "raise bids
    2x, asks by 1.25"): {ticker: {"bid": X, "ask": Y}}, applied last to what the side rests."""
    R1, R2 = "KXRPRESPRIMARY-28NM-TCAR", "KXRPRESPRIMARY-28TX-TCAR"
    TIERS = ("--cheap-tiers", "2:15,7.5:15,15:6.75", "--side-cap", "ALL:90.01:300",
             "--cost-mult", "KXRPRESPRIMARY:bid:1:5")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lst = os.path.join(self.d, "list.json")
        ents = {}
        for t in (self.R1, self.R2):
            e = entry(size=285)
            e.pop("bid_size")
            ents[t] = e
        json.dump({"markets": ents}, open(self.lst, "w"))
        self.mf = os.path.join(self.d, "mult.json")
        json.dump({self.R1: 2.5}, open(self.mf, "w"))       # R2 stays x1
        self.sf = os.path.join(self.d, "side.json")
        self.side({self.R1: {"bid": 2, "ask": 1.25}})
        self.touch = {self.R1: (1.0, 8.0, 0.0), self.R2: (1.0, 7.0, 0.0)}

    def side(self, d):
        with open(self.sf, "w") as fh:
            fh.write(d if isinstance(d, str) else json.dumps(d))

    def run_tune(self, extra=(), env=None, tiers=None):
        mod = load()
        mod.LIST, mod.LOCK = self.lst, os.path.join(self.d, "scan.lock")
        mod.read_touches = lambda ts_: (dict(self.touch), 0)
        old, sys.argv = sys.argv, ["x", "--series", "KXRPRESPRIMARY", "--since", "2026-11-13T17:30:00Z",
                                   "--write", "--size-mult-file", self.mf, "--side-mult-file", self.sf,
                                   *(self.TIERS if tiers is None else tiers), *extra]
        old_env = os.environ.get("PVD_SIDE_MULT")
        if env is not None:
            os.environ["PVD_SIDE_MULT"] = env
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.argv = old
            if env is not None:
                if old_env is None:
                    os.environ.pop("PVD_SIDE_MULT", None)
                else:
                    os.environ["PVD_SIDE_MULT"] = old_env
        with open(self.lst) as fh:
            return json.load(fh)["markets"], buf.getvalue()

    def test_bids_x2_and_the_capped_ask_x1_25_on_the_listed_market_only(self):
        L, out = self.run_tune()
        # R1 x2.5 own: 712 a side; 1c bid as x5 = 21,360, x2 = 42,720; the 92c ask: cap 300 x 1.25
        self.assertEqual((L[self.R1]["bid_size"], L[self.R1]["ask_size"]), (42720, 375))
        self.assertEqual((L[self.R2]["bid_size"], L[self.R2]["ask_size"]), (21375, 300))
        self.assertIn("side x2", L[self.R1]["cheap"])
        self.assertIn("side x1.25", L[self.R1]["cheap"])
        self.assertIn("side multiples (--side-mult-file, 1 markets): 2 side(s) of 1 markets", out)
        L, _ = self.run_tune()                                              # idempotent
        self.assertEqual((L[self.R1]["bid_size"], L[self.R1]["ask_size"]), (42720, 375))

    def test_dropping_the_ticker_or_the_kill_switch_takes_it_back(self):
        self.run_tune()
        L, out = self.run_tune(env="0")
        self.assertEqual((L[self.R1]["bid_size"], L[self.R1]["ask_size"]), (21360, 300))
        self.assertIn("off (PVD_SIDE_MULT=0)", out)
        self.run_tune()
        self.side({"_note": "emptied"})
        L, _ = self.run_tune()
        self.assertEqual((L[self.R1]["bid_size"], L[self.R1]["ask_size"]), (21360, 300))

    def test_a_plain_side_rests_its_size_times_x_and_goes_back(self):
        self.touch[self.R2] = (40.0, 60.0, 0.0)                            # no tier, under the cap
        self.side({self.R2: {"bid": 2, "ask": 1.25}})
        L, _ = self.run_tune()
        self.assertEqual((L[self.R2]["bid_size"], L[self.R2]["ask_size"]), (570, 356))
        self.side({})
        L, _ = self.run_tune()
        self.assertNotIn("bid_size", L[self.R2])
        self.assertNotIn("ask_size", L[self.R2])
        self.assertEqual(L[self.R2]["size"], 285)

    def test_an_off_cap_stays_off_and_a_pin_is_left_alone(self):
        L, _ = self.run_tune(tiers=("--cheap-tiers", "2:15", "--side-cap", "ALL:90.01:0"))
        self.assertEqual(L[self.R1]["ask_size"], 0)                        # 0 is OFF: x1.25 is still 0
        self.assertEqual(L[self.R1]["bid_size"], 712 * 15 * 2)
        L, _ = self.run_tune(extra=("--pin", f"{self.R1}:200"))
        self.assertEqual(L[self.R1]["size"], 200)
        self.assertEqual(L[self.R1]["ask_size"], 300)                      # the cap holds on a pin, unscaled
        self.assertNotIn("bid_size", L[self.R1])

    def test_a_side_with_no_touch_and_an_unreadable_file(self):
        self.touch[self.R1] = (0.0, 8.0, 0.0)                              # no YES bid on the book
        L, _ = self.run_tune()
        self.assertNotIn("bid_size", L[self.R1])
        self.assertEqual(L[self.R1]["ask_size"], 375)
        self.side("{not json")
        L, out = self.run_tune()
        self.assertEqual(L[self.R1]["ask_size"], 300)
        self.assertIn("unreadable", out)


if __name__ == "__main__":
    unittest.main()
