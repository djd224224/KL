#!/usr/bin/env python3
"""Unit tests for earnings_announcements.py -- an earnings call's date and time
from the company's own announcement (Jack 2026-09-27) -- and for its use in
imm_earnings_overrides.announced_release().

Run: python -m unittest test_earnings_announcements

The fixtures are the real notices, trimmed (contacts and dial-ins replaced):
Domino's (Nasdaq copy of a PR Newswire release), Carnival (PR Newswire), Nike
(Business Wire), Aritzia (its own Q4-hosted IR page, CNW), and Aritzia's Q4
event feed. Each one carries the "Published <date> <time> EDT" page stamp that
fooled the old parse_call_time() into a 4:05pm / 11:56am / 5:00pm "call".

What matters is the direction of every error: an anchor that is too EARLY
only stands the bot down early; one that is too LATE quotes through a call.
"""

import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import earnings_announcements as ea                                 # noqa: E402

ET = ea.ET
NOW = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def et(y, mo, d, h, mi=0):
    return ET.localize(datetime(y, mo, d, h, mi))


DPZ = (
    "Domino's® Announces Q3 2026 Earnings Webcast Published Sep 10, 2026 "
    "4:05pm EDT linkedin facebook x email reddit ANN ARBOR, Mich. , Sept. 10, "
    "2026 /PRNewswire/ -- Domino's Pizza, Inc. (Nasdaq: DPZ) announces the "
    "following event: What: Domino's Third Quarter 2026 Earnings Webcast When: "
    "Tuesday, October 13 at 8:30 a.m. ET Where: ir.dominos.com How: Live "
    "webcast (web address above) Contact: Investor Relations "
    "investors@example.com This event will be archived on Domino's website for "
    "replay. Results and supplemental material will be distributed at 6:05 "
    "a.m. ET on October 13, 2026 , and will be available on our website. About "
    "Domino's Pizza Founded in 1960, Domino's Pizza is the largest pizza "
    "company in the world.")

CCL = (
    "CARNIVAL CORPORATION LTD. TO HOLD CONFERENCE CALL ON THIRD QUARTER "
    "EARNINGS Published Sep 15, 2026 11:56am EDT linkedin facebook MIAMI , "
    "Sept. 15, 2026 /PRNewswire/ -- Carnival Corporation Ltd. (NYSE: CCL) has "
    "scheduled a conference call with analysts for Tuesday, September 29, "
    "2026 , at 10 a.m. (EDT) to discuss the company's third quarter financial "
    "results which are expected to be released that morning. A simulcast of "
    "the call will be available via the company's website at "
    "www.carnivalcorp.com . About Carnival Corporation Ltd.")

NKE = (
    "NIKE, Inc. Announces First Quarter Fiscal 2027 Earnings and Conference "
    "Call Published Aug 28, 2026 5:00pm EDT linkedin facebook BEAVERTON, Ore. "
    "--(BUSINESS WIRE)-- NIKE, Inc. (NYSE: NKE) plans to release its first "
    "quarter fiscal 2027 financial results on Thursday, October 1, 2026 , at "
    "approximately 1:15 p.m. PT , following the close of regular stock market "
    "trading hours. Following the news release, NIKE, Inc. management will "
    "host a conference call beginning at 2:00 p.m. PT to review results. The "
    "conference call will be broadcast live over the Internet and can be "
    "accessed at https://investors.nike.com /. For those unable to listen to "
    "the live broadcast, an archived version will be available at the same "
    "location through 9:00 p.m. PT , October 29, 2026 . About NIKE, Inc.")

ATZ = (
    "<html><body><nav>View all Press Releases</nav><h1>Aritzia to Release "
    "Second Quarter Fiscal 2027 Financial Results</h1><p>September 24, 2026</p>"
    "<p>VANCOUVER, BC , Sept. 24, 2026 /CNW/ -- Aritzia Inc. (TSX: ATZ) will "
    "release its second quarter Fiscal 2027 financial results after market "
    "close on October 8, 2026. A conference call to discuss the earnings "
    "results will follow.</p><p>Conference Call Details</p><p>Date: Thursday, "
    "October 8, 2026<br>Time: 1:30pm PT / 4:30pm ET</p><p>To participate in the "
    "conference call: Please dial 1-800-000-0000. The call is also accessible "
    "via webcast at http://investors.aritzia.com/events-and-presentations/ . A "
    "recording will be available shortly after the conclusion of the call: "
    "Please dial 1-800-000-0001 and the replay access code 0000000. An archive "
    "of the webcast will be accessible on Aritzia's website.</p><p>About "
    "Aritzia</p><script>var t = '5:00pm ET October 2';</script></body></html>")

