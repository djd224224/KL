#!/usr/bin/env python3
"""Tests for temp_mm_policy (the hourly-temp quoting policy, pure logic).

Constructed states only: no recording, no network, no orders. The sim-side
equivalence (shadow results bit-identical before / after the extraction) was
proven on the recorded 10/14-10/15 events and the test fixtures; here each
rule is pinned on hand-built books.

SANDBOXED like the other test_temp_* files: TEMP_SHADOW_DATA points at a
fresh temp dir before any temp_shadow module is imported (the cross-checks
import temp_shadow_scoring / temp_shadow_sim).

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_mm_policy -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tmp_pol_")

import random      # noqa: E402
import unittest    # noqa: E402

import temp_mm_policy as pol           # noqa: E402
import temp_shadow_common as C         # noqa: E402
import temp_shadow_scoring as scoring  # noqa: E402

assert C.DATA_ROOT == os.environ["TEMP_SHADOW_DATA"]

BY = {40: 500.0, 39: 1000.0}           # YES bids
BN = {45: 500.0, 44: 1000.0}           # NO bids (YES asks 55, 56)
NORISK = (1e9, 1e9, 1e9, 1e9)
T0 = 1_792_000_000.0 - (1_792_000_000.0 % 60.0)   # a minute boundary (epoch s)


def P(variant=None, **kw):
    return pol.make_policy(variant, **kw)


def bv(by=None, bn=None, target=1000.0):
    return pol.BookView(dict(BY if by is None else by), dict(BN if bn is None else bn), target)


def q(Pd, fair, book=None, slope=None, wflag=False, pflag=True, size=None, risk=NORISK,
      target=1000.0, tags=None, diag=None):
    if size is None:
        size = int(Pd["q_live"])
    return pol.quote_market(Pd, bv() if book is None else book, fair, slope, wflag, pflag,
                            size, risk, target, tags, diag)


class TestPolicyDicts(unittest.TestCase):
    def test_make_policy(self):
        self.assertEqual(P(), pol.DEFAULT_POLICY)
        f = P("F_full")
        self.assertEqual((f["q_live"], f["decided"], f["skew"], f["live"]), (600, True, 0.3, True))
        g = P("G_decided_only")
        self.assertEqual((g["live"], g["decided"], g["q_fav"]), (False, True, 300))
        j = P("J_decided_fav100")
        self.assertEqual((j["live"], j["decided"], j["q_fav"]), (False, True, 100))
        self.assertEqual(P("F_full", pos_cap=10)["pos_cap"], 10)
        with self.assertRaises(ValueError):
            P("Z_nope")
        with self.assertRaises(ValueError):
            P(q_lve=3)
        self.assertIsNot(P(), pol.DEFAULT_POLICY)       # a copy

    def test_variants_are_overrides_of_default(self):
        for name, ov in pol.VARIANTS.items():
            self.assertLessEqual(set(ov), set(pol.DEFAULT_POLICY), name)

    def test_sim_reexports_the_policy(self):
        import temp_shadow_sim as sim
        self.assertIs(sim.DEFAULT_POLICY, pol.DEFAULT_POLICY)
        self.assertIs(sim.VARIANTS, pol.VARIANTS)
        self.assertTrue(issubclass(sim._BookInfo, pol.BookView))
        self.assertIs(sim._g_tau, pol.g_tau)
        self.assertIs(sim._overlap_window, pol.overlap_window)
        self.assertEqual(pol.STEP_F, C.STEP_F)

    def test_decide_validates_the_policy(self):
        ctx = pol.EventContext(now=T0, close_ts=T0 + 1800)
        bad = dict(P())
        del bad["skew"]
        with self.assertRaises(ValueError):
            pol.decide(bad, ctx, [])
        with self.assertRaises(ValueError):
            pol.decide(P(window_mode="hide"), ctx, [])


class TestBookHelpers(unittest.TestCase):
    def test_same_rules_as_scoring(self):
        rng = random.Random(11)
        for _ in range(3000):
            by = {rng.randint(1, 99): float(rng.choice([0, 0.5, 5, 50, 150, 400, 900]))
                  for _ in range(rng.randint(0, 6))}
            bn = {rng.randint(1, 99): float(rng.choice([0, 0.5, 5, 50, 150, 400, 900]))
                  for _ in range(rng.randint(0, 6))}
            tgt = rng.choice([0.0, 250.0, 1000.0, 3000.0])
            self.assertEqual(pol.reference_price(by, tgt), scoring.reference_price(by, tgt))
            self.assertEqual(pol.best_price(by), scoring.best_price(by))
            for side in ("yes", "no", "yes_bid", "no_bid"):
                p = rng.randint(1, 99)
                self.assertEqual(pol.would_cross(by, bn, side, p),
                                 scoring.would_cross(by, bn, side, p))
        with self.assertRaises(ValueError):
            pol.would_cross({}, {}, "up", 5)

    def test_bookview(self):
        b = bv()
        self.assertEqual((b.best_y, b.best_n, b.ref_y, b.ref_n), (40, 45, 40, 45))
        self.assertEqual((b.sum_y, b.sum_n, b.mid, b.two_sided), (1500.0, 1500.0, 47.5, True))
        b = bv(by={30: 100.0, 29: 50.0}, bn={})
        self.assertEqual((b.ref_y, b.mid, b.two_sided, b.best_n), (None, 30.0, False, None))
        b = bv(by={}, bn={70: 10.0})
        self.assertEqual((b.mid, b.ref_n), (30.0, None))
        self.assertIsNone(bv(by={}, bn={}).mid)

    def test_external_book_strips_our_orders(self):
        y, n = pol.external_book({40: 800.0, 39: 1000.0, 1: 1300.0}, {45: 500.0},
                                 {40: 300.0, 1: 1300.0}, {45: 120.5})
        self.assertEqual(y, {40: 500.0, 39: 1000.0})
        self.assertEqual(n, {45: 379.5})
        self.assertEqual(pol.external_book({}, None), ({}, {}))


class TestTimeWindowProgram(unittest.TestCase):
    def test_g_tau_taper(self):
        p = P()
        self.assertEqual(pol.g_tau(p, 59.0), 1.0)
        self.assertEqual(pol.g_tau(p, 40.0), 1.0)
        self.assertAlmostEqual(pol.g_tau(p, 21.5), 0.3 + 0.7 * 0.5)
        self.assertAlmostEqual(pol.g_tau(p, 3.0), 0.3)
        self.assertEqual(pol.g_tau(p, 2.99), 0.0)
        self.assertEqual(pol.g_tau(P(live_tau_full=3.0), 3.5), 1.0)
        self.assertEqual(pol.live_size(P("F_full"), 21.5), 390)
        self.assertEqual(pol.live_size(P(), 50.0), 300)
        self.assertEqual(pol.live_size(P(), 1.0), 0)

    def test_overlap_window(self):
        ow = pol.overlap_window
        self.assertFalse(ow(T0 + 10.0, T0 + 11.0, 20.0, 45.0))
        self.assertTrue(ow(T0 + 19.65, T0 + 20.65, 20.0, 45.0))     # touches :20
        self.assertFalse(ow(T0 + 18.65, T0 + 19.65, 20.0, 45.0))
        self.assertTrue(ow(T0 + 45.9, T0 + 46.9, 20.0, 45.0))       # [20, 46)
        self.assertFalse(ow(T0 + 46.0, T0 + 47.0, 20.0, 45.0))
        self.assertTrue(ow(T0 + 50.0, T0 + 81.0, 20.0, 45.0))       # next minute's :20
        self.assertFalse(ow(T0 + 50.0, T0 + 80.0, 20.0, 45.0))

    def test_window_flag_modes(self):
        a, b = T0 + 30.15, T0 + 31.15
        self.assertTrue(pol.window_flag(P(), a, b))
        self.assertTrue(pol.window_flag(P(window_mode="back"), a, b))
        self.assertFalse(pol.window_flag(P(window_mode="none"), a, b))
        self.assertFalse(pol.window_flag(P(), T0 + 5.15, T0 + 6.15))
        self.assertTrue(pol.window_flag(P(win_lo=0, win_hi=10), T0 + 5.15, T0 + 6.15))

    def test_program_flag(self):
        p = P()
        self.assertTrue(pol.program_flag(p, 100.0, 101.0, 100.5, 200.0, 300.0))
        self.assertFalse(pol.program_flag(p, 99.0, 100.0, 100.0, 200.0, 300.0))
        self.assertFalse(pol.program_flag(p, 200.0, 201.0, 100.0, 200.0, 300.0))
        self.assertFalse(pol.program_flag(p, 150.0, 151.0, None, None, 300.0))
        self.assertTrue(pol.program_flag(p, 150.0, 151.0, None, None, 300.0, active=True))
        self.assertFalse(pol.program_flag(p, 150.0, 151.0, 100.0, 200.0, 300.0, active=False))
        p2 = P(program_only=False)
        self.assertTrue(pol.program_flag(p2, 250.0, 251.0, 100.0, 200.0, 300.0, active=False))
        self.assertFalse(pol.program_flag(p2, 300.0, 301.0, 100.0, 200.0, 300.0))

    def test_classify_and_guard(self):
        self.assertEqual([pol.classify(x) for x in (0.10, 0.5, 0.90, 0.0999, 0.03, 0.97,
                                                     0.9701, 0.0299, 0.0, 1.0)],
                         ["live", "live", "live", "near", "near", "near",
                          "decided", "decided", "decided", "decided"])
        self.assertEqual(pol.guard_buffer(P(), None), 1.0)
        self.assertEqual(pol.guard_buffer(P(), 0.4), 1.0)
        self.assertEqual(pol.guard_buffer(P(), 3.5), 3.5)
        self.assertEqual(pol.guard_buffer(P(guard_k=2.0, guard_min_c=2), 1.5), 3.0)
        # plain-normal slope: at the centre 100 * 0.39894 / sigma * 0.36
        self.assertAlmostEqual(pol.normal_slope_c(80.0, 1.0, 80.0), 100 * 0.3989422804 * 0.36,
                               places=6)
        self.assertLess(pol.normal_slope_c(80.0, 1.0, 82.0), pol.normal_slope_c(80.0, 1.0, 80.5))

    def test_clean_inputs(self):
        self.assertIsNone(pol.clean_prob(None))
        self.assertIsNone(pol.clean_prob(float("nan")))
        self.assertEqual(pol.clean_prob(1.2), 1.0)
        self.assertEqual(pol.clean_prob(-0.1), 0.0)
        self.assertIsNone(pol.clean_slope(float("inf")))
        self.assertEqual(pol.clean_slope(2), 2.0)


class TestLiveQuotes(unittest.TestCase):
    def test_reference_price_with_guard(self):
        # refs 40 / 45; slope 3c -> YES capped at floor(42 - 3) = 39; NO cap
        # floor(58 - 3) = 55 leaves the 45 reference
        tags, diag = {}, {}
        dy, dn, rule = q(P(), 0.42, slope=3.0, tags=tags, diag=diag)
        self.assertEqual((dy, dn, rule), ({39: 300.0}, {45: 300.0}, "live"))
        self.assertEqual(tags, {("yes", 39): ["live"], ("no", 45): ["live"]})
        self.assertEqual((diag["cls"], diag["fair_src"], diag["buf"]), ("live", "model", 3.0))

    def test_guard_min_and_off(self):
        self.assertEqual(q(P(), 0.42)[0], {40: 300.0})                    # buf 1c: 41 > 40
        self.assertEqual(q(P(), 0.405)[0], {39: 300.0})                   # floor(40.5 - 1)
        self.assertEqual(q(P("H_noguard_pull"), 0.30, slope=5.0)[0], {40: 300.0})
        self.assertEqual(q(P(), 0.30, slope=5.0)[0], {25: 300.0})

    def test_landing_window_modes(self):
        dy, dn, rule = q(P("B_live300_pull"), 0.42, wflag=True)
        self.assertEqual((dy, dn, rule), ({}, {}, "live"))
        dy, dn, _ = q(P("C_live300_back"), 0.42, slope=3.0, wflag=True)
        self.assertEqual((dy, dn), ({37: 300.0}, {43: 300.0}))           # 2 ticks behind
        self.assertEqual(q(P("A_live300_nowin"), 0.42, wflag=True)[0], {40: 300.0})
        self.assertEqual(q(P("C_live300_back"), 0.42, wflag=False)[0], {40: 300.0})

    def test_size_variants(self):
        self.assertEqual(q(P("D_live600_pull"), 0.5)[1], {45: 600.0})
        self.assertEqual(q(P(), 0.5, size=0), ({}, {}, "live"))
        self.assertEqual(q(P(), 0.5, size=90)[0], {40: 90.0})

    def test_never_crosses(self):
        # best NO 62 caps a YES bid at 37 (the empty YES side prices off the
        # fair: 44 -> 37); the NO ref 62 is guarded down to floor(55 - 1);
        # the empty YES side gets a 1c qualification pad
        b = bv(by={}, bn={62: 900.0})
        dy, dn, _ = q(P(), 0.45, book=b)
        self.assertEqual(dy, {37: 300.0, 1: 750.0})
        self.assertEqual(dn, {54: 300.0})
        b = bv(by={}, bn={})
        dy, dn, _ = q(P(qual_pads=False), 0.45, book=b, slope=2.0)
        self.assertEqual((dy, dn), ({43: 300.0}, {53: 300.0}))
        for p_ in dy:
            for n_ in dn:
                self.assertLess(p_ + n_, 100)

    def test_no_fair_uses_mid_without_guard(self):
        tags, diag = {}, {}
        dy, dn, rule = q(P(), None, slope=9.0, tags=tags, diag=diag)
        self.assertEqual((dy, dn, rule), ({40: 300.0}, {45: 300.0}, "live"))
        self.assertEqual((diag["fair_src"], diag["guard"]), ("mid", False))
        self.assertEqual(q(P(), None, book=bv(by={}, bn={})), ({}, {}, None))

    def test_untrusted_or_outside_program(self):
        d = {}
        self.assertEqual(pol.quote_market(P(), None, 0.5, None, False, True, 300, NORISK,
                                          1000.0, None, d), ({}, {}, None))
        self.assertEqual(d["reason"], "untrusted_book")
        d = {}
        self.assertEqual(q(P(), 0.5, pflag=False, diag=d), ({}, {}, None))
        self.assertEqual(d["reason"], "no_program")

    def test_decided_only_variants_skip_live_strikes(self):
        for v in ("G_decided_only", "I_pads_only", "J_decided_fav100", "L_decided_pad3000"):
            self.assertEqual(q(P(v), 0.42), ({}, {}, None), v)


class TestDecidedStrikes(unittest.TestCase):
    DBY = {2: 300.0, 1: 2000.0}
    DBN = {97: 800.0, 96: 1500.0}

    def book(self):
        return bv(by=self.DBY, bn=self.DBN)

    def test_longshot_pad_and_favorite_join(self):
        tags = {}
        dy, dn, rule = q(P("G_decided_only"), 0.01, book=self.book(), tags=tags)
        self.assertEqual((dy, dn, rule), ({1: 1000.0}, {97: 300.0}, "decided"))
        self.assertEqual(tags, {("yes", 1): ["pad"], ("no", 97): ["fav"]})
        # mirrored: a 99% strike pads NO at 1c, joins YES
        dy, dn, _ = q(P("G_decided_only"), 0.99, book=bv(by={97: 800.0}, bn={2: 300.0, 1: 50.0}))
        self.assertEqual((dy, dn), ({97: 300.0}, {1: 1000.0}))

    def test_pad_and_fav_size_variants(self):
        b = self.book
        self.assertEqual(q(P("I_pads_only"), 0.01, book=b())[:2], ({1: 1000.0}, {}))
        self.assertEqual(q(P("J_decided_fav100"), 0.01, book=b())[:2], ({1: 1000.0}, {97: 100.0}))
        self.assertEqual(q(P("L_decided_pad3000"), 0.01, book=b())[:2], ({1: 3000.0}, {97: 300.0}))
        self.assertEqual(q(P("G_decided_only", q_pad=0), 0.01, book=b())[:2], ({}, {97: 300.0}))

    def test_favorite_is_always_guarded(self):
        # fav fair 0.975 with a 3c slope: cap floor(97.5 - 3) = 94 < ref 97,
        # even with fair_guard off (the favorite join is guarded by spec)
        for v in ("G_decided_only", "H_noguard_pull"):
            dy, dn, _ = q(P(v, decided=True), 0.025, book=self.book(), slope=3.0)
            self.assertEqual(dn, {94: 300.0}, v)

    def test_fav_min_p(self):
        # 0.0299 -> fav fair 0.9701 >= 0.97: joins (guarded to floor(97.01 - 1));
        # fav_min_p 0.99 -> pad only
        self.assertEqual(q(P("G_decided_only"), 0.0299, book=self.book())[1], {96: 300.0})
        self.assertEqual(q(P("G_decided_only", fav_min_p=0.99), 0.0299, book=self.book())[1], {})

    def test_pad_needs_room_to_rest(self):
        # a YES bid at 1c crosses a 99c NO bid: no longshot pad (nor a YES
        # qualification pad); the NO favorite joins at floor(99 - 1)
        b = bv(by={}, bn={99: 500.0, 97: 800.0})
        dy, dn, _ = q(P("G_decided_only"), 0.01, book=b)
        self.assertEqual(dy, {})
        self.assertEqual(dn, {98: 300.0})

    def test_decided_off(self):
        self.assertEqual(q(P(), 0.01, book=self.book()), ({}, {}, None))


class TestNearDecided(unittest.TestCase):
    def test_half_size_live_rules(self):
        tags = {}
        dy, dn, rule = q(P("E_live300_pull_decided"), 0.06, tags=tags)
        self.assertEqual((dy, dn, rule), ({5: 150.0}, {45: 150.0}, "decided"))   # floor(6 - 1)
        self.assertEqual(tags, {("yes", 5): ["near"], ("no", 45): ["near"]})
        # G quotes near strikes too (q_live 300 -> 150), but never a live one
        self.assertEqual(q(P("G_decided_only"), 0.06)[:2], ({5: 150.0}, {45: 150.0}))
        # the window still pulls them
        self.assertEqual(q(P("G_decided_only"), 0.06, wflag=True), ({}, {}, "decided"))

    def test_near_without_half_rule_gets_decided_rules(self):
        # near_decided_half off: pad the longshot (YES 1c); fav 0.94 < 0.97
        dy, dn, rule = q(P("E_live300_pull_decided", near_decided_half=False), 0.06)
        self.assertEqual((dy, dn, rule), ({1: 1000.0}, {}, "decided"))

    def test_live_only_policy_ignores_near(self):
        self.assertEqual(q(P(), 0.06), ({}, {}, None))
        self.assertEqual(q(P(), 0.95), ({}, {}, None))


class TestSkew(unittest.TestCase):
    def test_skew_zones(self):
        f = P("F_full")
        self.assertEqual(q(f, 0.20)[:2], ({19: 420.0}, {45: 780.0}))
        self.assertEqual(q(f, 0.80)[:2], ({40: 420.0}, {19: 780.0}))
        self.assertEqual(q(f, 0.50)[:2], ({40: 600.0}, {45: 600.0}))
        sizes = {fx: (sum(q(f, fx)[0].values()), sum(q(f, fx)[1].values()))
                 for fx in (0.10, 0.30, 0.31, 0.69, 0.70, 0.90)}
        self.assertEqual(sizes[0.10], (420.0, 780.0))
        self.assertEqual(sizes[0.30], (420.0, 780.0))
        self.assertEqual(sizes[0.31], (600.0, 600.0))
        self.assertEqual(sizes[0.69], (600.0, 600.0))
        self.assertEqual(sizes[0.70], (420.0, 780.0))
        self.assertEqual(sizes[0.90], (420.0, 780.0))

    def test_skew_on_near_strikes(self):
        f = P("F_full")
        self.assertEqual(q(f, 0.06)[:2], ({5: 210.0}, {45: 390.0}))       # 300 x 0.7 / 1.3
        self.assertEqual(q(f, 0.04)[:2], ({3: 300.0}, {45: 300.0}))       # 3-5%: no skew


class TestQualificationPads(unittest.TestCase):
    def test_pad_lifts_thin_sides(self):
        b = bv(by={40: 50.0}, bn={45: 30.0})
        tags = {}
        dy, dn, _ = q(P(), 0.45, book=b, tags=tags)
        # refs: never reach 200 -> the best level; 50 + 300 -> pad 1000 + 50 - 350
        self.assertEqual(dy, {40: 300.0, 1: 700.0})
        self.assertEqual(dn, {45: 300.0, 1: 720.0})
        self.assertEqual(tags[("yes", 1)], ["qual"])
        self.assertEqual(q(P(qual_pads=False), 0.45, book=b)[:2], ({40: 300.0}, {45: 300.0}))
        self.assertEqual(q(P(pad_extra=0), 0.45, book=b, target=500.0)[:2],
                         ({40: 300.0, 1: 150.0}, {45: 300.0, 1: 170.0}))

    def test_pad_merges_with_longshot_pad(self):
        b = bv(by={2: 10.0}, bn={97: 300.0})
        tags = {}
        dy, dn, _ = q(P("G_decided_only"), 0.01, book=b, tags=tags)
        # YES: 1000 pad at 1c; depth 10 + 1000 >= 1000 -> no extra.
        # NO: fav 300 at 97; depth 300 + 300 < 1000 -> +ceil(1050 - 600) at 1c
        self.assertEqual(dy, {1: 1000.0})
        self.assertEqual(dn, {97: 300.0, 1: 450.0})
        b = bv(by={2: 10.0}, bn={97: 300.0})
        tags = {}
        dy, _, _ = q(P("G_decided_only", q_pad=500), 0.01, book=b, tags=tags)
        self.assertEqual(dy, {1: 500.0 + 540.0})                          # 1050 - 510
        self.assertEqual(tags[("yes", 1)], ["pad", "qual"])

    def test_no_pad_on_unquoted_market(self):
        self.assertEqual(q(P(), 0.5, book=bv(by={40: 5.0}, bn={45: 5.0}), wflag=True),
                         ({}, {}, "live"))


class TestRiskSizing(unittest.TestCase):
    def test_position_cap_trims_worst_prices_first(self):
        b = bv(by={40: 50.0}, bn={45: 30.0})
        # YES desired {40: 300, 1: 700}; room 200 -> the 1c pad goes first
        dy, dn, _ = q(P(), 0.45, book=b, risk=(200.0, 1e9, 1e9, 1e9))
        self.assertEqual(dy, {40: 200.0})
        self.assertEqual(dn, {45: 300.0, 1: 720.0})
        dy, _, _ = q(P(), 0.45, book=b, risk=(850.0, 1e9, 1e9, 1e9))
        self.assertEqual(dy, {40: 300.0, 1: 550.0})

    def test_worst_case_headroom_in_dollars(self):
        # $10 at 40c -> 25 contracts; <= 0 -> the side stops
        dy, dn, _ = q(P(), 0.45, risk=(1e9, 1e9, 10.0, 0.0))
        self.assertEqual((dy, dn), ({40: 25.0}, {}))
        # $1.50 over {40: 300, 1: 700}: the 1c pad is cut first (150 cents)
        b = bv(by={40: 50.0}, bn={45: 30.0})
        dy, _, _ = q(P(), 0.45, book=b, risk=(1e9, 1e9, 120.0 + 5.5, 1e9))
        self.assertEqual(dy, {40: 300.0, 1: 550.0})

    def test_scenarios_and_headroom(self):
        # 3 strikes; 100 YES bought at 40c on strike 1
        yes_q, no_q, cost = [0.0, 100.0, 0.0], [0.0, 0.0, 0.0], [0.0, 40.0, 0.0]
        self.assertEqual(pol.scenario_losses(yes_q, no_q, cost), [40.0, 40.0, -60.0, -60.0])
        self.assertEqual(pol.worst_case_loss(yes_q, no_q, cost), (40.0, [0, 1]))
        hr = pol.risk_headroom(P(pos_cap=150, event_wcl_cap=100.0), yes_q, no_q, cost)
        self.assertEqual(hr, [(150.0, 150.0, 60.0, 60.0), (50.0, 250.0, 60.0, 160.0),
                              (150.0, 150.0, 60.0, 160.0)])
        # flat book: no loss anywhere
        self.assertEqual(pol.worst_case_loss([0.0] * 2, [0.0] * 2, [0.0] * 2), (0.0, [0, 1, 2]))
        self.assertEqual(pol.risk_headroom(P(), [], [], []), [])

    def test_hedged_position(self):
        # YES 100 @40 on strike 0 + NO 100 @45 on strike 1 ($85): wins in
        # every scenario (+15 at either end, +115 between the strikes)
        yes_q, no_q, cost = [100.0, 0.0], [0.0, 100.0], [40.0, 45.0]
        self.assertEqual(pol.scenario_losses(yes_q, no_q, cost), [-15.0, -115.0, -15.0])
        self.assertEqual(pol.worst_case_loss(yes_q, no_q, cost), (0.0, [0, 2]))


class TestDiffOrders(unittest.TestCase):
    def test_ops(self):
        rest = {"yes": {40: 300.0, 1: 700.0}, "no": {45: 300.0}}
        ops = pol.diff_orders(rest, {40: 200.0, 39: 50.0}, {45: 400.0})
        self.assertEqual(ops, [("yes", "cancel", 1, 0.0), ("yes", "dec", 40, 100.0),
                               ("yes", "place", 39, 50.0), ("no", "inc", 45, 100.0)])
        self.assertEqual(ops[0].op, "cancel")
        self.assertEqual(pol.diff_orders(rest, {40: 300.0, 1: 700.0}, {45: 300.0}), [])
        self.assertEqual(pol.diff_orders({}, {5: 1.0}, {}), [("yes", "place", 5, 1.0)])
        side, op, p, s = ops[-1]
        self.assertEqual((side, op, p, s), ("no", "inc", 45, 100.0))


def _event(fairs=(0.995, 0.96, 0.60, 0.06, 0.004), strikes=(79.99, 80.99, 81.99, 82.99, 83.99),
           **kw):
    out = []
    for k, f in zip(strikes, fairs):
        if f >= 0.97:
            by, bn = {97: 800.0, 96: 900.0}, {2: 300.0, 1: 900.0}
        elif f <= 0.03:
            by, bn = {2: 300.0, 1: 900.0}, {97: 800.0, 96: 900.0}
        else:
            c = int(round(100 * f))
            by = {max(1, c - 2): 500.0, max(1, c - 3): 900.0}
            bn = {max(1, 98 - c): 500.0, max(1, 97 - c): 900.0}
        d = dict(ticker=f"KXTEMPMIAH-26OCT1515-T{k:.2f}", strike=k, book_yes=by, book_no=bn,
                 fair=f, slope_c=1.5, program_start=T0 - 3000.0, program_end=T0 + 1800.0)
        d.update(kw)
        out.append(pol.MarketState(**d))
    return out


class TestDecide(unittest.TestCase):
    def test_full_event_under_F(self):
        ctx = pol.EventContext(now=T0 + 5.0, close_ts=T0 + 1800.0)      # :05, tau 29.9
        mk = _event()
        dec = pol.decide(P("F_full"), ctx, list(reversed(mk)))           # any input order
        self.assertEqual([d.strike for d in dec.markets], sorted(m.strike for m in mk))
        self.assertFalse(dec.wflag)
        self.assertAlmostEqual(dec.rest_from, T0 + 5.15, delta=1e-5)
        self.assertEqual(dec.size_full, pol.live_size(P("F_full"), 1795.0 / 60.0))
        self.assertEqual(dec.worst_case_loss, 0.0)
        d = dec.by_ticker()
        hi, near_hi, live, near_lo, lo = (d[m.ticker] for m in mk)
        self.assertEqual((hi.cls, hi.rule), ("decided", "decided"))
        self.assertEqual([(o.side, o.price, o.size, o.tag) for o in hi.orders],
                         [("yes", 97, 300.0, "fav"), ("no", 1, 1000.0, "pad")])
        self.assertEqual((near_hi.cls, near_hi.rule), ("near", "decided"))
        self.assertEqual((live.cls, live.rule), ("live", "live"))
        self.assertEqual({o.tag for o in live.orders}, {"live"})
        self.assertEqual((lo.yes, lo.no), ({1: 1000.0}, {97: 300.0}))
        # every market's ops place exactly its desired orders (nothing rests)
        for m in dec.markets:
            self.assertEqual(sorted((o.side, o.price, o.size) for o in m.ops),
                             sorted((o.side, o.price, o.size) for o in m.orders))
            self.assertTrue(all(o.op == "place" for o in m.ops))
            self.assertAlmostEqual(m.diag["second_of_minute"], 5.15, delta=1e-5)

    def test_ops_against_resting(self):
        ctx = pol.EventContext(now=T0 + 5.0, close_ts=T0 + 1800.0)
        mk = _event(fairs=(0.004,), strikes=(83.99,))
        mk[0].resting_yes = {1: 400.0, 3: 10.0}
        mk[0].resting_no = {97: 300.0}
        dec = pol.decide(P("G_decided_only"), ctx, mk)
        self.assertEqual(dec.markets[0].ops, [("yes", "cancel", 3, 0.0),
                                              ("yes", "inc", 1, 600.0)])

    def test_window_program_trust(self):
        f = P("F_full")
        ctx_w = pol.EventContext(now=T0 + 30.0, close_ts=T0 + 1800.0)  # rests :30.15-:31.15
        dec = pol.decide(f, ctx_w, _event())
        self.assertTrue(dec.wflag)
        d = {m.cls: m for m in dec.markets}
        self.assertEqual(d["live"].orders, [])                          # pulled
        self.assertTrue(d["live"].diag["pulled"])
        self.assertTrue(d["decided"].orders)                            # pads stay
        # explicit resting interval
        dec = pol.decide(f, pol.EventContext(now=T0 + 30.0, close_ts=T0 + 1800.0,
                                             rest_from=T0 + 50.0, rest_until=T0 + 51.0),
                         _event())
        self.assertFalse(dec.wflag)
        # program over / override / untrusted
        ctx = pol.EventContext(now=T0 + 5.0, close_ts=T0 + 1800.0)
        for kw in (dict(program_end=T0), dict(program_start=None, program_end=None),
                   dict(program_active=False), dict(trusted=False)):
            dec = pol.decide(f, ctx, _event(**kw))
            self.assertTrue(all(m.orders == [] and m.rule is None for m in dec.markets), kw)
        dec = pol.decide(f, ctx, _event(program_start=None, program_end=None,
                                        program_active=True))
        self.assertTrue(any(m.orders for m in dec.markets))

    def test_positions_feed_the_risk(self):
        ctx = pol.EventContext(now=T0 + 5.0, close_ts=T0 + 1800.0)
        mk = _event()
        mk[2].yes_pos, mk[2].cost = 400.0, 240.0          # 400 YES @60 on the live strike
        f = P("F_full", event_wcl_cap=250.0, pos_cap=500)
        dec = pol.decide(f, ctx, mk)
        self.assertEqual(dec.worst_case_loss, 240.0)
        self.assertEqual(dec.wcl_scenarios, [0, 1, 2])
        live = dec.markets[2]
        self.assertEqual(live.diag["risk"], (100.0, 900.0, 10.0, 410.0))
        self.assertLessEqual(sum(live.yes.values()), 100.0)
        self.assertLessEqual(sum(p * s / 100.0 for p, s in live.yes.items()), 10.0 + 1e-9)
        # strike 0 / 1 YES lose in scenario 0 (already -$240): $10 left each
        for m in dec.markets[:2]:
            self.assertLessEqual(sum(p * s / 100.0 for p, s in m.yes.items()), 10.0 + 1e-9)

    def test_normal_slope_fallback(self):
        ctx = pol.EventContext(now=T0 + 5.0, close_ts=T0 + 1800.0)
        mk = _event(fairs=(0.42,), strikes=(81.99,), slope_c=None, mu=81.5, sigma=0.6,
                    threshold_f=81.82)
        dec = pol.decide(P(), ctx, mk)
        g = pol.normal_slope_c(81.5, 0.6, 81.82)
        self.assertEqual(dec.markets[0].diag["slope_src"], "normal")
        self.assertAlmostEqual(dec.markets[0].diag["buf"], g)
        self.assertEqual(dec.markets[0].yes, {pol._floor(100.0 * 0.42 - g): float(dec.size_full)})
        self.assertGreater(g, 10.0)
        mk = _event(fairs=(0.42,), strikes=(81.99,), slope_c=None)
        self.assertEqual(pol.decide(P(), ctx, mk).markets[0].diag["buf"], 1.0)

    def test_matches_quote_market(self):
        """decide() is quote_market + risk_headroom on the same inputs."""
        rng = random.Random(5)
        for _ in range(200):
            fairs = sorted((rng.random() for _ in range(4)), reverse=True)
            mk = _event(fairs=fairs, strikes=(80.99, 81.99, 82.99, 83.99))
            for m in mk:
                m.yes_pos = float(rng.choice([0, 0, 100, 700]))
                m.no_pos = float(rng.choice([0, 0, 50, 900]))
                m.cost = 0.3 * (m.yes_pos + m.no_pos)
            name = rng.choice(sorted(pol.VARIANTS))
            p = P(name, event_wcl_cap=rng.choice([1500.0, 300.0]))
            ctx = pol.EventContext(now=T0 + rng.uniform(0, 60), close_ts=T0 + rng.uniform(100, 3500))
            dec = pol.decide(p, ctx, mk)
            hr = pol.risk_headroom(p, [m.yes_pos for m in mk], [m.no_pos for m in mk],
                                   [m.cost for m in mk])
            for i, m in enumerate(mk):
                exp = pol.quote_market(p, pol.BookView(m.book_yes, m.book_no), m.fair, m.slope_c,
                                       dec.wflag, True, dec.size_full, hr[i], 1000.0)
                got = dec.markets[i]
                self.assertEqual((got.yes, got.no, got.rule), exp, name)


class TestFSupersetOfG(unittest.TestCase):
    """Jack 10/15: "build F, which is a superset of G right?" -- with the risk
    caps not binding: decided strikes identical, live strikes F only, near
    strikes quoted by both (F at q_live 600 / 2 with skew, G at 300 / 2)."""

    def test_by_class(self):
        f, g = P("F_full"), P("G_decided_only")
        rng = random.Random(3)
        seen = set()
        for _ in range(4000):
            fx = rng.random()
            by = {rng.randint(1, 98): float(rng.choice([20, 150, 600, 1500]))
                  for _ in range(rng.randint(0, 4))}
            bn = {rng.randint(1, 98): float(rng.choice([20, 150, 600, 1500]))
                  for _ in range(rng.randint(0, 4))}
            if by and bn and max(by) + max(bn) >= 100:
                continue
            b = pol.BookView(by, bn)
            slope = rng.choice([None, 0.5, 3.0])
            wflag = rng.random() < 0.3
            fy, fn, frule = pol.quote_market(f, b, fx, slope, wflag, True, 600, NORISK, 1000.0)
            gy, gn, grule = pol.quote_market(g, b, fx, slope, wflag, True, 300, NORISK, 1000.0)
            cls = pol.classify(fx)
            seen.add(cls)
            if cls == "decided":
                self.assertEqual((fy, fn, frule), (gy, gn, grule))
            elif cls == "live":
                self.assertEqual((gy, gn, grule), ({}, {}, None))
                self.assertEqual(frule, "live")
            else:
                self.assertEqual(frule, grule)
                self.assertEqual(set(fy) - {1}, set(gy) - {1})      # same prices
                if gy and not wflag and not (0.05 <= fx <= 0.95):
                    gl = {p: s for p, s in gy.items() if p != 1}
                    fl = {p: s for p, s in fy.items() if p != 1}
                    self.assertEqual(fl, {p: 2 * s for p, s in gl.items()})
        self.assertEqual(seen, {"live", "near", "decided"})


class TestRestingWCL(unittest.TestCase):
    """event_wcl_cap bounds FILLED positions; what RESTS can lose more when
    only the losing side fills (review 10/16: an 8-strike F book at tau 50
    risks $1,662 vs the $1,500 cap). wcl_resting (opt-in, live only) shares
    one budget across the event's resting orders."""
    STRIKES = (80.99, 81.99, 82.99, 83.99, 84.99, 85.99, 86.99, 87.99)
    FAIRS = (0.995, 0.985, 0.94, 0.80, 0.50, 0.20, 0.06, 0.01)
    SLOPES = (0.3, 0.6, 2.0, 4.0, 5.0, 4.0, 2.0, 0.3)

    def markets(self, **kw):
        out = []
        for k, f, g in zip(self.STRIKES, self.FAIRS, self.SLOPES):
            by, bn = int(round(100 * f)) - 3, int(round(100 * (1 - f))) - 3
            yb = {by: 300.0, by - 1: 1000.0} if by >= 2 else {1: 2000.0}
            nb = {bn: 300.0, bn - 1: 1000.0} if bn >= 2 else {1: 2000.0}
            out.append(pol.MarketState(ticker=f"T{k}", strike=k, book_yes=yb, book_no=nb,
                                       fair=f, slope_c=g, program_active=True, **kw))
        return out

    def decide(self, Pd, markets=None):
        now = T0 - (T0 % 3600.0) + 605.0                       # :10:05, tau 50
        return pol.decide(Pd, pol.EventContext(now=now, close_ts=now - 605.0 + 3600.0),
                          markets or self.markets())

    def adverse(self, dec):
        ny = [md.yes for md in dec.markets]
        nn = [md.no for md in dec.markets]
        z = [0.0] * len(ny)
        return max(pol.resting_scenario_losses(z, z, z, ny, nn))

    def test_default_cap_is_on_filled_positions_only(self):
        dec = self.decide(P("F_full"))
        self.assertGreater(dec.resting_wcl, 1500.0)              # $1,662: documented, not capped
        self.assertAlmostEqual(dec.resting_wcl, self.adverse(dec))
        self.assertEqual(dec.worst_case_loss, 0.0)

    def test_wcl_resting_keeps_the_adverse_case_under_the_cap(self):
        off = self.decide(P("F_full"))
        for variant, cap in (("F_full", 1500.0), ("K_full_wcl500", 500.0)):
            on = self.decide(P(variant, wcl_resting=True))
            self.assertLessEqual(on.resting_wcl, cap + 1e-6, variant)
            self.assertAlmostEqual(on.resting_wcl, self.adverse(on))
            for a, b in zip(off.markets, on.markets):          # only ever smaller
                for side in ("yes", "no"):
                    for p_, s_ in getattr(b, side).items():
                        self.assertLessEqual(s_, getattr(a, side).get(p_, 0.0))
            trimmed = [md for md in on.markets if md.diag.get("wcl_resting_trim")]
            self.assertTrue(trimmed)

    def test_wcl_resting_counts_filled_losses(self):
        # 2,000 NO already held on the top strike at 90c: scenario 8 already
        # loses $1,800 > the cap -> every order losing there is cleared
        mk = self.markets()
        mk[-1].no_pos, mk[-1].cost = 2000.0, 1800.0
        on = self.decide(P("F_full", wcl_resting=True), mk)
        self.assertEqual([md.no for md in on.markets], [{}] * len(mk))
        self.assertTrue(any(md.yes for md in on.markets))       # the YES side is untouched

    def test_off_by_default_and_not_simulated(self):
        self.assertFalse(pol.DEFAULT_POLICY["wcl_resting"])
        self.assertTrue(all(not v.get("wcl_resting") for v in pol.VARIANTS.values()))
        import temp_shadow_sim as sim
        with self.assertRaises(ValueError):
            sim._resolve_variants({"X": {"wcl_resting": True}})
        self.assertIn("F_full", sim._resolve_variants(None))


if __name__ == "__main__":
    unittest.main()
