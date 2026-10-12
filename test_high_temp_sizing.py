"""Base contracts per rung in high_temp_trading.py (2026-10-12).

high_temp_trading.py talks to Kalshi on import, so -- like
test_high_temp_run_slot.py -- this lifts the sizing blocks out of the source
and executes them. What it pins down:
  - with the size experiment ended (the default), morning and west_late rungs
    start at 25 contracts and evening rungs at 15;
  - BASE_CONTRACTS_DAY / BASE_CONTRACTS_EVENING override them;
  - SIZE_EXP_ENABLED=true still restores the sticky 15-vs-25 split.

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
                     'return SIZE_EXP_TREATMENT_CONTRACTS if arm == "treatment" else starting_contracts')
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

    def test_morning_and_west_late_are_25_evening_15(self):
        for slot, want in (("morning", 25), ("west_late", 25), ("evening", 15)):
            g = sizing(slot)
            self.assertFalse(g["SIZE_EXP_ENABLED"])
            self.assertEqual({base_for(g, t) for t in self.TICKERS}, {want}, slot)
            self.assertEqual(g["_size_exp_assign_arm"](self.TICKERS[0]), "off")

    def test_env_overrides_each_slot(self):
        g = sizing("morning", {"BASE_CONTRACTS_DAY": "30", "BASE_CONTRACTS_EVENING": "10"})
        self.assertEqual(base_for(g, self.TICKERS[0]), 30)
        g = sizing("evening", {"BASE_CONTRACTS_DAY": "30", "BASE_CONTRACTS_EVENING": "10"})
        self.assertEqual(base_for(g, self.TICKERS[0]), 10)

    def test_experiment_can_still_be_switched_back_on(self):
        for slot in ("morning", "evening"):
            g = sizing(slot, {"SIZE_EXP_ENABLED": "true"})
            arms = {t: g["_size_exp_assign_arm"](t) for t in self.TICKERS}
            self.assertEqual(set(arms.values()), {"treatment", "control"})
            for t, arm in arms.items():
                self.assertEqual(base_for(g, t), 25 if arm == "treatment" else 15)
            # sticky: same split for the same ticker whatever the slot
            self.assertEqual(arms, {t: sizing("morning", {"SIZE_EXP_ENABLED": "true"})["_size_exp_assign_arm"](t)
                                    for t in self.TICKERS})


if __name__ == "__main__":
    unittest.main()
