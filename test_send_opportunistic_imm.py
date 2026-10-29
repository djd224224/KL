#!/usr/bin/env python3
"""Unit tests for send_opportunistic_imm.py's new-since-last-email marking
(Jack 2026-09-13: "always bold the events that are new (werent in the
previous email)"). Run: python -m unittest test_send_opportunistic_imm"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import send_opportunistic_imm as so


LOG = """2026-09-12 11:25:01Z opportunistic body:
Opportunistic IMM - 2026-09-12

FINECON - curated
22/25 slots, +0/10 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
OLDONLY-26SEP30            an event only in the old one     3             -      26.41    18.77     -3.90    +22.51
TOTAL                                                      22    3        -      81.45    53.16    -40.40    +41.05
2026-09-12 11:25:02Z opportunistic send: ok
2026-09-13 11:25:08Z opportunistic body:
Opportunistic IMM - 2026-09-13

FINECON - curated Finance/Economics quiet prints
22/25 slots, +0/10 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
AAAGASMINM-26SEP30         Monthly low US gas price         3             -      26.41    18.77     -3.90    +22.51
CBDECISIONNZ-26OCT27       RBNZ rate decision               0    1        -       6.33    10.92     +5.40    +11.73
TOTAL                                                      22    3        -      81.45    53.16    -40.40    +41.05

OPEN SCAN - all other markets
35/30 slots, +0/5 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
BABELMANDEBWEEKLY-26SEP20  Traffic through the Bab el-M     1          4.41       9.23              +0.00     +9.23
TOTAL                                                       1             -       9.23              +0.00     +9.23

