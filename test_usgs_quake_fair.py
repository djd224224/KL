"""Tests for usgs_quake_fair (the KXBIGGESTQUAKE bid-only gate's feeds)."""
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import usgs_quake_fair as q

DAY = 1790467200.0          # 2026-09-27 00:00Z
H = 3600.0


def usgs_ev(eid, mag, t, net="us"):
    return {"id": eid, "mag": mag, "mag_type": "mww", "net": net, "time": t,
            "place": "somewhere"}


def gfz_ev(eid, mag, t):
    return {"id": eid, "mag": mag, "mag_type": "mb", "time": t, "place": "x"}


class TestRates(unittest.TestCase):
    def test_table_interpolation_and_limits(self):
        self.assertAlmostEqual(q.lam_eff(5.2), -math.log(1 - 0.890411), places=6)
        self.assertAlmostEqual(q.lam_eff(7.0), -math.log(1 - 0.034521), places=6)
        mid = q.lam_eff(5.3)
        self.assertAlmostEqual(mid, math.sqrt(q.lam_eff(5.2) * q.lam_eff(5.4)), places=9)
        self.assertGreater(q.lam_eff(4.8), q.lam_eff(5.2))       # extrapolated
        self.assertLess(q.lam_eff(7.4), q.lam_eff(7.0))
        self.assertIsNone(q.lam_eff(4.7))                         # past the reach
        self.assertIsNone(q.lam_eff(7.5))
        self.assertIsNone(q.lam_eff("x"))
        self.assertIsNone(q.lam_eff(float("nan")))

    def test_window_before_during_and_after_the_day(self):
        lam = q.lam_eff(6.0)
        full = 1 - math.exp(-lam)
        self.assertAlmostEqual(q.p_yes(6.0, DAY, DAY - 4 * H, 0.3), full)   # listed 20Z
        self.assertAlmostEqual(q.p_yes(6.0, DAY, DAY + 12 * H, 0.0),
                               1 - math.exp(-lam * 12 / 24))
        self.assertAlmostEqual(q.p_yes(6.0, DAY, DAY + 12 * H, 0.3),
                               1 - math.exp(-lam * 12.3 / 24))              # unpublished tail
        self.assertAlmostEqual(q.p_yes(6.0, DAY, DAY + 24 * H + 60, 0.3),
                               1 - math.exp(-lam * (0.3 - 60 / H) / 24))    # tail past close
        self.assertEqual(q.p_yes(6.0, DAY, DAY + 25 * H, 0.3), 0.0)
        self.assertIsNone(q.p_yes(8.0, DAY, DAY, 0.3))


class TestParsing(unittest.TestCase):
    def test_usgs_geojson(self):
        payload = {"metadata": {"generated": 1790500000000}, "features": [
            {"id": "us1", "properties": {"mag": 5.4, "magType": "mww", "net": "us",
                                         "time": 1790490000000, "place": "Fiji"}},
            {"id": "ak2", "properties": {"mag": None, "time": 1790490000000}},
            {"id": "ak3", "properties": {"mag": 4.9, "net": "AK", "time": 1790491000000,
                                         "updated": 1790492000000}}]}
        gen, evs = q.parse_usgs(payload)
        self.assertEqual(gen, 1790500000.0)
        self.assertEqual([e["id"] for e in evs], ["us1", "ak3"])
        self.assertEqual(evs[1]["net"], "ak")
        self.assertNotIn("updated", evs[0])
        self.assertEqual(evs[1]["updated"], 1790492000.0)
        self.assertEqual(q.parse_usgs({}), (None, []))

    def test_gfz_text(self):
        text = ("#EventID|Time|Latitude|Longitude|Depth/km|Author|Catalog|Contributor|"
                "ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType\n"
                "gfz2026a|2026-09-27T10:57:07.47|68.9|-17.0|10.0|||GFZ|gfz2026a|mb|4.87||Iceland Region|earthquake\n"
                "broken|line\n"
                "gfz2026b|2026-09-27T01:18:25|-21.2|168.5|10.0|||GFZ|gfz2026b|Mw|x||Loyalty|earthquake\n")
        evs = q.parse_gfz(text)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["id"], "gfz2026a")
        self.assertAlmostEqual(evs[0]["time"], DAY + 10 * H + 57 * 60 + 7.47, places=2)
        self.assertEqual(evs[0]["mag"], 4.87)
        self.assertEqual(q.parse_gfz(""), [])

    def test_fetch_gfz_no_content(self):
        r = mock.Mock(status_code=204)
        with mock.patch.object(q.requests, "get", return_value=r):
            self.assertEqual(q.fetch_gfz(now_ts=DAY), [])


