#!/usr/bin/env python3
"""Unit tests for imm_release_calendar.py (Jack 2026-10-22: "Yes email. Also
check daily for new calendar times for any unresolved ones, and check
calendars when adding new events to this auto-quoting family").

Run: python -m unittest test_imm_release_calendar
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_release_calendar as irc                                   # noqa: E402
import incentive_mm as imm                                           # noqa: E402

BLS = """<table class="release-list"><thead><tr><th>Reference Month</th><th>Release Date</th>
<th>Release Time</th></tr></thead><tbody><tr class="release-list-even-row"><td>December 2026</td>
<td>Jan. 22, 2027</td><td>10:00 AM</td></tr><tr><td>May 2027</td><td>Jun. 04, 2027</td>
<td>08:30 AM</td></tr><tr><td>August 2027</td><td>Sept. 17, 2027</td><td>10:00 AM</td></tr>
</tbody></table>"""
NASS = """<div>View: As Calendar Previous Next Date Time Release Status</div>
<div>Fri, 01/29/27</div><div>12:00 pm ET</div><div>Crop Production Historical Track Records</div>
<div>3:00 pm ET</div><div>Cattle</div><div>Report Pending ...</div><div>Cattle on Feed</div>
<div>Tue, 02/09/27</div><div>3:00 pm ET</div><div>Catfish Production</div>
<div>Wed, 02/10/27</div><div>12:00 pm ET</div><div>Crop Production</div>
<div>Crop Production Annual Summary</div><div>3:00 pm ET</div><div>Poultry Slaughter</div>
<div>Fri, 04/30/27</div><div>3:00 pm ET</div><div>Milk Production, Disposition, and Income</div>
<div>Poultry - Production and Value</div>"""
EPM = "<p>Release Date:</p><p>October 23, 2026</p><p>Next Release Date:</p><p>November 24, 2026 Go</p>"
NGM = "<span>Release Date: October 30, 2026 | Next Release: November 30, 2026 | full report</span>"
QCR = "<p>Release Date:</p><p>October 1, 2026</p><p>Next Release Date:</p><p>TBD</p>"
STEO = "<tr><td>December 2026</td><td>12/08/2026</td></tr><tr><td>July 2027</td><td>07/07/2027 (Wednesday)</td></tr>"
BPS = """<tr><th>Survey Month</th><th>New Residential Construction - 8:30 a.m.</th><th>Revised</th></tr>
<tr><td><strong>December 2027</strong></td><td align="center">January 19, 2028</td>
<td align="center">January 26, 2028</td></tr>"""
POP = """<tr><td>National and State population and demographic components of population change</td>
<td>7/1/2025</td><td>December 17, 2026 (7/1/2026 data)</td></tr>
<tr><td>County population</td><td>7/1/2025</td><td>March 26, 2027 (7/1/2026 data)</td></tr>"""
FHFA = """<h3>2027 HPI Release Dates</h3><table><tr><td>Tuesday, January 26</td><td>Monthly Index</td>
</tr><tr><td>Tuesday, February 23</td><td>Quarterly Index</td></tr></table>"""


class TestParsers(unittest.TestCase):
    def test_each_agency_page(self):
        self.assertEqual(irc.parse_bls(BLS), ["2027-01-22 10:00", "2027-06-04 08:30",
                                              "2027-09-17 10:00"])
        self.assertEqual(irc.parse_nass(NASS), {
            "nass_cattle": ["2027-01-29 15:00"], "nass_cof": ["2027-01-29 15:00"],
            "nass_catfish": ["2027-02-09 15:00"],
            "nass_crop": ["2027-02-10 12:00", "2027-02-10 12:00"],      # forecast + annual
            "nass_milk": ["2027-04-30 15:00"], "nass_poultry": ["2027-04-30 15:00"]})
        self.assertEqual(irc.parse_eia_next(EPM), ["2026-11-24 10:00"])
        self.assertEqual(irc.parse_eia_next(NGM), ["2026-11-30 10:00"])
        self.assertEqual(irc.parse_eia_next(QCR), [])                   # TBD
        self.assertEqual(irc.parse_steo(STEO), ["2026-12-08 12:00", "2027-07-07 12:00"])
        self.assertEqual(irc.parse_census_bps(BPS), ["2028-01-19 08:30", "2028-01-26 08:00",
                                                     "2028-05-01 08:00"])  # + May's annual
        self.assertEqual(irc.parse_census_popest(POP), ["2026-12-17 00:01"])
        self.assertEqual(irc.parse_census_popest(POP.replace("December 17, 2026",
                                                             "December 2026")), [])
        self.assertEqual(irc.parse_fhfa(FHFA), ["2027-01-26 09:00", "2027-02-23 09:00"])


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def get_market(self, t):
        m = self.markets[t]
        if isinstance(m, Exception):
            raise m
        return {"market": m}


class TestRun(unittest.TestCase):
    NOW = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_relcal_")
        self.cal = os.path.join(self.dir, "release_calendar.json")
        with open(self.cal, "w", encoding="utf-8") as f:
            json.dump({"sources": {
                "nass_cattle": {"name": "USDA NASS Cattle", "note": "late January",
                                "times_et": []},
                "eia_epm": {"name": "EIA Electric Power Monthly", "times_et": ["2026-10-23 10:00"]},
                "fhfa_hpi": {"name": "FHFA HPI", "times_et": ["2027-02-23 09:00"]}}}, f)
        with open(os.path.join(self.dir, imm.RELEASE_PAUSE_MAP_FILE), "w", encoding="utf-8") as f:
            json.dump({"series": {"KXSDCATTLE": ["nass_cattle"], "KXMTCATTLE": ["nass_cattle"],
                                  "KXILNUCLEAR": ["eia_epm"], "KXUTHOMEPRICE": ["fhfa_hpi"]}}, f)
        for p in (mock.patch.object(imm, "STATUS_DIR", self.dir),
                  mock.patch.object(imm, "RELEASE_CALENDAR_FILE", self.cal),
                  mock.patch.dict(imm._release_cal, {"checked": 0.0, "files": {}, "sources": {}}),
                  mock.patch.object(irc, "FETCH_GAP_SECS", 0),
                  mock.patch.object(irc, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.pages = {}
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.pages:
            return self.pages[url]
        return "<html></html>"

    def run_once(self, st, new=(), client=None, now=None, dry=False):
        return irc.run(client or FakeClient({}), list(new), st, dry, now=now or self.NOW,
                       fetcher=self.fetch)

    def test_a_report_with_no_date_is_looked_up_then_emailed_weekly(self):
        st = {}
        line, mail = self.run_once(st)
        self.assertTrue(any("nass.usda.gov" in u for u in self.fetched))     # nass_cattle: none
        self.assertTrue(any("electricity/monthly" in u for u in self.fetched))  # epm ends 10/23
        self.assertFalse(any("fhfa" in u for u in self.fetched))           # 2/23 is > 60 days out
        subject, text = mail
        self.assertEqual(subject, "IMM-WATCH release calendar: 1 report(s) with no upcoming date")
        self.assertIn("USDA NASS Cattle [NEW]", text)
        self.assertIn("Series:  KXMTCATTLE, KXSDCATTLE", text)
        self.assertIn("Note:    late January", text)
        self.assertIn("1 without an upcoming date", line)
        self.fetched.clear()
        _line, mail = self.run_once(st, now=self.NOW + timedelta(hours=1))   # same ET day
        self.assertIsNone(mail)
        self.assertEqual(self.fetched, [])                                  # read once a day
        _line, mail = self.run_once(st, now=self.NOW + timedelta(days=7, hours=1))
        self.assertIn("USDA NASS Cattle\n", mail[1])                         # weekly, not [NEW]

    def test_a_date_read_off_the_page_is_added_and_closes_it(self):
        self.pages[irc.NASS_URL.format(y=2027, m=1)] = (
            "<div>Fri, 01/29/27</div><div>3:00 pm ET</div><div>Cattle</div>")
        st = {}
        line, mail = self.run_once(st)
        self.assertIsNone(mail)
        self.assertIn("added 1 date(s)", line)
        with open(imm.release_calendar_found_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sources"]["nass_cattle"]["times_et"],
                             ["2027-01-29 15:00"])
        cal = imm.load_release_calendar(force=True)
        self.assertEqual(len(cal["nass_cattle"][1]), 1)                    # the bot merges it

    def test_dry_writes_nothing(self):
        self.pages[irc.NASS_URL.format(y=2027, m=1)] = (
            "<div>Fri, 01/29/27</div><div>3:00 pm ET</div><div>Cattle</div>")
        _line, mail = self.run_once({}, dry=True)
        self.assertIsNone(mail)
        self.assertFalse(os.path.exists(imm.release_calendar_found_path()))

    def test_a_report_that_loses_its_last_date_is_read_before_it_is_emailed(self):
        st = {}
        self.run_once(st)                                    # 10/22: epm still has 10/23
        self.fetched.clear()
        self.pages["https://www.eia.gov/electricity/monthly/"] = (
            "<p>Next Release Date:</p><p>November 24, 2026</p>")
        after = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)   # the 10/23 release passed
        st["looked_day"] = after.astimezone(imm.ET).strftime("%Y-%m-%d")  # already read today
        line, mail = self.run_once(st, now=after)
        self.assertEqual(self.fetched, ["https://www.eia.gov/electricity/monthly/"])
        self.assertIn("added 1 date(s)", line)
        self.assertNotIn("Electric Power", (mail or ("", ""))[1])

    def test_new_state_series_are_checked_against_their_rules(self):
        client = FakeClient({
            "KXGAPEACH-27JUN30-T1": {"rules_primary": "If USDA NASS reports that Georgia's peach "
                                                      "production", "rules_secondary": ""},
            "KXNVTOUR-27JAN31-T9": {"rules_primary": "If the Nevada visitor authority reports",
                                    "rules_secondary": "Las Vegas visitor volume"},
            "KXCORNX-27JAN31-T1": {"rules_primary": "If USDA NASS reports that Ohio's corn "
                                                    "production", "rules_secondary": ""},
            "KXDOWN-27JAN31-T1": RuntimeError("503")})
        st = {}
        new = [("KXGAPEACH", "KXGAPEACH-27JUN30-T1"), ("KXNVTOUR", "KXNVTOUR-27JAN31-T9"),
               ("KXCORNX", "KXCORNX-27JAN31-T1"), ("KXDOWN", "KXDOWN-27JAN31-T1")]
        line, mail = self.run_once(st, new=new, client=client)
        subject, text = mail
        # nass_cattle from the map, and KXCORNX's nass_crop has no dates in this calendar
        self.assertEqual(subject, "IMM-WATCH release calendar: 2 report(s) with no upcoming "
                                  "date, 2 new state series with no report on the calendar")
        self.assertIn("KXGAPEACH (e.g. KXGAPEACH-27JUN30-T1)", text)    # USDA, but peaches
        self.assertIn("KXNVTOUR (e.g. KXNVTOUR-27JAN31-T9)", text)
        self.assertIn("Rules:   If the Nevada visitor authority reports Las Vegas visitor volume",
                      text)
        self.assertNotIn("KXCORNX (e.g.", text)                          # nass_crop: mapped
        self.assertNotIn("KXDOWN", text)                                 # read failed: retried
        self.assertEqual([p[0] for p in st["pending"]], ["KXDOWN"])
        self.assertIn("4 new series checked", line)
        client.markets["KXDOWN-27JAN31-T1"] = {"rules_primary": "BLS series SMU47000003133600001"}
        _line, mail = self.run_once(st, client=client, now=self.NOW + timedelta(minutes=30))
        self.assertEqual(st["pending"], [])
        self.assertNotIn("KXNVTOUR", (mail or ("", ""))[1])                # sent once

    def test_due_resends_weekly_and_remembers_one_offs(self):
        t0 = 1_000_000.0
        items = {"src:a": {"name": "A", "series": ["X"]}, "new:Y": {"once": True}}
        out, st = irc.due({}, items, t0)
        self.assertEqual([(k, new) for k, _it, new in out], [("new:Y", True), ("src:a", True)])
        out, st = irc.due(st, {"src:a": items["src:a"]}, t0 + 6 * 86400)
        self.assertEqual(out, [])
        self.assertIn("new:Y", st)                                       # remembered
        out, st = irc.due(st, {"src:a": items["src:a"]}, t0 + 7 * 86400 + 1)
        self.assertEqual([(k, new) for k, _it, new in out], [("src:a", False)])
        out, st = irc.due(st, {}, t0 + 8 * 86400)                       # closed: leaves
        self.assertNotIn("src:a", st)


if __name__ == "__main__":
    unittest.main()