Q4_FEED = {"GetEventListResult": [
    {"Title": "Aritzia Second Quarter Fiscal 2027 Earnings Call",
     "StartDate": "10/08/2026 16:30:00", "TimeZone": "EST",
     "LinkToDetailPage": "/events-and-presentations/event-details/2026/q2"},
    {"Title": "Aritzia First Quarter Fiscal 2027 Earnings Call",
     "StartDate": "07/09/2026 13:30:00", "TimeZone": "PST",
     "LinkToDetailPage": "/q1"},
    {"Title": "Aritzia at the Global Retailing Conference",
     "StartDate": "09/30/2026 09:00:00", "TimeZone": "EST"},
    {"Title": "Annual General Meeting of Shareholders",
     "StartDate": "10/01/2026 11:00:00", "TimeZone": "PST"},
]}


class TestParseRealAnnouncements(unittest.TestCase):

    def parse(self, text):
        return ea.parse_announcement(text, NOW)

    def test_dominos_release_time_and_webcast(self):
        f = self.parse(DPZ)
        self.assertEqual(f["call"], et(2026, 10, 13, 8, 30))
        self.assertEqual((f["release"], f["release_how"]),
                         (et(2026, 10, 13, 6, 5), "stated"))
        # the whole point: 6:05, where the Nasdaq pre-market proxy says 7:00
        self.assertEqual(ea.anchor(f), et(2026, 10, 13, 6, 5))

    def test_carnival_released_that_morning(self):
        f = self.parse(CCL)
        self.assertEqual(f["call"], et(2026, 9, 29, 10))
        self.assertEqual(f["release_how"], "before open")
        self.assertEqual(ea.anchor(f), et(2026, 9, 29, 7))

    def test_nike_pacific_times_and_archive_window(self):
        f = self.parse(NKE)
        self.assertEqual(f["release"], et(2026, 10, 1, 16, 15))     # 1:15 PT
        self.assertEqual(f["call"], et(2026, 10, 1, 17))            # 2:00 PT,
        # dated from the release sentence; the Oct 29 archive end is ignored
        self.assertEqual(ea.anchor(f), et(2026, 10, 1, 16, 15))

    def test_aritzia_after_close_and_call_details_block(self):
        f = self.parse(ATZ)
        self.assertEqual((f["release"], f["release_how"]),
                         (et(2026, 10, 8, 16), "after close"))
        self.assertEqual(f["call"], et(2026, 10, 8, 16, 30))
        self.assertEqual(ea.anchor(f), et(2026, 10, 8, 16))

    def test_the_page_stamp_is_never_read(self):
        # "Published Sep 10, 2026 4:05pm EDT" is what parse_call_time() took
        # for Domino's call; the body starts after the wire dateline
        for text in (DPZ, CCL, NKE, ATZ):
            body = ea.release_body(ea.clean_text(text))
            self.assertNotIn("Published", body)
        self.assertNotEqual(self.parse(DPZ)["call"], et(2026, 10, 13, 16, 5))
        self.assertNotEqual(self.parse(CCL)["call"], et(2026, 9, 29, 11, 56))

    def test_script_text_is_not_read(self):
        self.assertNotIn("October 2", ea.clean_text(ATZ))

    def test_labels_are_short_and_never_read_as_a_guess(self):
        import imm_earnings_overrides as ieo
        for text in (DPZ, CCL, NKE, ATZ):
            lab = ea.label_for(self.parse(text), "press release")
            self.assertLessEqual(len(lab), 80)
            self.assertEqual(ieo.provenance_of(lab), "read", lab)

    def test_nothing_to_find_is_empty(self):
        f = self.parse("<p>Acme Corp (NYSE: ACME) announces a new CFO.</p>")
        self.assertEqual((f["call"], f["release"], ea.anchor(f)),
                         (None, None, None))


class TestAnchor(unittest.TestCase):

    def test_morning_call_without_a_release_time(self):
        self.assertEqual(ea.anchor({"call": et(2026, 10, 1, 8, 30)}),
                         et(2026, 10, 1, 6, 30))
        self.assertEqual(ea.anchor({"call": et(2026, 10, 1, 11)}),
                         et(2026, 10, 1, 7))

    def test_evening_call_without_a_release_time(self):
        self.assertEqual(ea.anchor({"call": et(2026, 10, 8, 16, 30)}),
                         et(2026, 10, 8, 16))

    def test_before_open_release_with_an_early_call(self):
        f = {"release": et(2026, 10, 1, 7), "release_how": "before open",
             "call": et(2026, 10, 1, 8)}
        self.assertEqual(ea.anchor(f), et(2026, 10, 1, 6))


