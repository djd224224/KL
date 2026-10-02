#!/usr/bin/env python3
"""Tests for imm_reward_recon.py and the digest's credit-ledger reporting.

The reconciliation is only worth having if it fails LOUDLY: a parser that
silently drops rows, or a merge that silently duplicates them, produces a
number that looks authoritative and is wrong. Most of what is asserted here is
that behaviour, not the happy path.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

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


class TestPerPeriodFloor(unittest.TestCase):
    """The $1 floor is per market PER PROGRAM PERIOD (Jack 2026-09-27: "The $1
    minimum payout is applied to each market's whole history rather than per
    program period ... fix this")."""

    def test_overlaps_merge_but_a_relisting_is_a_new_period(self):
        got = rec.merge_periods([
            ("2026-09-10T16:49:00Z", "2026-09-22T16:49:00Z", True),
            ("2026-09-22T16:49:00Z", "2026-09-23T16:49:00Z", False),   # re-listed
            ("2026-09-22T20:00:00Z", "2026-09-24T00:00:00Z", False),   # overlaps it
            ("2026-09-25T00:00:00Z", "2026-09-25T00:00:00Z", False),   # empty: dropped
        ])
        self.assertEqual(got, [["2026-09-10T16:49:00Z", "2026-09-22T16:49:00Z", True],
                               ["2026-09-22T16:49:00Z", "2026-09-24T00:00:00Z", False]])

    def test_a_boundary_hour_is_apportioned(self):
        h0 = 1000 * 3600.0
        per, out = rec.split_by_period({1000: 1.2}, [(h0, h0 + 900.0), (h0 + 900.0, h0 + 7200.0)])
        self.assertAlmostEqual(per[0], 0.3)
        self.assertAlmostEqual(per[1], 0.9)
        self.assertAlmostEqual(out, 0.0)
        per, out = rec.split_by_period({1000: 1.0, 1005: 2.0}, [(h0, h0 + 3600.0)])
        self.assertEqual((per, out), ([1.0], 2.0))

    def test_an_hour_a_period_only_partly_covers_belongs_to_it(self):
        # a program starting at :30 accrues only from :30, so the whole hour
        # bucket is that period's -- splitting it by clock time left half of
        # it "outside" as a sub-$1 fragment the floor then threw away
        h0 = 1000 * 3600.0
        per, out = rec.split_by_period({1000: 0.8, 1001: 0.9},
                                       [(h0 + 1800.0, h0 + 5400.0)])
        self.assertAlmostEqual(per[0], 1.7)
        self.assertEqual(out, 0.0)
        paid, n = rec.floored_accrual(1.7, {1000: 0.8, 1001: 0.9},
                                      [(h0 + 1800.0, h0 + 5400.0)])[:2]
        self.assertEqual((round(paid, 6), n), (1.7, 1))

    def test_the_floor_applies_per_period(self):
        periods = [(i * 7200.0, (i + 1) * 7200.0) for i in range(4)]
        four_small = {2 * i: 0.60 for i in range(4)}          # $0.60 in each period
        self.assertEqual(rec.floored_accrual(2.40, four_small, periods)[:2], (0.0, 0))
        paid, n, per, out = rec.floored_accrual(1.90, {0: 1.50, 2: 0.40}, periods[:2])
        self.assertEqual((round(paid, 6), n), (1.50, 1))
        # without program metadata or hourly data: the lifetime test, as before
        self.assertEqual(rec.floored_accrual(2.40, four_small, None)[:2], (2.40, 1))
        self.assertEqual(rec.floored_accrual(0.90, None, periods)[:2], (0.0, 0))

    def test_accrual_in_no_known_period_is_floored_on_its_own(self):
        paid, n, per, out = rec.floored_accrual(3.0, {0: 0.5, 100: 2.5}, [(0.0, 3600.0)])
        self.assertEqual((round(paid, 6), n, round(out, 6)), (2.5, 1, 2.5))

    def test_kill_switch_restores_the_lifetime_test(self):
        hours = {0: 0.6, 2: 0.6, 4: 0.6, 6: 0.6}
        periods = [(i * 7200.0, (i + 1) * 7200.0) for i in range(4)]
        with mock.patch.object(rec, "FLOOR_PER_PERIOD", False):
            self.assertEqual(rec.floored_accrual(2.40, hours, periods)[:2], (2.40, 1))

    def test_hourly_scan_adds_up_to_the_totals(self):
        # four cycles 30s apart across an hour boundary; each interval accrues
        # frac 0.1 x pool 86400/day x 30s = $3.00
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8", newline="")
        fh.write("ts,ticker,a,b,c,d,e,est_frac,sides,f,g,pool,h\n")
        for ts in ("10:59:00", "10:59:30", "11:00:00", "11:00:30"):
            fh.write(f"2026-09-27T{ts}Z,KXA-26SEP27-T1,0,0,0,0,0,0.1,2,0,0,86400,0\n")
        fh.close()
        hourly = {}
        tot = rec._scan_cycle_log(fh.name, 900.0, hourly=hourly)
        os.unlink(fh.name)
        self.assertAlmostEqual(tot["KXA-26SEP27-T1"][0], 9.0)
        hrs = hourly["KXA-26SEP27-T1"]
        self.assertAlmostEqual(sum(hrs.values()), 9.0)
        self.assertEqual(sorted(round(v, 6) for v in hrs.values()), [3.0, 6.0])
        # without the dict the scan is exactly what it was
        self.assertEqual(rec._scan_cycle_log.__defaults__, (None,))


def _h(iso):
    """UTC hour index of an ISO time (the hourly cache's key)."""
    from datetime import datetime as _dt
    return int(_dt.fromisoformat(iso.replace("Z", "+00:00")).timestamp() // 3600)


class _FakePrograms:
    """GET /incentive_programs by status, one list per page, cursor = page
    number; records every call."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        pages = self.pages.get(params.get("status")) or [[]]
        i = int(params.get("cursor") or 0)
        batch = pages[i] if i < len(pages) else []
        return {"incentive_programs": batch,
                "next_cursor": str(i + 1) if i + 1 < len(pages) else None}


def _prog(ticker, start, end, paid=False):
    return {"market_ticker": ticker, "incentive_type": "liquidity",
            "start_date": start, "end_date": end, "paid_out": paid}


class TestUnpaidEstimate(unittest.TestCase):
    """Jack 2026-09-28: "also include an estimated portfolio value after
    earnings are paid out". Earned-but-uncredited reward = the floored
    accrual of periods still running or ended since 00:00 ET yesterday."""

    NOW = None

    def setUp(self):
        from datetime import datetime as _dt, timezone as _tz
        self.now = _dt(2026, 9, 28, 11, 0, tzinfo=_tz.utc)        # 7am EDT
        # since = 00:00 EDT Sep 27 = 04:00Z
        self.cache = {
            "KXAAA-26SEP29-T1": {"periods": [["2026-09-27T10:00:00Z", "2026-09-29T00:00:00Z", False]]},
            "KXBBB-26SEP27-T1": {"periods": [["2026-09-26T20:00:00Z", "2026-09-27T20:00:00Z", False]]},
            "KXCCC-26SEP26-T1": {"periods": [["2026-09-25T00:00:00Z", "2026-09-26T23:00:00Z", False]]},
            "KXDDD-26SEP30-T1": {"periods": [["2026-09-27T00:00:00Z", "2026-09-30T00:00:00Z", False]]},
            "KXGGG-26SEP29-T1": {"periods": [["2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z", False],
                                              ["2026-09-27T06:00:00Z", "2026-09-29T00:00:00Z", False]]},
            "KXFOOMENTION-26SEP29-A": {"start": "2026-09-27T00:00:00Z",
                                       "end": "2026-09-29T00:00:00Z", "paid": False},
        }
        self.hourly = {
            "KXAAA-26SEP29-T1": {_h("2026-09-27T12:00:00Z"): 1.0, _h("2026-09-27T13:00:00Z"): 1.0,
                                 _h("2026-09-27T14:00:00Z"): 1.0},
            "KXBBB-26SEP27-T1": {_h("2026-09-27T10:00:00Z"): 2.0},
            "KXCCC-26SEP26-T1": {_h("2026-09-26T12:00:00Z"): 5.0},
            "KXDDD-26SEP30-T1": {_h("2026-09-27T12:00:00Z"): 0.6},
            "KXEEE-26SEP30-T1": {_h("2026-09-27T12:00:00Z"): 4.0},
            "KXEARNINGSMENTIONZZZ-26OCT01-X": {_h("2026-09-27T20:00:00Z"): 1.5},
            "KXGGG-26SEP29-T1": {_h("2026-09-20T12:00:00Z"): 10.0, _h("2026-09-27T12:00:00Z"): 1.2},
            "KXFOOMENTION-26SEP29-A": {_h("2026-09-27T12:00:00Z"): 2.0},
            "KXLATE-26SEP30-T1": {_h("2026-09-27T12:00:00Z"): 5.0},
        }
        self.calib = {"post_amendment": {"realization_factor": 1.1, "by_family": {
            "EARNINGS-MENTION": {"events": 44, "realization_factor": 1.2},
            "OTHER MENTION": {"events": 10, "realization_factor": 1.8}}}}
        self.client = _FakePrograms({
            "active": [[_prog("KXEEE-26SEP30-T1", "2026-09-27T00:00:00Z", "2026-09-30T00:00:00Z", paid=True),
                        _prog("KXEARNINGSMENTIONZZZ-26OCT01-X", "2026-09-27T18:00:00Z", "2026-09-30T00:00:00Z")]],
            "closed": [[]],
            # newest start first: page 1 reaches back past the fetch horizon, so
            # page 2 (a running program nobody should read) is never requested
            "settled": [[_prog("KXBBB-26SEP27-T1", "2026-09-26T20:00:00Z", "2026-09-27T20:00:00Z")],
                        [_prog("KXOLD-26SEP01-T1", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")],
                        [_prog("KXLATE-26SEP30-T1", "2026-08-20T00:00:00Z", "2026-09-30T00:00:00Z")]],
        })

    def _run(self):
        with mock.patch.object(rec, "load_programs", return_value=self.cache), \
                mock.patch.object(rec, "PROGRAM_CACHE", os.path.join(
                    tempfile.gettempdir(), "no_such_program_cache.json")):
            return rec.unpaid_estimate(self.client, now_utc=self.now,
                                       calib=self.calib, hourly=self.hourly)

    def test_running_and_recently_ended_periods_count(self):
        est = self._run()
        # counted: AAA 3.00 (running), BBB 2.00 (ended 9/27 20:00Z, after
        # since), GGG 1.20 (its current period only), FOOMENTION 2.00 (older
        # single-window cache record), EARNINGS 1.50 (fresh active read)
        self.assertAlmostEqual(est["raw"], 9.70)
        self.assertEqual(est["market_periods"], 5)
        self.assertEqual(est["markets"], 5)
        self.assertEqual(est["since"], "2026-09-27T04:00:00+00:00")

    def test_family_factors_and_the_thin_family_fallback(self):
        est = self._run()
        # EARNINGS-MENTION has 44 settled events -> its own 1.2; OTHER
        # MENTION has 10 -> the overall 1.1, not its 1.8; the rest -> 1.1
        self.assertAlmostEqual(est["total"], round(8.2 * 1.1 + 1.5 * 1.2, 2))
        self.assertAlmostEqual(est["by_family"]["EARNINGS-MENTION"][1], 1.80)
        self.assertAlmostEqual(est["by_family"]["OTHER MENTION"][1], 2.20)

    def test_excluded_periods(self):
        est = self._run()
        fams = est["by_family"]
        # CCC ended before 00:00 ET yesterday, DDD is under the $1 floor, EEE
        # is flagged paid_out, KXLATE sits past the paging horizon
        self.assertAlmostEqual(sum(v[0] for v in fams.values()), 9.70)
        settled_calls = [c for c in self.client.calls if c[1].get("status") == "settled"]
        self.assertEqual(len(settled_calls), 2)

    def test_since_moves_with_the_knob(self):
        with mock.patch.object(rec, "UNPAID_SINCE_DAYS", 2):
            est = self._run()
        # 00:00 ET Sep 26 -> CCC's period (ended 9/26 23:00Z) now counts
        self.assertAlmostEqual(est["raw"], 14.70)


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


class TestCalibrationSeries(unittest.TestCase):
    """The dashboard's paid rate per family is credited / RAW estimate on
    settled post-amendment events, rolled up from these per-series rows."""

    def test_post_amendment_series_carry_the_raw_estimate(self):
        d = tempfile.mkdtemp()
        cut = rec._ts(rec.AMENDMENT_CUTOVER + "+00:00")
        rows = [
            {"event": "KXA-1", "series": "KXA", "credited": 6.0, "est": 10.0, "est_floor": 8.0,
             "settled_any": True, "first": cut + 60, "imm": True},
            {"event": "KXA-2", "series": "KXA", "credited": 2.0, "est": 3.0, "est_floor": 2.5,
             "settled_any": True, "first": cut + 120, "imm": True},
            # quoted before the estimator rewrite: not calibration evidence
            {"event": "KXA-0", "series": "KXA", "credited": 50.0, "est": 1.0, "est_floor": 1.0,
             "settled_any": True, "first": cut - 3600, "imm": True},
            # not settled yet: no evidence either
            {"event": "KXB-1", "series": "KXB", "credited": 0.0, "est": 9.0, "est_floor": 9.0,
             "settled_any": False, "first": cut + 60, "imm": True},
        ]
        summary = {"lifetime": 58.0, "since_imm": 58.0, "imm_credit": 58.0,
                   "settled_credited": 58.0, "settled_est_floor": 11.5, "series": {}}
        with mock.patch.object(rec, "STATUS_DIR", d), \
                mock.patch.object(rec, "CALIB_PATH", os.path.join(d, "reward_calibration.json")):
            pay = rec.write_calibration(summary, rows, [])
        ser = pay["post_amendment"]["series"]
        self.assertEqual(ser["KXA"], {"credited": 8.0, "est": 13.0, "est_floor": 10.5, "n": 2})
        self.assertNotIn("KXB", ser)
        # the existing keys the digest reads are unchanged
        self.assertIn("by_family", pay["post_amendment"])
        self.assertAlmostEqual(pay["post_amendment"]["realization_factor"], round(8.0 / 10.5, 4))


class TestLedgerHorizon(unittest.TestCase):
    """Credits exist only as pasted statements. The 9/29 calibration counted
    the 9/27-28 NFL ladders as settled -- programs ended, paid_out set -- on a
    ledger last pasted 9/27, so they read $0 credited against $144 modelled
    and took the dashboard's Sports & awards paid rate from ~0.9 to 0.28."""

    NOW = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)   # ledger still at 9/27
    LEDGER = [("2026-09-27", "KXWEEK-26SEP26", 2.50, "Liquidity"),
              ("2026-09-25", "KXRAIN-26SEP24", 3.00, "Liquidity"),
              ("2026-09-20", "KXMLBGAME-26SEP20", 9.00, "Liquidity")]  # another bot's

    def setUp(self):
        p = mock.patch.object(rec, "CREDIT_LAG_DAYS", 1.0)   # the default, whatever the env
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _prog(start, end):
        return {"start": start, "end": end, "paid": True, "periods": [[start, end, True]]}

    @staticmethod
    def _est(first, last, est):
        return {"est": est, "first": rec._ts(first), "last": rec._ts(last),
                "cycles": 100, "two_sided": 100}

    def _reconcile(self, ledger=None):
        est = {
            # ended 00:00 ET 9/25, credited 9/25
            "KXRAIN-26SEP24-T1": self._est("2026-09-24T12:00:00Z", "2026-09-25T03:00:00Z", 3.2),
            # the bug: Sunday night's ladder, paid out, its credit not pasted yet
            "KXNFLLADDERREC-26SEP27BUFMIA-T50": self._est(
                "2026-09-25T16:00:00Z", "2026-09-28T00:20:00Z", 6.0),
            # ended 16:00 ET 9/26, credited the next day: inside the 1-day lag
            "KXWEEK-26SEP26-T1": self._est("2026-09-25T12:00:00Z", "2026-09-26T19:00:00Z", 2.4),
            # no program record: judged on its last quote, 9/28
            "KXAPP-26SEP28-T1": self._est("2026-09-24T12:00:00Z", "2026-09-28T12:00:00Z", 5.0),
        }
        progs = {
            "KXRAIN-26SEP24-T1": self._prog("2026-09-24T04:00:00Z", "2026-09-25T04:00:00Z"),
            "KXNFLLADDERREC-26SEP27BUFMIA-T50": self._prog(
                "2026-09-25T16:00:00Z", "2026-09-28T00:30:00Z"),
            "KXWEEK-26SEP26-T1": self._prog("2026-09-25T12:00:00Z", "2026-09-26T20:00:00Z"),
        }
        with mock.patch.object(rec, "load_ledger",
                               return_value=self.LEDGER if ledger is None else ledger),                 mock.patch.object(rec, "rebuild_estimates", return_value=est),                 mock.patch.object(rec, "load_programs", return_value=progs),                 mock.patch.object(rec, "rebuild_hourly", return_value={}):
            rows, led = rec.reconcile(now_utc=self.NOW)
        return {r["event"]: r for r in rows}, led

    def test_cutoff_is_the_end_of_the_day_a_lag_before_the_last_credit(self):
        # ledger through 9/27, 1-day lag -> programs ended by 00:00 EDT 9/27
        self.assertEqual(rec.ledger_cutoff(self.LEDGER), rec._ts("2026-09-27T04:00:00Z"))
        self.assertEqual(rec.ledger_cutoff(self.LEDGER, lag_days=2),
                         rec._ts("2026-09-26T04:00:00Z"))
        self.assertIsNone(rec.ledger_cutoff([]))

    def test_a_program_that_ended_after_the_last_paste_is_not_settled(self):
        rows, _ = self._reconcile()
        nfl = rows["KXNFLLADDERREC-26SEP27BUFMIA"]
        self.assertEqual(nfl["credited"], 0.0)
        self.assertFalse(nfl["settled_any"])
        self.assertTrue(nfl["after_ledger"])
        self.assertTrue(rows["KXRAIN-26SEP24"]["settled_any"])
        self.assertTrue(rows["KXWEEK-26SEP26"]["settled_any"])
        self.assertFalse(rows["KXWEEK-26SEP26"]["after_ledger"])
        # the clock-only branch (3 days since the last quote) obeys it too
        self.assertFalse(rows["KXAPP-26SEP28"]["settled_any"])
        self.assertTrue(rows["KXAPP-26SEP28"]["after_ledger"])
        # credits on an event IMM never quoted are attribution, as before
        self.assertTrue(rows["KXMLBGAME-26SEP20"]["settled_any"])

    def test_the_lag_is_a_knob(self):
        with mock.patch.object(rec, "CREDIT_LAG_DAYS", 2.0):
            rows, _ = self._reconcile()
        self.assertFalse(rows["KXWEEK-26SEP26"]["settled_any"])
        self.assertTrue(rows["KXRAIN-26SEP24"]["settled_any"])

    def test_an_empty_ledger_settles_nothing_imm_quoted(self):
        rows, _ = self._reconcile(ledger=[])
        self.assertFalse(any(r["settled_any"] for r in rows.values()))

    def test_the_calibration_the_dashboard_reads_leaves_them_out(self):
        rows, led = self._reconcile()
        d = tempfile.mkdtemp()
        with mock.patch.object(rec, "STATUS_DIR", d),                 mock.patch.object(rec, "CALIB_PATH", os.path.join(d, "reward_calibration.json")),                 contextlib.redirect_stdout(io.StringIO()) as out:
            pay = rec.write_calibration(rec.report(list(rows.values()), led),
                                        list(rows.values()), led)
        self.assertEqual(set(pay["post_amendment"]["series"]), {"KXRAIN", "KXWEEK"})
        self.assertNotIn("KXNFLLADDERREC", pay["series"])
        self.assertEqual((pay["ledger_through"], pay["settled_cutoff"]),
                         ("2026-09-27", "2026-09-27T04:00:00Z"))
        self.assertIn("2 events ended later", out.getvalue())


if __name__ == "__main__":
    unittest.main()
