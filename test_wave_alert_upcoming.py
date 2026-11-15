"""wave_alert.py upcoming: the advance notice of a wave the 24/7 check would list at its start
(Jack 2026-11-14: "market-wave skill should also scan upcoming rewards markets and callout if a
wave would be greenlit so i have advanced notice"), in the wave alerts' layout (Jack 2026-11-08:
"X # of events greenlit or denied, reasoning, 10 names"). No sends, no reads."""
import unittest
from datetime import datetime, timezone

import wave_alert as wa
from test_wave_alert import series

LADDER = ("live-price ladder ('after economic events'): only with a fair, while its market is shut "
          "(market-wave section 12)")


def window(start, notice, rows, fit=(), near=(), check=(), preflight=None, programs=100):
    return {"start": start, "last_start": start, "programs": programs, "series": rows, "notice": notice,
            "new_fit_series": list(fit), "new_near_series": list(near), "new_check_series": list(check),
            "preflight": preflight}


GREEN = window("2026-11-15T05:00:00Z", "greenlit",
               [series("KXGOOD", "FIT", 6, 600.0), series("KXSPORT", "CHECK", 6, 300.0, ["category Sports"]),
                series("KXONE", "FEW", 1, 80.0, ["1 event(s) lit: a wave needs 5+ in one series"]),
                dict(series("KXOLD", "FIT", 5, 200.0), alerted_before="2026-11-08T23:08:00Z"),
                series("KXRT", "MEMBER", 15, 2000.0, ["member series"])],
               fit=["KXGOOD"], check=["KXSPORT"],
               preflight={"at": "2026-11-14T18:00:00Z", "exit": 1, "pass": 14, "warn": [],
                          "fail": ["shard-0 cash: $3,200 free (a wave needs $5,000+; only Jack moves money)"]})
HELD = window("2026-11-15T16:00:00Z", "needs-you",
              [series("KXGOLDEVENT", "CHECK", 6, 900.0, [LADDER])], check=["KXGOLDEVENT"])
UP = {"at": "2026-11-14T18:00:00Z", "mode": "upcoming", "horizon_hours": 72, "window_min": 30, "markets": 900,
      "windows": [HELD, window("2026-11-15T01:00:00Z", None, []), GREEN]}


