"""Tests for approval_fair.py (the RCP Trump-approval fair for KXTRUMPAPPROVE
and KXAPRPOTUS). No network: the RCP feed and Kalshi are handed in."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import approval_fair as af

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)          # Sunday 22:00 ET
DAILY = "KXTRUMPAPPROVE-26OCT26"
WEEKLY = "KXAPRPOTUS-26OCT30"


def cms(dt: datetime) -> str:
    """RCP's stamp, in Central daylight time."""
    return (dt.astimezone(timezone(timedelta(hours=-5)))
            .strftime("%a, %d %b %Y %H:%M:%S -0500"))


def feed(values=(39, 42, 41, 37, 34, 42, 39, 34, 40, 42, 36, 38, 36), avg="38.5",
         weeks=8, now=NOW, extra=(), avg_id="8656") -> dict:
    """A feed shaped like RCP's: the 2nd-term table (its Average row, the
    in-average polls, older polls that date one session every weekday 10:00
    ET for `weeks` weeks), then the 1st term's table."""
    rows = [{"id": avg_id, "type": "rcp_average", "pollster": "rcp_average",
             "date": "10/9 - 10/23",
             "candidate": [{"name": "Approve", "value": avg}, {"name": "Disapprove",
                                                                "value": "60.2"}]}]
    pid = 160000
    thu = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)          # Thursday 10:00 ET
    for i, v in enumerate(values):
        pid += 1
        rows.append({"id": str(pid), "type": "poll_rcp_avg", "pollster": f"Pollster {i}",
                     "data_start_date": "2026/10/12", "data_end_date": "2026/10/19",
                     "updated": cms(thu + timedelta(minutes=3 * i)),
                     "candidate": [{"name": "Approve", "value": str(v)},
                                   {"name": "Disapprove", "value": "58"}]})
    rid = 150000                                             # ids rise with entry time
    d = now - timedelta(days=7 * weeks)
    while d < now - timedelta(days=5):
        if d.weekday() < 5:
            rid += 1
            at = datetime(d.year, d.month, d.day, 14, 0, tzinfo=UTC)       # 10:00 ET
            rows.append({"id": str(rid), "type": "poll", "pollster": "Rasmussen Reports",
                         "data_start_date": "2026/09/01", "data_end_date": "2026/09/05",
                         "updated": cms(at),
                         "candidate": [{"name": "Approve", "value": "43"}]})
        d += timedelta(days=1)
    rows.extend(extra)
    rows.append({"id": "6179", "type": "rcp_average", "pollster": "rcp_average",
                 "date": "1/9 - 1/19", "candidate": [{"name": "Approve", "value": "41.1"}]})
    rows.append({"id": "131753", "type": "poll_rcp_avg", "pollster": "Economist/YouGov",
                 "updated": "Wed, 20 Jan 2021 08:37:21 -0600",
                 "candidate": [{"name": "Approve", "value": "44"}]})
    return {"moduleInfo": {"id": "8656", "lastBuildDate": "Sun, 25 Oct 2026 11:50:25 -0500"},
            "poll": rows}


def daily_markets(v_lo=385, n=7, read_at="2026-10-26T17:00:00Z", event=DAILY):
    """The nine KXTRUMPAPPROVE buckets around 38.5 in the module's shape."""
    out = {f"{event}-U{v_lo / 10:.1f}": {"event": event, "series": "KXTRUMPAPPROVE",
                                          "lo": None, "hi": v_lo - 1, "read_at": read_at,
                                          "bid": 31.0, "ask": 33.0}}
    for k in range(n):
        v = v_lo + k
        out[f"{event}-E{v / 10:.1f}"] = {"event": event, "series": "KXTRUMPAPPROVE", "lo": v,
                                         "hi": v, "read_at": read_at, "bid": 5.0, "ask": 6.0}
    top = v_lo + n - 1
    out[f"{event}-A{top / 10:.1f}"] = {"event": event, "series": "KXTRUMPAPPROVE",
                                        "lo": top + 1, "hi": None, "read_at": read_at,
                                        "bid": 2.0, "ask": 3.0}
    return out


