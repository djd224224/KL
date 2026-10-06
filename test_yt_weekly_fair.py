"""Tests for yt_weekly_fair (the YouTube weekly artist-views pilot's fair)."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import yt_weekly_fair as yf

RATE = 100_000.0                     # views per hour per video (3 videos)
NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)    # Thursday
EV = "KXYTVIEWSW-KAT26OCT11"
WEEK_END = date(2026, 10, 11)


def write_snaps(path, start, end, artist="KATSEYE", vids=("a", "b", "c"), mode="w"):
    """Hourly snapshots: every video gains RATE views an hour."""
    t = start
    with open(path, mode, encoding="utf-8") as f:
        while t <= end:
            ts = t.timestamp()
            for i, v in enumerate(vids):
                f.write(json.dumps({"ts": ts, "artist": artist, "id": v,
                                    "views": int(1e6 * (i + 1) + RATE * (ts - 1.7e9) / 3600)}) + "\n")
            t += timedelta(hours=1)


def write_chart(path, upto=date(2026, 10, 4), value=7_200_000):
    chart = {}
    d = date(2026, 8, 25)
    while d <= upto:
        chart[d.isoformat()] = value
        d += timedelta(days=1)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"chart": {"KATSEYE": chart}}, f)


def markets(strikes=(6.5, 7.0, 7.5, 8.0, 8.5), revealed=()):
    out = []
    for k in strikes:
        out.append({"ticker": f"{EV}-{k:.1f}M", "floor_strike": k * 1e6, "status": "active",
                    "close_time": "2026-10-13T14:00:00Z"})
    for k, v, ct in revealed:
        out.append({"ticker": f"{EV}-{k:.1f}M", "floor_strike": k * 1e6, "status": "finalized",
                    "result": "yes", "expiration_value": str(v), "close_time": ct})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.snap = os.path.join(self.dir, "snaps.jsonl")
        self.chart = os.path.join(self.dir, "chart.json")
        self.out = os.path.join(self.dir, "fair.json")
        yf.reset_snapshots()
        write_snaps(self.snap, datetime(2026, 9, 20, tzinfo=timezone.utc),
                    NOW - timedelta(minutes=30))
        write_chart(self.chart)
        self.patches = [mock.patch.object(yf, "ARTISTS", {"KAT": "KATSEYE"}),
                        mock.patch.object(yf, "N_SIM", 2000)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        yf.reset_snapshots()

    def fair(self, ms=None, now=NOW):
        series, last = yf.load_snapshots(self.snap)
        chart = yf.load_chart(self.chart)
        return yf.event_fair(EV, ms if ms is not None else markets(), series.get("KATSEYE", {}),
                             last, chart.get("KATSEYE", {}), "KATSEYE", now.timestamp())


class TestTickersAndSnapshots(Base):
    def test_parse(self):
        self.assertEqual(yf.parse_event(EV), ("KAT", WEEK_END))
        self.assertEqual(yf.parse_event("KXYTVIEWSW-YE26OCT11"), ("YE", WEEK_END))
        self.assertIsNone(yf.parse_event("KXYTVIEWSW-KAT26XXX11"))
        self.assertIsNone(yf.parse_event("KXYTVIEWSHIGH-KAT26NOV"))
        self.assertEqual(yf.strike_key(f"{EV}-13.25M"), "13.25M")
        self.assertEqual(yf.strike_views("13.25M"), 13_250_000)
        self.assertIsNone(yf.strike_key(f"{EV}-B13"))

    def test_incremental_read_and_window(self):
        series, last = yf.load_snapshots(self.snap)
        self.assertEqual(set(series["KATSEYE"]), {"a", "b", "c"})
        self.assertAlmostEqual(last, (NOW - timedelta(hours=1)).timestamp())   # hourly from 00:00Z
        off = yf._snap["offset"]
        # the next pass appends: only it is read, and an unknown artist is skipped
        write_snaps(self.snap, NOW + timedelta(minutes=30), NOW + timedelta(minutes=30), mode="a")
        write_snaps(self.snap, NOW + timedelta(minutes=30), NOW + timedelta(minutes=30),
                    artist="Taylor Swift", mode="a")
        series, last2 = yf.load_snapshots(self.snap)
        self.assertGreater(yf._snap["offset"], off)
        self.assertNotIn("Taylor Swift", series)
        self.assertAlmostEqual(last2, (NOW + timedelta(minutes=30)).timestamp())
        # a full window of 3 videos at RATE/h
        T0, T1 = yf.window_bounds(date(2026, 10, 5))
        self.assertAlmostEqual(yf.api_window(series["KATSEYE"], T0, T1), 3 * RATE * 24, delta=1)
        # a window the snapshots do not cover
        self.assertIsNone(yf.api_window(series["KATSEYE"], T0 - 40 * 86400, T0 - 39 * 86400))


class TestEventFair(Base):
    def test_days_strikes_and_monotone_fair(self):
        e, new = self.fair()
        self.assertEqual(new, {})
        self.assertAlmostEqual(e["ratio"], 1.0, places=3)     # chart 7.2M = API 7.2M
        kinds = {d: v[0] for d, v in e["days"].items()}
        self.assertEqual(kinds["2026-10-05"], "complete")
        self.assertEqual(kinds["2026-10-07"], "complete")     # window ended 15:00Z today
        self.assertEqual(kinds["2026-10-08"], "partial")
        self.assertEqual(kinds["2026-10-11"], "future")
        self.assertAlmostEqual(e["days"]["2026-10-06"][1], 7.2e6, delta=1e4)
        p = {k: s["p"] for k, s in e["strikes"].items()}
        self.assertGreater(p["6.5M"], 0.95)
        self.assertTrue(p["6.5M"] >= p["7.0M"] >= p["7.5M"] >= p["8.0M"] >= p["8.5M"])
        self.assertLess(p["8.5M"], p["7.5M"])
        # holds: strikes within 1.5 spacings (0.75M) of a recent unprinted estimate
        self.assertTrue(e["strikes"]["7.0M"]["hold"])
        self.assertTrue(e["strikes"]["7.5M"]["hold"])
        self.assertEqual(e["strikes"]["8.5M"]["hold"], "")
        self.assertEqual(e["k_min_open"], 6.5e6)

    def test_revealed_print_floors_the_max_and_is_matched(self):
        # a print of 7.6M closed the 6.0M strike YES at 10/07 06:00Z: 30h after
        # 10/05 ended, near 10/05's nowcast -> it is 10/05's print
        ms = markets(revealed=[(6.0, 7_600_000, "2026-10-07T06:00:00Z")])
        e, new = self.fair(ms)
        self.assertEqual(e["m_pub"], 7_600_000)
        self.assertEqual(e["strikes"]["7.5M"]["p"], 1.0)
        self.assertEqual(new, {date(2026, 10, 5): 7_600_000})
        self.assertEqual(e["days"]["2026-10-05"][0], "print")
        # a value far from every nowcast is a floor but no day's print
        ms = markets(revealed=[(6.0, 12_000_000, "2026-10-07T06:00:00Z")])
        e, new = self.fair(ms)
        self.assertEqual(new, {})
        self.assertEqual(e["strikes"]["8.5M"]["p"], 1.0)

    def test_old_unprinted_days_are_capped_at_the_lowest_open_strike(self):
        # only 10/05 is old enough (> 50h) to have printed: with every strike
        # far above it, a capped 10/05 adds nothing and the fair falls
        ms = markets(strikes=(7.4, 7.6))
        e, _ = self.fair(ms)
        with mock.patch.object(yf, "PUBLISHED_AFTER_H", 1e9):
            e2, _ = self.fair(ms)
        self.assertLessEqual(e["strikes"]["7.6M"]["p"], e2["strikes"]["7.6M"]["p"])

    def test_fails_closed(self):
        # no ratio: the chart history is older than RATIO_MAX_AGE_DAYS
        write_chart(self.chart, upto=date(2026, 9, 10))
        e, _ = self.fair()
        self.assertIsNone(e)
        # no open strike
        write_chart(self.chart)
        e, _ = self.fair(markets(strikes=()))
        self.assertIsNone(e)


class TestWriteFairFile(Base):
    def reads(self):
        return (lambda: [EV, "KXYTVIEWSW-ARI26OCT11", "KXYTVIEWSW-KAT26XXX11"],
                lambda ev: markets(revealed=[(6.0, 7_600_000, "2026-10-07T06:00:00Z")]))

    def test_writes_pilot_events_and_saves_matched_prints(self):
        n, miss, status = yf.write_fair_file(self.out, now=NOW, reads=self.reads(),
                                             snap_path=self.snap, chart_path=self.chart)
        self.assertEqual((n, miss, status), (1, 0, "ok"))
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(list(data["entries"]), [EV])          # ARI is not a pilot artist
        self.assertIn("fetched_at", data["entries"][EV])
        self.assertEqual(yf.load_chart(self.chart)["KATSEYE"]["2026-10-05"], 7_600_000)

    def test_stale_snapshots_write_nothing(self):
        later = NOW + timedelta(seconds=yf.API_STALE_SECS + 3600)
        n, miss, status = yf.write_fair_file(self.out, now=later, reads=self.reads(),
                                             snap_path=self.snap, chart_path=self.chart)
        self.assertEqual((n, status), (0, "stale_api"))
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["entries"], {})


if __name__ == "__main__":
    unittest.main()
