"""Tests for resort_snow_fair.py (ski-resort season snowfall fair values for
incentive_mm's resort snow gate, Jack 2026-10-22 "Do it best you can and
quote it"). No network: the feeds are mocked."""

import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytz

import rain_monthly_fair as rmf
import resort_snow_fair as rsf

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)       # 14:00 MDT
RULES = ("If the total snowfall recorded at {place} October 1, 2026 through December "
         "24, 2026 (value recorded at 4:00 PM ET on December 24) is above {k} inches, "
         "then the market resolves to Yes.")
EV = "KXBEAVERSNOW-24DEC26"


def market(k, place="Beaver Creek Resort", ev=EV, bid="0.5700", ask="0.5800"):
    return {"event_ticker": ev, "ticker": f"{ev}-GT{k}", "floor_strike": k,
            "strike_type": "greater", "rules_primary": RULES.format(place=place, k=k),
            "yes_bid_dollars": bid, "yes_ask_dollars": ask}


def history(daily=0.3, end=date(2026, 10, 20)):
    """Fifteen complete proxy seasons at `daily` inches a day."""
    out, d = {}, date(2011, 7, 1)
    while d <= end:
        out[d.isoformat()] = daily
        d += timedelta(days=1)
    return out


class TestRulesAndEvents(unittest.TestCase):

    def test_parse_rules(self):
        self.assertEqual(rsf.parse_rules(RULES.format(place="Beaver Creek Resort", k=50)),
                         ("Beaver Creek Resort", date(2026, 10, 1), date(2026, 12, 24)))
        self.assertIsNone(rsf.parse_rules("If the high temperature ..."))

    def test_book_cents(self):
        self.assertEqual(rsf.book_cents({"yes_bid_dollars": "0.5700",
                                         "yes_ask_dollars": "0.5800"}), [57, 58])
        self.assertEqual(rsf.book_cents({"yes_bid": 12, "yes_ask": 15}), [12, 15])
        for bid, ask in (("0.0000", "0.5800"), ("0.5700", "1.0000"), ("0.6000", "0.5800")):
            self.assertIsNone(rsf.book_cents({"yes_bid_dollars": bid, "yes_ask_dollars": ask}))
        self.assertIsNone(rsf.book_cents({}))

    def test_fetch_events_reads_strikes_books_and_the_resort(self):
        def reader(path, params):
            s = params["series_ticker"]
            if s == "KXBEAVERSNOW":
                return {"markets": [market(80), market(90, bid="0.0000")]}
            if s == "KXKEYSTONESNOW":         # rules naming another resort: no code
                return {"markets": [market(60, place="Vail", ev="KXKEYSTONESNOW-24DEC26")]}
            return {}
        with mock.patch.object(rsf.time, "sleep"):
            ev = rsf.fetch_events(reader, series=("KXBEAVERSNOW", "KXKEYSTONESNOW"))
        b = ev[EV]
        self.assertEqual((b["code"], b["start"], b["end"]),
                         ("KXBEAVERSNOW", "2026-10-01", "2026-12-24"))
        self.assertEqual(b["strikes"], {f"{EV}-GT80": 80.0, f"{EV}-GT90": 90.0})
        self.assertEqual(b["books"], {f"{EV}-GT80": [57, 58]})     # GT90 one-sided
        self.assertIsNone(ev["KXKEYSTONESNOW-24DEC26"]["code"])


class TestToDate(unittest.TestCase):

    TZ = pytz.timezone("US/Mountain")

    def test_counts_todays_report_once_in_and_misses_only_past_days(self):
        h = {"2026-10-20": 1.0, "2026-10-22": 2.5}
        self.assertEqual(rsf.proxy_to_date(h, date(2026, 10, 20), NOW, self.TZ),
                         (3.5, "2026-10-22", 1))                  # 10/21 missing
        h = {"2026-10-20": 1.0, "2026-10-21": None}
        self.assertEqual(rsf.proxy_to_date(h, date(2026, 10, 20), NOW, self.TZ),
                         (1.0, "2026-10-20", 1))                  # today not yet in