class TestVerdict(unittest.TestCase):
    def watch(self, now, usgs=(), gfz=(), gen_age=30, gfz_age=5):
        w = q.QuakeWatch()
        w.created = now - 3600
        w.update_usgs(now - gen_age, list(usgs), now)
        w.update_gfz(list(gfz), now - gfz_age)
        return w

    @staticmethod
    def poll(w, t, usgs, gfz=None):
        """One refresher pass at time t (the feed generated 30 s earlier)."""
        w.update_usgs(t - 30, list(usgs), t)
        if gfz is not None:
            w.update_gfz(list(gfz), t)

    def test_quote_with_the_fair_cap(self):
        now = DAY + 12 * H
        v = self.watch(now).verdict(6.0, DAY, now)
        fair = q.p_yes(6.0, DAY, now, q.GFZ_LAG_H)
        self.assertEqual(v["action"], "quote")
        self.assertEqual(v["cap_c"], math.floor(fair * 100 - 1))
        self.assertTrue(v["gfz_fresh"])

    def test_stale_usgs_stands_aside(self):
        now = DAY + 12 * H
        w = self.watch(now)
        self.assertEqual(w.verdict(6.0, DAY, now + q.USGS_STALE_SECS + 1)["reason"],
                         "usgs_stale")
        w = self.watch(now, gen_age=q.USGS_GEN_STALE_SECS + 5)       # CDN serving old copy
        self.assertEqual(w.verdict(6.0, DAY, now)["reason"], "usgs_stale")
        self.assertEqual(q.QuakeWatch().verdict(6.0, DAY, now)["reason"], "usgs_stale")

    def test_crossed_only_by_this_days_quakes(self):
        now = DAY + 12 * H
        w = self.watch(now, usgs=[usgs_ev("us1", 6.1, DAY + 2 * H),
                                  usgs_ev("us0", 6.9, DAY - 2 * H)])   # yesterday's
        self.assertEqual(w.verdict(6.0, DAY, now)["reason"], "crossed")
        self.assertEqual(w.verdict(6.1, DAY, now)["reason"], "crossed")  # >= K
        self.assertEqual(w.verdict(6.2, DAY, now)["action"], "quote")

    def test_fair_floor_stands_aside(self):
        now = DAY + 23 * H
        v = self.watch(now).verdict(7.0, DAY, now)
        self.assertEqual((v["action"], v["reason"]), ("stand", "fair_floor"))

    def test_gfz_detection_holds_until_neic_confirms(self):
        now = DAY + 12 * H
        t0 = now - 300
        g1 = [gfz_ev("g1", 5.9, t0)]
        w = self.watch(now, gfz=g1, gfz_age=0)
        v = w.verdict(6.2, DAY, now)
        self.assertEqual((v["action"], v["reason"]), ("hold", "detection"))
        self.assertIsNotNone(v["cap_c"])
        # the tsunami centre's first number is not NEIC: still held
        pt = [usgs_ev("pt1", 6.0, t0 + 3, net="pt")]
        w.update_usgs(now, pt, now)
        self.poll(w, now + 170, pt, gfz=g1)
        self.assertEqual(w.verdict(6.2, DAY, now + 170)["action"], "hold")
        # NEIC takes over; held until its magnitude has shown CONFIRM_SECS
        us = [usgs_ev("pt1", 5.9, t0 + 3, net="us")]
        self.poll(w, now + 200, us, gfz=g1)
        self.poll(w, now + 250, us, gfz=g1)
        self.assertEqual(w.verdict(6.2, DAY, now + 250)["action"], "hold")
        self.poll(w, now + 200 + q.CONFIRM_SECS, us, gfz=g1)
        self.assertEqual(w.verdict(6.2, DAY, now + 200 + q.CONFIRM_SECS)["action"],
                         "quote")

    def test_regional_network_confirms_after_the_neic_wait(self):
        now = DAY + 12 * H
        ak = [usgs_ev("ak1", 5.0, now - 10 * 60, net="ak")]
        w = self.watch(now, usgs=ak)
        self.poll(w, now + q.CONFIRM_SECS, ak, gfz=[])
        self.assertEqual(w.verdict(6.0, DAY, now + q.CONFIRM_SECS)["action"], "hold")
        late = now - 10 * 60 + q.NEIC_WAIT_MIN * 60
        self.poll(w, late, ak, gfz=[])
        self.assertEqual(w.verdict(6.0, DAY, late)["action"], "quote")

    def test_old_small_and_expired_detections_do_not_hold(self):
        now = DAY + 12 * H
        w = self.watch(now, usgs=[usgs_ev("us9", 5.5, now - 3 * H)],     # not news
                       gfz=[gfz_ev("g2", 4.6, now - 60)])                 # too small
        self.assertEqual(w.verdict(6.0, DAY, now)["action"], "quote")
        g3 = [gfz_ev("g3", 5.5, now - 60)]
        w = self.watch(now, gfz=g3, gfz_age=0)
        later = now + q.FREEZE_MAX_MIN * 60 + 1
        self.poll(w, later, [], gfz=g3)                        # both feeds fresh
        self.assertEqual(w.verdict(6.0, DAY, later)["action"], "quote")  # expired

    def test_a_restart_does_not_extend_the_freeze(self):
        # Jack 2026-10-07: the 00:46Z / 00:56Z restarts each re-saw a 00:30Z
        # GFZ M5.08 as new. The clock starts no later than origin +
        # FIRST_SEEN_MAX_LAG_MIN, whatever the process saw.
        now = DAY + 12 * H
        origin = now - 60 * 60                    # an hour-old detection
        g = [gfz_ev("g7", 5.1, origin)]
        w = self.watch(now, gfz=g, gfz_age=0)     # a fresh process: first seen now
        end = origin + (q.FIRST_SEEN_MAX_LAG_MIN + q.FREEZE_MAX_MIN) * 60
        self.assertGreater(end, now)
        self.assertEqual(w.verdict(6.0, DAY, now)["action"], "hold")
        self.poll(w, end + 1, [], gfz=g)
        self.assertEqual(w.verdict(6.0, DAY, end + 1)["action"], "quote")
        # a live process that saw it at publication keeps the full freeze
        t0 = now - 5 * 60
        g2 = [gfz_ev("g8", 5.1, t0)]
        w = self.watch(now, gfz=g2, gfz_age=0)    # first seen 5 min after origin
        self.poll(w, now + q.FREEZE_MAX_MIN * 60 - 30, [], gfz=g2)
        self.assertEqual(w.verdict(6.0, DAY, now + q.FREEZE_MAX_MIN * 60 - 30)["action"], "hold")
        self.poll(w, now + q.FREEZE_MAX_MIN * 60 + 30, [], gfz=g2)
        self.assertEqual(w.verdict(6.0, DAY, now + q.FREEZE_MAX_MIN * 60 + 30)["action"], "quote")

    def test_a_restart_credits_a_settled_usgs_solution(self):
        # Jack 2026-10-07: the 02:33Z restart re-froze on gfz2026tpwq, already
        # matched to NEIC's settled M4.6. On the first read the clock runs
        # from the solution's 'updated' stamp up to the feed copy.
        now = DAY + 12 * H
        t0 = now - 25 * 60
        g = [gfz_ev("g9", 5.05, t0)]
        settled = dict(usgs_ev("us9", 4.6, t0 + 4), updated=now - 10 * 60)
        w = self.watch(now, usgs=[settled], gfz=g, gfz_age=0)
        self.assertEqual(w.verdict(6.0, DAY, now)["action"], "quote")
        # revised 20 s before the copy (generated now - 30): 100 s still to wait
        recent = dict(usgs_ev("us9", 4.6, t0 + 4), updated=now - 50)
        w = self.watch(now, usgs=[recent], gfz=g, gfz_age=0)
        self.assertEqual(w.verdict(6.0, DAY, now)["action"], "hold")
        self.poll(w, now + 90, [recent], gfz=g)
        self.assertEqual(w.verdict(6.0, DAY, now + 90)["action"], "hold")
        self.poll(w, now + 101, [recent], gfz=g)
        self.assertEqual(w.verdict(6.0, DAY, now + 101)["action"], "quote")
        # a stamp past the copy, or none: the old clock from the first read
        for ev in (dict(usgs_ev("us9", 4.6, t0 + 4), updated=now + 5),
                   usgs_ev("us9", 4.6, t0 + 4)):
            w = self.watch(now, usgs=[ev], gfz=g, gfz_age=0)
            self.assertEqual(w.verdict(6.0, DAY, now)["action"], "hold")
            self.poll(w, now + q.CONFIRM_SECS, [ev], gfz=g)
            self.assertEqual(w.verdict(6.0, DAY, now + q.CONFIRM_SECS)["action"], "quote")
        # only the first read: a running process keeps the full clock
        w = self.watch(now, usgs=[], gfz=g, gfz_age=0)
        self.poll(w, now + 15, [settled], gfz=g)
        self.assertEqual(w.verdict(6.0, DAY, now + 15)["action"], "hold")
        # a regional network still waits out NEIC_WAIT_MIN from the origin
        ak = dict(usgs_ev("ak9", 5.0, now - 5 * 60, net="ak"), updated=now - 4 * 60)
        w = self.watch(now, usgs=[ak], gfz_age=0)
        self.assertEqual(w.verdict(6.0, DAY, now)["action"], "hold")

    def test_stale_gfz_holds_then_prices_on_usgs_alone(self):
        now = DAY + 12 * H
        w = self.watch(now, gfz_age=q.GFZ_STALE_SECS + 10)
        v = w.verdict(6.0, DAY, now)
        self.assertEqual((v["action"], v["reason"]), ("hold", "gfz_stale"))
        w = self.watch(now, gfz_age=q.GFZ_STALE_SECS + q.FREEZE_MAX_MIN * 60 + 10)
        v = w.verdict(6.0, DAY, now)
        self.assertEqual(v["action"], "quote")
        self.assertEqual(v["lag_h"], q.USGS_LAG_H)
        never = q.QuakeWatch()                                # GFZ never answered
        never.created = now - q.FREEZE_MAX_MIN * 60 - 10
        never.update_usgs(now, [], now)
        self.assertEqual(never.verdict(6.0, DAY, now)["action"], "quote")

    def test_status_file(self):
        now = DAY + 12 * H
        w = self.watch(now, usgs=[usgs_ev("us1", 5.1, DAY + H)])
        path = os.path.join(tempfile.mkdtemp(), "st.json")
        q.write_status(path, w, now)
        st = json.load(open(path, encoding="utf-8"))
        self.assertEqual(st["today_max"]["mag"], 5.1)
        self.assertEqual(st["usgs_events"], 1)
        self.assertIn("freeze_max_min", st["knobs"])
        self.assertIn("ask_pull_below_mag", st["knobs"])


