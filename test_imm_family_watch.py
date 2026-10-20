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

_token_patch = None


def setUpModule():
    # no test may read the real research token (~/.claude/imm-research-token.txt)
    global _token_patch
    from unittest import mock
    _token_patch = mock.patch.object(
        fw, "RESEARCH_TOKEN_FILE", os.path.join(tempfile.mkdtemp(prefix="notok_"), "none.txt"))
    _token_patch.start()


def tearDownModule():
    _token_patch.stop()


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

    def test_legacy_unprefixed_series_take_hot_rows(self):
        # 2026-10-14: Kalshi's un-prefixed election series (CONTROLH, ECMOV)
        # were refused by a KX-only glob; a leading wildcard still is
        for g in ("KX*", "*", "C*", "controlh-2028"):
            self.assertIn("bad event glob", imm_rows.validate(
                "election", g, "2028-11-07", "", ["a", "b"]), g)
        ok, msg = imm_rows.add_row("election", "SENATEMOV-28NOV07*", "2028-11-07",
                                   ["2 U.S.C. 1", "2 U.S.C. 7"], by="test")
        self.assertTrue(ok, msg)
        self.assertGreater(imm.load_election_dates_extra(), 0)
        self.assertEqual(imm.election_cutoff_utc("SENATEMOV-28NOV07"),
                         datetime(2028, 11, 7, 5, 0, tzinfo=timezone.utc))

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
        # gone from the gaps file: the record outlives it for the grace period
        # (a bot restart empties the file for a refresh), so a return is not new
        due, st = fw.due_alerts(st, {"new:KXNEW": items["new:KXNEW"]}, t + 26 * 3600)
        self.assertEqual(set(st), {"gap:KXA-26", "gap:KXB-26", "new:KXNEW"})
        due, st = fw.due_alerts(st, items, t + 26.5 * 3600)
        self.assertEqual(due, [])                                     # back: not re-sent
        # gone past the grace: dropped (fixed, or gone)
        later = t + 26.5 * 3600 + (fw.STATE_GRACE_HOURS + 0.1) * 3600
        due, st = fw.due_alerts(st, {"new:KXNEW": items["new:KXNEW"]}, later)
        self.assertEqual(set(st), {"new:KXNEW"})

    def test_alert_body_names_value_age_and_fix(self):
        qs = {"ev:KXA-26": {"q": 0, "n": 3, "dec": fw.Counter({"payout_floor": 3}),
                            "cutoff": None}}
        subject, body = fw.alert_body([("gap:KXA-26", {"est": 44.0, "what": "KXA-26: no row",
                                                       "fix": "add a row"}, 95.0)], qs)
        self.assertEqual(subject, "IMM watch: 1 not quoted (~$44/day)")
        self.assertIn("- NOT QUOTED (0 of 3 markets; bot: payout_floor) -- missing a config "
                      "fix. ~$44.00/day (pool proxy); flagged 1.6h ago  KXA-26: no row", body)
        self.assertIn("fix: add a row", body)

    def test_quoted_vs_not_quoted_and_what_is_missing(self):
        # Jack 2026-10-17: '"Dark" email should be clear on if the event isn't
        # quoted vs if it's missing an end date'
        cut = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc).timestamp()
        qs = {"ev:KXTRUMPMENTIONB-26OCT19": {"q": 25, "n": 25, "dec": fw.Counter(),
                                             "cutoff": cut},
              "ev:KXSENGA-26": {"q": 0, "n": 4, "dec": fw.Counter(), "cutoff": None}}
        due = [("start:KXTRUMPMENTIONB-26OCT19",
                {"est": 49.86, "what": "KXTRUMPMENTIONB-26OCT19: no start time", "fix": "r"},
                1800.0),
               ("gap:KXSENGA-26", {"est": 12.0, "what": "KXSENGA-26: no verified election "
                                                        "day on file", "fix": "row"}, 60.0),
               ("new:KXNEW", {"est": 7.0, "what": "KXNEW: newly paying", "fix": "call"}, 60.0)]
        subject, body = fw.alert_body(due, qs)
        self.assertEqual(subject, "IMM watch: 2 not quoted (~$19/day); 1 quoted but missing "
                                  "a start time or date (~$50/day)")
        self.assertIn("- QUOTED on 25 of 25 markets -- missing its start time. ~$49.86/day", body)
        self.assertIn("quoting now, until Nov 02 10:00 AM ET, behind the live gates; a start "
                      "time makes it stop 10 min before the event", body)
        self.assertIn("- NOT QUOTED (0 of 4 markets) -- missing its election day.", body)
        self.assertIn("- NOT QUOTED (not in the bot's book) -- missing a place on the allowlist "
                      "(a new family: your call).", body)
        self.assertNotIn("dark ", body.split("(QUOTED / NOT QUOTED")[0])

    def test_quote_state_reads_the_dashboard(self):
        D = {"markets": {
            "KXE-26-A": {"ev": "KXE-26", "ser": "KXE", "cur": {"q": 2}, "dec": "selected",
                         "cutoff": 1900000000.0},
            "KXE-26-B": {"ev": "KXE-26", "ser": "KXE", "cur": {"q": 0}, "dec": "payout_floor",
                         "cutoff": 1800000000.0}}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dash.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write("<script>const D = " + json.dumps(D) + ";\n</script>")
            qs = fw.load_quote_state(path)
            self.assertEqual((qs["ev:KXE-26"]["q"], qs["ev:KXE-26"]["n"],
                              qs["ev:KXE-26"]["cutoff"]), (1, 2, 1900000000.0))
            self.assertEqual(dict(qs["ser:KXE"]["dec"]), {"payout_floor": 1})
            self.assertEqual(fw.load_quote_state(os.path.join(d, "missing.html")), {})

    def test_alert_says_what_each_item_is(self):
        # Jack 2026-10-11: "make sure the description of the event is listed,
        # otherwise i dont know what 'KXTSAD' is"
        class Client:
            def get(self, path, params=None):
                if path == "/series/KXTSAD":
                    return {"series": {"title": "Daily TSA passenger screenings",
                                       "category": "Economics", "frequency": "daily"}}
                if path == "/events/KXA-26":
                    return {"event": {"title": "Who will win the A primary?",
                                      "sub_title": "Nov 3, 2026"}}
                raise RuntimeError("down")
        due = [("new:KXTSAD", {"est": 68.0, "what": "KXTSAD: newly paying", "fix": "x"}, 30.0),
               ("gap:KXA-26", {"est": 44.0, "what": "KXA-26: no row", "fix": "y"}, 30.0),
               ("gap:KXB-26", {"est": 9.0, "what": "KXB-26: no row", "fix": "z"}, 30.0)]
        fw.describe(Client(), due)
        _subject, body = fw.alert_body(due)
        self.assertIn("KXTSAD: newly paying\n    what it is: Daily TSA passenger "
                      "screenings (Economics, daily)", body)
        self.assertIn("what it is: Who will win the A primary? -- Nov 3, 2026", body)
        self.assertNotIn("title", due[2][1])            # a failed read: no line


class TestEventKeys(unittest.TestCase):

    def test_one_market_event_is_keyed_by_its_own_ticker(self):
        # KXTUREKOUTPERFORMRCP-26NOV03: the market ticker IS the event ticker
        day = {"start_date": "2026-10-08T00:00:00Z", "end_date": "2026-10-09T00:00:00Z"}
        progs = {"KXTUREKOUTPERFORMRCP-26NOV03": dict(day, period_reward=56 * 10000),
                 "KXHURCAT-26ISAIAS-T3": dict(day, period_reward=100 * 10000),
                 "KXHURCAT-26ISAIAS-T4": dict(day, period_reward=100 * 10000)}
        self.assertEqual(dict(fw._by_event(progs)), {
            "KXTUREKOUTPERFORMRCP-26NOV03": ["KXTUREKOUTPERFORMRCP-26NOV03"],
            "KXHURCAT-26ISAIAS": ["KXHURCAT-26ISAIAS-T3", "KXHURCAT-26ISAIAS-T4"]})
        pools = fw.event_pools(progs)
        self.assertAlmostEqual(pools["KXTUREKOUTPERFORMRCP-26NOV03"], 56.0)
        self.assertAlmostEqual(pools["KXHURCAT-26ISAIAS"], 200.0)
        self.assertNotIn("KXTUREKOUTPERFORMRCP", pools)


class TestEventMap(unittest.TestCase):

    def tearDown(self):
        fw._EVENT_OF.clear()

    def test_a_dash_in_the_strike_is_read_from_the_market_data(self):
        # KXSENGOPLEADRUN-26-27JAN01-JTHU is event KXSENGOPLEADRUN-26
        from unittest import mock
        day = {"start_date": "2026-10-09T00:00:00Z", "end_date": "2026-10-10T00:00:00Z",
               "period_reward": 10 * 10000}
        progs = {"KXSENGOPLEADRUN-26-27JAN01-JTHU": day,
                 "KXSENGOPLEADRUN-26-27JAN01-JBAR": day,
                 "KXHURCAT-26ISAIAS-T3": day}
        reads = []

        def bulk(client, tickers):
            reads.append(list(tickers))
            return {t: {"ticker": t, "event_ticker": "KXSENGOPLEADRUN-26"} for t in tickers}
        state = {}
        with mock.patch.object(fw.imm_quote_gaps, "bulk_market_details", bulk):
            self.assertEqual(fw.load_event_map(None, progs, state), 2)
            self.assertEqual(sorted(fw._by_event(progs)),
                             ["KXHURCAT-26ISAIAS", "KXSENGOPLEADRUN-26"])
            self.assertAlmostEqual(fw.event_pools(progs)["KXSENGOPLEADRUN-26"], 20.0)
            self.assertEqual(fw._item_markets("gap:KXSENGOPLEADRUN-26", progs),
                             ["KXSENGOPLEADRUN-26-27JAN01-JBAR",
                              "KXSENGOPLEADRUN-26-27JAN01-JTHU"])
            # cached in the state: the next run reads nothing
            fw._EVENT_OF.clear()
            self.assertEqual(fw.load_event_map(None, progs, state), 0)
        self.assertEqual(len(reads), 1)
        self.assertEqual(fw.event_key("KXSENGOPLEADRUN-26-27JAN01-JTHU"), "KXSENGOPLEADRUN-26")
        self.assertEqual(fw.event_key("KXHURCAT-26ISAIAS-T3"), "KXHURCAT-26ISAIAS")


class TestResearchOnArrival(unittest.TestCase):
    """Jack 2026-10-08: "instead of separate gap-fixer routine, why not just
    research when a new series enrolls?" -> "yes switch"."""

    T = {"gap:KXISR-26OCT27": {"kind": "election", "event": "KXISR-26OCT27",
                               "series": "KXISR", "msg": "no verified election day"},
         "start:KXTRUMPMENTION-26OCT08": {"kind": "start", "event": "KXTRUMPMENTION-26OCT08",
                                          "series": "KXTRUMPMENTION", "msg": "no start time"}}

    def test_gap_kinds(self):
        self.assertEqual(fw.gap_kind("KXA-26 has no verified election day in ELECTION_DATES"),
                         "election")
        self.assertEqual(fw.gap_kind("no hand-table row for KXA-27 -- on its family's earliest"),
                         "award")
        self.assertEqual(fw.gap_kind("pre-event stand-down needs the event start but KXA-27 "
                                     "has no usable date (hand table, ...)"), "award")
        self.assertEqual(fw.gap_kind("Vercel pre-D cutoff needs the measured day"), "")

    def test_due_on_first_sight_then_backoff_judgment_and_cap(self):
        t = 1_800_000_000.0
        self.assertEqual(sorted(fw.research_due({}, self.T, t)), sorted(self.T))
        r = {"gap:KXISR-26OCT27": {"attempts": 1, "last": t, "status": "unresolved"},
             "start:KXTRUMPMENTION-26OCT08": {"attempts": 1, "last": t, "status": "judgment"}}
        self.assertEqual(fw.research_due(r, self.T, t + 1.9 * 3600), [])
        self.assertEqual(fw.research_due(r, self.T, t + 2.1 * 3600), ["gap:KXISR-26OCT27"])
        r["gap:KXISR-26OCT27"].update(attempts=2, last=t)
        self.assertEqual(fw.research_due(r, self.T, t + 3.9 * 3600), [])  # 4h after try 2
        self.assertEqual(fw.research_due(r, self.T, t + 4.1 * 3600), ["gap:KXISR-26OCT27"])
        r["gap:KXISR-26OCT27"].update(attempts=fw.RESEARCH_MAX_ATTEMPTS)
        self.assertEqual(fw.research_due(r, self.T, t + 99 * 3600), [])
        # highest value first, capped
        many = {f"gap:KX{i}-26": dict(self.T["gap:KXISR-26OCT27"], event=f"KX{i}-26")
                for i in range(20)}
        est = {f"gap:KX{i}-26": float(i) for i in range(20)}
        due = fw.research_due({}, many, t, est)
        self.assertEqual(len(due), fw.RESEARCH_MAX_TARGETS)
        self.assertEqual(due[0], "gap:KX19-26")
        # no active program ($0, or absent from est): waits until one lights
        unpaid = {k: 0.0 for k in many}
        unpaid["gap:KX3-26"] = 2.0
        self.assertEqual(fw.research_due({}, many, t, unpaid), ["gap:KX3-26"])
        self.assertEqual(fw.research_due({}, self.T, t, {}), [])

    def test_prompt_names_each_target_its_command_and_the_result_line(self):
        p = fw.research_prompt([(k, v, "Kalshi says ...") for k, v in self.T.items()])
        self.assertIn("KEY gap:KXISR-26OCT27 -- election", p)
        self.assertIn("python imm_rows.py election add KXISR-26OCT27 YYYY-MM-DD", p)
        self.assertIn("python imm_earnings_overrides.py --set KXTRUMPMENTION-26OCT08", p)
        self.assertIn("TWO independent sources", p)
        self.assertIn("RESULT_JSON:", p)
        self.assertIn("NOT a vote", p)
        # 10/09: a minister-after-the-election market takes the vote's day
        self.assertIn("OR by what a named upcoming vote decides", p)

    def test_parse_result_line(self):
        txt = ("did the work\n```\nRESULT_JSON: {\"gap:KXISR-26OCT27\": {\"status\": "
               "\"written\", \"note\": \"2026-10-27 via a, b\"}}\n```")
        self.assertEqual(fw.parse_research(txt)["gap:KXISR-26OCT27"]["status"], "written")
        self.assertEqual(fw.parse_research("no result line"), {})
        self.assertEqual(fw.parse_research("RESULT_JSON: {not json"), {})

    def test_run_research_passes_the_allowlist_and_budget(self):
        seen = {}

        class P:
            returncode = 0
            stdout = json.dumps({"result": "RESULT_JSON: {}", "total_cost_usd": 0.42})
            stderr = ""

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return P()
        from unittest import mock
        with mock.patch.object(fw.subprocess, "run", fake_run):
            text, cost, err = fw.run_research("do it")
        self.assertEqual((text, cost, err), ("RESULT_JSON: {}", 0.42, ""))
        cmd = seen["cmd"]
        self.assertEqual(cmd[1:3], ["-p", "do it"])
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--restricted", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], f"{fw.RESEARCH_BUDGET_USD:g}")
        self.assertIn("Bash(python imm_rows.py:*)", cmd)
        self.assertIn("Bash(python imm_earnings_overrides.py --set:*)", cmd)
        self.assertEqual(seen["kw"]["cwd"], fw.KL_DIR)
        self.assertEqual(seen["kw"]["timeout"], fw.RESEARCH_TIMEOUT_SECS)

    def test_hold_until_research_gives_up_or_stale(self):
        t = 1_800_000_000.0
        items = {"gap:KXISR-26OCT27": {"est": 30.0, "what": "w", "fix": "f"},
                 "new:KXNEW": {"est": 30.0, "what": "n", "fix": "f"}}
        state = {"research": {"gap:KXISR-26OCT27": {"attempts": 1, "status": "unresolved",
                                                     "note": "page not up"}},
                 "items": {"gap:KXISR-26OCT27": {"first": t}}}
        fw.hold_researched(items, self.T, state, t + 3600)
        self.assertTrue(items["gap:KXISR-26OCT27"]["hold"])           # research pending
        self.assertNotIn("hold", items["new:KXNEW"])                  # not researchable
        self.assertIn("research (1 try): unresolved -- page not up",
                      items["gap:KXISR-26OCT27"]["fix"])
        state["research"]["gap:KXISR-26OCT27"]["attempts"] = 2        # gave up
        fw.hold_researched(items, self.T, state, t + 3600)
        self.assertFalse(items["gap:KXISR-26OCT27"]["hold"])
        state["research"]["gap:KXISR-26OCT27"].update(attempts=1, status="judgment")
        fw.hold_researched(items, self.T, state, t + 3600)
        self.assertFalse(items["gap:KXISR-26OCT27"]["hold"])
        state["research"]["gap:KXISR-26OCT27"].update(status="unresolved")
        fw.hold_researched(items, self.T, state, t + 7 * 3600)        # stale: alert anyway
        self.assertFalse(items["gap:KXISR-26OCT27"]["hold"])
        # a held item is tracked but never due
        due, st = fw.due_alerts({"gap:KXISR-26OCT27": {"first": t}},
                                {"gap:KXISR-26OCT27": dict(items["gap:KXISR-26OCT27"],
                                                           hold=True)}, t + 3 * 3600)
        self.assertEqual((due, set(st)), ([], {"gap:KXISR-26OCT27"}))

    def test_a_gap_a_row_now_covers_is_not_researched(self):
        # 10/08 14:38Z: the dating pass wrote 8 rows seconds before research
        # read the bot's (older) config_gaps.json and re-confirmed 7 of them
        from unittest import mock
        gaps = {"KXA-26NOV03": {"series": "KXA", "msg": "KXA-26NOV03 has no verified "
                                "election day in ELECTION_DATES"},
                "KXB-26NOV03": {"series": "KXB", "msg": "KXB-26NOV03 has no verified "
                                "election day in ELECTION_DATES"}}
        dated = lambda ev, market=None: (datetime(2026, 11, 3, 5, tzinfo=timezone.utc)
                                         if ev == "KXA-26NOV03" else None)
        with mock.patch.object(fw.imm, "election_cutoff_utc", dated), \
                mock.patch.object(fw.ieo, "discover_broadcast_mention_events",
                                  lambda c, now: []):
            t = fw.research_targets(None, gaps, datetime.now(timezone.utc), dry=True)
        self.assertEqual(sorted(t), ["gap:KXB-26NOV03"])

    def test_an_election_excluded_series_is_not_researched(self):
        # 10/08 (Jack: "Talarico should not be quoted"): the bot keeps the gap
        # in config_gaps.json until a restart, so research must skip it itself
        from unittest import mock
        self.assertIn("KXPUBLICTALARICO", fw.imm.ELECTION_EXCLUDE)
        self.assertFalse(fw.imm.election_series("KXPUBLICTALARICO"))
        msg = " has no verified election day in ELECTION_DATES"
        gaps = {"KXPUBLICTALARICO-26OCT15": {"series": "KXPUBLICTALARICO",
                                             "msg": "KXPUBLICTALARICO-26OCT15" + msg},
                "KXB-26NOV03": {"series": "KXB", "msg": "KXB-26NOV03" + msg}}
        with mock.patch.object(fw.imm, "election_cutoff_utc", lambda ev, market=None: None), \
                mock.patch.object(fw.ieo, "discover_broadcast_mention_events",
                                  lambda c, now: []):
            t = fw.research_targets(None, gaps, datetime.now(timezone.utc), dry=True)
        self.assertEqual(sorted(t), ["gap:KXB-26NOV03"])

    def test_live_gaps_drops_fixed_and_excluded_gaps(self):
        # the alert reads the same filter as research: a gap whose row landed
        # after the bot's last write (or an excluded series) is not dark
        from unittest import mock
        msg = " has no verified election day in ELECTION_DATES"
        gaps = {"KXA-26NOV03": {"series": "KXA", "msg": "KXA-26NOV03" + msg},
                "KXB-26NOV03": {"series": "KXB", "msg": "KXB-26NOV03" + msg},
                "KXX-26": {"series": "KXX", "msg": "KXX-26" + msg},
                "KXV-1": {"series": "KXV", "msg": "Vercel pre-D cutoff needs the measured day"}}
        dated = lambda ev, market=None: (datetime(2026, 11, 3, 5, tzinfo=timezone.utc)
                                         if ev == "KXA-26NOV03" else None)
        with mock.patch.object(fw.imm, "election_cutoff_utc", dated), \
                mock.patch.object(fw.imm, "ELECTION_EXCLUDE", frozenset({"KXX"})):
            self.assertEqual(sorted(fw.live_gaps(gaps)), ["KXB-26NOV03", "KXV-1"])

    def test_start_targets_become_alert_items(self):
        progs = {"KXTRUMPMENTION-26OCT08-AI": {
            "period_reward": 5000 * 10000, "start_date": "2026-10-07T00:00:00Z",
            "end_date": "2026-10-08T00:00:00Z"}}
        items = fw.gap_items(progs, {}, {}, set(), self.T)
        self.assertEqual(set(items), {"start:KXTRUMPMENTION-26OCT08"})
        self.assertAlmostEqual(items["start:KXTRUMPMENTION-26OCT08"]["est"],
                               5000 * fw.GAP_CAPTURE, places=2)


class TestRealEstimates(unittest.TestCase):
    """Jack 2026-10-09: "in the dark email, try to apply what the actual
    rewards would be not just a 2% proxy", then "it should also show $ per
    event (for the whole duration)"."""

    NOW = 1_800_000_000.0

    def _iso(self, days):
        from datetime import timedelta
        t = datetime.fromtimestamp(self.NOW, timezone.utc) + timedelta(days=days)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _progs(self, events=("KXISRINTMIN-26OCT27",)):
        # a 3-day program, 2 days left, $18/day a market
        p = {"start_date": self._iso(-1), "end_date": self._iso(2),
             "period_reward": 18 * 3 * 10000}
        return {f"{ev}-{c}": dict(p) for ev in events for c in ("A", "B", "C")}

    def _fakes(self, per_market, progs, unreadable=()):
        from types import SimpleNamespace
        from datetime import timedelta
        calls = {"bots": 0, "books": 0}
        end = datetime.fromtimestamp(self.NOW, timezone.utc) + timedelta(days=2)

        class Bot:
            def fetch_programs(self_):
                return {t: {"dollars_per_day": 18.0} for t in progs}

            def _estimate_candidate_yield(self_, meta, own):
                calls["books"] += 1
                if meta.ticker in unreadable:
                    return False
                meta.floor_dollars_per_day = per_market[meta.ticker]
                return True

        def make_bot(client):
            calls["bots"] += 1
            return Bot()

        class Client:
            def get(self_, path, params=None):
                ev = (params or {}).get("event_ticker")
                return {"markets": [{"ticker": t} for t in progs
                                    if fw.event_key(t) == ev]}

        def build_meta(bot, t, info, m, now):
            return SimpleNamespace(ticker=t, cutoff="sentinel", close_time=None,
                                   program_end=end, floor_dollars_per_day=0.0)
        return calls, make_bot, Client(), build_meta

    def test_the_bot_estimate_and_program_total_replace_the_proxy(self):
        from unittest import mock
        progs = self._progs()
        gaps = {"KXISRINTMIN-26OCT27": {"msg": "no verified election day"}}
        items = fw.gap_items(progs, gaps, {}, set())
        self.assertAlmostEqual(items["gap:KXISRINTMIN-26OCT27"]["est"], 3 * 18 * fw.GAP_CAPTURE, 2)
        per = {"KXISRINTMIN-26OCT27-A": 0.53, "KXISRINTMIN-26OCT27-B": 0.20,
               "KXISRINTMIN-26OCT27-C": 0.0}
        calls, make_bot, client, build_meta = self._fakes(per, progs)
        state = {}
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items, progs, state, self.NOW, make_bot=make_bot)
        it = items["gap:KXISRINTMIN-26OCT27"]
        self.assertAlmostEqual(it["est"], 0.73, 2)
        self.assertAlmostEqual(it["est_total"], 1.46, 2)          # 0.73/day x 2 days
        self.assertEqual(it["est_src"], "bot estimate, 3 markets, best $0.53/day")
        ev = it["est_events"][0]
        self.assertEqual(ev[:5], ["KXISRINTMIN-26OCT27", 0.73, 1.46, 2.0, 3.0])
        # cached: a second run reads no book and builds no bot
        items2 = fw.gap_items(progs, gaps, {}, set())
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items2, progs, state, self.NOW + 3600, make_bot=make_bot)
        self.assertEqual((calls["bots"], calls["books"]), (1, 3))
        self.assertAlmostEqual(items2["gap:KXISRINTMIN-26OCT27"]["est_total"], 1.46, 2)

    def test_unread_books_keep_the_proxy_and_say_so(self):
        from unittest import mock
        progs = self._progs()
        items = fw.gap_items(progs, {"KXISRINTMIN-26OCT27": {"msg": "no verified election day"}},
                             {}, set())
        per = {t: 0.5 for t in progs}
        calls, make_bot, client, build_meta = self._fakes(
            per, progs, unreadable={"KXISRINTMIN-26OCT27-C"})
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items, progs, {}, self.NOW, max_books=2,
                              make_bot=make_bot)
        it = items["gap:KXISRINTMIN-26OCT27"]
        proxy = 18 * fw.GAP_CAPTURE
        self.assertAlmostEqual(it["est"], 1.0 + proxy, 2)          # 2 read + 1 proxy
        self.assertAlmostEqual(it["est_total"], 2 * (1.0 + proxy), 2)
        self.assertIn("1 at pool proxy", it["est_src"])

    def test_a_series_item_is_split_per_event(self):
        from unittest import mock
        progs = self._progs(("KXNEW-26A", "KXNEW-26B"))
        per = {t: (1.0 if t.startswith("KXNEW-26A") else 0.25) for t in progs}
        items = fw.gap_items(progs, {}, {"KXNEW": 108.0}, set())
        calls, make_bot, client, build_meta = self._fakes(per, progs)
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items, progs, {}, self.NOW, make_bot=make_bot)
        it = items["new:KXNEW"]
        self.assertAlmostEqual(it["est"], 3.75, 2)
        self.assertAlmostEqual(it["est_total"], 7.5, 2)
        self.assertEqual([e[:3] for e in it["est_events"]],
                         [["KXNEW-26A", 3.0, 6.0], ["KXNEW-26B", 0.75, 1.5]])
        subject, body = fw.alert_body([("new:KXNEW", it, 90.0)])
        self.assertIn("1 quote state unknown (~$4/day) (~$8 over their programs)", subject)
        self.assertIn("~$3.75/day, ~$7.50 over the rest of its program (2.0 of 3.0 days, ends ", body)
        self.assertIn("per event: KXNEW-26A ~$3.00/day = ~$6.00 (2.0d left); "
                      "KXNEW-26B ~$0.75/day = ~$1.50 (2.0d left)", body)

    def test_held_and_unpaid_items_are_not_estimated(self):
        progs = self._progs()
        items = {"gap:KXISRINTMIN-26OCT27": {"est": 1.2, "what": "w", "fix": "f", "hold": True},
                 "gap:KXUNPAID-26": {"est": 0.0, "what": "w", "fix": "f"}}
        calls, make_bot, client, _ = self._fakes({}, progs)
        fw.real_estimates(client, items, progs, {}, self.NOW, make_bot=make_bot)
        self.assertEqual(calls["bots"], 0)
        self.assertNotIn("est_src", items["gap:KXISRINTMIN-26OCT27"])

    def test_alert_body_labels_the_source(self):
        due = [("gap:A", {"est": 12.5, "est_src": "bot estimate, 3 markets, best $5.00/day",
                          "est_total": 25.0,
                          "est_events": [["A", 12.5, 25.0, 2.0, 3.0, "2026-10-11T03:59:00+00:00"]],
                          "what": "A: x", "fix": "f"}, 90.0),
               ("new:KXB", {"est": 6.0, "what": "KXB: y", "fix": "g"}, 60.0)]
        subject, body = fw.alert_body(due)
        self.assertIn("~$12.50/day, ~$25.00 over the rest of its program (2.0 of 3.0 "
                      "days, ends Oct 11 03:59Z) (bot estimate, 3 markets, best $5.00/day)", body)
        self.assertIn("~$6.00/day (pool proxy)", body)
        self.assertNotIn("per event:", body)               # one event: no breakdown
        self.assertIn("no $1/market floor", body)


