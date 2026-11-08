#!/usr/bin/env python3
"""What a TEXT from Jack may do (Jack 2026-11-08: "can i set it up so if i text
back and it goes to Claude?", "Yes, build it" (questions + stops), then "allow
sizing adjustments too. and allow refreshing the dashboards" on top of the
resume / un-halt / unwind / restart list behind a confirmation code).
sms_inbox.py runs a read-only Claude that names ONE action; sms_inbox runs it
here, so this file is the whole list of what a text can change. Nothing here
moves money, starts another bot, edits code or adds schedules.

Read-only:
    status | waves | alerts [N] | sizes          # sizes: every IMM_*_SIZE_MULT knob, launcher vs override
    describe <action...>                         # validates an action, prints what it would do (one line)
Immediate (reduce risk, or harmless; logged with the text that asked):
    stop-wave NAME | stop-series SERIES | pause-overnight | halt --confirm HALT
    unwind-only on | waves-unwind on
    refresh dashboard | imm-pocket | wave-pocket | all
Behind sms_inbox's confirmation code ("Reply YES 1234"):
    unhalt | resume-overnight | resume-wave NAME | unwind-only off | waves-unwind off
    restart [task] | size IMM_<NAME>_SIZE_MULT <x> | wave-set NAME scan_size|budget <n>
Every action takes --why "<Jack's text>".

Notes: stop-* move live list entries to the list's top-level "stopped" (the bot
never reads it) under the scanners' scan.lock; resume-wave brings back only the
ones a TEXT stopped (reason "Jack by text"), whose stop is still ahead -- markets
a session stopped stay stopped. Files are turned off by renaming to
<NAME>.off-<UTC stamp>, as the sessions do. size writes
run-logs/incentive-mm/env_overrides.txt (the launcher appends it to the bot's
env) and starts restart_imm.ps1 -Task, which hands the book over and waits for
the :50-:05 window while hourly temp is live. restart starts restart_imm.ps1
(default: new code, same env; "task": a full bounce for env changes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
LIST = os.path.join(STATUS_DIR, "post_vote_dead_books.json")
REGISTRY = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
LOCK = os.path.join(HERE, "run-logs", "pvd-scan", "scan.lock")
PAUSE = os.path.join(HERE, "run-logs", "pvd-scan", "OVERNIGHT_PAUSED")
ALERTS = os.path.join(HERE, "run-logs", "pvd-scan", "alerts_sent.jsonl")
LOG = os.path.join(HERE, "run-logs", "phone_actions.log")
HALT = os.path.join(STATUS_DIR, "HALT")
UNWIND = os.path.join(STATUS_DIR, "UNWIND_ONLY")
UNWIND_WAVES = os.path.join(STATUS_DIR, "UNWIND_ONLY_WAVES")
SUMMARY = os.path.join(STATUS_DIR, "dashboard", "imm_dashboard_summary.json")
STATUS = os.path.join(STATUS_DIR, "status_incentive_mm.json")
OVERRIDES = os.path.join(STATUS_DIR, "env_overrides.txt")
BOT_SRC = os.path.join(HERE, "incentive_mm.py")
LAUNCHER = os.path.join(HERE, "run_incentive_mm.ps1")
RESTART_PS1 = os.path.join(HERE, "restart_imm.ps1")
RESTART_LOG = os.path.join(HERE, "run-logs", "phone_restart.log")
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".claude", "imm-research-token.txt")
SIZE_MAX = 10.0
WAVE_LIMITS = {"scan_size": (1, 5000), "budget": (0, 300000)}
DASH_TASK = "KL imm dashboard"


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, obj) -> None:
    tmp = path + ".phone.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{now_z()} {msg}\n")


def live(e: dict, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(e.get("stop")).replace("Z", "+00:00")) > now
    except ValueError:
        return False


class ScanLock:
    """pvd_scan's lock: wait (up to 3 min) while a fresh one is held, then hold it."""
    def __enter__(self):
        t0 = time.time()
        while os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 1500:
            if time.time() - t0 > 180:
                raise SystemExit("a scan has held the list lock for 3+ min: try again shortly")
            time.sleep(2)
        open(LOCK, "w").close()
        return self

    def __exit__(self, *exc):
        try:
            os.remove(LOCK)
        except OSError:
            pass


def file_off(path: str) -> str:
    """Rename a control file to <name>.off-<stamp> (the sessions' convention)."""
    if not os.path.exists(path):
        return "already off"
    dst = f"{path}.off-{stamp()}"
    os.replace(path, dst)
    return f"renamed to {os.path.basename(dst)}"


