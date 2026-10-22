#!/usr/bin/env python3
"""Unit tests for imm_pocket.py's refresh explanation (Jack 2026-10-22: "When
I ask for refresh, always give me an explanation of the days performance").

Run: python -m unittest test_imm_pocket
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_pocket as ip                                              # noqa: E402


def fam(name, rew, nf, cr, cs, ev=None, fills=3):
    return {"name": name, "rew": rew, "pnl": nf + cr + cs, "net": rew + nf + cr + cs, "nf": nf,
            "cr": cr, "cs": cs, "fills": fills, "ev": ev or []}


class TestExplain(unittest.TestCase):
    def test_the_day_its_split_and_what_moved_it(self):
        data = {"titles": {"KXRAIN-26OCT22": "Rain in NYC on Oct 22?"},
                "ystd": {"rew": 600.0, "pnl": -500.0},
                "windows": {"today": {"tot": {"net": 24.4, "rew": 526.5, "pnl": -502.1, "cr": 61.2,
                                               "cs": -163.2, "fills": 409, "typ": 156.0},
                                      "fams": [fam("Weather & quakes", 11, 15, 0, -147,
                                                   [{"e": "KXRAIN-26OCT22", "net": -124.0}]),
                                               fam("Rotten Tomatoes", 42, -14, 106, 0),
                                               fam("Flat", 0.2, 0, 0, 0)]}}}
        lines = ip.explain(data)
        self.assertEqual(lines[0], "net +$24 vs a typical full day +$156 (7-day mean); yesterday "
                                   "at this time +$100 (rewards $600, trading -$500)")
        self.assertEqual(lines[1], "rewards $526, trading -$502 = new fills -$400 + carry -$102 "
                                   "(marks +$61, settled -$163); 409 fills")
        self.assertEqual(lines[2], "drag: Weather & quakes -$121 (rewards $11, new fills +$15, carry "
                                   "-$147, 3 fills); biggest event Rain in NYC on Oct 22? -$124")
        self.assertTrue(lines[3].startswith("lift: Rotten Tomatoes +$134"))
        self.assertEqual(len(lines), 4)                                # "Flat" moved nothing
        self.assertEqual(ip.explain({"windows": {}}), [])


if __name__ == "__main__":
    unittest.main()
