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

OUTSIDE HEARTBEAT (Jack 2026-10-12, healthchecks.io). Everything above runs
ON the trading box, so it goes quiet with it: after a restart this
Entra-joined PC waits at the sign-in screen (no auto sign-in), and nothing,
this alerter included, runs until someone signs in, while the bot's resting
orders sit unmanaged. So every scheduled run first pings the URL in
IMM_HEARTBEAT_URL (user env / HKCU\Environment, like the ALERT_EMAIL_*
pair), with the bot's state as the body. A restart, crash, sleep, dead
battery or lost internet stops the pings, and healthchecks.io says so from
outside. The ping goes out even when probe() raises, so a bug here cannot
pose as the box going dark. --status / --test do not ping.

SHARD CASH (Jack 2026-10-15): every scheduled run also checks each Kalshi
exchange shard's cash and emails when one runs out; see check_shard_cash.

Usage:
    python imm_health_alert.py            # check once, email on transition
    python imm_health_alert.py --status   # print state, send nothing
    python imm_health_alert.py --test     # force a test email
    python imm_health_alert.py --shards   # dry-run the shard-cash check
"""

import argparse
import copy
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):     # ImportError: non-Windows (tests)
        return ""


for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from incentive_mm import (STATUS_DIR, Alerter, log,  # noqa: E402
                          RESTART_HANDOFF_FILE, RESTART_HANDOFF_MAX_AGE_SECS,
                          RESTART_REQUEST_FILE)

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
# A DELIBERATE RESTART IS NOT AN OUTAGE (2026-10-01). restart_imm.ps1 -Task
# now stops the launcher first (task Ready) and waits up to a cycle for the
# bot to hand its book over and exit; the relaunch then takes ~1-2 min to
# bring python back. A planned code-change exit leaves python down ~30-90s
# the same way. While a fresh request or handoff file says a restart is
# under way, "task not Running" and "process gone" read as "restarting"
# instead of paging DOWN and then UP. Bounded: past RESTARTING_MAX_SECS on the
# request (the script's own 600s wait plus slack) or
# RESTART_HANDOFF_MAX_AGE_SECS on the handoff, a restart that never completed
# alerts as usual. A stale heartbeat on a live process is never excused.
RESTART_REQUEST_PATH = os.path.join(STATUS_DIR, RESTART_REQUEST_FILE)
RESTART_HANDOFF_PATH = os.path.join(STATUS_DIR, RESTART_HANDOFF_FILE)
RESTARTING_MAX_SECS = float(os.environ.get("IMM_HEALTH_RESTARTING_MAX_SECS", "660"))


def restart_in_progress(now_ts=None) -> bool:
    """A planned restart's request or handoff file, fresh enough to explain
    a missing process or a task that is not Running."""
    now_ts = time.time() if now_ts is None else now_ts
    for path, limit in ((RESTART_REQUEST_PATH, RESTARTING_MAX_SECS),
                        (RESTART_HANDOFF_PATH, RESTART_HANDOFF_MAX_AGE_SECS)):
        try:
            if 0 <= now_ts - os.path.getmtime(path) <= limit:
                return True
        except OSError:
            pass
    return False


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
    if (not pid or state != "Running") and restart_in_progress():
        return True, "restarting", base + " (planned restart under way)"
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


CREDITS_PATH = os.path.join(STATUS_DIR, "reward_credits.csv")
CREDIT_STALE_DAYS = 3


def check_credit_staleness(alerter, state: dict, now: str) -> None:
    """Alert when the reward-credit ledger stops being updated.

    reward_credits.csv is the only record of money actually PAID — the whole
    outcome side of a rent-collection strategy, ~$24k of realized value — and
    Kalshi exposes no credits endpoint (8 paths checked 2026-08-03). It grows
    only when a human opens the rewards UI and pastes the statement, and
    nothing has ever detected when that stops: the digest and the calibration
    quietly reconcile against an older ledger and the realization factor
    drifts, with no visible failure. The human IS the archive job here, so
    the archive job needs a monitor.

    At most one email per day, and never raises — a health alerter must not
    die on a side check."""
    try:
        if not os.path.exists(CREDITS_PATH):
            return
        latest = ""
        with open(CREDITS_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("credit_date") or "").strip()
                if d > latest:
                    latest = d
        if not latest:
            return
        today = datetime.now(timezone.utc).date()
        behind = (today - datetime.strptime(latest, "%Y-%m-%d").date()).days
        if behind <= CREDIT_STALE_DAYS:
            if state.pop("credit_alert_date", None):
                log(f"credit ledger current again (latest {latest})")
            return
        if state.get("credit_alert_date") == today.isoformat():
            return                       # already warned today
        state["credit_alert_date"] = today.isoformat()
        log(f"! credit ledger {behind}d stale (latest {latest})")
        alerter.send_message(
            f"The IMM reward-credit ledger is {behind} days stale as of {now}.\n\n"
            f"  Latest credit in reward_credits.csv: {latest}\n"
            f"  File: {CREDITS_PATH}\n\n"
            f"Kalshi has no credits API, so this ledger only grows when the "
            f"rewards statement is pasted in by hand. While it is stale the "
            f"digest and the reward calibration keep reconciling against old "
            f"data and the realization factor drifts silently — nothing else "
            f"fails visibly.\n\n"
            f"To refresh: open the Kalshi rewards page, select all, paste into "
            f"a text file, then run\n\n"
            f"  python imm_reward_recon.py --statement \"<that file>\"\n",
            subject=f"IMM reward ledger {behind}d stale")
    except Exception as e:
        log(f"! credit staleness check failed ({e}); continuing")


# SHARD CASH (Jack 2026-10-15: "email alert me if a shard ever runs out of
# cash"). Kalshi splits the account's cash by exchange shard (0 default,
# 1 combos, 2 crypto/commodities, 3 baseball/basketball/tennis). Cash on one
# shard cannot back an order on another, and resting orders reserve nothing,
# so a dry shard shows up only as bots quietly failing: every MLB MVP order
# was refused on the unfunded shard 3 for two days (10/13-10/15), and the
# crypto fleets sat out the end of September on a dry shard 2. Two triggers,
# per shard:
#   * LOW: a shard that has ever held cash is under its floor
#     (IMM_SHARD_CASH_FLOORS "shard:dollars,...", default shard 0 $1,000;
#     any other shard IMM_SHARD_CASH_FLOOR_OTHER, $100);
#   * REFUSED: at least IMM_SHARD_REFUSAL_MIN orders in the IMM orders log
#     refused for cash since the last run. This catches a shard that was
#     never funded, which LOW cannot.
# One email per shard per IMM_SHARD_REALERT_HOURS while it lasts, then an
# all-clear once the shard is back over SHARD_RECOVER_MULT x its floor.
# Read-only: it never moves money; the transfer command in the email is
# Jack's to run. balance_breakdown is ACCOUNT-WIDE, so a subaccount's cash
# counts on its shard (the sniper's $300 sits on shard 0).
SHARD_NAMES = {0: "default: the IMM's main book, KXHIGH",
               1: "combos",
               2: "crypto + commodities: the crypto bots",
               3: "sports (baseball, basketball, tennis): the IMM's MLB MVPs"}
SHARD_FLOOR_OTHER = float(os.environ.get("IMM_SHARD_CASH_FLOOR_OTHER", "100"))
SHARD_REALERT_HOURS = float(os.environ.get("IMM_SHARD_REALERT_HOURS", "6"))
SHARD_REFUSAL_MIN = int(os.environ.get("IMM_SHARD_REFUSAL_MIN", "3"))
SHARD_RECOVER_MULT = 1.25
# The orders log runs ~235 MB a day: it is read incrementally from a saved
# offset, and at most this much per run (after a long sleep, the newest part)
SHARD_LOG_READ_MAX = 30 * 1024 * 1024
# Kalshi's wording for a refusal for cash (crypto_touch_mm's markers)
CASH_REFUSAL_MARKERS = ("insufficient", "balance_too_low", "available_balance")
SHARD_TOPUP_DOLLARS = 500.0
HERE = os.path.dirname(os.path.abspath(__file__))


def shard_floors(spec: str = None) -> dict:
    """{shard: floor $} from IMM_SHARD_CASH_FLOORS ("0:1000,3:200")."""
    if spec is None:
        spec = os.environ.get("IMM_SHARD_CASH_FLOORS", "0:1000")
    out = {}
    for part in spec.split(","):
        k, _, v = part.partition(":")
        try:
            out[int(k)] = float(v)
        except ValueError:
            continue
    return out


def _kalshi_get(path: str, params: dict = None):
    import kalshi_reads                  # lazy: tests never reach the network
    return kalshi_reads.kalshi_get(path, params)


def read_shard_cash(get=None) -> dict:
    """{exchange_index: free cash $} from /portfolio/balance's breakdown."""
    js = (get or _kalshi_get)("/portfolio/balance") or {}
    return {int(r["exchange_index"]): float(r["balance"])
            for r in js.get("balance_breakdown") or []}


