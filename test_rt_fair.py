"""Tests for rt_fair (the KXRT Rotten Tomatoes fair-value writer, 2026-10-12)."""
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import rt_fair as r

UTC = timezone.utc


def page(liked, n, score=None):
    sc = {"criticsScore": {"likedCount": liked, "reviewCount": n, "score": score},
          "overlay": {"criticsAll": {"likedCount": liked, "notLikedCount": n - liked,
                                     "reviewCount": n,
                                     "score": None if score is None else str(score)},
                      "criticsTop": {"likedCount": 1, "reviewCount": 2}}}
    return ('<html><script id="media-scorecard-json" data-json="x" type="application/json">'
            + json.dumps(sc) + "</script></html>")


def bb_tail_brute(M, a, b, t):
    lb = lambda x, y: math.lgamma(x) + math.lgamma(y) - math.lgamma(x + y)
    return sum(math.exp(math.lgamma(M + 1) - math.lgamma(x + 1) - math.lgamma(M - x + 1)
                        + lb(x + a, M - x + b) - lb(a, b)) for x in range(max(t, 0), M + 1))


class TestScorecard(unittest.TestCase):
    def test_parses_the_all_critics_overlay(self):
        self.assertEqual(r.parse_scorecard(page(39, 41, 95)),
                         {"liked": 39, "n": 41, "score": 95})
        self.assertEqual(r.parse_scorecard(page(0, 0)), {"liked": 0, "n": 0, "score": None})
        self.assertIsNone(r.parse_scorecard("<html>no card</html>"))
        self.assertIsNone(r.parse_scorecard(
            '<script id="media-scorecard-json">{not json</script>'))
        self.assertIsNone(r.parse_scorecard(page(9, 5)))         # liked > reviews


class TestModel(unittest.TestCase):
    def test_threshold_is_round_half_up(self):
        # (<= 0: above the strike with no more fresh reviews)
        # 1 of 8 = 12.5% displays 13: above 12, one fresh short of 13
        self.assertLessEqual(r.yes_threshold(1, 8, 12), 0)
        self.assertEqual(r.yes_threshold(1, 8, 13), 1)
        # 132 of 259 = 50.97 -> 51 (Digger's settlement): above 50; above 51
        # takes 134 (133/259 = 51.35 still shows 51)
        self.assertLessEqual(r.yes_threshold(132, 259, 50), 0)
        self.assertEqual(r.yes_threshold(132, 259, 51), 2)
        # a score of exactly 75 is not above 75 (the rules' example)
        self.assertLessEqual(r.yes_threshold(3, 4, 74), 0)
        self.assertEqual(r.yes_threshold(3, 4, 75), 1)

    def test_beta_binomial_tail_matches_brute_force(self):
        for M, a, b in ((7, 2.5, 1.5), (30, 12.0, 5.0), (1, 0.5, 0.5)):
            th = [-2, 0, 1, M // 2, M, M + 1]
            got = r._bb_tail(M, a, b, th)
            for t, g in zip(th, got):
                self.assertAlmostEqual(g, min(1.0, bb_tail_brute(M, a, b, t)), places=9)

    def test_negative_binomial_points(self):
        self.assertEqual(r._nb_points(0.0, 4.0), [(0, 1.0)])
        pts = r._nb_points(20.0, 4.0)                     # exact support
        self.assertAlmostEqual(sum(w for _, w in pts), 1.0, places=9)
        self.assertAlmostEqual(sum(m * w for m, w in pts), 20.0, delta=0.2)
        big = r._nb_points(600.0, 4.0)                    # quantile points
        self.assertLessEqual(len(big), r.QUANTILES + 1)
        self.assertAlmostEqual(sum(w for _, w in big), 1.0, places=9)
        self.assertAlmostEqual(sum(m * w for m, w in big) / 600.0, 1.0, delta=0.02)

    def test_p_above_shape(self):
        ks = [float(k) for k in range(50, 100, 5)]
        ps = r.p_above(75, 80, ks, 20.0)                  # 94% at 20 days out
        vals = [ps[k] for k in ks]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in vals))
        self.assertGreater(ps[80.0], 0.85)
        # an hour before the close with 300 reviews the score barely moves:
        # 240/300 = 80 exactly
        late = r.p_above(240, 300, [78.0, 79.0, 80.0, 81.0], 0.04)
        self.assertGreater(late[79.0], 0.75)
        self.assertLess(late[81.0], 0.1)
        # far out the same count is far less certain
        early = r.p_above(240, 300, [79.0, 81.0], 20.0)
        self.assertLess(early[79.0], late[79.0])
        self.assertGreater(early[81.0], late[81.0])

    def test_fit_rows(self):
        self.assertEqual(r.fit_row(0.5), r.FIT[0][1:])
        self.assertEqual(r.fit_row(500.0), r.FIT[-1][1:])
        his = [row[0] for row in r.FIT]
        self.assertEqual(his, sorted(his))


