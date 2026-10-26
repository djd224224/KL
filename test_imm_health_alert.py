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

class TestHeartbeat(unittest.TestCase):
    """Jack 2026-10-12: an outside heartbeat (healthchecks.io), so a restart
    that leaves the box at the sign-in screen is noticed from outside. No test
    here may reach the network or read the real IMM_HEARTBEAT_URL."""

    URL = "https://hc-ping.example/test-uuid"

    def setUp(self):
        import io
        self.calls = []

        def fake_urlopen(req, timeout=None):
            self.calls.append((req.full_url, req.data, req.get_method(), timeout))
            if getattr(self, "net_down", False):
                raise OSError("network down")
            return io.BytesIO(b"OK")
        p1 = mock.patch.object(ha.urllib.request, "urlopen", fake_urlopen)
        p2 = mock.patch.object(ha, "_env_from_registry", lambda name: "")
        p3 = mock.patch.dict(os.environ, {ha.HEARTBEAT_ENV: self.URL})
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def test_no_url_no_ping(self):
        with mock.patch.dict(os.environ, {ha.HEARTBEAT_ENV: ""}):
            self.assertFalse(ha.heartbeat("x"))
        self.assertEqual(self.calls, [])

    def test_pings_the_url_with_the_status(self):
        self.assertTrue(ha.heartbeat("IMM OK -- alive"))
        self.assertEqual(self.calls, [(self.URL, b"IMM OK -- alive", "POST",
                                       ha.HEARTBEAT_TIMEOUT_SECS)])

    def test_a_failed_ping_is_logged_not_raised(self):
        self.net_down = True
        self.assertFalse(ha.heartbeat("x"))
        self.assertEqual(len(self.calls), 1)

    def _fake_alerter(self):
        sent = []

        class FakeAlerter:
            enabled = False

            def __init__(self, tag, live):
                pass

            def send_message(self, body, subject=None):
                sent.append(subject)
                return True
        return FakeAlerter, sent

    def test_main_pings_on_scheduled_runs_only(self):
        fa, sent = self._fake_alerter()
        with mock.patch.object(ha, "probe", lambda: (True, "alive", "pid 1")),                 mock.patch.object(ha, "Alerter", fa):
            ha.main([])                       # credentials-missing exit, after the ping
            self.assertEqual(len(self.calls), 1)
            self.assertIn(b"IMM OK", self.calls[0][1])
            ha.main(["--status"])
            ha.main(["--test"])
        self.assertEqual(len(self.calls), 1)  # neither manual mode pinged
        self.assertEqual(sent, ["IMM health: test"])

    def test_main_still_pings_when_the_probe_raises(self):
        fa, _ = self._fake_alerter()

        def boom():
            raise RuntimeError("probe bug")
        with mock.patch.object(ha, "probe", boom), mock.patch.object(ha, "Alerter", fa):
            with self.assertRaises(RuntimeError):
                ha.main([])
        self.assertEqual(len(self.calls), 1)
        self.assertIn(b"probe failed", self.calls[0][1])


