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


if __name__ == "__main__":
    unittest.main()
