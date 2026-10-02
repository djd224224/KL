"""Tests for mortgage_fair (the long-dated mortgage gate's fair values)."""
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import mortgage_fair as mf

FINAL_RULES = ("If the Freddie Mac Primary Mortgage Market Survey (PMMS) 30-Year "
               "Fixed-Rate Mortgage reported in Freddie Mac’s final PMMS "
               "release published during 2026 is above 7.00%, then the market "
               "resolves to Yes.")
MAX_RULES = ("If the 30-Yr FRM is above 7.25% in any Primary Mortgage Market "
             "Survey (PMMS) release published in 2027, then the market resolves "
             "to Yes.")
# Monday 2026-09-28 21:00 ET: last print 7.03 (9/24), the OCT01 ladder open
NOW = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc).timestamp()
PRINT = (7.03, date(2026, 9, 24))


def rung(k, bid, ask, ev="KX30YMORTW-26OCT01", close="2026-10-01T15:59:00Z"):
    return {"ticker": f"{ev}-T{k:.2f}", "event_ticker": ev, "floor_strike": k,
            "yes_bid_dollars": None if bid is None else f"{bid / 100:.4f}",
            "yes_ask_dollars": None if ask is None else f"{ask / 100:.4f}",
            "close_time": close}


# the OCT01 ladder as it stood on the evening of 9/28
LADDER_0928 = [rung(7.17, 94, 97), rung(7.18, 89, 97), rung(7.19, 81, 94),
               rung(7.20, 69, 84), rung(7.21, 54, 70), rung(7.22, 42, 52),
               rung(7.23, 38, 39)]


def market(ticker, rules, floor, close, strike_type="greater"):
    return {"ticker": ticker, "rules_primary": rules, "floor_strike": floor,
            "close_time": close, "strike_type": strike_type}


