"""wave_alert.py: one layout for every market-wave alert (Jack 2026-11-08: "X #
of events greenlit or denied, reasoning, 10 names"). No sends, no reads."""
import unittest

import wave_alert as wa


def series(s, verdict, events, pool, why=(), n_names=None):
    n = events if n_names is None else n_names
    return {"series": s, "title": s.title(), "category": "Politics", "verdict": verdict, "why": list(why),
            "markets": events, "events": events, "pool_day": pool, "median_settle": "2027-06-01T00:00:00Z",
            "event_list": [{"event": f"{s}-E{i}", "name": f"Will {s} event {i} happen? (Before Jun 1, 2027)",
                            "pool_day": pool / max(n, 1) - i * 0.01, "markets": 1} for i in range(n)]}


DETECT = {"at": "2026-11-08T05:10:00Z", "since": "2026-11-08T04:30:00Z", "programs": 120,
          "series": [series("KXGOOD", "FIT", 8, 400.0), series("KXSOON", "NEAR", 4, 80.0),
                     series("KXONE", "FEW", 1, 186.0, ["1 event(s) lit: a wave needs 5+ in one series"]),
                     series("KXNFLX", "CHECK", 6, 300.0, ["category Sports"]),
                     series("KXRT", "MEMBER", 15, 2000.0, ["member series"])],
          "new_fit_series": ["KXGOOD"], "new_near_series": ["KXSOON"], "new_check_series": ["KXNFLX"]}


class WaveAlertTests(unittest.TestCase):
    def msg(self, outcome, **kw):
        return wa.wave_msg(DETECT, outcome, "w1108-x", kw.get("why_not", ""), kw.get("listed"),
                           kw.get("est_day"), fetch_titles=False)

    def test_live_counts_reasons_and_ten_names(self):
        text, push, subject, body = self.msg("live", listed=12, est_day=40)
        lines = text.splitlines()
        self.assertEqual(lines[0], "WAVE w1108-x LIVE: 12 events greenlit, 7 denied (12 mkts listed, ~$40/day est)")
        self.assertTrue(lines[1].startswith("Greenlit because: 8 have 5+ events in one series"))
        self.assertIn("4 settle in 1-14d", lines[1])
        self.assertEqual(lines[2], "Denied because: 6 Sports; 1 under 5 events in their series")
        names = lines[3:]
        self.assertEqual(len(names), 10)
        self.assertTrue(all(n.startswith("+") for n in names))   # 12 greenlit fill all 10
        self.assertTrue(names[0].startswith("+1. KXGOOD event 0 happen?"))   # "Will " dropped
        self.assertNotIn("KXRT", text)                           # member series: not denied
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text), wa.TEXT_CHARS)
        self.assertEqual(push, lines[0])
        self.assertTrue(subject.startswith("market-wave: WAVE w1108-x LIVE"))
        self.assertIn("ALREADY HANDLED (not counted): KXRT member", body)
        self.assertIn("Denied, top names:", body)

    def test_denied_names_fill_the_ten_when_few_greenlit(self):
        d = dict(DETECT, new_near_series=[], series=[series("KXGOOD", "FIT", 3, 400.0)] + DETECT["series"][2:])
        text, *_ = wa.wave_msg(d, "live", "w", "", None, None, fetch_titles=False)
        names = text.splitlines()[3:]
        self.assertEqual([n[0] for n in names], ["+"] * 3 + ["x"] * 7)

    def test_not_live_says_why_first(self):
        text, *_ = self.msg("not-live", why_not="shard-0 cash $3.2k < $5k")
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("WAVE w1108-x NOT LIVE: 12 events passed but not quoted, 7 denied"))
        self.assertEqual(lines[1], "Not live because: shard-0 cash $3.2k < $5k")
        self.assertTrue(lines[2].startswith("Passed because: "))

    def test_needs_you_holds_the_check_series(self):
        text, *_ = self.msg("needs-you")
        lines = text.splitlines()
        self.assertEqual(lines[0], "WAVE? Your call: 6 events held for you, 0 greenlit, 1 denied")
        self.assertEqual(lines[1], "Held for you because: 6 Sports")
        self.assertTrue(all(n.startswith("?") or n.startswith("x") for n in lines[3:]))

    def test_names_fold_to_ascii_and_drop_the_date_when_long(self):
        self.assertEqual(wa.text_name("Will New York City’s rent rise?"), "New York City's rent rise?")
        long = "Will Illinois regulate data center energy and water use? (Before May 1, 2027)"
        self.assertEqual(wa.text_name(long), "Illinois regulate data center energy and water use?")

    def test_series_plural(self):
        self.assertEqual(wa.plural(2, "series"), "2 series")
        self.assertEqual(wa.plural(2, "event"), "2 events")