class TestResearchResilience(unittest.TestCase):
    """2026-10-10: a research run that wrote rows but printed no RESULT_JSON
    line, and a bot restart that emptied the gaps file for a run."""

    T = {"gap:KXALBERTAREFS-26OCT19": {"kind": "election", "event": "KXALBERTAREFS-26OCT19",
                                       "series": "KXALBERTAREFS", "msg": "m"},
         "gap:KXOBAMARALLY-26NOV03": {"kind": "election", "event": "KXOBAMARALLY-26NOV03",
                                      "series": "KXOBAMARALLY", "msg": "m"}}

    def test_no_result_line_is_checked_against_the_rows(self):
        from unittest import mock
        dated = lambda ev, market=None: (datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
                                         if ev == "KXALBERTAREFS-26OCT19" else None)
        rstate = {}
        with mock.patch.object(fw.imm, "election_cutoff_utc", dated), \
                mock.patch.object(fw.imm, "load_election_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_awards_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_file_event_overrides", lambda: 0):
            fw.apply_results(rstate, list(self.T), self.T, {}, "", 1_800_000_000.0)
        self.assertEqual(rstate["gap:KXALBERTAREFS-26OCT19"]["status"], "written")
        self.assertEqual(rstate["gap:KXOBAMARALLY-26NOV03"]["status"], "error")
        # an error retries after half an hour, not the 2h backoff
        t = 1_800_000_000.0
        self.assertEqual(fw.research_due(rstate, self.T, t + 0.4 * 3600), [])
        self.assertEqual(fw.research_due(rstate, self.T, t + 0.6 * 3600),
                         ["gap:KXOBAMARALLY-26NOV03"])

    def test_a_result_line_still_wins(self):
        from unittest import mock
        rstate = {}
        res = {"gap:KXOBAMARALLY-26NOV03": {"status": "judgment", "note": "a rally"}}
        with mock.patch.object(fw.imm, "election_cutoff_utc", lambda ev, market=None: None), \
                mock.patch.object(fw.imm, "load_election_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_awards_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_file_event_overrides", lambda: 0):
            fw.apply_results(rstate, ["gap:KXOBAMARALLY-26NOV03"], self.T, res, "", 1.0)
        self.assertEqual(rstate["gap:KXOBAMARALLY-26NOV03"]["status"], "judgment")

    def test_research_records_outlive_a_restart_blip(self):
        t = 1_800_000_000.0
        rstate = {"gap:KXOBAMARALLY-26NOV03": {"attempts": 1, "status": "judgment",
                                               "last": t - 86400, "seen": t}}
        kept = fw.prune_research(rstate, {}, t + 600)                 # gaps file empty
        self.assertEqual(set(kept), {"gap:KXOBAMARALLY-26NOV03"})
        self.assertEqual(fw.research_due(kept, self.T, t + 1200), ["gap:KXALBERTAREFS-26OCT19"])
        gone = fw.prune_research(rstate, {}, t + (fw.STATE_GRACE_HOURS + 0.1) * 3600)
        self.assertEqual(gone, {})
        back = fw.prune_research(rstate, self.T, t + 7200)
        self.assertEqual(back["gap:KXOBAMARALLY-26NOV03"]["seen"], t + 7200)