class TestRulesAndCalendar(unittest.TestCase):
    def test_the_two_modelled_shapes(self):
        self.assertEqual(mf.parse_rules(FINAL_RULES),
                         {"kind": "final", "year": 2026, "k": 7.0})
        self.assertEqual(mf.parse_rules(MAX_RULES),
                         {"kind": "max", "year": 2027, "k": 7.25})

    def test_in_year_touch_shapes_are_not_modelled(self):
        for other in (
                "If any Freddie Mac Primary Mortgage Market Survey (PMMS) "
                "release, between Issuance and Dec 31, 2026, inclusive, reports "
                "that the 30-year fixed-rate mortgage average is below 5.75%, "
                "then the market resolves to Yes.",
                "If the 30-Yr FRM is above 6.6% in 2026, then the market "
                "resolves to Yes.", "", None):
            self.assertIsNone(mf.parse_rules(other), other)

    def test_thursdays_and_the_release_instant(self):
        self.assertEqual(mf.first_thursday(2027), date(2027, 1, 7))
        self.assertEqual(mf.first_thursday(2026), date(2026, 1, 1))
        self.assertEqual(mf.last_thursday(2026), date(2026, 12, 31))
        self.assertEqual(mf.last_thursday(2027), date(2027, 12, 30))
        self.assertEqual(mf.release_utc(date(2026, 10, 1)),
                         datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(mf.release_utc(date(2026, 12, 3)),          # EST
                         datetime(2026, 12, 3, 17, 0, tzinfo=timezone.utc))


class TestModel(unittest.TestCase):
    def test_sd_scaling_matches_the_2000_2026_fit(self):
        # measured n-week sd of the weekly print since 2000 (bp)
        for n, bp in ((4, 22), (13, 41), (26, 58), (52, 85), (65, 98)):
            self.assertAlmostEqual(mf.sd_weeks(n) * 100, bp, delta=3.0, msg=n)
        self.assertEqual(mf.sd_weeks(0), 0.0)

    def test_final(self):
        ps = [mf.p_final(7.218, 13, k) for k in (6.5, 7.0, 7.25, 7.5, 8.0)]
        self.assertTrue(all(a > b for a, b in zip(ps, ps[1:])), ps)
        # "above K" on a two-decimal print: X0 == K + 0.005 is the median
        self.assertAlmostEqual(mf.p_final(7.0, 13, 6.995), 0.5, places=6)
        # sign-symmetrized 13-week block bootstrap of 2000-2026 (2026-09-28)
        self.assertAlmostEqual(mf.p_final(7.218, 13, 7.0), 0.720, delta=0.03)
        self.assertAlmostEqual(mf.p_final(7.218, 13, 7.25), 0.460, delta=0.03)
        self.assertAlmostEqual(mf.p_final(7.218, 13, 7.5), 0.215, delta=0.035)

    def test_max_is_calibrated_to_the_bootstrap(self):
        ref = {7.0: 0.903, 7.25: 0.787, 7.5: 0.625, 7.75: 0.450, 8.0: 0.307,
               8.5: 0.121, 9.0: 0.043}
        for k, r in ref.items():
            self.assertAlmostEqual(mf.p_max(7.218, 14, 51, k), r, delta=0.03, msg=k)
        for k in (7.0, 7.5, 8.0):     # the year's max is at least its first print
            self.assertGreaterEqual(mf.p_max(7.218, 14, 51, k),
                                    mf.p_final(7.218, 14, k))
        self.assertAlmostEqual(mf.p_max(7.218, 14, 0, 7.5),
                               mf.p_final(7.218, 14, 7.5), places=9)


class TestAnchor(unittest.TestCase):
    def test_median_of_the_0928_ladder(self):
        x, why = mf.anchor_from_ladder(LADDER_0928)
        self.assertEqual(why, "")
        self.assertAlmostEqual(x, 7.218, places=3)    # 7.21 62c .. 7.22 47c

    def test_noisy_mids_are_made_monotone(self):
        # mids 71, 49, 53, 31 -> 71, 51, 51, 31: the crossing is 7.02 -> 7.03
        x, _ = mf.anchor_from_ladder([rung(7.00, 70, 72), rung(7.01, 48, 50),
                                      rung(7.02, 52, 54), rung(7.03, 30, 32)])
        self.assertAlmostEqual(x, 7.0205, places=4)

    def test_what_does_not_count(self):
        # a wide strike, a one-sided strike, a 100c ask: nothing usable
        x, why = mf.anchor_from_ladder([rung(7.10, 60, 95), rung(7.11, None, 40),
                                        rung(7.12, 45, 100)])
        self.assertIsNone(x)
        self.assertIn("two-sided", why)
        # all above 50c and the top strike past the 95c edge: only "higher"
        x, why = mf.anchor_from_ladder([rung(7.10, 97, 99), rung(7.11, 96, 98)])
        self.assertIsNone(x)
        self.assertIn("does not bracket", why)
        self.assertIn("extrapolation edge", why)
        # the crossing sits between strikes 10bp apart
        x, why = mf.anchor_from_ladder([rung(7.00, 70, 72), rung(7.10, 30, 32)])
        self.assertIsNone(x)
        self.assertIn("too wide", why)

    def test_read_past_the_end_of_the_ladder(self):
        # 9/29 evening: the OCT01 ladder topped out at T7.23 bid 85 / ask 88
        top = [rung(7.20, 96, 97), rung(7.21, 94, 95), rung(7.22, 87, 90), rung(7.23, 85, 88)]
        d = mf.ladder_anchor(top)
        self.assertEqual(d["why"], "")
        self.assertEqual(d["extrap"], {"k": 7.23, "p": 86.5, "side": "top"})
        self.assertAlmostEqual(d["x"], 7.23 + 0.03 * 1.1031, places=3)    # ~7.263
        self.assertAlmostEqual(mf.anchor_from_ladder(top)[0], d["x"])
        # the bottom end reads the other way: 32c at 7.10 -> below it
        d = mf.ladder_anchor([rung(7.10, 30, 34), rung(7.11, 20, 24)])
        self.assertEqual(d["extrap"]["side"], "bottom")
        self.assertAlmostEqual(d["x"], 7.10 - 0.03 * 0.4677, places=3)    # ~7.086
        # the edge is inclusive at 95c / 5c
        self.assertIsNotNone(mf.ladder_anchor([rung(7.10, 96, 98), rung(7.11, 94, 96)])["x"])
        self.assertIsNotNone(mf.ladder_anchor([rung(7.10, 4, 6), rung(7.11, 2, 4)])["x"])
        self.assertIsNone(mf.ladder_anchor([rung(7.10, 3, 5), rung(7.11, 2, 4)])["x"])
        # a crossing still wins over extrapolation, and carries no extrap
        self.assertIsNone(mf.ladder_anchor(LADDER_0928)["extrap"])

    def test_latest_print_and_open_event(self):
        settled = [{"close_time": "2026-09-17T15:59:00Z", "expiration_value": "6.95"},
                   {"close_time": "2026-09-24T15:59:00Z", "expiration_value": "7.03"},
                   {"close_time": "2026-09-24T15:59:00Z", "expiration_value": ""}]
        self.assertEqual(mf.latest_print(settled), PRINT)
        self.assertIsNone(mf.latest_print([]))
        now = datetime.fromtimestamp(NOW, timezone.utc)
        nxt = [rung(7.30, 50, 52, ev="KX30YMORTW-26OCT08",
                    close="2026-10-08T15:59:00Z")]
        ev, mk, day = mf.open_anchor_event(nxt + LADDER_0928, now)
        self.assertEqual((ev, len(mk), day),
                         ("KX30YMORTW-26OCT01", 7, date(2026, 10, 1)))
        self.assertEqual(mf.open_anchor_event([], now), (None, [], None))


class TestX0(unittest.TestCase):
    def anchor(self, x=7.218, age=0.0, now=NOW):
        return {"x": x, "event": "KX30YMORTW-26OCT01", "day": "2026-10-01",
                "ts": now - age}

    def test_the_anchor_wins(self):
        x0, d, src = mf.choose_x0(NOW, self.anchor(), PRINT)
        self.assertEqual((x0, d), (7.218, date(2026, 10, 1)))
        self.assertTrue(src.startswith("anchor"), src)

    def test_an_extrapolated_anchor_says_so(self):
        a = dict(self.anchor(x=7.263), extrap={"k": 7.23, "p": 86.5, "side": "top"})
        x0, d, src = mf.choose_x0(NOW, a, PRINT)
        self.assertEqual((x0, d), (7.263, date(2026, 10, 1)))
        self.assertIn("past the top strike 7.23 at 86.5c", src)

    def test_a_stale_anchor_and_an_old_print_fail_closed(self):
        x0, d, src = mf.choose_x0(NOW, self.anchor(age=mf.ANCHOR_TTL_SECS + 5),
                                  PRINT)
        self.assertIsNone(x0)
        self.assertIn("no fresh anchor", src)
        self.assertEqual(mf.choose_x0(NOW, None, None)[2], "no settled weekly print")

    def test_the_print_alone_within_a_day_of_its_release(self):
        now = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc).timestamp()
        x0, d, src = mf.choose_x0(now, None, PRINT)
        self.assertEqual((x0, d), (7.03, date(2026, 9, 24)))
        self.assertTrue(src.startswith("print"), src)
        late = datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(mf.choose_x0(late, None, PRINT)[0])

    def test_an_anchor_far_from_the_print_is_refused(self):
        x0, _, src = mf.choose_x0(NOW, self.anchor(x=7.40), PRINT)   # 37bp
        self.assertIsNone(x0)
        self.assertIn("bp from the print", src)

    def test_a_print_over_eight_days_old_fails_closed(self):
        now = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc).timestamp()
        x0, _, src = mf.choose_x0(now, self.anchor(now=now), PRINT)
        self.assertIsNone(x0)
        self.assertIn("days old", src)


