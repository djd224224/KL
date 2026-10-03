"""IMM_EVENT_TOP_N_MULT (Jack 2026-10-03: "Double ... slot caps"): one knob
scales every per-event strike cap in the EVENT_TOP_N spec."""
import unittest
from unittest import mock

import incentive_mm as imm


class EventTopNMultTests(unittest.TestCase):

    def test_default_is_the_spec_as_written(self):
        self.assertEqual(imm.EVENT_TOP_N_MULT, 1.0)   # the launcher sets 2, tests do not
        self.assertEqual(imm.event_top_n_for("KXAAAGASD"), 3)

    def test_doubles_every_capped_family_and_keeps_uncapped_ones_uncapped(self):
        with mock.patch.object(imm, "EVENT_TOP_N_MULT", 2.0):
            self.assertEqual(imm.event_top_n_for("KXAAAGASD"), 6)       # prefix entry
            self.assertEqual(imm.event_top_n_for("KXDIESELW"), 6)
            self.assertEqual(imm.event_top_n_for("KXPADATACENTERS"), 6)  # suffix entry
            self.assertEqual(imm.event_top_n_for("KXART"), 6)            # exact entry
            self.assertEqual(imm.event_top_n_for("KXDIESELMONAK"), 0)    # =...:0 uncapped
            self.assertEqual(imm.event_top_n_for("KXTOKENUSE"), 0)
            self.assertEqual(imm.event_top_n_for("KXRT"), 0)              # no entry at all
            self.assertEqual(imm.event_top_n_for("KXOSCARMENTION"), 0)   # mention books

    def test_a_capped_family_never_rounds_down_to_uncapped(self):
        with mock.patch.object(imm, "EVENT_TOP_N_MULT", 0.1):
            self.assertEqual(imm.event_top_n_for("KXAAAGASD"), 1)
        with mock.patch.object(imm, "EVENT_TOP_N_MULT", 1.5):
            self.assertEqual(imm.event_top_n_for("KXAAAGASD"), 4)        # round(4.5) = 4

    def test_the_cut_uses_the_scaled_cap(self):
        """event_top_n_cut reads the cap through event_top_n_for, so six
        strikes of one gas event survive at x2 where three did at x1."""
        def meta(i):
            t = f"KXAAAGASDIL-26OCT05-{4.20 + i / 100:.2f}"
            return imm.MarketMeta(
                ticker=t, event_ticker=t.rsplit("-", 1)[0], series="KXAAAGASDIL",
                dollars_per_day=20.0, program_end=None, target_size=1000,
                discount_factor=0.5, cutoff=None, close_time=None,
                est_dollars_per_day=1.0 + i, est_exposure_dollars=10.0,
                est_collateral_dollars=0.0)
        ranked = [meta(i) for i in range(8)]
        cut1 = imm.event_top_n_cut(ranked, incumbent=set())
        with mock.patch.object(imm, "EVENT_TOP_N_MULT", 2.0):
            cut2 = imm.event_top_n_cut(ranked, incumbent=set())
        self.assertEqual(len(ranked) - len(cut1), 3)
        self.assertEqual(len(ranked) - len(cut2), 6)


if __name__ == "__main__":
    unittest.main()