def _watch(now, usgs=(), gfz=(), gen_age=30, gfz_age=5):
    w = q.QuakeWatch()
    w.created = now - 3600
    w.update_usgs(now - gen_age, list(usgs), now)
    w.update_gfz(list(gfz), now - gfz_age)
    return w


def settled_ev(eid, mag, t, net="us"):
    """A USGS solution unchanged since 10 min after its origin: a new
    process credits it as settled on its first read (_settled_since)."""
    return dict(usgs_ev(eid, mag, t, net=net), updated=t + 600)


class TestAftershockTerm(unittest.TestCase):
    """The raise-only Reasenberg-Jones term (Jack 2026-10-26: "Yes build and
    then go live and monitor it", on the 10/15-16 backtest)."""

    M77 = usgs_ev("us77", 7.7, DAY - 6 * H)

    @staticmethod
    def watch(now, catalog=None, cat_age=60, usgs=()):
        w = _watch(now, usgs=usgs)
        if catalog is not None:
            w.update_as_catalog(list(catalog), now - cat_age)
        return w

    def test_background_table_and_the_omori_integral(self):
        for k, lam in zip(q.KS, q.LAM_BG):
            self.assertAlmostEqual(q.lam_bg_eff(k), lam, places=9)
            self.assertLess(lam, q.lam_eff(k))     # the table minus the mean aftershock share
        p, c = q.AS_P, q.AS_C
        self.assertAlmostEqual(q._omori(0.0, 1.0), ((1 + c) ** (1 - p) - c ** (1 - p)) / (1 - p))
        self.assertEqual(q._omori(2.0, 1.0), 0.0)                 # an empty window

    def test_no_sources_leaves_the_table_cap(self):
        now = DAY + 12 * H
        table = q.p_yes(6.0, DAY, now, q.GFZ_LAG_H)
        for w in (self.watch(now), self.watch(now, catalog=[])):  # never read / empty
            v = w.verdict(6.0, DAY, now)
            self.assertEqual(v["cap_c"], math.floor(table * 100 - 1))
            self.assertEqual(v["fair"], v["fair_table"])
        self.assertIsNone(self.watch(now).verdict(6.0, DAY, now)["fair_as"])
        quiet = self.watch(now, catalog=[]).verdict(6.0, DAY, now)
        self.assertLess(quiet["fair_as"], quiet["fair_table"])     # raise-only: the table prices

    def test_a_big_quake_raises_the_cap_and_decays(self):
        now = DAY + 1 * H
        v = self.watch(now, catalog=[self.M77]).verdict(5.8, DAY, now)
        start = now - q.GFZ_LAG_H * H
        n = q.aftershock_n(5.8, start, DAY + 86400, now, [(self.M77["time"], 7.7)])
        want = 1 - math.exp(-(q.lam_bg_eff(5.8) * (DAY + 86400 - start) / 86400 + n))
        self.assertAlmostEqual(v["fair_as"], round(want * 100, 2))
        self.assertGreater(v["fair"], v["fair_table"] + 10)       # ~+13 points 7 h after
        self.assertEqual(v["fair"], v["fair_as"])
        self.assertEqual(v["cap_c"], math.floor(want * 100 - 1))
        later = DAY + 7 * 86400 + H                                # a week on: t^-1.08
        v7 = self.watch(later, catalog=[self.M77]).verdict(5.8, DAY + 7 * 86400, later)
        self.assertLess(v7["fair_as"] - v7["fair_table"], v["fair_as"] - v["fair_table"])
        # a source counts only AS_SRC_LAG_H after its origin, and for AS_WINDOW_D
        just = usgs_ev("us78", 7.7, now - 0.1 * H)
        self.assertEqual(q.aftershock_n(5.8, start, DAY + 86400, now,
                                        [(just["time"], 7.7)]), 0.0)
        old = DAY - 400 * 86400
        self.assertEqual(q.aftershock_n(5.8, start, DAY + 86400, now, [(old, 8.5)]), 0.0)

    def test_a_stale_catalog_falls_back_to_the_table(self):
        now = DAY + 1 * H
        v = self.watch(now, catalog=[self.M77],
                       cat_age=q.AS_STALE_SECS + 1).verdict(5.8, DAY, now)
        self.assertIsNone(v["fair_as"])
        self.assertEqual(v["fair"], v["fair_table"])

    def test_shadow_logs_without_pricing_and_off_skips_it(self):
        now = DAY + 1 * H
        with mock.patch.object(q, "AS_MODE", "shadow"):
            v = self.watch(now, catalog=[self.M77]).verdict(5.8, DAY, now)
            self.assertGreater(v["fair_as"], v["fair_table"])
            self.assertEqual(v["fair"], v["fair_table"])
        with mock.patch.object(q, "AS_MODE", "off"):
            v = self.watch(now, catalog=[self.M77]).verdict(5.8, DAY, now)
            self.assertIsNone(v["fair_as"])
            self.assertEqual(v["fair"], v["fair_table"])

    def test_the_live_feed_adds_confirmed_quakes_only(self):
        now = DAY + 3 * H
        settled = settled_ev("us65", 6.5, DAY + 1 * H)             # NEIC's, settled
        fresh = usgs_ev("us66", 6.5, DAY + 2.9 * H)                # just in, unconfirmed
        base = self.watch(now, catalog=[]).verdict(6.6, DAY, now)["fair_as"]
        on = self.watch(now, catalog=[], usgs=[settled]).verdict(6.6, DAY, now)
        off = self.watch(now, catalog=[], usgs=[fresh]).verdict(6.6, DAY, now)
        self.assertGreater(on["fair_as"], base)
        self.assertEqual(off["fair_as"], base)
        w = self.watch(now, catalog=[usgs_ev("small", 5.4, DAY - H), self.M77])
        self.assertEqual(list(w.as_catalog), ["us77"])             # under AS_MMIN dropped

    def test_one_quake_under_two_ids_counts_once(self):
        # ComCat's 10/24 Bonin M7.0: pt26297050 and pt26298050, same second
        now = DAY + 1 * H
        t = DAY - 30 * H
        twin = [usgs_ev("pt1", 7.0, t, net="pt"), usgs_ev("pt2", 7.0, t, net="pt")]
        one = self.watch(now, catalog=twin[:1]).verdict(6.0, DAY, now)["fair_as"]
        two = self.watch(now, catalog=twin).verdict(6.0, DAY, now)["fair_as"]
        self.assertEqual(one, two)
        self.assertEqual(q.dedup_sources([(t, 7.0), (t + 30, 7.2), (t + 600, 6.0)]),
                         [(t, 7.2), (t + 600, 6.0)])

    def test_the_ask_floor_and_the_status_file(self):
        now = DAY + 1 * H
        w = self.watch(now, catalog=[self.M77])
        a = w.ask_verdict(6.6, DAY, now)
        v = w.verdict(6.6, DAY, now)
        if a["ok"]:
            self.assertGreaterEqual(a["floor_c"], math.ceil(v["fair_as"] + q.ASK_MARGIN_CENTS))
        st = w.snapshot(now)["aftershock"]
        self.assertEqual((st["mode"], st["catalog_events"], st["sources"]), ("raise", 1, 1))
        self.assertGreater(st["n_as_6"], 0)
        self.assertGreater(st["fairs"]["5.8"][1], st["fairs"]["5.8"][0])


