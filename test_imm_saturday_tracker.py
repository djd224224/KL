"""Tests for imm_saturday_tracker's renderers (Jack 2026-09-21: the email was a
<pre> dump of DataFrame.to_string(); it must be real, legible tables now).

The frame under test is what build_rows() returns: one row per (et_date,
group) with the summed inputs _derive() works from. Values are chosen so the
derived metrics are the ones the assertions name (rent/fill = 100*rent/fills,
loss/fill = -100*settle_pnl/settled_cts, net/fill = the difference)."""
import re
import unittest

import numpy as np
import pandas as pd

import imm_saturday_tracker as sat


def _row(et_date, group, day_type, partial, resting, rent, fills, settled_cts=0.0, settle_pnl=0.0,
         mo24_per_ct=None, mult=1.0):
    mo24_n = fills if mo24_per_ct is not None else 0.0
    return dict(et_date=et_date, group=group, day_type=day_type, partial=partial, resting=float(resting),
                rent=float(rent), fills=float(fills), settled_cts=float(settled_cts), settle_pnl=float(settle_pnl),
                mo24_w=(mo24_per_ct or 0.0) * mo24_n, mo24_n=float(mo24_n), mult=float(mult))


def _frame() -> pd.DataFrame:
    return pd.DataFrame([
        # long-dated: a full Saturday, a full weekday, a PARTIAL weekday, a full Sunday with settled losses
        _row("2026-09-19", "long-dated", "Saturday", False, 50000, 568.0, 5800, mo24_per_ct=-2.0, mult=1.5),
        _row("2026-09-18", "long-dated", "Weekday", False, 33000, 464.0, 5288, mo24_per_ct=-5.0),
        _row("2026-09-14", "long-dated", "Weekday", True, 35000, 298.0, 4293, settled_cts=800, settle_pnl=-35.6),
        _row("2026-09-13", "long-dated", "Sunday", False, 19333, 152.8, 2433, settled_cts=340, settle_pnl=-46.512),
        # excluded (control): Saturday with a settled GAIN (negative loss), plus a weekday
        _row("2026-09-19", "excluded", "Saturday", False, 813, 42.4, 1621, settled_cts=1500, settle_pnl=7.65, mult=0.86),
        _row("2026-09-18", "excluded", "Weekday", False, 1921, 56.0, 1743, settled_cts=1690, settle_pnl=-48.2, mult=0.86),
    ])


class RenderHtmlTests(unittest.TestCase):
    def setUp(self):
        self.g = _frame()
        self.html = sat.render_html(self.g, "2026-09-20")

    def test_real_tables_not_a_pre_dump(self):
        # knobs, 2 headline, 2 per-day, legend
        self.assertGreaterEqual(self.html.count("<table"), 6)
        self.assertNotIn("<pre", self.html)
        for label in ("Rent/fill", "Net/ct-day", "Mark-out 24h", "Settled %", "Mult"):
            self.assertIn(label, self.html)

    def test_saturday_rows_are_tinted(self):
        # headline (now + baseline + delta) x 2 groups + per-day Saturday x 2 groups
        self.assertGreaterEqual(self.html.count(sat._SAT_BG), 8)

    def test_partial_day_is_starred_muted_and_kept_out_of_the_pool(self):
        self.assertIn("2026-09-14*", self.html)
        self.assertNotIn("2026-09-18*", self.html)
        per_day, pooled_m, _ = sat._tables(self.g)
        self.assertEqual(pooled_m.loc[("long-dated", "Weekday"), "days"], 1)   # 9/14 excluded
        self.assertEqual(pooled_m.loc[("long-dated", "Saturday"), "days"], 1)

    def test_missing_values_render_as_a_dash_never_nan(self):
        self.assertIsNone(re.search(r"\bnan\b", self.html, re.I))
        self.assertIn("–", self.html)          # the Saturday row has no settled fills yet

    def test_baseline_row_and_delta_under_each_day_type(self):
        self.assertIn(sat.BASELINE_LABEL, self.html)
        self.assertIn("Δ vs baseline", self.html)
        # long-dated Saturday rent $/day: 568.0 now vs 137.6 baseline, green in the delta row
        self.assertIn('color:#0a7a2f">+430.4', self.html)
        # ...but rent per FILL fell: 9.79 vs 13.48 -> -3.69, red (the decision metric)
        self.assertIn('color:#c0392b">-3.69', self.html)
        # a lower loss than baseline is GOOD: excluded Saturday -0.51 vs 2.63 -> -3.14, green
        self.assertIn('color:#0a7a2f">-3.14', self.html)
        # level rows leave rent plain: no coloured 9.79 anywhere
        self.assertNotIn('">9.79</span>', self.html)

    def test_negative_net_is_red_positive_green(self):
        # long-dated Sunday: rent/fill 6.28 - loss/fill 13.68 = -7.40
        self.assertIn('color:#c0392b">-7.40', self.html)
        # excluded Saturday: 2.62 - (-0.51) = +3.13 net/fill, green
        self.assertIn('color:#0a7a2f">3.13', self.html)

    def test_knobs_block_states_the_live_config(self):
        self.assertIn("Saturday multiplier", self.html)
        self.assertIn(f"×{sat.imm.SAT_SIZE_MULT:g} on Saturdays", self.html)
        self.assertIn("Quiet hours", self.html)
        self.assertIn("2026-09-12 → 2026-09-20", self.html)

    def test_empty_window(self):
        html = sat.render_html(pd.DataFrame(), "2026-09-20")
        self.assertIn("no cycle-log rows", html)
        self.assertIn("<table", html)              # the knobs block still renders