def file_on(path: str, why: str) -> str:
    if os.path.exists(path):
        return "already on"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{now_z()} on by text: {why}\n")
    return "created"


# ---------------------------------------------------------------- sizes
def size_knobs() -> dict:
    """{env name: code default} for every IMM_*_SIZE_MULT read as a plain number."""
    out = {}
    try:
        src = open(BOT_SRC, encoding="utf-8").read()
    except OSError:
        return out
    for m in re.finditer(r'_env_float\("(IMM_[A-Z0-9_]+_SIZE_MULT)",\s*([0-9.]+)', src):
        out.setdefault(m.group(1), float(m.group(2)))
    return out


def launcher_values() -> dict:
    try:
        src = open(LAUNCHER, encoding="utf-8").read()
    except OSError:
        return {}
    i = src.rfind("$ProbeEnv = \"set ")
    line = src[i: src.find("\n", i)] if i >= 0 else ""
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"set (IMM_[A-Z0-9_]+_SIZE_MULT)=([0-9.]+)", line)}


def overrides() -> dict:
    out = {}
    try:
        for ln in open(OVERRIDES, encoding="utf-8"):
            m = re.match(r"^\s*(IMM_[A-Z0-9_]+_SIZE_MULT)=([0-9]+(?:\.[0-9]+)?)\s*$", ln)
            if m:
                out[m.group(1)] = float(m.group(2))
    except OSError:
        pass
    return out


def size_now(name: str) -> tuple:
    """(value the next restart uses, where it comes from)."""
    ov, la, kn = overrides(), launcher_values(), size_knobs()
    if name in ov:
        return ov[name], "text override"
    if name in la:
        return la[name], "launcher"
    return kn.get(name), "code default"


def fmt(x) -> str:
    return "?" if x is None else (f"x{x:g}")


def cmd_sizes(_a) -> int:
    kn = size_knobs()
    for name in sorted(kn):
        v, src = size_now(name)
        print(f"{name}: {fmt(v)} ({src}{'' if src == 'code default' else f'; code {fmt(kn[name])}'})")
    print(f"({len(kn)} knobs; a change applies at the next restart_imm.ps1 -Task)")
    return 0


# ---------------------------------------------------------------- describe / validate
def validate(parts: list) -> str:
    """One line saying what the action does; SystemExit if it isn't allowed or valid."""
    if not parts:
        raise SystemExit("no action")
    a = parts[0]
    reg = (load(REGISTRY, {}).get("waves") or {})
    if a in ("stop-wave", "resume-wave"):
        if len(parts) != 2 or parts[1] not in reg:
            raise SystemExit(f"no wave named {parts[1:] or '?'} (waves: {', '.join(reg) or 'none'})")
        if a == "stop-wave":
            return f"turn wave {parts[1]} off and stop its live list entries"
        st = (load(LIST, {}).get("stopped") or {})
        ser = set(reg[parts[1]].get("series") or []) | set(reg[parts[1]].get("near") or [])
        n = sum(1 for t, s in st.items() if t.split("-")[0] in ser and str(s.get("reason", "")).startswith("Jack by text")
                and live(s.get("entry") or {}, datetime.now(timezone.utc)))
        return f"turn wave {parts[1]} back ON and restore {n} market(s) stopped by text"
    if a == "stop-series":
        if len(parts) != 2 or not re.fullmatch(r"KX[A-Z0-9]+", parts[1].upper()):
            raise SystemExit("stop-series needs a KX... series ticker")
        return f"stop {parts[1].upper()}'s live list entries and take it out of every wave"
    if a in ("pause-overnight", "halt", "unhalt", "resume-overnight") and len(parts) == 1:
        return {"pause-overnight": "pause the overnight market-wave check",
                "halt": "HALT the whole bot (cancel every order, quote nothing)",
                "unhalt": "un-halt the bot (rename HALT): it quotes again within a cycle",
                "resume-overnight": "resume the overnight market-wave check (it can list waves again)"}[a]
    if a in ("unwind-only", "waves-unwind") and len(parts) == 2 and parts[1] in ("on", "off"):
        what = "UNWIND_ONLY (every market only unwinds)" if a == "unwind-only" else \
            "UNWIND_ONLY_WAVES (wave markets only unwind, at breakeven or better)"
        return f"turn {what} {parts[1].upper()}"
    if a == "restart" and (len(parts) == 1 or parts[1:] == ["task"]):
        return ("restart the bot (full -Task bounce, re-reads the launcher env)" if len(parts) == 2 else
                "restart the bot (new code, same env)") + "; book handed over; waits for :50-:05 if hourly temp is live"
    if a == "size" and len(parts) == 3:
        name = parts[1].upper()
        if name not in size_knobs():
            raise SystemExit(f"{name} is not a size knob (see: phone_actions.py sizes)")
        try:
            x = float(parts[2].lower().strip().strip("x"))
        except ValueError:
            raise SystemExit("size needs a number, e.g. size IMM_TRUMPVH_SIZE_MULT 5")
        if not 0 <= x <= SIZE_MAX:
            raise SystemExit(f"size must be between 0 and {SIZE_MAX:g}")
        cur, src = size_now(name)
        return f"{name} {fmt(cur)} -> {fmt(x)} (was {src}), then a -Task restart to apply it"
    if a == "wave-set" and len(parts) == 4:
        if parts[1] not in reg:
            raise SystemExit(f"no wave named {parts[1]}")
        if parts[2] not in WAVE_LIMITS:
            raise SystemExit("wave-set takes scan_size or budget")
        try:
            v = float(parts[3].replace(",", "").lower().rstrip("k")) * (1000 if parts[3].lower().endswith("k") else 1)
        except ValueError:
            raise SystemExit("wave-set needs a number")
        lo, hi = WAVE_LIMITS[parts[2]]
        if not lo <= v <= hi:
            raise SystemExit(f"{parts[2]} must be between {lo:,} and {hi:,}")
        return f"wave {parts[1]} {parts[2]} {reg[parts[1]].get(parts[2])} -> {v:g} (the next waves pass, <= 15 min)"
    if a == "refresh" and len(parts) == 2 and parts[1] in ("dashboard", "imm-pocket", "wave-pocket", "all"):
        return f"refresh {parts[1]}"
    raise SystemExit(f"not an action a text can do: {' '.join(parts)[:60]}")