class TestAskVerdict(unittest.TestCase):
    """QuakeWatch.ask_verdict: when a far-strike YES ask may rest, and how low
    (Jack 2026-10-11: "yes GFZ-triggered cancel", "yes start at the far
    strikes"). Pull-below 0.8, day-max gap 0.4, cool-off 30 min (defaults)."""

    def test_quiet_feeds_allow_an_ask_floored_at_the_fair(self):
        now = DAY + 12 * H
        w = _watch(now)
        v = w.ask_verdict(6.6, DAY, now)
        fair = q.p_yes(6.6, DAY, now, q.GFZ_LAG_H) * 100
        self.assertTrue(v["ok"])
        self.assertEqual(v["floor_c"], math.ceil(fair + q.ASK_MARGIN_CENTS))
        self.assertEqual(w.ask_verdict(6.6, DAY, now, margin_cents=3)["floor_c"],
                         math.ceil(fair + 3))

    def test_either_feed_stale_turns_the_ask_off(self):
        now = DAY + 12 * H
        w = _watch(now)
        self.assertEqual(w.ask_verdict(6.6, DAY, now + q.USGS_STALE_SECS + 1)["reason"],
                         "usgs_stale")
        # a stale GFZ only HOLDS the bids; it takes the asks off (it is the trigger)
        w = _watch(now, gfz_age=q.GFZ_STALE_SECS + 10)
        self.assertEqual(w.verdict(6.6, DAY, now)["action"], "hold")
        self.assertEqual(w.ask_verdict(6.6, DAY, now)["reason"], "gfz_stale")
        never = q.QuakeWatch()
        never.update_usgs(now - 30, [], now)
        self.assertEqual(never.ask_verdict(6.6, DAY, now)["reason"], "gfz_stale")

    def test_a_gfz_detection_near_the_strike_pulls_only_that_strike(self):
        now = DAY + 12 * H
        w = _watch(now, gfz=[gfz_ev("g1", 5.9, now - 60)], gfz_age=0)
        v = w.ask_verdict(6.6, DAY, now)                 # 5.9 >= 6.6 - 0.8
        self.assertEqual((v["ok"], v["reason"]), (False, "detection"))
        self.assertIsNone(v["floor_c"])
        self.assertTrue(w.ask_verdict(7.0, DAY, now)["ok"])   # 5.9 < 6.2
        w = _watch(now, gfz=[gfz_ev("g2", 5.7, now - 60)], gfz_age=0)
        self.assertTrue(w.ask_verdict(6.6, DAY, now)["ok"])   # too small for any

    def test_off_through_the_cooloff_then_back_once_neic_settles_low(self):
        now = DAY + 12 * H
        t0 = now - 120
        g = [gfz_ev("g1", 6.0, t0)]
        w = _watch(now, gfz=g, gfz_age=0)
        self.assertEqual(w.ask_verdict(6.6, DAY, now)["reason"], "detection")
        us = [usgs_ev("us1", 6.0, t0 + 3)]               # NEIC publishes 5 min later
        TestVerdict.poll(w, now + 300, us, gfz=g)
        self.assertEqual(w.ask_verdict(6.6, DAY, now + 300)["reason"], "detection")
        late = now + 300 + q.ASK_COOLOFF_MIN * 60 + 10   # cool-off over, NEIC settled
        TestVerdict.poll(w, late, us, gfz=g)
        v = w.ask_verdict(6.6, DAY, late)
        self.assertTrue(v["ok"], v)
        # ...but the same day's M6.0 sits within the 0.4 gap of a 6.4 strike
        self.assertEqual(w.ask_verdict(6.4, DAY, late)["reason"], "near_daymax")

    def test_an_unconfirmed_detection_stays_pending_until_it_is_old_news(self):
        now = DAY + 12 * H
        g = [gfz_ev("g1", 5.9, now - 60 * 60)]           # an hour old, no NEIC match
        w = _watch(now, gfz=g, gfz_age=0)
        self.assertEqual(w.ask_verdict(6.6, DAY, now)["reason"], "pending")
        old = [gfz_ev("g1", 5.9, now - (q.NEWS_MAX_AGE_MIN + 5) * 60)]
        w = _watch(now, gfz=old, gfz_age=0)
        self.assertTrue(w.ask_verdict(6.6, DAY, now)["ok"])

    def test_a_big_gfz_number_inside_the_day_keeps_the_ask_off(self):
        now = DAY + 12 * H
        w = _watch(now, gfz=[gfz_ev("g1", 6.3, now - H)], gfz_age=0)
        self.assertEqual(w.ask_verdict(6.6, DAY, now)["reason"], "gfz_near")
        self.assertEqual(w.ask_verdict(6.8, DAY, now)["reason"], "pending")  # 6.3 >= 6.0
        # yesterday's: not this day's maximum, and long past news
        now = DAY + 2 * H
        w = _watch(now, gfz=[gfz_ev("g1", 6.3, DAY - 2 * H)], gfz_age=0)
        self.assertTrue(w.ask_verdict(6.6, DAY, now)["ok"])

    def test_todays_usgs_max_within_the_gap_keeps_the_ask_off(self):
        now = DAY + 12 * H
        w = _watch(now, usgs=[settled_ev("us1", 6.2, DAY + 2 * H)])
        v = w.ask_verdict(6.6, DAY, now)
        self.assertEqual((v["reason"], v["mag"]), ("near_daymax", 6.2))
        self.assertTrue(w.ask_verdict(6.8, DAY, now)["ok"])   # settled, old, 6.2 < 6.4
        w = _watch(now, usgs=[settled_ev("us0", 6.9, DAY - 2 * H)])   # yesterday's
        self.assertTrue(w.ask_verdict(6.6, DAY, now)["ok"])

    def test_the_ask_outlives_the_bid_late_in_the_day(self):
        now = DAY + 20 * H
        w = _watch(now)
        self.assertEqual(w.verdict(7.0, DAY, now)["reason"], "fair_floor")
        v = w.ask_verdict(7.0, DAY, now)
        self.assertTrue(v["ok"])
        self.assertEqual(v["floor_c"], 2)                 # ~0.6c fair + 1c, rounded up


