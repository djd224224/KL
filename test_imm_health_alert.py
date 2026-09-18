#!/usr/bin/env python3
"""Tests for imm_health_alert.probe() read-retry behaviour.

WHY THIS FILE EXISTS. On 2026-08-05 23:38:48Z the liveness watcher emailed
"IMM DOWN - heartbeat unreadable (PermissionError)" while the bot was fully
healthy: 331 cycles, errors_today 0, heartbeat 0.3m old. The probe's read had
landed inside the bot's own os.replace() of status_incentive_mm.json (the
file's updated_at was that same second). A monitor that cries wolf is worse
than no monitor, so a transient read failure must not read as death -- while a
genuinely unreadable file still must.
"""

import builtins
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_health_alert as ha  # noqa: E402


def _status(age_min: float = 0.1) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return json.dumps({"updated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})


class ProbeReadRetry(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(_status())
        p = mock.patch.object(ha, "STATUS_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        # healthy process + task, so the heartbeat read is the only variable
        ps = mock.patch.object(ha, "_ps",
                               side_effect=lambda c: "10972" if "Win32_Process" in c
                               else "Running")
        ps.start()
        self.addCleanup(ps.stop)
        # keep the suite fast; the real default is 1.0s
        r = mock.patch.object(ha, "READ_RETRY_SECS", 0.0)
        r.start()
        self.addCleanup(r.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _open_failing(self, n_failures, exc=PermissionError):
        """Real open(), but the first n_failures calls on STATUS_PATH raise --
        exactly what an os.replace() collision looks like to a reader."""
        real = builtins.open
        calls = {"n": 0}

        def fake(path, *a, **kw):
            if str(path) == self.path:
                calls["n"] += 1
                if calls["n"] <= n_failures:
                    raise exc(13, "being replaced")
            return real(path, *a, **kw)
        return mock.patch.object(builtins, "open", fake), calls

    def test_healthy_read_is_alive(self):
        ok, headline, _ = ha.probe()
        self.assertTrue(ok)
        self.assertEqual(headline, "alive")

    def test_single_collision_does_not_alert(self):
        """The 23:38:48Z false alarm: one PermissionError, bot perfectly fine."""
        patch, calls = self._open_failing(1)
        with patch:
            ok, headline, _ = ha.probe()
        self.assertTrue(ok, f"one transient read failure reported DOWN: {headline}")
        self.assertEqual(headline, "alive")
        self.assertEqual(calls["n"], 2, "should have retried exactly once")

    def test_collisions_up_to_the_retry_budget_do_not_alert(self):
        patch, _ = self._open_failing(ha.READ_TRIES - 1)
        with patch:
            ok, headline, _ = ha.probe()
        self.assertTrue(ok, f"reported DOWN inside the retry budget: {headline}")

    def test_persistent_failure_still_alerts(self):
        """A retry must not blind the watcher to a genuinely unreadable file."""
        patch, calls = self._open_failing(ha.READ_TRIES)
        with patch:
            ok, headline, detail = ha.probe()
        self.assertFalse(ok)
        self.assertEqual(headline, "heartbeat unreadable")
        self.assertIn(f"{ha.READ_TRIES} attempts", detail)
        self.assertEqual(calls["n"], ha.READ_TRIES)

    def test_missing_file_still_alerts(self):
        os.remove(self.path)
        ok, headline, _ = ha.probe()
        self.assertFalse(ok)
        self.assertEqual(headline, "heartbeat unreadable")

    def test_corrupt_json_still_alerts(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        ok, headline, _ = ha.probe()
        self.assertFalse(ok)
        self.assertEqual(headline, "heartbeat unreadable")

    def test_retry_does_not_mask_a_stale_heartbeat(self):
        """Readable but old is a DIFFERENT failure and must survive the retry."""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(_status(age_min=ha.STALE_MIN + 5))
        ok, headline, _ = ha.probe()
        self.assertFalse(ok)
        self.assertIn("HEARTBEAT STALE", headline)

    def test_retry_does_not_mask_a_dead_process(self):
        """Fresh heartbeat but no pid = the 2026-08-05 18:49Z crash shape."""
        with mock.patch.object(ha, "_ps",
                               side_effect=lambda c: "" if "Win32_Process" in c
                               else "Ready"):
            ok, headline, _ = ha.probe()
        self.assertFalse(ok)
        self.assertEqual(headline, "PROCESS GONE")


class _Alerter:
    """Records instead of sending. The suite must never SMTP the user."""

    def __init__(self):
        self.sent = []

    def send_message(self, body, subject=None, html=None):
        self.sent.append((subject, body))
        return True


class ScanPerfStaleness(unittest.TestCase):
    """The open-scan performance table gets the same treatment as the reward
    ledger, and for the same reason: the bot fails OPEN on scan_perf.json, so
    a scorer that quietly stops running breaks nothing and announces nothing
    — the tier just drifts back to ranking on gross ROI while the daily email
    keeps printing verdicts that are no longer applied. A failure that is
    invisible by construction is exactly what needs a monitor."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perf_health_")
        self.path = os.path.join(self.tmp, "scan_perf.json")
        p = mock.patch.object(ha, "SCAN_PERF_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, age_h):
        ts = datetime.now(timezone.utc) - timedelta(hours=age_h)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 1,
                       "generated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}, f)

    def test_a_three_day_old_table_alerts_once_a_day(self):
        self._write(72)
        a, state = _Alerter(), {}
        ha.check_scan_perf_staleness(a, state, "now")
        self.assertEqual(len(a.sent), 1, a.sent)
        self.assertIn("scan-perf", a.sent[0][0])
        self.assertIn("72", a.sent[0][0])
        self.assertIn(self.path, a.sent[0][1])
        # a second run the same day stays quiet
        ha.check_scan_perf_staleness(a, state, "now")
        self.assertEqual(len(a.sent), 1)

    def test_a_fresh_table_is_silent_and_clears_the_marker(self):
        self._write(4)
        a, state = _Alerter(), {"scan_perf_alert_date": "2026-01-01"}
        ha.check_scan_perf_staleness(a, state, "now")
        self.assertEqual(a.sent, [])
        self.assertNotIn("scan_perf_alert_date", state)

    def test_just_inside_the_threshold_is_silent(self):
        self._write(ha.SCAN_PERF_STALE_HOURS - 1)
        a = _Alerter()
        ha.check_scan_perf_staleness(a, {}, "now")
        self.assertEqual(a.sent, [])

    def test_a_missing_table_is_not_an_alert(self):
        """The loop may simply not be deployed. A monitor that pages about a
        feature nobody turned on is a monitor that gets ignored."""
        a = _Alerter()
        ha.check_scan_perf_staleness(a, {}, "now")
        self.assertEqual(a.sent, [])

    def test_a_garbled_table_never_raises(self):
        for junk in ("{not json", '{"version": 1}', '[]'):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(junk)
            a = _Alerter()
            ha.check_scan_perf_staleness(a, {}, "now")
            self.assertEqual(a.sent, [], junk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