def new_cash_refusals(sc: dict, status_dir: str, today: str) -> list:
    """The IMM orders log's reject rows refused for cash since the last run.
    sc["log"] = [file name, byte offset]; the first run starts at the end
    (no alerts on history). A UTC day roll finishes yesterday's file first.
    Only whole lines are consumed; a half-written last line waits."""
    name = f"orders_{today}.jsonl"
    pos = sc.get("log")
    if not pos:
        path = os.path.join(status_dir, name)
        sc["log"] = [name, os.path.getsize(path) if os.path.exists(path) else 0]
        return []
    todo = [(pos[0], int(pos[1]))]
    if pos[0] != name:
        todo.append((name, 0))
    rows = []
    for fname, off in todo:
        path = os.path.join(status_dir, fname)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        if size < off:                   # replaced or truncated
            off = 0
        skip_first = size - off > SHARD_LOG_READ_MAX
        if skip_first:
            log(f"[IMM-HEALTH] shard cash: {fname} ran {size - off} bytes ahead; "
                f"reading its last {SHARD_LOG_READ_MAX} only")
            off = size - SHARD_LOG_READ_MAX
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(size - off)
        end = chunk.rfind(b"\n") + 1
        lines = chunk[:end].split(b"\n")
        if skip_first:
            lines = lines[1:]            # the seek landed mid-line
        for raw in lines:
            if b'"reject"' not in raw:
                continue
            if not any(m.encode() in raw.lower() for m in CASH_REFUSAL_MARKERS):
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            body = str(row.get("error_body") or "").lower()
            if row.get("kind") == "reject" and any(m in body for m in CASH_REFUSAL_MARKERS):
                rows.append(row)
        sc["log"] = [fname, off + end]
    return rows