class TestEventFair(unittest.TestCase):
    """event_fair's to-date, scale and stale rules on a mocked proxy."""

    INFO = {"series": "KXBEAVERSNOW", "code": "KXBEAVERSNOW", "start": "2026-10-01",
            "end": "2026-12-24", "strikes": {f"{EV}-GT80": 80.0, f"{EV}-GT5": 5.0}}

    def fair(self, recent=None, last_wet_at=None, now=NOW, hist=None):
        sim = {"totals": [40.0] * 50 + [70.0] * 50, "n_segments": 15, "horizon_days": 7}
        recent = {} if recent is None else recent
        with mock.patch.object(rmf, "period_history", return_value=hist or history()), \
                mock.patch.object(rsf, "recent_reports",
                                  side_effect=recent if isinstance(recent, Exception)
                                  else lambda *a: recent), \
                mock.patch.object(rmf, "simulate_period", return_value=sim) as sp:
            f = rsf.event_fair(EV, dict(self.INFO, last_wet_at=last_wet_at), now)
        self.sim_kw = sp.call_args.kwargs if sp.call_args else None
        return f

    def test_scale_to_date_and_prices(self):
        f = self.fair(recent={"2026-10-21": 0.5, "2026-10-22": None})
        # proxy season ~0.3 x 365.25 = 109.6"; 226 / that x level 1.86 ~ 3.84
        scale = self.sim_kw["hist_scale"]
        self.assertAlmostEqual(scale, 226.0 / (0.3 * 365.25) * 1.86, places=2)
        self.assertEqual(f["scale"], round(scale, 3))
        # 20 days x 0.3 through 10/20, then 10/21's 0.5 from the recent read
        self.assertAlmostEqual(f["mtd"], 6.5 * scale, places=1)          # ~25"
        self.assertEqual((f["cli_date"], f["stale_cli"]), ("2026-10-21", False))
        self.assertEqual(f["p"][f"{EV}-GT5"], 1.0)
        self.assertEqual(f["p"][f"{EV}-GT80"], 0.5)        # needs 55" more: the 70s

    def test_precipitation_after_the_reports_morning_is_stale(self):
        # rain at EGE 10/21 06:30 MDT, before the 10/21 report's 07:00: counted
        f = self.fair(recent={"2026-10-21": 0.0},
                      last_wet_at="2026-10-21T12:30:00+00:00")
        self.assertFalse(f["stale_cli"])
        # ... at 08:00 MDT: the next report (10/22) has to count it
        f = self.fair(recent={"2026-10-21": 0.0},
                      last_wet_at="2026-10-21T14:00:00+00:00")
        self.assertTrue(f["stale_cli"])
        self.assertIn("after DILLON 1 E's latest report (2026-10-21", f["stale_why"])
        f = self.fair(recent={"2026-10-21": 0.0, "2026-10-22": 1.2},
                      last_wet_at="2026-10-21T14:00:00+00:00")
        self.assertFalse(f["stale_cli"])
        # rain right now, after this morning's report: stale until tomorrow's
        f = self.fair(recent={"2026-10-22": 0.0}, last_wet_at="2026-10-22T19:50:00+00:00")
        self.assertTrue(f["stale_cli"])

    def test_no_report_for_three_days_or_ever_is_stale(self):
        f = self.fair(hist=history(end=date(2026, 10, 18)))
        self.assertTrue(f["stale_cli"])
        self.assertIn("4 days old", f["stale_why"])
        f = self.fair(recent=RuntimeError("acis down"))           # daily history only
        self.assertFalse(f["stale_cli"])
        self.assertIn("recent reports unreadable", " ".join(f["notes"]))
        with mock.patch.object(rmf, "period_history",
                               return_value={k: v for k, v in history().items()
                                             if k < "2026-10-01"}), \
                mock.patch.object(rsf, "recent_reports", return_value={}), \
                mock.patch.object(rmf, "simulate_period",
                                  return_value={"totals": [1.0], "n_segments": 1,
                                                "horizon_days": 0}):
            f = rsf.event_fair(EV, dict(self.INFO), NOW)
        self.assertEqual((f["stale_cli"], f["cli_date"]), (True, None))

    def test_unknown_resort_or_window_over(self):
        self.assertIsNone(rsf.event_fair(EV, dict(self.INFO, code=None), NOW))
        self.assertIsNone(rsf.event_fair(EV, dict(self.INFO),
                                         datetime(2026, 12, 26, tzinfo=timezone.utc)))