class TestEntries(unittest.TestCase):
    D = date(2026, 10, 1)

    def test_final_and_max(self):
        e = mf.market_entry(market("KXFM30YMTG-26EOY-T7.00", FINAL_RULES, 7,
                                   "2026-12-31T16:55:00Z"), 7.218, self.D)
        self.assertEqual((e["kind"], e["year"], e["n"]), ("final", 2026, 13.0))
        self.assertAlmostEqual(e["p"], mf.p_final(7.218, 13, 7.0))
        e = mf.market_entry(market("KXMORTGAGERATE-27DEC30-T7.25", MAX_RULES, 7.25,
                                   "2027-12-30T16:59:00Z"), 7.218, self.D)
        self.assertEqual((e["kind"], e["n1"], e["m"]), ("max", 14.0, 51.0))
        self.assertAlmostEqual(e["p"], mf.p_max(7.218, 14, 51, 7.25))

    def test_every_doubt_is_an_error(self):
        f = lambda **kw: mf.market_entry(market(**dict(
            dict(ticker="KXFM30YMTG-26EOY-T7.00", rules=FINAL_RULES, floor=7,
                 close="2026-12-31T16:55:00Z"), **kw)), 7.218, self.D).get("err")
        self.assertIn("rules", f(rules="If the 30-Yr FRM is above 6.6% in 2026"))
        self.assertIn("strike mismatch", f(floor=7.25))
        self.assertIn("strike mismatch", f(ticker="KXFM30YMTG-26EOY-T7.25"))
        self.assertIn("strike_type", f(strike_type="less"))
        self.assertIn("not in 2026", f(close="2027-01-07T16:55:00Z"))
        self.assertIn("under a week", mf.market_entry(market(
            "KXFM30YMTG-26EOY-T7.00", FINAL_RULES, 7, "2026-12-31T16:55:00Z"),
            7.218, date(2026, 12, 31))["err"])
        self.assertIn("counting year", mf.market_entry(market(
            "KXMORTGAGERATE-27DEC30-T7.25", MAX_RULES, 7.25,
            "2027-12-30T16:59:00Z"), 7.3, date(2027, 1, 7))["err"])
        self.assertIn("no rate level", mf.market_entry(market(
            "KXFM30YMTG-26EOY-T7.00", FINAL_RULES, 7, "2026-12-31T16:55:00Z"),
            None, None)["err"])