class RenderTextTests(unittest.TestCase):
    def test_text_keeps_its_sections_and_partial_marker(self):
        text = sat.render(_frame(), "2026-09-20")
        self.assertIn("== per day: LONG-DATED", text)
        self.assertIn("== per day: EXCLUDED", text)
        self.assertIn("== pooled by day type since", text)
        self.assertIn("== baseline 2026-08-08..09-11", text)
        self.assertIn("2026-09-14*", text)
        self.assertNotIn("nan", text)

    def test_text_part_survives_a_cp1252_console(self):
        # the scheduled task prints the text to a cp1252-redirected stdout; a
        # stray glyph there raises UnicodeEncodeError BEFORE the email is sent
        # (the new-programs email failed exactly this way on 2026-09-16..18)
        text = sat.render(_frame(), "2026-09-20")
        text.encode("cp1252")
        self.assertIn(f"Saturday multiplier: x{sat.imm.SAT_SIZE_MULT:g}", text)
        self.assertIn("2026-09-12 -> 2026-09-20", text)


class HelperTests(unittest.TestCase):
    def test_hours_summary_collapses_runs(self):
        self.assertEqual(sat._hours_summary({h: 2.0 for h in range(10)}), "0-9 ET x2")
        self.assertEqual(sat._hours_summary({h: 2.0 for h in range(10)}, html=True), "0–9 ET ×2")
        self.assertEqual(sat._hours_summary({}), "off")
        self.assertEqual(sat._hours_summary({0: 2.0, 1: 2.0, 5: 0.5}), "0-1 ET x2; 5 ET x0.5")

    def test_fmt_and_colour(self):
        self.assertEqual(sat._fmt("resting", 12345.6), "12,346")
        self.assertEqual(sat._fmt("rent/fill", float("nan")), "–")
        self.assertEqual(sat._fmt("net/fill", 1.5, signed=True), "+1.50")
        self.assertNotIn("<span", sat._colour("resting", 5.0, "5"))         # unsigned column: no colour
        self.assertIn("#c0392b", sat._colour("loss/fill", 2.0, "2.00"))     # a loss is red
        self.assertIn("#0a7a2f", sat._colour("net/ct-day", 0.5, "0.50"))
        self.assertNotIn("<span", sat._colour("rent/fill", -1.0, "-1.00"))          # level row: plain
        self.assertIn("#c0392b", sat._colour("rent/fill", -1.0, "-1.00", delta=True))  # delta row: red


if __name__ == "__main__":
    unittest.main()