class TestAnchor(unittest.TestCase):

    S = {"A-GT40": 40.0, "A-GT60": 60.0, "A-GT80": 80.0}
    P = {"A-GT40": 0.80, "A-GT60": 0.50, "A-GT80": 0.30}
    T0 = 1_000_000.0

    def test_first_calm_read_is_the_mid_then_a_24h_follow(self):
        books = {"A-GT40": [69, 71], "A-GT60": [49, 51], "A-GT80": [9, 11]}
        offs, keep = rsf.anchor_offsets(self.S, self.P, books, {}, self.T0, calm=True)
        for t, mid in (("A-GT40", 0.70), ("A-GT60", 0.50), ("A-GT80", 0.10)):
            self.assertAlmostEqual(rsf._sigmoid(rsf._logit(self.P[t]) + offs[t]), mid, 5)
        # an hour later the book jumps to 70c at GT60: 1/24 of the way, ~1c
        books2 = {"A-GT60": [69, 71]}
        offs2, keep2 = rsf.anchor_offsets(self.S, self.P, books2, keep, self.T0 + 3600, True)
        target = rsf._logit(0.70) - rsf._logit(0.50)
        self.assertAlmostEqual(offs2["A-GT60"], (1 - math.exp(-1 / 24)) * target, 3)
        self.assertLess(rsf._sigmoid(offs2["A-GT60"]), 0.52)
        self.assertEqual(keep2["A-GT40"], keep["A-GT40"])         # no book: kept
        # a day of silence steps at most ANCHOR_MAX_STEP_HOURS
        offs3, _ = rsf.anchor_offsets(self.S, self.P, books2, keep, self.T0 + 86400, True)
        self.assertAlmostEqual(offs3["A-GT60"], (1 - math.exp(-2 / 24)) * target, 3)

    def test_frozen_unless_calm_and_wide_books_do_not_anchor(self):
        books = {"A-GT60": [49, 51]}
        offs, keep = rsf.anchor_offsets(self.S, self.P, books, {}, self.T0, calm=False)
        self.assertEqual((offs, keep), ({}, {}))                  # no anchor, no fair
        offs, keep = rsf.anchor_offsets(self.S, self.P, {"A-GT60": [40, 60]}, {},
                                        self.T0, calm=True)
        self.assertEqual(keep, {})                                # 20c wide
        old = {"A-GT60": {"off": 0.4, "ts": self.T0}}
        offs, keep = rsf.anchor_offsets(self.S, self.P, {"A-GT60": [80, 82]}, old,
                                        self.T0 + 3600, calm=False)
        self.assertEqual(keep, old)

    def test_strikes_without_a_book_borrow_their_neighbours(self):
        old = {"A-GT40": {"off": 0.2, "ts": self.T0}, "A-GT80": {"off": -0.6, "ts": self.T0}}
        offs, _ = rsf.anchor_offsets(self.S, self.P, {}, old, self.T0, calm=True)
        self.assertAlmostEqual(offs["A-GT60"], -0.2, 9)           # halfway
        offs, _ = rsf.anchor_offsets(dict(self.S, **{"A-GT99": 99.0}),
                                     dict(self.P, **{"A-GT99": 0.1}),
                                     {}, {"A-GT60": {"off": 0.5, "ts": self.T0}},
                                     self.T0, calm=True)
        self.assertEqual({t: offs[t] for t in sorted(offs)},
                         {"A-GT40": 0.5, "A-GT60": 0.5, "A-GT80": 0.5, "A-GT99": 0.5})