if __name__ == "__main__":
    unittest.main()


class TestQuoteCalls(unittest.TestCase):
    """Jack 2026-10-19: "In IMM watch emails, recommend if I should quote the
    market or not based on my strategies"."""

    TRACK = {"Politics & approval": {"rew": 801.0, "own": -259.0, "carried": -186.0,
                                     "quoting": 542.0, "net": 356.0},
             "Carbon Arc consumer": {"rew": 827.0, "own": -456.0, "carried": -520.0,
                                     "quoting": 371.0, "net": -149.0},
             "Sports & awards": {"rew": 531.0, "own": -872.0, "carried": -90.0,
                                 "quoting": -341.0, "net": -431.0}}

    def setUp(self):
        from unittest import mock
        self._p = [mock.patch.object(imm, "SERIES_BLOCKLIST_PREFIXES", ("KXHIGH", "KXLOW-")),
                   mock.patch.object(imm, "SCAN_EXCLUDE_PREFIXES", ("KXLOWT", "KXHIGH"))]
        for x in self._p:
            x.start()

    def tearDown(self):
        for x in self._p:
            x.stop()

    def call(self, key, fam="", **it):
        from unittest import mock
        it = dict({"est": 9.0, "est_total": 40.0, "what": key.split(":", 1)[1] + ": x",
                   "fix": "your call"}, **it)
        with mock.patch.object(fw, "family_name", lambda s, c="": fam):
            return fw.quote_call(key, it, self.TRACK)

    def test_another_bots_markets_are_never_quoted(self):
        c, why = self.call("new:KXHIGHTSDF")
        self.assertEqual(c, "DON'T QUOTE")
        self.assertIn("high_temp_trading.py (cloud) trades the KXHIGH dailies", why)
        self.assertIn("self-trade prevention", why)
        c, why = self.call("new:KXLOWTMIA")                 # scan-excluded, not blocklisted
        self.assertEqual(c, "DON'T QUOTE")
        self.assertIn("low_temp_trading.py", why)
        self.assertEqual(fw.owner_of("KXLOW-26OCT"), ("blocklisted in incentive_mm.py", False))
        self.assertEqual(fw.owner_of("KXFRANCEEMERGENCY"), ("", False))

    def test_rules_before_the_track_record(self):
        self.assertEqual(self.call("new:KXTINY", est_total=6.0)[0], "SKIP")
        self.assertEqual(self.call("new:KXMAMDANIMENTION", category="Mentions")[0],
                         "QUOTE WITH CARE")
        self.assertEqual(self.call("new:KXSENNEW", category="Elections")[0],
                         "QUOTE ONCE DATED")
        self.assertEqual(self.call("start:KXTRUMPMENTIONB-26OCT19")[0], "KEEP QUOTING")
        self.assertEqual(self.call("gap:KXSENGA-26", what="KXSENGA-26: no verified "
                                   "election day on file")[0], "QUOTE ONCE DATED")
        c, why = self.call("gap:KXRETIREMM-26", fix="research (1 try): judgment -- Not a "
                           "vote market: resolves on a resignation")
        self.assertEqual(c, "DON'T QUOTE")
        self.assertIn("non-vote political markets stay out", why)

    def test_the_family_it_would_join(self):
        c, why = self.call("new:KXBILLSNEW", fam="Politics & approval",
                           category="Politics", frequency="monthly")
        self.assertEqual(c, "QUOTE")
        self.assertIn("it would join Politics & approval: +$356 over the last 7 days "
                      "after rewards, its quoting +$542", why)
        c, why = self.call("new:KXNEWCC", fam="Carbon Arc consumer")
        self.assertEqual(c, "QUOTE WITH CARE")
        self.assertIn("the positions it held lost (-$520)", why)
        c, why = self.call("new:KXNEWSPORT", fam="Sports & awards")
        self.assertEqual(c, "NOT YET")
        self.assertIn("Sports & awards's own quoting lost $341", why)
        self.assertEqual(self.call("new:KXODD", fam="")[0], "YOUR CALL")

    def test_one_off_news_is_not_the_strategy(self):
        """Jack 2026-10-19: "Don't suggest quoting one-off news events. That's
        not part of my strategy is it?" -- a good family record never makes a
        one-off news event a QUOTE; a one-off vote waits for its date."""
        for tick, title in (("KXFRANCEEMERGENCY", "France state of emergency (Politics, one_off)"),
                            ("KXTRUMPXIPUTIN", "Trump, Xi and Putin trilateral meeting "
                                               "(Politics, one_off)"),
                            ("KXRUSDIESELIMPORT", "Will Russian diesel be imported into the "
                                                  "United States? (Politics, one_off)")):
            c, why = self.call("new:" + tick, fam="Politics & approval", category="Politics",
                               frequency="one_off", title=title)
            self.assertEqual(c, "DON'T QUOTE", tick)
            self.assertIn("not part of your strategy", why)
        for tick, title in (("KXSWEDENPMVOTE", "Swedish Prime Minister approval vote "
                                               "(Politics, one_off)"),
                            ("KXISRFIRSTMANDATE", "Who will get Israel's first mandate to form "
                                                  "a government? (Politics, one_off)")):
            c, why = self.call("new:" + tick, fam="Politics & approval", category="Politics",
                               frequency="one_off", title=title)
            self.assertEqual(c, "QUOTE ONCE DATED", tick)
        # a rally is news even with a vote word in it
        self.assertEqual(self.call("new:KXRALLY", fam="Politics & approval", frequency="one_off",
                                   title="Will he rally voters before the vote?")[0],
                         "DON'T QUOTE")
        # mentions keep their own rule
        self.assertEqual(self.call("new:KXXMENTION", category="Mentions",
                                   frequency="one_off")[0], "QUOTE WITH CARE")

    def test_track_record_and_the_email(self):
        F = ["rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
             "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled", "cr", "cs", "fx"]

        def row(rew, pnl, cr=0.0):
            r = [0.0] * len(F)
            r[F.index("rew")], r[F.index("pnl")], r[F.index("cr")] = rew, pnl, cr
            return r
        D = {"day_fields": F, "markets": {"KXA-1": {"fam": "Politics & approval"}},
             "days": {"2026-10-17": {"m": {"KXA-1": row(30.0, -10.0, -4.0)}},
                      "2026-10-18": {"m": {"KXA-1": row(20.0, 5.0)}},
                      "2026-10-19": {"m": {"KXA-1": row(999.0, 0.0)}}}}   # today: left out
        t = fw.family_track(D, "2026-10-19")["Politics & approval"]
        self.assertEqual((t["rew"], t["own"], t["carried"], t["quoting"], t["net"]),
                         (50.0, -1.0, -4.0, 49.0, 45.0))
        self.assertEqual(fw.family_track(None), {})
        due = [("new:KXHIGHTSDF", {"est": 51.3, "what": "KXHIGHTSDF: newly paying",
                                   "fix": "your call"}, 60.0),
               ("new:KXSTATERENAME", {"est": 12.0, "est_total": 62.7, "category": "Politics",
                                      "frequency": "weekly", "fix": "your call",
                                      "what": "KXSTATERENAME: newly paying"}, 60.0)]
        from unittest import mock
        with mock.patch.object(fw, "family_name",
                               lambda s, c="": "Politics & approval" if s == "KXSTATERENAME"
                               else ""):
            subject, body = fw.alert_body(due, {}, self.TRACK)
        self.assertTrue(subject.endswith("| QUOTE 1 ~$12/day, DON'T QUOTE 1 ~$51/day"))
        self.assertIn("\nCalls: QUOTE 1 (~$12/day), DON'T QUOTE 1 (~$51/day)\n", body)
        self.assertIn("    call: QUOTE -- it would join Politics & approval", body)
        self.assertIn("    call: DON'T QUOTE -- high_temp_trading.py", body)
        self.assertLess(body.index("call: DON'T"), body.index("fix: your call"))
        self.assertIn("(call = advice from your strategies, not an action", body)

    def test_model_coverage_gaps_get_a_call(self):
        import imm_model_coverage as mcov
        from unittest import mock
        shut = mcov._gap("Monthly snow (snow_monthly_fair)", "KXTTNSNOWM-26DEC",
                         ["KXTTNSNOWM-26DEC-12"], "closed", "no station", "add the city")
        opn = mcov._gap("Daily rain (rain_fair)", "BNA", ["KXRAIN-26OCT12-BNA"],
                        "open", "no station", "add a row")
        with mock.patch.object(fw, "family_name", lambda s, c="": "Politics & approval"):
            self.assertEqual(fw.model_gap_call(shut, self.TRACK)[0], "QUOTE ONCE MODELED")
            self.assertEqual(fw.model_gap_call(opn, self.TRACK)[0], "QUOTE WITH CARE")
            _s, text = mcov.body([(shut["key"], shut, 0.0, True)],
                                 call=lambda g: fw.model_gap_call(g, self.TRACK))
        self.assertIn("      call: QUOTE ONCE MODELED -- it sits dark until the model has it",
                      text)
        self.assertNotIn("call:", mcov.body([(shut["key"], shut, 0.0, True)])[1])