def cmd_describe(a) -> int:
    print(validate(a.parts))
    return 0


# ---------------------------------------------------------------- read-only
def effective_mode(files: set) -> str:
    """What the bot's control files add up to (incentive_mm.py's UNWIND_* comments)."""
    if "HALT" in files:
        return "HALTED: every order cancelled, nothing quotes"
    parts = []
    if "UNWIND_ONLY" in files:
        parts.append("every market only unwinds" + (", EXCEPT listed wave markets, which quote both sides"
                                                    if "UNWIND_ONLY_EXCEPT_WAVES" in files else ""))
    if "UNWIND_ONLY_WAVES" in files:
        parts.append("listed wave markets only unwind (whole position, at breakeven or better)"
                     + ("" if "UNWIND_ONLY" in files else "; every other market quotes as usual"))
    if not parts:
        return "normal quoting" + (" (UNWIND_ONLY_EXCEPT_WAVES does nothing without UNWIND_ONLY)"
                                   if "UNWIND_ONLY_EXCEPT_WAVES" in files else "")
    return "; ".join(parts)


def cmd_status(_a) -> int:
    s = load(STATUS, {})
    age = None
    try:
        age = time.time() - os.path.getmtime(STATUS)
    except OSError:
        pass
    files = [f for f in ("HALT", "UNWIND_ONLY", "UNWIND_ONLY_EXCEPT_WAVES", "UNWIND_ONLY_WAVES")
             if os.path.exists(os.path.join(STATUS_DIR, f))]
    print(f"bot status file {'%.0f s old' % age if age is not None else 'missing'}"
          f" ({'running' if age is not None and age < 300 else 'STALE: the bot may be down'})")
    print("control files on: " + (", ".join(files) if files else "none (normal quoting)"))
    print("effective mode: " + effective_mode(set(files)))
    if s.get("halted_until"):
        print(f"halted until {s['halted_until']}")
    # THE P&L is the command center's (imm_dashboard: ET days, mid marks, the IMM book). Jack 2026-11-08:
    # "today's P&L is -3692 not -62" -- a text had quoted the bot's own pnl_today (5 AM CT day, no
    # restart carry), so the bot's P&L counters are no longer printed at all.
    summ = load(SUMMARY, {})
    w = summ.get("windows") or {}
    try:
        built = f"built {(time.time() - float(summ['generated'])) / 60:.0f} min ago"
    except (KeyError, TypeError, ValueError):
        built = "build time unknown"
    if w:
        print(f"P&L = the command center (imm_dashboard.html, ET days, mid marks; {built}; the ONLY P&L to quote):")
        for k in ("today", "yesterday", "24h", "7d"):
            if k in w:
                print(f"  {k} ({w[k].get('desc')}): trading P&L {w[k].get('pnl'):+,.2f}, "
                      f"modeled rewards {w[k].get('rew'):+,.2f}")
    else:
        print("P&L: the command center summary is missing (imm_dashboard_summary.json); say so, don't guess")
    print("bot's inventory (not P&L): " + ", ".join(
        f"{k} {s[k]}" for k in ("inventory_contracts", "fills_today") if k in s))
    print("overnight check: " + ("PAUSED (OVERNIGHT_PAUSED)" if os.path.exists(PAUSE) else "on"))
    ov = overrides()
    if ov:
        print("size overrides by text (apply at the next -Task restart): " +
              ", ".join(f"{k} {fmt(v)}" for k, v in ov.items()))
    return cmd_waves(None)