class TestWriter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rsf_")
        self.path = os.path.join(self.tmp, "resort.json")
        self.p = {f"{EV}-GT80": 0.40, f"{EV}-GT90": 0.30}
        self.events = {EV: {"series": "KXBEAVERSNOW", "code": "KXBEAVERSNOW",
                            "start": "2026-10-01", "end": "2026-12-24",
                            "strikes": {f"{EV}-GT80": 80.0, f"{EV}-GT90": 90.0},
                            "books": {f"{EV}-GT80": [57, 58]}},
                       "KXVAILSNOW-24DEC26": {"series": "KXVAILSNOW", "code": None,
                                              "strikes": {"KXVAILSNOW-24DEC26-GT9": 9.0}}}
        self.stale = False

    def fair(self, ev, info, now):
        return {"p": dict(self.p), "boundary": [], "mtd": 3.0, "cli_mtd": None,
                "cli_date": "2026-10-21", "stale_cli": self.stale, "stale_why": None,
                "notes": [], "horizon_days": 7, "n_segments": 15, "in_period": True,
                "scale": 3.4, "proxy_avg": 123.5, "scale_seasons": 14,
                "median_total": 84.0}

    @staticmethod
    def obs(wet):
        return lambda spec, now: {"wet": wet, "obs_time": now.isoformat(), "phour": 0.0,
                                  "wx": "-RA" if wet else ""}

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_anchored_on_a_calm_read_frozen_through_rain(self):
        # raining at the airport on the first read: no anchor, no fair
        self.assertEqual(rsf.write_fair_file(self.path, now=NOW, events=self.events,
                                             fair_fn=self.fair, obs_fn=self.obs(True)),
                         (0, 2))
        d = self._read()
        self.assertEqual(d["markets"], {})
        self.assertEqual(d["event_state"][EV]["error"], "no anchor yet")
        self.assertEqual(d["event_state"]["KXVAILSNOW-24DEC26"]["error"], "no resort")
        # dry, but 30 minutes after the rain: still no anchor
        later = NOW + timedelta(minutes=30)
        rsf.write_fair_file(self.path, now=later, events=self.events, fair_fn=self.fair,
                            obs_fn=self.obs(False))
        self.assertEqual(self._read()["markets"], {})
        # an hour after: GT80 anchors at its 57.5 mid, GT90 (no book) borrows it
        later = NOW + timedelta(minutes=61)
        self.assertEqual(rsf.write_fair_file(self.path, now=later, events=self.events,
                                             fair_fn=self.fair, obs_fn=self.obs(False)),
                         (1, 1))
        d = self._read()
        m80, m90 = d["markets"][f"{EV}-GT80"], d["markets"][f"{EV}-GT90"]
        self.assertAlmostEqual(m80["p"], 0.575, 4)
        self.assertEqual((m80["p_model"], m80["anchored"], m90["anchored"]),
                         (0.40, True, False))
        off = rsf._logit(0.575) - rsf._logit(0.40)
        self.assertAlmostEqual(m90["p"], rsf._sigmoid(rsf._logit(0.30) + off), 4)
        self.assertEqual(d["event_state"][EV]["anchored"], 1)
        # rain again and the book runs to 80c: the anchor holds, the model moves
        self.p[f"{EV}-GT80"] = 0.45
        self.events[EV]["books"] = {f"{EV}-GT80": [79, 81]}
        rsf.write_fair_file(self.path, now=later + timedelta(hours=1), events=self.events,
                            fair_fn=self.fair, obs_fn=self.obs(True))
        m80 = self._read()["markets"][f"{EV}-GT80"]
        self.assertAlmostEqual(m80["p"], round(rsf._sigmoid(rsf._logit(0.45) + off), 4), 4)
        # stale (no rain now): still frozen
        self.stale = True
        rsf.write_fair_file(self.path, now=later + timedelta(hours=3), events=self.events,
                            fair_fn=self.fair, obs_fn=self.obs(False))
        self.assertAlmostEqual(self._read()["anchors"][f"{EV}-GT80"]["off"], round(off, 4), 4)

    def test_cached_events_never_step_and_a_failed_fair_keeps_its_anchors(self):
        calls = []

        def reader(path, params):
            calls.append(params["series_ticker"])
            return {"markets": [market(80)] if params["series_ticker"] == "KXBEAVERSNOW"
                    else []}
        kw = dict(fair_fn=self.fair, obs_fn=self.obs(False), get_json=reader)
        with mock.patch.object(rsf, "SERIES", ("KXBEAVERSNOW",)), \
                mock.patch.object(rsf.time, "sleep"):
            rsf.write_fair_file(self.path, now=NOW, **kw)
            a0 = self._read()["anchors"][f"{EV}-GT80"]
            self.assertEqual((a0["mid"], len(calls)), (57.5, 1))
            # within the hour the events (and their books) come from the file
            rsf.write_fair_file(self.path, now=NOW + timedelta(minutes=30), **kw)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self._read()["anchors"][f"{EV}-GT80"], a0)
            # the fair fails: no markets, the anchor waits
            rsf.write_fair_file(self.path, now=NOW + timedelta(minutes=40),
                                fair_fn=lambda *a: None, obs_fn=self.obs(False),
                                get_json=reader)
            d = self._read()
            self.assertEqual((d["markets"], d["anchors"][f"{EV}-GT80"]), ({}, a0))
            # the hour: a fresh read steps it
            rsf.write_fair_file(self.path, now=NOW + timedelta(minutes=61), **kw)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self._read()["anchors"][f"{EV}-GT80"]["ts"],
                             (NOW + timedelta(minutes=61)).timestamp())


if __name__ == "__main__":
    unittest.main()