class Getter:
    """A get_text stand-in: a queue of responses (text, None for 304, or an
    exception), recording the headers it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def text(js, etag='W/"abc"'):
    return json.dumps(js), {"ETag": etag, "Last-Modified": "Sun, 25 Oct 2026 16:59:04 GMT"}


class Offline(unittest.TestCase):
    """Any real HTTP call fails the test."""

    def setUp(self):
        p = mock.patch.object(af.requests, "get", side_effect=AssertionError("network used"))
        p.start()
        self.addCleanup(p.stop)
        # no headlines unless a test hands some in (get_news)
        n = mock.patch.object(af, "fetch_news", return_value=[])
        n.start()
        self.addCleanup(n.stop)


class TestFeed(Offline):

    def test_reads_the_second_term_table(self):
        got = af.parse_feed(feed(), NOW)
        self.assertEqual(got["value"], 38.5)
        self.assertEqual(got["n_in"], 13)
        self.assertAlmostEqual(got["mean_in"], 500 / 13, places=3)
        self.assertEqual(got["dates"], "10/9 - 10/23")
        self.assertNotIn("131753", [p["id"] for p in got["polls_in"]])   # the 1st term's
        self.assertGreaterEqual(len(got["sessions"]), af.MIN_SESSIONS)

    def test_a_table_that_looks_wrong_is_a_fault(self):
        bad = [
            feed(avg="39.5"),                                   # not the mean of its polls
            feed(avg_id="9999"),                                # no 2nd-term Average row
            feed(values=(39, 40, 38), avg="39.0"),              # too few polls in it
            feed(values=(80,) * 6, avg="80.0"),                 # out of range
            feed(weeks=1),                                      # too few dated sessions
            {"poll": "nope"},
        ]
        for js in bad:
            with self.assertRaises(af.FeedError):
                af.parse_feed(js, NOW)
        js = feed()
        js["poll"][1]["candidate"] = [{"name": "Disapprove", "value": "61"}]
        with self.assertRaises(af.FeedError):
            af.parse_feed(js, NOW)
        with self.assertRaises(af.FeedError):                   # not JSON at all
            af.fetch_feed(Getter("<html>captcha</html>"), now=NOW)

    def test_304_and_validators(self):
        g = Getter(text(feed()))
        parsed, hdr = af.fetch_feed(g, now=NOW)
        self.assertEqual(parsed["value"], 38.5)
        self.assertEqual(hdr, {"etag": 'W/"abc"', "last_modified": "Sun, 25 Oct 2026 16:59:04 GMT"})
        g2 = Getter(None)
        self.assertEqual(af.fetch_feed(g2, etag='W/"abc"', last_modified="x", now=NOW), (None, {}))
        self.assertEqual(g2.calls[0][1], {"If-None-Match": 'W/"abc"', "If-Modified-Since": "x"})

    def test_cms_stamps(self):
        self.assertEqual(af._cms_ts("Fri, 23 Oct 2026 09:10:09 -0500"),
                         datetime(2026, 10, 23, 14, 10, 9, tzinfo=UTC).timestamp())
        self.assertEqual(af._cms_ts("Wed, 20 Jan 2021 08:37:21 -0600"),
                         datetime(2021, 1, 20, 14, 37, 21, tzinfo=UTC).timestamp())
        self.assertIsNone(af._cms_ts("yesterday"))

    def test_sessions_from_ids_and_stamps(self):
        t0 = datetime(2026, 10, 20, 14, 0, tzinfo=UTC).timestamp()
        entries = [(1, t0), (2, t0 + 120),          # one session (2 min apart)
                   (3, t0 + 7200 + 86400 * 3),      # an old poll edited 3 days later...
                   (4, t0 + 7200),                  # ...created before id 4 (2h after t0)
                   (5, t0 + 86400)]
        got = af.session_times(entries)
        self.assertEqual(got, [t0, t0 + 7200, t0 + 86400, t0 + 7200 + 86400 * 3])


class TestKalshi(Offline):

    def test_buckets(self):
        self.assertEqual(af.bucket({"strike_type": "less", "cap_strike": 38.5}), (None, 384))
        self.assertEqual(af.bucket({"strike_type": "between", "floor_strike": 38.5,
                                    "cap_strike": 38.5}), (385, 385))
        self.assertEqual(af.bucket({"strike_type": "greater", "floor_strike": 39.1}), (392, None))
        self.assertEqual(af.bucket({"strike_type": "between", "floor_strike": 37.7,
                                    "cap_strike": 37.9}), (377, 379))
        self.assertEqual(af.bucket({"strike_type": "less", "cap_strike": 37.7}), (None, 376))
        self.assertEqual(af.bucket({"strike_type": "greater", "floor_strike": 39.4}), (395, None))
        self.assertIsNone(af.bucket({"strike_type": "custom"}))

    def test_read_times(self):
        d = {"rules_primary": "If Trump's RCP approval average is below 38.5% at exactly "
                              "1:00 PM ET on Oct 26, 2026, then the market resolves to Yes.",
             "event_ticker": DAILY}
        self.assertEqual(af.read_time(d, "KXTRUMPAPPROVE"),
                         datetime(2026, 10, 26, 17, 0, tzinfo=UTC))
        w = {"rules_primary": "If the President's approval rating is above 39.4% at 11:00 AM ET "
                              "on October 30, 2026 according to RealClearPolitics, then ...",
             "event_ticker": WEEKLY}
        self.assertEqual(af.read_time(w, "KXAPRPOTUS"), datetime(2026, 10, 30, 15, 0, tzinfo=UTC))
        est = {"event_ticker": "KXAPRPOTUS-26NOV06", "rules_primary": ""}     # after DST ends
        self.assertEqual(af.read_time(est, "KXAPRPOTUS"), datetime(2026, 11, 6, 16, 0, tzinfo=UTC))
        self.assertEqual(af.event_date("KXTRUMPAPPROVE-26OCT26-E38.5"), date(2026, 10, 26))

    def test_fetch_markets(self):
        def get_json(path, params):
            if params["series_ticker"] == "KXTRUMPAPPROVE":
                return {"markets": [{"ticker": "KXTRUMPAPPROVE-26OCT26-E38.5",
                                     "event_ticker": DAILY, "strike_type": "between",
                                     "floor_strike": 38.5, "cap_strike": 38.5,
                                     "yes_bid_dollars": "0.3800", "yes_ask_dollars": "0.4100",
                                     "rules_primary": "at exactly 1:00 PM ET on Oct 26, 2026"}]}
            return {"markets": [{"ticker": "KXAPRPOTUS-26OCT30-39.5", "event_ticker": WEEKLY,
                                 "strike_type": "greater", "floor_strike": 39.4}]}
        got = af.fetch_markets(get_json)
        self.assertEqual(got["KXTRUMPAPPROVE-26OCT26-E38.5"],
                         {"event": DAILY, "series": "KXTRUMPAPPROVE", "lo": 385, "hi": 385,
                          "read_at": "2026-10-26T17:00:00Z", "bid": 38.0, "ask": 41.0})
        self.assertEqual(got["KXAPRPOTUS-26OCT30-39.5"]["read_at"], "2026-10-30T15:00:00Z")


class TestModel(Offline):

    def setUp(self):
        super().setUp()
        self.sessions = af.parse_feed(feed(), NOW)["sessions"]
        self.p = af.slot_probs(self.sessions, NOW)

    def test_slot_probabilities(self):
        self.assertGreater(self.p[(0, 10)], 0.95)               # every weekday 10 ET
        self.assertLess(self.p[(5, 10)], 0.02)                   # never on Saturdays
        self.assertAlmostEqual(self.p[(2, 3)], 1 - (1 - af.SLOT_PRIOR / (8 + 2 * af.SLOT_PRIOR))
                               * (1 - af.BG_PER_HOUR), places=6)

    def test_distribution_shape(self):
        t1 = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)           # Monday 13:00 ET
        cnt = af.session_count_pmf(self.p, NOW, t1)
        self.assertAlmostEqual(sum(cnt), 1.0, places=9)
        pmf = af.delta_pmf(cnt)
        self.assertAlmostEqual(sum(pmf.values()), 1.0, places=9)
        for k in range(1, 6):
            self.assertAlmostEqual(pmf.get(k, 0.0), pmf.get(-k, 0.0), places=9)
        self.assertEqual(af.delta_pmf([1.0]), {0: 1.0})          # no session can come
        # a Saturday night is quiet; the Monday morning entry window is not
        sat = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
        quiet = af.delta_pmf(af.session_count_pmf(self.p, sat, sat + timedelta(hours=15)))
        busy = af.delta_pmf(af.session_count_pmf(self.p, NOW, t1))
        self.assertGreater(quiet[0], 0.75)
        self.assertLess(busy[0], quiet[0] - 0.3)
        # a full partition of buckets sums to one
        buckets = [(None, 384)] + [(v, v) for v in range(385, 392)] + [(392, None)]
        self.assertAlmostEqual(sum(af.p_bucket(385, busy, lo, hi) for lo, hi in buckets), 1.0,
                               places=9)


class TestReleaseMatch(unittest.TestCase):
    """Which headlines announce a sponsor's new national poll (Jack
    2026-10-26: "Build it", the released-but-not-entered hold)."""

    def test_the_10_19_to_25_headlines(self):
        m = af.match_release
        self.assertEqual(m("CNN", "Trump's approval rating hits a new low as midterms enter "
                                  "final stretch: CNN poll - CNN"), "CNN")
        self.assertEqual(m("CNBC", "Trump's approval rating on economy hits all-time low as "
                                   "midterm looms: CNBC survey - CNBC"), "CNBC")
        self.assertEqual(m("Financial Times", "US voters worried about corruption under "
                                              "Trump — FT poll - Financial Times"),
                         "Financial Times")
        self.assertEqual(m("The New York Times", "Trump Approval Hits New Low Two Weeks "
                                                 "Before Midterms, Poll Finds"),
                         "NY Times/Siena")                      # its own poll, own outlet
        self.assertEqual(m("www.ap.org", "Frustrated with Trump, voters overwhelmingly want "
                                         "change in Washington, an AP-NORC poll finds"),
                         "AP/NORC")
        # not a new national poll
        self.assertIsNone(m("The Independent", "Trump's tumbling poll numbers leave CNN "
                                               "data guru stunned"))
        self.assertIsNone(m("ROI-NJ", "Stockton poll: Most N.J. voters disapprove of "
                                      "President Trump's job performance"))
        self.assertIsNone(m("CBS News", "Breaking down new poll showing tight Senate race "
                                        "in Texas"))
        self.assertIsNone(m("CBS News", "Polls show Latino voters moving away from "
                                        "President Trump"))         # plural: others' polls
        self.assertIsNone(m("Newsweek", "Trump's Approval Rating Hits Record Low in 2 New "
                                        "Polls Days Before Midterms"))
        self.assertIsNone(m("Silver Bulletin", "How popular is Donald Trump?"))


class TestFile(Offline):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "approval_fair.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, getter, now=NOW, markets=None, get_json=None):
        return af.write_fair_file(self.path, now=now, get_text=getter, get_json=get_json,
                                  markets=markets if markets is not None else daily_markets())

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_writes_the_loader_shape(self):
        ok, miss = self.write(Getter(text(feed())))
        self.assertEqual((ok, miss), (9, 0))
        js = self.read()
        ps = [m["p"] for m in js["markets"].values()]
        self.assertAlmostEqual(sum(ps), 1.0, places=3)
        m = js["markets"]["KXTRUMPAPPROVE-26OCT26-E38.5"]
        self.assertEqual((m["event"], m["fetched_at"]), (DAILY, "2026-10-26T02:00:00Z"))
        self.assertTrue(0.0 < m["p"] < 1.0)
        self.assertEqual(js["rcp"]["value"], 38.5)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_fetched_at_is_the_last_good_read(self):
        self.write(Getter(text(feed())))
        g = Getter(None)                                        # 304: unchanged
        self.write(g, now=NOW + timedelta(minutes=3))
        self.assertEqual(g.calls[0][1]["If-None-Match"], 'W/"abc"')
        m = self.read()["markets"]["KXTRUMPAPPROVE-26OCT26-E38.5"]
        self.assertEqual(m["fetched_at"], "2026-10-26T02:03:00Z")
        self.write(Getter(OSError("timed out")), now=NOW + timedelta(minutes=6))
        m = self.read()["markets"]["KXTRUMPAPPROVE-26OCT26-E38.5"]
        self.assertEqual(m["fetched_at"], "2026-10-26T02:03:00Z")   # not refreshed
        g2 = Getter(text(feed()))
        self.write(g2, now=NOW + timedelta(minutes=6, seconds=30))  # inside RCP_MIN_READ_SECS
        self.assertEqual(g2.calls, [])

    def test_a_bad_read_drops_every_fair_until_a_good_one(self):
        self.write(Getter(text(feed())))
        ok, miss = self.write(Getter(text(feed(avg="40.0"))), now=NOW + timedelta(minutes=3))
        js = self.read()
        self.assertEqual((ok, miss, js["markets"]), (0, 1, {}))
        self.assertIn("not the mean", js["fault"])
        g = Getter(text(feed()))
        ok, _ = self.write(g, now=NOW + timedelta(minutes=6))
        self.assertNotIn("If-None-Match", g.calls[0][1])          # a full read after a fault
        self.assertEqual(ok, 9)
        self.assertIsNone(self.read()["fault"])

    def test_a_big_move_waits_for_a_second_read(self):
        self.write(Getter(text(feed())))
        moved = feed(values=(41, 44, 43, 39, 36, 44, 41, 36, 42, 44, 38, 40, 38), avg="40.5")
        ok, _ = self.write(Getter(text(moved, etag='W/"new"')), now=NOW + timedelta(minutes=3))
        js = self.read()
        self.assertEqual(ok, 0)
        self.assertEqual((js["rcp"]["value"], js["rcp"]["unconfirmed"]), (38.5, 40.5))
        ok, _ = self.write(Getter(None), now=NOW + timedelta(minutes=6))   # 304 confirms it
        js = self.read()
        self.assertEqual((ok, js["rcp"]["value"]), (9, 40.5))
        self.assertEqual(js["history"][-1], ["2026-10-26T02:06:00Z", 40.5])

    def test_a_released_poll_holds_the_daily_for_an_hour(self):
        """The daily stands aside (no fair) for HOLD_H after a sponsor's poll
        comes out, whether or not RCP enters it meanwhile (Jack 2026-10-26:
        "Just hold for 1 hour regardless if it's added to RCP or not"); the
        weekly stays priced."""
        out_at = NOW - timedelta(minutes=30)
        news = [{"ts": out_at.timestamp(), "source": "CNN", "link": "https://x",
                 "title": "Trump approval hits a new low: CNN poll - CNN"}]
        weekly = {f"{WEEKLY}-B38.5": {"event": WEEKLY, "series": "KXAPRPOTUS", "lo": 384,
                                      "hi": 386, "read_at": "2026-10-30T15:00:00Z",
                                      "bid": 30.0, "ask": 34.0}}
        mk = dict(daily_markets(), **weekly)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, miss = af.write_fair_file(self.path, now=NOW, get_text=Getter(text(feed())),
                                          markets=mk, get_news=lambda: news)
        js = self.read()
        self.assertEqual((ok, miss), (1, 1))                     # the weekly only
        self.assertIn("release hold: CNN", js["event_fair"][DAILY]["why"])
        self.assertIn("(held 1 h)", js["event_fair"][DAILY]["why"])
        self.assertEqual([h["key"] for h in js["release_holds"]], ["CNN"])
        self.assertIn(f"{WEEKLY}-B38.5", js["markets"])
        # RCP enters CNN's poll 35 min after its release: the daily stays out
        cnn = {"id": "170001", "type": "poll", "pollster": "CNN",
               "data_start_date": "2026/10/22", "data_end_date": "2026/10/24",
               "updated": cms(NOW + timedelta(minutes=5)),
               "candidate": [{"name": "Approve", "value": "35"}]}
        later = NOW + timedelta(minutes=10)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, _ = af.write_fair_file(self.path, now=later,
                                       get_text=Getter(text(feed(now=later, extra=[cnn]),
                                                            etag='W/"cnn"')),
                                       markets=mk, get_news=lambda: news)
        js = self.read()
        self.assertEqual((ok, [h["key"] for h in js["release_holds"]]), (1, ["CNN"]))
        # an hour after the release the hold ends and the daily is priced again
        end = out_at + timedelta(hours=af.HOLD_H, minutes=1)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, _ = af.write_fair_file(self.path, now=end, get_text=Getter(None),
                                       markets=mk, get_news=lambda: news)
        js = self.read()
        self.assertEqual(ok, 10)
        self.assertEqual(js["release_holds"], [])
        self.assertEqual(js["release_log"][-1]["how"], "time")
        # a follow-up CNN headline on the same poll does not hold again
        again = [{"ts": end.timestamp(), "source": "CNN", "link": "",
                  "title": "Trump approval hits a new low: CNN poll - CNN"}]
        with contextlib.redirect_stdout(io.StringIO()):
            ok, _ = af.write_fair_file(self.path, now=end + timedelta(minutes=6),
                                       get_text=Getter(None), markets=mk, get_news=lambda: again)
        self.assertEqual((ok, self.read()["release_holds"]), (10, []))

    def test_a_hold_ends_an_hour_after_the_release_and_old_news_is_skipped(self):
        out_at = NOW - timedelta(minutes=30)
        fox = "Fox News poll: Trump approval at 41% - Fox News"
        self.assertEqual(af.match_release("Fox News", fox), "FOX News")
        news = [{"ts": out_at.timestamp(), "source": "CNBC",
                 "link": "", "title": "Trump's approval slips: CNBC survey - CNBC"},
                {"ts": (NOW - timedelta(minutes=70)).timestamp(), "source": "Fox News",
                 "link": "", "title": fox}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.write(Getter(text(feed())))                     # no news yet (Offline)
            af.write_fair_file(self.path, now=NOW + timedelta(minutes=6), get_text=Getter(None),
                               markets=daily_markets(), get_news=lambda: news)
        # Fox's headline is over an hour old when first read: its hour is over
        self.assertEqual([h["key"] for h in self.read()["release_holds"]], ["CNBC"])
        end = out_at + timedelta(hours=af.HOLD_H, minutes=1)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, _ = af.write_fair_file(self.path, now=end, get_text=Getter(text(feed(now=end))),
                                       markets=daily_markets(), get_news=lambda: news)
        js = self.read()
        self.assertEqual((ok, js["release_holds"], js["release_log"][-1]["how"]), (9, [], "time"))

    def test_coverage_of_an_entered_poll_does_not_hold(self):
        # RCP entered CNN 11 h before the headline: the article is about that poll
        cnn = {"id": "170001", "type": "poll", "pollster": "CNN",
               "data_start_date": "2026/10/22", "data_end_date": "2026/10/24",
               "updated": cms(NOW - timedelta(hours=11)),
               "candidate": [{"name": "Approve", "value": "35"}]}
        news = [{"ts": (NOW - timedelta(minutes=20)).timestamp(), "source": "CNN", "link": "",
                 "title": "What the new CNN poll says about Trump - CNN"}]
        with contextlib.redirect_stdout(io.StringIO()):
            ok, _ = af.write_fair_file(self.path, now=NOW,
                                       get_text=Getter(text(feed(extra=[cnn]))),
                                       markets=daily_markets(), get_news=lambda: news)
        self.assertEqual((ok, self.read()["release_holds"]), (9, []))

    def test_a_poll_rcp_enters_at_once_still_holds_its_hour(self):
        # an entry 10 min after the headline, or 80 min before it (the search
        # lags some releases), is this poll: it does not cut the hour short
        for entered in (NOW - timedelta(minutes=10), NOW - timedelta(minutes=100)):
            with self.subTest(entered=entered):
                if os.path.exists(self.path):
                    os.remove(self.path)
                cnn = {"id": "170001", "type": "poll", "pollster": "CNN",
                       "data_start_date": "2026/10/22", "data_end_date": "2026/10/24",
                       "updated": cms(entered),
                       "candidate": [{"name": "Approve", "value": "35"}]}
                news = [{"ts": (NOW - timedelta(minutes=20)).timestamp(), "source": "CNN",
                         "link": "", "title": "Trump approval hits a new low: CNN poll - CNN"}]
                with contextlib.redirect_stdout(io.StringIO()):
                    ok, miss = af.write_fair_file(self.path, now=NOW,
                                                  get_text=Getter(text(feed(extra=[cnn]))),
                                                  markets=daily_markets(),
                                                  get_news=lambda: news)
                self.assertEqual((ok, miss), (0, 1))
                self.assertEqual([h["key"] for h in self.read()["release_holds"]], ["CNN"])

    def test_a_past_read_time_is_not_priced(self):
        mk = daily_markets(read_at="2026-10-25T17:00:00Z", event="KXTRUMPAPPROVE-26OCT25")
        ok, miss = self.write(Getter(text(feed())), markets=mk)
        self.assertEqual((ok, miss), (0, 1))

    def test_settlement_cross_check(self):
        rt = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
        settled = {"value": None}

        def get_json(path, params):
            v = settled["value"]
            if v is None:
                return {"markets": []}
            return {"markets": [{"ticker": "x", "result": "yes", "strike_type": "between",
                                 "floor_strike": v, "cap_strike": v},
                                {"ticker": "y", "result": "no", "strike_type": "greater",
                                 "floor_strike": 39.1}]}
        mk = daily_markets()
        for minutes in (-10, -6, -2, 2, 6):                     # reads across 1:00 PM ET
            self.write(Getter(None) if minutes > -10 else Getter(text(feed())),
                       now=rt + timedelta(minutes=minutes), markets=mk)
        tomorrow = daily_markets(read_at="2026-10-27T17:00:00Z", event="KXTRUMPAPPROVE-26OCT27")
        settled["value"] = 38.7                                 # Kalshi settled another bucket
        ok, miss = self.write(Getter(None), now=rt + timedelta(minutes=40), markets=tomorrow,
                              get_json=get_json)
        js = self.read()
        self.assertEqual(js["xcheck"][DAILY]["status"], "MISMATCH")
        self.assertEqual((ok, miss), (0, 1))
        self.assertIn("cross-check", js["event_fair"]["KXTRUMPAPPROVE-26OCT27"]["why"])
        # the next day's settlement agrees: fairs come back
        rt2 = rt + timedelta(days=1)
        for minutes in (-8, -4, 0, 4, 8):
            self.write(Getter(None), now=rt2 + timedelta(minutes=minutes), markets=tomorrow,
                       get_json=get_json)
        settled["value"] = 38.5
        nxt = daily_markets(read_at="2026-10-28T17:00:00Z", event="KXTRUMPAPPROVE-26OCT28")
        ok, miss = self.write(Getter(None), now=rt2 + timedelta(minutes=45), markets=nxt,
                              get_json=get_json)
        js = self.read()
        self.assertEqual(js["xcheck"]["KXTRUMPAPPROVE-26OCT27"]["status"], "ok")
        self.assertEqual((ok, miss), (9, 0))

    def test_main_prints_the_dry_run(self):
        out = os.path.join(self.dir, "dry.json")
        buf = io.StringIO()
        with mock.patch.object(af, "_http_get", Getter(text(feed()))), \
                mock.patch.object(af, "fetch_markets", lambda get_json=None: daily_markets()), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(af.main(["--out", out, "--now", "2026-10-26T02:00:00Z"]), 0)
        printed = buf.getvalue()
        self.assertIn("RCP Average 38.5", printed)
        self.assertIn("KXTRUMPAPPROVE-26OCT26-U38.5", printed)
        self.assertIn("book 31 x 33", printed)
        self.assertIn("9 markets priced, 0 events without a fair", printed)
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
