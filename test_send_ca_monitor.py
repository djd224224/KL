#!/usr/bin/env python3
"""Unit tests for send_ca_monitor.py -- the daily Carbon Arc email (Jack
2026-10-11: "i want careful monitoring on carbon arc").

Run: python -m unittest test_send_ca_monitor

Worth testing: the late-month window it judges against (Oct 18 -> Oct 27),
the verdict rules (a blocked-side fill or order, a read that moved with no
new data, the kept side running under its benchmark, the market's makers
doing better on the blocked side), the read-anomaly detector on the 10/10
point-of-sale hole, and the fill filters (pads, takers, landing windows).
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_ca_monitor as cm                                        # noqa: E402
import incentive_mm as imm                                          # noqa: E402


def setUpModule():
    """Sandbox file paths away from the live run-logs; no test may SMTP."""
    imm.STATUS_DIR = tempfile.mkdtemp(prefix="imm_ca_monitor_test_")
    imm.ALERT_RECIPIENTS = []


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def _fam(ticker):
    s = ticker.split("-")[0]
    for suf, cat in (("POS", "point of sale"), ("CC", "credit card"), ("APP", "app"),
                     ("FT", "foot traffic"), ("ADS", "advertising")):
        if s.endswith(suf):
            return cat
    return None


class TestWindow(unittest.TestCase):

    def test_october_window_is_the_18th_to_the_27th(self):
        ws, stop = cm.late_window(date(2026, 10, 10))
        self.assertEqual(ws.astimezone(imm.ET).strftime("%m-%d %H:%M"), "10-18 00:00")
        self.assertEqual(stop.astimezone(imm.ET).strftime("%m-%d %H:%M"), "10-27 00:00")
        ws, _stop = cm.late_window(date(2026, 12, 3))          # the year rolls
        self.assertEqual(ws.astimezone(imm.ET).strftime("%Y-%m-%d"), "2026-12-18")

    def test_blocked_side_follows_number_3(self):
        self.assertEqual([cm.blocked_side(c) for c, _t, _n in cm.FAMILIES],
                         ["ask", "ask", "ask", "bid", "bid"])


class TestVerdict(unittest.TestCase):

    def ctx(self, cat="point of sale", **kw):
        f = {"cat": cat, "money": {"net": 10.0, "net3": 5.0, "days7": 7},
             "read": {"n": 18, "anomalies": [], "refresh_h": 5.0, "data_d": 1.5},
             "gate": {}, "landing": {}, "selected": 100}
        f.update(kw)
        return f

    def test_a_quiet_day_is_ok(self):
        self.assertEqual(cm.verdict(self.ctx()), ("OK", []))

    def test_problems(self):
        lvl, why = cm.verdict(self.ctx(late_viol=[2, 0]))
        self.assertEqual(lvl, "PROBLEM")
        self.assertIn("BLOCKED ask side", why[0])
        lvl, why = cm.verdict(self.ctx(read={"n": 18, "anomalies": ["18 series at 03:00 ET"]}))
        self.assertEqual(lvl, "PROBLEM")
        self.assertEqual(cm.verdict(self.ctx(money={"net": -151.0}))[0], "PROBLEM")
        self.assertEqual(cm.verdict(self.ctx(read={"n": 0}))[0], "PROBLEM")

    def test_watches(self):
        self.assertEqual(cm.verdict(self.ctx(money={"net": -60.0}))[0], "WATCH")
        self.assertEqual(cm.verdict(self.ctx(money={"net": 5.0, "net3": -1.0, "days7": 3}))[0],
                         "WATCH")
        # point of sale refreshes daily: 31h with no new data is stale
        self.assertEqual(cm.verdict(self.ctx(read={"n": 18, "refresh_h": 31.0}))[0], "WATCH")
        # ...card spend skips weekends, so 31h is normal
        self.assertEqual(cm.verdict(self.ctx("credit card", read={"n": 31, "refresh_h": 31.0}))[0],
                         "OK")
        self.assertEqual(cm.verdict(self.ctx(gate={"storm_frac": 0.74, "storm_side": "bid",
                                                   "storm_at": "x"}))[0], "WATCH")
        self.assertEqual(cm.verdict(self.ctx(landing={"bid": [3, 300, -45.0, 0.0]}))[0], "WATCH")

    def test_the_kept_side_is_held_to_its_benchmark_in_the_window(self):
        # point of sale keeps BIDS; Aug/Sep makers made +4.2c..+14.8c on them
        fine = {"bid": [10, 400, -20.0, 0.0]}                       # -5c/ct: within 10c
        self.assertEqual(cm.verdict(self.ctx(sides_window=fine))[0], "OK")
        bad = {"bid": [10, 400, -40.0, 0.0]}                        # -10c/ct: 14c under
        lvl, why = cm.verdict(self.ctx(sides_window=bad))
        self.assertEqual(lvl, "WATCH")
        self.assertIn("the bids we still quote", why[0])
        thin = {"bid": [2, 100, -40.0, 0.0]}                        # too few contracts
        self.assertEqual(cm.verdict(self.ctx(sides_window=thin))[0], "OK")

    def test_market_makers_disagreeing_with_number_3(self):
        # foot traffic blocks BIDS; makers losing on asks instead = WATCH
        agree = {"bid": [5000, -1500.0], "ask": [5000, 900.0]}
        self.assertEqual(cm.verdict(self.ctx("foot traffic", market_window=agree))[0], "OK")
        disagree = {"bid": [5000, 300.0], "ask": [5000, -900.0]}
        self.assertEqual(cm.verdict(self.ctx("foot traffic", market_window=disagree))[0], "WATCH")
        thin = {"bid": [500, 300.0], "ask": [500, -900.0]}
        self.assertEqual(cm.verdict(self.ctx("foot traffic", market_window=thin))[0], "OK")


class TestReadHealth(unittest.TestCase):

    def test_the_10_10_point_of_sale_hole_is_caught(self):
        def v(at, refreshed, mu, thru):
            return {"series": "KXSPRITEPOS", "category": "Point of Sale", "month": "2026-10",
                    "fetched_at": at, "refreshed_at": refreshed, "mu": mu, "data_through": thru}
        vint = [v("2026-10-10T03:26:00+00:00", "2026-10-09T11:58:00+00:00", 91.64, "2026-10-08"),
                v("2026-10-10T07:00:00+00:00", "2026-10-09T11:58:00+00:00", 81.83, "2026-10-09"),
                v("2026-10-10T11:55:00+00:00", "2026-10-10T11:54:00+00:00", 93.56, "2026-10-09")]
        fair = {"entries": {"KXSPRITEPOS": {"category": "Point of Sale", "data_through": "2026-10-09",
                                            "refreshed_at": "2026-10-10T11:54:00+00:00"}}}
        a, b = cm.day_window(date(2026, 10, 10))
        now = datetime(2026, 10, 11, 11, 35, tzinfo=timezone.utc)
        with mock.patch.object(cm, "family_of", lambda s: _fam(s)):
            out = cm.read_health(fair, "point of sale", now, a, b, vint)
        self.assertEqual(out["changes"], 2)
        self.assertEqual(len(out["anomalies"]), 1)               # 03:00 ET, not the refresh
        self.assertIn("1 series at 03:00 ET", out["anomalies"][0])
        self.assertAlmostEqual(out["refresh_h"], 23.7, places=1)


class TestFills(unittest.TestCase):

    def test_side_stats_leaves_out_pads_takers_and_other_families(self):
        t0 = _ts(2026, 10, 10, 12)
        fills = [{"ts": t0, "t": "KXFAKEPOS-26NOV03-T90", "side": "bid", "n": 100, "mtc": -10.0, "mk": -5.0},
                 {"ts": t0, "t": "KXFAKEPOS-26NOV03-T90", "side": "ask", "n": 50, "mtc": 4.0, "mk": 1.0},
                 {"ts": t0, "t": "KXFAKEPOS-26NOV03-T90", "side": "bid", "n": 5, "mtc": -99, "pad": True},
                 {"ts": t0, "t": "KXFAKEPOS-26NOV03-T90", "side": "bid", "n": 5, "mtc": -99, "taker": True},
                 {"ts": t0, "t": "KXGOOD-26NOV03-T90", "side": "bid", "n": 5, "mtc": -99},
                 {"ts": t0 + 86400, "t": "KXFAKEPOS-26NOV03-T90", "side": "bid", "n": 5, "mtc": -99}]
        out = cm.side_stats(fills, _fam, t0 - 1, t0 + 3600)
        self.assertEqual(out["point of sale"]["bid"][:3], [1, 100.0, -10.0])
        self.assertEqual(out["point of sale"]["ask"][:3], [1, 50.0, 2.0])
        self.assertEqual(cm.cpc(out["point of sale"]["bid"]), "-10.0c")
        # 12:00Z = 08:00 ET: inside point of sale's 07-10 landing window
        inside = cm.side_stats(fills, _fam, t0 - 1, t0 + 3600,
                               windows={"point of sale": [(7, 10)]})
        self.assertEqual(inside["point of sale"]["bid"][1], 100.0)
        outside = cm.side_stats(fills, _fam, t0 - 1, t0 + 3600,
                                windows={"point of sale": [(11, 14)]})
        self.assertEqual(outside["point of sale"]["bid"][1], 0.0)

    def test_late_violations_count_only_the_blocked_side_inside_the_window(self):
        ver = {s: {"carbon_arc": True, "ts": 1.0, "title": ""} for s in ("KXFAKEPOS", "KXFAKEFT")}
        with mock.patch.dict(imm.FAMILY_VERDICTS, ver):
            t_in = _ts(2026, 10, 19, 15)                           # inside Oct 18 -> 27
            t_before = _ts(2026, 10, 17, 15)
            fills = [{"ts": t_in, "t": "KXFAKEPOS-26NOV03-T90", "side": "ask", "n": 10},   # blocked
                     {"ts": t_in, "t": "KXFAKEPOS-26NOV03-T90", "side": "bid", "n": 10},   # kept
                     {"ts": t_before, "t": "KXFAKEPOS-26NOV03-T90", "side": "ask", "n": 10},
                     {"ts": t_in, "t": "KXFAKEFT-26NOV08-T90", "side": "bid", "n": 10},    # blocked
                     {"ts": t_in, "t": "KXFAKEFT-26NOV08-T90", "side": "bid", "n": 1, "pad": True}]
            orders = [{"kind": "place", "ticker": "KXFAKEPOS-26NOV03-T95", "book_side": "ask",
                       "is_pad": False, "ts": "2026-10-19T15:00:00+00:00"},
                      {"kind": "cancel", "ticker": "KXFAKEPOS-26NOV03-T95", "book_side": "ask",
                       "is_pad": False, "ts": "2026-10-19T15:00:00+00:00"},
                      {"kind": "amend", "ticker": "KXFAKEFT-26NOV08-T95", "book_side": "ask",
                       "is_pad": False, "ts": "2026-10-19T15:00:00+00:00"}]
            out = cm.late_violations(fills, orders, _fam, _ts(2026, 10, 17, 0), _ts(2026, 10, 20, 0))
        self.assertEqual(out["point of sale"], [1, 1])
        self.assertEqual(out["foot traffic"], [1, 0])


class TestSimpleEmail(unittest.TestCase):
    """The simplified email (Jack 2026-10-11: "simplify the carbon arc email
    so it's easier to read"): a total, one row per family, reasons only for
    the families that aren't OK, no per-side / gate / read numbers."""

    def ctx(self):
        def fam(name, cat, level, net, reasons=()):
            return {"name": name, "tag": name[:3].upper(), "cat": cat, "level": level,
                    "reasons": list(reasons),
                    "money": {"rew": 10.0, "own": net - 10.0, "carried": 0.0, "net": net,
                              "net7": 2 * net},
                    "sides": {"bid": [2, 300, -20.0, 0.0], "ask": [1, 100, 5.0, 0.0]},
                    "book": {"markets": 4, "unreal": -12.0}}
        ws, stop = cm.late_window(date(2026, 10, 10))
        return {"day": date(2026, 10, 10), "now": datetime(2026, 10, 11, 11, 35, tzinfo=timezone.utc),
                "late": (ws, stop), "late_active": False, "notes": [],
                "families": [fam("Point of sale", "point of sale", "PROBLEM", 21.0,
                                 ["Our Carbon Arc read changed with no new data: x"]),
                             fam("Card spend", "credit card", "OK", 8.0),
                             fam("Apps", "app", "WATCH", 38.0, ["Down $115 over the last 3 days"]),
                             fam("Ad spend", "advertising", "PROBLEM", -363.0,
                                 ["Lost $363 on the day"])]}

    def test_subject_names_the_flagged_families_worst_first(self):
        self.assertEqual(cm.subject_for(self.ctx()),
                         "IMM Carbon Arc 10/10: net -$296 | PROBLEM Ad spend, Point of sale"
                         " | WATCH Apps")

    def test_body_lists_reasons_only_for_flagged_families(self):
        text = cm.render_text(self.ctx())
        self.assertIn("Net -$296 on the day", text)
        self.assertIn("Late-month rule (#3) starts Sun 10/18 (in 7 days)", text)
        look = text.split("NEEDS A LOOK")[1]
        self.assertLess(look.index("Ad spend -- PROBLEM"), look.index("Point of sale -- PROBLEM"))
        self.assertNotIn("Card spend", look)
        self.assertIn("Our fills: 3 (400 contracts, -$15). Open positions: 4 markets, -$12 "
                      "unrealized.", look)
        for noise in ("gate:", "stand-asides", "Aug / Sep", "c/ct"):
            self.assertNotIn(noise, text)
        html = cm.render_html(self.ctx())
        self.assertIn("Needs a look", html)
        self.assertNotIn("<pre", html)

    def test_a_clean_day_says_so(self):
        c = self.ctx()
        for f in c["families"]:
            f["level"], f["reasons"] = "OK", []
        self.assertTrue(cm.subject_for(c).endswith("| all OK"))
        self.assertIn("Every family performed as expected.", cm.render_text(c))


if __name__ == "__main__":
    unittest.main()
