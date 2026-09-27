"""Tests for usgs_quake_fair (the KXBIGGESTQUAKE bid-only gate's feeds)."""
import json
import math
import os
import tempfile
import unittest
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
            {"id": "ak3", "properties": {"mag": 4.9, "net": "AK", "time": 1790491000000}}]}
        gen, evs = q.parse_usgs(payload)
        self.assertEqual(gen, 1790500000.0)
        self.assertEqual([e["id"] for e in evs], ["us1", "ak3"])
        self.assertEqual(evs[1]["net"], "ak")
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


if __name__ == "__main__":
    unittest.main()