class TestWatch(unittest.TestCase):
    def fake(self, calls, ladder=LADDER_0928):
        def gj(path, params):
            calls.append((path, dict(params)))
            s, st = params["series_ticker"], params["status"]
            if s == "KX30YMORTW" and st == "settled":
                return {"markets": [{"close_time": "2026-09-24T15:59:00Z",
                                     "expiration_value": "7.03"}]}
            if s == "KX30YMORTW":
                return {"markets": ladder}
            if s == "KXFM30YMTG":
                return {"markets": [market("KXFM30YMTG-26EOY-T7.00", FINAL_RULES,
                                           7, "2026-12-31T16:55:00Z")]}
            return {"markets": [market("KXMORTGAGERATE-27DEC30-T7.25", MAX_RULES,
                                       7.25, "2027-12-30T16:59:00Z")]}
        return gj

    def test_refresh_end_to_end_without_the_network(self):
        calls = []
        w = mf.MortgageWatch(self.fake(calls))
        snap = w.refresh(now_ts=NOW)
        self.assertAlmostEqual(snap["x0"], 7.218, places=3)
        self.assertEqual(snap["x0_day"], "2026-10-01")
        self.assertTrue(snap["x0_src"].startswith("anchor"), snap["x0_src"])
        self.assertEqual(snap["print"], {"value": 7.03, "day": "2026-09-24"})
        self.assertEqual(snap["errors"], [])
        self.assertEqual(sorted(snap["markets"]), ["KXFM30YMTG-26EOY-T7.00",
                                                   "KXMORTGAGERATE-27DEC30-T7.25"])
        self.assertTrue(all("p" in e for e in snap["markets"].values()))
        self.assertIn("sd_a_bp", snap["model"])
        # inside META_REFRESH_SECS only the ladder is read again
        n = len(calls)
        w.refresh(now_ts=NOW + 60)
        self.assertEqual(len(calls), n + 1)
        self.assertEqual(calls[-1][1]["status"], "open")
        # the status file round-trips
        path = os.path.join(tempfile.mkdtemp(prefix="mort_"), "mortgage_fair.json")
        mf.write_status(path, snap)
        self.assertTrue(os.path.getsize(path) > 100)

    def test_a_ladder_that_cannot_anchor_keeps_the_last_good_one(self):
        calls = []
        w = mf.MortgageWatch(self.fake(calls))
        w.refresh(now_ts=NOW)
        w.get_json = self.fake(calls, ladder=[rung(7.30, 97, 99),
                                              rung(7.31, 96, 98)])
        snap = w.refresh(now_ts=NOW + 120)
        self.assertAlmostEqual(snap["x0"], 7.218, places=3)       # within TTL
        self.assertTrue(any("does not bracket" in e for e in snap["errors"]))
        snap = w.refresh(now_ts=NOW + mf.ANCHOR_TTL_SECS + 200)
        self.assertIsNone(snap["x0"])                             # then closed
        self.assertTrue(all("err" in e for e in snap["markets"].values()))

    def test_failed_reads_fall_back_then_fail_closed(self):
        def boom(path, params):
            raise RuntimeError("signed down")
        w = mf.MortgageWatch(boom)
        with mock.patch.object(mf.requests, "get",
                               side_effect=RuntimeError("public down")), \
                mock.patch.object(mf.time, "sleep"):
            snap = w.refresh(now_ts=NOW)
        self.assertIsNone(snap["x0"])
        self.assertEqual(snap["markets"], {})
        self.assertEqual(len(snap["errors"]), 2)                  # meta + anchor


