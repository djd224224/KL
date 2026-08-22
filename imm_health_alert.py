#!/usr/bin/env python3
r"""
imm_health_alert.py — READ-ONLY liveness alerter for incentive_mm.

Emails on every health TRANSITION (down -> up, up -> down) and stays silent
otherwise. Writes no bot state and never restarts anything: recovery is
run_watchdog_hidden.vbs's job (every 15 min, Ready-only). This exists so an
outage is NOTICED, not merely repaired — the two are different guarantees and
today proved you need both.

WHY IT EXISTS. On 2026-08-05 the bot's python process died at 13:00:37, the
launcher exited with it, the scheduled task fell back to Ready, and nothing
was watching: 23 minutes of ~670 orders resting unmanaged, discovered only
because a human noticed markets missing. The crypto fleet had watchdog cover;
the incentive bot never did.

THREE FAILURE MODES, all of which were true at once that day. Any single check
would have slept through at least one of them:
  * process gone
  * scheduled task back to Ready (launcher exited, nothing self-heals)
  * heartbeat stale while the process is alive (a wedged syscall — the bot is
    single-threaded, so one hung call freezes everything silently)

State is kept in a tiny JSON file next to the bot's status so repeated runs
under Task Scheduler do not re-send. SMS is dead (tmomail gateway bounces
since 2026-06-29) — email only.

Usage:
    python imm_health_alert.py            # check once, email on transition
    python imm_health_alert.py --status   # print state, send nothing
    python imm_health_alert.py --test     # force a test email
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from incentive_mm import STATUS_DIR, Alerter, log  # noqa: E402

STATUS_PATH = os.path.join(STATUS_DIR, "status_incentive_mm.json")
STATE_PATH = os.path.join(STATUS_DIR, "imm_health_alert_state.json")
TASK = os.environ.get("IMM_HEALTH_TASK", "KL incentive_mm")
# Cycles run about once a minute; 6 minutes is ~6 missed cycles, comfortably
# past normal jitter (a restart's own startup takes ~60s) but well inside the
# 23 minutes that went unnoticed.
STALE_MIN = float(os.environ.get("IMM_HEALTH_STALE_MIN", "6"))
# Three reads 1s apart span ~2s. The bot's write window is milliseconds, so a
# collision cannot survive all three; a genuinely dead/corrupt file fails all
# three and still alerts, only ~2s later.
READ_TRIES = int(os.environ.get("IMM_HEALTH_READ_TRIES", "3"))
READ_RETRY_SECS = float(os.environ.get("IMM_HEALTH_READ_RETRY_SECS", "1.0"))


def _ps(cmd: str) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=45)
        return r.stdout.strip()
    except Exception:
        return ""


def probe():
    """(ok, headline, detail) — read-only."""
    pid = _ps("(Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
              "Where-Object { $_.CommandLine -like '*incentive_mm.py*' } | "
              "Select-Object -First 1).ProcessId")
    state = _ps(f"(Get-ScheduledTask -TaskName '{TASK}').State")
    # RETRY, don't cry wolf. The bot rewrites this file every ~11s via
    # os.replace(), and on Windows a reader landing inside that swap gets
    # PermissionError. Treating one failed read as death emailed a false
    # "IMM DOWN" at 2026-08-05 23:38:48Z — the same second the file's own
    # updated_at was stamped. A dead bot stays unreadable across retries; a
    # write collision does not, so re-read before concluding anything.
    err = None
    for attempt in range(READ_TRIES):
        try:
            with open(STATUS_PATH, encoding="utf-8") as f:
                st = json.load(f)
            hb = datetime.strptime(st.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb).total_seconds() / 60.0
            break
        except Exception as e:
            err = e
            if attempt < READ_TRIES - 1:
                time.sleep(READ_RETRY_SECS)
    else:
        return (False, "heartbeat unreadable",
                f"{type(err).__name__} reading {STATUS_PATH} on "
                f"{READ_TRIES} attempts; task={state or '?'} "
                f"pid={pid or 'NONE'}")

    base = f"pid={pid or 'NONE'} task={state or '?'} heartbeat={age:.1f}m"
    if not pid:
        return False, "PROCESS GONE", base
    if state != "Running":
        return False, f"task {state}", base + " (launcher exited)"
    if age > STALE_MIN:
        return False, f"HEARTBEAT STALE {age:.0f}m", base + " (wedged?)"
    return True, "alive", base


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d: dict) -> None:
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"! health-alert state write failed: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="print, send nothing")
    ap.add_argument("--test", action="store_true", help="force a test email")
    args = ap.parse_args(argv)

    ok, headline, detail = probe()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"[IMM-HEALTH] {'OK' if ok else 'DOWN'} — {headline} — {detail}")
    if args.status:
        return 0

    alerter = Alerter("IMM-HEALTH", live=True)
    if args.test:
        alerter.send_message(f"IMM health test at {now}\n{headline} — {detail}",
                             subject="IMM health: test")
        return 0
    if not alerter.enabled:
        log("! alert credentials not configured — cannot email")
        return 1

    prev = load_state()
    was_ok = prev.get("ok")
    if was_ok is not None and bool(was_ok) == ok:
        return 0                      # no transition, stay quiet

    if not ok:
        body = (f"incentive_mm looks DOWN as of {now}.\n\n"
                f"  {headline}\n  {detail}\n\n"
                f"Automatic recovery: run_watchdog_hidden.vbs runs every 15 "
                f"minutes and restarts 'KL incentive_mm' when it sits in "
                f"Ready, so this should self-heal within ~15 min. If it does "
                f"not, the launcher itself is wedged and needs a manual "
                f"Start-ScheduledTask.\n\n"
                f"This alerter does not restart anything.")
        subj = f"IMM DOWN: {headline}"
    else:
        down_since = prev.get("since", "?")
        body = (f"incentive_mm is back up as of {now}.\n\n  {detail}\n\n"
                f"Was down since {down_since}.")
        subj = "IMM recovered"
    alerter.send_message(body, subject=subj)
    save_state({"ok": ok, "since": now, "headline": headline})
    return 0


if __name__ == "__main__":
    sys.exit(main())
