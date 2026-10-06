"""Tests for yt_pilot_feed (the KXYTVIEWSW pilot's durable YouTube API feed)."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

import yt_pilot_feed as feed
import yt_weekly_fair as yf

KEY = "AIza" + "Q7" * 17 + "x"                 # a fake key of the real shape
NOW = datetime(2026, 10, 6, 18, 0, 30, tzinfo=timezone.utc).timestamp()   # PT day 10/06
KAT = [f"k{i:03d}" for i in range(120)]
DRA = [f"d{i:03d}" for i in range(50)]


class FakeAPI:
    """The two endpoints the feed calls; fail_at = {call number: (code,
    reason) or an exception} for the calls that should fail."""

    def __init__(self, videos, playlists=None, fail_at=None):
        self.videos = videos                  # id -> views (None = hidden count)
        self.playlists = playlists or {}      # playlist id -> [video id]
        self.fail_at = fail_at or {}
        self.no_uploads = set()               # channels without an uploads playlist
        self.calls = []

    def __call__(self, url, headers):
        u = urllib.parse.urlsplit(url)
        path = u.path.rsplit("/", 1)[-1]
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((path, q, dict(headers), url))
        f = self.fail_at.get(len(self.calls))
        if isinstance(f, Exception):
            raise f
        if f:
            code, reason = f
            return code, json.dumps({"error": {"code": code, "message": "no",
                                               "errors": [{"reason": reason}]}}).encode()
        if path == "videos":
            items = [{"id": v, "statistics": {} if self.videos[v] is None else
                      {"viewCount": str(self.videos[v])}}
                     for v in q["id"].split(",") if v in self.videos]
            return 200, json.dumps({"items": items}).encode()
        if path == "playlistItems" and q["playlistId"] in self.no_uploads:
            return 404, json.dumps({"error": {"code": 404, "message": "not found",
                                              "errors": [{"reason": "playlistNotFound"}]}}).encode()
        if path == "playlistItems":
            return 200, json.dumps({"items": [{"contentDetails": {"videoId": v}}
                                              for v in self.playlists.get(q["playlistId"], [])]}).encode()
        return 404, b"{}"

    def paths(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        j = lambda n: os.path.join(self.dir, n)
        self.src = j("yt_artist_ids.json")
        self.write_src()
        self.patches = [
            mock.patch.object(feed, "OUT_FILE", j("yt_pilot_snapshots.jsonl")),
            mock.patch.object(feed, "IDS_FILE", j("yt_pilot_ids.json")),
            mock.patch.object(feed, "SOURCE_IDS_FILE", self.src),
            mock.patch.object(feed, "STATE_FILE", j("state.json")),
            mock.patch.object(feed, "LOG_FILE", j("feed.log")),
            mock.patch.object(feed, "LOCK_FILE", j("feed.lock")),
            mock.patch.object(feed, "DAILY_UNITS", 2000),
            mock.patch.object(yf, "ARTISTS", {"KAT": "KATSEYE", "DRA": "Drake"}),
        ]
        for p in self.patches:
            p.start()
        views = {v: 1_000_000 + i for i, v in enumerate(KAT + DRA)}
        del views["k000"]                      # deleted / private: not returned
        views["k001"] = None                   # hidden count: no line
        self.api = FakeAPI(views)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        feed._SECRETS.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_src(self, kat_extra=(), body=None):
        src = {"KATSEYE": {"channels": [{"id": "UCkat1", "title": "KATSEYE"},
                                        {"id": "UCkat2", "title": "KATSEYE - Topic"}],
                           "videos": KAT + list(kat_extra), "v": 2},
               "Drake": {"channels": [{"id": "UCdra1", "title": "Drake"}], "videos": DRA, "v": 2},
               "Taylor Swift": {"channels": [{"id": "UCts", "title": "Taylor Swift"}],
                                "videos": ["t1", "t2"], "v": 2}}
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(body if body is not None else json.dumps(src))

    def run_pass(self, at=NOW, **kw):
        kw.setdefault("fetch", self.api)
        kw.setdefault("key", KEY)
        return feed.run_pass(now_ts=at, sleep=lambda s: None, clock=lambda: at, **kw)

    def state(self):
        with open(feed.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()


class TestPass(Base):
    def test_snapshots_in_batches_of_50_in_the_fairs_format(self):
        code, summary = self.run_pass()
        self.assertEqual(code, 0, summary)
        # first pass: the scan (3 channels), then 3 + 1 videos.list calls
        self.assertEqual(self.api.paths(), ["playlistItems"] * 3 + ["videos"] * 4)
        asked = [v for p, q, _h, _u in self.api.calls if p == "videos" for v in q["id"].split(",")]
        self.assertEqual(sorted(asked), sorted(KAT + DRA))
        self.assertTrue(all(len(q["id"].split(",")) <= 50 for p, q, _h, _u in self.api.calls
                            if p == "videos"))
        # the key rides in a header, never in a URL
        for _p, q, h, url in self.api.calls:
            self.assertEqual(h["X-Goog-Api-Key"], KEY)
            self.assertNotIn(KEY, url)
            self.assertNotIn("key", q)
        # the fair reads the file as it reads the collector's
        yf.reset_snapshots()
        try:
            series, last = yf.load_snapshots(feed.OUT_FILE, names={"KATSEYE", "Drake", "Taylor Swift"})
        finally:
            yf.reset_snapshots()
        self.assertEqual(len(series["KATSEYE"]), 118)      # minus deleted + hidden
        self.assertEqual(len(series["Drake"]), 50)
        self.assertNotIn("Taylor Swift", series)            # not a pilot artist
        self.assertAlmostEqual(last, NOW)
        st = self.state()
        self.assertEqual(st["quota"], {"day": "2026-10-06", "units": 7})
        self.assertEqual((st["last_snapshot"], st["last_scan"]), (NOW, NOW))
        self.assertEqual(st["last_pass"]["status"], "ok")
        with open(feed.IDS_FILE, encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)), ["Drake", "KATSEYE"])
        for p in (feed.OUT_FILE, feed.IDS_FILE, feed.STATE_FILE, feed.LOG_FILE):
            self.assertNotIn(KEY, self.read(p))
        self.assertIn("snapshot 168 videos (KATSEYE 118/120, Drake 50/50)", self.read(feed.LOG_FILE))

    def test_merges_the_collectors_new_videos_and_survives_a_torn_file(self):
        self.api.playlists = {"UUkat2": ["k005", "scan1"]}
        self.api.videos["scan1"] = 5
        self.run_pass()
        self.assertIn("new upload tracked: KATSEYE scan1", self.read(feed.LOG_FILE))
        # the collector found k999 since; the feed's own scan1 stays
        self.write_src(kat_extra=["k999"])
        self.api.videos["k999"] = 9
        self.api.calls.clear()
        self.run_pass(at=NOW + 3600)
        asked = {v for p, q, _h, _u in self.api.calls if p == "videos" for v in q["id"].split(",")}
        self.assertTrue({"k999", "scan1"} <= asked)
        self.assertIn("KATSEYE: +1 videos from yt_artist_ids.json", self.read(feed.LOG_FILE))
        # the collector's file caught mid-rewrite: the feed's own list carries on
        self.write_src(body='{"KATSEYE": {"videos": [')
        self.api.calls.clear()
        code, _ = self.run_pass(at=NOW + 7200)
        self.assertEqual(code, 0)
        asked = {v for p, q, _h, _u in self.api.calls if p == "videos" for v in q["id"].split(",")}
        self.assertEqual(asked, set(KAT + DRA) | {"k999", "scan1"})

    def test_scans_every_three_hours_and_a_new_upload_is_snapshot_at_once(self):
        scans = []
        for h in range(5):
            if h == 3:
                self.api.playlists = {"UUdra1": ["fresh"]}
                self.api.videos["fresh"] = 77
            self.api.calls.clear()
            self.run_pass(at=NOW + h * 3600)
            scans.append(self.api.paths().count("playlistItems"))
        self.assertEqual(scans, [3, 0, 0, 3, 0])
        self.assertIn('"id": "fresh", "views": 77', self.read(feed.OUT_FILE))

    def test_a_channel_without_uploads_is_skipped_for_a_week_not_an_error(self):
        self.api.no_uploads = {"UUkat2"}
        code, summary = self.run_pass()
        self.assertEqual(code, 0, summary)
        self.assertIn("has no uploads playlist; skipped for 7 days", self.read(feed.LOG_FILE))
        scans = []
        for at in (NOW + 3 * 3600, NOW + 8 * 86400):
            self.api.calls.clear()
            self.run_pass(at=at)
            scans.append([q["playlistId"] for p, q, _h, _u in self.api.calls if p == "playlistItems"])
        self.assertEqual(scans, [["UUkat1", "UUdra1"], ["UUkat1", "UUkat2", "UUdra1"]])

    def test_gap_guard_and_force(self):
        self.run_pass()
        self.api.calls.clear()
        code, msg = self.run_pass(at=NOW + 600)
        self.assertEqual((code, self.api.calls), (0, []))
        self.assertIn("skip: last snapshot 10 min ago", msg)
        code, _ = self.run_pass(at=NOW + 600, force=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.paths(), ["videos"] * 4)


class TestQuota(Base):
    def test_pacific_day(self):
        z = lambda s: datetime.fromisoformat(s).timestamp()
        self.assertEqual(feed.pt_day(z("2026-10-07T06:59:00+00:00")), "2026-10-06")   # PDT
        self.assertEqual(feed.pt_day(z("2026-10-07T07:01:00+00:00")), "2026-10-07")
        self.assertEqual(feed.next_pt_midnight(NOW), z("2026-10-07T07:00:00+00:00"))
        self.assertEqual(feed.next_pt_midnight(z("2026-11-10T12:00:00+00:00")),
                         z("2026-11-11T08:00:00+00:00"))                              # PST

    def test_own_daily_cap_puts_the_snapshot_first_then_skips(self):
        with mock.patch.object(feed, "DAILY_UNITS", 6):
            code, _ = self.run_pass()            # 6 left: snapshot (4) yes, scan (+3) no
            self.assertEqual((code, self.api.paths()), (0, ["videos"] * 4))
            self.api.calls.clear()
            code, msg = self.run_pass(at=NOW + 3600)
            self.assertEqual((code, self.api.calls), (1, []))
            self.assertIn("2 units left", msg)
            # the next Pacific day starts a fresh ledger
            code, _ = self.run_pass(at=NOW + 14 * 3600)
            self.assertEqual(code, 0)
            self.assertEqual(self.state()["quota"], {"day": "2026-10-07", "units": 4})

    def test_quota_exceeded_keeps_what_was_read_and_blocks_to_pacific_midnight(self):
        self.api.fail_at = {5: (403, "quotaExceeded")}          # the 2nd videos.list call
        code, summary = self.run_pass()
        self.assertEqual(code, 1)
        self.assertIn("quota (quotaExceeded)", summary)
        self.assertIn("snapshot 48 videos (KATSEYE 48/120, Drake 0/50)", summary)
        self.assertEqual(len(self.read(feed.OUT_FILE).splitlines()), 48)   # batch 1 kept
        st = self.state()
        midnight = datetime(2026, 10, 7, 7, tzinfo=timezone.utc).timestamp()
        self.assertEqual(st["blocked_until"], midnight)
        self.assertEqual(st["last_snapshot"], NOW)
        self.api.calls.clear()
        self.api.fail_at = {}
        code, msg = self.run_pass(at=NOW + 3600)
        self.assertEqual((code, self.api.calls), (0, []))
        self.assertIn("blocked to 2026-10-07T07:00:00", msg)
        code, _ = self.run_pass(at=midnight + 300)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.paths(), ["playlistItems"] * 3 + ["videos"] * 4)


class TestErrors(Base):
    def test_transient_errors_are_retried_and_charged(self):
        self.api.fail_at = {4: (503, "backendError"), 6: (403, "rateLimitExceeded")}
        code, summary = self.run_pass()
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.state()["quota"]["units"], 9)        # 7 + 2 retries
        self.assertEqual(len(self.read(feed.OUT_FILE).splitlines()), 168)

    def test_a_failed_batch_is_skipped_and_the_pass_is_partial(self):
        err = OSError(f"Max retries exceeded with url: /videos?key={KEY}")
        self.api.fail_at = {4: err, 5: err, 6: err, 8: (400, "badRequest")}
        code, summary = self.run_pass()
        self.assertEqual(code, 1)
        self.assertIn("partial", summary)
        lines = self.read(feed.OUT_FILE).splitlines()
        self.assertEqual(len(lines), 168 - 48 - 20)            # KATSEYE's 1st and 3rd lost
        st = self.state()
        self.assertEqual(st["quota"]["units"], 3 + 3 + 1 + 1 + 1)   # a 400 is not retried
        self.assertEqual(len(st["last_pass"]["errors"]), 2)
        log = self.read(feed.LOG_FILE)
        self.assertIn("<key>", log)                             # scrubbed, not dropped
        self.assertNotIn(KEY, log + json.dumps(st))

    def test_a_hung_pass_stops_at_the_deadline_and_keeps_what_it_read(self):
        ticks = iter([0.0] * 5 + [1e9] * 50)          # 4 calls in time, then past it
        with mock.patch.object(feed.time, "monotonic", lambda: next(ticks)):
            code, summary = self.run_pass()
        self.assertEqual(code, 1)
        self.assertIn("deadline", summary)
        self.assertEqual(self.api.paths(), ["playlistItems"] * 3 + ["videos"])
        self.assertEqual(len(self.read(feed.OUT_FILE).splitlines()), 48)

    def test_no_key_file_and_no_video_list(self):
        with mock.patch.object(feed, "KEY_FILE", os.path.join(self.dir, "missing.txt")):
            code, msg = feed.run_pass(now_ts=NOW, fetch=self.api)
        self.assertEqual((code, self.api.calls), (2, []))
        self.assertIn("no API key", msg)
        self.write_src(body="{}")
        with mock.patch.object(yf, "ARTISTS", {"ZZZ": "Nobody"}):
            code, _ = self.run_pass()
        self.assertEqual((code, self.api.calls), (2, []))


class TestMain(Base):
    def test_lock_dry_run_and_status(self):
        held = feed.single_instance()
        try:
            self.assertEqual(feed.main([]), 3)
        finally:
            held.close()
        out = io.StringIO()
        with mock.patch.object(feed, "api_key", side_effect=AssertionError("read the key")),                 contextlib.redirect_stdout(out):
            self.assertEqual(feed.main(["--dry-run"]), 0)
        self.assertIn("KATSEYE: 120 videos -> 3 videos.list calls; 2 channels to scan", out.getvalue())
        for p in (feed.OUT_FILE, feed.IDS_FILE, feed.STATE_FILE):
            self.assertFalse(os.path.exists(p), p)
        self.run_pass()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(feed.main(["--status"]), 0)
        self.assertIn("units PT 2026-10-06: 7/2000", out.getvalue())

    def test_key_reader_registers_it_for_scrubbing(self):
        p = os.path.join(self.dir, "key.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"my key: {KEY}\n")
        self.assertEqual(feed.api_key(p), KEY)
        feed.log(f"oops {KEY}")
        self.assertNotIn(KEY, self.read(feed.LOG_FILE))


if __name__ == "__main__":
    unittest.main()