class TestModelCoveragePass(unittest.TestCase):
    """Pass 5 (Jack 2026-10-09: "ensure i get alerted"): gaps are emailed,
    and a failing pass is emailed too -- at most daily -- so it can never go
    quiet."""

    def test_gaps_are_emailed_and_a_failing_pass_says_so(self):
        from unittest import mock
        import imm_model_coverage as mcov
        sent = []
        gap = mcov._gap("Daily rain (rain_fair)", "BNA", ["KXRAIN-26OCT12-BNA"],
                        "open", "no station", "add a row")
        with mock.patch.object(imm.Alerter, "send_message",
                               lambda self, body, subject=None: sent.append(subject) or True), \
                mock.patch.object(mcov, "find_gaps", lambda progs, now: {gap["key"]: gap}):
            state, summary = {}, []
            fw.model_coverage_pass({}, state, False, summary)
            self.assertEqual(len(sent), 1)
            self.assertIn("model coverage: 1 gap(s)", sent[0])
            fw.model_coverage_pass({}, state, False, summary)   # same gap, same day: quiet
            self.assertEqual(len(sent), 1)

        def boom(progs, now):
            raise RuntimeError("fair file unreadable")
        with mock.patch.object(imm.Alerter, "send_message",
                               lambda self, body, subject=None: sent.append(subject) or True), \
                mock.patch.object(mcov, "find_gaps", boom):
            fw.model_coverage_pass({}, state, False, [])
            fw.model_coverage_pass({}, state, False, [])         # a second failure: no repeat
        self.assertEqual(sent[1:], ["IMM-WATCH model coverage pass FAILED"])