class UpcomingAlertTests(unittest.TestCase):
    def test_a_greenlit_wave_leads_and_texts(self):
        text, push, subject, body, do_text = wa.upcoming_msg(UP, fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "UPCOMING WAVE Sun 12:00 AM ET (in 11 h): 6 events would be greenlit, 7 denied "
                                   "(6 mkts, $600/day pools)")
        self.assertEqual(lines[1], "Greenlit because: 6 have 5+ events in one series, settle 14d+ out, a shape "
                                   "you've run (Politics)")
        self.assertEqual(lines[2], "Denied because: 6 Sports; 1 under 5 events in their series")
        self.assertTrue(lines[3].startswith("Preflight now: FAIL shard-0 cash: $3,200 free"))
        self.assertTrue(lines[3].endswith("-- fix it before the start, or nothing lists."))
        self.assertEqual(lines[4], "Also: Sun 11:00 AM ET (in 22 h): 6 events held for you")
        names = lines[5:]
        self.assertTrue(5 <= len(names) <= 10, names)        # whole names, as many as fit in 900
        self.assertEqual([n[0] for n in names], (["+"] * 6 + ["x"] * 4)[:len(names)])
        self.assertTrue(all(n.endswith("(Before Jun 1, 2027)") for n in names))   # none cut mid-line
        self.assertTrue(do_text)
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text), wa.TEXT_CHARS)
        self.assertEqual(push, lines[0])
        self.assertTrue(subject.startswith("market-wave upcoming: UPCOMING WAVE Sun 12:00 AM ET"))
        self.assertIn("ALERTED BEFORE (the start won't re-trigger them): KXOLD FIT (2026-11-08)", body)
        self.assertIn("ALREADY HANDLED (not counted): KXRT member", body)
        self.assertIn("PREFLIGHT NOW (pvd_waves.py preflight --auto, 2026-11-14T18:00:00Z): FAIL, 14 pass, 1 fail",
                      body)
        self.assertIn("Held for you because: 6 live-price ladders (type B: a fair first)", body)
        self.assertIn("Nothing was listed.", body)
        self.assertNotIn("KXOLD", text)

    def test_a_window_that_only_needs_you_emails(self):
        up = dict(UP, windows=[HELD])
        text, push, subject, body, do_text = wa.upcoming_msg(up, fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "UPCOMING, YOUR CALL Sun 11:00 AM ET (in 22 h): 6 events would be held for you, "
                                   "0 greenlit, 0 denied")
        self.assertEqual(lines[1], "Held for you because: 6 live-price ladders (type B: a fair first)")
        self.assertEqual(lines[2], "Auto mode won't list these: your call.")
        self.assertTrue(all(n.startswith("?") for n in lines[3:]))
        self.assertFalse(do_text)

    def test_a_pass_says_pass(self):
        g = dict(GREEN, preflight={"at": "x", "exit": 0, "pass": 15, "fail": [], "warn": ["resting-order room: ..."]})
        text, *_ = wa.upcoming_msg(dict(UP, windows=[g]), fetch_titles=False)
        self.assertIn("\nPreflight now: PASS (1 warning). At the start auto mode lists it on the default preset.\n", text)

    def test_nothing_new(self):
        text, push, subject, body, do_text = wa.upcoming_msg(dict(UP, windows=[window("2026-11-15T01:00:00Z", None, [])]),
                                                             fetch_titles=False)
        self.assertEqual(text, "UPCOMING: no new wave would be greenlit or held for you")
        self.assertFalse(do_text)

    def test_a_new_shape_is_held_for_you_and_texts(self):
        # Jack 2026-11-14: "for upcoming new shapes, notify me and it's my call so dont default to
        # turning it on e.g. KXYTVIEWSHIGH"
        yt = dict(series("KXYTVIEWSHIGH", "NEW", 16, 1000.0,
                         ["new shape: Entertainment (Views, Music) -- no wave has run one",
                          "settles on a live feed (charts.youtube.com): the bot's own scan keeps these out (Jack 09/24)"]),
                  category="Entertainment", dated="FIT")
        w = dict(window("2026-11-15T16:00:00Z", "needs-you", [yt], check=["KXYTVIEWSHIGH"],
                        preflight={"at": "x", "exit": 0, "pass": 15, "fail": [], "warn": []}),
                 new_shape_series=["KXYTVIEWSHIGH"], new_shape_over_bar=True)
        text, push, subject, body, do_text = wa.upcoming_msg(dict(UP, windows=[w]), fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "UPCOMING, YOUR CALL (new shape) Sun 11:00 AM ET (in 22 h): 16 events would be held "
                                   "for you, 0 greenlit, 0 denied (16 mkts, $1,000/day pools)")
        self.assertEqual(lines[1], "Held for you because: 16 new shape (Entertainment): no wave has run one")
        self.assertTrue(lines[2].startswith("Auto mode won't list a shape no wave has run: your call. Say \"run <SERIES>\""))
        self.assertTrue(lines[2].endswith("Preflight now, if you say yes: PASS."))
        self.assertTrue(do_text)
        self.assertIn("settles on a live feed (charts.youtube.com)", body)
        self.assertIn("PREFLIGHT NOW (pvd_waves.py preflight, as if you said yes, x): PASS", body)
        self.assertIn(wa.NEW_SHAPE_NOTE, body)

    def test_a_new_shape_beside_a_greenlit_wave_is_held_not_denied(self):
        yt = dict(series("KXSHOW", "NEW", 6, 500.0, ["new shape: Entertainment -- no wave has run one"]),
                  category="Entertainment")
        g = dict(GREEN, series=GREEN["series"] + [yt], new_check_series=["KXSPORT", "KXSHOW"])
        text, *_ = wa.upcoming_msg(dict(UP, windows=[g]), fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "UPCOMING WAVE Sun 12:00 AM ET (in 11 h): 6 events would be greenlit, 7 denied "
                                   "(6 mkts, $600/day pools)")
        self.assertIn("Held for you because: 6 new shape (Entertainment): no wave has run one", lines)

    def test_a_relight_emails_and_texts_only_on_a_preflight_fail(self):
        rl = {"waves": {"w1113-fxmetals": {"series": ["KXGOLDEVENT", "KXUSDJPYEVENT"], "markets": 1200, "events": 40,
                                           "pool_day": 15000.0, "window": "fx-shut"}},
              "fresh_series": ["KXGOLDEVENT", "KXUSDJPYEVENT"], "notice": True,
              "preflight": {"w1113-fxmetals": {"at": "x", "exit": 0, "pass": 12, "fail": [], "warn": []}}}
        w = dict(window("2026-11-20T18:00:00Z", None, []), relight=rl)
        text, push, subject, body, do_text = wa.upcoming_msg(dict(UP, windows=[w]), fetch_titles=False)
        lines = text.splitlines()
        self.assertEqual(lines[0], "UPCOMING RELIGHT Fri 1:00 PM ET (in 6 days): w1113-fxmetals relist 40 events "
                                   "(1,200 mkts, $15,000/day pools)")
        self.assertIn("w1113-fxmetals only in the FX close (Fri 5 PM - Sun 5 PM ET), with the fair", lines[1])
        self.assertEqual(lines[2], "Preflight now: PASS. Nothing needed from you.")
        self.assertFalse(do_text)
        rl["preflight"]["w1113-fxmetals"] = {"at": "x", "exit": 1, "pass": 11, "warn": [],
                                             "fail": ["cash shard 2: $400 free (needs $1,000)"]}
        text, push, subject, body, do_text = wa.upcoming_msg(dict(UP, windows=[w]), fetch_titles=False)
        self.assertIn("Preflight now: FAIL w1113-fxmetals: cash shard 2: $400 free (needs $1,000) -- fix it before "
                      "the start.", text)
        self.assertTrue(do_text)
        self.assertIn("FAIL cash shard 2: $400 free (needs $1,000)", body)

    def test_a_greenlit_wave_leads_a_relight(self):
        rl = {"waves": {"w1114-cryptoev": {"series": ["KXBTCEVENT"], "markets": 500, "events": 8, "pool_day": 3000.0,
                                           "window": ""}}, "fresh_series": ["KXBTCEVENT"], "notice": True,
              "preflight": {}}
        w = dict(window("2026-11-16T18:00:00Z", None, []), relight=rl)
        text, *_ = wa.upcoming_msg(dict(UP, windows=[w, GREEN]), fetch_titles=False)
        self.assertTrue(text.startswith("UPCOMING WAVE Sun 12:00 AM ET"))
        self.assertIn("Also: Mon 1:00 PM ET (in 2 days): w1114-cryptoev relights 500 mkts", text)

    def test_et_when(self):
        now = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(wa.et_when(datetime(2026, 11, 14, 18, 40, tzinfo=timezone.utc), now), "Sat 1:40 PM ET (in 40 min)")
        self.assertEqual(wa.et_when(datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc), now), "Tue 1:00 PM ET (in 3 days)")
        self.assertEqual(wa.et_when(datetime(2026, 7, 4, 16, 0, tzinfo=timezone.utc),
                                    datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)), "Sat 12:00 PM ET (in 4 h)")


if __name__ == "__main__":
    unittest.main()
