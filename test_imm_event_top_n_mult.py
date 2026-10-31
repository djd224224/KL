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


class ShippingEventTopNTests(unittest.TestCase):
    """Jack 2026-10-31 "Raise to 5": the strait weeklies, KXHORMUZPEAK and the
    port TEU series quote 5 strikes an event; every other graduate keeps the
    scan's 3."""

    SHIPPING = ("KXSUEZWEEKLY", "KXBABELMANDEBWEEKLY", "KXHORMUZWEEKLY",
                "KXPANAMAWEEKLY", "KXHORMUZPEAK", "KXVAPORTTEU", "KXNYNJPORTTEU",
                "KXMOBILETEU", "KXSAVTEU", "KXSCPORTTEU", "KXBALTPORTTEU")

    def test_the_shipping_family_takes_five(self):
        self.assertEqual(imm.SHIPPING_EVENT_TOP_N, 5)
        for s in self.SHIPPING:
            self.assertEqual(imm.event_top_n_for(s), 5, s)

    def test_every_other_graduate_keeps_three(self):
        for s in ("KXTRUMPENDORSEMENTS", "KXORBOFHARVEST", "KXMOMFGEMP",
                  "KXSOFTWAREUNEMP"):
            self.assertTrue(imm.scan_graduate_series(s), s)
            self.assertEqual(imm.event_top_n_for(s), 3, s)

    def test_one_entry_per_series(self):
        """event_top_n_for takes the first match, so a second entry for the
        same series would make the order decide."""
        pats = [p for p, _n in imm.EVENT_TOP_N]
        self.assertEqual(len(pats), len(set(pats)))

    def test_a_raise_only(self):
        f = imm.shipping_event_top_n
        self.assertEqual(f(5, 3), 5)
        self.assertEqual(f(0, 3), 3)          # off: back on the graduates' 3
        self.assertEqual(f(3, 3), 3)
        self.assertEqual(f(2, 3), 3)          # never below the graduates
        self.assertEqual(f(5, 6), 6)
        self.assertEqual(f(5, 0), 0)          # uncapped graduates stay uncapped

    def test_scaled_with_every_other_cap(self):
        with mock.patch.object(imm, "EVENT_TOP_N_MULT", 2.0):
            self.assertEqual(imm.event_top_n_for("KXPANAMAWEEKLY"), 10)
            self.assertEqual(imm.event_top_n_for("KXTRUMPENDORSEMENTS"), 6)

    def test_the_cut_keeps_five_strikes_of_a_strait_week(self):
        def meta(i):
            t = f"KXPANAMAWEEKLY-26NOV01-T{170 + 5 * i}"
            return imm.MarketMeta(
                ticker=t, event_ticker="KXPANAMAWEEKLY-26NOV01",
                series="KXPANAMAWEEKLY", dollars_per_day=100.0,
                program_end=None, target_size=1000, discount_factor=0.5,
                cutoff=None, close_time=None, est_dollars_per_day=1.0 + i,
                est_exposure_dollars=10.0, est_collateral_dollars=0.0)
        strikes = [meta(i) for i in range(8)]
        cut = imm.event_top_n_cut(strikes, incumbent=set())
        self.assertEqual(len(strikes) - len(cut), 5)
        self.assertEqual(cut, {m.ticker for m in strikes[:3]})   # lowest ROI out


if __name__ == "__main__":
    unittest.main()