class TestResearchLoginFailure(unittest.TestCase):
    """Jack 2026-10-12: "send an email right away when research fails on a
    login error, instead of waiting until items give up"."""

    EV = "KXWORLDNEWSMENTION-26OCT12"
    KEY = "start:" + EV
    T = {KEY: {"kind": "start", "event": EV, "series": "KXWORLDNEWSMENTION",
               "msg": "no start time"}}
    EXPIRED = ("Failed to authenticate: OAuth session expired and could not be "
               "refreshed", 0.0, "exit 1: ")

    def test_only_a_failed_runs_login_error_counts(self):
        self.assertEqual(fw.research_login_failure(*self.EXPIRED[::2]), self.EXPIRED[0])
        self.assertTrue(fw.research_login_failure("Invalid API key · Please run /login",
                                                  "exit 1: "))
        self.assertEqual(fw.research_login_failure(self.EXPIRED[0], ""), "")   # exit 0
        self.assertEqual(fw.research_login_failure("Exceeded USD budget (3)", "exit 1: "), "")
        self.assertEqual(fw.research_login_failure("", "TimeoutExpired: 720s"), "")

    def _run(self, state, t, result):
        from unittest import mock
        sent = []
        with mock.patch.object(fw, "research_targets", lambda *a, **k: dict(self.T)), \
                mock.patch.object(fw.ifa, "fetch_active_programs", lambda client: {}), \
                mock.patch.object(fw, "event_pools", lambda progs: {self.EV: 1000.0}), \
                mock.patch.object(fw.imm_rows, "lookup", lambda ev: "ctx"), \
                mock.patch.object(fw, "run_research", lambda prompt: result), \
                mock.patch.object(fw.time, "time", lambda: t), \
                mock.patch.object(fw.imm, "election_cutoff_utc", lambda ev, market=None: None), \
                mock.patch.object(fw.imm, "load_election_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_awards_dates_extra", lambda: 0), \
                mock.patch.object(fw.imm, "load_file_event_overrides", lambda: 0), \
                mock.patch.object(imm.Alerter, "send_message",
                                  lambda self, body, subject=None: sent.append((subject, body)) or True):
            _, notes = fw.research_pass(None, {}, state, False)
        return sent, notes

    def test_emailed_at_once_then_every_6h_and_no_try_is_spent(self):
        t, state = 1_800_000_000.0, {}
        sent, notes = self._run(state, t, self.EXPIRED)
        self.assertEqual([s for s, _ in sent], ["IMM-WATCH research DOWN: Claude Code login failed"])
        body = sent[0][1]
        self.assertIn(self.EXPIRED[0], body)
        self.assertIn(self.KEY, body)
        self.assertIn(f"~${1000 * fw.GAP_CAPTURE:.2f}/day", body)  # the pool proxy
        self.assertIn("/login", body)
        self.assertTrue(any("LOGIN FAILED" in n for n in notes))
        self.assertNotIn(self.KEY, state["research"])            # no try spent
        self.assertEqual(fw.research_due(state["research"], self.T, t + 60), [self.KEY])
        sent, _ = self._run(state, t + 1800, self.EXPIRED)       # 30 min on: quiet
        self.assertEqual(sent, [])
        sent, _ = self._run(state, t + 6.1 * 3600, self.EXPIRED)  # still down 6h on
        self.assertEqual(len(sent), 1)
        self.assertIn("failing since 2027-01-15 08:00Z", sent[0][1])

    def test_a_working_run_clears_it_and_the_next_failure_emails_at_once(self):
        t, state = 1_800_000_000.0, {}
        self._run(state, t, self.EXPIRED)
        ok = ('RESULT_JSON: {"%s": {"status": "unresolved", "note": "not listed"}}'
              % self.KEY, 0.12, "")
        sent, _ = self._run(state, t + 1800, ok)
        self.assertEqual(sent, [])
        self.assertNotIn("research_login", state)
        self.assertEqual(state["research"][self.KEY]["attempts"], 1)
        sent, _ = self._run(state, t + 3600, self.EXPIRED)       # in its 2h backoff: no run
        self.assertEqual(sent, [])
        sent, _ = self._run(state, t + 1800 + 2.1 * 3600, self.EXPIRED)
        self.assertEqual(len(sent), 1)
        self.assertEqual(state["research"][self.KEY]["attempts"], 1)

    def test_a_failed_send_is_retried_next_run(self):
        from unittest import mock
        state = {}
        with mock.patch.object(imm.Alerter, "send_message", lambda self, body, subject=None: False):
            self.assertFalse(fw.alert_login_failure(state, "x", [], {}, 1.0))
        self.assertNotIn("alerted", state["research_login"])
        with mock.patch.object(imm.Alerter, "send_message", lambda self, body, subject=None: True):
            self.assertTrue(fw.alert_login_failure(state, "x", [], {}, 1801.0))