class TestPolling(unittest.TestCase):
    def test_cadence(self):
        self.assertEqual(r.poll_every(3.0, 0), r.POLL_RELEASE_WEEK_SECS)
        self.assertEqual(r.poll_every(10.0, 0), r.POLL_EMBARGO_SECS)
        self.assertEqual(r.poll_every(40.0, 0), r.POLL_FAR_SECS)
        self.assertEqual(r.poll_every(40.0, 12), r.POLL_REVIEWED_SECS)

    def test_jump(self):
        self.assertFalse(r.is_jump(None, 50))             # first read ever
        self.assertTrue(r.is_jump(0, r.MIN_JUMP))
        # a film's first reviews are a jump whatever their number (Jack
        # 2026-10-15: "ship 1 - 4" -- KXRT-KLA's first 8 crashed the book)
        self.assertTrue(r.is_jump(0, r.MIN_JUMP - 1))
        self.assertTrue(r.is_jump(0, 1))
        self.assertFalse(r.is_jump(0, 0))
        self.assertFalse(r.is_jump(8, 9))                 # later trickles: the old rule
        self.assertTrue(r.is_jump(40, 80))
        self.assertFalse(r.is_jump(40, 55))               # +15 but not +50%


class TestWrite(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 22, 0, tzinfo=UTC)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rt_fair.json")
        far = (self.NOW + timedelta(days=28)).isoformat()
        near = (self.NOW + timedelta(days=3)).isoformat()
        self.events = {
            "KXRT-GOD": {"title": "Godzilla Minus Zero", "close": far,
                         "url": "https://www.rottentomatoes.com/m/godzilla_minus_zero",
                         "strikes": {"KXRT-GOD-90": 90.0, "KXRT-GOD-94": 94.0}},
            "KXRT-AVE": {"title": "Avengers: Doomsday", "close": far,
                         "url": "https://www.rottentomatoes.com/m/avengers_doomsday",
                         "strikes": {"KXRT-AVE-80": 80.0}},
            "KXRT-EBE": {"title": "Ebenezer", "close": far,
                         "url": "https://www.rottentomatoes.com/",
                         "strikes": {"KXRT-EBE-60": 60.0}},
            "KXRT-NEW": {"title": "A New Film", "close": near,
                         "url": "https://www.rottentomatoes.com/",
                         "strikes": {"KXRT-NEW-60": 60.0}}}
        self.counts = {"godzilla_minus_zero": (39, 41), "avengers_doomsday": (0, 0),
                       "ebenezer_2026": (3, 4)}
        self.reads = []

    def film(self, slug):
        self.reads.append(slug)
        liked, n = self.counts[slug]
        return {"liked": liked, "n": n, "score": None}

    def write(self, now, **kw):
        kw.setdefault("search_fn", lambda title, year: None)
        return r.write_fair_file(self.path, now=now, events=self.events,
                                 film_fn=self.film, **kw)

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_prices_reviewed_films_only(self):
        ok, miss = self.write(self.NOW)
        d = self.load()
        self.assertEqual(ok, 2)
        g90 = d["markets"]["KXRT-GOD-90"]
        self.assertEqual((g90["event"], g90["n"], g90["fetched_at"]),
                         ("KXRT-GOD", 41, self.NOW.isoformat()))
        self.assertGreater(g90["p"], d["markets"]["KXRT-GOD-94"]["p"])
        # no reviews yet / too few: listed without a price (the gate quotes as before)
        self.assertIsNone(d["markets"]["KXRT-AVE-80"]["p"])
        self.assertIsNone(d["markets"]["KXRT-EBE-60"]["p"])     # the override slug
        self.assertEqual(d["films"]["KXRT-EBE"]["slug"], "ebenezer_2026")
        self.assertEqual(d["missing"]["KXRT-NEW-60"], "no RT page for this event")
        self.assertNotIn("KXRT-NEW-60", d["markets"])

    def test_reads_are_paced_and_jumps_stamped(self):
        self.write(self.NOW)
        self.assertEqual(sorted(self.reads), ["avengers_doomsday", "ebenezer_2026",
                                              "godzilla_minus_zero"])
        self.reads.clear()
        self.write(self.NOW + timedelta(seconds=90))
        self.assertEqual(self.reads, [])                    # nothing due yet
        # the Avengers embargo lifts: 0 -> 60 reviews at the next due read
        self.counts["avengers_doomsday"] = (48, 60)
        later = self.NOW + timedelta(seconds=r.POLL_FAR_SECS)
        self.write(later)
        self.assertIn("avengers_doomsday", self.reads)
        d = self.load()
        f = d["films"]["KXRT-AVE"]
        self.assertEqual((f["jump_at"], f["jump_from"], f["first_reviews_at"]),
                         (later.isoformat(), 0, later.isoformat()))
        self.assertEqual(d["markets"]["KXRT-AVE-80"]["jump_at"], later.isoformat())
        self.assertIsNotNone(d["markets"]["KXRT-AVE-80"]["p"])

    def test_a_first_small_batch_is_stamped_a_jump(self):
        """0 -> 8 reviews (fewer than MIN_JUMP): jump_at and first_reviews_at
        are stamped, the strike stays unpriced with its count for the bot."""
        self.write(self.NOW)
        self.counts["avengers_doomsday"] = (1, 8)
        later = self.NOW + timedelta(seconds=r.POLL_FAR_SECS)
        self.write(later)
        d = self.load()
        f = d["films"]["KXRT-AVE"]
        self.assertEqual((f["jump_at"], f["jump_from"], f["first_reviews_at"]),
                         (later.isoformat(), 0, later.isoformat()))
        m = d["markets"]["KXRT-AVE-80"]
        self.assertEqual((m["p"], m["n"], m["jump_at"]), (None, 8, later.isoformat()))
        self.assertEqual(d["model"]["min_reviews"], r.MIN_REVIEWS)

    def test_a_failed_read_keeps_the_last_counts(self):
        self.write(self.NOW)
        def down(slug):
            raise ConnectionError("RT down")
        later = self.NOW + timedelta(seconds=r.POLL_REVIEWED_SECS)
        ok, _ = r.write_fair_file(self.path, now=later, events=self.events,
                                  film_fn=down, search_fn=lambda t, y: None)
        self.assertEqual(ok, 2)
        m = self.load()["markets"]["KXRT-GOD-90"]
        self.assertEqual(m["fetched_at"], self.NOW.isoformat())   # the gate's TTL ages it

    def test_search_resolves_a_root_url_once(self):
        calls = []
        def search(title, year):
            calls.append((title, year))
            return "a_new_film_2026"
        self.counts["a_new_film_2026"] = (20, 25)
        self.write(self.NOW, search_fn=search)
        self.write(self.NOW + timedelta(hours=1), search_fn=search)
        self.assertEqual(calls, [("A New Film", 2026)])
        d = self.load()
        self.assertEqual(d["films"]["KXRT-NEW"]["slug"], "a_new_film_2026")
        self.assertIsNotNone(d["markets"]["KXRT-NEW-60"]["p"])