def emsc_msg(unid, mag, t, action="create", region="SOMEWHERE"):
    """One message as EMSC's realtime websocket pushes it."""
    iso = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-5] + "Z"
    return json.dumps({"action": action, "data": {
        "type": "Feature", "id": unid,
        "geometry": {"type": "Point", "coordinates": [140.1, 35.2, -10.0]},
        "properties": {"source_id": "1", "source_catalog": "EMSC-RTS",
                       "lastupdate": iso, "time": iso, "flynn_region": region,
                       "lat": 35.2, "lon": 140.1, "depth": 10.0, "evtype": "ke",
                       "auth": "EMSC", "mag": mag, "magtype": "mb", "unid": unid}}})


class TestEmscFeed(unittest.TestCase):
    """EMSC's realtime push feed, the far-strike asks' third trigger (Jack
    2026-10-11: "poll GFZ every 10s instead of 20s, or add the faster EMSC
    feed" -> "implement"): parsed, kept from EMSC_MIN_MAG, and counted in
    ask_verdict like GFZ -- never in the bids' freeze."""

    def test_parse(self):
        now = DAY + 12 * H
        action, ev = q.parse_emsc(emsc_msg("20261011_0000123", 6.1, now - 90.5))
        self.assertEqual(action, "create")
        self.assertEqual((ev["id"], ev["mag"], ev["mag_type"], ev["place"]),
                         ("20261011_0000123", 6.1, "mb", "SOMEWHERE"))
        self.assertAlmostEqual(ev["time"], now - 90.5, places=1)
        self.assertIsNone(q.parse_emsc("not json"))
        self.assertIsNone(q.parse_emsc(json.dumps({"action": "create"})))
        self.assertIsNone(q.parse_emsc(emsc_msg("x", "nan", now)))
        bad = json.loads(emsc_msg("x", 5.0, now))
        bad["data"]["properties"]["time"] = ""
        self.assertIsNone(q.parse_emsc(json.dumps(bad)))

    def test_kept_from_the_floor_revised_in_place_and_pruned(self):
        now = DAY + 12 * H
        w = _watch(now)
        self.assertFalse(w.update_emsc(q.parse_emsc(emsc_msg("e0", 3.2, now))[1], now))
        self.assertTrue(w.update_emsc(q.parse_emsc(emsc_msg("e1", 6.0, now - 60))[1], now))
        self.assertEqual(w.emsc_ok, now)
        # a revision (even under the floor) replaces it, first sighting kept
        later = now + 120
        self.assertTrue(w.update_emsc(
            q.parse_emsc(emsc_msg("e1", 4.5, now - 60, action="update"))[1], later))
        self.assertEqual((w.emsc["e1"]["mag"], w.emsc["e1"]["first_seen"]), (4.5, now))
        gone = now - 60 + q.EMSC_KEEP_MIN * 60 + 1
        w.update_emsc(q.parse_emsc(emsc_msg("e2", 5.0, gone - 10))[1], gone)
        self.assertNotIn("e1", w.emsc)
        self.assertIn("e2", w.emsc)

    def test_an_emsc_quake_pulls_the_ask_like_gfz_and_never_holds_bids(self):
        now = DAY + 12 * H
        w = _watch(now)
        w.update_emsc(q.parse_emsc(emsc_msg("e1", 5.9, now - 60))[1], now)
        v = w.ask_verdict(6.6, DAY, now)
        self.assertEqual((v["ok"], v["reason"], v["src"]), (False, "detection", "emsc"))
        self.assertTrue(w.ask_verdict(7.0, DAY, now)["ok"])          # 5.9 < 6.2
        self.assertEqual(w.verdict(6.6, DAY, now)["action"], "quote")  # bids untouched
        # revised down under the pull line: the ask is back at once
        w.update_emsc(q.parse_emsc(emsc_msg("e1", 5.5, now - 60, "update"))[1], now + 30)
        self.assertTrue(w.ask_verdict(6.6, DAY, now + 30)["ok"])

    def test_an_emsc_number_near_the_strike_inside_the_day(self):
        now = DAY + 12 * H
        w = _watch(now)
        w.update_emsc(q.parse_emsc(emsc_msg("e1", 6.3, now - H))[1], now)
        v = w.ask_verdict(6.6, DAY, now)
        self.assertEqual((v["reason"], v["src"]), ("emsc_near", "emsc"))
        self.assertEqual(w.ask_verdict(6.8, DAY, now)["reason"], "pending")   # no NEIC match

    def test_status_reports_emsc(self):
        now = DAY + 12 * H
        w = _watch(now)
        w.update_emsc(q.parse_emsc(emsc_msg("e1", 5.6, now - 60))[1], now)
        snap = w.snapshot(now + 5)
        self.assertEqual((snap["emsc_events"], snap["emsc_max"], snap["emsc_ok_age_s"]),
                         (1, 5.6, 5))
        self.assertIn("emsc_min_mag", snap["knobs"])


if __name__ == "__main__":
    unittest.main()