class PickoffAlertTests(unittest.TestCase):
    """Jack 2026-11-08: "the pickoffs yesterday were unacceptable" -- a pick-off on a
    wave market alerts at once (pvd_waves.py pickoffs)."""
    ROW = {"ticker": "KXHOUSERACE-IN04-28-D", "side": "bid", "contracts": 1273.0, "vwap_c": 40.0, "mark_c": 5.5,
           "adverse_usd": 439.19, "wave": "2028 election wave (pass 4)", "registry": False, "pos_now": 1273,
           "last": "2026-11-08T01:05:00Z"}

    def test_new_pickoffs_text_with_the_numbers(self):
        r = {"at": "2026-11-08T05:10:00Z", "hours": 6, "flagged": [self.ROW], "new": [self.ROW], "acted": [],
             "series_trips": [], "flagged_usd": 439.19, "params": {"min_c": 5, "min_usd": 25}}
        text, push, subject, body, do_text = wa.pickoff_msg(r)
        lines = text.splitlines()
        self.assertEqual(lines[0], "PICK-OFF: 1 wave mkt -$439 new (1 flagged, -$439 in 6h)")
        self.assertIn("our orders out", lines[1])
        self.assertIn("legacy waves", text)
        self.assertIn("x1. KXHOUSERACE-IN04-28-D bought 1,273 @40 filled 8:05 PM ET (4h 05m ago), "
                      "mark 5.5 (-$439)", text)
        self.assertTrue(do_text)
        self.assertTrue(subject.startswith("market-wave pick-off: PICK-OFF"))

    def test_small_ones_email_only(self):
        row = dict(self.ROW, adverse_usd=40.0, contracts=100.0)
        r = {"hours": 6, "flagged": [row], "new": [row], "acted": [], "series_trips": [], "flagged_usd": 40.0}
        self.assertFalse(wa.pickoff_msg(r)[4])
        r["series_trips"] = [{"wave": "w", "series": "KXHOUSERACE", "markets": ["a", "b", "c"]}]
        self.assertTrue(wa.pickoff_msg(r)[4])                       # a series trip always texts

    def test_old_fill_says_when_it_filled(self):
        """Jack 2026-11-12 "are they delayed?": the 3:08 PM sweep fill alerted at 7:02 PM,
        when its mark first passed the bar. The line says when it filled."""
        row = dict(self.ROW, ticker="KXSENATENOM-TNR-28-GJAC", contracts=250.0, vwap_c=35.0, mark_c=23.5,
                   adverse_usd=28.75, wave="w1111-statenom", registry=True,
                   first="2026-11-12T20:08:53Z", last="2026-11-12T20:08:53Z")
        r = {"at": "2026-11-13T00:02:22Z", "hours": 6, "flagged": [row], "new": [row], "acted": [],
             "series_trips": [], "flagged_usd": 28.75}
        text, _, _, body, _ = wa.pickoff_msg(r)
        want = "KXSENATENOM-TNR-28-GJAC bought 250 @35 filled 3:08 PM ET (3h 53m ago), mark 23.5 (-$29)"
        self.assertIn("x1. " + want, text)
        self.assertIn(want, body)
        self.assertLessEqual(len(text), wa.TEXT_CHARS)

    def test_several_fills_give_the_span_and_minutes(self):
        row = dict(self.ROW, first="2026-11-12T19:08:00Z", last="2026-11-12T19:44:30Z")
        r = {"at": "2026-11-12T20:02:00Z", "hours": 6, "flagged": [row], "new": [row], "acted": [],
             "series_trips": [], "flagged_usd": 439.19}
        self.assertIn("@40 filled 2:08-2:44 PM ET (last 17 min ago), mark", wa.pickoff_msg(r)[0])

    def test_et_clock_summer_and_winter(self):
        from datetime import datetime, timezone
        self.assertEqual(wa.et_clock(datetime(2026, 7, 4, 16, 5, tzinfo=timezone.utc)), "12:05 PM")
        self.assertEqual(wa.et_clock(datetime(2026, 11, 12, 20, 8, tzinfo=timezone.utc)), "3:08 PM")
        self.assertEqual(wa.et_clock(datetime(2026, 11, 13, 0, 2, tzinfo=timezone.utc), ampm=False), "7:02")


