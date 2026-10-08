#!/usr/bin/env python3
"""Unit tests for imm_earnings_overrides.wh_schedule_start -- the Factbase
White-House calendar match that gives KXTRUMPMENTION events a start time.

Run: python -m unittest test_imm_earnings_overrides

Fixtures are real calendar entries, trimmed to the fields the matcher reads.
The direction of every error matters: no match only leaves the event on the
ticker-day live gate (and in the UNRESOLVED email); a WRONG match moves the
quoting cutoff and the live-gate arm to the wrong time.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_earnings_overrides as ieo                               # noqa: E402


def _e(day, time, details, location):
    return {"date": day, "time": time, "details": details, "location": location}


# KXTRUMPMENTION-26OCT05, as published the morning of the rally
OCT05 = [
    _e("2026-10-05", None, "TBD: The President departs the White House en "
       "route Grand Island, Nebraska", "The White House"),
    _e("2026-10-05", None, "TBD: The President arrives Grand Island, Nebraska "
       "[CDT Local]", "Pinnacle Bank Expo Center, Grand Island, NE"),
    _e("2026-10-05", "08:00:00", "The President participates in Executive Time",
       "The White House"),
    _e("2026-10-05", "09:00:00", "In-Town Pool Call Time", "The White House"),
    _e("2026-10-05", "10:00:00", "The President participates in a Policy "
       "Meeting", "Oval Office"),
    _e("2026-10-05", "13:55:00", "Out-of-Town Travel Pool Call Time",
       "Joint Base Andrews"),
    _e("2026-10-05", "19:00:00", "The President delivers Remarks [6:00 PM "
       "Local]", "Pinnacle Bank Expo Center, Grand Island, NE"),
]
OCT05_TITLE = "What will Trump say during his rally in Nebraska?"
OCT05_SUB = ("Donald Trump - Midterm Rally in Grand Island, Nebraska originally "
             "scheduled for October 5, 2026")

# KXTRUMPMENTION-26AUG05 (hand-set 16:30): the city ties the title match
AUG05 = [
    _e("2026-08-05", "00:57:00", "The President arrives at Harry Reid "
       "International Airport, Las Vegas", "Harry Reid International Airport, "
       "Las Vegas"),
    _e("2026-08-05", "16:02:00", "The President departs Trump International "
       "Hotel, Las Vegas en route Red Rock Casino Resort and Spa",
       "Trump International Hotel, Las Vegas"),
    _e("2026-08-05", "16:30:00", "The President delivers Remarks [1:30 PM "
       "Local]", "Red Rock Casino Resort and Spa, Las Vegas"),
    _e("2026-08-05", "18:37:00", "The President departs Red Rock Casino Resort "
       "and Spa, Las Vegas", "Red Rock Casino Resort and Spa, Las Vegas"),
]


class TestWhScheduleStart(unittest.TestCase):

    def _with(self, entries):
        saved = dict(ieo._wh_cache)
        ieo._wh_cache.clear()
        ieo._wh_cache["entries"] = entries
        self.addCleanup(lambda: (ieo._wh_cache.clear(),
                                 ieo._wh_cache.update(saved)))

    def test_rally_resolves_through_the_place_fallback(self):
        self._with(OCT05)
        dt, det = ieo.wh_schedule_start(OCT05_TITLE, date(2026, 10, 5),
                                        OCT05_SUB)
        self.assertEqual(dt.isoformat(), "2026-10-05T19:00:00-04:00")
        self.assertIn("delivers Remarks", det)

    def test_rally_without_sub_title_stays_unresolved(self):
        self._with(OCT05)
        self.assertIsNone(ieo.wh_schedule_start(OCT05_TITLE, date(2026, 10, 5)))

    def test_title_tie_falls_back_to_the_place_match(self):
        self._with(AUG05)
        title = "What will Trump say during his remarks in Las Vegas?"
        self.assertIsNone(ieo.wh_schedule_start(title, date(2026, 8, 5)))
        dt, _det = ieo.wh_schedule_start(
            title, date(2026, 8, 5), "Donald Trump - Remarks in Las Vegas, Nevada")
        self.assertEqual(dt.isoformat(), "2026-08-05T16:30:00-04:00")

    def test_place_tie_resolves_to_nothing(self):
        """A timed departure from the same venue scores the same as the
        remarks: ambiguous, so no start time rather than a guess."""
        self._with(OCT05 + [_e(
            "2026-10-05", "20:30:00", "The President departs Grand Island, "
            "Nebraska en route the White House [CDT Local]",
            "Pinnacle Bank Expo Center, Grand Island, NE")])
        self.assertIsNone(ieo.wh_schedule_start(OCT05_TITLE, date(2026, 10, 5),
                                                OCT05_SUB))

    def test_title_match_still_wins_first(self):
        self._with([
            _e("2026-08-03", "10:00:00", "The President participates in a "
               "Policy Meeting", "Oval Office"),
            _e("2026-08-03", "13:30:00", "The President signs an Executive "
               "Order", "Oval Office"),
        ])
        dt, det = ieo.wh_schedule_start(
            "What will Trump say when he signs an Executive Order?",
            date(2026, 8, 3), "Donald Trump - THE PRESIDENT signs an Executive Order")
        self.assertEqual(dt.isoformat(), "2026-08-03T13:30:00-04:00")
        self.assertEqual(det, "The President signs an Executive Order")

    def test_place_words_spell_out_one_word_states_only(self):
        self.assertIn("nebraska", ieo._wh_place_words("Expo Center, Grand Island, NE"))
        self.assertFalse({"new", "york"} & ieo._wh_place_words("MVP Arena, Albany, NY"))
        self.assertFalse({"white", "house"} & ieo._wh_place_words("The White House"))


# RNC events page (2026-10-06): the San Antonio rally page as served, trimmed
# to the date line and the venue the matcher reads
SA_SUB = ("Donald Trump - Midterm Rally in San Antonio, Texas originally "
          "scheduled for October 7, 2026")
SA_URL = ("https://events.gop.com/events/"
          "midterm-rally-in-san-antonio-texas-president-donald-j-trump")
SA_HTML = ("<title>Midterm Rally in San Antonio, Texas featuring President "
           "Donald J. Trump</title><div class=\"mobile-info\"><p>\n"
           "                 Wed, October 07, 2026 - 06:00 pm\n"
           "                                     (US/Central)\n"
           "                             </p><p>Doors Open: 02:30 PM</p>"
           "<p>Freeman Coliseum<br></p>3201 E Houston St, San Antonio, TX")


class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


class TestRncRallyStart(unittest.TestCase):

    def _get(self, pages):
        calls = []

        def get(url):
            calls.append(url)
            return _Resp(200, pages[url]) if url in pages else _Resp(404)
        return get, calls

    def test_name_from_the_sub_title_rallies_only(self):
        self.assertEqual(ieo.rnc_event_name(SA_SUB), "Midterm Rally in San Antonio, Texas")
        self.assertIsNone(ieo.rnc_event_name(
            "Donald Trump - Oval Office announcement originally scheduled for "
            "October 7, 2026"))
        self.assertIsNone(ieo.rnc_event_name("Donald Trump - Rally"))   # no place
        self.assertIsNone(ieo.rnc_event_name(""))

    def test_the_page_time_in_its_own_zone(self):
        get, calls = self._get({SA_URL: SA_HTML})
        dt, url = ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get)
        self.assertEqual(url, SA_URL)
        self.assertEqual(dt.isoformat(), "2026-10-07T19:00:00-04:00")   # 6pm CDT
        self.assertEqual(calls, [SA_URL])                 # first slug hit

    def test_remarks_time_beats_the_program_start(self):
        # Jack 2026-10-08: "Use the speaker time for things like trump remarks"
        # (Syracuse, 10/09: header 05:30 pm = program start; remarks 7:00 PM)
        html = (SA_HTML.replace("Wed, October 07, 2026 - 06:00 pm",
                                "Wed, October 07, 2026 - 05:30 pm")
                .replace("(US/Central)", "(US/Eastern)")
                + "<p>Event Schedule: 3:00 PM EST: Doors Open&nbsp; 5:30 PM EST: "
                  "Program Begins&nbsp; 7:00 PM EST: Remarks Begin</p>")
        get, _ = self._get({SA_URL: html})
        dt, _ = ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get)
        self.assertEqual(dt.isoformat(), "2026-10-07T19:00:00-04:00")
        # read in the header's zone: 7:00 PM CDT = 8:00 PM EDT
        get, _ = self._get({SA_URL: html.replace("(US/Eastern)", "(US/Central)")})
        dt, _ = ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get)
        self.assertEqual(dt.isoformat(), "2026-10-07T20:00:00-04:00")
        # a remarks time before the header, or > 6h after it, is not taken
        for bad in ("4:00 PM EST: Remarks Begin", "11:45 PM EST: Remarks Begin"):
            get, _ = self._get({SA_URL: html.replace("7:00 PM EST: Remarks Begin", bad)})
            dt, _ = ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get)
            self.assertEqual(dt.isoformat(), "2026-10-07T17:30:00-04:00", bad)

    def test_state_abbreviated_slug(self):
        # Syracuse 10/09: the page is "...-in-syracuse-ny-president-donald-j-trump"
        sub = ("Donald Trump - Midterm Rally in Syracuse, New York originally "
               "scheduled for October 9, 2026")
        url = ieo.RNC_EVENTS_BASE + "midterm-rally-in-syracuse-ny-president-donald-j-trump"
        html = ("Fri, October 09, 2026 - 05:30 pm (US/Eastern) Doors Open: 03:00 PM "
                "Nicholas J. Pirro Convention Center, Syracuse, NY. Event Schedule: "
                "3:00 PM EST: Doors Open&nbsp; 5:30 PM EST: Program Begins&nbsp; "
                "7:00 PM EST: Remarks Begin")
        get, calls = self._get({url: html})
        dt, got = ieo.rnc_rally_start(sub, date(2026, 10, 9), get=get)
        self.assertEqual((got, dt.isoformat()), (url, "2026-10-09T19:00:00-04:00"))
        self.assertEqual(len(calls), len(ieo.RNC_SLUG_SUFFIXES) + 1)  # full names first
        self.assertEqual(ieo._rnc_names("Rally in Washington, District of Columbia"),
                         ["Rally in Washington, District of Columbia", "Rally in Washington dc"])
        self.assertEqual(ieo._rnc_names("Rally at Mar-a-Lago"), ["Rally at Mar-a-Lago"])

    def test_later_slug_variants_are_tried(self):
        alt = SA_URL.replace("-president-donald-j-trump",
                             "-featuring-president-donald-j-trump")
        get, calls = self._get({alt: SA_HTML})
        dt, url = ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get)
        self.assertEqual((url, len(calls)), (alt, 2))

    def test_wrong_date_wrong_city_or_no_page_is_nothing(self):
        get, _ = self._get({SA_URL: SA_HTML})
        self.assertIsNone(ieo.rnc_rally_start(SA_SUB, date(2026, 10, 8), get=get))
        get, _ = self._get({SA_URL: SA_HTML.replace("San Antonio", "Austin")})
        self.assertIsNone(ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get))
        get, calls = self._get({})
        self.assertIsNone(ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get))
        self.assertEqual(len(calls), 2 * len(ieo.RNC_SLUG_SUFFIXES))   # -texas, -tx
        get, calls = self._get({SA_URL: SA_HTML.replace("06:00 pm", "TBD")})
        self.assertIsNone(ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=get))
        # a fetch error is a miss, not a crash
        def boom(url):
            raise OSError("down")
        self.assertIsNone(ieo.rnc_rally_start(SA_SUB, date(2026, 10, 7), get=boom))
        # not a rally: never fetched
        get, calls = self._get({})
        self.assertIsNone(ieo.rnc_rally_start(
            "Donald Trump - Oval Office announcement originally scheduled for "
            "October 7, 2026", date(2026, 10, 7), get=get))
        self.assertEqual(calls, [])

    def test_rnc_provenance_is_labelled_so_the_wh_schedule_can_replace_it(self):
        out = ieo.provenance_batch([], [], [], [
            ("KXTRUMPMENTION-26OCT07", "2026-10-07T19:00:00-04:00", "RNC events", "x"),
            ("KXTRUMPMENTION-26OCT05", "2026-10-05T19:00:00-04:00", "WH schedule", "y")])
        self.assertEqual([t[2] for t in out],
                         ["broadcast schedule [RNC events]", "broadcast schedule"])
        self.assertEqual(ieo.provenance_of(out[0][2]), "read")

# KXTRUMPMENTIONB-26OCT07 / KXTRUMPMENTION-26OCT07, as the calendar read the
# night before (2026-10-07 02:40Z)
OCT07 = [
    _e("2026-10-07", None, "TBD: The President departs the White House en "
       "route San Antonio, Texas", "The White House"),
    _e("2026-10-07", "08:00:00", "The President participates in Executive Time",
       "The White House"),
    _e("2026-10-07", "10:00:00", "The President participates in a Policy "
       "Meeting", "Oval Office"),
    _e("2026-10-07", "12:30:00", "The President participates in a Policy "
       "Meeting", "Oval Office"),
    _e("2026-10-07", "13:00:00", "The President makes an Announcement",
       "The White House"),
    _e("2026-10-07", "13:30:00", "Out-of-Town Travel Pool Call Time",
       "Joint Base Andrews"),
    _e("2026-10-07", "19:00:00", "The President delivers Remarks [6:00 PM "
       "Local]", "Freeman Coliseum, San Antonio, TX"),
]


class TestSingleWordUnique(unittest.TestCase):
    """Jack 2026-10-06: "teach the matcher to accept a single-word match when
    exactly one timed entry that day contains it"."""

    def setUp(self):
        self._saved = dict(ieo._wh_cache)
        ieo._wh_cache.clear()
        self.addCleanup(lambda: (ieo._wh_cache.clear(), ieo._wh_cache.update(self._saved)))

    def test_one_word_one_entry_resolves(self):
        ieo._wh_cache["entries"] = OCT07
        dt, det = ieo.wh_schedule_start(
            "What will Trump say during his announcement?", date(2026, 10, 7),
            "Donald Trump - Oval Office announcement originally scheduled for "
            "October 7, 2026")
        self.assertEqual((dt.isoformat(), det),
                         ("2026-10-07T13:00:00-04:00", "The President makes an Announcement"))
        # the rally still resolves through the place fallback
        dt, _ = ieo.wh_schedule_start(
            "What will Trump say during his rally in Texas?", date(2026, 10, 7),
            "Donald Trump - Midterm Rally in San Antonio, Texas originally "
            "scheduled for October 7, 2026")
        self.assertEqual(dt.isoformat(), "2026-10-07T19:00:00-04:00")

    def test_one_word_two_entries_is_nothing(self):
        ieo._wh_cache["entries"] = OCT07
        self.assertIsNone(ieo.wh_schedule_start(
            "What will Trump say during his meeting?", date(2026, 10, 7)))
        ieo._wh_cache["entries"] = OCT07 + [
            _e("2026-10-07", "16:00:00", "The President makes an Announcement",
               "Roosevelt Room")]
        self.assertIsNone(ieo.wh_schedule_start(
            "What will Trump say during his announcement?", date(2026, 10, 7)))
        # a travel entry never counts
        ieo._wh_cache["entries"] = [
            _e("2026-10-07", "13:00:00", "The President departs for the "
               "Announcement venue", "x")]
        self.assertIsNone(ieo.wh_schedule_start(
            "What will Trump say during his announcement?", date(2026, 10, 7)))
        # an untimed entry does not count, nor does another day's
        ieo._wh_cache["entries"] = [
            _e("2026-10-07", None, "TBD: The President makes an Announcement", "x"),
            _e("2026-10-08", "13:00:00", "The President makes an Announcement", "x")]
        self.assertIsNone(ieo.wh_schedule_start(
            "What will Trump say during his announcement?", date(2026, 10, 7)))

if __name__ == "__main__":
    unittest.main()