def cmd_waves(_a) -> int:
    reg = (load(REGISTRY, {}).get("waves") or {})
    mk = (load(LIST, {}).get("markets") or {})
    now = datetime.now(timezone.utc)
    for name, w in reg.items():
        ser = set(w.get("series") or []) | set(w.get("near") or [])
        n = sum(1 for t, e in mk.items() if t.split("-")[0] in ser and live(e, now))
        print(f"wave {name}: {'ON' if w.get('enabled', True) else 'off'}, {len(ser)} series, {n} live list entries, "
              f"scan_size {w.get('scan_size')}, budget {w.get('budget')}"
              + (f" ({', '.join(sorted(ser)[:6])}{' ...' if len(ser) > 6 else ''})" if ser else ""))
    if not reg:
        print("no waves in the registry")
    return 0


def cmd_alerts(a) -> int:
    try:
        with open(ALERTS, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
    except (OSError, ValueError):
        rows = []
    for r in rows[-(a.n or 3):]:
        print(f"--- {r.get('at')} ({r.get('kind')})\n{r.get('text')}")
    if not rows:
        print("no alerts logged yet")
    return 0


# ---------------------------------------------------------------- stops (immediate)
def stop_entries(series: set, why: str) -> int:
    """Move every live list entry of these series to "stopped"; returns how many."""
    now = datetime.now(timezone.utc)
    with ScanLock():
        d = load(LIST, None)
        if d is None:
            raise SystemExit("the list file could not be read: nothing changed")
        mk = d.get("markets") or {}
        st = d.setdefault("stopped", {})
        moved = [t for t, e in mk.items() if t.split("-")[0] in series and live(e, now)]
        for t in moved:
            st[t] = {"at": now_z(), "reason": f"Jack by text: {why}"[:300], "entry": mk.pop(t)}
        if moved:
            save(LIST, d)
    return len(moved)


def cmd_stop_wave(a) -> int:
    regd = load(REGISTRY, None)
    w = ((regd or {}).get("waves") or {}).get(a.name)
    if not w:
        names = ", ".join(((regd or {}).get("waves") or {}).keys()) or "none"
        raise SystemExit(f"no wave named {a.name} (waves: {names})")
    ser = set(w.get("series") or []) | set(w.get("near") or [])
    w["enabled"] = False
    w.setdefault("history", []).append(f"{now_z()} off by text: {a.why}"[:300])
    save(REGISTRY, regd)
    n = stop_entries(ser, a.why)
    log(f"stop-wave {a.name}: off, {n} list entries stopped ({a.why!r})")
    print(f"wave {a.name} is OFF; {n} live list entries stopped (the bot drops them at its next list reload)")
    return 0


def cmd_stop_series(a) -> int:
    s = a.series.strip().upper()
    if not s.startswith("KX"):
        raise SystemExit("a series ticker starts with KX")
    regd = load(REGISTRY, {"waves": {}})
    hit = []
    for name, w in (regd.get("waves") or {}).items():
        for k in ("series", "near"):
            if s in (w.get(k) or []):
                w[k] = [x for x in w[k] if x != s]
                hit.append(name)
    if hit:
        save(REGISTRY, regd)
    n = stop_entries({s}, a.why)
    log(f"stop-series {s}: {n} list entries stopped, out of waves {hit} ({a.why!r})")
    print(f"{s}: {n} live list entries stopped" + (f", taken out of {', '.join(sorted(set(hit)))}" if hit else "")
          + ". A series the bot quotes under its own family rules (not a wave) needs a blocklist change: "
            "that is for the Claude app.")
    return 0


def cmd_pause(a) -> int:
    with open(PAUSE, "w", encoding="utf-8") as fh:
        fh.write(f"{now_z()} paused by text: {a.why}\n")
    log(f"pause-overnight ({a.why!r})")
    print("the overnight market-wave check is PAUSED (it stops at its first step)")
    return 0


def cmd_halt(a) -> int:
    if a.confirm != "HALT":
        raise SystemExit("halt needs --confirm HALT")
    if os.path.exists(HALT):
        print("the bot is already halted (HALT exists)")
        return 0
    with open(HALT, "w", encoding="utf-8") as fh:
        fh.write(f"{now_z()} halted by text: {a.why}\n")
    log(f"HALT ({a.why!r})")
    print("HALT written: the bot cancels every order and quotes nothing until it is un-halted")
    return 0


# ---------------------------------------------------------------- mode files
def cmd_unhalt(a) -> int:
    r = file_off(HALT)
    log(f"unhalt: {r} ({a.why!r})")
    print(f"HALT {r}: the bot quotes again within a cycle (its own loss / net halts still apply)")
    return 0


def cmd_resume_overnight(a) -> int:
    r = file_off(PAUSE)
    log(f"resume-overnight: {r} ({a.why!r})")
    print(f"overnight check resumed (OVERNIGHT_PAUSED {r}); its next run is within 20 min of 6 PM-9 AM local")
    return 0


def cmd_mode(a) -> int:
    path = UNWIND if a.cmd == "unwind-only" else UNWIND_WAVES
    r = file_on(path, a.why) if a.state == "on" else file_off(path)
    log(f"{a.cmd} {a.state}: {os.path.basename(path)} {r} ({a.why!r})")
    print(f"{os.path.basename(path)} {a.state.upper()} ({r}); the bot reads it every cycle")
    return 0


def cmd_resume_wave(a) -> int:
    regd = load(REGISTRY, None)
    w = ((regd or {}).get("waves") or {}).get(a.name)
    if not w:
        raise SystemExit(f"no wave named {a.name}")
    ser = set(w.get("series") or []) | set(w.get("near") or [])
    now = datetime.now(timezone.utc)
    with ScanLock():
        d = load(LIST, None)
        if d is None:
            raise SystemExit("the list file could not be read: nothing changed")
        st, mk = d.setdefault("stopped", {}), d.setdefault("markets", {})
        back = [t for t, s in st.items() if t.split("-")[0] in ser
                and str(s.get("reason", "")).startswith("Jack by text") and live(s.get("entry") or {}, now)
                and t not in mk]
        for t in back:
            mk[t] = st.pop(t)["entry"]
        if back:
            save(LIST, d)
    w["enabled"] = True
    w.setdefault("history", []).append(f"{now_z()} on by text: {a.why}"[:300])
    save(REGISTRY, regd)
    log(f"resume-wave {a.name}: on, {len(back)} restored ({a.why!r})")
    print(f"wave {a.name} is ON; {len(back)} market(s) stopped by text restored (the bot reloads the list "
          f"within minutes; the waves pass relists relit programs)")
    return 0


# ---------------------------------------------------------------- restart / size / wave-set
def start_restart(task: bool, why: str) -> str:
    """Start restart_imm.ps1 detached (it outlives this task: breakaway), output to phone_restart.log."""
    flags = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000   # DETACHED, NEW_GROUP, NO_WINDOW, BREAKAWAY
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", RESTART_PS1] + (["-Task"] if task else [])
    with open(RESTART_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {now_z()} restart{' -Task' if task else ''} by text: {why}\n")
    out = open(RESTART_LOG, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(args, cwd=HERE, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
    except OSError:
        p = subprocess.Popen(args, cwd=HERE, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags & ~0x01000000, close_fds=True)
    return f"restart_imm.ps1{' -Task' if task else ''} started (pid {p.pid}); log run-logs/phone_restart.log"


def cmd_restart(a) -> int:
    r = start_restart(a.mode == "task", a.why)
    log(f"restart {a.mode or ''}: {r} ({a.why!r})")
    print(r + ". It waits for the :50-:05 window if hourly temp is live, then hands the book over.")
    return 0


def cmd_size(a) -> int:
    name = a.name.upper()
    validate(["size", name, a.value])              # SystemExit when not allowed
    x = float(a.value.lower().strip().strip("x"))
    before, src = size_now(name)
    ov = overrides()
    ov[name] = x
    tmp = OVERRIDES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# size overrides by text (phone_actions.py size); the launcher appends these to the bot's env.\n")
        for k, v in sorted(ov.items()):
            fh.write(f"{k}={v:g}\n")
    os.replace(tmp, OVERRIDES)
    r = start_restart(True, a.why)
    log(f"size {name} {fmt(before)} ({src}) -> {fmt(x)}; {r} ({a.why!r})")
    print(f"{name} {fmt(before)} -> {fmt(x)}: written; {r}. Applies when the restart finishes.")
    return 0


def cmd_wave_set(a) -> int:
    desc = validate(["wave-set", a.name, a.key, a.value])
    regd = load(REGISTRY, None)
    v = float(a.value.replace(",", "").lower().rstrip("k")) * (1000 if a.value.lower().endswith("k") else 1)
    regd["waves"][a.name][a.key] = int(v) if a.key == "scan_size" else v
    save(REGISTRY, regd)
    log(f"wave-set: {desc} ({a.why!r})")
    print(f"done: {desc}")
    return 0


# ---------------------------------------------------------------- refresh
def claude_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    try:
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok          # child processes only; never logged
    except OSError:
        pass
    return env


def cmd_refresh(a) -> int:
    which = ["dashboard", "imm-pocket", "wave-pocket"] if a.what == "all" else [a.what]
    rc, out = 0, []
    for w in which:
        if w == "dashboard":
            p = subprocess.run(["schtasks", "/run", "/tn", DASH_TASK], capture_output=True, text=True)
            ok = p.returncode == 0
            out.append(f"dashboard: {'rebuild started (task ' + DASH_TASK + ', ~1-3 min)' if ok else 'FAILED ' + (p.stderr or p.stdout).strip()[:120]}")
        else:
            script = "imm_pocket_publish.py" if w == "imm-pocket" else "wave_pocket_publish.py"
            try:
                p = subprocess.run([sys.executable, os.path.join(HERE, script)], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=claude_env(), cwd=HERE, timeout=540)
                tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
                last = tail[-1] if tail else (p.stderr or "").strip()[-160:]
                ok = p.returncode == 0
                out.append(f"{w}: {'published' if ok else 'FAILED'} -- {last[-160:]}")
            except subprocess.TimeoutExpired:
                ok = False
                out.append(f"{w}: FAILED (timed out)")
        rc = rc or (0 if ok else 1)
    log(f"refresh {a.what}: {' | '.join(out)} ({a.why!r})")
    print("; ".join(out) + ("" if a.what == "dashboard" else ". Reload the page on the phone."))
    return rc


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("waves")
    sub.add_parser("sizes")
    x = sub.add_parser("alerts")
    x.add_argument("n", nargs="?", type=int, default=3)
    x = sub.add_parser("describe")
    x.add_argument("parts", nargs="+")
    specs = {
        "stop-wave": (cmd_stop_wave, [("name", {})]),
        "stop-series": (cmd_stop_series, [("series", {})]),
        "pause-overnight": (cmd_pause, []),
        "halt": (cmd_halt, [("--confirm", {"required": True})]),
        "unhalt": (cmd_unhalt, []),
        "resume-overnight": (cmd_resume_overnight, []),
        "resume-wave": (cmd_resume_wave, [("name", {})]),
        "unwind-only": (cmd_mode, [("state", {"choices": ["on", "off"]})]),
        "waves-unwind": (cmd_mode, [("state", {"choices": ["on", "off"]})]),
        "restart": (cmd_restart, [("mode", {"nargs": "?", "choices": ["task"]})]),
        "size": (cmd_size, [("name", {}), ("value", {})]),
        "wave-set": (cmd_wave_set, [("name", {}), ("key", {"choices": list(WAVE_LIMITS)}), ("value", {})]),
        "refresh": (cmd_refresh, [("what", {"choices": ["dashboard", "imm-pocket", "wave-pocket", "all"]})]),
    }
    for name, (fn, args) in specs.items():
        x = sub.add_parser(name)
        for arg, kw in args:
            x.add_argument(arg, **kw)
        x.add_argument("--why", required=True, help="Jack's text that asked for it")
        x.set_defaults(fn=fn)
    a = ap.parse_args()
    fns = {"status": cmd_status, "waves": cmd_waves, "alerts": cmd_alerts, "sizes": cmd_sizes,
           "describe": cmd_describe}
    return (fns.get(a.cmd) or a.fn)(a)


if __name__ == "__main__":
    sys.exit(main())
