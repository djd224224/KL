"""Tests for gasbuddy_fair.py (the AAA state-gas fair values behind the IMM's
GasBuddy gate). No network: every fetch is injected or mocked."""
import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import gasbuddy_fair as gb


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class TestHelpers(unittest.TestCase):
    def test_event_ticker(self):
        self.assertEqual(gb.event_ticker("KXAAAGASDCA", date(2026, 9, 28)),
                         "KXAAAGASDCA-26SEP28")
        self.assertEqual(gb.event_ticker("KXAAAGASDTX", date(2027, 1, 5)),
                         "KXAAAGASDTX-27JAN05")

    def test_live_updated_is_et_wall_clock(self):
        self.assertEqual(gb.live_updated_utc({"updated": "2026-09-27T10:45:00.63"}),
                         utc(2026, 9, 27, 14, 45))                 # EDT
        self.assertEqual(gb.live_updated_utc({"updated": "2026-12-01T10:45:00"}),
                         utc(2026, 12, 1, 15, 45))                 # EST
        self.assertIsNone(gb.live_updated_utc({"updated": None}))

    def test_remain_sd_is_linear_in_the_et_hour(self):
        sd = gb.DAY_MOVE_SD[3]
        self.assertAlmostEqual(gb.remain_sd(3, 0.0), sd)
        self.assertAlmostEqual(gb.remain_sd(3, 12.0), sd / 2)
        self.assertEqual(gb.remain_sd(3, 24.0), 0.0)
        self.assertEqual(gb.remain_sd(3, 30.0), 0.0)
        self.assertAlmostEqual(gb.remain_sd(9, 0.0), 0.032)        # unknown -> pooled

    def test_p_above_is_strict(self):
        self.assertAlmostEqual(gb.p_above(4.0, 4.0, 0.005), 0.5)
        self.assertEqual(gb.p_above(4.0, 4.0, 0.0), 0.0)
        self.assertEqual(gb.p_above(3.99, 4.0, 0.0), 1.0)


class TestStateFair(unittest.TestCase):
    NOON_THU = datetime(2026, 10, 1, 12, 0, tzinfo=gb.ET)

    def test_friday_print_math(self):
        f = gb.state_fair("TX", date(2026, 10, 2), anchor=3.93, live=3.955,
                          prev=3.94, prev2=None, now_et=self.NOON_THU)
        alpha, b1, b2, e = gb.WEEKDAY_MODEL[4]
        bias, scale = gb.STATE_ADJ["TX"]
        self.assertAlmostEqual(f["mu"], round(3.93 + alpha + bias + b1 * 0.015, 5))
        rem = gb.DAY_MOVE_SD[3] * 0.5
        self.assertAlmostEqual(f["sigma"], round(math.sqrt((e * scale) ** 2 + (b1 * rem) ** 2), 5))
        self.assertEqual((f["weekday"], f["b2"]), (4, 0.0))

    def test_monday_print_needs_saturdays_move(self):
        sun = datetime(2026, 9, 27, 12, 0, tzinfo=gb.ET)
        self.assertIsNone(gb.state_fair("CA", date(2026, 9, 28), 6.3528, 6.347,
                                        6.350, None, sun))
        f = gb.state_fair("CA", date(2026, 9, 28), 6.3528, 6.347, 6.350, 6.321, sun)
        alpha, b1, b2, e = gb.WEEKDAY_MODEL[0]
        bias, _ = gb.STATE_ADJ["CA"]
        want = 6.3528 + alpha + bias + b1 * (6.347 - 6.350) + b2 * (6.350 - 6.321)
        self.assertAlmostEqual(f["mu"], round(want, 5))
        # Sunday's live move barely counts: sigma stays near the residual
        self.assertLess(f["sigma"], 0.006)

    def test_new_state_gets_the_pooled_scale(self):
        f = gb.state_fair("ZZ", date(2026, 10, 2), 4.0, 4.0, 4.0, None, self.NOON_THU)
        self.assertAlmostEqual(f["e"], round(gb.WEEKDAY_MODEL[4][3] * gb.NEW_STATE_SCALE, 5))


