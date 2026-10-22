#!/usr/bin/env python3
"""Unit tests for league_news.py (Jack 2026-10-22: "Build it" -- an NBA
team-news hold like the NFL's). No network: ESPN-shaped dicts.

Run: python -m unittest test_league_news
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import league_news as ln                                              # noqa: E402

TEAMS = [{"id": "1", "abbreviation": "ATL", "location": "ATLANTA", "name": "Atlanta Hawks"},
         {"id": "30", "abbreviation": "CHA", "location": "CHARLOTTE", "name": "Charlotte Hornets"},
         {"id": "9", "abbreviation": "GS", "location": "GOLDEN STATE", "name": "Golden State Warriors"},
         {"id": "12", "abbreviation": "LAC", "location": "LA", "name": "LA Clippers"},
         {"id": "13", "abbreviation": "LAL", "location": "LOS ANGELES", "name": "Los Angeles Lakers"},
         {"id": "3", "abbreviation": "NO", "location": "NEW ORLEANS", "name": "New Orleans Pelicans"},
         {"id": "18", "abbreviation": "NY", "location": "NEW YORK", "name": "New York Knicks"},
         {"id": "23", "abbreviation": "SAC", "location": "SACRAMENTO", "name": "Sacramento Kings"},
         {"id": "24", "abbreviation": "SA", "location": "SAN ANTONIO", "name": "San Antonio Spurs"},
         {"id": "26", "abbreviation": "UTAH", "location": "UTAH", "name": "Utah Jazz"},
         {"id": "27", "abbreviation": "WSH", "location": "WASHINGTON", "name": "Washington Wizards"},
         {"id": "28", "abbreviation": "TOR", "location": "TORONTO", "name": "Toronto Raptors"}]


def _inj(team_id, *players):
    return {"id": team_id, "injuries": [
        {"status": st, "date": d, "athlete": {"displayName": name}} for name, st, d in players]}


REPORT = {"injuries": [
    _inj("1", ("Mouhamed Gueye", "Out", "2026-10-19T19:38Z")),
    _inj("30", ("Grant Williams", "Day-To-Day", "2026-10-22T14:05Z"),
         ("Coby White", "Day-To-Day", "2026-10-22T14:05Z"),
         ("Kon Knueppel", "Day-To-Day", "2026-10-22T03:40Z"))]}
T0 = ln.espn_ts("2026-10-22T15:00Z")


class TestSnapshot(unittest.TestCase):

    def test_every_team_and_the_latest_entry(self):
        s = ln.build_snapshot("NBA", REPORT, TEAMS, now_ts=T0)
        self.assertEqual(len(s["teams"]), len(TEAMS))          # the uninjured too
        cha = s["teams"]["CHA"]
        self.assertEqual(cha["news_at"], ln.espn_ts("2026-10-22T14:05Z"))
        self.assertIn(cha["news"], ("Grant Williams Day-To-Day", "Coby White Day-To-Day"))
        self.assertEqual(cha["players"]["Kon Knueppel"]["status"], "Day-To-Day")
        self.assertIsNone(s["teams"]["TOR"]["news_at"])
        self.assertEqual(s["changes"], [])                     # no earlier read

    def test_changes_against_the_last_read_are_news(self):
        s0 = ln.build_snapshot("NBA", REPORT, TEAMS, now_ts=T0)
        changed = {"injuries": [
            _inj("1", ("Mouhamed Gueye", "Out", "2026-10-19T19:38Z")),
            _inj("30", ("Coby White", "Out", "2026-10-22T14:05Z"),      # same stamp
                 ("Kon Knueppel", "Day-To-Day", "2026-10-22T03:40Z")),
            _inj("28", ("Scottie Barnes", "Questionable", "2026-10-21T22:00Z"))]}
        s1 = ln.build_snapshot("NBA", changed, TEAMS, prev=s0, now_ts=T0 + 120)
        self.assertEqual(sorted(s1["changes"]),
                         ["CHA Coby White Out", "CHA Grant Williams cleared",
                          "TOR Scottie Barnes Questionable"])
        self.assertEqual(s1["teams"]["CHA"]["news_at"], T0 + 120)
        self.assertIn("Grant Williams cleared", s1["teams"]["CHA"]["news"])
        self.assertEqual(s1["teams"]["TOR"]["news_at"], T0 + 120)    # seen now, stamped earlier
        self.assertEqual(s1["teams"]["ATL"]["news_at"], s0["teams"]["ATL"]["news_at"])
        # the next quiet read keeps the news time (never back)
        s2 = ln.build_snapshot("NBA", changed, TEAMS, prev=s1, now_ts=T0 + 240)
        self.assertEqual(s2["changes"], [])
        self.assertEqual(s2["teams"]["CHA"]["news_at"], T0 + 120)

    def test_a_cleared_team_has_news_with_no_report_left(self):
        s0 = ln.build_snapshot("NBA", REPORT, TEAMS, now_ts=T0)
        s1 = ln.build_snapshot("NBA", {"injuries": [REPORT["injuries"][1]]}, TEAMS,
                               prev=s0, now_ts=T0 + 120)
        self.assertEqual(s1["changes"], ["ATL Mouhamed Gueye cleared"])
        self.assertEqual(s1["teams"]["ATL"]["news_at"], T0 + 120)
        self.assertEqual(s1["teams"]["ATL"]["players"], {})


class TestTeamPair(unittest.TestCase):

    def setUp(self):
        self.s = ln.build_snapshot("NBA", {}, TEAMS, now_ts=T0)

    def test_kalshi_codes_to_espn_teams(self):
        for seg, want in (("26OCT26GSWLAL", ("GS", "LAL")), ("26OCT23NOPUTA", ("NO", "UTAH")),
                          ("26OCT24TORWAS", ("TOR", "WSH")), ("26OCT24LACLAL", ("LAC", "LAL")),
                          ("26OCT24SASSAC", ("SA", "SAC")), ("26OCT22ATLNYK", ("ATL", "NY"))):
            self.assertEqual(ln.team_pair(self.s, f"KXNBAPTS-{seg}-XPLAYER1-25"), want, seg)

    def test_what_does_not_name_two_teams(self):
        self.assertIsNone(ln.team_pair(self.s, "KXNBA-27-WAS"))            # a season event
        self.assertIsNone(ln.team_pair(self.s, "KXNBAGAME-26OCT22ZZZQQQ"))  # unknown codes
        self.assertIsNone(ln.team_pair(self.s, "KXNBAGAME-26OCT22LALLAL"))  # one team twice
        self.assertIsNone(ln.team_pair({}, "KXNBAGAME-26OCT22ATLCHA"))      # no snapshot


if __name__ == "__main__":
    unittest.main()
