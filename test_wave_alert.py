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
        r = {"hours": 6, "flagged": [self.ROW], "new": [self.ROW], "acted": [], "series_trips": [],
             "flagged_usd": 439.19, "params": {"min_c": 5, "min_usd": 25}}
        text, push, subject, body, do_text = wa.pickoff_msg(r)
        lines = text.splitlines()
        self.assertEqual(lines[0], "PICK-OFF: 1 wave mkt -$439 new (1 flagged, -$439 in 6h)")
        self.assertIn("our orders out", lines[1])
        self.assertIn("legacy waves", text)
        self.assertIn("x1. KXHOUSERACE-IN04-28-D bought 1,273 @40, mark 5.5 (-$439)", text)
        self.assertTrue(do_text)
        self.assertTrue(subject.startswith("market-wave pick-off: PICK-OFF"))

    def test_small_ones_email_only(self):
        row = dict(self.ROW, adverse_usd=40.0, contracts=100.0)
        r = {"hours": 6, "flagged": [row], "new": [row], "acted": [], "series_trips": [], "flagged_usd": 40.0}
        self.assertFalse(wa.pickoff_msg(r)[4])
        r["series_trips"] = [{"wave": "w", "series": "KXHOUSERACE", "markets": ["a", "b", "c"]}]
        self.assertTrue(wa.pickoff_msg(r)[4])                       # a series trip always texts


if __name__ == "__main__":
    unittest.main()
