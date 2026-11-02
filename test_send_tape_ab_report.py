"""Tests for send_tape_ab_report.py (the tape trigger's live A/B email)."""
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

import send_tape_ab_report as em
from test_imm_tape_ab_report import make_fixture


class _FakeAlerter:
    sent = []

    def __init__(self, *a, **k):
        self.enabled = True

    def send_message(self, text, subject="", html=None):
        _FakeAlerter.sent.append((subject, text, html))
        return True


class TestTapeABEmail(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tape_ab_email_")
        self.addCleanup(shutil.rmtree, self.d, True)
        make_fixture(self.d)                 # one UTC day, 11/02, of A/B logs
        self.logged = []
        for target, name, v in ((em.imm, "STATUS_DIR", self.d),
                                (em.lm, "low_priority", lambda: None),
                                (em.ab, "family", lambda s: "Fam " + s),
                                (em.imm, "Alerter", _FakeAlerter),
                                (em, "log", self.logged.append)):
            p = mock.patch.object(target, name, v)
            p.start()
            self.addCleanup(p.stop)
        _FakeAlerter.sent = []

    def test_the_day_and_the_running_total_money_first(self):
        ctx = em.ab.build(self.d, em.SINCE, date(2026, 11, 2))
        text = em.render_text(ctx)
        self.assertIn("YESTERDAY: 1 windows paused, 2 controls. The pauses made +$0.37 at "
                      "4 h (+$0.12 at 5 min), after the $0.13 of reward they gave up. "
                      "Pausing every window would have made +$1.11.", text)
        self.assertIn("SO FAR (11/02 to 11/02): 1 windows paused, 2 controls. The pauses "
                      "made +$0.37 at 4 h (the stop line is -$150).", text)
        self.assertIn("VERDICT: RUNNING -- keep it running.", text)
        self.assertIn("  11/02: 1 paused / 2 controls, the pauses +$0.37; so far +$0.37", text)
        self.assertIn("CHECKS: 1 of 3 windows drew pause (expected 50%); every draw "
                      "recomputes from the log; every fill marked; the paused events "
                      "were dark 105s on average.", text)
        self.assertEqual(em.subject_for(ctx), "Tape A/B 11/02: +$0.37 yesterday, +$0.37 "
                                              "so far (day 1/21) | RUNNING")
        text.encode("ascii")                 # the task console is cp1252
        self.assertIn("<table", em.render_html(ctx))

    def test_it_sends_once_through_the_alerter(self):
        self.assertEqual(em.main(["--until", "2026-11-02"]), 0)
        (subject, _text, html), = _FakeAlerter.sent
        self.assertTrue(subject.startswith("Tape A/B 11/02: +$0.37 yesterday"), subject)
        self.assertIn("<table", html)

    def test_dry_sends_nothing(self):
        self.assertEqual(em.main(["--dry", "--until", "2026-11-02"]), 0)
        self.assertEqual(_FakeAlerter.sent, [])

    def test_a_day_without_windows_still_reports_the_running_total(self):
        self.assertEqual(em.main(["--until", "2026-11-03"]), 0)
        (subject, text, _html), = _FakeAlerter.sent
        self.assertEqual(subject, "Tape A/B 11/03: no comparison yesterday, +$0.37 so far "
                                  "(day 2/21) | RUNNING")
        self.assertIn("YESTERDAY: no A/B windows", text)

    def test_quiet_once_a_week_passes_without_a_window(self):
        self.assertEqual(em.main(["--until", "2026-11-08"]), 0)    # 6 days on: sends
        self.assertEqual(len(_FakeAlerter.sent), 1)
        self.assertEqual(em.main(["--until", "2026-11-09"]), 0)    # 7 days on: quiet
        self.assertEqual(len(_FakeAlerter.sent), 1)
        self.assertTrue(any("nothing sent" in s for s in self.logged))


if __name__ == "__main__":
    unittest.main()
