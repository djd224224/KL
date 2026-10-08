"""Tests for the family watch (imm_family_watch.py) and the hot-reloaded rows
(imm_rows.py + incentive_mm's ELECTION_DATES_EXTRA / AWARDS_DATES_EXTRA).
Jack 2026-10-07: "yes build all 5. $5/day is the right threshold"."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import imm_family_watch as fw
import imm_rows
import incentive_mm as imm

US = {"category": "Elections", "tags": ["Other US Elections"]}


def mk(rules, event="KXSTATELEG-TXSENA26", exp="2026-11-03T15:00:00Z", title=""):
    return {"rules_primary": rules, "event_ticker": event,
            "expected_expiration_time": exp, "title": title}


class TestUsGeneralRule(unittest.TestCase):

    def test_state_chamber_and_house_seats_pass(self):
        ok, _ = fw.us_general_vote_ok(US, mk(
            "If the Republican party wins the Texas Senate in 2026, then the market "
            "resolves to Yes. Winning is defined as holding more seats than any other "
            "party two weeks after the Senate is sat for the session following the election."))
        self.assertTrue(ok)
        ok, _ = fw.us_general_vote_ok(
            {"category": "Elections", "tags": ["House", "US Elections"]},
            mk("If Democrats win 9 seats in the 2026 U.S. House of Representatives "
               "elections in New Jersey, then the market resolves to Yes.",
               event="KXHOUSEWINSTATE-NJD", exp="2027-01-04T15:00:00Z"))
        self.assertTrue(ok)

    def test_the_ticker_year_counts_when_the_terms_omit_it(self):
        ok, _ = fw.us_general_vote_ok(
            {"category": "Elections", "tags": ["Local", "US Elections"]},
            mk("If Ron Nirenberg wins the Bexar County Judge election, then the market "
               "resolves to Yes.", event="KXBEXARCOUNTYJUDGE-26"))
        self.assertTrue(ok)
        ok, why = fw.us_general_vote_ok(
            {"category": "Elections", "tags": ["US Elections"]},
            mk("If X wins the Chicago mayoral election, then the market resolves to Yes.",
               event="KXCHICAGOMAYOR-27", exp="2027-02-23T15:00:00Z"))
        self.assertFalse(ok)

    def test_non_votes_foreign_and_wrong_windows_fail(self):
        cases = [
            # a rally under a US Elections tag (KXOBAMARALLY)
            ({"category": "Elections", "tags": ["Senate", "US Elections"]},
             mk("If Josh Turek attends an in-person rally with Barack Obama after "
                "Issuance and before Nov 3, 2026, then the market resolves to Yes.",
                event="KXOBAMARALLY-26NOV03", exp="2026-11-10T15:00:00Z"), "rally"),
            ({"category": "Elections", "tags": ["International elections", "Brazil"]},
             mk("If Lula wins the 2026 Brazilian presidential runoff ..."), "US"),
            (US, mk("If Smith wins the 2026 Republican primary for ..."), "primary"),
            (US, mk("If Jones wins the 2026 special election for ..."), "special"),
            (US, mk("If Lee wins the 2026 Georgia Senate runoff ..."), "runoff"),
            ({"category": "Politics", "tags": ["US Elections"]},
             mk("If the Republicans win the Senate in 2026 ..."), "category"),
            (US, mk("If the Republicans win the Senate in 2026 ...",
                    exp="2026-10-20T15:00:00Z"), "expiry"),
            (US, mk("Will a court invalidate the 2026 result ..."), "court"),
            (US, mk("Who will lead the 2026 polling average ..."), "vote language"),
        ]
        for series, market, label in cases:
            ok, why = fw.us_general_vote_ok(series, market)
            self.assertFalse(ok, label)


class TestRows(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rows_")
        self._saved = imm.STATUS_DIR
        imm.STATUS_DIR = self.tmp
        self.addCleanup(lambda: (setattr(imm, "STATUS_DIR", self._saved),
                                 shutil.rmtree(self.tmp, ignore_errors=True)))
        imm.ELECTION_DATES_EXTRA[:] = []
        imm.AWARDS_DATES_EXTRA[:] = []
        self.addCleanup(lambda: (imm.ELECTION_DATES_EXTRA.__setitem__(slice(None), []),
                                 imm.AWARDS_DATES_EXTRA.__setitem__(slice(None), [])))

    def test_two_sources_and_well_formed_rows_only(self):
        self.assertIn("two different sources", imm_rows.validate(
            "election", "KXSTATELEG-LAHOUSE27", "2027-10-23", "", ["a", "a"]))
        self.assertIn("bad event glob", imm_rows.validate(
            "election", "kx bad", "2027-10-23", "", ["a", "b"]))
        self.assertIn("bad date", imm_rows.validate(
            "election", "KXSTATELEG-LAHOUSE27", "Oct 23", "", ["a", "b"]))
        self.assertTrue(imm_rows.validate(
            "election", "KXSTATELEG-LAHOUSE27", "2027-10-23", "Mars/Olympus", ["a", "b"]))
        self.assertIn("election rows only", imm_rows.validate(
            "award", "KXEMMYX-27", "2026-12-01", "America/Chicago", ["a", "b"]))
        # the code table already dates it: fix it there
        self.assertIn("code table already dates", imm_rows.validate(
            "election", "KXSTATELEG-TXSENA26", "2026-11-03", "", ["a", "b"]))
        self.assertEqual(imm_rows.validate(
            "election", "KXSTATELEG-LAHOUSE27", "2027-10-23", "America/Chicago",
            ["https://sos.la.gov/x", "https://ballotpedia.org/y"]), "")

    def test_add_reload_lookup_replace_remove(self):
        ok, msg = imm_rows.add_row("election", "KXSTATELEG-LAHOUSE27", "2027-10-23",
                                   ["src one", "src two"], zone="America/Chicago", by="test")
        self.assertTrue(ok, msg)
        self.assertGreater(imm.load_election_dates_extra(), 0)
        # 00:00 ET is earlier than 00:00 Central: the cutoff
        self.assertEqual(imm.election_cutoff_utc("KXSTATELEG-LAHOUSE27"),
                         datetime(2027, 10, 23, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(imm.load_election_dates_extra(), 0)          # unchanged
        ok, msg = imm_rows.add_row("election", "KXSTATELEG-LAHOUSE27", "2027-10-24",
                                   ["src one", "src two"])
        self.assertFalse(ok)                                         # needs replace
        ok, _ = imm_rows.add_row("election", "KXSTATELEG-LAHOUSE27", "2027-10-23",
                                 ["src one", "src two"], zone="America/Chicago")
        self.assertTrue(ok)                                          # same row: no-op
        row = imm_rows.load_rows("election")[0]
        self.assertEqual((row["by"], row["sources"]), ("test", ["src one", "src two"]))
        ok, _ = imm_rows.remove_row("election", "KXSTATELEG-LAHOUSE27")
        self.assertTrue(ok)
        imm.load_election_dates_extra()
        self.assertIsNone(imm.election_cutoff_utc("KXSTATELEG-LAHOUSE27"))

    def test_award_rows_reach_the_lookup_and_the_family_default(self):
        ok, msg = imm_rows.add_row("award", "KXOSCARPIC-28", "2028-01-20",
                                   ["academy schedule", "deadline"])
        self.assertTrue(ok, msg)
        imm.load_awards_dates_extra()
        self.assertEqual(imm.awards_event_start("KXOSCARPIC-28", None, True),
                         datetime(2028, 1, 20, 5, 0, tzinfo=timezone.utc))
        # a sibling category of the same edition defaults to it
        self.assertEqual(imm.awards_family_default("KXOSCARDIR-28"),
                         datetime(2028, 1, 20, 5, 0, tzinfo=timezone.utc))

    def test_a_bad_row_in_the_file_is_skipped(self):
        with open(imm.election_dates_extra_path(), "w", encoding="utf-8") as f:
            json.dump({"rows": [{"glob": "bad glob", "date": "2027-01-01"},
                                {"glob": "KXX-27", "date": "Jan 1"},
                                {"glob": "KXSTATELEG-LAHOUSE27", "date": "2027-10-23",
                                 "zone": "Not/AZone"},
                                {"glob": "KXSTATELEG-MSHOUSE27", "date": "2027-11-02"}]}, f)
        imm.load_election_dates_extra()
        self.assertEqual([r[0] for r in imm.ELECTION_DATES_EXTRA], ["KXSTATELEG-MSHOUSE27"])


class TestGapAlert(unittest.TestCase):
    PROGS = {
        "KXA-26-X": {"period_reward": 20000 * 10000, "start_date": "2026-10-01T00:00:00Z",
                     "end_date": "2026-10-11T00:00:00Z"},                  # $2,000/day
        "KXB-26-X": {"period_reward": 100 * 10000, "start_date": "2026-10-01T00:00:00Z",
                     "end_date": "2026-10-11T00:00:00Z"},                  # $10/day
    }

    def test_items_from_gaps_and_new_series_only(self):
        gaps = {"KXA-26": {"series": "KXA", "msg": "no verified election day"},
                "KXB-26": {"series": "KXB", "msg": "no row"}}
        items = fw.gap_items(self.PROGS, gaps, {"KXNEW": 900.0, "KXOLD": 5000.0}, {"KXOLD"})
        self.assertEqual(set(items), {"gap:KXA-26", "gap:KXB-26", "new:KXNEW"})
        self.assertAlmostEqual(items["gap:KXA-26"]["est"], 2000 * fw.GAP_CAPTURE, places=2)
        self.assertAlmostEqual(items["new:KXNEW"]["est"], 900 * fw.GAP_CAPTURE, places=2)

    def test_due_after_30_min_over_5_dollars_once_then_daily(self):
        items = {"gap:KXA-26": {"est": 44.0, "what": "a", "fix": "f"},
                 "gap:KXB-26": {"est": 0.22, "what": "b", "fix": "f"},
                 "new:KXNEW": {"est": 19.8, "what": "n", "fix": "f"}}
        t = 1_800_000_000.0
        due, st = fw.due_alerts({}, items, t)
        self.assertEqual(due, [])                                     # first sight
        due, st = fw.due_alerts(st, items, t + 29 * 60)
        self.assertEqual(due, [])                                     # < 30 min
        due, st = fw.due_alerts(st, items, t + 31 * 60)
        self.assertEqual(sorted(k for k, _, _ in due), ["gap:KXA-26", "new:KXNEW"])
        due, st = fw.due_alerts(st, items, t + 90 * 60)
        self.assertEqual(due, [])                                     # sent already
        due, st = fw.due_alerts(st, items, t + 25 * 3600)
        self.assertEqual([k for k, _, _ in due], ["gap:KXA-26"])      # daily; new: once
        # fixed: drops out of the state
        due, st = fw.due_alerts(st, {"new:KXNEW": items["new:KXNEW"]}, t + 26 * 3600)
        self.assertEqual(set(st), {"new:KXNEW"})

    def test_alert_body_names_value_age_and_fix(self):
        subject, body = fw.alert_body([("gap:KXA-26", {"est": 44.0, "what": "KXA-26: no row",
                                                       "fix": "add a row"}, 95.0)])
        self.assertIn("1 dark item(s) worth ~$44/day", subject)
        self.assertIn("~$44.00/day, dark 1.6h  KXA-26: no row", body)
        self.assertIn("fix: add a row", body)


if __name__ == "__main__":
    unittest.main()
