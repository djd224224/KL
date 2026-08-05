#!/usr/bin/env python3
"""Tests for imm_reward_recon.py and the digest's credit-ledger reporting.

The reconciliation is only worth having if it fails LOUDLY: a parser that
silently drops rows, or a merge that silently duplicates them, produces a
number that looks authoritative and is wrong. Most of what is asserted here is
that behaviour, not the happy path.
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import imm_reward_recon as rec  # noqa: E402


def _statement(body, header=True):
    """Write a statement paste to a temp file and return its path."""
    head = ("Aug 2026\n$12.00\n\nLifetime rewards\n$50.00\n\n") if header else ""
    fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8")
    fh.write(head + body)
    fh.close()
    return fh.name


CREDIT = ("Liquidity Incentive for event {ev}\n{d}\n${a}\nLiquidity\n")


class TestStatementParsing(unittest.TestCase):
    def test_parses_a_normal_credit_block(self):
        p = _statement(CREDIT.format(ev="KXTEMPDCH-26AUG0213", d="Aug 3, 2026", a="4.83")
                       + CREDIT.format(ev="KXTEMPDCH-26AUG0213", d="Aug 3, 2026", a="3.98"))
        try:
            rows, bad = rec.parse_statement(p)
        finally:
            os.unlink(p)
        self.assertEqual(bad, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("2026-08-03", "KXTEMPDCH-26AUG0213", 4.83, "Liquidity"))

    def test_header_lines_are_not_mistaken_for_credits(self):
        p = _statement(CREDIT.format(ev="KXA-26AUG01", d="Aug 3, 2026", a="1.00"))
        try:
            rows, bad = rec.parse_statement(p)
            totals = rec.statement_totals(p)
        finally:
            os.unlink(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(totals["lifetime"], 50.00)
        self.assertEqual(totals["month"], ("Aug 2026", 12.00))

    def test_volume_incentives_are_captured_too(self):
        # The lifetime total includes them; dropping them makes the ledger
        # fail its own reconciliation by a few cents and look broken.
        p = _statement("Volume Incentive for event KXLAYOFFSYINFO-26\n"
                       "Mar 21, 2026\n$0.31\nVolume\n")
        try:
            rows, bad = rec.parse_statement(p)
        finally:
            os.unlink(p)
        self.assertEqual(bad, [])
        self.assertEqual(rows, [("2026-03-21", "KXLAYOFFSYINFO-26", 0.31, "Volume")])

    def test_amounts_with_thousands_separators(self):
        p = _statement(CREDIT.format(ev="KXA-26AUG01", d="Aug 3, 2026", a="1,234.56"))
        try:
            rows, _bad = rec.parse_statement(p)
        finally:
            os.unlink(p)
        self.assertEqual(rows[0][2], 1234.56)

    def test_malformed_credit_is_reported_not_swallowed(self):
        """A UI format change must surface as a loud count. Silently returning
        a short ledger would understate reward and look like a real drop."""
        p = _statement("Liquidity Incentive for event KXA-26AUG01\n"
                       "3 August 2026\n$4.83\nLiquidity\n")
        try:
            rows, bad = rec.parse_statement(p)
        finally:
            os.unlink(p)
        self.assertEqual(rows, [])
        self.assertEqual(len(bad), 1)


class TestLedgerMerge(unittest.TestCase):
    A = ("2026-08-03", "KXA-26AUG01", 2.50, "Liquidity")
    B = ("2026-08-03", "KXB-26AUG01", 1.00, "Liquidity")

    def test_re_pasting_the_same_statement_is_idempotent(self):
        merged, added = rec.merge_ledger([self.A, self.B], [self.A, self.B])
        self.assertEqual(added, 0)
        self.assertEqual(sorted(merged), sorted([self.A, self.B]))

    def test_identical_credits_on_one_day_are_kept(self):
        """An event pays once per MARKET, so the same amount legitimately
        repeats within a day. Set-dedup here would delete real money."""
        merged, added = rec.merge_ledger([], [self.A, self.A, self.A])
        self.assertEqual(added, 3)
        self.assertEqual(merged.count(self.A), 3)

    def test_overlapping_statements_take_the_max_count(self):
        merged, added = rec.merge_ledger([self.A, self.A], [self.A, self.A, self.A])
        self.assertEqual(merged.count(self.A), 3)
        self.assertEqual(added, 1)

    def test_new_events_are_appended(self):
        merged, added = rec.merge_ledger([self.A], [self.B])
        self.assertEqual(added, 1)
        self.assertEqual(sorted(merged), sorted([self.A, self.B]))


class TestPayoutFloorModel(unittest.TestCase):
    def test_floor_is_a_dollar(self):
        # Measured, not chosen: across all 2,720 LIQUIDITY credits on the
        # 2026-08-04 statement the minimum is exactly $1.00 and none is below.
        # (The statement's single VOLUME incentive is $0.31 — different
        # program, not floored — hence 2,721 ledger rows but 2,720 here.)
        self.assertEqual(rec.PAYOUT_FLOOR, 1.00)

    def test_inception_matches_the_bot_going_live(self):
        self.assertEqual(rec.IMM_INCEPTION, "2026-07-12")


class TestEventRollup(unittest.TestCase):
    def test_market_ticker_rolls_to_its_event(self):
        self.assertEqual(rec.event_of("KXTEMPDCH-26AUG0213-T75.99"),
                         "KXTEMPDCH-26AUG0213")
        self.assertEqual(rec.event_of("KXRAIN-26AUG03-DEN"), "KXRAIN-26AUG03")
        self.assertEqual(rec.series_of("KXTEMPDCH-26AUG0213-T75.99"), "KXTEMPDCH")

    def test_strikes_containing_dashes_still_roll_up(self):
        self.assertEqual(rec.event_of("KXA-26AUG01-T5.315-X"), "KXA-26AUG01")


class TestDigestCreditWindows(unittest.TestCase):
    """The digest must report IMM's reward, not the account's."""

    def setUp(self):
        import send_imm_digest as dg
        self.dg = dg
        self.today = date(2026, 8, 4)

    def _rows(self):
        return [
            ("2026-05-06", "KXHIGHTNOLA-26MAY06", 1.06),   # pre-inception
            ("2026-07-05", "KXBNBMAXMON-BNB", 11.31),      # pre-inception
            ("2026-08-03", "KXTEMPDCH-26AUG0213", 13.68),
            ("2026-08-02", "KXTEMPNYCH-26AUG0111", 6.75),
            ("2026-07-20", "KXWCMENTION-MENWORLDCUP", 152.94),
        ]

    def test_credits_before_inception_are_excluded(self):
        w = self.dg.credited_windows(self._rows(), {}, self.today)
        # 13.68 + 6.75 + 152.94; the May and July-5 rows are dropped
        self.assertAlmostEqual(w["lifetime"], 173.37)

    def test_day_and_mtd_windows(self):
        w = self.dg.credited_windows(self._rows(), {}, self.today)
        self.assertAlmostEqual(w["day"], 13.68)          # 2026-08-03
        self.assertAlmostEqual(w["mtd"], 20.43)          # Aug 2 + Aug 3
        self.assertEqual(w["latest"], "2026-08-03")

    def test_calibration_attribution_wins_when_present(self):
        """With a calibration file the lifetime figure is the per-event
        IMM-attributable total, which strips the other bots on this key."""
        calib = {"credited_imm_attributable": 20.43,
                 "credited_lifetime_account": 185.74,
                 "credited_non_imm": 165.31,
                 # the World Cup event is another bot's, so it is absent here
                 "credited_by_date_imm": {"2026-08-03": 13.68, "2026-08-02": 6.75}}
        w = self.dg.credited_windows(self._rows(), calib, self.today)
        self.assertAlmostEqual(w["lifetime"], 20.43)
        self.assertTrue(w["attributed"])
        self.assertAlmostEqual(w["account_lifetime"], 185.74)
        # every window is attribution-filtered, not just lifetime
        self.assertAlmostEqual(w["mtd"], 20.43)
        self.assertAlmostEqual(w["day"], 13.68)
        self.assertAlmostEqual(w["week"], 20.43)

    def test_windows_fall_back_to_raw_ledger_without_a_calibration(self):
        """No calibration file: report the date-filtered ledger and say the
        figure is NOT attribution-filtered rather than implying it is."""
        w = self.dg.credited_windows(self._rows(), {}, self.today)
        self.assertFalse(w["attributed"])
        self.assertAlmostEqual(w["lifetime"], 173.37)   # includes the WC event

    def test_no_ledger_returns_none(self):
        self.assertIsNone(self.dg.credited_windows([], {}, self.today))

    def test_week_window_is_seven_prior_days(self):
        rows = [((self.today - timedelta(days=i)).isoformat(), "KXA-26AUG01", 1.0)
                for i in range(0, 10)]
        w = self.dg.credited_windows(rows, {}, self.today)
        self.assertAlmostEqual(w["week"], 7.0)   # days -1..-7, not today


class TestDigestCapacity(unittest.TestCase):
    """Jack 2026-08-04: "add quoted events / total cap, and actuals vs any
    other caps. so i know how close i am to getting capped"."""

    def setUp(self):
        import send_imm_digest as dg
        self.dg = dg

    UNIVERSE = (
        "2026-08-05 00:53:41Z [IMM] universe: 4232 program markets -> 847 "
        "candidates -> 495 selected across 43/75 events (0 forced quote-all "
        "@ ~$8321, total ~$9412 ladder collateral, $5177 inventory reserve); "
        "skips {'one_sided': 43, 'cutoff': 60, 'budget': 12}")

    def test_parses_the_selection_gate_line(self):
        import re
        m = None
        for m in self.dg._UNIVERSE_RE.finditer(self.UNIVERSE):
            pass
        self.assertIsNotNone(m, "universe line must parse")
        self.assertEqual(int(m.group(2)), 847)      # candidates
        self.assertEqual(int(m.group(4)), 43)       # events used
        self.assertEqual(int(m.group(5)), 75)       # event cap
        self.assertEqual(float(m.group(7)), 9412)   # ladder collateral
        self.assertEqual(float(m.group(8)), 5177)   # inventory reserve

    def test_percentages_and_flags(self):
        rows = self.dg.capacity_rows(
            {"own_pos": {"KXTEMPAUSH-26AUG0409-T79.99": -70.0}},
            {}, {"events": 40, "orders": 979, "collateral": 12435.0}, -8.88)
        by = {r["label"]: r for r in rows}
        # the worst per-market position is reported against ITS series cap
        pm = by["Per-market position (worst)"]
        self.assertEqual(pm["actual"], 70.0)
        self.assertEqual(pm["cap"], 50.0)           # KXTEMP cap, not the global 150
        self.assertGreater(pm["pct"], 100)          # over cap -> flagged
        self.assertIn("KXTEMPAUSH", pm["note"])

    def test_a_loss_is_reported_against_the_halt_not_a_gain(self):
        rows = self.dg.capacity_rows({}, {}, {"events": 1, "orders": 1,
                                              "collateral": 0.0}, -600.0)
        halt = {r["label"]: r for r in rows}["Daily loss vs halt"]
        self.assertEqual(halt["actual"], 600.0)
        self.assertAlmostEqual(halt["pct"], 50.0)

    def test_a_profitable_day_shows_zero_against_the_halt(self):
        rows = self.dg.capacity_rows({}, {}, {"events": 1, "orders": 1,
                                              "collateral": 0.0}, +900.0)
        halt = {r["label"]: r for r in rows}["Daily loss vs halt"]
        self.assertEqual(halt["actual"], 0.0)

    def test_launcher_env_parses(self):
        self.assertTrue(self.dg.LAUNCHER_ENV,
                        "launcher $ProbeEnv did not parse — caps would be wrong")
        self.assertIn("IMM_COLLATERAL_BUDGET", self.dg.LAUNCHER_ENV)

    def test_note_reports_whether_the_caps_ACTUALLY_took_effect(self):
        """The section is misleading on defaults ($1,000 budget / 35 events vs
        the live $50,000 / 75). Counting parsed env vars is not enough: if
        anything imported incentive_mm before the env was applied, the vars
        parse fine and the constants stay at defaults. So the note is driven by
        comparing the live constants to the launcher values."""
        note = self.dg.capacity_note()
        mismatches = self.dg.capacity_config_mismatches()
        if mismatches:
            # e.g. running after test_incentive_mm has already imported the
            # module — the note MUST say so rather than claim "mirrored"
            self.assertIn("do NOT match", note)
            self.assertNotIn("verified", note)
        else:
            self.assertIn("verified", note)
            self.assertGreaterEqual(self.dg.imm.COLLATERAL_BUDGET, 20000)
            self.assertGreaterEqual(self.dg.imm.MAX_MARKETS, 50)

    def test_mismatch_is_detected_not_papered_over(self):
        saved = dict(self.dg.LAUNCHER_ENV)
        try:
            self.dg.LAUNCHER_ENV["IMM_COLLATERAL_BUDGET"] = "999999"
            bad = self.dg.capacity_config_mismatches()
            self.assertTrue(any(v == "IMM_COLLATERAL_BUDGET" for v, _w, _g in bad))
            self.assertIn("do NOT match", self.dg.capacity_note())
        finally:
            self.dg.LAUNCHER_ENV.clear()
            self.dg.LAUNCHER_ENV.update(saved)

    def test_note_shouts_when_the_mirror_fails(self):
        saved = dict(self.dg.LAUNCHER_ENV)
        try:
            self.dg.LAUNCHER_ENV.clear()
            self.assertIn("DEFAULTS", self.dg.capacity_note())
        finally:
            self.dg.LAUNCHER_ENV.update(saved)


if __name__ == "__main__":
    unittest.main()