class TestKalshiAndSearch(unittest.TestCase):
    def test_fetch_events(self):
        js = {"events": [
            {"event_ticker": "KXRT-GOD", "sub_title": "Godzilla Minus Zero",
             "settlement_sources": [{"url": "https://www.rottentomatoes.com/m/godzilla_minus_zero"}],
             "markets": [
                 {"ticker": "KXRT-GOD-90", "strike_type": "greater", "floor_strike": 90,
                  "close_time": "2026-11-09T15:00:00Z"},
                 {"ticker": "KXRT-GOD-X", "strike_type": "between", "floor_strike": 1,
                  "close_time": "2026-11-09T15:00:00Z"}]}],
            "cursor": ""}
        seen = []
        def get_json(path, params):
            seen.append((path, params["series_ticker"], params["with_nested_markets"]))
            return js
        ev = r.fetch_events(get_json)
        self.assertEqual(seen, [("/events", "KXRT", "true")])
        self.assertEqual(ev["KXRT-GOD"]["strikes"], {"KXRT-GOD-90": 90.0})
        self.assertEqual(ev["KXRT-GOD"]["close"], "2026-11-09T15:00:00Z")

    def test_search_slug_wants_one_exact_title_and_year(self):
        def row(year, slug, name):
            return (f'<search-page-media-row release-year="{year}" tomatometer-score="">'
                    f'<a href="https://www.rottentomatoes.com/m/{slug}" data-qa="info-name">'
                    f' {name} </a></search-page-media-row>')
        html = (row(2026, "ebenezer_2026", "Ebenezer") + row(1997, "ebenezer", "Ebenezer")
                + row(2026, "ebenezer_scrooge_x", "Ebenezer Scrooge: X"))
        resp = mock.Mock(text=html, status_code=200)
        resp.raise_for_status = lambda: None
        with mock.patch.object(r.requests, "get", return_value=resp):
            self.assertEqual(r.search_slug("Ebenezer", 2026), "ebenezer_2026")
            self.assertIsNone(r.search_slug("Ebenezer", 2030))
            self.assertIsNone(r.search_slug("Godzilla", 2026))


if __name__ == "__main__":
    unittest.main()