class TestShardCash(unittest.TestCase):
    """Jack 2026-10-15: "email alert me if a shard ever runs out of cash".
    The MLB MVPs sat on an unfunded shard 3 for two days with every order
    refused, so both a low balance and refused orders must alert. No test may
    reach Kalshi: every read goes through the fake `get` below."""

    REFUSED = ('{"error":{"code":"insufficient_shard_balance","message":"insufficient '
               'shard balance","details":"Exchange user not found."}}')
    CROSS = '{"error":{"code":"invalid_order","details":"post only cross"}}'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_shard_cash_")
        self.cash = {0: 28000.0, 1: 0.0, 2: 235.0, 3: 0.0}
        self.events = {"KXMLBWSMVP-26": 3, "KXMLBALCSMVP-26": 3, "KXGOOD-99DEC31": 0}
        self.markets = {}                # market ticker -> shard (the fallback read)
        self.reads, self.sent, self.state = [], [], {}
        self.down = False
        self.t = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        sent = self.sent

        class FakeAlerter:
            def send_message(self, body, subject=""):
                sent.append((subject, body))
                return True
        self.alerter = FakeAlerter()

    def get(self, path, params=None):
        self.reads.append(path)
        if self.down:
            raise RuntimeError("kalshi unreachable")
        if path == "/portfolio/balance":
            return {"balance_breakdown": [{"exchange_index": n, "balance": f"{v:.4f}"}
                                          for n, v in self.cash.items()]}
        if path.startswith("/events/"):
            return {"event": {"exchange_index": self.events[path.split("/")[2]]}}
        if path.startswith("/markets/"):
            return {"market": {"exchange_index": self.markets[path.split("/")[2]]}}
        raise AssertionError(f"unexpected read {path}")

    def run_check(self, hours=0.0):
        self.t += hours * 3600
        now = datetime.fromtimestamp(self.t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ha.check_shard_cash(self.alerter, self.state, now, get=self.get,
                                   status_dir=self.dir, now_ts=self.t)

    def log_path(self, day="2026-10-15"):
        return os.path.join(self.dir, f"orders_{day}.jsonl")

    def write(self, rows, day="2026-10-15", tail=""):
        with open(self.log_path(day), "a", encoding="utf-8", newline="") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            f.write(tail)

    def reject(self, ticker, series, body=None):
        return {"kind": "reject", "ticker": ticker, "series": series,
                "event_ticker": ticker.rsplit("-", 1)[0], "book_side": "bid",
                "error_body": body or self.REFUSED, "http_status": 404}

    def mvp(self, n, series="KXMLBWSMVP"):
        return [self.reject(f"{series}-26-P{i}", series) for i in range(n)]

    def test_first_run_starts_at_the_end_and_unfunded_shards_stay_quiet(self):
        self.write(self.mvp(5))                       # history, before the first run
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state["shard_cash"]["log"],
                         ["orders_2026-10-15.jsonl", os.path.getsize(self.log_path())])
        self.assertEqual(self.state["shard_cash"]["funded"], [0, 2])   # shards 1, 3: $0

    def test_refusals_on_an_unfunded_shard_alert_then_throttle(self):
        self.run_check()
        self.write(self.mvp(4) + self.mvp(1, "KXMLBALCSMVP")
                   + [self.reject("KXGOOD-99DEC31-A", "KXGOOD", self.CROSS)]
                   + [{"kind": "place", "ticker": "KXGOOD-99DEC31-A"}] * 2)
        self.run_check(hours=5 / 60)
        self.assertEqual(len(self.sent), 1)
        subj, body = self.sent[0]
        self.assertEqual(subj, "Kalshi shard 3 out of cash: $0.00")
        self.assertIn("since the last check: 5 (KXMLBWSMVP 4, KXMLBALCSMVP 1)", body)
        self.assertIn("'amount': 5000000, 'source_exchange_shard': 0, "
                      "'destination_exchange_shard': 3", body)
        self.assertEqual(self.reads.count("/events/KXMLBWSMVP-26"), 1)   # cached
        self.write(self.mvp(5))
        self.run_check(hours=1)
        self.assertEqual(len(self.sent), 1)           # inside the 6h window
        self.write(self.mvp(5))
        self.run_check(hours=ha.SHARD_REALERT_HOURS)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.reads.count("/events/KXMLBWSMVP-26"), 1)

    def test_a_four_part_ticker_falls_back_to_the_market_read(self):
        """2026-10-26: the orders log's event_ticker for a four-part ticker
        is the bot's own grouping, not Kalshi's event, so the event read
        404'd and the email said "an unknown shard". The market's own record
        names the shard."""
        t = "KXMLBSERIESHR-26TBMILWS-TBBWILLIAMSON15-1"
        self.cash[3] = 348.96
        self.markets[t] = 3
        self.run_check()
        self.write([self.reject(t, "KXMLBSERIESHR",
                                '{"error":{"code":"insufficient_balance",'
                                '"message":"insufficient balance"}}')] * 12)
        self.run_check(hours=5 / 60)
        self.assertEqual(len(self.sent), 1)
        subj, _body = self.sent[0]
        self.assertIn("shard 3", subj)
        self.assertNotIn("unknown", subj)
        self.assertEqual(self.reads.count(f"/markets/{t}"), 1)          # cached

    def test_funding_the_shard_sends_the_all_clear(self):
        self.run_check()
        self.write(self.mvp(3))
        self.run_check(hours=5 / 60)
        self.cash[3] = 1000.0
        self.run_check(hours=5 / 60)
        self.assertEqual([s for s, _ in self.sent],
                         ["Kalshi shard 3 out of cash: $0.00",
                          "Kalshi shard 3 cash OK again: $1,000.00"])
        self.assertEqual(self.state["shard_cash"]["alerts"], {})
        self.assertIn(3, self.state["shard_cash"]["funded"])

    def test_low_cash_on_a_funded_shard_alerts_with_hysteresis(self):
        self.run_check()                              # shard 2 funded at $235
        self.cash[2] = 50.0
        self.run_check(hours=5 / 60)
        self.assertEqual(self.sent[-1][0], "Kalshi shard 2 low on cash: $50.00 (floor $100)")
        self.cash[2] = 110.0                          # over the floor, under 1.25x
        self.run_check(hours=2)
        self.assertEqual(len(self.sent), 1)
        self.cash[2] = 130.0
        self.run_check(hours=5 / 60)
        self.assertEqual(self.sent[-1][0], "Kalshi shard 2 cash OK again: $130.00")
        self.cash[2] = 0.0                            # once funded, empty = out
        self.run_check(hours=5 / 60)
        self.assertEqual(self.sent[-1][0], "Kalshi shard 2 out of cash: $0.00")

    def test_shard_zero_floor_is_1000_and_names_a_donor(self):
        self.cash[3] = 1000.0
        self.run_check()
        self.cash[0] = 900.0
        self.run_check(hours=5 / 60)
        subj, body = self.sent[-1]
        self.assertEqual(subj, "Kalshi shard 0 low on cash: $900.00 (floor $1,000)")
        self.assertIn("deposit to the account", body)
        self.assertIn("'source_exchange_shard': 3, 'destination_exchange_shard': 0", body)
        self.cash[0] = 1300.0                         # >= 1.25 x $1,000
        self.run_check(hours=5 / 60)
        self.assertEqual(self.sent[-1][0], "Kalshi shard 0 cash OK again: $1,300.00")

    def test_post_only_crosses_and_too_few_refusals_stay_quiet(self):
        self.run_check()
        self.write(self.mvp(ha.SHARD_REFUSAL_MIN - 1)
                   + [self.reject("KXGOOD-99DEC31-A", "KXGOOD", self.CROSS)] * 10)
        self.run_check(hours=5 / 60)
        self.assertEqual(self.sent, [])

    def test_an_unreadable_balance_is_swallowed_and_loses_nothing(self):
        self.run_check()
        self.write(self.mvp(5))
        self.down = True
        self.assertEqual(self.run_check(hours=5 / 60), [])    # no raise, no email
        self.assertEqual(self.sent, [])
        self.down = False
        self.run_check(hours=5 / 60)                  # the refusals are still there
        self.assertEqual([s for s, _ in self.sent], ["Kalshi shard 3 out of cash: $0.00"])

    def test_the_day_roll_finishes_yesterdays_log(self):
        self.t = datetime(2026, 10, 14, 23, 58, tzinfo=timezone.utc).timestamp()
        self.write(self.mvp(2), day="2026-10-14")
        self.run_check()                              # starts at the end of 10/14
        self.write(self.mvp(2), day="2026-10-14")
        self.write(self.mvp(2))                       # 10/15
        self.run_check(hours=5 / 60)
        self.assertIn("since the last check: 4 (KXMLBWSMVP 4)", self.sent[-1][1])
        self.assertEqual(self.state["shard_cash"]["log"],
                         ["orders_2026-10-15.jsonl", os.path.getsize(self.log_path())])

    def test_a_half_written_line_waits_for_its_newline(self):
        self.run_check()
        partial = json.dumps(self.reject("KXMLBWSMVP-26-PX", "KXMLBWSMVP"))
        self.write(self.mvp(3), tail=partial)
        self.run_check(hours=5 / 60)
        self.assertIn("since the last check: 3 (", self.sent[-1][1])
        self.assertEqual(self.state["shard_cash"]["log"][1],
                         os.path.getsize(self.log_path()) - len(partial))

    def test_transfer_command_is_the_one_jack_ran(self):
        cmd = ha.transfer_command(0, 3, 1000)
        self.assertTrue(cmd.startswith('python -c "import sys, json; sys.path.insert(0, r\''))
        self.assertIn("import kalshi_subaccount as ks; print(ks.client().post(path="
                      "'/portfolio/intra_exchange_instance_transfer', body=json.dumps("
                      "{'source': 'event_contract', 'destination': 'event_contract', "
                      "'amount': 10000000, 'source_exchange_shard': 0, "
                      "'destination_exchange_shard': 3})))\"", cmd)

    def test_main_runs_it_on_scheduled_runs_only(self):
        calls = []
        d = tempfile.mkdtemp(prefix="imm_health_main_")

        class FakeAlerter:
            enabled = True

            def __init__(self, tag, live):
                pass

            def send_message(self, body, subject=None):
                return True
        with mock.patch.object(ha, "probe", lambda: (True, "alive", "pid 1")), \
                mock.patch.object(ha, "Alerter", FakeAlerter), \
                mock.patch.object(ha, "heartbeat", lambda body="": True), \
                mock.patch.object(ha, "check_credit_staleness", lambda *a: None), \
                mock.patch.object(ha, "STATE_PATH", os.path.join(d, "state.json")), \
                mock.patch.object(ha, "check_shard_cash",
                                  lambda alerter, state, now, **kw: calls.append(now)):
            ha.main([])
            ha.main(["--status"])
            ha.main(["--test"])
        self.assertEqual(len(calls), 1)

    def test_the_shards_flag_is_a_dry_run(self):
        import io
        from contextlib import redirect_stdout
        self.state = {"shard_cash": {"log": ["orders_2026-10-15.jsonl", 0], "funded": [0, 2]}}
        self.write(self.mvp(5))
        state_path = os.path.join(self.dir, "state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f)

        def saved():
            with open(state_path, encoding="utf-8") as f:
                return f.read()
        before = saved()
        buf = io.StringIO()
        with mock.patch.object(ha, "_kalshi_get", self.get), \
                mock.patch.object(ha, "STATE_PATH", state_path), \
                mock.patch.object(ha, "STATUS_DIR", self.dir), \
                mock.patch.object(ha, "Alerter", side_effect=AssertionError("no alerter")), \
                redirect_stdout(buf):
            self.assertEqual(ha.main(["--shards"]), 0)
        out = buf.getvalue()
        self.assertIn("shard cash: 0 = $28,000.00 | 1 = $0.00 | 2 = $235.00 | 3 = $0.00", out)
        self.assertIn("WOULD SEND: Kalshi shard 3 out of cash: $0.00", out)
        self.assertEqual(saved(), before)             # saved nothing


if __name__ == "__main__":
    unittest.main(verbosity=2)