class DarkAndRelightWordingTests(unittest.TestCase):
    """Jack 2026-11-11: the relight texts said "247 markets greenlit" for markets
    the old-wave lock kept dark. A relight now says "listed", and a DARK alert
    (pvd_waves.py darkcheck) says what rests nothing, why, and what was done."""

    def test_relight_says_listed_and_promises_the_check(self):
        r = {"listed": 247, "usd_day": 73.0, "programs_before": 5450, "programs_now": 5450, "programs_jump": 0,
             "passes": [{"ts": "05:47Z", "where": "2028", "taken": 247, "new": 9, "relit": 238, "extended": 0,
                         "usd_day": 73.0, "skipped": {"under bar": 548}}]}
        text = wa.relight_msg(r)[0]
        self.assertNotIn("greenlit", text)
        self.assertTrue(text.splitlines()[0].startswith("RELIGHT: 247 markets listed, 548 mkts denied"), text)
        self.assertIn("a DARK alert follows if they rest nothing", text)

    def test_a_program_jump_that_is_a_new_wave_says_so(self):
        # 2026-11-11 16:22Z: "RELIGHT: programs 5513 -> 6316 (+803), 0 listed" -- the
        # jump was w1111-statenom, listed (654 mkts) by the 24/7 wave check at 16:08Z
        r = {"listed": 0, "usd_day": 0, "programs_before": 5513, "programs_now": 6316, "programs_jump": 803,
             "passes": [], "new_waves": [{"name": "w1111-statenom", "added": "2026-11-11T16:08:15Z",
                                          "series": ["KXGOVNOM", "KXSENATENOM"], "markets": 654,
                                          "usd_day": 2096.0, "note": "auto: 803 new programs"}]}
        text, push, subject, body, big = wa.relight_msg(r)
        self.assertEqual(text.splitlines()[0], "RELIGHT: programs 5513 -> 6316 (+803) = new wave w1111-statenom, "
                                               "654 mkts listed (~$2,096/day)")
        self.assertNotIn("0 listed", text)
        self.assertIn("added 16:08Z by the 24/7 wave check, its own WAVE alert sent", text)
        self.assertIn("a DARK alert follows if they rest nothing", text)
        self.assertFalse(big)
        # a big jump the check added: its WAVE alert texted already -- no second text
        self.assertFalse(wa.relight_msg(dict(r, programs_jump=1500))[4])
        # a hand-added wave says so, and a big jump still texts
        hand = dict(r, programs_jump=1500, new_waves=[dict(r["new_waves"][0], note="hand (Jack)")])
        self.assertIn("by hand (/market-wave)", wa.relight_msg(hand)[0])
        self.assertTrue(wa.relight_msg(hand)[4])
        # no new wave: say who looks next instead of a bare "0 listed"
        bare = dict(r, new_waves=[])
        self.assertEqual(wa.relight_msg(bare)[0].splitlines()[0],
                         "RELIGHT: programs 5513 -> 6316 (+803), none listed by the relight tasks: the wave "
                         "check judges new series next (a WAVE alert follows if they fit)")

    def test_dark_alert(self):
        rows = [{"ticker": f"KXHOUSERACE-X{i}-28-D", "series": "KXHOUSERACE", "wave": "w1111-w28", "kind": "lock",
                 "reason": "old-wave lock: listed before the 11/08 UNWIND_ONLY_WAVES switch", "pos": 0.0,
                 "listed_at": "2026-11-11T05:20Z"} for i in range(12)]
        rows.append({"ticker": "KXPRESSTATE-GA-28-R", "series": "KXPRESSTATE", "wave": "w1111-elec2028",
                     "kind": "bot", "reason": "the bot's depth gate", "pos": 0.0, "listed_at": None})
        r = {"listed": 900, "checked": 880, "two_sided": 860, "one_sided": 7, "dark": 13, "dark_rows": rows,
             "one_rows": [], "new": rows, "alert": True,
             "acted": [{"action": "retuned", "wave": "w1111-w28", "series": ["KXHOUSERACE"], "markets": 12, "rc": 0}]}
        text, push, subject, body, do_text = wa.dark_msg(r)
        lines = text.splitlines()
        self.assertEqual(lines[0], "DARK: 13 listed mkts still rest nothing under the old-wave lock after "
                                   "the auto-fix (860 of 880 checked are two-sided)")
        self.assertTrue(wa.dark_msg(dict(r, wave_wide=True))[0].startswith(
            "DARK, WAVE-WIDE: 13 listed mkts rest nothing on Kalshi at two checks in a row"))
        self.assertTrue(lines[1].startswith("Why: 12 old-wave lock"), lines[1])
        self.assertEqual(lines[2], "Did: retuned w1111-w28 (KXHOUSERACE, 12 mkts)")
        self.assertIn("1 dark for the bot's own reasons", text)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("x")), 10)
        self.assertIn("(+3 more)", text)
        self.assertTrue(do_text)
        self.assertTrue(subject.startswith("market-wave dark: DARK: 13 listed mkts still"))
        self.assertIn("NEW KXPRESSTATE-GA-28-R", body)
        # without --act the lock case says it needs you
        r2 = dict(r, acted=[])
        self.assertIn("Did: nothing (darkcheck ran without --act): needs you", wa.dark_msg(r2)[0])
        self.assertFalse(wa.dark_msg(dict(r, alert=False))[4])


if __name__ == "__main__":
    unittest.main()
