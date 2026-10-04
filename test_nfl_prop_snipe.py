#!/usr/bin/env python3
"""Tests for nfl_prop_snipe (read-only snipe scanner, 2026-10-04)."""

import unittest

import nfl_prop_fair as nf
import nfl_prop_snipe as ns


class TestSnipeMath(unittest.TestCase):

    def test_implied_mu_inverts_the_fair(self):
        for series, mu in (("KXNFLESCALATORREC", 3.95), ("KXNFLLADDERREC", 3.95),
                           ("KXNFLESCALATORRECYDS", 37.16),
                           ("KXNFLLADDERRECYDS", 84.8), ("KXNFLFFPTSLADDER", 21.7)):
            stat, kind = nf.SERIES[series]
            spec = dict(nf.DEFAULT_SPEC[(stat, kind)], stat=stat, kind=kind)
            px = nf.expected_payout(spec, mu) * 100
            self.assertAlmostEqual(ns.implied_mu(spec, px, mu), mu,
                                   delta=0.01 * mu, msg=series)
        # Robinson's receiving-yards ladder at 19c on 10/3 implied ~76 yards
        spec = dict(nf.DEFAULT_SPEC[("recyds", "ladder")], stat="recyds",
                    kind="ladder")
        self.assertAlmostEqual(ns.implied_mu(spec, 19.0, 37.0), 76.0, delta=1.0)
        # a 99c receiving-yards ladder needs ~400 yards (the cap)
        self.assertGreater(ns.implied_mu(spec, 99.0, 37.0), 350.0)

    def test_taker_fee(self):
        self.assertAlmostEqual(ns.taker_fee_cents(50), 1.75)
        self.assertAlmostEqual(ns.taker_fee_cents(2), 0.1372)


if __name__ == "__main__":
    unittest.main()
