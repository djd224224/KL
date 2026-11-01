"""Base contracts per rung in high_temp_trading.py (2026-10-12).

high_temp_trading.py talks to Kalshi on import, so -- like
test_high_temp_run_slot.py -- this lifts the sizing blocks out of the source
and executes them. What it pins down:
  - morning markets split sticky 50/50 into the 25-vs-40 size test (Jack
    2026-11-01: "Test a bigger morning size, 25 vs 40"): control rungs start
    at the slot base, 25, treatment rungs at 40;
  - evening (15) and west_late (25) stay out of the test at their slot base;
  - BASE_CONTRACTS_DAY / BASE_CONTRACTS_EVENING / SIZE_EXP_TREATMENT_CONTRACTS
    override the sizes, and SIZE_EXP_ENABLED=false ends the test.

    python -m unittest test_high_temp_sizing
"""
import hashlib
import os
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "high_temp_trading.py")


def _slice(src, start_marker, end_marker):
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:src.index("\n", end) + 1]


def sizing(run_slot, env=None):
    """Execute the sizing config + helpers for one run slot under `env`."""
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    config = _slice(src, "SIZE_EXP_NAME = ",
                    'return BASE_CONTRACTS_EVENING if run_slot == "evening" else BASE_CONTRACTS_DAY')
    helpers = _slice(src, "def _size_exp_assign_arm(market_ticker):",
                     'return SIZE_EXP_TREATMENT_CONTRACTS if arm == "treatment" else slot_base_contracts(RUN_SLOT)')
    g = {"os": os, "hashlib": hashlib, "starting_contracts": 15, "RUN_SLOT": run_slot}
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("SIZE_EXP_", "BASE_CONTRACTS_"))}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True):
        exec(config + helpers, g)
    return g


def base_for(g, ticker):
    return g["_size_exp_base_contracts"](g["_size_exp_assign_arm"](ticker))


class TestBaseContracts(unittest.TestCase):
    TICKERS = [f"KXHIGHNY-26OCT{d:02d}-B{t}.5" for d in range(1, 29) for t in (60, 62, 64)]

    def test_mornings_split_25_vs_40(self):
        g = sizing("morning")
        self.assertTrue(g["SIZE_EXP_ACTIVE"])
        arms = {t: g["_size_exp_assign_arm"](t) for t in self.TICKERS}
        treated = sum(a == "treatment" for a in arms.values())
        self.assertEqual(set(arms.values()), {"treatment", "control"})
        self.assertTrue(0.3 < treated / len(arms) < 0.7, treated)
        for t, arm in arms.items():
            self.assertEqual(base_for(g, t), 40 if arm == "treatment" else 25)
        # sticky: a later run gives every market the same arm
        self.assertEqual(arms, {t: sizing("morning")["_size_exp_assign_arm"](t) for t in self.TICKERS})

    def test_evening_and_west_late_stay_out(self):
        for slot, want in (("west_late", 25), ("evening", 15)):
            g = sizing(slot)
            self.assertFalse(g["SIZE_EXP_ACTIVE"])
            self.assertEqual({g["_size_exp_assign_arm"](t) for t in self.TICKERS}, {"off"}, slot)
            self.assertEqual({base_for(g, t) for t in self.TICKERS}, {want}, slot)

    def test_switched_off_every_slot_is_its_base(self):
        for slot, want in (("morning", 25), ("west_late", 25), ("evening", 15)):
            g = sizing(slot, {"SIZE_EXP_ENABLED": "false"})
            self.assertEqual({g["_size_exp_assign_arm"](t) for t in self.TICKERS}, {"off"}, slot)
            self.assertEqual({base_for(g, t) for t in self.TICKERS}, {want}, slot)

    def test_env_overrides_each_size(self):
        env = {"BASE_CONTRACTS_DAY": "30", "BASE_CONTRACTS_EVENING": "10",
               "SIZE_EXP_TREATMENT_CONTRACTS": "50"}
        g = sizing("morning", env)
        self.assertEqual({base_for(g, t) for t in self.TICKERS}, {30, 50})
        g = sizing("evening", env)
        self.assertEqual({base_for(g, t) for t in self.TICKERS}, {10})


def caps(env=None):
    """Execute the per-market cap config + market_position_cap under `env`."""
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    code = (_slice(src, "max_contracts = 100", "max_contracts = 100")
            + _slice(src, "MARKET_CASH_CAP_DOLLARS = float(", "MARKET_CASH_CAP_DOLLARS = float(")
            + _slice(src, "CITY_MAX_CONTRACTS = {", "}\n")
            + _slice(src, "MORNING_CAP_MULT = float(", "            cap = int(round(cap * MORNING_CAP_MULT))")
            + "    return cap\n")
    g = {"os": os}
    clean = {k: v for k, v in os.environ.items()
             if k not in ("MORNING_CAP_MULT", "MARKET_CASH_CAP_DOLLARS")}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True):
        exec(code, g)
    return g


class TestCaps(unittest.TestCase):
    """Live workflow: MORNING_CAP_MULT=1.5 (37e11bf)."""

    def test_morning_overrides_and_base_caps(self):
        cap = caps({"MORNING_CAP_MULT": "1.5"})["market_position_cap"]
        for city in ("Boston", "Oklahoma City", "New Orleans", "Phoenix"):
            self.assertEqual(cap(city, "morning"), 225, city)
            self.assertEqual(cap(city, "evening"), 100, city)
            self.assertEqual(cap(city, "west_late"), 100, city)     # Phoenix runs west_late: base cap
        self.assertEqual(cap("New York City", "morning"), 225)
        self.assertEqual(cap("New York City", "evening"), 150)
        self.assertEqual(cap("Miami", "morning"), 150)              # not overridden
        self.assertEqual(cap("Atlanta", "morning"), 150)
        self.assertEqual(cap("San Diego", "morning"), 75)
        self.assertEqual(cap("San Diego", "evening"), 50)

    def test_reverting_the_morning_multiplier_still_reverts_the_scale(self):
        cap = caps({"MORNING_CAP_MULT": "1.0"})["market_position_cap"]
        self.assertEqual(cap("Boston", "morning"), 150)
        self.assertEqual(cap("New York City", "morning"), 150)
        self.assertEqual(cap("Boston", "evening"), 100)

    def test_cash_cap_default_and_override(self):
        self.assertEqual(caps()["MARKET_CASH_CAP_DOLLARS"], 175.0)
        self.assertEqual(caps({"MARKET_CASH_CAP_DOLLARS": "125"})["MARKET_CASH_CAP_DOLLARS"], 125.0)


if __name__ == "__main__":
    unittest.main()