def _event_shard(sc: dict, event: str, get):
    """The event's exchange shard (cached in state), None when unreadable."""
    cache = sc.setdefault("event_shard", {})
    if event in cache:
        return cache[event]
    try:
        ev = (get(f"/events/{event}") or {}).get("event") or {}
        shard = int(ev["exchange_index"])
    except Exception as e:
        log(f"[IMM-HEALTH] ! shard lookup failed for {event}: {e!r}")
        return None
    cache[event] = shard
    for k in list(cache)[:-300]:         # bounded, oldest first
        del cache[k]
    return shard


def transfer_command(src: int, dst: int, dollars: float) -> str:
    """The one-liner Jack ran on 10/15 to move cash between shards
    (amount in centicents: $1 = 10,000)."""
    body = ("{'source': 'event_contract', 'destination': 'event_contract', "
            f"'amount': {int(round(dollars * 10000))}, "
            f"'source_exchange_shard': {src}, 'destination_exchange_shard': {dst}" + "}")
    return ("python -c \"import sys, json; sys.path.insert(0, r'" + HERE + "'); "
            "import kalshi_subaccount as ks; print(ks.client().post("
            "path='/portfolio/intra_exchange_instance_transfer', body=json.dumps("
            + body + ")))\"")


def _all_shards_line(cash: dict) -> str:
    return " | ".join(f"{n} = ${v:,.2f}" for n, v in sorted(cash.items()))