class TestResearchToken(unittest.TestCase):
    """Jack 2026-10-12: "do it for me" -- research runs on a one-year
    `claude setup-token` token instead of the interactive login."""

    TOKEN = "sk-ant-oat01-" + "Ab3_x-" * 8

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix="tok_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "imm-research-token.txt")
        p = mock.patch.object(fw, "RESEARCH_TOKEN_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as f:
            f.write(text)

    def test_the_token_is_read_bare_or_from_a_pasted_output(self):
        self.assertEqual((fw.research_token(), fw.research_auth()), ("", "stored login"))
        self._write(self.TOKEN + "\r\n")
        self.assertEqual((fw.research_token(), fw.research_auth()), (self.TOKEN, "token file"))
        self._write("Long-lived authentication token created successfully!\n\n"
                    f"Your OAuth token (valid for 1 year):\n\n{self.TOKEN}\n\n"
                    "Store this token securely.")
        self.assertEqual(fw.research_token(), self.TOKEN)
        self._write(self.TOKEN, encoding="utf-8-sig")                 # Notepad's BOM
        self.assertEqual(fw.research_token(), self.TOKEN)
        self._write("  \n")
        self.assertEqual((fw.research_token(), fw.research_auth()),
                         ("", "stored login (no token in the token file)"))
        self._write("paste the token here")
        self.assertEqual(fw.research_token(), "")

    def test_only_the_research_run_gets_the_token_and_no_api_key(self):
        from unittest import mock
        seen = {}

        class P:
            returncode = 0
            stdout = json.dumps({"result": "OK", "total_cost_usd": 0.01})
            stderr = ""

        def fake_run(cmd, **kw):
            seen["env"] = kw["env"]
            return P()
        with mock.patch.object(fw.subprocess, "run", fake_run), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t"}):
            fw.run_research("x")                                       # no file: as before
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["env"])
            self.assertEqual(seen["env"]["ANTHROPIC_API_KEY"], "k")
            self._write(self.TOKEN)
            fw.run_research("x")
            self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], self.TOKEN)
            self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", seen["env"])
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)    # the watch's own env

    def test_the_login_email_names_the_fix_for_the_login_in_use(self):
        from unittest import mock
        bodies = []
        with mock.patch.object(imm.Alerter, "send_message",
                               lambda self, body, subject=None: bodies.append(body) or True):
            fw.alert_login_failure({}, "x", [], {}, 1.0)
            self._write(self.TOKEN)
            fw.alert_login_failure({}, "x", [], {}, 1.0)
        self.assertIn("/login", bodies[0])
        self.assertIn("claude setup-token", bodies[0])
        self.assertIn("was rejected", bodies[1])
        self.assertNotIn(self.TOKEN, bodies[1])

    def test_renewal_reminder_from_day_350_then_weekly(self):
        from unittest import mock
        now = 1_800_000_000.0
        self.assertIsNone(fw.token_renewal_due({}, now))               # no token
        self._write(self.TOKEN)
        os.utime(self.path, (now - 349 * 86400, now - 349 * 86400))
        self.assertIsNone(fw.token_renewal_due({}, now))
        os.utime(self.path, (now - 351 * 86400, now - 351 * 86400))
        sent = []
        with mock.patch.object(imm.Alerter, "send_message",
                               lambda self, body, subject=None: sent.append((subject, body)) or True):
            state = {}
            self.assertTrue(fw.remind_token_renewal(state, now, False))
            self.assertFalse(fw.remind_token_renewal(state, now + 6 * 86400, False))
            self.assertTrue(fw.remind_token_renewal(state, now + 7.1 * 86400, False))
            self.assertFalse(fw.remind_token_renewal({}, now, True))    # dry: log only
        self.assertEqual([s for s, _ in sent], ["IMM-WATCH renew the research token"] * 2)
        self.assertIn("351 days", sent[0][1])
        self.assertNotIn(self.TOKEN, sent[0][1])

    def test_check_login(self):
        from unittest import mock
        with mock.patch.object(fw, "run_research", lambda p: ("OK", 0.01, "")):
            self.assertEqual(fw.check_login(), 0)
        with mock.patch.object(fw, "run_research",
                               lambda p: ("Failed to authenticate: OAuth session expired",
                                          0.0, "exit 1: ")):
            self.assertEqual(fw.check_login(), 1)