class FakeHttp:
    """Stands in for earnings_announcements._get: url -> json dict or text."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, params=None, accept_json=False):
        self.calls.append(url)
        for key, body in self.routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return types.SimpleNamespace(
                    json=lambda b=body: b,
                    text=body if isinstance(body, str) else json.dumps(body))
        raise RuntimeError("404 " + url)


class SourceCase(unittest.TestCase):

    def setUp(self):
        self._get = ea._get

    def tearDown(self):
        ea._get = self._get

    def route(self, routes):
        ea._get = FakeHttp(routes)
        return ea._get


class TestQ4EventFeed(SourceCase):

    def test_next_earnings_call_skips_past_and_non_earnings(self):
        self.route({"investors.aritzia.com/feed/Event.svc": Q4_FEED})
        q = ea.q4_upcoming_call("investors.aritzia.com", NOW)
        self.assertEqual(q["call"], et(2026, 10, 8, 16, 30))
        self.assertIn("Second Quarter", q["title"])

    def test_pacific_zone_is_converted(self):
        feed = {"GetEventListResult": [
            {"Title": "Q2 Earnings Call", "StartDate": "10/08/2026 13:30:00",
             "TimeZone": "PST"}]}
        self.route({"feed/Event.svc": feed})
        self.assertEqual(ea.q4_upcoming_call("ir.x.com", NOW)["call"],
                         et(2026, 10, 8, 16, 30))

    def test_unknown_zone_and_non_q4_sites_find_nothing(self):
        feed = {"GetEventListResult": [
            {"Title": "Q2 Earnings Call", "StartDate": "10/08/2026 13:30:00",
             "TimeZone": "CET"}]}
        self.route({"feed/Event.svc": feed})
        self.assertIsNone(ea.q4_upcoming_call("ir.x.com", NOW))
        self.route({})                                   # 404 / HTML site
        self.assertIsNone(ea.q4_upcoming_call("www.example.com", NOW))


class TestNasdaqPressReleases(SourceCase):

    def rows(self, *titles):
        return {"data": {"rows": [
            {"title": t, "url": "/press-release/{}".format(i),
             "created": "Sep 10, 2026"} for i, t in enumerate(titles)]}}

    def test_finds_the_announcement_and_skips_other_releases(self):
        http = self.route({
            "api.nasdaq.com": self.rows(
                "Domino's Asks America: Is the Domino the First Icon?",
                "Domino's Announces Q3 2026 Earnings Webcast"),
            "press-release/1": DPZ})
        found = ea.nasdaq_announcements("DPZ", NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(ea.anchor(found[0]), et(2026, 10, 13, 6, 5))
        # the non-announcement title was never fetched
        self.assertFalse(any(u.endswith("press-release/0") for u in http.calls))

    def test_another_issuers_release_does_not_count(self):
        # the feed also carries releases that merely mention the symbol
        other = DPZ.replace("(Nasdaq: DPZ)", "(NYSE: XYZ)")
        self.route({"api.nasdaq.com": self.rows("XYZ to Host Webcast"),
                    "press-release/0": other})
        self.assertEqual(ea.nasdaq_announcements("DPZ", NOW), [])

    def test_a_dead_feed_or_a_non_symbol_finds_nothing(self):
        self.route({"api.nasdaq.com": RuntimeError("down")})
        self.assertEqual(ea.nasdaq_announcements("DPZ", NOW), [])
        self.assertEqual(ea.nasdaq_announcements("ARITZIA", NOW), [])


class TestFindAnnounced(SourceCase):

    def test_aritzia_from_the_ir_feed_alone(self):
        self.route({"investors.aritzia.com/feed/Event.svc": Q4_FEED})
        f = ea.find_announced(
            "ARITZIA",
            ["https://investors.aritzia.com/events-and-presentations/default.aspx"],
            NOW)
        self.assertEqual(f["anchor"], et(2026, 10, 8, 16))
        self.assertFalse(f["conflict"])
        self.assertIn("IR event feed", f["label"])

    def test_sources_days_apart_keep_the_earlier_and_say_so(self):
        feed = {"GetEventListResult": [
            {"Title": "Q3 Earnings Call", "StartDate": "10/20/2026 08:30:00",
             "TimeZone": "EST"}]}
        self.route({"api.nasdaq.com": {"data": {"rows": [
            {"title": "Domino's Announces Q3 2026 Earnings Webcast",
             "url": "/press-release/0", "created": "Sep 10, 2026"}]}},
            "press-release/0": DPZ, "ir.dominos.com/feed": feed})
        f = ea.find_announced("DPZ", ["https://ir.dominos.com/events"], NOW)
        self.assertEqual(f["anchor"], et(2026, 10, 13, 6, 5))
        self.assertTrue(f["conflict"])

    def test_a_split_reporter_is_one_event_not_a_conflict(self):
        # results after Tuesday's close, call Wednesday morning
        pr = CCL.replace("expected to be released that morning",
                         "released after market close on September 28, 2026")
        feed = {"GetEventListResult": [
            {"Title": "Q3 Earnings Call", "StartDate": "09/29/2026 10:00:00",
             "TimeZone": "EST"}]}
        self.route({"api.nasdaq.com": {"data": {"rows": [
            {"title": "Carnival to Hold Conference Call",
             "url": "/press-release/0", "created": "Sep 15, 2026"}]}},
            "press-release/0": pr, "ir.carnival.com/feed": feed})
        f = ea.find_announced("CCL", ["https://ir.carnival.com/"], NOW)
        self.assertFalse(f["conflict"])
        self.assertEqual(f["anchor"], et(2026, 9, 28, 16))

    def test_a_past_call_is_not_the_next_call(self):
        self.route({"api.nasdaq.com": {"data": {"rows": [
            {"title": "Domino's Announces Q3 2026 Earnings Webcast",
             "url": "/press-release/0", "created": "Sep 10, 2026"}]}},
            "press-release/0": DPZ})
        later = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.assertIsNone(ea.find_announced("DPZ", [], later))


class TestAnnouncedReleaseMerge(unittest.TestCase):
    """imm_earnings_overrides.announced_release: the company's notice merged
    with Nasdaq's calendar -- earlier wins, a day-plus disagreement is said."""

    def setUp(self):
        import imm_earnings_overrides as ieo
        self.ieo = ieo
        self._find, self._urls = ea.find_announced, ieo.source_urls
        ieo.source_urls = lambda client, ev: []

    def tearDown(self):
        ea.find_announced = self._find
        self.ieo.source_urls = self._urls

    def found(self, anchor_dt, conflict=False):
        ea.find_announced = lambda sym, urls, now: {
            "anchor": anchor_dt, "label": "announced: test; press release",
            "url": "https://example.com/pr", "conflict": conflict}

    def test_announcement_alone(self):
        self.found(et(2026, 10, 13, 6, 5))
        dt, label, url, note = self.ieo.announced_release(
            None, "KXEARNINGSMENTIONDPZ-26OCT15", NOW)
        self.assertEqual((dt, note), (et(2026, 10, 13, 6, 5), None))

    def test_nasdaq_same_day_proxy_never_moves_it_later(self):
        self.found(et(2026, 10, 13, 6, 5))
        rel = (et(2026, 10, 13, 7), "before open (~7am ET, Nasdaq)")
        dt, _l, _u, note = self.ieo.announced_release(
            None, "KXEARNINGSMENTIONDPZ-26OCT15", NOW, rel)
        self.assertEqual((dt, note), (et(2026, 10, 13, 6, 5), None))

    def test_nasdaq_days_earlier_wins_and_is_flagged(self):
        self.found(et(2026, 10, 13, 6, 5))
        rel = (et(2026, 10, 9, 7), "before open (~7am ET, Nasdaq)")
        dt, _l, _u, note = self.ieo.announced_release(
            None, "KXEARNINGSMENTIONDPZ-26OCT15", NOW, rel)
        self.assertEqual(dt, et(2026, 10, 9, 7))
        self.assertIn("Nasdaq", note)

    def test_far_past_the_ticker_date_is_not_written(self):
        self.found(et(2027, 1, 20, 16))                 # next quarter's notice
        self.assertIsNone(self.ieo.announced_release(
            None, "KXEARNINGSMENTIONDPZ-26OCT15", NOW))

    def test_a_crash_in_the_lookup_is_not_fatal(self):
        def boom(sym, urls, now):
            raise RuntimeError("parser bug")
        ea.find_announced = boom
        self.assertIsNone(self.ieo.announced_release(
            None, "KXEARNINGSMENTIONDPZ-26OCT15", NOW))


if __name__ == "__main__":
    unittest.main()