def par_csv(rows):
    return "Date,1 Mo,2 Yr,10 Yr\n" + "\n".join(
        f"{d.strftime('%m/%d/%Y')},4.1,4.8,{v}" for d, v in rows)


class TestFold10Y(unittest.TestCase):
    """Jack 2026-10-02: "yes fold the live 10Y into the mortgage anchor"."""

    def anchor(self, x=7.218, age=0.0, now=NOW):
        return {"x": x, "event": "KX30YMORTW-26OCT01", "day": "2026-10-01",
                "ts": now - age}

    def test_par_window_average(self):
        # the 9/24 print's window: Thu 9/17 .. Wed 9/23 (five business days)
        rows = [(date(2026, 9, 16), 9.0), (date(2026, 9, 17), 5.20),
                (date(2026, 9, 18), 5.22), (date(2026, 9, 21), 5.24),
                (date(2026, 9, 22), 5.26), (date(2026, 9, 23), 5.28),
                (date(2026, 9, 24), 9.0)]
        self.assertAlmostEqual(mf.par10_window_avg(date(2026, 9, 24), [par_csv(rows)]), 5.24)
        self.assertIsNone(mf.par10_window_avg(date(2026, 9, 24), [par_csv(rows[:3])]))

    def test_a_fresh_anchor_moves_with_the_10y_since_it_repriced(self):
        x0, d, src, a = mf.fold_x0(NOW, self.anchor(), PRINT, 5.30, 5.26, None)
        self.assertAlmostEqual(x0, 7.218 + 0.9 * 0.04, places=6)    # +3.6bp
        self.assertEqual((d, a), (date(2026, 10, 1), mf.ANCHOR_SD_BP))
        self.assertIn("+10Y fold +3.6bp", src)
        # no reference for this anchor yet: unchanged
        self.assertEqual(mf.fold_x0(NOW, self.anchor(), PRINT, 5.30, None, None)[0], 7.218)

    def test_no_anchor_the_print_carried_by_the_10y(self):
        # no fresh anchor and the print 4 days old: was "no rate level"
        self.assertIsNone(mf.choose_x0(NOW, None, PRINT)[0])
        x0, d, src, a = mf.fold_x0(NOW, None, PRINT, 5.24, None, 5.14)
        self.assertAlmostEqual(x0, 7.03 + 0.9 * 0.10, places=6)     # +9bp
        self.assertEqual(d, mf.et_date(datetime.fromtimestamp(NOW, timezone.utc)))
        self.assertEqual(a, mf.FOLD_SD_BP)
        self.assertTrue(src.startswith("print 2026-09-24 +10Y fold +9.0bp"), src)
        # the fold's wider anchor sd reaches the probabilities
        self.assertNotAlmostEqual(mf.p_final(7.1, 13, 7.0, a_bp=5.0),
                                  mf.p_final(7.1, 13, 7.0), places=6)

    def test_what_does_not_fold(self):
        # past the cap: no rate level rather than a guess
        x0, _, src, _a = mf.fold_x0(NOW, None, PRINT, 5.70, None, 5.24)
        self.assertIsNone(x0)
        self.assertIn("past 30bp", src)
        self.assertIsNone(mf.fold_x0(NOW, self.anchor(), PRINT, 5.70, 5.26, None)[0])
        # no live 10Y / no window average / the switch off: choose_x0 as was
        self.assertEqual(mf.fold_x0(NOW, None, PRINT, None, None, 5.24)[:3],
                         mf.choose_x0(NOW, None, PRINT))
        self.assertIsNone(mf.fold_x0(NOW, None, PRINT, 5.20, None, None)[0])
        with mock.patch.object(mf, "FOLD_ENABLE", False):
            self.assertIsNone(mf.fold_x0(NOW, None, PRINT, 5.20, None, 5.24)[0])
        # a print over eight days old stays refused
        late = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(mf.fold_x0(late, None, PRINT, 5.20, None, 5.24)[0])

    def test_refresh_tracks_the_anchor_reference(self):
        calls = []
        tw = TestWatch()
        w = mf.MortgageWatch(tw.fake(calls))
        with mock.patch.object(mf, "fetch_par10_window_avg", return_value=5.24) as fp:
            snap = w.refresh(now_ts=NOW, y10=(5.26, NOW - 5))
            self.assertAlmostEqual(snap["x0"], 7.218, places=3)    # reference set here
            self.assertEqual(snap["fold"], {"y10": 5.26, "y10_at_anchor": 5.26,
                                            "y10_ref": 5.24})
            # same ladder, the 10Y up 5bp: the anchor follows by 4.5bp
            snap = w.refresh(now_ts=NOW + 120, y10=(5.31, NOW + 115))
            self.assertAlmostEqual(snap["x0"], 7.218 + 0.045, places=3)
            self.assertIn("+10Y fold +4.5bp", snap["x0_src"])
            # a stale 10Y read: no fold
            snap = w.refresh(now_ts=NOW + 240, y10=(5.40, NOW + 240 - mf.Y10_MAX_AGE_SECS - 5))
            self.assertAlmostEqual(snap["x0"], 7.218, places=3)
            # the ladder reprices 3bp higher: the reference resets to the
            # 10Y then, so the fold starts again from zero
            moved = [rung(k + 0.03, b, a) for k, b, a in (
                (7.17, 94, 97), (7.18, 89, 97), (7.19, 81, 94), (7.20, 69, 84),
                (7.21, 54, 70), (7.22, 42, 52), (7.23, 38, 39))]
            w.get_json = tw.fake(calls, ladder=moved)
            snap = w.refresh(now_ts=NOW + 360, y10=(5.31, NOW + 355))
            self.assertAlmostEqual(w.anchor_y10[0], 7.248, places=3)
            self.assertEqual(w.anchor_y10[1], 5.31)
            self.assertAlmostEqual(snap["x0"], 7.248, places=3)
            self.assertNotIn("+10Y fold", snap["x0_src"].replace("+10Y fold +0.0bp", ""))
            self.assertEqual(fp.call_count, 1)                      # once per print
        self.assertIn("fold_beta", snap["model"])
        self.assertEqual(snap["x0_sd_bp"], mf.ANCHOR_SD_BP)


if __name__ == "__main__":
    unittest.main()
