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
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_health_alert as ha  # noqa: E402


def _status(age_min: float = 0.1) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return json.dumps({"updated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})


class _ProbeFixture(unittest.TestCase):
    """A healthy status file, process and task; no tests of its own."""

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
        # the planned-restart files live next to the LIVE status: point them
        # at paths that do not exist, or a real restart on the trading box
        # would excuse the DOWN cases below
        d = tempfile.mkdtemp(prefix="imm_health_")
        for name, fn in (("RESTART_REQUEST_PATH", "restart_handoff_request.json"),
                         ("RESTART_HANDOFF_PATH", "restart_handoff.json")):
            pr = mock.patch.object(ha, name, os.path.join(d, fn))
            pr.start()
            self.addCleanup(pr.stop)


class ProbeReadRetry(_ProbeFixture):
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


class ProbeRestartWindow(_ProbeFixture):
    """2026-10-01: restart_imm.ps1 -Task now stops the launcher (task Ready)
    and waits up to a cycle for the bot to hand its book over; the relaunch
    then needs ~1-2 min. A fresh request or handoff file explains that window
    -- a planned restart must not page DOWN then UP -- but only for a bounded
    time, and never a stale heartbeat on a live process."""

    def _touch(self, attr, age_secs):
        path = getattr(ha, attr)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        t = time.time() - age_secs
        os.utime(path, (t, t))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

    def _ps_as(self, pid, state):
        return mock.patch.object(ha, "_ps", side_effect=lambda c: pid
                                 if "Win32_Process" in c else state)

    def test_handoff_wait_reads_as_restarting(self):
        self._touch("RESTART_REQUEST_PATH", 30)
        with self._ps_as("10972", "Ready"):
            ok, headline, detail = ha.probe()
        self.assertTrue(ok)
        self.assertEqual(headline, "restarting")
        self.assertIn("planned restart", detail)

    def test_relaunch_gap_reads_as_restarting(self):
        self._touch("RESTART_HANDOFF_PATH", 60)
        with self._ps_as("", "Running"):
            ok, headline, _ = ha.probe()
        self.assertTrue(ok)
        self.assertEqual(headline, "restarting")

    def test_a_restart_that_never_finished_alerts(self):
        self._touch("RESTART_REQUEST_PATH", ha.RESTARTING_MAX_SECS + 60)
        self._touch("RESTART_HANDOFF_PATH", ha.RESTART_HANDOFF_MAX_AGE_SECS + 60)
        with self._ps_as("", "Ready"):
            ok, headline, _ = ha.probe()
        self.assertFalse(ok)
        self.assertEqual(headline, "PROCESS GONE")

    def test_a_stale_heartbeat_is_never_excused(self):
        self._touch("RESTART_REQUEST_PATH", 30)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(_status(age_min=ha.STALE_MIN + 5))
        ok, headline, _ = ha.probe()          # pid alive, task Running
        self.assertFalse(ok)
        self.assertIn("HEARTBEAT STALE", headline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