def _shard_problem_message(n, v, floor, series: dict, cash: dict, now: str,
                           rec: dict) -> tuple:
    label = f"shard {n}" if n is not None else "an unknown shard"
    name = SHARD_NAMES.get(n, "")
    nref = sum(series.values())
    if v is None:
        subj = f"Kalshi {label}: {nref} bot orders refused for cash"
    elif v < 1.0:
        subj = f"Kalshi {label} out of cash: ${v:,.2f}"
    elif v < floor:
        subj = f"Kalshi {label} low on cash: ${v:,.2f} (floor ${floor:,.0f})"
    else:
        subj = f"Kalshi {label}: {nref} bot orders refused for cash (${v:,.2f} left)"
    lines = [f"Kalshi exchange {label}" + (f" ({name})" if name else "")
             + (f" has ${v:,.2f} of free cash as of {now}. Its alert floor is ${floor:,.0f}."
                if v is not None else f": the shard could not be read, as of {now}.")]
    if nref:
        top = ", ".join(f"{s} {c}" for s, c in sorted(series.items(), key=lambda x: -x[1])[:8])
        lines.append(f"Bot orders refused for cash on it since the last check: {nref} ({top}).")
    lines.append(f"All shards: {_all_shards_line(cash)}")
    lines.append("Cash on one shard cannot back an order on another, and resting orders "
                 "reserve nothing, so the bots on this shard keep getting refused (the "
                 "crypto bots hold their quotes instead) until it is topped up.")
    if n is not None and n != 0:
        lines.append(f"To move ${SHARD_TOPUP_DOLLARS:,.0f} from shard 0 to shard {n} (you run "
                     f"this; nothing moves automatically; change the amount as needed, "
                     f"$1 = 10,000):\n\n  {transfer_command(0, n, SHARD_TOPUP_DOLLARS)}")
    elif n == 0:
        donors = [(m, w) for m, w in cash.items() if m != 0 and w >= 2 * SHARD_FLOOR_OTHER]
        if donors:
            src = max(donors, key=lambda x: x[1])[0]
            lines.append(f"Shard 0 is the main pool: deposit to the account, or move spare "
                         f"cash back from shard {src} (you run this; change the amount as "
                         f"needed, $1 = 10,000):\n\n  "
                         f"{transfer_command(src, 0, SHARD_TOPUP_DOLLARS)}")
        else:
            lines.append("Shard 0 is the main pool: deposit to the account.")
    lines.append(f"Flagged since {rec.get('since', now)}. Next reminder in "
                 f"{SHARD_REALERT_HOURS:g}h while it lasts; an all-clear follows once the "
                 f"shard is back over ${floor * SHARD_RECOVER_MULT:,.0f}.")
    return subj, "\n\n".join(lines)


