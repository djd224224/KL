"""Open scan off, its earners graduated, three losing classes cut (Jack
2026-10-03: "yes do 1 - 4 but also keep Jobs (+$16), travel (+$10) and
energy (+$5) and any other markets that are near-breakeven")."""
import unittest

import incentive_mm as imm

M = imm.IncentiveMarketMaker


class ScanGraduateTests(unittest.TestCase):

    def test_graduates_are_allowed_and_not_blocked(self):
        self.assertEqual(len(imm.SCAN_GRADUATE_SERIES), 83)
        for s in sorted(imm.SCAN_GRADUATE_SERIES):
            self.assertTrue(M._allowed(f"{s}-X"), s)
            self.assertFalse(M._blocked(f"{s}-X"), s)
        for s in ("KXTRUMPENDORSEMENTS", "KXSUEZWEEKLY", "KXORBOFHARVEST",
                  "KXCFNAI", "KXNYCSUBWAY", "KXWYCOAL", "KXOER"):
            self.assertTrue(imm.scan_graduate_series(s), s)

    def test_the_scan_losers_stay_out_of_the_normal_book(self):
        for s in ("KXBILLSSIGNED", "KXDIESELMON", "KXDIESELELECT",
                  "KXTOP10BBSPOTS", "KXUSHOMEINVENT", "KXEOWEEK"):
            self.assertFalse(imm.scan_graduate_series(s), s)
            self.assertFalse(M._allowed(f"{s}-X"), s)

    def test_three_strikes_per_event_like_the_scan(self):
        self.assertEqual(imm.SCAN_GRADUATE_EVENT_TOP_N, imm.SCAN_EVENT_TOP_N)
        for s in sorted(imm.SCAN_GRADUATE_SERIES):
            self.assertEqual(imm.event_top_n_for(s), 3, s)

    def test_no_yield_size_mode(self):
        for s in sorted(imm.SCAN_GRADUATE_SERIES):
            self.assertFalse(imm.yield_size_eligible(s), s)

    def test_members_clone_the_scan_guard_set(self):
        arch = imm.SERIES_OVERRIDES[imm.SCAN_GRADUATE_ARCHETYPE]
        self.assertTrue(arch.safe_join)
        self.assertEqual(arch.min_est_per_day, 0.0)
        self.assertEqual(arch.levels, imm.SCAN_LEVELS)
        self.assertEqual(arch.max_position, imm.SCAN_MAX_POSITION)
        # a gas graduate takes the graduate guard, not the gas-daily parent's
        for s in ("KXORBOFHARVEST", "KXAAAGASMTX"):
            saved = imm.SERIES_OVERRIDES.pop(s, None)
            try:
                imm.ensure_family_override(s)
                self.assertIs(imm.SERIES_OVERRIDES[s], arch, s)
            finally:
                imm.SERIES_OVERRIDES.pop(s, None)
                if saved is not None:
                    imm.SERIES_OVERRIDES[s] = saved


class CutSeriesTests(unittest.TestCase):

    def test_kpi_food_and_aaa_maxmin_are_frozen(self):
        for t in ("KXBA-26OCTDELIV-T50", "KXRBLX-26OCTDAU-T100",
                  "KXFSLRA-27JANBOOK-T50", "KXDKS-26Q3-T1", "KXPM-26OCT-T1",
                  "KXSBUXSAR-26NOV02-T5.1", "KXCHIPBURRITO-26NOV02-T9.8",
                  "KXWENBACONATOR-26NOV02-T8.73", "KXAMSAVO-26OCT10-T1.2",
                  "KXAAAGASMAXM-26OCT30-T3.5", "KXAAAGASMINM-26OCT31-T3",
                  "KXBA-X", "KXBA-26OCTDELIV"):
            self.assertTrue(M._blocked(t), t)

    def test_the_four_unblocked_kpi_series_quote_again(self):
        # Jack 2026-10-03: "unblock those four" -- Robinhood x2, First Solar,
        # Coinbase (positive over their whole history); the annual FSLRA stays
        for s in ("KXHOOD", "KXHOODA", "KXFSLR", "KXCOINBASE"):
            self.assertNotIn(s, imm._CUT_KPI_SERIES, s)
            self.assertFalse(M._blocked(f"{s}-26OCTFUNDED-T27"), s)
            self.assertTrue(M._allowed(f"{s}-X"), s)

    def test_one_series_per_entry_never_a_neighbour(self):
        # the trailing dash: a KPI name that prefixes another series
        for t in ("KXBABELMANDEBWEEKLY-26OCT11-T30",   # KXBA
                  "KXCMGFT-26NOV08-T100", "KXCMGCC-26NOV07-T100",  # KXCMG
                  "KXAMZNCC-26NOV07-T100",              # KXAMZN
                  "KXDGCC-26NOV07-T100",                # KXDG
                  "KXLOWCC-26NOV07-T100",               # KXLOW
                  "KXBKFT-26NOV08-T100", "KXYUMTBFT-26NOV08-T95",
                  "KXAAAGASM-26OCT31-T3.1", "KXAAAGASMTX-26OCT31-T3"):
            self.assertFalse(M._blocked(t), t)


if __name__ == "__main__":
    unittest.main()