CUMULATIVE - CREDITED and REALIZED cover every event
TIER             EVENTS  CREDITED$      EST$  REALIZED$      MTM$      NET$
FINECON              47     162.10     81.45     -12.30    -28.10    +41.05
2026-09-13 11:25:09Z opportunistic send: ok
2026-09-13 14:50:00Z opportunistic body:
Opportunistic IMM - 2026-09-13 (a --dry run: never sent, must not count)
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
DRYONLY-26SEP30            only in the dry run              1             -       1.00              +0.00     +1.00
TOTAL                                                       1             -       1.00              +0.00     +1.00
"""


def _row(ev, net=1.0):
    return {"event": ev, "label": "what it is", "tier": "scan", "mkts": 1,
            "held": 0, "earn": 1.0, "period": None, "cred": 0.0,
            "pnl": 0.0, "net": net, "pos": 0}


class TestParseLastSentBody(unittest.TestCase):
    def test_last_SENT_body_wins_and_a_dry_body_does_not_count(self):
        ts, evs = so.parse_last_sent_body(LOG)
        self.assertEqual(ts, "2026-09-13 11:25:08Z")
        self.assertEqual(evs, {"AAAGASMINM-26SEP30", "CBDECISIONNZ-26OCT27",
                               "BABELMANDEBWEEKLY-26SEP20"})
        self.assertNotIn("DRYONLY-26SEP30", evs)      # dry body, never sent
        self.assertNotIn("OLDONLY-26SEP30", evs)      # the email before last
        self.assertNotIn("FINECON", evs)              # cumulative TIER rows

    def test_no_sent_body(self):
        self.assertEqual(so.parse_last_sent_body(""), (None, None))
        self.assertEqual(so.parse_last_sent_body(
            "2026-09-13 14:50:00Z opportunistic body:\nEVENT   x\nA-1  y\n"),
            (None, None))


class TestPreviousEmailEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opp_test_")
        self._old = (so.LAST_SENT_PATH, so.TASK_LOG_PATH)
        so.LAST_SENT_PATH = os.path.join(self.tmp, "last.json")
        so.TASK_LOG_PATH = os.path.join(self.tmp, "task.log")

    def tearDown(self):
        so.LAST_SENT_PATH, so.TASK_LOG_PATH = self._old

    def test_none_when_nothing_exists(self):
        self.assertEqual(so.previous_email_events(), (None, None, "none"))

    def test_log_fallback_then_record_precedence(self):
        with open(so.TASK_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(LOG)
        ts, evs, src = so.previous_email_events()
        self.assertEqual((src, ts), ("log", "2026-09-13 11:25:08Z"))
        self.assertIn("AAAGASMINM-26SEP30", evs)
        # a record written by a send wins over the log, and is stored as
        # FULL event tickers but compared as short names
        now = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)
        so.write_last_sent(now, ["KXNEWREC-26OCT01", "KXOTHER-26OCT01"])
        ts2, evs2, src2 = so.previous_email_events()
        self.assertEqual(src2, "record")
        self.assertEqual(evs2, {"NEWREC-26OCT01", "OTHER-26OCT01"})
        self.assertTrue(ts2.startswith("2026-09-13T15:00"))
        with open(so.LAST_SENT_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["events"],
                             ["KXNEWREC-26OCT01", "KXOTHER-26OCT01"])


class TestRenderingMarksOnlyNewRows(unittest.TestCase):
    ROWS = [_row("KXNEWONE-26OCT01", 2.0), _row("KXOLDONE-26OCT01", 1.0)]

    def test_html_bolds_only_the_new_row(self):
        h = so.html_table(self.ROWS, new_events={"KXNEWONE-26OCT01"})
        self.assertIn("<b>NEWONE-26OCT01</b>", h)
        self.assertIn("NEW</span>", h)
        self.assertNotIn("<b>OLDONE-26OCT01</b>", h)
        self.assertIn(">OLDONE-26OCT01<", h)
        # nothing new -> nothing bold in the EVENT column at all
        h0 = so.html_table(self.ROWS)
        self.assertNotIn("<b>NEWONE", h0)
        self.assertNotIn("NEW</span>", h0)

    def test_text_twin_marks_with_an_asterisk(self):
        L = so.text_table(self.ROWS, new_events={"KXNEWONE-26OCT01"})
        body = "\n".join(L)
        self.assertIn("*NEWONE-26OCT01", body)
        self.assertNotIn("*OLDONE", body)
        # the 27-character EVENT column is preserved with the marker
        row = [l for l in L if "NEWONE" in l][0]
        self.assertEqual(row[:27].strip(), "*NEWONE-26OCT01")


class TestDailyPicksTier(unittest.TestCase):
    """Jack 2026-10-27: "... quote it at 1x size, and add it to the
    opportunistic daily report email" -- the AS test's daily picks as a third
    tier, membership by every event ever picked."""

    DOC = {"generated_at": "2026-10-28T14:30:00Z",
           "considered": {"eligible": 2, "scored": 9, "events": 70},
           "model": {"as_mult": 3.0, "as_floor_c": 1.0, "hist_events": 5,
                     "min_p_profit": 0.6, "min_net": 0.5},
           "picks": [{"event": "KXPICKA-26OCT30", "p_profit": 0.8, "n_hist": 5,
                      "net_mean": 2.5, "reward": 3.1, "n_markets": 2, "fills": 12,
                      "cost_c": 4.5, "fair": "buildable", "fair_source": "eia.gov",
                      "title": "Will A be above 5?"}],
           "ranked": [{"event": "KXPICKA-26OCT30"},
                      {"event": "KXPICKB-26OCT30", "p_profit": 0.4, "n_hist": 5,
                       "net_mean": -1.0, "eligible": False, "title": "B"}]}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opp_picks_")
        self._old = (so.PICKS_PATH, so.PICKS_HISTORY_PATH)
        so.PICKS_PATH = os.path.join(self.tmp, "astest_picks.json")
        so.PICKS_HISTORY_PATH = os.path.join(self.tmp, "astest_picks_history.jsonl")

    def tearDown(self):
        so.PICKS_PATH, so.PICKS_HISTORY_PATH = self._old

    def test_every_event_ever_picked_is_the_tiers_markets_by_event(self):
        with open(so.PICKS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.DOC, f)
        with open(so.PICKS_HISTORY_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps({"picks": ["KXOLDPICK-26OCT20"]}) + "\nnot json\n")
        doc, ever = so.load_picks()
        self.assertEqual(doc["generated_at"], self.DOC["generated_at"])
        self.assertEqual(ever, {"KXPICKA-26OCT30", "KXOLDPICK-26OCT20"})
        self.assertEqual(so.tier_of("KXPICKA-26OCT30-T5", set(), set(), ever), "picks")
        self.assertEqual(so.tier_of("KXOLDPICK-26OCT20", set(), set(), ever), "picks")
        self.assertIsNone(so.tier_of("KXPICKA-26NOV06-T5", set(), set(), ever))
        self.assertIsNone(so.tier_of("KXPICKA-26OCT30-T5", set(), set()))
        # finecon and the scan keep their own rows
        self.assertEqual(so.tier_of("KXPICKA-26OCT30-T5", {"KXPICKA"}, set(), ever),
                         "finecon")

    def test_no_files_no_picks(self):
        self.assertEqual(so.load_picks(), ({}, set()))
        now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        self.assertIn("no picks file yet", so.picks_status({}, now))

    def test_the_tier_line_and_the_picks_listed_with_the_pickers_numbers(self):
        now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        line = so.picks_status(self.DOC, now)
        self.assertIn("1/5 picks from the Oct 28 10:30 ET run", line)
        self.assertIn("2 eligible of 9 scored", line)
        self.assertNotIn("LAPSED", line)
        self.assertIn("LAPSED", so.picks_status(
            self.DOC, datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)))
        text, html = so.picks_detail(self.DOC)
        body = "\n".join(text)
        self.assertIn("PICKA-26OCT30", body)
        self.assertIn("P(profit) 80% over 5 past events, est net $+2.50", body)
        self.assertIn("fair buildable (eia.gov)", body)
        self.assertIn("Next best, not picked:", body)
        self.assertIn("PICKB-26OCT30", body)
        self.assertIn("[not eligible]", body)
        self.assertIn("3x the tape's maker loss", body)
        self.assertIn("PICKA-26OCT30", html)
        # a listed pick is not a table row for the new-since-last-email read
        log = ("2026-10-29 11:25:01Z opportunistic body:\n" + body
               + "\n2026-10-29 11:25:02Z opportunistic send: ok\n")
        self.assertEqual(so.parse_last_sent_body(log)[1], set())
        self.assertEqual(so.picks_detail({})[0], ["Picks in force: none.",
                                                  so.picks_detail({})[0][-1]])


class TestAsTestTier(unittest.TestCase):
    """Jack 2026-10-28: "i thought my new opportunitisc bot events e.g.
    votehub would be in my opportunistic morning email?" -- the AS test's
    named families as a tier of their own, by the bot's series sets."""

    NOW = datetime(2026, 10, 29, 11, 25, tzinfo=timezone.utc)

    def test_the_named_families_are_the_tiers_markets(self):
        for t, fam in (("KXVHGCB-26OCT29-E8.1", "VH"), ("KXVHGCB-26OCT29", "VH"),
                       ("KXBBSINGLE-26NOV07THRILLER-16T20", "BB"),
                       ("KXAAAGASDMS-26OCT27-3.825", "GAS"),
                       ("KXAAAGASDMS-26OCT27", "GAS")):
            self.assertEqual(so.astest_family_of(t), fam, t)
            self.assertEqual(so.tier_of(t, set(), set()), "astest", t)
        # the same state under the normal book before 9/14, a state outside
        # the test, the national and the rest of the book are not the test's
        for t in ("KXAAAGASDMS-26SEP14-3.1", "KXAAAGASDCA-26OCT27-4.5",
                  "KXAAAGASD-26OCT27-3.1", "KXTRUMPMENTION-26OCT29-WALL"):
            self.assertIsNone(so.astest_family_of(t), t)
            self.assertIsNone(so.tier_of(t, set(), set()), t)
        # a gas state the daily picks name is the picks' own; finecon first
        self.assertEqual(so.tier_of("KXAAAGASDCT-26OCT30-4.2", set(), set(),
                                    {"KXAAAGASDCT-26OCT30"}), "picks")
        self.assertEqual(so.tier_of("KXVHGCB-26OCT29-E8.1", {"KXVHGCB"}, set()),
                         "finecon")

    def test_the_tier_line_says_which_families_the_bot_is_quoting(self):
        tmp = tempfile.mkdtemp(prefix="opp_astest_")
        line = so.astest_status({}, self.NOW, tmp)
        self.assertIn("Billboard KXBBSINGLE x3 (150/market, 450/event, $100/day "
                      "loss halt; out 8 days before the chart date", line)
        self.assertIn("VoteHub KXVHGCB x3 (150/market, 450/event, $100/day loss "
                      "halt; 20:00-05:00 ET only, out at 05:00 ET on the read day): ON",
                      line)
        self.assertIn("gas states CT/MS/NY/TN/UT x1 (50/market, 150/event, "
                      "$100/day loss halt; 16:00-24:00 ET): ON", line)
        # a stop file and a loss halt for the roll day (STATUS_DIR is the
        # temp dir here, never the live run-logs)
        with open(os.path.join(tmp, "astest_stop.txt"), "w", encoding="utf-8") as f:
            f.write("stopped\n")
        state = {"twk_halt_day": {"ASTEST:VH": so.imm._halt_day_key(self.NOW),
                                  "ASTEST:BB": "2026-10-27"}}
        line = so.astest_status(state, self.NOW, tmp)
        self.assertIn("16:00-24:00 ET): STOPPED since", line)
        self.assertIn("(astest_stop.txt)", line)
        self.assertIn("on the read day): HALTED today (loss budget)", line)
        self.assertIn("(its tracking week's end)): ON", line)


if __name__ == "__main__":
    unittest.main()
