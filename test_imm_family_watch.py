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
        self.assertIn("~$44.00/day (pool proxy), dark 1.6h  KXA-26: no row", body)
        self.assertIn("fix: add a row", body)


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
    rewards would be not just a 2% proxy"."""

    DAY = {"start_date": "2026-10-09T00:00:00Z", "end_date": "2026-10-10T00:00:00Z"}

    def _progs(self):
        return {f"KXISRINTMIN-26OCT27-{c}": dict(self.DAY, period_reward=18 * 10000)
                for c in ("A", "B", "C")}

    def _fakes(self, per_market, unreadable=()):
        from types import SimpleNamespace
        calls = {"bots": 0, "books": 0}

        class Bot:
            def fetch_programs(self_):
                return {t: {"dollars_per_day": 18.0} for t in self._progs()}

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
                return {"markets": [{"ticker": t} for t in self._progs()]}

        def build_meta(bot, t, info, m, now):
            return SimpleNamespace(ticker=t, cutoff="sentinel", floor_dollars_per_day=0.0)
        return calls, make_bot, Client(), build_meta

    def test_the_bot_estimate_replaces_the_proxy(self):
        from unittest import mock
        progs = self._progs()
        items = fw.gap_items(progs, {"KXISRINTMIN-26OCT27": {"msg": "no verified election day"}},
                             {}, set())
        self.assertAlmostEqual(items["gap:KXISRINTMIN-26OCT27"]["est"], 3 * 18 * fw.GAP_CAPTURE, 2)
        per = {"KXISRINTMIN-26OCT27-A": 0.53, "KXISRINTMIN-26OCT27-B": 0.20,
               "KXISRINTMIN-26OCT27-C": 0.0}
        calls, make_bot, client, build_meta = self._fakes(per)
        state = {}
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items, progs, state, 1_800_000_000.0, make_bot=make_bot)
        it = items["gap:KXISRINTMIN-26OCT27"]
        self.assertAlmostEqual(it["est"], 0.73, 2)
        self.assertEqual(it["est_src"], "bot estimate, 3 markets, best $0.53")
        # cached: a second run reads no book and builds no bot
        items2 = fw.gap_items(progs, {"KXISRINTMIN-26OCT27": {"msg": "x election day"}}, {}, set())
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items2, progs, state, 1_800_000_000.0 + 3600,
                              make_bot=make_bot)
        self.assertEqual((calls["bots"], calls["books"]), (1, 3))
        self.assertAlmostEqual(items2["gap:KXISRINTMIN-26OCT27"]["est"], 0.73, 2)

    def test_unread_books_keep_the_proxy_and_say_so(self):
        from unittest import mock
        progs = self._progs()
        items = fw.gap_items(progs, {"KXISRINTMIN-26OCT27": {"msg": "no verified election day"}},
                             {}, set())
        per = {"KXISRINTMIN-26OCT27-A": 0.5, "KXISRINTMIN-26OCT27-B": 0.5,
               "KXISRINTMIN-26OCT27-C": 0.5}
        calls, make_bot, client, build_meta = self._fakes(per, unreadable={"KXISRINTMIN-26OCT27-C"})
        with mock.patch.object(fw.imm_quote_gaps, "build_meta", build_meta):
            fw.real_estimates(client, items, progs, {}, 1_800_000_000.0, max_books=2,
                              make_bot=make_bot)
        it = items["gap:KXISRINTMIN-26OCT27"]
        self.assertAlmostEqual(it["est"], 1.0 + 18 * fw.GAP_CAPTURE, 2)   # 2 read + 1 proxy
        self.assertIn("1 at pool proxy", it["est_src"])

    def test_held_and_unpaid_items_are_not_estimated(self):
        progs = self._progs()
        items = {"gap:KXISRINTMIN-26OCT27": {"est": 1.2, "what": "w", "fix": "f", "hold": True},
                 "gap:KXUNPAID-26": {"est": 0.0, "what": "w", "fix": "f"}}
        calls, make_bot, client, _ = self._fakes({})
        fw.real_estimates(client, items, progs, {}, 1_800_000_000.0, make_bot=make_bot)
        self.assertEqual(calls["bots"], 0)
        self.assertNotIn("est_src", items["gap:KXISRINTMIN-26OCT27"])

    def test_alert_body_labels_the_source(self):
        due = [("gap:A", {"est": 12.5, "est_src": "bot estimate, 3 markets, best $5.00",
                          "what": "A: x", "fix": "f"}, 90.0),
               ("new:KXB", {"est": 6.0, "what": "KXB: y", "fix": "g"}, 60.0)]
        subject, body = fw.alert_body(due)
        self.assertIn("~$12.50/day (bot estimate, 3 markets, best $5.00)", body)
        self.assertIn("~$6.00/day (pool proxy)", body)
        self.assertIn("no $1/market floor", body)


if __name__ == "__main__":
    unittest.main()
