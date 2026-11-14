#!/usr/bin/env python3
"""Tests for imm_heightened.py, the heightened bait mode's detector (Jack
2026-11-13: "anytime there is suspicious activity e.g. show better price and
pull 3 times, move to 'heightened awareness bait mode'", then "Build it dry").
Run: python -m unittest test_imm_heightened"""

import unittest

import imm_heightened as H

T0 = 1_800_000_000.0
DAY_END = T0 + 10 * 3600.0          # "ET midnight" for these tests


def day_end(ts):
    return DAY_END if ts < DAY_END else DAY_END + 86400.0


class TestSideRef(unittest.TestCase):

    def test_first_level_where_the_size_reaches_target_over_5(self):
        # target 1000: 200 needed; 50 @ 60 + 100 @ 55 + 100 @ 50 -> 50c
        self.assertEqual(H.side_ref({60.0: 50, 55.0: 100, 50.0: 100}, 1000), (50.0, 100))
        self.assertEqual(H.side_ref({60.0: 200}, 1000), (60.0, 200))
        self.assertEqual(H.side_ref({60.0: 50}, 1000), (None, 0.0))
        self.assertEqual(H.side_ref({60.0: 500}, 0), (None, 0.0))


class TestHeightenedWatch(unittest.TestCase):
    T, T2, EV = "KXEVT-26NOV13-A", "KXEVT-26NOV13-B", "KXEVT-26NOV13"

    def setUp(self):
        self.w = H.HeightenedWatch(day_end=day_end)
        self.base = {42.0: 300.0}      # the standing reference: 42c

    def _flash(self, t0, t=None, side="bid", px=55.0, size=10456.0, life=20.0,
               follow=True, own0=None):
        """A better price shown at t0 and gone `life` s later -- our rung
        (at `own0` before it came) at the better price as it goes when
        `follow` -- then the tape grace run out; the rows of the tick."""
        t = t or self.T
        w = self.w
        w.observe(t, self.EV, side, dict(self.base), 1000, t0 - 30.0, own_best=own0)
        w.observe(t, self.EV, side, {**self.base, px: size}, 1000, t0, own_best=own0)
        w.observe(t, self.EV, side, dict(self.base), 1000, t0 + life,
                  own_best=px if follow else own0)
        return w.tick(t0 + life + 5.0)

    def test_a_better_price_pulled_untraded_is_a_pull(self):
        rows = self._flash(T0)
        pull, = [r for r in rows if r["ev"] == "pull"]
        self.assertEqual((pull["ticker"], pull["side"], pull["base"], pull["px"],
                          pull["gain_c"], pull["life_s"], pull["size"], pull["added"],
                          pull["n_market"], pull["n_event"], pull["joined"],
                          pull["followed"]),
                         (self.T, "bid", 42.0, 55.0, 13.0, 20.0, 10456.0, 10456.0,
                          1, 1, True, True))
        self.assertEqual(self.w.stats["pulls"], 1)

    def test_the_tape_grace_waits_before_it_counts(self):
        w = self.w
        w.observe(self.T, self.EV, "bid", dict(self.base), 1000, T0 - 30)
        w.observe(self.T, self.EV, "bid", {**self.base, 55.0: 500}, 1000, T0)
        w.observe(self.T, self.EV, "bid", dict(self.base), 1000, T0 + 20, own_best=55.0)
        self.assertEqual(w.tick(T0 + 21), [])                 # inside the grace
        self.assertEqual(len(w.pending), 1)
        self.assertEqual([r["ev"] for r in w.tick(T0 + 24)], ["pull"])

    def test_a_better_price_that_traded_was_taken_not_pulled(self):
        # a seller hit the 55c bid (taker bought NO at 45 = sold YES at 55)
        self.w.note_print(self.T, "no", 55.0, 500, T0 + 19.5)
        self.assertEqual(self._flash(T0, size=500), [])
        self.assertEqual((self.w.stats["taken"], self.w.stats["pulls"]), (1, 0))

    def test_a_print_on_the_other_side_or_at_the_base_does_not_count_as_taken(self):
        self.w.note_print(self.T, "yes", 55.0, 500, T0 + 5)   # hit the ask side
        self.w.note_print(self.T, "no", 42.0, 50, T0 + 6)     # at the old base
        rows = self._flash(T0, size=500)
        self.assertEqual([r["ev"] for r in rows], ["pull"])

    def test_the_ask_side_reads_no_prices(self):
        # an ask flashed at YES 45c (NO bid 55c) over a YES 58c ask (NO 42c),
        # taken by a buyer of YES at 45: a take
        self.w.note_print(self.T, "yes", 45.0, 300, T0 + 10)
        self.assertEqual(self._flash(T0, side="ask", size=300), [])
        # untraded: a pull, logged in YES cents
        rows = self._flash(T0 + 600, side="ask", size=300)
        pull, = rows
        self.assertEqual((pull["side"], pull["base"], pull["px"]), ("ask", 58.0, 45.0))

    def test_a_better_price_that_stood_is_a_reprice(self):
        w = self.w
        w.observe(self.T, self.EV, "bid", dict(self.base), 1000, T0 - 30)
        w.observe(self.T, self.EV, "bid", {**self.base, 55.0: 500}, 1000, T0)
        # an identical book is not re-read; the next change past 2 min settles it
        w.observe(self.T, self.EV, "bid", {**self.base, 55.0: 500}, 1000, T0 + 60)
        self.assertEqual(w.stats["stood"], 0)
        w.observe(self.T, self.EV, "bid", {**self.base, 55.0: 500, 30.0: 10}, 1000,
                  T0 + 121)
        self.assertEqual(w.stats["stood"], 1)
        self.assertEqual(w.sides[(self.T, "bid")]["base"], 55.0)
        # its later removal is a worse price, not a pull
        w.observe(self.T, self.EV, "bid", dict(self.base), 1000, T0 + 200)
        self.assertEqual(w.tick(T0 + 300), [])
        self.assertEqual(w.stats["pulls"], 0)

    def test_a_small_better_order_that_does_not_move_the_reference_is_no_flash(self):
        w = self.w
        w.observe(self.T, self.EV, "bid", dict(self.base), 1000, T0 - 30)
        w.observe(self.T, self.EV, "bid", {**self.base, 55.0: 50}, 1000, T0)
        self.assertIsNone(w.sides[(self.T, "bid")]["flash"])
        self.assertEqual(w.stats["flashes"], 0)

    def test_a_walk_up_then_gone_is_one_pull_at_its_highest(self):
        w = self.w
        w.observe(self.T, self.EV, "bid", {42.0: 300}, 1000, T0 - 30)
        w.observe(self.T, self.EV, "bid", {42.0: 300, 44.0: 10456}, 1000, T0)
        w.observe(self.T, self.EV, "bid", {42.0: 300, 50.0: 10456}, 1000, T0 + 18)
        w.observe(self.T, self.EV, "bid", {42.0: 300, 55.0: 10456}, 1000, T0 + 35)
        w.observe(self.T, self.EV, "bid", {42.0: 300}, 1000, T0 + 56, own_best=55.0)
        pull, = w.tick(T0 + 60)
        self.assertEqual((pull["px"], pull["base"], pull["life_s"]), (55.0, 42.0, 56.0))

    def test_three_pulls_in_30_min_trip_the_market_and_the_event(self):
        self._flash(T0)
        self._flash(T0 + 300)
        rows = self._flash(T0 + 900)
        trips = {r["key"]: r for r in rows if r["ev"] == "trip"}
        self.assertEqual(sorted(trips), ["e|" + self.EV, "m|" + self.T])
        m = trips["m|" + self.T]
        self.assertEqual((m["scope"], m["trips_today"], m["to_midnight"], m["markets"],
                          len(m["pulls"])), ("market", 1, False, [self.T], 3))
        self.assertAlmostEqual(m["until"], T0 + 920 + 3600, places=1)
        self.assertTrue(m["pulls"][-1]["joined"])       # our rung had followed it
        # a fourth inside the window extends it, no new trip
        rows = self._flash(T0 + 1500)
        self.assertEqual([r["ev"] for r in rows], ["pull"])
        self.assertAlmostEqual(self.w.windows["m|" + self.T]["until"],
                               T0 + 1520 + 3600, places=1)

    def test_pulls_spread_over_the_event_trip_the_event_only(self):
        self._flash(T0, t=self.T)
        self._flash(T0 + 60, t=self.T2)
        rows = self._flash(T0 + 120, t=self.T)
        self.assertEqual([r["key"] for r in rows if r["ev"] == "trip"], ["e|" + self.EV])
        ev_trip, = [r for r in rows if r["ev"] == "trip"]
        self.assertEqual(ev_trip["markets"], [self.T, self.T2])

    def test_pulls_older_than_the_window_do_not_count(self):
        self._flash(T0)
        self._flash(T0 + 1000)
        rows = self._flash(T0 + 1900)            # the first is 1,920 s old
        self.assertEqual([r["ev"] for r in rows], ["pull"])

    def test_a_window_clears_after_its_hold_and_a_second_trip_runs_to_midnight(self):
        for k in range(3):
            self._flash(T0 + 60 * k)
        rows = self.w.tick(T0 + 140 + 3600 + 1)
        clears = {r["key"]: r for r in rows if r["ev"] == "clear"}
        self.assertEqual(sorted(clears), ["e|" + self.EV, "m|" + self.T])
        self.assertEqual(clears["m|" + self.T]["pulls"], 3)
        t1 = T0 + 4000
        for k in range(3):
            rows = self._flash(t1 + 60 * k)
        m, = [r for r in rows if r["ev"] == "trip" and r["scope"] == "market"]
        self.assertEqual((m["trips_today"], m["to_midnight"], m["until"]),
                         (2, True, DAY_END))

    def test_fills_inside_a_window_are_logged(self):
        self.assertEqual(self.w.note_fill(self.T, self.EV, "bid", 40.0, 100, T0, "o1"), [])
        for k in range(3):
            self._flash(T0 + 60 * k)
        # at the old base: no strike, just the window's fill
        f, = self.w.note_fill(self.T, self.EV, "bid", 42.0, 4000, T0 + 200, "o1")
        self.assertEqual((f["scope"], f["key"], f["count"], f["side_pulled"]),
                         ("market", "m|" + self.T, 4000.0, True))
        self.assertAlmostEqual(f["since_trip_s"], 200 - 140, places=1)
        # a sibling's fill falls in the event window
        g, = self.w.note_fill(self.T2, self.EV, "ask", 45.0, 10, T0 + 210, "o2")
        self.assertEqual((g["scope"], g["side_pulled"]), ("event", False))
        clear = {r["key"]: r for r in self.w.tick(T0 + 140 + 3601)}
        self.assertEqual((clear["m|" + self.T]["fills"], clear["e|" + self.EV]["fills"]),
                         (1, 1))

    def test_the_pulled_size_coming_back_in_the_event_is_logged(self):
        for k in range(3):
            self._flash(T0 + 60 * k, size=10456.0)
        w = self.w
        # the same lot on the other side, as on Paris 11/02 leg 2
        w.observe(self.T, self.EV, "ask", {42.0: 300}, 1000, T0 + 300)
        rows = w.observe(self.T, self.EV, "ask", {42.0: 300, 55.0: 10456.0}, 1000,
                         T0 + 360)
        a, = rows
        self.assertEqual((a["ev"], a["side"], a["px"], a["size"]),
                         ("actor", "ask", 45.0, 10456.0))
        # once per 10 minutes per level
        w.observe(self.T, self.EV, "ask", {42.0: 300}, 1000, T0 + 370)
        self.assertEqual(w.observe(self.T, self.EV, "ask", {42.0: 300, 55.0: 10456.0},
                                   1000, T0 + 380), [])

    def test_uncounted_pulls_are_stats_only(self):
        w = self.w
        self.assertEqual(self._flash(T0, px=45.0), [])                 # 3c over
        self.assertEqual(self._flash(T0 + 300, life=5.0), [])          # stood 5 s
        self.assertEqual(self._flash(T0 + 600, follow=False), [])      # not followed
        # our rung was at the better price before it came: not a follow
        self.assertEqual(self._flash(T0 + 900, own0=55.0), [])
        w.exempt = lambda t: "spot"
        self.assertEqual(self._flash(T0 + 1200), [])
        self.assertEqual((w.stats["raw_pulls"], w.stats["pulls"], w.stats["unc_small"],
                          w.stats["unc_brief"], w.stats["unc_unfollowed"],
                          w.stats["unc_exempt"]), (5, 0, 1, 1, 2, 1))
        self.assertEqual((w.pulls_mkt, w.windows), ({}, {}))
        # the knobs: every pull counts with them off
        w2 = H.HeightenedWatch(day_end=day_end, count_min_c=1, min_life_secs=0,
                               followed_only=False)
        self.w = w2
        self.assertEqual([r["ev"] for r in self._flash(T0, px=45.0, life=5.0,
                                                      follow=False)], ["pull"])

    def test_a_fill_of_ours_after_a_counted_pull_is_a_strike(self):
        self._flash(T0)                                   # pulled at T0 + 20
        s, = self.w.note_fill(self.T, self.EV, "bid", 55.0, 4000, T0 + 50, "o1")
        self.assertEqual((s["ev"], s["px"], s["count"], s["since_pull_s"], s["pull_px"],
                          s["pull_base"], s["order_id"]),
                         ("strike", 55.0, 4000.0, 30.0, 55.0, 42.0, "o1"))
        # at the old base, the other side, or too late: none
        self.assertEqual(self.w.note_fill(self.T, self.EV, "bid", 42.0, 10, T0 + 60, "o2"), [])
        self.assertEqual(self.w.note_fill(self.T, self.EV, "ask", 45.0, 10, T0 + 60, "o3"), [])
        self.assertEqual(self.w.note_fill(self.T, self.EV, "bid", 55.0, 10, T0 + 141, "o4"), [])
        self.assertEqual(self.w.stats["strikes"], 1)

    def test_hit_while_it_was_pulled_is_a_pull_and_a_strike_not_a_take(self):
        # Paris 11/02: the lot vanished as a taker sold 4,456 at 55 -- our
        # 4,000 and 456 of the other followers'
        self.w.note_print(self.T, "no", 55.0, 4000, T0 + 19.8)
        self.w.note_print(self.T, "no", 55.0, 456, T0 + 19.8)
        self.w.note_fill(self.T, self.EV, "bid", 55.0, 4000, T0 + 19.9, "o1")
        rows = self._flash(T0)
        self.assertEqual([r["ev"] for r in rows], ["pull", "strike"])
        self.assertEqual(rows[1]["since_pull_s"], -0.1)
        self.assertEqual(self.w.stats["taken"], 0)
        # ...while a print mostly of someone else's size is a take
        self.w.note_print(self.T, "no", 55.0, 6000, T0 + 619.8)
        self.assertEqual(self._flash(T0 + 600), [])
        self.assertEqual(self.w.stats["taken"], 1)

    def test_a_restart_keeps_windows_pulls_and_sizes(self):
        for k in range(2):
            self._flash(T0 + 60 * k)
        w2 = H.HeightenedWatch(day_end=day_end)
        w2.load(self.w.payload(T0 + 100), T0 + 100)
        self.w = w2
        rows = self._flash(T0 + 120)                   # the 3rd, after the restart
        self.assertEqual(sorted(r["key"] for r in rows if r["ev"] == "trip"),
                         ["e|" + self.EV, "m|" + self.T])
        w3 = H.HeightenedWatch(day_end=day_end)
        w3.load(w2.payload(T0 + 200), T0 + 200)
        self.assertEqual(sorted(w3.windows), ["e|" + self.EV, "m|" + self.T])
        self.assertIn(10456.0, w3.sizes[self.EV])
        # each pull is kept once, compactly
        pay = w2.payload(T0 + 200)
        self.assertEqual(len(pay["pulls"]), 3)
        self.assertNotIn("pulls_ev", pay)
        self.assertEqual(pay["pulls"][0][1:4], [self.T, self.EV, "bid"])
        # junk never raises
        H.HeightenedWatch(day_end=day_end).load({"windows": 5, "trips_day": {"x": "y"}}, T0)
        H.HeightenedWatch(day_end=day_end).load("nope", T0)

    def test_the_first_builds_payload_still_loads(self):
        old = {"pulls_mkt": {self.T: [
            {"ticker": self.T, "side": "bid", "ts": T0 + 60 * k, "base": 42.0,
             "px": 55.0, "life_s": 20.0, "size": 500.0, "added": 500.0,
             "joined": False} for k in range(2)]},
            "pulls_ev": {self.EV: []}}
        self.w.load(old, T0 + 130)
        self.assertEqual((len(self.w.pulls_mkt[self.T]), len(self.w.pulls_ev[self.EV])),
                         (2, 2))
        rows = self._flash(T0 + 140)
        self.assertEqual(sorted(r["key"] for r in rows if r["ev"] == "trip"),
                         ["e|" + self.EV, "m|" + self.T])

    def test_prune_forgets_old_sides_and_prints(self):
        self._flash(T0)
        self.w.note_print(self.T, "no", 30.0, 5, T0)
        self.w.tick(T0 + 4000)
        self.assertEqual((self.w.sides, self.w.prints, self.w.pulls_mkt), ({}, {}, {}))


if __name__ == "__main__":
    unittest.main()
