"""Tests for imm_saturday_tracker's renderers (Jack 2026-09-21: the email was a
<pre> dump of DataFrame.to_string(); it must be real, legible tables now).

The frame under test is what build_rows() returns: one row per (et_date,
group) with the summed inputs _derive() works from. Values are chosen so the
derived metrics are the ones the assertions name (rent/fill = 100*rent/fills,
loss/fill = -100*settle_pnl/settled_cts, net/fill = the difference)."""
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import imm_saturday_tracker as sat

# never read the box's live Saturday-gate verdict (see test_incentive_mm)
sat.imm.SAT_GATE_FILE = "sat_mult_gate.absent-under-test.json"
sat.imm.SAT_GATE_STATE.update(mtime=0.0, verdict="", mult=0.0, effective_from=None)


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


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _blk(et_date, block, ct_h, rent_usd, fills, mark=-3.0, hm=1.0, settled_cts=0.0, settle_pnl=0.0):
    """One gate_blocks() row. Rates it produces: rent 1000*100*rent_usd/ct_h
    and fills 1000*fills/ct_h per 1k resting contract-hours; net = rent +
    fills*mark."""
    return dict(et_date=et_date, block=block, day_type=sat.day_type(et_date), hours=10 if block == "quiet" else 14,
                ct_h=float(ct_h), rent_usd=float(rent_usd), fills=float(fills), mk_w=float(mark) * float(fills),
                mk_n=float(fills), settled_cts=float(settled_cts), settle_pnl=float(settle_pnl),
                hm=float(hm) if block == "day" else np.nan)


WEEKDAYS = ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")
DONE = _utc(2026, 9, 21, 12)          # Saturday 9/19 ended 9/20 04:00Z; +24h passed


def _week(sat_quiet=None, sat_day=None, sat_date="2026-09-19", weekdays=WEEKDAYS):
    """Weekdays: quiet net 42.5 - 4*5 = 22.5, day net 51.4 - 11.4*3.5 = 11.4.
    Saturday default: quiet net 48.3 + 3*0.7 = 50.4, day net 44.1 - 6.8*3.2 = 22.3."""
    rows = []
    for d in weekdays:
        rows.append(_blk(d, "quiet", 400_000, 170, 1_600, mark=-5.0))
        rows.append(_blk(d, "day", 350_000, 180, 4_000, mark=-3.5))
    rows.append(_blk(sat_date, "quiet", **(sat_quiet or dict(ct_h=600_000, rent_usd=290, fills=1_800, mark=0.7))))
    rows.append(_blk(sat_date, "day", **(sat_day or dict(ct_h=590_000, rent_usd=260, fills=4_000, mark=-3.2, hm=1.49))))
    return pd.DataFrame(rows)


