"""Tests for yt_topvid_feed (the global #1 / #2 top-video markets' YouTube API feed)."""
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

import yt_pilot_feed as ypf
import yt_topvid_feed as feed

KEY = "AIza" + "Q7" * 17 + "x"                 # a fake key of the real shape
NOW = datetime(2026, 10, 14, 18, 0, 30, tzinfo=timezone.utc).timestamp()   # PT day 10/14
EV2 = "KXYTTOPVIDEOG2D-26OCT14"
EV1 = "KXYTDAILYTOPVIDEOG-26OCT14"


def collector_ids(n=120):
    """The collector's yt_ids.json shape: code -> {"id", "yt_title", "channel"}."""
    ids = {f"P:v{i:03d}": {"id": f"v{i:03d}", "yt_title": f"Filler song {i}", "channel": f"Ch {i}"}
           for i in range(n)}
    ids["R01"] = {"id": "daidai_off", "yt_title": "Shakira - Dai Dai (Official Video)",
                  "channel": "Shakira"}
    ids["R02"] = {"id": "daidai_lyr", "yt_title": "Dai Dai (Lyrics)", "channel": "Lyric Hub"}
    ids["R03"] = {"id": "xamdam_vid", "yt_title": "Xamdam Sobirov - Peshta (Official Music Video)",
                  "channel": "Xamdam Sobirov"}
    return ids


def markets():
    return [
        {"ticker": f"{EV2}-DAI", "event_ticker": EV2, "yes_sub_title": "Dai Dai"},
        {"ticker": f"{EV2}-XAM", "event_ticker": EV2,
         "yes_sub_title": "Xamdam Sobirov - Peshta / Хамдам Собиров - Пешта / Video Klip"},
        {"ticker": f"{EV2}-KALY", "event_ticker": EV2,
         "yes_sub_title": "KALYANI (Remix) [YouTube video 12: z5y8Clp_TdE]"},
        {"ticker": f"{EV2}-CAS", "event_ticker": EV2, "yes_sub_title": "Casa Tupka Anthemo"},
        {"ticker": f"{EV1}-DAI", "event_ticker": EV1, "yes_sub_title": "Dai Dai"},
        {"ticker": f"{EV1}-CAS", "event_ticker": EV1, "yes_sub_title": "Casa Tupka Anthemo"},
    ]