class TestWriteFairFile(unittest.TestCase):
    """Thursday 2026-10-01 noon ET: the Friday-print events trade."""

    NOW = utc(2026, 10, 1, 16, 0)            # 12:00 EDT

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbfair_")
        self.path = os.path.join(self.dir, "gasbuddy_fair.json")
        self.log = os.path.join(self.dir, "gasbuddy_live.jsonl")
        self.calls = {"anchor": [], "prev": [], "hist": []}
        self.anchors = {"KXAAAGASDCA": ("ok", 6.38), "KXAAAGASDTX": ("ok", 3.93)}
        self.prevs = {"CA": ("2026-09-30", 6.39), "TX": ("2026-09-30", 3.94)}
        self.hist = {"CA": {"2026-09-29": 6.37}, "TX": {"2026-09-29": 3.92}}

    def meta(self, updated="2026-10-01T11:55:00", today="2026-10-01"):
        return {"updated": updated, "today": today, "prev": "2026-09-30"}

    def anchor_fn(self, series, d):
        self.calls["anchor"].append((series, d.isoformat()))
        return self.anchors.get(series, ("absent", None))

    def prev_fn(self, abbr):
        self.calls["prev"].append(abbr)
        return self.prevs.get(abbr, ("", None))

    def hist_fn(self, abbr):
        self.calls["hist"].append(abbr)
        return self.hist.get(abbr, {})

    def write(self, now=None, meta=None, live=None):
        return gb.write_fair_file(
            self.path, now=now or self.NOW, meta=meta or self.meta(),
            live=live if live is not None else {"CA": 6.40, "TX": 3.955, "FL": 4.4},
            anchor_fn=self.anchor_fn, prev_fn=self.prev_fn, history_fn=self.hist_fn,
            live_log=self.log, pace=0)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_happy_path_and_once_a_day_reads(self):
        self.assertEqual(self.write(), (2, 0))
        d = self.read()
        self.assertEqual(sorted(d["entries"]), ["KXAAAGASDCA-26OCT02", "KXAAAGASDTX-26OCT02"])
        tx = d["entries"]["KXAAAGASDTX-26OCT02"]
        want = gb.state_fair("TX", date(2026, 10, 2), 3.93, 3.955, 3.94, None,
                             self.NOW.astimezone(gb.ET))
        self.assertEqual((tx["mu"], tx["sigma"]), (want["mu"], want["sigma"]))
        self.assertEqual((tx["anchor"], tx["prev"], tx["prev_source"]), (3.93, 3.94, "gasbuddy"))
        self.assertEqual(tx["fetched_at"], self.NOW.isoformat())
        self.assertEqual(len(d["absent"]), 49)                   # every other state
        self.assertEqual(self.calls["hist"], [])                 # Friday print: no b2
        n_anchor, n_prev = len(self.calls["anchor"]), len(self.calls["prev"])
        # five minutes later: anchors, absents and yesterday's averages are cached
        self.assertEqual(self.write(now=utc(2026, 10, 1, 16, 5),
                                    meta=self.meta("2026-10-01T12:00:00")), (2, 0))
        self.assertEqual(len(self.calls["anchor"]), n_anchor)
        self.assertEqual(len(self.calls["prev"]), n_prev)

    def test_absent_states_are_reasked_after_six_hours(self):
        self.write()
        n = len(self.calls["anchor"])
        self.write(now=utc(2026, 10, 1, 22, 5), meta=self.meta("2026-10-01T18:00:00"))
        self.assertEqual(len(self.calls["anchor"]) - n, 49)

    def test_nothing_before_seven_et(self):
        early = utc(2026, 10, 1, 10, 30)                          # 06:30 EDT
        self.assertEqual(self.write(now=early, meta=self.meta("2026-10-01T06:25:00")), (0, 0))
        self.assertEqual(self.calls["anchor"], [])
        self.assertEqual(self.read()["closes"]["2026-10-01"]["CA"], 6.40)

    def test_stale_or_wrong_day_live_gives_no_entries(self):
        self.assertEqual(self.write(meta=self.meta("2026-10-01T11:25:00")), (0, 2))
        self.assertEqual(self.read()["entries"], {})
        self.assertEqual(self.write(meta=self.meta(today="2026-09-30")), (0, 2))

    def test_close_stands_in_for_a_missing_one_day_ago(self):
        self.prevs = {"CA": ("2026-09-29", 6.37)}                 # GasBuddy lags a day
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"closes": {"2026-09-30": {"CA": 6.385}}}, f)
        self.write()
        ca = self.read()["entries"]["KXAAAGASDCA-26OCT02"]
        self.assertEqual((ca["prev"], ca["prev_source"]), (6.385, "close"))

    def test_monday_trading_is_skipped(self):
        mon = utc(2026, 10, 5, 16, 0)
        self.assertEqual(self.write(now=mon, meta={"updated": "2026-10-05T11:55:00",
                                                   "today": "2026-10-05", "prev": "2026-10-04"}),
                         (0, 2))
        self.assertTrue(self.calls["anchor"])                    # anchors still cached

    def test_sunday_trading_reads_saturday_and_friday(self):
        sun = utc(2026, 10, 4, 16, 0)
        self.prevs = {"CA": ("2026-10-03", 6.39), "TX": ("2026-10-03", 3.94)}
        self.hist = {"CA": {"2026-10-02": 6.37}, "TX": {"2026-10-02": 3.95}}
        ok, miss = self.write(now=sun, meta={"updated": "2026-10-04T11:55:00",
                                             "today": "2026-10-04", "prev": "2026-10-03"})
        self.assertEqual((ok, miss), (2, 0))
        e = self.read()["entries"]["KXAAAGASDCA-26OCT05"]
        self.assertEqual((e["prev"], e["prev2"], e["b2"]), (6.39, 6.37, 0.47))
        self.assertEqual(sorted(self.calls["hist"]), ["CA", "TX"])
        self.write(now=utc(2026, 10, 4, 16, 5), meta={"updated": "2026-10-04T12:00:00",
                                                      "today": "2026-10-04", "prev": "2026-10-03"})
        self.assertEqual(len(self.calls["hist"]), 2)             # kept in "finals"

    def test_pending_and_failing_anchors(self):
        self.anchors = {"KXAAAGASDCA": ("pending", None)}

        def boom(series, d):
            if series == "KXAAAGASDTX":
                raise RuntimeError("429")
            return self.anchor_fn(series, d)
        ok, miss = gb.write_fair_file(self.path, now=self.NOW, meta=self.meta(),
                                      live={"CA": 6.4, "TX": 3.9}, anchor_fn=boom,
                                      prev_fn=self.prev_fn, history_fn=self.hist_fn,
                                      live_log=self.log, pace=0)
        self.assertEqual((ok, miss), (0, 2))
        d = self.read()
        self.assertNotIn("KXAAAGASDTX", d["absent"])             # an error is not "absent"
        self.assertNotIn("KXAAAGASDCA", d["anchors"])

    def test_live_log_every_fifteen_minutes(self):
        self.write()
        self.write()
        self.write(now=utc(2026, 10, 1, 16, 5), meta=self.meta("2026-10-01T12:00:00"))
        self.write(now=utc(2026, 10, 1, 16, 15), meta=self.meta("2026-10-01T12:10:00"))
        with open(self.log, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([r["updated"] for r in rows],
                         ["2026-10-01T15:55:00+00:00", "2026-10-01T16:10:00+00:00"])
        self.assertEqual(rows[0]["live"]["CA"], 6.40)


class TestFetchParsing(unittest.TestCase):
    def test_fetch_live_maps_names_and_learns_region_ids(self):
        payload = {"PriceRecords": [
            {"RegionID": 300005, "RegionName": "California", "Price": 6.356},
            {"RegionID": 300044, "RegionName": "Texas", "Price": 3.89},
            {"RegionID": 300008, "RegionName": "District of Columbia", "Price": None},
            {"RegionID": 1, "RegionName": "Atlantis", "Price": 9.99}]}
        with mock.patch.object(gb, "_get", return_value=payload):
            self.assertEqual(gb.fetch_live(), {"CA": 6.356, "TX": 3.89})
        self.assertEqual(gb._REGION_IDS["CA"], 300005)

    def test_fetch_anchor_states(self):
        with mock.patch.object(gb, "_get", return_value={"markets": []}):
            self.assertEqual(gb.fetch_anchor("KXAAAGASDAK", date(2026, 9, 27)), ("absent", None))
        with mock.patch.object(gb, "_get", return_value={"markets": [{"expiration_value": ""}]}):
            self.assertEqual(gb.fetch_anchor("KXAAAGASDCA", date(2026, 9, 27)), ("pending", None))
        with mock.patch.object(gb, "_get", return_value={"markets": [{"expiration_value": "6.3528"}]}) as g:
            self.assertEqual(gb.fetch_anchor("KXAAAGASDCA", date(2026, 9, 27)), ("ok", 6.3528))
            self.assertEqual(g.call_args[0][1]["event_ticker"], "KXAAAGASDCA-26SEP27")

    def test_fetch_live_avg(self):
        payload = {"LastUpdatedTime": "2026-09-27T10:45:00.63", "LiveTickingAvg": 6.355,
                   "AvgPriceDict": {"OneDayAgo": {"date": "2026-09-26T00:00:00-04:00",
                                                  "AvgPrice": 6.35},
                                    "Today": {"date": "2026-09-27T00:00:00-04:00",
                                              "AvgPrice": 6.355}}}
        with mock.patch.object(gb, "_get", return_value=payload):
            m = gb.fetch_live_avg(300005)
        self.assertEqual(m, {"updated": "2026-09-27T10:45:00.63", "today": "2026-09-27",
                             "prev": "2026-09-26", "prev_price": 6.35})
        gb._REGION_IDS["CA"] = 300005
        with mock.patch.object(gb, "_get", return_value=payload):
            self.assertEqual(gb.fetch_prev("CA"), ("2026-09-26", 6.35))
        self.assertEqual(gb.fetch_prev("QQ"), ("", None))


if __name__ == "__main__":
    unittest.main()