class GateEvaluateTests(unittest.TestCase):
    """The x2 gate (Jack 2026-09-26: "2x next saturday if today + prior
    saturdays show no sign of edge degradation") over gate_blocks-shaped frames."""

    def _failed(self, g):
        return {(c["saturday"], c["check"], c["block"]) for c in g["checks"] if c["ok"] is False}

    def test_a_saturday_with_its_premium_intact_passes(self):
        g = sat.evaluate_gate(_week(), DONE, "2026-09-20", 1.5)
        self.assertEqual(g["status"], "PASS", g["reason"])
        self.assertEqual(g["saturdays"], ["2026-09-19"])
        self.assertEqual({r["anchor"] for r in g["rows"]}, {"same week"})
        g4 = [c for c in g["checks"] if c["check"] == "G4 settled"][0]
        self.assertIsNone(g4["ok"])                   # nothing settled: not judged, not a fail
        day = [r for r in g["rows"] if r["block"] == "day"][0]
        self.assertAlmostEqual(day["sat"]["net_k"], 44.07 - 6.78 * 3.2, delta=0.1)
        self.assertAlmostEqual(day["wk"]["net_k"], 51.43 - 11.43 * 3.5, delta=0.1)

    def test_pending_until_the_saturday_is_a_day_plus_24h_old(self):
        g = sat.evaluate_gate(_week(), _utc(2026, 9, 21, 3, 59), "2026-09-20", 1.5)
        self.assertEqual(g["status"], "PENDING")
        self.assertTrue(all(c["ok"] is None for c in g["checks"] if c["saturday"] in ("2026-09-19", "pooled")))
        self.assertEqual(sat.evaluate_gate(_week(), _utc(2026, 9, 21, 4, 0), "2026-09-20", 1.5)["status"], "PASS")

    def test_g1_a_block_below_its_weekdays_fails(self):
        # day block rent 100/590k = 16.9 -> net 16.9 - 6.8*3.2 = -4.8 < weekday 11.4
        g = sat.evaluate_gate(_week(sat_day=dict(ct_h=590_000, rent_usd=100, fills=4_000, mark=-3.2, hm=1.49)),
                              DONE, "2026-09-20", 1.5)
        self.assertEqual(g["status"], "FAIL")
        self.assertIn(("2026-09-19", "G1 net vs weekdays", "day"), self._failed(g))
        self.assertNotIn(("2026-09-19", "G1 net vs weekdays", "quiet"), self._failed(g))
        self.assertIn("G1 net vs weekdays", g["reason"])

    def test_g2_a_saturday_that_nets_negative_fails_even_above_weak_weekdays(self):
        rows = _week(sat_quiet=dict(ct_h=600_000, rent_usd=10, fills=1_800, mark=-9.0),
                     sat_day=dict(ct_h=590_000, rent_usd=10, fills=4_000, mark=-9.0, hm=1.49))
        g = sat.evaluate_gate(rows, DONE, "2026-09-20", 1.5)
        self.assertIn(("2026-09-19", "G2 net positive", "boosted"), self._failed(g))

    def test_g3_markouts_more_than_two_cents_worse_fail(self):
        worse = _week(sat_quiet=dict(ct_h=600_000, rent_usd=900, fills=1_800, mark=-6.5),
                      sat_day=dict(ct_h=590_000, rent_usd=900, fills=4_000, mark=-6.5, hm=1.49))
        g = sat.evaluate_gate(worse, DONE, "2026-09-20", 1.5)
        self.assertEqual(self._failed(g), {("2026-09-19", "G3 mark-out", "boosted")})   # rent keeps G1/G2 green
        ok = _week(sat_quiet=dict(ct_h=600_000, rent_usd=900, fills=1_800, mark=-5.5),
                   sat_day=dict(ct_h=590_000, rent_usd=900, fills=4_000, mark=-5.5, hm=1.49))
        self.assertEqual(sat.evaluate_gate(ok, DONE, "2026-09-20", 1.5)["status"], "PASS")   # within 2c

    def test_g4_settled_loss_judged_only_past_the_minimum(self):
        small = _week(sat_day=dict(ct_h=590_000, rent_usd=260, fills=4_000, mark=-3.2, hm=1.49,
                                   settled_cts=400, settle_pnl=-400))
        g = sat.evaluate_gate(small, DONE, "2026-09-20", 1.5)
        self.assertEqual(g["status"], "PASS")
        big = _week(sat_day=dict(ct_h=590_000, rent_usd=260, fills=4_000, mark=-3.2, hm=1.49,
                                 settled_cts=800, settle_pnl=-200))      # 25c lost per settled fill > 9.5c rent
        g = sat.evaluate_gate(big, DONE, "2026-09-20", 1.5)
        self.assertEqual(self._failed(g), {("pooled", "G4 settled", "boosted")})

    def test_only_boosted_hours_are_judged(self):
        # 9/12: the knob went live ~09:10 ET, so its 0-9 block is not an observation
        rows = _week(sat_date="2026-09-12")
        g = sat.evaluate_gate(rows, _utc(2026, 9, 21, 12), "2026-09-20", 1.5)
        self.assertEqual([(r["saturday"], r["block"]) for r in g["rows"]], [("2026-09-12", "day")])
        self.assertEqual({r["anchor"] for r in g["rows"]}, {"all weekdays"})   # its own week predates the window
        # a Saturday whose day block shows no multiplier (knob off) is skipped
        off = _week(sat_day=dict(ct_h=590_000, rent_usd=260, fills=4_000, mark=-3.2, hm=1.0))
        g = sat.evaluate_gate(off, DONE, "2026-09-20", 1.5)
        self.assertEqual((g["status"], g["saturdays"]), ("PENDING", []))

    def test_every_boosted_saturday_must_pass(self):
        both = pd.concat([_week(), _week(sat_date="2026-09-26",
                                         weekdays=("2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"),
                                         sat_day=dict(ct_h=590_000, rent_usd=100, fills=4_000, mark=-3.2, hm=1.49))])
        g = sat.evaluate_gate(both, _utc(2026, 9, 28, 11, 40), "2026-09-27", 1.5)
        self.assertEqual(g["saturdays"], ["2026-09-19", "2026-09-26"])
        self.assertEqual(g["status"], "FAIL")
        self.assertEqual({c[0] for c in self._failed(g)}, {"2026-09-26"})

    def test_empty_window(self):
        self.assertEqual(sat.evaluate_gate(pd.DataFrame(), DONE, "2026-09-20", 1.5)["status"], "PENDING")