def check_shard_cash(alerter, state: dict, now: str, get=None,
                     status_dir: str = None, now_ts: float = None,
                     send: bool = True) -> list:
    """Email when a Kalshi exchange shard runs out of cash (see SHARD CASH).
    Mutates state["shard_cash"]. Returns the (subject, body) pairs sent, or
    that would be sent with send=False. Never raises: a health alerter must
    not die on a side check."""
    out = []
    try:
        get = get or _kalshi_get
        status_dir = status_dir or STATUS_DIR
        now_ts = time.time() if now_ts is None else now_ts
        sc = state.setdefault("shard_cash", {})
        cash = read_shard_cash(get)
        today = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        refused = {}                     # shard (None = unknown) -> {series: n}
        for r in new_cash_refusals(sc, status_dir, today):
            ev = r.get("event_ticker") or str(r.get("ticker", "")).rsplit("-", 1)[0]
            n = _event_shard(sc, ev, get) if ev else None
            per = refused.setdefault(n, {})
            s = r.get("series") or "?"
            per[s] = per.get(s, 0) + 1
        funded = set(sc.get("funded") or []) | {n for n, v in cash.items() if v >= 1.0}
        sc["funded"] = sorted(funded)
        floors = shard_floors()
        alerts = sc.setdefault("alerts", {})
        keys = set(cash) | set(refused) | {None if k == "?" else int(k) for k in alerts}
        for n in sorted(keys, key=lambda x: (x is None, x if x is not None else 0)):
            v = cash.get(n)
            floor = floors.get(n, SHARD_FLOOR_OTHER)
            series = refused.get(n, {})
            low = v is not None and n in funded and v < floor
            hit = sum(series.values()) >= SHARD_REFUSAL_MIN
            key = "?" if n is None else str(n)
            rec = alerts.get(key)
            if low or hit:
                rec = rec or {"since": now}
                alerts[key] = rec
                if now_ts - rec.get("sent_ts", 0) >= SHARD_REALERT_HOURS * 3600:
                    msg = _shard_problem_message(n, v, floor, series, cash, now, rec)
                    out.append(msg)
                    rec["sent_ts"] = now_ts
                    log(f"[IMM-HEALTH] ! {msg[0]}")
                    if send:
                        alerter.send_message(msg[1], subject=msg[0])
            elif rec is not None and v is not None and v >= floor * SHARD_RECOVER_MULT:
                subj = f"Kalshi shard {n} cash OK again: ${v:,.2f}"
                body = (f"Kalshi exchange shard {n} has ${v:,.2f} of free cash again as of "
                        f"{now} (alert floor ${floor:,.0f}; flagged since "
                        f"{rec.get('since', '?')}).\n\nAll shards: {_all_shards_line(cash)}")
                out.append((subj, body))
                del alerts[key]
                log(f"[IMM-HEALTH] {subj}")
                if send:
                    alerter.send_message(body, subject=subj)
    except Exception as e:
        log(f"[IMM-HEALTH] ! shard cash check failed ({e!r}); continuing")
    return out


HEARTBEAT_ENV = "IMM_HEARTBEAT_URL"
HEARTBEAT_TIMEOUT_SECS = 10


def heartbeat(body: str = "") -> bool:
    """Ping the outside heartbeat (see OUTSIDE HEARTBEAT); True when it
    answered. No URL configured = nothing to do. A failed ping is logged and
    swallowed: the email alerting below must still run, and a missed ping is
    exactly what the outside check exists to notice."""
    url = (os.environ.get(HEARTBEAT_ENV) or _env_from_registry(HEARTBEAT_ENV)).strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8")[:10000],
                                     method="POST")
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT_SECS) as r:
            r.read()
        return True
    except Exception as e:
        log(f"[IMM-HEALTH] ! heartbeat ping failed: {e!r}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="print, send nothing")
    ap.add_argument("--test", action="store_true", help="force a test email")
    ap.add_argument("--shards", action="store_true",
                    help="dry-run the shard-cash check: print, send and save nothing")
    args = ap.parse_args(argv)

    if args.shards:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        st = copy.deepcopy(load_state())
        print(f"shard cash: {_all_shards_line(read_shard_cash())}")
        for subj, body in check_shard_cash(None, st, now, send=False):
            print(f"\n=== WOULD SEND: {subj}\n{body}")
        return 0

    scheduled = not (args.status or args.test or args.shards)
    try:
        ok, headline, detail = probe()
    except Exception as e:
        if scheduled:
            heartbeat(f"probe failed: {e!r}")
        raise
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"[IMM-HEALTH] {'OK' if ok else 'DOWN'} — {headline} — {detail}")
    if scheduled:
        heartbeat(f"IMM {'OK' if ok else 'DOWN'} -- {headline} -- {detail}")
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

    # Credit-ledger staleness. Deliberately runs BEFORE the transition check
    # below, which returns early whenever the bot's up/down state is unchanged
    # — a stale ledger has nothing to do with whether the process is alive.
    check_credit_staleness(alerter, prev, now)
    # Shard cash, same reason: independent of the bot's up/down transitions.
    check_shard_cash(alerter, prev, now)

    was_ok = prev.get("ok")
    if was_ok is not None and bool(was_ok) == ok:
        save_state(prev)              # keep any credit-alert bookkeeping
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
    # Merge, don't replace: check_credit_staleness records its own last-alert
    # date in this dict and a bare overwrite would re-arm it every transition.
    save_state({**prev, "ok": ok, "since": now, "headline": headline})
    return 0


if __name__ == "__main__":
    sys.exit(main())
