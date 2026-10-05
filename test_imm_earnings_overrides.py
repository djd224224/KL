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


if __name__ == "__main__":
    unittest.main()