X2_WEEKS = (("2026-10-03", ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02")),
            ("2026-10-10", ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")),
            ("2026-10-17", ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")))
AFTER_X2 = _utc(2026, 10, 19, 12)     # the Monday run after the third x2 Saturday
# an x2 Saturday that earns pro rata: the x1.5 default's rent per ct-h at 4/3 the size
X2_PRO_RATA = dict(sat_quiet=dict(ct_h=800_000, rent_usd=387, fills=2_400, mark=0.7),
                   sat_day=dict(ct_h=787_000, rent_usd=347, fills=5_300, mark=-3.2, hm=1.98))
# the same size earning the x1.5 rent: the extra contracts earned nothing
X2_DILUTED = dict(sat_quiet=dict(ct_h=800_000, rent_usd=290, fills=1_800, mark=0.7),
                  sat_day=dict(ct_h=787_000, rent_usd=260, fills=4_000, mark=-3.2, hm=1.98))


def _x2(n=3, **sat_blocks):
    """The 9/19 x1.5 week plus n x2 weeks (each Saturday's blocks from
    `sat_blocks`, default X2_PRO_RATA)."""
    frames = [_week()]
    for d, wk in X2_WEEKS[:n]:
        frames.append(_week(sat_date=d, weekdays=wk, **(sat_blocks or X2_PRO_RATA)))
    return pd.concat(frames, ignore_index=True)


class StepUpWatchTests(unittest.TestCase):
    """Jack 2026-09-28: "keep an eye on if i should increase it even further,
    after a few saturdays at 2x" -- report only, never the bot's knob."""

    def test_off_without_a_step_up_in_force(self):
        st = sat.evaluate_stepup(_x2(), AFTER_X2, "2026-10-18", 1.5, 0.0)
        self.assertEqual(st["status"], "OFF")
        self.assertEqual(sat.stepup_text(st), [])
        self.assertEqual(sat.stepup_subject(st), "")
        self.assertEqual(sat.evaluate_stepup(_x2(), AFTER_X2, "2026-10-18", 1.5, 1.5)["status"], "OFF")

    def test_watching_until_three_x2_saturdays(self):
        st = sat.evaluate_stepup(_week(), _utc(2026, 9, 28, 11, 40), "2026-09-27", 1.5, 2.0)
        self.assertEqual((st["status"], st["reason"]), ("WATCHING", "no Saturday at x2 yet"))
        for n in (1, 2):
            st = sat.evaluate_stepup(_x2(n), AFTER_X2, "2026-10-18", 1.5, 2.0)
            self.assertEqual(st["status"], "WATCHING")
            self.assertEqual(st["prev_saturdays"], ["2026-09-19"])
            self.assertIn(f"{n} of 3 Saturdays at x2 judged, none degrading", st["reason"])
            self.assertEqual(sat.stepup_subject(st), f" - x2 watch {n}/3")

    def test_raise_after_three_clean_x2_saturdays_earning_pro_rata(self):
        st = sat.evaluate_stepup(_x2(), AFTER_X2, "2026-10-18", 1.5, 2.0)
        self.assertEqual(st["status"], "RAISE", st["reason"])
        self.assertEqual(st["saturdays"], ["2026-10-03", "2026-10-10", "2026-10-17"])
        self.assertEqual((st["prev_mult"], st["next_mult"]), (1.5, 2.5))
        self.assertAlmostEqual(st["rent_keep"], 1.0, delta=0.02)
        self.assertIn("x2.5 is worth trying", st["reason"])
        self.assertIn("IMM_SAT_SIZE_MULT_GATED=2.5", sat.stepup_how(st))
        self.assertEqual(sat.stepup_subject(st), " - step-up: x2.5 worth trying")

    def test_hold_when_the_extra_size_earns_nothing(self):
        st = sat.evaluate_stepup(_x2(**X2_DILUTED), AFTER_X2, "2026-10-18", 1.5, 2.0)
        self.assertEqual(st["status"], "HOLD", st["reason"])
        self.assertAlmostEqual(st["rent_keep"], 0.75, delta=0.02)        # x1.5/x2: zero marginal rent
        self.assertIn("crowding its own share", st["reason"])
        self.assertTrue(all(c["ok"] is not False for c in st["checks"]))  # G1-G4 still pass
        self.assertEqual(sat.stepup_how(st), "")

    def test_degraded_on_any_failed_x2_check_even_early(self):
        bad = dict(sat_quiet=dict(ct_h=800_000, rent_usd=387, fills=2_400, mark=0.7),
                   sat_day=dict(ct_h=787_000, rent_usd=100, fills=5_300, mark=-3.2, hm=1.98))
        st = sat.evaluate_stepup(_x2(1, **bad), AFTER_X2, "2026-10-18", 1.5, 2.0)
        self.assertEqual(st["status"], "DEGRADED")
        self.assertIn("G1 net vs weekdays", st["reason"])
        self.assertIn('"verdict": "FAIL"', sat.stepup_how(st))
        self.assertEqual(sat.stepup_subject(st), " - x2 DEGRADED: consider x1.5")

    def test_levels_and_mixed_saturdays(self):
        # a Saturday that ran part x1.5, part x2 (1.75) is neither level
        mixed = dict(sat_quiet=X2_PRO_RATA["sat_quiet"], sat_day=dict(X2_PRO_RATA["sat_day"], hm=1.75))
        rows = pd.concat([_x2(2), _week(sat_date="2026-10-17", weekdays=X2_WEEKS[2][1], **mixed)],
                         ignore_index=True)
        st = sat.evaluate_stepup(rows, AFTER_X2, "2026-10-18", 1.5, 2.0)
        self.assertEqual(st["saturdays"], ["2026-10-03", "2026-10-10"])
        # at x2.5 the comparison is the x2 Saturdays, and going back means x2
        x25 = dict(sat_quiet=dict(ct_h=1_000_000, rent_usd=480, fills=3_000, mark=0.7),
                   sat_day=dict(ct_h=985_000, rent_usd=434, fills=6_600, mark=-3.2, hm=2.47))
        rows = pd.concat([_x2(2), _week(sat_date="2026-10-17", weekdays=X2_WEEKS[2][1], **x25)],
                         ignore_index=True)
        st = sat.evaluate_stepup(rows, AFTER_X2, "2026-10-18", 1.5, 2.5)
        self.assertEqual((st["saturdays"], st["prev_mult"], st["next_mult"]), (["2026-10-17"], 2.0, 3.0))
        self.assertEqual(st["prev_saturdays"], ["2026-10-03", "2026-10-10"])

    def test_the_section_renders_in_text_html_and_subject(self):
        st = sat.evaluate_stepup(_x2(), AFTER_X2, "2026-10-18", 1.5, 2.0)
        text = sat.render(_frame(), "2026-10-18", sat.evaluate_gate(_x2(), AFTER_X2, "2026-10-18", 1.5), False, st)
        text.encode("cp1252")
        self.assertIn("== step-up watch: x2 -> x2.5?", text)
        self.assertIn("status: RAISE", text)
        self.assertNotIn("nan", text)
        html = sat.render_html(_frame(), "2026-10-18", None, False, st)
        self.assertIn("Step-up watch: ×2 → ×2.5?", html)
        self.assertIn(">RAISE<", html)
        self.assertIn("IMM_SAT_SIZE_MULT_GATED=2.5", html)
        self.assertNotIn("Step-up watch", sat.render_html(_frame(), "2026-10-18", None, False,
                                                          dict(st, status="OFF")))


class GateVerdictFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "sat_mult_gate.json")
        self.gate = sat.evaluate_gate(_week(), DONE, "2026-09-20", 1.5)

    def test_next_saturday_after(self):
        d = lambda s: datetime.strptime(s, "%Y-%m-%d").date()
        self.assertEqual(sat.next_saturday_after(d("2026-09-28")), d("2026-10-03"))   # Monday
        self.assertEqual(sat.next_saturday_after(d("2026-10-02")), d("2026-10-03"))   # Friday
        self.assertEqual(sat.next_saturday_after(d("2026-09-26")), d("2026-10-03"))   # a Saturday -> the next

    def test_written_once_and_read_back_by_the_bot_format(self):
        monday = _utc(2026, 9, 28, 11, 40)                 # the 07:40 ET run
        self.assertTrue(sat.write_gate_verdict(self.gate, monday, "2026-09-27", 1.5, 2.0, self.path))
        with open(self.path, encoding="utf-8") as f:
            v = json.load(f)
        self.assertEqual((v["verdict"], v["mult"], v["base_mult"], v["effective_from"]),
                         ("PASS", 2.0, 1.5, "2026-10-03"))
        self.assertEqual(v["saturdays"], ["2026-09-19"])
        self.assertTrue(v["checks"])
        # one-shot: the next Monday's run never overwrites; --gate-rewrite does
        fail = dict(self.gate, status="FAIL")
        self.assertFalse(sat.write_gate_verdict(fail, monday, "2026-09-27", 1.5, 2.0, self.path))
        self.assertTrue(sat.write_gate_verdict(fail, monday, "2026-09-27", 1.5, 2.0, self.path, overwrite=True))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["verdict"], "FAIL")

    def test_pending_and_error_are_never_written(self):
        for status in ("PENDING", "ERROR"):
            self.assertFalse(sat.write_gate_verdict(dict(self.gate, status=status), DONE, "2026-09-20", 1.5, 2.0,
                                                    self.path, overwrite=True))
        self.assertFalse(os.path.exists(self.path))


class GateInputsTests(unittest.TestCase):
    def test_gate_blocks_counts_fills_in_logged_hours_only(self):
        # the parser records hour_mult (q_rows / hm_sum) outside the 0-9 quiet window only
        ser = pd.DataFrame([dict(et_date="2026-09-19", et_hour=h, series="KXTRUMPMENTION", sum_est_usd=240.0,
                                 sum_quoted=50_000.0, n_rows=50, q_rows=(50 if h >= 10 else 0),
                                 hm_sum=(75.0 if h >= 10 else 0.0)) for h in (3, 12)])
        cyc = pd.DataFrame([dict(et_date="2026-09-19", et_hour=h, n_cycles=50) for h in (3, 12)])
        scored = pd.DataFrame([dict(et_date="2026-09-19", et_hour=h, group="long-dated", cnt=10.0, mk_w=-20.0,
                                    mk_n=10.0, settled_cts=0.0, settle_pnl=np.nan) for h in (3, 12, 15)])
        b = sat.gate_blocks(ser, cyc, scored, "2026-09-20").set_index("block")
        self.assertEqual(b.loc["day", "fills"], 10.0)       # the 15:00 fill sits in an unlogged hour
        self.assertEqual(b.loc["quiet", "fills"], 10.0)
        self.assertAlmostEqual(b.loc["day", "ct_h"], 1_000.0)
        self.assertAlmostEqual(b.loc["day", "rent_usd"], 240.0 / 50 / 24)
        self.assertAlmostEqual(b.loc["day", "hm"], 1.5)
        self.assertTrue(np.isnan(b.loc["quiet", "hm"]))     # the parser records hour_mult outside 0-9 only

    def test_mark_falls_back_to_settlement_then_last_mid(self):
        t = 1_789_000_200                                   # on a 10-minute boundary
        mids = {"A": ([t, t + 86400], [40.0, 55.0]),       # 24h mid exists
                "B": ([t], [40.0]),                         # settled, no 24h mid
                "C": ([t, t + 3 * 3600], [40.0, 30.0])}     # unsettled, logged 3h after only
        fills = pd.DataFrame([dict(t=t, et_date="2026-09-19", et_hour=12, ticker=k, series="KXFOO",
                                   eff_side="yes", px=40.0, cnt=1.0) for k in ("A", "B", "C")])
        f = sat.score_fills(fills, mids, {"B": 0.0}).set_index("ticker")     # load_results(): settled NO
        self.assertEqual(f.loc["A", "mo24"], 15.0)
        self.assertEqual(f.loc["A", "mk"], 15.0)
        self.assertTrue(np.isnan(f.loc["B", "mo24"]))
        self.assertEqual(f.loc["B", "mk"], -40.0)           # settled NO: a YES bought at 40 marks to 0
        self.assertTrue(np.isnan(f.loc["C", "mo24"]))
        self.assertEqual(f.loc["C", "mk"], -10.0)           # last logged mid inside the 24h


class SettlementTests(unittest.TestCase):
    """Scalar / void settlements (2026-09-29): the NFL ladders / escalators
    settle "scalar" at a fractional value and a void refunds cost; load_results
    kept only yes / no rows, so those fills never counted as settled."""

    def _sink(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "settlements_2026-09-28.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            f.write("not json\n")
        old = sat.STATUS_DIR
        sat.STATUS_DIR = tmp.name
        self.addCleanup(setattr, sat, "STATUS_DIR", old)

    def test_load_results_reads_scalar_and_void_rows(self):
        self._sink([{"ticker": "Y", "result": "yes", "settle_price_cents": 100.0},
                    {"ticker": "N", "result": "no", "settle_price_cents": 0.0},
                    {"ticker": "S", "result": "scalar", "settle_price_cents": 21.6},
                    {"ticker": "Z", "result": "scalar", "settle_price_cents": 0.0},
                    {"ticker": "V", "result": "void", "settle_price_cents": 37.5},
                    {"ticker": "U", "result": "scalar", "settle_price_cents": None},
                    {"ticker": "M", "result": "manual_offset", "settle_price_cents": None}])
        self.assertEqual(sat.load_results(), {"Y": 100.0, "N": 0.0, "S": 21.6, "Z": 0.0, "V": sat.VOID})

    def test_settle_pnl_per_side_at_the_scalar_value_and_void_at_cost(self):
        t = 1_789_000_200
        fills = pd.DataFrame([dict(t=t, et_date="2026-09-27", et_hour=12, ticker=k, series="KXNFLFFPTSLADDER",
                                   eff_side=s, px=px, cnt=10.0)
                              for k, s, px in (("S", "yes", 16.0), ("S", "no", 84.0), ("V", "no", 60.0),
                                               ("Y", "yes", 30.0), ("O", "yes", 30.0))])
        f = sat.score_fills(fills, {}, {"S": 12.0, "V": sat.VOID, "Y": 100.0})
        pnl = f["settle_pnl"].tolist()
        self.assertAlmostEqual(pnl[0], 10 * (12.0 - 16.0) / 100)      # YES pays the value
        self.assertAlmostEqual(pnl[1], 10 * (88.0 - 84.0) / 100)      # NO pays 100 minus it
        self.assertEqual(pnl[2], 0.0)                                  # void: refunded at cost
        self.assertAlmostEqual(pnl[3], 7.0)                            # yes / no unchanged
        self.assertTrue(np.isnan(pnl[4]))                              # unsettled
        self.assertEqual(f["settled_cts"].tolist(), [10.0, 10.0, 10.0, 10.0, 0.0])
        # no 24h mid logged: the gate's mark falls back to the settlement
        self.assertEqual(f["mk"].tolist()[:4], [-4.0, 4.0, 0.0, 70.0])
        self.assertTrue(np.isnan(f["mk"].tolist()[4]))
        self.assertTrue(f["mo24"].isna().all())


class GateRenderTests(unittest.TestCase):
    def setUp(self):
        self.gate = sat.evaluate_gate(_week(), DONE, "2026-09-20", 1.5)

    def test_text_section_is_ascii_and_has_no_nan(self):
        text = sat.render(_frame(), "2026-09-20", self.gate, written=True)
        text.encode("cp1252")
        self.assertIn("== 2x gate", text)
        self.assertIn("status: PASS - verdict WRITTEN this run", text)
        self.assertNotIn("nan", text)
        self.assertIn("Saturday step-up:", text)

    def test_html_section(self):
        html = sat.render_html(_frame(), "2026-09-20", self.gate)
        self.assertIn("2× gate", html)
        self.assertIn("G1 net vs weekdays", html)
        self.assertIn(">PASS<", html)

    def test_pending_rows_render_a_dash(self):
        g = sat.evaluate_gate(_week(sat_day=dict(ct_h=590_000, rent_usd=260, fills=4_000, mark=float("nan"),
                                                  hm=1.49)).assign(mk_n=lambda d: np.where(d["day_type"] == "Saturday", 0.0, d["mk_n"]),
                                                                   mk_w=lambda d: np.where(d["day_type"] == "Saturday", 0.0, d["mk_w"])),
                              _utc(2026, 9, 20, 12), "2026-09-20", 1.5)
        text = "\n".join(sat.gate_text(g, False))
        self.assertIn("PENDING", text)
        self.assertNotIn("nan", text)


if __name__ == "__main__":
    unittest.main()