class FakeAPI:
    """videos (by id, or chart=mostPopular by region) and search; fail_at =
    {call number: (code, reason) or an exception}."""

    def __init__(self, views, popular=None, search=None, fail_at=None):
        self.views = views                    # id -> (views or None, title, channel)
        self.popular = popular or {}          # region -> [(id, title, channel)]
        self.search = search or {}            # query -> [(id, title, channel)]
        self.fail_at = fail_at or {}
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
        if path == "videos" and q.get("chart") == "mostPopular":
            items = [{"id": i, "snippet": {"title": t, "channelTitle": c}}
                     for i, t, c in self.popular.get(q["regionCode"], [])]
            return 200, json.dumps({"items": items}).encode()
        if path == "videos":
            items = []
            for v in q["id"].split(","):
                if v not in self.views:
                    continue
                n, t, c = self.views[v]
                items.append({"id": v, "snippet": {"title": t, "channelTitle": c},
                              "statistics": {} if n is None else {"viewCount": str(n)}})
            return 200, json.dumps({"items": items}).encode()
        if path == "search":
            items = [{"id": {"videoId": i}, "snippet": {"title": t, "channelTitle": c}}
                     for i, t, c in self.search.get(q["q"], [])]
            return 200, json.dumps({"items": items}).encode()
        return 404, b"{}"

    def paths(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        j = lambda n: os.path.join(self.dir, n)
        self.src = j("yt_ids.json")
        with open(self.src, "w", encoding="utf-8") as f:
            json.dump(collector_ids(), f)
        self.patches = [
            mock.patch.object(feed, "OUT_FILE", j("yt_topvid_snapshots.jsonl")),
            mock.patch.object(feed, "IDS_FILE", j("yt_topvid_ids.json")),
            mock.patch.object(feed, "SOURCE_IDS_FILE", self.src),
            mock.patch.object(feed, "STATE_FILE", j("state.json")),
            mock.patch.object(feed, "LOG_FILE", j("feed.log")),
            mock.patch.object(feed, "LOCK_FILE", j("feed.lock")),
            mock.patch.object(feed, "DAILY_UNITS", 2000),
            mock.patch.object(feed, "REGIONS", ["US", "IN"]),
        ]
        for p in self.patches:
            p.start()
        views = {r["id"]: (1_000_000 + i, r["yt_title"], r["channel"])
                 for i, r in enumerate(collector_ids().values())}
        views["daidai_off"] = (90_000_000, "Shakira - Dai Dai (Official Video)", "Shakira")
        views["daidai_lyr"] = (2_000_000, "Dai Dai (Lyrics)", "Lyric Hub")
        views["z5y8Clp_TdE"] = (5_000_000, "KALYANI (Remix) | Official", "Remix Co")
        views["casa_tupka"] = (7_000_000, "Casa Tupka - Anthemo (Official Video)", "Casa Tupka")
        views["v005"] = (None, "Filler song 5", "Ch 5")          # hidden count: no line
        views["casa_react"] = (40_000, "Casa Tupka Anthemo REACTION", "Reacts")
        self.api = FakeAPI(views, search={"Casa Tupka Anthemo": [
            ("casa_cover", "Anthemo cover", "Someone"),
            ("casa_react", "Casa Tupka Anthemo REACTION", "Reacts"),   # closer title, ranked higher
            ("casa_tupka", "Casa Tupka - Anthemo (Official Video)", "Casa Tupka")]})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ypf._SECRETS.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_pass(self, at=NOW, mk=markets, **kw):
        kw.setdefault("fetch", self.api)
        kw.setdefault("key", KEY)
        return feed.run_pass(now_ts=at, kalshi=mk, sleep=lambda s: None, clock=lambda: at, **kw)

    def ids(self):
        with open(feed.IDS_FILE, encoding="utf-8") as f:
            return json.load(f)

    def state(self):
        with open(feed.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def lines(self):
        with open(feed.OUT_FILE, encoding="utf-8") as f:
            return [json.loads(x) for x in f]


class TestMatch(unittest.TestCase):
    VIDS = {"a": {"title": "Shakira - Dai Dai (Official Video)", "channel": "Shakira", "views": 9},
            "b": {"title": "Dai Dai (Lyrics)", "channel": "Lyric Hub", "views": 2},
            "x": {"title": "Xamdam Sobirov - Peshta (Official Music Video)", "channel": "X"},
            "m": {"title": "295", "channel": "Sidhu Moose Wala", "views": 5}}

    def test_an_id_in_the_name_wins(self):
        self.assertEqual(feed.match_video("KALYANI (Remix) [YouTube video 7: xvT1jH8B9AM]", {}),
                         "xvT1jH8B9AM")

    def test_every_word_of_one_alternative_most_viewed_first(self):
        self.assertEqual(feed.match_video("Dai Dai", self.VIDS), "a")
        self.assertEqual(feed.match_video(
            "Xamdam Sobirov - Peshta / Хамдам Собиров - Пешта / Video Klip", self.VIDS), "x")
        # words may come from the channel; noise words ("MUSIC VIDEO") never count
        self.assertEqual(feed.match_video("295 (MUSIC VIDEO) SIDHU MOOSE WALA", self.VIDS), "m")
        self.assertIsNone(feed.match_video("Dai Dai Remix", self.VIDS))     # a word missing
        self.assertIsNone(feed.match_video("", self.VIDS))
        # a (...) / [...] tag may be missing from the video's title
        rad = {"r": {"title": "@SaiAbhyankkar - Radhimaa (Music Video)", "channel": "Think Music"}}
        self.assertEqual(feed.match_video('Radhimaa [From "Think Indie"]', rad), "r")


class TestPass(Base):
    def test_maps_the_candidates_and_snapshots_every_tracked_video(self):
        code, summary = self.run_pass()
        self.assertEqual(code, 0, summary)
        ids = self.ids()
        ev2 = ids["events"][EV2]
        self.assertEqual(ev2["DAI"]["id"], "daidai_off")             # the official, most viewed
        self.assertEqual(ev2["XAM"]["id"], "xamdam_vid")
        self.assertEqual(ev2["KALY"]["id"], "z5y8Clp_TdE")
        self.assertEqual(ev2["KALY"]["via"], "id in name")
        self.assertEqual(ids["events"][EV1]["DAI"]["id"], "daidai_off")
        # CAS matched no tracked title: one search (100 units) and the hits'
        # view counts (1 unit); of the results holding its words the most
        # viewed, not the reaction video ranked above it -- for both events
        self.assertEqual(ev2["CAS"]["id"], "casa_tupka")
        self.assertEqual(ev2["CAS"]["via"], "search")
        self.assertEqual(ids["events"][EV1]["CAS"]["id"], "casa_tupka")
        self.assertEqual(self.api.paths().count("search"), 1)
        # the snapshot ran first: the 123 seeded videos, 50 a call (the
        # matching above read its view counts)
        self.assertEqual(self.api.paths()[:3], ["videos"] * 3)
        lines = self.lines()
        self.assertEqual({tuple(sorted(x)) for x in lines}, {("id", "ts", "views")})
        self.assertEqual(len(lines), 122)                             # v005's count is hidden
        self.assertNotIn("z5y8Clp_TdE", {x["id"] for x in lines})    # from the next pass
        st = self.state()
        self.assertEqual(st["quota"]["units"], 3 + 2 + 100 + 1)      # snapshot, discover, search, its views
        self.assertEqual(st["searches"]["n"], 1)
        self.assertEqual(st["last_pass"]["unmatched"], 0)
        # the key travels in the header only and is never logged
        for path, q, headers, url in self.api.calls:
            self.assertEqual(headers["X-Goog-Api-Key"], KEY)
            self.assertNotIn(KEY, url)
        with open(feed.LOG_FILE, encoding="utf-8") as f:
            self.assertNotIn(KEY, f.read())

    def test_the_next_pass_snapshots_what_the_last_one_found(self):
        self.run_pass()
        n0 = len(self.lines())
        self.api.calls.clear()
        code, _ = self.run_pass(at=NOW + 1200)
        self.assertEqual(code, 0)
        self.assertNotIn("search", self.api.paths())                 # the name map holds it
        new = {x["id"] for x in self.lines()[n0:]}
        self.assertIn("casa_tupka", new)                              # the search hit
        self.assertIn("z5y8Clp_TdE", new)                             # the id in a name
        self.assertEqual(self.ids()["videos"]["z5y8Clp_TdE"]["title"], "KALYANI (Remix) | Official")

    def test_discovery_every_three_hours_and_pruning(self):
        self.api.popular = {"US": [("newrel", "Brand New Release", "Star")]}
        self.api.views["newrel"] = (3_000_000, "Brand New Release", "Star")
        self.run_pass()
        self.assertIn("newrel", self.ids()["videos"])
        self.api.calls.clear()
        self.run_pass(at=NOW + 1200)                                  # not due again
        self.assertFalse(any(q.get("chart") for _p, q, _h, _u in self.api.calls))
        self.api.calls.clear()
        self.run_pass(at=NOW + 3 * 3600)
        self.assertTrue(any(q.get("chart") for _p, q, _h, _u in self.api.calls))
        # 15 days on: what nothing re-saw is dropped; open candidates stay
        later = NOW + 15 * 86400
        code, summary = self.run_pass(at=later, mk=lambda: markets()[:1])
        vids = self.ids()["videos"]
        self.assertIn("daidai_off", vids)
        self.assertNotIn("v010", vids)
        self.assertIn("dropped", summary)
        with mock.patch.object(feed, "MAX_TRACKED", 50):
            ids = self.ids()
            for i in range(80):
                ids["videos"][f"n{i}"] = {"title": "", "channel": "", "last": later + i}
            self.assertEqual(feed.prune(ids, later + 100, {"daidai_off"}), 80 + len(vids) - 50)
            self.assertIn("daidai_off", ids["videos"])
            self.assertIn("n79", ids["videos"])                       # the most recently seen
            self.assertEqual(len(ids["videos"]), 50)

    def test_the_cap_keeps_the_fastest_once_they_have_a_rate(self):
        with mock.patch.object(feed, "MAX_TRACKED", 100):
            code, summary = self.run_pass()
            vids = self.ids()["videos"]
            self.assertEqual(len(vids), 125)          # 123 seeds + 2 candidates' videos, all new
            self.assertNotIn("dropped", summary)
            for i in range(100, 120):                 # 20 videos gain views over 3 hours
                n, t, c = self.api.views[f"v{i:03d}"]
                self.api.views[f"v{i:03d}"] = (n + 3_000_000, t, c)
            self.run_pass(at=NOW + 3 * 3600)
            vids = self.ids()["videos"]
            self.assertEqual(len(vids), 100)
            for i in range(100, 120):
                self.assertIn(f"v{i:03d}", vids)
                self.assertEqual(vids[f"v{i:03d}"]["rate"], 24_000_000)   # views a day
            for c in ("daidai_off", "xamdam_vid", "z5y8Clp_TdE", "casa_tupka"):
                self.assertIn(c, vids)                # open candidates always stay
            # a dropped video is not re-discovered for REDISCOVER_DAYS
            gone = next(v for v in self.ids()["dropped"])
            self.api.popular = {"US": [(gone, "Filler", "Ch")]}
            self.run_pass(at=NOW + 6 * 3600 + 60)
            self.assertNotIn(gone, self.ids()["videos"])
            self.run_pass(at=NOW + 3 * 86400 + 6 * 3600)
            self.assertIn(gone, self.ids()["videos"])

    def test_a_kalshi_failure_still_snapshots(self):
        def down():
            raise RuntimeError("HTTP 503")
        code, summary = self.run_pass(mk=down)
        self.assertEqual(code, 1)
        self.assertIn("Kalshi read failed", summary)
        self.assertGreater(len(self.lines()), 100)

    def test_searches_are_capped_and_a_name_is_tried_once_a_day(self):
        nomatch = [{"ticker": f"{EV2}-Q{i}", "event_ticker": EV2, "yes_sub_title": f"Unknown {i}"}
                   for i in range(8)]
        with mock.patch.object(feed, "SEARCH_PER_DAY", 3):
            self.run_pass(mk=lambda: nomatch)
            self.assertEqual(self.api.paths().count("search"), 3)
            self.assertEqual(self.state()["last_pass"]["unmatched"], 8)
            self.api.calls.clear()
            self.run_pass(at=NOW + 1200, mk=lambda: nomatch)
            self.assertEqual(self.api.paths().count("search"), 0)     # the day's 3 are spent
        self.assertEqual(self.ids()["events"][EV2]["Q0"]["id"], None)

    def test_quota_exceeded_keeps_what_was_read_and_blocks(self):
        self.api.fail_at = {3: (403, "quotaExceeded")}
        code, summary = self.run_pass()
        self.assertEqual(code, 1)
        self.assertIn("quota", summary)
        self.assertEqual(len(self.lines()), 2 * 50 - 1)               # two batches, one hidden
        blocked = self.state()["blocked_until"]
        self.assertGreater(blocked, NOW)
        code, summary = self.run_pass(at=NOW + 1200)
        self.assertEqual(code, 0)
        self.assertIn("blocked", summary)

    def test_gap_guard_and_the_collector_seed_only_once(self):
        self.run_pass()
        code, summary = self.run_pass(at=NOW + 60)
        self.assertIn("skip", summary)
        with open(self.src, "w", encoding="utf-8") as f:
            json.dump({"P:late": {"id": "late1", "yt_title": "Late", "channel": "L"}}, f)
        self.run_pass(at=NOW + 1200)
        self.assertNotIn("late1", self.ids()["videos"])


class TestMain(Base):
    def test_dry_run_status_and_the_lock(self):
        self.assertEqual(feed.main(["--dry-run"]), 0)
        self.assertFalse(os.path.exists(feed.OUT_FILE))
        self.run_pass()
        self.assertIn("tracked videos", feed.status_text(NOW + 60))
        held = feed.single_instance()
        try:
            self.assertEqual(feed.main([]), 3)
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
