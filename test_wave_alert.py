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

    def test_a_new_shape_needs_you_and_texts(self):
        # Jack 2026-11-14: "for upcoming new shapes, notify me and it's my call so dont default to
        # turning it on e.g. KXYTVIEWSHIGH"
        yt = dict(series("KXYTVIEWSHIGH", "NEW", 16, 1000.0, ["new shape: Entertainment -- no wave has run one"]),
                  category="Entertainment")
        d = dict(DETECT, series=[yt, DETECT["series"][2]], new_fit_series=[], new_near_series=[],
                 new_check_series=["KXYTVIEWSHIGH"], new_shape_series=["KXYTVIEWSHIGH"], new_shape_over_bar=True)
        text, push, subject, body = wa.wave_msg(d, "needs-you", "", "", None, None, fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "WAVE? Your call, new shape: 16 events held for you (16 mkts, $1,000/day pools), "
                                   "0 greenlit, 1 denied")
        self.assertEqual(lines[1], "Held for you because: 16 new shape (Entertainment): no wave has run one")
        self.assertIn(wa.NEW_SHAPE_NOTE, body)
        self.assertTrue(wa.wave_do_text("needs-you", d))
        self.assertFalse(wa.wave_do_text("needs-you", dict(d, new_shape_over_bar=False)))   # other needs-you: email
        self.assertTrue(wa.wave_do_text("live", {}))

    def test_a_new_shape_beside_a_live_wave_is_held_not_denied(self):
        yt = dict(series("KXSHOW", "NEW", 6, 500.0, ["new shape: Entertainment -- no wave has run one"]),
                  category="Entertainment")
        d = dict(DETECT, series=DETECT["series"] + [yt], new_check_series=["KXNFLX", "KXSHOW"])
        text, push, subject, body = wa.wave_msg(d, "live", "w1108-x", "", 12, 40, fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "WAVE w1108-x LIVE: 12 events greenlit, 7 denied (12 mkts listed, ~$40/day est)")
        self.assertIn("Held for you because: 6 new shape (Entertainment): no wave has run one", lines)
        self.assertIn("HELD FOR YOUR CALL: 6 events in 1 series", body)




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
        """Jack 2026-11-20: "lower pick-off watch alert size to $25" (was $100): a $25+
        new pick-off texts; under $25 (a manual --min-usd run) only emails."""
        row = dict(self.ROW, adverse_usd=20.0, contracts=100.0)
        r = {"hours": 6, "flagged": [row], "new": [row], "acted": [], "series_trips": [], "flagged_usd": 20.0}
        self.assertFalse(wa.pickoff_msg(r)[4])
        r["series_trips"] = [{"wave": "w", "series": "KXHOUSERACE", "markets": ["a", "b", "c"]}]
        self.assertTrue(wa.pickoff_msg(r)[4])                       # a series trip always texts

    def test_twenty_five_dollars_texts(self):
        for usd, texts in ((24.99, False), (25.0, True), (40.0, True)):
            row = dict(self.ROW, adverse_usd=usd, contracts=100.0)
            r = {"hours": 6, "flagged": [row], "new": [row], "acted": [], "series_trips": [], "flagged_usd": usd}
            self.assertEqual(wa.pickoff_msg(r)[4], texts, usd)

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

    RT = {"ticker": "KXDPRESPRIMARY-28AZ-MKEL", "wave": "w1111-elec2028", "registry": True, "net_usd": -61.2,
          "legs": 2, "contracts": 950.0, "first": "2026-11-19T04:01:00Z", "last": "2026-11-19T05:32:17Z",
          "pos_now": 0.0, "worst": [{"dir": "long", "contracts": 750.0, "open_c": 12.0, "close_c": 5.2, "usd": -51.0},
                                    {"dir": "short", "contracts": 200.0, "open_c": 5.9, "close_c": 11.0, "usd": -10.2}]}

    def test_round_trips_alone(self):
        """Jack 2026-11-20 "Yes, add it": a market's round trips netting $25+ lost in 24 h
        alert on their own, with the closing leg's prices; old pick-offs aren't re-listed."""
        rt = {"hours": 24, "min_usd": 25, "flagged": [self.RT], "new": [self.RT], "flagged_usd": 61.2}
        r = {"at": "2026-11-19T05:45:00Z", "hours": 6, "flagged": [self.ROW], "new": [], "acted": [],
             "series_trips": [], "flagged_usd": 439.19, "roundtrips": rt}
        text, push, subject, body, do_text = wa.pickoff_msg(r)
        lines = text.splitlines()
        self.assertEqual(lines[0], "ROUND TRIPS: 1 wave mkt -$61 new (1 flagged, -$61 in 24h)")
        self.assertIn("opened AND closed again inside 24h", lines[1])
        self.assertIn("your word", lines[1])
        self.assertIn("r1. KXDPRESPRIMARY-28AZ-MKEL net -$61 in 2 closed legs filled 11:01 PM-12:32 AM ET", text)
        self.assertIn("worst bought 750 @12, sold @5.2 (-$51); sold 200 @5.9, bought back @11 (-$10)", text)
        self.assertNotIn("x1.", text)                                  # the old pick-off isn't news
        self.assertIn("turn off <TICKER>", body)
        self.assertTrue(do_text)
        self.assertLessEqual(len(text), wa.TEXT_CHARS)

    def test_round_trips_with_a_pickoff(self):
        rt = {"hours": 24, "min_usd": 25, "flagged": [self.RT], "new": [self.RT], "flagged_usd": 61.2}
        r = {"at": "2026-11-08T05:10:00Z", "hours": 6, "flagged": [self.ROW], "new": [self.ROW], "acted": [],
             "series_trips": [], "flagged_usd": 439.19, "roundtrips": rt}
        text = wa.pickoff_msg(r)[0]
        self.assertTrue(text.startswith("PICK-OFF: 1 wave mkt -$439 new"))
        self.assertIn("\nROUND TRIPS: 1 wave mkt -$61 new (1 flagged, -$61 in 24h); why: positions opened", text)
        self.assertIn("x1. KXHOUSERACE-IN04-28-D", text)
        self.assertIn("r1. KXDPRESPRIMARY-28AZ-MKEL", text)

    def test_small_round_trips_email_only(self):
        row = dict(self.RT, net_usd=-20.0)
        rt = {"hours": 24, "min_usd": 10, "flagged": [row], "new": [row], "flagged_usd": 20.0}
        r = {"hours": 6, "flagged": [], "new": [], "acted": [], "series_trips": [], "flagged_usd": 0, "roundtrips": rt}
        self.assertFalse(wa.pickoff_msg(r)[4])
        r["roundtrips"] = dict(rt, new=[])                             # nothing new, nothing texted
        self.assertFalse(wa.pickoff_msg(r)[4])

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
        self.assertTrue(text.splitlines()[0].startswith("RELIGHT: 247 markets listed (~$73/day), 548 mkts denied"),
                        text)
        self.assertIn("a DARK alert follows if they rest nothing", text)

    def test_the_denials_say_why_in_words(self):
        # Jack 2026-11-18, "why denied" of "RELIGHT: 16 events (125 mkts) listed, 211 mkts denied
        # (~$84/day) ... Denied because: 211 under bar", then "reword the alert": the $/day is
        # the listed markets', and "under bar" says what bar, at what size, and why it matters
        fx = {"ts": "2026-11-18 19:18:39Z", "where": "waves task / wave w1113-fxmetals main", "taken": 125,
              "new": 125, "relit": 0, "extended": 0, "usd_day": 84.0, "bar": 1.10,
              "skipped": {"no book": 0, "no quotable side": 0, "under bar": 211}}
        r = {"listed": 125, "usd_day": 84.0, "programs_before": 6343, "programs_now": 6348, "programs_jump": 5,
             "passes": [fx]}
        lines = wa.relight_msg(r)[0].splitlines()
        self.assertEqual(lines[0], "RELIGHT: 125 markets listed (~$84/day), 211 mkts denied")
        self.assertEqual(lines[1], "Listed because: their share of the pool projects over the $1.10 bar by the "
                                   "period end (new 125, relit 0, extended 0)")
        den = [ln for ln in lines if ln.startswith("Denied because: ")]
        self.assertEqual(den, ["Denied because: 211 under the $1.10 bar: their share of the pool at the wave's size "
                               "projects under $1.10 by the period end (Kalshi pays nothing under $1 a market per "
                               "period)"])
        # with the size-up (Jack: "1.5-3x the size"), and a second wave without it in the window
        sized = dict(fx, ts="2026-11-18 20:02:29Z", taken=201, new=201, usd_day=76.0, sized=189, bank_max=300,
                     sized_cap=2.0, skipped={"no book": 2, "no quotable side": 0, "under bar": 1})
        other = {"ts": "2026-11-18 20:03:00Z", "where": "waves task / wave w1111-elec2028 main", "taken": 1,
                 "new": 0, "relit": 1, "extended": 0, "usd_day": 2.0, "skipped": {"under bar": 3}}
        lines = wa.relight_msg(dict(r, listed=202, usd_day=78.0, passes=[sized, other]))[0].splitlines()
        self.assertTrue(lines[1].endswith("; 189 of them only sized up, to at most 2x the wave's bank size"), lines[1])
        den = [ln for ln in lines if ln.startswith("Denied because: ")][0]
        self.assertEqual(den, "Denied because: 3 under the $1.10 bar: their share of the pool at the wave's size "
                              "projects under $1.10 by the period end (Kalshi pays nothing under $1 a market per "
                              "period); 1 under the $1.10 bar: their share of the pool even sized up to 2x the "
                              "wave's bank size (300 a side, cheap tiers on top) projects under $1.10 by the period "
                              "end (Kalshi pays nothing under $1 a market per period); 2 with no book to judge "
                              "(an empty side, or no book read)")

    def test_a_big_relight_names_its_wave_and_texts(self):
        # Jack 2026-11-18: "i didnt get an email and text confirming the relight" -- the
        # 05:00Z w1111-elec2028 relight (976 mkts) read as "4 markets listed", no text
        r = {"since": "", "until": "", "listed": 977, "usd_day": 602.0, "programs_before": 5905,
             "programs_now": 5869, "programs_jump": 0, "passes": [
                 {"ts": "2026-11-18 05:07:10Z", "where": "waves task / wave w1111-elec2028 main", "taken": 975,
                  "new": 0, "relit": 975, "extended": 0, "usd_day": 600.0, "skipped": {"under bar": 1}},
                 {"ts": "2026-11-18 05:07:43Z", "where": "waves task / wave w1111-pol main", "taken": 1,
                  "new": 0, "relit": 1, "extended": 0, "usd_day": 2.0},
                 {"ts": "2026-11-18 05:15:35Z", "where": "waves task / wave w1111-elec2028 main", "taken": 1,
                  "new": 0, "relit": 1, "extended": 0, "usd_day": 0.0}]}
        text, push, subject, body, big = wa.relight_msg(r)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("RELIGHT: 977 markets listed"), text)
        self.assertEqual(lines[1], "Listed because: their share of the pool projects over the $1.10 bar by the "
                                   "period end (new 0, relit 977, extended 0)")
        self.assertEqual(lines[2], "By wave: w1111-elec2028 976, w1111-pol 1")
        self.assertTrue(big)
        # a pass whose summary line was lost (relight_watch's WROTE fallback) says so
        r2 = dict(r, passes=[dict(r["passes"][0], relit=0, unsplit=975)])
        self.assertIn("relit 0, extended 0, split unknown 975)", wa.relight_msg(r2)[0])
        self.assertIn("split unknown 975", wa.relight_msg(r2)[3])

    def test_relight_names_count_the_windows_first_minute(self):
        # entries are stamped with the pass's START minute ("scan 2026-11-18T05:00Z"), and a
        # check whose window opens in that minute must still name them
        import json
        import os
        import sys
        import tempfile
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="wa-relight-")
        lst = os.path.join(tmp, "pvd.json")
        with open(lst, "w", encoding="utf-8") as fh:
            json.dump({"markets": {
                "KXDPRESPRIMARY-28NH-PBUT": {"added": "scan 2026-11-18T05:00Z (relit; ROI 0.4%/day)"},
                "KXDPRESPRIMARY-28NH-JOSS": {"added": "scan 2026-11-18T05:00Z (relit; ROI 0.3%/day)"},
                "KXOLD-26NOV16-X": {"added": "scan 2026-11-18T04:45Z (relit; ROI 0.3%/day)"},
                "KXLATER-26NOV-X": {"added": "scan 2026-11-18T05:30Z (new; ROI 0.3%/day)"}}}, fh)
        fake = mock.MagicMock()
        fake.kalshi_get.return_value = {"markets": []}
        with mock.patch.object(wa, "LIST", lst), mock.patch.object(wa, "event_titles", lambda evs: {}), \
                mock.patch.dict(sys.modules, {"kalshi_reads": fake}):
            got = wa.relight_names({"since": "2026-11-18 05:00:24Z", "until": "2026-11-18 05:15:36Z"})
        self.assertEqual([(g["event"], g["markets"]) for g in got], [("KXDPRESPRIMARY-28NH", 2)])

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
